from __future__ import annotations

from types import SimpleNamespace

import pytest

from vlm_node.real_vlm import RealVLMNode


def _request(
    voice_text: str,
    *,
    adjustment_mode: str = "single",
    target_retractor_id: str = "left_malleable",
):
    return SimpleNamespace(
        request_id="req-123",
        voice_text=voice_text,
        adjustment_mode=adjustment_mode,
        target_retractor_id=target_retractor_id,
        direction_frame="surgeon_view",
        end_effector_profile="malleable",
    )


def _single_proposal(**overrides):
    payload = {
        "request_id": "req-123",
        "group_id": "retraction",
        "operation": "retraction",
        "adjustment_mode": "single",
        "target_retractor_id": "left_malleable",
        "direction_frame": "surgeon_view",
        "direction": "left",
        "axis": "none",
        "distance_mm": 10.0,
        "distance_origin": "explicit_with_unit",
        "raw_distance_text": "10 mm",
        "end_effector_profile": "malleable",
        "rationale": "visible fine-adjustment direction",
        "confidence": 0.9,
    }
    payload.update(overrides)
    return payload


def _multi_proposal(**overrides):
    payload = {
        "request_id": "req-123",
        "group_id": "retraction",
        "operation": "retraction",
        "adjustment_mode": "multi",
        "target_retractor_id": "both_malleable",
        "direction_frame": "surgeon_view",
        "direction": "none",
        "axis": "left_right",
        "distance_mm": 10.0,
        "distance_origin": "explicit_with_unit",
        "raw_distance_text": "10 mm",
        "end_effector_profile": "malleable",
        "rationale": "bilateral fine adjustment",
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


def test_single_accepts_explicit_thirty_millimeter_boundary() -> None:
    error = _validate(
        _single_proposal(
            distance_mm=30.0,
            distance_origin="explicit_with_unit",
            raw_distance_text="3 cm",
        ),
        _request("왼쪽 말레어블을 왼쪽으로 3 cm 당겨줘"),
    )
    assert error == ""


def test_single_rejects_explicit_distance_above_limit_without_clamping() -> None:
    error = _validate(
        _single_proposal(
            distance_mm=50.0,
            distance_origin="explicit_with_unit",
            raw_distance_text="5 cm",
        ),
        _request("왼쪽 말레어블을 왼쪽으로 5 cm 당겨줘"),
    )
    assert "30 mm contract limit" in error


def test_single_direction_cannot_be_overridden() -> None:
    error = _validate(
        _single_proposal(direction="right"),
        _request("왼쪽 말레어블을 왼쪽으로 10 mm 당겨줘"),
    )
    assert "expected LEFT" in error


def test_multi_axis_follows_execute_retraction_adjustment_contract() -> None:
    error = _validate(
        _multi_proposal(),
        _request(
            "좌우로 10 mm 당겨줘",
            adjustment_mode="multi",
            target_retractor_id="both_malleable",
        ),
    )
    assert error == ""


def test_proposal_cannot_change_adjustment_target() -> None:
    error = _validate(
        _single_proposal(target_retractor_id="right_malleable"),
        _request("왼쪽 말레어블을 왼쪽으로 10 mm 당겨줘"),
    )
    assert "target_retractor_id" in error


def test_direction_frame_must_match_request() -> None:
    error = _validate(
        _single_proposal(direction_frame="robot_base"),
        _request("왼쪽 말레어블을 왼쪽으로 10 mm 당겨줘"),
    )
    assert "surgeon_view" in error


@pytest.mark.parametrize(
    ("voice_text", "raw_distance_text", "distance_origin"),
    [
        ("왼쪽 말레어블을 위로 조금 당겨줘", "조금", "qualitative_inferred"),
        ("왼쪽 말레어블을 위로 당겨줘", "", "defaulted"),
        ("왼쪽 말레어블을 위로 10 당겨줘", "10", "explicit_with_unit"),
    ],
)
def test_non_explicit_distance_is_rejected(
    voice_text: str,
    raw_distance_text: str,
    distance_origin: str,
) -> None:
    error = _validate(
        _single_proposal(
            direction="up",
            distance_mm=10.0,
            distance_origin=distance_origin,
            raw_distance_text=raw_distance_text,
        ),
        _request(voice_text),
    )
    assert "explicit numeric mm/cm" in error
