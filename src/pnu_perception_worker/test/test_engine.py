from __future__ import annotations

import json
import struct

import pytest
from conftest import EmptyAdapter, metadata
from pnu_perception_worker.adapters import AdapterOutputError, AdapterRequestError
from pnu_perception_worker.contract import parse_metadata
from pnu_perception_worker.engine import (
    InvalidImageError,
    ModelsUnavailableError,
    WorkerBusyError,
)


def _request(algorithms: list[str] | None = None):
    payload = metadata(algorithms=algorithms)
    return parse_metadata(json.dumps(payload).encode(), depth_present=False)


def test_empty_detection_is_success_only_after_execution(
    ready_engine, jpeg_bytes
) -> None:
    response = ready_engine.infer(_request(), jpeg_bytes, None)
    assert response["schema"] == "taskplanner.pnu_perception.response.v1"
    assert response["source"] == metadata()["source"]
    assert response["accepted_algorithms"] == ["tool", "blood", "hand"]
    for name in ("tool", "blood", "hand"):
        assert response["models"][name]["ready"] is True
        assert response["models"][name]["executed"] is True
        assert response["models"][name]["status"] == "executed"
        assert response["results"][name]["executed"] is True
    assert response["results"]["tool"]["detections"] == []
    assert response["results"]["blood"]["detections"] == []
    assert response["results"]["hand"]["hands"] == []
    assert response["metric_3d"]["ready"] is False
    assert response["metric_3d"]["reasons"] == ["depth_missing"]
    assert response["depth_evidence"] == {
        "received": False,
        "decoded": False,
        "alignment_validated": False,
        "alignment_id": "",
        "rgb_frame_id": "cam_4_color_optical_frame",
        "depth_frame_id": "",
        "rgb_shape_hw": [24, 32],
        "depth_shape_hw": None,
        "depth_scale_m_per_unit": 0.0,
        "depth_scale_validated": False,
        "valid_pixels": 0,
        "valid_ratio": 0.0,
    }


def test_subset_executes_only_requested_blood_and_hand(
    ready_engine, jpeg_bytes
) -> None:
    response = ready_engine.infer(_request(["blood", "hand"]), jpeg_bytes, None)
    assert response["accepted_algorithms"] == ["blood", "hand"]
    assert list(response["results"]) == ["blood", "hand"]
    assert set(response["latency_ms"]) == {"decode", "blood", "hand", "total"}
    assert response["models"]["blood"]["executed"] is True
    assert response["models"]["hand"]["executed"] is True
    assert response["models"]["tool"]["executed"] is False
    assert response["models"]["tool"]["status"] == "loaded"
    assert ready_engine.adapters["tool"].calls == 0


def test_unavailable_model_is_not_faked_as_empty(ready_engine, jpeg_bytes) -> None:
    ready_engine.adapters.pop("blood")
    ready_engine.load_errors["blood"] = "checkpoint missing"
    with pytest.raises(ModelsUnavailableError) as caught:
        ready_engine.infer(_request(["blood"]), jpeg_bytes, None)
    assert caught.value.unavailable == ["blood"]
    assert ready_engine.model_records()["blood"]["status"] == "unavailable"


def test_zero_depth_queue_rejects_concurrent_frame(ready_engine, jpeg_bytes) -> None:
    assert ready_engine._inference_lock.acquire(blocking=False)
    try:
        with pytest.raises(WorkerBusyError, match="queue_depth=0"):
            ready_engine.infer(_request(["tool"]), jpeg_bytes, None)
    finally:
        ready_engine._inference_lock.release()


def test_invalid_rgb_never_calls_models(ready_engine) -> None:
    with pytest.raises(InvalidImageError):
        ready_engine.infer(_request(["tool"]), b"not an image", None)
    assert ready_engine.adapters["tool"].calls == 0


def test_color_camera_info_must_match_decoded_rgb(ready_engine, jpeg_bytes) -> None:
    payload = metadata(algorithms=["tool"])
    payload["color_camera_info"] = {
        "stamp_ns": payload["source"]["rgb"]["stamp_ns"],
        "frame_id": "cam_4_color_optical_frame",
        "width": 1280,
        "height": 720,
        "distortion_model": "plumb_bob",
        "d": [0.0] * 5,
        "k": [1.0, 0.0, 640.0, 0.0, 1.0, 360.0, 0.0, 0.0, 1.0],
        "r": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
        "p": [1.0, 0.0, 640.0, 0.0, 0.0, 1.0, 360.0, 0.0, 0.0, 0.0, 1.0, 0.0],
    }
    request = parse_metadata(json.dumps(payload).encode(), depth_present=False)
    with pytest.raises(InvalidImageError, match="do not match"):
        ready_engine.infer(request, jpeg_bytes, None)


@pytest.mark.parametrize("container", ["png", "jpeg"])
def test_oversized_rgb_container_is_rejected_before_opencv_allocation(
    ready_engine, monkeypatch, container
) -> None:
    payload = metadata(algorithms=["blood"])
    if container == "png":
        payload["source"]["rgb"]["format"] = "png"
        rgb_bytes = (
            b"\x89PNG\r\n\x1a\n"
            + struct.pack(">I", 13)
            + b"IHDR"
            + struct.pack(">II", 65_535, 65_535)
            + bytes([8, 2, 0, 0, 0])
        )
    else:
        payload["source"]["rgb"]["format"] = "jpeg"
        rgb_bytes = (
            b"\xff\xd8\xff\xc0"
            + struct.pack(">H", 17)
            + bytes([8])
            + struct.pack(">HH", 65_535, 65_535)
            + bytes([3])
            + b"\x01\x11\x00\x02\x11\x00\x03\x11\x00"
        )
    request = parse_metadata(json.dumps(payload).encode(), depth_present=False)
    monkeypatch.setattr(
        "pnu_perception_worker.engine.cv2.imdecode",
        lambda *_args, **_kwargs: pytest.fail("OpenCV decode must not be called"),
    )
    with pytest.raises(InvalidImageError, match="container dimensions"):
        ready_engine.infer(request, rgb_bytes, None)


def test_runtime_model_failure_degrades_future_health(ready_engine, jpeg_bytes) -> None:
    class FailingAdapter(EmptyAdapter):
        def infer(self, frame_bgr, request):
            raise RuntimeError("sensitive implementation detail")

    ready_engine.adapters["blood"] = FailingAdapter("blood")
    with pytest.raises(RuntimeError, match="sensitive implementation detail"):
        ready_engine.infer(_request(["blood"]), jpeg_bytes, None)
    status = ready_engine.model_records()["blood"]
    assert status["ready"] is False
    assert status["status"] == "unavailable"
    assert status["error"] == "runtime RuntimeError: model execution failed"


def test_request_geometry_failure_does_not_permanently_unload_model(
    ready_engine, jpeg_bytes
) -> None:
    class RequestGeometryFailingAdapter(EmptyAdapter):
        def infer(self, frame_bgr, request):
            raise AdapterRequestError("per-frame geometry")

    adapter = RequestGeometryFailingAdapter("tool")
    ready_engine.adapters["tool"] = adapter
    with pytest.raises(AdapterRequestError, match="per-frame geometry"):
        ready_engine.infer(_request(["tool"]), jpeg_bytes, None)
    assert ready_engine.adapters["tool"] is adapter
    status = ready_engine.model_records()["tool"]
    assert status["ready"] is True
    assert status["status"] == "loaded"


def test_bounded_frame_output_failure_does_not_permanently_unload_model(
    ready_engine, jpeg_bytes
) -> None:
    class OneBadFrameAdapter(EmptyAdapter):
        def infer(self, frame_bgr, request):
            self.calls += 1
            if self.calls == 1:
                raise AdapterOutputError("one frame exceeded its RLE budget")
            return {"schema": "pnu.blood.2d.v1", "detections": []}

    adapter = OneBadFrameAdapter("blood")
    ready_engine.adapters["blood"] = adapter
    with pytest.raises(AdapterOutputError, match="one frame"):
        ready_engine.infer(_request(["blood"]), jpeg_bytes, None)

    assert ready_engine.adapters["blood"] is adapter
    status = ready_engine.model_records()["blood"]
    assert status["ready"] is True
    assert status["status"] == "loaded"

    response = ready_engine.infer(_request(["blood"]), jpeg_bytes, None)
    assert response["results"]["blood"]["detections"] == []
    assert response["models"]["blood"]["executed"] is True
