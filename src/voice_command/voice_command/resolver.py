"""Grounded natural-language candidate generation for final STT text.

This is intentionally not a general chatbot.  It accepts natural Korean
wording around reviewed concepts (for example ``보비 내놔 빨리`` and ``자 이제
교시를 시작해보자``), but produces only a small, typed proposal set.  A model
selector can select from that set; it cannot add unspoken slots or create a
new action.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
import re
from typing import Mapping, Sequence

from procedure_spec import normalize_voice_alias

from .contracts import (
    DISPOSITION_PROPOSE,
    INTENT_RETRACTOR_COMMAND,
    INTENT_TOOL_HANDOVER,
    VoiceIntentProposal,
)
from .selector import CandidateSelection, CandidateSelector, DeterministicCandidateSelector


_GENERIC_TOOL_TERMS = ("도구", "기구", "툴", "tool", "instrument")
_HANDOVER_CUES = (
    "주세요",
    "주십시오",
    "줘요",
    "줘",
    "내놔요",
    "내놔",
    "건네주세요",
    "건네줘",
    "건네",
    "전달해주세요",
    "전달해줘",
    "전달해",
    "전달",
    "가져와주세요",
    "가져와줘",
    "가져와",
    "빨리",
    "서둘러",
    "give me",
    "hand me",
    "pass me",
    "please",
    "quickly",
    "urgent",
)
_URGENCY_CUES = ("빨리", "quickly", "urgent", "서둘러")
_SELECTOR_ONLY_TOOL_CUES = ("부탁",)
_NON_HANDOVER_ACTION_CUES = (
    "보여줘",
    "찾아줘",
    "확인",
    "사용",
    "사용중",
    "쓰고",
    "쓰지",
    "정리",
    "치워",
    "제거",
    "회수",
    "닦",
    "세척",
    "소독",
    "버려",
    "반납",
    "종료",
    "끝",
    "완료",
    "finished",
    "finish",
    "done",
)
_NEGATION_CUES = (
    "하지마",
    "하지말",
    "말자",
    "말고",
    "않",
    "아니",
    "못",
    "금지",
    "do not",
    "dont",
    "don't",
    " not ",
)
_QUESTION_CUES = (
    "할까",
    "할까요",
    "인가요",
    "인가",
    "나요",
    "겠습니까",
    "can you",
    "would you",
    "do we",
)
_DIRECT_TEACH_TERMS = (
    "직접 교시",
    "직접교시",
    "교시",
    "direct teach",
    "direct teaching",
)
# These are observed STT hypotheses.  They are *candidate-local repairs*, not
# global transcript rewrites, and always retain a confirmation requirement.
_DIRECT_TEACH_REPAIR_TERMS = ("직접 교실", "직접교실", "교시시")
_DIRECT_TEACH_START_CUES = ("시작", "개시", "start", "begin", "activate")
_DIRECT_TEACH_FINISH_CUES = ("종료", "끝", "완료", "마쳐", "마치", "stop", "finish", "end")


def normalize_text(value: object) -> str:
    """Normalize matching text without mutating the raw STT transcript.

    The active-catalog producer uses the same normalizer in
    :func:`procedure_spec.normalize_voice_alias`; keeping this as a thin alias
    prevents a transcript/catalog normalization drift.
    """

    return normalize_voice_alias(value)


def parse_tool_aliases_json(
    value: object,
    *,
    allow_empty: bool = False,
) -> dict[str, tuple[str, ...]]:
    """Validate a parameterized ``{tool_id: [aliases...]}`` catalog.

    The catalog is the resolver's only source of tool IDs.  Invalid input is
    rejected at startup instead of silently producing a partially grounded
    mapping.
    """

    parsed = json.loads(str(value))
    if not isinstance(parsed, dict):
        raise ValueError("tool_aliases_json must be an object")
    if not parsed and not allow_empty:
        raise ValueError("tool_aliases_json must be a non-empty object")
    result: dict[str, tuple[str, ...]] = {}
    claimed_aliases: dict[str, str] = {}
    for raw_tool_id, raw_aliases in parsed.items():
        tool_id = str(raw_tool_id).strip()
        if not tool_id:
            raise ValueError("tool alias catalog contains an empty tool_id")
        if not isinstance(raw_aliases, list) or not raw_aliases:
            raise ValueError(f"tool alias catalog for {tool_id!r} must be a non-empty list")
        aliases = tuple(normalize_text(alias) for alias in raw_aliases)
        if any(not alias for alias in aliases):
            raise ValueError(f"tool alias catalog for {tool_id!r} contains an empty alias")
        for alias in aliases:
            owner = claimed_aliases.setdefault(alias, tool_id)
            if owner != tool_id:
                raise ValueError(
                    f"tool alias {alias!r} is assigned to both {owner!r} and {tool_id!r}"
                )
        result[tool_id] = tuple(dict.fromkeys(aliases))
    return result


@dataclass(frozen=True)
class _Candidate:
    candidate_id: str
    intent: str
    tool_id: str = ""
    retractor_command: str = ""
    provenance: str = "deterministic_alias"
    requires_confirmation: bool = False
    selector_required: bool = False
    urgency: str = ""
    reason: str = ""
    evidence_spans: tuple[str, ...] = ()

    def selector_payload(self) -> dict[str, object]:
        """Only the fields the selector may choose between."""

        return {
            "candidate_id": self.candidate_id,
            "intent": self.intent,
            "tool_id": self.tool_id,
            "retractor_command": self.retractor_command,
            "evidence_spans": list(self.evidence_spans),
        }

    def to_proposal(
        self,
        *,
        raw_text: str,
        normalized_text: str,
        selector_provenance: str,
    ) -> VoiceIntentProposal:
        return VoiceIntentProposal(
            raw_text=raw_text,
            normalized_text=normalized_text,
            intent=self.intent,
            tool_id=self.tool_id,
            retractor_command=self.retractor_command,
            urgency=self.urgency,
            provenance=f"{self.provenance}|{selector_provenance}",
            requires_confirmation=self.requires_confirmation,
            disposition=DISPOSITION_PROPOSE,
            reason=self.reason,
            evidence_spans=self.evidence_spans,
        )


class VoiceIntentResolver:
    """Resolve a final transcript into a grounded proposal or non-action."""

    def __init__(
        self,
        *,
        tool_aliases: Mapping[str, Sequence[str]] | None = None,
        procedure_id: str = "",
        catalog_id: str = "",
        selector: CandidateSelector | None = None,
        allow_selector_natural_variants: bool = False,
    ) -> None:
        source = tool_aliases if tool_aliases is not None else {}
        self._tool_aliases = self._validate_aliases(source)
        self._procedure_id = str(procedure_id).strip()
        self._catalog_id = str(catalog_id).strip()
        self._tool_catalog_bound = bool(
            self._procedure_id and self._catalog_id and self._tool_aliases
        )
        self._selector = selector or DeterministicCandidateSelector()
        self._allow_selector_natural_variants = bool(
            allow_selector_natural_variants
        )

    @staticmethod
    def _validate_aliases(
        source: Mapping[str, Sequence[str]],
    ) -> dict[str, tuple[str, ...]]:
        return parse_tool_aliases_json(
            json.dumps(source, ensure_ascii=False),
            allow_empty=True,
        )

    def resolve(self, raw_text: object) -> VoiceIntentProposal:
        """Resolve while attaching the active procedure/catalog binding."""

        proposal = self._resolve_unbound(raw_text)
        return replace(
            proposal,
            procedure_id=self._procedure_id,
            catalog_id=self._catalog_id,
        )

    def _resolve_unbound(self, raw_text: object) -> VoiceIntentProposal:
        raw = str(raw_text or "")
        normalized = normalize_text(raw)
        if not normalized:
            return VoiceIntentProposal.no_command(raw, normalized, reason="empty_transcript")
        if not self._procedure_id:
            return VoiceIntentProposal.no_command(
                raw,
                normalized,
                reason="procedure_binding_unavailable",
            )

        compact = normalized.replace(" ", "")
        tool_matches = (
            self._find_tool_matches(normalized)
            if self._tool_catalog_bound
            else {}
        )
        direct_terms = _matched_direct_teach_terms(normalized)
        repair_terms = _matched_direct_teach_repair_terms(normalized)
        has_generic_tool = bool(_matched_terms(normalized, compact, _GENERIC_TOOL_TERMS))
        handover_cues = _matched_terms(normalized, compact, _HANDOVER_CUES)
        has_direct_start = bool(_matched_terms(normalized, compact, _DIRECT_TEACH_START_CUES))
        has_direct_finish = bool(_matched_terms(normalized, compact, _DIRECT_TEACH_FINISH_CUES))
        command_context = bool(
            tool_matches
            or direct_terms
            or repair_terms
            or (has_generic_tool and handover_cues)
        )

        if command_context and _has_question(raw, normalized, compact):
            return VoiceIntentProposal.reject(
                raw,
                normalized,
                reason="question_not_executable",
                evidence_spans=("question",),
            )
        if command_context and _has_negation(normalized, compact):
            return VoiceIntentProposal.reject(
                raw,
                normalized,
                reason="negated_command",
                evidence_spans=("negation",),
            )
        if tool_matches:
            disallowed_actions = _matched_terms(
                normalized,
                compact,
                _NON_HANDOVER_ACTION_CUES,
            )
            if disallowed_actions:
                return VoiceIntentProposal.reject(
                    raw,
                    normalized,
                    reason="non_handover_tool_action",
                    intent=INTENT_TOOL_HANDOVER,
                    evidence_spans=tuple(
                        _dedupe((*_tool_evidence(tool_matches), *disallowed_actions))
                    ),
                )

        candidates: list[_Candidate] = []
        if tool_matches and handover_cues:
            if len(tool_matches) > 1:
                return VoiceIntentProposal.reject(
                    raw,
                    normalized,
                    reason="multiple_tools_in_one_utterance",
                    intent=INTENT_TOOL_HANDOVER,
                    evidence_spans=tuple(_tool_evidence(tool_matches)),
                )
            tool_id, aliases = next(iter(tool_matches.items()))
            urgency = _matched_terms(normalized, compact, _URGENCY_CUES)
            candidates.append(
                _Candidate(
                    candidate_id=f"tool_handover:{tool_id}",
                    intent=INTENT_TOOL_HANDOVER,
                    tool_id=tool_id,
                    reason=(
                        "grounded_tool_handover_urgent_language"
                        if urgency
                        else "grounded_tool_handover"
                    ),
                    # Audit language only; it never represents robot speed,
                    # force, distance, or a bypass of downstream policy.
                    urgency="urgent" if urgency else "routine",
                    evidence_spans=tuple(_dedupe((*aliases, *handover_cues))),
                )
            )
        elif has_generic_tool and handover_cues:
            return VoiceIntentProposal.clarify_tool(
                raw,
                normalized,
                evidence_spans=tuple(_dedupe((*_matched_terms(normalized, compact, _GENERIC_TOOL_TERMS), *handover_cues))),
                reason=(
                    "tool_catalog_unavailable"
                    if not self._tool_catalog_bound
                    else "missing_tool_id"
                ),
            )
        elif (
            self._allow_selector_natural_variants
            and tool_matches
            and not handover_cues
        ):
            if len(tool_matches) > 1:
                return VoiceIntentProposal.reject(
                    raw,
                    normalized,
                    reason="multiple_tools_in_one_utterance",
                    intent=INTENT_TOOL_HANDOVER,
                    evidence_spans=tuple(_tool_evidence(tool_matches)),
                )
            tool_id, aliases = next(iter(tool_matches.items()))
            if _is_selector_only_tool_request(compact, aliases):
                candidates.append(
                    _Candidate(
                        candidate_id=f"tool_handover:{tool_id}",
                        intent=INTENT_TOOL_HANDOVER,
                        tool_id=tool_id,
                        provenance="vlm_anchored_natural_variant",
                        # There is not yet a confirmation/ack state machine;
                        # consumers must not turn this into auto-execution.
                        requires_confirmation=True,
                        selector_required=True,
                        reason="selector_anchored_tool_handover",
                        urgency="routine",
                        evidence_spans=tuple(_dedupe((*aliases, "부탁"))),
                    )
                )

        if (direct_terms or repair_terms) and has_direct_start and has_direct_finish:
            return VoiceIntentProposal.reject(
                raw,
                normalized,
                reason="conflicting_direct_teach_cues",
                intent=INTENT_RETRACTOR_COMMAND,
                evidence_spans=tuple(_dedupe((*direct_terms, *repair_terms))),
            )
        canonical_start_shape = _is_direct_teach_command_shape(
            compact,
            action="start",
            repair=False,
        )
        canonical_finish_shape = _is_direct_teach_command_shape(
            compact,
            action="finish",
            repair=False,
        )
        repair_start_shape = _is_direct_teach_command_shape(
            compact,
            action="start",
            repair=True,
        )
        repair_finish_shape = _is_direct_teach_command_shape(
            compact,
            action="finish",
            repair=True,
        )
        if (direct_terms or repair_terms) and (
            (has_direct_start and not (canonical_start_shape or repair_start_shape))
            or (has_direct_finish and not (canonical_finish_shape or repair_finish_shape))
        ):
            return VoiceIntentProposal.reject(
                raw,
                normalized,
                reason="direct_teach_not_a_standalone_command",
                intent=INTENT_RETRACTOR_COMMAND,
                evidence_spans=tuple(_dedupe((*direct_terms, *repair_terms))),
            )
        if direct_terms and canonical_start_shape:
            candidates.append(
                _Candidate(
                    candidate_id="retractor_command:start_direct_teach",
                    intent=INTENT_RETRACTOR_COMMAND,
                    retractor_command="start_direct_teach",
                    provenance="deterministic_direct_teach_alias",
                    reason="grounded_direct_teach_start",
                    evidence_spans=tuple(_dedupe((*direct_terms, "시작"))),
                )
            )
        elif direct_terms and canonical_finish_shape:
            candidates.append(
                _Candidate(
                    candidate_id="retractor_command:finish_direct_teach",
                    intent=INTENT_RETRACTOR_COMMAND,
                    retractor_command="finish_direct_teach",
                    provenance="deterministic_direct_teach_alias",
                    reason="grounded_direct_teach_finish",
                    evidence_spans=tuple(_dedupe((*direct_terms, "종료"))),
                )
            )
        elif repair_terms and repair_start_shape:
            candidates.append(
                _Candidate(
                    candidate_id="retractor_command:start_direct_teach",
                    intent=INTENT_RETRACTOR_COMMAND,
                    retractor_command="start_direct_teach",
                    provenance="observed_asr_repair",
                    requires_confirmation=True,
                    reason="observed_asr_repair_direct_teach_start",
                    evidence_spans=tuple(_dedupe((*repair_terms, "시작"))),
                )
            )
        elif repair_terms and repair_finish_shape:
            candidates.append(
                _Candidate(
                    candidate_id="retractor_command:finish_direct_teach",
                    intent=INTENT_RETRACTOR_COMMAND,
                    retractor_command="finish_direct_teach",
                    provenance="observed_asr_repair",
                    requires_confirmation=True,
                    reason="observed_asr_repair_direct_teach_finish",
                    evidence_spans=tuple(_dedupe((*repair_terms, "종료"))),
                )
            )
        elif (
            self._allow_selector_natural_variants
            and direct_terms
            and not has_direct_start
            and not has_direct_finish
            and _is_selector_only_direct_teach_request(compact)
        ):
            candidates.append(
                _Candidate(
                    candidate_id="retractor_command:start_direct_teach",
                    intent=INTENT_RETRACTOR_COMMAND,
                    retractor_command="start_direct_teach",
                    provenance="vlm_anchored_natural_variant",
                    # There is not yet a confirmation/ack state machine;
                    # consumers must not turn this into auto-execution.
                    requires_confirmation=True,
                    selector_required=True,
                    reason="selector_anchored_direct_teach_start",
                    evidence_spans=tuple(_dedupe((*direct_terms, "해보자"))),
                )
            )

        if len(candidates) > 1:
            return VoiceIntentProposal.reject(
                raw,
                normalized,
                reason="multiple_command_candidates",
                evidence_spans=tuple(
                    _dedupe(
                        span
                        for candidate in candidates
                        for span in candidate.evidence_spans
                    )
                ),
            )
        if not candidates:
            if tool_matches:
                return VoiceIntentProposal.no_command(
                    raw,
                    normalized,
                    reason="tool_named_without_handover_anchor",
                    evidence_spans=tuple(_tool_evidence(tool_matches)),
                )
            return VoiceIntentProposal.no_command(
                raw,
                normalized,
                reason="no_reviewed_command_candidate",
            )

        return self._select_candidate(raw, normalized, candidates)

    def _select_candidate(
        self,
        raw: str,
        normalized: str,
        candidates: Sequence[_Candidate],
    ) -> VoiceIntentProposal:
        # Keep short, fully-grounded commands on the low-latency local path.
        # A selector is reserved for candidates that explicitly require its
        # semantic judgment; otherwise an endpoint outage would add latency
        # without adding safety or information.
        if len(candidates) == 1 and not candidates[0].selector_required:
            return candidates[0].to_proposal(
                raw_text=raw,
                normalized_text=normalized,
                selector_provenance="deterministic_strong_anchor",
            )
        selection = self._selector.select(
            raw_text=raw,
            normalized_text=normalized,
            candidates=candidates,
        )
        selected = _candidate_by_id(candidates, selection.candidate_id)
        if selected is not None:
            return selected.to_proposal(
                raw_text=raw,
                normalized_text=normalized,
                selector_provenance=selection.provenance,
            )
        if (
            selection.unavailable
            and len(candidates) == 1
            and not candidates[0].selector_required
        ):
            return candidates[0].to_proposal(
                raw_text=raw,
                normalized_text=normalized,
                selector_provenance="deterministic_fallback_after_selector_unavailable",
            )
        return VoiceIntentProposal.reject(
            raw,
            normalized,
            reason=selection.reason or "candidate_selector_rejected",
            evidence_spans=tuple(
                _dedupe(span for candidate in candidates for span in candidate.evidence_spans)
            ),
        )

    def _find_tool_matches(self, normalized: str) -> dict[str, tuple[str, ...]]:
        matches: dict[str, tuple[str, ...]] = {}
        for tool_id, aliases in self._tool_aliases.items():
            found = tuple(
                alias
                for alias in aliases
                if _contains_tool_alias(normalized, alias)
            )
            if found:
                matches[tool_id] = found
        return matches


def _candidate_by_id(
    candidates: Sequence[_Candidate],
    candidate_id: str | None,
) -> _Candidate | None:
    if not candidate_id:
        return None
    return next(
        (candidate for candidate in candidates if candidate.candidate_id == candidate_id),
        None,
    )


def _matched_terms(text: str, compact: str, terms: Sequence[str]) -> tuple[str, ...]:
    return tuple(term for term in terms if _contains_term(text, compact, term))


def _contains_term(text: str, compact: str, term: str) -> bool:
    normalized_term = normalize_text(term)
    if not normalized_term:
        return False
    if " " in normalized_term:
        return normalized_term in text
    if normalized_term.isascii():
        return bool(re.search(rf"(?<![a-z0-9]){re.escape(normalized_term)}(?![a-z0-9])", text))
    return normalized_term in compact


def _contains_tool_alias(text: str, alias: str) -> bool:
    """Match a known alias plus Korean particles, not arbitrary substrings."""

    start = 0
    while True:
        index = text.find(alias, start)
        if index < 0:
            return False
        end = index + len(alias)
        before = text[index - 1] if index else ""
        after = text[end:]
        left_boundary = not before or not (before.isalnum() or "가" <= before <= "힣")
        right_boundary = (
            not after
            or after[0].isspace()
            or not (after[0].isalnum() or "가" <= after[0] <= "힣")
            or after.startswith(("은", "는", "이", "가", "을", "를", "의", "도", "만", "와", "과", "랑", "로", "에게", "한테"))
        )
        if left_boundary and right_boundary:
            return True
        start = end


def _matched_direct_teach_terms(text: str) -> tuple[str, ...]:
    """Match ``교시`` as a spoken unit, not the ASR repair ``교시시``."""

    compact = text.replace(" ", "")
    matches: list[str] = []
    for term in _DIRECT_TEACH_TERMS:
        normalized_term = normalize_text(term)
        if normalized_term.isascii() or " " in normalized_term:
            if _contains_term(text, compact, normalized_term):
                matches.append(term)
        elif _contains_tool_alias(text, normalized_term):
            matches.append(term)
    return tuple(matches)


def _matched_direct_teach_repair_terms(text: str) -> tuple[str, ...]:
    compact = text.replace(" ", "")
    return tuple(
        term
        for term in _DIRECT_TEACH_REPAIR_TERMS
        if normalize_text(term).replace(" ", "") in compact
    )


def _is_direct_teach_command_shape(
    compact: str,
    *,
    action: str,
    repair: bool,
) -> bool:
    """Allow short natural command grammar while rejecting long background.

    A final-STT segment can contain an ordinary conversation ending in command
    words.  The resolver intentionally accepts only filler + teaching-domain +
    one lifecycle action here.  It still admits natural forms such as ``자
    이제 교시를 시작해보자`` without making the tail of a long discussion an
    executable proposal.
    """

    prefixes = r"(?:(?:자|이제|그럼|그러면|좀|한번|한번만|우리|바로|지금))*"
    particles = r"(?:를|은|는|이|가|도|만|좀)?"
    interstitial = r"(?:(?:바로|지금|좀|한번|한번만))*"
    domain = (
        r"(?:직접교실|교시시)"
        if repair
        else r"(?:직접교시|교시|directteach(?:ing)?)"
    )
    action_forms = {
        "start": r"(?:시작(?:해(?:보자|요|줘|주세요)?|하(?:자|죠|겠습니다|자고)?|합니다|할게(?:요)?)?|개시(?:해(?:보자|요)?|하(?:자|겠습니다)?|합니다)?|start|begin|activate)",
        "finish": r"(?:종료(?:해(?:보자|요|줘|주세요)?|하(?:자|죠|겠습니다)?|합니다)?|끝(?:내(?:자|요)?|내)?|완료(?:해(?:요)?|하(?:자|겠습니다)?|합니다)?|마치(?:자|겠습니다|어요)?|stop|finish|end)",
    }
    return bool(
        re.fullmatch(
            rf"{prefixes}{domain}{particles}{interstitial}{action_forms[action]}",
            compact,
        )
    )


def _is_selector_only_tool_request(
    compact: str,
    aliases: Sequence[str],
) -> bool:
    """Recognize a narrow, isolated natural request for the model selector.

    This intentionally does not accept arbitrary surrounding conversation.  A
    selector-only candidate must still contain a resolved active-catalog alias
    and a request cue such as ``부탁해``.  It is confirmation-required and
    does not fall back to deterministic proposal creation if model selection
    is unavailable.
    """

    prefix = r"(?:(?:자|이제|좀|제발|하나|한번|한번만|바로|지금))*"
    particle = r"(?:을|를|은|는|이|가|도|만)?"
    filler = r"(?:(?:좀|하나|한번|한번만|제발|바로|지금))*"
    request = r"(?:부탁(?:해|합니다|드려요|드릴게요)?)"
    for alias in aliases:
        compact_alias = normalize_text(alias).replace(" ", "")
        if compact_alias and re.fullmatch(
            rf"{prefix}{re.escape(compact_alias)}{particle}{filler}{request}",
            compact,
        ):
            return True
    return False


def _is_selector_only_direct_teach_request(compact: str) -> bool:
    """Recognize ``교시를 해보자`` only as a selector-required candidate."""

    prefix = r"(?:(?:자|이제|그럼|그러면|좀|한번|한번만|우리|바로|지금))*"
    domain = r"(?:직접교시|교시|directteach(?:ing)?)"
    particle = r"(?:를|은|는|이|가|도|만|좀)?"
    filler = r"(?:(?:바로|지금|좀|한번|한번만))*"
    return bool(
        re.fullmatch(
            rf"{prefix}{domain}{particle}{filler}(?:해보자|해볼래|try)",
            compact,
        )
    )


def _has_question(raw: str, text: str, compact: str) -> bool:
    if "?" in raw or "？" in raw:
        return True
    return bool(_matched_terms(text, compact, _QUESTION_CUES))


def _has_negation(text: str, compact: str) -> bool:
    if any(
        term in compact
        for term in ("하지마", "하지말", "주지마", "주지말", "말자", "말고", "않", "아니", "못", "금지")
    ):
        return True
    # A bare "안" is common in unrelated words (for example 안전), so match
    # it only when it modifies an actionable Korean verb.
    if re.search(
        r"(?:^|[가-힣\s])안\s*(?:줘|주(?:지|세요|십시오)?|내놔|건네|전달|가져와|시작|해)",
        text,
    ):
        return True
    return any(term in text for term in _NEGATION_CUES if term.isascii() or " " in term)


def _tool_evidence(matches: Mapping[str, Sequence[str]]) -> tuple[str, ...]:
    return tuple(alias for aliases in matches.values() for alias in aliases)


def _dedupe(values: Sequence[str] | object) -> tuple[str, ...]:
    # ``values`` is often a generator assembled from candidate evidence.
    return tuple(dict.fromkeys(str(value) for value in values if str(value)))
