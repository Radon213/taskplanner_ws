#!/usr/bin/env python3
"""Repair future-dated assistant review provenance without losing old bytes.

This is a narrowly scoped recovery tool for an already-published case whose
event geometry is valid but whose assistant `reviewed_at` metadata was written
with a future wall-clock time.  It archives every affected canonical artifact,
changes only the review timestamp in the authoritative adjudication and Phase
context, updates their explicit hash bindings, and then regenerates the
observed, DT, Phase, projection-report, and manifest layers through the normal
strict finalizer/publisher.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from tools.real_surgery_annotation.finalize_assistant_interaction_review import (
    finalize,
)
from tools.real_surgery_annotation.finalize_interaction_review import (
    encode_json,
    encode_jsonl,
    publish_create_only,
    sha256_file,
)
from tools.real_surgery_annotation.interaction_review_gui import (
    FinalReviewBundle,
)
from tools.real_surgery_annotation.publish_assistant_case_reference import (
    build_manifest,
    encode_json as encode_manifest_json,
)
from tools.real_surgery_annotation.run_marlin2_proposals import (
    atomic_create_text,
)


SEOUL = ZoneInfo("Asia/Seoul")
REASON = "future_reviewed_at_metadata_only_no_event_geometry_change"


def load_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: JSON object required")
    return value


def load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        1,
    ):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number}: JSON object required")
        rows.append(value)
    return rows


def replace_future_reviewed_at(
    records: list[dict],
    *,
    reviewed_at: str,
    now_utc: datetime,
    label: str,
) -> tuple[list[dict], list[str]]:
    old_values: set[str] = set()
    changed = 0
    output = json.loads(json.dumps(records, ensure_ascii=False))
    for index, record in enumerate(output, 1):
        review = record.get("review")
        if not isinstance(review, dict):
            raise ValueError(f"{label}:{index}: review object missing")
        value = review.get("reviewed_at")
        if not isinstance(value, str):
            raise ValueError(f"{label}:{index}: reviewed_at missing")
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError(f"{label}:{index}: reviewed_at timezone missing")
        if parsed.astimezone(timezone.utc) <= now_utc:
            raise ValueError(
                f"{label}:{index}: reviewed_at is not future-dated: {value}"
            )
        old_values.add(value)
        review["reviewed_at"] = reviewed_at
        changed += 1
    if changed != len(records) or not records:
        raise ValueError(f"{label}: expected every non-empty row to change")
    return output, sorted(old_values)


def archive_originals(
    files: dict[str, Path],
    *,
    archive_dir: Path,
) -> dict[str, dict[str, str]]:
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive: dict[str, dict[str, str]] = {}
    payloads: dict[Path, bytes] = {}
    for label, path in files.items():
        if not path.is_file():
            raise ValueError(f"{label}: canonical artifact missing: {path}")
        archived_path = archive_dir / path.name
        data = path.read_bytes()
        if archived_path.exists():
            if archived_path.read_bytes() != data:
                raise ValueError(
                    f"{label}: archive path exists with different bytes"
                )
        else:
            payloads[archived_path] = data
        archive[label] = {
            "canonical_path": str(path),
            "archived_path": str(archived_path),
            "sha256": sha256_file(path),
        }
    if payloads:
        publish_create_only(payloads)
    return archive


def restore_originals(
    files: dict[str, Path],
    originals: dict[Path, bytes],
) -> None:
    for path in files.values():
        path.unlink(missing_ok=True)
    publish_create_only(originals)


def repair_case(case_dir: Path) -> dict:
    case_dir = case_dir.resolve()
    annotation_root = case_dir.parents[1]
    report_dir = annotation_root / "reports"
    case_id = case_dir.name
    paths = {
        "adjudications": (
            case_dir / "assistant_interaction_adjudications.final.v1.jsonl"
        ),
        "phase_context": (
            case_dir / "phase_context.assistant_adjudicated.v1.jsonl"
        ),
        "projection": case_dir / "dt_projection.explicit.v1.json",
        "reconciliation": (
            case_dir / "policy02_reconciliation_audit.final.v1.json"
        ),
        "observed": (
            case_dir / "interaction_events.observed.final.v1.jsonl"
        ),
        "dt_reference": (
            case_dir / "interaction_events.dt_reference.final.v1.jsonl"
        ),
        "phase_reference": (
            case_dir / "phase_events.provisional.final.v1.jsonl"
        ),
        "projection_report": (
            report_dir / f"{case_id}_assistant_dt_projection.final.v1.json"
        ),
        "manifest": case_dir / "annotation_manifest.json",
    }
    originals = {path: path.read_bytes() for path in paths.values()}
    archive_dir = (
        case_dir
        / "audit_archive"
        / "future_reviewed_at_provenance_error_v1"
    )
    archive = archive_originals(paths, archive_dir=archive_dir)

    now = datetime.now(SEOUL)
    now_utc = now.astimezone(timezone.utc)
    reviewed_at = now.isoformat(timespec="seconds")
    adjudications, adjudication_old_values = replace_future_reviewed_at(
        load_jsonl(paths["adjudications"]),
        reviewed_at=reviewed_at,
        now_utc=now_utc,
        label="adjudications",
    )
    phase_context, phase_old_values = replace_future_reviewed_at(
        load_jsonl(paths["phase_context"]),
        reviewed_at=reviewed_at,
        now_utc=now_utc,
        label="phase_context",
    )
    adjudication_data = encode_jsonl(adjudications)
    phase_context_data = encode_jsonl(phase_context)

    projection = load_json(paths["projection"])
    source = projection.get("source_adjudications")
    if not isinstance(source, dict):
        raise ValueError("projection source_adjudications missing")
    if source.get("sha256") != archive["adjudications"]["sha256"]:
        raise ValueError("projection old adjudication hash mismatch")
    source["sha256"] = hashlib.sha256(adjudication_data).hexdigest()
    projection_data = encode_json(projection)
    projection_sha256 = hashlib.sha256(projection_data).hexdigest()

    reconciliation = load_json(paths["reconciliation"])
    materialization = reconciliation.get("materialization")
    if not isinstance(materialization, dict):
        raise ValueError("reconciliation materialization missing")
    if (
        materialization.get("adjudication_sha256")
        != archive["adjudications"]["sha256"]
        or materialization.get("projection_sha256")
        != archive["projection"]["sha256"]
    ):
        raise ValueError("reconciliation old hash bindings mismatch")
    materialization["adjudication_sha256"] = source["sha256"]
    materialization["projection_sha256"] = projection_sha256
    reconciliation["provenance_repair"] = {
        "reason": REASON,
        "repaired_at": reviewed_at,
        "old_adjudication_reviewed_at_values": adjudication_old_values,
        "old_phase_reviewed_at_values": phase_old_values,
        "event_geometry_changed": False,
        "archived_original_directory": str(archive_dir),
    }
    reconciliation_data = encode_json(reconciliation)

    inputs = {
        paths["adjudications"]: adjudication_data,
        paths["phase_context"]: phase_context_data,
        paths["projection"]: projection_data,
        paths["reconciliation"]: reconciliation_data,
    }
    derived_labels = (
        "observed",
        "dt_reference",
        "phase_reference",
        "projection_report",
        "manifest",
    )
    try:
        for path in paths.values():
            path.unlink(missing_ok=True)
        publish_create_only(inputs)
        finalize(
            case_dir=case_dir,
            adjudications_path=paths["adjudications"],
            projection_path=paths["projection"],
            timeline_path=case_dir / "cam4_frame_timeline.v1.json",
            adjudication_schema_path=(
                annotation_root
                / "schema/assistant_interaction_adjudication.v1.schema.json"
            ),
            projection_schema_path=(
                annotation_root
                / "schema/explicit_dt_interaction_projection.v1.schema.json"
            ),
            point_schema_path=(
                annotation_root
                / "schema/observable_interaction_point.v1.schema.json"
            ),
            interval_schema_path=(
                annotation_root
                / "schema/observable_interaction_interval.v1.schema.json"
            ),
            tools_path=annotation_root / "catalogs/tools.yaml",
            observed_output_path=paths["observed"],
            dt_output_path=paths["dt_reference"],
            phase_context_path=paths["phase_context"],
            phase_output_path=paths["phase_reference"],
            report_output_path=paths["projection_report"],
        )
        manifest = build_manifest(
            case_dir=case_dir,
            report_path=paths["projection_report"],
            information_boundary_report_path=(
                report_dir / "information_boundary.final.v2.json"
            ),
        )
        publish_create_only(
            {paths["manifest"]: encode_manifest_json(manifest)}
        )
        bundle = FinalReviewBundle(manifest_path=paths["manifest"])
        for event in [*bundle.observed, *bundle.phase_events]:
            value = event.get("review", {}).get("reviewed_at")
            if value != reviewed_at:
                raise ValueError(
                    f"{event.get('event_id')}: repaired timestamp mismatch"
                )
    except Exception:
        for label in derived_labels:
            paths[label].unlink(missing_ok=True)
        for path in inputs:
            path.unlink(missing_ok=True)
        restore_originals(paths, originals)
        raise

    after = {
        label: {
            "path": str(path),
            "sha256": sha256_file(path),
        }
        for label, path in paths.items()
    }
    return {
        "schema": "taskplanner.review_provenance_repair_audit.v1",
        "case_id": case_id,
        "ok": True,
        "reason": REASON,
        "reviewed_at": reviewed_at,
        "event_geometry_changed": False,
        "old_reviewed_at_values": {
            "adjudications": adjudication_old_values,
            "phase_context": phase_old_values,
        },
        "archive": archive,
        "after": after,
        "bundle_counts": {
            "observed": len(bundle.observed),
            "dt_reference": len(bundle.dt_reference),
            "phase": len(bundle.phase_events),
            "voice": len(bundle.speech_events),
        },
        "bundle_revision": bundle.revision,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case_dir", type=Path)
    parser.add_argument("--audit-output", type=Path)
    args = parser.parse_args()
    try:
        report = repair_case(args.case_dir)
        output = (
            args.audit_output.resolve()
            if args.audit_output
            else args.case_dir.resolve()
            / "provenance_repair.future_reviewed_at.final.v1.json"
        )
        atomic_create_text(
            output,
            json.dumps(
                report,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
