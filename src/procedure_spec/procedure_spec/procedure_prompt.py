"""Optional compact procedure-prompt assets for VLM/LLM runtime prompts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


PROMPT_FILE_NAMES = ("vlm_procedure_prompt.yaml", "procedure_prompt.yaml")


def load_procedure_prompt(bundle_dir: str | Path) -> dict[str, Any]:
    """Load the optional procedure prompt YAML colocated with a procedure spec."""
    bundle_path = Path(bundle_dir)
    for name in PROMPT_FILE_NAMES:
        prompt_path = bundle_path / name
        if not prompt_path.is_file():
            continue
        with prompt_path.open("r", encoding="utf-8") as handle:
            payload = yaml.safe_load(handle) or {}
        if isinstance(payload, dict):
            return payload
        raise ValueError(f"{prompt_path} must contain a YAML mapping.")
    return {}


def compact_procedure_prompt(bundle_dir: str | Path) -> dict[str, Any]:
    """Return a token-conscious prompt block with doctor flow plus runtime crosswalk."""
    payload = load_procedure_prompt(bundle_dir)
    if not payload:
        return {}

    tools = payload.get("tools", {})
    phase_labels = payload.get("phase_labels", {})
    normal_phases = phase_labels.get("normal", {}) if isinstance(phase_labels, dict) else {}
    interrupt_phases = phase_labels.get("interrupt", {}) if isinstance(phase_labels, dict) else {}

    flow: list[list[Any]] = []
    phase_flow = payload.get("phase_flow", {})
    if isinstance(phase_flow, dict):
        for transition in phase_flow.get("normal_sequence", []) or []:
            if not isinstance(transition, dict):
                continue
            cues = transition.get("transition_cues", {}) or {}
            flow.append(
                [
                    str(transition.get("from", "")),
                    str(transition.get("to", "")),
                    str(cues.get("tool_cue", "")),
                ]
            )
        interrupt = phase_flow.get("interrupt_transition", {}) or {}
        if isinstance(interrupt, dict):
            flow.append(
                [
                    "*",
                    str(interrupt.get("phase", "")),
                    str((interrupt.get("enter_when", {}) or {}).get("tool_cue", "")),
                ]
            )

    phase_sequences: dict[str, list[list[str]]] = {}
    for phase_id, detail in (payload.get("phase_details", {}) or {}).items():
        if not isinstance(detail, dict):
            continue
        rows: list[list[str]] = []
        for item in detail.get("expected_tool_sequence", []) or []:
            if not isinstance(item, dict):
                continue
            current_tool = str(item.get("current", ""))
            next_tool = str(item.get("next", ""))
            rows.append(
                [
                    current_tool,
                    next_tool,
                    str(item.get("cue", "")),
                ]
            )
        if rows:
            phase_sequences[str(phase_id)] = rows

    return {
        "id": str(payload.get("id", "thyroidectomy_procedure_prompt")),
        "procedure": str((payload.get("procedure", {}) or {}).get("name", "")),
        "phase_labels": {**normal_phases, **interrupt_phases},
        "runtime_phase": {str(phase_id): str(phase_id) for phase_id in {**normal_phases, **interrupt_phases}},
        "tools": {
            str(tool_id): {
                "n": str(name),
                "rt": str(tool_id),
            }
            for tool_id, name in (tools if isinstance(tools, dict) else {}).items()
        },
        "flow": flow,
        "seq": phase_sequences,
    }
