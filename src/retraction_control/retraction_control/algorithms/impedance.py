"""Calibration-driven scalar impedance calculations.

All gains and safety clamps are required arguments.  The module deliberately
contains no robot-specific stiffness, damping, force target, or distance
default.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from numbers import Real

from ..command_models import AlgorithmValidationError, ErrorCode


@dataclass(frozen=True, slots=True)
class ImpedanceCorrection:
    target_force_n: float
    measured_force_n: float
    force_error_n: float
    unconstrained_offset_mm: float
    offset_mm: float
    saturated: bool


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


def compute_impedance_correction(
    *,
    target_force_n: Real,
    measured_force_n: Real,
    stiffness_n_per_mm: Real,
    max_abs_offset_mm: Real,
    damping_n_s_per_mm: Real,
    measured_velocity_mm_s: Real,
) -> ImpedanceCorrection:
    """Compute and clamp a one-axis spring-damper displacement.

    The positive direction is already encoded by the caller's approved force
    axis/sign mapping.  This function therefore performs no axis inference.
    """

    target = _finite(target_force_n, name="target_force_n")
    measured = _finite(measured_force_n, name="measured_force_n")
    stiffness = _finite(stiffness_n_per_mm, name="stiffness_n_per_mm")
    maximum = _finite(max_abs_offset_mm, name="max_abs_offset_mm")
    damping = _finite(damping_n_s_per_mm, name="damping_n_s_per_mm")
    velocity = _finite(measured_velocity_mm_s, name="measured_velocity_mm_s")
    if stiffness <= 0.0:
        raise AlgorithmValidationError(
            ErrorCode.INVALID_NUMERIC_VALUE,
            "stiffness_n_per_mm must be positive",
            field="stiffness_n_per_mm",
        )
    if maximum <= 0.0:
        raise AlgorithmValidationError(
            ErrorCode.INVALID_NUMERIC_VALUE,
            "max_abs_offset_mm must be positive",
            field="max_abs_offset_mm",
        )
    if damping < 0.0:
        raise AlgorithmValidationError(
            ErrorCode.INVALID_NUMERIC_VALUE,
            "damping_n_s_per_mm must be non-negative",
            field="damping_n_s_per_mm",
        )

    force_error = target - measured
    unconstrained = (force_error - damping * velocity) / stiffness
    constrained = max(-maximum, min(maximum, unconstrained))
    return ImpedanceCorrection(
        target_force_n=target,
        measured_force_n=measured,
        force_error_n=force_error,
        unconstrained_offset_mm=unconstrained,
        offset_mm=constrained,
        saturated=constrained != unconstrained,
    )


def force_within_tolerance(
    *,
    target_force_n: Real,
    measured_force_n: Real,
    tolerance_n: Real,
) -> bool:
    """Report observation only; it does not imply pose or motion completion."""

    target = _finite(target_force_n, name="target_force_n")
    measured = _finite(measured_force_n, name="measured_force_n")
    tolerance = _finite(tolerance_n, name="tolerance_n")
    if tolerance < 0.0:
        raise AlgorithmValidationError(
            ErrorCode.INVALID_NUMERIC_VALUE,
            "tolerance_n must be non-negative",
            field="tolerance_n",
        )
    return abs(target - measured) <= tolerance


compute_impedance_offset = compute_impedance_correction


__all__ = [
    "ImpedanceCorrection",
    "compute_impedance_correction",
    "compute_impedance_offset",
    "force_within_tolerance",
]
