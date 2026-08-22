from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from pnu_perception_worker.support_plane import (
    ARTIFACT_SCHEMA,
    CALIBRATION_SCOPE,
    SupportPlaneArtifactError,
    camera_info_sha256,
    canonical_payload_sha256,
    fit_plane_ransac,
    load_support_plane_calibration,
)


NOW = datetime(2026, 8, 21, 5, 30, tzinfo=timezone.utc)


def _camera_info() -> dict:
    return {
        "frame_id": "cam_4_color_optical_frame",
        "width": 64,
        "height": 48,
        "distortion_model": "plumb_bob",
        "d": [0.0] * 5,
        "k": [100.0, 0.0, 32.0, 0.0, 100.0, 24.0, 0.0, 0.0, 1.0],
        "r": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
        "p": [
            100.0,
            0.0,
            32.0,
            0.0,
            0.0,
            100.0,
            24.0,
            0.0,
            0.0,
            0.0,
            1.0,
            0.0,
        ],
    }


def _write_artifact(tmp_path: Path, *, expired: bool = False) -> tuple[Path, str, str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    info = _camera_info()
    calibration = {
        "calibration_id": "cam4-test-calibration",
        "created_at_utc": "2026-08-20T00:00:00Z",
        "valid_until_utc": (
            "2026-08-21T04:00:00Z" if expired else "2026-09-19T00:00:00Z"
        ),
        "source": {
            "camera_name": "cam_4",
            "camera_serial": "146222251000",
            "camera_model": "Intel RealSense D455",
            "firmware_version": "5.15.0.2",
            "recommended_firmware_version": "5.17.0.10",
            "usb_type_descriptor": "3.2",
            "profile": "RGB 64x48x15; depth 64x48x15",
            "frame_id": info["frame_id"],
            "width": info["width"],
            "height": info["height"],
            "distortion_model": "plumb_bob",
            "camera_info_sha256": camera_info_sha256(info),
            "alignment_id": "cam4-align-test",
            "depth_scale_m_per_unit": 0.001,
            "depth_scale_method": "librealsense_depth_sensor_get_depth_scale_read_only",
            "rgb_topic": "/rgb",
            "aligned_depth_topic": "/depth",
            "color_camera_info_topic": "/rgb_info",
            "aligned_depth_camera_info_topic": "/depth_info",
            "capture_bag_sha256": "b" * 64,
            "capture_duration_sec": 4.0,
            "topic_message_counts": {
                "/rgb": 60,
                "/depth": 60,
                "/rgb_info": 60,
                "/depth_info": 60,
            },
            "exact_quartet_count": 60,
        },
        "selection": {
            "policy": "test blue ROI",
            "roi_polygons_px": [[[1, 1], [62, 1], [62, 46], [1, 46]]],
            "hsv_bgr_to_hsv_lower": [80, 50, 45],
            "hsv_bgr_to_hsv_upper": [118, 255, 255],
            "erosion_kernel_px": 3,
            "depth_range_m": [0.55, 1.1],
            "max_points_per_frame": 7000,
            "random_seed": 1,
        },
        "plane": {
            "equation": "normal_dot_point_plus_offset_equals_zero",
            "normal": [0.0, 0.0, -1.0],
            "offset_m": 0.8,
        },
        "fit": {
            "algorithm": "deterministic_multi_frame_ransac_then_inlier_svd_v1",
            "sample_frame_count": 30,
            "sample_point_count": 100_000,
            "first_stamp_ns": 1,
            "last_stamp_ns": 4_000_000_001,
            "sample_span_sec": 4.0,
            "inlier_threshold_m": 0.008,
            "inlier_ratio": 0.95,
            "residual_median_m": 0.001,
            "residual_p95_m": 0.004,
            "temporal_normal_drift_deg": {"median": 0.1, "p95": 0.2, "max": 0.3},
            "temporal_offset_drift_m": {
                "median": 0.0001,
                "p95": 0.0002,
                "max": 0.0003,
            },
            "per_frame_selected_point_count": {
                "min": 2500,
                "median": 2600,
                "max": 2700,
            },
        },
        "acceptance": {
            "accepted": True,
            "reasons": [],
            "criteria": {"minimum_sample_frame_count": 20},
        },
        "runtime_gate": {
            "min_sample_count": 1000,
            "inlier_threshold_m": 0.012,
            "min_inlier_ratio": 0.85,
            "max_residual_median_m": 0.006,
            "max_residual_p95_m": 0.02,
        },
    }
    payload_digest = canonical_payload_sha256(calibration)
    version = f"cam4_test_plane_sha256_{payload_digest[:16]}"
    document = {
        "schema": ARTIFACT_SCHEMA,
        "scope": CALIBRATION_SCOPE,
        "support_plane_config_version": version,
        "integrity": {
            "algorithm": "sha256",
            "canonical_calibration_payload_sha256": payload_digest,
        },
        "calibration": calibration,
    }
    path = tmp_path / "plane.json"
    path.write_text(
        json.dumps(document, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return path, digest, version


def _load(path: Path, digest: str, version: str, **kwargs):
    return load_support_plane_calibration(
        artifact_path=path,
        expected_artifact_sha256=digest,
        expected_config_version=version,
        expected_camera_serial="146222251000",
        expected_camera_profile="RGB 64x48x15; depth 64x48x15",
        expected_firmware_version="5.15.0.2",
        expected_normal=(0.0, 0.0, -1.0),
        expected_offset_m=0.8,
        expected_inlier_ratio=0.95,
        expected_residual_p95_m=0.004,
        max_age_days=30,
        now_utc=NOW,
        **kwargs,
    )


def test_artifact_and_live_blue_surface_must_both_validate(tmp_path: Path) -> None:
    path, digest, version = _write_artifact(tmp_path)
    calibration = _load(path, digest, version)
    frame = np.full((48, 64, 3), (255, 128, 0), dtype=np.uint8)
    depth = np.full((48, 64), 0.8, dtype=np.float32)
    request = SimpleNamespace(
        source={"rgb": {"frame_id": "cam_4_color_optical_frame"}},
        metadata={"color_camera_info": _camera_info()},
    )
    result = calibration.validate_frame(
        request=request,
        depth_m=depth,
        depth_scale_m_per_unit=0.001,
        alignment_id="cam4-align-test",
        frame_bgr=frame,
    )
    assert result.valid is True
    assert result.evaluated is True
    assert result.metrics_available is True
    assert result.sample_count >= 1000
    assert result.inlier_ratio == pytest.approx(1.0)
    assert result.residual_p95_m == pytest.approx(0.0, abs=1.0e-6)

    drifted = calibration.validate_frame(
        request=request,
        depth_m=np.full((48, 64), 0.86, dtype=np.float32),
        depth_scale_m_per_unit=0.001,
        alignment_id="cam4-align-test",
        frame_bgr=frame,
    )
    assert drifted.valid is False
    assert "support_plane_runtime_inlier_ratio_low" in drifted.reasons


def test_artifact_rejects_full_file_tamper_and_expiry(tmp_path: Path) -> None:
    path, digest, version = _write_artifact(tmp_path)
    path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(SupportPlaneArtifactError, match="SHA-256 mismatch"):
        _load(path, digest, version)

    expired_path, expired_digest, expired_version = _write_artifact(
        tmp_path / "expired", expired=True
    )
    with pytest.raises(SupportPlaneArtifactError, match="expired"):
        _load(expired_path, expired_digest, expired_version)


def test_runtime_provenance_mismatch_fails_before_geometry(tmp_path: Path) -> None:
    path, digest, version = _write_artifact(tmp_path)
    calibration = _load(path, digest, version)
    info = _camera_info()
    info["k"][0] = 99.0
    request = SimpleNamespace(
        source={"rgb": {"frame_id": "cam_4_color_optical_frame"}},
        metadata={"color_camera_info": info},
    )
    result = calibration.validate_frame(
        request=request,
        depth_m=np.full((48, 64), 0.8, dtype=np.float32),
        depth_scale_m_per_unit=0.001,
        alignment_id="cam4-align-test",
        frame_bgr=np.full((48, 64, 3), (255, 128, 0), dtype=np.uint8),
    )
    assert result.valid is False
    assert result.evaluated is True
    assert result.metrics_available is False
    assert result.reasons == ("support_plane_camera_info_mismatch",)


def test_ransac_recovers_plane_while_rejecting_outliers() -> None:
    rng = np.random.default_rng(42)
    xy = rng.uniform(-0.3, 0.3, size=(4000, 2))
    z = 0.8 + rng.normal(0.0, 0.001, size=(4000, 1))
    plane = np.column_stack((xy, z))
    outliers = rng.uniform((-0.3, -0.3, 0.5), (0.3, 0.3, 1.1), size=(800, 3))
    fit = fit_plane_ransac(
        np.vstack((plane, outliers)),
        inlier_threshold_m=0.005,
        iterations=600,
        random_seed=7,
    )
    assert fit.normal == pytest.approx((0.0, 0.0, -1.0), abs=5.0e-4)
    assert fit.offset_m == pytest.approx(0.8, abs=5.0e-4)
    assert fit.inlier_ratio > 0.80
    assert fit.residual_median_m < 0.002
