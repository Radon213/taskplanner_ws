"""Shared language normalization for the bed-mounted retraction arm.

Physical arm selection and detailed controller state remain downstream
responsibilities. This module contains only the deterministic direction and
distance semantics used by the Taskplanner retraction lane.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import re


BED_ROBOT_ARM_GROUP_IDS = ("retraction",)
RETRACTION_DIRECTIONS = ("UP", "DOWN", "LEFT", "RIGHT", "LEFT_RIGHT", "UP_DOWN")
DISTANCE_ORIGINS = ("explicit_with_unit",)
MAX_RETRACTION_DISTANCE_MM = 30.0


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


def _validated_contract_distance(value: float, label: str) -> float:
    distance = float(value)
    if not math.isfinite(distance) or distance <= 0.0:
        raise BedRobotArmGroupNormalizationError(f"{label} must be a positive finite number")
    if distance > MAX_RETRACTION_DISTANCE_MM:
        raise BedRobotArmGroupNormalizationError(
            f"{label} exceeds the {MAX_RETRACTION_DISTANCE_MM:g} mm contract limit"
        )
    return distance


def normalize_retraction_distance(
    raw_text: str,
    *,
    qualitative_distance_mm: float | None = None,
) -> DistanceNormalization:
    """Normalize one explicit numeric ``mm``/``cm`` distance.

    The public controller contract does not define a default for missing,
    unitless, or qualitative distances. Such requests are rejected instead of
    being converted into physical motion.
    """
    text = str(raw_text).strip()
    if qualitative_distance_mm is not None:
        raise BedRobotArmGroupNormalizationError(
            "qualitative retraction distances are not permitted; "
            "an explicit numeric mm/cm value is required"
        )

    matches = list(_EXPLICIT_DISTANCE_RE.finditer(text))
    if not matches:
        raise BedRobotArmGroupNormalizationError(
            "retraction distance requires an explicit numeric mm/cm value"
        )
    if len(matches) != 1:
        raise BedRobotArmGroupNormalizationError(
            "retraction request must contain exactly one explicit mm/cm distance"
        )

    explicit = matches[0]
    distance = float(explicit.group("value"))
    unit = explicit.group("unit").lower()
    if unit in {"cm", "㎝", "센치", "센치미터", "센티미터", "씨엠"}:
        distance *= 10.0
    distance = _validated_contract_distance(distance, "distance")
    return DistanceNormalization(
        distance_mm=distance,
        distance_origin="explicit_with_unit",
        raw_distance_text=explicit.group(0).strip(),
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
    proposed = _validated_contract_distance(distance_mm, "proposed distance")
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
