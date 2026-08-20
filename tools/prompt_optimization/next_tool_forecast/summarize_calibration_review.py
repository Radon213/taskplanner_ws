#!/usr/bin/env python3
"""Summarize reviewed calibration errors and causal observability, offline only.

This joins already-completed calibration artifacts.  It never opens NInfer or
constructs a model request.  The report deliberately separates scored error
categories from qualitative observations made while inspecting the original
FLIR/CAM4 source-frame sheets.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import shutil
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping

from audit_asr_target_coverage import ALIASES
from run_ninfer_eval import RUNS_ROOT, RunError, read_jsonl


REPORT_SCHEMA = "taskplanner.next_tool_forecast_calibration_review_summary.v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-failure-dir", type=Path, required=True)
    parser.add_argument("--v3-failure-dir", type=Path, required=True)
    parser.add_argument("--asr-coverage-json", type=Path, required=True)
    parser.add_argument("--diagnostic-run-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RunError(f"JSON object required: {path}")
    return value


def ensure_under_runs(path: Path, *, label: str) -> Path:
    resolved = path.resolve()
    root = RUNS_ROOT.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise RunError(f"{label} must be under {root}") from exc
    return resolved


def ensure_safe_output_dir(path: Path) -> Path:
    output_dir = ensure_under_runs(path, label="output directory")
    if output_dir == RUNS_ROOT.resolve():
        raise RunError("output directory must be a run subdirectory")
    return output_dir


def reviewed_failure_bundle(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    directory = ensure_under_runs(path, label="failure directory")
    index = read_json(directory / "failure_index.json")
    reviewed = read_json(directory / "failure_index_reviewed.json")
    if index.get("schema") != "taskplanner.next_tool_forecast_failure_sheets.v1":
        raise RunError(f"unexpected failure-sheet schema: {directory}")
    if reviewed.get("direct_visual_review_status") != "complete":
        raise RunError(f"source-frame review is not complete: {directory}")
    original = index.get("failures")
    overlay = reviewed.get("failures")
    if not isinstance(original, list) or not isinstance(overlay, list) or len(original) != len(overlay):
        raise RunError(f"failure overlay does not match source index: {directory}")
    for source, review in zip(original, overlay):
        if not isinstance(source, Mapping) or not isinstance(review, Mapping):
            raise RunError("malformed failure record")
        if source.get("example_id") != review.get("example_id"):
            raise RunError("failure overlay example ID order changed")
        direct = review.get("direct_visual_review")
        if not isinstance(direct, Mapping) or direct.get("status") != "reviewed":
            raise RunError("failure overlay has an unreviewed entry")
    return index, reviewed


def metric_at_failure_threshold(index: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    run_dir = ensure_under_runs(Path(str(index.get("run_dir", ""))), label="source run directory")
    run = read_json(run_dir / "run.json")
    if run.get("execution_status") != "completed":
        raise RunError(f"source run not complete: {run_dir}")
    try:
        threshold = float(index["threshold"])
        metrics = run["summary"]["threshold_grid"][f"{threshold:.2f}"]
    except (KeyError, TypeError, ValueError) as exc:
        raise RunError(f"failure threshold is absent from source run: {run_dir}") from exc
    if not isinstance(metrics, dict):
        raise RunError("threshold metrics are malformed")
    return run, metrics


def public_asr_items(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    evidence = record.get("causal_evidence")
    if not isinstance(evidence, Mapping) or not isinstance(evidence.get("public_asr"), list):
        raise RunError("failure record has no causal ASR list")
    parsed: list[dict[str, Any]] = []
    for raw in evidence["public_asr"]:
        value: Any = raw
        if isinstance(raw, str):
            try:
                value = ast.literal_eval(raw)
            except (SyntaxError, ValueError) as exc:
                raise RunError("failure record has non-literal ASR entry") from exc
        if not isinstance(value, Mapping):
            raise RunError("failure record ASR entry is not an object")
        text = value.get("text")
        offset = value.get("available_offset_sec")
        if not isinstance(text, str) or isinstance(offset, bool):
            raise RunError("failure record ASR entry is malformed")
        parsed.append({"text": text, "available_offset_sec": float(offset)})
    return parsed


def expected_target_alias_present(record: Mapping[str, Any]) -> bool:
    target = str(record.get("target", ""))
    aliases = ALIASES.get(target)
    if aliases is None:
        return False
    return any(
        any(alias.casefold() in item["text"].casefold() for alias in aliases)
        for item in public_asr_items(record)
    )


def positive_failure_observability(index: Mapping[str, Any]) -> dict[str, Any]:
    failures = index.get("failures")
    if not isinstance(failures, list):
        raise RunError("failure index has no failures")
    positives = [record for record in failures if isinstance(record, Mapping) and record.get("target") != "none"]
    if len(positives) != sum(int(index.get("by_failure_kind", {}).get(key, 0)) for key in ("fn", "wrong_tool_fp_fn")):
        raise RunError("positive failure count does not agree with failure kinds")
    deltas: list[float] = []
    aliases: Counter[str] = Counter()
    for record in positives:
        event = record.get("target_event")
        if not isinstance(event, Mapping) or event.get("delta_sec") is None:
            raise RunError("positive failure has no target-event delta")
        deltas.append(float(event["delta_sec"]))
        if expected_target_alias_present(record):
            aliases[str(record["target"])] += 1
    ordered = sorted(deltas)
    return {
        "positive_failure_count": len(positives),
        "positive_failures_with_expected_target_alias_in_causal_asr": sum(aliases.values()),
        "per_target_alias_count": dict(sorted(aliases.items())),
        "target_event_delta_sec": {
            "min": min(ordered),
            "median": statistics.median(ordered),
            "max": max(ordered),
        },
    }


def bundle_row(label: str, index: Mapping[str, Any], run: Mapping[str, Any], metrics: Mapping[str, Any]) -> dict[str, Any]:
    reviewed = index.get("failures")
    if not isinstance(reviewed, list):
        raise RunError("failure index has no failures")
    pages = index.get("review_pages")
    if not isinstance(pages, list):
        raise RunError("failure index has no review pages")
    return {
        "label": label,
        "variant": str(run.get("variant", "")),
        "threshold": float(index["threshold"]),
        "failure_union_count": int(index.get("failure_count", 0)),
        "failure_kind_union": dict(index.get("by_failure_kind", {})),
        "review_page_count": len(pages),
        "metrics": {
            key: metrics.get(key)
            for key in (
                "count",
                "exact_top1_recall",
                "f1",
                "accuracy",
                "specificity",
                "tp",
                "fp",
                "fn",
                "tn",
                "false_positive_on_none",
                "wrong_tool_count",
                "schema_valid_rate",
            )
        },
        "positive_failure_observability": positive_failure_observability(index),
    }


def diagnostic_contract_summary(run_dir: Path) -> dict[str, Any]:
    directory = ensure_under_runs(run_dir, label="diagnostic run directory")
    run = read_json(directory / "run.json")
    if run.get("execution_status") != "completed" or run.get("variant") != "optimized_v3_diagnostic":
        raise RunError("diagnostic run is not the completed optimized_v3 diagnostic run")
    rows = read_jsonl(directory / "predictions.jsonl")
    counts: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    invalid_rows: list[dict[str, str]] = []
    for row in rows:
        prediction = row.get("prediction")
        if isinstance(prediction, Mapping):
            evidence = prediction.get("evidence_type")
            if isinstance(evidence, str):
                counts[evidence] += 1
        error = str(row.get("contract_error", ""))
        if error:
            errors[error] += 1
            invalid_rows.append({"example_id": str(row.get("example_id", "")), "contract_error": error})
    return {
        "run_dir": str(directory),
        "post_count": int(run.get("post_count", 0)),
        "schema_valid_count": int(run.get("summary", {}).get("overall", {}).get("schema_valid_count", 0)),
        "evidence_type_counts_valid_rows": dict(sorted(counts.items())),
        "contract_errors": dict(sorted(errors.items())),
        "invalid_rows": invalid_rows,
    }


def markdown_report(report: Mapping[str, Any]) -> str:
    control, v3 = report["scored_failure_bundles"]
    coverage = report["calibration_asr_target_coverage"]
    diagnostics = report["diagnostic_output_contract"]
    lines = [
        "# Calibration error taxonomy and causal observability",
        "",
        "Scope: completed timestamped-ASR calibration only. This report made no model call and did not read challenge or holdout labels.",
        "",
        "## Original source-frame review coverage",
        "",
        "| Scored configuration | Source-frame sheets reviewed | Review pages | Union of scored-error examples |",
        "| --- | ---: | ---: | ---: |",
    ]
    for row in (control, v3):
        lines.append(
            f"| `{row['label']}` | {row['failure_union_count']} | {row['review_page_count']} | {row['failure_union_count']} |"
        )
    lines.extend(
        [
            "",
            "Each page is a montage of exact original FLIR/CAM4 proxy-frame pairs. `failure_index_reviewed.json` is an overlay; the original index and source-frame sheets remain unchanged.",
            "",
            "## Scored error taxonomy",
            "",
            "A wrong-tool row is both a positive FN and a FP in binary metrics, so the union count is shown separately rather than added to FP/FN.",
            "",
            "| Configuration | Direct `none` on a positive | Wrong tool (FP+FN) | Handover on actual none | Exact-tool recall | F1 | None specificity |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in (control, v3):
        kinds = row["failure_kind_union"]
        metrics = row["metrics"]
        lines.append(
            "| `{label}` | {fn} | {wrong} | {fp} | {recall:.3f} | {f1:.3f} | {specificity:.3f} |".format(
                label=row["label"],
                fn=int(kinds.get("fn", 0)),
                wrong=int(kinds.get("wrong_tool_fp_fn", 0)),
                fp=int(kinds.get("fp", 0)),
                recall=float(metrics["exact_top1_recall"]),
                f1=float(metrics["f1"]),
                specificity=float(metrics["specificity"]),
            )
        )
    lines.extend(
        [
            "",
            "## Causal observability",
            "",
            f"Only {coverage['examples_with_target_alias_in_causal_asr']}/{coverage['positive_count']} calibration positives contain an alias of their eventual target tool in causally available ASR. This is an offline availability join; it was not provided to the model.",
            "",
            "| Eventual tool | Positive support | Target-alias rows in causal ASR | Coverage |",
            "| --- | ---: | ---: | ---: |",
        ]
    )
    for tool_id, values in coverage["per_tool"].items():
        lines.append(
            f"| `{tool_id}` | {values['positive_support']} | {values['examples_with_target_alias_in_causal_asr']} | {float(values['coverage_rate']):.3f} |"
        )
    lines.extend(["", "| Configuration | Positive error rows | Expected-target alias present in those rows | Event delta (min / median / max seconds) |", "| --- | ---: | ---: | ---: |"])
    for row in (control, v3):
        observable = row["positive_failure_observability"]
        delta = observable["target_event_delta_sec"]
        lines.append(
            "| `{label}` | {count} | {aliases} | {minimum:.3f} / {median:.3f} / {maximum:.3f} |".format(
                label=row["label"],
                count=observable["positive_failure_count"],
                aliases=observable["positive_failures_with_expected_target_alias_in_causal_asr"],
                minimum=float(delta["min"]),
                median=float(delta["median"]),
                maximum=float(delta["max"]),
            )
        )
    lines.extend(
        [
            "",
            "Full-page visual review found that the cutoff images generally show ongoing/current field activity while the CAM4 Mayo/receiving region is partly covered by the central drape. The labelled target transfer occurs roughly four seconds later. This is a qualitative observability finding, not an additional label or a claim that no future cue can ever exist.",
            "",
            "The wrong-tool and actual-none FP sheets show a recurrent carry-forward pattern: a current/residual instrument or stale request is treated as a future transfer. They identify a prompt failure mode, but the counts above do not establish a causal attribution to any one prompt sentence.",
            "",
            "## Diagnostic output-contract check",
            "",
            f"The calibration-only five-key diagnostic completed {diagnostics['post_count']} requests with {diagnostics['schema_valid_count']} valid outputs. Valid evidence types: `{json.dumps(diagnostics['evidence_type_counts_valid_rows'], ensure_ascii=False, sort_keys=True)}`.",
            f"Contract errors: `{json.dumps(diagnostics['contract_errors'], ensure_ascii=False, sort_keys=True)}`. The invalid row is retained as raw evidence and is not silently repaired: `{json.dumps(diagnostics['invalid_rows'], ensure_ascii=False)}`.",
            "",
            "The strict four-key `optimized_v3` result at 0.65 remains a calibration failure for the primary non-degeneracy gate; this taxonomy does not reselect a prompt or make a deployment claim.",
            "",
        ]
    )
    return "\n".join(lines)


def run(args: argparse.Namespace) -> dict[str, Any]:
    control_index, _control_reviewed = reviewed_failure_bundle(args.control_failure_dir)
    v3_index, _v3_reviewed = reviewed_failure_bundle(args.v3_failure_dir)
    control_run, control_metrics = metric_at_failure_threshold(control_index)
    v3_run, v3_metrics = metric_at_failure_threshold(v3_index)
    if control_run.get("variant") != "baseline_v0_timestamped_asr" or v3_run.get("variant") != "optimized_v3":
        raise RunError("unexpected control/v3 source variants")
    coverage_path = ensure_under_runs(args.asr_coverage_json, label="ASR coverage report")
    coverage = read_json(coverage_path)
    if coverage.get("schema") != "taskplanner.next_tool_forecast_asr_target_coverage.v1":
        raise RunError("unexpected ASR coverage schema")
    diagnostics = diagnostic_contract_summary(args.diagnostic_run_dir)
    report = {
        "schema": REPORT_SCHEMA,
        "data_boundary": (
            "offline calibration artifact join only; no model request, challenge labels, or holdout labels read"
        ),
        "sources": {
            "control_failure_dir": str(Path(args.control_failure_dir).resolve()),
            "v3_failure_dir": str(Path(args.v3_failure_dir).resolve()),
            "asr_coverage_json": str(coverage_path),
            "diagnostic_run_dir": str(Path(args.diagnostic_run_dir).resolve()),
        },
        "scored_failure_bundles": [
            bundle_row("control_timestamped_v0@0.90", control_index, control_run, control_metrics),
            bundle_row("optimized_v3_strict@0.65", v3_index, v3_run, v3_metrics),
        ],
        "calibration_asr_target_coverage": coverage,
        "diagnostic_output_contract": diagnostics,
    }
    output_dir = ensure_safe_output_dir(args.output_dir)
    if output_dir.exists():
        if not args.overwrite:
            raise RunError(f"output exists (use --overwrite): {output_dir}")
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    try:
        (output_dir / "error_taxonomy.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (output_dir / "error_taxonomy.md").write_text(markdown_report(report), encoding="utf-8")
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
                "scored_bundles": [row["label"] for row in result["report"]["scored_failure_bundles"]],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
