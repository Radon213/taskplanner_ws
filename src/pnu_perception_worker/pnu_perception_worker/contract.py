"""Strict request validation and JSON-safe result helpers."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any

from . import REQUEST_SCHEMA

ALGORITHMS = ("tool", "blood", "hand")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
TOP_LEVEL_KEYS = {
    "schema",
    "request_id",
    "source",
    "requested_algorithms",
    "deadline_unix_ms",
    "color_camera_info",
    "depth_camera_info",
    "alignment",
    "extrinsics",
    "depth_scale_m_per_unit",
    "depth_scale_validated",
}
SOURCE_KEYS = {"rgb", "depth"}
FRAME_KEYS = {"stamp_ns", "frame_id", "format", "aligned"}
CAMERA_INFO_KEYS = {
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


class ContractError(ValueError):
    """A client request violates the v1 contract."""


@dataclass(frozen=True)
class InferenceRequest:
    request_id: str
    source: dict[str, Any]
    requested_algorithms: tuple[str, ...]
    deadline_unix_ms: int
    metadata: dict[str, Any]


def _require_exact_keys(
    value: dict[str, Any], allowed: set[str], *, label: str
) -> None:
    unknown = sorted(set(value).difference(allowed))
    if unknown:
        raise ContractError(f"{label} has unsupported fields: {unknown}")


def _validate_frame(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{label} must be an object")
    _require_exact_keys(value, FRAME_KEYS, label=label)
    stamp = value.get("stamp_ns")
    if not isinstance(stamp, int) or isinstance(stamp, bool) or stamp < 0:
        raise ContractError(f"{label}.stamp_ns must be a non-negative integer")
    frame_id = value.get("frame_id")
    if (
        not isinstance(frame_id, str)
        or not frame_id
        or len(frame_id.encode("utf-8")) > 256
        or "\x00" in frame_id
    ):
        raise ContractError(f"{label}.frame_id is invalid")
    image_format = value.get("format")
    if (
        not isinstance(image_format, str)
        or not image_format
        or len(image_format) > 128
        or "\x00" in image_format
    ):
        raise ContractError(f"{label}.format is invalid")
    if "aligned" in value and not isinstance(value["aligned"], bool):
        raise ContractError(f"{label}.aligned must be a boolean")
    return value


def _finite_number(value: Any, *, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ContractError(f"{label} must be numeric")
    result = float(value)
    if not (-1.0e12 < result < 1.0e12):
        raise ContractError(f"{label} must be finite and bounded")
    return result


def _validate_camera_info(value: Any, *, label: str) -> None:
    if not isinstance(value, dict):
        raise ContractError(f"{label} must be an object")
    _require_exact_keys(value, CAMERA_INFO_KEYS, label=label)
    for field in ("stamp_ns", "width", "height"):
        item = value.get(field)
        if not isinstance(item, int) or isinstance(item, bool) or item < 0:
            raise ContractError(f"{label}.{field} must be a non-negative integer")
    if value["width"] == 0 or value["height"] == 0:
        raise ContractError(f"{label}.width and height must be positive")
    _validate_frame(
        {
            "stamp_ns": value["stamp_ns"],
            "frame_id": value.get("frame_id"),
            "format": "camera_info",
        },
        label=label,
    )
    for field, expected in (("k", 9), ("r", 9), ("p", 12)):
        sequence = value.get(field)
        if not isinstance(sequence, list) or len(sequence) != expected:
            raise ContractError(f"{label}.{field} must contain {expected} numbers")
        for index, item in enumerate(sequence):
            _finite_number(item, label=f"{label}.{field}[{index}]")
    distortion = value.get("d")
    if not isinstance(distortion, list):
        raise ContractError(f"{label}.d must be a numeric list")
    for index, item in enumerate(distortion):
        _finite_number(item, label=f"{label}.d[{index}]")
    model = value.get("distortion_model")
    if not isinstance(model, str) or len(model) > 64 or "\x00" in model:
        raise ContractError(f"{label}.distortion_model is invalid")
    # CAM4 currently publishes the ROS plumb_bob model with five coefficients.
    # Restricting the LAN request boundary to that deployed/OpenCV-supported
    # shape prevents malformed D arrays from reaching undistortPoints.
    if model != "plumb_bob" or len(distortion) != 5:
        raise ContractError(
            f"{label} must use deployed plumb_bob distortion with 5 coefficients"
        )


def parse_metadata(
    raw: bytes,
    *,
    depth_present: bool,
    now_unix_ms: int | None = None,
    max_deadline_ahead_ms: int = 15_000,
    max_rgb_depth_skew_ns: int = 50_000_000,
) -> InferenceRequest:
    try:
        metadata = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError("metadata must be valid UTF-8 JSON") from exc
    if not isinstance(metadata, dict):
        raise ContractError("metadata must be a JSON object")
    _require_exact_keys(metadata, TOP_LEVEL_KEYS, label="metadata")
    if metadata.get("schema") != REQUEST_SCHEMA:
        raise ContractError(f"metadata.schema must be {REQUEST_SCHEMA!r}")

    request_id = metadata.get("request_id")
    if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id):
        raise ContractError("request_id has an invalid format")

    source = metadata.get("source")
    if not isinstance(source, dict):
        raise ContractError("source must be an object")
    _require_exact_keys(source, SOURCE_KEYS, label="source")
    _validate_frame(source.get("rgb"), label="source.rgb")
    has_depth_metadata = "depth" in source
    if has_depth_metadata:
        _validate_frame(source["depth"], label="source.depth")
    if has_depth_metadata != depth_present:
        raise ContractError(
            "source.depth and the depth binary part must appear together"
        )
    if has_depth_metadata:
        skew_ns = abs(source["rgb"]["stamp_ns"] - source["depth"]["stamp_ns"])
        if skew_ns > max_rgb_depth_skew_ns:
            raise ContractError(
                f"RGB/depth stamp skew exceeds {max_rgb_depth_skew_ns} ns"
            )

    algorithms = metadata.get("requested_algorithms", list(ALGORITHMS))
    if (
        not isinstance(algorithms, list)
        or not algorithms
        or len(algorithms) > len(ALGORITHMS)
        or any(
            not isinstance(item, str) or item not in ALGORITHMS for item in algorithms
        )
        or len(set(algorithms)) != len(algorithms)
    ):
        raise ContractError(
            f"requested_algorithms must be a unique non-empty subset of {ALGORITHMS}"
        )

    deadline = metadata.get("deadline_unix_ms")
    if not isinstance(deadline, int) or isinstance(deadline, bool):
        raise ContractError("deadline_unix_ms must be an integer")
    now = int(time.time() * 1000) if now_unix_ms is None else now_unix_ms
    if deadline <= now:
        raise ContractError("request deadline has expired")
    if deadline - now > max_deadline_ahead_ms:
        raise ContractError("request deadline is too far in the future")

    for key in ("color_camera_info", "depth_camera_info"):
        if key in metadata:
            _validate_camera_info(metadata[key], label=key)
    if "depth_camera_info" in metadata and not depth_present:
        raise ContractError("depth_camera_info requires a depth binary part")

    for key in ("alignment", "extrinsics"):
        if key in metadata:
            gate = metadata[key]
            if not isinstance(gate, dict) or set(gate) != {"validated", "id"}:
                raise ContractError(f"{key} must contain exactly validated and id")
            if not isinstance(gate["validated"], bool):
                raise ContractError(f"{key}.validated must be a boolean")
            if not isinstance(gate["id"], str) or len(gate["id"]) > 128:
                raise ContractError(f"{key}.id is invalid")
    if "depth_scale_validated" in metadata and not isinstance(
        metadata["depth_scale_validated"], bool
    ):
        raise ContractError("depth_scale_validated must be a boolean")
    if "depth_scale_m_per_unit" in metadata:
        scale = _finite_number(
            metadata["depth_scale_m_per_unit"], label="depth_scale_m_per_unit"
        )
        if not 0.0 < scale <= 1.0:
            raise ContractError("depth_scale_m_per_unit must be in (0, 1]")

    return InferenceRequest(
        request_id=request_id,
        source=source,
        requested_algorithms=tuple(algorithms),
        deadline_unix_ms=deadline,
        metadata=metadata,
    )


def coco_rle(mask: Any, *, max_counts: int | None = None) -> dict[str, Any]:
    """Encode a 2-D boolean array using uncompressed COCO column-major RLE."""
    import numpy as np

    binary = np.asarray(mask, dtype=bool)
    if binary.ndim != 2:
        raise ValueError("mask must be 2-D")
    flat = binary.reshape(-1, order="F")
    if flat.size == 0:
        counts_array = np.asarray([0], dtype=np.int64)
    else:
        transitions = np.flatnonzero(flat[1:] != flat[:-1]) + 1
        boundaries = np.concatenate(
            (
                np.asarray([0], dtype=np.int64),
                transitions.astype(np.int64, copy=False),
                np.asarray([flat.size], dtype=np.int64),
            )
        )
        counts_array = np.diff(boundaries)
        if flat[0] != 0:
            counts_array = np.concatenate(
                (np.asarray([0], dtype=np.int64), counts_array)
            )
    if max_counts is not None and counts_array.size > max_counts:
        raise ValueError("mask RLE exceeds the configured count limit")
    return {
        "size": [int(binary.shape[0]), int(binary.shape[1])],
        "counts": counts_array.tolist(),
    }
