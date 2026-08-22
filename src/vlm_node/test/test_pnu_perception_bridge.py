from __future__ import annotations

import copy
import json
import math
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
import vlm_node.pnu_perception_bridge as bridge_module
from PIL import Image
from sensor_msgs.msg import CameraInfo
from surgical_perception_msgs.msg import ToolPose, ToolPoseArray
from vlm_node.pnu_perception_bridge import (
    CAPABILITIES_SCHEMA,
    EXPECTED_UPSTREAM_COMMIT,
    EXPECTED_UPSTREAM_REPOSITORY,
    REQUEST_SCHEMA,
    RESPONSE_SCHEMA,
    BinaryFrame,
    ContractError,
    PNUPerceptionBridgeNode,
    buffered_camera_info,
    build_blood_semantics,
    build_cam4_semantics,
    build_hand_keypoints_message,
    build_pnu_debug_overlay,
    build_pnu_pose_overlay,
    build_request_metadata,
    build_tool_ros_messages,
    closest_by_stamp,
    endpoint_is_loopback,
    has_exact_rgbd_frame_set,
    parse_expected_model_digests,
    resolve_endpoint_auth_mode,
    resolve_endpoint_transport_mode,
    validate_aligned_depth_contract,
    validate_capabilities,
    validate_service_url,
    validate_worker_health,
    validate_worker_response,
)
from vlm_node.rfdetr_contract import Cam4MayoPlacementTracker

from procedure_spec import load_bundle

DIGESTS = {
    "tool": "1" * 64,
    "blood": "2" * 64,
    "hand": "3" * 64,
}


def _frame(stamp_ns: int, *, depth: bool = False) -> BinaryFrame:
    return BinaryFrame(
        received_monotonic=time.monotonic(),
        stamp_ns=stamp_ns,
        frame_id=(
            "cam_4_depth_optical_frame" if depth else "cam_4_color_optical_frame"
        ),
        format=("16UC1; compressedDepth png" if depth else "jpeg"),
        data=(b"depth-png-wire-bytes" if depth else b"jpeg-wire-bytes"),
    )


def _real_frame(
    stamp_ns: int,
    *,
    depth: bool = False,
    width: int = 8,
    height: int = 6,
    frame_id: str = "cam_4_color_optical_frame",
) -> BinaryFrame:
    payload = BytesIO()
    if depth:
        Image.new("I;16", (width, height), 1000).save(payload, format="PNG")
        data = b"\x00" * 12 + payload.getvalue()
        image_format = "16UC1; compressedDepth png"
    else:
        Image.new("RGB", (width, height), (10, 20, 30)).save(payload, format="JPEG")
        data = payload.getvalue()
        image_format = "jpeg"
    return BinaryFrame(
        received_monotonic=time.monotonic(),
        stamp_ns=stamp_ns,
        frame_id=frame_id,
        format=image_format,
        data=data,
    )


def _camera_info(
    stamp_ns: int,
    *,
    width: int = 8,
    height: int = 6,
    frame_id: str = "cam_4_color_optical_frame",
):
    message = CameraInfo()
    message.header.stamp.sec = stamp_ns // 1_000_000_000
    message.header.stamp.nanosec = stamp_ns % 1_000_000_000
    message.header.frame_id = frame_id
    message.width = width
    message.height = height
    message.distortion_model = "plumb_bob"
    message.d = [0.0] * 5
    message.k = [100.0, 0.0, width / 2, 0.0, 100.0, height / 2, 0.0, 0.0, 1.0]
    message.r = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
    message.p = [
        100.0,
        0.0,
        width / 2,
        0.0,
        0.0,
        100.0,
        height / 2,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
    ]
    return buffered_camera_info(message)


def _pose_array(
    frame: BinaryFrame,
    *,
    position_m: tuple[float, float, float] | None = (0.0, 0.0, 0.8),
    orientation_xyzw: tuple[float, float, float, float] | None = None,
    class_name: str = "Scalpel",
) -> ToolPoseArray:
    array = ToolPoseArray()
    array.header.stamp.sec = frame.stamp_ns // 1_000_000_000
    array.header.stamp.nanosec = frame.stamp_ns % 1_000_000_000
    array.header.frame_id = frame.frame_id
    tool = ToolPose()
    tool.class_name = class_name
    tool.position_valid = position_m is not None
    tool.orientation_valid = orientation_xyzw is not None
    if position_m is not None:
        (
            tool.pose.position.x,
            tool.pose.position.y,
            tool.pose.position.z,
        ) = position_m
    if orientation_xyzw is not None:
        (
            tool.pose.orientation.x,
            tool.pose.orientation.y,
            tool.pose.orientation.z,
            tool.pose.orientation.w,
        ) = orientation_xyzw
    array.tools = [tool]
    return array


def _worker_loop_harness(*, max_rate_hz: float = 1000.0):
    node = PNUPerceptionBridgeNode.__new__(PNUPerceptionBridgeNode)
    node._condition = threading.Condition()
    node._pending_rgb = None
    node._depth_frames = deque(maxlen=32)
    node._color_infos = deque(maxlen=32)
    node._depth_infos = deque(maxlen=32)
    node._running = True
    node._enabled = True
    node._max_rate_hz = max_rate_hz
    node._last_request_started_monotonic = 0.0
    node._last_attempted_stamp_ns = 0
    node._max_depth_rgb_skew_sec = 0.05
    node._max_camera_info_skew_sec = 0.1
    captured = []
    processed = threading.Event()

    def _capture(*args):
        captured.append(args)
        with node._condition:
            node._running = False
            node._condition.notify_all()
        processed.set()

    node._process_frame = _capture
    worker = threading.Thread(target=node._worker_loop, daemon=True)
    worker.start()
    return node, worker, captured, processed


def test_exact_rgbd_frame_set_requires_all_four_same_stamp_inputs() -> None:
    stamp_ns = 10_000_000_000
    rgb = _real_frame(stamp_ns)
    depth = _real_frame(stamp_ns, depth=True)
    color_info = _camera_info(stamp_ns)
    depth_info = _camera_info(stamp_ns)

    assert has_exact_rgbd_frame_set(
        rgb_stamp_ns=rgb.stamp_ns,
        depth_frames=[depth],
        color_infos=[color_info],
        depth_infos=[depth_info],
    )
    assert not has_exact_rgbd_frame_set(
        rgb_stamp_ns=rgb.stamp_ns,
        depth_frames=[_real_frame(stamp_ns + 1, depth=True)],
        color_infos=[color_info],
        depth_infos=[depth_info],
    )


def test_worker_waits_boundedly_for_late_exact_stamp_aux_inputs(
    monkeypatch,
) -> None:
    stamp_ns = time.time_ns()
    node, worker, captured, processed = _worker_loop_harness()
    waiting_for_aux = threading.Event()
    original = bridge_module.has_exact_rgbd_frame_set

    def _observe_wait(**kwargs):
        ready = original(**kwargs)
        if not ready:
            waiting_for_aux.set()
        return ready

    monkeypatch.setattr(bridge_module, "has_exact_rgbd_frame_set", _observe_wait)
    monkeypatch.setattr(
        bridge_module,
        "_EXACT_RGBD_ORDERING_GRACE_SEC",
        0.1,
    )
    with node._condition:
        node._pending_rgb = _real_frame(stamp_ns)
        node._condition.notify_all()
    assert waiting_for_aux.wait(timeout=1.0)

    with node._condition:
        node._depth_frames.append(_real_frame(stamp_ns, depth=True))
        node._color_infos.append(_camera_info(stamp_ns))
        node._depth_infos.append(_camera_info(stamp_ns))
        node._condition.notify_all()

    assert processed.wait(timeout=1.0)
    worker.join(timeout=1.0)
    assert not worker.is_alive()
    assert len(captured) == 1
    rgb, depth, color_info, depth_info = captured[0]
    assert rgb.stamp_ns == stamp_ns
    assert depth.stamp_ns == stamp_ns
    assert color_info.stamp_ns == stamp_ns
    assert depth_info.stamp_ns == stamp_ns


def test_worker_sync_wait_is_bounded_and_falls_back_without_stale_aux(
    monkeypatch,
) -> None:
    stamp_ns = time.time_ns()
    node, worker, captured, processed = _worker_loop_harness()
    monkeypatch.setattr(
        bridge_module,
        "_EXACT_RGBD_ORDERING_GRACE_SEC",
        0.02,
    )
    started = time.monotonic()
    with node._condition:
        node._pending_rgb = _real_frame(stamp_ns)
        node._condition.notify_all()

    assert processed.wait(timeout=0.5)
    elapsed = time.monotonic() - started
    worker.join(timeout=1.0)
    assert 0.01 <= elapsed < 0.5
    assert len(captured) == 1
    rgb, depth, color_info, depth_info = captured[0]
    assert rgb.stamp_ns == stamp_ns
    assert depth is None
    assert color_info is None
    assert depth_info is None


def test_rate_gate_keeps_single_latest_rgb_until_dispatch(monkeypatch) -> None:
    first_stamp_ns = time.time_ns()
    second_stamp_ns = first_stamp_ns + 1
    node, worker, captured, processed = _worker_loop_harness(max_rate_hz=10.0)
    monkeypatch.setattr(
        bridge_module,
        "_EXACT_RGBD_ORDERING_GRACE_SEC",
        0.0,
    )
    node._last_request_started_monotonic = time.monotonic()
    with node._condition:
        node._pending_rgb = _real_frame(first_stamp_ns)
        node._condition.notify_all()
    time.sleep(0.01)
    with node._condition:
        node._pending_rgb = _real_frame(second_stamp_ns)
        node._condition.notify_all()

    assert processed.wait(timeout=1.0)
    worker.join(timeout=1.0)
    assert not worker.is_alive()
    assert len(captured) == 1
    assert captured[0][0].stamp_ns == second_stamp_ns


def test_camera_info_rejects_unmodeled_binning_or_roi() -> None:
    stamp_ns = time.time_ns()
    message = CameraInfo()
    message.header.stamp.sec = stamp_ns // 1_000_000_000
    message.header.stamp.nanosec = stamp_ns % 1_000_000_000
    message.header.frame_id = "cam_4_color_optical_frame"
    message.width = 8
    message.height = 6
    message.distortion_model = "plumb_bob"
    message.d = [0.0] * 5
    message.k = [1.0] * 9
    message.r = [1.0] * 9
    message.p = [1.0] * 12
    message.binning_x = 2
    with pytest.raises(ContractError, match="binning/ROI"):
        buffered_camera_info(message)

    message.binning_x = 0
    message.roi.width = 8
    with pytest.raises(ContractError, match="binning/ROI"):
        buffered_camera_info(message)


def _model_record(
    algorithm: str,
    *,
    executed: bool,
) -> dict[str, object]:
    return {
        "ready": True,
        "executed": executed,
        "status": "executed" if executed else "loaded",
        "version": f"{algorithm}-v1",
        "digest_sha256": DIGESTS[algorithm],
        "backend": "mediapipe" if algorithm == "hand" else "rfdetr",
        "error": None,
    }


def _models(
    *,
    executed: bool,
    executed_algorithms: set[str] | None = None,
    unavailable: set[str] | None = None,
) -> dict[str, dict[str, object]]:
    executed_algorithms = executed_algorithms or (
        {"tool", "blood", "hand"} if executed else set()
    )
    unavailable = unavailable or set()
    records = {}
    for algorithm in ("tool", "blood", "hand"):
        if algorithm in unavailable:
            records[algorithm] = {
                "ready": False,
                "executed": False,
                "status": "unavailable",
                "version": None,
                "digest_sha256": None,
                "backend": None,
                "error": "model artifact missing",
            }
        else:
            records[algorithm] = _model_record(
                algorithm,
                executed=algorithm in executed_algorithms,
            )
    return records


def _capabilities(*, auth_mode: str = "none") -> dict[str, object]:
    now_ms = int(time.time() * 1000)
    return {
        "schema": CAPABILITIES_SCHEMA,
        "generated_unix_ms": now_ms,
        "api_version": "v1",
        "request_schema": REQUEST_SCHEMA,
        "response_schema": RESPONSE_SCHEMA,
        "transport": {
            "content_type": "multipart/form-data",
            "fields": {
                "metadata": {
                    "required": True,
                    "content_type": "application/json",
                },
                "rgb": {
                    "required": True,
                    "content_types": ["image/jpeg", "image/png"],
                },
                "depth": {
                    "required": False,
                    "content_types": ["application/octet-stream", "image/png"],
                },
            },
            "base64_allowed": False,
        },
        "execution": {
            "latest_frame_only": True,
            "max_in_flight": 1,
            "queue_depth": 0,
            "overload_status": 429,
        },
        "limits": {
            "request_bytes": 32_000_000,
            "response_json_bytes": 16 * 1024 * 1024,
            "metadata_bytes": 262_144,
            "rgb_bytes": 8_000_000,
            "decoded_rgb_bytes": 16_000_000,
            "depth_bytes": 24_000_000,
            "image_pixels": 16_000_000,
            "detections_per_algorithm": 100,
            "total_rle_counts_per_algorithm": 1_000_000,
            "deadline_ahead_ms": 15_000,
            "rgb_depth_skew_ns": 50_000_000,
        },
        "algorithms": ["tool", "blood", "hand"],
        "models": _models(executed=False),
        "metric_3d": {
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
        },
        "auth": {"mode": auth_mode},
    }


def _health(now_ms: int) -> dict[str, object]:
    return {
        "schema": "taskplanner.pnu_perception.health.v1",
        "generated_unix_ms": now_ms,
        "status": "ready",
        "ready": True,
        "api_version": "v1",
        "upstream": {
            "repository": EXPECTED_UPSTREAM_REPOSITORY,
            "expected_commit": EXPECTED_UPSTREAM_COMMIT,
            "detected_commit": EXPECTED_UPSTREAM_COMMIT,
        },
        "models": _models(executed=False),
    }


def _metadata(
    *,
    now_ms: int,
    stamp_ns: int | None = None,
    algorithms: tuple[str, ...] = ("tool", "blood", "hand"),
) -> dict[str, object]:
    rgb = _frame(stamp_ns or now_ms * 1_000_000)
    depth = _frame(rgb.stamp_ns + 20_000_000, depth=True)
    return build_request_metadata(
        request_id="d7cb9505-38ae-4964-88e3-69c9dbdc1b2d",
        rgb=rgb,
        depth=depth,
        color_camera_info=None,
        depth_camera_info=None,
        requested_algorithms=algorithms,
        deadline_unix_ms=now_ms + 2_000,
    )


def _response(
    metadata: dict[str, object],
    *,
    generated_unix_ms: int,
    tool_detections: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    algorithms = list(metadata["requested_algorithms"])
    all_results: dict[str, object] = {
        "tool": {
            "schema": "pnu.tool.2d.v1",
            "executed": True,
            "image": {"width": 1280, "height": 720},
            "detections": tool_detections or [],
        },
        "blood": {
            "schema": "pnu.blood.2d.v1",
            "executed": True,
            "image": {"width": 1280, "height": 720},
            "detections": [],
        },
        "hand": {
            "schema": "pnu.hand.2d.v1",
            "executed": True,
            "image": {"width": 1280, "height": 720},
            "hands": [],
        },
    }
    return {
        "schema": RESPONSE_SCHEMA,
        "request_id": metadata["request_id"],
        "generated_unix_ms": generated_unix_ms,
        "source": metadata["source"],
        "accepted_algorithms": algorithms,
        "upstream": {
            "repository": EXPECTED_UPSTREAM_REPOSITORY,
            "commit": EXPECTED_UPSTREAM_COMMIT,
        },
        "models": _models(
            executed=True,
            executed_algorithms=set(algorithms),
        ),
        "latency_ms": {
            "decode": 1.0,
            **{
                algorithm: {"tool": 4.0, "blood": 5.0, "hand": 3.0}[algorithm]
                for algorithm in algorithms
            },
            "total": 13.0,
        },
        "results": {algorithm: all_results[algorithm] for algorithm in algorithms},
        "metric_3d": {
            "ready": False,
            "reasons": ["native_depth_not_color_aligned"],
        },
        "depth_received": "depth" in metadata["source"],
    }


def _support_plane_diagnostics(
    *,
    valid: bool = False,
    camera_info_sha256: str | None = None,
) -> dict[str, object]:
    if camera_info_sha256 is None:
        camera_info_sha256 = bridge_module._camera_info_sha256(
            _camera_info(1).payload
        )
    return {
        "schema": "pnu.tool.support_plane_diagnostics.v1",
        "validation_requested": True,
        "artifact_loaded": True,
        "static_reasons": [],
        "calibration_fit": {
            "available": True,
            "inlier_ratio": 0.95,
            "residual_p95_m": 0.004,
        },
        "runtime_validation": {
            "evaluated": True,
            "metrics_available": True,
            "valid": valid,
            "reasons": [] if valid else ["support_plane_runtime_inlier_ratio_low"],
            "sample_count": 12_345,
            "inlier_ratio": 0.96 if valid else 0.71,
            "residual_median_m": 0.002,
            "residual_p95_m": 0.008 if valid else 0.021,
            "camera_info_sha256": camera_info_sha256,
        },
    }


def _rgbd_fixture(now_ms: int):
    stamp_ns = now_ms * 1_000_000
    rgb = _real_frame(stamp_ns)
    depth = _real_frame(stamp_ns + 1_000_000, depth=True)
    color_info = _camera_info(stamp_ns)
    depth_info = _camera_info(stamp_ns + 1_000_000)
    metadata = build_request_metadata(
        request_id="rgbd-request-1",
        rgb=rgb,
        depth=depth,
        color_camera_info=color_info,
        depth_camera_info=depth_info,
        requested_algorithms=("tool", "blood", "hand"),
        deadline_unix_ms=now_ms + 2_000,
        depth_scale_m_per_unit=0.001,
        depth_scale_validated=True,
        depth_alignment_validated=True,
        depth_alignment_id="viplab-cam4-align-depth-to-color-v1",
    )
    payload = _response(metadata, generated_unix_ms=now_ms + 5)
    metric = {"ready": True, "status": "ready", "reasons": []}
    payload["results"]["tool"] = {
        "schema": "pnu.tool.rgbd.v1",
        "executed": True,
        "image": {"width": 8, "height": 6},
        "detections": [],
        "metric_3d": dict(metric),
        "ontology_version": "pnu.cam4.tool_ontology.v1",
        "calibration_version": "viplab-cam4-align-depth-to-color-v1",
        "pose_convention_version": "pnu.cam4.planar_tool_pose_convention.v2",
        "support_plane_config_version": "pnu-cam4-reference-plane-v1",
        "support_plane_validated": False,
        "support_plane_diagnostics": _support_plane_diagnostics(),
    }
    payload["results"]["blood"] = {
        "schema": "pnu.blood.rgbd.v1",
        "executed": True,
        "image": {"width": 8, "height": 6},
        "detections": [],
        "metric_3d": dict(metric),
        "combined_blood_centroid_xy_px": None,
        "combined_blood_centroid_depth_m": None,
    }
    payload["results"]["hand"] = {
        "schema": "pnu.hand.rgbd.v1",
        "executed": True,
        "image": {"width": 8, "height": 6},
        "hands": [],
        "metric_3d": dict(metric),
    }
    payload["metric_3d"] = {"ready": True, "reasons": []}
    payload["depth_evidence"] = {
        "received": True,
        "decoded": True,
        "alignment_validated": True,
        "alignment_id": "viplab-cam4-align-depth-to-color-v1",
        "rgb_frame_id": rgb.frame_id,
        "depth_frame_id": depth.frame_id,
        "rgb_shape_hw": [6, 8],
        "depth_shape_hw": [6, 8],
        "depth_scale_m_per_unit": 0.001,
        "depth_scale_validated": True,
        "valid_pixels": 48,
        "valid_ratio": 1.0,
    }
    return rgb, depth, color_info, depth_info, metadata, payload


def _rgbd_tool_detection(
    *,
    orientation_xyzw: list[float] | None,
) -> dict[str, object]:
    orientation_valid = orientation_xyzw is not None
    return {
        "instance_id": 4,
        "canonical_class_id": 1,
        "model_class_index": 0,
        "class_name": "Scalpel",
        "confidence": 0.91,
        "bbox_xyxy_px": [0.0, 0.0, 1.0, 1.0],
        "mask_rle": {"size": [6, 8], "counts": [0, 1, 47]},
        "observation": {
            "mask_bbox_xyxy_px": [0.0, 0.0, 1.0, 1.0],
            "mask_area_px": 1,
            "observation_point_uv_px": [0.0, 0.0],
            "observation_point_valid": True,
            "observation_point_inside_mask": True,
            "observation_point_depth_valid": True,
            "observation_point_depth_m": 0.8,
            "observation_point_selection_mode": "longitudinal_axis_midpoint",
            "observation_point_boundary_clearance_px": 0.0,
        },
        "pose": {
            # CameraInfo fx=fy=100, cx=4, cy=3: this projects exactly to
            # the observed mask pixel (0, 0) at z=0.8 m.
            "position_m": [-0.032, -0.024, 0.8],
            "orientation_xyzw": orientation_xyzw,
            "pose_mode": (
                "PLANAR_4DOF_WITH_NORMAL_PRIOR"
                if orientation_valid
                else "POSITION_3D_ONLY"
            ),
            "position_valid": True,
            "orientation_valid": orientation_valid,
            "dof_observed": [
                True,
                True,
                True,
                False,
                False,
                orientation_valid,
            ],
            "observation_point_definition": (
                "mask_internal_depth_valid_observed_surface_point_v1"
            ),
            "axis_definition": (
                "+Y handle/proximal to working tip; +Z support plane to free "
                "space; +X=+Yx+Z"
            ),
            "symmetry_type": "NONE",
            "endpoint_sign_confidence": 0.8 if orientation_valid else 0.4,
            "valid_depth_ratio": 1.0,
            "pose_point_count": 1,
            "axis_anisotropy": 2.0 if orientation_valid else 1.0,
            "support_plane_inlier_ratio": 0.95,
            "support_plane_residual_p95_m": 0.004,
            "pose_confidence": 0.0,
            "pose_confidence_calibrated": False,
            "validity": "VALID" if orientation_valid else "DEGRADED",
            "status_flags": [] if orientation_valid else ["SUPPORT_PLANE_UNVALIDATED"],
            "invalid_reason": "" if orientation_valid else "SUPPORT_PLANE_UNVALIDATED",
        },
    }


def _rgbd_hand_detection() -> dict[str, object]:
    joints_2d = [
        [float(1 + index % 6), float(1 + (index // 6) % 4)]
        for index in range(21)
    ]
    joints_2d[0] = [1.0, 4.0]
    joints_2d[2] = [2.0, 1.0]
    joints_2d[9] = [6.0, 4.0]
    joints_2d[17] = [6.0, 1.0]
    joints_3d = [
        [
            round((point[0] - 4.0) * 0.008, 6),
            round((point[1] - 3.0) * 0.008, 6),
            0.8,
        ]
        for point in joints_2d
    ]
    return {
        "hand_index": 0,
        "handedness": {"label": "Left", "score": 0.95},
        "joints_2d": joints_2d,
        "kp_scores": [1.0] * 21,
        "joints_3d": joints_3d,
        "kp_valid_depth": [True] * 21,
        "palm_6d": {
            "translation": [-0.004, 0.008, 0.8],
            "orientation_xyzw": [0.0, 0.0, 0.0, 1.0],
            "rotation_matrix": [
                1.0,
                0.0,
                0.0,
                0.0,
                1.0,
                0.0,
                0.0,
                0.0,
                1.0,
            ],
        },
    }


def test_service_url_is_one_explicit_origin_without_credentials() -> None:
    assert validate_service_url("http://127.0.0.1:8020/") == "http://127.0.0.1:8020"
    assert endpoint_is_loopback("http://localhost:8020") is True
    assert endpoint_is_loopback("http://localhost.localdomain.:8020") is True
    assert endpoint_is_loopback("http://192.168.1.20:8020") is False
    with pytest.raises(ValueError, match="credentials"):
        validate_service_url("http://user:secret@192.168.1.20:8020")
    with pytest.raises(ValueError, match="credentials"):
        validate_service_url("http://@192.168.1.20:8020")
    with pytest.raises(ValueError, match="API path"):
        validate_service_url("http://192.168.1.20:8020/v1/infer")
    with pytest.raises(ValueError, match="invalid port"):
        validate_service_url("http://192.168.1.20:not-a-port")


def test_dns_alias_never_receives_the_loopback_security_exception() -> None:
    assert endpoint_is_loopback("http://loopback-alias.example:8020") is False


def test_remote_auth_is_bearer_or_explicit_trusted_lan_dev_only() -> None:
    assert resolve_endpoint_auth_mode(
        "http://127.0.0.1:8020",
        has_token=False,
        allow_unauthenticated_remote=False,
    ) == ("none", "none_local")
    assert resolve_endpoint_auth_mode(
        "http://192.168.1.20:8020",
        has_token=True,
        allow_unauthenticated_remote=False,
    ) == ("bearer", "bearer")
    assert resolve_endpoint_auth_mode(
        "http://192.168.1.20:8020",
        has_token=False,
        allow_unauthenticated_remote=True,
    ) == ("none", "none_trusted_lan_dev")
    with pytest.raises(ValueError, match="requires api_token_file"):
        resolve_endpoint_auth_mode(
            "http://192.168.1.20:8020",
            has_token=False,
            allow_unauthenticated_remote=False,
        )


def test_remote_transport_is_https_or_explicit_trusted_lan_dev_only() -> None:
    assert resolve_endpoint_transport_mode(
        "http://127.0.0.1:8020",
        allow_insecure_remote_http=False,
    ) == "http_local"
    assert resolve_endpoint_transport_mode(
        "https://192.168.1.20:8020",
        allow_insecure_remote_http=False,
    ) == "https"
    assert resolve_endpoint_transport_mode(
        "http://192.168.1.20:8020",
        allow_insecure_remote_http=True,
    ) == "http_trusted_lan_dev"
    with pytest.raises(ValueError, match="remote PNU endpoint must use https"):
        resolve_endpoint_transport_mode(
            "http://192.168.1.20:8020",
            allow_insecure_remote_http=False,
        )


def test_remote_transport_and_auth_opt_ins_are_independent() -> None:
    assert resolve_endpoint_transport_mode(
        "http://192.168.1.20:8020",
        allow_insecure_remote_http=True,
    ) == "http_trusted_lan_dev"
    with pytest.raises(ValueError, match="requires api_token_file"):
        resolve_endpoint_auth_mode(
            "http://192.168.1.20:8020",
            has_token=False,
            allow_unauthenticated_remote=False,
        )


def test_expected_model_digests_require_lowercase_sha256() -> None:
    assert parse_expected_model_digests(json.dumps(DIGESTS)) == DIGESTS
    with pytest.raises(ValueError, match="lowercase SHA256"):
        parse_expected_model_digests('{"tool":"not-a-digest"}')
    with pytest.raises(ValueError, match="lowercase SHA256"):
        parse_expected_model_digests('{"tool":"' + "A" * 64 + '"}')


def test_expected_model_digests_pin_every_requested_algorithm() -> None:
    assert parse_expected_model_digests(
        json.dumps(DIGESTS),
        requested_algorithms=("tool", "blood", "hand"),
    ) == DIGESTS
    with pytest.raises(ValueError, match="pin every requested algorithm"):
        parse_expected_model_digests(
            "{}",
            requested_algorithms=("tool",),
        )
    with pytest.raises(ValueError, match="missing=.*blood"):
        parse_expected_model_digests(
            json.dumps({"tool": DIGESTS["tool"]}),
            requested_algorithms=("tool", "blood"),
        )


def test_native_depth_pairing_is_skew_bounded_and_never_claims_alignment() -> None:
    rgb = _frame(10_000_000_000)
    close_depth = _frame(10_040_000_000, depth=True)
    far_depth = _frame(10_300_000_000, depth=True)
    selected = closest_by_stamp([close_depth, far_depth], rgb.stamp_ns, 0.05)
    assert selected == close_depth
    assert closest_by_stamp([far_depth], rgb.stamp_ns, 0.05) is None

    metadata = build_request_metadata(
        request_id="request-1",
        rgb=rgb,
        depth=selected,
        color_camera_info=None,
        depth_camera_info=None,
        requested_algorithms=("tool",),
        deadline_unix_ms=20_000,
    )
    assert metadata["source"]["depth"]["aligned"] is False
    assert metadata["source"]["depth"]["stamp_ns"] == close_depth.stamp_ns
    assert metadata["depth_scale_validated"] is False
    scaled = build_request_metadata(
        request_id="request-scaled",
        rgb=rgb,
        depth=selected,
        color_camera_info=None,
        depth_camera_info=None,
        requested_algorithms=("tool",),
        deadline_unix_ms=20_000,
        depth_scale_m_per_unit=0.001,
        depth_scale_validated=True,
    )
    assert scaled["depth_scale_m_per_unit"] == 0.001
    assert scaled["depth_scale_validated"] is True
    with pytest.raises(ValueError, match="validated depth scale"):
        build_request_metadata(
            request_id="request-invalid-scale",
            rgb=rgb,
            depth=selected,
            color_camera_info=None,
            depth_camera_info=None,
            requested_algorithms=("tool",),
            deadline_unix_ms=20_000,
            depth_scale_m_per_unit=0.0,
            depth_scale_validated=True,
        )
    assert (
        "depth"
        not in build_request_metadata(
            request_id="request-2",
            rgb=rgb,
            depth=None,
            color_camera_info=None,
            depth_camera_info=None,
            requested_algorithms=("tool",),
            deadline_unix_ms=20_000,
        )["source"]
    )


def test_aligned_depth_requires_payload_frame_camera_info_and_scale_agreement() -> None:
    stamp_ns = 10_000_000_000
    rgb = _real_frame(stamp_ns)
    depth = _real_frame(stamp_ns + 1_000_000, depth=True)
    color_info = _camera_info(stamp_ns)
    depth_info = _camera_info(stamp_ns + 1_000_000)
    aligned = validate_aligned_depth_contract(
        rgb=rgb,
        depth=depth,
        color_camera_info=color_info,
        depth_camera_info=depth_info,
        configured_alignment_validated=True,
        alignment_id="viplab-cam4-align-depth-to-color-v1",
        depth_scale_m_per_unit=0.001,
        depth_scale_validated=True,
    )
    assert aligned.aligned is True
    metadata = build_request_metadata(
        request_id="aligned-request",
        rgb=rgb,
        depth=depth,
        color_camera_info=color_info,
        depth_camera_info=depth_info,
        requested_algorithms=("tool",),
        deadline_unix_ms=20_000,
        depth_scale_m_per_unit=0.001,
        depth_scale_validated=True,
        depth_alignment_validated=True,
        depth_alignment_id="viplab-cam4-align-depth-to-color-v1",
    )
    assert metadata["source"]["depth"]["aligned"] is True
    assert metadata["alignment"] == {
        "validated": True,
        "id": "viplab-cam4-align-depth-to-color-v1",
    }

    wrong_frame = _real_frame(
        stamp_ns + 1_000_000,
        depth=True,
        frame_id="cam_4_depth_optical_frame",
    )
    rejected = validate_aligned_depth_contract(
        rgb=rgb,
        depth=wrong_frame,
        color_camera_info=color_info,
        depth_camera_info=depth_info,
        configured_alignment_validated=True,
        alignment_id="viplab-cam4-align-depth-to-color-v1",
        depth_scale_m_per_unit=0.001,
        depth_scale_validated=True,
    )
    assert rejected.aligned is False
    assert "rgb_depth_frame_id_mismatch" in rejected.reasons

    wrong_shape = _real_frame(stamp_ns + 1_000_000, depth=True, width=7, height=6)
    rejected_shape = validate_aligned_depth_contract(
        rgb=rgb,
        depth=wrong_shape,
        color_camera_info=color_info,
        depth_camera_info=depth_info,
        configured_alignment_validated=True,
        alignment_id="viplab-cam4-align-depth-to-color-v1",
        depth_scale_m_per_unit=0.001,
        depth_scale_validated=True,
    )
    assert rejected_shape.aligned is False
    assert "rgb_depth_payload_shape_mismatch" in rejected_shape.reasons


def test_camera_info_is_flattened_to_the_worker_contract() -> None:
    message = CameraInfo()
    message.header.stamp.sec = 123
    message.header.stamp.nanosec = 45
    message.header.frame_id = "cam_4_color_optical_frame"
    message.width = 1280
    message.height = 720
    message.distortion_model = "plumb_bob"
    message.d = [0.0] * 5
    message.k = [1.0, 0.0, 640.0, 0.0, 1.0, 360.0, 0.0, 0.0, 1.0]
    message.r = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
    message.p = [1.0, 0.0, 640.0, 0.0, 0.0, 1.0, 360.0, 0.0, 0.0, 0.0, 1.0, 0.0]

    buffered = buffered_camera_info(message)

    assert set(buffered.payload) == {
        "stamp_ns",
        "frame_id",
        "width",
        "height",
        "distortion_model",
        "d",
        "k",
        "r",
        "p",
    }
    assert buffered.payload["stamp_ns"] == 123_000_000_045
    assert buffered.payload["frame_id"] == "cam_4_color_optical_frame"


def test_camera_info_digest_matches_the_worker_canonical_core() -> None:
    camera_info = _camera_info(123_000_000_045)
    assert bridge_module._camera_info_sha256(camera_info.payload) == (
        "48774320f72fb44df176beea2cd46b217a875e1915b63d16943af3dd7eed4153"
    )

    # Source timing is intentionally outside the calibration identity.
    same_core = copy.deepcopy(camera_info.payload)
    same_core["stamp_ns"] += 999
    same_core["transport_only"] = "ignored"
    assert bridge_module._camera_info_sha256(same_core) == (
        "48774320f72fb44df176beea2cd46b217a875e1915b63d16943af3dd7eed4153"
    )


def test_health_and_capabilities_pin_upstream_and_model_digests() -> None:
    now_ms = int(time.time() * 1000)
    health_digests = validate_worker_health(
        _health(now_ms),
        requested_algorithms=("tool", "blood", "hand"),
        received_unix_ms=now_ms,
        max_age_ms=5_000,
        max_clock_skew_ms=1_000,
    )
    capability_digests = validate_capabilities(
        _capabilities(),
        requested_algorithms=("tool", "blood", "hand"),
        expected_model_digests=DIGESTS,
        expected_auth_mode="none",
    )
    assert health_digests == capability_digests == DIGESTS

    wrong_commit = _health(now_ms)
    wrong_commit["upstream"]["detected_commit"] = "f" * 40
    with pytest.raises(ContractError, match="commit"):
        validate_worker_health(
            wrong_commit,
            requested_algorithms=("tool",),
            received_unix_ms=now_ms,
            max_age_ms=5_000,
            max_clock_skew_ms=1_000,
        )


def test_capabilities_pin_response_and_rle_resource_budgets() -> None:
    at_boundary = _capabilities()
    assert at_boundary["limits"]["response_json_bytes"] == 16 * 1024 * 1024
    assert at_boundary["limits"]["total_rle_counts_per_algorithm"] == 1_000_000
    validate_capabilities(
        at_boundary,
        requested_algorithms=("tool", "blood", "hand"),
        expected_model_digests=DIGESTS,
        expected_auth_mode="none",
    )

    for name, value, error in (
        ("response_json_bytes", 16 * 1024 * 1024 + 1, "response_json_bytes"),
        (
            "total_rle_counts_per_algorithm",
            1_000_001,
            "total_rle_counts_per_algorithm",
        ),
    ):
        oversized = _capabilities()
        oversized["limits"][name] = value
        with pytest.raises(ContractError, match=error):
            validate_capabilities(
                oversized,
                requested_algorithms=("tool", "blood", "hand"),
                expected_model_digests=DIGESTS,
                expected_auth_mode="none",
            )


def test_degraded_global_health_allows_only_a_ready_requested_subset() -> None:
    now_ms = int(time.time() * 1000)
    health = _health(now_ms)
    health["status"] = "degraded"
    health["ready"] = False
    health["models"] = _models(executed=False, unavailable={"tool"})
    digests = validate_worker_health(
        health,
        requested_algorithms=("blood", "hand"),
        received_unix_ms=now_ms,
        max_age_ms=5_000,
        max_clock_skew_ms=1_000,
    )
    assert digests == {"blood": DIGESTS["blood"], "hand": DIGESTS["hand"]}
    capabilities = _capabilities()
    capabilities["models"] = _models(executed=False, unavailable={"tool"})
    assert validate_capabilities(
        capabilities,
        requested_algorithms=("blood", "hand"),
        expected_model_digests={
            "blood": DIGESTS["blood"],
            "hand": DIGESTS["hand"],
        },
        expected_auth_mode="none",
    ) == {"blood": DIGESTS["blood"], "hand": DIGESTS["hand"]}
    with pytest.raises(ContractError, match="tool.ready"):
        validate_worker_health(
            health,
            requested_algorithms=("tool", "blood", "hand"),
            received_unix_ms=now_ms,
            max_age_ms=5_000,
            max_clock_skew_ms=1_000,
        )


def test_zero_detections_are_valid_only_with_explicit_execution_evidence() -> None:
    now_ms = int(time.time() * 1000)
    metadata = _metadata(now_ms=now_ms)
    payload = _response(metadata, generated_unix_ms=now_ms + 10)
    validated = validate_worker_response(
        payload,
        metadata=metadata,
        pinned_model_digests=DIGESTS,
        request_started_unix_ms=now_ms,
        received_unix_ms=now_ms + 20,
    )
    assert validated.tool_detections == ()
    assert validated.blood_detections == ()
    assert validated.hands == ()

    payload["results"]["tool"]["executed"] = False
    with pytest.raises(ContractError, match="executed"):
        validate_worker_response(
            payload,
            metadata=metadata,
            pinned_model_digests=DIGESTS,
            request_started_unix_ms=now_ms,
            received_unix_ms=now_ms + 20,
        )


def test_2d_tool_fallback_keeps_class_index_and_typed_pose_provenance() -> None:
    now_ms = int(time.time() * 1000)
    metadata = _metadata(now_ms=now_ms, algorithms=("tool",))
    payload = _response(
        metadata,
        generated_unix_ms=now_ms + 5,
        tool_detections=[
            {
                "instance_id": 3,
                "canonical_class_id": 8,
                "class_name": "Thyroid Retractor",
                "confidence": 0.92,
                "bbox_xyxy_px": [0.0, 0.0, 1.0, 1.0],
                "mask_rle": {
                    "size": [720, 1280],
                    "counts": [0, 1, 921_599],
                },
            }
        ],
    )
    validated = validate_worker_response(
        payload,
        metadata=metadata,
        pinned_model_digests={"tool": DIGESTS["tool"]},
        request_started_unix_ms=now_ms,
        received_unix_ms=now_ms + 10,
    )
    assert validated.tool_detections[0]["model_class_index"] == 7

    source = metadata["source"]["rgb"]
    frame = _frame(int(source["stamp_ns"]))
    poses, observations = build_tool_ros_messages(
        frame=frame,
        sequence=1,
        detections=validated.tool_detections,
        tool_result=payload["results"]["tool"],
        model_version="tool-v1",
    )
    assert poses.ontology_version == "pnu.cam4.tool_ontology.v1"
    assert poses.pose_convention_version == "pnu.cam4.planar_tool_pose_convention.v2"
    assert poses.calibration_version == "metric_3d_unavailable"
    assert observations.ontology_version == poses.ontology_version
    assert poses.tools[0].model_class_index == 7
    assert observations.instances[0].model_class_index == 7
    assert poses.tools[0].pose_mode == ToolPose.POSE_MODE_INVALID
    assert poses.tools[0].validity == ToolPose.VALIDITY_INVALID
    assert poses.tools[0].status_flags == ["METRIC_3D_UNAVAILABLE"]
    assert poses.tools[0].invalid_reason == "2d_only_metric_pose_unavailable"


def test_per_algorithm_rle_budget_is_cumulative(monkeypatch) -> None:
    monkeypatch.setattr(
        bridge_module,
        "_MAX_ADVERTISED_RLE_COUNTS_PER_ALGORITHM",
        5,
    )
    now_ms = int(time.time() * 1000)
    mask = {"size": [720, 1280], "counts": [0, 1, 921_599]}
    metadata = _metadata(now_ms=now_ms, algorithms=("tool",))
    payload = _response(
        metadata,
        generated_unix_ms=now_ms + 5,
        tool_detections=[
            {
                "instance_id": instance_id,
                "canonical_class_id": 1,
                "class_name": "Scalpel",
                "confidence": 0.9,
                "bbox_xyxy_px": [0.0, 0.0, 1.0, 1.0],
                "mask_rle": mask,
            }
            for instance_id in (1, 2)
        ],
    )
    with pytest.raises(ContractError, match="remaining RLE run budget"):
        validate_worker_response(
            payload,
            metadata=metadata,
            pinned_model_digests={"tool": DIGESTS["tool"]},
            request_started_unix_ms=now_ms,
            received_unix_ms=now_ms + 10,
        )

    metadata = _metadata(now_ms=now_ms, algorithms=("blood",))
    payload = _response(metadata, generated_unix_ms=now_ms + 5)
    payload["results"]["blood"]["detections"] = [
        {
            "instance_id": instance_id,
            "class_id": 1,
            "class_name": "blood",
            "confidence": 0.9,
            "bbox_xyxy_px": [0.0, 0.0, 1.0, 1.0],
            "centroid_xy_px": [0.0, 0.0],
            "mask_rle": mask,
        }
        for instance_id in (1, 2)
    ]
    with pytest.raises(ContractError, match="remaining RLE run budget"):
        validate_worker_response(
            payload,
            metadata=metadata,
            pinned_model_digests={"blood": DIGESTS["blood"]},
            request_started_unix_ms=now_ms,
            received_unix_ms=now_ms + 10,
        )


def test_rle_remaining_budget_rejects_before_uncompressed_copy_or_compressed_append(
    monkeypatch,
) -> None:
    uncompressed = {"size": [6, 8], "counts": [0, 1, 47]}

    def _must_not_decode(*_args, **_kwargs):
        raise AssertionError("oversized uncompressed RLE reached the copying decoder")

    monkeypatch.setattr(bridge_module, "_rle_counts", _must_not_decode)
    with pytest.raises(ContractError, match="remaining RLE run budget"):
        bridge_module._validate_rle(
            uncompressed,
            field="mask",
            remaining_counts=2,
        )

    compressed = bridge_module._coco_counts_to_compressed_string([0, 1, 47])
    with pytest.raises(ContractError, match="remaining RLE run budget"):
        bridge_module._compressed_coco_counts_to_list(
            compressed,
            field="mask.counts",
            max_counts=2,
        )
    assert bridge_module._compressed_coco_counts_to_list(
        compressed,
        field="mask.counts",
        max_counts=3,
    ) == [0, 1, 47]


def test_response_images_are_bound_to_the_actual_request_rgb_dimensions() -> None:
    now_ms = int(time.time() * 1000)
    metadata = _metadata(now_ms=now_ms, algorithms=("tool",))
    payload = _response(metadata, generated_unix_ms=now_ms + 5)
    validate_worker_response(
        payload,
        metadata=metadata,
        pinned_model_digests={"tool": DIGESTS["tool"]},
        request_started_unix_ms=now_ms,
        received_unix_ms=now_ms + 10,
        expected_rgb_dimensions=(1280, 720),
    )

    forged = copy.deepcopy(payload)
    forged["results"]["tool"]["image"] = {"width": 32768, "height": 32768}
    with pytest.raises(ContractError, match="request RGB image"):
        validate_worker_response(
            forged,
            metadata=metadata,
            pinned_model_digests={"tool": DIGESTS["tool"]},
            request_started_unix_ms=now_ms,
            received_unix_ms=now_ms + 10,
            expected_rgb_dimensions=(1280, 720),
        )


def test_depth_evidence_rgb_shape_is_bound_to_the_decoded_request_rgb() -> None:
    now_ms = int(time.time() * 1000)
    _rgb, _depth, _color, _depth_info, metadata, payload = _rgbd_fixture(now_ms)
    payload["depth_evidence"]["rgb_shape_hw"] = [32768, 32768]
    with pytest.raises(ContractError, match="request RGB image"):
        validate_worker_response(
            payload,
            metadata=metadata,
            pinned_model_digests=DIGESTS,
            request_started_unix_ms=now_ms,
            received_unix_ms=now_ms + 10,
            expected_rgb_dimensions=(8, 6),
        )


def test_blood_centroids_must_match_each_mask_and_the_mask_union() -> None:
    now_ms = int(time.time() * 1000)
    _rgb, _depth, _color, _depth_info, metadata, payload = _rgbd_fixture(now_ms)

    def detection(instance_id: int, x: int, y: int) -> dict[str, object]:
        offset = x * 6 + y
        return {
            "instance_id": instance_id,
            "class_id": 1,
            "class_name": "blood",
            "confidence": 0.9,
            "bbox_xyxy_px": [float(x), float(y), float(x + 1), float(y + 1)],
            "centroid_xy_px": [float(x), float(y)],
            "centroid_depth_m": 0.8,
            "mask_rle": {
                "size": [6, 8],
                "counts": [offset, 1, 48 - offset - 1],
            },
        }

    payload["results"]["blood"]["detections"] = [
        detection(0, 0, 0),
        detection(1, 2, 0),
    ]
    payload["results"]["blood"]["combined_blood_centroid_xy_px"] = [1.0, 0.0]
    payload["results"]["blood"]["combined_blood_centroid_depth_m"] = 0.8
    validate_worker_response(
        payload,
        metadata=metadata,
        pinned_model_digests=DIGESTS,
        request_started_unix_ms=now_ms,
        received_unix_ms=now_ms + 10,
    )

    wrong_instance = copy.deepcopy(payload)
    wrong_instance["results"]["blood"]["detections"][0]["centroid_xy_px"] = [
        0.01,
        0.0,
    ]
    with pytest.raises(ContractError, match="validated mask"):
        validate_worker_response(
            wrong_instance,
            metadata=metadata,
            pinned_model_digests=DIGESTS,
            request_started_unix_ms=now_ms,
            received_unix_ms=now_ms + 10,
        )

    wrong_union = copy.deepcopy(payload)
    wrong_union["results"]["blood"]["combined_blood_centroid_xy_px"] = [0.0, 0.0]
    with pytest.raises(ContractError, match="validated mask union"):
        validate_worker_response(
            wrong_union,
            metadata=metadata,
            pinned_model_digests=DIGESTS,
            request_started_unix_ms=now_ms,
            received_unix_ms=now_ms + 10,
        )

    missing_union = copy.deepcopy(payload)
    missing_union["results"]["blood"]["combined_blood_centroid_xy_px"] = None
    missing_union["results"]["blood"]["combined_blood_centroid_depth_m"] = None
    with pytest.raises(ContractError, match="lacks a combined centroid"):
        validate_worker_response(
            missing_union,
            metadata=metadata,
            pinned_model_digests=DIGESTS,
            request_started_unix_ms=now_ms,
            received_unix_ms=now_ms + 10,
        )

    empty_mask = copy.deepcopy(payload)
    empty_mask["results"]["blood"]["detections"] = [detection(0, 0, 0)]
    empty_mask["results"]["blood"]["detections"][0]["mask_rle"]["counts"] = [48]
    empty_mask["results"]["blood"]["combined_blood_centroid_xy_px"] = [0.0, 0.0]
    with pytest.raises(ContractError, match="mask_rle is empty"):
        validate_worker_response(
            empty_mask,
            metadata=metadata,
            pinned_model_digests=DIGESTS,
            request_started_unix_ms=now_ms,
            received_unix_ms=now_ms + 10,
        )


def test_rgbd_zero_detections_are_metric_ready_and_publish_empty_typed_arrays() -> None:
    now_ms = int(time.time() * 1000)
    rgb, depth, color_info, depth_info, metadata, payload = _rgbd_fixture(now_ms)
    validated = validate_worker_response(
        payload,
        metadata=metadata,
        pinned_model_digests=DIGESTS,
        request_started_unix_ms=now_ms,
        received_unix_ms=now_ms + 10,
    )
    assert validated.metric_3d_ready is True

    node = PNUPerceptionBridgeNode.__new__(PNUPerceptionBridgeNode)
    node._semantics_pub = _Publisher()
    node._mayo_pub = _Publisher()
    node._diagnostics_pub = _Publisher()
    node._tool_pose_pub = _Publisher()
    node._tool_observations_pub = _Publisher()
    node._blood_semantics_pub = _Publisher()
    node._hand_keypoints_pub = _Publisher()
    node._overlay_pub = _Publisher()
    node._pose_overlay_pub = _Publisher()
    node._mayo_tracker = Cam4MayoPlacementTracker()
    node._requested_algorithms = ("tool", "blood", "hand")
    node._overlay_enabled = True
    node._overlay_max_rate_hz = 5.0
    node._overlay_max_pixels = 1_000
    node._last_overlay_published_monotonic = 0.0
    node._pose_overlay_enabled = True
    node._pose_overlay_max_rate_hz = 15.0
    node._pose_overlay_max_pixels = 1_000
    node._pose_axis_length_m = 0.05
    node._last_pose_overlay_published_monotonic = 0.0
    node._last_pose_overlay_signature = (0, 0)
    node._sequence = 0
    node._dropped_frames = 0
    node._last_success_monotonic = 0.0
    node._last_success_source_stamp_ns = 0
    node._last_detection_count = -1
    node._auth_mode = "none_local"
    node._transport_mode = "http_local"
    health = []
    node._publish_health = lambda **kwargs: health.append(kwargs)

    node._publish_success(
        rgb=rgb,
        depth=depth,
        color_info=color_info,
        depth_info=depth_info,
        request_id=str(metadata["request_id"]),
        validated=validated,
        source_to_output_latency_ms=20.0,
    )

    assert node._tool_pose_pub.messages[0].tools == []
    assert node._tool_pose_pub.messages[0].header.frame_id == rgb.frame_id
    assert node._tool_observations_pub.messages[0].instances == []
    assert node._hand_keypoints_pub.messages[0].hands == []
    assert node._hand_keypoints_pub.messages[0].depth_source == "real"
    assert len(node._overlay_pub.messages) == 1
    overlay_message = node._overlay_pub.messages[0]
    assert overlay_message.format == "webp"
    assert overlay_message.header.stamp.sec == rgb.stamp_ns // 1_000_000_000
    assert overlay_message.header.stamp.nanosec == rgb.stamp_ns % 1_000_000_000
    assert overlay_message.header.frame_id == rgb.frame_id
    with Image.open(BytesIO(bytes(overlay_message.data))) as overlay:
        assert overlay.format == "WEBP"
        assert overlay.size == (8, 6)
        assert overlay.convert("RGBA").getchannel("A").getextrema() == (0, 0)
    assert len(node._pose_overlay_pub.messages) == 1
    pose_overlay_message = node._pose_overlay_pub.messages[0]
    assert pose_overlay_message.format == "webp"
    assert pose_overlay_message.header.stamp.sec == rgb.stamp_ns // 1_000_000_000
    assert (
        pose_overlay_message.header.stamp.nanosec
        == rgb.stamp_ns % 1_000_000_000
    )
    assert pose_overlay_message.header.frame_id == rgb.frame_id
    with Image.open(BytesIO(bytes(pose_overlay_message.data))) as overlay:
        assert overlay.size == (8, 6)
        assert overlay.convert("RGBA").getchannel("A").getextrema() == (0, 0)
    blood = json.loads(node._blood_semantics_pub.messages[0].data)
    diagnostics = json.loads(node._diagnostics_pub.messages[0].data)
    assert blood["metric_3d_ready"] is True
    assert blood["detections"] == []
    assert diagnostics["metric_3d_ready"] is True
    assert diagnostics["depth_aligned"] is True
    assert diagnostics["support_plane_config_version"] == (
        "pnu-cam4-reference-plane-v1"
    )
    assert diagnostics["support_plane_validated"] is False
    plane_diagnostics = diagnostics["support_plane_diagnostics"]
    assert plane_diagnostics == validated.tool_support_plane_diagnostics
    assert plane_diagnostics["calibration_fit"]["residual_p95_m"] == 0.004
    assert plane_diagnostics["runtime_validation"]["residual_p95_m"] == 0.021
    assert plane_diagnostics["runtime_validation"]["reasons"] == [
        "support_plane_runtime_inlier_ratio_low"
    ]
    assert diagnostics["overlay_published"] is True
    assert diagnostics["overlay_status"] == "published"
    assert diagnostics["overlay_truncated"] is False
    assert diagnostics["pose_overlay_published"] is True
    assert diagnostics["pose_overlay_status"] == "published"
    assert diagnostics["pose_overlay_drawn_axis_count"] == 0
    assert diagnostics["pose_overlay_drawn_position_only_count"] == 0
    assert diagnostics["pose_overlay_truncated"] is False
    assert diagnostics["render_encode_latency_ms"] >= 0.0
    assert health[-1]["metric_3d_ready"] is True
    assert health[-1]["support_plane_config_version"] == ("pnu-cam4-reference-plane-v1")
    assert health[-1]["support_plane_validated"] is False


def test_support_plane_diagnostics_are_bounded_and_fail_closed() -> None:
    now_ms = int(time.time() * 1000)
    _rgb, _depth, _color, _depth_info, metadata, payload = _rgbd_fixture(now_ms)

    oversized = copy.deepcopy(payload)
    oversized["results"]["tool"]["support_plane_diagnostics"][
        "static_reasons"
    ] = ["x" * 161]
    with pytest.raises(ContractError, match="static_reasons"):
        validate_worker_response(
            oversized,
            metadata=metadata,
            pinned_model_digests=DIGESTS,
            request_started_unix_ms=now_ms,
            received_unix_ms=now_ms + 10,
        )

    missing_live_metric = copy.deepcopy(payload)
    missing_live_metric["results"]["tool"]["support_plane_diagnostics"][
        "runtime_validation"
    ]["residual_p95_m"] = None
    with pytest.raises(ContractError, match="runtime_validation metrics"):
        validate_worker_response(
            missing_live_metric,
            metadata=metadata,
            pinned_model_digests=DIGESTS,
            request_started_unix_ms=now_ms,
            received_unix_ms=now_ms + 10,
        )

    contradictory = copy.deepcopy(payload)
    contradictory["results"]["tool"]["support_plane_diagnostics"] = (
        _support_plane_diagnostics(valid=True)
    )
    with pytest.raises(ContractError, match="validity disagrees"):
        validate_worker_response(
            contradictory,
            metadata=metadata,
            pinned_model_digests=DIGESTS,
            request_started_unix_ms=now_ms,
            received_unix_ms=now_ms + 10,
        )


def test_reviewed_support_plane_rechecks_camera_info_and_runtime_boundaries() -> None:
    now_ms = int(time.time() * 1000)
    _rgb, _depth, _color, _depth_info, metadata, payload = _rgbd_fixture(now_ms)
    reviewed_version = (
        "viplab_cam4_146222251000_support_plane_v1_sha256_b683ecd5a5382a4f"
    )
    expected_camera_digest = bridge_module._camera_info_sha256(
        metadata["color_camera_info"]
    )
    tool_result = payload["results"]["tool"]
    tool_result["support_plane_config_version"] = reviewed_version
    tool_result["support_plane_validated"] = True
    tool_result["support_plane_diagnostics"] = _support_plane_diagnostics(
        valid=True,
        camera_info_sha256=expected_camera_digest,
    )

    def validate(candidate):
        return validate_worker_response(
            candidate,
            metadata=metadata,
            pinned_model_digests=DIGESTS,
            request_started_unix_ms=now_ms,
            received_unix_ms=now_ms + 10,
            expected_tool_support_plane_config_version=reviewed_version,
        )

    boundary = copy.deepcopy(payload)
    boundary_runtime = boundary["results"]["tool"]["support_plane_diagnostics"][
        "runtime_validation"
    ]
    boundary_runtime.update(
        {
            "sample_count": 5_000,
            "inlier_ratio": 0.85,
            "residual_median_m": 0.006,
            "residual_p95_m": 0.02,
        }
    )
    assert validate(boundary).tool_support_plane_diagnostics is not None

    for name, value in (
        ("sample_count", 4_999),
        ("inlier_ratio", 0.849_999),
        ("residual_median_m", 0.006_001),
        ("residual_p95_m", 0.020_001),
    ):
        outside = copy.deepcopy(boundary)
        outside["results"]["tool"]["support_plane_diagnostics"][
            "runtime_validation"
        ][name] = value
        with pytest.raises(ContractError, match="reviewed support-plane"):
            validate(outside)

    digest_mismatch = copy.deepcopy(boundary)
    digest_mismatch["results"]["tool"]["support_plane_diagnostics"][
        "runtime_validation"
    ]["camera_info_sha256"] = "b" * 64
    with pytest.raises(ContractError, match="CameraInfo digest"):
        validate(digest_mismatch)

    empty_digest = copy.deepcopy(boundary)
    empty_digest["results"]["tool"]["support_plane_diagnostics"][
        "runtime_validation"
    ]["camera_info_sha256"] = ""
    with pytest.raises(ContractError, match="CameraInfo digest"):
        validate(empty_digest)

    # An explicit invalid result remains observable with its measured values;
    # it simply cannot promote the plane or any orientation to valid.
    invalid_with_metrics = copy.deepcopy(payload)
    invalid_tool = invalid_with_metrics["results"]["tool"]
    invalid_tool["support_plane_validated"] = False
    invalid_tool["support_plane_diagnostics"] = _support_plane_diagnostics(
        valid=False,
        camera_info_sha256=expected_camera_digest,
    )
    assert validate(invalid_with_metrics).tool_support_plane_diagnostics[
        "runtime_validation"
    ]["residual_p95_m"] == pytest.approx(0.021)


def test_rgbd_response_rejects_unaligned_claim_and_inconsistent_depth_evidence() -> (
    None
):
    now_ms = int(time.time() * 1000)
    _rgb, _depth, _color, _depth_info, metadata, payload = _rgbd_fixture(now_ms)
    payload["depth_evidence"]["valid_ratio"] = 0.5
    with pytest.raises(ContractError, match="valid pixel ratio"):
        validate_worker_response(
            payload,
            metadata=metadata,
            pinned_model_digests=DIGESTS,
            request_started_unix_ms=now_ms,
            received_unix_ms=now_ms + 10,
        )

    _rgb, _depth, _color, _depth_info, metadata, payload = _rgbd_fixture(now_ms)
    metadata["source"]["depth"]["aligned"] = False
    payload["source"]["depth"]["aligned"] = False
    with pytest.raises(ContractError, match="ineligible request"):
        validate_worker_response(
            payload,
            metadata=metadata,
            pinned_model_digests=DIGESTS,
            request_started_unix_ms=now_ms,
            received_unix_ms=now_ms + 10,
        )


def test_rgbd_nonempty_fields_map_to_upstream_typed_messages_and_reject_bad_pose() -> (
    None
):
    now_ms = int(time.time() * 1000)
    rgb, _depth, _color, _depth_info, metadata, payload = _rgbd_fixture(now_ms)
    mask_rle = {"size": [6, 8], "counts": [0, 1, 47]}
    payload["results"]["tool"]["detections"] = [
        {
            "instance_id": 4,
            "canonical_class_id": 1,
            "model_class_index": 0,
            "class_name": "Scalpel",
            "confidence": 0.91,
            "bbox_xyxy_px": [0.0, 0.0, 1.0, 1.0],
            "mask_rle": mask_rle,
            "observation": {
                "mask_bbox_xyxy_px": [0.0, 0.0, 1.0, 1.0],
                "mask_area_px": 1,
                "observation_point_uv_px": [0.0, 0.0],
                "observation_point_valid": True,
                "observation_point_inside_mask": True,
                "observation_point_depth_valid": True,
                "observation_point_depth_m": 0.8,
                "observation_point_selection_mode": "longitudinal_axis_midpoint",
                "observation_point_boundary_clearance_px": 0.0,
            },
            "pose": {
                    "position_m": [-0.032, -0.024, 0.8],
                "orientation_xyzw": None,
                    "pose_mode": "POSITION_3D_ONLY",
                "position_valid": True,
                "orientation_valid": False,
                "dof_observed": [True, True, True, False, False, False],
                "observation_point_definition": "mask_internal_depth_valid_observed_surface_point_v1",
                    "axis_definition": (
                        "+Y handle/proximal to working tip; +Z support plane to "
                        "free space; +X=+Yx+Z"
                    ),
                    "symmetry_type": "NONE",
                "endpoint_sign_confidence": 0.4,
                "valid_depth_ratio": 1.0,
                "pose_point_count": 1,
                "axis_anisotropy": 1.0,
                "support_plane_inlier_ratio": 0.95,
                "support_plane_residual_p95_m": 0.004,
                "pose_confidence": 0.0,
                "pose_confidence_calibrated": False,
                "validity": "DEGRADED",
                "status_flags": ["SUPPORT_PLANE_UNVALIDATED"],
                "invalid_reason": "SUPPORT_PLANE_UNVALIDATED",
            },
        }
    ]
    payload["results"]["blood"]["detections"] = [
        {
            "instance_id": 0,
            "class_id": 1,
            "class_name": "blood",
            "confidence": 0.88,
            "bbox_xyxy_px": [0.0, 0.0, 1.0, 1.0],
            "centroid_xy_px": [0.0, 0.0],
            "centroid_depth_m": 0.8,
            "mask_rle": mask_rle,
        }
    ]
    payload["results"]["blood"]["combined_blood_centroid_xy_px"] = [0.0, 0.0]
    payload["results"]["blood"]["combined_blood_centroid_depth_m"] = 0.8
    payload["results"]["hand"]["hands"] = [_rgbd_hand_detection()]
    validated = validate_worker_response(
        payload,
        metadata=metadata,
        pinned_model_digests=DIGESTS,
        request_started_unix_ms=now_ms,
        received_unix_ms=now_ms + 10,
    )
    poses, observations = build_tool_ros_messages(
        frame=rgb,
        sequence=7,
        detections=validated.tool_detections,
        tool_result=payload["results"]["tool"],
        model_version="tool-v1",
    )
    hands = build_hand_keypoints_message(
        frame=rgb, hands=validated.hands, metric_3d_ready=True
    )
    blood = build_blood_semantics(
        validated.blood_detections,
        source_stamp_ns=rgb.stamp_ns,
        source_stamp_sec=rgb.source_stamp_sec,
        frame_id=rgb.frame_id,
        metric_3d_ready=True,
        combined_centroid_xy_px=[0.0, 0.0],
        combined_centroid_depth_m=0.8,
    )
    compatibility_semantics = build_cam4_semantics(
        validated.tool_detections,
        source_stamp_sec=rgb.source_stamp_sec,
        inference_latency_ms=1.0,
    )
    assert poses.tools[0].position_valid is True
    # Typed PNU evidence retains its provider ontology; only the legacy
    # compatibility projection receives the Taskplanner catalog name.
    assert poses.tools[0].class_name == "Scalpel"
    assert poses.ontology_version == payload["results"]["tool"]["ontology_version"]
    assert compatibility_semantics["tools"][0]["name"] == "#15 Scalpel"
    assert poses.tools[0].orientation_valid is False
    assert poses.tools[0].pose.position.z == pytest.approx(0.8)
    assert observations.instances[0].observation_point_depth_m == pytest.approx(0.8)
    assert observations.instances[0].mask_counts
    assert hands.hands[0].has_palm_6d is True
    assert hands.hands[0].palm_6d.translation.z == pytest.approx(0.8)
    assert set(blood) == {
        "schema",
        "source",
        "provider",
        "source_stamp_ns",
        "source_stamp_sec",
        "frame_id",
        "ground_truth",
        "metric_3d_ready",
        "detections",
        "combined_centroid_xy_px",
        "combined_centroid_depth_valid",
        "combined_centroid_depth_m",
    }
    assert blood["combined_centroid_depth_valid"] is True
    assert blood["source_stamp_ns"] == str(rgb.stamp_ns)

    rendered = build_pnu_debug_overlay(
        frame=rgb,
        validated=validated,
        max_pixels=1_000,
        max_rle_runs=100,
        max_mask_segments=100,
    )
    assert rendered.message.format == "webp"
    assert rendered.drawn_tool_count == 1
    assert rendered.drawn_blood_count == 1
    assert rendered.drawn_hand_count == 1
    assert rendered.truncated is False
    with Image.open(BytesIO(bytes(rendered.message.data))) as overlay:
        rgba = overlay.convert("RGBA")
        assert rgba.size == (8, 6)
        assert rgba.getchannel("A").getextrema()[1] > 0

    bounded = build_pnu_debug_overlay(
        frame=rgb,
        validated=validated,
        max_pixels=1_000,
        max_rle_runs=100,
        max_mask_segments=1,
    )
    assert bounded.truncated is True

    invalid = copy.deepcopy(payload)
    invalid["results"]["hand"]["hands"][0]["palm_6d"]["orientation_xyzw"] = [
        0.0,
        0.0,
        0.0,
        2.0,
    ]
    with pytest.raises(ContractError, match="quaternion"):
        validate_worker_response(
            invalid,
            metadata=metadata,
            pinned_model_digests=DIGESTS,
            request_started_unix_ms=now_ms,
            received_unix_ms=now_ms + 10,
        )

    unsafe_plane = copy.deepcopy(payload)
    unsafe_pose = unsafe_plane["results"]["tool"]["detections"][0]["pose"]
    unsafe_pose["orientation_xyzw"] = [0.0, 0.0, 0.0, 1.0]
    unsafe_pose["orientation_valid"] = True
    unsafe_pose["pose_mode"] = "PLANAR_4DOF_WITH_NORMAL_PRIOR"
    unsafe_pose["dof_observed"] = [True, True, True, False, False, True]
    unsafe_pose["axis_anisotropy"] = 2.0
    unsafe_pose["validity"] = "VALID"
    unsafe_pose["status_flags"] = []
    unsafe_pose["invalid_reason"] = ""
    with pytest.raises(ContractError, match="support_plane_validated is false"):
        validate_worker_response(
            unsafe_plane,
            metadata=metadata,
            pinned_model_digests=DIGESTS,
            request_started_unix_ms=now_ms,
            received_unix_ms=now_ms + 10,
        )

    contradictory_plane = copy.deepcopy(payload)
    contradictory_plane["results"]["tool"]["support_plane_validated"] = True
    contradictory_plane["results"]["tool"]["support_plane_diagnostics"] = (
        _support_plane_diagnostics(valid=True)
    )
    with pytest.raises(ContractError, match="support_plane_validated is true"):
        validate_worker_response(
            contradictory_plane,
            metadata=metadata,
            pinned_model_digests=DIGESTS,
            request_started_unix_ms=now_ms,
            received_unix_ms=now_ms + 10,
        )

    invalid_plane_type = copy.deepcopy(payload)
    invalid_plane_type["results"]["tool"]["support_plane_validated"] = "false"
    with pytest.raises(ContractError, match="must be boolean"):
        validate_worker_response(
            invalid_plane_type,
            metadata=metadata,
            pinned_model_digests=DIGESTS,
            request_started_unix_ms=now_ms,
            received_unix_ms=now_ms + 10,
        )


def test_rgbd_hand_geometry_is_self_consistent_with_camera_and_palm_frame() -> None:
    now_ms = int(time.time() * 1000)
    _rgb, _depth, _color, _depth_info, metadata, payload = _rgbd_fixture(now_ms)
    payload["results"]["hand"]["hands"] = [_rgbd_hand_detection()]

    def validate(candidate):
        return validate_worker_response(
            candidate,
            metadata=metadata,
            pinned_model_digests=DIGESTS,
            request_started_unix_ms=now_ms,
            received_unix_ms=now_ms + 10,
        )

    assert validate(payload).hands[0]["palm_6d"] is not None

    bad_reprojection = copy.deepcopy(payload)
    bad_reprojection["results"]["hand"]["hands"][0]["joints_3d"][5][0] += 0.02
    with pytest.raises(ContractError, match="does not reproject"):
        validate(bad_reprojection)

    bad_translation = copy.deepcopy(payload)
    bad_translation["results"]["hand"]["hands"][0]["palm_6d"]["translation"][
        0
    ] += 0.01
    with pytest.raises(ContractError, match=r"not \(j0\+j9\)/2"):
        validate(bad_translation)

    reflected = copy.deepcopy(payload)
    reflected["results"]["hand"]["hands"][0]["palm_6d"]["rotation_matrix"] = [
        1.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        -1.0,
    ]
    with pytest.raises(ContractError, match=r"determinant is not \+1"):
        validate(reflected)

    quaternion_mismatch = copy.deepcopy(payload)
    quaternion_mismatch["results"]["hand"]["hands"][0]["palm_6d"][
        "orientation_xyzw"
    ] = [0.0, 0.0, 1.0, 0.0]
    with pytest.raises(ContractError, match="quaternion and rotation matrix disagree"):
        validate(quaternion_mismatch)

    wrong_palm_frame = copy.deepcopy(payload)
    root_half = math.sqrt(0.5)
    wrong_palm = wrong_palm_frame["results"]["hand"]["hands"][0]["palm_6d"]
    wrong_palm["orientation_xyzw"] = [0.0, 0.0, root_half, root_half]
    wrong_palm["rotation_matrix"] = [
        0.0,
        -1.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
    ]
    with pytest.raises(ContractError, match="palm_frame_v2"):
        validate(wrong_palm_frame)


def test_validated_tool_support_plane_requires_exact_taskplanner_pin() -> None:
    now_ms = int(time.time() * 1000)
    _rgb, _depth, _color, _depth_info, metadata, payload = _rgbd_fixture(now_ms)
    tool_result = payload["results"]["tool"]
    tool_result["support_plane_validated"] = True
    tool_result["support_plane_diagnostics"] = _support_plane_diagnostics(valid=True)
    tool_result["support_plane_config_version"] = "cam4-plane-sha256:abc123"
    tool_result["detections"] = [
        _rgbd_tool_detection(orientation_xyzw=[0.0, 0.0, 0.0, 1.0])
    ]

    with pytest.raises(ContractError, match="not pinned by Taskplanner"):
        validate_worker_response(
            payload,
            metadata=metadata,
            pinned_model_digests=DIGESTS,
            request_started_unix_ms=now_ms,
            received_unix_ms=now_ms + 10,
        )
    with pytest.raises(ContractError, match="does not match the Taskplanner pin"):
        validate_worker_response(
            payload,
            metadata=metadata,
            pinned_model_digests=DIGESTS,
            request_started_unix_ms=now_ms,
            received_unix_ms=now_ms + 10,
            expected_tool_support_plane_config_version="cam4-plane-sha256:different",
            expected_tool_support_plane_normal=(0.0, 0.0, 1.0),
        )

    validated = validate_worker_response(
        payload,
        metadata=metadata,
        pinned_model_digests=DIGESTS,
        request_started_unix_ms=now_ms,
        received_unix_ms=now_ms + 10,
        expected_tool_support_plane_config_version="cam4-plane-sha256:abc123",
        expected_tool_support_plane_normal=(0.0, 0.0, 1.0),
    )
    assert validated.tool_detections[0]["pose"]["orientation_valid"] is True

    low_depth_support = copy.deepcopy(payload)
    low_depth_support["results"]["tool"]["detections"][0]["pose"][
        "valid_depth_ratio"
    ] = 0.049999
    with pytest.raises(ContractError, match="minimum depth ratio"):
        validate_worker_response(
            low_depth_support,
            metadata=metadata,
            pinned_model_digests=DIGESTS,
            request_started_unix_ms=now_ms,
            received_unix_ms=now_ms + 10,
            expected_tool_support_plane_config_version="cam4-plane-sha256:abc123",
        )

    ambiguous_axis = copy.deepcopy(payload)
    ambiguous_axis["results"]["tool"]["detections"][0]["pose"][
        "axis_anisotropy"
    ] = 1.999999
    with pytest.raises(ContractError, match="axis anisotropy"):
        validate_worker_response(
            ambiguous_axis,
            metadata=metadata,
            pinned_model_digests=DIGESTS,
            request_started_unix_ms=now_ms,
            received_unix_ms=now_ms + 10,
            expected_tool_support_plane_config_version="cam4-plane-sha256:abc123",
        )

    unsigned_axis = copy.deepcopy(payload)
    unsigned_axis["results"]["tool"]["detections"][0]["pose"][
        "endpoint_sign_confidence"
    ] = 0.199999
    with pytest.raises(ContractError, match="endpoint sign confidence"):
        validate_worker_response(
            unsigned_axis,
            metadata=metadata,
            pinned_model_digests=DIGESTS,
            request_started_unix_ms=now_ms,
            received_unix_ms=now_ms + 10,
            expected_tool_support_plane_config_version="cam4-plane-sha256:abc123",
        )

    c2_axis = copy.deepcopy(payload)
    c2_detection = c2_axis["results"]["tool"]["detections"][0]
    c2_detection["canonical_class_id"] = 7
    c2_detection["model_class_index"] = 6
    c2_detection["class_name"] = "Army-Navy Retractor"
    c2_detection["pose"]["symmetry_type"] = "C2"
    c2_detection["pose"]["endpoint_sign_confidence"] = 0.0
    validated_c2 = validate_worker_response(
        c2_axis,
        metadata=metadata,
        pinned_model_digests=DIGESTS,
        request_started_unix_ms=now_ms,
        received_unix_ms=now_ms + 10,
        expected_tool_support_plane_config_version="cam4-plane-sha256:abc123",
        expected_tool_support_plane_normal=(0.0, 0.0, 1.0),
    )
    assert validated_c2.tool_detections[0]["pose"]["orientation_valid"] is True

    flipped_c2 = copy.deepcopy(c2_axis)
    flipped_c2["results"]["tool"]["detections"][0]["pose"][
        "orientation_xyzw"
    ] = [1.0, 0.0, 0.0, 0.0]
    with pytest.raises(ContractError, match=r"\+Z axis disagrees"):
        validate_worker_response(
            flipped_c2,
            metadata=metadata,
            pinned_model_digests=DIGESTS,
            request_started_unix_ms=now_ms,
            received_unix_ms=now_ms + 10,
            expected_tool_support_plane_config_version="cam4-plane-sha256:abc123",
            expected_tool_support_plane_normal=(0.0, 0.0, 1.0),
        )

    arbitrary_z = copy.deepcopy(payload)
    arbitrary_z["results"]["tool"]["detections"][0]["pose"][
        "orientation_xyzw"
    ] = [math.sqrt(0.5), 0.0, 0.0, math.sqrt(0.5)]
    with pytest.raises(ContractError, match=r"\+Z axis disagrees"):
        validate_worker_response(
            arbitrary_z,
            metadata=metadata,
            pinned_model_digests=DIGESTS,
            request_started_unix_ms=now_ms,
            received_unix_ms=now_ms + 10,
            expected_tool_support_plane_config_version="cam4-plane-sha256:abc123",
            expected_tool_support_plane_normal=(0.0, 0.0, 1.0),
        )

    # A non-authoritative position-only result remains observable even when no
    # support-plane identity is pinned locally.
    position_only = copy.deepcopy(payload)
    position_only["results"]["tool"]["support_plane_validated"] = False
    position_only["results"]["tool"]["support_plane_diagnostics"] = (
        _support_plane_diagnostics()
    )
    position_only["results"]["tool"]["detections"] = [
        _rgbd_tool_detection(orientation_xyzw=None)
    ]
    validated_position = validate_worker_response(
        position_only,
        metadata=metadata,
        pinned_model_digests=DIGESTS,
        request_started_unix_ms=now_ms,
        received_unix_ms=now_ms + 10,
    )
    assert validated_position.tool_detections[0]["pose"]["position_valid"] is True
    assert (
        validated_position.tool_detections[0]["pose"]["orientation_valid"] is False
    )

    with pytest.raises(ContractError, match="does not match the Taskplanner pin"):
        validate_worker_response(
            position_only,
            metadata=metadata,
            pinned_model_digests=DIGESTS,
            request_started_unix_ms=now_ms,
            received_unix_ms=now_ms + 10,
            expected_tool_support_plane_config_version=(
                "cam4-plane-sha256:different"
            ),
            expected_tool_support_plane_normal=(0.0, 0.0, 1.0),
        )


def test_rgbd_tool_pose_contract_is_pinned_and_geometry_is_cross_checked() -> None:
    now_ms = int(time.time() * 1000)
    _rgb, _depth, _color, _depth_info, metadata, payload = _rgbd_fixture(now_ms)
    payload["results"]["tool"]["detections"] = [
        _rgbd_tool_detection(orientation_xyzw=None)
    ]

    def validate(candidate):
        return validate_worker_response(
            candidate,
            metadata=metadata,
            pinned_model_digests=DIGESTS,
            request_started_unix_ms=now_ms,
            received_unix_ms=now_ms + 10,
        )

    assert validate(payload).tool_detections[0]["pose"]["position_valid"] is True

    mismatched_plane_fit = copy.deepcopy(payload)
    mismatched_plane_fit["results"]["tool"]["detections"][0]["pose"][
        "support_plane_inlier_ratio"
    ] = 0.949
    with pytest.raises(ContractError, match="fit disagrees with diagnostics"):
        validate(mismatched_plane_fit)

    for field, value, match in (
        ("ontology_version", "other-ontology", "ontology_version"),
        ("calibration_version", "other-alignment", "calibration_version"),
        ("pose_convention_version", "other-pose", "pose_convention_version"),
    ):
        candidate = copy.deepcopy(payload)
        candidate["results"]["tool"][field] = value
        with pytest.raises(ContractError, match=match):
            validate(candidate)

    candidate = copy.deepcopy(payload)
    pose = candidate["results"]["tool"]["detections"][0]["pose"]
    pose["pose_mode"] = "FULL_6D"
    with pytest.raises(ContractError, match="pinned planar Tool contract"):
        validate(candidate)

    candidate = copy.deepcopy(payload)
    pose = candidate["results"]["tool"]["detections"][0]["pose"]
    pose["dof_observed"] = [True, True, True, True, False, False]
    with pytest.raises(ContractError, match="dof_observed"):
        validate(candidate)

    candidate = copy.deepcopy(payload)
    pose = candidate["results"]["tool"]["detections"][0]["pose"]
    pose["axis_definition"] = "+Z guessed by remote worker"
    with pytest.raises(ContractError, match="axis_definition"):
        validate(candidate)

    candidate = copy.deepcopy(payload)
    pose = candidate["results"]["tool"]["detections"][0]["pose"]
    pose["position_m"][2] = 0.81
    with pytest.raises(ContractError, match="pose Z disagrees"):
        validate(candidate)

    candidate = copy.deepcopy(payload)
    pose = candidate["results"]["tool"]["detections"][0]["pose"]
    pose["position_m"][0] += 0.02
    with pytest.raises(ContractError, match="does not reproject"):
        validate(candidate)

    candidate = copy.deepcopy(payload)
    pose = candidate["results"]["tool"]["detections"][0]["pose"]
    pose["valid_depth_ratio"] = 0.5
    with pytest.raises(ContractError, match="point evidence"):
        validate(candidate)

    candidate = copy.deepcopy(payload)
    pose = candidate["results"]["tool"]["detections"][0]["pose"]
    pose["pose_point_count"] = 2
    with pytest.raises(ContractError, match="exceeds the instance mask area"):
        validate(candidate)

    candidate = copy.deepcopy(payload)
    observation = candidate["results"]["tool"]["detections"][0]["observation"]
    observation["observation_point_uv_px"] = [1.0, 0.0]
    with pytest.raises(ContractError, match="mask-membership"):
        validate(candidate)

    candidate = copy.deepcopy(payload)
    detection = candidate["results"]["tool"]["detections"][0]
    detection["canonical_class_id"] = 7
    with pytest.raises(ContractError, match="pinned Tool ontology"):
        validate(candidate)

    candidate = copy.deepcopy(payload)
    detection = candidate["results"]["tool"]["detections"][0]
    detection["model_class_index"] = 1
    with pytest.raises(ContractError, match="model_class_index disagrees"):
        validate(candidate)

    candidate = copy.deepcopy(payload)
    observation = candidate["results"]["tool"]["detections"][0]["observation"]
    observation["observation_point_uv_px"] = [1.0, 0.0]
    observation["observation_point_inside_mask"] = False
    with pytest.raises(ContractError, match="outside the instance mask"):
        validate(candidate)


def test_response_rejects_source_digest_and_freshness_mismatch() -> None:
    now_ms = int(time.time() * 1000)
    metadata = _metadata(now_ms=now_ms)

    wrong_source = _response(metadata, generated_unix_ms=now_ms + 10)
    wrong_source["source"] = dict(wrong_source["source"])
    wrong_source["source"]["rgb"] = dict(wrong_source["source"]["rgb"])
    wrong_source["source"]["rgb"]["frame_id"] = "other_camera"
    with pytest.raises(ContractError, match="source identity"):
        validate_worker_response(
            wrong_source,
            metadata=metadata,
            pinned_model_digests=DIGESTS,
            request_started_unix_ms=now_ms,
            received_unix_ms=now_ms + 20,
        )

    wrong_digest = _response(metadata, generated_unix_ms=now_ms + 10)
    wrong_digest["models"]["tool"]["digest_sha256"] = "f" * 64
    with pytest.raises(ContractError, match="digests changed"):
        validate_worker_response(
            wrong_digest,
            metadata=metadata,
            pinned_model_digests=DIGESTS,
            request_started_unix_ms=now_ms,
            received_unix_ms=now_ms + 20,
        )

    late = _response(metadata, generated_unix_ms=now_ms + 10)
    with pytest.raises(ContractError, match="deadline"):
        validate_worker_response(
            late,
            metadata=metadata,
            pinned_model_digests=DIGESTS,
            request_started_unix_ms=now_ms,
            received_unix_ms=now_ms + 2_001,
        )


def test_real_hand_result_shape_requires_bounded_handedness_and_keypoints() -> None:
    now_ms = int(time.time() * 1000)
    metadata = _metadata(now_ms=now_ms, algorithms=("hand",))
    payload = _response(metadata, generated_unix_ms=now_ms + 5)
    payload["results"]["hand"]["hands"] = [
        {
            "hand_index": 0,
            "handedness": {"label": "Left", "score": 0.94},
            "joints_2d": [[100.0 + index, 200.0 + index] for index in range(21)],
            "kp_scores": [1.0] * 21,
        }
    ]
    validated = validate_worker_response(
        payload,
        metadata=metadata,
        pinned_model_digests={"hand": DIGESTS["hand"]},
        request_started_unix_ms=now_ms,
        received_unix_ms=now_ms + 10,
    )
    assert validated.hands[0]["handedness"] == {
        "label": "left",
        "score": 0.94,
    }


def test_pnu_tool_ontology_projects_to_existing_semantics_without_boxes() -> None:
    semantics = build_cam4_semantics(
        [
            {
                "canonical_class_id": 1,
                "class_name": "Scalpel",
                "confidence": 0.9,
                "bbox_xyxy_px": [1, 2, 3, 4],
            },
            {
                "canonical_class_id": 1,
                "class_name": "Scalpel",
                "confidence": 0.8,
                "bbox_xyxy_px": [5, 6, 7, 8],
            },
            {
                "canonical_class_id": 7,
                "class_name": "Army-Navy Retractor",
                "confidence": 0.7,
            },
        ],
        source_stamp_sec=11.25,
        inference_latency_ms=14.0,
    )
    assert semantics["schema"] == "taskplanner.cam4_semantics.v1"
    assert semantics["source"] == "cam4_rfdetr_small"
    assert semantics["provider"] == "pnu_hand_blood"
    assert semantics["tools"] == [
        {
            "name": "#15 Scalpel",
            "count": 2,
            "max_confidence": 0.9,
            "mean_confidence": 0.85,
        },
        {
            "name": "Army navy retractor",
            "count": 1,
            "max_confidence": 0.7,
            "mean_confidence": 0.7,
        },
    ]
    encoded = json.dumps(semantics)
    assert "bbox" not in encoded
    assert semantics["tool_request"]["state"] == "uncertain"


def test_pnu_compatibility_names_resolve_against_real_thyroid_catalogs() -> None:
    raw_names = (
        "Scalpel",
        "Allis Forceps",
        "Mosquito",
        "Adson Forceps",
        "Bipolar Forceps",
        "Bovie",
        "Army-Navy Retractor",
        "Thyroid Retractor",
    )
    detections = [
        {
            "canonical_class_id": index,
            "class_name": name,
            "confidence": 0.9,
        }
        for index, name in enumerate(raw_names, start=1)
    ]
    semantics = build_cam4_semantics(
        detections,
        source_stamp_sec=11.25,
        inference_latency_ms=14.0,
    )
    compatibility_names = [row["name"] for row in semantics["tools"]]
    spec_root = (
        Path(__file__).parents[2] / "procedure_spec" / "procedure_spec" / "specs"
    )
    demo_spec = load_bundle(spec_root / "thyroidectomy_demo")
    normal_spec = load_bundle(spec_root / "thyroidectomy")

    # This reproduces why the provider labels cannot be forwarded raw: only a
    # subset happens to be an alias in each active procedure catalog.
    assert (
        sum(demo_spec.resolve_instrument_alias(name) is not None for name in raw_names)
        == 4
    )
    assert (
        sum(
            normal_spec.resolve_instrument_alias(name) is not None for name in raw_names
        )
        == 3
    )

    assert {
        name: demo_spec.resolve_instrument_alias(name) for name in compatibility_names
    } == {
        "#15 Scalpel": "T01",
        "Adson forceps": "T02",
        "Allis clamp forceps": "T03",
        "Army navy retractor": "T05",
        "Bipolar cautery": "T07",
        "Bovie surgical cautery": "T04",
        "Mosquito forceps": "T08",
        "Thyroid retractor": "T11",
    }
    normal_resolved = {
        name: normal_spec.resolve_instrument_alias(name) for name in compatibility_names
    }
    assert sum(value is not None for value in normal_resolved.values()) == 7
    assert normal_resolved["Thyroid retractor"] is None
    assert "T11" not in normal_spec.list_instrument_ids()


def test_unknown_or_mismatched_pnu_tool_never_reaches_compatibility_mayo_path() -> None:
    semantics = build_cam4_semantics(
        [
            {
                "canonical_class_id": 999,
                "class_name": "Unknown powered tool",
                "confidence": 0.99,
            },
            {
                "canonical_class_id": 1,
                "class_name": "Thyroid Retractor",
                "confidence": 0.99,
            },
        ],
        source_stamp_sec=1.0,
        inference_latency_ms=1.0,
    )
    assert semantics["tools"] == []

    tracker = Cam4MayoPlacementTracker(
        min_stable_samples=1,
        min_stable_duration_sec=0.0,
    )
    assert tracker.update(semantics) == []


class _Publisher:
    def __init__(self) -> None:
        self.messages = []

    def publish(self, message) -> None:
        self.messages.append(message)


def test_pose_projection_uses_camera_info_distortion_and_rejects_unknown_model() -> (
    None
):
    camera = _camera_info(10_000_000_000, width=200, height=100)
    camera.payload["d"] = [1.0, 0.0, 0.0, 0.0, 0.0]
    pixel = bridge_module._project_camera_point((0.1, 0.0, 1.0), camera)
    # Pinhole would be cx + 10.0. k1=1 increases normalized x to 0.101.
    assert pixel == pytest.approx((110.1, 50.0))

    camera.payload["distortion_model"] = "equidistant"
    camera.payload["d"] = [0.0, 0.0, 0.0, 0.0]
    fisheye_pixel = bridge_module._project_camera_point((0.1, 0.0, 1.0), camera)
    assert fisheye_pixel[0] < 110.0

    camera.payload["distortion_model"] = "unsupported_camera_model"
    with pytest.raises(ContractError, match="does not support distortion model"):
        bridge_module._project_camera_point((0.1, 0.0, 1.0), camera)


def test_pose_overlay_draws_only_valid_axes_and_position_only_is_distinct() -> None:
    stamp_ns = 10_000_000_000
    width, height = 128, 96
    rgb = _real_frame(stamp_ns, width=width, height=height)
    camera = _camera_info(stamp_ns, width=width, height=height)
    half_angle = math.radians(15.0)
    oriented = _pose_array(
        rgb,
        orientation_xyzw=(math.sin(half_angle), 0.0, 0.0, math.cos(half_angle)),
    )
    rendered = build_pnu_pose_overlay(
        frame=rgb,
        pose_array=oriented,
        color_camera_info=camera,
        max_pixels=width * height,
    )
    assert rendered.message.format == "webp"
    assert rendered.message.header.stamp.sec == rgb.stamp_ns // 1_000_000_000
    assert rendered.message.header.stamp.nanosec == rgb.stamp_ns % 1_000_000_000
    assert rendered.message.header.frame_id == rgb.frame_id
    assert rendered.drawn_axis_count == 1
    assert rendered.drawn_position_only_count == 0
    with Image.open(BytesIO(bytes(rendered.message.data))) as image:
        rgba = image.convert("RGBA")
        colors = {color for _count, color in (rgba.getcolors(width * height) or [])}
        assert rgba.size == (width, height)
        assert (255, 0, 0, 255) in colors
        assert (0, 210, 0, 255) in colors
        assert (0, 80, 255, 255) in colors

    position_only = _pose_array(rgb, orientation_xyzw=None)
    position_rendered = build_pnu_pose_overlay(
        frame=rgb,
        pose_array=position_only,
        color_camera_info=camera,
        max_pixels=width * height,
    )
    assert position_rendered.drawn_axis_count == 0
    assert position_rendered.drawn_position_only_count == 1
    with Image.open(BytesIO(bytes(position_rendered.message.data))) as image:
        rgba = image.convert("RGBA")
        colors = {color for _count, color in (rgba.getcolors(width * height) or [])}
        assert (255, 171, 64, 255) in colors
        assert (255, 0, 0, 255) not in colors
        assert (0, 210, 0, 255) not in colors
        assert (0, 80, 255, 255) not in colors

    empty = _pose_array(rgb)
    empty.tools = []
    empty_rendered = build_pnu_pose_overlay(
        frame=rgb,
        pose_array=empty,
        color_camera_info=None,
        max_pixels=width * height,
    )
    assert empty_rendered.drawn_axis_count == 0
    assert empty_rendered.drawn_position_only_count == 0
    with Image.open(BytesIO(bytes(empty_rendered.message.data))) as image:
        assert image.convert("RGBA").getchannel("A").getextrema() == (0, 0)


def test_pose_overlay_uses_xyzw_rotation_columns_without_transpose(monkeypatch) -> None:
    stamp_ns = 10_000_000_000
    width, height = 128, 96
    rgb = _real_frame(stamp_ns, width=width, height=height)
    camera = _camera_info(stamp_ns, width=width, height=height)
    sine = math.sqrt(0.5)
    quaternion = (0.0, 0.0, sine, sine)
    rotation = bridge_module._quaternion_xyzw_to_rotation_matrix(quaternion)
    expected_rotation = (
        (0.0, -1.0, 0.0),
        (1.0, 0.0, 0.0),
        (0.0, 0.0, 1.0),
    )
    for actual_row, expected_row in zip(rotation, expected_rotation, strict=True):
        assert actual_row == pytest.approx(expected_row, abs=1e-12)

    arrows = []

    def capture_arrow(_draw, *, origin, endpoint, color, line_width):
        arrows.append((origin, endpoint, color, line_width))

    monkeypatch.setattr(bridge_module, "_draw_pose_arrow", capture_arrow)
    build_pnu_pose_overlay(
        frame=rgb,
        pose_array=_pose_array(rgb, orientation_xyzw=quaternion),
        color_camera_info=camera,
        max_pixels=width * height,
    )
    by_color = {arrow[2][:3]: arrow for arrow in arrows}
    red_origin, red_endpoint, _red, _red_width = by_color[(255, 0, 0)]
    green_origin, green_endpoint, _green, _green_width = by_color[(0, 210, 0)]
    # Local +X rotates to camera +Y (down); local +Y rotates to camera -X.
    assert red_endpoint[0] == pytest.approx(red_origin[0], abs=1e-9)
    assert red_endpoint[1] > red_origin[1]
    assert green_endpoint[0] < green_origin[0]
    assert green_endpoint[1] == pytest.approx(green_origin[1], abs=1e-9)


def test_pose_overlay_is_bounded_exact_identity_and_malformed_fail_closed() -> None:
    stamp_ns = 10_000_000_000
    width, height = 128, 96
    rgb = _real_frame(stamp_ns, width=width, height=height)
    camera = _camera_info(stamp_ns, width=width, height=height)
    pose_array = _pose_array(rgb, orientation_xyzw=(0.0, 0.0, 0.0, 1.0))

    with pytest.raises(ValueError, match="axis_length_m"):
        build_pnu_pose_overlay(
            frame=rgb,
            pose_array=pose_array,
            color_camera_info=camera,
            axis_length_m=1.0,
            max_pixels=width * height,
        )
    with pytest.raises(ContractError, match="exceeding"):
        build_pnu_pose_overlay(
            frame=rgb,
            pose_array=pose_array,
            color_camera_info=camera,
            max_pixels=width * height - 1,
        )

    wrong_stamp = _pose_array(rgb, orientation_xyzw=(0.0, 0.0, 0.0, 1.0))
    wrong_stamp.header.stamp.nanosec += 1
    with pytest.raises(ContractError, match="identity"):
        build_pnu_pose_overlay(
            frame=rgb,
            pose_array=wrong_stamp,
            color_camera_info=camera,
            max_pixels=width * height,
        )

    bad_quaternion = _pose_array(rgb, orientation_xyzw=(0.0, 0.0, 0.0, 2.0))
    with pytest.raises(ContractError, match="unit length"):
        build_pnu_pose_overlay(
            frame=rgb,
            pose_array=bad_quaternion,
            color_camera_info=camera,
            max_pixels=width * height,
        )

    wrong_camera = _camera_info(
        stamp_ns,
        width=width + 1,
        height=height,
    )
    with pytest.raises(ContractError, match="CameraInfo does not match"):
        build_pnu_pose_overlay(
            frame=rgb,
            pose_array=pose_array,
            color_camera_info=wrong_camera,
            max_pixels=width * height,
        )


def test_pose_overlay_rate_gate_bypasses_visibility_transitions_to_clear_stale(
    monkeypatch,
) -> None:
    stamp_ns = 10_000_000_000
    width, height = 128, 96
    rgb = _real_frame(stamp_ns, width=width, height=height)
    camera = _camera_info(stamp_ns, width=width, height=height)
    node = PNUPerceptionBridgeNode.__new__(PNUPerceptionBridgeNode)
    node._pose_overlay_pub = _Publisher()
    node._pose_overlay_enabled = True
    node._pose_overlay_max_rate_hz = 15.0
    node._pose_overlay_max_pixels = width * height
    node._pose_axis_length_m = 0.05
    node._last_pose_overlay_published_monotonic = 0.0
    node._last_pose_overlay_signature = (0, 0)

    oriented = _pose_array(rgb, orientation_xyzw=(0.0, 0.0, 0.0, 1.0))
    first = node._publish_pose_overlay(
        rgb=rgb,
        pose_array=oriented,
        color_camera_info=camera,
    )
    original_renderer = bridge_module.build_pnu_pose_overlay
    monkeypatch.setattr(
        bridge_module,
        "build_pnu_pose_overlay",
        lambda **_kwargs: pytest.fail("rate-limited frame must not be encoded"),
    )
    repeated = node._publish_pose_overlay(
        rgb=rgb,
        pose_array=oriented,
        color_camera_info=camera,
    )
    monkeypatch.setattr(
        bridge_module,
        "build_pnu_pose_overlay",
        original_renderer,
    )
    assert first["pose_overlay_published"] is True
    assert first["pose_overlay_status"] == "published"
    assert first["pose_overlay_drawn_axis_count"] == 1
    assert repeated["pose_overlay_published"] is False
    assert repeated["pose_overlay_status"] == "rate_limited"

    position_only = _pose_array(rgb, orientation_xyzw=None)
    position_result = node._publish_pose_overlay(
        rgb=rgb,
        pose_array=position_only,
        color_camera_info=camera,
    )
    assert position_result["pose_overlay_published"] is True
    assert position_result["pose_overlay_drawn_axis_count"] == 0
    assert position_result["pose_overlay_drawn_position_only_count"] == 1

    empty = _pose_array(rgb)
    empty.tools = []
    clear = node._publish_pose_overlay(
        rgb=rgb,
        pose_array=empty,
        color_camera_info=None,
    )
    assert clear["pose_overlay_published"] is True
    assert clear["pose_overlay_status"] == "published"
    assert clear["pose_overlay_drawn_axis_count"] == 0
    assert clear["pose_overlay_drawn_position_only_count"] == 0
    with Image.open(BytesIO(bytes(node._pose_overlay_pub.messages[-1].data))) as image:
        assert image.convert("RGBA").getchannel("A").getextrema() == (0, 0)

    repeated_clear = node._publish_pose_overlay(
        rgb=rgb,
        pose_array=empty,
        color_camera_info=None,
    )
    assert repeated_clear["pose_overlay_status"] == "rate_limited"
    assert len(node._pose_overlay_pub.messages) == 3

    malformed = _pose_array(rgb, orientation_xyzw=(0.0, 0.0, 0.0, 2.0))
    failed = node._publish_pose_overlay(
        rgb=rgb,
        pose_array=malformed,
        color_camera_info=camera,
    )
    assert failed["pose_overlay_published"] is False
    assert failed["pose_overlay_status"] == "render_error"
    assert len(node._pose_overlay_pub.messages) == 3


def test_failure_or_stale_state_publishes_one_transparent_overlay_clear() -> None:
    stamp_ns = 10_000_000_000
    width, height = 128, 96
    rgb = _real_frame(stamp_ns, width=width, height=height)
    node = PNUPerceptionBridgeNode.__new__(PNUPerceptionBridgeNode)
    node._overlay_pub = _Publisher()
    node._pose_overlay_pub = _Publisher()
    node._overlay_enabled = True
    node._pose_overlay_enabled = True
    node._pose_overlay_max_pixels = width * height
    node._last_overlay_visible = True
    node._last_pose_overlay_signature = (1, 0)
    node._last_overlay_published_monotonic = 0.0
    node._last_pose_overlay_published_monotonic = 0.0

    assert node._clear_stale_overlays(rgb) is True
    assert len(node._overlay_pub.messages) == 1
    assert len(node._pose_overlay_pub.messages) == 1
    for message in (
        node._overlay_pub.messages[0],
        node._pose_overlay_pub.messages[0],
    ):
        assert message.header.stamp.sec == rgb.stamp_ns // 1_000_000_000
        assert message.header.stamp.nanosec == rgb.stamp_ns % 1_000_000_000
        assert message.header.frame_id == rgb.frame_id
        with Image.open(BytesIO(bytes(message.data))) as image:
            assert image.convert("RGBA").getchannel("A").getextrema() == (0, 0)
    assert node._clear_stale_overlays(rgb) is False
    assert len(node._overlay_pub.messages) == 1
    assert len(node._pose_overlay_pub.messages) == 1


def test_overlay_is_exact_stamp_rate_limited_and_fail_closed() -> None:
    now_ms = int(time.time() * 1000)
    rgb, _depth, _color, _depth_info, metadata, payload = _rgbd_fixture(now_ms)
    validated = validate_worker_response(
        payload,
        metadata=metadata,
        pinned_model_digests=DIGESTS,
        request_started_unix_ms=now_ms,
        received_unix_ms=now_ms + 10,
    )
    node = PNUPerceptionBridgeNode.__new__(PNUPerceptionBridgeNode)
    node._overlay_pub = _Publisher()
    node._overlay_enabled = True
    node._overlay_max_rate_hz = 5.0
    node._overlay_max_pixels = 1_000
    node._last_overlay_published_monotonic = 0.0

    first = node._publish_debug_overlay(rgb=rgb, validated=validated)
    second = node._publish_debug_overlay(rgb=rgb, validated=validated)
    assert first["overlay_published"] is True
    assert first["overlay_status"] == "published"
    assert second["overlay_published"] is False
    assert second["overlay_status"] == "rate_limited"
    assert len(node._overlay_pub.messages) == 1

    node._last_overlay_published_monotonic = 0.0
    malformed_rgb = BinaryFrame(
        received_monotonic=rgb.received_monotonic,
        stamp_ns=rgb.stamp_ns,
        frame_id=rgb.frame_id,
        format=rgb.format,
        data=b"not-an-image",
    )
    failed = node._publish_debug_overlay(
        rgb=malformed_rgb,
        validated=validated,
    )
    assert failed["overlay_published"] is False
    assert failed["overlay_status"] == "render_error"
    assert len(node._overlay_pub.messages) == 1

    wrong_stamp = BinaryFrame(
        received_monotonic=rgb.received_monotonic,
        stamp_ns=rgb.stamp_ns + 1,
        frame_id=rgb.frame_id,
        format=rgb.format,
        data=rgb.data,
    )
    with pytest.raises(ContractError, match="identity"):
        build_pnu_debug_overlay(
            frame=wrong_stamp,
            validated=validated,
            max_pixels=1_000,
        )


def test_spoofed_tool_class_name_is_rejected_before_overlay() -> None:
    now_ms = int(time.time() * 1000)
    width, height = 128, 96
    rgb = _real_frame(now_ms * 1_000_000, width=width, height=height)
    metadata = build_request_metadata(
        request_id="adversarial-tool-name-overlay",
        rgb=rgb,
        depth=None,
        color_camera_info=None,
        depth_camera_info=None,
        requested_algorithms=("tool",),
        deadline_unix_ms=now_ms + 2_000,
    )
    payload = _response(metadata, generated_unix_ms=now_ms + 5)
    payload["results"]["tool"]["image"] = {"width": width, "height": height}
    mask_x, mask_y = 60, 60
    mask_offset = mask_x * height + mask_y
    payload["results"]["tool"]["detections"] = [
        {
            "instance_id": 0,
            "canonical_class_id": 1,
            # This label is adversarial but accepted by the upstream response
            # contract. Rendering identity must still come from the Tool
            # result collection, never this user/model-controlled string.
            "class_name": "blood",
            "confidence": 0.9,
            "bbox_xyxy_px": [40.0, 40.0, 80.0, 80.0],
            "mask_rle": {
                "size": [height, width],
                "counts": [
                    mask_offset,
                    1,
                    width * height - mask_offset - 1,
                ],
            },
        }
    ]
    with pytest.raises(ContractError, match="pinned Tool ontology"):
        validate_worker_response(
            payload,
            metadata=metadata,
            pinned_model_digests={"tool": DIGESTS["tool"]},
            request_started_unix_ms=now_ms,
            received_unix_ms=now_ms + 10,
        )


def test_low_confidence_hand_is_transparent_and_not_counted_as_drawn() -> None:
    now_ms = int(time.time() * 1000)
    width, height = 128, 96
    rgb = _real_frame(now_ms * 1_000_000, width=width, height=height)
    metadata = build_request_metadata(
        request_id="low-confidence-hand-overlay",
        rgb=rgb,
        depth=None,
        color_camera_info=None,
        depth_camera_info=None,
        requested_algorithms=("hand",),
        deadline_unix_ms=now_ms + 2_000,
    )
    payload = _response(metadata, generated_unix_ms=now_ms + 5)
    payload["results"]["hand"]["image"] = {"width": width, "height": height}
    payload["results"]["hand"]["hands"] = [
        {
            "hand_index": 0,
            "handedness": {"label": "unknown", "score": 0.9},
            "joints_2d": [[64.0, 48.0] for _ in range(21)],
            "kp_scores": [0.19] * 21,
        }
    ]
    validated = validate_worker_response(
        payload,
        metadata=metadata,
        pinned_model_digests={"hand": DIGESTS["hand"]},
        request_started_unix_ms=now_ms,
        received_unix_ms=now_ms + 10,
    )

    rendered = build_pnu_debug_overlay(
        frame=rgb,
        validated=validated,
        max_pixels=width * height,
    )
    assert len(validated.hands) == 1
    assert rendered.drawn_hand_count == 0
    with Image.open(BytesIO(bytes(rendered.message.data))) as overlay:
        assert overlay.convert("RGBA").getchannel("A").getextrema() == (0, 0)

    node = PNUPerceptionBridgeNode.__new__(PNUPerceptionBridgeNode)
    node._overlay_pub = _Publisher()
    node._overlay_enabled = True
    node._overlay_max_rate_hz = 5.0
    node._overlay_max_pixels = width * height
    node._last_overlay_published_monotonic = 0.0
    overlay_diagnostics = node._publish_debug_overlay(
        rgb=rgb,
        validated=validated,
    )
    assert overlay_diagnostics["overlay_published"] is True
    assert overlay_diagnostics["overlay_drawn_hand_count"] == 0
    with Image.open(BytesIO(bytes(node._overlay_pub.messages[0].data))) as overlay:
        assert overlay.convert("RGBA").getchannel("A").getextrema() == (0, 0)


def test_successful_empty_execution_publishes_empty_semantics_and_ready_health() -> (
    None
):
    now_ms = int(time.time() * 1000)
    metadata = _metadata(now_ms=now_ms)
    payload = _response(metadata, generated_unix_ms=now_ms + 5)
    validated = validate_worker_response(
        payload,
        metadata=metadata,
        pinned_model_digests=DIGESTS,
        request_started_unix_ms=now_ms,
        received_unix_ms=now_ms + 10,
    )

    node = PNUPerceptionBridgeNode.__new__(PNUPerceptionBridgeNode)
    node._semantics_pub = _Publisher()
    node._mayo_pub = _Publisher()
    node._diagnostics_pub = _Publisher()
    node._tool_pose_pub = _Publisher()
    node._tool_observations_pub = _Publisher()
    node._blood_semantics_pub = _Publisher()
    node._hand_keypoints_pub = _Publisher()
    node._mayo_tracker = Cam4MayoPlacementTracker()
    node._requested_algorithms = ("tool", "blood", "hand")
    node._sequence = 0
    node._dropped_frames = 0
    node._last_success_monotonic = 0.0
    node._last_success_source_stamp_ns = 0
    node._last_detection_count = -1
    node._auth_mode = "none_local"
    node._transport_mode = "http_local"
    health = []
    node._publish_health = lambda **kwargs: health.append(kwargs)

    rgb = _frame(now_ms * 1_000_000)
    node._publish_success(
        rgb=rgb,
        depth=None,
        color_info=None,
        depth_info=None,
        request_id=str(metadata["request_id"]),
        validated=validated,
        source_to_output_latency_ms=20.0,
    )

    semantics = json.loads(node._semantics_pub.messages[0].data)
    diagnostics = json.loads(node._diagnostics_pub.messages[0].data)
    assert semantics["tools"] == []
    assert diagnostics["empty_detection_result"] is True
    assert diagnostics["requested_algorithms"] == ["tool", "blood", "hand"]
    assert diagnostics["executed_algorithms"] == ["tool", "blood", "hand"]
    assert diagnostics["instance_count"] == 0
    assert diagnostics["auth_mode"] == "none_local"
    assert diagnostics["transport_mode"] == "http_local"
    assert len(node._tool_pose_pub.messages) == 1
    assert node._tool_pose_pub.messages[0].tools == []
    assert len(node._tool_observations_pub.messages) == 1
    assert node._tool_observations_pub.messages[0].instances == []
    assert (
        json.loads(node._blood_semantics_pub.messages[0].data)["metric_3d_ready"]
        is False
    )
    assert node._hand_keypoints_pub.messages[0].hands == []
    assert node._hand_keypoints_pub.messages[0].depth_source == "2d_only"
    assert health[-1]["connected"] is True
    assert health[-1]["status"] == "ready"
    assert health[-1]["detection_count"] == 0
    assert health[-1]["semantic_ready"] is True


def test_partial_blood_hand_execution_never_publishes_tool_semantics() -> None:
    now_ms = int(time.time() * 1000)
    metadata = _metadata(now_ms=now_ms, algorithms=("blood", "hand"))
    payload = _response(metadata, generated_unix_ms=now_ms + 5)
    subset_digests = {"blood": DIGESTS["blood"], "hand": DIGESTS["hand"]}
    validated = validate_worker_response(
        payload,
        metadata=metadata,
        pinned_model_digests=subset_digests,
        request_started_unix_ms=now_ms,
        received_unix_ms=now_ms + 10,
    )

    node = PNUPerceptionBridgeNode.__new__(PNUPerceptionBridgeNode)
    node._semantics_pub = _Publisher()
    node._mayo_pub = _Publisher()
    node._diagnostics_pub = _Publisher()
    node._tool_pose_pub = _Publisher()
    node._tool_observations_pub = _Publisher()
    node._blood_semantics_pub = _Publisher()
    node._hand_keypoints_pub = _Publisher()
    node._mayo_tracker = Cam4MayoPlacementTracker()
    node._requested_algorithms = ("blood", "hand")
    node._sequence = 0
    node._dropped_frames = 0
    node._last_success_monotonic = 0.0
    node._last_success_source_stamp_ns = 0
    node._last_detection_count = -1
    health = []
    node._publish_health = lambda **kwargs: health.append(kwargs)

    node._publish_success(
        rgb=_frame(now_ms * 1_000_000),
        depth=None,
        color_info=None,
        depth_info=None,
        request_id=str(metadata["request_id"]),
        validated=validated,
        source_to_output_latency_ms=20.0,
    )

    assert node._semantics_pub.messages == []
    assert node._mayo_pub.messages == []
    assert len(node._diagnostics_pub.messages) == 1
    assert health[-1]["connected"] is True
    assert health[-1]["status"] == "partial_ready"
    assert health[-1]["semantic_ready"] is False
    diagnostics = json.loads(node._diagnostics_pub.messages[-1].data)
    assert diagnostics["executed_algorithms"] == ["blood", "hand"]


def test_ros_health_contract_separates_connection_from_semantic_readiness() -> None:
    node = PNUPerceptionBridgeNode.__new__(PNUPerceptionBridgeNode)
    node._enabled = True
    node._pinned_model_digests = dict(DIGESTS)
    node._requested_algorithms = ("blood", "hand")
    node._auth_mode = "none_local"
    node._transport_mode = "http_local"
    node._health_pub = _Publisher()
    node.get_clock = lambda: SimpleNamespace(
        now=lambda: SimpleNamespace(
            to_msg=lambda: SimpleNamespace(sec=100, nanosec=200)
        )
    )

    node._publish_health(
        connected=True,
        status="partial_ready",
        source_stamp_ns=50_000_000_001,
        detection_count=0,
        error_code="",
        error_message="",
        semantic_ready=False,
    )
    partial = json.loads(node._health_pub.messages[-1].data)
    assert partial["connected"] is True
    assert partial["status"] == "partial_ready"
    assert partial["semantic_ready"] is False
    assert partial["cam4_aligned"] is False
    assert partial["auth_mode"] == "none_local"
    assert partial["transport_mode"] == "http_local"
    assert partial["executed_algorithms"] == ["blood", "hand"]
    assert partial["metric_3d_ready"] is False
    assert partial["metric_3d_reasons"] == ["no_validated_metric_3d_result"]
    assert partial["support_plane_config_version"] == ""
    assert partial["support_plane_validated"] is False

    node._publish_health(
        connected=True,
        status="ready",
        source_stamp_ns=50_000_000_002,
        detection_count=0,
        error_code="",
        error_message="",
        semantic_ready=True,
    )
    ready = json.loads(node._health_pub.messages[-1].data)
    assert ready["semantic_ready"] is True
    assert ready["cam4_aligned"] is True
    assert ready["empty_detection_result"] is True

    node._publish_health(
        connected=True,
        status="ready",
        source_stamp_ns=50_000_000_003,
        detection_count=0,
        error_code="",
        error_message="",
        semantic_ready=True,
        depth_aligned=True,
        metric_3d_ready=True,
        metric_3d_reasons=[],
        support_plane_config_version="pnu-cam4-reference-plane-v1",
        support_plane_validated=True,
    )
    metric = json.loads(node._health_pub.messages[-1].data)
    assert metric["depth_aligned"] is True
    assert metric["metric_3d_ready"] is True
    assert metric["metric_3d_reasons"] == []
    assert metric["support_plane_config_version"] == ("pnu-cam4-reference-plane-v1")
    assert metric["support_plane_validated"] is True


def test_http_path_uses_raw_multipart_bytes_and_never_falls_back() -> None:
    now_ns = time.time_ns()
    rgb = _real_frame(now_ns, width=1280, height=720)
    depth = _real_frame(
        now_ns + 10_000_000,
        depth=True,
        width=1280,
        height=720,
        frame_id="cam_4_depth_optical_frame",
    )
    posts = []
    successes = []
    failures = []

    class _Response:
        def __init__(self, payload):
            self._body = json.dumps(payload).encode("utf-8")
            self.headers = {"Content-Length": str(len(self._body))}
            self.closed = False

        def raise_for_status(self):
            return None

        def iter_content(self, *, chunk_size):
            assert chunk_size == 64 * 1024
            yield self._body

        def close(self):
            self.closed = True

    class _Session:
        def post(self, url, *, files, timeout, stream, allow_redirects):
            metadata = json.loads(files["metadata"][1])
            posts.append((url, files, timeout, stream, allow_redirects))
            return _Response(
                _response(
                    metadata,
                    generated_unix_ms=int(time.time() * 1000),
                )
            )

    node = PNUPerceptionBridgeNode.__new__(PNUPerceptionBridgeNode)
    node._max_source_age_sec = 2.0
    node._request_timeout_sec = 2.0
    node._requested_algorithms = ("tool", "blood", "hand")
    node._max_worker_clock_skew_ms = 1_000
    node._service_url = "http://192.168.1.20:8020"
    node._session = _Session()
    node._condition = threading.Condition()
    node._enabled = True
    node._running = True
    node._last_published_stamp_ns = 0
    node._ensure_capabilities = lambda: dict(DIGESTS)
    node._publish_success = lambda **kwargs: successes.append(kwargs)
    node._publish_failure = lambda *args: failures.append(args)
    node.get_logger = lambda: SimpleNamespace(warning=lambda *_args, **_kwargs: None)

    node._process_frame(rgb, depth, None, None)

    assert len(posts) == 1
    assert posts[0][0] == "http://192.168.1.20:8020/v1/infer"
    assert posts[0][1]["rgb"][1] is rgb.data
    assert posts[0][1]["depth"][1] is depth.data
    assert posts[0][1]["metadata"][2] == "application/json"
    assert posts[0][1]["rgb"][2] == "image/jpeg"
    assert posts[0][3] is True
    assert posts[0][4] is False
    assert failures == []
    assert len(successes) == 1
    assert successes[0]["validated"].tool_detections == ()


def test_streamed_json_reader_bounds_content_length_chunks_and_deadline() -> None:
    class _Response:
        def __init__(self, chunks, content_length=None):
            self._chunks = chunks
            self.headers = {}
            if content_length is not None:
                self.headers["Content-Length"] = str(content_length)
            self.closed = False

        def raise_for_status(self):
            return None

        def iter_content(self, *, chunk_size):
            assert chunk_size == 64 * 1024
            yield from self._chunks

        def close(self):
            self.closed = True

    valid = _Response([b'{"ready":', b"true}"])
    assert bridge_module._read_bounded_json_response(
        valid,
        field="test",
        deadline_monotonic=time.monotonic() + 1.0,
        maximum_bytes=64,
    ) == {"ready": True}
    assert valid.closed is True

    declared_oversize = _Response([], content_length=65)
    with pytest.raises(ContractError, match="oversized"):
        bridge_module._read_bounded_json_response(
            declared_oversize,
            field="test",
            deadline_monotonic=time.monotonic() + 1.0,
            maximum_bytes=64,
        )
    assert declared_oversize.closed is True

    streamed_oversize = _Response([b"x" * 40, b"y" * 40])
    with pytest.raises(ContractError, match="oversized"):
        bridge_module._read_bounded_json_response(
            streamed_oversize,
            field="test",
            deadline_monotonic=time.monotonic() + 1.0,
            maximum_bytes=64,
        )
    assert streamed_oversize.closed is True

    expired = _Response([b"{}"])
    with pytest.raises(ContractError, match="deadline"):
        bridge_module._read_bounded_json_response(
            expired,
            field="test",
            deadline_monotonic=time.monotonic() - 1.0,
            maximum_bytes=64,
        )
    assert expired.closed is True


def test_streamed_json_reader_enforces_absolute_deadline_against_slow_drip() -> None:
    body = b'{"ready":true}'
    first_byte_sent = threading.Event()

    class _SlowHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            return

        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            for byte in body:
                try:
                    self.wfile.write(bytes((byte,)))
                    self.wfile.flush()
                    first_byte_sent.set()
                except (BrokenPipeError, ConnectionResetError):
                    return
                time.sleep(0.04)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _SlowHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    response = bridge_module.requests.get(
        f"http://127.0.0.1:{server.server_port}/slow",
        headers={"Accept-Encoding": "identity"},
        timeout=1.0,
        stream=True,
        allow_redirects=False,
    )
    started = time.monotonic()
    try:
        with pytest.raises(
            ContractError,
            match="exceeded the monotonic request deadline",
        ):
            bridge_module._read_bounded_json_response(
                response,
                field="slow worker",
                deadline_monotonic=started + 0.18,
                maximum_bytes=64,
            )
        assert time.monotonic() - started < 0.40
        assert first_byte_sent.wait(timeout=0.1)
        assert response.raw.closed is True
    finally:
        response.close()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=1.0)


def test_streamed_json_reader_accepts_real_urllib3_http_response() -> None:
    body = b'{"ready":true}'

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            return

        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    response = bridge_module.requests.get(
        f"http://127.0.0.1:{server.server_port}/health",
        headers={"Accept-Encoding": "identity"},
        timeout=1.0,
        stream=True,
        allow_redirects=False,
    )
    try:
        assert bridge_module._read_bounded_json_response(
            response,
            field="real worker",
            deadline_monotonic=time.monotonic() + 1.0,
            maximum_bytes=64,
        ) == {"ready": True}
        assert response.raw.closed is True
    finally:
        response.close()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=1.0)
