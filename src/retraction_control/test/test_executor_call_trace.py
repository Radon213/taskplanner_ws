import threading
from pathlib import Path
from types import SimpleNamespace

from retraction_control.adapters import ForceTorqueSample, SingleOwnerGuard
from retraction_control.adapters.clock import FakeClock
from retraction_control.adapters.fake import (
    CallTrace,
    FakeAft200Adapter,
    FakeIndyDcp3Adapter,
)
from retraction_control.command_executor import (
    CommandExecutor,
    ExecutionStatus,
    ExecutorState,
)
from retraction_control.profile_loader import load_profile
from retraction_control.teaching_session import TeachingSessionRepository


CONFIG_ROOT = Path(__file__).resolve().parents[1] / "config"


def _request(
    command_id: str,
    command: int,
    *,
    target_side: int = 0,
    distance_m: float = 0.0,
):
    return SimpleNamespace(
        command_id=command_id,
        command=command,
        target_side=target_side,
        distance_m=distance_m,
    )


def _executor(tmp_path):
    clock = FakeClock(
        monotonic_value_ns=100,
        wall_value_ns=1_000_000,
        auto_step_ns=1,
    )
    trace = CallTrace(clock)
    robot = FakeIndyDcp3Adapter(
        trace=trace,
        joint_positions={"arm_1": (0.1, 0.2), "arm_2": (0.3, 0.4)},
    )
    sensor = FakeAft200Adapter(
        trace=trace,
        samples={
            "fake_left": ForceTorqueSample(
                100, "fake_left", (1.0, 0.0, 0.0), (0.0, 0.0, 0.0)
            ),
            "fake_right": ForceTorqueSample(
                100, "fake_right", (2.0, 0.0, 0.0), (0.0, 0.0, 0.0)
            ),
        },
    )
    executor = CommandExecutor(
        robot=robot,
        force_sensor=sensor,
        profile=load_profile(CONFIG_ROOT / "fake.yaml", require_approved=True),
        sessions=TeachingSessionRepository(tmp_path / "sessions"),
        robot_id="robot-test",
        controller_id="controller-test",
        source_revision="revision-test",
        clock=clock,
        owner_guard=SingleOwnerGuard(tmp_path / "hardware.lock"),
    )
    return executor, robot, sensor, trace


def _assert_success(outcome, state: ExecutorState):
    assert outcome.status is ExecutionStatus.SUCCEEDED
    assert outcome.success
    assert outcome.executor_state is state


def test_all_six_commands_produce_deterministic_confirmed_call_trace(tmp_path):
    executor, _robot, _sensor, trace = _executor(tmp_path)
    executor.start()
    assert executor.state is ExecutorState.IDLE
    trace.clear()

    started = executor.execute(_request("teach-start", 1))
    _assert_success(started, ExecutorState.DIRECT_TEACHING)
    assert trace.method_names == (
        "set_friction_compensation",
        "set_custom_gain",
        "set_direct_teaching",
        "is_direct_teaching_enabled",
        "begin_recording",
        "read_joint_state",
        "latest_sample",
        "read_joint_state",
        "latest_sample",
    )
    assert started.session_id == "teach-teach-start"

    trace.clear()
    finished = executor.execute(_request("teach-finish", 2))
    _assert_success(finished, ExecutorState.TAUGHT_READY)
    assert trace.method_names == (
        "read_joint_state",
        "latest_sample",
        "read_joint_state",
        "latest_sample",
        "set_direct_teaching",
        "is_direct_teaching_enabled",
        "set_friction_compensation",
        "end_recording",
    )
    assert finished.session_id == started.session_id

    trace.clear()
    retraction = executor.execute(_request("retract-start", 3))
    _assert_success(retraction, ExecutorState.RETRACTING)
    assert trace.method_names == (
        "latest_sample",
        "latest_sample",
        "set_custom_gain",
        "move_joint_positions",
        "move_joint_positions",
        "controller_state",
    )
    assert retraction.affected_arm_id == "arm_1,arm_2"

    trace.clear()
    adjusted = executor.execute(
        _request("adjust-left", 4, target_side=1, distance_m=0.050)
    )
    _assert_success(adjusted, ExecutorState.RETRACTING)
    assert trace.method_names == ("latest_sample", "jog_tcp", "controller_state")
    jog_call = trace.records[1]
    assert jog_call.args == ("arm_1",)
    assert dict(jog_call.kwargs)["distance_mm"] == 50.0
    assert adjusted.details["distance_mm"] == 50.0
    assert adjusted.affected_arm_id == "arm_1"
    assert adjusted.target_side == 1

    trace.clear()
    changed = executor.execute(_request("tool-change", 5))
    _assert_success(changed, ExecutorState.RETRACTING)
    assert trace.method_names == (
        "move_joint_positions",
        "move_joint_positions",
        "controller_state",
    )
    assert changed.affected_arm_id == "arm_1,arm_2"

    trace.clear()
    stopped = executor.execute(_request("retract-stop", 6))
    _assert_success(stopped, ExecutorState.TAUGHT_READY)
    assert trace.method_names == (
        "stop_motion",
        "hold_position",
        "set_custom_gain",
        "controller_state",
    )

    executor.shutdown()


def test_executor_preflight_has_no_adapter_calls_and_stop_bypasses_busy(tmp_path):
    executor, _robot, _sensor, trace = _executor(tmp_path)
    not_started = executor.check_admission(_request("teach", 1))
    assert not not_started.accepted
    assert not_started.code == "executor_not_started"
    assert trace.records == ()

    executor.start()
    trace.clear()
    executor._state = ExecutorState.RETRACTING
    executor._execute_lock.acquire()
    try:
        busy_adjust = executor.check_admission(
            _request("adjust", 4, target_side=1, distance_m=0.001)
        )
        urgent_stop = executor.check_admission(_request("stop", 6))
    finally:
        executor._execute_lock.release()

    assert not busy_adjust.accepted
    assert busy_adjust.code == "executor_busy"
    assert urgent_stop.accepted

    # START_RETRACTION has not yet earned the RETRACTING physical state while
    # its first motion call is in flight, but STOP must still be admissible.
    executor._state = ExecutorState.TAUGHT_READY
    executor._active_operation = "start_retraction"
    executor._execute_lock.acquire()
    try:
        stop_during_start = executor.check_admission(_request("stop-start", 6))
    finally:
        executor._execute_lock.release()
        executor._active_operation = ""
    assert stop_during_start.accepted
    assert trace.records == ()
    executor.shutdown()


def test_shutdown_cleanup_order_is_fixed_before_hardware_authority_release(tmp_path):
    executor, _robot, _sensor, trace = _executor(tmp_path)
    executor.start()
    trace.clear()

    outcome = executor.shutdown()

    assert outcome.success
    assert outcome.executor_state is ExecutorState.SHUTDOWN
    assert trace.method_names == (
        "stop_motion",
        "end_recording",
        "set_direct_teaching",
        "set_custom_gain",
        "set_friction_compensation",
        "stop",
        "close",
        "close",
    )
    assert executor.owner_guard is not None
    assert not executor.owner_guard.acquired


def test_cooperative_cancel_keeps_queued_stop_executable_after_cleanup(tmp_path):
    executor, _robot, _sensor, trace = _executor(tmp_path)
    executor.start()
    executor._state = ExecutorState.RETRACTING
    trace.clear()
    stop_event = threading.Event()
    stop_event.set()

    canceled = executor.execute(
        _request("adjust-cancel", 4, target_side=1, distance_m=0.001),
        stop_event,
    )

    assert canceled.status is ExecutionStatus.CANCELED
    assert canceled.executor_state is ExecutorState.TAUGHT_READY
    assert executor.check_admission(_request("queued-stop", 6)).accepted

    stopped = executor.execute(_request("queued-stop", 6))
    assert stopped.success
    assert stopped.executor_state is ExecutorState.TAUGHT_READY
    assert trace.method_names == (
        "stop_motion",
        "set_custom_gain",
        "stop_motion",
        "hold_position",
        "set_custom_gain",
        "controller_state",
    )
    executor.shutdown()
