"""Deterministic validation and analysis of timestamped force samples."""

from __future__ import annotations

from dataclasses import dataclass
import math
from numbers import Real
from typing import Iterable, Mapping, Sequence

from ..command_models import AlgorithmValidationError, ErrorCategory, ErrorCode


_FORCE_AXIS_INDEX = {
    "x": 0,
    "fx": 0,
    "force_x": 0,
    "y": 1,
    "fy": 1,
    "force_y": 1,
    "z": 2,
    "fz": 2,
    "force_z": 2,
}


def _finite(value: Real, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise AlgorithmValidationError(
            ErrorCode.INVALID_NUMERIC_VALUE,
            f"{name} must be a real number",
            field=name,
            category=ErrorCategory.SENSOR,
        )
    converted = float(value)
    if not math.isfinite(converted):
        raise AlgorithmValidationError(
            ErrorCode.INVALID_NUMERIC_VALUE,
            f"{name} must be finite",
            field=name,
            category=ErrorCategory.SENSOR,
        )
    return converted


def _triplet(values: Sequence[Real], *, name: str) -> tuple[float, float, float]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise AlgorithmValidationError(
            ErrorCode.INVALID_SAMPLE,
            f"{name} must be a three-value numeric sequence",
            field=name,
            category=ErrorCategory.SENSOR,
        )
    if len(values) != 3:
        raise AlgorithmValidationError(
            ErrorCode.INVALID_VECTOR_LENGTH,
            f"{name} must contain exactly three values",
            field=name,
            category=ErrorCategory.SENSOR,
        )
    return (
        _finite(values[0], name=f"{name}[0]"),
        _finite(values[1], name=f"{name}[1]"),
        _finite(values[2], name=f"{name}[2]"),
    )


@dataclass(frozen=True, slots=True)
class ForceTorqueSample:
    timestamp_s: float
    force_n: tuple[float, float, float]
    torque_nm: tuple[float, float, float]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "timestamp_s", _finite(self.timestamp_s, name="timestamp_s")
        )
        object.__setattr__(self, "force_n", _triplet(self.force_n, name="force_n"))
        object.__setattr__(
            self, "torque_nm", _triplet(self.torque_nm, name="torque_nm")
        )

    @classmethod
    def from_values(
        cls,
        *,
        timestamp_s: Real,
        force_n: Sequence[Real],
        torque_nm: Sequence[Real],
    ) -> ForceTorqueSample:
        return cls(
            timestamp_s=timestamp_s,  # type: ignore[arg-type]
            force_n=tuple(force_n),  # type: ignore[arg-type]
            torque_nm=tuple(torque_nm),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class ForceAnalysis:
    axis: str
    sign: int
    sample_count: int
    first_timestamp_s: float
    latest_timestamp_s: float
    age_s: float
    projected_values_n: tuple[float, ...]
    mean_force_n: float
    latest_force_n: float
    minimum_force_n: float
    maximum_force_n: float


def normalize_force_axis(axis: object) -> tuple[str, int]:
    if not isinstance(axis, str):
        raise AlgorithmValidationError(
            ErrorCode.INVALID_FORCE_AXIS,
            "force axis must be a configured string",
            field="axis",
            category=ErrorCategory.SENSOR,
        )
    normalized = axis.strip().lower()
    if normalized not in _FORCE_AXIS_INDEX:
        raise AlgorithmValidationError(
            ErrorCode.INVALID_FORCE_AXIS,
            f"unsupported translational force axis: {axis!r}",
            field="axis",
            category=ErrorCategory.SENSOR,
        )
    return normalized, _FORCE_AXIS_INDEX[normalized]


def project_force(
    sample: ForceTorqueSample,
    *,
    axis: str,
    sign: int,
) -> float:
    normalized_axis, index = normalize_force_axis(axis)
    del normalized_axis
    if isinstance(sign, bool) or sign not in (-1, 1):
        raise AlgorithmValidationError(
            ErrorCode.INVALID_FORCE_AXIS,
            "force sign must be exactly -1 or 1",
            field="sign",
            category=ErrorCategory.SENSOR,
        )
    return float(sign) * sample.force_n[index]


def analyze_force_samples(
    samples: Iterable[ForceTorqueSample],
    *,
    axis: str,
    sign: int,
    now_s: Real,
    freshness_timeout_s: Real,
    min_samples: int,
) -> ForceAnalysis:
    """Validate ordered/fresh data and summarize an approved force axis."""

    values = tuple(samples)
    if (
        isinstance(min_samples, bool)
        or not isinstance(min_samples, int)
        or min_samples <= 0
    ):
        raise AlgorithmValidationError(
            ErrorCode.INVALID_NUMERIC_VALUE,
            "min_samples must be a positive integer",
            field="min_samples",
            category=ErrorCategory.SENSOR,
        )
    if len(values) < min_samples:
        raise AlgorithmValidationError(
            ErrorCode.INSUFFICIENT_FORCE_SAMPLES,
            "not enough force samples for analysis",
            field="samples",
            category=ErrorCategory.SENSOR,
            context={"required": min_samples, "received": len(values)},
        )
    if any(not isinstance(sample, ForceTorqueSample) for sample in values):
        raise AlgorithmValidationError(
            ErrorCode.INVALID_SAMPLE,
            "all samples must be validated ForceTorqueSample instances",
            field="samples",
            category=ErrorCategory.SENSOR,
        )
    timestamps = tuple(sample.timestamp_s for sample in values)
    if any(later <= earlier for earlier, later in zip(timestamps, timestamps[1:])):
        raise AlgorithmValidationError(
            ErrorCode.NON_MONOTONIC_SAMPLES,
            "force sample timestamps must be strictly increasing",
            field="samples",
            category=ErrorCategory.SENSOR,
        )
    now = _finite(now_s, name="now_s")
    freshness = _finite(freshness_timeout_s, name="freshness_timeout_s")
    if freshness <= 0.0:
        raise AlgorithmValidationError(
            ErrorCode.INVALID_NUMERIC_VALUE,
            "freshness_timeout_s must be positive",
            field="freshness_timeout_s",
            category=ErrorCategory.SENSOR,
        )
    age = now - timestamps[-1]
    if age < 0.0:
        raise AlgorithmValidationError(
            ErrorCode.INVALID_SAMPLE,
            "latest force sample timestamp is in the future",
            field="samples",
            category=ErrorCategory.SENSOR,
            context={"age_s": age},
        )
    if age > freshness:
        raise AlgorithmValidationError(
            ErrorCode.STALE_FORCE_SAMPLE,
            "latest force sample exceeds the configured freshness timeout",
            field="samples",
            category=ErrorCategory.SENSOR,
            context={"age_s": age, "freshness_timeout_s": freshness},
        )
    normalized_axis, _ = normalize_force_axis(axis)
    projected = tuple(
        project_force(sample, axis=normalized_axis, sign=sign)
        for sample in values
    )
    return ForceAnalysis(
        axis=normalized_axis,
        sign=sign,
        sample_count=len(values),
        first_timestamp_s=timestamps[0],
        latest_timestamp_s=timestamps[-1],
        age_s=age,
        projected_values_n=projected,
        mean_force_n=math.fsum(projected) / len(projected),
        latest_force_n=projected[-1],
        minimum_force_n=min(projected),
        maximum_force_n=max(projected),
    )


def analyze_force_records(
    records: Iterable[Mapping[str, object]],
    **kwargs: object,
) -> ForceAnalysis:
    """Convenience adapter for persisted records with explicit field names."""

    samples: list[ForceTorqueSample] = []
    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            raise AlgorithmValidationError(
                ErrorCode.INVALID_SAMPLE,
                "force record must be a mapping",
                field=f"records[{index}]",
                category=ErrorCategory.SENSOR,
            )
        missing = {"timestamp_s", "force_n", "torque_nm"} - set(record)
        if missing:
            raise AlgorithmValidationError(
                ErrorCode.INVALID_SAMPLE,
                f"force record is missing fields: {', '.join(sorted(missing))}",
                field=f"records[{index}]",
                category=ErrorCategory.SENSOR,
            )
        samples.append(
            ForceTorqueSample.from_values(
                timestamp_s=record["timestamp_s"],  # type: ignore[arg-type]
                force_n=record["force_n"],  # type: ignore[arg-type]
                torque_nm=record["torque_nm"],  # type: ignore[arg-type]
            )
        )
    return analyze_force_samples(tuple(samples), **kwargs)  # type: ignore[arg-type]


__all__ = [
    "ForceAnalysis",
    "ForceTorqueSample",
    "analyze_force_records",
    "analyze_force_samples",
    "normalize_force_axis",
    "project_force",
]
