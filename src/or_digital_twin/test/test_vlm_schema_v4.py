from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from or_digital_twin.node import ORDigitalTwinNode
from procedure_spec import load_bundle
from surgical_msgs.msg import VLMResult


def _prepare_result_handler(node: ORDigitalTwinNode) -> None:
    node._handle_vlm_implicit_request = lambda *_args: None
    node._mayo_retrieve_stability = {}
    node._mayo_reuse_stability = {}
    node._publish_world_state = lambda: None


def test_v4_result_reaches_tool_prediction_reducer() -> None:
    spec_dir = (
        Path(__file__).parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
        / "thyroidectomy_demo"
    )
    node = ORDigitalTwinNode.__new__(ORDigitalTwinNode)
    node._twin = SimpleNamespace(
        spec=load_bundle(spec_dir),
        state=SimpleNamespace(predicted_tool=""),
    )
    node._stamp_sec = lambda _stamp: 12.0
    _prepare_result_handler(node)
    observed: dict[str, object] = {}
    node._handle_vlm_tool_prediction = (
        lambda payload, msg, now_sec: observed.update(
            payload=payload,
            schema_version=msg.schema_version,
            now_sec=now_sec,
        )
    )

    msg = VLMResult()
    msg.schema_version = "4"
    msg.raw_json = json.dumps(
        {
            "v": "4",
            "phase": [["P03", 0.9]],
            "tool": [["T02", 0.9]],
            "intent": ["none", "", 0.0],
            "mayo": [],
            "mayo_retrieve": ["", 0.0],
            "bed_robot_arm_group": None,
        }
    )
    msg.phase_ids = ["P03"]
    msg.phase_confidences = [0.9]

    node._on_vlm_result(msg)

    assert observed["schema_version"] == "4"
    assert observed["now_sec"] == 12.0
    assert observed["payload"]["tool"] == [["T02", 0.9]]


def test_v4_ranked_tool_rows_are_preserved_for_fusion() -> None:
    node = ORDigitalTwinNode.__new__(ORDigitalTwinNode)

    assert node._vlm_tool_rows(
        {
            "v": "4",
            "tool": [["T02", 0.91], ["T04", 0.63]],
        }
    ) == [["T02", 0.91], ["T04", 0.63]]


def test_vlm_result_is_accepted_while_optional_perception_is_disabled() -> None:
    spec_dir = (
        Path(__file__).parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
        / "thyroidectomy_demo"
    )
    node = ORDigitalTwinNode.__new__(ORDigitalTwinNode)
    node._vlm_mode = "real"
    node._perception_health_seen = True
    node._perception_enabled = False
    node._twin = SimpleNamespace(
        spec=load_bundle(spec_dir),
        state=SimpleNamespace(predicted_tool=""),
    )
    node._stamp_sec = lambda _stamp: 12.0
    _prepare_result_handler(node)
    observed: list[dict] = []
    node._handle_vlm_tool_prediction = (
        lambda payload, _msg, _now_sec: observed.append(payload)
    )

    msg = VLMResult()
    msg.schema_version = "4"
    msg.raw_json = json.dumps(
        {
            "v": "4",
            "phase": [["P03", 0.9]],
            "tool": [["T02", 0.9]],
            "intent": ["none", "", 0.0],
            "mayo": [],
            "mayo_retrieve": ["", 0.0],
            "bed_robot_arm_group": None,
        }
    )

    node._on_vlm_result(msg)

    assert len(observed) == 1
    assert observed[0]["tool"] == [["T02", 0.9]]
