#!/usr/bin/env python3
"""Freeze the one allowed non-deployable post-calibration v3 diagnostic.

The strict v3 prompt is intentionally calibration-only in the normal runner.
This tool writes a narrow lock only after its calibration result has failed the
predeclared non-degeneracy gate.  The lock pins the exact prompt hashes,
generation parameters, timestamped input manifests, selected IDs, batch-one
fresh-worker policy, and one output directory for each frozen split.

It is an evaluation exception, never a prompt selection or deployment grant.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Mapping

from prompt_contract import MODEL_ID, asr_input_contract_name, output_contract_name, prompts
from run_ninfer_eval import RUNS_ROOT, RunError, read_jsonl, sha256_file
from select_calibration import suitability_assessment


LOCK_SCHEMA = "taskplanner.next_tool_forecast_failed_candidate_diagnostic.v1"
REQUIRED_SPLITS = {
    "development_challenge": 56,
    "final_holdout": 54,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run-dir", type=Path, required=True)
    parser.add_argument("--challenge-manifest-dir", type=Path, required=True)
    parser.add_argument("--holdout-manifest-dir", type=Path, required=True)
    parser.add_argument("--challenge-output-dir", type=Path, required=True)
    parser.add_argument("--holdout-output-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RunError(f"JSON object required: {path}")
    return value


def ensure_under_runs(path: Path, *, label: str, require_exists: bool = True) -> Path:
    resolved = path.resolve()
    root = RUNS_ROOT.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise RunError(f"{label} must be under {root}") from exc
    if require_exists and not resolved.exists():
        raise RunError(f"{label} is missing: {resolved}")
    return resolved


def ensure_safe_output_dir(path: Path) -> Path:
    output_dir = ensure_under_runs(path, label="output directory", require_exists=False)
    if output_dir == RUNS_ROOT.resolve():
        raise RunError("output directory must be a run subdirectory")
    return output_dir


def frozen_prompt_hash() -> dict[str, str]:
    system, developer = prompts("optimized_v3")
    return {
        "system": hashlib.sha256(system.encode("utf-8")).hexdigest(),
        "developer": hashlib.sha256(developer.encode("utf-8")).hexdigest(),
    }


def source_calibration(run_dir: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    directory = ensure_under_runs(run_dir, label="source calibration run")
    path = directory / "run.json"
    run = read_json(path)
    if run.get("execution_status") != "completed":
        raise RunError("source calibration run is not completed")
    if run.get("variant") != "optimized_v3":
        raise RunError("source calibration variant must be optimized_v3")
    if run.get("input_contract") != "timestamped_relative_asr":
        raise RunError("source calibration must use timestamped ASR")
    if run.get("output_contract") != "deployable_four_key":
        raise RunError("source calibration must use the strict four-key contract")
    if run.get("prompt_sha256") != frozen_prompt_hash():
        raise RunError("source calibration prompt hash differs from current optimized_v3")
    generation = run.get("generation")
    if not isinstance(generation, dict) or float(generation.get("threshold", -1)) != 0.65:
        raise RunError("source calibration must be the v3@0.65 run")
    try:
        metrics = run["summary"]["threshold_grid"]["0.65"]
    except (KeyError, TypeError) as exc:
        raise RunError("source calibration has no v3@0.65 metrics") from exc
    if not isinstance(metrics, dict):
        raise RunError("source v3@0.65 metrics are malformed")
    suitability = suitability_assessment(metrics)
    if suitability["status"] != "fail":
        raise RunError("a passing candidate cannot be labelled failed_candidate_diagnostic")
    return run, metrics, suitability


def frozen_target(manifest_dir: Path, *, split: str, output_dir: Path) -> dict[str, Any]:
    directory = ensure_under_runs(manifest_dir, label=f"{split} manifest")
    inputs_path = directory / "inputs.jsonl"
    labels_path = directory / "labels.jsonl"
    inputs = read_jsonl(inputs_path)
    labels = read_jsonl(labels_path)
    if len(inputs) != REQUIRED_SPLITS[split] or len(labels) != len(inputs):
        raise RunError(f"{split} manifest count is not frozen")
    input_ids = [str(row.get("example_id", "")) for row in inputs]
    label_ids = [str(row.get("example_id", "")) for row in labels]
    if not input_ids or set(input_ids) != set(label_ids) or len(set(input_ids)) != len(input_ids):
        raise RunError(f"{split} manifest input/label identity is not one-to-one")
    if {row.get("split") for row in inputs} != {split} or {row.get("split") for row in labels} != {split}:
        raise RunError(f"{split} manifest has an unexpected split")
    for row in inputs:
        context = row.get("public_context")
        if not isinstance(context, Mapping) or context.get("asr_input_format") != "timestamped_relative":
            raise RunError(f"{split} manifest is not timestamped_relative ASR")
    selected_ids = sorted(input_ids)
    resolved_output = ensure_under_runs(output_dir, label=f"{split} output directory", require_exists=False)
    if resolved_output.exists():
        raise RunError(f"frozen diagnostic output already exists: {resolved_output}")
    return {
        "manifest_dir": str(directory),
        "inputs_sha256": sha256_file(inputs_path),
        "labels_sha256": sha256_file(labels_path),
        "selected_example_ids": selected_ids,
        "example_count": len(selected_ids),
        "output_dir": str(resolved_output),
    }


def markdown_report(lock: Mapping[str, Any]) -> str:
    suitability = lock["suitability"]
    observed = suitability["observed"]
    failed = ", ".join(suitability["failed_criteria"])
    lines = [
        "# Frozen failed-candidate diagnostic lock",
        "",
        "Status: **non-deployable failed-candidate diagnostic**. This is not a selected prompt and cannot be used for deployment or prompt reselection.",
        "",
        "The source `optimized_v3` calibration result at threshold `0.65` failed the predeclared suitability gate:",
        "",
        f"- exact-tool recall: `{observed['exact_top1_recall']:.3f}`",
        f"- exact-tool F1: `{observed['f1']:.3f}`",
        f"- actual-none specificity: `{observed['specificity']:.3f}`",
        f"- failed criteria: `{failed}`",
        "",
        "Exactly two single-pass research evaluations are authorized below. Each must use the stored strict prompt hash, timestamped-ASR manifest hash, selected IDs, threshold `0.65`, batch size `1`, fresh worker lifecycle, and no transport retry.",
        "",
        "| Frozen split | Examples | Predeclared output directory |",
        "| --- | ---: | --- |",
    ]
    for split, target in lock["evaluation_targets"].items():
        lines.append(f"| `{split}` | {target['example_count']} | `{target['output_dir']}` |")
    lines.extend(
        [
            "",
            f"Frozen config SHA-256: `{lock['frozen_config_sha256']}`.",
            "",
        ]
    )
    return "\n".join(lines)


def run(args: argparse.Namespace) -> dict[str, Any]:
    source_run, source_metrics, suitability = source_calibration(args.source_run_dir)
    challenge = frozen_target(
        args.challenge_manifest_dir,
        split="development_challenge",
        output_dir=args.challenge_output_dir,
    )
    holdout = frozen_target(
        args.holdout_manifest_dir,
        split="final_holdout",
        output_dir=args.holdout_output_dir,
    )
    source_path = ensure_under_runs(args.source_run_dir, label="source calibration run") / "run.json"
    frozen_config = {
        "variant": "optimized_v3",
        "model": MODEL_ID,
        "input_contract": asr_input_contract_name("optimized_v3"),
        "output_contract": output_contract_name("optimized_v3"),
        "prompt_sha256": frozen_prompt_hash(),
        "generation": {
            key: source_run["generation"][key]
            for key in ("temperature", "top_p", "seed", "max_tokens", "enable_thinking", "threshold")
        },
        "execution_guard": {
            "batch_size": 1,
            "automatic_transport_retry": False,
            "manager_reload_before_each_batch": True,
            "manager_loaded_vision_check": True,
            "direct_worker_catalog_check": True,
        },
    }
    lock = {
        "schema": LOCK_SCHEMA,
        "candidate_status": "failed_candidate_diagnostic",
        "deployment_status": "non_deployable",
        "selection_status": "not_selected; no calibration candidate passed the primary suitability gate",
        "purpose": (
            "user-requested frozen research diagnostic only; challenge/holdout results must not change "
            "prompt selection or be described as deployment validation"
        ),
        "source_calibration_run": {
            "run_dir": str(Path(args.source_run_dir).resolve()),
            "run_json_sha256": sha256_file(source_path),
            "metrics_at_threshold": source_metrics,
        },
        "suitability": suitability,
        "frozen_config": frozen_config,
        "frozen_config_sha256": hashlib.sha256(canonical_json(frozen_config).encode("utf-8")).hexdigest(),
        "evaluation_targets": {
            "development_challenge": challenge,
            "final_holdout": holdout,
        },
        "one_pass_policy": {
            "per_target": 1,
            "no_prompt_or_threshold_change_after_lock": True,
            "no_retry_after_transport_or_contract_failure": True,
            "no_prompt_reselection_from_challenge_or_holdout": True,
        },
    }
    output_dir = ensure_safe_output_dir(args.output_dir)
    if output_dir.exists():
        if not args.overwrite:
            raise RunError(f"output exists (use --overwrite): {output_dir}")
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    try:
        path = output_dir / "failed_candidate_diagnostic.json"
        path.write_text(json.dumps(lock, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (output_dir / "failed_candidate_diagnostic.md").write_text(markdown_report(lock), encoding="utf-8")
        (output_dir / "failed_candidate_diagnostic.sha256").write_text(
            f"{sha256_file(path)}  {path.name}\n", encoding="utf-8"
        )
        return {"output_dir": str(output_dir), "lock": lock, "lock_path": str(path)}
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
                "lock_path": result["lock_path"],
                "candidate_status": result["lock"]["candidate_status"],
                "deployment_status": result["lock"]["deployment_status"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
