"""Operational microphone ASR ROS 2 endpoint for Taskplanner.

Unlike Debug Mode, this node exposes a stable control/status contract to the
operational runtime and does not persist microphone audio or transcripts by
default.  The finalized sentence publisher exists only while the configured
ASR WebSocket is connected, so publisher-count readiness cannot report a
microphone source that is merely starting, disconnected, or stopping.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any, Callable

import rclpy
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import (
    DurabilityPolicy,
    HistoryPolicy,
    QoSProfile,
    ReliabilityPolicy,
)
from rclpy.validate_full_topic_name import validate_full_topic_name
from std_msgs.msg import String
from surgical_msgs.srv import AsrControl

from integration_debug.asr_runtime import (
    AsrMicrophoneRuntime,
    validate_websocket_url,
)


NODE_NAME = "taskplanner_asr"
STATUS_TOPIC = "/input/asr/runtime_status"
CONTROL_SERVICE = "/input/asr/control"
SENTENCE_TOPIC = "/sensors/surgeon/sentence"
STATUS_SCHEMA = "taskplanner.asr.status.v1"
DEFAULT_SERVER_URL = "wss://arpa.worker-02.puzzle-ai.com"
DEFAULT_OUTPUT_DIR = "/taskplanner-runs/asr/operational"
DEFAULT_CAPTURE_LOCK = "/taskplanner-runs/asr/microphone.lock"


def _status_qos() -> QoSProfile:
    return QoSProfile(
        history=HistoryPolicy.KEEP_LAST,
        depth=1,
        reliability=ReliabilityPolicy.RELIABLE,
        durability=DurabilityPolicy.TRANSIENT_LOCAL,
    )


def _sentence_qos() -> QoSProfile:
    return QoSProfile(
        history=HistoryPolicy.KEEP_LAST,
        depth=10,
        reliability=ReliabilityPolicy.RELIABLE,
        durability=DurabilityPolicy.VOLATILE,
    )


def _json_dumps(value: Any) -> str:
    """Serialize contract payloads without non-standard NaN/Infinity values."""

    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def _absolute_topic_name(value: Any) -> str:
    topic = str(value or "").strip()
    if not topic.startswith("/") or topic.startswith("//"):
        raise ValueError("sentence_topic must be an absolute ROS topic name")
    validate_full_topic_name(topic)
    return topic


class OperationalAsrNode(Node):
    """Own the operational ASR runtime and its fixed ROS interface."""

    def __init__(
        self,
        *,
        runtime_factory: Callable[..., AsrMicrophoneRuntime] = AsrMicrophoneRuntime,
    ) -> None:
        super().__init__(NODE_NAME)
        self._callback_group = ReentrantCallbackGroup()
        self._publisher_lock = threading.RLock()
        self._control_lock = threading.RLock()
        self._close_lock = threading.Lock()
        self._closed = False
        self._capture_requested = False
        self._sentence_pub: Any | None = None

        env_server_url = os.environ.get("PUZZLE_ASR_URL", DEFAULT_SERVER_URL)
        self.declare_parameter("server_url", env_server_url)
        self.declare_parameter(
            "sentence_topic",
            os.environ.get("SENTENCE_INPUT_TOPIC", SENTENCE_TOPIC),
        )
        self.declare_parameter(
            "output_dir",
            os.environ.get("TASKPLANNER_ASR_OUTPUT_DIR", DEFAULT_OUTPUT_DIR),
        )
        self.declare_parameter(
            "capture_lock_path",
            os.environ.get("TASKPLANNER_ASR_CAPTURE_LOCK", DEFAULT_CAPTURE_LOCK),
        )
        self.declare_parameter("status_period_sec", 0.5)

        self._server_url = validate_websocket_url(
            self.get_parameter("server_url").get_parameter_value().string_value
        )
        self._sentence_topic = _absolute_topic_name(
            self.get_parameter("sentence_topic").get_parameter_value().string_value
        )
        output_dir = self.get_parameter("output_dir").get_parameter_value().string_value
        capture_lock_path = (
            self.get_parameter("capture_lock_path")
            .get_parameter_value()
            .string_value
        )
        status_period_sec = max(
            0.1,
            self.get_parameter("status_period_sec")
            .get_parameter_value()
            .double_value,
        )

        self._runtime = runtime_factory(
            default_url=self._server_url,
            topic=self._sentence_topic,
            output_dir=output_dir,
            # Operational ASR never persists raw audio or transcripts. This is
            # deliberately not a ROS parameter, so a launch override cannot
            # silently change the data-retention boundary.
            save_artifacts=False,
            capture_lock_path=capture_lock_path,
        )
        self._status_pub = self.create_publisher(
            String,
            STATUS_TOPIC,
            _status_qos(),
        )
        self._control_service = self.create_service(
            AsrControl,
            CONTROL_SERVICE,
            self._handle_control,
            callback_group=self._callback_group,
        )
        self._event_timer = self.create_timer(
            0.05,
            self._drain_runtime_events,
            callback_group=self._callback_group,
        )
        self._status_timer = self.create_timer(
            status_period_sec,
            self._publish_status,
            callback_group=self._callback_group,
        )
        self._publish_status()

    def _stamp_sec(self) -> float:
        return round(self.get_clock().now().nanoseconds / 1_000_000_000.0, 9)

    def _status_envelope(self) -> dict[str, Any]:
        return {
            "schema": STATUS_SCHEMA,
            "stamp_sec": self._stamp_sec(),
            "asr": self._runtime.snapshot(),
        }

    def _publish_status(self) -> None:
        message = String()
        try:
            message.data = _json_dumps(self._status_envelope())
        except (TypeError, ValueError) as exc:
            self.get_logger().error(f"ASR status is not JSON-safe: {exc}")
            return
        self._status_pub.publish(message)

    def _result_json(self) -> str:
        # Service responses and the transient-local topic intentionally share
        # one schema so browsers and CLI consumers cannot interpret two subtly
        # different status shapes.
        return _json_dumps(self._status_envelope())

    def _set_response(
        self,
        response: AsrControl.Response,
        *,
        accepted: bool,
        message: str,
    ) -> AsrControl.Response:
        response.accepted = bool(accepted)
        response.message = str(message)[:500]
        try:
            response.result_json = self._result_json()
        except (TypeError, ValueError) as exc:
            response.accepted = False
            response.message = f"ASR runtime returned non-JSON-safe state: {exc}"[:500]
            response.result_json = "{}"
        response.stamp = self.get_clock().now().to_msg()
        self._publish_status()
        return response

    def _handle_control(
        self,
        request: AsrControl.Request,
        response: AsrControl.Response,
    ) -> AsrControl.Response:
        with self._control_lock:
            if self._closed:
                return self._set_response(
                    response,
                    accepted=False,
                    message="operational ASR node is shutting down",
                )
            return self._handle_control_locked(request, response)

    def _handle_control_locked(
        self,
        request: AsrControl.Request,
        response: AsrControl.Response,
    ) -> AsrControl.Response:
        operation = str(request.operation).strip().casefold()
        try:
            if operation == "refresh_devices":
                devices = self._runtime.refresh_devices()
                return self._set_response(
                    response,
                    accepted=True,
                    message=f"found {len(devices)} microphone input device(s)",
                )
            if operation == "start":
                requested_url = str(request.server_url).strip()
                if requested_url and requested_url != self._server_url:
                    return self._set_response(
                        response,
                        accepted=False,
                        message=(
                            "server_url override is not allowed; use the node's "
                            "configured Puzzle ASR endpoint"
                        ),
                    )
                device_id = int(request.device_id)
                if device_id < -1:
                    return self._set_response(
                        response,
                        accepted=False,
                        message="device_id must be -1 (Ubuntu default) or non-negative",
                    )
                state = str(self._runtime.snapshot().get("state", ""))
                if state not in {"STOPPED", "ERROR"}:
                    return self._set_response(
                        response,
                        accepted=False,
                        message="ASR microphone session is already active",
                    )
                self._runtime.start(
                    device_id=None if device_id == -1 else device_id,
                    server_url=self._server_url,
                )
                with self._publisher_lock:
                    self._capture_requested = True
                self._sync_sentence_publisher(
                    bool(self._runtime.snapshot().get("connected", False))
                )
                return self._set_response(
                    response,
                    accepted=True,
                    message="operational ASR microphone session started",
                )
            if operation == "stop":
                with self._publisher_lock:
                    self._capture_requested = False
                self._sync_sentence_publisher(False)
                self._runtime.stop_async()
                return self._set_response(
                    response,
                    accepted=True,
                    message="operational ASR microphone stop requested",
                )
            return self._set_response(
                response,
                accepted=False,
                message="operation must be refresh_devices, start, or stop",
            )
        except Exception as exc:
            # A failed start must never leave a stale graph-visible publisher.
            if operation == "start":
                with self._publisher_lock:
                    self._capture_requested = False
                self._sync_sentence_publisher(False)
            return self._set_response(
                response,
                accepted=False,
                message=str(exc) or type(exc).__name__,
            )

    def _ensure_sentence_publisher(self) -> None:
        with self._publisher_lock:
            if self._sentence_pub is None:
                self._sentence_pub = self.create_publisher(
                    String,
                    self._sentence_topic,
                    _sentence_qos(),
                )

    def _destroy_sentence_publisher(self) -> None:
        with self._publisher_lock:
            publisher = self._sentence_pub
            self._sentence_pub = None
            if publisher is not None:
                self.destroy_publisher(publisher)

    def _sync_sentence_publisher(self, connected: bool) -> None:
        with self._publisher_lock:
            should_publish = bool(connected and self._capture_requested)
        if should_publish:
            self._ensure_sentence_publisher()
        else:
            self._destroy_sentence_publisher()

    def _drain_runtime_events(self) -> None:
        for event in self._runtime.drain_events():
            event_type = str(event.get("type", ""))
            if event_type == "asr_connection":
                self._sync_sentence_publisher(bool(event.get("connected", False)))
                continue
            if event_type == "asr_final":
                text = str(event.get("text", "")).strip()
                if not text:
                    continue
                with self._publisher_lock:
                    publisher = self._sentence_pub
                    if publisher is not None and self._capture_requested:
                        message = String()
                        message.data = text
                        publisher.publish(message)
                continue
            if event_type == "asr_stopped":
                with self._publisher_lock:
                    self._capture_requested = False
                self._sync_sentence_publisher(False)
        snapshot = self._runtime.snapshot()
        self._sync_sentence_publisher(bool(snapshot.get("connected", False)))

    def close(self) -> bool:
        # Wait for any in-flight start/stop service transition before closing
        # PortAudio and the WebSocket worker. New controls fail once _closed is
        # visible under this same lock.
        with self._control_lock:
            with self._close_lock:
                if self._closed:
                    return True
                self._closed = True
            with self._publisher_lock:
                self._capture_requested = False
            self._sync_sentence_publisher(False)
            stopped = self._runtime.close()
            self._drain_runtime_events()
            self._sync_sentence_publisher(False)
            self._publish_status()
            return stopped


def main() -> None:
    rclpy.init()
    node = OperationalAsrNode()
    executor = MultiThreadedExecutor(num_threads=3)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        executor.shutdown()
        node.close()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
