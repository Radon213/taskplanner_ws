"""HTTP snapshot bridge for field-camera style VLM inputs."""

from __future__ import annotations

import imghdr
import time

import requests
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import CompressedImage


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
        self._publisher = self.create_publisher(CompressedImage, self._output_topic, 10)
        self._session = requests.Session()
        self._last_success_sec = 0.0
        self._last_sequence = -1
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
            if source_age_sec > self._max_source_age_sec:
                raise RuntimeError(
                    f"source frame is stale: {source_age_sec:.3f}s "
                    f"> {self._max_source_age_sec:.3f}s"
                )
            if sequence <= self._last_sequence:
                return
            image_format = imghdr.what(None, h=payload) or "jpeg"
            msg = CompressedImage()
            msg.header.stamp = self.get_clock().now().to_msg()
            msg.format = "png" if image_format == "png" else "jpeg"
            msg.data = payload
            self._publisher.publish(msg)
            self._last_sequence = sequence
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
