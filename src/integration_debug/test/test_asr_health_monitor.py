import threading

from integration_debug.asr_health_monitor import (
    LAN_HEALTH_READY,
    LAN_HEALTH_STALE,
    LAN_HEALTH_UNAVAILABLE,
    LanAsrHealthMonitor,
)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_monitor_caches_websocket_handshake_without_audio() -> None:
    calls = []
    clock = Clock()

    def probe(url: str, timeout_sec: float) -> float:
        calls.append((url, timeout_sec))
        return 12.4

    monitor = LanAsrHealthMonitor(
        url="ws://192.168.1.5:1196/",
        interval_sec=1.0,
        timeout_sec=0.5,
        stale_after_sec=2.0,
        probe=probe,
        monotonic=clock,
    )

    assert monitor.run_once() is True
    assert calls == [("ws://192.168.1.5:1196/", 0.5)]
    assert monitor.snapshot() == {
        "enabled": True,
        "state": LAN_HEALTH_READY,
        "method": "websocket_handshake",
        "age_ms": 0.0,
        "latency_ms": 12.4,
        "consecutive_failures": 0,
        "last_error": "",
    }


def test_monitor_marks_failures_and_stale_results_without_network() -> None:
    clock = Clock()

    def unavailable(_url: str, _timeout_sec: float) -> float:
        raise TimeoutError("timed out")

    monitor = LanAsrHealthMonitor(
        url="ws://192.168.1.5:1196/",
        interval_sec=1.0,
        timeout_sec=0.5,
        stale_after_sec=2.0,
        probe=unavailable,
        monotonic=clock,
    )

    assert monitor.run_once() is False
    unavailable_snapshot = monitor.snapshot()
    assert unavailable_snapshot["state"] == LAN_HEALTH_UNAVAILABLE
    assert unavailable_snapshot["consecutive_failures"] == 1
    assert unavailable_snapshot["last_error"].startswith("TimeoutError:")

    clock.now = 2.1
    assert monitor.snapshot()["state"] == LAN_HEALTH_STALE


def test_monitor_worker_probes_immediately_and_stops_bounded() -> None:
    called = threading.Event()

    def probe(_url: str, _timeout_sec: float) -> float:
        called.set()
        return 1.0

    monitor = LanAsrHealthMonitor(
        url="ws://192.168.1.5:1196/",
        interval_sec=10.0,
        timeout_sec=0.1,
        probe=probe,
    )
    monitor.start()

    assert called.wait(timeout=1.0)
    assert monitor.close() is True
