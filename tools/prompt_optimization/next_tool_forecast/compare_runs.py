#!/usr/bin/env python3
"""Compare prompt variants only when they used the identical frozen benchmark.

The checker rejects a comparison if inputs/labels hashes, selected example IDs,
model ID, generation settings, or evaluation split differ.  It is therefore a
guard against accidentally reading a sample-size or split change as a prompt
improvement.  It performs no model calls.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Mapping

from run_ninfer_eval import RUNS_ROOT, RunError


COMPARISON_SCHEMA = "taskplanner.next_tool_forecast_run_comparison.v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, action="append", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--selection-lock",
        type=Path,
        help="Optional calibration_selection.json that freezes the final candidate.",
    )
    parser.add_argument(
        "--exploratory-only",
        action="store_true",
        help="Mark this comparison as diagnostic; it cannot revise --selection-lock.",
    )
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RunError(f"JSON object required: {path}")
    return value


def ensure_safe_output_dir(path: Path) -> Path:
    output_dir = path.resolve()
    runs_root = RUNS_ROOT.resolve()
    try:
        output_dir.relative_to(runs_root)
    except ValueError as exc:
        raise RunError(f"output directory must be under {runs_root}") from exc
    if output_dir == runs_root:
        raise RunError("output directory must be a run subdirectory")
    return output_dir


def comparable_signature(run: Mapping[str, Any]) -> dict[str, Any]:
    benchmark = run.get("benchmark")
    generation = run.get("generation")
    if not isinstance(benchmark, Mapping) or not isinstance(generation, Mapping):
        raise RunError("run document lacks benchmark/generation metadata")
    return {
        "model": run.get("model"),
        # Pre-v3 completed strict runs did not serialize this field.  Treat
        # their historical four-key contract as the same strict contract, but
        # prevent a five-key diagnostic trace from being compared as a
        # deployable-output accuracy result.
        "output_contract": run.get("output_contract", "deployable_four_key"),
        # Timestamped-ASR candidates use a different model-visible input
        # contract and therefore require their own frozen control, not a
        # direct claim against historical plain-ASR results.
        "input_contract": run.get("input_contract", "plain_asr"),
        "inputs_sha256": benchmark.get("inputs_sha256"),
        "labels_sha256": benchmark.get("labels_sha256"),
        "split": benchmark.get("split"),
        "regimes": benchmark.get("regimes"),
        "selected_example_ids": benchmark.get("selected_example_ids"),
        "temperature": generation.get("temperature"),
        "top_p": generation.get("top_p"),
        "seed": generation.get("seed"),
        "max_tokens": generation.get("max_tokens"),
        "enable_thinking": generation.get("enable_thinking"),
        "threshold": generation.get("threshold"),
    }


def metric_row(run_dir: Path, run: Mapping[str, Any]) -> dict[str, Any]:
    summary = run.get("summary")
    if not isinstance(summary, Mapping) or not isinstance(summary.get("overall"), Mapping):
        raise RunError(f"run summary missing: {run_dir}")
    overall = summary["overall"]
    return {
        "variant": run.get("variant"),
        "run_dir": str(run_dir),
        "prompt_sha256": run.get("prompt_sha256"),
        "output_contract": run.get("output_contract", "deployable_four_key"),
        "input_contract": run.get("input_contract", "plain_asr"),
        "overall": {
            key: overall.get(key)
            for key in (
                "count",
                "schema_valid_rate",
                "exact_top1_correct",
                "exact_top1_recall",
                "precision",
                "recall",
                "f1",
                "accuracy",
                "specificity",
                "balanced_accuracy",
                "tp",
                "fp",
                "fn",
                "tn",
                "false_positive_on_none",
                "wrong_tool_count",
            )
        },
        "by_regime": summary.get("by_regime"),
        "model_failure_count": summary.get("model_failure_count"),
    }


def markdown_report(report: Mapping[str, Any]) -> str:
    lines = [
        "# Frozen next-tool prompt comparison",
        "",
        "All rows below passed identical benchmark/model/generation signature checks.",
        "",
        "| Variant | Exact top-1 recall | Accuracy | Balanced accuracy | Precision | Recall | FP | FN | Schema valid |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    interpretation = report.get("interpretation")
    if isinstance(interpretation, str) and interpretation:
        lines[2:2] = [interpretation, ""]
    for row in report["runs"]:
        metrics = row["overall"]
        lines.append(
            "| {variant} | {exact:.3f} | {accuracy:.3f} | {balanced:.3f} | {precision:.3f} | {recall:.3f} | {fp} | {fn} | {valid:.3f} |".format(
                variant=row["variant"],
                exact=float(metrics["exact_top1_recall"]),
                accuracy=float(metrics["accuracy"]),
                balanced=float(metrics["balanced_accuracy"]),
                precision=float(metrics["precision"]),
                recall=float(metrics["recall"]),
                fp=metrics["fp"],
                fn=metrics["fn"],
                valid=float(metrics["schema_valid_rate"]),
            )
        )
    lines.extend(
        [
            "",
            "The first comparison signature is retained in `comparison.json`; no source labels or raw requests are copied into this report.",
            "",
        ]
    )
    return "\n".join(lines)


def run(args: argparse.Namespace) -> dict[str, Any]:
    run_dirs = [path.resolve() for path in args.run_dir]
    if len(run_dirs) < 2:
        raise RunError("at least two --run-dir values are required")
    documents = [(path, read_json(path / "run.json")) for path in run_dirs]
    variants = [str(document.get("variant", "")) for _path, document in documents]
    if len(variants) != len(set(variants)) or "" in variants:
        raise RunError("comparison requires one complete run per distinct prompt variant")
    baseline = comparable_signature(documents[0][1])
    for path, document in documents[1:]:
        signature = comparable_signature(document)
        if signature != baseline:
            changed = sorted(key for key in set(baseline) | set(signature) if baseline.get(key) != signature.get(key))
            raise RunError(f"not a frozen comparison; signature differs for {path}: {', '.join(changed)}")
    output_dir = ensure_safe_output_dir(args.output_dir)
    if output_dir.exists():
        if not args.overwrite:
            raise RunError(f"output exists (use --overwrite): {output_dir}")
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    try:
        selection_lock: dict[str, Any] | None = None
        if args.selection_lock is not None:
            selection_document = read_json(args.selection_lock.resolve())
            locked = selection_document.get("locked_selection")
            if not isinstance(locked, dict):
                raise RunError("selection lock has no locked_selection")
            selection_lock = {
                "path": str(args.selection_lock.resolve()),
                "variant": locked.get("variant"),
                "threshold": locked.get("threshold"),
                "prompt_sha256": locked.get("prompt_sha256"),
            }
        if args.exploratory_only and selection_lock is None:
            raise RunError("--exploratory-only requires --selection-lock")
        report = {
            "schema": COMPARISON_SCHEMA,
            "frozen_signature": baseline,
            "runs": [metric_row(path, document) for path, document in documents],
            "selection_lock": selection_lock,
            "interpretation": (
                "Exploratory frozen-challenge comparison only: its v1/v2 results do not change the calibration-locked final holdout candidate."
                if args.exploratory_only
                else ""
            ),
        }
        (output_dir / "comparison.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (output_dir / "comparison.md").write_text(markdown_report(report), encoding="utf-8")
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
    print(json.dumps({"output_dir": result["output_dir"], "variants": [row["variant"] for row in result["report"]["runs"]]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
