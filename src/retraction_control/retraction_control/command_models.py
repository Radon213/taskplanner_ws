"""Typed, ROS-independent command and error contracts.

The public ROS service is intentionally a very small wire contract.  This
module is the strict boundary between that untrusted wire representation and
the control core.  A :class:`CommandRequest` can only be constructed when all
fields are valid for the selected command.

No class in this module claims that a physical command has completed.  Service
admission and physical execution are deliberately modelled elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from enum import Enum, IntEnum
import math
from numbers import Real
import re
from types import MappingProxyType
from typing import Any, Mapping


SUPPORTED_PROTOCOL_VERSION = 1
PROTOCOL_VERSION_V1 = SUPPORTED_PROTOCOL_VERSION
MAX_SOURCE_ID_LENGTH = 128
MAX_COMMAND_ID_LENGTH = 128

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")


class Command(IntEnum):
    """Values defined by ``ExecuteRetractionCommand.srv``."""

    START_DIRECT_TEACH = 1
    FINISH_DIRECT_TEACH = 2
    START_RETRACTION = 3
    ADJUST_RETRACTION = 4
    CHANGE_TOOL = 5
    STOP_RETRACTION = 6

    @property
    def is_adjustment(self) -> bool:
        return self is Command.ADJUST_RETRACTION


class TargetSide(IntEnum):
    """Values defined by ``ExecuteRetractionCommand.srv``."""

    NONE = 0
    LEFT = 1
    RIGHT = 2


class ResultCode(IntEnum):
    """Response result values defined by the public service IDL."""

    ACCEPTED = 0
    INVALID_COMMAND = 1
    INVALID_PARAMETER = 2
    REJECTED = 3
    ERROR = 255


class ErrorCategory(str, Enum):
    VALIDATION = "validation"
    STATE = "state"
    PROFILE = "profile"
    LIMIT = "limit"
    SENSOR = "sensor"
    ALGORITHM = "algorithm"
    EXECUTION = "execution"


class ErrorCode(str, Enum):
    """Stable machine-readable error taxonomy used by the pure core."""

    UNSUPPORTED_PROTOCOL_VERSION = "unsupported_protocol_version"
    INVALID_SOURCE_ID = "invalid_source_id"
    INVALID_COMMAND_ID = "invalid_command_id"
    INVALID_COMMAND = "invalid_command"
    INVALID_TARGET_SIDE = "invalid_target_side"
    INVALID_DISTANCE = "invalid_distance"
    TARGET_SIDE_REQUIRED = "target_side_required"
    TARGET_SIDE_NOT_ALLOWED = "target_side_not_allowed"
    DISTANCE_REQUIRED = "distance_required"
    DISTANCE_NOT_ALLOWED = "distance_not_allowed"

    COMMAND_NOT_ALLOWED = "command_not_allowed"
    COMMAND_ALREADY_ACTIVE = "command_already_active"
    COMMAND_NOT_ACTIVE = "command_not_active"
    COMMAND_ID_MISMATCH = "command_id_mismatch"
    SESSION_NOT_VALID = "session_not_valid"
    FAULT_RESET_NOT_VERIFIED = "fault_reset_not_verified"

    PROFILE_SCHEMA_INVALID = "profile_schema_invalid"
    PROFILE_NOT_APPROVED = "profile_not_approved"
    PROFILE_CHECKSUM_INVALID = "profile_checksum_invalid"
    PROFILE_CHECKSUM_MISMATCH = "profile_checksum_mismatch"
    SIDE_MAPPING_MISSING = "side_mapping_missing"
    CALIBRATION_VALUE_MISSING = "calibration_value_missing"

    INVALID_NUMERIC_VALUE = "invalid_numeric_value"
    INVALID_VECTOR_LENGTH = "invalid_vector_length"
    INVALID_JOINT_SLICE = "invalid_joint_slice"
    INVALID_FORCE_AXIS = "invalid_force_axis"
    INVALID_SAMPLE = "invalid_sample"
    NON_MONOTONIC_SAMPLES = "non_monotonic_samples"
    INSUFFICIENT_FORCE_SAMPLES = "insufficient_force_samples"
    STALE_FORCE_SAMPLE = "stale_force_sample"
    DISTANCE_LIMIT_EXCEEDED = "distance_limit_exceeded"
    CUMULATIVE_DISTANCE_LIMIT_EXCEEDED = "cumulative_distance_limit_exceeded"


@dataclass(frozen=True, slots=True)
class ErrorDetail:
    """Serializable details for a rejected command or failed computation."""

    code: ErrorCode
    category: ErrorCategory
    message: str
    result_code: ResultCode
    field: str | None = None
    context: Mapping[str, Any] = dataclass_field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "context", MappingProxyType(dict(self.context)))

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "category": self.category.value,
            "message": self.message,
            "result_code": int(self.result_code),
            "field": self.field,
            "context": dict(self.context),
        }


class RetractionControlError(ValueError):
    """Base exception carrying a stable :class:`ErrorDetail`."""

    default_category = ErrorCategory.EXECUTION
    default_result_code = ResultCode.ERROR

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        field: str | None = None,
        category: ErrorCategory | None = None,
        result_code: ResultCode | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        self.detail = ErrorDetail(
            code=code,
            category=category if category is not None else self.default_category,
            message=message,
            result_code=(
                result_code
                if result_code is not None
                else self.default_result_code
            ),
            field=field,
            context=context or {},
        )
        super().__init__(f"{code.value}: {message}")

    @property
    def code(self) -> ErrorCode:
        return self.detail.code

    @property
    def field(self) -> str | None:
        return self.detail.field

    @property
    def category(self) -> ErrorCategory:
        return self.detail.category

    @property
    def result_code(self) -> ResultCode:
        return self.detail.result_code

    @property
    def context(self) -> Mapping[str, Any]:
        return self.detail.context

    def as_dict(self) -> dict[str, Any]:
        return self.detail.as_dict()


class CommandValidationError(RetractionControlError):
    default_category = ErrorCategory.VALIDATION
    default_result_code = ResultCode.INVALID_PARAMETER


class StateTransitionError(RetractionControlError):
    default_category = ErrorCategory.STATE
    default_result_code = ResultCode.REJECTED


class ProfileValidationError(RetractionControlError):
    default_category = ErrorCategory.PROFILE
    default_result_code = ResultCode.ERROR


class AlgorithmValidationError(RetractionControlError):
    default_category = ErrorCategory.ALGORITHM
    default_result_code = ResultCode.ERROR


def _wire_integer(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CommandValidationError(
            ErrorCode.INVALID_NUMERIC_VALUE,
            f"{field_name} must be an integer",
            field=field_name,
        )
    return int(value)


def _wire_float(value: object, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise CommandValidationError(
            ErrorCode.INVALID_NUMERIC_VALUE,
            f"{field_name} must be a real number",
            field=field_name,
        )
    converted = float(value)
    if not math.isfinite(converted):
        raise CommandValidationError(
            ErrorCode.INVALID_NUMERIC_VALUE,
            f"{field_name} must be finite",
            field=field_name,
        )
    return converted


def _identifier(
    value: object,
    *,
    field_name: str,
    max_length: int,
    error_code: ErrorCode,
) -> str:
    if not isinstance(value, str):
        raise CommandValidationError(
            error_code,
            f"{field_name} must be a string",
            field=field_name,
        )
    if not value or value != value.strip():
        raise CommandValidationError(
            error_code,
            f"{field_name} must be non-empty and must not contain outer whitespace",
            field=field_name,
        )
    if len(value) > max_length or _IDENTIFIER_RE.fullmatch(value) is None:
        raise CommandValidationError(
            error_code,
            (
                f"{field_name} must be at most {max_length} ASCII identifier "
                "characters"
            ),
            field=field_name,
        )
    return value


@dataclass(frozen=True, slots=True)
class CommandRequest:
    """One fully validated service request.

    ``distance_m`` remains in the public SI unit.  Conversion to millimetres is
    performed exactly once by :mod:`algorithms.force_jog`.
    """

    protocol_version: int
    source_id: str
    command_id: str
    command: Command
    target_side: TargetSide
    distance_m: float

    def __post_init__(self) -> None:
        protocol_version = _wire_integer(
            self.protocol_version, field_name="protocol_version"
        )
        if protocol_version != SUPPORTED_PROTOCOL_VERSION:
            raise CommandValidationError(
                ErrorCode.UNSUPPORTED_PROTOCOL_VERSION,
                (
                    f"protocol_version must be {SUPPORTED_PROTOCOL_VERSION}, "
                    f"got {protocol_version}"
                ),
                field="protocol_version",
                context={"supported": SUPPORTED_PROTOCOL_VERSION},
            )

        source_id = _identifier(
            self.source_id,
            field_name="source_id",
            max_length=MAX_SOURCE_ID_LENGTH,
            error_code=ErrorCode.INVALID_SOURCE_ID,
        )
        command_id = _identifier(
            self.command_id,
            field_name="command_id",
            max_length=MAX_COMMAND_ID_LENGTH,
            error_code=ErrorCode.INVALID_COMMAND_ID,
        )

        try:
            command = Command(_wire_integer(self.command, field_name="command"))
        except CommandValidationError:
            raise
        except ValueError as exc:
            raise CommandValidationError(
                ErrorCode.INVALID_COMMAND,
                f"command is not one of the six supported values: {self.command!r}",
                field="command",
                result_code=ResultCode.INVALID_COMMAND,
            ) from exc

        try:
            target_side = TargetSide(
                _wire_integer(self.target_side, field_name="target_side")
            )
        except CommandValidationError:
            raise
        except ValueError as exc:
            raise CommandValidationError(
                ErrorCode.INVALID_TARGET_SIDE,
                f"target_side is invalid: {self.target_side!r}",
                field="target_side",
            ) from exc

        distance_m = _wire_float(self.distance_m, field_name="distance_m")

        if command is Command.ADJUST_RETRACTION:
            if target_side not in (TargetSide.LEFT, TargetSide.RIGHT):
                raise CommandValidationError(
                    ErrorCode.TARGET_SIDE_REQUIRED,
                    "adjust_retraction requires LEFT or RIGHT target_side",
                    field="target_side",
                )
            if distance_m <= 0.0:
                raise CommandValidationError(
                    ErrorCode.DISTANCE_REQUIRED,
                    "adjust_retraction requires a positive distance_m",
                    field="distance_m",
                )
        else:
            if target_side is not TargetSide.NONE:
                raise CommandValidationError(
                    ErrorCode.TARGET_SIDE_NOT_ALLOWED,
                    f"{command.name} requires target_side NONE",
                    field="target_side",
                )
            if distance_m != 0.0:
                raise CommandValidationError(
                    ErrorCode.DISTANCE_NOT_ALLOWED,
                    f"{command.name} requires distance_m 0.0",
                    field="distance_m",
                )

        object.__setattr__(self, "protocol_version", protocol_version)
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "command_id", command_id)
        object.__setattr__(self, "command", command)
        object.__setattr__(self, "target_side", target_side)
        object.__setattr__(self, "distance_m", distance_m)

    @classmethod
    def from_wire(
        cls,
        *,
        protocol_version: object,
        source_id: object,
        command_id: object,
        command: object,
        target_side: object,
        distance_m: object,
    ) -> CommandRequest:
        return cls(
            protocol_version=protocol_version,  # type: ignore[arg-type]
            source_id=source_id,  # type: ignore[arg-type]
            command_id=command_id,  # type: ignore[arg-type]
            command=command,  # type: ignore[arg-type]
            target_side=target_side,  # type: ignore[arg-type]
            distance_m=distance_m,  # type: ignore[arg-type]
        )

    @classmethod
    def from_ros(cls, request: object) -> CommandRequest:
        """Validate a generated ROS Request without importing ROS packages."""

        missing = [
            name
            for name in (
                "protocol_version",
                "source_id",
                "command_id",
                "command",
                "target_side",
                "distance_m",
            )
            if not hasattr(request, name)
        ]
        if missing:
            raise CommandValidationError(
                ErrorCode.INVALID_COMMAND,
                f"request is missing fields: {', '.join(missing)}",
                field=missing[0],
            )
        return cls.from_wire(
            protocol_version=getattr(request, "protocol_version"),
            source_id=getattr(request, "source_id"),
            command_id=getattr(request, "command_id"),
            command=getattr(request, "command"),
            target_side=getattr(request, "target_side"),
            distance_m=getattr(request, "distance_m"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "protocol_version": self.protocol_version,
            "source_id": self.source_id,
            "command_id": self.command_id,
            "command": int(self.command),
            "target_side": int(self.target_side),
            "distance_m": self.distance_m,
        }


ValidatedCommand = CommandRequest
RetractionCommand = Command
RetractionTargetSide = TargetSide

# Wire-name aliases keep the IDL constants discoverable without importing a
# generated ROS module.  IntEnum values remain directly serializable as ints.
COMMAND_START_DIRECT_TEACH = Command.START_DIRECT_TEACH
COMMAND_FINISH_DIRECT_TEACH = Command.FINISH_DIRECT_TEACH
COMMAND_START_RETRACTION = Command.START_RETRACTION
COMMAND_ADJUST_RETRACTION = Command.ADJUST_RETRACTION
COMMAND_CHANGE_TOOL = Command.CHANGE_TOOL
COMMAND_STOP_RETRACTION = Command.STOP_RETRACTION
TARGET_NONE = TargetSide.NONE
TARGET_LEFT = TargetSide.LEFT
TARGET_RIGHT = TargetSide.RIGHT


def validate_request(request: CommandRequest | object) -> CommandRequest:
    """Return a validated request, adapting ROS-like objects when necessary."""

    if isinstance(request, CommandRequest):
        return request
    return CommandRequest.from_ros(request)


validate_command_request = validate_request


__all__ = [
    "AlgorithmValidationError",
    "Command",
    "CommandRequest",
    "CommandValidationError",
    "COMMAND_ADJUST_RETRACTION",
    "COMMAND_CHANGE_TOOL",
    "COMMAND_FINISH_DIRECT_TEACH",
    "COMMAND_START_DIRECT_TEACH",
    "COMMAND_START_RETRACTION",
    "COMMAND_STOP_RETRACTION",
    "ErrorCategory",
    "ErrorCode",
    "ErrorDetail",
    "MAX_COMMAND_ID_LENGTH",
    "MAX_SOURCE_ID_LENGTH",
    "PROTOCOL_VERSION_V1",
    "ProfileValidationError",
    "RetractionCommand",
    "ResultCode",
    "RetractionControlError",
    "RetractionTargetSide",
    "SUPPORTED_PROTOCOL_VERSION",
    "StateTransitionError",
    "TargetSide",
    "TARGET_LEFT",
    "TARGET_NONE",
    "TARGET_RIGHT",
    "ValidatedCommand",
    "validate_command_request",
    "validate_request",
]
