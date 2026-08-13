#!/usr/bin/env python3
"""Apply persisted Debug Mode ROS network settings, then exec the runtime."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

from integration_debug.networking import (
    load_network_settings,
    validate_network_settings,
    write_fastdds_udp_profile,
)


def _enabled(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _select_network_settings(
    settings_path: Path,
) -> tuple[dict[str, object] | None, bool]:
    """Resolve the network without letting saved standalone settings bypass runtime locks."""

    locked_to_runtime = _enabled(
        os.environ.get("TASKPLANNER_DEBUG_LOCK_TO_RUNTIME_NETWORK")
    )
    if locked_to_runtime:
        settings = validate_network_settings(
            {
                "domain_id": os.environ.get("ROS_DOMAIN_ID", "0"),
                "discovery_range": os.environ.get(
                    "ROS_AUTOMATIC_DISCOVERY_RANGE", "SUBNET"
                ),
            }
        )
        return settings, True
    try:
        return load_network_settings(settings_path), False
    except ValueError as exc:
        print(f"warning: {exc}; using Compose network settings", file=sys.stderr)
        return None, False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--settings", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        parser.error("a runtime command is required after --")

    settings_path = Path(args.settings)
    settings, locked_to_runtime = _select_network_settings(settings_path)
    discovery_range = str(
        settings["discovery_range"]
        if settings is not None
        else os.environ.get("ROS_AUTOMATIC_DISCOVERY_RANGE", "SUBNET")
    ).upper()
    debug_interface = os.environ.get(
        "TASKPLANNER_DEBUG_NETWORK_INTERFACE", ""
    ).strip()
    if not debug_interface:
        raise SystemExit(
            "TASKPLANNER_DEBUG_NETWORK_INTERFACE is required for Debug Mode"
        )
    if discovery_range == "LOCALHOST":
        # A physical-interface-only custom transport prevents Fast DDS from
        # advertising loopback locators. Built-in transports plus the ROS
        # discovery restriction provide the correct host-only boundary.
        os.environ.pop("FASTRTPS_DEFAULT_PROFILES_FILE", None)
        print(
            "Using built-in loopback DDS transport for LOCALHOST discovery",
            flush=True,
        )
    else:
        fastdds_profile = settings_path.parent / "fastdds_udp.xml"
        try:
            write_fastdds_udp_profile(fastdds_profile, debug_interface)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        os.environ["FASTRTPS_DEFAULT_PROFILES_FILE"] = str(fastdds_profile)
        print(
            f"Bound Debug Mode Fast DDS UDP transport to {debug_interface}",
            flush=True,
        )
    if settings is not None:
        os.environ["ROS_DOMAIN_ID"] = str(settings["domain_id"])
        os.environ["ROS_AUTOMATIC_DISCOVERY_RANGE"] = str(
            settings["discovery_range"]
        )
        if locked_to_runtime:
            print(
                "Locked integrated Debug Mode to runtime network: "
                f"domain={settings['domain_id']} "
                f"discovery={settings['discovery_range']}",
                flush=True,
            )
        else:
            print(
                "Loaded Debug Mode network settings: "
                f"domain={settings['domain_id']} "
                f"discovery={settings['discovery_range']}",
                flush=True,
            )
    os.environ["TASKPLANNER_DEBUG_NETWORK_SETTINGS"] = str(settings_path)
    os.execvpe(command[0], command, os.environ)


if __name__ == "__main__":
    main()
