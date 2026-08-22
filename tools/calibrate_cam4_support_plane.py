#!/usr/bin/env python3
"""Fit and attest the VIPLab CAM4 blue support surface from an exact RGB-D bag.

The output is only a camera-frame normal prior for constrained planar tool
orientation.  It is not a robot/world/TCP calibration and must not be used to
authorize physical motion.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any

import cv2
import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKER_ROOT = REPO_ROOT / "src/pnu_perception_worker"
if str(WORKER_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKER_ROOT))

from pnu_perception_worker.support_plane import (  # noqa: E402
    ARTIFACT_SCHEMA,
    CALIBRATION_SCOPE,
    camera_info_sha256,
    canonical_payload_sha256,
    fit_plane_ransac,
    normal_angle_degrees,
    select_support_plane_points,
)


RGB_TOPIC = "/synced/cam_4/color/image_raw/compressed"
COLOR_INFO_TOPIC = "/synced/cam_4/color/camera_info"
DEPTH_TOPIC = (
    "/synced/cam_4/aligned_depth_to_color/image_raw/compressedDepth"
)
DEPTH_INFO_TOPIC = "/synced/cam_4/aligned_depth_to_color/camera_info"
TOPICS = (RGB_TOPIC, COLOR_INFO_TOPIC, DEPTH_TOPIC, DEPTH_INFO_TOPIC)
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

ROI_POLYGONS = (
    ((485, 135), (890, 120), (900, 470), (490, 480)),
)
HSV_LOWER = (80, 50, 45)
HSV_UPPER = (118, 255, 255)
EROSION_KERNEL_PX = 5
DEPTH_RANGE_M = (0.55, 1.10)
MAX_POINTS_PER_FRAME = 7_000
RANDOM_SEED = 20_260_821
FIT_INLIER_THRESHOLD_M = 0.008

ACCEPTANCE_CRITERIA = {
    "minimum_exact_quartet_count": 20,
    "minimum_sample_frame_count": 20,
    "minimum_sample_span_sec": 2.0,
    "minimum_selected_points_per_frame": 5_000,
    "minimum_fit_inlier_ratio_at_8mm": 0.80,
    "maximum_fit_residual_median_m": 0.006,
    "maximum_fit_residual_p95_m": 0.015,
    "maximum_temporal_normal_drift_p95_deg": 0.75,
    "maximum_temporal_normal_drift_max_deg": 1.0,
    "maximum_temporal_offset_drift_p95_m": 0.002,
    "maximum_temporal_offset_drift_max_m": 0.003,
    "maximum_plane_normal_z": -0.8,
    "require_runtime_gate_all_sampled_frames_passed": True,
}

RUNTIME_GATE = {
    "min_sample_count": 5_000,
    "inlier_threshold_m": 0.012,
    "min_inlier_ratio": 0.85,
    "max_residual_median_m": 0.006,
    "max_residual_p95_m": 0.020,
}


def _stamp_ns(message: Any) -> int:
    return int(message.header.stamp.sec) * 1_000_000_000 + int(
        message.header.stamp.nanosec
    )


def _camera_info(message: Any) -> dict[str, Any]:
    return {
        "frame_id": str(message.header.frame_id),
        "width": int(message.width),
        "height": int(message.height),
        "distortion_model": str(message.distortion_model),
        "d": [float(item) for item in message.d],
        "k": [float(item) for item in message.k],
        "r": [float(item) for item in message.r],
        "p": [float(item) for item in message.p],
    }


def _decode_rgb(message: Any) -> np.ndarray:
    declared = str(message.format).casefold()
    payload = bytes(message.data)
    if "jpeg" not in declared and "jpg" not in declared:
        raise RuntimeError("CAM4 RGB must use JPEG compressed transport")
    frame = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        raise RuntimeError("CAM4 RGB JPEG cannot be decoded")
    return frame


def _decode_depth(message: Any, scale_m_per_unit: float) -> np.ndarray:
    declared = str(message.format)
    if not declared.upper().startswith("16UC1;") or "compresseddepth" not in declared.lower():
        raise RuntimeError("CAM4 aligned depth must be 16UC1 compressedDepth")
    payload = bytes(message.data)
    offset = payload.find(PNG_SIGNATURE)
    if offset < 0:
        raise RuntimeError("CAM4 compressedDepth payload has no PNG signature")
    decoded = cv2.imdecode(
        np.frombuffer(payload[offset:], dtype=np.uint8), cv2.IMREAD_UNCHANGED
    )
    if decoded is None or decoded.ndim != 2 or decoded.dtype != np.uint16:
        raise RuntimeError("CAM4 compressedDepth did not decode as uint16 HxW")
    return decoded.astype(np.float32) * np.float32(scale_m_per_unit)


def _bag_digest(path: Path) -> tuple[str, list[dict[str, Any]]]:
    files = sorted(item for item in path.rglob("*") if item.is_file())
    if not files:
        raise RuntimeError("bag directory is empty")
    manifest: list[dict[str, Any]] = []
    for item in files:
        digest = hashlib.sha256()
        with item.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        manifest.append(
            {
                "path": item.relative_to(path).as_posix(),
                "size_bytes": item.stat().st_size,
                "sha256": digest.hexdigest(),
            }
        )
    canonical = json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest(), manifest


def _read_bag(path: Path) -> tuple[dict[str, dict[int, Any]], dict[str, int]]:
    try:
        import rosbag2_py
        from rclpy.serialization import deserialize_message
        from rosidl_runtime_py.utilities import get_message
    except ImportError as exc:
        raise RuntimeError(
            "ROS 2 Python packages are required to read the calibration bag"
        ) from exc
    reader = rosbag2_py.SequentialReader()
    reader.open(
        rosbag2_py.StorageOptions(uri=str(path), storage_id="sqlite3"),
        rosbag2_py.ConverterOptions(
            input_serialization_format="cdr", output_serialization_format="cdr"
        ),
    )
    topic_types = {item.name: item.type for item in reader.get_all_topics_and_types()}
    missing = [topic for topic in TOPICS if topic not in topic_types]
    if missing:
        raise RuntimeError("bag lacks required topics: " + ", ".join(missing))
    messages: dict[str, dict[int, Any]] = {topic: {} for topic in TOPICS}
    counts = {topic: 0 for topic in TOPICS}
    while reader.has_next():
        topic, serialized, _received_ns = reader.read_next()
        if topic not in messages:
            continue
        message = deserialize_message(serialized, get_message(topic_types[topic]))
        stamp = _stamp_ns(message)
        if stamp in messages[topic]:
            previous = messages[topic][stamp]
            if topic in {COLOR_INFO_TOPIC, DEPTH_INFO_TOPIC}:
                identical = _camera_info(previous) == _camera_info(message)
            else:
                identical = (
                    str(previous.header.frame_id) == str(message.header.frame_id)
                    and str(previous.format) == str(message.format)
                    and bytes(previous.data) == bytes(message.data)
                )
            if not identical:
                raise RuntimeError(
                    f"conflicting messages share a source stamp on {topic}: {stamp}"
                )
            counts[topic] += 1
            continue
        messages[topic][stamp] = message
        counts[topic] += 1
    return messages, counts


def _percentile(values: list[float], q: float) -> float:
    return float(np.percentile(np.asarray(values, dtype=np.float64), q))


def _round(value: float) -> float:
    return round(float(value), 12)


def calibrate(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    bag_path = args.bag.resolve()
    messages, topic_counts = _read_bag(bag_path)
    exact_stamps = sorted(
        set.intersection(*(set(messages[topic]) for topic in TOPICS))
    )
    if not exact_stamps:
        raise RuntimeError("bag has no exact RGB/depth/CameraInfo quartet")
    sample_count = min(args.sample_frames, len(exact_stamps))
    sample_indices = sorted(
        set(int(item) for item in np.linspace(0, len(exact_stamps) - 1, sample_count))
    )
    sample_stamps = [exact_stamps[index] for index in sample_indices]
    first_info = _camera_info(messages[COLOR_INFO_TOPIC][sample_stamps[0]])
    camera_digest = camera_info_sha256(first_info)
    if (
        first_info["frame_id"] != args.frame_id
        or first_info["width"] != args.width
        or first_info["height"] != args.height
    ):
        raise RuntimeError("bag CameraInfo does not match expected CAM4 frame/profile")

    all_points: list[np.ndarray] = []
    per_frame_fits = []
    selected_counts: list[int] = []
    for index, stamp in enumerate(sample_stamps):
        color_info = _camera_info(messages[COLOR_INFO_TOPIC][stamp])
        depth_info = _camera_info(messages[DEPTH_INFO_TOPIC][stamp])
        if color_info != depth_info:
            raise RuntimeError(f"aligned-depth CameraInfo differs at stamp {stamp}")
        if camera_info_sha256(color_info) != camera_digest:
            raise RuntimeError(f"CAM4 CameraInfo changed at stamp {stamp}")
        rgb = messages[RGB_TOPIC][stamp]
        depth = messages[DEPTH_TOPIC][stamp]
        if str(rgb.header.frame_id) != args.frame_id or str(depth.header.frame_id) != args.frame_id:
            raise RuntimeError(f"CAM4 frame identity mismatch at stamp {stamp}")
        frame = _decode_rgb(rgb)
        depth_m = _decode_depth(depth, args.depth_scale_m_per_unit)
        points = select_support_plane_points(
            frame_bgr=frame,
            depth_m=depth_m,
            camera_info=color_info,
            roi_polygons_px=ROI_POLYGONS,
            hsv_lower=HSV_LOWER,
            hsv_upper=HSV_UPPER,
            erosion_kernel_px=EROSION_KERNEL_PX,
            depth_range_m=DEPTH_RANGE_M,
            max_points=MAX_POINTS_PER_FRAME,
            random_seed=RANDOM_SEED + index,
        )
        selected_counts.append(int(len(points)))
        if len(points) < 3:
            raise RuntimeError(f"support-plane selection is empty at stamp {stamp}")
        all_points.append(points)
        per_frame_fits.append(
            fit_plane_ransac(
                points,
                inlier_threshold_m=FIT_INLIER_THRESHOLD_M,
                iterations=400,
                random_seed=RANDOM_SEED + index,
                evaluation_sample_limit=10_000,
            )
        )

    aggregate = np.vstack(all_points)
    fit = fit_plane_ransac(
        aggregate,
        inlier_threshold_m=FIT_INLIER_THRESHOLD_M,
        iterations=1_500,
        random_seed=RANDOM_SEED,
        evaluation_sample_limit=20_000,
    )
    normal_drifts = [
        normal_angle_degrees(frame.normal, fit.normal) for frame in per_frame_fits
    ]
    offset_drifts = [abs(frame.offset_m - fit.offset_m) for frame in per_frame_fits]
    span_sec = (sample_stamps[-1] - sample_stamps[0]) / 1_000_000_000.0
    normal_drift = {
        "median": _round(median(normal_drifts)),
        "p95": _round(_percentile(normal_drifts, 95.0)),
        "max": _round(max(normal_drifts)),
    }
    offset_drift = {
        "median": _round(median(offset_drifts)),
        "p95": _round(_percentile(offset_drifts, 95.0)),
        "max": _round(max(offset_drifts)),
    }

    runtime_frame_rows: list[dict[str, Any]] = []
    for stamp, points in zip(sample_stamps, all_points, strict=True):
        residuals = np.abs(
            points @ np.asarray(fit.normal, dtype=np.float64) + fit.offset_m
        )
        row = {
            "stamp_ns": stamp,
            "sample_count": int(len(points)),
            "inlier_ratio": _round(
                np.mean(residuals <= RUNTIME_GATE["inlier_threshold_m"])
            ),
            "residual_median_m": _round(np.median(residuals)),
            "residual_p95_m": _round(np.percentile(residuals, 95.0)),
        }
        row["passed"] = bool(
            row["sample_count"] >= RUNTIME_GATE["min_sample_count"]
            and row["inlier_ratio"] >= RUNTIME_GATE["min_inlier_ratio"]
            and row["residual_median_m"]
            <= RUNTIME_GATE["max_residual_median_m"]
            and row["residual_p95_m"] <= RUNTIME_GATE["max_residual_p95_m"]
        )
        runtime_frame_rows.append(row)
    runtime_gate_capture_validation = {
        "all_sampled_frames_passed": all(
            bool(row["passed"]) for row in runtime_frame_rows
        ),
        "failed_stamp_ns": [
            int(row["stamp_ns"]) for row in runtime_frame_rows if not row["passed"]
        ],
        "observed": {
            "minimum_sample_count": min(
                int(row["sample_count"]) for row in runtime_frame_rows
            ),
            "minimum_inlier_ratio": min(
                float(row["inlier_ratio"]) for row in runtime_frame_rows
            ),
            "maximum_residual_median_m": max(
                float(row["residual_median_m"]) for row in runtime_frame_rows
            ),
            "maximum_residual_p95_m": max(
                float(row["residual_p95_m"]) for row in runtime_frame_rows
            ),
        },
    }

    criteria = ACCEPTANCE_CRITERIA
    reasons: list[str] = []
    checks = (
        (len(exact_stamps) >= criteria["minimum_exact_quartet_count"], "exact_quartets_low"),
        (len(sample_stamps) >= criteria["minimum_sample_frame_count"], "sample_frames_low"),
        (span_sec >= criteria["minimum_sample_span_sec"], "sample_span_short"),
        (min(selected_counts) >= criteria["minimum_selected_points_per_frame"], "selected_points_low"),
        (fit.inlier_ratio >= criteria["minimum_fit_inlier_ratio_at_8mm"], "fit_inlier_ratio_low"),
        (fit.residual_median_m <= criteria["maximum_fit_residual_median_m"], "fit_residual_median_high"),
        (fit.residual_p95_m <= criteria["maximum_fit_residual_p95_m"], "fit_residual_p95_high"),
        (normal_drift["p95"] <= criteria["maximum_temporal_normal_drift_p95_deg"], "normal_drift_p95_high"),
        (normal_drift["max"] <= criteria["maximum_temporal_normal_drift_max_deg"], "normal_drift_max_high"),
        (offset_drift["p95"] <= criteria["maximum_temporal_offset_drift_p95_m"], "offset_drift_p95_high"),
        (offset_drift["max"] <= criteria["maximum_temporal_offset_drift_max_m"], "offset_drift_max_high"),
        (fit.normal[2] <= criteria["maximum_plane_normal_z"], "plane_normal_faces_away_from_camera"),
        (
            runtime_gate_capture_validation["all_sampled_frames_passed"],
            "runtime_gate_failed_on_calibration_capture",
        ),
    )
    reasons.extend(reason for passed, reason in checks if not passed)
    accepted = not reasons

    bag_digest, bag_manifest = _bag_digest(bag_path)
    # Source stamps are synchronized wall-clock time in this reviewed VIPLab
    # contract.  Using the last exact quartet makes the artifact byte-for-byte
    # reproducible from the same capture instead of depending on run time.
    created = datetime.fromtimestamp(
        exact_stamps[-1] / 1_000_000_000.0, tz=timezone.utc
    ).replace(microsecond=0)
    valid_until = created + timedelta(days=args.valid_days)
    calibration = {
        "calibration_id": (
            f"viplab_cam4_{args.camera_serial}_{created:%Y%m%dT%H%M%SZ}"
        ),
        "created_at_utc": created.isoformat().replace("+00:00", "Z"),
        "valid_until_utc": valid_until.isoformat().replace("+00:00", "Z"),
        "source": {
            "camera_name": "cam_4",
            "camera_serial": args.camera_serial,
            "camera_model": args.camera_model,
            "firmware_version": args.firmware_version,
            "recommended_firmware_version": args.recommended_firmware_version,
            "usb_type_descriptor": args.usb_type_descriptor,
            "profile": args.profile,
            "frame_id": args.frame_id,
            "width": args.width,
            "height": args.height,
            "distortion_model": first_info["distortion_model"],
            "camera_info_sha256": camera_digest,
            "alignment_id": args.alignment_id,
            "depth_scale_m_per_unit": args.depth_scale_m_per_unit,
            "depth_scale_method": "librealsense_depth_sensor_get_depth_scale_read_only",
            "rgb_topic": RGB_TOPIC,
            "aligned_depth_topic": DEPTH_TOPIC,
            "color_camera_info_topic": COLOR_INFO_TOPIC,
            "aligned_depth_camera_info_topic": DEPTH_INFO_TOPIC,
            "capture_bag_sha256": bag_digest,
            "capture_duration_sec": _round(
                (exact_stamps[-1] - exact_stamps[0]) / 1_000_000_000.0
            ),
            "topic_message_counts": topic_counts,
            "exact_quartet_count": len(exact_stamps),
        },
        "selection": {
            "policy": (
                "bounded_tray_roi_and_hsv_blue_surface; 5px erosion removes JPEG "
                "edges; depth range rejects floor/background; dark/metal tools, "
                "hands, and non-blue outliers are excluded before RANSAC"
            ),
            "roi_polygons_px": [
                [[int(x), int(y)] for x, y in polygon] for polygon in ROI_POLYGONS
            ],
            "hsv_bgr_to_hsv_lower": list(HSV_LOWER),
            "hsv_bgr_to_hsv_upper": list(HSV_UPPER),
            "erosion_kernel_px": EROSION_KERNEL_PX,
            "depth_range_m": list(DEPTH_RANGE_M),
            "max_points_per_frame": MAX_POINTS_PER_FRAME,
            "random_seed": RANDOM_SEED,
        },
        "plane": {
            "equation": "normal_dot_point_plus_offset_equals_zero",
            "normal": [_round(item) for item in fit.normal],
            "offset_m": _round(fit.offset_m),
        },
        "fit": {
            "algorithm": "deterministic_multi_frame_ransac_then_inlier_svd_v1",
            "sample_frame_count": len(sample_stamps),
            "sample_point_count": fit.sample_count,
            "first_stamp_ns": sample_stamps[0],
            "last_stamp_ns": sample_stamps[-1],
            "sample_span_sec": _round(span_sec),
            "inlier_threshold_m": FIT_INLIER_THRESHOLD_M,
            "inlier_ratio": _round(fit.inlier_ratio),
            "residual_median_m": _round(fit.residual_median_m),
            "residual_p95_m": _round(fit.residual_p95_m),
            "temporal_normal_drift_deg": normal_drift,
            "temporal_offset_drift_m": offset_drift,
            "per_frame_selected_point_count": {
                "min": min(selected_counts),
                "median": int(median(selected_counts)),
                "max": max(selected_counts),
            },
        },
        "acceptance": {
            "accepted": accepted,
            "reasons": reasons,
            "criteria": criteria,
        },
        "runtime_gate": RUNTIME_GATE,
    }
    payload_digest = canonical_payload_sha256(calibration)
    config_version = (
        f"viplab_cam4_{args.camera_serial}_support_plane_v1_sha256_"
        f"{payload_digest[:16]}"
    )
    artifact = {
        "schema": ARTIFACT_SCHEMA,
        "scope": CALIBRATION_SCOPE,
        "support_plane_config_version": config_version,
        "integrity": {
            "algorithm": "sha256",
            "canonical_calibration_payload_sha256": payload_digest,
        },
        "calibration": calibration,
    }
    artifact_bytes = (
        json.dumps(artifact, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    ).encode("utf-8")
    artifact_digest = hashlib.sha256(artifact_bytes).hexdigest()
    report = {
        "schema": "taskplanner.pnu.cam4_support_plane_calibration_report.v1",
        "generated_at_utc": created.isoformat().replace("+00:00", "Z"),
        "accepted": accepted,
        "reasons": reasons,
        "scope": CALIBRATION_SCOPE,
        "safety_boundary": {
            "camera_frame_planar_4dof_orientation_prior_only": True,
            "robot_world_tf_calibration": False,
            "tcp_calibration": False,
            "motion_authority": False,
        },
        "artifact": {
            "path": args.output_artifact.as_posix(),
            "sha256": artifact_digest,
            "canonical_calibration_payload_sha256": payload_digest,
            "support_plane_config_version": config_version,
            "valid_until_utc": calibration["valid_until_utc"],
        },
        "capture": {
            "bag_directory_name": bag_path.name,
            "bag_manifest_digest_method": (
                "sha256(canonical_json([{relative_path,size_bytes,file_sha256},...]))"
            ),
            "bag_sha256": bag_digest,
            "bag_files": bag_manifest,
            "topic_message_counts": topic_counts,
            "exact_quartet_count": len(exact_stamps),
            "sample_frame_count": len(sample_stamps),
            "sample_span_sec": _round(span_sec),
        },
        "provenance": calibration["source"],
        "selection": calibration["selection"],
        "fit": calibration["fit"],
        "acceptance": calibration["acceptance"],
        "runtime_gate": calibration["runtime_gate"],
        "runtime_gate_capture_validation": runtime_gate_capture_validation,
        "deployment": {
            "required_read_only_mount": args.output_artifact.as_posix(),
            "recommended_environment": {
                "PNU_TOOL_SUPPORT_PLANE_ARTIFACT": args.runtime_artifact_path,
                "PNU_TOOL_SUPPORT_PLANE_ARTIFACT_SHA256": artifact_digest,
                "PNU_TOOL_SUPPORT_PLANE_CAMERA_SERIAL": args.camera_serial,
                "PNU_TOOL_SUPPORT_PLANE_CAMERA_PROFILE": args.profile,
                "PNU_TOOL_SUPPORT_PLANE_FIRMWARE_VERSION": (
                    args.firmware_version
                ),
                "PNU_TOOL_SUPPORT_PLANE_MAX_AGE_DAYS": str(args.valid_days),
                "PNU_TOOL_SUPPORT_PLANE_NORMAL": ",".join(
                    f"{item:.12f}" for item in fit.normal
                ),
                "PNU_TOOL_SUPPORT_PLANE_OFFSET_M": f"{fit.offset_m:.12f}",
                "PNU_TOOL_SUPPORT_PLANE_CONFIG_VERSION": config_version,
                "PNU_TOOL_SUPPORT_PLANE_INLIER_RATIO": f"{fit.inlier_ratio:.12f}",
                "PNU_TOOL_SUPPORT_PLANE_RESIDUAL_P95_M": (
                    f"{fit.residual_p95_m:.12f}"
                ),
                "PNU_TOOL_SUPPORT_PLANE_VALIDATED": (
                    "true" if accepted else "false"
                ),
            },
        },
        "limitations": [
            "The blue support is cloth and is not a metrology-grade rigid plane.",
            "The pose is constrained planar 4DoF, not unconstrained 6DoF.",
            "The runtime gate revokes orientation on source or surface drift.",
            "Recalibrate after camera/tray movement, profile/intrinsics/alignment changes, or artifact expiry.",
            "CAM4 firmware is recorded exactly; firmware updates require recalibration.",
        ],
    }
    return artifact, report


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bag", type=Path, required=True)
    parser.add_argument("--output-artifact", type=Path, required=True)
    parser.add_argument("--output-report", type=Path, required=True)
    parser.add_argument("--sample-frames", type=int, default=30)
    parser.add_argument("--valid-days", type=int, default=30)
    parser.add_argument("--camera-serial", default="146222251000")
    parser.add_argument("--camera-model", default="Intel RealSense D455")
    parser.add_argument("--firmware-version", default="5.15.0.2")
    parser.add_argument("--recommended-firmware-version", default="5.17.0.10")
    parser.add_argument("--usb-type-descriptor", default="3.2")
    parser.add_argument("--profile", default="RGB 1280x720x15; depth 1280x720x15")
    parser.add_argument("--frame-id", default="cam_4_color_optical_frame")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument(
        "--alignment-id", default="viplab-cam4-rgbd-align-v1-6af70cfd906e807f"
    )
    parser.add_argument("--depth-scale-m-per-unit", type=float, default=0.001)
    parser.add_argument(
        "--runtime-artifact-path",
        default="/config/pnu_perception/cam4_support_plane_20260821.json",
    )
    args = parser.parse_args()
    if args.sample_frames <= 0 or args.valid_days <= 0:
        parser.error("--sample-frames and --valid-days must be positive")
    if args.width <= 0 or args.height <= 0 or args.depth_scale_m_per_unit <= 0.0:
        parser.error("camera dimensions and depth scale must be positive")
    return args


def main() -> int:
    args = parse_args()
    artifact, report = calibrate(args)
    _write_json(args.output_artifact, artifact)
    _write_json(args.output_report, report)
    print(
        json.dumps(
            {
                "accepted": report["accepted"],
                "artifact": report["artifact"],
                "fit": report["fit"],
                "recommended_environment": report["deployment"][
                    "recommended_environment"
                ],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if report["accepted"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
