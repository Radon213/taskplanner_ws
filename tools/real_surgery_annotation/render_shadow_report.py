#!/usr/bin/env python3
"""Render a compact Markdown table and SVG timeline from one shadow run."""

from __future__ import annotations

import argparse
from html import escape
import json
from pathlib import Path
from typing import Any


LAYER_ORDER = (
    "vlm_model_raw",
    "vlm_raw",
    "reducer_fused",
    "bt_decision",
    "skill_command",
)
LAYER_LABELS = {
    "vlm_model_raw": "VLM model raw (input time)",
    "vlm_raw": "VLM operational (publish time)",
    "reducer_fused": "Reducer fused",
    "bt_decision": "BT decision",
    "skill_command": "Skill command",
}
OUTCOME_COLORS = {
    "exact_match": "#2ca56f",
    "wrong_prediction": "#e3a128",
    "missed_opportunity": "#d95b5b",
    "unsafe_or_impossible": "#bd3b4f",
    "needs_human_adjudication": "#7c6de0",
    "not_evaluable": "#8290a3",
}


def _percent(value: Any) -> str:
    if value is None:
        return "N/A"
    return f"{100.0 * float(value):.1f}%"


def _number(value: Any) -> str:
    if value is None:
        return "N/A"
    return f"{float(value):.3f}"


def render_markdown(
    *,
    manifest: dict[str, Any],
    evaluation: dict[str, Any],
) -> str:
    runtime = evaluation.get("runtime", {})
    input_integrity = (
        manifest.get("runtime", {}).get("input_integrity", {})
    )

    def trace_coverage(label: str) -> str:
        payload = input_integrity.get(f"{label}_coverage", {})
        ratio = payload.get("coverage_ratio")
        dropped = payload.get("dropped", 0)
        return f"{_percent(ratio)} ({dropped} dropped)"

    lines = [
        f"# Shadow Replay Report: {manifest['case_id']}",
        "",
        "## Run",
        "",
        f"- Run ID: `{manifest['run_id']}`",
        f"- Mode: `{manifest['mode']}`",
        f"- Status: `{manifest['status']}`",
        f"- Reference authority: `{evaluation.get('reference_authority', 'unknown')}`",
        f"- Ground truth visible at runtime: `{manifest['reference']['runtime_visible']}`",
        f"- Source MCAP SHA-256: `{manifest['source_bag']['sha256']}`",
        "",
        "## Runtime",
        "",
        f"- Input frames: {runtime.get('input_image_count', 0)}",
        f"- Source transcripts / admitted speech: "
        f"{runtime.get('source_transcript_count', 0)} / "
        f"{runtime.get('admitted_speech_count', 0)}",
        f"- VLM results during input / total: "
        f"{runtime.get('vlm_result_during_input_count', 0)} / "
        f"{runtime.get('vlm_result_count', 0)} "
        f"({_number(runtime.get('vlm_effective_rate_hz'))} Hz)",
        f"- VLM unhealthy during input / post-input: "
        f"{runtime.get('vlm_unhealthy_during_input_count', 0)} / "
        f"{runtime.get('vlm_unhealthy_post_input_count', 0)}",
        f"- VLM latency median / p95: "
        f"{_number(runtime.get('vlm_latency_sec', {}).get('median'))} / "
        f"{_number(runtime.get('vlm_latency_sec', {}).get('p95'))} s",
        f"- Replay source / wall time: "
        f"{_number(runtime.get('replay_source_duration_sec'))} / "
        f"{_number(runtime.get('replay_wall_elapsed_sec'))} s "
        f"({_number(runtime.get('replay_realtime_factor'))}x real time)",
        f"- Elastic hold total / breakdown: "
        f"{_number(runtime.get('replay_elastic_hold_sec'))} s / "
        f"{runtime.get('replay_hold_breakdown_sec', {})}",
        f"- Trace contract errors: {runtime.get('trace_contract_error_count', 0)}",
        f"- Trace input coverage (field / FLIR / CAM4 / bbox / segmentation): "
        f"{trace_coverage('field_image')} / "
        f"{trace_coverage('flir_image')} / "
        f"{trace_coverage('cam4_image')} / "
        f"{trace_coverage('bbox')} / "
        f"{trace_coverage('segmentation')}",
        f"- Skill commands / semantic admissions / duplicate-suppressed: "
        f"{runtime.get('skill_command_count', 0)} / "
        f"{runtime.get('skill_command_semantic_admission_count', 0)} / "
        f"{runtime.get('skill_command_duplicate_suppressed_count', 0)} "
        f"({_percent(runtime.get('skill_command_duplicate_rate'))})",
        f"- Tool-instance inventory assumptions: "
        f"{runtime.get('skill_command_instance_resolution_assumed_count', 0)}",
        f"- Shadow state reconciliations from public requests: "
        f"{runtime.get('shadow_state_assumption_count', 0)} "
        f"{runtime.get('shadow_state_assumption_counts', {})}",
        f"- Shadow state reconciliation GT use: "
        f"{runtime.get('shadow_state_assumption_ground_truth_use_count', 0)}",
        f"- Counterfactual events / completed statuses: "
        f"{runtime.get('counterfactual_skill_event_count', 0)} / "
        f"{runtime.get('counterfactual_success_status_count', 0)} "
        f"(GT-independent, no physical execution)",
        f"- Commands after completion: "
        f"{runtime.get('skill_command_after_completion_count', 0)}",
        "",
        "## Tool Decision Layers",
        "",
        "| Layer | Exact / target | Top-1 | Stable | Request-backed | Anticipatory | Wrong | Missed | Unsafe | Unmatched predictions | Other actions | Median lead (s) |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for layer in LAYER_ORDER:
        payload = evaluation.get("layers", {}).get(layer, {})
        if (
            layer == "vlm_model_raw"
            and payload.get("prediction_record_count", 0) <= 0
        ):
            continue
        outcomes = payload.get("outcomes", {})
        lines.append(
            "| "
            + " | ".join(
                (
                    LAYER_LABELS[layer],
                    f"{outcomes.get('exact_match', 0)} / {payload.get('target_count', 0)}",
                    _percent(payload.get("top1_exact_rate")),
                    _percent(payload.get("stable_exact_rate")),
                    str(payload.get("request_backed_exact_count", 0)),
                    str(payload.get("anticipatory_exact_count", 0)),
                    str(outcomes.get("wrong_prediction", 0)),
                    str(outcomes.get("missed_opportunity", 0)),
                    str(outcomes.get("unsafe_or_impossible", 0)),
                    str(payload.get("false_positive_count", 0)),
                    str(payload.get("non_handover_action_episode_count", 0)),
                    _number(
                        payload.get("first_correct_lead_sec", {}).get("median")
                    ),
                )
            )
            + " |"
        )
    vlm_tool_layer = evaluation.get("layers", {}).get("vlm_raw", {})
    lines.extend(
        [
            "",
            "- VLM exact tool matches by evidence timing: "
            f"{vlm_tool_layer.get('request_backed_exact_count', 0)} "
            "request-backed, "
            f"{vlm_tool_layer.get('anticipatory_exact_count', 0)} "
            "anticipatory. Top-1 combines both and must not be read as "
            "anticipatory accuracy alone.",
        ]
    )
    scorecard = evaluation.get("scorecard", {})
    scorecard_rows: list[tuple[str, str, dict[str, Any], str]] = []
    phase_score = scorecard.get("phase_estimation", {})
    for layer, payload in phase_score.get("layers", {}).items():
        scorecard_rows.append(
            (
                "Phase estimation",
                LAYER_LABELS.get(layer, layer),
                payload,
                str(phase_score.get("status") or "unknown"),
            )
        )
    tool_score = scorecard.get("next_tool_prediction", {})
    for layer, payload in tool_score.get("layers", {}).items():
        scorecard_rows.append(
            (
                "Next-tool prediction",
                LAYER_LABELS.get(layer, layer),
                payload,
                "complete",
            )
        )
    model_raw_intent_score = scorecard.get(
        "model_raw_intent_recognition",
        {},
    )
    if model_raw_intent_score.get("status") not in {
        None,
        "",
        "not_available",
    }:
        scorecard_rows.append(
            (
                "Intent recognition",
                "VLM model raw (input time)",
                model_raw_intent_score,
                str(
                    model_raw_intent_score.get("status")
                    or "unknown"
                ),
            )
        )
    intent_score = scorecard.get("intent_recognition", {})
    scorecard_rows.append(
        (
            "Intent recognition",
            "VLM operational (publish time)",
            intent_score,
            str(intent_score.get("status") or "unknown"),
        )
    )
    dt_score = scorecard.get("dt_tool_management", {})
    scorecard_rows.append(
        (
            "DT tool endpoint",
            "Reducer fused",
            {
                "correct_count": dt_score.get("correct_count"),
                "evaluated_count": dt_score.get("evaluated_count"),
                "accuracy": dt_score.get("endpoint_accuracy"),
            },
            str(dt_score.get("status") or "unknown"),
        )
    )
    inventory_contract = dt_score.get("inventory_contract", {})
    inventory_accuracy = dt_score.get("instance_inventory_accuracy")
    expected_instances = int(
        inventory_contract.get("physical_instance_count", 0) or 0
    )
    scorecard_rows.append(
        (
            "DT inventory conservation",
            "Reducer fused",
            {
                "correct_count": (
                    round(float(inventory_accuracy) * expected_instances)
                    if inventory_accuracy is not None
                    else 0
                ),
                "evaluated_count": expected_instances,
                "accuracy": inventory_accuracy,
            },
            str(
                dt_score.get("instance_inventory_status")
                or "unknown"
            ),
        )
    )
    command_score = scorecard.get("command_fulfillment", {})
    scorecard_rows.append(
        (
            "Command fulfillment",
            "Skill execution",
            {
                "correct_count": command_score.get("fulfilled_count"),
                "evaluated_count": command_score.get("command_count"),
                "accuracy": command_score.get("fulfillment_rate"),
            },
            str(command_score.get("status") or "unknown"),
        )
    )
    lines.extend(
        [
            "",
            "## Integrated Scorecard",
            "",
            "| Metric | Layer | Correct / evaluable | Accuracy | Status |",
            "|---|---|---:|---:|---|",
        ]
    )
    for metric, layer, payload, status in scorecard_rows:
        lines.append(
            "| "
            + " | ".join(
                (
                    metric,
                    layer,
                    f"{payload.get('correct_count', 0)} / "
                    f"{payload.get('evaluated_count', 0)}",
                    _percent(payload.get("accuracy")),
                    status,
                )
            )
            + " |"
        )
    if intent_score:
        lines.extend(
            [
                "",
                "- Intent precision / recall / F1: "
                f"{_percent(intent_score.get('precision'))} / "
                f"{_percent(intent_score.get('recall'))} / "
                f"{_percent(intent_score.get('f1'))}; "
                f"false-positive episodes: "
                f"{intent_score.get('false_positive_episode_count', 0)}.",
            ]
        )
    if command_score:
        lines.append(
            "- Skill commands emitted / admitted / non-admitted: "
            f"{command_score.get('emitted_command_count', 0)} / "
            f"{command_score.get('command_count', 0)} / "
            f"{command_score.get('non_admitted_command_count', 0)}."
        )
    recovery = evaluation.get("recovery_audit", {})
    lines.extend(
        [
            "",
            "## Recovery Action Audit",
            "",
            f"- Recovery commands: {recovery.get('recovery_action_count', 0)}",
            f"- Severity counts: {recovery.get('severity_counts', {})}",
            f"- Reuse warning window: "
            f"{_number(recovery.get('reuse_warning_sec'))} s",
            "",
            "| Time (s) | Tool | Observable state | Next handover (s) | Severity | Outcome |",
            "|---:|---|---|---:|---|---|",
        ]
    )
    for action in recovery.get("actions", []):
        lines.append(
            "| "
            + " | ".join(
                (
                    _number(action.get("time_sec")),
                    str(action.get("tool_id") or "unknown"),
                    str(action.get("observable_feasibility") or "unknown"),
                    _number(action.get("next_confirmed_handover_after_sec")),
                    str(action.get("severity") or "unknown"),
                    str(action.get("outcome") or "unknown"),
                )
            )
            + " |"
        )
    vlm_non_exact = [
        event
        for event in evaluation.get("layers", {})
        .get("vlm_raw", {})
        .get("events", [])
        if event.get("outcome") != "exact_match"
    ]
    lines.extend(
        [
            "",
            "## VLM Non-exact Events",
            "",
            "| Time (s) | Target | VLM proposal | Outcome |",
            "|---:|---|---|---|",
        ]
    )
    for event in vlm_non_exact:
        lines.append(
            "| "
            + " | ".join(
                (
                    _number(event.get("time_sec")),
                    str(event.get("target_tool_id") or "unknown"),
                    str(event.get("predicted_tool_id") or "none"),
                    str(event.get("outcome") or "unknown"),
                )
            )
            + " |"
        )
    phase = evaluation.get("phase", {})
    lines.extend(
        [
            "",
            "## Phase Evaluation",
            "",
            (
                "Phase ground truth is not available for this case; phase "
                "accuracy is intentionally not reported."
                if phase.get("status") == "not_available"
                else json.dumps(phase, ensure_ascii=False, sort_keys=True)
            ),
            "",
            "## Interpretation",
            "",
            "- Strict results are end-to-end results; reconciled and oracle runs are reported separately.",
            "- One tool-prediction or handover episode can match at most one confirmed handover.",
            "- Recovery commands are excluded from handover false positives and audited against observable state separately.",
            "- Post-event predictions and stale predictions are excluded from a target match.",
            "- Dataset tool IDs and procedure tool refs are normalized through the hashed tool catalog.",
            "",
        ]
    )
    return "\n".join(lines)


def render_timeline_svg(
    *,
    manifest: dict[str, Any],
    evaluation: dict[str, Any],
) -> str:
    render_layers = [
        layer
        for layer in LAYER_ORDER
        if (
            layer != "vlm_model_raw"
            or evaluation.get("layers", {})
            .get(layer, {})
            .get("prediction_record_count", 0)
            > 0
        )
    ]
    width = 1600
    left = 250
    right = 70
    top = 145
    row_height = 115
    target_legend_rows = 2
    height = (
        top
        + row_height * len(render_layers)
        + 75
        + target_legend_rows * 34
        + 70
    )
    events = [
        event
        for layer in render_layers
        for event in evaluation.get("layers", {}).get(layer, {}).get("events", [])
    ]
    maximum = max(
        [float(event.get("time_sec", 0.0)) for event in events]
        + [float(manifest.get("runtime", {}).get("source_duration_sec", 1.0)), 1.0]
    )
    plot_width = width - left - right

    def x_position(seconds: float) -> float:
        return left + (max(0.0, min(maximum, seconds)) / maximum) * plot_width

    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        "<style>",
        "text{font-family:Inter,Arial,sans-serif;fill:#102133;letter-spacing:0}",
        ".title{font-size:28px;font-weight:700}.subtitle{font-size:16px;fill:#526476}",
        ".label{font-size:18px;font-weight:650}.small{font-size:13px;fill:#526476}",
        ".axis{stroke:#adc0d0;stroke-width:1}.track{stroke:#dce6ee;stroke-width:2}",
        "</style>",
        '<rect width="100%" height="100%" fill="#f8fbfd"/>',
        f'<text x="55" y="55" class="title">Shadow Replay Decision Timeline</text>',
        (
            f'<text x="55" y="88" class="subtitle">'
            f'{escape(str(manifest["case_id"]))} · {escape(str(manifest["mode"]))} · '
            f'{escape(str(manifest["run_id"]))}</text>'
        ),
    ]
    for tick in range(0, 6):
        seconds = maximum * tick / 5.0
        x = x_position(seconds)
        svg.append(
            f'<line x1="{x:.2f}" y1="{top - 34}" x2="{x:.2f}" '
            f'y2="{height - 72}" class="axis" opacity="0.35"/>'
        )
        svg.append(
            f'<text x="{x:.2f}" y="{top - 46}" text-anchor="middle" '
            f'class="small">{seconds:.1f}s</text>'
        )

    for index, layer in enumerate(render_layers):
        y = top + index * row_height
        layer_payload = evaluation.get("layers", {}).get(layer, {})
        exact_count = layer_payload.get("outcomes", {}).get(
            "exact_match",
            0,
        )
        target_count = layer_payload.get("target_count", 0)
        svg.append(
            f'<text x="55" y="{y + 7}" class="label">'
            f'{escape(LAYER_LABELS[layer])}</text>'
        )
        svg.append(
            f'<text x="55" y="{y + 29}" class="small">'
            f'{exact_count}/{target_count} exact</text>'
        )
        svg.append(
            f'<line x1="{left}" y1="{y}" x2="{width - right}" y2="{y}" class="track"/>'
        )
        layer_events = layer_payload.get("events", [])
        for event_index, event in enumerate(layer_events, start=1):
            target_time = float(event.get("time_sec", 0.0))
            target_x = x_position(target_time)
            lead = event.get("first_correct_lead_sec")
            outcome = str(event.get("outcome", "not_evaluable"))
            color = OUTCOME_COLORS.get(outcome, "#8290a3")
            prediction_x = target_x
            if lead is not None:
                prediction_x = x_position(target_time - float(lead))
                svg.append(
                    f'<line x1="{prediction_x:.2f}" y1="{y}" '
                    f'x2="{target_x:.2f}" y2="{y}" '
                    f'stroke="{color}" stroke-width="5" opacity="0.45"/>'
                )
            svg.append(
                f'<g><title>{escape(str(event.get("event_id", "")))}: '
                f'{escape(str(event.get("predicted_tool_id") or "none"))} to '
                f'{escape(str(event.get("target_tool_id") or "unknown"))} '
                f'({escape(outcome)})</title>'
                f'<circle cx="{target_x:.2f}" cy="{y}" r="10" '
                f'fill="white" stroke="{color}" stroke-width="4"/>'
                f'<text x="{target_x:.2f}" y="{y + 3.5}" '
                f'text-anchor="middle" style="font-size:8px;font-weight:700">'
                f'{event_index}</text></g>'
            )
            if lead is not None:
                svg.append(
                    f'<circle cx="{prediction_x:.2f}" cy="{y}" r="8" fill="{color}"/>'
                )

    target_events = (
        evaluation.get("layers", {})
        .get("skill_command", {})
        .get("events", [])
    )
    target_legend_y = top + row_height * len(render_layers) + 23
    svg.append(
        f'<text x="55" y="{target_legend_y}" class="label">Confirmed handover targets</text>'
    )
    target_column_width = (width - 110) / 7.0
    for event_index, event in enumerate(target_events, start=1):
        row = (event_index - 1) // 7
        column = (event_index - 1) % 7
        x = 55 + column * target_column_width
        y = target_legend_y + 28 + row * 34
        tool = str(event.get("target_tool_id") or "unknown").replace("_", " ")
        svg.append(
            f'<text x="{x:.2f}" y="{y}" class="small">'
            f'<tspan style="font-weight:700">{event_index:02d}</tspan> '
            f'{escape(tool)} · {float(event.get("time_sec", 0.0)):.1f}s</text>'
        )

    legend_y = height - 30
    x = 55
    for outcome, color in OUTCOME_COLORS.items():
        svg.append(f'<circle cx="{x}" cy="{legend_y}" r="6" fill="{color}"/>')
        svg.append(
            f'<text x="{x + 12}" y="{legend_y + 5}" class="small">'
            f'{escape(outcome)}</text>'
        )
        x += 220
    svg.append("</svg>")
    return "\n".join(svg) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--evaluation", type=Path, required=True)
    parser.add_argument("--markdown", type=Path, required=True)
    parser.add_argument("--svg", type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    evaluation = json.loads(args.evaluation.read_text(encoding="utf-8"))
    args.markdown.parent.mkdir(parents=True, exist_ok=True)
    args.svg.parent.mkdir(parents=True, exist_ok=True)
    args.markdown.write_text(
        render_markdown(manifest=manifest, evaluation=evaluation),
        encoding="utf-8",
    )
    args.svg.write_text(
        render_timeline_svg(manifest=manifest, evaluation=evaluation),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
