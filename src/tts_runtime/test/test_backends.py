from __future__ import annotations

from pathlib import Path
import struct
import threading
import time
import wave

import pytest

import tts_runtime.backends as backends
from tts_runtime.backends import PipeWirePlayer, PlaybackInterrupted


def _wav(path: Path) -> Path:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8_000)
        handle.writeframes(struct.pack("<h", 0) * 80)
    return path


class _CompletedProcess:
    def __init__(self) -> None:
        self.returncode: int | None = 0
        self.terminate_calls = 0
        self.kill_calls = 0

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.returncode = -15

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -9

    def communicate(self, timeout: float | None = None):
        return None, ""


class _BlockingProcess(_CompletedProcess):
    def __init__(self) -> None:
        super().__init__()
        self.returncode = None
        self.done = threading.Event()

    def terminate(self) -> None:
        super().terminate()
        self.done.set()

    def kill(self) -> None:
        super().kill()
        self.done.set()

    def communicate(self, timeout: float | None = None):
        if not self.done.wait(timeout=2.0):
            raise TimeoutError("test process was not interrupted")
        return None, ""


def test_idle_interrupt_does_not_cancel_next_direct_playback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _CompletedProcess()
    spawned: list[list[str]] = []

    def fake_popen(command, **_kwargs):
        spawned.append(command)
        return process

    monkeypatch.setattr(backends.subprocess, "Popen", fake_popen)
    player = PipeWirePlayer(target="")

    assert player.interrupt() is False
    result = player.play(_wav(tmp_path / "idle.wav"))

    assert result.playback_latency_ms >= 0
    assert len(spawned) == 1
    assert player.interrupt() is False


def test_interrupt_after_arm_cancels_before_popen_and_resets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _CompletedProcess()
    spawn_count = 0

    def fake_popen(*_args, **_kwargs):
        nonlocal spawn_count
        spawn_count += 1
        return process

    monkeypatch.setattr(backends.subprocess, "Popen", fake_popen)
    player = PipeWirePlayer()
    wav_path = _wav(tmp_path / "pre-spawn.wav")

    player.arm()
    assert player.interrupt() is True
    with pytest.raises(PlaybackInterrupted, match="before pw-play spawn"):
        player.play(wav_path)
    assert spawn_count == 0

    # Cancellation belongs to exactly one armed attempt and cannot leak into
    # speech admitted by the next authoritative procedure scope.
    player.play(wav_path)
    assert spawn_count == 1


def test_interrupt_racing_popen_terminates_current_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _BlockingProcess()
    spawn_entered = threading.Event()
    release_spawn = threading.Event()

    def fake_popen(*_args, **_kwargs):
        spawn_entered.set()
        if not release_spawn.wait(timeout=2.0):
            raise TimeoutError("test did not release Popen")
        return process

    monkeypatch.setattr(backends.subprocess, "Popen", fake_popen)
    player = PipeWirePlayer()
    wav_path = _wav(tmp_path / "spawn-race.wav")
    player.arm()
    playback_errors: list[BaseException] = []
    interrupt_results: list[bool] = []

    def play() -> None:
        try:
            player.play(wav_path)
        except BaseException as exc:
            playback_errors.append(exc)

    playback_thread = threading.Thread(target=play)
    playback_thread.start()
    assert spawn_entered.wait(timeout=1.0)
    interrupt_thread = threading.Thread(
        target=lambda: interrupt_results.append(player.interrupt())
    )
    interrupt_thread.start()
    time.sleep(0.01)
    assert interrupt_thread.is_alive()

    release_spawn.set()
    interrupt_thread.join(timeout=1.0)
    playback_thread.join(timeout=1.0)

    assert not interrupt_thread.is_alive()
    assert not playback_thread.is_alive()
    assert interrupt_results == [True]
    assert process.terminate_calls == 1
    assert len(playback_errors) == 1
    assert isinstance(playback_errors[0], PlaybackInterrupted)
    assert player.interrupt() is False
