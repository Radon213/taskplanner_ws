"""Fail-closed ROS adapter for the versioned PNU perception worker.

The worker may run beside Taskplanner or on another LAN host.  This adapter is
the only ROS-facing component: it coalesces live VIPLab CAM4 frames, sends the
original compressed bytes over the same versioned HTTP API in either layout,
and publishes only responses that match the request, source timestamps and
model digests.  It has no planner or robot-motion authority.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
import socket
import struct
import threading
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import rclpy
import requests
from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError
from rclpy.node import Node
from rclpy.qos import (
    DurabilityPolicy,
    HistoryPolicy,
    QoSProfile,
    ReliabilityPolicy,
)
from sensor_msgs.msg import CameraInfo, CompressedImage
from std_msgs.msg import String

from hand_keypoint_interfaces.msg import Hand, HandKeypoints, PalmPose6D, Point2D
from surgical_msgs.msg import ToolObservation
from surgical_perception_msgs.msg import (
    ToolObservation2D,
    ToolObservation2DArray,
    ToolPose,
    ToolPoseArray,
)

from .rfdetr_contract import Cam4MayoPlacementTracker

REQUEST_SCHEMA = "taskplanner.pnu_perception.request.v1"
RESPONSE_SCHEMA = "taskplanner.pnu_perception.response.v1"
CAPABILITIES_SCHEMA = "taskplanner.pnu_perception.capabilities.v1"
HEALTH_SCHEMA = "taskplanner.rfdetr_health.v1"
DIAGNOSTICS_SCHEMA = "pnu.rfdetr_diagnostics.v2"
SEMANTICS_SCHEMA = "taskplanner.cam4_semantics.v1"
BLOOD_SEMANTICS_SCHEMA = "taskplanner.cam4_blood_semantics.v1"
WORKER_HEALTH_SCHEMA = "taskplanner.pnu_perception.health.v1"
EXPECTED_UPSTREAM_REPOSITORY = "hanwae-py/hand-blood-tools"
EXPECTED_UPSTREAM_COMMIT = "0f9e93115b8cc1d470398c92e010e3fc6ef1de5d"

SUPPORTED_ALGORITHMS = ("tool", "blood", "hand")
# PNU publishes its own frozen ontology on the typed ToolPose/Observation
# topics.  The legacy CAM4 semantics and Mayo paths predate that ontology and
# are consumed through the active procedure catalog, so translate only the
# exact reviewed (canonical id, canonical name) pairs at that compatibility
# boundary.  Never turn an unknown provider label into a Taskplanner tool id.
_PNU_TOOL_COMPATIBILITY_NAMES: dict[int, tuple[str, str]] = {
    1: ("Scalpel", "#15 Scalpel"),
    2: ("Allis Forceps", "Allis clamp forceps"),
    3: ("Mosquito", "Mosquito forceps"),
    4: ("Adson Forceps", "Adson forceps"),
    5: ("Bipolar Forceps", "Bipolar cautery"),
    6: ("Bovie", "Bovie surgical cautery"),
    7: ("Army-Navy Retractor", "Army navy retractor"),
    8: ("Thyroid Retractor", "Thyroid retractor"),
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_RESPONSE_JSON_BYTES = 16 * 1024 * 1024
_MAX_DETECTIONS = 256
_MAX_HANDS = 8
_MAX_RLE_COUNTS = 1_000_000
_MAX_ADVERTISED_RLE_COUNTS_PER_ALGORITHM = 1_000_000
_MAX_BLOOD_UNION_SEGMENTS = 1_000_000
_BLOOD_CENTROID_TOLERANCE_PX = 1e-6
_MAX_STATUS_FLAGS = 64
_DEFAULT_OVERLAY_MAX_PIXELS = 4_194_304
_DEFAULT_OVERLAY_MAX_RATE_HZ = 5.0
_DEFAULT_POSE_OVERLAY_MAX_RATE_HZ = 15.0
_DEFAULT_POSE_AXIS_LENGTH_M = 0.05
_MIN_POSE_AXIS_LENGTH_M = 0.005
_MAX_POSE_AXIS_LENGTH_M = 0.25
_MAX_OVERLAY_INSTANCES_PER_ALGORITHM = 64
_MAX_POSE_OVERLAY_INSTANCES = 64
_MAX_OVERLAY_RLE_RUNS = 200_000
_MAX_OVERLAY_MASK_SEGMENTS = 50_000
_MAX_OVERLAY_WEBP_BYTES = 8 * 1024 * 1024
_POSE_SCHEMA = "pnu.surgical_tool_pose_array.v1.3"
_OBSERVATION_SCHEMA = "pnu.surgical_tool_observation_2d_array.v1.3"
_TOOL_ONTOLOGY_VERSION = "pnu.cam4.tool_ontology.v1"
_TOOL_POSE_CONVENTION_VERSION = "pnu.cam4.planar_tool_pose_convention.v2"
_SUPPORT_PLANE_DIAGNOSTICS_SCHEMA = "pnu.tool.support_plane_diagnostics.v1"
_REVIEWED_SUPPORT_PLANE_CONFIG_VERSION = (
    "viplab_cam4_146222251000_support_plane_v1_sha256_b683ecd5a5382a4f"
)
_REVIEWED_SUPPORT_PLANE_RUNTIME_MIN_SAMPLE_COUNT = 5_000
_REVIEWED_SUPPORT_PLANE_RUNTIME_MIN_INLIER_RATIO = 0.85
_REVIEWED_SUPPORT_PLANE_RUNTIME_MAX_RESIDUAL_MEDIAN_M = 0.006
_REVIEWED_SUPPORT_PLANE_RUNTIME_MAX_RESIDUAL_P95_M = 0.02
_TOOL_METRIC_3D_UNAVAILABLE_CALIBRATION = "metric_3d_unavailable"
_TOOL_OBSERVATION_POINT_DEFINITION = (
    "mask_internal_depth_valid_observed_surface_point_v1"
)
_TOOL_AXIS_DEFINITION = (
    "+Y handle/proximal to working tip; +Z support plane to free space; "
    "+X=+Yx+Z"
)
_TOOL_CANONICAL_CLASSES = {
    1: "Scalpel",
    2: "Allis Forceps",
    3: "Mosquito",
    4: "Adson Forceps",
    5: "Bipolar Forceps",
    6: "Bovie",
    7: "Army-Navy Retractor",
    8: "Thyroid Retractor",
}
_TOOL_POSITION_DEPTH_ABS_TOLERANCE_M = 2e-6
_TOOL_POSITION_REPROJECTION_TOLERANCE_PX = 1.0
_HAND_JOINT_REPROJECTION_TOLERANCE_PX = 1.0
_HAND_PALM_TRANSLATION_TOLERANCE_M = 2e-6
_HAND_PALM_ROTATION_TOLERANCE = 2e-5
_DEFAULT_SUPPORT_PLANE_NORMAL_TOLERANCE_DEG = 1.0
_MAX_SUPPORT_PLANE_NORMAL_TOLERANCE_DEG = 10.0
# These are part of the pinned upstream planar_pose.PlanarPoseConfig. A remote
# worker may degrade below them, but must never claim a valid position/axis.
_TOOL_MINIMUM_VALID_DEPTH_RATIO = 0.05
_TOOL_MINIMUM_AXIS_ANISOTROPY = 2.0
_TOOL_MINIMUM_ENDPOINT_SIGN_CONFIDENCE = 0.20
# DDS may deliver the four exact-stamp CAM4 inputs to this node in a different
# callback order.  Give only that already-arriving frame set a short grace
# period; this is not an inference queue and never permits a stale frame to be
# sent to the worker.
_EXACT_RGBD_ORDERING_GRACE_SEC = 0.02


class ContractError(RuntimeError):
    """A worker or input violated the fail-closed perception contract."""


@dataclass(frozen=True, slots=True)
class BinaryFrame:
    received_monotonic: float
    stamp_ns: int
    frame_id: str
    format: str
    data: bytes

    @property
    def source_stamp_sec(self) -> float:
        return float(self.stamp_ns) / 1_000_000_000.0


@dataclass(frozen=True, slots=True)
class CameraCalibration:
    received_monotonic: float
    stamp_ns: int
    frame_id: str
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AlignmentValidation:
    aligned: bool
    reasons: tuple[str, ...]
    width: int = 0
    height: int = 0


@dataclass(frozen=True, slots=True)
class ValidatedWorkerResponse:
    payload: dict[str, Any]
    model_digests: dict[str, str]
    tool_detections: tuple[dict[str, Any], ...]
    blood_detections: tuple[dict[str, Any], ...]
    hands: tuple[dict[str, Any], ...]
    metric_3d_ready: bool
    tool_support_plane_diagnostics: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class RenderedDebugOverlay:
    """One bounded transparent overlay associated with an accepted RGB frame."""

    message: CompressedImage
    render_encode_latency_ms: float
    drawn_tool_count: int
    drawn_blood_count: int
    drawn_hand_count: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class RenderedPoseOverlay:
    """One bounded transparent pose layer derived from a ToolPoseArray."""

    message: CompressedImage
    render_encode_latency_ms: float
    drawn_axis_count: int
    drawn_position_only_count: int
    truncated: bool


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _camera_info_sha256(value: Any) -> str:
    """Hash the exact CameraInfo core used by the pinned PNU worker.

    Keep this byte-for-byte compatible with
    ``pnu_perception_worker.support_plane.camera_info_sha256``.  Source stamps
    and any transport-only metadata are deliberately excluded from the pin.
    """

    if not isinstance(value, Mapping):
        raise ContractError("color CameraInfo must be an object")
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
        raise ContractError("color CameraInfo is malformed") from exc
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
        raise ContractError("color CameraInfo geometry is invalid")
    try:
        canonical = json.dumps(
            core,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ContractError("color CameraInfo is not canonically serializable") from exc
    return hashlib.sha256(canonical).hexdigest()


def parse_support_plane_normal(
    value: Any,
    *,
    allow_empty: bool = False,
) -> tuple[float, float, float] | None:
    if isinstance(value, str):
        raw = value.strip()
        if not raw and allow_empty:
            return None
        parts: Sequence[Any] = raw.split(",")
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        parts = value
    else:
        raise ValueError("expected Tool support-plane normal must be a three-vector")
    if len(parts) != 3:
        raise ValueError("expected Tool support-plane normal must be a three-vector")
    try:
        vector = tuple(float(item) for item in parts)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "expected Tool support-plane normal must contain finite numbers"
        ) from exc
    if not all(math.isfinite(item) for item in vector):
        raise ValueError(
            "expected Tool support-plane normal must contain finite numbers"
        )
    norm = math.sqrt(sum(item * item for item in vector))
    if not 0.999 <= norm <= 1.001:
        raise ValueError("expected Tool support-plane normal must be unit length")
    return tuple(item / norm for item in vector)


def _dot3(left: Sequence[float], right: Sequence[float]) -> float:
    return sum(float(a) * float(b) for a, b in zip(left, right, strict=True))


def _cross3(
    left: Sequence[float], right: Sequence[float]
) -> tuple[float, float, float]:
    return (
        float(left[1]) * float(right[2]) - float(left[2]) * float(right[1]),
        float(left[2]) * float(right[0]) - float(left[0]) * float(right[2]),
        float(left[0]) * float(right[1]) - float(left[1]) * float(right[0]),
    )


def _palm_frame_v2_from_joints(
    j0: Sequence[float],
    j2: Sequence[float],
    j9: Sequence[float],
    j17: Sequence[float],
) -> tuple[tuple[float, float, float], tuple[float, ...]]:
    """Recompute the pinned upstream palm_frame_v2 without NumPy."""

    origin = tuple(0.5 * (float(j0[i]) + float(j9[i])) for i in range(3))
    x_axis_raw = tuple(float(j9[i]) - float(j0[i]) for i in range(3))
    x_norm = math.sqrt(_dot3(x_axis_raw, x_axis_raw))
    x_axis = tuple(item / (x_norm + 1.0e-9) for item in x_axis_raw)
    y_axis_raw = tuple(
        0.5 * (float(j0[i]) + float(j17[i])) - float(j2[i])
        for i in range(3)
    )
    projection = _dot3(y_axis_raw, x_axis)
    y_axis_orthogonal = tuple(
        y_axis_raw[i] - projection * x_axis[i] for i in range(3)
    )
    y_norm = math.sqrt(_dot3(y_axis_orthogonal, y_axis_orthogonal))
    y_axis = tuple(item / (y_norm + 1.0e-9) for item in y_axis_orthogonal)
    z_axis_raw = _cross3(x_axis, y_axis)
    z_norm = math.sqrt(_dot3(z_axis_raw, z_axis_raw))
    z_axis = tuple(item / (z_norm + 1.0e-9) for item in z_axis_raw)
    rotation_row_major = (
        x_axis[0],
        y_axis[0],
        z_axis[0],
        x_axis[1],
        y_axis[1],
        z_axis[1],
        x_axis[2],
        y_axis[2],
        z_axis[2],
    )
    return origin, rotation_row_major


def _rotation_determinant(rotation: Sequence[float]) -> float:
    return (
        float(rotation[0])
        * (float(rotation[4]) * float(rotation[8]) - float(rotation[5]) * float(rotation[7]))
        - float(rotation[1])
        * (float(rotation[3]) * float(rotation[8]) - float(rotation[5]) * float(rotation[6]))
        + float(rotation[2])
        * (float(rotation[3]) * float(rotation[7]) - float(rotation[4]) * float(rotation[6]))
    )


def _bounded_string(value: Any, *, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{field} must be a string")
    result = value.strip()
    if not result or len(result) > maximum:
        raise ContractError(f"{field} is empty or exceeds {maximum} characters")
    return result


def _exact_keys(
    value: Mapping[str, Any],
    expected: set[str],
    *,
    field: str,
) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ContractError(
            f"{field} keys do not match schema (missing={missing}, extra={extra})"
        )


def _remaining_deadline_sec(deadline_monotonic: float, *, field: str) -> float:
    remaining = float(deadline_monotonic) - time.monotonic()
    if not math.isfinite(remaining) or remaining <= 0.0:
        raise ContractError(f"{field} exceeded the monotonic request deadline")
    return remaining


def _read_bounded_json_response(
    response: Any,
    *,
    field: str,
    deadline_monotonic: float,
    maximum_bytes: int = _MAX_RESPONSE_JSON_BYTES,
) -> Any:
    """Read one streamed JSON response under a byte and elapsed-time bound."""

    try:
        response.raise_for_status()
        raw_length = response.headers.get("Content-Length")
        content_length: int | None = None
        if raw_length not in {None, ""}:
            try:
                content_length = int(raw_length)
            except (TypeError, ValueError) as exc:
                raise ContractError(f"{field} Content-Length is invalid") from exc
            if content_length < 0 or content_length > maximum_bytes:
                raise ContractError(f"{field} response is oversized")
        content_encoding = str(response.headers.get("Content-Encoding", "")).strip()
        if content_encoding.casefold() not in {"", "identity"}:
            # The client explicitly requests identity encoding. Reading the
            # http.client stream directly below gives us one bounded socket
            # read per loop, so an endpoint that ignores that request must
            # fail closed instead of hiding a decompressor behind the byte cap.
            raise ContractError(f"{field} response Content-Encoding is unsupported")
        body = bytearray()
        _remaining_deadline_sec(deadline_monotonic, field=field)

        # requests' scalar timeout is an inactivity timeout, not an absolute
        # deadline. iter_content also waits for its full chunk while a peer can
        # drip one byte forever. For a real urllib3 response, read at most once
        # from the socket per iteration and reset that socket to the remaining
        # absolute budget. Fake unit-test responses intentionally use the
        # bounded iterator fallback below.
        raw = getattr(response, "raw", None)
        if raw is not None:
            fp = getattr(raw, "_fp", None)
            # urllib3 2.x exposes the one-underlying-read primitive on the
            # wrapped http.client.HTTPResponse rather than urllib3's wrapper.
            # http.client.read1 also preserves Content-Length/chunk framing.
            read1 = getattr(fp, "read1", None)
            buffered = getattr(fp, "fp", None)
            socket_io = getattr(buffered, "raw", None)
            response_socket = getattr(socket_io, "_sock", None)
            if not isinstance(response_socket, socket.socket):
                response_socket = getattr(
                    getattr(raw, "_connection", None), "sock", None
                )
            if not callable(read1) or not isinstance(response_socket, socket.socket):
                raise ContractError(
                    f"{field} response cannot enforce the absolute read deadline"
                )

            def response_chunks() -> Iterable[bytes]:
                while True:
                    remaining = _remaining_deadline_sec(
                        deadline_monotonic,
                        field=field,
                    )
                    # A complete Content-Length body may cause urllib3 to
                    # release/close the socket immediately after read1.  Any
                    # bytes already held by http.client remain readable; only
                    # set a new timeout while the descriptor is still live.
                    if response_socket.fileno() >= 0:
                        response_socket.settimeout(max(0.001, remaining))
                    chunk = read1(64 * 1024)
                    if not chunk:
                        return
                    yield chunk

            chunks = response_chunks()
        else:
            chunks = response.iter_content(chunk_size=64 * 1024)

        for chunk in chunks:
            _remaining_deadline_sec(deadline_monotonic, field=field)
            if not chunk:
                continue
            body.extend(chunk)
            if len(body) > maximum_bytes:
                raise ContractError(f"{field} response is oversized")
            if content_length is not None and len(body) >= content_length:
                if len(body) != content_length:
                    raise ContractError(f"{field} Content-Length does not match body")
                break
        if content_length is not None and len(body) != content_length:
            raise ContractError(f"{field} Content-Length does not match body")
        _remaining_deadline_sec(deadline_monotonic, field=field)
        try:
            return json.loads(bytes(body))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ContractError(f"{field} response is not valid JSON") from exc
    except ContractError:
        raise
    except Exception as exc:
        if time.monotonic() >= deadline_monotonic:
            raise ContractError(
                f"{field} exceeded the monotonic request deadline"
            ) from exc
        raise ContractError(f"{field} response read failed") from exc
    finally:
        response.close()


def validate_service_url(value: str) -> str:
    """Validate one explicit HTTP(S) endpoint without adding a fallback."""

    candidate = str(value or "").strip().rstrip("/")
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("service_url must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("service_url must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("service_url must not contain a query or fragment")
    if parsed.path not in {"", "/"}:
        raise ValueError("service_url must name an origin, not an API path")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("service_url has an invalid port") from exc
    return candidate


def endpoint_is_loopback(value: str) -> bool:
    """Return true only for an explicit loopback literal or reviewed local name.

    Arbitrary DNS names are intentionally never granted the local transport or
    authentication exception: their address can change after this startup
    check (DNS rebinding / split-horizon changes).
    """

    hostname = (urlsplit(validate_service_url(value)).hostname or "").casefold()
    hostname = hostname.rstrip(".")
    if hostname in {"localhost", "localhost.localdomain"}:
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def resolve_endpoint_transport_mode(
    service_url: str,
    *,
    allow_insecure_remote_http: bool,
) -> str:
    """Return the reviewed transport mode or reject insecure remote HTTP."""

    endpoint = validate_service_url(service_url)
    if urlsplit(endpoint).scheme == "https":
        return "https"
    if endpoint_is_loopback(endpoint):
        return "http_local"
    if allow_insecure_remote_http:
        return "http_trusted_lan_dev"
    raise ValueError(
        "remote PNU endpoint must use https; isolated trusted-LAN development "
        "over HTTP requires allow_insecure_remote_http=true"
    )


def resolve_endpoint_auth_mode(
    service_url: str,
    *,
    has_token: bool,
    allow_unauthenticated_remote: bool,
) -> tuple[str, str]:
    """Return worker auth mode and public health label without secret data."""

    if has_token:
        return "bearer", "bearer"
    if endpoint_is_loopback(service_url):
        return "none", "none_local"
    if allow_unauthenticated_remote:
        return "none", "none_trusted_lan_dev"
    raise ValueError(
        "remote PNU endpoint requires api_token_file; trusted-LAN dev use "
        "requires allow_unauthenticated_remote=true"
    )


def normalize_algorithms(values: Iterable[Any]) -> tuple[str, ...]:
    result: list[str] = []
    for raw in values:
        value = str(raw).strip().casefold()
        if value not in SUPPORTED_ALGORITHMS:
            raise ValueError(
                "requested_algorithms entries must be tool, blood, or hand"
            )
        if value not in result:
            result.append(value)
    if not result:
        raise ValueError("requested_algorithms must not be empty")
    return tuple(result)


def parse_expected_model_digests(
    raw_json: str,
    *,
    requested_algorithms: Sequence[str] | None = None,
) -> dict[str, str]:
    try:
        value = json.loads(str(raw_json or "{}"))
    except json.JSONDecodeError as exc:
        raise ValueError("expected_model_digests_json is invalid JSON") from exc
    if not isinstance(value, dict):
        # This is a malformed parameter value, rather than a Python API type
        # error: the ROS parameter itself is always provided as a string.
        raise ValueError(  # noqa: TRY004
            "expected_model_digests_json must be an object"
        )
    result: dict[str, str] = {}
    for raw_algorithm, raw_digest in value.items():
        algorithm = str(raw_algorithm).strip().casefold()
        digest = str(raw_digest).strip()
        if (
            algorithm not in SUPPORTED_ALGORITHMS
            or digest != digest.casefold()
            or not _SHA256_RE.fullmatch(digest)
            or algorithm in result
        ):
            raise ValueError(
                "expected model digests must map tool/blood/hand to lowercase SHA256"
            )
        result[algorithm] = digest
    if requested_algorithms is not None:
        requested = normalize_algorithms(requested_algorithms)
        missing = [algorithm for algorithm in requested if algorithm not in result]
        if missing:
            raise ValueError(
                "expected_model_digests_json must pin every requested algorithm "
                f"(missing={missing})"
            )
    return result


def _message_stamp_ns(message: Any) -> int:
    stamp = message.header.stamp
    return int(stamp.sec) * 1_000_000_000 + int(stamp.nanosec)


def buffered_image(message: CompressedImage) -> BinaryFrame:
    stamp_ns = _message_stamp_ns(message)
    if stamp_ns <= 0:
        raise ContractError("image source stamp must be positive")
    frame_id = _bounded_string(
        str(message.header.frame_id), field="image.header.frame_id", maximum=240
    )
    image_format = _bounded_string(
        str(message.format), field="image.format", maximum=120
    )
    data = bytes(message.data)
    if not data:
        raise ContractError("image payload is empty")
    return BinaryFrame(
        received_monotonic=time.monotonic(),
        stamp_ns=stamp_ns,
        frame_id=frame_id,
        format=image_format,
        data=data,
    )


def _finite_list(values: Sequence[Any], *, length: int, field: str) -> list[float]:
    if len(values) != length or any(not _is_number(item) for item in values):
        raise ContractError(f"{field} must contain {length} finite numbers")
    return [float(item) for item in values]


def buffered_camera_info(message: CameraInfo) -> CameraCalibration:
    stamp_ns = _message_stamp_ns(message)
    if stamp_ns <= 0:
        raise ContractError("CameraInfo source stamp must be positive")
    frame_id = _bounded_string(
        str(message.header.frame_id),
        field="camera_info.header.frame_id",
        maximum=240,
    )
    width = int(message.width)
    height = int(message.height)
    if width <= 0 or height <= 0:
        raise ContractError("CameraInfo width and height must be positive")
    distortion_model = _bounded_string(
        str(message.distortion_model),
        field="camera_info.distortion_model",
        maximum=80,
    )
    distortion = list(message.d)
    if len(distortion) > 32 or any(not _is_number(item) for item in distortion):
        raise ContractError("CameraInfo.d must contain at most 32 finite numbers")
    # The worker contract intentionally accepts only calibration fields used by
    # the current algorithms.  Reject binning/ROI instead of silently applying
    # full-resolution intrinsics to a cropped or binned image.
    roi = message.roi
    if (
        int(message.binning_x) != 0
        or int(message.binning_y) != 0
        or int(roi.x_offset) != 0
        or int(roi.y_offset) != 0
        or int(roi.height) != 0
        or int(roi.width) != 0
        or bool(roi.do_rectify)
    ):
        raise ContractError("CameraInfo binning/ROI is unsupported")
    payload = {
        "stamp_ns": stamp_ns,
        "frame_id": frame_id,
        "width": width,
        "height": height,
        "distortion_model": distortion_model,
        "d": [float(item) for item in distortion],
        "k": _finite_list(message.k, length=9, field="CameraInfo.k"),
        "r": _finite_list(message.r, length=9, field="CameraInfo.r"),
        "p": _finite_list(message.p, length=12, field="CameraInfo.p"),
    }
    return CameraCalibration(
        received_monotonic=time.monotonic(),
        stamp_ns=stamp_ns,
        frame_id=frame_id,
        payload=payload,
    )


def _rgb_dimensions(frame: BinaryFrame) -> tuple[int, int]:
    """Read and verify the compressed RGB container without re-encoding it."""

    try:
        with Image.open(BytesIO(frame.data)) as image:
            width, height = image.size
            detected_format = str(image.format or "").casefold()
            expected = _rgb_content_type(frame.format)
            if (expected == "image/jpeg" and detected_format != "jpeg") or (
                expected == "image/png" and detected_format != "png"
            ):
                raise ContractError("RGB payload container does not match image.format")
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ContractError("RGB payload is not a valid JPEG/PNG image") from exc
    if width <= 0 or height <= 0 or width * height > 16_000_000:
        raise ContractError("RGB payload dimensions are invalid or oversized")
    return int(width), int(height)


def _compressed_depth_dimensions(frame: BinaryFrame) -> tuple[int, int]:
    """Validate the ROS compressedDepth envelope and its 16UC1 PNG IHDR."""

    if "16uc1" not in frame.format.casefold() or "compresseddepth" not in (
        frame.format.casefold()
    ):
        raise ContractError("aligned depth must be 16UC1 compressedDepth")
    signature = b"\x89PNG\r\n\x1a\n"
    offset = frame.data.find(signature)
    # compressed_depth_image_transport prepends a small transport header.  A
    # signature deeper in arbitrary bytes is not accepted as that envelope.
    if offset < 0 or offset > 64 or len(frame.data) < offset + 33:
        raise ContractError("compressedDepth payload has no bounded PNG envelope")
    if frame.data[offset + 12 : offset + 16] != b"IHDR":
        raise ContractError("compressedDepth PNG does not start with IHDR")
    ihdr_length = struct.unpack(">I", frame.data[offset + 8 : offset + 12])[0]
    if ihdr_length != 13:
        raise ContractError("compressedDepth PNG has an invalid IHDR")
    width, height = struct.unpack(">II", frame.data[offset + 16 : offset + 24])
    bit_depth = frame.data[offset + 24]
    color_type = frame.data[offset + 25]
    if (
        width <= 0
        or height <= 0
        or width * height > 16_000_000
        or bit_depth != 16
        or color_type != 0
    ):
        raise ContractError("compressedDepth PNG must be bounded 16-bit grayscale")
    return int(width), int(height)


def _camera_infos_exactly_match(
    color: CameraCalibration,
    aligned_depth: CameraCalibration,
) -> bool:
    """Compare the color-coordinate calibration while ignoring source stamps."""

    if color.frame_id != aligned_depth.frame_id:
        return False
    keys = {"frame_id", "width", "height", "distortion_model", "d", "k", "r", "p"}
    return all(color.payload.get(key) == aligned_depth.payload.get(key) for key in keys)


def validate_aligned_depth_contract(
    *,
    rgb: BinaryFrame,
    depth: BinaryFrame | None,
    color_camera_info: CameraCalibration | None,
    depth_camera_info: CameraCalibration | None,
    configured_alignment_validated: bool,
    alignment_id: str,
    depth_scale_m_per_unit: float,
    depth_scale_validated: bool,
) -> AlignmentValidation:
    """Return an aligned gate only after every observable contract agrees.

    The operator/configuration approval is necessary but not sufficient.  The
    two actual image payloads, their optical frames, and both CameraInfo
    messages must prove one common RGB pixel grid on every request.
    """

    reasons: list[str] = []
    if depth is None:
        reasons.append("depth_missing")
    if not configured_alignment_validated:
        reasons.append("alignment_not_operator_validated")
    if not str(alignment_id).strip():
        reasons.append("alignment_id_missing")
    if not depth_scale_validated:
        reasons.append("depth_scale_unvalidated")
    if (
        not math.isfinite(float(depth_scale_m_per_unit))
        or float(depth_scale_m_per_unit) <= 0.0
        or float(depth_scale_m_per_unit) > 1.0
    ):
        reasons.append("depth_scale_invalid")
    if color_camera_info is None:
        reasons.append("color_camera_info_missing")
    if depth_camera_info is None:
        reasons.append("aligned_depth_camera_info_missing")

    width = 0
    height = 0
    if depth is not None:
        if depth.frame_id != rgb.frame_id:
            reasons.append("rgb_depth_frame_id_mismatch")
        try:
            rgb_size = _rgb_dimensions(rgb)
        except ContractError:
            rgb_size = None
            reasons.append("rgb_payload_shape_unvalidated")
        try:
            depth_size = _compressed_depth_dimensions(depth)
        except ContractError:
            depth_size = None
            reasons.append("depth_payload_shape_unvalidated")
        if rgb_size is not None:
            width, height = rgb_size
        if rgb_size is not None and depth_size is not None and rgb_size != depth_size:
            reasons.append("rgb_depth_payload_shape_mismatch")
        if color_camera_info is not None and rgb_size is not None:
            color_size = (
                int(color_camera_info.payload["width"]),
                int(color_camera_info.payload["height"]),
            )
            if color_size != rgb_size:
                reasons.append("rgb_camera_info_shape_mismatch")
        if depth_camera_info is not None and depth_size is not None:
            depth_info_size = (
                int(depth_camera_info.payload["width"]),
                int(depth_camera_info.payload["height"]),
            )
            if depth_info_size != depth_size:
                reasons.append("depth_camera_info_shape_mismatch")

    if color_camera_info is not None and color_camera_info.frame_id != rgb.frame_id:
        reasons.append("rgb_camera_info_frame_id_mismatch")
    if depth_camera_info is not None and depth_camera_info.frame_id != rgb.frame_id:
        reasons.append("depth_camera_info_frame_id_mismatch")
    if (
        color_camera_info is not None
        and depth_camera_info is not None
        and not _camera_infos_exactly_match(color_camera_info, depth_camera_info)
    ):
        reasons.append("aligned_depth_camera_info_not_color_calibration")

    unique_reasons = tuple(dict.fromkeys(reasons))
    return AlignmentValidation(
        aligned=not unique_reasons,
        reasons=unique_reasons,
        width=width,
        height=height,
    )


def closest_by_stamp(
    values: Sequence[BinaryFrame] | Sequence[CameraCalibration],
    reference_stamp_ns: int,
    max_skew_sec: float,
) -> BinaryFrame | CameraCalibration | None:
    if not values:
        return None
    closest = min(values, key=lambda item: abs(item.stamp_ns - reference_stamp_ns))
    if abs(closest.stamp_ns - reference_stamp_ns) > int(max_skew_sec * 1e9):
        return None
    return closest


def has_exact_rgbd_frame_set(
    *,
    rgb_stamp_ns: int,
    depth_frames: Sequence[BinaryFrame],
    color_infos: Sequence[CameraCalibration],
    depth_infos: Sequence[CameraCalibration],
) -> bool:
    """Return whether all aligned CAM4 inputs for an RGB stamp have arrived."""

    return all(
        any(item.stamp_ns == rgb_stamp_ns for item in values)
        for values in (depth_frames, color_infos, depth_infos)
    )


def _rgb_content_type(image_format: str) -> str:
    normalized = image_format.casefold()
    if "jpeg" in normalized or "jpg" in normalized:
        return "image/jpeg"
    if "png" in normalized:
        return "image/png"
    raise ContractError(f"unsupported RGB compressed format: {image_format}")


def build_request_metadata(
    *,
    request_id: str,
    rgb: BinaryFrame,
    depth: BinaryFrame | None,
    color_camera_info: CameraCalibration | None,
    depth_camera_info: CameraCalibration | None,
    requested_algorithms: Sequence[str],
    deadline_unix_ms: int,
    depth_scale_m_per_unit: float = 0.0,
    depth_scale_validated: bool = False,
    depth_alignment_validated: bool = False,
    depth_alignment_id: str = "",
) -> dict[str, Any]:
    algorithms = normalize_algorithms(requested_algorithms)
    if not isinstance(deadline_unix_ms, int) or deadline_unix_ms <= 0:
        raise ValueError("deadline_unix_ms must be a positive integer")
    source: dict[str, Any] = {
        "rgb": {
            "stamp_ns": int(rgb.stamp_ns),
            "frame_id": rgb.frame_id,
            "format": rgb.format,
        }
    }
    alignment = validate_aligned_depth_contract(
        rgb=rgb,
        depth=depth,
        color_camera_info=color_camera_info,
        depth_camera_info=depth_camera_info,
        configured_alignment_validated=bool(depth_alignment_validated),
        alignment_id=str(depth_alignment_id),
        depth_scale_m_per_unit=float(depth_scale_m_per_unit),
        depth_scale_validated=bool(depth_scale_validated),
    )
    if depth is not None:
        source["depth"] = {
            "stamp_ns": int(depth.stamp_ns),
            "frame_id": depth.frame_id,
            "format": depth.format,
            "aligned": alignment.aligned,
        }
    metadata: dict[str, Any] = {
        "schema": REQUEST_SCHEMA,
        "request_id": _bounded_string(request_id, field="request_id", maximum=80),
        "source": source,
        "deadline_unix_ms": deadline_unix_ms,
        "requested_algorithms": list(algorithms),
    }
    if color_camera_info is not None:
        metadata["color_camera_info"] = color_camera_info.payload
    if depth_camera_info is not None and depth is not None:
        metadata["depth_camera_info"] = depth_camera_info.payload
    if depth is not None:
        scale = float(depth_scale_m_per_unit)
        if not math.isfinite(scale) or scale < 0.0 or scale > 1.0:
            raise ValueError("depth_scale_m_per_unit must be finite and in [0, 1]")
        if depth_scale_validated and scale <= 0.0:
            raise ValueError(
                "a validated depth scale requires depth_scale_m_per_unit > 0"
            )
        metadata["depth_scale_validated"] = bool(depth_scale_validated)
        if scale > 0.0:
            metadata["depth_scale_m_per_unit"] = scale
        metadata["alignment"] = {
            "validated": alignment.aligned,
            "id": str(depth_alignment_id).strip()[:128],
        }
    return metadata


def _validate_model_records(
    raw: Any,
    algorithms: Sequence[str],
    *,
    field: str,
    executed: bool,
) -> dict[str, str]:
    if not isinstance(raw, dict) or set(raw) != set(SUPPORTED_ALGORITHMS):
        raise ContractError(f"{field} must contain exactly tool, blood, and hand")
    requested = set(algorithms)
    result: dict[str, str] = {}
    for algorithm in SUPPORTED_ALGORITHMS:
        record = raw.get(algorithm)
        if not isinstance(record, dict):
            raise ContractError(f"{field}.{algorithm} must be an object")
        _exact_keys(
            record,
            {
                "ready",
                "executed",
                "status",
                "version",
                "digest_sha256",
                "backend",
                "error",
            },
            field=f"{field}.{algorithm}",
        )
        ready = record["ready"]
        did_execute = record["executed"]
        if not isinstance(ready, bool) or not isinstance(did_execute, bool):
            raise ContractError(f"{field}.{algorithm} readiness flags must be boolean")
        if ready:
            expected_status_for_record = "executed" if did_execute else "loaded"
            if record["status"] != expected_status_for_record:
                raise ContractError(
                    f"{field}.{algorithm}.status is inconsistent with execution"
                )
            _bounded_string(
                record["version"],
                field=f"{field}.{algorithm}.version",
                maximum=160,
            )
            _bounded_string(
                record["backend"],
                field=f"{field}.{algorithm}.backend",
                maximum=80,
            )
            digest = str(record["digest_sha256"]).strip().casefold()
            if not _SHA256_RE.fullmatch(digest):
                raise ContractError(f"{field}.{algorithm}.digest_sha256 is invalid")
            if record["error"] not in {None, ""}:
                raise ContractError(f"{field}.{algorithm}.error must be empty")
        else:
            if (
                did_execute
                or record["status"] != "unavailable"
                or record["version"] is not None
                or record["digest_sha256"] is not None
                or record["backend"] is not None
                or not isinstance(record["error"], str)
                or not record["error"].strip()
            ):
                raise ContractError(
                    f"{field}.{algorithm} unavailable record is inconsistent"
                )
            digest = ""

        if algorithm in requested:
            if not ready:
                raise ContractError(f"{field}.{algorithm}.ready must be true")
            if did_execute is not executed:
                raise ContractError(
                    f"{field}.{algorithm}.executed must be {str(executed).lower()}"
                )
            expected_status = "executed" if executed else "loaded"
            if record["status"] != expected_status:
                raise ContractError(
                    f"{field}.{algorithm}.status must be {expected_status}"
                )
            result[algorithm] = digest
    return result


def validate_capabilities(
    payload: Any,
    *,
    requested_algorithms: Sequence[str],
    expected_model_digests: Mapping[str, str],
    expected_auth_mode: str | None = None,
    received_unix_ms: int | None = None,
    max_age_ms: int = 5_000,
    max_clock_skew_ms: int = 1_000,
) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise ContractError("capabilities response must be an object")
    required = {
        "schema",
        "generated_unix_ms",
        "api_version",
        "request_schema",
        "response_schema",
        "transport",
        "execution",
        "limits",
        "algorithms",
        "models",
        "metric_3d",
        "auth",
    }
    _exact_keys(payload, required, field="capabilities")
    if payload["schema"] != CAPABILITIES_SCHEMA:
        raise ContractError("unexpected capabilities schema")
    if payload["api_version"] != "v1":
        raise ContractError("unsupported PNU API version")
    if payload["request_schema"] != REQUEST_SCHEMA:
        raise ContractError("worker request schema does not match adapter")
    if payload["response_schema"] != RESPONSE_SCHEMA:
        raise ContractError("worker response schema does not match adapter")
    generated = payload["generated_unix_ms"]
    if not isinstance(generated, int) or isinstance(generated, bool):
        raise ContractError("capabilities.generated_unix_ms must be an integer")
    if received_unix_ms is not None and (
        generated < received_unix_ms - max_age_ms
        or generated > received_unix_ms + max_clock_skew_ms
    ):
        raise ContractError("worker capabilities timestamp is stale or invalid")

    algorithms = normalize_algorithms(payload["algorithms"])
    if list(algorithms) != list(SUPPORTED_ALGORITHMS):
        raise ContractError("worker algorithms must use the canonical v1 order")
    requested = normalize_algorithms(requested_algorithms)
    if any(item not in algorithms for item in requested):
        raise ContractError("worker does not advertise every requested algorithm")

    transport = payload["transport"]
    if not isinstance(transport, dict):
        raise ContractError("capabilities.transport must be an object")
    _exact_keys(
        transport,
        {"content_type", "fields", "base64_allowed"},
        field="capabilities.transport",
    )
    if transport.get("content_type") != "multipart/form-data":
        raise ContractError("worker does not advertise multipart transport")
    if transport.get("base64_allowed") is not False:
        raise ContractError("worker must reject base64 image transport")
    fields = transport.get("fields")
    if not isinstance(fields, dict) or set(fields) != {"metadata", "rgb", "depth"}:
        raise ContractError("worker multipart fields are incomplete")
    metadata_field = fields["metadata"]
    if not isinstance(metadata_field, dict):
        raise ContractError("worker metadata field contract is invalid")
    _exact_keys(
        metadata_field,
        {"required", "content_type"},
        field="capabilities.transport.fields.metadata",
    )
    if metadata_field != {"required": True, "content_type": "application/json"}:
        raise ContractError("worker metadata field contract is incompatible")
    for name, required, content_types in (
        ("rgb", True, ["image/jpeg", "image/png"]),
        ("depth", False, ["application/octet-stream", "image/png"]),
    ):
        field_record = fields[name]
        if not isinstance(field_record, dict):
            raise ContractError(f"worker {name} field contract is invalid")
        _exact_keys(
            field_record,
            {"required", "content_types"},
            field=f"capabilities.transport.fields.{name}",
        )
        if field_record != {
            "required": required,
            "content_types": content_types,
        }:
            raise ContractError(f"worker {name} field contract is incompatible")

    execution = payload["execution"]
    if not isinstance(execution, dict):
        raise ContractError("capabilities.execution must be an object")
    if (
        execution.get("latest_frame_only") is not True
        or execution.get("max_in_flight") != 1
        or execution.get("queue_depth") != 0
        or execution.get("overload_status") != 429
    ):
        raise ContractError("worker execution policy is not latest-frame fail-closed")
    _exact_keys(
        execution,
        {"latest_frame_only", "max_in_flight", "queue_depth", "overload_status"},
        field="capabilities.execution",
    )

    limits = payload["limits"]
    expected_limit_keys = {
        "request_bytes",
        "response_json_bytes",
        "metadata_bytes",
        "rgb_bytes",
        "decoded_rgb_bytes",
        "depth_bytes",
        "image_pixels",
        "detections_per_algorithm",
        "total_rle_counts_per_algorithm",
        "deadline_ahead_ms",
        "rgb_depth_skew_ns",
    }
    if not isinstance(limits, dict):
        raise ContractError("capabilities.limits must be an object")
    _exact_keys(limits, expected_limit_keys, field="capabilities.limits")
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value <= 0
        for value in limits.values()
    ):
        raise ContractError("capabilities limits must be positive integers")
    if limits["response_json_bytes"] > _MAX_RESPONSE_JSON_BYTES:
        raise ContractError(
            "worker response_json_bytes exceeds the adapter response budget"
        )
    if (
        limits["total_rle_counts_per_algorithm"]
        > _MAX_ADVERTISED_RLE_COUNTS_PER_ALGORITHM
    ):
        raise ContractError(
            "worker total_rle_counts_per_algorithm exceeds the adapter budget"
        )

    metric = payload["metric_3d"]
    if not isinstance(metric, dict):
        raise ContractError("capabilities.metric_3d must be an object")
    _exact_keys(
        metric,
        {"enabled", "reason", "required_gates"},
        field="capabilities.metric_3d",
    )
    if not isinstance(metric.get("enabled"), bool):
        raise ContractError("capabilities.metric_3d.enabled must be boolean")
    _bounded_string(metric.get("reason"), field="capabilities.metric_3d.reason")
    gates = metric.get("required_gates")
    if (
        not isinstance(gates, list)
        or not gates
        or any(not isinstance(item, str) or not item.strip() for item in gates)
    ):
        raise ContractError("capabilities.metric_3d.required_gates is invalid")
    normalized_gates = {str(item).strip() for item in gates}
    if not {
        "registered_or_alignment_validated_depth",
        "alignment_validated_with_nonempty_id",
        "matching_rgb_frame_and_dimensions",
        "color_camera_info",
        "matching_color_and_depth_camera_info",
        "validated_depth_scale",
    }.issubset(normalized_gates):
        raise ContractError("worker metric 3-D gates omit a required RGB-D input gate")
    auth = payload["auth"]
    if not isinstance(auth, dict) or auth.get("mode") not in {"none", "bearer"}:
        raise ContractError("worker auth mode is unsupported")
    _exact_keys(auth, {"mode"}, field="capabilities.auth")
    if expected_auth_mode is not None and auth.get("mode") != expected_auth_mode:
        raise ContractError("worker auth mode does not match adapter configuration")

    digests = _validate_model_records(
        payload["models"],
        requested,
        field="capabilities.models",
        executed=False,
    )
    for algorithm, expected in expected_model_digests.items():
        if algorithm in requested and digests.get(algorithm) != expected:
            raise ContractError(f"capabilities model digest mismatch for {algorithm}")
    return digests


def validate_worker_health(
    payload: Any,
    *,
    requested_algorithms: Sequence[str],
    received_unix_ms: int,
    max_age_ms: int,
    max_clock_skew_ms: int,
) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise ContractError("worker health response must be an object")
    _exact_keys(
        payload,
        {
            "schema",
            "generated_unix_ms",
            "status",
            "ready",
            "api_version",
            "upstream",
            "models",
        },
        field="worker_health",
    )
    if payload["schema"] != WORKER_HEALTH_SCHEMA or payload["api_version"] != "v1":
        raise ContractError("worker health schema or API version is unsupported")
    status = payload["status"]
    ready = payload["ready"]
    if status not in {"ready", "degraded"} or not isinstance(ready, bool):
        raise ContractError("worker health status is invalid")
    raw_models = payload["models"]
    if not isinstance(raw_models, dict):
        raise ContractError("worker_health.models must be an object")
    all_models_ready = all(
        isinstance(raw_models.get(name), dict) and raw_models[name].get("ready") is True
        for name in SUPPORTED_ALGORITHMS
    )
    if (ready, status) != (
        all_models_ready,
        "ready" if all_models_ready else "degraded",
    ):
        raise ContractError("worker global health is inconsistent with model records")
    generated = payload["generated_unix_ms"]
    if (
        not isinstance(generated, int)
        or isinstance(generated, bool)
        or generated < received_unix_ms - max_age_ms
        or generated > received_unix_ms + max_clock_skew_ms
    ):
        raise ContractError("worker health timestamp is stale or invalid")
    upstream = payload["upstream"]
    if not isinstance(upstream, dict):
        raise ContractError("worker_health.upstream must be an object")
    _exact_keys(
        upstream,
        {"repository", "expected_commit", "detected_commit"},
        field="worker_health.upstream",
    )
    if (
        upstream["repository"] != EXPECTED_UPSTREAM_REPOSITORY
        or upstream["expected_commit"] != EXPECTED_UPSTREAM_COMMIT
        or upstream["detected_commit"] != EXPECTED_UPSTREAM_COMMIT
    ):
        raise ContractError("worker upstream repository commit does not match the pin")
    return _validate_model_records(
        raw_models,
        normalize_algorithms(requested_algorithms),
        field="worker_health.models",
        executed=False,
    )


def _validate_bbox(value: Any, *, field: str) -> list[float]:
    if not isinstance(value, list) or len(value) != 4:
        raise ContractError(f"{field} must be a four-number array")
    result = []
    for item in value:
        if not _is_number(item):
            raise ContractError(f"{field} must contain finite numbers")
        number = float(item)
        if number < 0.0:
            raise ContractError(f"{field} coordinates must be nonnegative")
        result.append(number)
    if result[2] < result[0] or result[3] < result[1]:
        raise ContractError(f"{field} has reversed coordinates")
    return result


def _validate_confidence(value: Any, *, field: str) -> float:
    if not _is_number(value) or not 0.0 <= float(value) <= 1.0:
        raise ContractError(f"{field} must be a finite probability")
    return float(value)


def _compressed_coco_counts_to_list(
    value: str,
    *,
    field: str,
    max_counts: int = _MAX_RLE_COUNTS,
) -> list[int]:
    if not isinstance(max_counts, int) or isinstance(max_counts, bool) or max_counts < 0:
        raise ContractError(f"{field} has an invalid RLE budget")
    counts: list[int] = []
    cursor = 0
    while cursor < len(value):
        result = 0
        shift = 0
        while True:
            if cursor >= len(value):
                raise ContractError(f"{field} has a truncated compressed count")
            encoded = ord(value[cursor]) - 48
            cursor += 1
            if encoded < 0 or encoded > 0x3F:
                raise ContractError(f"{field} has an invalid compressed character")
            result |= (encoded & 0x1F) << (5 * shift)
            more = bool(encoded & 0x20)
            shift += 1
            if not more:
                if encoded & 0x10:
                    result |= -1 << (5 * shift)
                break
            if shift > 16:
                raise ContractError(f"{field} compressed count is oversized")
        if len(counts) > 2:
            result += counts[-2]
        if result < 0:
            raise ContractError(f"{field} decodes to a negative run")
        if len(counts) >= max_counts:
            raise ContractError(f"{field} exceeds the remaining RLE run budget")
        counts.append(int(result))
    return counts


def _coco_counts_to_compressed_string(counts: Sequence[int]) -> str:
    output: list[str] = []
    for index, raw in enumerate(counts):
        value = int(raw)
        if index > 2:
            value -= int(counts[index - 2])
        while True:
            encoded = value & 0x1F
            value >>= 5
            more = value != (-1 if encoded & 0x10 else 0)
            if more:
                encoded |= 0x20
            output.append(chr(encoded + 48))
            if not more:
                break
    return "".join(output)


def _rle_counts(
    value: Mapping[str, Any],
    *,
    field: str,
    max_counts: int = _MAX_RLE_COUNTS,
) -> list[int]:
    counts = value["counts"]
    if isinstance(counts, str):
        return _compressed_coco_counts_to_list(
            counts,
            field=f"{field}.counts",
            max_counts=max_counts,
        )
    if len(counts) > max_counts:
        raise ContractError(f"{field}.counts exceeds the remaining RLE run budget")
    return [int(item) for item in counts]


def _validate_rle(
    value: Any,
    *,
    field: str,
    remaining_counts: int = _MAX_RLE_COUNTS,
) -> int:
    if (
        not isinstance(remaining_counts, int)
        or isinstance(remaining_counts, bool)
        or remaining_counts < 0
        or remaining_counts > _MAX_RLE_COUNTS
    ):
        raise ContractError(f"{field} has an invalid remaining RLE budget")
    if not isinstance(value, dict):
        raise ContractError(f"{field} must be an object")
    _exact_keys(value, {"size", "counts"}, field=field)
    size = value["size"]
    if (
        not isinstance(size, list)
        or len(size) != 2
        or any(
            not isinstance(item, int) or isinstance(item, bool) or item <= 0
            for item in size
        )
    ):
        raise ContractError(f"{field}.size must contain two positive integers")
    counts = value["counts"]
    if isinstance(counts, str):
        if not counts or len(counts) > _MAX_RLE_COUNTS:
            raise ContractError(f"{field}.counts is empty or oversized")
    elif isinstance(counts, list):
        if len(counts) > remaining_counts:
            raise ContractError(
                f"{field}.counts exceeds the remaining RLE run budget"
            )
        if len(counts) > _MAX_RLE_COUNTS or any(
            not isinstance(item, int) or isinstance(item, bool) or item < 0
            for item in counts
        ):
            raise ContractError(f"{field}.counts must be nonnegative integers")
    else:
        raise ContractError(f"{field}.counts must be a string or integer array")
    decoded = _rle_counts(value, field=field, max_counts=remaining_counts)
    if not decoded or sum(decoded) != int(size[0]) * int(size[1]):
        raise ContractError(f"{field}.counts do not cover the declared mask")
    return len(decoded)


def _rle_mask_stats(value: Mapping[str, Any]) -> tuple[list[float], int, str]:
    height, width = (int(item) for item in value["size"])
    counts = _rle_counts(value, field="mask_rle")
    area = sum(counts[1::2])
    min_x, min_y = width, height
    max_x = max_y = -1
    offset = 0
    for run_index, run_length in enumerate(counts):
        end = offset + int(run_length)
        if run_index % 2 == 1 and run_length:
            start_column = offset // height
            end_column = (end - 1) // height
            min_x = min(min_x, start_column)
            max_x = max(max_x, end_column)
            if start_column == end_column:
                min_y = min(min_y, offset % height)
                max_y = max(max_y, (end - 1) % height)
            else:
                min_y = 0
                max_y = height - 1
        offset = end
    bbox = (
        [float(min_x), float(min_y), float(max_x + 1), float(max_y + 1)]
        if area > 0
        else [0.0, 0.0, 0.0, 0.0]
    )
    encoded = (
        str(value["counts"])
        if isinstance(value["counts"], str)
        else _coco_counts_to_compressed_string(counts)
    )
    return bbox, int(area), encoded


def _rle_foreground_segments(
    value: Mapping[str, Any],
    *,
    field: str,
) -> Iterable[tuple[int, int, int]]:
    """Yield bounded ``(x, y_start, y_end)`` foreground runs.

    COCO RLE is column-major.  Splitting a foreground run only at column
    boundaries lets us recompute centroids and unions without expanding an
    attacker-controlled mask to a dense image.
    """

    height, width = (int(item) for item in value["size"])
    offset = 0
    segment_count = 0
    for run_index, raw_run_length in enumerate(
        _rle_counts(value, field=field, max_counts=_MAX_RLE_COUNTS)
    ):
        run_length = int(raw_run_length)
        end = offset + run_length
        if run_index % 2 == 1:
            cursor = offset
            while cursor < end:
                x = cursor // height
                if x >= width:
                    raise ContractError(f"{field} foreground exceeds its mask")
                column_end = min(end, (x + 1) * height)
                segment_count += 1
                if segment_count > _MAX_BLOOD_UNION_SEGMENTS:
                    raise ContractError(
                        f"{field} exceeds the bounded mask-segment budget"
                    )
                yield x, cursor % height, (column_end - 1) % height + 1
                cursor = column_end
        offset = end


def _rle_centroid(
    value: Mapping[str, Any],
    *,
    field: str,
) -> list[float] | None:
    area = 0
    sum_x = 0
    sum_y = 0
    for x, y_start, y_end in _rle_foreground_segments(value, field=field):
        length = y_end - y_start
        area += length
        sum_x += x * length
        sum_y += (y_start + y_end - 1) * length // 2
    if area == 0:
        return None
    return [sum_x / area, sum_y / area]


def _rle_union_centroid(
    values: Sequence[Mapping[str, Any]],
    *,
    field: str,
) -> list[float] | None:
    """Recompute the pixel centroid of a bounded union of validated RLEs."""

    intervals_by_column: dict[int, list[tuple[int, int]]] = defaultdict(list)
    segment_count = 0
    for mask_index, value in enumerate(values):
        for x, y_start, y_end in _rle_foreground_segments(
            value,
            field=f"{field}[{mask_index}]",
        ):
            segment_count += 1
            if segment_count > _MAX_BLOOD_UNION_SEGMENTS:
                raise ContractError(f"{field} exceeds the bounded union budget")
            intervals_by_column[x].append((y_start, y_end))

    area = 0
    sum_x = 0
    sum_y = 0
    for x, intervals in intervals_by_column.items():
        intervals.sort()
        merged_start: int | None = None
        merged_end = 0
        for y_start, y_end in intervals:
            if merged_start is None:
                merged_start, merged_end = y_start, y_end
                continue
            if y_start <= merged_end:
                merged_end = max(merged_end, y_end)
                continue
            length = merged_end - merged_start
            area += length
            sum_x += x * length
            sum_y += (merged_start + merged_end - 1) * length // 2
            merged_start, merged_end = y_start, y_end
        if merged_start is not None:
            length = merged_end - merged_start
            area += length
            sum_x += x * length
            sum_y += (merged_start + merged_end - 1) * length // 2
    if area == 0:
        return None
    return [sum_x / area, sum_y / area]


def _rle_contains_pixel(
    value: Mapping[str, Any],
    *,
    x: int,
    y: int,
) -> bool:
    """Check one pixel in bounded COCO column-major RLE without expanding it."""

    height, width = (int(item) for item in value["size"])
    if not (0 <= x < width and 0 <= y < height):
        return False
    target = x * height + y
    offset = 0
    for run_index, run_length in enumerate(
        _rle_counts(value, field="mask_rle")
    ):
        end = offset + int(run_length)
        if offset <= target < end:
            return run_index % 2 == 1
        offset = end
    return False


def _validate_image_shape(value: Any, *, field: str) -> tuple[int, int]:
    if not isinstance(value, dict):
        raise ContractError(f"{field} must be an object")
    _exact_keys(value, {"width", "height"}, field=field)
    width = value["width"]
    height = value["height"]
    if (
        not isinstance(width, int)
        or isinstance(width, bool)
        or not isinstance(height, int)
        or isinstance(height, bool)
        or width <= 0
        or height <= 0
        or width > 32768
        or height > 32768
    ):
        raise ContractError(f"{field} dimensions are invalid")
    return width, height


def _normalize_expected_rgb_dimensions(
    value: Sequence[int] | None,
    *,
    metadata: Mapping[str, Any],
) -> tuple[int, int] | None:
    camera_dimensions: tuple[int, int] | None = None
    camera_info = metadata.get("color_camera_info")
    if isinstance(camera_info, Mapping):
        camera_width = camera_info.get("width")
        camera_height = camera_info.get("height")
        if (
            not isinstance(camera_width, int)
            or isinstance(camera_width, bool)
            or not isinstance(camera_height, int)
            or isinstance(camera_height, bool)
            or camera_width <= 0
            or camera_height <= 0
            or camera_width > 32768
            or camera_height > 32768
            or camera_width * camera_height > 16_000_000
        ):
            raise ContractError("request color CameraInfo dimensions are invalid")
        camera_dimensions = (camera_width, camera_height)

    if value is None:
        return camera_dimensions
    if (
        isinstance(value, (str, bytes, bytearray))
        or not isinstance(value, Sequence)
        or len(value) != 2
        or any(
            not isinstance(item, int) or isinstance(item, bool) or item <= 0
            for item in value
        )
    ):
        raise ValueError("expected_rgb_dimensions must be (width, height)")
    dimensions = (int(value[0]), int(value[1]))
    if (
        dimensions[0] > 32768
        or dimensions[1] > 32768
        or dimensions[0] * dimensions[1] > 16_000_000
    ):
        raise ValueError("expected_rgb_dimensions is oversized")
    if camera_dimensions is not None and dimensions != camera_dimensions:
        raise ContractError(
            "decoded RGB dimensions disagree with request color CameraInfo"
        )
    return dimensions


def _validate_metric_result(value: Any, *, field: str) -> bool:
    if not isinstance(value, dict):
        raise ContractError(f"{field} must be an object")
    _exact_keys(value, {"ready", "status", "reasons"}, field=field)
    ready = value["ready"]
    if not isinstance(ready, bool):
        raise ContractError(f"{field}.ready must be boolean")
    status = _bounded_string(value["status"], field=f"{field}.status", maximum=80)
    reasons = value["reasons"]
    if (
        not isinstance(reasons, list)
        or len(reasons) > 64
        or any(
            not isinstance(item, str) or not item.strip() or len(item) > 160
            for item in reasons
        )
    ):
        raise ContractError(f"{field}.reasons must be a bounded string array")
    if ready and (reasons or status not in {"ready", "metric_ready"}):
        raise ContractError(f"{field} ready state is inconsistent")
    if not ready and not reasons:
        raise ContractError(f"{field} unready state requires reasons")
    return ready


def _optional_vector(
    value: Any,
    *,
    length: int,
    field: str,
) -> list[float] | None:
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or len(value) != length
        or any(not _is_number(item) for item in value)
    ):
        raise ContractError(f"{field} must be null or {length} finite numbers")
    return [float(item) for item in value]


def _validate_tool_observation(value: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{field} must be an object")
    _exact_keys(
        value,
        {
            "mask_bbox_xyxy_px",
            "mask_area_px",
            "observation_point_uv_px",
            "observation_point_valid",
            "observation_point_inside_mask",
            "observation_point_depth_valid",
            "observation_point_depth_m",
            "observation_point_selection_mode",
            "observation_point_boundary_clearance_px",
        },
        field=field,
    )
    mask_bbox = _validate_bbox(
        value["mask_bbox_xyxy_px"], field=f"{field}.mask_bbox_xyxy_px"
    )
    area = value["mask_area_px"]
    if not isinstance(area, int) or isinstance(area, bool) or area < 0:
        raise ContractError(f"{field}.mask_area_px must be nonnegative integer")
    point = _optional_vector(
        value["observation_point_uv_px"],
        length=2,
        field=f"{field}.observation_point_uv_px",
    )
    flags = (
        value["observation_point_valid"],
        value["observation_point_inside_mask"],
        value["observation_point_depth_valid"],
    )
    if any(not isinstance(item, bool) for item in flags):
        raise ContractError(f"{field} observation point flags must be boolean")
    point_valid, inside_mask, depth_valid = flags
    depth_m = value["observation_point_depth_m"]
    if depth_m is not None and (not _is_number(depth_m) or float(depth_m) <= 0.0):
        raise ContractError(f"{field}.observation_point_depth_m is invalid")
    if point_valid != (point is not None):
        raise ContractError(f"{field} point validity is inconsistent")
    if (inside_mask or depth_valid) and not point_valid:
        raise ContractError(f"{field} point evidence is inconsistent")
    if depth_valid != (depth_m is not None):
        raise ContractError(f"{field} depth validity is inconsistent")
    selection_mode = str(value["observation_point_selection_mode"])
    if point_valid:
        selection_mode = _bounded_string(
            selection_mode,
            field=f"{field}.observation_point_selection_mode",
            maximum=160,
        )
    elif selection_mode:
        raise ContractError(f"{field} invalid point must not claim a selection mode")
    clearance = value["observation_point_boundary_clearance_px"]
    if not _is_number(clearance) or float(clearance) < 0.0:
        raise ContractError(f"{field} boundary clearance is invalid")
    return {
        "mask_bbox_xyxy_px": mask_bbox,
        "mask_area_px": int(area),
        "observation_point_uv_px": point,
        "observation_point_valid": point_valid,
        "observation_point_inside_mask": inside_mask,
        "observation_point_depth_valid": depth_valid,
        "observation_point_depth_m": float(depth_m) if depth_m is not None else None,
        "observation_point_selection_mode": selection_mode,
        "observation_point_boundary_clearance_px": float(clearance),
    }


def _validate_tool_pose(value: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{field} must be an object")
    expected = {
        "position_m",
        "orientation_xyzw",
        "pose_mode",
        "position_valid",
        "orientation_valid",
        "dof_observed",
        "observation_point_definition",
        "axis_definition",
        "symmetry_type",
        "endpoint_sign_confidence",
        "valid_depth_ratio",
        "pose_point_count",
        "axis_anisotropy",
        "support_plane_inlier_ratio",
        "support_plane_residual_p95_m",
        "pose_confidence",
        "pose_confidence_calibrated",
        "validity",
        "status_flags",
        "invalid_reason",
    }
    _exact_keys(value, expected, field=field)
    position = _optional_vector(
        value["position_m"], length=3, field=f"{field}.position_m"
    )
    orientation = _optional_vector(
        value["orientation_xyzw"], length=4, field=f"{field}.orientation_xyzw"
    )
    position_valid = value["position_valid"]
    orientation_valid = value["orientation_valid"]
    if not isinstance(position_valid, bool) or not isinstance(orientation_valid, bool):
        raise ContractError(f"{field} pose validity flags must be boolean")
    if position_valid != (position is not None) or orientation_valid != (
        orientation is not None
    ):
        raise ContractError(f"{field} pose vectors and validity flags disagree")
    if position is not None and (
        any(abs(item) > 100.0 for item in position) or position[2] <= 0.0
    ):
        raise ContractError(f"{field}.position_m is outside bounded camera space")
    if orientation is not None:
        norm = math.sqrt(sum(item * item for item in orientation))
        if not 0.999 <= norm <= 1.001:
            raise ContractError(f"{field}.orientation_xyzw is not a unit quaternion")
    pose_mode = str(value["pose_mode"])
    if pose_mode not in {
        "INVALID",
        "POSITION_3D_ONLY",
        "PLANAR_4DOF_WITH_NORMAL_PRIOR",
    }:
        raise ContractError(
            f"{field}.pose_mode is outside the pinned planar Tool contract"
        )
    dof = value["dof_observed"]
    if (
        not isinstance(dof, list)
        or len(dof) != 6
        or any(not isinstance(item, bool) for item in dof)
    ):
        raise ContractError(f"{field}.dof_observed must contain six booleans")
    expected_dof = [
        position_valid,
        position_valid,
        position_valid,
        False,
        False,
        orientation_valid,
    ]
    if list(dof) != expected_dof:
        raise ContractError(
            f"{field}.dof_observed is outside the pinned planar Tool contract"
        )
    validity = str(value["validity"])
    if validity not in {"INVALID", "VALID", "DEGRADED", "STALE"}:
        raise ContractError(f"{field}.validity is invalid")
    if validity == "VALID" and not position_valid:
        raise ContractError(f"{field} valid pose has no metric position")
    expected_pose_mode = (
        "PLANAR_4DOF_WITH_NORMAL_PRIOR"
        if orientation_valid
        else "POSITION_3D_ONLY"
        if position_valid
        else "INVALID"
    )
    if pose_mode != expected_pose_mode:
        raise ContractError(
            f"{field}.pose_mode is inconsistent with the pinned planar Tool contract"
        )
    if orientation_valid and validity not in {"VALID", "DEGRADED"}:
        raise ContractError(f"{field} oriented pose has an unusable validity")
    if position_valid and not orientation_valid and validity != "DEGRADED":
        raise ContractError(f"{field} position-only pose must be DEGRADED")
    if not position_valid and validity != "INVALID":
        raise ContractError(f"{field} positionless pose must be INVALID")
    probabilities: dict[str, float] = {}
    for name in (
        "endpoint_sign_confidence",
        "valid_depth_ratio",
        "support_plane_inlier_ratio",
        "pose_confidence",
    ):
        probabilities[name] = _validate_confidence(value[name], field=f"{field}.{name}")
    nonnegative: dict[str, float] = {}
    for name in (
        "axis_anisotropy",
        "support_plane_residual_p95_m",
    ):
        item = value[name]
        if not _is_number(item) or float(item) < 0.0:
            raise ContractError(f"{field}.{name} must be finite and nonnegative")
        nonnegative[name] = float(item)
    point_count = value["pose_point_count"]
    if (
        not isinstance(point_count, int)
        or isinstance(point_count, bool)
        or point_count < 0
    ):
        raise ContractError(f"{field}.pose_point_count is invalid")
    calibrated = value["pose_confidence_calibrated"]
    if not isinstance(calibrated, bool):
        raise ContractError(f"{field}.pose_confidence_calibrated must be boolean")
    if calibrated or probabilities["pose_confidence"] != 0.0:
        raise ContractError(
            f"{field} must not claim a calibrated pose confidence"
        )
    status_flags = value["status_flags"]
    if (
        not isinstance(status_flags, list)
        or len(status_flags) > _MAX_STATUS_FLAGS
        or any(
            not isinstance(item, str) or not item.strip() or len(item) > 160
            for item in status_flags
        )
    ):
        raise ContractError(f"{field}.status_flags is invalid")
    invalid_reason = str(value["invalid_reason"])
    if len(invalid_reason) > 500:
        raise ContractError(f"{field}.invalid_reason is oversized")
    if validity == "INVALID" and not invalid_reason.strip():
        raise ContractError(f"{field} invalid pose requires an invalid_reason")
    for name in ("observation_point_definition", "axis_definition", "symmetry_type"):
        _bounded_string(value[name], field=f"{field}.{name}", maximum=500)
    if value["observation_point_definition"] != _TOOL_OBSERVATION_POINT_DEFINITION:
        raise ContractError(
            f"{field}.observation_point_definition does not match the pin"
        )
    if value["axis_definition"] != _TOOL_AXIS_DEFINITION:
        raise ContractError(f"{field}.axis_definition does not match the pin")
    if value["symmetry_type"] not in {"NONE", "C2"}:
        raise ContractError(f"{field}.symmetry_type is outside the pinned contract")
    if (
        position_valid
        and probabilities["valid_depth_ratio"] < _TOOL_MINIMUM_VALID_DEPTH_RATIO
    ):
        raise ContractError(
            f"{field} position_valid violates the pinned minimum depth ratio"
        )
    if (
        orientation_valid
        and nonnegative["axis_anisotropy"] < _TOOL_MINIMUM_AXIS_ANISOTROPY
    ):
        raise ContractError(
            f"{field} orientation_valid violates the pinned axis anisotropy"
        )
    if (
        orientation_valid
        and value["symmetry_type"] != "C2"
        and probabilities["endpoint_sign_confidence"]
        < _TOOL_MINIMUM_ENDPOINT_SIGN_CONFIDENCE
    ):
        raise ContractError(
            f"{field} orientation_valid violates the pinned endpoint sign confidence"
        )
    return {
        **{name: value[name] for name in expected},
        "position_m": position,
        "orientation_xyzw": orientation,
        "dof_observed": list(dof),
        **probabilities,
        **nonnegative,
        "pose_point_count": int(point_count),
        "status_flags": list(status_flags),
    }


def _validate_support_plane_diagnostics(
    value: Any,
    *,
    support_plane_validated: bool,
    expected_camera_info_sha256: str,
    support_plane_config_version: str,
    expected_support_plane_config_version: str,
) -> dict[str, Any]:
    field = "results.tool.support_plane_diagnostics"
    if not isinstance(value, dict):
        raise ContractError(f"{field} must be an object")
    _exact_keys(
        value,
        {
            "schema",
            "validation_requested",
            "artifact_loaded",
            "static_reasons",
            "calibration_fit",
            "runtime_validation",
        },
        field=field,
    )
    if value["schema"] != _SUPPORT_PLANE_DIAGNOSTICS_SCHEMA:
        raise ContractError(f"{field}.schema is invalid")
    validation_requested = value["validation_requested"]
    artifact_loaded = value["artifact_loaded"]
    if not isinstance(validation_requested, bool) or not isinstance(
        artifact_loaded, bool
    ):
        raise ContractError(f"{field} static flags must be boolean")

    static_reasons = value["static_reasons"]
    if (
        not isinstance(static_reasons, list)
        or len(static_reasons) > 16
        or any(
            not isinstance(item, str)
            or not item.strip()
            or len(item) > 160
            or "\n" in item
            or "\r" in item
            for item in static_reasons
        )
        or len(set(static_reasons)) != len(static_reasons)
    ):
        raise ContractError(f"{field}.static_reasons is invalid")
    if artifact_loaded == bool(static_reasons):
        raise ContractError(f"{field} artifact state and static reasons disagree")
    if artifact_loaded and not validation_requested:
        raise ContractError(f"{field} loaded artifact was not requested")

    calibration_fit = value["calibration_fit"]
    if not isinstance(calibration_fit, dict):
        raise ContractError(f"{field}.calibration_fit must be an object")
    _exact_keys(
        calibration_fit,
        {"available", "inlier_ratio", "residual_p95_m"},
        field=f"{field}.calibration_fit",
    )
    fit_available = calibration_fit["available"]
    if not isinstance(fit_available, bool) or fit_available is not artifact_loaded:
        raise ContractError(f"{field}.calibration_fit availability is inconsistent")
    fit_inlier_ratio = calibration_fit["inlier_ratio"]
    fit_residual_p95_m = calibration_fit["residual_p95_m"]
    if fit_available:
        if (
            not _is_number(fit_inlier_ratio)
            or not 0.0 <= float(fit_inlier_ratio) <= 1.0
            or not _is_number(fit_residual_p95_m)
            or not 0.0 <= float(fit_residual_p95_m) <= 10.0
        ):
            raise ContractError(f"{field}.calibration_fit metrics are invalid")
    elif fit_inlier_ratio is not None or fit_residual_p95_m is not None:
        raise ContractError(f"{field}.calibration_fit unavailable metrics must be null")

    runtime = value["runtime_validation"]
    if not isinstance(runtime, dict):
        raise ContractError(f"{field}.runtime_validation must be an object")
    _exact_keys(
        runtime,
        {
            "evaluated",
            "metrics_available",
            "valid",
            "reasons",
            "sample_count",
            "inlier_ratio",
            "residual_median_m",
            "residual_p95_m",
            "camera_info_sha256",
        },
        field=f"{field}.runtime_validation",
    )
    evaluated = runtime["evaluated"]
    metrics_available = runtime["metrics_available"]
    runtime_valid = runtime["valid"]
    if any(
        not isinstance(item, bool)
        for item in (evaluated, metrics_available, runtime_valid)
    ):
        raise ContractError(f"{field}.runtime_validation flags must be boolean")
    runtime_reasons = runtime["reasons"]
    if (
        not isinstance(runtime_reasons, list)
        or len(runtime_reasons) > 16
        or any(
            not isinstance(item, str)
            or not item.strip()
            or len(item) > 160
            or "\n" in item
            or "\r" in item
            for item in runtime_reasons
        )
        or len(set(runtime_reasons)) != len(runtime_reasons)
        or runtime_valid == bool(runtime_reasons)
    ):
        raise ContractError(f"{field}.runtime_validation reasons are inconsistent")
    sample_count = runtime["sample_count"]
    if (
        not isinstance(sample_count, int)
        or isinstance(sample_count, bool)
        or not 0 <= sample_count <= 50_000
    ):
        raise ContractError(f"{field}.runtime_validation.sample_count is invalid")
    runtime_metrics = (
        runtime["inlier_ratio"],
        runtime["residual_median_m"],
        runtime["residual_p95_m"],
    )
    if metrics_available:
        if (
            not evaluated
            or sample_count <= 0
            or not _is_number(runtime_metrics[0])
            or not 0.0 <= float(runtime_metrics[0]) <= 1.0
            or any(
                not _is_number(item) or not 0.0 <= float(item) <= 10.0
                for item in runtime_metrics[1:]
            )
        ):
            raise ContractError(f"{field}.runtime_validation metrics are invalid")
    elif sample_count != 0 or any(item is not None for item in runtime_metrics):
        raise ContractError(
            f"{field}.runtime_validation unavailable metrics must be null"
        )
    camera_info_sha256 = runtime["camera_info_sha256"]
    if not isinstance(camera_info_sha256, str) or (
        camera_info_sha256 and not _SHA256_RE.fullmatch(camera_info_sha256)
    ):
        raise ContractError(
            f"{field}.runtime_validation.camera_info_sha256 is invalid"
        )
    if not evaluated and camera_info_sha256:
        raise ContractError(f"{field}.runtime_validation was not evaluated")
    if evaluated and camera_info_sha256 != expected_camera_info_sha256:
        raise ContractError(
            f"{field}.runtime_validation CameraInfo digest does not match the request"
        )
    if runtime_valid != support_plane_validated:
        raise ContractError(f"{field} validity disagrees with results.tool")
    if runtime_valid and (
        not validation_requested
        or not artifact_loaded
        or not evaluated
        or not metrics_available
        or not camera_info_sha256
    ):
        raise ContractError(f"{field} valid state lacks live plane evidence")
    reviewed_plane_is_pinned = (
        support_plane_config_version == _REVIEWED_SUPPORT_PLANE_CONFIG_VERSION
        and expected_support_plane_config_version
        == _REVIEWED_SUPPORT_PLANE_CONFIG_VERSION
    )
    if runtime_valid and reviewed_plane_is_pinned and (
        sample_count < _REVIEWED_SUPPORT_PLANE_RUNTIME_MIN_SAMPLE_COUNT
        or float(runtime_metrics[0])
        < _REVIEWED_SUPPORT_PLANE_RUNTIME_MIN_INLIER_RATIO
        or float(runtime_metrics[1])
        > _REVIEWED_SUPPORT_PLANE_RUNTIME_MAX_RESIDUAL_MEDIAN_M
        or float(runtime_metrics[2])
        > _REVIEWED_SUPPORT_PLANE_RUNTIME_MAX_RESIDUAL_P95_M
    ):
        raise ContractError(
            f"{field}.runtime_validation violates the reviewed support-plane "
            "runtime thresholds"
        )

    return {
        "schema": _SUPPORT_PLANE_DIAGNOSTICS_SCHEMA,
        "validation_requested": validation_requested,
        "artifact_loaded": artifact_loaded,
        "static_reasons": list(static_reasons),
        "calibration_fit": {
            "available": fit_available,
            "inlier_ratio": (
                float(fit_inlier_ratio) if fit_inlier_ratio is not None else None
            ),
            "residual_p95_m": (
                float(fit_residual_p95_m)
                if fit_residual_p95_m is not None
                else None
            ),
        },
        "runtime_validation": {
            "evaluated": evaluated,
            "metrics_available": metrics_available,
            "valid": runtime_valid,
            "reasons": list(runtime_reasons),
            "sample_count": sample_count,
            "inlier_ratio": (
                float(runtime_metrics[0]) if runtime_metrics[0] is not None else None
            ),
            "residual_median_m": (
                float(runtime_metrics[1]) if runtime_metrics[1] is not None else None
            ),
            "residual_p95_m": (
                float(runtime_metrics[2]) if runtime_metrics[2] is not None else None
            ),
            "camera_info_sha256": camera_info_sha256,
        },
    }


def _validate_tool_results(
    value: Any,
    *,
    color_camera_info: Mapping[str, Any] | None = None,
    expected_calibration_version: str = "",
    expected_support_plane_config_version: str = "",
    expected_support_plane_normal: tuple[float, float, float] | None = None,
    support_plane_normal_tolerance_deg: float = (
        _DEFAULT_SUPPORT_PLANE_NORMAL_TOLERANCE_DEG
    ),
) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, dict):
        raise ContractError("results.tool must be an object")
    schema = value.get("schema")
    rgbd = schema == "pnu.tool.rgbd.v1"
    expected_keys = {"schema", "executed", "image", "detections"}
    if rgbd:
        expected_keys |= {
            "metric_3d",
            "ontology_version",
            "calibration_version",
            "pose_convention_version",
            "support_plane_config_version",
            "support_plane_validated",
            "support_plane_diagnostics",
        }
    _exact_keys(value, expected_keys, field="results.tool")
    if schema not in {"pnu.tool.2d.v1", "pnu.tool.rgbd.v1"}:
        raise ContractError("unexpected tool result schema")
    if value["executed"] is not True:
        raise ContractError("results.tool.executed must be true")
    support_plane_diagnostics: dict[str, Any] | None = None
    if rgbd:
        if not _validate_metric_result(
            value["metric_3d"], field="results.tool.metric_3d"
        ):
            raise ContractError("RGBD tool result must have metric 3-D ready")
        for name in (
            "ontology_version",
            "calibration_version",
            "pose_convention_version",
            "support_plane_config_version",
        ):
            _bounded_string(value[name], field=f"results.tool.{name}", maximum=240)
        if not isinstance(value["support_plane_validated"], bool):
            raise ContractError("results.tool.support_plane_validated must be boolean")
        if not isinstance(color_camera_info, Mapping):
            raise ContractError("RGBD Tool result requires request color CameraInfo")
        expected_camera_info_sha256 = _camera_info_sha256(color_camera_info)
        support_plane_diagnostics = _validate_support_plane_diagnostics(
            value["support_plane_diagnostics"],
            support_plane_validated=value["support_plane_validated"],
            expected_camera_info_sha256=expected_camera_info_sha256,
            support_plane_config_version=str(value["support_plane_config_version"]),
            expected_support_plane_config_version=(
                expected_support_plane_config_version
            ),
        )
        if value["ontology_version"] != _TOOL_ONTOLOGY_VERSION:
            raise ContractError("results.tool.ontology_version does not match the pin")
        if value["pose_convention_version"] != _TOOL_POSE_CONVENTION_VERSION:
            raise ContractError(
                "results.tool.pose_convention_version does not match the pin"
            )
        if value["calibration_version"] != expected_calibration_version:
            raise ContractError(
                "results.tool.calibration_version does not match the request"
            )
    image_width, image_height = _validate_image_shape(
        value["image"], field="results.tool.image"
    )
    detections = value["detections"]
    if not isinstance(detections, list) or len(detections) > _MAX_DETECTIONS:
        raise ContractError("results.tool.detections must be a bounded array")
    normalized: list[dict[str, Any]] = []
    ids: set[int] = set()
    total_rle_counts = 0
    for index, row in enumerate(detections):
        field = f"results.tool.detections[{index}]"
        if not isinstance(row, dict):
            raise ContractError(f"{field} must be an object")
        expected_row_keys = {
            "instance_id",
            "canonical_class_id",
            "class_name",
            "confidence",
            "bbox_xyxy_px",
            "mask_rle",
        }
        if rgbd:
            expected_row_keys |= {"model_class_index", "observation", "pose"}
        _exact_keys(row, expected_row_keys, field=field)
        instance_id = row["instance_id"]
        class_id = row["canonical_class_id"]
        if (
            not isinstance(instance_id, int)
            or isinstance(instance_id, bool)
            or instance_id < 0
            or instance_id in ids
        ):
            raise ContractError(f"{field}.instance_id is invalid or duplicated")
        if not isinstance(class_id, int) or isinstance(class_id, bool) or class_id <= 0:
            raise ContractError(f"{field}.canonical_class_id must be positive")
        ids.add(instance_id)
        class_name = _bounded_string(
            row["class_name"], field=f"{field}.class_name", maximum=80
        )
        if _TOOL_CANONICAL_CLASSES.get(class_id) != class_name:
            raise ContractError(
                f"{field} class id/name disagrees with the pinned Tool ontology"
            )
        confidence = _validate_confidence(
            row["confidence"], field=f"{field}.confidence"
        )
        bbox = _validate_bbox(row["bbox_xyxy_px"], field=f"{field}.bbox_xyxy_px")
        if bbox[2] > image_width or bbox[3] > image_height:
            raise ContractError(f"{field}.bbox_xyxy_px exceeds image dimensions")
        total_rle_counts += _validate_rle(
            row["mask_rle"],
            field=f"{field}.mask_rle",
            remaining_counts=(
                _MAX_ADVERTISED_RLE_COUNTS_PER_ALGORITHM - total_rle_counts
            ),
        )
        if total_rle_counts > _MAX_ADVERTISED_RLE_COUNTS_PER_ALGORITHM:
            raise ContractError("results.tool exceeds the cumulative RLE budget")
        if row["mask_rle"]["size"] != [image_height, image_width]:
            raise ContractError(f"{field}.mask_rle size does not match image")
        mask_bbox, mask_area, mask_counts = _rle_mask_stats(row["mask_rle"])
        if rgbd:
            model_class_index = row["model_class_index"]
            if (
                not isinstance(model_class_index, int)
                or isinstance(model_class_index, bool)
                or not 0 <= model_class_index <= 65535
            ):
                raise ContractError(f"{field}.model_class_index is invalid")
            if model_class_index != class_id - 1:
                raise ContractError(
                    f"{field}.model_class_index disagrees with the pinned Tool ontology"
                )
            observation = _validate_tool_observation(
                row["observation"], field=f"{field}.observation"
            )
            pose = _validate_tool_pose(row["pose"], field=f"{field}.pose")
            assert support_plane_diagnostics is not None
            calibration_fit = support_plane_diagnostics["calibration_fit"]
            if calibration_fit["available"] and (
                not math.isclose(
                    float(pose["support_plane_inlier_ratio"]),
                    float(calibration_fit["inlier_ratio"]),
                    rel_tol=0.0,
                    abs_tol=1e-6,
                )
                or not math.isclose(
                    float(pose["support_plane_residual_p95_m"]),
                    float(calibration_fit["residual_p95_m"]),
                    rel_tol=0.0,
                    abs_tol=1e-6,
                )
            ):
                raise ContractError(
                    f"{field}.pose support-plane fit disagrees with diagnostics"
                )
            support_plane_identity_is_pinned = bool(
                expected_support_plane_config_version
                and value["support_plane_config_version"]
                == expected_support_plane_config_version
            )
            if (
                pose["orientation_valid"]
                and value["support_plane_validated"]
                and support_plane_identity_is_pinned
            ):
                if expected_support_plane_normal is None:
                    raise ContractError(
                        f"{field}.pose orientation lacks a pinned support-plane normal"
                    )
                orientation_rotation = _quaternion_xyzw_to_rotation_matrix(
                    pose["orientation_xyzw"]
                )
                orientation_z_axis = tuple(
                    float(orientation_rotation[row_index][2])
                    for row_index in range(3)
                )
                cosine = max(
                    -1.0,
                    min(1.0, _dot3(orientation_z_axis, expected_support_plane_normal)),
                )
                angular_error_deg = math.degrees(math.acos(cosine))
                if angular_error_deg > support_plane_normal_tolerance_deg:
                    raise ContractError(
                        f"{field}.pose +Z axis disagrees with the pinned "
                        "support-plane normal"
                    )
            if observation["mask_bbox_xyxy_px"] != mask_bbox:
                raise ContractError(f"{field}.observation mask bbox does not match RLE")
            if observation["mask_area_px"] != mask_area:
                raise ContractError(f"{field}.observation mask area does not match RLE")
            if mask_area <= 0:
                raise ContractError(f"{field} RGB-D instance mask must be nonempty")
            if pose["pose_point_count"] > mask_area:
                raise ContractError(
                    f"{field}.pose_point_count exceeds the instance mask area"
                )
            expected_valid_depth_ratio = pose["pose_point_count"] / mask_area
            if not math.isclose(
                pose["valid_depth_ratio"],
                expected_valid_depth_ratio,
                rel_tol=0.0,
                abs_tol=1e-6,
            ):
                raise ContractError(
                    f"{field}.valid_depth_ratio disagrees with pose point evidence"
                )
            if pose["position_valid"] != observation["observation_point_depth_valid"]:
                raise ContractError(
                    f"{field} pose and observation depth validity disagree"
                )
            point_uv = observation["observation_point_uv_px"]
            if point_uv is not None:
                point_x, point_y = (int(round(float(item))) for item in point_uv)
                point_inside_image = (
                    0 <= float(point_uv[0]) < image_width
                    and 0 <= float(point_uv[1]) < image_height
                    and 0 <= point_x < image_width
                    and 0 <= point_y < image_height
                )
                point_inside_mask = bool(
                    point_inside_image
                    and _rle_contains_pixel(
                        row["mask_rle"],
                        x=point_x,
                        y=point_y,
                    )
                )
                if observation["observation_point_inside_mask"] is not point_inside_mask:
                    raise ContractError(
                        f"{field} observed point mask-membership claim is false"
                    )
                if not point_inside_mask:
                    raise ContractError(
                        f"{field} observed point is outside the instance mask"
                    )
            if pose["position_valid"]:
                position = pose["position_m"]
                observation_depth_m = observation["observation_point_depth_m"]
                assert position is not None
                assert observation_depth_m is not None
                assert point_uv is not None
                if not math.isclose(
                    float(position[2]),
                    float(observation_depth_m),
                    rel_tol=0.0,
                    abs_tol=_TOOL_POSITION_DEPTH_ABS_TOLERANCE_M,
                ):
                    raise ContractError(
                        f"{field} pose Z disagrees with observed depth"
                    )
                camera = CameraCalibration(
                    received_monotonic=0.0,
                    stamp_ns=int(color_camera_info.get("stamp_ns", 0)),
                    frame_id=str(color_camera_info.get("frame_id", "")),
                    payload=dict(color_camera_info),
                )
                projected_uv = _project_camera_point(position, camera)
                if math.hypot(
                    projected_uv[0] - float(point_uv[0]),
                    projected_uv[1] - float(point_uv[1]),
                ) > _TOOL_POSITION_REPROJECTION_TOLERANCE_PX:
                    raise ContractError(
                        f"{field} pose origin does not reproject to the observed point"
                    )
            expected_symmetry = "C2" if class_id == 7 else "NONE"
            if pose["symmetry_type"] != expected_symmetry:
                raise ContractError(
                    f"{field}.pose.symmetry_type disagrees with the pinned ontology"
                )
        else:
            model_class_index = class_id - 1
            observation = {
                "mask_bbox_xyxy_px": mask_bbox,
                "mask_area_px": mask_area,
                "observation_point_uv_px": None,
                "observation_point_valid": False,
                "observation_point_inside_mask": False,
                "observation_point_depth_valid": False,
                "observation_point_depth_m": None,
                "observation_point_selection_mode": "",
                "observation_point_boundary_clearance_px": 0.0,
            }
            pose = None
        normalized.append(
            {
                "instance_id": instance_id,
                "canonical_class_id": class_id,
                "model_class_index": int(model_class_index),
                "class_name": class_name,
                "confidence": confidence,
                "bbox_xyxy_px": bbox,
                "mask_rle": dict(row["mask_rle"]),
                "mask_counts": mask_counts,
                "observation": observation,
                "pose": pose,
            }
        )
    if rgbd and value["support_plane_validated"] is False:
        for index, row in enumerate(normalized):
            pose = row["pose"]
            if pose["orientation_valid"] is True:
                raise ContractError(
                    "results.tool.support_plane_validated is false but "
                    f"detections[{index}] claims a valid orientation"
                )
            if "SUPPORT_PLANE_UNVALIDATED" not in pose["status_flags"]:
                raise ContractError(
                    "results.tool.support_plane_validated is false but "
                    f"detections[{index}] lacks SUPPORT_PLANE_UNVALIDATED"
                )
            if pose["validity"] == "VALID":
                raise ContractError(
                    "results.tool.support_plane_validated is false but "
                    f"detections[{index}] claims VALID"
                )
    elif rgbd:
        for index, row in enumerate(normalized):
            if "SUPPORT_PLANE_UNVALIDATED" in row["pose"]["status_flags"]:
                raise ContractError(
                    "results.tool.support_plane_validated is true but "
                    f"detections[{index}] claims SUPPORT_PLANE_UNVALIDATED"
                )
    return tuple(normalized)


def _validate_blood_results(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, dict):
        raise ContractError("results.blood must be an object")
    schema = value.get("schema")
    rgbd = schema == "pnu.blood.rgbd.v1"
    expected_keys = {"schema", "executed", "image", "detections"}
    if rgbd:
        expected_keys |= {
            "metric_3d",
            "combined_blood_centroid_xy_px",
            "combined_blood_centroid_depth_m",
        }
    _exact_keys(value, expected_keys, field="results.blood")
    if schema not in {"pnu.blood.2d.v1", "pnu.blood.rgbd.v1"}:
        raise ContractError("unexpected blood result schema")
    if value["executed"] is not True:
        raise ContractError("results.blood.executed must be true")
    if rgbd:
        _validate_metric_result(value["metric_3d"], field="results.blood.metric_3d")
    image_width, image_height = _validate_image_shape(
        value["image"], field="results.blood.image"
    )
    detections = value["detections"]
    if not isinstance(detections, list) or len(detections) > _MAX_DETECTIONS:
        raise ContractError("results.blood.detections must be a bounded array")
    normalized: list[dict[str, Any]] = []
    validated_masks: list[Mapping[str, Any]] = []
    ids: set[int] = set()
    total_rle_counts = 0
    for index, row in enumerate(detections):
        field = f"results.blood.detections[{index}]"
        if not isinstance(row, dict):
            raise ContractError(f"{field} must be an object")
        expected_row_keys = {
            "instance_id",
            "class_id",
            "class_name",
            "confidence",
            "bbox_xyxy_px",
            "centroid_xy_px",
            "mask_rle",
        }
        if rgbd:
            expected_row_keys.add("centroid_depth_m")
        _exact_keys(row, expected_row_keys, field=field)
        instance_id = row["instance_id"]
        if (
            not isinstance(instance_id, int)
            or isinstance(instance_id, bool)
            or instance_id < 0
            or instance_id in ids
        ):
            raise ContractError(f"{field}.instance_id is invalid or duplicated")
        ids.add(instance_id)
        if row["class_id"] != 1 or row["class_name"] != "blood":
            raise ContractError(f"{field} must use the canonical blood class")
        confidence = _validate_confidence(
            row["confidence"], field=f"{field}.confidence"
        )
        bbox = _validate_bbox(row["bbox_xyxy_px"], field=f"{field}.bbox_xyxy_px")
        if bbox[2] > image_width or bbox[3] > image_height:
            raise ContractError(f"{field}.bbox_xyxy_px exceeds image dimensions")
        centroid = row["centroid_xy_px"]
        if (
            not isinstance(centroid, list)
            or len(centroid) != 2
            or any(not _is_number(item) or float(item) < 0.0 for item in centroid)
            or float(centroid[0]) > image_width
            or float(centroid[1]) > image_height
        ):
            raise ContractError(f"{field}.centroid_xy_px is invalid")
        total_rle_counts += _validate_rle(
            row["mask_rle"],
            field=f"{field}.mask_rle",
            remaining_counts=(
                _MAX_ADVERTISED_RLE_COUNTS_PER_ALGORITHM - total_rle_counts
            ),
        )
        if total_rle_counts > _MAX_ADVERTISED_RLE_COUNTS_PER_ALGORITHM:
            raise ContractError("results.blood exceeds the cumulative RLE budget")
        if row["mask_rle"]["size"] != [image_height, image_width]:
            raise ContractError(f"{field}.mask_rle size does not match image")
        calculated_centroid = _rle_centroid(
            row["mask_rle"],
            field=f"{field}.mask_rle",
        )
        if calculated_centroid is None:
            raise ContractError(f"{field}.mask_rle is empty")
        if any(
            not math.isclose(
                float(claimed),
                calculated,
                rel_tol=0.0,
                abs_tol=_BLOOD_CENTROID_TOLERANCE_PX,
            )
            for claimed, calculated in zip(
                centroid,
                calculated_centroid,
                strict=True,
            )
        ):
            raise ContractError(
                f"{field}.centroid_xy_px disagrees with its validated mask"
            )
        validated_masks.append(row["mask_rle"])
        centroid_depth_m = row.get("centroid_depth_m") if rgbd else None
        if centroid_depth_m is not None and (
            not _is_number(centroid_depth_m) or float(centroid_depth_m) <= 0.0
        ):
            raise ContractError(f"{field}.centroid_depth_m is invalid")
        normalized.append(
            {
                "instance_id": instance_id,
                "class_name": "blood",
                "confidence": confidence,
                "bbox_xyxy_px": bbox,
                "centroid_xy_px": [float(item) for item in centroid],
                "centroid_depth_m": (
                    float(centroid_depth_m) if centroid_depth_m is not None else None
                ),
                # Retain only the already-validated segmentation evidence for
                # the local debug renderer.  The same normalized response is
                # used whether the HTTP worker is loopback or on the LAN.
                "mask_rle": dict(row["mask_rle"]),
            }
        )
    if rgbd:
        combined_centroid = _optional_vector(
            value["combined_blood_centroid_xy_px"],
            length=2,
            field="results.blood.combined_blood_centroid_xy_px",
        )
        if combined_centroid is not None and (
            combined_centroid[0] < 0.0
            or combined_centroid[1] < 0.0
            or combined_centroid[0] > image_width
            or combined_centroid[1] > image_height
        ):
            raise ContractError("combined Blood centroid is outside the image")
        combined_depth = value["combined_blood_centroid_depth_m"]
        if combined_depth is not None and (
            not _is_number(combined_depth) or float(combined_depth) <= 0.0
        ):
            raise ContractError("combined Blood centroid depth is invalid")
        if combined_depth is not None and combined_centroid is None:
            raise ContractError("combined Blood depth lacks a centroid")
        if not detections and (
            combined_centroid is not None or combined_depth is not None
        ):
            raise ContractError("empty Blood result claims a combined centroid")
        calculated_combined_centroid = _rle_union_centroid(
            validated_masks,
            field="results.blood.detections.mask_rle",
        )
        if detections and combined_centroid is None:
            raise ContractError("nonempty Blood result lacks a combined centroid")
        if combined_centroid is not None and (
            calculated_combined_centroid is None
            or any(
                not math.isclose(
                    claimed,
                    calculated,
                    rel_tol=0.0,
                    abs_tol=_BLOOD_CENTROID_TOLERANCE_PX,
                )
                for claimed, calculated in zip(
                    combined_centroid,
                    calculated_combined_centroid,
                    strict=True,
                )
            )
        ):
            raise ContractError(
                "combined Blood centroid disagrees with the validated mask union"
            )
    return tuple(normalized)


def _validate_hand_results(
    value: Any,
    *,
    color_camera_info: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, dict):
        raise ContractError("results.hand must be an object")
    schema = value.get("schema")
    rgbd = schema == "pnu.hand.rgbd.v1"
    expected_keys = {"schema", "executed", "image", "hands"}
    if rgbd:
        expected_keys.add("metric_3d")
    _exact_keys(value, expected_keys, field="results.hand")
    if schema not in {"pnu.hand.2d.v1", "pnu.hand.rgbd.v1"}:
        raise ContractError("unexpected hand result schema")
    if value["executed"] is not True:
        raise ContractError("results.hand.executed must be true")
    if rgbd and not _validate_metric_result(
        value["metric_3d"], field="results.hand.metric_3d"
    ):
        raise ContractError("RGBD hand result must have metric 3-D ready")
    camera = None
    if rgbd:
        if not isinstance(color_camera_info, Mapping):
            raise ContractError("RGBD Hand result requires request color CameraInfo")
        camera = CameraCalibration(
            received_monotonic=0.0,
            stamp_ns=int(color_camera_info.get("stamp_ns", 0)),
            frame_id=str(color_camera_info.get("frame_id", "")),
            payload=dict(color_camera_info),
        )
    image_width, image_height = _validate_image_shape(
        value["image"], field="results.hand.image"
    )
    hands = value["hands"]
    if not isinstance(hands, list) or len(hands) > _MAX_HANDS:
        raise ContractError("results.hand.hands must be a bounded array")
    normalized: list[dict[str, Any]] = []
    indexes: set[int] = set()
    for index, row in enumerate(hands):
        field = f"results.hand.hands[{index}]"
        if not isinstance(row, dict):
            raise ContractError(f"{field} must be an object")
        expected_row_keys = {"hand_index", "handedness", "joints_2d", "kp_scores"}
        if rgbd:
            expected_row_keys |= {"joints_3d", "kp_valid_depth", "palm_6d"}
        _exact_keys(row, expected_row_keys, field=field)
        hand_index = row["hand_index"]
        if (
            not isinstance(hand_index, int)
            or isinstance(hand_index, bool)
            or hand_index < 0
            or hand_index in indexes
        ):
            raise ContractError(f"{field}.hand_index is invalid or duplicated")
        indexes.add(hand_index)
        handedness_raw = row["handedness"]
        if not isinstance(handedness_raw, dict):
            raise ContractError(f"{field}.handedness must be an object")
        _exact_keys(
            handedness_raw,
            {"label", "score"},
            field=f"{field}.handedness",
        )
        handedness_label = str(handedness_raw["label"]).strip().casefold()
        if handedness_label not in {"left", "right", "unknown"}:
            raise ContractError(f"{field}.handedness.label is invalid")
        handedness_score = _validate_confidence(
            handedness_raw["score"], field=f"{field}.handedness.score"
        )
        joints = row["joints_2d"]
        scores = row["kp_scores"]
        if (
            not isinstance(joints, list)
            or not isinstance(scores, list)
            or len(joints) != 21
            or len(scores) != len(joints)
        ):
            raise ContractError(f"{field} joint arrays are inconsistent")
        normalized_joints: list[list[float]] = []
        for joint_index, joint in enumerate(joints):
            if (
                not isinstance(joint, list)
                or len(joint) != 2
                or any(not _is_number(item) for item in joint)
            ):
                raise ContractError(f"{field}.joints_2d[{joint_index}] is invalid")
            normalized_joints.append([float(joint[0]), float(joint[1])])
            if (
                not -float(image_width) <= float(joint[0]) <= 2.0 * image_width
                or not -float(image_height) <= float(joint[1]) <= 2.0 * image_height
            ):
                raise ContractError(
                    f"{field}.joints_2d[{joint_index}] exceeds bounded image space"
                )
        normalized_scores = [
            _validate_confidence(item, field=f"{field}.kp_scores") for item in scores
        ]
        normalized_joints_3d = [[0.0, 0.0, 0.0] for _ in range(21)]
        valid_depth = [False] * 21
        palm = None
        if rgbd:
            raw_joints_3d = row["joints_3d"]
            raw_valid_depth = row["kp_valid_depth"]
            if (
                not isinstance(raw_joints_3d, list)
                or len(raw_joints_3d) != 21
                or not isinstance(raw_valid_depth, list)
                or len(raw_valid_depth) != 21
                or any(not isinstance(item, bool) for item in raw_valid_depth)
            ):
                raise ContractError(f"{field} 3-D joint arrays are inconsistent")
            normalized_joints_3d = []
            valid_depth = list(raw_valid_depth)
            for joint_index, (joint, valid) in enumerate(
                zip(raw_joints_3d, valid_depth, strict=True)
            ):
                point = _optional_vector(
                    joint, length=3, field=f"{field}.joints_3d[{joint_index}]"
                )
                if point is None:
                    raise ContractError(
                        f"{field}.joints_3d[{joint_index}] cannot be null"
                    )
                if valid:
                    if point[2] <= 0.0 or any(abs(item) > 100.0 for item in point):
                        raise ContractError(
                            f"{field}.joints_3d[{joint_index}] is outside camera space"
                        )
                elif any(abs(item) > 1.0e-9 for item in point):
                    raise ContractError(
                        f"{field}.joints_3d[{joint_index}] must be zero when invalid"
                    )
                normalized_joints_3d.append(point)
                if valid:
                    joint_uv = normalized_joints[joint_index]
                    if not (
                        0.0 <= joint_uv[0] < image_width
                        and 0.0 <= joint_uv[1] < image_height
                    ):
                        raise ContractError(
                            f"{field}.kp_valid_depth[{joint_index}] is outside image"
                        )
                    assert camera is not None
                    projected_uv = _project_camera_point(point, camera)
                    if math.hypot(
                        projected_uv[0] - joint_uv[0],
                        projected_uv[1] - joint_uv[1],
                    ) > _HAND_JOINT_REPROJECTION_TOLERANCE_PX:
                        raise ContractError(
                            f"{field}.joints_3d[{joint_index}] does not reproject "
                            "to joints_2d"
                        )
            raw_palm = row["palm_6d"]
            if raw_palm is not None:
                if not isinstance(raw_palm, dict):
                    raise ContractError(f"{field}.palm_6d must be null or object")
                _exact_keys(
                    raw_palm,
                    {"translation", "orientation_xyzw", "rotation_matrix"},
                    field=f"{field}.palm_6d",
                )
                translation = _optional_vector(
                    raw_palm["translation"],
                    length=3,
                    field=f"{field}.palm_6d.translation",
                )
                orientation = _optional_vector(
                    raw_palm["orientation_xyzw"],
                    length=4,
                    field=f"{field}.palm_6d.orientation_xyzw",
                )
                rotation = _optional_vector(
                    raw_palm["rotation_matrix"],
                    length=9,
                    field=f"{field}.palm_6d.rotation_matrix",
                )
                if translation is None or orientation is None or rotation is None:
                    raise ContractError(f"{field}.palm_6d fields cannot be null")
                if translation[2] <= 0.0 or any(
                    abs(item) > 100.0 for item in translation
                ):
                    raise ContractError(f"{field}.palm_6d.translation is invalid")
                quaternion_norm = math.sqrt(sum(item * item for item in orientation))
                if not 0.999 <= quaternion_norm <= 1.001:
                    raise ContractError(f"{field}.palm_6d quaternion is not unit")
                columns = (
                    (rotation[0], rotation[3], rotation[6]),
                    (rotation[1], rotation[4], rotation[7]),
                    (rotation[2], rotation[5], rotation[8]),
                )
                for column in columns:
                    if not 0.995 <= sum(item * item for item in column) <= 1.005:
                        raise ContractError(
                            f"{field}.palm_6d rotation is not normalized"
                        )
                if any(
                    abs(sum(a * b for a, b in zip(columns[left], columns[right])))
                    > 0.005
                    for left, right in ((0, 1), (0, 2), (1, 2))
                ):
                    raise ContractError(f"{field}.palm_6d rotation is not orthogonal")
                determinant = _rotation_determinant(rotation)
                if not 0.995 <= determinant <= 1.005:
                    raise ContractError(
                        f"{field}.palm_6d rotation determinant is not +1"
                    )
                if not all(valid_depth[index] for index in (0, 2, 9, 17)):
                    raise ContractError(f"{field}.palm_6d lacks required valid joints")
                quaternion_rotation = _quaternion_xyzw_to_rotation_matrix(
                    orientation
                )
                quaternion_rotation_flat = tuple(
                    item for matrix_row in quaternion_rotation for item in matrix_row
                )
                if max(
                    abs(left - right)
                    for left, right in zip(
                        rotation, quaternion_rotation_flat, strict=True
                    )
                ) > _HAND_PALM_ROTATION_TOLERANCE:
                    raise ContractError(
                        f"{field}.palm_6d quaternion and rotation matrix disagree"
                    )
                expected_translation, expected_rotation = _palm_frame_v2_from_joints(
                    normalized_joints_3d[0],
                    normalized_joints_3d[2],
                    normalized_joints_3d[9],
                    normalized_joints_3d[17],
                )
                if max(
                    abs(left - right)
                    for left, right in zip(
                        translation, expected_translation, strict=True
                    )
                ) > _HAND_PALM_TRANSLATION_TOLERANCE_M:
                    raise ContractError(
                        f"{field}.palm_6d translation is not (j0+j9)/2"
                    )
                if max(
                    abs(left - right)
                    for left, right in zip(rotation, expected_rotation, strict=True)
                ) > _HAND_PALM_ROTATION_TOLERANCE:
                    raise ContractError(
                        f"{field}.palm_6d rotation disagrees with palm_frame_v2"
                    )
                palm = {
                    "translation": translation,
                    "orientation_xyzw": orientation,
                    "rotation_matrix": rotation,
                }
        normalized.append(
            {
                "hand_index": hand_index,
                "handedness": {
                    "label": handedness_label,
                    "score": handedness_score,
                },
                "joints_2d": normalized_joints,
                "joints_3d": normalized_joints_3d,
                "kp_scores": normalized_scores,
                "kp_valid_depth": valid_depth,
                "palm_6d": palm,
            }
        )
    return tuple(normalized)


def _validate_depth_evidence(
    value: Any,
    *,
    metadata: Mapping[str, Any],
    results: Mapping[str, Any],
    metric_ready: bool,
    expected_rgb_dimensions: tuple[int, int] | None,
) -> None:
    if not isinstance(value, dict):
        raise ContractError("depth_evidence must be an object")
    _exact_keys(
        value,
        {
            "received",
            "decoded",
            "alignment_validated",
            "alignment_id",
            "rgb_frame_id",
            "depth_frame_id",
            "rgb_shape_hw",
            "depth_shape_hw",
            "depth_scale_m_per_unit",
            "depth_scale_validated",
            "valid_pixels",
            "valid_ratio",
        },
        field="depth_evidence",
    )
    for name in ("received", "decoded", "alignment_validated", "depth_scale_validated"):
        if not isinstance(value[name], bool):
            raise ContractError(f"depth_evidence.{name} must be boolean")
    source = metadata.get("source", {})
    depth_source = source.get("depth") if isinstance(source, Mapping) else None
    expected_received = isinstance(depth_source, Mapping)
    if value["received"] is not expected_received:
        raise ContractError("depth_evidence.received disagrees with request")
    rgb_source = source.get("rgb", {}) if isinstance(source, Mapping) else {}
    if value["rgb_frame_id"] != rgb_source.get("frame_id"):
        raise ContractError("depth_evidence RGB frame does not match request")
    expected_depth_frame = depth_source.get("frame_id", "") if expected_received else ""
    if value["depth_frame_id"] != expected_depth_frame:
        raise ContractError("depth_evidence depth frame does not match request")
    alignment = metadata.get("alignment", {})
    expected_alignment = bool(
        isinstance(alignment, Mapping) and alignment.get("validated") is True
    )
    expected_alignment_id = (
        str(alignment.get("id", ""))
        if isinstance(alignment, Mapping) and expected_alignment
        else ""
    )
    if value["alignment_validated"] is not expected_alignment:
        raise ContractError("depth_evidence alignment gate does not match request")
    if value["alignment_id"] != expected_alignment_id:
        raise ContractError("depth_evidence alignment id does not match request")
    expected_scale_validated = metadata.get("depth_scale_validated") is True
    expected_scale = float(metadata.get("depth_scale_m_per_unit", 0.0))
    if value["depth_scale_validated"] is not expected_scale_validated:
        raise ContractError("depth_evidence scale gate does not match request")
    if not _is_number(value["depth_scale_m_per_unit"]) or not math.isclose(
        float(value["depth_scale_m_per_unit"]),
        expected_scale,
        rel_tol=0.0,
        abs_tol=1e-12,
    ):
        raise ContractError("depth_evidence scale does not match request")
    rgb_shape = value["rgb_shape_hw"]
    if (
        not isinstance(rgb_shape, list)
        or len(rgb_shape) != 2
        or any(
            not isinstance(item, int) or isinstance(item, bool) or item <= 0
            for item in rgb_shape
        )
    ):
        raise ContractError("depth_evidence.rgb_shape_hw is invalid")
    if expected_rgb_dimensions is not None and rgb_shape != [
        expected_rgb_dimensions[1],
        expected_rgb_dimensions[0],
    ]:
        raise ContractError(
            "depth_evidence RGB shape disagrees with the request RGB image"
        )
    for name, result in results.items():
        image = result.get("image", {}) if isinstance(result, Mapping) else {}
        if rgb_shape != [image.get("height"), image.get("width")]:
            raise ContractError(f"depth_evidence RGB shape disagrees with {name}")
    depth_shape = value["depth_shape_hw"]
    if depth_shape is not None and (
        not isinstance(depth_shape, list)
        or len(depth_shape) != 2
        or any(
            not isinstance(item, int) or isinstance(item, bool) or item <= 0
            for item in depth_shape
        )
    ):
        raise ContractError("depth_evidence.depth_shape_hw is invalid")
    if value["decoded"] and depth_shape is None:
        raise ContractError("decoded depth evidence has no shape")
    if not value["decoded"] and depth_shape is not None:
        raise ContractError("undecoded depth evidence claims a shape")
    if value["decoded"] and not value["received"]:
        raise ContractError("decoded depth evidence was not received")
    valid_pixels = value["valid_pixels"]
    if (
        not isinstance(valid_pixels, int)
        or isinstance(valid_pixels, bool)
        or valid_pixels < 0
    ):
        raise ContractError("depth_evidence.valid_pixels is invalid")
    valid_ratio = value["valid_ratio"]
    if not _is_number(valid_ratio) or not 0.0 <= float(valid_ratio) <= 1.0:
        raise ContractError("depth_evidence.valid_ratio is invalid")
    if depth_shape is None:
        if valid_pixels != 0 or float(valid_ratio) != 0.0:
            raise ContractError("undecoded depth evidence claims valid pixels")
    else:
        pixel_count = int(depth_shape[0]) * int(depth_shape[1])
        if valid_pixels > pixel_count or not math.isclose(
            float(valid_ratio), valid_pixels / pixel_count, rel_tol=0.0, abs_tol=1e-6
        ):
            raise ContractError("depth_evidence valid pixel ratio is inconsistent")
    if metric_ready and (
        not value["decoded"]
        or not value["alignment_validated"]
        or not value["depth_scale_validated"]
        or depth_shape != rgb_shape
    ):
        raise ContractError("metric 3-D lacks aligned decoded depth evidence")


def validate_worker_response(
    payload: Any,
    *,
    metadata: Mapping[str, Any],
    pinned_model_digests: Mapping[str, str],
    request_started_unix_ms: int,
    received_unix_ms: int,
    max_clock_skew_ms: int = 1_000,
    expected_tool_support_plane_config_version: str = "",
    expected_tool_support_plane_normal: Sequence[float] | None = None,
    tool_support_plane_normal_tolerance_deg: float = (
        _DEFAULT_SUPPORT_PLANE_NORMAL_TOLERANCE_DEG
    ),
    expected_rgb_dimensions: Sequence[int] | None = None,
) -> ValidatedWorkerResponse:
    if not isinstance(payload, dict):
        raise ContractError("worker response must be a JSON object")
    expected_response_keys = {
        "schema",
        "request_id",
        "generated_unix_ms",
        "source",
        "accepted_algorithms",
        "upstream",
        "models",
        "latency_ms",
        "results",
        "metric_3d",
        "depth_received",
    }
    if "depth_evidence" in payload:
        expected_response_keys.add("depth_evidence")
    _exact_keys(payload, expected_response_keys, field="response")
    if payload["schema"] != RESPONSE_SCHEMA:
        raise ContractError("unexpected worker response schema")
    if payload["request_id"] != metadata.get("request_id"):
        raise ContractError("worker response request_id does not match")
    if payload["source"] != metadata.get("source"):
        raise ContractError("worker response source identity does not match")
    upstream = payload["upstream"]
    if not isinstance(upstream, dict):
        raise ContractError("response.upstream must be an object")
    _exact_keys(
        upstream,
        {"repository", "commit"},
        field="response.upstream",
    )
    if (
        upstream["repository"] != EXPECTED_UPSTREAM_REPOSITORY
        or upstream["commit"] != EXPECTED_UPSTREAM_COMMIT
    ):
        raise ContractError("worker response upstream commit does not match the pin")
    expected_depth_received = "depth" in metadata.get("source", {})
    if payload["depth_received"] is not expected_depth_received:
        raise ContractError("worker depth_received does not match the request")
    generated_unix_ms = payload["generated_unix_ms"]
    deadline_unix_ms = metadata.get("deadline_unix_ms")
    if (
        not isinstance(generated_unix_ms, int)
        or isinstance(generated_unix_ms, bool)
        or not isinstance(request_started_unix_ms, int)
        or not isinstance(deadline_unix_ms, int)
        or generated_unix_ms < request_started_unix_ms - max_clock_skew_ms
        or generated_unix_ms > deadline_unix_ms
        or generated_unix_ms > received_unix_ms + max_clock_skew_ms
    ):
        raise ContractError("worker response completion time is stale or invalid")
    if received_unix_ms > deadline_unix_ms:
        raise ContractError("worker response arrived after the request deadline")

    requested = normalize_algorithms(metadata.get("requested_algorithms", []))
    accepted_raw = payload["accepted_algorithms"]
    if not isinstance(accepted_raw, list):
        raise ContractError("accepted_algorithms must be an array")
    accepted = normalize_algorithms(accepted_raw)
    if accepted != requested or list(accepted) != accepted_raw:
        raise ContractError("worker did not execute exactly the requested algorithms")

    digests = _validate_model_records(
        payload["models"], accepted, field="models", executed=True
    )
    if digests != dict(pinned_model_digests):
        raise ContractError(
            "worker response model digests changed after capabilities pin"
        )

    latency = payload["latency_ms"]
    if not isinstance(latency, dict):
        raise ContractError("latency_ms must be an object")
    _exact_keys(
        latency,
        {"decode", "total", *accepted},
        field="latency_ms",
    )
    if any(not _is_number(item) or float(item) < 0.0 for item in latency.values()):
        raise ContractError("latency_ms values must be finite and nonnegative")

    results = payload["results"]
    if not isinstance(results, dict) or set(results) != set(accepted):
        raise ContractError("results must contain exactly the accepted algorithms")
    normalized_rgb_dimensions = _normalize_expected_rgb_dimensions(
        expected_rgb_dimensions,
        metadata=metadata,
    )
    if normalized_rgb_dimensions is not None:
        for algorithm in accepted:
            result = results[algorithm]
            if not isinstance(result, Mapping):
                raise ContractError(f"results.{algorithm} must be an object")
            actual_dimensions = _validate_image_shape(
                result.get("image"),
                field=f"results.{algorithm}.image",
            )
            if actual_dimensions != normalized_rgb_dimensions:
                raise ContractError(
                    f"results.{algorithm}.image disagrees with the request RGB image"
                )
    expected_support_plane_version = str(
        expected_tool_support_plane_config_version or ""
    ).strip()
    if len(expected_support_plane_version) > 240 or any(
        character in expected_support_plane_version for character in ("\n", "\r")
    ):
        raise ContractError("expected Tool support-plane version is invalid")
    normalized_support_plane_normal = (
        parse_support_plane_normal(expected_tool_support_plane_normal)
        if expected_tool_support_plane_normal is not None
        else None
    )
    if (
        not _is_number(tool_support_plane_normal_tolerance_deg)
        or not 0.0 < float(tool_support_plane_normal_tolerance_deg)
        <= _MAX_SUPPORT_PLANE_NORMAL_TOLERANCE_DEG
    ):
        raise ValueError(
            "tool_support_plane_normal_tolerance_deg must be in (0, 10]"
        )
    tool_detections: tuple[dict[str, Any], ...] = ()
    blood_detections: tuple[dict[str, Any], ...] = ()
    hands: tuple[dict[str, Any], ...] = ()
    tool_support_plane_diagnostics: dict[str, Any] | None = None
    for algorithm in accepted:
        if algorithm == "tool":
            alignment = metadata.get("alignment")
            expected_calibration_version = (
                str(alignment.get("id", ""))
                if isinstance(alignment, Mapping)
                else ""
            )
            tool_detections = _validate_tool_results(
                results[algorithm],
                color_camera_info=(
                    metadata.get("color_camera_info")
                    if isinstance(metadata.get("color_camera_info"), Mapping)
                    else None
                ),
                expected_calibration_version=expected_calibration_version,
                expected_support_plane_config_version=(
                    expected_support_plane_version
                ),
                expected_support_plane_normal=normalized_support_plane_normal,
                support_plane_normal_tolerance_deg=float(
                    tool_support_plane_normal_tolerance_deg
                ),
            )
            if results[algorithm].get("schema") == "pnu.tool.rgbd.v1":
                color_camera_info = metadata.get("color_camera_info")
                if not isinstance(color_camera_info, Mapping):
                    raise ContractError(
                        "RGBD Tool result requires request color CameraInfo"
                    )
                tool_support_plane_diagnostics = _validate_support_plane_diagnostics(
                    results[algorithm]["support_plane_diagnostics"],
                    support_plane_validated=results[algorithm][
                        "support_plane_validated"
                    ],
                    expected_camera_info_sha256=_camera_info_sha256(
                        color_camera_info
                    ),
                    support_plane_config_version=str(
                        results[algorithm]["support_plane_config_version"]
                    ),
                    expected_support_plane_config_version=(
                        expected_support_plane_version
                    ),
                )
        elif algorithm == "blood":
            blood_detections = _validate_blood_results(results[algorithm])
        elif algorithm == "hand":
            hands = _validate_hand_results(
                results[algorithm],
                color_camera_info=(
                    metadata.get("color_camera_info")
                    if isinstance(metadata.get("color_camera_info"), Mapping)
                    else None
                ),
            )

    tool_result = results.get("tool")
    if (
        isinstance(tool_result, Mapping)
        and tool_result.get("schema") == "pnu.tool.rgbd.v1"
    ):
        actual_support_plane_version = str(
            tool_result.get("support_plane_config_version", "")
        )
        if (
            tool_result.get("support_plane_validated") is True
            and not expected_support_plane_version
        ):
            raise ContractError(
                "validated Tool support plane is not pinned by Taskplanner"
            )
        if (
            expected_support_plane_version
            and actual_support_plane_version != expected_support_plane_version
        ):
            raise ContractError(
                "Tool support-plane version does not match the Taskplanner pin"
            )

    metric = payload["metric_3d"]
    if not isinstance(metric, dict):
        raise ContractError("metric_3d must be an object")
    _exact_keys(metric, {"ready", "reasons"}, field="metric_3d")
    reasons = metric["reasons"]
    ready = metric["ready"]
    if (
        not isinstance(ready, bool)
        or not isinstance(reasons, list)
        or len(reasons) > 64
        or any(
            not isinstance(item, str) or not item.strip() or len(item) > 160
            for item in reasons
        )
        or (ready and reasons)
        or (not ready and not reasons)
    ):
        raise ContractError("metric_3d readiness and reasons are inconsistent")
    source = metadata.get("source", {})
    alignment = metadata.get("alignment", {})
    metric_request_eligible = bool(
        isinstance(source, Mapping)
        and isinstance(source.get("depth"), Mapping)
        and source["depth"].get("aligned") is True
        and source["depth"].get("frame_id") == source.get("rgb", {}).get("frame_id")
        and isinstance(alignment, Mapping)
        and alignment.get("validated") is True
        and isinstance(alignment.get("id"), str)
        and bool(alignment.get("id", "").strip())
        and metadata.get("depth_scale_validated") is True
        and _is_number(metadata.get("depth_scale_m_per_unit"))
        and float(metadata["depth_scale_m_per_unit"]) > 0.0
        and isinstance(metadata.get("color_camera_info"), Mapping)
        and isinstance(metadata.get("depth_camera_info"), Mapping)
        and all(
            metadata["color_camera_info"].get(key)
            == metadata["depth_camera_info"].get(key)
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
        )
    )
    rgbd_schemas = {
        "tool": "pnu.tool.rgbd.v1",
        "blood": "pnu.blood.rgbd.v1",
        "hand": "pnu.hand.rgbd.v1",
    }
    result_rgbd_ready = all(
        results[name].get("schema") == rgbd_schemas[name]
        and results[name].get("metric_3d", {}).get("ready") is True
        for name in accepted
    )
    if ready and not metric_request_eligible:
        raise ContractError("worker claimed metric 3-D for an ineligible request")
    if ready != result_rgbd_ready:
        raise ContractError("top-level and per-algorithm metric 3-D disagree")
    if ready and "depth_evidence" not in payload:
        raise ContractError("metric 3-D response lacks depth_evidence")
    if "depth_evidence" in payload:
        _validate_depth_evidence(
            payload["depth_evidence"],
            metadata=metadata,
            results=results,
            metric_ready=ready,
            expected_rgb_dimensions=normalized_rgb_dimensions,
        )

    return ValidatedWorkerResponse(
        payload=dict(payload),
        model_digests=digests,
        tool_detections=tool_detections,
        blood_detections=blood_detections,
        hands=hands,
        metric_3d_ready=ready,
        tool_support_plane_diagnostics=tool_support_plane_diagnostics,
    )


_HAND_SKELETON = (
    (0, 1),
    (1, 2),
    (2, 3),
    (3, 4),
    (0, 5),
    (5, 6),
    (6, 7),
    (7, 8),
    (0, 9),
    (9, 10),
    (10, 11),
    (11, 12),
    (0, 13),
    (13, 14),
    (14, 15),
    (15, 16),
    (0, 17),
    (17, 18),
    (18, 19),
    (19, 20),
)
_TOOL_OVERLAY_COLORS = (
    (40, 196, 255),
    (64, 224, 163),
    (255, 196, 61),
    (176, 124, 255),
    (255, 128, 184),
    (94, 234, 241),
    (138, 218, 84),
    (255, 154, 75),
)
_BLOOD_OVERLAY_COLOR = (255, 68, 88)
_HAND_OVERLAY_COLORS = ((92, 255, 166), (255, 211, 92))


def _overlay_font(width: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    size = max(11, min(24, round(width / 85.0)))
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size=size)
    except OSError:
        return ImageFont.load_default()


def _draw_overlay_label(
    draw: ImageDraw.ImageDraw,
    *,
    xy: tuple[int, int],
    text: str,
    color: tuple[int, int, int],
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    image_size: tuple[int, int],
) -> None:
    width, height = image_size
    if width <= 0 or height <= 0:
        return
    text = text[:96]
    text_box = draw.textbbox((0, 0), text, font=font)
    text_width = max(1, int(text_box[2] - text_box[0]))
    text_height = max(1, int(text_box[3] - text_box[1]))
    padding = 3
    x = max(0, min(int(xy[0]), max(0, width - text_width - 2 * padding)))
    y = max(0, min(int(xy[1]), max(0, height - text_height - 2 * padding)))
    draw.rectangle(
        (x, y, x + text_width + 2 * padding, y + text_height + 2 * padding),
        fill=(*color, 238),
    )
    draw.text(
        (x + padding, y + padding - text_box[1]),
        text,
        fill=(5, 8, 12, 255),
        font=font,
    )


def _draw_coco_rle_overlay(
    draw: ImageDraw.ImageDraw,
    *,
    rle: Mapping[str, Any],
    expected_size: tuple[int, int],
    color: tuple[int, int, int],
    max_runs: int,
    max_segments: int,
) -> tuple[int, int, bool]:
    """Paint COCO's column-major RLE without allocating a mask per instance."""

    height, width = (int(item) for item in rle["size"])
    if (width, height) != expected_size:
        raise ContractError("overlay RLE size does not match the RGB image")
    raw_counts = rle["counts"]
    if len(raw_counts) > max_runs:
        return 0, 0, False
    counts = _rle_counts(rle, field="overlay.mask_rle")
    if len(counts) > max_runs:
        return 0, 0, False

    cursor = 0
    segments = 0
    for run_index, run_length in enumerate(counts):
        run_end = cursor + int(run_length)
        if run_index % 2 == 1:
            while cursor < run_end:
                if segments >= max_segments:
                    return len(counts), segments, False
                x = cursor // height
                column_end = min(run_end, (x + 1) * height)
                y0 = cursor % height
                y1 = (column_end - 1) % height
                draw.line((x, y0, x, y1), fill=(*color, 78), width=1)
                segments += 1
                cursor = column_end
        cursor = run_end
    return len(counts), segments, True


def _validated_overlay_dimensions(
    frame: BinaryFrame,
    validated: ValidatedWorkerResponse,
) -> tuple[int, int]:
    source = validated.payload.get("source")
    source_rgb = source.get("rgb") if isinstance(source, Mapping) else None
    if not isinstance(source_rgb, Mapping) or any(
        (
            source_rgb.get("stamp_ns") != frame.stamp_ns,
            source_rgb.get("frame_id") != frame.frame_id,
            source_rgb.get("format") != frame.format,
        )
    ):
        raise ContractError("overlay RGB identity does not match the accepted response")
    width, height = _rgb_dimensions(frame)
    accepted = normalize_algorithms(validated.payload.get("accepted_algorithms", []))
    results = validated.payload.get("results")
    if not isinstance(results, Mapping):
        raise ContractError("validated overlay response has no results")
    for algorithm in accepted:
        result = results.get(algorithm)
        if not isinstance(result, Mapping):
            raise ContractError(f"validated overlay result is missing {algorithm}")
        result_width, result_height = _validate_image_shape(
            result.get("image"), field=f"results.{algorithm}.image"
        )
        if (result_width, result_height) != (width, height):
            raise ContractError(
                f"results.{algorithm}.image does not match the exact RGB payload"
            )
    return width, height


def build_pnu_debug_overlay(
    *,
    frame: BinaryFrame,
    validated: ValidatedWorkerResponse,
    max_pixels: int = _DEFAULT_OVERLAY_MAX_PIXELS,
    max_instances_per_algorithm: int = _MAX_OVERLAY_INSTANCES_PER_ALGORITHM,
    max_rle_runs: int = _MAX_OVERLAY_RLE_RUNS,
    max_mask_segments: int = _MAX_OVERLAY_MASK_SEGMENTS,
) -> RenderedDebugOverlay:
    """Render the accepted Tool/Blood/Hand evidence as a transparent WebP.

    Operational status is intentionally excluded because this shared overlay
    may also be composited into a RealVLM input.  Debug UI status comes from
    the exact-stamp diagnostics and health topics instead.
    """

    if (
        not isinstance(max_pixels, int)
        or max_pixels <= 0
        or not isinstance(max_instances_per_algorithm, int)
        or max_instances_per_algorithm <= 0
        or not isinstance(max_rle_runs, int)
        or max_rle_runs <= 0
        or not isinstance(max_mask_segments, int)
        or max_mask_segments <= 0
    ):
        raise ValueError("overlay resource bounds must be positive integers")
    started = time.perf_counter()
    width, height = _validated_overlay_dimensions(frame, validated)
    if width * height > max_pixels:
        raise ContractError(
            f"overlay image has {width * height} pixels, exceeding {max_pixels}"
        )

    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay, "RGBA")
    font = _overlay_font(width)
    line_width = max(2, min(5, round(width / 500.0)))
    point_radius = max(2, min(6, round(width / 350.0)))
    remaining_runs = max_rle_runs
    remaining_segments = max_mask_segments
    truncated = False

    tool_rows = sorted(
        validated.tool_detections,
        key=lambda row: (int(row["canonical_class_id"]), int(row["instance_id"])),
    )
    blood_rows = sorted(
        validated.blood_detections,
        key=lambda row: int(row["instance_id"]),
    )
    hand_rows = sorted(validated.hands, key=lambda row: int(row["hand_index"]))
    drawn_tools = tool_rows[:max_instances_per_algorithm]
    drawn_blood = blood_rows[:max_instances_per_algorithm]
    drawn_hands = hand_rows[:max_instances_per_algorithm]
    truncated = truncated or any(
        len(rows) > max_instances_per_algorithm
        for rows in (tool_rows, blood_rows, hand_rows)
    )

    for row in drawn_tools:
        if remaining_runs <= 0 or remaining_segments <= 0:
            truncated = True
            break
        color = _TOOL_OVERLAY_COLORS[
            (int(row["canonical_class_id"]) - 1) % len(_TOOL_OVERLAY_COLORS)
        ]
        runs, segments, complete = _draw_coco_rle_overlay(
            draw,
            rle=row["mask_rle"],
            expected_size=(width, height),
            color=color,
            max_runs=remaining_runs,
            max_segments=remaining_segments,
        )
        remaining_runs -= runs
        remaining_segments -= segments
        truncated = truncated or not complete

    for row in drawn_blood:
        if remaining_runs <= 0 or remaining_segments <= 0:
            truncated = True
            break
        runs, segments, complete = _draw_coco_rle_overlay(
            draw,
            rle=row["mask_rle"],
            expected_size=(width, height),
            color=_BLOOD_OVERLAY_COLOR,
            max_runs=remaining_runs,
            max_segments=remaining_segments,
        )
        remaining_runs -= runs
        remaining_segments -= segments
        truncated = truncated or not complete

    for row in drawn_tools:
        color = _TOOL_OVERLAY_COLORS[
            (int(row["canonical_class_id"]) - 1) % len(_TOOL_OVERLAY_COLORS)
        ]
        x0, y0, x1, y1 = (round(item) for item in row["bbox_xyxy_px"])
        draw.rectangle((x0, y0, x1, y1), outline=(*color, 255), width=line_width)
        observation = row.get("observation")
        depth_m = (
            observation.get("observation_point_depth_m")
            if isinstance(observation, Mapping)
            else None
        )
        label = f"{row['class_name']} {float(row['confidence']):.2f}"
        if _is_number(depth_m) and float(depth_m) > 0.0:
            label += f" z={float(depth_m):.3f}m"
        _draw_overlay_label(
            draw,
            xy=(x0, max(0, y0 - 24)),
            text=label,
            color=color,
            font=font,
            image_size=(width, height),
        )
        point = (
            observation.get("observation_point_uv_px")
            if isinstance(observation, Mapping)
            else None
        )
        if isinstance(point, Sequence) and len(point) == 2:
            px, py = (round(float(item)) for item in point)
            draw.ellipse(
                (
                    px - point_radius,
                    py - point_radius,
                    px + point_radius,
                    py + point_radius,
                ),
                fill=(255, 255, 255, 255),
                outline=(*color, 255),
                width=max(1, line_width - 1),
            )

    for row in drawn_blood:
        x0, y0, x1, y1 = (round(item) for item in row["bbox_xyxy_px"])
        draw.rectangle(
            (x0, y0, x1, y1),
            outline=(*_BLOOD_OVERLAY_COLOR, 255),
            width=line_width,
        )
        label = f"blood {float(row['confidence']):.2f}"
        depth_m = row.get("centroid_depth_m")
        if _is_number(depth_m) and float(depth_m) > 0.0:
            label += f" z={float(depth_m):.3f}m"
        _draw_overlay_label(
            draw,
            xy=(x0, max(0, y0 - 24)),
            text=label,
            color=_BLOOD_OVERLAY_COLOR,
            font=font,
            image_size=(width, height),
        )
        cx, cy = (round(float(item)) for item in row["centroid_xy_px"])
        draw.line(
            (cx - 2 * point_radius, cy, cx + 2 * point_radius, cy),
            fill=(*_BLOOD_OVERLAY_COLOR, 255),
            width=line_width,
        )
        draw.line(
            (cx, cy - 2 * point_radius, cx, cy + 2 * point_radius),
            fill=(*_BLOOD_OVERLAY_COLOR, 255),
            width=line_width,
        )

    rendered_hand_count = 0
    for hand_position, row in enumerate(drawn_hands):
        color = _HAND_OVERLAY_COLORS[hand_position % len(_HAND_OVERLAY_COLORS)]
        joints = row["joints_2d"]
        scores = row["kp_scores"]
        visible = [float(score) >= 0.2 for score in scores]
        for start_index, end_index in _HAND_SKELETON:
            if not (visible[start_index] and visible[end_index]):
                continue
            start = tuple(round(float(item)) for item in joints[start_index])
            end = tuple(round(float(item)) for item in joints[end_index])
            draw.line((*start, *end), fill=(*color, 230), width=line_width)
        visible_points: list[tuple[int, int]] = []
        for joint_index, joint in enumerate(joints):
            if not visible[joint_index]:
                continue
            x, y = (round(float(item)) for item in joint)
            visible_points.append((x, y))
            valid_depth = bool(row["kp_valid_depth"][joint_index])
            draw.ellipse(
                (
                    x - point_radius,
                    y - point_radius,
                    x + point_radius,
                    y + point_radius,
                ),
                fill=(*color, 255 if valid_depth else 100),
                outline=(*color, 255),
                width=1,
            )
        if visible_points:
            rendered_hand_count += 1
            label_x = min(item[0] for item in visible_points)
            label_y = max(0, min(item[1] for item in visible_points) - 24)
            handedness = row["handedness"]
            label = (
                f"{str(handedness['label']).title()} hand "
                f"{float(handedness['score']):.2f}"
            )
            palm = row.get("palm_6d")
            if isinstance(palm, Mapping):
                translation = palm.get("translation")
                if isinstance(translation, Sequence) and len(translation) == 3:
                    label += f" z={float(translation[2]):.3f}m"
            _draw_overlay_label(
                draw,
                xy=(label_x, label_y),
                text=label,
                color=color,
                font=font,
                image_size=(width, height),
            )

    encoded = BytesIO()
    try:
        overlay.save(
            encoded,
            format="WEBP",
            lossless=True,
            method=0,
            exact=True,
        )
    except (KeyError, OSError, ValueError) as exc:
        raise ContractError("transparent lossless WebP overlay encode failed") from exc
    overlay_bytes = encoded.getvalue()
    if not overlay_bytes or len(overlay_bytes) > _MAX_OVERLAY_WEBP_BYTES:
        raise ContractError("transparent WebP overlay is empty or oversized")

    message = CompressedImage()
    message.header.stamp.sec = frame.stamp_ns // 1_000_000_000
    message.header.stamp.nanosec = frame.stamp_ns % 1_000_000_000
    message.header.frame_id = frame.frame_id
    message.format = "webp"
    message.data = overlay_bytes
    return RenderedDebugOverlay(
        message=message,
        render_encode_latency_ms=round(
            max(0.0, time.perf_counter() - started) * 1000.0, 3
        ),
        drawn_tool_count=len(drawn_tools),
        drawn_blood_count=len(drawn_blood),
        drawn_hand_count=rendered_hand_count,
        truncated=truncated,
    )


_POSE_AXIS_COLORS_RGBA = {
    # Match the upstream pnu_surgical_tool visualization convention after
    # converting its BGR constants to Pillow's RGBA order.
    "X": (255, 0, 0, 255),
    "Y": (0, 210, 0, 255),
    "Z": (0, 80, 255, 255),
}
_POSITION_ONLY_COLOR_RGBA = (255, 171, 64, 255)


def _quaternion_xyzw_to_rotation_matrix(
    quaternion_xyzw: Sequence[Any],
) -> tuple[tuple[float, float, float], ...]:
    if (
        not isinstance(quaternion_xyzw, Sequence)
        or isinstance(quaternion_xyzw, (str, bytes))
        or len(quaternion_xyzw) != 4
        or any(not _is_number(item) for item in quaternion_xyzw)
    ):
        raise ContractError("pose overlay quaternion must contain four finite values")
    x, y, z, w = (float(item) for item in quaternion_xyzw)
    norm = math.sqrt(x * x + y * y + z * z + w * w)
    if not 0.999 <= norm <= 1.001:
        raise ContractError("pose overlay quaternion must be unit length")
    x, y, z, w = (item / norm for item in (x, y, z, w))
    return (
        (
            1.0 - 2.0 * (y * y + z * z),
            2.0 * (x * y - z * w),
            2.0 * (x * z + y * w),
        ),
        (
            2.0 * (x * y + z * w),
            1.0 - 2.0 * (x * x + z * z),
            2.0 * (y * z - x * w),
        ),
        (
            2.0 * (x * z - y * w),
            2.0 * (y * z + x * w),
            1.0 - 2.0 * (x * x + y * y),
        ),
    )


def _distort_normalized_point(
    x: float,
    y: float,
    *,
    distortion_model: str,
    distortion: Sequence[Any],
) -> tuple[float, float]:
    """Apply the ROS CameraInfo distortion model without a cv2 dependency."""

    if (
        not _is_number(x)
        or not _is_number(y)
        or abs(float(x)) > 10_000.0
        or abs(float(y)) > 10_000.0
    ):
        raise ContractError("normalized pose projection is outside resource bounds")
    if isinstance(distortion, (str, bytes)) or any(
        not _is_number(item) for item in distortion
    ):
        raise ContractError("pose overlay distortion coefficients must be finite")
    coefficients = [float(item) for item in distortion]
    model = str(distortion_model).strip().casefold()
    x_value = float(x)
    y_value = float(y)
    if not coefficients:
        if model not in {"plumb_bob", "rational_polynomial", "equidistant"}:
            raise ContractError(
                f"pose overlay does not support distortion model {distortion_model!r}"
            )
        return x_value, y_value

    try:
        if model in {"plumb_bob", "rational_polynomial"}:
            expected_lengths = {4, 5} if model == "plumb_bob" else {8}
            if len(coefficients) not in expected_lengths:
                raise ContractError(
                    f"{model} pose projection received {len(coefficients)} coefficients"
                )
            padded = coefficients + [0.0] * (8 - len(coefficients))
            k1, k2, p1, p2, k3, k4, k5, k6 = padded[:8]
            radius_2 = x_value * x_value + y_value * y_value
            radius_4 = radius_2 * radius_2
            radius_6 = radius_4 * radius_2
            numerator = 1.0 + k1 * radius_2 + k2 * radius_4 + k3 * radius_6
            denominator = 1.0
            if model == "rational_polynomial":
                denominator += k4 * radius_2 + k5 * radius_4 + k6 * radius_6
            if not math.isfinite(denominator) or abs(denominator) < 1e-12:
                raise ContractError("pose projection radial denominator is singular")
            radial = numerator / denominator
            distorted_x = (
                x_value * radial
                + 2.0 * p1 * x_value * y_value
                + p2 * (radius_2 + 2.0 * x_value * x_value)
            )
            distorted_y = (
                y_value * radial
                + p1 * (radius_2 + 2.0 * y_value * y_value)
                + 2.0 * p2 * x_value * y_value
            )
        elif model == "equidistant":
            if len(coefficients) != 4:
                raise ContractError(
                    "equidistant pose projection requires four coefficients"
                )
            radius = math.hypot(x_value, y_value)
            if radius < 1e-12:
                return x_value, y_value
            theta = math.atan(radius)
            theta_2 = theta * theta
            k1, k2, k3, k4 = coefficients
            theta_distorted = theta * (
                1.0
                + k1 * theta_2
                + k2 * theta_2**2
                + k3 * theta_2**3
                + k4 * theta_2**4
            )
            scale = theta_distorted / radius
            distorted_x = x_value * scale
            distorted_y = y_value * scale
        else:
            raise ContractError(
                f"pose overlay does not support distortion model {distortion_model!r}"
            )
    except OverflowError as exc:
        raise ContractError("pose overlay distortion arithmetic overflowed") from exc
    if not (_is_number(distorted_x) and _is_number(distorted_y)):
        raise ContractError("pose overlay distortion produced non-finite coordinates")
    return float(distorted_x), float(distorted_y)


def _project_camera_point(
    point_m: Sequence[Any],
    camera: CameraCalibration,
) -> tuple[float, float]:
    """Project one color-camera-frame metric point through CameraInfo K/D."""

    if (
        isinstance(point_m, (str, bytes))
        or len(point_m) != 3
        or any(not _is_number(item) for item in point_m)
    ):
        raise ContractError("pose overlay position must contain three finite values")
    point_x, point_y, point_z = (float(item) for item in point_m)
    if point_z <= 1e-6 or any(abs(item) > 100.0 for item in (point_x, point_y, point_z)):
        raise ContractError("pose overlay position is outside bounded camera space")
    payload = camera.payload
    camera_matrix = payload.get("k")
    distortion = payload.get("d")
    if (
        not isinstance(camera_matrix, Sequence)
        or isinstance(camera_matrix, (str, bytes))
        or len(camera_matrix) != 9
        or any(not _is_number(item) for item in camera_matrix)
        or not isinstance(distortion, Sequence)
        or isinstance(distortion, (str, bytes))
    ):
        raise ContractError("pose overlay CameraInfo K/D is malformed")
    normalized_x, normalized_y = point_x / point_z, point_y / point_z
    distorted_x, distorted_y = _distort_normalized_point(
        normalized_x,
        normalized_y,
        distortion_model=str(payload.get("distortion_model", "")),
        distortion=distortion,
    )
    matrix = [float(item) for item in camera_matrix]
    homogeneous_u = matrix[0] * distorted_x + matrix[1] * distorted_y + matrix[2]
    homogeneous_v = matrix[3] * distorted_x + matrix[4] * distorted_y + matrix[5]
    homogeneous_w = matrix[6] * distorted_x + matrix[7] * distorted_y + matrix[8]
    if not _is_number(homogeneous_w) or abs(float(homogeneous_w)) < 1e-12:
        raise ContractError("pose overlay CameraInfo K is singular")
    pixel_u = homogeneous_u / homogeneous_w
    pixel_v = homogeneous_v / homogeneous_w
    if (
        not _is_number(pixel_u)
        or not _is_number(pixel_v)
        or abs(float(pixel_u)) > 1e12
        or abs(float(pixel_v)) > 1e12
    ):
        raise ContractError("pose overlay projection produced unbounded pixels")
    return float(pixel_u), float(pixel_v)


def _clip_axis_endpoint_to_image(
    origin: tuple[float, float],
    endpoint: tuple[float, float],
    *,
    width: int,
    height: int,
) -> tuple[float, float] | None:
    origin_x, origin_y = origin
    endpoint_x, endpoint_y = endpoint
    if not (0.0 <= origin_x <= width - 1 and 0.0 <= origin_y <= height - 1):
        return None
    delta_x = endpoint_x - origin_x
    delta_y = endpoint_y - origin_y
    scale = 1.0
    if delta_x > 0.0:
        scale = min(scale, (width - 1 - origin_x) / delta_x)
    elif delta_x < 0.0:
        scale = min(scale, (0.0 - origin_x) / delta_x)
    if delta_y > 0.0:
        scale = min(scale, (height - 1 - origin_y) / delta_y)
    elif delta_y < 0.0:
        scale = min(scale, (0.0 - origin_y) / delta_y)
    if not _is_number(scale) or float(scale) < 0.0:
        return None
    clipped = (
        origin_x + delta_x * float(scale),
        origin_y + delta_y * float(scale),
    )
    if math.hypot(clipped[0] - origin_x, clipped[1] - origin_y) < 1.0:
        return None
    return clipped


def _draw_pose_arrow(
    draw: ImageDraw.ImageDraw,
    *,
    origin: tuple[float, float],
    endpoint: tuple[float, float],
    color: tuple[int, int, int, int],
    line_width: int,
) -> None:
    draw.line((*origin, *endpoint), fill=color, width=line_width)
    delta_x = endpoint[0] - origin[0]
    delta_y = endpoint[1] - origin[1]
    length = math.hypot(delta_x, delta_y)
    if length < 1.0:
        return
    unit_x, unit_y = delta_x / length, delta_y / length
    head_length = max(3.0, min(12.0, length * 0.25))
    head_width = max(2.0, min(7.0, head_length * 0.55))
    base_x = endpoint[0] - unit_x * head_length
    base_y = endpoint[1] - unit_y * head_length
    perpendicular_x, perpendicular_y = -unit_y, unit_x
    draw.polygon(
        (
            endpoint,
            (
                base_x + perpendicular_x * head_width,
                base_y + perpendicular_y * head_width,
            ),
            (
                base_x - perpendicular_x * head_width,
                base_y - perpendicular_y * head_width,
            ),
        ),
        fill=color,
    )


def build_pnu_pose_overlay(
    *,
    frame: BinaryFrame,
    pose_array: ToolPoseArray,
    color_camera_info: CameraCalibration | None,
    axis_length_m: float = _DEFAULT_POSE_AXIS_LENGTH_M,
    max_pixels: int = _DEFAULT_OVERLAY_MAX_PIXELS,
    max_instances: int = _MAX_POSE_OVERLAY_INSTANCES,
) -> RenderedPoseOverlay:
    """Render ToolPoseArray evidence into a dedicated transparent WebP layer.

    +X/+Y/+Z are the quaternion rotation-matrix columns in the color-camera
    frame, matching upstream.  Axes are drawn only for explicitly valid
    orientations.  A position-only pose gets an amber ring/cross and label,
    never a synthetic orientation.
    """

    if (
        not _is_number(axis_length_m)
        or not _MIN_POSE_AXIS_LENGTH_M
        <= float(axis_length_m)
        <= _MAX_POSE_AXIS_LENGTH_M
    ):
        raise ValueError(
            "axis_length_m must be finite and in "
            f"[{_MIN_POSE_AXIS_LENGTH_M}, {_MAX_POSE_AXIS_LENGTH_M}]"
        )
    if (
        not isinstance(max_pixels, int)
        or isinstance(max_pixels, bool)
        or max_pixels <= 0
        or not isinstance(max_instances, int)
        or isinstance(max_instances, bool)
        or max_instances <= 0
    ):
        raise ValueError("pose overlay resource bounds must be positive integers")
    if (
        _message_stamp_ns(pose_array) != frame.stamp_ns
        or str(pose_array.header.frame_id) != frame.frame_id
    ):
        raise ContractError("pose overlay ToolPoseArray identity does not match RGB")

    started = time.perf_counter()
    width, height = _rgb_dimensions(frame)
    if width * height > max_pixels:
        raise ContractError(
            f"pose overlay image has {width * height} pixels, exceeding {max_pixels}"
        )
    tools = list(pose_array.tools)
    selected_tools = tools[:max_instances]
    truncated = len(tools) > max_instances
    requires_projection = any(bool(item.position_valid) for item in selected_tools)
    if requires_projection:
        if color_camera_info is None:
            raise ContractError("pose overlay requires color CameraInfo for metric poses")
        camera_width = int(color_camera_info.payload.get("width", 0))
        camera_height = int(color_camera_info.payload.get("height", 0))
        if (
            color_camera_info.frame_id != frame.frame_id
            or (camera_width, camera_height) != (width, height)
        ):
            raise ContractError("pose overlay CameraInfo does not match the RGB frame")

    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay, "RGBA")
    font = _overlay_font(width)
    line_width = max(2, min(5, round(width / 500.0)))
    marker_radius = max(4, min(10, round(width / 180.0)))
    drawn_axes = 0
    drawn_position_only = 0
    for tool in selected_tools:
        position_valid = tool.position_valid
        orientation_valid = tool.orientation_valid
        if not isinstance(position_valid, bool) or not isinstance(
            orientation_valid, bool
        ):
            raise ContractError("pose overlay validity fields must be boolean")
        if orientation_valid and not position_valid:
            raise ContractError("pose overlay orientation requires a valid position")
        if not position_valid:
            continue
        position = (
            float(tool.pose.position.x),
            float(tool.pose.position.y),
            float(tool.pose.position.z),
        )
        if any(not _is_number(item) for item in position):
            raise ContractError("pose overlay position contains non-finite values")
        assert color_camera_info is not None
        origin = _project_camera_point(position, color_camera_info)
        if not (0.0 <= origin[0] < width and 0.0 <= origin[1] < height):
            # A valid tool pose is allowed to fall outside this view, but it
            # must not create huge/off-canvas Pillow coordinates.
            continue

        class_name = str(tool.class_name).strip()[:64] or "tool"
        if not orientation_valid:
            center_x, center_y = (round(item) for item in origin)
            radius = marker_radius
            draw.ellipse(
                (
                    center_x - radius,
                    center_y - radius,
                    center_x + radius,
                    center_y + radius,
                ),
                outline=_POSITION_ONLY_COLOR_RGBA,
                width=line_width,
            )
            draw.line(
                (center_x - radius, center_y, center_x + radius, center_y),
                fill=_POSITION_ONLY_COLOR_RGBA,
                width=line_width,
            )
            draw.line(
                (center_x, center_y - radius, center_x, center_y + radius),
                fill=_POSITION_ONLY_COLOR_RGBA,
                width=line_width,
            )
            _draw_overlay_label(
                draw,
                xy=(center_x + radius + 2, center_y + radius + 2),
                text=f"{class_name} position-only",
                color=_POSITION_ONLY_COLOR_RGBA[:3],
                font=font,
                image_size=(width, height),
            )
            drawn_position_only += 1
            continue

        quaternion = (
            float(tool.pose.orientation.x),
            float(tool.pose.orientation.y),
            float(tool.pose.orientation.z),
            float(tool.pose.orientation.w),
        )
        rotation = _quaternion_xyzw_to_rotation_matrix(quaternion)
        axis_lines = 0
        for axis_index, axis_name in enumerate(("X", "Y", "Z")):
            endpoint_m = tuple(
                position[row_index]
                + float(axis_length_m) * rotation[row_index][axis_index]
                for row_index in range(3)
            )
            try:
                projected_endpoint = _project_camera_point(
                    endpoint_m,
                    color_camera_info,
                )
            except ContractError:
                # An axis endpoint can cross the camera plane for a very near
                # but otherwise bounded pose.  Omitting that axis is safer than
                # inventing a direction or aborting the accepted typed result.
                continue
            clipped_endpoint = _clip_axis_endpoint_to_image(
                origin,
                projected_endpoint,
                width=width,
                height=height,
            )
            if clipped_endpoint is None:
                continue
            color = _POSE_AXIS_COLORS_RGBA[axis_name]
            _draw_pose_arrow(
                draw,
                origin=origin,
                endpoint=clipped_endpoint,
                color=color,
                line_width=line_width,
            )
            label_x = max(0, min(width - 1, round(clipped_endpoint[0]) + 3))
            label_y = max(0, min(height - 1, round(clipped_endpoint[1]) - 3))
            draw.text((label_x, label_y), axis_name, fill=color, font=font)
            axis_lines += 1
        if axis_lines > 0:
            center_x, center_y = (round(item) for item in origin)
            origin_radius = max(2, line_width)
            draw.ellipse(
                (
                    center_x - origin_radius,
                    center_y - origin_radius,
                    center_x + origin_radius,
                    center_y + origin_radius,
                ),
                fill=(255, 255, 255, 255),
            )
            drawn_axes += 1

    encoded = BytesIO()
    try:
        overlay.save(
            encoded,
            format="WEBP",
            lossless=True,
            method=0,
            exact=True,
        )
    except (KeyError, OSError, ValueError) as exc:
        raise ContractError("transparent pose WebP overlay encode failed") from exc
    overlay_bytes = encoded.getvalue()
    if not overlay_bytes or len(overlay_bytes) > _MAX_OVERLAY_WEBP_BYTES:
        raise ContractError("transparent pose WebP overlay is empty or oversized")

    message = CompressedImage()
    message.header.stamp.sec = frame.stamp_ns // 1_000_000_000
    message.header.stamp.nanosec = frame.stamp_ns % 1_000_000_000
    message.header.frame_id = frame.frame_id
    message.format = "webp"
    message.data = overlay_bytes
    return RenderedPoseOverlay(
        message=message,
        render_encode_latency_ms=round(
            max(0.0, time.perf_counter() - started) * 1000.0,
            3,
        ),
        drawn_axis_count=drawn_axes,
        drawn_position_only_count=drawn_position_only,
        truncated=truncated,
    )


def build_cam4_semantics(
    detections: Sequence[Mapping[str, Any]],
    *,
    source_stamp_sec: float,
    inference_latency_ms: float,
) -> dict[str, Any]:
    """Project PNU tool instances onto the existing Taskplanner VLM contract."""

    grouped: dict[str, list[float]] = defaultdict(list)
    for row in detections:
        canonical_class_id = row.get("canonical_class_id")
        if not isinstance(canonical_class_id, int) or isinstance(
            canonical_class_id, bool
        ):
            continue
        compatibility = _PNU_TOOL_COMPATIBILITY_NAMES.get(canonical_class_id)
        if compatibility is None:
            continue
        provider_name, name = compatibility
        # Require both sides of the frozen provider ontology pair.  This keeps
        # a corrupt/mismatched id-name record visible on the typed evidence
        # topics while preventing it from mutating legacy planner state.
        if str(row.get("class_name", "")).strip() != provider_name:
            continue
        confidence = row.get("confidence")
        if name and _is_number(confidence) and 0.0 <= float(confidence) <= 1.0:
            grouped[name].append(float(confidence))
    tools = [
        {
            "name": name,
            "count": len(confidences),
            "max_confidence": round(max(confidences), 4),
            "mean_confidence": round(sum(confidences) / len(confidences), 4),
        }
        for name, confidences in sorted(grouped.items())
    ]
    return {
        "schema": SEMANTICS_SCHEMA,
        # Keep this source identifier until all existing VLM consumers gain a
        # provider-neutral schema.  The provider field removes ambiguity.
        "source": "cam4_rfdetr_small",
        "provider": "pnu_hand_blood",
        "source_stamp_sec": round(float(source_stamp_sec), 6),
        "ground_truth": False,
        "cam4_image_forwarded_to_vlm": False,
        "tools": tools,
        # PNU 2-D hand landmarks do not classify a surgical tool request.
        "tool_request": {
            "state": "uncertain",
            "requested": None,
            "confidence": 0.0,
            "detector_class": "",
        },
        "inference_latency_ms": round(max(0.0, float(inference_latency_ms)), 3),
    }


def build_blood_semantics(
    detections: Sequence[Mapping[str, Any]],
    *,
    source_stamp_ns: int,
    source_stamp_sec: float,
    frame_id: str,
    metric_3d_ready: bool,
    combined_centroid_xy_px: Sequence[float] | None = None,
    combined_centroid_depth_m: float | None = None,
) -> dict[str, Any]:
    """Publish Blood's metric centroid evidence without inventing a suction pose."""

    if (
        not isinstance(source_stamp_ns, int)
        or isinstance(source_stamp_ns, bool)
        or source_stamp_ns <= 0
    ):
        raise ValueError("source_stamp_ns must be a positive integer")
    instances = []
    for row in detections:
        depth = row.get("centroid_depth_m")
        instances.append(
            {
                "instance_id": int(row["instance_id"]),
                "confidence": round(float(row["confidence"]), 6),
                "bbox_xyxy_px": [round(float(item), 3) for item in row["bbox_xyxy_px"]],
                "centroid_xy_px": [
                    round(float(item), 3) for item in row["centroid_xy_px"]
                ],
                "centroid_depth_valid": bool(depth is not None and metric_3d_ready),
                "centroid_depth_m": (
                    round(float(depth), 6)
                    if depth is not None and metric_3d_ready
                    else None
                ),
            }
        )
    combined_valid = bool(
        metric_3d_ready
        and combined_centroid_xy_px is not None
        and combined_centroid_depth_m is not None
    )
    return {
        "schema": BLOOD_SEMANTICS_SCHEMA,
        "source": "cam4_pnu_blood",
        "provider": "pnu_hand_blood",
        # JSON numbers cannot carry every ROS nanosecond stamp losslessly.
        # Publish the exact integer as a bounded decimal string for UI joins.
        "source_stamp_ns": str(source_stamp_ns),
        "source_stamp_sec": round(float(source_stamp_sec), 6),
        "frame_id": str(frame_id),
        "ground_truth": False,
        "metric_3d_ready": bool(metric_3d_ready),
        "detections": instances,
        "combined_centroid_xy_px": (
            [round(float(item), 3) for item in combined_centroid_xy_px]
            if combined_centroid_xy_px is not None
            else None
        ),
        "combined_centroid_depth_valid": combined_valid,
        "combined_centroid_depth_m": (
            round(float(combined_centroid_depth_m), 6) if combined_valid else None
        ),
    }


def _safe_token(path_value: str) -> str:
    path_text = str(path_value or "").strip()
    if not path_text:
        return ""
    path = Path(path_text)
    token = path.read_text(encoding="utf-8").strip()
    if not token or "\n" in token or "\r" in token or len(token) > 4096:
        raise ValueError("api_token_file does not contain one bounded token")
    return token


def _set_source_header(message: Any, frame: BinaryFrame) -> None:
    message.header.stamp.sec = frame.stamp_ns // 1_000_000_000
    message.header.stamp.nanosec = frame.stamp_ns % 1_000_000_000
    message.header.frame_id = frame.frame_id


def build_tool_ros_messages(
    *,
    frame: BinaryFrame,
    sequence: int,
    detections: Sequence[Mapping[str, Any]],
    tool_result: Mapping[str, Any],
    model_version: str,
) -> tuple[ToolPoseArray, ToolObservation2DArray]:
    observation_id = f"cam4:{frame.stamp_ns}"
    pose_array = ToolPoseArray()
    observation_array = ToolObservation2DArray()
    _set_source_header(pose_array, frame)
    _set_source_header(observation_array, frame)
    pose_array.sequence = int(sequence)
    pose_array.schema_version = _POSE_SCHEMA
    pose_array.observation_id = observation_id
    pose_array.source_view = "cam4"
    pose_array.model_version = str(model_version)
    metric_3d_result = tool_result.get("schema") == "pnu.tool.rgbd.v1"
    if metric_3d_result:
        pose_array.ontology_version = str(tool_result["ontology_version"])
        pose_array.calibration_version = str(tool_result["calibration_version"])
        pose_array.pose_convention_version = str(
            tool_result["pose_convention_version"]
        )
    else:
        # A 2-D fallback still belongs to the frozen Tool ontology and pose
        # message contract.  The explicit marker says that no metric
        # calibration was applied; empty provenance would make the Debug
        # consumer silently discard otherwise useful INVALID pose evidence.
        pose_array.ontology_version = _TOOL_ONTOLOGY_VERSION
        pose_array.calibration_version = _TOOL_METRIC_3D_UNAVAILABLE_CALIBRATION
        pose_array.pose_convention_version = _TOOL_POSE_CONVENTION_VERSION
    observation_array.sequence = int(sequence)
    observation_array.schema_version = _OBSERVATION_SCHEMA
    observation_array.observation_id = observation_id
    observation_array.view = "cam4"
    image = tool_result["image"]
    observation_array.image_width = int(image["width"])
    observation_array.image_height = int(image["height"])
    observation_array.model_version = str(model_version)
    observation_array.ontology_version = pose_array.ontology_version

    pose_messages: list[ToolPose] = []
    observation_messages: list[ToolObservation2D] = []
    pose_mode_values = {
        "INVALID": ToolPose.POSE_MODE_INVALID,
        "POSITION_3D_ONLY": ToolPose.POSE_MODE_POSITION_3D_ONLY,
        "PLANAR_4DOF_WITH_NORMAL_PRIOR": ToolPose.POSE_MODE_PLANAR_4DOF_WITH_NORMAL_PRIOR,
        "FULL_6D": ToolPose.POSE_MODE_FULL_6D,
        "AMBIGUOUS": ToolPose.POSE_MODE_AMBIGUOUS,
    }
    validity_values = {
        "INVALID": ToolPose.VALIDITY_INVALID,
        "VALID": ToolPose.VALIDITY_VALID,
        "DEGRADED": ToolPose.VALIDITY_DEGRADED,
        "STALE": ToolPose.VALIDITY_STALE,
    }
    for row in detections:
        observation = row["observation"]
        evidence = ToolObservation2D()
        evidence.frame_local_instance_id = int(row["instance_id"])
        evidence.canonical_class_id = int(row["canonical_class_id"])
        evidence.model_class_index = int(row["model_class_index"])
        evidence.class_name = str(row["class_name"])
        evidence.class_confidence = float(row["confidence"])
        evidence.segmentation_confidence = float(row["confidence"])
        evidence.bbox_xyxy_px = [float(item) for item in row["bbox_xyxy_px"]]
        evidence.mask_bbox_xyxy_px = [
            float(item) for item in observation["mask_bbox_xyxy_px"]
        ]
        evidence.mask_area_px = int(observation["mask_area_px"])
        point = observation["observation_point_uv_px"]
        if point is not None:
            evidence.observation_point_uv_px = [float(item) for item in point]
        evidence.observation_point_valid = bool(observation["observation_point_valid"])
        evidence.observation_point_inside_mask = bool(
            observation["observation_point_inside_mask"]
        )
        evidence.observation_point_depth_valid = bool(
            observation["observation_point_depth_valid"]
        )
        evidence.observation_point_depth_m = float(
            observation["observation_point_depth_m"] or 0.0
        )
        evidence.observation_point_selection_mode = str(
            observation["observation_point_selection_mode"]
        )
        evidence.observation_point_boundary_clearance_px = float(
            observation["observation_point_boundary_clearance_px"]
        )
        evidence.mask_encoding = ToolObservation2D.MASK_ENCODING_COCO_RLE_COMPRESSED
        evidence.mask_height = int(row["mask_rle"]["size"][0])
        evidence.mask_width = int(row["mask_rle"]["size"][1])
        evidence.mask_counts = str(row["mask_counts"])
        observation_messages.append(evidence)

        message = ToolPose()
        message.frame_local_instance_id = evidence.frame_local_instance_id
        message.canonical_class_id = evidence.canonical_class_id
        message.model_class_index = evidence.model_class_index
        message.class_name = evidence.class_name
        message.class_confidence = evidence.class_confidence
        pose = row["pose"]
        if pose is None:
            message.pose_mode = ToolPose.POSE_MODE_INVALID
            message.validity = ToolPose.VALIDITY_INVALID
            message.status_flags = ["METRIC_3D_UNAVAILABLE"]
            message.invalid_reason = "2d_only_metric_pose_unavailable"
        else:
            position = pose["position_m"]
            orientation = pose["orientation_xyzw"]
            if position is not None:
                (
                    message.pose.position.x,
                    message.pose.position.y,
                    message.pose.position.z,
                ) = (float(item) for item in position)
            if orientation is not None:
                (
                    message.pose.orientation.x,
                    message.pose.orientation.y,
                    message.pose.orientation.z,
                    message.pose.orientation.w,
                ) = (float(item) for item in orientation)
            message.pose_mode = pose_mode_values[str(pose["pose_mode"])]
            message.position_valid = bool(pose["position_valid"])
            message.orientation_valid = bool(pose["orientation_valid"])
            message.dof_observed = [bool(item) for item in pose["dof_observed"]]
            message.observation_point_definition = str(
                pose["observation_point_definition"]
            )
            message.axis_definition = str(pose["axis_definition"])
            message.symmetry_type = str(pose["symmetry_type"])
            message.endpoint_sign_confidence = float(pose["endpoint_sign_confidence"])
            message.valid_depth_ratio = float(pose["valid_depth_ratio"])
            message.pose_point_count = int(pose["pose_point_count"])
            message.axis_anisotropy = float(pose["axis_anisotropy"])
            message.support_plane_inlier_ratio = float(
                pose["support_plane_inlier_ratio"]
            )
            message.support_plane_residual_p95_m = float(
                pose["support_plane_residual_p95_m"]
            )
            message.pose_confidence = float(pose["pose_confidence"])
            message.pose_confidence_calibrated = bool(
                pose["pose_confidence_calibrated"]
            )
            message.validity = validity_values[str(pose["validity"])]
            message.status_flags = [str(item) for item in pose["status_flags"]]
            message.invalid_reason = str(pose["invalid_reason"])
        if point is not None:
            message.observation_point_uv_px = [float(item) for item in point]
        message.observation_point_inside_mask = bool(
            observation["observation_point_inside_mask"]
        )
        message.observation_point_depth_valid = bool(
            observation["observation_point_depth_valid"]
        )
        message.observation_point_selection_mode = str(
            observation["observation_point_selection_mode"]
        )
        message.observation_point_boundary_clearance_px = float(
            observation["observation_point_boundary_clearance_px"]
        )
        pose_messages.append(message)
    pose_array.tools = pose_messages
    observation_array.instances = observation_messages
    return pose_array, observation_array


def build_hand_keypoints_message(
    *,
    frame: BinaryFrame,
    hands: Sequence[Mapping[str, Any]],
    metric_3d_ready: bool,
) -> HandKeypoints:
    message = HandKeypoints()
    _set_source_header(message, frame)
    message.depth_source = "real" if metric_3d_ready else "2d_only"
    hand_messages: list[Hand] = []
    for row in hands:
        item = Hand()
        item.hand_index = int(row["hand_index"])
        handedness = row["handedness"]
        label = str(handedness["label"])
        item.has_handedness = label in {"left", "right"}
        if item.has_handedness:
            item.handedness_label = label.capitalize()
            item.handedness_score = float(handedness["score"])
        item.joints_2d = [
            Point2D(u=float(point[0]), v=float(point[1])) for point in row["joints_2d"]
        ]
        for target, point in zip(item.joints_3d, row["joints_3d"], strict=True):
            target.x, target.y, target.z = (float(value) for value in point)
        item.kp_scores = [float(value) for value in row["kp_scores"]]
        item.kp_valid_depth = [bool(value) for value in row["kp_valid_depth"]]
        palm = row["palm_6d"]
        item.has_palm_6d = palm is not None
        if palm is not None:
            palm_message = PalmPose6D()
            (
                palm_message.translation.x,
                palm_message.translation.y,
                palm_message.translation.z,
            ) = (float(value) for value in palm["translation"])
            (
                palm_message.orientation.x,
                palm_message.orientation.y,
                palm_message.orientation.z,
                palm_message.orientation.w,
            ) = (float(value) for value in palm["orientation_xyzw"])
            palm_message.rotation_matrix = [
                float(value) for value in palm["rotation_matrix"]
            ]
            item.palm_6d = palm_message
        hand_messages.append(item)
    message.hands = hand_messages
    return message


class PNUPerceptionBridgeNode(Node):
    """Live VIPLab CAM4 to local-or-remote PNU worker adapter."""

    def __init__(self) -> None:
        super().__init__("pnu_perception_bridge")
        self._service_url = validate_service_url(
            str(self.declare_parameter("service_url", "http://127.0.0.1:8020").value)
        )
        self._rgb_input_topic = str(
            self.declare_parameter(
                "rgb_input_topic",
                "/synced/cam_4/color/image_raw/compressed",
            ).value
        )
        self._color_camera_info_topic = str(
            self.declare_parameter(
                "color_camera_info_topic",
                "/synced/cam_4/color/camera_info",
            ).value
        )
        self._depth_input_topic = str(
            self.declare_parameter(
                "depth_input_topic",
                "/synced/cam_4/aligned_depth_to_color/image_raw/compressedDepth",
            ).value
        )
        self._depth_camera_info_topic = str(
            self.declare_parameter(
                "depth_camera_info_topic",
                "/synced/cam_4/aligned_depth_to_color/camera_info",
            ).value
        )
        self._cam4_semantics_topic = str(
            self.declare_parameter(
                "cam4_semantics_topic",
                "/surgery/perception/cam4/semantics/json",
            ).value
        )
        self._cam4_mayo_observation_topic = str(
            self.declare_parameter(
                "cam4_mayo_observation_topic",
                "/surgery/perception/cam4/mayo_tool_observations",
            ).value
        )
        self._cam4_tool_pose_topic = str(
            self.declare_parameter(
                "cam4_tool_pose_topic",
                "/surgery/perception/cam4/tool_poses",
            ).value
        )
        self._cam4_tool_observations_topic = str(
            self.declare_parameter(
                "cam4_tool_observations_topic",
                "/surgery/perception/cam4/observations",
            ).value
        )
        self._cam4_hand_keypoints_topic = str(
            self.declare_parameter(
                "cam4_hand_keypoints_topic",
                "/surgery/perception/cam4/hand_keypoints",
            ).value
        )
        self._cam4_blood_semantics_topic = str(
            self.declare_parameter(
                "cam4_blood_semantics_topic",
                "/surgery/perception/cam4/blood_semantics/json",
            ).value
        )
        self._diagnostics_topic = str(
            self.declare_parameter(
                "diagnostics_topic",
                "/surgery/perception/rfdetr/diagnostics/json",
            ).value
        )
        self._health_topic = str(
            self.declare_parameter(
                "health_topic", "/surgery/perception/rfdetr/health"
            ).value
        )
        self._cam4_overlay_topic = str(
            self.declare_parameter(
                "cam4_overlay_topic",
                "/surgery/images/cam4/detection_overlay/compressed",
            ).value
        )
        self._cam4_pose_overlay_topic = str(
            self.declare_parameter(
                "cam4_pose_overlay_topic",
                "/surgery/images/cam4/pose_overlay/compressed",
            ).value
        )
        self._requested_algorithms = normalize_algorithms(
            self.declare_parameter(
                "requested_algorithms", list(SUPPORTED_ALGORITHMS)
            ).value
        )
        self._expected_model_digests = parse_expected_model_digests(
            str(self.declare_parameter("expected_model_digests_json", "{}").value),
            requested_algorithms=self._requested_algorithms,
        )
        self._expected_tool_support_plane_config_version = str(
            self.declare_parameter(
                "expected_tool_support_plane_config_version",
                "",
            ).value
        ).strip()
        if len(self._expected_tool_support_plane_config_version) > 240 or any(
            character in self._expected_tool_support_plane_config_version
            for character in ("\n", "\r")
        ):
            raise ValueError(
                "expected_tool_support_plane_config_version is invalid"
            )
        expected_plane_normal_raw = str(
            self.declare_parameter(
                "expected_tool_support_plane_normal",
                os.environ.get("PNU_TOOL_SUPPORT_PLANE_NORMAL", ""),
            ).value
        ).strip()
        self._expected_tool_support_plane_normal = parse_support_plane_normal(
            expected_plane_normal_raw,
            allow_empty=True,
        )
        if (
            self._expected_tool_support_plane_config_version
            and self._expected_tool_support_plane_normal is None
        ):
            raise ValueError(
                "a pinned Tool support-plane version requires its expected normal"
            )
        self._tool_support_plane_normal_tolerance_deg = float(
            self.declare_parameter(
                "tool_support_plane_normal_tolerance_deg",
                _DEFAULT_SUPPORT_PLANE_NORMAL_TOLERANCE_DEG,
            ).value
        )
        if (
            not math.isfinite(self._tool_support_plane_normal_tolerance_deg)
            or not 0.0 < self._tool_support_plane_normal_tolerance_deg
            <= _MAX_SUPPORT_PLANE_NORMAL_TOLERANCE_DEG
        ):
            raise ValueError(
                "tool_support_plane_normal_tolerance_deg must be in (0, 10]"
            )
        self._max_depth_rgb_skew_sec = max(
            0.0,
            float(self.declare_parameter("max_depth_rgb_skew_sec", 0.05).value),
        )
        self._max_camera_info_skew_sec = max(
            0.0,
            float(self.declare_parameter("max_camera_info_skew_sec", 0.1).value),
        )
        self._max_source_age_sec = max(
            0.1,
            float(self.declare_parameter("max_source_age_sec", 2.0).value),
        )
        self._request_timeout_sec = max(
            0.1,
            float(self.declare_parameter("request_timeout_sec", 2.0).value),
        )
        self._max_rate_hz = max(
            0.1,
            float(self.declare_parameter("max_rate_hz", 15.0).value),
        )
        self._overlay_enabled = bool(
            self.declare_parameter("overlay_enabled", True).value
        )
        self._overlay_max_rate_hz = float(
            self.declare_parameter(
                "overlay_max_rate_hz", _DEFAULT_OVERLAY_MAX_RATE_HZ
            ).value
        )
        self._overlay_max_pixels = int(
            self.declare_parameter(
                "overlay_max_pixels", _DEFAULT_OVERLAY_MAX_PIXELS
            ).value
        )
        self._pose_overlay_enabled = bool(
            self.declare_parameter("pose_overlay_enabled", True).value
        )
        self._pose_overlay_max_rate_hz = float(
            self.declare_parameter(
                "pose_overlay_max_rate_hz",
                _DEFAULT_POSE_OVERLAY_MAX_RATE_HZ,
            ).value
        )
        self._pose_overlay_max_pixels = int(
            self.declare_parameter(
                "pose_overlay_max_pixels",
                _DEFAULT_OVERLAY_MAX_PIXELS,
            ).value
        )
        self._pose_axis_length_m = float(
            self.declare_parameter(
                "pose_axis_length_m",
                _DEFAULT_POSE_AXIS_LENGTH_M,
            ).value
        )
        if (
            not math.isfinite(self._overlay_max_rate_hz)
            or not 0.1 <= self._overlay_max_rate_hz <= 30.0
        ):
            raise ValueError("overlay_max_rate_hz must be in [0.1, 30]")
        if not 1 <= self._overlay_max_pixels <= 16_000_000:
            raise ValueError("overlay_max_pixels must be in [1, 16000000]")
        if (
            not math.isfinite(self._pose_overlay_max_rate_hz)
            or not 0.1 <= self._pose_overlay_max_rate_hz <= 30.0
        ):
            raise ValueError("pose_overlay_max_rate_hz must be in [0.1, 30]")
        if not 1 <= self._pose_overlay_max_pixels <= 16_000_000:
            raise ValueError("pose_overlay_max_pixels must be in [1, 16000000]")
        if not (
            math.isfinite(self._pose_axis_length_m)
            and _MIN_POSE_AXIS_LENGTH_M
            <= self._pose_axis_length_m
            <= _MAX_POSE_AXIS_LENGTH_M
        ):
            raise ValueError(
                "pose_axis_length_m must be in "
                f"[{_MIN_POSE_AXIS_LENGTH_M}, {_MAX_POSE_AXIS_LENGTH_M}]"
            )
        self._depth_scale_m_per_unit = float(
            self.declare_parameter("depth_scale_m_per_unit", 0.0).value
        )
        self._depth_scale_validated = bool(
            self.declare_parameter("depth_scale_validated", False).value
        )
        self._depth_alignment_validated = bool(
            self.declare_parameter("depth_alignment_validated", False).value
        )
        self._depth_alignment_id = str(
            self.declare_parameter("depth_alignment_id", "").value
        ).strip()
        if (
            not math.isfinite(self._depth_scale_m_per_unit)
            or self._depth_scale_m_per_unit < 0.0
            or self._depth_scale_m_per_unit > 1.0
            or (self._depth_scale_validated and self._depth_scale_m_per_unit <= 0.0)
        ):
            raise ValueError("depth_scale_m_per_unit must be in (0, 1] when validated")
        if self._depth_alignment_validated and not self._depth_alignment_id:
            raise ValueError(
                "depth_alignment_validated requires a nonempty depth_alignment_id"
            )
        if len(self._depth_alignment_id) > 128:
            raise ValueError("depth_alignment_id exceeds 128 characters")
        self._max_worker_clock_skew_ms = max(
            0,
            int(
                float(self.declare_parameter("max_worker_clock_skew_sec", 1.0).value)
                * 1000.0
            ),
        )
        self._worker_health_max_age_ms = max(
            100,
            int(
                float(self.declare_parameter("worker_health_max_age_sec", 5.0).value)
                * 1000.0
            ),
        )
        self._enabled = bool(self.declare_parameter("enabled", True).value)
        allow_insecure_remote_http = bool(
            self.declare_parameter("allow_insecure_remote_http", False).value
        )
        self._transport_mode = resolve_endpoint_transport_mode(
            self._service_url,
            allow_insecure_remote_http=allow_insecure_remote_http,
        )
        allow_unauthenticated_remote = bool(
            self.declare_parameter("allow_unauthenticated_remote", False).value
        )
        token = _safe_token(str(self.declare_parameter("api_token_file", "").value))
        self._worker_auth_mode, self._auth_mode = resolve_endpoint_auth_mode(
            self._service_url,
            has_token=bool(token),
            allow_unauthenticated_remote=allow_unauthenticated_remote,
        )

        self._session = requests.Session()
        self._session.trust_env = False
        if token:
            self._session.headers.update({"Authorization": f"Bearer {token}"})
        self._session.headers.update(
            {
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "User-Agent": "taskplanner-pnu-bridge/1",
            }
        )

        input_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=20,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.VOLATILE,
        )
        semantics_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=10,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.VOLATILE,
        )
        diagnostics_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=10,
            reliability=ReliabilityPolicy.BEST_EFFORT,
            durability=DurabilityPolicy.VOLATILE,
        )
        health_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        overlay_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=2,
            reliability=ReliabilityPolicy.BEST_EFFORT,
            durability=DurabilityPolicy.VOLATILE,
        )

        self._semantics_pub = self.create_publisher(
            String, self._cam4_semantics_topic, semantics_qos
        )
        self._mayo_pub = self.create_publisher(
            ToolObservation, self._cam4_mayo_observation_topic, 30
        )
        self._tool_pose_pub = self.create_publisher(
            ToolPoseArray, self._cam4_tool_pose_topic, semantics_qos
        )
        self._tool_observations_pub = self.create_publisher(
            ToolObservation2DArray,
            self._cam4_tool_observations_topic,
            semantics_qos,
        )
        self._hand_keypoints_pub = self.create_publisher(
            HandKeypoints, self._cam4_hand_keypoints_topic, semantics_qos
        )
        self._blood_semantics_pub = self.create_publisher(
            String, self._cam4_blood_semantics_topic, semantics_qos
        )
        self._diagnostics_pub = self.create_publisher(
            String, self._diagnostics_topic, diagnostics_qos
        )
        self._health_pub = self.create_publisher(String, self._health_topic, health_qos)
        # Render locally from the strictly validated structured response.  The
        # exact same transparent overlay contract is produced for loopback and
        # LAN workers without trusting worker-supplied image bytes.
        self._overlay_pub = self.create_publisher(
            CompressedImage, self._cam4_overlay_topic, overlay_qos
        )
        self._pose_overlay_pub = self.create_publisher(
            CompressedImage,
            self._cam4_pose_overlay_topic,
            overlay_qos,
        )

        self.create_subscription(
            CompressedImage, self._rgb_input_topic, self._on_rgb, input_qos
        )
        self.create_subscription(
            CompressedImage, self._depth_input_topic, self._on_depth, input_qos
        )
        self.create_subscription(
            CameraInfo,
            self._color_camera_info_topic,
            self._on_color_camera_info,
            input_qos,
        )
        self.create_subscription(
            CameraInfo,
            self._depth_camera_info_topic,
            self._on_depth_camera_info,
            input_qos,
        )

        self._condition = threading.Condition()
        self._pending_rgb: BinaryFrame | None = None
        self._depth_frames: deque[BinaryFrame] = deque(maxlen=32)
        self._color_infos: deque[CameraCalibration] = deque(maxlen=32)
        self._depth_infos: deque[CameraCalibration] = deque(maxlen=32)
        self._running = True
        self._dropped_frames = 0
        self._sequence = 0
        self._last_request_started_monotonic = 0.0
        self._last_published_stamp_ns = 0
        self._last_attempted_stamp_ns = 0
        self._last_success_monotonic = 0.0
        self._last_success_source_stamp_ns = 0
        self._last_detection_count = 0
        self._last_overlay_published_monotonic = 0.0
        self._last_overlay_visible = False
        self._last_pose_overlay_published_monotonic = 0.0
        self._last_pose_overlay_signature = (0, 0)
        self._last_success_rgb: BinaryFrame | None = None
        self._pinned_model_digests: dict[str, str] | None = None
        self._mayo_tracker = Cam4MayoPlacementTracker()
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="pnu-perception-bridge-worker",
            daemon=True,
        )
        self._worker.start()
        self._health_timer = self.create_timer(1.0, self._publish_idle_health)

    def _record_input_error(self, kind: str, exc: Exception) -> None:
        self._publish_health(
            connected=False,
            status="input_error",
            source_stamp_ns=0,
            detection_count=0,
            error_code=f"INVALID_{kind.upper()}",
            error_message=str(exc),
        )
        self.get_logger().warning(
            f"PNU bridge rejected {kind}: {exc}", throttle_duration_sec=3.0
        )

    def _on_rgb(self, message: CompressedImage) -> None:
        try:
            frame = buffered_image(message)
            _rgb_content_type(frame.format)
        except (ContractError, ValueError) as exc:
            self._record_input_error("rgb", exc)
            return
        with self._condition:
            if not self._enabled:
                return
            if frame.stamp_ns <= max(
                self._last_published_stamp_ns, self._last_attempted_stamp_ns
            ):
                return
            if self._pending_rgb is not None:
                self._dropped_frames += 1
            self._pending_rgb = frame
            self._condition.notify()

    def _on_depth(self, message: CompressedImage) -> None:
        try:
            frame = buffered_image(message)
            if "compresseddepth" not in frame.format.casefold():
                raise ContractError("depth input is not ROS compressedDepth")
        except (ContractError, ValueError) as exc:
            self._record_input_error("depth", exc)
            return
        with self._condition:
            if self._enabled:
                self._depth_frames.append(frame)
                self._condition.notify_all()

    def _on_color_camera_info(self, message: CameraInfo) -> None:
        try:
            info = buffered_camera_info(message)
        except ContractError as exc:
            self._record_input_error("color_camera_info", exc)
            return
        with self._condition:
            if self._enabled:
                self._color_infos.append(info)
                self._condition.notify_all()

    def _on_depth_camera_info(self, message: CameraInfo) -> None:
        try:
            info = buffered_camera_info(message)
        except ContractError as exc:
            self._record_input_error("depth_camera_info", exc)
            return
        with self._condition:
            if self._enabled:
                self._depth_infos.append(info)
                self._condition.notify_all()

    def _worker_loop(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(
                    lambda: (
                        not self._running
                        or (self._enabled and self._pending_rgb is not None)
                    )
                )
                if not self._running:
                    return

                # Apply the rate gate before claiming the one pending slot.
                # While the gate is closed, callbacks may replace that slot,
                # so the eventual request always uses the freshest RGB frame.
                minimum_period = 1.0 / self._max_rate_hz
                remaining = minimum_period - (
                    time.monotonic() - self._last_request_started_monotonic
                )
                if remaining > 0.0:
                    self._condition.wait(timeout=remaining)
                    continue

                # RGB can be delivered just before its exact-stamp aligned
                # depth and CameraInfo callbacks.  Wait for only this frame
                # set, for a fixed and tightly bounded interval.  A newer RGB
                # still replaces the pending slot; the deadline is not reset.
                sync_deadline = time.monotonic() + _EXACT_RGBD_ORDERING_GRACE_SEC
                while self._running and self._enabled:
                    rgb = self._pending_rgb
                    if rgb is None or has_exact_rgbd_frame_set(
                        rgb_stamp_ns=rgb.stamp_ns,
                        depth_frames=self._depth_frames,
                        color_infos=self._color_infos,
                        depth_infos=self._depth_infos,
                    ):
                        break
                    remaining = sync_deadline - time.monotonic()
                    if remaining <= 0.0:
                        break
                    self._condition.wait(timeout=remaining)
                if not self._running:
                    return
                if not self._enabled:
                    continue

                rgb = self._pending_rgb
                self._pending_rgb = None
                if rgb is None:
                    continue
                self._last_attempted_stamp_ns = max(
                    self._last_attempted_stamp_ns, rgb.stamp_ns
                )
                depth = (
                    closest_by_stamp(
                        self._depth_frames,
                        rgb.stamp_ns,
                        self._max_depth_rgb_skew_sec,
                    )
                    if rgb is not None
                    else None
                )
                color_info = (
                    closest_by_stamp(
                        self._color_infos,
                        rgb.stamp_ns,
                        self._max_camera_info_skew_sec,
                    )
                    if rgb is not None
                    else None
                )
                depth_info = (
                    closest_by_stamp(
                        self._depth_infos,
                        depth.stamp_ns,
                        self._max_camera_info_skew_sec,
                    )
                    if isinstance(depth, BinaryFrame)
                    else None
                )
            self._last_request_started_monotonic = time.monotonic()
            self._process_frame(
                rgb,
                depth if isinstance(depth, BinaryFrame) else None,
                color_info if isinstance(color_info, CameraCalibration) else None,
                depth_info if isinstance(depth_info, CameraCalibration) else None,
            )

    def _ensure_capabilities(self) -> dict[str, str]:
        if self._pinned_model_digests is not None:
            return dict(self._pinned_model_digests)
        deadline_monotonic = time.monotonic() + self._request_timeout_sec
        health_response = self._session.get(
            f"{self._service_url}/v1/health",
            timeout=_remaining_deadline_sec(
                deadline_monotonic,
                field="worker health",
            ),
            stream=True,
            allow_redirects=False,
        )
        health_payload = _read_bounded_json_response(
            health_response,
            field="worker health",
            deadline_monotonic=deadline_monotonic,
        )
        health_digests = validate_worker_health(
            health_payload,
            requested_algorithms=self._requested_algorithms,
            received_unix_ms=time.time_ns() // 1_000_000,
            max_age_ms=self._worker_health_max_age_ms,
            max_clock_skew_ms=self._max_worker_clock_skew_ms,
        )
        response = self._session.get(
            f"{self._service_url}/v1/capabilities",
            timeout=_remaining_deadline_sec(
                deadline_monotonic,
                field="worker capabilities",
            ),
            stream=True,
            allow_redirects=False,
        )
        payload = _read_bounded_json_response(
            response,
            field="worker capabilities",
            deadline_monotonic=deadline_monotonic,
        )
        digests = validate_capabilities(
            payload,
            requested_algorithms=self._requested_algorithms,
            expected_model_digests=self._expected_model_digests,
            expected_auth_mode=self._worker_auth_mode,
            received_unix_ms=time.time_ns() // 1_000_000,
            max_age_ms=self._worker_health_max_age_ms,
            max_clock_skew_ms=self._max_worker_clock_skew_ms,
        )
        if digests != health_digests:
            raise ContractError(
                "worker health and capabilities model digests do not match"
            )
        # Once pinned, a worker restart or checkpoint swap requires an adapter
        # restart; an in-flight endpoint cannot silently change model identity.
        self._pinned_model_digests = dict(digests)
        return digests

    def _process_frame(
        self,
        rgb: BinaryFrame,
        depth: BinaryFrame | None,
        color_info: CameraCalibration | None,
        depth_info: CameraCalibration | None,
    ) -> None:
        started_perf = time.perf_counter()
        source_age_sec = (time.time_ns() - rgb.stamp_ns) / 1e9
        if source_age_sec < -0.25 or source_age_sec > self._max_source_age_sec:
            self._publish_failure(
                rgb,
                started_perf,
                "STALE_SOURCE_FRAME",
                f"RGB source age {source_age_sec:.3f}s is outside policy",
            )
            return
        try:
            rgb_dimensions = _rgb_dimensions(rgb)
            pinned_digests = self._ensure_capabilities()
            request_started_unix_ms = time.time_ns() // 1_000_000
            if (time.time_ns() - rgb.stamp_ns) / 1e9 > self._max_source_age_sec:
                raise ContractError(
                    "source frame became stale during capabilities preflight"
                )
            request_id = str(uuid.uuid4())
            deadline_unix_ms = request_started_unix_ms + int(
                self._request_timeout_sec * 1000.0
            )
            metadata = build_request_metadata(
                request_id=request_id,
                rgb=rgb,
                depth=depth,
                color_camera_info=color_info,
                depth_camera_info=depth_info,
                requested_algorithms=self._requested_algorithms,
                deadline_unix_ms=deadline_unix_ms,
                depth_scale_m_per_unit=getattr(self, "_depth_scale_m_per_unit", 0.0),
                depth_scale_validated=getattr(self, "_depth_scale_validated", False),
                depth_alignment_validated=getattr(
                    self, "_depth_alignment_validated", False
                ),
                depth_alignment_id=getattr(self, "_depth_alignment_id", ""),
            )
            metadata_bytes = json.dumps(
                metadata,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            files: dict[str, tuple[str, bytes, str]] = {
                "metadata": (
                    "metadata.json",
                    metadata_bytes,
                    "application/json",
                ),
                "rgb": ("rgb.bin", rgb.data, _rgb_content_type(rgb.format)),
            }
            if depth is not None:
                files["depth"] = (
                    "depth.bin",
                    depth.data,
                    "application/octet-stream",
                )
            response_deadline_monotonic = (
                time.monotonic() + self._request_timeout_sec
            )
            response = self._session.post(
                f"{self._service_url}/v1/infer",
                files=files,
                timeout=_remaining_deadline_sec(
                    response_deadline_monotonic,
                    field="worker inference",
                ),
                stream=True,
                allow_redirects=False,
            )
            payload = _read_bounded_json_response(
                response,
                field="worker inference",
                deadline_monotonic=response_deadline_monotonic,
            )
            received_unix_ms = time.time_ns() // 1_000_000
            validated = validate_worker_response(
                payload,
                metadata=metadata,
                pinned_model_digests=pinned_digests,
                request_started_unix_ms=request_started_unix_ms,
                received_unix_ms=received_unix_ms,
                max_clock_skew_ms=self._max_worker_clock_skew_ms,
                expected_tool_support_plane_config_version=str(
                    getattr(
                        self,
                        "_expected_tool_support_plane_config_version",
                        "",
                    )
                ),
                expected_tool_support_plane_normal=getattr(
                    self,
                    "_expected_tool_support_plane_normal",
                    None,
                ),
                tool_support_plane_normal_tolerance_deg=float(
                    getattr(
                        self,
                        "_tool_support_plane_normal_tolerance_deg",
                        _DEFAULT_SUPPORT_PLANE_NORMAL_TOLERANCE_DEG,
                    )
                ),
                expected_rgb_dimensions=rgb_dimensions,
            )
            response_source_age_sec = (time.time_ns() - rgb.stamp_ns) / 1e9
            if response_source_age_sec > self._max_source_age_sec:
                raise ContractError("worker result became stale before publication")
            with self._condition:
                if not self._enabled or rgb.stamp_ns <= self._last_published_stamp_ns:
                    return
                self._last_published_stamp_ns = rgb.stamp_ns
            self._publish_success(
                rgb=rgb,
                depth=depth,
                color_info=color_info,
                depth_info=depth_info,
                request_id=request_id,
                validated=validated,
                source_to_output_latency_ms=(time.perf_counter() - started_perf)
                * 1000.0,
            )
        except Exception as exc:  # noqa: BLE001
            # rclpy's signal handler may invalidate the context while an HTTP
            # request is in flight.  Shutdown is not an inference failure and
            # must not attempt a second publish through an invalid context.
            if not self._running or not rclpy.ok(context=self.context):
                return
            self._publish_failure(
                rgb,
                started_perf,
                "PNU_REQUEST_REJECTED",
                str(exc),
            )
            self.get_logger().warning(
                f"PNU perception request failed: {exc}",
                throttle_duration_sec=3.0,
            )

    def _publish_debug_overlay(
        self,
        *,
        rgb: BinaryFrame,
        validated: ValidatedWorkerResponse,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "overlay_published": False,
            "overlay_status": "publisher_unavailable",
            "overlay_truncated": False,
            "overlay_drawn_tool_count": 0,
            "overlay_drawn_blood_count": 0,
            "overlay_drawn_hand_count": 0,
            "render_encode_latency_ms": 0.0,
        }
        publisher = getattr(self, "_overlay_pub", None)
        if publisher is None:
            return result
        if not bool(getattr(self, "_overlay_enabled", True)):
            result["overlay_status"] = "disabled"
            return result

        max_rate_hz = float(
            getattr(self, "_overlay_max_rate_hz", _DEFAULT_OVERLAY_MAX_RATE_HZ)
        )
        now_monotonic = time.monotonic()
        last_published = float(getattr(self, "_last_overlay_published_monotonic", 0.0))
        if last_published > 0.0 and (
            now_monotonic - last_published < 1.0 / max_rate_hz
        ):
            result["overlay_status"] = "rate_limited"
            return result

        try:
            rendered = build_pnu_debug_overlay(
                frame=rgb,
                validated=validated,
                max_pixels=int(
                    getattr(
                        self,
                        "_overlay_max_pixels",
                        _DEFAULT_OVERLAY_MAX_PIXELS,
                    )
                ),
            )
            publisher.publish(rendered.message)
        except Exception as exc:  # noqa: BLE001
            # Debug imagery is observability-only and must never invalidate an
            # otherwise accepted semantic result.  It also never publishes a
            # replacement/stale image when rendering fails.
            result["overlay_status"] = "render_error"
            try:
                self.get_logger().warning(
                    f"PNU debug overlay skipped: {exc}",
                    throttle_duration_sec=3.0,
                )
            except Exception:  # noqa: BLE001,S110
                pass
            return result

        self._last_overlay_published_monotonic = now_monotonic
        self._last_overlay_visible = bool(
            rendered.drawn_tool_count
            or rendered.drawn_blood_count
            or rendered.drawn_hand_count
        )
        result.update(
            {
                "overlay_published": True,
                "overlay_status": "published",
                "overlay_truncated": rendered.truncated,
                "overlay_drawn_tool_count": rendered.drawn_tool_count,
                "overlay_drawn_blood_count": rendered.drawn_blood_count,
                "overlay_drawn_hand_count": rendered.drawn_hand_count,
                "render_encode_latency_ms": rendered.render_encode_latency_ms,
            }
        )
        if rendered.truncated:
            try:
                self.get_logger().warning(
                    "PNU debug overlay drawing was bounded; diagnostics mark truncation",
                    throttle_duration_sec=3.0,
                )
            except Exception:  # noqa: BLE001,S110
                pass
        return result

    def _clear_stale_overlays(self, rgb: BinaryFrame) -> bool:
        """Replace previously visible volatile layers with an exact-stamp clear."""

        detection_visible = bool(getattr(self, "_last_overlay_visible", False))
        pose_visible = tuple(
            getattr(self, "_last_pose_overlay_signature", (0, 0))
        ) != (0, 0)
        if not detection_visible and not pose_visible:
            return False
        empty = ToolPoseArray()
        empty.header.stamp.sec = rgb.stamp_ns // 1_000_000_000
        empty.header.stamp.nanosec = rgb.stamp_ns % 1_000_000_000
        empty.header.frame_id = rgb.frame_id
        try:
            rendered = build_pnu_pose_overlay(
                frame=rgb,
                pose_array=empty,
                color_camera_info=None,
                max_pixels=int(
                    getattr(
                        self,
                        "_pose_overlay_max_pixels",
                        _DEFAULT_OVERLAY_MAX_PIXELS,
                    )
                ),
            )
            published = False
            if detection_visible and bool(getattr(self, "_overlay_enabled", True)):
                publisher = getattr(self, "_overlay_pub", None)
                if publisher is not None:
                    publisher.publish(rendered.message)
                    published = True
            if pose_visible and bool(getattr(self, "_pose_overlay_enabled", True)):
                publisher = getattr(self, "_pose_overlay_pub", None)
                if publisher is not None:
                    publisher.publish(rendered.message)
                    published = True
        except Exception as exc:  # noqa: BLE001
            try:
                self.get_logger().warning(
                    f"PNU stale overlay clear failed: {exc}",
                    throttle_duration_sec=3.0,
                )
            except Exception:  # noqa: BLE001,S110
                pass
            return False
        self._last_overlay_visible = False
        self._last_pose_overlay_signature = (0, 0)
        now_monotonic = time.monotonic()
        self._last_overlay_published_monotonic = now_monotonic
        self._last_pose_overlay_published_monotonic = now_monotonic
        return published

    def _publish_pose_overlay(
        self,
        *,
        rgb: BinaryFrame,
        pose_array: ToolPoseArray,
        color_camera_info: CameraCalibration | None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "pose_overlay_published": False,
            "pose_overlay_status": "publisher_unavailable",
            "pose_overlay_drawn_axis_count": 0,
            "pose_overlay_drawn_position_only_count": 0,
            "pose_overlay_render_encode_latency_ms": 0.0,
            "pose_overlay_truncated": False,
        }
        publisher = getattr(self, "_pose_overlay_pub", None)
        if publisher is None:
            return result
        if not bool(getattr(self, "_pose_overlay_enabled", True)):
            result["pose_overlay_status"] = "disabled"
            return result

        now_monotonic = time.monotonic()
        max_rate_hz = float(
            getattr(
                self,
                "_pose_overlay_max_rate_hz",
                _DEFAULT_POSE_OVERLAY_MAX_RATE_HZ,
            )
        )
        last_published = float(
            getattr(self, "_last_pose_overlay_published_monotonic", 0.0)
        )
        bounded_tools = tuple(getattr(pose_array, "tools", ()))[:
            _MAX_POSE_OVERLAY_INSTANCES
        ]
        signature = (
            sum(
                bool(tool.position_valid and tool.orientation_valid)
                for tool in bounded_tools
            ),
            sum(
                bool(tool.position_valid and not tool.orientation_valid)
                for tool in bounded_tools
            ),
        )
        last_signature = tuple(
            getattr(self, "_last_pose_overlay_signature", (0, 0))
        )
        # A visibility-class transition must replace the previous layer even
        # inside the normal rate window. In particular, an accepted zero-tool
        # frame publishes a transparent image to clear stale axes/markers.
        # Stable classes are rejected before allocating/encoding a 4 MP WebP.
        visibility_changed = signature != last_signature
        if (
            last_published > 0.0
            and now_monotonic - last_published < 1.0 / max_rate_hz
            and not visibility_changed
        ):
            result["pose_overlay_status"] = "rate_limited"
            return result

        try:
            rendered = build_pnu_pose_overlay(
                frame=rgb,
                pose_array=pose_array,
                color_camera_info=color_camera_info,
                axis_length_m=float(
                    getattr(
                        self,
                        "_pose_axis_length_m",
                        _DEFAULT_POSE_AXIS_LENGTH_M,
                    )
                ),
                max_pixels=int(
                    getattr(
                        self,
                        "_pose_overlay_max_pixels",
                        _DEFAULT_OVERLAY_MAX_PIXELS,
                    )
                ),
            )
        except Exception as exc:  # noqa: BLE001
            result["pose_overlay_status"] = "render_error"
            try:
                self.get_logger().warning(
                    f"PNU pose overlay skipped: {exc}",
                    throttle_duration_sec=3.0,
                )
            except Exception:  # noqa: BLE001,S110
                pass
            return result

        try:
            publisher.publish(rendered.message)
        except Exception as exc:  # noqa: BLE001
            result["pose_overlay_status"] = "render_error"
            try:
                self.get_logger().warning(
                    f"PNU pose overlay publish failed: {exc}",
                    throttle_duration_sec=3.0,
                )
            except Exception:  # noqa: BLE001,S110
                pass
            return result

        self._last_pose_overlay_published_monotonic = now_monotonic
        self._last_pose_overlay_signature = signature
        result.update(
            {
                "pose_overlay_published": True,
                "pose_overlay_status": "published",
                "pose_overlay_drawn_axis_count": rendered.drawn_axis_count,
                "pose_overlay_drawn_position_only_count": (
                    rendered.drawn_position_only_count
                ),
                "pose_overlay_render_encode_latency_ms": (
                    rendered.render_encode_latency_ms
                ),
                "pose_overlay_truncated": rendered.truncated,
            }
        )
        if rendered.truncated:
            try:
                self.get_logger().warning(
                    "PNU pose overlay drawing was bounded; diagnostics mark truncation",
                    throttle_duration_sec=3.0,
                )
            except Exception:  # noqa: BLE001,S110
                pass
        return result

    def _publish_success(
        self,
        *,
        rgb: BinaryFrame,
        depth: BinaryFrame | None,
        color_info: CameraCalibration | None,
        depth_info: CameraCalibration | None,
        request_id: str,
        validated: ValidatedWorkerResponse,
        source_to_output_latency_ms: float,
    ) -> None:
        latency = validated.payload["latency_ms"]
        self._sequence += 1
        tool_executed = "tool" in self._requested_algorithms
        pose_array: ToolPoseArray | None = None
        if tool_executed:
            semantics = build_cam4_semantics(
                validated.tool_detections,
                source_stamp_sec=rgb.source_stamp_sec,
                inference_latency_ms=float(latency["tool"]),
            )
            semantics_message = String()
            semantics_message.data = json.dumps(
                semantics,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            self._semantics_pub.publish(semantics_message)
            pose_array, observation_array = build_tool_ros_messages(
                frame=rgb,
                sequence=self._sequence,
                detections=validated.tool_detections,
                tool_result=validated.payload["results"]["tool"],
                model_version=str(validated.payload["models"]["tool"]["version"]),
            )
            self._tool_pose_pub.publish(pose_array)
            self._tool_observations_pub.publish(observation_array)
            if "hand" not in self._requested_algorithms:
                self._mayo_tracker.reset()
            elif validated.hands:
                blocked_semantics = dict(semantics)
                blocked_semantics["tool_request"] = {
                    "state": "hand_with_tool",
                    "requested": None,
                    "confidence": 0.0,
                }
                self._publish_mayo_observations(blocked_semantics)
            else:
                self._publish_mayo_observations(semantics)

        if "blood" in self._requested_algorithms:
            blood_result = validated.payload["results"]["blood"]
            blood_semantics = build_blood_semantics(
                validated.blood_detections,
                source_stamp_ns=rgb.stamp_ns,
                source_stamp_sec=rgb.source_stamp_sec,
                frame_id=rgb.frame_id,
                metric_3d_ready=validated.metric_3d_ready,
                combined_centroid_xy_px=blood_result.get(
                    "combined_blood_centroid_xy_px"
                ),
                combined_centroid_depth_m=blood_result.get(
                    "combined_blood_centroid_depth_m"
                ),
            )
            blood_message = String()
            blood_message.data = json.dumps(
                blood_semantics,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            self._blood_semantics_pub.publish(blood_message)

        if "hand" in self._requested_algorithms:
            self._hand_keypoints_pub.publish(
                build_hand_keypoints_message(
                    frame=rgb,
                    hands=validated.hands,
                    metric_3d_ready=validated.metric_3d_ready,
                )
            )

        detection_count = (
            len(validated.tool_detections)
            + len(validated.blood_detections)
            + len(validated.hands)
        )
        overlay_diagnostics = self._publish_debug_overlay(
            rgb=rgb,
            validated=validated,
        )
        pose_overlay_diagnostics: dict[str, Any] = {}
        if pose_array is not None:
            pose_overlay_diagnostics = self._publish_pose_overlay(
                rgb=rgb,
                pose_array=pose_array,
                color_camera_info=color_info,
            )
        tool_result = validated.payload.get("results", {}).get("tool", {})
        support_plane_config_version = str(
            tool_result.get("support_plane_config_version", "")
        )
        support_plane_validated = bool(
            tool_result.get("support_plane_validated", False)
        )
        diagnostics = {
            "schema": DIAGNOSTICS_SCHEMA,
            "view": "cam4",
            "source_stamp_sec": rgb.stamp_ns // 1_000_000_000,
            "source_stamp_nanosec": rgb.stamp_ns % 1_000_000_000,
            "frame_id": rgb.frame_id,
            "observation_id": f"cam4:{rgb.stamp_ns}",
            "sequence": self._sequence,
            "decode_latency_ms": round(float(latency["decode"]), 3),
            "depth_to_xyz_latency_ms": 0.0,
            "inference_latency_ms": round(
                sum(float(latency[name]) for name in self._requested_algorithms),
                3,
            ),
            "pose_latency_ms": 0.0,
            "render_encode_latency_ms": overlay_diagnostics["render_encode_latency_ms"],
            "source_to_output_latency_ms": round(
                max(
                    0.0,
                    source_to_output_latency_ms
                    + float(overlay_diagnostics["render_encode_latency_ms"])
                    + float(
                        pose_overlay_diagnostics.get(
                            "pose_overlay_render_encode_latency_ms",
                            0.0,
                        )
                    ),
                ),
                3,
            ),
            "queue_age_ms": round(
                max(0.0, time.monotonic() - rgb.received_monotonic) * 1000.0,
                3,
            ),
            "dropped_frames": self._dropped_frames,
            "instance_count": detection_count,
            "valid_pose_count": sum(
                1
                for row in validated.tool_detections
                if isinstance(row.get("pose"), Mapping)
                and row["pose"].get("position_valid") is True
            )
            + sum(1 for row in validated.hands if row.get("palm_6d") is not None),
            "endpoint_sign_low_count": 0,
            "model_version": ",".join(
                f"{name}:{validated.payload['models'][name]['version']}"
                for name in self._requested_algorithms
            )[:120],
            "calibration_version": str(
                tool_result.get(
                    "calibration_version",
                    f"color:{color_info.stamp_ns}" if color_info is not None else "",
                )
            ),
            "support_plane_config_version": support_plane_config_version,
            "support_plane_validated": support_plane_validated,
            "support_plane_diagnostics": validated.tool_support_plane_diagnostics,
            "error_code": "",
            "error_message": "",
            "provider": "pnu_hand_blood",
            "auth_mode": getattr(self, "_auth_mode", "unknown"),
            "transport_mode": getattr(self, "_transport_mode", "unknown"),
            "request_id": request_id,
            "requested_algorithms": list(self._requested_algorithms),
            # This list is emitted only after the worker response validator
            # proved model.ready/executed/status for every requested model.
            "executed_algorithms": list(self._requested_algorithms),
            "model_digests": validated.model_digests,
            "tool_detection_count": len(validated.tool_detections),
            "blood_detection_count": len(validated.blood_detections),
            "hand_count": len(validated.hands),
            "depth_included": depth is not None,
            "depth_camera_info_included": depth_info is not None,
            "depth_scale_m_per_unit": (
                getattr(self, "_depth_scale_m_per_unit", 0.0)
                if depth is not None
                else 0.0
            ),
            "depth_scale_validated": bool(
                depth is not None and getattr(self, "_depth_scale_validated", False)
            ),
            "depth_aligned": bool(
                validated.payload["source"].get("depth", {}).get("aligned", False)
            ),
            "metric_3d_ready": validated.metric_3d_ready,
            "metric_3d_reasons": list(validated.payload["metric_3d"]["reasons"]),
            "depth_evidence": validated.payload.get("depth_evidence"),
            "empty_detection_result": detection_count == 0,
            **overlay_diagnostics,
            **pose_overlay_diagnostics,
        }
        diagnostics_message = String()
        diagnostics_message.data = json.dumps(
            diagnostics,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        self._diagnostics_pub.publish(diagnostics_message)
        self._last_success_monotonic = time.monotonic()
        self._last_success_source_stamp_ns = rgb.stamp_ns
        self._last_success_rgb = rgb
        self._last_detection_count = detection_count
        self._publish_health(
            connected=True,
            status="ready" if tool_executed else "partial_ready",
            source_stamp_ns=rgb.stamp_ns,
            detection_count=detection_count,
            error_code="",
            error_message="",
            semantic_ready=tool_executed,
            depth_aligned=bool(
                validated.payload["source"].get("depth", {}).get("aligned", False)
            ),
            metric_3d_ready=validated.metric_3d_ready,
            metric_3d_reasons=validated.payload["metric_3d"]["reasons"],
            support_plane_config_version=support_plane_config_version,
            support_plane_validated=support_plane_validated,
        )

    def _publish_mayo_observations(self, semantics: dict[str, Any]) -> None:
        for placement in self._mayo_tracker.update(semantics):
            stamp_sec = int(placement.source_stamp_sec)
            stamp_nanosec = round(
                (placement.source_stamp_sec - stamp_sec) * 1_000_000_000
            )
            if stamp_nanosec >= 1_000_000_000:
                stamp_sec += 1
                stamp_nanosec -= 1_000_000_000
            observation = ToolObservation()
            observation.stamp.sec = stamp_sec
            observation.stamp.nanosec = stamp_nanosec
            observation.instrument_id = placement.instrument_name
            observation.location_id = "mayo_stand"
            observation.location_type = "mayo_stand"
            observation.confidence = placement.confidence
            observation.visible = True
            self._mayo_pub.publish(observation)

    def _publish_failure(
        self,
        rgb: BinaryFrame,
        started_perf: float,
        error_code: str,
        error_message: str,
    ) -> None:
        self._sequence += 1
        self._clear_stale_overlays(rgb)
        diagnostics = String()
        diagnostics.data = json.dumps(
            {
                "schema": DIAGNOSTICS_SCHEMA,
                "view": "cam4",
                "source_stamp_sec": rgb.stamp_ns // 1_000_000_000,
                "source_stamp_nanosec": rgb.stamp_ns % 1_000_000_000,
                "frame_id": rgb.frame_id,
                "observation_id": "",
                "sequence": self._sequence,
                "decode_latency_ms": 0.0,
                "depth_to_xyz_latency_ms": 0.0,
                "inference_latency_ms": 0.0,
                "pose_latency_ms": 0.0,
                "render_encode_latency_ms": 0.0,
                "source_to_output_latency_ms": round(
                    max(0.0, time.perf_counter() - started_perf) * 1000.0, 3
                ),
                "queue_age_ms": round(
                    max(0.0, time.monotonic() - rgb.received_monotonic) * 1000.0,
                    3,
                ),
                "dropped_frames": self._dropped_frames,
                "instance_count": 0,
                "valid_pose_count": 0,
                "endpoint_sign_low_count": 0,
                "model_version": "",
                "calibration_version": "",
                "support_plane_config_version": "",
                "support_plane_validated": False,
                "error_code": error_code,
                "error_message": str(error_message)[:500],
                "provider": "pnu_hand_blood",
                "auth_mode": self._auth_mode,
                "transport_mode": getattr(self, "_transport_mode", "unknown"),
                "requested_algorithms": list(self._requested_algorithms),
                "executed_algorithms": [],
                "depth_aligned": False,
                "metric_3d_ready": False,
                "metric_3d_reasons": [str(error_code)[:160]],
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        self._diagnostics_pub.publish(diagnostics)
        self._publish_health(
            connected=False,
            status="error",
            source_stamp_ns=rgb.stamp_ns,
            detection_count=0,
            error_code=error_code,
            error_message=error_message,
        )

    def _publish_health(
        self,
        *,
        connected: bool,
        status: str,
        source_stamp_ns: int,
        detection_count: int,
        error_code: str,
        error_message: str,
        semantic_ready: bool = False,
        depth_aligned: bool = False,
        metric_3d_ready: bool = False,
        metric_3d_reasons: Sequence[str] = (),
        support_plane_config_version: str = "",
        support_plane_validated: bool = False,
    ) -> None:
        stamp = self.get_clock().now().to_msg()
        normalized_metric_reasons = [str(item)[:160] for item in metric_3d_reasons]
        if not metric_3d_ready and not normalized_metric_reasons:
            normalized_metric_reasons = [
                str(error_code or "no_validated_metric_3d_result")[:160]
            ]
        message = String()
        message.data = json.dumps(
            {
                "schema": HEALTH_SCHEMA,
                "enabled": self._enabled,
                "connected": bool(connected),
                "status": status if self._enabled else "disabled",
                # Existing preflight interprets this as a validated CAM4 result
                # association.  It does not claim RGB/depth registration.
                "cam4_aligned": bool(
                    connected and semantic_ready and source_stamp_ns > 0
                ),
                "semantic_ready": bool(connected and semantic_ready),
                "depth_aligned": bool(connected and depth_aligned),
                "metric_3d_ready": bool(connected and metric_3d_ready),
                "metric_3d_reasons": (
                    [] if connected and metric_3d_ready else normalized_metric_reasons
                ),
                "support_plane_config_version": (
                    str(support_plane_config_version)[:240] if connected else ""
                ),
                "support_plane_validated": bool(connected and support_plane_validated),
                "model_ready": bool(connected and self._pinned_model_digests),
                "provider": "pnu_hand_blood",
                "auth_mode": self._auth_mode,
                "transport_mode": getattr(self, "_transport_mode", "unknown"),
                "requested_algorithms": list(self._requested_algorithms),
                "executed_algorithms": (
                    list(self._requested_algorithms)
                    if connected and source_stamp_ns > 0
                    else []
                ),
                "stamp_sec": int(stamp.sec),
                "stamp_nanosec": int(stamp.nanosec),
                "source_stamp_sec": source_stamp_ns // 1_000_000_000,
                "source_stamp_nanosec": source_stamp_ns % 1_000_000_000,
                "detection_count": max(0, int(detection_count)),
                "empty_detection_result": bool(connected and detection_count == 0),
                "last_error_code": str(error_code)[:120],
                "last_error_message": str(error_message)[:500],
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        self._health_pub.publish(message)

    def _publish_idle_health(self) -> None:
        if not self._enabled:
            self._publish_health(
                connected=False,
                status="disabled",
                source_stamp_ns=0,
                detection_count=0,
                error_code="",
                error_message="",
            )
            return
        if self._last_success_monotonic <= 0.0:
            self._publish_health(
                connected=False,
                status="waiting_for_frame",
                source_stamp_ns=0,
                detection_count=0,
                error_code="",
                error_message="",
            )
            return
        age_sec = time.monotonic() - self._last_success_monotonic
        if age_sec <= self._max_source_age_sec:
            return
        last_rgb = getattr(self, "_last_success_rgb", None)
        if isinstance(last_rgb, BinaryFrame):
            self._clear_stale_overlays(last_rgb)
        self._publish_health(
            connected=False,
            status="stale",
            source_stamp_ns=self._last_success_source_stamp_ns,
            detection_count=0,
            error_code="STALE_RESULT",
            error_message=f"last accepted inference is {age_sec:.3f}s old",
        )

    def destroy_node(self):
        with self._condition:
            self._running = False
            self._condition.notify_all()
        self._worker.join(timeout=self._request_timeout_sec + 1.0)
        self._session.close()
        return super().destroy_node()


def main() -> None:
    rclpy.init()
    node = PNUPerceptionBridgeNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            node.destroy_node()
        finally:
            if rclpy.ok():
                rclpy.shutdown()


if __name__ == "__main__":
    main()
