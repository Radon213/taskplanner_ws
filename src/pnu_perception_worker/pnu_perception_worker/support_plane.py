"""Artifact-gated CAM4 support-plane calibration and runtime drift checks.

This module validates a plane in the RGB camera frame for the upstream
``PLANAR_4DOF_WITH_NORMAL_PRIOR`` tool-pose estimator.  It is deliberately not
a robot/world/TCP calibration and never grants motion authority.
"""

from __future__ import annotations

import hashlib
import json
import math
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import cv2
import numpy as np


ARTIFACT_SCHEMA = "taskplanner.pnu.cam4_support_plane.v1"
CALIBRATION_SCOPE = "camera_frame_planar_4dof_orientation_prior_only"
_MAX_ARTIFACT_BYTES = 512 * 1024


class SupportPlaneArtifactError(ValueError):
    """A calibration artifact or its deployment pin is not trustworthy."""


@dataclass(frozen=True)
class PlaneFit:
    normal: tuple[float, float, float]
    offset_m: float
    sample_count: int
    inlier_threshold_m: float
    inlier_ratio: float
    residual_median_m: float
    residual_p95_m: float


@dataclass(frozen=True)
class RuntimePlaneValidation:
    valid: bool
    reasons: tuple[str, ...]
    evaluated: bool = False
    metrics_available: bool = False
    sample_count: int = 0
    inlier_ratio: float = 0.0
    residual_median_m: float | None = None
    residual_p95_m: float | None = None
    camera_info_sha256: str = ""


@dataclass(frozen=True)
class SupportPlaneCalibration:
    artifact_sha256: str
    canonical_payload_sha256: str
    config_version: str
    created_at_utc: datetime
    valid_until_utc: datetime
    camera_serial: str
    profile: str
    frame_id: str
    width: int
    height: int
    camera_info_sha256: str
    alignment_id: str
    depth_scale_m_per_unit: float
    normal: tuple[float, float, float]
    offset_m: float
    inlier_ratio: float
    residual_p95_m: float
    roi_polygons_px: tuple[tuple[tuple[int, int], ...], ...]
    hsv_lower: tuple[int, int, int]
    hsv_upper: tuple[int, int, int]
    erosion_kernel_px: int
    depth_range_m: tuple[float, float]
    runtime_min_sample_count: int
    runtime_inlier_threshold_m: float
    runtime_min_inlier_ratio: float
    runtime_max_residual_median_m: float
    runtime_max_residual_p95_m: float

    def validate_frame(
        self,
        *,
        request: Any,
        depth_m: np.ndarray,
        depth_scale_m_per_unit: float | None,
        alignment_id: str | None,
        frame_bgr: np.ndarray,
    ) -> RuntimePlaneValidation:
        """Verify source identity and that the observed tray still matches.

        The color gate intentionally selects the blue support-cloth surface
        and therefore excludes dark/metal tools, hands, and most unrelated
        scene geometry before residuals are computed.
        """

        reasons: list[str] = []
        source = getattr(request, "source", {})
        metadata = getattr(request, "metadata", {})
        rgb = source.get("rgb", {}) if isinstance(source, dict) else {}
        info = metadata.get("color_camera_info") if isinstance(metadata, dict) else None
        if rgb.get("frame_id") != self.frame_id:
            reasons.append("support_plane_rgb_frame_mismatch")
        if alignment_id != self.alignment_id:
            reasons.append("support_plane_alignment_id_mismatch")
        if (
            depth_scale_m_per_unit is None
            or not math.isfinite(float(depth_scale_m_per_unit))
            or not math.isclose(
                float(depth_scale_m_per_unit),
                self.depth_scale_m_per_unit,
                rel_tol=0.0,
                abs_tol=1.0e-12,
            )
        ):
            reasons.append("support_plane_depth_scale_mismatch")
        try:
            info_digest = camera_info_sha256(info)
        except SupportPlaneArtifactError:
            info_digest = ""
            reasons.append("support_plane_camera_info_invalid")
        if info_digest and info_digest != self.camera_info_sha256:
            reasons.append("support_plane_camera_info_mismatch")
        if frame_bgr.shape[:2] != (self.height, self.width):
            reasons.append("support_plane_rgb_dimensions_mismatch")
        if depth_m.shape != (self.height, self.width):
            reasons.append("support_plane_depth_dimensions_mismatch")
        if reasons:
            return RuntimePlaneValidation(
                valid=False,
                reasons=tuple(reasons),
                evaluated=True,
                camera_info_sha256=info_digest,
            )

        try:
            points = select_support_plane_points(
                frame_bgr=frame_bgr,
                depth_m=depth_m,
                camera_info=info,
                roi_polygons_px=self.roi_polygons_px,
                hsv_lower=self.hsv_lower,
                hsv_upper=self.hsv_upper,
                erosion_kernel_px=self.erosion_kernel_px,
                depth_range_m=self.depth_range_m,
                max_points=50_000,
                random_seed=0,
            )
        except (SupportPlaneArtifactError, cv2.error, ValueError):
            return RuntimePlaneValidation(
                valid=False,
                reasons=("support_plane_runtime_sampling_failed",),
                evaluated=True,
                camera_info_sha256=info_digest,
            )
        sample_count = int(len(points))
        if sample_count:
            residuals = np.abs(
                points @ np.asarray(self.normal, dtype=np.float64)
                + float(self.offset_m)
            )
            inlier_ratio = float(
                np.count_nonzero(residuals <= self.runtime_inlier_threshold_m)
                / sample_count
            )
            residual_median_m = float(np.median(residuals))
            residual_p95_m = float(np.percentile(residuals, 95.0))
        else:
            inlier_ratio = 0.0
            residual_median_m = None
            residual_p95_m = None
        if sample_count < self.runtime_min_sample_count:
            reasons.append("support_plane_runtime_samples_low")
        if inlier_ratio < self.runtime_min_inlier_ratio:
            reasons.append("support_plane_runtime_inlier_ratio_low")
        if (
            residual_median_m is None
            or residual_median_m > self.runtime_max_residual_median_m
        ):
            reasons.append("support_plane_runtime_residual_median_high")
        if residual_p95_m is None or residual_p95_m > self.runtime_max_residual_p95_m:
            reasons.append("support_plane_runtime_residual_p95_high")
        return RuntimePlaneValidation(
            valid=not reasons,
            reasons=tuple(reasons),
            evaluated=True,
            metrics_available=sample_count > 0,
            sample_count=sample_count,
            inlier_ratio=inlier_ratio,
            residual_median_m=residual_median_m,
            residual_p95_m=residual_p95_m,
            camera_info_sha256=info_digest,
        )


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_payload_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SupportPlaneArtifactError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _exact_keys(value: Any, expected: set[str], field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise SupportPlaneArtifactError(f"{field} has unexpected keys")
    return value


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SupportPlaneArtifactError(f"{field} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise SupportPlaneArtifactError(f"{field} must be a finite number")
    return result


def _positive_int_value(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SupportPlaneArtifactError(f"{field} must be a positive integer")
    return value


def _bounded_string(value: Any, field: str, maximum: int = 512) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or len(value.encode("utf-8")) > maximum
    ):
        raise SupportPlaneArtifactError(f"{field} must be a bounded string")
    return value


def _parse_utc(value: Any, field: str) -> datetime:
    raw = _bounded_string(value, field, maximum=64)
    if not raw.endswith("Z"):
        raise SupportPlaneArtifactError(f"{field} must use UTC Z notation")
    try:
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
    except ValueError as exc:
        raise SupportPlaneArtifactError(f"{field} is not ISO-8601") from exc
    return parsed.astimezone(timezone.utc)


def _camera_info_core(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SupportPlaneArtifactError("camera_info must be an object")
    try:
        core = {
            "frame_id": str(value["frame_id"]),
            "width": int(value["width"]),
            "height": int(value["height"]),
            "distortion_model": str(value["distortion_model"]),
            "d": [float(item) for item in value["d"]],
            "k": [float(item) for item in value["k"]],
            "r": [float(item) for item in value["r"]],
            "p": [float(item) for item in value["p"]],
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise SupportPlaneArtifactError("camera_info is malformed") from exc
    if (
        not core["frame_id"]
        or core["width"] <= 0
        or core["height"] <= 0
        or not core["distortion_model"]
        or len(core["k"]) != 9
        or len(core["r"]) != 9
        or len(core["p"]) != 12
        or not core["d"]
        or not all(
            math.isfinite(item)
            for name in ("d", "k", "r", "p")
            for item in core[name]
        )
    ):
        raise SupportPlaneArtifactError("camera_info geometry is invalid")
    return core


def camera_info_sha256(value: Any) -> str:
    return canonical_payload_sha256(_camera_info_core(value))


def _vector3(value: Any, field: str) -> tuple[float, float, float]:
    if not isinstance(value, list) or len(value) != 3:
        raise SupportPlaneArtifactError(f"{field} must be a three-vector")
    result = tuple(_finite_number(item, field) for item in value)
    if np.linalg.norm(result) < 1.0e-9:
        raise SupportPlaneArtifactError(f"{field} is degenerate")
    return result


def load_support_plane_calibration(
    *,
    artifact_path: Path | None,
    expected_artifact_sha256: str,
    expected_config_version: str,
    expected_camera_serial: str,
    expected_camera_profile: str,
    expected_firmware_version: str,
    expected_normal: Sequence[float],
    expected_offset_m: float,
    expected_inlier_ratio: float,
    expected_residual_p95_m: float,
    max_age_days: int,
    now_utc: datetime | None = None,
) -> SupportPlaneCalibration:
    """Load a strictly pinned artifact or raise without granting orientation."""

    if artifact_path is None:
        raise SupportPlaneArtifactError("support-plane artifact path is missing")
    if len(expected_artifact_sha256) != 64 or any(
        char not in "0123456789abcdef" for char in expected_artifact_sha256
    ):
        raise SupportPlaneArtifactError("support-plane artifact SHA-256 pin is missing")
    path = artifact_path
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise SupportPlaneArtifactError("support-plane artifact is missing") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise SupportPlaneArtifactError("support-plane artifact file type is unsafe")
    if metadata.st_size <= 0 or metadata.st_size > _MAX_ARTIFACT_BYTES:
        raise SupportPlaneArtifactError("support-plane artifact size is invalid")
    actual_artifact_digest = sha256_file(path)
    if actual_artifact_digest != expected_artifact_sha256:
        raise SupportPlaneArtifactError("support-plane artifact SHA-256 mismatch")
    try:
        document = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SupportPlaneArtifactError("support-plane artifact is not valid JSON") from exc
    root = _exact_keys(
        document,
        {
            "schema",
            "scope",
            "support_plane_config_version",
            "integrity",
            "calibration",
        },
        "artifact",
    )
    if root["schema"] != ARTIFACT_SCHEMA or root["scope"] != CALIBRATION_SCOPE:
        raise SupportPlaneArtifactError("support-plane artifact schema/scope mismatch")
    config_version = _bounded_string(
        root["support_plane_config_version"], "support_plane_config_version", 240
    )
    if config_version != expected_config_version:
        raise SupportPlaneArtifactError("support-plane config-version pin mismatch")
    integrity = _exact_keys(
        root["integrity"], {"algorithm", "canonical_calibration_payload_sha256"}, "integrity"
    )
    if integrity["algorithm"] != "sha256":
        raise SupportPlaneArtifactError("support-plane integrity algorithm mismatch")
    payload_digest = _bounded_string(
        integrity["canonical_calibration_payload_sha256"],
        "integrity.canonical_calibration_payload_sha256",
        64,
    )
    if len(payload_digest) != 64 or any(
        char not in "0123456789abcdef" for char in payload_digest
    ):
        raise SupportPlaneArtifactError("support-plane payload digest is invalid")
    calibration = _exact_keys(
        root["calibration"],
        {
            "calibration_id",
            "created_at_utc",
            "valid_until_utc",
            "source",
            "selection",
            "plane",
            "fit",
            "acceptance",
            "runtime_gate",
        },
        "calibration",
    )
    if canonical_payload_sha256(calibration) != payload_digest:
        raise SupportPlaneArtifactError("support-plane canonical payload digest mismatch")
    if not config_version.endswith(payload_digest[:16]):
        raise SupportPlaneArtifactError("support-plane config version lacks payload digest")
    _bounded_string(calibration["calibration_id"], "calibration.calibration_id", 240)
    created = _parse_utc(calibration["created_at_utc"], "calibration.created_at_utc")
    valid_until = _parse_utc(
        calibration["valid_until_utc"], "calibration.valid_until_utc"
    )
    now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if (created - now).total_seconds() > 300:
        raise SupportPlaneArtifactError("support-plane artifact is future-dated")
    if valid_until <= created or now > valid_until:
        raise SupportPlaneArtifactError("support-plane artifact is expired")
    if max_age_days <= 0 or (now - created).total_seconds() > max_age_days * 86400:
        raise SupportPlaneArtifactError("support-plane artifact exceeds deployment max age")

    source = _exact_keys(
        calibration["source"],
        {
            "camera_name",
            "camera_serial",
            "camera_model",
            "firmware_version",
            "recommended_firmware_version",
            "usb_type_descriptor",
            "profile",
            "frame_id",
            "width",
            "height",
            "distortion_model",
            "camera_info_sha256",
            "alignment_id",
            "depth_scale_m_per_unit",
            "depth_scale_method",
            "rgb_topic",
            "aligned_depth_topic",
            "color_camera_info_topic",
            "aligned_depth_camera_info_topic",
            "capture_bag_sha256",
            "capture_duration_sec",
            "topic_message_counts",
            "exact_quartet_count",
        },
        "calibration.source",
    )
    camera_serial = _bounded_string(source["camera_serial"], "source.camera_serial", 80)
    if not expected_camera_serial or camera_serial != expected_camera_serial:
        raise SupportPlaneArtifactError("support-plane camera-serial pin mismatch")
    profile = _bounded_string(source["profile"], "source.profile", 80)
    if not expected_camera_profile or profile != expected_camera_profile:
        raise SupportPlaneArtifactError("support-plane camera-profile pin mismatch")
    firmware_version = _bounded_string(
        source["firmware_version"], "source.firmware_version", 80
    )
    if (
        not expected_firmware_version
        or firmware_version != expected_firmware_version
    ):
        raise SupportPlaneArtifactError("support-plane firmware-version pin mismatch")
    frame_id = _bounded_string(source["frame_id"], "source.frame_id", 160)
    width = _positive_int_value(source["width"], "source.width")
    height = _positive_int_value(source["height"], "source.height")
    camera_digest = _bounded_string(
        source["camera_info_sha256"], "source.camera_info_sha256", 64
    )
    if len(camera_digest) != 64 or any(
        char not in "0123456789abcdef" for char in camera_digest
    ):
        raise SupportPlaneArtifactError("source.camera_info_sha256 is invalid")
    alignment_id = _bounded_string(source["alignment_id"], "source.alignment_id", 240)
    depth_scale = _finite_number(
        source["depth_scale_m_per_unit"], "source.depth_scale_m_per_unit"
    )
    if depth_scale <= 0.0:
        raise SupportPlaneArtifactError("source depth scale must be positive")

    selection = _exact_keys(
        calibration["selection"],
        {
            "policy",
            "roi_polygons_px",
            "hsv_bgr_to_hsv_lower",
            "hsv_bgr_to_hsv_upper",
            "erosion_kernel_px",
            "depth_range_m",
            "max_points_per_frame",
            "random_seed",
        },
        "calibration.selection",
    )
    raw_polygons = selection["roi_polygons_px"]
    if not isinstance(raw_polygons, list) or not raw_polygons:
        raise SupportPlaneArtifactError("selection ROI polygons are missing")
    polygons: list[tuple[tuple[int, int], ...]] = []
    for polygon in raw_polygons:
        if not isinstance(polygon, list) or len(polygon) < 3 or len(polygon) > 32:
            raise SupportPlaneArtifactError("selection ROI polygon is invalid")
        points: list[tuple[int, int]] = []
        for point in polygon:
            if (
                not isinstance(point, list)
                or len(point) != 2
                or any(isinstance(item, bool) or not isinstance(item, int) for item in point)
            ):
                raise SupportPlaneArtifactError("selection ROI point is invalid")
            if not (0 <= point[0] < width and 0 <= point[1] < height):
                raise SupportPlaneArtifactError("selection ROI point exceeds image")
            points.append((point[0], point[1]))
        polygons.append(tuple(points))
    hsv_lower = tuple(
        int(item)
        for item in _vector3(
            selection["hsv_bgr_to_hsv_lower"], "selection.hsv_lower"
        )
    )
    hsv_upper = tuple(
        int(item)
        for item in _vector3(
            selection["hsv_bgr_to_hsv_upper"], "selection.hsv_upper"
        )
    )
    if any(not 0 <= item <= 255 for item in (*hsv_lower, *hsv_upper)) or any(
        low > high for low, high in zip(hsv_lower, hsv_upper, strict=True)
    ):
        raise SupportPlaneArtifactError("selection HSV bounds are invalid")
    erosion = _positive_int_value(
        selection["erosion_kernel_px"], "selection.erosion_kernel_px"
    )
    if erosion > 31 or erosion % 2 == 0:
        raise SupportPlaneArtifactError("selection erosion kernel must be odd and <=31")
    raw_depth_range = selection["depth_range_m"]
    if not isinstance(raw_depth_range, list) or len(raw_depth_range) != 2:
        raise SupportPlaneArtifactError("selection depth range is invalid")
    depth_range = tuple(
        _finite_number(item, "selection.depth_range_m") for item in raw_depth_range
    )
    if depth_range[0] < 0.0 or depth_range[1] <= depth_range[0]:
        raise SupportPlaneArtifactError("selection depth range is not increasing")

    plane = _exact_keys(
        calibration["plane"], {"equation", "normal", "offset_m"}, "calibration.plane"
    )
    if plane["equation"] != "normal_dot_point_plus_offset_equals_zero":
        raise SupportPlaneArtifactError("support-plane equation mismatch")
    normal_raw = _vector3(plane["normal"], "plane.normal")
    normal_array = np.asarray(normal_raw, dtype=np.float64)
    normal_array /= np.linalg.norm(normal_array)
    normal = tuple(float(item) for item in normal_array)
    offset = _finite_number(plane["offset_m"], "plane.offset_m")
    expected_normal_array = np.asarray(expected_normal, dtype=np.float64)
    expected_normal_array /= np.linalg.norm(expected_normal_array)
    if not np.allclose(normal_array, expected_normal_array, rtol=0.0, atol=5.0e-10):
        raise SupportPlaneArtifactError("support-plane normal env pin mismatch")
    if not math.isclose(offset, expected_offset_m, rel_tol=0.0, abs_tol=5.0e-10):
        raise SupportPlaneArtifactError("support-plane offset env pin mismatch")

    fit = _exact_keys(
        calibration["fit"],
        {
            "algorithm",
            "sample_frame_count",
            "sample_point_count",
            "first_stamp_ns",
            "last_stamp_ns",
            "sample_span_sec",
            "inlier_threshold_m",
            "inlier_ratio",
            "residual_median_m",
            "residual_p95_m",
            "temporal_normal_drift_deg",
            "temporal_offset_drift_m",
            "per_frame_selected_point_count",
        },
        "calibration.fit",
    )
    inlier_ratio = _finite_number(fit["inlier_ratio"], "fit.inlier_ratio")
    residual_p95 = _finite_number(fit["residual_p95_m"], "fit.residual_p95_m")
    if not math.isclose(
        inlier_ratio, expected_inlier_ratio, rel_tol=0.0, abs_tol=5.0e-10
    ):
        raise SupportPlaneArtifactError("support-plane inlier-ratio env pin mismatch")
    if not math.isclose(
        residual_p95, expected_residual_p95_m, rel_tol=0.0, abs_tol=5.0e-10
    ):
        raise SupportPlaneArtifactError("support-plane residual env pin mismatch")
    acceptance = _exact_keys(
        calibration["acceptance"], {"accepted", "reasons", "criteria"}, "acceptance"
    )
    if acceptance["accepted"] is not True or acceptance["reasons"] != []:
        raise SupportPlaneArtifactError("support-plane calibration was not accepted")
    if not isinstance(acceptance["criteria"], dict):
        raise SupportPlaneArtifactError("support-plane acceptance criteria are missing")
    runtime = _exact_keys(
        calibration["runtime_gate"],
        {
            "min_sample_count",
            "inlier_threshold_m",
            "min_inlier_ratio",
            "max_residual_median_m",
            "max_residual_p95_m",
        },
        "runtime_gate",
    )
    runtime_min_samples = _positive_int_value(
        runtime["min_sample_count"], "runtime_gate.min_sample_count"
    )
    runtime_threshold = _finite_number(
        runtime["inlier_threshold_m"], "runtime_gate.inlier_threshold_m"
    )
    runtime_min_ratio = _finite_number(
        runtime["min_inlier_ratio"], "runtime_gate.min_inlier_ratio"
    )
    runtime_max_median = _finite_number(
        runtime["max_residual_median_m"], "runtime_gate.max_residual_median_m"
    )
    runtime_max_p95 = _finite_number(
        runtime["max_residual_p95_m"], "runtime_gate.max_residual_p95_m"
    )
    if (
        runtime_threshold <= 0.0
        or not 0.0 <= runtime_min_ratio <= 1.0
        or runtime_max_median < 0.0
        or runtime_max_p95 < runtime_max_median
    ):
        raise SupportPlaneArtifactError("support-plane runtime gate is invalid")
    return SupportPlaneCalibration(
        artifact_sha256=actual_artifact_digest,
        canonical_payload_sha256=payload_digest,
        config_version=config_version,
        created_at_utc=created,
        valid_until_utc=valid_until,
        camera_serial=camera_serial,
        profile=profile,
        frame_id=frame_id,
        width=width,
        height=height,
        camera_info_sha256=camera_digest,
        alignment_id=alignment_id,
        depth_scale_m_per_unit=depth_scale,
        normal=normal,
        offset_m=offset,
        inlier_ratio=inlier_ratio,
        residual_p95_m=residual_p95,
        roi_polygons_px=tuple(polygons),
        hsv_lower=hsv_lower,
        hsv_upper=hsv_upper,
        erosion_kernel_px=erosion,
        depth_range_m=(float(depth_range[0]), float(depth_range[1])),
        runtime_min_sample_count=runtime_min_samples,
        runtime_inlier_threshold_m=runtime_threshold,
        runtime_min_inlier_ratio=runtime_min_ratio,
        runtime_max_residual_median_m=runtime_max_median,
        runtime_max_residual_p95_m=runtime_max_p95,
    )


def select_support_plane_points(
    *,
    frame_bgr: np.ndarray,
    depth_m: np.ndarray,
    camera_info: Mapping[str, Any],
    roi_polygons_px: Sequence[Sequence[Sequence[int]]],
    hsv_lower: Sequence[int],
    hsv_upper: Sequence[int],
    erosion_kernel_px: int,
    depth_range_m: Sequence[float],
    max_points: int,
    random_seed: int,
) -> np.ndarray:
    """Select bounded blue-surface 3-D samples in the RGB optical frame."""

    info = _camera_info_core(camera_info)
    height, width = frame_bgr.shape[:2]
    if frame_bgr.ndim != 3 or frame_bgr.shape[2] != 3:
        raise SupportPlaneArtifactError("frame_bgr must be HxWx3")
    if depth_m.shape != (height, width):
        raise SupportPlaneArtifactError("aligned depth dimensions do not match RGB")
    if (info["height"], info["width"]) != (height, width):
        raise SupportPlaneArtifactError("CameraInfo dimensions do not match RGB")
    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(
        hsv,
        np.asarray(hsv_lower, dtype=np.uint8),
        np.asarray(hsv_upper, dtype=np.uint8),
    )
    roi = np.zeros((height, width), dtype=np.uint8)
    for polygon in roi_polygons_px:
        cv2.fillPoly(roi, [np.asarray(polygon, dtype=np.int32)], 255)
    mask = cv2.bitwise_and(mask, roi)
    if erosion_kernel_px > 1:
        kernel = np.ones((erosion_kernel_px, erosion_kernel_px), dtype=np.uint8)
        mask = cv2.erode(mask, kernel, iterations=1)
    depth = np.asarray(depth_m, dtype=np.float64)
    valid = (
        (mask > 0)
        & np.isfinite(depth)
        & (depth >= float(depth_range_m[0]))
        & (depth <= float(depth_range_m[1]))
    )
    rows, columns = np.where(valid)
    if not len(columns):
        return np.empty((0, 3), dtype=np.float64)
    if max_points <= 0:
        raise SupportPlaneArtifactError("max_points must be positive")
    if len(columns) > max_points:
        selected = np.random.default_rng(random_seed).choice(
            len(columns), max_points, replace=False
        )
        rows = rows[selected]
        columns = columns[selected]
    pixels = np.column_stack((columns, rows)).astype(np.float64)
    rays_xy = cv2.undistortPoints(
        pixels.reshape(-1, 1, 2),
        np.asarray(info["k"], dtype=np.float64).reshape(3, 3),
        np.asarray(info["d"], dtype=np.float64),
    ).reshape(-1, 2)
    z = depth[rows, columns]
    return np.column_stack((rays_xy[:, 0] * z, rays_xy[:, 1] * z, z))


def fit_plane_ransac(
    points: np.ndarray,
    *,
    inlier_threshold_m: float,
    iterations: int,
    random_seed: int,
    evaluation_sample_limit: int = 20_000,
) -> PlaneFit:
    """Deterministic bounded RANSAC followed by inlier SVD refinement."""

    values = np.asarray(points, dtype=np.float64)
    if values.ndim != 2 or values.shape[1] != 3 or len(values) < 3:
        raise ValueError("at least three 3-D points are required")
    if not np.all(np.isfinite(values)):
        raise ValueError("plane points must be finite")
    if inlier_threshold_m <= 0.0 or iterations <= 0:
        raise ValueError("RANSAC bounds must be positive")
    rng = np.random.default_rng(random_seed)
    if len(values) > evaluation_sample_limit:
        evaluation = values[
            rng.choice(len(values), evaluation_sample_limit, replace=False)
        ]
    else:
        evaluation = values
    best_count = -1
    best_normal: np.ndarray | None = None
    best_offset = 0.0
    for _ in range(iterations):
        a, b, c = evaluation[rng.choice(len(evaluation), 3, replace=False)]
        normal = np.cross(b - a, c - a)
        length = float(np.linalg.norm(normal))
        if length < 1.0e-9:
            continue
        normal /= length
        offset = -float(normal @ a)
        count = int(
            np.count_nonzero(
                np.abs(evaluation @ normal + offset) <= inlier_threshold_m
            )
        )
        if count > best_count:
            best_count = count
            best_normal = normal
            best_offset = offset
    if best_normal is None:
        raise ValueError("RANSAC could not find a non-degenerate plane")
    initial_residuals = np.abs(values @ best_normal + best_offset)
    inliers = initial_residuals <= inlier_threshold_m
    if int(np.count_nonzero(inliers)) < 3:
        raise ValueError("RANSAC plane has too few inliers")
    center = values[inliers].mean(axis=0)
    _, _, vectors = np.linalg.svd(values[inliers] - center, full_matrices=False)
    normal = vectors[-1]
    normal /= np.linalg.norm(normal)
    offset = -float(normal @ center)
    if normal[2] > 0.0:
        normal *= -1.0
        offset *= -1.0
    residuals = np.abs(values @ normal + offset)
    return PlaneFit(
        normal=tuple(float(item) for item in normal),
        offset_m=offset,
        sample_count=int(len(values)),
        inlier_threshold_m=float(inlier_threshold_m),
        inlier_ratio=float(np.mean(residuals <= inlier_threshold_m)),
        residual_median_m=float(np.median(residuals)),
        residual_p95_m=float(np.percentile(residuals, 95.0)),
    )


def normal_angle_degrees(a: Sequence[float], b: Sequence[float]) -> float:
    left = np.asarray(a, dtype=np.float64)
    right = np.asarray(b, dtype=np.float64)
    left /= np.linalg.norm(left)
    right /= np.linalg.norm(right)
    return float(np.degrees(np.arccos(np.clip(left @ right, -1.0, 1.0))))
