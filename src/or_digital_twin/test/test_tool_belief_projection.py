from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from or_digital_twin.models import (
    LIFECYCLE_MAYO_REUSE,
    LIFECYCLE_PREPOSITIONED_RIGHT,
    LIFECYCLE_SURGEON_OWNED,
)
from or_digital_twin.node import ORDigitalTwinNode
from or_digital_twin.twin import ORDigitalTwin
from procedure_spec import load_bundle


def _twin() -> ORDigitalTwin:
    spec = load_bundle(
        Path(__file__).parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
        / "thyroidectomy_demo"
    )
    return ORDigitalTwin(spec)


def test_committed_belief_projects_only_the_authored_instance() -> None:
    twin = _twin()
    state = twin.instrument_states["T04#1"]
    twin._set_lifecycle(
        state,
        LIFECYCLE_SURGEON_OWNED,
        location_type="surgeon_hand",
        location_id="surgeon_hand",
        confidence=1.0,
    )
    inventory_before = set(twin.instrument_states)

    result = twin.project_committed_tool_belief(
        instrument_id="Bovie surgical cautery",
        instance_id="T04#1",
        committed_location_id="mayo",
        confidence=0.91,
        source_stamp_sec=42.0,
        evidence_sources=("cam4:clear",),
    )

    assert result["accepted"] is True
    assert state.lifecycle_stage == LIFECYCLE_MAYO_REUSE
    assert state.location_id == "mayo_stand"
    assert state.mayo_placement_evidence == "tool_belief_committed_location"
    assert set(twin.instrument_states) == inventory_before


def test_projection_rejects_an_unauthored_instance_without_creating_capacity() -> None:
    twin = _twin()
    inventory_before = set(twin.instrument_states)

    result = twin.project_committed_tool_belief(
        instrument_id="T02",
        instance_id="T02#2",
        committed_location_id="mayo",
        confidence=0.98,
    )

    assert result["accepted"] is False
    assert result["reducer_reason"] == "tool_belief_instance_outside_fixed_inventory"
    assert set(twin.instrument_states) == inventory_before
    assert "T02#2" not in twin.instrument_states


def test_persistent_committed_belief_can_reverse_robot_custody_receipt() -> None:
    twin = _twin()
    state = twin.instrument_states["T04#1"]
    twin._set_lifecycle(
        state,
        LIFECYCLE_PREPOSITIONED_RIGHT,
        location_type="robot_right_hand",
        location_id="robot_right_hand",
        confidence=1.0,
    )

    result = twin.project_committed_tool_belief(
        instrument_id="T04",
        instance_id="T04#1",
        committed_location_id="mayo",
        confidence=0.97,
    )

    assert result["accepted"] is True
    assert result["reducer_reason"] == "committed_belief_projected"
    assert state.lifecycle_stage == LIFECYCLE_MAYO_REUSE


def test_robot_commit_projects_prepared_custody_without_direct_skill_mutation() -> None:
    twin = _twin()
    state = twin.instrument_states["T04#1"]

    result = twin.project_committed_tool_belief(
        instrument_id="T04",
        instance_id="T04#1",
        committed_location_id="robot",
        confidence=0.93,
        evidence_sources=("skill:completed",),
    )

    assert result["accepted"] is True
    assert state.lifecycle_stage == LIFECYCLE_PREPOSITIONED_RIGHT
    assert state.location_id == "robot_right_hand"


def test_finishing_ignores_ambiguous_robot_commit_for_non_prepositioned_tool() -> None:
    twin = _twin()
    state = twin.instrument_states["T08#1"]
    twin._set_lifecycle(
        state,
        LIFECYCLE_MAYO_REUSE,
        location_type="mayo_stand",
        location_id="mayo_stand",
        confidence=1.0,
    )
    twin.state.execution_state = "finishing"

    result = twin.project_committed_tool_belief(
        instrument_id="T08",
        instance_id="T08#1",
        committed_location_id="robot",
        confidence=0.93,
        evidence_sources=("camera:cam_4", "skill:completed"),
    )

    assert result["accepted"] is False
    assert result["reducer_reason"] == "finishing_robot_location_ambiguous"
    assert state.lifecycle_stage == LIFECYCLE_MAYO_REUSE


def test_voice_request_remains_retryable_until_target_location_commits() -> None:
    twin = _twin()
    assert twin.update_resolved_voice_tool_handover("T04") == "T04"

    source_result = twin.project_committed_tool_belief(
        instrument_id="T04",
        instance_id="T04#1",
        committed_location_id="tray",
        confidence=0.97,
        evidence_sources=("camera:cam_3", "skill:completed"),
    )

    assert source_result["accepted"] is True
    assert twin.state.surgeon_request_tool == "T04"

    target_result = twin.project_committed_tool_belief(
        instrument_id="T04",
        instance_id="T04#1",
        committed_location_id="robot",
        confidence=0.93,
        evidence_sources=("skill:completed",),
    )

    assert target_result["accepted"] is True
    assert twin.state.surgeon_request_tool == ""


def test_node_projects_only_matching_run_commits() -> None:
    twin = _twin()
    twin.state.procedure_run_id = "run-current"
    state = twin.instrument_states["T04#1"]
    twin._set_lifecycle(
        state,
        LIFECYCLE_SURGEON_OWNED,
        location_type="surgeon_hand",
        location_id="surgeon_hand",
        confidence=1.0,
    )
    node = ORDigitalTwinNode.__new__(ORDigitalTwinNode)
    node._twin = twin
    node._stamp = lambda: SimpleNamespace(sec=50, nanosec=0)
    published: list[tuple[str, dict]] = []
    node._publish_reducer_decision_event = lambda **kwargs: published.append(
        ("reducer", kwargs)
    )
    node._publish_event = lambda event_type, **kwargs: published.append(
        (event_type, kwargs)
    )
    node._publish_world_state = lambda: published.append(("world", {}))
    belief = SimpleNamespace(
        track_id="T04#1",
        instrument_id="T04",
        instance_id="T04#1",
        committed_location_id="mayo",
        committed_location_probability=0.93,
        existence_probability=1.0,
        evidence_sources=["cam4:clear"],
    )
    message = SimpleNamespace(
        observation_only=True,
        procedure_id=twin.state.procedure_id,
        procedure_run_id="run-current",
        header=SimpleNamespace(stamp=SimpleNamespace(sec=51, nanosec=0)),
        tools=[belief],
    )

    node._on_tool_beliefs(message)

    assert state.lifecycle_stage == LIFECYCLE_MAYO_REUSE
    assert [event_type for event_type, _ in published] == [
        "reducer",
        "ToolBeliefProjectionAccepted",
        "world",
    ]

    state.lifecycle_stage = LIFECYCLE_SURGEON_OWNED
    message.procedure_run_id = "run-stale"
    node._on_tool_beliefs(message)
    assert state.lifecycle_stage == LIFECYCLE_SURGEON_OWNED
