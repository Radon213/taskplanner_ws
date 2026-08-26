from __future__ import annotations

import json
import subprocess
from types import MethodType, SimpleNamespace

from builtin_interfaces.msg import Time
import pytest
from surgical_msgs.msg import ExecutionTrace, SkillCommand, TwinEvent

from bringup.manual_probe import (
    HAND_FRAME_ID,
    HAND_RELEASE_DURATION_SEC,
    HAND_RELEASE_SAMPLE_COUNT,
    ManualProbeHarness,
    PROBE_CAM3_TOOL_OBSERVATIONS_TOPIC,
    PROBE_CAM4_TOOL_OBSERVATIONS_TOPIC,
    VIRTUAL_TOOL_HANDOVER_ENDPOINT,
    _build_hand_observation_pair,
    _build_synthetic_rfdetr_readiness_frame,
    _build_synthetic_hand_health,
    _probe_runtime_command,
    _positive_duration,
    _probe_domain_nodes,
    _validate_virtual_route_payload,
    main,
    parse_args,
)


class _Publisher:
    def __init__(self) -> None:
        self.messages = []

    def publish(self, message) -> None:
        self.messages.append(message)


class _Clock:
    def __init__(self) -> None:
        self._index = 0

    def now(self):
        self._index += 1
        stamp = Time(sec=100, nanosec=self._index * 1_000_000)
        return SimpleNamespace(to_msg=lambda: stamp)


def _injection_harness():
    clock = _Clock()
    intervals: list[float] = []
    harness = SimpleNamespace(
        _hand_gesture_pub=_Publisher(),
        _hand_facing_pub=_Publisher(),
        _hand_health_pub=_Publisher(),
        get_clock=lambda: clock,
        _spin_hand_interval=intervals.append,
        _hand_intervals=intervals,
    )
    for name in (
        "publish_synthetic_hand_health",
        "_emit_hand_observation_sequence",
        "emit_hand_handover_probe",
        "emit_hand_handover_release",
        "emit_hand_handover_stability_probe",
    ):
        setattr(
            harness,
            name,
            MethodType(getattr(ManualProbeHarness, name), harness),
        )
    return harness


def _stamp_key(message) -> tuple[int, int, str]:
    return (
        int(message.header.stamp.sec),
        int(message.header.stamp.nanosec),
        str(message.header.frame_id),
    )


def test_request_and_release_pairs_are_exact_stamp_and_valid_classifications() -> None:
    request_gesture, request_facing = _build_hand_observation_pair(
        Time(sec=10, nanosec=20),
        request_pose=True,
    )
    release_gesture, release_facing = _build_hand_observation_pair(
        Time(sec=11, nanosec=30),
        request_pose=False,
    )

    assert _stamp_key(request_gesture) == _stamp_key(request_facing)
    assert request_gesture.header.frame_id == HAND_FRAME_ID
    assert request_gesture.hands[0].category_name == "Open_Palm"
    assert request_facing.hands[0].facing_label == "PALM_UP"

    assert _stamp_key(release_gesture) == _stamp_key(release_facing)
    assert release_gesture.hands[0].has_classification is True
    assert release_gesture.hands[0].category_name == "Closed_Fist"
    assert release_facing.hands[0].has_facing is True
    assert release_facing.hands[0].facing_label == "PALM_UP"


def test_repeated_probe_inserts_default_valid_release_between_episodes() -> None:
    harness = _injection_harness()

    harness.emit_hand_handover_stability_probe(
        iterations=3,
        allow_topic_injection=True,
        inject_synthetic_health=True,
    )

    gestures = harness._hand_gesture_pub.messages
    facings = harness._hand_facing_pub.messages
    assert len(gestures) == len(facings) == 3 * 8 + 2 * HAND_RELEASE_SAMPLE_COUNT
    assert len(harness._hand_health_pub.messages) == 5
    labels = [message.hands[0].category_name for message in gestures]
    assert labels == (
        ["Open_Palm"] * 8
        + ["Closed_Fist"] * 10
        + ["Open_Palm"] * 8
        + ["Closed_Fist"] * 10
        + ["Open_Palm"] * 8
    )
    keys = [_stamp_key(message) for message in gestures]
    assert len(keys) == len(set(keys))
    assert keys == [_stamp_key(message) for message in facings]
    assert harness._hand_intervals[:7] == pytest.approx([0.08] * 7)
    assert harness._hand_intervals[7:16] == pytest.approx(
        [HAND_RELEASE_DURATION_SEC / 9.0] * 9
    )


def test_all_hand_publish_apis_are_fail_closed_without_explicit_opt_in() -> None:
    harness = _injection_harness()

    with pytest.raises(RuntimeError, match="injection is disabled"):
        harness.emit_hand_handover_probe()
    with pytest.raises(RuntimeError, match="injection is disabled"):
        harness.emit_hand_handover_release()
    with pytest.raises(RuntimeError, match="injection is disabled"):
        harness.emit_hand_handover_stability_probe(iterations=2)

    assert harness._hand_gesture_pub.messages == []
    assert harness._hand_facing_pub.messages == []
    assert harness._hand_health_pub.messages == []


def test_synthetic_health_is_pinned_but_remains_opt_in() -> None:
    payload = json.loads(_build_synthetic_hand_health().data)

    assert payload["schema"] == "pnu.hand_keypoint_health.v1"
    assert payload["palm_facing_mapping_verified"] is True
    assert payload["depth_registration_backend_active"] == "cuda_cabi_v1"

    with pytest.raises(ValueError, match="must be > 0"):
        _positive_duration("duration", float("nan"))


def test_synthetic_rfdetr_readiness_is_typed_fresh_empty_evidence() -> None:
    stamp = Time(sec=100, nanosec=22)
    cam3 = _build_synthetic_rfdetr_readiness_frame(
        stamp,
        view="cam_3",
        sequence=9,
    )
    cam4 = _build_synthetic_rfdetr_readiness_frame(
        stamp,
        view="cam_4",
        sequence=9,
    )

    assert cam3.schema_version == "pnu.tool_observation_2d.v1"
    assert cam3.view == "cam_3"
    assert cam4.view == "cam_4"
    assert cam3.sequence == cam4.sequence == 9
    assert cam3.instances == cam4.instances == []
    assert cam3.model_version == cam4.model_version
    with pytest.raises(ValueError, match="unsupported"):
        _build_synthetic_rfdetr_readiness_frame(
            stamp,
            view="cam_2",
            sequence=10,
        )


def test_probe_domain_discovery_uses_no_daemon_and_returns_nodes(monkeypatch) -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout="/simulation_manager\n/tree_executor\n/tree_executor\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert _probe_domain_nodes() == ["/simulation_manager", "/tree_executor"]
    assert calls[0][0] == ["ros2", "node", "list", "--no-daemon"]


def test_virtual_route_validation_rejects_external_or_unready_routes() -> None:
    valid = {
        "schema": "taskplanner.execution_route_state.v1",
        "selected_source": "virtual",
        "tool_handover_endpoint": VIRTUAL_TOOL_HANDOVER_ENDPOINT,
        "action_server_ready": True,
    }
    assert _validate_virtual_route_payload(valid) == valid

    with pytest.raises(RuntimeError, match="non-virtual"):
        _validate_virtual_route_payload({**valid, "selected_source": "external"})
    with pytest.raises(RuntimeError, match="unexpected tool endpoint"):
        _validate_virtual_route_payload(
            {**valid, "tool_handover_endpoint": "/surgery/tool_handover"}
        )
    with pytest.raises(RuntimeError, match="not ready"):
        _validate_virtual_route_payload({**valid, "action_server_ready": False})


def test_virtual_result_observer_correlates_one_deterministic_episode() -> None:
    run_id = "a" * 32
    command = SkillCommand()
    command.command_id = f"skill-hand-{run_id}-3-direct_handover"
    command.procedure_run_id = run_id
    command.implicit_request_generation = 3
    command.mode = "implicit_request"
    command.action = "direct_handover"
    command.instrument_id = "T02"
    command.instrument_instance_id = "T02#1"

    trace = ExecutionTrace()
    trace.command_id = command.command_id
    trace.endpoint_source = "virtual"
    trace.endpoint = VIRTUAL_TOOL_HANDOVER_ENDPOINT
    trace.stage = "completed"
    trace.terminal = True
    trace.reason_code = "completed"

    event = TwinEvent()
    event.event_type = "ToolHandoverCompleted"
    event.instrument_id = "T02"

    def wait_until(predicate, _timeout, description):
        if not predicate():
            raise RuntimeError(f"not observed: {description}")

    harness = SimpleNamespace(
        _skill_command_log=[command],
        _execution_trace_log=[trace],
        _event_log=[event],
        assert_virtual_execution_route=lambda **_kwargs: {},
        wait_until=wait_until,
    )
    harness.wait_for_virtual_hand_episode_result = MethodType(
        ManualProbeHarness.wait_for_virtual_hand_episode_result,
        harness,
    )

    observation = harness.wait_for_virtual_hand_episode_result(
        expected_tool_id="T02",
        command_log_start=0,
        trace_log_start=0,
        event_log_start=0,
    )

    assert observation.command_id == command.command_id
    assert observation.generation == 3
    assert observation.instrument_instance_id == "T02#1"
    assert observation.terminal_reason_code == "completed"


def test_probe_launch_is_pinned_to_virtual_endpoints() -> None:
    args = parse_args(
        ["--allow-hand-topic-injection", "--inject-synthetic-hand-health"]
    )
    command = _probe_runtime_command(args, "/tmp/spec")

    assert "execution_backend:=mock" in command
    assert "execution_contract:=direct" in command
    assert "robot_endpoint_source:=virtual" in command
    assert "retraction_endpoint_source:=virtual" in command
    assert "enable_runtime_route_control:=false" in command
    assert "require_integration_preflight:=true" in command
    assert "preflight_require_rfdetr_tool_observations:=true" in command
    assert (
        f"cam3_tool_observations_topic:={PROBE_CAM3_TOOL_OBSERVATIONS_TOPIC}"
        in command
    )
    assert (
        f"cam4_tool_observations_topic:={PROBE_CAM4_TOOL_OBSERVATIONS_TOPIC}"
        in command
    )
    assert not any("external" in item for item in command)


def test_cli_defaults_are_isolated_and_refuse_topic_injection(capsys) -> None:
    args = parse_args([])
    assert args.allow_hand_topic_injection is False
    assert args.inject_synthetic_hand_health is False
    assert args.inject_synthetic_rfdetr_readiness is False
    assert args.hand_iterations == 1
    assert args.hand_only is False
    assert args.hand_release_duration_sec == pytest.approx(HAND_RELEASE_DURATION_SEC)
    assert args.hand_release_samples == HAND_RELEASE_SAMPLE_COUNT
    assert args.ros_domain_id == 223

    assert main([]) == 2
    assert "refused" in capsys.readouterr().err
