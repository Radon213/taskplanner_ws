"""Digital twin belief manager for taskplanner v1."""

from __future__ import annotations

from collections import Counter, defaultdict, deque
from dataclasses import asdict
from difflib import SequenceMatcher
from typing import Any
import json
import math
import re
import time

from procedure_spec import ProcedureSpec
from surgical_msgs.msg import (
    BedRobotArmGroupRequest,
    BedRobotArmGroupStatus,
    FilteredPhase,
    PhaseEvidence,
    PhaseTransitionCue,
    SurgeonActorEvent,
    SurgeonRequest,
    ToolObservation,
    TwinEvent,
)

from .models import (
    ActiveRobotTask,
    BedRobotArmGroupBelief,
    InstrumentBelief,
    SurgeonRequestCue,
    TwinState,
    LIFECYCLE_CLEANED_LEFT,
    LIFECYCLE_CLEANING_LEFT,
    LIFECYCLE_DROPPED_FLOOR,
    LIFECYCLE_HOME_RACK,
    LIFECYCLE_MAYO_RECOVERY,
    LIFECYCLE_MAYO_REUSE,
    LIFECYCLE_PREPOSITIONED_RIGHT,
    LIFECYCLE_RECOVERING_LEFT,
    LIFECYCLE_RETURNED_HOME,
    LIFECYCLE_SURGEON_OWNED,
)


BED_ROBOT_ARM_GROUP_IDS = ("suction", "retraction")


SURGEON_OWNED_LOCATION_TYPES = {"surgeon_hand", "surgical_field", "bed_fixed_tool", "return_zone"}
ACTIVE_REQUEST_INTENTS = {"request_tool", "voice_request", "extend_hand_for_handover"}
ACTIVE_RETURN_INTENTS = {"return_tool", "extend_hand_for_retrieval"}
RIGHT_HAND_LIFECYCLES = {LIFECYCLE_PREPOSITIONED_RIGHT}
LEFT_HAND_LIFECYCLES = {LIFECYCLE_RECOVERING_LEFT}
PENDING_TRANSITIONS_REQUIRE_ACTION = {
    "recover_left",
    "clean_left",
    "return_home",
    "return_unused_preposition",
    "human_recovery_required",
}
PHASE_INTERACTION_MIN_FRACTION = 0.4
BLOCKING_SAFETY_FLAGS = {
    "right_arm_overloaded",
    "left_arm_overloaded",
    "surgeon_owned_overloaded",
    "duplicate_tool_holder",
    "vlm_unhealthy",
    "dropped_tool_requires_human",
}
ALLOWED_EVENT_TRANSITIONS = {
    LIFECYCLE_HOME_RACK: {LIFECYCLE_PREPOSITIONED_RIGHT, LIFECYCLE_RETURNED_HOME},
    LIFECYCLE_RETURNED_HOME: {LIFECYCLE_PREPOSITIONED_RIGHT, LIFECYCLE_SURGEON_OWNED},
    LIFECYCLE_PREPOSITIONED_RIGHT: {
        LIFECYCLE_PREPOSITIONED_RIGHT,
        LIFECYCLE_SURGEON_OWNED,
        LIFECYCLE_RETURNED_HOME,
        LIFECYCLE_DROPPED_FLOOR,
    },
    LIFECYCLE_SURGEON_OWNED: {LIFECYCLE_SURGEON_OWNED, LIFECYCLE_MAYO_REUSE, LIFECYCLE_MAYO_RECOVERY, LIFECYCLE_RECOVERING_LEFT, LIFECYCLE_DROPPED_FLOOR},
    LIFECYCLE_MAYO_REUSE: {LIFECYCLE_MAYO_REUSE, LIFECYCLE_PREPOSITIONED_RIGHT, LIFECYCLE_SURGEON_OWNED, LIFECYCLE_MAYO_RECOVERY, LIFECYCLE_RECOVERING_LEFT, LIFECYCLE_DROPPED_FLOOR},
    LIFECYCLE_MAYO_RECOVERY: {LIFECYCLE_MAYO_RECOVERY, LIFECYCLE_PREPOSITIONED_RIGHT, LIFECYCLE_SURGEON_OWNED, LIFECYCLE_RECOVERING_LEFT, LIFECYCLE_DROPPED_FLOOR},
    LIFECYCLE_DROPPED_FLOOR: {LIFECYCLE_RETURNED_HOME},
    LIFECYCLE_RECOVERING_LEFT: {LIFECYCLE_RECOVERING_LEFT, LIFECYCLE_CLEANING_LEFT, LIFECYCLE_RETURNED_HOME},
    LIFECYCLE_CLEANING_LEFT: {LIFECYCLE_CLEANING_LEFT, LIFECYCLE_CLEANED_LEFT},
    LIFECYCLE_CLEANED_LEFT: {LIFECYCLE_CLEANED_LEFT, LIFECYCLE_RETURNED_HOME},
}
OBSERVATION_STICKY_DIRECT_BLOCKS = {
    (LIFECYCLE_HOME_RACK, LIFECYCLE_SURGEON_OWNED),
    (LIFECYCLE_SURGEON_OWNED, LIFECYCLE_RETURNED_HOME),
    (LIFECYCLE_MAYO_REUSE, LIFECYCLE_MAYO_RECOVERY),
}
CAM4_MAYO_OBSERVATION_SOURCES = frozenset(
    {
        "cam4_rfdetr_mayo_observation",
        "vlm_cam4_mayo_observation",
    }
)
CAM4_MAYO_TRANSITION_MIN_CONFIDENCE = 0.60
SURGEON_ACTOR_LOCATION_EVENTS = {
    "place_on_mayo": ("mayo_reuse_zone", "mayo_reuse_zone", LIFECYCLE_MAYO_REUSE),
    "place_on_mayo_reuse": ("mayo_reuse_zone", "mayo_reuse_zone", LIFECYCLE_MAYO_REUSE),
    "place_on_mayo_recovery": ("mayo_recovery_zone", "mayo_recovery_zone", LIFECYCLE_MAYO_RECOVERY),
    "continue_using": ("surgeon_hand", "surgeon_hand", LIFECYCLE_SURGEON_OWNED),
}


def _separate_latin_hangul_boundaries(raw_text: str) -> str:
    return re.sub(
        r"(?<=[0-9a-z_])(?=[가-힣])|(?<=[가-힣])(?=[0-9a-z_])",
        " ",
        str(raw_text or ""),
        flags=re.IGNORECASE,
    )


def _stamp_to_sec(stamp) -> float:
    return float(stamp.sec) + float(stamp.nanosec) / 1_000_000_000.0


def _normalize_request_text(raw_text: str) -> list[str]:
    lowered = raw_text.lower()
    stop_words = {
        "but",
        "instead",
        "please",
        "rather",
        "tool",
        "the",
        "a",
        "an",
        "다시",
        "줘",
        "주세요",
        "부탁해",
        "부탁합니다",
    }

    def tokens_for(text: str) -> list[str]:
        cleaned = re.sub(
            r"[^a-z0-9_가-힣\s]",
            " ",
            _separate_latin_hangul_boundaries(text),
        )
        return [
            token
            for token in cleaned.split()
            if token and token not in stop_words
        ]

    def ngrams(tokens: list[str], *, latest_first: bool) -> list[str]:
        spans = [
            (start, width, " ".join(tokens[start : start + width]))
            for width in range(1, len(tokens) + 1)
            for start in range(0, len(tokens) - width + 1)
        ]
        if latest_first:
            spans.sort(key=lambda item: (item[0] + item[1], item[1]), reverse=True)
        else:
            spans.sort(key=lambda item: (-item[1], item[0]))
        return [candidate for _, _, candidate in spans if candidate]

    candidates: list[str] = []
    correction_parts = re.split(
        r"\b(?:not|instead(?:\s+of)?|rather(?:\s+than)?)\b|"
        r"(?:아니(?:야|고|라)?|말고)",
        lowered,
    )
    if len(correction_parts) > 1:
        for candidate in ngrams(tokens_for(correction_parts[-1]), latest_first=True):
            if candidate not in candidates:
                candidates.append(candidate)
    for candidate in ngrams(tokens_for(lowered), latest_first=False):
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates


_PROCEDURE_CONTROL_PATTERN = re.compile(
    r"\b(?:start|begin|commence|resume|continue)(?:s|d|ing)?\b|"
    r"(?:시작|개시|재개|진행|계속)\s*"
    r"(?:하겠습니다|하겠어요|하죠|하자|합니다|합시다|해요|해서|해|할게요|할게)?",
    re.IGNORECASE,
)
_PROCEDURE_CONTEXT_TOKENS = frozenset(
    {
        "operation",
        "phase",
        "procedure",
        "stage",
        "surgery",
        "단계",
        "수술",
        "시술",
        "절제술",
    }
)
_PROCEDURE_NAME_STOP_TOKENS = frozenset(
    {
        "a",
        "an",
        "demonstration",
        "demo",
        "open",
        "procedure",
        "repair",
        "surgery",
        "the",
        "시연",
    }
)
_HANDOVER_REQUEST_PATTERN = re.compile(
    r"\b(?:please|pass|hand|give|get\s+me|i\s+need|i\s+want|"
    r"can\s+i\s+have)\b|"
    r"(?<![가-힣])(?:줘(?:요|보세요)?|주(?:세요|시고|십시오)|"
    r"건네(?:줘|주세요)?|달라|부탁(?:해|합니다)?|받고)"
    r"(?=$|[\s,.!?])",
    re.IGNORECASE,
)
_NON_HANDOVER_TOOL_ACTION_PATTERN = re.compile(
    r"\b(?:clean|wipe|wash|remove|discard|stop|turn\s+off|put\s+down|"
    r"take\s+away)\b|"
    r"(?:닦|세척|소독|정리|치워|버려|제거|회수|빼|멈춰|정지|꺼)",
    re.IGNORECASE,
)
_ASR_REQUEST_STOP_TOKENS = frozenset(
    {
        "a",
        "an",
        "another",
        "can",
        "get",
        "give",
        "hand",
        "have",
        "i",
        "me",
        "more",
        "need",
        "one",
        "pass",
        "please",
        "the",
        "want",
        "건네",
        "달라",
        "부탁",
        "주세요",
        "줘",
    }
)
_ASR_GENERIC_TOOL_TOKENS = frozenset(
    {
        "blade",
        "cautery",
        "clamp",
        "device",
        "dissector",
        "forceps",
        "hemostat",
        "holder",
        "instrument",
        "needle",
        "retractor",
        "scalpel",
        "scissors",
        "shear",
        "suction",
        "tool",
        "가위",
        "겸자",
        "기구",
        "리트랙터",
        "메스",
        "석션",
        "전기소작기",
        "포셉",
    }
)


def _lexical_tokens(raw_text: str) -> list[str]:
    return re.findall(
        r"[0-9a-z_가-힣]+",
        _separate_latin_hangul_boundaries(raw_text).lower(),
    )


def _compact_lexical_text(raw_text: str) -> str:
    return "".join(_lexical_tokens(raw_text))


def _has_handover_request_marker(raw_text: str) -> bool:
    return bool(_HANDOVER_REQUEST_PATTERN.search(str(raw_text or "")))


def _has_non_handover_tool_action(raw_text: str) -> bool:
    return bool(_NON_HANDOVER_TOOL_ACTION_PATTERN.search(str(raw_text or "")))


def _latin_soundex(raw_text: str) -> str:
    letters = re.sub(r"[^a-z]", "", str(raw_text or "").lower())
    if not letters:
        return ""
    codes = {
        **dict.fromkeys("bfpv", "1"),
        **dict.fromkeys("cgjkqsxz", "2"),
        **dict.fromkeys("dt", "3"),
        "l": "4",
        **dict.fromkeys("mn", "5"),
        "r": "6",
    }
    result = letters[0].upper()
    previous = codes.get(letters[0], "")
    for letter in letters[1:]:
        code = codes.get(letter, "")
        if code and code != previous:
            result += code
            if len(result) == 4:
                break
        previous = code
    return result.ljust(4, "0")


def _asr_name_match_score(left: str, right: str) -> float:
    if not left or not right or min(len(left), len(right)) < 4:
        return 0.0
    ratio = SequenceMatcher(None, left, right).ratio()
    if ratio >= 0.78:
        return ratio
    left_soundex = _latin_soundex(left)
    right_soundex = _latin_soundex(right)
    if left_soundex and left_soundex == right_soundex and ratio >= 0.60:
        return ratio
    return 0.0


def _resolve_asr_fuzzy_request_tool(
    spec: ProcedureSpec,
    raw_text: str,
) -> str:
    """Resolve one unambiguous spoken tool name inside an explicit request."""

    if (
        not _has_handover_request_marker(raw_text)
        or _has_non_handover_tool_action(raw_text)
        or _has_procedure_reference(spec, raw_text)
    ):
        return ""
    text_tokens = _lexical_tokens(raw_text)
    spoken_classes = set(text_tokens) & _ASR_GENERIC_TOOL_TOKENS
    distinctive = [
        token
        for token in text_tokens
        if token not in _ASR_REQUEST_STOP_TOKENS
        and token not in _ASR_GENERIC_TOOL_TOKENS
    ]
    spoken_names = {
        "".join(distinctive[start : start + width])
        for width in (1, 2)
        for start in range(0, len(distinctive) - width + 1)
    }
    if not spoken_names:
        return ""

    scores: dict[str, float] = {}
    for instrument in spec.bundle.instruments:
        best = 0.0
        for alias in {
            instrument.display_name,
            instrument.display_name_ko,
            *instrument.aliases,
        }:
            alias_tokens = _lexical_tokens(alias)
            alias_classes = set(alias_tokens) & _ASR_GENERIC_TOOL_TOKENS
            if spoken_classes and not (spoken_classes & alias_classes):
                continue
            alias_name = "".join(
                token
                for token in alias_tokens
                if token not in _ASR_GENERIC_TOOL_TOKENS
            )
            for spoken_name in spoken_names:
                best = max(
                    best,
                    _asr_name_match_score(spoken_name, alias_name),
                )
        if best:
            scores[instrument.id] = best

    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    if not ranked:
        return ""
    if len(ranked) > 1 and ranked[0][1] - ranked[1][1] < 0.08:
        return ""
    return ranked[0][0]


def _resolve_request_tool_with_asr_fallback(
    spec: ProcedureSpec,
    raw_text: str,
    *,
    reject_procedure_modifiers: bool = False,
) -> str:
    resolved = _resolve_request_tool(
        spec,
        raw_text,
        reject_procedure_modifiers=reject_procedure_modifiers,
    )
    if resolved:
        return resolved
    return _resolve_asr_fuzzy_request_tool(spec, raw_text)


def _resolve_compound_handover_tool(
    spec: ProcedureSpec,
    raw_text: str,
) -> str:
    """Resolve the handover clause without swallowing adjacent tool commands."""

    text = str(raw_text or "")
    handover_matches = list(_HANDOVER_REQUEST_PATTERN.finditer(text))
    non_handover_matches = list(
        _NON_HANDOVER_TOOL_ACTION_PATTERN.finditer(text)
    )
    for handover in reversed(handover_matches):
        left = max(
            (
                match.end()
                for match in non_handover_matches
                if match.end() <= handover.start()
            ),
            default=0,
        )
        right = min(
            (
                match.start()
                for match in non_handover_matches
                if match.start() >= handover.end()
            ),
            default=len(text),
        )
        segment = text[left:right].strip()
        if not segment:
            continue
        marker_start = max(0, handover.start() - left)
        marker_end = max(marker_start, handover.end() - left)
        prefix = segment[:marker_start].strip()
        suffix = segment[marker_end:].strip()
        marker = handover.group(0)
        parts = (
            (suffix, prefix, segment)
            if re.search(r"[a-z]", marker, re.IGNORECASE)
            else (prefix, suffix, segment)
        )
        for part in parts:
            resolved = _resolve_request_tool(spec, part)
            if resolved:
                return resolved
    return ""


def _procedure_name_tokens(spec: ProcedureSpec) -> set[str]:
    result: set[str] = set()
    names = (
        spec.bundle.procedure_id.replace("_", " "),
        spec.bundle.procedure_display_name,
        spec.bundle.procedure_display_name_ko,
    )
    for name in names:
        without_parenthetical = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", str(name))
        for token in _lexical_tokens(without_parenthetical):
            if len(token) >= 3 and token not in _PROCEDURE_NAME_STOP_TOKENS:
                result.add(token)
    return result


def _tokens_share_procedure_root(left: str, right: str) -> bool:
    left_token = str(left or "").strip().lower()
    right_token = str(right or "").strip().lower()
    if min(len(left_token), len(right_token)) < 3:
        return False
    return left_token in right_token or right_token in left_token


def _is_bare_procedure_name_tool_alias(
    spec: ProcedureSpec,
    raw_text: str,
    instrument_id: str,
) -> bool:
    """Reject anatomy/procedure shorthand unless the tool class is also spoken."""

    text_tokens = set(_lexical_tokens(raw_text))
    procedure_tokens = _procedure_name_tokens(spec)
    procedure_like = {
        token
        for token in text_tokens
        if any(
            _tokens_share_procedure_root(token, procedure_token)
            for procedure_token in procedure_tokens
        )
    }
    if not procedure_like:
        return False

    instrument = next(
        (
            item
            for item in spec.bundle.instruments
            if item.id == instrument_id
        ),
        None,
    )
    if instrument is None:
        return True
    instrument_tokens = set(
        _lexical_tokens(
            " ".join(
                (
                    instrument.display_name,
                    instrument.display_name_ko,
                    *instrument.aliases,
                )
            )
        )
    )
    distinguishing_tool_tokens = {
        token
        for token in text_tokens & instrument_tokens
        if token not in procedure_like
        and not any(
            _tokens_share_procedure_root(token, procedure_token)
            for procedure_token in procedure_tokens
        )
    }
    return not distinguishing_tool_tokens


def _has_procedure_reference(spec: ProcedureSpec, raw_text: str) -> bool:
    tokens = set(_lexical_tokens(raw_text))
    if tokens & _PROCEDURE_CONTEXT_TOKENS:
        return True
    compact_text = _compact_lexical_text(raw_text)
    for name in (
        spec.bundle.procedure_id.replace("_", " "),
        spec.bundle.procedure_display_name,
        spec.bundle.procedure_display_name_ko,
    ):
        without_parenthetical = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", str(name))
        compact_name = _compact_lexical_text(without_parenthetical)
        if len(compact_name) >= 4 and compact_name in compact_text:
            return True
    return bool(tokens & _procedure_name_tokens(spec))


def _candidate_modifies_procedure(candidate: str, raw_text: str) -> bool:
    candidate_tokens = _lexical_tokens(candidate)
    text_tokens = _lexical_tokens(raw_text)
    if not candidate_tokens or not text_tokens:
        return False
    width = len(candidate_tokens)
    for index in range(0, len(text_tokens) - width + 1):
        if text_tokens[index : index + width] != candidate_tokens:
            continue
        adjacent = set()
        if index > 0:
            adjacent.add(text_tokens[index - 1])
        if index + width < len(text_tokens):
            adjacent.add(text_tokens[index + width])
        if adjacent & _PROCEDURE_CONTEXT_TOKENS:
            return True
    return False


def _resolve_request_tool(
    spec: ProcedureSpec,
    raw_text: str,
    *,
    reject_procedure_modifiers: bool = False,
) -> str:
    for candidate in _normalize_request_text(raw_text):
        resolved = spec.resolve_instrument_alias(candidate) or ""
        if not resolved:
            continue
        if reject_procedure_modifiers and _candidate_modifies_procedure(
            candidate,
            raw_text,
        ):
            continue
        return resolved
    return ""


def _requests_additional_instance(raw_text: str) -> bool:
    lowered = str(raw_text or "").lower().replace("\xa0", " ")
    return bool(
        re.search(
            r"\b(?:one\s+more|another|an\s+additional|a\s+second)\b|"
            r"(?:하나|하나만|한\s*개|한개)\s*더",
            lowered,
        )
    )


def _instrument_mention_count(
    spec: ProcedureSpec,
    instrument_id: str,
    raw_text: str,
) -> int:
    text_tokens = re.findall(r"[0-9a-z_가-힣]+", str(raw_text or "").lower())
    if not text_tokens:
        return 0
    instrument = next(
        (
            item
            for item in spec.bundle.instruments
            if item.id == instrument_id
        ),
        None,
    )
    if instrument is None:
        return 0
    alias_tokens = {
        tuple(re.findall(r"[0-9a-z_가-힣]+", str(alias).lower()))
        for alias in {
            instrument.id,
            instrument.display_name,
            instrument.display_name_ko,
            *instrument.aliases,
        }
    }
    aliases = sorted(
        (tokens for tokens in alias_tokens if tokens),
        key=lambda tokens: (-len(tokens), tokens),
    )
    count = 0
    index = 0
    while index < len(text_tokens):
        matched = next(
            (
                tokens
                for tokens in aliases
                if tuple(text_tokens[index : index + len(tokens)]) == tokens
            ),
            None,
        )
        if matched is None:
            index += 1
            continue
        count += 1
        index += len(matched)
    return count


def _owner_for_lifecycle(state: InstrumentBelief) -> str:
    if state.lifecycle_stage == LIFECYCLE_PREPOSITIONED_RIGHT:
        return "robot_right_hand"
    if state.lifecycle_stage in LEFT_HAND_LIFECYCLES:
        return "robot_left_hand"
    if state.lifecycle_stage == LIFECYCLE_CLEANED_LEFT:
        return "none"
    if state.lifecycle_stage == LIFECYCLE_SURGEON_OWNED:
        return "surgeon"
    return "none"


def _status_for_lifecycle(state: InstrumentBelief) -> str:
    if state.lifecycle_stage in {LIFECYCLE_HOME_RACK, LIFECYCLE_RETURNED_HOME}:
        return "available"
    if state.lifecycle_stage == LIFECYCLE_PREPOSITIONED_RIGHT:
        return "prepared"
    if state.lifecycle_stage == LIFECYCLE_SURGEON_OWNED:
        if state.location_type in {"surgical_field", "bed_fixed_tool"}:
            return "in_use"
        if state.location_type == "return_zone":
            return "presented_for_return"
        return "handed_over"
    if state.lifecycle_stage == LIFECYCLE_MAYO_REUSE:
        return "parked_for_reuse"
    if state.lifecycle_stage == LIFECYCLE_MAYO_RECOVERY:
        return "awaiting_retrieval"
    if state.lifecycle_stage == LIFECYCLE_DROPPED_FLOOR:
        return "requires_human_recovery"
    if state.lifecycle_stage == LIFECYCLE_RECOVERING_LEFT:
        return "received_return"
    if state.lifecycle_stage == LIFECYCLE_CLEANING_LEFT:
        return "cleaning"
    if state.lifecycle_stage == LIFECYCLE_CLEANED_LEFT:
        return "ready_to_return"
    return "available"


def _location_for_lifecycle(state: InstrumentBelief, lifecycle_stage: str) -> tuple[str, str]:
    if lifecycle_stage == LIFECYCLE_PREPOSITIONED_RIGHT:
        return ("robot_right_hand", "robot_right_hand")
    if lifecycle_stage == LIFECYCLE_SURGEON_OWNED:
        if state.location_type in SURGEON_OWNED_LOCATION_TYPES:
            return (state.location_type, state.location_id or "surgeon_hand")
        return ("surgeon_hand", "surgeon_hand")
    if lifecycle_stage == LIFECYCLE_MAYO_REUSE:
        return ("mayo_reuse_zone", "mayo_reuse_zone")
    if lifecycle_stage == LIFECYCLE_MAYO_RECOVERY:
        return ("mayo_recovery_zone", "mayo_recovery_zone")
    if lifecycle_stage == LIFECYCLE_DROPPED_FLOOR:
        return ("floor_zone", "floor_zone")
    if lifecycle_stage == LIFECYCLE_RECOVERING_LEFT:
        return ("robot_left_hand", "robot_left_hand")
    if lifecycle_stage == LIFECYCLE_CLEANING_LEFT:
        return ("cleaner_slot", "cleaner_slot")
    if lifecycle_stage == LIFECYCLE_CLEANED_LEFT:
        return ("cleaner_slot", "cleaner_slot")
    return (state.home_location_type, state.home_location_id)


def _observed_lifecycle_for_location(state: InstrumentBelief, location_type: str, location_id: str) -> str:
    if location_type == "robot_right_hand":
        return LIFECYCLE_PREPOSITIONED_RIGHT
    if location_type in SURGEON_OWNED_LOCATION_TYPES:
        return LIFECYCLE_SURGEON_OWNED
    if location_type in {"mayo_stand", "mayo_reuse_zone"}:
        return LIFECYCLE_MAYO_REUSE
    if location_type == "mayo_recovery_zone":
        return LIFECYCLE_MAYO_RECOVERY
    if location_type == "floor_zone":
        return LIFECYCLE_DROPPED_FLOOR
    if location_type == "robot_left_hand":
        return LIFECYCLE_RECOVERING_LEFT
    if location_type == "cleaner_slot":
        return LIFECYCLE_CLEANED_LEFT if state.cleanliness_state == "ready" and not state.contaminated else LIFECYCLE_CLEANING_LEFT
    if location_type == state.home_location_type and location_id == state.home_location_id:
        if state.ever_surgeon_owned or state.lifecycle_stage not in {LIFECYCLE_HOME_RACK, LIFECYCLE_RETURNED_HOME}:
            return LIFECYCLE_RETURNED_HOME
        return LIFECYCLE_HOME_RACK
    return state.lifecycle_stage or LIFECYCLE_HOME_RACK


def _is_available_for_handover(state: InstrumentBelief) -> bool:
    if state.lifecycle_stage in {LIFECYCLE_MAYO_REUSE, LIFECYCLE_MAYO_RECOVERY}:
        return True
    return (
        state.lifecycle_stage in {LIFECYCLE_HOME_RACK, LIFECYCLE_RETURNED_HOME, LIFECYCLE_PREPOSITIONED_RIGHT}
        and not state.contaminated
    )


class ORDigitalTwin:
    """Event-driven runtime belief manager."""

    def __init__(
        self,
        spec: ProcedureSpec,
        *,
        allow_shadow_request_capacity_reconciliation: bool = False,
        allow_shadow_type_instance_requests: bool = False,
        allow_open_set_phase_bootstrap: bool = False,
        phase_transition_required_counts: dict[
            tuple[str, str], dict[str, int]
        ] | None = None,
    ):
        self.spec = spec
        self._allow_shadow_request_capacity_reconciliation = bool(
            allow_shadow_request_capacity_reconciliation
        )
        self._allow_shadow_type_instance_requests = bool(
            allow_shadow_type_instance_requests
        )
        self._allow_open_set_phase_bootstrap = bool(
            allow_open_set_phase_bootstrap
        )
        self._configured_phase_transition_required_counts = {
            (str(source), str(target)): {
                str(tool_id): max(1, int(count))
                for tool_id, count in requirements.items()
            }
            for (source, target), requirements in (
                phase_transition_required_counts or {}
            ).items()
        }
        self._request_generation_counter = 0
        self._shadow_assumption_audit: deque[dict[str, Any]] = deque()
        self.event_history: deque[dict[str, Any]] = deque(maxlen=200)
        self._bed_robot_arm_group_status_signatures: dict[str, tuple[Any, ...]] = {}
        self._bed_robot_arm_group_ignored_status_signatures: dict[
            tuple[str, str], tuple[Any, ...]
        ] = {}
        self.instrument_states: dict[str, InstrumentBelief] = {}
        self._observation_candidates: dict[str, dict[str, Any]] = {}
        self._shadow_counterfactual_locked_instances: set[str] = set()
        self._observation_violation_cooldowns: dict[tuple[str, str, str, str], float] = {}
        self._phase_evidence_history: deque[dict[str, Any]] = deque(
            maxlen=max(
                8,
                int(self.spec.bundle.phase_guard.smoothing_window) * 2,
            )
        )
        self._pending_phase_cues: dict[str, dict[str, Any]] = {}
        self._phase_decision_cooldowns: dict[tuple[str, str, str], float] = {}
        self._last_normal_phase_before_interrupt = ""
        self._active_interrupt_event_phase = ""
        self._active_interrupt_event_seen_sec = 0.0
        self._phase_bootstrap_open = self._allow_open_set_phase_bootstrap
        self._phase_entered_sec = self._monotonic_sec()
        self._initial_phase_floor_index = 0
        self._phase_instance_interactions: dict[
            str, dict[str, set[str]]
        ] = defaultdict(lambda: defaultdict(set))
        self._inventory_violation_signature: tuple[Any, ...] | None = None
        self.reset_spec(spec)

    def reset_spec(self, spec: ProcedureSpec | None = None, *, seed_from_perception: bool = False) -> None:
        if spec is not None:
            self.spec = spec
        self.state = TwinState(
            procedure_id=self.spec.procedure_id,
            filtered_phase=self.spec.default_phase_id,
            phase_confidence=0.0,
            phase_uncertain=True,
            phase_stability=0.0,
        )
        group_spec = self.spec.get_bed_robot_arm_group_spec()
        profile_defaults = {
            group.id: group.initial_end_effector_profile
            for group in (group_spec.groups if group_spec is not None else [])
        }
        self.state.bed_robot_arm_groups = {
            group_id: BedRobotArmGroupBelief(
                group_id=group_id,
                connected=True,
                state="standby",
                end_effector_profile=str(profile_defaults.get(group_id, "")),
            )
            for group_id in BED_ROBOT_ARM_GROUP_IDS
        }
        self.instrument_states = {}
        self.event_history.clear()
        self._bed_robot_arm_group_status_signatures.clear()
        self._bed_robot_arm_group_ignored_status_signatures.clear()
        self._observation_candidates.clear()
        self._shadow_counterfactual_locked_instances.clear()
        self._observation_violation_cooldowns.clear()
        phase_history_capacity = max(
            8,
            int(self.spec.bundle.phase_guard.smoothing_window) * 2,
        )
        if self._phase_evidence_history.maxlen != phase_history_capacity:
            self._phase_evidence_history = deque(
                maxlen=phase_history_capacity
            )
        else:
            self._phase_evidence_history.clear()
        self._pending_phase_cues.clear()
        self._phase_decision_cooldowns.clear()
        self._shadow_assumption_audit.clear()
        self._last_normal_phase_before_interrupt = ""
        self._clear_active_interrupt_context()
        self._phase_bootstrap_open = self._allow_open_set_phase_bootstrap
        self._phase_entered_sec = self._monotonic_sec()
        self._initial_phase_floor_index = self._normal_phase_index(
            self.spec.default_phase_id
        )
        self._phase_instance_interactions.clear()
        self._inventory_violation_signature = None
        for instrument_id in self.spec.list_instrument_ids():
            location_id = self.spec.get_initial_location(instrument_id) or "unknown"
            location_type = self.spec.get_initial_location_type(instrument_id) or "unknown"
            inventory_count = max(
                1, int(self.spec.get_inventory_count(instrument_id))
            )
            for instance_index in range(1, inventory_count + 1):
                instance_id = f"{instrument_id}#{instance_index}"
                self.instrument_states[instance_id] = InstrumentBelief(
                    instrument_id=instrument_id,
                    instance_id=instance_id,
                    home_location_type=location_type,
                    home_location_id=location_id,
                    location_type=location_type,
                    location_id=location_id,
                    owner="none",
                    status="available",
                    confidence=0.9,
                    lifecycle_stage=LIFECYCLE_HOME_RACK,
                    visual_anchor_id=location_id,
                )
        for initial_state in self.spec.get_initial_instrument_states():
            state = self.instrument_states.get(initial_state.instance_id)
            if state is None:
                continue
            self._set_lifecycle(
                state,
                initial_state.lifecycle_stage,
                location_type=self.spec.get_location_type(
                    initial_state.location_id
                ),
                location_id=initial_state.location_id,
                confidence=initial_state.confidence,
                last_update_sec=self._monotonic_sec(),
            )
            self._update_visual_anchor(state)
        if seed_from_perception:
            self._seed_from_initial_perception()
        self._recompute_transient_state()

    def _normal_phase_index(self, phase_id: str) -> int:
        try:
            return self.spec.normal_phase_ids.index(phase_id)
        except ValueError:
            return -1

    def remaining_procedure_use_instruments(self) -> set[str]:
        """Tools still mentioned by current/future authored sequence or roles."""

        if self.state.execution_state in {"finishing", "completed"}:
            return set()
        phase_id = self.state.filtered_phase or self.spec.default_phase_id
        if self.spec.is_interrupt_phase(phase_id):
            phase_id = (
                self._last_normal_phase_before_interrupt
                or self.spec.default_phase_id
            )
        return set(
            self.spec.get_remaining_expected_instruments(
                phase_id,
                include_current=True,
            )
        )

    def procedure_future_use_expected(
        self,
        instrument_or_instance_id: str,
    ) -> bool:
        state = self._state_by_instance(instrument_or_instance_id)
        instrument_id = (
            state.instrument_id
            if state is not None
            else self.spec.resolve_instrument_alias(
                str(instrument_or_instance_id or "")
            )
            or str(instrument_or_instance_id or "")
        )
        return bool(
            instrument_id
            and instrument_id in self.remaining_procedure_use_instruments()
        )

    def _instances_for_type(self, instrument_id: str) -> list[InstrumentBelief]:
        return sorted(
            (
                state
                for state in self.instrument_states.values()
                if state.instrument_id == instrument_id
            ),
            key=lambda state: state.instance_id,
        )

    def _state_by_instance(self, instance_id: str) -> InstrumentBelief | None:
        return self.instrument_states.get(str(instance_id or ""))

    def _select_instance(
        self,
        instrument_id: str,
        *,
        preferred_instance_id: str = "",
        allowed_lifecycles: set[str] | None = None,
        exclude_reserved: bool = False,
    ) -> InstrumentBelief | None:
        if preferred_instance_id:
            state = self._state_by_instance(preferred_instance_id)
            if state is not None and state.instrument_id == instrument_id:
                if allowed_lifecycles is None or state.lifecycle_stage in allowed_lifecycles:
                    return state
        candidates = self._instances_for_type(instrument_id)
        if allowed_lifecycles is not None:
            candidates = [
                state
                for state in candidates
                if state.lifecycle_stage in allowed_lifecycles
            ]
        if exclude_reserved:
            reserved_instances = {
                cue.instance_id
                for cue in self.state.surgeon_request_queue
                if cue.instance_id
            }
            candidates = [
                state
                for state in candidates
                if state.instance_id not in reserved_instances
            ]
        return min(
            candidates,
            key=lambda state: (
                state.lifecycle_stage
                not in {LIFECYCLE_HOME_RACK, LIFECYCLE_RETURNED_HOME},
                float(state.last_update_sec or 0.0),
                state.instance_id,
            ),
            default=None,
        )

    def get_instrument_state(
        self,
        instrument_or_instance_id: str,
        *,
        allowed_lifecycles: set[str] | None = None,
    ) -> InstrumentBelief | None:
        direct = self._state_by_instance(instrument_or_instance_id)
        if direct is not None:
            if allowed_lifecycles is None or direct.lifecycle_stage in allowed_lifecycles:
                return direct
            return None
        resolved = (
            self.spec.resolve_instrument_alias(instrument_or_instance_id)
            or instrument_or_instance_id
        )
        return self._select_instance(
            resolved,
            allowed_lifecycles=allowed_lifecycles,
        )

    def update_bed_robot_arm_group_request(self, request: BedRobotArmGroupRequest) -> None:
        """Record an accepted public request without inventing physical arm state."""

        group_id = str(request.group_id or "").strip().lower()
        belief = self.state.bed_robot_arm_groups.get(group_id)
        if belief is None:
            self._record_event(
                "BedRobotArmGroupRequestRejected",
                {
                    "request_id": request.request_id,
                    "group_id": group_id,
                    "operation": request.operation,
                    "reason": "unknown_group",
                },
            )
            return
        # Public requests are observations, not yet approved controller work.
        # Active IDs and operation are committed by the BT command/status path
        # so a rejected duplicate cannot overwrite an in-flight group action.
        self._record_event(
            "BedRobotArmGroupRequestObserved",
            {
                "request_id": request.request_id,
                "group_id": group_id,
                "operation": request.operation,
                "voice_text": request.voice_text,
                "source": request.source,
                "end_effector_profile": request.end_effector_profile,
            },
        )

    @staticmethod
    def _bed_robot_arm_group_status_signature(
        status: BedRobotArmGroupStatus,
    ) -> tuple[Any, ...]:
        progress = max(0.0, min(1.0, float(status.progress)))
        progress_milestone = min(4, int(progress * 4))
        return (
            str(status.request_id),
            str(status.command_id),
            str(status.group_id).strip().lower(),
            str(status.operation),
            str(status.state),
            str(status.outcome),
            bool(status.terminal),
            bool(status.success),
            str(status.direction),
            round(float(status.distance_mm), 6),
            str(status.distance_origin),
            str(status.raw_distance_text),
            str(status.end_effector_profile),
            progress_milestone,
            str(status.error_code),
            str(status.rejection_reason),
        )

    def update_bed_robot_arm_group_status(
        self, status: BedRobotArmGroupStatus
    ) -> bool | None:
        """Reduce group feedback and report whether semantic state changed."""

        group_id = str(status.group_id or "").strip().lower()
        belief = self.state.bed_robot_arm_groups.get(group_id)
        if belief is None:
            return None
        is_health = not str(status.operation or "").strip() and str(
            status.request_id or ""
        ).startswith("health-")
        status_signature = self._bed_robot_arm_group_status_signature(status)
        signature: tuple[Any, ...] | None = None
        if not is_health:
            signature = status_signature
            if self._bed_robot_arm_group_status_signatures.get(group_id) == signature:
                return False
        status_ns = int(status.stamp.sec) * 1_000_000_000 + int(
            status.stamp.nanosec
        )
        current_ns = int(belief.last_update_stamp_sec) * 1_000_000_000 + int(
            belief.last_update_stamp_nanosec
        )
        if current_ns and status_ns < current_ns:
            ignored_key = (group_id, "status_older_than_current_group_state")
            if (
                self._bed_robot_arm_group_ignored_status_signatures.get(
                    ignored_key
                )
                == status_signature
            ):
                return False
            self._bed_robot_arm_group_ignored_status_signatures[
                ignored_key
            ] = status_signature
            self._record_event(
                "BedRobotArmGroupStatusIgnored",
                {
                    "request_id": status.request_id,
                    "command_id": status.command_id,
                    "group_id": group_id,
                    "operation": status.operation,
                    "state": status.state,
                    "outcome": status.outcome,
                    "reason": "status_older_than_current_group_state",
                },
            )
            return None
        mismatched_active_request = bool(
            belief.active_request_id
            and status.request_id
            and status.request_id != belief.active_request_id
        )
        if mismatched_active_request:
            ignored_key = (
                group_id,
                "status_request_does_not_match_active_group_request",
            )
            if (
                self._bed_robot_arm_group_ignored_status_signatures.get(
                    ignored_key
                )
                == status_signature
            ):
                return False
            self._bed_robot_arm_group_ignored_status_signatures[
                ignored_key
            ] = status_signature
            self._record_event(
                "BedRobotArmGroupCommandRejected" if status.terminal else "BedRobotArmGroupStatusIgnored",
                {
                    "request_id": status.request_id,
                    "command_id": status.command_id,
                    "group_id": group_id,
                    "operation": status.operation,
                    "state": status.state,
                    "outcome": status.outcome,
                    "success": bool(status.success),
                    "terminal": bool(status.terminal),
                    "error_code": status.error_code,
                    "rejection_reason": status.rejection_reason,
                    "reason": "status_request_does_not_match_active_group_request",
                    "active_request_id": belief.active_request_id,
                },
            )
            return None
        for ignored_key in [
            key
            for key in self._bed_robot_arm_group_ignored_status_signatures
            if key[0] == group_id
        ]:
            self._bed_robot_arm_group_ignored_status_signatures.pop(
                ignored_key, None
            )
        if signature is not None:
            self._bed_robot_arm_group_status_signatures[group_id] = signature
        if is_health:
            available = (
                str(status.error_code) != "server_unavailable"
                and bool(status.success)
            )
            next_state = (
                "offline"
                if not available
                else (
                    str(status.state or "standby")
                    if belief.state == "offline"
                    else belief.state
                )
            )
            changed = bool(
                belief.connected != available
                or belief.state != next_state
            )
            belief.connected = available
            belief.state = next_state
            # Health heartbeats report transport availability only.  Do not
            # erase (or replace with ``server_unavailable``) the last
            # operational error returned by the group controller: that
            # rejection must remain inspectable in the twin and UI until a
            # later operation supplies a new result.
            if changed:
                belief.last_update_stamp_sec = int(status.stamp.sec)
                belief.last_update_stamp_nanosec = int(status.stamp.nanosec)
            if changed:
                self._record_event(
                    "BedRobotArmGroupAvailabilityChanged",
                    {
                        "request_id": status.request_id,
                        "group_id": group_id,
                        "state": next_state,
                        "outcome": status.outcome,
                        "available": available,
                        "changed": True,
                    },
                )
            return changed
        belief.last_update_stamp_sec = int(status.stamp.sec)
        belief.last_update_stamp_nanosec = int(status.stamp.nanosec)
        next_state = str(status.state or belief.state or "standby")
        belief.connected = (
            str(status.error_code) != "server_unavailable"
            and next_state not in {"offline", "fault"}
        )
        belief.state = next_state
        belief.operation = str(status.operation)
        belief.direction = str(status.direction)
        belief.distance_mm = float(status.distance_mm)
        belief.distance_origin = str(status.distance_origin)
        belief.raw_distance_text = str(status.raw_distance_text)
        belief.active_request_id = str(status.request_id or belief.active_request_id)
        belief.active_command_id = str(status.command_id or belief.active_command_id)
        belief.progress = max(0.0, min(1.0, float(status.progress)))
        belief.error_code = str(status.error_code)
        control_cancelled = str(status.outcome) == "cancelled_by_runtime_control"
        belief.error_message = (
            str(status.message)
            if bool(status.terminal) and not bool(status.success) and not control_cancelled
            else ""
        )
        belief.rejection_reason = (
            "" if control_cancelled else str(status.rejection_reason)
        )
        if status.end_effector_profile and bool(status.terminal and status.success):
            belief.end_effector_profile = str(status.end_effector_profile)
        if bool(status.terminal):
            belief.active_request_id = ""
            belief.active_command_id = ""
        if control_cancelled:
            event_type = "BedRobotArmGroupCommandCancelled"
        elif not status.operation and status.request_id.startswith("health-"):
            event_type = "BedRobotArmGroupAvailabilityChanged"
        else:
            event_type = "BedRobotArmGroupCommandCompleted" if bool(status.terminal and status.success) else (
                "BedRobotArmGroupCommandRejected" if bool(status.terminal) else "BedRobotArmGroupStatusUpdated"
            )
        self._record_event(
            event_type,
            {
                "request_id": status.request_id,
                "command_id": status.command_id,
                "group_id": group_id,
                "operation": status.operation,
                "state": status.state,
                "outcome": status.outcome,
                "success": bool(status.success),
                "terminal": bool(status.terminal),
                "direction": status.direction,
                "distance_mm": float(status.distance_mm),
                "distance_origin": status.distance_origin,
                "end_effector_profile": status.end_effector_profile,
                "error_code": status.error_code,
                "rejection_reason": status.rejection_reason,
            },
        )
        return True

    def bed_robot_arm_group_payload(self) -> list[dict[str, Any]]:
        return [
            asdict(self.state.bed_robot_arm_groups[group_id])
            for group_id in BED_ROBOT_ARM_GROUP_IDS
            if group_id in self.state.bed_robot_arm_groups
        ]

    def reset_runtime(self) -> None:
        self.reset_spec(self.spec, seed_from_perception=False)

    def clear_perception_evidence(self) -> None:
        """Drop uncommitted visual evidence while retaining accepted history."""
        self._observation_candidates.clear()
        self._phase_evidence_history.clear()
        self.state.phase_confidence = 0.0
        self.state.phase_uncertain = True
        self.state.phase_stability = 0.0

    def clear_object_detection_evidence(self) -> None:
        """Drop advisory detector candidates without invalidating VLM evidence."""
        self._observation_candidates.clear()

    def set_initial_phase(self, phase_id: str) -> str:
        requested_phase = str(phase_id or "").strip()
        resolved_phase = requested_phase if requested_phase in self.spec.phase_ids else self.spec.default_phase_id
        if requested_phase and requested_phase != resolved_phase:
            self._record_invariant_violation(
                reason="start_phase_out_of_bundle_scope",
                event_type="InitialPhaseSelected",
                instrument_id="",
                proposed_stage=requested_phase,
            )
        self.state.filtered_phase = resolved_phase
        self.state.phase_confidence = 1.0
        self.state.phase_uncertain = False
        self.state.phase_stability = 1.0
        self._relocate_phase_field_deployed_tools(
            resolved_phase,
            reason="initial_phase_selected",
        )
        self._phase_entered_sec = self._monotonic_sec()
        self._phase_evidence_history.clear()
        self._pending_phase_cues.clear()
        self._phase_decision_cooldowns.clear()
        self._last_normal_phase_before_interrupt = ""
        self._clear_active_interrupt_context()
        self._phase_bootstrap_open = False
        self._initial_phase_floor_index = self._normal_phase_index(
            resolved_phase
        )
        self._recompute_transient_state()
        self._record_event(
            "InitialPhaseSelected",
            {
                "phase_id": resolved_phase,
                "requested_phase_id": requested_phase,
            },
        )
        return resolved_phase

    @property
    def phase_bootstrap_open(self) -> bool:
        return bool(self._phase_bootstrap_open)

    def set_execution_state(self, running: bool, execution_state: str) -> None:
        self.state.running = running
        self.state.execution_state = execution_state
        if not running and execution_state in {"idle", "halted", "completed"}:
            self._clear_active_robot_task()
            self.state.robot_state = "idle"
            self.state.cleaner_busy = False
            self.state.cleaner_remaining_sec = 0.0

    def normalize_for_publish(self) -> None:
        """Close transient invariants before exposing the authoritative state.

        Skill, surgeon-actor, and observation callbacks already recompute after
        each mutation. This extra public boundary makes the reducer contract
        explicit: no externally published snapshot should contain a temporary
        hand/cleaner occupancy conflict even if multiple inputs arrive in the
        same ROS tick.
        """
        self._sync_active_request_from_queue()
        self._recompute_transient_state()

    def _monotonic_sec(self) -> float:
        return time.monotonic()

    def _field_anchor_id(self) -> str:
        for anchor in self.spec.get_simulation_anchors():
            if anchor.id.startswith("field_region"):
                return anchor.id
        return "field_region"

    def _is_field_deployed_for_phase(
        self,
        instrument_or_instance_id: str,
        phase_id: str | None = None,
    ) -> bool:
        resolved_phase = str(
            phase_id or self.state.filtered_phase or self.spec.default_phase_id
        )
        if resolved_phase not in self.spec.phase_ids:
            return False
        candidate_phases = [resolved_phase]
        next_phase = self.spec.get_next_normal_phase(resolved_phase)
        if next_phase:
            candidate_phases.append(next_phase)
        return any(
            self.spec.is_field_deployed_instrument(
                candidate_phase,
                instrument_or_instance_id,
            )
            for candidate_phase in candidate_phases
        )

    def _phase_field_deployment_ready(self, phase_id: str) -> bool:
        required_types = set(
            self.spec.get_field_deployed_instruments(phase_id)
        )
        if not required_types:
            return True
        return any(
            state.instrument_id in required_types
            and state.lifecycle_stage == LIFECYCLE_SURGEON_OWNED
            and state.location_type in {"surgical_field", "bed_fixed_tool"}
            for state in self.instrument_states.values()
        )

    def _relocate_phase_field_deployed_tools(
        self,
        phase_id: str,
        *,
        reason: str,
    ) -> list[str]:
        if phase_id not in self.spec.phase_ids:
            return []
        field_tool_ids = set(
            self.spec.get_field_deployed_instruments(phase_id)
        )
        if not field_tool_ids:
            return []

        field_anchor_id = self._field_anchor_id()
        relocated: list[str] = []
        for state in self._surgeon_owned_hand_states():
            if state.instrument_id not in field_tool_ids:
                continue
            previous_location_type = state.location_type
            previous_location_id = state.location_id
            state.location_type = "surgical_field"
            state.location_id = field_anchor_id
            state.owner = _owner_for_lifecycle(state)
            state.status = _status_for_lifecycle(state)
            state.reserved_for = ""
            state.mayo_placement_evidence = ""
            self._clear_observation_candidate(state.instance_id)
            self._update_visual_anchor(state)
            relocated.append(state.instance_id)
            self._record_event(
                "ToolFieldDeploymentInferred",
                {
                    "phase_id": phase_id,
                    "instrument_id": state.instrument_id,
                    "instance_id": state.instance_id,
                    "previous_location_type": previous_location_type,
                    "previous_location_id": previous_location_id,
                    "location_type": state.location_type,
                    "location_id": state.location_id,
                    "reason": reason,
                },
            )
        return relocated

    def _resolve_visual_anchor(
        self,
        *,
        lifecycle_stage: str,
        location_type: str,
        location_id: str,
        home_location_id: str,
    ) -> str:
        if location_id and any(
            location_id == anchor.id for anchor in self.spec.get_simulation_anchors()
        ):
            return location_id
        if lifecycle_stage in {LIFECYCLE_HOME_RACK, LIFECYCLE_RETURNED_HOME}:
            return home_location_id
        if lifecycle_stage == LIFECYCLE_PREPOSITIONED_RIGHT or location_type == "robot_right_hand":
            return "robot_right_hand"
        if lifecycle_stage == LIFECYCLE_RECOVERING_LEFT or location_type == "robot_left_hand":
            return "robot_left_hand"
        if lifecycle_stage in {LIFECYCLE_CLEANING_LEFT, LIFECYCLE_CLEANED_LEFT} or location_type == "cleaner_slot":
            return "cleaner_slot"
        if lifecycle_stage == LIFECYCLE_DROPPED_FLOOR or location_type == "floor_zone":
            return "floor_zone"
        if lifecycle_stage == LIFECYCLE_MAYO_REUSE or location_type == "mayo_reuse_zone":
            return "mayo_reuse_zone"
        if lifecycle_stage == LIFECYCLE_MAYO_RECOVERY or location_type == "mayo_recovery_zone":
            return "mayo_recovery_zone"
        if location_type == "return_zone":
            return "surgeon_return_zone"
        if location_type == "handover_zone":
            return "surgeon_receive_zone"
        if location_type == "surgeon_hand":
            return "surgeon_right_hand"
        if location_type in {"surgical_field", "bed_fixed_tool"}:
            return location_id or self._field_anchor_id()
        return location_id or home_location_id

    def _update_visual_anchor(self, state: InstrumentBelief) -> None:
        state.visual_anchor_id = self._resolve_visual_anchor(
            lifecycle_stage=state.lifecycle_stage,
            location_type=state.location_type,
            location_id=state.location_id,
            home_location_id=state.home_location_id,
        )

    def _clear_active_robot_task(self) -> None:
        self.state.active_robot_task = None

    def _clear_surgeon_request_state(self) -> None:
        self.state.surgeon_request_queue.clear()
        self.state.explicit_request_tool = ""
        self.state.surgeon_request_tool = ""
        self.state.surgeon_request_instance_id = ""
        self.state.surgeon_request_generation = 0
        self.state.surgeon_request_additional_instance_assumed = False
        self.state.surgeon_ready_for_handover = False
        self.state.surgeon_ready_for_retrieval = False

    def _is_priority_interrupt_phase(self, phase_id: str) -> bool:
        return bool(phase_id and self.spec.is_interrupt_phase(phase_id))

    def _interrupt_priority_tool(self, phase_id: str) -> str:
        for candidate in self.spec.get_expected_instruments(phase_id):
            resolved = self.spec.resolve_instrument_alias(candidate) or candidate
            if self._instances_for_type(resolved):
                return resolved
        return ""

    def _apply_emergency_interrupt_preemption(
        self,
        *,
        target_phase: str,
        reason: str,
        cue_id: str = "",
        priority_tool: str = "",
    ) -> None:
        if not self._is_priority_interrupt_phase(target_phase):
            return

        previous_task = asdict(self.state.active_robot_task) if self.state.active_robot_task else {}
        previous_queue = [cue.instrument_id for cue in self.state.surgeon_request_queue]
        previous_request_tool = self.state.surgeon_request_tool
        previous_pending = list(self.state.pending_transition_tools)

        if self.state.active_robot_task is not None:
            self._record_event(
                "RobotTaskPreemptedByEmergency",
                {
                    "target_phase": target_phase,
                    "reason": reason,
                    "cue_id": cue_id,
                    "previous_task": previous_task,
                },
            )
            self._clear_active_robot_task()

        self.state.pending_transition_tools = []
        self._clear_surgeon_request_state()

        if priority_tool:
            resolved_priority_tool = (
                self.spec.resolve_instrument_alias(priority_tool) or priority_tool
            )
        else:
            resolved_priority_tool = self._interrupt_priority_tool(target_phase)
        if self._instances_for_type(resolved_priority_tool):
            display_name = next(
                (
                    instrument.display_name
                    for instrument in self.spec.bundle.instruments
                    if instrument.id == resolved_priority_tool
                ),
                resolved_priority_tool,
            )
            self._enqueue_surgeon_request(
                event_type="voice_request",
                instrument_id=resolved_priority_tool,
                voice_text=f"Interrupt priority: {display_name} please",
                note=f"emergency_preemption:{target_phase}:{reason}",
                ready_for_handover=True,
                ready_for_retrieval=False,
                override=True,
            )
            self.state.surgeon_intent = "voice_request"
            self.state.surgeon_ready_for_handover = True

        self._record_event(
            "EmergencyInterruptPreemptionApplied",
            {
                "target_phase": target_phase,
                "reason": reason,
                "cue_id": cue_id,
                "priority_tool": resolved_priority_tool,
                "previous_request_tool": previous_request_tool,
                "previous_queue": previous_queue,
                "previous_pending_transition_tools": previous_pending,
            },
        )


    def _active_requested_tool_id(self) -> str:
        return self.state.surgeon_request_tool or self.state.explicit_request_tool or ""

    def _active_requested_instance_id(self) -> str:
        return self.state.surgeon_request_instance_id or ""

    def _is_active_requested_tool(self, instrument_id: str) -> bool:
        if not instrument_id:
            return False
        if instrument_id == self._active_requested_instance_id():
            return True
        state = self._state_by_instance(instrument_id)
        tool_type = state.instrument_id if state is not None else instrument_id
        return tool_type == self._active_requested_tool_id()

    def _surgeon_owned_hand_states(self) -> list[InstrumentBelief]:
        return [
            state
            for state in self.instrument_states.values()
            if state.lifecycle_stage == LIFECYCLE_SURGEON_OWNED
            and (state.location_type == "surgeon_hand" or state.status == "handed_over")
        ]

    def _active_request_cue(self) -> SurgeonRequestCue | None:
        return self.state.surgeon_request_queue[0] if self.state.surgeon_request_queue else None

    def _sync_active_request_from_queue(self) -> None:
        cue = self._active_request_cue()
        if cue is None:
            if self.state.surgeon_intent in ACTIVE_REQUEST_INTENTS or (
                self.state.surgeon_intent in ACTIVE_RETURN_INTENTS
                and not self.state.active_recovery_tools
            ):
                self.state.surgeon_intent = "idle"
            self.state.explicit_request_tool = ""
            self.state.surgeon_request_tool = ""
            self.state.surgeon_request_instance_id = ""
            self.state.surgeon_request_generation = 0
            self.state.surgeon_request_additional_instance_assumed = False
            self.state.surgeon_ready_for_handover = False
            self.state.surgeon_ready_for_retrieval = False
            return

        self.state.surgeon_intent = cue.event_type
        self.state.surgeon_request_tool = cue.instrument_id
        self.state.surgeon_request_instance_id = cue.instance_id
        self.state.surgeon_request_generation = int(cue.generation)
        self.state.surgeon_request_additional_instance_assumed = bool(
            cue.shadow_additional_instance_assumed
        )
        self.state.surgeon_ready_for_handover = bool(cue.ready_for_handover)
        self.state.surgeon_ready_for_retrieval = bool(cue.ready_for_retrieval)
        if cue.event_type in ACTIVE_REQUEST_INTENTS:
            self.state.explicit_request_tool = cue.instrument_id

    def _resolve_request_instance(
        self,
        *,
        instrument_id: str,
        event_type: str,
        additional_instance_requested: bool,
    ) -> InstrumentBelief | None:
        if event_type in ACTIVE_RETURN_INTENTS:
            return self._select_instance(
                instrument_id,
                allowed_lifecycles={
                    LIFECYCLE_SURGEON_OWNED,
                    LIFECYCLE_MAYO_REUSE,
                    LIFECYCLE_MAYO_RECOVERY,
                    LIFECYCLE_RECOVERING_LEFT,
                    LIFECYCLE_CLEANING_LEFT,
                    LIFECYCLE_CLEANED_LEFT,
                },
            )

        if not additional_instance_requested:
            same_type_prepositioned = self._select_instance(
                instrument_id,
                allowed_lifecycles={LIFECYCLE_PREPOSITIONED_RIGHT},
            )
            if same_type_prepositioned is not None:
                return same_type_prepositioned

        same_type_surgeon_owned = self._select_instance(
            instrument_id,
            allowed_lifecycles={LIFECYCLE_SURGEON_OWNED},
        )
        if same_type_surgeon_owned is not None and not additional_instance_requested:
            if same_type_surgeon_owned.location_type not in {
                "surgical_field",
                "bed_fixed_tool",
            }:
                return same_type_surgeon_owned
            available_instance = self._select_instance(
                instrument_id,
                allowed_lifecycles={
                    LIFECYCLE_HOME_RACK,
                    LIFECYCLE_RETURNED_HOME,
                    LIFECYCLE_MAYO_REUSE,
                    LIFECYCLE_MAYO_RECOVERY,
                },
                exclude_reserved=True,
            )
            if available_instance is not None:
                return available_instance
            return same_type_surgeon_owned

        return self._select_instance(
            instrument_id,
            allowed_lifecycles={
                LIFECYCLE_HOME_RACK,
                LIFECYCLE_RETURNED_HOME,
                LIFECYCLE_PREPOSITIONED_RIGHT,
                LIFECYCLE_MAYO_REUSE,
                LIFECYCLE_MAYO_RECOVERY,
            },
            exclude_reserved=True,
        )

    def _request_cue_committed(self, cue: SurgeonRequestCue) -> bool:
        task = self.state.active_robot_task
        if (
            task is not None
            and cue.instance_id
            and task.instrument_instance_id == cue.instance_id
        ):
            return True
        state = self._state_by_instance(cue.instance_id)
        return bool(
            state is not None
            and state.lifecycle_stage
            in {LIFECYCLE_PREPOSITIONED_RIGHT, LIFECYCLE_SURGEON_OWNED}
        )

    def _supersede_blocked_active_voice_request(
        self,
        *,
        incoming_instrument_id: str,
    ) -> bool:
        cue = self._active_request_cue()
        if (
            cue is None
            or cue.event_type != "voice_request"
            or cue.instrument_id == incoming_instrument_id
            or self._request_cue_committed(cue)
            or self.handover_allowed()
        ):
            return False

        superseded = self.state.surgeon_request_queue.popleft()
        self._sync_active_request_from_queue()
        self._record_event(
            "SurgeonRequestSuperseded",
            {
                "superseded_tool": superseded.instrument_id,
                "superseded_instance_id": superseded.instance_id,
                "superseded_generation": superseded.generation,
                "incoming_tool": incoming_instrument_id,
                "reason": "newer_public_voice_request_replaced_uncommitted_blocked_request",
                "queue_length": len(self.state.surgeon_request_queue),
            },
        )
        return True

    def _enqueue_surgeon_request(
        self,
        *,
        event_type: str,
        instrument_id: str,
        voice_text: str = "",
        note: str = "",
        ready_for_handover: bool = True,
        ready_for_retrieval: bool = False,
        override: bool = False,
        force_shadow_additional_instance_assumption: bool = False,
    ) -> bool:
        if not instrument_id:
            return False
        additional_instance_requested = bool(
            force_shadow_additional_instance_assumption
            or _requests_additional_instance(voice_text)
        )
        if event_type in ACTIVE_REQUEST_INTENTS and not additional_instance_requested:
            for existing in self.state.surgeon_request_queue:
                if (
                    existing.instrument_id == instrument_id
                    and existing.event_type in ACTIVE_REQUEST_INTENTS
                ):
                    if voice_text and not existing.voice_text:
                        existing.voice_text = voice_text
                    existing.ready_for_handover = bool(
                        existing.ready_for_handover or ready_for_handover
                    )
                    existing.override = bool(existing.override or override)
                    self._sync_active_request_from_queue()
                    self._record_event(
                        "SurgeonRequestCoalesced",
                        {
                            "queued_tool": instrument_id,
                            "event_type": event_type,
                            "queue_length": len(self.state.surgeon_request_queue),
                            "active_request_tool": self.state.surgeon_request_tool,
                        },
                    )
                    return bool(existing.shadow_additional_instance_assumed)

        if event_type == "voice_request" and not additional_instance_requested:
            self._supersede_blocked_active_voice_request(
                incoming_instrument_id=instrument_id,
            )
            self._reconcile_shadow_capacity_for_public_request(
                instrument_id=instrument_id,
                voice_text=voice_text,
            )

        selected_instance = self._resolve_request_instance(
            instrument_id=instrument_id,
            event_type=event_type,
            additional_instance_requested=additional_instance_requested,
        )
        if selected_instance is None:
            self._record_invariant_violation(
                reason="requested_tool_inventory_exhausted",
                event_type=event_type,
                instrument_id=instrument_id,
            )
            return False
        self._request_generation_counter += 1
        cue = SurgeonRequestCue(
            event_type=event_type,
            instrument_id=instrument_id,
            instance_id=selected_instance.instance_id,
            generation=self._request_generation_counter,
            voice_text=voice_text,
            note=note,
            ready_for_handover=ready_for_handover,
            ready_for_retrieval=ready_for_retrieval,
            override=override,
            shadow_additional_instance_assumed=additional_instance_requested,
        )
        self.state.surgeon_request_queue.append(cue)
        self._sync_active_request_from_queue()
        self._record_event(
            "SurgeonRequestQueued",
            {
                "queued_tool": instrument_id,
                "queued_instance_id": cue.instance_id,
                "event_type": event_type,
                "voice_text": voice_text,
                "queue_length": len(self.state.surgeon_request_queue),
                "active_request_tool": self.state.surgeon_request_tool,
                "request_generation": cue.generation,
            },
        )
        return additional_instance_requested

    def _reconcile_shadow_capacity_for_public_request(
        self,
        *,
        instrument_id: str,
        voice_text: str,
    ) -> bool:
        if not self._allow_shadow_request_capacity_reconciliation:
            return False
        additional_instance = (
            self._allow_shadow_type_instance_requests
            and _requests_additional_instance(voice_text)
        )
        hand_states = self._surgeon_owned_hand_states()
        if len(hand_states) < 2:
            return False
        if (
            not additional_instance
            and any(state.instrument_id == instrument_id for state in hand_states)
        ):
            return False
        if self._is_field_deployed_for_phase(instrument_id):
            return False

        released = min(
            hand_states,
            key=lambda state: (
                float(state.last_update_sec or 0.0),
                state.instance_id,
            ),
        )
        previous_location_type = released.location_type
        previous_location_id = released.location_id
        released.location_type = "surgical_field"
        released.location_id = self._field_anchor_id()
        released.owner = _owner_for_lifecycle(released)
        released.status = _status_for_lifecycle(released)
        self._update_visual_anchor(released)
        self._record_shadow_assumption(
            "ShadowPublicRequestHandCapacityReconciled",
            {
                "instrument_id": released.instrument_id,
                "instance_id": released.instance_id,
                "incoming_request_tool": instrument_id,
                "previous_location_type": previous_location_type,
                "previous_location_id": previous_location_id,
                "location_type": released.location_type,
                "location_id": released.location_id,
                "reason": (
                    "public_voice_request_implies_one_active_handover_slot;"
                    "exact_non_hand_location_remains_unobserved"
                ),
            },
        )
        return True

    def _dequeue_active_request(self, reason: str) -> None:
        cue = self._active_request_cue()
        if cue is None:
            self._sync_active_request_from_queue()
            return
        completed_tool = cue.instrument_id
        completed_instance_id = cue.instance_id
        self.state.surgeon_request_queue.popleft()
        self._sync_active_request_from_queue()
        self._record_event(
            "SurgeonRequestDequeued",
            {
                "completed_tool": completed_tool,
                "completed_instance_id": completed_instance_id,
                "reason": reason,
                "queue_length": len(self.state.surgeon_request_queue),
                "active_request_tool": self.state.surgeon_request_tool,
            },
        )

    def request_queue_summary(self) -> dict[str, Any]:
        return {
            "queue_length": len(self.state.surgeon_request_queue),
            "active_request_tool": self.state.surgeon_request_tool,
            "active_request_instance_id": self.state.surgeon_request_instance_id,
            "active_request_generation": self.state.surgeon_request_generation,
            "active_request_additional_instance_assumed": (
                self.state.surgeon_request_additional_instance_assumed
            ),
            "queued_tools": [cue.instrument_id for cue in self.state.surgeon_request_queue],
            "queued_instance_ids": [
                cue.instance_id for cue in self.state.surgeon_request_queue
            ],
            "queued_generations": [cue.generation for cue in self.state.surgeon_request_queue],
            "queued_additional_instance_assumptions": [
                cue.shadow_additional_instance_assumed
                for cue in self.state.surgeon_request_queue
            ],
        }

    def _cleanup_still_pending(self) -> bool:
        if (
            self.state.cleaner_busy
            or self.state.left_hand_tool
            or self.state.right_hand_tool
            or self.state.prepositioned_tool
            or self.state.active_recovery_tools
            or self.state.active_robot_task is not None
        ):
            return True
        for state in self.instrument_states.values():
            if state.lifecycle_stage not in {LIFECYCLE_HOME_RACK, LIFECYCLE_RETURNED_HOME}:
                return True
        return False

    def _begin_completion_cleanup(self) -> None:
        self._clear_surgeon_request_state()
        self.state.surgeon_intent = "procedure_finishing"
        if self.state.execution_state != "completed":
            self.state.running = True
            self.state.execution_state = "finishing"

    def _mark_completed(self) -> None:
        was_completed = self.state.execution_state == "completed"
        self._clear_surgeon_request_state()
        self._clear_active_robot_task()
        self.state.cleaner_busy = False
        self.state.cleaner_remaining_sec = 0.0
        self.state.left_hand_tool = ""
        self.state.left_hand_tool_instance_id = ""
        self.state.right_hand_tool = ""
        self.state.right_hand_tool_instance_id = ""
        self.state.prepositioned_tool = ""
        self.state.prepositioned_tool_instance_id = ""
        self.state.robot_state = "idle"
        self.state.running = False
        self.state.execution_state = "completed"
        self.state.surgeon_intent = "procedure_complete"
        self.state.pending_transition_tools = []
        self.state.active_recovery_tools = []
        self.state.active_recovery_tool_instances = []
        if not was_completed:
            self._record_event(
                "ProcedureCompleted",
                {
                    "reason": "cleanup_complete",
                    "filtered_phase": self.state.filtered_phase,
                },
            )

    def _complete_if_cleanup_finished(self) -> None:
        if self.state.execution_state == "finishing" and not self._cleanup_still_pending():
            self._mark_completed()

    def _start_active_robot_task(
        self,
        *,
        task_id: str,
        task_type: str,
        instrument_id: str,
        arm: str,
        source_anchor_id: str,
        target_anchor_id: str,
        duration_sec: float,
        instrument_instance_id: str = "",
    ) -> None:
        self.state.active_robot_task = ActiveRobotTask(
            task_id=task_id,
            task_type=task_type,
            instrument_id=instrument_id,
            instrument_instance_id=instrument_instance_id,
            arm=arm,
            source_anchor_id=source_anchor_id,
            target_anchor_id=target_anchor_id,
            started_at_sec=self._monotonic_sec(),
            duration_sec=max(float(duration_sec), 0.1),
            progress=0.0,
            remaining_sec=max(float(duration_sec), 0.0),
        )

    def _refresh_active_robot_task(self) -> None:
        task = self.state.active_robot_task
        if task is None or not task.task_id:
            return
        elapsed = max(self._monotonic_sec() - float(task.started_at_sec), 0.0)
        duration = max(float(task.duration_sec), 0.1)
        task.progress = max(0.0, min(1.0, elapsed / duration))
        task.remaining_sec = max(duration - elapsed, 0.0)

    def _seed_from_initial_perception(self) -> None:
        stages = self.spec.get_mock_perception_stages()
        if not stages:
            return
        bootstrap_index = self.spec.get_mock_perception_bootstrap_stage_index()
        stage = stages[min(bootstrap_index, len(stages) - 1)]
        if stage.phase_hypotheses:
            best_phase = max(stage.phase_hypotheses, key=lambda hypothesis: float(hypothesis.confidence))
            self.state.filtered_phase = best_phase.phase_id or self.state.filtered_phase
            self.state.phase_confidence = float(best_phase.confidence)
            self.state.phase_uncertain = bool(stage.uncertainty >= 0.35)
            self.state.phase_stability = max(self.state.phase_stability, 0.5)
        for observation in stage.observations:
            if not observation.visible:
                continue
            state = self.get_instrument_state(observation.instrument_id)
            if state is None:
                continue
            self._apply_observation_rebase(
                state=state,
                location_type=observation.location_type,
                location_id=observation.location_id,
                confidence=float(observation.confidence),
                stamp_sec=0.0,
            )
        self._relocate_phase_field_deployed_tools(
            self.state.filtered_phase,
            reason="initial_perception_phase",
        )

    def _record_event(self, event_type: str, payload: dict[str, Any]) -> None:
        self.event_history.append({"event_type": event_type, **payload})
        self.state.recent_event_types.appendleft(event_type)

    def _record_shadow_assumption(
        self,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        audit_payload = {
            "event_type": event_type,
            **payload,
            "ground_truth_used": False,
        }
        self._shadow_assumption_audit.append(dict(audit_payload))
        self._record_event(event_type, audit_payload)

    def drain_shadow_assumption_audit(self) -> list[dict[str, Any]]:
        assumptions = list(self._shadow_assumption_audit)
        self._shadow_assumption_audit.clear()
        return assumptions

    def _phase_evidence_rows(
        self,
        phase_id: str,
        *,
        include_later_normal_phases: bool = False,
    ) -> list[dict[str, float]]:
        """Classify bounded evidence as support, contradiction, or unknown."""

        if not phase_id or not self._phase_evidence_history:
            return []
        floor_index = self._normal_phase_index(phase_id)
        minimum_clear_confidence = float(
            self.spec.bundle.phase_guard.min_confidence_to_keep
        )
        rows: list[dict[str, float]] = []
        for sample in self._phase_evidence_history:
            phase_scores = sample.get("scores", {})
            if not isinstance(phase_scores, dict) or not phase_scores:
                continue
            uncertainty = float(sample.get("uncertainty", 1.0))
            explicit_scores: list[float] = []
            if include_later_normal_phases and floor_index >= 0:
                explicit_scores = [
                    float(score)
                    for candidate, score in phase_scores.items()
                    if self._normal_phase_index(str(candidate)) >= floor_index
                ]
            elif phase_id in phase_scores:
                explicit_scores = [float(phase_scores[phase_id])]

            if explicit_scores:
                score = max(explicit_scores)
            else:
                strongest_alternative = max(
                    (float(score) for score in phase_scores.values()),
                    default=0.0,
                )
                if (
                    uncertainty > 0.45
                    or strongest_alternative < minimum_clear_confidence
                ):
                    # Occlusion, weak ranking, or a genuinely indeterminate
                    # frame does not erase otherwise valid temporal evidence.
                    continue
                score = 0.0
            rows.append(
                {
                    "score": score,
                    "uncertainty": uncertainty,
                    "stamp_sec": float(sample.get("stamp_sec", 0.0)),
                }
            )
        return rows[
            -max(
                1,
                int(self.spec.bundle.phase_guard.smoothing_window),
            ) :
        ]

    def _phase_evidence_summary(self, phase_id: str) -> tuple[float, float, int]:
        rows = self._phase_evidence_rows(phase_id)
        if not rows:
            return (0.0, 1.0, 0)
        return (
            sum(row["score"] for row in rows) / len(rows),
            sum(row["uncertainty"] for row in rows) / len(rows),
            len(rows),
        )

    def _phase_evidence_summary_at_or_after(
        self, phase_id: str
    ) -> tuple[float, float, int]:
        """Treat a stable later normal phase as evidence that earlier steps passed."""

        rows = self._phase_evidence_rows(
            phase_id,
            include_later_normal_phases=True,
        )
        if not rows:
            return (0.0, 1.0, 0)
        return (
            sum(row["score"] for row in rows) / len(rows),
            sum(row["uncertainty"] for row in rows) / len(rows),
            len(rows),
        )

    def _phase_evidence_source_span_sec(
        self,
        phase_id: str,
        *,
        include_later_normal_phases: bool = False,
    ) -> tuple[float, bool]:
        rows = self._phase_evidence_rows(
            phase_id,
            include_later_normal_phases=include_later_normal_phases,
        )
        if not rows:
            return (0.0, False)
        stamps = sorted(
            {
                float(row["stamp_sec"])
                for row in rows
                if float(row["stamp_sec"]) > 0.0
            }
        )
        if not stamps:
            # Legacy/manual evidence without a source clock keeps the prior
            # sample-count contract. Camera-backed evidence always carries one.
            return (0.0, False)
        if len(stamps) < 2:
            return (0.0, True)
        return (max(stamps) - min(stamps), True)

    def _phase_transition_required_counts(
        self, current_phase: str, target_phase: str
    ) -> dict[str, int]:
        configured = self._configured_phase_transition_required_counts.get(
            (current_phase, target_phase)
        )
        return dict(configured) if configured is not None else {}

    def _phase_interaction_complete(
        self, current_phase: str, target_phase: str
    ) -> bool:
        required_counts = self._phase_transition_required_counts(
            current_phase, target_phase
        )
        if required_counts:
            observed = self._phase_instance_interactions.get(
                current_phase, {}
            )
            return all(
                len(observed.get(tool_id, set())) >= required_count
                for tool_id, required_count in required_counts.items()
            )

        expected = list(self.spec.get_expected_instruments(current_phase))
        if not expected:
            return True
        completed_count = 0
        for tool_id in expected:
            instances = self._instances_for_type(tool_id)
            if any(state.ever_surgeon_owned for state in instances):
                completed_count += 1
                continue
            if any(
                state.lifecycle_stage
                in {
                    LIFECYCLE_SURGEON_OWNED,
                    LIFECYCLE_MAYO_REUSE,
                    LIFECYCLE_MAYO_RECOVERY,
                    LIFECYCLE_RECOVERING_LEFT,
                    LIFECYCLE_CLEANING_LEFT,
                    LIFECYCLE_CLEANED_LEFT,
                    LIFECYCLE_RETURNED_HOME,
                }
                for state in instances
            ):
                completed_count += 1
        required_count = max(1, math.ceil(len(expected) * PHASE_INTERACTION_MIN_FRACTION))
        return completed_count >= required_count

    def _phase_blocking_reason(self) -> str:
        # Phase is a belief about the surgical context, not about whether the
        # humanoid has finished its current action. BT/action guards already
        # block new robot commands while execution or recovery is pending.
        return ""

    def _set_active_interrupt_context(self, phase_id: str, reason: str) -> None:
        if not phase_id or not self.spec.is_interrupt_phase(phase_id):
            return
        self._active_interrupt_event_phase = phase_id
        self._active_interrupt_event_seen_sec = self._monotonic_sec()
        self.state.predicted_tool = ""
        self.state.predicted_tool_confidence = 0.0
        self.state.predicted_tool_stability_sec = 0.0
        self.state.recent_event_types.appendleft(f"ActiveInterruptContext:{phase_id}")
        self._record_event(
            "ActiveInterruptContextUpdated",
            {
                "phase_id": phase_id,
                "reason": reason,
            },
        )

    def _clear_active_interrupt_context(self) -> None:
        self._active_interrupt_event_phase = ""
        self._active_interrupt_event_seen_sec = 0.0

    def _active_context_phase_id(self) -> str:
        if not self._active_interrupt_event_phase:
            return self.state.filtered_phase
        if self._monotonic_sec() - self._active_interrupt_event_seen_sec > 8.0:
            self._clear_active_interrupt_context()
            return self.state.filtered_phase
        return self._active_interrupt_event_phase

    def _record_phase_decision(
        self,
        *,
        target_phase: str,
        accepted: bool,
        reason: str,
        confidence: float,
        cue_id: str = "",
        current_phase: str | None = None,
    ) -> dict[str, Any]:
        decision_current_phase = current_phase or self.state.filtered_phase
        if not accepted:
            key = (decision_current_phase, target_phase, reason)
            now = self._monotonic_sec()
            last_seen = self._phase_decision_cooldowns.get(key, -9999.0)
            if now - last_seen < 3.0:
                return {}
            self._phase_decision_cooldowns[key] = now
        event_type = "PhaseTransitionAccepted" if accepted else "PhaseTransitionRejected"
        payload = {
            "current_phase": decision_current_phase,
            "target_phase": target_phase,
            "accepted": accepted,
            "reason": reason,
            "confidence": float(confidence),
            "cue_id": cue_id,
        }
        self._record_event(event_type, payload)
        return {"event_type": event_type, **payload}

    def _record_interrupt_event_decision(
        self,
        *,
        target_phase: str,
        accepted: bool,
        reason: str,
        confidence: float,
        cue_id: str = "",
        current_phase: str | None = None,
    ) -> dict[str, Any]:
        decision_current_phase = current_phase or self.state.filtered_phase
        key = (decision_current_phase, target_phase, reason)
        now = self._monotonic_sec()
        last_seen = self._phase_decision_cooldowns.get(key, -9999.0)
        if now - last_seen < 5.0:
            return {}
        self._phase_decision_cooldowns[key] = now
        event_type = "InterruptEventDetected" if accepted else "InterruptEventRejected"
        payload = {
            "current_phase": decision_current_phase,
            "target_phase": target_phase,
            "accepted": accepted,
            "reason": reason,
            "confidence": float(confidence),
            "cue_id": cue_id,
        }
        self.event_history.append({"event_type": event_type, **payload})
        self.state.recent_event_types.appendleft(f"{event_type}:{target_phase}")
        return {"event_type": event_type, **payload}

    def _phase_evidence_stable_enough(
        self,
        target_phase: str,
        *,
        require_full_window: bool = True,
        include_later_normal_phases: bool = False,
    ) -> tuple[bool, float, int]:
        guard = self.spec.bundle.phase_guard
        if include_later_normal_phases:
            average_confidence, average_uncertainty, sample_count = (
                self._phase_evidence_summary_at_or_after(target_phase)
            )
        else:
            average_confidence, average_uncertainty, sample_count = (
                self._phase_evidence_summary(target_phase)
            )
        source_span_sec, has_source_clock = (
            self._phase_evidence_source_span_sec(
                target_phase,
                include_later_normal_phases=include_later_normal_phases,
            )
        )
        required_samples = (
            2
            if require_full_window and has_source_clock
            else (
                max(2, int(guard.smoothing_window))
                if require_full_window
                else 1
            )
        )
        minimum_source_span_sec = (
            max(0.0, float(guard.min_evidence_duration_sec))
            if require_full_window
            else 0.0
        )
        stable = (
            sample_count >= required_samples
            and average_confidence >= float(guard.min_confidence_to_switch)
            and average_uncertainty <= 0.45
            and (
                not has_source_clock
                or source_span_sec >= minimum_source_span_sec
            )
        )
        return stable, average_confidence, sample_count

    def _static_or_dynamic_transition_allowed(self, current_phase: str, target_phase: str) -> bool:
        current_index = self._normal_phase_index(current_phase)
        target_index = self._normal_phase_index(target_phase)
        if (
            target_index >= 0
            and (
                target_index < self._initial_phase_floor_index
                or (current_index >= 0 and target_index < current_index)
            )
        ):
            return False
        if self.spec.is_transition_allowed(current_phase, target_phase):
            return True
        if self.spec.is_interrupt_phase(current_phase):
            return bool(target_phase and target_phase == self._last_normal_phase_before_interrupt)
        return False

    def _approve_phase_transition(
        self,
        target_phase: str,
        *,
        reason: str,
        confidence: float,
        cue_id: str = "",
    ) -> dict[str, Any]:
        current_phase = self.state.filtered_phase or self.spec.default_phase_id
        target_index = self._normal_phase_index(target_phase)
        current_index = self._normal_phase_index(current_phase)
        if (
            target_index >= 0
            and (
                target_index < self._initial_phase_floor_index
                or (current_index >= 0 and target_index < current_index)
            )
        ):
            return self._record_phase_decision(
                target_phase=target_phase,
                accepted=False,
                reason="phase_regression_below_start_floor",
                confidence=confidence,
                cue_id=cue_id,
                current_phase=current_phase,
            )
        if self.spec.is_interrupt_phase(target_phase) and self.spec.is_normal_phase(current_phase):
            self._last_normal_phase_before_interrupt = current_phase
            self._set_active_interrupt_context(target_phase, reason)
            self._apply_emergency_interrupt_preemption(
                target_phase=target_phase,
                reason=reason,
                cue_id=cue_id,
            )
        elif self.spec.is_normal_phase(target_phase):
            self._last_normal_phase_before_interrupt = ""
        self.state.filtered_phase = target_phase
        self.state.phase_confidence = float(confidence)
        self.state.phase_uncertain = False
        self.state.phase_stability = min(1.0, float(confidence))
        self._relocate_phase_field_deployed_tools(
            target_phase,
            reason="phase_transition_approved",
        )
        self._phase_bootstrap_open = False
        self._phase_entered_sec = self._monotonic_sec()
        self._pending_phase_cues.clear()
        self._phase_decision_cooldowns.clear()
        if self.state.robot_state == "retracted":
            self.state.robot_state = "idle"
        self._recompute_transient_state()
        return self._record_phase_decision(
            target_phase=target_phase,
            accepted=True,
            reason=reason,
            confidence=confidence,
            cue_id=cue_id,
            current_phase=current_phase,
        )

    def _resolve_phase_bootstrap_at_current_phase(
        self,
        current_phase: str,
        *,
        confidence: float,
    ) -> dict[str, Any]:
        self._phase_bootstrap_open = False
        current_index = self._normal_phase_index(current_phase)
        if current_index >= 0:
            self._initial_phase_floor_index = current_index
        self._phase_entered_sec = self._monotonic_sec()
        self._pending_phase_cues.clear()
        self._phase_decision_cooldowns.clear()
        self.state.phase_confidence = float(confidence)
        self.state.phase_uncertain = False
        self.state.phase_stability = min(1.0, float(confidence))
        payload = {
            "current_phase": current_phase,
            "target_phase": current_phase,
            "accepted": True,
            "reason": "stable_vlm_phase_bootstrap_confirmed_current",
            "confidence": float(confidence),
            "cue_id": "",
        }
        self._record_event("PhaseBootstrapResolved", payload)
        return {"event_type": "PhaseBootstrapResolved", **payload}

    def _try_approve_phase_transition(
        self,
        target_phase: str,
        *,
        cue_id: str = "",
        allow_downstream_evidence: bool = False,
    ) -> dict[str, Any]:
        current_phase = self.state.filtered_phase or self.spec.default_phase_id
        guard = self.spec.bundle.phase_guard
        if self.state.execution_state in {"finishing", "completed"}:
            return {}
        if not target_phase or target_phase == current_phase:
            return self._record_phase_decision(
                target_phase=target_phase or current_phase,
                accepted=False,
                reason="no_phase_change_requested",
                confidence=self.state.phase_confidence,
                cue_id=cue_id,
            )
        if target_phase not in self.spec.phase_ids:
            return self._record_phase_decision(
                target_phase=target_phase,
                accepted=False,
                reason="phase_out_of_bundle_scope",
                confidence=0.0,
                cue_id=cue_id,
            )
        target_index = self._normal_phase_index(target_phase)
        current_index = self._normal_phase_index(current_phase)
        if (
            target_index >= 0
            and (
                target_index < self._initial_phase_floor_index
                or (current_index >= 0 and target_index < current_index)
            )
        ):
            return self._record_phase_decision(
                target_phase=target_phase,
                accepted=False,
                reason="phase_regression_below_start_floor",
                confidence=0.0,
                cue_id=cue_id,
            )
        if (
            self._phase_bootstrap_open
            and current_phase == self.spec.default_phase_id
            and self.spec.is_normal_phase(target_phase)
            and target_phase != current_phase
            and target_phase not in self._pending_phase_cues
        ):
            stable, average_confidence, _ = self._phase_evidence_stable_enough(
                target_phase
            )
            if stable:
                return self._approve_phase_transition(
                    target_phase,
                    reason="stable_vlm_phase_bootstrap",
                    confidence=average_confidence,
                    cue_id=cue_id,
                )
            return self._record_phase_decision(
                target_phase=target_phase,
                accepted=False,
                reason="phase_bootstrap_evidence_not_stable",
                confidence=average_confidence,
                cue_id=cue_id,
            )
        if not self._static_or_dynamic_transition_allowed(current_phase, target_phase):
            return self._record_phase_decision(
                target_phase=target_phase,
                accepted=False,
                reason="phase_transition_not_allowed_by_spec",
                confidence=0.0,
                cue_id=cue_id,
            )
        if (
            not self._phase_bootstrap_open
            and not self._phase_field_deployment_ready(target_phase)
        ):
            return self._record_phase_decision(
                target_phase=target_phase,
                accepted=False,
                reason="phase_field_deployment_not_observed",
                confidence=0.0,
                cue_id=cue_id,
            )
        cue = self._pending_phase_cues.get(target_phase)
        if self.spec.is_interrupt_phase(target_phase):
            cue_confidence = float(cue.get("confidence", 0.0)) if cue is not None else 0.0
            cue_source = str(cue.get("source", "")) if cue is not None else ""
            cue_reason = str(cue.get("reason", "")) if cue is not None else ""
            emergency_keywords = ("bleed", "bleeding", "hemostasis", "haemostasis", "blood", "emergency")
            manual_emergency_cue = (
                cue is not None
                and cue_confidence >= 0.90
                and (
                    cue_source in {"manual_test", "simulation_manager", "surgeon_actor", "manual", "test"}
                    or any(keyword in cue_reason.lower() for keyword in emergency_keywords)
                )
            )
            if manual_emergency_cue:
                self._record_interrupt_event_decision(
                    target_phase=target_phase,
                    accepted=True,
                    reason="manual_emergency_interrupt_cue",
                    confidence=cue_confidence,
                    cue_id=cue_id,
                    current_phase=current_phase,
                )
                return self._approve_phase_transition(
                    target_phase,
                    reason="manual_emergency_interrupt_cue",
                    confidence=cue_confidence,
                    cue_id=cue_id,
                )

            stable, average_confidence, sample_count = self._phase_evidence_stable_enough(target_phase)
            if stable:
                self._record_interrupt_event_decision(
                    target_phase=target_phase,
                    accepted=True,
                    reason="stable_interrupt_event_evidence",
                    confidence=average_confidence,
                    cue_id=cue_id,
                    current_phase=current_phase,
                )
                return self._approve_phase_transition(
                    target_phase,
                    reason="stable_interrupt_event_evidence",
                    confidence=average_confidence,
                    cue_id=cue_id,
                )
            return self._record_interrupt_event_decision(
                target_phase=target_phase,
                accepted=False,
                reason="interrupt_phase_evidence_not_stable",
                confidence=average_confidence,
                cue_id=cue_id,
                current_phase=current_phase,
            )
        if self.spec.is_interrupt_phase(current_phase) and self.spec.is_normal_phase(target_phase):
            expected_return = self._last_normal_phase_before_interrupt
            if expected_return and target_phase != expected_return:
                return self._record_phase_decision(
                    target_phase=target_phase,
                    accepted=False,
                    reason="interrupt_return_target_not_previous_phase",
                    confidence=0.0,
                    cue_id=cue_id,
                )
            if self._is_priority_interrupt_phase(current_phase):
                cue = self._pending_phase_cues.get(target_phase)
                cue_reason = str(cue.get("reason", "")) if cue is not None else ""
                cue_source = str(cue.get("source", "")) if cue is not None else ""
                resolved_keywords = ("resolved", "clear", "cleared", "controlled", "hemostasis complete", "bleeding resolved")
                explicit_resolved = (
                    cue is not None
                    and (
                        cue_source in {"manual_test", "simulation_manager", "surgeon_actor", "manual", "test"}
                        or any(keyword in cue_reason.lower() for keyword in resolved_keywords)
                    )
                    and any(keyword in cue_reason.lower() for keyword in resolved_keywords)
                )
                if not explicit_resolved:
                    return self._record_phase_decision(
                        target_phase=target_phase,
                        accepted=False,
                        reason="emergency_interrupt_return_requires_resolution",
                        confidence=0.0,
                        cue_id=cue_id,
                    )

            stable, average_confidence, sample_count = self._phase_evidence_stable_enough(target_phase)
            if stable:
                return self._approve_phase_transition(
                    target_phase,
                    reason="stable_interrupt_return_evidence",
                    confidence=average_confidence,
                    cue_id=cue_id,
                )
            return self._record_phase_decision(
                target_phase=target_phase,
                accepted=False,
                reason="interrupt_return_evidence_not_stable",
                confidence=average_confidence,
                cue_id=cue_id,
            )
        evidence_stable = False
        evidence_confidence = 0.0
        if cue is None:
            evidence_stable, evidence_confidence, _ = (
                self._phase_evidence_stable_enough(
                    target_phase,
                    include_later_normal_phases=allow_downstream_evidence,
                )
            )
            if not evidence_stable:
                return self._record_phase_decision(
                    target_phase=target_phase,
                    accepted=False,
                    reason="phase_evidence_not_stable",
                    confidence=evidence_confidence,
                    cue_id=cue_id,
                )
        resolved_cue_id = (
            str(cue.get("cue_id", cue_id))
            if cue is not None
            else cue_id
        )
        dwell_elapsed = self._monotonic_sec() - self._phase_entered_sec
        min_dwell = max(float(guard.min_dwell_time_sec), float(self.spec.get_phase_min_duration(current_phase)))
        if dwell_elapsed < min_dwell:
            return self._record_phase_decision(
                target_phase=target_phase,
                accepted=False,
                reason="phase_min_dwell_not_satisfied",
                confidence=0.0,
                cue_id=resolved_cue_id,
            )
        blocking_reason = self._phase_blocking_reason()
        if blocking_reason:
            return self._record_phase_decision(
                target_phase=target_phase,
                accepted=False,
                reason=blocking_reason,
                confidence=0.0,
                cue_id=resolved_cue_id,
            )
        required_counts = self._phase_transition_required_counts(
            current_phase, target_phase
        )
        if required_counts and not self._phase_interaction_complete(
            current_phase, target_phase
        ):
            return self._record_phase_decision(
                target_phase=target_phase,
                accepted=False,
                reason="required_transition_evidence_incomplete",
                confidence=0.0,
                cue_id=resolved_cue_id,
            )

        if allow_downstream_evidence:
            average_confidence, average_uncertainty, sample_count = (
                self._phase_evidence_summary_at_or_after(target_phase)
            )
        else:
            average_confidence, average_uncertainty, sample_count = (
                self._phase_evidence_summary(target_phase)
            )
        if cue is None:
            return self._approve_phase_transition(
                target_phase,
                reason=(
                    "stable_downstream_vlm_evidence"
                    if allow_downstream_evidence
                    else "stable_vlm_phase_evidence"
                ),
                confidence=max(evidence_confidence, average_confidence),
                cue_id=cue_id,
            )
        cue_confidence = max(float(cue.get("confidence", 0.0)), average_confidence)
        return self._approve_phase_transition(
            target_phase,
            reason="surgeon_cue_accepted",
            confidence=cue_confidence,
            cue_id=resolved_cue_id,
        )

    def apply_phase_transition_cue(self, cue: PhaseTransitionCue) -> dict[str, Any]:
        target_phase = cue.target_phase or self.state.filtered_phase
        cue_id = cue.cue_id or f"{cue.source}:{self.state.filtered_phase}->{target_phase}"
        self._pending_phase_cues[target_phase] = {
            "cue_id": cue_id,
            "source": cue.source,
            "current_phase": cue.current_phase or self.state.filtered_phase,
            "target_phase": target_phase,
            "confidence": float(cue.confidence),
            "reason": cue.reason,
            "stamp_sec": _stamp_to_sec(cue.stamp),
        }
        self._record_event(
            "PhaseTransitionCueObserved",
            {
                "cue_id": cue_id,
                "source": cue.source,
                "current_phase": cue.current_phase or self.state.filtered_phase,
                "target_phase": target_phase,
                "confidence": float(cue.confidence),
                "reason": cue.reason,
            },
        )
        return self._try_approve_phase_transition(target_phase, cue_id=cue_id)

    def apply_phase_evidence(self, evidence: PhaseEvidence) -> list[dict[str, Any]]:
        scores = {
            self.spec.resolve_phase_id(str(phase_id)) or str(phase_id): float(confidence)
            for phase_id, confidence in zip(evidence.phase_ids, evidence.phase_confidences)
        }
        if not scores:
            return []
        stamp_sec = _stamp_to_sec(evidence.stamp)
        sample = {
            "source": evidence.source,
            "scores": scores,
            "uncertainty": float(evidence.uncertainty),
            "stamp_sec": stamp_sec,
            "summary": evidence.scene_summary,
        }
        correlated_index = None
        if stamp_sec > 0.0:
            for index in range(len(self._phase_evidence_history) - 1, -1, -1):
                previous = self._phase_evidence_history[index]
                if (
                    str(previous.get("source", "")) == str(evidence.source)
                    and abs(
                        float(previous.get("stamp_sec", 0.0)) - stamp_sec
                    )
                    <= 1e-6
                ):
                    correlated_index = index
                    break
        if correlated_index is not None:
            previous = self._phase_evidence_history[correlated_index]
            if (
                previous.get("scores") == sample["scores"]
                and float(previous.get("uncertainty", 1.0))
                == sample["uncertainty"]
                and str(previous.get("summary", "")) == sample["summary"]
            ):
                self._record_event(
                    "PhaseEvidenceDuplicateSuppressed",
                    {
                        "source": evidence.source,
                        "stamp_sec": stamp_sec,
                        "scores": scores,
                        "uncertainty": float(evidence.uncertainty),
                    },
                )
                return []
            self._phase_evidence_history[correlated_index] = sample
            self._record_event(
                "PhaseEvidenceCorrelatedFrameUpdated",
                {
                    "source": evidence.source,
                    "stamp_sec": stamp_sec,
                    "previous_scores": previous.get("scores", {}),
                    "scores": scores,
                    "uncertainty": float(evidence.uncertainty),
                },
            )
        else:
            self._phase_evidence_history.append(sample)
        current_phase = self.state.filtered_phase or self.spec.default_phase_id
        current_confidence = float(scores.get(current_phase, 0.0))
        minimum_keep_confidence = (
            float(self.spec.bundle.phase_guard.min_confidence_to_keep)
            if self.spec.bundle.phase_guard is not None
            else 0.5
        )
        self.state.phase_confidence = current_confidence
        self.state.phase_stability = current_confidence
        self.state.phase_uncertain = bool(
            float(evidence.uncertainty) > 0.35
            or current_confidence < minimum_keep_confidence
        )
        self._record_event(
            "PhaseEvidenceObserved",
            {
                "source": evidence.source,
                "scores": scores,
                "uncertainty": float(evidence.uncertainty),
                "scene_summary": evidence.scene_summary,
            },
        )
        decisions: list[dict[str, Any]] = []
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        if ranked:
            top_phase = ranked[0][0]
            if (
                self._phase_bootstrap_open
                and top_phase == current_phase
                and self.spec.is_normal_phase(current_phase)
            ):
                stable, average_confidence, _ = self._phase_evidence_stable_enough(
                    current_phase
                )
                if stable:
                    decisions.append(
                        self._resolve_phase_bootstrap_at_current_phase(
                            current_phase,
                            confidence=average_confidence,
                        )
                    )
            elif top_phase != current_phase and self.spec.is_interrupt_phase(current_phase):
                decisions.append(self._try_approve_phase_transition(top_phase))
            elif (
                top_phase != current_phase
                and self.spec.is_normal_phase(current_phase)
                and self.spec.is_normal_phase(top_phase)
            ):
                current_index = self._normal_phase_index(current_phase)
                top_index = self._normal_phase_index(top_phase)
                if self._phase_bootstrap_open:
                    decisions.append(
                        self._try_approve_phase_transition(top_phase)
                    )
                elif self._static_or_dynamic_transition_allowed(
                    current_phase, top_phase
                ):
                    decisions.append(
                        self._try_approve_phase_transition(top_phase)
                    )
                elif top_index > current_index:
                    next_phase = self.spec.get_next_normal_phase(current_phase)
                    next_index = self._normal_phase_index(next_phase)
                    if next_phase and current_index < next_index <= top_index:
                        decisions.append(
                            self._try_approve_phase_transition(
                                next_phase,
                                allow_downstream_evidence=True,
                            )
                        )
            if self.spec.is_normal_phase(current_phase):
                for phase_id, confidence in ranked:
                    if phase_id == current_phase or not self.spec.is_interrupt_phase(phase_id):
                        continue
                    if float(confidence) < 0.35:
                        continue
                    decisions.append(self._try_approve_phase_transition(phase_id))
                    break
        for target_phase in list(self._pending_phase_cues):
            decisions.append(self._try_approve_phase_transition(target_phase))
        return [decision for decision in decisions if decision]

    def _open_recovery_transaction(
        self, instrument_or_instance_id: str, reason: str
    ) -> None:
        state = self.get_instrument_state(
            instrument_or_instance_id,
            allowed_lifecycles={
                LIFECYCLE_SURGEON_OWNED,
                LIFECYCLE_MAYO_REUSE,
                LIFECYCLE_MAYO_RECOVERY,
                LIFECYCLE_RECOVERING_LEFT,
                LIFECYCLE_CLEANING_LEFT,
                LIFECYCLE_CLEANED_LEFT,
            },
        )
        if state is None:
            return
        instrument_id = state.instrument_id
        instance_id = state.instance_id
        if self._is_active_requested_tool(instance_id) and self.state.surgeon_intent in ACTIVE_REQUEST_INTENTS:
            self._record_event(
                "RecoveryBlockedForActiveRequest",
                {
                    "instrument_id": instrument_id,
                    "instance_id": instance_id,
                    "reason": reason,
                    "active_request_tool": self._active_requested_tool_id(),
                    "surgeon_intent": self.state.surgeon_intent,
                    "lifecycle_stage": state.lifecycle_stage,
                    "location_type": state.location_type,
                    "location_id": state.location_id,
                },
            )
            return

        if state.lifecycle_stage == LIFECYCLE_MAYO_REUSE:
            self._record_event(
                "RecoveryTransactionMarkedMayoReuse",
                {
                    "instrument_id": instrument_id,
                    "instance_id": instance_id,
                    "reason": reason,
                    "location_id": state.location_id,
                    "location_type": state.location_type,
                    "lifecycle_stage": state.lifecycle_stage,
                },
            )
        elif state.lifecycle_stage not in {
            LIFECYCLE_SURGEON_OWNED,
            LIFECYCLE_MAYO_RECOVERY,
            LIFECYCLE_RECOVERING_LEFT,
            LIFECYCLE_CLEANING_LEFT,
            LIFECYCLE_CLEANED_LEFT,
        }:
            self._record_event(
                "RecoveryTransactionIgnored",
                {
                    "instrument_id": instrument_id,
                    "instance_id": instance_id,
                    "reason": reason,
                    "lifecycle_stage": state.lifecycle_stage,
                },
            )
            return
        if instance_id in self.state.active_recovery_tool_instances:
            return
        self.state.active_recovery_tool_instances.append(instance_id)
        if instrument_id not in self.state.active_recovery_tools:
            self.state.active_recovery_tools.append(instrument_id)
        self._record_event(
            "RecoveryTransactionOpened",
            {
                "instrument_id": instrument_id,
                "instance_id": instance_id,
                "reason": reason,
            },
        )

    def _close_recovery_transaction(
        self, instrument_or_instance_id: str, reason: str
    ) -> None:
        state = self.get_instrument_state(instrument_or_instance_id)
        instance_id = (
            state.instance_id if state is not None else instrument_or_instance_id
        )
        if (
            not instance_id
            or instance_id not in self.state.active_recovery_tool_instances
        ):
            return
        self.state.active_recovery_tool_instances = [
            candidate
            for candidate in self.state.active_recovery_tool_instances
            if candidate != instance_id
        ]
        instrument_id = (
            state.instrument_id
            if state is not None
            else instance_id.partition("#")[0]
        )
        remaining_types = {
            candidate.partition("#")[0]
            for candidate in self.state.active_recovery_tool_instances
        }
        self.state.active_recovery_tools = sorted(remaining_types)
        self._record_event(
            "RecoveryTransactionClosed",
            {
                "instrument_id": instrument_id,
                "instance_id": instance_id,
                "reason": reason,
            },
        )

    def _recovery_transaction_active(
        self, instrument_or_instance_id: str
    ) -> bool:
        if not instrument_or_instance_id:
            return False
        if (
            instrument_or_instance_id
            in self.state.active_recovery_tool_instances
        ):
            return True
        state = self._state_by_instance(instrument_or_instance_id)
        instrument_id = (
            state.instrument_id
            if state is not None
            else instrument_or_instance_id
        )
        return any(
            candidate.partition("#")[0] == instrument_id
            for candidate in self.state.active_recovery_tool_instances
        )

    def _set_flag(self, flag: str, enabled: bool) -> None:
        if enabled:
            if flag not in self.state.safety_flags:
                self.state.safety_flags.append(flag)
            return
        self.state.safety_flags = [existing for existing in self.state.safety_flags if existing != flag]

    def set_safety_flag(self, flag: str, enabled: bool) -> None:
        self._set_flag(flag, enabled)

    def _record_invariant_violation(
        self, *, reason: str, event_type: str, instrument_id: str, active_tool: str = "", proposed_stage: str = ""
    ) -> None:
        if reason in BLOCKING_SAFETY_FLAGS:
            self._set_flag(reason, True)
        self._record_event(
            "InvariantViolationIgnored",
            {
                "reason": reason,
                "blocked_event_type": event_type,
                "instrument_id": instrument_id,
                "active_tool": active_tool,
                "proposed_stage": proposed_stage,
            },
        )

    def _record_vlm_proposal_decision(
        self,
        *,
        reducer_result: str,
        reducer_reason: str,
        source: str,
        proposal_id: str,
        instrument_id: str,
        current_stage: str,
        observed_stage: str,
        location_type: str,
        location_id: str,
        confidence: float,
        instance_id: str = "",
    ) -> dict[str, Any]:
        event_type = {
            "accepted": "VLMProposalAccepted",
            "rejected": "VLMProposalRejected",
            "quarantined": "VLMProposalQuarantined",
        }.get(reducer_result, "VLMProposalIgnored")
        payload = {
            "source": source,
            "proposal_id": proposal_id,
            "instrument_id": instrument_id,
            "instance_id": instance_id,
            "current_lifecycle": current_stage,
            "proposed_lifecycle": observed_stage,
            "proposed_transition": f"{current_stage}->{observed_stage}",
            "location_type": location_type,
            "location_id": location_id,
            "confidence": confidence,
            "reducer_result": reducer_result,
            "reducer_reason": reducer_reason,
        }
        self._record_event(event_type, payload)
        return {"event_type": event_type, **payload, "accepted": reducer_result == "accepted"}

    def _record_observation_violation(
        self,
        *,
        reason: str,
        source: str,
        proposal_id: str,
        instrument_id: str,
        current_stage: str,
        observed_stage: str,
        location_type: str,
        location_id: str,
        confidence: float,
        stamp_sec: float,
        instance_id: str = "",
    ) -> dict[str, Any] | None:
        key = (
            instance_id or instrument_id,
            current_stage,
            observed_stage,
            location_type,
        )
        last_seen = self._observation_violation_cooldowns.get(key, -9999.0)
        if stamp_sec - last_seen < 1.0:
            return None
        self._observation_violation_cooldowns[key] = stamp_sec
        return self._record_vlm_proposal_decision(
            reducer_result="rejected",
            reducer_reason=reason,
            source=source,
            proposal_id=proposal_id,
            instrument_id=instrument_id,
            current_stage=current_stage,
            observed_stage=observed_stage,
            location_type=location_type,
            location_id=location_id,
            confidence=confidence,
            instance_id=instance_id,
        )

    def record_mayo_policy_evidence(
        self,
        *,
        instrument_id: str,
        evidence_type: str,
        confidence: float,
        stability_sec: float,
        source: str,
        proposal_id: str,
        stamp_sec: float,
    ) -> dict[str, Any] | None:
        direct_state = self._state_by_instance(instrument_id)
        resolved = (
            direct_state.instrument_id
            if direct_state is not None
            else self.spec.resolve_instrument_alias(instrument_id)
            or instrument_id
        )
        state = self._select_instance(
            resolved,
            preferred_instance_id=(
                direct_state.instance_id if direct_state is not None else ""
            ),
            allowed_lifecycles={
                LIFECYCLE_MAYO_REUSE,
                LIFECYCLE_MAYO_RECOVERY,
            },
        )
        if state is None:
            return self._record_vlm_proposal_decision(
                reducer_result="rejected",
                reducer_reason="mayo_policy_tool_not_on_mayo",
                source=source,
                proposal_id=proposal_id,
                instrument_id=resolved,
                current_stage="not_on_mayo",
                observed_stage="not_on_mayo",
                location_type="mayo_stand",
                location_id="mayo_stand",
                confidence=confidence,
            )
        current_stage = state.lifecycle_stage
        if current_stage not in {LIFECYCLE_MAYO_REUSE, LIFECYCLE_MAYO_RECOVERY}:
            return self._record_vlm_proposal_decision(
                reducer_result="rejected",
                reducer_reason="mayo_policy_tool_not_on_mayo",
                source=source,
                proposal_id=proposal_id,
                instrument_id=resolved,
                current_stage=current_stage,
                observed_stage=current_stage,
                location_type=state.location_type,
                location_id=state.location_id,
                confidence=confidence,
                instance_id=state.instance_id,
            )
        if evidence_type not in {"recover", "reuse"}:
            return self._record_vlm_proposal_decision(
                reducer_result="rejected",
                reducer_reason="unknown_mayo_policy_evidence",
                source=source,
                proposal_id=proposal_id,
                instrument_id=resolved,
                current_stage=current_stage,
                observed_stage=current_stage,
                location_type=state.location_type,
                location_id=state.location_id,
                confidence=confidence,
                instance_id=state.instance_id,
            )

        bounded_confidence = max(0.0, min(1.0, float(confidence)))
        bounded_stability = max(0.0, float(stability_sec))
        if evidence_type == "recover":
            state.mayo_recovery_confidence = bounded_confidence
            state.mayo_recovery_stability_sec = bounded_stability
            state.mayo_reuse_confidence = 0.0
            state.mayo_reuse_stability_sec = 0.0
        else:
            state.mayo_reuse_confidence = bounded_confidence
            state.mayo_reuse_stability_sec = bounded_stability
            state.mayo_recovery_confidence = 0.0
            state.mayo_recovery_stability_sec = 0.0
        state.mayo_evidence_source = source
        state.last_update_sec = max(float(state.last_update_sec), float(stamp_sec))
        result = self._record_vlm_proposal_decision(
            reducer_result="accepted",
            reducer_reason=f"verified_mayo_{evidence_type}_evidence",
            source=source,
            proposal_id=proposal_id,
            instrument_id=resolved,
            current_stage=current_stage,
            observed_stage=current_stage,
            location_type=state.location_type,
            location_id=state.location_id,
            confidence=bounded_confidence,
            instance_id=state.instance_id,
        )
        result["evidence_type"] = evidence_type
        result["stability_sec"] = bounded_stability
        result["procedure_future_use_expected"] = self.procedure_future_use_expected(
            state.instance_id
        )
        return result

    def clear_mayo_policy_evidence(self, instrument_id: str) -> bool:
        direct_state = self._state_by_instance(instrument_id)
        resolved = (
            direct_state.instrument_id
            if direct_state is not None
            else self.spec.resolve_instrument_alias(instrument_id)
            or instrument_id
        )
        state = self._select_instance(
            resolved,
            preferred_instance_id=(
                direct_state.instance_id if direct_state is not None else ""
            ),
            allowed_lifecycles={
                LIFECYCLE_MAYO_REUSE,
                LIFECYCLE_MAYO_RECOVERY,
            },
        )
        if state is None:
            return False
        state.mayo_reuse_confidence = 0.0
        state.mayo_reuse_stability_sec = 0.0
        state.mayo_recovery_confidence = 0.0
        state.mayo_recovery_stability_sec = 0.0
        state.mayo_evidence_source = ""
        return True

    def _set_lifecycle(
        self,
        state: InstrumentBelief,
        lifecycle_stage: str,
        *,
        location_type: str | None = None,
        location_id: str | None = None,
        confidence: float | None = None,
        reserved_for: str | None = None,
        last_update_sec: float | None = None,
        placement_evidence: str | None = None,
    ) -> None:
        previous_stage = state.lifecycle_stage
        state.lifecycle_stage = lifecycle_stage
        if lifecycle_stage in {LIFECYCLE_SURGEON_OWNED, LIFECYCLE_MAYO_REUSE, LIFECYCLE_MAYO_RECOVERY}:
            state.ever_surgeon_owned = True
        if lifecycle_stage == LIFECYCLE_SURGEON_OWNED:
            phase_id = self.state.filtered_phase or self.spec.default_phase_id
            self._phase_instance_interactions[phase_id][
                state.instrument_id
            ].add(state.instance_id)
        if lifecycle_stage in {LIFECYCLE_RECOVERING_LEFT, LIFECYCLE_CLEANING_LEFT, LIFECYCLE_CLEANED_LEFT} and state.last_holder == "surgeon":
            state.ever_surgeon_owned = True

        default_location_type, default_location_id = _location_for_lifecycle(state, lifecycle_stage)
        state.location_type = location_type or default_location_type
        state.location_id = location_id or default_location_id
        state.owner = _owner_for_lifecycle(state)
        state.status = _status_for_lifecycle(state)
        if confidence is not None:
            state.confidence = confidence
        if last_update_sec is not None:
            state.last_update_sec = last_update_sec
        if lifecycle_stage in {LIFECYCLE_MAYO_REUSE, LIFECYCLE_MAYO_RECOVERY}:
            if previous_stage not in {LIFECYCLE_MAYO_REUSE, LIFECYCLE_MAYO_RECOVERY}:
                state.mayo_reuse_confidence = 0.0
                state.mayo_reuse_stability_sec = 0.0
                state.mayo_recovery_confidence = 0.0
                state.mayo_recovery_stability_sec = 0.0
                state.mayo_evidence_source = ""
            if placement_evidence is not None:
                state.mayo_placement_evidence = placement_evidence
        else:
            state.mayo_placement_evidence = ""
            state.mayo_reuse_confidence = 0.0
            state.mayo_reuse_stability_sec = 0.0
            state.mayo_recovery_confidence = 0.0
            state.mayo_recovery_stability_sec = 0.0
            state.mayo_evidence_source = ""

        if lifecycle_stage in {LIFECYCLE_HOME_RACK, LIFECYCLE_PREPOSITIONED_RIGHT}:
            state.cleanliness_state = "sterile"
            state.contaminated = False
        elif lifecycle_stage == LIFECYCLE_RETURNED_HOME:
            state.cleanliness_state = "ready" if state.ever_surgeon_owned else "sterile"
            state.contaminated = False
        elif lifecycle_stage in {LIFECYCLE_SURGEON_OWNED, LIFECYCLE_MAYO_REUSE, LIFECYCLE_MAYO_RECOVERY, LIFECYCLE_RECOVERING_LEFT}:
            state.cleanliness_state = "used"
            state.contaminated = True
            state.last_holder = "surgeon"
        elif lifecycle_stage == LIFECYCLE_DROPPED_FLOOR:
            state.cleanliness_state = "contaminated"
            state.contaminated = True
            state.owner = "none"
            state.status = "requires_human_recovery"
            state.last_holder = "floor"
        elif lifecycle_stage == LIFECYCLE_CLEANING_LEFT:
            state.cleanliness_state = "cleaning"
            state.contaminated = True
            state.last_holder = "surgeon"
        elif lifecycle_stage == LIFECYCLE_CLEANED_LEFT:
            state.cleanliness_state = "ready"
            state.contaminated = False
            state.last_holder = "cleaner"

        if lifecycle_stage == LIFECYCLE_PREPOSITIONED_RIGHT:
            state.last_holder = "robot_right_hand"
            if reserved_for is not None:
                state.reserved_for = reserved_for
        elif lifecycle_stage not in {LIFECYCLE_PREPOSITIONED_RIGHT}:
            state.reserved_for = ""
        self._update_visual_anchor(state)

    def _transition_allowed(self, state: InstrumentBelief, next_stage: str) -> bool:
        allowed_targets = ALLOWED_EVENT_TRANSITIONS.get(state.lifecycle_stage, set())
        return next_stage in allowed_targets

    def _has_strong_return_context(
        self, instrument_or_instance_id: str
    ) -> bool:
        if self._recovery_transaction_active(instrument_or_instance_id):
            return True
        state = self.get_instrument_state(instrument_or_instance_id)
        instrument_id = (
            state.instrument_id if state is not None else instrument_or_instance_id
        )
        instance_id = state.instance_id if state is not None else ""
        request_matches = bool(
            (
                instance_id
                and self.state.surgeon_request_instance_id == instance_id
            )
            or (
                not self.state.surgeon_request_instance_id
                and self.state.surgeon_request_tool == instrument_id
            )
        )
        if (
            request_matches
            and self.state.surgeon_intent in ACTIVE_RETURN_INTENTS
        ):
            return True
        if self.state.surgeon_ready_for_retrieval and (
            not self.state.surgeon_request_tool or request_matches
        ):
            return True
        return False

    def _observation_transition_allowed(
        self,
        *,
        state: InstrumentBelief,
        observed_stage: str,
        source: str,
        confidence: float,
    ) -> tuple[bool, str]:
        current_stage = state.lifecycle_stage
        if observed_stage == current_stage:
            return (True, "")
        if (current_stage, observed_stage) in OBSERVATION_STICKY_DIRECT_BLOCKS:
            return (False, "observation_direct_rebase_forbidden")
        corroborated_mayo_rebase = bool(
            source in CAM4_MAYO_OBSERVATION_SOURCES
            and current_stage
            in {LIFECYCLE_HOME_RACK, LIFECYCLE_RETURNED_HOME}
            and observed_stage == LIFECYCLE_MAYO_REUSE
        )
        if (
            not self._transition_allowed(state, observed_stage)
            and not corroborated_mayo_rebase
        ):
            return (False, "illegal_observation_transition")
        if observed_stage == LIFECYCLE_MAYO_RECOVERY and current_stage in {
            LIFECYCLE_SURGEON_OWNED,
            LIFECYCLE_MAYO_REUSE,
        }:
            if not self._has_strong_return_context(state.instance_id):
                return (False, "observation_recovery_without_return_context")
        if (
            current_stage == LIFECYCLE_SURGEON_OWNED
            and state.location_type in {"surgical_field", "bed_fixed_tool"}
            and observed_stage in {
                LIFECYCLE_MAYO_REUSE,
                LIFECYCLE_MAYO_RECOVERY,
            }
            and not self._has_strong_return_context(state.instance_id)
        ):
            stable_cam4_mayo_return = bool(
                source in CAM4_MAYO_OBSERVATION_SOURCES
                and observed_stage == LIFECYCLE_MAYO_REUSE
                and confidence >= CAM4_MAYO_TRANSITION_MIN_CONFIDENCE
            )
            if not stable_cam4_mayo_return:
                return (
                    False,
                    "field_deployed_tool_requires_explicit_return_context",
                )
        if observed_stage == LIFECYCLE_RETURNED_HOME and current_stage == LIFECYCLE_SURGEON_OWNED:
            return (False, "observation_direct_home_snap_forbidden")
        return (True, "")

    def _required_observation_streak(
        self,
        *,
        state: InstrumentBelief,
        observed_stage: str,
        confidence: float,
        source: str,
    ) -> int:
        if observed_stage == state.lifecycle_stage:
            return 1
        if (
            source in CAM4_MAYO_OBSERVATION_SOURCES
            and observed_stage == LIFECYCLE_MAYO_REUSE
        ):
            return 1
        if confidence >= 0.97:
            return 1
        if observed_stage in {LIFECYCLE_SURGEON_OWNED, LIFECYCLE_MAYO_REUSE, LIFECYCLE_MAYO_RECOVERY}:
            return 2
        return 1

    def _clear_observation_candidate(self, instance_id: str) -> None:
        self._observation_candidates.pop(instance_id, None)

    def _observation_candidate_ready(
        self,
        *,
        state: InstrumentBelief,
        observed_stage: str,
        location_type: str,
        location_id: str,
        confidence: float,
        stamp_sec: float,
        source: str,
    ) -> bool:
        required = self._required_observation_streak(
            state=state,
            observed_stage=observed_stage,
            confidence=confidence,
            source=source,
        )
        if required <= 1:
            return True
        candidate = self._observation_candidates.get(state.instance_id)
        if (
            candidate
            and candidate["stage"] == observed_stage
            and candidate["location_type"] == location_type
            and candidate["location_id"] == location_id
            and stamp_sec - float(candidate["last_seen"]) <= 3.5
        ):
            candidate["count"] = int(candidate["count"]) + 1
            candidate["last_seen"] = stamp_sec
        else:
            self._observation_candidates[state.instance_id] = {
                "stage": observed_stage,
                "location_type": location_type,
                "location_id": location_id,
                "count": 1,
                "first_seen": stamp_sec,
                "last_seen": stamp_sec,
            }
            return False
        return int(candidate["count"]) >= required

    def _apply_event_transition(
        self,
        *,
        state: InstrumentBelief,
        next_stage: str,
        event_type: str,
        location_type: str | None = None,
        location_id: str | None = None,
        confidence: float | None = None,
        reserved_for: str | None = None,
        placement_evidence: str | None = None,
    ) -> bool:
        if not self._transition_allowed(state, next_stage):
            self._record_invariant_violation(
                reason="illegal_lifecycle_transition",
                event_type=event_type,
                instrument_id=state.instrument_id,
                proposed_stage=next_stage,
            )
            return False
        self._set_lifecycle(
            state,
            next_stage,
            location_type=location_type,
            location_id=location_id,
            confidence=confidence,
            reserved_for=reserved_for,
            last_update_sec=getattr(self, "_current_event_stamp_sec", None),
            placement_evidence=placement_evidence,
        )
        self._clear_observation_candidate(state.instance_id)
        return True

    def _apply_observation_rebase(
        self,
        *,
        state: InstrumentBelief,
        location_type: str,
        location_id: str,
        confidence: float,
        stamp_sec: float,
    ) -> None:
        observed_stage = _observed_lifecycle_for_location(state, location_type, location_id)
        self._set_lifecycle(
            state,
            observed_stage,
            location_type=location_type,
            location_id=location_id,
            confidence=confidence,
            placement_evidence=(
                "public_visual_observation"
                if observed_stage
                in {LIFECYCLE_MAYO_REUSE, LIFECYCLE_MAYO_RECOVERY}
                else None
            ),
        )
        state.last_update_sec = stamp_sec
        self._clear_observation_candidate(state.instance_id)

    def _clear_satisfied_request(self) -> None:
        self._sync_active_request_from_queue()
        requested_tool = self.state.surgeon_request_tool or self.state.explicit_request_tool
        if not requested_tool:
            return
        requested_state = self.get_instrument_state(
            self.state.surgeon_request_instance_id or requested_tool
        )
        if requested_state is None:
            return

        if (
            self.state.surgeon_intent in ACTIVE_REQUEST_INTENTS
            and requested_state.lifecycle_stage == LIFECYCLE_SURGEON_OWNED
        ):
            self._dequeue_active_request("requested_tool_handed_over")
            return

        if self.state.surgeon_intent in ACTIVE_RETURN_INTENTS and requested_state.lifecycle_stage in {
            LIFECYCLE_RECOVERING_LEFT,
            LIFECYCLE_CLEANING_LEFT,
            LIFECYCLE_CLEANED_LEFT,
            LIFECYCLE_RETURNED_HOME,
        }:
            self._dequeue_active_request("requested_tool_retrieved")

    def _derive_next_required_transition(self, state: InstrumentBelief) -> str:
        if (
            self._is_active_requested_tool(state.instance_id)
            and self.state.surgeon_intent in ACTIVE_REQUEST_INTENTS
            and state.lifecycle_stage in {LIFECYCLE_MAYO_REUSE, LIFECYCLE_MAYO_RECOVERY}
            and not self._recovery_transaction_active(state.instance_id)
        ):
            return ""
        if state.lifecycle_stage == LIFECYCLE_PREPOSITIONED_RIGHT:
            if self.state.execution_state in {"finishing", "completed"}:
                return "return_unused_preposition"
            requested_tool = self.state.surgeon_request_tool or self.state.explicit_request_tool
            if requested_tool and requested_tool != state.instrument_id:
                return "return_unused_preposition"
            if self.state.right_hand_tool and self.state.right_hand_tool != state.instrument_id:
                return "return_unused_preposition"
            return ""
        if state.lifecycle_stage == LIFECYCLE_SURGEON_OWNED:
            if self.state.execution_state == "finishing":
                return "recover_left"
            if self._recovery_transaction_active(state.instance_id):
                return "recover_left"
            if (
                self.state.surgeon_intent in ACTIVE_RETURN_INTENTS
                and (
                    self.state.surgeon_request_instance_id == state.instance_id
                    or (
                        not self.state.surgeon_request_instance_id
                        and self.state.surgeon_request_tool
                        == state.instrument_id
                    )
                )
            ):
                return "recover_left"
            return ""
        if state.lifecycle_stage == LIFECYCLE_MAYO_REUSE:
            if self._recovery_transaction_active(state.instance_id):
                return "recover_left"
            return ""
        if state.lifecycle_stage == LIFECYCLE_MAYO_RECOVERY:
            return "recover_left"
        if state.lifecycle_stage == LIFECYCLE_DROPPED_FLOOR:
            return "human_recovery_required"
        if state.lifecycle_stage == LIFECYCLE_RECOVERING_LEFT:
            return "clean_left" if state.contaminated else "return_home"
        if state.lifecycle_stage == LIFECYCLE_CLEANING_LEFT:
            return "clean_left"
        if state.lifecycle_stage == LIFECYCLE_CLEANED_LEFT:
            return "return_home"
        return ""

    def _recompute_transient_state(self) -> None:
        self._refresh_active_robot_task()
        self._normalize_surgeon_hand_conflicts()
        self._normalize_cleaner_conflicts()
        for instance_id in list(
            self.state.active_recovery_tool_instances
        ):
            state = self._state_by_instance(instance_id)
            if state is None or state.lifecycle_stage in {LIFECYCLE_HOME_RACK, LIFECYCLE_RETURNED_HOME}:
                self._close_recovery_transaction(
                    instance_id, "terminal_lifecycle_observed"
                )
        right_candidates = [
            state
            for state in self.instrument_states.values()
            if state.lifecycle_stage in RIGHT_HAND_LIFECYCLES
        ]
        left_candidates = [
            state
            for state in self.instrument_states.values()
            if state.lifecycle_stage in LEFT_HAND_LIFECYCLES
        ]

        right_state = max(
            right_candidates, key=lambda state: state.last_update_sec or 0.0, default=None
        )
        left_state = max(
            left_candidates, key=lambda state: state.last_update_sec or 0.0, default=None
        )
        self.state.right_hand_tool = (
            right_state.instrument_id if right_state else ""
        )
        self.state.right_hand_tool_instance_id = (
            right_state.instance_id if right_state else ""
        )
        self.state.left_hand_tool = (
            left_state.instrument_id if left_state else ""
        )
        self.state.left_hand_tool_instance_id = (
            left_state.instance_id if left_state else ""
        )
        self.state.prepositioned_tool = self.state.right_hand_tool if right_candidates else ""
        self.state.prepositioned_tool_instance_id = (
            self.state.right_hand_tool_instance_id if right_candidates else ""
        )
        self.state.cleaner_busy = any(
            state.lifecycle_stage == LIFECYCLE_CLEANING_LEFT for state in self.instrument_states.values()
        )
        if not self.state.cleaner_busy:
            self.state.cleaner_remaining_sec = 0.0
        dropped_floor_tools = [
            state.instrument_id
            for state in self.instrument_states.values()
            if state.lifecycle_stage == LIFECYCLE_DROPPED_FLOOR
        ]
        self._set_flag("dropped_tool_requires_human", bool(dropped_floor_tools))
        pending_tools: list[str] = []
        for state in self.instrument_states.values():
            state.next_required_transition = self._derive_next_required_transition(state)
            if state.next_required_transition:
                pending_tools.append(state.instrument_id)
        self.state.pending_transition_tools = pending_tools

        right_conflict = len(right_candidates) > 1
        left_conflict = len(left_candidates) > 1
        self._set_flag("right_arm_overloaded", right_conflict)
        self._set_flag("left_arm_overloaded", left_conflict)
        surgeon_owned_count = len(self._surgeon_owned_hand_states())
        surgeon_owned_overloaded = surgeon_owned_count > 2
        was_surgeon_overloaded = "surgeon_owned_overloaded" in self.state.safety_flags
        self._set_flag("surgeon_owned_overloaded", surgeon_owned_overloaded)
        if surgeon_owned_overloaded and not was_surgeon_overloaded:
            self._record_event(
                "InvariantViolationIgnored",
                {
                    "reason": "surgeon_owned_overloaded",
                    "surgeon_owned_count": surgeon_owned_count,
                },
            )
        self._clear_satisfied_request()
        self._validate_inventory_invariants()
        self._normalize_robot_state()
        self._complete_if_cleanup_finished()

    def _normalize_surgeon_hand_conflicts(self) -> None:
        hand_states = self._surgeon_owned_hand_states()
        if len(hand_states) <= 2:
            return
        already_reported = (
            "surgeon_owned_overloaded" in self.state.safety_flags
        )
        self._set_flag("surgeon_owned_overloaded", True)
        if already_reported:
            return

        self._record_event(
            "StateInvariantViolation",
            {
                "reason": "surgeon_hand_capacity_exceeded",
                "surgeon_hand_tools": [
                    state.instrument_id for state in hand_states
                ],
                "surgeon_hand_instances": [
                    state.instance_id for state in hand_states
                ],
                "active_request_tool": self._active_requested_tool_id(),
                "active_request_instance_id": (
                    self._active_requested_instance_id()
                ),
                "policy": "fail_closed_without_invented_mayo_placement",
            },
        )

    def _validate_inventory_invariants(self) -> None:
        expected_counts = self.spec.get_tool_inventory()
        actual_counts = Counter(
            state.instrument_id
            for state in self.instrument_states.values()
        )
        instance_ids = [
            state.instance_id for state in self.instrument_states.values()
        ]
        duplicate_instance_ids = sorted(
            instance_id
            for instance_id, count in Counter(instance_ids).items()
            if count > 1
        )
        key_mismatches = sorted(
            key
            for key, state in self.instrument_states.items()
            if key != state.instance_id
        )
        count_mismatches = {
            tool_id: {
                "expected": int(expected_counts.get(tool_id, 0)),
                "actual": int(actual_counts.get(tool_id, 0)),
            }
            for tool_id in sorted(
                set(expected_counts) | set(actual_counts)
            )
            if int(expected_counts.get(tool_id, 0))
            != int(actual_counts.get(tool_id, 0))
        }
        violation = bool(
            duplicate_instance_ids or key_mismatches or count_mismatches
        )
        self._set_flag("duplicate_tool_holder", violation)
        signature = (
            tuple(duplicate_instance_ids),
            tuple(key_mismatches),
            tuple(
                (
                    tool_id,
                    values["expected"],
                    values["actual"],
                )
                for tool_id, values in count_mismatches.items()
            ),
        )
        if violation and signature != self._inventory_violation_signature:
            self._inventory_violation_signature = signature
            self._record_event(
                "StateInvariantViolation",
                {
                    "reason": "instrument_inventory_invariant_failed",
                    "duplicate_instance_ids": duplicate_instance_ids,
                    "key_mismatches": key_mismatches,
                    "count_mismatches": count_mismatches,
                },
            )
        elif not violation:
            self._inventory_violation_signature = None

    def _normalize_cleaner_conflicts(self) -> None:
        cleaner_states = [
            state
            for state in self.instrument_states.values()
            if state.lifecycle_stage == LIFECYCLE_CLEANING_LEFT
        ]
        if len(cleaner_states) <= 1:
            return
        active_task_instance = (
            self.state.active_robot_task.instrument_instance_id
            if self.state.active_robot_task is not None
            else ""
        )
        cleaner_states.sort(
            key=lambda state: (
                0 if state.instance_id == active_task_instance else 1,
                state.last_update_sec or 0.0,
                state.instance_id,
            )
        )
        kept_state = cleaner_states[0]
        for queued_state in cleaner_states[1:]:
            self._set_lifecycle(
                queued_state,
                LIFECYCLE_RECOVERING_LEFT,
                location_type="robot_left_hand",
                location_id="robot_left_hand",
                confidence=max(queued_state.confidence, 0.9),
            )
            self._record_event(
                "CleanerConflictQueued",
                {
                    "instrument_id": queued_state.instrument_id,
                    "instance_id": queued_state.instance_id,
                    "kept_tool": kept_state.instrument_id,
                    "kept_instance_id": kept_state.instance_id,
                    "reason": "cleaner_single_tool_invariant",
                },
            )
            self._open_recovery_transaction(
                queued_state.instance_id,
                "cleaner_single_tool_invariant",
            )

    def _normalize_robot_state(self) -> None:
        if self.state.robot_state == "fault":
            return
        task = self.state.active_robot_task
        if task is not None and task.task_id:
            if task.task_type in {"insert_into_cleaner", "cleaning_hold"}:
                self.state.robot_state = "cleaning"
            elif task.task_type == "auto_return_to_rack":
                self.state.robot_state = "returning_home"
            elif task.task_type == "move_to_handover":
                self.state.robot_state = "handover_in_progress"
            elif task.task_type == "receive_from_recovery_zone":
                self.state.robot_state = "recovery_in_progress"
            elif task.task_type == "pick_from_rack":
                self.state.robot_state = "picking"
            else:
                self.state.robot_state = "busy"
            return
        if self.state.cleaner_busy:
            self.state.robot_state = "cleaning"
            return
        left_state = self._state_by_instance(
            self.state.left_hand_tool_instance_id
        )
        if left_state is not None:
            self.state.robot_state = "busy"
            return
        if any(state.lifecycle_stage == LIFECYCLE_CLEANED_LEFT for state in self.instrument_states.values()):
            self.state.robot_state = "ready_to_return"
            return
        right_state = self._state_by_instance(
            self.state.right_hand_tool_instance_id
        )
        if right_state is not None:
            self.state.robot_state = "handover_ready"
            return
        if self.state.phase_uncertain and self.state.robot_state == "retracted":
            return
        self.state.robot_state = "idle"

    def _right_arm_conflict(self, instance_id: str) -> bool:
        self._recompute_transient_state()
        return bool(
            self.state.right_hand_tool_instance_id
            and self.state.right_hand_tool_instance_id != instance_id
        )

    def _left_arm_conflict(self, instance_id: str) -> bool:
        self._recompute_transient_state()
        return bool(
            self.state.left_hand_tool_instance_id
            and self.state.left_hand_tool_instance_id != instance_id
        )

    def _surgeon_hand_conflict(self, instance_id: str) -> str:
        hand_states = [
            state
            for candidate_id, state in self.instrument_states.items()
            if candidate_id != instance_id
            and state.lifecycle_stage == LIFECYCLE_SURGEON_OWNED
            and (state.location_type == "surgeon_hand" or state.status == "handed_over")
        ]
        if len(hand_states) < 2:
            return ""
        hand_states.sort(key=lambda state: state.last_update_sec or 0.0)
        return hand_states[0].instance_id

    def update_explicit_request(self, request_text: str) -> str:
        if not request_text.strip():
            self._record_event("ExplicitRequestUpdated", {"text": request_text, "resolved_tool": ""})
            return ""
        resolved = self.resolve_explicit_voice_tool_request(request_text)
        if resolved:
            first_request_is_additional = self._enqueue_surgeon_request(
                event_type="voice_request",
                instrument_id=resolved,
                voice_text=request_text,
                note="explicit voice text resolved to request cue",
                ready_for_handover=True,
            )
            if (
                not first_request_is_additional
                and self._allow_shadow_type_instance_requests
                and _requests_additional_instance(request_text)
                and _instrument_mention_count(
                    self.spec,
                    resolved,
                    request_text,
                )
                >= 2
            ):
                self._enqueue_surgeon_request(
                    event_type="voice_request",
                    instrument_id=resolved,
                    voice_text=request_text,
                    note=(
                        "repeated public tool mention with additional-instance cue"
                    ),
                    ready_for_handover=True,
                    force_shadow_additional_instance_assumption=True,
                )
        self._recompute_transient_state()
        self._record_event("ExplicitRequestUpdated", {"text": request_text, "resolved_tool": resolved})
        return resolved

    def explicit_request_voice_backed(self) -> bool:
        cue = self._active_request_cue()
        if cue is None or cue.event_type not in ACTIVE_REQUEST_INTENTS:
            return False
        return bool(cue.event_type == "voice_request" or cue.voice_text.strip())

    def is_explicit_procedure_completion_request(self, request_text: str) -> bool:
        text = re.sub(
            r"\s+",
            " ",
            str(request_text or "").strip().lower(),
        )
        text = re.sub(r"[.!?]+$", "", text).strip()
        if not text:
            return False
        return bool(
            re.fullmatch(
                r"(?:네\s+)?(?:수술\s+)?"
                r"(?:마치겠습니다|마칩니다|마무리하겠습니다|"
                r"끝났습니다|종료하겠습니다)",
                text,
            )
            or re.fullmatch(
                r"(?:(?:okay|ok|yes|all right)\s+)?"
                r"(?:(?:the\s+)?(?:procedure|surgery)\s+)?"
                r"(?:(?:is|has been)\s+)?"
                r"(?:complete|completed|finished|done)(?:\s+now)?",
                text,
            )
        )

    def is_explicit_voice_tool_request(self, request_text: str) -> bool:
        return bool(self.resolve_explicit_voice_tool_request(request_text))

    def resolve_explicit_voice_tool_request(self, request_text: str) -> str:
        text = str(request_text or "").strip()
        if not text:
            return ""

        signature = re.sub(r"[^0-9a-z가-힣]+", "", text.lower())
        group_specs = [
            *self.spec.get_bed_robot_arm_group_cues(),
            *self.spec.get_bed_robot_arm_end_effector_transitions(),
        ]
        for group_spec in group_specs:
            for utterance in group_spec.utterances:
                candidate = re.sub(r"[^0-9a-z가-힣]+", "", str(utterance).lower())
                if signature and signature == candidate:
                    return ""

        lowered = text.lower()
        control_match = (
            _PROCEDURE_CONTROL_PATTERN.search(text)
            if _has_procedure_reference(self.spec, text)
            else None
        )
        if control_match is not None:
            # A public sentence may contain both a procedure-control clause and
            # a terse instrument request. Only the text outside the procedure
            # clause can name the requested tool.
            suffix = text[control_match.end() :].strip()
            resolved = _resolve_request_tool_with_asr_fallback(
                self.spec,
                suffix,
                reject_procedure_modifiers=True,
            )
            if resolved:
                return resolved

            prefix = text[: control_match.start()].strip()
            if _has_handover_request_marker(prefix):
                return _resolve_request_tool(
                    self.spec,
                    prefix,
                    reject_procedure_modifiers=True,
                )
            return ""

        resolved = _resolve_request_tool_with_asr_fallback(self.spec, text)
        if not resolved:
            return ""
        if _has_non_handover_tool_action(text):
            return _resolve_compound_handover_tool(self.spec, text)
        if _has_handover_request_marker(text):
            return resolved

        if _is_bare_procedure_name_tool_alias(self.spec, text, resolved):
            return ""

        word_count = len(_lexical_tokens(lowered))
        return resolved if 0 < word_count <= 6 else ""

    def update_surgeon_request(self, request: SurgeonRequest) -> str:
        resolved = self.spec.resolve_instrument_alias(request.requested_tool) or request.requested_tool
        if not resolved and request.voice_text:
            for token in _normalize_request_text(request.voice_text):
                resolved = self.spec.resolve_instrument_alias(token) or ""
                if resolved:
                    break

        if request.event_type == "cancel_request":
            previous_queue = [cue.instrument_id for cue in self.state.surgeon_request_queue]
            previous_active = self.state.surgeon_request_tool
            self._clear_surgeon_request_state()
            self.state.surgeon_intent = "idle"
            self._record_event(
                "SurgeonRequestQueueCleared",
                {
                    "requested_tool": resolved,
                    "voice_text": request.voice_text,
                    "previous_queue": previous_queue,
                    "previous_active_request_tool": previous_active,
                    "override": bool(request.override),
                    "reason": request.note or "cancel_request",
                },
            )
        elif request.event_type == "request_procedure_completion":
            self._begin_completion_cleanup()
        elif request.event_type == "complete_procedure":
            if self._cleanup_still_pending():
                self._begin_completion_cleanup()
            else:
                self._mark_completed()
        elif request.event_type in ACTIVE_REQUEST_INTENTS:
            self._enqueue_surgeon_request(
                event_type=request.event_type or "request_tool",
                instrument_id=resolved,
                voice_text=request.voice_text,
                note=request.note,
                ready_for_handover=bool(request.ready_for_handover or resolved),
                ready_for_retrieval=False,
                override=bool(request.override),
            )
        else:
            self.state.surgeon_intent = request.event_type or self.state.surgeon_intent
            self.state.surgeon_request_tool = resolved
            self.state.surgeon_ready_for_handover = bool(request.ready_for_handover)
            self.state.surgeon_ready_for_retrieval = bool(request.ready_for_retrieval)
            if request.event_type in ACTIVE_RETURN_INTENTS:
                self._open_recovery_transaction(resolved, "surgeon_request_return_tool")

        self._recompute_transient_state()
        self._record_event(
            "SurgeonRequestUpdated",
            {
                "event_type": request.event_type,
                "requested_tool": resolved,
                "voice_text": request.voice_text,
                "override": bool(request.override),
            },
        )
        return resolved

    def apply_surgeon_actor_event(self, event: SurgeonActorEvent) -> None:
        event_type = (event.event_type or "").strip()
        direct_state = self._state_by_instance(event.tool_id)
        tool_id = (
            direct_state.instrument_id
            if direct_state is not None
            else self.spec.resolve_instrument_alias(event.tool_id)
            or event.tool_id
        )
        phase_id = event.phase_id or self.state.filtered_phase
        allowed_lifecycles = (
            {
                LIFECYCLE_SURGEON_OWNED,
                LIFECYCLE_MAYO_REUSE,
                LIFECYCLE_MAYO_RECOVERY,
            }
            if event_type
            in {
                "return_tool",
                "place_on_mayo",
                "place_on_mayo_reuse",
                "place_on_mayo_recovery",
                "continue_using",
            }
            else None
        )
        state = (
            self._select_instance(
                tool_id,
                preferred_instance_id=(
                    direct_state.instance_id
                    if direct_state is not None
                    else self.state.surgeon_request_instance_id
                ),
                allowed_lifecycles=allowed_lifecycles,
            )
            if tool_id
            else None
        )

        if event_type in {"request_tool", "voice_request"}:
            # /surgeon/request is the canonical cue source. Actor events are kept
            # as observability signals so the same mock decision is not queued twice.
            self._sync_active_request_from_queue()
        elif event_type == "return_tool":
            self.state.surgeon_intent = event_type
            self.state.surgeon_request_tool = tool_id
            self.state.surgeon_request_instance_id = (
                state.instance_id if state is not None else ""
            )
            self.state.surgeon_ready_for_handover = False
            self.state.surgeon_ready_for_retrieval = bool(event.ready_for_retrieval or tool_id)
            if state is not None:
                self._open_recovery_transaction(
                    state.instance_id, "surgeon_actor_return_tool"
                )
        elif event_type == "cancel_request":
            self._record_event(
                "LegacyCancelActorEventIgnored",
                {
                    "instrument_id": tool_id,
                    "queue_length": len(self.state.surgeon_request_queue),
                    "active_request_tool": self.state.surgeon_request_tool,
                },
            )
        elif event_type in {"advance_phase", "advance_phase_cue"}:
            cue = PhaseTransitionCue()
            cue.stamp = event.stamp
            cue.source = "mock_surgeon"
            cue.cue_id = f"surgeon:{self.state.filtered_phase}->{phase_id}:{_stamp_to_sec(event.stamp):.3f}"
            cue.current_phase = self.state.filtered_phase
            cue.target_phase = phase_id
            cue.confidence = 0.96
            cue.reason = event.note or event_type
            self.apply_phase_transition_cue(cue)
        elif event_type == "field_event":
            self._set_active_interrupt_context(phase_id, "surgeon_actor_field_event")
        elif event_type == "field_event_resolved":
            self._clear_active_interrupt_context()
            self._record_event(
                "ActiveInterruptContextCleared",
                {
                    "phase_id": phase_id,
                    "reason": "surgeon_actor_field_event_resolved",
                },
            )
        elif event_type == "request_procedure_completion":
            self._begin_completion_cleanup()
        elif event_type == "complete_procedure":
            if self._cleanup_still_pending():
                self._begin_completion_cleanup()
            else:
                self._mark_completed()
        elif event_type in {"human_recovered_dropped_tool", "human_recovered_floor_tool"} and state is not None:
            if state.lifecycle_stage != LIFECYCLE_DROPPED_FLOOR:
                self._record_event(
                    "HumanRecoveryIgnored",
                    {
                        "instrument_id": tool_id,
                        "reason": "tool_not_in_floor_drop_state",
                        "lifecycle_stage": state.lifecycle_stage,
                    },
                )
            elif self._apply_event_transition(
                state=state,
                next_stage=LIFECYCLE_RETURNED_HOME,
                event_type=event_type,
                location_type=state.home_location_type,
                location_id=state.home_location_id,
                confidence=max(state.confidence, 0.96),
            ):
                self.state.surgeon_intent = "human_recovered_dropped_tool"
                self._record_event(
                    "DroppedToolHumanRecovered",
                    {
                        "instrument_id": tool_id,
                        "target_stage": LIFECYCLE_RETURNED_HOME,
                        "reason": event.note or "human_removed_dropped_tool_and_replaced_sterile_equivalent",
                    },
                )
        elif event_type in SURGEON_ACTOR_LOCATION_EVENTS and state is not None:
            location_type, location_id, next_stage = SURGEON_ACTOR_LOCATION_EVENTS[event_type]
            reuse_tool_marked_for_recovery = (
                event_type == "place_on_mayo_recovery"
                and state.lifecycle_stage == LIFECYCLE_MAYO_REUSE
            )
            if reuse_tool_marked_for_recovery:
                self.state.surgeon_intent = "return_tool"
                self.state.surgeon_ready_for_handover = False
                self.state.surgeon_ready_for_retrieval = True
                self._open_recovery_transaction(
                    state.instance_id,
                    "surgeon_actor_place_on_mayo_recovery",
                )
            else:
                if not self._apply_event_transition(
                    state=state,
                    next_stage=next_stage,
                    event_type=event_type,
                    location_type=location_type,
                    location_id=location_id,
                    confidence=max(state.confidence, 0.96),
                    placement_evidence=(
                        "public_surgeon_event"
                        if next_stage
                        in {
                            LIFECYCLE_MAYO_REUSE,
                            LIFECYCLE_MAYO_RECOVERY,
                        }
                        else None
                    ),
                ):
                    return
                if event_type == "continue_using":
                    self.state.surgeon_intent = "continue_using"
                    self.state.surgeon_ready_for_handover = False
                    self.state.surgeon_ready_for_retrieval = False
                elif event_type in {"place_on_mayo", "place_on_mayo_reuse"}:
                    if self._active_request_cue() is not None:
                        # Keep the active requested tool alive after parking another tool on Mayo.
                        # This is used when surgeon hand is full and a retrieval-choice selector
                        # chooses one currently held tool to free hand capacity.
                        self._sync_active_request_from_queue()
                    else:
                        self.state.surgeon_intent = "park_for_reuse"
                        self.state.surgeon_ready_for_handover = False
                        self.state.surgeon_ready_for_retrieval = False
                elif event_type == "place_on_mayo_recovery":
                    self.state.surgeon_intent = "return_tool"
                    self.state.surgeon_ready_for_handover = False
                    self.state.surgeon_ready_for_retrieval = True
                    self._open_recovery_transaction(
                        state.instance_id,
                        "surgeon_actor_place_on_mayo_recovery",
                    )

        self._recompute_transient_state()
        self._record_event(
            "SurgeonActorEventApplied",
            {
                "event_type": event_type,
                "instrument_id": tool_id,
                "instance_id": (
                    state.instance_id if state is not None else ""
                ),
                "phase_id": phase_id,
                "override": bool(event.override),
                "voice_text": event.voice_text,
                "note": event.note,
            },
        )

    def update_phase(self, filtered_phase: FilteredPhase) -> None:
        phase_id = filtered_phase.phase_id or self.state.filtered_phase
        if phase_id not in self.spec.phase_ids:
            self._record_invariant_violation(
                reason="phase_out_of_bundle_scope",
                event_type="PhaseUpdated",
                instrument_id="",
                proposed_stage=phase_id,
            )
            phase_id = self.state.filtered_phase if self.state.filtered_phase in self.spec.phase_ids else self.spec.default_phase_id
        current_phase = (
            self.state.filtered_phase
            if self.state.filtered_phase in self.spec.phase_ids
            else self.spec.default_phase_id
        )
        target_index = self._normal_phase_index(phase_id)
        current_index = self._normal_phase_index(current_phase)
        if (
            target_index >= 0
            and (
                target_index < self._initial_phase_floor_index
                or (current_index >= 0 and target_index < current_index)
            )
        ):
            self._record_phase_decision(
                target_phase=phase_id,
                accepted=False,
                reason="phase_regression_below_start_floor",
                confidence=float(filtered_phase.confidence),
                current_phase=current_phase,
            )
            return
        self.state.filtered_phase = phase_id
        self.state.phase_confidence = float(filtered_phase.confidence)
        self.state.phase_uncertain = bool(filtered_phase.uncertain)
        self.state.phase_stability = float(filtered_phase.stability)
        self._relocate_phase_field_deployed_tools(
            phase_id,
            reason="filtered_phase_updated",
        )
        if not self.state.phase_uncertain and self.state.robot_state == "retracted":
            self.state.robot_state = "idle"
        self._record_event(
            "PhaseUpdated",
            {
                "phase_id": self.state.filtered_phase,
                "confidence": self.state.phase_confidence,
                "uncertain": self.state.phase_uncertain,
            },
        )
        self._recompute_transient_state()

    def reconcile_observation(
        self,
        observation: ToolObservation,
        *,
        source: str = "legacy_tool_observation",
        proposal_id: str = "",
    ) -> dict[str, Any] | None:
        if not observation.visible:
            return None
        direct_state = self._state_by_instance(observation.instrument_id)
        instrument_id = (
            direct_state.instrument_id
            if direct_state is not None
            else self.spec.resolve_instrument_alias(
                observation.instrument_id
            )
            or observation.instrument_id
        )
        location_type = observation.location_type
        preferred_lifecycles: set[str] | None = None
        if location_type in {
            "mayo_stand",
            "mayo_reuse_zone",
            "mayo_recovery_zone",
        }:
            preferred_lifecycles = {
                LIFECYCLE_MAYO_REUSE,
                LIFECYCLE_MAYO_RECOVERY,
            }
        elif location_type in SURGEON_OWNED_LOCATION_TYPES:
            preferred_lifecycles = {
                LIFECYCLE_SURGEON_OWNED,
                LIFECYCLE_PREPOSITIONED_RIGHT,
            }
        current = (
            direct_state
            if direct_state is not None
            else self._select_instance(
                instrument_id,
                allowed_lifecycles=preferred_lifecycles,
            )
        )
        if current is None and preferred_lifecycles is not None:
            current = self._select_instance(instrument_id)
        if current is None:
            return None
        location_type = observation.location_type or current.location_type
        location_id = observation.location_id or current.location_id
        confidence = float(observation.confidence)
        stamp_sec = _stamp_to_sec(observation.stamp)
        proposal_id = proposal_id or (
            f"{source}:{current.instance_id}:{location_type}:"
            f"{location_id}:{stamp_sec:.3f}"
        )
        observed_stage = _observed_lifecycle_for_location(current, location_type, location_id)

        shadow_locked_instances = {
            instance_id
            for instance_id in self._shadow_counterfactual_locked_instances
            if (
                (locked_state := self._state_by_instance(instance_id))
                is not None
                and locked_state.instrument_id == instrument_id
            )
        }
        if (
            source in CAM4_MAYO_OBSERVATION_SOURCES
            and shadow_locked_instances
        ):
            self._clear_observation_candidate(current.instance_id)
            return self._record_vlm_proposal_decision(
                reducer_result="quarantined",
                reducer_reason="shadow_counterfactual_branch_conflict",
                source=source,
                proposal_id=proposal_id,
                instrument_id=instrument_id,
                current_stage=current.lifecycle_stage,
                observed_stage=observed_stage,
                location_type=location_type,
                location_id=location_id,
                confidence=confidence,
                instance_id=current.instance_id,
            )

        allowed, violation_reason = self._observation_transition_allowed(
            state=current,
            observed_stage=observed_stage,
            source=source,
            confidence=confidence,
        )
        if not allowed:
            result = self._record_observation_violation(
                reason=violation_reason,
                source=source,
                proposal_id=proposal_id,
                instrument_id=instrument_id,
                current_stage=current.lifecycle_stage,
                observed_stage=observed_stage,
                location_type=location_type,
                location_id=location_id,
                confidence=confidence,
                stamp_sec=stamp_sec,
                instance_id=current.instance_id,
            )
            self._clear_observation_candidate(current.instance_id)
            return result

        if not self._observation_candidate_ready(
            state=current,
            observed_stage=observed_stage,
            location_type=location_type,
            location_id=location_id,
            confidence=confidence,
            stamp_sec=stamp_sec,
            source=source,
        ):
            return self._record_vlm_proposal_decision(
                reducer_result="quarantined",
                reducer_reason="awaiting_observation_hysteresis",
                source=source,
                proposal_id=proposal_id,
                instrument_id=instrument_id,
                current_stage=current.lifecycle_stage,
                observed_stage=observed_stage,
                location_type=location_type,
                location_id=location_id,
                confidence=confidence,
                instance_id=current.instance_id,
            )

        previous_stage = current.lifecycle_stage
        self._apply_observation_rebase(
            state=current,
            location_type=location_type,
            location_id=location_id,
            confidence=confidence,
            stamp_sec=stamp_sec,
        )
        self._recompute_transient_state()
        return self._record_vlm_proposal_decision(
            reducer_result="accepted",
            reducer_reason="legal_observation_transition" if previous_stage != current.lifecycle_stage else "state_already_consistent",
            source=source,
            proposal_id=proposal_id,
            instrument_id=instrument_id,
            current_stage=previous_stage,
            observed_stage=current.lifecycle_stage,
            location_type=current.location_type,
            location_id=current.location_id,
            confidence=confidence,
            instance_id=current.instance_id,
        )

    def _resolve_event_state(
        self, event: TwinEvent, detail: dict[str, Any]
    ) -> InstrumentBelief | None:
        raw_instrument_id = str(event.instrument_id or "")
        direct_instance_id = str(
            getattr(event, "instance_id", "")
            or detail.get("instrument_instance_id", "")
            or detail.get("instance_id", "")
        )
        direct = self._state_by_instance(direct_instance_id)
        if direct is not None:
            if not raw_instrument_id or direct.instrument_id == raw_instrument_id:
                return direct

        resolved_type = (
            self.spec.resolve_instrument_alias(raw_instrument_id)
            or raw_instrument_id.partition("#")[0]
        )
        if not resolved_type:
            return None

        task = self.state.active_robot_task
        if (
            task is not None
            and task.instrument_instance_id
            and task.instrument_id == resolved_type
        ):
            task_state = self._state_by_instance(task.instrument_instance_id)
            if task_state is not None:
                return task_state

        request_instance_id = self.state.surgeon_request_instance_id
        if (
            request_instance_id
            and self.state.surgeon_request_tool == resolved_type
            and event.event_type
            in {
                "RobotTaskStarted",
                "RobotGraspedTool",
                "ToolPrepared",
                "ToolHandoverCompleted",
                "ShadowAdditionalToolHandoverCompleted",
            }
        ):
            request_state = self._state_by_instance(request_instance_id)
            if request_state is not None:
                return request_state

        if event.event_type in {
            "ToolReceivedFromSurgeon",
            "ToolRetrievedFromMayo",
            "ToolSentToCleaner",
            "ToolCleaningProgress",
            "ToolCleaningCompleted",
            "ToolReturnedToTray",
        }:
            for instance_id in self.state.active_recovery_tool_instances:
                recovery_state = self._state_by_instance(instance_id)
                if recovery_state is not None and recovery_state.instrument_id == resolved_type:
                    return recovery_state

        preferred_instance_id = ""
        preferred_lifecycles: set[str] | None = None
        if event.event_type == "RobotTaskStarted":
            task_type = str(
                detail.get("task_type", detail.get("action", ""))
            )
            if task_type == "return_unused_preposition":
                preferred_instance_id = (
                    self.state.right_hand_tool_instance_id
                )
                preferred_lifecycles = {
                    LIFECYCLE_PREPOSITIONED_RIGHT
                }
            elif task_type in {
                "predict_tool",
                "tool_predict",
                "pick_up_and_handover",
                "tool_handover",
            }:
                preferred_lifecycles = {
                    LIFECYCLE_HOME_RACK,
                    LIFECYCLE_RETURNED_HOME,
                    LIFECYCLE_PREPOSITIONED_RIGHT,
                    LIFECYCLE_MAYO_REUSE,
                    LIFECYCLE_MAYO_RECOVERY,
                }
            elif task_type in {
                "retrieve_from_mayo",
                "retrieve_from_hand",
                "tool_retrieve",
            }:
                preferred_lifecycles = {
                    LIFECYCLE_MAYO_RECOVERY,
                    LIFECYCLE_MAYO_REUSE,
                    LIFECYCLE_SURGEON_OWNED,
                    LIFECYCLE_RECOVERING_LEFT,
                }
        elif event.event_type in {"RobotGraspedTool", "ToolPrepared", "ToolHandoverCompleted"}:
            preferred_instance_id = self.state.right_hand_tool_instance_id
            preferred_lifecycles = {
                LIFECYCLE_HOME_RACK,
                LIFECYCLE_RETURNED_HOME,
                LIFECYCLE_PREPOSITIONED_RIGHT,
                LIFECYCLE_MAYO_REUSE,
                LIFECYCLE_MAYO_RECOVERY,
            }
        elif event.event_type in {
            "ToolReceivedFromSurgeon",
            "ToolRetrievedFromMayo",
        }:
            preferred_lifecycles = {
                LIFECYCLE_MAYO_RECOVERY,
                LIFECYCLE_MAYO_REUSE,
                LIFECYCLE_SURGEON_OWNED,
            }
        elif event.event_type in {"ToolSentToCleaner", "ToolCleaningProgress"}:
            preferred_instance_id = self.state.left_hand_tool_instance_id
            preferred_lifecycles = {
                LIFECYCLE_RECOVERING_LEFT,
                LIFECYCLE_CLEANING_LEFT,
            }
        elif event.event_type == "ToolCleaningCompleted":
            preferred_lifecycles = {LIFECYCLE_CLEANING_LEFT}
        elif event.event_type == "ToolReturnedToTray":
            preferred_lifecycles = {
                LIFECYCLE_CLEANED_LEFT,
                LIFECYCLE_RECOVERING_LEFT,
            }
        elif event.event_type in {
            "PredictedToolReturnedToRack",
            "UnusedPrepositionReturned",
        }:
            preferred_instance_id = self.state.right_hand_tool_instance_id
            preferred_lifecycles = {LIFECYCLE_PREPOSITIONED_RIGHT}

        selected = self._select_instance(
            resolved_type,
            preferred_instance_id=preferred_instance_id,
            allowed_lifecycles=preferred_lifecycles,
        )
        if selected is not None:
            return selected
        return self._select_instance(resolved_type)

    def apply_event(self, event: TwinEvent) -> None:
        detail = json.loads(event.detail_json) if event.detail_json else {}
        if not isinstance(detail, dict):
            detail = {}
        if event.event_type == "ShadowAdditionalToolHandoverCompleted":
            detail.setdefault(
                "compatibility_event_type",
                "ShadowAdditionalToolHandoverCompleted",
            )
            event.event_type = "ToolHandoverCompleted"
        state = self._resolve_event_state(event, detail)
        instrument_id = (
            state.instrument_id
            if state is not None
            else self.spec.resolve_instrument_alias(event.instrument_id)
            or event.instrument_id
        )
        instance_id = state.instance_id if state is not None else ""
        self._current_event_stamp_sec = _stamp_to_sec(event.stamp)
        self._recompute_transient_state()

        if event.event_type == "RobotTaskStarted":
            self._start_active_robot_task(
                task_id=str(detail.get("task_id", detail.get("command_id", instrument_id))),
                task_type=str(detail.get("task_type", event.mode or event.event_type)),
                instrument_id=instrument_id,
                instrument_instance_id=instance_id,
                arm=event.arm or str(detail.get("arm", "")),
                source_anchor_id=event.source_location_id or str(detail.get("source_anchor_id", "")),
                target_anchor_id=event.target_location_id or str(detail.get("target_anchor_id", "")),
                duration_sec=float(detail.get("duration_sec", 0.1)),
            )
            self._record_event(
                event.event_type,
                {
                    "instrument_id": instrument_id,
                    "instance_id": instance_id,
                    **detail,
                },
            )
            self._recompute_transient_state()
            return

        if event.event_type == "RobotTaskCompleted":
            self._clear_active_robot_task()
            self._record_event(
                event.event_type,
                {
                    "instrument_id": instrument_id,
                    "instance_id": instance_id,
                    **detail,
                },
            )
            self._recompute_transient_state()
            return

        if state and event.event_type in {
            "RobotGraspedTool",
            "ToolPrepared",
            "ToolHandoverCompleted",
            "PredictedToolReturnedToRack",
            "UnusedPrepositionReturned",
        }:
            if self._right_arm_conflict(instance_id):
                self._record_invariant_violation(
                    reason="right_arm_busy",
                    event_type=event.event_type,
                    instrument_id=instrument_id,
                    active_tool=self.state.right_hand_tool_instance_id,
                )
                return

        if state and event.event_type in {
            "ToolReceivedFromSurgeon",
            "ToolRetrievedFromMayo",
            "ToolSentToCleaner",
            "ToolCleaningProgress",
            "ToolCleaningCompleted",
            "ToolReturnedToTray",
        }:
            if self._left_arm_conflict(instance_id):
                self._record_invariant_violation(
                    reason="left_arm_busy",
                    event_type=event.event_type,
                    instrument_id=instrument_id,
                    active_tool=self.state.left_hand_tool_instance_id,
                )
                return

        if event.event_type == "RobotGraspedTool" and state:
            origin_lifecycle = state.lifecycle_stage
            origin_location_type = (
                event.source_location_type or state.location_type
            )
            origin_location_id = event.source_location_id or state.location_id
            picked_from_mayo = (
                state.lifecycle_stage in {LIFECYCLE_MAYO_REUSE, LIFECYCLE_MAYO_RECOVERY}
                or event.source_location_type in {"mayo_stand", "mayo_reuse_zone", "mayo_recovery_zone"}
                or event.source_location_id in {"mayo_stand", "mayo_reuse_zone", "mayo_recovery_zone"}
            )
            if not self._apply_event_transition(
                state=state,
                next_stage=LIFECYCLE_PREPOSITIONED_RIGHT,
                event_type=event.event_type,
                location_type=event.target_location_type or event.location_type or "robot_right_hand",
                location_id=event.target_location_id or event.location_id or "robot_right_hand",
                confidence=max(float(event.confidence), 0.95),
            ):
                return
            state.preposition_origin_location_type = origin_location_type
            state.preposition_origin_location_id = origin_location_id
            state.preposition_origin_lifecycle_stage = origin_lifecycle
            if picked_from_mayo:
                self._close_recovery_transaction(
                    state.instance_id,
                    "mayo_tool_reused_for_handover",
                )
            self.state.robot_state = "busy"
        elif event.event_type == "ToolPrepared" and state:
            origin_lifecycle = (
                state.preposition_origin_lifecycle_stage
                or state.lifecycle_stage
            )
            origin_location_type = (
                state.preposition_origin_location_type
                or event.source_location_type
                or state.location_type
            )
            origin_location_id = (
                state.preposition_origin_location_id
                or event.source_location_id
                or state.location_id
            )
            if not self._apply_event_transition(
                state=state,
                next_stage=LIFECYCLE_PREPOSITIONED_RIGHT,
                event_type=event.event_type,
                location_type=event.target_location_type or event.location_type or "robot_right_hand",
                location_id=event.target_location_id or event.location_id or "robot_right_hand",
                confidence=max(float(event.confidence), 0.95),
                reserved_for=detail.get("reserved_for", self.state.filtered_phase),
            ):
                return
            state.preposition_origin_location_type = origin_location_type
            state.preposition_origin_location_id = origin_location_id
            state.preposition_origin_lifecycle_stage = origin_lifecycle
            self.state.robot_state = "prepared"
        elif event.event_type == "ToolHandoverCompleted" and state:
            handed_over_from_mayo = state.lifecycle_stage in {
                LIFECYCLE_MAYO_REUSE,
                LIFECYCLE_MAYO_RECOVERY,
            }
            field_deployed = self._is_field_deployed_for_phase(
                state.instance_id
            )
            active_surgeon_hand_tool = (
                ""
                if field_deployed
                else self._surgeon_hand_conflict(state.instance_id)
            )
            if active_surgeon_hand_tool:
                self._record_invariant_violation(
                    reason="surgeon_hand_capacity_exceeded",
                    event_type=event.event_type,
                    instrument_id=instrument_id,
                    active_tool=active_surgeon_hand_tool,
                )
                self._record_event(
                    "ToolHandoverBlocked",
                    {
                        "instrument_id": instrument_id,
                        "instance_id": state.instance_id,
                        "reason": "surgeon_hand_capacity_exceeded",
                        "policy": (
                            "public_mayo_placement_or_completion_cleanup_required"
                        ),
                    },
                )
                return
            if not self._apply_event_transition(
                state=state,
                next_stage=LIFECYCLE_SURGEON_OWNED,
                event_type=event.event_type,
                location_type=(
                    "surgical_field"
                    if field_deployed
                    else "surgeon_hand"
                ),
                location_id=(
                    self._field_anchor_id()
                    if field_deployed
                    else "surgeon_hand"
                ),
                confidence=max(float(event.confidence), 0.95),
            ):
                return
            if handed_over_from_mayo:
                self._close_recovery_transaction(
                    state.instance_id,
                    "mayo_tool_handover_completed",
                )
            state.preposition_origin_location_type = ""
            state.preposition_origin_location_id = ""
            state.preposition_origin_lifecycle_stage = ""
            self.state.robot_state = "idle"
            if self._is_active_requested_tool(state.instance_id):
                self._dequeue_active_request("handover_completed")
            else:
                self._sync_active_request_from_queue()
        elif event.event_type in {
            "ToolReceivedFromSurgeon",
            "ToolRetrievedFromMayo",
        } and state:
            self._open_recovery_transaction(
                state.instance_id, "robot_received_returned_tool"
            )
            if not self._apply_event_transition(
                state=state,
                next_stage=LIFECYCLE_RECOVERING_LEFT,
                event_type=event.event_type,
                location_type="robot_left_hand",
                location_id="robot_left_hand",
                confidence=max(float(event.confidence), 0.95),
            ):
                return
            if self._is_active_requested_tool(state.instance_id):
                self._dequeue_active_request("retrieval_started")
            else:
                self._sync_active_request_from_queue()
        elif event.event_type == "ToolSentToCleaner" and state:
            active_cleaner_tool = next(
                (
                    candidate_id
                    for candidate_id, cleaner_state in self.instrument_states.items()
                    if candidate_id != state.instance_id
                    and cleaner_state.lifecycle_stage
                    == LIFECYCLE_CLEANING_LEFT
                ),
                "",
            )
            if active_cleaner_tool:
                self._set_lifecycle(
                    state,
                    LIFECYCLE_RECOVERING_LEFT,
                    location_type="robot_left_hand",
                    location_id="robot_left_hand",
                    confidence=max(float(event.confidence), 0.9),
                )
                self.state.cleaner_busy = True
                self.state.robot_state = "busy"
                self._open_recovery_transaction(
                    state.instance_id, "cleaner_busy_queued"
                )
                self._record_event(
                    "CleanerBusyToolQueued",
                    {
                        "instrument_id": instrument_id,
                        "instance_id": state.instance_id,
                        "active_cleaner_tool": active_cleaner_tool,
                        "reason": "cleaner_allows_one_tool",
                    },
                )
                self._recompute_transient_state()
                return
            if not self._apply_event_transition(
                state=state,
                next_stage=LIFECYCLE_CLEANING_LEFT,
                event_type=event.event_type,
                location_type="cleaner_slot",
                location_id="cleaner_slot",
                confidence=max(float(event.confidence), 0.95),
            ):
                return
            self.state.cleaner_busy = True
            self.state.cleaner_remaining_sec = float(detail.get("remaining_sec", 0.0))
            self.state.robot_state = "cleaning"
        elif event.event_type == "ToolCleaningProgress" and state:
            if state.lifecycle_stage != LIFECYCLE_CLEANING_LEFT:
                self._record_invariant_violation(
                    reason="cleaning_progress_without_cleaning_state",
                    event_type=event.event_type,
                    instrument_id=instrument_id,
                    proposed_stage=LIFECYCLE_CLEANING_LEFT,
                )
                return
            self.state.cleaner_busy = True
            self.state.cleaner_remaining_sec = float(detail.get("remaining_sec", self.state.cleaner_remaining_sec))
            self.state.robot_state = "cleaning"
        elif event.event_type == "ToolCleaningCompleted" and state:
            if not self._apply_event_transition(
                state=state,
                next_stage=LIFECYCLE_CLEANED_LEFT,
                event_type=event.event_type,
                location_type="cleaner_slot",
                location_id="cleaner_slot",
                confidence=max(float(event.confidence), 0.95),
            ):
                return
            self.state.cleaner_busy = False
            self.state.cleaner_remaining_sec = 0.0
            self.state.robot_state = "ready_to_return"
        elif event.event_type in {
            "ToolReturnedToTray",
            "PredictedToolReturnedToRack",
            "UnusedPrepositionReturned",
        } and state:
            return_to_mayo = (
                event.event_type == "UnusedPrepositionReturned"
                and (
                    str(detail.get("target_lifecycle_stage", ""))
                    == LIFECYCLE_MAYO_REUSE
                    or event.target_location_type
                    in {"mayo_stand", "mayo_reuse_zone"}
                    or event.target_location_id
                    in {"mayo_stand", "mayo_reuse_zone"}
                )
            )
            if return_to_mayo:
                if state.lifecycle_stage != LIFECYCLE_PREPOSITIONED_RIGHT:
                    self._record_invariant_violation(
                        reason="unused_preposition_return_requires_right_hand",
                        event_type=event.event_type,
                        instrument_id=instrument_id,
                        proposed_stage=LIFECYCLE_MAYO_REUSE,
                    )
                    return
                self._set_lifecycle(
                    state,
                    LIFECYCLE_MAYO_REUSE,
                    location_type=(
                        event.target_location_type or "mayo_reuse_zone"
                    ),
                    location_id=(
                        event.target_location_id or "mayo_reuse_zone"
                    ),
                    confidence=max(float(event.confidence), 0.95),
                    last_update_sec=getattr(
                        self, "_current_event_stamp_sec", None
                    ),
                    placement_evidence=(
                        "robot_returned_unused_preposition"
                    ),
                )
                self._clear_observation_candidate(state.instance_id)
            elif not self._apply_event_transition(
                state=state,
                next_stage=LIFECYCLE_RETURNED_HOME,
                event_type=event.event_type,
                location_type=(
                    event.target_location_type
                    or state.home_location_type
                ),
                location_id=(
                    event.target_location_id or state.home_location_id
                ),
                confidence=max(float(event.confidence), 0.95),
            ):
                return
            state.preposition_origin_location_type = ""
            state.preposition_origin_location_id = ""
            state.preposition_origin_lifecycle_stage = ""
            if event.mode == "shadow_counterfactual":
                self._shadow_counterfactual_locked_instances.add(
                    state.instance_id
                )
            self.state.robot_state = "idle"
            if event.event_type == "ToolReturnedToTray":
                self.state.cleaner_busy = False
                self.state.cleaner_remaining_sec = 0.0
                self._close_recovery_transaction(
                    state.instance_id, "tool_returned_home"
                )
        elif event.event_type == "SafetyRetract":
            if self.state.left_hand_tool or self.state.right_hand_tool or self.state.cleaner_busy:
                self._record_invariant_violation(
                    reason="retract_blocked_by_payload",
                    event_type=event.event_type,
                    instrument_id=instrument_id or "",
                    active_tool=self.state.left_hand_tool or self.state.right_hand_tool,
                )
            else:
                self.state.robot_state = "retracted"
        elif event.event_type == "RobotWentIdle":
            self.state.robot_state = "idle"

        self._recompute_transient_state()
        self._record_event(
            event.event_type,
            {
                "instrument_id": instrument_id,
                "instance_id": instance_id,
                "lifecycle_stage": state.lifecycle_stage if state is not None else "",
                **detail,
            },
        )

    def get_expected_instruments(self) -> list[str]:
        phase_id = self._active_context_phase_id()
        if phase_id not in self.spec.phase_ids:
            phase_id = self.spec.default_phase_id
        return self.spec.get_expected_instruments(phase_id)

    def get_available_instruments(self) -> list[str]:
        return sorted(
            {
                state.instrument_id
                for state in self.instrument_states.values()
                if _is_available_for_handover(state)
            }
        )

    def handover_allowed(self) -> bool:
        voice_backed_request = self.explicit_request_voice_backed()
        if any(
            flag in BLOCKING_SAFETY_FLAGS
            and not (voice_backed_request and flag == "vlm_unhealthy")
            for flag in self.state.safety_flags
        ):
            return False
        if (
            self.spec.bundle.action_guard
            and self.spec.bundle.action_guard.block_handover_when_phase_uncertain
            and self.state.phase_uncertain
            and not voice_backed_request
        ):
            return False
        if self.state.active_robot_task is not None:
            return False
        if self.state.robot_state in {"fault", "retracted"} or self.state.cleaner_busy:
            return False
        requested_tool = self.state.surgeon_request_tool or self.state.explicit_request_tool
        if not requested_tool:
            return True
        requested_state = self.get_instrument_state(
            self.state.surgeon_request_instance_id or requested_tool
        )
        if requested_state is None:
            return False
        if self.state.surgeon_intent in ACTIVE_REQUEST_INTENTS and not self.state.surgeon_ready_for_handover:
            return False
        requested_on_mayo = requested_state.lifecycle_stage in {
            LIFECYCLE_MAYO_REUSE,
            LIFECYCLE_MAYO_RECOVERY,
        }
        if requested_state.contaminated and not requested_on_mayo:
            return False
        if requested_state.lifecycle_stage == LIFECYCLE_SURGEON_OWNED:
            return False
        surgeon_owned_count = sum(
            1
            for state in self.instrument_states.values()
            if state.lifecycle_stage == LIFECYCLE_SURGEON_OWNED
            and (state.location_type == "surgeon_hand" or state.status == "handed_over")
        )
        field_deployed = self._is_field_deployed_for_phase(
            requested_state.instance_id
        )
        if surgeon_owned_count >= 2 and not field_deployed:
            return False
        if requested_state.lifecycle_stage not in {
            LIFECYCLE_HOME_RACK,
            LIFECYCLE_RETURNED_HOME,
            LIFECYCLE_PREPOSITIONED_RIGHT,
            LIFECYCLE_MAYO_REUSE,
            LIFECYCLE_MAYO_RECOVERY,
        }:
            return False
        if requested_state.owner not in {"", "none", "robot_right_hand"}:
            return False
        return True

    def recovery_required(self) -> bool:
        return bool(self.state.active_recovery_tools) or any(
            state.next_required_transition in PENDING_TRANSITIONS_REQUIRE_ACTION
            for state in self.instrument_states.values()
        )

    def instrument_payload(self) -> list[dict[str, Any]]:
        return [asdict(state) for state in self.instrument_states.values()]

    def get_simulation_snapshot(self) -> dict[str, Any]:
        self._refresh_active_robot_task()
        return {
            "procedure_id": self.state.procedure_id,
            "running": self.state.running,
            "execution_state": self.state.execution_state,
            "filtered_phase": self.state.filtered_phase,
            "robot_state": self.state.robot_state,
            "surgeon_intent": self.state.surgeon_intent,
            "surgeon_request_tool": self.state.surgeon_request_tool,
            "surgeon_request_instance_id": (
                self.state.surgeon_request_instance_id
            ),
            "surgeon_ready_for_handover": self.state.surgeon_ready_for_handover,
            "surgeon_ready_for_retrieval": self.state.surgeon_ready_for_retrieval,
            "cleaner_busy": self.state.cleaner_busy,
            "cleaner_remaining_sec": self.state.cleaner_remaining_sec,
            "pending_transition_tools": list(self.state.pending_transition_tools),
            "active_recovery_tools": list(self.state.active_recovery_tools),
            "active_recovery_tool_instances": list(
                self.state.active_recovery_tool_instances
            ),
            "right_hand_tool": self.state.right_hand_tool,
            "right_hand_tool_instance_id": (
                self.state.right_hand_tool_instance_id
            ),
            "left_hand_tool": self.state.left_hand_tool,
            "left_hand_tool_instance_id": (
                self.state.left_hand_tool_instance_id
            ),
            "prepositioned_tool": self.state.prepositioned_tool,
            "prepositioned_tool_instance_id": (
                self.state.prepositioned_tool_instance_id
            ),
            "active_robot_task": asdict(self.state.active_robot_task) if self.state.active_robot_task else {},
            "recent_events": list(self.state.recent_event_types),
            "instrument_states": self.instrument_payload(),
        }
