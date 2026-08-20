"""Pure helpers for selecting and composing joint targets.

The functions operate only on explicitly supplied vectors and half-open joint
ranges.  They contain no robot-name, arm-count, or calibration defaults.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from numbers import Real
from typing import Iterable

from ..command_models import AlgorithmValidationError, ErrorCode
from ..profile_loader import JointSlice


@dataclass(frozen=True, slots=True)
class JointTarget:
    positions: tuple[float, ...]
    replaced_slice: JointSlice


def _numeric_vector(values: Iterable[Real], *, name: str) -> tuple[float, ...]:
    if isinstance(values, (str, bytes)):
        raise AlgorithmValidationError(
            ErrorCode.INVALID_VECTOR_LENGTH,
            f"{name} must be a numeric sequence",
            field=name,
        )
    try:
        raw = tuple(values)
    except TypeError as exc:
        raise AlgorithmValidationError(
            ErrorCode.INVALID_VECTOR_LENGTH,
            f"{name} must be an iterable numeric vector",
            field=name,
        ) from exc
    converted: list[float] = []
    for index, value in enumerate(raw):
        if isinstance(value, bool) or not isinstance(value, Real):
            raise AlgorithmValidationError(
                ErrorCode.INVALID_NUMERIC_VALUE,
                f"{name}[{index}] must be a real number",
                field=f"{name}[{index}]",
            )
        number = float(value)
        if not math.isfinite(number):
            raise AlgorithmValidationError(
                ErrorCode.INVALID_NUMERIC_VALUE,
                f"{name}[{index}] must be finite",
                field=f"{name}[{index}]",
            )
        converted.append(number)
    if not converted:
        raise AlgorithmValidationError(
            ErrorCode.INVALID_VECTOR_LENGTH,
            f"{name} must not be empty",
            field=name,
        )
    return tuple(converted)


def normalize_joint_slice(
    joint_slice: JointSlice | slice | tuple[int, int],
    *,
    vector_length: int,
) -> JointSlice:
    """Validate a configured half-open joint range against a vector length."""

    if isinstance(joint_slice, JointSlice):
        normalized = joint_slice
    elif isinstance(joint_slice, slice):
        if (
            joint_slice.step not in (None, 1)
            or joint_slice.start is None
            or joint_slice.stop is None
        ):
            raise AlgorithmValidationError(
                ErrorCode.INVALID_JOINT_SLICE,
                "joint slice must have explicit start/stop and unit step",
                field="joint_slice",
            )
        normalized = JointSlice(joint_slice.start, joint_slice.stop)
    else:
        try:
            start, end = joint_slice
        except (TypeError, ValueError) as exc:
            raise AlgorithmValidationError(
                ErrorCode.INVALID_JOINT_SLICE,
                "joint_slice must contain exactly (start, end)",
                field="joint_slice",
            ) from exc
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, int)
            or not isinstance(end, int)
        ):
            raise AlgorithmValidationError(
                ErrorCode.INVALID_JOINT_SLICE,
                "joint slice indexes must be integers",
                field="joint_slice",
            )
        normalized = JointSlice(start, end)

    if (
        normalized.start < 0
        or normalized.end <= normalized.start
        or normalized.end > vector_length
    ):
        raise AlgorithmValidationError(
            ErrorCode.INVALID_JOINT_SLICE,
            "joint slice is outside the target vector",
            field="joint_slice",
            context={
                "start": normalized.start,
                "end": normalized.end,
                "vector_length": vector_length,
            },
        )
    return normalized


def select_joint_slice(
    joints: Iterable[Real],
    joint_slice: JointSlice | slice | tuple[int, int],
) -> tuple[float, ...]:
    vector = _numeric_vector(joints, name="joints")
    selected = normalize_joint_slice(joint_slice, vector_length=len(vector))
    return vector[selected.start : selected.end]


def replace_joint_slice(
    base_joints: Iterable[Real],
    replacement_joints: Iterable[Real],
    joint_slice: JointSlice | slice | tuple[int, int],
) -> JointTarget:
    """Replace exactly one configured arm slice in a full joint vector."""

    base = _numeric_vector(base_joints, name="base_joints")
    replacement = _numeric_vector(replacement_joints, name="replacement_joints")
    selected = normalize_joint_slice(joint_slice, vector_length=len(base))
    if len(replacement) != selected.size:
        raise AlgorithmValidationError(
            ErrorCode.INVALID_VECTOR_LENGTH,
            "replacement vector length must equal the configured joint slice size",
            field="replacement_joints",
            context={"expected": selected.size, "received": len(replacement)},
        )
    positions = base[: selected.start] + replacement + base[selected.end :]
    return JointTarget(positions=positions, replaced_slice=selected)


def mean_joint_positions(samples: Iterable[Iterable[Real]]) -> tuple[float, ...]:
    """Compute a deterministic component-wise mean of teaching samples."""

    rows = tuple(
        _numeric_vector(sample, name=f"samples[{index}]")
        for index, sample in enumerate(samples)
    )
    if not rows:
        raise AlgorithmValidationError(
            ErrorCode.INVALID_VECTOR_LENGTH,
            "at least one joint sample is required",
            field="samples",
        )
    width = len(rows[0])
    if any(len(row) != width for row in rows[1:]):
        raise AlgorithmValidationError(
            ErrorCode.INVALID_VECTOR_LENGTH,
            "all joint samples must have the same width",
            field="samples",
        )
    # math.fsum makes the result deterministic and less sensitive to input
    # magnitude than repeated binary addition.
    return tuple(
        math.fsum(row[index] for row in rows) / len(rows)
        for index in range(width)
    )


def compose_joint_target(
    current_joints: Iterable[Real],
    taught_joints: Iterable[Real],
    joint_slice: JointSlice | slice | tuple[int, int],
) -> JointTarget:
    """Compose a full target from either full-width or arm-local taught data."""

    current = _numeric_vector(current_joints, name="current_joints")
    taught = _numeric_vector(taught_joints, name="taught_joints")
    selected = normalize_joint_slice(joint_slice, vector_length=len(current))
    if len(taught) == len(current):
        replacement = taught[selected.start : selected.end]
    elif len(taught) == selected.size:
        replacement = taught
    else:
        raise AlgorithmValidationError(
            ErrorCode.INVALID_VECTOR_LENGTH,
            "taught_joints must be a full vector or exactly the configured arm slice",
            field="taught_joints",
            context={
                "full_length": len(current),
                "slice_length": selected.size,
                "received": len(taught),
            },
        )
    return replace_joint_slice(current, replacement, selected)


synthesize_joint_target = compose_joint_target


__all__ = [
    "JointTarget",
    "compose_joint_target",
    "mean_joint_positions",
    "normalize_joint_slice",
    "replace_joint_slice",
    "select_joint_slice",
    "synthesize_joint_target",
]
