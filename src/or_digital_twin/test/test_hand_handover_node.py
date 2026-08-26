from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from or_digital_twin.hand_handover_gate import (
    FrameDisposition,
    HandFrameEvidence,
    HandGateUpdate,
)
from or_digital_twin.node import (
    HAND_HANDOVER_WATCHDOG_PERIOD_SEC,
    ORDigitalTwinNode,
)
from or_digital_twin.models import LIFECYCLE_PREPOSITIONED_RIGHT
from or_digital_twin.twin import ORDigitalTwin
from procedure_spec import load_bundle


def _node() -> tuple[ORDigitalTwinNode, SimpleNamespace, list[dict]]:
    node = ORDigitalTwinNode.__new__(ORDigitalTwinNode)
    state = SimpleNamespace(
        implicit_request_visible=False,
        implicit_request_tool="",
        implicit_request_hand_pose="",
        implicit_request_confidence=0.0,
        implicit_request_stability_sec=0.0,
        implicit_request_generation=0,
    )
    node._twin = SimpleNamespace(state=state)
    decisions: list[dict] = []
    node._publish_reducer_decision_event = lambda **kwargs: decisions.append(kwargs)
    node._publish_event = lambda *_args, **_kwargs: None
    node._publish_world_state_if_dirty = lambda: None
    return node, state, decisions


def _positive_update(*, rising: bool = True, generation: int = 1) -> HandGateUpdate:
    return HandGateUpdate(
        accepted_sample=True,
        active=True,
        rising_edge=rising,
        generation=generation,
        stability_sec=0.3,
        confidence=0.91,
        reason="right_open_palm_palm_up",
        disposition=FrameDisposition.POSITIVE,
    )


def _positive_evidence() -> HandFrameEvidence:
    return HandFrameEvidence(
        source_stamp_sec=100.3,
        disposition=FrameDisposition.POSITIVE,
        reason="right_open_palm_palm_up",
        confidence=0.91,
        hand_index=0,
        gesture_score=0.95,
        handedness_score=0.99,
        palm_up_score=0.82,
    )


def test_direct_hand_signal_exposes_tool_agnostic_evidence() -> None:
    node, state, decisions = _node()

    node._apply_hand_handover_update(_positive_update(), _positive_evidence())

    assert state.implicit_request_visible is True
    assert state.implicit_request_tool == ""
    assert state.implicit_request_hand_pose == "open_receive"
    assert state.implicit_request_confidence == 0.91
    assert state.implicit_request_stability_sec == 0.3
    assert state.implicit_request_generation == 1
    assert decisions[-1]["input_type"] == "hand_handover_signal"
    assert decisions[-1]["reason"] == "right_open_palm_palm_up_300ms"
    assert decisions[-1]["detail"]["request_created"] is False
    assert decisions[-1]["detail"]["tool_resolved"] is False


def test_held_episode_does_not_emit_a_second_acceptance() -> None:
    node, state, decisions = _node()
    node._apply_hand_handover_update(_positive_update(), _positive_evidence())
    node._apply_hand_handover_update(
        _positive_update(rising=False), _positive_evidence()
    )

    assert state.implicit_request_visible is True
    assert state.implicit_request_generation == 1
    assert len(decisions) == 1


def test_negative_update_withdraws_visibility_without_rewriting_generation() -> None:
    node, state, _decisions = _node()
    node._apply_hand_handover_update(_positive_update(generation=4), _positive_evidence())
    negative = HandGateUpdate(
        accepted_sample=True,
        active=False,
        rising_edge=False,
        generation=4,
        stability_sec=0.0,
        confidence=0.0,
        reason="observed_non_request_pose",
        disposition=FrameDisposition.RELEASE,
    )

    node._apply_hand_handover_update(negative, _positive_evidence())

    assert state.implicit_request_visible is False
    assert state.implicit_request_tool == ""
    assert state.implicit_request_hand_pose == ""
    assert state.implicit_request_generation == 4


def test_vlm_result_path_rejects_legacy_hand_fields_without_consuming_them() -> None:
    assert not hasattr(ORDigitalTwinNode, "_handle_vlm_implicit_request")
    source = inspect.getsource(ORDigitalTwinNode._on_vlm_result)
    assert "vlm_hand_fields_forbidden" in source
    assert "_handle_vlm_tool_prediction" in source
    assert "implicit_request" not in source


def test_non_running_publication_suspends_hand_signal_until_fresh_release() -> None:
    source = inspect.getsource(ORDigitalTwinNode._emit_world_state)
    lifecycle_source = inspect.getsource(ORDigitalTwinNode._on_control)
    observation_source = inspect.getsource(
        ORDigitalTwinNode._on_hand_observation_pair
    )

    assert "_suspend_hand_handover_state" in source
    assert 'execution_state) != "running"' in source
    assert 'command == "pause"' in lifecycle_source
    assert "_suspend_hand_handover_state()" in lifecycle_source
    assert "_reset_hand_handover_state()" not in lifecycle_source[
        lifecycle_source.index('elif command == "pause"') :
        lifecycle_source.index('elif command == "resume"')
    ]
    assert "_suspend_hand_handover_state()" in observation_source


def test_observation_silence_lease_is_400ms_with_a_100ms_watchdog() -> None:
    constructor = inspect.getsource(ORDigitalTwinNode.__init__)
    watchdog = inspect.getsource(ORDigitalTwinNode._on_hand_handover_watchdog)

    assert HAND_HANDOVER_WATCHDOG_PERIOD_SEC == pytest.approx(0.1)
    assert '"hand_handover_observation_timeout_sec", 0.400' in constructor
    assert "max_receipt_silence_sec=self._hand_observation_timeout_sec" in constructor
    assert "max_receipt_silence_sec=self._hand_health_timeout_sec" not in constructor
    assert "HAND_HANDOVER_WATCHDOG_PERIOD_SEC" in constructor
    assert "self._expire_hand_handover_evidence()" in watchdog
    assert "self._world_maintenance_signature() != before" in watchdog
    assert "self._emit_world_state()" in watchdog


def test_every_start_assigns_a_new_opaque_procedure_run_id() -> None:
    lifecycle_source = inspect.getsource(ORDigitalTwinNode._on_control)
    start_branch = lifecycle_source[
        lifecycle_source.index('if command in {"start", "start_runtime"}') :
        lifecycle_source.index('elif command == "pause"')
    ]
    emit_source = inspect.getsource(ORDigitalTwinNode._emit_world_state)

    assert "self._twin.reset_spec" in start_branch
    assert "self._twin.state.procedure_run_id = uuid.uuid4().hex" in start_branch
    assert start_branch.index("self._twin.reset_spec") < start_branch.index(
        "uuid.uuid4().hex"
    )
    assert "world.procedure_run_id = self._twin.state.procedure_run_id" in emit_source


def test_direct_preposition_can_ignore_only_vlm_health() -> None:
    spec_dir = (
        Path(__file__).parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
        / "thyroidectomy"
    )
    twin = ORDigitalTwin(load_bundle(spec_dir))
    candidate = twin.instrument_states["T01#1"]
    twin._set_lifecycle(candidate, LIFECYCLE_PREPOSITIONED_RIGHT)
    twin.state.running = True
    twin.state.execution_state = "running"
    twin.state.prepositioned_tool = candidate.instrument_id
    twin.state.prepositioned_tool_instance_id = candidate.instance_id
    twin.state.right_hand_tool = candidate.instrument_id
    twin.state.right_hand_tool_instance_id = candidate.instance_id
    twin.state.implicit_request_visible = True
    twin.state.implicit_request_tool = ""
    twin.state.implicit_request_hand_pose = "open_receive"
    twin.state.implicit_request_confidence = 0.9
    twin.state.implicit_request_stability_sec = 0.3
    twin.state.safety_flags = ["vlm_unhealthy"]

    assert twin.direct_hand_preposition_ready() is True
    assert twin.handover_allowed() is True

    twin.state.safety_flags.append("dropped_tool_requires_human")
    assert twin.handover_allowed() is False

    twin.state.safety_flags = ["vlm_unhealthy"]
    twin.state.execution_state = "finishing"
    assert twin.direct_hand_preposition_ready() is False
    assert twin.handover_allowed() is False


@pytest.mark.parametrize(
    "mutation",
    [
        "tool_type",
        "lifecycle",
        "owner",
        "location_id",
        "location_type",
        "prepositioned_instance",
        "right_hand_instance",
    ],
)
def test_direct_preposition_fails_closed_on_every_identity_mutation(
    mutation: str,
) -> None:
    spec_dir = (
        Path(__file__).parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
        / "thyroidectomy"
    )
    twin = ORDigitalTwin(load_bundle(spec_dir))
    candidate = twin.instrument_states["T01#1"]
    twin._set_lifecycle(candidate, LIFECYCLE_PREPOSITIONED_RIGHT)
    twin.state.running = True
    twin.state.execution_state = "running"
    twin.state.prepositioned_tool = candidate.instrument_id
    twin.state.prepositioned_tool_instance_id = candidate.instance_id
    twin.state.right_hand_tool = candidate.instrument_id
    twin.state.right_hand_tool_instance_id = candidate.instance_id
    twin.state.implicit_request_visible = True
    twin.state.implicit_request_tool = ""
    twin.state.implicit_request_hand_pose = "open_receive"
    twin.state.implicit_request_confidence = 0.9
    twin.state.implicit_request_stability_sec = 0.3
    twin.state.safety_flags = ["vlm_unhealthy"]

    if mutation == "tool_type":
        twin.state.prepositioned_tool = "not-the-candidate-type"
    elif mutation == "lifecycle":
        candidate.lifecycle_stage = "home_rack"
    elif mutation == "owner":
        candidate.owner = "none"
    elif mutation == "location_id":
        candidate.location_id = "mayo_stand"
    elif mutation == "location_type":
        candidate.location_type = "mayo_stand"
    elif mutation == "prepositioned_instance":
        twin.state.prepositioned_tool_instance_id = "missing-instance"
    elif mutation == "right_hand_instance":
        twin.state.right_hand_tool_instance_id = "different-instance"
    else:  # pragma: no cover - parametrization is intentionally exhaustive.
        raise AssertionError(f"unknown mutation: {mutation}")

    assert twin.direct_hand_preposition_ready() is False
    assert twin.handover_allowed() is False
