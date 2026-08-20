from __future__ import annotations

import pytest

from retraction_control.algorithms.force_jog import build_force_jog
from retraction_control.command_models import (
    AlgorithmValidationError,
    ErrorCategory,
    ErrorCode,
    TargetSide,
)
from retraction_control.profile_loader import JogLimits, JointSlice, SideMapping


def _mapping(side: TargetSide) -> SideMapping:
    if side is TargetSide.LEFT:
        return SideMapping(
            side=side,
            arm_id="arm-left",
            role_instance_id="left-malleable",
            sensor_id=11,
            joint_slice=JointSlice(0, 2),
            jog_axis="x",
            jog_sign=1,
            jog_frame="tcp",
            force_axis="fx",
            force_sign=1,
        )
    return SideMapping(
        side=side,
        arm_id="arm-right",
        role_instance_id="right-malleable",
        sensor_id=12,
        joint_slice=JointSlice(2, 4),
        jog_axis="y",
        jog_sign=-1,
        jog_frame="tcp",
        force_axis="fy",
        force_sign=-1,
    )


def test_left_and_right_generate_distinct_adapter_ready_plans() -> None:
    limits = JogLimits(single_jog_mm=50.0, cumulative_jog_mm=100.0)

    left = build_force_jog(
        mapping=_mapping(TargetSide.LEFT), limits=limits, distance_m=0.050
    )
    right = build_force_jog(
        mapping=_mapping(TargetSide.RIGHT), limits=limits, distance_m=0.050
    )

    assert left.distance_mm == 50.0
    assert left.signed_distance_mm == 50.0
    assert (left.arm_id, left.sensor_id, left.axis, left.frame) == (
        "arm-left",
        11,
        "x",
        "tcp",
    )
    assert right.distance_mm == 50.0
    assert right.signed_distance_mm == -50.0
    assert (right.arm_id, right.sensor_id, right.axis, right.frame) == (
        "arm-right",
        12,
        "y",
        "tcp",
    )


def test_single_and_cumulative_limits_are_inclusive_at_boundary() -> None:
    plan = build_force_jog(
        mapping=_mapping(TargetSide.LEFT),
        limits=JogLimits(single_jog_mm=50.0, cumulative_jog_mm=100.0),
        distance_m=0.050,
        previous_cumulative_mm=50.0,
    )

    assert plan.previous_cumulative_mm == 50.0
    assert plan.cumulative_distance_mm == 100.0


def test_single_jog_limit_violation_has_structured_limit_error() -> None:
    with pytest.raises(AlgorithmValidationError) as raised:
        build_force_jog(
            mapping=_mapping(TargetSide.LEFT),
            limits=JogLimits(single_jog_mm=49.0, cumulative_jog_mm=100.0),
            distance_m=0.050,
        )

    assert raised.value.code is ErrorCode.DISTANCE_LIMIT_EXCEEDED
    assert raised.value.category is ErrorCategory.LIMIT
    assert raised.value.context["distance_mm"] == 50.0


def test_cumulative_limit_violation_is_separate_from_single_limit() -> None:
    with pytest.raises(AlgorithmValidationError) as raised:
        build_force_jog(
            mapping=_mapping(TargetSide.RIGHT),
            limits=JogLimits(single_jog_mm=50.0, cumulative_jog_mm=75.0),
            distance_m=0.050,
            previous_cumulative_mm=30.0,
        )

    assert raised.value.code is ErrorCode.CUMULATIVE_DISTANCE_LIMIT_EXCEEDED
    assert raised.value.context["previous_cumulative_mm"] == 30.0


def test_no_unconfigured_side_or_mapping_object_is_accepted() -> None:
    with pytest.raises(AlgorithmValidationError) as raised:
        build_force_jog(
            mapping=object(),  # type: ignore[arg-type]
            limits=JogLimits(single_jog_mm=50.0, cumulative_jog_mm=100.0),
            distance_m=0.050,
        )

    assert raised.value.code is ErrorCode.SIDE_MAPPING_MISSING


def test_invalid_unapproved_limit_values_fail_closed() -> None:
    with pytest.raises(AlgorithmValidationError) as raised:
        build_force_jog(
            mapping=_mapping(TargetSide.LEFT),
            limits=JogLimits(single_jog_mm=50.0, cumulative_jog_mm=10.0),
            distance_m=0.005,
        )

    assert raised.value.code is ErrorCode.CALIBRATION_VALUE_MISSING
    assert raised.value.category is ErrorCategory.LIMIT
