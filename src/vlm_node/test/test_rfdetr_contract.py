from __future__ import annotations

from collections import deque
import json
from types import SimpleNamespace
import threading

from vlm_node.real_vlm import (
    RealVLMNode,
    is_model_ready_visual_source,
    normalize_clinical_analysis,
)
from vlm_node.rfdetr_bridge import (
    BufferedFrame,
    RFDETRBridgeNode,
    build_contract_diagnostics,
    closest_aligned_frame,
)
from vlm_node.rfdetr_contract import (
    Cam4MayoPlacementTracker,
    parse_cam4_semantics_json,
    summarize_cam4_detections,
)


def _frame(stamp: float) -> BufferedFrame:
    seconds = int(stamp)
    return BufferedFrame(
        received_monotonic=1.0,
        stamp_sec=seconds,
        stamp_nanosec=int(round((stamp - seconds) * 1_000_000_000)),
        frame_id="cam4",
        format="jpeg",
        data=b"image",
    )


def test_local_diagnostics_project_to_cv_workbook_schema() -> None:
    result = build_contract_diagnostics(
        {
            "decode_latency_ms": 2.5,
            "render_encode_latency_ms": 3.5,
            "cam4": {
                "model": "RFDETRSmall",
                "inference_latency_ms": 12.25,
                "instances": [{"id": 1}, {"id": 2}],
            },
        },
        cam4=_frame(12.25),
        sequence=7,
        source_to_output_latency_ms=22.0,
    )

    assert result["schema"] == "pnu.rfdetr_diagnostics.v2"
    assert result["source_stamp_sec"] == 12
    assert result["source_stamp_nanosec"] == 250_000_000
    assert result["frame_id"] == "cam4"
    assert result["sequence"] == 7
    assert result["instance_count"] == 2
    assert result["valid_pose_count"] == 0
    assert result["error_code"] == ""


def test_local_diagnostics_fail_closed_without_aligned_cam4() -> None:
    result = build_contract_diagnostics(
        {}, cam4=None, sequence=1, source_to_output_latency_ms=float("nan")
    )

    assert result["observation_id"] == ""
    assert result["source_to_output_latency_ms"] == 0.0
    assert result["error_code"] == "NO_ALIGNED_CAM4"


def test_cam4_summary_contains_counts_and_request_but_no_coordinates() -> None:
    summary = summarize_cam4_detections(
        [
            {
                "class_name": "Adson forceps",
                "confidence": 0.91,
                "xyxy": [1, 2, 3, 4],
            },
            {
                "class_name": "Adson forceps",
                "confidence": 0.81,
                "xyxy": [5, 6, 7, 8],
            },
            {
                "class_name": "Bovie surgical cautery",
                "confidence": 0.93,
                "xyxy": [9, 10, 11, 12],
            },
            {
                "class_name": "Hand_request",
                "confidence": 0.88,
                "xyxy": [13, 14, 15, 16],
            },
        ],
        source_stamp_sec=44.08,
        inference_latency_ms=18.2,
    )

    assert summary["cam4_image_forwarded_to_vlm"] is False
    assert summary["tools"] == [
        {
            "name": "Adson forceps",
            "count": 2,
            "max_confidence": 0.91,
            "mean_confidence": 0.86,
        },
        {
            "name": "Bovie surgical cautery",
            "count": 1,
            "max_confidence": 0.93,
            "mean_confidence": 0.93,
        },
    ]
    assert summary["tool_request"] == {
        "state": "request",
        "requested": True,
        "confidence": 0.88,
        "detector_class": "Hand_request",
    }
    encoded = json.dumps(summary)
    assert "xyxy" not in encoded
    assert "bbox" not in encoded


def test_missing_hand_detection_remains_uncertain() -> None:
    summary = summarize_cam4_detections(
        [],
        source_stamp_sec=10.0,
        inference_latency_ms=1.0,
    )
    assert summary["tool_request"]["state"] == "uncertain"
    assert summary["tool_request"]["requested"] is None


def _cam4_mayo_summary(
    stamp_sec: float,
    *,
    hand_state: str = "not_request",
    confidence: float = 0.9,
) -> dict[str, object]:
    return {
        "schema": "taskplanner.cam4_semantics.v1",
        "source": "cam4_rfdetr_small",
        "source_stamp_sec": stamp_sec,
        "tools": [
            {
                "name": "Bovie surgical cautery",
                "count": 1,
                "max_confidence": confidence,
                "mean_confidence": confidence,
            }
        ],
        "tool_request": {
            "state": hand_state,
            "requested": hand_state == "request",
            "confidence": 0.9,
        },
    }


def test_cam4_mayo_tracker_emits_within_one_four_hz_confirmation_frame() -> None:
    tracker = Cam4MayoPlacementTracker()

    assert tracker.update(
        _cam4_mayo_summary(10.0, hand_state="hand_with_tool")
    ) == []
    assert tracker.update(_cam4_mayo_summary(10.25)) == []
    placements = tracker.update(_cam4_mayo_summary(10.5))

    assert len(placements) == 1
    assert placements[0].instrument_name == "Bovie surgical cautery"
    assert placements[0].source_stamp_sec == 10.5
    assert placements[0].stable_sample_count == 2
    assert placements[0].stable_duration_sec == 0.25


def test_cam4_mayo_tracker_resets_stability_while_hand_carries_tool() -> None:
    tracker = Cam4MayoPlacementTracker()

    assert tracker.update(_cam4_mayo_summary(20.0)) == []
    assert tracker.update(
        _cam4_mayo_summary(20.25, hand_state="hand_with_tool")
    ) == []
    assert tracker.update(_cam4_mayo_summary(20.5)) == []
    placements = tracker.update(_cam4_mayo_summary(20.75))

    assert len(placements) == 1
    assert placements[0].source_stamp_sec == 20.75


def test_cam4_mayo_tracker_rejects_low_confidence_and_duplicate_stamps() -> None:
    tracker = Cam4MayoPlacementTracker()

    assert tracker.update(
        _cam4_mayo_summary(30.0, confidence=0.57)
    ) == []
    assert tracker.update(_cam4_mayo_summary(30.25)) == []
    assert tracker.update(_cam4_mayo_summary(30.25)) == []
    placements = tracker.update(_cam4_mayo_summary(30.5))

    assert len(placements) == 1
    assert placements[0].source_stamp_sec == 30.5


def test_public_cam4_parser_strips_unknown_fields_and_boxes() -> None:
    raw = json.dumps(
        {
            "schema": "taskplanner.cam4_semantics.v1",
            "source_stamp_sec": 12.5,
            "tools": [
                {
                    "name": "Adson forceps",
                    "count": 2,
                    "max_confidence": 0.9,
                    "mean_confidence": 0.8,
                    "xyxy": [1, 2, 3, 4],
                }
            ],
            "tool_request": {
                "state": "request",
                "confidence": 0.7,
                "bbox": [1, 2, 3, 4],
            },
            "cam4_image_base64": "secret-pixels",
        }
    )
    parsed = parse_cam4_semantics_json(raw)
    encoded = json.dumps(parsed)
    assert parsed["tool_request"]["requested"] is True
    assert "xyxy" not in encoded
    assert "bbox" not in encoded
    assert "secret-pixels" not in encoded


def test_closest_cam4_frame_uses_source_stamp_and_skew_gate() -> None:
    frames = [_frame(10.0), _frame(10.08), _frame(10.3)]
    assert closest_aligned_frame(frames, 10.1, 0.05) == frames[1]
    assert closest_aligned_frame(frames, 10.2, 0.05) is None


def test_clinical_analysis_removes_internal_stabilizer_markers() -> None:
    analysis = normalize_clinical_analysis(
        "Fine dissection continues around the thyroid; "
        "candidate-stabilized; public-sequence-anchor=P05"
    )
    assert analysis == "Fine dissection continues around the thyroid"


def test_live_visual_boundary_accepts_fused_and_flir_only_visual_inputs() -> None:
    assert is_model_ready_visual_source("flir_cam4_rfdetr_segmented")
    assert is_model_ready_visual_source("flir_cam4_raw_fallback")
    assert is_model_ready_visual_source("flir_rfdetr_segmented")
    assert is_model_ready_visual_source("flir_raw_fallback")
    assert not is_model_ready_visual_source("field")
    assert not is_model_ready_visual_source("field+tray")
    assert not is_model_ready_visual_source("composite(cam4+flir)")
    assert not is_model_ready_visual_source("cam4")


def test_bridge_disable_clears_pending_frames_and_invalidates_inflight_work() -> None:
    node = RFDETRBridgeNode.__new__(RFDETRBridgeNode)
    node._condition = threading.Condition()
    node._enabled = True
    node._generation = 7
    node._pending_flir = _frame(11.0)
    node._cam4_frames = deque([_frame(11.0)], maxlen=4)
    node._cam4_mayo_tracker = Cam4MayoPlacementTracker()
    published_health: list[dict] = []
    node._publish_health = lambda **kwargs: published_health.append(kwargs)

    response = SimpleNamespace(success=False, message="")
    node._set_enabled(SimpleNamespace(data=False), response)

    assert response.success is True
    assert node._enabled is False
    assert node._generation == 8
    assert node._pending_flir is None
    assert list(node._cam4_frames) == []
    assert node._generation_is_active(7) is False
    assert published_health[-1]["status"] == "disabled"


def test_bridge_publishes_mayo_observation_with_detector_source_stamp() -> None:
    class _Publisher:
        def __init__(self) -> None:
            self.messages = []

        def publish(self, message) -> None:
            self.messages.append(message)

    node = RFDETRBridgeNode.__new__(RFDETRBridgeNode)
    node._cam4_mayo_tracker = Cam4MayoPlacementTracker()
    node._cam4_mayo_observation_pub = _Publisher()

    node._publish_cam4_mayo_observations(_cam4_mayo_summary(44.25))
    node._publish_cam4_mayo_observations(_cam4_mayo_summary(44.5))

    assert len(node._cam4_mayo_observation_pub.messages) == 1
    observation = node._cam4_mayo_observation_pub.messages[0]
    assert observation.instrument_id == "Bovie surgical cautery"
    assert observation.location_type == "mayo_stand"
    assert observation.stamp.sec == 44
    assert observation.stamp.nanosec == 500_000_000


def test_real_vlm_disable_preserves_raw_visual_fallback_caches() -> None:
    node = RealVLMNode.__new__(RealVLMNode)
    node._perception_enabled = True
    node._perception_generation = 2
    node._latest_images = {
        "field": object(),
        "raw_field": object(),
        "cam4": object(),
        "tray": object(),
    }
    node._image_buffers = {
        "field": deque([object()]),
        "raw_field": deque([object()]),
        "cam4": deque([object()]),
        "tray": deque([object()]),
    }
    node._latest_perception = {"cam4_semantics": (1.0, {"tools": []})}
    node._current_visual_input = {"source": "flir_rfdetr_segmented"}
    node._current_perception_reference_stamp_sec = 1.0
    node._last_good_raw = "{}"
    node._last_good_payload = {"v": "4"}
    node._last_periodic_live_image_stamp_sec = 1.0
    health: list[dict] = []
    node._publish_health = lambda **kwargs: health.append(kwargs)

    node._on_perception_health(
        SimpleNamespace(
            data=json.dumps(
                {
                    "schema": "taskplanner.rfdetr_health.v1",
                    "enabled": False,
                    "status": "disabled",
                }
            )
        )
    )

    assert node._perception_enabled is False
    assert node._perception_generation == 3
    assert set(node._latest_images) == {"raw_field", "cam4", "tray"}
    assert set(node._image_buffers) == {"raw_field", "cam4", "tray"}
    assert node._latest_perception == {}
    assert node._last_good_payload is None
    assert health[-1]["mode"] == "raw_visual_fallback_pending"
    assert health[-1]["healthy"] is True
