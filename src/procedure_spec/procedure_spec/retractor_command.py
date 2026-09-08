"""ROS-independent normalization and admission tracking for retractor commands.

The module deliberately produces only a closed command vocabulary.  It does
not call ROS, infer a physical arm, or assert that a controller completed a
motion.  ``apply_retractor_service_admission`` advances the local state only
after the Service server says that it accepted the Request.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
import re
import unicodedata


DEFAULT_ADJUSTMENT_DISTANCE_M = 0.050
_MAX_ADJUSTMENT_DISTANCE_M = 0.050


class RetractionCommand(str, Enum):
    """Closed command vocabulary sent to the retractor Service."""

    START_DIRECT_TEACH = "start_direct_teach"
    FINISH_DIRECT_TEACH = "finish_direct_teach"
    START_RETRACTION = "start_retraction"
    ADJUST_RETRACTION = "adjust_retraction"
    CHANGE_TOOL = "change_tool"
    STOP_RETRACTION = "stop_retraction"


class RetractionTargetSide(str, Enum):
    """Wire-level retraction target side."""

    NONE = "none"
    LEFT = "left"
    RIGHT = "right"
    BOTH = "both"


class RetractionState(str, Enum):
    """Local admission state, not a claim about physical controller state."""

    IDLE = "idle"
    DIRECT_TEACHING = "direct_teaching"
    TAUGHT_READY = "taught_ready"
    RETRACTION_ACTIVE = "retraction_active"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class NormalizedRetractionCommand:
    """One normalized command, or a rejection represented by ``command=None``."""

    command: RetractionCommand | None
    target_side: RetractionTargetSide
    distance_m: float
    confidence: float
    reason: str


_ALLOWED_COMMANDS: dict[RetractionState, frozenset[RetractionCommand]] = {
    RetractionState.IDLE: frozenset(
        {
            RetractionCommand.START_DIRECT_TEACH,
            RetractionCommand.CHANGE_TOOL,
        }
    ),
    RetractionState.DIRECT_TEACHING: frozenset(
        {
            RetractionCommand.FINISH_DIRECT_TEACH,
        }
    ),
    RetractionState.TAUGHT_READY: frozenset(
        {
            RetractionCommand.START_DIRECT_TEACH,
            RetractionCommand.START_RETRACTION,
        }
    ),
    RetractionState.RETRACTION_ACTIVE: frozenset(
        {
            RetractionCommand.ADJUST_RETRACTION,
            RetractionCommand.STOP_RETRACTION,
        }
    ),
    RetractionState.UNKNOWN: frozenset(),
}

_ACCEPTED_TRANSITIONS: dict[
    tuple[RetractionState, RetractionCommand], RetractionState
] = {
    (
        RetractionState.IDLE,
        RetractionCommand.START_DIRECT_TEACH,
    ): RetractionState.DIRECT_TEACHING,
    (
        RetractionState.IDLE,
        RetractionCommand.CHANGE_TOOL,
    ): RetractionState.IDLE,
    (
        RetractionState.DIRECT_TEACHING,
        RetractionCommand.FINISH_DIRECT_TEACH,
    ): RetractionState.TAUGHT_READY,
    (
        RetractionState.TAUGHT_READY,
        RetractionCommand.START_DIRECT_TEACH,
    ): RetractionState.DIRECT_TEACHING,
    (
        RetractionState.TAUGHT_READY,
        RetractionCommand.START_RETRACTION,
    ): RetractionState.RETRACTION_ACTIVE,
    (
        RetractionState.RETRACTION_ACTIVE,
        RetractionCommand.STOP_RETRACTION,
    ): RetractionState.IDLE,
}

_DIRECT_TEACH_TERMS = (
    "직접교시",
    # Common final-STT repair for ``교시`` -> ``교실``.  Keep the ``직접``
    # anchor so an unrelated classroom sentence cannot become a robot command.
    "직접교실",
    "직접오씨",
    "직접oc",
    "직접기시",
    "직접교수",
    "다이렉트티치",
    "다이렉트티칭",
    "directteach",
    "directteech",
    "directteaching",
    "teaching",
    "가르치기",
    "가르치",
    "가르쳐",
    "가르쳤",
    "교시",
)
_RETRACTION_TERMS = (
    "아미",
    "아미네이비",
    "리트랙션",
    "리트렉션",
    "리트랙숀",
    "리트렉숀",
    "리트락션",
    "리트락숀",
    "리트랙터",
    "리트렉터",
    "리트랙타",
    "리트렉타",
    "견인",
    "retraction",
    "retration",
    "retracton",
    "retractoin",
    "retracion",
    "retraccion",
    "retractor",
    "retrator",
    "retract",
    "army",
    "armynavy",
)
_TOOL_CHANGE_TERMS = (
    "툴체인지",
    "툴체인",
    "툴교체",
    "툴교환",
    "툴바꿔",
    "툴을바꿔",
    "툴을교체",
    "툴을교환",
    "도구교체",
    "도구교환",
    "도구변경",
    "도구바꿔",
    "도구를바꿔",
    "도구를교환",
    "도구로교환",
    "새도구",
    "새도구로교환",
    "다른도구",
    "기구교체",
    "기구교환",
    "기구바꿔",
    "기구를바꿔",
    "새기구",
    "다른기구",
    "장비바꿔",
    "장비를바꿔",
    "장비교체",
    "장비교환",
    "장비로바꿔",
    "장비를교환",
    "새장비",
    "다른장비",
    "엔드이펙터교체",
    "엔드이펙터교환",
    "엔드이펙터바꿔",
    "엔드이펙터를바꿔",
    "toolchange",
    "toolchage",
    "toolcheange",
    "switchtool",
    "changetool",
    "endeffectorchange",
    "endeffecterchange",
)
_START_TERMS = ("시작", "개시", "start")
_KOREAN_START_TERMS = ("시작", "개시")
_STOP_TERMS = (
    "종료",
    "완료",
    "끝",
    "그만",
    "중지",
    "멈춰",
    "멈추",
    "해제",
    "마치",
    "마칠",
    "마쳐",
    "마무리",
    "스톱",
    "스탑",
    "끝내",
    "끝낼",
    "stop",
    "end",
    "finish",
    "done",
)
_KOREAN_STOP_TERMS = (
    "종료",
    "완료",
    "끝",
    "그만",
    "중지",
    "멈춰",
    "멈추",
    "해제",
    "마치",
    "마칠",
    "마쳐",
    "마무리",
    "스톱",
    "스탑",
    "끝내",
    "끝낼",
)
_ADJUSTMENT_TERMS = (
    "더",
    "추가",
    "조정",
    "이동",
    "당겨",
    "당기",
    "땡겨",
    "땡기",
    "끌어",
    "밀어",
    "조금",
    "살짝",
    "한번더",
    "한번만더",
    "조금더",
    "좀더",
    "미세조정",
    "more",
    "adjust",
    "move",
    "pull",
    "shift",
)
_LESS_PULL_TERMS = (
    "덜당겨",
    "덜당기",
    "덜땡겨",
    "덜땡기",
    "lesspull",
    "pullless",
)
_LEFT_TERMS = (
    "왼쪽",
    "왠쪽",
    "왼편",
    "왼팔",
    "왼쪽팔",
    "좌측",
    "좌방",
    "레프트",
    "left",
)
_RIGHT_TERMS = (
    "오른쪽",
    "오룬쪽",
    "오른편",
    "오른팔",
    "오른쪽팔",
    "우측",
    "우방",
    "라이트",
    "right",
)
_BILATERAL_TERMS = ("양쪽", "양팔", "좌우", "both", "bilateral")

_EXPLICIT_DISTANCE_RE = re.compile(
    r"(?<![\d.])(?P<value>[+-]?(?:\d+(?:\.\d+)?|\.\d+))\s*"
    r"(?P<unit>"
    r"millimet(?:er|re)s?|mm|밀\s*리\s*미\s*터|밀\s*리|미\s*리|"
    r"centimet(?:er|re)s?|cm|센\s*티\s*미\s*터|센\s*치\s*미\s*터|"
    r"센\s*티|센\s*치|씨\s*엠|"
    r"met(?:er|re)s?|m"
    r")(?![a-z0-9.])",
    re.IGNORECASE,
)
_KOREAN_DISTANCE_RE = re.compile(
    r"(?<![가-힣])(?P<value>다섯|여섯|일곱|여덟|아홉|열|십|영|공|한|일|두|이|세|삼|네|사|오|육|칠|팔|구)\s*"
    r"(?P<unit>"
    r"밀\s*리\s*미\s*터|밀\s*리|미\s*리|"
    r"센\s*티\s*미\s*터|센\s*치\s*미\s*터|센\s*티|센\s*치|"
    r"미\s*터"
    r")(?![가-힣])"
)
_KOREAN_DISTANCE_VALUES = {
    "영": 0,
    "공": 0,
    "한": 1,
    "일": 1,
    "두": 2,
    "이": 2,
    "세": 3,
    "삼": 3,
    "네": 4,
    "사": 4,
    "다섯": 5,
    "오": 5,
    "여섯": 6,
    "육": 6,
    "일곱": 7,
    "칠": 7,
    "여덟": 8,
    "팔": 8,
    "아홉": 9,
    "구": 9,
    "열": 10,
    "십": 10,
}
_MINUS_SIGN = "\N{MINUS SIGN}"
_PRESERVED_MINUS = "\ue000"
_PRESERVED_PLUS = "\ue001"
_SIGNED_NUMBER_MARKER_RE = re.compile(r"(?P<sign>[+-])\s*(?=(?:\d|\.\d))")


def _canonicalize(value: object) -> tuple[str, str]:
    """Return a spaced and whitespace-free form tolerant of STT spacing."""

    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = text.replace("\u200b", "").replace("\u00a0", " ")
    text = text.replace(_MINUS_SIGN, "-")
    text = re.sub(r"(?<=\d)\s*,\s*(?=\d)", ".", text)
    text = _SIGNED_NUMBER_MARKER_RE.sub(
        lambda match: (
            _PRESERVED_MINUS if match.group("sign") == "-" else _PRESERVED_PLUS
        ),
        text,
    )
    text = re.sub(
        rf"[^0-9a-z가-힣.{_PRESERVED_MINUS}{_PRESERVED_PLUS}]+", " ", text
    )
    text = text.replace(_PRESERVED_MINUS, "-").replace(_PRESERVED_PLUS, "+")
    spaced = re.sub(r"\s+", " ", text).strip()
    return spaced, re.sub(r"\s+", "", spaced)


def _contains_english_term(spaced: str, value: str) -> bool:
    """Match an English term as a token while tolerating STT-inserted spaces."""

    letters_with_optional_spaces = r"\s*".join(re.escape(letter) for letter in value)
    return bool(
        re.search(
            rf"(?<![a-z]){letters_with_optional_spaces}(?![a-z])",
            spaced,
        )
    )


def _contains_any(compact: str, spaced: str, values: tuple[str, ...]) -> bool:
    """Use compact matching for Korean and token matching for English terms."""

    return any(
        _contains_english_term(spaced, value)
        if value.isascii() and value.isalpha()
        else value in compact
        for value in values
    )


def _coerce_state(value: RetractionState | str) -> RetractionState:
    if isinstance(value, RetractionState):
        return value
    _, compact = _canonicalize(value)
    aliases = {
        "idle": RetractionState.IDLE,
        "대기": RetractionState.IDLE,
        "directteaching": RetractionState.DIRECT_TEACHING,
        "directteach": RetractionState.DIRECT_TEACHING,
        "직접교시중": RetractionState.DIRECT_TEACHING,
        "taughtready": RetractionState.TAUGHT_READY,
        "teachready": RetractionState.TAUGHT_READY,
        "교시완료": RetractionState.TAUGHT_READY,
        "retractionactive": RetractionState.RETRACTION_ACTIVE,
        "retracting": RetractionState.RETRACTION_ACTIVE,
        "견인중": RetractionState.RETRACTION_ACTIVE,
        "unknown": RetractionState.UNKNOWN,
    }
    return aliases.get(compact, RetractionState.UNKNOWN)


def _coerce_command(value: RetractionCommand | str | None) -> RetractionCommand | None:
    if isinstance(value, RetractionCommand):
        return value
    if value is None:
        return None
    try:
        return RetractionCommand(str(value).strip().lower())
    except ValueError:
        return None


def _target_side(
    compact: str,
    spaced: str,
    *,
    allow_bilateral: bool = False,
) -> tuple[RetractionTargetSide | None, str]:
    left = _contains_any(compact, spaced, _LEFT_TERMS)
    right = _contains_any(compact, spaced, _RIGHT_TERMS)
    bilateral = _contains_any(compact, spaced, _BILATERAL_TERMS)
    # A bilateral selector is a physical target in the adjustment contract,
    # but it is not a valid direct-teach completion selector.  Also reject
    # contradictory wording such as "양쪽 오른쪽" instead of silently
    # preferring one arm.
    if left and right or (bilateral and (left or right)):
        return None, "ambiguous_target_side"
    if bilateral:
        if allow_bilateral:
            return RetractionTargetSide.BOTH, ""
        return None, "ambiguous_target_side"
    if left:
        return RetractionTargetSide.LEFT, ""
    if right:
        return RetractionTargetSide.RIGHT, ""
    return None, "adjustment_side_missing"


def _distance_m(spaced: str) -> tuple[float | None, str]:
    numeric_matches = list(_EXPLICIT_DISTANCE_RE.finditer(spaced))
    korean_matches = list(_KOREAN_DISTANCE_RE.finditer(spaced))
    if len(numeric_matches) + len(korean_matches) > 1:
        return None, "multiple_adjustment_distances"
    if numeric_matches:
        match = numeric_matches[0]
        value = float(match.group("value"))
        unit = re.sub(r"\s+", "", match.group("unit").casefold())
    elif korean_matches:
        match = korean_matches[0]
        value = float(_KOREAN_DISTANCE_VALUES[match.group("value")])
        unit = re.sub(r"\s+", "", match.group("unit").casefold())
    else:
        value = None
        unit = ""
    if value is not None:
        if not math.isfinite(value) or value <= 0.0:
            return None, "invalid_adjustment_distance"
        if unit in {"mm", "millimeter", "millimeters", "millimetre", "millimetres", "밀리미터", "밀리", "미리"}:
            value /= 1_000.0
        elif unit in {"cm", "centimeter", "centimeters", "centimetre", "centimetres", "센티미터", "센치미터", "센티", "센치", "씨엠"}:
            value /= 100.0
        if (
            not math.isfinite(value)
            or value <= 0.0
            or value > _MAX_ADJUSTMENT_DISTANCE_M
        ):
            return None, "invalid_adjustment_distance"
        return value, "explicit_adjustment_distance"
    if re.search(r"\d", spaced):
        return None, "adjustment_distance_unit_missing"
    return DEFAULT_ADJUSTMENT_DISTANCE_M, "default_adjustment_distance"


def _is_less_pull_intent(compact: str, spaced: str) -> bool:
    """Return whether a pull adjustment explicitly asks for less tension.

    The sign is derived from an explicit lexical cue rather than accepting a
    spoken negative number.  This keeps malformed/signed measurements
    rejected while encoding ``1 cm 덜 당겨줘`` as the existing adjustment
    command with ``distance_m=-0.01``.
    """

    return _contains_any(compact, spaced, _LESS_PULL_TERMS)


def _is_adjustment_intent(
    compact: str,
    spaced: str,
    state: RetractionState,
) -> bool:
    retraction_named = _contains_any(compact, spaced, _RETRACTION_TERMS)
    bilateral_named = _contains_any(compact, spaced, _BILATERAL_TERMS)
    active_context = state == RetractionState.RETRACTION_ACTIVE
    # A bilateral command is often spoken without repeating the noun after the
    # retraction mode is established ("양쪽으로 1mm씩 당겨줘").  Its explicit
    # bilateral selector remains a sufficient closed-vocabulary anchor even in
    # the scenario-free Debug bypass; normal state admission still rejects it
    # outside RETRACTION_ACTIVE.
    if not (retraction_named or active_context or bilateral_named):
        return False
    has_marker = _contains_any(compact, spaced, _ADJUSTMENT_TERMS)
    has_explicit_distance = bool(
        _EXPLICIT_DISTANCE_RE.search(spaced)
        or _KOREAN_DISTANCE_RE.search(spaced)
    )
    has_number = bool(re.search(r"\d", spaced))
    # State may supply the omitted word "retraction", but a spatial phrase
    # alone (for example "오른쪽 절개 부위 5 cm") is not motion intent.  When
    # the retractor itself is not named, retain an explicit movement cue.
    if active_context and not retraction_named:
        return has_marker
    if bilateral_named and not retraction_named:
        return has_marker or has_explicit_distance or has_number
    return has_marker or has_explicit_distance or has_number


def _is_bare_korean_lifecycle_utterance(
    compact: str,
    cue_terms: tuple[str, ...],
    *,
    allow_target_side: bool = False,
) -> bool:
    """Accept short state-context verbs without swallowing another task.

    The demo permits compact phrases such as "시작해" and "이제 끝".  Merely
    finding the same verb inside "봉합은 여기서 끝내" must not stop the
    retractor, so after removing a lifecycle cue only a small set of polite
    fillers may remain.
    """

    remainder = compact
    for term in sorted(cue_terms, key=len, reverse=True):
        if not term.isascii():
            remainder = remainder.replace(term, "")
    if allow_target_side:
        # In direct-teach state a final-STT result such as ``오른쪽 완료`` is a
        # common elliptical form of ``오른쪽 직접 교시 종료``.  Only remove a
        # single explicit side selector here; the caller still grounds it
        # through ``_target_side`` and bilateral/contradictory wording remains
        # rejected.  This option is intentionally not used for retraction stop
        # so an ambient ``오른쪽 끝`` cannot stop an active robot.
        for term in sorted((*_LEFT_TERMS, *_RIGHT_TERMS), key=len, reverse=True):
            if not term.isascii():
                remainder = remainder.replace(term, "")
    for filler in (
        "이제",
        "그럼",
        "그러면",
        "자",
        "좀",
        "해줘",
        "해주세요",
        "해",
        "줘",
        "주세요",
        "할게",
        "게요",
        "게",
        "하자",
        "요",
    ):
        remainder = remainder.replace(filler, "")
    return not remainder


def _detected_command(
    compact: str,
    spaced: str,
    state: RetractionState,
) -> tuple[RetractionCommand | None, str]:
    commands: set[RetractionCommand] = set()
    direct_teach_named = _contains_any(compact, spaced, _DIRECT_TEACH_TERMS)
    retraction_named = _contains_any(compact, spaced, _RETRACTION_TERMS)
    has_start = _contains_any(compact, spaced, _START_TERMS)
    has_stop = _contains_any(compact, spaced, _STOP_TERMS)

    if direct_teach_named:
        if has_stop:
            commands.add(RetractionCommand.FINISH_DIRECT_TEACH)
        else:
            commands.add(RetractionCommand.START_DIRECT_TEACH)

    if retraction_named:
        if has_stop:
            commands.add(RetractionCommand.STOP_RETRACTION)
        elif has_start:
            commands.add(RetractionCommand.START_RETRACTION)
        elif state == RetractionState.TAUGHT_READY and not _is_adjustment_intent(
            compact, spaced, state
        ):
            commands.add(RetractionCommand.START_RETRACTION)

    if _contains_any(compact, spaced, _TOOL_CHANGE_TERMS):
        commands.add(RetractionCommand.CHANGE_TOOL)

    if _is_adjustment_intent(compact, spaced, state):
        commands.add(RetractionCommand.ADJUST_RETRACTION)

    # In demo operation the state machine supplies the omitted noun when the
    # utterance still contains an unambiguous lifecycle verb.  This lets common
    # STT results such as "시작해" or "이제 끝" succeed without expanding the
    # closed command vocabulary.
    if not commands:
        # Bare lifecycle verbs are intentionally Korean-only.  They support
        # compact STT results such as "시작해" and "이제 끝", while avoiding an
        # unrelated English sentence such as "please start the note".
        has_korean_start = _contains_any(
            compact, spaced, _KOREAN_START_TERMS
        ) and _is_bare_korean_lifecycle_utterance(compact, _KOREAN_START_TERMS)
        has_korean_stop = _contains_any(
            compact, spaced, _KOREAN_STOP_TERMS
        ) and _is_bare_korean_lifecycle_utterance(compact, _KOREAN_STOP_TERMS)
        if has_korean_start and state == RetractionState.IDLE:
            commands.add(RetractionCommand.START_DIRECT_TEACH)
        elif (
            _contains_any(compact, spaced, _KOREAN_STOP_TERMS)
            and _is_bare_korean_lifecycle_utterance(
                compact,
                _KOREAN_STOP_TERMS,
                allow_target_side=True,
            )
            and state == RetractionState.DIRECT_TEACHING
        ):
            commands.add(RetractionCommand.FINISH_DIRECT_TEACH)
        elif has_korean_start and state == RetractionState.TAUGHT_READY:
            commands.add(RetractionCommand.START_RETRACTION)
        elif has_korean_stop and state == RetractionState.RETRACTION_ACTIVE:
            commands.add(RetractionCommand.STOP_RETRACTION)

    if not commands:
        return None, "no_supported_command"
    if len(commands) != 1:
        return None, "ambiguous_command"
    return next(iter(commands)), ""


def _rejected(reason: str) -> NormalizedRetractionCommand:
    return NormalizedRetractionCommand(
        command=None,
        target_side=RetractionTargetSide.NONE,
        distance_m=0.0,
        confidence=0.0,
        reason=reason,
    )


def allowed_retractor_commands(
    current_state: RetractionState | str,
) -> frozenset[RetractionCommand]:
    """Return the only commands that may be emitted from the local state."""

    return _ALLOWED_COMMANDS[_coerce_state(current_state)]


def normalize_retractor_adjustment_parameters(
    transcript: str,
) -> NormalizedRetractionCommand:
    """Ground only an adjustment's physical side and distance in raw text.

    This helper deliberately does not infer whether the surgeon intended an
    adjustment.  It exists so a text model may classify a fuzzy utterance while
    the two physical parameters still come exclusively from the STT evidence.
    One side or the bilateral target is required; an omitted distance uses the
    reviewed 5 cm demo default, and malformed/signed/out-of-range values remain
    rejected.  ``both`` means the same distance is applied to each arm.
    """

    spaced, compact = _canonicalize(transcript)
    if not compact:
        return _rejected("empty_transcript")
    target_side, reason = _target_side(compact, spaced, allow_bilateral=True)
    if target_side is None:
        return _rejected(reason)
    distance_m, distance_reason = _distance_m(spaced)
    if distance_m is None:
        return _rejected(distance_reason)
    less_pull = _is_less_pull_intent(compact, spaced)
    if less_pull:
        distance_m = -distance_m
    return NormalizedRetractionCommand(
        command=RetractionCommand.ADJUST_RETRACTION,
        target_side=target_side,
        distance_m=distance_m,
        confidence=(
            0.96
            if distance_reason == "explicit_adjustment_distance"
            else 0.90
        ),
        reason=(
            f"grounded_adjust_retraction_{distance_reason}"
            + ("_less_pull" if less_pull else "")
        ),
    )


def normalize_retractor_command(
    transcript: str,
    current_state: RetractionState | str,
    *,
    enforce_state: bool = True,
) -> NormalizedRetractionCommand:
    """Normalize one STT transcript without guessing a missing adjustment side.

    The function is deterministic.  A rejected or ambiguous transcript has
    ``command=None`` and a machine-readable ``reason`` so callers cannot send a
    partially inferred physical command. ``enforce_state=False`` is reserved
    for the scenario-free Debug test harness; it keeps the closed vocabulary
    and parameter grounding but omits only the local lifecycle admission gate.
    """

    spaced, compact = _canonicalize(transcript)
    if not compact:
        return _rejected("empty_transcript")

    state = _coerce_state(current_state)
    if enforce_state and state == RetractionState.UNKNOWN:
        return _rejected("state_unknown")

    command, reason = _detected_command(compact, spaced, state)
    if command is None:
        return _rejected(reason)

    target_side = RetractionTargetSide.NONE
    distance_m = 0.0
    confidence = 0.98
    success_reason = f"normalized_{command.value}"
    if command == RetractionCommand.ADJUST_RETRACTION:
        grounded_adjustment = normalize_retractor_adjustment_parameters(transcript)
        if grounded_adjustment.command is None:
            return grounded_adjustment
        target_side = grounded_adjustment.target_side
        distance_m = grounded_adjustment.distance_m
        confidence = grounded_adjustment.confidence
        success_reason = grounded_adjustment.reason.replace(
            "grounded_", "normalized_", 1
        )
    elif command == RetractionCommand.FINISH_DIRECT_TEACH:
        # Finishing direct teach may optionally identify one arm.  No side is
        # still valid and remains the neutral value for a controller-wide
        # finish request; bilateral/contradictory side wording is rejected.
        finish_side, side_reason = _target_side(compact, spaced)
        if side_reason == "ambiguous_target_side":
            return _rejected(side_reason)
        if finish_side is not None:
            target_side = finish_side

    if enforce_state and command not in allowed_retractor_commands(state):
        return _rejected(f"command_not_allowed_in_{state.value}")

    return NormalizedRetractionCommand(
        command=command,
        target_side=target_side,
        distance_m=distance_m,
        confidence=confidence,
        reason=success_reason,
    )


def apply_retractor_service_admission(
    current_state: RetractionState | str,
    command: RetractionCommand | str | None,
    request_accepted: bool,
) -> RetractionState:
    """Advance local state only after the retractor Service admits a command.

    An admission is not evidence of physical execution or completion.  Unknown,
    rejected, malformed, or state-disallowed commands leave the state unchanged.
    """

    state = _coerce_state(current_state)
    normalized_command = _coerce_command(command)
    if (
        not request_accepted
        or state == RetractionState.UNKNOWN
        or normalized_command is None
        or normalized_command not in allowed_retractor_commands(state)
    ):
        return state
    return _ACCEPTED_TRANSITIONS.get((state, normalized_command), state)
