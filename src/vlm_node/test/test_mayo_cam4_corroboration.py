from __future__ import annotations

from collections import deque
import json
from pathlib import Path
from types import SimpleNamespace

from builtin_interfaces.msg import Time
import pytest
from procedure_spec import load_bundle
from vlm_node.real_vlm import RealVLMNode


def _node() -> RealVLMNode:
    spec_dir = (
        Path(__file__).parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
        / "thyroidectomy"
    )
    node = RealVLMNode.__new__(RealVLMNode)
    node._spec = load_bundle(spec_dir)
    node._perception_image_max_skew_sec = 0.2
    return node


def _demo_node() -> RealVLMNode:
    spec_dir = (
        Path(__file__).parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
        / "thyroidectomy_demo"
    )
    node = RealVLMNode.__new__(RealVLMNode)
    node._spec = load_bundle(spec_dir)
    node._perception_image_max_skew_sec = 0.2
    return node


class _Publisher:
    def __init__(self) -> None:
        self.messages = []

    def publish(self, message) -> None:
        self.messages.append(message)


def _cam4_summary(
    stamp_sec: float,
    *,
    request_state: str = "none",
) -> dict[str, object]:
    return {
        "schema": "taskplanner.cam4_semantics.v1",
        "source": "cam4_rfdetr_small",
        "source_stamp_sec": stamp_sec,
        "tools": [
            {
                "name": "Bovie surgical cautery",
                "count": 1,
                "max_confidence": 0.9,
            }
        ],
        "tool_request": {"state": request_state},
    }


def test_fast_cam4_path_publishes_once_after_non_request_stability() -> None:
    node = _node()
    node._active = True
    node._perception_enabled = True
    node._latest_perception = {}
    node._perception_buffers = {"cam4_semantics": deque(maxlen=64)}
    node._fast_cam4_mayo_published_tools = set()
    node._fast_cam4_mayo_last_seen_stamp_sec = {}
    node._tool_pub = _Publisher()

    for stamp_sec in (44.5, 44.65, 44.8):
        summary = _cam4_summary(stamp_sec)
        node._latest_perception["cam4_semantics"] = (
            stamp_sec,
            summary,
        )
        node._perception_buffers["cam4_semantics"].append(
            (stamp_sec, summary)
        )
        node._publish_fast_cam4_mayo_observations(summary)

    assert len(node._tool_pub.messages) == 1
    observation = node._tool_pub.messages[0]
    assert observation.instrument_id == "T04"
    assert observation.location_type == "mayo_stand"
    assert observation.stamp.sec == 44
    assert observation.stamp.nanosec == 800_000_000

    node._publish_fast_cam4_mayo_observations(
        _cam4_summary(44.9)
    )
    assert len(node._tool_pub.messages) == 1


def test_fast_cam4_path_suppresses_active_hand_request() -> None:
    node = _node()
    node._active = True
    node._perception_enabled = True
    node._latest_perception = {}
    node._perception_buffers = {"cam4_semantics": deque(maxlen=64)}
    node._fast_cam4_mayo_published_tools = set()
    node._fast_cam4_mayo_last_seen_stamp_sec = {}
    node._tool_pub = _Publisher()

    for stamp_sec in (42.2, 42.4, 42.6, 42.8):
        summary = _cam4_summary(
            stamp_sec,
            request_state="request",
        )
        node._perception_buffers["cam4_semantics"].append(
            (stamp_sec, summary)
        )
        node._publish_fast_cam4_mayo_observations(summary)

    assert node._tool_pub.messages == []


def test_real_vlm_cam4_callback_only_buffers_public_semantics() -> None:
    node = _node()
    node._latest_perception = {}
    node._perception_buffers = {}
    node._causal_now_sec = lambda: 44.6
    node._publish_fast_cam4_mayo_observations = lambda summary: pytest.fail(
        "Mayo placement publishing must stay outside the VLM executor"
    )

    callback = node._make_perception_cb("cam4_semantics")
    summary = _cam4_summary(44.5)
    summary["tools"][0]["mean_confidence"] = 0.88
    callback(SimpleNamespace(data=json.dumps(summary)))

    assert node._latest_perception["cam4_semantics"][1][
        "source_stamp_sec"
    ] == 44.5
    assert len(node._perception_buffers["cam4_semantics"]) == 1


def _aligned_context(
    tools: list[dict[str, object]],
    *,
    tool_request_state: str = "none",
    digital_twin_tools: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "digital_twin": {
            "hands": {},
            "tools": digital_twin_tools or [],
        },
        "observable_perception": {
            "schema": "taskplanner.cam4_semantics.v1",
            "source": "cam4_rfdetr_small",
            "alignment": {
                "status": "aligned",
                "detector_stamp_sec": 44.08,
                "offset_sec": 0.03,
            },
            "tools": tools,
            "tool_request": {
                "state": tool_request_state,
            },
        }
    }


def test_aligned_cam4_intersects_mayo_rows_and_retrieve_candidate() -> None:
    payload = {
        "mayo": [
            ["T02", "reuse", 0.84],
            ["T04", "recover", 0.91],
        ],
        "mayo_retrieve": ["T04", 0.91],
    }
    context = _aligned_context(
        [
            {
                "name": "Bovie surgical cautery",
                "count": 1,
                "max_confidence": 0.94,
                "mean_confidence": 0.91,
            }
        ]
    )

    _node()._corroborate_mayo_with_cam4_semantics(payload, context)

    assert payload["mayo"] == [["T04", "recover", 0.91]]
    assert payload["mayo_retrieve"] == ["T04", 0.91]


def test_aligned_cam4_detection_count_limits_duplicate_mayo_rows() -> None:
    payload = {
        "mayo": [
            ["T02", "reuse", 0.88],
            ["T02", "recover", 0.71],
        ],
        "mayo_retrieve": ["T02", 0.71],
    }
    context = _aligned_context(
        [
            {
                "name": "Adson forceps",
                "count": 1,
                "max_confidence": 0.92,
                "mean_confidence": 0.92,
            }
        ]
    )

    _node()._corroborate_mayo_with_cam4_semantics(payload, context)

    assert payload["mayo"] == [["T02", "reuse", 0.88]]
    assert payload["mayo_retrieve"] == ["", 0.0]


@pytest.mark.parametrize(
    "perception",
    [
        {},
        {
            "schema": "taskplanner.cam4_semantics.v1",
            "source": "cam4_rfdetr_small",
            "alignment": {"status": "missing"},
            "tools": [
                {"name": "Bovie surgical cautery", "count": 1},
            ],
        },
        {
            "schema": "taskplanner.cam4_semantics.v1",
            "source": "cam4_rfdetr_small",
            "alignment": {
                "status": "omitted_source_timestamp_misaligned",
            },
            "tools": [
                {"name": "Bovie surgical cautery", "count": 1},
            ],
        },
    ],
)
def test_missing_or_unaligned_cam4_clears_mayo_claims(
    perception: dict[str, object],
) -> None:
    payload = {
        "mayo": [["T04", "recover", 0.91]],
        "mayo_retrieve": ["T04", 0.91],
    }

    _node()._corroborate_mayo_with_cam4_semantics(
        payload,
        {"observable_perception": perception},
    )

    assert payload["mayo"] == []
    assert payload["mayo_retrieve"] == ["", 0.0]


def test_raw_cam4_pixels_preserve_mayo_claims_without_detector_rows() -> None:
    payload = {
        "mayo": [["T04", "recover", 0.81]],
        "mayo_retrieve": ["T04", 0.81],
    }
    context = {
        "visual_input": {"cam4_image_forwarded_to_vlm": True},
        "observable_perception": {
            "source": "cam4_rfdetr_small",
            "alignment": {"status": "missing"},
        },
    }

    _node()._corroborate_mayo_with_cam4_semantics(payload, context)

    assert payload["mayo"] == [["T04", "recover", 0.81]]
    assert payload["mayo_retrieve"] == ["T04", 0.81]


def test_raw_cam4_visual_request_survives_stabilization_without_detector() -> None:
    payload = {
        "v": "4",
        "phase": [["P02", 0.82]],
        "tool": [["T02", 0.77]],
        "intent": ["handover", "T02", 0.74],
        "gesture": ["request_tool", "T02", "open_receive", 0.74],
        "mayo": [],
        "mayo_retrieve": ["", 0.0],
        "u": 0.26,
        "sum": "An open palm is extended toward the assistant.",
        "bed_robot_arm_group": None,
    }
    context = {
        "phase_search_mode": "temporal_prior",
        "evidence_window": {
            "speech": [],
            "observed_signals": [],
        },
        "visual_input": {
            "image_source": "flir_raw_fallback",
            "cam4_image_forwarded_to_vlm": True,
            "detector_advisory": False,
        },
        "observable_perception": {
            "source": "cam4_rfdetr_small",
            "alignment": {"status": "missing"},
        },
        "candidates": {
            "phase": [["P02", 0.9]],
            "tool": [["T02", 0.68]],
            "evidence": {
                "current_phase": "P02",
                "allowed_next": ["P03"],
                "phase_search_mode": "temporal_prior",
            },
        },
        "digital_twin": {"hands": {}, "tools": []},
    }

    stabilized = _node()._stabilize_actor_log_payload(payload, context)

    assert stabilized["intent"] == ["handover", "T02", 0.74]
    assert stabilized["gesture"] == [
        "request_tool",
        "T02",
        "open_receive",
        0.74,
    ]


def test_raw_cam4_request_pose_survives_before_tool_is_identified() -> None:
    payload = {
        "v": "4",
        "phase": [["P02", 0.82]],
        "tool": [["T02", 0.51]],
        "intent": ["none", "", 0.0],
        "gesture": ["request_tool", "", "open_receive", 0.62],
        "mayo": [["T04", "reuse", 0.68]],
        "mayo_retrieve": ["", 0.0],
        "u": 0.38,
        "sum": "An empty open palm is extended while a cautery rests on Mayo.",
        "bed_robot_arm_group": None,
    }
    context = {
        "phase_search_mode": "temporal_prior",
        "evidence_window": {
            "speech": [],
            "observed_signals": [],
        },
        "visual_input": {
            "image_source": "flir_raw_fallback",
            "cam4_image_forwarded_to_vlm": True,
            "detector_advisory": False,
        },
        "observable_perception": {
            "source": "cam4_rfdetr_small",
            "alignment": {"status": "missing"},
        },
        "candidates": {
            "phase": [["P02", 0.9]],
            "tool": [["T02", 0.51]],
            "evidence": {
                "current_phase": "P02",
                "allowed_next": ["P03"],
                "phase_search_mode": "temporal_prior",
            },
        },
        "digital_twin": {"hands": {}, "tools": []},
    }

    stabilized = _node()._stabilize_actor_log_payload(payload, context)

    assert stabilized["gesture"] == [
        "request_tool",
        "",
        "open_receive",
        0.62,
    ]
    assert stabilized["intent"] == ["none", "", 0.0]
    assert stabilized["mayo"] == [["T04", "reuse", 0.68]]


def test_detector_independent_prompt_separates_pose_and_tool_identity() -> None:
    instruction = RealVLMNode.__new__(
        RealVLMNode
    )._actor_log_developer_instruction()

    assert "Mandatory CAM4 visual pass" in instruction
    assert "Tool identity is a separate question" in instruction
    assert "empty tool_id" in instruction
    assert "tool-unknown gesture" in instruction
    assert 'intent must be exactly ["none","",0.0]' in instruction
    assert "request_tool is a gesture event, not an intent value" in instruction
    assert "visually compare all visible hands" in instruction
    assert "inspect each independently" in instruction
    assert "Ignore bare patient hands" in instruction
    assert "Use the supplied CAM4 crop as-is" in instruction
    assert "center-right interior request zone" in instruction
    assert "covered-patient region to the right of the blue Mayo work surface" in instruction
    assert "search prior only" in instruction
    assert "one flat four-item array" in instruction
    assert "Trace each candidate wrist to its source" in instruction
    assert "elastic cuff into a blue or green sterile gown sleeve" in instruction
    assert "common positive appearance is that skin-toned staff glove" in instruction
    assert "do not require spread fingers" in instruction
    assert "sterile drape" in instruction
    assert "broad complete palmar surface and several relaxed uncurled fingers" in instruction
    assert "palm/thenar-pad and finger-pad geometry" in instruction
    assert "Medical gloves may be beige, pink, white, or blue" in instruction
    assert "dorsal-only hand" in instruction
    assert "Mayo placement/pickup requires visible hand contact or grip on a tool" in instruction
    assert "Use 0.45-0.79 only when the complete empty palm" in instruction
    assert "wearer role, wrist orientation, or cuff visibility remains uncertain" in instruction
    assert "If the palm, several fingers, or open-versus-gripping state is not directly visible" in instruction
    assert "Mere contact with the body is not stabilization" in instruction
    assert "visible pressure, bracing, tissue traction, or active manipulation" in instruction
    assert "a palm or multiple fingers clipped at the bottom or side frame edge" in instruction
    assert "fingertips or knuckles without a complete visible palm" in instruction
    assert "A nearby tool or cable is not placement/pickup" in instruction
    assert "require visible hand contact or grip" in instruction
    assert "Never copy a tool from Mayo" in instruction
    assert "An open palm alone never names a tool" in instruction
    assert "a nearby or Mayo tool is not a directed cue" in instruction
    assert "Final CAM4 audit immediately before JSON" in instruction
    assert "inspect only CAM4 for gesture and Mayo" in instruction
    assert "expected request appearance" in instruction
    assert "Mandatory positive rule" in instruction
    assert "gesture MUST be request_tool" in instruction
    assert "stationary, touching the body, or another hand is operating" in instruction
    assert "One positive candidate overrides unrelated negative hands" in instruction
    assert "With no named request" in instruction
    assert "No speech or transfer is required" in instruction
    assert "Detector absence must not erase" in instruction


def test_detector_independent_prompt_requires_full_mayo_inventory() -> None:
    instruction = RealVLMNode.__new__(
        RealVLMNode
    )._actor_log_developer_instruction()

    assert "sweep the fixed stand left-to-right and top-to-bottom" in instruction
    assert "one mayo row per visible instance" in instruction
    assert "Identify by morphology before procedure likelihood" in instruction
    assert "thumb forceps have no finger rings" in instruction
    assert "a ring-handled tool can never be Adson" in instruction
    assert (
        "A small slim ring-handled clamp with long narrow straight jaws "
        "is a Mosquito hemostat"
    ) in instruction
    assert "Allis is bulkier and requires directly visible short broad toothed grasping jaws" in instruction
    assert "ring handles alone are insufficient for Allis" in instruction
    assert "prefer Mosquito at lower confidence over inventing Allis" in instruction
    assert "freeze this geometry-based inventory" in instruction
    assert "object recognition is disabled" in instruction
    assert "Every mayo row requires a distinct tool silhouette directly visible in CAM4" in instruction
    assert "Never copy a tool from FLIR" in instruction
    assert "perceptual evidence only" in instruction


def test_cam4_observations_precede_procedure_priors_in_prompt() -> None:
    instruction = RealVLMNode.__new__(
        RealVLMNode
    )._actor_log_developer_instruction()

    assert "Populate gesture and mayo from CAM4 pixels first" in instruction
    assert "must not overwrite a directly visible CAM4 observation" in instruction
    assert instruction.index('"gesture"') < instruction.index('"phase"')


def test_handover_intent_does_not_fabricate_visual_gesture() -> None:
    payload = {
        "v": "4",
        "phase": [["P02", 0.82]],
        "tool": [["T02", 0.77]],
        "intent": ["handover", "T02", 0.91],
        "gesture": ["", "", "", 0.0],
        "mayo": [],
        "mayo_retrieve": ["", 0.0],
        "u": 0.26,
        "sum": "No hand gesture is directly visible.",
        "bed_robot_arm_group": None,
    }
    context = {
        "phase_search_mode": "temporal_prior",
        "evidence_window": {
            "speech": [],
            "observed_signals": [],
        },
        "visual_input": {
            "image_source": "flir_raw_fallback",
            "cam4_image_forwarded_to_vlm": True,
            "detector_advisory": False,
        },
        "candidates": {
            "phase": [["P02", 0.9]],
            "tool": [["T02", 0.68]],
            "evidence": {
                "current_phase": "P02",
                "allowed_next": ["P03"],
                "phase_search_mode": "temporal_prior",
            },
        },
        "digital_twin": {"hands": {}, "tools": []},
    }

    stabilized = _node()._stabilize_actor_log_payload(payload, context)

    assert stabilized["gesture"] == ["", "", "", 0.0]
    assert stabilized["intent"] == ["none", "", 0.0]


def test_aligned_cam4_with_no_tools_clears_mayo_claims() -> None:
    payload = {
        "mayo": [["T04", "reuse", 0.87]],
        "mayo_retrieve": ["T04", 0.87],
    }

    _node()._corroborate_mayo_with_cam4_semantics(
        payload,
        _aligned_context([]),
    )

    assert payload["mayo"] == []
    assert payload["mayo_retrieve"] == ["", 0.0]


def test_stable_cam4_detection_adds_fail_closed_reuse_observation() -> None:
    payload = {
        "mayo": [],
        "mayo_retrieve": ["", 0.0],
    }
    context = _aligned_context(
        [
            {
                "name": "Bovie surgical cautery",
                "count": 1,
                "max_confidence": 0.91,
                "mean_confidence": 0.89,
                "stable_sample_count": 4,
                "stable_duration_sec": 0.34,
            }
        ],
        digital_twin_tools=[
            {
                "id": "T04",
                "lc": "surgeon_owned",
                "lt": "surgical_field",
            }
        ],
    )

    _node()._corroborate_mayo_with_cam4_semantics(payload, context)

    assert payload["mayo"] == [["T04", "reuse", 0.91]]
    assert payload["mayo_retrieve"] == ["", 0.0]


@pytest.mark.parametrize(
    ("tool_request_state", "stable_sample_count", "stable_duration_sec"),
    [
        ("request", 4, 0.34),
        ("none", 2, 0.34),
        ("none", 4, 0.2),
    ],
)
def test_cam4_fallback_requires_stable_non_request_evidence(
    tool_request_state: str,
    stable_sample_count: int,
    stable_duration_sec: float,
) -> None:
    payload = {
        "mayo": [],
        "mayo_retrieve": ["", 0.0],
    }
    context = _aligned_context(
        [
            {
                "name": "Bovie surgical cautery",
                "count": 1,
                "max_confidence": 0.91,
                "stable_sample_count": stable_sample_count,
                "stable_duration_sec": stable_duration_sec,
            }
        ],
        tool_request_state=tool_request_state,
    )

    _node()._corroborate_mayo_with_cam4_semantics(payload, context)

    assert payload["mayo"] == []
    assert payload["mayo_retrieve"] == ["", 0.0]


def test_field_deployed_tool_is_not_reclassified_as_mayo() -> None:
    payload = {
        "mayo": [["T05", "recover", 0.93]],
        "mayo_retrieve": ["T05", 0.93],
    }
    context = {
        "digital_twin": {
            "hands": {},
            "tools": [
                {
                    "id": "T05",
                    "lc": "surgeon_owned",
                    "lt": "surgical_field",
                }
            ],
        }
    }

    _node()._suppress_non_mayo_recovery_candidates(payload, context)

    assert payload["mayo"] == []
    assert payload["mayo_retrieve"] == ["", 0.0]


def test_future_procedure_tool_is_kept_on_mayo_for_reuse() -> None:
    payload = {
        "mayo": [
            ["T05", "recover", 0.95],
            ["T01", "recover", 0.83],
        ],
        "mayo_retrieve": ["T05", 0.95],
    }
    context = {
        "candidates": {
            "evidence": {
                "current_phase": "P03",
            }
        },
        "digital_twin": {
            "hands": {},
            "tools": [],
        },
        "visual_input": {
            "cam4_image_forwarded_to_vlm": True,
        },
    }

    _demo_node()._suppress_non_mayo_recovery_candidates(payload, context)

    assert payload["mayo"] == [
        ["T05", "reuse", 0.95],
        ["T01", "recover", 0.83],
    ]
    assert payload["mayo_retrieve"] == ["", 0.0]


def test_actor_log_stabilization_applies_cam4_mayo_corroboration() -> None:
    payload = {
        "v": "4",
        "phase": [["P02", 0.82]],
        "tool": [],
        "intent": ["none", "", 0.0],
        "mayo": [
            ["T02", "reuse", 0.84],
            ["T04", "recover", 0.91],
        ],
        "mayo_retrieve": ["T04", 0.91],
        "u": 0.2,
        "sum": "The field appears stable.",
        "bed_robot_arm_group": None,
    }
    context = {
        "phase_search_mode": "temporal_prior",
        "evidence_window": {
            "speech": [],
            "observed_signals": [],
        },
        "candidates": {
            "phase": [["P02", 0.9]],
            "tool": [],
            "evidence": {
                "current_phase": "P02",
                "allowed_next": ["P03"],
                "phase_search_mode": "temporal_prior",
            },
        },
        "digital_twin": {
            "hands": {},
            "tools": [],
        },
        "observable_perception": _aligned_context(
            [
                {
                    "name": "Adson forceps",
                    "count": 1,
                    "max_confidence": 0.94,
                    "mean_confidence": 0.91,
                }
            ]
        )["observable_perception"],
    }

    stabilized = _node()._stabilize_actor_log_payload(payload, context)

    assert stabilized["mayo"] == [["T02", "reuse", 0.84]]
    assert stabilized["mayo_retrieve"] == ["", 0.0]


def test_schema_v4_corroborated_mayo_rows_publish_tool_observations() -> None:
    class _Publisher:
        def __init__(self) -> None:
            self.messages = []

        def publish(self, message) -> None:
            self.messages.append(message)

    node = _node()
    node._phase_pub = _Publisher()
    node._gesture_pub = _Publisher()
    node._result_pub = _Publisher()
    node._tool_pub = _Publisher()
    node._publish_bed_robot_arm_group_proposal = lambda *args, **kwargs: None
    node._publish_health = lambda **kwargs: None
    payload = {
        "v": "4",
        "phase": [["P02", 0.82]],
        "tool": [],
        "intent": ["none", "", 0.0],
        "mayo": [["T04", "reuse", 0.91]],
        "mayo_retrieve": ["", 0.0],
        "u": 0.2,
        "sum": "The operative field appears stable.",
        "bed_robot_arm_group": None,
    }

    node._publish_vlm_outputs(
        payload,
        "{}",
        observation_stamp=Time(sec=44),
        image_source="flir_rfdetr_segmented",
        latency_sec=0.2,
        prompt_chars=100,
        parse_retry_count=0,
        last_error="",
        mode="openai_compat",
        healthy=True,
        connected=True,
    )

    assert len(node._tool_pub.messages) == 1
    observation = node._tool_pub.messages[0]
    assert observation.instrument_id == "T04"
    assert observation.location_type == "mayo_stand"
    assert observation.location_id == "mayo_stand"
    assert observation.confidence == pytest.approx(0.91)
