"""Procedure-derived phase and next-tool priors.

The scorer intentionally uses only procedure specs/prompts and public evidence.
It does not know about a surgeon actor's hidden state.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from .query_api import ProcedureSpec


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _rank(scores: dict[str, float], *, limit: int = 4) -> list[list[Any]]:
    cleaned = {key: _clamp01(value) for key, value in scores.items() if key and value > 0.0}
    if not cleaned:
        return []
    max_score = max(cleaned.values())
    if max_score <= 0.0:
        return []
    normalized = {key: min(1.0, value / max_score) for key, value in cleaned.items()}
    return [[key, round(value, 3)] for key, value in sorted(normalized.items(), key=lambda item: item[1], reverse=True)[:limit]]


_GENERIC_TOOL_TOKENS = {
    "tool",
    "instrument",
    "forceps",
    "clamp",
    "retractor",
    "cautery",
    "scissors",
    "pencil",
    "tip",
    "blade",
    "handle",
    "needle",
    "holder",
    "please",
    "next",
}


class ProcedurePriorScorer:
    """Compute generic phase/tool priors from procedure flow plus public evidence."""

    def __init__(self, spec: ProcedureSpec, procedure_prompt: dict[str, Any] | None = None) -> None:
        self.spec = spec
        self.prompt = procedure_prompt or {}
        self._requestable = {
            instrument.id
            for instrument in spec.bundle.instruments
            if bool(getattr(instrument, "requestable", True))
        }
        self._expected_by_phase = {
            phase.id: list(phase.expected_instruments)
            for phase in spec.bundle.phases
        }
        self._phase_display_by_id = {
            phase.id: " ".join(
                item
                for item in [phase.id, phase.display_name, getattr(phase, "display_name_ko", "")]
                if item
            )
            for phase in spec.bundle.phases
        }
        self._phase_aliases_by_id = {
            phase.id: [
                item
                for item in [phase.id, phase.display_name, getattr(phase, "display_name_ko", "")]
                if item
            ]
            for phase in spec.bundle.phases
        }
        for group in ("normal", "interrupt"):
            labels = (self.prompt.get("phase_labels", {}) or {}).get(group, {})
            if isinstance(labels, dict):
                for phase_id, label in labels.items():
                    if str(phase_id) in self._phase_display_by_id:
                        self._phase_display_by_id[str(phase_id)] += f" {label}"
                        self._phase_aliases_by_id.setdefault(str(phase_id), []).append(str(label))
            ko_labels = (self.prompt.get("phase_labels_ko", {}) or {}).get(group, {})
            if isinstance(ko_labels, dict):
                for phase_id, label in ko_labels.items():
                    if str(phase_id) in self._phase_display_by_id:
                        self._phase_display_by_id[str(phase_id)] += f" {label}"
                        self._phase_aliases_by_id.setdefault(str(phase_id), []).append(str(label))
        self._interrupt_phase_ids = set(getattr(spec, "interrupt_phase_ids", []))
        self._phase_by_tool: dict[str, set[str]] = defaultdict(set)
        for phase_id, tools in self._expected_by_phase.items():
            for tool_id in tools:
                self._phase_by_tool[tool_id].add(phase_id)
        alias_counts: dict[str, set[str]] = defaultdict(set)
        for instrument in spec.bundle.instruments:
            for alias in getattr(instrument, "aliases", []):
                normalized_alias = str(alias).strip().lower()
                if normalized_alias:
                    alias_counts[normalized_alias].add(instrument.id)
        self._ambiguous_aliases = {
            alias for alias, instrument_ids in alias_counts.items() if len(instrument_ids) > 1
        }
        self._runtime_phase = {
            str(key): str(value)
            for key, value in (self.prompt.get("runtime_phase", {}) or {}).items()
            if str(value)
        }
        phase_policy = self.prompt.get("phase_policy", {}) or {}
        if not isinstance(phase_policy, dict):
            phase_policy = {}
        self._tool_only_detailed_phase_transition_allowed = bool(
            phase_policy.get(
                "tool_only_detailed_phase_transition_allowed",
                True,
            )
        )
        self._tool_sequence_open_set_anchor_allowed = bool(
            phase_policy.get(
                "tool_sequence_open_set_anchor_allowed",
                True,
            )
        )
        self._sequence_rows: list[tuple[str, str, str]] = []
        self._sequence_tools_by_phase: dict[str, list[str]] = defaultdict(list)
        self._primary_sequence_by_phase: dict[str, list[str]] = defaultdict(list)
        for prompt_phase, rows in (self.prompt.get("seq", {}) or {}).items():
            runtime_phase = self._runtime_phase.get(str(prompt_phase), str(prompt_phase))
            if runtime_phase not in self.spec.phase_ids:
                continue
            for row in rows or []:
                if not isinstance(row, list) or len(row) < 2:
                    continue
                current_tool = self._resolve_tool(row[0])
                next_tool = self._resolve_tool(row[1])
                if current_tool or next_tool:
                    self._sequence_rows.append((runtime_phase, current_tool, next_tool))
                for tool_id in (current_tool, next_tool):
                    if (
                        tool_id
                        and tool_id in self._requestable
                        and tool_id not in self._sequence_tools_by_phase[runtime_phase]
                    ):
                        self._sequence_tools_by_phase[runtime_phase].append(tool_id)
                primary = self._primary_sequence_by_phase[runtime_phase]
                if not primary:
                    if current_tool in self._requestable:
                        primary.append(current_tool)
                    if next_tool in self._requestable:
                        primary.append(next_tool)
                elif current_tool == primary[-1] and next_tool in self._requestable:
                    primary.append(next_tool)

        self._handover_paths: list[tuple[str, int, list[str]]] = []
        raw_handover_patterns = self.prompt.get("handover_patterns", {}) or {}
        if isinstance(raw_handover_patterns, dict):
            for source in ("primary", "alternatives"):
                for path_index, raw_path in enumerate(
                    raw_handover_patterns.get(source, []) or []
                ):
                    if not isinstance(raw_path, list):
                        continue
                    path = [
                        tool_id
                        for tool_id in (
                            self._resolve_tool(raw_tool) for raw_tool in raw_path
                        )
                        if tool_id in self._requestable
                    ]
                    if len(path) >= 2:
                        self._handover_paths.append((source, path_index, path))
        if not any(source == "primary" for source, _, _ in self._handover_paths):
            for phase_id in self.spec.normal_phase_ids:
                path = list(self._primary_sequence_by_phase.get(phase_id, []))
                if len(path) >= 2:
                    self._handover_paths.append(("phase", len(self._handover_paths), path))

    def _resolve_tool(self, raw: Any) -> str:
        text = str(raw or "").strip()
        if not text:
            return ""
        return self.spec.resolve_instrument_alias(text) or (text if text in self.spec.list_instrument_ids() else "")

    def _tools_from_text(self, text: str) -> list[str]:
        lowered = text.lower()
        text_tokens = self._token_set(text)
        matches: list[str] = []
        for instrument in self.spec.bundle.instruments:
            display_tokens = self._token_set(str(instrument.display_name or ""))
            names = [(instrument.id, False), (instrument.display_name, False)]
            names.extend((alias, True) for alias in getattr(instrument, "aliases", []))
            if any(
                self._name_matches_text(str(name), lowered, text_tokens)
                for name, is_alias in names
                if name
                and not (
                    is_alias
                    and str(name).strip().lower() in self._ambiguous_aliases
                    and not self._token_set(str(name)).issubset(display_tokens)
                )
            ):
                matches.append(instrument.id)
        return matches

    def _token_set(self, text: str) -> set[str]:
        normalized = "".join(char.lower() if char.isalnum() else " " for char in text)
        return {token for token in normalized.split() if len(token) >= 2}

    def _name_matches_text(self, name: str, lowered_text: str, text_tokens: set[str]) -> bool:
        lowered_name = name.strip().lower()
        if not lowered_name:
            return False
        name_tokens = self._token_set(lowered_name)
        if not name_tokens:
            return False
        if len(name_tokens) == 1 and next(iter(name_tokens)) in _GENERIC_TOOL_TOKENS:
            return False
        if lowered_name in lowered_text:
            return True
        distinctive = name_tokens.difference(_GENERIC_TOOL_TOKENS)
        if distinctive and distinctive.issubset(text_tokens):
            return True
        if len(name_tokens) >= 2 and len(name_tokens.intersection(text_tokens)) >= min(2, len(name_tokens)):
            return True
        return False

    def _field_event_text(self, evidence: dict[str, Any]) -> str:
        field_event = evidence.get("field_event", [])
        if isinstance(field_event, str):
            return field_event
        if isinstance(field_event, list):
            return " ".join(str(item) for item in field_event if str(item).strip())
        return ""

    def _phase_mentions_from_text(self, text: str) -> list[str]:
        lowered = text.lower()
        text_tokens = self._token_set(text)
        mentions: list[str] = []
        for phase_id, labels in self._phase_aliases_by_id.items():
            for label in labels:
                label_tokens = self._token_set(label)
                if not label_tokens:
                    continue
                if str(label).strip().lower() in lowered:
                    mentions.append(phase_id)
                    break
                distinctive = {token for token in label_tokens if not token.startswith("p")}
                if distinctive and distinctive.issubset(text_tokens):
                    mentions.append(phase_id)
                    break
        return mentions

    def _boost_next_tools_after(
        self,
        tool_scores: dict[str, float],
        anchor_tool: str,
        *,
        current_phase: str,
        allowed_next: set[str],
        weight: float,
        first_unprogressed_tool: str = "",
        current_sequence_order: dict[str, int] | None = None,
    ) -> bool:
        boosted = False
        candidate_phases = {current_phase, *allowed_next}
        for phase_id, current_tool, next_tool in self._sequence_rows:
            if phase_id not in candidate_phases:
                continue
            if current_tool != anchor_tool or next_tool not in self._requestable:
                continue
            if (
                phase_id == current_phase
                and first_unprogressed_tool
                and current_sequence_order
                and current_tool in current_sequence_order
                and first_unprogressed_tool in current_sequence_order
                and current_sequence_order[current_tool] > current_sequence_order[first_unprogressed_tool]
            ):
                continue
            phase_weight = weight if phase_id == current_phase else weight * 0.22
            tool_scores[next_tool] += phase_weight
            boosted = True
        return boosted

    def _sequence_tools(self, phase_id: str) -> list[str]:
        sequence_tools = list(self._sequence_tools_by_phase.get(phase_id, []))
        if sequence_tools:
            return sequence_tools
        return [
            tool_id
            for tool_id in self._expected_by_phase.get(phase_id, [])
            if tool_id in self._requestable
        ]

    def _first_unprogressed_tool(self, sequence_tools: list[str], progressed_tools: set[str]) -> str:
        for tool_id in sequence_tools:
            if tool_id and tool_id in self._requestable and tool_id not in progressed_tools:
                return tool_id
        return ""

    def _next_primary_sequence_tool(
        self,
        phase_id: str,
        observed_tools: list[str],
    ) -> str:
        sequence = self._primary_sequence_by_phase.get(phase_id, [])
        if not sequence:
            return self._first_unprogressed_tool(
                self._sequence_tools(phase_id),
                set(observed_tools),
            )
        cursor = 0
        for observed_tool in observed_tools:
            if cursor >= len(sequence):
                break
            if observed_tool == sequence[cursor]:
                cursor += 1
                continue
            try:
                later_index = sequence.index(observed_tool, cursor + 1)
            except ValueError:
                continue
            # Public evidence may omit an unspoken handover. Follow a later
            # observed sequence item instead of freezing the prior forever.
            cursor = later_index + 1
        return sequence[cursor] if cursor < len(sequence) else ""

    def _normal_transition_signatures(
        self,
        current_phase: str,
        allowed_next: set[str],
    ) -> dict[str, set[str]]:
        current_tools = set(self._sequence_tools(current_phase))
        interrupt_tools = {
            tool_id
            for phase_id in self._interrupt_phase_ids
            for tool_id in self._sequence_tools(phase_id)
        }
        signatures: dict[str, set[str]] = {}
        for phase_id in allowed_next:
            if phase_id in self._interrupt_phase_ids:
                continue
            next_tools = set(self._sequence_tools(phase_id))
            signature = {
                tool_id
                for tool_id in next_tools.difference(current_tools, interrupt_tools)
                if tool_id in self._requestable
            }
            if signature:
                signatures[phase_id] = signature
        return signatures

    def _row_after_phase_entry(self, row: Any, phase_entered_sec: float) -> bool:
        if phase_entered_sec <= 0.0 or not isinstance(row, dict):
            return True
        raw_time = row.get("at", row.get("stamp_sec", row.get("time_sec", 0.0)))
        try:
            event_sec = float(raw_time)
        except (TypeError, ValueError):
            return True
        if event_sec <= 0.0:
            return True
        return event_sec + 0.25 >= phase_entered_sec

    def _evidence_tools(self, evidence: dict[str, Any]) -> tuple[list[str], list[str]]:
        try:
            phase_entered_sec = float(evidence.get("phase_entered_sec", 0.0) or 0.0)
        except (TypeError, ValueError):
            phase_entered_sec = 0.0
        speech_tools: list[str] = []
        for item in evidence.get("speech", []) or []:
            if not self._row_after_phase_entry(item, phase_entered_sec):
                continue
            text = str(item.get("text", "") if isinstance(item, dict) else item)
            speech_tools.extend(self._tools_from_text(text))
        recent_tools: list[str] = []
        for tool in evidence.get("recent_tools", []) or []:
            if not self._row_after_phase_entry(tool, phase_entered_sec):
                continue
            resolved = self._resolve_tool(tool.get("tool", "") if isinstance(tool, dict) else tool)
            if resolved:
                recent_tools.append(resolved)
        for signal in evidence.get("observed_signals", []) or []:
            if not isinstance(signal, dict):
                continue
            if not self._row_after_phase_entry(signal, phase_entered_sec):
                continue
            if str(signal.get("type", "")) not in {"request_tool", "voice_request", "continue_using"}:
                continue
            resolved = self._resolve_tool(signal.get("tool", ""))
            if resolved:
                recent_tools.append(resolved)
        handover_actions = {
            "direct_handover",
            "pick_up_and_handover",
            "put_down_and_handover",
            "handover",
        }
        for status in evidence.get("skill_status", []) or []:
            if not isinstance(status, dict):
                continue
            if not self._row_after_phase_entry(status, phase_entered_sec):
                continue
            action = str(status.get("action", "")).strip()
            if action not in handover_actions:
                continue
            resolved = self._resolve_tool(status.get("tool", ""))
            if resolved:
                recent_tools.append(resolved)
        if not evidence.get("recent_tools") and not evidence.get(
            "completed_handovers"
        ):
            progress_event_types = {
                "ToolHandoverCompleted",
                "ToolReceivedFromSurgeon",
            }
            for event in evidence.get("events", []) or []:
                if not isinstance(event, dict):
                    continue
                if not self._row_after_phase_entry(event, phase_entered_sec):
                    continue
                if str(event.get("t", "")) not in progress_event_types:
                    continue
                resolved = self._resolve_tool(event.get("tool", ""))
                if resolved:
                    recent_tools.append(resolved)
        return speech_tools, recent_tools

    def _history_tools(self, evidence: dict[str, Any], key: str) -> list[str]:
        tools: list[str] = []
        for row in evidence.get(key, []) or []:
            raw_tool = row.get("tool", "") if isinstance(row, dict) else row
            resolved = self._resolve_tool(raw_tool)
            if resolved:
                # Repeated requests are meaningful when the procedure owns
                # multiple instances of the same instrument.
                tools.append(resolved)
        return tools

    def _handover_forecast(
        self,
        histories: list[tuple[str, list[str], int]],
    ) -> dict[str, Any]:
        if not self._handover_paths:
            return {}

        non_empty = [row for row in histories if row[1]]
        if non_empty:
            history_source, history, _ = max(
                non_empty,
                # Validated requests are the earliest public progression fact.
                # Completion history is a fallback because transport delays
                # and duplicate lifecycle events can make it look longer.
                key=lambda row: (row[2], len(row[1])),
            )
        else:
            history_source, history = "entry", []

        candidates: list[dict[str, Any]] = []
        for source, path_index, path in self._handover_paths:
            source_bonus = 0.05 if source == "primary" else 0.0
            if not history:
                if source != "primary" or path_index != 0:
                    continue
                candidates.append(
                    {
                        "tool": path[0],
                        "confidence": 0.86,
                        "match_length": 0,
                        "path_start": 0,
                        "source": source,
                        "path_index": path_index,
                    }
                )
                continue

            max_match = min(len(history), len(path) - 1)
            for match_length in range(max_match, 0, -1):
                suffix = history[-match_length:]
                positions = [
                    start
                    for start in range(0, len(path) - match_length)
                    if path[start : start + match_length] == suffix
                ]
                if not positions:
                    continue
                expected_start = max(
                    0,
                    min(
                        len(path) - match_length - 1,
                        len(history) - match_length,
                    ),
                )
                path_start = min(
                    positions,
                    key=lambda start: (abs(start - expected_start), start),
                )
                confidence = min(
                    0.95,
                    0.76
                    + source_bonus
                    + min(0.14, match_length * 0.06)
                    + min(0.05, max(0, match_length - 3) * 0.015),
                )
                candidates.append(
                    {
                        "tool": path[path_start + match_length],
                        "confidence": round(confidence, 3),
                        "match_length": match_length,
                        "path_start": path_start,
                        "source": source,
                        "path_index": path_index,
                    }
                )
                break

        if not candidates:
            return {
                "tool": "",
                "confidence": 0.0,
                "history": history[-12:],
                "history_source": history_source,
                "match_length": 0,
                "source": "none",
                "candidates": [],
            }

        candidates.sort(
            key=lambda row: (
                float(row["confidence"]),
                int(row["match_length"]),
                row["source"] == "primary",
                -int(row["path_index"]),
            ),
            reverse=True,
        )
        selected = candidates[0]
        return {
            **selected,
            "history": history[-12:],
            "history_source": history_source,
            "candidates": candidates[:4],
        }

    def _sequence_alignment_score(
        self,
        observed_tools: list[str],
        phase_sequence: list[str],
    ) -> tuple[int, int]:
        """Return ordered matches and adjacent transitions for one phase.

        This is deliberately a small dynamic-programming prior, not a phase
        labeler. It lets a left-censored replay use public tool exchanges to
        compare every procedure phase before a temporal anchor exists.
        """
        if not observed_tools or not phase_sequence:
            return 0, 0
        rows = len(observed_tools) + 1
        cols = len(phase_sequence) + 1
        lcs = [[0] * cols for _ in range(rows)]
        for row in range(1, rows):
            for col in range(1, cols):
                if observed_tools[row - 1] == phase_sequence[col - 1]:
                    lcs[row][col] = lcs[row - 1][col - 1] + 1
                else:
                    lcs[row][col] = max(
                        lcs[row - 1][col],
                        lcs[row][col - 1],
                    )
        adjacent = 0
        phase_pairs = set(zip(phase_sequence, phase_sequence[1:]))
        for pair in zip(observed_tools, observed_tools[1:]):
            if pair in phase_pairs:
                adjacent += 1
        return lcs[-1][-1], adjacent

    def score_open_set(self, evidence: dict[str, Any]) -> dict[str, Any]:
        """Rank all normal phases from public evidence without a phase anchor."""
        speech_tools, recent_tools = self._evidence_tools(evidence)
        observed_tools = list(speech_tools[-8:] or recent_tools[-12:])
        if len(observed_tools) < 2:
            return {
                "phase": [],
                "tool": [],
                "evidence": {
                    "current_phase": "",
                    "allowed_next": [],
                    "phase_search_mode": "open_set",
                    "tool_sequence_open_set_anchor_allowed": (
                        self._tool_sequence_open_set_anchor_allowed
                    ),
                    "speech_tools": speech_tools[-4:],
                    "recent_tools": recent_tools[-8:],
                },
            }

        phase_scores: dict[str, float] = defaultdict(float)
        alignment_by_phase: dict[str, tuple[int, int]] = {}
        for phase_id in self.spec.normal_phase_ids:
            sequence = list(
                self._primary_sequence_by_phase.get(phase_id)
                or self._sequence_tools(phase_id)
            )
            matches, adjacent = self._sequence_alignment_score(
                observed_tools,
                sequence,
            )
            if matches <= 0:
                continue
            alignment_by_phase[phase_id] = (matches, adjacent)
            coverage = matches / max(1, len(sequence))
            precision = matches / max(1, min(len(observed_tools), len(sequence)))
            phase_scores[phase_id] += (
                0.03
                + coverage * 0.10
                + precision * 0.30
                + min(0.48, matches * 0.12)
                + min(0.24, adjacent * 0.10)
            )
            if observed_tools[-1] in sequence:
                phase_scores[phase_id] += 0.03

        ranked_phases = _rank(phase_scores, limit=4)
        tool_scores: dict[str, float] = defaultdict(float)
        for rank, row in enumerate(ranked_phases[:2]):
            phase_id = str(row[0])
            sequence = list(
                self._primary_sequence_by_phase.get(phase_id)
                or self._sequence_tools(phase_id)
            )
            next_tool = self._next_primary_sequence_tool(
                phase_id,
                observed_tools,
            )
            if next_tool:
                tool_scores[next_tool] += max(0.35, 1.0 - rank * 0.35)
            for tool_id in self._expected_by_phase.get(phase_id, []):
                if tool_id in self._requestable:
                    tool_scores[tool_id] += max(0.04, 0.12 - rank * 0.04)

        return {
            "phase": ranked_phases,
            "tool": _rank(tool_scores, limit=5),
            "evidence": {
                "current_phase": "",
                "allowed_next": [],
                "phase_search_mode": "open_set",
                "tool_sequence_open_set_anchor_allowed": (
                    self._tool_sequence_open_set_anchor_allowed
                ),
                "speech_tools": speech_tools[-4:],
                "recent_tools": recent_tools[-8:],
                "observed_tool_sequence": observed_tools,
                "sequence_alignment": {
                    phase_id: {
                        "matches": values[0],
                        "adjacent": values[1],
                    }
                    for phase_id, values in alignment_by_phase.items()
                },
            },
        }

    def score(self, evidence: dict[str, Any]) -> dict[str, Any]:
        current_phase = str(evidence.get("current_phase", "") or self.spec.default_phase_id)
        if current_phase not in self.spec.phase_ids:
            current_phase = self.spec.default_phase_id
        allowed_next = set(self.spec.get_allowed_next_phases(current_phase))
        field_event_text = self._field_event_text(evidence)
        interrupt_evidence_active = bool(field_event_text) or current_phase in self._interrupt_phase_ids
        regular_allowed_next = {
            phase_id
            for phase_id in allowed_next
            if interrupt_evidence_active or phase_id not in self._interrupt_phase_ids
        }
        phase_scores: dict[str, float] = defaultdict(float)
        tool_scores: dict[str, float] = defaultdict(float)
        phase_scores[current_phase] += 0.55
        for phase_id in regular_allowed_next:
            phase_scores[phase_id] += 0.14

        speech_tools, recent_tools = self._evidence_tools(evidence)
        request_tools = self._history_tools(evidence, "tool_requests")
        completed_handover_tools = self._history_tools(
            evidence,
            "completed_handovers",
        )
        path_forecast = self._handover_forecast(
            [
                ("validated_requests", request_tools, 4),
                ("public_speech", speech_tools, 3),
                ("completed_handovers", completed_handover_tools, 2),
                ("recent_tools", recent_tools, 1),
            ]
        )
        try:
            phase_entered_sec = float(evidence.get("phase_entered_sec", 0.0) or 0.0)
        except (TypeError, ValueError):
            phase_entered_sec = 0.0
        phase_mentions: list[str] = []
        for item in evidence.get("speech", []) or []:
            text = str(item.get("text", "") if isinstance(item, dict) else item)
            phase_mentions.extend(self._phase_mentions_from_text(text))
        advance_transition_phases: set[str] = set()
        for item in evidence.get("observed_signals", []) or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("type", "")) != "advance_phase_cue":
                continue
            text = str(item.get("speech", "") or "")
            mentions = [
                phase_id
                for phase_id in self._phase_mentions_from_text(text)
                if phase_id != current_phase and phase_id not in self._interrupt_phase_ids
            ]
            if mentions:
                advance_transition_phases.update(mentions)
                phase_mentions.extend(mentions)
                continue
            try:
                cue_sec = float(item.get("at", 0.0) or 0.0)
            except (TypeError, ValueError):
                cue_sec = 0.0
            if phase_entered_sec > 0.0 and cue_sec > 0.0 and cue_sec <= phase_entered_sec + 0.5:
                continue
            next_normal_phase = self.spec.get_next_normal_phase(current_phase)
            if next_normal_phase and next_normal_phase in allowed_next:
                advance_transition_phases.add(next_normal_phase)
        mentioned_transition_phases: set[str] = set()
        for index, phase_id in enumerate(reversed(phase_mentions[-3:])):
            if phase_id != current_phase and phase_id not in regular_allowed_next:
                continue
            if phase_id in self._interrupt_phase_ids and not interrupt_evidence_active:
                continue
            phase_scores[phase_id] += max(0.2, 1.25 - index * 0.25)
            if phase_id != current_phase:
                mentioned_transition_phases.add(phase_id)
        if mentioned_transition_phases:
            phase_scores[current_phase] *= 0.72
        for phase_id in advance_transition_phases:
            if phase_id not in self.spec.phase_ids:
                continue
            phase_scores[phase_id] += 2.2
            if phase_id != current_phase:
                mentioned_transition_phases.add(phase_id)
        if advance_transition_phases.difference({current_phase}):
            phase_scores[current_phase] *= 0.25

        transition_evidence = speech_tools[-4:] if speech_tools else recent_tools[-6:]
        signature_transition_hits: dict[str, int] = {}
        for phase_id, signature in self._normal_transition_signatures(
            current_phase,
            regular_allowed_next,
        ).items():
            hits = [tool_id for tool_id in transition_evidence if tool_id in signature]
            if not hits:
                continue
            hit_count = len(hits)
            signature_transition_hits[phase_id] = hit_count
            phase_scores[phase_id] += 0.95 + min(0.70, (hit_count - 1) * 0.35)
            phase_scores[current_phase] *= 0.55 if hit_count == 1 else 0.30
            mentioned_transition_phases.add(phase_id)

        mayo_tools = {self._resolve_tool(tool) for tool in evidence.get("mayo_tools", []) or []}
        hand_tools = {self._resolve_tool(tool) for tool in evidence.get("hand_tools", []) or []}
        mayo_tools.discard("")
        hand_tools.discard("")
        current_sequence = self._sequence_tools(current_phase)
        current_sequence_set = set(current_sequence)
        current_expected_set = set(self._expected_by_phase.get(current_phase, []))
        current_progressed_tools = set(recent_tools[-6:]) | set(speech_tools[-3:]) | mayo_tools | hand_tools
        ordered_progress_tools = (
            list(speech_tools[-8:])
            if speech_tools
            else list(recent_tools[-12:])
        )
        if not ordered_progress_tools:
            ordered_progress_tools = sorted(mayo_tools | hand_tools)
        current_sequence_tool = self._next_primary_sequence_tool(
            current_phase,
            ordered_progress_tools,
        )
        current_sequence_order = {tool_id: index for index, tool_id in enumerate(current_sequence)}

        def phase_tool_weight(phase_id: str, tool_id: str, base_weight: float) -> float:
            if phase_id == current_phase:
                return base_weight
            if field_event_text:
                if phase_id in self._interrupt_phase_ids or phase_id in mentioned_transition_phases:
                    return base_weight
                return base_weight * 0.15
            if phase_id in mentioned_transition_phases:
                return base_weight
            if tool_id in current_sequence_set or tool_id in current_expected_set:
                return base_weight * 0.18
            return base_weight * 0.42

        if not field_event_text and not mentioned_transition_phases:
            public_current_tool_count = len(
                {
                    tool_id
                    for tool_id in [*recent_tools[-4:], *speech_tools[-2:]]
                    if tool_id in current_sequence_set or tool_id in current_expected_set
                }
            )
            phase_scores[current_phase] += 0.18 + min(0.18, public_current_tool_count * 0.06)

        active_event_sequence_tools: set[str] = set()
        active_event_next_tools: set[str] = set()
        if field_event_text:
            phase_scores[current_phase] += 0.22
            event_tokens = self._token_set(field_event_text)
            event_tools = set(self._tools_from_text(field_event_text))
            for phase_id in self._interrupt_phase_ids:
                phase_tokens = self._token_set(self._phase_display_by_id.get(phase_id, phase_id))
                expected_tools = set(self._expected_by_phase.get(phase_id, []))
                overlap = len(event_tokens.intersection(phase_tokens))
                tool_overlap = len(event_tools.intersection(expected_tools))
                relevant_event = (
                    phase_id in allowed_next
                    or current_phase in self._interrupt_phase_ids
                    or overlap > 0
                    or tool_overlap > 0
                )
                if phase_id in allowed_next or current_phase in self._interrupt_phase_ids:
                    phase_scores[phase_id] += 0.72
                if overlap:
                    phase_scores[phase_id] += min(0.28, overlap * 0.08)
                if tool_overlap:
                    phase_scores[phase_id] += min(0.24, tool_overlap * 0.08)
                if relevant_event:
                    sequence_tools = self._sequence_tools(phase_id)
                    active_event_sequence_tools.update(sequence_tools)
                    progressed_tools = (
                        set(recent_tools[-8:])
                        | set(speech_tools[-4:])
                        | mayo_tools
                        | hand_tools
                    )
                    next_event_tool = self._first_unprogressed_tool(sequence_tools, progressed_tools)
                    if next_event_tool:
                        active_event_next_tools.add(next_event_tool)
                    for index, tool_id in enumerate(sequence_tools[:4]):
                        event_weight = max(0.18, 0.84 - index * 0.18)
                        if tool_id == next_event_tool:
                            event_weight += 2.0
                        elif tool_id in progressed_tools:
                            event_weight *= 0.16
                        else:
                            event_weight *= 0.36
                        tool_scores[tool_id] += event_weight
                    if next_event_tool:
                        tool_scores[next_event_tool] += 1.4

            # Public field events are interrupt overlays, not normal procedure
            # advances. Hold the current normal phase unless speech explicitly
            # names a transition target.
            phase_scores[current_phase] += 0.65
            for phase_id in list(phase_scores):
                if phase_id == current_phase or phase_id in self._interrupt_phase_ids:
                    continue
                if phase_id in mentioned_transition_phases:
                    continue
                phase_scores[phase_id] *= 0.55

        if not field_event_text and path_forecast.get("tool"):
            # This is a preparation prior only. The reducer exposes it as a
            # hypothesis; inventory, ownership and handover authorization stay
            # in the Behavior Tree.
            tool_scores[str(path_forecast["tool"])] += (
                2.4 * float(path_forecast.get("confidence", 0.0))
            )

        for index, tool_id in enumerate(reversed(recent_tools[-6:])):
            weight = max(0.08, 0.28 - index * 0.035)
            for phase_id in self._phase_by_tool.get(tool_id, set()):
                if phase_id in self._interrupt_phase_ids and not interrupt_evidence_active:
                    continue
                if phase_id != current_phase and phase_id not in regular_allowed_next:
                    continue
                phase_scores[phase_id] += phase_tool_weight(phase_id, tool_id, weight)
            next_tool_weight = max(0.18, 0.56 - index * 0.05)
            if speech_tools:
                next_tool_weight *= 0.32
            self._boost_next_tools_after(
                tool_scores,
                tool_id,
                current_phase=current_phase,
                allowed_next=regular_allowed_next,
                weight=next_tool_weight,
                first_unprogressed_tool=current_sequence_tool,
                current_sequence_order=current_sequence_order,
            )
        for index, tool_id in enumerate(reversed(speech_tools[-4:])):
            speech_weight = max(0.08, 0.88 - index * 0.50)
            for phase_id in self._phase_by_tool.get(tool_id, set()):
                if phase_id in self._interrupt_phase_ids and not interrupt_evidence_active:
                    continue
                if phase_id != current_phase and phase_id not in regular_allowed_next:
                    continue
                phase_scores[phase_id] += phase_tool_weight(phase_id, tool_id, max(0.06, 0.42 - index * 0.18))
            # Spoken/requested tools describe the current handover intent. The
            # public `tool` output should predict the next likely request, so use
            # the procedure sequence after the spoken tool instead of echoing it.
            self._boost_next_tools_after(
                tool_scores,
                tool_id,
                current_phase=current_phase,
                allowed_next=regular_allowed_next,
                weight=speech_weight,
                first_unprogressed_tool=current_sequence_tool,
                current_sequence_order=current_sequence_order,
            )

        active_tool = speech_tools[-1] if speech_tools else next((tool for tool in reversed(recent_tools) if tool), "")
        for phase_id, current_tool, next_tool in self._sequence_rows:
            if phase_id in self._interrupt_phase_ids and not interrupt_evidence_active:
                continue
            if phase_id != current_phase and phase_id not in regular_allowed_next:
                continue
            if (
                phase_id == current_phase
                and current_sequence_tool
                and current_tool in current_sequence_order
                and current_sequence_tool in current_sequence_order
                and current_sequence_order[current_tool] > current_sequence_order[current_sequence_tool]
            ):
                continue
            if current_tool and active_tool == current_tool:
                phase_scores[phase_id] += (
                    0.36 if phase_id == current_phase else phase_tool_weight(phase_id, active_tool, 0.12)
                )
                if next_tool in self._requestable:
                    tool_scores[next_tool] += 0.58 if phase_id == current_phase else 0.16
            if next_tool and active_tool == next_tool:
                phase_scores[phase_id] += (
                    0.22 if phase_id == current_phase else phase_tool_weight(phase_id, active_tool, 0.08)
                )

        if not speech_tools and not recent_tools:
            for phase_id, current_tool, next_tool in self._sequence_rows:
                if phase_id != current_phase:
                    continue
                initial_tool = current_tool if current_tool in self._requestable else next_tool
                if initial_tool in self._requestable:
                    tool_scores[initial_tool] += 0.62
                    break

        if current_sequence_tool and not field_event_text:
            tool_scores[current_sequence_tool] += 1.35

        for tool_id in self._expected_by_phase.get(current_phase, []):
            if tool_id in self._requestable:
                tool_scores[tool_id] += 0.16
        for phase_id in regular_allowed_next:
            for tool_id in self._expected_by_phase.get(phase_id, []):
                if tool_id in self._requestable:
                    tool_scores[tool_id] += 0.09

        if field_event_text and active_event_sequence_tools:
            progressed_event_tools = (set(recent_tools[-8:]) | set(speech_tools[-4:]) | mayo_tools | hand_tools).intersection(
                active_event_sequence_tools
            )
            for tool_id in list(tool_scores):
                if tool_id not in active_event_sequence_tools:
                    tool_scores[tool_id] *= 0.18
                elif tool_id in progressed_event_tools:
                    tool_scores[tool_id] *= 0.25
                elif active_event_next_tools and tool_id not in active_event_next_tools:
                    tool_scores[tool_id] *= 0.28

        for tool_id in set(speech_tools[-3:]):
            if tool_id in tool_scores and tool_id != current_sequence_tool:
                tool_scores[tool_id] *= 0.34

        for tool_id in mayo_tools.union(hand_tools):
            if tool_id in tool_scores and tool_id != current_sequence_tool:
                tool_scores[tool_id] *= 0.45

        if current_sequence_tool and not field_event_text:
            current_expected = set(self._expected_by_phase.get(current_phase, []))
            for tool_id in list(tool_scores):
                if tool_id == current_sequence_tool:
                    continue
                if tool_id in current_progressed_tools:
                    tool_scores[tool_id] *= 0.5
                elif tool_id in current_sequence_set or tool_id in current_expected:
                    tool_scores[tool_id] *= 0.72
                else:
                    tool_scores[tool_id] *= 0.45

        if signature_transition_hits and not field_event_text:
            strongest_phase, strongest_count = max(
                signature_transition_hits.items(),
                key=lambda item: (item[1], phase_scores[item[0]]),
            )
            phase_scores[current_phase] *= (
                0.45 if strongest_count == 1 else 0.25
            )
            phase_scores[strongest_phase] += (
                0.45 + min(0.35, (strongest_count - 1) * 0.18)
            )

        if (
            not self._tool_only_detailed_phase_transition_allowed
            and not advance_transition_phases
        ):
            # Voice, handover and detector identities remain useful next-tool
            # context, but cannot by themselves establish a new visual
            # functional state. Keep the known current detailed phase ahead
            # of every normal alternative; the VLM's current surgical-field
            # image can still outrank this prior downstream.
            current_score = max(phase_scores[current_phase], 0.01)
            for phase_id in list(phase_scores):
                if (
                    phase_id == current_phase
                    or phase_id in self._interrupt_phase_ids
                ):
                    continue
                phase_scores[phase_id] = min(
                    phase_scores[phase_id],
                    current_score * 0.92,
                )

        rankable_phase_scores = {
            phase_id: score
            for phase_id, score in phase_scores.items()
            if phase_id not in self._interrupt_phase_ids
        }

        return {
            "phase": _rank(rankable_phase_scores or phase_scores, limit=4),
            "tool": _rank(tool_scores, limit=5),
            "evidence": {
                "current_phase": current_phase,
                "interrupt_event_active": bool(field_event_text),
                "speech_tools": speech_tools[-4:],
                "recent_tools": recent_tools[-8:],
                "tool_requests": request_tools[-12:],
                "completed_handovers": completed_handover_tools[-12:],
                "procedure_path_forecast": path_forecast,
                "allowed_next": sorted(regular_allowed_next),
                "tool_only_detailed_phase_transition_allowed": (
                    self._tool_only_detailed_phase_transition_allowed
                ),
            },
        }
