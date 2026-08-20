from __future__ import annotations

import pytest

from retraction_control.algorithms.force_jog import (
    distance_m_to_mm,
    meters_to_millimeters,
)
from retraction_control.command_models import AlgorithmValidationError, ErrorCode


@pytest.mark.parametrize(
    ("distance_m", "distance_mm"),
    [
        (0.050, 50.0),
        (0.005, 5.0),
        (0.001, 1.0),
    ],
)
def test_si_distance_is_converted_once_at_boundary(
    distance_m: float, distance_mm: float
) -> None:
    assert meters_to_millimeters(distance_m) == distance_mm
    assert distance_m_to_mm(distance_m) == distance_mm


@pytest.mark.parametrize(
    "distance_m",
    [0.0, -0.001, float("nan"), float("inf"), True, "0.05"],
)
def test_invalid_distance_never_reaches_sdk_units(distance_m: object) -> None:
    with pytest.raises(AlgorithmValidationError) as raised:
        meters_to_millimeters(distance_m)  # type: ignore[arg-type]

    assert raised.value.code in {
        ErrorCode.INVALID_DISTANCE,
        ErrorCode.INVALID_NUMERIC_VALUE,
    }
