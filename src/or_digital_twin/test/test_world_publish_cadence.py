import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from builtin_interfaces.msg import Time
from std_msgs.msg import String
from surgical_msgs.msg import BedRobotArmGroupState

from or_digital_twin.node import (
    ORDigitalTwinNode,
    WORLD_STATE_IDLE_CHECKPOINT_SEC,
    WORLD_STATE_MAINTENANCE_PERIOD_SEC,
)
from or_digital_twin.twin import ORDigitalTwin
from procedure_spec import load_bundle


def _cadence_node(*, running: bool, execution_state: str) -> ORDigitalTwinNode:
    node = ORDigitalTwinNode.__new__(ORDigitalTwinNode)
    node._twin = SimpleNamespace(
        state=SimpleNamespace(
            running=running,
            execution_state=execution_state,
        )
    )
    node._last_world_emit_signature = ("same",)
    node._last_world_emit_monotonic = 10.0
    return node


def test_inactive_unchanged_output_uses_two_second_checkpoint() -> None:
    node = _cadence_node(running=False, execution_state="halted")

    assert not node._world_state_emit_due(
        now_monotonic=11.999,
        signature=("same",),
    )
    assert node._world_state_emit_due(
        now_monotonic=12.0,
        signature=("same",),
    )
    assert WORLD_STATE_IDLE_CHECKPOINT_SEC < 3.0
    assert WORLD_STATE_IDLE_CHECKPOINT_SEC < 4.0


@pytest.mark.parametrize(
    ("running", "execution_state"),
    [
        (True, "running"),
        (True, "paused"),
        (False, "paused"),
        (False, "starting"),
        (False, "unknown"),
    ],
)
def test_active_paused_and_unknown_states_keep_fail_safe_cadence(
    running: bool,
    execution_state: str,
) -> None:
    node = _cadence_node(
        running=running,
        execution_state=execution_state,
    )

    assert node._world_state_emit_due(
        now_monotonic=10.1,
        signature=("same",),
    )
    assert WORLD_STATE_MAINTENANCE_PERIOD_SEC == 0.5


def test_dirty_maintenance_signature_emits_before_checkpoint() -> None:
    node = _cadence_node(running=False, execution_state="completed")

    assert node._world_state_emit_due(
        now_monotonic=10.1,
        signature=("changed",),
    )


def test_timer_runs_maintenance_once_without_reentering_wrapper() -> None:
    node = _cadence_node(running=False, execution_state="idle")
    calls: list[str] = []
    node._run_time_based_maintenance = lambda: calls.append("maintenance")
    node._world_maintenance_signature = lambda: ("changed",)
    node._monotonic_sec = lambda: 10.1
    node._emit_world_state = lambda: calls.append("emit")

    node._on_world_state_timer()

    assert calls == ["maintenance", "emit"]


def test_direct_semantic_publish_is_immediate() -> None:
    node = _cadence_node(running=False, execution_state="idle")
    calls: list[str] = []
    node._run_time_based_maintenance = lambda: calls.append("maintenance")
    node._emit_world_state = lambda: calls.append("emit")

    node._publish_world_state()

    assert calls == ["maintenance", "emit"]


def test_perception_gate_publishes_health_timeout_edge_immediately() -> None:
    node = _cadence_node(running=False, execution_state="halted")
    node._twin.state.safety_flags = ["vlm_unhealthy"]
    calls: list[str] = []
    node._publish_world_state_if_dirty = lambda: calls.append("publish_if_dirty")

    assert node._perception_gate_active()
    assert calls == ["publish_if_dirty"]


def test_unchanged_perception_health_heartbeat_does_not_force_full_emit() -> None:
    node = _cadence_node(running=False, execution_state="halted")
    node._perception_health_seen = True
    node._perception_enabled = True
    cleared: list[bool] = []
    node._twin.clear_object_detection_evidence = lambda: cleared.append(True)
    node._run_time_based_maintenance = lambda: None
    node._world_maintenance_signature = lambda: ("same",)
    emitted: list[bool] = []
    node._emit_world_state = lambda: emitted.append(True)

    unchanged = String()
    unchanged.data = json.dumps(
        {"schema": "taskplanner.rfdetr_health.v1", "enabled": True}
    )
    node._on_perception_health(unchanged)

    disabled = String()
    disabled.data = json.dumps(
        {"schema": "taskplanner.rfdetr_health.v1", "enabled": False}
    )
    node._on_perception_health(disabled)
    node._on_perception_health(disabled)

    assert emitted == [True]
    assert cleared == [True, True]


def test_emit_gate_commits_only_after_full_public_bundle() -> None:
    source = inspect.getsource(ORDigitalTwinNode._emit_world_state)

    commit_index = source.index("self._last_world_emit_signature =")
    assert source.index("self._twin.normalize_for_publish()") < source.index(
        "world = WorldState()"
    )
    assert source.index("self._world_pub.publish(world)") < commit_index
    assert source.index("self._simulation_state_pub.publish(simulation)") < commit_index
    assert source.index("self._publish_perception_scene(world)") < commit_index
    assert source.index("self._publish_vlm_context(world)") < commit_index


def test_maintenance_does_not_apply_untracked_publish_normalization() -> None:
    source = inspect.getsource(ORDigitalTwinNode._run_time_based_maintenance)

    assert "normalize_for_publish" not in source


def _signature_node() -> ORDigitalTwinNode:
    retraction = SimpleNamespace(
        connected=True,
        state="standby",
        arm_id="arm_1",
        end_effector_profile="retractor",
        error_code="ok",
    )
    prediction = SimpleNamespace(
        rank=1,
        instrument_id="T01",
        confidence=0.8,
        stability_sec=1.2,
    )
    state = SimpleNamespace(
        bed_robot_arm_groups={"retraction": retraction},
        filtered_phase="P03",
        phase_confidence=0.9,
        phase_uncertain=False,
        phase_stability=2.0,
        safety_flags=[],
        predicted_tool="T01",
        predicted_tool_confidence=0.8,
        predicted_tool_stability_sec=1.2,
        ranked_tool_predictions=[prediction],
        implicit_request_visible=True,
        implicit_request_tool="T02",
        implicit_request_hand_pose="open_hand",
        implicit_request_confidence=0.7,
        implicit_request_stability_sec=0.5,
    )
    node = ORDigitalTwinNode.__new__(ORDigitalTwinNode)
    node._twin = SimpleNamespace(state=state)
    return node


@pytest.mark.parametrize(
    "mutate",
    [
        lambda node: setattr(
            node._twin.state.bed_robot_arm_groups["retraction"],
            "connected",
            False,
        ),
        lambda node: setattr(
            node._twin.state.bed_robot_arm_groups["retraction"],
            "state",
            "stale",
        ),
        lambda node: setattr(
            node._twin.state.bed_robot_arm_groups["retraction"],
            "arm_id",
            "arm_2",
        ),
        lambda node: setattr(
            node._twin.state.bed_robot_arm_groups["retraction"],
            "end_effector_profile",
            "alternate",
        ),
        lambda node: setattr(
            node._twin.state.bed_robot_arm_groups["retraction"],
            "error_code",
            "stale",
        ),
        lambda node: setattr(node._twin.state, "filtered_phase", "P04"),
        lambda node: setattr(node._twin.state, "phase_confidence", 0.1),
        lambda node: setattr(node._twin.state, "phase_uncertain", True),
        lambda node: setattr(node._twin.state, "phase_stability", 3.0),
        lambda node: setattr(node._twin.state, "safety_flags", ["vlm_unhealthy"]),
        lambda node: setattr(node._twin.state, "predicted_tool", "T02"),
        lambda node: setattr(node._twin.state, "predicted_tool_confidence", 0.1),
        lambda node: setattr(
            node._twin.state,
            "predicted_tool_stability_sec",
            9.0,
        ),
        lambda node: setattr(
            node._twin.state.ranked_tool_predictions[0],
            "rank",
            2,
        ),
        lambda node: setattr(
            node._twin.state.ranked_tool_predictions[0],
            "instrument_id",
            "T03",
        ),
        lambda node: setattr(
            node._twin.state.ranked_tool_predictions[0],
            "confidence",
            0.2,
        ),
        lambda node: setattr(
            node._twin.state.ranked_tool_predictions[0],
            "stability_sec",
            4.0,
        ),
        lambda node: setattr(node._twin.state, "implicit_request_visible", False),
        lambda node: setattr(node._twin.state, "implicit_request_tool", "T03"),
        lambda node: setattr(
            node._twin.state,
            "implicit_request_hand_pose",
            "pinch",
        ),
        lambda node: setattr(
            node._twin.state,
            "implicit_request_confidence",
            0.1,
        ),
        lambda node: setattr(
            node._twin.state,
            "implicit_request_stability_sec",
            4.0,
        ),
    ],
)
def test_maintenance_signature_tracks_every_gated_public_field(mutate) -> None:
    node = _signature_node()
    baseline = node._world_maintenance_signature()

    mutate(node)
    assert node._world_maintenance_signature() != baseline


class _Publisher:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.messages: list[object] = []

    def publish(self, message) -> None:
        self.messages.append(message)
        if self.fail:
            raise RuntimeError("injected publisher failure")


def test_failed_public_bundle_does_not_commit_emit_checkpoint() -> None:
    spec_dir = (
        Path(__file__).parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
        / "thyroidectomy_demo"
    )
    node = ORDigitalTwinNode.__new__(ORDigitalTwinNode)
    node._twin = ORDigitalTwin(load_bundle(spec_dir))
    node._stamp = Time
    node._monotonic_sec = lambda: 999.0
    node._bundle_metadata_cache = []
    node._tool_pub = _Publisher()
    node._world_pub = _Publisher()
    node._simulation_state_pub = _Publisher(fail=True)
    node._publish_perception_scene = lambda _world: None
    node._publish_vlm_context = lambda _world: None
    node._bed_robot_arm_group_state_message = (
        lambda _payload, _stamp: BedRobotArmGroupState()
    )
    node._last_world_emit_signature = ("last-success",)
    node._last_world_emit_monotonic = 123.0

    with pytest.raises(RuntimeError, match="injected publisher failure"):
        node._emit_world_state()

    assert node._last_world_emit_signature == ("last-success",)
    assert node._last_world_emit_monotonic == 123.0
