from __future__ import annotations

import pytest

from retraction_control.algorithms.force_analysis import (
    ForceTorqueSample,
    analyze_force_records,
    analyze_force_samples,
)
from retraction_control.algorithms.impedance import compute_impedance_correction
from retraction_control.algorithms.joint_targets import (
    compose_joint_target,
    mean_joint_positions,
)
from retraction_control.command_models import AlgorithmValidationError, ErrorCode
from retraction_control.profile_loader import JointSlice


def test_joint_target_replaces_only_configured_slice() -> None:
    target = compose_joint_target(
        current_joints=(1.0, 2.0, 3.0, 4.0),
        taught_joints=(30.0, 40.0),
        joint_slice=JointSlice(2, 4),
    )

    assert target.positions == (1.0, 2.0, 30.0, 40.0)
    assert target.replaced_slice == JointSlice(2, 4)


def test_joint_sample_mean_requires_rectangular_finite_data() -> None:
    assert mean_joint_positions(((1.0, 3.0), (3.0, 5.0))) == (2.0, 4.0)

    with pytest.raises(AlgorithmValidationError) as raised:
        mean_joint_positions(((1.0, 2.0), (3.0,)))
    assert raised.value.code is ErrorCode.INVALID_VECTOR_LENGTH


def test_impedance_uses_only_explicit_gain_and_clamp_values() -> None:
    correction = compute_impedance_correction(
        target_force_n=10.0,
        measured_force_n=4.0,
        stiffness_n_per_mm=2.0,
        damping_n_s_per_mm=0.5,
        measured_velocity_mm_s=2.0,
        max_abs_offset_mm=2.0,
    )

    assert correction.force_error_n == 6.0
    assert correction.unconstrained_offset_mm == 2.5
    assert correction.offset_mm == 2.0
    assert correction.saturated is True


def test_force_analysis_projects_approved_axis_and_checks_freshness() -> None:
    samples = (
        ForceTorqueSample(10.0, (1.0, 2.0, 3.0), (0.1, 0.2, 0.3)),
        ForceTorqueSample(10.1, (2.0, 4.0, 6.0), (0.2, 0.4, 0.6)),
    )

    result = analyze_force_samples(
        samples,
        axis="fy",
        sign=-1,
        now_s=10.15,
        freshness_timeout_s=0.1,
        min_samples=2,
    )

    assert result.projected_values_n == (-2.0, -4.0)
    assert result.mean_force_n == -3.0
    assert result.latest_force_n == -4.0


def test_force_analysis_rejects_stale_and_non_monotonic_samples() -> None:
    samples = (
        ForceTorqueSample(10.0, (1.0, 2.0, 3.0), (0.1, 0.2, 0.3)),
        ForceTorqueSample(10.1, (2.0, 4.0, 6.0), (0.2, 0.4, 0.6)),
    )
    with pytest.raises(AlgorithmValidationError) as stale:
        analyze_force_samples(
            samples,
            axis="x",
            sign=1,
            now_s=10.3,
            freshness_timeout_s=0.1,
            min_samples=2,
        )
    assert stale.value.code is ErrorCode.STALE_FORCE_SAMPLE

    with pytest.raises(AlgorithmValidationError) as unordered:
        analyze_force_samples(
            tuple(reversed(samples)),
            axis="x",
            sign=1,
            now_s=10.3,
            freshness_timeout_s=1.0,
            min_samples=2,
        )
    assert unordered.value.code is ErrorCode.NON_MONOTONIC_SAMPLES


def test_force_record_never_substitutes_missing_torque_data() -> None:
    with pytest.raises(AlgorithmValidationError) as raised:
        analyze_force_records(
            ({"timestamp_s": 1.0, "force_n": (1.0, 2.0, 3.0)},),
            axis="x",
            sign=1,
            now_s=1.0,
            freshness_timeout_s=0.1,
            min_samples=1,
        )

    assert raised.value.code is ErrorCode.INVALID_SAMPLE
    assert raised.value.field == "records[0]"
