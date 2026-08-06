from io import BytesIO
import json
from pathlib import Path
import threading
import time

from PIL import Image

from vlm_node.real_vlm import (
    INFERENCE_TRIGGER_REPLAY_FRAME,
    INFERENCE_TRIGGER_SOURCE_FRAME,
    ImageSample,
    InferenceBackpressure,
    RealVLMNode,
    bound_image_for_model,
    compose_flir_cam4_for_model,
    crop_cam4_for_model,
    dynamic_cam4_crop_xywh,
    explicit_phase_start_floor_context,
    image_samples_are_aligned,
    should_run_periodic_live_frame,
    should_trigger_replay_frame,
    should_trigger_source_time_live_frame,
    should_use_open_set_phase_bootstrap,
    source_frame_is_fresh,
    summarize_public_perception_json,
)


def test_first_replay_frame_triggers() -> None:
    assert should_trigger_replay_frame(None, 0.2, 1.0)


def test_replay_trigger_uses_source_stamp_period() -> None:
    assert not should_trigger_replay_frame(0.2, 1.19, 1.0)
    assert should_trigger_replay_frame(0.2, 1.2, 1.0)


def test_replay_trigger_recovers_from_source_time_reset() -> None:
    assert should_trigger_replay_frame(8.0, 0.0, 1.0)


def test_periodic_live_inference_does_not_repeat_a_stalled_frame() -> None:
    assert should_run_periodic_live_frame(None, 71.518)
    assert not should_run_periodic_live_frame(71.518, 71.518)
    assert should_run_periodic_live_frame(71.518, 77.504)
    assert should_run_periodic_live_frame(77.504, 0.0)


def test_source_frame_freshness_uses_replay_clock_lag() -> None:
    assert source_frame_is_fresh(44.35, 44.0, 0.35)
    assert not source_frame_is_fresh(44.351, 44.0, 0.35)
    assert source_frame_is_fresh(0.0, 44.0, 0.35)
    assert source_frame_is_fresh(99.0, 1.0, 0.0)


def test_source_time_live_cadence_tolerates_frame_jitter() -> None:
    assert should_trigger_source_time_live_frame(40.118, 41.117, 1.0)
    assert not should_trigger_source_time_live_frame(40.118, 41.05, 1.0)


def test_open_set_phase_bootstrap_is_bounded_by_successful_observations() -> None:
    assert should_use_open_set_phase_bootstrap(0, 8, False)
    assert should_use_open_set_phase_bootstrap(7, 8, False)
    assert not should_use_open_set_phase_bootstrap(8, 8, False)


def test_explicit_start_phase_disables_open_set_bootstrap() -> None:
    assert not should_use_open_set_phase_bootstrap(0, 8, True)
    assert not should_use_open_set_phase_bootstrap(0, 0, False)


def test_explicit_start_phase_is_persistent_normal_phase_floor() -> None:
    floor = explicit_phase_start_floor_context(
        "P03",
        explicit_start_phase=True,
        normal_phase_ids=["P01", "P02", "P03", "P04"],
        interrupt_phase_ids=["I01"],
    )
    assert floor == {
        "id": "P03",
        "source": "operator_or_procedure_selected_start",
        "ground_truth": False,
        "policy": "normal_phase_floor",
        "allowed_normal_phase_ids": ["P03", "P04"],
        "interrupt_phase_ids": ["I01"],
    }
    assert explicit_phase_start_floor_context(
        "P03",
        explicit_start_phase=False,
        normal_phase_ids=["P01", "P02", "P03"],
        interrupt_phase_ids=[],
    ) is None
    assert explicit_phase_start_floor_context(
        "I01",
        explicit_start_phase=True,
        normal_phase_ids=["P01", "P02", "P03"],
        interrupt_phase_ids=["I01"],
    ) is None


def test_model_image_is_bounded_without_changing_aspect_ratio() -> None:
    source = BytesIO()
    Image.new("RGB", (1840, 1280), color=(64, 96, 128)).save(
        source,
        format="JPEG",
    )

    bounded, mime_type = bound_image_for_model(
        source.getvalue(),
        "image/jpeg",
        1024,
    )

    with Image.open(BytesIO(bounded)) as image:
        assert image.size == (1024, 712)
    assert mime_type == "image/jpeg"


def test_small_model_image_is_not_reencoded() -> None:
    source = BytesIO()
    Image.new("RGB", (640, 480), color=(64, 96, 128)).save(
        source,
        format="JPEG",
    )
    payload = source.getvalue()

    bounded, mime_type = bound_image_for_model(
        payload,
        "image/jpeg",
        1024,
    )

    assert bounded is payload
    assert mime_type == "image/jpeg"


def _jpeg(size: tuple[int, int], color: tuple[int, int, int]) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, color=color).save(output, format="JPEG")
    return output.getvalue()


def _transparent_webp(
    size: tuple[int, int],
    color: tuple[int, int, int, int],
) -> bytes:
    output = BytesIO()
    Image.new("RGBA", size, color=color).save(
        output,
        format="WEBP",
        lossless=True,
    )
    return output.getvalue()


def _overlay_sample(
    *,
    stamp_sec: int,
    stamp_nanosec: int,
    color: tuple[int, int, int, int],
) -> ImageSample:
    return ImageSample(
        received_monotonic=time.time(),
        stamp_sec=stamp_sec,
        stamp_nanosec=stamp_nanosec,
        frame_id="camera|rfdetr_bbox_overlay",
        data=_transparent_webp((1280, 720), color),
        mime_type="image/webp",
    )


def _sample(
    *,
    stamp_sec: int,
    stamp_nanosec: int,
    color: tuple[int, int, int],
) -> ImageSample:
    return ImageSample(
        received_monotonic=time.time(),
        stamp_sec=stamp_sec,
        stamp_nanosec=stamp_nanosec,
        frame_id="camera",
        data=_jpeg((1280, 720), color),
        mime_type="image/jpeg",
    )


def test_source_time_live_scheduler_admits_every_frame_without_period_throttle() -> None:
    node = RealVLMNode.__new__(RealVLMNode)
    node._inference_backpressure = InferenceBackpressure()
    node._source_time_triggered_live = True
    node._response_mode = "live"
    node._active = True
    node._publish_period_sec = 1.0
    node._last_source_live_trigger_stamp_sec = None
    node._source_live_trigger_pending = False
    node._source_live_trigger_lock = threading.Lock()

    node._queue_source_time_live_frame(
        _sample(stamp_sec=10, stamp_nanosec=0, color=(0, 0, 0))
    )
    assert node._source_live_trigger_pending
    assert node._last_source_live_trigger_stamp_sec == 10.0

    node._source_live_trigger_pending = False
    node._queue_source_time_live_frame(
        _sample(stamp_sec=10, stamp_nanosec=900_000_000, color=(0, 0, 0))
    )
    assert node._source_live_trigger_pending

    node._queue_source_time_live_frame(
        _sample(stamp_sec=11, stamp_nanosec=0, color=(0, 0, 0))
    )
    assert node._source_live_trigger_pending
    assert node._last_source_live_trigger_stamp_sec == 11.0
    assert node._inference_backpressure.snapshot() == {
        "in_flight": False,
        "current_trigger": "",
        "pending_trigger": INFERENCE_TRIGGER_SOURCE_FRAME,
        "coalesced_count": 2,
    }


def test_composite_is_one_bounded_labeled_image() -> None:
    first, mime_type = compose_flir_cam4_for_model(
        _jpeg((1920, 1080), (100, 10, 10)),
        "image/jpeg",
        _jpeg((1280, 720), (10, 100, 10)),
        "image/jpeg",
        cam4_crop_xywh_norm=(0.3, 0.2, 0.6, 0.7),
        max_side_px=1024,
    )
    second, _ = compose_flir_cam4_for_model(
        _jpeg((1920, 1080), (100, 10, 10)),
        "image/jpeg",
        _jpeg((1280, 720), (10, 100, 10)),
        "image/jpeg",
        cam4_crop_xywh_norm=(0.3, 0.2, 0.6, 0.7),
        max_side_px=1024,
    )

    with Image.open(BytesIO(first)) as image:
        assert max(image.size) <= 1024
        assert image.width > image.height
    assert first == second
    assert mime_type == "image/jpeg"


def test_composite_uses_the_same_visible_height_for_flir_and_cam4() -> None:
    composite, _ = compose_flir_cam4_for_model(
        _jpeg((1920, 1080), (180, 20, 20)),
        "image/jpeg",
        _jpeg((1280, 720), (20, 180, 20)),
        "image/jpeg",
        cam4_crop_xywh_norm=(0.3, 0.2, 0.6, 0.7),
        max_side_px=1024,
    )

    with Image.open(BytesIO(composite)) as image:
        # The header is dark, but both panes must fill the complete image row
        # immediately below it.  This rejects the former CAM4 letterbox.
        content_y = 60
        right_pixel = image.getpixel((image.width - 8, content_y))
        lower_right_pixel = image.getpixel((image.width - 8, image.height - 8))

    assert right_pixel[1] > right_pixel[0] * 2
    assert lower_right_pixel[1] > lower_right_pixel[0] * 2


def test_composite_blends_the_cam4_detector_overlay() -> None:
    composite, _ = compose_flir_cam4_for_model(
        _jpeg((1920, 1080), (180, 20, 20)),
        "image/jpeg",
        _jpeg((1280, 720), (20, 180, 20)),
        "image/jpeg",
        cam4_crop_xywh_norm=(0.3, 0.2, 0.6, 0.7),
        max_side_px=1024,
        cam4_overlay_bytes=_transparent_webp(
            (1280, 720),
            (200, 20, 220, 255),
        ),
        cam4_overlay_mime_type="image/webp",
    )

    with Image.open(BytesIO(composite)) as image:
        right_pixel = image.getpixel((image.width - 8, 60))

    # An opaque detector overlay turns the raw green CAM4 panel magenta.
    assert right_pixel[0] > right_pixel[1] * 2
    assert right_pixel[2] > right_pixel[1] * 2


def test_dynamic_cam4_crop_frames_union_with_padding_and_minimum_size() -> None:
    crop = dynamic_cam4_crop_xywh(
        [
            {
                "instances": [
                    {"bbox_xywh_norm": [0.465, 0.449, 0.163, 0.072]},
                    {"bbox_xywh_norm": [0.7, 0.7, 0.05, 0.1]},
                ]
            }
        ],
        fallback_xywh_norm=(0.32, 0.18, 0.62, 0.78),
        padding_norm=0.08,
        minimum_width_norm=0.45,
        minimum_height_norm=0.55,
    )

    x, y, width, height = crop
    assert width >= 0.45
    assert height >= 0.55
    assert x <= 0.465
    assert y <= 0.449
    assert x + width >= 0.75
    assert y + height >= 0.8


def test_dynamic_cam4_crop_uses_broad_fallback_without_detections() -> None:
    assert dynamic_cam4_crop_xywh(
        [],
        fallback_xywh_norm=(0.32, 0.18, 0.62, 0.78),
    ) == (0.32, 0.18, 0.62, 0.78)


def test_cam4_model_crop_has_decoder_aligned_jpeg_dimensions() -> None:
    source = Image.new("RGB", (640, 360), (20, 40, 60))
    encoded = BytesIO()
    source.save(encoded, format="JPEG")

    cropped, mime_type = crop_cam4_for_model(
        encoded.getvalue(),
        cam4_crop_xywh_norm=(0.32, 0.18, 0.62, 0.78),
        max_side_px=512,
    )

    with Image.open(BytesIO(cropped)) as decoded:
        assert decoded.width % 16 == 0
        assert decoded.height % 16 == 0
    assert mime_type == "image/jpeg"


def test_public_segmentation_summary_removes_full_rle_and_is_bounded() -> None:
    instances = [
        {
            "class_id": 6,
            "class_name": "Bovie",
            "track_id": index,
            "bbox_xywh_norm": [0.46, 0.44, 0.16, 0.07],
            "segmentation_rle": {
                "size": [720, 1280],
                "counts": "private-large-mask-payload",
            },
            "mask_area_px": 1871,
            "mask_centroid_norm": [0.535, 0.46],
        }
        for index in range(30)
    ]
    summary = summarize_public_perception_json(
        json.dumps(
            {
                "bag_timestamp_sec": 44.954728699,
                "image": {"width": 1280, "height": 720},
                "frame": {"frame_id": "frame_000661", "instances": instances},
            }
        ),
        kind="segmentation",
        max_instances=24,
    )

    encoded = json.dumps(summary)
    assert len(summary["instances"]) == 24
    assert summary["truncated"] is True
    assert summary["full_mask_rle_included"] is False
    assert "segmentation_rle" not in encoded
    assert "counts" not in encoded
    assert summary["timestamp_sec"] == 44.954729
    assert summary["instances"][0]["mask_area_norm"] == 0.00203


def test_public_bbox_summary_preserves_missing_confidence_truthfully() -> None:
    summary = summarize_public_perception_json(
        json.dumps(
            {
                "bag_timestamp_sec": 44.0,
                "image": {"width": 1280, "height": 720},
                "confidence_available": False,
                "frame": {
                    "frame_id": "frame_000661",
                    "instances": [
                        {
                            "class_name": "Bovie",
                            "track_id": 1,
                            "bbox_xywh_norm": [0.465, 0.449, 0.163, 0.072],
                        }
                    ],
                },
            }
        ),
        kind="bboxes",
    )

    assert summary["confidence_available"] is False
    assert summary["instances"] == [
        {
            "class_name": "Bovie",
            "track_id": 1,
            "bbox_xywh_norm": [0.465, 0.449, 0.163, 0.072],
        }
    ]


def test_multiview_alignment_uses_source_stamp_tolerance() -> None:
    flir = _sample(
        stamp_sec=44,
        stamp_nanosec=0,
        color=(100, 0, 0),
    )
    aligned_cam4 = _sample(
        stamp_sec=44,
        stamp_nanosec=90_000_000,
        color=(0, 100, 0),
    )
    stale_cam4 = _sample(
        stamp_sec=43,
        stamp_nanosec=800_000_000,
        color=(0, 100, 0),
    )

    assert image_samples_are_aligned(
        flir,
        aligned_cam4,
        max_skew_sec=0.1,
    )
    assert not image_samples_are_aligned(
        flir,
        stale_cam4,
        max_skew_sec=0.1,
    )


def test_replay_force_tick_uses_segmented_flir_without_cam4_image() -> None:
    node = RealVLMNode.__new__(RealVLMNode)
    node._inference_backpressure = InferenceBackpressure()
    node._response_mode = "replay"
    node._active = True
    node._image_stale_sec = 3.0
    node._publish_period_sec = 1.0
    node._last_replay_image_stamp_sec = None
    node._latest_images = {
        "field": _sample(
            stamp_sec=44,
            stamp_nanosec=0,
            color=(100, 0, 0),
        ),
    }

    node._maybe_trigger_replay_image_tick()
    assert node._last_replay_image_stamp_sec == 44.0
    assert (
        node._inference_backpressure.snapshot()["pending_trigger"]
        == INFERENCE_TRIGGER_REPLAY_FRAME
    )


def _segmented_flir_node() -> RealVLMNode:
    node = RealVLMNode.__new__(RealVLMNode)
    node._latest_images = {
        "field": _sample(
            stamp_sec=44,
            stamp_nanosec=50_000_000,
            color=(100, 0, 0),
        ),
    }
    node._latest_perception = {}
    node._perception_buffers = {}
    node._image_buffers = {}
    node._perception_enabled = True
    node._image_stale_sec = 3.0
    node._image_max_side_px = 1024
    node._multiview_image_max_side_px = 512
    node._field_image_topic = (
        "/surgery/images/flir/segmented/compressed"
    )
    node._raw_field_image_topic = "/surgery/images/flir/compressed"
    node._cam4_image_topic = "/surgery/images/cam4/compressed"
    node._cam4_overlay_image_topic = (
        "/surgery/images/cam4/detection_overlay/compressed"
    )
    node._composite_image_topic = (
        "/surgery/images/vlm/composite/compressed"
    )
    node._require_cam4_image = False
    node._multiview_max_skew_sec = 0.1
    node._cam4_dynamic_crop = False
    node._cam4_crop_xywh_norm = (0.32, 0.18, 0.62, 0.78)
    node._perception_stale_sec = 3.0
    node._perception_image_max_skew_sec = 0.2
    node._cam4_semantics_topic = (
        "/surgery/perception/cam4/semantics/json"
    )
    node._perception_bboxes_topic = ""
    node._perception_segmentation_topic = ""
    node._current_perception_reference_stamp_sec = None
    node._current_image_input_error = ""
    return node


def test_model_selection_fuses_segmented_flir_and_cam4_overlay_into_one_image() -> None:
    node = _segmented_flir_node()
    node._latest_images["cam4"] = _sample(
        stamp_sec=44,
        stamp_nanosec=80_000_000,
        color=(0, 100, 0),
    )
    node._latest_images["cam4_overlay"] = _overlay_sample(
        stamp_sec=44,
        stamp_nanosec=80_000_000,
        color=(190, 10, 210, 255),
    )

    images, image_source, model_image = node._select_images()

    assert image_source == "flir_cam4_rfdetr_segmented"
    assert len(images) == 1
    assert images[0][0] == (
        "Synchronized FLIR surgical field + CAM4 Mayo/surgeon-hand context"
    )
    for _label, image_bytes, _mime_type in images:
        with Image.open(BytesIO(image_bytes)) as image:
            assert max(image.size) <= 1024
            assert image.width > image.height
    assert model_image is not None
    assert model_image.stamp_sec == 44
    assert model_image.stamp_nanosec == 50_000_000
    assert model_image.data == images[0][1]
    assert node._current_visual_input == {
        "image_source": "flir_cam4_rfdetr_segmented",
        "model_ready_topic": "/surgery/images/vlm/composite/compressed",
        "perception_image_max_skew_sec": 0.2,
        "sources": [
            {
                "role": "flir_segmented",
                "topic": "/surgery/images/flir/segmented/compressed",
                "stamp_sec": 44.05,
                "frame_id": "camera",
            },
            {
                "role": "cam4_mayo_hand_crop",
                "topic": "/surgery/images/cam4/compressed",
                "stamp_sec": 44.08,
                "frame_id": "camera",
                "offset_sec": 0.03,
            },
            {
                "role": "cam4_rfdetr_small_overlay",
                "topic": "/surgery/images/cam4/detection_overlay/compressed",
                "stamp_sec": 44.08,
                "frame_id": "camera|rfdetr_bbox_overlay",
                "offset_sec": 0.03,
                "cam4_offset_sec": 0.0,
            },
        ],
        "preprocessing": (
            "single side-by-side composite: RFDETRSegSmall FLIR + "
            "RFDETRSmall CAM4 bbox/hand overlay"
        ),
        "image_layout": "flir_left_cam4_right",
        "cam4_image_forwarded_to_vlm": True,
        "cam4_alignment_skew_sec": 0.03,
        "cam4_detector_overlay_forwarded_to_vlm": True,
        "cam4_detector_overlay_alignment_skew_sec": 0.0,
        "detector_advisory": True,
        "cam4_fallback_reason": "",
        "cam4_overlay_fallback_reason": "",
        "input_error": "",
    }


def test_model_selection_falls_back_to_raw_flir_when_detector_is_off() -> None:
    node = _segmented_flir_node()
    node._perception_enabled = False
    node._latest_images = {
        "raw_field": _sample(
            stamp_sec=51,
            stamp_nanosec=0,
            color=(120, 40, 0),
        ),
        "cam4": _sample(
            stamp_sec=51,
            stamp_nanosec=40_000_000,
            color=(0, 120, 40),
        ),
    }

    images, image_source, model_image = node._select_images()

    assert image_source == "flir_cam4_raw_fallback"
    assert len(images) == 1
    assert model_image is not None
    assert images[0][0].startswith("Synchronized FLIR")
    assert node._current_visual_input["cam4_image_forwarded_to_vlm"] is True
    assert (
        node._current_visual_input["cam4_detector_overlay_forwarded_to_vlm"]
        is False
    )
    assert "perception is disabled" in node._current_visual_input[
        "cam4_overlay_fallback_reason"
    ]
    assert node._current_visual_input["detector_advisory"] is False


def test_model_selection_fails_closed_without_any_flir() -> None:
    node = _segmented_flir_node()
    node._latest_images = {}

    images, image_source, model_image = node._select_images()

    assert images == []
    assert image_source == "missing(flir_visual)"
    assert model_image is None
    assert "segmented and raw fallback unavailable" in node._current_image_input_error


def _cam4_semantics(timestamp_sec: float) -> dict[str, object]:
    return {
        "schema": "taskplanner.cam4_semantics.v1",
        "source": "cam4_rfdetr_small",
        "source_stamp_sec": timestamp_sec,
        "ground_truth": False,
        "cam4_image_forwarded_to_vlm": False,
        "tools": [
            {
                "name": "Bovie surgical cautery",
                "count": 1,
                "max_confidence": 0.9,
                "mean_confidence": 0.9,
            }
        ],
        "tool_request": {
            "state": "request",
            "requested": True,
            "confidence": 0.8,
        },
    }


def test_cam4_semantics_is_aligned_to_segmented_flir_stamp() -> None:
    node = _segmented_flir_node()
    node._latest_perception["cam4_semantics"] = (
        time.time(),
        _cam4_semantics(44.08),
    )

    node._select_images()
    context = node._public_perception_context()

    assert context["tools"][0]["name"] == "Bovie surgical cautery"
    assert context["cam4_image_forwarded_to_vlm"] is False
    assert context["alignment"] == {
        "status": "aligned",
        "detector_stamp_sec": 44.08,
        "offset_sec": 0.03,
    }
    assert context["flir_reference_stamp_sec"] == 44.05
    assert context["max_source_skew_sec"] == 0.2


def test_misaligned_cam4_semantics_is_omitted() -> None:
    for timestamp_sec, expected_offset in (
        (43.849, -0.201),
        (44.251, 0.201),
    ):
        node = _segmented_flir_node()
        node._latest_perception["cam4_semantics"] = (
            time.time(),
            _cam4_semantics(timestamp_sec),
        )

        node._select_images()
        context = node._public_perception_context()

        assert "tools" not in context
        assert context["alignment"] == {
            "status": "omitted_source_timestamp_misaligned",
            "detector_stamp_sec": timestamp_sec,
            "offset_sec": expected_offset,
        }


def test_cam4_semantics_buffer_selects_nearest_source_frame() -> None:
    node = _segmented_flir_node()
    now = time.time()
    aligned = (now - 0.1, _cam4_semantics(44.08))
    newer_but_misaligned = (now, _cam4_semantics(44.72))
    node._latest_perception["cam4_semantics"] = newer_but_misaligned
    node._perception_buffers["cam4_semantics"] = [
        aligned,
        newer_but_misaligned,
    ]

    node._select_images()
    context = node._public_perception_context()

    assert context["alignment"] == {
        "status": "aligned",
        "detector_stamp_sec": 44.08,
        "offset_sec": 0.03,
    }
    assert context["tools"][0]["name"] == "Bovie surgical cautery"


def test_shadow_launch_uses_rfdetr_input_contract() -> None:
    launch_source = (
        Path(__file__).parents[2]
        / "bringup"
        / "launch"
        / "taskplanner_shadow.launch.py"
    ).read_text(encoding="utf-8")

    for topic in (
        "/surgery/images/flir/compressed",
        "/surgery/images/cam4/compressed",
        "/surgery/images/flir/segmented/compressed",
        "/surgery/images/cam4/detection_overlay/compressed",
        "/surgery/images/vlm/composite/compressed",
        "/surgery/perception/cam4/semantics/json",
    ):
        assert topic in launch_source
    assert '"require_cam4_image": False' in launch_source
    assert 'executable="rfdetr_perception_bridge"' in launch_source
    assert '"vlm_perception_image_max_skew_sec"' in launch_source
    assert '"replay_vlm_max_visual_lead_sec"' in launch_source
    assert '"vlm_model_input_max_source_lag_sec"' in launch_source
    assert '"source_time_triggered_live": True' in launch_source
    assert '"model_input_max_source_lag_sec"' in launch_source
    assert '"vlm_input_image_topic"' in launch_source
    assert (
        '"vlm_input_image_topic": (\n'
        "                            composite_image_topic\n"
        "                        )"
    ) in launch_source
