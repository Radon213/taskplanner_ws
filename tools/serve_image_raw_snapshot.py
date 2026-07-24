#!/usr/bin/env python3
"""Serve the latest ROS Image frame as a JPEG snapshot over HTTP."""

from __future__ import annotations

import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import threading
import time
import uuid

from PIL import Image as PILImage
import rclpy
from rclpy.node import Node
from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import Image


class LatestSnapshot:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jpeg = b""
        self._stamp_sec = 0.0
        self._received_monotonic_sec = 0.0
        self._sequence = 0
        self._frame_id = ""
        self._width = 0
        self._height = 0
        self._encoding = ""
        self._error = ""
        self._source_instance_id = uuid.uuid4().hex

    def update(self, jpeg: bytes, msg: Image) -> None:
        stamp = float(msg.header.stamp.sec) + float(msg.header.stamp.nanosec) / 1_000_000_000.0
        with self._lock:
            self._jpeg = jpeg
            self._stamp_sec = stamp or time.time()
            self._received_monotonic_sec = time.monotonic()
            self._sequence += 1
            self._frame_id = str(msg.header.frame_id)
            self._width = int(msg.width)
            self._height = int(msg.height)
            self._encoding = str(msg.encoding)
            self._error = ""

    def set_error(self, error: str) -> None:
        with self._lock:
            self._error = error

    def read(self) -> tuple[bytes, dict[str, object]]:
        with self._lock:
            return bytes(self._jpeg), self._status_locked()

    def _status_locked(self) -> dict[str, object]:
        age = (
            time.monotonic() - self._received_monotonic_sec
            if self._received_monotonic_sec
            else None
        )
        return {
            "has_frame": bool(self._jpeg),
            "age_sec": age,
            "stamp_sec": self._stamp_sec,
            "sequence": self._sequence,
            "frame_id": self._frame_id,
            "width": self._width,
            "height": self._height,
            "encoding": self._encoding,
            "jpeg_bytes": len(self._jpeg),
            "last_error": self._error,
            "source_instance_id": self._source_instance_id,
        }

    def status(self) -> dict[str, object]:
        with self._lock:
            return self._status_locked()


class ImageRawSnapshotServer(Node):
    def __init__(
        self,
        image_topic: str,
        quality: int,
        snapshot: LatestSnapshot,
        reliability: str = "best_effort",
    ) -> None:
        super().__init__("image_raw_snapshot_server")
        self._quality = quality
        self._snapshot = snapshot
        qos = QoSProfile(
            reliability=(
                ReliabilityPolicy.RELIABLE
                if reliability == "reliable"
                else ReliabilityPolicy.BEST_EFFORT
            ),
            history=HistoryPolicy.KEEP_LAST,
            depth=5,
        )
        self.create_subscription(Image, image_topic, self._on_image, qos)
        self.get_logger().info(f"Subscribing: {image_topic}")

    def _on_image(self, msg: Image) -> None:
        try:
            image = decode_image(msg)
            if image.mode != "RGB":
                image = image.convert("RGB")
            encoded = BytesIO()
            image.save(encoded, format="JPEG", quality=self._quality)
            self._snapshot.update(encoded.getvalue(), msg)
        except Exception as exc:
            self._snapshot.set_error(str(exc))
            self.get_logger().warn(f"Failed to encode snapshot: {exc}", throttle_duration_sec=5.0)


def decode_image(msg: Image) -> PILImage.Image:
    width = int(msg.width)
    height = int(msg.height)
    encoding = str(msg.encoding).lower()
    data = bytes(msg.data)
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


def make_handler(snapshot: LatestSnapshot, max_frame_age_sec: float):
    class SnapshotHandler(BaseHTTPRequestHandler):
        server_version = "ImageRawSnapshotHTTP/1.0"

        def do_GET(self) -> None:
            if self.path in {"/", "/health", "/status"}:
                self._send_json(snapshot.status())
                return
            if self.path not in {"/snapshot.jpg", "/snapshot.jpeg"}:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            jpeg, status = snapshot.read()
            if not jpeg:
                self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "no frame received yet")
                return
            age_sec = status.get("age_sec")
            if age_sec is None or float(age_sec) > max_frame_age_sec:
                self.send_error(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    f"source frame is stale (age={age_sec})",
                )
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(jpeg)))
            self.send_header("X-Source-Stamp-Sec", str(status["stamp_sec"]))
            self.send_header("X-Frame-Sequence", str(status["sequence"]))
            self.send_header("X-Source-Instance", str(status["source_instance_id"]))
            self.send_header("X-Source-Age-Sec", f"{float(age_sec):.6f}")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(jpeg)

        def log_message(self, fmt: str, *args) -> None:
            return

        def _send_json(self, payload: dict[str, object]) -> None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return SnapshotHandler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image-topic", default="/surgery/images/field/image_raw")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--quality", type=int, default=90)
    parser.add_argument("--max-frame-age-sec", type=float, default=3.0)
    parser.add_argument(
        "--reliability",
        choices=("best_effort", "reliable"),
        default="best_effort",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    snapshot = LatestSnapshot()
    httpd = ThreadingHTTPServer(
        (args.host, args.port),
        make_handler(snapshot, max(0.1, float(args.max_frame_age_sec))),
    )
    http_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    http_thread.start()
    print(f"Snapshot URL: http://{args.host}:{args.port}/snapshot.jpg", flush=True)
    print(f"Status URL  : http://{args.host}:{args.port}/status", flush=True)

    rclpy.init()
    node = ImageRawSnapshotServer(
        args.image_topic,
        args.quality,
        snapshot,
        reliability=args.reliability,
    )
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
