from __future__ import annotations

import json

import cv2
import numpy as np
import pytest
from conftest import EmptyAdapter, metadata
from fastapi.testclient import TestClient
from pnu_perception_worker.app import create_app
from pnu_perception_worker.contract import parse_metadata
from pnu_perception_worker.depth import (
    InvalidDepthError,
    decode_compressed_depth_16uc1,
    qualify_aligned_depth,
)
from pnu_perception_worker.engine import PerceptionEngine


def _camera_info(payload: dict, *, width: int = 32, height: int = 24) -> dict:
    stamp = payload["source"]["rgb"]["stamp_ns"]
    frame = payload["source"]["rgb"]["frame_id"]
    return {
        "stamp_ns": stamp,
        "frame_id": frame,
        "width": width,
        "height": height,
        "distortion_model": "plumb_bob",
        "d": [0.0] * 5,
        "k": [100.0, 0.0, 16.0, 0.0, 100.0, 12.0, 0.0, 0.0, 1.0],
        "r": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
        "p": [
            100.0,
            0.0,
            16.0,
            0.0,
            0.0,
            100.0,
            12.0,
            0.0,
            0.0,
            0.0,
            1.0,
            0.0,
        ],
    }


def aligned_metadata(*, algorithms: list[str] | None = None) -> dict:
    payload = metadata(algorithms=algorithms, depth=True)
    rgb = payload["source"]["rgb"]
    payload["source"]["depth"].update(frame_id=rgb["frame_id"], aligned=True)
    payload["color_camera_info"] = _camera_info(payload)
    payload["depth_camera_info"] = _camera_info(payload)
    payload["alignment"] = {"validated": True, "id": "viplab-cam4-align-v1"}
    payload["depth_scale_m_per_unit"] = 0.001
    payload["depth_scale_validated"] = True
    return payload


def compressed_depth(depth: np.ndarray) -> bytes:
    ok, encoded = cv2.imencode(".png", np.asarray(depth))
    assert ok
    return b"\x00" * 12 + encoded.tobytes()


def _parsed(payload: dict):
    return parse_metadata(json.dumps(payload).encode(), depth_present=True)


def test_aligned_16uc1_depth_decodes_to_filtered_metric_map(config) -> None:
    payload = aligned_metadata()
    raw = np.full((24, 32), 875, dtype=np.uint16)
    raw[0, 0] = 0
    context = qualify_aligned_depth(
        _parsed(payload),
        compressed_depth(raw),
        rgb_width=32,
        rgb_height=24,
        config=config,
    )
    assert context.public_gate() == {"ready": True, "reasons": []}
    assert context.depth_m is not None
    assert context.depth_m.dtype == np.float32
    assert context.depth_m[4, 4] == pytest.approx(0.875)
    assert context.depth_m[0, 0] == 0.0
    assert context.valid_pixels == 24 * 32 - 1


@pytest.mark.parametrize(
    "payload",
    [
        b"not-a-png",
        b"\x00" * 12
        + cv2.imencode(".png", np.zeros((24, 32), dtype=np.uint8))[1].tobytes(),
    ],
)
def test_malformed_or_non_16bit_compressed_depth_is_rejected(
    config, payload: bytes
) -> None:
    with pytest.raises(InvalidDepthError):
        qualify_aligned_depth(
            _parsed(aligned_metadata()),
            payload,
            rgb_width=32,
            rgb_height=24,
            config=config,
        )


def test_png_dimensions_are_bounded_before_opencv_allocation() -> None:
    encoded = bytearray(compressed_depth(np.ones((2, 2), dtype=np.uint16)))
    signature = encoded.index(b"\x89PNG\r\n\x1a\n")
    encoded[signature + 16 : signature + 24] = (100_000).to_bytes(4, "big") * 2
    with pytest.raises(InvalidDepthError, match="dimensions exceed"):
        decode_compressed_depth_16uc1(
            bytes(encoded),
            "16UC1; compressedDepth png",
            max_pixels=4_194_304,
        )


def test_dimension_mismatch_is_safe_2d_fallback(config) -> None:
    context = qualify_aligned_depth(
        _parsed(aligned_metadata()),
        compressed_depth(np.full((12, 16), 800, dtype=np.uint16)),
        rgb_width=32,
        rgb_height=24,
        config=config,
    )
    assert context.decoded is True
    assert context.input_ready is False
    assert context.depth_m is None
    assert "depth_rgb_dimension_mismatch" in context.reasons


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (
            lambda payload: payload["alignment"].update(id=""),
            "rgb_depth_alignment_id_missing",
        ),
        (
            lambda payload: payload["source"]["depth"].update(
                frame_id="cam_4_depth_optical_frame"
            ),
            "depth_frame_is_not_rgb_frame",
        ),
        (
            lambda payload: payload["depth_camera_info"].update(
                frame_id="cam_4_depth_optical_frame"
            ),
            "depth_camera_info_frame_mismatch",
        ),
    ],
)
def test_alignment_evidence_mismatch_has_stable_fail_closed_reason(
    config, mutate, reason: str
) -> None:
    payload = aligned_metadata()
    mutate(payload)
    context = qualify_aligned_depth(
        _parsed(payload),
        compressed_depth(np.full((24, 32), 800, dtype=np.uint16)),
        rgb_width=32,
        rgb_height=24,
        config=config,
    )
    assert context.input_ready is False
    assert reason in context.reasons


def test_native_or_unvalidated_depth_remains_a_valid_2d_input(
    config, compressed_depth_bytes
) -> None:
    request = _parsed(metadata(depth=True))
    context = qualify_aligned_depth(
        request,
        compressed_depth_bytes,
        rgb_width=32,
        rgb_height=24,
        config=config,
    )
    assert context.input_ready is False
    assert "depth_not_declared_rgb_aligned" in context.reasons
    assert "rgb_depth_alignment_unvalidated" in context.reasons
    assert context.depth_m is None


def test_all_zero_aligned_depth_fails_metric_gate_with_stable_reason(config) -> None:
    context = qualify_aligned_depth(
        _parsed(aligned_metadata()),
        compressed_depth(np.zeros((24, 32), dtype=np.uint16)),
        rgb_width=32,
        rgb_height=24,
        config=config,
    )
    assert context.input_ready is False
    assert context.has_valid_samples is False
    assert context.reasons == ("depth_has_no_valid_samples",)
    assert context.valid_pixels == 0
    assert np.count_nonzero(context.depth_m) == 0


def test_engine_all_zero_depth_is_explicit_2d_fallback(config, jpeg_bytes) -> None:
    class ZeroDepthAwareAdapter(EmptyAdapter):
        def infer(self, frame_bgr, request, depth):
            assert depth.input_ready is False
            assert depth.reasons == ("depth_has_no_valid_samples",)
            return {"schema": "pnu.blood.2d.v1", "detections": []}

    engine = PerceptionEngine(
        config,
        upstream_revision="0" * 40,
        adapters={"blood": ZeroDepthAwareAdapter("blood")},
    )
    response = engine.infer(
        _parsed(aligned_metadata(algorithms=["blood"])),
        jpeg_bytes,
        compressed_depth(np.zeros((24, 32), dtype=np.uint16)),
    )
    assert response["metric_3d"] == {
        "ready": False,
        "reasons": ["depth_has_no_valid_samples"],
    }
    assert response["results"]["blood"]["schema"] == "pnu.blood.2d.v1"
    assert response["depth_evidence"]["valid_pixels"] == 0


def test_engine_passes_valid_metric_depth_to_each_requested_adapter(
    config, jpeg_bytes
) -> None:
    class DepthAwareAdapter(EmptyAdapter):
        def infer(self, frame_bgr, request, depth):
            assert depth.input_ready is True
            assert depth.depth_m[2, 2] == pytest.approx(0.8)
            return {
                "schema": "pnu.blood.rgbd.v1",
                "detections": [],
                "metric_3d": {"ready": True, "status": "no_detections", "reasons": []},
            }

    engine = PerceptionEngine(
        config,
        upstream_revision="0" * 40,
        adapters={"blood": DepthAwareAdapter("blood")},
    )
    payload = aligned_metadata(algorithms=["blood"])
    response = engine.infer(
        _parsed(payload),
        jpeg_bytes,
        compressed_depth(np.full((24, 32), 800, dtype=np.uint16)),
    )
    assert response["metric_3d"] == {"ready": True, "reasons": []}
    assert response["depth_evidence"] == {
        "received": True,
        "decoded": True,
        "alignment_validated": True,
        "alignment_id": "viplab-cam4-align-v1",
        "rgb_frame_id": "cam_4_color_optical_frame",
        "depth_frame_id": "cam_4_color_optical_frame",
        "rgb_shape_hw": [24, 32],
        "depth_shape_hw": [24, 32],
        "depth_scale_m_per_unit": 0.001,
        "depth_scale_validated": True,
        "valid_pixels": 24 * 32,
        "valid_ratio": 1.0,
    }
    assert response["results"]["blood"]["schema"] == "pnu.blood.rgbd.v1"
    assert response["depth_received"] is True
    assert "depth_decoded" not in response
    assert set(response["latency_ms"]) == {"decode", "blood", "total"}


def test_api_rejects_malformed_compressed_depth_as_invalid_depth(
    ready_engine, jpeg_bytes
) -> None:
    payload = aligned_metadata(algorithms=["hand"])
    files = {
        "metadata": (
            "metadata.json",
            json.dumps(payload).encode(),
            "application/json",
        ),
        "rgb": ("frame.jpg", jpeg_bytes, "image/jpeg"),
        "depth": ("depth.bin", b"corrupt", "application/octet-stream"),
    }
    response = TestClient(create_app(ready_engine)).post("/v1/infer", files=files)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_depth"
