"""Bounded, latest-frame inference orchestration."""

from __future__ import annotations

import inspect
import struct
import threading
import time
from typing import Any

import cv2
import numpy as np

from . import RESPONSE_SCHEMA
from .adapters import AdapterOutputError, AdapterRequestError
from .config import WorkerConfig
from .contract import ALGORITHMS, InferenceRequest
from .depth import DepthContext, qualify_aligned_depth


class WorkerBusyError(RuntimeError):
    """The zero-depth queue is busy; callers should retain only the latest frame."""


class ModelsUnavailableError(RuntimeError):
    def __init__(self, unavailable: list[str]) -> None:
        self.unavailable = unavailable
        super().__init__("requested models are unavailable: " + ", ".join(unavailable))


class InferenceDeadlineError(TimeoutError):
    """A frame expired before all requested algorithms could execute."""


class InvalidImageError(ValueError):
    """RGB bytes do not decode to a supported bounded image."""


def _preflight_rgb_dimensions(
    payload: bytes,
    declared_format: str,
    *,
    max_pixels: int,
    max_decoded_bytes: int,
) -> tuple[int, int]:
    """Read PNG IHDR/JPEG SOF bounds before OpenCV can allocate output."""

    width = height = decoded_channels = 0
    detected_format = ""
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        detected_format = "png"
        if len(payload) < 29 or payload[12:16] != b"IHDR":
            raise InvalidImageError("RGB PNG has no valid IHDR header")
        ihdr_length = struct.unpack(">I", payload[8:12])[0]
        if ihdr_length != 13:
            raise InvalidImageError("RGB PNG has an invalid IHDR length")
        width, height = struct.unpack(">II", payload[16:24])
        bit_depth = int(payload[24])
        color_type = int(payload[25])
        channels_by_color_type = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
        source_channels = channels_by_color_type.get(color_type, 0)
        if bit_depth != 8 or source_channels == 0:
            raise InvalidImageError("RGB PNG must use a supported 8-bit color encoding")
        decoded_channels = max(3, source_channels)
    elif payload.startswith(b"\xff\xd8"):
        detected_format = "jpeg"
        position = 2
        sof_markers = {
            0xC0,
            0xC1,
            0xC2,
            0xC3,
            0xC5,
            0xC6,
            0xC7,
            0xC9,
            0xCA,
            0xCB,
            0xCD,
            0xCE,
            0xCF,
        }
        while position < len(payload):
            while position < len(payload) and payload[position] != 0xFF:
                position += 1
            while position < len(payload) and payload[position] == 0xFF:
                position += 1
            if position >= len(payload):
                break
            marker = int(payload[position])
            position += 1
            if marker in {0x00, 0x01, 0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
                continue
            if position + 2 > len(payload):
                break
            segment_length = struct.unpack(">H", payload[position : position + 2])[0]
            if segment_length < 2 or position + segment_length > len(payload):
                raise InvalidImageError("RGB JPEG contains an invalid segment")
            if marker in sof_markers:
                if segment_length < 8:
                    raise InvalidImageError("RGB JPEG SOF segment is truncated")
                precision = int(payload[position + 2])
                height = struct.unpack(">H", payload[position + 3 : position + 5])[0]
                width = struct.unpack(">H", payload[position + 5 : position + 7])[0]
                components = int(payload[position + 7])
                if precision != 8 or components not in {1, 3}:
                    raise InvalidImageError(
                        "RGB JPEG must use 8-bit grayscale or three components"
                    )
                decoded_channels = 3
                break
            if marker == 0xDA:
                break
            position += segment_length
        if width == 0 or height == 0:
            raise InvalidImageError("RGB JPEG has no supported SOF dimensions")
    else:
        raise InvalidImageError("RGB binary part is not a JPEG or PNG container")

    normalized_declared = declared_format.casefold()
    declared_matches = (
        detected_format == "jpeg"
        and ("jpeg" in normalized_declared or "jpg" in normalized_declared)
    ) or (detected_format == "png" and "png" in normalized_declared)
    if not declared_matches:
        raise InvalidImageError("RGB payload container does not match source format")
    if width <= 0 or height <= 0 or width * height > max_pixels:
        raise InvalidImageError("RGB container dimensions exceed the configured limit")
    if width * height * decoded_channels > max_decoded_bytes:
        raise InvalidImageError("RGB decoded image bytes exceed the configured limit")
    return int(width), int(height)


class PerceptionEngine:
    def __init__(
        self,
        config: WorkerConfig,
        *,
        upstream_revision: str,
        adapters: dict[str, Any],
        load_errors: dict[str, str] | None = None,
    ) -> None:
        self.config = config
        self.upstream_revision = upstream_revision
        self.adapters = dict(adapters)
        self.load_errors = dict(load_errors or {})
        self._inference_lock = threading.Lock()

    def model_records(self, *, executed: set[str] | None = None) -> dict[str, Any]:
        executed = executed or set()
        records: dict[str, Any] = {}
        for name in ALGORITHMS:
            adapter = self.adapters.get(name)
            if adapter is None:
                records[name] = {
                    "ready": False,
                    "executed": False,
                    "status": "unavailable",
                    "version": None,
                    "digest_sha256": None,
                    "backend": None,
                    "error": self.load_errors.get(name, "not loaded"),
                }
            else:
                identity = adapter.identity
                did_execute = name in executed
                records[name] = {
                    "ready": True,
                    "executed": did_execute,
                    "status": "executed" if did_execute else "loaded",
                    "version": identity.version,
                    "digest_sha256": identity.digest_sha256,
                    "backend": identity.backend,
                    "error": None,
                }
        return records

    @property
    def ready(self) -> bool:
        return all(name in self.adapters for name in ALGORITHMS)

    @staticmethod
    def _run_adapter(
        adapter: Any,
        frame: np.ndarray,
        request: InferenceRequest,
        depth: DepthContext,
    ) -> dict[str, Any]:
        """Pass depth to v2 adapters while retaining private v1 test adapters."""
        parameters = inspect.signature(adapter.infer).parameters.values()
        accepts_depth = (
            any(
                parameter.kind
                in {
                    inspect.Parameter.VAR_POSITIONAL,
                    inspect.Parameter.VAR_KEYWORD,
                }
                for parameter in parameters
            )
            or len(inspect.signature(adapter.infer).parameters) >= 3
        )
        if accepts_depth:
            return adapter.infer(frame, request, depth)
        return adapter.infer(frame, request)

    def infer(
        self,
        request: InferenceRequest,
        rgb_bytes: bytes,
        depth_bytes: bytes | None,
    ) -> dict[str, Any]:
        if not self._inference_lock.acquire(blocking=False):
            raise WorkerBusyError(
                "worker is busy; queue_depth=0, send the latest frame"
            )
        started = time.perf_counter()
        try:
            unavailable = [
                name
                for name in request.requested_algorithms
                if name not in self.adapters
            ]
            if unavailable:
                raise ModelsUnavailableError(unavailable)
            if int(time.time() * 1000) >= request.deadline_unix_ms:
                raise InferenceDeadlineError("request expired before decode")

            decode_started = time.perf_counter()
            container_width, container_height = _preflight_rgb_dimensions(
                rgb_bytes,
                str(request.source["rgb"]["format"]),
                max_pixels=self.config.max_image_pixels,
                max_decoded_bytes=self.config.max_decoded_rgb_bytes,
            )
            frame = cv2.imdecode(
                np.frombuffer(rgb_bytes, dtype=np.uint8), cv2.IMREAD_COLOR
            )
            if frame is None:
                raise InvalidImageError("RGB binary part is not a decodable image")
            height, width = frame.shape[:2]
            if (width, height) != (container_width, container_height):
                raise InvalidImageError(
                    "decoded RGB dimensions do not match container preflight"
                )
            if (
                width <= 0
                or height <= 0
                or width * height > self.config.max_image_pixels
            ):
                raise InvalidImageError(
                    "decoded RGB image dimensions exceed the configured limit"
                )
            color_info = request.metadata.get("color_camera_info")
            if color_info is not None and (
                color_info["width"] != width or color_info["height"] != height
            ):
                raise InvalidImageError(
                    "decoded RGB dimensions do not match color_camera_info"
                )
            latency: dict[str, float] = {}
            depth_context: DepthContext = qualify_aligned_depth(
                request,
                depth_bytes,
                rgb_width=width,
                rgb_height=height,
                config=self.config,
            )
            latency["decode"] = round(
                (time.perf_counter() - decode_started) * 1000.0, 3
            )
            results: dict[str, Any] = {}
            executed: set[str] = set()
            for name in request.requested_algorithms:
                if int(time.time() * 1000) >= request.deadline_unix_ms:
                    raise InferenceDeadlineError(
                        f"request expired before {name} inference; partial result discarded"
                    )
                model_started = time.perf_counter()
                try:
                    results[name] = self._run_adapter(
                        self.adapters[name], frame, request, depth_context
                    )
                except (AdapterRequestError, AdapterOutputError):
                    # Calibration/frame geometry and reviewed response bounds
                    # are request-scoped. Keeping the already-loaded model
                    # prevents one authenticated bad frame from permanently
                    # degrading worker health.
                    raise
                except Exception as exc:
                    self.load_errors[name] = (
                        f"runtime {type(exc).__name__}: model execution failed"
                    )
                    self.adapters.pop(name, None)
                    raise
                results[name]["executed"] = True
                latency[name] = round((time.perf_counter() - model_started) * 1000.0, 3)
                executed.add(name)
            if int(time.time() * 1000) >= request.deadline_unix_ms:
                raise InferenceDeadlineError(
                    "request expired during inference; completed result discarded"
                )
            latency["total"] = round((time.perf_counter() - started) * 1000.0, 3)
            return {
                "schema": RESPONSE_SCHEMA,
                "request_id": request.request_id,
                "generated_unix_ms": int(time.time() * 1000),
                "source": request.source,
                "accepted_algorithms": list(request.requested_algorithms),
                "upstream": {
                    "repository": "hanwae-py/hand-blood-tools",
                    "commit": self.upstream_revision,
                },
                "models": self.model_records(executed=executed),
                "latency_ms": latency,
                "results": results,
                "metric_3d": depth_context.public_gate(),
                "depth_evidence": depth_context.public_evidence(
                    request, rgb_width=width, rgb_height=height
                ),
                "depth_received": depth_bytes is not None,
            }
        finally:
            self._inference_lock.release()
