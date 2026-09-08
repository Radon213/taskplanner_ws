from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from or_digital_twin.models import (
    LIFECYCLE_CLEANING_LEFT,
    LIFECYCLE_CLEANED_LEFT,
    LIFECYCLE_HOME_RACK,
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
    twin.set_cam4_mayo_hand_present(False)
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


def test_mayo_handover_does_not_limit_surgeon_owned_tool_count() -> None:
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

    assert twin.handover_allowed() is True

    twin.apply_event(
        _event(
            "RobotGraspedTool",
            mayo_tool,
            source="mayo_reuse_zone",
            source_type="mayo_reuse_zone",
            target="robot_right_hand",
            target_type="robot_right_hand",
        )
    )
    twin.apply_event(
        _event(
            "ToolHandoverCompleted",
            mayo_tool,
            source="robot_right_hand",
            source_type="robot_right_hand",
            target="surgeon_receive_zone",
            target_type="handover_zone",
        )
    )

    assert _state(twin, mayo_tool).lifecycle_stage == LIFECYCLE_SURGEON_OWNED
    assert "surgeon_owned_overloaded" not in twin.state.safety_flags


def test_type_only_mayo_observation_cannot_move_home_inventory() -> None:
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
    assert result["reducer_result"] == "rejected"
    assert result["reducer_reason"] == "cam4_mayo_no_surgeon_owned_instance"
    assert state.lifecycle_stage != LIFECYCLE_MAYO_REUSE


def test_vlm_only_mayo_visibility_cannot_move_field_tool() -> None:
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
    assert result["reducer_result"] == "rejected"
    assert result["reducer_reason"] == "cam4_mayo_requires_typed_detector_episode"
    assert state.lifecycle_stage == LIFECYCLE_SURGEON_OWNED


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
    assert result["reducer_reason"] == "cam4_mayo_confidence_below_threshold"
    assert state.lifecycle_stage == LIFECYCLE_SURGEON_OWNED


def _typed_cam4_mayo_observation(
    instrument_id: str,
    *,
    observed_count: int = 1,
    confidence: float = 0.91,
    stamp_sec: float = 44.08,
) -> ToolObservation:
    observation = ToolObservation()
    observation.stamp.sec = int(stamp_sec)
    observation.stamp.nanosec = int(
        round((stamp_sec - int(stamp_sec)) * 1_000_000_000)
    )
    observation.instrument_id = instrument_id
    observation.location_type = "mayo_stand"
    observation.location_id = "mayo_stand"
    observation.confidence = confidence
    observation.visible = True
    observation.correlation_id = (
        f"cam4-typed-mayo:v1:1:7:1:{observed_count}"
    )
    return observation


def _confirm_typed_cam4_mayo(
    twin: ORDigitalTwin,
    instrument_id: str,
    *,
    observed_count: int = 1,
    confidence: float = 0.91,
    frame_count: int = 5,
    start_sec: float = 44.0,
) -> list[dict]:
    twin.state.running = True
    twin.state.execution_state = "running"
    results: list[dict] = []
    for frame_index in range(frame_count):
        stamp_sec = start_sec + frame_index * 0.25
        for occurrence in range(observed_count):
            result = twin.reconcile_observation(
                _typed_cam4_mayo_observation(
                    instrument_id,
                    observed_count=observed_count,
                    confidence=confidence,
                    stamp_sec=stamp_sec,
                ),
                source="cam4_typed_mayo_observation",
                proposal_id=(
                    f"test:typed-confirm:{instrument_id}:"
                    f"{frame_index}:{occurrence}"
                ),
            )
            assert result is not None
            results.append(result)
    return results


def test_typed_cam4_mayo_appearance_returns_surgeon_tool_without_handover_history() -> None:
    twin = _thyroid_demo_twin()
    state = _state(twin, "T04")
    twin._set_lifecycle(
        state,
        LIFECYCLE_SURGEON_OWNED,
        location_type="surgical_field",
        location_id="surgical_field",
        confidence=1.0,
    )
    twin.state.running = True
    twin.state.execution_state = "running"

    occupied_result = twin.reconcile_observation(
        _typed_cam4_mayo_observation("Bovie surgical cautery"),
        source="cam4_typed_mayo_observation",
        proposal_id="test:typed-human-return-occupied",
    )

    assert occupied_result is not None
    assert occupied_result["reducer_result"] == "quarantined"
    assert (
        occupied_result["reducer_reason"]
        == "cam4_typed_mayo_waiting_for_hand_clear"
    )
    assert state.lifecycle_stage == LIFECYCLE_SURGEON_OWNED

    twin.set_cam4_mayo_hand_present(False)
    results = _confirm_typed_cam4_mayo(
        twin,
        "Bovie surgical cautery",
    )

    assert all(
        result["reducer_reason"] == "awaiting_observation_hysteresis"
        for result in results[:-1]
    )
    assert results[-1]["reducer_result"] == "accepted"
    assert state.lifecycle_stage == LIFECYCLE_MAYO_REUSE
    assert state.location_id == "mayo_stand"
    assert state.mayo_placement_evidence == "cam4_typed_mayo_observation"


def test_typed_cam4_mayo_cannot_override_reset_or_idle_state() -> None:
    twin = _thyroid_demo_twin()
    state = _state(twin, "T04")
    twin._set_lifecycle(
        state,
        LIFECYCLE_SURGEON_OWNED,
        location_type="surgical_field",
        location_id="surgical_field",
        confidence=1.0,
    )
    twin.set_cam4_mayo_hand_present(False)

    result = twin.reconcile_observation(
        _typed_cam4_mayo_observation("Bovie surgical cautery"),
        source="cam4_typed_mayo_observation",
        proposal_id="test:typed-idle-reset-fence",
    )

    assert result is not None
    assert result["reducer_result"] == "quarantined"
    assert result["reducer_reason"] == "cam4_typed_mayo_inactive_scenario"
    assert state.lifecycle_stage == LIFECYCLE_SURGEON_OWNED


def test_typed_cam4_mayo_does_not_rebind_during_robot_or_cleaner_ownership() -> None:
    twin = _thyroid_demo_twin()
    state = _state(twin, "T04")
    twin._set_lifecycle(
        state,
        LIFECYCLE_PREPOSITIONED_RIGHT,
        location_type="robot_right_hand",
        location_id="robot_right_hand",
        confidence=1.0,
    )
    robot_result = twin.reconcile_observation(
        _typed_cam4_mayo_observation("Bovie surgical cautery"),
        source="cam4_typed_mayo_observation",
        proposal_id="test:typed-robot-held",
    )
    assert robot_result is not None
    assert robot_result["reducer_reason"] == "cam4_typed_mayo_robot_held"
    assert state.lifecycle_stage == LIFECYCLE_PREPOSITIONED_RIGHT

    twin._set_lifecycle(
        state,
        LIFECYCLE_CLEANING_LEFT,
        location_type="cleaner_slot",
        location_id="cleaner_slot",
        confidence=1.0,
    )
    cleaner_observation = _typed_cam4_mayo_observation("Bovie surgical cautery")
    cleaner_observation.stamp.sec = 46
    cleaner_result = twin.reconcile_observation(
        cleaner_observation,
        source="cam4_typed_mayo_observation",
        proposal_id="test:typed-cleaner-held",
    )
    assert cleaner_result is not None
    assert cleaner_result["reducer_reason"] == "cam4_typed_mayo_cleaner_held"
    assert state.lifecycle_stage == LIFECYCLE_CLEANING_LEFT


def test_typed_cam4_mayo_blocks_active_same_tool_robot_task() -> None:
    twin = _thyroid_demo_twin()
    state = _state(twin, "T04")
    twin._set_lifecycle(
        state,
        LIFECYCLE_SURGEON_OWNED,
        location_type="surgical_field",
        location_id="surgical_field",
        confidence=1.0,
    )
    twin._start_active_robot_task(
        task_id="same-tool-moving",
        task_type="tool_handover",
        instrument_id="T04",
        instrument_instance_id="T04#1",
        arm="right",
        source_anchor_id="tray",
        target_anchor_id="surgeon",
        duration_sec=10.0,
    )

    result = twin.reconcile_observation(
        _typed_cam4_mayo_observation("Bovie surgical cautery"),
        source="cam4_typed_mayo_observation",
        proposal_id="test:typed-active-robot-task",
    )

    assert result is not None
    assert result["reducer_reason"] == "cam4_typed_mayo_active_same_tool_task"
    assert state.lifecycle_stage == LIFECYCLE_SURGEON_OWNED


def test_typed_cam4_mayo_allows_stable_unrelated_return_during_robot_task() -> None:
    twin = _thyroid_demo_twin()
    state = _state(twin, "T04")
    twin._set_lifecycle(
        state,
        LIFECYCLE_SURGEON_OWNED,
        location_type="surgical_field",
        location_id="surgical_field",
        confidence=1.0,
    )
    twin.set_cam4_mayo_hand_present(False)
    twin._start_active_robot_task(
        task_id="other-tool-moving",
        task_type="tool_handover",
        instrument_id="T02",
        instrument_instance_id="T02#1",
        arm="right",
        source_anchor_id="tray",
        target_anchor_id="surgeon",
        duration_sec=10.0,
    )

    results = _confirm_typed_cam4_mayo(
        twin,
        "Bovie surgical cautery",
    )

    assert results[-1]["reducer_result"] == "accepted"
    assert state.lifecycle_stage == LIFECYCLE_MAYO_REUSE


def test_typed_cam4_mayo_uses_count_to_rebind_exchangeable_slots_once_each() -> None:
    twin = _thyroid_demo_twin()
    first = _state(twin, "T04")
    second = deepcopy(first)
    second.instance_id = "T04#2"
    twin.instrument_states[second.instance_id] = second
    twin._set_lifecycle(
        first,
        LIFECYCLE_SURGEON_OWNED,
        location_type="surgical_field",
        location_id="surgical_field",
        confidence=1.0,
        last_update_sec=20.0,
    )
    twin._set_lifecycle(
        second,
        LIFECYCLE_SURGEON_OWNED,
        location_type="surgical_field",
        location_id="surgical_field",
        confidence=1.0,
        last_update_sec=10.0,
    )
    twin.set_cam4_mayo_hand_present(False)
    results = _confirm_typed_cam4_mayo(
        twin,
        "Bovie surgical cautery",
        observed_count=2,
        frame_count=10,
    )

    accepted_instances = {
        result["instance_id"]
        for result in results
        if result["reducer_result"] == "accepted"
        and result["reducer_reason"] == "legal_observation_transition"
    }
    assert accepted_instances == {"T04#1", "T04#2"}
    assert second.lifecycle_stage == LIFECYCLE_MAYO_REUSE
    assert first.lifecycle_stage == LIFECYCLE_MAYO_REUSE


def test_typed_cam4_mayo_continuation_does_not_consume_another_exchangeable_slot() -> None:
    twin = _thyroid_demo_twin()
    mayo = _state(twin, "T04")
    surgeon = deepcopy(mayo)
    surgeon.instance_id = "T04#2"
    twin.instrument_states[surgeon.instance_id] = surgeon
    twin._set_lifecycle(
        mayo,
        LIFECYCLE_MAYO_REUSE,
        location_type="mayo_stand",
        location_id="mayo_stand",
        confidence=1.0,
    )
    twin._set_lifecycle(
        surgeon,
        LIFECYCLE_SURGEON_OWNED,
        location_type="surgical_field",
        location_id="surgical_field",
        confidence=1.0,
    )

    result = twin.reconcile_observation(
        _typed_cam4_mayo_observation("Bovie surgical cautery"),
        source="cam4_typed_mayo_observation",
        proposal_id="test:typed-mayo-continuation",
    )

    assert result is not None
    assert result["reducer_result"] == "accepted"
    assert mayo.lifecycle_stage == LIFECYCLE_MAYO_REUSE
    assert surgeon.lifecycle_stage == LIFECYCLE_SURGEON_OWNED


def test_typed_cam4_mayo_cannot_activate_capacity_beyond_start_inventory() -> None:
    twin = _thyroid_demo_twin()
    rack = _state(twin, "T02")
    twin.set_cam4_mayo_hand_present(False)

    results = _confirm_typed_cam4_mayo(twin, "Adson forceps")
    result = results[-1]

    assert result is not None
    assert result["reducer_result"] == "rejected"
    assert result["reducer_reason"] == "cam4_typed_mayo_capacity_activation_retired"
    assert rack.lifecycle_stage == LIFECYCLE_HOME_RACK
    assert rack.location_id == rack.home_location_id
    assert "T02#2" not in twin.instrument_states


def test_typed_cam4_mayo_capacity_activation_stays_rejected_on_repeat() -> None:
    twin = _thyroid_demo_twin()
    twin.set_cam4_mayo_hand_present(False)
    first = _confirm_typed_cam4_mayo(twin, "Adson forceps")[-1]
    repeated = _typed_cam4_mayo_observation("Adson forceps")
    repeated.stamp.sec = 45
    second = twin.reconcile_observation(
        repeated,
        source="cam4_typed_mayo_observation",
        proposal_id="test:typed-capacity-repeat",
    )

    assert first is not None and second is not None
    assert first["reducer_reason"] == second["reducer_reason"] == (
        "cam4_typed_mayo_capacity_activation_retired"
    )
    assert sorted(twin.instrument_states) == [
        "T02#1",
        "T04#1",
        "T07#1",
        "T08#1",
    ]

    overflow = _typed_cam4_mayo_observation(
        "Adson forceps",
        observed_count=2,
    )
    overflow.stamp.sec = 46
    rejected = twin.reconcile_observation(
        overflow,
        source="cam4_typed_mayo_observation",
        proposal_id="test:typed-capacity-overflow",
    )

    assert rejected is not None
    assert rejected["reducer_result"] == "rejected"
    assert rejected["reducer_reason"] == "cam4_typed_mayo_capacity_activation_retired"
    assert "T02#3" not in twin.instrument_states


def test_exchangeable_population_over_capacity_remains_fail_closed() -> None:
    twin = _thyroid_demo_twin()
    for index in (2, 3):
        overflow = deepcopy(_state(twin, "T02"))
        overflow.instance_id = f"T02#{index}"
        twin.instrument_states[overflow.instance_id] = overflow

    twin.normalize_for_publish()

    assert "duplicate_tool_holder" in twin.state.safety_flags


def test_direct_cam4_mayo_observation_preserves_source_timestamp() -> None:
    twin = _thyroid_twin()
    state = _state(twin, "T04")
    twin._set_lifecycle(
        state,
        LIFECYCLE_PREPOSITIONED_RIGHT,
        location_type="robot_right_hand",
        location_id="robot_right_hand",
        confidence=1.0,
    )
    handover = _event(
        "ToolHandoverCompleted",
        "T04",
        source="robot_right_hand",
        source_type="robot_right_hand",
        target="surgeon_receive_zone",
        target_type="handover_zone",
    )
    handover.stamp.sec = 44
    twin.apply_event(handover)
    observation = ToolObservation()
    observation.stamp.sec = 45
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
        placement_episode_started_sec=45.0,
        placement_episode_id="episode:45",
    )

    assert result is not None
    assert result["reducer_result"] == "accepted"
    assert state.lifecycle_stage == LIFECYCLE_MAYO_REUSE
    assert state.last_update_sec == 45.5


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
    assert result["reducer_reason"] == "cam4_mayo_no_surgeon_owned_instance"
    assert state.lifecycle_stage == LIFECYCLE_PREPOSITIONED_RIGHT
    assert state.location_type == "robot_right_hand"


def test_pre_handover_episode_stays_rejected_until_new_release_episode() -> None:
    twin = _thyroid_twin()
    state = _state(twin, "T04")
    twin._set_lifecycle(
        state,
        LIFECYCLE_PREPOSITIONED_RIGHT,
        location_type="robot_right_hand",
        location_id="robot_right_hand",
        confidence=1.0,
    )

    first = ToolObservation()
    first.stamp.sec = 44
    first.instrument_id = "Bovie surgical cautery"
    first.location_type = "mayo_stand"
    first.location_id = "mayo_stand"
    first.confidence = 0.92
    first.visible = True
    rejected = twin.reconcile_observation(
        first,
        source="cam4_rfdetr_mayo_observation",
        proposal_id="test:cam4-lease-before-handover",
    )

    assert rejected is not None
    assert rejected["reducer_reason"] == "cam4_mayo_no_surgeon_owned_instance"
    assert state.lifecycle_stage == LIFECYCLE_PREPOSITIONED_RIGHT

    handover = _event(
        "ToolHandoverCompleted",
        "T04",
        source="robot_right_hand",
        source_type="robot_right_hand",
        target="surgeon_receive_zone",
        target_type="handover_zone",
    )
    handover.stamp.sec = 45
    twin.apply_event(handover)
    renewed = ToolObservation()
    renewed.stamp.sec = 45
    renewed.instrument_id = "Bovie surgical cautery"
    renewed.location_type = "mayo_stand"
    renewed.location_id = "mayo_stand"
    renewed.confidence = 0.92
    renewed.visible = True
    still_rejected = twin.reconcile_observation(
        renewed,
        source="cam4_rfdetr_mayo_observation",
        proposal_id="test:cam4-lease-after-handover",
        placement_episode_started_sec=44.0,
        placement_episode_id="episode:44",
    )

    assert still_rejected is not None
    assert still_rejected["reducer_reason"] == (
        "cam4_mayo_episode_precedes_return_authority"
    )
    renewed.stamp.sec = 46
    accepted = twin.reconcile_observation(
        renewed,
        source="cam4_rfdetr_mayo_observation",
        proposal_id="test:cam4-new-release-after-handover",
        placement_episode_started_sec=45.5,
        placement_episode_id="episode:45.5",
    )
    assert accepted is not None
    assert accepted["reducer_result"] == "accepted"
    assert state.lifecycle_stage == LIFECYCLE_MAYO_REUSE
    assert state.location_type == "mayo_stand"
    assert state.last_update_sec == 46.0


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
    twin.set_initial_phase("P10")
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
        proposal_id="test:no-future-use",
        stamp_sec=50.0,
    )

    assert result is not None
    assert result["reducer_result"] == "accepted"
    # This compatibility field no longer encodes a hard phase-role
    # no-future-use verdict.  Autonomous recovery is driven by the n-gram
    # probability policy, while every requestable tool remains explicitly
    # requestable until completion.
    assert result["procedure_future_use_expected"] is True
    assert state.lifecycle_stage == LIFECYCLE_MAYO_REUSE
    assert state.next_required_transition == ""
    assert twin.state.active_recovery_tool_instances == []


def test_mayo_policy_evidence_rejects_noncanonical_mayo_location() -> None:
    twin = _thyroid_demo_twin()
    state = _state(twin, "T04")
    twin._set_lifecycle(
        state,
        LIFECYCLE_MAYO_REUSE,
        location_type="mayo_stand",
        location_id="mayo_stand",
        confidence=0.9,
    )
    # Simulate an inconsistent legacy/stale projection. A lifecycle label by
    # itself must never make the VLM policy path eligible.
    state.location_type = "mayo_reuse_zone"
    state.location_id = "mayo_reuse_zone"

    result = twin.record_mayo_policy_evidence(
        instrument_id=state.instance_id,
        evidence_type="recover",
        confidence=0.92,
        stability_sec=5.0,
        source="vlm_mayo_retrieve",
        proposal_id="test:noncanonical-mayo",
        stamp_sec=50.0,
    )

    assert result is not None
    assert result["reducer_result"] == "rejected"
    assert result["reducer_reason"] == "mayo_policy_tool_not_on_mayo"
    assert state.mayo_recovery_confidence == 0.0


def test_recovery_transaction_promotes_mayo_state_and_queues_once() -> None:
    twin = _thyroid_demo_twin()
    state = _state(twin, "T04")
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
    state = _state(twin, "T04")
    twin._set_lifecycle(
        state,
        LIFECYCLE_SURGEON_OWNED,
        location_type="surgeon_hand",
        location_id="surgeon_hand",
        confidence=1.0,
    )
    twin._open_recovery_transaction(state.instance_id, "surgeon_return_request")
    twin._recovery_transaction_opened_stamp_by_instance[state.instance_id] = 9.0

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
        placement_episode_started_sec=9.5,
        placement_episode_id="episode:9.5",
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

    # A surgeon-held tool is not retroactively made a Mayo recovery target.
    # With no eligible Mayo snapshot, voice completion settles immediately.
    assert twin.state.execution_state == "completed"
    assert state.lifecycle_stage == LIFECYCLE_SURGEON_OWNED
    assert state.location_type == "surgeon_hand"
    assert twin.state.active_recovery_tool_instances == []


def test_completion_cleanup_snapshots_only_eligible_mayo_instances() -> None:
    twin = _thyroid_demo_twin()
    adson = _state(twin, "T02")
    bovie = _state(twin, "T04")
    bipolar = _state(twin, "T07")
    mosquito = _state(twin, "T08")
    for state in (adson, bovie, bipolar, mosquito):
        twin._set_lifecycle(
            state,
            LIFECYCLE_MAYO_REUSE,
            location_type="mayo_stand",
            location_id="mayo_stand",
            confidence=1.0,
        )

    twin._begin_completion_cleanup()
    twin._recompute_transient_state()

    assert twin.completion_recovery_target_instances == (
        adson.instance_id,
        mosquito.instance_id,
    )
    assert twin.state.execution_state == "finishing"
    assert bovie.lifecycle_stage == LIFECYCLE_MAYO_REUSE
    assert bipolar.lifecycle_stage == LIFECYCLE_MAYO_REUSE


def test_completion_cleanup_releases_only_excluded_preposition_before_recovery() -> None:
    twin = _thyroid_demo_twin()
    adson = _state(twin, "T02")
    bovie = _state(twin, "T04")
    twin._set_lifecycle(
        adson,
        LIFECYCLE_MAYO_REUSE,
        location_type="mayo_stand",
        location_id="mayo_stand",
        confidence=1.0,
    )
    twin._set_lifecycle(
        bovie,
        LIFECYCLE_PREPOSITIONED_RIGHT,
        location_type="robot_right_hand",
        location_id="robot_right_hand",
        confidence=1.0,
    )

    twin._begin_completion_cleanup()
    twin.normalize_for_publish()

    assert twin.state.execution_state == "finishing"
    assert twin.completion_recovery_target_instances == (adson.instance_id,)
    assert bovie.next_required_transition == "return_unused_preposition"
    assert adson.next_required_transition == "recover_left"


def test_completion_cleanup_returns_prepared_recovery_tool_directly_to_tray() -> None:
    twin = _thyroid_demo_twin()
    mosquito = _state(twin, "T08")
    twin._set_lifecycle(
        mosquito,
        LIFECYCLE_PREPOSITIONED_RIGHT,
        location_type="robot_right_hand",
        location_id="robot_right_hand",
        confidence=1.0,
    )

    twin._begin_completion_cleanup()
    twin.normalize_for_publish()

    assert twin.state.execution_state == "finishing"
    assert twin.completion_recovery_target_instances == ()
    assert mosquito.next_required_transition == "return_preposition_to_tray"


def test_completion_cleanup_target_snapshot_is_not_widened_by_later_mayo_state() -> None:
    twin = _thyroid_demo_twin()
    adson = _state(twin, "T02")
    mosquito = _state(twin, "T08")
    twin._set_lifecycle(
        adson,
        LIFECYCLE_MAYO_REUSE,
        location_type="mayo_stand",
        location_id="mayo_stand",
        confidence=1.0,
    )

    twin._begin_completion_cleanup()
    assert twin.completion_recovery_target_instances == (adson.instance_id,)
    twin._set_lifecycle(
        mosquito,
        LIFECYCLE_MAYO_REUSE,
        location_type="mayo_stand",
        location_id="mayo_stand",
        confidence=1.0,
    )
    twin._recompute_transient_state()

    assert twin.completion_recovery_target_instances == (adson.instance_id,)
    assert twin.state.execution_state == "finishing"
    assert mosquito.next_required_transition == ""

    twin._set_lifecycle(
        adson,
        LIFECYCLE_RETURNED_HOME,
        location_type="tool_rack",
        location_id="tool_rack",
        confidence=1.0,
    )
    twin._recompute_transient_state()

    assert twin.state.execution_state == "completed"
    assert mosquito.lifecycle_stage == LIFECYCLE_MAYO_REUSE


def test_legacy_complete_procedure_event_uses_the_same_mayo_snapshot() -> None:
    twin = _thyroid_demo_twin()
    adson = _state(twin, "T02")
    twin._set_lifecycle(
        adson,
        LIFECYCLE_MAYO_REUSE,
        location_type="mayo_stand",
        location_id="mayo_stand",
        confidence=1.0,
    )
    request = SurgeonRequest()
    request.event_type = "complete_procedure"

    twin.update_surgeon_request(request)

    assert twin.state.execution_state == "finishing"
    assert twin.completion_recovery_target_instances == (adson.instance_id,)


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
    assert result["reducer_result"] == "rejected"
    assert result["reducer_reason"] == "cam4_mayo_no_surgeon_owned_instance"
    assert state.lifecycle_stage == LIFECYCLE_RETURNED_HOME
    assert state.location_type == state.home_location_type

    twin.reset_runtime()
    reset_result = twin.reconcile_observation(
        observation,
        source="vlm_cam4_mayo_observation",
        proposal_id="test:post-reset-mayo",
    )
    assert reset_result is not None
    assert reset_result["reducer_result"] == "rejected"
    assert reset_result["reducer_reason"] == "cam4_mayo_no_surgeon_owned_instance"
