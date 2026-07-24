"""Shared validation rules for bed robot-arm group commands.

The task planner deliberately addresses logical groups only.  No member count,
physical arm identifier, or mount position belongs in this contract.
"""

from __future__ import annotations

import math


GROUP_SUCTION = "suction"
GROUP_RETRACTION = "retraction"
GROUPS = frozenset({GROUP_SUCTION, GROUP_RETRACTION})

GROUP_STATES = frozenset(
    {
        "offline",
        "standby",
        "suctioning",
        "stopping",
        "retracting",
        "holding",
        "releasing",
        "changing_end_effector",
        "approaching",
        "fault",
    }
)

OPERATION_SUCTION_START = "suction_start"
OPERATION_SUCTION_STOP = "suction_stop"
OPERATION_RETRACTION = "retraction"
OPERATION_RELEASE_RETRACTION = "release_retraction"
OPERATION_CHANGE_END_EFFECTOR = "change_end_effector"

OPERATIONS_BY_GROUP = {
    GROUP_SUCTION: frozenset({OPERATION_SUCTION_START, OPERATION_SUCTION_STOP}),
    GROUP_RETRACTION: frozenset(
        {
            OPERATION_RETRACTION,
            OPERATION_RELEASE_RETRACTION,
            OPERATION_CHANGE_END_EFFECTOR,
        }
    ),
}

DIRECTION_UP = "UP"
DIRECTION_DOWN = "DOWN"
DIRECTION_LEFT = "LEFT"
DIRECTION_RIGHT = "RIGHT"
DIRECTION_LEFT_RIGHT = "LEFT_RIGHT"
DIRECTION_UP_DOWN = "UP_DOWN"
DIRECTIONS = frozenset(
    {
        DIRECTION_UP,
        DIRECTION_DOWN,
        DIRECTION_LEFT,
        DIRECTION_RIGHT,
        DIRECTION_LEFT_RIGHT,
        DIRECTION_UP_DOWN,
    }
)

DISTANCE_EXPLICIT_WITH_UNIT = "explicit_with_unit"
DISTANCE_EXPLICIT_UNIT_INFERRED = "explicit_unit_inferred"
DISTANCE_QUALITATIVE_INFERRED = "qualitative_inferred"
DISTANCE_DEFAULTED = "defaulted"
DISTANCE_ORIGINS = frozenset(
    {
        DISTANCE_EXPLICIT_WITH_UNIT,
        DISTANCE_EXPLICIT_UNIT_INFERRED,
        DISTANCE_QUALITATIVE_INFERRED,
        DISTANCE_DEFAULTED,
    }
)

QUALITATIVE_MIN_MM = 1.0
QUALITATIVE_MAX_MM = 30.0
DEFAULT_DISTANCE_MM = 10.0


def validate_command_values(
    *,
    group_id: str,
    operation: str,
    direction: str,
    distance_mm: float,
    distance_origin: str,
    confidence: float,
) -> str:
    """Return an empty string when a command satisfies the wire contract."""

    if group_id not in GROUPS:
        return f"unsupported group_id '{group_id}'"
    if operation not in OPERATIONS_BY_GROUP[group_id]:
        return f"operation '{operation}' is not valid for group '{group_id}'"
    if not math.isfinite(confidence) or confidence < 0.0 or confidence > 1.0:
        return "confidence must be finite and between 0 and 1"

    if operation != OPERATION_RETRACTION:
        if direction:
            return f"operation '{operation}' must not include a direction"
        if distance_mm != 0.0:
            return f"operation '{operation}' must use distance_mm=0"
        if distance_origin:
            return f"operation '{operation}' must not include a distance_origin"
        return ""

    if direction not in DIRECTIONS:
        return f"direction '{direction}' is not one of the six supported directions"
    if not math.isfinite(distance_mm) or distance_mm <= 0.0:
        return "retraction distance_mm must be finite and greater than zero"
    if distance_origin not in DISTANCE_ORIGINS:
        return f"unsupported distance_origin '{distance_origin}'"
    if distance_origin == DISTANCE_QUALITATIVE_INFERRED and not (
        QUALITATIVE_MIN_MM <= distance_mm <= QUALITATIVE_MAX_MM
    ):
        return "qualitative retraction distance must be between 1 and 30 mm"
    if (
        distance_origin == DISTANCE_QUALITATIVE_INFERRED
        and not float(distance_mm).is_integer()
    ):
        return "qualitative retraction distance must be an integer number of millimetres"
    if distance_origin == DISTANCE_DEFAULTED and distance_mm != DEFAULT_DISTANCE_MM:
        return "defaulted retraction distance must be 10 mm"
    return ""


def mock_safety_rejection(
    *, operation: str, distance_mm: float, max_retraction_mm: float
) -> tuple[str, str]:
    """Return the downstream mock-controller safety rejection, if any.

    Explicit distances are intentionally not clamped by the task planner.  This
    separate check models the lower controller refusing a command such as
    50 mm when its configured safe increment is 30 mm.
    """

    if operation != OPERATION_RETRACTION:
        return "", ""
    if distance_mm <= max_retraction_mm:
        return "", ""
    return (
        "distance_limit_exceeded",
        (
            f"requested {distance_mm:g} mm exceeds mock controller safety "
            f"limit {max_retraction_mm:g} mm"
        ),
    )
