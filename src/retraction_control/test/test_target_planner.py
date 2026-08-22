from types import SimpleNamespace

import pytest

from retraction_control.adapters import ForceTorqueSample, JointStateSample
from retraction_control.adapters.clock import FakeClock
from retraction_control.adapters.fake import (
    CallTrace,
    FakeAft200Adapter,
    FakeIndyDcp3Adapter,
)
from retraction_control.command_executor import CommandExecutor
from retraction_control.profile_loader import load_profile
from retraction_control.target_planner import (
    CallableTargetPlanner,
    LastSampleTargetPlanner,
    TargetPlannerIdentity,
)
from retraction_control.teaching_session import TeachingSessionRepository


def _samples():
    joints = (
        JointStateSample(1, "arm_1", (0.1, 0.2)),
        JointStateSample(2, "arm_1", (0.3, 0.4)),
    )
    forces = (
        ForceTorqueSample(1, "sensor_1", (1.0, 2.0, 3.0), (0.0, 0.0, 0.0)),
        ForceTorqueSample(2, "sensor_1", (4.0, 5.0, 6.0), (0.0, 0.0, 0.0)),
    )
    return joints, forces


def test_last_sample_planner_is_deterministic_and_permanently_synthetic():
    joints, forces = _samples()
    planner = LastSampleTargetPlanner()

    first = planner.plan(joints, forces)
    second = planner.plan(joints, forces)

    assert first == second
    assert first.identity.synthetic
    assert first.identity.name == "synthetic_last_sample"
    assert first.joint_positions["arm_1"] == (0.3, 0.4)
    assert first.force_targets_n["sensor_1"] == (4.0, 5.0, 6.0)


def test_callable_planner_requires_explicit_checksum_identity():
    identity = TargetPlannerIdentity(
        "reviewed-test", "1.0.0", "sha256:" + "c" * 64, False
    )
    planner = CallableTargetPlanner(
        identity,
        lambda _joints, _forces: ({"arm_1": (0.0,)}, {"sensor_1": (0, 0, 0)}),
    )
    joints, forces = _samples()
    assert planner.plan(joints, forces).identity == identity


def test_synthetic_planner_is_rejected_before_hardware_adapter_start(tmp_path):
    config = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "config"
        / "fake.yaml"
    )
    profile = load_profile(config, require_approved=True)
    clock = FakeClock()
    trace = CallTrace(clock)
    robot = FakeIndyDcp3Adapter(
        trace=trace,
        joint_positions={"arm_1": (0.0,) * 6, "arm_2": (0.0,) * 6},
    )
    sensor = FakeAft200Adapter(trace=trace)

    with pytest.raises(ValueError, match="synthetic target planner"):
        CommandExecutor(
            robot=robot,
            force_sensor=sensor,
            profile=profile,
            sessions=TeachingSessionRepository(tmp_path / "sessions"),
            robot_id="robot",
            controller_id="controller",
            source_revision="test",
            execution_mode="hardware",
        )

    assert trace.records == ()


def test_session_is_bound_to_target_planner_checksum(tmp_path):
    config = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "config"
        / "fake.yaml"
    )
    profile = load_profile(config, require_approved=True)
    clock = FakeClock(monotonic_value_ns=10, wall_value_ns=100)
    trace = CallTrace(clock)
    robot = FakeIndyDcp3Adapter(
        trace=trace,
        joint_positions={"arm_1": (0.0,) * 6, "arm_2": (0.0,) * 6},
    )
    sensor = FakeAft200Adapter(
        trace=trace,
        samples={
            "fake_left": ForceTorqueSample(
                10, "fake_left", (0, 0, 0), (0, 0, 0)
            ),
            "fake_right": ForceTorqueSample(
                10, "fake_right", (0, 0, 0), (0, 0, 0)
            ),
        },
    )
    repository = TeachingSessionRepository(tmp_path / "sessions")
    executor = CommandExecutor(
        robot=robot,
        force_sensor=sensor,
        profile=profile,
        sessions=repository,
        robot_id="robot",
        controller_id="controller",
        source_revision="test",
        clock=clock,
    )
    executor.start()
    assert executor.execute(SimpleNamespace(command_id="teach", command=1)).success
    finished = executor.execute(SimpleNamespace(command_id="finish", command=2))
    assert finished.success
    session = repository.load(finished.session_id)
    assert session.metadata.target_planner == executor.target_planner.identity.as_dict()
