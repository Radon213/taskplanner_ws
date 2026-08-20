#!/usr/bin/env python3
"""Measure calibration target-name coverage in causal ASR, offline only.

This is a data-availability audit, not a feature builder: labels are joined
only after reading the complete benchmark and no result is sent to NInfer.
It explains which next-tool classes can plausibly benefit from a timestamped
ASR prompt without asserting that an observed word is a future handover.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping

from run_ninfer_eval import RUNS_ROOT, RunError, read_jsonl


AUDIT_SCHEMA = "taskplanner.next_tool_forecast_asr_target_coverage.v1"
ALIASES = {
    "scalpel": ("scalpel", "메스"),
    "adson_forceps": ("adson", "애드슨", "아드손", "앳슨"),
    "allis_forceps": ("allis", "알리스"),
    "bovie": ("bovie", "보비", "보위", "cautery", "커터리"),
    "army_navy_retractor": ("army navy", "army-navy", "아미 네이비", "아미네이비"),
    "bipolar_forceps": ("bipolar", "바이폴라", "바이포라"),
    "mosquito_forceps": ("mosquito", "모스키토", "모스키또"),
    "kocher_retractor": ("kocher", "코처", "thyroid retractor", "갑상선 견인기"),
    "senn_miller_retractor": ("senn", "센 밀러", "센밀러"),
    "harmonic_shears": ("harmonic", "하모닉"),
    "yankauer_suction": ("yankauer", "yankeur", "양카우어", "얀카우어", "석션"),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark-dir", type=Path, required=True)
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


def matching_items(items: list[Mapping[str, Any]], tool_id: str) -> list[dict[str, Any]]:
    aliases = ALIASES.get(tool_id)
    if aliases is None:
        raise RunError(f"no ASR aliases defined for {tool_id}")
    matches = []
    for item in items:
        text = item.get("text")
        offset = item.get("available_offset_sec")
        if not isinstance(text, str) or isinstance(offset, bool):
            raise RunError("malformed timestamped ASR item")
        if any(alias.casefold() in text.casefold() for alias in aliases):
            matches.append({"text": text, "available_offset_sec": float(offset)})
    return matches


def run(args: argparse.Namespace) -> dict[str, Any]:
    benchmark_dir = args.benchmark_dir.resolve()
    inputs = read_jsonl(benchmark_dir / "inputs.jsonl")
    labels = read_jsonl(benchmark_dir / "labels.jsonl")
    label_by_id = {str(row.get("example_id", "")): row for row in labels}
    if len(label_by_id) != len(labels) or {str(row.get("example_id", "")) for row in inputs} != set(label_by_id):
        raise RunError("inputs and labels are not one-to-one")
    positives: list[dict[str, Any]] = []
    for input_row in inputs:
        example_id = str(input_row.get("example_id", ""))
        label = label_by_id[example_id]
        target = label.get("target")
        context = input_row.get("public_context")
        if not isinstance(target, Mapping) or not isinstance(context, Mapping):
            raise RunError("benchmark target/context is malformed")
        if target.get("decision") != "handover":
            continue
        if label.get("split") != "development_calibration":
            raise RunError("ASR target coverage audit requires calibration rows only")
        if context.get("asr_input_format") != "timestamped_relative":
            raise RunError("ASR target coverage audit requires timestamped_relative inputs")
        items = context.get("asr")
        if not isinstance(items, list) or not all(isinstance(item, Mapping) for item in items):
            raise RunError("timestamped ASR item list is malformed")
        tool_id = str(target.get("tool_id", ""))
        positives.append(
            {
                "example_id": example_id,
                "tool_id": tool_id,
                "matches": matching_items(items, tool_id),
            }
        )
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in positives:
        grouped[row["tool_id"]].append(row)
    per_tool = {}
    all_match_offsets: list[float] = []
    for tool_id, rows in sorted(grouped.items()):
        matched = [row for row in rows if row["matches"]]
        offsets = [
            item["available_offset_sec"] for row in matched for item in row["matches"]
        ]
        all_match_offsets.extend(offsets)
        per_tool[tool_id] = {
            "positive_support": len(rows),
            "examples_with_target_alias_in_causal_asr": len(matched),
            "coverage_rate": len(matched) / len(rows) if rows else 0.0,
            "matching_available_offsets_sec": sorted(offsets),
        }
    report = {
        "schema": AUDIT_SCHEMA,
        "benchmark_dir": str(benchmark_dir),
        "data_boundary": "labels joined only for offline availability audit; this report is never a model input",
        "positive_count": len(positives),
        "examples_with_target_alias_in_causal_asr": sum(1 for row in positives if row["matches"]),
        "matching_available_offsets_sec": sorted(all_match_offsets),
        "per_tool": per_tool,
    }
    output_dir = ensure_safe_output_dir(args.output_dir)
    if output_dir.exists():
        if not args.overwrite:
            raise RunError(f"output exists (use --overwrite): {output_dir}")
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    try:
        (output_dir / "asr_target_coverage.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        lines = [
            "# Calibration causal-ASR target coverage",
            "",
            f"Positives: {report['positive_count']}; target-name coverage: {report['examples_with_target_alias_in_causal_asr']}.",
            "",
            "This is offline GT availability analysis only; none of these joined labels or aggregates is sent to the model.",
            "",
            "| Tool | Positive support | Causal target-alias rows | Coverage |",
            "| --- | ---: | ---: | ---: |",
        ]
        for tool_id, values in per_tool.items():
            lines.append(
                f"| {tool_id} | {values['positive_support']} | "
                f"{values['examples_with_target_alias_in_causal_asr']} | {values['coverage_rate']:.3f} |"
            )
        lines.extend(["", ""])
        (output_dir / "asr_target_coverage.md").write_text("\n".join(lines), encoding="utf-8")
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
    report = result["report"]
    print(
        json.dumps(
            {
                "output_dir": result["output_dir"],
                "positive_count": report["positive_count"],
                "target_alias_coverage": report["examples_with_target_alias_in_causal_asr"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
