"""Small append-only trace writer used by the ROS recorder."""

from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import queue
import threading
from typing import Any


TRACE_SCHEMA = "taskplanner.shadow_trace.v1"
RUN_MODES = {"strict", "reconciled", "oracle"}


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def payload_sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class TraceWriter:
    def __init__(
        self,
        path: Path,
        *,
        run_id: str,
        mode: str,
        flush_every_records: int = 1,
    ) -> None:
        if mode not in RUN_MODES:
            raise ValueError(f"invalid shadow mode {mode!r}")
        if flush_every_records < 1:
            raise ValueError("flush_every_records must be at least 1")
        self._path = path
        self._run_id = run_id
        self._mode = mode
        self._flush_every_records = flush_every_records
        self._sequence = 0
        path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = path.open("x", encoding="utf-8")

    @property
    def count(self) -> int:
        return self._sequence

    def append(
        self,
        *,
        layer: str,
        topic: str,
        message_type: str,
        ros_time_sec: float,
        wall_time_sec: float,
        payload: dict[str, Any],
        source_stamp_sec: float | None = None,
        correlation_id: str = "",
    ) -> None:
        record: dict[str, Any] = {
            "schema": TRACE_SCHEMA,
            "run_id": self._run_id,
            "sequence": self._sequence,
            "mode": self._mode,
            "layer": layer,
            "topic": topic,
            "message_type": message_type,
            "ros_time_sec": round(max(0.0, float(ros_time_sec)), 9),
            "wall_time_sec": round(max(0.0, float(wall_time_sec)), 9),
            "payload": payload,
            "payload_sha256": payload_sha256(payload),
        }
        if source_stamp_sec is not None and source_stamp_sec >= 0.0:
            record["source_stamp_sec"] = round(float(source_stamp_sec), 9)
        if correlation_id:
            record["correlation_id"] = correlation_id
        self._stream.write(canonical_json(record) + "\n")
        next_sequence = self._sequence + 1
        if next_sequence % self._flush_every_records == 0:
            self._stream.flush()
        self._sequence = next_sequence

    def close(self) -> None:
        if not self._stream.closed:
            error: BaseException | None = None
            try:
                self._stream.flush()
            except BaseException as exc:
                error = exc
            try:
                self._stream.close()
            except BaseException as exc:
                if error is None:
                    error = exc
            if error is not None:
                raise error

    def flush(self) -> None:
        if not self._stream.closed:
            self._stream.flush()


@dataclass(frozen=True)
class _AppendRequest:
    kwargs: dict[str, Any]


_STOP = object()


class AsyncTraceWriter:
    """Serialize trace writes on one worker and surface worker failures."""

    def __init__(
        self,
        writer: TraceWriter,
        *,
        idle_flush_interval_sec: float = 2.0,
        max_pending_records: int = 8192,
        enqueue_timeout_sec: float = 0.05,
        close_timeout_sec: float = 5.0,
    ) -> None:
        if idle_flush_interval_sec <= 0.0:
            raise ValueError("idle flush interval must be positive")
        if max_pending_records < 1:
            raise ValueError("max pending records must be at least 1")
        if enqueue_timeout_sec < 0.0:
            raise ValueError("enqueue timeout cannot be negative")
        if close_timeout_sec <= 0.0:
            raise ValueError("close timeout must be positive")
        self._writer = writer
        self._idle_flush_interval_sec = float(idle_flush_interval_sec)
        self._enqueue_timeout_sec = float(enqueue_timeout_sec)
        self._close_timeout_sec = float(close_timeout_sec)
        self._pending_slots = threading.BoundedSemaphore(max_pending_records)
        self._queue: queue.Queue[_AppendRequest | object] = queue.Queue(
            # Keep one reserved slot so shutdown can always enqueue its
            # sentinel even when every data slot is occupied.
            maxsize=max_pending_records + 1
        )
        self._state_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._closed = False
        self._underlying_closed = False
        self._failure: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="shadow-trace-writer",
            daemon=True,
        )
        self._thread.start()

    @property
    def count(self) -> int:
        return self._writer.count

    def _set_failure(self, error: BaseException) -> None:
        with self._state_lock:
            if self._failure is None:
                self._failure = error

    def _failure_snapshot(self) -> BaseException | None:
        with self._state_lock:
            return self._failure

    @staticmethod
    def _raise_failure(error: BaseException | None) -> None:
        if error is not None:
            raise RuntimeError("asynchronous trace writer failed") from error

    def append(
        self,
        *,
        layer: str,
        topic: str,
        message_type: str,
        ros_time_sec: float,
        wall_time_sec: float,
        payload: dict[str, Any],
        source_stamp_sec: float | None = None,
        correlation_id: str = "",
    ) -> None:
        request = _AppendRequest(
            kwargs={
                "layer": layer,
                "topic": topic,
                "message_type": message_type,
                "ros_time_sec": ros_time_sec,
                "wall_time_sec": wall_time_sec,
                "payload": copy.deepcopy(payload),
                "source_stamp_sec": source_stamp_sec,
                "correlation_id": correlation_id,
            }
        )
        with self._state_lock:
            if self._closed:
                raise RuntimeError("asynchronous trace writer is closed")
            self._raise_failure(self._failure)
            if not self._pending_slots.acquire(timeout=self._enqueue_timeout_sec):
                failure = BufferError(
                    "asynchronous trace queue is full; trace storage is not "
                    "keeping up"
                )
                self._failure = failure
                raise RuntimeError("asynchronous trace writer failed") from failure
            try:
                self._queue.put_nowait(request)
            except BaseException:
                self._pending_slots.release()
                raise

    def _run(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=self._idle_flush_interval_sec)
            except queue.Empty:
                flush = getattr(self._writer, "flush", None)
                if callable(flush) and self._failure_snapshot() is None:
                    try:
                        flush()
                    except BaseException as exc:
                        self._set_failure(exc)
                continue
            if isinstance(item, _AppendRequest):
                # Capacity tracks records waiting in the queue. The worker's
                # one in-flight record is separately bounded by this thread.
                self._pending_slots.release()
            try:
                if item is _STOP:
                    try:
                        self._writer.close()
                    except BaseException as exc:
                        self._set_failure(exc)
                    finally:
                        with self._state_lock:
                            self._underlying_closed = True
                    return
                if not isinstance(item, _AppendRequest):
                    self._set_failure(
                        TypeError("unexpected asynchronous trace queue item")
                    )
                elif self._failure_snapshot() is None:
                    try:
                        self._writer.append(**item.kwargs)
                    except BaseException as exc:
                        self._set_failure(exc)
            finally:
                self._queue.task_done()

    def close(self) -> None:
        with self._close_lock:
            with self._state_lock:
                if not self._closed:
                    self._closed = True
                    enqueue_stop = True
                else:
                    enqueue_stop = False
            if enqueue_stop:
                self._queue.put_nowait(_STOP)
            self._thread.join(timeout=self._close_timeout_sec)
            if self._thread.is_alive():
                self._set_failure(
                    TimeoutError(
                        "asynchronous trace writer did not close within "
                        f"{self._close_timeout_sec:.3f}s"
                    )
                )
            self._raise_failure(self._failure_snapshot())
