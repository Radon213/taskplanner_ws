#!/usr/bin/env python3
"""Select a Qwen3.5 runtime checkpoint using semantic, fail-closed gates."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument(
        "--candidate",
        type=Path,
        action="append",
        required=True,
        help="Candidate summary.json; pass once per checkpoint.",
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def metric_values(value: dict[str, Any]) -> dict[str, float]:
    return {
        "parse_rate": float(value.get("parse_rate", 0.0)),
        "schema_valid_rate": float(value.get("schema_valid_rate", 0.0)),
        "bed_null_rate": float(value.get("bed_null_rate", 0.0)),
        "forecast_top1": float(value.get("forecast", {}).get("positive_top1_accuracy", 0.0)),
        "forecast_trigger_f1": float(value.get("forecast", {}).get("trigger", {}).get("f1", 0.0)),
        "gesture_f1": float(value.get("gesture", {}).get("trigger", {}).get("f1", 0.0)),
        "intent_accuracy": float(value.get("intent", {}).get("semantic_exact_accuracy", 0.0)),
        "phase_accuracy": float(value.get("phase", {}).get("top1_accuracy", 0.0)),
        "summary_rouge_l": float(value.get("summary_teacher_agreement", {}).get("rouge_l_f1_mean", 0.0)),
        "mayo_rfdetr_agreement": float(value.get("mayo_rfdetr_agreement_not_ground_truth", {}).get("multiset_f1_mean", 0.0)),
    }


def score(metrics: dict[str, float]) -> float:
    weights = {
        "forecast_top1": 0.24,
        "forecast_trigger_f1": 0.16,
        "gesture_f1": 0.18,
        "intent_accuracy": 0.10,
        "phase_accuracy": 0.10,
        "summary_rouge_l": 0.06,
        "mayo_rfdetr_agreement": 0.04,
        "schema_valid_rate": 0.12,
    }
    return sum(metrics[key] * weight for key, weight in weights.items())


def gates(metrics: dict[str, float], baseline: dict[str, float]) -> dict[str, bool]:
    return {
        "parse_rate_at_least_0_98": metrics["parse_rate"] >= 0.98,
        "schema_valid_rate_at_least_0_98": metrics["schema_valid_rate"] >= 0.98,
        "null_bed_safety_exact": metrics["bed_null_rate"] == 1.0,
        "forecast_top1_improves": metrics["forecast_top1"] > baseline["forecast_top1"],
        "forecast_trigger_f1_improves": metrics["forecast_trigger_f1"] > baseline["forecast_trigger_f1"],
        "gesture_not_regressed": metrics["gesture_f1"] >= baseline["gesture_f1"],
        "intent_not_regressed": metrics["intent_accuracy"] >= baseline["intent_accuracy"],
        "phase_not_regressed": metrics["phase_accuracy"] >= baseline["phase_accuracy"],
        "summary_within_ten_percent": metrics["summary_rouge_l"] >= baseline["summary_rouge_l"] * 0.90,
    }


def main() -> int:
    args = parse_args()
    baseline_payload = load(args.baseline.resolve())
    baseline_metrics = metric_values(baseline_payload)
    rows: list[dict[str, Any]] = []
    for path in args.candidate:
        resolved = path.resolve()
        payload = load(resolved)
        metrics = metric_values(payload)
        gate_results = gates(metrics, baseline_metrics)
        rows.append({
            "summary_path": str(resolved),
            "model": payload.get("model"),
            "metrics": metrics,
            "deltas_vs_baseline": {key: metrics[key] - baseline_metrics[key] for key in metrics},
            "gates": gate_results,
            "eligible": all(gate_results.values()),
            "score": score(metrics),
        })
    eligible = [row for row in rows if row["eligible"]]
    selected = max(eligible, key=lambda row: row["score"]) if eligible else None
    result = {
        "schema": "taskplanner.qwen35_9b_runtime_checkpoint_selection.v1",
        "baseline": {"summary_path": str(args.baseline.resolve()), "metrics": baseline_metrics},
        "candidates": rows,
        "status": "selected" if selected else "no_candidate_passed",
        "selected": selected,
        "policy": {
            "validation_only": True,
            "test_split_not_used_for_selection": True,
            "semantic_metrics_override_loss": True,
            "all_gates_required": True,
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if selected else 2


if __name__ == "__main__":
    raise SystemExit(main())
