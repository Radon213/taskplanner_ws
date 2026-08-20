from __future__ import annotations

from types import SimpleNamespace

import pytest

from retraction_control.command_models import (
    Command,
    CommandRequest,
    CommandValidationError,
    ErrorCode,
    ResultCode,
    TargetSide,
    validate_request,
)


@pytest.mark.parametrize("command", list(Command))
def test_all_six_wire_commands_validate(command: Command) -> None:
    adjustment = command is Command.ADJUST_RETRACTION
    request = CommandRequest.from_wire(
        protocol_version=1,
        source_id="taskplanner.bt_orchestrator",
        command_id=f"cmd-{int(command)}",
        command=int(command),
        target_side=int(TargetSide.LEFT if adjustment else TargetSide.NONE),
        distance_m=0.050 if adjustment else 0.0,
    )

    assert request.command is command
    assert request.distance_m == (0.050 if adjustment else 0.0)


@pytest.mark.parametrize("value", [0, 2, True, 1.0, "1"])
def test_protocol_version_is_exact_integer_v1(value: object) -> None:
    with pytest.raises(CommandValidationError) as raised:
        CommandRequest.from_wire(
            protocol_version=value,
            source_id="taskplanner",
            command_id="cmd-1",
            command=1,
            target_side=0,
            distance_m=0.0,
        )

    assert raised.value.code in {
        ErrorCode.UNSUPPORTED_PROTOCOL_VERSION,
        ErrorCode.INVALID_NUMERIC_VALUE,
    }


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("source_id", "", ErrorCode.INVALID_SOURCE_ID),
        ("source_id", " taskplanner", ErrorCode.INVALID_SOURCE_ID),
        ("source_id", "task planner", ErrorCode.INVALID_SOURCE_ID),
        ("command_id", "", ErrorCode.INVALID_COMMAND_ID),
        ("command_id", "cmd\n1", ErrorCode.INVALID_COMMAND_ID),
    ],
)
def test_source_and_command_identifiers_are_closed_and_bounded(
    field: str, value: str, code: ErrorCode
) -> None:
    fields: dict[str, object] = {
        "protocol_version": 1,
        "source_id": "taskplanner",
        "command_id": "cmd-1",
        "command": 1,
        "target_side": 0,
        "distance_m": 0.0,
    }
    fields[field] = value

    with pytest.raises(CommandValidationError) as raised:
        CommandRequest.from_wire(**fields)

    assert raised.value.code is code
    assert raised.value.field == field


def test_unknown_command_maps_to_service_invalid_command_result() -> None:
    with pytest.raises(CommandValidationError) as raised:
        CommandRequest(1, "taskplanner", "cmd-x", 99, 0, 0.0)  # type: ignore[arg-type]

    assert raised.value.code is ErrorCode.INVALID_COMMAND
    assert raised.value.result_code is ResultCode.INVALID_COMMAND


@pytest.mark.parametrize("side", [TargetSide.NONE, 3, -1])
def test_adjustment_requires_left_or_right(side: TargetSide | int) -> None:
    with pytest.raises(CommandValidationError) as raised:
        CommandRequest(  # type: ignore[arg-type]
            1, "taskplanner", "cmd-adjust", 4, side, 0.050
        )

    assert raised.value.code in {
        ErrorCode.TARGET_SIDE_REQUIRED,
        ErrorCode.INVALID_TARGET_SIDE,
    }


@pytest.mark.parametrize("distance", [0.0, -0.001, float("nan"), float("inf")])
def test_adjustment_requires_positive_finite_distance(distance: float) -> None:
    with pytest.raises(CommandValidationError) as raised:
        CommandRequest(  # type: ignore[arg-type]
            1, "taskplanner", "cmd-adjust", 4, 1, distance
        )

    assert raised.value.field == "distance_m"


@pytest.mark.parametrize(
    ("side", "distance", "code"),
    [
        (TargetSide.LEFT, 0.0, ErrorCode.TARGET_SIDE_NOT_ALLOWED),
        (TargetSide.NONE, 0.001, ErrorCode.DISTANCE_NOT_ALLOWED),
    ],
)
def test_non_adjustment_rejects_adjustment_only_parameters(
    side: TargetSide, distance: float, code: ErrorCode
) -> None:
    with pytest.raises(CommandValidationError) as raised:
        CommandRequest(  # type: ignore[arg-type]
            1, "taskplanner", "cmd-start", 1, side, distance
        )

    assert raised.value.code is code


def test_ros_like_request_is_adapted_without_importing_ros() -> None:
    ros_request = SimpleNamespace(
        protocol_version=1,
        source_id="taskplanner",
        command_id="cmd-ros",
        command=3,
        target_side=0,
        distance_m=0.0,
    )

    validated = validate_request(ros_request)

    assert validated.command is Command.START_RETRACTION
    assert validated.as_dict()["command"] == 3
