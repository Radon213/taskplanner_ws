from __future__ import annotations

from array import array
from collections import deque
import json
import time
from types import SimpleNamespace
import threading

import numpy as np

from vlm_node.real_vlm import (
    RealVLMNode,
    is_model_ready_visual_source,
    normalize_clinical_analysis,
)
from vlm_node.rfdetr_bridge import (
    _camera_model_identity,
    BufferedFrame,
    BufferedHandEvidence,
    RFDETRBridgeNode,
    build_camera_tool_observations,
    build_contract_diagnostics,
    closest_aligned_frame,
)
from vlm_node.rfdetr_contract import (
    CAM4_CLASS_NAMES,
    Cam4MayoPlacementTracker,
    LOCAL_RFDETR_CAM4_OVERLAY_TOPIC,
    LOCAL_RFDETR_FLIR_OVERLAY_TOPIC,
    LOCAL_RFDETR_FLIR_SEGMENTED_TOPIC,
    RFDETR_CAM4_OVERLAY_FRAME_MARKER,
    RFDETR_FLIR_SEGMENTED_FRAME_MARKER,
    append_rfdetr_frame_marker,
    frame_id_has_rfdetr_marker,
    parse_cam4_semantics_json,
    summarize_cam4_detections,
    summarize_rfdetr_tool_observations,
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


def _camera_diagnostics(*, instances: list[dict] | None = None) -> dict:
    return {
        "model_version": "rfdetr-mayo-bbox-3class-20260824-b78d759bdba2",
        "ontology_version": "mayo-bbox-3class-v1",
        "model_provenance": {
            "checkpoint_id": (
                "mayo_bbox_3class_strong_inventory6_20260824/"
                "checkpoint_best_regular.pth"
            ),
            "sha256": "b78d759b" + "a" * 56,
            "training_run": "mayo_bbox_3class_strong_inventory6_20260824",
        },
        "instances": [] if instances is None else instances,
    }


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


def test_cam4_summary_contains_counts_but_no_coordinates_or_hand_decision() -> None:
    summary = summarize_cam4_detections(
        [
            {
                "class_name": "Adson",
                "confidence": 0.91,
                "xyxy": [1, 2, 3, 4],
            },
            {
                "class_name": "Adson",
                "confidence": 0.81,
                "xyxy": [5, 6, 7, 8],
            },
            {
                "class_name": "Bovie",
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
            "name": "Adson",
            "count": 2,
            "max_confidence": 0.91,
            "mean_confidence": 0.86,
        },
        {
            "name": "Bovie",
            "count": 1,
            "max_confidence": 0.93,
            "mean_confidence": 0.93,
        },
    ]
    assert summary["tool_request"] == {
        "state": "uncertain",
        "requested": None,
        "confidence": 0.0,
        "detector_class": "",
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


def test_cam4_exact_three_class_ontology_ignores_removed_legacy_classes() -> None:
    assert CAM4_CLASS_NAMES == (
        "Adson",
        "Bovie",
        "Bipolar",
    )
    summary = summarize_cam4_detections(
        [
            {"class_name": "Allis", "confidence": 0.99},
            {"class_name": "Mosquito", "confidence": 0.99},
            {"class_name": "Hand_request", "confidence": 0.99},
            {"class_name": "Army navy retractor", "confidence": 0.99},
            {"class_name": "Hand_with_tool", "confidence": 0.99},
            {"class_name": "Bipolar", "confidence": 0.82},
        ],
        source_stamp_sec=11.0,
        inference_latency_ms=1.0,
    )
    assert summary["tools"] == [
        {
            "name": "Bipolar",
            "count": 1,
            "max_confidence": 0.82,
            "mean_confidence": 0.82,
        }
    ]
    assert summary["tool_request"]["state"] == "uncertain"


def _typed_rfdetr_observation(
    *,
    view: str = "cam_3",
    model_version: str = "cam3-rfdetr-tool-v1",
) -> SimpleNamespace:
    return SimpleNamespace(
        header=SimpleNamespace(
            stamp=SimpleNamespace(sec=44, nanosec=80_000_000),
            frame_id="cam_optical_frame_should_not_forward",
        ),
        sequence=18,
        schema_version="pnu.tool_observation_2d.v1",
        observation_id="cam3:44:80000000",
        view=view,
        image_width=1280,
        image_height=720,
        model_version=model_version,
        ontology_version="thyroid-tools-v1",
        instances=[
            SimpleNamespace(
                frame_local_instance_id=3,
                canonical_class_id=7,
                model_class_index=4,
                class_name="Bovie surgical cautery",
                class_confidence=0.91,
                bbox_xyxy_px=[128.0, 144.0, 640.0, 432.0],
                observation_point_valid=True,
                observation_point_uv_px=[512.0, 360.0],
                observation_point_depth_valid=True,
                observation_point_depth_m=0.83,
                mask_counts="private-large-rle-must-not-cross-to-vlm",
            ),
        ],
    )


def test_typed_rfdetr_observation_is_bounded_and_excludes_mask_data() -> None:
    summary = summarize_rfdetr_tool_observations(
        _typed_rfdetr_observation(),
        expected_view="cam_3",
    )

    assert summary == {
        "schema": "taskplanner.rfdetr_tool_observations.v1",
        "source": "rfdetr_tool_observation_2d",
        "view": "cam_3",
        "source_stamp_sec": 44.08,
        "sequence": 18,
        "model_version": "cam3-rfdetr-tool-v1",
        "ontology_version": "thyroid-tools-v1",
        "instances": [
            {
                "class_name": "Bovie surgical cautery",
                "canonical_class_id": 7,
                "model_class_index": 4,
                "confidence": 0.91,
                "bbox_xyxy_norm": [0.1, 0.2, 0.5, 0.6],
                "center_uv_norm": [0.3, 0.4],
                "image_region": "middle_center",
                "frame_local_instance_id": 3,
                "observation_point_uv_norm": [0.4, 0.5],
                "depth_m": 0.83,
            }
        ],
        "detection_status": "detections",
        "truncated": False,
        "ground_truth": False,
        "mask_rle_forwarded_to_vlm": False,
    }
    encoded = json.dumps(summary)
    assert "mask_counts" not in encoded
    assert "private-large-rle" not in encoded
    assert "cam_optical_frame" not in encoded


def test_typed_rfdetr_observation_rejects_wrong_view_or_missing_provenance() -> None:
    assert not summarize_rfdetr_tool_observations(
        _typed_rfdetr_observation(view="cam_4"),
        expected_view="cam_3",
    )
    assert not summarize_rfdetr_tool_observations(
        _typed_rfdetr_observation(model_version=""),
        expected_view="cam_3",
    )


def test_typed_rfdetr_observation_allows_unpinned_model_rollout() -> None:
    summary = summarize_rfdetr_tool_observations(
        _typed_rfdetr_observation(model_version="provider-vNext-2026-09"),
        expected_view="cam_3",
    )

    assert summary["model_version"] == "provider-vNext-2026-09"


def test_typed_rfdetr_observation_honors_explicit_exact_model_pin() -> None:
    message = _typed_rfdetr_observation(model_version="provider-v2")

    assert not summarize_rfdetr_tool_observations(
        message,
        expected_view="cam_3",
        expected_model_version="provider-v1",
    )
    assert summarize_rfdetr_tool_observations(
        message,
        expected_view="cam_3",
        expected_model_version="provider-v2",
    )


def test_typed_rfdetr_empty_frame_is_valid_no_detection_not_failure() -> None:
    message = _typed_rfdetr_observation()
    message.instances = []

    summary = summarize_rfdetr_tool_observations(
        message,
        expected_view="cam_3",
    )

    assert summary["detection_status"] == "no_detections"
    assert summary["instances"] == []
    assert summary["truncated"] is False


def test_typed_rfdetr_accepts_ros_primitive_array_coordinates() -> None:
    """ROS generated float arrays must not be mistaken for malformed boxes."""

    message = _typed_rfdetr_observation()
    instance = message.instances[0]
    instance.bbox_xyxy_px = array("f", instance.bbox_xyxy_px)
    instance.observation_point_uv_px = array(
        "f",
        instance.observation_point_uv_px,
    )

    summary = summarize_rfdetr_tool_observations(
        message,
        expected_view="cam_3",
    )

    assert summary["detection_status"] == "detections"
    assert summary["instances"][0]["bbox_xyxy_norm"] == [
        0.1,
        0.2,
        0.5,
        0.6,
    ]
    assert summary["instances"][0]["observation_point_uv_norm"] == [
        0.4,
        0.5,
    ]


def test_typed_rfdetr_accepts_generated_numpy_coordinates() -> None:
    """The live ROS binding presents fixed float arrays as ``ndarray``."""

    message = _typed_rfdetr_observation()
    instance = message.instances[0]
    instance.bbox_xyxy_px = np.asarray(instance.bbox_xyxy_px, dtype=np.float32)
    instance.observation_point_uv_px = np.asarray(
        instance.observation_point_uv_px,
        dtype=np.float32,
    )

    summary = summarize_rfdetr_tool_observations(
        message,
        expected_view="cam_3",
    )

    assert summary["detection_status"] == "detections"
    assert summary["instances"][0]["center_uv_norm"] == [0.3, 0.4]


def test_typed_rfdetr_rejects_matrix_shaped_numpy_coordinates() -> None:
    message = _typed_rfdetr_observation()
    message.instances[0].bbox_xyxy_px = np.asarray(
        [[128.0], [144.0], [640.0], [432.0]],
        dtype=np.float32,
    )

    assert not summarize_rfdetr_tool_observations(
        message,
        expected_view="cam_3",
    )


def test_typed_rfdetr_malformed_nonempty_frame_is_not_zero_detection() -> None:
    message = _typed_rfdetr_observation()
    message.instances[0].bbox_xyxy_px = [10.0, 10.0, 10.0, 20.0]

    assert not summarize_rfdetr_tool_observations(
        message,
        expected_view="cam_3",
    )


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
                "name": "Bovie",
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


def test_cam4_mayo_tracker_renews_stable_placement_lease() -> None:
    tracker = Cam4MayoPlacementTracker(lease_renewal_sec=0.75)

    assert tracker.update(_cam4_mayo_summary(40.0)) == []
    first = tracker.update(_cam4_mayo_summary(40.25))
    assert len(first) == 1

    assert tracker.update(_cam4_mayo_summary(40.50)) == []
    assert tracker.update(_cam4_mayo_summary(40.75)) == []
    renewed = tracker.update(_cam4_mayo_summary(41.0))

    assert len(renewed) == 1
    assert renewed[0].instrument_name == "Bovie surgical cautery"
    assert renewed[0].source_stamp_sec == 41.0
    assert renewed[0].stable_sample_count == 5


def test_public_cam4_parser_strips_unknown_fields_and_boxes() -> None:
    raw = json.dumps(
        {
            "schema": "taskplanner.cam4_semantics.v1",
            "source_stamp_sec": 12.5,
            "tools": [
                {
                    "name": "Adson",
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


def test_local_rfdetr_raster_contract_is_internal_and_provenance_tagged() -> None:
    assert {
        LOCAL_RFDETR_FLIR_SEGMENTED_TOPIC,
        LOCAL_RFDETR_FLIR_OVERLAY_TOPIC,
        LOCAL_RFDETR_CAM4_OVERLAY_TOPIC,
    } == {
        "/taskplanner/internal/rfdetr/flir/segmented/compressed",
        "/taskplanner/internal/rfdetr/flir/segmentation_overlay/compressed",
        "/taskplanner/internal/rfdetr/cam4/detection_overlay/compressed",
    }
    assert all(
        topic.startswith("/taskplanner/internal/rfdetr/")
        for topic in (
            LOCAL_RFDETR_FLIR_SEGMENTED_TOPIC,
            LOCAL_RFDETR_FLIR_OVERLAY_TOPIC,
            LOCAL_RFDETR_CAM4_OVERLAY_TOPIC,
        )
    )

    flir_frame_id = append_rfdetr_frame_marker(
        "flir_color",
        RFDETR_FLIR_SEGMENTED_FRAME_MARKER,
    )
    assert flir_frame_id == "flir_color|rfdetr_seg"
    assert frame_id_has_rfdetr_marker(
        flir_frame_id,
        RFDETR_FLIR_SEGMENTED_FRAME_MARKER,
    )
    assert not frame_id_has_rfdetr_marker(
        "flir_color|rfdetr_seg_overlay",
        RFDETR_FLIR_SEGMENTED_FRAME_MARKER,
    )
    assert append_rfdetr_frame_marker(
        "cam4_color",
        RFDETR_CAM4_OVERLAY_FRAME_MARKER,
    ) == "cam4_color|rfdetr_cam4_overlay"


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
    node._cam4_mayo_source_epoch = 7
    node._cam4_mayo_source_sequence = 0
    now = time.monotonic()
    node._cam4_hand_evidence = deque(
        [
            BufferedHandEvidence(now, 44, 250_000_000, 0, "real"),
            BufferedHandEvidence(now, 44, 500_000_000, 0, "real"),
        ],
        maxlen=8,
    )
    node._cam4_mayo_roi_norm = (0.30, 0.22, 0.66, 0.75)
    node._cam4_mayo_min_bbox_overlap = 0.5
    diagnostics = {
        "instances": [
            {
                "class_name": "Bovie",
                "confidence": 0.9,
                "xyxy": [601.0, 251.75, 712.5, 398.0],
            }
        ]
    }
    image = {"width": 1280, "height": 720}

    node._publish_cam4_mayo_observations(
        _cam4_mayo_summary(44.25),
        diagnostics=diagnostics,
        image=image,
    )
    node._publish_cam4_mayo_observations(
        _cam4_mayo_summary(44.5),
        diagnostics=diagnostics,
        image=image,
    )

    assert len(node._cam4_mayo_observation_pub.messages) == 1
    observation = node._cam4_mayo_observation_pub.messages[0]
    assert observation.instrument_id == "Bovie surgical cautery"
    assert observation.location_type == "mayo_stand"
    assert observation.source == "cam4_rfdetr_mayo_observation"
    assert observation.correlation_id == (
        "cam4-rfdetr-mayo:v2:7:1:44250000000:"
        "Bovie surgical cautery"
    )
    assert observation.source_epoch == 7
    assert observation.source_sequence == 1
    assert observation.stamp.sec == 44
    assert observation.stamp.nanosec == 500_000_000


def test_bridge_drops_duplicate_or_out_of_order_typed_camera_evidence() -> None:
    class _Publisher:
        def __init__(self) -> None:
            self.messages = []

        def publish(self, message) -> None:
            self.messages.append(message)

    class _Logger:
        def __init__(self) -> None:
            self.warnings = []

        def warning(self, message, **_kwargs) -> None:
            self.warnings.append(str(message))

    node = RFDETRBridgeNode.__new__(RFDETRBridgeNode)
    node._tool_observation_sequences = {"cam_3": 0, "cam_4": 0}
    node._last_tool_observation_source_stamps = {
        "cam_3": None,
        "cam_4": None,
    }
    logger = _Logger()
    node.get_logger = lambda: logger
    publisher = _Publisher()
    image = {"width": 640, "height": 480}
    diagnostics = _camera_diagnostics()

    node._publish_camera_tool_observations(
        frame=_frame(44.25),
        view="cam_3",
        diagnostics=diagnostics,
        image=image,
        publisher=publisher,
    )
    # The same frame can be selected once by a CAM4 callback and again by a
    # coalesced FLIR callback.  It must not be republished as fresh evidence.
    node._publish_camera_tool_observations(
        frame=_frame(44.25),
        view="cam_3",
        diagnostics=diagnostics,
        image=image,
        publisher=publisher,
    )
    node._publish_camera_tool_observations(
        frame=_frame(44.20),
        view="cam_3",
        diagnostics=diagnostics,
        image=image,
        publisher=publisher,
    )
    node._publish_camera_tool_observations(
        frame=_frame(44.30),
        view="cam_3",
        diagnostics=diagnostics,
        image=image,
        publisher=publisher,
    )

    assert [message.sequence for message in publisher.messages] == [1, 2]
    assert [message.observation_id for message in publisher.messages] == [
        "cam_3:44:250000000",
        "cam_3:44:300000000",
    ]
    assert len(logger.warnings) == 2


def test_typed_camera_evidence_uses_full_attested_checkpoint_identity() -> None:
    diagnostics = _camera_diagnostics()
    identity = _camera_model_identity(diagnostics, view="cam_3")
    message = build_camera_tool_observations(
        frame=_frame(44.25),
        view="cam_3",
        sequence=1,
        diagnostics=diagnostics,
        image={"width": 640, "height": 480},
    )

    expected = (
        "cam3-rfdetr:"
        "mayo_bbox_3class_strong_inventory6_20260824/"
        "checkpoint_best_regular.pth"
        "@sha256:b78d759b" + "a" * 56
    )
    assert identity == (expected, "mayo-bbox-3class-v1")
    assert message is not None
    assert message.model_version == expected
    # The bridge must withhold a typed observation rather than reintroducing
    # a generic local version if a service response loses provenance.
    diagnostics["model_provenance"] = {"sha256": "4938d76c" + "a" * 56}
    assert _camera_model_identity(diagnostics, view="cam_3") is None


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
