from __future__ import annotations

from types import SimpleNamespace

from vlm_node.real_vlm import RealVLMNode


def _request(voice_text: str):
    return SimpleNamespace(
        request_id="req-123",
        voice_text=voice_text,
        end_effector_profile="army",
    )


def _proposal(**overrides):
    payload = {
        "request_id": "req-123",
        "group_id": "retraction",
        "operation": "retraction",
        "direction": "LEFT_RIGHT",
        "distance_mm": 10.0,
        "distance_origin": "qualitative_inferred",
        "raw_distance_text": "조금",
        "end_effector_profile": "army",
        "rationale": "exposure",
        "confidence": 0.9,
    }
    payload.update(overrides)
    return payload


def _validate(proposal, request) -> str:
    return RealVLMNode._validate_bed_robot_arm_group_proposal(
        object(),
        proposal,
        request,
    )


def test_explicit_five_centimeters_is_not_clamped() -> None:
    error = _validate(
        _proposal(
            direction="LEFT",
            distance_mm=50.0,
            distance_origin="explicit_with_unit",
            raw_distance_text="5 cm",
        ),
        _request("왼쪽으로 5 cm 당겨줘"),
    )
    assert error == ""


def test_explicit_conversion_mismatch_is_rejected() -> None:
    error = _validate(
        _proposal(
            direction="LEFT",
            distance_mm=30.0,
            distance_origin="explicit_with_unit",
            raw_distance_text="5 cm",
        ),
        _request("왼쪽으로 5 cm 당겨줘"),
    )
    assert "expected 50 mm" in error


def test_spoken_direction_cannot_be_overridden() -> None:
    error = _validate(
        _proposal(direction="RIGHT"),
        _request("왼쪽으로 조금 당겨줘"),
    )
    assert "expected LEFT" in error


def test_missing_distance_defaults_to_ten_millimeters() -> None:
    error = _validate(
        _proposal(
            direction="UP_DOWN",
            distance_mm=10.0,
            distance_origin="defaulted",
            raw_distance_text="",
        ),
        _request("상하로 당겨줘"),
    )
    assert error == ""


def test_qualitative_value_above_thirty_is_rejected() -> None:
    error = _validate(
        _proposal(
            direction="UP",
            distance_mm=31.0,
            distance_origin="qualitative_inferred",
            raw_distance_text="중간 정도",
        ),
        _request("위로 중간 정도 당겨줘"),
    )
    assert "between 1 and 30" in error


def test_qualitative_value_without_spoken_intensity_is_rejected() -> None:
    error = _validate(
        _proposal(
            direction="LEFT",
            distance_mm=15.0,
            distance_origin="qualitative_inferred",
            raw_distance_text="당겨줘",
        ),
        _request("왼쪽으로 당겨줘"),
    )
    assert "intensity expression" in error
