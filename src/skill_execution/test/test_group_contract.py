import pytest

from skill_execution.group_contract import (
    DEFAULT_DISTANCE_MM,
    DIRECTIONS,
    DISTANCE_DEFAULTED,
    DISTANCE_EXPLICIT_WITH_UNIT,
    DISTANCE_QUALITATIVE_INFERRED,
    GROUP_RETRACTION,
    OPERATION_RETRACTION,
    mock_safety_rejection,
    validate_command_values,
)


@pytest.mark.parametrize("direction", sorted(DIRECTIONS))
def test_all_six_wire_directions_are_valid(direction):
    assert (
        validate_command_values(
            group_id=GROUP_RETRACTION,
            operation=OPERATION_RETRACTION,
            direction=direction,
            distance_mm=10.0,
            distance_origin=DISTANCE_DEFAULTED,
            confidence=0.9,
        )
        == ""
    )


def test_only_six_directions_are_exposed():
    assert DIRECTIONS == {
        "UP",
        "DOWN",
        "LEFT",
        "RIGHT",
        "LEFT_RIGHT",
        "UP_DOWN",
    }


def test_explicit_50_mm_is_not_clamped_or_rejected_by_wire_contract():
    assert (
        validate_command_values(
            group_id=GROUP_RETRACTION,
            operation=OPERATION_RETRACTION,
            direction="LEFT_RIGHT",
            distance_mm=50.0,
            distance_origin=DISTANCE_EXPLICIT_WITH_UNIT,
            confidence=0.95,
        )
        == ""
    )


def test_mock_controller_rejects_50_mm_above_configured_limit():
    error_code, reason = mock_safety_rejection(
        operation=OPERATION_RETRACTION,
        distance_mm=50.0,
        max_retraction_mm=30.0,
    )
    assert error_code == "distance_limit_exceeded"
    assert "50 mm" in reason
    assert "30 mm" in reason


@pytest.mark.parametrize("distance_mm", [0.9, 30.1, 50.0])
def test_qualitative_distance_is_limited_to_1_through_30_mm(distance_mm):
    error = validate_command_values(
        group_id=GROUP_RETRACTION,
        operation=OPERATION_RETRACTION,
        direction="UP",
        distance_mm=distance_mm,
        distance_origin=DISTANCE_QUALITATIVE_INFERRED,
        confidence=0.9,
    )
    assert "between 1 and 30 mm" in error


def test_qualitative_distance_must_be_integer_millimetres_at_wire_boundary():
    error = validate_command_values(
        group_id=GROUP_RETRACTION,
        operation=OPERATION_RETRACTION,
        direction="UP",
        distance_mm=10.5,
        distance_origin=DISTANCE_QUALITATIVE_INFERRED,
        confidence=0.9,
    )
    assert "integer number of millimetres" in error


def test_defaulted_distance_must_be_10_mm():
    assert DEFAULT_DISTANCE_MM == 10.0
    error = validate_command_values(
        group_id=GROUP_RETRACTION,
        operation=OPERATION_RETRACTION,
        direction="RIGHT",
        distance_mm=5.0,
        distance_origin=DISTANCE_DEFAULTED,
        confidence=0.9,
    )
    assert error == "defaulted retraction distance must be 10 mm"


def test_removed_suction_group_is_rejected_at_internal_boundary():
    assert validate_command_values(
        group_id="suction",
        operation="suction_start",
        direction="",
        distance_mm=0.0,
        distance_origin="",
        confidence=1.0,
    ) == "unsupported group_id 'suction'"
