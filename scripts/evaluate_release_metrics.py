#!/usr/bin/env python3
"""Gate a shadow campaign against its clean-set baseline and safety targets."""

from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import html
import json
import math
from pathlib import Path
from typing import Any, Iterable


CORE_METRICS = (
    ("phase_estimation", "vlm_model_raw"),
    ("phase_estimation", "vlm_raw"),
    ("phase_estimation", "reducer_fused"),
    ("combined_tool_action_selection", "vlm_model_raw"),
    ("combined_tool_action_selection", "vlm_raw"),
    ("combined_tool_action_selection", "reducer_fused"),
    ("combined_tool_action_selection", "bt_decision"),
    ("model_raw_intent_recognition", "vlm_model_raw"),
    ("intent_recognition", "vlm_raw"),
)

ADVISORY_METRICS = (
    ("proactive_next_tool", "vlm_model_raw"),
    ("proactive_next_tool", "vlm_raw"),
    ("preparation_coverage", "skill_execution"),
)

VISUAL_INPUT_UNAVAILABLE_MODES = {
    "missing_visual_input",
    "no_fresh_image",
}


@dataclass(frozen=True)
class GateResult:
    gate: str
    status: str
    required: bool
    value: float | int | str | bool | None
    threshold: float | int | str | bool | None
    detail: str


def _as_bool(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "passed"}


def _float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _load_aggregate(path: Path) -> dict[tuple[str, str], dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    return {(row["metric"], row["layer"]): row for row in rows}


def _load_cases(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def _percentile(values: Iterable[float], fraction: float) -> float | None:
    ordered = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = min(max(fraction, 0.0), 1.0) * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _trace_health(report_dir: Path) -> dict[str, Any]:
    latencies: list[float] = []
    prompt_max = 0
    unhealthy = 0
    visual_input_unavailable = 0
    inference_unhealthy = 0
    health_count = 0
    fault_scenario_ids: set[str] = set()
    fault_counters: dict[str, dict[str, int]] = {}
    runs_dir = report_dir.parent / "runs"
    for trace_path in sorted(runs_dir.glob("case-*/shadow_trace.v1.jsonl")):
        last_fault_status: dict[str, Any] = {}
        with trace_path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if record.get("layer") == "fault_injection_status":
                    payload = record.get("payload", {})
                    if isinstance(payload, dict):
                        last_fault_status = payload
                    continue
                if record.get("layer") != "vlm_health":
                    continue
                payload = record.get("payload", {})
                if not isinstance(payload, dict):
                    continue
                health_count += 1
                if not bool(payload.get("healthy")):
                    unhealthy += 1
                    if str(payload.get("last_mode", "")) in VISUAL_INPUT_UNAVAILABLE_MODES:
                        visual_input_unavailable += 1
                    else:
                        inference_unhealthy += 1
                prompt_max = max(prompt_max, int(payload.get("prompt_chars", 0) or 0))
                latency = _float(payload.get("latency_sec"), -1.0)
                if latency >= 0.0 and bool(payload.get("healthy")):
                    latencies.append(latency)
        scenario_id = str(last_fault_status.get("scenario_id", "")).strip()
        if scenario_id:
            fault_scenario_ids.add(scenario_id)
        counters = last_fault_status.get("counters", {})
        if isinstance(counters, dict):
            for source, values in counters.items():
                if not isinstance(values, dict):
                    continue
                target = fault_counters.setdefault(str(source), {})
                for key, value in values.items():
                    target[str(key)] = target.get(str(key), 0) + int(
                        _float(value)
                    )
    return {
        "health_count": health_count,
        "unhealthy_count": unhealthy,
        "visual_input_unavailable_count": visual_input_unavailable,
        "inference_unhealthy_count": inference_unhealthy,
        "prompt_chars_max": prompt_max,
        "latency_p95_sec": _percentile(latencies, 0.95),
        "latency_count": len(latencies),
        "fault_scenario_ids": sorted(fault_scenario_ids),
        "fault_counters": fault_counters,
    }


def evaluate(
    *,
    candidate_report_dir: Path,
    baseline_report_dir: Path,
    max_regression_pp: float,
    prompt_chars_max: int,
    vlm_p95_max_sec: float,
    safety_only: bool = False,
) -> tuple[list[GateResult], dict[str, Any]]:
    candidate = _load_aggregate(candidate_report_dir / "aggregate_metrics.csv")
    baseline = _load_aggregate(baseline_report_dir / "aggregate_metrics.csv")
    cases = _load_cases(candidate_report_dir / "case_metrics.csv")
    results: list[GateResult] = []

    for required, metric_keys in ((True, CORE_METRICS), (False, ADVISORY_METRICS)):
        for key in metric_keys:
            gate_required = required and not safety_only
            candidate_row = candidate.get(key)
            baseline_row = baseline.get(key)
            label = f"accuracy:{key[0]}:{key[1]}"
            if candidate_row is None or baseline_row is None:
                results.append(
                    GateResult(
                        gate=label,
                        status="failed" if gate_required else "warning",
                        required=gate_required,
                        value=None,
                        threshold=-max_regression_pp,
                        detail="metric is missing from candidate or baseline",
                    )
                )
                continue
            candidate_pct = _float(candidate_row.get("micro_accuracy")) * 100.0
            baseline_pct = _float(baseline_row.get("micro_accuracy")) * 100.0
            delta_pp = candidate_pct - baseline_pct
            results.append(
                GateResult(
                    gate=label,
                    status=(
                        "passed"
                        if delta_pp >= -max_regression_pp
                        else ("failed" if gate_required else "warning")
                    ),
                    required=gate_required,
                    value=round(delta_pp, 4),
                    threshold=round(-max_regression_pp, 4),
                    detail=(
                        f"candidate={candidate_pct:.2f}%, "
                        f"baseline={baseline_pct:.2f}%"
                    ),
                )
            )

    completed = sum(str(row.get("status", "")).lower() == "complete" for row in cases)
    strict_ok = sum(_as_bool(row.get("strict_boundary_ok")) for row in cases)
    input_ok = sum(_as_bool(row.get("input_integrity_ok")) for row in cases)
    invariant_count = sum(int(_float(row.get("invariant_violation_count"))) for row in cases)
    post_terminal = sum(int(_float(row.get("commands_after_completion"))) for row in cases)
    command_correct = sum(int(_float(row.get("command_correct"))) for row in cases)
    command_total = sum(int(_float(row.get("command_total"))) for row in cases)
    case_count = len(cases)
    for gate, value, threshold, detail in (
        ("campaign:completed_cases", completed, case_count, f"{completed}/{case_count}"),
        ("safety:strict_boundary", strict_ok, case_count, f"{strict_ok}/{case_count}"),
        ("safety:input_integrity", input_ok, case_count, f"{input_ok}/{case_count}"),
        ("safety:invariant_violations", invariant_count, 0, "must remain zero"),
        ("safety:post_terminal_commands", post_terminal, 0, "must remain zero"),
        (
            "action:command_fulfillment",
            command_correct,
            command_total,
            f"{command_correct}/{command_total}",
        ),
    ):
        passes = value == threshold
        results.append(
            GateResult(
                gate=gate,
                status="passed" if passes else "failed",
                required=True,
                value=value,
                threshold=threshold,
                detail=detail,
            )
        )

    trace_health = _trace_health(candidate_report_dir)
    health_checks = (
        (
            "vlm:provider_or_inference_failures",
            trace_health["inference_unhealthy_count"],
            0,
            (
                "clean replay must not emit provider, parse, timeout, or "
                "inference failures; source-unavailable degradation is reported separately"
            ),
            not safety_only,
        ),
        (
            "vlm:prompt_chars_max",
            trace_health["prompt_chars_max"],
            prompt_chars_max,
            "value must be <= threshold",
            True,
        ),
        (
            "vlm:fresh_frame_p95_sec",
            trace_health["latency_p95_sec"],
            vlm_p95_max_sec,
            f"n={trace_health['latency_count']}",
            not safety_only,
        ),
    )
    for gate, value, threshold, detail, required in health_checks:
        passes = value is not None and float(value) <= float(threshold)
        results.append(
            GateResult(
                gate=gate,
                status=(
                    "passed" if passes else ("failed" if required else "warning")
                ),
                required=required,
                value=round(float(value), 6) if value is not None else None,
                threshold=threshold,
                detail=detail,
            )
        )
    visual_gap_count = int(trace_health["visual_input_unavailable_count"])
    results.append(
        GateResult(
            gate="observability:visual_input_unavailable",
            status="passed" if visual_gap_count == 0 else "warning",
            required=False,
            value=visual_gap_count,
            threshold=0,
            detail=(
                "camera/source gaps are allowed to degrade to voice-only, but are "
                "reported for dataset and capture review"
            ),
        )
    )
    return results, trace_health


def write_outputs(
    *,
    output_dir: Path,
    candidate_report_dir: Path,
    baseline_report_dir: Path,
    results: list[GateResult],
    trace_health: dict[str, Any],
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    failed_required = [
        result for result in results if result.required and result.status != "passed"
    ]
    payload = {
        "schema": "taskplanner.release_metric_gate.v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "passed" if not failed_required else "failed",
        "candidate_report_dir": str(candidate_report_dir.resolve()),
        "baseline_report_dir": str(baseline_report_dir.resolve()),
        "trace_health": trace_health,
        "results": [asdict(result) for result in results],
    }
    (output_dir / "release_metric_gate.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with (output_dir / "release_metric_gate.csv").open(
        "w", encoding="utf-8", newline=""
    ) as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=("gate", "status", "required", "value", "threshold", "detail"),
        )
        writer.writeheader()
        writer.writerows(asdict(result) for result in results)
    lines = [
        "# Taskplanner release metric gate",
        "",
        f"- Status: **{payload['status']}**",
        f"- Candidate: `{candidate_report_dir}`",
        f"- Baseline: `{baseline_report_dir}`",
        "",
        "| Gate | Required | Status | Value | Threshold | Detail |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for result in results:
        lines.append(
            f"| {result.gate} | {result.required} | {result.status} | "
            f"{result.value} | {result.threshold} | {result.detail} |"
        )
    (output_dir / "release_metric_gate.md").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )
    width = 1280
    row_height = 34
    height = 90 + len(results) * row_height
    svg = [
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
            f'height="{height}" viewBox="0 0 {width} {height}">'
        ),
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        '<text x="24" y="34" font-family="sans-serif" font-size="22" '
        'font-weight="700">Taskplanner release metric gate</text>',
        (
            f'<text x="24" y="58" font-family="sans-serif" font-size="13" '
            f'fill="#526170">{html.escape(payload["status"].upper())} | '
            'accuracy values are percentage-point deltas from baseline</text>'
        ),
    ]
    for index, result in enumerate(results):
        y = 90 + index * row_height
        color = (
            "#12805c"
            if result.status == "passed"
            else ("#b77600" if result.status == "warning" else "#c53b45")
        )
        label = html.escape(result.gate)
        value = html.escape(str(result.value))
        svg.extend(
            [
                f'<rect x="24" y="{y - 17}" width="14" height="14" rx="2" fill="{color}"/>',
                f'<text x="48" y="{y - 5}" font-family="sans-serif" font-size="13">{label}</text>',
                f'<text x="1040" y="{y - 5}" text-anchor="end" font-family="monospace" font-size="13">{value}</text>',
                f'<text x="1240" y="{y - 5}" text-anchor="end" font-family="sans-serif" font-size="12" fill="{color}">{result.status.upper()}</text>',
            ]
        )
    svg.append("</svg>")
    (output_dir / "release_metric_gate.svg").write_text(
        "\n".join(svg) + "\n",
        encoding="utf-8",
    )
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-report-dir", type=Path, required=True)
    parser.add_argument("--baseline-report-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-regression-pp", type=float, default=2.0)
    parser.add_argument("--prompt-chars-max", type=int, default=16_000)
    parser.add_argument("--vlm-p95-max-sec", type=float, default=1.0)
    parser.add_argument(
        "--safety-only",
        action="store_true",
        help=(
            "Keep campaign completion, boundary, invariant, post-terminal, "
            "action fulfillment, and prompt-budget gates required while "
            "reporting accuracy and injected VLM performance faults as warnings."
        ),
    )
    args = parser.parse_args()

    results, trace_health = evaluate(
        candidate_report_dir=args.candidate_report_dir,
        baseline_report_dir=args.baseline_report_dir,
        max_regression_pp=args.max_regression_pp,
        prompt_chars_max=args.prompt_chars_max,
        vlm_p95_max_sec=args.vlm_p95_max_sec,
        safety_only=args.safety_only,
    )
    payload = write_outputs(
        output_dir=args.output_dir,
        candidate_report_dir=args.candidate_report_dir,
        baseline_report_dir=args.baseline_report_dir,
        results=results,
        trace_health=trace_health,
    )
    print(args.output_dir / "release_metric_gate.json")
    return 0 if payload["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
