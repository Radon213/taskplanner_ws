from __future__ import annotations

from collections import deque
import inspect
import json
from pathlib import Path
import time
from types import SimpleNamespace

from builtin_interfaces.msg import Time
from procedure_spec import load_bundle
from vlm_node.real_vlm import (
    ACTOR_LOG_CONTEXT_MAX_CHARS,
    INFERENCE_TRIGGER_PERIODIC_LIVE,
    INFERENCE_TRIGGER_SPEECH,
    InferenceBackpressure,
    RealVLMNode,
    bound_actor_log_context,
)


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
    node._last_vlm_phase = "P02"
    node._last_authoritative_phase = "P05"
    node._phase_bootstrap_id = ""
    node._last_good_payload = {
        "v": "4",
        "phase": [["P02", 0.8]],
    }
    node._phase_entered_wall_sec = 123.0
    node._recent_speech = deque()
    node._recent_observed_signals = deque()
    node._recent_skill_statuses = deque()
    node._recent_events = deque()
    node._latest_bed_robot_arm_group_request = None
    node._last_bed_robot_arm_group_proposal_request_id = ""
    node._last_replay_image_stamp_sec = None
    node._inference_backpressure = InferenceBackpressure()
    node._latest_perception = {}
    node._perception_stale_sec = 3.0
    node._perception_image_max_skew_sec = 0.2
    node._perception_bboxes_topic = ""
    node._perception_segmentation_topic = ""
    node._current_perception_reference_stamp_sec = None
    node._current_visual_input = {}
    node._active = False
    node._world = SimpleNamespace(
        filtered_phase="P05",
        instrument_states=[
            SimpleNamespace(
                instrument_id="T01",
                lifecycle_stage="surgeon_owned",
                location_type="surgeon",
            ),
            SimpleNamespace(
                instrument_id="T02",
                lifecycle_stage="mayo_reuse",
                location_type="mayo",
            ),
        ],
    )
    return node


def test_prior_uses_authoritative_twin_phase_not_previous_vlm_phase() -> None:
    evidence = _node()._actor_log_prior_evidence()
    assert evidence["current_phase"] == "P05"
    assert evidence["current_phase"] != "P02"


def test_prior_falls_back_to_authoritative_simulation_phase() -> None:
    node = _node()
    node._world.filtered_phase = ""
    node._simulation = SimpleNamespace(filtered_phase="P04")

    evidence = node._actor_log_prior_evidence()

    assert evidence["current_phase"] == "P04"


def test_public_evidence_uses_ros_source_time_when_available() -> None:
    node = _node()
    node.get_clock = lambda: SimpleNamespace(
        now=lambda: SimpleNamespace(nanoseconds=12_500_000_000)
    )

    assert node._causal_now_sec() == 12.5
    assert node._append_public_speech("Adson")
    assert node._recent_speech[-1]["at"] == 12.5


def test_vlm_input_freshness_uses_source_time_and_concurrent_callbacks() -> None:
    image_callback_source = inspect.getsource(RealVLMNode._make_image_cb)
    perception_callback_source = inspect.getsource(
        RealVLMNode._make_perception_cb
    )
    select_images_source = inspect.getsource(RealVLMNode._select_images)
    init_source = inspect.getsource(RealVLMNode.__init__)

    assert "_causal_now_sec()" in image_callback_source
    assert "_causal_now_sec()" in perception_callback_source
    assert "_causal_now_sec()" in select_images_source
    assert "callback_group=state_group" in init_source
    assert "callback_group=visual_group" in init_source
    assert "self._inference_callback_group" in init_source
    assert "ClockType.STEADY_TIME" in init_source


def test_vlm_backpressure_coalesces_every_trigger_to_latest() -> None:
    policy = InferenceBackpressure()

    assert policy.request(INFERENCE_TRIGGER_PERIODIC_LIVE).disposition == "started"
    assert policy.request(INFERENCE_TRIGGER_PERIODIC_LIVE).disposition == "queued"
    assert policy.request(INFERENCE_TRIGGER_SPEECH).disposition == "coalesced"
    assert policy.complete() == INFERENCE_TRIGGER_SPEECH
    assert policy.complete() is None


def test_open_set_prior_does_not_cut_history_at_model_phase_fluctuations() -> None:
    node = _node()
    node._phase_entered_wall_sec = time.time()
    node._recent_speech.extend(
        [
            {"text": "Adson", "at": time.time() - 20.0},
            {"text": "Adson 하나 더", "at": time.time() - 15.0},
        ]
    )

    evidence = node._actor_log_prior_evidence(
        open_set_phase_search=True,
    )

    assert evidence["phase_entered_sec"] == 0.0
    assert [row["text"] for row in evidence["speech"]] == [
        "Adson",
        "Adson 하나 더",
    ]


def test_actor_log_context_is_bounded_without_repeating_static_ontology() -> None:
    context = {
        "phases": [{"id": f"P{index:02d}"} for index in range(20)],
        "tools": [{"id": f"T{index:02d}"} for index in range(20)],
        "evidence_window": {
            "speech": [
                {"text": f"speech-{index}-" + ("x" * 180)}
                for index in range(10)
            ],
            "observed_signals": [
                {"type": f"signal-{index}", "detail": "x" * 180}
                for index in range(12)
            ],
            "skill_status": [
                {"action": f"skill-{index}", "detail": "x" * 180}
                for index in range(8)
            ],
        },
        "digital_twin": {
            "hands": {},
            "tools": [{"id": "T01", "lc": "mayo_reuse"}],
            "events": [
                {"t": f"event-{index}", "detail": "x" * 180}
                for index in range(6)
            ],
        },
        "candidates": {"phase": [["P03", 0.8]], "tool": [["T04", 0.8]]},
    }

    bounded = bound_actor_log_context(context)

    assert "phases" not in bounded
    assert "tools" not in bounded
    assert bounded["digital_twin"]["tools"] == [
        {"id": "T01", "lc": "mayo_reuse"}
    ]
    assert len(
        json.dumps(
            bounded,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    ) <= ACTOR_LOG_CONTEXT_MAX_CHARS


def test_operator_selected_start_phase_bootstraps_public_prior() -> None:
    node = _node()
    node._world = None
    node._simulation = None
    node._last_vlm_phase = ""
    node._on_control(SimpleNamespace(data="start_actors:P03"))
    evidence = node._actor_log_prior_evidence()
    assert evidence["current_phase"] == "P03"


def test_model_phase_ranking_is_not_overwritten_by_procedure_prior() -> None:
    node = _node()
    payload = {
        "v": "4",
        "phase": [["P04", 0.88], ["P03", 0.52]],
        "tool": [],
        "intent": ["none", "", 0.0],
        "mayo": [],
        "mayo_retrieve": ["", 0.0],
        "u": 0.2,
        "sum": "thyroid region visible",
        "bed_robot_arm_group": None,
    }
    context = {
        "evidence_window": {
            "speech": [],
            "observed_signals": [],
        },
        "candidates": {
            "phase": [["P03", 0.95], ["P04", 0.61]],
            "tool": [],
            "evidence": {
                "current_phase": "P03",
                "allowed_next": ["P04"],
                "phase_search_mode": "temporal_prior",
            },
        },
        "phase_search_mode": "temporal_prior",
        "digital_twin": {"hands": {}, "tools": []},
    }

    stabilized = node._stabilize_actor_log_payload(payload, context)

    assert stabilized["phase"] == [["P04", 0.88], ["P03", 0.52]]


def test_temporal_phase_stabilizer_drops_skipped_normal_phase() -> None:
    node = _node()
    payload = {
        "v": "4",
        "phase": [["P06", 0.95], ["P04", 0.42]],
        "tool": [],
        "intent": ["none", "", 0.0],
        "mayo": [],
        "mayo_retrieve": ["", 0.0],
        "u": 0.2,
        "sum": "energy use looked like a later phase",
        "bed_robot_arm_group": None,
    }
    context = {
        "phase_search_mode": "temporal_prior",
        "evidence_window": {"speech": [], "observed_signals": []},
        "candidates": {
            "phase": [["P03", 1.0], ["P04", 0.4]],
            "tool": [],
            "evidence": {
                "current_phase": "P03",
                "allowed_next": ["P04"],
                "phase_search_mode": "temporal_prior",
            },
        },
        "digital_twin": {"hands": {}, "tools": []},
    }

    stabilized = node._stabilize_actor_log_payload(payload, context)

    assert [row[0] for row in stabilized["phase"]] == ["P04", "P03"]
    assert all(row[0] != "P06" for row in stabilized["phase"])


def test_open_set_public_tool_sequence_can_anchor_phase() -> None:
    node = _node()
    payload = {
        "v": "4",
        "phase": [["P07", 0.95], ["P04", 0.4]],
        "tool": [],
        "intent": ["none", "", 0.0],
        "mayo": [],
        "mayo_retrieve": ["", 0.0],
        "u": 0.2,
        "sum": "close-up image overestimated progress",
        "bed_robot_arm_group": None,
    }
    context = {
        "phase_search_mode": "open_set",
        "evidence_window": {"speech": [], "observed_signals": []},
        "candidates": {
            "phase": [["P03", 1.0], ["P07", 0.8]],
            "tool": [],
            "evidence": {
                "current_phase": "",
                "allowed_next": [],
                "phase_search_mode": "open_set",
                "sequence_alignment": {
                    "P03": {"matches": 3, "adjacent": 2},
                    "P07": {"matches": 2, "adjacent": 1},
                },
            },
        },
        "digital_twin": {"hands": {}, "tools": []},
    }

    stabilized = node._stabilize_actor_log_payload(payload, context)

    assert stabilized["phase"][0][0] == "P03"
    assert stabilized["sum"] == "close-up image overestimated progress"
    assert "public-sequence-anchor" not in stabilized["sum"]


def test_open_set_phase_waits_for_public_sequence_anchor() -> None:
    node = _node()
    payload = {
        "v": "4",
        "phase": [["P07", 0.95], ["P04", 0.4]],
        "tool": [],
        "intent": ["none", "", 0.0],
        "mayo": [],
        "mayo_retrieve": ["", 0.0],
        "u": 0.2,
        "sum": "close-up image is ambiguous",
        "bed_robot_arm_group": None,
    }
    context = {
        "phase_search_mode": "open_set",
        "evidence_window": {"speech": [], "observed_signals": []},
        "candidates": {
            "phase": [["P04", 0.62], ["P07", 0.55]],
            "tool": [],
            "evidence": {
                "current_phase": "",
                "allowed_next": [],
                "phase_search_mode": "open_set",
                "sequence_alignment": {
                    "P04": {"matches": 1, "adjacent": 0},
                    "P07": {"matches": 0, "adjacent": 0},
                },
            },
        },
        "digital_twin": {"hands": {}, "tools": []},
    }

    stabilized = node._stabilize_actor_log_payload(payload, context)

    assert stabilized["phase"] == []
    assert stabilized["sum"] == "close-up image is ambiguous"
    assert "phase-bootstrap" not in stabilized["sum"]


def test_open_set_visual_phase_is_not_overridden_by_tool_sequence_when_forbidden() -> None:
    node = _node()
    payload = {
        "v": "4",
        "phase": [["P05", 0.86], ["P04", 0.51]],
        "tool": [],
        "intent": ["none", "", 0.0],
        "mayo": [],
        "mayo_retrieve": ["", 0.0],
        "u": 0.2,
        "sum": "stable retraction with sustained target manipulation",
        "bed_robot_arm_group": None,
    }
    context = {
        "phase_search_mode": "open_set",
        "evidence_window": {"speech": [], "observed_signals": []},
        "candidates": {
            "phase": [["P04", 1.0], ["P05", 0.7]],
            "tool": [],
            "evidence": {
                "current_phase": "",
                "allowed_next": [],
                "phase_search_mode": "open_set",
                "tool_sequence_open_set_anchor_allowed": False,
                "sequence_alignment": {
                    "P04": {"matches": 3, "adjacent": 2},
                    "P05": {"matches": 2, "adjacent": 1},
                },
            },
        },
        "digital_twin": {"hands": {}, "tools": []},
    }

    stabilized = node._stabilize_actor_log_payload(payload, context)

    assert stabilized["phase"] == [["P05", 0.86], ["P04", 0.51]]


def test_phase_abstention_is_published_without_default_phase() -> None:
    node = _node()

    class _Publisher:
        def __init__(self) -> None:
            self.messages = []

        def publish(self, message) -> None:
            self.messages.append(message)

    node._phase_pub = _Publisher()
    node._gesture_pub = _Publisher()
    node._result_pub = _Publisher()
    node._tool_pub = _Publisher()
    node._publish_bed_robot_arm_group_proposal = lambda *args, **kwargs: None
    node._publish_health = lambda **kwargs: None
    payload = {
        "v": "4",
        "phase": [],
        "tool": [],
        "intent": ["none", "", 0.0],
        "mayo": [],
        "mayo_retrieve": ["", 0.0],
        "u": 0.8,
        "sum": "phase-bootstrap-waiting-for-public-sequence",
        "bed_robot_arm_group": None,
    }

    node._publish_vlm_outputs(
        payload,
        "{}",
        observation_stamp=Time(),
        image_source="shadow",
        latency_sec=0.1,
        prompt_chars=10,
        parse_retry_count=0,
        last_error="",
        mode="openai_compat",
        healthy=True,
        connected=True,
    )

    assert node._phase_pub.messages[-1].phase_ids == []
    assert node._result_pub.messages[-1].phase_ids == []
    assert node._gesture_pub.messages[-1].phase_id == ""


def test_model_raw_audit_preserves_pre_stabilization_intent() -> None:
    class _Publisher:
        def __init__(self) -> None:
            self.messages = []

        def publish(self, message) -> None:
            self.messages.append(message)

    node = _node()
    node._model_raw_result_pub = _Publisher()
    payload = {
        "v": "4",
        "phase": [["P03", 0.84]],
        "tool": [["T02", 0.91]],
        "intent": ["handover", "T02", 0.88],
        "gesture": ["request_tool", "T02", "open_receive", 0.86],
        "mayo": [],
        "mayo_retrieve": ["", 0.0],
        "u": 0.16,
        "sum": "Fine dissection continues.",
        "bed_robot_arm_group": None,
    }
    raw_json = json.dumps(
        payload,
        separators=(",", ":"),
        sort_keys=True,
    )

    node._publish_model_raw_result(
        payload,
        raw_json,
        observation_stamp=Time(sec=9),
        mode="openai_compat:speech",
    )

    result = node._model_raw_result_pub.messages[-1]
    assert result.raw_json == raw_json
    assert result.phase_ids == ["P03"]
    assert result.gesture_event_type == "request_tool"
    assert result.gesture_requested_tool == "T02"
    assert result.gesture_hand_pose == "open_receive"
    assert result.source.startswith("real_vlm_model_raw:")


def test_model_tool_ranking_is_not_overwritten_by_procedure_prior() -> None:
    node = _node()
    payload = {
        "v": "4",
        "phase": [["P03", 0.88]],
        "tool": [["T02", 0.91], ["T04", 0.63]],
        "intent": ["none", "", 0.0],
        "mayo": [],
        "mayo_retrieve": ["", 0.0],
        "u": 0.2,
        "sum": "fine grasping is visible",
        "bed_robot_arm_group": None,
    }
    context = {
        "evidence_window": {
            "speech": [],
            "observed_signals": [],
        },
        "candidates": {
            "phase": [["P03", 0.95]],
            "tool": [["T05", 1.0], ["T02", 0.56]],
            "evidence": {"current_phase": "P03"},
        },
        "digital_twin": {"hands": {}, "tools": []},
    }

    stabilized = node._stabilize_actor_log_payload(payload, context)

    assert stabilized["tool"][:3] == [
        ["T02", 0.91],
        ["T04", 0.63],
        ["T05", 1.0],
    ]


def test_model_rankings_are_normalized_by_confidence_before_publication() -> None:
    node = _node()
    payload = {
        "v": "4",
        "phase": [["P03", 0.55], ["P04", 0.90]],
        "tool": [["T04", 0.60], ["T02", 0.91]],
        "intent": ["none", "", 0.0],
        "mayo": [],
        "mayo_retrieve": ["", 0.0],
        "u": 0.2,
        "sum": "rank order needs normalization",
        "bed_robot_arm_group": None,
    }
    context = {
        "evidence_window": {
            "speech": [],
            "observed_signals": [],
        },
        "candidates": {
            "phase": [["P03", 0.95]],
            "tool": [["T05", 1.0]],
            "evidence": {"current_phase": "P03"},
        },
        "digital_twin": {"hands": {}, "tools": []},
    }

    stabilized = node._stabilize_actor_log_payload(payload, context)

    assert stabilized["phase"] == [["P04", 0.9], ["P03", 0.55]]
    assert stabilized["tool"] == [
        ["T02", 0.91],
        ["T04", 0.6],
        ["T05", 1.0],
    ]


def test_model_cannot_invent_handover_intent_without_public_request() -> None:
    node = _node()
    payload = {
        "v": "4",
        "phase": [["P03", 0.88]],
        "tool": [["T04", 0.91]],
        "intent": ["handover", "T04", 0.95],
        "mayo": [],
        "mayo_retrieve": ["", 0.0],
        "u": 0.2,
        "sum": "model guessed a request",
        "bed_robot_arm_group": None,
    }
    context = {
        "evidence_window": {
            "speech": [],
            "observed_signals": [],
        },
        "candidates": {
            "phase": [],
            "tool": [],
            "evidence": {"current_phase": "P03"},
        },
        "digital_twin": {"hands": {}, "tools": []},
    }

    stabilized = node._stabilize_actor_log_payload(payload, context)

    assert stabilized["intent"] == ["none", "", 0.0]


def test_public_voice_request_overrides_model_intent_guess() -> None:
    node = _node()
    payload = {
        "v": "4",
        "phase": [["P03", 0.88]],
        "tool": [["T04", 0.91]],
        "intent": ["handover", "T02", 0.4],
        "mayo": [],
        "mayo_retrieve": ["", 0.0],
        "u": 0.2,
        "sum": "public request resolves intent",
        "bed_robot_arm_group": None,
    }
    context = {
        "evidence_window": {
            "speech": [{"text": "Bovie", "at": time.time()}],
            "observed_signals": [],
        },
        "candidates": {
            "phase": [],
            "tool": [],
            "evidence": {"current_phase": "P03"},
        },
        "digital_twin": {"hands": {}, "tools": []},
    }

    stabilized = node._stabilize_actor_log_payload(payload, context)

    assert stabilized["intent"] == ["handover", "T04", 0.72]
    assert stabilized["tool"][0] == ["T04", 1.0]
    assert stabilized["sum"] == "public request resolves intent"
    assert "public-tool-anchor" not in stabilized["sum"]


def test_public_voice_correction_prefers_last_named_tool() -> None:
    node = _node()
    node._recent_speech.append(
        {
            "text": "Bovie 아니 bipolar",
            "at": time.time(),
        }
    )

    assert node._tool_from_public_speech(list(node._recent_speech)) == "T07"
    assert node._tools_from_public_speech(list(node._recent_speech)) == ["T07"]


def test_completed_handover_expires_public_voice_request() -> None:
    node = _node()
    requested_at = time.time() - 1.0
    node._recent_speech.append({"text": "Bovie", "at": requested_at})
    node._recent_skill_statuses.append(
        {
            "at": requested_at + 0.5,
            "action": "pick_up_and_handover",
            "tool": "T04",
            "state": "completed",
            "success": True,
        }
    )

    assert node._tool_from_public_speech(list(node._recent_speech)) == ""


def test_stale_public_voice_request_does_not_persist_as_intent() -> None:
    node = _node()
    node._recent_speech.append(
        {
            "text": "Bovie",
            "at": time.time() - 7.0,
        }
    )

    assert node._tool_from_public_speech(list(node._recent_speech)) == ""


def test_new_public_transcript_queues_one_nonblocking_inference() -> None:
    node = _node()
    node._active = True
    node._response_mode = "live"

    node._on_request_text(SimpleNamespace(data="Bovie"))
    node._on_request_text(SimpleNamespace(data="Bovie"))

    assert node._inference_backpressure.snapshot()["pending_trigger"] == (
        INFERENCE_TRIGGER_SPEECH
    )
    assert node._inference_backpressure.snapshot()["coalesced_count"] == 0


def test_voice_twin_event_and_text_topic_trigger_only_once() -> None:
    node = _node()
    node._active = True
    node._response_mode = "live"
    voice_event = SimpleNamespace(
        event_type="VoiceTranscriptObserved",
        instrument_id="",
    )

    node._ingest_public_twin_event(voice_event, {"voice_text": "Adson"})
    node._on_request_text(SimpleNamespace(data="Adson"))

    assert node._inference_backpressure.snapshot()["pending_trigger"] == (
        INFERENCE_TRIGGER_SPEECH
    )
    assert node._inference_backpressure.snapshot()["coalesced_count"] == 0


def test_actor_overlay_truth_is_absent_from_public_prior() -> None:
    node = _node()
    node._actor_overlay = {
        "hand": "hidden",
        "mayo": ["T99"],
        "field_event": ["hidden"],
    }
    evidence = node._actor_log_prior_evidence()
    assert "field_event" not in evidence
    assert evidence["mayo_tools"] == ["T02"]
    assert "T99" not in evidence["mayo_tools"]


def test_v4_payload_ids_are_canonicalized_without_legacy_keys() -> None:
    node = _node()
    payload = node._canonicalize_payload_ids(
        {
            "v": "4",
            "phase": [["P02", 0.8]],
            "tool": [["T01", 0.7]],
            "intent": ["handover", "T01", 0.9],
            "mayo": [],
            "mayo_retrieve": ["", 0.0],
            "u": 0.2,
            "sum": "ok",
            "bed_robot_arm_group": None,
        }
    )
    assert payload["phase"] == [["P02", 0.8]]
    assert payload["tool"] == [["T01", 0.7]]
    assert payload["intent"] == ["handover", "T01", 0.9]
    assert "ph" not in payload
    assert "sg" not in payload


def test_real_vlm_does_not_subscribe_to_validation_truth_topics() -> None:
    init_source = inspect.getsource(RealVLMNode.__init__)
    assert '"/surgeon/actor_event"' not in init_source
    assert '"/surgeon/actor_overlay"' not in init_source
    assert '"/surgeon/outward_signal"' not in init_source
    assert '"/surgeon/request"' not in init_source
    assert '"/bt/decision"' not in init_source
    assert '"/surgery/audio/request_text"' in init_source


def test_public_perception_context_is_bounded_observer_evidence() -> None:
    node = _node()
    node._perception_bboxes_topic = (
        "/surgery/perception/cam4/tools/bboxes/json"
    )
    node._perception_segmentation_topic = (
        "/surgery/perception/cam4/tools/segmentation/json"
    )
    node._current_perception_reference_stamp_sec = 44.0
    received_at = node._causal_now_sec()
    node._latest_perception = {
        "bboxes": (
            received_at,
            {
                "kind": "bboxes",
                "source": "cam4_public_detector",
                "timestamp_sec": 44.0,
                "instances": [
                    {
                        "class_name": "Bovie",
                        "bbox_xywh_norm": [0.465, 0.449, 0.163, 0.072],
                    }
                ],
                "confidence_available": False,
            },
        ),
        "segmentation": (
            received_at,
            {
                "kind": "segmentation",
                "source": "cam4_public_detector",
                "timestamp_sec": 44.05,
                "instances": [
                    {
                        "class_name": "Bovie",
                        "mask_area_norm": 0.00203,
                    }
                ],
                "segmentation_summary_only": True,
                "full_mask_rle_included": False,
            },
        ),
    }

    context = node._public_perception_context()

    assert context["source"] == "cam4_public_detector"
    assert context["bounded"] is True
    assert context["ground_truth"] is False
    assert context["bboxes"]["confidence_available"] is False
    assert context["segmentation"]["full_mask_rle_included"] is False
    assert context["alignment"]["bboxes"]["status"] == "aligned"
    assert context["alignment"]["segmentation"]["status"] == "aligned"
