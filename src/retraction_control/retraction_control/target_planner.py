"""Versioned, checksum-bound teaching target planner contracts.

The built-in planner intentionally mirrors the deterministic fake baseline: it
selects the last sample observed for every arm and force sensor.  Its identity
is permanently marked synthetic so it can never silently become a production
calibration algorithm.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re
from types import MappingProxyType
from typing import Callable, Mapping, Protocol, Sequence, runtime_checkable

from .adapters import ForceTorqueSample, JointStateSample


_CHECKSUM_RE = re.compile(r"^(?:sha256:)?([0-9a-fA-F]{64})$")


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value)).hexdigest()


def _required_text(value: object, field_name: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


def _normalize_checksum(value: object) -> str:
    match = _CHECKSUM_RE.fullmatch(str(value).strip())
    if match is None:
        raise ValueError("planner checksum must be a SHA-256 digest")
    return "sha256:" + match.group(1).lower()


def _finite_mapping(
    values: Mapping[str, Sequence[float]], *, field_name: str, width: int | None = None
) -> Mapping[str, tuple[float, ...]]:
    normalized: dict[str, tuple[float, ...]] = {}
    for raw_key, raw_values in values.items():
        key = _required_text(raw_key, f"{field_name} key")
        if key in normalized:
            raise ValueError(f"duplicate {field_name} key: {key}")
        items = tuple(float(item) for item in raw_values)
        if not items or not all(math.isfinite(item) for item in items):
            raise ValueError(f"{field_name}.{key} must contain finite values")
        if width is not None and len(items) != width:
            raise ValueError(f"{field_name}.{key} must contain {width} values")
        normalized[key] = items
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return MappingProxyType(normalized)


@dataclass(frozen=True, slots=True)
class TargetPlannerIdentity:
    name: str
    version: str
    checksum: str
    synthetic: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _required_text(self.name, "planner name"))
        object.__setattr__(
            self, "version", _required_text(self.version, "planner version")
        )
        object.__setattr__(self, "checksum", _normalize_checksum(self.checksum))
        if type(self.synthetic) is not bool:
            raise ValueError("planner synthetic flag must be a boolean")

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "version": self.version,
            "checksum": self.checksum,
            "synthetic": self.synthetic,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "TargetPlannerIdentity":
        if set(value) != {"name", "version", "checksum", "synthetic"}:
            raise ValueError("target planner identity fields are invalid")
        return cls(
            name=value["name"],
            version=value["version"],
            checksum=value["checksum"],
            synthetic=value["synthetic"],
        )


@dataclass(frozen=True, slots=True)
class TargetPlan:
    identity: TargetPlannerIdentity
    input_checksum: str
    joint_positions: Mapping[str, tuple[float, ...]]
    force_targets_n: Mapping[str, tuple[float, ...]]

    def __post_init__(self) -> None:
        if not isinstance(self.identity, TargetPlannerIdentity):
            raise ValueError("target plan requires a validated planner identity")
        object.__setattr__(
            self, "input_checksum", _normalize_checksum(self.input_checksum)
        )
        object.__setattr__(
            self,
            "joint_positions",
            _finite_mapping(self.joint_positions, field_name="joint_positions"),
        )
        object.__setattr__(
            self,
            "force_targets_n",
            _finite_mapping(
                self.force_targets_n, field_name="force_targets_n", width=3
            ),
        )


def target_input_checksum(
    joint_samples: Sequence[JointStateSample],
    force_samples: Sequence[ForceTorqueSample],
) -> str:
    payload = {
        "joint_samples": [
            {
                "timestamp_ns": int(sample.timestamp_ns),
                "arm_id": sample.arm_id,
                "positions": list(sample.positions),
            }
            for sample in joint_samples
        ],
        "force_samples": [
            {
                "timestamp_ns": int(sample.timestamp_ns),
                "sensor_id": sample.sensor_id,
                "force_n": list(sample.force_n),
                "torque_nm": list(sample.torque_nm),
                "calibration_id": sample.calibration_id,
                "valid": bool(sample.valid),
            }
            for sample in force_samples
        ],
    }
    return _sha256(payload)


@runtime_checkable
class TargetPlanner(Protocol):
    @property
    def identity(self) -> TargetPlannerIdentity: ...

    def plan(
        self,
        joint_samples: tuple[JointStateSample, ...],
        force_samples: tuple[ForceTorqueSample, ...],
    ) -> TargetPlan: ...


_LAST_SAMPLE_DESCRIPTOR = {
    "name": "synthetic_last_sample",
    "version": "1.0.0",
    "algorithm": "last sample per arm and sensor",
    "physical_authority": False,
}


class LastSampleTargetPlanner:
    """Deterministic fake baseline, never an approved physical algorithm."""

    identity = TargetPlannerIdentity(
        name=str(_LAST_SAMPLE_DESCRIPTOR["name"]),
        version=str(_LAST_SAMPLE_DESCRIPTOR["version"]),
        checksum=_sha256(_LAST_SAMPLE_DESCRIPTOR),
        synthetic=True,
    )

    def plan(
        self,
        joint_samples: tuple[JointStateSample, ...],
        force_samples: tuple[ForceTorqueSample, ...],
    ) -> TargetPlan:
        joints: dict[str, tuple[float, ...]] = {}
        forces: dict[str, tuple[float, ...]] = {}
        for sample in joint_samples:
            joints[sample.arm_id] = sample.positions
        for sample in force_samples:
            if not sample.valid:
                raise ValueError("target planner received an invalid force sample")
            forces[sample.sensor_id] = sample.force_n
        return TargetPlan(
            identity=self.identity,
            input_checksum=target_input_checksum(joint_samples, force_samples),
            joint_positions=joints,
            force_targets_n=forces,
        )


class CallableTargetPlanner:
    """Explicitly identified adapter for an injected, reviewed planner callable."""

    def __init__(
        self,
        identity: TargetPlannerIdentity,
        calculator: Callable[
            [tuple[JointStateSample, ...], tuple[ForceTorqueSample, ...]],
            tuple[Mapping[str, Sequence[float]], Mapping[str, Sequence[float]]],
        ],
    ) -> None:
        if not callable(calculator):
            raise TypeError("target calculator must be callable")
        self._identity = identity
        self._calculator = calculator

    @property
    def identity(self) -> TargetPlannerIdentity:
        return self._identity

    def plan(
        self,
        joint_samples: tuple[JointStateSample, ...],
        force_samples: tuple[ForceTorqueSample, ...],
    ) -> TargetPlan:
        joint_positions, force_targets = self._calculator(
            joint_samples, force_samples
        )
        return TargetPlan(
            identity=self.identity,
            input_checksum=target_input_checksum(joint_samples, force_samples),
            joint_positions=joint_positions,
            force_targets_n=force_targets,
        )


__all__ = [
    "CallableTargetPlanner",
    "LastSampleTargetPlanner",
    "TargetPlan",
    "TargetPlanner",
    "TargetPlannerIdentity",
    "target_input_checksum",
]
