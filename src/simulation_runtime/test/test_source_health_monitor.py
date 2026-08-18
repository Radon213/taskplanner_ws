from simulation_runtime.source_health_monitor import (
    DISABLED,
    ERROR,
    MISSING,
    READY,
    RECOVERING,
    STALE,
    STATUS_CHECKPOINT_SEC,
    SourceTracker,
    SourceHealthMonitor,
    StatusPublicationGate,
)
from builtin_interfaces.msg import Time
from surgical_msgs.msg import InputSourceStatus, VLMHealth, VLMResult


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


def _status(
    source: str,
    state: str,
    healthy: bool,
    *,
    epoch: int = 1,
    error_code: str = "",
    detail: str = "fresh_observation",
    count: int = 1,
    age_sec: float = 0.0,
) -> InputSourceStatus:
    status = InputSourceStatus()
    status.source_id = source
    status.modality = "image"
    status.state = state
    status.healthy = healthy
    status.epoch = epoch
    status.error_code = error_code
    status.detail = detail
    status.received_count = count
    status.accepted_count = count
    status.age_sec = age_sec
    return status


def test_unchanged_status_skips_quarter_second_ticks_and_checkpoints_at_one_second():
    gate = StatusPublicationGate()
    initial = _status("flir", MISSING, False)
    assert gate.due("flir", initial, 10.0)
    gate.commit("flir", initial, 10.0)

    for elapsed, count in ((0.25, 2), (0.5, 3), (0.75, 4)):
        diagnostic_update = _status(
            "flir",
            MISSING,
            False,
            count=count,
            age_sec=elapsed,
        )
        diagnostic_update.stamp.sec = 100 + count
        diagnostic_update.last_observation_stamp.sec = 200 + count
        assert not gate.due("flir", diagnostic_update, 10.0 + elapsed)

    checkpoint = _status("flir", MISSING, False, count=5, age_sec=1.0)
    assert gate.due("flir", checkpoint, 11.0)
    gate.commit("flir", checkpoint, 11.0)
    assert STATUS_CHECKPOINT_SEC == 1.0


def test_each_source_transition_is_due_independently_before_checkpoint():
    gate = StatusPublicationGate()
    for source in ("flir", "cam4", "vlm"):
        missing = _status(source, MISSING, False, epoch=0)
        gate.commit(source, missing, 20.0)
        recovering = _status(source, RECOVERING, False, epoch=1)
        assert gate.due(source, recovering, 20.25)
        gate.commit(source, recovering, 20.25)
        ready = _status(source, READY, True, epoch=1)
        assert gate.due(source, ready, 20.5)
        gate.commit(source, ready, 20.5)
        stale = _status(source, STALE, False, epoch=1, detail="source_stale")
        assert gate.due(source, stale, 20.75)


def test_source_identity_or_modality_miswire_is_an_immediate_edge():
    gate = StatusPublicationGate()
    ready = _status("flir", READY, True)
    gate.commit("flir", ready, 25.0)
    wrong_identity = _status("cam4", READY, True)
    assert gate.due("flir", wrong_identity, 25.25)
    wrong_modality = _status("flir", READY, True)
    wrong_modality.modality = "audio"
    assert gate.due("flir", wrong_modality, 25.25)


def test_vlm_error_and_recovery_edges_are_due_without_waiting_for_checkpoint():
    gate = StatusPublicationGate()
    ready = _status("vlm", READY, True)
    gate.commit("vlm", ready, 30.0)
    error = _status(
        "vlm",
        ERROR,
        False,
        error_code="vlm_disconnected",
        detail="provider unavailable",
    )
    assert gate.due("vlm", error, 30.25)
    gate.commit("vlm", error, 30.25)
    recovery = _status(
        "vlm",
        MISSING,
        False,
        epoch=1,
        detail="vlm_health_recovered_waiting_for_result",
    )
    assert gate.due("vlm", recovery, 30.5)


def test_publish_failure_does_not_commit_gate_and_next_tick_retries():
    node = SourceHealthMonitor.__new__(SourceHealthMonitor)
    status = _status("cam4", READY, True)
    calls = []

    class FailingPublisher:
        def publish(self, message):
            calls.append(message)
            if len(calls) == 1:
                raise RuntimeError("transport unavailable")

    class Logger:
        def error(self, _message):
            return None

    node._status_publication_gate = StatusPublicationGate()
    node._status_publishers = {"cam4": FailingPublisher()}
    node.get_logger = lambda: Logger()

    assert not node._publish_status_if_due("cam4", status, now_monotonic=40.0)
    assert node._publish_status_if_due("cam4", status, now_monotonic=40.25)
    assert not node._publish_status_if_due("cam4", status, now_monotonic=40.5)
    assert len(calls) == 2


def test_checkpoint_publishes_the_latest_diagnostic_counters():
    node = SourceHealthMonitor.__new__(SourceHealthMonitor)
    published = []
    node._status_publication_gate = StatusPublicationGate()
    node._status_publishers = {
        "flir": type(
            "Publisher",
            (),
            {"publish": lambda _self, message: published.append(message)},
        )()
    }

    initial = _status("flir", READY, True, count=1)
    assert node._publish_status_if_due("flir", initial, now_monotonic=50.0)
    quarter_tick = _status("flir", READY, True, count=2, age_sec=0.25)
    assert not node._publish_status_if_due(
        "flir", quarter_tick, now_monotonic=50.25
    )
    checkpoint = _status("flir", READY, True, count=9, age_sec=1.0)
    assert node._publish_status_if_due("flir", checkpoint, now_monotonic=51.0)

    assert [message.received_count for message in published] == [1, 9]
    assert published[-1].age_sec == 1.0


def test_internal_freshness_evaluation_remains_at_each_quarter_second_tick():
    node = SourceHealthMonitor.__new__(SourceHealthMonitor)
    tracker = SourceTracker("flir", "image", 1.0)
    snapshot_times = []
    original_snapshot = tracker.snapshot
    tracker.snapshot = lambda now: (
        snapshot_times.append(now) or original_snapshot(now)
    )
    published = []
    tick_times = iter((60.0, 60.25, 60.5, 60.75))
    node._trackers = {"flir": tracker}
    node._last_stamps = {"flir": None}
    node._status_publication_gate = StatusPublicationGate()
    node._status_publishers = {
        "flir": type(
            "Publisher",
            (),
            {"publish": lambda _self, message: published.append(message)},
        )()
    }
    node._monotonic = lambda: next(tick_times)
    node.get_clock = lambda: type(
        "Clock",
        (),
        {"now": lambda _self: type("Now", (), {"to_msg": lambda _self: Time()})()},
    )()

    for _ in range(4):
        node._publish()

    assert snapshot_times == [60.0, 60.25, 60.5, 60.75]
    assert len(published) == 1
