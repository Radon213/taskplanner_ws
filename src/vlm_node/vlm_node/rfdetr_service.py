"""GPU RF-DETR preprocessing service for FLIR and CAM4 frames.

Run this module with the dedicated RF-DETR Python environment. ROS remains in
the Taskplanner container and talks to this process over a small local HTTP
contract, avoiding Python/CUDA dependency coupling.
"""

from __future__ import annotations

import argparse
import base64
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from typing import Any
import warnings

import cv2
from fastapi import FastAPI, HTTPException, Request
import numpy as np
from rfdetr import RFDETRSegSmall, RFDETRSmall
import supervision as sv
import torch
import uvicorn

from .rfdetr_contract import (
    CAMERA_BBOX_ONTOLOGY_VERSION,
    CAM4_CLASS_NAMES,
    FLIR_CLASS_NAMES,
    summarize_cam4_detections,
)


PALETTE = (
    (71, 99, 255),
    (50, 205, 50),
    (225, 105, 65),
    (0, 215, 255),
    (211, 85, 186),
    (209, 206, 0),
    (0, 140, 255),
    (60, 20, 220),
    (212, 255, 127),
    (140, 230, 240),
)


@dataclass(frozen=True, slots=True)
class CameraPreprocessProfile:
    """Deployment-specific crop/orientation applied before camera inference."""

    roi_xyxy: tuple[int, int, int, int] | None = None
    rotation_deg: int = 0
    threshold: float = 0.50


@dataclass(frozen=True, slots=True)
class CameraFrameTransform:
    """Invertible mapping from a model input back to its source image."""

    raw_width: int
    raw_height: int
    crop_x0: int
    crop_y0: int
    crop_width: int
    crop_height: int
    rotation_deg: int


def _parse_camera_roi(value: str, *, label: str) -> tuple[int, int, int, int] | None:
    normalized = str(value).strip()
    if not normalized:
        return None
    try:
        parts = tuple(int(part.strip()) for part in normalized.split(","))
    except ValueError as exc:
        raise ValueError(f"{label} must be x0,y0,x1,y1 integer pixels") from exc
    if len(parts) != 4:
        raise ValueError(f"{label} must contain exactly four integers")
    x0, y0, x1, y1 = parts
    if x0 < 0 or y0 < 0 or x1 <= x0 or y1 <= y0:
        raise ValueError(f"{label} must describe a positive, non-negative ROI")
    return x0, y0, x1, y1


def _normalize_camera_rotation(value: int, *, label: str) -> int:
    rotation = int(value)
    if rotation not in {0, 90, 180, 270}:
        raise ValueError(f"{label} must be one of 0, 90, 180, or 270 degrees")
    return rotation


def _validate_camera_threshold(value: float, *, label: str) -> float:
    threshold = float(value)
    if not 0.0 < threshold <= 1.0:
        raise ValueError(f"{label} must be greater than 0 and at most 1")
    return threshold


def _prepare_camera_frame(
    frame: np.ndarray,
    profile: CameraPreprocessProfile,
) -> tuple[np.ndarray, CameraFrameTransform]:
    """Crop/rotate one camera frame while retaining an exact inverse map."""

    if frame.ndim < 2:
        raise ValueError("camera frame must have at least two dimensions")
    raw_height, raw_width = frame.shape[:2]
    roi = profile.roi_xyxy or (0, 0, raw_width, raw_height)
    x0, y0, x1, y1 = roi
    if x1 > raw_width or y1 > raw_height:
        raise ValueError(
            "camera ROI exceeds the source frame: "
            f"roi={roi}, frame={raw_width}x{raw_height}"
        )
    cropped = frame[y0:y1, x0:x1]
    rotation = _normalize_camera_rotation(
        profile.rotation_deg,
        label="camera rotation",
    )
    if rotation == 90:
        processed = cv2.rotate(cropped, cv2.ROTATE_90_CLOCKWISE)
    elif rotation == 180:
        processed = cv2.rotate(cropped, cv2.ROTATE_180)
    elif rotation == 270:
        processed = cv2.rotate(cropped, cv2.ROTATE_90_COUNTERCLOCKWISE)
    else:
        processed = cropped
    return processed, CameraFrameTransform(
        raw_width=raw_width,
        raw_height=raw_height,
        crop_x0=x0,
        crop_y0=y0,
        crop_width=x1 - x0,
        crop_height=y1 - y0,
        rotation_deg=rotation,
    )


def _restore_camera_detections(
    detections: sv.Detections,
    transform: CameraFrameTransform,
) -> sv.Detections:
    """Map detector boxes from a cropped/rotated input to source pixels."""

    if len(detections) == 0:
        return detections
    boxes = np.asarray(detections.xyxy, dtype=float).copy()
    x0 = boxes[:, 0].copy()
    y0 = boxes[:, 1].copy()
    x1 = boxes[:, 2].copy()
    y1 = boxes[:, 3].copy()
    width = float(transform.crop_width)
    height = float(transform.crop_height)
    if transform.rotation_deg == 90:
        boxes[:, 0] = y0
        boxes[:, 1] = height - x1
        boxes[:, 2] = y1
        boxes[:, 3] = height - x0
    elif transform.rotation_deg == 180:
        boxes[:, 0] = width - x1
        boxes[:, 1] = height - y1
        boxes[:, 2] = width - x0
        boxes[:, 3] = height - y0
    elif transform.rotation_deg == 270:
        boxes[:, 0] = width - y1
        boxes[:, 1] = x0
        boxes[:, 2] = width - y0
        boxes[:, 3] = x1
    boxes[:, (0, 2)] += float(transform.crop_x0)
    boxes[:, (1, 3)] += float(transform.crop_y0)
    boxes[:, (0, 2)] = np.clip(boxes[:, (0, 2)], 0.0, transform.raw_width)
    boxes[:, (1, 3)] = np.clip(boxes[:, (1, 3)], 0.0, transform.raw_height)
    detections.xyxy = boxes
    return detections


def _color_for(class_id: int) -> tuple[int, int, int]:
    return PALETTE[int(class_id) % len(PALETTE)]


def _checkpoint_id(checkpoint: Path) -> str:
    """Return a stable, non-host-specific model identifier for diagnostics."""

    return f"{checkpoint.parent.name}/{checkpoint.name}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _camera_checkpoint_provenance(checkpoint: Path) -> dict[str, Any]:
    """Validate the CAM3/CAM4 checkpoint ABI and provide observable provenance.

    CAM3 and CAM4 are intentionally decoded through the exact three-class bbox
    ontology in :mod:`rfdetr_contract`. Loading a historical checkpoint with a
    different class-index order would silently mislabel observations, so a
    deployment must carry the reviewed training ``run_metadata.json`` beside
    the selected checkpoint. The manifest's preferred checkpoint digest is
    checked before model construction instead of merely trusting a mutable path.
    """

    metadata_path = checkpoint.parent / "run_metadata.json"
    if not metadata_path.is_file():
        raise ValueError(
            "CAM3/CAM4 checkpoint provenance is missing: expected "
            f"{metadata_path}. Refusing an unverified or legacy model."
        )
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"CAM3/CAM4 checkpoint provenance is invalid: {metadata_path}"
        ) from exc

    class_names = metadata.get("class_names")
    if not isinstance(class_names, list) or tuple(class_names) != CAM4_CLASS_NAMES:
        raise ValueError(
            "CAM3/CAM4 checkpoint class ABI is incompatible. Expected "
            f"{list(CAM4_CLASS_NAMES)!r}, received {class_names!r}."
        )
    preferred_checkpoint = metadata.get("preferred_checkpoint")
    if not isinstance(preferred_checkpoint, str) or (
        Path(preferred_checkpoint).name != checkpoint.name
    ):
        raise ValueError(
            "CAM3/CAM4 checkpoint is not the reviewed preferred checkpoint in "
            f"{metadata_path}."
        )
    expected_sha256 = metadata.get("preferred_checkpoint_sha256")
    if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
        raise ValueError(
            "CAM3/CAM4 checkpoint provenance lacks a valid preferred SHA-256 "
            f"in {metadata_path}."
        )
    actual_sha256 = _sha256_file(checkpoint)
    if actual_sha256.lower() != expected_sha256.lower():
        raise ValueError(
            "CAM3/CAM4 checkpoint digest does not match the reviewed training "
            f"manifest: expected {expected_sha256}, got {actual_sha256}."
        )
    return {
        "checkpoint_id": _checkpoint_id(checkpoint),
        "sha256": actual_sha256,
        "class_names": list(CAM4_CLASS_NAMES),
        "training_run": checkpoint.parent.name,
    }


def _decode_image(encoded: Any, *, label: str) -> np.ndarray:
    if not isinstance(encoded, str) or not encoded:
        raise ValueError(f"{label} image is missing")
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{label} image is not valid base64") from exc
    frame = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        raise ValueError(f"{label} image could not be decoded")
    return frame


def _bgr_to_model_rgb(frame: np.ndarray) -> np.ndarray:
    """Convert an OpenCV BGR frame to RF-DETR's contiguous NumPy RGB ABI."""

    if frame.ndim != 3 or frame.shape[2] != 3:
        raise ValueError("RF-DETR model input must be a three-channel BGR image")
    return np.ascontiguousarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))


def _encode_jpeg(frame: np.ndarray, quality: int) -> str:
    ok, encoded = cv2.imencode(
        ".jpg",
        frame,
        [cv2.IMWRITE_JPEG_QUALITY, int(quality)],
    )
    if not ok:
        raise RuntimeError("annotated perception frame could not be encoded")
    return base64.b64encode(encoded.tobytes()).decode("ascii")


def _encode_overlay_webp(frame: np.ndarray) -> str:
    ok, encoded = cv2.imencode(
        ".webp",
        frame,
        # OpenCV/libwebp uses a value above 100 for lossless mode. On the
        # deployed host this preserves RGBA exactly while encoding much faster
        # than PNG for these sparse overlays.
        [cv2.IMWRITE_WEBP_QUALITY, 101],
    )
    if not ok:
        raise RuntimeError("perception overlay could not be encoded")
    return base64.b64encode(encoded.tobytes()).decode("ascii")


def _render_flir_segmented(
    frame: np.ndarray,
    detections: sv.Detections,
    quality: int,
) -> str:
    return _encode_jpeg(_annotate_flir(frame, detections), quality)


def _render_flir_overlay(
    frame: np.ndarray,
    detections: sv.Detections,
) -> str:
    overlay = _flir_overlay(frame.shape, detections)
    height, width = overlay.shape[:2]
    overlay = cv2.resize(
        overlay,
        (max(1, width // 2), max(1, height // 2)),
        interpolation=cv2.INTER_AREA,
    )
    return _encode_overlay_webp(overlay)


def _render_cam4_annotated(
    frame: np.ndarray,
    detections: sv.Detections,
    quality: int,
) -> str:
    return _encode_jpeg(_annotate_cam4(frame, detections), quality)


def _render_cam4_overlay(
    frame: np.ndarray,
    detections: sv.Detections,
) -> str:
    return _encode_overlay_webp(_cam4_overlay(frame.shape, detections))


def _output_requested(
    payload: dict[str, Any],
    key: str,
    *,
    default: bool,
) -> bool:
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a JSON boolean")
    return value


def _records(
    detections: sv.Detections,
    class_names: tuple[str, ...],
) -> list[dict[str, Any]]:
    class_ids = (
        np.asarray(detections.class_id, dtype=int)
        if detections.class_id is not None
        else np.zeros(len(detections), dtype=int)
    )
    confidences = (
        np.asarray(detections.confidence, dtype=float)
        if detections.confidence is not None
        else np.ones(len(detections), dtype=float)
    )
    tracker_ids = (
        np.asarray(detections.tracker_id, dtype=int)
        if detections.tracker_id is not None
        else None
    )
    records: list[dict[str, Any]] = []
    for index, (box, class_id, confidence) in enumerate(
        zip(detections.xyxy, class_ids, confidences, strict=True)
    ):
        name = (
            class_names[int(class_id)]
            if 0 <= int(class_id) < len(class_names)
            else f"class_{int(class_id)}"
        )
        row: dict[str, Any] = {
            "class_id": int(class_id),
            "class_name": name,
            "confidence": round(float(confidence), 6),
            "xyxy": [round(float(value), 3) for value in box],
        }
        if tracker_ids is not None:
            row["tracker_id"] = int(tracker_ids[index])
        records.append(row)
    return records


def _annotate_flir(frame: np.ndarray, detections: sv.Detections) -> np.ndarray:
    output = frame.copy()
    class_ids = (
        np.asarray(detections.class_id, dtype=int)
        if detections.class_id is not None
        else np.zeros(len(detections), dtype=int)
    )
    confidences = (
        np.asarray(detections.confidence, dtype=float)
        if detections.confidence is not None
        else np.ones(len(detections), dtype=float)
    )
    tracker_ids = (
        np.asarray(detections.tracker_id, dtype=int)
        if detections.tracker_id is not None
        else np.full(len(detections), -1, dtype=int)
    )
    masks = detections.mask
    if masks is not None:
        overlay = np.zeros_like(output)
        used = np.zeros(output.shape[:2], dtype=bool)
        for mask, class_id in zip(masks, class_ids, strict=True):
            mask_array = np.asarray(mask).astype(bool)
            if mask_array.shape != output.shape[:2]:
                mask_array = cv2.resize(
                    mask_array.astype(np.uint8),
                    (output.shape[1], output.shape[0]),
                    interpolation=cv2.INTER_NEAREST,
                ).astype(bool)
            overlay[mask_array] = _color_for(int(class_id))
            used |= mask_array
        blended = cv2.addWeighted(output, 0.62, overlay, 0.38, 0.0)
        output[used] = blended[used]

    for box, class_id, confidence, tracker_id in zip(
        detections.xyxy,
        class_ids,
        confidences,
        tracker_ids,
        strict=True,
    ):
        x0, y0, x1, y1 = np.rint(box).astype(int)
        class_id = int(class_id)
        color = _color_for(class_id)
        cv2.rectangle(output, (x0, y0), (x1, y1), color, 2)
        name = (
            FLIR_CLASS_NAMES[class_id]
            if 0 <= class_id < len(FLIR_CLASS_NAMES)
            else f"class_{class_id}"
        )
        local_track_id = int(tracker_id) % 10_000
        label = f"{name} #{local_track_id} {float(confidence):.2f}"
        (width, height), baseline = cv2.getTextSize(
            label,
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            2,
        )
        label_y0 = max(0, y0 - height - baseline - 6)
        cv2.rectangle(
            output,
            (x0, label_y0),
            (x0 + width + 8, label_y0 + height + baseline + 6),
            color,
            -1,
        )
        cv2.putText(
            output,
            label,
            (x0 + 4, label_y0 + height + 2),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (0, 0, 0),
            2,
            cv2.LINE_AA,
        )
    return output


def _annotate_cam4(frame: np.ndarray, detections: sv.Detections) -> np.ndarray:
    output = frame.copy()
    class_ids = (
        np.asarray(detections.class_id, dtype=int)
        if detections.class_id is not None
        else np.zeros(len(detections), dtype=int)
    )
    confidences = (
        np.asarray(detections.confidence, dtype=float)
        if detections.confidence is not None
        else np.ones(len(detections), dtype=float)
    )
    for box, class_id, confidence in zip(
        detections.xyxy,
        class_ids,
        confidences,
        strict=True,
    ):
        x0, y0, x1, y1 = np.rint(box).astype(int)
        class_id = int(class_id)
        color = _color_for(class_id)
        cv2.rectangle(output, (x0, y0), (x1, y1), color, 2)
        name = (
            CAM4_CLASS_NAMES[class_id]
            if 0 <= class_id < len(CAM4_CLASS_NAMES)
            else f"class_{class_id}"
        )
        label = f"{name} {float(confidence):.2f}"
        (width, height), baseline = cv2.getTextSize(
            label,
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            2,
        )
        label_y0 = max(0, y0 - height - baseline - 6)
        cv2.rectangle(
            output,
            (x0, label_y0),
            (x0 + width + 8, label_y0 + height + baseline + 6),
            color,
            -1,
        )
        cv2.putText(
            output,
            label,
            (x0 + 4, label_y0 + height + 2),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (0, 0, 0),
            2,
            cv2.LINE_AA,
        )
    return output


def _flir_overlay(
    frame_shape: tuple[int, ...],
    detections: sv.Detections,
) -> np.ndarray:
    height, width = frame_shape[:2]
    overlay = np.zeros((height, width, 4), dtype=np.uint8)
    class_ids = (
        np.asarray(detections.class_id, dtype=int)
        if detections.class_id is not None
        else np.zeros(len(detections), dtype=int)
    )
    confidences = (
        np.asarray(detections.confidence, dtype=float)
        if detections.confidence is not None
        else np.ones(len(detections), dtype=float)
    )
    tracker_ids = (
        np.asarray(detections.tracker_id, dtype=int)
        if detections.tracker_id is not None
        else np.full(len(detections), -1, dtype=int)
    )
    masks = detections.mask
    if masks is not None:
        for mask, class_id in zip(masks, class_ids, strict=True):
            mask_array = np.asarray(mask).astype(bool)
            if mask_array.shape != (height, width):
                mask_array = cv2.resize(
                    mask_array.astype(np.uint8),
                    (width, height),
                    interpolation=cv2.INTER_NEAREST,
                ).astype(bool)
            color = _color_for(int(class_id))
            overlay[mask_array, :3] = color
            overlay[mask_array, 3] = 96

    for box, class_id, confidence, tracker_id in zip(
        detections.xyxy,
        class_ids,
        confidences,
        tracker_ids,
        strict=True,
    ):
        x0, y0, x1, y1 = np.rint(box).astype(int)
        class_id = int(class_id)
        color = (*_color_for(class_id), 255)
        cv2.rectangle(overlay, (x0, y0), (x1, y1), color, 2)
        name = (
            FLIR_CLASS_NAMES[class_id]
            if 0 <= class_id < len(FLIR_CLASS_NAMES)
            else f"class_{class_id}"
        )
        local_track_id = int(tracker_id) % 10_000
        label = f"{name} #{local_track_id} {float(confidence):.2f}"
        (label_width, label_height), baseline = cv2.getTextSize(
            label,
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            2,
        )
        label_y0 = max(0, y0 - label_height - baseline - 6)
        cv2.rectangle(
            overlay,
            (x0, label_y0),
            (
                x0 + label_width + 8,
                label_y0 + label_height + baseline + 6,
            ),
            color,
            -1,
        )
        cv2.putText(
            overlay,
            label,
            (x0 + 4, label_y0 + label_height + 2),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (0, 0, 0, 255),
            2,
            cv2.LINE_AA,
        )
    return overlay


def _cam4_overlay(
    frame_shape: tuple[int, ...],
    detections: sv.Detections,
) -> np.ndarray:
    height, width = frame_shape[:2]
    overlay = np.zeros((height, width, 4), dtype=np.uint8)
    class_ids = (
        np.asarray(detections.class_id, dtype=int)
        if detections.class_id is not None
        else np.zeros(len(detections), dtype=int)
    )
    confidences = (
        np.asarray(detections.confidence, dtype=float)
        if detections.confidence is not None
        else np.ones(len(detections), dtype=float)
    )
    for box, class_id, confidence in zip(
        detections.xyxy,
        class_ids,
        confidences,
        strict=True,
    ):
        x0, y0, x1, y1 = np.rint(box).astype(int)
        class_id = int(class_id)
        color = (*_color_for(class_id), 255)
        cv2.rectangle(overlay, (x0, y0), (x1, y1), color, 2)
        name = (
            CAM4_CLASS_NAMES[class_id]
            if 0 <= class_id < len(CAM4_CLASS_NAMES)
            else f"class_{class_id}"
        )
        label = f"{name} {float(confidence):.2f}"
        (label_width, label_height), baseline = cv2.getTextSize(
            label,
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            2,
        )
        label_y0 = max(0, y0 - label_height - baseline - 6)
        cv2.rectangle(
            overlay,
            (x0, label_y0),
            (
                x0 + label_width + 8,
                label_y0 + label_height + baseline + 6,
            ),
            color,
            -1,
        )
        cv2.putText(
            overlay,
            label,
            (x0 + 4, label_y0 + label_height + 2),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (0, 0, 0, 255),
            2,
            cv2.LINE_AA,
        )
    return overlay


class PerClassByteTrack:
    """Keep ByteTrack identities class-local, matching the validated pipeline."""

    def __init__(self, *, frame_rate: float) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FutureWarning)
            self._trackers = [
                sv.ByteTrack(
                    track_activation_threshold=0.30,
                    lost_track_buffer=20,
                    minimum_matching_threshold=0.95,
                    frame_rate=max(1.0, float(frame_rate)),
                    minimum_consecutive_frames=1,
                )
                for _ in FLIR_CLASS_NAMES
            ]

    def update(self, detections: sv.Detections) -> sv.Detections:
        if detections.class_id is None:
            return sv.Detections.empty()
        class_ids = np.asarray(detections.class_id, dtype=int)
        tracked_groups: list[sv.Detections] = []
        for class_id, tracker in enumerate(self._trackers):
            tracked = tracker.update_with_detections(
                detections[class_ids == class_id]
            )
            if len(tracked) == 0:
                continue
            if tracked.tracker_id is None:
                continue
            tracked.tracker_id = (
                np.asarray(tracked.tracker_id, dtype=int)
                + class_id * 10_000
            )
            tracked_groups.append(tracked)
        if not tracked_groups:
            return sv.Detections.empty()
        return sv.Detections.merge(tracked_groups)


class RFDETRPerceptionEngine:
    def __init__(
        self,
        *,
        flir_checkpoint: Path,
        cam4_checkpoint: Path,
        cam3_checkpoint: Path,
        frame_rate: float,
        optimize: bool,
        jpeg_quality: int,
        cam3_profile: CameraPreprocessProfile = CameraPreprocessProfile(),
        cam4_profile: CameraPreprocessProfile = CameraPreprocessProfile(),
    ) -> None:
        if not flir_checkpoint.is_file():
            raise FileNotFoundError(flir_checkpoint)
        if not cam4_checkpoint.is_file():
            raise FileNotFoundError(cam4_checkpoint)
        if not cam3_checkpoint.is_file():
            raise FileNotFoundError(cam3_checkpoint)
        if cam3_checkpoint.resolve() != cam4_checkpoint.resolve():
            raise ValueError(
                "CAM3 must use the configured CAM4 checkpoint; a second "
                "camera-model instance is intentionally not supported"
            )

        self._flir_checkpoint_id = _checkpoint_id(flir_checkpoint)
        self._camera_checkpoint_provenance = _camera_checkpoint_provenance(
            cam4_checkpoint
        )
        self._camera_model_version = (
            "rfdetr-"
            f"{self._camera_checkpoint_provenance['training_run'].replace('_', '-')}"
            f"-{self._camera_checkpoint_provenance['sha256'][:12]}"
        )
        self._camera_ontology_version = CAMERA_BBOX_ONTOLOGY_VERSION
        self._camera_profiles = {
            "cam_3": CameraPreprocessProfile(
                roi_xyxy=cam3_profile.roi_xyxy,
                rotation_deg=_normalize_camera_rotation(
                    cam3_profile.rotation_deg,
                    label="CAM3 rotation",
                ),
                threshold=_validate_camera_threshold(
                    cam3_profile.threshold,
                    label="CAM3 threshold",
                ),
            ),
            "cam_4": CameraPreprocessProfile(
                roi_xyxy=cam4_profile.roi_xyxy,
                rotation_deg=_normalize_camera_rotation(
                    cam4_profile.rotation_deg,
                    label="CAM4 rotation",
                ),
                threshold=_validate_camera_threshold(
                    cam4_profile.threshold,
                    label="CAM4 threshold",
                ),
            ),
        }
        started = time.perf_counter()
        self._flir_model = RFDETRSegSmall.from_checkpoint(str(flir_checkpoint))
        # CAM3 and CAM4 intentionally share one RF-DETRSmall instance.  The
        # checkpoint is the same by contract, so loading a duplicate model
        # would consume VRAM without increasing recognition capability.
        self._camera_model = RFDETRSmall.from_checkpoint(str(cam4_checkpoint))
        self._optimized = bool(optimize)
        if optimize:
            for model in (self._flir_model, self._camera_model):
                model.optimize_for_inference(
                    compile=True,
                    batch_size=1,
                    dtype=torch.float16,
                    inplace=False,
                )
            if torch.cuda.is_available():
                torch.cuda.synchronize()
        self._load_sec = time.perf_counter() - started
        self._tracker = PerClassByteTrack(frame_rate=frame_rate)
        self._jpeg_quality = max(60, min(98, int(jpeg_quality)))
        self._inference_pool = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="rfdetr-inference",
        )
        self._render_pool = ThreadPoolExecutor(
            max_workers=4,
            thread_name_prefix="rfdetr-render",
        )
        if torch.cuda.is_available():
            self._flir_stream: torch.cuda.Stream | None = torch.cuda.Stream()
            self._camera_stream: torch.cuda.Stream | None = torch.cuda.Stream()
        else:
            self._flir_stream = None
            self._camera_stream = None
        self._camera_inference_lock = threading.Lock()
        self._request_count = 0
        self._last_latency_ms = 0.0

    @property
    def health(self) -> dict[str, Any]:
        return {
            "status": "ready",
            "models": {
                "flir": "RFDETRSegSmall",
                "cam3": "RFDETRSmall (shared CAM4 checkpoint)",
                "cam4": "RFDETRSmall",
            },
            "checkpoint_provenance": {
                "flir": {
                    "checkpoint_id": self._flir_checkpoint_id,
                },
                "cam3": self._camera_checkpoint_provenance,
                "cam4": self._camera_checkpoint_provenance,
            },
            "camera_model_version": self._camera_model_version,
            "camera_ontology_version": self._camera_ontology_version,
            "cam3_cam4_share_model_instance": True,
            "camera_model_instances": 1,
            "camera_preprocessing": {
                view: {
                    "roi_xyxy": (
                        list(profile.roi_xyxy)
                        if profile.roi_xyxy is not None
                        else None
                    ),
                    "rotation_deg": profile.rotation_deg,
                    "threshold": profile.threshold,
                }
                for view, profile in self._camera_profiles.items()
            },
            "optimized": self._optimized,
            "load_sec": round(self._load_sec, 3),
            "request_count": self._request_count,
            "last_latency_ms": round(self._last_latency_ms, 3),
            "cuda_available": bool(torch.cuda.is_available()),
            "parallel_inference": True,
            "cuda_streams": (
                2
                if self._flir_stream is not None
                and self._camera_stream is not None
                else 0
            ),
        }

    @staticmethod
    def _predict(
        model: RFDETRSegSmall | RFDETRSmall,
        frame_bgr: np.ndarray,
        *,
        threshold: float,
        stream: torch.cuda.Stream | None,
    ) -> tuple[sv.Detections, float]:
        started = time.perf_counter()
        model_rgb = _bgr_to_model_rgb(frame_bgr)
        if stream is None:
            detections = model.predict(model_rgb, threshold=threshold)
        else:
            with torch.cuda.stream(stream):
                detections = model.predict(model_rgb, threshold=threshold)
            stream.synchronize()
        return detections, (time.perf_counter() - started) * 1000.0

    def _predict_camera_views(
        self,
        *,
        cam4: np.ndarray | None,
        cam3: np.ndarray | None,
    ) -> tuple[
        sv.Detections | None,
        float,
        sv.Detections | None,
        float,
    ]:
        """Run both overhead views serially on one shared model instance."""

        with self._camera_inference_lock:
            cam4_detections: sv.Detections | None = None
            cam4_inference_ms = 0.0
            cam3_detections: sv.Detections | None = None
            cam3_inference_ms = 0.0
            if cam4 is not None:
                cam4_model_input, cam4_transform = _prepare_camera_frame(
                    cam4,
                    self._camera_profiles["cam_4"],
                )
                cam4_detections, cam4_inference_ms = self._predict(
                    self._camera_model,
                    cam4_model_input,
                    threshold=self._camera_profiles["cam_4"].threshold,
                    stream=self._camera_stream,
                )
                cam4_detections = _restore_camera_detections(
                    cam4_detections,
                    cam4_transform,
                )
            if cam3 is not None:
                cam3_model_input, cam3_transform = _prepare_camera_frame(
                    cam3,
                    self._camera_profiles["cam_3"],
                )
                cam3_detections, cam3_inference_ms = self._predict(
                    self._camera_model,
                    cam3_model_input,
                    threshold=self._camera_profiles["cam_3"].threshold,
                    stream=self._camera_stream,
                )
                cam3_detections = _restore_camera_detections(
                    cam3_detections,
                    cam3_transform,
                )
        return (
            cam4_detections,
            cam4_inference_ms,
            cam3_detections,
            cam3_inference_ms,
        )

    def perceive(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_started = time.perf_counter()
        include_flir_segmented = _output_requested(
            payload,
            "include_flir_segmented_image",
            default=True,
        )
        include_cam4_annotated = _output_requested(
            payload,
            "include_cam4_annotated_image",
            default=True,
        )
        include_cam3_annotated = _output_requested(
            payload,
            "include_cam3_annotated_image",
            default=True,
        )
        flir_encoded = payload.get("flir_image_base64")
        flir = (
            _decode_image(flir_encoded, label="FLIR")
            if isinstance(flir_encoded, str) and flir_encoded
            else None
        )
        # CAM3/CAM4 are the normal local tool-recognition inputs.  FLIR is an
        # optional surgical-field view and must not prevent the overhead
        # detector from running when it is not present on the current VIPLab
        # calibration topology.
        if flir is None and not any(
            isinstance(payload.get(key), str) and payload.get(key)
            for key in ("cam4_image_base64", "cam3_image_base64")
        ):
            raise ValueError("at least one FLIR, CAM3, or CAM4 image is required")
        if flir is None:
            include_flir_segmented = False
        flir_stamp = float(payload.get("flir_stamp_sec", 0.0))
        cam4_encoded = payload.get("cam4_image_base64")
        cam4 = (
            _decode_image(cam4_encoded, label="CAM4")
            if isinstance(cam4_encoded, str) and cam4_encoded
            else None
        )
        cam4_stamp = (
            float(payload.get("cam4_stamp_sec", 0.0))
            if cam4 is not None
            else 0.0
        )
        cam3_encoded = payload.get("cam3_image_base64")
        cam3 = (
            _decode_image(cam3_encoded, label="CAM3")
            if isinstance(cam3_encoded, str) and cam3_encoded
            else None
        )
        cam3_stamp = (
            float(payload.get("cam3_stamp_sec", 0.0))
            if cam3 is not None
            else 0.0
        )
        decode_latency_ms = (
            time.perf_counter() - request_started
        ) * 1000.0

        inference_started = time.perf_counter()
        flir_future: Future[tuple[sv.Detections, float]] | None = None
        if flir is not None:
            flir_future = self._inference_pool.submit(
                self._predict,
                self._flir_model,
                flir,
                threshold=0.10,
                stream=self._flir_stream,
            )
        camera_future: Future[
            tuple[
                sv.Detections | None,
                float,
                sv.Detections | None,
                float,
            ]
        ] | None = None
        if cam4 is not None or cam3 is not None:
            camera_future = self._inference_pool.submit(
                self._predict_camera_views,
                cam4=cam4,
                cam3=cam3,
            )
        flir_candidates: sv.Detections | None = None
        flir_inference_ms = 0.0
        if flir_future is not None:
            flir_candidates, flir_inference_ms = flir_future.result()
        cam4_detections: sv.Detections | None = None
        cam4_inference_ms = 0.0
        cam3_detections: sv.Detections | None = None
        cam3_inference_ms = 0.0
        if camera_future is not None:
            (
                cam4_detections,
                cam4_inference_ms,
                cam3_detections,
                cam3_inference_ms,
            ) = camera_future.result()
        parallel_inference_ms = (
            time.perf_counter() - inference_started
        ) * 1000.0

        postprocess_started = time.perf_counter()
        tracked: sv.Detections | None = None
        flir_records: list[dict[str, Any]] = []
        if flir_candidates is not None:
            tracked = self._tracker.update(flir_candidates)
            flir_records = _records(tracked, FLIR_CLASS_NAMES)
        cam4_records: list[dict[str, Any]] = []
        if cam4_detections is not None:
            cam4_records = _records(cam4_detections, CAM4_CLASS_NAMES)
        cam3_records: list[dict[str, Any]] = []
        if cam3_detections is not None:
            cam3_records = _records(cam3_detections, CAM4_CLASS_NAMES)
        postprocess_latency_ms = (
            time.perf_counter() - postprocess_started
        ) * 1000.0

        render_started = time.perf_counter()
        render_jobs: dict[str, Future[str]] = {}
        if flir is not None and tracked is not None:
            render_jobs["flir_overlay"] = self._render_pool.submit(
                _render_flir_overlay,
                flir,
                tracked,
            )
        if flir is not None and tracked is not None and include_flir_segmented:
            render_jobs["flir_segmented"] = self._render_pool.submit(
                _render_flir_segmented,
                flir,
                tracked,
                self._jpeg_quality,
            )
        if cam4 is not None and cam4_detections is not None:
            render_jobs["cam4_overlay"] = self._render_pool.submit(
                _render_cam4_overlay,
                cam4,
                cam4_detections,
            )
            if include_cam4_annotated:
                render_jobs["cam4_annotated"] = self._render_pool.submit(
                    _render_cam4_annotated,
                    cam4,
                    cam4_detections,
                    self._jpeg_quality,
                )
        # CAM3 uses the exact CAM4 tool/hand checkpoint and its ontology, so
        # the same deterministic renderer and class palette apply.
        if cam3 is not None and cam3_detections is not None:
            render_jobs["cam3_overlay"] = self._render_pool.submit(
                _render_cam4_overlay,
                cam3,
                cam3_detections,
            )
            if include_cam3_annotated:
                render_jobs["cam3_annotated"] = self._render_pool.submit(
                    _render_cam4_annotated,
                    cam3,
                    cam3_detections,
                    self._jpeg_quality,
                )
        rendered = {
            name: future.result()
            for name, future in render_jobs.items()
        }
        render_encode_latency_ms = (
            time.perf_counter() - render_started
        ) * 1000.0
        service_latency_ms = (
            time.perf_counter() - request_started
        ) * 1000.0

        result = {
            "schema": "taskplanner.rfdetr_perception.v1",
            "diagnostics": {
                "flir": {
                    "source_stamp_sec": round(flir_stamp, 6),
                    "model": "RFDETRSegSmall",
                    "postprocess": "class-aware ByteTrack",
                    "inference_latency_ms": round(flir_inference_ms, 3),
                    "instances": flir_records,
                },
                "cam4": (
                    {
                        "source_stamp_sec": round(cam4_stamp, 6),
                        "model": (
                            "RFDETRSmall "
                            f"({self._camera_checkpoint_provenance['training_run']})"
                        ),
                        "model_version": self._camera_model_version,
                        "ontology_version": self._camera_ontology_version,
                        "model_provenance": self._camera_checkpoint_provenance,
                        "inference_latency_ms": round(cam4_inference_ms, 3),
                        "instances": cam4_records,
                    }
                    if cam4 is not None
                    else {
                        "status": "omitted_no_aligned_frame",
                        "model": (
                            "RFDETRSmall "
                            f"({self._camera_checkpoint_provenance['training_run']})"
                        ),
                        "model_version": self._camera_model_version,
                        "ontology_version": self._camera_ontology_version,
                        "model_provenance": self._camera_checkpoint_provenance,
                        "instances": [],
                    }
                ),
                "cam3": (
                    {
                        "source_stamp_sec": round(cam3_stamp, 6),
                        "model": (
                            "RFDETRSmall "
                            f"({self._camera_checkpoint_provenance['training_run']}; "
                            "shared CAM4 checkpoint)"
                        ),
                        "model_version": self._camera_model_version,
                        "ontology_version": self._camera_ontology_version,
                        "model_provenance": self._camera_checkpoint_provenance,
                        "inference_latency_ms": round(cam3_inference_ms, 3),
                        "instances": cam3_records,
                    }
                    if cam3 is not None
                    else {
                        "status": "omitted_no_aligned_frame",
                        "model": (
                            "RFDETRSmall "
                            f"({self._camera_checkpoint_provenance['training_run']}; "
                            "shared CAM4 checkpoint)"
                        ),
                        "model_version": self._camera_model_version,
                        "ontology_version": self._camera_ontology_version,
                        "model_provenance": self._camera_checkpoint_provenance,
                        "instances": [],
                    }
                ),
                "execution": (
                    "parallel_flir_with_serial_shared_camera_cuda"
                    if flir is not None and (cam4 is not None or cam3 is not None)
                    and self._flir_stream is not None
                    and self._camera_stream is not None
                    else "parallel_flir_with_serial_shared_camera_cpu"
                    if flir is not None and (cam4 is not None or cam3 is not None)
                    else "camera_only_shared_cuda"
                    if (cam4 is not None or cam3 is not None)
                    and self._camera_stream is not None
                    else "camera_only_shared_cpu"
                    if cam4 is not None or cam3 is not None
                    else "single_view"
                ),
                "decode_latency_ms": round(decode_latency_ms, 3),
                "parallel_inference_latency_ms": round(
                    parallel_inference_ms,
                    3,
                ),
                "postprocess_latency_ms": round(
                    postprocess_latency_ms,
                    3,
                ),
                "render_encode_latency_ms": round(
                    render_encode_latency_ms,
                    3,
                ),
                "pipeline_latency_ms": round(service_latency_ms, 3),
                "outputs": {
                    "flir_segmented_image": include_flir_segmented,
                    "flir_overlay_image": flir is not None,
                    "cam4_annotated_image": (
                        include_cam4_annotated and cam4 is not None
                    ),
                    "cam4_overlay_image": cam4 is not None,
                    "cam3_annotated_image": (
                        include_cam3_annotated and cam3 is not None
                    ),
                    "cam3_overlay_image": cam3 is not None,
                },
            },
        }
        if flir is not None and include_flir_segmented:
            result["flir_segmented_image"] = {
                "mime_type": "image/jpeg",
                "data_base64": rendered["flir_segmented"],
                "source_stamp_sec": round(flir_stamp, 6),
                "width": int(flir.shape[1]),
                "height": int(flir.shape[0]),
            }
        if flir is not None:
            result["flir_overlay_image"] = {
                "mime_type": "image/webp",
                "data_base64": rendered["flir_overlay"],
                "source_stamp_sec": round(flir_stamp, 6),
                "width": int(flir.shape[1] // 2),
                "height": int(flir.shape[0] // 2),
            }
        if cam4 is not None:
            result["cam4_semantics"] = summarize_cam4_detections(
                cam4_records,
                source_stamp_sec=cam4_stamp,
                inference_latency_ms=cam4_inference_ms,
            )
            result["cam4_overlay_image"] = {
                "mime_type": "image/webp",
                "data_base64": rendered["cam4_overlay"],
                "source_stamp_sec": round(cam4_stamp, 6),
                "width": int(cam4.shape[1]),
                "height": int(cam4.shape[0]),
            }
            if include_cam4_annotated:
                result["cam4_annotated_image"] = {
                    "mime_type": "image/jpeg",
                    "data_base64": rendered["cam4_annotated"],
                    "source_stamp_sec": round(cam4_stamp, 6),
                    "width": int(cam4.shape[1]),
                    "height": int(cam4.shape[0]),
                }
        if cam3 is not None:
            result["cam3_overlay_image"] = {
                "mime_type": "image/webp",
                "data_base64": rendered["cam3_overlay"],
                "source_stamp_sec": round(cam3_stamp, 6),
                "width": int(cam3.shape[1]),
                "height": int(cam3.shape[0]),
            }
            if include_cam3_annotated:
                result["cam3_annotated_image"] = {
                    "mime_type": "image/jpeg",
                    "data_base64": rendered["cam3_annotated"],
                    "source_stamp_sec": round(cam3_stamp, 6),
                    "width": int(cam3.shape[1]),
                    "height": int(cam3.shape[0]),
                }
        self._request_count += 1
        self._last_latency_ms = service_latency_ms
        return result


def create_app(engine: RFDETRPerceptionEngine) -> FastAPI:
    app = FastAPI(title="Taskplanner RF-DETR Perception", version="1")

    @app.get("/health")
    def health() -> dict[str, Any]:
        return engine.health

    @app.post("/v1/perceive")
    async def perceive(request: Request) -> dict[str, Any]:
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("request body must be an object")
            return engine.perceive(payload)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    return app


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--flir-checkpoint",
        type=Path,
        default=Path(
            os.environ.get(
                "RFDETR_FLIR_CHECKPOINT",
                "/models/rfdetr/flir/checkpoint_best_total.pth",
            )
        ),
    )
    parser.add_argument(
        "--cam4-checkpoint",
        type=Path,
        default=Path(
            os.environ.get(
                "RFDETR_CAM4_CHECKPOINT",
                "/models/rfdetr/cam4/checkpoint_best_total.pth",
            )
        ),
    )
    parser.add_argument(
        "--cam3-checkpoint",
        type=Path,
        default=Path(
            os.environ.get(
                "RFDETR_CAM3_CHECKPOINT",
                os.environ.get(
                    "RFDETR_CAM4_CHECKPOINT",
                    "/models/rfdetr/cam4/checkpoint_best_total.pth",
                ),
            )
        ),
        help=(
            "Must resolve to the CAM4 checkpoint. CAM3/CAM4 share one "
            "RF-DETRSmall instance to avoid a duplicate VRAM allocation."
        ),
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--frame-rate", type=float, default=15.0)
    parser.add_argument("--jpeg-quality", type=int, default=90)
    parser.add_argument(
        "--cam3-roi",
        default=os.environ.get("RFDETR_CAM3_ROI", ""),
        help="Optional CAM3 inference ROI as x0,y0,x1,y1 source pixels.",
    )
    parser.add_argument(
        "--cam4-roi",
        default=os.environ.get("RFDETR_CAM4_ROI", ""),
        help="Optional CAM4 inference ROI as x0,y0,x1,y1 source pixels.",
    )
    parser.add_argument(
        "--cam3-rotation",
        type=int,
        default=int(os.environ.get("RFDETR_CAM3_ROTATION", "0")),
        help="Clockwise CAM3 model-input rotation: 0, 90, 180, or 270.",
    )
    parser.add_argument(
        "--cam4-rotation",
        type=int,
        default=int(os.environ.get("RFDETR_CAM4_ROTATION", "0")),
        help="Clockwise CAM4 model-input rotation: 0, 90, 180, or 270.",
    )
    parser.add_argument(
        "--cam3-threshold",
        type=float,
        default=float(os.environ.get("RFDETR_CAM3_THRESHOLD", "0.20")),
    )
    parser.add_argument(
        "--cam4-threshold",
        type=float,
        default=float(os.environ.get("RFDETR_CAM4_THRESHOLD", "0.20")),
    )
    parser.add_argument("--no-optimize", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    engine = RFDETRPerceptionEngine(
        flir_checkpoint=args.flir_checkpoint,
        cam4_checkpoint=args.cam4_checkpoint,
        cam3_checkpoint=args.cam3_checkpoint,
        frame_rate=args.frame_rate,
        optimize=not args.no_optimize,
        jpeg_quality=args.jpeg_quality,
        cam3_profile=CameraPreprocessProfile(
            roi_xyxy=_parse_camera_roi(args.cam3_roi, label="CAM3 ROI"),
            rotation_deg=args.cam3_rotation,
            threshold=args.cam3_threshold,
        ),
        cam4_profile=CameraPreprocessProfile(
            roi_xyxy=_parse_camera_roi(args.cam4_roi, label="CAM4 ROI"),
            rotation_deg=args.cam4_rotation,
            threshold=args.cam4_threshold,
        ),
    )
    uvicorn.run(
        create_app(engine),
        host=str(args.host),
        port=int(args.port),
        log_level="info",
    )


if __name__ == "__main__":
    main()
