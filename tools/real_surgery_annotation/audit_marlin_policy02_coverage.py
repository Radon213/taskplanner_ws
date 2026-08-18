#!/usr/bin/env python3
"""Audit actual Marlin Policy02 full-scan clip coverage across cases.

The main batch settings are not sufficient evidence of temporal coverage:
clips can be trimmed at observability gaps or the end of a video.  This audit
loads every completed main and supplemental scan record, validates it against
the canonical CAM4 timeline, unions the *actual* clip intervals per
observability segment, and fails if even one interval remains uncovered.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools.real_surgery_annotation.artifact_path_contract import (
    resolve_repo_artifact_identity,
)
from tools.real_surgery_annotation.build_policy02_review_index import (
    calculate_scan_coverage,
    load_json,
    load_jsonl,
    validate_timeline,
)
from tools.real_surgery_annotation.run_marlin2_proposals import (
    MODEL_QUERIES,
    MODEL_QUERY_POLICY_ID,
    atomic_create_text,
    canonical_json_sha256,
    sha256_file,
)


DEFAULT_CASES = tuple(f"0704_{index}" for index in range(7, 18))
EXPECTED_EVENT_TYPES = (
    "implicit_tool_request",
    "mayo_stand_to_scrub_nurse",
    "surgeon_to_scrub_nurse",
    "scrub_nurse_to_mayo_stand",
    "scrub_nurse_to_surgeon",
    "surgeon_to_mayo_stand",
)


def finite_number(value: Any, location: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ValueError(f"{location}: finite number required")
    return float(value)


def validate_run_report(
    *,
    case_id: str,
    proposal_path: Path,
    report_path: Path,
    timeline_path: Path,
    repo_root: Path,
) -> dict[str, Any]:
    report = load_json(report_path)
    errors: list[str] = []
    if report.get("schema") != "taskplanner.marlin2_proposal_run.v1":
        errors.append("unexpected report schema")
    if report.get("status") != "completed":
        errors.append("run status is not completed")
    if report.get("authority") != "proposal_only_not_ground_truth":
        errors.append("run is not proposal-only")
    if report.get("case_id") != case_id:
        errors.append("case_id mismatch")
    if report.get("phase_annotation_performed") is not False:
        errors.append("Marlin must not annotate Phase")
    proposal_sha256 = sha256_file(proposal_path)
    try:
        resolve_repo_artifact_identity(
            report.get("output"),
            expected_path=proposal_path,
            repo_root=repo_root,
            expected_sha256=report.get("output_sha256"),
            label="report output",
        )
    except ValueError as exc:
        errors.append(str(exc))
    if report.get("output_sha256") != proposal_sha256:
        errors.append("proposal SHA-256 mismatch")

    inputs = report.get("inputs", {})
    try:
        resolve_repo_artifact_identity(
            inputs.get("timeline"),
            expected_path=timeline_path,
            repo_root=repo_root,
            expected_sha256=inputs.get("timeline_sha256"),
            label="timeline",
        )
    except ValueError as exc:
        errors.append(str(exc))
    if inputs.get("timeline_sha256") != sha256_file(timeline_path):
        errors.append("timeline SHA-256 mismatch")
    anchor_value = inputs.get("anchors")
    anchor_path: Path | None = None
    if isinstance(anchor_value, str) and anchor_value:
        declared_anchor = Path(anchor_value)
        canonical_parent = Path(
            "annotations", "observable_tool_events", "cases", case_id
        )
        canonical_tail = (*canonical_parent.parts, declared_anchor.name)
        if declared_anchor.name and tuple(
            declared_anchor.parts[-len(canonical_tail) :]
        ) == canonical_tail:
            anchor_path = repo_root / canonical_parent / declared_anchor.name
    if anchor_path is None:
        errors.append("anchor path does not match the canonical case contract")
    else:
        try:
            resolve_repo_artifact_identity(
                anchor_value,
                expected_path=anchor_path,
                repo_root=repo_root,
                expected_sha256=inputs.get("anchors_sha256"),
                label="anchor",
            )
        except ValueError as exc:
            errors.append(str(exc))
    if (
        anchor_path is not None
        and anchor_path.is_file()
        and inputs.get("anchors_sha256") != sha256_file(anchor_path)
    ):
        errors.append("anchor SHA-256 mismatch")
    video_value = inputs.get("video")
    video_sha256 = inputs.get("video_sha256")
    if not isinstance(video_value, str) or not video_value:
        errors.append("source video path missing")
    if not isinstance(video_sha256, str) or len(video_sha256) != 64:
        errors.append("source video SHA-256 missing")

    settings = report.get("settings", {})
    if settings.get("query_policy_id") != MODEL_QUERY_POLICY_ID:
        errors.append("query policy mismatch")
    event_types = settings.get("event_types")
    if (
        not isinstance(event_types, list)
        or set(event_types) != set(EXPECTED_EVENT_TYPES)
        or len(event_types) != len(EXPECTED_EVENT_TYPES)
    ):
        errors.append("full-scan event type contract mismatch")
    expected_queries = {
        event_type: MODEL_QUERIES[event_type]
        for event_type in event_types
        if event_type in MODEL_QUERIES
    }
    if settings.get("queries") != expected_queries:
        errors.append("query text mismatch")
    if settings.get("query_prompt_sha256") != canonical_json_sha256(
        expected_queries
    ):
        errors.append("query prompt SHA-256 mismatch")
    if settings.get("skip_caption") is not True:
        errors.append("full-scan caption was not skipped")
    model = report.get("model", {})
    if not isinstance(model.get("revision"), str) or not model["revision"]:
        errors.append("model revision missing")
    if errors:
        raise ValueError(f"{report_path}: {'; '.join(errors)}")
    return {
        "proposal_file": str(proposal_path.resolve()),
        "proposal_file_sha256": proposal_sha256,
        "report_file": str(report_path.resolve()),
        "report_file_sha256": sha256_file(report_path),
        "model_id": model.get("id"),
        "model_revision": model.get("revision"),
        "anchor_file": anchor_value,
        "anchor_file_sha256": inputs.get("anchors_sha256"),
        "video_file": video_value,
        "video_file_sha256": video_sha256,
        "clip_before_sec": settings.get("clip_before_sec"),
        "clip_after_sec": settings.get("clip_after_sec"),
    }


def clip_intervals_from_records(
    *,
    case_id: str,
    proposal_path: Path,
    records: list[dict[str, Any]],
    segments: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    segment_by_id = {str(item["id"]): item for item in segments}
    intervals: list[dict[str, Any]] = []
    completed = 0
    skipped = 0
    for record in records:
        line = int(record["_source_line"])
        location = f"{proposal_path}:{line}"
        if record.get("schema") != "taskplanner.marlin2_anchor_evidence.v1":
            raise ValueError(f"{location}: unexpected record schema")
        if record.get("case_id") != case_id:
            raise ValueError(f"{location}: case_id mismatch")
        status = record.get("processing_status")
        if status == "skipped_anchor_inside_observability_gap":
            skipped += 1
            if record.get("clip") is not None:
                raise ValueError(f"{location}: skipped gap anchor has a clip")
            continue
        if status != "completed":
            raise ValueError(f"{location}: invalid processing status {status!r}")
        completed += 1
        clip = record.get("clip")
        if not isinstance(clip, dict):
            raise ValueError(f"{location}: completed record has no clip")
        segment_id = str(clip.get("observability_segment_id", ""))
        if segment_id not in segment_by_id:
            raise ValueError(f"{location}: unknown observability segment")
        start = finite_number(
            clip.get("start", {}).get("bag_time_sec"),
            f"{location}.clip.start",
        )
        end = finite_number(
            clip.get("end", {}).get("bag_time_sec"),
            f"{location}.clip.end",
        )
        segment = segment_by_id[segment_id]
        segment_start = float(segment["start_bag_time_sec"])
        segment_end = float(segment["end_bag_time_sec"])
        if (
            end < start
            or start < segment_start - 1e-9
            or end > segment_end + 1e-9
        ):
            raise ValueError(
                f"{location}: clip crosses observability boundary"
            )
        intervals.append(
            {
                "pass": "scan",
                "proposal_file": str(proposal_path.resolve()),
                "proposal_line": line,
                "anchor_id": record.get("anchor", {}).get("anchor_id"),
                "observability_segment_id": segment_id,
                "start_sec": start,
                "end_sec": end,
            }
        )
    return intervals, {
        "record_count": len(records),
        "completed_anchor_count": completed,
        "skipped_anchor_inside_gap_count": skipped,
    }


def audit_case(repo_root: Path, case_id: str) -> dict[str, Any]:
    annotation_root = repo_root / "annotations" / "observable_tool_events"
    case_dir = annotation_root / "cases" / case_id
    proposal_dir = annotation_root / "proposals"
    report_dir = annotation_root / "reports"
    timeline_path = case_dir / "cam4_frame_timeline.v1.json"
    result: dict[str, Any] = {
        "case_id": case_id,
        "ok": False,
        "errors": [],
    }
    try:
        timeline = load_json(timeline_path)
        timestamps, source_fps, segments, _ = validate_timeline(
            timeline,
            case_id=case_id,
        )
        proposal_paths = sorted(
            proposal_dir.glob(f"{case_id}_marlin2_scan*.policy02.v1.jsonl")
        )
        main_name = f"{case_id}_marlin2_scan.policy02.v1.jsonl"
        if not proposal_paths or not any(
            path.name == main_name for path in proposal_paths
        ):
            raise ValueError("canonical full-scan proposal is missing")

        run_records: list[dict[str, Any]] = []
        all_intervals: list[dict[str, Any]] = []
        aggregate_counts = {
            "record_count": 0,
            "completed_anchor_count": 0,
            "skipped_anchor_inside_gap_count": 0,
        }
        revisions: set[str] = set()
        for proposal_path in proposal_paths:
            report_path = report_dir / f"{proposal_path.stem}.json"
            run = validate_run_report(
                case_id=case_id,
                proposal_path=proposal_path,
                report_path=report_path,
                timeline_path=timeline_path,
                repo_root=repo_root,
            )
            revisions.add(str(run["model_revision"]))
            intervals, counts = clip_intervals_from_records(
                case_id=case_id,
                proposal_path=proposal_path,
                records=load_jsonl(proposal_path),
                segments=segments,
            )
            run["counts"] = counts
            run_records.append(run)
            all_intervals.extend(intervals)
            for key in aggregate_counts:
                aggregate_counts[key] += counts[key]
        if len(revisions) != 1:
            raise ValueError(
                f"scan runs use multiple model revisions: {sorted(revisions)}"
            )

        coverage = calculate_scan_coverage(
            scan_clip_intervals=all_intervals,
            scan_counts={
                "record_count": aggregate_counts["record_count"],
                "skipped_anchor_inside_gap_count": aggregate_counts[
                    "skipped_anchor_inside_gap_count"
                ],
            },
            segments=segments,
            gaps=timeline.get("gaps", []),
            timeline_start_sec=timestamps[0],
            timeline_end_sec=timestamps[-1],
        )
        uncovered = [
            {
                "observability_segment_id": segment[
                    "observability_segment_id"
                ],
                **interval,
            }
            for segment in coverage["segments"]
            for interval in segment["uncovered_intervals"]
        ]
        result.update(
            {
                "ok": (
                    not uncovered
                    and coverage["observable_coverage_ratio"] == 1.0
                ),
                "timeline": {
                    "file": str(timeline_path.relative_to(repo_root)),
                    "sha256": sha256_file(timeline_path),
                    "frame_count": len(timestamps),
                    "source_fps": source_fps,
                    "observability_segment_count": len(segments),
                    "gap_count": len(timeline.get("gaps", [])),
                },
                "model_revisions": sorted(revisions),
                "run_count": len(run_records),
                "runs": run_records,
                "counts": aggregate_counts,
                "coverage": coverage,
                "uncovered_intervals": uncovered,
            }
        )
        if not result["ok"]:
            result["errors"].append(
                "actual full-scan clip union does not cover every "
                "observable interval"
            )
    except Exception as exc:
        result["errors"].append(f"{type(exc).__name__}: {exc}")
    return result


def build_report(repo_root: Path, cases: tuple[str, ...]) -> dict[str, Any]:
    case_results = [audit_case(repo_root, case_id) for case_id in cases]
    return {
        "schema": "taskplanner.marlin2_policy02_coverage_audit.v1",
        "authority": "deterministic_read_only_validation",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "information_boundary": "proposal_evidence_only_not_ground_truth",
        "coverage_contract": (
            "The union of actual completed full-scan clip intervals, including "
            "create-only supplemental runs, must cover 100% of every canonical "
            "observable segment. Declared gaps are excluded and never inferred."
        ),
        "cases": case_results,
        "counts": {
            "case_count": len(case_results),
            "passed_case_count": sum(item["ok"] for item in case_results),
            "failed_case_count": sum(not item["ok"] for item in case_results),
            "scan_run_count": sum(
                int(item.get("run_count", 0)) for item in case_results
            ),
            "completed_anchor_count": sum(
                int(item.get("counts", {}).get("completed_anchor_count", 0))
                for item in case_results
            ),
        },
        "ok": all(item["ok"] for item in case_results),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--cases", nargs="+", default=list(DEFAULT_CASES))
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional create-only JSON report path.",
    )
    args = parser.parse_args()
    report = build_report(args.repo.resolve(), tuple(args.cases))
    payload = (
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    )
    if args.output is not None:
        output = (
            args.output
            if args.output.is_absolute()
            else args.repo.resolve() / args.output
        )
        atomic_create_text(output, payload)
    print(payload, end="")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
