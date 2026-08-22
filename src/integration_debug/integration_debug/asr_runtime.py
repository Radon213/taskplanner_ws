"""USB microphone and Puzzle AI WebSocket ASR runtime for Debug Mode.

The runtime intentionally publishes only server-finalized sentences. Partial
recognition is exposed as diagnostic state but never leaves the ASR panel as a
ROS input message.
"""

from __future__ import annotations

import asyncio
from collections import deque
import contextlib
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import subprocess
import threading
import time
from typing import Any
import wave

from integration_debug.asr_endpoints import validate_websocket_url
from integration_debug.puzzle_asr_postprocess import KEYWORDS, correct


SAMPLE_RATE = 16_000
CHANNELS = 1
SAMPLE_WIDTH = 2
CHUNK_FRAMES = 4_096
CHUNK_BYTES = CHUNK_FRAMES * SAMPLE_WIDTH * CHANNELS
DEFAULT_BLOCK_FRAMES = 1_600
# The new Puzzle AI handoff owns instrument vocabulary and lexical correction.
# Keep this public alias for existing callers/tests that inspect the transport
# configuration directly.
DEFAULT_KEYWORDS = KEYWORDS

PIPEWIRE_DEFAULT_SOURCE = "@DEFAULT_AUDIO_SOURCE@"
PIPEWIRE_CAPTURE_NAMES = frozenset({"default", "pipewire"})
DEVICE_STATUS_READY = "READY"
DEVICE_STATUS_NO_INPUT = "NO_INPUT"
DEVICE_STATUS_HOST_AUDIO_UNAVAILABLE = "HOST_AUDIO_UNAVAILABLE"
DEVICE_STATUS_BRIDGE_ERROR = "BRIDGE_ERROR"


class AudioInputUnavailable(RuntimeError):
    """A classified host-audio state discovered without opening a device."""

    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _optional_audio_modules() -> tuple[Any | None, Any | None, Any | None, str]:
    try:
        import numpy as np  # type: ignore[import-not-found]
        import sounddevice as sd  # type: ignore[import-not-found]
        import websockets  # type: ignore[import-not-found]
    except Exception as exc:
        return None, None, None, f"ASR dependency unavailable: {exc}"
    return np, sd, websockets, ""


def _parse_wpctl_properties(output: str) -> dict[str, str]:
    """Parse stable ``key = value`` properties from ``wpctl inspect``."""

    properties: dict[str, str] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line.startswith("*"):
            line = line[1:].strip()
        if " = " not in line:
            continue
        key, raw_value = line.split(" = ", 1)
        value = raw_value.strip()
        if value.startswith('"') and value.endswith('"'):
            try:
                value = str(json.loads(value))
            except (TypeError, ValueError):
                value = value[1:-1]
        properties[key.strip()] = value
    return properties


def _query_pipewire_default_source() -> dict[str, Any]:
    """Return Ubuntu's effective logical input without exposing raw ALSA ports."""

    try:
        completed = subprocess.run(
            ["wpctl", "inspect", PIPEWIRE_DEFAULT_SOURCE],
            check=True,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except subprocess.CalledProcessError as exc:
        # ``wpctl inspect @DEFAULT_AUDIO_SOURCE@`` exits non-zero both when the
        # PipeWire server cannot be reached and when the live graph simply has
        # no default Source. Probe the graph itself to keep the normal
        # unplugged/no-input state out of the error channel.
        try:
            subprocess.run(
                ["wpctl", "status"],
                check=True,
                capture_output=True,
                text=True,
                timeout=2.0,
            )
        except (OSError, subprocess.SubprocessError) as status_exc:
            raise AudioInputUnavailable(
                DEVICE_STATUS_HOST_AUDIO_UNAVAILABLE,
                "Ubuntu PipeWire is not reachable from Debug Mode",
            ) from status_exc
        raise AudioInputUnavailable(
            DEVICE_STATUS_NO_INPUT,
            "Ubuntu currently exposes no PipeWire microphone input",
        ) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise AudioInputUnavailable(
            DEVICE_STATUS_HOST_AUDIO_UNAVAILABLE,
            "Ubuntu PipeWire is not reachable from Debug Mode",
        ) from exc
    properties = _parse_wpctl_properties(completed.stdout)
    if properties.get("media.class") != "Audio/Source":
        raise AudioInputUnavailable(
            DEVICE_STATUS_NO_INPUT,
            "Ubuntu currently exposes no PipeWire microphone input",
        )
    nickname = (
        properties.get("node.nick")
        or properties.get("api.alsa.card.name")
        or properties.get("alsa.card_name")
        or "System Microphone"
    ).strip()
    if not nickname:
        raise RuntimeError("Ubuntu PipeWire default input has no device name")
    try:
        channels = max(1, int(properties.get("audio.channels", "1")))
    except ValueError:
        channels = 1
    input_kind = (
        "Analog Input"
        if "analog" in properties.get("device.icon-name", "").casefold()
        else "Input"
    )
    return {
        "name": f"{input_kind} - {nickname}",
        "input_channels": channels,
    }


@dataclass(frozen=True, slots=True)
class AudioInputFormat:
    """A hardware capture format that is converted to the ASR wire format."""

    sample_rate: int
    channels: int
    block_frames: int

    @property
    def requires_conversion(self) -> bool:
        return self.sample_rate != SAMPLE_RATE or self.channels != CHANNELS


def _input_format_candidates(device_info: Any) -> list[AudioInputFormat]:
    """Prefer the wire format, then formats based on the device's native values."""

    try:
        max_channels = max(0, int(device_info.get("max_input_channels", 0)))
    except (AttributeError, TypeError, ValueError):
        max_channels = 0
    try:
        native_rate = int(round(float(device_info.get("default_samplerate", 0.0))))
    except (AttributeError, TypeError, ValueError):
        native_rate = 0
    if max_channels <= 0:
        raise RuntimeError("Selected device has no microphone input channels")

    raw_candidates: list[tuple[int, int]] = [(SAMPLE_RATE, CHANNELS)]
    if native_rate > 0:
        # Some USB interfaces accept mono at their native rate, while others
        # expose only stereo or their full channel layout. Probe the least
        # expensive layouts first and retain the advertised maximum as the
        # final native fallback.
        native_channels = [1]
        if max_channels >= 2:
            native_channels.append(2)
        if max_channels not in native_channels:
            native_channels.append(max_channels)
        raw_candidates.extend((native_rate, channels) for channels in native_channels)

    candidates: list[AudioInputFormat] = []
    seen: set[tuple[int, int]] = set()
    for sample_rate, channels in raw_candidates:
        key = (sample_rate, channels)
        if sample_rate <= 0 or channels <= 0 or channels > max_channels or key in seen:
            continue
        seen.add(key)
        block_frames = max(
            1,
            int(round(DEFAULT_BLOCK_FRAMES * sample_rate / SAMPLE_RATE)),
        )
        candidates.append(
            AudioInputFormat(
                sample_rate=sample_rate,
                channels=channels,
                block_frames=block_frames,
            )
        )
    return candidates


def _select_input_format(
    sounddevice_module: Any,
    device_id: int | None,
) -> tuple[AudioInputFormat, Any]:
    """Return the first PortAudio-supported target or native capture format."""

    device_info = sounddevice_module.query_devices(device_id, "input")
    candidates = _input_format_candidates(device_info)
    checker = getattr(sounddevice_module, "check_input_settings", None)
    if checker is None:
        raise RuntimeError("sounddevice cannot validate microphone input formats")
    for candidate in candidates:
        try:
            checker(
                device=device_id,
                channels=candidate.channels,
                dtype="int16",
                samplerate=candidate.sample_rate,
            )
        except Exception:
            continue
        return candidate, device_info
    raise RuntimeError(
        "Selected microphone supports neither 16 kHz mono nor a usable native format"
    )


class Pcm16MonoResampler:
    """Stateful channel mixer and linear resampler for callback-sized PCM blocks."""

    def __init__(
        self,
        numpy_module: Any,
        *,
        input_sample_rate: int,
        input_channels: int,
    ) -> None:
        if input_sample_rate <= 0:
            raise ValueError("input_sample_rate must be positive")
        if input_channels <= 0:
            raise ValueError("input_channels must be positive")
        self._np = numpy_module
        self.input_sample_rate = int(input_sample_rate)
        self.input_channels = int(input_channels)
        self._step = float(self.input_sample_rate) / float(SAMPLE_RATE)
        self._input_frames_seen = 0
        self._output_frames_emitted = 0
        self._carry_sample: float | None = None

    def process(self, indata: Any) -> bytes:
        """Convert one native int16 block to streaming 16 kHz mono int16 PCM."""

        np = self._np
        block = np.asarray(indata)
        if block.ndim == 1:
            if self.input_channels > 1:
                if block.size % self.input_channels:
                    raise ValueError("microphone block does not match its channel count")
                block = block.reshape((-1, self.input_channels))
            else:
                block = block.reshape((-1, 1))
        if block.ndim != 2 or block.shape[1] != self.input_channels:
            raise ValueError("microphone block does not match its channel count")
        if block.shape[0] == 0:
            return b""

        # float64 prevents overflow while averaging signed int16 channels.
        mono = block.astype(np.float64, copy=False).mean(axis=1)
        block_start = self._input_frames_seen
        available_end = block_start + int(mono.size) - 1
        if self._carry_sample is None:
            samples = mono
            sample_start = block_start
        else:
            samples = np.concatenate(
                (np.asarray([self._carry_sample], dtype=np.float64), mono)
            )
            sample_start = block_start - 1

        next_output_position = self._output_frames_emitted * self._step
        remaining = available_end - next_output_position
        if remaining < -1e-9:
            converted = np.empty(0, dtype=np.float64)
        else:
            count = int(math.floor((remaining / self._step) + 1e-12)) + 1
            output_indexes = np.arange(
                self._output_frames_emitted,
                self._output_frames_emitted + count,
                dtype=np.float64,
            )
            positions = output_indexes * self._step
            positions = positions[positions <= available_end + 1e-9]
            sample_positions = sample_start + np.arange(samples.size, dtype=np.float64)
            converted = np.interp(positions, sample_positions, samples)
            self._output_frames_emitted += int(positions.size)

        self._input_frames_seen += int(mono.size)
        self._carry_sample = float(mono[-1])
        if converted.size == 0:
            return b""
        pcm = np.clip(np.rint(converted), -32768, 32767).astype("<i2")
        return pcm.tobytes()


class AsrWsClient:
    """ZIP-derived Puzzle AI transport for 16 kHz mono signed PCM.

    The received protocol is preserved (server VAD, keyword configuration,
    ``partial``/``is_final`` JSON and EOF flush).  Bounded buffering,
    terminal-frame padding and final-only callbacks are Taskplanner safety
    guards around that protocol.
    """

    def __init__(
        self,
        *,
        url: str,
        websockets_module: Any,
        on_final: Any,
        on_final_metadata: Any = None,
        on_partial: Any,
        on_connection: Any,
        on_error: Any,
        keywords: tuple[tuple[str, int], ...] = DEFAULT_KEYWORDS,
        queue_max: int = 64,
        reconnect_delay_sec: float = 2.0,
        eof_wait_sec: float = 15.0,
    ) -> None:
        self._url = validate_websocket_url(url)
        self._websockets = websockets_module
        self._on_final = on_final
        self._on_final_metadata = on_final_metadata
        self._on_partial = on_partial
        self._on_connection = on_connection
        self._on_error = on_error
        self._keywords = keywords
        self._queue_max = max(8, int(queue_max))
        self._reconnect_delay_sec = max(0.2, float(reconnect_delay_sec))
        self._eof_wait_sec = max(1.0, float(eof_wait_sec))
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._run_task: asyncio.Task[Any] | None = None
        self._queue: asyncio.Queue[bytes] | None = None
        self._ready = threading.Event()
        self._connected = threading.Event()
        self._stopping = False
        self._inflight = 0
        self._inflight_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._sent = 0
        self._responses = 0
        self._dropped = 0
        self._sessions = 0
        self._padded_final_bytes = 0
        self._last_partial = ""
        self._last_audio_sent_monotonic_ns = 0

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stopping = False
        self._thread = threading.Thread(
            target=self._thread_main,
            name="debug-asr-websocket",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=5.0):
            raise RuntimeError("ASR WebSocket worker did not initialize")

    def feed(self, data: bytes) -> None:
        if self._stopping or not data or not self._ready.is_set():
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        with self._inflight_lock:
            self._inflight += 1
        loop.call_soon_threadsafe(self._put, bytes(data))

    def pending(self) -> int:
        with self._inflight_lock:
            inflight = self._inflight
        queue_size = self._queue.qsize() if self._queue is not None else 0
        return queue_size + inflight

    def stats(self) -> dict[str, Any]:
        with self._stats_lock:
            return {
                "sent_chunks": self._sent,
                "responses": self._responses,
                "dropped_chunks": self._dropped,
                "sessions": self._sessions,
                "padded_final_bytes": self._padded_final_bytes,
                "pending_chunks": self.pending(),
                "connected": self._connected.is_set(),
            }

    def stop(self, *, flush_timeout_sec: float = 4.0) -> bool:
        thread = self._thread
        if thread is None:
            return True
        deadline = time.monotonic() + max(0.2, flush_timeout_sec)
        while self.pending() and time.monotonic() < deadline:
            time.sleep(0.02)
        self._stopping = True
        thread.join(timeout=self._eof_wait_sec + 5.0)
        if thread.is_alive():
            loop = self._loop
            task = self._run_task
            if loop is not None and not loop.is_closed() and task is not None:
                loop.call_soon_threadsafe(task.cancel)
            thread.join(timeout=6.0)
        if thread.is_alive():
            self._on_error("ASR WebSocket worker did not stop after cancellation")
            return False
        self._thread = None
        self._loop = None
        self._queue = None
        self._ready.clear()
        self._connected.clear()
        self._on_connection(False)
        return True

    def _put(self, data: bytes) -> None:
        with self._inflight_lock:
            self._inflight = max(0, self._inflight - 1)
        queue = self._queue
        if queue is None:
            return
        try:
            queue.put_nowait(data)
            return
        except asyncio.QueueFull:
            pass
        with contextlib.suppress(asyncio.QueueEmpty):
            queue.get_nowait()
        with self._stats_lock:
            self._dropped += 1
        with contextlib.suppress(asyncio.QueueFull):
            queue.put_nowait(data)

    def _drain(self) -> None:
        queue = self._queue
        if queue is None:
            return
        while True:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                return

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except asyncio.CancelledError:
            if not self._stopping:
                self._on_error("ASR WebSocket worker was cancelled unexpectedly")
        except Exception as exc:
            self._on_error(f"ASR WebSocket worker stopped: {exc}")

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._run_task = asyncio.current_task()
        self._queue = asyncio.Queue(maxsize=self._queue_max)
        self._ready.set()
        try:
            while not self._stopping:
                try:
                    await self._session()
                except Exception as exc:
                    self._connected.clear()
                    self._on_connection(False)
                    if self._stopping:
                        break
                    self._on_error(f"ASR connection lost: {type(exc).__name__}: {exc}")
                    self._drain()
                    await asyncio.sleep(self._reconnect_delay_sec)
        finally:
            self._run_task = None

    def _config(self) -> dict[str, Any]:
        # Match the received ZIP protocol. Sentence boundaries remain a
        # server-VAD decision; timestamps are not part of the external text
        # input contract.
        config: dict[str, Any] = {"use_vad": True, "use_timestamp": False}
        if self._keywords:
            config["keywords"] = [
                {"keyword": keyword, "sensitivity": sensitivity}
                for keyword, sensitivity in self._keywords
            ]
        return {"config": config}

    async def _session(self) -> None:
        async with self._websockets.connect(
            self._url,
            max_size=None,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
        ) as websocket:
            await websocket.send(json.dumps(self._config(), ensure_ascii=False))
            with self._stats_lock:
                self._sessions += 1
            self._last_partial = ""
            self._last_audio_sent_monotonic_ns = 0
            self._connected.set()
            self._on_connection(True)
            tasks = [
                asyncio.create_task(self._sender(websocket), name="debug-asr-sender"),
                asyncio.create_task(self._receiver(websocket), name="debug-asr-receiver"),
            ]
            done: set[asyncio.Task[Any]] = set()
            pending: set[asyncio.Task[Any]] = set(tasks)
            try:
                done, pending = await asyncio.wait(
                    tasks,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if self._stopping and pending:
                    finished, pending = await asyncio.wait(
                        pending,
                        timeout=self._eof_wait_sec,
                    )
                    done |= finished
            finally:
                self._connected.clear()
                self._on_connection(False)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                task.result()

    async def _sender(self, websocket: Any) -> None:
        buffer = bytearray()
        while True:
            queue = self._queue
            if queue is None:
                return
            with contextlib.suppress(asyncio.TimeoutError):
                buffer += await asyncio.wait_for(queue.get(), timeout=0.2)
            while len(buffer) >= CHUNK_BYTES:
                await websocket.send(bytes(buffer[:CHUNK_BYTES]))
                self._last_audio_sent_monotonic_ns = time.monotonic_ns()
                del buffer[:CHUNK_BYTES]
                with self._stats_lock:
                    self._sent += 1
            if self._stopping:
                # The server requires exact 8192-byte PCM frames. Pad the last
                # remainder with silence so a short terminal utterance is not
                # dropped while preserving the wire contract.
                if buffer:
                    remainder = len(buffer)
                    padding = CHUNK_BYTES - remainder
                    buffer.extend(b"\x00" * padding)
                    await websocket.send(bytes(buffer))
                    self._last_audio_sent_monotonic_ns = time.monotonic_ns()
                    with self._stats_lock:
                        self._sent += 1
                        self._padded_final_bytes += padding
                await websocket.send(json.dumps({"eof": True}))
                return

    async def _receiver(self, websocket: Any) -> None:
        async for raw in websocket:
            received_monotonic_ns = time.monotonic_ns()
            if isinstance(raw, (bytes, bytearray)):
                continue
            try:
                data = json.loads(raw)
            except (TypeError, ValueError):
                self._on_error("ASR server returned invalid JSON")
                continue
            with self._stats_lock:
                self._responses += 1
            if "is_final" not in data:
                self._on_error("ASR response is missing is_final")
                continue
            text = str(data.get("partial") or "").strip()
            if data.get("is_final"):
                self._last_partial = ""
                if text:
                    delta_ns = (
                        received_monotonic_ns
                        - self._last_audio_sent_monotonic_ns
                    )
                    response_latency_ms = (
                        round(delta_ns / 1_000_000.0, 1)
                        if self._last_audio_sent_monotonic_ns > 0
                        and delta_ns >= 0
                        else None
                    )
                    if self._on_final_metadata is not None:
                        self._on_final_metadata(
                            {
                                "response_latency_ms": response_latency_ms,
                                "latency_basis": (
                                    "latest_pcm_send_complete_to_final_receive"
                                ),
                                "latency_correlated": False,
                            }
                        )
                    self._on_final(text)
                continue
            if text and text != self._last_partial:
                self._last_partial = text
                self._on_partial(text)


class AsrMicrophoneRuntime:
    """Own one microphone/ASR test session and expose a JSON-safe snapshot."""

    def __init__(
        self,
        *,
        default_url: str,
        topic: str,
        output_dir: str | Path,
        save_artifacts: bool = True,
        capture_lock_path: str | Path | None = None,
    ) -> None:
        self._np, self._sd, self._websockets, dependency_error = _optional_audio_modules()
        self._default_url = validate_websocket_url(default_url)
        self._topic = str(topic)
        self._output_dir = Path(output_dir)
        self._save_artifacts_enabled = bool(save_artifacts)
        self._capture_lock_path = (
            Path(capture_lock_path) if capture_lock_path is not None else None
        )
        self._capture_lock_fd: int | None = None
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.Lock()
        self._events: deque[dict[str, Any]] = deque(maxlen=256)
        self._devices: list[dict[str, Any]] = []
        self._state = "STOPPED" if not dependency_error else "UNAVAILABLE"
        self._dependency_error = dependency_error
        self._last_error = dependency_error
        self._device_status = (
            DEVICE_STATUS_BRIDGE_ERROR if dependency_error else DEVICE_STATUS_NO_INPUT
        )
        self._device_message = dependency_error or (
            "Ubuntu microphone input has not been discovered yet"
        )
        self._server_url = self._default_url
        self._device_id: int | None = None
        self._device_name = ""
        self._connected = False
        self._started_monotonic = 0.0
        self._stopped_monotonic = 0.0
        self._audio_level_dbfs = -99.0
        self._peak_level_dbfs = -99.0
        self._blocks_captured = 0
        self._input_dropped = 0
        self._partial_text = ""
        self._finals: deque[dict[str, Any]] = deque(maxlen=40)
        self._recorded_pcm: list[bytes] = []
        self._recording_path = ""
        self._transcript_path = ""
        self._stream: Any | None = None
        self._client: AsrWsClient | None = None
        self._resampler: Pcm16MonoResampler | None = None
        self._pending_final_metadata: dict[str, Any] | None = None
        self._input_sample_rate = SAMPLE_RATE
        self._input_channels = CHANNELS
        self._input_block_frames = DEFAULT_BLOCK_FRAMES
        self._stop_thread: threading.Thread | None = None
        self._last_transport_stats: dict[str, Any] = {
            "sent_chunks": 0,
            "responses": 0,
            "dropped_chunks": 0,
            "sessions": 0,
            "padded_final_bytes": 0,
            "pending_chunks": 0,
            "connected": False,
        }
        if not dependency_error:
            self.refresh_devices()

    def refresh_devices(self) -> list[dict[str, Any]]:
        sd = self._sd
        if sd is None:
            return []
        try:
            logical_source = _query_pipewire_default_source()
            default_pair = sd.default.device
            default_input = int(default_pair[0]) if default_pair is not None else -1
            if default_input < 0:
                raise RuntimeError("PortAudio has no default PipeWire input")
            info = sd.query_devices(default_input, "input")
            capture_name = str(info.get("name", "")).strip().casefold()
            if capture_name not in PIPEWIRE_CAPTURE_NAMES:
                raise RuntimeError(
                    "PortAudio is not connected to Ubuntu PipeWire input"
                )
            if int(info.get("max_input_channels", 0)) <= 0:
                raise RuntimeError("Ubuntu PipeWire input has no capture channels")
            rows = [
                {
                    "id": default_input,
                    "name": logical_source["name"],
                    "input_channels": logical_source["input_channels"],
                    "default_samplerate": round(
                        float(info.get("default_samplerate", 0.0)),
                        1,
                    ),
                    "default": True,
                }
            ]
            with self._lock:
                self._devices = rows
                if self._state == "UNAVAILABLE":
                    self._state = "STOPPED"
                self._dependency_error = ""
                self._last_error = ""
                self._device_status = DEVICE_STATUS_READY
                self._device_message = (
                    f"Ubuntu current input: {logical_source['name']}"
                )
            return rows
        except AudioInputUnavailable as exc:
            with self._lock:
                self._devices = []
                self._device_status = exc.status
                self._device_message = str(exc)
                # A reachable host graph with no Source is an expected
                # hotplug/selection state, not a runtime failure.
                self._last_error = (
                    "" if exc.status == DEVICE_STATUS_NO_INPUT else str(exc)
                )
            return []
        except Exception as exc:
            with self._lock:
                self._devices = []
                self._device_status = DEVICE_STATUS_BRIDGE_ERROR
                self._device_message = f"Microphone discovery failed: {exc}"
                self._last_error = self._device_message
            return []

    def start(self, *, device_id: Any = None, server_url: Any = None) -> None:
        # Device probing, WebSocket setup, and PortAudio startup form one
        # lifecycle transition. Serialize them with stop_async so a concurrent
        # stop cannot report STOPPED and then have this start resurrect audio.
        with self._lifecycle_lock:
            self._start_locked(device_id=device_id, server_url=server_url)

    def _start_locked(
        self, *, device_id: Any = None, server_url: Any = None
    ) -> None:
        if self._dependency_error or self._sd is None or self._websockets is None:
            raise RuntimeError(self._dependency_error or "ASR dependencies are unavailable")
        # Resolve the logical default again at the privacy-sensitive open
        # boundary. Ubuntu's selected source can change after the UI snapshot;
        # never capture a new default under a stale device label.
        self.refresh_devices()
        with self._lock:
            if self._state not in {"STOPPED", "ERROR"}:
                raise ValueError("ASR microphone session is already active")
            if self._client is not None or self._stream is not None:
                raise RuntimeError(
                    "previous ASR session has not released its resources"
                )
            url = validate_websocket_url(server_url or self._default_url)
            visible_devices = {int(row["id"]): row for row in self._devices}
            if device_id in {None, "", "default"}:
                selected = next(
                    (row for row in self._devices if row.get("default")),
                    self._devices[0] if self._devices else None,
                )
                if selected is None:
                    raise ValueError("No Ubuntu microphone input is available")
                requested_device = int(selected["id"])
            else:
                try:
                    requested_device = int(device_id)
                except (TypeError, ValueError) as exc:
                    raise ValueError("device_id must be a microphone device number") from exc
                selected = visible_devices.get(requested_device)
                if selected is None:
                    raise ValueError(
                        "Selected microphone is not the current Ubuntu input"
                    )
            logical_device_name = str(selected["name"])
            self._state = "STARTING"
            self._last_error = ""
            self._server_url = url
            self._device_id = requested_device
            self._device_name = ""
            self._connected = False
            self._started_monotonic = time.monotonic()
            self._stopped_monotonic = 0.0
            self._audio_level_dbfs = -99.0
            self._peak_level_dbfs = -99.0
            self._blocks_captured = 0
            self._input_dropped = 0
            self._partial_text = ""
            self._finals.clear()
            self._recorded_pcm = []
            self._recording_path = ""
            self._transcript_path = ""
            self._resampler = None
            self._pending_final_metadata = None
            self._input_sample_rate = SAMPLE_RATE
            self._input_channels = CHANNELS
            self._input_block_frames = DEFAULT_BLOCK_FRAMES
            self._last_transport_stats = {
                "sent_chunks": 0,
                "responses": 0,
                "dropped_chunks": 0,
                "sessions": 0,
                "padded_final_bytes": 0,
                "pending_chunks": 0,
                "connected": False,
            }

        client: AsrWsClient | None = None
        stream: Any | None = None
        try:
            self._acquire_capture_lock()
            input_format, _device_info = _select_input_format(
                self._sd,
                requested_device,
            )
            resampler = Pcm16MonoResampler(
                self._np,
                input_sample_rate=input_format.sample_rate,
                input_channels=input_format.channels,
            )
            client = AsrWsClient(
                url=url,
                websockets_module=self._websockets,
                on_final=self._on_final,
                on_final_metadata=self._on_final_metadata,
                on_partial=self._on_partial,
                on_connection=self._on_connection,
                on_error=self._on_error,
            )
            client.start()
            stream = self._sd.InputStream(
                samplerate=input_format.sample_rate,
                channels=input_format.channels,
                dtype="int16",
                blocksize=input_format.block_frames,
                device=requested_device,
                callback=self._on_audio,
            )
            # Publish and record callback data only after the converter and
            # client are installed, including callbacks fired immediately by
            # PortAudio during stream.start().
            with self._lock:
                self._client = client
                self._resampler = resampler
                self._input_sample_rate = input_format.sample_rate
                self._input_channels = input_format.channels
                self._input_block_frames = input_format.block_frames
            stream.start()
        except Exception as exc:
            if stream is not None:
                with contextlib.suppress(Exception):
                    stream.stop()
                with contextlib.suppress(Exception):
                    stream.close()
            if client is not None:
                client.stop(flush_timeout_sec=0.2)
            with self._lock:
                self._client = None
                self._resampler = None
                self._state = "ERROR"
                self._last_error = f"Microphone start failed: {exc}"
            self._release_capture_lock()
            raise RuntimeError(self._last_error) from exc
        with self._lock:
            self._stream = stream
            self._device_id = int(stream.device)
            self._device_name = logical_device_name
            self._state = "LISTENING"
            self._events.append(
                {
                    "type": "asr_started",
                    "stamp": _utc_now(),
                    "device_id": self._device_id,
                    "device_name": self._device_name,
                    "server_url": self._server_url,
                    "input_sample_rate": self._input_sample_rate,
                    "input_channels": self._input_channels,
                    "resampling": input_format.requires_conversion,
                }
            )

    def stop_async(self) -> None:
        with self._lifecycle_lock:
            self._stop_async_locked()

    def _stop_async_locked(self) -> None:
        with self._lock:
            if self._state in {"STOPPED", "UNAVAILABLE"}:
                return
            if self._state == "STOPPING":
                return
            self._state = "STOPPING"
            stream = self._stream
            self._stream = None
        if stream is not None:
            with contextlib.suppress(Exception):
                stream.stop()
            with contextlib.suppress(Exception):
                stream.close()
        thread = threading.Thread(
            target=self._finish_stop,
            name="debug-asr-stop",
            daemon=True,
        )
        self._stop_thread = thread
        thread.start()

    def close(self) -> bool:
        self.stop_async()
        thread = self._stop_thread
        if thread is not None:
            thread.join(timeout=32.0)
            if thread.is_alive():
                self._on_error("ASR microphone stop did not finish before shutdown")
                with self._lock:
                    self._state = "ERROR"
                self._release_capture_lock()
                return False
        self._release_capture_lock()
        return True

    def drain_events(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = list(self._events)
            self._events.clear()
        return rows

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            client = self._client
            elapsed = (
                max(0.0, (self._stopped_monotonic or now) - self._started_monotonic)
                if self._started_monotonic
                else 0.0
            )
            snapshot = {
                "available": not bool(self._dependency_error),
                "dependency_error": self._dependency_error,
                "state": self._state,
                "server_url": self._server_url,
                "topic": self._topic,
                "device_id": self._device_id,
                "device_name": self._device_name,
                "devices": list(self._devices),
                "device_status": self._device_status,
                "device_message": self._device_message,
                "connected": self._connected,
                "audio_level_dbfs": round(self._audio_level_dbfs, 1),
                "peak_level_dbfs": round(self._peak_level_dbfs, 1),
                "elapsed_sec": round(elapsed, 1),
                "blocks_captured": self._blocks_captured,
                "input_dropped": self._input_dropped,
                "partial_text": self._partial_text,
                "finals": list(self._finals),
                "last_error": self._last_error,
                "recording_path": self._recording_path,
                "transcript_path": self._transcript_path,
                "artifacts_enabled": self._save_artifacts_enabled,
                "sample_rate": SAMPLE_RATE,
                "channels": CHANNELS,
                "sample_width_bits": SAMPLE_WIDTH * 8,
                "block_frames": DEFAULT_BLOCK_FRAMES,
                "wire_chunk_bytes": CHUNK_BYTES,
                "input_sample_rate": self._input_sample_rate,
                "input_channels": self._input_channels,
                "input_block_frames": self._input_block_frames,
                "resampling": (
                    self._input_sample_rate != SAMPLE_RATE
                    or self._input_channels != CHANNELS
                ),
            }
        snapshot.update(
            client.stats()
            if client is not None
            else dict(self._last_transport_stats)
        )
        return snapshot

    def _on_audio(self, indata: Any, _frames: int, _time_info: Any, status: Any) -> None:
        with self._lock:
            resampler = self._resampler
        if resampler is None:
            self._on_error("Microphone block arrived before audio conversion was ready")
            return
        try:
            pcm = resampler.process(indata)
            array = self._np.frombuffer(pcm, dtype="<i2")
            peak = float(self._np.abs(array.astype(self._np.int32)).max()) if array.size else 0.0
            dbfs = 20.0 * math.log10(peak / 32768.0) if peak > 0.0 else -99.0
        except Exception as exc:
            self._on_error(f"Microphone block processing failed: {exc}")
            return
        with self._lock:
            client = self._client
            self._blocks_captured += 1
            self._audio_level_dbfs = max(-99.0, dbfs)
            self._peak_level_dbfs = max(self._audio_level_dbfs, self._peak_level_dbfs - 0.5)
            if pcm and self._save_artifacts_enabled:
                self._recorded_pcm.append(pcm)
            if status:
                self._input_dropped += 1
                self._last_error = f"Microphone stream warning: {status}"
        if client is not None and pcm:
            client.feed(pcm)

    def _on_partial(self, text: str) -> None:
        with self._lock:
            self._partial_text = text
            self._events.append(
                {"type": "asr_partial", "stamp": _utc_now(), "text": text}
            )

    def _on_final_metadata(self, metadata: dict[str, Any]) -> None:
        with self._lock:
            self._pending_final_metadata = dict(metadata)

    def _on_final(self, text: str) -> None:
        normalized, corrections = correct(text.strip())
        normalized = normalized.strip()
        if not normalized:
            return
        with self._lock:
            metadata = self._pending_final_metadata or {
                "response_latency_ms": None,
                "latency_basis": "unavailable",
                "latency_correlated": False,
            }
            self._pending_final_metadata = None
            row = {
                "stamp": _utc_now(),
                "text": normalized,
                "postprocess_corrections": len(corrections),
                **metadata,
            }
            self._partial_text = ""
            self._finals.append(row)
            self._events.append({"type": "asr_final", **row})

    def _on_connection(self, connected: bool) -> None:
        with self._lock:
            self._connected = bool(connected)
            if connected:
                self._last_error = ""
            self._events.append(
                {
                    "type": "asr_connection",
                    "stamp": _utc_now(),
                    "connected": bool(connected),
                }
            )

    def _on_error(self, message: str) -> None:
        with self._lock:
            self._last_error = str(message)[:500]
            self._events.append(
                {"type": "asr_error", "stamp": _utc_now(), "message": self._last_error}
            )

    def _finish_stop(self) -> None:
        try:
            self._finish_stop_impl()
        finally:
            # The microphone stream is already closed before this worker is
            # launched. Never retain cross-container ownership because the
            # WebSocket worker or artifact writer failed during teardown.
            self._release_capture_lock()

    def _finish_stop_impl(self) -> None:
        with self._lock:
            client = self._client
        transport_stopped = True
        if client is not None:
            transport_result = client.stop()
            transport_stopped = transport_result is not False
            transport_stats = client.stats()
        else:
            transport_stats = dict(self._last_transport_stats)
        if not transport_stopped:
            self._on_error("ASR WebSocket worker did not stop cleanly")
            with self._lock:
                self._connected = False
                self._state = "ERROR"
            return
        try:
            recording_path, transcript_path = self._save_artifacts()
            stop_error = ""
        except Exception as exc:
            recording_path, transcript_path = "", ""
            stop_error = f"ASR artifact save failed: {exc}"
        with self._lock:
            self._client = None
            self._last_transport_stats = transport_stats
            self._connected = False
            self._stopped_monotonic = time.monotonic()
            self._recording_path = recording_path
            self._transcript_path = transcript_path
            if stop_error:
                self._state = "ERROR"
                self._last_error = stop_error
            else:
                self._state = "STOPPED"
            self._events.append(
                {
                    "type": "asr_stopped",
                    "stamp": _utc_now(),
                    "recording_path": recording_path,
                    "transcript_path": transcript_path,
                    "final_count": len(self._finals),
                    "error": stop_error,
                }
            )

    def _save_artifacts(self) -> tuple[str, str]:
        with self._lock:
            pcm = b"".join(self._recorded_pcm)
            finals = list(self._finals)
            self._recorded_pcm = []
        if not self._save_artifacts_enabled:
            return "", ""
        if not pcm and not finals:
            return "", ""
        self._output_dir.mkdir(parents=True, exist_ok=True)
        stem = datetime.now(timezone.utc).strftime("asr_%Y%m%dT%H%M%SZ")
        wav_path = self._output_dir / f"{stem}.wav"
        txt_path = self._output_dir / f"{stem}.txt"
        if pcm:
            with wave.open(str(wav_path), "wb") as stream:
                stream.setnchannels(CHANNELS)
                stream.setsampwidth(SAMPLE_WIDTH)
                stream.setframerate(SAMPLE_RATE)
                stream.writeframes(pcm)
        else:
            wav_path = Path()
        if finals:
            txt_path.write_text(
                "\n".join(str(row["text"]) for row in finals) + "\n",
                encoding="utf-8",
            )
        else:
            txt_path = Path()
        return (
            str(wav_path) if str(wav_path) != "." else "",
            str(txt_path) if str(txt_path) != "." else "",
        )

    def _acquire_capture_lock(self) -> None:
        path = self._capture_lock_path
        if path is None:
            return
        with self._lock:
            if self._capture_lock_fd is not None:
                raise RuntimeError("microphone capture lock is already held")
        path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise RuntimeError(
                "microphone capture is already owned by another Taskplanner ASR runtime"
            ) from exc
        except Exception:
            os.close(fd)
            raise
        with self._lock:
            self._capture_lock_fd = fd

    def _release_capture_lock(self) -> None:
        with self._lock:
            fd = self._capture_lock_fd
            self._capture_lock_fd = None
        if fd is None:
            return
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(fd)
