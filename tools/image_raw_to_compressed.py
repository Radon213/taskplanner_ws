#!/usr/bin/env python3
import argparse
from io import BytesIO

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
from PIL import Image as PILImage

from sensor_msgs.msg import Image, CompressedImage


class ImageRawToCompressed(Node):
    def __init__(
        self,
        input_topic: str,
        output_topic: str,
        quality: int,
        reliability: str,
    ):
        super().__init__("image_raw_to_compressed")
        self._quality = quality

        input_qos = QoSProfile(
            reliability=(
                ReliabilityPolicy.RELIABLE
                if reliability == "reliable"
                else ReliabilityPolicy.BEST_EFFORT
            ),
            history=HistoryPolicy.KEEP_LAST,
            depth=5,
        )

        self.sub = self.create_subscription(
            Image,
            input_topic,
            self.image_callback,
            input_qos,
        )

        self.pub = self.create_publisher(
            CompressedImage,
            output_topic,
            10,
        )

        self.get_logger().info(f"Subscribing: {input_topic}")
        self.get_logger().info(f"Publishing : {output_topic}")

    def image_callback(self, msg: Image):
        try:
            image = self._decode_image(msg)

            if image.mode != "RGB":
                image = image.convert("RGB")

            encoded = BytesIO()
            image.save(encoded, format="JPEG", quality=self._quality)

            out = CompressedImage()
            out.header = msg.header
            out.format = "jpeg"
            out.data = encoded.getvalue()

            self.pub.publish(out)
            self.get_logger().debug(
                f"Published compressed image: {msg.width}x{msg.height}, "
                f"encoding={msg.encoding}, bytes={len(out.data)}"
            )

        except Exception as e:
            self.get_logger().error(f"Failed to convert image: {e}")

    @staticmethod
    def _decode_image(msg: Image) -> PILImage.Image:
        width = int(msg.width)
        height = int(msg.height)
        encoding = str(msg.encoding).lower()
        raw_modes = {
            "rgb8": ("RGB", "RGB", 3),
            "bgr8": ("RGB", "BGR", 3),
            "rgba8": ("RGBA", "RGBA", 4),
            "bgra8": ("RGBA", "BGRA", 4),
            "mono8": ("L", "L", 1),
            "8uc1": ("L", "L", 1),
        }
        if encoding not in raw_modes:
            raise ValueError(f"unsupported encoding: {msg.encoding}")
        mode, raw_mode, bytes_per_pixel = raw_modes[encoding]
        row_stride = int(msg.step) or width * bytes_per_pixel
        minimum_stride = width * bytes_per_pixel
        if row_stride < minimum_stride:
            raise ValueError(
                f"invalid row stride {row_stride} for {width}px {encoding} image"
            )
        data = bytes(msg.data)
        required_bytes = row_stride * height
        if len(data) < required_bytes:
            raise ValueError(
                f"image payload too short: {len(data)} < {required_bytes} bytes"
            )
        return PILImage.frombytes(
            mode,
            (width, height),
            data,
            "raw",
            raw_mode,
            row_stride,
            1,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-topic", default="/surgery/images/field/image_raw")
    parser.add_argument("--output-topic", default="/surgery/images/field/compressed")
    parser.add_argument("--quality", type=int, default=90)
    parser.add_argument(
        "--reliability",
        choices=("best_effort", "reliable"),
        default="best_effort",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    rclpy.init()
    node = ImageRawToCompressed(
        args.input_topic,
        args.output_topic,
        max(1, min(100, args.quality)),
        args.reliability,
    )

    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
