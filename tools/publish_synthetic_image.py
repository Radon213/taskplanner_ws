#!/usr/bin/env python3

from pathlib import Path
import sys

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import CompressedImage


class SyntheticImagePublisher(Node):
    def __init__(self, image_path: str):
        super().__init__("synthetic_image_publisher")

        self.image_path = Path(image_path).expanduser()
        if not self.image_path.is_file():
            raise FileNotFoundError(f"Image not found: {self.image_path}")

        suffix = self.image_path.suffix.lower()
        self.image_format = "jpeg" if suffix in [".jpg", ".jpeg"] else "png"
        self.image_bytes = self.image_path.read_bytes()

        self.pub = self.create_publisher(
            CompressedImage,
            "/surgery/images/synthetic/compressed",
            10,
        )

        self.timer = self.create_timer(1.0, self.publish_image)

        self.get_logger().info(f"Publishing image: {self.image_path}")
        self.get_logger().info("Topic: /surgery/images/synthetic/compressed")

    def publish_image(self):
        msg = CompressedImage()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = "isaac_sim_vlm_camera"
        msg.format = self.image_format
        msg.data = self.image_bytes
        self.pub.publish(msg)
        self.get_logger().info(
            f"Published {self.image_format} image, {len(self.image_bytes)} bytes"
        )


def main():
    if len(sys.argv) < 2:
        print("Usage: publish_synthetic_image.py <image_path>")
        sys.exit(1)

    rclpy.init()
    node = SyntheticImagePublisher(sys.argv[1])

    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
