"""Fail-closed decoding and qualification of RGB-aligned metric depth."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np

from .config import WorkerConfig
from .contract import InferenceRequest

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class InvalidDepthError(ValueError):
    """A supplied compressedDepth part cannot be decoded safely."""


@dataclass(frozen=True)
class DepthContext:
    """One decoded depth frame and the evidence authorizing metric use."""

    received: bool
    decoded: bool
    input_ready: bool
    reasons: tuple[str, ...]
    raw_shape: tuple[int, int] | None = None
    depth_m: np.ndarray | None = None
    depth_scale_m_per_unit: float | None = None
    valid_pixels: int = 0
    valid_ratio: float = 0.0
    alignment_id: str | None = None

    @property
    def has_valid_samples(self) -> bool:
        return self.input_ready and self.valid_pixels > 0

    def public_gate(self) -> dict[str, Any]:
        """Keep the top-level v1 gate intentionally small and stable."""
        return {"ready": self.input_ready, "reasons": list(self.reasons)}

    def public_evidence(
        self,
        request: InferenceRequest,
        *,
        rgb_width: int,
        rgb_height: int,
    ) -> dict[str, Any]:
        source_depth = request.source.get("depth", {})
        alignment = request.metadata.get("alignment", {})
        return {
            "received": self.received,
            "decoded": self.decoded,
            "alignment_validated": bool(
                isinstance(alignment, dict) and alignment.get("validated") is True
            ),
            "alignment_id": (
                str(alignment.get("id", "")) if isinstance(alignment, dict) else ""
            ),
            "rgb_frame_id": str(request.source["rgb"]["frame_id"]),
            "depth_frame_id": (
                str(source_depth.get("frame_id"))
                if isinstance(source_depth, dict) and source_depth.get("frame_id")
                else ""
            ),
            "rgb_shape_hw": [int(rgb_height), int(rgb_width)],
            "depth_shape_hw": (
                list(self.raw_shape) if self.raw_shape is not None else None
            ),
            "depth_scale_m_per_unit": float(
                request.metadata.get("depth_scale_m_per_unit", 0.0)
            ),
            "depth_scale_validated": bool(
                request.metadata.get("depth_scale_validated") is True
            ),
            "valid_pixels": int(self.valid_pixels),
            "valid_ratio": round(float(self.valid_ratio), 8),
        }


def decode_compressed_depth_16uc1(
    payload: bytes,
    message_format: str,
    *,
    max_pixels: int | None = None,
) -> np.ndarray:
    """Decode ROS image_transport ``16UC1; compressedDepth png`` bytes.

    image_transport prepends a codec header. Searching for the PNG signature
    matches the pinned upstream decoder while avoiding a platform-specific
    fixed header length. 32FC1 inverse-depth is deliberately unsupported.
    """

    declared_encoding = message_format.split(";", 1)[0].strip().upper()
    if declared_encoding != "16UC1" or "compresseddepth" not in message_format.lower():
        raise InvalidDepthError("depth format must be '16UC1; compressedDepth png'")
    signature_offset = payload.find(PNG_SIGNATURE)
    if signature_offset < 0:
        raise InvalidDepthError(
            "compressedDepth binary part does not contain a PNG signature"
        )
    png = payload[signature_offset:]
    if len(png) < 24 or png[12:16] != b"IHDR":
        raise InvalidDepthError("compressedDepth PNG has no valid IHDR header")
    width, height = struct.unpack(">II", png[16:24])
    if width <= 0 or height <= 0:
        raise InvalidDepthError("compressedDepth PNG dimensions must be positive")
    if max_pixels is not None and width * height > max_pixels:
        raise InvalidDepthError(
            "compressedDepth PNG dimensions exceed the configured limit"
        )
    decoded = cv2.imdecode(
        np.frombuffer(png, dtype=np.uint8),
        cv2.IMREAD_UNCHANGED,
    )
    if decoded is None:
        raise InvalidDepthError("compressedDepth PNG cannot be decoded")
    if decoded.ndim != 2 or decoded.dtype != np.uint16:
        raise InvalidDepthError(
            "compressedDepth must decode to a uint16 single-channel image"
        )
    return decoded


def qualify_aligned_depth(
    request: InferenceRequest,
    depth_bytes: bytes | None,
    *,
    rgb_width: int,
    rgb_height: int,
    config: WorkerConfig,
) -> DepthContext:
    """Decode depth and enable metric use only when every RGB-grid gate passes.

    A missing or unvalidated depth frame is a normal 2-D request. A malformed
    binary part is not: it is rejected so corruption is never disguised as an
    empty or 2-D-only perception result.
    """

    if depth_bytes is None:
        return DepthContext(
            received=False,
            decoded=False,
            input_ready=False,
            reasons=("depth_missing",),
        )

    source = request.source.get("depth")
    if not isinstance(source, dict):  # parse_metadata normally prevents this.
        raise InvalidDepthError("depth source metadata is missing")
    depth_raw = decode_compressed_depth_16uc1(
        depth_bytes,
        str(source.get("format", "")),
        max_pixels=config.max_image_pixels,
    )
    if depth_raw.size > config.max_image_pixels:
        raise InvalidDepthError(
            "decoded depth image dimensions exceed the configured limit"
        )

    reasons: list[str] = []
    if source.get("aligned") is not True:
        reasons.append("depth_not_declared_rgb_aligned")

    alignment = request.metadata.get("alignment")
    alignment_id: str | None = None
    if isinstance(alignment, dict):
        alignment_id = str(alignment.get("id", "")).strip()
    if not isinstance(alignment, dict) or alignment.get("validated") is not True:
        reasons.append("rgb_depth_alignment_unvalidated")
    if not alignment_id:
        reasons.append("rgb_depth_alignment_id_missing")

    rgb_source = request.source["rgb"]
    if source.get("frame_id") != rgb_source.get("frame_id"):
        reasons.append("depth_frame_is_not_rgb_frame")

    color_info = request.metadata.get("color_camera_info")
    if not isinstance(color_info, dict):
        reasons.append("color_camera_info_missing")
    else:
        if color_info.get("frame_id") != rgb_source.get("frame_id"):
            reasons.append("color_camera_info_frame_mismatch")
        if (int(color_info.get("height", 0)), int(color_info.get("width", 0))) != (
            rgb_height,
            rgb_width,
        ):
            reasons.append("color_camera_info_dimension_mismatch")
        intrinsics = color_info.get("k", [])
        if (
            not isinstance(intrinsics, list)
            or len(intrinsics) != 9
            or float(intrinsics[0]) <= 0.0
            or float(intrinsics[4]) <= 0.0
            or abs(float(intrinsics[8])) < 1.0e-12
        ):
            reasons.append("color_camera_info_intrinsics_invalid")

    depth_info = request.metadata.get("depth_camera_info")
    if not isinstance(depth_info, dict):
        reasons.append("depth_camera_info_missing")
    else:
        if depth_info.get("frame_id") != rgb_source.get("frame_id"):
            reasons.append("depth_camera_info_frame_mismatch")
        if (int(depth_info.get("height", 0)), int(depth_info.get("width", 0))) != (
            rgb_height,
            rgb_width,
        ):
            reasons.append("depth_camera_info_dimension_mismatch")
        if isinstance(color_info, dict) and any(
            color_info.get(key) != depth_info.get(key)
            for key in (
                "frame_id",
                "width",
                "height",
                "distortion_model",
                "d",
                "k",
                "r",
                "p",
            )
        ):
            reasons.append("aligned_depth_camera_info_mismatch")

    if depth_raw.shape != (rgb_height, rgb_width):
        reasons.append("depth_rgb_dimension_mismatch")

    supplied_scale = request.metadata.get("depth_scale_m_per_unit")
    scale: float | None = None
    if isinstance(supplied_scale, (int, float)) and not isinstance(
        supplied_scale, bool
    ):
        scale = float(supplied_scale)
    if request.metadata.get("depth_scale_validated") is not True:
        reasons.append("depth_scale_unvalidated")
    if scale is None or not np.isfinite(scale) or scale <= 0.0:
        reasons.append("depth_scale_missing_or_invalid")

    if reasons:
        return DepthContext(
            received=True,
            decoded=True,
            input_ready=False,
            reasons=tuple(reasons),
            raw_shape=(int(depth_raw.shape[0]), int(depth_raw.shape[1])),
            depth_scale_m_per_unit=scale,
            alignment_id=alignment_id,
        )

    assert scale is not None
    depth_m = depth_raw.astype(np.float32) * np.float32(scale)
    valid = (
        (depth_raw > 0)
        & np.isfinite(depth_m)
        & (depth_m >= config.depth_min_m)
        & (depth_m <= config.depth_max_m)
    )
    depth_m[~valid] = 0.0
    valid_pixels = int(np.count_nonzero(valid))
    if valid_pixels == 0:
        return DepthContext(
            received=True,
            decoded=True,
            input_ready=False,
            reasons=("depth_has_no_valid_samples",),
            raw_shape=(int(depth_raw.shape[0]), int(depth_raw.shape[1])),
            depth_m=depth_m,
            depth_scale_m_per_unit=scale,
            valid_pixels=0,
            valid_ratio=0.0,
            alignment_id=alignment_id,
        )
    return DepthContext(
        received=True,
        decoded=True,
        input_ready=True,
        reasons=(),
        raw_shape=(int(depth_raw.shape[0]), int(depth_raw.shape[1])),
        depth_m=depth_m,
        depth_scale_m_per_unit=scale,
        valid_pixels=valid_pixels,
        valid_ratio=float(valid_pixels / max(depth_raw.size, 1)),
        alignment_id=alignment_id,
    )
