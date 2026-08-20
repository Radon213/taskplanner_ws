from __future__ import annotations

from copy import deepcopy

import pytest

from retraction_control.command_models import (
    ErrorCode,
    ProfileValidationError,
    TargetSide,
)
from retraction_control.profile_loader import (
    DraftProfile,
    ExecutionProfile,
    compute_profile_checksum,
    load_profile,
)


def approved_profile_payload() -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "profile": {
            "name": "synthetic-test-profile",
            "version": "1.0.0",
            "procedure_type": "synthetic",
        },
        "calibration": {
            "approved": True,
            "revision": "test-only-r1",
            "expected_checksum": None,
        },
        "robot": {
            "controller_ip": "192.0.2.10",
            "sdk_version": "3.5.0.7",
            "timeout_sec": 1.0,
        },
        "data": {"directory": "/tmp/retraction-control-test"},
        "control": {
            "friction_compensation": {"direct_teach": 0.0, "normal": 1.0},
            "custom_gain": {"kp": [1.0, 2.0], "kd": [0.1, 0.2]},
            "force_threshold": 2.0,
            "force_freshness_timeout_sec": 0.1,
            "stop_policy": "hold",
        },
        "side_mapping": {
            "LEFT": {
                "arm_id": "arm-left",
                "role_instance_id": "left-malleable",
                "sensor_id": 11,
                "joint_start": 0,
                "joint_end": 2,
                "jog": {"axis": "x", "sign": 1, "frame": "tcp"},
                "force": {"axis": "fx", "sign": 1},
            },
            "RIGHT": {
                "arm_id": "arm-right",
                "role_instance_id": "right-malleable",
                "sensor_id": 12,
                "joint_start": 2,
                "joint_end": 4,
                "jog": {"axis": "y", "sign": -1, "frame": "tcp"},
                "force": {"axis": "fy", "sign": -1},
            },
        },
        "tool_change": {
            "waypoints": [
                {
                    "name": "synthetic-wait",
                    "arm_id": "arm-left",
                    "joint_positions": [0.0, 0.1],
                }
            ]
        },
        "limits": {"single_jog_mm": 50.0, "cumulative_jog_mm": 100.0},
    }
    calibration = payload["calibration"]
    assert isinstance(calibration, dict)
    calibration["expected_checksum"] = compute_profile_checksum(payload)
    return payload


def test_approved_profile_resolves_distinct_side_arm_sensor_and_axis() -> None:
    loaded = load_profile(approved_profile_payload(), require_approved=True)

    assert isinstance(loaded, ExecutionProfile)
    left = loaded.resolve_side(TargetSide.LEFT)
    right = loaded.resolve_side(TargetSide.RIGHT)
    assert (left.arm_id, left.sensor_id, left.jog_axis, left.jog_sign) == (
        "arm-left",
        11,
        "x",
        1,
    )
    assert (right.arm_id, right.sensor_id, right.jog_axis, right.jog_sign) == (
        "arm-right",
        12,
        "y",
        -1,
    )
    assert loaded.jog_single_max_mm == 50.0
    assert loaded.jog_cumulative_max_mm == 100.0
    assert loaded.stop_policy == "hold"
    assert loaded.force_freshness_timeout_ns == 100_000_000
    assert compute_profile_checksum(loaded.raw) == loaded.checksum


def test_checksum_binds_all_content_except_expected_checksum_field() -> None:
    payload = approved_profile_payload()
    original_checksum = compute_profile_checksum(payload)
    payload["calibration"]["expected_checksum"] = (  # type: ignore[index]
        "sha256:" + original_checksum.removeprefix("sha256:").upper()
    )
    assert compute_profile_checksum(payload) == original_checksum

    payload["limits"]["single_jog_mm"] = 49.0  # type: ignore[index]
    with pytest.raises(ProfileValidationError) as raised:
        load_profile(payload)

    assert raised.value.code is ErrorCode.PROFILE_CHECKSUM_MISMATCH


def test_unapproved_null_filled_profile_loads_only_as_draft() -> None:
    payload = {
        "schema_version": 1,
        "profile": {
            "name": "throat-draft",
            "version": "0.0.0-draft",
            "procedure_type": "throat",
        },
        "calibration": {
            "approved": False,
            "revision": None,
            "expected_checksum": None,
        },
        "robot": {},
        "data": {"directory": None},
        "control": {
            "friction_compensation": None,
            "custom_gain": None,
            "force_threshold": None,
            "force_freshness_timeout_sec": None,
            "stop_policy": None,
        },
        "side_mapping": {"LEFT": None, "RIGHT": None},
        "tool_change": {"waypoints": None},
        "limits": {"single_jog_mm": None, "cumulative_jog_mm": None},
    }

    loaded = load_profile(payload)
    assert isinstance(loaded, DraftProfile)
    assert loaded.calibration_approved is False

    with pytest.raises(ProfileValidationError) as raised:
        load_profile(payload, require_approved=True)
    assert raised.value.code is ErrorCode.PROFILE_NOT_APPROVED

    with pytest.raises(ProfileValidationError):
        loaded.resolve_side(TargetSide.LEFT)


def test_approved_profile_requires_checksum_and_motion_critical_values() -> None:
    payload = approved_profile_payload()
    payload["calibration"]["expected_checksum"] = None  # type: ignore[index]

    with pytest.raises(ProfileValidationError) as raised:
        load_profile(payload)

    assert raised.value.code is ErrorCode.PROFILE_CHECKSUM_INVALID


def test_unknown_schema_key_is_rejected_for_draft_and_approved_profiles() -> None:
    payload = approved_profile_payload()
    payload["control"]["mystery_gain"] = 1.0  # type: ignore[index]
    payload["calibration"][  # type: ignore[index]
        "expected_checksum"
    ] = compute_profile_checksum(
        payload
    )

    with pytest.raises(ProfileValidationError) as raised:
        load_profile(payload)

    assert raised.value.code is ErrorCode.PROFILE_SCHEMA_INVALID
    assert raised.value.field == "control"


def test_non_null_draft_values_are_still_strictly_validated() -> None:
    payload = {
        "schema_version": 1,
        "profile": {
            "name": "draft",
            "version": "draft-1",
            "procedure_type": "synthetic",
        },
        "calibration": {"approved": False},
        "limits": {"single_jog_mm": "unknown", "cumulative_jog_mm": None},
    }

    with pytest.raises(ProfileValidationError) as raised:
        load_profile(payload)

    assert raised.value.code is ErrorCode.PROFILE_SCHEMA_INVALID
    assert raised.value.field == "limits.single_jog_mm"


def test_data_directory_cannot_be_relative_or_filesystem_root() -> None:
    for unsafe_path in ("relative/output", "/"):
        payload = approved_profile_payload()
        payload["data"]["directory"] = unsafe_path  # type: ignore[index]
        payload["calibration"][  # type: ignore[index]
            "expected_checksum"
        ] = compute_profile_checksum(payload)

        with pytest.raises(ProfileValidationError) as raised:
            load_profile(payload)

        assert raised.value.code is ErrorCode.PROFILE_SCHEMA_INVALID
        assert raised.value.field == "data.directory"


def test_left_and_right_cannot_silently_alias_same_physical_arm() -> None:
    payload = approved_profile_payload()
    payload["side_mapping"]["RIGHT"][  # type: ignore[index]
        "arm_id"
    ] = "arm-left"
    payload["calibration"][  # type: ignore[index]
        "expected_checksum"
    ] = compute_profile_checksum(
        payload
    )

    with pytest.raises(ProfileValidationError) as raised:
        load_profile(payload)

    assert raised.value.code is ErrorCode.PROFILE_SCHEMA_INVALID
    assert raised.value.field == "side_mapping"


def test_left_and_right_cannot_silently_alias_same_force_sensor() -> None:
    payload = approved_profile_payload()
    payload["side_mapping"]["RIGHT"][  # type: ignore[index]
        "sensor_id"
    ] = 11
    payload["calibration"][  # type: ignore[index]
        "expected_checksum"
    ] = compute_profile_checksum(payload)

    with pytest.raises(ProfileValidationError) as raised:
        load_profile(payload)

    assert raised.value.code is ErrorCode.PROFILE_SCHEMA_INVALID
    assert raised.value.field == "side_mapping"


def test_profile_checksum_is_independent_of_mapping_insertion_order() -> None:
    payload = approved_profile_payload()
    reordered = {key: deepcopy(payload[key]) for key in reversed(tuple(payload))}

    assert compute_profile_checksum(reordered) == compute_profile_checksum(payload)
