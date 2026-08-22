"""Observational monitor for the pending VIPLab/CV-team interface.

Large VIPLab RGB-D payloads are represented by retained, bounded stream-status
messages so this observer does not become another LAN image reader.  CameraInfo
and other small calibration messages remain directly validated.  Custom CV
result messages are inspected through the ROS graph by fully-qualified type.
The monitor never republishes camera data, CV evidence, pose data or dummies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import time
from typing import Any, Callable
from urllib.parse import urlsplit

import rclpy
from ament_index_python.packages import PackageNotFoundError, get_package_prefix
from rclpy.node import Node
from rclpy.qos import (
    DurabilityPolicy,
    HistoryPolicy,
    QoSProfile,
    ReliabilityPolicy,
    qos_profile_sensor_data,
)
from sensor_msgs.msg import CameraInfo, CompressedImage, Image
from std_msgs.msg import String

from .cv_contract import (
    CUSTOM_IDL_PACKAGES,
    CV_CONTRACT_SCHEMA,
    CV_CONTRACT_VERSION,
    EXTERNAL_OUTPUT_ENDPOINTS,
    endpoint_by_key,
    resolve_perception_selection,
    validate_perception_endpoint,
)


VIPLAB_STREAM_STATUS_SCHEMA = "arpa_multicam.stream_status.v1"
VIPLAB_STREAM_STATUS_KEYS = frozenset(
    {
        "schema",
        "stream_id",
        "source_topic",
        "source_stamp",
        "frame_id",
        "format",
        "measured_hz",
        "payload_bytes",
        "published_count",
        "dropped_count",
        "qos",
    }
)


def _non_negative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def validate_viplab_stream_status(
    message: Any,
    *,
    expected_stream_id: str,
    expected_source_topic: str,
    compressed_depth: bool,
) -> tuple[bool, list[str], dict[str, Any]]:
    """Validate the exact small status that stands in for one large stream."""

    errors: list[str] = []
    try:
        payload = json.loads(str(getattr(message, "data", "")))
    except (TypeError, ValueError):
        payload = None
    if not isinstance(payload, dict):
        return False, ["invalid_stream_status_json"], {}
    if frozenset(payload) != VIPLAB_STREAM_STATUS_KEYS:
        errors.append("stream_status_keys_mismatch")
    if payload.get("schema") != VIPLAB_STREAM_STATUS_SCHEMA:
        errors.append("stream_status_schema_mismatch")
    if payload.get("stream_id") != expected_stream_id:
        errors.append("stream_status_id_mismatch")
    if payload.get("source_topic") != expected_source_topic:
        errors.append("stream_status_source_topic_mismatch")

    stamp = payload.get("source_stamp")
    if (
        not isinstance(stamp, dict)
        or frozenset(stamp) != {"sec", "nanosec"}
        or not _non_negative_int(stamp.get("sec"))
        or not _non_negative_int(stamp.get("nanosec"))
        or int(stamp.get("nanosec", 1_000_000_000)) >= 1_000_000_000
    ):
        errors.append("invalid_stream_status_stamp")
        source_stamp_sec = None
    else:
        source_stamp_sec = float(stamp["sec"]) + float(stamp["nanosec"]) / 1e9

    frame_id = payload.get("frame_id")
    format_text = payload.get("format")
    if not isinstance(frame_id, str) or not frame_id or len(frame_id) > 128:
        errors.append("invalid_stream_status_frame_id")
    if not isinstance(format_text, str) or not format_text or len(format_text) > 128:
        errors.append("invalid_stream_status_format")
    elif compressed_depth and "compresseddepth" not in format_text.casefold():
        errors.append("format_is_not_compressed_depth")

    measured_hz = payload.get("measured_hz")
    if (
        isinstance(measured_hz, bool)
        or not isinstance(measured_hz, (int, float))
        or not 0.0 <= float(measured_hz) <= 240.0
    ):
        errors.append("invalid_stream_status_rate")
    for key in ("payload_bytes", "published_count", "dropped_count"):
        if not _non_negative_int(payload.get(key)):
            errors.append(f"invalid_stream_status_{key}")

    qos = payload.get("qos")
    if not isinstance(qos, dict) or frozenset(qos) != {
        "reliability",
        "durability",
        "depth",
    }:
        errors.append("invalid_stream_status_qos")
    elif (
        qos.get("reliability") not in {"best_effort", "reliable"}
        or qos.get("durability") not in {"volatile", "transient_local"}
        or not _non_negative_int(qos.get("depth"))
        or int(qos.get("depth", 0)) < 1
    ):
        errors.append("invalid_stream_status_qos")

    return not errors, errors, {
        "source_stamp_sec": source_stamp_sec,
        "frame_id": frame_id if isinstance(frame_id, str) else "",
        "format": format_text if isinstance(format_text, str) else "",
        "payload_bytes": payload.get("payload_bytes"),
        "measured_hz": payload.get("measured_hz"),
        "published_count": payload.get("published_count"),
        "dropped_count": payload.get("dropped_count"),
        "transport": "compressedDepth" if compressed_depth else "compressedImage",
        "evidence_transport": "retained_stream_status",
    }


def qos_contract_state(expected: str, actual: dict[str, Any]) -> str:
    """Return MATCH, MISMATCH, or UNVERIFIABLE for a workbook QoS profile."""

    normalized = str(expected).upper()
    reliability = (
        int(ReliabilityPolicy.RELIABLE)
        if "RELIABLE" in normalized
        else int(ReliabilityPolicy.BEST_EFFORT)
    )
    durability = (
        int(DurabilityPolicy.TRANSIENT_LOCAL)
        if "TRANSIENT_LOCAL" in normalized
        else int(DurabilityPolicy.VOLATILE)
    )
    depth_marker = "KEEP_LAST("
    depth = None
    if depth_marker in normalized:
        suffix = normalized.split(depth_marker, 1)[1]
        try:
            depth = int(suffix.split(")", 1)[0])
        except ValueError:
            return "MISMATCH"
    if (
        int(actual.get("reliability", -1)) != reliability
        or int(actual.get("durability", -1)) != durability
    ):
        return "MISMATCH"
    actual_depth = int(actual.get("depth", -1))
    if depth is not None and actual_depth <= 0:
        # Some RMW endpoint introspection paths expose reliability/durability
        # but report history/depth as UNKNOWN.  Do not lie that it matched.
        return "UNVERIFIABLE_DEPTH"
    if depth is not None and actual_depth != depth:
        return "MISMATCH"
    return "MATCH"


def qos_contract_matches(expected: str, actual: dict[str, Any]) -> bool:
    """Compatibility helper for callers/tests requiring a Boolean result."""

    return qos_contract_state(expected, actual) == "MATCH"


def _stamp_sec(message: Any) -> float | None:
    stamp = getattr(getattr(message, "header", None), "stamp", None)
    if stamp is None:
        return None
    value = float(getattr(stamp, "sec", 0)) + float(
        getattr(stamp, "nanosec", 0)
    ) / 1_000_000_000.0
    return value if math.isfinite(value) and value > 0.0 else None


def _frame_id(message: Any) -> str:
    return str(getattr(getattr(message, "header", None), "frame_id", "")).strip()


def _finite(values: Any) -> bool:
    try:
        return all(math.isfinite(float(value)) for value in values)
    except (TypeError, ValueError):
        return False


def validate_compressed_image(message: Any) -> tuple[bool, list[str], dict[str, Any]]:
    """Validate only the stable wire-level properties of RGB input."""

    errors: list[str] = []
    data = getattr(message, "data", b"")
    if not data:
        errors.append("empty_payload")
    if _stamp_sec(message) is None:
        errors.append("missing_source_stamp")
    if not _frame_id(message):
        errors.append("missing_frame_id")
    return not errors, errors, {
        "format": str(getattr(message, "format", "")).strip(),
        "payload_bytes": len(data),
        "source_stamp_sec": _stamp_sec(message),
        "frame_id": _frame_id(message),
    }


def validate_compressed_depth(
    message: Any,
) -> tuple[bool, list[str], dict[str, Any]]:
    """Validate the observed native compressedDepth wire contract only."""

    valid, errors, details = validate_compressed_image(message)
    format_text = str(getattr(message, "format", "")).strip()
    if "compresseddepth" not in format_text.casefold():
        errors.append("format_is_not_compressed_depth")
    details = {
        **details,
        "transport": "compressedDepth",
        "alignment_state": "NATIVE_DEPTH_FRAME_NOT_COLOR_ALIGNED",
        "depth_units_policy": "UNVERIFIED_NO_LIVE_SCALE_CONTRACT",
    }
    return not errors and valid, errors, details


def validate_aligned_compressed_depth(
    message: Any,
) -> tuple[bool, list[str], dict[str, Any]]:
    """Validate the CAM4 RGB-aligned compressedDepth transport claim.

    This validates the wire format and color optical frame only.  Metric use
    remains gated in the PNU bridge by paired CameraInfo, dimensions, stamps
    and the serial-specific live depth scale.
    """

    valid, errors, details = validate_compressed_image(message)
    format_text = str(getattr(message, "format", "")).strip()
    frame_id = _frame_id(message)
    if "compresseddepth" not in format_text.casefold():
        errors.append("format_is_not_compressed_depth")
    if "16uc1" not in format_text.casefold():
        errors.append("format_is_not_16uc1")
    if frame_id != "cam_4_color_optical_frame":
        errors.append("frame_is_not_cam4_color_optical_frame")
    details = {
        **details,
        "transport": "compressedDepth",
        "alignment_state": "RGB_ALIGNED_CAM4_COLOR_OPTICAL_FRAME",
        "depth_units_policy": "REQUIRES_VALIDATED_LIVE_SENSOR_SCALE",
    }
    return not errors and valid, errors, details


def validate_camera_info(message: Any) -> tuple[bool, list[str], dict[str, Any]]:
    """Validate dimensions and calibration matrix shape, not calibration truth."""

    errors: list[str] = []
    width = int(getattr(message, "width", 0))
    height = int(getattr(message, "height", 0))
    if width <= 0 or height <= 0:
        errors.append("invalid_dimensions")
    if _stamp_sec(message) is None:
        errors.append("missing_source_stamp")
    if not _frame_id(message):
        errors.append("missing_frame_id")
    matrices = (
        ("k", 9),
        ("r", 9),
        ("p", 12),
    )
    for name, expected_length in matrices:
        values = getattr(message, name, ())
        if len(values) != expected_length or not _finite(values):
            errors.append(f"invalid_{name}_matrix")
    return not errors, errors, {
        "width": width,
        "height": height,
        "source_stamp_sec": _stamp_sec(message),
        "frame_id": _frame_id(message),
        "calibration_policy": "PENDING_EXTERNAL_CALIBRATION_VERSION",
    }


def validate_depth_image(message: Any) -> tuple[bool, list[str], dict[str, Any]]:
    """Validate a depth frame structurally without assuming units or encoding."""

    errors: list[str] = []
    width = int(getattr(message, "width", 0))
    height = int(getattr(message, "height", 0))
    step = int(getattr(message, "step", 0))
    data = getattr(message, "data", b"")
    if width <= 0 or height <= 0 or step <= 0:
        errors.append("invalid_dimensions_or_step")
    elif len(data) < step * height:
        errors.append("payload_shorter_than_step_times_height")
    if not str(getattr(message, "encoding", "")).strip():
        errors.append("missing_encoding")
    if _stamp_sec(message) is None:
        errors.append("missing_source_stamp")
    if not _frame_id(message):
        errors.append("missing_frame_id")
    return not errors, errors, {
        "width": width,
        "height": height,
        "step": step,
        "encoding": str(getattr(message, "encoding", "")).strip(),
        "payload_bytes": len(data),
        "source_stamp_sec": _stamp_sec(message),
        "frame_id": _frame_id(message),
        "depth_units_policy": "PENDING_EXTERNAL_CONTRACT",
    }


@dataclass
class InputTracker:
    key: str
    topic: str
    expected_type: str
    expected_qos: str
    pending_reason: str = ""
    received_count: int = 0
    accepted_count: int = 0
    rejected_count: int = 0
    last_receipt_monotonic_sec: float | None = None
    last_source_stamp_sec: float | None = None
    last_frame_id: str = ""
    last_details: dict[str, Any] = field(default_factory=dict)
    last_errors: list[str] = field(default_factory=list)

    def observe(
        self,
        message: Any,
        validator: Callable[[Any], tuple[bool, list[str], dict[str, Any]]],
    ) -> None:
        self.received_count += 1
        valid, errors, details = validator(message)
        source_stamp = details.get("source_stamp_sec")
        if (
            valid
            and source_stamp is not None
            and self.last_source_stamp_sec is not None
            and source_stamp <= self.last_source_stamp_sec
        ):
            valid = False
            errors = [*errors, "duplicate_or_out_of_order_source_stamp"]
        self.last_receipt_monotonic_sec = time.monotonic()
        self.last_details = details
        self.last_errors = errors
        self.last_frame_id = str(details.get("frame_id", ""))
        if valid:
            self.accepted_count += 1
            self.last_source_stamp_sec = source_stamp
        else:
            self.rejected_count += 1

    def snapshot(self, publishers: list[dict[str, Any]]) -> dict[str, Any]:
        publisher_count = len(publishers)
        observed_types = sorted({publisher["type"] for publisher in publishers})
        qos_states = [
            qos_contract_state(self.expected_qos, publisher)
            for publisher in publishers
        ]
        if publisher_count <= 0:
            state = "WAITING_FOR_PUBLISHER"
        elif publisher_count > 1:
            state = "AMBIGUOUS_PUBLISHERS"
        elif observed_types != [self.expected_type]:
            state = "TYPE_MISMATCH"
        elif "MISMATCH" in qos_states:
            state = "QOS_MISMATCH"
        elif "UNVERIFIABLE_DEPTH" in qos_states:
            state = "QOS_UNVERIFIABLE_DEPTH"
        elif self.received_count <= 0:
            state = "WAITING_FOR_SAMPLE"
        elif self.last_errors:
            state = "INVALID_SAMPLE"
        else:
            state = "OBSERVED_POLICY_PENDING"
        age = (
            max(0.0, time.monotonic() - self.last_receipt_monotonic_sec)
            if self.last_receipt_monotonic_sec is not None
            else -1.0
        )
        return {
            "topic": self.topic,
            "expected_type": self.expected_type,
            "expected_qos": self.expected_qos,
            "publisher_count": max(0, int(publisher_count)),
            "publishers": publishers,
            "qos_verification": qos_states,
            "state": state,
            "received_count": self.received_count,
            "accepted_count": self.accepted_count,
            "rejected_count": self.rejected_count,
            "last_receipt_age_sec": round(age, 3) if age >= 0.0 else -1.0,
            "last_source_stamp_sec": self.last_source_stamp_sec,
            "frame_id": self.last_frame_id,
            "details": self.last_details,
            "errors": self.last_errors,
            "policy_state": "PENDING_STALE_AND_RGB_DEPTH_SKEW_THRESHOLD",
            "pending_reason": self.pending_reason,
        }


class CvContractMonitor(Node):
    """Expose one latched JSON status without authorizing external evidence."""

    def __init__(self) -> None:
        super().__init__("cv_contract_monitor")
        self.declare_parameter("perception_backend", "local")
        self.declare_parameter("perception_provider", "")
        self.declare_parameter("perception_location", "")
        self.declare_parameter("perception_endpoint", "")
        self.declare_parameter("status_topic", "/integration/cv_contract/status")
        self.declare_parameter(
            "cam4_rgb_topic", "/synced/cam_4/color/image_raw/compressed"
        )
        self.declare_parameter("cam4_rgb_status_topic", "/synced/cam_4/status")
        self.declare_parameter(
            "cam4_rgb_alias_topic", "/surgery/images/cam4/compressed"
        )
        self.declare_parameter(
            "cam4_camera_info_topic",
            "/synced/cam_4/color/camera_info",
        )
        self.declare_parameter(
            "cam4_depth_camera_info_topic",
            "/synced/cam_4/depth/camera_info",
        )
        self.declare_parameter(
            "cam4_native_depth_compressed_topic",
            "/synced/cam_4/depth/image_rect_raw/compressedDepth",
        )
        self.declare_parameter(
            "cam4_native_depth_status_topic",
            "/synced/cam_4/depth/status",
        )
        self.declare_parameter(
            "cam4_depth_to_color_extrinsics_topic",
            "/synced/cam_4/extrinsics/depth_to_color",
        )
        self.declare_parameter(
            "cam4_aligned_depth_compressed_topic",
            (
                "/synced/cam_4/aligned_depth_to_color/"
                "image_raw/compressedDepth"
            ),
        )
        self.declare_parameter(
            "cam4_aligned_depth_camera_info_topic",
            "/synced/cam_4/aligned_depth_to_color/camera_info",
        )
        self.declare_parameter(
            "handover_tray_rgb_topic", "/surgery/images/tray/compressed"
        )
        self.declare_parameter(
            "handover_tray_camera_info_topic",
            "/surgery/cameras/tray/color/camera_info",
        )
        self.declare_parameter(
            "handover_tray_aligned_depth_topic",
            "/surgery/cameras/tray/aligned_depth",
        )

        self._perception = resolve_perception_selection(
            provider=self.get_parameter("perception_provider").value,
            location=self.get_parameter("perception_location").value,
            legacy_backend=self.get_parameter("perception_backend").value,
        )
        self._backend = self._perception.legacy_backend
        endpoint_value = str(
            self.get_parameter("perception_endpoint").value
        ).strip()
        if (
            not endpoint_value
            and self._perception.source == "legacy_backend"
            and self._perception.provider == "builtin_rfdetr"
        ):
            endpoint_value = "http://127.0.0.1:8010"
        self._perception_endpoint = validate_perception_endpoint(
            endpoint_value, self._perception
        )
        self._trackers = {
            "cam4_rgb": InputTracker(
                "cam4_rgb",
                str(self.get_parameter("cam4_rgb_topic").value),
                "sensor_msgs/msg/CompressedImage",
                endpoint_by_key("cam4_rgb").qos,
            ),
            "cam4_camera_info": InputTracker(
                "cam4_camera_info",
                str(self.get_parameter("cam4_camera_info_topic").value),
                "sensor_msgs/msg/CameraInfo",
                endpoint_by_key("cam4_camera_info").qos,
                "provider_and_calibration_pending",
            ),
            "cam4_depth_camera_info": InputTracker(
                "cam4_depth_camera_info",
                str(self.get_parameter("cam4_depth_camera_info_topic").value),
                "sensor_msgs/msg/CameraInfo",
                endpoint_by_key("cam4_depth_camera_info").qos,
                "calibration_version_and_depth_to_color_extrinsics_pending",
            ),
            "cam4_native_depth_compressed": InputTracker(
                "cam4_native_depth_compressed",
                str(
                    self.get_parameter(
                        "cam4_native_depth_compressed_topic"
                    ).value
                ),
                "sensor_msgs/msg/CompressedImage",
                endpoint_by_key("cam4_native_depth_compressed").qos,
                (
                    "native_depth_optical_frame; align_depth_disabled; "
                    "registration_pending"
                ),
            ),
            "cam4_aligned_depth_compressed": InputTracker(
                "cam4_aligned_depth_compressed",
                str(
                    self.get_parameter(
                        "cam4_aligned_depth_compressed_topic"
                    ).value
                ),
                "sensor_msgs/msg/CompressedImage",
                endpoint_by_key("cam4_aligned_depth_compressed").qos,
                "paired_camera_info_dimensions_stamps_and_scale_gate_pending",
            ),
            "cam4_aligned_depth_camera_info": InputTracker(
                "cam4_aligned_depth_camera_info",
                str(
                    self.get_parameter(
                        "cam4_aligned_depth_camera_info_topic"
                    ).value
                ),
                "sensor_msgs/msg/CameraInfo",
                endpoint_by_key("cam4_aligned_depth_camera_info").qos,
                "must_match_cam4_color_camera_info",
            ),
            "handover_tray_rgb": InputTracker(
                "handover_tray_rgb",
                str(self.get_parameter("handover_tray_rgb_topic").value),
                "sensor_msgs/msg/CompressedImage",
                "BEST_EFFORT/VOLATILE/KEEP_LAST(5)",
                "optional_handover_tray_camera_not_mayo",
            ),
            "handover_tray_camera_info": InputTracker(
                "handover_tray_camera_info",
                str(self.get_parameter("handover_tray_camera_info_topic").value),
                "sensor_msgs/msg/CameraInfo",
                endpoint_by_key("handover_tray_camera_info").qos,
                "optional_handover_tray_camera_not_mayo",
            ),
            "handover_tray_aligned_depth": InputTracker(
                "handover_tray_aligned_depth",
                str(self.get_parameter("handover_tray_aligned_depth_topic").value),
                "sensor_msgs/msg/Image",
                "BEST_EFFORT/VOLATILE/KEEP_LAST(5)",
                "optional_handover_tray_camera_not_mayo",
            ),
        }
        synced_info_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=20,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.VOLATILE,
        )
        tray_info_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=5,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.VOLATILE,
        )
        stream_status_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        self.create_subscription(
            String,
            str(self.get_parameter("cam4_rgb_status_topic").value),
            lambda message: self._trackers["cam4_rgb"].observe(
                message,
                lambda sample: validate_viplab_stream_status(
                    sample,
                    expected_stream_id="cam_4",
                    expected_source_topic=self._trackers["cam4_rgb"].topic,
                    compressed_depth=False,
                ),
            ),
            stream_status_qos,
        )
        self.create_subscription(
            CameraInfo,
            self._trackers["cam4_camera_info"].topic,
            lambda message: self._trackers["cam4_camera_info"].observe(
                message, validate_camera_info
            ),
            synced_info_qos,
        )
        self.create_subscription(
            CameraInfo,
            self._trackers["cam4_depth_camera_info"].topic,
            lambda message: self._trackers["cam4_depth_camera_info"].observe(
                message, validate_camera_info
            ),
            synced_info_qos,
        )
        self.create_subscription(
            String,
            str(self.get_parameter("cam4_native_depth_status_topic").value),
            lambda message: self._trackers[
                "cam4_native_depth_compressed"
            ].observe(
                message,
                lambda sample: validate_viplab_stream_status(
                    sample,
                    expected_stream_id="cam_4_depth",
                    expected_source_topic=self._trackers[
                        "cam4_native_depth_compressed"
                    ].topic,
                    compressed_depth=True,
                ),
            ),
            stream_status_qos,
        )
        self.create_subscription(
            CameraInfo,
            self._trackers["cam4_aligned_depth_camera_info"].topic,
            lambda message: self._trackers[
                "cam4_aligned_depth_camera_info"
            ].observe(message, validate_camera_info),
            synced_info_qos,
        )
        self.create_subscription(
            CompressedImage,
            self._trackers["handover_tray_rgb"].topic,
            lambda message: self._trackers["handover_tray_rgb"].observe(
                message, validate_compressed_image
            ),
            qos_profile_sensor_data,
        )
        self.create_subscription(
            CameraInfo,
            self._trackers["handover_tray_camera_info"].topic,
            lambda message: self._trackers["handover_tray_camera_info"].observe(
                message, validate_camera_info
            ),
            tray_info_qos,
        )
        self.create_subscription(
            Image,
            self._trackers["handover_tray_aligned_depth"].topic,
            lambda message: self._trackers["handover_tray_aligned_depth"].observe(
                message, validate_depth_image
            ),
            qos_profile_sensor_data,
        )
        self._status_publisher = self.create_publisher(
            String,
            str(self.get_parameter("status_topic").value),
            QoSProfile(
                history=HistoryPolicy.KEEP_LAST,
                depth=1,
                reliability=ReliabilityPolicy.RELIABLE,
                durability=DurabilityPolicy.TRANSIENT_LOCAL,
            ),
        )
        self.create_timer(1.0, self._publish_status)

    def _publisher_info(self, topic: str) -> list[dict[str, Any]]:
        info = self.get_publishers_info_by_topic(topic)
        result = []
        for endpoint in info:
            qos = endpoint.qos_profile
            result.append(
                {
                    "node": endpoint.node_name,
                    "namespace": endpoint.node_namespace,
                    "type": endpoint.topic_type,
                    "reliability": int(qos.reliability),
                    "durability": int(qos.durability),
                    "depth": int(qos.depth),
                }
            )
        return result

    @staticmethod
    def _external_idl_state() -> dict[str, Any]:
        available: dict[str, bool] = {}
        for package in CUSTOM_IDL_PACKAGES:
            try:
                get_package_prefix(package)
            except PackageNotFoundError:
                available[package] = False
            else:
                available[package] = True
        return {
            "required_packages": available,
            "ready": all(available.values()),
            "state": "INSTALLED" if all(available.values()) else "PENDING_EXTERNAL_IDL",
        }

    def _output_snapshot(self) -> dict[str, Any]:
        outputs: dict[str, Any] = {}
        for endpoint in EXTERNAL_OUTPUT_ENDPOINTS:
            observed = self._publisher_info(endpoint.topic)
            observed_types = sorted({entry["type"] for entry in observed})
            qos_states = [
                qos_contract_state(endpoint.qos, publisher)
                for publisher in observed
            ]
            local_rfdetr_present = any(
                entry["node"] == "rfdetr_perception_bridge"
                for entry in observed
            )
            pnu_adapter_present = any(
                entry["node"] == "pnu_perception_bridge"
                for entry in observed
            )
            if not observed:
                state = "WAITING_FOR_EXTERNAL_PUBLISHER"
            elif len(observed) > 1:
                state = "AMBIGUOUS_PUBLISHERS"
            elif observed_types != [endpoint.message_type]:
                state = "TYPE_MISMATCH"
            elif (
                local_rfdetr_present
                and self._perception.provider == "builtin_rfdetr"
            ):
                state = "LOCAL_BACKEND_OWNS_COMPATIBILITY_TOPIC"
            elif (
                pnu_adapter_present
                and self._perception.provider == "pnu_hand_blood"
            ):
                state = "PNU_ADAPTER_OWNS_COMPATIBILITY_TOPIC"
            elif local_rfdetr_present:
                state = "LOCAL_BACKEND_COLLISION"
            elif pnu_adapter_present:
                state = "PNU_ADAPTER_COLLISION"
            elif "MISMATCH" in qos_states:
                state = "QOS_MISMATCH"
            elif "UNVERIFIABLE_DEPTH" in qos_states:
                state = "QOS_UNVERIFIABLE_DEPTH"
            else:
                state = "OBSERVED"
            outputs[endpoint.key] = {
                "topic": endpoint.topic,
                "expected_type": endpoint.message_type,
                "expected_qos": endpoint.qos,
                "owner": endpoint.owner,
                "state": state,
                "publisher_count": len(observed),
                "publishers": observed,
                "qos_verification": qos_states,
                "pending_reason": endpoint.pending_reason,
            }
        return outputs

    def _snapshot(self) -> dict[str, Any]:
        inputs = {
            key: tracker.snapshot(self._publisher_info(tracker.topic))
            for key, tracker in self._trackers.items()
        }
        alias_topic = str(self.get_parameter("cam4_rgb_alias_topic").value)
        alias_publishers = self._publisher_info(alias_topic)
        alias_contract = endpoint_by_key("cam4_rgb_alias")
        inputs["cam4_rgb_alias"] = {
            "topic": alias_topic,
            "expected_type": alias_contract.message_type,
            "expected_qos": alias_contract.qos,
            "alias_of": "cam4_rgb",
            "publisher_count": len(alias_publishers),
            "publishers": alias_publishers,
            "qos_verification": [
                qos_contract_state(alias_contract.qos, publisher)
                for publisher in alias_publishers
            ],
            "state": "ALIAS_NOT_SAMPLED_TO_AVOID_DOUBLE_COUNTING",
            "policy_state": "SCENARIO_AND_DEMAND_GATED_BY_TASKPLANNER",
        }
        extrinsics_contract = endpoint_by_key(
            "cam4_depth_to_color_extrinsics"
        )
        extrinsics_topic = str(
            self.get_parameter(
                "cam4_depth_to_color_extrinsics_topic"
            ).value
        )
        extrinsics_publishers = self._publisher_info(extrinsics_topic)
        extrinsics_types = sorted(
            {entry["type"] for entry in extrinsics_publishers}
        )
        extrinsics_qos = [
            qos_contract_state(extrinsics_contract.qos, publisher)
            for publisher in extrinsics_publishers
        ]
        if not extrinsics_publishers:
            extrinsics_state = "WAITING_FOR_PUBLISHER"
        elif len(extrinsics_publishers) > 1:
            extrinsics_state = "AMBIGUOUS_PUBLISHERS"
        elif extrinsics_types != [extrinsics_contract.message_type]:
            extrinsics_state = "TYPE_MISMATCH"
        elif "MISMATCH" in extrinsics_qos:
            extrinsics_state = "QOS_MISMATCH"
        elif "UNVERIFIABLE_DEPTH" in extrinsics_qos:
            extrinsics_state = "QOS_UNVERIFIABLE_DEPTH"
        else:
            extrinsics_state = "OBSERVED_LAYOUT_UNVALIDATED"
        inputs["cam4_depth_to_color_extrinsics"] = {
            "topic": extrinsics_topic,
            "expected_type": extrinsics_contract.message_type,
            "expected_qos": extrinsics_contract.qos,
            "publisher_count": len(extrinsics_publishers),
            "publishers": extrinsics_publishers,
            "qos_verification": extrinsics_qos,
            "state": extrinsics_state,
            "sample_values_consumed": False,
            "layout_validated": False,
            "pending_reason": extrinsics_contract.pending_reason,
        }
        idl = self._external_idl_state()
        if self._perception.provider == "pnu_hand_blood":
            readiness_state = "PNU_ALIGNED_DEPTH_3D_ADAPTER_AWAITING_HEALTH"
            adapter_state = "PNU_HTTP_ADAPTER_IMPLEMENTED_3D_FAIL_CLOSED"
        elif self._perception.provider == "disabled":
            readiness_state = "DISABLED"
            adapter_state = "DISABLED"
        else:
            readiness_state = "LOCAL_BACKEND_ACTIVE_EXTERNAL_CONTRACT_MONITORED"
            adapter_state = "BUILTIN_RFDETR_ADAPTER_ACTIVE"
        return {
            "schema": CV_CONTRACT_SCHEMA,
            "contract_version": CV_CONTRACT_VERSION,
            "stamp_sec": round(
                self.get_clock().now().nanoseconds / 1_000_000_000.0,
                6,
            ),
            "perception_backend": self._backend,
            "perception_provider": self._perception.provider,
            "perception_location": self._perception.location,
            "perception_selection_source": self._perception.source,
            "perception_worker_origin": self._perception_worker_origin(),
            "readiness_state": readiness_state,
            # Runtime authorization for PNU comes from the versioned bridge
            # health message, not this observational custom-IDL monitor.
            "ready_for_external_evidence": False,
            "adapter_state": adapter_state,
            "policy_pending": {
                "rgb_depth_skew_limit": True,
                "source_stale_timeout": True,
                "depth_units_scale": True,
                "depth_to_color_extrinsics_layout": True,
                "tf_tree_and_calibration": True,
                "ontology_version_and_tool_mapping": True,
            },
            "custom_idl": idl,
            "inputs": inputs,
            "external_outputs": self._output_snapshot(),
            "safety": {
                "dummy_publishers_created": False,
                "custom_idl_reimplemented": False,
                "tool_pose_consumption": "DISABLED_PENDING_CALIBRATION_AND_TF",
                "hand_target_pose_consumption": "MONITOR_ONLY_DISABLED_FOR_CONTROL",
                "handover_tray_semantics": "handover_tray_not_mayo",
            },
        }

    def _perception_worker_origin(self) -> str:
        """Return only the non-secret worker origin for operator diagnostics."""

        if not self._perception_endpoint:
            return ""
        parsed = urlsplit(self._perception_endpoint)
        hostname = parsed.hostname or ""
        rendered_host = f"[{hostname}]" if ":" in hostname else hostname
        rendered_port = f":{parsed.port}" if parsed.port is not None else ""
        return f"{parsed.scheme}://{rendered_host}{rendered_port}"

    def _publish_status(self) -> None:
        message = String()
        message.data = json.dumps(
            self._snapshot(), separators=(",", ":"), sort_keys=True
        )
        self._status_publisher.publish(message)


def main() -> None:
    rclpy.init()
    node = CvContractMonitor()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
