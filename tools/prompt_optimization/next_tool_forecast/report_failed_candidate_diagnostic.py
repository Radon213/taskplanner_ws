#!/usr/bin/env python3
"""Validate and report the two frozen non-deployable diagnostic evaluations.

No model request is made.  The reporter rejects a run unless it exactly matches
the failed-candidate lock's prompt/config/manifest/selected-ID/one-pass policy.
It produces a descriptive research report, never a selection or deployment
decision.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Mapping

from run_ninfer_eval import (
    FROZEN_DIAGNOSTIC_LOCK_SCHEMA,
    RUNS_ROOT,
    RunError,
    canonical_json,
    read_jsonl,
    sha256_file,
)


REPORT_SCHEMA = "taskplanner.next_tool_forecast_failed_candidate_diagnostic_report.v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--challenge-run-dir", type=Path, required=True)
    parser.add_argument("--holdout-run-dir", type=Path, required=True)
    parser.add_argument("--challenge-failure-dir", type=Path, required=True)
    parser.add_argument("--holdout-failure-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RunError(f"JSON object required: {path}")
    return value


def under_runs(path: Path, *, label: str, exists: bool = True) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(RUNS_ROOT.resolve())
    except ValueError as exc:
        raise RunError(f"{label} must be under {RUNS_ROOT.resolve()}") from exc
    if exists and not resolved.exists():
        raise RunError(f"{label} is missing: {resolved}")
    return resolved


def metrics_subset(metrics: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: metrics.get(key)
        for key in (
            "count",
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
            "schema_valid_count",
            "schema_valid_rate",
        )
    }


def source_frame_review_summary(failure_dir: Path, *, expected_run_dir: Path) -> dict[str, Any]:
    """Return review-page inventory without claiming an external review occurred."""

    directory = under_runs(failure_dir, label="source-frame failure directory")
    index_path = directory / "failure_index.json"
    index = read_json(index_path)
    if index.get("schema") != "taskplanner.next_tool_forecast_failure_sheets.v1":
        raise RunError("unexpected source-frame failure-sheet schema")
    if Path(str(index.get("run_dir", ""))).resolve() != expected_run_dir.resolve():
        raise RunError("source-frame failure index belongs to a different run")
    failures = index.get("failures")
    pages = index.get("review_pages")
    if not isinstance(failures, list) or not isinstance(pages, list):
        raise RunError("source-frame failure index is malformed")
    if int(index.get("failure_count", -1)) != len(failures):
        raise RunError("source-frame failure count does not match entries")
    if len(pages) != (len(failures) + 3) // 4:
        raise RunError("source-frame review page count does not match four-sheet pagination")
    for page in pages:
        if not isinstance(page, str) or not Path(page).is_file():
            raise RunError("source-frame review page is missing")
    for record in failures:
        if not isinstance(record, Mapping):
            raise RunError("source-frame failure record is malformed")
        sheet = record.get("sheet")
        if not isinstance(sheet, str) or not Path(sheet).is_file():
            raise RunError("source-frame failure sheet is missing")
    statuses = sorted(
        {
            str(record.get("direct_visual_review", {}).get("status", "missing"))
            for record in failures
            if isinstance(record, Mapping)
        }
    )
    return {
        "failure_dir": str(directory),
        "failure_index": str(index_path),
        "failure_count_union": len(failures),
        "by_failure_kind": index.get("by_failure_kind"),
        "review_page_count": len(pages),
        "review_pages": pages,
        "montage": index.get("montage"),
        "direct_visual_review_status_in_source_index": statuses,
        "review_boundary": (
            "pages are generated source evidence; no external reviewer completion is inferred by this report"
        ),
    }


def load_lock(path: Path) -> tuple[Path, dict[str, Any]]:
    lock_path = under_runs(path, label="frozen diagnostic lock")
    lock = read_json(lock_path)
    if lock.get("schema") != FROZEN_DIAGNOSTIC_LOCK_SCHEMA:
        raise RunError("unexpected frozen diagnostic lock schema")
    if lock.get("candidate_status") != "failed_candidate_diagnostic" or lock.get("deployment_status") != "non_deployable":
        raise RunError("lock is not explicitly a non-deployable failed candidate")
    config = lock.get("frozen_config")
    digest = lock.get("frozen_config_sha256")
    if not isinstance(config, Mapping) or not isinstance(digest, str):
        raise RunError("lock has no frozen configuration hash")
    if digest != hashlib.sha256(canonical_json(config).encode("utf-8")).hexdigest():
        raise RunError("lock frozen configuration hash mismatch")
    if not isinstance(lock.get("evaluation_targets"), Mapping):
        raise RunError("lock has no evaluation targets")
    return lock_path, lock


def validate_run(
    *, lock_path: Path, lock: Mapping[str, Any], run_dir: Path, split: str
) -> dict[str, Any]:
    directory = under_runs(run_dir, label=f"{split} run")
    run = read_json(directory / "run.json")
    if run.get("execution_status") != "completed":
        raise RunError(f"{split} run is not completed")
    frozen = run.get("frozen_candidate_diagnostic")
    if not isinstance(frozen, Mapping):
        raise RunError(f"{split} run lacks frozen diagnostic proof")
    if frozen.get("candidate_status") != "failed_candidate_diagnostic" or frozen.get("deployment_status") != "non_deployable":
        raise RunError(f"{split} run does not retain non-deployable status")
    if frozen.get("lock_path") != str(lock_path) or frozen.get("lock_sha256") != sha256_file(lock_path):
        raise RunError(f"{split} run lock identity differs from frozen source")
    if frozen.get("frozen_config_sha256") != lock.get("frozen_config_sha256"):
        raise RunError(f"{split} run config hash differs from lock")
    config = lock["frozen_config"]
    for key in ("variant", "model", "input_contract", "output_contract", "prompt_sha256", "generation"):
        if run.get(key) != config.get(key):
            raise RunError(f"{split} run differs from frozen {key}")
    guard = run.get("execution_guard")
    if not isinstance(guard, Mapping):
        raise RunError(f"{split} run has no execution guard")
    for key, expected in config["execution_guard"].items():
        if guard.get(key) != expected:
            raise RunError(f"{split} run execution guard differs at {key}")
    target = lock["evaluation_targets"].get(split)
    if not isinstance(target, Mapping):
        raise RunError(f"lock has no target for {split}")
    benchmark = run.get("benchmark")
    if not isinstance(benchmark, Mapping):
        raise RunError(f"{split} run has no benchmark proof")
    for key in ("inputs_sha256", "labels_sha256", "selected_example_ids"):
        if benchmark.get(key) != target.get(key):
            raise RunError(f"{split} run benchmark differs at {key}")
    if benchmark.get("split") != split or int(target.get("example_count", -1)) != int(run.get("post_count", -1)):
        raise RunError(f"{split} run count/split differs from lock")
    lifecycle = run.get("lifecycle_batches")
    if not isinstance(lifecycle, list) or len(lifecycle) != int(target["example_count"]):
        raise RunError(f"{split} run does not have one lifecycle per frozen example")
    if any(
        not isinstance(batch, Mapping)
        or batch.get("status") != "completed"
        or batch.get("post_cap") != 1
        for batch in lifecycle
    ):
        raise RunError(f"{split} run has incomplete or non-batch-one lifecycle evidence")
    rows = read_jsonl(directory / "predictions.jsonl")
    if len(rows) != int(target["example_count"]) or any(row.get("request_attempts") != 1 for row in rows):
        raise RunError(f"{split} run has non-single-pass request evidence")
    if any(str(row.get("transport_error", "")) for row in rows):
        raise RunError(f"{split} run has a transport error and must not be scored")
    summary = run.get("summary")
    if not isinstance(summary, Mapping) or not isinstance(summary.get("overall"), Mapping):
        raise RunError(f"{split} run has no score summary")
    contract_errors = [
        {
            "example_id": str(row.get("example_id", "")),
            "contract_error": str(row.get("contract_error", "")),
        }
        for row in rows
        if str(row.get("contract_error", ""))
    ]
    return {
        "run_dir": str(directory),
        "run_json_sha256": sha256_file(directory / "run.json"),
        "predictions_sha256": run.get("predictions_sha256"),
        "post_count": run.get("post_count"),
        "model_failure_count": summary.get("model_failure_count"),
        "contract_errors": contract_errors,
        "overall": metrics_subset(summary["overall"]),
        "by_regime": {
            str(regime): metrics_subset(metrics)
            for regime, metrics in sorted(summary.get("by_regime", {}).items())
            if isinstance(metrics, Mapping)
        },
        "per_expected_tool": summary.get("per_expected_tool"),
    }


def markdown_report(report: Mapping[str, Any]) -> str:
    lines = [
        "# Frozen failed-candidate diagnostic results",
        "",
        "Status: **non-deployable failed-candidate diagnostic**. These results do not select a prompt, do not change the calibration decision, and are not a deployment validation.",
        "",
        "Both runs matched the frozen strict `optimized_v3` configuration, timestamped-ASR manifests, single selected-ID list, batch-one fresh-worker policy, and no-retry requirement.",
        "",
        "## Overall metrics",
        "",
        "| Frozen split | N | Exact-tool recall | F1 | Accuracy | None specificity | Wrong tool | Actual-none FP | Schema valid | Contract failures |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for split, run in report["runs"].items():
        metrics = run["overall"]
        lines.append(
            "| `{split}` | {count} | {exact:.3f} | {f1:.3f} | {accuracy:.3f} | {specificity:.3f} | {wrong} | {none_fp} | {valid:.3f} | {failures} |".format(
                split=split,
                count=metrics["count"],
                exact=float(metrics["exact_top1_recall"]),
                f1=float(metrics["f1"]),
                accuracy=float(metrics["accuracy"]),
                specificity=float(metrics["specificity"]),
                wrong=metrics["wrong_tool_count"],
                none_fp=metrics["false_positive_on_none"],
                valid=float(metrics["schema_valid_rate"]),
                failures=run["model_failure_count"],
            )
        )
    lines.extend(
        [
            "",
            "## Per-regime metrics",
            "",
            "| Frozen split / regime | N | Exact-tool recall | F1 | Accuracy | None specificity | Wrong tool | Actual-none FP |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for split, run in report["runs"].items():
        for regime, metrics in run["by_regime"].items():
            lines.append(
                "| `{split}` / `{regime}` | {count} | {exact:.3f} | {f1:.3f} | {accuracy:.3f} | {specificity:.3f} | {wrong} | {none_fp} |".format(
                    split=split,
                    regime=regime,
                    count=metrics["count"],
                    exact=float(metrics["exact_top1_recall"]),
                    f1=float(metrics["f1"]),
                    accuracy=float(metrics["accuracy"]),
                    specificity=float(metrics["specificity"]),
                    wrong=metrics["wrong_tool_count"],
                    none_fp=metrics["false_positive_on_none"],
            )
        )
    lines.extend(
        [
            "",
            "## Source-frame failure review index",
            "",
            "Each page contains four exact original FLIR/CAM4 source-frame sheets. The index records the causal ASR, event/timing evidence, source proxy paths, and a pending review field; this report does not imply that an external reviewer has completed those pages.",
            "",
            "| Frozen split | Error-example union | Direct `none` FN | Wrong tool (FP+FN) | Actual-none FP | Review pages | Source-index review state |",
            "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for split, review in report["source_frame_reviews"].items():
        kinds = review["by_failure_kind"]
        lines.append(
            "| `{split}` | {total} | {fn} | {wrong} | {fp} | {pages} | `{status}` |".format(
                split=split,
                total=review["failure_count_union"],
                fn=int(kinds.get("fn", 0)),
                wrong=int(kinds.get("wrong_tool_fp_fn", 0)),
                fp=int(kinds.get("fp", 0)),
                pages=review["review_page_count"],
                status=", ".join(review["direct_visual_review_status_in_source_index"]),
            )
        )
    lines.extend(
        [
            "",
            "## Strict output-contract exceptions",
            "",
        ]
    )
    for split, run in report["runs"].items():
        errors = run["contract_errors"]
        if errors:
            lines.append(f"- `{split}`: `{json.dumps(errors, ensure_ascii=False)}`")
        else:
            lines.append(f"- `{split}`: none.")
    lines.extend(
        [
            "",
            "Source-frame FP/FN bundles are stored alongside each run and must be used for qualitative failure review; the aggregate table does not substitute for frame inspection.",
            "",
        ]
    )
    return "\n".join(lines)


def run(args: argparse.Namespace) -> dict[str, Any]:
    lock_path, lock = load_lock(args.lock)
    challenge = validate_run(
        lock_path=lock_path,
        lock=lock,
        run_dir=args.challenge_run_dir,
        split="development_challenge",
    )
    holdout = validate_run(
        lock_path=lock_path,
        lock=lock,
        run_dir=args.holdout_run_dir,
        split="final_holdout",
    )
    challenge_review = source_frame_review_summary(
        args.challenge_failure_dir, expected_run_dir=Path(args.challenge_run_dir).resolve()
    )
    holdout_review = source_frame_review_summary(
        args.holdout_failure_dir, expected_run_dir=Path(args.holdout_run_dir).resolve()
    )
    report = {
        "schema": REPORT_SCHEMA,
        "candidate_status": "failed_candidate_diagnostic",
        "deployment_status": "non_deployable",
        "lock_path": str(lock_path),
        "lock_sha256": sha256_file(lock_path),
        "frozen_config_sha256": lock["frozen_config_sha256"],
        "integrity_status": "passed",
        "interpretation_boundary": (
            "descriptive frozen evaluation only; no prompt reselection, deployment claim, or "
            "calibration-decision change is permitted"
        ),
        "runs": {
            "development_challenge": challenge,
            "final_holdout": holdout,
        },
        "source_frame_reviews": {
            "development_challenge": challenge_review,
            "final_holdout": holdout_review,
        },
    }
    output_dir = under_runs(args.output_dir, label="output directory", exists=False)
    if output_dir == RUNS_ROOT.resolve():
        raise RunError("output directory must be a run subdirectory")
    if output_dir.exists():
        if not args.overwrite:
            raise RunError(f"output exists (use --overwrite): {output_dir}")
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    try:
        (output_dir / "failed_candidate_diagnostic_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (output_dir / "failed_candidate_diagnostic_report.md").write_text(
            markdown_report(report), encoding="utf-8"
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
        canonical_json(
            {
                "output_dir": result["output_dir"],
                "integrity_status": result["report"]["integrity_status"],
                "candidate_status": result["report"]["candidate_status"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
