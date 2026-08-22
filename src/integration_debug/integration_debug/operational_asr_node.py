"""Operational microphone ASR ROS 2 endpoint for Taskplanner.

Unlike Debug Mode, this node exposes a stable control/status contract to the
operational runtime and does not persist microphone audio or transcripts by
default.  The finalized sentence publisher exists only while the configured
ASR WebSocket is connected, so publisher-count readiness cannot report a
microphone source that is merely starting, disconnected, or stopping.
"""

from __future__ import annotations

import json
import math
import os
import threading
from typing import Any, Callable

import rclpy
from rclpy._rclpy_pybind11 import RCLError
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

from integration_debug.asr_endpoints import (
    ASR_ENDPOINT_CLOUD,
    ASR_ENDPOINT_LAN,
    DEFAULT_ASR_ENDPOINT,
    DEFAULT_CLOUD_SERVER_URL,
    DEFAULT_LAN_SERVER_URL,
    DEFAULT_ASR_ROUTE_POLICY,
    ASR_ROUTE_POLICY_AUTO,
    resolve_puzzle_asr_endpoint,
    validate_asr_route_policy,
)
from integration_debug.asr_health_monitor import (
    LAN_HEALTH_READY,
    LanAsrHealthMonitor,
)
from integration_debug.asr_runtime import AsrMicrophoneRuntime


NODE_NAME = "taskplanner_asr"
STATUS_TOPIC = "/input/asr/runtime_status"
CONTROL_SERVICE = "/input/asr/control"
SENTENCE_TOPIC = "/sensors/surgeon/sentence"
STATUS_SCHEMA = "taskplanner.asr.status.v1"
# Preserve the established public constant name for launch/tests that import it.
DEFAULT_SERVER_URL = DEFAULT_CLOUD_SERVER_URL
DEFAULT_OUTPUT_DIR = "/taskplanner-runs/asr/operational"
DEFAULT_CAPTURE_LOCK = "/taskplanner-runs/asr/microphone.lock"
DEFAULT_LAN_HEALTH_INTERVAL_SEC = 1.0
DEFAULT_LAN_HEALTH_FAILURE_INTERVAL_SEC = 0.5
DEFAULT_LAN_HEALTH_TIMEOUT_SEC = 0.5
DEFAULT_LAN_HEALTH_STALE_AFTER_SEC = 2.0


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


def _bounded_float(value: Any, *, default: float, minimum: float) -> float:
    """Parse deployment tuning without making a malformed env value fatal."""

    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed):
        return default
    return max(minimum, parsed)


class OperationalAsrNode(Node):
    """Own the operational ASR runtime and its fixed ROS interface."""

    def __init__(
        self,
        *,
        runtime_factory: Callable[..., AsrMicrophoneRuntime] = AsrMicrophoneRuntime,
        health_monitor_factory: Callable[..., Any] = LanAsrHealthMonitor,
    ) -> None:
        super().__init__(NODE_NAME)
        self._callback_group = ReentrantCallbackGroup()
        self._publisher_lock = threading.RLock()
        self._control_lock = threading.RLock()
        self._close_lock = threading.Lock()
        self._closed = False
        self._capture_requested = False
        self._sentence_pub: Any | None = None

        self.declare_parameter(
            "endpoint",
            os.environ.get("PUZZLE_ASR_ENDPOINT", DEFAULT_ASR_ENDPOINT)
            or DEFAULT_ASR_ENDPOINT,
        )
        # ``route_policy`` is Live-only.  Keep ``PUZZLE_ASR_ENDPOINT`` as the
        # backwards-compatible fixed-route default shared with standalone
        # Debug, while allowing Live to opt into a preflight-only LAN policy.
        self.declare_parameter(
            "route_policy",
            os.environ.get("PUZZLE_ASR_ROUTE_POLICY")
            or os.environ.get("PUZZLE_ASR_ENDPOINT", DEFAULT_ASR_ROUTE_POLICY)
            or DEFAULT_ASR_ROUTE_POLICY,
        )
        # ``PUZZLE_ASR_URL`` remains the compatible cloud-route override.  It
        # is not used for the LAN route, which has its own reviewed setting.
        self.declare_parameter(
            "server_url",
            os.environ.get("PUZZLE_ASR_URL", DEFAULT_SERVER_URL)
            or DEFAULT_SERVER_URL,
        )
        self.declare_parameter(
            "lan_server_url",
            os.environ.get("PUZZLE_ASR_LAN_URL", DEFAULT_LAN_SERVER_URL)
            or DEFAULT_LAN_SERVER_URL,
        )
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
        self.declare_parameter(
            "lan_health_interval_sec",
            _bounded_float(
                os.environ.get("PUZZLE_ASR_LAN_HEALTH_INTERVAL_SEC"),
                default=DEFAULT_LAN_HEALTH_INTERVAL_SEC,
                minimum=0.2,
            ),
        )
        self.declare_parameter(
            "lan_health_failure_interval_sec",
            _bounded_float(
                os.environ.get("PUZZLE_ASR_LAN_HEALTH_FAILURE_INTERVAL_SEC"),
                default=DEFAULT_LAN_HEALTH_FAILURE_INTERVAL_SEC,
                minimum=0.2,
            ),
        )
        self.declare_parameter(
            "lan_health_timeout_sec",
            _bounded_float(
                os.environ.get("PUZZLE_ASR_LAN_HEALTH_TIMEOUT_SEC"),
                default=DEFAULT_LAN_HEALTH_TIMEOUT_SEC,
                minimum=0.05,
            ),
        )
        self.declare_parameter(
            "lan_health_stale_after_sec",
            _bounded_float(
                os.environ.get("PUZZLE_ASR_LAN_HEALTH_STALE_AFTER_SEC"),
                default=DEFAULT_LAN_HEALTH_STALE_AFTER_SEC,
                minimum=0.2,
            ),
        )

        # Validate each named deployment route independently.  ``auto`` is a
        # policy, never an endpoint accepted by the resolver itself.
        self._cloud_url = self.get_parameter("server_url").get_parameter_value().string_value
        self._lan_url = self.get_parameter("lan_server_url").get_parameter_value().string_value
        resolve_puzzle_asr_endpoint(
            self.get_parameter("endpoint").get_parameter_value().string_value,
            cloud_url=self._cloud_url,
            lan_url=self._lan_url,
        )
        self._route_policy = validate_asr_route_policy(
            self.get_parameter("route_policy").get_parameter_value().string_value
        )
        self._lan_monitor = health_monitor_factory(
            url=self._lan_url,
            interval_sec=self.get_parameter("lan_health_interval_sec")
            .get_parameter_value()
            .double_value,
            failure_interval_sec=self.get_parameter("lan_health_failure_interval_sec")
            .get_parameter_value()
            .double_value,
            timeout_sec=self.get_parameter("lan_health_timeout_sec")
            .get_parameter_value()
            .double_value,
            stale_after_sec=self.get_parameter("lan_health_stale_after_sec")
            .get_parameter_value()
            .double_value,
        )
        self._endpoint, self._server_url, self._selection_reason = (
            self._resolve_route_for_policy()
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
        # Start the independent, non-audio LAN probe only after all node
        # resources are ready.  It never runs in the microphone start path.
        self._lan_monitor.start()
        self._publish_status()

    def _stamp_sec(self) -> float:
        return round(self.get_clock().now().nanoseconds / 1_000_000_000.0, 9)

    def _lan_health_snapshot(self) -> dict[str, Any]:
        """Read the monitor cache without performing network I/O."""

        return dict(self._lan_monitor.snapshot())

    def _resolve_route_for_policy(self) -> tuple[str, str, str]:
        """Resolve the next concrete route from the cached health result."""

        if self._route_policy == ASR_ENDPOINT_CLOUD:
            endpoint, url = resolve_puzzle_asr_endpoint(
                ASR_ENDPOINT_CLOUD,
                cloud_url=self._cloud_url,
                lan_url=self._lan_url,
            )
            return endpoint, url, "cloud_only"
        if self._route_policy == ASR_ENDPOINT_LAN:
            endpoint, url = resolve_puzzle_asr_endpoint(
                ASR_ENDPOINT_LAN,
                cloud_url=self._cloud_url,
                lan_url=self._lan_url,
            )
            return endpoint, url, "lan_only"

        health = self._lan_health_snapshot()
        if health.get("state") == LAN_HEALTH_READY:
            endpoint, url = resolve_puzzle_asr_endpoint(
                ASR_ENDPOINT_LAN,
                cloud_url=self._cloud_url,
                lan_url=self._lan_url,
            )
            return endpoint, url, "lan_ready"
        endpoint, url = resolve_puzzle_asr_endpoint(
            ASR_ENDPOINT_CLOUD,
            cloud_url=self._cloud_url,
            lan_url=self._lan_url,
        )
        return endpoint, url, "lan_unavailable_fallback"

    def _lan_is_ready_for_forced_start(self) -> bool:
        return self._lan_health_snapshot().get("state") == LAN_HEALTH_READY

    def _preview_route(self) -> tuple[str, str, str]:
        """Expose the next decision while stopped without changing a session."""

        state = str(self._runtime.snapshot().get("state", ""))
        if state in {"STARTING", "LISTENING", "STOPPING"}:
            return self._endpoint, self._server_url, self._selection_reason
        return self._resolve_route_for_policy()

    def _status_envelope(self) -> dict[str, Any]:
        # A timer may publish while a control request changes the policy.  The
        # reentrant lock keeps each status envelope internally consistent;
        # monitor reads remain cached and do not perform I/O while it is held.
        with self._control_lock:
            asr = dict(self._runtime.snapshot())
            endpoint, url, selection_reason = self._preview_route()
            # Route metadata is additive to v1 and intentionally distinct from
            # ``connected``: monitor readiness is non-audio preflight state,
            # whereas connected belongs only to the active microphone session.
            asr["endpoint_id"] = endpoint
            asr["server_url"] = url
            asr["route_policy"] = self._route_policy
            asr["selection_reason"] = selection_reason
            asr["lan_health"] = self._lan_health_snapshot()
            return {
                "schema": STATUS_SCHEMA,
                "stamp_sec": self._stamp_sec(),
                "asr": asr,
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
            if operation == "set_route_policy":
                state = str(self._runtime.snapshot().get("state", ""))
                if state not in {"STOPPED", "ERROR"}:
                    return self._set_response(
                        response,
                        accepted=False,
                        message=(
                            "ASR route policy cannot change while the microphone "
                            "session is active"
                        ),
                    )
                self._route_policy = validate_asr_route_policy(
                    getattr(request, "route_policy", "")
                )
                (
                    self._endpoint,
                    self._server_url,
                    self._selection_reason,
                ) = self._resolve_route_for_policy()
                return self._set_response(
                    response,
                    accepted=True,
                    message=(
                        f"ASR route policy set to {self._route_policy}; "
                        f"next start selects {self._endpoint}"
                    ),
                )
            if operation == "start":
                requested_url = str(request.server_url).strip()
                if requested_url and requested_url not in {
                    self._cloud_url,
                    self._lan_url,
                }:
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
                if (
                    self._route_policy == ASR_ENDPOINT_LAN
                    and not self._lan_is_ready_for_forced_start()
                ):
                    return self._set_response(
                        response,
                        accepted=False,
                        message=(
                            "LAN ASR is not ready; microphone was not opened. "
                            "Choose auto for cloud fallback or wait for LAN health."
                        ),
                    )
                endpoint, server_url, selection_reason = self._resolve_route_for_policy()
                self._runtime.start(
                    device_id=None if device_id == -1 else device_id,
                    server_url=server_url,
                )
                self._endpoint = endpoint
                self._server_url = server_url
                self._selection_reason = selection_reason
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
                message=(
                    "operation must be refresh_devices, set_route_policy, start, "
                    "or stop"
                ),
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
            monitor_stopped = self._lan_monitor.close()
            if not monitor_stopped:
                self.get_logger().warning(
                    "LAN ASR health monitor did not stop before shutdown timeout"
                )
            stopped = self._runtime.close()
            self._drain_runtime_events()
            self._sync_sentence_publisher(False)
            # SIGINT may invalidate the rclpy context before the executor
            # reaches this finally path. Runtime/device cleanup must still
            # complete, but publishing on that invalid context would turn an
            # otherwise graceful shutdown into exit code 1.
            if rclpy.ok(context=self.context):
                try:
                    self._publish_status()
                except RCLError:
                    # Close only the signal race where the context becomes
                    # invalid after the check. Preserve every RCLError raised
                    # while the context is still live.
                    if rclpy.ok(context=self.context):
                        raise
            return bool(stopped and monitor_stopped)


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
