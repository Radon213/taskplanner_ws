"""Hardware-independent admission ledger and single-owner command worker.

The ROS service callback only validates and admits work.  This module owns the
durable at-most-once fence and the one worker thread used for physical calls;
it deliberately has no dependency on rclpy, the robot SDK, or CAN libraries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import heapq
import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Callable, Mapping, Protocol


_NONTERMINAL_STAGES = frozenset({"admitted", "queued", "running", "stopping"})
_TERMINAL_STAGES = frozenset(
    {"completed", "failed", "canceled", "rejected", "interrupted"}
)
_ALL_STAGES = _NONTERMINAL_STAGES | _TERMINAL_STAGES


def canonical_request_fingerprint(fields: Mapping[str, Any]) -> str:
    """Return a stable digest without persisting the request's raw values."""

    encoded = json.dumps(
        dict(fields),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class LedgerDecision(str, Enum):
    NEW = "new"
    DUPLICATE = "duplicate"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class LedgerReservation:
    decision: LedgerDecision
    command_id: str
    stage: str
    message: str = ""


@dataclass(frozen=True, slots=True)
class LedgerRecord:
    command_id: str
    fingerprint: str
    stage: str
    admission_accepted: bool
    admission_result_code: int
    admission_message: str
    result_code: str
    message: str
    created_ns: int
    updated_ns: int


class CommandLedger:
    """SQLite-backed at-most-once command ledger.

    IDs are not evicted: a command that was once admitted cannot unexpectedly
    become executable again after process restart or after a bounded cache
    rolls over.  Raw request bodies are never stored.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if not self.path.is_absolute():
            raise ValueError("command ledger path must be absolute")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.path,
            timeout=5.0,
            check_same_thread=False,
            isolation_level=None,
        )
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS commands (
                    command_id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    admission_accepted INTEGER NOT NULL DEFAULT 0,
                    admission_result_code INTEGER NOT NULL DEFAULT 255,
                    admission_message TEXT NOT NULL DEFAULT '',
                    result_code TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL DEFAULT '',
                    created_ns INTEGER NOT NULL,
                    updated_ns INTEGER NOT NULL
                )
                """
            )

    def record_admission(
        self,
        command_id: str,
        *,
        accepted: bool,
        result_code: int,
        message: str,
        stage: str,
    ) -> None:
        """Persist the stable Service response separately from execution."""

        if stage not in _ALL_STAGES:
            raise ValueError(f"unsupported ledger stage: {stage}")
        normalized_id = str(command_id).strip()
        with self._lock:
            cursor = self._connection.execute(
                """
                UPDATE commands
                   SET admission_accepted = ?, admission_result_code = ?,
                       admission_message = ?, stage = ?, updated_ns = ?
                 WHERE command_id = ?
                """,
                (
                    int(bool(accepted)),
                    int(result_code),
                    str(message).strip(),
                    stage,
                    time.time_ns(),
                    normalized_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(normalized_id)

    def reserve(self, command_id: str, fingerprint: str) -> LedgerReservation:
        normalized_id = str(command_id).strip()
        normalized_fingerprint = str(fingerprint).strip().lower()
        if not normalized_id:
            raise ValueError("command_id must not be empty")
        if len(normalized_fingerprint) != 64 or any(
            char not in "0123456789abcdef" for char in normalized_fingerprint
        ):
            raise ValueError("fingerprint must be a lowercase SHA-256 digest")

        now_ns = time.time_ns()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT fingerprint, stage, message FROM commands WHERE command_id = ?",
                    (normalized_id,),
                ).fetchone()
                if row is None:
                    self._connection.execute(
                        """
                        INSERT INTO commands
                            (command_id, fingerprint, stage, created_ns, updated_ns)
                        VALUES (?, ?, 'admitted', ?, ?)
                        """,
                        (
                            normalized_id,
                            normalized_fingerprint,
                            now_ns,
                            now_ns,
                        ),
                    )
                    self._connection.execute("COMMIT")
                    return LedgerReservation(
                        LedgerDecision.NEW, normalized_id, "admitted"
                    )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

        previous_fingerprint, stage, message = row
        if previous_fingerprint == normalized_fingerprint:
            return LedgerReservation(
                LedgerDecision.DUPLICATE,
                normalized_id,
                str(stage),
                str(message),
            )
        return LedgerReservation(
            LedgerDecision.CONFLICT,
            normalized_id,
            str(stage),
            "command_id_reused_with_different_payload",
        )

    def update(
        self,
        command_id: str,
        stage: str,
        *,
        result_code: str = "",
        message: str = "",
    ) -> None:
        if stage not in _ALL_STAGES:
            raise ValueError(f"unsupported ledger stage: {stage}")
        normalized_id = str(command_id).strip()
        with self._lock:
            cursor = self._connection.execute(
                """
                UPDATE commands
                   SET stage = ?, result_code = ?, message = ?, updated_ns = ?
                 WHERE command_id = ?
                """,
                (
                    stage,
                    str(result_code).strip(),
                    str(message).strip(),
                    time.time_ns(),
                    normalized_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(normalized_id)

    def get(self, command_id: str) -> LedgerRecord | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT command_id, fingerprint, stage,
                       admission_accepted, admission_result_code,
                       admission_message, result_code, message, created_ns,
                       updated_ns
                  FROM commands WHERE command_id = ?
                """,
                (str(command_id).strip(),),
            ).fetchone()
        if row is None:
            return None
        values = list(row)
        values[3] = bool(values[3])
        return LedgerRecord(*values)

    def mark_interrupted(self) -> tuple[str, ...]:
        """Fail-close commands left nonterminal by an earlier process."""

        placeholders = ",".join("?" for _ in _NONTERMINAL_STAGES)
        stages = tuple(sorted(_NONTERMINAL_STAGES))
        with self._lock:
            rows = self._connection.execute(
                f"SELECT command_id FROM commands WHERE stage IN ({placeholders})",
                stages,
            ).fetchall()
            command_ids = tuple(str(row[0]) for row in rows)
            if command_ids:
                self._connection.execute(
                    f"""
                    UPDATE commands
                       SET stage = 'interrupted',
                           result_code = 'process_restarted',
                           message = 'execution_state_unknown_after_restart',
                           updated_ns = ?
                     WHERE stage IN ({placeholders})
                    """,
                    (time.time_ns(), *stages),
                )
        return command_ids

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "CommandLedger":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


@dataclass(frozen=True, slots=True)
class RuntimeCommand:
    command_id: str
    command: int
    payload: object
    is_stop: bool = False


@dataclass(frozen=True, slots=True)
class ExecutionReport:
    success: bool
    result_code: str
    message: str = ""
    canceled: bool = False


class CommandExecutor(Protocol):
    def __call__(
        self, command: RuntimeCommand, stop_requested: threading.Event
    ) -> ExecutionReport: ...


@dataclass(order=True, slots=True)
class _QueueItem:
    priority: int
    sequence: int
    command: RuntimeCommand = field(compare=False)


class SubmitDecision(str, Enum):
    QUEUED = "queued"
    BUSY = "busy"
    STOP_ALREADY_PENDING = "stop_already_pending"
    SHUTTING_DOWN = "shutting_down"


class CommandWorker:
    """One physical-execution worker with an out-of-band stop signal."""

    def __init__(
        self,
        executor: CommandExecutor,
        ledger: CommandLedger,
        *,
        max_pending: int = 16,
        on_stage: Callable[[str, str, str], None] | None = None,
        thread_name: str = "retraction-control-worker",
    ) -> None:
        if max_pending < 1:
            raise ValueError("max_pending must be positive")
        self._executor = executor
        self._ledger = ledger
        self._max_pending = int(max_pending)
        self._on_stage = on_stage
        self._condition = threading.Condition()
        self._items: list[_QueueItem] = []
        self._sequence = 0
        self._stop_pending = False
        self._shutdown = False
        self._active_command_id = ""
        self._stop_requested = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=thread_name,
            daemon=True,
        )

    @property
    def active_command_id(self) -> str:
        with self._condition:
            return self._active_command_id

    @property
    def pending_count(self) -> int:
        with self._condition:
            return len(self._items)

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested.is_set()

    def start(self) -> None:
        self._thread.start()

    def submit(self, command: RuntimeCommand) -> SubmitDecision:
        with self._condition:
            if self._shutdown:
                return SubmitDecision.SHUTTING_DOWN
            if command.is_stop:
                if self._stop_pending:
                    return SubmitDecision.STOP_ALREADY_PENDING
                self._stop_requested.set()
                self._stop_pending = True
                priority = 0
            else:
                if len(self._items) >= self._max_pending:
                    return SubmitDecision.BUSY
                priority = 10
            self._sequence += 1
            heapq.heappush(
                self._items,
                _QueueItem(priority, self._sequence, command),
            )
            self._ledger.update(command.command_id, "queued")
            self._condition.notify()
        self._notify(command.command_id, "queued", "")
        return SubmitDecision.QUEUED

    def shutdown(self, timeout_sec: float = 5.0) -> bool:
        self._stop_requested.set()
        with self._condition:
            self._shutdown = True
            pending = tuple(item.command for item in self._items)
            self._items.clear()
            self._stop_pending = False
            self._condition.notify_all()
        for command in pending:
            self._ledger.update(
                command.command_id,
                "canceled",
                result_code="controller_shutting_down",
                message="queued command was not executed during shutdown",
            )
            self._notify(
                command.command_id,
                "canceled",
                "queued command was not executed during shutdown",
            )
        if self._thread.ident is None:
            return True
        self._thread.join(max(0.0, float(timeout_sec)))
        return not self._thread.is_alive()

    def _notify(self, command_id: str, stage: str, message: str) -> None:
        if self._on_stage is not None:
            self._on_stage(command_id, stage, message)

    def _take(self) -> RuntimeCommand | None:
        with self._condition:
            while not self._items and not self._shutdown:
                self._condition.wait()
            if self._shutdown or not self._items:
                return None
            item = heapq.heappop(self._items)
            self._active_command_id = item.command.command_id
            return item.command

    def _finish_active(self, command: RuntimeCommand) -> None:
        with self._condition:
            self._active_command_id = ""
            if command.is_stop:
                self._stop_pending = False
                self._stop_requested.clear()

    def _run(self) -> None:
        while True:
            command = self._take()
            if command is None:
                return
            stage = "stopping" if command.is_stop else "running"
            self._ledger.update(command.command_id, stage)
            self._notify(command.command_id, stage, "")
            try:
                report = self._executor(command, self._stop_requested)
            except Exception as exc:  # fail closed at the worker boundary
                report = ExecutionReport(
                    False,
                    "unhandled_execution_error",
                    f"{type(exc).__name__}: {exc}",
                )
            terminal_stage = (
                "canceled"
                if report.canceled
                else "completed"
                if report.success
                else "failed"
            )
            self._ledger.update(
                command.command_id,
                terminal_stage,
                result_code=report.result_code,
                message=report.message,
            )
            self._notify(command.command_id, terminal_stage, report.message)
            self._finish_active(command)
