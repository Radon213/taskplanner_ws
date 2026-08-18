#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import io
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "taskplanner_ros_readiness.py"
SPEC = importlib.util.spec_from_file_location("taskplanner_ros_readiness", MODULE_PATH)
assert SPEC and SPEC.loader
readiness = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = readiness
SPEC.loader.exec_module(readiness)


class ReadinessPayloadTests(unittest.TestCase):
    def test_replay_requires_loaded_selected_error_free_case(self) -> None:
        valid = {
            "loaded": True,
            "state": "paused",
            "case_id": "0704_6",
            "duration_sec": 12.0,
            "last_error": "",
        }
        readiness.validate_payload("replay", valid, "0704_6")
        for key, value in (
            ("loaded", False),
            ("state", "error"),
            ("case_id", "wrong"),
            ("duration_sec", 0.0),
            ("last_error", "bag unavailable"),
        ):
            invalid = dict(valid)
            invalid[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                readiness.validate_payload("replay", invalid, "0704_6")

    def test_operational_state_requires_identity_bundle_and_instruments(self) -> None:
        valid = {
            "procedure_id": "thyroidectomy",
            "active_bundle": "thyroidectomy",
            "instrument_states": [{"instrument_id": "scalpel"}],
            "running": False,
            "execution_state": "idle",
        }
        readiness.validate_payload(
            "live", valid, expected_bundle="thyroidectomy"
        )
        for key, value in (
            ("procedure_id", ""),
            ("active_bundle", ""),
            ("instrument_states", []),
            ("running", True),
            ("execution_state", ""),
        ):
            invalid = dict(valid)
            invalid[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                readiness.validate_payload("llm-surgeon", invalid)

    def test_operational_state_rejects_wrong_selected_bundle(self) -> None:
        valid = {
            "procedure_id": "thyroidectomy",
            "active_bundle": "thyroidectomy",
            "instrument_states": [{"instrument_id": "scalpel"}],
            "running": False,
            "execution_state": "idle",
        }
        for key in ("procedure_id", "active_bundle"):
            invalid = dict(valid)
            invalid[key] = "nephrectomy"
            with self.subTest(key=key), self.assertRaises(ValueError):
                readiness.validate_payload(
                    "live", invalid, expected_bundle="thyroidectomy"
                )

    def test_operational_state_rejects_transitional_paused_and_unknown_states(self) -> None:
        valid = {
            "procedure_id": "thyroidectomy",
            "active_bundle": "thyroidectomy",
            "instrument_states": [{"instrument_id": "scalpel"}],
            "running": False,
            "execution_state": "idle",
        }
        for execution_state, running in (
            ("starting", False),
            ("stopping", False),
            ("resetting", False),
            ("garbage", False),
            ("paused", True),
        ):
            invalid = {**valid, "execution_state": execution_state, "running": running}
            with self.subTest(execution_state=execution_state), self.assertRaises(
                ValueError
            ):
                readiness.validate_payload("llm-surgeon", invalid)

    def test_debug_status_requires_schema_and_session_identity(self) -> None:
        payload = {
            "data": json.dumps(
                {
                    "schema": "taskplanner.integration_debug.status.v1",
                    "session": {"session_id": "debug-1"},
                }
            )
        }
        readiness.validate_payload("debug", payload)
        payload["data"] = json.dumps({"schema": "wrong", "session": {}})
        with self.assertRaises(ValueError):
            readiness.validate_payload("debug", payload)

    def test_cli_accepts_ros_echo_trailing_document_separator(self) -> None:
        previous_stdin = sys.stdin
        previous_argv = sys.argv
        try:
            sys.stdin = io.StringIO(
                "loaded: true\n"
                "state: paused\n"
                "case_id: '0704_6'\n"
                "duration_sec: 12.0\n"
                "last_error: ''\n"
                "---\n"
            )
            sys.argv = [
                str(MODULE_PATH),
                "--mode",
                "replay",
                "--expected-case",
                "0704_6",
            ]
            self.assertEqual(readiness.main(), 0)
        finally:
            sys.stdin = previous_stdin
            sys.argv = previous_argv


if __name__ == "__main__":
    unittest.main()
