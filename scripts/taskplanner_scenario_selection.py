#!/usr/bin/env python3
"""Read the small, owner-local ScenarioStore selection preference.

This tool intentionally has no ROS/Compose dependency.  The launch wrapper
uses it only to align cold-start defaults with ScenarioStore's last-good
selection; ScenarioStore remains the authority that parses the real bundle and
falls back safely if it was edited or removed.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


SELECTION_SCHEMA = "taskplanner.scenario_selection.v1"
MAX_SELECTION_BYTES = 16_384
_BUNDLE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def load_persisted_bundle(path: str | Path) -> str | None:
    """Return a path-safe saved bundle name, or ``None`` for unusable state."""

    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    if len(raw) > MAX_SELECTION_BYTES:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != SELECTION_SCHEMA:
        return None
    bundle_name = str(payload.get("bundle_name", "")).strip()
    if not _BUNDLE_NAME.fullmatch(bundle_name) or ".." in bundle_name:
        return None
    return bundle_name


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("selection_path", type=Path)
    args = parser.parse_args()
    bundle_name = load_persisted_bundle(args.selection_path)
    if bundle_name:
        print(bundle_name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
