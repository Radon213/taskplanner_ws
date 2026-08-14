from __future__ import annotations

import json
from pathlib import Path

from or_digital_twin.models import (
    LIFECYCLE_CLEANED_LEFT,
    LIFECYCLE_MAYO_RECOVERY,
    LIFECYCLE_MAYO_REUSE,
    LIFECYCLE_PREPOSITIONED_RIGHT,
    LIFECYCLE_RECOVERING_LEFT,
    LIFECYCLE_RETURNED_HOME,
    LIFECYCLE_SURGEON_OWNED,
)
from or_digital_twin.twin import ORDigitalTwin
from procedure_spec import load_bundle
from surgical_msgs.msg import SurgeonRequest, ToolObservation, TwinEvent


def _thyroid_twin() -> ORDigitalTwin:
    spec = load_bundle(
        Path(__file__).parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
        / "thyroidectomy"
    )
    return ORDigitalTwin(spec)


def _thyroid_demo_twin() -> ORDigitalTwin:
    spec = load_bundle(
        Path(__file__).parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
        / "thyroidectomy_demo"
    )
    return ORDigitalTwin(spec)


def _state(twin: ORDigitalTwin, tool_id: str, index: int = 1):
    return twin.instrument_states[f"{tool_id}#{index}"]


def _request(tool_id: str, event_type: str = "request_tool") -> SurgeonRequest:
    request = SurgeonRequest()
    request.event_type = event_type
    request.requested_tool = tool_id
    request.ready_for_handover = True
    return request


def _event(
    event_type: str,
    tool_id: str,
    *,
    source: str,
    source_type: str,
    target: str,
    target_type: str,
) -> TwinEvent:
    event = TwinEvent()
    event.event_type = event_type
    event.instrument_id = tool_id
    event.source_location_id = source
    event.source_location_type = source_type
    event.target_location_id = target
    event.target_location_type = target_type
    event.location_id = target
    event.location_type = target_type
    event.arm = "right"
    event.confidence = 1.0
    return event


def test_requested_mayo_reuse_tool_can_be_handed_over_again() -> None:
    twin = _thyroid_twin()
    tool_id = "T01"
    state = _state(twin, tool_id)
    twin._set_lifecycle(
        state,
        LIFECYCLE_MAYO_REUSE,
        location_type="mayo_reuse_zone",
        location_id="mayo_reuse_zone",
        confidence=1.0,
    )

    assert state.contaminated is True
    twin.state.phase_uncertain = False
    assert twin.update_surgeon_request(_request(tool_id)) == tool_id
    assert twin.handover_allowed() is True

    twin.apply_event(
        _event(
            "RobotGraspedTool",
            tool_id,
            source="mayo_reuse_zone",
            source_type="mayo_reuse_zone",
            target="robot_right_hand",
            target_type="robot_right_hand",
        )
    )
    assert state.lifecycle_stage == LIFECYCLE_PREPOSITIONED_RIGHT
    assert twin.state.right_hand_tool == tool_id

    twin.apply_event(
        _event(
            "ToolHandoverCompleted",
            tool_id,
            source="robot_right_hand",
            source_type="robot_right_hand",
            target="surgeon_receive_zone",
            target_type="handover_zone",
        )
    )
    assert state.lifecycle_stage == LIFECYCLE_SURGEON_OWNED
    assert twin.state.surgeon_request_tool == ""


def test_confirmed_public_retrieve_moves_mayo_tool_back_to_tray() -> None:
    twin = _thyroid_twin()
    state = _state(twin, "T04")
    twin._set_lifecycle(
        state,
        LIFECYCLE_MAYO_RECOVERY,
        location_type="mayo_recovery_zone",
        location_id="mayo_recovery_zone",
        confidence=1.0,
    )

    retrieved = _event(
        "ToolRetrievedFromMayo",
        "T04",
        source="mayo_recovery_zone",
        source_type="mayo_recovery_zone",
        target="robot_left_hand",
        target_type="robot_left_hand",
    )
    retrieved.instance_id = state.instance_id
    twin.apply_event(retrieved)
    assert state.lifecycle_stage == LIFECYCLE_RECOVERING_LEFT

    returned = _event(
        "ToolReturnedToTray",
        "T04",
        source="robot_left_hand",
        source_type="robot_left_hand",
        target=state.home_location_id,
        target_type=state.home_location_type,
    )
    returned.instance_id = state.instance_id
    twin.apply_event(returned)
    assert state.lifecycle_stage == LIFECYCLE_RETURNED_HOME
    assert state.location_id == state.home_location_id


def test_unused_mayo_preposition_returns_to_mayo_instead_of_rack() -> None:
    twin = _thyroid_twin()
    tool_id = "T01"
    state = _state(twin, tool_id)
    twin._set_lifecycle(
        state,
        LIFECYCLE_MAYO_REUSE,
        location_type="mayo_reuse_zone",
        location_id="mayo_reuse_zone",
        confidence=1.0,
        placement_evidence="public_visual_observation",
    )

    twin.apply_event(
        _event(
            "RobotGraspedTool",
            tool_id,
            source="mayo_reuse_zone",
            source_type="mayo_reuse_zone",
            target="robot_right_hand",
            target_type="robot_right_hand",
        )
    )

    assert state.lifecycle_stage == LIFECYCLE_PREPOSITIONED_RIGHT
    assert state.preposition_origin_location_id == "mayo_stand"
    assert state.preposition_origin_lifecycle_stage == LIFECYCLE_MAYO_REUSE

    returned = _event(
        "UnusedPrepositionReturned",
        tool_id,
        source="robot_right_hand",
        source_type="robot_right_hand",
        target="mayo_reuse_zone",
        target_type="mayo_reuse_zone",
    )
    returned.detail_json = json.dumps(
        {"target_lifecycle_stage": LIFECYCLE_MAYO_REUSE}
    )
    twin.apply_event(returned)

    assert state.lifecycle_stage == LIFECYCLE_MAYO_REUSE
    assert state.location_id == "mayo_stand"
    assert state.contaminated is True
    assert state.preposition_origin_location_id == ""
    assert twin.state.right_hand_tool == ""


def test_active_mayo_recovery_is_canceled_when_tool_is_requested_for_reuse() -> None:
    twin = _thyroid_twin()
    tool_id = "T01"
    state = _state(twin, tool_id)
    twin._set_lifecycle(
        state,
        LIFECYCLE_MAYO_RECOVERY,
        location_type="mayo_recovery_zone",
        location_id="mayo_recovery_zone",
        confidence=1.0,
    )
    twin._open_recovery_transaction(tool_id, "test_recovery_candidate")
    assert tool_id in twin.state.active_recovery_tools

    twin.update_surgeon_request(_request(tool_id, "voice_request"))
    assert twin.handover_allowed() is True

    twin.apply_event(
        _event(
            "RobotGraspedTool",
            tool_id,
            source="mayo_recovery_zone",
            source_type="mayo_recovery_zone",
            target="robot_right_hand",
            target_type="robot_right_hand",
        )
    )

    assert state.lifecycle_stage == LIFECYCLE_PREPOSITIONED_RIGHT
    assert tool_id not in twin.state.active_recovery_tools


def test_mayo_handover_respects_two_tool_surgeon_capacity() -> None:
    twin = _thyroid_twin()
    mayo_tool = "T01"
    twin._set_lifecycle(
        _state(twin, mayo_tool),
        LIFECYCLE_MAYO_REUSE,
        location_type="mayo_reuse_zone",
        location_id="mayo_reuse_zone",
        confidence=1.0,
    )
    for tool_id in ("T02", "T03"):
        twin._set_lifecycle(
            _state(twin, tool_id),
            LIFECYCLE_SURGEON_OWNED,
            location_type="surgeon_hand",
            location_id="surgeon_hand",
            confidence=1.0,
        )

    twin.update_surgeon_request(_request(mayo_tool))

    assert twin.handover_allowed() is False


def test_corroborated_public_mayo_stand_observation_maps_to_mayo_reuse() -> None:
    twin = _thyroid_twin()
    observation = ToolObservation()
    observation.stamp.sec = 12
    observation.instrument_id = "T04"
    observation.location_type = "mayo_stand"
    observation.location_id = "mayo_stand"
    observation.confidence = 0.75
    observation.visible = True

    result = twin.reconcile_observation(
        observation,
        source="vlm_cam4_mayo_observation",
        proposal_id="test:cam4-mayo",
    )

    state = _state(twin, "T04")
    assert result is not None
    assert result["reducer_result"] == "accepted"
    assert state.lifecycle_stage == LIFECYCLE_MAYO_REUSE
    assert state.location_type == "mayo_stand"
    assert state.mayo_placement_evidence == "public_visual_observation"


def test_stable_cam4_can_move_field_tool_to_mayo_reuse() -> None:
    twin = _thyroid_twin()
    state = _state(twin, "T04")
    twin._set_lifecycle(
        state,
        LIFECYCLE_SURGEON_OWNED,
        location_type="surgical_field",
        location_id="surgical_field",
        confidence=1.0,
    )
    observation = ToolObservation()
    observation.stamp.sec = 44
    observation.instrument_id = "T04"
    observation.location_type = "mayo_stand"
    observation.location_id = "mayo_stand"
    observation.confidence = 0.91
    observation.visible = True

    result = twin.reconcile_observation(
        observation,
        source="vlm_cam4_mayo_observation",
        proposal_id="test:stable-cam4-field-to-mayo",
    )

    assert result is not None
    assert result["reducer_result"] == "accepted"
    assert state.lifecycle_stage == LIFECYCLE_MAYO_REUSE
    assert state.mayo_placement_evidence == "public_visual_observation"


def test_low_confidence_cam4_cannot_move_field_tool_to_mayo() -> None:
    twin = _thyroid_twin()
    state = _state(twin, "T04")
    twin._set_lifecycle(
        state,
        LIFECYCLE_SURGEON_OWNED,
        location_type="surgical_field",
        location_id="surgical_field",
        confidence=1.0,
    )
    observation = ToolObservation()
    observation.stamp.sec = 44
    observation.instrument_id = "T04"
    observation.location_type = "mayo_stand"
    observation.location_id = "mayo_stand"
    observation.confidence = 0.59
    observation.visible = True

    result = twin.reconcile_observation(
        observation,
        source="vlm_cam4_mayo_observation",
        proposal_id="test:weak-cam4-field-to-mayo",
    )

    assert result is not None
    assert result["reducer_result"] == "rejected"
    assert (
        result["reducer_reason"]
        == "field_deployed_tool_requires_explicit_return_context"
    )
    assert state.lifecycle_stage == LIFECYCLE_SURGEON_OWNED


def test_direct_cam4_mayo_observation_preserves_source_timestamp() -> None:
    twin = _thyroid_twin()
    state = _state(twin, "T04")
    twin._set_lifecycle(
        state,
        LIFECYCLE_SURGEON_OWNED,
        location_type="surgical_field",
        location_id="surgical_field",
        confidence=1.0,
    )
    observation = ToolObservation()
    observation.stamp.sec = 44
    observation.stamp.nanosec = 500_000_000
    observation.instrument_id = "Bovie surgical cautery"
    observation.location_type = "mayo_stand"
    observation.location_id = "mayo_stand"
    observation.confidence = 0.82
    observation.visible = True

    result = twin.reconcile_observation(
        observation,
        source="cam4_rfdetr_mayo_observation",
        proposal_id="test:direct-cam4-source-time",
    )

    assert result is not None
    assert result["reducer_result"] == "accepted"
    assert state.lifecycle_stage == LIFECYCLE_MAYO_REUSE
    assert state.last_update_sec == 44.5


def test_direct_cam4_mayo_observation_cannot_move_robot_held_tool() -> None:
    twin = _thyroid_twin()
    state = _state(twin, "T04")
    twin._set_lifecycle(
        state,
        LIFECYCLE_PREPOSITIONED_RIGHT,
        location_type="robot_right_hand",
        location_id="robot_right_hand",
        confidence=1.0,
    )
    observation = ToolObservation()
    observation.stamp.sec = 44
    observation.instrument_id = "Bovie surgical cautery"
    observation.location_type = "mayo_stand"
    observation.location_id = "mayo_stand"
    observation.confidence = 0.95
    observation.visible = True

    result = twin.reconcile_observation(
        observation,
        source="cam4_rfdetr_mayo_observation",
        proposal_id="test:cam4-holder-invariant",
    )

    assert result is not None
    assert result["reducer_result"] == "rejected"
    assert result["reducer_reason"] == "illegal_observation_transition"
    assert state.lifecycle_stage == LIFECYCLE_PREPOSITIONED_RIGHT
    assert state.location_type == "robot_right_hand"


def test_recovery_evidence_preserves_future_use_fact_for_bt() -> None:
    twin = _thyroid_demo_twin()
    twin.set_initial_phase("P03")

    for tool_id in ("T04", "T07"):
        state = _state(twin, tool_id)
        twin._set_lifecycle(
            state,
            LIFECYCLE_MAYO_REUSE,
            location_type="mayo_stand",
            location_id="mayo_stand",
            confidence=0.9,
        )
        result = twin.record_mayo_policy_evidence(
            instrument_id=state.instance_id,
            evidence_type="recover",
            confidence=0.92,
            stability_sec=5.0,
            source="vlm_mayo_retrieve",
            proposal_id=f"test:future-use:{tool_id}",
            stamp_sec=50.0,
        )

        assert result is not None
        assert result["reducer_result"] == "accepted"
        assert result["procedure_future_use_expected"] is True
        assert state.lifecycle_stage == LIFECYCLE_MAYO_REUSE
        assert state.mayo_recovery_confidence == 0.92
        assert twin.state.active_recovery_tool_instances == []


def test_recovery_evidence_never_promotes_lifecycle_or_opens_transaction() -> None:
    twin = _thyroid_demo_twin()
    twin.set_initial_phase("P03")
    state = _state(twin, "T01")
    twin._set_lifecycle(
        state,
        LIFECYCLE_MAYO_REUSE,
        location_type="mayo_stand",
        location_id="mayo_stand",
        confidence=0.9,
    )

    result = twin.record_mayo_policy_evidence(
        instrument_id=state.instance_id,
        evidence_type="recover",
        confidence=0.92,
        stability_sec=5.0,
        source="vlm_mayo_retrieve",
        proposal_id="test:no-future-use",
        stamp_sec=50.0,
    )

    assert result is not None
    assert result["reducer_result"] == "accepted"
    assert result["procedure_future_use_expected"] is False
    assert state.lifecycle_stage == LIFECYCLE_MAYO_REUSE
    assert state.next_required_transition == ""
    assert twin.state.active_recovery_tool_instances == []


def test_recovery_transaction_promotes_mayo_state_and_queues_once() -> None:
    twin = _thyroid_demo_twin()
    state = _state(twin, "T01")
    twin._set_lifecycle(
        state,
        LIFECYCLE_MAYO_REUSE,
        location_type="mayo_reuse_zone",
        location_id="mayo_reuse_zone",
        confidence=0.9,
    )

    twin._open_recovery_transaction(state.instance_id, "approved_return")
    twin._open_recovery_transaction(state.instance_id, "duplicate_return")

    assert state.lifecycle_stage == LIFECYCLE_MAYO_RECOVERY
    assert state.status == "awaiting_retrieval"
    assert (state.location_type, state.location_id) == (
        "mayo_stand",
        "mayo_stand",
    )
    assert twin.state.active_recovery_tool_instances == [state.instance_id]


def test_open_return_waits_for_physical_mayo_arrival_before_state_promotion() -> None:
    twin = _thyroid_demo_twin()
    state = _state(twin, "T01")
    twin._set_lifecycle(
        state,
        LIFECYCLE_SURGEON_OWNED,
        location_type="surgeon_hand",
        location_id="surgeon_hand",
        confidence=1.0,
    )
    twin._open_recovery_transaction(state.instance_id, "surgeon_return_request")

    assert state.lifecycle_stage == LIFECYCLE_SURGEON_OWNED
    assert twin.state.active_recovery_tool_instances == [state.instance_id]

    observation = ToolObservation()
    observation.instrument_id = state.instance_id
    observation.location_type = "mayo_stand"
    observation.location_id = "mayo_stand"
    observation.visible = True
    observation.confidence = 1.0
    observation.stamp.sec = 10
    twin.reconcile_observation(
        observation,
        source="cam4_rfdetr_mayo_observation",
        proposal_id="test:mayo-arrival",
    )

    assert state.lifecycle_stage == LIFECYCLE_MAYO_RECOVERY
    assert state.status == "awaiting_retrieval"
    assert twin.state.active_recovery_tool_instances == [state.instance_id]


def test_completion_cleanup_policy_evidence_still_does_not_mutate_lifecycle() -> None:
    twin = _thyroid_demo_twin()
    twin.set_initial_phase("P03")
    twin.state.execution_state = "finishing"
    state = _state(twin, "T04")
    twin._set_lifecycle(
        state,
        LIFECYCLE_MAYO_REUSE,
        location_type="mayo_stand",
        location_id="mayo_stand",
        confidence=0.9,
    )

    result = twin.record_mayo_policy_evidence(
        instrument_id=state.instance_id,
        evidence_type="recover",
        confidence=0.92,
        stability_sec=5.0,
        source="vlm_mayo_retrieve",
        proposal_id="test:completion-cleanup",
        stamp_sec=50.0,
    )

    assert result is not None
    assert result["reducer_result"] == "accepted"
    assert state.lifecycle_stage == LIFECYCLE_MAYO_REUSE
    assert twin.state.active_recovery_tool_instances == []


def test_completion_cleanup_does_not_synthesize_surgeon_to_mayo_transition() -> None:
    twin = _thyroid_demo_twin()
    state = _state(twin, "T04")
    twin._set_lifecycle(
        state,
        LIFECYCLE_SURGEON_OWNED,
        location_type="surgeon_hand",
        location_id="surgeon_hand",
        confidence=1.0,
    )

    twin._begin_completion_cleanup()
    twin._recompute_transient_state()

    assert twin.state.execution_state == "finishing"
    assert state.lifecycle_stage == LIFECYCLE_SURGEON_OWNED
    assert state.location_type == "surgeon_hand"
    assert twin.state.active_recovery_tool_instances == []


def test_recorded_mayo_observation_cannot_undo_shadow_recovery() -> None:
    twin = _thyroid_twin()
    state = _state(twin, "T04")
    twin._set_lifecycle(
        state,
        LIFECYCLE_CLEANED_LEFT,
        location_type="cleaner_slot",
        location_id="cleaner_slot",
        confidence=1.0,
    )
    returned = _event(
        "ToolReturnedToTray",
        "T04",
        source="cleaner_slot",
        source_type="cleaner_slot",
        target=state.home_location_id,
        target_type=state.home_location_type,
    )
    returned.instance_id = state.instance_id
    returned.mode = "shadow_counterfactual"
    twin.apply_event(returned)

    observation = ToolObservation()
    observation.stamp.sec = 20
    observation.instrument_id = "T04"
    observation.location_type = "mayo_stand"
    observation.location_id = "mayo_stand"
    observation.confidence = 0.92
    observation.visible = True
    result = twin.reconcile_observation(
        observation,
        source="vlm_cam4_mayo_observation",
        proposal_id="test:stale-recorded-mayo",
    )

    assert result is not None
    assert result["reducer_result"] == "quarantined"
    assert (
        result["reducer_reason"]
        == "shadow_counterfactual_branch_conflict"
    )
    assert state.lifecycle_stage == LIFECYCLE_RETURNED_HOME
    assert state.location_type == state.home_location_type

    twin.reset_runtime()
    reset_result = twin.reconcile_observation(
        observation,
        source="vlm_cam4_mayo_observation",
        proposal_id="test:post-reset-mayo",
    )
    assert reset_result is not None
    assert reset_result["reducer_result"] == "accepted"
