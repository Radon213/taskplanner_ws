"""Durable first-writer receipts for gated VLM voice intents.

The receipt is an idempotency boundary, not execution authority.  A caller
holds the SQLite write transaction while it validates and applies one reducer
mutation, then the receipt is committed before its ``TwinEvent`` is emitted.
Exact retries recover the stored event without invoking the reducer callback.

Only a SHA-256 digest of a closed, transcript-free semantic frame is stored.
Raw/normalised speech and evidence spans are deliberately outside this module.
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
from typing import Any, Callable, Iterator, Mapping


_FINGERPRINT_FIELDS = (
    "utterance_id",
    "source_stamp_sec",
    "source_stamp_nanosec",
    "source",
    "source_is_final",
    "source_speaker_role",
    "source_has_confidence",
    "source_confidence",
    "procedure_id",
    "catalog_id",
    "intent",
    "tool_id",
    "retractor_command",
    "target_side",
    "distance_m",
    "urgency",
    "provenance",
    "requires_confirmation",
    "disposition",
    "reason",
)

_RECEIPT_DETAIL_FIELDS = frozenset(
    {
        "accepted",
        "catalog_id",
        "category",
        "display_key",
        "disposition",
        "function_request_id",
        "gateway_instance_id",
        "intent",
        "mode",
        "normalized_text_present",
        "procedure_id",
        "procedure_run_id",
        "provenance",
        "raw_text_present",
        "reason",
        "request_generation",
        "requires_confirmation",
        "resolved_tool",
        "resolver_reason",
        "severity",
        "source",
        "source_is_final",
        "tone",
        "tool_id",
        "urgency",
        "urgency_applied_to_execution",
        "utterance_id",
    }
)


def _canonical_scalar(value: Any) -> str | bool | int:
    """Return a JSON-stable scalar without permitting NaN/Infinity."""

    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return "nonfinite"
        # A string avoids differences between SQLite/Python JSON float
        # formatting while retaining the complete binary64 value.
        return format(value, ".17g")
    if value is None:
        return ""
    return str(value).strip()


def voice_intent_fingerprint(payload: Mapping[str, Any]) -> str:
    """Hash the reviewed semantic envelope, excluding all transcript fields.

    Unknown keys are ignored.  In particular, ``raw_text``,
    ``normalized_text`` and ``evidence_spans`` can never enter the canonical
    representation even if a caller accidentally includes them.
    """

    canonical = {
        field_name: _canonical_scalar(payload.get(field_name))
        for field_name in _FINGERPRINT_FIELDS
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _receipt_detail(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Copy only the closed receipt schema; reject transcript-capable extras."""

    detail = dict(payload)
    unknown = sorted(set(detail) - _RECEIPT_DETAIL_FIELDS)
    if unknown:
        raise ValueError(
            "voice receipt detail contains unsupported fields: "
            + ",".join(unknown)
        )
    # These presence booleans retain useful audit evidence without retaining
    # either transcript.  Enforce their type so a caller cannot smuggle text
    # into the similarly named fields.
    for field_name in ("raw_text_present", "normalized_text_present"):
        if field_name in detail and not isinstance(detail[field_name], bool):
            raise ValueError(
                f"voice receipt {field_name} must be boolean"
            )
    return detail


@dataclass(frozen=True, slots=True)
class VoiceIntentReceiptKey:
    gateway_instance_id: str
    procedure_run_id: str
    function_request_id: str

    def __post_init__(self) -> None:
        if not all(
            str(value or "").strip()
            for value in (
                self.gateway_instance_id,
                self.procedure_run_id,
                self.function_request_id,
            )
        ):
            raise ValueError("voice receipt key fields must be non-empty")


@dataclass(frozen=True, slots=True)
class VoiceIntentReceiptDraft:
    instrument_id: str
    mode: str
    detail: Mapping[str, Any] = field(repr=False)
    accepted: bool = False


@dataclass(frozen=True, slots=True)
class StoredVoiceIntentReceipt:
    key: VoiceIntentReceiptKey
    utterance_id: str
    fingerprint_sha256: str
    instrument_id: str
    mode: str
    detail_json: str = field(repr=False)
    accepted: bool = False

    def detail(self) -> dict[str, Any]:
        parsed = json.loads(self.detail_json)
        if not isinstance(parsed, dict):
            raise ValueError("stored voice receipt detail is not an object")
        return parsed


@dataclass(frozen=True, slots=True)
class FirstWriterReceiptResult:
    state: str
    receipt: StoredVoiceIntentReceipt = field(repr=False)

    @property
    def is_new(self) -> bool:
        return self.state == "new"

    @property
    def is_duplicate(self) -> bool:
        return self.state == "duplicate"

    @property
    def is_collision(self) -> bool:
        return self.state == "collision"


class VoiceIntentReceiptLedger:
    """Secure SQLite ledger for one immutable ODT admission receipt per key."""

    _SELECT = (
        "gateway_instance_id, procedure_run_id, function_request_id, "
        "utterance_id, fingerprint_sha256, instrument_id, mode, detail_json, "
        "accepted"
    )

    def __init__(self, path: str | os.PathLike[str]) -> None:
        raw_path = str(path)
        if not raw_path.strip():
            raise ValueError("voice receipt ledger path is empty")
        self._memory = raw_path == ":memory:"
        self._path = None if self._memory else Path(raw_path).expanduser()
        self._lock = threading.RLock()
        self._closed = False
        if self._path is not None:
            if not self._path.is_absolute():
                raise ValueError("voice receipt ledger path must be absolute")
            self._prepare_path()
        self._connection = sqlite3.connect(
            raw_path if self._memory else str(self._path),
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
    def path(self) -> Path | None:
        return self._path

    def _prepare_path(self) -> None:
        assert self._path is not None
        if self._path.is_symlink():
            raise ValueError("voice receipt ledger path must not be a symlink")
        parent = self._path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if parent.is_symlink() or parent.resolve() != parent.absolute():
            raise ValueError("voice receipt directory must not traverse symlinks")
        parent_stat = parent.stat()
        if parent_stat.st_uid != os.getuid():
            raise PermissionError("voice receipt directory has a foreign owner")
        if parent_stat.st_mode & 0o077:
            raise PermissionError("voice receipt directory permissions must be 0700")
        if self._path.exists() and not self._path.is_file():
            raise ValueError("voice receipt ledger path must be a regular file")
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
                raise PermissionError("voice receipt ledger has a foreign owner")
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)

    def _configure(self) -> None:
        self._connection.execute("PRAGMA busy_timeout=10000")
        if not self._memory:
            mode = self._connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                raise RuntimeError("voice receipt ledger requires WAL mode")
            self._connection.execute("PRAGMA synchronous=FULL")
            synchronous = self._connection.execute(
                "PRAGMA synchronous"
            ).fetchone()[0]
            if int(synchronous) != 2:
                raise RuntimeError(
                    "voice receipt ledger requires synchronous=FULL"
                )
        self._connection.execute("PRAGMA trusted_schema=OFF")

    def _create_schema(self) -> None:
        with self._write_transaction():
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS voice_intent_receipts (
                    gateway_instance_id TEXT NOT NULL,
                    procedure_run_id TEXT NOT NULL,
                    function_request_id TEXT NOT NULL,
                    utterance_id TEXT NOT NULL,
                    fingerprint_sha256 TEXT NOT NULL,
                    instrument_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    accepted INTEGER NOT NULL,
                    created_at_ns INTEGER NOT NULL,
                    PRIMARY KEY (
                        gateway_instance_id,
                        procedure_run_id,
                        function_request_id
                    ),
                    CHECK (accepted IN (0, 1)),
                    CHECK (length(fingerprint_sha256) = 64)
                ) WITHOUT ROWID
                """
            )
            self._connection.execute("PRAGMA user_version=1")

    @contextmanager
    def _write_transaction(self) -> Iterator[None]:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            # Secure and sync every file that SQLite may have created while
            # the transaction is still rollback-capable.  The final COMMIT is
            # itself durable because WAL mode is configured with
            # synchronous=FULL; no Python operation that can fail follows it.
            # This keeps callers from observing a raised post-COMMIT error for
            # a row which actually became the durable first writer.
            self._secure_files()
            self._fsync_storage()
            self._connection.execute("COMMIT")
        except BaseException:
            if self._connection.in_transaction:
                try:
                    self._connection.execute("ROLLBACK")
                except sqlite3.Error:
                    # Preserve the initiating storage/SQLite exception.  The
                    # caller latches its in-memory reducer fail-closed if it
                    # cannot restore the pre-mutation snapshot.
                    pass
            raise

    def _secure_files(self) -> None:
        if self._path is None:
            return
        parent_stat = self._path.parent.stat()
        if parent_stat.st_uid != os.getuid() or parent_stat.st_mode & 0o077:
            raise PermissionError("voice receipt directory lost 0700 protection")
        for candidate in (
            self._path,
            Path(f"{self._path}-wal"),
            Path(f"{self._path}-shm"),
        ):
            if candidate.exists():
                candidate.chmod(0o600)

    def _fsync_storage(self) -> None:
        if self._path is None:
            return
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
        directory_fd = os.open(
            self._path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("voice receipt ledger is closed")

    def _lookup_in_transaction(
        self, key: VoiceIntentReceiptKey
    ) -> StoredVoiceIntentReceipt | None:
        row = self._connection.execute(
            f"""
            SELECT {self._SELECT}
              FROM voice_intent_receipts
             WHERE gateway_instance_id = ?
               AND procedure_run_id = ?
               AND function_request_id = ?
            """,
            (
                key.gateway_instance_id,
                key.procedure_run_id,
                key.function_request_id,
            ),
        ).fetchone()
        return self._row_to_receipt(row) if row is not None else None

    @staticmethod
    def _row_to_receipt(row: sqlite3.Row) -> StoredVoiceIntentReceipt:
        return StoredVoiceIntentReceipt(
            key=VoiceIntentReceiptKey(
                str(row["gateway_instance_id"]),
                str(row["procedure_run_id"]),
                str(row["function_request_id"]),
            ),
            utterance_id=str(row["utterance_id"]),
            fingerprint_sha256=str(row["fingerprint_sha256"]),
            instrument_id=str(row["instrument_id"]),
            mode=str(row["mode"]),
            detail_json=str(row["detail_json"]),
            accepted=bool(row["accepted"]),
        )

    def first_writer(
        self,
        *,
        key: VoiceIntentReceiptKey,
        utterance_id: str,
        fingerprint_sha256: str,
        build_receipt: Callable[[], VoiceIntentReceiptDraft],
    ) -> FirstWriterReceiptResult:
        """Run ``build_receipt`` at most once and durably commit its output.

        A collision returns the immutable first receipt just like a duplicate;
        callers can therefore re-emit the first ACK while never admitting the
        conflicting payload.
        """

        normalized_utterance_id = str(utterance_id or "").strip()
        normalized_fingerprint = str(fingerprint_sha256 or "").strip().lower()
        if not normalized_utterance_id:
            raise ValueError("voice receipt utterance_id must be non-empty")
        if (
            len(normalized_fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in normalized_fingerprint)
        ):
            raise ValueError("voice receipt fingerprint must be SHA-256 hex")
        with self._lock:
            self._require_open()
            with self._write_transaction():
                existing = self._lookup_in_transaction(key)
                if existing is not None:
                    state = (
                        "duplicate"
                        if existing.utterance_id == normalized_utterance_id
                        and existing.fingerprint_sha256 == normalized_fingerprint
                        else "collision"
                    )
                    return FirstWriterReceiptResult(state, existing)

                draft = build_receipt()
                detail_json = json.dumps(
                    _receipt_detail(draft.detail),
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                    allow_nan=False,
                )
                now_ns = time.time_ns()
                self._connection.execute(
                    """
                    INSERT INTO voice_intent_receipts (
                        gateway_instance_id,
                        procedure_run_id,
                        function_request_id,
                        utterance_id,
                        fingerprint_sha256,
                        instrument_id,
                        mode,
                        detail_json,
                        accepted,
                        created_at_ns
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        key.gateway_instance_id,
                        key.procedure_run_id,
                        key.function_request_id,
                        normalized_utterance_id,
                        normalized_fingerprint,
                        str(draft.instrument_id or "").strip(),
                        str(draft.mode or "").strip(),
                        detail_json,
                        int(bool(draft.accepted)),
                        now_ns,
                    ),
                )
                receipt = StoredVoiceIntentReceipt(
                    key=key,
                    utterance_id=normalized_utterance_id,
                    fingerprint_sha256=normalized_fingerprint,
                    instrument_id=str(draft.instrument_id or "").strip(),
                    mode=str(draft.mode or "").strip(),
                    detail_json=detail_json,
                    accepted=bool(draft.accepted),
                )
            return FirstWriterReceiptResult("new", receipt)

    def lookup(
        self, key: VoiceIntentReceiptKey
    ) -> StoredVoiceIntentReceipt | None:
        with self._lock:
            self._require_open()
            return self._lookup_in_transaction(key)

    def count(self) -> int:
        with self._lock:
            self._require_open()
            row = self._connection.execute(
                "SELECT COUNT(*) FROM voice_intent_receipts"
            ).fetchone()
            return int(row[0])

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                if not self._memory:
                    self._connection.execute("PRAGMA wal_checkpoint(FULL)")
                    self._secure_files()
                    self._fsync_storage()
            finally:
                self._connection.close()
                self._closed = True

    def __enter__(self) -> "VoiceIntentReceiptLedger":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


@dataclass(frozen=True, slots=True)
class GatewayLeaseScope:
    gateway_instance_id: str
    procedure_run_id: str
    procedure_type: str
    catalog_version: str
    schema_version: str
    interface_version: str

    @property
    def identity(self) -> tuple[str, str]:
        return self.gateway_instance_id, self.procedure_run_id


class GatewayLeaseAuthority:
    """Pure steady-clock authority for the public GatewayInfo heartbeat.

    Gateway epochs have no natural ordering.  A strictly newer source stamp
    may switch to a new epoch, after which the previous identity is retired
    for this ODT process.  Metadata is immutable within an epoch/run; mutation
    poisons that identity until either component changes.
    """

    def __init__(self, *, timeout_sec: float = 3.0) -> None:
        timeout = float(timeout_sec)
        if not math.isfinite(timeout) or timeout <= 0.0:
            raise ValueError("gateway lease timeout must be positive")
        self.timeout_sec = timeout
        self._lock = threading.RLock()
        self._scope: GatewayLeaseScope | None = None
        self._last_seen_monotonic: float | None = None
        self._last_source_stamp_ns = 0
        self._last_revision_by_gateway: dict[str, int] = {}
        self._last_stamp_by_gateway: dict[str, int] = {}
        self._metadata_by_identity: dict[tuple[str, str], tuple[str, ...]] = {}
        self._retired: set[tuple[str, str]] = set()
        self._poisoned: set[tuple[str, str]] = set()
        self._unavailable_reason = "gateway_lease_missing"

    @property
    def scope(self) -> GatewayLeaseScope | None:
        with self._lock:
            return self._scope

    @property
    def unavailable_reason(self) -> str:
        with self._lock:
            return self._unavailable_reason

    def _withdraw(self, reason: str, *, retire: bool) -> None:
        if retire and self._scope is not None:
            self._retired.add(self._scope.identity)
        self._scope = None
        self._last_seen_monotonic = None
        self._unavailable_reason = str(reason or "gateway_lease_unavailable")

    def expire(self, *, now_monotonic: float) -> bool:
        with self._lock:
            return self._expire_unlocked(now_monotonic=now_monotonic)

    def _expire_unlocked(self, *, now_monotonic: float) -> bool:
        if self._scope is None or self._last_seen_monotonic is None:
            return False
        if (
            float(now_monotonic) - self._last_seen_monotonic
            <= self.timeout_sec
        ):
            return False
        # Timeout does not retire the identity. A later fresh, monotonic
        # heartbeat may restore the same scope without changing first-writer
        # semantics.
        self._withdraw("gateway_heartbeat_timeout", retire=False)
        return True

    def observe(
        self,
        *,
        gateway_instance_id: str,
        procedure_run_id: str,
        procedure_type: str,
        catalog_version: str,
        schema_version: str,
        interface_version: str,
        procedure_active: bool,
        revision: int,
        source_stamp_ns: int,
        now_ns: int,
        received_monotonic: float,
        expected_procedure_type: str,
    ) -> str:
        """Observe one heartbeat and return its closed reason code."""

        with self._lock:
            return self._observe_unlocked(
                gateway_instance_id=gateway_instance_id,
                procedure_run_id=procedure_run_id,
                procedure_type=procedure_type,
                catalog_version=catalog_version,
                schema_version=schema_version,
                interface_version=interface_version,
                procedure_active=procedure_active,
                revision=revision,
                source_stamp_ns=source_stamp_ns,
                now_ns=now_ns,
                received_monotonic=received_monotonic,
                expected_procedure_type=expected_procedure_type,
            )

    def _observe_unlocked(
        self,
        *,
        gateway_instance_id: str,
        procedure_run_id: str,
        procedure_type: str,
        catalog_version: str,
        schema_version: str,
        interface_version: str,
        procedure_active: bool,
        revision: int,
        source_stamp_ns: int,
        now_ns: int,
        received_monotonic: float,
        expected_procedure_type: str,
    ) -> str:

        gateway_id = str(gateway_instance_id or "").strip()
        run_id = str(procedure_run_id or "").strip()
        procedure = str(procedure_type or "").strip()
        catalog = str(catalog_version or "").strip()
        schema = str(schema_version or "").strip()
        interface = str(interface_version or "").strip()
        expected = str(expected_procedure_type or "").strip()
        revision_value = int(revision)
        stamp_ns = int(source_stamp_ns)
        local_now_ns = int(now_ns)
        if (
            not gateway_id
            or revision_value <= 0
            or stamp_ns <= 0
            or local_now_ns <= 0
        ):
            self._withdraw("gateway_heartbeat_malformed", retire=False)
            return self._unavailable_reason
        age_sec = (local_now_ns - stamp_ns) / 1_000_000_000.0
        # No future tolerance is granted at this execution boundary.
        if age_sec < 0.0:
            self._withdraw("gateway_heartbeat_future", retire=False)
            return self._unavailable_reason
        if age_sec > self.timeout_sec:
            self._withdraw("gateway_heartbeat_stale", retire=False)
            return self._unavailable_reason

        previous_revision = self._last_revision_by_gateway.get(gateway_id, -1)
        previous_stamp = self._last_stamp_by_gateway.get(gateway_id, 0)
        if revision_value <= previous_revision or stamp_ns <= previous_stamp:
            # Retained/replayed data is not a heartbeat and cannot extend a
            # current steady-clock lease.
            return "gateway_heartbeat_replayed"
        if (
            gateway_id not in self._last_revision_by_gateway
            and stamp_ns <= self._last_source_stamp_ns
        ):
            return "gateway_epoch_not_newer"
        self._last_revision_by_gateway[gateway_id] = revision_value
        self._last_stamp_by_gateway[gateway_id] = stamp_ns
        self._last_source_stamp_ns = max(self._last_source_stamp_ns, stamp_ns)

        if not bool(procedure_active):
            if run_id:
                self._withdraw("inactive_gateway_has_run_id", retire=True)
                return self._unavailable_reason
            self._withdraw("gateway_procedure_inactive", retire=True)
            return self._unavailable_reason
        if not all((run_id, procedure, catalog, schema, interface, expected)):
            self._withdraw("active_gateway_metadata_missing", retire=True)
            return self._unavailable_reason

        identity = (gateway_id, run_id)
        metadata = (procedure, catalog, schema, interface)
        if identity in self._retired:
            return "gateway_scope_retired"
        if identity in self._poisoned:
            self._withdraw("gateway_scope_metadata_poisoned", retire=False)
            return self._unavailable_reason
        previous_metadata = self._metadata_by_identity.get(identity)
        if previous_metadata is not None and previous_metadata != metadata:
            self._poisoned.add(identity)
            self._withdraw("gateway_scope_metadata_mutation", retire=True)
            return self._unavailable_reason
        if procedure != expected:
            self._poisoned.add(identity)
            self._withdraw("gateway_procedure_type_mismatch", retire=True)
            return self._unavailable_reason

        if self._scope is not None and self._scope.identity != identity:
            self._retired.add(self._scope.identity)
        self._metadata_by_identity.setdefault(identity, metadata)
        self._scope = GatewayLeaseScope(
            gateway_instance_id=gateway_id,
            procedure_run_id=run_id,
            procedure_type=procedure,
            catalog_version=catalog,
            schema_version=schema,
            interface_version=interface,
        )
        # Account for DDS delivery age instead of granting a fresh full lease
        # at callback receipt time.
        self._last_seen_monotonic = float(received_monotonic) - age_sec
        self._unavailable_reason = ""
        return "gateway_scope_active"

    def rejection_reason(
        self,
        *,
        gateway_instance_id: str,
        procedure_run_id: str,
        twin_procedure_run_id: str,
        expected_procedure_type: str,
        now_monotonic: float,
    ) -> str:
        with self._lock:
            self._expire_unlocked(now_monotonic=float(now_monotonic))
            scope = self._scope
            if scope is None:
                return self._unavailable_reason or "gateway_lease_unavailable"
            if scope.procedure_type != str(
                expected_procedure_type or ""
            ).strip():
                return "gateway_procedure_type_mismatch"
            if (
                str(gateway_instance_id or "").strip()
                != scope.gateway_instance_id
            ):
                return "gated_voice_intent_gateway_instance_id_mismatch"
            run_id = str(procedure_run_id or "").strip()
            if run_id != scope.procedure_run_id:
                return "gated_voice_intent_gateway_run_id_mismatch"
            if run_id != str(twin_procedure_run_id or "").strip():
                return "gated_voice_intent_twin_run_id_mismatch"
            return ""
