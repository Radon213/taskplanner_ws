"""Profile-bound force-jog planning with one SI-to-SDK unit conversion."""

from __future__ import annotations

from dataclasses import dataclass
import math
from numbers import Real

from ..command_models import (
    AlgorithmValidationError,
    ErrorCategory,
    ErrorCode,
    TargetSide,
)
from ..profile_loader import ExecutionProfile, JogLimits, SideMapping


@dataclass(frozen=True, slots=True)
class ForceJogPlan:
    side: TargetSide
    arm_id: str
    role_instance_id: str
    sensor_id: str | int
    axis: str
    sign: int
    frame: str
    requested_distance_m: float
    distance_mm: float
    signed_distance_mm: float
    previous_cumulative_mm: float
    cumulative_distance_mm: float

    @property
    def role(self) -> str:
        return self.role_instance_id


def _finite(value: Real, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise AlgorithmValidationError(
            ErrorCode.INVALID_NUMERIC_VALUE,
            f"{name} must be a real number",
            field=name,
        )
    converted = float(value)
    if not math.isfinite(converted):
        raise AlgorithmValidationError(
            ErrorCode.INVALID_NUMERIC_VALUE,
            f"{name} must be finite",
            field=name,
        )
    return converted


def meters_to_millimeters(distance_m: Real) -> float:
    """Convert the public SI distance exactly once at the control boundary."""

    distance = _finite(distance_m, name="distance_m")
    if distance <= 0.0:
        raise AlgorithmValidationError(
            ErrorCode.INVALID_DISTANCE,
            "distance_m must be positive",
            field="distance_m",
        )
    return distance * 1000.0


distance_m_to_mm = meters_to_millimeters
convert_distance_m_to_mm = meters_to_millimeters


def validate_jog_limits(
    *,
    distance_mm: Real,
    previous_cumulative_mm: Real,
    limits: JogLimits,
) -> float:
    """Return the new cumulative magnitude or raise a structured limit error."""

    distance = _finite(distance_mm, name="distance_mm")
    previous = _finite(previous_cumulative_mm, name="previous_cumulative_mm")
    if distance <= 0.0:
        raise AlgorithmValidationError(
            ErrorCode.INVALID_DISTANCE,
            "distance_mm must be positive",
            field="distance_mm",
        )
    if previous < 0.0:
        raise AlgorithmValidationError(
            ErrorCode.INVALID_DISTANCE,
            "previous_cumulative_mm must be non-negative",
            field="previous_cumulative_mm",
        )
    if not isinstance(limits, JogLimits):
        raise AlgorithmValidationError(
            ErrorCode.CALIBRATION_VALUE_MISSING,
            "approved JogLimits are required",
            field="limits",
        )
    single_limit = _finite(limits.single_jog_mm, name="limits.single_jog_mm")
    cumulative_limit = _finite(
        limits.cumulative_jog_mm, name="limits.cumulative_jog_mm"
    )
    if single_limit <= 0.0 or cumulative_limit < single_limit:
        raise AlgorithmValidationError(
            ErrorCode.CALIBRATION_VALUE_MISSING,
            "jog limits must be positive and cumulative must cover one jog",
            field="limits",
            category=ErrorCategory.LIMIT,
        )
    if distance > single_limit:
        raise AlgorithmValidationError(
            ErrorCode.DISTANCE_LIMIT_EXCEEDED,
            "requested jog exceeds the approved single-command limit",
            field="distance_m",
            category=ErrorCategory.LIMIT,
            context={
                "distance_mm": distance,
                "single_jog_mm": single_limit,
            },
        )
    cumulative = previous + distance
    if cumulative > cumulative_limit:
        raise AlgorithmValidationError(
            ErrorCode.CUMULATIVE_DISTANCE_LIMIT_EXCEEDED,
            "requested jog exceeds the approved cumulative limit",
            field="distance_m",
            category=ErrorCategory.LIMIT,
            context={
                "previous_cumulative_mm": previous,
                "distance_mm": distance,
                "cumulative_jog_mm": cumulative_limit,
            },
        )
    return cumulative


def build_force_jog(
    *,
    mapping: SideMapping,
    limits: JogLimits,
    distance_m: Real,
    previous_cumulative_mm: Real = 0.0,
) -> ForceJogPlan:
    """Build one adapter-ready relative jog from approved explicit settings."""

    if not isinstance(mapping, SideMapping):
        raise AlgorithmValidationError(
            ErrorCode.SIDE_MAPPING_MISSING,
            "an approved SideMapping is required",
            field="mapping",
        )
    if mapping.side not in (TargetSide.LEFT, TargetSide.RIGHT):
        raise AlgorithmValidationError(
            ErrorCode.SIDE_MAPPING_MISSING,
            "force jog requires a LEFT or RIGHT mapping",
            field="mapping.side",
        )
    if (
        mapping.jog_sign not in (-1, 1)
        or not mapping.jog_axis
        or not mapping.jog_frame
    ):
        raise AlgorithmValidationError(
            ErrorCode.CALIBRATION_VALUE_MISSING,
            "jog axis, sign, and frame must be approved",
            field="mapping.jog",
        )
    requested = _finite(distance_m, name="distance_m")
    distance_mm = meters_to_millimeters(requested)
    previous = _finite(previous_cumulative_mm, name="previous_cumulative_mm")
    cumulative = validate_jog_limits(
        distance_mm=distance_mm,
        previous_cumulative_mm=previous,
        limits=limits,
    )
    return ForceJogPlan(
        side=mapping.side,
        arm_id=mapping.arm_id,
        role_instance_id=mapping.role_instance_id,
        sensor_id=mapping.sensor_id,
        axis=mapping.jog_axis,
        sign=mapping.jog_sign,
        frame=mapping.jog_frame,
        requested_distance_m=requested,
        distance_mm=distance_mm,
        signed_distance_mm=float(mapping.jog_sign) * distance_mm,
        previous_cumulative_mm=previous,
        cumulative_distance_mm=cumulative,
    )


def plan_force_jog(
    profile: ExecutionProfile,
    target_side: TargetSide | int,
    distance_m: Real,
    *,
    previous_cumulative_mm: Real = 0.0,
) -> ForceJogPlan:
    """Resolve a side through an approved profile, then build a bounded jog."""

    if not isinstance(profile, ExecutionProfile):
        require_ready = getattr(profile, "require_motion_ready", None)
        if callable(require_ready):
            require_ready()
        raise AlgorithmValidationError(
            ErrorCode.PROFILE_NOT_APPROVED,
            "force jog requires a checksum-validated ExecutionProfile",
            field="profile",
        )
    profile.require_motion_ready()
    mapping = profile.resolve_side(target_side)
    return build_force_jog(
        mapping=mapping,
        limits=profile.limits,
        distance_m=distance_m,
        previous_cumulative_mm=previous_cumulative_mm,
    )


plan_adjustment = plan_force_jog


__all__ = [
    "ForceJogPlan",
    "build_force_jog",
    "convert_distance_m_to_mm",
    "distance_m_to_mm",
    "meters_to_millimeters",
    "plan_adjustment",
    "plan_force_jog",
    "validate_jog_limits",
]
