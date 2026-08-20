#!/usr/bin/env python3
"""Read-only, allowlisted capture and comparison of retraction upstream sources.

The tool deliberately has no network or robot integrations.  It only reads the
file and directory roots named by the caller, writes a new snapshot directory,
and optionally gathers local process metadata using non-networking Python APIs
and ``python -m pip freeze``.  Notebook exports are produced by parsing JSON and
writing code cells; cells are never executed.
"""

from __future__ import annotations

import argparse
import ast
from collections import Counter
import configparser
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from typing import Any, Iterable, Mapping, Sequence
import xml.etree.ElementTree as ET


SCHEMA_VERSION = "1.0"
TOOL_VERSION = "1.0"
ACCEPTANCE_SCHEMA_VERSION = "1.0"
REDACTION_MARKER = "<REDACTED>"


class SnapshotError(RuntimeError):
    """Raised when a capture, comparison, or verification is unsafe/invalid."""


@dataclass(frozen=True)
class SourceSpec:
    """One explicitly allowlisted source root.

    ``kind`` is either ``file`` or ``directory``.  A label is a stable logical
    root used for comparison when source host paths differ.
    """

    label: str
    path: Path
    kind: str


_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_WINDOWS_DRIVE_ROOT_RE = re.compile(r"^[A-Za-z]:(?:[\\/]*)$")
_SENSITIVE_KEY_SOURCE = (
    r"(?:license(?:[_-]?key)?|licen[cs]e[_-]?token|password|passwd|pwd|"
    r"secret|token|(?:api|access|refresh|auth)[_-]?token|api[_-]?key|"
    r"access[_-]?key|private[_-]?key|"
    r"client[_-]?secret|auth(?:orization)?|credential)"
)
_SENSITIVE_KEY_RE = re.compile(rf"(?i)^{_SENSITIVE_KEY_SOURCE}$")
_QUOTED_SECRET_RE = re.compile(
    rf"(?i)(?P<prefix>[\"']?{_SENSITIVE_KEY_SOURCE}[\"']?\s*[:=]\s*)"
    r"(?P<quote>[\"'])(?P<value>.*?)(?P=quote)"
)
_UNQUOTED_SECRET_RE = re.compile(
    rf"(?i)(?P<prefix>[\"']?{_SENSITIVE_KEY_SOURCE}[\"']?\s*[:=]\s*)"
    r"(?P<value>(?![\"'])[^\s,;#}]+)"
)
_BEARER_RE = re.compile(r"(?i)(?P<prefix>\bBearer\s+)(?P<value>[A-Za-z0-9._~+/=-]{8,})")
_URL_CREDENTIAL_RE = re.compile(
    r"(?P<prefix>\b[a-z][a-z0-9+.-]*://[^:/\s]+:)(?P<value>[^@\s/]+)(?P<suffix>@)",
    re.IGNORECASE,
)
_KNOWN_TOKEN_RE = re.compile(
    r"(?P<value>(?:sk-[A-Za-z0-9_-]{10,}|gh[pousr]_[A-Za-z0-9]{20,}|"
    r"AKIA[0-9A-Z]{16}))"
)
_SAFE_DYNAMIC_VALUE_RE = re.compile(
    r"(?i)^(?:none|null|true|false|os\.(?:getenv|environ)|getenv\(|env\(|"
    r"\$\{|\$[A-Za-z_]|<REDACTED>)"
)
_TEXT_SUFFIXES = {
    "",
    ".c",
    ".cc",
    ".cfg",
    ".cmake",
    ".cpp",
    ".cxx",
    ".env",
    ".h",
    ".hh",
    ".hpp",
    ".ini",
    ".ipynb",
    ".json",
    ".md",
    ".py",
    ".sh",
    ".toml",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
_CONFIG_SUFFIXES = {".cfg", ".env", ".ini", ".json", ".toml", ".xml", ".yaml", ".yml"}
_CPP_SUFFIXES = {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp"}

CLASSIFICATION_ORDER = (
    "control_algorithm_change",
    "waypoint_gain_sensor_calibration_change",
    "ros_connection_change",
    "environment_dependency_change",
    "notebook_experiment_visualization_change",
    "notebook_output_cell_change",
    "sensitive_value_redacted_change",
    "non_executable_text_change",
    "other_source_change",
)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _redaction_replacement(match: re.Match[str]) -> str:
    value = match.group("value")
    if value == REDACTION_MARKER or _SAFE_DYNAMIC_VALUE_RE.match(value):
        return match.group(0)
    result = match.groupdict().get("prefix", "")
    quote = match.groupdict().get("quote", "")
    result += quote + REDACTION_MARKER + quote
    result += match.groupdict().get("suffix", "")
    return result


def redact_text(text: str) -> tuple[str, int]:
    """Mask explicit secret assignments, URL credentials, and known token forms."""

    count = 0
    current = text
    # Credential-specific forms run first.  Otherwise a generic
    # ``Authorization: Bearer ...`` match would mask only the word ``Bearer``
    # and strand the credential that follows it.
    for pattern in (_BEARER_RE, _URL_CREDENTIAL_RE, _KNOWN_TOKEN_RE, _QUOTED_SECRET_RE, _UNQUOTED_SECRET_RE):
        current, replacements = pattern.subn(_redaction_replacement, current)
        count += replacements
    return current, count


def _redact_value(value: Any, *, key: str | None = None) -> tuple[Any, int]:
    if key is not None and _SENSITIVE_KEY_RE.fullmatch(key):
        if value == REDACTION_MARKER:
            return value, 0
        return REDACTION_MARKER, 1
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        result: list[Any] = []
        count = 0
        for item in value:
            redacted, item_count = _redact_value(item)
            result.append(redacted)
            count += item_count
        return result, count
    if isinstance(value, dict):
        result_dict: dict[str, Any] = {}
        count = 0
        for raw_key, item in value.items():
            string_key = str(raw_key)
            redacted, item_count = _redact_value(item, key=string_key)
            result_dict[string_key] = redacted
            count += item_count
        return result_dict, count
    return value, 0


def _decode_text(raw: bytes, source: Path) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SnapshotError(
            f"binary/non-UTF-8 source is refused so secrets cannot bypass scanning: {source}"
        ) from exc


def _redact_source_bytes(raw: bytes, source: Path) -> tuple[bytes, int]:
    if source.suffix.lower() not in _TEXT_SUFFIXES and source.name != "CMakeLists.txt":
        raise SnapshotError(f"unsupported non-text source is refused: {source}")
    text = _decode_text(raw, source)
    if source.suffix.lower() == ".ipynb":
        try:
            notebook = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SnapshotError(f"invalid notebook JSON: {source}: {exc}") from exc
        redacted_notebook, count = _redact_value(notebook)
        if count == 0:
            return raw, 0
        rendered = json.dumps(redacted_notebook, ensure_ascii=False, indent=1) + "\n"
        return rendered.encode("utf-8"), count
    redacted, count = redact_text(text)
    if count == 0:
        return raw, 0
    return redacted.encode("utf-8"), count


def _cell_source(cell: Mapping[str, Any]) -> str:
    source = cell.get("source", "")
    if isinstance(source, list):
        return "".join(str(item) for item in source)
    return str(source)


def _pythonize_notebook_cell(source: str) -> str:
    lines = source.splitlines()
    first_code = next((line.lstrip() for line in lines if line.strip()), "")
    if first_code.startswith("%%"):
        return "\n".join(f"# IPython cell not executable as Python: {line}" for line in lines)
    rendered: list[str] = []
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith(("%", "!", "?")):
            indent = line[: len(line) - len(stripped)]
            rendered.append(f"{indent}# IPython line not executed: {stripped}")
        else:
            rendered.append(line)
    return "\n".join(rendered)


def export_notebook(notebook_bytes: bytes, *, source_name: str = "notebook.ipynb") -> bytes:
    """Return a non-executing Python text export of notebook code cells."""

    try:
        notebook = json.loads(_decode_text(notebook_bytes, Path(source_name)))
    except json.JSONDecodeError as exc:
        raise SnapshotError(f"invalid notebook JSON: {source_name}: {exc}") from exc
    cells = notebook.get("cells", [])
    if not isinstance(cells, list):
        raise SnapshotError(f"notebook cells must be a list: {source_name}")
    parts = [
        "# Generated by retraction_upstream_snapshot.py.",
        "# Notebook cells were parsed and copied; no cell was executed.",
        "",
    ]
    for index, cell in enumerate(cells):
        if not isinstance(cell, dict) or cell.get("cell_type") != "code":
            continue
        parts.append(f"# %% [source cell {index}]")
        parts.append(_pythonize_notebook_cell(_cell_source(cell)))
        parts.append("")
    return ("\n".join(parts).rstrip() + "\n").encode("utf-8")


def _is_drive_or_filesystem_root(path: Path) -> bool:
    raw = str(path).strip()
    if _WINDOWS_DRIVE_ROOT_RE.fullmatch(raw):
        return True
    resolved = path.resolve(strict=False)
    return resolved == Path(resolved.anchor)


def _validate_spec(spec: SourceSpec) -> SourceSpec:
    if not _LABEL_RE.fullmatch(spec.label):
        raise SnapshotError(f"invalid source label {spec.label!r}")
    if spec.kind not in {"file", "directory"}:
        raise SnapshotError(f"invalid source kind {spec.kind!r}")
    path = Path(spec.path).expanduser()
    if spec.kind == "directory" and _is_drive_or_filesystem_root(path):
        raise SnapshotError(f"filesystem/drive roots may never be captured: {path}")
    if path.is_symlink():
        raise SnapshotError(f"symlink source roots are refused: {path}")
    if spec.kind == "file" and not path.is_file():
        raise SnapshotError(f"allowlisted file does not exist: {path}")
    if spec.kind == "directory" and not path.is_dir():
        raise SnapshotError(f"allowlisted directory does not exist: {path}")
    return SourceSpec(spec.label, path.resolve(), spec.kind)


def _iter_spec_files(spec: SourceSpec) -> Iterable[tuple[Path, Path]]:
    if spec.kind == "file":
        yield spec.path, Path(spec.path.name)
        return
    for root_text, directory_names, file_names in os.walk(spec.path, topdown=True, followlinks=False):
        root = Path(root_text)
        for name in list(directory_names):
            candidate = root / name
            if candidate.is_symlink():
                raise SnapshotError(f"symlink within allowlisted directory is refused: {candidate}")
        directory_names.sort()
        file_names.sort()
        for name in file_names:
            candidate = root / name
            if candidate.is_symlink():
                raise SnapshotError(f"symlink within allowlisted directory is refused: {candidate}")
            if not candidate.is_file():
                raise SnapshotError(f"non-regular source is refused: {candidate}")
            yield candidate, candidate.relative_to(spec.path)


def _safe_destination(root: Path, relative_path: str) -> Path:
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise SnapshotError(f"unsafe snapshot-relative path: {relative_path}")
    destination = (root / relative).resolve(strict=False)
    try:
        destination.relative_to(root.resolve())
    except ValueError as exc:
        raise SnapshotError(f"snapshot path escapes destination: {relative_path}") from exc
    return destination


def collect_local_environment(*, include_pip_freeze: bool = False) -> dict[str, Any]:
    """Collect metadata without importing an SDK or making network/device calls."""

    metadata: dict[str, Any] = {
        "scope": "capture_host; not upstream unless capture ran in the upstream environment",
        "python_version": sys.version,
        "python_implementation": sys.implementation.name,
        "ros_distro": os.environ.get("ROS_DISTRO", ""),
        "ros_version": os.environ.get("ROS_VERSION", ""),
        "rmw_implementation": os.environ.get("RMW_IMPLEMENTATION", ""),
    }
    try:
        metadata["neuromeka_sdk_version"] = importlib.metadata.version("neuromeka")
    except importlib.metadata.PackageNotFoundError:
        metadata["neuromeka_sdk_version"] = "not-installed"
    if include_pip_freeze:
        command = [sys.executable, "-m", "pip", "freeze", "--disable-pip-version-check"]
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            metadata["pip_freeze"] = {
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
        else:
            metadata["pip_freeze"] = {
                "status": "captured" if result.returncode == 0 else "failed",
                "command": ["python", "-m", "pip", "freeze", "--disable-pip-version-check"],
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
    redacted, _ = _redact_value(metadata)
    return redacted


def _environment_completeness(environment: Mapping[str, Any], file_labels: Iterable[str]) -> dict[str, str]:
    lowered: set[str] = set()
    for key, value in environment.items():
        if value in (None, "", [], {}):
            continue
        if isinstance(value, dict) and value.get("status") not in (None, "captured"):
            continue
        lowered.add(str(key).lower())
    labels = {label.lower() for label in file_labels}
    aliases = {
        "python_version": {"python", "python_version"},
        "ros_distro": {"ros", "ros_distro"},
        "neuromeka_sdk_version": {"sdk", "sdk_version", "neuromeka", "neuromeka_sdk_version"},
        "pip_freeze": {"pip", "pip_freeze", "requirements"},
    }
    return {
        key: "recorded" if (names & lowered or names & labels) else "missing"
        for key, names in aliases.items()
    }


def _identity_payload(manifest: Mapping[str, Any]) -> dict[str, Any]:
    file_identity = [
        {
            "logical_path": item["logical_path"],
            "size_bytes": item["size_bytes"],
            "mtime_ns": item["mtime_ns"],
            "sha256": item["sha256"],
        }
        for item in manifest["files"]
    ]
    environment_files = [
        {
            "label": item["label"],
            "size_bytes": item["size_bytes"],
            "sha256": item["sha256"],
        }
        for item in manifest["environment"]["files"]
    ]
    return {
        "schema_version": manifest["schema_version"],
        "files": file_identity,
        "environment": {
            "supplied": manifest["environment"]["supplied"],
            "local_capture": manifest["environment"]["local_capture"],
            "files": environment_files,
        },
    }


def capture_snapshot(
    specs: Sequence[SourceSpec],
    output_dir: Path,
    *,
    supplied_environment: Mapping[str, Any] | None = None,
    environment_files: Mapping[str, Path] | None = None,
    collect_local: bool = False,
    include_pip_freeze: bool = False,
    created_at_utc: str | None = None,
) -> dict[str, Any]:
    """Capture explicit roots into a new snapshot directory.

    The output must not exist and must not be inside an allowlisted directory.
    Source files are never opened for writing.
    """

    if not specs:
        raise SnapshotError("at least one explicit --file or --directory is required")
    validated = [_validate_spec(spec) for spec in specs]
    labels = [spec.label for spec in validated]
    if len(labels) != len(set(labels)):
        raise SnapshotError("source labels must be unique")

    output_dir = Path(output_dir).expanduser().resolve(strict=False)
    if output_dir.exists():
        raise SnapshotError(f"snapshot destination already exists: {output_dir}")
    for spec in validated:
        if output_dir == spec.path:
            raise SnapshotError("snapshot destination may not equal a source")
        if spec.kind == "directory":
            try:
                output_dir.relative_to(spec.path)
            except ValueError:
                pass
            else:
                raise SnapshotError("snapshot destination may not be inside an allowlisted directory")

    supplied_raw = dict(supplied_environment or {})
    supplied_redacted, supplied_redaction_count = _redact_value(supplied_raw)
    local_metadata = (
        collect_local_environment(include_pip_freeze=include_pip_freeze)
        if collect_local or include_pip_freeze
        else {}
    )
    env_file_map = dict(environment_files or {})
    for label in env_file_map:
        if not _LABEL_RE.fullmatch(label):
            raise SnapshotError(f"invalid environment file label {label!r}")

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.tmp-", dir=output_dir.parent))
    try:
        manifest_files: list[dict[str, Any]] = []
        derived_files: list[dict[str, Any]] = []
        seen_logical: set[str] = set()
        for spec in sorted(validated, key=lambda item: item.label):
            for source, relative in _iter_spec_files(spec):
                logical_path = (Path(spec.label) / relative).as_posix()
                if logical_path in seen_logical:
                    raise SnapshotError(f"duplicate logical source path: {logical_path}")
                seen_logical.add(logical_path)
                stat_before = source.stat()
                raw = source.read_bytes()
                stat = source.stat()
                if (
                    stat_before.st_size != stat.st_size
                    or stat_before.st_mtime_ns != stat.st_mtime_ns
                    or len(raw) != stat.st_size
                ):
                    raise SnapshotError(f"source changed while being captured: {source}")
                stored, redaction_count = _redact_source_bytes(raw, source)
                snapshot_path = (Path("sources") / logical_path).as_posix()
                destination = _safe_destination(temporary, snapshot_path)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(stored)
                entry = {
                    "source_label": spec.label,
                    "source_kind": spec.kind,
                    "source_path": redact_text(str(source))[0],
                    "relative_path": relative.as_posix(),
                    "logical_path": logical_path,
                    "snapshot_path": snapshot_path,
                    "size_bytes": len(raw),
                    "mtime_ns": stat.st_mtime_ns,
                    "mtime_utc": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                    "sha256": _sha256_bytes(raw),
                    "stored_size_bytes": len(stored),
                    "stored_sha256": _sha256_bytes(stored),
                    "content_transform": "secret_redacted" if redaction_count else "byte_exact_copy",
                    "redaction_count": redaction_count,
                }
                manifest_files.append(entry)
                if source.suffix.lower() == ".ipynb":
                    export = export_notebook(stored, source_name=logical_path)
                    export_path = (Path("notebook_exports") / Path(logical_path).with_suffix(".py")).as_posix()
                    export_destination = _safe_destination(temporary, export_path)
                    export_destination.parent.mkdir(parents=True, exist_ok=True)
                    export_destination.write_bytes(export)
                    derived_files.append(
                        {
                            "kind": "notebook_python_export",
                            "source_logical_path": logical_path,
                            "snapshot_path": export_path,
                            "size_bytes": len(export),
                            "sha256": _sha256_bytes(export),
                            "execution": "not_executed",
                        }
                    )

        environment_entries: list[dict[str, Any]] = []
        for label, raw_path in sorted(env_file_map.items()):
            source = Path(raw_path).expanduser()
            if source.is_symlink() or not source.is_file():
                raise SnapshotError(f"environment metadata file is not a regular file: {source}")
            stat_before = source.stat()
            raw = source.read_bytes()
            stat_after = source.stat()
            if (
                stat_before.st_size != stat_after.st_size
                or stat_before.st_mtime_ns != stat_after.st_mtime_ns
                or len(raw) != stat_after.st_size
            ):
                raise SnapshotError(f"environment metadata changed while being captured: {source}")
            stored, redaction_count = _redact_source_bytes(raw, source)
            suffix = source.suffix if source.suffix.lower() in _TEXT_SUFFIXES else ".txt"
            relative_path = (Path("environment") / f"{label}{suffix}").as_posix()
            destination = _safe_destination(temporary, relative_path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(stored)
            environment_entries.append(
                {
                    "label": label,
                    "source_path": redact_text(str(source.resolve()))[0],
                    "snapshot_path": relative_path,
                    "size_bytes": len(raw),
                    "sha256": _sha256_bytes(raw),
                    "stored_size_bytes": len(stored),
                    "stored_sha256": _sha256_bytes(stored),
                    "redaction_count": redaction_count,
                }
            )

        manifest: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "tool": {"name": "retraction_upstream_snapshot", "version": TOOL_VERSION},
            "created_at_utc": created_at_utc or _utc_now(),
            "capture_policy": {
                "inputs": "explicit_allowlist_only",
                "filesystem_roots": "refused",
                "symlinks": "refused",
                "network_or_hardware_access": "none",
                "binary_sources": "refused",
                "notebook_execution": "never",
            },
            "allowlist": [
                {
                    "label": spec.label,
                    "kind": spec.kind,
                    "source_path": redact_text(str(spec.path))[0],
                }
                for spec in sorted(validated, key=lambda item: item.label)
            ],
            "files": sorted(manifest_files, key=lambda item: item["logical_path"]),
            "derived_files": sorted(derived_files, key=lambda item: item["snapshot_path"]),
            "environment": {
                "supplied": supplied_redacted,
                "supplied_redaction_count": supplied_redaction_count,
                "local_capture": local_metadata,
                "files": environment_entries,
                "required_metadata_status": _environment_completeness(
                    {**supplied_redacted, **local_metadata}, env_file_map
                ),
                "provenance_note": (
                    "supplied values/files describe upstream only if provided by that environment; "
                    "local_capture describes the machine running this tool"
                ),
            },
            "acceptance": {
                "state": "unreviewed_candidate",
                "tag": None,
                "partner_approval": "not_recorded",
                "note": "A manifest or local tag is not evidence of partner approval.",
            },
        }
        manifest["snapshot_id"] = _sha256_bytes(_canonical_json(_identity_payload(manifest)).encode("utf-8"))
        manifest_path = temporary / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        verification = verify_snapshot(temporary)
        if not verification["ok"]:
            raise SnapshotError("new snapshot failed verification: " + "; ".join(verification["errors"]))
        temporary.rename(output_dir)
        return manifest
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _load_manifest(snapshot_dir: Path) -> dict[str, Any]:
    path = Path(snapshot_dir) / "manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SnapshotError(f"cannot read snapshot manifest {path}: {exc}") from exc
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise SnapshotError(f"unsupported snapshot schema: {manifest.get('schema_version')!r}")
    return manifest


def _secret_findings(text: str) -> list[str]:
    findings: list[str] = []
    for label, pattern in (
        ("quoted-sensitive-value", _QUOTED_SECRET_RE),
        ("unquoted-sensitive-value", _UNQUOTED_SECRET_RE),
        ("bearer-token", _BEARER_RE),
        ("url-credential", _URL_CREDENTIAL_RE),
        ("known-token-form", _KNOWN_TOKEN_RE),
    ):
        for match in pattern.finditer(text):
            value = match.group("value")
            if value == REDACTION_MARKER or _SAFE_DYNAMIC_VALUE_RE.match(value):
                continue
            findings.append(label)
    return findings


def verify_snapshot(snapshot_dir: Path) -> dict[str, Any]:
    """Verify stored hashes, capture identity, path containment, and redaction."""

    root = Path(snapshot_dir).resolve()
    errors: list[str] = []
    try:
        manifest = _load_manifest(root)
    except SnapshotError as exc:
        return {"ok": False, "errors": [str(exc)]}
    records = list(manifest.get("files", [])) + list(manifest.get("derived_files", []))
    records += list(manifest.get("environment", {}).get("files", []))
    for record in records:
        relative = record.get("snapshot_path", "")
        try:
            path = _safe_destination(root, relative)
        except SnapshotError as exc:
            errors.append(str(exc))
            continue
        if not path.is_file():
            errors.append(f"missing stored file: {relative}")
            continue
        expected_size = record.get("stored_size_bytes", record.get("size_bytes"))
        expected_sha = record.get("stored_sha256", record.get("sha256"))
        if path.stat().st_size != expected_size:
            errors.append(f"stored size mismatch: {relative}")
        if _sha256_file(path) != expected_sha:
            errors.append(f"stored sha256 mismatch: {relative}")
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            errors.append(f"stored file is not secret-scannable UTF-8: {relative}")
        else:
            findings = _secret_findings(text)
            if findings:
                errors.append(f"unredacted secret pattern in {relative}: {sorted(set(findings))}")
    for metadata_name in ("manifest.json", "acceptance.json"):
        metadata_path = root / metadata_name
        if not metadata_path.is_file():
            continue
        try:
            findings = _secret_findings(metadata_path.read_text(encoding="utf-8"))
        except UnicodeDecodeError:
            errors.append(f"snapshot metadata is not UTF-8: {metadata_name}")
        else:
            if findings:
                errors.append(
                    f"unredacted secret pattern in {metadata_name}: {sorted(set(findings))}"
                )
    try:
        expected_id = _sha256_bytes(_canonical_json(_identity_payload(manifest)).encode("utf-8"))
    except (KeyError, TypeError, ValueError) as exc:
        errors.append(f"manifest identity fields are invalid: {exc}")
    else:
        if manifest.get("snapshot_id") != expected_id:
            errors.append("snapshot_id does not match manifest identity fields")
    return {"ok": not errors, "snapshot_id": manifest.get("snapshot_id"), "errors": errors}


def _fingerprint(value: Any) -> str:
    if isinstance(value, str):
        payload = value
    else:
        payload = _canonical_json(value)
    return _sha256_bytes(payload.encode("utf-8"))


def _flatten_config(value: Any, prefix: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {}
    if isinstance(value, dict):
        for key in sorted(value, key=str):
            child = f"{prefix}.{key}" if prefix else str(key)
            result.update(_flatten_config(value[key], child))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            result.update(_flatten_config(item, f"{prefix}[{index}]"))
    else:
        result[prefix or "<root>"] = value
    return result


def _simple_yaml(text: str) -> dict[str, Any]:
    """Small mapping-only fallback used when PyYAML is unavailable."""

    result: dict[str, Any] = {}
    stack: list[tuple[int, str]] = []
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        match = re.match(r"^(?P<indent>\s*)(?P<key>[^:#][^:]*):(?:\s*(?P<value>.*))?$", raw_line)
        if not match:
            continue
        indent = len(match.group("indent").replace("\t", "    "))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        key = match.group("key").strip().strip("\"'")
        prefix = ".".join(item[1] for item in stack)
        path = f"{prefix}.{key}" if prefix else key
        value = (match.group("value") or "").strip()
        if not value:
            stack.append((indent, key))
            continue
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = value.strip("\"'")
        result[path] = parsed
    return result


def _parse_config(path: str, text: str) -> dict[str, Any] | None:
    suffix = Path(path).suffix.lower()
    try:
        if suffix == ".json":
            return _flatten_config(json.loads(text))
        if suffix == ".toml":
            return _flatten_config(tomllib.loads(text))
        if suffix in {".ini", ".cfg"}:
            parser = configparser.ConfigParser()
            parser.read_string(text)
            return {
                f"{section}.{key}": value
                for section in parser.sections()
                for key, value in sorted(parser.items(section))
            }
        if suffix == ".env":
            values: dict[str, Any] = {}
            for line in text.splitlines():
                if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
            return values
        if suffix in {".yaml", ".yml"}:
            try:
                import yaml  # type: ignore[import-not-found]
            except ImportError:
                return _simple_yaml(text)
            return _flatten_config(yaml.safe_load(text))
        if suffix == ".xml":
            root = ET.fromstring(text)
            values: dict[str, Any] = {}

            def visit(element: ET.Element, prefix: str) -> None:
                indexed = prefix or element.tag
                for key, value in sorted(element.attrib.items()):
                    values[f"{indexed}.@{key}"] = value
                if element.text and element.text.strip():
                    values[indexed] = element.text.strip()
                counters: Counter[str] = Counter()
                for child in element:
                    counters[child.tag] += 1
                    visit(child, f"{indexed}.{child.tag}[{counters[child.tag] - 1}]")

            visit(root, root.tag)
            return values
    except (ValueError, configparser.Error, ET.ParseError):
        return None
    return None


_CONFIG_NAME_RE = re.compile(
    r"(?i)(config|profile|waypoint|gain|calibr|sensor|force|threshold|robot[_-]?ip|"
    r"can[_-]?(?:channel|bitrate)|mapping|joint[_-]?slice|topic|service|ros)"
)


class _FunctionVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.stack: list[str] = []
        self.counts: Counter[str] = Counter()
        self.records: dict[tuple[str, str], dict[str, str]] = {}

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        base = ".".join([*self.stack, node.name])
        self.counts[base] += 1
        symbol = base if self.counts[base] == 1 else f"{base}#{self.counts[base]}"
        canonical = ast.dump(node, include_attributes=False)
        try:
            keywords = ast.unparse(node)
        except Exception:
            keywords = symbol
        self.records[("function", symbol)] = {
            "fingerprint": _fingerprint(canonical),
            "keywords": keywords,
        }
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)


def _assignment_target(node: ast.Assign | ast.AnnAssign) -> str | None:
    if isinstance(node, ast.Assign):
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            return None
        return node.targets[0].id
    return node.target.id if isinstance(node.target, ast.Name) else None


def _python_semantics(text: str) -> dict[tuple[str, str], dict[str, str]]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return {}
    visitor = _FunctionVisitor()
    visitor.visit(tree)
    records = dict(visitor.records)
    handled_nodes: set[int] = set()
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        name = _assignment_target(node)
        if not name:
            continue
        value_node = node.value
        try:
            value = ast.literal_eval(value_node)
        except (ValueError, TypeError):
            continue
        if not name.isupper() and (_CONFIG_NAME_RE.search(name) or isinstance(value, (dict, list))):
            flattened = _flatten_config(value, name)
            for key, item in flattened.items():
                records[("config", key)] = {
                    "fingerprint": _fingerprint(item),
                    "keywords": f"{key} {_canonical_json(item)}",
                }
            handled_nodes.add(id(node))
        elif name.isupper():
            records[("constant", name)] = {
                "fingerprint": _fingerprint(value),
                "keywords": f"{name} {_canonical_json(value)}",
            }
            handled_nodes.add(id(node))
    top_level = [
        node
        for node in tree.body
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and id(node) not in handled_nodes
    ]
    if top_level:
        canonical = "\n".join(ast.dump(node, include_attributes=False) for node in top_level)
        try:
            keywords = "\n".join(ast.unparse(node) for node in top_level)
        except Exception:
            keywords = "<module>"
        records[("module_code", "<module>")] = {
            "fingerprint": _fingerprint(canonical),
            "keywords": keywords,
        }
    return records


_CPP_FUNCTION_RE = re.compile(
    r"(?m)^[ \t]*(?:template\s*<[^;{}]+>\s*)?"
    r"(?:[A-Za-z_~][\w:<>,*&~]*[ \t]+)+"
    r"(?P<name>[A-Za-z_~][\w:~]*)[ \t]*\([^;{}]*\)"
    r"[ \t]*(?:const[ \t]*)?(?:noexcept[ \t]*)?(?:->[^{]+)?\{"
)
_CPP_CONSTANT_RE = re.compile(
    r"(?m)^[ \t]*(?:inline[ \t]+)?(?:static[ \t]+)?(?:constexpr|const)[^;=]+"
    r"\b(?P<name>[A-Z][A-Z0-9_]*)[ \t]*=[ \t]*(?P<value>[^;]+);"
)


def _brace_block(text: str, opening: int) -> str:
    depth = 0
    for index in range(opening, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[opening : index + 1]
    return text[opening:]


def _cpp_semantics(text: str) -> dict[tuple[str, str], dict[str, str]]:
    records: dict[tuple[str, str], dict[str, str]] = {}
    counts: Counter[str] = Counter()
    for match in _CPP_FUNCTION_RE.finditer(text):
        name = match.group("name")
        counts[name] += 1
        symbol = name if counts[name] == 1 else f"{name}#{counts[name]}"
        opening = text.find("{", match.start(), match.end())
        block = _brace_block(text, opening)
        normalized = re.sub(r"\s+", " ", block).strip()
        records[("function", symbol)] = {
            "fingerprint": _fingerprint(normalized),
            "keywords": f"{symbol} {normalized}",
        }
    for match in _CPP_CONSTANT_RE.finditer(text):
        name = match.group("name")
        value = re.sub(r"\s+", " ", match.group("value")).strip()
        granularity = "config" if _CONFIG_NAME_RE.search(name) else "constant"
        records[(granularity, name)] = {
            "fingerprint": _fingerprint(value),
            "keywords": f"{name} {value}",
        }
    return records


def _notebook_parts(text: str) -> dict[str, str]:
    notebook = json.loads(text)
    code: list[str] = []
    markdown: list[str] = []
    outputs: list[Any] = []
    structure: list[Any] = []
    metadata: list[Any] = []
    for cell in notebook.get("cells", []):
        if not isinstance(cell, dict):
            continue
        kind = cell.get("cell_type", "")
        source = _cell_source(cell)
        structure.append(
            {
                key: value
                for key, value in cell.items()
                if key not in {"outputs", "execution_count"}
            }
        )
        metadata.append({"cell_type": kind, "metadata": cell.get("metadata", {})})
        if kind == "code":
            code.append(_pythonize_notebook_cell(source))
            outputs.append({"outputs": cell.get("outputs", []), "execution_count": cell.get("execution_count")})
        elif kind == "markdown":
            markdown.append(source)
    return {
        "code": "\n\n".join(code),
        "markdown": "\n\n".join(markdown),
        "outputs": _canonical_json(outputs),
        "structure": _canonical_json(
            {
                "nbformat": notebook.get("nbformat"),
                "nbformat_minor": notebook.get("nbformat_minor"),
                "metadata": notebook.get("metadata", {}),
                "cells": structure,
            }
        ),
        "metadata": _canonical_json(
            {"notebook": notebook.get("metadata", {}), "cells": metadata}
        ),
    }


def _semantic_records(logical_path: str, text: str) -> dict[tuple[str, str], dict[str, str]]:
    suffix = Path(logical_path).suffix.lower()
    if suffix == ".ipynb":
        return _python_semantics(_notebook_parts(text)["code"])
    if suffix == ".py":
        return _python_semantics(text)
    if suffix in _CPP_SUFFIXES:
        return _cpp_semantics(text)
    if suffix in _CONFIG_SUFFIXES:
        config = _parse_config(logical_path, text)
        if config is not None:
            return {
                ("config", key): {
                    "fingerprint": _fingerprint(value),
                    "keywords": f"{key} {_canonical_json(value)}",
                }
                for key, value in sorted(config.items())
            }
    return {}


def _classifications(
    logical_path: str,
    symbol: str,
    granularity: str,
    keywords: str,
) -> list[str]:
    material = f"{logical_path} {symbol} {keywords}".lower()
    categories: set[str] = set()
    if granularity == "notebook_output":
        return ["notebook_output_cell_change"]
    if granularity in {"notebook_markdown", "notebook_structure"}:
        return ["notebook_experiment_visualization_change"]
    if granularity == "redacted_value":
        return ["sensitive_value_redacted_change"]
    if granularity == "non_executable_text":
        return ["non_executable_text_change"]
    if re.search(r"\b(?:ros|rclpy|roslibpy|rosbridge|topic|service|dds|domain|node)\b|/surgery/", material):
        categories.add("ros_connection_change")
    if re.search(
        r"waypoint|gain|calibr|sensor|aft200|force[_ -]?(?:zero|threshold)|can[_ -]?"
        r"(?:channel|bitrate)|joint[_ -]?slice|arm[_ -]?map|friction|robot[_ -]?ip",
        material,
    ):
        categories.add("waypoint_gain_sensor_calibration_change")
    if re.search(r"plot|matplotlib|seaborn|display|visuali[sz]|experiment|widget|ipy", material):
        categories.add("notebook_experiment_visualization_change")
    if re.search(
        r"retract|impedance|jog|motion|control|direct[_ -]?teach|trajectory|target[_ -]?joint|"
        r"movej|movel|stop|hold|state[_ -]?machine",
        material,
    ):
        categories.add("control_algorithm_change")
    if not categories:
        if Path(logical_path).suffix.lower() == ".ipynb":
            categories.add("notebook_experiment_visualization_change")
        else:
            categories.add("other_source_change")
    return [category for category in CLASSIFICATION_ORDER if category in categories]


def _snapshot_text(snapshot_dir: Path, entry: Mapping[str, Any]) -> str:
    path = _safe_destination(Path(snapshot_dir).resolve(), str(entry["snapshot_path"]))
    return path.read_text(encoding="utf-8-sig")


def _python_ast_fingerprint(text: str) -> str | None:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    return _fingerprint(ast.dump(tree, include_attributes=False))


def _environment_changes(
    before_manifest: Mapping[str, Any], after_manifest: Mapping[str, Any]
) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    before_environment = before_manifest.get("environment", {})
    after_environment = after_manifest.get("environment", {})
    before_values = _flatten_config(
        {
            "supplied": before_environment.get("supplied", {}),
            "local_capture": before_environment.get("local_capture", {}),
        }
    )
    after_values = _flatten_config(
        {
            "supplied": after_environment.get("supplied", {}),
            "local_capture": after_environment.get("local_capture", {}),
        }
    )
    for symbol in sorted(set(before_values) | set(after_values)):
        old_present = symbol in before_values
        new_present = symbol in after_values
        old = before_values.get(symbol)
        new = after_values.get(symbol)
        if old_present and new_present and old == new:
            continue
        material = symbol.lower()
        classifications = (
            ["ros_connection_change"]
            if any(term in material for term in ("ros_", "ros.", "rmw", "dds", "domain"))
            else ["environment_dependency_change"]
        )
        changes.append(
            {
                "logical_path": "environment/metadata",
                "granularity": "environment",
                "symbol": symbol,
                "change": "modified" if old_present and new_present else "added" if new_present else "removed",
                "before_fingerprint": _fingerprint(old) if old_present else None,
                "after_fingerprint": _fingerprint(new) if new_present else None,
                "classifications": classifications,
            }
        )
    before_files = {item["label"]: item for item in before_environment.get("files", [])}
    after_files = {item["label"]: item for item in after_environment.get("files", [])}
    for label in sorted(set(before_files) | set(after_files)):
        old = before_files.get(label)
        new = after_files.get(label)
        if old and new and old["stored_sha256"] == new["stored_sha256"]:
            if old["sha256"] == new["sha256"]:
                continue
            granularity = "redacted_value"
            classifications = ["sensitive_value_redacted_change"]
        else:
            granularity = "environment"
            classifications = (
                ["ros_connection_change"]
                if "ros" in label.lower()
                else ["environment_dependency_change"]
            )
        changes.append(
            {
                "logical_path": f"environment/{label}",
                "granularity": granularity,
                "symbol": label,
                "change": "modified" if old and new else "added" if new else "removed",
                "before_fingerprint": (
                    old["sha256"]
                    if old and granularity == "redacted_value"
                    else old["stored_sha256"] if old else None
                ),
                "after_fingerprint": (
                    new["sha256"]
                    if new and granularity == "redacted_value"
                    else new["stored_sha256"] if new else None
                ),
                "classifications": classifications,
            }
        )
    return changes


def compare_snapshots(before_dir: Path, after_dir: Path) -> dict[str, Any]:
    """Compare two verified snapshots at semantic/source granularity."""

    before_verification = verify_snapshot(before_dir)
    after_verification = verify_snapshot(after_dir)
    if not before_verification["ok"]:
        raise SnapshotError("before snapshot is invalid: " + "; ".join(before_verification["errors"]))
    if not after_verification["ok"]:
        raise SnapshotError("after snapshot is invalid: " + "; ".join(after_verification["errors"]))
    before_manifest = _load_manifest(Path(before_dir))
    after_manifest = _load_manifest(Path(after_dir))
    before_files = {item["logical_path"]: item for item in before_manifest["files"]}
    after_files = {item["logical_path"]: item for item in after_manifest["files"]}
    changes: list[dict[str, Any]] = []

    for logical_path in sorted(set(before_files) | set(after_files)):
        path_change_start = len(changes)
        before_entry = before_files.get(logical_path)
        after_entry = after_files.get(logical_path)
        before_text = _snapshot_text(Path(before_dir), before_entry) if before_entry else ""
        after_text = _snapshot_text(Path(after_dir), after_entry) if after_entry else ""
        before_semantics = _semantic_records(logical_path, before_text) if before_entry else {}
        after_semantics = _semantic_records(logical_path, after_text) if after_entry else {}
        semantic_change_count = 0
        for key in sorted(set(before_semantics) | set(after_semantics)):
            granularity, symbol = key
            old = before_semantics.get(key)
            new = after_semantics.get(key)
            if old and new and old["fingerprint"] == new["fingerprint"]:
                continue
            semantic_change_count += 1
            change_type = "modified" if old and new else "added" if new else "removed"
            keywords = " ".join(item["keywords"] for item in (old, new) if item)
            changes.append(
                {
                    "logical_path": logical_path,
                    "granularity": granularity,
                    "symbol": symbol,
                    "change": change_type,
                    "before_fingerprint": old["fingerprint"] if old else None,
                    "after_fingerprint": new["fingerprint"] if new else None,
                    "classifications": _classifications(logical_path, symbol, granularity, keywords),
                }
            )

        suffix = Path(logical_path).suffix.lower()
        stored_changed = bool(
            not before_entry
            or not after_entry
            or before_entry["stored_sha256"] != after_entry["stored_sha256"]
        )
        source_changed = bool(
            not before_entry or not after_entry or before_entry["sha256"] != after_entry["sha256"]
        )
        if suffix == ".ipynb" and before_entry and after_entry:
            before_parts = _notebook_parts(before_text)
            after_parts = _notebook_parts(after_text)
            if before_parts["outputs"] != after_parts["outputs"]:
                changes.append(
                    {
                        "logical_path": logical_path,
                        "granularity": "notebook_output",
                        "symbol": "<outputs>",
                        "change": "modified",
                        "output_only": before_parts["structure"] == after_parts["structure"],
                        "before_fingerprint": _fingerprint(before_parts["outputs"]),
                        "after_fingerprint": _fingerprint(after_parts["outputs"]),
                        "classifications": ["notebook_output_cell_change"],
                    }
                )
            if before_parts["code"] != after_parts["code"] and semantic_change_count == 0:
                before_ast = _python_ast_fingerprint(before_parts["code"])
                after_ast = _python_ast_fingerprint(after_parts["code"])
                executable_change = before_ast is None or after_ast is None or before_ast != after_ast
                granularity = "module_code" if executable_change else "non_executable_text"
                symbol = "<notebook-code>" if executable_change else "<notebook-code-comments>"
                changes.append(
                    {
                        "logical_path": logical_path,
                        "granularity": granularity,
                        "symbol": symbol,
                        "change": "modified",
                        "before_fingerprint": _fingerprint(before_parts["code"]),
                        "after_fingerprint": _fingerprint(after_parts["code"]),
                        "classifications": _classifications(
                            logical_path,
                            symbol,
                            granularity,
                            f"{before_parts['code']} {after_parts['code']}",
                        ),
                    }
                )
            if before_parts["markdown"] != after_parts["markdown"]:
                changes.append(
                    {
                        "logical_path": logical_path,
                        "granularity": "notebook_markdown",
                        "symbol": "<markdown>",
                        "change": "modified",
                        "before_fingerprint": _fingerprint(before_parts["markdown"]),
                        "after_fingerprint": _fingerprint(after_parts["markdown"]),
                        "classifications": ["notebook_experiment_visualization_change"],
                    }
                )
            if before_parts["metadata"] != after_parts["metadata"]:
                changes.append(
                    {
                        "logical_path": logical_path,
                        "granularity": "notebook_structure",
                        "symbol": "<metadata>",
                        "change": "modified",
                        "before_fingerprint": _fingerprint(before_parts["metadata"]),
                        "after_fingerprint": _fingerprint(after_parts["metadata"]),
                        "classifications": ["notebook_experiment_visualization_change"],
                    }
                )
            if (
                before_parts["structure"] != after_parts["structure"]
                and before_parts["code"] == after_parts["code"]
                and before_parts["markdown"] == after_parts["markdown"]
                and before_parts["metadata"] == after_parts["metadata"]
            ):
                changes.append(
                    {
                        "logical_path": logical_path,
                        "granularity": "notebook_structure",
                        "symbol": "<format-or-cell-identity>",
                        "change": "modified",
                        "before_fingerprint": _fingerprint(before_parts["structure"]),
                        "after_fingerprint": _fingerprint(after_parts["structure"]),
                        "classifications": ["non_executable_text_change"],
                    }
                )
            if len(changes) == path_change_start and stored_changed:
                changes.append(
                    {
                        "logical_path": logical_path,
                        "granularity": "non_executable_text",
                        "symbol": "<notebook-json-formatting>",
                        "change": "modified",
                        "before_fingerprint": before_entry["stored_sha256"],
                        "after_fingerprint": after_entry["stored_sha256"],
                        "classifications": ["non_executable_text_change"],
                    }
                )
        if before_entry and after_entry and not stored_changed and source_changed:
            changes.append(
                {
                    "logical_path": logical_path,
                    "granularity": "redacted_value",
                    "symbol": "<masked-sensitive-value>",
                    "change": "modified",
                    "before_fingerprint": before_entry["sha256"],
                    "after_fingerprint": after_entry["sha256"],
                    "classifications": ["sensitive_value_redacted_change"],
                }
            )
        elif stored_changed and semantic_change_count == 0 and (
            suffix != ".ipynb" or not before_entry or not after_entry
        ):
            granularity = "file" if not before_entry or not after_entry else "non_executable_text"
            change_type = "modified" if before_entry and after_entry else "added" if after_entry else "removed"
            changes.append(
                {
                    "logical_path": logical_path,
                    "granularity": granularity,
                    "symbol": "<file>",
                    "change": change_type,
                    "before_fingerprint": before_entry["stored_sha256"] if before_entry else None,
                    "after_fingerprint": after_entry["stored_sha256"] if after_entry else None,
                    "classifications": _classifications(
                        logical_path,
                        "<file>",
                        granularity,
                        f"{before_text} {after_text}",
                    ),
                }
            )

    changes.extend(_environment_changes(before_manifest, after_manifest))

    classification_counts = Counter(
        classification for change in changes for classification in change["classifications"]
    )
    change_counts = Counter(change["change"] for change in changes)
    return {
        "schema_version": SCHEMA_VERSION,
        "before_snapshot_id": before_manifest["snapshot_id"],
        "after_snapshot_id": after_manifest["snapshot_id"],
        "changes": changes,
        "summary": {
            "change_count": len(changes),
            "by_change": dict(sorted(change_counts.items())),
            "by_classification": {
                key: classification_counts[key]
                for key in CLASSIFICATION_ORDER
                if classification_counts[key]
            },
        },
    }


def record_acceptance_tag(
    snapshot_dir: Path,
    *,
    recorded_by: str,
    partner_approved_by: str,
    partner_approval_reference: str,
    partner_approved_at: str,
    recorded_at_utc: str | None = None,
) -> dict[str, Any]:
    """Record a supplied accepted-upstream claim without asserting verification."""

    required = {
        "recorded_by": recorded_by,
        "partner_approved_by": partner_approved_by,
        "partner_approval_reference": partner_approval_reference,
        "partner_approved_at": partner_approved_at,
    }
    missing = [key for key, value in required.items() if not str(value).strip()]
    if missing:
        raise SnapshotError("accepted-upstream requires supplied approval evidence fields: " + ", ".join(missing))
    verification = verify_snapshot(snapshot_dir)
    if not verification["ok"]:
        raise SnapshotError("cannot tag invalid snapshot: " + "; ".join(verification["errors"]))
    destination = Path(snapshot_dir) / "acceptance.json"
    if destination.exists():
        raise SnapshotError(f"acceptance record already exists and will not be overwritten: {destination}")
    redacted_fields, redaction_count = _redact_value(required)
    record = {
        "schema_version": ACCEPTANCE_SCHEMA_VERSION,
        "snapshot_id": verification["snapshot_id"],
        "tag": "accepted-upstream",
        "record_state": "user_supplied_external_approval_claim_unverified_by_tool",
        "recorded_at_utc": recorded_at_utc or _utc_now(),
        "recorded_by": redacted_fields["recorded_by"],
        "partner_approval": {
            "approved_by_supplied": redacted_fields["partner_approved_by"],
            "approved_at_supplied": redacted_fields["partner_approved_at"],
            "reference_supplied": redacted_fields["partner_approval_reference"],
            "verification": "not_verified_by_this_tool",
        },
        "redaction_count": redaction_count,
        "disclaimer": (
            "This local tag records caller-supplied approval metadata. It does not prove, "
            "perform, or replace partner approval."
        ),
    }
    destination.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    findings = _secret_findings(destination.read_text(encoding="utf-8"))
    if findings:
        destination.unlink()
        raise SnapshotError("acceptance metadata contains an unredacted secret pattern")
    return record


def _labeled_path(value: str, kind: str) -> SourceSpec:
    if "=" not in value:
        raise argparse.ArgumentTypeError("expected LABEL=PATH")
    label, raw_path = value.split("=", 1)
    if not label or not raw_path:
        raise argparse.ArgumentTypeError("expected non-empty LABEL=PATH")
    return SourceSpec(label, Path(raw_path), kind)


def _key_value(value: str) -> tuple[str, str]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("expected KEY=VALUE")
    key, raw_value = value.split("=", 1)
    if not key:
        raise argparse.ArgumentTypeError("expected non-empty KEY=VALUE")
    return key, raw_value


def _write_new_json(path: Path, payload: Mapping[str, Any]) -> None:
    if path.exists():
        raise SnapshotError(f"output already exists and will not be overwritten: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    capture = subparsers.add_parser("capture", help="capture explicit source roots into a new snapshot")
    capture.add_argument("--file", action="append", default=[], metavar="LABEL=PATH")
    capture.add_argument("--directory", action="append", default=[], metavar="LABEL=PATH")
    capture.add_argument("--output", required=True, type=Path)
    capture.add_argument("--environment", action="append", default=[], metavar="KEY=VALUE")
    capture.add_argument("--environment-file", action="append", default=[], metavar="LABEL=PATH")
    capture.add_argument("--collect-local-environment", action="store_true")
    capture.add_argument("--collect-pip-freeze", action="store_true")

    compare = subparsers.add_parser("compare", help="compare two verified snapshots")
    compare.add_argument("before", type=Path)
    compare.add_argument("after", type=Path)
    compare.add_argument("--output", type=Path)

    verify = subparsers.add_parser("verify", help="verify hashes, containment, and redaction")
    verify.add_argument("snapshot", type=Path)

    tag = subparsers.add_parser("tag-accepted-upstream", help="record caller-supplied partner approval metadata")
    tag.add_argument("snapshot", type=Path)
    tag.add_argument("--recorded-by", required=True)
    tag.add_argument("--partner-approved-by", required=True)
    tag.add_argument("--partner-approval-reference", required=True)
    tag.add_argument("--partner-approved-at", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "capture":
            specs = [_labeled_path(item, "file") for item in args.file]
            specs += [_labeled_path(item, "directory") for item in args.directory]
            environment = dict(_key_value(item) for item in args.environment)
            env_files = {
                spec.label: spec.path
                for spec in (_labeled_path(item, "file") for item in args.environment_file)
            }
            manifest = capture_snapshot(
                specs,
                args.output,
                supplied_environment=environment,
                environment_files=env_files,
                collect_local=args.collect_local_environment,
                include_pip_freeze=args.collect_pip_freeze,
            )
            payload: Mapping[str, Any] = {
                "snapshot_id": manifest["snapshot_id"],
                "manifest": str(args.output / "manifest.json"),
                "file_count": len(manifest["files"]),
            }
        elif args.command == "compare":
            payload = compare_snapshots(args.before, args.after)
            if args.output:
                _write_new_json(args.output, payload)
        elif args.command == "verify":
            payload = verify_snapshot(args.snapshot)
        else:
            payload = record_acceptance_tag(
                args.snapshot,
                recorded_by=args.recorded_by,
                partner_approved_by=args.partner_approved_by,
                partner_approval_reference=args.partner_approval_reference,
                partner_approved_at=args.partner_approved_at,
            )
    except (SnapshotError, argparse.ArgumentTypeError) as exc:
        parser.error(str(exc))
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.command == "verify" and not payload["ok"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
