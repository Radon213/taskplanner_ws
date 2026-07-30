#!/usr/bin/env python3
"""Create-only remediation of provisional Phase boundaries and case manifest.

The source Phase reference and projection report remain untouched.  A new
Phase JSONL and a report bound to that JSONL are created, the prior canonical
manifest is archived byte-for-byte, and a newly validated canonical manifest
is published transactionally.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any

import jsonschema

from .finalize_assistant_interaction_review import (
    validate_reviewed_at_not_future,
)
from .finalize_interaction_review import encode_jsonl, publish_create_only
from .publish_assistant_case_reference import (
    build_manifest,
    encode_json,
    load_json,
    load_jsonl,
    sha256_file,
)


SPEC_SCHEMA = "taskplanner.phase_boundary_remediation_spec.v1"
AUDIT_SCHEMA = "taskplanner.phase_boundary_remediation_audit.v1"
EXPLICIT_USER_OVERRIDE_AUTHORITY = "explicit_user_override"
ASSISTANT_PHASE_CONTEXT_AUTHORITY = (
    "user_authorized_ai_assistant_video_adjudication_"
    "provisional_context_not_scoring_ground_truth"
)


class RemediationError(ValueError):
    """The proposed Phase remediation is unsafe or internally inconsistent."""


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _require_new(path: Path, *, label: str) -> Path:
    resolved = path.resolve()
    if resolved.exists():
        raise RemediationError(f"{label} create-only target exists: {resolved}")
    return resolved


def _frame_time(timeline: dict[str, Any], frame_index: int) -> float:
    timestamps = timeline.get("timestamps_sec")
    if (
        not isinstance(timestamps, list)
        or len(timestamps) != timeline.get("frame_count")
    ):
        raise RemediationError("timeline timestamps_sec/frame_count mismatch")
    if not 0 <= frame_index < len(timestamps):
        raise RemediationError(f"frame outside timeline: {frame_index}")
    return float(timestamps[frame_index])


def _validate_phase_sequence(
    *,
    phases: list[dict[str, Any]],
    timeline: dict[str, Any],
    point_schema: dict[str, Any],
) -> None:
    phase_ids = [str(item.get("phase_id")) for item in phases]
    if phase_ids != ["P03", "P04", "P05", "P06"]:
        raise RemediationError(f"expected P03-P06 sequence, got {phase_ids}")
    frames = [int(item["source_frame_idx"]) for item in phases]
    if frames[0] != 0 or any(
        current >= following for current, following in zip(frames, frames[1:])
    ):
        raise RemediationError(f"Phase frames must be strictly increasing: {frames}")
    gaps = timeline.get("gaps", [])
    for index, phase in enumerate(phases, 1):
        try:
            jsonschema.validate(phase, point_schema)
        except jsonschema.ValidationError as exc:
            raise RemediationError(
                f"Phase row {index} schema error: {exc.message}"
            ) from exc
        frame = int(phase["source_frame_idx"])
        expected_time = _frame_time(timeline, frame)
        if float(phase["time_sec"]) != expected_time:
            raise RemediationError(
                f"{phase['phase_id']}: non-canonical frame time"
            )
        if any(
            float(gap.get("start_sec", gap.get("before_time_sec")))
            < expected_time
            < float(gap.get("end_sec", gap.get("after_time_sec")))
            for gap in gaps
        ):
            raise RemediationError(
                f"{phase['phase_id']}: boundary lies inside visual gap"
            )


def _validated_explicit_user_override(
    *,
    spec: dict[str, Any],
    case_dir: Path,
) -> dict[str, Any] | None:
    raw = spec.get("promotion")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise RemediationError("promotion object required")
    if raw.get("authority") != EXPLICIT_USER_OVERRIDE_AUTHORITY:
        raise RemediationError(
            "promotion.authority must be explicit_user_override"
        )
    if raw.get("label_origin") != "assistant_video_adjudication":
        raise RemediationError(
            "explicit user override must use assistant_video_adjudication"
        )
    if raw.get("reviewer_kind") != "ai_assistant":
        raise RemediationError(
            "explicit user override reviewer_kind must be ai_assistant"
        )
    reviewer_id = str(raw.get("reviewer_id", "")).strip()
    authorized_by = str(raw.get("authorized_by", "")).strip()
    if not reviewer_id or not authorized_by:
        raise RemediationError(
            "explicit user override reviewer_id/authorized_by required"
        )

    phase_review_notes = raw.get("phase_review_notes")
    if (
        not isinstance(phase_review_notes, dict)
        or set(phase_review_notes) != {"P03", "P04", "P05", "P06"}
        or any(
            not isinstance(note, str) or not note.strip()
            for note in phase_review_notes.values()
        )
    ):
        raise RemediationError(
            "explicit user override requires P03-P06 phase_review_notes"
        )

    proposal_file = str(raw.get("source_proposal_file", "")).strip()
    proposal_sha256 = str(raw.get("source_proposal_sha256", "")).strip()
    catalog_file = str(raw.get("procedure_catalog_file", "")).strip()
    catalog_sha256 = str(raw.get("procedure_catalog_sha256", "")).strip()
    ontology_file = str(raw.get("ontology_file", "")).strip()
    ontology_sha256 = str(raw.get("ontology_sha256", "")).strip()
    if not all(
        (
            proposal_file,
            proposal_sha256,
            catalog_file,
            catalog_sha256,
            ontology_file,
            ontology_sha256,
        )
    ):
        raise RemediationError(
            "explicit user override proposal/catalog/ontology descriptors required"
        )

    annotation_root = case_dir.parents[1]
    proposal_path = (case_dir / proposal_file).resolve()
    catalog_path = (case_dir / catalog_file).resolve()
    ontology_path = (case_dir / ontology_file).resolve()
    if (
        proposal_path.parent != case_dir
        or catalog_path.parent != case_dir
        or (
            ontology_path != annotation_root
            and annotation_root not in ontology_path.parents
        )
    ):
        raise RemediationError(
            "explicit user override artifact path escapes its allowed root"
        )
    for label, path, expected_sha256 in (
        ("source proposal", proposal_path, proposal_sha256),
        ("procedure catalog", catalog_path, catalog_sha256),
        ("ontology", ontology_path, ontology_sha256),
    ):
        if not path.is_file() or sha256_file(path) != expected_sha256:
            raise RemediationError(
                f"explicit user override {label} hash mismatch"
            )

    return {
        "authority": EXPLICIT_USER_OVERRIDE_AUTHORITY,
        "authorized_by": authorized_by,
        "reviewer_kind": "ai_assistant",
        "reviewer_id": reviewer_id,
        "label_origin": "assistant_video_adjudication",
        "phase_review_notes": {
            phase_id: str(note).strip()
            for phase_id, note in phase_review_notes.items()
        },
        "source_proposal_file": proposal_file,
        "source_proposal_sha256": proposal_sha256,
        "source_proposal_path": proposal_path,
        "procedure_catalog_file": catalog_file,
        "procedure_catalog_sha256": catalog_sha256,
        "procedure_catalog_path": catalog_path,
        "ontology_file": ontology_file,
        "ontology_sha256": ontology_sha256,
        "ontology_path": ontology_path,
    }


def _updated_report(
    *,
    source_report: dict[str, Any],
    source_report_path: Path,
    source_phase_path: Path,
    target_phase_path: Path,
    target_phase_data: bytes,
    phases: list[dict[str, Any]],
    corrections: list[dict[str, Any]],
    reviewed_at: str,
    promotion: dict[str, Any] | None,
) -> dict[str, Any]:
    report = copy.deepcopy(source_report)
    target_sha256 = _sha256_bytes(target_phase_data)
    report.setdefault("inputs", {})["phase_context"] = target_sha256
    report.setdefault("outputs", {})["phase_reference"] = {
        "path": str(target_phase_path.resolve()),
        "sha256": target_sha256,
        "record_count": len(phases),
        "status": "provisional_ambiguous_context_only_not_scored",
    }
    descriptor = (
        report.setdefault("manifest_handoff", {})
        .setdefault("evaluation_reference", {})
        .setdefault("phase_reference", {})
    )
    descriptor.update(
        {
            "file": target_phase_path.name,
            "sha256": target_sha256,
            "event_count": len(phases),
            "event_type_counts": {"phase_start": len(phases)},
            "review_status_counts": {"ambiguous": len(phases)},
            "scoring_role": "context_only_not_ground_truth",
            "status": "provisional_ambiguous",
        }
    )
    report["phase_remediation"] = {
        "authority": "user_authorized_codex_cross_case_phase_qa",
        "reviewed_at": reviewed_at,
        "source_phase_file": source_phase_path.name,
        "source_phase_sha256": sha256_file(source_phase_path),
        "source_projection_report": str(source_report_path.resolve()),
        "source_projection_report_sha256": sha256_file(source_report_path),
        "corrections": copy.deepcopy(corrections),
        "scoring_role": "context_only_not_ground_truth",
    }
    if promotion is not None:
        report.setdefault("authority", {})["phase_reference"] = (
            "explicit_user_override_authorized_ai_assistant_"
            "provisional_context"
        )
        descriptor.update(
            {
                "promotion_authority": promotion["authority"],
                "ontology_file": promotion["ontology_file"],
                "ontology_sha256": promotion["ontology_sha256"],
            }
        )
        report["phase_remediation"].update(
            {
                "authority": promotion["authority"],
                "authorized_by": promotion["authorized_by"],
                "reviewer_kind": promotion["reviewer_kind"],
                "reviewer_id": promotion["reviewer_id"],
                "source_proposal_file": promotion["source_proposal_file"],
                "source_proposal_sha256": promotion[
                    "source_proposal_sha256"
                ],
                "procedure_catalog_file": promotion[
                    "procedure_catalog_file"
                ],
                "procedure_catalog_sha256": promotion[
                    "procedure_catalog_sha256"
                ],
                "ontology_file": promotion["ontology_file"],
                "ontology_sha256": promotion["ontology_sha256"],
            }
        )
    return report


def _relative(path: Path, *, case_dir: Path) -> str:
    return os.path.relpath(path.resolve(), case_dir.resolve())


def _updated_manifest_from_current(
    *,
    current_manifest: dict[str, Any],
    case_dir: Path,
    source_phase_path: Path,
    target_phase_path: Path,
    target_phase_sha256: str,
    phases: list[dict[str, Any]],
    source_report_path: Path,
    target_report_path: Path,
    target_report_sha256: str,
    promotion: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Rebind only Phase/report descriptors in an existing manifest.

    This path is used when immutable source-bag metadata is temporarily
    offline.  It refuses to rebuild or reinterpret observed, DT, mask, speech,
    or source-bag descriptors.
    """

    case_id = case_dir.name
    manifest = copy.deepcopy(current_manifest)
    if (
        manifest.get("schema")
        != "taskplanner.observable_annotation_manifest.v1"
        or manifest.get("case_id") != case_id
    ):
        raise RemediationError("current manifest schema/case_id mismatch")
    reference = manifest.get("evaluation_reference")
    minimal = manifest.get("minimal_interaction_annotation")
    phase_annotation = manifest.get("phase_annotation")
    if not all(
        isinstance(value, dict)
        for value in (reference, minimal, phase_annotation)
    ):
        raise RemediationError("current manifest Phase descriptors are missing")

    phase_reference = reference.get("phase_reference")
    if not isinstance(phase_reference, dict):
        raise RemediationError("current evaluation Phase descriptor is missing")
    source_phase_sha256 = sha256_file(source_phase_path)
    if (
        phase_reference.get("file") != source_phase_path.name
        or phase_reference.get("sha256") != source_phase_sha256
    ):
        raise RemediationError("current evaluation Phase is not the requested source")
    phase_annotation_file = phase_annotation.get(
        "provisional_reference_file"
    )
    phase_annotation_sha256 = phase_annotation.get(
        "provisional_reference_sha256"
    )
    phase_annotation_matches_source = (
        phase_annotation_file == source_phase_path.name
        and phase_annotation_sha256 == source_phase_sha256
    )
    if not phase_annotation_matches_source:
        if promotion is None:
            raise RemediationError(
                "current phase_annotation is not bound to the requested source"
            )
        legacy_phase_path = (
            case_dir / str(phase_annotation_file or "")
        ).resolve()
        if (
            legacy_phase_path.parent != case_dir
            or not legacy_phase_path.is_file()
            or sha256_file(legacy_phase_path) != phase_annotation_sha256
        ):
            raise RemediationError(
                "current legacy human phase_annotation is invalid"
            )
        source_geometry = [
            (
                str(item.get("phase_id")),
                int(item["source_frame_idx"]),
                float(item["time_sec"]),
            )
            for item in load_jsonl(source_phase_path)
        ]
        legacy_geometry = [
            (
                str(item.get("phase_id")),
                int(item["source_frame_idx"]),
                float(item["time_sec"]),
            )
            for item in load_jsonl(legacy_phase_path)
        ]
        if legacy_geometry != source_geometry:
            raise RemediationError(
                "current legacy human phase_annotation geometry differs "
                "from the requested source"
            )
    if (
        minimal.get("provisional_phase_reference_file")
        != source_phase_path.name
        or minimal.get("provisional_phase_reference_sha256")
        != source_phase_sha256
    ):
        raise RemediationError(
            "current minimal Phase descriptor is not bound to the source"
        )
    current_report_path = (
        case_dir / str(reference.get("projection_report_file", ""))
    ).resolve()
    if (
        current_report_path != source_report_path.resolve()
        or reference.get("projection_report_sha256")
        != sha256_file(source_report_path)
    ):
        raise RemediationError("current projection report is not the requested source")

    old_phase_reference = copy.deepcopy(phase_reference)
    old_phase_annotation = copy.deepcopy(phase_annotation)
    phase_reference.update(
        {
            "file": target_phase_path.name,
            "sha256": target_phase_sha256,
            "event_count": len(phases),
            "event_type_counts": dict(
                sorted(Counter(item["event_type"] for item in phases).items())
            ),
            "review_status_counts": dict(
                sorted(Counter(item["review_status"] for item in phases).items())
            ),
        }
    )
    reference["projection_report_file"] = _relative(
        target_report_path,
        case_dir=case_dir,
    )
    reference["projection_report_sha256"] = target_report_sha256
    minimal["provisional_phase_reference_file"] = target_phase_path.name
    minimal["provisional_phase_reference_sha256"] = target_phase_sha256
    phase_annotation["provisional_reference_file"] = target_phase_path.name
    phase_annotation["provisional_reference_sha256"] = target_phase_sha256
    phase_annotation["event_count"] = len(phases)
    phase_annotation["review_status_counts"] = {
        status: sum(item["review_status"] == status for item in phases)
        for status in ("ambiguous", "confirmed", "rejected")
    }
    if promotion is not None:
        for field in (
            "candidate_file",
            "candidate_sha256",
            "effective_review_status_counts",
            "human_decision_file",
            "human_decision_sha256",
        ):
            phase_annotation.pop(field, None)
        phase_annotation.update(
            {
                "authority": ASSISTANT_PHASE_CONTEXT_AUTHORITY,
                "procedure_catalog_file": promotion[
                    "procedure_catalog_file"
                ],
                "procedure_catalog_sha256": promotion[
                    "procedure_catalog_sha256"
                ],
                "review_authority": {
                    "authority": promotion["authority"],
                    "authorized_by": promotion["authorized_by"],
                    "reviewer_ids": [promotion["reviewer_id"]],
                    "reviewer_kind": promotion["reviewer_kind"],
                },
            }
        )
        phase_reference.update(
            {
                "promotion_authority": promotion["authority"],
                "ontology_file": promotion["ontology_file"],
                "ontology_sha256": promotion["ontology_sha256"],
            }
        )
    manifest.setdefault("phase_reference_history", []).append(
        {
            "phase_reference": old_phase_reference,
            "phase_annotation": old_phase_annotation,
            "reason": (
                (
                    "Superseded by an explicit task-owner override that "
                    "promoted the authorized assistant v3 functional-state "
                    "Phase review; retained for provenance."
                )
                if promotion is not None
                else (
                    "Superseded by a create-only cross-case functional-state "
                    "Phase remediation; retained for provenance."
                )
            ),
        }
    )
    manifest.setdefault("notes", []).append(
        (
            "The task owner explicitly overrode the prior human provisional "
            "Phase decision and promoted the authorized assistant v3 "
            "functional-state review as canonical evaluation-only context. "
            "The prior human files and manifest remain archived."
        )
        if promotion is not None
        else (
            "The canonical provisional Phase reference was rebound "
            "create-only; all non-Phase source, interaction, mask, speech, "
            "and source-bag descriptors were preserved from the prior "
            "manifest."
        )
    )
    return manifest


def remediate(
    *,
    case_dir: Path,
    spec_path: Path,
    source_phase_path: Path,
    target_phase_path: Path,
    source_report_path: Path,
    target_report_path: Path,
    information_boundary_report_path: Path,
    point_schema_path: Path,
    audit_output_path: Path,
    manifest_path: Path,
    archive_manifest_path: Path,
    reuse_manifest_source_descriptors: bool = False,
) -> dict[str, Any]:
    case_dir = case_dir.resolve()
    case_id = case_dir.name
    target_phase_path = _require_new(
        target_phase_path,
        label="target Phase",
    )
    target_report_path = _require_new(
        target_report_path,
        label="target report",
    )
    audit_output_path = _require_new(
        audit_output_path,
        label="remediation audit",
    )
    archive_manifest_path = _require_new(
        archive_manifest_path,
        label="manifest archive",
    )

    spec = load_json(spec_path)
    promotion = _validated_explicit_user_override(
        spec=spec,
        case_dir=case_dir,
    )
    timeline = load_json(case_dir / "cam4_frame_timeline.v1.json")
    source_phases = load_jsonl(source_phase_path)
    source_report = load_json(source_report_path)
    point_schema = load_json(point_schema_path)
    if spec.get("schema") != SPEC_SCHEMA or spec.get("case_id") != case_id:
        raise RemediationError("spec schema/case_id mismatch")
    if timeline.get("case_id") != case_id or source_report.get("case_id") != case_id:
        raise RemediationError("timeline/report case_id mismatch")

    reviewed_at = str(spec.get("reviewed_at", ""))
    validate_reviewed_at_not_future(
        reviewed_at,
        context=f"{case_id} Phase remediation",
    )
    corrections = spec.get("corrections")
    if not isinstance(corrections, list) or not corrections:
        raise RemediationError("at least one correction is required")

    phases = copy.deepcopy(source_phases)
    by_phase = {str(item.get("phase_id")): item for item in phases}
    if len(by_phase) != len(phases):
        raise RemediationError("duplicate phase_id in source Phase reference")
    seen: set[str] = set()
    applied: list[dict[str, Any]] = []
    for correction in corrections:
        if not isinstance(correction, dict):
            raise RemediationError("correction object required")
        phase_id = str(correction.get("phase_id", ""))
        if phase_id in seen or phase_id not in by_phase:
            raise RemediationError(f"unknown or duplicate correction: {phase_id}")
        seen.add(phase_id)
        new_frame = int(correction["new_source_frame_idx"])
        notes = str(correction.get("review_notes", "")).strip()
        if not notes:
            raise RemediationError(f"{phase_id}: review_notes required")
        phase = by_phase[phase_id]
        old_frame = int(phase["source_frame_idx"])
        old_time = float(phase["time_sec"])
        new_time = _frame_time(timeline, new_frame)
        if new_frame == old_frame:
            raise RemediationError(f"{phase_id}: correction does not change frame")
        phase["source_frame_idx"] = new_frame
        phase["time_sec"] = new_time
        phase["review"]["reviewed_at"] = reviewed_at
        phase["review"]["notes"] = notes
        applied.append(
            {
                "phase_id": phase_id,
                "old_source_frame_idx": old_frame,
                "old_time_sec": old_time,
                "new_source_frame_idx": new_frame,
                "new_time_sec": new_time,
                "evidence": copy.deepcopy(correction.get("evidence", {})),
            }
        )

    if promotion is not None:
        proposal_phases = load_jsonl(promotion["source_proposal_path"])
        proposal_frames = {
            str(item.get("phase_id")): int(item["source_frame_idx"])
            for item in proposal_phases
        }
        target_frames = {
            str(item.get("phase_id")): int(item["source_frame_idx"])
            for item in phases
        }
        if (
            len(proposal_phases) != 4
            or proposal_frames != target_frames
        ):
            raise RemediationError(
                "explicit user override target does not match source proposal"
            )
        for phase in phases:
            phase_id = str(phase["phase_id"])
            phase["label_origin"] = promotion["label_origin"]
            phase["review"] = {
                "authorized_by": promotion["authorized_by"],
                "notes": promotion["phase_review_notes"][phase_id],
                "reviewed_at": reviewed_at,
                "reviewer_id": promotion["reviewer_id"],
                "reviewer_kind": promotion["reviewer_kind"],
            }

    _validate_phase_sequence(
        phases=phases,
        timeline=timeline,
        point_schema=point_schema,
    )
    target_phase_data = encode_jsonl(phases)
    target_report = _updated_report(
        source_report=source_report,
        source_report_path=source_report_path,
        source_phase_path=source_phase_path,
        target_phase_path=target_phase_path,
        target_phase_data=target_phase_data,
        phases=phases,
        corrections=applied,
        reviewed_at=reviewed_at,
        promotion=promotion,
    )
    target_report_data = encode_json(target_report)

    publish_create_only(
        {
            target_phase_path: target_phase_data,
            target_report_path: target_report_data,
        }
    )

    try:
        if reuse_manifest_source_descriptors:
            manifest = _updated_manifest_from_current(
                current_manifest=load_json(manifest_path),
                case_dir=case_dir,
                source_phase_path=source_phase_path,
                target_phase_path=target_phase_path,
                target_phase_sha256=_sha256_bytes(target_phase_data),
                phases=phases,
                source_report_path=source_report_path,
                target_report_path=target_report_path,
                target_report_sha256=_sha256_bytes(target_report_data),
                promotion=promotion,
            )
        else:
            manifest = build_manifest(
                case_dir=case_dir,
                report_path=target_report_path,
                information_boundary_report_path=information_boundary_report_path,
                phase_reference_path=target_phase_path,
            )
        manifest_data = encode_json(manifest)
        manifest_before = manifest_path.read_bytes()
        audit = {
            "schema": AUDIT_SCHEMA,
            "case_id": case_id,
            "authority": "deterministic_create_only_phase_remediation",
            "reviewed_at": reviewed_at,
            "source_phase_file": source_phase_path.name,
            "source_phase_sha256": sha256_file(source_phase_path),
            "target_phase_file": target_phase_path.name,
            "target_phase_sha256": _sha256_bytes(target_phase_data),
            "source_projection_report": str(source_report_path.resolve()),
            "source_projection_report_sha256": sha256_file(source_report_path),
            "target_projection_report": str(target_report_path.resolve()),
            "target_projection_report_sha256": _sha256_bytes(target_report_data),
            "manifest_sha256_before": _sha256_bytes(manifest_before),
            "manifest_sha256_after": _sha256_bytes(manifest_data),
            "corrections": applied,
            "information_boundary": "evaluation_only",
        }
        if promotion is not None:
            audit.update(
                {
                    "promotion_authority": promotion["authority"],
                    "authorized_by": promotion["authorized_by"],
                    "reviewer_kind": promotion["reviewer_kind"],
                    "reviewer_id": promotion["reviewer_id"],
                    "source_proposal_file": promotion[
                        "source_proposal_file"
                    ],
                    "source_proposal_sha256": promotion[
                        "source_proposal_sha256"
                    ],
                    "procedure_catalog_file": promotion[
                        "procedure_catalog_file"
                    ],
                    "procedure_catalog_sha256": promotion[
                        "procedure_catalog_sha256"
                    ],
                    "ontology_file": promotion["ontology_file"],
                    "ontology_sha256": promotion["ontology_sha256"],
                }
            )
        publish_create_only(
            {
                archive_manifest_path: manifest_before,
                audit_output_path: encode_json(audit),
            }
        )
    except Exception:
        target_phase_path.unlink(missing_ok=True)
        target_report_path.unlink(missing_ok=True)
        raise

    manifest_path.unlink()
    try:
        publish_create_only({manifest_path: manifest_data})
    except Exception:
        os.link(archive_manifest_path, manifest_path)
        raise

    return {
        "ok": True,
        "case_id": case_id,
        "target_phase": str(target_phase_path),
        "target_phase_sha256": _sha256_bytes(target_phase_data),
        "target_report": str(target_report_path),
        "target_report_sha256": _sha256_bytes(target_report_data),
        "manifest": str(manifest_path),
        "manifest_sha256": _sha256_bytes(manifest_data),
        "audit": str(audit_output_path),
        "corrections": applied,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case_dir", type=Path)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--source-phase", type=Path)
    parser.add_argument("--target-phase", type=Path)
    parser.add_argument("--source-report", type=Path)
    parser.add_argument("--target-report", type=Path)
    parser.add_argument("--information-boundary-report", type=Path)
    parser.add_argument("--point-schema", type=Path)
    parser.add_argument("--audit-output", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--archive-manifest", type=Path)
    parser.add_argument(
        "--reuse-manifest-source-descriptors",
        action="store_true",
        help=(
            "Rebind only Phase/report descriptors in the current validated "
            "manifest when immutable source-bag metadata is offline."
        ),
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    case_dir = args.case_dir.resolve()
    annotation_root = case_dir.parents[1]
    case_id = case_dir.name
    try:
        if args.information_boundary_report:
            information_boundary_report_path = (
                args.information_boundary_report.resolve()
            )
        else:
            current_manifest = load_json(case_dir / "annotation_manifest.json")
            boundary_reference = (
                current_manifest.get("evaluation_reference", {}).get(
                    "information_boundary_report_file"
                )
            )
            if not isinstance(boundary_reference, str) or not boundary_reference:
                raise RemediationError(
                    "current manifest information-boundary reference is missing"
                )
            information_boundary_report_path = (
                case_dir / boundary_reference
            ).resolve()
        result = remediate(
            case_dir=case_dir,
            spec_path=args.spec.resolve(),
            source_phase_path=(
                args.source_phase.resolve()
                if args.source_phase
                else case_dir / "phase_events.provisional.final.v1.jsonl"
            ),
            target_phase_path=(
                args.target_phase.resolve()
                if args.target_phase
                else case_dir / "phase_events.provisional.final.v2.jsonl"
            ),
            source_report_path=(
                args.source_report.resolve()
                if args.source_report
                else annotation_root
                / f"reports/{case_id}_assistant_dt_projection.final.v1.json"
            ),
            target_report_path=(
                args.target_report.resolve()
                if args.target_report
                else annotation_root
                / f"reports/{case_id}_assistant_dt_projection.final.v2.json"
            ),
            information_boundary_report_path=information_boundary_report_path,
            point_schema_path=(
                args.point_schema.resolve()
                if args.point_schema
                else annotation_root
                / "schema/observable_interaction_point.v1.schema.json"
            ),
            audit_output_path=(
                args.audit_output.resolve()
                if args.audit_output
                else case_dir / "phase_boundary_remediation.audit.v2.json"
            ),
            manifest_path=(
                args.manifest.resolve()
                if args.manifest
                else case_dir / "annotation_manifest.json"
            ),
            archive_manifest_path=(
                args.archive_manifest.resolve()
                if args.archive_manifest
                else case_dir
                / "audit_archive/phase_boundary_remediation_v2/"
                "annotation_manifest.before_phase_v2.json"
            ),
            reuse_manifest_source_descriptors=(
                args.reuse_manifest_source_descriptors
            ),
        )
    except (OSError, ValueError, jsonschema.ValidationError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
