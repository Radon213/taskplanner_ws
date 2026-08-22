import json

import pytest

from retraction_control.adapters.clock import FakeClock
from retraction_control.adapters.fake import CallTrace
from retraction_control.adapters.shadow import ShadowIndyDcp3Adapter
from retraction_control.trace_artifact import ShadowTraceRepository, TraceArtifactError


def test_shadow_robot_records_intent_without_mutating_observed_joints():
    clock = FakeClock(monotonic_value_ns=10, auto_step_ns=1)
    trace = CallTrace(clock)
    robot = ShadowIndyDcp3Adapter(
        trace=trace,
        clock=clock,
        observed_joint_positions={"arm_1": (0.1, 0.2)},
    )
    trace.set_command_context("shadow-command")

    before = robot.read_joint_state("arm_1")
    robot.move_joint_positions("arm_1", (9.0, 9.0), waypoint_name="planned")
    robot.jog_tcp("arm_1", axis="x", distance_mm=50.0, frame="tool")
    after = robot.read_joint_state("arm_1")

    assert before.positions == after.positions == (0.1, 0.2)
    assert trace.method_names == (
        "observe_joint_state",
        "intent_move_joint_positions",
        "intent_jog_tcp",
        "observe_joint_state",
    )
    assert all(call.command_id == "shadow-command" for call in trace.records)


def test_shadow_trace_is_atomic_checksum_bound_and_never_claims_motion(tmp_path):
    clock = FakeClock(monotonic_value_ns=10, auto_step_ns=1)
    trace = CallTrace(clock)
    trace.set_command_context("command-1")
    trace.record("indy_dcp3_shadow", "intent_stop_motion")
    repository = ShadowTraceRepository((tmp_path / "traces").resolve())

    path = repository.save(
        command_id="command-1",
        command=6,
        profile_name="synthetic_fake",
        profile_version="1.0.0",
        profile_checksum="sha256:" + "a" * 64,
        source_revision="test",
        target_planner={
            "name": "synthetic_last_sample",
            "version": "1.0.0",
            "checksum": "sha256:" + "b" * 64,
            "synthetic": True,
        },
        terminal_stage="completed",
        terminal_code="completed",
        terminal_message="recorded",
        calls=trace.records_for("command-1"),
    )

    artifact = repository.load_verified("command-1")
    assert path.stat().st_mode & 0o777 == 0o600
    assert artifact["evidence_level"] == "record_only"
    assert artifact["physical_motion_executed"] is False
    assert artifact["calls"][0]["method"] == "intent_stop_motion"

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["physical_motion_executed"] = True
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(TraceArtifactError, match="checksum"):
        repository.load_verified("command-1")


def test_shadow_trace_rejects_cross_command_records(tmp_path):
    clock = FakeClock()
    trace = CallTrace(clock)
    trace.set_command_context("other")
    trace.record("shadow", "intent")
    repository = ShadowTraceRepository((tmp_path / "traces").resolve())

    with pytest.raises(TraceArtifactError, match="different command"):
        repository.save(
            command_id="expected",
            command=1,
            profile_name="fake",
            profile_version="1",
            profile_checksum="sha256:" + "a" * 64,
            source_revision="test",
            target_planner={"synthetic": True},
            terminal_stage="completed",
            terminal_code="completed",
            terminal_message="",
            calls=trace.records,
        )
