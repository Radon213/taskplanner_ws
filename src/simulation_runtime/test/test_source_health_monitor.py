from simulation_runtime.source_health_monitor import (
    DISABLED,
    ERROR,
    MISSING,
    READY,
    RECOVERING,
    STALE,
    SourceTracker,
    SourceHealthMonitor,
)
from surgical_msgs.msg import VLMHealth, VLMResult


def test_source_requires_two_fresh_samples_after_start_and_recovery():
    tracker = SourceTracker("cam4", "image", 1.0, recovery_samples=2)

    assert tracker.snapshot(0.0) == (MISSING, False, -1.0)
    assert tracker.observe(now_monotonic_sec=1.0, source_stamp_sec=10.0)
    assert tracker.snapshot(1.0)[0] == RECOVERING
    assert tracker.observe(now_monotonic_sec=1.1, source_stamp_sec=10.1)
    assert tracker.snapshot(1.1)[0] == READY

    assert tracker.snapshot(2.2)[0] == STALE
    assert tracker.observe(now_monotonic_sec=2.3, source_stamp_sec=11.0)
    assert tracker.epoch == 2
    assert tracker.snapshot(2.3)[0] == RECOVERING
    assert tracker.observe(now_monotonic_sec=2.4, source_stamp_sec=11.1)
    assert tracker.snapshot(2.4)[0] == READY


def test_duplicate_and_out_of_order_source_stamps_are_counted():
    tracker = SourceTracker("flir", "image", 1.0, recovery_samples=1)
    assert tracker.observe(now_monotonic_sec=1.0, source_stamp_sec=4.0)
    assert not tracker.observe(now_monotonic_sec=1.1, source_stamp_sec=4.0)
    assert not tracker.observe(now_monotonic_sec=1.2, source_stamp_sec=3.9)
    assert tracker.received_count == 3
    assert tracker.accepted_count == 1
    assert tracker.rejected_count == 2
    assert tracker.dropped_count == 2


def test_error_and_disabled_states_fail_closed():
    tracker = SourceTracker("vlm", "vision_language_model", 3.0)
    tracker.set_error("vlm_disconnected")
    assert tracker.snapshot(1.0)[0] == ERROR
    tracker.enabled = False
    assert tracker.snapshot(1.0)[0] == DISABLED


def test_vlm_health_error_is_latched_until_healthy_signal_and_fresh_result():
    node = SourceHealthMonitor.__new__(SourceHealthMonitor)
    tracker = SourceTracker(
        "vlm",
        "vision_language_model",
        3.0,
        recovery_samples=1,
    )
    node._trackers = {"vlm": tracker}
    node._last_stamps = {"vlm": None}
    node._vlm_health_blocked = False
    node._monotonic = lambda: 1.0

    unhealthy = VLMHealth()
    unhealthy.connected = False
    unhealthy.healthy = False
    unhealthy.last_error = "provider unavailable"
    node._on_vlm_health(unhealthy)

    result = VLMResult()
    result.stamp.sec = 10
    node._on_vlm_result(result)

    assert tracker.snapshot(1.0)[0] == ERROR
    assert tracker.accepted_count == 0
    assert tracker.rejected_count == 1
    assert tracker.dropped_count == 1

    recovered = VLMHealth()
    recovered.connected = True
    recovered.healthy = True
    node._on_vlm_health(recovered)
    assert tracker.snapshot(1.0)[0] == MISSING

    node._on_vlm_result(result)
    assert tracker.snapshot(1.0)[0] == READY
