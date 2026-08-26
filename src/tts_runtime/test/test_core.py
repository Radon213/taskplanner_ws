from __future__ import annotations

from pathlib import Path
import sqlite3
import stat
import struct
import threading
import time
import wave

import pytest

from tts_runtime.core import (
    DeterministicWavCache,
    PlaybackDispatcher,
    PlaybackResult,
    PlaybackStore,
    ReplyRequest,
    RuntimeConfig,
    SynthesisResult,
    cache_key_for,
    supertonic_model_manifest_sha256,
)


class FakeSynthesizer:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, int]] = []
        self.closed = False

    def synthesize(self, text: str, output_path: Path, seed: int) -> SynthesisResult:
        self.calls.append((text, seed))
        if self.fail:
            raise RuntimeError("fake synthesis failure")
        sample_rate = 8_000
        frames = 800
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(output_path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(sample_rate)
            handle.writeframes(struct.pack("<h", 1000) * frames)
        return SynthesisResult(synth_latency_ms=12.5, audio_duration_sec=0.1)

    def close(self) -> None:
        self.closed = True


class FakePlayer:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[Path] = []
        self._active = 0
        self.max_active = 0
        self._lock = threading.Lock()
        self._armed = False
        self._cancelled = False
        self.interrupt_calls = 0

    def arm(self) -> None:
        with self._lock:
            if self._armed:
                raise RuntimeError("fake playback already armed")
            self._armed = True
            self._cancelled = False

    def interrupt(self) -> bool:
        with self._lock:
            if not self._armed:
                return False
            self._cancelled = True
            self.interrupt_calls += 1
            return True

    def play(self, wav_path: Path) -> PlaybackResult:
        with self._lock:
            if not self._armed:
                self._armed = True
                self._cancelled = False
            if self._cancelled:
                self._armed = False
                self._cancelled = False
                raise RuntimeError("fake playback interrupted before start")
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        try:
            self.calls.append(wav_path)
            if self.fail:
                raise RuntimeError("fake playback failure")
            time.sleep(0.005)
            with self._lock:
                if self._cancelled:
                    raise RuntimeError("fake playback interrupted")
            return PlaybackResult(playback_latency_ms=5.0)
        finally:
            with self._lock:
                self._active -= 1
                self._armed = False
                self._cancelled = False


class BlockingSynthesizer(FakeSynthesizer):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def synthesize(self, text: str, output_path: Path, seed: int) -> SynthesisResult:
        self.started.set()
        if not self.release.wait(timeout=2.0):
            raise TimeoutError("test did not release synthesis")
        return super().synthesize(text, output_path, seed)


class ScopeBlockingPlayer:
    """Fake audible player whose first attempt blocks until interrupted."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._armed = False
        self._cancelled = False
        self._attempt = 0
        self.first_started = threading.Event()
        self._release_first = threading.Event()
        self.started_paths: list[Path] = []
        self.completed_paths: list[Path] = []
        self.interrupt_calls = 0

    def arm(self) -> None:
        with self._lock:
            if self._armed:
                raise RuntimeError("scope fake already armed")
            self._armed = True
            self._cancelled = False
            self._attempt += 1

    def interrupt(self) -> bool:
        with self._lock:
            if not self._armed:
                return False
            self._cancelled = True
            self.interrupt_calls += 1
            self._release_first.set()
            return True

    def play(self, wav_path: Path) -> PlaybackResult:
        with self._lock:
            attempt = self._attempt
            if self._cancelled:
                self._armed = False
                self._cancelled = False
                raise RuntimeError("scope fake cancelled before spawn")
            self.started_paths.append(wav_path)
        if attempt == 1:
            self.first_started.set()
            if not self._release_first.wait(timeout=2.0):
                raise TimeoutError("test did not release first playback")
        with self._lock:
            cancelled = self._cancelled
            self._armed = False
            self._cancelled = False
        if cancelled:
            raise RuntimeError("scope fake interrupted")
        self.completed_paths.append(wav_path)
        return PlaybackResult(playback_latency_ms=5.0)


def request(
    reply_id: str,
    *,
    text: str = "안녕하세요",
    timing: str = "immediate",
) -> ReplyRequest:
    return ReplyRequest(
        reply_id=reply_id,
        turn_id=f"turn:{reply_id}",
        utterance_id=f"utterance:{reply_id}",
        gateway_instance_id="gateway-1",
        procedure_run_id="run-1",
        text=text,
        timing=timing,
        function_call_name=(
            "request_tool_handover" if timing != "immediate" else ""
        ),
        function_arguments_json=(
            '{"tool_id":"T07"}' if timing != "immediate" else ""
        ),
        function_request_id=f"function:{reply_id}" if timing != "immediate" else "",
    )


def runtime(tmp_path: Path, *, synth=None, player=None, events=None):
    emitted = [] if events is None else events
    synth = FakeSynthesizer() if synth is None else synth
    player = FakePlayer() if player is None else player
    store = PlaybackStore(tmp_path / "state.sqlite3")
    dispatcher = PlaybackDispatcher(
        store=store,
        cache=DeterministicWavCache(tmp_path / "cache"),
        synthesizer=synth,
        player=player,
        config=RuntimeConfig(model_identity="test-model"),
        on_event=emitted.append,
    )
    dispatcher.set_active_procedure_scope("gateway-1", "run-1")
    return dispatcher, store, synth, player, emitted


def states(events, reply_id: str) -> list[str]:
    return [event.state for event in events if event.reply_id == reply_id]


def test_immediate_reply_runs_one_ordered_terminal_sequence(tmp_path: Path) -> None:
    dispatcher, store, synth, player, events = runtime(tmp_path)
    dispatcher.start()
    result = dispatcher.submit(request("r1"))
    dispatcher.wait_idle()

    assert result.accepted is True
    assert states(events, "r1") == ["queued", "playing", "played"]
    assert [event.sequence for event in events] == sorted(
        event.sequence for event in events
    )
    assert events[-1].terminal is True
    assert events[-1].success is True
    assert events[-1].synth_latency_ms == pytest.approx(12.5)
    assert events[-1].audio_duration_sec == pytest.approx(0.1)
    assert events[-1].playback_latency_ms == pytest.approx(5.0)
    assert len(synth.calls) == 1
    assert len(player.calls) == 1
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(player.calls[0].parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(player.calls[0].stat().st_mode) == 0o600
    dispatcher.close()


def test_reply_id_primary_key_deduplicates_redelivery(tmp_path: Path) -> None:
    dispatcher, _store, synth, player, events = runtime(tmp_path)
    dispatcher.start()
    first = dispatcher.submit(request("same"))
    duplicate = dispatcher.submit(request("same"))
    dispatcher.wait_idle()

    assert first.accepted and not first.duplicate
    assert not duplicate.accepted and duplicate.duplicate
    assert states(events, "same") == [
        "queued",
        "duplicate_suppressed",
        "playing",
        "played",
    ]
    assert duplicate.event.state == "duplicate_suppressed"
    assert len(synth.calls) == 1
    assert len(player.calls) == 1
    dispatcher.close()


@pytest.mark.parametrize(
    "logical_override",
    [
        {"procedure_run_id": "run-other"},
        {"gateway_instance_id": "gateway-other"},
        {"utterance_id": "utterance-other"},
    ],
)
def test_reply_id_collision_with_different_logical_turn_is_rejected(
    tmp_path: Path,
    logical_override: dict[str, str],
) -> None:
    dispatcher, store, _synth, _player, _events = runtime(tmp_path)
    first = request("collision", text="첫 문장")
    store.insert(
        first,
        dispatcher.config,
        cache_key_for(first.text, dispatcher.config),
    )
    with pytest.raises(ValueError, match="reply_id collision"):
        changed = ReplyRequest(
            **{
                **request("collision", text="다른 문장").__dict__,
                **logical_override,
            }
        )
        store.insert(
            changed,
            dispatcher.config,
            cache_key_for(changed.text, dispatcher.config),
        )
    dispatcher.close()


def test_rejected_request_emits_correlated_terminal_nack_without_job(
    tmp_path: Path,
) -> None:
    dispatcher, store, _synth, _player, events = runtime(tmp_path)
    rejected = request("rejected-before-insert", text="재생하면 안 되는 문장")

    event = dispatcher.reject(
        rejected,
        error_code="invalid_reply",
        message="tts_reply_rejected_before_playback",
    )

    assert event.reply_id == rejected.reply_id
    assert event.utterance_id == rejected.utterance_id
    assert event.gateway_instance_id == rejected.gateway_instance_id
    assert event.procedure_run_id == rejected.procedure_run_id
    assert event.state == "failed"
    assert event.terminal is True
    assert event.success is False
    assert event.error_code == "invalid_reply"
    assert store.get(rejected.reply_id) is None
    assert events == [event]
    dispatcher.close()


def test_restart_wording_change_is_first_writer_wins_duplicate(
    tmp_path: Path,
) -> None:
    dispatcher, store, _synth, player, events = runtime(tmp_path)
    dispatcher.start()
    first = request("stable", text="첫 번째 응답")
    dispatcher.submit(first)
    dispatcher.wait_idle()
    restarted = ReplyRequest(
        **{
            **first.__dict__,
            "turn_id": "restart-epoch:run-1:utterance:stable",
            "text": "재시작 후 달라진 응답",
        }
    )
    duplicate = dispatcher.submit(restarted)
    dispatcher.wait_idle()

    assert duplicate.duplicate
    assert duplicate.event.state == "duplicate_suppressed"
    assert len(player.calls) == 1
    assert store.get("stable").text == "첫 번째 응답"
    assert states(events, "stable") == [
        "queued",
        "playing",
        "played",
        "duplicate_suppressed",
    ]
    dispatcher.close()


def test_content_cache_reuses_wav_for_new_reply_id(tmp_path: Path) -> None:
    dispatcher, _store, synth, player, events = runtime(tmp_path)
    dispatcher.start()
    dispatcher.submit(request("r1", text="동일 문장"))
    dispatcher.wait_idle()
    dispatcher.submit(request("r2", text="동일 문장"))
    dispatcher.wait_idle()

    assert len(synth.calls) == 1
    assert len(player.calls) == 2
    r2_playing = next(
        event for event in events if event.reply_id == "r2" and event.state == "playing"
    )
    assert r2_playing.synth_latency_ms == 0
    assert r2_playing.message == "playing_cached_wav"
    assert player.calls[0] == player.calls[1]
    dispatcher.close()


def test_prewarm_is_silent_and_second_start_is_all_cache_hits(
    tmp_path: Path,
) -> None:
    texts = ["바이폴라 전달드리겠습니다", "도구 회수중입니다"]
    first, first_store, first_synth, first_player, first_events = runtime(tmp_path)
    warmed = first.prewarm([*texts, texts[0], ""])
    assert [result.text for result in warmed] == texts
    assert all(not result.cache_hit for result in warmed)
    assert len(first_synth.calls) == 2
    assert not first_player.calls
    assert not first_events
    assert first_store.list_waiting() == []
    first.close()

    second_synth = FakeSynthesizer()
    second_player = FakePlayer()
    second = PlaybackDispatcher(
        store=PlaybackStore(tmp_path / "second.sqlite3"),
        cache=DeterministicWavCache(tmp_path / "cache"),
        synthesizer=second_synth,
        player=second_player,
        config=RuntimeConfig(model_identity="test-model"),
    )
    cached = second.prewarm(texts)
    assert all(result.cache_hit for result in cached)
    assert all(result.synth_latency_ms == 0 for result in cached)
    assert not second_synth.calls
    assert not second_player.calls
    second.close()


@pytest.mark.parametrize(
    "timing", ["on_function_accepted", "on_function_completed"]
)
def test_admission_timing_waits_durably_until_release(
    tmp_path: Path, timing: str
) -> None:
    dispatcher, store, synth, player, events = runtime(tmp_path)
    dispatcher.start()
    dispatcher.submit(request("gated", timing=timing))
    dispatcher.wait_idle()

    waiting_state = f"waiting_function_{timing.removeprefix('on_function_')}"
    assert states(events, "gated") == [waiting_state]
    stored = store.get("gated")
    assert stored.state == waiting_state
    assert stored.function_call_name == "request_tool_handover"
    assert stored.function_arguments_json == '{"tool_id":"T07"}'
    assert store.list_waiting() == [stored]
    assert not synth.calls
    assert not player.calls

    released = dispatcher.release_waiting("gated")
    dispatcher.wait_idle()
    assert released is not None
    assert states(events, "gated") == [
        waiting_state,
        "queued",
        "playing",
        "played",
    ]
    assert dispatcher.release_waiting("gated") is None
    assert len(player.calls) == 1
    dispatcher.close()


def test_crash_recovery_requeues_only_safe_work(tmp_path: Path) -> None:
    config = RuntimeConfig(model_identity="test-model")
    database = tmp_path / "state.sqlite3"
    first_store = PlaybackStore(database)
    for reply_id, timing in (
        ("queued", "immediate"),
        ("waiting", "on_function_accepted"),
        ("ambiguous", "immediate"),
    ):
        item = request(reply_id, timing=timing)
        first_store.insert(item, config, cache_key_for(item.text, config))
    playing = first_store.transition(
        "ambiguous",
        expected_state="queued",
        state="playing",
        message="simulated_crash_during_playback",
    )
    assert playing is not None
    first_store.close()

    events = []
    synth = FakeSynthesizer()
    player = FakePlayer()
    dispatcher = PlaybackDispatcher(
        store=PlaybackStore(database),
        cache=DeterministicWavCache(tmp_path / "cache"),
        synthesizer=synth,
        player=player,
        config=config,
        on_event=events.append,
    )
    dispatcher.set_active_procedure_scope("gateway-1", "run-1")
    dispatcher.start()
    dispatcher.wait_idle()

    assert dispatcher.store.get("queued").state == "played"
    assert dispatcher.store.get("waiting").state == "waiting_function_accepted"
    interrupted = dispatcher.store.get("ambiguous")
    assert interrupted.state == "failed"
    assert interrupted.terminal is True
    assert interrupted.success is False
    assert interrupted.error_code == "interrupted_unknown"
    assert len(player.calls) == 1
    assert states(events, "queued") == ["queued", "playing", "played"]
    assert states(events, "waiting") == ["waiting_function_accepted"]
    assert states(events, "ambiguous") == ["failed"]
    dispatcher.close()


def test_synthesis_failure_is_terminal_and_not_played(tmp_path: Path) -> None:
    dispatcher, store, _synth, player, events = runtime(
        tmp_path, synth=FakeSynthesizer(fail=True)
    )
    dispatcher.start()
    dispatcher.submit(request("synth-fail"))
    dispatcher.wait_idle()

    failed = store.get("synth-fail")
    assert states(events, "synth-fail") == ["queued", "failed"]
    assert failed.error_code == "synthesis_failed"
    assert failed.terminal and not failed.success
    assert not player.calls
    dispatcher.close()


def test_waiting_timeout_is_terminal_and_cannot_be_released(tmp_path: Path) -> None:
    dispatcher, store, synth, player, events = runtime(tmp_path)
    dispatcher.start()
    dispatcher.submit(request("timeout", timing="on_function_accepted"))
    expired = store.expire_waiting(
        timeout_sec=10.0,
        now_unix_sec=time.time() + 11.0,
    )

    assert [event.reply_id for event in expired] == ["timeout"]
    failed = store.get("timeout")
    assert failed.state == "failed"
    assert failed.error_code == "waiting_timeout"
    assert failed.terminal and not failed.success
    assert dispatcher.release_waiting("timeout") is None
    assert not synth.calls
    assert not player.calls
    dispatcher.close()


def test_scope_change_terminalizes_only_other_run_waiting(tmp_path: Path) -> None:
    dispatcher, store, _synth, _player, _events = runtime(tmp_path)
    dispatcher.start()
    current = request("current", timing="on_function_accepted")
    stale = ReplyRequest(
        **{**request("stale", timing="on_function_accepted").__dict__, "procedure_run_id": "run-old"}
    )
    dispatcher.submit(current)
    store.insert(
        stale,
        dispatcher.config,
        cache_key_for(stale.text, dispatcher.config),
    )

    failed = store.fail_waiting_outside_run("run-1")
    assert [event.reply_id for event in failed] == ["stale"]
    assert store.get("current").state == "waiting_function_accepted"
    assert store.get("stale").state == "failed"
    assert store.get("stale").error_code == "stale_procedure_run"
    dispatcher.close()


def test_startup_scope_cleanup_fails_stale_queued_and_waiting(tmp_path: Path) -> None:
    store = PlaybackStore(tmp_path / "scope.sqlite3")
    config = RuntimeConfig(model_identity="test-model")
    items = [
        request("current-queued"),
        ReplyRequest(
            **{**request("old-queued").__dict__, "procedure_run_id": "run-old"}
        ),
        ReplyRequest(
            **{
                **request("old-waiting", timing="on_function_accepted").__dict__,
                "procedure_run_id": "run-old",
            }
        ),
    ]
    for item in items:
        store.insert(item, config, cache_key_for(item.text, config))

    failed = store.fail_pending_outside_run("run-1")
    assert [event.reply_id for event in failed] == [
        "old-queued",
        "old-waiting",
    ]
    assert store.get("current-queued").state == "queued"
    assert store.get("old-queued").error_code == "stale_procedure_run"
    assert store.get("old-waiting").error_code == "stale_procedure_run"
    store.close()


def test_no_worker_before_scope_and_unstarted_shutdown_are_safe(tmp_path: Path) -> None:
    synth = FakeSynthesizer()
    player = FakePlayer()
    store = PlaybackStore(tmp_path / "state.sqlite3")
    dispatcher = PlaybackDispatcher(
        store=store,
        cache=DeterministicWavCache(tmp_path / "cache"),
        synthesizer=synth,
        player=player,
        config=RuntimeConfig(model_identity="test-model"),
    )
    with pytest.raises(ValueError, match="active playback scope"):
        dispatcher.submit(request("pre-scope"))
    with pytest.raises(RuntimeError, match="must be set"):
        dispatcher.start()
    assert store.get("pre-scope") is None
    assert not synth.calls
    assert not player.calls
    dispatcher.close()
    assert synth.closed is True


def test_interrupted_playing_can_be_terminalized_before_worker_start(
    tmp_path: Path,
) -> None:
    store = PlaybackStore(tmp_path / "interrupted.sqlite3")
    config = RuntimeConfig(model_identity="test-model")
    item = request("interrupted")
    store.insert(item, config, cache_key_for(item.text, config))
    store.transition(
        item.reply_id,
        expected_state="queued",
        state="playing",
        message="simulated_process_exit",
    )

    failed = store.fail_interrupted_playing()
    assert [event.reply_id for event in failed] == ["interrupted"]
    assert failed[0].state == "failed"
    assert failed[0].error_code == "interrupted_unknown"
    assert failed[0].terminal and not failed[0].success
    store.close()


def test_scope_change_during_synthesis_never_reaches_player(tmp_path: Path) -> None:
    synth = BlockingSynthesizer()
    dispatcher, store, _synth, player, events = runtime(tmp_path, synth=synth)
    dispatcher.start()
    dispatcher.submit(request("synth-race", text="합성 중인 이전 수술 대사"))
    assert synth.started.wait(timeout=1.0)

    dispatcher.set_active_procedure_run("run-2")
    synth.release.set()
    dispatcher.wait_idle()

    failed = store.get("synth-race")
    assert failed.state == "failed"
    assert failed.error_code == "stale_procedure_run"
    assert failed.terminal and not failed.success
    assert not player.calls
    assert states(events, "synth-race") == ["queued", "failed"]
    dispatcher.close()


def test_scope_switch_interrupts_playing_fails_pending_and_allows_new_run(
    tmp_path: Path,
) -> None:
    player = ScopeBlockingPlayer()
    dispatcher, store, _synth, _player, events = runtime(
        tmp_path,
        player=player,
    )
    dispatcher.start()
    dispatcher.submit(request("old-playing", text="이전 수술 재생 중"))
    assert player.first_started.wait(timeout=1.0)
    dispatcher.submit(request("old-queued", text="이전 수술 대기 중"))
    dispatcher.submit(
        request(
            "old-waiting",
            text="이전 수술 승인 대기 중",
            timing="on_function_accepted",
        )
    )

    dispatcher.set_active_procedure_run("run-2")
    new_request = ReplyRequest(
        **{
            **request("new-run", text="새 수술 대사").__dict__,
            "procedure_run_id": "run-2",
        }
    )
    dispatcher.submit(new_request)
    dispatcher.wait_idle()

    interrupted = store.get("old-playing")
    assert interrupted.state == "failed"
    assert interrupted.error_code == "procedure_scope_changed"
    assert interrupted.terminal and not interrupted.success
    for reply_id in ("old-queued", "old-waiting"):
        stale = store.get(reply_id)
        assert stale.state == "failed"
        assert stale.error_code == "stale_procedure_run"
        assert stale.terminal and not stale.success
    assert store.get("new-run").state == "played"
    assert player.interrupt_calls == 1
    assert len(player.started_paths) == 2
    assert len(player.completed_paths) == 1
    assert states(events, "old-playing") == ["queued", "playing", "failed"]
    assert states(events, "new-run") == ["queued", "playing", "played"]
    assert [event.sequence for event in events] == sorted(
        event.sequence for event in events
    )
    dispatcher.close()


def test_same_run_new_gateway_fences_every_old_epoch_playback_state(
    tmp_path: Path,
) -> None:
    player = ScopeBlockingPlayer()
    dispatcher, store, _synth, _player, events = runtime(
        tmp_path,
        player=player,
    )
    dispatcher.start()
    dispatcher.submit(request("old-gateway-playing", text="이전 게이트웨이 재생 중"))
    assert player.first_started.wait(timeout=1.0)
    dispatcher.submit(request("old-gateway-queued", text="이전 게이트웨이 큐"))
    dispatcher.submit(
        request(
            "old-gateway-waiting",
            text="이전 게이트웨이 승인 대기",
            timing="on_function_accepted",
        )
    )

    dispatcher.set_active_procedure_scope("gateway-2", "run-1")
    current = ReplyRequest(
        **{
            **request("new-gateway", text="새 게이트웨이 대사").__dict__,
            "gateway_instance_id": "gateway-2",
        }
    )
    dispatcher.submit(current)
    dispatcher.wait_idle()

    interrupted = store.get("old-gateway-playing")
    assert interrupted.state == "failed"
    assert interrupted.error_code == "procedure_scope_changed"
    for reply_id in ("old-gateway-queued", "old-gateway-waiting"):
        stale = store.get(reply_id)
        assert stale.state == "failed"
        assert stale.error_code == "stale_gateway_scope"
        assert stale.gateway_instance_id == "gateway-1"
    assert store.get("new-gateway").state == "played"
    assert store.get("new-gateway").gateway_instance_id == "gateway-2"
    assert player.interrupt_calls == 1
    assert len(player.started_paths) == 2
    assert len(player.completed_paths) == 1
    assert states(events, "new-gateway") == ["queued", "playing", "played"]
    dispatcher.close()


def test_recovery_never_replays_other_gateway_with_reused_run_id(
    tmp_path: Path,
) -> None:
    database = tmp_path / "epoch-recovery.sqlite3"
    config = RuntimeConfig(model_identity="test-model")
    first_store = PlaybackStore(database)
    old_queued = request("old-recovery-queued")
    old_waiting = request(
        "old-recovery-waiting", timing="on_function_completed"
    )
    current = ReplyRequest(
        **{
            **request("current-recovery").__dict__,
            "gateway_instance_id": "gateway-2",
        }
    )
    for item in (old_queued, old_waiting, current):
        first_store.insert(item, config, cache_key_for(item.text, config))
    first_store.close()

    events: list = []
    player = FakePlayer()
    dispatcher = PlaybackDispatcher(
        store=PlaybackStore(database),
        cache=DeterministicWavCache(tmp_path / "epoch-cache"),
        synthesizer=FakeSynthesizer(),
        player=player,
        config=config,
        on_event=events.append,
    )
    dispatcher.set_active_procedure_scope("gateway-2", "run-1")
    dispatcher.start()
    dispatcher.wait_idle()

    for reply_id in ("old-recovery-queued", "old-recovery-waiting"):
        stale = dispatcher.store.get(reply_id)
        assert stale.state == "failed"
        assert stale.error_code == "stale_gateway_scope"
    assert dispatcher.store.get("current-recovery").state == "played"
    assert len(player.calls) == 1
    assert states(events, "old-recovery-queued") == ["failed"]
    assert states(events, "old-recovery-waiting") == ["failed"]
    dispatcher.close()


def test_store_recovery_returns_only_exact_gateway_and_run_queue_ids(
    tmp_path: Path,
) -> None:
    store = PlaybackStore(tmp_path / "direct-recovery.sqlite3")
    config = RuntimeConfig(model_identity="test-model")
    old_epoch = request("direct-old")
    current_epoch = ReplyRequest(
        **{
            **request("direct-current").__dict__,
            "gateway_instance_id": "gateway-2",
        }
    )
    for item in (old_epoch, current_epoch):
        store.insert(item, config, cache_key_for(item.text, config))

    recovered, queued_ids = store.recover(
        gateway_instance_id="gateway-2",
        procedure_run_id="run-1",
    )

    assert queued_ids == ["direct-current"]
    assert [(event.reply_id, event.state) for event in recovered] == [
        ("direct-old", "failed"),
        ("direct-current", "queued"),
    ]
    assert store.get("direct-old").error_code == "stale_gateway_scope"
    assert store.get("direct-current").message == "recovered_queued"
    store.close()


def test_legacy_database_rows_without_gateway_epoch_are_migrated_fail_closed(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy.sqlite3"
    config = RuntimeConfig(model_identity="test-model")
    legacy_store = PlaybackStore(database)
    item = request("legacy-row")
    legacy_store.insert(item, config, cache_key_for(item.text, config))
    legacy_store.close()
    with sqlite3.connect(database) as connection:
        connection.execute("DROP INDEX playback_jobs_scope_state_idx")
        connection.execute(
            "ALTER TABLE playback_jobs DROP COLUMN gateway_instance_id"
        )
        connection.execute("PRAGMA user_version=1")

    migrated = PlaybackStore(database)
    columns = {
        str(row[1])
        for row in migrated._connection.execute(
            "PRAGMA table_info(playback_jobs)"
        ).fetchall()
    }
    assert "gateway_instance_id" in columns
    assert migrated.get("legacy-row").gateway_instance_id == ""
    assert migrated._connection.execute("PRAGMA user_version").fetchone()[0] == 2
    migrated.close()

    player = FakePlayer()
    dispatcher = PlaybackDispatcher(
        store=PlaybackStore(database),
        cache=DeterministicWavCache(tmp_path / "legacy-cache"),
        synthesizer=FakeSynthesizer(),
        player=player,
        config=config,
    )
    dispatcher.set_active_procedure_scope("gateway-2", "run-1")
    dispatcher.start()
    dispatcher.wait_idle()

    stale = dispatcher.store.get("legacy-row")
    assert stale.state == "failed"
    assert stale.error_code == "stale_gateway_scope"
    assert not player.calls
    dispatcher.close()


def test_idle_scope_switch_does_not_poison_next_playback(tmp_path: Path) -> None:
    dispatcher, store, _synth, player, _events = runtime(tmp_path)
    dispatcher.start()

    dispatcher.set_active_procedure_run("run-2")
    assert player.interrupt_calls == 0
    with pytest.raises(ValueError, match="active playback scope"):
        dispatcher.submit(request("old-after-switch"))
    next_request = ReplyRequest(
        **{
            **request("new-after-idle-switch").__dict__,
            "procedure_run_id": "run-2",
        }
    )
    dispatcher.submit(next_request)
    dispatcher.wait_idle()

    assert store.get("new-after-idle-switch").state == "played"
    assert len(player.calls) == 1
    dispatcher.set_active_procedure_run("")
    assert player.interrupt_calls == 0
    with pytest.raises(ValueError, match="active playback scope"):
        dispatcher.submit(next_request)
    dispatcher.close()


def test_shutdown_fences_pending_and_interrupts_current_playback(
    tmp_path: Path,
) -> None:
    player = ScopeBlockingPlayer()
    dispatcher, store, _synth, _player, events = runtime(
        tmp_path,
        player=player,
    )
    dispatcher.start()
    dispatcher.submit(request("shutdown-playing", text="종료 시 재생 중"))
    assert player.first_started.wait(timeout=1.0)
    dispatcher.submit(request("shutdown-queued", text="종료 시 큐 대기"))
    dispatcher.submit(
        request(
            "shutdown-waiting",
            text="종료 시 승인 대기",
            timing="on_function_completed",
        )
    )

    dispatcher.stop(timeout_sec=1.0)

    playing = store.get("shutdown-playing")
    assert playing.state == "failed"
    assert playing.error_code == "procedure_scope_changed"
    for reply_id in ("shutdown-queued", "shutdown-waiting"):
        pending = store.get(reply_id)
        assert pending.state == "failed"
        assert pending.error_code == "stale_procedure_run"
    assert player.interrupt_calls == 1
    assert len(player.started_paths) == 1
    assert not player.completed_paths
    assert [event.sequence for event in events] == sorted(
        event.sequence for event in events
    )
    dispatcher.close()


def test_playback_failure_is_terminal(tmp_path: Path) -> None:
    dispatcher, store, _synth, player, events = runtime(
        tmp_path, player=FakePlayer(fail=True)
    )
    dispatcher.start()
    dispatcher.submit(request("play-fail"))
    dispatcher.wait_idle()

    failed = store.get("play-fail")
    assert states(events, "play-fail") == ["queued", "playing", "failed"]
    assert failed.error_code == "playback_failed"
    assert failed.terminal and not failed.success
    assert len(player.calls) == 1
    dispatcher.close()


def test_single_worker_never_overlaps_playback(tmp_path: Path) -> None:
    dispatcher, _store, _synth, player, events = runtime(tmp_path)
    dispatcher.start()
    for index in range(8):
        dispatcher.submit(request(f"r{index}", text=f"문장 {index}"))
    dispatcher.wait_idle()

    assert player.max_active == 1
    assert len(player.calls) == 8
    assert all(states(events, f"r{index}")[-1] == "played" for index in range(8))
    dispatcher.close()


def test_cache_key_covers_voice_and_generation_settings() -> None:
    base = RuntimeConfig(model_identity="model-a")
    key = cache_key_for("문장", base)
    assert key == cache_key_for("문장", base)
    assert key != cache_key_for("다른 문장", base)
    assert key != cache_key_for(
        "문장", RuntimeConfig(model_identity="model-a", voice_id="F2")
    )
    assert key != cache_key_for(
        "문장", RuntimeConfig(model_identity="model-a", steps=12)
    )
    assert key != cache_key_for(
        "문장", RuntimeConfig(model_identity="model-b")
    )


def test_model_manifest_is_path_independent_and_content_sensitive(
    tmp_path: Path,
) -> None:
    relative_files = (
        "onnx/duration_predictor.onnx",
        "onnx/text_encoder.onnx",
        "onnx/vector_estimator.onnx",
        "onnx/vocoder.onnx",
        "onnx/tts.json",
        "onnx/unicode_indexer.json",
        "voice_styles/F1.json",
    )
    roots = (tmp_path / "host-model", tmp_path / "container-model")
    for root in roots:
        for index, relative in enumerate(relative_files):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"artifact-{index}".encode())

    first = supertonic_model_manifest_sha256(roots[0], voice_id="F1")
    second = supertonic_model_manifest_sha256(roots[1], voice_id="F1")
    assert first == second
    (roots[1] / "voice_styles/F1.json").write_bytes(b"changed-voice")
    assert supertonic_model_manifest_sha256(roots[1], voice_id="F1") != first


def test_legacy_database_migrates_all_function_metadata_columns(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy.sqlite3"
    initial = PlaybackStore(database)
    initial.close()
    connection = sqlite3.connect(database)
    for column in (
        "function_call_name",
        "function_arguments_json",
        "function_request_id",
    ):
        connection.execute(f"ALTER TABLE playback_jobs DROP COLUMN {column}")
    connection.commit()
    connection.close()

    upgraded = PlaybackStore(database)
    columns = {
        row[1]
        for row in upgraded._connection.execute(
            "PRAGMA table_info(playback_jobs)"
        ).fetchall()
    }
    assert {
        "function_call_name",
        "function_arguments_json",
        "function_request_id",
    } <= columns
    item = request("migrated", timing="on_function_accepted")
    config = RuntimeConfig(model_identity="test-model")
    upgraded.insert(item, config, cache_key_for(item.text, config))
    restored = upgraded.get("migrated")
    assert restored.function_call_name == item.function_call_name
    assert restored.function_arguments_json == item.function_arguments_json
    assert restored.function_request_id == item.function_request_id
    upgraded.close()
