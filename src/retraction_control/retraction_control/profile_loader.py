"""Strict loader for versioned retraction procedure profiles.

Two profile states are intentional:

* :class:`DraftProfile` preserves an unapproved, intentionally incomplete
  partner-calibration document but cannot resolve motion parameters.
* :class:`ExecutionProfile` is returned only after every motion-critical field
  and the canonical SHA-256 checksum have been validated.

This distinction lets the repository carry honest ``null`` placeholders while
ensuring that unknown physical calibration never becomes executable through a
fallback or invented default.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import hmac
import ipaddress
import json
import math
from numbers import Real
import os
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Mapping, Sequence, TypeAlias

import yaml

from .command_models import (
    ErrorCode,
    ProfileValidationError,
    TargetSide,
)


PROFILE_SCHEMA_VERSION = 2
_CHECKSUM_RE = re.compile(r"^(?:sha256:)?([0-9a-fA-F]{64})$")

_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "profile",
        "calibration",
        "robot",
        "sensor",
        "data",
        "control",
        "side_mapping",
        "tool_change",
        "limits",
    }
)


@dataclass(frozen=True, slots=True)
class JointSlice:
    """Half-open joint index range ``[start, end)``."""

    start: int
    end: int

    @property
    def size(self) -> int:
        return self.end - self.start


@dataclass(frozen=True, slots=True)
class SideMapping:
    side: TargetSide
    arm_id: str
    role_instance_id: str
    sensor_id: str | int
    joint_slice: JointSlice
    jog_axis: str
    jog_sign: int
    jog_frame: str
    force_axis: str | None = None
    force_sign: int | None = None

    @property
    def role(self) -> str:
        """Short compatibility alias used by the execution layer."""

        return self.role_instance_id


@dataclass(frozen=True, slots=True)
class ToolChangeWaypoint:
    name: str
    arm_id: str
    joint_positions: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class RobotSettings:
    controller_ip: str
    sdk_version: str
    timeout_sec: float


@dataclass(frozen=True, slots=True)
class SensorSettings:
    transport: str
    channel: str
    bitrate: int
    sample_rate_hz: float
    zeroing_policy: str


@dataclass(frozen=True, slots=True)
class ControlSettings:
    teach_friction: float
    normal_friction: float
    custom_gain: object
    force_threshold: float
    force_freshness_timeout_sec: float
    impedance_target_force_n: float
    impedance_tolerance_n: float
    stop_policy: str


@dataclass(frozen=True, slots=True)
class JogLimits:
    single_jog_mm: float
    cumulative_jog_mm: float


@dataclass(frozen=True, slots=True)
class DraftProfile:
    """Structurally valid metadata whose calibration is not executable."""

    schema_version: int
    name: str
    version: str
    procedure_type: str
    public_procedure_type: str | None
    calibration_revision: str | None
    expected_checksum: str | None
    computed_checksum: str
    data_directory: Path | None
    readiness_issues: tuple[str, ...]
    raw: Mapping[str, Any]

    @property
    def calibration_approved(self) -> bool:
        return False

    @property
    def checksum(self) -> str:
        return self.computed_checksum

    @property
    def side_mappings(self) -> Mapping[TargetSide, SideMapping]:
        return MappingProxyType({})

    @property
    def tool_change_waypoints(self) -> tuple[ToolChangeWaypoint, ...]:
        return ()

    def require_motion_ready(self) -> None:
        raise ProfileValidationError(
            ErrorCode.PROFILE_NOT_APPROVED,
            f"profile {self.name!r} is a non-executable calibration draft",
            field="calibration.approved",
            context={"readiness_issues": self.readiness_issues},
        )

    def resolve_side(self, side: TargetSide | int) -> SideMapping:
        del side
        self.require_motion_ready()
        raise AssertionError("unreachable")


@dataclass(frozen=True, slots=True)
class ExecutionProfile:
    """Fully validated and checksum-bound physical execution parameters."""

    schema_version: int
    name: str
    version: str
    procedure_type: str
    public_procedure_type: str
    calibration_revision: str
    expected_checksum: str
    computed_checksum: str
    robot: RobotSettings
    sensor: SensorSettings
    data_directory: Path
    control: ControlSettings
    side_mappings: Mapping[TargetSide, SideMapping]
    tool_change_waypoints: tuple[ToolChangeWaypoint, ...]
    limits: JogLimits
    raw: Mapping[str, Any]

    @property
    def calibration_approved(self) -> bool:
        return True

    @property
    def checksum(self) -> str:
        return self.computed_checksum

    @property
    def teach_friction(self) -> float:
        return self.control.teach_friction

    @property
    def normal_friction(self) -> float:
        return self.control.normal_friction

    @property
    def custom_gain(self) -> object:
        return self.control.custom_gain

    @property
    def force_threshold(self) -> float:
        return self.control.force_threshold

    @property
    def force_freshness_timeout_sec(self) -> float:
        return self.control.force_freshness_timeout_sec

    @property
    def force_freshness_timeout_ns(self) -> int:
        return int(self.control.force_freshness_timeout_sec * 1_000_000_000)

    @property
    def stop_policy(self) -> str:
        return self.control.stop_policy

    @property
    def jog_single_max_mm(self) -> float:
        return self.limits.single_jog_mm

    @property
    def jog_cumulative_max_mm(self) -> float:
        return self.limits.cumulative_jog_mm

    def require_motion_ready(self) -> None:
        """Symmetric API with :class:`DraftProfile`; success is silent."""

    def resolve_side(self, side: TargetSide | int) -> SideMapping:
        try:
            normalized = TargetSide(side)
        except (TypeError, ValueError) as exc:
            raise ProfileValidationError(
                ErrorCode.SIDE_MAPPING_MISSING,
                f"invalid target side: {side!r}",
                field="target_side",
            ) from exc
        if normalized is TargetSide.NONE or normalized not in self.side_mappings:
            raise ProfileValidationError(
                ErrorCode.SIDE_MAPPING_MISSING,
                f"no approved mapping exists for target side {normalized.name}",
                field=f"side_mapping.{normalized.name}",
            )
        return self.side_mappings[normalized]


LoadedProfile: TypeAlias = DraftProfile | ExecutionProfile


def canonical_profile_payload(payload: Mapping[str, Any]) -> bytes:
    """Return canonical JSON bytes, excluding the self-referential checksum."""

    material = _plain_copy(payload)
    calibration = material.get("calibration")
    if isinstance(calibration, Mapping):
        calibration_copy = dict(calibration)
        calibration_copy.pop("expected_checksum", None)
        material["calibration"] = calibration_copy
    try:
        encoded = json.dumps(
            material,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ProfileValidationError(
            ErrorCode.PROFILE_SCHEMA_INVALID,
            "profile contains a value that cannot be represented canonically",
            context={"cause": str(exc)},
        ) from exc
    return encoded.encode("utf-8")


def compute_profile_checksum(payload: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_profile_payload(payload)).hexdigest()


def load_profile(
    source: str | os.PathLike[str] | Mapping[str, Any],
    *,
    require_approved: bool = False,
) -> LoadedProfile:
    """Load YAML or a mapping and return a draft or executable profile."""

    if isinstance(source, Mapping):
        payload = _plain_copy(source)
    else:
        path = Path(source)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ProfileValidationError(
                ErrorCode.PROFILE_SCHEMA_INVALID,
                f"unable to read profile: {path}",
                field="profile_path",
                context={"cause": str(exc)},
            ) from exc
        try:
            payload = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ProfileValidationError(
                ErrorCode.PROFILE_SCHEMA_INVALID,
                f"invalid YAML in profile: {path}",
                field="profile_path",
                context={"cause": str(exc)},
            ) from exc
    if not isinstance(payload, Mapping):
        raise _schema_error("profile root must be a mapping", "$")
    loaded = _load_mapping(dict(payload))
    if require_approved and isinstance(loaded, DraftProfile):
        loaded.require_motion_ready()
    return loaded


def load_profile_mapping(
    payload: Mapping[str, Any], *, require_approved: bool = False
) -> LoadedProfile:
    return load_profile(payload, require_approved=require_approved)


validate_profile = load_profile_mapping


def _load_mapping(payload: dict[str, Any]) -> LoadedProfile:
    _check_keys(
        payload,
        _TOP_LEVEL_KEYS,
        {"schema_version", "profile", "calibration"},
        "$",
    )
    schema_version = _strict_int(payload.get("schema_version"), "schema_version")
    if schema_version != PROFILE_SCHEMA_VERSION:
        raise _schema_error(
            f"schema_version must be {PROFILE_SCHEMA_VERSION}", "schema_version"
        )

    profile_node = _mapping(payload.get("profile"), "profile")
    _check_keys(
        profile_node,
        {"name", "version", "procedure_type", "public_procedure_type"},
        {"name", "version", "procedure_type", "public_procedure_type"},
        "profile",
    )
    name = _nonempty_string(profile_node.get("name"), "profile.name")
    version = _nonempty_string(profile_node.get("version"), "profile.version")
    procedure_type = _nonempty_string(
        profile_node.get("procedure_type"), "profile.procedure_type"
    )
    public_procedure_type = _optional_public_procedure_type(
        profile_node.get("public_procedure_type"),
        "profile.public_procedure_type",
    )

    calibration = _mapping(payload.get("calibration"), "calibration")
    _check_keys(
        calibration,
        {"approved", "revision", "expected_checksum"},
        {"approved"},
        "calibration",
    )
    approved = _strict_bool(calibration.get("approved"), "calibration.approved")
    revision = _optional_string(calibration.get("revision"), "calibration.revision")
    expected_checksum = _optional_checksum(
        calibration.get("expected_checksum"), "calibration.expected_checksum"
    )
    computed_checksum = compute_profile_checksum(payload)
    if expected_checksum is not None and not hmac.compare_digest(
        expected_checksum, computed_checksum
    ):
        raise ProfileValidationError(
            ErrorCode.PROFILE_CHECKSUM_MISMATCH,
            "profile content does not match calibration.expected_checksum",
            field="calibration.expected_checksum",
            context={
                "expected": expected_checksum,
                "computed": computed_checksum,
            },
        )

    _validate_optional_section_shapes(payload)
    _validate_present_draft_values(payload)
    raw = _freeze(payload)

    if not approved:
        data_directory = _draft_data_directory(payload.get("data"))
        return DraftProfile(
            schema_version=schema_version,
            name=name,
            version=version,
            procedure_type=procedure_type,
            public_procedure_type=public_procedure_type,
            calibration_revision=revision,
            expected_checksum=expected_checksum,
            computed_checksum=computed_checksum,
            data_directory=data_directory,
            readiness_issues=_draft_readiness_issues(
                payload, revision, expected_checksum
            ),
            raw=raw,
        )

    if revision is None:
        raise _missing_calibration("calibration.revision")
    if expected_checksum is None:
        raise ProfileValidationError(
            ErrorCode.PROFILE_CHECKSUM_INVALID,
            "approved profile requires calibration.expected_checksum",
            field="calibration.expected_checksum",
        )
    if public_procedure_type is None:
        raise _missing_calibration("profile.public_procedure_type")

    robot = _parse_robot(payload.get("robot"))
    sensor = _parse_sensor(payload.get("sensor"))
    data_directory = _parse_data(payload.get("data"))
    control = _parse_control(payload.get("control"))
    side_mappings = _parse_side_mappings(payload.get("side_mapping"))
    waypoints = _parse_tool_change(payload.get("tool_change"))
    limits = _parse_limits(payload.get("limits"))

    return ExecutionProfile(
        schema_version=schema_version,
        name=name,
        version=version,
        procedure_type=procedure_type,
        public_procedure_type=public_procedure_type,
        calibration_revision=revision,
        expected_checksum=expected_checksum,
        computed_checksum=computed_checksum,
        robot=robot,
        sensor=sensor,
        data_directory=data_directory,
        control=control,
        side_mappings=MappingProxyType(side_mappings),
        tool_change_waypoints=waypoints,
        limits=limits,
        raw=raw,
    )


def _validate_optional_section_shapes(payload: Mapping[str, Any]) -> None:
    """Reject unknown keys even for a null-filled draft profile."""

    specs: tuple[tuple[str, set[str]], ...] = (
        ("robot", {"controller_ip", "sdk_version", "timeout_sec"}),
        (
            "sensor",
            {"transport", "channel", "bitrate", "sample_rate_hz", "zeroing_policy"},
        ),
        ("data", {"directory"}),
        (
            "control",
            {
                "friction_compensation",
                "custom_gain",
                "force_threshold",
                "force_freshness_timeout_sec",
                "impedance",
                "stop_policy",
            },
        ),
        ("side_mapping", {"LEFT", "RIGHT"}),
        ("tool_change", {"waypoints"}),
        ("limits", {"single_jog_mm", "cumulative_jog_mm"}),
    )
    for section, allowed in specs:
        value = payload.get(section)
        if value is None:
            continue
        node = _mapping(value, section)
        _check_keys(node, allowed, set(), section)

    control = payload.get("control")
    if isinstance(control, Mapping):
        friction = control.get("friction_compensation")
        if friction is not None:
            node = _mapping(friction, "control.friction_compensation")
            _check_keys(
                node,
                {"direct_teach", "normal"},
                set(),
                "control.friction_compensation",
            )
        impedance = control.get("impedance")
        if impedance is not None:
            node = _mapping(impedance, "control.impedance")
            _check_keys(
                node,
                {"target_force_n", "tolerance_n"},
                set(),
                "control.impedance",
            )

    side_mapping = payload.get("side_mapping")
    if isinstance(side_mapping, Mapping):
        for side_name in ("LEFT", "RIGHT"):
            value = side_mapping.get(side_name)
            if value is None:
                continue
            path = f"side_mapping.{side_name}"
            node = _mapping(value, path)
            _check_keys(
                node,
                {
                    "arm_id",
                    "role_instance_id",
                    "sensor_id",
                    "joint_start",
                    "joint_end",
                    "jog",
                    "force",
                },
                set(),
                path,
            )
            for subsection in ("jog", "force"):
                subvalue = node.get(subsection)
                if subvalue is not None:
                    subpath = f"{path}.{subsection}"
                    subnode = _mapping(subvalue, subpath)
                    _check_keys(subnode, {"axis", "sign", "frame"}, set(), subpath)

    tool_change = payload.get("tool_change")
    if isinstance(tool_change, Mapping):
        waypoints = tool_change.get("waypoints")
        if waypoints is not None:
            if isinstance(waypoints, (str, bytes)) or not isinstance(
                waypoints, Sequence
            ):
                raise _schema_error(
                    "waypoints must be a sequence", "tool_change.waypoints"
                )
            for index, waypoint in enumerate(waypoints):
                path = f"tool_change.waypoints[{index}]"
                node = _mapping(waypoint, path)
                _check_keys(
                    node,
                    {"name", "arm_id", "joint_positions"},
                    set(),
                    path,
                )


def _validate_present_draft_values(payload: Mapping[str, Any]) -> None:
    """Validate every non-null draft value without requiring missing calibration."""

    robot = payload.get("robot")
    if isinstance(robot, Mapping):
        controller_ip = robot.get("controller_ip")
        if controller_ip is not None:
            address = _nonempty_string(controller_ip, "robot.controller_ip")
            try:
                ipaddress.ip_address(address)
            except ValueError as exc:
                raise _schema_error(
                    "controller_ip must be an IP address", "robot.controller_ip"
                ) from exc
        if robot.get("sdk_version") is not None:
            _nonempty_string(robot.get("sdk_version"), "robot.sdk_version")
        if robot.get("timeout_sec") is not None:
            _positive_float(robot.get("timeout_sec"), "robot.timeout_sec")

    sensor = payload.get("sensor")
    if isinstance(sensor, Mapping):
        for field_name in ("transport", "channel", "zeroing_policy"):
            if sensor.get(field_name) is not None:
                _nonempty_string(sensor.get(field_name), f"sensor.{field_name}")
        if sensor.get("bitrate") is not None:
            _positive_int(sensor.get("bitrate"), "sensor.bitrate")
        if sensor.get("sample_rate_hz") is not None:
            _positive_float(sensor.get("sample_rate_hz"), "sensor.sample_rate_hz")

    control = payload.get("control")
    if isinstance(control, Mapping):
        friction = control.get("friction_compensation")
        if isinstance(friction, Mapping):
            if friction.get("direct_teach") is not None:
                _finite_float(
                    friction.get("direct_teach"),
                    "control.friction_compensation.direct_teach",
                )
            if friction.get("normal") is not None:
                _finite_float(
                    friction.get("normal"),
                    "control.friction_compensation.normal",
                )
        if control.get("custom_gain") is not None:
            _validated_gain(control.get("custom_gain"), "control.custom_gain")
        if control.get("force_threshold") is not None:
            _positive_float(
                control.get("force_threshold"), "control.force_threshold"
            )
        if control.get("force_freshness_timeout_sec") is not None:
            _positive_float(
                control.get("force_freshness_timeout_sec"),
                "control.force_freshness_timeout_sec",
            )
        if control.get("stop_policy") is not None:
            _stop_policy(control.get("stop_policy"), "control.stop_policy")
        impedance = control.get("impedance")
        if isinstance(impedance, Mapping):
            if impedance.get("target_force_n") is not None:
                _finite_float(
                    impedance.get("target_force_n"),
                    "control.impedance.target_force_n",
                )
            if impedance.get("tolerance_n") is not None:
                _nonnegative_float(
                    impedance.get("tolerance_n"),
                    "control.impedance.tolerance_n",
                )

    side_mapping = payload.get("side_mapping")
    if isinstance(side_mapping, Mapping):
        for side_name in ("LEFT", "RIGHT"):
            value = side_mapping.get(side_name)
            if not isinstance(value, Mapping):
                continue
            path = f"side_mapping.{side_name}"
            if value.get("arm_id") is not None:
                _nonempty_string(value.get("arm_id"), f"{path}.arm_id")
            if value.get("role_instance_id") is not None:
                _nonempty_string(
                    value.get("role_instance_id"), f"{path}.role_instance_id"
                )
            if value.get("sensor_id") is not None:
                _sensor_id(value.get("sensor_id"), f"{path}.sensor_id")
            start = value.get("joint_start")
            end = value.get("joint_end")
            if start is not None:
                start = _nonnegative_int(start, f"{path}.joint_start")
            if end is not None:
                end = _nonnegative_int(end, f"{path}.joint_end")
            if start is not None and end is not None and end <= start:
                raise _schema_error(
                    "joint_end must be greater than joint_start",
                    f"{path}.joint_end",
                )
            for subsection in ("jog", "force"):
                axis_mapping = value.get(subsection)
                if not isinstance(axis_mapping, Mapping):
                    continue
                subpath = f"{path}.{subsection}"
                if axis_mapping.get("axis") is not None:
                    _nonempty_string(axis_mapping.get("axis"), f"{subpath}.axis")
                if axis_mapping.get("sign") is not None:
                    sign = _strict_int(axis_mapping.get("sign"), f"{subpath}.sign")
                    if sign not in (-1, 1):
                        raise _schema_error(
                            "sign must be exactly -1 or 1", f"{subpath}.sign"
                        )
                if axis_mapping.get("frame") is not None:
                    _nonempty_string(axis_mapping.get("frame"), f"{subpath}.frame")

    tool_change = payload.get("tool_change")
    if isinstance(tool_change, Mapping) and tool_change.get("waypoints") is not None:
        _parse_tool_change(tool_change)

    limits = payload.get("limits")
    if isinstance(limits, Mapping):
        single = limits.get("single_jog_mm")
        cumulative = limits.get("cumulative_jog_mm")
        if single is not None:
            single = _positive_float(single, "limits.single_jog_mm")
        if cumulative is not None:
            cumulative = _positive_float(cumulative, "limits.cumulative_jog_mm")
        if single is not None and cumulative is not None and cumulative < single:
            raise _schema_error(
                "cumulative_jog_mm must be greater than or equal to single_jog_mm",
                "limits.cumulative_jog_mm",
            )


def _parse_robot(value: object) -> RobotSettings:
    node = _required_mapping(value, "robot")
    _check_keys(
        node,
        {"controller_ip", "sdk_version", "timeout_sec"},
        {"controller_ip", "sdk_version", "timeout_sec"},
        "robot",
    )
    controller_ip = _nonempty_string(
        node.get("controller_ip"), "robot.controller_ip"
    )
    try:
        ipaddress.ip_address(controller_ip)
    except ValueError as exc:
        raise _schema_error(
            "controller_ip must be an IP address", "robot.controller_ip"
        ) from exc
    return RobotSettings(
        controller_ip=controller_ip,
        sdk_version=_nonempty_string(node.get("sdk_version"), "robot.sdk_version"),
        timeout_sec=_positive_float(node.get("timeout_sec"), "robot.timeout_sec"),
    )


def _parse_sensor(value: object) -> SensorSettings:
    node = _required_mapping(value, "sensor")
    fields = {
        "transport",
        "channel",
        "bitrate",
        "sample_rate_hz",
        "zeroing_policy",
    }
    _check_keys(node, fields, fields, "sensor")
    return SensorSettings(
        transport=_nonempty_string(node.get("transport"), "sensor.transport"),
        channel=_nonempty_string(node.get("channel"), "sensor.channel"),
        bitrate=_positive_int(node.get("bitrate"), "sensor.bitrate"),
        sample_rate_hz=_positive_float(
            node.get("sample_rate_hz"), "sensor.sample_rate_hz"
        ),
        zeroing_policy=_nonempty_string(
            node.get("zeroing_policy"), "sensor.zeroing_policy"
        ),
    )


def _parse_data(value: object) -> Path:
    node = _required_mapping(value, "data")
    _check_keys(node, {"directory"}, {"directory"}, "data")
    raw_path = _nonempty_string(node.get("directory"), "data.directory")
    path = Path(raw_path)
    if not path.is_absolute():
        raise _schema_error("data.directory must be absolute", "data.directory")
    if path == Path(path.anchor):
        raise _schema_error(
            "data.directory must not be a filesystem root", "data.directory"
        )
    return path


def _draft_data_directory(value: object) -> Path | None:
    if value is None:
        return None
    node = _mapping(value, "data")
    raw_path = node.get("directory")
    if raw_path is None:
        return None
    return _parse_data(node)


def _parse_control(value: object) -> ControlSettings:
    node = _required_mapping(value, "control")
    _check_keys(
        node,
        {
            "friction_compensation",
            "custom_gain",
            "force_threshold",
            "force_freshness_timeout_sec",
            "impedance",
            "stop_policy",
        },
        {
            "friction_compensation",
            "custom_gain",
            "force_threshold",
            "force_freshness_timeout_sec",
            "impedance",
            "stop_policy",
        },
        "control",
    )
    friction = _required_mapping(
        node.get("friction_compensation"), "control.friction_compensation"
    )
    _check_keys(
        friction,
        {"direct_teach", "normal"},
        {"direct_teach", "normal"},
        "control.friction_compensation",
    )
    custom_gain = _validated_gain(node.get("custom_gain"), "control.custom_gain")
    impedance = _required_mapping(node.get("impedance"), "control.impedance")
    _check_keys(
        impedance,
        {"target_force_n", "tolerance_n"},
        {"target_force_n", "tolerance_n"},
        "control.impedance",
    )
    return ControlSettings(
        teach_friction=_finite_float(
            friction.get("direct_teach"),
            "control.friction_compensation.direct_teach",
        ),
        normal_friction=_finite_float(
            friction.get("normal"), "control.friction_compensation.normal"
        ),
        custom_gain=custom_gain,
        force_threshold=_positive_float(
            node.get("force_threshold"), "control.force_threshold"
        ),
        force_freshness_timeout_sec=_positive_float(
            node.get("force_freshness_timeout_sec"),
            "control.force_freshness_timeout_sec",
        ),
        impedance_target_force_n=_finite_float(
            impedance.get("target_force_n"),
            "control.impedance.target_force_n",
        ),
        impedance_tolerance_n=_nonnegative_float(
            impedance.get("tolerance_n"),
            "control.impedance.tolerance_n",
        ),
        stop_policy=_stop_policy(node.get("stop_policy"), "control.stop_policy"),
    )


def _parse_side_mappings(value: object) -> dict[TargetSide, SideMapping]:
    node = _required_mapping(value, "side_mapping")
    _check_keys(node, {"LEFT", "RIGHT"}, {"LEFT", "RIGHT"}, "side_mapping")
    result: dict[TargetSide, SideMapping] = {}
    for side_name, side in (("LEFT", TargetSide.LEFT), ("RIGHT", TargetSide.RIGHT)):
        path = f"side_mapping.{side_name}"
        entry = _required_mapping(node.get(side_name), path)
        required = {
            "arm_id",
            "role_instance_id",
            "sensor_id",
            "joint_start",
            "joint_end",
            "jog",
        }
        _check_keys(
            entry,
            required | {"force"},
            required,
            path,
        )
        joint_start = _nonnegative_int(entry.get("joint_start"), f"{path}.joint_start")
        joint_end = _nonnegative_int(entry.get("joint_end"), f"{path}.joint_end")
        if joint_end <= joint_start:
            raise _schema_error(
                "joint_end must be greater than joint_start",
                f"{path}.joint_end",
            )
        jog = _parse_axis_mapping(entry.get("jog"), f"{path}.jog", require_frame=True)
        force_value = entry.get("force")
        force = (
            _parse_axis_mapping(force_value, f"{path}.force", require_frame=False)
            if force_value is not None
            else None
        )
        result[side] = SideMapping(
            side=side,
            arm_id=_nonempty_string(entry.get("arm_id"), f"{path}.arm_id"),
            role_instance_id=_nonempty_string(
                entry.get("role_instance_id"), f"{path}.role_instance_id"
            ),
            sensor_id=_sensor_id(entry.get("sensor_id"), f"{path}.sensor_id"),
            joint_slice=JointSlice(joint_start, joint_end),
            jog_axis=jog[0],
            jog_sign=jog[1],
            jog_frame=jog[2] or "",
            force_axis=force[0] if force else None,
            force_sign=force[1] if force else None,
        )
    if result[TargetSide.LEFT].arm_id == result[TargetSide.RIGHT].arm_id:
        raise _schema_error(
            "LEFT and RIGHT must map to distinct arm_id values",
            "side_mapping",
        )
    if (
        result[TargetSide.LEFT].role_instance_id
        == result[TargetSide.RIGHT].role_instance_id
    ):
        raise _schema_error(
            "LEFT and RIGHT must map to distinct role_instance_id values",
            "side_mapping",
        )
    if result[TargetSide.LEFT].sensor_id == result[TargetSide.RIGHT].sensor_id:
        raise _schema_error(
            "LEFT and RIGHT must map to distinct sensor_id values",
            "side_mapping",
        )
    return result


def _parse_axis_mapping(
    value: object, path: str, *, require_frame: bool
) -> tuple[str, int, str | None]:
    node = _required_mapping(value, path)
    required = {"axis", "sign"} | ({"frame"} if require_frame else set())
    _check_keys(node, {"axis", "sign", "frame"}, required, path)
    sign = _strict_int(node.get("sign"), f"{path}.sign")
    if sign not in (-1, 1):
        raise _schema_error("sign must be exactly -1 or 1", f"{path}.sign")
    frame = _optional_string(node.get("frame"), f"{path}.frame")
    if require_frame and frame is None:
        raise _missing_calibration(f"{path}.frame")
    return _nonempty_string(node.get("axis"), f"{path}.axis"), sign, frame


def _parse_tool_change(value: object) -> tuple[ToolChangeWaypoint, ...]:
    node = _required_mapping(value, "tool_change")
    _check_keys(node, {"waypoints"}, {"waypoints"}, "tool_change")
    values = node.get("waypoints")
    if (
        isinstance(values, (str, bytes))
        or not isinstance(values, Sequence)
        or not values
    ):
        raise _missing_calibration("tool_change.waypoints")
    waypoints: list[ToolChangeWaypoint] = []
    for index, raw in enumerate(values):
        path = f"tool_change.waypoints[{index}]"
        waypoint = _required_mapping(raw, path)
        _check_keys(
            waypoint,
            {"name", "arm_id", "joint_positions"},
            {"name", "arm_id", "joint_positions"},
            path,
        )
        joints = _numeric_sequence(
            waypoint.get("joint_positions"), f"{path}.joint_positions"
        )
        if not joints:
            raise _missing_calibration(f"{path}.joint_positions")
        waypoints.append(
            ToolChangeWaypoint(
                name=_nonempty_string(waypoint.get("name"), f"{path}.name"),
                arm_id=_nonempty_string(
                    waypoint.get("arm_id"), f"{path}.arm_id"
                ),
                joint_positions=joints,
            )
        )
    return tuple(waypoints)


def _parse_limits(value: object) -> JogLimits:
    node = _required_mapping(value, "limits")
    _check_keys(
        node,
        {"single_jog_mm", "cumulative_jog_mm"},
        {"single_jog_mm", "cumulative_jog_mm"},
        "limits",
    )
    single = _positive_float(node.get("single_jog_mm"), "limits.single_jog_mm")
    cumulative = _positive_float(
        node.get("cumulative_jog_mm"), "limits.cumulative_jog_mm"
    )
    if cumulative < single:
        raise _schema_error(
            "cumulative_jog_mm must be greater than or equal to single_jog_mm",
            "limits.cumulative_jog_mm",
        )
    return JogLimits(single_jog_mm=single, cumulative_jog_mm=cumulative)


def _draft_readiness_issues(
    payload: Mapping[str, Any],
    revision: str | None,
    expected_checksum: str | None,
) -> tuple[str, ...]:
    issues = ["calibration.approved is false"]
    if revision is None:
        issues.append("calibration.revision is missing")
    if expected_checksum is None:
        issues.append("calibration.expected_checksum is missing")
    sections = (
        "robot",
        "sensor",
        "data",
        "control",
        "side_mapping",
        "tool_change",
        "limits",
    )
    for section in sections:
        value = payload.get(section)
        if value is None or (isinstance(value, Mapping) and not value):
            issues.append(f"{section} is incomplete")
    return tuple(issues)


def _optional_checksum(value: object, path: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ProfileValidationError(
            ErrorCode.PROFILE_CHECKSUM_INVALID,
            "expected_checksum must be a SHA-256 string or null",
            field=path,
        )
    match = _CHECKSUM_RE.fullmatch(value)
    if match is None:
        raise ProfileValidationError(
            ErrorCode.PROFILE_CHECKSUM_INVALID,
            "expected_checksum must contain exactly 64 hexadecimal SHA-256 digits",
            field=path,
        )
    return "sha256:" + match.group(1).lower()


def _optional_public_procedure_type(value: object, path: str) -> str | None:
    if value is None or value == "":
        return None
    normalized = _nonempty_string(value, path)
    if normalized not in {"thyroidectomy", "nephrectomy"}:
        raise _schema_error(
            "public_procedure_type must be thyroidectomy, nephrectomy, or null",
            path,
        )
    return normalized


def _validated_gain(value: object, path: str) -> object:
    if value is None:
        raise _missing_calibration(path)
    if isinstance(value, Mapping):
        if not value:
            raise _missing_calibration(path)
        result: dict[str, object] = {}
        for key, child in value.items():
            if not isinstance(key, str) or not key:
                raise _schema_error("custom_gain keys must be non-empty strings", path)
            result[key] = _validated_gain_value(child, f"{path}.{key}")
        return MappingProxyType(result)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if not value:
            raise _missing_calibration(path)
        return tuple(_validated_gain_value(child, f"{path}[]") for child in value)
    raise _schema_error("custom_gain must be a non-empty mapping or sequence", path)


def _validated_gain_value(value: object, path: str) -> object:
    if isinstance(value, Mapping):
        if not value:
            raise _missing_calibration(path)
        return MappingProxyType(
            {
                _nonempty_string(key, path): _validated_gain_value(
                    child, f"{path}.{key}"
                )
                for key, child in value.items()
            }
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if not value:
            raise _missing_calibration(path)
        return tuple(_validated_gain_value(child, f"{path}[]") for child in value)
    return _finite_float(value, path)


def _numeric_sequence(value: object, path: str) -> tuple[float, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise _schema_error("value must be a numeric sequence", path)
    return tuple(
        _finite_float(item, f"{path}[{index}]")
        for index, item in enumerate(value)
    )


def _sensor_id(value: object, path: str) -> str | int:
    if isinstance(value, bool):
        raise _schema_error("sensor_id must be a string or non-negative integer", path)
    if isinstance(value, int):
        if value < 0:
            raise _schema_error("numeric sensor_id must be non-negative", path)
        return value
    return _nonempty_string(value, path)


def _stop_policy(value: object, path: str) -> str:
    policy = _nonempty_string(value, path)
    if policy not in {"stop", "hold"}:
        raise _schema_error("stop_policy must be exactly 'stop' or 'hold'", path)
    return policy


def _strict_bool(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        raise _schema_error("value must be boolean", path)
    return value


def _strict_int(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _schema_error("value must be an integer", path)
    return int(value)


def _nonnegative_int(value: object, path: str) -> int:
    converted = _strict_int(value, path)
    if converted < 0:
        raise _schema_error("value must be non-negative", path)
    return converted


def _positive_int(value: object, path: str) -> int:
    converted = _strict_int(value, path)
    if converted <= 0:
        raise _schema_error("value must be positive", path)
    return converted


def _finite_float(value: object, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise _schema_error("value must be a real number", path)
    converted = float(value)
    if not math.isfinite(converted):
        raise _schema_error("value must be finite", path)
    return converted


def _positive_float(value: object, path: str) -> float:
    converted = _finite_float(value, path)
    if converted <= 0.0:
        raise _schema_error("value must be positive", path)
    return converted


def _nonnegative_float(value: object, path: str) -> float:
    converted = _finite_float(value, path)
    if converted < 0.0:
        raise _schema_error("value must be non-negative", path)
    return converted


def _nonempty_string(value: object, path: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise _schema_error("value must be a non-empty trimmed string", path)
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise _schema_error("value must not contain control characters", path)
    return value


def _optional_string(value: object, path: str) -> str | None:
    if value is None or value == "":
        return None
    return _nonempty_string(value, path)


def _mapping(value: object, path: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _schema_error("value must be a mapping", path)
    if not all(isinstance(key, str) for key in value):
        raise _schema_error("mapping keys must be strings", path)
    return dict(value)


def _required_mapping(value: object, path: str) -> dict[str, Any]:
    if value is None:
        raise _missing_calibration(path)
    return _mapping(value, path)


def _check_keys(
    node: Mapping[str, Any],
    allowed: set[str] | frozenset[str],
    required: set[str] | frozenset[str],
    path: str,
) -> None:
    unknown = sorted(set(node) - set(allowed))
    if unknown:
        raise _schema_error(f"unknown keys: {', '.join(unknown)}", path)
    missing = sorted(set(required) - set(node))
    if missing:
        raise _schema_error(f"missing required keys: {', '.join(missing)}", path)


def _schema_error(message: str, path: str) -> ProfileValidationError:
    return ProfileValidationError(
        ErrorCode.PROFILE_SCHEMA_INVALID,
        message,
        field=path,
    )


def _missing_calibration(path: str) -> ProfileValidationError:
    return ProfileValidationError(
        ErrorCode.CALIBRATION_VALUE_MISSING,
        "approved profile is missing a motion-critical calibration value",
        field=path,
    )


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze(child) for key, child in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze(child) for child in value)
    if isinstance(value, tuple):
        return tuple(_freeze(child) for child in value)
    return value


def _plain_copy(value: object) -> object:
    """Recursively copy regular and immutable mapping/sequence containers."""

    if isinstance(value, Mapping):
        return {key: _plain_copy(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_copy(child) for child in value]
    return deepcopy(value)


__all__ = [
    "ControlSettings",
    "DraftProfile",
    "ExecutionProfile",
    "JogLimits",
    "JointSlice",
    "LoadedProfile",
    "PROFILE_SCHEMA_VERSION",
    "RobotSettings",
    "SensorSettings",
    "SideMapping",
    "ToolChangeWaypoint",
    "canonical_profile_payload",
    "compute_profile_checksum",
    "load_profile",
    "load_profile_mapping",
    "validate_profile",
]
