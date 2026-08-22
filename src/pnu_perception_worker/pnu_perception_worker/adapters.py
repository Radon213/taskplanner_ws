"""Thin, explicit imports of hand-blood-tools inference cores.

The upstream checkout and all model files stay read-only deployment mounts.
This module intentionally contains adaptation/serialization only; Tool uses
``pnu_surgical_tool.SurgicalToolDetector``, Hand uses the upstream
``process_frame`` core, and Blood follows the upstream RF-DETR Seg-Small core.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import cv2
import numpy as np

from .config import WorkerConfig
from .contract import coco_rle
from .depth import DepthContext
from .support_plane import (
    RuntimePlaneValidation,
    SupportPlaneArtifactError,
    load_support_plane_calibration,
)
from .upstream_manifest import (
    UPSTREAM_EXECUTABLE_ROOTS,
    UPSTREAM_MANIFEST_COMMIT,
    UPSTREAM_SOURCE_MANIFEST,
)

TOOL_POINT_DEFINITION = "mask_internal_depth_valid_observed_surface_point_v1"
TOOL_AXIS_DEFINITION = (
    "+Y handle/proximal to working tip; +Z support plane to free space; +X=+Yx+Z"
)
SUPPORT_PLANE_DIAGNOSTICS_SCHEMA = "pnu.tool.support_plane_diagnostics.v1"
_MAX_SUPPORT_PLANE_REASONS = 16
_MAX_SUPPORT_PLANE_REASON_CHARS = 160


class AdapterLoadError(RuntimeError):
    """A configured upstream core or model could not be loaded safely."""


class AdapterRequestError(RuntimeError):
    """A single frame/calibration failed without proving model corruption."""


class AdapterOutputError(RuntimeError):
    """A single frame exceeded a reviewed output bound.

    These failures reject the current response, but do not prove that the
    loaded checkpoint/runtime is corrupt.  The engine therefore keeps the
    adapter available for the next frame.
    """


def _bounded_coco_rle(mask: Any, *, max_counts: int) -> dict[str, Any]:
    """Encode one mask while translating bounded frame output to adapter scope."""

    try:
        return coco_rle(mask, max_counts=max_counts)
    except ValueError as exc:
        raise AdapterOutputError(
            "per-frame segmentation output exceeds the reviewed RLE bound"
        ) from exc


def _metric_status(
    *, ready: bool, status: str, reasons: list[str] | tuple[str, ...]
) -> dict[str, Any]:
    return {
        "ready": bool(ready),
        "status": str(status),
        "reasons": [str(reason) for reason in reasons],
    }


def _bounded_support_plane_reasons(
    reasons: tuple[str, ...] | list[str],
    *,
    fallback: str,
) -> list[str]:
    """Keep operator-facing gate reasons finite and safe for JSON/ROS logs."""

    result: list[str] = []
    for reason in reasons[:_MAX_SUPPORT_PLANE_REASONS]:
        normalized = " ".join(str(reason).split())[:_MAX_SUPPORT_PLANE_REASON_CHARS]
        if normalized and normalized not in result:
            result.append(normalized)
    return result or [fallback]


def _depth_or_missing(depth: DepthContext | None) -> DepthContext:
    return depth or DepthContext(
        received=False,
        decoded=False,
        input_ready=False,
        reasons=("depth_missing",),
    )


def _mask_bbox(mask: np.ndarray) -> list[float]:
    ys, xs = np.where(np.asarray(mask, dtype=bool))
    if not len(xs):
        return [0.0, 0.0, 0.0, 0.0]
    return [
        float(xs.min()),
        float(ys.min()),
        float(xs.max() + 1),
        float(ys.max() + 1),
    ]


def _masked_centroid_depth(
    depth_m: np.ndarray,
    mask: np.ndarray,
    centroid_xy: list[float] | None,
) -> tuple[list[float] | None, float | None]:
    """Attach robust mask-internal depth without changing centroid semantics.

    A binary-mask centroid is not guaranteed to lie inside a concave mask or
    the union of disjoint instances. Sampling depth at that pixel can therefore
    report background geometry as blood. Preserve the mathematical 2-D mask
    centroid and associate it with the median of finite positive depth samples
    inside the mask. The bridge exposes this as centroid evidence, not as a
    robot suction target. If no mask-internal sample exists, emit no depth.
    """

    if centroid_xy is None:
        return None, None
    if depth_m.ndim != 2 or mask.shape != depth_m.shape:
        raise RuntimeError("Blood mask and aligned depth dimensions do not match")
    target = np.asarray(centroid_xy, dtype=np.float64)
    if target.shape != (2,) or not np.all(np.isfinite(target)):
        raise RuntimeError("Blood core returned an invalid centroid")
    valid = np.asarray(mask, dtype=bool) & np.isfinite(depth_m) & (depth_m > 0.0)
    samples = np.asarray(depth_m[valid], dtype=np.float64)
    if not samples.size:
        return [float(target[0]), float(target[1])], None
    return [float(target[0]), float(target[1])], float(np.median(samples))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_dir(root: Path) -> Path:
    marker = root / ".git"
    if marker.is_dir():
        return marker.resolve()
    if marker.is_file():
        value = marker.read_text(encoding="utf-8").strip()
        if value.startswith("gitdir: "):
            target = Path(value[8:])
            return (target if target.is_absolute() else root / target).resolve()
    raise AdapterLoadError(f"upstream checkout has no readable .git metadata: {root}")


def read_git_revision(root: Path) -> str:
    git_dir = _git_dir(root)
    head = (git_dir / "HEAD").read_text(encoding="ascii").strip()
    if len(head) == 40 and all(ch in "0123456789abcdef" for ch in head.lower()):
        return head.lower()
    if not head.startswith("ref: "):
        raise AdapterLoadError("upstream .git/HEAD is not a full commit or ref")
    ref_name = head[5:]
    if ref_name.startswith("/") or ".." in Path(ref_name).parts:
        raise AdapterLoadError("unsafe upstream git ref")
    loose = git_dir / ref_name
    if loose.is_file():
        revision = loose.read_text(encoding="ascii").strip().lower()
    else:
        revision = ""
        packed = git_dir / "packed-refs"
        if packed.is_file():
            for line in packed.read_text(encoding="ascii").splitlines():
                if not line or line.startswith(("#", "^")):
                    continue
                candidate, _, name = line.partition(" ")
                if name == ref_name:
                    revision = candidate.lower()
                    break
    if len(revision) != 40 or any(ch not in "0123456789abcdef" for ch in revision):
        raise AdapterLoadError(f"cannot resolve upstream git ref: {ref_name}")
    return revision


def verify_source_manifest(
    root: Path,
    manifest: dict[str, str] | None = None,
    executable_roots: tuple[str, ...] | None = None,
) -> None:
    """Verify imported source bytes and reject shadow executable artifacts."""

    root = root.resolve()
    manifest = manifest or UPSTREAM_SOURCE_MANIFEST
    executable_roots = executable_roots or UPSTREAM_EXECUTABLE_ROOTS
    expected_paths = set(manifest)
    for relative, expected_digest in manifest.items():
        path = root / relative
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise AdapterLoadError(
                "pinned upstream executable source is missing"
            ) from exc
        if (
            not resolved.is_relative_to(root)
            or path.is_symlink()
            or not resolved.is_file()
        ):
            raise AdapterLoadError(
                "pinned upstream executable source has an unsafe file type"
            )
        if sha256_file(resolved) != expected_digest:
            raise AdapterLoadError(
                "upstream executable source digest does not match pinned manifest"
            )

    executable_suffixes = {".py", ".pyc", ".pyo", ".so", ".pyd", ".dll", ".dylib"}
    for relative_root in executable_roots:
        source_root = root / relative_root
        if source_root.is_symlink() or not source_root.is_dir():
            raise AdapterLoadError("upstream executable source root is unsafe")
        for candidate in source_root.rglob("*"):
            if candidate.name == "__pycache__":
                raise AdapterLoadError(
                    "upstream executable source contains a forbidden bytecode cache"
                )
            if candidate.is_file() or candidate.is_symlink():
                relative = candidate.relative_to(root).as_posix()
                if (
                    candidate.suffix.casefold() in executable_suffixes
                    and relative not in expected_paths
                ):
                    raise AdapterLoadError(
                        "upstream executable source contains an unmanifested artifact"
                    )


def verify_upstream(config: WorkerConfig) -> str:
    root = config.upstream_root.resolve()
    if not root.is_dir():
        raise AdapterLoadError(f"upstream root is missing: {root}")
    revision = read_git_revision(root)
    if revision != config.expected_upstream_commit:
        raise AdapterLoadError(
            "upstream commit mismatch: "
            f"expected {config.expected_upstream_commit}, found {revision}"
        )
    if UPSTREAM_MANIFEST_COMMIT != config.expected_upstream_commit:
        raise AdapterLoadError(
            "worker source manifest does not match configured upstream commit"
        )
    verify_source_manifest(root)
    expected_ontology = (
        root / "components/tool_runtime_v1_6/algorithm/model/ontology.json"
    ).resolve()
    if config.tool_ontology.resolve() != expected_ontology:
        raise AdapterLoadError("PNU_TOOL_ONTOLOGY must use the pinned manifest asset")
    return revision


def _load_module_from_file(name: str, path: Path) -> ModuleType:
    resolved = path.resolve()
    if not resolved.is_file():
        raise AdapterLoadError(f"upstream module is missing: {resolved}")
    spec = importlib.util.spec_from_file_location(name, resolved)
    if spec is None or spec.loader is None:
        raise AdapterLoadError(f"cannot create module spec for {resolved}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


@dataclass(frozen=True)
class AdapterIdentity:
    name: str
    backend: str
    version: str
    digest_sha256: str


class ToolAdapter:
    def __init__(self, config: WorkerConfig) -> None:
        checkpoint = config.tool_checkpoint.resolve()
        ontology = config.tool_ontology.resolve()
        if not checkpoint.is_file():
            raise AdapterLoadError(f"Tool checkpoint is missing: {checkpoint}")
        if not ontology.is_file():
            raise AdapterLoadError(f"Tool ontology is missing: {ontology}")

        source_root = (
            config.upstream_root / "components/tool_runtime_v1_6/algorithm/src"
        ).resolve()
        sys.path.insert(0, str(source_root))
        try:
            package = importlib.import_module("pnu_surgical_tool")
            package_path = Path(package.__file__).resolve()
            if not package_path.is_relative_to(source_root):
                raise AdapterLoadError(
                    f"pnu_surgical_tool resolved outside upstream mount: {package_path}"
                )
            DetectorConfig = package.DetectorConfig
            CameraCalibration = package.CameraCalibration
            PlanarPoseEstimator = package.PlanarPoseEstimator
            SupportPlane = package.SupportPlane
            SurgicalToolAlgorithm = package.SurgicalToolAlgorithm
            SurgicalToolDetector = package.SurgicalToolDetector
        finally:
            try:
                sys.path.remove(str(source_root))
            except ValueError:
                pass

        import torch

        if config.device_policy == "cuda_required" and not torch.cuda.is_available():
            raise AdapterLoadError("Tool model requires CUDA but CUDA is unavailable")
        optimize = config.optimize_rfdetr and torch.cuda.is_available()
        detector_config = DetectorConfig(
            checkpoint_path=checkpoint,
            ontology_path=ontology,
            confidence_threshold=config.tool_threshold,
            optimize=optimize,
            jit_compile=optimize,
            fp16=optimize,
        )
        self._detector = SurgicalToolDetector(detector_config)
        self._detector.load()
        self._algorithm = SurgicalToolAlgorithm(self._detector, PlanarPoseEstimator())
        self._camera_type = CameraCalibration
        self._support_plane = SupportPlane(
            normal=np.asarray(config.tool_support_plane_normal, dtype=np.float64),
            offset_m=config.tool_support_plane_offset_m,
            config_version=config.tool_support_plane_config_version,
            inlier_ratio=config.tool_support_plane_inlier_ratio,
            residual_p95_m=config.tool_support_plane_residual_p95_m,
        )
        self._support_plane_validation_requested = bool(
            config.tool_support_plane_validated
        )
        self._support_plane_calibration = None
        self._support_plane_static_reasons: tuple[str, ...] = ()
        self._last_support_plane_validation = RuntimePlaneValidation(
            valid=False,
            reasons=("support_plane_validation_not_requested",),
        )
        if self._support_plane_validation_requested:
            try:
                self._support_plane_calibration = load_support_plane_calibration(
                    artifact_path=config.tool_support_plane_artifact,
                    expected_artifact_sha256=(
                        config.tool_support_plane_artifact_sha256
                    ),
                    expected_config_version=(
                        config.tool_support_plane_config_version
                    ),
                    expected_camera_serial=(
                        config.tool_support_plane_camera_serial
                    ),
                    expected_camera_profile=(
                        config.tool_support_plane_camera_profile
                    ),
                    expected_firmware_version=(
                        config.tool_support_plane_firmware_version
                    ),
                    expected_normal=config.tool_support_plane_normal,
                    expected_offset_m=config.tool_support_plane_offset_m,
                    expected_inlier_ratio=(
                        config.tool_support_plane_inlier_ratio
                    ),
                    expected_residual_p95_m=(
                        config.tool_support_plane_residual_p95_m
                    ),
                    max_age_days=config.tool_support_plane_max_age_days,
                )
            except SupportPlaneArtifactError as exc:
                # A bad/missing/stale artifact degrades only orientation. Tool
                # detection and depth-backed observed-surface position remain
                # available and never inherit a false calibration claim.
                self._support_plane_static_reasons = (
                    "support_plane_artifact_invalid",
                    " ".join(str(exc).split())[:_MAX_SUPPORT_PLANE_REASON_CHARS],
                )
        self._support_plane_validated = bool(
            self._support_plane_calibration is not None
        )
        self._max_detections = config.max_detections_per_algorithm
        self._max_total_rle_counts = config.max_total_rle_counts
        self.identity = AdapterIdentity(
            name="tool",
            backend="rfdetr",
            version=str(detector_config.model_version),
            digest_sha256=sha256_file(checkpoint),
        )

    def _encode_detection(
        self,
        instance: Any,
        total_rle_counts: int,
        *,
        include_model_class_index: bool = False,
    ) -> tuple[dict[str, Any], int]:
        encoded_mask = _bounded_coco_rle(
            instance.mask,
            max_counts=self._max_total_rle_counts - total_rle_counts,
        )
        row = {
            "instance_id": int(instance.frame_local_instance_id),
            "canonical_class_id": int(instance.canonical_class_id),
            "class_name": str(instance.class_name),
            "confidence": round(float(instance.class_confidence), 6),
            "bbox_xyxy_px": [round(float(value), 3) for value in instance.bbox_xyxy_px],
            "mask_rle": encoded_mask,
        }
        if include_model_class_index:
            row["model_class_index"] = int(instance.model_class_index)
        return (
            row,
            total_rle_counts + len(encoded_mask["counts"]),
        )

    @staticmethod
    def _point_flags(instance: Any) -> tuple[bool, bool, bool]:
        point_raw = instance.observation_point_uv_px
        if point_raw is None:
            return False, False, False
        try:
            point = np.asarray(point_raw, dtype=np.float64)
        except (TypeError, ValueError):
            return False, False, False
        if point.shape != (2,) or not np.all(np.isfinite(point)):
            return False, False, False
        u, v = (round(float(value)) for value in point)
        inside_image = (
            0 <= v < instance.mask.shape[0] and 0 <= u < instance.mask.shape[1]
        )
        inside_mask = bool(inside_image and instance.mask[v, u])
        point_depth = getattr(instance, "observation_point_depth_m", None)
        depth_valid = bool(
            instance.position_valid
            and inside_mask
            and isinstance(point_depth, (int, float, np.integer, np.floating))
            and not isinstance(point_depth, (bool, np.bool_))
            and np.isfinite(float(point_depth))
            and float(point_depth) > 0.0
        )
        return True, inside_mask, depth_valid

    def _encode_rgbd_instance(
        self,
        instance: Any,
        total_rle_counts: int,
        *,
        support_plane_validated: bool | None = None,
    ) -> tuple[dict[str, Any], int]:
        if support_plane_validated is None:
            support_plane_validated = bool(self._support_plane_validated)
        row, total_rle_counts = self._encode_detection(
            instance, total_rle_counts, include_model_class_index=True
        )
        point_valid, inside_mask, depth_valid = self._point_flags(instance)
        point = (
            [round(float(value), 3) for value in instance.observation_point_uv_px]
            if point_valid
            else None
        )
        point_depth = getattr(instance, "observation_point_depth_m", None)
        point_depth = round(float(point_depth), 6) if depth_valid else None
        position_raw = getattr(instance, "position_m", None)
        try:
            position_values = np.asarray(position_raw, dtype=np.float64)
        except (TypeError, ValueError):
            position_values = np.empty((0,), dtype=np.float64)
        position_valid = bool(
            instance.position_valid
            and depth_valid
            and position_values.shape == (3,)
            and np.all(np.isfinite(position_values))
            and position_values[2] > 0.0
        )
        if not position_valid:
            depth_valid = False
            point_depth = None

        orientation_raw = getattr(instance, "orientation_xyzw", None)
        try:
            orientation_values = np.asarray(orientation_raw, dtype=np.float64)
        except (TypeError, ValueError):
            orientation_values = np.empty((0,), dtype=np.float64)
        orientation_norm = (
            float(np.linalg.norm(orientation_values))
            if orientation_values.shape == (4,)
            and np.all(np.isfinite(orientation_values))
            else 0.0
        )
        orientation_valid = bool(
            position_valid
            and instance.orientation_valid
            and support_plane_validated
            and 0.999 <= orientation_norm <= 1.001
        )

        additional_flags: list[str] = []
        if not position_valid:
            additional_flags.append("POSITION_EVIDENCE_INVALID")
        if not support_plane_validated:
            additional_flags.append("SUPPORT_PLANE_UNVALIDATED")
        elif instance.orientation_valid and not orientation_valid:
            additional_flags.append("ORIENTATION_EVIDENCE_INVALID")

        raw_validity = str(instance.validity)
        if not position_valid:
            validity = "INVALID"
        elif not orientation_valid:
            validity = "INVALID" if raw_validity == "INVALID" else "DEGRADED"
        elif raw_validity in {"INVALID", "VALID", "DEGRADED", "STALE"}:
            validity = raw_validity
        else:
            validity = "DEGRADED"
            additional_flags.append("UPSTREAM_VALIDITY_UNKNOWN")

        raw_pose_mode = str(instance.pose_mode)
        if not position_valid:
            pose_mode = "INVALID"
        elif not orientation_valid:
            pose_mode = "POSITION_3D_ONLY"
        elif raw_pose_mode in {
            "PLANAR_4DOF_WITH_NORMAL_PRIOR",
            "FULL_6D",
            "AMBIGUOUS",
        }:
            pose_mode = raw_pose_mode
        else:
            pose_mode = "AMBIGUOUS"
            additional_flags.append("UPSTREAM_POSE_MODE_UNKNOWN")

        invalid_reason_parts = (
            [str(instance.invalid_reason)] if instance.invalid_reason else []
        )
        invalid_reason_parts.extend(additional_flags)
        if validity == "INVALID" and not invalid_reason_parts:
            invalid_reason_parts.append("UPSTREAM_POSE_INVALID")
        status_flags = [
            str(flag)
            for flag in instance.status_flags
            if not (
                support_plane_validated
                and str(flag) == "SUPPORT_PLANE_UNVALIDATED"
            )
        ]
        status_flags.extend(
            flag for flag in additional_flags if flag not in status_flags
        )
        observation = {
            "mask_bbox_xyxy_px": _mask_bbox(instance.mask),
            "mask_area_px": int(np.count_nonzero(instance.mask)),
            "observation_point_uv_px": point,
            "observation_point_valid": point_valid,
            "observation_point_inside_mask": inside_mask,
            "observation_point_depth_valid": depth_valid,
            "observation_point_depth_m": point_depth,
            "observation_point_selection_mode": (
                str(instance.observation_point_selection_mode) if point_valid else ""
            ),
            "observation_point_boundary_clearance_px": round(
                float(instance.observation_point_boundary_clearance_px)
                if point_valid
                else 0.0,
                6,
            ),
        }
        pose = {
            "position_m": (
                [round(float(value), 6) for value in position_values]
                if position_valid
                else None
            ),
            "orientation_xyzw": (
                [round(float(value), 8) for value in orientation_values]
                if orientation_valid
                else None
            ),
            "pose_mode": pose_mode,
            "position_valid": position_valid,
            "orientation_valid": orientation_valid,
            "dof_observed": [
                position_valid,
                position_valid,
                position_valid,
                False,
                False,
                orientation_valid,
            ],
            "observation_point_definition": TOOL_POINT_DEFINITION,
            "axis_definition": TOOL_AXIS_DEFINITION,
            "symmetry_type": str(instance.symmetry_type),
            "endpoint_sign_confidence": round(
                float(instance.endpoint_sign_confidence), 6
            ),
            "valid_depth_ratio": round(float(instance.valid_depth_ratio), 6),
            "pose_point_count": int(instance.pose_point_count),
            "axis_anisotropy": round(float(instance.axis_anisotropy), 6),
            "support_plane_inlier_ratio": round(
                float(self._support_plane.inlier_ratio or 0.0), 6
            ),
            "support_plane_residual_p95_m": round(
                float(self._support_plane.residual_p95_m or 0.0), 6
            ),
            "pose_confidence": 0.0,
            "pose_confidence_calibrated": False,
            "validity": validity,
            "status_flags": status_flags,
            "invalid_reason": ";".join(invalid_reason_parts),
        }
        row["observation"] = observation
        row["pose"] = pose
        return row, total_rle_counts

    def _support_plane_diagnostics(self) -> dict[str, Any]:
        """Serialize static calibration and latest live drift evidence separately."""

        requested = bool(
            getattr(self, "_support_plane_validation_requested", False)
        )
        calibration = getattr(self, "_support_plane_calibration", None)
        artifact_loaded = calibration is not None
        static_reasons_raw = tuple(
            getattr(self, "_support_plane_static_reasons", ())
        )
        if artifact_loaded:
            static_reasons: list[str] = []
        elif static_reasons_raw:
            static_reasons = _bounded_support_plane_reasons(
                static_reasons_raw,
                fallback="support_plane_artifact_invalid",
            )
        elif requested:
            static_reasons = ["support_plane_artifact_unavailable"]
        else:
            static_reasons = ["support_plane_validation_not_requested"]

        last = getattr(self, "_last_support_plane_validation", None)
        if not isinstance(last, RuntimePlaneValidation):
            last = RuntimePlaneValidation(
                valid=False,
                reasons=("support_plane_runtime_not_evaluated",),
            )
        evaluated = bool(last.evaluated)
        metrics_available = bool(last.metrics_available and evaluated)
        runtime_valid = bool(last.valid and evaluated and artifact_loaded)
        runtime_reasons = (
            []
            if runtime_valid
            else _bounded_support_plane_reasons(
                list(last.reasons),
                fallback="support_plane_runtime_not_evaluated",
            )
        )

        fit_inlier_ratio = (
            round(float(calibration.inlier_ratio), 6)
            if artifact_loaded
            else None
        )
        fit_residual_p95_m = (
            round(float(calibration.residual_p95_m), 6)
            if artifact_loaded
            else None
        )
        runtime_inlier_ratio = (
            round(float(last.inlier_ratio), 6) if metrics_available else None
        )
        runtime_residual_median_m = (
            round(float(last.residual_median_m), 6)
            if metrics_available and last.residual_median_m is not None
            else None
        )
        runtime_residual_p95_m = (
            round(float(last.residual_p95_m), 6)
            if metrics_available and last.residual_p95_m is not None
            else None
        )
        return {
            "schema": SUPPORT_PLANE_DIAGNOSTICS_SCHEMA,
            "validation_requested": requested,
            "artifact_loaded": artifact_loaded,
            "static_reasons": static_reasons,
            "calibration_fit": {
                "available": artifact_loaded,
                "inlier_ratio": fit_inlier_ratio,
                "residual_p95_m": fit_residual_p95_m,
            },
            "runtime_validation": {
                "evaluated": evaluated,
                "metrics_available": metrics_available,
                "valid": runtime_valid,
                "reasons": runtime_reasons,
                "sample_count": int(last.sample_count) if evaluated else 0,
                "inlier_ratio": runtime_inlier_ratio,
                "residual_median_m": runtime_residual_median_m,
                "residual_p95_m": runtime_residual_p95_m,
                "camera_info_sha256": (
                    str(last.camera_info_sha256)[:64] if evaluated else ""
                ),
            },
        }

    def _validate_support_plane_for_frame(
        self,
        *,
        request: Any,
        depth: DepthContext,
        frame_bgr: np.ndarray,
    ) -> bool:
        calibration = getattr(self, "_support_plane_calibration", None)
        requested = getattr(
            self,
            "_support_plane_validation_requested",
            bool(getattr(self, "_support_plane_validated", False)),
        )
        if calibration is None:
            # Keep private unit-test adapters backwards-compatible while the
            # production constructor always requires a verified artifact.
            legacy_valid = bool(
                requested
                and getattr(self, "_support_plane_validated", False)
                and not hasattr(self, "_support_plane_validation_requested")
            )
            if not legacy_valid:
                unavailable_reason = (
                    "support_plane_artifact_unavailable"
                    if requested
                    else "support_plane_validation_not_requested"
                )
                self._last_support_plane_validation = RuntimePlaneValidation(
                    valid=False,
                    reasons=(unavailable_reason,),
                )
            return legacy_valid
        if not requested or not depth.input_ready or depth.depth_m is None:
            self._last_support_plane_validation = RuntimePlaneValidation(
                valid=False,
                reasons=("support_plane_metric_depth_unavailable",),
            )
            return False
        validation = calibration.validate_frame(
            request=request,
            depth_m=depth.depth_m,
            depth_scale_m_per_unit=depth.depth_scale_m_per_unit,
            alignment_id=depth.alignment_id,
            frame_bgr=frame_bgr,
        )
        self._last_support_plane_validation = validation
        return bool(validation.valid)

    def infer(
        self,
        frame_bgr: np.ndarray,
        request: Any,
        depth: DepthContext | None = None,
    ) -> dict[str, Any]:
        depth = _depth_or_missing(depth)
        if depth.input_ready:
            assert depth.depth_m is not None
            support_plane_validated = self._validate_support_plane_for_frame(
                request=request,
                depth=depth,
                frame_bgr=frame_bgr,
            )
            info = request.metadata["color_camera_info"]
            camera = self._camera_type(
                width=int(info["width"]),
                height=int(info["height"]),
                k=np.asarray(info["k"], dtype=np.float64).reshape(3, 3),
                distortion=np.asarray(info["d"], dtype=np.float64),
                frame_name=str(info["frame_id"]),
                calibration_version=str(depth.alignment_id),
            )
            try:
                result = self._algorithm.detect_and_estimate(
                    image=frame_bgr,
                    aligned_depth_m=depth.depth_m,
                    camera=camera,
                    support_plane=self._support_plane,
                    color_order="BGR",
                    frame_key=request.source["rgb"]["stamp_ns"],
                )
            except cv2.error as exc:
                raise AdapterRequestError(
                    "tool RGB-D calibration or per-frame geometry is invalid"
                ) from exc
            instances = result.instances
            image_width = camera.width
            image_height = camera.height
            rgbd_versions = {
                "ontology_version": str(result.ontology_version),
                "calibration_version": str(result.calibration_version),
                "pose_convention_version": str(result.pose_convention_version),
            }
        else:
            batch = self._detector.predict(frame_bgr, "BGR")
            instances = batch.instances
            image_width = int(batch.image_width)
            image_height = int(batch.image_height)
        if len(instances) > self._max_detections:
            raise AdapterOutputError(
                "Tool detection count exceeds the configured limit"
            )
        rows: list[dict[str, Any]] = []
        total_rle_counts = 0
        for instance in instances:
            if depth.input_ready:
                row, total_rle_counts = self._encode_rgbd_instance(
                    instance,
                    total_rle_counts,
                    support_plane_validated=support_plane_validated,
                )
            else:
                row, total_rle_counts = self._encode_detection(
                    instance, total_rle_counts
                )
            rows.append(row)
        payload = {
            "schema": "pnu.tool.rgbd.v1" if depth.input_ready else "pnu.tool.2d.v1",
            "image": {
                "width": image_width,
                "height": image_height,
            },
            "detections": rows,
        }
        if depth.input_ready:
            payload["metric_3d"] = _metric_status(
                ready=True, status="ready", reasons=[]
            )
            payload.update(rgbd_versions)
            payload["support_plane_config_version"] = str(
                self._support_plane.config_version
            )
            payload["support_plane_validated"] = bool(
                support_plane_validated
            )
            payload["support_plane_diagnostics"] = (
                self._support_plane_diagnostics()
            )
        return payload


class BloodAdapter:
    def __init__(self, config: WorkerConfig) -> None:
        checkpoint = config.blood_checkpoint.resolve()
        if not checkpoint.is_file():
            raise AdapterLoadError(f"Blood checkpoint is missing: {checkpoint}")
        script = (
            config.upstream_root
            / "components/blood_detection/offline_blood_segmentation.py"
        )
        self._upstream = _load_module_from_file("_pnu_upstream_blood_detection", script)

        import torch
        from rfdetr import RFDETRSegSmall

        if config.device_policy == "cuda_required" and not torch.cuda.is_available():
            raise AdapterLoadError("Blood model requires CUDA but CUDA is unavailable")
        self._torch = torch
        self._threshold = config.blood_threshold
        self._max_detections = config.max_detections_per_algorithm
        self._max_total_rle_counts = config.max_total_rle_counts
        self._model = RFDETRSegSmall.from_checkpoint(str(checkpoint))
        if config.optimize_rfdetr and torch.cuda.is_available():
            self._model.optimize_for_inference(
                compile=True,
                batch_size=1,
                dtype=torch.float16,
                inplace=False,
            )
        self.identity = AdapterIdentity(
            name="blood",
            backend="rfdetr",
            version="RF-DETR-Seg-Small-blood",
            digest_sha256=sha256_file(checkpoint),
        )

    def infer(
        self,
        frame_bgr: np.ndarray,
        request: Any,
        depth: DepthContext | None = None,
    ) -> dict[str, Any]:
        depth = _depth_or_missing(depth)
        if self._torch.cuda.is_available():
            self._torch.cuda.synchronize()
        detections = self._model.predict(
            frame_bgr,
            threshold=self._threshold,
            include_source_image=False,
        )
        if self._torch.cuda.is_available():
            self._torch.cuda.synchronize()
        raw_masks = getattr(detections, "mask", None)
        if raw_masks is None:
            raise RuntimeError("Blood checkpoint did not return segmentation masks")
        height, width = frame_bgr.shape[:2]
        if len(detections.xyxy) > self._max_detections:
            raise AdapterOutputError(
                "Blood detection count exceeds the configured limit"
            )
        rows: list[dict[str, Any]] = []
        union_mask = np.zeros((height, width), dtype=bool)
        total_rle_counts = 0
        for index, (box, class_id, confidence) in enumerate(
            zip(
                detections.xyxy,
                detections.class_id,
                detections.confidence,
                strict=True,
            )
        ):
            if int(class_id) != 0:
                raise RuntimeError(
                    f"Blood checkpoint returned unexpected class index {class_id}"
                )
            mask = np.asarray(raw_masks[index], dtype=bool)
            if mask.shape != (height, width):
                mask = cv2.resize(
                    mask.astype(np.uint8),
                    (width, height),
                    interpolation=cv2.INTER_NEAREST,
                ).astype(bool)
            union_mask |= mask
            center = self._upstream.centroid(mask)
            centroid_depth_m = None
            if depth.input_ready:
                assert depth.depth_m is not None
                center, centroid_depth_m = _masked_centroid_depth(
                    depth.depth_m, mask, center
                )
            encoded_mask = _bounded_coco_rle(
                mask,
                max_counts=self._max_total_rle_counts - total_rle_counts,
            )
            total_rle_counts += len(encoded_mask["counts"])
            row = {
                "instance_id": index,
                "class_id": 1,
                "class_name": "blood",
                "confidence": round(float(confidence), 6),
                "bbox_xyxy_px": [round(float(value), 3) for value in box],
                "centroid_xy_px": center,
                "mask_rle": encoded_mask,
            }
            if depth.input_ready:
                row["centroid_depth_m"] = centroid_depth_m
            rows.append(row)
        combined_centroid = self._upstream.centroid(union_mask)
        combined_depth_m = None
        if depth.input_ready:
            assert depth.depth_m is not None
            combined_centroid, combined_depth_m = _masked_centroid_depth(
                depth.depth_m, union_mask, combined_centroid
            )
        payload = {
            "schema": "pnu.blood.rgbd.v1" if depth.input_ready else "pnu.blood.2d.v1",
            "image": {"width": width, "height": height},
            "detections": rows,
        }
        if depth.input_ready:
            payload.update(
                combined_blood_centroid_xy_px=combined_centroid,
                combined_blood_centroid_depth_m=combined_depth_m,
                metric_3d=_metric_status(ready=True, status="ready", reasons=[]),
            )
        return payload


class HandAdapter:
    def __init__(self, config: WorkerConfig) -> None:
        model = config.hand_model.resolve()
        if not model.is_file():
            raise AdapterLoadError(f"Hand model asset is missing: {model}")
        if model.name != "hand_landmarker.task":
            raise AdapterLoadError(
                "PNU_HAND_MODEL must be mounted with basename hand_landmarker.task"
            )
        core_path = (
            config.upstream_root
            / "components/hand_keypoints_ros/ros2_ws/src/hand_keypoint_ros/hand_keypoint_ros/core.py"
        )
        self._core = _load_module_from_file("_pnu_upstream_hand_core", core_path)
        # load_mediapipe() calls ensure_mediapipe_model(); pointing its cache at
        # the read-only asset prevents any network download or hidden mutation.
        self._core.MEDIAPIPE_CACHE = str(model.parent)
        self._mp, self._detector = self._core.load_mediapipe(
            config.max_hands, cpu_only=True
        )
        self._last_media_timestamp_ms = -1
        self.identity = AdapterIdentity(
            name="hand",
            backend="mediapipe",
            version="MediaPipe-HandLandmarker-0.10.18",
            digest_sha256=sha256_file(model),
        )

    @staticmethod
    def _intrinsics(request: Any, width: int, height: int) -> tuple[Any, ...]:
        info = request.metadata.get("color_camera_info")
        if info is not None:
            values = np.asarray(info["k"], dtype=np.float32).reshape(3, 3)
            distortion = np.asarray(info["d"], dtype=np.float32)
            fx, fy, cx, cy = (
                float(values[0, 0]),
                float(values[1, 1]),
                float(values[0, 2]),
                float(values[1, 2]),
            )
            if fx > 0.0 and fy > 0.0:
                return values, distortion, fx, fy, cx, cy
        focal = float(max(width, height))
        values = np.array(
            [[focal, 0.0, width / 2.0], [0.0, focal, height / 2.0], [0, 0, 1]],
            dtype=np.float32,
        )
        return (
            values,
            np.zeros((5,), dtype=np.float32),
            focal,
            focal,
            width / 2.0,
            height / 2.0,
        )

    def infer(
        self,
        frame_bgr: np.ndarray,
        request: Any,
        depth: DepthContext | None = None,
    ) -> dict[str, Any]:
        depth = _depth_or_missing(depth)
        height, width = frame_bgr.shape[:2]
        matrix, distortion, fx, fy, cx, cy = self._intrinsics(request, width, height)
        source_ms = int(request.source["rgb"]["stamp_ns"] // 1_000_000)
        media_timestamp = max(source_ms, self._last_media_timestamp_ms + 1)
        self._last_media_timestamp_ms = media_timestamp
        if depth.input_ready:
            assert depth.depth_m is not None
            depth_map = depth.depth_m
            depth_source_label = "RGB-ALIGNED METRIC DEPTH"
        else:
            depth_map = np.full((height, width), np.nan, dtype=np.float32)
            depth_source_label = "2D ONLY"
        hands, _overlay, _total_valid_kps = self._core.process_frame(
            frame_bgr,
            depth_map,
            self._detector,
            self._mp,
            matrix,
            fx,
            fy,
            cx,
            cy,
            width,
            height,
            media_timestamp,
            draw_overlay=False,
            depth_source_label=depth_source_label,
            allow_2d_only=True,
        )
        rows = []
        for hand in hands:
            handedness = hand.get("handedness")
            if not isinstance(handedness, dict):
                handedness = {"label": "Unknown", "score": 0.0}
            label = str(handedness.get("label", "Unknown"))
            if label not in {"Left", "Right"}:
                label = "Unknown"
            score = float(handedness.get("score", 0.0))
            row = {
                "hand_index": int(hand["hand_index"]),
                "handedness": {
                    "label": label,
                    "score": round(min(1.0, max(0.0, score)), 6),
                },
                "joints_2d": hand["joints_2d"],
                "kp_scores": hand["kp_scores"],
            }
            if depth.input_ready:
                joints_3d = hand.get("joints_3d")
                valid_depth = hand.get("kp_valid_depth")
                joints_2d = hand.get("joints_2d")
                if not isinstance(joints_3d, list) or len(joints_3d) != 21:
                    raise RuntimeError("Hand core returned invalid joints_3d shape")
                if not isinstance(valid_depth, list) or len(valid_depth) != 21:
                    raise RuntimeError(
                        "Hand core returned invalid kp_valid_depth shape"
                    )
                if not isinstance(joints_2d, list) or len(joints_2d) != 21:
                    raise RuntimeError("Hand core returned invalid joints_2d shape")
                try:
                    pixel_points = np.asarray(joints_2d, dtype=np.float64)
                except (TypeError, ValueError) as exc:
                    raise RuntimeError("Hand core returned invalid 2-D joints") from exc
                if pixel_points.shape != (21, 2) or not np.all(
                    np.isfinite(pixel_points)
                ):
                    raise RuntimeError("Hand core returned non-finite 2-D joints")
                try:
                    normalized_rays = cv2.undistortPoints(
                        pixel_points.reshape(-1, 1, 2),
                        np.asarray(matrix, dtype=np.float64),
                        np.asarray(distortion, dtype=np.float64),
                    ).reshape(-1, 2)
                except cv2.error as exc:
                    raise AdapterRequestError(
                        "hand RGB-D calibration cannot undistort keypoints"
                    ) from exc
                serialized_joints: list[list[float]] = []
                serialized_valid_depth: list[bool] = []
                for index, (joint, joint_2d, upstream_valid) in enumerate(
                    zip(joints_3d, joints_2d, valid_depth, strict=True)
                ):
                    if not isinstance(joint, (list, tuple)) or len(joint) != 3:
                        raise RuntimeError("Hand core returned invalid 3-D joint")
                    if not isinstance(joint_2d, (list, tuple)) or len(joint_2d) != 2:
                        raise RuntimeError("Hand core returned invalid 2-D joint")
                    values = [float(value) for value in joint]
                    uv = [float(value) for value in joint_2d]
                    in_frame = 0.0 <= uv[0] < width and 0.0 <= uv[1] < height
                    z_m = float(values[2])
                    ray = normalized_rays[index]
                    valid = bool(upstream_valid) and in_frame
                    valid = bool(
                        valid
                        and np.isfinite(z_m)
                        and z_m > 0.0
                        and np.all(np.isfinite(ray))
                    )
                    serialized_valid_depth.append(valid)
                    if valid:
                        corrected = [float(ray[0]) * z_m, float(ray[1]) * z_m, z_m]
                        serialized_joints.append(
                            [round(value, 6) for value in corrected]
                        )
                    else:
                        serialized_joints.append([0.0, 0.0, 0.0])
                row["joints_3d"] = serialized_joints
                row["kp_valid_depth"] = serialized_valid_depth
                required_palm_joints = (0, 2, 9, 17)
                if not all(
                    serialized_valid_depth[index] for index in required_palm_joints
                ):
                    row["palm_6d"] = None
                else:
                    corrected = np.asarray(serialized_joints, dtype=np.float64)
                    translation, rotation = self._core.palm_frame_v2(
                        corrected[0], corrected[2], corrected[9], corrected[17]
                    )
                    translation = np.asarray(translation, dtype=np.float64)
                    rotation = np.asarray(rotation, dtype=np.float64)
                    quat_wxyz = np.asarray(
                        self._core.rot_to_quat_wxyz(rotation), dtype=np.float64
                    )
                    rotation_error = (
                        np.linalg.norm(rotation.T @ rotation - np.eye(3))
                        if rotation.shape == (3, 3)
                        else np.inf
                    )
                    determinant = (
                        float(np.linalg.det(rotation))
                        if rotation.shape == (3, 3)
                        else 0.0
                    )
                    quaternion_norm = (
                        float(np.linalg.norm(quat_wxyz))
                        if quat_wxyz.shape == (4,)
                        else 0.0
                    )
                    if (
                        translation.shape != (3,)
                        or rotation.shape != (3, 3)
                        or quat_wxyz.shape != (4,)
                        or not np.all(np.isfinite(translation))
                        or not np.all(np.isfinite(rotation))
                        or not np.all(np.isfinite(quat_wxyz))
                        or rotation_error > 5.0e-3
                        or not 0.995 <= determinant <= 1.005
                        or not 0.999 <= quaternion_norm <= 1.001
                    ):
                        row["palm_6d"] = None
                    else:
                        row["palm_6d"] = {
                            "translation": [
                                round(float(value), 6) for value in translation
                            ],
                            "orientation_xyzw": [
                                round(float(quat_wxyz[index]), 8)
                                for index in (1, 2, 3, 0)
                            ],
                            "rotation_matrix": [
                                round(float(value), 8) for value in rotation.reshape(-1)
                            ],
                        }
            rows.append(row)
        payload = {
            "schema": "pnu.hand.rgbd.v1" if depth.input_ready else "pnu.hand.2d.v1",
            "image": {"width": width, "height": height},
            "hands": rows,
        }
        if depth.input_ready:
            payload["metric_3d"] = _metric_status(
                ready=True, status="ready", reasons=[]
            )
        return payload


def load_adapters(config: WorkerConfig) -> tuple[str, dict[str, Any], dict[str, str]]:
    """Load each model independently so health exposes every failed gate."""
    revision = verify_upstream(config)
    loaded: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for name, adapter_type in (
        ("tool", ToolAdapter),
        ("blood", BloodAdapter),
        ("hand", HandAdapter),
    ):
        try:
            loaded[name] = adapter_type(config)
        except Exception as exc:  # noqa: BLE001 - third-party load failures are per-model state
            errors[name] = f"{type(exc).__name__}: {exc}"
    return revision, loaded, errors
