"""Supertonic synthesis and PipeWire playback adapters."""

from __future__ import annotations

from collections import deque
import json
import os
from pathlib import Path
import selectors
import subprocess
import threading
from time import monotonic, perf_counter

from .core import PlaybackResult, SynthesisResult, wav_duration


class SupertonicSubprocessSynthesizer:
    """Lazy, persistent Supertonic worker isolated in its Python environment."""

    def __init__(
        self,
        *,
        python_executable: Path | str,
        model_dir: Path | str,
        voice_id: str = "F1",
        language: str = "ko",
        steps: int = 8,
        speed: float = 1.05,
        silence_duration: float = 0.3,
        request_timeout_sec: float = 120.0,
        intra_op_threads: int = 4,
        inter_op_threads: int = 1,
    ) -> None:
        # Do not resolve this path: virtualenv executables are commonly symlinks,
        # and dereferencing one bypasses the virtualenv's site-packages.
        self.python_executable = Path(python_executable).expanduser().absolute()
        self.model_dir = Path(model_dir).expanduser().resolve()
        self.voice_id = voice_id
        self.language = language
        self.steps = int(steps)
        self.speed = float(speed)
        self.silence_duration = float(silence_duration)
        self.request_timeout_sec = float(request_timeout_sec)
        self.intra_op_threads = int(intra_op_threads)
        self.inter_op_threads = int(inter_op_threads)
        self._process: subprocess.Popen[str] | None = None
        self._request_lock = threading.Lock()
        self._stderr_tail: deque[str] = deque(maxlen=40)
        self._stderr_thread: threading.Thread | None = None

    def _drain_stderr(self, process: subprocess.Popen[str]) -> None:
        if process.stderr is None:
            return
        for line in process.stderr:
            self._stderr_tail.append(line.rstrip())

    def _start_locked(self) -> subprocess.Popen[str]:
        if self._process is not None and self._process.poll() is None:
            return self._process
        if not self.python_executable.is_file():
            raise FileNotFoundError(
                f"Supertonic Python executable not found: {self.python_executable}"
            )
        required = (
            self.model_dir / "onnx" / "tts.json",
            self.model_dir / "voice_styles" / f"{self.voice_id}.json",
        )
        missing = [str(path) for path in required if not path.is_file()]
        if missing:
            raise FileNotFoundError(
                "Supertonic model files are missing: " + ", ".join(missing)
            )
        worker_script = Path(__file__).with_name("backend_worker.py")
        command = [
            str(self.python_executable),
            str(worker_script),
            "--model-dir",
            str(self.model_dir),
            "--voice",
            self.voice_id,
            "--language",
            self.language,
            "--steps",
            str(self.steps),
            "--speed",
            str(self.speed),
            "--silence-duration",
            str(self.silence_duration),
            "--intra-op-threads",
            str(self.intra_op_threads),
            "--inter-op-threads",
            str(self.inter_op_threads),
        ]
        environment = os.environ.copy()
        environment.setdefault("OMP_NUM_THREADS", str(self.intra_op_threads))
        self._stderr_tail.clear()
        self._process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=environment,
        )
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            args=(self._process,),
            name="supertonic-stderr-drain",
            daemon=True,
        )
        self._stderr_thread.start()
        return self._process

    def _read_response_locked(self, process: subprocess.Popen[str]) -> dict[str, object]:
        if process.stdout is None:
            raise RuntimeError("Supertonic worker stdout is unavailable")
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = monotonic() + self.request_timeout_sec
        try:
            while True:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    self._terminate_locked()
                    raise TimeoutError("Supertonic synthesis timed out")
                ready = selector.select(remaining)
                if not ready:
                    continue
                line = process.stdout.readline()
                if not line:
                    tail = " | ".join(self._stderr_tail)
                    raise RuntimeError(
                        f"Supertonic worker exited with {process.poll()}: {tail}"
                    )
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    return value
        finally:
            selector.close()

    def synthesize(self, text: str, output_path: Path, seed: int) -> SynthesisResult:
        with self._request_lock:
            request_started = perf_counter()
            process = self._start_locked()
            if process.stdin is None:
                raise RuntimeError("Supertonic worker stdin is unavailable")
            request = {
                "command": "synthesize",
                "text": text,
                "output_path": str(output_path),
                "seed": int(seed),
            }
            try:
                process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                self._terminate_locked()
                raise RuntimeError("Supertonic worker request failed") from exc
            response = self._read_response_locked(process)
            if not response.get("ok"):
                raise RuntimeError(str(response.get("error", "unknown synthesis error")))
            measured_duration = wav_duration(output_path)
            return SynthesisResult(
                # This is the user-observable request latency. On the first
                # cache miss it intentionally includes subprocess startup,
                # lazy model/style load, synthesis, WAV write and validation.
                synth_latency_ms=(perf_counter() - request_started) * 1000.0,
                audio_duration_sec=measured_duration,
            )

    def _terminate_locked(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3.0)

    def close(self) -> None:
        with self._request_lock:
            process = self._process
            if process is not None and process.poll() is None and process.stdin is not None:
                try:
                    process.stdin.write('{"command":"shutdown"}\n')
                    process.stdin.flush()
                    process.wait(timeout=3.0)
                except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                    self._terminate_locked()
            self._process = None


class PipeWirePlayer:
    """Synchronous playback through PipeWire's current route.

    ``arm`` gives the dispatcher a cancellation point before ``Popen``.  This
    matters when the authoritative procedure scope changes in the small gap
    between committing the durable ``playing`` state and spawning ``pw-play``.
    """

    def __init__(
        self,
        *,
        target: str = "",
        executable: str = "pw-play",
        timeout_margin_sec: float = 10.0,
    ) -> None:
        self.target = target.strip()
        self.executable = executable
        self.timeout_margin_sec = float(timeout_margin_sec)
        self._lock = threading.Lock()
        self._armed = False
        self._cancel_requested = False
        self._process: subprocess.Popen[str] | None = None

    def arm(self) -> None:
        """Arm one playback attempt without spawning an audible side effect."""

        with self._lock:
            if self._armed or self._process is not None:
                raise RuntimeError("PipeWire playback attempt is already active")
            self._armed = True
            self._cancel_requested = False

    def interrupt(self) -> bool:
        """Cancel the armed/current playback, including the pre-spawn window."""

        with self._lock:
            if not self._armed:
                return False
            self._cancel_requested = True
            process = self._process
            if process is not None and process.poll() is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                except OSError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
            return True

    def _reset_attempt_locked(self) -> None:
        self._process = None
        self._armed = False
        self._cancel_requested = False

    def play(self, wav_path: Path) -> PlaybackResult:
        command = [
            self.executable,
            "--media-type",
            "Audio",
            "--media-category",
            "Playback",
            "--media-role",
            "Communication",
        ]
        if self.target:
            command.extend(["--target", self.target])
        command.append(str(wav_path))
        try:
            duration = 0.0
            duration = wav_duration(wav_path)
        except Exception:
            duration = 0.0
        started = perf_counter()
        with self._lock:
            # Direct callers remain supported; the dispatcher explicitly arms
            # first so an intervening scope change can cancel before spawn.
            if not self._armed:
                self._armed = True
                self._cancel_requested = False
            if self._cancel_requested:
                self._reset_attempt_locked()
                raise PlaybackInterrupted("playback cancelled before pw-play spawn")
            try:
                self._process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            except BaseException:
                self._reset_attempt_locked()
                raise
            process = self._process
        try:
            _stdout, stderr = process.communicate(
                timeout=max(10.0, duration + self.timeout_margin_sec)
            )
        except subprocess.TimeoutExpired as exc:
            process.kill()
            _stdout, stderr = process.communicate()
            with self._lock:
                cancelled = self._cancel_requested
                self._reset_attempt_locked()
            if cancelled:
                raise PlaybackInterrupted("playback interrupted") from exc
            detail = (stderr or "").strip()
            raise TimeoutError(detail or "pw-play timed out") from exc
        except BaseException:
            with self._lock:
                self._reset_attempt_locked()
            raise
        with self._lock:
            cancelled = self._cancel_requested
            self._reset_attempt_locked()
        elapsed_ms = (perf_counter() - started) * 1000.0
        if cancelled:
            raise PlaybackInterrupted("playback interrupted")
        if process.returncode != 0:
            detail = (stderr or "").strip() or f"pw-play exited {process.returncode}"
            raise RuntimeError(detail)
        return PlaybackResult(playback_latency_ms=elapsed_ms)


class PlaybackInterrupted(RuntimeError):
    """An armed PipeWire playback was cancelled by an authority boundary."""
