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
import re
import subprocess
import threading
import time
from typing import Any
import wave

from integration_debug.asr_endpoints import validate_websocket_url
from integration_debug.puzzle_asr_postprocess import (
    KEYWORDS,
    correct,
)


SAMPLE_RATE = 16_000
CHANNELS = 1
SAMPLE_WIDTH = 2
CHUNK_FRAMES = 4_096
CHUNK_BYTES = CHUNK_FRAMES * SAMPLE_WIDTH * CHANNELS
DEFAULT_BLOCK_FRAMES = 1_600
# Artifact capture is a diagnostic window, not an unbounded in-memory audio
# archive.  At 16 kHz mono PCM16 this retains the newest ten minutes (about
# 19 MiB) until the operator stops recording, while the ASR transport itself
# continues to receive every callback block.
DEFAULT_RECORDING_MAX_SECONDS = 10.0 * 60.0
# This is deliberately only a local diagnostic marker.  It is a callback-time
# dBFS threshold crossing, not a claim about the physical start of speech.
DEFAULT_LOCAL_ONSET_DBFS = -45.0
LOCAL_ONSET_BASIS = "audio_callback_dbfs_threshold_crossing_approximate"
# PortAudio invokes its callback from a real-time-ish audio thread.  Never
# enqueue one asyncio callback per microphone block: when the loop is briefly
# busy those callbacks themselves become an unbounded, increasingly stale
# audio backlog.  Retain a ten-second bridge window before discarding stale
# PCM, and schedule a single loop-side drain for it.
DEFAULT_PCM_INGRESS_MAX_AGE_SEC = 10.0
# Capture callbacks are nominally 100 ms.  Keep enough slots that this count
# cap cannot evict fresh PCM before the ten-second age policy does.
DEFAULT_PCM_QUEUE_MAX = 128
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


def _configured_pipewire_source_id(status_output: str) -> str | None:
    """Find the selected logical Source's current PipeWire object id.

    WirePlumber 1.6 can report the sink for ``@DEFAULT_AUDIO_SOURCE@`` even
    though its Settings section contains a valid selected input.  Resolve that
    selected *logical* source from ``wpctl status --name`` rather than falling
    back to a raw ALSA device or an arbitrary microphone.
    """

    configured_name = ""
    source_ids_by_name: dict[str, str] = {}
    in_sources = False
    for raw_line in status_output.splitlines():
        line = raw_line.replace("│", " ").strip()
        if line.endswith("Sources:"):
            in_sources = True
            continue
        if in_sources and line.endswith(":"):
            in_sources = False
        if in_sources:
            match = re.match(r"^\*?\s*(\d+)\.\s+(\S+)", line)
            if match:
                source_ids_by_name[match.group(2)] = match.group(1)
        if "Audio/Source" in line:
            configured_name = line.split("Audio/Source", 1)[1].strip()
    return source_ids_by_name.get(configured_name) or None


def _run_wpctl_status() -> str:
    """Return named PipeWire graph status or classify an unreachable server."""

    try:
        return str(
            subprocess.run(
                ["wpctl", "status", "--name"],
                check=True,
                capture_output=True,
                text=True,
                timeout=2.0,
            ).stdout
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AudioInputUnavailable(
            DEVICE_STATUS_HOST_AUDIO_UNAVAILABLE,
            "Ubuntu PipeWire is not reachable from Debug Mode",
        ) from exc


def _query_pipewire_default_source() -> dict[str, Any]:
    """Return Ubuntu's effective logical input without exposing raw ALSA ports."""

    properties: dict[str, str] = {}
    try:
        completed = subprocess.run(
            ["wpctl", "inspect", PIPEWIRE_DEFAULT_SOURCE],
            check=True,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except subprocess.CalledProcessError as exc:
        # ``wpctl inspect @DEFAULT_AUDIO_SOURCE@`` may fail when there is no
        # Source, and WirePlumber 1.6 can also resolve it to the default Sink.
        # The named Settings view below distinguishes a real selected input
        # from both states without broadening capture to an arbitrary device.
        del exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise AudioInputUnavailable(
            DEVICE_STATUS_HOST_AUDIO_UNAVAILABLE,
            "Ubuntu PipeWire is not reachable from Debug Mode",
        ) from exc
    else:
        properties = _parse_wpctl_properties(completed.stdout)

    if properties.get("media.class") != "Audio/Source":
        status_output = _run_wpctl_status()
        source_id = _configured_pipewire_source_id(status_output)
        if not source_id:
            raise AudioInputUnavailable(
                DEVICE_STATUS_NO_INPUT,
                "Ubuntu currently exposes no PipeWire microphone input",
            )
        try:
            fallback = subprocess.run(
                ["wpctl", "inspect", source_id],
                check=True,
                capture_output=True,
                text=True,
                timeout=2.0,
            )
        except subprocess.CalledProcessError as exc:
            raise AudioInputUnavailable(
                DEVICE_STATUS_NO_INPUT,
                "Ubuntu currently exposes no PipeWire microphone input",
            ) from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise AudioInputUnavailable(
                DEVICE_STATUS_HOST_AUDIO_UNAVAILABLE,
                "Ubuntu PipeWire is not reachable from Debug Mode",
            ) from exc
        properties = _parse_wpctl_properties(fallback.stdout)
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
    """Prefer a full-channel wire capture, then native device formats.

    Some PipeWire logical inputs expose a stereo capture stream even when the
    microphone signal is wired to only one channel.  Requesting mono in that
    situation lets PortAudio choose its first channel, which can be silent.
    Capture stereo first and do the channel-aware mono conversion locally.
    """

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

    preferred_channels = [1]
    if max_channels >= 2:
        preferred_channels.insert(0, 2)

    raw_candidates: list[tuple[int, int]] = [
        (SAMPLE_RATE, channels) for channels in preferred_channels
    ]
    if native_rate > 0:
        # Some USB interfaces accept mono at their native rate, while others
        # expose only stereo or their full channel layout. Keep the full
        # two-channel input ahead of mono so an active right-only microphone
        # channel is never discarded at device-open time.
        native_channels = list(preferred_channels)
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

        samples = block.astype(np.float64, copy=False)
        if self.input_channels == 1:
            mono = samples[:, 0]
        else:
            # Preserve conventional averaging for an actual stereo signal,
            # but do not halve (or effectively discard) a microphone wired to
            # only one channel.  This is common for PipeWire USB inputs where
            # FL is silent and FR carries the microphone.
            channel_energy = np.mean(np.abs(samples), axis=0)
            strongest_channel = int(np.argmax(channel_energy))
            strongest_energy = float(channel_energy[strongest_channel])
            weakest_energy = float(np.min(channel_energy))
            if strongest_energy > 0.0 and weakest_energy <= strongest_energy * 0.15:
                mono = samples[:, strongest_channel]
            else:
                mono = samples.mean(axis=1)
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
        queue_max: int = DEFAULT_PCM_QUEUE_MAX,
        ingress_max_age_sec: float = DEFAULT_PCM_INGRESS_MAX_AGE_SEC,
        reconnect_delay_sec: float = 2.0,
        eof_wait_sec: float = 15.0,
        rollover_after_final: bool = False,
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
        # Puzzle's server-final frames are sentence boundaries, not transport
        # failures.  Keep the capture session persistent by default; a fresh
        # WebSocket after every transcript is available only for explicit
        # server-behavior comparison.
        self._rollover_after_final = bool(rollover_after_final)
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._run_task: asyncio.Task[Any] | None = None
        self._queue: asyncio.Queue[tuple[int, bytes]] | None = None
        self._ready = threading.Event()
        self._connected = threading.Event()
        self._stopping = False
        self._ingress_lock = threading.Lock()
        self._ingress: deque[tuple[int, bytes]] = deque()
        self._ingress_drain_scheduled = False
        self._ingress_max_age_ns = int(
            max(0.05, float(ingress_max_age_sec)) * 1_000_000_000
        )
        self._stats_lock = threading.Lock()
        self._sent = 0
        self._responses = 0
        self._dropped = 0
        self._ingress_dropped = 0
        self._stale_ingress_dropped = 0
        self._queue_dropped = 0
        self._stale_queue_dropped = 0
        self._sessions = 0
        self._normal_rollovers = 0
        self._padded_final_bytes = 0
        # Keep the transport-side remainder across an optional server-final
        # rollover.  A server final is a sentence boundary, not permission to
        # discard microphone PCM already queued for the next sentence.
        self._send_buffer = bytearray()
        self._send_buffer_captured_monotonic_ns = 0
        self._last_partial = ""
        self._last_changed_partial_received_monotonic_ns = 0
        self._last_audio_sent_monotonic_ns = 0
        self._local_onset_monotonic_ns = 0
        self._local_onset_dbfs: float | None = None
        self._local_onset_threshold_dbfs: float | None = None
        self._awaiting_first_changed_partial = False
        self._last_local_onset_to_first_partial_ms: float | None = None

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

    def feed(
        self,
        data: bytes,
        *,
        captured_monotonic_ns: int | None = None,
    ) -> None:
        """Accept a microphone block without building a loop callback backlog.

        This is called on the PortAudio callback thread.  It only appends to a
        bounded local deque and, when needed, schedules one loop callback.
        Both the deque and the loop queue enforce a newest-audio policy, so a
        transient network or event-loop stall is observable as dropped audio
        rather than turning into ever-growing recognition latency.
        """

        if self._stopping or not data or not self._ready.is_set():
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        captured_ns = int(captured_monotonic_ns or time.monotonic_ns())
        if captured_ns <= 0:
            captured_ns = time.monotonic_ns()
        now_ns = time.monotonic_ns()
        schedule_drain = False
        with self._ingress_lock:
            self._drop_stale_ingress_locked(now_ns)
            while len(self._ingress) >= self._queue_max:
                self._ingress.popleft()
                self._record_ingress_drop_locked(stale=False)
            self._ingress.append((captured_ns, bytes(data)))
            if not self._ingress_drain_scheduled:
                self._ingress_drain_scheduled = True
                schedule_drain = True
        if schedule_drain:
            try:
                loop.call_soon_threadsafe(self._drain_ingress)
            except RuntimeError:
                # Loop shutdown races are normal during ASR stop.  Drop the
                # locally buffered PCM instead of retaining it for a future
                # session.
                with self._ingress_lock:
                    self._drop_all_ingress_locked()
                    self._ingress_drain_scheduled = False

    def note_local_audio_onset(
        self,
        captured_monotonic_ns: int,
        *,
        dbfs: float,
        threshold_dbfs: float,
    ) -> None:
        """Record an approximate local onset for diagnostic latency only.

        The timestamp comes from the PortAudio callback after a local dBFS
        threshold crossing.  It never changes server VAD, PCM framing, or the
        final-only command path.
        """

        timestamp = int(captured_monotonic_ns)
        if timestamp <= 0:
            return
        with self._stats_lock:
            if self._awaiting_first_changed_partial:
                return
            self._local_onset_monotonic_ns = timestamp
            self._local_onset_dbfs = round(float(dbfs), 1)
            self._local_onset_threshold_dbfs = round(float(threshold_dbfs), 1)
            self._awaiting_first_changed_partial = True

    def pending(self) -> int:
        with self._ingress_lock:
            ingress = len(self._ingress)
        queue_size = self._queue.qsize() if self._queue is not None else 0
        return queue_size + ingress

    def stats(self) -> dict[str, Any]:
        with self._stats_lock:
            snapshot = {
                "sent_chunks": self._sent,
                "responses": self._responses,
                "dropped_chunks": self._dropped,
                "ingress_dropped_chunks": self._ingress_dropped,
                "stale_ingress_dropped_chunks": self._stale_ingress_dropped,
                "queue_dropped_chunks": self._queue_dropped,
                "stale_queue_dropped_chunks": self._stale_queue_dropped,
                "pcm_ingress_max_age_ms": round(
                    self._ingress_max_age_ns / 1_000_000.0, 1
                ),
                "sessions": self._sessions,
                "normal_rollovers": self._normal_rollovers,
                "padded_final_bytes": self._padded_final_bytes,
                "connected": self._connected.is_set(),
                "local_onset_to_first_partial_ms": (
                    self._last_local_onset_to_first_partial_ms
                ),
                "local_onset_basis": LOCAL_ONSET_BASIS,
                "local_onset_dbfs": self._local_onset_dbfs,
                "local_onset_threshold_dbfs": self._local_onset_threshold_dbfs,
            }
        # Do not acquire ``_ingress_lock`` while holding ``_stats_lock``:
        # PortAudio-side drops take the locks in the opposite order.
        snapshot["pending_chunks"] = self.pending()
        return snapshot

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
        with self._ingress_lock:
            self._drop_all_ingress_locked()
            self._ingress_drain_scheduled = False
        self._send_buffer.clear()
        self._send_buffer_captured_monotonic_ns = 0
        self._ready.clear()
        self._connected.clear()
        self._on_connection(False)
        return True

    def _record_ingress_drop_locked(self, *, stale: bool) -> None:
        """Record a bridge drop while ``_ingress_lock`` is held."""

        with self._stats_lock:
            self._dropped += 1
            self._ingress_dropped += 1
            if stale:
                self._stale_ingress_dropped += 1

    def _record_queue_drop(self, *, stale: bool) -> None:
        with self._stats_lock:
            self._dropped += 1
            self._queue_dropped += 1
            if stale:
                self._stale_queue_dropped += 1

    def _drop_stale_ingress_locked(self, now_ns: int) -> None:
        cutoff_ns = int(now_ns) - self._ingress_max_age_ns
        while self._ingress and self._ingress[0][0] < cutoff_ns:
            self._ingress.popleft()
            self._record_ingress_drop_locked(stale=True)

    def _drop_all_ingress_locked(self) -> None:
        while self._ingress:
            self._ingress.popleft()
            self._record_ingress_drop_locked(stale=False)

    def _drain_ingress(self) -> None:
        """Move a bounded batch from callback-thread ingress to asyncio.

        Only this method touches the asyncio queue.  It drains at most one
        bridge window per event-loop turn, then reschedules itself if callback
        ingress refilled meanwhile.  That keeps a sustained microphone stream
        from monopolizing the event loop and starving the websocket sender or
        receiver while retaining the one-outstanding-drain invariant.
        """
        now_ns = time.monotonic_ns()
        with self._ingress_lock:
            self._drop_stale_ingress_locked(now_ns)
            if not self._ingress:
                self._ingress_drain_scheduled = False
                return
            batch: list[tuple[int, bytes]] = []
            while self._ingress and len(batch) < self._queue_max:
                batch.append(self._ingress.popleft())

        queue = self._queue
        if queue is None:
            with self._ingress_lock:
                for _captured_ns, _data in batch:
                    self._record_ingress_drop_locked(stale=False)
        else:
            for captured_ns, data in batch:
                if now_ns - captured_ns > self._ingress_max_age_ns:
                    with self._ingress_lock:
                        self._record_ingress_drop_locked(stale=True)
                    continue
                try:
                    queue.put_nowait((captured_ns, data))
                    continue
                except asyncio.QueueFull:
                    pass
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                self._record_queue_drop(stale=False)
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait((captured_ns, data))

        with self._ingress_lock:
            self._drop_stale_ingress_locked(time.monotonic_ns())
            if not self._ingress:
                self._ingress_drain_scheduled = False
                return
        loop = self._loop
        if loop is None or loop.is_closed():
            with self._ingress_lock:
                self._drop_all_ingress_locked()
                self._ingress_drain_scheduled = False
            return
        try:
            # ``call_soon_threadsafe`` is intentional even on the loop thread:
            # it keeps the fake-loop unit seam and the callback-side path on
            # the same narrow scheduling API.
            loop.call_soon_threadsafe(self._drain_ingress)
        except RuntimeError:
            with self._ingress_lock:
                self._drop_all_ingress_locked()
                self._ingress_drain_scheduled = False

    def _drain(self) -> None:
        queue = self._queue
        if queue is not None:
            while True:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
        with self._ingress_lock:
            self._drop_all_ingress_locked()
            self._ingress_drain_scheduled = False

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
            self._last_changed_partial_received_monotonic_ns = 0
            self._last_audio_sent_monotonic_ns = 0
            self._connected.set()
            self._on_connection(True)
            rollover_requested = (
                asyncio.Event() if self._rollover_after_final else None
            )
            tasks = [
                asyncio.create_task(
                    self._sender(websocket, rollover_requested),
                    name="debug-asr-sender",
                ),
                asyncio.create_task(
                    self._receiver(websocket, rollover_requested),
                    name="debug-asr-receiver",
                ),
            ]
            done: set[asyncio.Task[Any]] = set()
            pending: set[asyncio.Task[Any]] = set(tasks)
            normal_rollover = False
            try:
                done, pending = await asyncio.wait(
                    tasks,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                normal_rollover = bool(
                    rollover_requested is not None
                    and rollover_requested.is_set()
                    and not self._stopping
                )
                if normal_rollover and pending:
                    # The receiver returned after a server final.  Give the
                    # sender one short poll interval to retain a dequeued
                    # block in ``_send_buffer`` and exit without sending it
                    # to the old session.  There is no EOF and no reconnect
                    # delay on this normal sentence rollover.
                    finished, pending = await asyncio.wait(pending, timeout=0.3)
                    done |= finished
                elif self._stopping and pending:
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
            if normal_rollover:
                with self._stats_lock:
                    self._normal_rollovers += 1

    async def _sender(
        self,
        websocket: Any,
        rollover_requested: asyncio.Event | None = None,
    ) -> None:
        buffer = self._send_buffer
        while True:
            if (
                rollover_requested is not None
                and rollover_requested.is_set()
                and not self._stopping
            ):
                return
            queue = self._queue
            if queue is None:
                return
            now_ns = time.monotonic_ns()
            if (
                buffer
                and self._send_buffer_captured_monotonic_ns > 0
                and now_ns - self._send_buffer_captured_monotonic_ns
                > self._ingress_max_age_ns
            ):
                # This is an intentionally lossy live-audio boundary.  Sending
                # old PCM would make every later partial/final lag behind the
                # microphone and eventually reproduce the reported slowdown.
                buffer.clear()
                self._send_buffer_captured_monotonic_ns = 0
                self._record_queue_drop(stale=True)
            try:
                queued = await asyncio.wait_for(queue.get(), timeout=0.2)
            except asyncio.TimeoutError:
                queued = None
            if isinstance(queued, tuple) and len(queued) == 2:
                captured_ns, data = int(queued[0]), bytes(queued[1])
            elif queued is None:
                captured_ns, data = 0, b""
            else:
                # Keep direct unit tests and one-off local adapters that used
                # the old bytes-only queue shape working; production ingress
                # always supplies the capture timestamp above.
                captured_ns, data = time.monotonic_ns(), bytes(queued)
            if data:
                if time.monotonic_ns() - captured_ns > self._ingress_max_age_ns:
                    self._record_queue_drop(stale=True)
                    continue
                if not buffer:
                    self._send_buffer_captured_monotonic_ns = captured_ns
                buffer += data
            if (
                rollover_requested is not None
                and rollover_requested.is_set()
                and not self._stopping
            ):
                # If queue.get() won the race with the final, its bytes are
                # already retained in the persistent buffer for the next
                # session.  Do not send them through the just-finalized one.
                return
            while len(buffer) >= CHUNK_BYTES:
                if (
                    rollover_requested is not None
                    and rollover_requested.is_set()
                    and not self._stopping
                ):
                    return
                await websocket.send(bytes(buffer[:CHUNK_BYTES]))
                self._last_audio_sent_monotonic_ns = time.monotonic_ns()
                del buffer[:CHUNK_BYTES]
                if not buffer:
                    self._send_buffer_captured_monotonic_ns = 0
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
                    buffer.clear()
                    self._send_buffer_captured_monotonic_ns = 0
                await websocket.send(json.dumps({"eof": True}))
                return

    def _clear_pending_local_onset(self) -> None:
        with self._stats_lock:
            self._local_onset_monotonic_ns = 0
            self._awaiting_first_changed_partial = False

    def _record_first_changed_partial_latency(
        self, received_monotonic_ns: int
    ) -> None:
        with self._stats_lock:
            onset_ns = self._local_onset_monotonic_ns
            if not self._awaiting_first_changed_partial or onset_ns <= 0:
                return
            delta_ns = int(received_monotonic_ns) - onset_ns
            self._last_local_onset_to_first_partial_ms = (
                round(delta_ns / 1_000_000.0, 1) if delta_ns >= 0 else None
            )
            self._local_onset_monotonic_ns = 0
            self._awaiting_first_changed_partial = False

    async def _receiver(
        self,
        websocket: Any,
        rollover_requested: asyncio.Event | None = None,
    ) -> None:
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
                partial_to_final_delta_ns = (
                    received_monotonic_ns
                    - self._last_changed_partial_received_monotonic_ns
                )
                last_changed_partial_to_final_ms = (
                    round(partial_to_final_delta_ns / 1_000_000.0, 1)
                    if self._last_changed_partial_received_monotonic_ns > 0
                    and partial_to_final_delta_ns >= 0
                    else None
                )
                self._last_partial = ""
                self._last_changed_partial_received_monotonic_ns = 0
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
                                "last_changed_partial_to_final_ms": (
                                    last_changed_partial_to_final_ms
                                ),
                            }
                        )
                    self._on_final(text)
                self._clear_pending_local_onset()
                # Empty server finals are ordinary silence/no-speech VAD
                # boundaries.  A nonempty final also stays on this persistent
                # capture session unless explicit comparison rollover is on.
                if (
                    text
                    and self._rollover_after_final
                    and rollover_requested is not None
                ):
                    rollover_requested.set()
                    return
                continue
            if text and text != self._last_partial:
                self._last_partial = text
                self._last_changed_partial_received_monotonic_ns = received_monotonic_ns
                self._record_first_changed_partial_latency(received_monotonic_ns)
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
        recording_default_active: bool = True,
        recording_max_seconds: float = DEFAULT_RECORDING_MAX_SECONDS,
        capture_lock_path: str | Path | None = None,
        local_onset_threshold_dbfs: float = DEFAULT_LOCAL_ONSET_DBFS,
        rollover_after_final: bool = False,
    ) -> None:
        self._np, self._sd, self._websockets, dependency_error = _optional_audio_modules()
        self._default_url = validate_websocket_url(default_url)
        self._topic = str(topic)
        self._output_dir = Path(output_dir)
        self._save_artifacts_enabled = bool(save_artifacts)
        self._recording_default_active = bool(
            recording_default_active and self._save_artifacts_enabled
        )
        self._recording_max_seconds = float(recording_max_seconds)
        if (
            not math.isfinite(self._recording_max_seconds)
            or self._recording_max_seconds < 1.0
        ):
            raise ValueError("recording_max_seconds must be finite and at least one second")
        self._recording_max_bytes = max(
            SAMPLE_WIDTH * CHANNELS,
            int(round(
                self._recording_max_seconds
                * SAMPLE_RATE
                * SAMPLE_WIDTH
                * CHANNELS
            )),
        )
        self._rollover_after_final = bool(rollover_after_final)
        self._local_onset_dbfs_threshold = float(local_onset_threshold_dbfs)
        if not math.isfinite(self._local_onset_dbfs_threshold):
            raise ValueError("local_onset_threshold_dbfs must be finite")
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
        self._local_voice_active = False
        self._blocks_captured = 0
        self._input_dropped = 0
        self._partial_text = ""
        self._finals: deque[dict[str, Any]] = deque(maxlen=40)
        self._recorded_pcm: deque[bytes] = deque()
        self._recorded_pcm_bytes = 0
        self._recording_dropped_bytes = 0
        self._recording_truncated = False
        self._recorded_finals: list[dict[str, Any]] = []
        self._recording_active = False
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
            "ingress_dropped_chunks": 0,
            "stale_ingress_dropped_chunks": 0,
            "queue_dropped_chunks": 0,
            "stale_queue_dropped_chunks": 0,
            "pcm_ingress_max_age_ms": round(
                DEFAULT_PCM_INGRESS_MAX_AGE_SEC * 1000.0, 1
            ),
            "sessions": 0,
            "normal_rollovers": 0,
            "padded_final_bytes": 0,
            "pending_chunks": 0,
            "connected": False,
            "local_onset_to_first_partial_ms": None,
            "local_onset_basis": LOCAL_ONSET_BASIS,
            "local_onset_dbfs": None,
            "local_onset_threshold_dbfs": round(
                self._local_onset_dbfs_threshold, 1
            ),
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
            self._local_voice_active = False
            self._blocks_captured = 0
            self._input_dropped = 0
            self._partial_text = ""
            self._finals.clear()
            self._reset_recording_buffer()
            self._recorded_finals = []
            self._recording_active = self._recording_default_active
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
                "ingress_dropped_chunks": 0,
                "stale_ingress_dropped_chunks": 0,
                "queue_dropped_chunks": 0,
                "stale_queue_dropped_chunks": 0,
                "pcm_ingress_max_age_ms": round(
                    DEFAULT_PCM_INGRESS_MAX_AGE_SEC * 1000.0, 1
                ),
                "sessions": 0,
                "normal_rollovers": 0,
                "padded_final_bytes": 0,
                "pending_chunks": 0,
                "connected": False,
                "local_onset_to_first_partial_ms": None,
                "local_onset_basis": LOCAL_ONSET_BASIS,
                "local_onset_dbfs": None,
                "local_onset_threshold_dbfs": round(
                    self._local_onset_dbfs_threshold, 1
                ),
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
                rollover_after_final=self._rollover_after_final,
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
                self._recording_active = False
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

    def start_recording(self) -> dict[str, Any]:
        """Begin a bounded artifact window without restarting microphone ASR."""

        with self._lock:
            if not self._save_artifacts_enabled:
                raise RuntimeError("ASR recording artifacts are disabled")
            if self._state != "LISTENING":
                raise RuntimeError("ASR microphone must be LISTENING before recording")
            if self._recording_active:
                raise RuntimeError("ASR recording is already active")
            self._reset_recording_buffer()
            self._recorded_finals = []
            self._recording_path = ""
            self._transcript_path = ""
            self._recording_active = True
            self._events.append(
                {"type": "asr_recording_started", "stamp": _utc_now()}
            )
        return self.snapshot()

    def stop_recording(self) -> dict[str, Any]:
        """Close the current artifact window and synchronously persist it."""

        with self._lock:
            if not self._recording_active:
                raise RuntimeError("ASR recording is not active")
            self._recording_active = False
        recording_path, transcript_path = self._save_artifacts()
        with self._lock:
            self._recording_path = recording_path
            self._transcript_path = transcript_path
            self._events.append(
                {
                    "type": "asr_recording_stopped",
                    "stamp": _utc_now(),
                    "recording_path": recording_path,
                    "transcript_path": transcript_path,
                }
            )
        return self.snapshot()

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
                "recording_active": self._recording_active,
                "recording_max_sec": round(self._recording_max_seconds, 1),
                "recording_buffered_sec": round(
                    self._recorded_pcm_bytes / (SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS),
                    3,
                ),
                "recording_dropped_sec": round(
                    self._recording_dropped_bytes / (SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS),
                    3,
                ),
                "recording_truncated": self._recording_truncated,
                "sample_rate": SAMPLE_RATE,
                "channels": CHANNELS,
                "sample_width_bits": SAMPLE_WIDTH * 8,
                "block_frames": DEFAULT_BLOCK_FRAMES,
                "wire_chunk_bytes": CHUNK_BYTES,
                "local_onset_basis": LOCAL_ONSET_BASIS,
                "local_onset_threshold_dbfs": round(
                    self._local_onset_dbfs_threshold, 1
                ),
                "local_onset_to_first_partial_ms": None,
                "local_onset_dbfs": None,
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
        # The runtime owns the configured threshold.  The transport only sees
        # a threshold after the first crossing, so keep this configuration
        # field stable before and after that diagnostic event.
        snapshot["local_onset_basis"] = LOCAL_ONSET_BASIS
        snapshot["local_onset_threshold_dbfs"] = round(
            self._local_onset_dbfs_threshold, 1
        )
        return snapshot

    def _on_audio(self, indata: Any, _frames: int, _time_info: Any, status: Any) -> None:
        captured_monotonic_ns = time.monotonic_ns()
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
            local_voice_active = dbfs >= self._local_onset_dbfs_threshold
            local_voice_started = local_voice_active and not self._local_voice_active
            self._local_voice_active = local_voice_active
            self._blocks_captured += 1
            self._audio_level_dbfs = max(-99.0, dbfs)
            self._peak_level_dbfs = max(self._audio_level_dbfs, self._peak_level_dbfs - 0.5)
            if pcm and self._save_artifacts_enabled and self._recording_active:
                self._append_recording_pcm(pcm)
            if status:
                self._input_dropped += 1
                self._last_error = f"Microphone stream warning: {status}"
        if client is not None and pcm:
            if local_voice_started:
                note_onset = getattr(client, "note_local_audio_onset", None)
                if callable(note_onset):
                    note_onset(
                        captured_monotonic_ns,
                        dbfs=dbfs,
                        threshold_dbfs=self._local_onset_dbfs_threshold,
                    )
            client.feed(pcm, captured_monotonic_ns=captured_monotonic_ns)

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
        raw_text = str(text or "").strip()
        if not raw_text:
            return
        # The ASR postprocess table is the single lexical normalization source
        # for finalized utterances.  Publish its canonical text directly so
        # the dashboard, resolver, and typed SpeechUtterance all observe the
        # same result.
        corrected_text, corrections = correct(raw_text)
        corrected_text = corrected_text.strip()
        if not corrected_text:
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
                "text": corrected_text,
                "raw_text": raw_text,
                "corrected_text": corrected_text,
                "postprocess_corrections": len(corrections),
                "postprocess_correction_pairs": tuple(corrections),
                "postprocess_command_correction_pairs": tuple(corrections),
                "postprocess_applied_to_command": bool(corrections),
                **metadata,
            }
            self._partial_text = ""
            self._finals.append(row)
            if self._save_artifacts_enabled and self._recording_active:
                self._recorded_finals.append(dict(row))
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
        with self._lock:
            self._recording_active = False
            self._local_voice_active = False
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
            if recording_path:
                self._recording_path = recording_path
            if transcript_path:
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
                    "recording_path": self._recording_path,
                    "transcript_path": self._transcript_path,
                    "final_count": len(self._finals),
                    "error": stop_error,
                }
            )

    def _save_artifacts(self) -> tuple[str, str]:
        with self._lock:
            pcm = b"".join(self._recorded_pcm)
            finals = list(self._recorded_finals)
            self._recorded_pcm.clear()
            self._recorded_pcm_bytes = 0
            self._recorded_finals = []
        if not self._save_artifacts_enabled:
            return "", ""
        if not pcm and not finals:
            return "", ""
        self._output_dir.mkdir(parents=True, exist_ok=True)
        stem = datetime.now(timezone.utc).strftime("asr_%Y%m%dT%H%M%S_%fZ")
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

    def _reset_recording_buffer(self) -> None:
        """Start a new bounded artifact window while holding ``_lock``."""

        self._recorded_pcm.clear()
        self._recorded_pcm_bytes = 0
        self._recording_dropped_bytes = 0
        self._recording_truncated = False

    def _append_recording_pcm(self, pcm: bytes) -> None:
        """Keep only the newest configured artifact window while holding ``_lock``."""

        if not pcm:
            return
        # A callback can be larger than a deliberately small configured
        # window.  Trim from its beginning on PCM16 sample boundaries before
        # adding it to the ring buffer.
        if len(pcm) > self._recording_max_bytes:
            dropped = len(pcm) - self._recording_max_bytes
            dropped -= dropped % (SAMPLE_WIDTH * CHANNELS)
            if dropped:
                self._recording_dropped_bytes += dropped
                pcm = pcm[dropped:]
        self._recorded_pcm.append(pcm)
        self._recorded_pcm_bytes += len(pcm)
        dropped_any = False
        while self._recorded_pcm and self._recorded_pcm_bytes > self._recording_max_bytes:
            removed = self._recorded_pcm.popleft()
            self._recorded_pcm_bytes -= len(removed)
            self._recording_dropped_bytes += len(removed)
            dropped_any = True
        if dropped_any or self._recording_dropped_bytes:
            if not self._recording_truncated:
                self._events.append(
                    {
                        "type": "asr_recording_window_truncated",
                        "stamp": _utc_now(),
                        "max_seconds": self._recording_max_seconds,
                    }
                )
            self._recording_truncated = True

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
