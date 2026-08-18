from types import SimpleNamespace

from or_digital_twin.node import ORDigitalTwinNode


def _node(*, running: bool, execution_state: str) -> ORDigitalTwinNode:
    node = ORDigitalTwinNode.__new__(ORDigitalTwinNode)
    node._twin = SimpleNamespace(
        state=SimpleNamespace(
            running=running,
            execution_state=execution_state,
        )
    )
    node._last_lifecycle_control_signature = None
    return node


def test_duplicate_stop_is_mutation_idempotent_but_still_acknowledged() -> None:
    node = _node(running=False, execution_state="halted")
    node._last_lifecycle_control_signature = ("stop", "")
    acknowledgments: list[bool] = []
    node._publish_world_state = lambda: acknowledgments.append(True)
    node._advance_visual_runtime_epoch = lambda: (_ for _ in ()).throw(
        AssertionError("duplicate stop advanced visual epoch")
    )

    node._on_control(SimpleNamespace(data="stop"))

    assert acknowledgments == [True]


def test_running_start_heartbeat_preserves_state_and_acknowledges() -> None:
    node = _node(running=True, execution_state="running")
    acknowledgments: list[bool] = []
    node._publish_world_state = lambda: acknowledgments.append(True)
    node._advance_visual_runtime_epoch = lambda: (_ for _ in ()).throw(
        AssertionError("running start heartbeat advanced visual epoch")
    )

    node._on_control(SimpleNamespace(data="start"))

    assert node._last_lifecycle_control_signature == ("start", "")
    assert acknowledgments == [True]


def test_start_reset_start_edges_mutate_once_and_each_reset_is_applied() -> None:
    state = SimpleNamespace(running=False, execution_state="idle")
    reset_spec_calls: list[bool] = []
    reset_runtime_calls: list[bool] = []
    node = ORDigitalTwinNode.__new__(ORDigitalTwinNode)

    def set_execution_state(running: bool, execution_state: str) -> None:
        state.running = running
        state.execution_state = execution_state

    node._twin = SimpleNamespace(
        state=state,
        spec=object(),
        reset_spec=lambda *_args, **_kwargs: reset_spec_calls.append(True),
        reset_runtime=lambda: reset_runtime_calls.append(True),
        set_initial_phase=lambda _phase: None,
        set_execution_state=set_execution_state,
    )
    node._last_lifecycle_control_signature = None
    node._pending_bed_robot_arm_group_requests = {}
    node._clear_vlm_implicit_request_state = lambda: None
    node._clear_tool_histories = lambda: None
    node._reset_bed_robot_controller_freshness = lambda: None
    node._stamp_all_bed_robot_arm_groups = lambda: None
    node._stamp = lambda: SimpleNamespace(sec=0, nanosec=0)
    node._stamp_sec = lambda _stamp: 0.0
    node._publish_world_state = lambda: None
    visual_epoch_advances: list[bool] = []
    node._advance_visual_runtime_epoch = lambda: visual_epoch_advances.append(True)

    node._on_control(SimpleNamespace(data="start"))
    node._on_control(SimpleNamespace(data="start"))
    node._on_control(SimpleNamespace(data="reset"))
    node._on_control(SimpleNamespace(data="reset"))
    node._on_control(SimpleNamespace(data="start"))

    assert len(reset_spec_calls) == 2
    assert len(reset_runtime_calls) == 2
    assert len(visual_epoch_advances) == 4
    assert state.running is True
    assert state.execution_state == "running"
