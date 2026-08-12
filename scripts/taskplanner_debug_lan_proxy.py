#!/usr/bin/env python3
"""Expose loopback-only Debug Mode ports on one wired LAN interface."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import fcntl
import ipaddress
import signal
import socket
import struct


SIOCGIFADDR = 0x8915
SIOCGIFNETMASK = 0x891B


@dataclass(frozen=True, slots=True)
class Route:
    listen_port: int
    target_host: str
    target_port: int


def parse_route(raw: str) -> Route:
    """Parse LISTEN_PORT=TARGET_HOST:TARGET_PORT."""

    try:
        listen_raw, target_raw = raw.split("=", 1)
        target_host, target_port_raw = target_raw.rsplit(":", 1)
        listen_port = int(listen_raw)
        target_port = int(target_port_raw)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "route must be LISTEN_PORT=TARGET_HOST:TARGET_PORT"
        ) from exc
    if not target_host or not 1 <= listen_port <= 65535 or not 1 <= target_port <= 65535:
        raise argparse.ArgumentTypeError("route host and ports are invalid")
    return Route(listen_port, target_host, target_port)


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


async def forward_connection(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    route: Route,
    allowed_network: ipaddress.IPv4Network,
) -> None:
    peer = writer.get_extra_info("peername")
    peer_host = str(peer[0]) if isinstance(peer, tuple) and peer else ""
    try:
        peer_address = ipaddress.ip_address(peer_host)
    except ValueError:
        peer_address = None
    if peer_address is None or peer_address not in allowed_network:
        print(
            f"Rejected non-LAN connection from {peer_host or 'unknown'} "
            f"to port {route.listen_port}",
            flush=True,
        )
        writer.close()
        await writer.wait_closed()
        return

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

    tasks = {
        asyncio.create_task(_copy_stream(reader, target_writer)),
        asyncio.create_task(_copy_stream(target_reader, writer)),
    }
    try:
        _done, pending = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
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
    interface: str, routes: list[Route], poll_sec: float
) -> None:
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)

    active_identity: tuple[str, str] | None = None
    servers: list[asyncio.AbstractServer] = []
    while not stop.is_set():
        try:
            address, allowed_network = interface_network(interface)
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
                    for route in routes:
                        server = await asyncio.start_server(
                            lambda reader, writer, selected=route, network=allowed_network: forward_connection(
                                reader,
                                writer,
                                route=selected,
                                allowed_network=network,
                            ),
                            host=address,
                            port=route.listen_port,
                        )
                        servers.append(server)
                except OSError as exc:
                    await close_servers(servers)
                    servers = []
                    print(f"LAN proxy bind failed on {address}: {exc}", flush=True)
                else:
                    active_identity = identity
                    rendered = ", ".join(
                        f"{address}:{route.listen_port} -> "
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
    parser.add_argument("--interface", required=True)
    parser.add_argument(
        "--route",
        action="append",
        type=parse_route,
        required=True,
        help="LISTEN_PORT=TARGET_HOST:TARGET_PORT (repeatable)",
    )
    parser.add_argument("--poll-sec", type=float, default=2.0)
    args = parser.parse_args()
    if args.poll_sec < 0.2:
        parser.error("--poll-sec must be at least 0.2")
    return args


def main() -> None:
    args = parse_args()
    asyncio.run(run_proxy(args.interface, args.route, args.poll_sec))


if __name__ == "__main__":
    main()
