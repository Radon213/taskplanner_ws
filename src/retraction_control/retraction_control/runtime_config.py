"""Strict, local-only runtime/storage configuration loader."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping

import yaml


RUNTIME_CONFIG_SCHEMA = "retraction_control.runtime.v1"
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class RuntimeConfigError(ValueError):
    pass


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _mapping(value: object, path: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        raise RuntimeConfigError(f"{path} must be a string-keyed mapping")
    return dict(value)


def _keys(
    value: Mapping[str, object],
    allowed: set[str],
    required: set[str],
    path: str,
) -> None:
    unknown = sorted(set(value) - allowed)
    missing = sorted(required - set(value))
    if unknown:
        raise RuntimeConfigError(f"{path} has unknown keys: {', '.join(unknown)}")
    if missing:
        raise RuntimeConfigError(f"{path} is missing keys: {', '.join(missing)}")


def _positive_float(value: object, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeConfigError(f"{path} must be a number")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0.0:
        raise RuntimeConfigError(f"{path} must be finite and positive")
    return normalized


def _safe_name(value: object, path: str) -> str:
    if not isinstance(value, str) or not _SAFE_NAME_RE.fullmatch(value):
        raise RuntimeConfigError(f"{path} must be one safe relative path component")
    if value in {".", ".."}:
        raise RuntimeConfigError(f"{path} must not traverse directories")
    return value


def validate_data_directory(value: object) -> Path:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise RuntimeConfigError("storage.data_directory must be a trimmed path")
    path = Path(value)
    if not path.is_absolute() or path == Path(path.anchor):
        raise RuntimeConfigError(
            "storage.data_directory must be absolute and not a filesystem root"
        )
    if any(candidate.is_symlink() for candidate in (path, *path.parents)):
        raise RuntimeConfigError(
            "storage.data_directory must not traverse a symlink"
        )
    return path


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    schema: str
    data_directory: Path
    ledger_filename: str
    session_directory_name: str
    shadow_trace_directory_name: str
    atomic_fsync: bool
    status_period_sec: float
    diagnostics_period_sec: float
    checksum: str


def load_runtime_config(source: str | Path | Mapping[str, object]) -> RuntimeSettings:
    if isinstance(source, Mapping):
        payload = dict(source)
    else:
        path = Path(source)
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise RuntimeConfigError(f"could not load runtime config: {exc}") from exc
    root = _mapping(payload, "runtime config")
    root_fields = {"schema", "storage", "publish"}
    _keys(root, root_fields, root_fields, "runtime config")
    if root["schema"] != RUNTIME_CONFIG_SCHEMA:
        raise RuntimeConfigError(f"schema must be {RUNTIME_CONFIG_SCHEMA}")

    storage = _mapping(root["storage"], "storage")
    storage_fields = {
        "data_directory",
        "ledger_filename",
        "session_directory_name",
        "shadow_trace_directory_name",
        "atomic_fsync",
    }
    _keys(storage, storage_fields, storage_fields, "storage")
    if storage["atomic_fsync"] is not True:
        raise RuntimeConfigError("storage.atomic_fsync must remain true")

    publish = _mapping(root["publish"], "publish")
    publish_fields = {"status_period_sec", "diagnostics_period_sec"}
    _keys(publish, publish_fields, publish_fields, "publish")

    return RuntimeSettings(
        schema=RUNTIME_CONFIG_SCHEMA,
        data_directory=validate_data_directory(storage["data_directory"]),
        ledger_filename=_safe_name(storage["ledger_filename"], "storage.ledger_filename"),
        session_directory_name=_safe_name(
            storage["session_directory_name"], "storage.session_directory_name"
        ),
        shadow_trace_directory_name=_safe_name(
            storage["shadow_trace_directory_name"],
            "storage.shadow_trace_directory_name",
        ),
        atomic_fsync=True,
        status_period_sec=_positive_float(
            publish["status_period_sec"], "publish.status_period_sec"
        ),
        diagnostics_period_sec=_positive_float(
            publish["diagnostics_period_sec"], "publish.diagnostics_period_sec"
        ),
        checksum="sha256:" + hashlib.sha256(_canonical(root)).hexdigest(),
    )


__all__ = [
    "RUNTIME_CONFIG_SCHEMA",
    "RuntimeConfigError",
    "RuntimeSettings",
    "load_runtime_config",
    "validate_data_directory",
]
