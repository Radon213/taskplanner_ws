#!/usr/bin/env python3
"""Audit an immutable calibration lock against the non-degeneracy gate.

This tool intentionally does not rank candidates or read challenge/holdout
artifacts.  It gives an already locked calibration choice a precise
deployability-status statement without changing that choice.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from compare_runs import read_json
from run_ninfer_eval import RUNS_ROOT, RunError
from select_calibration import SUITABILITY_GUARD, suitability_assessment


AUDIT_SCHEMA = "taskplanner.next_tool_forecast_selection_suitability_audit.v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--selection-lock",
        type=Path,
        required=True,
        help="Existing calibration_selection.json; it is read only.",
    )
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


def locked_source(lock: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    locked = lock.get("locked_selection")
    if not isinstance(locked, dict):
        raise RunError("selection lock has no locked_selection")
    run_dir_text = locked.get("run_dir")
    if not isinstance(run_dir_text, str) or not run_dir_text:
        raise RunError("locked_selection has no source run directory")
    run = read_json(Path(run_dir_text).resolve() / "run.json")
    if run.get("execution_status") != "completed":
        raise RunError("locked source run is not completed")
    benchmark = run.get("benchmark")
    if not isinstance(benchmark, dict) or benchmark.get("split") != "development_calibration":
        raise RunError("locked source is not a calibration run")
    if run.get("variant") != locked.get("variant"):
        raise RunError("locked variant differs from source run")
    if run.get("prompt_sha256") != locked.get("prompt_sha256"):
        raise RunError("locked prompt hash differs from source run")
    summary = run.get("summary")
    if not isinstance(summary, dict) or not isinstance(summary.get("threshold_grid"), dict):
        raise RunError("locked source run lacks threshold grid")
    try:
        threshold_key = f"{float(locked['threshold']):.2f}"
        source_metrics = summary["threshold_grid"][threshold_key]
    except (KeyError, TypeError, ValueError) as exc:
        raise RunError("locked threshold is absent from source grid") from exc
    if not isinstance(source_metrics, dict):
        raise RunError("locked threshold metrics are malformed")
    if locked.get("metrics") != source_metrics:
        raise RunError("selection lock metrics differ from its source run")
    return locked, source_metrics


def markdown_report(locked: dict[str, Any], gate: dict[str, Any]) -> str:
    observed = gate["observed"]
    failed = gate["failed_criteria"]
    lines = [
        "# Immutable calibration-lock suitability audit",
        "",
        f"Evaluation lock retained unchanged: `{locked['variant']}` at `{float(locked['threshold']):.2f}`.",
        "",
        "Overall accuracy is not sufficient for next-tool usefulness because a high `none` rate can hide a failure to identify true tools.",
        "",
        "| Guard | Minimum | Observed |",
        "| --- | ---: | ---: |",
        f"| Exact-tool recall | {SUITABILITY_GUARD['minimum_exact_top1_recall']:.3f} | {observed['exact_top1_recall']:.3f} |",
        f"| Exact-tool F1 | {SUITABILITY_GUARD['minimum_exact_tool_f1']:.3f} | {observed['f1']:.3f} |",
        f"| Actual-none specificity | {SUITABILITY_GUARD['minimum_none_specificity']:.3f} | {observed['specificity']:.3f} |",
        "",
        f"Primary suitability status: **{gate['status']}**.",
        "",
        "Failed criteria: " + (", ".join(failed) if failed else "none") + ".",
        "",
        "This is a calibration-only audit. It does not change the selection lock, rerank prompts, read challenge/holdout, or make a clinical deployment claim.",
        "",
    ]
    return "\n".join(lines)


def run(args: argparse.Namespace) -> dict[str, Any]:
    lock_path = args.selection_lock.resolve()
    lock = read_json(lock_path)
    locked, source_metrics = locked_source(lock)
    gate = suitability_assessment(source_metrics)
    output_dir = ensure_safe_output_dir(args.output_dir)
    if output_dir.exists():
        if not args.overwrite:
            raise RunError(f"output exists (use --overwrite): {output_dir}")
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    try:
        report = {
            "schema": AUDIT_SCHEMA,
            "selection_lock_path": str(lock_path),
            "immutable_selection": locked,
            "suitability_guard": SUITABILITY_GUARD,
            "suitability": gate,
            "selection_changed": False,
            "data_boundary": "read selection lock and its calibration source only",
        }
        (output_dir / "selection_suitability.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (output_dir / "selection_suitability.md").write_text(
            markdown_report(locked, gate), encoding="utf-8"
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
    print(
        json.dumps(
            {
                "output_dir": result["output_dir"],
                "status": result["report"]["suitability"]["status"],
                "selection_changed": False,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
