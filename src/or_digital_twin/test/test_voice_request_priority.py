from __future__ import annotations

import json
from pathlib import Path

from or_digital_twin.models import (
    LIFECYCLE_MAYO_RECOVERY,
    LIFECYCLE_MAYO_REUSE,
    LIFECYCLE_PREPOSITIONED_RIGHT,
    LIFECYCLE_RECOVERING_LEFT,
    LIFECYCLE_RETURNED_HOME,
    LIFECYCLE_SURGEON_OWNED,
)
from or_digital_twin.twin import ORDigitalTwin
from procedure_spec import load_bundle
from surgical_msgs.msg import SurgeonRequest, TwinEvent


def _twin() -> ORDigitalTwin:
    return ORDigitalTwin(
        load_bundle(
            Path(__file__).parents[2]
            / "procedure_spec"
            / "procedure_spec"
            / "specs"
            / "thyroidectomy_demo"
        )
    )


def _two_adson_twin() -> ORDigitalTwin:
    twin = _twin()
    next(
        instrument
        for instrument in twin.spec.bundle.instruments
        if instrument.id == "T02"
    ).inventory_count = 2
    # Inventory instances are materialized during construction, so rebuild
    # from the isolated synthetic spec after changing its catalog count.
    return ORDigitalTwin(twin.spec)


def _request(tool_id: str, event_type: str = "request_tool") -> SurgeonRequest:
    request = SurgeonRequest()
    request.event_type = event_type
    request.requested_tool = tool_id
    request.ready_for_handover = True
    return request


def _event(event_type: str, tool_id: str, instance_id: str) -> TwinEvent:
    event = TwinEvent()
    event.event_type = event_type
    event.instrument_id = tool_id
    event.instance_id = instance_id
    event.confidence = 1.0
    return event


def _authoritative_completion(
    *,
    tool_id: str = "T04",
    instance_id: str = "T04#1",
    command_id: str = "controller-handover-1",
    request_generation: int = 1,
    stamp_sec: int = 100,
    final_state: str = "completed",
    event_type: str = "ToolHandoverCompleted",
    source_location: str = "tray",
    target_location: str = "surgeon",
    projection_step: str = "handover_completed",
    projection_index: int = 1,
    projection_count: int = 1,
    event_source_location: str | None = None,
    event_target_location: str | None = None,
) -> TwinEvent:
    event = _event(event_type, tool_id, instance_id)
    event.stamp.sec = stamp_sec
    event.status = "prepared" if event_type == "ToolPrepared" else "completed"
    event_source = event_source_location or source_location
    event_target = event_target_location or target_location
    event.source_location_type = event_source
    event.source_location_id = event_source
    event.target_location_type = event_target
    event.target_location_id = event_target
    event.detail_json = json.dumps(
        {
            "authoritative_controller_completion": True,
            "command_id": command_id,
            "controller_final_state": final_state,
            "controller_reason_code": final_state,
            "controller_source_location": source_location,
            "controller_target_location": target_location,
            "controller_projection_step": projection_step,
            "controller_projection_index": projection_index,
            "controller_projection_count": projection_count,
            "request_generation": request_generation,
        }
    )
    return event


def test_authoritative_completion_projects_location_without_rechecking_arm_or_lifecycle() -> None:
    twin = _twin()
    completed = twin.instrument_states["T04#1"]
    stale_right_hand = twin.instrument_states["T07#1"]
    assert twin.update_resolved_voice_tool_handover("T04") == "T04"
    generation = twin.state.surgeon_request_generation
    twin._set_lifecycle(
        stale_right_hand,
        LIFECYCLE_PREPOSITIONED_RIGHT,
        location_type="robot_right_hand",
        location_id="robot_right_hand",
        confidence=1.0,
    )
    twin._recompute_transient_state()

    twin.apply_event(
        _authoritative_completion(request_generation=generation)
    )

    assert completed.lifecycle_stage == LIFECYCLE_SURGEON_OWNED
    assert completed.location_type == "surgeon_hand"
    assert completed.location_id == "surgeon_hand"
    assert twin.state.surgeon_request_tool == ""


def test_authoritative_completion_rejects_duplicate_stale_and_wrong_correlation() -> None:
    twin = _twin()
    completed = twin.instrument_states["T04#1"]
    assert twin.update_resolved_voice_tool_handover("T04") == "T04"
    generation = twin.state.surgeon_request_generation
    first = _authoritative_completion(request_generation=generation)
    twin.apply_event(first)
    twin._set_lifecycle(
        completed,
        LIFECYCLE_RETURNED_HOME,
        location_type=completed.home_location_type,
        location_id=completed.home_location_id,
        confidence=1.0,
    )

    duplicate = _authoritative_completion(
        request_generation=generation,
        stamp_sec=101,
    )
    twin.apply_event(duplicate)
    stale = _authoritative_completion(
        command_id="controller-handover-stale",
        request_generation=generation,
        stamp_sec=99,
    )
    twin.apply_event(stale)
    wrong_instance = _authoritative_completion(
        instance_id="T02#1",
        command_id="controller-handover-wrong-instance",
        request_generation=generation,
        stamp_sec=102,
    )
    twin.apply_event(wrong_instance)

    assert completed.lifecycle_stage == LIFECYCLE_RETURNED_HOME
    rejected_reasons = {
        event["reason"]
        for event in twin.event_history
        if event["event_type"]
        == "AuthoritativeToolHandoverCompletionRejected"
    }
    assert "duplicate_completion_command" in rejected_reasons
    assert "stale_completion_stamp" in rejected_reasons
    assert "instrument_instance_correlation_mismatch" in rejected_reasons


def test_authoritative_completion_rejects_failed_and_canceled_results() -> None:
    for index, final_state in enumerate(("failed", "canceled"), start=1):
        twin = _twin()
        state = twin.instrument_states["T04#1"]
        original_stage = state.lifecycle_stage
        event = _authoritative_completion(
            command_id=f"controller-handover-{final_state}",
            stamp_sec=100 + index,
            final_state=final_state,
        )
        twin.apply_event(event)

        assert state.lifecycle_stage == original_stage
        assert any(
            row["event_type"]
            == "AuthoritativeToolHandoverCompletionRejected"
            and row["reason"] == "controller_result_not_completed"
            for row in twin.event_history
        )


def test_authoritative_prepare_overrides_stale_nonvoice_surgeon_belief() -> None:
    twin = _twin()
    state = twin.instrument_states["T04#1"]
    conflicting = twin.instrument_states["T07#1"]
    twin._set_lifecycle(
        state,
        LIFECYCLE_SURGEON_OWNED,
        location_type="surgeon_hand",
        location_id="surgeon_hand",
        confidence=1.0,
    )
    twin._set_lifecycle(
        conflicting,
        LIFECYCLE_PREPOSITIONED_RIGHT,
        location_type="robot_right_hand",
        location_id="robot_right_hand",
        confidence=1.0,
    )
    twin._recompute_transient_state()

    event = _authoritative_completion(
        command_id="controller-prepare-1",
        event_type="ToolPrepared",
        source_location="tray",
        target_location="robot",
        projection_step="prepared",
    )
    twin.apply_event(event)

    assert state.lifecycle_stage == LIFECYCLE_PREPOSITIONED_RIGHT
    assert state.location_type == "robot"
    assert "right_arm_overloaded" in twin.state.safety_flags


def test_authoritative_unused_return_overrides_right_arm_belief_and_parks_on_mayo() -> None:
    twin = _twin()
    state = twin.instrument_states["T04#1"]
    conflicting = twin.instrument_states["T02#1"]
    twin._set_lifecycle(
        state,
        LIFECYCLE_RETURNED_HOME,
        location_type=state.home_location_type,
        location_id=state.home_location_id,
        confidence=1.0,
    )
    twin._set_lifecycle(
        conflicting,
        LIFECYCLE_PREPOSITIONED_RIGHT,
        location_type="robot_right_hand",
        location_id="robot_right_hand",
        confidence=1.0,
    )

    event = _authoritative_completion(
        command_id="controller-unused-return-1",
        event_type="UnusedPrepositionReturned",
        source_location="robot",
        target_location="mayo",
        event_source_location="robot",
        event_target_location="mayo_stand",
        projection_step="unused_preposition_returned",
    )
    twin.apply_event(event)

    assert state.lifecycle_stage == LIFECYCLE_MAYO_REUSE
    assert state.location_type == "mayo_stand"
    assert state.location_id == "mayo_stand"


def test_authoritative_robot_to_tray_recovery_overrides_local_lifecycle() -> None:
    twin = _twin()
    state = twin.instrument_states["T04#1"]
    twin._set_lifecycle(
        state,
        LIFECYCLE_SURGEON_OWNED,
        location_type="surgeon_hand",
        location_id="surgeon_hand",
        confidence=1.0,
    )

    twin.apply_event(
        _authoritative_completion(
            command_id="controller-robot-tray-recovery-1",
            event_type="ToolReturnedToTray",
            source_location="robot",
            target_location="tray",
            projection_step="returned_to_tray",
        )
    )

    assert state.lifecycle_stage == LIFECYCLE_RETURNED_HOME
    assert state.location_type == "tray"


def test_authoritative_mayo_retrieve_steps_override_left_arm_belief_and_share_command() -> None:
    twin = _twin()
    state = twin.instrument_states["T04#1"]
    conflicting = twin.instrument_states["T02#1"]
    twin._set_lifecycle(
        state,
        LIFECYCLE_SURGEON_OWNED,
        location_type="surgeon_hand",
        location_id="surgeon_hand",
        confidence=1.0,
    )
    twin._set_lifecycle(
        conflicting,
        LIFECYCLE_RECOVERING_LEFT,
        location_type="robot_left_hand",
        location_id="robot_left_hand",
        confidence=1.0,
    )

    retrieved = _authoritative_completion(
        command_id="controller-retrieve-1",
        stamp_sec=200,
        event_type="ToolRetrievedFromMayo",
        source_location="mayo",
        target_location="tray",
        event_target_location="robot_left_hand",
        projection_step="retrieved_from_mayo",
        projection_index=1,
        projection_count=2,
    )
    returned = _authoritative_completion(
        command_id="controller-retrieve-1",
        stamp_sec=200,
        event_type="ToolReturnedToTray",
        source_location="mayo",
        target_location="tray",
        event_source_location="robot_left_hand",
        event_target_location="tray",
        projection_step="returned_to_tray",
        projection_index=2,
        projection_count=2,
    )

    twin.apply_event(retrieved)
    assert state.lifecycle_stage == LIFECYCLE_RECOVERING_LEFT
    twin.apply_event(returned)

    assert state.lifecycle_stage == LIFECYCLE_RETURNED_HOME
    assert state.location_type == "tray"


def test_authoritative_multi_projection_command_cannot_switch_instance() -> None:
    twin = _twin()
    first = twin.instrument_states["T04#1"]
    other = twin.instrument_states["T02#1"]
    twin.apply_event(
        _authoritative_completion(
            command_id="controller-retrieve-correlated",
            stamp_sec=210,
            event_type="ToolRetrievedFromMayo",
            source_location="mayo",
            target_location="tray",
            event_target_location="robot_left_hand",
            projection_step="retrieved_from_mayo",
            projection_index=1,
            projection_count=2,
        )
    )

    twin.apply_event(
        _authoritative_completion(
            tool_id="T02",
            instance_id="T02#1",
            command_id="controller-retrieve-correlated",
            stamp_sec=210,
            event_type="ToolReturnedToTray",
            source_location="mayo",
            target_location="tray",
            event_source_location="robot_left_hand",
            event_target_location="tray",
            projection_step="returned_to_tray",
            projection_index=2,
            projection_count=2,
        )
    )

    assert first.lifecycle_stage == LIFECYCLE_RECOVERING_LEFT
    assert other.lifecycle_stage != LIFECYCLE_RETURNED_HOME
    assert any(
        row["event_type"] == "AuthoritativeToolHandoverCompletionRejected"
        and row["reason"] == "command_instance_correlation_mismatch"
        for row in twin.event_history
    )


def test_latest_typed_voice_request_supersedes_older_voice_and_leads_queue() -> None:
    twin = _twin()
    twin.update_surgeon_request(_request("T04"))

    assert twin.update_resolved_voice_tool_handover("T02") == "T02"
    first_voice_generation = twin.state.surgeon_request_generation
    assert twin.update_resolved_voice_tool_handover("T04") == "T04"

    summary = twin.request_queue_summary()
    assert summary["active_request_event_type"] == "voice_request"
    assert summary["active_request_tool"] == "T04"
    assert summary["active_request_generation"] > first_voice_generation
    assert summary["queued_event_types"] == ["voice_request", "request_tool"]
    assert summary["queued_tools"] == ["T04", "T04"]
    assert any(
        event["event_type"] == "SurgeonRequestSuperseded"
        and event["superseded_generation"] == first_voice_generation
        and event["incoming_generation"]
        == summary["active_request_generation"]
        and event["reason"]
        == "newer_validated_voice_request_latest_wins"
        for event in twin.event_history
    )

    # A later implicit/visual-style request remains lower priority.
    twin.update_surgeon_request(_request("T07", "extend_hand_for_handover"))
    assert twin.state.surgeon_intent == "voice_request"
    assert twin.state.surgeon_request_tool == "T04"
    assert twin.state.surgeon_request_generation == summary[
        "active_request_generation"
    ]


def test_repeated_typed_voice_request_gets_a_fresh_generation() -> None:
    twin = _twin()

    assert twin.update_resolved_voice_tool_handover("T04") == "T04"
    first_generation = twin.state.surgeon_request_generation
    assert twin.update_resolved_voice_tool_handover("T04") == "T04"

    assert twin.request_queue_summary()["queued_tools"] == ["T04"]
    assert twin.state.surgeon_request_generation > first_generation


def test_stale_controller_completion_cannot_dequeue_newer_voice_generation() -> None:
    twin = _twin()
    state = twin.instrument_states["T04#1"]

    assert twin.update_resolved_voice_tool_handover("T04") == "T04"
    stale_generation = twin.state.surgeon_request_generation
    twin._set_lifecycle(
        state,
        LIFECYCLE_PREPOSITIONED_RIGHT,
        location_type="robot_right_hand",
        location_id="robot_right_hand",
        confidence=1.0,
    )
    assert twin.update_resolved_voice_tool_handover("T04") == "T04"
    latest_generation = twin.state.surgeon_request_generation

    completion = _event("ToolHandoverCompleted", "T04", state.instance_id)
    completion.detail_json = json.dumps(
        {
            "command_id": "older-command",
            "request_generation": stale_generation,
        }
    )
    twin.apply_event(completion)

    assert state.lifecycle_stage == LIFECYCLE_SURGEON_OWNED
    assert twin.state.surgeon_request_tool == "T04"
    assert twin.state.surgeon_request_generation == latest_generation
    assert any(
        event["event_type"] == "StaleSurgeonRequestCompletionIgnored"
        and event["completion_request_generation"]
        == stale_generation
        and event["active_request_generation"]
        == latest_generation
        for event in twin.event_history
    )


def test_latest_same_tool_voice_uses_another_instance_when_old_cue_is_committed() -> None:
    twin = _two_adson_twin()

    twin.update_resolved_voice_tool_handover("T02")
    old_generation = twin.state.surgeon_request_generation
    old_instance_id = twin.state.surgeon_request_instance_id
    first = twin.instrument_states[old_instance_id]
    twin._set_lifecycle(
        first,
        LIFECYCLE_PREPOSITIONED_RIGHT,
        location_type="robot_right_hand",
        location_id="robot_right_hand",
        confidence=1.0,
    )

    twin.update_resolved_voice_tool_handover("T02")

    new_instance_id = twin.state.surgeon_request_instance_id
    assert new_instance_id in {"T02#1", "T02#2"}
    assert new_instance_id != old_instance_id
    assert twin.state.surgeon_request_generation > old_generation

    old_completion = _event("ToolHandoverCompleted", "T02", old_instance_id)
    old_completion.detail_json = json.dumps(
        {"request_generation": old_generation, "command_id": "old-t02"}
    )
    twin.apply_event(old_completion)

    assert twin.state.surgeon_request_instance_id == new_instance_id
    assert twin.state.surgeon_request_generation > old_generation


def test_legacy_completion_without_generation_remains_compatible() -> None:
    twin = _twin()
    state = twin.instrument_states["T04#1"]
    twin.update_resolved_voice_tool_handover("T04")
    twin._set_lifecycle(
        state,
        LIFECYCLE_PREPOSITIONED_RIGHT,
        location_type="robot_right_hand",
        location_id="robot_right_hand",
        confidence=1.0,
    )

    twin.apply_event(_event("ToolHandoverCompleted", "T04", state.instance_id))

    assert twin.state.surgeon_request_tool == ""
    assert twin.state.surgeon_request_generation == 0


def test_visual_lifecycle_update_does_not_clear_active_voice_request() -> None:
    twin = _twin()
    state = twin.instrument_states["T04#1"]
    twin.update_resolved_voice_tool_handover("T04")
    generation = twin.state.surgeon_request_generation

    twin._set_lifecycle(
        state,
        LIFECYCLE_SURGEON_OWNED,
        location_type="surgeon_hand",
        location_id="surgeon_hand",
        confidence=0.95,
    )
    twin._recompute_transient_state()

    assert twin.state.surgeon_request_tool == "T04"
    assert twin.state.surgeon_request_generation == generation


def test_phase_uncertainty_is_not_a_handover_gate() -> None:
    twin = _twin()
    twin.state.phase_uncertain = True
    twin.update_surgeon_request(_request("T04"))

    assert twin.handover_allowed() is True


def test_voice_mayo_retrieval_preserves_generation_until_handover() -> None:
    twin = _twin()
    state = twin.instrument_states["T04#1"]
    twin._set_lifecycle(
        state,
        LIFECYCLE_MAYO_RECOVERY,
        location_type="mayo_stand",
        location_id="mayo_stand",
        confidence=1.0,
    )
    twin.update_resolved_voice_tool_handover("T04")
    generation = twin.state.surgeon_request_generation

    retrieved = _event("ToolRetrievedFromMayo", "T04", state.instance_id)
    retrieved.source_location_type = "mayo_stand"
    retrieved.source_location_id = "mayo_stand"
    retrieved.target_location_type = "robot_left_hand"
    retrieved.target_location_id = "robot_left_hand"
    twin.apply_event(retrieved)

    assert state.lifecycle_stage == LIFECYCLE_RECOVERING_LEFT
    assert twin.state.surgeon_request_tool == "T04"
    assert twin.state.surgeon_request_generation == generation

    returned = _event("ToolReturnedToTray", "T04", state.instance_id)
    returned.source_location_type = "robot_left_hand"
    returned.source_location_id = "robot_left_hand"
    returned.target_location_type = state.home_location_type
    returned.target_location_id = state.home_location_id
    twin.apply_event(returned)

    assert state.lifecycle_stage == LIFECYCLE_RETURNED_HOME
    assert twin.state.surgeon_request_tool == "T04"
    assert twin.state.surgeon_request_generation == generation
    assert twin.handover_allowed() is True
    assert any(
        event["event_type"]
        == "SurgeonHandoverRequestPreservedAfterMayoRetrieval"
        and event["request_generation"] == generation
        for event in twin.event_history
    )
