from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import threading

import cv2
import numpy as np
import pytest


pytest.importorskip("rfdetr")
sv = pytest.importorskip("supervision")

from vlm_node.rfdetr_service import (  # noqa: E402
    CameraPreprocessProfile,
    RFDETRPerceptionEngine,
    _annotate_cam4,
    _camera_checkpoint_provenance,
    _decode_image,
    _normalize_camera_rotation,
    _parse_camera_roi,
    _prepare_camera_frame,
    _restore_camera_detections,
    _validate_camera_threshold,
)
from vlm_node.rfdetr_contract import CAM4_CLASS_NAMES  # noqa: E402


def _detections(box: list[float]):
    return sv.Detections(
        xyxy=np.asarray([box], dtype=float),
        confidence=np.asarray([0.91], dtype=float),
        class_id=np.asarray([2], dtype=int),
    )


def test_roi_and_rotation_fail_closed() -> None:
    assert _parse_camera_roi("250,170,980,650", label="CAM3 ROI") == (
        250,
        170,
        980,
        650,
    )
    for invalid in ("1,2,3", "-1,0,20,30", "20,0,10,30", "x,0,10,30"):
        with pytest.raises(ValueError):
            _parse_camera_roi(invalid, label="camera ROI")
    with pytest.raises(ValueError):
        _normalize_camera_rotation(45, label="camera rotation")
    with pytest.raises(ValueError):
        _validate_camera_threshold(0.0, label="camera threshold")


def test_cam3_crop_maps_model_box_back_to_source_pixels() -> None:
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    model_input, transform = _prepare_camera_frame(
        frame,
        CameraPreprocessProfile(roi_xyxy=(250, 170, 980, 650)),
    )

    assert model_input.shape == (480, 730, 3)
    restored = _restore_camera_detections(
        _detections([139.0, 54.0, 267.0, 159.0]),
        transform,
    )
    assert restored.xyxy.tolist() == [[389.0, 224.0, 517.0, 329.0]]
    assert restored.confidence.tolist() == [0.91]
    assert restored.class_id.tolist() == [2]


def test_cam4_crop_and_180_rotation_restore_original_bbox() -> None:
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    model_input, transform = _prepare_camera_frame(
        frame,
        CameraPreprocessProfile(
            roi_xyxy=(800, 300, 1280, 720),
            rotation_deg=180,
        ),
    )

    assert model_input.shape == (420, 480, 3)
    restored = _restore_camera_detections(
        _detections([219.0, 139.0, 314.0, 208.0]),
        transform,
    )
    assert restored.xyxy.tolist() == [[966.0, 512.0, 1061.0, 581.0]]


def test_roi_must_fit_actual_source_frame() -> None:
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    with pytest.raises(ValueError, match="exceeds the source frame"):
        _prepare_camera_frame(
            frame,
            CameraPreprocessProfile(roi_xyxy=(250, 170, 980, 650)),
        )


def _reviewed_checkpoint(tmp_path, *, class_names: list[str]):
    run_dir = tmp_path / "three_class_run"
    run_dir.mkdir()
    checkpoint = run_dir / "checkpoint_best_regular.pth"
    checkpoint.write_bytes(b"reviewed-three-class-checkpoint")
    digest = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
    metadata = {
        "class_names": class_names,
        "preferred_checkpoint": f"/workspace/{run_dir.name}/{checkpoint.name}",
        "preferred_checkpoint_sha256": digest,
    }
    (run_dir / "run_metadata.json").write_text(
        json.dumps(metadata),
        encoding="utf-8",
    )
    return checkpoint, metadata


def test_camera_checkpoint_requires_exact_three_class_abi_and_digest(
    tmp_path,
) -> None:
    checkpoint, metadata = _reviewed_checkpoint(
        tmp_path,
        class_names=list(CAM4_CLASS_NAMES),
    )

    provenance = _camera_checkpoint_provenance(checkpoint)
    assert provenance["class_names"] == ["Adson", "Bovie", "Bipolar"]
    assert provenance["sha256"] == metadata["preferred_checkpoint_sha256"]

    metadata_path = checkpoint.parent / "run_metadata.json"
    for incompatible in (
        ["Adson", "Bipolar", "Bovie"],
        ["Adson", "Allis", "Bovie", "Bipolar", "Mosquito", "Hand_request"],
    ):
        metadata["class_names"] = incompatible
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        with pytest.raises(ValueError, match="class ABI is incompatible"):
            _camera_checkpoint_provenance(checkpoint)

    metadata["class_names"] = list(CAM4_CLASS_NAMES)
    metadata["preferred_checkpoint_sha256"] = "0" * 64
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(ValueError, match="digest does not match"):
        _camera_checkpoint_provenance(checkpoint)


class _CapturingModel:
    def __init__(self) -> None:
        self.inputs: list[np.ndarray] = []

    def predict(self, frame: np.ndarray, *, threshold: float):
        assert threshold > 0.0
        self.inputs.append(frame.copy())
        return sv.Detections.empty()


def test_opencv_bgr_is_rgb_only_at_model_boundary_and_render_stays_bgr() -> None:
    source_bgr = np.asarray(
        [
            [[11, 22, 33], [41, 52, 63]],
            [[71, 82, 93], [101, 112, 123]],
        ],
        dtype=np.uint8,
    )
    ok, encoded = cv2.imencode(".png", source_bgr)
    assert ok
    decoded_bgr = _decode_image(
        base64.b64encode(encoded.tobytes()).decode("ascii"),
        label="test",
    )
    original_bgr = decoded_bgr.copy()
    expected_rgb = np.ascontiguousarray(decoded_bgr[:, :, ::-1])

    camera_model = _CapturingModel()
    engine = RFDETRPerceptionEngine.__new__(RFDETRPerceptionEngine)
    engine._camera_inference_lock = threading.Lock()
    engine._camera_model = camera_model
    engine._camera_stream = None
    engine._camera_profiles = {
        "cam_3": CameraPreprocessProfile(),
        "cam_4": CameraPreprocessProfile(),
    }
    engine._predict_camera_views(cam4=decoded_bgr, cam3=None)

    flir_model = _CapturingModel()
    engine._predict(
        flir_model,
        decoded_bgr,
        threshold=0.1,
        stream=None,
    )

    for model in (camera_model, flir_model):
        assert len(model.inputs) == 1
        assert model.inputs[0].flags.c_contiguous
        assert np.array_equal(model.inputs[0], expected_rgb)

    # Inference conversion must not mutate or replace the BGR source used by
    # OpenCV annotation/encoding. With no boxes the renderer is pixel-exact.
    assert np.array_equal(decoded_bgr, original_bgr)
    rendered_bgr = _annotate_cam4(decoded_bgr, sv.Detections.empty())
    assert np.array_equal(rendered_bgr, original_bgr)


def test_camera_response_preserves_source_stamps_and_model_version() -> None:
    frame = np.zeros((12, 16, 3), dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", frame)
    assert ok
    image_base64 = base64.b64encode(encoded.tobytes()).decode("ascii")

    engine = RFDETRPerceptionEngine.__new__(RFDETRPerceptionEngine)
    engine._inference_pool = ThreadPoolExecutor(max_workers=1)
    engine._render_pool = ThreadPoolExecutor(max_workers=2)
    engine._jpeg_quality = 90
    engine._camera_stream = None
    engine._flir_stream = None
    engine._camera_checkpoint_provenance = {
        "training_run": "mayo_bbox_3class_strong_inventory6_20260824",
        "sha256": "b78d759b" * 8,
    }
    engine._camera_model_version = (
        "rfdetr-mayo-bbox-3class-20260824-b78d759bdba2"
    )
    engine._camera_ontology_version = "mayo-bbox-3class-v1"
    engine._request_count = 0
    engine._last_latency_ms = 0.0
    detected = _detections([2.0, 3.0, 8.0, 9.0])
    engine._predict_camera_views = lambda **_kwargs: (
        detected,
        1.25,
        detected,
        1.5,
    )

    try:
        result = engine.perceive(
            {
                "cam3_image_base64": image_base64,
                "cam3_stamp_sec": 123.4567894,
                "cam4_image_base64": image_base64,
                "cam4_stamp_sec": 987.6543214,
                "include_cam3_annotated_image": False,
                "include_cam4_annotated_image": False,
            }
        )
    finally:
        engine._inference_pool.shutdown(wait=True)
        engine._render_pool.shutdown(wait=True)

    expected_version = "rfdetr-mayo-bbox-3class-20260824-b78d759bdba2"
    assert result["diagnostics"]["cam3"]["source_stamp_sec"] == 123.456789
    assert result["diagnostics"]["cam4"]["source_stamp_sec"] == 987.654321
    assert result["diagnostics"]["cam3"]["model_version"] == expected_version
    assert result["diagnostics"]["cam4"]["model_version"] == expected_version
    assert result["cam3_overlay_image"]["source_stamp_sec"] == 123.456789
    assert result["cam4_overlay_image"]["source_stamp_sec"] == 987.654321
