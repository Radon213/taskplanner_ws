from __future__ import annotations

import json
from pathlib import Path

from or_digital_twin.models import (
    LIFECYCLE_HOME_RACK,
    LIFECYCLE_MAYO_RECOVERY,
    LIFECYCLE_MAYO_REUSE,
    LIFECYCLE_PREPOSITIONED_RIGHT,
    LIFECYCLE_SURGEON_OWNED,
)
from or_digital_twin.twin import ORDigitalTwin
from procedure_spec import load_bundle
from surgical_msgs.msg import FilteredPhase, TwinEvent


def _demo_spec():
    return load_bundle(
        Path(__file__).parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
        / "thyroidectomy_demo"
    )


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
    twin.apply_event(_event("RobotTaskCompleted", tool_id))
    return instance_id


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


def test_one_more_is_a_distinct_generation_and_handover_instance() -> None:
    twin = ORDigitalTwin(_demo_spec())

    assert twin.update_explicit_request("Adson") == "T02"
    first_generation = twin.state.surgeon_request_generation
    assert twin.update_explicit_request("Adson 하나 더") == "T02"
    queued = list(twin.state.surgeon_request_queue)
    assert [cue.instance_id for cue in queued] == ["T02#1", "T02#2"]
    assert queued[1].generation > first_generation

    assert _handover_active_request(twin) == "T02#1"
    assert twin.state.surgeon_request_instance_id == "T02#2"
    assert _handover_active_request(twin) == "T02#2"
    assert twin.instrument_states["T02#1"].lifecycle_stage == (
        LIFECYCLE_SURGEON_OWNED
    )
    assert twin.instrument_states["T02#2"].lifecycle_stage == (
        LIFECYCLE_SURGEON_OWNED
    )
    assert twin.state.surgeon_request_tool == ""


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


def test_mayo_reuse_and_recovery_are_instance_scoped() -> None:
    twin = ORDigitalTwin(_demo_spec())
    twin.set_initial_phase("P09")
    first = twin.instrument_states["T05#1"]
    second = twin.instrument_states["T05#2"]
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
        proposal_id="test:T05#1",
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
        twin.instrument_states["T01#1"],
        twin.instrument_states["T02#1"],
        twin.instrument_states["T03#1"],
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


def test_phase_transition_requires_two_t05_instances_and_never_regresses() -> None:
    twin = ORDigitalTwin(
        _demo_spec(),
        phase_transition_required_counts={("P03", "P04"): {"T05": 2}},
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
        twin.instrument_states["T05#1"],
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
        twin.instrument_states["T05#2"],
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
