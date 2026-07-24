from __future__ import annotations

import pytest

from procedure_spec import BedRobotArmGroupNormalizationError
from vlm_node.node import MockVLMNode


@pytest.mark.parametrize(
    ("voice_text", "direction", "distance_mm", "origin"),
    [
        ("좌우로 5 cm 당겨줘", "LEFT_RIGHT", 50.0, "explicit_with_unit"),
        ("상하로 당겨줘", "UP_DOWN", 10.0, "defaulted"),
        ("위로 중간 정도 당겨줘", "UP", 15.0, "qualitative_inferred"),
        ("아래로 약간 당겨줘", "DOWN", 3.0, "qualitative_inferred"),
    ],
)
def test_mock_vlm_group_normalization(
    voice_text: str,
    direction: str,
    distance_mm: float,
    origin: str,
) -> None:
    normalized = MockVLMNode._normalize_mock_group_request(voice_text)
    assert normalized.direction == direction
    assert normalized.distance_mm == distance_mm
    assert normalized.distance_origin == origin


def test_mock_vlm_refuses_request_without_direction_evidence() -> None:
    with pytest.raises(BedRobotArmGroupNormalizationError, match="direction"):
        MockVLMNode._normalize_mock_group_request("조금 당겨줘")
