#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

from .event_model import (
    canonical_json,
    load_jsonl,
    load_yaml,
    sha256_file,
    strip_internal_fields,
)
from .validate_annotations import validate_records


REPORT_SCHEMA = "taskplanner.candidate_proposal_merge_report.v1"


class CandidateMergeError(ValueError):
    """Raised when candidate inputs cannot be merged without ambiguity."""

    def __init__(self, errors: Sequence[str]):
        self.errors = list(errors)
        super().__init__("candidate proposal merge failed:\n" + "\n".join(self.errors))


def _record_location(path: Path, record: dict[str, Any]) -> str:
    line = record.get("_jsonl_line")
    return f"{path}:{line}" if line is not None else str(path)


def _numeric_sort_value(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return float("inf")
    return float(value)


def _candidate_sort_key(record: dict[str, Any]) -> tuple[float, float, float, str]:
    return (
        _numeric_sort_value(record.get("time_sec")),
        _numeric_sort_value(record.get("candidate_start_sec")),
        _numeric_sort_value(record.get("candidate_end_sec")),
        str(record.get("event_id", "")),
    )


def merge_candidate_files(
    *,
    input_paths: Sequence[Path],
    schema: dict[str, Any],
    tool_catalog: dict[str, Any],
    case_id: str,
    duration_sec: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load, deduplicate, sort, and validate non-authoritative proposals."""
    if not input_paths:
        raise CandidateMergeError(["at least one input JSONL file is required"])
    if duration_sec < 0:
        raise CandidateMergeError(["duration_sec must be non-negative"])

    merged: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    seen_event_ids: dict[str, tuple[str, str]] = {}
    merge_errors: list[str] = []

    for input_path in input_paths:
        records = load_jsonl(input_path)
        sources.append(
            {
                "path": str(input_path),
                "sha256": sha256_file(input_path),
                "candidate_count": len(records),
            }
        )
        for record in records:
            location = _record_location(input_path, record)
            public_record = strip_internal_fields(record)
            event_id = public_record.get("event_id")
            if isinstance(event_id, str):
                fingerprint = canonical_json(public_record)
                previous = seen_event_ids.get(event_id)
                if previous is not None:
                    previous_fingerprint, previous_location = previous
                    kind = (
                        "duplicate"
                        if fingerprint == previous_fingerprint
                        else "conflicting"
                    )
                    merge_errors.append(
                        f"{location}: {kind} event_id {event_id!r}; "
                        f"first seen at {previous_location}"
                    )
                    continue
                seen_event_ids[event_id] = (fingerprint, location)

            if public_record.get("review_status") != "proposed":
                merge_errors.append(
                    f"{location}: candidate review_status must be 'proposed'; "
                    f"got {public_record.get('review_status')!r}"
                )
            record["_jsonl_source"] = str(input_path)
            merged.append(record)

    if merge_errors:
        raise CandidateMergeError(merge_errors)

    merged.sort(key=_candidate_sort_key)
    validation_errors = validate_records(
        merged,
        schema=schema,
        tool_catalog=tool_catalog,
        case_id=case_id,
        duration_sec=duration_sec,
    )
    if validation_errors:
        raise CandidateMergeError(
            [f"schema/catalog validation: {error}" for error in validation_errors]
        )

    public_records = [strip_internal_fields(record) for record in merged]
    status_counts = Counter(record["review_status"] for record in public_records)
    ground_truth_event_count = sum(
        status_counts[status] for status in ("confirmed", "ambiguous")
    )
    if ground_truth_event_count:
        raise CandidateMergeError(
            [
                "merged candidate output unexpectedly contains authoritative "
                f"records: {ground_truth_event_count}"
            ]
        )

    report = {
        "schema": REPORT_SCHEMA,
        "case_id": case_id,
        "ok": True,
        "input_files": sources,
        "input_candidate_count": sum(item["candidate_count"] for item in sources),
        "merged_candidate_count": len(public_records),
        "review_status_counts": dict(sorted(status_counts.items())),
        "ground_truth_event_count": 0,
        "human_review_required": True,
        "all_candidates_remain_proposed": True,
        "sort_order": [
            "time_sec",
            "candidate_start_sec",
            "candidate_end_sec",
            "event_id",
        ],
        "errors": [],
    }
    return public_records, report


def _ensure_new_destinations(output_path: Path, report_path: Path) -> None:
    if output_path.resolve() == report_path.resolve():
        raise FileExistsError("output and report paths must be different")
    existing = [path for path in (output_path, report_path) if path.exists()]
    if existing:
        rendered = ", ".join(str(path) for path in existing)
        raise FileExistsError(f"refusing to overwrite existing file(s): {rendered}")


def write_new_outputs(
    *,
    output_path: Path,
    report_path: Path,
    records: Sequence[dict[str, Any]],
    report: dict[str, Any],
) -> None:
    """Create output and report together, never replacing an existing path."""
    _ensure_new_destinations(output_path, report_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    output_text = "".join(canonical_json(record) + "\n" for record in records)
    report_text = (
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    created: list[Path] = []
    try:
        with output_path.open("x", encoding="utf-8") as stream:
            created.append(output_path)
            stream.write(output_text)
        with report_path.open("x", encoding="utf-8") as stream:
            created.append(report_path)
            stream.write(report_text)
    except Exception:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        raise


def _failure_report(message: str, case_id: str | None) -> dict[str, Any]:
    return {
        "schema": REPORT_SCHEMA,
        "case_id": case_id,
        "ok": False,
        "ground_truth_event_count": 0,
        "human_review_required": True,
        "errors": [message],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Merge candidate proposal JSONL files without promoting any record "
            "to ground truth."
        )
    )
    parser.add_argument(
        "--input",
        dest="inputs",
        action="append",
        type=Path,
        required=True,
        help="Input candidate JSONL; repeat for each source.",
    )
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--duration-sec", type=float, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--tools", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        _ensure_new_destinations(args.output, args.report)
        schema = json.loads(args.schema.read_text(encoding="utf-8"))
        tool_catalog = load_yaml(args.tools)
        records, report = merge_candidate_files(
            input_paths=args.inputs,
            schema=schema,
            tool_catalog=tool_catalog,
            case_id=args.case_id,
            duration_sec=args.duration_sec,
        )
        write_new_outputs(
            output_path=args.output,
            report_path=args.report,
            records=records,
            report=report,
        )
    except (CandidateMergeError, FileExistsError, OSError, ValueError) as exc:
        print(
            json.dumps(
                _failure_report(str(exc), args.case_id),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1

    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
