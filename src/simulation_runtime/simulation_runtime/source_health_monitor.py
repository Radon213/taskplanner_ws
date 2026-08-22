"""Uniform freshness monitoring for Taskplanner's optional input sources.

The monitor is deliberately observational.  It never republishes evidence and
never authorizes an action; it only exposes whether a source is usable now.
Receipt time is monotonic so replay clock jumps cannot hide a dead publisher.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

import rclpy
from rclpy.node import Node
from rclpy.qos import (
    DurabilityPolicy,
    HistoryPolicy,
    QoSProfile,
    ReliabilityPolicy,
)
from sensor_msgs.msg import CompressedImage

from surgical_msgs.msg import InputSourceStatus, VLMHealth, VLMResult

MISSING = "MISSING"
RECOVERING = "RECOVERING"
READY = "READY"
STALE = "STALE"
ERROR = "ERROR"
DISABLED = "DISABLED"

STATUS_CHECKPOINT_SEC = 1.0
CAMERA_INPUT_QOS_DEPTH = 1


def camera_input_qos() -> QoSProfile:
    """Match the latest-frame-only VIPLab operator preview contract."""

    return QoSProfile(
        history=HistoryPolicy.KEEP_LAST,
        depth=CAMERA_INPUT_QOS_DEPTH,
        reliability=ReliabilityPolicy.BEST_EFFORT,
        durability=DurabilityPolicy.VOLATILE,
    )


def status_semantic_signature(status: InputSourceStatus) -> tuple[object, ...]:
    """Return the fields whose change must be published without checkpoint delay."""

    return (
        str(status.source_id),
        str(status.modality),
        str(status.state),
        bool(status.healthy),
        int(status.epoch),
        str(status.error_code),
        str(status.detail),
    )


@dataclass
class StatusPublicationGate:
    """Per-source transition gate with sparse unchanged-state checkpoints."""

    checkpoint_sec: float = STATUS_CHECKPOINT_SEC

    def __post_init__(self) -> None:
        self.checkpoint_sec = max(0.0, float(self.checkpoint_sec))
        self._last_success: dict[str, tuple[tuple[object, ...], float]] = {}

    def due(
        self,
        source: str,
        status: InputSourceStatus,
        now_monotonic_sec: float,
    ) -> bool:
        previous = self._last_success.get(str(source))
        if previous is None:
            return True
        signature = status_semantic_signature(status)
        return bool(
            signature != previous[0]
            or float(now_monotonic_sec) - previous[1] >= self.checkpoint_sec
        )

    def commit(
        self,
        source: str,
        status: InputSourceStatus,
        now_monotonic_sec: float,
    ) -> None:
        self._last_success[str(source)] = (
            status_semantic_signature(status),
            float(now_monotonic_sec),
        )


@dataclass
class SourceTracker:
    source_id: str
    modality: str
    stale_after_sec: float
    recovery_samples: int = 2
    enabled: bool = True
    epoch: int = 0
    received_count: int = 0
    accepted_count: int = 0
    rejected_count: int = 0
    dropped_count: int = 0
    last_receipt_monotonic_sec: float | None = None
    last_source_stamp_sec: float | None = None
    samples_in_epoch: int = 0
    error_code: str = ""
    detail: str = "waiting_for_source"
    _stale_latched: bool = False

    def observe(
        self,
        *,
        now_monotonic_sec: float,
        source_stamp_sec: float | None,
    ) -> bool:
        """Record one observation and return whether its timestamp is usable."""

        self.received_count += 1
        if not self.enabled:
            self.rejected_count += 1
            self.detail = "source_disabled"
            return False

        if (
            source_stamp_sec is not None
            and math.isfinite(source_stamp_sec)
            and self.last_source_stamp_sec is not None
            and source_stamp_sec <= self.last_source_stamp_sec
        ):
            self.rejected_count += 1
            self.dropped_count += 1
            self.detail = "duplicate_or_out_of_order_stamp"
            return False

        if self.epoch == 0 or self._stale_latched or self.error_code:
            self.epoch += 1
            self.samples_in_epoch = 0
        self._stale_latched = False
        self.error_code = ""
        self.last_receipt_monotonic_sec = now_monotonic_sec
        if source_stamp_sec is not None and math.isfinite(source_stamp_sec):
            self.last_source_stamp_sec = source_stamp_sec
        self.accepted_count += 1
        self.samples_in_epoch += 1
        self.detail = "fresh_observation"
        return True

    def set_error(self, code: str, detail: str = "") -> None:
        self.error_code = str(code or "source_error")
        self.detail = str(detail or self.error_code)

    def snapshot(self, now_monotonic_sec: float) -> tuple[str, bool, float]:
        if not self.enabled:
            return DISABLED, False, -1.0
        if self.error_code:
            age = self._age(now_monotonic_sec)
            return ERROR, False, age
        if self.last_receipt_monotonic_sec is None:
            return MISSING, False, -1.0
        age = self._age(now_monotonic_sec)
        if age > self.stale_after_sec:
            self._stale_latched = True
            self.detail = "source_stale"
            return STALE, False, age
        if self.samples_in_epoch < max(1, self.recovery_samples):
            return RECOVERING, False, age
        return READY, True, age

    def _age(self, now_monotonic_sec: float) -> float:
        if self.last_receipt_monotonic_sec is None:
            return -1.0
        return max(0.0, now_monotonic_sec - self.last_receipt_monotonic_sec)


def _stamp_sec(stamp) -> float | None:
    seconds = float(stamp.sec) + float(stamp.nanosec) / 1_000_000_000.0
    return seconds if seconds > 0.0 else None


class SourceHealthMonitor(Node):
    def __init__(self) -> None:
        super().__init__("source_health_monitor")
        self.declare_parameter("flir_topic", "/surgery/images/flir/compressed")
        self.declare_parameter("cam4_topic", "/surgery/images/cam4/compressed")
        self.declare_parameter("vlm_result_topic", "/vlm/result")
        self.declare_parameter("vlm_health_topic", "/vlm/health")
        self.declare_parameter("flir_status_topic", "/input/flir/status")
        self.declare_parameter("cam4_status_topic", "/input/cam4/status")
        self.declare_parameter("vlm_status_topic", "/input/vlm/status")
        self.declare_parameter("camera_stale_after_sec", 1.0)
        self.declare_parameter("vlm_stale_after_sec", 3.0)
        self.declare_parameter("recovery_samples", 2)
        self.declare_parameter("enable_flir", True)
        self.declare_parameter("enable_cam4", True)
        self.declare_parameter("enable_vlm", True)

        recovery_samples = max(1, int(self.get_parameter("recovery_samples").value))
        camera_stale = max(
            0.1, float(self.get_parameter("camera_stale_after_sec").value)
        )
        self._trackers = {
            "flir": SourceTracker(
                "flir",
                "image",
                camera_stale,
                recovery_samples,
                bool(self.get_parameter("enable_flir").value),
            ),
            "cam4": SourceTracker(
                "cam4",
                "image",
                camera_stale,
                recovery_samples,
                bool(self.get_parameter("enable_cam4").value),
            ),
            "vlm": SourceTracker(
                "vlm",
                "vision_language_model",
                max(0.1, float(self.get_parameter("vlm_stale_after_sec").value)),
                1,
                bool(self.get_parameter("enable_vlm").value),
            ),
        }
        self._last_stamps = {key: None for key in self._trackers}
        self._vlm_health_blocked = False
        self._status_publication_gate = StatusPublicationGate()
        self._status_publishers = {
            "flir": self.create_publisher(
                InputSourceStatus,
                str(self.get_parameter("flir_status_topic").value),
                10,
            ),
            "cam4": self.create_publisher(
                InputSourceStatus,
                str(self.get_parameter("cam4_status_topic").value),
                10,
            ),
            "vlm": self.create_publisher(
                InputSourceStatus,
                str(self.get_parameter("vlm_status_topic").value),
                10,
            ),
        }
        self.create_subscription(
            CompressedImage,
            str(self.get_parameter("flir_topic").value),
            lambda message: self._on_image("flir", message),
            camera_input_qos(),
        )
        self.create_subscription(
            CompressedImage,
            str(self.get_parameter("cam4_topic").value),
            lambda message: self._on_image("cam4", message),
            camera_input_qos(),
        )
        self.create_subscription(
            VLMResult,
            str(self.get_parameter("vlm_result_topic").value),
            self._on_vlm_result,
            10,
        )
        self.create_subscription(
            VLMHealth,
            str(self.get_parameter("vlm_health_topic").value),
            self._on_vlm_health,
            10,
        )
        self.create_timer(0.25, self._publish)

    @staticmethod
    def _monotonic() -> float:
        return time.monotonic()

    def _observe(self, source: str, source_stamp_sec: float | None) -> None:
        accepted = self._trackers[source].observe(
            now_monotonic_sec=self._monotonic(),
            source_stamp_sec=source_stamp_sec,
        )
        if accepted:
            self._last_stamps[source] = source_stamp_sec

    def _on_image(self, source: str, message: CompressedImage) -> None:
        self._observe(source, _stamp_sec(message.header.stamp))

    def _on_vlm_result(self, message: VLMResult) -> None:
        if self._vlm_health_blocked:
            tracker = self._trackers["vlm"]
            tracker.received_count += 1
            tracker.rejected_count += 1
            tracker.dropped_count += 1
            tracker.detail = "vlm_result_rejected_while_health_blocked"
            return
        self._observe("vlm", _stamp_sec(message.stamp))

    def _on_vlm_health(self, message: VLMHealth) -> None:
        tracker = self._trackers["vlm"]
        if not message.connected:
            self._vlm_health_blocked = True
            tracker.set_error("vlm_disconnected", message.last_error)
        elif not message.healthy:
            self._vlm_health_blocked = True
            tracker.set_error("vlm_unhealthy", message.last_error)
        else:
            was_blocked = self._vlm_health_blocked
            self._vlm_health_blocked = False
            if was_blocked:
                tracker.error_code = ""
                tracker.detail = "vlm_health_recovered_waiting_for_result"
                tracker.samples_in_epoch = 0
                tracker._stale_latched = True

    def _status_message(
        self,
        source: str,
        tracker: SourceTracker,
        *,
        now_monotonic: float,
        now_stamp,
    ) -> InputSourceStatus:
        state, healthy, age = tracker.snapshot(now_monotonic)
        status = InputSourceStatus()
        status.stamp = now_stamp
        status.source_id = tracker.source_id
        status.modality = tracker.modality
        status.state = state
        status.healthy = healthy
        source_stamp = self._last_stamps[source]
        if source_stamp is not None:
            total_nanoseconds = max(
                0,
                round(source_stamp * 1_000_000_000),
            )
            whole, nanoseconds = divmod(
                total_nanoseconds,
                1_000_000_000,
            )
            status.last_observation_stamp.sec = whole
            status.last_observation_stamp.nanosec = nanoseconds
        status.age_sec = float(age)
        status.received_count = tracker.received_count
        status.accepted_count = tracker.accepted_count
        status.rejected_count = tracker.rejected_count
        status.epoch = tracker.epoch
        status.dropped_count = tracker.dropped_count
        status.error_code = tracker.error_code
        status.detail = tracker.detail
        return status

    def _publish_status_if_due(
        self,
        source: str,
        status: InputSourceStatus,
        *,
        now_monotonic: float,
    ) -> bool:
        gate = self._status_publication_gate
        if not gate.due(source, status, now_monotonic):
            return False
        try:
            self._status_publishers[source].publish(status)
        except Exception as exc:  # noqa: BLE001 - defensive ROS boundary
            self.get_logger().error(f"Unable to publish {source} input status: {exc}")
            return False
        gate.commit(source, status, now_monotonic)
        return True

    def _publish(self) -> None:
        # Keep freshness evaluation at 4 Hz so READY->STALE and recovery edges
        # retain their existing <=250 ms detection latency. Only unchanged
        # diagnostic snapshots are checkpointed at the slower cadence.
        now_monotonic = self._monotonic()
        now_stamp = self.get_clock().now().to_msg()
        for source, tracker in self._trackers.items():
            status = self._status_message(
                source,
                tracker,
                now_monotonic=now_monotonic,
                now_stamp=now_stamp,
            )
            self._publish_status_if_due(
                source,
                status,
                now_monotonic=now_monotonic,
            )


def main() -> None:
    rclpy.init()
    node = SourceHealthMonitor()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
