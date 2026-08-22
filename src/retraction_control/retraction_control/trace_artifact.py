"""Atomic, checksum-bound command traces for record-only shadow execution."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable, Mapping

from .adapters.fake import AdapterCall


TRACE_SCHEMA_VERSION = 1


class TraceArtifactError(RuntimeError):
    pass


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TraceArtifactError("trace contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    return str(value)


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            _json_safe(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _call_dict(call: AdapterCall) -> dict[str, object]:
    return {
        "sequence": int(call.sequence),
        "timestamp_ns": int(call.timestamp_ns),
        "command_id": call.command_id,
        "component": call.component,
        "method": call.method,
        "args": list(call.args),
        "kwargs": dict(call.kwargs),
    }


class ShadowTraceRepository:
    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        if not self.root.is_absolute():
            raise ValueError("shadow trace directory must be absolute")

    def path_for(self, command_id: str) -> Path:
        normalized = str(command_id).strip()
        if not normalized:
            raise TraceArtifactError("command_id must not be empty")
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        return self.root / f"command-{digest}.json"

    def save(
        self,
        *,
        command_id: str,
        command: int | None,
        profile_name: str,
        profile_version: str,
        profile_checksum: str,
        source_revision: str,
        target_planner: Mapping[str, object],
        terminal_stage: str,
        terminal_code: str,
        terminal_message: str,
        calls: Iterable[AdapterCall],
    ) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.root.is_dir() or self.root.is_symlink():
            raise TraceArtifactError("shadow trace root must be a real directory")
        final_path = self.path_for(command_id)
        records = tuple(calls)
        if any(call.command_id != str(command_id).strip() for call in records):
            raise TraceArtifactError("trace contains a different command context")
        body: dict[str, object] = {
            "schema_version": TRACE_SCHEMA_VERSION,
            "evidence_level": "record_only",
            "physical_motion_executed": False,
            "command_id": str(command_id).strip(),
            "command": None if command is None else int(command),
            "profile": {
                "name": str(profile_name),
                "version": str(profile_version),
                "checksum": str(profile_checksum),
            },
            "source_revision": str(source_revision),
            "target_planner": dict(target_planner),
            "terminal": {
                "stage": str(terminal_stage),
                "code": str(terminal_code),
                "message": str(terminal_message),
            },
            "calls": [_call_dict(call) for call in records],
        }
        body["artifact_sha256"] = _sha256(_canonical_json(body))
        payload = _canonical_json(body)
        if final_path.exists():
            if final_path.read_bytes() == payload:
                return final_path
            raise TraceArtifactError(
                "a different trace already exists for this command_id"
            )

        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{final_path.name}.", suffix=".tmp", dir=self.root
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(0o600)
            os.link(temporary, final_path)
            directory_fd = os.open(
                self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            return final_path
        except FileExistsError as exc:
            raise TraceArtifactError(
                "a trace was concurrently committed for this command_id"
            ) from exc
        except OSError as exc:
            raise TraceArtifactError(f"could not persist shadow trace: {exc}") from exc
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def load_verified(self, command_id: str) -> Mapping[str, object]:
        path = self.path_for(command_id)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TraceArtifactError(f"could not read shadow trace: {exc}") from exc
        if not isinstance(value, dict):
            raise TraceArtifactError("shadow trace must contain an object")
        checksum = value.get("artifact_sha256")
        body = dict(value)
        body.pop("artifact_sha256", None)
        if not isinstance(checksum, str) or checksum != _sha256(
            _canonical_json(body)
        ):
            raise TraceArtifactError("shadow trace checksum mismatch")
        if value.get("physical_motion_executed") is not False:
            raise TraceArtifactError("shadow trace claims physical motion")
        if value.get("evidence_level") != "record_only":
            raise TraceArtifactError("shadow trace evidence level is invalid")
        return value


__all__ = ["ShadowTraceRepository", "TRACE_SCHEMA_VERSION", "TraceArtifactError"]
