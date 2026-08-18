from pathlib import Path

import pytest
from rclpy.qos import ReliabilityPolicy

from shadow_evaluation.trace_recorder import (
    DIAGNOSTICS_TRACE_QOS,
    IMAGE_TRACE_QOS,
    SemanticTraceGate,
    open_run_trace_writer,
    semantic_trace_signature,
)
from shadow_evaluation.trace_io import AsyncTraceWriter, TraceWriter


def _append_one(writer):
    writer.append(
        layer="test",
        topic="/test",
        message_type="std_msgs/msg/String",
        ros_time_sec=1.0,
        wall_time_sec=2.0,
        payload={"value": 1},
    )


def test_existing_trace_is_preserved_and_new_run_gets_unique_path(tmp_path):
    requested = tmp_path / "shadow_trace.jsonl"
    requested.write_text("sentinel\n", encoding="utf-8")

    writer, actual = open_run_trace_writer(
        requested,
        run_id="0704_6/run two",
        mode="strict",
    )
    _append_one(writer)
    writer.close()

    assert actual != requested
    assert actual.parent == requested.parent
    assert actual.name.startswith("shadow_trace.0704_6-run-two.")
    assert requested.read_text(encoding="utf-8") == "sentinel\n"
    assert '"run_id":"0704_6/run two"' in actual.read_text(
        encoding="utf-8"
    )


def test_first_run_keeps_requested_trace_path(tmp_path):
    requested = tmp_path / "shadow_trace.jsonl"

    writer, actual = open_run_trace_writer(
        requested,
        run_id="first",
        mode="strict",
    )
    writer.close()

    assert actual == requested
    assert requested.exists()


def test_error_policy_retains_legacy_collision_behavior(tmp_path):
    requested = Path(tmp_path) / "shadow_trace.jsonl"
    requested.write_text("existing\n", encoding="utf-8")

    with pytest.raises(FileExistsError):
        open_run_trace_writer(
            requested,
            run_id="second",
            mode="strict",
            existing_file_policy="error",
        )


def test_image_trace_qos_accepts_best_effort_camera_publishers():
    assert IMAGE_TRACE_QOS.reliability == ReliabilityPolicy.BEST_EFFORT
    assert IMAGE_TRACE_QOS.depth == 256


def test_diagnostics_trace_qos_accepts_best_effort_contract_publisher():
    assert DIAGNOSTICS_TRACE_QOS.reliability == ReliabilityPolicy.BEST_EFFORT
    assert DIAGNOSTICS_TRACE_QOS.depth == 50


def test_semantic_trace_signature_ignores_nested_freshness_metadata():
    baseline = {
        "stamp": {"sec": 10, "nanosec": 0},
        "revision": 4,
        "arms": [
            {
                "arm_id": "arm_1",
                "state": "standby",
                "stamp": {"sec": 10, "nanosec": 0},
            }
        ],
    }
    heartbeat = {
        "stamp": {"sec": 11, "nanosec": 0},
        "revision": 5,
        "arms": [
            {
                "arm_id": "arm_1",
                "state": "standby",
                "stamp": {"sec": 11, "nanosec": 0},
            }
        ],
    }

    assert semantic_trace_signature(baseline) == semantic_trace_signature(
        heartbeat
    )


def test_semantic_trace_gate_keeps_transitions_and_sparse_checkpoints():
    gate = SemanticTraceGate(checkpoint_sec=30.0)
    kwargs = {
        "layer": "runtime_state",
        "topic": "/simulation/state",
    }

    assert gate.should_append(payload={"state": "idle"}, now_monotonic=1.0, **kwargs)
    assert not gate.should_append(
        payload={"state": "idle", "stamp": {"sec": 2}},
        now_monotonic=2.0,
        **kwargs,
    )
    assert gate.should_append(
        payload={"state": "running", "stamp": {"sec": 3}},
        now_monotonic=3.0,
        **kwargs,
    )
    assert not gate.should_append(
        payload={"state": "running", "stamp": {"sec": 4}},
        now_monotonic=20.0,
        **kwargs,
    )
    assert gate.should_append(
        payload={"state": "running", "stamp": {"sec": 34}},
        now_monotonic=34.0,
        **kwargs,
    )


def test_semantic_trace_gate_does_not_deduplicate_event_layers():
    gate = SemanticTraceGate(checkpoint_sec=30.0)
    kwargs = {
        "layer": "skill_event",
        "topic": "/skill/events",
        "payload": {"event_type": "ToolHandoverCompleted"},
    }

    assert gate.should_append(now_monotonic=1.0, **kwargs)
    assert gate.should_append(now_monotonic=1.1, **kwargs)


def test_semantic_trace_gate_checkpoints_unchanged_rfdetr_health_stamps():
    gate = SemanticTraceGate(checkpoint_sec=30.0)
    kwargs = {
        "layer": "rfdetr_health",
        "topic": "/surgery/perception/rfdetr/health",
    }
    baseline = {
        "schema": "pnu.rfdetr_health.v2",
        "state": "waiting_for_frame",
        "model_ready": False,
        "stamp_sec": 10,
        "stamp_nanosec": 20,
    }

    assert gate.should_append(payload=baseline, now_monotonic=0.0, **kwargs)
    assert not gate.should_append(
        payload={**baseline, "stamp_sec": 11, "stamp_nanosec": 30},
        now_monotonic=1.0,
        **kwargs,
    )
    assert gate.should_append(
        payload={**baseline, "state": "ready", "model_ready": True},
        now_monotonic=2.0,
        **kwargs,
    )


def test_semantic_trace_gate_checkpoints_repeated_runtime_control_state():
    gate = SemanticTraceGate(checkpoint_sec=30.0)
    kwargs = {
        "layer": "runtime_control",
        "topic": "/simulation/control_state",
    }

    assert gate.should_append(
        payload={"data": "stop"}, now_monotonic=1.0, **kwargs
    )
    assert not gate.should_append(
        payload={"data": "stop"}, now_monotonic=1.1, **kwargs
    )
    assert gate.should_append(
        payload={"data": "start"}, now_monotonic=1.2, **kwargs
    )
    assert gate.should_append(
        payload={"data": "mute_actor:5.0"}, now_monotonic=1.3, **kwargs
    )
    assert gate.should_append(
        payload={"data": "mute_actor:5.0"}, now_monotonic=1.4, **kwargs
    )


def test_runtime_reset_is_never_checkpointed_and_reopens_start_edge():
    gate = SemanticTraceGate(checkpoint_sec=30.0)
    kwargs = {
        "layer": "runtime_control",
        "topic": "/simulation/control_state",
    }

    assert gate.should_append(
        payload={"data": "start"}, now_monotonic=1.0, **kwargs
    )
    assert not gate.should_append(
        payload={"data": "start"}, now_monotonic=1.1, **kwargs
    )
    assert gate.should_append(
        payload={"data": "reset"}, now_monotonic=1.2, **kwargs
    )
    assert gate.should_append(
        payload={"data": "reset"}, now_monotonic=1.3, **kwargs
    )
    assert gate.should_append(
        payload={"data": "start"}, now_monotonic=1.4, **kwargs
    )


def test_semantic_trace_gate_preserves_controller_revision_rollback():
    gate = SemanticTraceGate(checkpoint_sec=30.0)
    kwargs = {
        "layer": "bed_robot_arm_status",
        "topic": "/bed_robot_arm/status",
    }

    assert gate.should_append(
        payload={"revision": 9, "arms": [{"arm_id": "arm_1", "state": "standby"}]},
        now_monotonic=1.0,
        **kwargs,
    )
    assert not gate.should_append(
        payload={"revision": 10, "arms": [{"arm_id": "arm_1", "state": "standby"}]},
        now_monotonic=2.0,
        **kwargs,
    )
    assert gate.should_append(
        payload={"revision": 1, "arms": [{"arm_id": "arm_1", "state": "standby"}]},
        now_monotonic=3.0,
        **kwargs,
    )


def test_bed_robot_heartbeat_trace_keeps_edges_and_sparse_checkpoints():
    gate = SemanticTraceGate(checkpoint_sec=30.0)
    kwargs = {
        "layer": "bed_robot_arm_status",
        "topic": "/external/bed_robot_arms/status",
    }

    assert gate.should_append(
        payload={"revision": 1, "arms": [{"arm_id": "arm_1", "state": "standby"}]},
        now_monotonic=1.0,
        **kwargs,
    )
    assert not gate.should_append(
        payload={"revision": 2, "arms": [{"arm_id": "arm_1", "state": "standby"}]},
        now_monotonic=1.5,
        **kwargs,
    )
    assert gate.should_append(
        payload={"revision": 3, "arms": [{"arm_id": "arm_1", "state": "retracting"}]},
        now_monotonic=2.0,
        **kwargs,
    )
    assert not gate.should_append(
        payload={"revision": 4, "arms": [{"arm_id": "arm_1", "state": "retracting"}]},
        now_monotonic=2.5,
        **kwargs,
    )
    assert gate.should_append(
        payload={"revision": 5, "arms": [{"arm_id": "arm_1", "state": "retracting"}]},
        now_monotonic=32.0,
        **kwargs,
    )


def test_open_writer_keeps_synchronous_default(tmp_path):
    writer, _actual = open_run_trace_writer(
        tmp_path / "default.jsonl",
        run_id="default",
        mode="strict",
    )
    try:
        assert isinstance(writer, TraceWriter)
    finally:
        writer.close()


def test_open_writer_can_enable_async_recorder_mode(tmp_path):
    writer, actual = open_run_trace_writer(
        tmp_path / "async.jsonl",
        run_id="async",
        mode="strict",
        asynchronous=True,
        flush_every_records=8,
    )
    try:
        assert isinstance(writer, AsyncTraceWriter)
        _append_one(writer)
    finally:
        writer.close()

    assert actual.read_text(encoding="utf-8").count("\n") == 1
