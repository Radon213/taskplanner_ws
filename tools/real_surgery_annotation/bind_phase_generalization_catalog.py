#!/usr/bin/env python3
"""Bind case-local Phase catalogs to the cross-case functional v3 ontology.

The operation is intentionally narrow:

* the current canonical Phase JSONL is discovered from the manifest;
* a case-local catalog is derived from the common ontology and those records;
* only the catalog descriptor and ontology descriptor are rebound;
* the prior manifest is archived byte-for-byte before an atomic replacement.

Existing Phase records, projection reports, and every unrelated manifest value
remain untouched.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .finalize_interaction_review import publish_create_only
from .publish_assistant_case_reference import encode_json, load_jsonl


CATALOG_SCHEMA = "taskplanner.demo_procedure_phase_catalog.v3"
AUDIT_SCHEMA = "taskplanner.phase_catalog_binding_audit.v1"
LOCAL_CATALOG_NAME = "procedure_phases.generalization.v3.yaml"
AUDIT_NAME = "phase_catalog_generalization.audit.v3.json"
ARCHIVE_RELATIVE = Path(
    "audit_archive/phase_catalog_generalization_v3/"
    "annotation_manifest.before_catalog_v3.json"
)
COMMON_CATALOG_NAME = "procedure_phases.cross_case_provisional.v3.yaml"
EXPECTED_PHASES = ("P03", "P04", "P05", "P06")
LOCAL_RUNTIME_STATUS = "evaluation_only_draft_not_frozen"


class CatalogBindingError(ValueError):
    """A catalog binding would be incomplete, destructive, or inconsistent."""


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _load_json_bytes(data: bytes, *, path: Path) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CatalogBindingError(f"{path}: invalid JSON manifest") from exc
    if not isinstance(value, dict):
        raise CatalogBindingError(f"{path}: manifest must be an object")
    return value


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise CatalogBindingError(f"{path}: invalid YAML catalog") from exc
    if not isinstance(value, dict):
        raise CatalogBindingError(f"{path}: catalog must be an object")
    return value


def _relative_confined_file(
    *,
    base_dir: Path,
    value: Any,
    allowed_root: Path,
    label: str,
) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise CatalogBindingError(f"{label}: relative file is required")
    relative = Path(value)
    if relative.is_absolute():
        raise CatalogBindingError(f"{label}: absolute path is forbidden")
    target = (base_dir / relative).resolve()
    root = allowed_root.resolve()
    if target != root and root not in target.parents:
        raise CatalogBindingError(f"{label}: path escapes allowed root")
    if not target.is_file():
        raise CatalogBindingError(f"{label}: file does not exist: {target}")
    return target


def _case_local_output(
    *,
    case_dir: Path,
    value: str | Path,
    label: str,
    suffixes: tuple[str, ...],
) -> tuple[str, Path]:
    raw = str(value).strip()
    relative = Path(raw)
    if (
        not raw
        or relative.is_absolute()
        or relative == Path(".")
        or any(part in {".", ".."} for part in relative.parts)
    ):
        raise CatalogBindingError(
            f"{label}: a normalized case-local relative path is required"
        )
    if relative.suffix.lower() not in suffixes:
        raise CatalogBindingError(
            f"{label}: expected one of these suffixes: {suffixes}"
        )
    target = (case_dir / relative).resolve()
    root = case_dir.resolve()
    if target == root or root not in target.parents:
        raise CatalogBindingError(f"{label}: path escapes the case directory")
    return relative.as_posix(), target


def _phase_records(
    *,
    case_id: str,
    phase_path: Path,
    expected_sha256: Any,
) -> list[dict[str, Any]]:
    if not isinstance(expected_sha256, str):
        raise CatalogBindingError("phase reference SHA-256 is missing")
    actual_sha256 = _sha256_file(phase_path)
    if actual_sha256 != expected_sha256:
        raise CatalogBindingError(
            "phase reference SHA-256 mismatch: "
            f"expected={expected_sha256}, actual={actual_sha256}"
        )
    records = load_jsonl(phase_path)
    phase_ids = [str(record.get("phase_id", "")) for record in records]
    if phase_ids != list(EXPECTED_PHASES):
        raise CatalogBindingError(
            f"{case_id}: expected canonical P03-P06, got {phase_ids}"
        )
    frames: list[int] = []
    for index, record in enumerate(records, 1):
        location = f"{phase_path}:{index}"
        if record.get("case_id") != case_id:
            raise CatalogBindingError(f"{location}: case_id mismatch")
        if record.get("event_type") != "phase_start":
            raise CatalogBindingError(f"{location}: not a phase_start")
        frame = record.get("source_frame_idx")
        if not isinstance(frame, int) or frame < 0:
            raise CatalogBindingError(f"{location}: invalid source frame")
        frames.append(frame)
        try:
            time_sec = float(record.get("time_sec"))
        except (TypeError, ValueError) as exc:
            raise CatalogBindingError(
                f"{location}: invalid time_sec"
            ) from exc
        if time_sec < 0:
            raise CatalogBindingError(f"{location}: negative time_sec")
        review = record.get("review")
        if not isinstance(review, dict) or not str(
            review.get("notes", "")
        ).strip():
            raise CatalogBindingError(f"{location}: review notes are required")
    if frames[0] != 0 or any(
        left >= right for left, right in zip(frames, frames[1:])
    ):
        raise CatalogBindingError(
            f"{case_id}: Phase frames must start at 0 and increase: {frames}"
        )
    return records


def _tool_pattern(phase: dict[str, Any]) -> str:
    raw_roles = phase.get("tool_role_examples")
    roles: list[str] = []
    if isinstance(raw_roles, dict):
        for role, examples in raw_roles.items():
            if isinstance(examples, list):
                names = ", ".join(str(value) for value in examples)
            else:
                names = str(examples)
            roles.append(f"{role}={names}")
    role_text = "; ".join(roles) if roles else "no named tool required"
    if phase.get("phase_id") == "P06":
        return (
            "Functional examples only, never required or ordered: "
            f"{role_text}. P06 begins with persistent focal control; an "
            "energy-tool appearance or exchange is neither required nor "
            "sufficient."
        )
    return (
        "Functional examples only, never required or ordered: "
        f"{role_text}. Use the visible functional state and persistent "
        "tool-to-tissue interaction."
    )


def build_case_catalog(
    *,
    case_id: str,
    phase_path: Path,
    phase_sha256: str,
    phase_records: list[dict[str, Any]],
    common_catalog_path: Path,
    common_catalog: dict[str, Any],
) -> dict[str, Any]:
    """Return a deterministic case-local browser catalog."""

    if common_catalog.get("schema") != CATALOG_SCHEMA:
        raise CatalogBindingError("cross-case v3 catalog schema mismatch")
    raw_phases = common_catalog.get("phases")
    if not isinstance(raw_phases, list):
        raise CatalogBindingError("cross-case v3 phases must be a list")
    common_by_id = {
        str(phase.get("phase_id")): phase
        for phase in raw_phases
        if isinstance(phase, dict)
    }
    missing = set(EXPECTED_PHASES) - set(common_by_id)
    if missing:
        raise CatalogBindingError(
            f"cross-case v3 is missing phases: {sorted(missing)}"
        )
    event_by_phase = {
        str(record["phase_id"]): record for record in phase_records
    }

    phases: list[dict[str, Any]] = []
    copied_fields = (
        "phase_id",
        "name",
        "name_ko",
        "definition_source",
        "observable_definition",
        "positive_cues",
        "negative_cues",
        "tool_role_examples",
        "boundary_rule",
    )
    for phase_id in EXPECTED_PHASES:
        common_phase = common_by_id[phase_id]
        event = event_by_phase[phase_id]
        review = event["review"]
        frame = int(event["source_frame_idx"])
        local_phase = {
            key: copy.deepcopy(common_phase[key])
            for key in copied_fields
            if key in common_phase
        }
        local_phase[f"observed_in_{case_id}"] = True
        local_phase["tool_pattern"] = _tool_pattern(common_phase)
        local_phase["annotation_note"] = str(review["notes"]).strip()
        local_phase["case_observation"] = {
            "event_id": str(event.get("event_id", "")),
            "annotation_frame": frame,
            "observed_key_frame": frame,
            "time_sec": float(event["time_sec"]),
            "source_views": copy.deepcopy(event.get("source_views", [])),
            "phase_boundary_kind": str(
                event.get("phase_boundary_kind", "")
            ),
            "review_status": str(event.get("review_status", "")),
        }
        phases.append(local_phase)

    annotation_root = common_catalog_path.parent
    case_dir = phase_path.parent
    return {
        "schema": CATALOG_SCHEMA,
        "procedure_id": common_catalog.get("procedure_id"),
        "phase_namespace": common_catalog.get("phase_namespace"),
        "case_id_used_for_observed_subset": case_id,
        "authority": (
            "user_authorized_ai_assistant_case_binding_of_cross_case_v3"
        ),
        "reviewer_model": common_catalog.get("reviewer_model"),
        "runtime_status": LOCAL_RUNTIME_STATUS,
        "source_ontology": {
            "file": os.path.relpath(common_catalog_path, case_dir),
            "sha256": _sha256_file(common_catalog_path),
            "role": "authoritative_cross_case_functional_definition",
        },
        "source_phase_reference": {
            "file": phase_path.name,
            "sha256": phase_sha256,
            "event_count": len(phase_records),
            "annotation_frames": {
                record["phase_id"]: int(record["source_frame_idx"])
                for record in phase_records
            },
        },
        "information_boundary": {
            "runtime_input_allowed": False,
            "scoring_role": "context_only_not_ground_truth",
            "held_out_eligible": False,
            "common_catalog_root": os.path.relpath(
                annotation_root,
                case_dir,
            ),
        },
        "granularity_policy": copy.deepcopy(
            common_catalog.get("granularity_policy", {})
        ),
        "assignment_rules": copy.deepcopy(
            common_catalog.get("assignment_rules", {})
        ),
        "phase_order": list(EXPECTED_PHASES),
        "phases": phases,
    }


def encode_catalog(catalog: dict[str, Any]) -> bytes:
    return yaml.safe_dump(
        catalog,
        allow_unicode=True,
        sort_keys=False,
        width=96,
    ).encode("utf-8")


def _without_catalog_bindings(manifest: dict[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(manifest)
    phase_annotation = value.get("phase_annotation")
    if isinstance(phase_annotation, dict):
        phase_annotation.pop("procedure_catalog_file", None)
        phase_annotation.pop("procedure_catalog_sha256", None)
    phase_reference = (
        value.get("evaluation_reference", {}).get("phase_reference")
        if isinstance(value.get("evaluation_reference"), dict)
        else None
    )
    if isinstance(phase_reference, dict):
        phase_reference.pop("ontology_file", None)
        phase_reference.pop("ontology_sha256", None)
    return value


def updated_manifest(
    *,
    manifest: dict[str, Any],
    catalog_file: str = LOCAL_CATALOG_NAME,
    catalog_sha256: str,
    ontology_file: str,
    ontology_sha256: str,
) -> dict[str, Any]:
    updated = copy.deepcopy(manifest)
    phase_annotation = updated.get("phase_annotation")
    reference = updated.get("evaluation_reference")
    phase_reference = (
        reference.get("phase_reference")
        if isinstance(reference, dict)
        else None
    )
    if not isinstance(phase_annotation, dict):
        raise CatalogBindingError("manifest.phase_annotation is required")
    if not isinstance(phase_reference, dict):
        raise CatalogBindingError(
            "manifest.evaluation_reference.phase_reference is required"
        )
    phase_annotation["procedure_catalog_file"] = catalog_file
    phase_annotation["procedure_catalog_sha256"] = catalog_sha256
    phase_reference["ontology_file"] = ontology_file
    phase_reference["ontology_sha256"] = ontology_sha256
    if _without_catalog_bindings(updated) != _without_catalog_bindings(
        manifest
    ):
        raise CatalogBindingError("non-catalog manifest values changed")
    return updated


def _atomic_replace(path: Path, data: bytes, *, source_mode: int) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    succeeded = False
    try:
        os.fchmod(descriptor, stat.S_IMODE(source_mode))
        offset = 0
        while offset < len(data):
            written = os.write(descriptor, data[offset:])
            if written <= 0:
                raise CatalogBindingError(f"{path}: staging write failed")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        succeeded = True
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if not succeeded:
            temporary.unlink(missing_ok=True)


def _remove_created(paths: list[Path]) -> None:
    for path in reversed(paths):
        path.unlink(missing_ok=True)


def bind_case(
    *,
    case_dir: Path,
    common_catalog_path: Path,
    catalog_relative: str | Path = LOCAL_CATALOG_NAME,
    audit_relative: str | Path = AUDIT_NAME,
    archive_relative: str | Path = ARCHIVE_RELATIVE,
    bound_at: str | None = None,
    max_snapshot_retries: int = 3,
) -> dict[str, Any]:
    """Create and publish one case catalog against the latest manifest."""

    case_dir = case_dir.resolve()
    case_id = case_dir.name
    if case_id == "0704_6":
        raise CatalogBindingError(
            "0704_6 is explicitly outside this batch binding operation"
        )
    manifest_path = case_dir / "annotation_manifest.json"
    catalog_file, catalog_path = _case_local_output(
        case_dir=case_dir,
        value=catalog_relative,
        label="local catalog",
        suffixes=(".yaml", ".yml"),
    )
    audit_file, audit_path = _case_local_output(
        case_dir=case_dir,
        value=audit_relative,
        label="binding audit",
        suffixes=(".json",),
    )
    archive_file, archive_path = _case_local_output(
        case_dir=case_dir,
        value=archive_relative,
        label="archived manifest",
        suffixes=(".json",),
    )
    output_paths = (catalog_path, audit_path, archive_path)
    if len(set(output_paths)) != len(output_paths):
        raise CatalogBindingError("binding output paths must be distinct")
    if manifest_path.resolve() in output_paths:
        raise CatalogBindingError(
            "binding output must not replace the canonical manifest directly"
        )
    annotation_root = case_dir.parents[1]
    common_catalog_path = common_catalog_path.resolve()
    expected_common_path = annotation_root / COMMON_CATALOG_NAME
    if common_catalog_path != expected_common_path.resolve():
        raise CatalogBindingError(
            "common catalog must be the annotation-root v3 ontology"
        )
    common_catalog = _load_yaml(common_catalog_path)
    ontology_sha256 = _sha256_file(common_catalog_path)
    ontology_file = os.path.relpath(common_catalog_path, case_dir)

    for _attempt in range(max_snapshot_retries):
        manifest_before = manifest_path.read_bytes()
        manifest = _load_json_bytes(manifest_before, path=manifest_path)
        if manifest.get("case_id") != case_id:
            raise CatalogBindingError(f"{case_id}: manifest case_id mismatch")
        reference = manifest.get("evaluation_reference")
        phase_reference = (
            reference.get("phase_reference")
            if isinstance(reference, dict)
            else None
        )
        if not isinstance(phase_reference, dict):
            raise CatalogBindingError(
                "manifest evaluation Phase reference is missing"
            )
        phase_path = _relative_confined_file(
            base_dir=case_dir,
            value=phase_reference.get("file"),
            allowed_root=case_dir,
            label="phase reference",
        )
        phase_sha256 = str(phase_reference.get("sha256", ""))
        records = _phase_records(
            case_id=case_id,
            phase_path=phase_path,
            expected_sha256=phase_sha256,
        )
        if phase_path in output_paths:
            raise CatalogBindingError(
                "binding output must not replace the canonical Phase file"
            )
        catalog = build_case_catalog(
            case_id=case_id,
            phase_path=phase_path,
            phase_sha256=phase_sha256,
            phase_records=records,
            common_catalog_path=common_catalog_path,
            common_catalog=common_catalog,
        )
        catalog_data = encode_catalog(catalog)
        catalog_sha256 = _sha256_bytes(catalog_data)
        updated = updated_manifest(
            manifest=manifest,
            catalog_file=catalog_file,
            catalog_sha256=catalog_sha256,
            ontology_file=ontology_file,
            ontology_sha256=ontology_sha256,
        )
        manifest_after = encode_json(updated)

        already_bound = (
            manifest_before == manifest_after
            and catalog_path.is_file()
            and _sha256_file(catalog_path) == catalog_sha256
            and archive_path.is_file()
            and audit_path.is_file()
        )
        if already_bound:
            return {
                "ok": True,
                "case_id": case_id,
                "already_bound": True,
                "phase_file": phase_path.name,
                "phase_sha256": phase_sha256,
                "catalog": str(catalog_path),
                "catalog_sha256": catalog_sha256,
                "manifest": str(manifest_path),
                "manifest_sha256": _sha256_bytes(manifest_before),
                "audit": str(audit_path),
            }
        existing = [
            path
            for path in (catalog_path, archive_path, audit_path)
            if path.exists()
        ]
        if existing:
            raise CatalogBindingError(
                "create-only binding target exists before manifest binding: "
                + ", ".join(str(path) for path in existing)
            )

        if manifest_path.read_bytes() != manifest_before:
            continue

        timestamp = bound_at or datetime.now(timezone.utc).isoformat()
        phase_annotation_before = copy.deepcopy(
            manifest["phase_annotation"]
        )
        phase_reference_before = copy.deepcopy(phase_reference)
        audit = {
            "schema": AUDIT_SCHEMA,
            "case_id": case_id,
            "authority": "deterministic_create_only_catalog_binding",
            "bound_at": timestamp,
            "binding_audit_file": audit_file,
            "source_manifest_file": manifest_path.name,
            "manifest_sha256_before": _sha256_bytes(manifest_before),
            "manifest_sha256_after": _sha256_bytes(manifest_after),
            "archived_manifest_file": archive_file,
            "canonical_phase_reference": {
                "file": phase_path.name,
                "sha256": phase_sha256,
                "annotation_frames": {
                    record["phase_id"]: record["source_frame_idx"]
                    for record in records
                },
            },
            "procedure_catalog": {
                "file": catalog_file,
                "sha256": catalog_sha256,
                "derived_from_ontology_file": ontology_file,
                "derived_from_ontology_sha256": ontology_sha256,
            },
            "manifest_bindings_before": {
                "procedure_catalog_file": phase_annotation_before.get(
                    "procedure_catalog_file"
                ),
                "procedure_catalog_sha256": phase_annotation_before.get(
                    "procedure_catalog_sha256"
                ),
                "ontology_file": phase_reference_before.get("ontology_file"),
                "ontology_sha256": phase_reference_before.get(
                    "ontology_sha256"
                ),
            },
            "manifest_bindings_after": {
                "procedure_catalog_file": catalog_file,
                "procedure_catalog_sha256": catalog_sha256,
                "ontology_file": ontology_file,
                "ontology_sha256": ontology_sha256,
            },
            "preservation": {
                "canonical_phase_file_unchanged": True,
                "canonical_phase_sha256_unchanged": True,
                "projection_report_descriptor_unchanged": (
                    manifest["evaluation_reference"].get(
                        "projection_report_file"
                    )
                    == updated["evaluation_reference"].get(
                        "projection_report_file"
                    )
                    and manifest["evaluation_reference"].get(
                        "projection_report_sha256"
                    )
                    == updated["evaluation_reference"].get(
                        "projection_report_sha256"
                    )
                ),
                "all_non_catalog_manifest_values_unchanged": (
                    _without_catalog_bindings(manifest)
                    == _without_catalog_bindings(updated)
                ),
            },
            "information_boundary": (
                "evaluation_only_context_never_runtime_input"
            ),
        }
        audit_data = encode_json(audit)
        created = [catalog_path, archive_path, audit_path]
        publish_create_only(
            {
                catalog_path: catalog_data,
                archive_path: manifest_before,
                audit_path: audit_data,
            }
        )
        try:
            if manifest_path.read_bytes() != manifest_before:
                _remove_created(created)
                continue
            _atomic_replace(
                manifest_path,
                manifest_after,
                source_mode=manifest_path.stat().st_mode,
            )
        except Exception:
            _remove_created(created)
            raise
        return {
            "ok": True,
            "case_id": case_id,
            "already_bound": False,
            "phase_file": phase_path.name,
            "phase_sha256": phase_sha256,
            "catalog": str(catalog_path),
            "catalog_sha256": catalog_sha256,
            "manifest": str(manifest_path),
            "manifest_sha256": _sha256_bytes(manifest_after),
            "audit": str(audit_path),
        }

    raise CatalogBindingError(
        f"{case_id}: manifest changed during all snapshot attempts"
    )


def bind_cases(
    *,
    annotation_root: Path,
    case_ids: list[str],
    catalog_relative: str | Path = LOCAL_CATALOG_NAME,
    audit_relative: str | Path = AUDIT_NAME,
    archive_relative: str | Path = ARCHIVE_RELATIVE,
    bound_at: str | None = None,
) -> list[dict[str, Any]]:
    annotation_root = annotation_root.resolve()
    common_catalog_path = annotation_root / COMMON_CATALOG_NAME
    results: list[dict[str, Any]] = []
    for case_id in case_ids:
        if case_id == "0704_6":
            raise CatalogBindingError("0704_6 must not be altered by this tool")
        results.append(
            bind_case(
                case_dir=annotation_root / "cases" / case_id,
                common_catalog_path=common_catalog_path,
                catalog_relative=catalog_relative,
                audit_relative=audit_relative,
                archive_relative=archive_relative,
                bound_at=bound_at,
            )
        )
    return results


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--annotation-root",
        type=Path,
        default=Path("annotations/observable_tool_events"),
    )
    parser.add_argument(
        "case_ids",
        nargs="*",
        default=[f"0704_{index}" for index in range(7, 18)],
    )
    parser.add_argument(
        "--bound-at",
        help="Optional fixed ISO timestamp for a reproducible audit.",
    )
    parser.add_argument(
        "--catalog-file",
        default=LOCAL_CATALOG_NAME,
        help=(
            "Create-only case-local relative YAML path. Use a new versioned "
            "name when rebinding after a Phase promotion."
        ),
    )
    parser.add_argument(
        "--audit-file",
        default=AUDIT_NAME,
        help="Create-only case-local relative JSON audit path.",
    )
    parser.add_argument(
        "--archive-relative",
        default=str(ARCHIVE_RELATIVE),
        help=(
            "Create-only case-local relative JSON path for the byte-identical "
            "pre-binding manifest."
        ),
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        results = bind_cases(
            annotation_root=args.annotation_root,
            case_ids=args.case_ids,
            catalog_relative=args.catalog_file,
            audit_relative=args.audit_file,
            archive_relative=args.archive_relative,
            bound_at=args.bound_at,
        )
    except (CatalogBindingError, OSError, ValueError) as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
    print(json.dumps(results, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
