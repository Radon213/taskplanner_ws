from __future__ import annotations

from dataclasses import asdict
import json
import threading
from types import SimpleNamespace

from builtin_interfaces.msg import Time
from surgical_interop_msgs.msg import BedRobotArmState, BedRobotArmStateArray
from std_msgs.msg import String

from surgical_interop_gateway.projections import (
    DT_ACCEPTED,
    MODEL_OBSERVED,
    UNKNOWN,
    RobotEndEffectorProjection,
    ToolPredictionProjection,
    freshness_from_receipt,
    project_clinical_observation,
    project_context,
    project_event,
    project_bed_robot_arm_state,
    project_instrument,
    project_robot_end_effectors,
    project_skill_robot_status,
    project_tool_predictions,
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


def test_invalid_context_confidence_forces_uncertain_unknown_state():
    projected = project_context(
        SimpleNamespace(
            procedure_id="thyroidectomy",
            running=True,
            filtered_phase="P03",
            phase_confidence=float("nan"),
            phase_uncertain=False,
        )
    )

    assert projected.phase_confidence == 0.0
    assert projected.phase_uncertain is True
    assert projected.evidence_status == UNKNOWN


def test_tool_prediction_projects_only_reviewed_forecast_fields():
    world = SimpleNamespace(
        stamp=SimpleNamespace(sec=4, nanosec=0),
        predicted_tool="T09",
        predicted_tool_confidence=0.82,
        predicted_tool_stability_sec=1.4,
        surgeon_intent="private",
        pending_transition_tools=["T03"],
    )

    projected = [asdict(item) for item in project_tool_predictions(world)]

    assert projected == [
        {
            "stamp": world.stamp,
            "rank": 1,
            "instrument_id": "T09",
            "instance_id": "",
            "confidence": 0.82,
            "stability_sec": 1.4,
            "source": "digital_twin",
            "evidence_status": DT_ACCEPTED,
        }
    ]
    assert "surgeon_intent" not in projected[0]
    assert "pending_transition_tools" not in projected[0]


def test_tool_prediction_is_empty_without_a_current_forecast():
    assert project_tool_predictions(SimpleNamespace(predicted_tool="")) == ()


def test_tool_prediction_projects_reducer_accepted_top_three():
    world = SimpleNamespace(
        stamp=SimpleNamespace(sec=4, nanosec=0),
        predicted_tool="T02",
        predicted_tool_confidence=0.91,
        predicted_tool_stability_sec=3.4,
        ranked_tool_predictions=[
            SimpleNamespace(rank=1, instrument_id="T02", confidence=0.91, stability_sec=3.4),
            SimpleNamespace(rank=2, instrument_id="T04", confidence=0.73, stability_sec=0.0),
            SimpleNamespace(rank=3, instrument_id="T07", confidence=0.61, stability_sec=0.0),
        ],
    )

    projected = project_tool_predictions(world)

    assert [row.rank for row in projected] == [1, 2, 3]
    assert [row.instrument_id for row in projected] == ["T02", "T04", "T07"]
    assert [row.confidence for row in projected] == [0.91, 0.73, 0.61]
    assert [row.stability_sec for row in projected] == [3.4, 0.0, 0.0]


def test_ranked_prediction_snapshot_fails_closed_as_one_unit():
    valid_rows = [
        SimpleNamespace(rank=1, instrument_id="T02", confidence=0.91, stability_sec=3.4),
        SimpleNamespace(rank=2, instrument_id="T04", confidence=0.73, stability_sec=0.0),
    ]
    malformed_snapshots = [
        [valid_rows[0], SimpleNamespace(rank=3, instrument_id="T04", confidence=0.73, stability_sec=0.0)],
        [valid_rows[0], SimpleNamespace(rank=2, instrument_id="T02", confidence=0.73, stability_sec=0.0)],
        [valid_rows[0], SimpleNamespace(rank=2, instrument_id="T04", confidence=0.99, stability_sec=0.0)],
        valid_rows + [
            SimpleNamespace(rank=3, instrument_id="T07", confidence=0.61, stability_sec=0.0),
            SimpleNamespace(rank=4, instrument_id="T08", confidence=0.55, stability_sec=0.0),
        ],
    ]
    for rows in malformed_snapshots:
        world = SimpleNamespace(
            predicted_tool="T02",
            predicted_tool_confidence=0.91,
            predicted_tool_stability_sec=3.4,
            ranked_tool_predictions=rows,
        )
        assert project_tool_predictions(world) == ()

    scalar_mismatch = SimpleNamespace(
        predicted_tool="T99",
        predicted_tool_confidence=0.91,
        predicted_tool_stability_sec=3.4,
        ranked_tool_predictions=valid_rows,
    )
    assert project_tool_predictions(scalar_mismatch) == ()


def test_empty_ranked_prediction_field_does_not_fall_back_to_scalar():
    world = SimpleNamespace(
        predicted_tool="T02",
        predicted_tool_confidence=0.91,
        predicted_tool_stability_sec=3.4,
        ranked_tool_predictions=[],
    )

    assert project_tool_predictions(world) == ()


def test_tool_prediction_drops_non_finite_or_out_of_range_numeric_claims():
    for confidence in (float("nan"), float("inf"), -0.01, 1.01):
        world = SimpleNamespace(
            predicted_tool="T09",
            predicted_tool_confidence=confidence,
            predicted_tool_stability_sec=1.0,
        )
        assert project_tool_predictions(world) == ()

    world = SimpleNamespace(
        predicted_tool="T09",
        predicted_tool_confidence=0.8,
        predicted_tool_stability_sec=float("nan"),
    )
    assert project_tool_predictions(world) == ()


def test_public_prediction_converter_defensively_rejects_nan():
    projection = ToolPredictionProjection(
        stamp=Time(sec=1),
        rank=1,
        instrument_id="T09",
        instance_id="",
        confidence=float("nan"),
        stability_sec=1.0,
        source="digital_twin",
    )

    assert SurgicalInteropGateway._to_public_prediction(projection) is None


def test_robot_end_effectors_distinguish_holding_from_known_empty():
    world = SimpleNamespace(
        stamp=SimpleNamespace(sec=4, nanosec=0),
        right_hand_tool="T04",
        right_hand_tool_instance_id="T04#1",
        left_hand_tool="",
        left_hand_tool_instance_id="",
    )

    projected = [asdict(item) for item in project_robot_end_effectors(world)]

    assert [(item["end_effector_id"], item["state"]) for item in projected] == [
        ("right_hand", "HOLDING"),
        ("left_hand", "EMPTY"),
    ]
    assert projected[0]["instrument_id"] == "T04"
    assert projected[0]["instance_id"] == "T04#1"
    assert projected[1]["instrument_id"] == ""


def test_public_end_effector_converter_turns_nan_into_unknown_without_tool_claim():
    projection = RobotEndEffectorProjection(
        stamp=Time(sec=1),
        robot_id="humanoid",
        end_effector_id="right_hand",
        state="HOLDING",
        instrument_id="T04",
        instance_id="T04#1",
        confidence=float("nan"),
    )

    message = SurgicalInteropGateway._to_public_end_effector(projection)

    assert message.state == message.STATE_UNKNOWN
    assert message.instrument_id == ""
    assert message.instance_id == ""
    assert message.confidence == 0.0
    assert message.evidence_status == UNKNOWN


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

    assert projected["location_type"] == "mayo_stand"
    assert projected["location_id"] == "mayo_stand"
    assert projected["visible"] is False
    assert projected["evidence_status"] == DT_ACCEPTED
    assert "visual_anchor_id" not in projected
    assert "cleanliness_state" not in projected
    assert "reserved_for" not in projected


def test_invalid_instrument_confidence_is_explicitly_unknown():
    projected = project_instrument(
        SimpleNamespace(instrument_id="T01", confidence=float("nan"))
    )

    assert projected.confidence == 0.0
    assert projected.evidence_status == UNKNOWN


def test_public_instrument_locations_hide_internal_policy_and_planner_zones():
    mayo_reuse = project_instrument(
        SimpleNamespace(
            instrument_id="T01",
            instance_id="T01#1",
            location_type="mayo_reuse_zone",
            location_id="mayo_reuse_zone",
            owner="none",
            status="parked_for_reuse",
            confidence=0.9,
        )
    )
    mayo_recovery = project_instrument(
        SimpleNamespace(
            instrument_id="T04",
            instance_id="T04#1",
            location_type="mayo_recovery_zone",
            location_id="mayo_recovery_zone",
            owner="none",
            status="awaiting_retrieval",
            confidence=0.9,
        )
    )
    surgeon_field = project_instrument(
        SimpleNamespace(
            instrument_id="T03",
            instance_id="T03#1",
            location_type="surgical_field",
            location_id="field_region_procedure",
            owner="surgeon",
            status="in_use",
            confidence=1.0,
        )
    )

    assert (mayo_reuse.location_type, mayo_reuse.location_id) == (
        "mayo_stand",
        "mayo_stand",
    )
    assert (mayo_recovery.location_type, mayo_recovery.location_id) == (
        "mayo_stand",
        "mayo_stand",
    )
    assert mayo_reuse.state == "parked_for_reuse"
    assert mayo_recovery.state == "awaiting_retrieval"
    assert (surgeon_field.location_type, surgeon_field.location_id) == (
        "surgeon",
        "surgeon",
    )
    assert surgeon_field.holder_role == "surgeon"


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
    assert projected["location_type"] == "surgeon"
    assert projected["location_id"] == "surgeon"
    assert projected["state"] == "completed"
    assert projected["evidence_status"] == DT_ACCEPTED
    assert "detail_json" not in projected
    assert "target_owner" not in projected
    assert "cleaning_required" not in projected
    assert "mode" not in projected


def test_event_projection_makes_rejection_outcome_explicit():
    event = SimpleNamespace(
        event_type="PhaseTransitionRejected",
        phase_id="P03",
        status="",
        confidence=0.3,
        detail_json='{"reason":"insufficient evidence"}',
    )

    projected = asdict(project_event(event))

    assert projected["state"] == "rejected"
    # Evidence acceptance is distinct from operation outcome.
    assert projected["evidence_status"] == DT_ACCEPTED


def test_event_projection_exposes_only_command_correlation_from_private_detail():
    event = SimpleNamespace(
        event_type="ToolHandoverCompleted",
        status="completed",
        confidence=1.0,
        detail_json='{"command_id":"cmd-7","private":"never publish"}',
    )

    projected = asdict(project_event(event))

    assert projected["correlation_id"] == "cmd-7"
    assert "private" not in projected


def test_invalid_event_confidence_is_finite_and_unknown():
    projected = project_event(
        SimpleNamespace(event_type="ToolObserved", confidence=float("inf"))
    )

    assert projected.confidence == 0.0
    assert projected.evidence_status == UNKNOWN


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
    assert projected["observed_location_types"] == ("surgeon",)
    assert projected["observed_location_ids"] == ("surgeon",)
    assert "raw_json" not in projected
    assert "predicted_tool_ids" not in projected


def test_clinical_projection_drops_misaligned_parallel_groups():
    result = SimpleNamespace(
        phase_ids=["P01", "P02"],
        phase_confidences=[0.9],
        observed_tool_ids=["T01"],
        observed_location_ids=["mayo", "field"],
        observed_location_types=["mayo_tray"],
        observed_confidences=[0.8],
        gesture_confidence=0.0,
        uncertainty=0.2,
    )

    projected = project_clinical_observation(result)

    assert projected.phase_ids == ()
    assert projected.phase_confidences == ()
    assert projected.observed_tool_ids == ()
    assert projected.observed_location_ids == ()
    assert projected.observed_location_types == ()
    assert projected.observed_confidences == ()
    assert projected.evidence_status == UNKNOWN


def test_clinical_projection_drops_only_bad_rows_and_maximizes_bad_uncertainty():
    result = SimpleNamespace(
        phase_ids=["P01", "P02"],
        phase_confidences=[0.9, float("nan")],
        observed_tool_ids=["T01", "T02"],
        observed_location_ids=["mayo", "field"],
        observed_location_types=["mayo_tray", "surgical_field"],
        observed_confidences=[float("inf"), 0.7],
        gesture_event_type="request_tool",
        gesture_requested_tool="T01",
        gesture_hand_pose="open_receive",
        gesture_confidence=1.2,
        uncertainty=float("nan"),
    )

    projected = project_clinical_observation(result)

    assert projected.phase_ids == ("P01",)
    assert projected.phase_confidences == (0.9,)
    assert projected.observed_tool_ids == ("T02",)
    assert projected.observed_location_ids == ("surgeon",)
    assert projected.observed_location_types == ("surgeon",)
    assert projected.observed_confidences == (0.7,)
    assert projected.gesture_event_type == ""
    assert projected.gesture_requested_tool == ""
    assert projected.gesture_hand_pose == ""
    assert projected.gesture_confidence == 0.0
    assert projected.uncertainty == 1.0
    assert projected.evidence_status == UNKNOWN


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


def test_invalid_robot_progress_is_finite_and_unknown():
    projected = project_skill_robot_status(
        SimpleNamespace(
            state="executing",
            command_id="command-42",
            progress=float("nan"),
            success=True,
        )
    )

    message = SurgicalInteropGateway._to_public_robot(projected)

    assert message.progress == 0.0
    assert message.evidence_status == UNKNOWN


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


def _event_test_node(
    *, running: bool, received_at: float, now: float, source_stamp_sec: int = 8
):
    node = SurgicalInteropGateway.__new__(SurgicalInteropGateway)
    node._lock = threading.RLock()
    node._world_stale_after_sec = 3.0
    node._last_procedure_active = running
    node._procedure_spec = SimpleNamespace(procedure_id="thyroidectomy")
    node._world = SimpleNamespace(
        message=SimpleNamespace(
            running=running,
            procedure_id="thyroidectomy",
            stamp=Time(sec=source_stamp_sec),
        ),
        received_monotonic_sec=received_at,
    )
    node._event_sequence = 0
    node._gateway_instance_id = "gateway-test"
    node._procedure_run_id = "run-test" if running else ""
    node._procedure_run_start_source_stamp_sec = (
        float(source_stamp_sec) if running else None
    )
    node._catalog_version = "sha256:test"
    node._vlm_result = None
    node._skill_status = None
    node._bed_robot_arm_status = None
    node._bed_robot_arm_revision = None
    node._bed_robot_arm_source_stamp_sec = None
    node._speech_text = None
    node._speech_sequence = 0
    node._monotonic = lambda: now
    published: list[object] = []
    node._events_pub = SimpleNamespace(publish=published.append)
    node.get_logger = lambda: SimpleNamespace(
        error=lambda *_: None, warning=lambda *_: None
    )
    node._stamp_or_now = lambda stamp: stamp or Time()
    return node, published


def _health_mismatch_test_node():
    node = SurgicalInteropGateway.__new__(SurgicalInteropGateway)
    node._SOURCE_NAMES = SurgicalInteropGateway._SOURCE_NAMES
    node._required_health_sources = {"world_state"}
    node._procedure_mismatch = True
    node._vlm_health = None
    node._skill_status = None
    node._bed_robot_arm_status = None
    node._input_statuses = {}
    node._lock = threading.RLock()
    node.get_clock = lambda: SimpleNamespace(
        now=lambda: SimpleNamespace(to_msg=lambda: Time())
    )
    return node


def test_health_reports_procedure_catalog_mismatch():
    node = _health_mismatch_test_node()
    fresh = {
        name: SimpleNamespace(available=True, fresh=True)
        for name in node._SOURCE_NAMES
    }

    message = node._health_message(revision=1, fresh=fresh)

    assert message.healthy is False
    assert message.state == "degraded"
    assert "procedure_catalog_mismatch" in message.error_codes


def _lifecycle_test_node():
    node = SurgicalInteropGateway.__new__(SurgicalInteropGateway)
    node._lock = threading.RLock()
    node._world = None
    node._vlm_result = SimpleNamespace(message="old-vlm")
    node._skill_status = SimpleNamespace(message="old-skill")
    node._bed_robot_arm_status = SimpleNamespace(message="old-arm")
    node._bed_robot_arm_revision = 9
    node._bed_robot_arm_source_stamp_sec = 9.0
    node._speech_text = SimpleNamespace(message="old-speech")
    node._event_sequence = 7
    node._clinical_sequence = 8
    node._speech_sequence = 9
    node._procedure_run_id = ""
    node._procedure_run_start_source_stamp_sec = None
    node._gateway_instance_id = "gateway-1"
    node._catalog_version = "sha256:test"
    node._last_procedure_active = False
    node._procedure_mismatch = False
    node._procedure_spec = SimpleNamespace(procedure_id="thyroidectomy")
    node._world_stale_after_sec = 3.0
    node._monotonic = lambda: 10.0
    node.get_logger = lambda: SimpleNamespace(
        error=lambda *_: None, warning=lambda *_: None
    )
    return node


def test_world_start_establishes_run_before_first_event_and_does_not_reset_twice():
    node = _lifecycle_test_node()
    published: list[object] = []
    node._events_pub = SimpleNamespace(publish=published.append)
    node._stamp_or_now = lambda stamp: stamp or Time()

    node._on_world(
        SimpleNamespace(
            running=True, procedure_id="thyroidectomy", stamp=Time(sec=10)
        )
    )
    first_run_id = node._procedure_run_id
    node._on_event(
        SimpleNamespace(
            event_type="PhaseTransitionAccepted", stamp=Time(sec=10), confidence=1.0
        )
    )
    node._on_world(
        SimpleNamespace(
            running=True, procedure_id="thyroidectomy", stamp=Time(sec=11)
        )
    )
    node._on_event(
        SimpleNamespace(
            event_type="PhaseTransitionAccepted", stamp=Time(sec=11), confidence=1.0
        )
    )

    assert first_run_id
    assert node._procedure_run_id == first_run_id
    assert node._event_sequence == 9
    assert len(published) == 2
    assert published[0].gateway_instance_id == "gateway-1"
    assert published[0].procedure_run_id == first_run_id
    assert published[0].procedure_type == "thyroidectomy"
    assert published[0].schema_version == "1.1.0"
    assert published[0].catalog_version == "sha256:test"


def test_event_identity_distinguishes_new_run_and_gateway_restart():
    node = _lifecycle_test_node()
    first_process_events: list[object] = []
    node._events_pub = SimpleNamespace(publish=first_process_events.append)
    node._stamp_or_now = lambda stamp: stamp or Time()

    node._on_world(
        SimpleNamespace(
            running=True, procedure_id="thyroidectomy", stamp=Time(sec=10)
        )
    )
    node._on_event(
        SimpleNamespace(event_type="RunStarted", stamp=Time(sec=10), confidence=1.0)
    )
    first_run_id = first_process_events[-1].procedure_run_id
    node._on_world(
        SimpleNamespace(
            running=False, procedure_id="thyroidectomy", stamp=Time(sec=19)
        )
    )
    node._on_world(
        SimpleNamespace(
            running=True, procedure_id="thyroidectomy", stamp=Time(sec=20)
        )
    )
    node._on_event(
        SimpleNamespace(event_type="RunStarted", stamp=Time(sec=20), confidence=1.0)
    )
    second_run_event = first_process_events[-1]

    restarted = _lifecycle_test_node()
    restarted._gateway_instance_id = "gateway-2"
    restarted_events: list[object] = []
    restarted._events_pub = SimpleNamespace(publish=restarted_events.append)
    restarted._stamp_or_now = lambda stamp: stamp or Time()
    restarted._on_world(
        SimpleNamespace(
            running=True, procedure_id="thyroidectomy", stamp=Time(sec=30)
        )
    )
    restarted._on_event(
        SimpleNamespace(event_type="RunStarted", stamp=Time(sec=30), confidence=1.0)
    )

    assert first_run_id != second_run_event.procedure_run_id
    assert second_run_event.gateway_instance_id == "gateway-1"
    assert restarted_events[0].gateway_instance_id == "gateway-2"
    assert restarted_events[0].procedure_run_id not in {
        first_run_id,
        second_run_event.procedure_run_id,
    }


def test_gateway_rejects_stamped_event_older_than_current_run_start():
    node, published = _event_test_node(
        running=True, received_at=9.0, now=10.0, source_stamp_sec=100
    )

    node._on_event(
        SimpleNamespace(
            event_type="OldRunEvent", stamp=Time(sec=99), confidence=1.0
        )
    )
    node._on_event(
        SimpleNamespace(
            event_type="CurrentRunEvent", stamp=Time(sec=100), confidence=1.0
        )
    )

    assert [message.event_type for message in published] == ["CurrentRunEvent"]
    assert published[0].procedure_run_id == "run-test"
    assert node._event_sequence == 1


def test_new_run_clears_previous_run_scoped_snapshots():
    node = _lifecycle_test_node()

    node._on_world(SimpleNamespace(running=True, procedure_id="thyroidectomy"))

    assert node._vlm_result is None
    assert node._skill_status is None
    assert node._bed_robot_arm_status is None
    assert node._speech_text is None
    assert node._clinical_sequence == 8
    assert node._speech_sequence == 0


def test_world_stop_clears_current_run_and_prevents_replay():
    node = _lifecycle_test_node()
    node._on_world(SimpleNamespace(running=True, procedure_id="thyroidectomy"))
    node._vlm_result = SimpleNamespace(message="current-vlm")
    node._speech_text = SimpleNamespace(message="current-speech")

    node._on_world(SimpleNamespace(running=False, procedure_id="thyroidectomy"))

    assert node._procedure_run_id == ""
    assert node._last_procedure_active is False
    assert node._vlm_result is None
    assert node._speech_text is None


def test_event_and_clinical_sequences_remain_gateway_instance_monotonic():
    node = _lifecycle_test_node()

    node._on_world(SimpleNamespace(running=True, procedure_id="thyroidectomy"))
    node._on_world(SimpleNamespace(running=False, procedure_id="thyroidectomy"))

    assert node._event_sequence == 7
    assert node._clinical_sequence == 8


def test_active_world_with_wrong_catalog_is_fail_closed():
    node = _lifecycle_test_node()

    node._on_world(SimpleNamespace(running=True, procedure_id="nephrectomy"))
    _, active = node._public_world_locked(now_monotonic_sec=10.0)

    assert active is False
    assert node._procedure_mismatch is True
    assert node._procedure_run_id == ""


def test_stale_world_ends_run_and_clears_run_scoped_data():
    node = _lifecycle_test_node()
    node._world_stale_after_sec = 3.0
    node._on_world(SimpleNamespace(running=True, procedure_id="thyroidectomy"))
    node._vlm_result = SimpleNamespace(message="current-vlm")
    node._speech_text = SimpleNamespace(message="current-speech")

    _, active = node._public_world_locked(now_monotonic_sec=14.0)

    assert active is False
    assert node._procedure_run_id == ""
    assert node._last_procedure_active is False
    assert node._vlm_result is None
    assert node._speech_text is None


def test_gateway_does_not_publish_events_while_procedure_is_idle():
    node, published = _event_test_node(running=False, received_at=9.0, now=10.0)

    node._on_event(SimpleNamespace(event_type="ToolHandoverCompleted"))

    assert published == []
    assert node._event_sequence == 0


def test_gateway_does_not_publish_events_from_stale_world_session():
    node, published = _event_test_node(running=True, received_at=1.0, now=10.0)

    node._on_event(SimpleNamespace(event_type="ToolHandoverCompleted"))

    assert published == []
    assert node._event_sequence == 0


def _speech_input_test_node():
    node = SurgicalInteropGateway.__new__(SurgicalInteropGateway)
    node._lock = threading.RLock()
    node._speech_text = None
    node._speech_sequence = 0
    node._asr_status = None
    node._monotonic = lambda: 4.0
    warnings: list[str] = []
    node.get_logger = lambda: SimpleNamespace(warning=warnings.append)
    return node, warnings


def test_gateway_speech_boundary_rejects_malformed_status_json():
    node, warnings = _speech_input_test_node()
    message = String()
    message.data = "not-json"

    node._on_asr_status(message)

    assert node._asr_status is None
    assert warnings


def test_gateway_speech_boundary_accepts_expected_bounded_schema():
    node, warnings = _speech_input_test_node()
    message = String()
    message.data = json.dumps(
        {
            "schema": "taskplanner.asr.status.v1",
            "stamp_sec": 1.0,
            "asr": {"available": True, "connected": True, "state": "RECORDING"},
        }
    )

    node._on_asr_status(message)

    assert node._asr_status is not None
    assert warnings == []


def test_gateway_speech_boundary_rejects_oversized_final_text():
    node, warnings = _speech_input_test_node()
    message = String()
    message.data = "x" * 2001

    node._on_speech_text(message)

    assert node._speech_text is None
    assert warnings


def _speech_projection_test_node(*, publish_free_text: bool):
    node, _ = _speech_input_test_node()
    node._publish_free_text = publish_free_text
    node._gateway_instance_id = "gateway-1"
    node._procedure_run_id = "run-1"
    node._catalog_version = "sha256:test"
    node._health_stale_after_sec = 6.0
    text = String()
    text.data = "보비 주세요"
    node._speech_text = SimpleNamespace(
        message=text,
        sequence=3,
        received_monotonic_sec=3.0,
        received_stamp=Time(sec=12),
    )
    node._asr_status = SimpleNamespace(
        received_monotonic_sec=3.0,
        message={
            "asr": {
                "available": True,
                "connected": True,
                "state": "RECORDING",
                "finals": [
                    {
                        "text": "보비 주세요",
                        "response_latency_ms": 184.2,
                        "latency_basis": "latest_pcm_send_complete_to_final_receive",
                    }
                ],
            }
        },
    )
    return node


def test_public_speech_matches_final_latency_and_receipt_stamp():
    node = _speech_projection_test_node(publish_free_text=True)

    message = node._speech_message(
        stamp=Time(sec=20),
        revision=4,
        procedure_type="thyroidectomy",
        procedure_active=True,
    )

    assert message.available is True
    assert message.connected is True
    assert message.state == message.STATE_LISTENING
    assert message.text == "보비 주세요"
    assert message.utterance_sequence == 3
    assert message.utterance_stamp.sec == 12
    assert message.latency_available is True
    assert round(message.response_latency_ms, 1) == 184.2


def test_public_speech_redacts_text_by_default_but_keeps_typed_metadata():
    node = _speech_projection_test_node(publish_free_text=False)

    message = node._speech_message(
        stamp=Time(sec=20),
        revision=4,
        procedure_type="thyroidectomy",
        procedure_active=True,
    )

    assert message.available is True
    assert message.connected is True
    assert message.text == ""
    assert message.utterance_sequence == 3
    assert message.utterance_stamp.sec == 12
    assert message.latency_available is True
    assert round(message.response_latency_ms, 1) == 184.2
    assert message.evidence_status == "GATEWAY_OBSERVED_REDACTED"


def test_public_speech_uses_replay_input_status_when_operational_asr_is_absent():
    node = _speech_projection_test_node(publish_free_text=True)
    node._asr_status = None
    node._input_statuses = {
        "speech_input": SimpleNamespace(
            received_monotonic_sec=3.0,
            message=SimpleNamespace(
                source_id="recorded_transcript:0704_6:run-1",
                state="READY",
                healthy=True,
            ),
        )
    }

    message = node._speech_message(
        stamp=Time(sec=20),
        revision=4,
        procedure_type="thyroidectomy",
        procedure_active=True,
    )

    assert message.available is True
    assert message.connected is True
    assert message.state == message.STATE_READY
    assert message.source == "recorded_transcript:0704_6:run-1"
    assert message.text == "보비 주세요"
    assert message.utterance_sequence == 3
    assert message.latency_available is False


def test_public_speech_rejects_unreviewed_free_form_latency_basis():
    node = _speech_projection_test_node(publish_free_text=False)
    node._asr_status.message["asr"]["finals"][0]["latency_basis"] = (
        "patient-specific diagnostic text"
    )

    message = node._speech_message(
        stamp=Time(sec=20),
        revision=4,
        procedure_type="thyroidectomy",
        procedure_active=True,
    )

    assert message.latency_available is False
    assert message.response_latency_ms == 0.0
    assert message.latency_basis == ""


def test_public_speech_is_empty_when_idle_or_status_stale():
    node = _speech_projection_test_node(publish_free_text=False)
    idle = node._speech_message(
        stamp=Time(sec=20),
        revision=4,
        procedure_type="thyroidectomy",
        procedure_active=False,
    )
    node._monotonic = lambda: 20.0
    stale = node._speech_message(
        stamp=Time(sec=20),
        revision=5,
        procedure_type="thyroidectomy",
        procedure_active=True,
    )

    assert idle.available is False and idle.text == ""
    assert stale.available is False and stale.text == ""


def test_public_clinical_summary_is_redacted_without_dropping_structured_evidence():
    result = SimpleNamespace(
        stamp=Time(sec=8),
        source="cam4_vlm",
        summary="A clamp is visible near the surgical field.",
        phase_ids=["P03"],
        phase_confidences=[0.75],
        observed_tool_ids=["T02"],
        observed_location_ids=["surgical_field"],
        observed_location_types=["surgical_field"],
        observed_confidences=[0.84],
        gesture_event_type="request_tool",
        gesture_requested_tool="T02",
        gesture_hand_pose="open_receive",
        gesture_confidence=0.8,
        uncertainty=0.22,
    )
    projection = project_clinical_observation(result)
    node = SurgicalInteropGateway.__new__(SurgicalInteropGateway)
    node._publish_free_text = False

    message = node._to_public_clinical(projection, sequence=9)

    assert message.summary == ""
    assert message.phase_ids == ["P03"]
    assert list(message.phase_confidences) == [0.75]
    assert message.observed_tool_ids == ["T02"]
    assert message.gesture_event_type == "request_tool"
    assert message.evidence_status == "MODEL_OBSERVED_REDACTED"

    node._publish_free_text = True
    opted_in = node._to_public_clinical(projection, sequence=9)
    assert opted_in.summary == result.summary
    assert opted_in.evidence_status == MODEL_OBSERVED


def test_public_clinical_malformed_numeric_state_stays_unknown_when_text_redacted():
    projection = project_clinical_observation(
        SimpleNamespace(
            summary="Free-form text",
            phase_ids=["P01"],
            phase_confidences=[float("nan")],
            observed_tool_ids=[],
            observed_location_ids=[],
            observed_location_types=[],
            observed_confidences=[],
            gesture_confidence=0.0,
            uncertainty=float("nan"),
        )
    )
    node = SurgicalInteropGateway.__new__(SurgicalInteropGateway)
    node._publish_free_text = False

    message = node._to_public_clinical(projection, sequence=10)

    assert message.summary == ""
    assert message.phase_ids == []
    assert list(message.phase_confidences) == []
    assert message.uncertainty == 1.0
    assert message.evidence_status == UNKNOWN


def test_freshness_uses_receipt_time_and_has_missing_source_state():
    missing = freshness_from_receipt(None, now_monotonic_sec=10.0, stale_after_sec=2.0)
    fresh = freshness_from_receipt(8.5, now_monotonic_sec=10.0, stale_after_sec=2.0)
    stale = freshness_from_receipt(7.0, now_monotonic_sec=10.0, stale_after_sec=2.0)

    assert (missing.available, missing.fresh, missing.age_sec) == (False, False, -1.0)
    assert (fresh.available, fresh.fresh) == (True, True)
    assert (stale.available, stale.fresh) == (True, False)
