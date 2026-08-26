"""Bounded, read-only HLS projection of the public FLIR camera topic.

The gateway deliberately subscribes to the already gated public camera alias.
It never publishes ROS messages and never exposes a control service. A
single-slot mailbox prevents a slow encoder or TV from applying backpressure to
the ROS callback thread.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from typing import Callable

import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import CompressedImage


@dataclass(frozen=True)
class EncoderSettings:
    output_root: Path
    ffmpeg_binary: str = "ffmpeg"
    width: int = 1280
    height: int = 720
    frame_rate: int = 5
    bitrate_kbps: int = 2400
    segment_seconds: int = 1
    playlist_segments: int = 3


def build_ffmpeg_command(settings: EncoderSettings) -> tuple[str, ...]:
    """Build a webOS-compatible, low-latency H.264/HLS command."""

    width = max(320, min(3840, int(settings.width)))
    height = max(180, min(2160, int(settings.height)))
    frame_rate = max(1, min(60, int(settings.frame_rate)))
    bitrate = max(256, min(40000, int(settings.bitrate_kbps)))
    segment_seconds = max(1, min(10, int(settings.segment_seconds)))
    playlist_segments = max(2, min(12, int(settings.playlist_segments)))
    keyframe_interval = max(frame_rate, frame_rate * segment_seconds)
    output_root = settings.output_root.resolve()
    playlist = output_root / "flir.m3u8"
    segments = output_root / "flir-%08d.ts"
    scale = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black"
    )
    return (
        settings.ffmpeg_binary,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostdin",
        "-fflags",
        "+genpts+nobuffer",
        "-f",
        "image2pipe",
        "-framerate",
        str(frame_rate),
        "-vcodec",
        "mjpeg",
        "-i",
        "pipe:0",
        "-an",
        "-vf",
        scale,
        "-r",
        str(frame_rate),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-tune",
        "zerolatency",
        "-profile:v",
        "main",
        "-level",
        "4.0",
        "-pix_fmt",
        "yuv420p",
        "-b:v",
        f"{bitrate}k",
        "-maxrate",
        f"{round(bitrate * 1.25)}k",
        "-bufsize",
        f"{bitrate * 2}k",
        "-g",
        str(keyframe_interval),
        "-keyint_min",
        str(keyframe_interval),
        "-sc_threshold",
        "0",
        "-f",
        "hls",
        "-hls_time",
        str(segment_seconds),
        "-hls_list_size",
        str(playlist_segments),
        "-hls_delete_threshold",
        "2",
        "-hls_flags",
        "delete_segments+independent_segments+omit_endlist+temp_file",
        "-hls_segment_filename",
        str(segments),
        str(playlist),
    )


class LatestFrameMailbox:
    """Thread-safe single-frame mailbox with explicit replacement accounting."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._frame: bytes | None = None
        self._closed = False

    def submit(self, frame: bytes) -> bool:
        """Store the newest frame and return True when an older frame was dropped."""

        with self._condition:
            if self._closed:
                return False
            replaced = self._frame is not None
            self._frame = frame
            self._condition.notify()
            return replaced

    def take(self, timeout_sec: float = 1.0) -> bytes | None:
        with self._condition:
            if self._frame is None and not self._closed:
                self._condition.wait(max(0.0, timeout_sec))
            frame = self._frame
            self._frame = None
            return frame

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._frame = None
            self._condition.notify_all()


def atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(path)


class MonitorMediaGateway(Node):
    """Project a latest-only compressed ROS camera stream to local HLS files."""

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__("monitor_media_gateway")
        self._monotonic = monotonic
        output_root = Path(
            str(self.declare_parameter("output_root", "/var/run/taskplanner-monitor-media").value)
        ).resolve()
        self._input_topic = str(
            self.declare_parameter(
                "input_topic", "/surgery/images/flir/compressed"
            ).value
        ).strip()
        self._max_frame_bytes = max(
            1024,
            int(self.declare_parameter("max_frame_bytes", 16 * 1024 * 1024).value),
        )
        self._stale_after_sec = max(
            1.0,
            float(self.declare_parameter("stale_after_sec", 5.0).value),
        )
        self._settings = EncoderSettings(
            output_root=output_root,
            ffmpeg_binary=str(self.declare_parameter("ffmpeg_binary", "ffmpeg").value),
            width=int(self.declare_parameter("width", 1280).value),
            height=int(self.declare_parameter("height", 720).value),
            frame_rate=int(self.declare_parameter("frame_rate", 5).value),
            bitrate_kbps=int(self.declare_parameter("bitrate_kbps", 2400).value),
            segment_seconds=int(self.declare_parameter("segment_seconds", 1).value),
            playlist_segments=int(self.declare_parameter("playlist_segments", 3).value),
        )
        output_root.mkdir(parents=True, exist_ok=True)
        self._health_path = output_root / "health.json"
        self._playlist_path = output_root / "flir.m3u8"
        self._mailbox = LatestFrameMailbox()
        self._encoder: subprocess.Popen[bytes] | None = None
        self._encoder_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._last_encoder_start_at = 0.0
        self._last_frame_at: float | None = None
        self._last_error = ""
        self._frames_received = 0
        self._frames_encoded = 0
        self._frames_dropped = 0
        self._invalid_frames = 0
        self._worker = threading.Thread(
            target=self._encode_loop,
            name="monitor-hls-encoder",
            daemon=True,
        )
        qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
            reliability=ReliabilityPolicy.BEST_EFFORT,
            durability=DurabilityPolicy.VOLATILE,
        )
        self._subscription = self.create_subscription(
            CompressedImage,
            self._input_topic,
            self._on_frame,
            qos,
        )
        self._worker.start()
        self._health_timer = self.create_timer(1.0, self._write_health)
        self._write_health()
        self.get_logger().info(
            f"Read-only monitor media gateway waiting on {self._input_topic}"
        )

    def _on_frame(self, message: CompressedImage) -> None:
        image_format = str(message.format or "").strip().lower()
        payload = bytes(message.data)
        if "jpeg" not in image_format and "jpg" not in image_format:
            self._invalid_frames += 1
            self._last_error = f"unsupported image format: {image_format or 'unknown'}"
            return
        if not payload or len(payload) > self._max_frame_bytes:
            self._invalid_frames += 1
            self._last_error = "compressed frame is empty or exceeds max_frame_bytes"
            return
        self._frames_received += 1
        self._last_frame_at = self._monotonic()
        if self._mailbox.submit(payload):
            self._frames_dropped += 1

    def _clean_stale_outputs(self) -> None:
        for pattern in ("flir.m3u8", "flir-*.ts", ".flir*.tmp"):
            for path in self._settings.output_root.glob(pattern):
                if path.is_file():
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass

    def _start_encoder(self) -> subprocess.Popen[bytes] | None:
        now = self._monotonic()
        if now - self._last_encoder_start_at < 1.0:
            return None
        self._last_encoder_start_at = now
        self._clean_stale_outputs()
        try:
            process = subprocess.Popen(
                build_ffmpeg_command(self._settings),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as error:
            self._last_error = f"encoder start failed: {error}"
            self.get_logger().error(self._last_error)
            return None
        self._last_error = ""
        self.get_logger().info("H.264/HLS encoder started")
        return process

    def _ensure_encoder(self) -> subprocess.Popen[bytes] | None:
        with self._encoder_lock:
            if self._encoder is not None and self._encoder.poll() is None:
                return self._encoder
            if self._encoder is not None:
                self._encoder = None
            self._encoder = self._start_encoder()
            return self._encoder

    def _encode_loop(self) -> None:
        while not self._stop_event.is_set():
            frame = self._mailbox.take(0.5)
            if frame is None:
                continue
            encoder = self._ensure_encoder()
            if encoder is None or encoder.stdin is None:
                self._frames_dropped += 1
                continue
            try:
                encoder.stdin.write(frame)
                encoder.stdin.flush()
                self._frames_encoded += 1
            except (BrokenPipeError, OSError, ValueError) as error:
                self._frames_dropped += 1
                self._last_error = f"encoder write failed: {error}"
                with self._encoder_lock:
                    if self._encoder is encoder:
                        self._encoder = None
                self._terminate_encoder(encoder)

    def _encoder_running(self) -> bool:
        with self._encoder_lock:
            return self._encoder is not None and self._encoder.poll() is None

    def _health_payload(self) -> dict[str, object]:
        now = self._monotonic()
        age = None if self._last_frame_at is None else max(0.0, now - self._last_frame_at)
        playlist_ready = self._playlist_path.is_file() and self._playlist_path.stat().st_size > 0
        encoder_running = self._encoder_running()
        if self._last_frame_at is None:
            state = "waiting_for_camera"
        elif age is not None and age > self._stale_after_sec:
            state = "camera_stale"
        elif playlist_ready and encoder_running:
            state = "streaming"
        elif self._last_error:
            state = "encoder_error"
        else:
            state = "starting_encoder"
        return {
            "service": "taskplanner-monitor-media",
            "state": state,
            "input_topic": self._input_topic,
            "playlist": "flir.m3u8",
            "playlist_ready": playlist_ready,
            "encoder_running": encoder_running,
            "last_frame_age_sec": None if age is None else round(age, 3),
            "frames_received": self._frames_received,
            "frames_encoded": self._frames_encoded,
            "frames_dropped": self._frames_dropped,
            "invalid_frames": self._invalid_frames,
            "last_error": self._last_error[-512:],
            "updated_unix_sec": round(time.time(), 3),
        }

    def _write_health(self) -> None:
        try:
            atomic_write_json(self._health_path, self._health_payload())
        except OSError as error:
            self.get_logger().error(f"could not write media health: {error}")

    @staticmethod
    def _terminate_encoder(encoder: subprocess.Popen[bytes]) -> None:
        if encoder.stdin is not None:
            try:
                encoder.stdin.close()
            except OSError:
                pass
        if encoder.poll() is None:
            encoder.terminate()
            try:
                encoder.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                encoder.kill()
                encoder.wait(timeout=1.0)

    def destroy_node(self) -> bool:
        self._stop_event.set()
        self._mailbox.close()
        self._worker.join(timeout=2.0)
        with self._encoder_lock:
            encoder = self._encoder
            self._encoder = None
        if encoder is not None:
            self._terminate_encoder(encoder)
        self._write_health()
        return super().destroy_node()


def main(args: list[str] | None = None) -> None:
    rclpy.init(args=args)
    node = MonitorMediaGateway()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
