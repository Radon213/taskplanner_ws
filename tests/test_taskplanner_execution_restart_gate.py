from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "taskplanner_execution_restart_gate.py"
SPEC = importlib.util.spec_from_file_location("taskplanner_execution_restart_gate_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
gate = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gate
SPEC.loader.exec_module(gate)


def _message(**updates: object) -> bytes:
    payload: dict[str, object] = {
        "schema": gate.SCHEMA,
        "stamp_sec": 100.0,
        "active_request_count": 0,
        "restart_allowed": True,
        "restart_blocker": "",
    }
    payload.update(updates)
    return yaml.safe_dump({"data": json.dumps(payload)}).encode()


def test_allows_only_fresh_idle_snapshot() -> None:
    assert gate.parse_restart_gate(_message(), now=101.0) == (True, "")


def test_reports_execution_owned_blocker() -> None:
    assert gate.parse_restart_gate(
        _message(
            restart_allowed=False,
            restart_blocker="active_controller_request",
            active_request_count=1,
        ),
        now=101.0,
    ) == (False, "active_controller_request")


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"data: not-json\n",
        _message(schema="wrong"),
        _message(stamp_sec=90.0),
        _message(restart_allowed=True, active_request_count=1),
        _message(restart_allowed=False, restart_blocker=""),
    ],
)
def test_rejects_missing_stale_or_inconsistent_state(raw: bytes) -> None:
    with pytest.raises(ValueError):
        gate.parse_restart_gate(raw, now=101.0, max_age_sec=3.0)
