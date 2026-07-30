#!/usr/bin/env python3
"""Create-only correction of evaluation-side voice, event, and interval roles."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import jsonschema

from .finalize_assistant_interaction_review import (
    validate_reviewed_at_not_future,
)
from .finalize_interaction_review import publish_create_only
from .publish_assistant_case_reference import (
    build_manifest,
    encode_json,
    load_json,
    load_jsonl,
    sha256_file,
    validate_evaluation_masks,
)


SPEC_SCHEMA = "taskplanner.voice_handover_target_remediation_spec.v1"
AUDIT_SCHEMA = "taskplanner.voice_handover_target_remediation_audit.v1"


class VoiceRoleRemediationError(ValueError):
    """The requested voice-role correction is invalid or unsafe."""


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _resolve_manifest_reference(
    *,
    case_dir: Path,
    manifest: dict[str, Any],
    dotted_keys: tuple[str, ...],
) -> Path:
    value: Any = manifest
    for key in dotted_keys:
        if not isinstance(value, dict):
            raise VoiceRoleRemediationError(
                f"manifest reference missing: {'.'.join(dotted_keys)}"
            )
        value = value.get(key)
    if not isinstance(value, str) or not value:
        raise VoiceRoleRemediationError(
            f"manifest reference missing: {'.'.join(dotted_keys)}"
        )
    path = (case_dir / value).resolve()
    if not path.is_file():
        raise VoiceRoleRemediationError(f"manifest reference not found: {path}")
    return path


def remediate(
    *,
    case_dir: Path,
    spec_path: Path,
    source_masks_path: Path,
    target_masks_path: Path,
    audit_output_path: Path,
    manifest_path: Path,
    archive_manifest_path: Path,
    report_path: Path,
    information_boundary_report_path: Path,
    phase_reference_path: Path,
    mask_schema_path: Path,
) -> dict[str, Any]:
    case_dir = case_dir.resolve()
    case_id = case_dir.name
    for label, path in (
        ("target masks", target_masks_path),
        ("audit", audit_output_path),
        ("manifest archive", archive_manifest_path),
    ):
        if path.resolve().exists():
            raise VoiceRoleRemediationError(
                f"{label} create-only target exists: {path.resolve()}"
            )

    spec = load_json(spec_path)
    masks = load_json(source_masks_path)
    timeline = load_json(case_dir / "cam4_frame_timeline.v1.json")
    observed = load_jsonl(
        case_dir / "interaction_events.observed.final.v1.jsonl"
    )
    phases = load_jsonl(phase_reference_path)
    voice = load_jsonl(case_dir / "voice_events.source.v2.jsonl")
    mask_schema = load_json(mask_schema_path)
    if (
        spec.get("schema") != SPEC_SCHEMA
        or spec.get("case_id") != case_id
        or masks.get("case_id") != case_id
    ):
        raise VoiceRoleRemediationError("spec/masks schema or case_id mismatch")
    reviewed_at = str(spec.get("reviewed_at", ""))
    validate_reviewed_at_not_future(
        reviewed_at,
        context=f"{case_id} voice-role remediation",
    )
    corrections = spec.get("corrections", [])
    event_role_corrections = spec.get("event_role_corrections", [])
    interval_mask_corrections = spec.get("interval_mask_corrections", [])
    if (
        not isinstance(corrections, list)
        or not isinstance(event_role_corrections, list)
        or not isinstance(interval_mask_corrections, list)
        or not (
            corrections
            or event_role_corrections
            or interval_mask_corrections
        )
    ):
        raise VoiceRoleRemediationError("at least one correction is required")

    revised = copy.deepcopy(masks)
    roles = revised.get("voice_context_roles")
    if not isinstance(roles, list):
        raise VoiceRoleRemediationError("voice_context_roles array required")
    by_id = {
        str(role.get("event_id")): role
        for role in roles
        if isinstance(role, dict)
    }
    if len(by_id) != len(roles):
        raise VoiceRoleRemediationError("duplicate/invalid voice role IDs")
    voice_ids = {str(event["event_id"]) for event in voice}
    seen: set[str] = set()
    applied: list[dict[str, Any]] = []
    for correction in corrections:
        if not isinstance(correction, dict):
            raise VoiceRoleRemediationError("correction object required")
        event_id = str(correction.get("event_id", ""))
        if (
            event_id in seen
            or event_id not in by_id
            or event_id not in voice_ids
        ):
            raise VoiceRoleRemediationError(
                f"unknown or duplicate voice correction: {event_id}"
            )
        seen.add(event_id)
        target = correction.get("handover_target")
        reason = str(correction.get("reason", "")).strip()
        if not isinstance(target, bool) or not reason:
            raise VoiceRoleRemediationError(
                f"{event_id}: boolean handover_target and reason required"
            )
        role = by_id[event_id]
        old_target = role.get("handover_target")
        old_reason = role.get("reason")
        if old_target is target and old_reason == reason:
            raise VoiceRoleRemediationError(
                f"{event_id}: correction does not change the role"
            )
        role["handover_target"] = target
        role["reason"] = reason
        applied.append(
            {
                "event_id": event_id,
                "old_handover_target": old_target,
                "new_handover_target": target,
                "old_reason": old_reason,
                "new_reason": reason,
                "evidence": copy.deepcopy(correction.get("evidence", {})),
            }
        )

    event_roles = revised.get("event_roles")
    if not isinstance(event_roles, list):
        raise VoiceRoleRemediationError("event_roles array required")
    event_roles_by_id = {
        str(role.get("event_id")): role
        for role in event_roles
        if isinstance(role, dict)
    }
    if len(event_roles_by_id) != len(event_roles):
        raise VoiceRoleRemediationError("duplicate/invalid event role IDs")
    applied_event_roles: list[dict[str, Any]] = []
    seen_event_roles: set[str] = set()
    for correction in event_role_corrections:
        if not isinstance(correction, dict):
            raise VoiceRoleRemediationError("event role correction object required")
        event_id = str(correction.get("event_id", ""))
        if event_id in seen_event_roles or event_id not in event_roles_by_id:
            raise VoiceRoleRemediationError(
                f"unknown or duplicate event role correction: {event_id}"
            )
        seen_event_roles.add(event_id)
        new_role = correction.get("role")
        new_eligibility = correction.get("metric_eligibility")
        new_reason = str(correction.get("reason", "")).strip()
        if (
            not isinstance(new_role, str)
            or not isinstance(new_eligibility, dict)
            or not new_reason
        ):
            raise VoiceRoleRemediationError(
                f"{event_id}: role, metric_eligibility, and reason required"
            )
        role = event_roles_by_id[event_id]
        old = copy.deepcopy(role)
        role["role"] = new_role
        role["metric_eligibility"] = copy.deepcopy(new_eligibility)
        role["reason"] = new_reason
        if role == old:
            raise VoiceRoleRemediationError(
                f"{event_id}: event role correction changes nothing"
            )
        applied_event_roles.append(
            {
                "event_id": event_id,
                "old": old,
                "new": copy.deepcopy(role),
                "evidence": copy.deepcopy(correction.get("evidence", {})),
            }
        )

    interval_masks = revised.get("interval_masks")
    if not isinstance(interval_masks, list):
        raise VoiceRoleRemediationError("interval_masks array required")
    interval_masks_by_id = {
        str(mask.get("mask_id")): mask
        for mask in interval_masks
        if isinstance(mask, dict)
    }
    if len(interval_masks_by_id) != len(interval_masks):
        raise VoiceRoleRemediationError("duplicate/invalid interval mask IDs")
    applied_interval_masks: list[dict[str, Any]] = []
    seen_interval_masks: set[str] = set()
    for correction in interval_mask_corrections:
        if not isinstance(correction, dict):
            raise VoiceRoleRemediationError(
                "interval mask correction object required"
            )
        mask_id = str(correction.get("mask_id", ""))
        if mask_id in seen_interval_masks or mask_id not in interval_masks_by_id:
            raise VoiceRoleRemediationError(
                f"unknown or duplicate interval mask correction: {mask_id}"
            )
        seen_interval_masks.add(mask_id)
        new_eligibility = correction.get("metric_eligibility")
        new_reason = str(correction.get("reason", "")).strip()
        if not isinstance(new_eligibility, dict) or not new_reason:
            raise VoiceRoleRemediationError(
                f"{mask_id}: metric_eligibility and reason required"
            )
        mask = interval_masks_by_id[mask_id]
        old = copy.deepcopy(mask)
        mask["metric_eligibility"] = copy.deepcopy(new_eligibility)
        mask["reason"] = new_reason
        if mask == old:
            raise VoiceRoleRemediationError(
                f"{mask_id}: interval mask correction changes nothing"
            )
        applied_interval_masks.append(
            {
                "mask_id": mask_id,
                "old": old,
                "new": copy.deepcopy(mask),
                "evidence": copy.deepcopy(correction.get("evidence", {})),
            }
        )

    try:
        jsonschema.validate(revised, mask_schema)
    except jsonschema.ValidationError as exc:
        raise VoiceRoleRemediationError(
            f"revised mask schema error: {exc.message}"
        ) from exc
    validate_evaluation_masks(
        masks=revised,
        mask_schema=mask_schema,
        case_id=case_id,
        observed=observed,
        phases=phases,
        voice=voice,
        timeline=timeline,
    )
    target_masks_data = encode_json(revised)
    publish_create_only({target_masks_path.resolve(): target_masks_data})

    try:
        manifest = build_manifest(
            case_dir=case_dir,
            report_path=report_path,
            information_boundary_report_path=information_boundary_report_path,
            phase_reference_path=phase_reference_path,
            evaluation_masks_path=target_masks_path,
        )
        manifest_data = encode_json(manifest)
        manifest_before = manifest_path.read_bytes()
        audit = {
            "schema": AUDIT_SCHEMA,
            "case_id": case_id,
            "authority": "user_authorized_codex_voice_visual_reconciliation",
            "reviewed_at": reviewed_at,
            "source_masks_file": source_masks_path.name,
            "source_masks_sha256": sha256_file(source_masks_path),
            "target_masks_file": target_masks_path.name,
            "target_masks_sha256": _sha256_bytes(target_masks_data),
            "manifest_sha256_before": _sha256_bytes(manifest_before),
            "manifest_sha256_after": _sha256_bytes(manifest_data),
            "corrections": applied,
            "event_role_corrections": applied_event_roles,
            "interval_mask_corrections": applied_interval_masks,
            "information_boundary": "evaluation_only",
        }
        publish_create_only(
            {
                archive_manifest_path.resolve(): manifest_before,
                audit_output_path.resolve(): encode_json(audit),
            }
        )
    except Exception:
        target_masks_path.unlink(missing_ok=True)
        raise

    manifest_path.unlink()
    try:
        publish_create_only({manifest_path.resolve(): manifest_data})
    except Exception:
        manifest_path.unlink(missing_ok=True)
        os.link(archive_manifest_path.resolve(), manifest_path.resolve())
        try:
            descriptor = os.open(manifest_path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            pass
        raise
    return {
        "ok": True,
        "case_id": case_id,
        "target_masks": str(target_masks_path.resolve()),
        "target_masks_sha256": _sha256_bytes(target_masks_data),
        "manifest": str(manifest_path.resolve()),
        "manifest_sha256": _sha256_bytes(manifest_data),
        "audit": str(audit_output_path.resolve()),
        "corrections": applied,
        "event_role_corrections": applied_event_roles,
        "interval_mask_corrections": applied_interval_masks,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case_dir", type=Path)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--source-masks", type=Path)
    parser.add_argument("--target-masks", type=Path)
    parser.add_argument("--audit-output", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--archive-manifest", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--information-boundary-report", type=Path)
    parser.add_argument("--phase-reference", type=Path)
    parser.add_argument("--mask-schema", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    case_dir = args.case_dir.resolve()
    annotation_root = case_dir.parents[1]
    manifest_path = (
        args.manifest.resolve()
        if args.manifest
        else case_dir / "annotation_manifest.json"
    )
    try:
        current_manifest = load_json(manifest_path)
        source_masks_path = (
            args.source_masks.resolve()
            if args.source_masks
            else _resolve_manifest_reference(
                case_dir=case_dir,
                manifest=current_manifest,
                dotted_keys=(
                    "evaluation_reference",
                    "evaluation_masks",
                    "file",
                ),
            )
        )
        report_path = (
            args.report.resolve()
            if args.report
            else _resolve_manifest_reference(
                case_dir=case_dir,
                manifest=current_manifest,
                dotted_keys=(
                    "evaluation_reference",
                    "projection_report_file",
                ),
            )
        )
        boundary_path = (
            args.information_boundary_report.resolve()
            if args.information_boundary_report
            else _resolve_manifest_reference(
                case_dir=case_dir,
                manifest=current_manifest,
                dotted_keys=(
                    "evaluation_reference",
                    "information_boundary_report_file",
                ),
            )
        )
        phase_path = (
            args.phase_reference.resolve()
            if args.phase_reference
            else _resolve_manifest_reference(
                case_dir=case_dir,
                manifest=current_manifest,
                dotted_keys=(
                    "evaluation_reference",
                    "phase_reference",
                    "file",
                ),
            )
        )
        result = remediate(
            case_dir=case_dir,
            spec_path=args.spec.resolve(),
            source_masks_path=source_masks_path,
            target_masks_path=(
                args.target_masks.resolve()
                if args.target_masks
                else case_dir / "evaluation_masks.v2.json"
            ),
            audit_output_path=(
                args.audit_output.resolve()
                if args.audit_output
                else case_dir
                / "voice_handover_target_remediation.audit.v2.json"
            ),
            manifest_path=manifest_path,
            archive_manifest_path=(
                args.archive_manifest.resolve()
                if args.archive_manifest
                else case_dir
                / "audit_archive/voice_handover_target_remediation_v2/"
                "annotation_manifest.before_voice_role_v2.json"
            ),
            report_path=report_path,
            information_boundary_report_path=boundary_path,
            phase_reference_path=phase_path,
            mask_schema_path=(
                args.mask_schema.resolve()
                if args.mask_schema
                else annotation_root / "schema/evaluation_masks.v1.schema.json"
            ),
        )
    except (OSError, ValueError, KeyError, jsonschema.ValidationError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
