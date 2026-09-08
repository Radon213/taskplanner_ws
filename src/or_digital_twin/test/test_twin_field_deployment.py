from __future__ import annotations

from pathlib import Path

from or_digital_twin.models import (
    LIFECYCLE_HOME_RACK,
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


def test_demo_starts_with_exact_four_authored_rack_tools() -> None:
    twin = ORDigitalTwin(_demo_spec())

    assert set(twin.instrument_states) == {
        "T02#1",
        "T04#1",
        "T07#1",
        "T08#1",
    }
    assert [
        (
            state.instance_id,
            state.lifecycle_stage,
            state.location_type,
            state.location_id,
        )
        for state in twin.instrument_states.values()
    ] == [
        ("T02#1", LIFECYCLE_HOME_RACK, "tray_slot", "main_tray_slot_1"),
        ("T04#1", LIFECYCLE_HOME_RACK, "tray_slot", "main_tray_slot_2"),
        ("T07#1", LIFECYCLE_HOME_RACK, "tray_slot", "main_tray_slot_3"),
        ("T08#1", LIFECYCLE_HOME_RACK, "tray_slot", "main_tray_slot_4"),
    ]


def test_paused_scenario_swap_preserves_observed_tool_state_without_reset() -> None:
    twin = ORDigitalTwin(_demo_spec())
    observed = twin.instrument_states["T02#1"]
    twin._set_lifecycle(
        observed,
        LIFECYCLE_SURGEON_OWNED,
        location_type="surgeon_hand",
        location_id="surgeon_hand",
        confidence=0.97,
    )
    twin.state.procedure_run_id = "paused-run"
    twin.state.running = True
    twin.state.execution_state = "paused"

    twin.swap_spec_preserving_paused_world(_demo_spec())

    preserved = twin.instrument_states["T02#1"]
    untouched = twin.instrument_states["T04#1"]
    assert twin.state.running is True
    assert twin.state.execution_state == "paused"
    assert twin.state.procedure_run_id == "paused-run"
    assert preserved.lifecycle_stage == LIFECYCLE_SURGEON_OWNED
    assert preserved.location_type == "surgeon_hand"
    assert preserved.location_id == "surgeon_hand"
    assert untouched.location_id == "main_tray_slot_2"


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
    for instance_id in ("T02#1", "T04#1"):
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
