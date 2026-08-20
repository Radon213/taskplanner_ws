#!/usr/bin/env python3
"""Assess calibration candidates without letting accuracy hide an all-``none`` mode.

An immutable evaluation lock can retain the historical accuracy leader for a
predeclared challenge/holdout run.  Separately, a candidate must pass an
exact-tool recall/F1/none-specificity gate before it may be called suitable for
deployment.  The gate is deliberately reported independently so a high overall
accuracy cannot be mistaken for useful next-tool anticipation.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from compare_runs import comparable_signature, read_json
from run_ninfer_eval import RUNS_ROOT, RunError


SELECTION_SCHEMA = "taskplanner.next_tool_forecast_calibration_selection.v2"

# This is a deliberately minimal non-degeneracy screen, not a clinical release
# criterion.  It rejects a candidate that attains apparent accuracy by saying
# ``none`` for nearly every true handover, while also rejecting one that gains
# recall by making almost every actual-none window a handover.
SUITABILITY_GUARD = {
    "minimum_exact_top1_recall": 0.10,
    "minimum_exact_tool_f1": 0.10,
    "minimum_none_specificity": 0.90,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, action="append", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def ensure_safe_output_dir(path: Path) -> Path:
    output_dir = path.resolve()
    try:
        output_dir.relative_to(RUNS_ROOT.resolve())
    except ValueError as exc:
        raise RunError(f"output directory must be under {RUNS_ROOT.resolve()}") from exc
    if output_dir == RUNS_ROOT.resolve():
        raise RunError("output directory must be a run subdirectory")
    return output_dir


def _metric(metrics: dict[str, Any], name: str) -> float:
    try:
        return float(metrics[name])
    except (KeyError, TypeError, ValueError) as exc:
        raise RunError(f"calibration metric missing/invalid: {name}") from exc


def suitability_assessment(metrics: dict[str, Any]) -> dict[str, Any]:
    """Return an explicit non-degeneracy result for one threshold row."""

    observed = {
        "exact_top1_recall": _metric(metrics, "exact_top1_recall"),
        "f1": _metric(metrics, "f1"),
        "specificity": _metric(metrics, "specificity"),
    }
    criteria = {
        "exact_top1_recall": SUITABILITY_GUARD["minimum_exact_top1_recall"],
        "f1": SUITABILITY_GUARD["minimum_exact_tool_f1"],
        "specificity": SUITABILITY_GUARD["minimum_none_specificity"],
    }
    failed = [name for name, minimum in criteria.items() if observed[name] < minimum]
    return {
        "status": "pass" if not failed else "fail",
        "criteria": criteria,
        "observed": observed,
        "failed_criteria": failed,
        "scope": "minimum non-degeneracy only; not a clinical deployment claim",
    }


def rank_key(row: dict[str, Any]) -> tuple[float, float, float, int, int, float]:
    metrics = row["metrics"]
    return (
        _metric(metrics, "accuracy"),
        _metric(metrics, "exact_top1_recall"),
        _metric(metrics, "balanced_accuracy"),
        -int(metrics.get("false_positive_on_none", 0)),
        -int(metrics.get("wrong_tool_count", 0)),
        -float(row["threshold"]),
    )


def threshold_candidates(run_dir: Path, document: dict[str, Any]) -> list[dict[str, Any]]:
    if document.get("execution_status") != "completed":
        raise RunError(f"calibration run is not completed: {run_dir}")
    summary = document.get("summary")
    if not isinstance(summary, dict) or not isinstance(summary.get("threshold_grid"), dict):
        raise RunError(f"calibration threshold grid missing: {run_dir}")
    rows = []
    for threshold_text, metrics in summary["threshold_grid"].items():
        if not isinstance(metrics, dict):
            continue
        rows.append(
            {
                "variant": str(document.get("variant", "")),
                "threshold": float(threshold_text),
                "metrics": metrics,
                "run_dir": str(run_dir),
                "prompt_sha256": document.get("prompt_sha256"),
                "suitability": suitability_assessment(metrics),
            }
        )
    if not rows:
        raise RunError(f"no calibration thresholds: {run_dir}")
    return rows


def candidate(run_dir: Path, document: dict[str, Any]) -> dict[str, Any]:
    """Keep the accuracy leader as an evaluation lock, with its gate result."""

    return max(threshold_candidates(run_dir, document), key=rank_key)


def run(args: argparse.Namespace) -> dict[str, Any]:
    documents = [(path.resolve(), read_json(path.resolve() / "run.json")) for path in args.run_dir]
    if len(documents) < 2:
        raise RunError("at least two calibration runs are required")
    baseline = comparable_signature(documents[0][1])
    for path, document in documents[1:]:
        if comparable_signature(document) != baseline:
            raise RunError(f"calibration sources/generation differ: {path}")
    all_thresholds = [
        row for path, document in documents for row in threshold_candidates(path, document)
    ]
    candidates = [candidate(path, document) for path, document in documents]
    # Retained for a predeclared research evaluation only.  This must not be
    # labelled deployable until the separate suitability gate passes.
    winner = max(candidates, key=rank_key)
    suitable_rows = [
        row for row in all_thresholds if row["suitability"]["status"] == "pass"
    ]
    suitability_selected = max(suitable_rows, key=rank_key) if suitable_rows else None
    output_dir = ensure_safe_output_dir(args.output_dir)
    if output_dir.exists():
        if not args.overwrite:
            raise RunError(f"output exists (use --overwrite): {output_dir}")
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    try:
        report = {
            "schema": SELECTION_SCHEMA,
            "selection_policy": {
                "evaluation_lock": "maximize strict overall exact_top1 accuracy",
                "tie_breakers": [
                    "higher exact_top1_recall",
                    "higher balanced_accuracy",
                    "fewer false_positive_on_none",
                    "fewer wrong_tool_count",
                    "higher threshold",
                ],
                "accuracy_only_caveat": (
                    "overall accuracy can be none-degenerate when handover windows "
                    "are common; it is not a suitability decision"
                ),
                "primary_suitability_gate": SUITABILITY_GUARD,
                "suitability_rule": (
                    "a deployable candidate must pass exact-tool recall, exact-tool F1, "
                    "and actual-none specificity simultaneously"
                ),
                "data_boundary": "calibration runs only; frozen challenge/final holdout were not read",
            },
            "calibration_signature": baseline,
            "best_per_variant": candidates,
            "threshold_assessments": all_thresholds,
            "locked_selection": winner,
            "suitability_selected": suitability_selected,
            "suitability_status": (
                "pass" if suitability_selected is not None else "fail_no_candidate"
            ),
        }
        (output_dir / "calibration_selection.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        metrics = winner["metrics"]
        locked_gate = winner["suitability"]
        gate_lines = [
            "# Calibration prompt lock and suitability gate",
            "",
            f"Immutable evaluation lock: `{winner['variant']}` at threshold `{winner['threshold']:.2f}` by strict overall exact top-1 accuracy: "
            f"{float(metrics['accuracy']):.4f}.",
            "",
            "Overall accuracy alone is vulnerable to a `none`-degenerate result: it can look high while missing most true handovers. "
            "It is therefore an evaluation-lock metric, not a deployability decision.",
            "",
            "## Primary suitability gate",
            "",
            "A deployable candidate must simultaneously meet: "
            f"exact-tool recall >= {SUITABILITY_GUARD['minimum_exact_top1_recall']:.2f}, "
            f"exact-tool F1 >= {SUITABILITY_GUARD['minimum_exact_tool_f1']:.2f}, and "
            f"actual-none specificity >= {SUITABILITY_GUARD['minimum_none_specificity']:.2f}.",
            "",
            f"Evaluation-lock gate status: **{locked_gate['status']}**; failed criteria: "
            + (", ".join(locked_gate["failed_criteria"]) or "none")
            + ".",
        ]
        if suitability_selected is None:
            gate_lines.extend(
                [
                    "",
                    "No calibration threshold satisfies the primary suitability gate. The immutable evaluation lock remains available for the predeclared research evaluation, but it must not be described as deployable.",
                ]
            )
        else:
            gate_lines.extend(
                [
                    "",
                    f"Suitability-selected candidate: `{suitability_selected['variant']}` at threshold `{suitability_selected['threshold']:.2f}`. This is only a minimum non-degeneracy result, not a clinical deployment claim.",
                ]
            )
        gate_lines.extend(
            [
                "",
                "This report used calibration only; challenge and holdout were not read.",
                "",
            ]
        )
        (output_dir / "calibration_selection.md").write_text(
            "\n".join(gate_lines), encoding="utf-8"
        )
        return {"output_dir": str(output_dir), "report": report}
    except Exception:
        shutil.rmtree(output_dir, ignore_errors=True)
        raise


def main() -> int:
    args = parse_args()
    try:
        result = run(args)
    except (RunError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    winner = result["report"]["locked_selection"]
    print(
        json.dumps(
            {"output_dir": result["output_dir"], "variant": winner["variant"], "threshold": winner["threshold"]},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
