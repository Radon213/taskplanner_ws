#!/usr/bin/env python3
"""Build a strict, single-selected-prompt Mayo frozen-arrival report."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import mayo_prompt_eval as evaluator


class ReportError(RuntimeError):
    pass


def load_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReportError(f"cannot read JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ReportError(f"JSON root must be object: {path}")
    return payload


def _markdown(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("|", "\\|")


def _expected_ids() -> list[str]:
    return [f"0704_5-challenge-arrival-{event_id}" for event_id in evaluator.FROZEN_CHALLENGE_EVENT_IDS]


def _arrival_records(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    records = payload.get("records")
    if not isinstance(records, list):
        raise ReportError("result has no records")
    selected: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        input_record = record.get("input") if isinstance(record.get("input"), dict) else {}
        if input_record.get("mode") != "arrival":
            continue
        sample_id = str(input_record.get("sample_id", ""))
        if sample_id:
            selected[sample_id] = record
    return selected


def _failure_tags(record: dict[str, Any]) -> list[str]:
    score = record.get("score") if isinstance(record.get("score"), dict) else {}
    tags: list[str] = []
    if record.get("request_error") or score.get("transport_error"):
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


def validate_selected_run(
    *, result: dict[str, Any], selection: dict[str, Any], selection_path: Path
) -> dict[str, dict[str, Any]]:
    if selection.get("schema") != "taskplanner.mayo_frozen_selection.v1" or selection.get("selection_status") != "locked":
        raise ReportError("selection artifact is not a locked Mayo frozen selection")
    config = selection.get("frozen_config") if isinstance(selection.get("frozen_config"), dict) else {}
    if result.get("suite") != "frozen_arrival" or result.get("variant") != config.get("selected_variant"):
        raise ReportError("result does not use the selected frozen prompt variant")
    if result.get("prompt_version") != config.get("prompt_version"):
        raise ReportError("result prompt version differs from locked selection")
    if result.get("dry_run"):
        raise ReportError("frozen result is dry-run, not an inference result")
    execution = result.get("execution") if isinstance(result.get("execution"), dict) else {}
    if execution.get("status") != "completed" or execution.get("unexecuted_sample_ids"):
        raise ReportError("frozen result is incomplete")
    if execution.get("inference_http_request_count") != 5 or execution.get("max_inference_requests_per_fresh_worker_batch") != 1:
        raise ReportError("frozen result does not have exactly five one-POST batches")
    batches = execution.get("batches")
    if not isinstance(batches, list) or len(batches) != 5:
        raise ReportError("frozen result lacks five fresh-worker batch records")
    batch_ids: list[str] = []
    for batch in batches:
        if not isinstance(batch, dict):
            raise ReportError("frozen result batch is invalid")
        lifecycle = batch.get("lifecycle") if isinstance(batch.get("lifecycle"), dict) else {}
        readiness = batch.get("post_batch_readiness") if isinstance(batch.get("post_batch_readiness"), dict) else {}
        ids = batch.get("sample_ids")
        if (
            batch.get("status") != "completed"
            or batch.get("inference_http_request_count") != 1
            or not isinstance(ids, list)
            or len(ids) != 1
            or lifecycle.get("status") != "ready"
            or not readiness.get("manager_loaded")
            or not readiness.get("direct_worker_ready")
        ):
            raise ReportError("frozen result has an incomplete one-POST fresh-worker batch")
        batch_ids.extend(str(value) for value in ids)
    expected_ids = _expected_ids()
    if batch_ids != expected_ids:
        raise ReportError("frozen batch IDs differ from the pre-registered order")
    if config.get("sample_ids") != expected_ids:
        raise ReportError("selection artifact sample IDs differ from the pre-registered order")
    source = result.get("source") if isinstance(result.get("source"), dict) else {}
    if source.get("event_reference_sha256") != config.get("event_reference_sha256"):
        raise ReportError("result event reference differs from locked selection")
    lock = result.get("frozen_selection_lock") if isinstance(result.get("frozen_selection_lock"), dict) else {}
    if lock.get("sha256") != evaluator.sha256_file(selection_path):
        raise ReportError("result does not cite the supplied locked selection artifact")
    if lock.get("selection_id") != selection.get("selection_id"):
        raise ReportError("result selection ID differs from supplied artifact")
    policy = result.get("image_policy") if isinstance(result.get("image_policy"), dict) else {}
    if policy.get("preprocessor") != config.get("image_preprocess_policy"):
        raise ReportError("result preprocessor differs from locked selection")
    normalizer = result.get("normalizer_validation") if isinstance(result.get("normalizer_validation"), dict) else {}
    if not normalizer.get("all_request_image_integrity_checks_passed"):
        raise ReportError("result lacks normalized per-image integrity evidence")
    rows = _arrival_records(result)
    if list(rows) != expected_ids:
        raise ReportError("result records differ from the pre-registered order")
    for sample_id, record in rows.items():
        score = record.get("score") if isinstance(record.get("score"), dict) else {}
        if record.get("request_error") or score.get("transport_error") or score.get("not_inferred"):
            raise ReportError(f"frozen result has no usable output for {sample_id}")
    return rows


def build_report(
    *,
    coverage: dict[str, Any],
    result: dict[str, Any],
    selection: dict[str, Any],
    result_path: Path,
    selection_path: Path,
    source_review: str,
    normalized_review: str,
) -> str:
    rows = validate_selected_run(result=result, selection=selection, selection_path=selection_path)
    coverage_summary = coverage.get("summary") if isinstance(coverage.get("summary"), dict) else {}
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    arrival = summary.get("arrival") if isinstance(summary.get("arrival"), dict) else {}
    evidence = selection.get("calibration_selection_evidence") if isinstance(selection.get("calibration_selection_evidence"), dict) else {}
    crop_regression = evidence.get("explicit_crop_semantic_regression") if isinstance(evidence.get("explicit_crop_semantic_regression"), dict) else {}
    lines = [
        "# Mayo selected v4 frozen temporal-arrival report",
        "",
        "## Scope and frozen lock",
        "",
        f"- Selected prompt: `{result.get('prompt_version')}` / `{result.get('variant')}`.",
        f"- Locked selection artifact: `{selection_path}` (ID `{selection.get('selection_id')}`).",
        f"- Frozen result: `{result_path}`.",
        f"- Accuracy-eligible cases: `{', '.join(coverage_summary.get('accuracy_eligible_cases', [])) or 'none'}`.",
        "- This is a pre-registered, within-case, time-separated arrival challenge; it makes **no cross-case or clinical-generalization claim**.",
        "- Unlabelled 0704 cases are excluded from every accuracy, FP, FN, and negative denominator.",
        "- Ground truth was attached after inference only; request bodies contain no event id, label, timestamp, bbox, phase, speech, or DT state.",
        "- Prompt, letterbox preprocessor, and confidence-threshold policy were locked before the first frozen POST. No post-frozen change or rerun is valid for this split.",
        "",
        "## Selection caveat retained",
        "",
        f"- Crop semantic calibration regression was explicitly accepted for the temporal-arrival objective: `{crop_regression.get('baseline_correct')}/{11}` to `{crop_regression.get('selected_v4_correct')}/{11}` correct.",
        "",
        "## Semantic and strict-contract metrics",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]
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
        lines.append(f"| {key} | {_markdown(arrival.get(key))} |")
    lines.extend(
        [
            "",
            "## Post-inference visual review artifacts",
            "",
            f"- Source failure sheet: `{source_review or 'not rendered'}`",
            f"- Exact normalized model-input failure sheet: `{normalized_review or 'not rendered'}`",
            "",
            "## Per-sample results",
            "",
            "| Frozen sample | Reference (evaluation-only) | Prediction | Tags |",
            "|---|---|---|---|",
        ]
    )
    for sample_id in _expected_ids():
        record = rows[sample_id]
        score = record.get("score") if isinstance(record.get("score"), dict) else {}
        lines.append(
            "| "
            + " | ".join(
                (
                    sample_id,
                    _markdown(record.get("evaluation_reference")),
                    _markdown(score.get("predicted")),
                    ", ".join(_failure_tags(record)),
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Interpretation guardrail",
            "",
            "This frozen report can describe only this locked temporal challenge. Frame review may explain failures, but cannot alter v4, normalization, thresholds, or the five-sample result. A new labelled case or separately pre-registered partition is required for any next prompt experiment.",
            "",
        ]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coverage", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-review", default="")
    parser.add_argument("--normalized-review", default="")
    args = parser.parse_args(argv)
    try:
        if args.output.exists():
            raise ReportError(f"refusing to overwrite output: {args.output}")
        text = build_report(
            coverage=load_object(args.coverage),
            result=load_object(args.result),
            selection=load_object(args.selection),
            result_path=args.result,
            selection_path=args.selection,
            source_review=args.source_review,
            normalized_review=args.normalized_review,
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
