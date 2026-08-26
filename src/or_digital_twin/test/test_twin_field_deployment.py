from __future__ import annotations

from pathlib import Path

from or_digital_twin.models import (
    LIFECYCLE_SURGEON_OWNED,
)
from or_digital_twin.twin import ORDigitalTwin
from procedure_spec import load_bundle
from surgical_msgs.msg import PhaseEvidence, ToolObservation


def _demo_spec():
    return load_bundle(
        Path(__file__).parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
        / "thyroidectomy_demo"
    )


def _mayo_observation(instance_id: str, stamp_sec: int) -> ToolObservation:
    observation = ToolObservation()
    observation.stamp.sec = stamp_sec
    observation.instrument_id = instance_id
    observation.location_type = "mayo_reuse_zone"
    observation.location_id = "mayo_reuse_zone"
    observation.confidence = 0.99
    observation.visible = True
    return observation


def test_demo_starts_with_both_allis_instances_in_active_surgeon_use() -> None:
    twin = ORDigitalTwin(_demo_spec())

    allis_states = [
        twin.instrument_states["T03#1"],
        twin.instrument_states["T03#2"],
    ]
    assert all(
        state.lifecycle_stage == LIFECYCLE_SURGEON_OWNED
        and state.location_type == "surgical_field"
        and state.location_id == "field_region_procedure"
        and state.owner == "surgeon"
        and state.status == "in_use"
        and state.contaminated
        for state in allis_states
    )
    assert not any(
        state.instance_id.startswith("T03#")
        and state.lifecycle_stage != LIFECYCLE_SURGEON_OWNED
        for state in twin.instrument_states.values()
    )


def test_demo_omits_bed_retractors_from_instances_and_rack_slots() -> None:
    twin = ORDigitalTwin(_demo_spec())
    controller_owned_retractors = {"T05", "T11"}

    assert controller_owned_retractors.isdisjoint(
        twin.spec.list_instrument_ids()
    )
    assert all(
        state.instrument_id not in controller_owned_retractors
        for state in twin.instrument_states.values()
    )
    assert all(
        twin.spec.get_initial_location(tool_id) is None
        and twin.spec.get_initial_location_type(tool_id) is None
        for tool_id in controller_owned_retractors
    )


def test_demo_phase_entry_does_not_relocate_rack_tools_for_controller_retraction() -> None:
    twin = ORDigitalTwin(_demo_spec())
    hand_tool = twin.instrument_states["T02#1"]
    twin._set_lifecycle(
        hand_tool,
        LIFECYCLE_SURGEON_OWNED,
        location_type="surgeon_hand",
        location_id="surgeon_hand",
        confidence=1.0,
    )

    twin.set_initial_phase("P04")

    assert hand_tool.location_type == "surgeon_hand"
    assert not any(
        event["event_type"] == "ToolFieldDeploymentInferred"
        for event in twin.event_history
    )


def test_demo_retractor_names_do_not_consume_handover_capacity() -> None:
    twin = ORDigitalTwin(_demo_spec())
    twin.set_initial_phase("P04")
    for instance_id in ("T02#1", "T03#1"):
        twin._set_lifecycle(
            twin.instrument_states[instance_id],
            LIFECYCLE_SURGEON_OWNED,
            location_type="surgeon_hand",
            location_id="surgeon_hand",
            confidence=1.0,
        )
    twin.normalize_for_publish()

    for request_text in (
        "Army navy retractor please",
        "thyroid retractor please",
    ):
        assert twin.update_explicit_request(request_text) == ""
        assert twin.request_queue_summary()["queue_length"] == 0

    assert len(twin._surgeon_owned_hand_states()) == 2
    assert "surgeon_owned_overloaded" not in twin.state.safety_flags


def test_next_phase_controller_retraction_does_not_require_rack_handover() -> None:
    twin = ORDigitalTwin(_demo_spec())
    now = [100.0]
    twin._monotonic_sec = lambda: now[0]
    twin.set_initial_phase("P03")
    now[0] = 106.0

    evidence = PhaseEvidence()
    evidence.source = "real_vlm:test"
    evidence.phase_ids = ["P04", "P03"]
    evidence.phase_confidences = [0.95, 0.05]
    evidence.uncertainty = 0.05
    for _ in range(twin.spec.bundle.phase_guard.smoothing_window + 1):
        twin.apply_phase_evidence(evidence)
        now[0] += 1.0

    assert twin.state.filtered_phase == "P04"
    assert not any(
        event["event_type"] == "PhaseTransitionRejected"
        and event["reason"] == "phase_field_deployment_not_observed"
        for event in twin.event_history
    )


def test_surgical_field_tool_does_not_jump_to_mayo_without_return_context() -> None:
    twin = ORDigitalTwin(_demo_spec())
    twin.set_initial_phase("P04")
    state = twin.instrument_states["T02#1"]
    twin._set_lifecycle(
        state,
        LIFECYCLE_SURGEON_OWNED,
        location_type="surgical_field",
        location_id=twin._field_anchor_id(),
        confidence=1.0,
    )

    rejected = twin.reconcile_observation(
        _mayo_observation(state.instance_id, 10),
        source="test_cam4",
    )

    assert rejected is not None
    assert rejected["accepted"] is False
    assert rejected["reducer_reason"] == (
        "field_deployed_tool_requires_explicit_return_context"
    )
    assert state.lifecycle_stage == LIFECYCLE_SURGEON_OWNED
    assert state.location_type == "surgical_field"

    twin._open_recovery_transaction(
        state.instance_id,
        "explicit_public_return_context",
    )
    accepted = twin.reconcile_observation(
        _mayo_observation(state.instance_id, 11),
        source="test_cam4",
    )

    assert accepted is not None
    assert accepted["accepted"] is True
    assert state.location_type == "mayo_stand"
