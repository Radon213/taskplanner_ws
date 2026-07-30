import threading
from types import SimpleNamespace

import pytest
from builtin_interfaces.msg import Time
from std_msgs.msg import String

import shadow_evaluation.interactive_replay_controller as replay_controller
from shadow_evaluation.interactive_replay_controller import (
    ElasticReplayGate,
    InteractiveReplayControllerNode,
    actual_vlm_progress,
    coalesce_stateful_records,
    parse_control_command,
    pending_vlm_source_lag_sec,
    perception_enabled_from_health,
    record_vlm_input_obligation,
    replay_drain_decision,
    replay_state_change_key,
    unresolvable_vlm_tail_count,
    vlm_completion_watermark,
    vlm_observation_id,
    vlm_obligation_progress,
    vlm_result_observation_id,
    vlm_result_slot_id,
    vlm_source_slot_id,
    vlm_watchdog_error,
)


def _decision(
    gate,
    *,
    source_time_sec,
    completed_vlm_count,
    mode="elastic_demo",
    active_skill_count=0,
    active_cleanup_count=0,
    vlm_ready=True,
):
    return gate.sync_decision(
        mode=mode,
        source_time_sec=source_time_sec,
        image_duration_sec=100.0,
        published_image_count=100,
        completed_vlm_count=completed_vlm_count,
        active_skill_count=active_skill_count,
        active_cleanup_count=active_cleanup_count,
        vlm_ready=vlm_ready,
        vlm_grace_elapsed=True,
    )


def test_soft_vlm_lag_is_observed_without_slowing_source_time():
    gate = ElasticReplayGate(
        vlm_period_sec=1.0,
        soft_lag_sec=1.0,
        hard_lag_sec=4.0,
        hard_release_lag_sec=0.5,
        min_rate_factor=0.25,
    )

    decision = _decision(
        gate,
        source_time_sec=3.2,
        completed_vlm_count=1,
    )

    assert decision.vlm_lag_sec == pytest.approx(2.0)
    assert decision.hold_reason == ""
    assert decision.playback_rate_factor == 1.0


def test_vlm_results_are_counted_by_periodic_source_slot():
    assert vlm_result_slot_id(Time(sec=1, nanosec=0), 1.0) == 1
    assert vlm_result_slot_id(Time(sec=1, nanosec=500_000_000), 1.0) == 1
    assert vlm_result_slot_id(Time(sec=2, nanosec=0), 1.0) == 2
    assert vlm_result_slot_id(Time(sec=0, nanosec=500_000_000), 1.0) is None


def test_vlm_results_are_matched_to_exact_model_input_frame():
    assert vlm_result_observation_id(
        Time(sec=1, nanosec=500_000_000)
    ) == 1_500_000_000
    assert vlm_result_observation_id(Time()) is None


def test_newer_vlm_slot_supersedes_older_stateful_backlog_holes():
    assert vlm_completion_watermark({1, 2, 4, 5}, 5) == 5
    assert vlm_completion_watermark({1, 2, 4, 5}, 3) == 2
    assert vlm_completion_watermark(set(), 5) == 0


def test_actual_frame_ledger_does_not_invent_vlm_work_inside_source_gap():
    expected = {
        slot
        for source_sec in (70.02, 71.518, 77.504)
        if (slot := vlm_source_slot_id(source_sec, 1.0)) is not None
    }

    assert expected == {70, 71, 77}
    assert actual_vlm_progress(
        expected,
        {70, 71, 72, 73, 74},
    ) == (2, 1)
    assert actual_vlm_progress(expected, {78}) == (0, 3)


def test_failed_vlm_slot_releases_replay_without_counting_as_success():
    assert vlm_obligation_progress(
        {80, 81, 82, 83, 84, 85},
        {80, 81, 82},
        {83},
    ) == (3, 1, 2)


def test_exact_obligations_do_not_hide_earlier_failed_and_pending_frames():
    assert vlm_obligation_progress(
        {80, 81, 82, 83, 84, 85, 86},
        {80, 81, 82, 86},
        {83},
    ) == (4, 1, 2)


def test_actual_frame_backlog_override_avoids_virtual_gap_deadlock():
    gate = ElasticReplayGate(
        vlm_period_sec=1.0,
        soft_lag_sec=0.5,
        hard_lag_sec=2.5,
        hard_release_lag_sec=1.0,
    )

    decision = gate.sync_decision(
        mode="elastic_demo",
        source_time_sec=77.0,
        image_duration_sec=138.4,
        published_image_count=1051,
        completed_vlm_count=74,
        active_skill_count=0,
        active_cleanup_count=0,
        vlm_ready=True,
        vlm_grace_elapsed=True,
        observed_pending_vlm_count=0,
    )

    assert decision.hold_reason == ""
    assert decision.vlm_lag_sec == 0.0
    assert decision.playback_rate_factor == 1.0


def test_pending_vlm_source_lag_uses_actual_oldest_frame_time():
    assert pending_vlm_source_lag_sec(
        source_time_sec=12.42,
        expected_slots={10, 11, 12},
        expected_slot_times={10: 10.04, 11: 11.03, 12: 12.08},
        completed_slots={10, 11},
        failed_slots=set(),
        period_sec=1.0,
    ) == pytest.approx(0.34)


def test_vlm_obligation_starts_at_actual_model_input_slot():
    expected: set[int] = set()
    times: dict[int, float] = {}

    assert record_vlm_input_obligation(
        stamp_sec=0.953,
        period_sec=1.0,
        image_duration_sec=138.4,
        expected_slots=expected,
        expected_slot_times=times,
    ) == 953_000_000
    assert expected == {953_000_000}

    assert record_vlm_input_obligation(
        stamp_sec=1.081,
        period_sec=1.0,
        image_duration_sec=138.4,
        expected_slots=expected,
        expected_slot_times=times,
    ) == 1_081_000_000
    assert expected == {953_000_000, 1_081_000_000}
    assert times[1_081_000_000] == pytest.approx(1.081)


def test_vlm_obligation_keeps_distinct_real_frames_within_same_second():
    expected: set[int] = set()
    times: dict[int, float] = {}

    for stamp_sec in (1.42, 1.08, 1.31):
        record_vlm_input_obligation(
            stamp_sec=stamp_sec,
            period_sec=1.0,
            image_duration_sec=138.4,
            expected_slots=expected,
            expected_slot_times=times,
        )

    assert expected == {
        vlm_observation_id(1.42),
        vlm_observation_id(1.08),
        vlm_observation_id(1.31),
    }
    assert sorted(times.values()) == pytest.approx([1.08, 1.31, 1.42])


def test_visual_alignment_lag_never_holds_video():
    gate = ElasticReplayGate(
        vlm_period_sec=1.0,
        max_visual_lead_sec=0.35,
    )

    aligned = gate.sync_decision(
        mode="elastic_demo",
        source_time_sec=12.3,
        image_duration_sec=100.0,
        published_image_count=100,
        completed_vlm_count=11,
        active_skill_count=0,
        active_cleanup_count=0,
        vlm_ready=True,
        vlm_grace_elapsed=True,
        observed_pending_vlm_count=1,
        observed_vlm_lag_sec=0.3,
    )
    held = gate.sync_decision(
        mode="elastic_demo",
        source_time_sec=12.4,
        image_duration_sec=100.0,
        published_image_count=100,
        completed_vlm_count=11,
        active_skill_count=0,
        active_cleanup_count=0,
        vlm_ready=True,
        vlm_grace_elapsed=True,
        observed_pending_vlm_count=1,
        observed_vlm_lag_sec=0.4,
    )

    assert aligned.hold_reason == ""
    assert held.hold_reason == ""
    assert aligned.playback_rate_factor == 1.0
    assert held.playback_rate_factor == 1.0
    assert held.vlm_lag_sec == pytest.approx(0.4)


def test_legacy_sync_thresholds_do_not_change_playback_rate():
    gate = ElasticReplayGate(vlm_period_sec=1.0)

    one_slot = _decision(
        gate,
        source_time_sec=1.2,
        completed_vlm_count=0,
    )
    two_slots = _decision(
        gate,
        source_time_sec=2.2,
        completed_vlm_count=0,
    )

    assert one_slot.hold_reason == ""
    assert one_slot.playback_rate_factor == 1.0
    assert two_slots.hold_reason == ""
    assert two_slots.playback_rate_factor == 1.0
    assert two_slots.vlm_lag_sec > one_slot.vlm_lag_sec


def test_hard_vlm_thresholds_are_observational_only():
    gate = ElasticReplayGate(
        vlm_period_sec=1.0,
        soft_lag_sec=1.0,
        hard_lag_sec=4.0,
        hard_release_lag_sec=0.5,
    )

    entered = _decision(
        gate,
        source_time_sec=5.2,
        completed_vlm_count=1,
    )
    still_held = _decision(
        gate,
        source_time_sec=5.2,
        completed_vlm_count=4,
    )
    released = _decision(
        gate,
        source_time_sec=5.2,
        completed_vlm_count=5,
    )

    assert entered.hold_reason == ""
    assert not entered.hard_hold_active
    assert still_held.hold_reason == ""
    assert not still_held.hard_hold_active
    assert released.hold_reason == ""
    assert not released.hard_hold_active
    assert entered.playback_rate_factor == 1.0
    assert still_held.playback_rate_factor == 1.0
    assert released.playback_rate_factor == 1.0


def test_skill_and_cleanup_are_causal_holds_in_every_mode():
    gate = ElasticReplayGate(require_vlm=False)

    skill = _decision(
        gate,
        source_time_sec=4.0,
        completed_vlm_count=0,
        mode="realtime_1x",
        active_skill_count=1,
    )
    cleanup = _decision(
        gate,
        source_time_sec=4.0,
        completed_vlm_count=0,
        mode="realtime_1x",
        active_cleanup_count=1,
    )

    assert skill.hold_reason == "skill_execution"
    assert cleanup.hold_reason == "cleanup_execution"


def test_require_vlm_false_runs_in_degraded_speech_only_mode():
    gate = ElasticReplayGate(require_vlm=False)

    decision = _decision(
        gate,
        source_time_sec=30.0,
        completed_vlm_count=0,
        vlm_ready=False,
    )

    assert decision.hold_reason == ""
    assert decision.degraded
    assert decision.playback_rate_factor == 1.0


def test_replay_gate_defaults_to_optional_visual_evidence():
    gate = ElasticReplayGate()

    decision = _decision(
        gate,
        source_time_sec=30.0,
        completed_vlm_count=0,
        vlm_ready=False,
    )

    assert gate.require_vlm is False
    assert decision.hold_reason == ""
    assert decision.degraded
    assert decision.playback_rate_factor == 1.0


def test_optional_vlm_backlog_remains_observable_without_holding_media():
    node = InteractiveReplayControllerNode.__new__(
        InteractiveReplayControllerNode
    )
    node._gate = ElasticReplayGate(require_vlm=False, vlm_period_sec=1.0)
    node._source_time_sec = 12.0
    node._expected_vlm_slots = {10, 11}
    node._expected_vlm_slot_times = {10: 10.0, 11: 11.0}
    node._completed_vlm_slots = {10}
    node._failed_vlm_slots = set()

    assert node._vlm_lag_sec() == pytest.approx(1.0)


def test_runtime_perception_disable_bypasses_only_vlm_sync():
    gate = ElasticReplayGate(require_vlm=True)

    degraded = gate.sync_decision(
        mode="elastic_demo",
        source_time_sec=30.0,
        image_duration_sec=100.0,
        published_image_count=100,
        completed_vlm_count=0,
        active_skill_count=0,
        active_cleanup_count=0,
        vlm_ready=False,
        vlm_grace_elapsed=True,
        require_vlm=False,
    )
    skill_hold = gate.sync_decision(
        mode="elastic_demo",
        source_time_sec=30.0,
        image_duration_sec=100.0,
        published_image_count=100,
        completed_vlm_count=0,
        active_skill_count=1,
        active_cleanup_count=0,
        vlm_ready=False,
        vlm_grace_elapsed=True,
        require_vlm=False,
    )

    assert degraded.degraded
    assert degraded.hold_reason == ""
    assert degraded.playback_rate_factor == 1.0
    assert skill_hold.hold_reason == "skill_execution"


def test_runtime_perception_disable_keeps_vlm_sync_required() -> None:
    node = InteractiveReplayControllerNode.__new__(
        InteractiveReplayControllerNode
    )
    node._gate = ElasticReplayGate(require_vlm=True)
    node._perception_enabled = False

    assert node._vlm_required() is True


def test_perception_health_toggle_requires_the_expected_schema():
    enabled = String()
    enabled.data = (
        '{"schema":"taskplanner.rfdetr_health.v1","enabled":true}'
    )
    disabled = String()
    disabled.data = (
        '{"schema":"taskplanner.rfdetr_health.v1","enabled":false}'
    )
    unrelated = String()
    unrelated.data = '{"schema":"other","enabled":false}'

    assert perception_enabled_from_health(enabled) is True
    assert perception_enabled_from_health(disabled) is False
    assert perception_enabled_from_health(unrelated) is None


def test_vlm_startup_health_is_observed_without_clock_gate():
    gate = ElasticReplayGate(
        require_vlm=True,
        soft_lag_sec=1.0,
        hard_lag_sec=2.0,
    )

    decision = gate.sync_decision(
        mode="elastic_demo",
        source_time_sec=6.0,
        image_duration_sec=100.0,
        published_image_count=100,
        completed_vlm_count=0,
        active_skill_count=0,
        active_cleanup_count=0,
        vlm_ready=False,
        vlm_grace_elapsed=False,
    )

    assert decision.hold_reason == ""
    assert decision.playback_rate_factor == 1.0
    assert decision.degraded


def test_vlm_watchdog_reports_missing_input_and_response_for_metadata():
    input_error = vlm_watchdog_error(
        require_vlm=True,
        image_input_active=True,
        timeout_sec=10.0,
        source_time_sec=2.01,
        pending_vlm_count=0,
        hold_reason="",
        no_input_elapsed_sec=10.1,
        response_wait_elapsed_sec=None,
        last_input_error="missing segmented FLIR",
    )
    response_error = vlm_watchdog_error(
        require_vlm=True,
        image_input_active=True,
        timeout_sec=10.0,
        source_time_sec=8.0,
        pending_vlm_count=4,
        hold_reason="vlm_backlog",
        no_input_elapsed_sec=None,
        response_wait_elapsed_sec=10.0,
        last_input_error="",
    )
    degraded = vlm_watchdog_error(
        require_vlm=False,
        image_input_active=True,
        timeout_sec=10.0,
        source_time_sec=8.0,
        pending_vlm_count=4,
        hold_reason="vlm_backlog",
        no_input_elapsed_sec=100.0,
        response_wait_elapsed_sec=100.0,
        last_input_error="missing segmented FLIR",
    )

    assert "VLM input unavailable" in input_error
    assert "VLM response timeout" in response_error
    assert degraded == ""


def test_runtime_tick_records_vlm_timeout_without_blocking_media_clock(
    monkeypatch,
):
    node = InteractiveReplayControllerNode.__new__(
        InteractiveReplayControllerNode
    )
    node._lock = threading.RLock()
    node._wall_elapsed_sec = 0.0
    node._last_tick_at = 9.0
    node._state = "running"
    node._source_time_sec = 1.0
    node._source = SimpleNamespace(
        duration_sec=100.0,
        exhausted=lambda: False,
    )
    node._image_duration_sec = 100.0
    node._published_image_count = 10
    node._vlm_startup_grace_sec = 0.0
    node._gate = ElasticReplayGate(require_vlm=True)
    node._mode = "elastic_demo"
    node._playback_rate = 1.0
    node._effective_playback_rate = 0.0
    node._elastic_hold_sec = 0.0
    node._hold_reason = ""
    node._last_vlm_health_error = ""
    node._force_state_publish = False
    node._publish_due_records = lambda: None
    node._publish_clock = lambda: None
    node._completed_vlm_count = lambda: 0
    node._active_non_cleanup_count = lambda: 0
    node._active_cleanup_count = lambda: 0
    node._vlm_ready_for_sync = lambda now: False
    node._pending_vlm_count = lambda: 10
    node._vlm_lag_sec = lambda: 10.0
    node._vlm_required = lambda: True
    node._vlm_timeout_error = (
        lambda **kwargs: "VLM response timeout (observation only)"
    )
    node._block = lambda *args, **kwargs: pytest.fail(
        "VLM timeout must not block replay"
    )

    monkeypatch.setattr(replay_controller.time, "monotonic", lambda: 10.0)
    node._tick()

    assert node._state == "running"
    assert node._source_time_sec == pytest.approx(1.25)
    assert node._effective_playback_rate == 1.0
    assert (
        node._last_vlm_health_error
        == "VLM response timeout (observation only)"
    )
    assert node._force_state_publish


def test_stateful_records_coalesce_but_speech_events_are_lossless():
    records = [
        ("/cam4", b"frame-1", 1.0),
        ("/speech", b"first", 1.1),
        ("/cam4", b"frame-2", 1.2),
        ("/perception", b"old", 1.3),
        ("/speech", b"second", 1.4),
        ("/perception", b"new", 1.5),
    ]

    kept, coalesced = coalesce_stateful_records(
        records,
        stateful_topics={"/cam4", "/perception"},
    )

    assert kept == [
        ("/speech", b"first", 1.1),
        ("/cam4", b"frame-2", 1.2),
        ("/speech", b"second", 1.4),
        ("/perception", b"new", 1.5),
    ]
    assert coalesced == 2


def test_media_end_drain_waits_then_marks_timeout_explicitly():
    waiting = replay_drain_decision(
        require_vlm=True,
        pending_vlm_count=2,
        active_skill_count=1,
        active_cleanup_count=1,
        elapsed_sec=2.0,
        timeout_sec=10.0,
    )
    timed_out = replay_drain_decision(
        require_vlm=True,
        pending_vlm_count=2,
        active_skill_count=1,
        active_cleanup_count=1,
        elapsed_sec=10.0,
        timeout_sec=10.0,
    )
    degraded_complete = replay_drain_decision(
        require_vlm=False,
        pending_vlm_count=99,
        active_skill_count=0,
        active_cleanup_count=0,
        elapsed_sec=0.0,
        timeout_sec=10.0,
    )
    vlm_only_complete = replay_drain_decision(
        require_vlm=True,
        pending_vlm_count=99,
        active_skill_count=0,
        active_cleanup_count=0,
        elapsed_sec=0.0,
        timeout_sec=10.0,
    )

    assert not waiting.completed
    assert waiting.hold_reason == "draining:skill+cleanup"
    assert timed_out.completed
    assert timed_out.timed_out
    assert timed_out.hold_reason == "drain_timeout"
    assert timed_out.pending_labels == ("skill", "cleanup")
    assert degraded_complete.completed
    assert not degraded_complete.timed_out
    assert vlm_only_complete.completed
    assert not vlm_only_complete.timed_out
    assert vlm_only_complete.pending_labels == ()


def test_media_end_marks_only_expired_unresolvable_vlm_tail_slots():
    assert (
        unresolvable_vlm_tail_count(
            require_vlm=True,
            image_input_active=False,
            pending_vlm_count=2,
            drain_elapsed_sec=69.9,
            grace_sec=70.0,
        )
        == 0
    )
    assert (
        unresolvable_vlm_tail_count(
            require_vlm=True,
            image_input_active=False,
            pending_vlm_count=2,
            drain_elapsed_sec=70.0,
            grace_sec=70.0,
        )
        == 2
    )
    assert (
        unresolvable_vlm_tail_count(
            require_vlm=True,
            image_input_active=True,
            pending_vlm_count=2,
            drain_elapsed_sec=100.0,
            grace_sec=70.0,
        )
        == 0
    )
    assert (
        unresolvable_vlm_tail_count(
            require_vlm=False,
            image_input_active=False,
            pending_vlm_count=2,
            drain_elapsed_sec=100.0,
            grace_sec=70.0,
        )
        == 0
    )


def test_state_publish_key_ignores_clock_churn_but_tracks_semantics():
    base = {
        "run_id": "run-a",
        "state": "running",
        "mode": "elastic_demo",
        "source_time_sec": 1.0,
        "wall_elapsed_sec": 2.0,
        "published_image_count": 30,
        "pending_vlm_count": 0,
        "active_skill_count": 0,
    }
    clock_only = {
        **base,
        "source_time_sec": 1.5,
        "wall_elapsed_sec": 2.5,
        "published_image_count": 45,
    }
    held = {**clock_only, "state": "held", "hold_reason": "skill_execution"}

    assert replay_state_change_key(base) == replay_state_change_key(
        clock_only
    )
    assert replay_state_change_key(base) != replay_state_change_key(held)


def test_pause_and_stop_accept_optional_traceable_reason():
    assert parse_control_command("pause") == ("pause", "")
    assert parse_control_command("pause|operator inspection") == (
        "pause",
        "operator inspection",
    )
    assert parse_control_command("stop:provider failure") == (
        "stop",
        "provider failure",
    )
