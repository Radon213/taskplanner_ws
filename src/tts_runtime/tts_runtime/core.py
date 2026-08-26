"""Dependency-free durable queue and playback state machine.

ROS conversion is intentionally kept out of this module so crash recovery,
deduplication, cache behavior and state transitions can be tested without a ROS
graph or an audio device.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from queue import Queue
import sqlite3
import threading
import time
from typing import Callable, Protocol
import uuid
import wave


VALID_TIMINGS = frozenset(
    {"immediate", "on_function_accepted", "on_function_completed"}
)
TERMINAL_STATES = frozenset({"played", "failed"})
LEGACY_GATEWAY_INSTANCE_ID = "legacy-local-gateway"
SUPERTONIC_MODEL_FILES = (
    "onnx/duration_predictor.onnx",
    "onnx/text_encoder.onnx",
    "onnx/vector_estimator.onnx",
    "onnx/vocoder.onnx",
    "onnx/tts.json",
    "onnx/unicode_indexer.json",
)


@dataclass(frozen=True)
class ReplyRequest:
    reply_id: str
    turn_id: str
    utterance_id: str
    procedure_run_id: str
    text: str
    timing: str
    function_call_name: str = ""
    function_arguments_json: str = ""
    function_request_id: str = ""
    # Kept last with a compatibility default so older local callers still
    # construct the dataclass. Production admission replaces this sentinel
    # with the exact GatewayInfo epoch before persistence.
    gateway_instance_id: str = LEGACY_GATEWAY_INSTANCE_ID


@dataclass(frozen=True)
class RuntimeConfig:
    model_identity: str
    voice_id: str = "F1"
    language: str = "ko"
    steps: int = 8
    speed: float = 1.05
    silence_duration: float = 0.3
    output_device: str = "default"


@dataclass(frozen=True)
class SynthesisResult:
    synth_latency_ms: float
    audio_duration_sec: float


@dataclass(frozen=True)
class PlaybackResult:
    playback_latency_ms: float


@dataclass(frozen=True)
class PrewarmResult:
    text: str
    cache_key: str
    wav_path: Path
    cache_hit: bool
    synth_latency_ms: float
    audio_duration_sec: float


@dataclass(frozen=True)
class PlaybackEvent:
    sequence: int
    reply_id: str
    turn_id: str
    utterance_id: str
    procedure_run_id: str
    function_call_name: str
    function_arguments_json: str
    function_request_id: str
    state: str
    timing: str
    text: str
    text_sha256: str
    voice_id: str
    output_device: str
    cache_key: str
    synth_latency_ms: float
    audio_duration_sec: float
    playback_latency_ms: float
    terminal: bool
    success: bool
    error_code: str
    message: str
    gateway_instance_id: str = ""


@dataclass(frozen=True)
class EnqueueResult:
    accepted: bool
    duplicate: bool
    event: PlaybackEvent


class Synthesizer(Protocol):
    def synthesize(self, text: str, output_path: Path, seed: int) -> SynthesisResult:
        """Write one valid WAV file to output_path."""

    def close(self) -> None:
        """Release resident model resources."""


class Player(Protocol):
    def arm(self) -> None:
        """Arm exactly one playback attempt before it can spawn a process."""

    def play(self, wav_path: Path) -> PlaybackResult:
        """Block until playback completes or raise an exception."""

    def interrupt(self) -> bool:
        """Cancel the armed/current attempt; return whether one existed."""


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _best_effort_mode(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        pass


def supertonic_model_manifest_sha256(
    model_dir: Path | str,
    *,
    voice_id: str,
) -> str:
    """Hash every selected model artifact without including its mount path."""

    root = Path(model_dir).expanduser().resolve()
    relative_paths = (*SUPERTONIC_MODEL_FILES, f"voice_styles/{voice_id}.json")
    manifest: dict[str, str] = {}
    for relative in relative_paths:
        path = root / relative
        if not path.is_file():
            raise FileNotFoundError(f"Supertonic model artifact not found: {path}")
        manifest[relative] = _sha256_file(path)
    return _sha256_text(_canonical_json(manifest))


def cache_key_for(text: str, config: RuntimeConfig) -> str:
    """Return a stable key for all inputs that affect generated samples."""

    material = {
        "cache_schema": "taskplanner.supertonic.wav.v1",
        "engine": "supertonic-3",
        "language": config.language,
        "model_identity": config.model_identity,
        "seed_strategy": "cache-key-prefix-v1",
        "silence_duration": config.silence_duration,
        "speed": config.speed,
        "steps": config.steps,
        "text": text,
        "voice_id": config.voice_id,
    }
    return _sha256_text(_canonical_json(material))


def deterministic_seed(cache_key: str) -> int:
    return int(cache_key[:8], 16) & 0x7FFFFFFF


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        frames = handle.getnframes()
    if rate <= 0 or frames <= 0:
        raise ValueError(f"invalid WAV metadata: rate={rate}, frames={frames}")
    return frames / float(rate)


class DeterministicWavCache:
    """Content-addressed WAV cache with atomic publication."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        _best_effort_mode(self.root, 0o700)

    def path_for(self, cache_key: str) -> Path:
        if len(cache_key) != 64 or any(ch not in "0123456789abcdef" for ch in cache_key):
            raise ValueError("cache_key must be a lowercase SHA-256 digest")
        return self.root / cache_key[:2] / f"{cache_key}.wav"

    def materialize(
        self,
        *,
        cache_key: str,
        text: str,
        synthesizer: Synthesizer,
    ) -> tuple[Path, SynthesisResult, bool]:
        final_path = self.path_for(cache_key)
        if final_path.is_file():
            try:
                duration = wav_duration(final_path)
                _best_effort_mode(final_path.parent, 0o700)
                _best_effort_mode(final_path, 0o600)
                return final_path, SynthesisResult(0.0, duration), True
            except (OSError, EOFError, wave.Error, ValueError):
                # The content-addressed name is only authoritative for a valid WAV.
                final_path.unlink(missing_ok=True)

        final_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _best_effort_mode(final_path.parent, 0o700)
        temporary = final_path.parent / (
            f".{cache_key}.{uuid.uuid4().hex}.partial.wav"
        )
        try:
            result = synthesizer.synthesize(
                text,
                temporary,
                deterministic_seed(cache_key),
            )
            measured_duration = wav_duration(temporary)
            if abs(measured_duration - result.audio_duration_sec) > 0.05:
                raise ValueError(
                    "synthesizer duration does not match generated WAV: "
                    f"{result.audio_duration_sec} != {measured_duration}"
                )
            os.replace(temporary, final_path)
            _best_effort_mode(final_path, 0o600)
            return final_path, result, False
        finally:
            temporary.unlink(missing_ok=True)


class PlaybackStore:
    """SQLite-backed idempotency ledger and state transition store."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _best_effort_mode(self.path.parent, 0o700)
        if not self.path.exists():
            try:
                descriptor = os.open(
                    self.path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError:
                pass
            else:
                os.close(descriptor)
        _best_effort_mode(self.path, 0o600)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            str(self.path),
            check_same_thread=False,
            isolation_level=None,
        )
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO metadata(key, value)
                    VALUES ('event_sequence', 0);

                CREATE TABLE IF NOT EXISTS playback_jobs (
                    reply_id TEXT PRIMARY KEY,
                    payload_sha256 TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    utterance_id TEXT NOT NULL,
                    gateway_instance_id TEXT NOT NULL,
                    procedure_run_id TEXT NOT NULL,
                    function_call_name TEXT NOT NULL,
                    function_arguments_json TEXT NOT NULL,
                    function_request_id TEXT NOT NULL,
                    timing TEXT NOT NULL,
                    text TEXT NOT NULL,
                    text_sha256 TEXT NOT NULL,
                    voice_id TEXT NOT NULL,
                    output_device TEXT NOT NULL,
                    cache_key TEXT NOT NULL,
                    state TEXT NOT NULL,
                    synth_latency_ms REAL NOT NULL DEFAULT 0,
                    audio_duration_sec REAL NOT NULL DEFAULT 0,
                    playback_latency_ms REAL NOT NULL DEFAULT 0,
                    terminal INTEGER NOT NULL DEFAULT 0,
                    success INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL DEFAULT '',
                    sequence INTEGER NOT NULL,
                    created_unix_sec REAL NOT NULL,
                    updated_unix_sec REAL NOT NULL,
                    CHECK (state IN ('queued', 'waiting', 'playing', 'played', 'failed')),
                    CHECK (timing IN ('immediate', 'on_function_accepted', 'on_function_completed'))
                );
                CREATE INDEX IF NOT EXISTS playback_jobs_state_idx
                    ON playback_jobs(state, sequence);
                """
            )
            # Forward-compatible with a database created by the first local
            # development build before function metadata became durable.
            columns = {
                str(row["name"])
                for row in self._connection.execute(
                    "PRAGMA table_info(playback_jobs)"
                ).fetchall()
            }
            for name in (
                "function_call_name",
                "function_arguments_json",
                "function_request_id",
                "gateway_instance_id",
            ):
                if name not in columns:
                    self._connection.execute(
                        f"ALTER TABLE playback_jobs ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"
                    )
            # Create the scope index only after a legacy table has received
            # the gateway column.
            self._connection.execute(
                """
                CREATE INDEX IF NOT EXISTS playback_jobs_scope_state_idx
                    ON playback_jobs(
                        gateway_instance_id, procedure_run_id, state, sequence
                    )
                """
            )
            user_version = int(
                self._connection.execute("PRAGMA user_version").fetchone()[0]
            )
            if user_version < 2:
                self._connection.execute("PRAGMA user_version=2")
            for suffix in ("", "-wal", "-shm"):
                candidate = Path(f"{self.path}{suffix}")
                if candidate.exists():
                    _best_effort_mode(candidate, 0o600)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @staticmethod
    def _payload_sha256(request: ReplyRequest) -> str:
        return _sha256_text(
            _canonical_json(
                {
                    "function_request_id": request.function_request_id,
                    "function_call_name": request.function_call_name,
                    "function_arguments_json": request.function_arguments_json,
                    "gateway_instance_id": request.gateway_instance_id,
                    "procedure_run_id": request.procedure_run_id,
                    "reply_id": request.reply_id,
                    "text": request.text,
                    "timing": request.timing,
                    "turn_id": request.turn_id,
                    "utterance_id": request.utterance_id,
                }
            )
        )

    def _next_sequence_locked(self) -> int:
        self._connection.execute(
            "UPDATE metadata SET value = value + 1 WHERE key = 'event_sequence'"
        )
        row = self._connection.execute(
            "SELECT value FROM metadata WHERE key = 'event_sequence'"
        ).fetchone()
        return int(row["value"])

    @staticmethod
    def _event(row: sqlite3.Row) -> PlaybackEvent:
        raw_state = str(row["state"])
        public_state = raw_state
        if raw_state == "waiting":
            public_state = {
                "on_function_accepted": "waiting_function_accepted",
                "on_function_completed": "waiting_function_completed",
            }.get(str(row["timing"]), "waiting")
        return PlaybackEvent(
            sequence=int(row["sequence"]),
            reply_id=str(row["reply_id"]),
            turn_id=str(row["turn_id"]),
            utterance_id=str(row["utterance_id"]),
            gateway_instance_id=str(row["gateway_instance_id"]),
            procedure_run_id=str(row["procedure_run_id"]),
            function_call_name=str(row["function_call_name"]),
            function_arguments_json=str(row["function_arguments_json"]),
            function_request_id=str(row["function_request_id"]),
            state=public_state,
            timing=str(row["timing"]),
            text=str(row["text"]),
            text_sha256=str(row["text_sha256"]),
            voice_id=str(row["voice_id"]),
            output_device=str(row["output_device"]),
            cache_key=str(row["cache_key"]),
            synth_latency_ms=float(row["synth_latency_ms"]),
            audio_duration_sec=float(row["audio_duration_sec"]),
            playback_latency_ms=float(row["playback_latency_ms"]),
            terminal=bool(row["terminal"]),
            success=bool(row["success"]),
            error_code=str(row["error_code"]),
            message=str(row["message"]),
        )

    def insert(
        self,
        request: ReplyRequest,
        config: RuntimeConfig,
        cache_key: str,
    ) -> EnqueueResult:
        if not request.reply_id.strip():
            raise ValueError("reply_id must be non-empty")
        if not request.turn_id.strip():
            raise ValueError("turn_id must be non-empty")
        if not request.gateway_instance_id.strip():
            raise ValueError("gateway_instance_id must be non-empty")
        if not request.procedure_run_id.strip():
            raise ValueError("procedure_run_id must be non-empty")
        if not request.text.strip():
            raise ValueError("text must be non-empty")
        if request.timing not in VALID_TIMINGS:
            raise ValueError(f"unsupported timing: {request.timing!r}")

        payload_sha256 = self._payload_sha256(request)
        state = "queued" if request.timing == "immediate" else "waiting"
        now = time.time()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                existing = self._connection.execute(
                    "SELECT * FROM playback_jobs WHERE reply_id = ?",
                    (request.reply_id,),
                ).fetchone()
                if existing is not None:
                    same_logical_turn = (
                        str(existing["gateway_instance_id"])
                        == request.gateway_instance_id
                        and str(existing["procedure_run_id"])
                        == request.procedure_run_id
                        and str(existing["utterance_id"]) == request.utterance_id
                    )
                    if not same_logical_turn:
                        raise ValueError(
                            "reply_id collision: gateway_instance_id/"
                            "procedure_run_id/utterance_id differ"
                        )
                    # First writer wins. Model text, turn_id and function
                    # wording may change after a VLM restart, but the stable
                    # gateway+run+utterance reply_id must never produce a
                    # second sound.
                    self._connection.execute("COMMIT")
                    return EnqueueResult(
                        accepted=False,
                        duplicate=True,
                        event=self._event(existing),
                    )

                sequence = self._next_sequence_locked()
                message = "queued_for_playback" if state == "queued" else "waiting_for_release"
                self._connection.execute(
                    """
                    INSERT INTO playback_jobs(
                        reply_id, payload_sha256, turn_id, utterance_id,
                        gateway_instance_id, procedure_run_id, function_call_name,
                        function_arguments_json, function_request_id, timing, text,
                        text_sha256, voice_id, output_device, cache_key, state,
                        terminal, success, error_code, message, sequence,
                        created_unix_sec, updated_unix_sec
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, '', ?, ?, ?, ?)
                    """,
                    (
                        request.reply_id,
                        payload_sha256,
                        request.turn_id,
                        request.utterance_id,
                        request.gateway_instance_id,
                        request.procedure_run_id,
                        request.function_call_name,
                        request.function_arguments_json,
                        request.function_request_id,
                        request.timing,
                        request.text,
                        _sha256_text(request.text),
                        config.voice_id,
                        config.output_device,
                        cache_key,
                        state,
                        message,
                        sequence,
                        now,
                        now,
                    ),
                )
                row = self._connection.execute(
                    "SELECT * FROM playback_jobs WHERE reply_id = ?",
                    (request.reply_id,),
                ).fetchone()
                self._connection.execute("COMMIT")
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
        return EnqueueResult(accepted=True, duplicate=False, event=self._event(row))

    def get(self, reply_id: str) -> PlaybackEvent | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM playback_jobs WHERE reply_id = ?", (reply_id,)
            ).fetchone()
        return None if row is None else self._event(row)

    def list_waiting(self) -> list[PlaybackEvent]:
        """Return durable admission-gated jobs in creation order."""

        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM playback_jobs
                 WHERE state = 'waiting' AND terminal = 0
                 ORDER BY sequence ASC
                """
            ).fetchall()
        return [self._event(row) for row in rows]

    def expire_waiting(
        self,
        *,
        timeout_sec: float,
        now_unix_sec: float | None = None,
    ) -> list[PlaybackEvent]:
        """Fail admission-gated jobs that exceeded their bounded lifetime."""

        if timeout_sec <= 0:
            raise ValueError("timeout_sec must be positive")
        now = time.time() if now_unix_sec is None else float(now_unix_sec)
        cutoff = now - float(timeout_sec)
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT reply_id FROM playback_jobs
                 WHERE state = 'waiting' AND terminal = 0
                   AND created_unix_sec <= ?
                 ORDER BY sequence ASC
                """,
                (cutoff,),
            ).fetchall()
        expired: list[PlaybackEvent] = []
        for row in rows:
            event = self.transition(
                str(row["reply_id"]),
                expected_state="waiting",
                state="failed",
                error_code="waiting_timeout",
                message="authoritative_release_evidence_timeout",
            )
            if event is not None:
                expired.append(event)
        return expired

    @staticmethod
    def _scope_error(
        event: PlaybackEvent,
        *,
        gateway_instance_id: str,
        procedure_run_id: str,
    ) -> tuple[str, str]:
        if (
            procedure_run_id
            and event.procedure_run_id == procedure_run_id
            and event.gateway_instance_id != gateway_instance_id
        ):
            return (
                "stale_gateway_scope",
                "pending_reply_does_not_belong_to_active_gateway_epoch",
            )
        return (
            "stale_procedure_run",
            "pending_reply_does_not_belong_to_active_procedure_run",
        )

    def fail_pending_outside_scope(
        self,
        gateway_instance_id: str,
        procedure_run_id: str,
    ) -> list[PlaybackEvent]:
        """Terminalize queued/waiting work outside one exact authority scope."""

        active_gateway = gateway_instance_id.strip()
        active_run = procedure_run_id.strip()
        if bool(active_gateway) != bool(active_run):
            raise ValueError(
                "gateway_instance_id and procedure_run_id must both be set or empty"
            )
        with self._lock:
            if active_gateway:
                rows = self._connection.execute(
                    """
                    SELECT * FROM playback_jobs
                     WHERE state IN ('queued', 'waiting') AND terminal = 0
                       AND (
                           gateway_instance_id != ? OR procedure_run_id != ?
                       )
                     ORDER BY sequence ASC
                    """,
                    (active_gateway, active_run),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    """
                    SELECT * FROM playback_jobs
                     WHERE state IN ('queued', 'waiting') AND terminal = 0
                     ORDER BY sequence ASC
                    """
                ).fetchall()
        failed: list[PlaybackEvent] = []
        for row in rows:
            current = self._event(row)
            error_code, message = self._scope_error(
                current,
                gateway_instance_id=active_gateway,
                procedure_run_id=active_run,
            )
            event = self.transition(
                current.reply_id,
                expected_state=str(row["state"]),
                state="failed",
                error_code=error_code,
                message=message,
            )
            if event is not None:
                failed.append(event)
        return failed

    def fail_pending_outside_run(self, procedure_run_id: str) -> list[PlaybackEvent]:
        """Backward-compatible run-only cleanup helper.

        Production dispatch uses :meth:`fail_pending_outside_scope`; this
        helper intentionally retains its original run-only semantics for local
        callers that have not yet adopted gateway epochs.
        """

        active_run = procedure_run_id.strip()
        with self._lock:
            if active_run:
                rows = self._connection.execute(
                    """
                    SELECT reply_id, state FROM playback_jobs
                     WHERE state IN ('queued', 'waiting') AND terminal = 0
                       AND procedure_run_id != ?
                     ORDER BY sequence ASC
                    """,
                    (active_run,),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    """
                    SELECT reply_id, state FROM playback_jobs
                     WHERE state IN ('queued', 'waiting') AND terminal = 0
                     ORDER BY sequence ASC
                    """
                ).fetchall()
        failed: list[PlaybackEvent] = []
        for row in rows:
            event = self.transition(
                str(row["reply_id"]),
                expected_state=str(row["state"]),
                state="failed",
                error_code="stale_procedure_run",
                message="pending_reply_does_not_belong_to_active_procedure_run",
            )
            if event is not None:
                failed.append(event)
        return failed

    def fail_interrupted_playing(self) -> list[PlaybackEvent]:
        """Terminalize playback whose audible side effect is crash-ambiguous."""

        with self._lock:
            rows = self._connection.execute(
                """
                SELECT reply_id FROM playback_jobs
                 WHERE state = 'playing' AND terminal = 0
                 ORDER BY sequence ASC
                """
            ).fetchall()
        failed: list[PlaybackEvent] = []
        for row in rows:
            event = self.transition(
                str(row["reply_id"]),
                expected_state="playing",
                state="failed",
                error_code="interrupted_unknown",
                message="playback_interrupted_before_terminal_receipt",
            )
            if event is not None:
                failed.append(event)
        return failed

    def fail_waiting_outside_run(self, procedure_run_id: str) -> list[PlaybackEvent]:
        """Backward-compatible waiting-only scope cleanup helper."""

        active_run = procedure_run_id.strip()
        with self._lock:
            if active_run:
                rows = self._connection.execute(
                    """
                    SELECT reply_id FROM playback_jobs
                     WHERE state = 'waiting' AND terminal = 0
                       AND procedure_run_id != ?
                     ORDER BY sequence ASC
                    """,
                    (active_run,),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    """
                    SELECT reply_id FROM playback_jobs
                     WHERE state = 'waiting' AND terminal = 0
                     ORDER BY sequence ASC
                    """
                ).fetchall()
        failed: list[PlaybackEvent] = []
        for row in rows:
            event = self.transition(
                str(row["reply_id"]),
                expected_state="waiting",
                state="failed",
                error_code="stale_procedure_run",
                message="waiting_reply_does_not_belong_to_active_procedure_run",
            )
            if event is not None:
                failed.append(event)
        return failed

    def duplicate_event(self, reply_id: str) -> PlaybackEvent:
        """Allocate an observable ACK without mutating the job ledger state."""

        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT * FROM playback_jobs WHERE reply_id = ?", (reply_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(reply_id)
                sequence = self._next_sequence_locked()
                self._connection.execute("COMMIT")
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
        original = self._event(row)
        return PlaybackEvent(
            **{
                **original.__dict__,
                "sequence": sequence,
                "state": "duplicate_suppressed",
                "message": f"duplicate_suppressed_existing_state:{original.state}",
            }
        )

    def rejection_event(
        self,
        request: ReplyRequest,
        config: RuntimeConfig,
        *,
        error_code: str,
        message: str,
    ) -> PlaybackEvent:
        """Allocate a terminal typed NACK for a request that was not stored.

        A first-writer collision or malformed delivery cannot be inserted into
        the playback ledger, but the durable producer still needs a correlated
        terminal result so it does not retry forever.  Only the event sequence
        is persisted here; no rejected transcript is written to SQLite.
        """

        normalized_error = str(error_code or "").strip()
        normalized_message = str(message or "").strip()
        if not normalized_error:
            raise ValueError("rejection error_code must be non-empty")
        if not normalized_message:
            raise ValueError("rejection message must be non-empty")
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                sequence = self._next_sequence_locked()
                self._connection.execute("COMMIT")
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
        return PlaybackEvent(
            sequence=sequence,
            reply_id=str(request.reply_id),
            turn_id=str(request.turn_id),
            utterance_id=str(request.utterance_id),
            gateway_instance_id=str(request.gateway_instance_id),
            procedure_run_id=str(request.procedure_run_id),
            function_call_name=str(request.function_call_name),
            function_arguments_json=str(request.function_arguments_json),
            function_request_id=str(request.function_request_id),
            state="failed",
            timing=str(request.timing),
            text=str(request.text),
            text_sha256=_sha256_text(str(request.text)),
            voice_id=config.voice_id,
            output_device=config.output_device,
            cache_key=cache_key_for(str(request.text), config),
            synth_latency_ms=0.0,
            audio_duration_sec=0.0,
            playback_latency_ms=0.0,
            terminal=True,
            success=False,
            error_code=normalized_error,
            message=normalized_message,
        )

    def transition(
        self,
        reply_id: str,
        *,
        expected_state: str,
        state: str,
        synth_latency_ms: float | None = None,
        audio_duration_sec: float | None = None,
        playback_latency_ms: float | None = None,
        error_code: str = "",
        message: str,
    ) -> PlaybackEvent | None:
        terminal = state in TERMINAL_STATES
        success = state == "played"
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT * FROM playback_jobs WHERE reply_id = ?",
                    (reply_id,),
                ).fetchone()
                if row is None:
                    self._connection.execute("COMMIT")
                    return None
                if row["state"] != expected_state:
                    self._connection.execute("COMMIT")
                    return None
                sequence = self._next_sequence_locked()
                values = {
                    "synth_latency_ms": (
                        float(row["synth_latency_ms"])
                        if synth_latency_ms is None
                        else float(synth_latency_ms)
                    ),
                    "audio_duration_sec": (
                        float(row["audio_duration_sec"])
                        if audio_duration_sec is None
                        else float(audio_duration_sec)
                    ),
                    "playback_latency_ms": (
                        float(row["playback_latency_ms"])
                        if playback_latency_ms is None
                        else float(playback_latency_ms)
                    ),
                }
                self._connection.execute(
                    """
                    UPDATE playback_jobs SET
                        state = ?, synth_latency_ms = ?, audio_duration_sec = ?,
                        playback_latency_ms = ?, terminal = ?, success = ?,
                        error_code = ?, message = ?, sequence = ?, updated_unix_sec = ?
                    WHERE reply_id = ?
                    """,
                    (
                        state,
                        values["synth_latency_ms"],
                        values["audio_duration_sec"],
                        values["playback_latency_ms"],
                        int(terminal),
                        int(success),
                        error_code,
                        message,
                        sequence,
                        time.time(),
                        reply_id,
                    ),
                )
                updated = self._connection.execute(
                    "SELECT * FROM playback_jobs WHERE reply_id = ?",
                    (reply_id,),
                ).fetchone()
                self._connection.execute("COMMIT")
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
        return self._event(updated)

    def _recovery_refresh_locked(
        self,
        reply_id: str,
        *,
        message: str,
    ) -> PlaybackEvent:
        sequence = self._next_sequence_locked()
        self._connection.execute(
            """
            UPDATE playback_jobs
               SET message = ?, sequence = ?, updated_unix_sec = ?
             WHERE reply_id = ?
            """,
            (message, sequence, time.time(), reply_id),
        )
        row = self._connection.execute(
            "SELECT * FROM playback_jobs WHERE reply_id = ?", (reply_id,)
        ).fetchone()
        return self._event(row)

    def recover(
        self,
        *,
        gateway_instance_id: str,
        procedure_run_id: str,
    ) -> tuple[list[PlaybackEvent], list[str]]:
        """Recover only safe work in the exact authoritative scope.

        A legacy row has an empty gateway epoch after migration and is thus
        terminalized rather than relabelled with the current gateway. Playing
        rows remain crash-ambiguous even when their scope matches.
        """

        active_gateway = gateway_instance_id.strip()
        active_run = procedure_run_id.strip()
        if not active_gateway or not active_run:
            raise ValueError("an exact nonempty recovery scope is required")

        emitted: list[PlaybackEvent] = []
        queued_ids: list[str] = []
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                rows = self._connection.execute(
                    """
                    SELECT * FROM playback_jobs
                     WHERE terminal = 0
                     ORDER BY sequence ASC
                    """
                ).fetchall()
                for row in rows:
                    reply_id = str(row["reply_id"])
                    state = str(row["state"])
                    event = self._event(row)
                    in_scope = bool(
                        event.gateway_instance_id == active_gateway
                        and event.procedure_run_id == active_run
                    )
                    if state == "playing":
                        sequence = self._next_sequence_locked()
                        self._connection.execute(
                            """
                            UPDATE playback_jobs SET
                                state = 'failed', terminal = 1, success = 0,
                                error_code = 'interrupted_unknown',
                                message = 'playback_interrupted_before_terminal_receipt',
                                sequence = ?, updated_unix_sec = ?
                            WHERE reply_id = ? AND state = 'playing'
                            """,
                            (sequence, time.time(), reply_id),
                        )
                        failed = self._connection.execute(
                            "SELECT * FROM playback_jobs WHERE reply_id = ?",
                            (reply_id,),
                        ).fetchone()
                        emitted.append(self._event(failed))
                    elif not in_scope:
                        error_code, message = self._scope_error(
                            event,
                            gateway_instance_id=active_gateway,
                            procedure_run_id=active_run,
                        )
                        sequence = self._next_sequence_locked()
                        self._connection.execute(
                            """
                            UPDATE playback_jobs SET
                                state = 'failed', terminal = 1, success = 0,
                                error_code = ?, message = ?,
                                sequence = ?, updated_unix_sec = ?
                            WHERE reply_id = ?
                              AND state IN ('queued', 'waiting')
                            """,
                            (
                                error_code,
                                message,
                                sequence,
                                time.time(),
                                reply_id,
                            ),
                        )
                        failed = self._connection.execute(
                            "SELECT * FROM playback_jobs WHERE reply_id = ?",
                            (reply_id,),
                        ).fetchone()
                        emitted.append(self._event(failed))
                    elif state == "queued":
                        emitted.append(
                            self._recovery_refresh_locked(
                                reply_id, message="recovered_queued"
                            )
                        )
                        queued_ids.append(reply_id)
                    elif state == "waiting":
                        emitted.append(
                            self._recovery_refresh_locked(
                                reply_id, message="recovered_waiting"
                            )
                        )
                self._connection.execute("COMMIT")
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
        return emitted, queued_ids


class PlaybackDispatcher:
    """Single-worker, durable, idempotent synthesis/playback dispatcher."""

    _STOP = object()

    def __init__(
        self,
        *,
        store: PlaybackStore,
        cache: DeterministicWavCache,
        synthesizer: Synthesizer,
        player: Player,
        config: RuntimeConfig,
        on_event: Callable[[PlaybackEvent], None] | None = None,
    ) -> None:
        self.store = store
        self.cache = cache
        self.synthesizer = synthesizer
        self.player = player
        self.config = config
        self.on_event = on_event or (lambda _event: None)
        self._queue: Queue[str | object] = Queue()
        self._lifecycle_lock = threading.Lock()
        self._scope_lock = threading.RLock()
        self._active_gateway_instance = ""
        self._active_procedure_run = ""
        self._playing_reply_id = ""
        self._playing_gateway_instance = ""
        self._playing_procedure_run = ""
        self._started = False
        self._stopping = threading.Event()
        self._worker: threading.Thread | None = None

    def _emit(self, event: PlaybackEvent) -> None:
        try:
            self.on_event(event)
        except Exception:
            # Observability must never change durable playback state.
            pass

    def start(self) -> None:
        with self._scope_lock:
            if not self._active_gateway_instance or not self._active_procedure_run:
                raise RuntimeError(
                    "exact active gateway/procedure scope must be set before worker start"
                )
            # Keep the authority snapshot and recovery atomic with respect to
            # scope changes. The lock order matches stop(): scope, lifecycle.
            with self._lifecycle_lock:
                if self._started:
                    return
                recovered, queued_ids = self.store.recover(
                    gateway_instance_id=self._active_gateway_instance,
                    procedure_run_id=self._active_procedure_run,
                )
                for event in recovered:
                    self._emit(event)
                self._stopping.clear()
                self._worker = threading.Thread(
                    target=self._run,
                    name="tts-playback-worker",
                    daemon=True,
                )
                self._worker.start()
                self._started = True
                for reply_id in queued_ids:
                    self._queue.put(reply_id)

    @property
    def active_procedure_run(self) -> str:
        with self._scope_lock:
            return self._active_procedure_run

    @property
    def active_gateway_instance(self) -> str:
        with self._scope_lock:
            return self._active_gateway_instance

    @property
    def active_scope(self) -> tuple[str, str]:
        with self._scope_lock:
            return (
                self._active_gateway_instance,
                self._active_procedure_run,
            )

    def set_active_procedure_scope(
        self,
        gateway_instance_id: str,
        procedure_run_id: str,
    ) -> list[PlaybackEvent]:
        """Atomically fence playback to one exact gateway/run authority scope."""

        active_gateway = gateway_instance_id.strip()
        active_run = procedure_run_id.strip()
        if bool(active_gateway) != bool(active_run):
            raise ValueError(
                "gateway_instance_id and procedure_run_id must both be set or empty"
            )
        emitted: list[PlaybackEvent] = []
        with self._scope_lock:
            self._active_gateway_instance = active_gateway
            self._active_procedure_run = active_run
            if self._playing_reply_id and (
                self._playing_gateway_instance != active_gateway
                or self._playing_procedure_run != active_run
            ):
                interrupted = self.store.transition(
                    self._playing_reply_id,
                    expected_state="playing",
                    state="failed",
                    error_code="procedure_scope_changed",
                    message="playback_interrupted_by_procedure_scope_change",
                )
                if interrupted is not None:
                    emitted.append(interrupted)
                interrupt = getattr(self.player, "interrupt", None)
                if callable(interrupt):
                    interrupt()
            # Stop the audible side effect before walking an arbitrarily long
            # durable pending queue.
            emitted.extend(
                self.store.fail_pending_outside_scope(active_gateway, active_run)
            )
            # Publish while the scope fence is still held. Otherwise the
            # worker could publish an earlier cached ``playing`` event after
            # this terminal scope-change event.
            for event in emitted:
                self._emit(event)
        return emitted

    def set_active_procedure_run(
        self,
        procedure_run_id: str,
    ) -> list[PlaybackEvent]:
        """Compatibility wrapper preserving an established gateway epoch."""

        active_run = procedure_run_id.strip()
        with self._scope_lock:
            active_gateway = (
                self._active_gateway_instance
                if active_run and self._active_gateway_instance
                else LEGACY_GATEWAY_INSTANCE_ID if active_run else ""
            )
        return self.set_active_procedure_scope(active_gateway, active_run)

    def prewarm(self, texts: list[str] | tuple[str, ...]) -> list[PrewarmResult]:
        """Materialize fixed WAVs without touching the ledger or player."""

        with self._lifecycle_lock:
            if self._started:
                raise RuntimeError("prewarm must run before the playback worker starts")
            results: list[PrewarmResult] = []
            seen: set[str] = set()
            for raw_text in texts:
                text = str(raw_text).strip()
                if not text or text in seen:
                    continue
                seen.add(text)
                key = cache_key_for(text, self.config)
                wav_path, synthesis, cache_hit = self.cache.materialize(
                    cache_key=key,
                    text=text,
                    synthesizer=self.synthesizer,
                )
                results.append(
                    PrewarmResult(
                        text=text,
                        cache_key=key,
                        wav_path=wav_path,
                        cache_hit=cache_hit,
                        synth_latency_ms=synthesis.synth_latency_ms,
                        audio_duration_sec=synthesis.audio_duration_sec,
                    )
                )
        return results

    def submit(self, request: ReplyRequest) -> EnqueueResult:
        with self._scope_lock:
            if (
                not self._active_gateway_instance
                or not self._active_procedure_run
                or request.gateway_instance_id.strip()
                != self._active_gateway_instance
                or request.procedure_run_id.strip()
                != self._active_procedure_run
            ):
                raise ValueError(
                    "reply gateway/procedure scope does not match active playback scope"
                )
            key = cache_key_for(request.text, self.config)
            result = self.store.insert(request, self.config, key)
            if result.duplicate:
                duplicate = self.store.duplicate_event(request.reply_id)
                self._emit(duplicate)
                return EnqueueResult(
                    accepted=False,
                    duplicate=True,
                    event=duplicate,
                )
            if result.accepted:
                self._emit(result.event)
                if result.event.state == "queued":
                    self._queue.put(request.reply_id)
            return result

    def reject(
        self,
        request: ReplyRequest,
        *,
        error_code: str,
        message: str,
    ) -> PlaybackEvent:
        """Publish a correlated terminal NACK without accepting playback."""

        event = self.store.rejection_event(
            request,
            self.config,
            error_code=error_code,
            message=message,
        )
        self._emit(event)
        return event

    def release_waiting(self, reply_id: str) -> PlaybackEvent | None:
        """Release one admission-gated reply without invoking robot control."""

        with self._scope_lock:
            current = self.store.get(reply_id)
            if (
                current is None
                or not self._active_gateway_instance
                or not self._active_procedure_run
                or current.gateway_instance_id != self._active_gateway_instance
                or current.procedure_run_id != self._active_procedure_run
            ):
                return None
            event = self.store.transition(
                reply_id,
                expected_state="waiting",
                state="queued",
                message="released_for_playback",
            )
            if event is not None:
                self._emit(event)
                self._queue.put(reply_id)
        return event

    def _fail(
        self,
        reply_id: str,
        *,
        expected_state: str,
        error_code: str,
        error: BaseException,
    ) -> None:
        detail = str(error).strip() or error.__class__.__name__
        event = self.store.transition(
            reply_id,
            expected_state=expected_state,
            state="failed",
            error_code=error_code,
            message=detail[:500],
        )
        if event is not None:
            self._emit(event)

    def _process(self, reply_id: str) -> None:
        scope_failure: PlaybackEvent | None = None
        with self._scope_lock:
            current = self.store.get(reply_id)
            if current is None or current.state != "queued":
                return
            if (
                not self._active_gateway_instance
                or not self._active_procedure_run
                or current.gateway_instance_id != self._active_gateway_instance
                or current.procedure_run_id != self._active_procedure_run
            ):
                scope_failure = self.store.transition(
                    reply_id,
                    expected_state="queued",
                    state="failed",
                    error_code="procedure_scope_changed",
                    message="queued_reply_outside_active_procedure_scope",
                )
            if scope_failure is not None:
                self._emit(scope_failure)
                return
        try:
            wav_path, synthesis, cache_hit = self.cache.materialize(
                cache_key=current.cache_key,
                text=current.text,
                synthesizer=self.synthesizer,
            )
        except Exception as exc:
            self._fail(
                reply_id,
                expected_state="queued",
                error_code="synthesis_failed",
                error=exc,
            )
            return

        playing: PlaybackEvent | None = None
        scope_failure = None
        arm_error: BaseException | None = None
        with self._scope_lock:
            latest = self.store.get(reply_id)
            if latest is None or latest.state != "queued":
                return
            if (
                not self._active_gateway_instance
                or not self._active_procedure_run
                or latest.gateway_instance_id != self._active_gateway_instance
                or latest.procedure_run_id != self._active_procedure_run
            ):
                scope_failure = self.store.transition(
                    reply_id,
                    expected_state="queued",
                    state="failed",
                    error_code="procedure_scope_changed",
                    message="procedure_scope_changed_during_synthesis",
                )
            else:
                playing = self.store.transition(
                    reply_id,
                    expected_state="queued",
                    state="playing",
                    synth_latency_ms=synthesis.synth_latency_ms,
                    audio_duration_sec=synthesis.audio_duration_sec,
                    message=(
                        "playing_cached_wav"
                        if cache_hit
                        else "playing_generated_wav"
                    ),
                )
                if playing is not None:
                    self._playing_reply_id = reply_id
                    self._playing_gateway_instance = latest.gateway_instance_id
                    self._playing_procedure_run = latest.procedure_run_id
                    arm = getattr(self.player, "arm", None)
                    if callable(arm):
                        try:
                            arm()
                        except BaseException as exc:
                            arm_error = exc
                    # Keep durable state publications ordered with the scope
                    # transition that may immediately cancel this attempt.
                    self._emit(playing)
                    if arm_error is not None:
                        self._playing_reply_id = ""
                        self._playing_gateway_instance = ""
                        self._playing_procedure_run = ""
            if scope_failure is not None:
                self._emit(scope_failure)
                return
        if playing is None:
            return
        if arm_error is not None:
            self._fail(
                reply_id,
                expected_state="playing",
                error_code="playback_arm_failed",
                error=arm_error,
            )
            return
        try:
            playback = self.player.play(wav_path)
        except Exception as exc:
            with self._scope_lock:
                self._fail(
                    reply_id,
                    expected_state="playing",
                    error_code="playback_failed",
                    error=exc,
                )
                if self._playing_reply_id == reply_id:
                    self._playing_reply_id = ""
                    self._playing_gateway_instance = ""
                    self._playing_procedure_run = ""
            return
        terminal: PlaybackEvent | None
        with self._scope_lock:
            if (
                self._active_gateway_instance
                and self._active_procedure_run
                and self._playing_gateway_instance
                == self._active_gateway_instance
                and self._playing_procedure_run == self._active_procedure_run
            ):
                terminal = self.store.transition(
                    reply_id,
                    expected_state="playing",
                    state="played",
                    playback_latency_ms=playback.playback_latency_ms,
                    message="playback_completed",
                )
            else:
                terminal = self.store.transition(
                    reply_id,
                    expected_state="playing",
                    state="failed",
                    error_code="procedure_scope_changed",
                    message="procedure_scope_changed_before_playback_receipt",
                )
            if self._playing_reply_id == reply_id:
                self._playing_reply_id = ""
                self._playing_gateway_instance = ""
                self._playing_procedure_run = ""
            if terminal is not None:
                self._emit(terminal)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is self._STOP:
                    return
                if self._stopping.is_set():
                    return
                self._process(str(item))
            finally:
                self._queue.task_done()

    def wait_idle(self) -> None:
        self._queue.join()

    def stop(self, timeout_sec: float = 10.0) -> None:
        # Keep the same lock order as start (scope, then lifecycle). Clearing
        # the authority scope before join both rejects new submissions and
        # interrupts an armed/current player while the store is still open for
        # its terminal status event.
        with self._scope_lock:
            with self._lifecycle_lock:
                started = self._started
                worker = self._worker
                if started:
                    self._stopping.set()
                    self._queue.put(self._STOP)
            self.set_active_procedure_scope("", "")
        if not started:
            self.synthesizer.close()
            return
        if worker is not None:
            worker.join(timeout=max(0.0, timeout_sec))
        try:
            self.synthesizer.close()
        finally:
            with self._lifecycle_lock:
                self._started = False
                self._worker = None

    def close(self, timeout_sec: float = 10.0) -> None:
        self.stop(timeout_sec=timeout_sec)
        self.store.close()
