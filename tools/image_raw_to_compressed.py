#!/usr/bin/env python3
from io import BytesIO

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
from PIL import Image as PILImage

from sensor_msgs.msg import Image, CompressedImage


class ImageRawToCompressed(Node):
    def __init__(self):
        super().__init__("image_raw_to_compressed")

        qos = QoSProfile(
            reliability=ReliabilityPolicy.RELIABLE,
            history=HistoryPolicy.KEEP_LAST,
            depth=10,
        )

        self.sub = self.create_subscription(
            Image,
            "/surgery/images/field/image_raw",
            self.image_callback,
            qos,
        )

        self.pub = self.create_publisher(
            CompressedImage,
            "/surgery/images/field/compressed",
            qos,
        )

        self.get_logger().info("Subscribing: /surgery/images/field/image_raw")
        self.get_logger().info("Publishing : /surgery/images/field/compressed")

    def image_callback(self, msg: Image):
        try:
            height = msg.height
            width = msg.width
            encoding = msg.encoding.lower()

            image = self._decode_image(width, height, encoding, bytes(msg.data))
            if image is None:
                self.get_logger().warn(f"Unsupported encoding: {msg.encoding}")
                return

            if image.mode != "RGB":
                image = image.convert("RGB")

            encoded = BytesIO()
            image.save(encoded, format="JPEG", quality=90)

            out = CompressedImage()
            out.header = msg.header
            out.format = "jpeg"
            out.data = encoded.getvalue()

            self.pub.publish(out)
            self.get_logger().info(
                f"Published compressed image: {width}x{height}, encoding={msg.encoding}, bytes={len(out.data)}"
            )

        except Exception as e:
            self.get_logger().error(f"Failed to convert image: {e}")

    @staticmethod
    def _decode_image(width: int, height: int, encoding: str, data: bytes) -> PILImage.Image | None:
        if encoding == "rgb8":
            return PILImage.frombytes("RGB", (width, height), data)
        if encoding == "bgr8":
            return PILImage.frombytes("RGB", (width, height), data, "raw", "BGR")
        if encoding == "rgba8":
            return PILImage.frombytes("RGBA", (width, height), data)
        if encoding == "bgra8":
            return PILImage.frombytes("RGBA", (width, height), data, "raw", "BGRA")
        if encoding in {"mono8", "8uc1"}:
            return PILImage.frombytes("L", (width, height), data)
        return None


def main():
    rclpy.init()
    node = ImageRawToCompressed()

    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
