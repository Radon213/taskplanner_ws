from __future__ import annotations

from types import SimpleNamespace

import pytest

from procedure_spec import BedRobotArmGroupNormalizationError
from vlm_node.node import MockVLMNode


@pytest.mark.parametrize(
    ("voice_text", "direction", "distance_mm", "origin"),
    [
        ("좌우로 3 cm 당겨줘", "LEFT_RIGHT", 30.0, "explicit_with_unit"),
        ("상하로 10 mm 당겨줘", "UP_DOWN", 10.0, "explicit_with_unit"),
        ("위로 1.5 cm 당겨줘", "UP", 15.0, "explicit_with_unit"),
        ("아래로 3 mm 당겨줘", "DOWN", 3.0, "explicit_with_unit"),
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


@pytest.mark.parametrize(
    ("voice_text", "error"),
    [
        ("좌우로 5 cm 당겨줘", "30 mm contract limit"),
        ("상하로 당겨줘", "explicit numeric mm/cm"),
        ("위로 중간 정도 당겨줘", "explicit numeric mm/cm"),
        ("아래로 약간 당겨줘", "explicit numeric mm/cm"),
    ],
)
def test_mock_vlm_refuses_unsafe_or_inferred_distance(
    voice_text: str,
    error: str,
) -> None:
    with pytest.raises(BedRobotArmGroupNormalizationError, match=error):
        MockVLMNode._normalize_mock_group_request(voice_text)


def test_mock_vlm_reset_is_repeatable_and_reopens_the_next_start_edge() -> None:
    node = MockVLMNode.__new__(MockVLMNode)
    node._last_lifecycle_control_command = ""
    node._active = False
    node._state_activation_enabled = False
    node._tick = 0
    node._state_backed_observations_enabled = lambda: False
    node._perception_scene_observations_enabled = lambda: False
    publish_calls: list[bool] = []
    node._publish = lambda: publish_calls.append(True)
    node._latest_state = object()
    node._latest_scene = object()
    node._latest_outward_signal = object()
    node._tool_location_history = {"T01": ["surgeon"]}
    node._state_phase_id = "P02"
    node._state_phase_ticks = 3
    node._state_stage_index = 2
    node._state_stage_ticks = 4
    node._delivered_by_phase = {"P02": {"T01"}}
    node._completion_request_emitted = True
    node._completion_confirm_emitted = True
    node._seen_bed_group_request_ids = {"request-1"}

    node._on_control(SimpleNamespace(data="start"))
    node._on_control(SimpleNamespace(data="start"))
    node._on_control(SimpleNamespace(data="reset"))
    node._on_control(SimpleNamespace(data="reset"))
    node._on_control(SimpleNamespace(data="start"))

    assert publish_calls == [True, True]
    assert node._active is True
    assert node._last_lifecycle_control_command == "start"
