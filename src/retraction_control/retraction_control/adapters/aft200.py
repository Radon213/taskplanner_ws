"""Receive-only AFT200 adapter shell around an injected CAN reader backend."""

from __future__ import annotations

import threading
from typing import Protocol, runtime_checkable

from . import AdapterError, AdapterUnavailableError, ForceTorqueSample


@runtime_checkable
class Aft200ReaderBackend(Protocol):
    """Version-specific backend implemented by the deployment integration."""

    def read_sample(self, timeout_sec: float) -> ForceTorqueSample | None: ...

    def close(self) -> None: ...


class Aft200Adapter:
    """Own one bounded receive thread and expose only validated latest samples.

    No CAN channel is opened until :meth:`start` is called.  The injected reader
    owns channel/bitrate/udev details, all of which remain deployment config.
    """

    component = "aft200"

    def __init__(
        self,
        backend: Aft200ReaderBackend | None = None,
        *,
        read_timeout_sec: float = 0.1,
        stop_timeout_sec: float = 1.0,
    ) -> None:
        if read_timeout_sec <= 0.0 or stop_timeout_sec <= 0.0:
            raise ValueError("AFT200 reader timeouts must be positive")
        self._backend = backend
        self._read_timeout_sec = float(read_timeout_sec)
        self._stop_timeout_sec = float(stop_timeout_sec)
        self._samples: dict[str, ForceTorqueSample] = {}
        self._last_error: BaseException | None = None
        self._recording_session_id = ""
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._closed = False
        self._lock = threading.RLock()

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def recording_session_id(self) -> str:
        with self._lock:
            return self._recording_session_id

    def start(self) -> None:
        if self._closed:
            raise AdapterUnavailableError(
                "sensor_closed",
                "AFT200 adapter is closed",
                component=self.component,
                operation="start",
            )
        if self._backend is None:
            raise AdapterUnavailableError(
                "aft200_backend_unconfigured",
                "an explicit AFT200 CAN reader backend is required",
                component=self.component,
                operation="start",
            )
        if self.running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._read_loop,
            name="aft200-reader",
            daemon=True,
        )
        self._thread.start()

    def _read_loop(self) -> None:
        assert self._backend is not None
        while not self._stop_event.is_set():
            try:
                sample = self._backend.read_sample(self._read_timeout_sec)
                if sample is None:
                    continue
                if not isinstance(sample, ForceTorqueSample):
                    raise TypeError("reader returned an untyped force sample")
                with self._lock:
                    self._samples[sample.sensor_id] = sample
                    self._last_error = None
            except BaseException as exc:  # thread boundary: save and fail closed
                with self._lock:
                    self._last_error = exc
                self._stop_event.set()

    def stop(self) -> None:
        thread = self._thread
        self._stop_event.set()
        if thread is not None:
            thread.join(self._stop_timeout_sec)
            if thread.is_alive():
                raise AdapterError(
                    "aft200_reader_stop_timeout",
                    "AFT200 receive thread did not stop before its deadline",
                    component=self.component,
                    operation="stop",
                )
        self._thread = None
        with self._lock:
            self._recording_session_id = ""

    def begin_recording(self, session_id: str) -> None:
        if not self.running:
            raise AdapterUnavailableError(
                "sensor_not_running",
                "AFT200 reader must be running before recording",
                component=self.component,
                operation="begin_recording",
            )
        normalized = str(session_id).strip()
        if not normalized:
            raise ValueError("session_id must not be empty")
        with self._lock:
            if self._recording_session_id:
                raise AdapterError(
                    "recording_busy",
                    "an AFT200 recording is already active",
                    component=self.component,
                    operation="begin_recording",
                )
            self._recording_session_id = normalized

    def end_recording(self) -> None:
        with self._lock:
            self._recording_session_id = ""

    def latest_sample(self, sensor_id: str) -> ForceTorqueSample:
        if not self.running:
            with self._lock:
                error = self._last_error
            detail = f": {error}" if error is not None else ""
            raise AdapterUnavailableError(
                "sensor_not_running",
                f"AFT200 reader is not running{detail}",
                component=self.component,
                operation="latest_sample",
                retryable=True,
            )
        with self._lock:
            sample = self._samples.get(str(sensor_id))
        if sample is None:
            raise AdapterUnavailableError(
                "force_sample_missing",
                f"no AFT200 sample is available for {sensor_id!r}",
                component=self.component,
                operation="latest_sample",
                retryable=True,
            )
        if not sample.valid:
            raise AdapterError(
                "force_sample_invalid",
                f"latest AFT200 sample for {sensor_id!r} is invalid",
                component=self.component,
                operation="latest_sample",
            )
        return sample

    def close(self) -> None:
        if self._closed:
            return
        stop_error: BaseException | None = None
        try:
            self.stop()
        except BaseException as exc:
            stop_error = exc
        self._closed = True
        backend = self._backend
        if backend is not None:
            try:
                backend.close()
            except Exception as exc:
                if stop_error is None:
                    stop_error = AdapterError(
                        "aft200_close_error",
                        f"AFT200 backend close failed: {exc}",
                        component=self.component,
                        operation="close",
                    )
        if stop_error is not None:
            raise stop_error


__all__ = ["Aft200Adapter", "Aft200ReaderBackend"]
