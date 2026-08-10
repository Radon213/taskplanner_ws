"""Safe network inspection and ICMP diagnostics for integration Debug Mode."""

from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import socket
import struct
import time
from typing import Any
from uuid import uuid4


NETWORK_SETTINGS_SCHEMA = "taskplanner.integration_debug.network_settings.v1"
VALID_DISCOVERY_RANGES = {"LOCALHOST", "SUBNET"}
MIN_ROS_DOMAIN_ID = 0
MAX_ROS_DOMAIN_ID = 232

_SIOCGIFFLAGS = 0x8913
_SIOCGIFADDR = 0x8915
_SIOCGIFNETMASK = 0x891B
_IFF_UP = 0x1
_IFF_LOOPBACK = 0x8
_IFF_MULTICAST = 0x1000


def validate_network_settings(payload: dict[str, Any]) -> dict[str, Any]:
    raw_domain = payload.get("domain_id")
    if isinstance(raw_domain, bool) or raw_domain is None:
        raise ValueError("domain_id must be an integer between 0 and 232")
    if isinstance(raw_domain, float) and not raw_domain.is_integer():
        raise ValueError("domain_id must be an integer between 0 and 232")
    try:
        domain_id = int(str(raw_domain).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("domain_id must be an integer between 0 and 232") from exc
    if not MIN_ROS_DOMAIN_ID <= domain_id <= MAX_ROS_DOMAIN_ID:
        raise ValueError("domain_id must be an integer between 0 and 232")

    discovery_range = str(payload.get("discovery_range", "")).strip().upper()
    if discovery_range not in VALID_DISCOVERY_RANGES:
        raise ValueError("discovery_range must be LOCALHOST or SUBNET")
    return {
        "schema": NETWORK_SETTINGS_SCHEMA,
        "domain_id": domain_id,
        "discovery_range": discovery_range,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def write_network_settings(path: str | Path, settings: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(settings, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    temporary.write_text(encoded, encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, target)


def load_network_settings(path: str | Path) -> dict[str, Any] | None:
    target = Path(path)
    if not target.is_file():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"network settings file is invalid: {target}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != NETWORK_SETTINGS_SCHEMA:
        raise ValueError(f"network settings file is invalid: {target}")
    return validate_network_settings(payload)


def validate_ping_target(value: Any) -> str:
    text = str(value or "").strip()
    try:
        address = ipaddress.ip_address(text)
    except ValueError as exc:
        raise ValueError("target_ip must be a valid IPv4 address") from exc
    if not isinstance(address, ipaddress.IPv4Address):
        raise ValueError("target_ip must be a valid IPv4 address")
    if address.is_multicast or address.is_unspecified:
        raise ValueError("target_ip must be a unicast IPv4 address")
    return str(address)


def _checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return (~total) & 0xFFFF


def ping_ipv4(
    target: Any,
    *,
    count: int = 3,
    timeout_sec: float = 1.0,
    interval_sec: float = 0.15,
) -> dict[str, Any]:
    address = validate_ping_target(target)
    sent = 0
    round_trip_ms: list[float] = []
    source_ip = ""
    last_error = ""
    identifier = os.getpid() & 0xFFFF

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route_probe:
            route_probe.connect((address, 9))
            source_ip = str(route_probe.getsockname()[0] or "")
    except OSError:
        source_ip = ""

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP) as sock:
            sock.settimeout(timeout_sec)
            for sequence in range(1, max(1, count) + 1):
                payload = struct.pack("!d", time.monotonic()) + b"taskplanner-debug-ping"
                header = struct.pack("!BBHHH", 8, 0, 0, identifier, sequence)
                packet = header + payload
                packet = (
                    struct.pack(
                        "!BBHHH", 8, 0, _checksum(packet), identifier, sequence
                    )
                    + payload
                )
                started = time.monotonic()
                sent += 1
                try:
                    sock.sendto(packet, (address, 0))
                    deadline = started + timeout_sec
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0.0:
                            raise TimeoutError("request timed out")
                        sock.settimeout(remaining)
                        reply, peer = sock.recvfrom(4096)
                        if peer[0] != address or len(reply) < 8:
                            continue
                        icmp_type, icmp_code = reply[0], reply[1]
                        if icmp_type == 0 and icmp_code == 0:
                            round_trip_ms.append((time.monotonic() - started) * 1000.0)
                            break
                except (OSError, TimeoutError) as exc:
                    last_error = str(exc)
                if sequence < count:
                    time.sleep(interval_sec)
    except OSError as exc:
        last_error = str(exc)

    received = len(round_trip_ms)
    loss = 100.0 * (sent - received) / sent if sent else 100.0
    result: dict[str, Any] = {
        "target_ip": address,
        "source_ip": source_ip,
        "sent": sent,
        "received": received,
        "packet_loss_percent": round(loss, 1),
        "reachable": received > 0,
        "error": "" if received else (last_error or "no ICMP echo reply"),
        "rtt_ms": None,
    }
    if round_trip_ms:
        result["rtt_ms"] = {
            "min": round(min(round_trip_ms), 3),
            "avg": round(sum(round_trip_ms) / received, 3),
            "max": round(max(round_trip_ms), 3),
        }
    return result


def _interface_request(name: str) -> bytes:
    return struct.pack("256s", name.encode("utf-8")[:15])


def _interface_ioctl(sock: socket.socket, name: str, operation: int) -> bytes:
    return fcntl.ioctl(sock.fileno(), operation, _interface_request(name))


def _default_ipv4_route(path: str | Path = "/proc/net/route") -> tuple[str, str]:
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()[1:]
    except OSError:
        return "", ""
    candidates: list[tuple[int, str, str]] = []
    for line in lines:
        fields = line.split()
        if len(fields) < 8 or fields[1] != "00000000":
            continue
        try:
            flags = int(fields[3], 16)
            metric = int(fields[6])
            gateway = socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
        except (OSError, ValueError, struct.error):
            continue
        if flags & 0x1:
            candidates.append((metric, fields[0], gateway))
    if candidates:
        _metric, interface, gateway = min(candidates)
        return interface, gateway
    return "", ""


def collect_network_status() -> dict[str, Any]:
    default_interface, gateway = _default_ipv4_route()
    addresses: list[dict[str, Any]] = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        for _index, name in socket.if_nameindex():
            try:
                flags = struct.unpack(
                    "H", _interface_ioctl(probe, name, _SIOCGIFFLAGS)[16:18]
                )[0]
                address = socket.inet_ntoa(
                    _interface_ioctl(probe, name, _SIOCGIFADDR)[20:24]
                )
                netmask = socket.inet_ntoa(
                    _interface_ioctl(probe, name, _SIOCGIFNETMASK)[20:24]
                )
                prefix_length = ipaddress.IPv4Network(
                    f"0.0.0.0/{netmask}"
                ).prefixlen
            except (OSError, ValueError, struct.error):
                continue
            mac_path = Path("/sys/class/net") / name / "address"
            try:
                mac_address = mac_path.read_text(encoding="utf-8").strip()
            except OSError:
                mac_address = ""
            addresses.append(
                {
                    "interface": name,
                    "address": address,
                    "prefix_length": prefix_length,
                    "mac_address": mac_address,
                    "up": bool(flags & _IFF_UP),
                    "loopback": bool(flags & _IFF_LOOPBACK),
                    "multicast": bool(flags & _IFF_MULTICAST),
                    "primary": name == default_interface,
                }
            )
    addresses.sort(
        key=lambda row: (
            not bool(row["primary"]),
            bool(row["loopback"]),
            str(row["interface"]),
        )
    )
    primary = next((row for row in addresses if row["primary"]), None)
    if primary is None:
        primary = next(
            (row for row in addresses if row["up"] and not row["loopback"]),
            None,
        )
    return {
        "primary_interface": str(primary["interface"]) if primary else "",
        "primary_ipv4": str(primary["address"]) if primary else "",
        "prefix_length": int(primary["prefix_length"]) if primary else 0,
        "gateway_ipv4": gateway,
        "multicast_capable": bool(primary and primary["multicast"]),
        "addresses": addresses,
    }
