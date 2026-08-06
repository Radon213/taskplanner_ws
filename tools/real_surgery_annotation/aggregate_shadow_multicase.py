#!/usr/bin/env python3
"""Aggregate strict shadow replay runs into a reproducible multi-case report."""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import statistics
from typing import Any, Iterable

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


METRIC_SPECS = (
    ("phase_model_raw", "phase_estimation", "vlm_model_raw"),
    ("phase_operational", "phase_estimation", "vlm_raw"),
    ("phase_reducer", "phase_estimation", "reducer_fused"),
    (
        "tool_model_raw",
        "combined_tool_action_selection",
        "vlm_model_raw",
    ),
    ("tool_operational", "combined_tool_action_selection", "vlm_raw"),
    (
        "tool_reducer",
        "combined_tool_action_selection",
        "reducer_fused",
    ),
    ("tool_bt", "combined_tool_action_selection", "bt_decision"),
    ("tool_model_proactive", "proactive_next_tool", "vlm_model_raw"),
    ("tool_operational_proactive", "proactive_next_tool", "vlm_raw"),
    (
        "intent_model_raw",
        "model_raw_intent_recognition",
        "vlm_model_raw",
    ),
    ("intent_operational", "intent_recognition", "vlm_raw"),
    ("command_fulfillment", "command_fulfillment", "skill_execution"),
    ("preparation_coverage", "preparation_coverage", "skill_execution"),
)

DISPLAY_NAMES = {
    "phase_model_raw": "Phase: model raw",
    "phase_operational": "Phase: VLM operational",
    "phase_reducer": "Phase: reducer fused",
    "tool_model_raw": "Tool: model raw",
    "tool_operational": "Tool: VLM operational",
    "tool_reducer": "Tool: reducer fused",
    "tool_bt": "Tool: BT decision",
    "tool_model_proactive": "Proactive tool: model raw",
    "tool_operational_proactive": "Proactive tool: VLM operational",
    "intent_model_raw": "Intent: model raw",
    "intent_operational": "Intent: VLM operational",
    "command_fulfillment": "Command fulfillment",
    "preparation_coverage": "Preparation coverage",
}


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _percent(value: float | None) -> str:
    return "N/A" if value is None else f"{100.0 * value:.1f}%"


def _seconds(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.3f}s"


def _ratio(correct: int | None, total: int | None) -> str:
    if correct is None or total is None or total <= 0:
        return "N/A"
    return f"{correct}/{total} ({100.0 * correct / total:.1f}%)"


def _distribution(values: Iterable[float]) -> dict[str, float | int | None]:
    ordered = sorted(float(value) for value in values if math.isfinite(value))
    if not ordered:
        return {
            "count": 0,
            "mean": None,
            "median": None,
            "p95": None,
            "max": None,
        }
    index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return {
        "count": len(ordered),
        "mean": statistics.fmean(ordered),
        "median": statistics.median(ordered),
        "p95": ordered[index],
        "max": ordered[-1],
    }


def _markdown_table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(value) for value in row) + " |")
    return "\n".join(lines)


def _case_number(case_id: str) -> int:
    return int(case_id.rsplit("_", 1)[-1])


@dataclass
class Score:
    metric: str
    layer: str
    correct: int | None
    evaluated: int | None
    accuracy: float | None
    status: str
    reference_quality: str


@dataclass
class CaseRun:
    case_id: str
    path: Path
    manifest: dict[str, Any]
    evaluation: dict[str, Any]
    static_boundary: dict[str, Any]
    runtime_boundary: dict[str, Any]
    scores: dict[tuple[str, str], Score]
    vlm_latencies: list[float]

    def score(self, metric: str, layer: str) -> Score:
        return self.scores.get(
            (metric, layer),
            Score(metric, layer, None, None, None, "missing", ""),
        )


def _load_scores(path: Path) -> dict[tuple[str, str], Score]:
    result: dict[tuple[str, str], Score] = {}
    with path.open(newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            score = Score(
                metric=row["metric"],
                layer=row["layer"],
                correct=_int(row.get("correct_count")),
                evaluated=_int(row.get("evaluated_count")),
                accuracy=_float(row.get("accuracy")),
                status=row.get("status", ""),
                reference_quality=row.get("reference_quality", ""),
            )
            result[(score.metric, score.layer)] = score
    return result


def _load_vlm_latencies(path: Path) -> list[float]:
    values: list[float] = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            record = json.loads(line)
            if record.get("topic") != "/vlm/health":
                continue
            payload = record.get("payload", {})
            latency = _float(payload.get("latency_sec"))
            if payload.get("healthy") and latency is not None and latency > 0.0:
                values.append(latency)
    return values


def load_case(path: Path) -> CaseRun:
    manifest = _read_json(path / "run_manifest.json")
    evaluation = _read_json(path / "shadow_evaluation.v2.json")
    return CaseRun(
        case_id=manifest["case_id"],
        path=path,
        manifest=manifest,
        evaluation=evaluation,
        static_boundary=_read_json(path / "static_boundary.json"),
        runtime_boundary=_read_json(path / "runtime_boundary.json"),
        scores=_load_scores(path / "shadow_scorecard.csv"),
        vlm_latencies=_load_vlm_latencies(path / "shadow_trace.v1.jsonl"),
    )


def validate_cases(cases: list[CaseRun], expected_case_ids: list[str]) -> dict[str, Any]:
    errors: list[str] = []
    actual = [case.case_id for case in cases]
    if actual != expected_case_ids:
        errors.append(f"case set/order mismatch: expected={expected_case_ids}, actual={actual}")

    first_runtime = cases[0].manifest["runtime"]
    expected_vlm = first_runtime["vlm"]
    expected_hashes = {
        key: value["sha256"]
        for key, value in first_runtime["code_artifacts"].items()
    }
    expected_commit = first_runtime["git"]["commit"]
    expected_replay_mode = first_runtime.get("replay_mode", "unknown")
    expected_replay_rate = float(first_runtime.get("rate", 1.0) or 1.0)
    expected_fault_injection = first_runtime.get(
        "fault_injection",
        {"enabled": False, "scenario": None},
    )
    for case in cases:
        runtime = case.manifest["runtime"]
        if case.manifest.get("status") != "complete":
            errors.append(f"{case.case_id}: run status is not complete")
        if case.evaluation.get("status") != "complete":
            errors.append(f"{case.case_id}: evaluation status is not complete")
        if case.manifest.get("mode") != "strict":
            errors.append(f"{case.case_id}: mode is not strict")
        if not case.static_boundary.get("ok"):
            errors.append(f"{case.case_id}: static boundary audit failed")
        if not case.runtime_boundary.get("ok"):
            errors.append(f"{case.case_id}: runtime boundary audit failed")
        if not runtime.get("input_integrity", {}).get("ok"):
            errors.append(f"{case.case_id}: input integrity failed")
        if not runtime.get("shadow_feedback_integrity", {}).get("ok"):
            errors.append(f"{case.case_id}: shadow feedback integrity failed")
        if not runtime.get("score_provisional_phase"):
            errors.append(f"{case.case_id}: provisional phase scoring was not enabled")
        if runtime.get("vlm") != expected_vlm:
            errors.append(f"{case.case_id}: VLM configuration differs")
        hashes = {
            key: value["sha256"]
            for key, value in runtime["code_artifacts"].items()
        }
        if hashes != expected_hashes:
            errors.append(f"{case.case_id}: code artifact hashes differ")
        if runtime["git"]["commit"] != expected_commit:
            errors.append(f"{case.case_id}: git commit differs")
        if runtime.get("replay_mode", "unknown") != expected_replay_mode:
            errors.append(f"{case.case_id}: replay mode differs")
        if float(runtime.get("rate", 1.0) or 1.0) != expected_replay_rate:
            errors.append(f"{case.case_id}: replay rate differs")
        if runtime.get("fault_injection") != expected_fault_injection:
            errors.append(f"{case.case_id}: fault-injection setup differs")

    return {
        "ok": not errors,
        "errors": errors,
        "case_count": len(cases),
        "expected_case_ids": expected_case_ids,
        "model": expected_vlm,
        "git_commit": expected_commit,
        "code_artifacts": expected_hashes,
        "replay": {
            "mode": expected_replay_mode,
            "rate": expected_replay_rate,
        },
        "fault_injection": expected_fault_injection,
    }


def aggregate_score(cases: list[CaseRun], metric: str, layer: str) -> dict[str, Any]:
    scores = [case.score(metric, layer) for case in cases]
    valid = [score for score in scores if score.evaluated and score.accuracy is not None]
    correct = sum(score.correct or 0 for score in valid)
    evaluated = sum(score.evaluated or 0 for score in valid)
    return {
        "metric": metric,
        "layer": layer,
        "correct_count": correct,
        "evaluated_count": evaluated,
        "micro_accuracy": correct / evaluated if evaluated else None,
        "macro_accuracy": (
            statistics.fmean(score.accuracy for score in valid) if valid else None
        ),
        "case_count": len(valid),
    }


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _input_coverage_min(case: CaseRun) -> float | None:
    integrity = case.manifest["runtime"].get("input_integrity", {})
    ratios = [
        value.get("coverage_ratio")
        for key, value in integrity.items()
        if key.endswith("_coverage") and isinstance(value, dict)
    ]
    values = [float(value) for value in ratios if value is not None]
    return min(values) if values else None


def _metric_value(case: CaseRun, key: str) -> Score:
    _, metric, layer = next(spec for spec in METRIC_SPECS if spec[0] == key)
    return case.score(metric, layer)


def _case_rows(cases: list[CaseRun]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for case in cases:
        runtime = case.evaluation["runtime"]
        behavior = case.evaluation["behavior_quality"]
        layers = case.evaluation["layers"]
        dt = case.evaluation["scorecard"]["dt_tool_management"]
        command = _metric_value(case, "command_fulfillment")
        latency = _distribution(case.vlm_latencies)
        row: dict[str, Any] = {
            "case_id": case.case_id,
            "status": case.manifest["status"],
            "strict_boundary_ok": (
                case.static_boundary["ok"] and case.runtime_boundary["ok"]
            ),
            "input_integrity_ok": case.manifest["runtime"]["input_integrity"]["ok"],
            "min_input_coverage": _input_coverage_min(case),
            "source_duration_sec": runtime.get("replay_source_duration_sec"),
            "wall_elapsed_sec": runtime.get("replay_wall_elapsed_sec"),
            "realtime_factor": runtime.get("replay_realtime_factor"),
            "vlm_result_count": runtime.get("vlm_result_count"),
            "vlm_effective_rate_hz": runtime.get("vlm_effective_rate_hz"),
            "vlm_latency_median_sec": latency["median"],
            "vlm_latency_p95_sec": latency["p95"],
            "vlm_unhealthy_count": runtime.get("vlm_unhealthy_count"),
            "vlm_parse_retry_count": runtime.get("vlm_parse_retry_count"),
            "request_readiness": behavior["request_readiness"].get("coverage"),
            "unnecessary_preparation_rate": behavior["unnecessary_preparation"].get(
                "rate"
            ),
            "invariant_violation_count": behavior["invariant_violations"].get(
                "count"
            ),
            "commands_after_completion": runtime.get(
                "skill_command_after_completion_count"
            ),
            "command_correct": command.correct,
            "command_total": command.evaluated,
            "dt_inventory_conservation": dt.get("instance_inventory_accuracy"),
            "dt_endpoint_correct": dt.get("correct_count"),
            "dt_endpoint_total": dt.get("evaluated_count"),
        }
        for key, _, _ in METRIC_SPECS:
            score = _metric_value(case, key)
            row[f"{key}_correct"] = score.correct
            row[f"{key}_total"] = score.evaluated
            row[f"{key}_accuracy"] = score.accuracy
        for key, layer in (
            ("tool_model_raw", "vlm_model_raw"),
            ("tool_operational", "vlm_raw"),
            ("tool_reducer", "reducer_fused"),
            ("tool_bt", "bt_decision"),
        ):
            row[f"{key}_proposal_episodes"] = layers[layer].get(
                "prediction_episode_count"
            )
            row[f"{key}_false_positive_episodes"] = layers[layer].get(
                "false_positive_count"
            )
        rows.append(row)
    return rows


def _behavior_latencies(cases: list[CaseRun]) -> dict[str, dict[str, Any]]:
    keys = (
        "latency_sec",
        "wall_clock_latency_sec",
        "ground_truth_to_dt_request_fact_latency_sec",
        "dt_request_fact_to_bt_ingress_latency_sec",
        "dt_request_fact_to_bt_ingress_wall_clock_latency_sec",
        "dt_request_fact_to_bt_evaluation_latency_sec",
        "dt_request_fact_to_bt_acceptance_latency_sec",
        "bt_acceptance_to_handover_wall_clock_latency_sec",
    )
    values: dict[str, list[float]] = {key: [] for key in keys}
    for case in cases:
        for episode in case.evaluation["behavior_quality"]["request_to_handover"].get(
            "episodes", []
        ):
            for key in keys:
                value = _float(episode.get(key))
                if value is not None:
                    values[key].append(value)
    return {key: _distribution(items) for key, items in values.items()}


def _plot_grouped(
    path: Path,
    cases: list[CaseRun],
    keys: list[str],
    title: str,
    ylabel: str = "Accuracy (%)",
) -> None:
    labels = [case.case_id.replace("0704_", "C") for case in cases]
    colors = ["#2397d4", "#46b39d", "#f4a340", "#d9607f"]
    width = 0.8 / len(keys)
    x = list(range(len(cases)))
    fig, ax = plt.subplots(figsize=(15, 6.5))
    for index, key in enumerate(keys):
        values = [(_metric_value(case, key).accuracy or 0.0) * 100 for case in cases]
        offset = (index - (len(keys) - 1) / 2) * width
        ax.bar(
            [item + offset for item in x],
            values,
            width=width,
            label=DISPLAY_NAMES[key],
            color=colors[index],
        )
    ax.set_xticks(x, labels)
    ax.set_ylim(0, 105)
    ax.set_ylabel(ylabel)
    ax.set_title(title, pad=14)
    ax.grid(axis="y", alpha=0.25)
    ax.legend(
        ncol=min(4, len(keys)),
        loc="lower center",
        bbox_to_anchor=(0.5, 1.10),
    )
    fig.tight_layout()
    fig.savefig(path, dpi=180, bbox_inches="tight")
    plt.close(fig)


def _plot_latency(path: Path, cases: list[CaseRun], case_rows: list[dict[str, Any]]) -> None:
    labels = [case.case_id.replace("0704_", "C") for case in cases]
    x = list(range(len(cases)))
    medians = [row["vlm_latency_median_sec"] for row in case_rows]
    p95 = [row["vlm_latency_p95_sec"] for row in case_rows]
    e2e_source = []
    e2e_wall = []
    for case in cases:
        summary = case.evaluation["behavior_quality"]["summary"]
        e2e_source.append(
            summary["request_to_handover_latency_sec"].get("median") or 0.0
        )
        e2e_wall.append(
            summary["request_to_handover_wall_clock_latency_sec"].get("median")
            or 0.0
        )

    fig, axes = plt.subplots(2, 1, figsize=(15, 9), sharex=True)
    axes[0].plot(x, medians, marker="o", label="VLM median", color="#2397d4")
    axes[0].plot(x, p95, marker="s", label="VLM p95", color="#d9607f")
    axes[0].set_ylabel("Seconds")
    axes[0].set_title("VLM response latency")
    axes[0].grid(alpha=0.25)
    axes[0].legend()

    width = 0.36
    axes[1].bar(
        [item - width / 2 for item in x],
        e2e_source,
        width,
        label="Request-to-handover, source clock",
        color="#46b39d",
    )
    axes[1].bar(
        [item + width / 2 for item in x],
        e2e_wall,
        width,
        label="Request-to-handover, wall clock",
        color="#f4a340",
    )
    axes[1].set_ylabel("Seconds")
    axes[1].set_title("Operational handover latency (case median)")
    axes[1].set_xticks(x, labels)
    axes[1].grid(axis="y", alpha=0.25)
    axes[1].legend()
    fig.tight_layout()
    fig.savefig(path, dpi=180)
    plt.close(fig)


def _plot_behavior(path: Path, cases: list[CaseRun], case_rows: list[dict[str, Any]]) -> None:
    labels = [case.case_id.replace("0704_", "C") for case in cases]
    x = list(range(len(cases)))
    width = 0.25
    prep = [(row["preparation_coverage_accuracy"] or 0.0) * 100 for row in case_rows]
    readiness = [(row["request_readiness"] or 0.0) * 100 for row in case_rows]
    unnecessary = [
        (row["unnecessary_preparation_rate"] or 0.0) * 100 for row in case_rows
    ]
    fig, ax = plt.subplots(figsize=(15, 6.5))
    ax.bar([item - width for item in x], prep, width, label="Preparation coverage", color="#2397d4")
    ax.bar(x, readiness, width, label="Ready at request", color="#46b39d")
    ax.bar([item + width for item in x], unnecessary, width, label="Unnecessary preparation", color="#d9607f")
    ax.set_xticks(x, labels)
    ax.set_ylim(0, 105)
    ax.set_ylabel("Rate (%)")
    ax.set_title("Anticipatory behavior quality", pad=14)
    ax.grid(axis="y", alpha=0.25)
    ax.legend(ncol=3, loc="lower center", bbox_to_anchor=(0.5, 1.10))
    fig.tight_layout()
    fig.savefig(path, dpi=180, bbox_inches="tight")
    plt.close(fig)


def _report(
    cases: list[CaseRun],
    case_rows: list[dict[str, Any]],
    aggregates: list[dict[str, Any]],
    validation: dict[str, Any],
    behavior_latency: dict[str, dict[str, Any]],
) -> str:
    by_key = {
        key: next(
            row
            for row in aggregates
            if row["metric"] == metric and row["layer"] == layer
        )
        for key, metric, layer in METRIC_SPECS
    }
    total_source = sum(float(row["source_duration_sec"] or 0.0) for row in case_rows)
    total_wall = sum(float(row["wall_elapsed_sec"] or 0.0) for row in case_rows)
    total_vlm = sum(int(row["vlm_result_count"] or 0) for row in case_rows)
    total_unhealthy = sum(int(row["vlm_unhealthy_count"] or 0) for row in case_rows)
    total_retries = sum(int(row["vlm_parse_retry_count"] or 0) for row in case_rows)
    pooled_vlm = _distribution(
        latency for case in cases for latency in case.vlm_latencies
    )
    command = by_key["command_fulfillment"]
    inventory_ok = sum(
        row["dt_inventory_conservation"] == 1.0 for row in case_rows
    )
    endpoint_total = sum(int(row["dt_endpoint_total"] or 0) for row in case_rows)
    invariant_total = sum(
        int(row["invariant_violation_count"] or 0) for row in case_rows
    )
    after_complete = sum(
        int(row["commands_after_completion"] or 0) for row in case_rows
    )
    recovery_actions = sum(
        case.evaluation["recovery_audit"].get("recovery_action_count", 0)
        for case in cases
    )
    specialized_targets = sum(
        case.evaluation["specialized_group_actions"].get("target_count", 0)
        for case in cases
    )

    summary_rows = []
    for key in (
        "phase_model_raw",
        "phase_reducer",
        "tool_model_raw",
        "tool_operational",
        "tool_reducer",
        "tool_bt",
        "tool_model_proactive",
        "intent_model_raw",
        "intent_operational",
        "command_fulfillment",
    ):
        row = by_key[key]
        summary_rows.append(
            [
                DISPLAY_NAMES[key],
                _ratio(row["correct_count"], row["evaluated_count"]),
                _percent(row["macro_accuracy"]),
            ]
        )

    case_table = []
    for row in case_rows:
        case_table.append(
            [
                row["case_id"],
                _ratio(row["tool_model_raw_correct"], row["tool_model_raw_total"]),
                _ratio(row["tool_reducer_correct"], row["tool_reducer_total"]),
                _ratio(row["tool_bt_correct"], row["tool_bt_total"]),
                _ratio(row["intent_model_raw_correct"], row["intent_model_raw_total"]),
                _ratio(row["phase_model_raw_correct"], row["phase_model_raw_total"]),
                _ratio(row["phase_reducer_correct"], row["phase_reducer_total"]),
                f"{row['vlm_latency_median_sec']:.3f}/{row['vlm_latency_p95_sec']:.3f}",
            ]
        )

    proposal_table = []
    for row in case_rows:
        proposal_table.append(
            [
                row["case_id"],
                f"{row['tool_model_raw_correct']}/{row['tool_model_raw_proposal_episodes']}/{row['tool_model_raw_total']}",
                f"{row['tool_operational_correct']}/{row['tool_operational_proposal_episodes']}/{row['tool_operational_total']}",
                f"{row['tool_reducer_correct']}/{row['tool_reducer_proposal_episodes']}/{row['tool_reducer_total']}",
                f"{row['tool_bt_correct']}/{row['tool_bt_proposal_episodes']}/{row['tool_bt_total']}",
            ]
        )

    runtime_rows = []
    for row in case_rows:
        runtime_rows.append(
            [
                row["case_id"],
                f"{row['source_duration_sec']:.1f}",
                f"{row['wall_elapsed_sec']:.1f}",
                f"{row['realtime_factor']:.3f}x",
                row["vlm_result_count"],
                f"{row['vlm_effective_rate_hz']:.3f}",
                _percent(row["min_input_coverage"]),
                row["vlm_unhealthy_count"],
            ]
        )

    phase_delta = [
        (
            row["case_id"],
            (row["phase_reducer_accuracy"] or 0.0)
            - (row["phase_model_raw_accuracy"] or 0.0),
        )
        for row in case_rows
    ]
    worst_phase = min(phase_delta, key=lambda item: item[1])
    best_phase = max(phase_delta, key=lambda item: item[1])
    tool_delta = [
        (
            row["case_id"],
            (row["tool_reducer_accuracy"] or 0.0)
            - (row["tool_model_raw_accuracy"] or 0.0),
        )
        for row in case_rows
    ]
    best_tool = max(tool_delta, key=lambda item: item[1])
    case_count = len(cases)
    first_case = case_rows[0]["case_id"]
    last_case = case_rows[-1]["case_id"]
    case_scope = (
        f"`{first_case}`"
        if case_count == 1
        else f"`{first_case}`부터 `{last_case}`까지"
    )
    complete_ratio = f"{case_count}/{case_count}"
    replay = validation["replay"]
    fault_injection = validation["fault_injection"]
    if fault_injection.get("enabled"):
        scenario = fault_injection.get("scenario") or {}
        fault_description = (
            f"enabled (`{Path(str(scenario.get('path', 'unknown'))).name}`, "
            f"SHA-256 `{scenario.get('sha256', 'unknown')}`)"
        )
    else:
        fault_description = "disabled"
    bt_ingress_wall_p95 = behavior_latency[
        "dt_request_fact_to_bt_ingress_wall_clock_latency_sec"
    ]["p95"]
    bt_ingress_gate = (
        "PASS"
        if bt_ingress_wall_p95 is not None and bt_ingress_wall_p95 <= 0.250
        else "FAIL"
    )

    return f"""# Taskplanner {case_count}-case Shadow Replay 성능 보고서

## 요약

- 대상: {case_scope} 실제 수술 시연 영상 {case_count}개
- 실행 성공: **{complete_ratio}**, strict 정보 경계 감사: **{complete_ratio} 통과**
- 모델: **{validation['model']['provider_id']} / {validation['model']['model_id']}**, structured JSON, seed {validation['model']['generation_seed']}
- 총 영상 시간 / 실제 처리 시간: **{total_source:.1f}s / {total_wall:.1f}s**
- VLM 유효 결과: **{total_vlm}건**, pooled latency median/p95: **{pooled_vlm['median']:.3f}/{pooled_vlm['p95']:.3f}s**
- 핵심 결과: reducer의 도구 선택은 model raw보다 전반적으로 개선됐지만, BT 단계에서 정책·가용성 제약으로 일부 recall이 다시 낮아졌다.
- 가장 큰 미해결 영역: proactive 준비 성능, 케이스별 phase 안정성, instance-resolved DT endpoint 정답 라벨이다.

> 이 결과는 동일 시연 캠페인의 {case_count}개 개발 영상에 대한 shadow 평가다. 임상 효능, 다른 집도의·병원·카메라 환경에 대한 외부 일반화 성능을 의미하지 않는다. Phase 점수는 현재 provisional/ambiguous 라벨을 사용한 개발용 수치다.

## 실험 조건

- Procedure bundle: `thyroidectomy_demo`
- Mode: `strict`, ground-truth label은 런타임 입력에 노출하지 않고 오프라인 평가에서만 사용
- Replay: `{replay['mode']}`, {replay['rate']:.1f}x source rate; fault injection {fault_description}
- VLM 입력 주기: 1.0s, max output 320 tokens, thinking/reasoning `none`
- Object perception: RF-DETR FLIR segmentation + CAM4 detection service 사용
- Git commit: `{validation['git_commit']}`; dirty 작업 트리는 각 핵심 코드 파일 SHA-256을 고정해 동일성을 검증
- Phase: 현재 provisional phase interval을 명시적으로 scoring했으며 임상 확정 GT로 취급하지 않음

## 유효성 게이트

{_markdown_table(
    ['검증 항목', '결과'],
    [
        ['완료된 실행', complete_ratio],
        ['Static information-boundary audit', f'{complete_ratio} 통과'],
        ['Runtime information-boundary audit', f'{complete_ratio} 통과'],
        ['Input integrity', f'{complete_ratio} 통과'],
        ['Counterfactual feedback integrity', f'{complete_ratio} 통과'],
        ['동일 모델 설정', f'{complete_ratio} 일치'],
        ['동일 핵심 코드 SHA-256', f'{complete_ratio} 일치'],
    ],
)}

## 종합 성능

`Micro`는 전체 정답 수/전체 평가 가능 수이고, `Macro`는 {case_count}개 케이스 정확도의 단순 평균이다.

{_markdown_table(['지표', 'Micro 정답/전체', 'Macro'], summary_rows)}

![Overall tool accuracy](tool_accuracy_by_case.png)

### 도구 제안량

표기 순서는 **정답 수 / 실제 제안 episode 수 / 평가 가능한 target 수**다. 제안 episode는 같은 target을 향한 반복·변경 제안을 포함하므로 target 수보다 클 수 있다.

{_markdown_table(
    ['Case', 'Model raw', 'VLM operational', 'Reducer fused', 'BT'],
    proposal_table,
)}

## 케이스별 결과

{_markdown_table(
    [
        'Case',
        'Tool raw',
        'Tool reducer',
        'Tool BT',
        'Intent raw',
        'Phase raw*',
        'Phase reducer*',
        'VLM med/p95 (s)',
    ],
    case_table,
)}

`*` Phase는 provisional/ambiguous reference에 대한 개발용 frame/snapshot accuracy다.

![Provisional phase accuracy](phase_accuracy_by_case.png)

## 지연 및 처리량

{_markdown_table(
    ['Case', '영상(s)', 'Wall(s)', 'RTF', 'VLM 결과', 'VLM Hz', '최저 입력 커버리지', 'Unhealthy'],
    runtime_rows,
)}

- Pooled VLM latency: n={pooled_vlm['count']}, mean={pooled_vlm['mean']:.3f}s, median={pooled_vlm['median']:.3f}s, p95={pooled_vlm['p95']:.3f}s, max={pooled_vlm['max']:.3f}s
- VLM unhealthy: {total_unhealthy}건, parse retry: {total_retries}건
- Request→handover source-clock latency: n={behavior_latency['latency_sec']['count']}, median={behavior_latency['latency_sec']['median']:.3f}s, p95={behavior_latency['latency_sec']['p95']:.3f}s
- Request→handover wall-clock latency: n={behavior_latency['wall_clock_latency_sec']['count']}, median={behavior_latency['wall_clock_latency_sec']['median']:.3f}s, p95={behavior_latency['wall_clock_latency_sec']['p95']:.3f}s
- GT request→DT fact source latency: median={behavior_latency['ground_truth_to_dt_request_fact_latency_sec']['median']:.3f}s
- DT fact→BT context ingress wall latency: median/p95={_seconds(behavior_latency['dt_request_fact_to_bt_ingress_wall_clock_latency_sec']['median'])}/{_seconds(bt_ingress_wall_p95)}
- DT→BT ingress p95 ≤ 0.250s software gate: **{bt_ingress_gate}**
- DT fact→BT first decision publication source latency: median/p95={_seconds(behavior_latency['dt_request_fact_to_bt_evaluation_latency_sec']['median'])}/{_seconds(behavior_latency['dt_request_fact_to_bt_evaluation_latency_sec']['p95'])}
- DT fact→BT action acceptance source latency: median/p95={_seconds(behavior_latency['dt_request_fact_to_bt_acceptance_latency_sec']['median'])}/{_seconds(behavior_latency['dt_request_fact_to_bt_acceptance_latency_sec']['p95'])} (정책·가용성 대기 포함)
- BT acceptance→handover wall latency: median={behavior_latency['bt_acceptance_to_handover_wall_clock_latency_sec']['median']:.3f}s

![Latency by case](latency_by_case.png)

## 능동 준비와 BT 동작

![Anticipatory behavior](behavior_quality_by_case.png)

- Proactive next-tool 성능은 combined tool selection보다 현저히 낮다. 음성 요청 이후의 올바른 전달과 사전 예측을 구분해 해석해야 한다.
- Command fulfillment: {_ratio(command['correct_count'], command['evaluated_count'])}. 이는 counterfactual mock execution 성공률이며 실제 로봇 물리 성공률이 아니다.
- Invariant violation: {invariant_total}건, 완료 후 skill command: {after_complete}건.
- Recovery command: {recovery_actions}건. 이번 reference에서는 recovery 동작의 충분한 정확도 표본이 확보되지 않았다.
- Specialized bed-robot action reference target: {specialized_targets}건. 해당 항목은 현재 정답 라벨 공백으로 정확도를 산출할 수 없다.

## DT 평가 범위

- 선언된 도구 instance slot 보존은 **{inventory_ok}/{case_count} run**에서 만족했다.
- 그러나 confirmed endpoint 비교 가능 표본은 **0건**이다. 평가 마스크가 type-level 중복 instance와 불완전한 위치 전이를 제외했기 때문이다.
- 따라서 이번 결과를 “DT 도구 위치 정확도 100%”로 표현하면 안 된다. 확인된 것은 **선언 inventory 보존**이며, 물리 instance별 위치 정확도는 아직 `N/A`다.

## 주요 관찰

1. **Reducer는 도구 후보 정리에 효과적이다.** 가장 큰 case-level 개선은 `{best_tool[0]}`에서 {best_tool[1] * 100:+.1f}%p였다.
2. **BT는 안전·가용성 정책 때문에 reducer의 target recall을 그대로 실행하지 않는다.** 이는 일부 정답 손실과 함께 잘못된 행동 차단을 동반하므로 blocker/suspicious audit과 함께 봐야 한다.
3. **Phase reducer 효과는 일관되지 않다.** 가장 큰 개선은 `{best_phase[0]}` {best_phase[1] * 100:+.1f}%p, 가장 큰 저하는 `{worst_phase[0]}` {worst_phase[1] * 100:+.1f}%p다.
4. **Intent는 operational 후처리에서 안정적이지만 model raw와 차이가 있다.** 두 수치를 분리해 모델 자체 성능과 시스템 보정 효과를 구분해야 한다.
5. **능동 준비는 아직 병목이다.** combined handover 정확도만 보면 실제 요청 이후 반응 성능이 proactive 예측 부족을 가릴 수 있다.

## 다음 개선 우선순위

1. `{worst_phase[0]}` 등 phase reducer가 악화된 케이스의 transition evidence와 hysteresis를 분석한다.
2. Proactive tool prediction은 정확도와 함께 unnecessary preparation, wrong-preposition release latency를 공동 최적화한다.
3. DT 위치 정확도를 평가하려면 type-level 도구명을 physical instance ID로 분리한 initial inventory와 endpoint label을 보강한다.
4. Recovery와 specialized group action은 confirmed target이 부족하므로 별도 평가 가능한 이벤트 라벨을 추가한다.
5. 외부 일반화 주장을 위해 다른 세션·집도의·시점의 untouched test set과 반복 seed 실험을 별도로 구성한다.

## 산출물

- `case_metrics.csv`: 케이스별 wide-form 지표
- `metric_rows.csv`: 원본 scorecard long-form
- `aggregate_metrics.csv`: micro/macro 집계
- `tool_accuracy_by_case.png`, `phase_accuracy_by_case.png`, `latency_by_case.png`, `behavior_quality_by_case.png`
- `batch_provenance.json`: 입력 run, 모델, 코드 hash, 생성 파일 hash
"""


def generate(runs_root: Path, output_dir: Path, expected_case_ids: list[str]) -> None:
    paths = sorted(
        [path for path in runs_root.iterdir() if (path / "run_manifest.json").exists()],
        key=lambda path: _case_number(_read_json(path / "run_manifest.json")["case_id"]),
    )
    cases = [load_case(path) for path in paths]
    if not cases:
        raise ValueError(f"no complete shadow runs found under {runs_root}")
    validation = validate_cases(cases, expected_case_ids)
    if not validation["ok"]:
        raise ValueError("batch validation failed: " + "; ".join(validation["errors"]))

    output_dir.mkdir(parents=True, exist_ok=True)
    case_rows = _case_rows(cases)
    case_fields = list(case_rows[0])
    _write_csv(output_dir / "case_metrics.csv", case_rows, case_fields)

    metric_rows: list[dict[str, Any]] = []
    for case in cases:
        with (case.path / "shadow_scorecard.csv").open(
            newline="", encoding="utf-8"
        ) as stream:
            for row in csv.DictReader(stream):
                metric_rows.append({"case_id": case.case_id, **row})
    metric_fields = list(metric_rows[0])
    _write_csv(output_dir / "metric_rows.csv", metric_rows, metric_fields)

    aggregates = [
        aggregate_score(cases, metric, layer)
        for _, metric, layer in METRIC_SPECS
    ]
    aggregate_fields = list(aggregates[0])
    _write_csv(output_dir / "aggregate_metrics.csv", aggregates, aggregate_fields)

    _plot_grouped(
        output_dir / "tool_accuracy_by_case.png",
        cases,
        ["tool_model_raw", "tool_operational", "tool_reducer", "tool_bt"],
        "Tool action selection by case",
    )
    _plot_grouped(
        output_dir / "phase_accuracy_by_case.png",
        cases,
        ["phase_model_raw", "phase_operational", "phase_reducer"],
        "Phase estimation by case (provisional reference)",
    )
    _plot_latency(output_dir / "latency_by_case.png", cases, case_rows)
    _plot_behavior(output_dir / "behavior_quality_by_case.png", cases, case_rows)

    behavior_latency = _behavior_latencies(cases)
    report_path = output_dir / "taskplanner_12case_shadow_report_ko.md"
    report_path.write_text(
        _report(cases, case_rows, aggregates, validation, behavior_latency),
        encoding="utf-8",
    )

    artifact_names = [
        "case_metrics.csv",
        "metric_rows.csv",
        "aggregate_metrics.csv",
        "tool_accuracy_by_case.png",
        "phase_accuracy_by_case.png",
        "latency_by_case.png",
        "behavior_quality_by_case.png",
        "taskplanner_12case_shadow_report_ko.md",
    ]
    provenance = {
        "schema": "taskplanner.shadow_multicase_report.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "runs_root": str(runs_root.resolve()),
        "case_ids": [case.case_id for case in cases],
        "run_ids": [case.manifest["run_id"] for case in cases],
        "validation": validation,
        "behavior_latency": behavior_latency,
        "artifacts": {
            name: {"sha256": _sha256(output_dir / name)}
            for name in artifact_names
        },
    }
    (output_dir / "batch_provenance.json").write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--expected-cases",
        nargs="+",
        default=[f"0704_{number}" for number in range(6, 18)],
    )
    args = parser.parse_args()
    generate(args.runs_root, args.output_dir, args.expected_cases)
    print(args.output_dir.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
