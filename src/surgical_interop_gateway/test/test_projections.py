from __future__ import annotations

from dataclasses import asdict
from types import SimpleNamespace

from surgical_interop_gateway.projections import (
    DT_ACCEPTED,
    MODEL_OBSERVED,
    freshness_from_receipt,
    project_clinical_observation,
    project_context,
    project_event,
    project_instrument,
    project_skill_robot_status,
)


def test_context_is_only_dt_accepted_state_not_planner_predictions():
    world = SimpleNamespace(
        stamp=SimpleNamespace(sec=4, nanosec=0),
        procedure_id="thyroidectomy",
        running=True,
        execution_state="running",
        filtered_phase="inferior_pole_dissection",
        phase_confidence=0.91,
        phase_uncertain=False,
        safety_flags=["sterile_field"],
        predicted_tool="harmonic",
        surgeon_intent="hand_over_harmonic",
    )

    projected = asdict(project_context(world))

    assert projected == {
        "stamp": world.stamp,
        "procedure_type": "thyroidectomy",
        "procedure_active": True,
        "current_phase": "inferior_pole_dissection",
        "phase_confidence": 0.91,
        "phase_uncertain": False,
        "execution_state": "running",
        "safety_flags": ("sterile_field",),
        "evidence_status": DT_ACCEPTED,
    }
    assert "predicted_tool" not in projected
    assert "surgeon_intent" not in projected


def test_instrument_location_is_semantic_and_visibility_is_not_inferred():
    instrument = SimpleNamespace(
        stamp=SimpleNamespace(sec=5, nanosec=0),
        instrument_id="mayo_scissors",
        instance_id="mayo_scissors#1",
        location_type="mayo_tray",
        location_id="mayo_zone_a",
        owner="none",
        status="available",
        confidence=0.87,
        visual_anchor_id="mayo_zone_a",
        cleanliness_state="sterile",
        reserved_for="next_phase",
    )

    projected = asdict(project_instrument(instrument))

    assert projected["location_type"] == "mayo_tray"
    assert projected["location_id"] == "mayo_zone_a"
    assert projected["visible"] is False
    assert projected["evidence_status"] == DT_ACCEPTED
    assert "visual_anchor_id" not in projected
    assert "cleanliness_state" not in projected
    assert "reserved_for" not in projected


def test_event_projection_removes_detail_and_planner_intent():
    event = SimpleNamespace(
        stamp=SimpleNamespace(sec=7, nanosec=0),
        event_type="ToolHandoverCompleted",
        instrument_id="forceps",
        instance_id="forceps#2",
        phase_id="dissection",
        location_type="surgeon_hand",
        location_id="right",
        status="completed",
        confidence=1.0,
        detail_json='{"private":"payload"}',
        target_owner="surgeon",
        cleaning_required=True,
        mode="planned",
    )

    projected = asdict(project_event(event))

    assert projected["subject_type"] == "instrument"
    assert projected["subject_id"] == "forceps#2"
    assert projected["phase"] == "dissection"
    assert projected["state"] == "completed"
    assert projected["evidence_status"] == DT_ACCEPTED
    assert "detail_json" not in projected
    assert "target_owner" not in projected
    assert "cleaning_required" not in projected
    assert "mode" not in projected


def test_clinical_projection_never_leaks_raw_vlm_json_or_prediction_fields():
    result = SimpleNamespace(
        stamp=SimpleNamespace(sec=8, nanosec=0),
        source="cam4_vlm",
        summary="A clamp is visible near the surgical field.",
        phase_ids=["dissection"],
        phase_confidences=[0.75],
        observed_tool_ids=["clamp"],
        observed_location_ids=["surgical_field"],
        observed_location_types=["surgical_field"],
        observed_confidences=[0.84],
        gesture_event_type="",
        gesture_requested_tool="",
        gesture_hand_pose="",
        gesture_confidence=0.0,
        uncertainty=0.22,
        raw_json='{"reasoning":"do not publish"}',
        predicted_tool_ids=["forceps"],
    )

    projected = asdict(project_clinical_observation(result))

    assert projected["source"] == "cam4_vlm"
    assert projected["evidence_status"] == MODEL_OBSERVED
    assert "raw_json" not in projected
    assert "predicted_tool_ids" not in projected


def test_skill_status_does_not_fabricate_connection_state():
    status = SimpleNamespace(
        stamp=SimpleNamespace(sec=9, nanosec=0),
        state="executing",
        command_id="command-42",
        progress=0.4,
        success=True,
    )

    projected = project_skill_robot_status(status)

    assert projected.robot_id == "humanoid"
    assert projected.connection_state == "unknown"
    assert projected.reason_code == ""


def test_freshness_uses_receipt_time_and_has_missing_source_state():
    missing = freshness_from_receipt(None, now_monotonic_sec=10.0, stale_after_sec=2.0)
    fresh = freshness_from_receipt(8.5, now_monotonic_sec=10.0, stale_after_sec=2.0)
    stale = freshness_from_receipt(7.0, now_monotonic_sec=10.0, stale_after_sec=2.0)

    assert (missing.available, missing.fresh, missing.age_sec) == (False, False, -1.0)
    assert (fresh.available, fresh.fresh) == (True, True)
    assert (stale.available, stale.fresh) == (True, False)
