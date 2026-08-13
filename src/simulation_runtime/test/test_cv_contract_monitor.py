from types import SimpleNamespace

import pytest

from simulation_runtime.cv_contract import normalize_perception_backend
from simulation_runtime.cv_contract_monitor import (
    qos_contract_matches,
    qos_contract_state,
    validate_camera_info,
    validate_compressed_image,
    validate_depth_image,
)


def _header(*, sec: int = 10, nanosec: int = 0, frame_id: str = "cam4"):
    return SimpleNamespace(
        stamp=SimpleNamespace(sec=sec, nanosec=nanosec), frame_id=frame_id
    )


def test_normalize_perception_backend_is_explicit() -> None:
    assert normalize_perception_backend(" External ") == "external"
    assert normalize_perception_backend("LOCAL") == "local"
    with pytest.raises(ValueError, match="PERCEPTION_BACKEND"):
        normalize_perception_backend("automatic")


def test_qos_contract_checker_requires_exact_external_profile() -> None:
    expected = "RELIABLE/TRANSIENT_LOCAL/KEEP_LAST(1)"
    assert qos_contract_matches(
        expected,
        {"reliability": 1, "durability": 1, "depth": 1},
    )
    assert not qos_contract_matches(
        expected,
        {"reliability": 1, "durability": 2, "depth": 10},
    )
    assert qos_contract_state(
        expected,
        {"reliability": 1, "durability": 1, "depth": 0},
    ) == "UNVERIFIABLE_DEPTH"


def test_compressed_image_requires_payload_stamp_and_frame() -> None:
    valid, errors, details = validate_compressed_image(
        SimpleNamespace(header=_header(), format="jpeg", data=b"123")
    )
    assert valid
    assert errors == []
    assert details["payload_bytes"] == 3

    valid, errors, _ = validate_compressed_image(
        SimpleNamespace(header=_header(sec=0, frame_id=""), format="", data=b"")
    )
    assert not valid
    assert set(errors) == {
        "empty_payload",
        "missing_source_stamp",
        "missing_frame_id",
    }


def test_camera_info_checks_matrix_shape_without_claiming_calibration_validity() -> None:
    message = SimpleNamespace(
        header=_header(),
        width=1280,
        height=720,
        k=[1.0] * 9,
        r=[1.0] * 9,
        p=[1.0] * 12,
    )
    valid, errors, details = validate_camera_info(message)
    assert valid
    assert errors == []
    assert details["calibration_policy"] == "PENDING_EXTERNAL_CALIBRATION_VERSION"

    message.p = [float("nan")] * 12
    valid, errors, _ = validate_camera_info(message)
    assert not valid
    assert "invalid_p_matrix" in errors


def test_depth_image_is_structural_only_until_unit_and_sync_contract_arrives() -> None:
    message = SimpleNamespace(
        header=_header(),
        width=4,
        height=2,
        step=8,
        encoding="16UC1",
        data=b"x" * 16,
    )
    valid, errors, details = validate_depth_image(message)
    assert valid
    assert errors == []
    assert details["depth_units_policy"] == "PENDING_EXTERNAL_CONTRACT"

    message.data = b"x" * 15
    valid, errors, _ = validate_depth_image(message)
    assert not valid
    assert "payload_shorter_than_step_times_height" in errors
