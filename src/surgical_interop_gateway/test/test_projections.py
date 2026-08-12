from __future__ import annotations

from dataclasses import asdict
import threading
from types import SimpleNamespace

from surgical_interop_msgs.msg import BedRobotArmState, BedRobotArmStateArray

from surgical_interop_gateway.projections import (
    DT_ACCEPTED,
    MODEL_OBSERVED,
    freshness_from_receipt,
    project_clinical_observation,
    project_context,
    project_event,
    project_bed_robot_arm_state,
    project_instrument,
    project_skill_robot_status,
)
from surgical_interop_gateway.node import SurgicalInteropGateway


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


def test_external_retraction_arm_state_uses_document_fields_only():
    arm = SimpleNamespace(
        arm_id="arm_1",
        role="retraction",
        role_instance_id="army_navy",
        state="retracting",
        direct_teach_active=False,
        reason_code="ok",
    )
    stamp = SimpleNamespace(sec=10, nanosec=5)

    projected = asdict(project_bed_robot_arm_state(arm, stamp))

    assert projected["stamp"] == stamp
    assert projected["robot_id"] == "arm_1"
    assert projected["robot_type"] == "bed_retraction_arm"
    assert projected["connection_state"] == "unknown"
    assert projected["execution_state"] == "retracting"
    assert projected["active_command_id"] == ""
    assert projected["reason_code"] == "ok"


def _status_snapshot(procedure_type: str, roles: tuple[str, ...]) -> BedRobotArmStateArray:
    message = BedRobotArmStateArray()
    message.stamp.sec = 10
    message.procedure_type = procedure_type
    message.revision = 1
    for index, role_instance in enumerate(roles, start=1):
        arm = BedRobotArmState()
        arm.arm_id = f"arm_{index}"
        arm.role = "retraction"
        arm.role_instance_id = role_instance
        arm.state = "standby"
        arm.reason_code = "ok"
        message.arms.append(arm)
    return message


def test_gateway_rejects_invalid_controller_arm_layout():
    node = SurgicalInteropGateway.__new__(SurgicalInteropGateway)
    node._lock = threading.RLock()
    node._bed_robot_arm_status = None
    node._bed_robot_arm_revision = None
    node._bed_robot_arm_source_stamp_sec = None
    node._monotonic = lambda: 1.0
    node.get_logger = lambda: SimpleNamespace(warning=lambda *_: None)

    node._on_bed_robot_arm_status(
        _status_snapshot("thyroidectomy", ("left_malleable", "right_malleable"))
    )
    assert node._bed_robot_arm_status is None

    node._on_bed_robot_arm_status(
        _status_snapshot("thyroidectomy", ("army_navy",))
    )
    assert node._bed_robot_arm_status is not None


def test_gateway_rejects_non_increasing_controller_revision():
    node = SurgicalInteropGateway.__new__(SurgicalInteropGateway)
    node._lock = threading.RLock()
    node._bed_robot_arm_status = None
    node._bed_robot_arm_revision = None
    node._bed_robot_arm_source_stamp_sec = None
    node._monotonic = lambda: 1.0
    node.get_logger = lambda: SimpleNamespace(warning=lambda *_: None)

    current = _status_snapshot("thyroidectomy", ("army_navy",))
    current.revision = 8
    node._on_bed_robot_arm_status(current)
    assert node._bed_robot_arm_revision == 8

    stale = _status_snapshot("thyroidectomy", ("army_navy",))
    stale.revision = 7
    node._on_bed_robot_arm_status(stale)
    assert node._bed_robot_arm_revision == 8
    assert node._bed_robot_arm_status.message.revision == 8


def test_gateway_accepts_revision_reset_only_with_newer_source_stamp():
    node = SurgicalInteropGateway.__new__(SurgicalInteropGateway)
    node._lock = threading.RLock()
    node._bed_robot_arm_status = None
    node._bed_robot_arm_revision = None
    node._bed_robot_arm_source_stamp_sec = None
    node._monotonic = lambda: 1.0
    node.get_logger = lambda: SimpleNamespace(warning=lambda *_: None)

    current = _status_snapshot("thyroidectomy", ("army_navy",))
    current.stamp.sec = 100
    current.revision = 8
    node._on_bed_robot_arm_status(current)

    delayed = _status_snapshot("thyroidectomy", ("army_navy",))
    delayed.stamp.sec = 99
    delayed.revision = 9
    node._on_bed_robot_arm_status(delayed)
    assert node._bed_robot_arm_status.message.revision == 8

    restarted = _status_snapshot("thyroidectomy", ("army_navy",))
    restarted.stamp.sec = 101
    restarted.revision = 1
    node._on_bed_robot_arm_status(restarted)
    assert node._bed_robot_arm_status.message.revision == 1
    assert node._bed_robot_arm_source_stamp_sec == 101.0


def test_freshness_uses_receipt_time_and_has_missing_source_state():
    missing = freshness_from_receipt(None, now_monotonic_sec=10.0, stale_after_sec=2.0)
    fresh = freshness_from_receipt(8.5, now_monotonic_sec=10.0, stale_after_sec=2.0)
    stale = freshness_from_receipt(7.0, now_monotonic_sec=10.0, stale_after_sec=2.0)

    assert (missing.available, missing.fresh, missing.age_sec) == (False, False, -1.0)
    assert (fresh.available, fresh.fresh) == (True, True)
    assert (stale.available, stale.fresh) == (True, False)
