from __future__ import annotations

import inspect
import time
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
from or_digital_twin.models import (
    ActiveRobotTask,
    LIFECYCLE_PREPOSITIONED_RIGHT,
)
from or_digital_twin.twin import ORDigitalTwin
from procedure_spec import load_bundle
from surgical_msgs.msg import TwinEvent


def _node() -> tuple[ORDigitalTwinNode, SimpleNamespace, list[dict]]:
    node = ORDigitalTwinNode.__new__(ORDigitalTwinNode)
    state = SimpleNamespace(
        implicit_request_visible=False,
        implicit_request_tool="",
        implicit_request_hand_pose="",
        implicit_request_confidence=0.0,
        implicit_request_stability_sec=0.0,
        implicit_request_generation=0,
        cam4_mayo_hand_present=True,
        running=True,
        execution_state="running",
        active_robot_task=None,
        right_hand_tool="",
    )
    node._twin = SimpleNamespace(state=state)
    decisions: list[dict] = []
    node._publish_reducer_decision_event = lambda **kwargs: decisions.append(kwargs)
    node._publish_event = lambda *_args, **_kwargs: None
    node._publish_world_state_if_dirty = lambda: None
    return node, state, decisions


def _gesture_frame(
    *,
    sec: int,
    hands: list[object],
    frame_id: str = "cam4_color_optical_frame",
    model_name: str = "MediaPipe Gesture Recognizer",
    model_version: str = "0.10.18",
    model_sha256: str = "gesture-sha",
) -> SimpleNamespace:
    return SimpleNamespace(
        header=SimpleNamespace(
            stamp=SimpleNamespace(sec=sec, nanosec=0),
            frame_id=frame_id,
        ),
        model_name=model_name,
        model_version=model_version,
        model_asset_sha256=model_sha256,
        hands=hands,
    )


def _configure_mayo_hand_pins(node: ORDigitalTwinNode) -> None:
    node._hand_perception_pins = SimpleNamespace(
        source_frame_id="cam4_color_optical_frame",
        gesture_model_name="MediaPipe Gesture Recognizer",
        gesture_model_version="0.10.18",
        gesture_model_sha256="gesture-sha",
    )
    node._cam4_mayo_hand_present = True
    node._cam4_mayo_hand_source_stamp_ns = None
    node._cam4_mayo_hand_received_monotonic = 0.0
    node._hand_observation_timeout_sec = 0.4
    node._hand_source_max_age_sec = 1.0e12
    node._hand_source_future_tolerance_sec = 1.0e12
    node._stamp = lambda: None
    node._stamp_sec = lambda _stamp: 10.0
    node._hand_health_timeout_sec = 2.0
    node._hand_health_received_monotonic = time.monotonic()
    node._hand_health_payload = {
        "schema": "pnu.hand_keypoint_health.v1",
        "rgb_ready": False,
        "hand_inference_ready": False,
        "gesture_rgb_ready": True,
        "gesture_model_ready": True,
        "gesture_inference_ready": True,
        "gesture_model_version": "0.10.18",
        "gesture_model_asset_sha256": "gesture-sha",
    }


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


def _direct_delivery_task() -> ActiveRobotTask:
    return ActiveRobotTask(
        task_id="direct-delivery-1",
        task_type="direct_handover",
        instrument_id="T01",
        target_anchor_id="surgeon_receive_zone",
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


def test_voice_only_repeat_adson_cannot_reuse_open_palm_signal() -> None:
    node, state, decisions = _node()
    state.right_hand_tool = "T02"
    node._repeat_handover_requires_explicit_voice = (
        lambda _tool_id, _phase_id="": True
    )

    node._apply_hand_handover_update(_positive_update(), _positive_evidence())

    assert state.implicit_request_visible is False
    assert decisions[-1]["accepted"] is False
    assert decisions[-1]["affected_tool"] == "T02"
    assert decisions[-1]["reason"] == (
        "automatic_repeat_handover_requires_explicit_voice"
    )
    assert decisions[-1]["detail"]["required_request_source"] == "voice"


def test_cam4_mayo_occupancy_ignores_pose_and_clears_only_on_new_pinned_empty_frame() -> None:
    node, state, _decisions = _node()
    _configure_mayo_hand_pins(node)

    assert node._update_cam4_mayo_hand_presence(
        _gesture_frame(sec=1, hands=[])
    )
    assert state.cam4_mayo_hand_present is False

    # Gesture class, facing, handedness and posture are deliberately not read.
    arbitrary_pose = SimpleNamespace(
        category_name="Closed_Fist",
        facing_label="PALM_DOWN",
        handedness_label="Left",
    )
    assert node._update_cam4_mayo_hand_presence(
        _gesture_frame(sec=2, hands=[arbitrary_pose])
    )
    assert state.cam4_mayo_hand_present is True

    # Older or unpinned frames cannot clear a latched positive observation.
    assert not node._update_cam4_mayo_hand_presence(
        _gesture_frame(sec=1, hands=[])
    )
    assert not node._update_cam4_mayo_hand_presence(
        _gesture_frame(sec=3, hands=[], model_sha256="untrusted")
    )
    assert state.cam4_mayo_hand_present is True

    assert node._update_cam4_mayo_hand_presence(
        _gesture_frame(sec=4, hands=[])
    )
    assert state.cam4_mayo_hand_present is False

    # Even an older pinned positive is consumed conservatively; source rewind
    # may block Mayo motion but must never hide a detected hand.
    assert node._update_cam4_mayo_hand_presence(
        _gesture_frame(sec=2, hands=[arbitrary_pose])
    )
    assert state.cam4_mayo_hand_present is True


def test_cam4_mayo_unhealthy_empty_frame_cannot_clear_occupancy() -> None:
    node, state, _decisions = _node()
    _configure_mayo_hand_pins(node)
    node._hand_health_payload["gesture_inference_ready"] = False

    assert not node._update_cam4_mayo_hand_presence(
        _gesture_frame(sec=1, hands=[])
    )
    assert state.cam4_mayo_hand_present is True

    node._hand_health_payload["gesture_inference_ready"] = True
    assert node._update_cam4_mayo_hand_presence(
        _gesture_frame(sec=2, hands=[])
    )
    assert state.cam4_mayo_hand_present is False


def test_cam4_mayo_stale_or_future_empty_frame_cannot_clear_occupancy() -> None:
    node, state, _decisions = _node()
    _configure_mayo_hand_pins(node)
    source_now = {"sec": 10.0}
    node._stamp_sec = lambda _stamp: source_now["sec"]
    node._hand_source_max_age_sec = 0.5
    node._hand_source_future_tolerance_sec = 0.5

    # A rejected far-future empty must not poison the order watermark.
    assert not node._update_cam4_mayo_hand_presence(
        _gesture_frame(sec=20, hands=[])
    )
    assert state.cam4_mayo_hand_present is True

    assert node._update_cam4_mayo_hand_presence(
        _gesture_frame(sec=10, hands=[])
    )
    assert state.cam4_mayo_hand_present is False
    admitted_receipt = node._cam4_mayo_hand_received_monotonic

    # A newer but stale replay neither renews the free lease nor leaves the
    # workspace clear.
    source_now["sec"] = 12.0
    assert node._update_cam4_mayo_hand_presence(
        _gesture_frame(sec=11, hands=[])
    )
    assert state.cam4_mayo_hand_present is True
    assert node._cam4_mayo_hand_received_monotonic == admitted_receipt

    assert node._update_cam4_mayo_hand_presence(
        _gesture_frame(sec=12, hands=[])
    )
    assert state.cam4_mayo_hand_present is False


def test_cam4_mayo_empty_frame_silence_relatches_occupancy(monkeypatch) -> None:
    node, state, _decisions = _node()
    _configure_mayo_hand_pins(node)
    monotonic = {"now": 10.0}
    monkeypatch.setattr(
        "or_digital_twin.node.time.monotonic",
        lambda: monotonic["now"],
    )
    node._hand_health_received_monotonic = 10.0

    assert node._update_cam4_mayo_hand_presence(
        _gesture_frame(sec=1, hands=[])
    )
    assert state.cam4_mayo_hand_present is False

    monotonic["now"] = 10.399
    assert not node._expire_cam4_mayo_hand_free_lease()
    assert state.cam4_mayo_hand_present is False

    monotonic["now"] = 10.401
    assert node._expire_cam4_mayo_hand_free_lease()
    assert state.cam4_mayo_hand_present is True


def test_cam4_mayo_occupancy_update_precedes_direct_delivery_signal_suppression() -> None:
    node, state, _decisions = _node()
    _configure_mayo_hand_pins(node)
    state.cam4_mayo_hand_present = False
    node._cam4_mayo_hand_present = False
    state.active_robot_task = _direct_delivery_task()
    calls: list[str] = []
    message = _gesture_frame(sec=5, hands=[SimpleNamespace()])
    node._hand_handover_joiner = SimpleNamespace(
        add_gesture=lambda _message: (message, object(), object()),
        clear=lambda: calls.append("clear"),
    )
    node._hand_handover_gate = SimpleNamespace(
        inhibit_until_release=lambda: calls.append("inhibit")
    )
    node._publish_world_state_if_dirty = lambda: calls.append("publish")

    node._on_hand_gesture(message)

    assert state.cam4_mayo_hand_present is True
    assert calls == ["clear", "inhibit", "publish", "publish"]


def test_raw_tool_observations_cannot_mutate_twin_location_state() -> None:
    node = ORDigitalTwinNode.__new__(ORDigitalTwinNode)
    reconciled: list[tuple[str, bool]] = []
    node._reconcile_tool_observation = lambda _message, *, source, cam4_mayo_channel=False: reconciled.append(
        (source, cam4_mayo_channel)
    )
    typed_message = SimpleNamespace(
        location_type="mayo_stand",
        source="cam4_typed_mayo_observation",
    )

    node._on_observation(typed_message)
    node._on_cam4_mayo_observation(typed_message)
    node._on_cam4_mayo_observation(
        SimpleNamespace(location_type="mayo_stand", source="untrusted")
    )

    assert reconciled == []


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


def test_active_delivery_suspends_and_ignores_receiving_hand_pair() -> None:
    node, state, _decisions = _node()
    state.active_robot_task = _direct_delivery_task()
    state.implicit_request_visible = True
    state.implicit_request_tool = ""
    state.implicit_request_hand_pose = "open_receive"
    state.implicit_request_confidence = 0.91
    state.implicit_request_stability_sec = 0.3
    state.implicit_request_generation = 7
    calls: list[str] = []
    node._hand_handover_joiner = SimpleNamespace(
        clear=lambda: calls.append("clear")
    )
    node._hand_handover_gate = SimpleNamespace(
        inhibit_until_release=lambda: calls.append("inhibit")
    )
    node._publish_world_state_if_dirty = lambda: calls.append("publish")

    # Plain objects prove the guard returns before timestamp/classification.
    node._on_hand_observation_triplet(object(), object(), object())

    assert calls == ["clear", "inhibit", "publish"]
    assert state.implicit_request_visible is False
    assert state.implicit_request_tool == ""
    assert state.implicit_request_hand_pose == ""
    assert state.implicit_request_confidence == 0.0
    assert state.implicit_request_stability_sec == 0.0
    assert state.implicit_request_generation == 7


def test_robot_task_started_direct_delivery_suspends_existing_cue() -> None:
    node, state, _decisions = _node()
    state.implicit_request_visible = True
    state.implicit_request_hand_pose = "open_receive"
    state.implicit_request_confidence = 0.91
    state.implicit_request_stability_sec = 0.3
    state.implicit_request_generation = 4
    calls: list[str] = []

    def apply_event(_message: TwinEvent) -> None:
        state.active_robot_task = _direct_delivery_task()

    node._twin = SimpleNamespace(
        state=state,
        apply_event=apply_event,
        request_queue_summary=lambda: {},
    )
    node._hand_handover_joiner = SimpleNamespace(
        clear=lambda: calls.append("clear")
    )
    node._hand_handover_gate = SimpleNamespace(
        inhibit_until_release=lambda: calls.append("inhibit")
    )
    node._augment_event_detail = lambda _event_type, detail, **_kwargs: detail
    node._event_pub = SimpleNamespace(publish=lambda _message: None)
    node._simulation_event_pub = SimpleNamespace(publish=lambda _message: None)
    node._record_important_event = lambda _message: None
    node._publish_world_state = lambda: calls.append("publish")
    message = TwinEvent()
    message.event_type = "RobotTaskStarted"
    message.instrument_id = "T01"
    message.source_location_id = "robot_right_hand"
    message.target_location_id = "surgeon_receive_zone"
    message.mode = "direct_handover"
    message.detail_json = '{"task_type":"direct_handover"}'

    node._on_skill_event(message)

    assert calls == ["clear", "inhibit", "publish"]
    assert state.implicit_request_visible is False
    assert state.implicit_request_hand_pose == ""
    assert state.implicit_request_confidence == 0.0
    assert state.implicit_request_stability_sec == 0.0
    assert state.implicit_request_generation == 4


def test_non_delivery_task_does_not_claim_hand_semantics() -> None:
    node, state, _decisions = _node()
    state.active_robot_task = ActiveRobotTask(
        task_id="return-1",
        task_type="return_unused_preposition",
        target_anchor_id="mayo_stand",
    )

    assert node._active_robot_task_is_direct_delivery() is False


def test_vlm_result_path_rejects_legacy_hand_fields_without_consuming_them() -> None:
    assert not hasattr(ORDigitalTwinNode, "_handle_vlm_implicit_request")
    source = inspect.getsource(ORDigitalTwinNode._on_vlm_result)
    assert "vlm_hand_fields_forbidden" in source
    assert "_handle_vlm_tool_prediction" in source
    assert "implicit_request" not in source


def test_non_running_publication_keeps_hand_signal_observable_but_not_executable() -> None:
    source = inspect.getsource(ORDigitalTwinNode._emit_world_state)
    lifecycle_source = inspect.getsource(ORDigitalTwinNode._on_control)
    observation_source = inspect.getsource(
        ORDigitalTwinNode._on_hand_observation_triplet
    )
    direct_delivery_source = inspect.getsource(
        ORDigitalTwin.direct_hand_preposition_ready
    )

    assert "_suspend_hand_handover_state" not in source
    assert 'not bool(state.running) or str(state.execution_state) != "running"' not in observation_source
    # An in-flight direct delivery still suppresses its own receiving hand;
    # that is separate from the scenario-idle observer path.
    assert "_active_robot_task_is_direct_delivery" in observation_source
    assert "_suspend_hand_handover_state" in observation_source
    assert 'command == "pause"' in lifecycle_source
    assert "_suspend_hand_handover_state()" in lifecycle_source
    assert "_reset_hand_handover_state()" not in lifecycle_source[
        lifecycle_source.index('elif command == "pause"') :
        lifecycle_source.index('elif command == "resume"')
    ]
    assert "not bool(state.running)" in direct_delivery_source
    assert 'state.execution_state != "running"' in direct_delivery_source


def test_observation_silence_lease_is_500ms_with_a_100ms_watchdog() -> None:
    constructor = inspect.getsource(ORDigitalTwinNode.__init__)
    gate_factory = inspect.getsource(
        ORDigitalTwinNode._hand_handover_gate_config_from_parameters
    )
    gate_builder = inspect.getsource(ORDigitalTwinNode._build_hand_handover_gate)
    watchdog = inspect.getsource(ORDigitalTwinNode._on_hand_handover_watchdog)

    assert HAND_HANDOVER_WATCHDOG_PERIOD_SEC == pytest.approx(0.1)
    assert '"hand_handover_observation_timeout_sec", 0.500' in constructor
    assert '"hand_handover_release_confirm_sec", 0.180' in constructor
    assert '"hand_handover_soft_unknown_grace_sec", 0.180' in constructor
    assert '"hand_handover_observation_timeout_sec"' in gate_factory
    assert "ContinuousHandHandoverGate(**values)" in gate_builder
    assert "HAND_HANDOVER_WATCHDOG_PERIOD_SEC" in constructor
    assert "self._expire_hand_handover_evidence()" in watchdog
    assert "self._world_maintenance_signature() != before" in watchdog
    assert "self._emit_world_state()" in watchdog


def test_every_start_assigns_a_new_opaque_procedure_run_id_without_resetting_the_world() -> None:
    lifecycle_source = inspect.getsource(ORDigitalTwinNode._on_control)
    start_branch = lifecycle_source[
        lifecycle_source.index('if command in {"start", "start_runtime"}') :
        lifecycle_source.index('elif command == "pause"')
    ]
    emit_source = inspect.getsource(ORDigitalTwinNode._emit_world_state)

    assert "self._twin.reset_spec" not in start_branch
    assert "self._twin.state.procedure_run_id = uuid.uuid4().hex" in start_branch
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

    # Older controller feedback reports the same owned right-hand slot as the
    # generic robot anchor. It remains eligible only because ownership is
    # still explicitly the humanoid's right hand.
    candidate.location_type = "robot"
    candidate.location_id = "robot"
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
