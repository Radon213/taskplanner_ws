from __future__ import annotations

import json
from pathlib import Path
import threading
import time
from typing import Any

import pytest

from shadow_evaluation.trace_io import AsyncTraceWriter, TraceWriter


def _append(writer: Any, value: int, payload: dict[str, Any] | None = None) -> None:
    writer.append(
        layer="test",
        topic="/test",
        message_type="test/msg/Test",
        ros_time_sec=float(value),
        wall_time_sec=float(value),
        payload=payload if payload is not None else {"value": value},
    )


def test_trace_writer_default_still_flushes_each_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Stream:
        closed = False
        flush_count = 0

        def write(self, value: str) -> int:
            return len(value)

        def flush(self) -> None:
            self.flush_count += 1

        def close(self) -> None:
            self.closed = True

    stream = Stream()
    monkeypatch.setattr(Path, "open", lambda *_args, **_kwargs: stream)

    writer = TraceWriter(tmp_path / "trace.jsonl", run_id="run", mode="strict")
    _append(writer, 1)
    _append(writer, 2)

    assert stream.flush_count == 2
    writer.close()
    assert stream.flush_count == 3
    assert stream.closed


def test_trace_writer_batches_flushes_and_flushes_on_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Stream:
        closed = False
        flush_count = 0

        def write(self, value: str) -> int:
            return len(value)

        def flush(self) -> None:
            self.flush_count += 1

        def close(self) -> None:
            self.closed = True

    stream = Stream()
    monkeypatch.setattr(Path, "open", lambda *_args, **_kwargs: stream)

    writer = TraceWriter(
        tmp_path / "trace.jsonl",
        run_id="run",
        mode="strict",
        flush_every_records=3,
    )
    _append(writer, 1)
    _append(writer, 2)
    assert stream.flush_count == 0

    _append(writer, 3)
    assert stream.flush_count == 1

    writer.close()
    assert stream.flush_count == 2
    assert stream.closed


def test_async_writer_preserves_order_snapshots_payload_and_drains() -> None:
    class RecordingWriter:
        def __init__(self) -> None:
            self.records: list[dict[str, Any]] = []
            self.closed = False

        @property
        def count(self) -> int:
            return len(self.records)

        def append(self, **kwargs: Any) -> None:
            self.records.append(kwargs)

        def close(self) -> None:
            self.closed = True

    target = RecordingWriter()
    writer = AsyncTraceWriter(target)
    payload = {"value": 0}
    for value in range(100):
        payload["value"] = value
        _append(writer, value, payload)
    payload["value"] = -1

    writer.close()

    assert [record["payload"]["value"] for record in target.records] == list(
        range(100)
    )
    assert target.closed
    assert writer.count == 100


def test_async_writer_does_not_wait_for_slow_disk_append() -> None:
    class SlowWriter:
        count = 0

        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()
            self.closed = False

        def append(self, **_kwargs: Any) -> None:
            self.started.set()
            assert self.release.wait(timeout=2.0)
            self.count += 1

        def close(self) -> None:
            self.closed = True

    target = SlowWriter()
    writer = AsyncTraceWriter(target)
    start = time.monotonic()
    _append(writer, 1)
    elapsed = time.monotonic() - start

    assert elapsed < 0.5
    assert target.started.wait(timeout=1.0)
    target.release.set()
    writer.close()
    assert target.count == 1
    assert target.closed


def test_async_writer_bounds_pending_records_and_fails_loudly() -> None:
    class SlowWriter:
        count = 0

        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()
            self.closed = False

        def append(self, **_kwargs: Any) -> None:
            self.started.set()
            assert self.release.wait(timeout=2.0)
            self.count += 1

        def close(self) -> None:
            self.closed = True

    target = SlowWriter()
    writer = AsyncTraceWriter(
        target,
        max_pending_records=1,
        enqueue_timeout_sec=0.01,
    )
    _append(writer, 1)
    assert target.started.wait(timeout=1.0)
    _append(writer, 2)

    with pytest.raises(RuntimeError, match="asynchronous trace writer failed"):
        _append(writer, 3)

    target.release.set()
    with pytest.raises(RuntimeError, match="asynchronous trace writer failed"):
        writer.close()
    assert target.count == 1
    assert target.closed


def test_async_writer_close_timeout_does_not_hang_runtime_shutdown() -> None:
    class HungWriter:
        count = 0

        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()
            self.closed = False

        def append(self, **_kwargs: Any) -> None:
            self.started.set()
            assert self.release.wait(timeout=2.0)
            self.count += 1

        def close(self) -> None:
            self.closed = True

    target = HungWriter()
    writer = AsyncTraceWriter(target, close_timeout_sec=0.02)
    _append(writer, 1)
    assert target.started.wait(timeout=1.0)

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="asynchronous trace writer failed") as exc:
        writer.close()
    assert time.monotonic() - started < 0.5
    assert isinstance(exc.value.__cause__, TimeoutError)
    assert not target.closed

    target.release.set()
    deadline = time.monotonic() + 1.0
    while not target.closed and time.monotonic() < deadline:
        time.sleep(0.01)
    assert target.closed


def test_async_writer_underlying_close_timeout_is_bounded() -> None:
    class HungCloseWriter:
        count = 0

        def __init__(self) -> None:
            self.close_started = threading.Event()
            self.release = threading.Event()
            self.closed = False

        def append(self, **_kwargs: Any) -> None:
            self.count += 1

        def close(self) -> None:
            self.close_started.set()
            assert self.release.wait(timeout=2.0)
            self.closed = True

    target = HungCloseWriter()
    writer = AsyncTraceWriter(target, close_timeout_sec=0.03)
    _append(writer, 1)

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="asynchronous trace writer failed") as exc:
        writer.close()
    assert target.close_started.wait(timeout=0.5)
    assert time.monotonic() - started < 0.5
    assert isinstance(exc.value.__cause__, TimeoutError)
    assert not target.closed

    target.release.set()
    deadline = time.monotonic() + 1.0
    while not target.closed and time.monotonic() < deadline:
        time.sleep(0.01)
    assert target.closed


def test_async_writer_surfaces_worker_failure_and_closes() -> None:
    class FailingWriter:
        count = 0

        def __init__(self) -> None:
            self.failed = threading.Event()
            self.closed = False

        def append(self, **_kwargs: Any) -> None:
            self.failed.set()
            raise OSError("disk unavailable")

        def close(self) -> None:
            self.closed = True

    target = FailingWriter()
    writer = AsyncTraceWriter(target)
    _append(writer, 1)
    assert target.failed.wait(timeout=1.0)

    with pytest.raises(RuntimeError, match="asynchronous trace writer failed"):
        writer.close()

    assert target.closed


def test_async_trace_file_is_valid_append_only_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    writer = AsyncTraceWriter(
        TraceWriter(
            path,
            run_id="run",
            mode="strict",
            flush_every_records=16,
        )
    )
    for value in range(40):
        _append(writer, value)
    writer.close()

    records = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert [record["sequence"] for record in records] == list(range(40))
    assert [record["payload"]["value"] for record in records] == list(
        range(40)
    )


def test_async_trace_flushes_sparse_records_during_idle(tmp_path: Path) -> None:
    path = tmp_path / "sparse.jsonl"
    writer = AsyncTraceWriter(
        TraceWriter(
            path,
            run_id="run",
            mode="strict",
            flush_every_records=128,
        ),
        idle_flush_interval_sec=0.02,
    )
    try:
        _append(writer, 1)
        deadline = time.monotonic() + 1.0
        while path.stat().st_size == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert path.stat().st_size > 0
    finally:
        writer.close()
