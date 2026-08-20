from pathlib import Path
from types import SimpleNamespace

import pytest

from retraction_control.adapters import (
    AdapterTimeoutError,
    AdapterUnavailableError,
    ForceTorqueSample,
    OwnershipError,
    SingleOwnerGuard,
)
from retraction_control.adapters.aft200 import Aft200Adapter
from retraction_control.adapters.clock import FakeClock
from retraction_control.adapters.fake import (
    CallTrace,
    FakeAft200Adapter,
    FakeIndyDcp3Adapter,
)
from retraction_control.adapters.indy_dcp3 import IndyDcp3Adapter
from retraction_control.command_executor import (
    CommandExecutor,
    ExecutionStatus,
    ExecutorState,
)
from retraction_control.profile_loader import load_profile
from retraction_control.teaching_session import TeachingSessionRepository


CONFIG_ROOT = Path(__file__).resolve().parents[1] / "config"


def _make_executor(tmp_path, *, now_ns: int = 10):
    clock = FakeClock(monotonic_value_ns=now_ns, wall_value_ns=1_000)
    trace = CallTrace(clock)
    robot = FakeIndyDcp3Adapter(
        trace=trace,
        joint_positions={"arm_1": (0.0, 0.0), "arm_2": (0.0, 0.0)},
    )
    sensor = FakeAft200Adapter(
        trace=trace,
        samples={
            "fake_left": ForceTorqueSample(
                0, "fake_left", (1.0, 0.0, 0.0), (0.0, 0.0, 0.0)
            ),
            "fake_right": ForceTorqueSample(
                0, "fake_right", (1.0, 0.0, 0.0), (0.0, 0.0, 0.0)
            )
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
    )
    executor.start()
    trace.clear()
    return executor, robot, sensor, trace


def _request(command_id: str, command: int, **values):
    return SimpleNamespace(
        command_id=command_id,
        command=command,
        target_side=values.get("target_side", 0),
        distance_m=values.get("distance_m", 0.0),
    )


def test_adapter_error_code_reaches_outcome_and_rollback_order_is_stable(tmp_path):
    executor, robot, _sensor, trace = _make_executor(tmp_path)
    robot.fail_next(
        "set_direct_teaching",
        AdapterTimeoutError(
            "direct_teach_timeout",
            "controller confirmation timed out",
            component="indy_dcp3",
            operation="set_direct_teaching",
            retryable=True,
        ),
    )

    outcome = executor.execute(_request("teach-timeout", 1))

    assert outcome.status is ExecutionStatus.FAILED
    assert outcome.code == "direct_teach_timeout"
    assert outcome.result_code == "direct_teach_timeout"
    assert not outcome.success
    assert outcome.executor_state is ExecutorState.FAULT
    assert outcome.cleanup_errors == ()
    assert trace.method_names == (
        "set_friction_compensation",
        "set_custom_gain",
        "set_direct_teaching",
        "end_recording",
        "set_direct_teaching",
        "set_custom_gain",
        "set_friction_compensation",
    )
    assert executor.active_command_id == ""
    assert executor.active_operation == ""


def test_cleanup_failure_is_reported_without_masking_primary_error(tmp_path):
    executor, robot, _sensor, _trace = _make_executor(tmp_path)
    robot.fail_next(
        "set_direct_teaching",
        AdapterTimeoutError(
            "primary_timeout",
            "primary failure",
            component="indy_dcp3",
            operation="set_direct_teaching",
        ),
    )
    robot.fail_next(
        "set_direct_teaching",
        AdapterUnavailableError(
            "cleanup_disconnected",
            "cleanup failed",
            component="indy_dcp3",
            operation="set_direct_teaching",
        ),
    )

    outcome = executor.execute(_request("teach-cleanup-failure", 1))

    assert outcome.code == "primary_timeout"
    assert len(outcome.cleanup_errors) == 1
    assert outcome.cleanup_errors[0].startswith(
        "disable_direct_teaching:cleanup_disconnected:"
    )


def test_stale_force_sample_fails_before_jog_and_reports_affected_arm(tmp_path):
    executor, _robot, _sensor, trace = _make_executor(
        tmp_path, now_ns=2_000_000_000
    )
    executor._state = ExecutorState.RETRACTING

    outcome = executor.execute(
        _request("stale-adjust", 4, target_side=1, distance_m=0.010)
    )

    assert outcome.status is ExecutionStatus.FAILED
    assert outcome.code == "force_sample_stale"
    assert outcome.affected_arm_id == "arm_1"
    assert "jog_tcp" not in trace.method_names
    assert trace.method_names == (
        "latest_sample",
        "stop_motion",
        "set_custom_gain",
    )


def test_retraction_never_enables_gain_when_any_force_channel_is_stale(tmp_path):
    executor, _robot, sensor, trace = _make_executor(tmp_path)
    assert executor.execute(_request("teach-start", 1)).success
    assert executor.execute(_request("teach-finish", 2)).success
    executor.clock.advance(2_000_000_000)
    sensor.set_sample(
        ForceTorqueSample(
            0, "fake_left", (1.0, 0.0, 0.0), (0.0, 0.0, 0.0)
        )
    )
    trace.clear()

    outcome = executor.execute(_request("retract-stale", 3))

    assert outcome.status is ExecutionStatus.FAILED
    assert outcome.code == "force_sample_stale"
    assert "move_joint_positions" not in trace.method_names
    custom_gain_calls = [
        call for call in trace.records if call.method == "set_custom_gain"
    ]
    # The only gain call is the failure cleanup's explicit disable.
    assert len(custom_gain_calls) == 1
    assert custom_gain_calls[0].args[0] is False


def test_process_lock_rejects_second_owner_until_first_releases(tmp_path):
    path = tmp_path / "single-owner.lock"
    first = SingleOwnerGuard(path)
    second = SingleOwnerGuard(path)

    first.acquire()
    with pytest.raises(OwnershipError):
        second.acquire()
    first.release()
    second.acquire()
    assert second.acquired
    second.release()


def test_production_shells_are_inert_and_fail_closed_without_backends():
    robot = IndyDcp3Adapter()
    sensor = Aft200Adapter()

    assert not robot.configured
    assert not sensor.running
    with pytest.raises(AdapterUnavailableError) as robot_error:
        robot.controller_state()
    with pytest.raises(AdapterUnavailableError) as sensor_error:
        sensor.start()

    assert robot_error.value.code == "indy_backend_unconfigured"
    assert sensor_error.value.code == "aft200_backend_unconfigured"
