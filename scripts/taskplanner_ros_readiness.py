#!/usr/bin/env python3
"""Validate one ROS state sample used by the launcher readiness gate."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import yaml


def validate_payload(
    mode: str,
    payload: dict[str, Any],
    expected_case: str = "",
    expected_bundle: str = "",
) -> None:
    if mode == "replay":
        if payload.get("loaded") is not True:
            raise ValueError("replay source is not loaded")
        if str(payload.get("state", "")) not in {"ready", "running", "paused"}:
            raise ValueError("replay state is not ready, running, or paused")
        if expected_case and str(payload.get("case_id", "")) != expected_case:
            raise ValueError("replay case_id does not match the selected case")
        if float(payload.get("duration_sec", 0.0)) <= 0.0:
            raise ValueError("replay duration is empty")
        if str(payload.get("last_error", "")).strip():
            raise ValueError("replay state reports an error")
        return

    if mode in {"live", "llm-surgeon"}:
        procedure_id = str(payload.get("procedure_id", "")).strip()
        active_bundle = str(payload.get("active_bundle", "")).strip()
        if not procedure_id:
            raise ValueError("simulation procedure_id is empty")
        if not active_bundle:
            raise ValueError("simulation active_bundle is empty")
        if expected_bundle and active_bundle != expected_bundle:
            raise ValueError("simulation active_bundle does not match the selected bundle")
        if expected_bundle and procedure_id != expected_bundle:
            raise ValueError("simulation procedure_id does not match the selected bundle")
        instruments = payload.get("instrument_states")
        if not isinstance(instruments, list) or not instruments:
            raise ValueError("simulation instrument state is empty")
        running = payload.get("running")
        execution_state = str(payload.get("execution_state", "")).strip().lower()
        if not isinstance(running, bool):
            raise ValueError("simulation running state is missing")
        allowed_execution_states = {
            "idle",
            "running",
            "finishing",
            "halted",
            "completed",
        }
        if execution_state not in allowed_execution_states:
            raise ValueError("simulation execution_state is not a recognized ready state")
        active_execution = execution_state in {"running", "finishing"}
        if running != active_execution:
            raise ValueError("simulation running and execution_state disagree")
        return

    if mode == "debug":
        raw = payload.get("data")
        if not isinstance(raw, str):
            raise ValueError("debug status data is missing")
        status = json.loads(raw)
        if status.get("schema") != "taskplanner.integration_debug.status.v1":
            raise ValueError("debug status schema is invalid")
        session = status.get("session")
        if not isinstance(session, dict) or not str(session.get("session_id", "")).strip():
            raise ValueError("debug session identity is empty")
        return

    raise ValueError(f"unsupported runtime mode: {mode}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True)
    parser.add_argument("--expected-case", default="")
    parser.add_argument("--expected-bundle", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        payload = next(
            (document for document in yaml.safe_load_all(sys.stdin.read()) if document is not None),
            None,
        )
        if not isinstance(payload, dict):
            raise ValueError("ROS state sample is not a mapping")
        validate_payload(
            args.mode,
            payload,
            args.expected_case,
            args.expected_bundle,
        )
    except (ValueError, TypeError, json.JSONDecodeError, yaml.YAMLError) as error:
        print(f"semantic readiness rejected: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
