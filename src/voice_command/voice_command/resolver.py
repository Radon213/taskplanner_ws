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

from procedure_spec import (
    NormalizedRetractionCommand,
    RetractionCommand,
    RetractionState,
    RetractionTargetSide,
    normalize_retractor_adjustment_parameters,
    normalize_retractor_command,
    normalize_voice_alias,
)
from .contracts import (
    DISPOSITION_NO_COMMAND,
    DISPOSITION_PROPOSE,
    INTENT_PROCEDURE_START,
    INTENT_PROCEDURE_STOP,
    INTENT_RETRACTOR_COMMAND,
    INTENT_TOOL_HANDOVER,
    INTENT_TOOL_RETRIEVE,
    VoiceIntentProposal,
)
from .command_catalog import (
    bounded_command_suffixes,
    bounded_final_command_clause,
    command_context_is_blocked,
    command_is_reported_or_quoted,
    explicit_command_clauses,
    semantic_command_categories,
)
from .selector import CandidateSelection, CandidateSelector, DeterministicCandidateSelector


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
    "왜",
    "할까",
    "할까요",
    "인가요",
    "인가",
    "나요",
    "겠습니까",
    "can you",
    "would you",
    "do we",
    "도 돼",
)
_DIRECT_TEACH_TERMS = (
    "직접 교시",
    "직접교시",
    "교시",
    "direct teach",
    "direct teaching",
    "teaching",
)
# These are observed STT hypotheses.  They are *candidate-local repairs*, not
# global transcript rewrites, and always retain a confirmation requirement.
_DIRECT_TEACH_REPAIR_TERMS = ("직접 교실", "직접교실", "교시시")
_DIRECT_TEACH_START_CUES = ("시작", "개시", "start", "begin", "activate")
_DIRECT_TEACH_FINISH_CUES = ("종료", "끝", "완료", "마쳐", "마치", "stop", "finish", "end")

# This is a small reviewed vocabulary, not a procedure-name classifier.  The
# resolver only admits a lifecycle phrase when its spoken name matches the
# currently bound procedure.  It deliberately never turns a bare "start" or
# "stop" into a runtime command.
_PROCEDURE_SPOKEN_ALIASES = {
    "thyroidectomy": ("갑상선절제술", "갑상선 수술", "thyroidectomy"),
    "thyroidectomy_demo": ("갑상선절제술", "갑상선 수술", "thyroidectomy"),
}
# Generic ``수술`` is intentionally stop-only. It admits the reviewed phrase
# "수술 종료" without also allowing an ambiguous bare "수술 시작".
_PROCEDURE_STOP_ONLY_ALIASES = {
    "thyroidectomy": ("수술",),
    "thyroidectomy_demo": ("수술",),
}
_PROCEDURE_START_CUES = ("시작", "개시", "스타트", "start", "begin")
_PROCEDURE_STOP_CUES = (
    "종료",
    "마무리",
    "끝내",
    "스탑",
    "stop",
    "finish",
    "end",
)
_TOOL_REQUEST_TERMS = (
    "줘",
    "주지",
    "주세요",
    "주십시오",
    "전달",
    "내놔",
    "부탁",
    "서둘러",
    "가져와",
    "handover",
    "hand over",
    "give",
    "please",
)
_TOOL_RETRIEVE_TERMS = (
    "회수",
    "치워",
    "치우",
    "정리",
    "retrieve",
    "remove",
    "clear",
)
_GENERIC_TOOL_TERMS = ("도구", "기구", "instrument", "tool")
_URGENT_TOOL_TERMS = ("빨리", "서둘러", "긴급", "urgent", "quick")
_TOOL_BACKGROUND_CUES = (
    "training",
    "disabled",
    "we call",
    "read",
)
# Reject-only ambiguity detector for two fully parameterized pull requests in
# one ASR final.  It deliberately does not parse a command or determine a
# side/distance; ``procedure_spec.normalize_retractor_command`` remains the
# sole normalizer.  Its narrow job is to prevent a later slot from silently
# overwriting an earlier independently spoken adjustment when ASR omitted a
# connector.
_EXPLICIT_PULL_ADJUSTMENT_SPAN_RE = re.compile(
    r"(?:\d+(?:[.,]\d+)?|\.\d+)\s*"
    r"(?:mm|cm|㎜|㎝|밀\s*리(?:\s*미\s*터)?|미\s*리|"
    r"센\s*티(?:\s*미\s*터)?|센\s*치(?:\s*미\s*터)?|씨\s*엠)"
    r"[^.!?;。！？；]{0,32}?"
    r"(?:당(?:겨|기(?:어)?)|땡(?:겨|기(?:어)?)|pull)",
    re.IGNORECASE,
)

# The two operator-driven demonstration bundles intentionally allow one terse,
# reviewed bilateral fine-adjustment command.  It is kept as an exact phrase
# set so an arbitrary mention of "pull" cannot acquire a distance or target.
_DEMO_BARE_BILATERAL_ADJUSTMENT_PROCEDURES = frozenset(
    {"thyroidectomy_demo", "inguinal_hernia_repair_demo"}
)
_DEMO_BARE_BILATERAL_ADJUSTMENT_PHRASES = frozenset(
    {
        "당겨줘",
        "당겨주세요",
        "당겨줄래",
        "당겨줄래요",
        "당겨주실래",
        "당겨주실래요",
        "더당겨",
        "더당겨줘",
        "더당겨달라",
        "좀더당겨줘",
        "좀더당겨달라",
        "조금더당겨줘",
        "조금더당겨달라",
        "덜당겨",
        "덜당겨줘",
        "덜당겨달라",
        "좀덜당겨줘",
        "조금덜당겨줘",
    }
)
_DEMO_BARE_BILATERAL_ADJUSTMENT_DISTANCE_M = 0.005

# Thyroidectomy operators may use a reviewed one-line adjustment form such as
# ``1 cm 더 당겨줘`` or ``오른쪽으로 1 cm 더 당겨줘``.  It remains narrowly
# anchored to one explicit mm/cm distance and the pull verb.  When the side is
# omitted, this procedure's documented default is the left retractor.
_THYROID_EXPLICIT_DISTANCE_ADJUSTMENT_RE = re.compile(
    r"^\s*(?:(?P<side>오른쪽|왼쪽|right|left)\s*(?:으로|로)?\s*)?"
    r"(?:\d+(?:[.,]\d+)?|\.\d+)\s*"
    r"(?:mm|cm|㎜|㎝|밀\s*리(?:\s*미\s*터)?|미\s*리|"
    r"센\s*티(?:\s*미\s*터)?|센\s*치(?:\s*미\s*터)?|씨\s*엠)\s*"
    r"(?:씩\s*)?(?:만(?:큼)?\s*)?(?:(?:조금|좀|살짝)\s*)?"
    r"(?:(?:더|덜)\s*)?당(?:겨|기(?:어)?)\s*"
    r"(?:줘|주세요|요)?\s*[.!?]?\s*$",
    re.IGNORECASE,
)

_THYROID_EXPLICIT_ADJUSTMENT_SIDE = {
    "오른쪽": RetractionTargetSide.RIGHT,
    "오룬쪽": RetractionTargetSide.RIGHT,
    "오른편": RetractionTargetSide.RIGHT,
    "오른팔": RetractionTargetSide.RIGHT,
    "오른쪽팔": RetractionTargetSide.RIGHT,
    "우측": RetractionTargetSide.RIGHT,
    "우방": RetractionTargetSide.RIGHT,
    "라이트": RetractionTargetSide.RIGHT,
    "right": RetractionTargetSide.RIGHT,
    "왼쪽": RetractionTargetSide.LEFT,
    "왠쪽": RetractionTargetSide.LEFT,
    "왼편": RetractionTargetSide.LEFT,
    "왼팔": RetractionTargetSide.LEFT,
    "왼쪽팔": RetractionTargetSide.LEFT,
    "좌측": RetractionTargetSide.LEFT,
    "좌방": RetractionTargetSide.LEFT,
    "레프트": RetractionTargetSide.LEFT,
    "left": RetractionTargetSide.LEFT,
}
_THYROID_EXPLICIT_ADJUSTMENT_SIDE_RE = re.compile(
    "|".join(
        re.escape(term)
        for term in sorted(
            _THYROID_EXPLICIT_ADJUSTMENT_SIDE,
            key=len,
            reverse=True,
        )
    ),
    re.IGNORECASE,
)
_THYROID_PULL_STEM_RE = re.compile(r"(?:당|땡)(?:겨|기)")


def _canonicalize_thyroid_pull_aliases(value: str) -> str:
    """Keep the common spoken ``땡겨`` form inside the reviewed pull intent."""

    return re.sub(r"땡(?=(?:겨|기))", "당", value)


def _thyroid_explicit_adjustment_side(
    value: str,
) -> RetractionTargetSide | None:
    """Return one unambiguous spoken side for a thyroid adjustment phrase.

    This is deliberately semantic only within the thyroidectomy demo: an
    explicit side, explicit distance (grounded by ``procedure_spec`` below),
    and a pull stem are enough.  Filler wording and order are not command
    slots, so they must not affect the result.
    """

    if not _THYROID_PULL_STEM_RE.search(value):
        return None
    sides = {
        _THYROID_EXPLICIT_ADJUSTMENT_SIDE[match.group(0).casefold()]
        for match in _THYROID_EXPLICIT_ADJUSTMENT_SIDE_RE.finditer(value)
    }
    return next(iter(sides)) if len(sides) == 1 else None


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
    target_side: str = "none"
    distance_m: float = 0.0
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
            "target_side": self.target_side,
            "distance_m": self.distance_m,
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
            target_side=self.target_side,
            distance_m=self.distance_m,
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
        retractor_commands: Sequence[str] | None = None,
        retractor_max_distance_m: float = 0.0,
        retractor_require_explicit_unit: bool = True,
        selector: CandidateSelector | None = None,
        allow_selector_natural_variants: bool = False,
    ) -> None:
        source = tool_aliases if tool_aliases is not None else {}
        self._tool_aliases = self._validate_aliases(source)
        self._procedure_id = str(procedure_id).strip()
        self._catalog_id = str(catalog_id).strip()
        supported_retractor_commands = {
            command.value for command in RetractionCommand
        }
        requested_retractor_commands = tuple(
            dict.fromkeys(
                str(command).strip()
                for command in (retractor_commands or ())
                if str(command).strip()
            )
        )
        unsupported_retractor_commands = sorted(
            set(requested_retractor_commands) - supported_retractor_commands
        )
        if unsupported_retractor_commands:
            raise ValueError(
                "unsupported procedure retractor commands: "
                + ", ".join(unsupported_retractor_commands)
            )
        self._retractor_commands = frozenset(requested_retractor_commands)
        self._retractor_max_distance_m = max(
            0.0,
            float(retractor_max_distance_m),
        )
        self._retractor_require_explicit_unit = bool(
            retractor_require_explicit_unit
        )
        if (
            RetractionCommand.ADJUST_RETRACTION.value in self._retractor_commands
            and self._retractor_max_distance_m <= 0.0
        ):
            raise ValueError(
                "adjust_retraction requires a positive procedure max distance"
            )
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
        """Resolve one scenario-local semantic intent with no registry hop.

        This is deliberately the end of the deterministic voice path.  The
        resolver grounds only tool aliases, procedure lifecycle phrases, and
        procedure-supported retraction slots.  The typed consumers own their
        endpoint validation and idempotency; a VLM function, registry binding,
        schema fingerprint, or scenario command allowlist never participates.
        """

        raw = str(raw_text or "")
        proposal = self._resolve_unbound(raw)
        multiple_command_clauses = self._has_multiple_executable_command_clauses(raw)
        if multiple_command_clauses:
            proposal = VoiceIntentProposal.reject(
                raw,
                normalize_text(raw),
                reason="multiple_command_clauses",
                evidence_spans=("compound",),
            )
        if not multiple_command_clauses and not proposal.is_executable_proposal:
            final_clause = bounded_final_command_clause(raw)
            if final_clause:
                final_proposal = self._resolve_unbound(final_clause)
                if final_proposal.is_executable_proposal:
                    proposal = replace(
                        final_proposal,
                        raw_text=raw,
                        normalized_text=normalize_text(raw),
                    )
        if (
            not proposal.is_executable_proposal
            and proposal.reason
            in {
                "procedure_control_not_a_standalone_command",
                "direct_teach_not_a_standalone_command",
                "tool_named_without_handover_anchor",
                "no_reviewed_command_candidate",
            }
            and not multiple_command_clauses
            and not command_context_is_blocked(raw)
            and not _has_tool_background_context(raw)
        ):
            # A final ASR result may contain an older sentence immediately
            # before the current command without punctuation.  Reuse the same
            # resolver on bounded suffixes instead of adding a second command
            # grammar or asking a VLM to reconstruct intent.  Longest suffixes
            # run first so direction, distance, and tool slots are retained.
            for suffix in bounded_command_suffixes(raw):
                if final_clause and normalize_text(suffix) == normalize_text(final_clause):
                    continue
                suffix_proposal = self._resolve_unbound(suffix)
                if (
                    suffix_proposal.is_executable_proposal
                    and _suffix_preserves_retractor_command_shape(
                        raw,
                        suffix_proposal,
                    )
                ):
                    proposal = replace(
                        suffix_proposal,
                        raw_text=raw,
                        normalized_text=normalize_text(raw),
                    )
                    break
        return replace(
            proposal,
            procedure_id=self._procedure_id,
            catalog_id=self._catalog_id,
        )

    def _has_multiple_executable_command_clauses(self, raw: str) -> bool:
        """Reject two real commands even when ASR omits a connector.

        The fast catalog owner performs the same coarse screening before it
        dispatches catalog-only commands. Keeping the common span detector
        here closes the symmetric resolver-only case without a second VLM or
        scenario-policy hop.
        """

        if len(semantic_command_categories(raw)) > 1:
            return True

        if (
            RetractionCommand.ADJUST_RETRACTION.value
            in self._retractor_commands
            and len(_EXPLICIT_PULL_ADJUSTMENT_SPAN_RE.findall(raw)) > 1
        ):
            return True

        clauses = explicit_command_clauses(raw)
        if len(clauses) < 2:
            return False
        executable = 0
        for clause in clauses:
            if self._resolve_unbound(clause).is_executable_proposal:
                executable += 1
                if executable > 1:
                    return True
        return False

    def _catalog_retractor_match(
        self,
        raw: str,
        compact: str,
    ) -> tuple[NormalizedRetractionCommand | None, str]:
        """Normalize one procedure-supported retractor phrase once, safely."""

        thyroid_compact = _canonicalize_thyroid_pull_aliases(compact)
        demo_bare_bilateral_adjustment = bool(
            self._procedure_id
            in _DEMO_BARE_BILATERAL_ADJUSTMENT_PROCEDURES
            and RetractionCommand.ADJUST_RETRACTION.value
            in self._retractor_commands
            and thyroid_compact in _DEMO_BARE_BILATERAL_ADJUSTMENT_PHRASES
        )
        thyroid_explicit_adjustment = (
            _THYROID_EXPLICIT_DISTANCE_ADJUSTMENT_RE.fullmatch(thyroid_compact)
            if self._procedure_id == "thyroidectomy_demo"
            and RetractionCommand.ADJUST_RETRACTION.value
            in self._retractor_commands
            else None
        )
        thyroid_explicit_side = (
            _thyroid_explicit_adjustment_side(thyroid_compact)
            if self._procedure_id == "thyroidectomy_demo"
            and RetractionCommand.ADJUST_RETRACTION.value
            in self._retractor_commands
            else None
        )
        thyroid_default_left_adjustment = bool(
            thyroid_explicit_adjustment is not None
            and thyroid_explicit_adjustment.group("side") is None
        )
        normalized_retractor = None
        if self._retractor_commands:
            if demo_bare_bilateral_adjustment:
                normalized_retractor = NormalizedRetractionCommand(
                    command=RetractionCommand.ADJUST_RETRACTION,
                    target_side=RetractionTargetSide.BOTH,
                    distance_m=(
                        -_DEMO_BARE_BILATERAL_ADJUSTMENT_DISTANCE_M
                        if "덜당" in thyroid_compact
                        else _DEMO_BARE_BILATERAL_ADJUSTMENT_DISTANCE_M
                    ),
                    confidence=1.0,
                    reason=(
                        "normalized_adjust_retraction_demo_default_bilateral_5mm"
                    ),
                )
            else:
                # The procedure-owned adapter supplies only the two omitted
                # slots documented for this demo (the retraction noun and a
                # default/explicit side).  Parsing distance, direction of
                # travel, units, and all generic retraction vocabulary stays
                # in the single ``procedure_spec`` normalizer below.
                normalization_text = raw
                scenario_target_side: RetractionTargetSide | None = None
                if thyroid_explicit_side is not None:
                    normalization_text = f"리트랙션 {thyroid_compact}"
                    scenario_target_side = thyroid_explicit_side
                elif thyroid_default_left_adjustment:
                    normalization_text = f"왼쪽 리트랙션 {thyroid_compact}"
                    scenario_target_side = RetractionTargetSide.LEFT
                normalized_retractor = normalize_retractor_command(
                    normalization_text,
                    RetractionState.UNKNOWN,
                    enforce_state=False,
                )
                if (
                    scenario_target_side is not None
                    and normalized_retractor.command is not None
                ):
                    normalized_retractor = replace(
                        normalized_retractor,
                        target_side=scenario_target_side,
                        reason=(
                            "normalized_adjust_retraction_thyroid_explicit_side_"
                            "explicit_adjustment_distance"
                        ),
                    )
        catalog_match = (
            normalized_retractor
            if normalized_retractor is not None
            and normalized_retractor.command is not None
            and normalized_retractor.command.value in self._retractor_commands
            and normalized_retractor.command
            not in {
                RetractionCommand.START_DIRECT_TEACH,
                RetractionCommand.FINISH_DIRECT_TEACH,
            }
            else None
        )
        rejection_reason = ""
        if (
            catalog_match is not None
            and catalog_match.command is RetractionCommand.ADJUST_RETRACTION
        ):
            if (
                self._retractor_require_explicit_unit
                and not demo_bare_bilateral_adjustment
                and "explicit_adjustment_distance" not in catalog_match.reason
            ):
                rejection_reason = "adjustment_requires_explicit_distance_unit"
            elif (
                abs(catalog_match.distance_m)
                > self._retractor_max_distance_m + 1e-12
            ):
                rejection_reason = (
                    "adjustment_exceeds_procedure_distance_limit"
                )
        return catalog_match, rejection_reason

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
        procedure_control = _match_procedure_control(
            compact,
            procedure_id=self._procedure_id,
        )
        procedure_aliases = (
            *_procedure_aliases_for(self._procedure_id),
            *_procedure_stop_only_aliases_for(self._procedure_id),
        )
        mentions_active_procedure = any(
            normalize_text(alias).replace(" ", "") in compact
            for alias in procedure_aliases
        )
        has_procedure_lifecycle_cue = bool(
            _matched_terms(
                normalized,
                compact,
                (*_PROCEDURE_START_CUES, *_PROCEDURE_STOP_CUES),
            )
        )
        if mentions_active_procedure and has_procedure_lifecycle_cue:
            if _has_question(raw, normalized, compact):
                return VoiceIntentProposal.reject(
                    raw,
                    normalized,
                    reason="procedure_control_question_not_executable",
                    evidence_spans=("question",),
                )
            if _has_negation(normalized, compact):
                return VoiceIntentProposal.reject(
                    raw,
                    normalized,
                    reason="procedure_control_negated",
                    evidence_spans=("negation",),
                )
            if command_context_is_blocked(raw):
                return VoiceIntentProposal.reject(
                    raw,
                    normalized,
                    reason="procedure_control_background_mention",
                    evidence_spans=("background",),
                )
            if procedure_control is None:
                return VoiceIntentProposal.reject(
                    raw,
                    normalized,
                    reason="procedure_control_not_a_standalone_command",
                    evidence_spans=tuple(procedure_aliases),
                )
        (
            catalog_retractor_normalized,
            catalog_retractor_rejection_reason,
        ) = self._catalog_retractor_match(raw, compact)
        tool_matches = _tool_alias_matches(normalized, self._tool_aliases)
        tool_retrieve_shape = _is_explicit_tool_retrieve_shape(
            normalized,
            compact,
        )
        tool_request_shape = (
            not tool_retrieve_shape
            and _is_explicit_tool_handover_shape(
                normalized,
                compact,
            )
        )
        selector_tool_request = bool(
            self._allow_selector_natural_variants
            and tool_matches
            and _is_selector_only_tool_handover_request(compact)
        )
        bare_tool_request = bool(
            tool_matches and _is_bare_tool_handover_name(compact, tool_matches)
        )
        generic_tool_request = bool(
            not tool_matches
            and _contains_any_tool_term(normalized, compact, _GENERIC_TOOL_TERMS)
            and tool_request_shape
        )
        generic_tool_retrieve = bool(
            not tool_matches
            and _contains_any_tool_term(normalized, compact, _GENERIC_TOOL_TERMS)
            and tool_retrieve_shape
        )
        tool_request_context = bool(
            tool_matches
            and (
                tool_request_shape
                or selector_tool_request
                or bare_tool_request
            )
        )
        tool_retrieve_context = bool(tool_matches and tool_retrieve_shape)
        direct_terms = _matched_direct_teach_terms(normalized)
        repair_terms = _matched_direct_teach_repair_terms(normalized)
        has_direct_start = bool(_matched_terms(normalized, compact, _DIRECT_TEACH_START_CUES))
        has_direct_finish = bool(_matched_terms(normalized, compact, _DIRECT_TEACH_FINISH_CUES))
        command_context = bool(
            direct_terms
            or repair_terms
            or catalog_retractor_normalized is not None
            or tool_request_context
            or generic_tool_request
            or tool_retrieve_context
            or generic_tool_retrieve
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
        if command_context and command_context_is_blocked(raw):
            return VoiceIntentProposal.reject(
                raw,
                normalized,
                reason="background_command_mention",
                evidence_spans=("background",),
            )
        if catalog_retractor_rejection_reason:
            return VoiceIntentProposal.reject(
                raw,
                normalized,
                reason=catalog_retractor_rejection_reason,
                intent=INTENT_RETRACTOR_COMMAND,
                evidence_spans=("adjust_retraction",),
            )
        if _is_compound_command(normalized, compact):
            return VoiceIntentProposal.reject(
                raw,
                normalized,
                reason="multiple_command_phrases",
                evidence_spans=("compound",),
            )
        if tool_matches and _contains_tool_terminal_nonrequest(normalized, compact):
            return VoiceIntentProposal.reject(
                raw,
                normalized,
                reason="tool_handover_not_a_request",
                intent=(
                    INTENT_TOOL_RETRIEVE
                    if tool_retrieve_shape
                    else INTENT_TOOL_HANDOVER
                ),
                evidence_spans=tuple(alias for _, alias in tool_matches),
            )
        if generic_tool_retrieve:
            return VoiceIntentProposal.clarify_tool(
                raw,
                normalized,
                evidence_spans=("generic_tool_retrieve",),
                reason=(
                    "missing_tool_id"
                    if self._tool_catalog_bound
                    else "tool_catalog_unavailable"
                ),
                intent=INTENT_TOOL_RETRIEVE,
            )
        if generic_tool_request:
            return VoiceIntentProposal.clarify_tool(
                raw,
                normalized,
                evidence_spans=("generic_tool_request",),
                reason=(
                    "missing_tool_id"
                    if self._tool_catalog_bound
                    else "tool_catalog_unavailable"
                ),
            )
        candidates: list[_Candidate] = []
        if tool_retrieve_context:
            tool_ids = tuple(dict.fromkeys(tool_id for tool_id, _ in tool_matches))
            if len(tool_ids) != 1:
                return VoiceIntentProposal.reject(
                    raw,
                    normalized,
                    reason="multiple_tool_alias_candidates",
                    intent=INTENT_TOOL_RETRIEVE,
                    evidence_spans=tuple(alias for _, alias in tool_matches),
                )
            if not self._tool_catalog_bound:
                return VoiceIntentProposal.no_command(
                    raw,
                    normalized,
                    reason="tool_catalog_unavailable",
                    evidence_spans=tuple(alias for _, alias in tool_matches),
                )
            tool_id = tool_ids[0]
            aliases = tuple(alias for _, alias in tool_matches)
            candidates.append(
                _Candidate(
                    candidate_id=f"tool_retrieve:{tool_id}",
                    intent=INTENT_TOOL_RETRIEVE,
                    tool_id=tool_id,
                    provenance="deterministic_tool_retrieve_alias",
                    reason="grounded_tool_retrieve",
                    evidence_spans=aliases,
                )
            )
        elif tool_request_context:
            tool_ids = tuple(dict.fromkeys(tool_id for tool_id, _ in tool_matches))
            if len(tool_ids) != 1:
                return VoiceIntentProposal.reject(
                    raw,
                    normalized,
                    reason="multiple_tool_alias_candidates",
                    intent=INTENT_TOOL_HANDOVER,
                    evidence_spans=tuple(alias for _, alias in tool_matches),
                )
            if not self._tool_catalog_bound:
                return VoiceIntentProposal.no_command(
                    raw,
                    normalized,
                    reason="tool_catalog_unavailable",
                    evidence_spans=tuple(alias for _, alias in tool_matches),
                )
            tool_id = tool_ids[0]
            aliases = tuple(alias for _, alias in tool_matches)
            candidates.append(
                _Candidate(
                    candidate_id=f"tool_handover:{tool_id}",
                    intent=INTENT_TOOL_HANDOVER,
                    tool_id=tool_id,
                    urgency=(
                        "urgent"
                        if _contains_any_tool_term(
                            normalized, compact, _URGENT_TOOL_TERMS
                        )
                        else "routine"
                    ),
                    provenance="deterministic_tool_alias",
                    requires_confirmation=selector_tool_request,
                    selector_required=selector_tool_request,
                    reason="grounded_tool_handover",
                    evidence_spans=aliases,
                )
            )
        elif tool_matches:
            # Naming a tool in observation/background speech is not a handover
            # request. Keep the reason useful to the operator without turning
            # the mention into an executable proposal.
            return VoiceIntentProposal.no_command(
                raw,
                normalized,
                reason="tool_named_without_handover_anchor",
                evidence_spans=tuple(alias for _, alias in tool_matches),
            )
        if procedure_control is not None:
            action, evidence = procedure_control
            candidates.append(
                _Candidate(
                    candidate_id=f"{action}:{self._procedure_id}",
                    intent=(
                        INTENT_PROCEDURE_START
                        if action == "procedure_start"
                        else INTENT_PROCEDURE_STOP
                    ),
                    provenance="deterministic_procedure_control_alias",
                    reason=f"grounded_{action}",
                    evidence_spans=evidence,
                )
            )
        if (
            catalog_retractor_normalized is not None
            and not catalog_retractor_rejection_reason
            and catalog_retractor_normalized.command
            not in {
                RetractionCommand.START_DIRECT_TEACH,
                RetractionCommand.FINISH_DIRECT_TEACH,
            }
        ):
            command = catalog_retractor_normalized.command
            assert command is not None
            # The procedure-local retractor vocabulary is the semantic
            # boundary.  The typed endpoint adapter still validates the
            # command/slot shape, state, and idempotent request identity.
            candidates.append(
                _Candidate(
                    candidate_id=f"retractor_command:{command.value}",
                    intent=INTENT_RETRACTOR_COMMAND,
                    retractor_command=command.value,
                    target_side=(
                        catalog_retractor_normalized.target_side.value
                    ),
                    distance_m=float(
                        catalog_retractor_normalized.distance_m
                    ),
                    provenance="deterministic_retractor_catalog",
                    reason=catalog_retractor_normalized.reason,
                    evidence_spans=(command.value,),
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
        # Direct-teach phrases are executable only when the active
        # procedure's retraction vocabulary opted into them.  An unbound or
        # tool-only catalog must not gain a retraction command by omission.
        start_direct_teach_allowed = (
            RetractionCommand.START_DIRECT_TEACH.value
            in self._retractor_commands
        )
        finish_direct_teach_allowed = (
            RetractionCommand.FINISH_DIRECT_TEACH.value
            in self._retractor_commands
        )
        if direct_terms and canonical_start_shape and start_direct_teach_allowed:
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
        elif (
            direct_terms
            and canonical_finish_shape
            and finish_direct_teach_allowed
        ):
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
        elif repair_terms and repair_start_shape and start_direct_teach_allowed:
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
        elif (
            repair_terms
            and repair_finish_shape
            and finish_direct_teach_allowed
        ):
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
            and start_direct_teach_allowed
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


def _procedure_aliases_for(procedure_id: str) -> tuple[str, ...]:
    """Return only reviewed aliases for the currently bound procedure."""

    return tuple(_PROCEDURE_SPOKEN_ALIASES.get(str(procedure_id).casefold(), ()))


def _procedure_stop_only_aliases_for(procedure_id: str) -> tuple[str, ...]:
    """Return reviewed aliases that may pause, but can never start, a run."""

    return tuple(
        _PROCEDURE_STOP_ONLY_ALIASES.get(str(procedure_id).casefold(), ())
    )


def _match_procedure_control(
    compact: str,
    *,
    procedure_id: str,
) -> tuple[str, tuple[str, ...]] | None:
    """Match one short, explicit lifecycle command for the active procedure.

    Long surrounding speech, a question, and a negation are rejected by the
    caller.  This routine only accepts an alias plus one terminal action, so
    background discussion cannot start or stop a scenario by accident.
    """

    prefixes = r"(?:(?:자|이제|그럼|그러면|좀|한번|한번만|우리|바로|지금))*"
    particle = r"(?:을|를|은|는|이|가|도|만|좀)?"
    filler = r"(?:(?:바로|지금|좀|한번|한번만))*"
    start = (
        r"(?:시작(?:하(?:겠습니다|겠어요|자|죠)?|해(?:보자|요|줘|주세요)?|합니다|할게(?:요)?)?"
        r"|개시(?:하(?:겠습니다|겠어요|자)?|해(?:보자|요)?|합니다)?|스타트|start|begin)"
    )
    stop = (
        r"(?:종료(?:하(?:겠습니다|겠어요|자|죠)?|해(?:보자|요)?|합니다|할게(?:요)?)?"
        r"|마무리(?:하(?:겠습니다|겠어요|자|죠)?|해(?:보자|요)?|합니다|할게(?:요)?)?"
        r"|끝내(?:겠습니다|겠어요|자|요|다)?|스탑|stop|finish|end)"
    )
    for alias in _procedure_aliases_for(procedure_id):
        compact_alias = normalize_text(alias).replace(" ", "")
        if not compact_alias:
            continue
        base = rf"{prefixes}{re.escape(compact_alias)}{particle}{filler}"
        if re.fullmatch(base + start, compact):
            return "procedure_start", (alias, "시작")
        if re.fullmatch(base + stop, compact):
            return "procedure_stop", (alias, "종료")
    for alias in _procedure_stop_only_aliases_for(procedure_id):
        compact_alias = normalize_text(alias).replace(" ", "")
        if not compact_alias:
            continue
        base = rf"{prefixes}{re.escape(compact_alias)}{particle}{filler}"
        if re.fullmatch(base + stop, compact):
            return "procedure_stop", (alias, "종료")
    return None


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


def _tool_alias_matches(
    text: str,
    aliases_by_tool: Mapping[str, Sequence[str]],
) -> tuple[tuple[str, str], ...]:
    """Return the authored aliases actually spoken, without global IDs."""

    matches: list[tuple[str, str]] = []
    for tool_id, aliases in aliases_by_tool.items():
        for alias in aliases:
            if _contains_tool_alias(text, alias):
                matches.append((str(tool_id), str(alias)))
    return tuple(matches)


def _contains_any_tool_term(
    text: str,
    compact: str,
    terms: Sequence[str],
) -> bool:
    return bool(_matched_terms(text, compact, terms))


def _is_explicit_tool_handover_shape(text: str, compact: str) -> bool:
    """Recognize a compact request while excluding quoted/background speech."""

    if _has_tool_background_context(text):
        return False
    # "보비 좀 부탁해" is intentionally not an immediate command.  It may be
    # offered as a confirmation-required selector candidate only when that
    # optional mode is explicitly enabled below.
    if "부탁해" in compact:
        return False
    return _contains_any_tool_term(text, compact, _TOOL_REQUEST_TERMS)


def _is_explicit_tool_retrieve_shape(text: str, compact: str) -> bool:
    """Recognize an explicit tool return/recovery request.

    The resolver accepts natural spacing and polite endings because it matches
    the stable retrieval stem, while the surrounding catalog alias remains the
    only source of a tool ID.
    """

    if _has_tool_background_context(text):
        return False
    return _contains_any_tool_term(text, compact, _TOOL_RETRIEVE_TERMS)


def _has_tool_background_context(text: str) -> bool:
    """Keep a quoted, explanatory, or training mention out of command recovery."""

    lowered = str(text or "").casefold()
    return bool(
        command_is_reported_or_quoted(text)
        or any(cue in lowered for cue in _TOOL_BACKGROUND_CUES)
        or re.search(r"\b(?:is this|what is|why is|can you explain)\b", lowered)
    )


def _suffix_preserves_retractor_command_shape(
    raw: str,
    proposal: VoiceIntentProposal,
) -> bool:
    """Do not turn rejected explicit slots into a shorter default command."""

    if (
        proposal.intent != INTENT_RETRACTOR_COMMAND
        or proposal.retractor_command != RetractionCommand.ADJUST_RETRACTION.value
    ):
        return True
    if "explicit_adjustment_distance" not in proposal.reason:
        # A suffix such as ``당겨줘`` has the demo's 5 mm default.  It must
        # not erase a spoken 4 cm/invalid/unitless measurement after a longer
        # candidate was rejected.  Reuse the physical parameter normalizer
        # instead of duplicating its number/unit grammar here.  Only supply
        # a neutral side when the original utterance did not contain one.
        original_parameters = normalize_retractor_adjustment_parameters(raw)
        if original_parameters.reason == "adjustment_side_missing":
            original_parameters = normalize_retractor_adjustment_parameters(
                f"양쪽 {raw}"
            )
        if (
            original_parameters.command is None
            or "default_adjustment_distance" not in original_parameters.reason
            or original_parameters.target_side.value != proposal.target_side
            or (original_parameters.distance_m < 0.0) != (proposal.distance_m < 0.0)
        ):
            return False
    normalized = _canonicalize_thyroid_pull_aliases(normalize_text(raw))
    compact = normalized.replace(" ", "")
    mentions_retractor = any(
        term in compact
        for term in ("리트랙션", "retraction", "retractor")
    )
    return not mentions_retractor or _THYROID_EXPLICIT_ADJUSTMENT_SIDE_RE.search(normalized) is not None


def _is_bare_tool_handover_name(
    compact: str,
    tool_matches: Sequence[tuple[str, str]],
) -> bool:
    """Accept exactly one spoken catalog alias as a handover request.

    An operator's complete final utterance may be just ``보비`` or
    ``Adson forceps``.  Do not broaden that into arbitrary sentences which
    happen to mention a tool: the normalized whole utterance must exactly
    equal a currently matched, procedure-local alias.  Questions and
    negations are still rejected by the caller's shared command guards.
    """

    return any(
        compact == normalize_text(alias).replace(" ", "")
        for _, alias in tool_matches
    )


def _contains_tool_terminal_nonrequest(text: str, compact: str) -> bool:
    """Recognize a bounded terminal statement that must not hand over a tool."""

    lowered = text.casefold()
    return "finished" in lowered or "완료" in compact


def _is_selector_only_tool_handover_request(compact: str) -> bool:
    """Keep one observed softened request outside the automatic path."""

    return bool(re.search(r"(?:좀)?부탁해(?:요)?$", compact))


def _is_compound_command(text: str, compact: str) -> bool:
    """Reject multi-command STT segments instead of selecting one fragment."""

    if "그리고" not in compact and " then " not in text.casefold() and " and " not in text.casefold():
        return False
    command_terms = (
        *_TOOL_REQUEST_TERMS,
        *_TOOL_RETRIEVE_TERMS,
        *_DIRECT_TEACH_TERMS,
        *_PROCEDURE_START_CUES,
        *_PROCEDURE_STOP_CUES,
    )
    return len(_matched_terms(text, compact, command_terms)) >= 2


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
        else r"(?:직접교시|교시|directteach(?:ing)?|teaching)"
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


def _dedupe(values: Sequence[str] | object) -> tuple[str, ...]:
    # ``values`` is often a generator assembled from candidate evidence.
    return tuple(dict.fromkeys(str(value) for value in values if str(value)))
