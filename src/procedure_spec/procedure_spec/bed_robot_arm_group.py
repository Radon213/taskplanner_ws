"""Shared language normalization for logical bed-robot-arm groups.

The task planner deliberately knows only the ``suction`` and ``retraction``
groups.  It never resolves a request to a physical arm.  This module contains
the deterministic part of the retraction wire contract so VLM, BT, and test
code can validate the same direction and distance semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import re


BED_ROBOT_ARM_GROUP_IDS = ("suction", "retraction")
RETRACTION_DIRECTIONS = ("UP", "DOWN", "LEFT", "RIGHT", "LEFT_RIGHT", "UP_DOWN")
DISTANCE_ORIGINS = (
    "explicit_with_unit",
    "explicit_unit_inferred",
    "qualitative_inferred",
    "defaulted",
)

DEFAULT_RETRACTION_DISTANCE_MM = 10.0
MIN_QUALITATIVE_DISTANCE_MM = 1.0
MAX_QUALITATIVE_DISTANCE_MM = 30.0


class BedRobotArmGroupNormalizationError(ValueError):
    """Raised when a group-level retraction request cannot be normalized."""


@dataclass(frozen=True, slots=True)
class DistanceNormalization:
    distance_mm: float
    distance_origin: str
    raw_distance_text: str


@dataclass(frozen=True, slots=True)
class RetractionNormalization:
    direction: str
    distance_mm: float
    distance_origin: str
    raw_distance_text: str


_DIRECTION_ALIAS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "LEFT_RIGHT",
        re.compile(
            r"좌\s*우(?:\s*(?:동시|동시에))?|"
            r"왼쪽\s*[,·/와과및하고 ]+\s*오른쪽(?:\s*(?:동시|동시에))?|"
            r"오른쪽\s*[,·/와과및하고 ]+\s*왼쪽(?:\s*(?:동시|동시에))?|"
            r"양\s*(?:쪽|옆)"
        ),
    ),
    (
        "UP_DOWN",
        re.compile(
            r"상\s*하(?:\s*(?:동시|동시에))?|"
            r"위\s*[,·/와과및하고 ]+\s*아래(?:\s*(?:동시|동시에))?|"
            r"아래\s*[,·/와과및하고 ]+\s*위(?:\s*(?:동시|동시에))?"
        ),
    ),
    (
        "UP",
        re.compile(
            r"위쪽|위로|상방|상측|"
            r"(?:^|[\s,])위(?=$|[\s,])|"
            r"(?:^|[\s,])상(?:으로|쪽)?(?=$|[\s,])"
        ),
    ),
    (
        "DOWN",
        re.compile(
            r"아래쪽|아래로|하방|하측|"
            r"(?:^|[\s,])아래(?=$|[\s,])|"
            r"(?:^|[\s,])하(?:로|쪽)?(?=$|[\s,])"
        ),
    ),
    ("LEFT", re.compile(r"왼쪽|좌측|좌방|(?:^|[\s,])좌(?:로|쪽)?(?=$|[\s,])")),
    ("RIGHT", re.compile(r"오른쪽|우측|우방|(?:^|[\s,])우(?:로|쪽)?(?=$|[\s,])")),
)

_EXPLICIT_DISTANCE_RE = re.compile(
    r"(?<![\d.])(?P<value>[+-]?(?:\d+(?:\.\d+)?|\.\d+))\s*"
    r"(?P<unit>센티미터|센치미터|밀리미터|센치|씨엠|밀리|cm|mm|㎝|㎜)",
    re.IGNORECASE,
)
_UNITLESS_DISTANCE_RE = re.compile(
    r"(?<![\d.])(?P<value>[+-]?(?:\d+(?:\.\d+)?|\.\d+))(?![\d.])"
)

# Match longer phrases first so "아주 많이" is not reduced to "많이".
_QUALITATIVE_ANCHORS: tuple[tuple[float, tuple[str, ...]], ...] = (
    (30.0, ("아주 많이", "최대한")),
    (1.0, ("아주 살짝", "미세하게")),
    (10.0, ("조금 더", "좀 더")),
    (20.0, ("많이",)),
    (5.0, ("살짝",)),
    (10.0, ("조금", "좀")),
)

# Non-anchor degree language may be interpolated by VLM, but a numeric VLM
# value is never allowed to manufacture a qualitative expression that the
# surgeon did not actually say.  Keep the cue vocabulary deliberately about
# magnitude/intensity; a generic verb such as ``당겨줘`` is not a cue.
_QUALITATIVE_CUE_RE = re.compile(
    r"아주|미세|살짝|조금|좀|많이|최대한|약간|적당|중간\s*정도|"
    r"약하게|강하게|세게|덜|더"
)


def normalize_retraction_direction(direction: str) -> str:
    """Validate and canonicalize one of the six direction wire enums."""
    normalized = str(direction).strip().upper()
    if normalized not in RETRACTION_DIRECTIONS:
        allowed = ", ".join(RETRACTION_DIRECTIONS)
        raise BedRobotArmGroupNormalizationError(
            f"unsupported retraction direction '{direction}'; expected one of {allowed}"
        )
    return normalized


def infer_retraction_direction(raw_text: str) -> str | None:
    """Return a direction enum when the Korean utterance states one explicitly."""
    text = str(raw_text).strip()
    for direction, pattern in _DIRECTION_ALIAS_PATTERNS:
        if pattern.search(text):
            return direction
    return None


def _validated_positive_distance(value: float, label: str) -> float:
    distance = float(value)
    if not math.isfinite(distance) or distance <= 0.0:
        raise BedRobotArmGroupNormalizationError(f"{label} must be a positive finite number")
    return distance


def _qualitative_anchor(raw_text: str) -> tuple[float, str] | None:
    for distance_mm, aliases in _QUALITATIVE_ANCHORS:
        for alias in aliases:
            if alias in raw_text:
                return distance_mm, alias
    return None


def normalize_retraction_distance(
    raw_text: str,
    *,
    qualitative_distance_mm: float | None = None,
) -> DistanceNormalization:
    """Normalize a spoken distance using the agreed precedence rules.

    Explicit values are never clamped.  An explicit ``cm`` value is converted
    to millimetres, while a number without a unit is interpreted as mm.  Exact
    qualitative anchors map to 1/5/10/20/30 mm.  A VLM may supply another
    qualitative value, but only inside 1..30 mm.  With no distance expression,
    the default is 10 mm.
    """
    text = str(raw_text).strip()
    explicit = _EXPLICIT_DISTANCE_RE.search(text)
    if explicit:
        distance = _validated_positive_distance(float(explicit.group("value")), "distance")
        unit = explicit.group("unit").lower()
        if unit in {"cm", "㎝", "센치", "센치미터", "센티미터", "씨엠"}:
            distance *= 10.0
        return DistanceNormalization(
            distance_mm=distance,
            distance_origin="explicit_with_unit",
            raw_distance_text=explicit.group(0).strip(),
        )

    unitless = _UNITLESS_DISTANCE_RE.search(text)
    if unitless:
        distance = _validated_positive_distance(float(unitless.group("value")), "distance")
        return DistanceNormalization(
            distance_mm=distance,
            distance_origin="explicit_unit_inferred",
            raw_distance_text=unitless.group(0).strip(),
        )

    anchor = _qualitative_anchor(text)
    if anchor:
        distance, phrase = anchor
        return DistanceNormalization(
            distance_mm=distance,
            distance_origin="qualitative_inferred",
            raw_distance_text=phrase,
        )

    if qualitative_distance_mm is not None:
        if not text or _QUALITATIVE_CUE_RE.search(text) is None:
            raise BedRobotArmGroupNormalizationError(
                "qualitative distance requires a spoken intensity expression"
            )
        distance = _validated_positive_distance(qualitative_distance_mm, "qualitative distance")
        if not MIN_QUALITATIVE_DISTANCE_MM <= distance <= MAX_QUALITATIVE_DISTANCE_MM:
            raise BedRobotArmGroupNormalizationError(
                "qualitative distance must be between 1 and 30 mm"
            )
        if not distance.is_integer():
            raise BedRobotArmGroupNormalizationError(
                "qualitative distance must be an integer number of millimetres"
            )
        return DistanceNormalization(
            distance_mm=distance,
            distance_origin="qualitative_inferred",
            raw_distance_text=text,
        )

    return DistanceNormalization(
        distance_mm=DEFAULT_RETRACTION_DISTANCE_MM,
        distance_origin="defaulted",
        raw_distance_text="",
    )


def normalize_retraction_request(
    raw_text: str,
    *,
    vlm_direction: str = "",
    qualitative_distance_mm: float | None = None,
) -> RetractionNormalization:
    """Normalize one group-level retraction request without choosing an arm."""
    spoken_direction = infer_retraction_direction(raw_text)
    direction = spoken_direction or normalize_retraction_direction(vlm_direction)
    distance = normalize_retraction_distance(
        raw_text,
        qualitative_distance_mm=qualitative_distance_mm,
    )
    return RetractionNormalization(
        direction=direction,
        distance_mm=distance.distance_mm,
        distance_origin=distance.distance_origin,
        raw_distance_text=distance.raw_distance_text,
    )


def validate_retraction_distance_proposal(
    *,
    raw_distance_text: str,
    distance_mm: float,
    distance_origin: str,
) -> DistanceNormalization:
    """Deterministically re-check distance fields emitted by VLM schema v4."""
    origin = str(distance_origin).strip()
    if origin not in DISTANCE_ORIGINS:
        raise BedRobotArmGroupNormalizationError(f"unsupported distance origin '{origin}'")
    proposed = _validated_positive_distance(distance_mm, "proposed distance")

    if origin == "qualitative_inferred":
        normalized = normalize_retraction_distance(
            raw_distance_text,
            qualitative_distance_mm=proposed,
        )
    else:
        normalized = normalize_retraction_distance(raw_distance_text)

    if normalized.distance_origin != origin:
        raise BedRobotArmGroupNormalizationError(
            f"distance origin mismatch: text implies {normalized.distance_origin}, proposal says {origin}"
        )
    if not math.isclose(normalized.distance_mm, proposed, rel_tol=0.0, abs_tol=1e-6):
        raise BedRobotArmGroupNormalizationError(
            f"distance mismatch: text implies {normalized.distance_mm:g} mm, proposal says {proposed:g} mm"
        )
    return normalized
