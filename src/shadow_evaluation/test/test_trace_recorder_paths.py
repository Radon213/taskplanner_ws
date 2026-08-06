from pathlib import Path

import pytest
from rclpy.qos import ReliabilityPolicy

from shadow_evaluation.trace_recorder import (
    IMAGE_TRACE_QOS,
    open_run_trace_writer,
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
