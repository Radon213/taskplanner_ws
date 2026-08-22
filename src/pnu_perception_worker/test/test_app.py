from __future__ import annotations

import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from conftest import EmptyAdapter, metadata
from fastapi.testclient import TestClient
from pnu_perception_worker.adapters import AdapterOutputError, AdapterRequestError
from pnu_perception_worker.app import create_app
from pnu_perception_worker.engine import PerceptionEngine
from starlette.requests import Request


def _files(jpeg_bytes: bytes, payload: dict | None = None):
    return {
        "metadata": (
            "metadata.json",
            json.dumps(payload or metadata()).encode(),
            "application/json",
        ),
        "rgb": ("frame.jpg", jpeg_bytes, "image/jpeg"),
    }


def test_request_geometry_error_is_sanitized_without_degrading_model(
    ready_engine, jpeg_bytes
) -> None:
    class RequestGeometryFailingAdapter(EmptyAdapter):
        def infer(self, frame_bgr, request):
            raise AdapterRequestError("sensitive OpenCV detail")

    adapter = RequestGeometryFailingAdapter("tool")
    ready_engine.adapters["tool"] = adapter
    response = TestClient(create_app(ready_engine)).post(
        "/v1/infer", files=_files(jpeg_bytes, metadata(algorithms=["tool"]))
    )
    assert response.status_code == 422
    assert response.json()["error"] == {
        "code": "invalid_perception_geometry",
        "message": "request calibration or per-frame geometry is invalid",
    }
    assert ready_engine.adapters["tool"] is adapter


def test_bounded_frame_output_error_is_sanitized_and_next_frame_recovers(
    ready_engine, jpeg_bytes
) -> None:
    class OneBadFrameAdapter(EmptyAdapter):
        def infer(self, frame_bgr, request):
            self.calls += 1
            if self.calls == 1:
                raise AdapterOutputError("sensitive mask details")
            return {"schema": "pnu.blood.2d.v1", "detections": []}

    adapter = OneBadFrameAdapter("blood")
    ready_engine.adapters["blood"] = adapter
    client = TestClient(create_app(ready_engine))
    files = _files(jpeg_bytes, metadata(algorithms=["blood"]))

    rejected = client.post("/v1/infer", files=files)
    assert rejected.status_code == 422
    assert rejected.json()["error"] == {
        "code": "invalid_perception_output",
        "message": "per-frame perception output exceeds reviewed limits",
    }
    assert ready_engine.adapters["blood"] is adapter
    assert ready_engine.model_records()["blood"]["ready"] is True

    recovered = client.post(
        "/v1/infer", files=_files(jpeg_bytes, metadata(algorithms=["blood"]))
    )
    assert recovered.status_code == 200
    assert recovered.json()["results"]["blood"]["detections"] == []


def test_health_and_capabilities_are_stable_and_binary_only(ready_engine) -> None:
    client = TestClient(create_app(ready_engine))
    health = client.get("/v1/health")
    assert health.status_code == 200
    assert health.json()["schema"] == "taskplanner.pnu_perception.health.v1"
    assert health.json()["ready"] is True
    capabilities = client.get("/v1/capabilities")
    assert capabilities.status_code == 200
    payload = capabilities.json()
    assert payload["schema"] == "taskplanner.pnu_perception.capabilities.v1"
    assert payload["transport"]["base64_allowed"] is False
    assert payload["execution"] == {
        "latest_frame_only": True,
        "max_in_flight": 1,
        "queue_depth": 0,
        "overload_status": 429,
    }
    assert payload["limits"]["response_json_bytes"] == 16 * 1024 * 1024
    assert payload["limits"]["total_rle_counts_per_algorithm"] == 1_000_000
    assert payload["metric_3d"] == {
        "enabled": True,
        "reason": "enabled_for_validated_rgb_aligned_depth",
        "required_gates": [
            "registered_or_alignment_validated_depth",
            "alignment_validated_with_nonempty_id",
            "matching_rgb_frame_and_dimensions",
            "color_camera_info",
            "matching_color_and_depth_camera_info",
            "validated_depth_scale",
        ],
    }


def test_multipart_inference_preserves_source_and_marks_execution(
    ready_engine, jpeg_bytes
) -> None:
    request_payload = metadata(algorithms=["tool"])
    response = TestClient(create_app(ready_engine)).post(
        "/v1/infer", files=_files(jpeg_bytes, request_payload)
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["source"] == request_payload["source"]
    assert payload["results"]["tool"]["executed"] is True
    assert payload["results"]["tool"]["detections"] == []
    assert payload["models"]["tool"]["digest_sha256"] == "t" * 64


def test_native_depth_and_both_camera_infos_cross_binary_boundary_without_3d_claim(
    ready_engine, jpeg_bytes, compressed_depth_bytes
) -> None:
    request_payload = metadata(algorithms=["hand"], depth=True)
    identity_r = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
    projection = [1.0, 0.0, 16.0, 0.0, 0.0, 1.0, 12.0, 0.0, 0.0, 0.0, 1.0, 0.0]
    for key, source_key in (
        ("color_camera_info", "rgb"),
        ("depth_camera_info", "depth"),
    ):
        request_payload[key] = {
            "stamp_ns": request_payload["source"][source_key]["stamp_ns"],
            "frame_id": request_payload["source"][source_key]["frame_id"],
            "width": 32,
            "height": 24,
            "distortion_model": "plumb_bob",
            "d": [0.0] * 5,
            "k": [1.0, 0.0, 16.0, 0.0, 1.0, 12.0, 0.0, 0.0, 1.0],
            "r": identity_r,
            "p": projection,
        }
    request_payload["depth_scale_m_per_unit"] = 0.001
    request_payload["depth_scale_validated"] = True
    files = {
        **_files(jpeg_bytes, request_payload),
        "depth": (
            "depth.bin",
            compressed_depth_bytes,
            "application/octet-stream",
        ),
    }
    response = TestClient(create_app(ready_engine)).post("/v1/infer", files=files)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["source"] == request_payload["source"]
    assert payload["depth_received"] is True
    assert payload["metric_3d"]["ready"] is False
    assert "depth_not_declared_rgb_aligned" in payload["metric_3d"]["reasons"]
    assert "rgb_depth_alignment_unvalidated" in payload["metric_3d"]["reasons"]


def test_missing_requested_model_returns_503_not_empty(config, jpeg_bytes) -> None:
    engine = PerceptionEngine(
        config,
        upstream_revision="0" * 40,
        adapters={"tool": EmptyAdapter("tool")},
        load_errors={"blood": "checkpoint missing", "hand": "asset missing"},
    )
    response = TestClient(create_app(engine)).post(
        "/v1/infer", files=_files(jpeg_bytes, metadata(algorithms=["blood"]))
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "models_unavailable"
    assert response.json()["error"]["unavailable"] == ["blood"]


def test_missing_tool_keeps_global_health_degraded_but_allows_blood_hand_subset(
    config, jpeg_bytes
) -> None:
    engine = PerceptionEngine(
        config,
        upstream_revision="0" * 40,
        adapters={
            "blood": EmptyAdapter("blood"),
            "hand": EmptyAdapter("hand"),
        },
        load_errors={"tool": "checkpoint access pending"},
    )
    client = TestClient(create_app(engine))
    health = client.get("/v1/health")
    assert health.status_code == 200
    assert health.json()["ready"] is False
    assert health.json()["models"]["tool"]["status"] == "unavailable"

    response = client.post(
        "/v1/infer",
        files=_files(jpeg_bytes, metadata(algorithms=["blood", "hand"])),
    )
    assert response.status_code == 200, response.text
    assert response.json()["accepted_algorithms"] == ["blood", "hand"]
    assert set(response.json()["results"]) == {"blood", "hand"}


def test_bearer_token_is_compared_without_echo(ready_engine, jpeg_bytes) -> None:
    client = TestClient(create_app(ready_engine, api_token="secret-value"))
    denied_capabilities = client.get("/v1/capabilities")
    assert denied_capabilities.status_code == 401
    accepted_capabilities = client.get(
        "/v1/capabilities",
        headers={"Authorization": "Bearer secret-value"},
    )
    assert accepted_capabilities.status_code == 200
    denied = client.post("/v1/infer", files=_files(jpeg_bytes))
    assert denied.status_code == 401
    assert "secret-value" not in denied.text
    accepted = client.post(
        "/v1/infer",
        files=_files(jpeg_bytes),
        headers={"Authorization": "Bearer secret-value"},
    )
    assert accepted.status_code == 200


def test_request_size_is_rejected_before_multipart_parse(config, jpeg_bytes) -> None:
    tiny_config = replace(config, max_request_bytes=64)
    engine = PerceptionEngine(
        tiny_config,
        upstream_revision="0" * 40,
        adapters={name: EmptyAdapter(name) for name in ("tool", "blood", "hand")},
    )
    response = TestClient(create_app(engine)).post(
        "/v1/infer", files=_files(jpeg_bytes)
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"


def test_response_size_is_enforced_before_success_serialization(
    config, jpeg_bytes
) -> None:
    tiny_config = replace(config, max_response_json_bytes=128)
    engine = PerceptionEngine(
        tiny_config,
        upstream_revision="0" * 40,
        adapters={name: EmptyAdapter(name) for name in ("tool", "blood", "hand")},
    )
    response = TestClient(create_app(engine)).post(
        "/v1/infer", files=_files(jpeg_bytes)
    )
    assert response.status_code == 507
    assert response.json()["error"]["code"] == "response_too_large"


def test_slow_multipart_ingress_releases_slot_at_absolute_deadline(
    config, jpeg_bytes, monkeypatch
) -> None:
    tiny_config = replace(config, max_ingress_read_sec=0.05)
    engine = PerceptionEngine(
        tiny_config,
        upstream_revision="0" * 40,
        adapters={name: EmptyAdapter(name) for name in ("tool", "blood", "hand")},
    )
    original_form = Request.form

    async def slow_form(self, **kwargs):
        await asyncio.sleep(1.0)
        return await original_form(self, **kwargs)

    monkeypatch.setattr(Request, "form", slow_form)
    client = TestClient(create_app(engine))
    started = time.monotonic()
    response = client.post("/v1/infer", files=_files(jpeg_bytes))
    assert response.status_code == 408
    assert response.json()["error"]["code"] == "ingress_deadline_exceeded"
    assert time.monotonic() - started < 0.5


def test_expired_deadline_is_contract_error(ready_engine, jpeg_bytes) -> None:
    payload = metadata(deadline_offset_ms=-1)
    response = TestClient(create_app(ready_engine)).post(
        "/v1/infer", files=_files(jpeg_bytes, payload)
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "contract_error"


def test_wrong_media_type_and_unknown_field_are_rejected(
    ready_engine, jpeg_bytes
) -> None:
    client = TestClient(create_app(ready_engine))
    wrong_type = client.post(
        "/v1/infer",
        files={
            "metadata": ("metadata.json", json.dumps(metadata()), "text/plain"),
            "rgb": ("frame.jpg", jpeg_bytes, "image/jpeg"),
        },
    )
    assert wrong_type.status_code == 422
    unknown = client.post(
        "/v1/infer",
        files={
            **_files(jpeg_bytes),
            "overlay": ("x.bin", b"x", "application/octet-stream"),
        },
    )
    assert unknown.status_code == 422


def test_ingress_has_one_slot_and_no_hidden_request_queue(
    ready_engine, jpeg_bytes
) -> None:
    started = threading.Event()
    release = threading.Event()

    class BlockingAdapter(EmptyAdapter):
        def infer(self, frame_bgr, request):
            started.set()
            assert release.wait(timeout=2.0)
            return super().infer(frame_bgr, request)

    ready_engine.adapters["tool"] = BlockingAdapter("tool")
    app = create_app(ready_engine)
    first_client = TestClient(app)
    second_client = TestClient(app)
    request_files = _files(jpeg_bytes, metadata(algorithms=["tool"]))
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(first_client.post, "/v1/infer", files=request_files)
        assert started.wait(timeout=2.0)
        second = second_client.post(
            "/v1/infer",
            files=_files(jpeg_bytes, metadata(algorithms=["tool"])),
        )
        assert second.status_code == 429
        assert second.json()["error"]["code"] == "worker_busy"
        assert second.headers["Retry-After"] == "0"
        release.set()
        assert first.result(timeout=2.0).status_code == 200
