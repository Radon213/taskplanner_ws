"""Minimal scenario-free ROS runtime backing the Taskplanner Debug Mode UI."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import threading
import time
from typing import Any, Callable
from uuid import uuid4

from ament_index_python.packages import get_package_share_directory
import rclpy
from rclpy.action import ActionClient
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import (
    QoSDurabilityPolicy,
    QoSHistoryPolicy,
    QoSProfile,
    QoSReliabilityPolicy,
    qos_profile_sensor_data,
)
from sensor_msgs.msg import CompressedImage
from std_msgs.msg import String
from std_srvs.srv import Trigger
from surgical_interop_msgs.action import ExecuteRetraction, ExecuteToolHandover
from surgical_interop_msgs.msg import (
    ClinicalObservation,
    ClinicalObservationArray,
    InstrumentState,
    InstrumentStateArray,
    RobotState,
    RobotStateArray,
    SurgeryContext,
    SurgeryEvent,
    SurgeryHealth,
)
from surgical_interop_msgs.srv import SetSuction
from surgical_msgs.srv import IntegrationDebugCommand

from integration_debug.contracts import (
    decode_payload,
    load_config,
    measured_rate,
    parse_voice_command,
    validate_retraction,
    validate_tool_handover,
)
from integration_debug.networking import (
    collect_network_status,
    ping_ipv4,
    validate_network_settings,
    write_network_settings,
)


STATUS_SCHEMA = "taskplanner.integration_debug.status.v1"
EVENT_SCHEMA = "taskplanner.integration_debug.event.v1"
PUBLIC_OUTPUT_TYPES: dict[str, type[Any]] = {
    "surgical_interop_msgs/msg/SurgeryContext": SurgeryContext,
    "surgical_interop_msgs/msg/InstrumentStateArray": InstrumentStateArray,
    "surgical_interop_msgs/msg/RobotStateArray": RobotStateArray,
    "surgical_interop_msgs/msg/SurgeryEvent": SurgeryEvent,
    "surgical_interop_msgs/msg/ClinicalObservationArray": ClinicalObservationArray,
    "surgical_interop_msgs/msg/SurgeryHealth": SurgeryHealth,
}


@dataclass(slots=True)
class InputStats:
    arrivals: deque[float] = field(default_factory=lambda: deque(maxlen=512))
    sizes: deque[tuple[float, int]] = field(default_factory=lambda: deque(maxlen=512))
    last_received_monotonic: float = 0.0
    source_delay_sec: float | None = None
    last_sample: str = ""
    message_count: int = 0


@dataclass(slots=True)
class OutputState:
    topic: str
    message_type: str
    rate_hz: float
    enabled: bool = False
    last_published_monotonic: float = 0.0
    publish_times: deque[float] = field(default_factory=lambda: deque(maxlen=256))
    publish_count: int = 0
    sequence: int = 0


def _snapshot_qos() -> QoSProfile:
    return QoSProfile(
        history=QoSHistoryPolicy.KEEP_LAST,
        depth=1,
        reliability=QoSReliabilityPolicy.RELIABLE,
        durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
    )


def _event_qos() -> QoSProfile:
    return QoSProfile(
        history=QoSHistoryPolicy.KEEP_LAST,
        depth=50,
        reliability=QoSReliabilityPolicy.RELIABLE,
        durability=QoSDurabilityPolicy.VOLATILE,
    )


def _policy_name(value: Any) -> str:
    name = getattr(value, "name", "")
    return str(name or value).split(".")[-1].lower()


def _node_identity(namespace: str, name: str) -> str:
    prefix = namespace.rstrip("/")
    return f"{prefix}/{name}" if prefix else f"/{name}"


class IntegrationDebugNode(Node):
    """Monitor external inputs and manually exercise public integration endpoints."""

    def __init__(self) -> None:
        super().__init__("integration_debug_gateway")
        default_config = str(
            Path(get_package_share_directory("integration_debug"))
            / "config"
            / "integration_debug.yaml"
        )
        self.declare_parameter("config_path", default_config)
        self.declare_parameter(
            "run_root",
            os.environ.get("TASKPLANNER_RUN_ROOT", "/tmp/taskplanner-runs"),
        )
        config_path = str(self.get_parameter("config_path").value)
        self._config = load_config(config_path)
        self._lock = threading.RLock()
        self._log_lock = threading.Lock()
        self._callback_group = ReentrantCallbackGroup()
        self._monitor_window_sec = max(
            1.0, float(self._config.get("monitor_window_sec", 5.0))
        )
        self._heartbeat_timeout_sec = max(
            2.0, float(self._config.get("heartbeat_timeout_sec", 6.0))
        )
        self._armed = False
        self._fault_locked = False
        self._last_heartbeat_monotonic = 0.0
        self._last_error = ""
        self._active_route = ""
        self._active_command_id = ""
        self._active_goal_handle: Any | None = None
        self._action_status: dict[str, Any] = self._idle_action_status()
        self._voice_auto_execute = False
        self._last_sentence = ""
        self._last_voice_parse: dict[str, Any] = {}
        self._last_voice_dispatch_text = ""
        self._last_voice_dispatch_monotonic = 0.0
        self._recent_events: deque[dict[str, Any]] = deque(maxlen=60)

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self._session_id = f"debug-{timestamp}-{uuid4().hex[:8]}"
        run_root = Path(str(self.get_parameter("run_root").value)).expanduser()
        self._network_settings_path = Path(
            os.environ.get(
                "TASKPLANNER_DEBUG_NETWORK_SETTINGS",
                str(run_root / "debug" / "network-settings.json"),
            )
        ).expanduser()
        self._restart_supported = (
            os.environ.get("TASKPLANNER_DEBUG_ALLOW_SELF_RESTART", "")
            .strip()
            .lower()
            in {"1", "true", "yes", "on"}
        )
        self._restart_scheduled = False
        self._session_dir = run_root / "debug" / self._session_id
        self._session_dir.mkdir(parents=True, exist_ok=True)
        self._event_log_path = self._session_dir / "events.jsonl"

        self._status_pub = self.create_publisher(
            String, "/integration/debug/status", 10
        )
        self._event_pub = self.create_publisher(
            String, "/integration/debug/events", 50
        )
        self._readiness_pub = self.create_publisher(
            String, "/integration/readiness", 10
        )
        self._command_service = self.create_service(
            IntegrationDebugCommand,
            "/integration/debug/command",
            self._handle_command,
            callback_group=self._callback_group,
        )
        self._readiness_service = self.create_service(
            Trigger,
            "/integration/check_readiness",
            self._handle_readiness,
            callback_group=self._callback_group,
        )

        self._input_stats: dict[str, InputStats] = {}
        self._input_subscriptions: list[Any] = []
        for row in self._config["inputs"]:
            topic = str(row["topic"])
            message_type = str(row["type"])
            self._input_stats[topic] = InputStats()
            if message_type == "std_msgs/msg/String":
                subscription = self.create_subscription(
                    String,
                    topic,
                    lambda msg, source_topic=topic: self._on_string_input(
                        source_topic, msg
                    ),
                    QoSProfile(
                        history=QoSHistoryPolicy.KEEP_LAST,
                        depth=20,
                        reliability=QoSReliabilityPolicy.RELIABLE,
                        durability=QoSDurabilityPolicy.VOLATILE,
                    ),
                    callback_group=self._callback_group,
                )
            elif message_type == "sensor_msgs/msg/CompressedImage":
                subscription = self.create_subscription(
                    CompressedImage,
                    topic,
                    lambda msg, source_topic=topic: self._on_image_input(
                        source_topic, msg
                    ),
                    qos_profile_sensor_data,
                    callback_group=self._callback_group,
                )
            else:
                raise ValueError(f"unsupported debug input type: {message_type}")
            self._input_subscriptions.append(subscription)

        self._tool_client = ActionClient(
            self,
            ExecuteToolHandover,
            "/surgery/tool_handover",
            callback_group=self._callback_group,
        )
        self._retraction_client = ActionClient(
            self,
            ExecuteRetraction,
            "/surgery/retraction",
            callback_group=self._callback_group,
        )
        self._suction_client = self.create_client(
            SetSuction,
            "/surgery/suction/set",
            callback_group=self._callback_group,
        )

        self._output_states: dict[str, OutputState] = {}
        self._output_publishers: dict[str, Any] = {}
        for row in self._config["outputs"]:
            topic = str(row["topic"])
            message_type = str(row["type"])
            message_class = PUBLIC_OUTPUT_TYPES.get(message_type)
            if message_class is None:
                raise ValueError(f"unsupported debug output type: {message_type}")
            qos = _event_qos() if str(row.get("qos")) == "event" else _snapshot_qos()
            self._output_publishers[topic] = self.create_publisher(
                message_class, topic, qos
            )
            self._output_states[topic] = OutputState(
                topic=topic,
                message_type=message_type,
                rate_hz=max(0.1, float(row.get("default_hz", 1.0))),
            )

        status_period = max(0.2, float(self._config.get("status_period_sec", 1.0)))
        self.create_timer(
            status_period,
            self._publish_status,
            callback_group=self._callback_group,
        )
        self.create_timer(
            0.1,
            self._publish_enabled_outputs,
            callback_group=self._callback_group,
        )
        self._record(
            "session_started",
            {
                "config_path": config_path,
                "ros_domain_id": os.environ.get("ROS_DOMAIN_ID", "0"),
            },
        )

    @staticmethod
    def _idle_action_status() -> dict[str, Any]:
        return {
            "route": "",
            "command_id": "",
            "state": "idle",
            "progress": 0.0,
            "success": False,
            "terminal": True,
            "reason_code": "",
            "started_monotonic": 0.0,
        }

    def _session_state(self) -> str:
        if self._fault_locked:
            return "FAULT_LOCKED"
        if self._active_command_id:
            return "BUSY"
        if self._armed:
            return "ARMED"
        return "MONITOR_ONLY"

    def _record(self, event_type: str, payload: dict[str, Any]) -> None:
        row = {
            "schema": EVENT_SCHEMA,
            "stamp": datetime.now(timezone.utc).isoformat(),
            "session_id": self._session_id,
            "event_type": event_type,
            "payload": payload,
        }
        encoded = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
        with self._log_lock:
            with self._event_log_path.open("a", encoding="utf-8") as stream:
                stream.write(encoded + "\n")
        summary = {
            "stamp": row["stamp"],
            "event_type": event_type,
            "payload": payload,
        }
        with self._lock:
            self._recent_events.append(summary)
        if not self.context.ok():
            return
        message = String()
        message.data = encoded
        self._event_pub.publish(message)

    def _on_string_input(self, topic: str, msg: String) -> None:
        now = time.monotonic()
        text = str(msg.data).strip()
        with self._lock:
            stats = self._input_stats[topic]
            stats.arrivals.append(now)
            stats.sizes.append((now, len(msg.data.encode("utf-8"))))
            stats.last_received_monotonic = now
            stats.last_sample = text[:240]
            stats.message_count += 1
            self._last_sentence = text
        parsed = parse_voice_command(text, dict(self._config.get("voice", {})))
        with self._lock:
            self._last_voice_parse = parsed.as_dict()
            should_dispatch = (
                self._voice_auto_execute
                and self._armed
                and not self._active_command_id
                and parsed.matched
                and text != self._last_voice_dispatch_text
            ) or (
                self._voice_auto_execute
                and self._armed
                and not self._active_command_id
                and parsed.matched
                and now - self._last_voice_dispatch_monotonic > 2.0
            )
            if should_dispatch:
                self._last_voice_dispatch_text = text
                self._last_voice_dispatch_monotonic = now
        self._record(
            "sentence_received",
            {"topic": topic, "text": text, "parse": parsed.as_dict()},
        )
        if should_dispatch and parsed.payload is not None:
            accepted, command_id, message = self._dispatch_action(
                parsed.operation, parsed.payload, source="voice"
            )
            self._record(
                "voice_dispatch",
                {
                    "accepted": accepted,
                    "command_id": command_id,
                    "message": message,
                },
            )

    def _on_image_input(self, topic: str, msg: CompressedImage) -> None:
        now = time.monotonic()
        source_sec = float(msg.header.stamp.sec) + float(msg.header.stamp.nanosec) / 1e9
        ros_now_sec = self.get_clock().now().nanoseconds / 1e9
        source_delay = ros_now_sec - source_sec if source_sec > 0.0 else None
        with self._lock:
            stats = self._input_stats[topic]
            stats.arrivals.append(now)
            stats.sizes.append((now, len(msg.data)))
            stats.last_received_monotonic = now
            stats.source_delay_sec = source_delay
            stats.last_sample = f"{msg.format or 'unknown'} · {len(msg.data)} bytes"
            stats.message_count += 1

    def _blocked_nodes(self) -> list[str]:
        expected = {str(value) for value in self._config.get("blocked_nodes", [])}
        try:
            discovered = {name for name, _namespace in self.get_node_names_and_namespaces()}
        except Exception:
            return []
        return sorted(expected & discovered)

    def _output_conflicts(self, topic: str) -> list[str]:
        conflicts: set[str] = set()
        try:
            infos = self.get_publishers_info_by_topic(topic)
        except Exception:
            return []
        for info in infos:
            if (
                str(info.node_name) == self.get_name()
                and str(info.node_namespace) == self.get_namespace()
            ):
                continue
            conflicts.add(_node_identity(str(info.node_namespace), str(info.node_name)))
        return sorted(conflicts)

    def _handle_command(
        self,
        request: IntegrationDebugCommand.Request,
        response: IntegrationDebugCommand.Response,
    ) -> IntegrationDebugCommand.Response:
        operation = str(request.operation).strip().lower()
        result: dict[str, Any] = {}
        try:
            payload = decode_payload(request.payload_json)
            if operation == "apply_network_settings":
                accepted, command_id, message, result = self._apply_network_settings(
                    payload
                )
            elif operation == "ping_host":
                accepted, command_id, message, result = self._ping_host(payload)
            else:
                accepted, command_id, message = self._execute_command(
                    operation, payload
                )
        except ValueError as exc:
            accepted, command_id, message = False, "", str(exc)
        except Exception as exc:  # fail closed at the browser boundary
            self.get_logger().error(f"integration debug command failed: {exc}")
            accepted, command_id, message = False, "", f"command failed: {exc}"
        response.accepted = accepted
        response.command_id = command_id
        response.message = message
        response.result_json = json.dumps(
            result,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if not accepted:
            with self._lock:
                self._last_error = message
        if operation != "heartbeat":
            self._record(
                "ui_command",
                {
                    "operation": operation,
                    "accepted": accepted,
                    "command_id": command_id,
                    "message": message,
                    "result": result,
                },
            )
        return response

    def _execute_command(
        self, operation: str, payload: dict[str, Any]
    ) -> tuple[bool, str, str]:
        now = time.monotonic()
        if operation == "heartbeat":
            with self._lock:
                self._last_heartbeat_monotonic = now
            return True, "", "heartbeat accepted"
        if operation == "arm":
            blocked = self._blocked_nodes()
            with self._lock:
                if self._fault_locked:
                    return False, "", "reset the fault lock before arming"
                if blocked:
                    return False, "", "full Taskplanner nodes are active: " + ", ".join(blocked)
                self._armed = True
                self._last_heartbeat_monotonic = now
                self._last_error = ""
            return True, "", "manual control armed"
        if operation == "disarm":
            with self._lock:
                self._armed = False
                self._voice_auto_execute = False
            if self._active_command_id:
                self._request_cancel()
                return True, self._active_command_id, "disarmed; active Action cancel requested"
            return True, "", "manual control disarmed"
        if operation == "reset_fault":
            with self._lock:
                if self._active_command_id:
                    return False, "", "cannot reset while an Action is active"
                self._fault_locked = False
                self._last_error = ""
                self._action_status = self._idle_action_status()
            return True, "", "fault lock reset"
        if operation == "cancel_active":
            return self._request_cancel()
        if operation == "configure_voice":
            enabled = bool(payload.get("enabled", False))
            with self._lock:
                if enabled and not self._armed:
                    return False, "", "arm manual control before enabling voice dispatch"
                self._voice_auto_execute = enabled
            return True, "", "voice auto-dispatch enabled" if enabled else "voice auto-dispatch disabled"
        if operation == "configure_output":
            return self._configure_output(payload)
        if operation == "publish_once":
            topic = str(payload.get("topic", "")).strip()
            if topic not in self._output_states:
                return False, "", "unknown public output topic"
            conflicts = self._output_conflicts(topic)
            if conflicts:
                return False, "", "another publisher owns the topic: " + ", ".join(conflicts)
            self._publish_output(topic)
            return True, "", f"published one debug message on {topic}"
        if operation == "stop_outputs":
            with self._lock:
                for state in self._output_states.values():
                    state.enabled = False
            return True, "", "all debug output publishers stopped"
        return self._dispatch_action(operation, payload, source="ui")

    def _apply_network_settings(
        self, payload: dict[str, Any]
    ) -> tuple[bool, str, str, dict[str, Any]]:
        if not self._restart_supported:
            return (
                False,
                "",
                "network restart supervisor is unavailable",
                {},
            )
        settings = validate_network_settings(payload)
        with self._lock:
            if self._restart_scheduled:
                return False, "", "network restart is already scheduled", {}
            if self._active_command_id:
                return False, "", "stop the active command before changing DDS settings", {}
            if self._armed:
                return False, "", "disarm manual control before changing DDS settings", {}
            if any(state.enabled for state in self._output_states.values()):
                return False, "", "stop debug output publishers before changing DDS settings", {}

        current_domain = int(os.environ.get("ROS_DOMAIN_ID", "0") or 0)
        current_discovery = os.environ.get(
            "ROS_AUTOMATIC_DISCOVERY_RANGE", ""
        ).strip().upper()
        result = {
            "domain_id": settings["domain_id"],
            "discovery_range": settings["discovery_range"],
            "restart_required": (
                current_domain != settings["domain_id"]
                or current_discovery != settings["discovery_range"]
            ),
        }
        if not result["restart_required"]:
            return True, "", "DDS network settings are already active", result

        write_network_settings(self._network_settings_path, settings)
        with self._lock:
            self._restart_scheduled = True
            self._armed = False
            self._voice_auto_execute = False
        threading.Thread(
            target=self._restart_runtime_after_response,
            name="debug-network-restart",
            daemon=True,
        ).start()
        return (
            True,
            "",
            "DDS settings saved; Debug Mode is restarting",
            result,
        )

    def _restart_runtime_after_response(self) -> None:
        time.sleep(1.5)
        try:
            os.kill(os.getpid(), signal.SIGTERM)
        except OSError as exc:
            with self._lock:
                self._restart_scheduled = False
                self._last_error = f"failed to restart Debug Mode: {exc}"
            self.get_logger().error(self._last_error)

    @staticmethod
    def _ping_host(
        payload: dict[str, Any]
    ) -> tuple[bool, str, str, dict[str, Any]]:
        result = ping_ipv4(payload.get("target_ip"), count=3, timeout_sec=1.0)
        message = (
            "ping reply received"
            if result["reachable"]
            else "ping completed without an ICMP reply"
        )
        return True, "", message, result

    def _dispatch_action(
        self, operation: str, payload: dict[str, Any], *, source: str
    ) -> tuple[bool, str, str]:
        with self._lock:
            if not self._armed:
                return False, "", "manual control is not armed"
            if self._fault_locked:
                return False, "", "manual control is fault locked"
            if self._active_command_id:
                return False, self._active_command_id, "another command is active"
        blocked = self._blocked_nodes()
        if blocked:
            return False, "", "full Taskplanner nodes are active: " + ", ".join(blocked)
        command_id = f"debug-{uuid4()}"
        if operation == "tool_handover":
            mapped = validate_tool_handover(payload)
            if not self._tool_client.server_is_ready():
                return False, "", "/surgery/tool_handover Action server is unavailable"
            goal = ExecuteToolHandover.Goal()
            goal.command_id = command_id
            goal.instrument_id = mapped["instrument_id"]
            goal.instrument_instance_id = mapped["instrument_instance_id"]
            goal.source_location = mapped["source_location"]
            goal.target_location = mapped["target_location"]
            self._start_action("tool_handover", command_id, source)
            future = self._tool_client.send_goal_async(
                goal,
                feedback_callback=lambda feedback: self._on_action_feedback(
                    "tool_handover", command_id, feedback
                ),
            )
            future.add_done_callback(
                lambda result: self._on_goal_response(
                    "tool_handover", command_id, result
                )
            )
            return True, command_id, "tool handover Goal submitted"
        if operation == "retraction":
            mapped = validate_retraction(payload)
            if not self._retraction_client.server_is_ready():
                return False, "", "/surgery/retraction Action server is unavailable"
            goal = ExecuteRetraction.Goal()
            goal.command_id = command_id
            goal.operation = mapped["operation"]
            goal.direction = mapped["direction"]
            goal.distance_mm = mapped["distance_mm"]
            goal.end_effector_profile = mapped["end_effector_profile"]
            self._start_action("retraction", command_id, source)
            future = self._retraction_client.send_goal_async(
                goal,
                feedback_callback=lambda feedback: self._on_action_feedback(
                    "retraction", command_id, feedback
                ),
            )
            future.add_done_callback(
                lambda result: self._on_goal_response("retraction", command_id, result)
            )
            return True, command_id, "retraction Goal submitted"
        if operation == "suction":
            if not self._suction_client.service_is_ready():
                return False, "", "/surgery/suction/set Service is unavailable"
            request = SetSuction.Request()
            request.command_id = command_id
            request.enabled = bool(payload.get("enabled", False))
            self._start_action("suction", command_id, source)
            future = self._suction_client.call_async(request)
            future.add_done_callback(
                lambda result: self._on_suction_result(command_id, result)
            )
            return True, command_id, "suction request submitted"
        return False, "", "unsupported integration debug operation"

    def _start_action(self, route: str, command_id: str, source: str) -> None:
        with self._lock:
            self._active_route = route
            self._active_command_id = command_id
            self._active_goal_handle = None
            self._action_status = {
                "route": route,
                "command_id": command_id,
                "state": "submitting",
                "progress": 0.0,
                "success": False,
                "terminal": False,
                "reason_code": "",
                "source": source,
                "started_monotonic": time.monotonic(),
            }
        self._record(
            "command_started",
            {"route": route, "command_id": command_id, "source": source},
        )

    def _on_goal_response(self, route: str, command_id: str, future: Any) -> None:
        try:
            goal_handle = future.result()
        except Exception as exc:
            self._finish_action(route, command_id, False, "failed", f"goal_error:{exc}")
            return
        if not goal_handle.accepted:
            self._finish_action(route, command_id, False, "rejected", "goal_rejected")
            return
        with self._lock:
            if self._active_command_id != command_id:
                return
            self._active_goal_handle = goal_handle
            self._action_status["state"] = "accepted"
        result_future = goal_handle.get_result_async()
        result_future.add_done_callback(
            lambda result: self._on_action_result(route, command_id, result)
        )

    def _on_action_feedback(
        self, route: str, command_id: str, feedback_message: Any
    ) -> None:
        feedback = feedback_message.feedback
        with self._lock:
            if self._active_command_id != command_id:
                return
            self._action_status["state"] = str(feedback.state or "executing")
            self._action_status["progress"] = min(
                1.0, max(0.0, float(feedback.progress))
            )

    def _on_action_result(self, route: str, command_id: str, future: Any) -> None:
        try:
            wrapped = future.result()
            result = wrapped.result
            success = bool(result.success)
            final_state = str(result.final_state or ("completed" if success else "failed"))
            reason_code = str(result.reason_code or final_state)
        except Exception as exc:
            success = False
            final_state = "failed"
            reason_code = f"result_error:{exc}"
        self._finish_action(route, command_id, success, final_state, reason_code)

    def _on_suction_result(self, command_id: str, future: Any) -> None:
        try:
            result = future.result()
            success = bool(result.success)
            final_state = str(result.state or ("completed" if success else "failed"))
            reason_code = str(result.reason_code or final_state)
        except Exception as exc:
            success = False
            final_state = "failed"
            reason_code = f"service_error:{exc}"
        self._finish_action("suction", command_id, success, final_state, reason_code)

    def _finish_action(
        self,
        route: str,
        command_id: str,
        success: bool,
        final_state: str,
        reason_code: str,
    ) -> None:
        with self._lock:
            if self._active_command_id != command_id:
                return
            started = float(self._action_status.get("started_monotonic", 0.0))
            self._action_status.update(
                {
                    "state": final_state,
                    "progress": 1.0,
                    "success": success,
                    "terminal": True,
                    "reason_code": reason_code,
                    "elapsed_sec": max(0.0, time.monotonic() - started),
                }
            )
            self._active_route = ""
            self._active_command_id = ""
            self._active_goal_handle = None
            if reason_code in {"cancel_recovery_failed", "cancel_rejected"}:
                self._fault_locked = True
                self._armed = False
                self._voice_auto_execute = False
        self._record(
            "command_finished",
            {
                "route": route,
                "command_id": command_id,
                "success": success,
                "final_state": final_state,
                "reason_code": reason_code,
            },
        )

    def _request_cancel(self) -> tuple[bool, str, str]:
        with self._lock:
            command_id = self._active_command_id
            goal_handle = self._active_goal_handle
            route = self._active_route
            if not command_id:
                return False, "", "no active Action to cancel"
            if route == "suction":
                return False, command_id, "suction is a non-cancellable Service"
            if goal_handle is None:
                return False, command_id, "Action Goal has not been accepted yet"
            self._action_status["state"] = "cancel_requested"
        future = goal_handle.cancel_goal_async()
        future.add_done_callback(
            lambda result: self._on_cancel_response(route, command_id, result)
        )
        self._record("cancel_requested", {"route": route, "command_id": command_id})
        return True, command_id, "Action cancel requested"

    def _on_cancel_response(self, route: str, command_id: str, future: Any) -> None:
        try:
            response = future.result()
            accepted = bool(response.goals_canceling)
        except Exception:
            accepted = False
        if accepted:
            return
        with self._lock:
            if self._active_command_id == command_id:
                self._fault_locked = True
                self._armed = False
                self._voice_auto_execute = False
                self._action_status["state"] = "cancel_rejected"
                self._action_status["reason_code"] = "cancel_rejected"
        self._record(
            "cancel_rejected", {"route": route, "command_id": command_id}
        )

    def _configure_output(
        self, payload: dict[str, Any]
    ) -> tuple[bool, str, str]:
        topic = str(payload.get("topic", "")).strip()
        state = self._output_states.get(topic)
        if state is None:
            return False, "", "unknown public output topic"
        enabled = bool(payload.get("enabled", False))
        try:
            rate_hz = float(payload.get("rate_hz", state.rate_hz))
        except (TypeError, ValueError):
            return False, "", "rate_hz must be numeric"
        if not 0.1 <= rate_hz <= 10.0:
            return False, "", "rate_hz must be between 0.1 and 10"
        if enabled:
            conflicts = self._output_conflicts(topic)
            if conflicts:
                return False, "", "another publisher owns the topic: " + ", ".join(conflicts)
        with self._lock:
            state.rate_hz = rate_hz
            state.enabled = enabled
            state.last_published_monotonic = 0.0
        return True, "", f"{topic} {'enabled' if enabled else 'disabled'} at {rate_hz:.2f} Hz"

    def _publish_enabled_outputs(self) -> None:
        now = time.monotonic()
        due: list[str] = []
        with self._lock:
            for topic, state in self._output_states.items():
                if not state.enabled:
                    continue
                period = 1.0 / max(0.1, state.rate_hz)
                if state.last_published_monotonic <= 0.0 or now - state.last_published_monotonic >= period:
                    due.append(topic)
        for topic in due:
            if self._output_conflicts(topic):
                with self._lock:
                    self._output_states[topic].enabled = False
                    self._last_error = f"stopped {topic}: another publisher was discovered"
                self._record(
                    "output_conflict",
                    {"topic": topic, "publishers": self._output_conflicts(topic)},
                )
                continue
            self._publish_output(topic)

    def _publish_output(self, topic: str) -> None:
        with self._lock:
            state = self._output_states[topic]
            state.sequence += 1
            sequence = state.sequence
        message = self._dummy_message(topic, sequence)
        self._output_publishers[topic].publish(message)
        now = time.monotonic()
        with self._lock:
            state.last_published_monotonic = now
            state.publish_times.append(now)
            state.publish_count += 1

    def _dummy_message(self, topic: str, sequence: int) -> Any:
        stamp = self.get_clock().now().to_msg()
        if topic == "/surgery/context":
            msg = SurgeryContext()
            msg.stamp = stamp
            msg.revision = sequence
            msg.procedure_type = "integration_debug"
            msg.procedure_active = False
            msg.current_phase = ""
            msg.phase_confidence = 0.0
            msg.phase_uncertain = True
            msg.execution_state = "debug"
            msg.evidence_status = "UNKNOWN"
            msg.safety_flags = ["DEBUG_DUMMY_DATA"]
            return msg
        if topic == "/surgery/instruments":
            item = InstrumentState()
            item.stamp = stamp
            item.instrument_id = "DEBUG_DUMMY_DATA"
            item.instance_id = f"debug-instrument-{sequence}"
            item.location_type = "debug"
            item.location_id = "integration_debug"
            item.holder_role = "none"
            item.state = "dummy"
            item.visible = False
            item.confidence = 0.0
            item.evidence_status = "UNKNOWN"
            msg = InstrumentStateArray()
            msg.stamp = stamp
            msg.revision = sequence
            msg.instruments = [item]
            return msg
        if topic == "/surgery/robots":
            item = RobotState()
            item.stamp = stamp
            item.robot_id = "integration_debug"
            item.robot_type = "DEBUG_DUMMY_DATA"
            item.connection_state = "debug"
            item.execution_state = "idle"
            item.active_command_id = ""
            item.progress = 0.0
            item.reason_code = "DEBUG_DUMMY_DATA"
            item.evidence_status = "UNKNOWN"
            msg = RobotStateArray()
            msg.stamp = stamp
            msg.revision = sequence
            msg.robots = [item]
            return msg
        if topic == "/surgery/events":
            msg = SurgeryEvent()
            msg.stamp = stamp
            msg.sequence = sequence
            msg.event_type = "DEBUG_DUMMY_DATA"
            msg.subject_type = "integration_debug"
            msg.subject_id = self._session_id
            msg.phase = ""
            msg.location_type = "debug"
            msg.location_id = "integration_debug"
            msg.state = "dummy"
            msg.correlation_id = f"{self._session_id}:{sequence}"
            msg.confidence = 0.0
            msg.evidence_status = "UNKNOWN"
            return msg
        if topic == "/surgery/clinical_observations":
            item = ClinicalObservation()
            item.stamp = stamp
            item.sequence = sequence
            item.source = "integration_debug"
            item.summary = "DEBUG_DUMMY_DATA"
            item.phase_ids = []
            item.phase_confidences = []
            item.observed_tool_ids = []
            item.observed_location_types = []
            item.observed_location_ids = []
            item.observed_confidences = []
            item.gesture_event_type = ""
            item.gesture_requested_tool = ""
            item.gesture_hand_pose = ""
            item.gesture_confidence = 0.0
            item.uncertainty = 1.0
            item.evidence_status = "UNKNOWN"
            msg = ClinicalObservationArray()
            msg.stamp = stamp
            msg.revision = sequence
            msg.observations = [item]
            return msg
        if topic == "/surgery/health":
            msg = SurgeryHealth()
            msg.stamp = stamp
            msg.revision = sequence
            msg.healthy = False
            msg.state = "integration_debug"
            msg.unavailable_sources = []
            msg.stale_sources = []
            msg.error_codes = ["DEBUG_DUMMY_DATA"]
            msg.evidence_status = "UNKNOWN"
            return msg
        raise ValueError(f"unsupported debug output topic: {topic}")

    def _input_status_rows(self, now: float) -> list[dict[str, Any]]:
        graph_types = dict(self.get_topic_names_and_types())
        rows: list[dict[str, Any]] = []
        for config in self._config["inputs"]:
            topic = str(config["topic"])
            with self._lock:
                stats = self._input_stats[topic]
                arrivals = list(stats.arrivals)
                sizes = list(stats.sizes)
                last_received = stats.last_received_monotonic
                last_sample = stats.last_sample
                message_count = stats.message_count
                source_delay = stats.source_delay_sec
            rate_hz, window_count = measured_rate(
                arrivals, now, self._monitor_window_sec
            )
            recent_sizes = [size for stamp, size in sizes if now - stamp <= self._monitor_window_sec]
            bandwidth = sum(recent_sizes) / self._monitor_window_sec
            actual_types = [str(value) for value in graph_types.get(topic, [])]
            expected_type = str(config["type"])
            try:
                publisher_infos = self.get_publishers_info_by_topic(topic)
            except Exception:
                publisher_infos = []
            publishers = sorted(
                {
                    _node_identity(str(info.node_namespace), str(info.node_name))
                    for info in publisher_infos
                }
            )
            qos_profiles = sorted(
                {
                    f"{_policy_name(info.qos_profile.reliability)}/"
                    f"{_policy_name(info.qos_profile.durability)}"
                    for info in publisher_infos
                }
            )
            age_sec = now - last_received if last_received > 0.0 else None
            stale_after = float(config.get("stale_after_sec", 0.0))
            expected_hz = float(config.get("expected_hz", 0.0))
            if publisher_infos and expected_type not in actual_types:
                state = "TYPE_MISMATCH"
            elif not publisher_infos:
                state = "WAITING_PUBLISHER"
            elif message_count == 0:
                state = "WAITING_MESSAGES"
            elif stale_after > 0.0 and age_sec is not None and age_sec > stale_after:
                state = "STALE"
            elif expected_hz > 0.0 and window_count >= 2 and rate_hz < expected_hz * 0.8:
                state = "LOW_RATE"
            else:
                state = "READY"
            rows.append(
                {
                    "name": str(config.get("name", topic)),
                    "topic": topic,
                    "expected_type": expected_type,
                    "actual_types": actual_types,
                    "publisher_count": len(publisher_infos),
                    "publishers": publishers,
                    "qos_profiles": qos_profiles,
                    "expected_qos": str(config.get("qos", "")),
                    "expected_hz": expected_hz,
                    "measured_hz": round(rate_hz, 3),
                    "message_count": message_count,
                    "window_message_count": window_count,
                    "last_age_sec": round(age_sec, 3) if age_sec is not None else None,
                    "source_delay_sec": round(source_delay, 3) if source_delay is not None else None,
                    "bandwidth_bytes_sec": round(bandwidth, 1),
                    "last_sample": last_sample,
                    "state": state,
                }
            )
        return rows

    def _subscription_nodes(self, topic: str) -> list[str]:
        try:
            infos = self.get_subscriptions_info_by_topic(topic)
        except Exception:
            return []
        return sorted(
            {
                _node_identity(str(info.node_namespace), str(info.node_name))
                for info in infos
                if not (
                    str(info.node_name) == self.get_name()
                    and str(info.node_namespace) == self.get_namespace()
                )
            }
        )

    def _output_status_rows(self, now: float) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        with self._lock:
            states = list(self._output_states.values())
        for state in states:
            rate_hz, _count = measured_rate(
                list(state.publish_times), now, self._monitor_window_sec
            )
            subscribers = self._subscription_nodes(state.topic)
            conflicts = self._output_conflicts(state.topic)
            age = (
                now - state.last_published_monotonic
                if state.last_published_monotonic > 0.0
                else None
            )
            rows.append(
                {
                    "topic": state.topic,
                    "type": state.message_type,
                    "enabled": state.enabled,
                    "configured_hz": state.rate_hz,
                    "measured_hz": round(rate_hz, 3),
                    "publish_count": state.publish_count,
                    "sequence": state.sequence,
                    "last_age_sec": round(age, 3) if age is not None else None,
                    "subscriber_count": len(subscribers),
                    "subscribers": subscribers,
                    "conflicting_publishers": conflicts,
                }
            )
        return rows

    def _status_snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            action = dict(self._action_status)
            if not action.get("terminal") and action.get("started_monotonic"):
                action["elapsed_sec"] = round(
                    now - float(action["started_monotonic"]), 3
                )
            action.pop("started_monotonic", None)
            recent_events = list(self._recent_events)
            voice = {
                "auto_execute": self._voice_auto_execute,
                "last_sentence": self._last_sentence,
                "last_parse": dict(self._last_voice_parse),
            }
            armed = self._armed
            last_error = self._last_error
        blocked = self._blocked_nodes()
        try:
            network = collect_network_status()
        except Exception as exc:
            network = {
                "primary_interface": "",
                "primary_ipv4": "",
                "prefix_length": 0,
                "gateway_ipv4": "",
                "multicast_capable": False,
                "addresses": [],
                "error": str(exc),
            }
        network.update(
            {
                "settings_path": str(self._network_settings_path),
                "restart_supported": self._restart_supported,
                "restart_scheduled": self._restart_scheduled,
            }
        )
        endpoints = [
            {
                "name": "tool_handover",
                "endpoint": "/surgery/tool_handover",
                "kind": "action",
                "ready": self._tool_client.server_is_ready(),
            },
            {
                "name": "retraction",
                "endpoint": "/surgery/retraction",
                "kind": "action",
                "ready": self._retraction_client.server_is_ready(),
            },
            {
                "name": "suction",
                "endpoint": "/surgery/suction/set",
                "kind": "service",
                "ready": self._suction_client.service_is_ready(),
            },
        ]
        return {
            "schema": STATUS_SCHEMA,
            "stamp_sec": round(self.get_clock().now().nanoseconds / 1e9, 6),
            "session": {
                "session_id": self._session_id,
                "state": self._session_state(),
                "armed": armed,
                "fault_locked": self._fault_locked,
                "last_error": last_error,
                "event_log_path": str(self._event_log_path),
            },
            "runtime": {
                "ros_domain_id": os.environ.get("ROS_DOMAIN_ID", "0"),
                "rmw_implementation": os.environ.get("RMW_IMPLEMENTATION", ""),
                "discovery_range": os.environ.get(
                    "ROS_AUTOMATIC_DISCOVERY_RANGE", ""
                ),
                "blocked_nodes": blocked,
                "network": network,
            },
            "inputs": self._input_status_rows(now),
            "endpoints": endpoints,
            "action": action,
            "outputs": self._output_status_rows(now),
            "voice": voice,
            "recent_events": recent_events,
        }

    def _readiness_snapshot(self) -> dict[str, Any]:
        checks = {
            "sentence_publisher": self.count_publishers(
                "/sensors/surgeon/sentence"
            )
            > 0,
            "tool_handover_server": self._tool_client.server_is_ready(),
            "retraction_server": self._retraction_client.server_is_ready(),
            "suction_service": self._suction_client.service_is_ready(),
        }
        missing = [name for name, passed in checks.items() if not passed]
        return {
            "schema": "taskplanner.integration_readiness.v1",
            "ready": not missing,
            "checks": checks,
            "missing": missing,
            "details": {"mode": "debug", "perception_required": False},
            "stamp_sec": round(self.get_clock().now().nanoseconds / 1e9, 6),
        }

    def _handle_readiness(
        self,
        _request: Trigger.Request,
        response: Trigger.Response,
    ) -> Trigger.Response:
        snapshot = self._readiness_snapshot()
        response.success = bool(snapshot["ready"])
        response.message = (
            "integration ready"
            if response.success
            else "integration not ready: " + ", ".join(snapshot["missing"])
        )
        self._publish_readiness(snapshot)
        return response

    def _publish_readiness(self, snapshot: dict[str, Any] | None = None) -> None:
        message = String()
        message.data = json.dumps(
            snapshot or self._readiness_snapshot(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        self._readiness_pub.publish(message)

    def _check_heartbeat(self) -> None:
        now = time.monotonic()
        with self._lock:
            expired = (
                self._armed
                and self._last_heartbeat_monotonic > 0.0
                and now - self._last_heartbeat_monotonic > self._heartbeat_timeout_sec
            )
            if not expired:
                return
            self._armed = False
            self._voice_auto_execute = False
            command_id = self._active_command_id
        self._record(
            "heartbeat_timeout", {"active_command_id": command_id}
        )
        if command_id:
            self._request_cancel()

    def _publish_status(self) -> None:
        self._check_heartbeat()
        message = String()
        message.data = json.dumps(
            self._status_snapshot(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        self._status_pub.publish(message)
        self._publish_readiness()

    def close(self) -> None:
        with self._lock:
            self._armed = False
            self._voice_auto_execute = False
            for state in self._output_states.values():
                state.enabled = False
        self._record("session_stopped", {})


def main() -> None:
    rclpy.init()
    node = IntegrationDebugNode()
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        node.close()
        executor.shutdown()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
