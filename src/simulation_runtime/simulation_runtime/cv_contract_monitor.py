"""Observational monitor for the pending VIPLab/CV-team interface.

Only standard ROS image messages are subscribed today.  Custom CV result
messages are inspected through the ROS graph by their fully-qualified type
name, which permits a useful preflight without claiming an unavailable IDL is
compatible.  The monitor never republishes camera data, CV evidence, pose data
or dummy samples.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import time
from typing import Any, Callable

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
    normalize_perception_backend,
)


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
        self.declare_parameter("status_topic", "/integration/cv_contract/status")
        self.declare_parameter(
            "cam4_rgb_topic", "/synced/cam_4/color/image_raw/compressed"
        )
        self.declare_parameter(
            "cam4_rgb_alias_topic", "/surgery/images/cam4/compressed"
        )
        self.declare_parameter(
            "cam4_camera_info_topic",
            "/synced/cam_4/color/camera_info",
        )
        self.declare_parameter(
            "cam4_aligned_depth_topic", "/synced/cam_4/depth/image_rect_raw"
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

        self._backend = normalize_perception_backend(
            self.get_parameter("perception_backend").value
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
            "cam4_aligned_depth": InputTracker(
                "cam4_aligned_depth",
                str(self.get_parameter("cam4_aligned_depth_topic").value),
                "sensor_msgs/msg/Image",
                "BEST_EFFORT/VOLATILE/KEEP_LAST(5)",
                "provider_encoding_units_and_sync_policy_pending",
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
        synced_rgb_qos = QoSProfile(
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
        self.create_subscription(
            CompressedImage,
            self._trackers["cam4_rgb"].topic,
            lambda message: self._trackers["cam4_rgb"].observe(
                message, validate_compressed_image
            ),
            synced_rgb_qos,
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
            Image,
            self._trackers["cam4_aligned_depth"].topic,
            lambda message: self._trackers["cam4_aligned_depth"].observe(
                message, validate_depth_image
            ),
            qos_profile_sensor_data,
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
            if not observed:
                state = "WAITING_FOR_EXTERNAL_PUBLISHER"
            elif len(observed) > 1:
                state = "AMBIGUOUS_PUBLISHERS"
            elif observed_types != [endpoint.message_type]:
                state = "TYPE_MISMATCH"
            elif local_rfdetr_present and self._backend == "local":
                state = "LOCAL_BACKEND_OWNS_COMPATIBILITY_TOPIC"
            elif local_rfdetr_present:
                state = "LOCAL_BACKEND_COLLISION"
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
        idl = self._external_idl_state()
        if self._backend == "external":
            readiness_state = "PENDING_EXTERNAL_IDL_AND_ADAPTER"
        elif self._backend == "disabled":
            readiness_state = "DISABLED"
        else:
            readiness_state = "LOCAL_BACKEND_ACTIVE_EXTERNAL_CONTRACT_MONITORED"
        return {
            "schema": CV_CONTRACT_SCHEMA,
            "contract_version": CV_CONTRACT_VERSION,
            "stamp_sec": round(
                self.get_clock().now().nanoseconds / 1_000_000_000.0,
                6,
            ),
            "perception_backend": self._backend,
            "readiness_state": readiness_state,
            "ready_for_external_evidence": False,
            "adapter_state": "NOT_IMPLEMENTED_PENDING_EXTERNAL_IDL",
            "policy_pending": {
                "rgb_depth_skew_limit": True,
                "source_stale_timeout": True,
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
