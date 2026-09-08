"""Append-only, local-only scenario decision journal for Debug Mode.

This recorder deliberately has no ROS or network dependency.  The Debug
observer owns it and provides already-curated message fields, so a clinical
recording owner can remain responsible for its own summaries and uploads.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import threading
from typing import Any


SCHEMA = "taskplanner.scenario_debug_recording.v1"
MAX_TEXT_CHARS = 4096
MAX_COLLECTION_ITEMS = 64
MAX_ENCODED_EVENT_BYTES = 64 * 1024


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _safe_file_component(value: str) -> str:
    compact = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip(".-")
    return compact[:80] or "procedure-run"


def _bounded(value: Any) -> Any:
    """Keep a malformed or verbose diagnostic field from growing the journal."""

    if isinstance(value, str):
        if len(value) <= MAX_TEXT_CHARS:
            return value
        return f"{value[:MAX_TEXT_CHARS]}… [{len(value) - MAX_TEXT_CHARS} chars omitted]"
    if isinstance(value, dict):
        return {
            str(key): _bounded(item)
            for key, item in list(value.items())[:MAX_COLLECTION_ITEMS]
        }
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        bounded_items = [_bounded(item) for item in items[:MAX_COLLECTION_ITEMS]]
        if len(items) > MAX_COLLECTION_ITEMS:
            bounded_items.append(f"[{len(items) - MAX_COLLECTION_ITEMS} items omitted]")
        return bounded_items
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return _bounded(str(value))


class ScenarioDebugRecorder:
    """Write one bounded JSONL journal per accepted procedure run.

    A journal is created only after the observer sees ``running=True`` with a
    procedure run ID.  It contains diagnostic evidence only: it never reads
    image payloads, uploads data, or issues ROS requests.
    """

    def __init__(self, output_dir: Path) -> None:
        self._output_dir = Path(output_dir)
        self._lock = threading.Lock()
        self._stream: Any | None = None
        self._path: Path | None = None
        self._procedure_run_id = ""
        self._sequence = 0

    @property
    def active_procedure_run_id(self) -> str:
        with self._lock:
            return self._procedure_run_id

    @property
    def path(self) -> Path | None:
        with self._lock:
            return self._path

    def start(
        self,
        *,
        procedure_run_id: str,
        context: dict[str, Any],
        source_stamp: dict[str, int] | None = None,
    ) -> Path | None:
        """Open a journal for ``procedure_run_id`` or return the active path."""

        normalized_run_id = str(procedure_run_id).strip()
        if not normalized_run_id:
            return None
        with self._lock:
            if self._stream is not None and self._procedure_run_id == normalized_run_id:
                return self._path
            if self._stream is not None:
                self._stop_locked("procedure_run_changed")

            self._output_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(self._output_dir, 0o700)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            stem = f"{stamp}--{_safe_file_component(normalized_run_id)}"
            path = self._allocate_path_locked(stem)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            self._stream = os.fdopen(fd, "w", encoding="utf-8")
            self._path = path
            self._procedure_run_id = normalized_run_id
            self._sequence = 0
            self._write_locked(
                "recording_started",
                {
                    "procedure_run_id": normalized_run_id,
                    "context": _bounded(context),
                },
                source_stamp=source_stamp,
            )
            return path

    def append(
        self,
        record_type: str,
        payload: dict[str, Any],
        *,
        source_stamp: dict[str, int] | None = None,
    ) -> bool:
        """Append a diagnostic record if its scenario run is currently active."""

        with self._lock:
            if self._stream is None:
                return False
            self._write_locked(record_type, _bounded(payload), source_stamp=source_stamp)
            return True

    def stop(self, reason: str) -> Path | None:
        """Finalize the active journal without affecting any other recorder."""

        with self._lock:
            if self._stream is None:
                return self._path
            return self._stop_locked(reason)

    def _allocate_path_locked(self, stem: str) -> Path:
        for index in range(10_000):
            suffix = "" if index == 0 else f"--{index:02d}"
            candidate = self._output_dir / f"{stem}{suffix}.jsonl"
            if not candidate.exists():
                return candidate
        raise RuntimeError("could not allocate a scenario debug journal filename")

    def _stop_locked(self, reason: str) -> Path | None:
        path = self._path
        self._write_locked(
            "recording_stopped",
            {
                "procedure_run_id": self._procedure_run_id,
                "reason": str(reason).strip() or "scenario_stopped",
            },
            source_stamp=None,
        )
        assert self._stream is not None
        self._stream.flush()
        self._stream.close()
        self._stream = None
        self._procedure_run_id = ""
        return path

    def _write_locked(
        self,
        record_type: str,
        payload: dict[str, Any],
        *,
        source_stamp: dict[str, int] | None,
    ) -> None:
        assert self._stream is not None
        self._sequence += 1
        row: dict[str, Any] = {
            "schema": SCHEMA,
            "record_type": str(record_type).strip() or "diagnostic",
            "recorded_at_utc": _utc_now(),
            "sequence": self._sequence,
            "procedure_run_id": self._procedure_run_id,
            "payload": payload,
        }
        if source_stamp is not None:
            row["source_stamp"] = {
                "sec": int(source_stamp.get("sec", 0)),
                "nanosec": int(source_stamp.get("nanosec", 0)),
            }
        encoded = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_ENCODED_EVENT_BYTES:
            row["payload"] = {
                "truncated": True,
                "reason": "encoded_event_exceeded_limit",
            }
            encoded = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
        self._stream.write(encoded + "\n")
        self._stream.flush()
