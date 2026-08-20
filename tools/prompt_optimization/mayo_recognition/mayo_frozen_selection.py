#!/usr/bin/env python3
"""Lock the selected Mayo v4 frozen-challenge configuration before any POST.

The artifact records why the temporal-arrival objective selected v4 while also
preserving its crop-calibration regression. It contains no model request body
or raw model output and is validated by ``mayo_prompt_eval.py`` before a frozen
frame is decoded or sent.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import mayo_prompt_eval as evaluator


class SelectionError(RuntimeError):
    pass


def load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SelectionError(f"cannot read result artifact: {path}") from exc
    if not isinstance(value, dict):
        raise SelectionError(f"result artifact must be a JSON object: {path}")
    return value


def _summary(payload: dict[str, Any], mode: str) -> dict[str, Any]:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    value = summary.get(mode)
    if not isinstance(value, dict):
        raise SelectionError(f"calibration result has no {mode} summary")
    return value


def _strict_contract_count(payload: dict[str, Any]) -> int:
    records = payload.get("records")
    if not isinstance(records, list):
        raise SelectionError("calibration result has no records")
    return sum(
        bool(record.get("score", {}).get("contract_valid"))
        for record in records
        if isinstance(record, dict) and isinstance(record.get("score"), dict)
    )


def validate_calibration(payload: dict[str, Any], *, variant: str) -> None:
    if payload.get("suite") != "calibration" or payload.get("variant") != variant:
        raise SelectionError(f"result is not the required calibration {variant} artifact")
    execution = payload.get("execution") if isinstance(payload.get("execution"), dict) else {}
    scoring = payload.get("scoring") if isinstance(payload.get("scoring"), dict) else {}
    if execution.get("status") != "completed" or execution.get("unexecuted_sample_ids"):
        raise SelectionError(f"{variant} calibration is incomplete")
    if scoring.get("performed") is not True:
        raise SelectionError(f"{variant} calibration lacks complete-only scoring")
    if execution.get("inference_http_request_count") != 14:
        raise SelectionError(f"{variant} calibration does not contain exactly 14 POSTs")
    normalizer = payload.get("normalizer_validation") if isinstance(payload.get("normalizer_validation"), dict) else {}
    if not normalizer.get("all_request_image_integrity_checks_passed"):
        raise SelectionError(f"{variant} calibration lacks normalized image integrity evidence")
    policy = payload.get("image_policy") if isinstance(payload.get("image_policy"), dict) else {}
    preprocessor = policy.get("preprocessor") if isinstance(policy.get("preprocessor"), dict) else {}
    if preprocessor.get("requested_flag") != evaluator.IMAGE_PREPROCESS_LETTERBOX_512_Q95:
        raise SelectionError(f"{variant} calibration used a different image preprocessor")
    _summary(payload, "arrival")
    _summary(payload, "crop")


def build_selection(
    *,
    baseline: dict[str, Any],
    v4: dict[str, Any],
    baseline_path: Path,
    v4_path: Path,
    frozen_samples: list[evaluator.Sample],
    event_reference_sha256: str,
) -> dict[str, Any]:
    validate_calibration(baseline, variant="baseline")
    validate_calibration(v4, variant="optimized_v4")
    baseline_source = baseline.get("source") if isinstance(baseline.get("source"), dict) else {}
    v4_source = v4.get("source") if isinstance(v4.get("source"), dict) else {}
    if baseline_source.get("event_reference_sha256") != event_reference_sha256:
        raise SelectionError("baseline event-reference hash differs from the selected frozen reference")
    if v4_source.get("event_reference_sha256") != event_reference_sha256:
        raise SelectionError("v4 event-reference hash differs from the selected frozen reference")
    if v4.get("prompt_version") != evaluator.prompt_version_for("optimized_v4"):
        raise SelectionError("v4 calibration prompt version does not match the selected v4 text")
    model_id = str(v4.get("model", ""))
    if not model_id or model_id != baseline.get("model"):
        raise SelectionError("calibration artifacts do not use the same valid model ID")
    frozen_config = evaluator.frozen_config_for(
        model_id=model_id,
        event_reference_sha256=event_reference_sha256,
        samples=frozen_samples,
    )
    baseline_arrival = _summary(baseline, "arrival")
    v4_arrival = _summary(v4, "arrival")
    baseline_crop = _summary(baseline, "crop")
    v4_crop = _summary(v4, "crop")
    evidence = {
        "baseline_result_sha256": evaluator.sha256_file(baseline_path),
        "v4_result_sha256": evaluator.sha256_file(v4_path),
        "temporal_arrival_gain": {
            "baseline_target_recall": baseline_arrival.get("target_recall"),
            "selected_v4_target_recall": v4_arrival.get("target_recall"),
            "baseline_false_positive_total": baseline_arrival.get("false_positive_total"),
            "selected_v4_false_positive_total": v4_arrival.get("false_positive_total"),
            "baseline_accepted_target_recall": baseline_arrival.get("accepted_target_recall"),
            "selected_v4_accepted_target_recall": v4_arrival.get("accepted_target_recall"),
        },
        "strict_contract_gain": {
            "baseline_valid_contracts": _strict_contract_count(baseline),
            "selected_v4_valid_contracts": _strict_contract_count(v4),
        },
        "explicit_crop_semantic_regression": {
            "baseline_correct": baseline_crop.get("correct"),
            "baseline_accuracy": baseline_crop.get("accuracy"),
            "selected_v4_correct": v4_crop.get("correct"),
            "selected_v4_accuracy": v4_crop.get("accuracy"),
            "acknowledged": True,
            "selection_rationale": "Frozen objective is temporal arrival; crop regression is recorded and not hidden.",
        },
    }
    selection_id = evaluator.canonical_json_sha256(
        {
            "frozen_config": frozen_config,
            "baseline_result_sha256": evidence["baseline_result_sha256"],
            "v4_result_sha256": evidence["v4_result_sha256"],
        }
    )
    return {
        "schema": "taskplanner.mayo_frozen_selection.v1",
        "selection_status": "locked",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "selection_id": selection_id,
        "selected_for_objective": "pre_registered_within_case_temporal_arrival_only",
        "selected_variant": "optimized_v4",
        "prompt_version": evaluator.prompt_version_for("optimized_v4"),
        "frozen_config": frozen_config,
        "calibration_selection_evidence": evidence,
        "post_selection_policy": {
            "prompt_change_after_frozen_review": "prohibited",
            "normalizer_change_after_frozen_review": "prohibited",
            "threshold_change_after_frozen_review": "prohibited",
            "frozen_post_budget": 5,
            "no_cross_case_claim": True,
        },
    }


def write_new(path: Path, payload: dict[str, Any]) -> None:
    if path.exists():
        raise SelectionError(f"refusing to overwrite selection artifact: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--v4", type=Path, required=True)
    parser.add_argument("--events", type=Path, default=evaluator.DEFAULT_EVENTS)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        events = evaluator.load_events(args.events)
        frozen_samples = evaluator.make_frozen_arrival_samples(events)
        payload = build_selection(
            baseline=load_object(args.baseline),
            v4=load_object(args.v4),
            baseline_path=args.baseline,
            v4_path=args.v4,
            frozen_samples=frozen_samples,
            event_reference_sha256=evaluator.sha256_file(args.events),
        )
        write_new(args.output, payload)
    except (SelectionError, evaluator.EvaluationError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"output": str(args.output), "selection_id": payload["selection_id"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
