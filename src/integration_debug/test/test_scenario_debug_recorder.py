from __future__ import annotations

import json
import stat

from integration_debug.scenario_debug_recorder import SCHEMA, ScenarioDebugRecorder


def _read_rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_recorder_creates_a_private_timestamped_journal(tmp_path) -> None:
    recorder = ScenarioDebugRecorder(tmp_path / "scenario-recordings")

    path = recorder.start(
        procedure_run_id="run-01",
        context={"procedure_id": "thyroidectomy_demo"},
        source_stamp={"sec": 12, "nanosec": 345},
    )
    assert path is not None
    assert path.parent == tmp_path / "scenario-recordings"
    assert path.suffix == ".jsonl"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    assert recorder.append(
        "bt_decision",
        {"rationale": "prepared tool matches explicit request"},
        source_stamp={"sec": 13, "nanosec": 0},
    )
    assert recorder.stop("completed") == path

    rows = _read_rows(path)
    assert [row["record_type"] for row in rows] == [
        "recording_started",
        "bt_decision",
        "recording_stopped",
    ]
    assert all(row["schema"] == SCHEMA for row in rows)
    assert all(row["procedure_run_id"] == "run-01" for row in rows)
    assert all(row["recorded_at_utc"] for row in rows)
    assert rows[0]["source_stamp"] == {"sec": 12, "nanosec": 345}
    assert rows[1]["source_stamp"] == {"sec": 13, "nanosec": 0}
    assert rows[-1]["payload"]["reason"] == "completed"


def test_recorder_separates_changed_procedure_runs(tmp_path) -> None:
    recorder = ScenarioDebugRecorder(tmp_path)
    first = recorder.start(procedure_run_id="run-one", context={})
    assert first is not None
    second = recorder.start(procedure_run_id="run-two", context={})
    assert second is not None
    assert first != second
    recorder.stop("completed")

    first_rows = _read_rows(first)
    second_rows = _read_rows(second)
    assert first_rows[-1]["record_type"] == "recording_stopped"
    assert first_rows[-1]["payload"]["reason"] == "procedure_run_changed"
    assert second_rows[0]["procedure_run_id"] == "run-two"


def test_recorder_bounds_oversized_debug_text(tmp_path) -> None:
    recorder = ScenarioDebugRecorder(tmp_path)
    path = recorder.start(procedure_run_id="run-text", context={})
    assert path is not None
    recorder.append("dt_event", {"detail_json": "x" * 9_000})
    recorder.stop("completed")

    row = _read_rows(path)[1]
    detail = row["payload"]["detail_json"]
    assert len(detail) < 5_000
    assert "chars omitted" in detail
