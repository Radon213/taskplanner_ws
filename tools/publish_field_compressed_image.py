#!/usr/bin/env python3
from pathlib import Path
import sys

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import CompressedImage


class FieldCompressedImagePublisher(Node):
    def __init__(self, image_path: str):
        super().__init__("field_compressed_image_publisher")

        self.image_path = Path(image_path)
        if not self.image_path.is_file():
            raise FileNotFoundError(f"Image not found: {self.image_path}")

        suffix = self.image_path.suffix.lower()
        self.image_format = "jpeg" if suffix in [".jpg", ".jpeg"] else "png"
        self.image_bytes = self.image_path.read_bytes()

        self.pub = self.create_publisher(
            CompressedImage,
            "/surgery/images/field/compressed",
            10,
        )

        self.timer = self.create_timer(1.0, self.publish_image)

        self.get_logger().info(f"Publishing image: {self.image_path}")
        self.get_logger().info("Topic: /surgery/images/field/compressed")
        self.get_logger().info(
            f"Format: {self.image_format}, bytes: {len(self.image_bytes)}"
        )

    def publish_image(self):
        msg = CompressedImage()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = "vlm_overview_camera"
        msg.format = self.image_format
        msg.data = self.image_bytes

        self.pub.publish(msg)
        self.get_logger().info("Published field image")


def main():
    if len(sys.argv) < 2:
        print("Usage: publish_field_compressed_image.py <image_path>")
        sys.exit(1)

    rclpy.init()
    node = FieldCompressedImagePublisher(sys.argv[1])

    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
