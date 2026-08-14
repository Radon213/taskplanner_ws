"""ROS bridge for the local RF-DETR preprocessing service."""

from __future__ import annotations

import base64
from collections import deque
from dataclasses import dataclass
import json
import threading
import time
from typing import Any

import requests
import rclpy
from rclpy.node import Node
from rclpy.qos import (
    DurabilityPolicy,
    HistoryPolicy,
    QoSProfile,
    ReliabilityPolicy,
    qos_profile_sensor_data,
)
from sensor_msgs.msg import CompressedImage
from std_msgs.msg import String
from std_srvs.srv import SetBool
from surgical_msgs.msg import ToolObservation

from .rfdetr_contract import (
    Cam4MayoPlacementTracker,
    parse_cam4_semantics_json,
)


@dataclass(frozen=True, slots=True)
class BufferedFrame:
    received_monotonic: float
    stamp_sec: int
    stamp_nanosec: int
    frame_id: str
    format: str
    data: bytes

    @property
    def source_stamp_sec(self) -> float:
        return (
            float(self.stamp_sec)
            + float(self.stamp_nanosec) / 1_000_000_000.0
        )


def closest_aligned_frame(
    frames: list[BufferedFrame] | deque[BufferedFrame],
    reference_stamp_sec: float,
    max_skew_sec: float,
) -> BufferedFrame | None:
    if not frames:
        return None
    closest = min(
        frames,
        key=lambda frame: abs(frame.source_stamp_sec - reference_stamp_sec),
    )
    if abs(closest.source_stamp_sec - reference_stamp_sec) > max_skew_sec:
        return None
    return closest


def _nonnegative_float(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    if result < 0.0 or result != result or result in (float("inf"), float("-inf")):
        return 0.0
    return round(result, 3)


def build_contract_diagnostics(
    raw: Any,
    *,
    cam4: BufferedFrame | None,
    sequence: int,
    source_to_output_latency_ms: float,
) -> dict[str, Any]:
    """Project the local service result onto the CV-team diagnostics schema."""

    payload = raw if isinstance(raw, dict) else {}
    cam4_payload = payload.get("cam4")
    cam4_diag = cam4_payload if isinstance(cam4_payload, dict) else {}
    instances = cam4_diag.get("instances")
    instance_rows = instances if isinstance(instances, list) else []
    if cam4 is None:
        stamp_sec = 0
        stamp_nanosec = 0
        frame_id = ""
        observation_id = ""
        error_code = "NO_ALIGNED_CAM4"
        error_message = "no CAM4 frame satisfied the alignment policy"
    else:
        stamp_sec = int(cam4.stamp_sec)
        stamp_nanosec = int(cam4.stamp_nanosec)
        frame_id = str(cam4.frame_id)
        observation_id = f"cam4:{stamp_sec}:{stamp_nanosec}"
        error_code = ""
        error_message = ""
    return {
        "schema": "pnu.rfdetr_diagnostics.v2",
        "view": "cam4",
        "source_stamp_sec": stamp_sec,
        "source_stamp_nanosec": stamp_nanosec,
        "frame_id": frame_id,
        "observation_id": observation_id,
        "sequence": max(0, int(sequence)),
        "decode_latency_ms": _nonnegative_float(payload.get("decode_latency_ms")),
        "depth_to_xyz_latency_ms": 0.0,
        "inference_latency_ms": _nonnegative_float(
            cam4_diag.get("inference_latency_ms")
        ),
        "pose_latency_ms": 0.0,
        "render_encode_latency_ms": _nonnegative_float(
            payload.get("render_encode_latency_ms")
        ),
        "source_to_output_latency_ms": _nonnegative_float(
            source_to_output_latency_ms
        ),
        "queue_age_ms": 0.0,
        "dropped_frames": 0,
        "instance_count": len(instance_rows),
        "valid_pose_count": 0,
        "endpoint_sign_low_count": 0,
        "model_version": str(cam4_diag.get("model", "RFDETRSmall"))[:120],
        "calibration_version": "",
        "error_code": error_code,
        "error_message": error_message,
    }


class RFDETRBridgeNode(Node):
    def __init__(self) -> None:
        super().__init__("rfdetr_perception_bridge")
        self._service_url = str(
            self.declare_parameter(
                "service_url",
                "http://127.0.0.1:8010",
            ).value
        ).rstrip("/")
        self._flir_input_topic = str(
            self.declare_parameter(
                "flir_input_topic",
                "/surgery/images/flir/compressed",
            ).value
        )
        self._cam4_input_topic = str(
            self.declare_parameter(
                "cam4_input_topic",
                "/surgery/images/cam4/compressed",
            ).value
        )
        self._flir_output_topic = str(
            self.declare_parameter(
                "flir_output_topic",
                "/surgery/images/flir/segmented/compressed",
            ).value
        )
        self._cam4_output_topic = str(
            self.declare_parameter(
                "cam4_output_topic",
                "/surgery/images/cam4/detected/compressed",
            ).value
        )
        self._flir_overlay_topic = str(
            self.declare_parameter(
                "flir_overlay_topic",
                "/surgery/images/flir/segmentation_overlay/compressed",
            ).value
        )
        self._cam4_overlay_topic = str(
            self.declare_parameter(
                "cam4_overlay_topic",
                "/surgery/images/cam4/detection_overlay/compressed",
            ).value
        )
        self._cam4_semantics_topic = str(
            self.declare_parameter(
                "cam4_semantics_topic",
                "/surgery/perception/cam4/semantics/json",
            ).value
        )
        self._cam4_mayo_observation_topic = str(
            self.declare_parameter(
                "cam4_mayo_observation_topic",
                "/surgery/perception/cam4/mayo_tool_observations",
            ).value
        )
        self._diagnostics_topic = str(
            self.declare_parameter(
                "diagnostics_topic",
                "/surgery/perception/rfdetr/diagnostics/json",
            ).value
        )
        self._health_topic = str(
            self.declare_parameter(
                "health_topic",
                "/surgery/perception/rfdetr/health",
            ).value
        )
        self._max_source_skew_sec = max(
            0.0,
            float(
                self.declare_parameter(
                    "max_source_skew_sec",
                    0.1,
                ).value
            ),
        )
        self._request_timeout_sec = max(
            0.1,
            float(
                self.declare_parameter(
                    "request_timeout_sec",
                    5.0,
                ).value
            ),
        )
        self._max_rate_hz = max(
            0.1,
            float(self.declare_parameter("max_rate_hz", 15.0).value),
        )
        self._segmented_output_rate_hz = max(
            0.1,
            float(
                self.declare_parameter(
                    "segmented_output_rate_hz",
                    2.0,
                ).value
            ),
        )
        self._enabled = bool(
            self.declare_parameter("enabled", True).value
        )
        self._generation = 0

        contract_overlay_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=2,
            reliability=ReliabilityPolicy.BEST_EFFORT,
            durability=DurabilityPolicy.VOLATILE,
        )
        contract_diagnostics_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=10,
            reliability=ReliabilityPolicy.BEST_EFFORT,
            durability=DurabilityPolicy.VOLATILE,
        )
        contract_health_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )

        self._segmented_flir_pub = self.create_publisher(
            CompressedImage,
            self._flir_output_topic,
            qos_profile_sensor_data,
        )
        self._detected_cam4_pub = self.create_publisher(
            CompressedImage,
            self._cam4_output_topic,
            qos_profile_sensor_data,
        )
        self._flir_overlay_pub = self.create_publisher(
            CompressedImage,
            self._flir_overlay_topic,
            qos_profile_sensor_data,
        )
        self._cam4_overlay_pub = self.create_publisher(
            CompressedImage,
            self._cam4_overlay_topic,
            contract_overlay_qos,
        )
        self._cam4_semantics_pub = self.create_publisher(
            String,
            self._cam4_semantics_topic,
            10,
        )
        self._cam4_mayo_observation_pub = self.create_publisher(
            ToolObservation,
            self._cam4_mayo_observation_topic,
            30,
        )
        self._diagnostics_pub = self.create_publisher(
            String,
            self._diagnostics_topic,
            contract_diagnostics_qos,
        )
        self._health_pub = self.create_publisher(
            String,
            self._health_topic,
            contract_health_qos,
        )
        self.create_subscription(
            CompressedImage,
            self._flir_input_topic,
            self._on_flir,
            qos_profile_sensor_data,
        )
        self._control_service = self.create_service(
            SetBool,
            "~/set_enabled",
            self._set_enabled,
        )
        self.create_subscription(
            CompressedImage,
            self._cam4_input_topic,
            self._on_cam4,
            qos_profile_sensor_data,
        )

        self._cam4_frames: deque[BufferedFrame] = deque(maxlen=48)
        self._condition = threading.Condition()
        self._pending_flir: BufferedFrame | None = None
        self._running = True
        self._session = requests.Session()
        self._cam4_mayo_tracker = Cam4MayoPlacementTracker()
        self._last_request_started = 0.0
        self._last_success_monotonic = 0.0
        self._last_segmented_requested_monotonic = 0.0
        self._diagnostics_sequence = 0
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="rfdetr-bridge-worker",
            daemon=True,
        )
        self._worker.start()
        self._health_timer = self.create_timer(1.0, self._publish_idle_health)

    @staticmethod
    def _buffered(msg: CompressedImage) -> BufferedFrame:
        return BufferedFrame(
            received_monotonic=time.monotonic(),
            stamp_sec=int(msg.header.stamp.sec),
            stamp_nanosec=int(msg.header.stamp.nanosec),
            frame_id=str(msg.header.frame_id),
            format=str(msg.format or "jpeg"),
            data=bytes(msg.data),
        )

    def _on_cam4(self, msg: CompressedImage) -> None:
        with self._condition:
            if not self._enabled:
                return
            self._cam4_frames.append(self._buffered(msg))

    def _on_flir(self, msg: CompressedImage) -> None:
        with self._condition:
            if not self._enabled:
                return
            # Latest-frame coalescing prevents an inference backlog during replay.
            self._pending_flir = self._buffered(msg)
            self._condition.notify()

    def _set_enabled(
        self,
        request: SetBool.Request,
        response: SetBool.Response,
    ) -> SetBool.Response:
        enabled = bool(request.data)
        with self._condition:
            changed = self._enabled != enabled
            self._enabled = enabled
            if changed:
                self._generation += 1
                self._pending_flir = None
                self._cam4_frames.clear()
                self._last_segmented_requested_monotonic = 0.0
                self._cam4_mayo_tracker.reset()
            self._condition.notify_all()
        response.success = True
        response.message = (
            "RF-DETR object recognition enabled; waiting for fresh frames"
            if enabled
            else "RF-DETR object recognition disabled; raw views remain available"
        )
        self._publish_health(
            connected=False,
            status="waiting_for_frame" if enabled else "disabled",
            latency_ms=0.0,
            pair_skew_sec=None,
            error="",
        )
        return response

    def _generation_is_active(self, generation: int) -> bool:
        with self._condition:
            return self._enabled and self._generation == generation

    def _worker_loop(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(
                    lambda: (
                        not self._running
                        or (self._enabled and self._pending_flir is not None)
                    )
                )
                if not self._running:
                    return
                flir = self._pending_flir
                self._pending_flir = None
                generation = self._generation
                cam4 = (
                    closest_aligned_frame(
                        self._cam4_frames,
                        flir.source_stamp_sec,
                        self._max_source_skew_sec,
                    )
                    if flir is not None
                    else None
                )
            if flir is None:
                continue
            minimum_period = 1.0 / self._max_rate_hz
            delay = minimum_period - (time.monotonic() - self._last_request_started)
            if delay > 0.0:
                time.sleep(delay)
            self._last_request_started = time.monotonic()
            self._process_pair(flir, cam4, generation)

    def _process_pair(
        self,
        flir: BufferedFrame,
        cam4: BufferedFrame | None,
        generation: int,
    ) -> None:
        now_monotonic = time.monotonic()
        segmented_period = 1.0 / self._segmented_output_rate_hz
        include_flir_segmented = (
            self._last_segmented_requested_monotonic <= 0.0
            or now_monotonic - self._last_segmented_requested_monotonic
            >= segmented_period
        )
        if include_flir_segmented:
            self._last_segmented_requested_monotonic = now_monotonic
        request_payload: dict[str, Any] = {
            "flir_stamp_sec": flir.source_stamp_sec,
            "flir_image_base64": base64.b64encode(flir.data).decode("ascii"),
            "include_flir_segmented_image": include_flir_segmented,
            # The browser composites this service's transparent overlay over
            # the raw CAM4 frame, so another full-frame JPEG is redundant.
            "include_cam4_annotated_image": False,
        }
        pair_skew_sec: float | None = None
        if cam4 is not None:
            request_payload["cam4_stamp_sec"] = cam4.source_stamp_sec
            request_payload["cam4_image_base64"] = base64.b64encode(
                cam4.data
            ).decode("ascii")
            pair_skew_sec = abs(cam4.source_stamp_sec - flir.source_stamp_sec)
        started = time.perf_counter()
        try:
            response = self._session.post(
                f"{self._service_url}/v1/perceive",
                json=request_payload,
                timeout=self._request_timeout_sec,
            )
            response.raise_for_status()
            payload = response.json()
            if (
                not isinstance(payload, dict)
                or payload.get("schema")
                != "taskplanner.rfdetr_perception.v1"
            ):
                raise RuntimeError("unexpected RF-DETR service response")
            segmented = payload.get("flir_segmented_image")
            image_bytes: bytes | None = None
            if include_flir_segmented:
                if not isinstance(segmented, dict):
                    raise RuntimeError("RF-DETR response omitted segmented FLIR")
                encoded_image = segmented.get("data_base64")
                if not isinstance(encoded_image, str) or not encoded_image:
                    raise RuntimeError("RF-DETR response has no FLIR image bytes")
                image_bytes = base64.b64decode(encoded_image, validate=True)
            if not self._generation_is_active(generation):
                return

            if image_bytes is not None:
                image_msg = CompressedImage()
                image_msg.header.stamp.sec = flir.stamp_sec
                image_msg.header.stamp.nanosec = flir.stamp_nanosec
                image_msg.header.frame_id = (
                    f"{flir.frame_id}|rfdetr_seg"
                    if flir.frame_id
                    else "flir_rfdetr_seg"
                )
                image_msg.format = "jpeg"
                image_msg.data = image_bytes
                self._segmented_flir_pub.publish(image_msg)

            flir_overlay = payload.get("flir_overlay_image")
            if isinstance(flir_overlay, dict):
                encoded_overlay = flir_overlay.get("data_base64")
                if isinstance(encoded_overlay, str) and encoded_overlay:
                    overlay_msg = CompressedImage()
                    overlay_msg.header.stamp.sec = flir.stamp_sec
                    overlay_msg.header.stamp.nanosec = flir.stamp_nanosec
                    overlay_msg.header.frame_id = (
                        f"{flir.frame_id}|rfdetr_seg_overlay"
                        if flir.frame_id
                        else "flir_rfdetr_seg_overlay"
                    )
                    overlay_msg.format = str(
                        flir_overlay.get("mime_type", "image/webp")
                    ).removeprefix("image/")
                    overlay_msg.data = base64.b64decode(
                        encoded_overlay,
                        validate=True,
                    )
                    self._flir_overlay_pub.publish(overlay_msg)

            annotated_cam4 = payload.get("cam4_annotated_image")
            if cam4 is not None and isinstance(annotated_cam4, dict):
                encoded_cam4 = annotated_cam4.get("data_base64")
                if isinstance(encoded_cam4, str) and encoded_cam4:
                    cam4_msg = CompressedImage()
                    cam4_msg.header.stamp.sec = cam4.stamp_sec
                    cam4_msg.header.stamp.nanosec = cam4.stamp_nanosec
                    cam4_msg.header.frame_id = (
                        f"{cam4.frame_id}|rfdetr_bbox"
                        if cam4.frame_id
                        else "cam4_rfdetr_bbox"
                    )
                    cam4_msg.format = "jpeg"
                    cam4_msg.data = base64.b64decode(
                        encoded_cam4,
                        validate=True,
                    )
                    self._detected_cam4_pub.publish(cam4_msg)

            cam4_overlay = payload.get("cam4_overlay_image")
            if cam4 is not None and isinstance(cam4_overlay, dict):
                encoded_cam4_overlay = cam4_overlay.get("data_base64")
                if (
                    isinstance(encoded_cam4_overlay, str)
                    and encoded_cam4_overlay
                ):
                    cam4_overlay_msg = CompressedImage()
                    cam4_overlay_msg.header.stamp.sec = cam4.stamp_sec
                    cam4_overlay_msg.header.stamp.nanosec = cam4.stamp_nanosec
                    cam4_overlay_msg.header.frame_id = (
                        cam4.frame_id
                    )
                    cam4_overlay_msg.format = str(
                        cam4_overlay.get("mime_type", "image/webp")
                    ).removeprefix("image/")
                    cam4_overlay_msg.data = base64.b64decode(
                        encoded_cam4_overlay,
                        validate=True,
                    )
                    self._cam4_overlay_pub.publish(cam4_overlay_msg)

            semantics = payload.get("cam4_semantics")
            if isinstance(semantics, dict):
                semantics_json = json.dumps(
                    semantics,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                public_semantics = parse_cam4_semantics_json(semantics_json)
                if not public_semantics:
                    raise RuntimeError("invalid CAM4 semantic summary")
                semantics_msg = String()
                semantics_msg.data = semantics_json
                self._cam4_semantics_pub.publish(semantics_msg)
                self._publish_cam4_mayo_observations(public_semantics)

            latency_ms = (time.perf_counter() - started) * 1000.0
            self._diagnostics_sequence += 1
            diagnostics_msg = String()
            diagnostics_msg.data = json.dumps(
                build_contract_diagnostics(
                    payload.get("diagnostics", {}),
                    cam4=cam4,
                    sequence=self._diagnostics_sequence,
                    source_to_output_latency_ms=latency_ms,
                ),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            self._diagnostics_pub.publish(diagnostics_msg)
            self._last_success_monotonic = time.monotonic()
            self._publish_health(
                connected=True,
                status="ready",
                latency_ms=latency_ms,
                pair_skew_sec=pair_skew_sec,
                error="",
            )
        except Exception as exc:
            if not self._generation_is_active(generation):
                return
            self._publish_health(
                connected=False,
                status="error",
                latency_ms=(time.perf_counter() - started) * 1000.0,
                pair_skew_sec=pair_skew_sec,
                error=str(exc),
            )
            self.get_logger().warning(
                f"RF-DETR preprocessing failed: {exc}",
                throttle_duration_sec=3.0,
            )

    def _publish_cam4_mayo_observations(
        self,
        public_semantics: dict[str, Any],
    ) -> None:
        """Publish deterministic Mayo placements outside the VLM executor."""

        for placement in self._cam4_mayo_tracker.update(public_semantics):
            stamp_sec = int(placement.source_stamp_sec)
            stamp_nanosec = int(
                round(
                    (placement.source_stamp_sec - stamp_sec)
                    * 1_000_000_000
                )
            )
            if stamp_nanosec >= 1_000_000_000:
                stamp_sec += 1
                stamp_nanosec -= 1_000_000_000
            observation = ToolObservation()
            observation.stamp.sec = stamp_sec
            observation.stamp.nanosec = stamp_nanosec
            observation.instrument_id = placement.instrument_name
            observation.location_id = "mayo_stand"
            observation.location_type = "mayo_stand"
            observation.confidence = placement.confidence
            observation.visible = True
            self._cam4_mayo_observation_pub.publish(observation)

    def _publish_health(
        self,
        *,
        connected: bool,
        status: str,
        latency_ms: float,
        pair_skew_sec: float | None,
        error: str,
    ) -> None:
        with self._condition:
            enabled = self._enabled
        stamp = self.get_clock().now().to_msg()
        cam4_ready = bool(connected and pair_skew_sec is not None)
        msg = String()
        msg.data = json.dumps(
            {
                "schema": "pnu.rfdetr_health.v2",
                "stamp_sec": int(stamp.sec),
                "stamp_nanosec": int(stamp.nanosec),
                "node": self.get_name(),
                "state": status if enabled else "disabled",
                "cam4_rgb_ready": cam4_ready,
                "cam4_camera_info_ready": False,
                "cam4_depth_ready": False,
                "cam4_calibration_ready": False,
                "cam4_pose_ready": False,
                "tray_rgb_ready": False,
                "tray_camera_info_ready": False,
                "tray_depth_ready": False,
                "tray_calibration_ready": False,
                "tray_model_ready": False,
                "tray_pose_ready": False,
                "model_ready": bool(connected),
                "last_error_code": "RFDETR_REQUEST_FAILED" if error else "",
                "last_error_message": str(error)[:500],
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        self._health_pub.publish(msg)

    def _publish_idle_health(self) -> None:
        with self._condition:
            enabled = self._enabled
        if not enabled:
            self._publish_health(
                connected=False,
                status="disabled",
                latency_ms=0.0,
                pair_skew_sec=None,
                error="",
            )
            return
        if self._last_success_monotonic <= 0.0:
            self._publish_health(
                connected=False,
                status="waiting_for_frame",
                latency_ms=0.0,
                pair_skew_sec=None,
                error="",
            )
            return
        age_sec = time.monotonic() - self._last_success_monotonic
        if age_sec <= 2.0:
            return
        self._publish_health(
            connected=True,
            status="waiting_for_frame",
            latency_ms=0.0,
            pair_skew_sec=None,
            error="",
        )

    def destroy_node(self):
        with self._condition:
            self._running = False
            self._condition.notify_all()
        self._worker.join(timeout=self._request_timeout_sec + 1.0)
        self._session.close()
        return super().destroy_node()


def main() -> None:
    rclpy.init()
    node = RFDETRBridgeNode()
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


if __name__ == "__main__":
    main()
