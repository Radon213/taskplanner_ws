"""Durable, fail-closed admission gate for VLM dialogue functions.

The VLM is a proposal source, never an Action or Service client.  This module
joins four independently delivered facts for one surgeon utterance:

* an active ``GatewayInfo`` epoch,
* the admitted final ``SpeechUtterance``,
* the resolver's procedure-grounded ``VoiceCommandIntent`` proposal, and
* the VLM's validated ``HumanoidReply`` carrying an optional function call.

Only an exact join is promoted to the existing ``/surgery/voice/intent``
boundary.  The reply is separately promoted to ``/tts/admitted_reply``.  Both
outputs are stored before publication and retried until their own downstream
receipt is observed.  This module never calls robot Actions or Services.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Iterator, Mapping


GATEWAY_WATCHDOG_SEC = 3.0
MAX_PENDING_LIMIT = 1024
MAX_IN_MEMORY_TURNS = 1024
MAX_ACTIVE_LEDGER_TURNS = 4096
MAX_RETAINED_STALE_TURNS = 1024
# Current unconstrained NInfer responses may require multiple bounded schema
# repair attempts. Eight seconds covers the measured retry tail while retaining
# an explicit, short-lived exact-utterance execution window.
DEFAULT_INTENT_TTL_SEC = 8.0
MAX_PERSISTED_JSON_BYTES = 1_000_000
HANDOVER_FUNCTION = "request_tool_handover"
ADJUST_RETRACTION_FUNCTION = "adjust_retraction"
SUPPORTED_FUNCTIONS = frozenset(
    {HANDOVER_FUNCTION, ADJUST_RETRACTION_FUNCTION}
)
FUNCTION_REPLY_TIMINGS = frozenset(
    {"on_function_accepted", "on_function_completed"}
)
TTS_RECEIPT_STATES = frozenset(
    {
        "queued",
        "waiting_function_accepted",
        "waiting_function_completed",
        "playing",
        "played",
        "duplicate_suppressed",
        "failed",
    }
)


def _json_load_object(value: str) -> dict[str, Any] | None:
    """Parse strict JSON without accepting JavaScript NaN/Infinity tokens."""

    def reject_constant(_value: str) -> None:
        raise ValueError("non-finite JSON number")

    try:
        parsed = json.loads(value, parse_constant=reject_constant)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def _finite_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


@dataclass(frozen=True, slots=True)
class GatewayScope:
    gateway_instance_id: str
    procedure_run_id: str
    procedure_type: str
    catalog_version: str
    source_stamp_ns: int

    def __post_init__(self) -> None:
        if not self.gateway_instance_id.strip():
            raise ValueError("gateway_instance_id must be non-empty")
        if not self.procedure_run_id.strip():
            raise ValueError("procedure_run_id must be non-empty")
        if not self.procedure_type.strip():
            raise ValueError("procedure_type must be non-empty")
        if self.source_stamp_ns <= 0:
            raise ValueError("source_stamp_ns must be positive")

    @property
    def identity(self) -> tuple[str, str]:
        return (self.gateway_instance_id, self.procedure_run_id)


@dataclass(frozen=True, slots=True)
class ScopedUtterance:
    gateway_instance_id: str
    procedure_run_id: str
    utterance_id: str

    def __post_init__(self) -> None:
        if not all(
            str(value).strip()
            for value in (
                self.gateway_instance_id,
                self.procedure_run_id,
                self.utterance_id,
            )
        ):
            raise ValueError("scoped utterance fields must be non-empty")


@dataclass(frozen=True, slots=True)
class SpeechFact:
    scope: GatewayScope
    utterance_id: str
    source_stamp_ns: int
    source: str
    is_final: bool
    speaker_role: str
    text: str = field(repr=False)
    has_confidence: bool = False
    confidence: float = 0.0

    @property
    def key(self) -> ScopedUtterance:
        return ScopedUtterance(
            self.scope.gateway_instance_id,
            self.scope.procedure_run_id,
            self.utterance_id,
        )


@dataclass(frozen=True, slots=True)
class ProposalFact:
    scope: GatewayScope
    utterance_id: str
    source_stamp_ns: int
    payload: Mapping[str, Any] = field(repr=False)

    @property
    def key(self) -> ScopedUtterance:
        return ScopedUtterance(
            self.scope.gateway_instance_id,
            self.scope.procedure_run_id,
            self.utterance_id,
        )


@dataclass(frozen=True, slots=True)
class ReplyFact:
    gateway_instance_id: str
    procedure_run_id: str
    utterance_id: str
    source_stamp_ns: int
    # Local receipt time is used only for the first fact's TTL check. It is not
    # part of the producer-owned identity: a durable outbox may redeliver the
    # exact same reply later with a different local receive timestamp.
    received_at_ns: int = field(compare=False)
    payload: Mapping[str, Any] = field(repr=False)

    @property
    def key(self) -> ScopedUtterance:
        return ScopedUtterance(
            self.gateway_instance_id,
            self.procedure_run_id,
            self.utterance_id,
        )


@dataclass(frozen=True, slots=True)
class DurableAdmission:
    gateway_instance_id: str
    procedure_run_id: str
    utterance_id: str
    reply_id: str
    function_request_id: str
    function_call_name: str
    intent_expires_at_ns: int
    reply_json: str = field(repr=False)
    intent_json: str = field(repr=False)

    @property
    def has_intent(self) -> bool:
        return bool(self.intent_json)

    @property
    def delivery_id(self) -> str:
        return self.function_request_id or self.reply_id

    @property
    def scope(self) -> tuple[str, str]:
        return (self.gateway_instance_id, self.procedure_run_id)

    @property
    def fingerprint(self) -> str:
        payload = {
            "gateway_instance_id": self.gateway_instance_id,
            "procedure_run_id": self.procedure_run_id,
            "utterance_id": self.utterance_id,
            "reply_id": self.reply_id,
            "function_request_id": self.function_request_id,
            "function_call_name": self.function_call_name,
            "intent_expires_at_ns": self.intent_expires_at_ns,
            "reply_sha256": hashlib.sha256(
                self.reply_json.encode("utf-8")
            ).hexdigest(),
            "intent_sha256": hashlib.sha256(
                self.intent_json.encode("utf-8")
            ).hexdigest(),
        }
        return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()

    def reply_payload(self) -> dict[str, Any]:
        parsed = _json_load_object(self.reply_json)
        if parsed is None:
            raise ValueError("stored reply JSON is invalid")
        return parsed

    def intent_payload(self) -> dict[str, Any]:
        if not self.intent_json:
            return {}
        parsed = _json_load_object(self.intent_json)
        if parsed is None:
            raise ValueError("stored intent JSON is invalid")
        return parsed


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    state: str
    reason_code: str
    admission: DurableAdmission | None = field(default=None, repr=False)

    @property
    def admitted(self) -> bool:
        return self.state == "admitted" and self.admission is not None


class LedgerCollisionError(RuntimeError):
    """A logical turn attempted to overwrite its durable first writer."""

    def __init__(self, delivery_id: str, utterance_id: str) -> None:
        self.delivery_id = delivery_id
        self.utterance_id = utterance_id
        super().__init__(
            "function gate ledger collision for delivery_id/utterance_id "
            f"({delivery_id!r}, {utterance_id!r})"
        )


class FunctionGateLedger:
    """SQLite output ledger with independent intent and reply receipts."""

    _SELECT = (
        "gateway_instance_id, procedure_run_id, utterance_id, reply_id, "
        "function_request_id, function_call_name, intent_expires_at_ns, "
        "reply_json, intent_json, admission_sha256"
    )

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)
        self._lock = threading.RLock()
        self._closed = False
        self._prepare_path()
        self._connection = sqlite3.connect(
            str(self._path),
            timeout=10.0,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        try:
            self._configure()
            self._create_schema()
            self._secure_files()
        except BaseException:
            self._connection.close()
            raise

    @property
    def path(self) -> Path:
        return self._path

    def _prepare_path(self) -> None:
        if self._path.is_symlink():
            raise ValueError("ledger path must not be a symbolic link")
        parent = self._path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if parent.is_symlink() or parent.resolve() != parent.absolute():
            raise ValueError("ledger directory must not traverse symbolic links")
        parent_stat = parent.stat()
        if parent_stat.st_uid != os.getuid():
            raise PermissionError("ledger directory must be owned by this user")
        if parent_stat.st_mode & 0o077:
            raise PermissionError("ledger directory permissions must be 0700")
        if self._path.exists() and not self._path.is_file():
            raise ValueError("ledger path must be a regular file")
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(self._path, flags, 0o600)
        try:
            file_stat = os.fstat(descriptor)
            if file_stat.st_uid != os.getuid():
                raise PermissionError("ledger file must be owned by this user")
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)

    def _configure(self) -> None:
        self._connection.execute("PRAGMA busy_timeout=10000")
        mode = self._connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            raise RuntimeError("function gate ledger requires WAL mode")
        self._connection.execute("PRAGMA synchronous=FULL")
        if int(self._connection.execute("PRAGMA synchronous").fetchone()[0]) != 2:
            raise RuntimeError("function gate ledger requires synchronous=FULL")
        self._connection.execute("PRAGMA trusted_schema=OFF")

    def _create_schema(self) -> None:
        with self._write_transaction():
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS admitted_turns (
                    delivery_id TEXT PRIMARY KEY,
                    gateway_instance_id TEXT NOT NULL,
                    procedure_run_id TEXT NOT NULL,
                    utterance_id TEXT NOT NULL,
                    reply_id TEXT NOT NULL UNIQUE,
                    function_request_id TEXT NOT NULL,
                    function_call_name TEXT NOT NULL,
                    intent_expires_at_ns INTEGER NOT NULL DEFAULT 0,
                    reply_json TEXT NOT NULL,
                    intent_json TEXT NOT NULL,
                    admission_sha256 TEXT NOT NULL DEFAULT '',
                    has_intent INTEGER NOT NULL,
                    reply_acked INTEGER NOT NULL DEFAULT 0,
                    intent_acked INTEGER NOT NULL DEFAULT 0,
                    reply_ack_success INTEGER NOT NULL DEFAULT -1,
                    intent_ack_success INTEGER NOT NULL DEFAULT -1,
                    reply_ack_state TEXT NOT NULL DEFAULT '',
                    intent_ack_state TEXT NOT NULL DEFAULT '',
                    stale INTEGER NOT NULL DEFAULT 0,
                    stale_reason TEXT NOT NULL DEFAULT '',
                    admitted_at_ns INTEGER NOT NULL,
                    updated_at_ns INTEGER NOT NULL,
                    UNIQUE (gateway_instance_id, procedure_run_id, utterance_id),
                    CHECK (has_intent IN (0, 1)),
                    CHECK (reply_acked IN (0, 1)),
                    CHECK (intent_acked IN (0, 1)),
                    CHECK (reply_ack_success IN (-1, 0, 1)),
                    CHECK (intent_ack_success IN (-1, 0, 1)),
                    CHECK (stale IN (0, 1))
                )
                """
            )
            columns = {
                str(row[1])
                for row in self._connection.execute(
                    "PRAGMA table_info(admitted_turns)"
                ).fetchall()
            }
            had_intent_expiry = "intent_expires_at_ns" in columns
            migrations = {
                "intent_expires_at_ns": (
                    "ALTER TABLE admitted_turns ADD COLUMN "
                    "intent_expires_at_ns INTEGER NOT NULL DEFAULT 0"
                ),
                "admission_sha256": (
                    "ALTER TABLE admitted_turns ADD COLUMN "
                    "admission_sha256 TEXT NOT NULL DEFAULT ''"
                ),
                "reply_ack_success": (
                    "ALTER TABLE admitted_turns ADD COLUMN "
                    "reply_ack_success INTEGER NOT NULL DEFAULT -1"
                ),
                "intent_ack_success": (
                    "ALTER TABLE admitted_turns ADD COLUMN "
                    "intent_ack_success INTEGER NOT NULL DEFAULT -1"
                ),
            }
            for column, statement in migrations.items():
                if column not in columns:
                    self._connection.execute(statement)
            if not had_intent_expiry:
                # A pre-expiry ledger has no trustworthy source-time deadline.
                # Fence its unacknowledged intents rather than replaying an
                # arbitrarily old surgical proposal after an upgrade.
                self._connection.execute(
                    "UPDATE admitted_turns SET intent_expires_at_ns = 1 "
                    "WHERE has_intent = 1 AND intent_acked = 0"
                )
            rows = self._connection.execute(
                f"SELECT rowid, {self._SELECT} FROM admitted_turns "
                "WHERE admission_sha256 = ''"
            ).fetchall()
            for row in rows:
                admission = self._row_to_admission(row)
                self._connection.execute(
                    "UPDATE admitted_turns SET admission_sha256 = ? "
                    "WHERE rowid = ?",
                    (admission.fingerprint, int(row["rowid"])),
                )
            self._connection.execute("PRAGMA user_version=2")

    @contextmanager
    def _write_transaction(self) -> Iterator[None]:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            # Keep all explicit filesystem checks inside the rollback-capable
            # region. SQLite synchronous=FULL owns the commit-record fsync;
            # a manual post-COMMIT failure would otherwise make the caller
            # report rejection even though a durable row already existed.
            self._secure_files()
            self._fsync_storage()
            self._connection.execute("COMMIT")
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise

    def _secure_files(self) -> None:
        parent_stat = self._path.parent.stat()
        if parent_stat.st_uid != os.getuid() or parent_stat.st_mode & 0o077:
            raise PermissionError("ledger directory lost its 0700 protection")
        for candidate in (
            self._path,
            Path(f"{self._path}-wal"),
            Path(f"{self._path}-shm"),
        ):
            if candidate.exists():
                candidate.chmod(0o600)

    def _fsync_storage(self) -> None:
        for candidate in (self._path, Path(f"{self._path}-wal")):
            if not candidate.exists():
                continue
            descriptor = os.open(
                candidate,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        directory = os.open(
            self._path.parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("function gate ledger is closed")

    @staticmethod
    def _row_to_admission(row: sqlite3.Row) -> DurableAdmission:
        return DurableAdmission(
            gateway_instance_id=str(row["gateway_instance_id"]),
            procedure_run_id=str(row["procedure_run_id"]),
            utterance_id=str(row["utterance_id"]),
            reply_id=str(row["reply_id"]),
            function_request_id=str(row["function_request_id"]),
            function_call_name=str(row["function_call_name"]),
            intent_expires_at_ns=int(row["intent_expires_at_ns"]),
            reply_json=str(row["reply_json"]),
            intent_json=str(row["intent_json"]),
        )

    def admit(self, admission: DurableAdmission) -> bool:
        if not isinstance(admission, DurableAdmission):
            raise TypeError("admission must be DurableAdmission")
        if not admission.delivery_id.strip() or not admission.reply_id.strip():
            raise ValueError("delivery and reply identifiers must be non-empty")
        if not all(
            value.strip()
            for value in (
                admission.gateway_instance_id,
                admission.procedure_run_id,
                admission.utterance_id,
            )
        ):
            raise ValueError("admission scope identifiers must be non-empty")
        if any(
            len(value) > 256
            for value in (
                admission.delivery_id,
                admission.gateway_instance_id,
                admission.procedure_run_id,
                admission.utterance_id,
                admission.reply_id,
                admission.function_request_id,
                admission.function_call_name,
            )
        ):
            raise ValueError("admission identifier exceeds 256 characters")
        if (
            len(admission.reply_json.encode("utf-8")) > MAX_PERSISTED_JSON_BYTES
            or len(admission.intent_json.encode("utf-8"))
            > MAX_PERSISTED_JSON_BYTES
        ):
            raise ValueError("admission payload exceeds persistence limit")
        with self._lock:
            self._ensure_open()
            with self._write_transaction():
                existing = self._connection.execute(
                    f"SELECT {self._SELECT} FROM admitted_turns "
                    "WHERE delivery_id = ? OR reply_id = ? OR "
                    "(gateway_instance_id = ? AND "
                    "procedure_run_id = ? AND utterance_id = ?)",
                    (
                        admission.delivery_id,
                        admission.reply_id,
                        admission.gateway_instance_id,
                        admission.procedure_run_id,
                        admission.utterance_id,
                    ),
                ).fetchone()
                if existing is not None:
                    if str(existing["admission_sha256"]) == admission.fingerprint:
                        return False
                    raise LedgerCollisionError(
                        admission.delivery_id, admission.utterance_id
                    )
                active_count = int(
                    self._connection.execute(
                        "SELECT COUNT(*) FROM admitted_turns WHERE "
                        "gateway_instance_id = ? AND procedure_run_id = ? "
                        "AND stale = 0",
                        (
                            admission.gateway_instance_id,
                            admission.procedure_run_id,
                        ),
                    ).fetchone()[0]
                )
                if active_count >= MAX_ACTIVE_LEDGER_TURNS:
                    raise RuntimeError("active function gate ledger is full")
                now_ns = time.time_ns()
                try:
                    self._connection.execute(
                        """
                        INSERT INTO admitted_turns (
                            delivery_id, gateway_instance_id, procedure_run_id,
                            utterance_id, reply_id, function_request_id,
                            function_call_name, intent_expires_at_ns,
                            reply_json, intent_json, admission_sha256,
                            has_intent, reply_acked, intent_acked,
                            reply_ack_success, intent_ack_success,
                            admitted_at_ns, updated_at_ns
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?,
                                  -1, ?, ?, ?)
                        """,
                        (
                            admission.delivery_id,
                            admission.gateway_instance_id,
                            admission.procedure_run_id,
                            admission.utterance_id,
                            admission.reply_id,
                            admission.function_request_id,
                            admission.function_call_name,
                            admission.intent_expires_at_ns,
                            admission.reply_json,
                            admission.intent_json,
                            admission.fingerprint,
                            int(admission.has_intent),
                            int(not admission.has_intent),
                            1 if not admission.has_intent else -1,
                            now_ns,
                            now_ns,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise LedgerCollisionError(
                        admission.delivery_id, admission.utterance_id
                    ) from exc
            return True

    def _pending(
        self,
        scope: GatewayScope,
        *,
        output: str,
        limit: int,
    ) -> list[DurableAdmission]:
        if output not in {"reply", "intent"}:
            raise ValueError("output must be reply or intent")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_PENDING_LIMIT:
            raise ValueError(f"limit must be in [1, {MAX_PENDING_LIMIT}]")
        predicate = (
            "reply_acked = 0"
            if output == "reply"
            else "has_intent = 1 AND intent_acked = 0"
        )
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                f"SELECT {self._SELECT} FROM admitted_turns WHERE "
                "gateway_instance_id = ? AND procedure_run_id = ? AND "
                f"stale = 0 AND {predicate} ORDER BY rowid ASC LIMIT ?",
                (
                    scope.gateway_instance_id,
                    scope.procedure_run_id,
                    limit,
                ),
            ).fetchall()
            return [self._row_to_admission(row) for row in rows]

    def pending_replies(
        self, scope: GatewayScope, *, limit: int = 64
    ) -> list[DurableAdmission]:
        return self._pending(scope, output="reply", limit=limit)

    def pending_intents(
        self, scope: GatewayScope, *, limit: int = 64
    ) -> list[DurableAdmission]:
        return self._pending(scope, output="intent", limit=limit)

    def get_by_function_request_id(
        self, function_request_id: str
    ) -> DurableAdmission | None:
        if not function_request_id.strip():
            return None
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                f"SELECT {self._SELECT} FROM admitted_turns "
                "WHERE function_request_id = ?",
                (function_request_id,),
            ).fetchone()
            return self._row_to_admission(row) if row is not None else None

    def get_by_reply_id(self, reply_id: str) -> DurableAdmission | None:
        if not reply_id.strip():
            return None
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                f"SELECT {self._SELECT} FROM admitted_turns WHERE reply_id = ?",
                (reply_id,),
            ).fetchone()
            return self._row_to_admission(row) if row is not None else None

    def _ack(
        self,
        *,
        column: str,
        id_column: str,
        identifier: str,
        state: str,
        success: bool | None,
        scope: GatewayScope,
    ) -> bool:
        if column not in {"reply", "intent"}:
            raise ValueError("invalid acknowledgement column")
        if id_column not in {"reply_id", "function_request_id"}:
            raise ValueError("invalid acknowledgement identifier")
        if not identifier.strip() or not state.strip():
            return False
        with self._lock:
            self._ensure_open()
            with self._write_transaction():
                row = self._connection.execute(
                    f"SELECT {column}_acked AS acked, "
                    f"{column}_ack_success AS ack_success "
                    "FROM admitted_turns "
                    f"WHERE {id_column} = ? AND gateway_instance_id = ? AND "
                    "procedure_run_id = ? AND stale = 0",
                    (
                        identifier,
                        scope.gateway_instance_id,
                        scope.procedure_run_id,
                    ),
                ).fetchone()
                if row is None:
                    return False

                acked = bool(row["acked"])
                current_outcome = int(row["ack_success"])
                next_outcome = -1 if success is None else int(bool(success))
                if acked:
                    # A provisional delivery receipt stops retries, but a
                    # later terminal result must still be durable. Failure is
                    # sticky and may replace either an unknown or (defensively)
                    # positive result; success can only fill an unknown result.
                    if current_outcome == 0:
                        return False
                    if next_outcome < 0 or current_outcome == next_outcome:
                        return False

                cursor = self._connection.execute(
                    f"UPDATE admitted_turns SET {column}_acked = 1, "
                    f"{column}_ack_success = ?, {column}_ack_state = ?, "
                    f"{column}_json = '{{}}', updated_at_ns = ? "
                    f"WHERE {id_column} = ? AND gateway_instance_id = ? AND "
                    "procedure_run_id = ? AND stale = 0",
                    (
                        next_outcome,
                        state,
                        time.time_ns(),
                        identifier,
                        scope.gateway_instance_id,
                        scope.procedure_run_id,
                    ),
                )
            return cursor.rowcount == 1

    def ack_reply(
        self,
        reply_id: str,
        state: str,
        scope: GatewayScope,
        *,
        success: bool | None = True,
    ) -> bool:
        return self._ack(
            column="reply",
            id_column="reply_id",
            identifier=reply_id,
            state=state,
            success=success,
            scope=scope,
        )

    def ack_intent(
        self,
        function_request_id: str,
        state: str,
        scope: GatewayScope,
        *,
        success: bool | None = True,
    ) -> bool:
        return self._ack(
            column="intent",
            id_column="function_request_id",
            identifier=function_request_id,
            state=state,
            success=success,
            scope=scope,
        )

    def acknowledgement_state(
        self, delivery_id: str
    ) -> tuple[bool, bool, bool] | None:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT reply_acked, intent_acked, stale FROM admitted_turns "
                "WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
            if row is None:
                return None
            return (
                bool(row["reply_acked"]),
                bool(row["intent_acked"]),
                bool(row["stale"]),
            )

    def acknowledgement_outcomes(
        self, delivery_id: str
    ) -> tuple[bool | None, bool | None] | None:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                "SELECT reply_ack_success, intent_ack_success "
                "FROM admitted_turns WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
            if row is None:
                return None

            def value(raw: object) -> bool | None:
                number = int(raw)
                return None if number < 0 else bool(number)

            return value(row["reply_ack_success"]), value(
                row["intent_ack_success"]
            )

    def expire_intents(
        self, scope: GatewayScope, *, now_ns: int
    ) -> list[DurableAdmission]:
        if now_ns <= 0:
            return []
        with self._lock:
            self._ensure_open()
            with self._write_transaction():
                rows = self._connection.execute(
                    f"SELECT {self._SELECT} FROM admitted_turns WHERE "
                    "gateway_instance_id = ? AND procedure_run_id = ? AND "
                    "stale = 0 AND has_intent = 1 AND intent_acked = 0 AND "
                    "intent_expires_at_ns > 0 AND intent_expires_at_ns <= ?",
                    (
                        scope.gateway_instance_id,
                        scope.procedure_run_id,
                        now_ns,
                    ),
                ).fetchall()
                if rows:
                    self._connection.execute(
                        "UPDATE admitted_turns SET intent_acked = 1, "
                        "intent_ack_success = 0, intent_ack_state = 'expired', "
                        "intent_json = '{}', updated_at_ns = ? WHERE "
                        "gateway_instance_id = ? AND procedure_run_id = ? AND "
                        "stale = 0 AND has_intent = 1 AND intent_acked = 0 AND "
                        "intent_expires_at_ns > 0 AND intent_expires_at_ns <= ?",
                        (
                            time.time_ns(),
                            scope.gateway_instance_id,
                            scope.procedure_run_id,
                            now_ns,
                        ),
                    )
            return [self._row_to_admission(row) for row in rows]

    def mark_stale_outside_scope(
        self, scope: GatewayScope | None, *, reason: str = "outside_active_gateway_scope"
    ) -> int:
        if not reason.strip():
            raise ValueError("stale reason must be non-empty")
        with self._lock:
            self._ensure_open()
            with self._write_transaction():
                if scope is None:
                    cursor = self._connection.execute(
                        "UPDATE admitted_turns SET stale = 1, stale_reason = ?, "
                        "reply_json = '{}', intent_json = '{}', "
                        "updated_at_ns = ? WHERE stale = 0",
                        (reason, time.time_ns()),
                    )
                else:
                    cursor = self._connection.execute(
                        "UPDATE admitted_turns SET stale = 1, stale_reason = ?, "
                        "reply_json = '{}', intent_json = '{}', "
                        "updated_at_ns = ? WHERE stale = 0 AND "
                        "(gateway_instance_id <> ? OR procedure_run_id <> ?)",
                        (
                            reason,
                            time.time_ns(),
                            scope.gateway_instance_id,
                            scope.procedure_run_id,
                        ),
                    )
                stale_count = int(
                    self._connection.execute(
                        "SELECT COUNT(*) FROM admitted_turns WHERE stale = 1"
                    ).fetchone()[0]
                )
                excess = max(0, stale_count - MAX_RETAINED_STALE_TURNS)
                if excess:
                    self._connection.execute(
                        "DELETE FROM admitted_turns WHERE rowid IN ("
                        "SELECT rowid FROM admitted_turns WHERE stale = 1 "
                        "ORDER BY updated_at_ns ASC LIMIT ?)",
                        (excess,),
                    )
            return int(cursor.rowcount)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._connection.close()
            self._closed = True
            self._secure_files()


class VLMFunctionAdmissionGate:
    """Order-independent pure joiner backed by ``FunctionGateLedger``."""

    def __init__(
        self,
        ledger: FunctionGateLedger,
        *,
        intent_ttl_sec: float = DEFAULT_INTENT_TTL_SEC,
    ) -> None:
        self._ledger = ledger
        if not math.isfinite(float(intent_ttl_sec)) or intent_ttl_sec <= 0.0:
            raise ValueError("intent_ttl_sec must be positive and finite")
        self._intent_ttl_ns = int(float(intent_ttl_sec) * 1_000_000_000)
        self._scope: GatewayScope | None = None
        self._poisoned_scope_identity: tuple[str, str] | None = None
        self._last_scope_identity: tuple[str, str] | None = None
        self._retired_scope_identities: set[tuple[str, str]] = set()
        self._speech: dict[ScopedUtterance, SpeechFact] = {}
        self._proposals: dict[ScopedUtterance, ProposalFact] = {}
        self._replies: dict[ScopedUtterance, ReplyFact] = {}
        self._decisions: dict[ScopedUtterance, AdmissionDecision] = {}

    @property
    def scope(self) -> GatewayScope | None:
        return self._scope

    def activate_scope(
        self,
        scope: GatewayScope | None,
        *,
        retire_current: bool = False,
    ) -> int:
        if scope is None and retire_current:
            if self._last_scope_identity is not None:
                self._retired_scope_identities.add(
                    self._last_scope_identity
                )
            self._last_scope_identity = None
        if (
            scope is not None
            and scope.identity in self._retired_scope_identities
        ):
            # Gateway epochs are opaque rather than ordered. Once a newer
            # identity has replaced one epoch/run (or it went explicitly
            # inactive), a delayed retained heartbeat cannot reopen it.
            return 0
        if scope is not None:
            if self._poisoned_scope_identity == scope.identity:
                return 0
            if (
                self._poisoned_scope_identity is not None
                and self._poisoned_scope_identity != scope.identity
            ):
                self._poisoned_scope_identity = None
        if self._scope is not None and scope is not None:
            if self._scope.identity == scope.identity:
                if (
                    self._scope.procedure_type == scope.procedure_type
                    and self._scope.catalog_version == scope.catalog_version
                ):
                    # A heartbeat refreshes the node's steady-clock lease; it
                    # must not advance the source fence or erase a partial
                    # three-fact join for the same immutable epoch/run.
                    return 0
                # Procedure/catalog mutation inside one run is a contract
                # violation. Poison this epoch/run until either public identity
                # changes; a second mutated heartbeat must not reopen it.
                self._poisoned_scope_identity = self._scope.identity
                self._retired_scope_identities.add(self._scope.identity)
                self._last_scope_identity = None
                scope = None
        if scope is not None:
            if (
                self._last_scope_identity is not None
                and self._last_scope_identity != scope.identity
            ):
                self._retired_scope_identities.add(
                    self._last_scope_identity
                )
            self._last_scope_identity = scope.identity
        # An explicit inactive/expired lease must fence rows recovered from a
        # prior process too, even when this in-memory gate starts at None.
        self._scope = scope
        self._speech.clear()
        self._proposals.clear()
        self._replies.clear()
        self._decisions.clear()
        return self._ledger.mark_stale_outside_scope(scope)

    @staticmethod
    def _trim_mapping(mapping: dict[ScopedUtterance, Any]) -> None:
        while len(mapping) > MAX_IN_MEMORY_TURNS:
            mapping.pop(next(iter(mapping)))

    def prune_before_source_stamp(self, cutoff_ns: int) -> int:
        if cutoff_ns <= 0:
            return 0
        stale_keys = {
            key
            for key, fact in self._speech.items()
            if fact.source_stamp_ns < cutoff_ns
        }
        stale_keys.update(
            key
            for key, fact in self._proposals.items()
            if fact.source_stamp_ns < cutoff_ns
        )
        stale_keys.update(
            key
            for key, fact in self._replies.items()
            if fact.source_stamp_ns < cutoff_ns
        )
        for key in stale_keys:
            self._speech.pop(key, None)
            self._proposals.pop(key, None)
            self._replies.pop(key, None)
            self._decisions.pop(key, None)
        return len(stale_keys)

    def _scope_matches(self, key: ScopedUtterance) -> bool:
        return bool(
            self._scope is not None
            and key.gateway_instance_id == self._scope.gateway_instance_id
            and key.procedure_run_id == self._scope.procedure_run_id
        )

    def _store_first(
        self,
        mapping: dict[ScopedUtterance, Any],
        key: ScopedUtterance,
        value: Any,
        collision_reason: str,
    ) -> AdmissionDecision | None:
        existing = mapping.get(key)
        if existing is None:
            mapping[key] = value
            self._trim_mapping(mapping)
            return None
        if existing == value:
            return None
        decision = AdmissionDecision("rejected", collision_reason)
        self._decisions[key] = decision
        self._trim_mapping(self._decisions)
        return decision

    def observe_speech(self, fact: SpeechFact) -> AdmissionDecision | None:
        if not self._scope_matches(fact.key):
            return AdmissionDecision("rejected", "speech_outside_active_scope")
        collision = self._store_first(
            self._speech,
            fact.key,
            fact,
            "speech_fact_collision",
        )
        return collision or self._try_admit(fact.key)

    def observe_proposal(self, fact: ProposalFact) -> AdmissionDecision | None:
        if not self._scope_matches(fact.key):
            return AdmissionDecision("rejected", "proposal_outside_active_scope")
        collision = self._store_first(
            self._proposals,
            fact.key,
            fact,
            "proposal_fact_collision",
        )
        return collision or self._try_admit(fact.key)

    def observe_reply(self, fact: ReplyFact) -> AdmissionDecision | None:
        if not self._scope_matches(fact.key):
            return AdmissionDecision("rejected", "reply_outside_active_scope")
        collision = self._store_first(
            self._replies,
            fact.key,
            fact,
            "reply_fact_collision",
        )
        return collision or self._try_admit(fact.key)

    def _try_admit(self, key: ScopedUtterance) -> AdmissionDecision | None:
        existing_decision = self._decisions.get(key)
        if existing_decision is not None:
            return None
        speech = self._speech.get(key)
        proposal = self._proposals.get(key)
        reply = self._replies.get(key)
        if speech is None or proposal is None or reply is None:
            return None
        decision = self._validate_join(speech, proposal, reply)
        if decision.admitted:
            assert decision.admission is not None
            try:
                self._ledger.admit(decision.admission)
            except LedgerCollisionError:
                decision = AdmissionDecision(
                    "rejected", "durable_first_writer_collision"
                )
            except (OSError, RuntimeError, ValueError, sqlite3.DatabaseError):
                decision = AdmissionDecision(
                    "rejected", "durable_persistence_unavailable"
                )
        self._decisions[key] = decision
        self._trim_mapping(self._decisions)
        return decision

    def _validate_join(
        self,
        speech: SpeechFact,
        proposal: ProposalFact,
        reply: ReplyFact,
    ) -> AdmissionDecision:
        scope = self._scope
        if scope is None:
            return AdmissionDecision("rejected", "gateway_scope_unavailable")
        if not (speech.key == proposal.key == reply.key):
            return AdmissionDecision("rejected", "join_key_mismatch")
        if speech.source_stamp_ns <= scope.source_stamp_ns:
            return AdmissionDecision("rejected", "speech_precedes_gateway_scope")
        if proposal.source_stamp_ns != speech.source_stamp_ns:
            return AdmissionDecision("rejected", "proposal_source_stamp_mismatch")
        if reply.source_stamp_ns <= scope.source_stamp_ns:
            return AdmissionDecision("rejected", "reply_precedes_gateway_scope")
        if not speech.is_final or speech.speaker_role.strip().casefold() != "surgeon":
            return AdmissionDecision("rejected", "speech_not_final_surgeon")
        if not speech.source.strip() or not speech.text.strip():
            return AdmissionDecision("rejected", "speech_source_or_text_missing")

        proposal_payload = dict(proposal.payload)
        reply_payload = dict(reply.payload)
        if str(proposal_payload.get("utterance_id", "")).strip() != speech.utterance_id:
            return AdmissionDecision("rejected", "proposal_utterance_mismatch")
        if str(proposal_payload.get("source", "")).strip() != speech.source.strip():
            return AdmissionDecision("rejected", "proposal_source_mismatch")
        if proposal_payload.get("source_is_final") is not True:
            return AdmissionDecision("rejected", "proposal_source_not_final")
        if str(proposal_payload.get("source_speaker_role", "")).strip().casefold() != "surgeon":
            return AdmissionDecision("rejected", "proposal_speaker_mismatch")
        if str(proposal_payload.get("raw_text", "")) != speech.text:
            return AdmissionDecision("rejected", "proposal_text_mismatch")
        if bool(proposal_payload.get("source_has_confidence")) != bool(
            speech.has_confidence
        ):
            return AdmissionDecision("rejected", "proposal_confidence_flag_mismatch")
        if speech.has_confidence:
            speech_confidence = _finite_float(speech.confidence)
            proposal_confidence = _finite_float(
                proposal_payload.get("source_confidence")
            )
            if (
                speech_confidence is None
                or proposal_confidence is None
                or not math.isclose(
                    speech_confidence,
                    proposal_confidence,
                    rel_tol=0.0,
                    abs_tol=1e-7,
                )
            ):
                return AdmissionDecision(
                    "rejected", "proposal_confidence_mismatch"
                )
        if str(proposal_payload.get("procedure_id", "")).strip() != scope.procedure_type:
            return AdmissionDecision("rejected", "proposal_procedure_mismatch")
        if not str(proposal_payload.get("catalog_id", "")).strip():
            return AdmissionDecision("rejected", "proposal_catalog_missing")
        proposal_gateway_id = str(
            proposal_payload.get("gateway_instance_id", "")
        ).strip()
        if proposal_gateway_id:
            return AdmissionDecision(
                "rejected", "resolver_proposal_claims_gateway_scope"
            )
        proposal_run_id = str(
            proposal_payload.get("procedure_run_id", "")
        ).strip()
        if proposal_run_id:
            return AdmissionDecision(
                "rejected", "resolver_proposal_claims_procedure_run"
            )
        if str(proposal_payload.get("function_request_id", "")).strip():
            return AdmissionDecision(
                "rejected", "resolver_proposal_claims_function_identity"
            )
        if _finite_float(proposal_payload.get("distance_m")) is None:
            return AdmissionDecision("rejected", "proposal_distance_not_finite")

        if reply_payload.get("valid") is not True or reply_payload.get("speak") is not True:
            return AdmissionDecision("rejected", "reply_not_valid_and_spoken")
        if str(reply_payload.get("gateway_instance_id", "")).strip() != scope.gateway_instance_id:
            return AdmissionDecision("rejected", "reply_gateway_mismatch")
        if str(reply_payload.get("procedure_run_id", "")).strip() != scope.procedure_run_id:
            return AdmissionDecision("rejected", "reply_run_mismatch")
        if str(reply_payload.get("utterance_id", "")).strip() != speech.utterance_id:
            return AdmissionDecision("rejected", "reply_utterance_mismatch")
        reply_id = str(reply_payload.get("reply_id", "")).strip()
        if not reply_id or not str(reply_payload.get("turn_id", "")).strip():
            return AdmissionDecision("rejected", "reply_identity_missing")
        if not str(reply_payload.get("text", "")).strip():
            return AdmissionDecision("rejected", "reply_text_missing")

        function_name = str(
            reply_payload.get("function_call_name", "")
        ).strip()
        function_request_id = str(
            reply_payload.get("function_request_id", "")
        ).strip()
        arguments_text = str(
            reply_payload.get("function_arguments_json", "")
        )
        timing = str(reply_payload.get("timing", "")).strip()
        disposition = str(proposal_payload.get("disposition", "")).strip()
        requires_confirmation = proposal_payload.get("requires_confirmation") is True

        if (
            function_name
            and reply.received_at_ns
            >= speech.source_stamp_ns + self._intent_ttl_ns
        ):
            return AdmissionDecision(
                "rejected", "function_reply_missed_intent_ttl"
            )

        if not function_name:
            if function_request_id or arguments_text.strip():
                return AdmissionDecision(
                    "rejected", "function_null_has_metadata"
                )
            if timing != "immediate":
                return AdmissionDecision(
                    "rejected", "function_null_requires_immediate_reply"
                )
            if disposition == "propose":
                return AdmissionDecision(
                    "rejected", "executable_proposal_missing_function_call"
                )
            try:
                reply_json = _canonical_json(reply_payload)
            except (TypeError, ValueError, OverflowError):
                return AdmissionDecision(
                    "rejected", "reply_payload_not_serializable"
                )
            admission = DurableAdmission(
                gateway_instance_id=scope.gateway_instance_id,
                procedure_run_id=scope.procedure_run_id,
                utterance_id=speech.utterance_id,
                reply_id=reply_id,
                function_request_id="",
                function_call_name="",
                intent_expires_at_ns=0,
                reply_json=reply_json,
                intent_json="",
            )
            return AdmissionDecision("admitted", "answer_admitted", admission)

        if function_name not in SUPPORTED_FUNCTIONS:
            return AdmissionDecision("rejected", "unsupported_function_name")
        if not function_request_id:
            return AdmissionDecision("rejected", "function_request_id_missing")
        if timing not in FUNCTION_REPLY_TIMINGS:
            return AdmissionDecision("rejected", "function_reply_timing_invalid")
        if disposition != "propose" or requires_confirmation:
            return AdmissionDecision("rejected", "proposal_not_executable")
        arguments = _json_load_object(arguments_text)
        if arguments is None:
            return AdmissionDecision("rejected", "function_arguments_invalid_json")

        if function_name == HANDOVER_FUNCTION:
            argument_reason = self._validate_handover(arguments, proposal_payload)
        else:
            argument_reason = self._validate_adjust_retraction(
                arguments, proposal_payload, timing
            )
        if argument_reason:
            return AdmissionDecision("rejected", argument_reason)

        intent_payload = dict(proposal_payload)
        intent_payload["gateway_instance_id"] = scope.gateway_instance_id
        intent_payload["procedure_run_id"] = scope.procedure_run_id
        intent_payload["function_request_id"] = function_request_id
        try:
            reply_json = _canonical_json(reply_payload)
            intent_json = _canonical_json(intent_payload)
        except (TypeError, ValueError, OverflowError):
            return AdmissionDecision(
                "rejected", "admission_payload_not_serializable"
            )
        admission = DurableAdmission(
            gateway_instance_id=scope.gateway_instance_id,
            procedure_run_id=scope.procedure_run_id,
            utterance_id=speech.utterance_id,
            reply_id=reply_id,
            function_request_id=function_request_id,
            function_call_name=function_name,
            intent_expires_at_ns=(
                speech.source_stamp_ns + self._intent_ttl_ns
            ),
            reply_json=reply_json,
            intent_json=intent_json,
        )
        return AdmissionDecision("admitted", "function_admitted", admission)

    @staticmethod
    def _validate_handover(
        arguments: Mapping[str, Any], proposal: Mapping[str, Any]
    ) -> str:
        if set(arguments) != {"tool_id"}:
            return "handover_arguments_not_exact"
        tool_id = str(arguments.get("tool_id", "")).strip()
        if not tool_id or tool_id != str(proposal.get("tool_id", "")).strip():
            return "handover_tool_mismatch"
        if str(proposal.get("intent", "")).strip() != "tool_handover":
            return "handover_proposal_intent_mismatch"
        if str(proposal.get("retractor_command", "")).strip():
            return "handover_proposal_has_retractor_command"
        if str(proposal.get("target_side", "")).strip() != "none":
            return "handover_proposal_has_target_side"
        distance = _finite_float(proposal.get("distance_m"))
        if distance is None or distance != 0.0:
            return "handover_proposal_has_distance"
        return ""

    @staticmethod
    def _validate_adjust_retraction(
        arguments: Mapping[str, Any],
        proposal: Mapping[str, Any],
        timing: str,
    ) -> str:
        if set(arguments) != {"command", "target_side", "distance_m"}:
            return "adjust_retraction_arguments_not_exact"
        if str(arguments.get("command", "")).strip() != "adjust_retraction":
            return "adjust_retraction_command_mismatch"
        if str(proposal.get("intent", "")).strip() != "retractor_command":
            return "adjust_retraction_proposal_intent_mismatch"
        if str(proposal.get("retractor_command", "")).strip() != "adjust_retraction":
            return "adjust_retraction_proposal_command_mismatch"
        if str(proposal.get("tool_id", "")).strip():
            return "adjust_retraction_proposal_has_tool"
        side = str(arguments.get("target_side", "")).strip()
        proposal_side = str(proposal.get("target_side", "")).strip()
        if side not in {"left", "right", "both"} or side != proposal_side:
            return "adjust_retraction_target_side_mismatch"
        distance = _finite_float(arguments.get("distance_m"))
        proposal_distance = _finite_float(proposal.get("distance_m"))
        if distance is None or proposal_distance is None or distance <= 0.0:
            return "adjust_retraction_distance_invalid"
        if not math.isclose(distance, proposal_distance, rel_tol=0.0, abs_tol=1e-12):
            return "adjust_retraction_distance_mismatch"
        # The reviewed Service has admission evidence only; it does not provide
        # a physical completion contract.
        if timing != "on_function_accepted":
            return "adjust_retraction_completion_timing_unsupported"
        return ""


# ROS imports are optional so the durable join and validation logic can be
# exercised by ordinary pytest without a sourced ROS installation.
try:  # pragma: no cover - import availability is environment-specific
    import rclpy
    from rclpy.clock import Clock, ClockType
    from rclpy.node import Node
    from rclpy.qos import (
        DurabilityPolicy,
        HistoryPolicy,
        QoSProfile,
        ReliabilityPolicy,
    )
    from std_msgs.msg import String
    from surgical_interop_msgs.msg import GatewayInfo
    from surgical_msgs.msg import (
        HumanoidReply,
        SpeechUtterance,
        TTSPlaybackStatus,
        TwinEvent,
        VLMFunctionCallStatus,
        VoiceCommandIntent,
    )

    _ROS_AVAILABLE = True
except ImportError:  # pragma: no cover - pure-test path
    Node = object  # type: ignore[assignment,misc]
    _ROS_AVAILABLE = False


def _stamp_ns(stamp: object) -> int:
    return int(getattr(stamp, "sec", 0)) * 1_000_000_000 + int(
        getattr(stamp, "nanosec", 0)
    )


def _first_positive_stamp_ns(message: object, *field_names: str) -> int:
    for field_name in field_names:
        value = _stamp_ns(getattr(message, field_name, None))
        if value > 0:
            return value
    return 0


class VLMFunctionAdmissionNode(Node):  # type: ignore[misc]
    """ROS adapter; all admission decisions remain in the pure gate above."""

    def __init__(self) -> None:
        if not _ROS_AVAILABLE:  # pragma: no cover - defensive import path
            raise RuntimeError("ROS dependencies are unavailable")
        super().__init__("vlm_function_admission_gate")
        default_dir = f"/tmp/taskplanner-vlm-function-gate-{os.getuid()}"
        default_ledger = os.environ.get(
            "TASKPLANNER_VLM_FUNCTION_GATE_LEDGER_PATH",
            f"{default_dir}/admissions.sqlite3",
        )
        self.declare_parameter("ledger_path", default_ledger)
        self.declare_parameter("proposal_topic", "/surgery/voice/proposal")
        self.declare_parameter(
            "admitted_speech_topic", "/surgery/audio/admitted_utterance"
        )
        self.declare_parameter("vlm_reply_topic", "/vlm/humanoid_reply")
        self.declare_parameter("intent_output_topic", "/surgery/voice/intent")
        self.declare_parameter("reply_output_topic", "/tts/admitted_reply")
        self.declare_parameter("tts_status_topic", "/tts/playback_status")
        self.declare_parameter("twin_event_topic", "/twin/events")
        self.declare_parameter(
            "retractor_status_topic",
            "/bed_robot_arm_group/voice_normalization_status",
        )
        self.declare_parameter("gateway_topic", "/surgery/gateway_info")
        self.declare_parameter("retry_period_sec", 0.5)
        self.declare_parameter("input_max_age_sec", 3.0)
        self.declare_parameter("join_timeout_sec", 30.0)
        self.declare_parameter("intent_ttl_sec", DEFAULT_INTENT_TTL_SEC)

        self._ledger = FunctionGateLedger(
            str(self.get_parameter("ledger_path").value)
        )
        self._gate = VLMFunctionAdmissionGate(
            self._ledger,
            intent_ttl_sec=float(self.get_parameter("intent_ttl_sec").value),
        )
        self._gateway_received_monotonic = 0.0
        self._gateway_revision_by_instance: dict[str, int] = {}
        self._gateway_stamp_by_instance: dict[str, int] = {}
        self._gateway_source_stamp_floor_ns = 0
        self._input_max_age_sec = min(
            GATEWAY_WATCHDOG_SEC,
            max(0.1, float(self.get_parameter("input_max_age_sec").value)),
        )
        self._join_timeout_sec = max(
            self._input_max_age_sec,
            float(self.get_parameter("join_timeout_sec").value),
        )
        if not math.isfinite(self._join_timeout_sec):
            raise ValueError("join_timeout_sec must be finite")

        reliable = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=32,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.VOLATILE,
        )
        retained = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=64,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        gateway_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        self._intent_pub = self.create_publisher(
            VoiceCommandIntent,
            str(self.get_parameter("intent_output_topic").value),
            reliable,
        )
        self._reply_pub = self.create_publisher(
            HumanoidReply,
            str(self.get_parameter("reply_output_topic").value),
            reliable,
        )
        self._status_pub = self.create_publisher(
            VLMFunctionCallStatus,
            "/vlm/function_call_status",
            retained,
        )
        self.create_subscription(
            GatewayInfo,
            str(self.get_parameter("gateway_topic").value),
            self._on_gateway,
            gateway_qos,
        )
        self.create_subscription(
            SpeechUtterance,
            str(self.get_parameter("admitted_speech_topic").value),
            self._on_speech,
            reliable,
        )
        self.create_subscription(
            VoiceCommandIntent,
            str(self.get_parameter("proposal_topic").value),
            self._on_proposal,
            reliable,
        )
        self.create_subscription(
            HumanoidReply,
            str(self.get_parameter("vlm_reply_topic").value),
            self._on_reply,
            retained,
        )
        self.create_subscription(
            TTSPlaybackStatus,
            str(self.get_parameter("tts_status_topic").value),
            self._on_tts_status,
            retained,
        )
        self.create_subscription(
            TwinEvent,
            str(self.get_parameter("twin_event_topic").value),
            self._on_twin_event,
            reliable,
        )
        self.create_subscription(
            String,
            str(self.get_parameter("retractor_status_topic").value),
            self._on_retractor_status,
            reliable,
        )
        retry_period = max(
            0.2, float(self.get_parameter("retry_period_sec").value)
        )
        self._steady_clock = Clock(clock_type=ClockType.STEADY_TIME)
        self.create_timer(
            min(retry_period, 1.0),
            self._retry_pending,
            clock=self._steady_clock,
        )

    def _now_ns(self) -> int:
        return int(self.get_clock().now().nanoseconds)

    def _scope_fresh(self) -> bool:
        return bool(
            self._gate.scope is not None
            and self._gateway_received_monotonic > 0.0
            and time.monotonic() - self._gateway_received_monotonic
            <= GATEWAY_WATCHDOG_SEC
        )

    def _expire_gateway_scope_if_needed(self) -> bool:
        if (
            self._gate.scope is not None
            and self._gateway_received_monotonic > 0.0
            and time.monotonic() - self._gateway_received_monotonic
            > GATEWAY_WATCHDOG_SEC
        ):
            # Timeout is a hard delivery boundary, even if a later heartbeat
            # repeats the same public IDs. This clears all partial joins and
            # stales every unacknowledged durable output before a fresh source
            # fence may be established.
            self._gate.activate_scope(None)
            return True
        return False

    def _input_stamp_is_current(self, stamp_ns: int) -> bool:
        scope = self._gate.scope
        if not self._scope_fresh() or scope is None:
            return False
        if stamp_ns <= scope.source_stamp_ns:
            return False
        age_sec = (self._now_ns() - stamp_ns) / 1_000_000_000.0
        return -1.0 <= age_sec <= self._input_max_age_sec

    def _reply_stamp_is_joinable(self, stamp_ns: int) -> bool:
        scope = self._gate.scope
        if not self._scope_fresh() or scope is None:
            return False
        if stamp_ns <= scope.source_stamp_ns:
            return False
        age_sec = (self._now_ns() - stamp_ns) / 1_000_000_000.0
        return -1.0 <= age_sec <= self._join_timeout_sec

    def _prune_input_facts(self) -> None:
        cutoff_ns = self._now_ns() - int(
            self._join_timeout_sec * 1_000_000_000
        )
        self._gate.prune_before_source_stamp(cutoff_ns)

    def _on_gateway(self, message: GatewayInfo) -> None:
        self._expire_gateway_scope_if_needed()
        gateway_id = str(message.gateway_instance_id or "").strip()
        revision = int(message.revision)
        stamp_ns = _stamp_ns(message.stamp)
        now_ns = self._now_ns()
        age_sec = (now_ns - stamp_ns) / 1_000_000_000.0 if stamp_ns > 0 else math.inf
        if (
            not gateway_id
            or revision <= 0
            or stamp_ns <= 0
            or age_sec > GATEWAY_WATCHDOG_SEC
            or age_sec < -1.0
        ):
            return
        previous_revision = self._gateway_revision_by_instance.get(
            gateway_id, -1
        )
        previous_stamp = self._gateway_stamp_by_instance.get(gateway_id, 0)
        if revision <= previous_revision or stamp_ns <= previous_stamp:
            return
        if (
            gateway_id not in self._gateway_revision_by_instance
            and stamp_ns <= self._gateway_source_stamp_floor_ns
        ):
            return
        self._gateway_revision_by_instance[gateway_id] = revision
        self._gateway_stamp_by_instance[gateway_id] = stamp_ns
        self._gateway_source_stamp_floor_ns = max(
            self._gateway_source_stamp_floor_ns, stamp_ns
        )
        if (
            not bool(message.procedure_active)
            or not gateway_id
            or not str(message.procedure_run_id or "").strip()
            or not str(message.procedure_type or "").strip()
        ):
            self._gate.activate_scope(None, retire_current=True)
            return
        scope = GatewayScope(
            gateway_instance_id=gateway_id,
            procedure_run_id=str(message.procedure_run_id).strip(),
            procedure_type=str(message.procedure_type).strip(),
            catalog_version=str(message.catalog_version or "").strip(),
            source_stamp_ns=stamp_ns,
        )
        self._gate.activate_scope(scope)
        active_scope = self._gate.scope
        if active_scope is None or active_scope.identity != scope.identity:
            return
        self._gateway_received_monotonic = time.monotonic()
        self._retry_pending()

    @staticmethod
    def _proposal_payload(message: VoiceCommandIntent) -> dict[str, Any]:
        return {
            "header": {
                "stamp_sec": int(message.header.stamp.sec),
                "stamp_nanosec": int(message.header.stamp.nanosec),
                "frame_id": str(message.header.frame_id),
            },
            "utterance_id": str(message.utterance_id),
            "gateway_instance_id": str(
                getattr(message, "gateway_instance_id", "")
            ),
            "procedure_run_id": str(
                getattr(message, "procedure_run_id", "")
            ),
            "function_request_id": str(
                getattr(message, "function_request_id", "")
            ),
            "source": str(message.source),
            "source_is_final": bool(message.source_is_final),
            "source_speaker_role": str(message.source_speaker_role),
            "source_has_confidence": bool(message.source_has_confidence),
            "source_confidence": float(message.source_confidence),
            "raw_text": str(message.raw_text),
            "normalized_text": str(message.normalized_text),
            "procedure_id": str(message.procedure_id),
            "catalog_id": str(message.catalog_id),
            "intent": str(message.intent),
            "tool_id": str(message.tool_id),
            "retractor_command": str(message.retractor_command),
            "target_side": str(message.target_side),
            "distance_m": float(message.distance_m),
            "urgency": str(message.urgency),
            "provenance": str(message.provenance),
            "requires_confirmation": bool(message.requires_confirmation),
            "disposition": str(message.disposition),
            "reason": str(message.reason),
            "evidence_spans": [str(item) for item in message.evidence_spans],
        }

    @staticmethod
    def _reply_payload(message: HumanoidReply) -> dict[str, Any]:
        return {
            "stamp_sec": int(message.stamp.sec),
            "stamp_nanosec": int(message.stamp.nanosec),
            "source": str(message.source),
            "source_epoch": int(message.source_epoch),
            "source_sequence": int(message.source_sequence),
            "correlation_id": str(message.correlation_id),
            "schema_version": str(message.schema_version),
            "gateway_instance_id": str(message.gateway_instance_id),
            "procedure_run_id": str(message.procedure_run_id),
            "utterance_id": str(message.utterance_id),
            "turn_id": str(message.turn_id),
            "reply_id": str(message.reply_id),
            "text": str(message.text),
            "kind": str(message.kind),
            "timing": str(message.timing),
            "speak": bool(message.speak),
            "function_call_name": str(message.function_call_name),
            "function_arguments_json": str(message.function_arguments_json),
            "function_request_id": str(message.function_request_id),
            "valid": bool(message.valid),
            "validation_error": str(message.validation_error),
        }

    def _on_speech(self, message: SpeechUtterance) -> None:
        self._prune_input_facts()
        scope = self._gate.scope
        # Keep this order identical to voice_command.node's
        # source_observation_stamp(): the immutable envelope stamp wins over
        # optional segment boundaries.
        stamp_ns = _first_positive_stamp_ns(
            message,
            "stamp",
            "end_stamp",
            "start_stamp",
        )
        if scope is None or not self._input_stamp_is_current(stamp_ns):
            return
        try:
            fact = SpeechFact(
                scope=scope,
                utterance_id=str(message.utterance_id).strip(),
                source_stamp_ns=stamp_ns,
                source=str(message.source).strip(),
                is_final=bool(message.is_final),
                speaker_role=str(message.speaker_role).strip(),
                text=str(message.text),
                has_confidence=bool(message.has_confidence),
                confidence=float(message.confidence),
            )
        except ValueError:
            return
        self._handle_decision(self._gate.observe_speech(fact), fact.key)

    def _on_proposal(self, message: VoiceCommandIntent) -> None:
        self._prune_input_facts()
        scope = self._gate.scope
        stamp_ns = _stamp_ns(message.header.stamp)
        if scope is None or not self._input_stamp_is_current(stamp_ns):
            return
        try:
            fact = ProposalFact(
                scope=scope,
                utterance_id=str(message.utterance_id).strip(),
                source_stamp_ns=stamp_ns,
                payload=self._proposal_payload(message),
            )
        except ValueError:
            return
        self._handle_decision(self._gate.observe_proposal(fact), fact.key)

    def _on_reply(self, message: HumanoidReply) -> None:
        self._prune_input_facts()
        stamp_ns = _stamp_ns(message.stamp)
        if not self._reply_stamp_is_joinable(stamp_ns):
            return
        received_at_ns = self._now_ns()
        try:
            fact = ReplyFact(
                gateway_instance_id=str(message.gateway_instance_id).strip(),
                procedure_run_id=str(message.procedure_run_id).strip(),
                utterance_id=str(message.utterance_id).strip(),
                source_stamp_ns=stamp_ns,
                received_at_ns=received_at_ns,
                payload=self._reply_payload(message),
            )
        except ValueError:
            return
        self._handle_decision(self._gate.observe_reply(fact), fact.key)

    def _handle_decision(
        self, decision: AdmissionDecision | None, key: ScopedUtterance
    ) -> None:
        if decision is None:
            return
        if decision.admitted:
            assert decision.admission is not None
            self._publish_status(
                decision.admission,
                state="admitted",
                success=True,
                terminal=False,
                reason_code=decision.reason_code,
            )
            self._retry_pending()
            return
        self._publish_rejection(key, decision.reason_code)

    def _publish_rejection(self, key: ScopedUtterance, reason_code: str) -> None:
        status = VLMFunctionCallStatus()
        status.stamp = self.get_clock().now().to_msg()
        status.gateway_instance_id = key.gateway_instance_id
        status.procedure_run_id = key.procedure_run_id
        status.utterance_id = key.utterance_id
        status.state = "rejected"
        status.terminal = True
        status.success = False
        status.reason_code = str(reason_code)
        self._status_pub.publish(status)

    def _publish_status(
        self,
        admission: DurableAdmission,
        *,
        state: str,
        success: bool,
        terminal: bool,
        reason_code: str,
    ) -> None:
        status = VLMFunctionCallStatus()
        status.stamp = self.get_clock().now().to_msg()
        status.gateway_instance_id = admission.gateway_instance_id
        status.procedure_run_id = admission.procedure_run_id
        status.utterance_id = admission.utterance_id
        status.reply_id = admission.reply_id
        status.function_request_id = admission.function_request_id
        status.function_call_name = admission.function_call_name
        status.state = state
        status.terminal = bool(terminal)
        status.success = bool(success)
        status.reason_code = reason_code
        self._status_pub.publish(status)

    @staticmethod
    def _dict_to_reply(payload: Mapping[str, Any]) -> HumanoidReply:
        message = HumanoidReply()
        message.stamp.sec = int(payload["stamp_sec"])
        message.stamp.nanosec = int(payload["stamp_nanosec"])
        for name in (
            "source",
            "correlation_id",
            "schema_version",
            "gateway_instance_id",
            "procedure_run_id",
            "utterance_id",
            "turn_id",
            "reply_id",
            "text",
            "kind",
            "timing",
            "function_call_name",
            "function_arguments_json",
            "function_request_id",
            "validation_error",
        ):
            setattr(message, name, str(payload.get(name, "")))
        message.source_epoch = int(payload.get("source_epoch", 0))
        message.source_sequence = int(payload.get("source_sequence", 0))
        message.speak = bool(payload.get("speak", False))
        message.valid = bool(payload.get("valid", False))
        return message

    @staticmethod
    def _dict_to_intent(payload: Mapping[str, Any]) -> VoiceCommandIntent:
        message = VoiceCommandIntent()
        header = payload.get("header", {})
        if isinstance(header, Mapping):
            message.header.stamp.sec = int(header.get("stamp_sec", 0))
            message.header.stamp.nanosec = int(header.get("stamp_nanosec", 0))
            message.header.frame_id = str(header.get("frame_id", ""))
        for name in (
            "utterance_id",
            "gateway_instance_id",
            "procedure_run_id",
            "function_request_id",
            "source",
            "source_speaker_role",
            "raw_text",
            "normalized_text",
            "procedure_id",
            "catalog_id",
            "intent",
            "tool_id",
            "retractor_command",
            "target_side",
            "urgency",
            "provenance",
            "disposition",
            "reason",
        ):
            setattr(message, name, str(payload.get(name, "")))
        message.source_is_final = bool(payload.get("source_is_final", False))
        message.source_has_confidence = bool(
            payload.get("source_has_confidence", False)
        )
        message.source_confidence = float(payload.get("source_confidence", 0.0))
        message.distance_m = float(payload.get("distance_m", 0.0))
        message.requires_confirmation = bool(
            payload.get("requires_confirmation", False)
        )
        message.evidence_spans = [
            str(item) for item in payload.get("evidence_spans", [])
        ]
        return message

    def _publish_admission(self, admission: DurableAdmission) -> None:
        scope = self._gate.scope
        if not self._scope_fresh() or scope is None or admission.scope != scope.identity:
            return
        ack_state = self._ledger.acknowledgement_state(admission.delivery_id)
        if ack_state is None or ack_state[2]:
            return
        reply_acked, intent_acked, _stale = ack_state
        if not reply_acked:
            self._reply_pub.publish(
                self._dict_to_reply(admission.reply_payload())
            )
        if admission.has_intent and not intent_acked:
            self._intent_pub.publish(
                self._dict_to_intent(admission.intent_payload())
            )

    def _retry_pending(self) -> None:
        if self._expire_gateway_scope_if_needed():
            return
        self._prune_input_facts()
        scope = self._gate.scope
        if not self._scope_fresh() or scope is None:
            return
        for admission in self._ledger.expire_intents(
            scope,
            now_ns=self._now_ns(),
        ):
            self._publish_status(
                admission,
                state="stale",
                success=False,
                terminal=True,
                reason_code="intent_source_ttl_expired",
            )
        admissions: dict[str, DurableAdmission] = {}
        for admission in self._ledger.pending_replies(scope, limit=64):
            admissions[admission.delivery_id] = admission
        for admission in self._ledger.pending_intents(scope, limit=64):
            admissions[admission.delivery_id] = admission
        for admission in admissions.values():
            self._publish_admission(admission)

    def _on_tts_status(self, message: TTSPlaybackStatus) -> None:
        scope = self._gate.scope
        if not self._scope_fresh() or scope is None:
            return
        reply_id = str(message.reply_id or "").strip()
        state = str(message.state or "").strip()
        if state not in TTS_RECEIPT_STATES:
            return
        terminal = bool(message.terminal)
        reported_success = bool(message.success)
        if state == "failed":
            if not terminal or reported_success:
                return
            terminal_outcome: bool | None = False
        elif state == "played":
            if not terminal or not reported_success:
                return
            terminal_outcome = True
        elif state == "duplicate_suppressed":
            terminal_outcome = reported_success if terminal else None
        else:
            if terminal:
                return
            terminal_outcome = None
        admission = self._ledger.get_by_reply_id(reply_id)
        if (
            admission is None
            or admission.scope != scope.identity
            or str(message.procedure_run_id or "").strip()
            != admission.procedure_run_id
            or str(message.utterance_id or "").strip()
            != admission.utterance_id
            or not self._ledger.ack_reply(
                reply_id,
                state,
                scope,
                success=terminal_outcome,
            )
        ):
            return
        self._publish_status(
            admission,
            state="reply_acked",
            success=terminal_outcome is not False,
            terminal=terminal_outcome is False,
            reason_code=f"tts_{state}",
        )
        self._publish_completed_if_ready(admission)

    def _on_twin_event(self, message: TwinEvent) -> None:
        scope = self._gate.scope
        if (
            not self._scope_fresh()
            or scope is None
            or str(message.event_type) != "VoiceCommandIntentObserved"
        ):
            return
        detail = _json_load_object(str(message.detail_json or ""))
        if detail is None:
            return
        function_request_id = str(
            detail.get("function_request_id", "")
        ).strip()
        admission = self._ledger.get_by_function_request_id(function_request_id)
        if (
            admission is None
            or admission.function_call_name != HANDOVER_FUNCTION
            or admission.scope != scope.identity
            or str(detail.get("gateway_instance_id", "")).strip()
            != scope.gateway_instance_id
            or str(detail.get("procedure_run_id", "")).strip()
            != scope.procedure_run_id
            or str(detail.get("utterance_id", "")).strip()
            != admission.utterance_id
        ):
            return
        accepted_value = detail.get("accepted")
        if not isinstance(accepted_value, bool):
            return
        accepted = accepted_value
        ack_state = "handover_intent_accepted" if accepted else "handover_intent_rejected"
        if not self._ledger.ack_intent(
            function_request_id,
            ack_state,
            scope,
            success=accepted,
        ):
            return
        self._publish_status(
            admission,
            state="intent_acked",
            success=accepted,
            terminal=not accepted,
            reason_code=ack_state,
        )
        self._publish_completed_if_ready(admission)

    def _on_retractor_status(self, message: String) -> None:
        scope = self._gate.scope
        if not self._scope_fresh() or scope is None:
            return
        detail = _json_load_object(str(message.data or ""))
        if detail is None:
            return
        if (
            str(detail.get("schema_version", "")).strip()
            != "retractor_voice_normalization.v1"
            or str(detail.get("interpreter_source", "")).strip()
            != "voice_command_intent"
        ):
            return
        function_request_id = str(
            detail.get("function_request_id", detail.get("request_id", ""))
        ).strip()
        admission = self._ledger.get_by_function_request_id(function_request_id)
        if (
            admission is None
            or admission.function_call_name != ADJUST_RETRACTION_FUNCTION
            or admission.scope != scope.identity
            or str(detail.get("gateway_instance_id", "")).strip()
            != scope.gateway_instance_id
            or str(detail.get("procedure_run_id", "")).strip()
            != scope.procedure_run_id
        ):
            return
        stage = str(detail.get("stage", "")).strip()
        accepted_by_stage = {
            "service_admitted": True,
            "service_not_admitted": False,
            "not_dispatched": False,
            "typed_intent_rejected": False,
        }
        if stage not in accepted_by_stage:
            return
        accepted = accepted_by_stage[stage]
        ack_state = (
            "retractor_intent_observed"
            if accepted
            else "retractor_intent_rejected"
        )
        if not self._ledger.ack_intent(
            function_request_id,
            ack_state,
            scope,
            success=accepted,
        ):
            return
        self._publish_status(
            admission,
            state="intent_acked",
            success=accepted,
            terminal=not accepted,
            reason_code=ack_state,
        )
        self._publish_completed_if_ready(admission)

    def _publish_completed_if_ready(self, admission: DurableAdmission) -> None:
        state = self._ledger.acknowledgement_state(admission.delivery_id)
        outcomes = self._ledger.acknowledgement_outcomes(
            admission.delivery_id
        )
        if state == (True, True, False) and outcomes == (True, True):
            self._publish_status(
                admission,
                state="completed",
                success=True,
                terminal=True,
                reason_code="independent_receipts_complete",
            )

    def destroy_node(self) -> bool:
        self._ledger.close()
        return super().destroy_node()


def main(args: list[str] | None = None) -> None:
    if not _ROS_AVAILABLE:
        raise RuntimeError("ROS dependencies are unavailable")
    rclpy.init(args=args)
    node = VLMFunctionAdmissionNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":  # pragma: no cover
    main()
