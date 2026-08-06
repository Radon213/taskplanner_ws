#!/usr/bin/env python3
"""Compare two shadow runs while excluding wall time and correlation ids."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

try:
    from .shadow_contract import canonical_json, load_jsonl, sha256_bytes
except ImportError:  # Support direct execution from the repository root.
    from shadow_contract import canonical_json, load_jsonl, sha256_bytes


SEMANTIC_LAYERS = (
    "vlm_model_raw",
    "vlm_raw",
    "reducer_fused",
    "bt_decision",
    "skill_command",
    "shadow_sink",
)


def _project_payload(layer: str, payload: dict[str, Any]) -> Any:
    if layer == "input_image":
        return {
            key: payload.get(key)
            for key in (
                "source",
                "frame_id",
                "format",
                "byte_count",
                "sha256",
                "header_stamp_sec",
            )
        }
    if layer == "input_transcript":
        raw = payload.get("data")
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                return {"data": raw}
            if isinstance(parsed, dict):
                return {
                    key: parsed.get(key)
                    for key in ("text", "start_sec", "end_sec", "utterance_id")
                    if key in parsed
                }
        return payload
    if layer in {"vlm_model_raw", "vlm_raw"}:
        return {
            "raw_json": payload.get("raw_json"),
            "phase_ids": payload.get("phase_ids"),
            "predicted_tool_id": payload.get("predicted_tool_id"),
            "gesture_event_type": payload.get("gesture_event_type"),
            "gesture_requested_tool": payload.get("gesture_requested_tool"),
        }
    if layer == "reducer_fused":
        running = bool(payload.get("running"))
        execution_state = payload.get("execution_state")
        safety_flags = payload.get("safety_flags")
        if not running and execution_state in {"completed", "halted"}:
            # Launch teardown can stop the VLM before the reducer. Ignore the
            # resulting terminal-only health flag; it cannot affect a command.
            safety_flags = []
        instruments = [
            {
                key: row.get(key)
                for key in (
                    "instrument_id",
                    "lifecycle_stage",
                    "location_type",
                    "location_id",
                    "owner",
                    "status",
                    "contaminated",
                    "next_required_transition",
                )
            }
            for row in payload.get("instrument_states", [])
            if isinstance(row, dict)
        ]
        projected = {
            key: payload.get(key)
            for key in (
                "running",
                "execution_state",
                "filtered_phase",
                "phase_uncertain",
                "predicted_tool",
                "explicit_request_tool",
                "surgeon_request_tool",
                "handover_allowed",
                "prepositioned_tool",
                "right_hand_tool",
                "left_hand_tool",
                "active_recovery_tools",
            )
        }
        projected["safety_flags"] = safety_flags
        projected["instrument_states"] = instruments
        return projected
    if layer == "bt_decision":
        return {
            key: payload.get(key)
            for key in (
                "decision",
                "action",
                "selected_tool",
                "selected_tool_lifecycle",
                "blocking_guard",
                "handover_allowed",
                "next_required_transition",
                "decision_reason",
            )
        }
    if layer == "skill_command":
        return {
            key: payload.get(key)
            for key in (
                "action",
                "instrument_id",
                "arm",
                "mode",
                "source_location_type",
                "source_location_id",
                "target_location_type",
                "target_location_id",
                "target_owner",
                "cleaning_required",
            )
        }
    if layer == "shadow_sink":
        return {
            key: payload.get(key)
            for key in (
                "action",
                "instrument_id",
                "arm",
                "status",
                "reason",
                "execution_attempted",
                "world_phase",
                "world_execution_state",
            )
        }
    return payload


def _collapse(values: list[Any]) -> list[Any]:
    result: list[Any] = []
    for value in values:
        if not result or result[-1] != value:
            result.append(value)
    return result


def semantic_trace_signature(records: list[dict[str, Any]]) -> dict[str, Any]:
    signature: dict[str, Any] = {}
    for layer in ("input_image", "input_transcript", *SEMANTIC_LAYERS):
        values = [
            _project_payload(layer, record.get("payload", {}))
            for record in records
            if record.get("layer") == layer
        ]
        if layer in SEMANTIC_LAYERS:
            values = _collapse(values)
        signature[layer] = values
    return signature


def semantic_evaluation_signature(report: dict[str, Any]) -> dict[str, Any]:
    layers: dict[str, Any] = {}
    for layer, payload in report.get("layers", {}).items():
        layers[layer] = {
            "target_count": payload.get("target_count"),
            "outcomes": payload.get("outcomes"),
            "false_positive_count": payload.get("false_positive_count"),
            "top1_exact_rate": payload.get("top1_exact_rate"),
            "stable_exact_count": payload.get("stable_exact_count"),
            "request_backed_exact_count": payload.get(
                "request_backed_exact_count"
            ),
            "proactive_exact_count": payload.get(
                "proactive_exact_count",
                payload.get("anticipatory_exact_count"),
            ),
            "post_request_visual_exact_count": payload.get(
                "post_request_visual_exact_count"
            ),
            "anticipatory_exact_count": payload.get(
                "anticipatory_exact_count"
            ),
            "events": [
                {
                    key: event.get(key)
                    for key in (
                        "event_id",
                        "target_tool_id",
                        "outcome",
                        "predicted_tool_id",
                        "predicted_raw_tool_id",
                        "predicted_action",
                        "prediction_source",
                        "feasibility",
                    )
                }
                for event in payload.get("events", [])
            ],
        }
    runtime = report.get("runtime", {})
    return {
        "mode": report.get("mode"),
        "status": report.get("status"),
        "confirmed_handover_count": report.get("confirmed_handover_count"),
        "layers": layers,
        "phase": report.get("phase"),
        "runtime": {
            key: runtime.get(key)
            for key in (
                "input_image_count",
                "input_transcript_count",
                "source_transcript_count",
                "admitted_speech_count",
                "vlm_result_count",
                "vlm_result_during_input_count",
                "vlm_unhealthy_count",
                "vlm_unhealthy_during_input_count",
                "vlm_unhealthy_post_input_count",
                "vlm_parse_retry_count",
                "skill_command_count",
                "skill_command_semantic_admission_count",
                "skill_command_duplicate_suppressed_count",
                "skill_command_duplicate_rate",
                "skill_command_instance_resolution_assumed_count",
                "counterfactual_skill_event_count",
                "counterfactual_success_status_count",
                "skill_command_after_completion_count",
                "trace_contract_error_count",
            )
        },
    }


def _digest(value: Any) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def compare_runs(run_a: Path, run_b: Path) -> dict[str, Any]:
    manifest_a = json.loads((run_a / "run_manifest.json").read_text(encoding="utf-8"))
    manifest_b = json.loads((run_b / "run_manifest.json").read_text(encoding="utf-8"))
    trace_a = semantic_trace_signature(
        load_jsonl(run_a / "shadow_trace.v1.jsonl")
    )
    trace_b = semantic_trace_signature(
        load_jsonl(run_b / "shadow_trace.v1.jsonl")
    )
    evaluation_a = semantic_evaluation_signature(
        json.loads(
            (run_a / "shadow_evaluation.v2.json").read_text(encoding="utf-8")
        )
    )
    evaluation_b = semantic_evaluation_signature(
        json.loads(
            (run_b / "shadow_evaluation.v2.json").read_text(encoding="utf-8")
        )
    )
    checks = {
        "same_mode": manifest_a.get("mode") == manifest_b.get("mode"),
        "same_source_bag_hash": (
            manifest_a.get("source_bag", {}).get("sha256")
            == manifest_b.get("source_bag", {}).get("sha256")
        ),
        "same_reference_hash": (
            manifest_a.get("reference", {}).get("event_file", {}).get("sha256")
            == manifest_b.get("reference", {}).get("event_file", {}).get("sha256")
        ),
        "same_semantic_trace": trace_a == trace_b,
        "same_semantic_evaluation": evaluation_a == evaluation_b,
    }
    layer_checks = {
        layer: trace_a.get(layer) == trace_b.get(layer)
        for layer in trace_a
    }
    return {
        "schema": "taskplanner.shadow_determinism_comparison.v1",
        "ok": all(checks.values()),
        "run_a": str(run_a.resolve()),
        "run_b": str(run_b.resolve()),
        "checks": checks,
        "semantic_layer_checks": layer_checks,
        "digests": {
            "trace_a": _digest(trace_a),
            "trace_b": _digest(trace_b),
            "evaluation_a": _digest(evaluation_a),
            "evaluation_b": _digest(evaluation_b),
        },
        "notes": [
            "Wall time, ROS timing jitter, sequence ids, command ids, and repeated identical ticks are excluded.",
            "Public input fingerprints and collapsed semantic VLM/reducer/BT/skill trajectories must match.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-a", type=Path, required=True)
    parser.add_argument("--run-b", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = compare_runs(args.run_a.resolve(), args.run_b.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
