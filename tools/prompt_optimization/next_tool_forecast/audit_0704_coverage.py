#!/usr/bin/env python3
"""Audit 0704_5--17 before any next-tool prompt evaluation.

This is deliberately an audit, not a label builder.  A case with an absent or
incomplete evaluation reference is reported as excluded and is never inferred
to be a no-handover (negative) example.  Generated reports remain under this
task's ignored ``runs/`` directory and do not alter production code or data.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

from build_eval_manifest import (
    AUDIT_CASES,
    BENCHMARK_CASES,
    BenchmarkError,
    REPO_ROOT,
    RUNS_ROOT,
    TOOL_ID_SET,
    canonical_json,
    media_binding,
    read_json,
    read_jsonl,
    resolve_bound_file,
    sha256_file,
    temporal_split_metadata,
    validate_timeline,
)


AUDIT_SCHEMA = "taskplanner.next_tool_forecast_coverage_audit.v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--proxy-root",
        type=Path,
        default=Path.home() / ".cache/taskplanner_annotation",
    )
    parser.add_argument("--verify-proxy-sha256", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def ensure_safe_output_dir(path: Path) -> Path:
    output_dir = path.resolve()
    runs_root = RUNS_ROOT.resolve()
    try:
        output_dir.relative_to(runs_root)
    except ValueError as exc:
        raise BenchmarkError(f"output directory must be under {runs_root}") from exc
    if output_dir == runs_root:
        raise BenchmarkError("output directory must be a run subdirectory")
    return output_dir


def legacy_artifacts(case_dir: Path) -> dict[str, Any]:
    """Describe old files without treating them as a usable evaluation source."""

    records: dict[str, Any] = {}
    for filename in ("tool_events.final.v1.jsonl", "tool_events.v1.jsonl"):
        path = case_dir / filename
        if not path.is_file():
            continue
        try:
            rows = read_jsonl(path)
            records[filename] = {"rows": len(rows), "sha256": sha256_file(path)}
        except BenchmarkError as exc:
            records[filename] = {"read_error": str(exc)}
    return records


def archival_capture_candidates(case_id: str) -> list[dict[str, Any]]:
    """Inventory archived MCAPs without treating them as causal evaluation media."""

    root = REPO_ROOT / "annotated_bags"
    candidates = []
    for path in sorted(root.glob(f"{case_id}*/*.mcap")):
        if path.is_file():
            candidates.append(
                {
                    "path": str(path.relative_to(REPO_ROOT)),
                    "size_bytes": path.stat().st_size,
                }
            )
    return candidates


def proxy_coverage(case_id: str, proxy_root: Path) -> dict[str, Any]:
    manifest_path = proxy_root / case_id / "review_multiview.manifest.json"
    if not manifest_path.is_file():
        return {"proxy_manifest_exists": False, "paired_flir_cam4_available": False}
    try:
        manifest = read_json(manifest_path)
        outputs = manifest.get("outputs")
        if not isinstance(outputs, dict):
            return {"proxy_manifest_exists": True, "paired_flir_cam4_available": False}
        available = []
        for view in ("flir", "cam4"):
            descriptor = outputs.get(view)
            path = Path(str(descriptor.get("path", ""))) if isinstance(descriptor, dict) else Path()
            if path.is_file():
                available.append(view)
        return {
            "proxy_manifest_exists": True,
            "paired_flir_cam4_available": set(available) == {"flir", "cam4"},
            "available_views": available,
        }
    except BenchmarkError:
        return {"proxy_manifest_exists": True, "paired_flir_cam4_available": False}


def incomplete_report(
    case_id: str,
    reasons: list[str],
    legacy: Mapping[str, Any],
    proxy_status: Mapping[str, Any],
    archive_candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "status": "excluded_incomplete_causal_benchmark",
        "reasons": reasons,
        "negative_policy": "not used as a negative or positive benchmark example",
        "legacy_artifacts": dict(legacy),
        "proxy_coverage": dict(proxy_status),
        "archival_capture_candidates": archive_candidates,
    }


def audit_case(case_id: str, proxy_root: Path, verify_proxy_sha256: bool) -> dict[str, Any]:
    case_dir = REPO_ROOT / "annotations/observable_tool_events/cases" / case_id
    legacy = legacy_artifacts(case_dir)
    proxy_status = proxy_coverage(case_id, proxy_root)
    archive_candidates = archival_capture_candidates(case_id)
    manifest_path = case_dir / "annotation_manifest.json"
    if not manifest_path.is_file():
        return incomplete_report(case_id, ["annotation_manifest_missing"], legacy, proxy_status, archive_candidates)
    try:
        manifest = read_json(manifest_path)
    except BenchmarkError as exc:
        return incomplete_report(case_id, [f"annotation_manifest_invalid:{exc}"], legacy, proxy_status, archive_candidates)
    reasons: list[str] = []
    if manifest.get("case_id") != case_id:
        reasons.append("annotation_manifest_case_mismatch")
    evaluation = manifest.get("evaluation_reference")
    minimal = manifest.get("minimal_interaction_annotation")
    speech = manifest.get("speech_timeline")
    if not isinstance(evaluation, dict) or evaluation.get("complete") is not True:
        reasons.append("complete_evaluation_reference_missing")
    elif not str(evaluation.get("information_boundary", "")).startswith("evaluation_only"):
        reasons.append("evaluation_reference_not_evaluation_only")
    if not isinstance(minimal, dict):
        reasons.append("causal_frame_timeline_binding_missing")
    if not isinstance(speech, dict):
        reasons.append("causal_public_asr_binding_missing")
    if reasons:
        if not bool(proxy_status.get("paired_flir_cam4_available")):
            reasons.append("paired_flir_cam4_proxy_missing")
        return incomplete_report(case_id, reasons, legacy, proxy_status, archive_candidates)

    assert isinstance(evaluation, dict)
    assert isinstance(minimal, dict)
    assert isinstance(speech, dict)
    observed = evaluation.get("observed_reference")
    if not isinstance(observed, dict):
        return incomplete_report(case_id, ["observed_evaluation_reference_missing"], legacy, proxy_status, archive_candidates)
    try:
        observed_path, observed_sha256 = resolve_bound_file(
            base=case_dir,
            relative=observed.get("file"),
            expected_sha256=observed.get("sha256"),
            label=f"{case_id} observed GT",
        )
        timeline_path, timeline_sha256 = resolve_bound_file(
            base=case_dir,
            relative=minimal.get("timeline_file"),
            expected_sha256=minimal.get("timeline_sha256"),
            label=f"{case_id} frame timeline",
        )
        voice_path, voice_sha256 = resolve_bound_file(
            base=case_dir,
            relative=speech.get("file"),
            expected_sha256=speech.get("sha256"),
            label=f"{case_id} public ASR",
        )
        timeline, gaps = validate_timeline(case_id, read_json(timeline_path))
        media = media_binding(
            case_id=case_id,
            proxy_root=proxy_root,
            frame_count=len(timeline),
            verify_sha256=verify_proxy_sha256,
        )
        events = read_jsonl(observed_path)
        voices = read_jsonl(voice_path)
    except BenchmarkError as exc:
        return incomplete_report(case_id, [f"bound_source_or_media_invalid:{exc}"], legacy, proxy_status, archive_candidates)

    confirmed = [row for row in events if row.get("review_status") == "confirmed"]
    confirmed_transfers = [
        row
        for row in confirmed
        if row.get("event_type") == "tool_transfer"
        and row.get("from") == "scrub_nurse"
        and row.get("to") == "surgeon"
    ]
    scoreable_transfers = [
        row for row in confirmed_transfers if str(row.get("tool", "")) in TOOL_ID_SET
    ]
    unresolved_transfers = [row for row in confirmed_transfers if row not in scoreable_transfers]
    return {
        "case_id": case_id,
        "status": "eligible_causal_benchmark",
        "negative_policy": "negatives may be derived only from this complete reference",
        "frame_count": len(timeline),
        "timeline_time_range_sec": [round(timeline[0], 9), round(timeline[-1], 9)],
        "timeline_gap_count": len(gaps),
        "public_asr_rows": len(voices),
        "event_counts": {
            "all_observed": len(events),
            "confirmed": len(confirmed),
            "confirmed_by_type": dict(sorted(Counter(str(row.get("event_type", "")) for row in confirmed).items())),
            "confirmed_scrub_to_surgeon_transfers": len(confirmed_transfers),
            "scoreable_scrub_to_surgeon_transfers": len(scoreable_transfers),
            "unresolved_or_unsupported_transfer_count": len(unresolved_transfers),
            "scoreable_transfer_by_tool": dict(
                sorted(Counter(str(row.get("tool", "")) for row in scoreable_transfers).items())
            ),
        },
        "temporal_partition": (
            None if case_id not in BENCHMARK_CASES or int(case_id.removeprefix("0704_")) >= 15
            else temporal_split_metadata(timeline)
        ),
        "source_hashes": {
            "annotation_manifest": sha256_file(manifest_path),
            "observed_reference": observed_sha256,
            "timeline": timeline_sha256,
            "public_asr": voice_sha256,
            "proxy_manifest": media["proxy_manifest"]["sha256"],
            "flir_proxy": media["views"]["flir"]["sha256"],
            "cam4_proxy": media["views"]["cam4"]["sha256"],
        },
        "proxy_sha256_verified": bool(verify_proxy_sha256),
        "proxy_coverage": proxy_status,
        "legacy_artifacts": legacy,
        "archival_capture_candidates": archive_candidates,
    }


def markdown_report(report: Mapping[str, Any]) -> str:
    lines = [
        "# 0704 next-tool forecast coverage audit",
        "",
        "`0704_5`--`0704_17` were inspected. Incomplete references are excluded, not treated as negatives.",
        "",
        "| Case | Status | Frames | Confirmed scrub→surgeon transfers | Scoreable transfers | Public ASR rows | Note |",
        "| --- | --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in report["cases"]:
        counts = row.get("event_counts", {})
        note = "; ".join(row.get("reasons", [])) or "complete evaluation-only reference + paired proxies"
        archive_count = len(row.get("archival_capture_candidates", []))
        if archive_count:
            note += f"; archival MCAP candidates={archive_count} (not benchmark-bound)"
        lines.append(
            "| {case} | {status} | {frames} | {transfers} | {scoreable} | {asr} | {note} |".format(
                case=row["case_id"],
                status=row["status"],
                frames=row.get("frame_count", "-"),
                transfers=counts.get("confirmed_scrub_to_surgeon_transfers", "-"),
                scoreable=counts.get("scoreable_scrub_to_surgeon_transfers", "-"),
                asr=row.get("public_asr_rows", "-"),
                note=note.replace("|", "/"),
            )
        )
    lines.extend(
        [
            "",
            "## Evaluation policy",
            "",
            "- Only `eligible_causal_benchmark` cases may contribute positives or derived negatives.",
            "- Development uses 0704_6--14 with a temporal central embargo; 0704_15--17 is a case-disjoint final holdout.",
            "- This audit does not expose labels to NInfer and does not modify production runtime paths.",
            "",
        ]
    )
    return "\n".join(lines)


def run(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = ensure_safe_output_dir(args.output_dir)
    if output_dir.exists():
        if not args.overwrite:
            raise BenchmarkError(f"output exists (use --overwrite): {output_dir}")
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    try:
        rows = [
            audit_case(case_id, args.proxy_root, args.verify_proxy_sha256)
            for case_id in AUDIT_CASES
        ]
        report = {
            "schema": AUDIT_SCHEMA,
            "cases": rows,
            "summary": {
                "audited_cases": list(AUDIT_CASES),
                "eligible_cases": [row["case_id"] for row in rows if row["status"] == "eligible_causal_benchmark"],
                "excluded_cases": [row["case_id"] for row in rows if row["status"] != "eligible_causal_benchmark"],
                "negative_policy": "unlabeled or incomplete cases never become negative examples",
            },
        }
        json_path = output_dir / "coverage_audit.json"
        markdown_path = output_dir / "coverage_audit.md"
        json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        markdown_path.write_text(markdown_report(report), encoding="utf-8")
        return {"output_dir": str(output_dir), "report": report}
    except Exception:
        shutil.rmtree(output_dir, ignore_errors=True)
        raise


def main() -> int:
    args = parse_args()
    try:
        result = run(args)
    except BenchmarkError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    report = result["report"]
    print(
        canonical_json(
            {
                "output_dir": result["output_dir"],
                "eligible_cases": report["summary"]["eligible_cases"],
                "excluded_cases": report["summary"]["excluded_cases"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
