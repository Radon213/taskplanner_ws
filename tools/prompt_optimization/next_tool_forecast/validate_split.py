#!/usr/bin/env python3
"""Fail closed if next-tool calibration/challenge/holdout manifests overlap.

This validates generated artifacts instead of merely trusting the builder:
development calibration's full 8-second outcome window ends before the central
embargo, challenge's full 6-second visual lookback begins after it, and final
holdout case IDs are disjoint. No model calls are made.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Mapping

from build_eval_manifest import (
    DEVELOPMENT_CASES,
    FINAL_HOLDOUT_CASES,
    HORIZON_SEC,
    LOOKBACK_SEC,
    RUNS_ROOT,
    TEMPORAL_EMBARGO_SEC,
    BenchmarkError,
    canonical_json,
    read_jsonl,
)


VALIDATION_SCHEMA = "taskplanner.next_tool_forecast_split_validation.v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calibration-dir", type=Path, required=True)
    parser.add_argument("--challenge-dir", type=Path, required=True)
    parser.add_argument("--holdout-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def ensure_safe_output_dir(path: Path) -> Path:
    output_dir = path.resolve()
    try:
        output_dir.relative_to(RUNS_ROOT.resolve())
    except ValueError as exc:
        raise BenchmarkError(f"output directory must be under {RUNS_ROOT.resolve()}") from exc
    if output_dir == RUNS_ROOT.resolve():
        raise BenchmarkError("output directory must be a run subdirectory")
    return output_dir


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BenchmarkError(f"JSON object required: {path}")
    return value


def read_manifest(directory: Path, expected_split: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    directory = directory.resolve()
    inputs = read_jsonl(directory / "inputs.jsonl")
    labels = read_jsonl(directory / "labels.jsonl")
    audit = read_json(directory / "audit.json")
    input_ids = [str(row.get("example_id", "")) for row in inputs]
    label_ids = [str(row.get("example_id", "")) for row in labels]
    if not input_ids or input_ids != label_ids or len(input_ids) != len(set(input_ids)):
        raise BenchmarkError(f"{directory}: input/label identity is not one-to-one")
    if {row.get("split") for row in inputs} != {expected_split} or {row.get("split") for row in labels} != {expected_split}:
        raise BenchmarkError(f"{directory}: unexpected split label")
    return inputs, labels, audit


def source_case_ids(inputs: list[Mapping[str, Any]]) -> set[str]:
    case_ids = {str(row.get("provenance", {}).get("case_id", "")) for row in inputs}
    if "" in case_ids:
        raise BenchmarkError("input provenance has no case ID")
    return case_ids


def source_snapshots(audit: Mapping[str, Any]) -> Mapping[str, Any]:
    value = audit.get("source_snapshots")
    if not isinstance(value, Mapping):
        raise BenchmarkError("audit has no source snapshots")
    return value


def run(args: argparse.Namespace) -> dict[str, Any]:
    calibration_inputs, _calibration_labels, calibration_audit = read_manifest(
        args.calibration_dir, "development_calibration"
    )
    challenge_inputs, _challenge_labels, challenge_audit = read_manifest(
        args.challenge_dir, "development_challenge"
    )
    holdout_inputs, _holdout_labels, holdout_audit = read_manifest(args.holdout_dir, "final_holdout")
    calibration_cases = source_case_ids(calibration_inputs)
    challenge_cases = source_case_ids(challenge_inputs)
    holdout_cases = source_case_ids(holdout_inputs)
    if calibration_cases != set(DEVELOPMENT_CASES) or challenge_cases != set(DEVELOPMENT_CASES):
        raise BenchmarkError("development manifests do not cover exactly 0704_6-14")
    if holdout_cases != set(FINAL_HOLDOUT_CASES):
        raise BenchmarkError("holdout manifest does not cover exactly 0704_15-17")
    if (calibration_cases | challenge_cases) & holdout_cases:
        raise BenchmarkError("case-level holdout overlaps development")
    calibration_sources = source_snapshots(calibration_audit)
    challenge_sources = source_snapshots(challenge_audit)
    for case_id in DEVELOPMENT_CASES:
        left, right = calibration_sources.get(case_id), challenge_sources.get(case_id)
        if not isinstance(left, Mapping) or not isinstance(right, Mapping):
            raise BenchmarkError(f"{case_id}: absent from development source snapshots")
        for key in ("annotation_manifest", "observed_gt", "timeline", "public_asr"):
            if left.get(key) != right.get(key):
                raise BenchmarkError(f"{case_id}: source changed between calibration and challenge for {key}")
    minimum_separation: dict[str, dict[str, float]] = {}
    for row in calibration_inputs:
        provenance = row["provenance"]
        case_id = str(provenance["case_id"])
        partition = calibration_sources[case_id].get("temporal_partition")
        if not isinstance(partition, Mapping):
            raise BenchmarkError(f"{case_id}: calibration temporal metadata absent")
        boundary = float(partition["boundary_sec"])
        cutoff = float(provenance["cutoff_sec"])
        if cutoff + HORIZON_SEC[1] > boundary - TEMPORAL_EMBARGO_SEC + 1e-6:
            raise BenchmarkError(f"{case_id}: calibration target window crosses embargo")
    for row in challenge_inputs:
        provenance = row["provenance"]
        case_id = str(provenance["case_id"])
        partition = challenge_sources[case_id].get("temporal_partition")
        if not isinstance(partition, Mapping):
            raise BenchmarkError(f"{case_id}: challenge temporal metadata absent")
        boundary = float(partition["boundary_sec"])
        cutoff = float(provenance["cutoff_sec"])
        if cutoff - LOOKBACK_SEC < boundary + TEMPORAL_EMBARGO_SEC - 1e-6:
            raise BenchmarkError(f"{case_id}: challenge image lookback crosses embargo")
    for case_id in DEVELOPMENT_CASES:
        cal_ends = [
            float(row["provenance"]["cutoff_sec"]) + HORIZON_SEC[1]
            for row in calibration_inputs
            if row["provenance"]["case_id"] == case_id
        ]
        challenge_starts = [
            float(row["provenance"]["cutoff_sec"]) - LOOKBACK_SEC
            for row in challenge_inputs
            if row["provenance"]["case_id"] == case_id
        ]
        minimum_separation[case_id] = {
            "latest_calibration_target_end_sec": max(cal_ends),
            "earliest_challenge_image_start_sec": min(challenge_starts),
            "gap_sec": min(challenge_starts) - max(cal_ends),
        }
    output_dir = ensure_safe_output_dir(args.output_dir)
    if output_dir.exists():
        if not args.overwrite:
            raise BenchmarkError(f"output exists (use --overwrite): {output_dir}")
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    try:
        report = {
            "schema": VALIDATION_SCHEMA,
            "status": "passed",
            "case_sets": {
                "development_calibration": sorted(calibration_cases),
                "development_challenge": sorted(challenge_cases),
                "final_holdout": sorted(holdout_cases),
            },
            "minimum_temporal_separation": minimum_separation,
            "rule": {
                "calibration": "cutoff + 8s <= boundary - 4s",
                "challenge": "cutoff - 6s >= boundary + 4s",
            },
            "holdout_source_snapshot_count": len(source_snapshots(holdout_audit)),
        }
        (output_dir / "split_validation.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return {"output_dir": str(output_dir), "report": report}
    except Exception:
        shutil.rmtree(output_dir, ignore_errors=True)
        raise


def main() -> int:
    args = parse_args()
    try:
        result = run(args)
    except BenchmarkError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(canonical_json({"output_dir": result["output_dir"], "status": result["report"]["status"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
