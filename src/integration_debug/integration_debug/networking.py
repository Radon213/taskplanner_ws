"""Safe network inspection and ICMP diagnostics for integration Debug Mode."""

from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import struct
import time
from typing import Any
from uuid import uuid4
from xml.sax.saxutils import escape


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
_INTERFACE_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,15}$")


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


def write_fastdds_udp_profile(
    path: str | Path,
    interface: str,
    *,
    sys_class_net: str | Path = "/sys/class/net",
) -> None:
    """Write a Fast DDS UDP profile constrained to one verified host NIC."""

    selected = str(interface).strip()
    if not _INTERFACE_NAME.fullmatch(selected):
        raise ValueError("debug network interface name is invalid")
    if not (Path(sys_class_net) / selected).is_dir():
        raise ValueError(f"debug network interface does not exist: {selected}")
    rendered = f"""<?xml version="1.0" encoding="UTF-8" ?>
<profiles xmlns="http://www.eprosima.com/XMLSchemas/fastRTPS_Profiles">
  <transport_descriptors>
    <transport_descriptor>
      <transport_id>DebugUdpTransport</transport_id>
      <type>UDPv4</type>
      <interfaceWhiteList>
        <interface>{escape(selected)}</interface>
        <interface>lo</interface>
      </interfaceWhiteList>
    </transport_descriptor>
  </transport_descriptors>
  <participant profile_name="debug_udp_transport_profile" is_default_profile="true">
    <rtps>
      <userTransports>
        <transport_id>DebugUdpTransport</transport_id>
      </userTransports>
      <useBuiltinTransports>false</useBuiltinTransports>
    </rtps>
  </participant>
</profiles>
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    temporary.write_text(rendered, encoding="utf-8")
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


def _read_sysfs_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _interface_kind(name: str, sys_class_net: Path) -> str:
    interface_path = sys_class_net / name
    if (interface_path / "wireless").exists():
        return "wifi"
    if (interface_path / "device").exists():
        return "ethernet"
    return "virtual"


def _select_primary_interface(
    interfaces: list[dict[str, Any]],
    *,
    default_interface: str,
    preferred_interface: str,
) -> tuple[str, str]:
    names = {str(row["interface"]) for row in interfaces}
    if preferred_interface:
        if preferred_interface in names:
            return preferred_interface, "configured"
        return preferred_interface, "configured_missing"
    if default_interface in names:
        return default_interface, "default_route"
    for kind in ("ethernet", "wifi", "virtual"):
        candidate = next(
            (
                row
                for row in interfaces
                if row["up"]
                and not row["loopback"]
                and row["address"]
                and row["kind"] == kind
            ),
            None,
        )
        if candidate is not None:
            return str(candidate["interface"]), f"fallback_{kind}"
    return "", "unavailable"


def _ipv4_default_routes(
    path: str | Path = "/proc/net/route",
) -> list[tuple[int, str, str]]:
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()[1:]
    except OSError:
        return []
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
    return sorted(candidates)


def _default_ipv4_route(path: str | Path = "/proc/net/route") -> tuple[str, str]:
    candidates = _ipv4_default_routes(path)
    if candidates:
        _metric, interface, gateway = candidates[0]
        return interface, gateway
    return "", ""


def collect_network_status(
    *,
    preferred_interface: str | None = None,
    sys_class_net: str | Path = "/sys/class/net",
) -> dict[str, Any]:
    configured_interface = str(
        os.environ.get("TASKPLANNER_DEBUG_NETWORK_INTERFACE", "")
        if preferred_interface is None
        else preferred_interface
    ).strip()
    sysfs_root = Path(sys_class_net)
    default_routes = _ipv4_default_routes()
    if default_routes:
        _default_metric, default_interface, _default_gateway = default_routes[0]
    else:
        default_interface = ""
    interfaces: list[dict[str, Any]] = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        for _index, name in socket.if_nameindex():
            try:
                flags = struct.unpack(
                    "H", _interface_ioctl(probe, name, _SIOCGIFFLAGS)[16:18]
                )[0]
            except (OSError, ValueError, struct.error):
                continue
            address = ""
            prefix_length = 0
            try:
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
                address = ""
                prefix_length = 0
            interface_path = sysfs_root / name
            kind = _interface_kind(name, sysfs_root)
            carrier = _read_sysfs_text(interface_path / "carrier") == "1"
            operstate = _read_sysfs_text(interface_path / "operstate")
            interfaces.append(
                {
                    "interface": name,
                    "address": address,
                    "prefix_length": prefix_length,
                    "mac_address": _read_sysfs_text(interface_path / "address"),
                    "up": bool(flags & _IFF_UP),
                    "loopback": bool(flags & _IFF_LOOPBACK),
                    "multicast": bool(flags & _IFF_MULTICAST),
                    "carrier": carrier,
                    "operstate": operstate,
                    "kind": kind,
                }
            )

    selected_interface, selection_source = _select_primary_interface(
        interfaces,
        default_interface=default_interface,
        preferred_interface=configured_interface,
    )
    selected = next(
        (
            row
            for row in interfaces
            if str(row["interface"]) == selected_interface
        ),
        None,
    )
    addresses = [
        {
            **row,
            "primary": str(row["interface"]) == selected_interface,
        }
        for row in interfaces
        if row["address"]
    ]
    addresses.sort(
        key=lambda row: (
            not bool(row["primary"]),
            bool(row["loopback"]),
            str(row["interface"]),
        )
    )
    link_up = bool(
        selected
        and (
            selected["carrier"]
            if selected["kind"] in {"ethernet", "wifi"}
            else selected["operstate"] == "up"
        )
    )
    selected_gateway = next(
        (
            route_gateway
            for _metric, route_interface, route_gateway in default_routes
            if route_interface == selected_interface
        ),
        "",
    )
    return {
        "preferred_interface": configured_interface,
        "primary_interface": selected_interface,
        "primary_ipv4": str(selected["address"]) if selected else "",
        "prefix_length": int(selected["prefix_length"]) if selected else 0,
        "gateway_ipv4": selected_gateway,
        "multicast_capable": bool(selected and selected["multicast"]),
        "interface_present": selected is not None,
        "interface_kind": str(selected["kind"]) if selected else "unknown",
        "link_up": link_up,
        "selection_source": selection_source,
        "addresses": addresses,
    }
