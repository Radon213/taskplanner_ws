"""Read-only projection of the DT's live n-gram policy, not another reducer."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


TOOL_POLICY_STATUS_TOPIC = "/twin/tool_policy_status"
TOOL_POLICY_STATUS_SCHEMA = "taskplanner.tool_policy_status.v1"
TOOL_POLICY_PARAMETER_NAMES = frozenset({
    "ngram_prepare_probability_threshold",
    "ngram_recovery_probability_threshold",
    "ngram_recovery_enabled_tools",
    "ngram_policy_stability_sec",
})


def tool_policy_status_payload(
    *,
    state: Any,
    instruments: Mapping[str, Any],
    prepare_probability_threshold: float,
    recovery_probability_threshold: float,
    dwell_sec: float,
    recovery_enabled_tools: frozenset[str],
    recovery_dwell: Mapping[str, Mapping[str, float]],
) -> dict:
    """Copy policy configuration and reducer-measured dwell into one snapshot.

    Recovery probability is p(next tool), compared *below* its threshold. It
    is neither a camera detection score nor the VLM's surgeon-demand forecast.
    No clock, scorer, admission rule or state mutation belongs in this view.
    """
    run_id = str(state.procedure_run_id)
    execution_state = str(state.execution_state)
    active = bool(state.running and execution_state == "running" and run_id)
    preparation = None
    if active and state.predicted_tool:
        preparation = {
            "instrument_id": str(state.predicted_tool),
            "probability": float(state.predicted_tool_confidence),
            "stability_sec": float(state.predicted_tool_stability_sec),
        }
    recovery = []
    if active:
        for instance_id, entry in sorted(recovery_dwell.items()):
            instrument = instruments.get(instance_id)
            if instrument is None or instrument.instrument_id not in recovery_enabled_tools:
                continue
            recovery.append({
                "instrument_id": str(instrument.instrument_id),
                "instance_id": str(instance_id),
                "probability": float(entry["probability"]),
                "stability_sec": max(
                    0.0, float(entry["last_seen"]) - float(entry["first_seen"])
                ),
            })
    return {
        "schema": TOOL_POLICY_STATUS_SCHEMA,
        "source": "handover_ngram_0704",
        "procedure_id": str(state.procedure_id),
        "procedure_run_id": run_id,
        "running": bool(state.running),
        "execution_state": execution_state,
        "prepare": {
            "probability_threshold": float(prepare_probability_threshold),
            "comparison": "gte",
            "dwell_sec": float(dwell_sec),
            "candidate": preparation,
        },
        "recovery": {
            "probability_threshold": float(recovery_probability_threshold),
            "comparison": "lte",
            "dwell_sec": float(dwell_sec),
            "enabled_instrument_ids": sorted(recovery_enabled_tools),
            "candidates": recovery,
        },
    }
