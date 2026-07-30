from __future__ import annotations

from pathlib import Path

import pytest
from builtin_interfaces.msg import Time
from or_digital_twin.node import ORDigitalTwinNode
from or_digital_twin.twin import ORDigitalTwin
from procedure_spec import load_bundle
from surgical_msgs.msg import VLMResult


def _node() -> tuple[ORDigitalTwinNode, list[dict]]:
    spec_dir = (
        Path(__file__).parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
        / "thyroidectomy_demo"
    )
    node = ORDigitalTwinNode.__new__(ORDigitalTwinNode)
    node._twin = ORDigitalTwin(load_bundle(spec_dir))
    node._twin.set_execution_state(True, "running")
    node._vlm_implicit_request_threshold = 0.8
    node._vlm_implicit_request_stability_sec = 0.7
    node._vlm_implicit_request_release_sec = 1.5
    node._vlm_implicit_request_stability = {}
    node._vlm_implicit_request_episode_tool = ""
    node._vlm_implicit_request_release_since = None
    node._tool_predict_stability = {}
    decisions: list[dict] = []
    node._publish_reducer_decision_event = lambda **kwargs: decisions.append(kwargs)
    node._publish_event = lambda *_args, **_kwargs: None
    node._publish_world_state = lambda: None
    node._stamp = lambda: Time()
    return node, decisions


def _result(
    *,
    tool_id: str = "T02",
    confidence: float = 0.86,
    visible: bool = True,
) -> VLMResult:
    msg = VLMResult()
    msg.source = "real_vlm:test"
    msg.schema_version = "4"
    if visible:
        msg.gesture_event_type = "request_tool"
        msg.gesture_requested_tool = tool_id
        msg.gesture_hand_pose = "open_receive"
        msg.gesture_confidence = confidence
    return msg


def test_visual_request_is_exposed_as_evidence_without_creating_request() -> None:
    node, decisions = _node()
    payload = {
        "v": "4",
        "tool": [["T02", 0.91]],
    }
    msg = _result()
    node._twin.state.predicted_tool = "T02"

    node._handle_vlm_implicit_request(payload, msg, 10.0)
    assert node._twin.request_queue_summary()["queue_length"] == 0

    node._handle_vlm_implicit_request(payload, msg, 10.8)
    assert node._twin.request_queue_summary()["queue_length"] == 0
    assert node._twin.state.implicit_request_visible is True
    assert node._twin.state.implicit_request_tool == "T02"
    assert node._twin.state.implicit_request_hand_pose == "open_receive"
    assert node._twin.state.implicit_request_stability_sec == pytest.approx(0.8)
    assert all(bool(item["accepted"]) for item in decisions)
    assert decisions[-1]["reason"] == "verified_visual_open_palm_evidence"
    assert decisions[-1]["detail"]["policy_ready"] is True
    assert decisions[-1]["detail"]["request_created"] is False


def test_visual_evidence_does_not_depend_on_same_response_next_tool() -> None:
    node, decisions = _node()
    msg = _result()
    payload = {
        "v": "4",
        "tool": [["T04", 0.92]],
    }

    node._handle_vlm_implicit_request(payload, msg, 10.0)
    node._handle_vlm_implicit_request(payload, msg, 10.8)

    assert node._twin.request_queue_summary()["queue_length"] == 0
    assert node._twin.state.implicit_request_visible is True
    assert node._twin.state.implicit_request_tool == "T02"
    assert decisions[-1]["reason"] == "verified_visual_open_palm_evidence"
    assert decisions[-1]["detail"]["request_created"] is False


def test_visual_request_pose_is_retained_when_tool_is_unresolved() -> None:
    node, decisions = _node()
    msg = _result(tool_id="", confidence=0.72)

    node._handle_vlm_implicit_request({"v": "4", "tool": []}, msg, 10.0)

    assert node._twin.state.implicit_request_visible is True
    assert node._twin.state.implicit_request_tool == ""
    assert node._twin.state.implicit_request_hand_pose == "open_receive"
    assert decisions[-1]["reason"] == "verified_visual_open_palm_evidence"
    assert decisions[-1]["detail"]["tool_resolved"] is False
    assert decisions[-1]["detail"]["policy_ready"] is False


def test_handover_intent_without_visual_gesture_does_not_queue_request() -> None:
    node, decisions = _node()
    msg = _result(visible=False)
    payload = {
        "v": "4",
        "tool": [["T02", 0.91]],
        "intent": ["handover", "T02", 0.95],
    }

    node._handle_vlm_implicit_request(payload, msg, 10.0)
    node._handle_vlm_implicit_request(payload, msg, 11.0)

    assert node._twin.request_queue_summary()["queue_length"] == 0
    assert decisions == []
