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
    tool_inventory = payload.get("tool_inventory", {})
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
            interrupt_phase = str(interrupt.get("phase", ""))
            if interrupt_phase:
                flow.append(
                    [
                        "*",
                        interrupt_phase,
                        str((interrupt.get("enter_when", {}) or {}).get("tool_cue", "")),
                    ]
                )

    phase_sequences: dict[str, list[list[str]]] = {}
    phase_cues: dict[str, list[str]] = {}
    phase_exclusions: dict[str, list[str]] = {}
    phase_tool_roles: dict[str, dict[str, list[str]]] = {}
    for phase_id, detail in (payload.get("phase_details", {}) or {}).items():
        if not isinstance(detail, dict):
            continue
        cues = [
            str(cue).strip()
            for cue in detail.get("visual_cues", []) or []
            if str(cue).strip()
        ]
        if cues:
            # Two short cues are enough to preserve the discriminative visual
            # contract without rebuilding the full authoring document.
            phase_cues[str(phase_id)] = cues[:2]
        exclusions = [
            str(cue).strip()
            for cue in detail.get("exclusion_cues", []) or []
            if str(cue).strip()
        ]
        if exclusions:
            phase_exclusions[str(phase_id)] = exclusions[:2]
        raw_tool_roles = detail.get("tool_roles", {}) or {}
        if isinstance(raw_tool_roles, dict):
            compact_roles: dict[str, list[str]] = {}
            for role, tool_ids in raw_tool_roles.items():
                if not isinstance(tool_ids, list):
                    continue
                normalized_ids = [
                    str(tool_id).strip()
                    for tool_id in tool_ids
                    if str(tool_id).strip()
                ]
                if normalized_ids:
                    compact_roles[str(role)] = normalized_ids
            if compact_roles:
                phase_tool_roles[str(phase_id)] = compact_roles
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
                    str(item.get("strength", "medium")),
                ]
            )
        if rows:
            phase_sequences[str(phase_id)] = rows

    bed_robot_arm_groups = payload.get("bed_robot_arm_groups", {})
    if not isinstance(bed_robot_arm_groups, dict):
        bed_robot_arm_groups = {}

    raw_inference_policy = payload.get("phase_inference_policy", {}) or {}
    phase_inference_policy = (
        {
            str(key): value
            for key, value in raw_inference_policy.items()
            if isinstance(value, (str, int, float, bool, list, dict))
        }
        if isinstance(raw_inference_policy, dict)
        else {}
    )
    raw_phase_groups = payload.get("phase_groups", {}) or {}
    phase_groups = (
        {
            str(group_id): group
            for group_id, group in raw_phase_groups.items()
            if isinstance(group, dict)
        }
        if isinstance(raw_phase_groups, dict)
        else {}
    )

    raw_handover_patterns = payload.get("handover_patterns", {}) or {}
    handover_patterns: dict[str, list[list[str]]] = {}
    if isinstance(raw_handover_patterns, dict):
        for strength in ("primary", "alternatives"):
            normalized_paths: list[list[str]] = []
            for path in raw_handover_patterns.get(strength, []) or []:
                if not isinstance(path, list):
                    continue
                normalized = [
                    str(tool_id).strip()
                    for tool_id in path
                    if str(tool_id).strip()
                ]
                if len(normalized) >= 2:
                    normalized_paths.append(normalized)
            if normalized_paths:
                handover_patterns[strength] = normalized_paths

    return {
        "id": str(payload.get("id", "thyroidectomy_procedure_prompt")),
        "procedure": str((payload.get("procedure", {}) or {}).get("name", "")),
        "phase_labels": {**normal_phases, **interrupt_phases},
        "runtime_phase": {str(phase_id): str(phase_id) for phase_id in {**normal_phases, **interrupt_phases}},
        "tools": {
            str(tool_id): {
                "n": str(name),
                "rt": str(tool_id),
                "q": int(
                    tool_inventory.get(tool_id, 1)
                    if isinstance(tool_inventory, dict)
                    else 1
                ),
            }
            for tool_id, name in (tools if isinstance(tools, dict) else {}).items()
        },
        "flow": flow,
        "cues": phase_cues,
        "exclude": phase_exclusions,
        "roles": phase_tool_roles,
        "seq": phase_sequences,
        "phase_policy": phase_inference_policy,
        "phase_groups": phase_groups,
        "handover_patterns": handover_patterns,
        # Preserve the group-level scenario contract.  This intentionally has
        # no physical arm identifiers or arm-count assumptions.
        "bed_robot_arm_groups": bed_robot_arm_groups,
    }
