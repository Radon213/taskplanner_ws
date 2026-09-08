from __future__ import annotations

import json
from pathlib import Path

from or_digital_twin.models import (
    ActiveRobotTask,
    InstrumentBelief,
    LIFECYCLE_HOME_RACK,
    LIFECYCLE_MAYO_RECOVERY,
    LIFECYCLE_MAYO_REUSE,
    LIFECYCLE_PREPOSITIONED_RIGHT,
    LIFECYCLE_RETURNED_HOME,
    LIFECYCLE_SURGEON_OWNED,
)
from or_digital_twin.twin import ORDigitalTwin
from procedure_spec import load_bundle
from surgical_msgs.msg import FilteredPhase, SurgeonRequest, TwinEvent


def _demo_spec():
    spec = load_bundle(
        Path(__file__).parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
        / "thyroidectomy_demo"
    )
    # The reviewed Production demo now has one physical Adson.  This suite is
    # about duplicate-instance mechanics, so give its isolated fixture a
    # second instance instead of making production inventory serve as test
    # scaffolding.
    next(
        instrument
        for instrument in spec.bundle.instruments
        if instrument.id == "T02"
    ).inventory_count = 2
    return spec


def _event(event_type: str, tool_id: str, **detail) -> TwinEvent:
    event = TwinEvent()
    event.event_type = event_type
    event.instrument_id = tool_id
    event.confidence = 1.0
    event.detail_json = json.dumps(detail)
    return event


def _handover_active_request(twin: ORDigitalTwin) -> str:
    instance_id = twin.state.surgeon_request_instance_id
    tool_id = twin.state.surgeon_request_tool
    twin.apply_event(
        _event(
            "RobotTaskStarted",
            tool_id,
            task_id=f"task:{instance_id}",
            task_type="tool_handover",
        )
    )
    twin.apply_event(_event("RobotGraspedTool", tool_id))
    assert twin.instrument_states[instance_id].lifecycle_stage == (
        LIFECYCLE_PREPOSITIONED_RIGHT
    )
    twin.apply_event(_event("ToolHandoverCompleted", tool_id))
    twin.apply_event(
        _event(
            "RobotTaskCompleted",
            tool_id,
            task_id=f"task:{instance_id}",
        )
    )
    return instance_id


def _place_adson_duplicate_on_tray_and_mayo(
    twin: ORDigitalTwin,
) -> tuple[InstrumentBelief, InstrumentBelief]:
    tray_instance = twin.instrument_states["T02#1"]
    mayo_instance = twin.instrument_states["T02#2"]
    twin._set_lifecycle(
        tray_instance,
        LIFECYCLE_HOME_RACK,
        location_type=tray_instance.home_location_type,
        location_id=tray_instance.home_location_id,
        confidence=1.0,
    )
    twin._set_lifecycle(
        mayo_instance,
        LIFECYCLE_MAYO_REUSE,
        location_type="mayo_stand",
        location_id="mayo_stand",
        confidence=1.0,
        placement_evidence="controller_confirmed_unused_preposition_to_mayo",
    )
    return tray_instance, mayo_instance


def test_stale_task_events_cannot_replace_or_clear_a_current_active_task() -> None:
    twin = ORDigitalTwin(_demo_spec())

    twin.apply_event(
        _event(
            "RobotTaskStarted",
            "T02",
            task_id="current-action",
            task_type="tool_handover",
        )
    )
    twin.apply_event(
        _event(
            "RobotTaskStarted",
            "T02",
            task_id="previous-action",
            task_type="tool_handover",
        )
    )
    twin.apply_event(
        _event(
            "RobotTaskCompleted",
            "T02",
            task_id="previous-action",
        )
    )

    assert twin.state.active_robot_task is not None
    assert twin.state.active_robot_task.task_id == "current-action"
    assert twin.event_history[-1]["event_type"] == "RobotTaskCompletionIgnored"

    twin.apply_event(
        _event(
            "RobotTaskCompleted",
            "T02",
            task_id="current-action",
        )
    )

    assert twin.state.active_robot_task is None


def test_inventory_count_creates_stable_instance_ids_and_keeps_type_ids() -> None:
    twin = ORDigitalTwin(_demo_spec())

    assert [state.instance_id for state in twin._instances_for_type("T02")] == [
        "T02#1",
        "T02#2",
    ]
    assert [state.instrument_id for state in twin._instances_for_type("T02")] == [
        "T02",
        "T02",
    ]
    payload = [
        row for row in twin.instrument_payload() if row["instrument_id"] == "T02"
    ]
    assert {row["instance_id"] for row in payload} == {"T02#1", "T02#2"}


def test_request_skips_surgeon_owned_duplicate_and_handover_uses_tray_copy() -> None:
    twin = ORDigitalTwin(_demo_spec())

    # A surgeon-held instance is not a controllable pickup source.  The only
    # new handover must use the other physical copy still on the tray.
    twin._set_lifecycle(
        twin.instrument_states["T02#1"],
        LIFECYCLE_SURGEON_OWNED,
        location_type="surgeon_hand",
        location_id="surgeon_hand",
        confidence=1.0,
    )

    assert twin.update_explicit_request("Adson") == "T02"
    queued = list(twin.state.surgeon_request_queue)
    assert [cue.instance_id for cue in queued] == ["T02#2"]
    assert twin.handover_allowed() is True
    assert _handover_active_request(twin) == "T02#2"
    assert twin.instrument_states["T02#1"].lifecycle_stage == (
        LIFECYCLE_SURGEON_OWNED
    )
    assert twin.instrument_states["T02#2"].lifecycle_stage == (
        LIFECYCLE_SURGEON_OWNED
    )
    assert twin.state.surgeon_request_tool == ""


def test_typed_voice_request_rejects_surgeon_only_supplier_before_queue() -> None:
    twin = ORDigitalTwin(_demo_spec())
    state = twin.instrument_states["T04#1"]
    twin._set_lifecycle(
        state,
        LIFECYCLE_SURGEON_OWNED,
        location_type="surgeon_hand",
        location_id="surgeon_hand",
        confidence=1.0,
    )

    assert twin._enqueue_surgeon_request(
        event_type="voice_request",
        instrument_id="T04",
        typed_voice_execution_required=True,
    ) is False
    assert twin.state.surgeon_request_instance_id == ""
    assert twin.update_resolved_voice_tool_handover("T04") == ""
    assert list(twin.state.surgeon_request_queue) == []
    assert twin.state.surgeon_request_instance_id == ""
    assert twin.event_history[-1]["event_type"] == "ResolvedVoiceToolHandoverUpdated"
    assert twin.event_history[-1]["accepted"] is False


def test_structured_request_rejects_surgeon_only_supplier_before_history_admission() -> None:
    twin = ORDigitalTwin(_demo_spec())
    state = twin.instrument_states["T04#1"]
    twin._set_lifecycle(
        state,
        LIFECYCLE_SURGEON_OWNED,
        location_type="surgeon_hand",
        location_id="surgeon_hand",
        confidence=1.0,
    )
    request = SurgeonRequest()
    request.event_type = "voice_request"
    request.requested_tool = "T04"
    request.ready_for_handover = True

    assert twin.update_surgeon_request(request) == ""
    assert list(twin.state.surgeon_request_queue) == []
    assert twin.state.surgeon_request_instance_id == ""
    assert twin.event_history[-1]["accepted"] is False
    assert twin.event_history[-1]["reason"] == "tool_inventory_unavailable"


def test_typed_voice_request_rejects_missing_or_unknown_supplier_before_queue() -> None:
    missing = ORDigitalTwin(_demo_spec())
    del missing.instrument_states["T04#1"]

    assert missing.update_resolved_voice_tool_handover("T04") == ""
    assert list(missing.state.surgeon_request_queue) == []
    assert missing.state.surgeon_request_instance_id == ""

    unknown = ORDigitalTwin(_demo_spec())
    state = unknown.instrument_states["T04#1"]
    state.home_location_type = "unknown"
    state.home_location_id = "unknown"
    state.location_type = "unknown"
    state.location_id = "unknown"

    assert unknown.update_resolved_voice_tool_handover("T04") == ""
    assert list(unknown.state.surgeon_request_queue) == []
    assert unknown.state.surgeon_request_instance_id == ""


def test_typed_voice_request_accepts_canonical_tray_mayo_and_robot_sources() -> None:
    tray = ORDigitalTwin(_demo_spec())
    assert tray.update_resolved_voice_tool_handover("T04") == "T04"
    assert tray.state.surgeon_request_instance_id == "T04#1"
    assert tray.handover_allowed() is True

    mayo = ORDigitalTwin(_demo_spec())
    mayo_state = mayo.instrument_states["T04#1"]
    mayo._set_lifecycle(
        mayo_state,
        LIFECYCLE_MAYO_REUSE,
        location_type="mayo_stand",
        location_id="mayo_stand",
        confidence=1.0,
    )
    assert mayo.state.cam4_mayo_hand_present is True
    assert mayo.update_resolved_voice_tool_handover("T04") == "T04"
    assert mayo.state.surgeon_request_instance_id == mayo_state.instance_id
    assert mayo.handover_allowed() is True

    robot = ORDigitalTwin(_demo_spec())
    robot_state = robot.instrument_states["T04#1"]
    robot._set_lifecycle(
        robot_state,
        LIFECYCLE_PREPOSITIONED_RIGHT,
        location_type="robot_right_hand",
        location_id="robot_right_hand",
        confidence=1.0,
    )
    assert robot.update_resolved_voice_tool_handover("T04") == "T04"
    assert robot.state.surgeon_request_instance_id == robot_state.instance_id
    assert robot.handover_allowed() is True

    # The current controller projection still uses the legacy generic robot
    # anchor for an owned right-hand tool. It is the same controllable source,
    # not an unknown robot observation.
    legacy_robot = ORDigitalTwin(_demo_spec())
    legacy_robot_state = legacy_robot.instrument_states["T04#1"]
    legacy_robot._set_lifecycle(
        legacy_robot_state,
        LIFECYCLE_PREPOSITIONED_RIGHT,
        location_type="robot",
        location_id="robot",
        confidence=1.0,
    )
    assert legacy_robot.update_resolved_voice_tool_handover("T04") == "T04"
    assert legacy_robot.handover_allowed() is True


def test_voice_request_supersedes_mayo_recovery_while_cam4_has_hand() -> None:
    twin = ORDigitalTwin(_demo_spec())
    state = twin.instrument_states["T04#1"]
    twin._set_lifecycle(
        state,
        LIFECYCLE_MAYO_RECOVERY,
        location_type="mayo_stand",
        location_id="mayo_stand",
        confidence=1.0,
    )

    assert twin.state.cam4_mayo_hand_present is True
    assert twin.update_resolved_voice_tool_handover("T04") == "T04"
    assert twin.state.surgeon_request_instance_id == state.instance_id
    assert twin.handover_allowed() is True


def test_explicit_request_prefers_same_type_prepositioned_instance() -> None:
    twin = ORDigitalTwin(_demo_spec())
    prepositioned = twin.instrument_states["T02#1"]
    twin._set_lifecycle(
        prepositioned,
        LIFECYCLE_PREPOSITIONED_RIGHT,
        location_type="robot_right_hand",
        location_id="robot_right_hand",
        confidence=1.0,
    )
    twin.state.right_hand_tool = prepositioned.instrument_id
    twin.state.right_hand_tool_instance_id = prepositioned.instance_id
    twin.state.prepositioned_tool = prepositioned.instrument_id
    twin.state.prepositioned_tool_instance_id = prepositioned.instance_id

    assert twin.update_explicit_request("Adson") == "T02"
    assert twin.state.surgeon_request_instance_id == "T02#1"
    twin.normalize_for_publish()

    assert prepositioned.next_required_transition == ""


def test_explicit_request_prefers_same_type_mayo_instance_over_tray() -> None:
    twin = ORDigitalTwin(_demo_spec())
    _tray_instance, mayo_instance = _place_adson_duplicate_on_tray_and_mayo(
        twin
    )
    twin.set_cam4_mayo_hand_present(False)

    assert twin.update_explicit_request("Adson") == "T02"
    assert twin.state.surgeon_request_instance_id == mayo_instance.instance_id


def test_explicit_voice_request_keeps_mayo_supplier_while_cam4_has_hand() -> None:
    twin = ORDigitalTwin(_demo_spec())
    _tray_instance, mayo_instance = _place_adson_duplicate_on_tray_and_mayo(
        twin
    )

    assert twin.state.cam4_mayo_hand_present is True
    assert twin.update_explicit_request("Adson") == "T02"
    assert twin.state.surgeon_request_instance_id == mayo_instance.instance_id


def test_cam4_hand_rebinds_uncommitted_nonvoice_request_to_tray_duplicate() -> None:
    twin = ORDigitalTwin(_demo_spec())
    tray_instance, mayo_instance = _place_adson_duplicate_on_tray_and_mayo(
        twin
    )
    request = SurgeonRequest()
    request.event_type = "request_tool"
    request.requested_tool = "T02"
    request.ready_for_handover = True
    twin.set_cam4_mayo_hand_present(False)
    assert twin.update_surgeon_request(request) == "T02"
    assert twin.state.surgeon_request_instance_id == mayo_instance.instance_id
    request_generation = twin.state.surgeon_request_generation

    assert twin.set_cam4_mayo_hand_present(True)
    assert twin.state.surgeon_request_instance_id == tray_instance.instance_id
    assert twin.state.surgeon_request_generation == request_generation


def test_cam4_hand_does_not_rebind_uncommitted_voice_mayo_request() -> None:
    twin = ORDigitalTwin(_demo_spec())
    _tray_instance, mayo_instance = _place_adson_duplicate_on_tray_and_mayo(
        twin
    )
    twin.set_cam4_mayo_hand_present(False)
    assert twin.update_explicit_request("Adson") == "T02"
    request_generation = twin.state.surgeon_request_generation

    assert twin.set_cam4_mayo_hand_present(True)
    assert twin.state.surgeon_request_instance_id == mayo_instance.instance_id
    assert twin.state.surgeon_request_generation == request_generation


def test_return_unused_preposition_targets_physical_instance_with_duplicate_type() -> None:
    twin = ORDigitalTwin(_demo_spec())
    handed_over = twin.instrument_states["T02#1"]
    prepositioned = twin.instrument_states["T02#2"]
    twin._set_lifecycle(
        handed_over,
        LIFECYCLE_SURGEON_OWNED,
        location_type="surgeon_hand",
        location_id="surgeon_hand",
        confidence=1.0,
    )
    twin._set_lifecycle(
        prepositioned,
        LIFECYCLE_PREPOSITIONED_RIGHT,
        location_type="robot_right_hand",
        location_id="robot_right_hand",
        confidence=1.0,
    )
    twin.normalize_for_publish()

    started = _event(
        "RobotTaskStarted",
        "T02",
        task_id="return:T02#2",
        task_type="return_unused_preposition",
        instrument_instance_id="T02#2",
    )
    started.instance_id = "T02#2"
    twin.apply_event(started)
    returned = _event(
        "UnusedPrepositionReturned",
        "T02",
        instrument_instance_id="T02#2",
        target_lifecycle_stage=LIFECYCLE_MAYO_REUSE,
    )
    returned.instance_id = "T02#2"
    returned.source_location_id = "robot_right_hand"
    returned.source_location_type = "robot_right_hand"
    returned.target_location_id = "mayo_stand"
    returned.target_location_type = "mayo_stand"
    twin.apply_event(returned)

    assert handed_over.lifecycle_stage == LIFECYCLE_SURGEON_OWNED
    assert prepositioned.lifecycle_stage == LIFECYCLE_MAYO_REUSE
    assert prepositioned.location_id == "mayo_stand"
    assert twin.state.right_hand_tool_instance_id == ""


def test_controller_cancel_recovery_to_tray_overrides_mayo_origin() -> None:
    twin = ORDigitalTwin(_demo_spec())
    state = twin.instrument_states["T02#1"]
    twin._set_lifecycle(
        state,
        LIFECYCLE_PREPOSITIONED_RIGHT,
        location_type="robot_right_hand",
        location_id="robot_right_hand",
        confidence=1.0,
    )
    state.preposition_origin_lifecycle_stage = LIFECYCLE_MAYO_REUSE
    state.preposition_origin_location_type = "mayo_stand"
    state.preposition_origin_location_id = "mayo_stand"

    canceled = _event(
        "UnusedPrepositionReturned",
        "T02",
        controller_final_state="canceled",
        controller_reason_code="canceled_recovered_to_tray",
    )
    canceled.instance_id = state.instance_id
    canceled.source_location_type = "robot"
    canceled.source_location_id = "robot"
    canceled.target_location_type = "tray"
    canceled.target_location_id = "tray"
    twin.apply_event(canceled)

    assert state.lifecycle_stage == LIFECYCLE_RETURNED_HOME
    assert state.location_type == "tray"
    assert state.location_id == "tray"
    assert state.preposition_origin_lifecycle_stage == ""


def test_legacy_return_task_prefers_prepositioned_duplicate_instance() -> None:
    twin = ORDigitalTwin(_demo_spec())
    twin._set_lifecycle(
        twin.instrument_states["T02#1"],
        LIFECYCLE_SURGEON_OWNED,
        location_type="surgeon_hand",
        location_id="surgeon_hand",
        confidence=1.0,
    )
    twin._set_lifecycle(
        twin.instrument_states["T02#2"],
        LIFECYCLE_PREPOSITIONED_RIGHT,
        location_type="robot_right_hand",
        location_id="robot_right_hand",
        confidence=1.0,
    )
    twin.normalize_for_publish()

    twin.apply_event(
        _event(
            "RobotTaskStarted",
            "T02",
            task_id="legacy-return",
            task_type="return_unused_preposition",
        )
    )
    twin.apply_event(_event("PredictedToolReturnedToRack", "T02"))

    assert (
        twin.instrument_states["T02#1"].lifecycle_stage
        == LIFECYCLE_SURGEON_OWNED
    )
    assert (
        twin.instrument_states["T02#2"].lifecycle_stage
        == LIFECYCLE_RETURNED_HOME
    )


def test_preposition_is_not_rejected_only_for_phase_expected_list_mismatch() -> None:
    twin = ORDigitalTwin(_demo_spec())
    twin.set_initial_phase("P04")
    expected_types = set(twin.get_expected_instruments())
    state = next(
        candidate
        for candidate in twin.instrument_states.values()
        if candidate.instrument_id not in expected_types
        and candidate.lifecycle_stage == LIFECYCLE_HOME_RACK
    )
    twin.state.execution_state = "running"
    twin.state.running = True
    twin._set_lifecycle(
        state,
        LIFECYCLE_PREPOSITIONED_RIGHT,
        location_type="robot_right_hand",
        location_id="robot_right_hand",
        confidence=1.0,
    )
    twin.normalize_for_publish()

    assert state.instrument_id not in expected_types
    assert state.next_required_transition == ""


def test_conflicting_explicit_request_still_releases_preposition() -> None:
    twin = ORDigitalTwin(_demo_spec())
    twin.set_initial_phase("P03")
    state = next(
        candidate
        for candidate in twin.instrument_states.values()
        if candidate.lifecycle_stage == LIFECYCLE_HOME_RACK
    )
    other_tool = next(
        candidate.instrument_id
        for candidate in twin.instrument_states.values()
        if candidate.instrument_id != state.instrument_id
    )
    twin.state.execution_state = "running"
    twin.state.running = True
    twin._set_lifecycle(
        state,
        LIFECYCLE_PREPOSITIONED_RIGHT,
        location_type="robot_right_hand",
        location_id="robot_right_hand",
        confidence=1.0,
    )
    assert twin.update_explicit_request(other_tool) == other_tool
    twin.normalize_for_publish()

    assert state.next_required_transition == "return_unused_preposition"


def test_finishing_or_completed_alone_does_not_release_preposition() -> None:
    for execution_state in ("finishing", "completed"):
        twin = ORDigitalTwin(_demo_spec())
        state = next(
            candidate
            for candidate in twin.instrument_states.values()
            if candidate.lifecycle_stage == LIFECYCLE_HOME_RACK
        )
        twin._set_lifecycle(
            state,
            LIFECYCLE_PREPOSITIONED_RIGHT,
            location_type="robot_right_hand",
            location_id="robot_right_hand",
            confidence=1.0,
        )
        twin.state.execution_state = execution_state

        assert twin._derive_next_required_transition(state) == ""


def test_right_hand_belief_mismatch_alone_does_not_release_preposition() -> None:
    twin = ORDigitalTwin(_demo_spec())
    state = next(
        candidate
        for candidate in twin.instrument_states.values()
        if candidate.lifecycle_stage == LIFECYCLE_HOME_RACK
    )
    other_tool = next(
        candidate.instrument_id
        for candidate in twin.instrument_states.values()
        if candidate.instrument_id != state.instrument_id
    )
    twin.state.execution_state = "running"
    twin.state.running = True
    twin._set_lifecycle(
        state,
        LIFECYCLE_PREPOSITIONED_RIGHT,
        location_type="robot_right_hand",
        location_id="robot_right_hand",
        confidence=1.0,
    )
    twin.state.right_hand_tool = other_tool

    assert twin._derive_next_required_transition(state) == ""


def test_extend_hand_implicit_intent_does_not_release_preposition() -> None:
    twin = ORDigitalTwin(_demo_spec())
    state = next(
        candidate
        for candidate in twin.instrument_states.values()
        if candidate.lifecycle_stage == LIFECYCLE_HOME_RACK
    )
    other_tool = next(
        candidate.instrument_id
        for candidate in twin.instrument_states.values()
        if candidate.instrument_id != state.instrument_id
    )
    twin.state.execution_state = "running"
    twin.state.running = True
    twin._set_lifecycle(
        state,
        LIFECYCLE_PREPOSITIONED_RIGHT,
        location_type="robot_right_hand",
        location_id="robot_right_hand",
        confidence=1.0,
    )
    twin.state.surgeon_intent = "extend_hand_for_handover"
    twin.state.surgeon_request_tool = other_tool
    twin.state.explicit_request_tool = other_tool
    twin.state.surgeon_request_generation = 1

    assert twin._derive_next_required_transition(state) == ""


def test_explicit_replacement_waits_for_active_tool_action_terminal() -> None:
    twin = ORDigitalTwin(_demo_spec())
    state = next(
        candidate
        for candidate in twin.instrument_states.values()
        if candidate.lifecycle_stage == LIFECYCLE_HOME_RACK
    )
    other_tool = next(
        candidate.instrument_id
        for candidate in twin.instrument_states.values()
        if candidate.instrument_id != state.instrument_id
    )
    twin.state.execution_state = "running"
    twin.state.running = True
    twin._set_lifecycle(
        state,
        LIFECYCLE_PREPOSITIONED_RIGHT,
        location_type="robot_right_hand",
        location_id="robot_right_hand",
        confidence=1.0,
    )
    assert twin.update_explicit_request(other_tool) == other_tool
    twin.state.active_robot_task = ActiveRobotTask(
        task_id="other-tool-action",
        task_type="prepare_tool",
        instrument_id=other_tool,
    )

    twin.normalize_for_publish()
    assert state.next_required_transition == ""
    assert twin.state.surgeon_request_generation > 0

    twin.state.active_robot_task = None
    twin.normalize_for_publish()
    assert state.next_required_transition == "return_unused_preposition"


def test_mayo_reuse_and_recovery_are_instance_scoped() -> None:
    twin = ORDigitalTwin(_demo_spec())
    twin.set_initial_phase("P09")
    first = twin.instrument_states["T02#1"]
    second = twin.instrument_states["T02#2"]
    for state in (first, second):
        twin._set_lifecycle(
            state,
            LIFECYCLE_SURGEON_OWNED,
            location_type="surgeon_hand",
            location_id="surgeon_hand",
            confidence=1.0,
        )
    twin._set_lifecycle(
        first,
        LIFECYCLE_MAYO_REUSE,
        location_type="mayo_reuse_zone",
        location_id="mayo_reuse_zone",
        confidence=1.0,
        placement_evidence="public_visual_observation",
    )
    result = twin.record_mayo_policy_evidence(
        instrument_id=first.instance_id,
        evidence_type="recover",
        confidence=0.9,
        stability_sec=5.0,
        source="test",
        proposal_id="test:T02#1",
        stamp_sec=10.0,
    )

    assert result and result["accepted"] is True
    assert result["instance_id"] == first.instance_id
    assert first.lifecycle_stage == LIFECYCLE_MAYO_REUSE
    assert first.mayo_recovery_confidence == 0.9
    assert second.lifecycle_stage == LIFECYCLE_SURGEON_OWNED
    assert twin.state.active_recovery_tool_instances == []


def test_default_mayo_policy_never_forces_recovery_from_capacity() -> None:
    twin = ORDigitalTwin(_demo_spec())
    twin.state.running = True
    twin.state.execution_state = "running"
    selected = [
        twin.instrument_states["T02#2"],
        twin.instrument_states["T04#1"],
        twin.instrument_states["T07#1"],
    ]
    for state in selected:
        twin._set_lifecycle(
            state,
            LIFECYCLE_MAYO_REUSE,
            location_type="mayo_reuse_zone",
            location_id="mayo_reuse_zone",
            confidence=1.0,
            placement_evidence="public_visual_observation",
        )

    twin.normalize_for_publish()

    assert all(
        state.lifecycle_stage == LIFECYCLE_MAYO_REUSE for state in selected
    )
    assert twin.state.active_recovery_tool_instances == []


def test_phase_transition_requires_two_t02_instances_and_never_regresses() -> None:
    twin = ORDigitalTwin(
        _demo_spec(),
        phase_transition_required_counts={("P03", "P04"): {"T02": 2}},
    )
    now = [100.0]
    twin._monotonic_sec = lambda: now[0]
    twin.set_initial_phase("P03")
    twin._phase_entered_sec = 0.0
    for _ in range(twin.spec.bundle.phase_guard.smoothing_window):
        twin._phase_evidence_history.append(
            {
                "scores": {"P04": 0.95},
                "uncertainty": 0.05,
            }
        )

    twin._set_lifecycle(
        twin.instrument_states["T02#1"],
        LIFECYCLE_SURGEON_OWNED,
        location_type="surgical_field",
        location_id="operative_field",
        confidence=1.0,
    )
    rejected = twin._try_approve_phase_transition("P04")
    assert rejected["accepted"] is False
    assert rejected["reason"] == "required_transition_evidence_incomplete"
    assert twin.state.filtered_phase == "P03"

    twin._set_lifecycle(
        twin.instrument_states["T02#2"],
        LIFECYCLE_SURGEON_OWNED,
        location_type="surgical_field",
        location_id="operative_field",
        confidence=1.0,
    )
    accepted = twin._try_approve_phase_transition("P04")
    assert accepted["accepted"] is True
    assert twin.state.filtered_phase == "P04"

    regressive = FilteredPhase()
    regressive.phase_id = "P02"
    regressive.confidence = 1.0
    regressive.uncertain = False
    regressive.stability = 1.0
    twin.update_phase(regressive)
    assert twin.state.filtered_phase == "P04"


def test_inventory_count_violation_is_fail_closed() -> None:
    twin = ORDigitalTwin(_demo_spec())
    removed = twin.instrument_states.pop("T02#2")
    assert removed.lifecycle_stage == LIFECYCLE_HOME_RACK

    twin.normalize_for_publish()

    assert "duplicate_tool_holder" in twin.state.safety_flags
    assert any(
        event.get("reason") == "instrument_inventory_invariant_failed"
        for event in twin.event_history
    )
