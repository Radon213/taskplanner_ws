"""Non-audio LAN WebSocket readiness monitoring for operational ASR.

The monitor deliberately proves only that the reviewed LAN endpoint accepts a
WebSocket Upgrade request.  It never opens a microphone, sends PCM, or waits
for recognition output.  A separate live ASR session still owns the audio and
is the only path that can publish finalized speech.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, Callable

from integration_debug.asr_endpoints import validate_websocket_url


LAN_HEALTH_UNKNOWN = "UNKNOWN"
LAN_HEALTH_CHECKING = "CHECKING"
LAN_HEALTH_READY = "READY"
LAN_HEALTH_UNAVAILABLE = "UNAVAILABLE"
LAN_HEALTH_STALE = "STALE"
LAN_HEALTH_METHOD = "websocket_handshake"

Probe = Callable[[str, float], float]


def probe_websocket_handshake(url: str, timeout_sec: float) -> float:
    """Return the WebSocket Upgrade latency without sending ASR data.

    ``websockets.connect`` performs the HTTP Upgrade and validates the peer's
    WebSocket response.  The connection is closed immediately afterwards; no
    protocol configuration, ping, audio, transcript, or credentials are sent.
    """

    endpoint = validate_websocket_url(url)
    timeout = max(0.05, float(timeout_sec))

    async def connect_once() -> float:
        try:
            import websockets  # type: ignore[import-not-found]
        except Exception as exc:  # pragma: no cover - deployment dependency
            raise RuntimeError(f"ASR WebSocket dependency unavailable: {exc}") from exc

        started = time.monotonic()
        async with websockets.connect(
            endpoint,
            open_timeout=timeout,
            close_timeout=min(0.2, timeout),
            ping_interval=None,
            max_size=1_024,
        ):
            return round((time.monotonic() - started) * 1_000.0, 1)

    # Bound the entire connection and close lifecycle.  This runs only in the
    # monitor's dedicated thread, never inside a ROS callback or microphone
    # start request.
    return asyncio.run(asyncio.wait_for(connect_once(), timeout=timeout + 0.3))


class LanAsrHealthMonitor:
    """Continuously cache LAN WebSocket readiness for instant route choice."""

    def __init__(
        self,
        *,
        url: str,
        interval_sec: float = 1.0,
        failure_interval_sec: float = 0.5,
        timeout_sec: float = 0.5,
        stale_after_sec: float = 2.0,
        probe: Probe = probe_websocket_handshake,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._url = validate_websocket_url(url)
        self._interval_sec = max(0.2, float(interval_sec))
        self._failure_interval_sec = max(0.2, float(failure_interval_sec))
        self._timeout_sec = max(0.05, float(timeout_sec))
        self._stale_after_sec = max(self._interval_sec, float(stale_after_sec))
        self._probe = probe
        self._monotonic = monotonic
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._checking = False
        self._ready: bool | None = None
        self._checked_monotonic: float | None = None
        self._latency_ms: float | None = None
        self._consecutive_failures = 0
        self._last_error = ""

    def start(self) -> None:
        """Start one daemon worker and probe immediately before its first wait."""

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="taskplanner-asr-lan-health",
                daemon=True,
            )
            self._thread.start()

    def close(self) -> bool:
        """Stop monitoring without blocking the operational shutdown forever."""

        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=self._timeout_sec + 1.0)
        return not thread.is_alive()

    def run_once(self) -> bool:
        """Perform one probe; exposed so tests never need a real LAN endpoint."""

        started = self._monotonic()
        with self._lock:
            self._checking = True
        try:
            reported_latency = float(self._probe(self._url, self._timeout_sec))
            elapsed_ms = max(0.0, (self._monotonic() - started) * 1_000.0)
            latency_ms = reported_latency if reported_latency >= 0 else elapsed_ms
        except Exception as exc:
            with self._lock:
                self._checking = False
                self._ready = False
                self._checked_monotonic = self._monotonic()
                self._latency_ms = None
                self._consecutive_failures += 1
                self._last_error = f"{type(exc).__name__}: {exc}"[:240]
            return False

        with self._lock:
            self._checking = False
            self._ready = True
            self._checked_monotonic = self._monotonic()
            self._latency_ms = round(latency_ms, 1)
            self._consecutive_failures = 0
            self._last_error = ""
        return True

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-safe, cached result; this function does no I/O."""

        now = self._monotonic()
        with self._lock:
            checked = self._checked_monotonic
            age_ms = (
                round(max(0.0, now - checked) * 1_000.0, 1)
                if checked is not None
                else None
            )
            stale = age_ms is None or age_ms > self._stale_after_sec * 1_000.0
            if self._checking and checked is None:
                state = LAN_HEALTH_CHECKING
            elif self._ready is True and not stale:
                state = LAN_HEALTH_READY
            elif stale:
                state = LAN_HEALTH_STALE
            elif self._ready is False:
                state = LAN_HEALTH_UNAVAILABLE
            else:
                state = LAN_HEALTH_UNKNOWN
            return {
                "enabled": True,
                "state": state,
                "method": LAN_HEALTH_METHOD,
                "age_ms": age_ms,
                "latency_ms": self._latency_ms,
                "consecutive_failures": self._consecutive_failures,
                "last_error": self._last_error,
            }

    def _run(self) -> None:
        while not self._stop.is_set():
            cycle_started = self._monotonic()
            ready = self.run_once()
            target_period = self._interval_sec if ready else self._failure_interval_sec
            # The configured cadence is measured from probe start, rather than
            # adding another full wait after a slow timeout.  This keeps an
            # unavailable LAN on the requested fast retry cadence without
            # overlapping probes.
            delay = max(0.0, target_period - (self._monotonic() - cycle_started))
            self._stop.wait(delay)
