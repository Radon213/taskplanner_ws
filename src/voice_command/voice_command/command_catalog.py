"""Small, hot-reloadable command catalog for researcher-owned ROS wiring.

The catalog deliberately owns *routing* rather than procedure policy.  A
command can therefore be added by editing one YAML file and reloading this
process; it does not need to be duplicated in a VLM prompt, BT tree, Digital
Twin reducer, launch allowlist, or generated message schema.

ROS interface types remain explicit at the transport boundary.  The catalog is
not an authentication or physical-safety boundary: a controller still owns
its own limits, interlocks, and request idempotency.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import copy
import hashlib
import math
from pathlib import Path
import re
from typing import Any, Callable, Mapping
import uuid

import yaml


CATALOG_SCHEMA = "taskplanner.command-catalog.v1"
RETRACTION_SERVICE_TYPE = "surgical_interop_msgs/srv/ExecuteRetractionCommand"
TOOL_HANDOVER_ACTION_TYPE = "surgical_interop_msgs/action/ExecuteToolHandover"
# These are public, fixed execution-owner endpoints.  Keep literals here so
# the lightweight command owner never imports route selection or controller
# implementation code; the execution proxy independently serves the same
# stable names.
EXECUTION_RETRACTION_PROXY_SERVICE_ENDPOINT = (
    "/taskplanner/execution/retraction/command"
)
EXECUTION_TOOL_HANDOVER_PROXY_ENDPOINT = "/taskplanner/execution/tool_handover"
RETRACTION_PROTOCOL_VERSION = 1
DEFAULT_ACTION_TIMEOUT_SEC = 30.0

_INTERFACE_TYPE_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9_]*/(?:msg|srv|action)/[A-Za-z][A-Za-z0-9_]*$"
)
_FINAL_CLAUSE_BOUNDARY_RE = re.compile(
    r"(?:[!?;。！？；]+|(?<!\d)\.+(?!\d)|\r?\n+)"
)
# This shares the final-clause decimal rule, while treating a comma as an
# explicit command boundary only when it is not the decimal separator in a
# number.  It is used solely to compare independently spoken clauses; actual
# catalog/semantic matching remains unchanged.
_EXPLICIT_COMMAND_CLAUSE_BOUNDARY_RE = re.compile(
    r"(?:[!?;。！？；]+|(?<!\d)\.+(?!\d)|(?:[,，](?!\d)|(?<!\d)[,，])|\r?\n+)"
)
_MAX_FINAL_COMMAND_CLAUSE_CHARS = 120
_MAX_FINAL_COMMAND_CLAUSE_WORDS = 20
_MAX_COMMAND_SUFFIX_CANDIDATES = 24
_COMMAND_SUFFIX_BOUNDARY_RE = re.compile(r"(?:\s+|[!?;。！？；]+|(?<!\d)\.+(?!\d))")
_COMMAND_CONNECTOR_RE = re.compile(r"(?:\s*(?:그리고|그다음|다음으로)\s*|\s+then\s+|\s+and\s+)", re.IGNORECASE)
# The compact span matcher below deliberately removes whitespace, so keep a
# companion connector detector for the gap between two otherwise independent
# catalog spans.  This is rejection-only: it never turns a phrase into a
# command.  In particular, a noisy ``suction suction 시작`` remains one
# candidate, while ``suction 시작 그리고 suction 시작`` is two requests.
_COMPACT_COMMAND_SPAN_BOUNDARY_RE = re.compile(
    r"(?:그리고|그다음|다음으로|then|and|[,，!?;。！？；]+|(?<!\d)\.+(?!\d))",
    re.IGNORECASE,
)

# These are not a second command grammar. They are deliberately coarse span
# detectors used only to reject an STT final containing two distinct operations
# before the fast catalog path selects one fragment. Detailed procedure, side,
# distance, and endpoint validation remains in the typed semantic resolver.
_PULL_MARKERS = ("당겨", "땡겨", "pull")
_RETRACTION_TERMS = ("리트랙션", "retraction", "retractor")
_DIRECT_TEACH_TERMS = ("직접교시", "교시", "directteach", "teaching")
_START_STOP_TERMS = ("시작", "개시", "종료", "중지", "stop", "start", "finish", "end")
_PROCEDURE_TERMS = ("갑상선절제술", "thyroidectomy", "갑상선수술", "수술")
_NON_TOOL_REQUEST_TARGETS = frozenset(
    {
        "빼",
        "빠져",
        "들어와",
        "시작해",
        "시작",
        "켜",
        "꺼",
        "제거해",
        "중지해",
        "당겨",
        "땡겨",
    }
)
# A coarse ambiguity detector must never promote arbitrary conversational
# Korean such as ``결론에 적어 주세요`` into a tool operation.  Dynamic tool
# grounding remains in ``VoiceIntentResolver``; this intentionally small
# cross-procedure vocabulary exists only to identify an already-recognizable
# tool-request span next to a second command in the same final ASR delivery.
_KNOWN_TOOL_REQUEST_LEXEMES = frozenset(
    {
        "도구",
        "기구",
        "tool",
        "instrument",
        "보비",
        "bovie",
        "바이폴라",
        "bipolar",
        "애드슨",
        "adson",
        "모스키토",
        "mosquito",
        "앨리스",
        "allis",
        "메스",
        "scissor",
        "가위",
        "켈리",
        "kelly",
        "army",
        "메첸바움",
        "metzenbaum",
        "말레어블",
        "malleable",
        "포셉",
        "forcep",
        "forceps",
        "피넛",
        "peanut",
        "썬더비트",
        "thunderbeat",
        "debakey",
    }
)
_EXPLICIT_TOOL_REQUEST_RE = re.compile(
    r"(?P<tool>[a-zA-Z가-힣]{2,})(?:을|를)?\s*(?:줘|주세요|회수(?:해|해줘)?|치워(?:줘)?|정리(?:해|해줘)?|전달(?:해|해줘)?|가져와(?:줘)?)",
    re.IGNORECASE,
)
_REPORTED_COMMAND_END_RE = re.compile(
    r"(?:라고|이라는|라는)?(?:말(?:했|해|한다|씀|했다)|읽(?:었|어|는다)|"
    r"표현(?:했|해)|설명(?:했|해)|들었)(?:어|어요|다|습니다|죠)?[.!?]?$"
    # ASR often omits the literal ``말`` in a report such as
    # ``suction 빠져라고 했어요``.  Treat that as reported speech too; this
    # remains end-anchored, so a real final command after earlier prose is
    # still eligible for the bounded suffix path.
    r"|(?:라고|이라는|라는)했(?:어|어요|다|습니다|죠)?[.!?]?$"
    r"|(?:라고|이라는|라는)(?:표현|문장)(?:을|를)?(?:쓰|말|읽)"
    r"(?:세요|었어|었다|습니다|다)?[.!?]?$"
)
_COMMAND_QUOTE_PAIRS = (("\"", "\""), ("'", "'"), ("“", "”"), ("‘", "’"), ("「", "」"), ("『", "』"))
_EMBEDDED_COMMAND_QUESTION_CUES = (
    "왜",
    "할까",
    "할까요",
    "될까",
    "될까요",
    "인가요",
    "나요",
    "겠습니까",
    "can you",
    "would you",
    "do we",
    "도 돼",
)
_EMBEDDED_COMMAND_NEGATION_CUES = (
    "하지마",
    "하지말",
    "지마",
    "지말",
    "말자",
    "말고",
    "않",
    "금지",
    "do not",
    "dont",
    "don't",
)


class CommandCatalogError(ValueError):
    """A human-readable catalog error that leaves the last catalog active."""


def normalize_command_phrase(value: object) -> str:
    """Normalize only matching whitespace/case; do not reinterpret speech."""

    return " ".join(str(value or "").strip().casefold().split())


def bounded_final_command_clause(value: object) -> str:
    """Return a short final sentence after an explicit punctuation boundary.

    This is intentionally not a fuzzy suffix search.  A caller still has to
    match the returned clause as a complete command, so unrelated ambient
    speech cannot become actionable merely because it happens to contain a
    command word.  Decimal points are not treated as sentence boundaries.
    """

    raw = str(value or "").strip()
    if not raw:
        return ""
    boundaries = tuple(_FINAL_CLAUSE_BOUNDARY_RE.finditer(raw))
    for boundary in reversed(boundaries):
        clause = raw[boundary.end() :].strip()
        if not clause:
            continue
        normalized = normalize_command_phrase(clause)
        if (
            not normalized
            or len(clause) > _MAX_FINAL_COMMAND_CLAUSE_CHARS
            or len(normalized.split()) > _MAX_FINAL_COMMAND_CLAUSE_WORDS
        ):
            return ""
        return clause
    return ""


def explicit_command_clauses(value: object) -> tuple[str, ...]:
    """Split only explicit sentence/connector boundaries for ambiguity checks.

    This helper deliberately does not guess where one spoken command ends.
    It lets the resolver prove that *two independently bounded clauses* are
    executable, then reject the entire ASR final rather than silently running
    only its last clause.  A free-form sentence with no explicit boundary is
    returned intact so existing noise-tolerant matching continues unchanged.
    """

    raw = str(value or "").strip()
    if not raw:
        return ()
    clauses: list[str] = []
    for sentence in _EXPLICIT_COMMAND_CLAUSE_BOUNDARY_RE.split(raw):
        for clause in _COMMAND_CONNECTOR_RE.split(sentence):
            normalized = clause.strip()
            if normalized:
                clauses.append(normalized)
    return tuple(clauses)


def bounded_command_suffixes(value: object) -> tuple[str, ...]:
    """Return bounded suffixes that may contain a trailing spoken command.

    Final ASR deliveries can concatenate an earlier sentence and the current
    command without punctuation.  Trying suffixes from longest to shortest
    lets the existing typed resolver retain all available slots (for example
    side and distance) while discarding only an unrelated leading fragment.
    """

    raw = str(value or "").strip()
    if not raw:
        return ()
    starts = [match.end() for match in _COMMAND_SUFFIX_BOUNDARY_RE.finditer(raw)]
    starts = starts[-_MAX_COMMAND_SUFFIX_CANDIDATES:]
    candidates: list[str] = []
    seen: set[str] = set()
    for start in starts:
        suffix = raw[start:].strip()
        normalized = normalize_command_phrase(suffix)
        if (
            not normalized
            or normalized in seen
            or len(suffix) > _MAX_FINAL_COMMAND_CLAUSE_CHARS
            or len(normalized.split()) > _MAX_FINAL_COMMAND_CLAUSE_WORDS
        ):
            continue
        seen.add(normalized)
        candidates.append(suffix)
    return tuple(candidates)


def command_is_reported_or_quoted(value: object) -> bool:
    """Return whether the utterance quotes or reports a command phrase.

    The report check is anchored at the end so unrelated earlier speech such
    as ``아까 5cm라고 했어 오른쪽 1cm 당겨줘`` can still recover the real
    trailing command.
    """

    raw = str(value or "")
    stripped = raw.strip().rstrip(".!?。！？").rstrip()
    if any(
        len(stripped) >= 2
        and stripped.endswith(right)
        and stripped.rfind(left, 0, len(stripped) - len(right)) >= 0
        for left, right in _COMMAND_QUOTE_PAIRS
    ):
        return True
    compact = normalize_command_phrase(raw).replace(" ", "")
    return bool(_REPORTED_COMMAND_END_RE.search(compact))


def command_context_is_blocked(value: object) -> bool:
    """Keep keyword recovery out of questions, negations, and quotations."""

    raw = str(value or "")
    normalized = normalize_command_phrase(raw)
    compact = normalized.replace(" ", "")
    if "?" in raw or "？" in raw:
        return True
    if any(cue.replace(" ", "") in compact for cue in _EMBEDDED_COMMAND_QUESTION_CUES):
        return True
    if any(cue.replace(" ", "") in compact for cue in _EMBEDDED_COMMAND_NEGATION_CUES):
        return True
    return command_is_reported_or_quoted(raw)


def _ordered_phrase_score(text: str, phrase: str) -> tuple[int, int, int] | None:
    """Score phrase keywords found in order, allowing unrelated ASR filler."""

    compact_text = text.replace(" ", "")
    terms = tuple(part.replace(" ", "") for part in phrase.split() if part)
    if not compact_text or not terms:
        return None
    cursor = 0
    first = -1
    end = -1
    for term in terms:
        index = compact_text.find(term, cursor)
        if index < 0:
            return None
        if first < 0:
            first = index
        end = index + len(term)
        cursor = end
    # More authored keyword terms and more literal characters are more
    # specific. For equal phrases, prefer the tighter observed span.
    return len(terms), sum(len(term) for term in terms), -(end - first)


def _ordered_phrase_spans(
    text: str,
    phrase: str,
    *,
    limit: int = 16,
) -> tuple[tuple[int, int], ...]:
    """Return a bounded set of ordered compact-text phrase spans.

    ``_ordered_phrase_score`` intentionally permits harmless ASR filler, but
    using only its *first* match makes ``suction 시작 ... suction 빠져`` look
    like one long ``suction 빠져`` span.  For ambiguity rejection we need the
    local second occurrence as well.  The matcher therefore enumerates a
    small, deterministic set of ordered spans; it never changes routing or
    creates a payload.
    """

    compact_text = text.replace(" ", "")
    terms = tuple(part.replace(" ", "") for part in phrase.split() if part)
    if not compact_text or not terms:
        return ()
    positions: list[tuple[int, ...]] = []
    for term in terms:
        starts: list[int] = []
        cursor = 0
        while len(starts) < limit:
            index = compact_text.find(term, cursor)
            if index < 0:
                break
            starts.append(index)
            cursor = index + 1
        if not starts:
            return ()
        positions.append(tuple(starts))

    spans: list[tuple[int, int]] = []

    def visit(term_index: int, start: int, cursor: int) -> None:
        if len(spans) >= limit:
            return
        if term_index >= len(terms):
            spans.append((start, cursor))
            return
        term = terms[term_index]
        for index in positions[term_index]:
            if index < cursor:
                continue
            visit(
                term_index + 1,
                index if start < 0 else start,
                index + len(term),
            )
            if len(spans) >= limit:
                return

    visit(0, -1, 0)
    return tuple(dict.fromkeys(spans))


def _catalog_command_category(command: "CatalogCommand") -> str:
    """Return a coarse operation category for ambiguity screening only."""

    name = str(command.name).casefold()
    if "suction" in name:
        return name
    if "direct" in name or "teach" in name:
        return "direct_teach"
    if "retract" in name:
        return "retraction"
    if "procedure" in name or "surgery" in name:
        return "procedure"
    if "retrieve" in name or "return" in name:
        return "tool_retrieve"
    if "handover" in name or "tool" in name:
        return "tool_handover"
    return f"catalog:{name}"


def semantic_command_categories(value: object) -> frozenset[str]:
    """Find independently recognizable operation families in one utterance.

    This deliberately does not return an endpoint or payload. Its only job is
    to reject a final STT delivery containing two distinct operations before a
    fast-path matcher can select one fragment. A single noisy command remains
    admissible because one category alone is not ambiguity.
    """

    normalized = normalize_command_phrase(value)
    compact = normalized.replace(" ", "")
    if not compact:
        return frozenset()
    categories: set[str] = set()
    if any(marker in compact for marker in _PULL_MARKERS):
        categories.add("retraction")
    has_retraction = any(term in compact for term in _RETRACTION_TERMS)
    has_direct_teach = any(term in compact for term in _DIRECT_TEACH_TERMS)
    has_lifecycle = any(term in compact for term in _START_STOP_TERMS)
    if has_direct_teach and has_lifecycle:
        categories.add("direct_teach")
    elif has_retraction and has_lifecycle:
        categories.add("retraction")
    if any(term in compact for term in _PROCEDURE_TERMS) and has_lifecycle:
        categories.add("procedure")
    for match in _EXPLICIT_TOOL_REQUEST_RE.finditer(normalized):
        target = normalize_command_phrase(match.group("tool")).replace(" ", "")
        if (
            target in _NON_TOOL_REQUEST_TARGETS
            or target not in _KNOWN_TOOL_REQUEST_LEXEMES
        ):
            continue
        suffix = normalize_command_phrase(match.group(0)).replace(" ", "")
        categories.add(
            "tool_retrieve"
            if any(term in suffix for term in ("회수", "치워", "정리"))
            else "tool_handover"
        )
        break
    return frozenset(categories)


def _mapping(value: object, *, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CommandCatalogError(f"{label} must be an object")
    return dict(value)


def _string(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise CommandCatalogError(f"{label} must be a non-empty trimmed string")
    return value


def _json_value(value: object, *, label: str) -> Any:
    """Detach YAML values and reject non-serializable YAML constructs."""

    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, list):
        return [_json_value(item, label=label) for item in value]
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CommandCatalogError(f"{label} keys must be strings")
            result[key] = _json_value(item, label=label)
        return result
    raise CommandCatalogError(
        f"{label} contains unsupported value {type(value).__name__}"
    )


def _validate_endpoint(value: object, *, label: str) -> str:
    endpoint = _string(value, label=label)
    if not endpoint.startswith("/") or " " in endpoint:
        raise CommandCatalogError(f"{label} must be an absolute ROS name")
    return endpoint


@dataclass(frozen=True, slots=True)
class CatalogCommand:
    """One speech phrase to ROS transport mapping from the active YAML file."""

    name: str
    phrases: tuple[str, ...]
    kind: str
    interface_type: str
    endpoint: str
    payload: Mapping[str, Any]
    command_id_prefix: str
    action_timeout_sec: float | None


@dataclass(frozen=True, slots=True)
class RoutedCommand:
    """Fully rendered outbound request, before ROS transport dispatch."""

    command: CatalogCommand
    command_id: str
    endpoint: str
    payload: Mapping[str, Any]


class CommandCatalog:
    """Immutable catalog lookup; phrases are intentionally exact-match only."""

    def __init__(self, commands: tuple[CatalogCommand, ...]) -> None:
        if not commands:
            raise CommandCatalogError("commands must contain at least one command")
        by_phrase: dict[str, CatalogCommand] = {}
        for command in commands:
            for phrase in command.phrases:
                owner = by_phrase.setdefault(phrase, command)
                if owner is not command:
                    raise CommandCatalogError(
                        f"duplicate phrase {phrase!r} in {owner.name!r} and "
                        f"{command.name!r}"
                    )
        self._commands = commands
        self._by_phrase = by_phrase

    @property
    def commands(self) -> tuple[CatalogCommand, ...]:
        return self._commands

    def match(self, text: object) -> CatalogCommand | None:
        return self._by_phrase.get(normalize_command_phrase(text))

    def has_multiple_command_spans(self, text: object) -> bool:
        """Return whether one final delivery contains distinct operations.

        This runs before direct catalog dispatch. Without it, a sentence such
        as ``오른쪽 1 cm 더 당겨줘 suction 빠져`` can match the catalog's
        suction phrase and bypass the semantic resolver that would otherwise
        reject the two-command delivery.
        """

        if command_context_is_blocked(text):
            return False
        normalized = normalize_command_phrase(text)
        phrase_matches: list[
            tuple[tuple[int, int, int], tuple[int, int], CatalogCommand]
        ] = []
        for phrase, command in self._by_phrase.items():
            score = _ordered_phrase_score(normalized, phrase)
            if score is None:
                continue
            for span in _ordered_phrase_spans(normalized, phrase):
                # Favor a local authored span over a cross-sentence one while
                # retaining the catalog phrase's semantic specificity.
                local_score = (score[0], score[1], -(span[1] - span[0]))
                phrase_matches.append((local_score, span, command))
        # Longer authored phrases win when they occupy the same words (for
        # example ``suction 빠져`` must not look like both ``suction`` and
        # ``suction_out``). Distinct non-overlapping spans stay independent.
        selected_spans: list[tuple[int, int]] = []
        catalog_categories: set[str] = set()
        for score, span, command in sorted(
            phrase_matches,
            key=lambda item: (item[0], -(item[1][1] - item[1][0])),
            reverse=True,
        ):
            if any(span[0] < end and start < span[1] for start, end in selected_spans):
                continue
            selected_spans.append(span)
            catalog_categories.add(_catalog_command_category(command))
        semantic_categories = set(semantic_command_categories(normalized))
        # A catalog retraction/direct-teach/procedure command can naturally
        # satisfy the corresponding coarse detector. Count that as one
        # operation, while keeping suction start/out intentionally distinct.
        categories = catalog_categories | semantic_categories
        if len(categories) > 1:
            return True

        # Categories alone cannot distinguish two repetitions of the same
        # catalog command.  Treat them as compound only when a deliberate
        # connector or sentence boundary separates the non-overlapping spans;
        # otherwise repeated ASR words are still allowed to resolve as one
        # noise-tolerant command.
        compact = normalized.replace(" ", "")
        ordered_spans = sorted(selected_spans)
        for left, right in zip(ordered_spans, ordered_spans[1:]):
            if _COMPACT_COMMAND_SPAN_BOUNDARY_RE.search(
                compact[left[1] : right[0]]
            ):
                return True
        return False

    def match_embedded_keywords(self, text: object) -> CatalogCommand | None:
        """Match one unambiguous catalog command inside a noisy ASR delivery.

        This remains catalog-driven: the fallback can only select an already
        authored phrase and never invents an endpoint or payload.  A longer
        phrase such as ``석션 빠져`` wins over its shorter ``석션`` prefix.
        """

        if command_context_is_blocked(text):
            return None
        normalized = normalize_command_phrase(text)
        if self.has_multiple_command_spans(normalized):
            return None

        # A single final ASR delivery may contain unrelated prose plus one
        # command, but two independently resolvable command clauses are
        # ambiguous and must not be collapsed to whichever phrase is longer.
        clause_owners: list[CatalogCommand] = []
        for clause in _COMMAND_CONNECTOR_RE.split(normalized):
            owner = self._best_embedded_command(clause)
            if owner is not None and all(item is not owner for item in clause_owners):
                clause_owners.append(owner)
        if len(clause_owners) > 1:
            return None
        return self._best_embedded_command(normalized)

    def _best_embedded_command(self, normalized: str) -> CatalogCommand | None:
        scored: list[tuple[tuple[int, int, int], CatalogCommand]] = []
        for phrase, command in self._by_phrase.items():
            score = _ordered_phrase_score(normalized, phrase)
            if score is not None:
                scored.append((score, command))
        if not scored:
            return None
        best_score = max(score for score, _ in scored)
        owners: list[CatalogCommand] = []
        for score, command in scored:
            if score == best_score and all(owner is not command for owner in owners):
                owners.append(command)
        return owners[0] if len(owners) == 1 else None


def _parse_command(raw: object, *, index: int) -> CatalogCommand:
    item = _mapping(raw, label=f"commands[{index}]")
    allowed = {
        "name",
        "phrases",
        "dispatch",
        "command_id_prefix",
    }
    unknown = sorted(set(item) - allowed)
    if unknown:
        raise CommandCatalogError(
            f"commands[{index}] has unknown fields: {', '.join(unknown)}"
        )
    name = _string(item.get("name"), label=f"commands[{index}].name")
    raw_phrases = item.get("phrases")
    if not isinstance(raw_phrases, list) or not raw_phrases:
        raise CommandCatalogError(f"commands[{index}].phrases must be a non-empty list")
    phrases = tuple(
        dict.fromkeys(
            normalize_command_phrase(
                _string(value, label=f"commands[{index}].phrases")
            )
            for value in raw_phrases
        )
    )
    if not all(phrases):
        raise CommandCatalogError(f"commands[{index}].phrases contains an empty phrase")
    dispatch = _mapping(item.get("dispatch"), label=f"commands[{index}].dispatch")
    dispatch_allowed = {"kind", "type", "endpoint", "payload", "timeout_sec"}
    dispatch_unknown = sorted(set(dispatch) - dispatch_allowed)
    if dispatch_unknown:
        raise CommandCatalogError(
            f"commands[{index}].dispatch has unknown fields: "
            f"{', '.join(dispatch_unknown)}"
        )
    kind = _string(dispatch.get("kind"), label=f"commands[{index}].dispatch.kind")
    if kind not in {"topic", "service", "action"}:
        raise CommandCatalogError(
            f"commands[{index}].dispatch.kind must be topic, service, or action"
        )
    interface_type = _string(
        dispatch.get("type"), label=f"commands[{index}].dispatch.type"
    )
    expected_segment = {"topic": "/msg/", "service": "/srv/", "action": "/action/"}[kind]
    if not _INTERFACE_TYPE_RE.fullmatch(interface_type) or expected_segment not in interface_type:
        raise CommandCatalogError(
            f"commands[{index}].dispatch.type must be a {kind} interface type"
        )
    endpoint = _validate_endpoint(
        dispatch.get("endpoint"), label=f"commands[{index}].dispatch.endpoint"
    )
    payload = _json_value(
        _mapping(dispatch.get("payload"), label=f"commands[{index}].dispatch.payload"),
        label=f"commands[{index}].dispatch.payload",
    )
    raw_timeout = dispatch.get("timeout_sec")
    if raw_timeout is not None and kind != "action":
        raise CommandCatalogError(
            f"commands[{index}].dispatch.timeout_sec is only valid for actions"
        )
    if kind == "action":
        if raw_timeout is None:
            action_timeout_sec = DEFAULT_ACTION_TIMEOUT_SEC
        elif (
            isinstance(raw_timeout, bool)
            or not isinstance(raw_timeout, (int, float))
            or not math.isfinite(float(raw_timeout))
            or float(raw_timeout) <= 0.0
        ):
            raise CommandCatalogError(
                f"commands[{index}].dispatch.timeout_sec must be a finite positive number"
            )
        else:
            action_timeout_sec = float(raw_timeout)
    else:
        action_timeout_sec = None
    prefix = _string(
        item.get("command_id_prefix", name),
        label=f"commands[{index}].command_id_prefix",
    )
    return CatalogCommand(
        name=name,
        phrases=phrases,
        kind=kind,
        interface_type=interface_type,
        endpoint=endpoint,
        payload=payload,
        command_id_prefix=prefix,
        action_timeout_sec=action_timeout_sec,
    )


def load_command_catalog(path: str | Path) -> CommandCatalog:
    """Load a researcher-editable catalog without any generated-code build."""

    catalog_path = Path(path)
    try:
        text = catalog_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CommandCatalogError(f"cannot read command catalog {catalog_path}: {exc}") from exc
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise CommandCatalogError(f"invalid YAML in {catalog_path}: {exc}") from exc
    root = _mapping(document, label="command catalog")
    allowed = {"schema", "commands"}
    unknown = sorted(set(root) - allowed)
    if unknown:
        raise CommandCatalogError(
            f"command catalog has unknown fields: {', '.join(unknown)}"
        )
    if root.get("schema") != CATALOG_SCHEMA:
        raise CommandCatalogError(
            f"command catalog schema must be {CATALOG_SCHEMA!r}"
        )
    raw_commands = root.get("commands")
    if not isinstance(raw_commands, list):
        raise CommandCatalogError("command catalog commands must be a list")
    catalog = CommandCatalog(
        tuple(_parse_command(command, index=index) for index, command in enumerate(raw_commands))
    )
    _validate_retraction_protocol_versions(catalog)
    _validate_execution_proxy_endpoints(catalog)
    return catalog


def _validate_retraction_protocol_versions(catalog: CommandCatalog) -> None:
    """Keep the one versioned retraction transport invariant in the catalog.

    The catalog owns only phrase-to-transport routing.  It must not duplicate
    a particular command's request schema or semantic policy: rosidl request
    construction and the endpoint server already validate the fields they
    receive.  The protocol version is different because it identifies the
    wire contract itself, so every catalog Service using this type stays on
    the v1 request shape.
    """

    for command in catalog.commands:
        if (
            command.kind != "service"
            or command.interface_type != RETRACTION_SERVICE_TYPE
        ):
            continue
        if command.payload.get("protocol_version") != RETRACTION_PROTOCOL_VERSION:
            raise CommandCatalogError(
                "ExecuteRetractionCommand protocol_version is fixed to 1"
            )


def _validate_execution_proxy_endpoints(catalog: CommandCatalog) -> None:
    """Reject catalog edits that bypass execution-owned physical transport.

    The catalog is intentionally free to route ordinary Topics and Services,
    but these two controller-facing ROS types must enter through the fixed
    owner proxy.  The proxy follows the bridge-selected route and preserves
    in-flight/cancel semantics, so a hot YAML edit cannot accidentally pin a
    command directly to external or virtual controller names.
    """

    expected_endpoints = {
        RETRACTION_SERVICE_TYPE: EXECUTION_RETRACTION_PROXY_SERVICE_ENDPOINT,
        TOOL_HANDOVER_ACTION_TYPE: EXECUTION_TOOL_HANDOVER_PROXY_ENDPOINT,
    }
    for command in catalog.commands:
        expected = expected_endpoints.get(command.interface_type)
        if expected is not None and command.endpoint != expected:
            raise CommandCatalogError(
                f"{command.interface_type} must use execution proxy {expected}"
            )


class CommandCatalogReloader:
    """Reload on atomic file replacement and retain the last valid catalog."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.catalog: CommandCatalog | None = None
        self._fingerprint: tuple[int, int] | None = None

    def reload_if_changed(self, *, force: bool = False) -> tuple[bool, str]:
        try:
            stat = self.path.stat()
        except OSError as exc:
            return False, f"cannot stat command catalog {self.path}: {exc}"
        fingerprint = (int(stat.st_mtime_ns), int(stat.st_size))
        if not force and fingerprint == self._fingerprint:
            return False, ""
        try:
            catalog = load_command_catalog(self.path)
        except CommandCatalogError as exc:
            # Keep a previous good catalog. Do not advance the fingerprint so
            # an atomic fix with matching metadata still gets another chance.
            return False, str(exc)
        self.catalog = catalog
        self._fingerprint = fingerprint
        return True, ""


def render_command_payload(template: Mapping[str, Any], command_id: str) -> dict[str, Any]:
    """Render the one supported runtime token without arbitrary evaluation."""

    def render(value: Any) -> Any:
        if isinstance(value, str):
            return value.replace("{command_id}", command_id)
        if isinstance(value, list):
            return [render(item) for item in value]
        if isinstance(value, Mapping):
            return {str(key): render(item) for key, item in value.items()}
        return copy.deepcopy(value)

    return render(dict(template))


class CommandRouter:
    """Pure catalog matcher and request renderer used by the ROS node."""

    def __init__(
        self,
        catalog: CommandCatalog,
        *,
        command_id_factory: Callable[[str], str] | None = None,
    ) -> None:
        self.catalog = catalog
        self._command_id_factory = command_id_factory or new_command_id

    def route(
        self,
        text: object,
        *,
        source: str = "",
        utterance_id: str = "",
    ) -> RoutedCommand | None:
        """Render one exact catalog command.

        Live ASR deliveries carry an immutable source/utterance identity.  Use
        that identity to make a retry of the same delivery reach the endpoint
        with the same command ID, where the controller's idempotency contract
        is authoritative.  The random fallback remains only for local
        developer calls that do not originate from the admitted ASR ingress.
        """

        # Keep the pure router safe when it is exercised outside the ROS node
        # (tests, replay tools, or a future small adapter).  The node performs
        # this same check before deciding whether to forward a miss to the
        # resolver, but a direct exact match must not bypass the one-final /
        # one-operation invariant.
        if self.has_multiple_command_spans(text):
            return None

        command = self.catalog.match(text)
        if command is None:
            final_clause = bounded_final_command_clause(text)
            if final_clause:
                command = self.catalog.match(final_clause)
        if command is None:
            command = self.catalog.match_embedded_keywords(text)
        if command is None:
            return None
        normalized_source = str(source or "").strip()
        normalized_utterance_id = str(utterance_id or "").strip()
        command_id = (
            deterministic_command_id(
                command.command_id_prefix,
                source=normalized_source,
                utterance_id=normalized_utterance_id,
                command_name=command.name,
            )
            if normalized_source and normalized_utterance_id
            else self._command_id_factory(command.command_id_prefix)
        )
        if not isinstance(command_id, str) or not command_id.strip():
            raise CommandCatalogError("command_id factory returned an empty ID")
        return RoutedCommand(
            command=command,
            command_id=command_id,
            endpoint=command.endpoint,
            payload=render_command_payload(command.payload, command_id),
        )

    def has_multiple_command_spans(self, text: object) -> bool:
        """Expose the catalog's pre-dispatch ambiguity check to ROS ingress."""

        return self.catalog.has_multiple_command_spans(text)


def new_command_id(prefix: str) -> str:
    """Generate a readable, collision-resistant controller request identity."""

    day = datetime.now().strftime("%Y%m%d")
    return f"{prefix}-{day}-{uuid.uuid4().hex[:8]}"


def deterministic_command_id(
    prefix: str,
    *,
    source: str,
    utterance_id: str,
    command_name: str,
) -> str:
    """Return the stable endpoint identity for one admitted ASR delivery.

    Keep the readable catalog prefix, but derive the suffix from all semantic
    identity fields.  Including the command name prevents a researcher from
    accidentally making two newly-added catalog commands collide merely
    because they reused a convenience prefix.
    """

    normalized_prefix = str(prefix or "").strip()
    normalized_source = str(source or "").strip()
    normalized_utterance_id = str(utterance_id or "").strip()
    normalized_command_name = str(command_name or "").strip()
    if not all(
        (
            normalized_prefix,
            normalized_source,
            normalized_utterance_id,
            normalized_command_name,
        )
    ):
        raise CommandCatalogError(
            "deterministic command ID requires prefix, source, utterance_id, and command_name"
        )
    digest = hashlib.sha256(
        "\x00".join(
            (
                normalized_source,
                normalized_utterance_id,
                normalized_command_name,
            )
        ).encode("utf-8")
    ).hexdigest()
    return f"{normalized_prefix}-{digest[:20]}"


def default_command_catalog_path() -> Path:
    """Find the installed catalog, falling back to a source checkout for tests."""

    try:
        from ament_index_python.packages import get_package_share_directory

        installed = Path(get_package_share_directory("voice_command")) / "config" / "command_catalog.yaml"
        if installed.is_file():
            return installed
    except Exception:
        pass
    return Path(__file__).resolve().parents[1] / "config" / "command_catalog.yaml"


__all__ = [
    "CATALOG_SCHEMA",
    "DEFAULT_ACTION_TIMEOUT_SEC",
    "EXECUTION_RETRACTION_PROXY_SERVICE_ENDPOINT",
    "EXECUTION_TOOL_HANDOVER_PROXY_ENDPOINT",
    "RETRACTION_PROTOCOL_VERSION",
    "RETRACTION_SERVICE_TYPE",
    "TOOL_HANDOVER_ACTION_TYPE",
    "CatalogCommand",
    "CommandCatalog",
    "CommandCatalogError",
    "CommandCatalogReloader",
    "CommandRouter",
    "bounded_final_command_clause",
    "bounded_command_suffixes",
    "command_context_is_blocked",
    "semantic_command_categories",
    "command_is_reported_or_quoted",
    "deterministic_command_id",
    "RoutedCommand",
    "default_command_catalog_path",
    "load_command_catalog",
    "new_command_id",
    "normalize_command_phrase",
    "render_command_payload",
]
