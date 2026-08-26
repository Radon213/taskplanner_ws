"""Durable, ROS-independent storage for one-shot humanoid replies.

The outbox deliberately stores scalar values only.  A ROS-facing producer can
therefore persist a complete ``HumanoidReply`` envelope before committing its
in-memory dialogue turn, then reconstruct and republish the envelope after a
process restart.  Speech text and function arguments are never logged here.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Iterator


ALLOWED_TIMINGS = frozenset(
    {"immediate", "on_function_accepted", "on_function_completed"}
)
ALLOWED_ACK_STATES = frozenset(
    {
        "queued",
        "waiting_function_accepted",
        "waiting_function_completed",
        "duplicate_suppressed",
        "playing",
        "played",
        "failed",
    }
)
_TERMINAL_ACK_STATES = frozenset(
    {"duplicate_suppressed", "played", "failed"}
)
_ACK_RANK = {
    "queued": 10,
    "waiting_function_accepted": 20,
    "waiting_function_completed": 20,
    "playing": 30,
    "duplicate_suppressed": 40,
    "played": 40,
    "failed": 40,
}
_MAX_PENDING_LIMIT = 1024


@dataclass(frozen=True, slots=True)
class ReplyEnvelope:
    """Scalar snapshot sufficient to reconstruct ``HumanoidReply`` exactly."""

    stamp_sec: int
    stamp_nanosec: int
    source: str
    source_epoch: int
    source_sequence: int
    correlation_id: str
    schema_version: str
    gateway_instance_id: str
    procedure_run_id: str
    utterance_id: str
    turn_id: str
    reply_id: str
    text: str = field(repr=False)
    kind: str
    timing: str
    speak: bool
    function_call_name: str
    function_arguments_json: str = field(repr=False)
    function_request_id: str
    valid: bool
    validation_error: str = field(repr=False)

    def __post_init__(self) -> None:
        for name in (
            "gateway_instance_id",
            "procedure_run_id",
            "utterance_id",
            "turn_id",
            "reply_id",
            "text",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if self.speak is not True:
            raise ValueError("speak must be true for a durable reply")
        if self.valid is not True:
            raise ValueError("valid must be true for a durable reply")
        if self.timing not in ALLOWED_TIMINGS:
            raise ValueError("timing is not an allowed humanoid reply timing")
        if not isinstance(self.stamp_sec, int) or self.stamp_sec < 0:
            raise ValueError("stamp_sec must be a non-negative integer")
        if (
            not isinstance(self.stamp_nanosec, int)
            or self.stamp_nanosec < 0
            or self.stamp_nanosec >= 1_000_000_000
        ):
            raise ValueError("stamp_nanosec must be in [0, 1000000000)")
        for name in ("source_epoch", "source_sequence"):
            value = getattr(self, name)
            if not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")


class ReplyCollisionError(RuntimeError):
    """Raised when a reply or logical turn would overwrite its first writer."""

    def __init__(
        self,
        *,
        reply_id: str,
        procedure_run_id: str,
        utterance_id: str,
    ) -> None:
        self.reply_id = reply_id
        self.procedure_run_id = procedure_run_id
        self.utterance_id = utterance_id
        super().__init__(
            "durable reply collision for reply_id/logical turn "
            f"({reply_id!r}, {procedure_run_id!r}, {utterance_id!r})"
        )


_ENVELOPE_COLUMNS = (
    "stamp_sec",
    "stamp_nanosec",
    "source",
    "source_epoch",
    "source_sequence",
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
    "speak",
    "function_call_name",
    "function_arguments_json",
    "function_request_id",
    "valid",
    "validation_error",
)
_SELECT_ENVELOPE = ", ".join(_ENVELOPE_COLUMNS)


class ReplyOutbox:
    """Small, thread-safe SQLite outbox with first-writer-wins semantics."""

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
            self._configure_connection()
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
            raise ValueError("reply outbox path must not be a symbolic link")
        parent = self._path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent.chmod(0o700)
        if self._path.exists() and not self._path.is_file():
            raise ValueError("reply outbox path must be a regular file")

    def _configure_connection(self) -> None:
        self._connection.execute("PRAGMA busy_timeout=10000")
        journal_mode = self._connection.execute(
            "PRAGMA journal_mode=WAL"
        ).fetchone()[0]
        if str(journal_mode).lower() != "wal":
            raise RuntimeError("reply outbox requires SQLite WAL mode")
        self._connection.execute("PRAGMA synchronous=FULL")
        synchronous = self._connection.execute(
            "PRAGMA synchronous"
        ).fetchone()[0]
        if int(synchronous) != 2:
            raise RuntimeError("reply outbox requires SQLite synchronous=FULL")
        self._connection.execute("PRAGMA trusted_schema=OFF")

    def _create_schema(self) -> None:
        with self._write_transaction():
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS reply_outbox (
                    reply_id TEXT PRIMARY KEY,
                    stamp_sec INTEGER NOT NULL,
                    stamp_nanosec INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    source_epoch INTEGER NOT NULL,
                    source_sequence INTEGER NOT NULL,
                    correlation_id TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    gateway_instance_id TEXT NOT NULL,
                    procedure_run_id TEXT NOT NULL,
                    utterance_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    timing TEXT NOT NULL,
                    speak INTEGER NOT NULL,
                    function_call_name TEXT NOT NULL,
                    function_arguments_json TEXT NOT NULL,
                    function_request_id TEXT NOT NULL,
                    valid INTEGER NOT NULL,
                    validation_error TEXT NOT NULL,
                    ack_state TEXT,
                    acked_at_ns INTEGER,
                    stale INTEGER NOT NULL DEFAULT 0,
                    stale_reason TEXT NOT NULL DEFAULT '',
                    enqueued_at_ns INTEGER NOT NULL,
                    updated_at_ns INTEGER NOT NULL,
                    UNIQUE (
                        gateway_instance_id,
                        procedure_run_id,
                        utterance_id
                    ),
                    CHECK (speak = 1),
                    CHECK (valid = 1),
                    CHECK (stale IN (0, 1)),
                    CHECK (
                        timing IN (
                            'immediate',
                            'on_function_accepted',
                            'on_function_completed'
                        )
                    ),
                    CHECK (
                        ack_state IS NULL OR ack_state IN (
                            'queued',
                            'waiting_function_accepted',
                            'waiting_function_completed',
                            'duplicate_suppressed',
                            'playing',
                            'played',
                            'failed'
                        )
                    )
                )
                """
            )
            columns = {
                str(row[1])
                for row in self._connection.execute(
                    "PRAGMA table_info(reply_outbox)"
                ).fetchall()
            }
            if "gateway_instance_id" not in columns:
                # Legacy rows cannot be attributed to a gateway epoch and are
                # therefore never eligible for scoped recovery.
                self._connection.execute(
                    "ALTER TABLE reply_outbox ADD COLUMN "
                    "gateway_instance_id TEXT NOT NULL DEFAULT ''"
                )
            unique_layouts: set[tuple[str, ...]] = set()
            for index_row in self._connection.execute(
                "PRAGMA index_list(reply_outbox)"
            ).fetchall():
                if not int(index_row["unique"]):
                    continue
                index_name = str(index_row["name"]).replace('"', '""')
                unique_layouts.add(
                    tuple(
                        str(column_row["name"])
                        for column_row in self._connection.execute(
                            f'PRAGMA index_info("{index_name}")'
                        ).fetchall()
                    )
                )
            if (
                ("procedure_run_id", "utterance_id") in unique_layouts
                and (
                    "gateway_instance_id",
                    "procedure_run_id",
                    "utterance_id",
                )
                not in unique_layouts
            ):
                self._migrate_legacy_logical_turn_key()
            self._connection.execute("PRAGMA user_version=3")

    def _migrate_legacy_logical_turn_key(self) -> None:
        """Replace the pre-epoch UNIQUE(run, utterance) constraint."""

        self._connection.execute("DROP TABLE IF EXISTS reply_outbox_v3")
        self._connection.execute(
            """
            CREATE TABLE reply_outbox_v3 (
                reply_id TEXT PRIMARY KEY,
                stamp_sec INTEGER NOT NULL,
                stamp_nanosec INTEGER NOT NULL,
                source TEXT NOT NULL,
                source_epoch INTEGER NOT NULL,
                source_sequence INTEGER NOT NULL,
                correlation_id TEXT NOT NULL,
                schema_version TEXT NOT NULL,
                gateway_instance_id TEXT NOT NULL,
                procedure_run_id TEXT NOT NULL,
                utterance_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                text TEXT NOT NULL,
                kind TEXT NOT NULL,
                timing TEXT NOT NULL,
                speak INTEGER NOT NULL,
                function_call_name TEXT NOT NULL,
                function_arguments_json TEXT NOT NULL,
                function_request_id TEXT NOT NULL,
                valid INTEGER NOT NULL,
                validation_error TEXT NOT NULL,
                ack_state TEXT,
                acked_at_ns INTEGER,
                stale INTEGER NOT NULL DEFAULT 0,
                stale_reason TEXT NOT NULL DEFAULT '',
                enqueued_at_ns INTEGER NOT NULL,
                updated_at_ns INTEGER NOT NULL,
                UNIQUE (
                    gateway_instance_id,
                    procedure_run_id,
                    utterance_id
                ),
                CHECK (speak = 1),
                CHECK (valid = 1),
                CHECK (stale IN (0, 1)),
                CHECK (
                    timing IN (
                        'immediate',
                        'on_function_accepted',
                        'on_function_completed'
                    )
                ),
                CHECK (
                    ack_state IS NULL OR ack_state IN (
                        'queued',
                        'waiting_function_accepted',
                        'waiting_function_completed',
                        'duplicate_suppressed',
                        'playing',
                        'played',
                        'failed'
                    )
                )
            )
            """
        )
        copy_columns = (
            *_ENVELOPE_COLUMNS,
            "ack_state",
            "acked_at_ns",
            "stale",
            "stale_reason",
            "enqueued_at_ns",
            "updated_at_ns",
        )
        column_list = ", ".join(copy_columns)
        self._connection.execute(
            f"INSERT INTO reply_outbox_v3 ({column_list}) "
            f"SELECT {column_list} FROM reply_outbox ORDER BY rowid"
        )
        self._connection.execute("DROP TABLE reply_outbox")
        self._connection.execute(
            "ALTER TABLE reply_outbox_v3 RENAME TO reply_outbox"
        )

    @contextmanager
    def _write_transaction(self) -> Iterator[None]:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        else:
            self._connection.execute("COMMIT")
            # synchronous=FULL makes the WAL commit durable.  Explicitly sync
            # the backing files as an additional persistence boundary before
            # returning to the dialogue producer.
            self._secure_files()
            self._fsync_storage()

    def _secure_files(self) -> None:
        self._path.parent.chmod(0o700)
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
            raise RuntimeError("reply outbox is closed")

    @staticmethod
    def _row_to_envelope(row: sqlite3.Row) -> ReplyEnvelope:
        values = {name: row[name] for name in _ENVELOPE_COLUMNS}
        values["speak"] = bool(values["speak"])
        values["valid"] = bool(values["valid"])
        return ReplyEnvelope(**values)

    def enqueue(self, envelope: ReplyEnvelope) -> bool:
        """Durably insert an envelope; return false for an exact redelivery.

        Neither an existing reply ID nor an existing ``(run, utterance)`` key
        is ever overwritten.  A non-identical reuse raises
        :class:`ReplyCollisionError`.
        """

        if not isinstance(envelope, ReplyEnvelope):
            raise TypeError("envelope must be a ReplyEnvelope")
        with self._lock:
            self._ensure_open()
            with self._write_transaction():
                by_reply = self._connection.execute(
                    f"SELECT {_SELECT_ENVELOPE} FROM reply_outbox "
                    "WHERE reply_id = ?",
                    (envelope.reply_id,),
                ).fetchone()
                if by_reply is not None:
                    if self._row_to_envelope(by_reply) == envelope:
                        return False
                    raise ReplyCollisionError(
                        reply_id=envelope.reply_id,
                        procedure_run_id=envelope.procedure_run_id,
                        utterance_id=envelope.utterance_id,
                    )
                by_turn = self._connection.execute(
                    "SELECT reply_id FROM reply_outbox "
                    "WHERE gateway_instance_id = ? "
                    "AND procedure_run_id = ? AND utterance_id = ?",
                    (
                        envelope.gateway_instance_id,
                        envelope.procedure_run_id,
                        envelope.utterance_id,
                    ),
                ).fetchone()
                if by_turn is not None:
                    raise ReplyCollisionError(
                        reply_id=envelope.reply_id,
                        procedure_run_id=envelope.procedure_run_id,
                        utterance_id=envelope.utterance_id,
                    )
                now_ns = time.time_ns()
                columns = ", ".join(_ENVELOPE_COLUMNS)
                placeholders = ", ".join("?" for _ in _ENVELOPE_COLUMNS)
                values = [getattr(envelope, name) for name in _ENVELOPE_COLUMNS]
                self._connection.execute(
                    f"INSERT INTO reply_outbox ({columns}, enqueued_at_ns, "
                    f"updated_at_ns) VALUES ({placeholders}, ?, ?)",
                    (*values, now_ns, now_ns),
                )
            return True

    def pending_for_run(
        self,
        active_run: str,
        *,
        limit: int = 64,
    ) -> list[ReplyEnvelope]:
        """Return unacknowledged, non-stale entries for one exact run ID."""

        if not isinstance(active_run, str) or not active_run.strip():
            raise ValueError("active_run must be a non-empty string")
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit < 1
            or limit > _MAX_PENDING_LIMIT
        ):
            raise ValueError(f"limit must be in [1, {_MAX_PENDING_LIMIT}]")
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                f"SELECT {_SELECT_ENVELOPE} FROM reply_outbox "
                "WHERE procedure_run_id = ? AND ack_state IS NULL "
                "AND stale = 0 ORDER BY rowid ASC LIMIT ?",
                (active_run, limit),
            ).fetchall()
            return [self._row_to_envelope(row) for row in rows]

    def pending_for_scope(
        self,
        gateway_instance_id: str,
        active_run: str,
        *,
        limit: int = 64,
    ) -> list[ReplyEnvelope]:
        """Return pending entries for one exact gateway epoch and run."""

        if not isinstance(gateway_instance_id, str) or not gateway_instance_id.strip():
            raise ValueError("gateway_instance_id must be a non-empty string")
        if not isinstance(active_run, str) or not active_run.strip():
            raise ValueError("active_run must be a non-empty string")
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit < 1
            or limit > _MAX_PENDING_LIMIT
        ):
            raise ValueError(f"limit must be in [1, {_MAX_PENDING_LIMIT}]")
        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                f"SELECT {_SELECT_ENVELOPE} FROM reply_outbox "
                "WHERE gateway_instance_id = ? AND procedure_run_id = ? "
                "AND ack_state IS NULL AND stale = 0 "
                "ORDER BY rowid ASC LIMIT ?",
                (gateway_instance_id, active_run, limit),
            ).fetchall()
            return [self._row_to_envelope(row) for row in rows]

    def pending_count(self, active_run: str | None = None) -> int:
        """Count unacknowledged, non-stale entries globally or for one run."""

        if active_run is not None and (
            not isinstance(active_run, str) or not active_run.strip()
        ):
            raise ValueError("active_run must be a non-empty string")
        with self._lock:
            self._ensure_open()
            if active_run is None:
                row = self._connection.execute(
                    "SELECT COUNT(*) FROM reply_outbox "
                    "WHERE ack_state IS NULL AND stale = 0"
                ).fetchone()
            else:
                row = self._connection.execute(
                    "SELECT COUNT(*) FROM reply_outbox "
                    "WHERE procedure_run_id = ? AND ack_state IS NULL "
                    "AND stale = 0",
                    (active_run,),
                ).fetchone()
            return int(row[0])

    def mark_acked(self, reply_id: str, ack_state: str) -> bool:
        """Record a monotonic consumer ACK; repeated/stale ACKs are no-ops."""

        if not isinstance(reply_id, str) or not reply_id.strip():
            raise ValueError("reply_id must be a non-empty string")
        if ack_state not in ALLOWED_ACK_STATES:
            raise ValueError("ack_state is not allowed")
        with self._lock:
            self._ensure_open()
            with self._write_transaction():
                row = self._connection.execute(
                    "SELECT ack_state FROM reply_outbox WHERE reply_id = ?",
                    (reply_id,),
                ).fetchone()
                if row is None:
                    return False
                current = row["ack_state"]
                if current == ack_state or current in _TERMINAL_ACK_STATES:
                    return False
                if current is not None and _ACK_RANK[ack_state] <= _ACK_RANK[current]:
                    return False
                self._connection.execute(
                    "UPDATE reply_outbox SET ack_state = ?, acked_at_ns = ?, "
                    "updated_at_ns = ? WHERE reply_id = ?",
                    (ack_state, time.time_ns(), time.time_ns(), reply_id),
                )
            return True

    def mark_stale(
        self,
        reply_id: str,
        reason: str = "dialogue_commit_rejected",
    ) -> bool:
        """Fail closed for a persisted envelope whose dialogue commit failed."""

        if not isinstance(reply_id, str) or not reply_id.strip():
            raise ValueError("reply_id must be a non-empty string")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason must be a non-empty string")
        with self._lock:
            self._ensure_open()
            with self._write_transaction():
                cursor = self._connection.execute(
                    "UPDATE reply_outbox SET stale = 1, stale_reason = ?, "
                    "updated_at_ns = ? WHERE reply_id = ? AND stale = 0",
                    (reason, time.time_ns(), reply_id),
                )
            return cursor.rowcount == 1

    def mark_stale_outside_run(self, active_run: str) -> int:
        """Exclude entries outside the run, or all pending entries while idle."""

        if not isinstance(active_run, str):
            raise ValueError("active_run must be a string")
        normalized_run = active_run.strip()
        with self._lock:
            self._ensure_open()
            with self._write_transaction():
                if normalized_run:
                    cursor = self._connection.execute(
                        "UPDATE reply_outbox SET stale = 1, "
                        "stale_reason = 'outside_active_run', updated_at_ns = ? "
                        "WHERE procedure_run_id <> ? AND stale = 0",
                        (time.time_ns(), normalized_run),
                    )
                else:
                    cursor = self._connection.execute(
                        "UPDATE reply_outbox SET stale = 1, "
                        "stale_reason = 'procedure_inactive', updated_at_ns = ? "
                        "WHERE stale = 0 AND ack_state IS NULL",
                        (time.time_ns(),),
                    )
            return int(cursor.rowcount)

    def mark_stale_outside_scope(
        self,
        gateway_instance_id: str,
        active_run: str,
    ) -> int:
        """Exclude pending entries outside one exact gateway/run authority.

        An empty component means that no active authority exists, so every
        unacknowledged envelope is fenced.  This prevents a reused run ID from
        reviving speech created by an earlier gateway process.
        """

        if not isinstance(gateway_instance_id, str):
            raise ValueError("gateway_instance_id must be a string")
        if not isinstance(active_run, str):
            raise ValueError("active_run must be a string")
        normalized_gateway = gateway_instance_id.strip()
        normalized_run = active_run.strip()
        with self._lock:
            self._ensure_open()
            with self._write_transaction():
                if normalized_gateway and normalized_run:
                    cursor = self._connection.execute(
                        "UPDATE reply_outbox SET stale = 1, "
                        "stale_reason = 'outside_active_scope', "
                        "updated_at_ns = ? WHERE "
                        "(gateway_instance_id <> ? OR procedure_run_id <> ?) "
                        "AND stale = 0 AND ack_state IS NULL",
                        (
                            time.time_ns(),
                            normalized_gateway,
                            normalized_run,
                        ),
                    )
                else:
                    cursor = self._connection.execute(
                        "UPDATE reply_outbox SET stale = 1, "
                        "stale_reason = 'authority_unavailable', "
                        "updated_at_ns = ? WHERE stale = 0 "
                        "AND ack_state IS NULL",
                        (time.time_ns(),),
                    )
            return int(cursor.rowcount)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._connection.close()
            self._closed = True
            self._secure_files()

    def __enter__(self) -> "ReplyOutbox":
        self._ensure_open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
