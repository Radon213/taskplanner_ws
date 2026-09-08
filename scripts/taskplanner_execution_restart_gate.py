#!/usr/bin/env python3
"""Validate one bounded execution-route snapshot for owner restart."""

from __future__ import annotations

import argparse
import json
import math
import sys
import time

import yaml


MAX_INPUT_BYTES = 16 * 1024
SCHEMA = "taskplanner.execution_route_state.v1"


def parse_restart_gate(
    raw: bytes,
    *,
    now: float | None = None,
    max_age_sec: float = 3.0,
    future_tolerance_sec: float = 0.5,
) -> tuple[bool, str]:
    if not raw or len(raw) > MAX_INPUT_BYTES:
        raise ValueError("route state is empty or oversized")
    try:
        documents = list(yaml.safe_load_all(raw.decode("utf-8")))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ValueError("route state is not valid UTF-8 YAML") from exc
    message = next((item for item in documents if isinstance(item, dict)), None)
    if not isinstance(message, dict) or not isinstance(message.get("data"), str):
        raise ValueError("route state has no std_msgs/String data")
    data = message["data"]
    if len(data.encode("utf-8")) > MAX_INPUT_BYTES:
        raise ValueError("route state JSON is oversized")
    try:
        payload = json.loads(data)
    except ValueError as exc:
        raise ValueError("route state data is not JSON") from exc
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise ValueError("route state schema is unsupported")
    stamp = payload.get("stamp_sec")
    if not isinstance(stamp, (int, float)) or isinstance(stamp, bool):
        raise ValueError("route state stamp is invalid")
    stamp = float(stamp)
    if not math.isfinite(stamp):
        raise ValueError("route state stamp is invalid")
    age = (time.time() if now is None else float(now)) - stamp
    if age < -future_tolerance_sec or age > max_age_sec:
        raise ValueError("route state is stale")
    allowed = payload.get("restart_allowed")
    blocker = payload.get("restart_blocker")
    if not isinstance(allowed, bool) or not isinstance(blocker, str) or len(blocker) > 128:
        raise ValueError("route state restart fields are invalid")
    active_count = payload.get("active_request_count")
    if not isinstance(active_count, int) or isinstance(active_count, bool) or active_count < 0:
        raise ValueError("route state active request count is invalid")
    if allowed and (blocker or active_count != 0):
        raise ValueError("route state restart fields are inconsistent")
    if not allowed and not blocker:
        raise ValueError("route state blocker is missing")
    return allowed, blocker


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-age-sec", type=float, default=3.0)
    args = parser.parse_args()
    if not 0.1 <= args.max_age_sec <= 30.0:
        print("invalid max age", file=sys.stderr)
        return 2
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    try:
        allowed, blocker = parse_restart_gate(raw, max_age_sec=args.max_age_sec)
    except ValueError as exc:
        print(f"invalid:{exc}")
        return 2
    if not allowed:
        print(f"blocked:{blocker}")
        return 3
    print("allowed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
