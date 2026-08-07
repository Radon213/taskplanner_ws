from __future__ import annotations

from collections import deque
import json
from pathlib import Path
from types import SimpleNamespace

from builtin_interfaces.msg import Time
import pytest
from procedure_spec import compact_procedure_prompt, load_bundle
from vlm_node.real_vlm import RealVLMNode, compact_actor_log_procedure_context


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
    node._procedure_prompt = compact_procedure_prompt(spec_dir)
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

    assert "inspect every visible hand" in instruction
    assert "substantially visible empty receiving palm" in instruction
    assert "Orientation, motion, glove color, and exact image position are irrelevant" in instruction
    assert "operating hand does not cancel a separate requesting hand" in instruction
    assert "cropped fragments" in instruction
    assert "dorsal-only hands" in instruction
    assert "patient/bystander hands" in instruction
    assert 'emit ["request_tool","","open_receive",confidence]' in instruction
    assert "Never infer its tool id" in instruction
    assert 'intent ["none","",0.0]' in instruction
    assert "gesture always has exactly four values" in instruction
    assert 'no request is exactly ["","","",0.0]' in instruction
    assert "Never emit [\"open_receive\",\"\",0.85]" in instruction
    assert "center-right interior request zone" not in instruction
    assert "blue Mayo work surface" not in instruction
    assert "skin-toned staff glove" not in instruction
    assert "blue or green sterile gown" not in instruction


def test_detector_independent_prompt_requires_full_mayo_inventory() -> None:
    instruction = RealVLMNode.__new__(
        RealVLMNode
    )._actor_log_developer_instruction()

    assert "scan the complete hand/Mayo image" in instruction
    assert "one row per distinct visible instrument instance" in instruction
    assert "preserve duplicates" in instruction
    assert "rings, hinge, shaft, jaws, blade, insulation/cable, or lumen" in instruction
    assert "omit unidentifiable silhouettes" in instruction
    assert "Detector rows may support a match" in instruction
    assert "absence does not erase clear pixels" in instruction
    assert "Never copy instruments from the surgical-field image" in instruction
    assert "advisory observation" in instruction
    assert "mayo is always an array of three-value rows" in instruction
    assert 'never ["Txx"], ["tool name"]' in instruction
    assert "Pxx/Txx are shape placeholders only" in instruction
    assert '[["P03",0.80]]' not in instruction
    assert '[["T02",0.70]]' not in instruction


def test_cam4_observations_precede_procedure_priors_in_prompt() -> None:
    instruction = RealVLMNode.__new__(
        RealVLMNode
    )._actor_log_developer_instruction()

    assert instruction.index("GESTURE:") < instruction.index("MAYO:")
    assert instruction.index("MAYO:") < instruction.index("PHASE/NEXT TOOL:")
    assert "gesture and mayo must come only from the hand/Mayo pixels" in instruction


def test_next_tool_prompt_encourages_calibrated_proactive_forecast() -> None:
    instruction = RealVLMNode.__new__(
        RealVLMNode
    )._actor_log_developer_instruction()

    assert "calibrated 2-8 second forecast" in instruction
    assert "forecast of a new handover" in instruction
    assert "not a label for the tool currently in use" in instruction
    assert "which additional instrument the assistant should prepare next" in instruction
    assert "does not inventory visible instruments" in instruction
    assert "do not wait for a hand gesture or spoken request" in instruction
    assert "most plausible near-term additional tool" in instruction
    assert "visible task trajectory" in instruction
    assert "broad procedure-role transitions" in instruction
    assert "distinguish instruments already held" in instruction
    assert "public evidence specifically supports another instance" in instruction
    assert "an already active type must stay below 0.65" in instruction
    assert "forecast a plausible unused tool instead" in instruction
    assert "digital_twin.forecast_inventory.available" in instruction
    assert "rack_available unused stock" in instruction
    assert "mayo_reuse surgeon-used tools expected later" in instruction
    assert "trajectory supports imminent reuse" in instruction
    assert "A tool type may appear in available and unavailable" in instruction
    assert "must have available count >0" in instruction
    assert "never authorizes action" in instruction
    assert "Do not memorize case timing" in instruction
    assert "keep every weak candidate below 0.65" in instruction
    assert "not a confirmed request" in instruction
    assert "match the longest suffix" in instruction
    assert "against every procedure chain" in instruction
    assert "Independently of your phase candidate" in instruction
    assert "Do not choose the next tool solely from your phase output" in instruction


def test_demo_procedure_context_exposes_recurring_chains_and_alternatives() -> None:
    node = _demo_node()

    context = compact_actor_log_procedure_context(
        node._spec,
        node._procedure_prompt,
    )
    phases = {phase["id"]: phase for phase in context["phases"]}

    assert phases["P03"]["chain"] == [
        ["T02", "T02", "T04", "T07", "T04", "T05", "T05"],
        ["T04", "T02"],
    ]
    assert ["T04", "T04"] in phases["P03"]["alt"]
    assert phases["P04"]["chain"] == [["T05", "T05", "T02"]]
    assert phases["P05"]["chain"] == [["T02", "T07", "T08"]]
    assert phases["P06"]["chain"] == [["T08", "T07", "T04"]]
    assert "sequence" not in phases["P03"]


def test_phase_start_floor_prompt_is_runtime_constraint_not_ground_truth() -> None:
    instruction = RealVLMNode.__new__(
        RealVLMNode
    )._actor_log_developer_instruction()

    assert "phase_start_floor" in instruction
    assert "limits phase only, never tool/intent" in instruction
    assert "Not ground truth" in instruction
    assert "allowed_normal_phase_ids, never earlier" in instruction
    assert "Interrupts need visible evidence" in instruction


def test_actor_log_prompt_requires_frame_specific_uncertainty() -> None:
    instruction = RealVLMNode.__new__(
        RealVLMNode
    )._actor_log_developer_instruction()

    assert '"u":1.0' not in instruction
    assert "calculate u independently on every frame" in instruction
    assert "0.26-0.45 for usable adjacent-phase ambiguity" in instruction
    assert "0.80-1.00 only when the relevant view is unusable" in instruction
    assert "Do not copy the structural 0.50 value" in instruction


def test_actor_log_prompt_separates_current_tools_from_next_handover() -> None:
    instruction = RealVLMNode.__new__(
        RealVLMNode
    )._actor_log_developer_instruction()

    assert "currently_in_use lists surgeon-held tools and counts" in instruction
    assert "tool is only a calibrated 2-8 second forecast" in instruction
    assert "available_for_next_handover" in instruction
    assert "the reducer and BT, not the VLM, validate availability" in instruction


def test_actor_log_prompt_is_camera_agnostic_and_token_bounded() -> None:
    node = _demo_node()

    system_prompt = node._actor_log_system_prompt()
    developer_prompt = node._actor_log_developer_instruction()

    assert "do not assume a fixed pixel position" in system_prompt
    assert "ground truth" in system_prompt
    assert "Procedure context:" in system_prompt
    assert "temporal_prior favors current/next" in system_prompt
    assert '"policy":' not in system_prompt
    assert '"flow":' not in system_prompt
    assert '"groups":' in system_prompt
    assert '"roles":' in system_prompt
    assert "central dissection continues without a persistently widened" in system_prompt
    assert "stable exposure is already followed by sustained central target" in system_prompt
    assert "temporal_prior is a preference, not a candidate filter" in developer_prompt
    assert "ASR near-homophones" in developer_prompt
    assert len(system_prompt) + len(developer_prompt) < 14_000


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
