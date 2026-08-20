#!/usr/bin/env python3
"""Build a traceable Markdown comparison for a frozen Mayo prompt challenge."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


class ReportError(RuntimeError):
    pass


FROZEN_SAMPLE_IDS = frozenset(
    f"0704_5-challenge-arrival-{event_id}"
    for event_id in (
        "0704_5-E0016",
        "0704_5-E0020",
        "0704_5-E0031",
        "0704_5-E0037",
        "0704_5-E0041",
    )
)


def load_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReportError(f"cannot read JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ReportError(f"JSON root must be object: {path}")
    return payload


def arrival_records(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    records = payload.get("records")
    if not isinstance(records, list):
        raise ReportError("result has no records")
    selected: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        input_record = record.get("input")
        if not isinstance(input_record, dict) or input_record.get("mode") != "arrival":
            continue
        sample_id = str(input_record.get("sample_id", ""))
        if sample_id:
            selected[sample_id] = record
    return selected


def _markdown_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("|", "\\|")


def failure_tags(record: dict[str, Any]) -> list[str]:
    score = record.get("score") if isinstance(record.get("score"), dict) else {}
    tags: list[str] = []
    if record.get("request_error"):
        tags.append("transport")
    if score.get("valid_json") is False:
        tags.append("invalid-json")
    if score.get("contract_valid") is False:
        tags.append("contract")
    if score.get("target_recalled") is False:
        tags.append("FN")
    if score.get("false_positives"):
        tags.append("FP")
    if not tags and score.get("exact") is True:
        tags.append("exact-TP")
    return tags or ["unclassified"]


def validate_fresh_worker_execution(
    execution: dict[str, Any], *, label: str, expected_sample_ids: set[str]
) -> None:
    """Reject frozen results that skipped the max-three lifecycle guard."""

    if execution.get("lifecycle_invoked") is not True:
        raise ReportError(f"{label} did not invoke the fresh-worker lifecycle guard")
    batches = execution.get("batches")
    if not isinstance(batches, list) or not batches:
        raise ReportError(f"{label} has no fresh-worker batch evidence")
    batch_sample_ids: set[str] = set()
    for batch in batches:
        if not isinstance(batch, dict) or batch.get("status") != "completed":
            raise ReportError(f"{label} has an incomplete fresh-worker batch")
        request_count = batch.get("inference_http_request_count")
        if not isinstance(request_count, int) or isinstance(request_count, bool) or request_count < 0:
            raise ReportError(f"{label} has an invalid fresh-worker request count")
        if request_count > 3:
            raise ReportError(f"{label} exceeded the three-POST fresh-worker limit")
        lifecycle = batch.get("lifecycle") if isinstance(batch.get("lifecycle"), dict) else {}
        readiness = batch.get("post_batch_readiness") if isinstance(batch.get("post_batch_readiness"), dict) else {}
        if lifecycle.get("status") != "ready" or not readiness.get("manager_loaded") or not readiness.get("direct_worker_ready"):
            raise ReportError(f"{label} lacks successful manager/direct-worker readiness evidence")
        ids = batch.get("sample_ids")
        if not isinstance(ids, list) or not all(isinstance(sample_id, str) for sample_id in ids):
            raise ReportError(f"{label} has invalid fresh-worker batch sample IDs")
        if len(ids) > 3:
            raise ReportError(f"{label} scheduled more than three samples in a fresh-worker batch")
        batch_sample_ids.update(ids)
    if batch_sample_ids != expected_sample_ids:
        raise ReportError(f"{label} lifecycle batches do not cover the complete frozen sample set")


def validate_comparison(baseline: dict[str, Any], optimized: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    for payload, label in ((baseline, "baseline"), (optimized, "optimized")):
        if payload.get("suite") != "frozen_arrival":
            raise ReportError(f"{label} is not a frozen_arrival result")
        if payload.get("dry_run"):
            raise ReportError(f"{label} is dry-run, not an inference result")
        execution = payload.get("execution") if isinstance(payload.get("execution"), dict) else {}
        if execution.get("status") != "completed" or execution.get("unexecuted_sample_ids"):
            raise ReportError(f"{label} did not complete the frozen batch")
    source_a = baseline.get("source") if isinstance(baseline.get("source"), dict) else {}
    source_b = optimized.get("source") if isinstance(optimized.get("source"), dict) else {}
    if source_a.get("event_reference_sha256") != source_b.get("event_reference_sha256"):
        raise ReportError("baseline and optimized use different event references")
    rows_a = arrival_records(baseline)
    rows_b = arrival_records(optimized)
    if set(rows_a) != set(rows_b):
        raise ReportError("baseline and optimized do not cover the same frozen samples")
    if set(rows_a) != FROZEN_SAMPLE_IDS:
        raise ReportError("result does not cover the complete pre-registered frozen sample set")
    execution_a = baseline.get("execution") if isinstance(baseline.get("execution"), dict) else {}
    execution_b = optimized.get("execution") if isinstance(optimized.get("execution"), dict) else {}
    validate_fresh_worker_execution(execution_a, label="baseline", expected_sample_ids=set(rows_a))
    validate_fresh_worker_execution(execution_b, label="optimized", expected_sample_ids=set(rows_b))
    for rows, label in ((rows_a, "baseline"), (rows_b, "optimized")):
        for sample_id, record in rows.items():
            score = record.get("score") if isinstance(record.get("score"), dict) else {}
            if record.get("request_error") or score.get("transport_error") or score.get("not_inferred"):
                raise ReportError(f"{label} has no usable model output for {sample_id}")
    return rows_a, rows_b


def build_report(
    *,
    coverage: dict[str, Any],
    baseline: dict[str, Any],
    optimized: dict[str, Any],
    baseline_path: Path,
    optimized_path: Path,
    baseline_review: str,
    optimized_review: str,
) -> str:
    baseline_rows, optimized_rows = validate_comparison(baseline, optimized)
    coverage_summary = coverage.get("summary") if isinstance(coverage.get("summary"), dict) else {}
    source = baseline.get("source") if isinstance(baseline.get("source"), dict) else {}
    lines = [
        "# Mayo prompt frozen-challenge report",
        "",
        "## Scope and evidence boundary",
        "",
        f"- 0704 coverage audit: {coverage_summary.get('case_count', '?')} cases; accuracy-eligible cases: `{', '.join(coverage_summary.get('accuracy_eligible_cases', [])) or 'none'}`.",
        f"- Cross-case holdout possible: `{coverage_summary.get('cross_case_holdout_possible')}`. This report therefore describes a within-case temporal challenge only.",
        "- Unlabelled 0704 cases are excluded from every accuracy, FP, FN, and negative denominator.",
        f"- Event reference SHA-256: `{source.get('event_reference_sha256', '')}`.",
        "- Ground truth was attached after inference only; request payloads contain no event id, label, timestamp, bbox, phase, speech, or DT state.",
        "",
        "## Run comparison",
        "",
        f"- Baseline: `{baseline_path}`",
        f"- Optimized: `{optimized_path}`",
        f"- Baseline review images: `{baseline_review or 'not rendered'}`",
        f"- Optimized review images: `{optimized_review or 'not rendered'}`",
        "",
        "| Metric | Baseline | Optimized |",
        "|---|---:|---:|",
    ]
    baseline_summary = ((baseline.get("summary") or {}).get("arrival") or {})
    optimized_summary = ((optimized.get("summary") or {}).get("arrival") or {})
    for key in (
        "attempted",
        "model_outputs",
        "transport_errors",
        "valid_json",
        "contract_valid",
        "target_recall",
        "exact_match",
        "accepted_target_recall",
        "accepted_exact_match",
        "false_positive_total",
    ):
        lines.append(f"| {key} | {_markdown_value(baseline_summary.get(key))} | {_markdown_value(optimized_summary.get(key))} |")
    lines.extend(
        [
            "",
            "## Per-sample error analysis",
            "",
            "| Frozen sample | Reference (evaluation-only) | Baseline prediction / tags | Optimized prediction / tags |",
            "|---|---|---|---|",
        ]
    )
    for sample_id in sorted(baseline_rows):
        base = baseline_rows[sample_id]
        opt = optimized_rows[sample_id]
        base_score = base.get("score") if isinstance(base.get("score"), dict) else {}
        opt_score = opt.get("score") if isinstance(opt.get("score"), dict) else {}
        base_text = f"{_markdown_value(base_score.get('predicted'))}; {', '.join(failure_tags(base))}"
        opt_text = f"{_markdown_value(opt_score.get('predicted'))}; {', '.join(failure_tags(opt))}"
        lines.append(
            "| "
            + " | ".join(
                (
                    sample_id,
                    _markdown_value(base.get("evaluation_reference")),
                    base_text,
                    opt_text,
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Interpretation guardrail",
            "",
            "A post-challenge frame review may explain errors and motivate the next experiment, but it must not be used to edit and rerun the same frozen split as a new accuracy claim. A new labelled case or separately pre-registered partition is required.",
            "",
        ]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coverage", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--optimized", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline-review", default="")
    parser.add_argument("--optimized-review", default="")
    args = parser.parse_args(argv)
    try:
        if args.output.exists():
            raise ReportError(f"refusing to overwrite output: {args.output}")
        text = build_report(
            coverage=load_object(args.coverage),
            baseline=load_object(args.baseline),
            optimized=load_object(args.optimized),
            baseline_path=args.baseline,
            optimized_path=args.optimized,
            baseline_review=args.baseline_review,
            optimized_review=args.optimized_review,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    except (ReportError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
