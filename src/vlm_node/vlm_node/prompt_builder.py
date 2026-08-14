"""Procedure-aware prompt builder for the real VLM node."""

from __future__ import annotations

from pathlib import Path

from procedure_spec import ProcedureSpec, load_bundle

from .common import tool_display_name


class PromptBuilder:
    def __init__(self) -> None:
        self._cache: dict[str, str] = {}

    def build(self, spec_dir: str | Path) -> str:
        spec_path = str(Path(spec_dir))
        if spec_path not in self._cache:
            self._cache[spec_path] = self._build_prompt(load_bundle(spec_path), Path(spec_path))
        return self._cache[spec_path]

    def _build_prompt(self, spec: ProcedureSpec, spec_path: Path) -> str:
        procedure_lines = [
            f"Procedure: {spec.bundle.procedure_display_name} ({spec.procedure_id})",
            "You are a surgical perception model. Read image cues and the compact context JSON together.",
            "Return JSON only. Never explain your reasoning in prose.",
        ]

        phase_lines = ["Phases:"]
        for phase in spec.bundle.phases:
            expected = ", ".join(phase.expected_instruments) or "none"
            phase_lines.append(
                f"- {phase.id}: expected tools [{expected}], next [{', '.join(phase.possible_next)}]"
            )

        instrument_lines = ["Instrument ontology:"]
        for instrument in spec.bundle.instruments:
            aliases = ", ".join(sorted({instrument.id, *instrument.aliases}))
            instrument_lines.append(
                f"- {instrument.id} ({tool_display_name(instrument.id)}), aliases [{aliases}]"
            )

        location_lines = [
            "Location ontology:",
            "- tray_slot: home rack slot",
            "- mayo_stand: the single physical Mayo stand location",
            "- Mayo reuse versus recovery is a policy state, not a location",
            "- surgeon: surgeon-held/used tool; do not infer hand versus field",
            "- cleaner_slot: left-hand occupied tool held in cleaner",
            "- robot_right_hand: anticipatory/prepositioned or handover-ready tool",
            "- robot_left_hand: recovered tool on way to cleaner or rack",
        ]

        gesture_lines = [
            "Gesture semantics:",
            "- sg[0] request_tool means surgeon requests a tool handover.",
            "- sg[0] return_tool means surgeon presents a used tool for recovery.",
            "- hand_pose open_receive supports request_tool.",
            "- hand_pose present_return supports return_tool.",
            "- If there is no reliable gesture, emit sg as ['', '', '', 0.0].",
        ]

        schema_lines = [
            "Schema:",
            '{\"v\":\"1\",\"ph\":[[phase_id,confidence],...],\"to\":[[tool_id,location_id,location_type,confidence],...],\"sg\":[event_type,requested_tool,hand_pose,confidence],\"u\":uncertainty,\"sum\":\"optional short note\"}',
            "Keep output compact. Prefer only likely visible tools. Use exact tool ids and location ids from context.",
        ]

        optional_context = self._optional_context_block(spec_path)
        blocks = [
            "\n".join(procedure_lines),
            "\n".join(phase_lines),
            "\n".join(instrument_lines),
            "\n".join(location_lines),
            "\n".join(gesture_lines),
            "\n".join(schema_lines),
        ]
        if optional_context:
            blocks.append(f"Procedure note:\n{optional_context}")
        return "\n\n".join(blocks)

    def _optional_context_block(self, spec_path: Path) -> str:
        for candidate_name in ("vlm_context.md", "procedure_note.md"):
            candidate = spec_path / candidate_name
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8").strip()
        return ""
