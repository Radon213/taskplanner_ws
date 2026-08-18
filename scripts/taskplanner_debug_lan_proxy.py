#!/usr/bin/env python3
"""Expose loopback-only bridges through a selected interface or path router."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import fcntl
import ipaddress
import signal
import socket
import struct
from urllib.parse import urlsplit


SIOCGIFADDR = 0x8915
SIOCGIFNETMASK = 0x891B


@dataclass(frozen=True, slots=True)
class Route:
    listen_port: int
    target_host: str
    target_port: int
    websocket_path: str | None = None


def parse_route(raw: str) -> Route:
    """Parse LISTEN_PORT[/WEBSOCKET_PATH]=TARGET_HOST:TARGET_PORT."""

    try:
        listen_spec, target_raw = raw.split("=", 1)
        listen_raw, separator, websocket_path = listen_spec.partition("/")
        if separator:
            websocket_path = f"/{websocket_path}"
        else:
            websocket_path = None
        target_host, target_port_raw = target_raw.rsplit(":", 1)
        listen_port = int(listen_raw)
        target_port = int(target_port_raw)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "route must be LISTEN_PORT=TARGET_HOST:TARGET_PORT"
        ) from exc
    if not target_host or not 1 <= listen_port <= 65535 or not 1 <= target_port <= 65535:
        raise argparse.ArgumentTypeError("route host and ports are invalid")
    if websocket_path is not None:
        parsed = urlsplit(websocket_path)
        if (
            websocket_path == "/"
            or parsed.scheme
            or parsed.netloc
            or parsed.path != websocket_path
            or parsed.query
            or parsed.fragment
        ):
            raise argparse.ArgumentTypeError(
                "websocket route path must be a non-root path without query or fragment"
            )
    return Route(listen_port, target_host, target_port, websocket_path)


def group_routes(routes: list[Route]) -> dict[int, tuple[Route, ...]]:
    """Group routes by listener and require one default target per port."""

    grouped: dict[int, list[Route]] = {}
    for route in routes:
        grouped.setdefault(route.listen_port, []).append(route)

    result: dict[int, tuple[Route, ...]] = {}
    for listen_port, grouped_routes in grouped.items():
        defaults = [route for route in grouped_routes if route.websocket_path is None]
        if len(defaults) != 1:
            raise ValueError(
                f"listener {listen_port} requires exactly one default --route target"
            )
        paths = [
            route.websocket_path
            for route in grouped_routes
            if route.websocket_path is not None
        ]
        if len(paths) != len(set(paths)):
            raise ValueError(f"listener {listen_port} has duplicate websocket route paths")
        result[listen_port] = tuple(grouped_routes)
    return result


def _interface_value(interface: str, request: int) -> str:
    encoded = interface.encode("utf-8")
    if not encoded or len(encoded) > 15:
        raise OSError("network interface name is invalid")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
        response = fcntl.ioctl(
            control.fileno(), request, struct.pack("256s", encoded)
        )
    return socket.inet_ntoa(response[20:24])


def interface_network(
    interface: str,
) -> tuple[str, ipaddress.IPv4Network]:
    address = _interface_value(interface, SIOCGIFADDR)
    netmask = _interface_value(interface, SIOCGIFNETMASK)
    network = ipaddress.ip_network(f"{address}/{netmask}", strict=False)
    if address.startswith("127."):
        raise OSError("LAN proxy cannot use a loopback interface")
    return address, network


async def _copy_stream(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    while chunk := await reader.read(64 * 1024):
        writer.write(chunk)
        await writer.drain()


def default_route(routes: tuple[Route, ...]) -> Route:
    return next(route for route in routes if route.websocket_path is None)


def select_route(
    routes: tuple[Route, ...], request_header: bytes
) -> tuple[Route, bytes]:
    """Select a websocket path target and rewrite it to the upstream root."""

    fallback = default_route(routes)
    if len(routes) == 1:
        return fallback, request_header
    try:
        request_line, remainder = request_header.split(b"\r\n", 1)
        method, raw_target, version = request_line.split(b" ", 2)
        if method != b"GET":
            return fallback, request_header
        parsed_target = urlsplit(raw_target.decode("ascii"))
        if parsed_target.scheme or parsed_target.netloc or parsed_target.fragment:
            return fallback, request_header
        request_path = parsed_target.path
    except (UnicodeDecodeError, ValueError):
        return fallback, request_header

    selected = next(
        (route for route in routes if route.websocket_path == request_path), fallback
    )
    if selected.websocket_path is None:
        return selected, request_header
    upstream_target = "/"
    if parsed_target.query:
        upstream_target += f"?{parsed_target.query}"
    return (
        selected,
        b" ".join((method, upstream_target.encode("ascii"), version))
        + b"\r\n"
        + remainder,
    )


async def select_connection_route(
    reader: asyncio.StreamReader, routes: tuple[Route, ...]
) -> tuple[Route, bytes] | None:
    """Read only the initial websocket handshake when path routing is enabled."""

    fallback = default_route(routes)
    if len(routes) == 1:
        return fallback, b""
    try:
        request_header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3.0)
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
        print("Rejected incomplete path-routed websocket request", flush=True)
        return None
    selected, rewritten_header = select_route(routes, request_header)
    print(
        "Path-routed websocket request -> "
        f"{selected.target_host}:{selected.target_port}",
        flush=True,
    )
    return selected, rewritten_header


async def forward_connection(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    routes: tuple[Route, ...],
    allowed_network: ipaddress.IPv4Network,
    allow_loopback_clients: bool,
) -> None:
    peer = writer.get_extra_info("peername")
    peer_host = str(peer[0]) if isinstance(peer, tuple) and peer else ""
    try:
        peer_address = ipaddress.ip_address(peer_host)
    except ValueError:
        peer_address = None
    is_allowed = peer_address is not None and (
        peer_address in allowed_network
        or (allow_loopback_clients and peer_address.is_loopback)
    )
    if not is_allowed:
        print(
            f"Rejected non-LAN connection from {peer_host or 'unknown'} "
            f"to port {default_route(routes).listen_port}",
            flush=True,
        )
        writer.close()
        await writer.wait_closed()
        return

    selected = await select_connection_route(reader, routes)
    if selected is None:
        writer.close()
        await writer.wait_closed()
        return
    route, request_header = selected
    try:
        target_reader, target_writer = await asyncio.open_connection(
            route.target_host, route.target_port
        )
    except OSError as exc:
        print(
            f"Target {route.target_host}:{route.target_port} unavailable: {exc}",
            flush=True,
        )
        writer.close()
        await writer.wait_closed()
        return

    tasks: set[asyncio.Task[None]] = set()
    try:
        if request_header:
            target_writer.write(request_header)
            await target_writer.drain()

        tasks = {
            asyncio.create_task(_copy_stream(reader, target_writer)),
            asyncio.create_task(_copy_stream(target_reader, writer)),
        }
        _done, pending = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        target_writer.close()
        writer.close()
        await asyncio.gather(
            target_writer.wait_closed(),
            writer.wait_closed(),
            return_exceptions=True,
        )


async def close_servers(servers: list[asyncio.AbstractServer]) -> None:
    for server in servers:
        server.close()
    await asyncio.gather(
        *(server.wait_closed() for server in servers),
        return_exceptions=True,
    )


async def run_proxy(
    interface: str | None,
    bind_address: str | None,
    routes: list[Route],
    poll_sec: float,
    allowed_network_override: ipaddress.IPv4Network | None = None,
    allow_loopback_clients: bool = False,
) -> None:
    routes_by_port = group_routes(routes)
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)

    active_identity: tuple[str, str] | None = None
    servers: list[asyncio.AbstractServer] = []
    while not stop.is_set():
        try:
            if bind_address is not None:
                address = bind_address
                interface_allowed_network = ipaddress.ip_network(
                    f"{address}/32", strict=False
                )
            else:
                assert interface is not None
                address, interface_allowed_network = interface_network(interface)
            allowed_network = allowed_network_override or interface_allowed_network
            identity = (address, str(allowed_network))
        except OSError as exc:
            identity = None
            print(f"Waiting for IPv4 on {interface}: {exc}", flush=True)

        if identity != active_identity:
            await close_servers(servers)
            servers = []
            active_identity = None
            if identity is not None:
                try:
                    for listen_port, selected_routes in routes_by_port.items():
                        server = await asyncio.start_server(
                            lambda reader, writer, selected=selected_routes, network=allowed_network: forward_connection(
                                reader,
                                writer,
                                routes=selected,
                                allowed_network=network,
                                allow_loopback_clients=allow_loopback_clients,
                            ),
                            host=address,
                            port=listen_port,
                        )
                        servers.append(server)
                except OSError as exc:
                    await close_servers(servers)
                    servers = []
                    print(f"LAN proxy bind failed on {address}: {exc}", flush=True)
                else:
                    active_identity = identity
                    rendered = ", ".join(
                        f"{address}:{route.listen_port}{route.websocket_path or ''} -> "
                        f"{route.target_host}:{route.target_port}"
                        for route in routes
                    )
                    print(
                        f"Debug LAN proxy ready for {allowed_network}: {rendered}",
                        flush=True,
                    )

        try:
            await asyncio.wait_for(stop.wait(), timeout=poll_sec)
        except TimeoutError:
            pass

    await close_servers(servers)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    listener = parser.add_mutually_exclusive_group(required=True)
    listener.add_argument("--interface")
    listener.add_argument("--bind-address")
    parser.add_argument(
        "--route",
        action="append",
        type=parse_route,
        required=True,
        help="LISTEN_PORT[/WEBSOCKET_PATH]=TARGET_HOST:TARGET_PORT (repeatable)",
    )
    parser.add_argument("--poll-sec", type=float, default=2.0)
    parser.add_argument(
        "--allow-network",
        type=lambda value: ipaddress.ip_network(value, strict=False),
        help=(
            "optional IPv4 client network allowed on the selected interface; "
            "required for point-to-point interfaces such as tailscale0"
        ),
    )
    parser.add_argument(
        "--allow-loopback-clients",
        action="store_true",
        help="also permit loopback clients when an internal router is required",
    )
    args = parser.parse_args()
    if args.poll_sec < 0.2:
        parser.error("--poll-sec must be at least 0.2")
    if args.allow_network is not None and args.allow_network.version != 4:
        parser.error("--allow-network must be an IPv4 network")
    if args.bind_address is not None:
        try:
            address = ipaddress.ip_address(args.bind_address)
        except ValueError as exc:
            parser.error(f"--bind-address must be an IPv4 address: {exc}")
        if address.version != 4:
            parser.error("--bind-address must be an IPv4 address")
        args.bind_address = str(address)
    try:
        group_routes(args.route)
    except ValueError as exc:
        parser.error(str(exc))
    return args


def main() -> None:
    args = parse_args()
    asyncio.run(
        run_proxy(
            args.interface,
            args.bind_address,
            args.route,
            args.poll_sec,
            args.allow_network,
            args.allow_loopback_clients,
        )
    )


if __name__ == "__main__":
    main()
