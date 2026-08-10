import json

import pytest

from integration_debug.networking import (
    NETWORK_SETTINGS_SCHEMA,
    _default_ipv4_route,
    _ipv4_default_routes,
    _select_primary_interface,
    load_network_settings,
    validate_network_settings,
    validate_ping_target,
    write_network_settings,
)


def test_network_settings_accept_only_supported_ros_values() -> None:
    settings = validate_network_settings(
        {"domain_id": "97", "discovery_range": "subnet"}
    )
    assert settings["domain_id"] == 97
    assert settings["discovery_range"] == "SUBNET"
    assert settings["schema"] == NETWORK_SETTINGS_SCHEMA

    with pytest.raises(ValueError, match="0 and 232"):
        validate_network_settings({"domain_id": 233, "discovery_range": "SUBNET"})
    with pytest.raises(ValueError, match="LOCALHOST or SUBNET"):
        validate_network_settings({"domain_id": 97, "discovery_range": "OFF"})


def test_network_settings_round_trip_is_atomic_and_private(tmp_path) -> None:
    target = tmp_path / "network-settings.json"
    settings = validate_network_settings(
        {"domain_id": 42, "discovery_range": "LOCALHOST"}
    )
    write_network_settings(target, settings)
    loaded = load_network_settings(target)
    assert loaded is not None
    assert loaded["domain_id"] == 42
    assert loaded["discovery_range"] == "LOCALHOST"
    assert target.stat().st_mode & 0o777 == 0o600
    assert json.loads(target.read_text())["schema"] == NETWORK_SETTINGS_SCHEMA


def test_ping_target_rejects_names_multicast_and_ipv6() -> None:
    assert validate_ping_target("10.125.185.91") == "10.125.185.91"
    with pytest.raises(ValueError, match="IPv4"):
        validate_ping_target("partner.local")
    with pytest.raises(ValueError, match="unicast"):
        validate_ping_target("239.255.0.1")
    with pytest.raises(ValueError, match="IPv4"):
        validate_ping_target("::1")


def test_default_route_parser_uses_little_endian_gateway_and_lowest_metric(
    tmp_path,
) -> None:
    route = tmp_path / "route"
    route.write_text(
        "Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT\n"
        "wlan0 00000000 0100A8C0 0003 0 0 600 00000000 0 0 0\n"
        "enp13s0 00000000 01B97D0A 0003 0 0 100 00000000 0 0 0\n",
        encoding="utf-8",
    )
    assert _default_ipv4_route(route) == ("enp13s0", "10.125.185.1")
    assert _ipv4_default_routes(route) == [
        (100, "enp13s0", "10.125.185.1"),
        (600, "wlan0", "192.168.0.1"),
    ]


def test_configured_wired_interface_wins_without_an_ipv4_address() -> None:
    interfaces = [
        {
            "interface": "enp13s0",
            "address": "",
            "up": True,
            "loopback": False,
            "kind": "ethernet",
        },
        {
            "interface": "wlan0",
            "address": "10.228.39.105",
            "up": True,
            "loopback": False,
            "kind": "wifi",
        },
    ]
    assert _select_primary_interface(
        interfaces,
        default_interface="wlan0",
        preferred_interface="enp13s0",
    ) == ("enp13s0", "configured")


def test_default_route_remains_primary_without_an_explicit_preference() -> None:
    interfaces = [
        {
            "interface": "enp13s0",
            "address": "10.125.185.90",
            "up": True,
            "loopback": False,
            "kind": "ethernet",
        },
        {
            "interface": "wlan0",
            "address": "10.228.39.105",
            "up": True,
            "loopback": False,
            "kind": "wifi",
        },
    ]
    assert _select_primary_interface(
        interfaces,
        default_interface="wlan0",
        preferred_interface="",
    ) == ("wlan0", "default_route")
