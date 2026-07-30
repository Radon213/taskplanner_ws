"""HTTP snapshot bridge for field-camera style VLM inputs."""

from __future__ import annotations

import imghdr
import time

import requests
import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import CompressedImage


class SnapshotSequenceGate:
    """Reject duplicate frames while recovering from source process restarts."""

    def __init__(self) -> None:
        self.last_sequence = -1
        self.last_source_instance = ""

    def accept(self, source_instance: str, sequence: int) -> bool:
        if source_instance != self.last_source_instance:
            self.last_sequence = -1
        elif not source_instance and sequence < self.last_sequence:
            self.last_sequence = -1
        if sequence <= self.last_sequence:
            return False
        self.last_sequence = sequence
        self.last_source_instance = source_instance
        return True


class SnapshotBridgeNode(Node):
    def __init__(self) -> None:
        super().__init__("snapshot_bridge")
        self._snapshot_url = str(self.declare_parameter("snapshot_url", "").value).strip()
        self._poll_period_sec = float(self.declare_parameter("poll_period_sec", 0.75).value)
        self._timeout_sec = float(self.declare_parameter("timeout_sec", 2.5).value)
        self._max_source_age_sec = float(
            self.declare_parameter("max_source_age_sec", 3.0).value
        )
        self._output_topic = str(
            self.declare_parameter("output_topic", "/surgery/images/field/compressed").value
        )
        self._publisher = self.create_publisher(
            CompressedImage,
            self._output_topic,
            qos_profile_sensor_data,
        )
        self._session = requests.Session()
        self._last_success_sec = 0.0
        self._sequence_gate = SnapshotSequenceGate()
        self._timer = self.create_timer(self._poll_period_sec, self._tick)

    def destroy_node(self):
        self._session.close()
        return super().destroy_node()

    def _tick(self) -> None:
        if not self._snapshot_url:
            return
        try:
            response = self._session.get(self._snapshot_url, timeout=self._timeout_sec)
            response.raise_for_status()
            payload = bytes(response.content)
            if not payload:
                raise RuntimeError("snapshot response was empty")
            source_age_sec = float(response.headers["X-Source-Age-Sec"])
            sequence = int(response.headers["X-Frame-Sequence"])
            source_instance = str(response.headers.get("X-Source-Instance", "")).strip()
            if source_age_sec > self._max_source_age_sec:
                raise RuntimeError(
                    f"source frame is stale: {source_age_sec:.3f}s "
                    f"> {self._max_source_age_sec:.3f}s"
                )
            if not self._sequence_gate.accept(source_instance, sequence):
                return
            image_format = imghdr.what(None, h=payload) or "jpeg"
            msg = CompressedImage()
            msg.header.stamp = self.get_clock().now().to_msg()
            msg.format = "png" if image_format == "png" else "jpeg"
            msg.data = payload
            self._publisher.publish(msg)
            self._last_success_sec = time.monotonic()
        except Exception as exc:
            self.get_logger().warn(f"snapshot fetch failed: {exc}", throttle_duration_sec=5.0)


def main() -> None:
    rclpy.init()
    node = SnapshotBridgeNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            node.destroy_node()
        finally:
            try:
                if rclpy.ok():
                    rclpy.shutdown()
            except Exception:
                pass
