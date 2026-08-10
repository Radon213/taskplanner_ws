#!/usr/bin/env python3
"""Apply persisted Debug Mode ROS network settings, then exec the runtime."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

from integration_debug.networking import load_network_settings


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
    try:
        settings = load_network_settings(settings_path)
    except ValueError as exc:
        print(f"warning: {exc}; using Compose network settings", file=sys.stderr)
        settings = None
    if settings is not None:
        os.environ["ROS_DOMAIN_ID"] = str(settings["domain_id"])
        os.environ["ROS_AUTOMATIC_DISCOVERY_RANGE"] = str(
            settings["discovery_range"]
        )
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
