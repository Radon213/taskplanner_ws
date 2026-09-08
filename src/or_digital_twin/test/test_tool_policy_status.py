"""Read-only policy projection: no ROS/hardware runtime is needed."""

import copy
from types import SimpleNamespace

import pytest

from or_digital_twin.tool_policy_status import tool_policy_status_payload


def _inputs():
    return {
        "state": SimpleNamespace(
            procedure_id="thyroidectomy_demo", procedure_run_id="run-1",
            running=True, execution_state="running", predicted_tool="T04",
            predicted_tool_confidence=0.42, predicted_tool_stability_sec=0.12,
        ),
        "instruments": {
            "T02#1": SimpleNamespace(instrument_id="T02"),
            "T02#2": SimpleNamespace(instrument_id="T02"),
            "T07#1": SimpleNamespace(instrument_id="T07"),
        },
        "prepare_probability_threshold": 0.21,
        "recovery_probability_threshold": 0.34,
        "dwell_sec": 0.7,
        "recovery_enabled_tools": frozenset({"T02", "T08"}),
        "recovery_dwell": {
            "T02#1": {"first_seen": 10.0, "last_seen": 10.25, "probability": 0.1},
            "T02#2": {"first_seen": 10.1, "last_seen": 10.25, "probability": 0.1},
            "T07#1": {"first_seen": 10.0, "last_seen": 10.25, "probability": 0.01},
            "gone": {"first_seen": 10.0, "last_seen": 10.25, "probability": 0.01},
        },
    }


def test_snapshot_copies_live_policy_and_per_instance_dwell_without_mutation():
    inputs = _inputs()
    before = copy.deepcopy(inputs)
    result = tool_policy_status_payload(**inputs)

    assert result["procedure_run_id"] == "run-1"
    assert result["source"] == "handover_ngram_0704"
    assert result["prepare"] == {
        "probability_threshold": 0.21, "comparison": "gte", "dwell_sec": 0.7,
        "candidate": {"instrument_id": "T04", "probability": 0.42, "stability_sec": 0.12},
    }
    recovery = result["recovery"]
    assert recovery["probability_threshold"] == 0.34
    assert recovery["comparison"] == "lte"
    assert recovery["dwell_sec"] == 0.7
    assert recovery["enabled_instrument_ids"] == ["T02", "T08"]
    assert [row["instance_id"] for row in recovery["candidates"]] == ["T02#1", "T02#2"]
    assert [row["stability_sec"] for row in recovery["candidates"]] == pytest.approx([0.25, 0.15])
    assert inputs == before


@pytest.mark.parametrize("lifecycle", ["idle", "starting", "paused", "halted", "completed"])
def test_inactive_lifecycle_keeps_configuration_but_never_old_countdowns(lifecycle):
    inputs = _inputs()
    inputs["state"].execution_state = lifecycle
    result = tool_policy_status_payload(**inputs)
    assert result["execution_state"] == lifecycle
    assert result["prepare"]["candidate"] is None
    assert result["recovery"]["candidates"] == []
    assert result["prepare"]["dwell_sec"] == 0.7


def test_empty_run_id_has_no_countdowns():
    inputs = _inputs()
    inputs["state"].procedure_run_id = ""
    result = tool_policy_status_payload(**inputs)
    assert result["prepare"]["candidate"] is None
    assert result["recovery"]["candidates"] == []


def test_mayo_camera_and_vlm_demand_confidence_cannot_change_policy_projection():
    inputs = _inputs()
    before = tool_policy_status_payload(**inputs)
    instrument = inputs["instruments"]["T02#1"]
    instrument.confidence = 0.99
    instrument.mayo_recovery_confidence = 0.95
    instrument.mayo_reuse_confidence = 0.88
    instrument.mayo_recovery_stability_sec = 99.0
    assert tool_policy_status_payload(**inputs) == before
