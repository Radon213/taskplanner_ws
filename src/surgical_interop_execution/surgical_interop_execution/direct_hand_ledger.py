"""Durable at-most-once reservations for direct CAM4 hand episodes."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading
import time


_RUN_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_HANDOVER_ACTIONS = frozenset({"direct_handover", "pick_up_and_handover"})


@dataclass(frozen=True, slots=True)
class DirectHandReservation:
    accepted: bool
    reason: str
    payload_sha256: str


def valid_procedure_run_id(value: str) -> bool:
    return bool(_RUN_ID_RE.fullmatch(str(value or "")))


def direct_hand_payload_sha256(
    *,
    action: str,
    instrument_id: str,
    instrument_instance_id: str,
    source_location: str,
    target_location: str,
) -> str:
    payload = {
        "action": str(action).strip(),
        "instrument_id": str(instrument_id).strip(),
        "instrument_instance_id": str(instrument_instance_id).strip(),
        "source_location": str(source_location).strip().casefold(),
        "target_location": str(target_location).strip().casefold(),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class DurableDirectHandLedger:
    """SQLite reservation ledger committed before any Action Goal is sent.

    A process crash after reservation can suppress an otherwise valid retry.
    That is deliberate: an unknown physical submission must never be replayed
    automatically. A fresh release/open-palm episode creates a new generation.
    """

    def __init__(self, database_path: str) -> None:
        raw_path = str(database_path or "").strip()
        if not raw_path:
            raise ValueError("direct hand ledger path is empty")
        if raw_path != ":memory:":
            path = Path(raw_path).expanduser()
            if not path.is_absolute():
                raise ValueError("direct hand ledger path must be absolute")
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.is_symlink():
                raise ValueError("direct hand ledger path must not be a symlink")
            raw_path = str(path)
        self.database_path = raw_path
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            raw_path,
            timeout=5.0,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.execute("PRAGMA busy_timeout=5000")
        if raw_path != ":memory:":
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS direct_hand_reservations (
                reservation_key TEXT PRIMARY KEY,
                procedure_run_id TEXT NOT NULL,
                episode_generation INTEGER NOT NULL,
                reservation_kind TEXT NOT NULL,
                command_id TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                stage TEXT NOT NULL,
                created_ns INTEGER NOT NULL,
                updated_ns INTEGER NOT NULL
            )
            """
        )
        # A prior process may have committed immediately before transport I/O.
        # Its outcome is unknowable after restart, so preserve and mark it;
        # never infer that it is safe to replay.
        now_ns = time.time_ns()
        self._connection.execute(
            """
            UPDATE direct_hand_reservations
               SET stage = 'interrupted_unknown', updated_ns = ?
             WHERE stage IN ('reserved', 'submitted')
            """,
            (now_ns,),
        )

    @staticmethod
    def _keys(
        *,
        procedure_run_id: str,
        episode_generation: int,
        action: str,
    ) -> tuple[tuple[str, str], ...]:
        episode = f"{procedure_run_id}:{int(episode_generation)}"
        command_key = f"command:{episode}:{str(action).strip()}"
        rows: list[tuple[str, str]] = [(command_key, "command")]
        if str(action).strip() in _HANDOVER_ACTIONS:
            rows.append((f"handover:{episode}", "handover"))
        return tuple(rows)

    def reserve(
        self,
        *,
        procedure_run_id: str,
        episode_generation: int,
        command_id: str,
        action: str,
        instrument_id: str,
        instrument_instance_id: str,
        source_location: str,
        target_location: str,
    ) -> DirectHandReservation:
        run_id = str(procedure_run_id or "").strip()
        generation = int(episode_generation)
        normalized_command_id = str(command_id or "").strip()
        normalized_action = str(action or "").strip()
        if (
            not valid_procedure_run_id(run_id)
            or generation <= 0
            or not normalized_command_id
            or not normalized_action
        ):
            return DirectHandReservation(
                False,
                "direct_hand_episode_invalid",
                "",
            )
        fingerprint = direct_hand_payload_sha256(
            action=normalized_action,
            instrument_id=instrument_id,
            instrument_instance_id=instrument_instance_id,
            source_location=source_location,
            target_location=target_location,
        )
        keys = self._keys(
            procedure_run_id=run_id,
            episode_generation=generation,
            action=normalized_action,
        )
        now_ns = time.time_ns()
        with self._lock:
            connection = self._connection
            try:
                connection.execute("BEGIN IMMEDIATE")
                for reservation_key, _kind in keys:
                    existing = connection.execute(
                        """
                        SELECT payload_sha256
                          FROM direct_hand_reservations
                         WHERE reservation_key = ?
                        """,
                        (reservation_key,),
                    ).fetchone()
                    if existing is None:
                        continue
                    connection.execute("ROLLBACK")
                    reason = (
                        "duplicate_command"
                        if str(existing[0]) == fingerprint
                        else "direct_episode_payload_conflict"
                    )
                    return DirectHandReservation(False, reason, fingerprint)
                connection.executemany(
                    """
                    INSERT INTO direct_hand_reservations (
                        reservation_key,
                        procedure_run_id,
                        episode_generation,
                        reservation_kind,
                        command_id,
                        payload_sha256,
                        stage,
                        created_ns,
                        updated_ns
                    ) VALUES (?, ?, ?, ?, ?, ?, 'reserved', ?, ?)
                    """,
                    [
                        (
                            reservation_key,
                            run_id,
                            generation,
                            kind,
                            normalized_command_id,
                            fingerprint,
                            now_ns,
                            now_ns,
                        )
                        for reservation_key, kind in keys
                    ],
                )
                connection.execute("COMMIT")
            except Exception:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        return DirectHandReservation(True, "reserved", fingerprint)

    def mark_stage(self, command_id: str, stage: str) -> None:
        normalized_command_id = str(command_id or "").strip()
        normalized_stage = str(stage or "").strip()
        if not normalized_command_id or not normalized_stage:
            return
        with self._lock:
            self._connection.execute(
                """
                UPDATE direct_hand_reservations
                   SET stage = ?, updated_ns = ?
                 WHERE command_id = ?
                """,
                (normalized_stage, time.time_ns(), normalized_command_id),
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()
