"""GPU RF-DETR preprocessing service for FLIR and CAM4 frames.

Run this module with the dedicated RF-DETR Python environment. ROS remains in
the Taskplanner container and talks to this process over a small local HTTP
contract, avoiding Python/CUDA dependency coupling.
"""

from __future__ import annotations

import argparse
import base64
from concurrent.futures import Future, ThreadPoolExecutor
import os
from pathlib import Path
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


def _color_for(class_id: int) -> tuple[int, int, int]:
    return PALETTE[int(class_id) % len(PALETTE)]


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
        frame_rate: float,
        optimize: bool,
        jpeg_quality: int,
    ) -> None:
        if not flir_checkpoint.is_file():
            raise FileNotFoundError(flir_checkpoint)
        if not cam4_checkpoint.is_file():
            raise FileNotFoundError(cam4_checkpoint)

        started = time.perf_counter()
        self._flir_model = RFDETRSegSmall.from_checkpoint(str(flir_checkpoint))
        self._cam4_model = RFDETRSmall.from_checkpoint(str(cam4_checkpoint))
        self._optimized = bool(optimize)
        if optimize:
            for model in (self._flir_model, self._cam4_model):
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
            self._cam4_stream: torch.cuda.Stream | None = torch.cuda.Stream()
        else:
            self._flir_stream = None
            self._cam4_stream = None
        self._request_count = 0
        self._last_latency_ms = 0.0

    @property
    def health(self) -> dict[str, Any]:
        return {
            "status": "ready",
            "models": {
                "flir": "RFDETRSegSmall",
                "cam4": "RFDETRSmall",
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
                and self._cam4_stream is not None
                else 0
            ),
        }

    @staticmethod
    def _predict(
        model: RFDETRSegSmall | RFDETRSmall,
        frame: np.ndarray,
        *,
        threshold: float,
        stream: torch.cuda.Stream | None,
    ) -> tuple[sv.Detections, float]:
        started = time.perf_counter()
        if stream is None:
            detections = model.predict(frame, threshold=threshold)
        else:
            with torch.cuda.stream(stream):
                detections = model.predict(frame, threshold=threshold)
            stream.synchronize()
        return detections, (time.perf_counter() - started) * 1000.0

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
        flir_stamp = float(payload.get("flir_stamp_sec", 0.0))
        flir = _decode_image(payload.get("flir_image_base64"), label="FLIR")
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
        decode_latency_ms = (
            time.perf_counter() - request_started
        ) * 1000.0

        inference_started = time.perf_counter()
        flir_future = self._inference_pool.submit(
            self._predict,
            self._flir_model,
            flir,
            threshold=0.10,
            stream=self._flir_stream,
        )
        cam4_future: Future[tuple[sv.Detections, float]] | None = None
        if cam4 is not None:
            cam4_future = self._inference_pool.submit(
                self._predict,
                self._cam4_model,
                cam4,
                threshold=0.50,
                stream=self._cam4_stream,
            )
        flir_candidates, flir_inference_ms = flir_future.result()
        cam4_detections: sv.Detections | None = None
        cam4_inference_ms = 0.0
        if cam4_future is not None:
            cam4_detections, cam4_inference_ms = cam4_future.result()
        parallel_inference_ms = (
            time.perf_counter() - inference_started
        ) * 1000.0

        postprocess_started = time.perf_counter()
        tracked = self._tracker.update(flir_candidates)
        flir_records = _records(tracked, FLIR_CLASS_NAMES)
        cam4_records: list[dict[str, Any]] = []
        if cam4_detections is not None:
            cam4_records = _records(cam4_detections, CAM4_CLASS_NAMES)
        postprocess_latency_ms = (
            time.perf_counter() - postprocess_started
        ) * 1000.0

        render_started = time.perf_counter()
        render_jobs: dict[str, Future[str]] = {
            "flir_overlay": self._render_pool.submit(
                _render_flir_overlay,
                flir,
                tracked,
            )
        }
        if include_flir_segmented:
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
            "flir_overlay_image": {
                "mime_type": "image/webp",
                "data_base64": rendered["flir_overlay"],
                "source_stamp_sec": round(flir_stamp, 6),
                "width": int(flir.shape[1] // 2),
                "height": int(flir.shape[0] // 2),
            },
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
                        "model": "RFDETRSmall",
                        "inference_latency_ms": round(cam4_inference_ms, 3),
                        "instances": cam4_records,
                    }
                    if cam4 is not None
                    else {
                        "status": "omitted_no_aligned_frame",
                        "model": "RFDETRSmall",
                        "instances": [],
                    }
                ),
                "execution": (
                    "parallel_cuda_streams"
                    if cam4 is not None and self._flir_stream is not None
                    else "parallel_cpu_workers"
                    if cam4 is not None
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
                    "flir_overlay_image": True,
                    "cam4_annotated_image": (
                        include_cam4_annotated and cam4 is not None
                    ),
                    "cam4_overlay_image": cam4 is not None,
                },
            },
        }
        if include_flir_segmented:
            result["flir_segmented_image"] = {
                "mime_type": "image/jpeg",
                "data_base64": rendered["flir_segmented"],
                "source_stamp_sec": round(flir_stamp, 6),
                "width": int(flir.shape[1]),
                "height": int(flir.shape[0]),
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
                "/home/arl/Documents/ARPA-H/rfdetr_0704_20260729/"
                "outputs/surg_full/checkpoint_best_total.pth",
            )
        ),
    )
    parser.add_argument(
        "--cam4-checkpoint",
        type=Path,
        default=Path(
            os.environ.get(
                "RFDETR_CAM4_CHECKPOINT",
                "/home/arl/Documents/ARPA-H/rfdetr_0704_20260729/"
                "outputs/mayo_full/checkpoint_best_total.pth",
            )
        ),
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--frame-rate", type=float, default=15.0)
    parser.add_argument("--jpeg-quality", type=int, default=90)
    parser.add_argument("--no-optimize", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    engine = RFDETRPerceptionEngine(
        flir_checkpoint=args.flir_checkpoint,
        cam4_checkpoint=args.cam4_checkpoint,
        frame_rate=args.frame_rate,
        optimize=not args.no_optimize,
        jpeg_quality=args.jpeg_quality,
    )
    uvicorn.run(
        create_app(engine),
        host=str(args.host),
        port=int(args.port),
        log_level="info",
    )


if __name__ == "__main__":
    main()
