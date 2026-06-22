"""Blank CompressedImage publisher for image-input-unavailable VLM testing."""

from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageDraw, ImageFont
import rclpy
from rcl_interfaces.msg import SetParametersResult
from rclpy.node import Node
from sensor_msgs.msg import CompressedImage


class NoImageCameraNode(Node):
    def __init__(self) -> None:
        super().__init__("no_image_camera")
        self.declare_parameter("image_topic", "/surgery/images/field/compressed")
        self.declare_parameter("width", 1024)
        self.declare_parameter("height", 576)
        self.declare_parameter("fps", 30.0)
        self.declare_parameter("label", "No image")
        self.declare_parameter("jpeg_quality", 88)

        self._image_topic = str(self.get_parameter("image_topic").value)
        self._width = int(self.get_parameter("width").value)
        self._height = int(self.get_parameter("height").value)
        self._fps = float(self.get_parameter("fps").value)
        self._label = str(self.get_parameter("label").value)
        self._jpeg_quality = int(self.get_parameter("jpeg_quality").value)
        self._publisher = self.create_publisher(CompressedImage, self._image_topic, 10)
        self._jpeg_payload = self._render_payload()
        self._timer = self.create_timer(self._period_sec(), self._publish)
        self.add_on_set_parameters_callback(self._on_parameters_changed)

    def _period_sec(self) -> float:
        return 1.0 / max(self._fps, 1.0)

    def _on_parameters_changed(self, params):
        rebuild_image = False
        rebuild_timer = False
        for parameter in params:
            if parameter.name == "width":
                self._width = int(parameter.value)
                rebuild_image = True
            elif parameter.name == "height":
                self._height = int(parameter.value)
                rebuild_image = True
            elif parameter.name == "label":
                self._label = str(parameter.value)
                rebuild_image = True
            elif parameter.name == "jpeg_quality":
                self._jpeg_quality = int(parameter.value)
                rebuild_image = True
            elif parameter.name == "fps":
                self._fps = float(parameter.value)
                rebuild_timer = True

        if rebuild_image:
            self._jpeg_payload = self._render_payload()
        if rebuild_timer:
            self._timer.cancel()
            self._timer = self.create_timer(self._period_sec(), self._publish)
        return SetParametersResult(successful=True)

    def _render_payload(self) -> bytes:
        image = Image.new("RGB", (self._width, self._height), "#000000")
        draw = ImageDraw.Draw(image)
        font = self._font()
        text_bbox = draw.textbbox((0, 0), self._label, font=font)
        text_width = text_bbox[2] - text_bbox[0]
        text_height = text_bbox[3] - text_bbox[1]
        x = (self._width - text_width) // 2
        y = (self._height - text_height) // 2
        draw.text((x, y), self._label, fill="#ffffff", font=font)
        buffer = BytesIO()
        image.save(buffer, format="JPEG", quality=max(1, min(self._jpeg_quality, 100)))
        return buffer.getvalue()

    def _font(self):
        for path in (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ):
            try:
                return ImageFont.truetype(path, max(24, self._width // 16))
            except OSError:
                continue
        return ImageFont.load_default()

    def _publish(self) -> None:
        msg = CompressedImage()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = "no_image_camera"
        msg.format = "jpeg"
        msg.data = self._jpeg_payload
        self._publisher.publish(msg)


def main() -> None:
    rclpy.init()
    node = NoImageCameraNode()
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
