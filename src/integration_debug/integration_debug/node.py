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
from typing import Any
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
)
from sensor_msgs.msg import CompressedImage
from std_msgs.msg import String
from std_srvs.srv import Trigger
from surgical_interop_msgs.action import ExecuteToolHandover
from surgical_interop_msgs.msg import (
    BedRobotArmStateArray,
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
from surgical_interop_msgs.srv import ExecuteRetractionCommand
from surgical_msgs.msg import (
    BTDecision,
    BedRobotArmGroupStatus,
    ExecutionTrace,
    InputSourceStatus,
    SimulationState,
    SkillStatus,
    SpeechUtterance,
    TwinEvent,
    VLMResult,
    WorldState,
)
from surgical_msgs.srv import AsrControl, IntegrationDebugCommand

from procedure_spec import (
    RetractionCommand,
    RetractionState,
    apply_retractor_service_admission,
)
from integration_debug.asr_endpoints import (
    DEFAULT_ASR_ENDPOINT,
    DEFAULT_CLOUD_SERVER_URL,
    DEFAULT_LAN_SERVER_URL,
    resolve_puzzle_asr_endpoint,
)
from integration_debug.capabilities import DebugCapabilities, capability_for_operation
from integration_debug.contracts import (
    action_watchdog_reason,
    decode_payload,
    load_action_watchdog_policy,
    load_config,
    manual_write_block_reason,
    measured_rate,
    operational_runtime_intervention_block_reason,
    operational_runtime_stopped,
    operational_state_publisher_trusted,
    validate_action_recovery_acknowledgement,
    validate_bed_robot_arm_status,
    validate_dispatch_payload,
)
from integration_debug.typed_dispatch import (
    DispatchEndpoint,
    DispatchPolicy,
    ResolvedDispatch,
)
from integration_debug.ros_transport import (
    action_feedback_fields,
    action_result_fields,
    build_wire_payload,
    resolve_interface,
)
from integration_debug.status_composition import (
    CONTROL_OWNER_STATUS_MAX_AGE_SEC,
    compose_observer_status,
    control_owner_status,
)
from integration_debug.scenario_debug_recorder import ScenarioDebugRecorder


STATUS_SCHEMA = "taskplanner.integration_debug.status.v1"
EVENT_SCHEMA = "taskplanner.integration_debug.event.v1"
DEBUG_STATUS_TOPIC = "/integration/debug/status"
DEBUG_EVENTS_TOPIC = "/integration/debug/events"
DEBUG_READINESS_TOPIC = "/integration/debug/readiness"
DEBUG_CONTROL_STATUS_TOPIC = "/integration/debug/control/status"
DEBUG_CONTROL_EVENTS_TOPIC = "/integration/debug/control/events"
DEBUG_CONTROL_READINESS_TOPIC = "/integration/debug/control/readiness"
DEBUG_READINESS_SERVICE = "/integration/debug/check_readiness"
DEBUG_CONTROL_READINESS_SERVICE = "/integration/debug/control/check_readiness"
VIPLAB_STREAM_STATUS_SCHEMA = "arpa_multicam.stream_status.v1"
VIPLAB_STREAM_IDS = frozenset(
    {"cam_1", "cam_2", "cam_3", "cam_3_depth", "cam_4", "cam_4_depth", "flir"}
)
VIPLAB_STREAM_STATUS_KEYS = frozenset(
    {
        "schema",
        "stream_id",
        "source_topic",
        "source_stamp",
        "frame_id",
        "format",
        "measured_hz",
        "payload_bytes",
        "published_count",
        "dropped_count",
        "qos",
    }
)
MAX_EVENT_SUMMARY_STRING_CHARS = 2048
MAX_EVENT_SUMMARY_ITEMS = 32
RETRACTION_SERVICE_DEFAULT_NAME = "/surgery/retraction/command"
VIRTUAL_RETRACTION_SERVICE_DEFAULT_NAME = (
    "/integration/debug/virtual/retraction/command"
)
TOOL_HANDOVER_DEFAULT_NAME = "/surgery/tool_handover"
VIRTUAL_TOOL_HANDOVER_DEFAULT_NAME = (
    "/integration/debug/virtual/tool_handover"
)
BED_ROBOT_STATUS_DEFAULT_TOPIC = "/external/bed_robot_arms/status"
VIRTUAL_BED_ROBOT_STATUS_DEFAULT_TOPIC = (
    "/integration/debug/virtual/bed_robot_arms/status"
)
VIRTUAL_ROBOT_PROFILE_ID = "integration_debug_virtual_robot_v1"
OPERATIONAL_ASR_STATUS_TOPIC = "/input/asr/runtime_status"
OPERATIONAL_ASR_CONTROL_SERVICE = "/input/asr/control"
OPERATIONAL_ASR_STATUS_SCHEMA = "taskplanner.asr.status.v1"
RETRACTION_SERVICE_SOURCE_ID = "taskplanner_debug"
RETRACTION_COMMAND_CONSTANTS = {
    "start_direct_teach": "COMMAND_START_DIRECT_TEACH",
    "finish_direct_teach": "COMMAND_FINISH_DIRECT_TEACH",
    "start_retraction": "COMMAND_START_RETRACTION",
    "adjust_retraction": "COMMAND_ADJUST_RETRACTION",
    "change_tool": "COMMAND_CHANGE_TOOL",
    "stop_retraction": "COMMAND_STOP_RETRACTION",
    "suction": "COMMAND_SUCTION",
    "suction_out": "COMMAND_SUCTION_OUT",
}
RETRACTION_STATE_NEUTRAL_COMMANDS = frozenset({"suction", "suction_out"})
RETRACTION_TARGET_SIDE_CONSTANTS = {
    "none": "TARGET_NONE",
    "left": "TARGET_LEFT",
    "right": "TARGET_RIGHT",
    "both": "TARGET_BOTH",
}
PUBLIC_OUTPUT_TYPES: dict[str, type[Any]] = {
    "surgical_interop_msgs/msg/SurgeryContext": SurgeryContext,
    "surgical_interop_msgs/msg/InstrumentStateArray": InstrumentStateArray,
    "surgical_interop_msgs/msg/RobotStateArray": RobotStateArray,
    "surgical_interop_msgs/msg/SurgeryEvent": SurgeryEvent,
    "surgical_interop_msgs/msg/ClinicalObservationArray": ClinicalObservationArray,
    "surgical_interop_msgs/msg/SurgeryHealth": SurgeryHealth,
}


def _is_non_negative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _parse_viplab_stream_status(sample: str) -> dict[str, Any] | None:
    """Parse the bounded VIPLab health contract without accepting lookalikes."""

    try:
        payload = json.loads(sample)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or frozenset(payload) != VIPLAB_STREAM_STATUS_KEYS:
        return None
    if payload.get("schema") != VIPLAB_STREAM_STATUS_SCHEMA:
        return None
    stream_id = payload.get("stream_id")
    source_topic = payload.get("source_topic")
    if stream_id not in VIPLAB_STREAM_IDS or not isinstance(source_topic, str):
        return None
    if not source_topic.startswith("/synced/") or len(source_topic) > 256:
        return None
    frame_id = payload.get("frame_id")
    format_text = payload.get("format")
    if (
        not isinstance(frame_id, str)
        or not frame_id
        or len(frame_id) > 128
        or not isinstance(format_text, str)
        or not format_text
        or len(format_text) > 128
    ):
        return None

    stamp = payload.get("source_stamp")
    if not isinstance(stamp, dict) or frozenset(stamp) != {"sec", "nanosec"}:
        return None
    if not _is_non_negative_int(stamp.get("sec")):
        return None
    if not _is_non_negative_int(stamp.get("nanosec")) or stamp["nanosec"] >= 1_000_000_000:
        return None

    measured_hz = payload.get("measured_hz")
    if isinstance(measured_hz, bool) or not isinstance(measured_hz, (int, float)):
        return None
    measured_hz = float(measured_hz)
    if not (0.0 <= measured_hz <= 240.0):
        return None
    for key in ("payload_bytes", "published_count", "dropped_count"):
        if not _is_non_negative_int(payload.get(key)):
            return None

    qos = payload.get("qos")
    if not isinstance(qos, dict) or frozenset(qos) != {
        "reliability",
        "durability",
        "depth",
    }:
        return None
    if qos.get("reliability") not in {"best_effort", "reliable"}:
        return None
    if qos.get("durability") not in {"volatile", "transient_local"}:
        return None
    if not _is_non_negative_int(qos.get("depth")) or qos["depth"] < 1:
        return None

    payload["measured_hz"] = measured_hz
    return payload


def _bounded_event_summary(value: Any) -> Any:
    """Bound event payloads embedded in the once-per-second status snapshot.

    The append-only JSONL event log and the event topic retain the authoritative
    payload.  The status topic is a dashboard snapshot, so allowing one large
    diagnostic String to be copied into every recent-event row makes rosbridge
    repeatedly serialize megabytes and can starve the Debug UI.
    """

    if isinstance(value, str):
        if len(value) <= MAX_EVENT_SUMMARY_STRING_CHARS:
            return value
        omitted = len(value) - MAX_EVENT_SUMMARY_STRING_CHARS
        return f"{value[:MAX_EVENT_SUMMARY_STRING_CHARS]}… [{omitted} chars omitted]"
    if isinstance(value, dict):
        snapshot = {
            str(key): _bounded_event_summary(item)
            for key, item in list(value.items())[:MAX_EVENT_SUMMARY_ITEMS]
        }
        return snapshot
    if isinstance(value, (list, tuple)):
        items = [
            _bounded_event_summary(item)
            for item in list(value)[:MAX_EVENT_SUMMARY_ITEMS]
        ]
        if len(value) > MAX_EVENT_SUMMARY_ITEMS:
            items.append(f"[{len(value) - MAX_EVENT_SUMMARY_ITEMS} items omitted]")
        return items
    return value


def _message_source_stamp(msg: Any) -> dict[str, int] | None:
    """Return the original ROS stamp for a local diagnostic record."""

    stamp = getattr(msg, "stamp", None)
    if stamp is None:
        return None
    try:
        return {"sec": int(stamp.sec), "nanosec": int(stamp.nanosec)}
    except (AttributeError, TypeError, ValueError):
        return None


@dataclass(slots=True)
class InputStats:
    arrivals: deque[float] = field(default_factory=lambda: deque(maxlen=512))
    sizes: deque[tuple[float, int]] = field(default_factory=lambda: deque(maxlen=512))
    last_received_monotonic: float = 0.0
    source_delay_sec: float | None = None
    last_sample: str = ""
    message_count: int = 0
    reported_rate_hz: float | None = None
    reported_payload_bytes: int | None = None
    reported_source_topic: str = ""
    reported_published_count: int | None = None
    reported_dropped_count: int | None = None
    reported_qos: str = ""


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


def _configured_string_input_qos(qos_name: object) -> QoSProfile:
    """Build the configured monitor QoS without latching live speech text."""

    transient_local = str(qos_name or "").strip().lower() == "reliable_transient_local"
    return QoSProfile(
        history=QoSHistoryPolicy.KEEP_LAST,
        depth=1 if transient_local else 20,
        reliability=QoSReliabilityPolicy.RELIABLE,
        durability=(
            QoSDurabilityPolicy.TRANSIENT_LOCAL
            if transient_local
            else QoSDurabilityPolicy.VOLATILE
        ),
    )


def _policy_name(value: Any) -> str:
    name = getattr(value, "name", "")
    return str(name or value).split(".")[-1].lower()


def _node_identity(namespace: str, name: str) -> str:
    prefix = namespace.rstrip("/")
    return f"{prefix}/{name}" if prefix else f"/{name}"


class IntegrationDebugNode(Node):
    """Monitor external inputs and manually exercise public integration endpoints."""

    def __init__(
        self,
        *,
        capability_override: str | None = None,
        owner_role: str | None = None,
    ) -> None:
        super().__init__("integration_debug_gateway")
        self._owner_role = str(owner_role or "combined").strip().casefold()
        if self._owner_role not in {"combined", "observer", "control"}:
            raise ValueError("debug owner_role must be combined, observer, or control")
        is_control_owner = self._owner_role == "control"
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
        self.declare_parameter(
            "capabilities",
            os.environ.get("TASKPLANNER_DEBUG_CAPABILITIES", "full"),
        )
        self.declare_parameter(
            "status_topic",
            DEBUG_CONTROL_STATUS_TOPIC if is_control_owner else DEBUG_STATUS_TOPIC,
        )
        self.declare_parameter(
            "events_topic",
            DEBUG_CONTROL_EVENTS_TOPIC if is_control_owner else DEBUG_EVENTS_TOPIC,
        )
        self.declare_parameter(
            "readiness_topic",
            (
                DEBUG_CONTROL_READINESS_TOPIC
                if is_control_owner
                else DEBUG_READINESS_TOPIC
            ),
        )
        self.declare_parameter(
            "control_status_topic",
            DEBUG_CONTROL_STATUS_TOPIC if self._owner_role == "observer" else "",
        )
        self.declare_parameter(
            "readiness_service",
            (
                DEBUG_CONTROL_READINESS_SERVICE
                if is_control_owner
                else DEBUG_READINESS_SERVICE
            ),
        )
        self.declare_parameter(
            "retraction_service_name", RETRACTION_SERVICE_DEFAULT_NAME
        )
        self.declare_parameter(
            "robot_endpoint_source",
            os.environ.get("TASKPLANNER_DEBUG_ROBOT_ENDPOINT_SOURCE", "external"),
        )
        self.declare_parameter("virtual_robot_enabled", False)
        self.declare_parameter(
            "virtual_retraction_service_name",
            VIRTUAL_RETRACTION_SERVICE_DEFAULT_NAME,
        )
        self.declare_parameter(
            "virtual_tool_handover_name",
            VIRTUAL_TOOL_HANDOVER_DEFAULT_NAME,
        )
        self.declare_parameter(
            "virtual_bed_robot_status_topic",
            VIRTUAL_BED_ROBOT_STATUS_DEFAULT_TOPIC,
        )
        self._capabilities = DebugCapabilities.parse(
            capability_override
            if capability_override is not None
            else self.get_parameter("capabilities").value
        )
        self._status_topic = str(self.get_parameter("status_topic").value).strip()
        self._events_topic = str(self.get_parameter("events_topic").value).strip()
        self._readiness_topic = str(
            self.get_parameter("readiness_topic").value
        ).strip()
        self._control_status_topic = str(
            self.get_parameter("control_status_topic").value
        ).strip()
        self._readiness_service_name = str(
            self.get_parameter("readiness_service").value
        ).strip()
        if (
            not self._status_topic
            or not self._events_topic
            or not self._readiness_topic
            or not self._readiness_service_name
        ):
            raise ValueError("Debug status, event, and readiness names must not be empty")
        if (
            self._owner_role == "observer"
            and self._control_status_topic == self._status_topic
        ):
            raise ValueError("observer control_status_topic must differ from status_topic")
        config_path = str(self.get_parameter("config_path").value)
        external_retraction_service_name = str(
            self.get_parameter("retraction_service_name").value
        ).strip()
        if not external_retraction_service_name:
            raise ValueError("retraction_service_name must not be empty")
        self._virtual_retraction_service_name = str(
            self.get_parameter("virtual_retraction_service_name").value
        ).strip()
        self._virtual_tool_handover_name = str(
            self.get_parameter("virtual_tool_handover_name").value
        ).strip()
        self._virtual_bed_robot_status_topic = str(
            self.get_parameter("virtual_bed_robot_status_topic").value
        ).strip()
        if (
            not self._virtual_retraction_service_name
            or not self._virtual_tool_handover_name
            or not self._virtual_bed_robot_status_topic
        ):
            raise ValueError("virtual robot endpoint names must not be empty")
        if self._virtual_retraction_service_name == external_retraction_service_name:
            raise ValueError(
                "virtual retraction Service must use a dedicated endpoint"
            )
        self._virtual_robot_enabled = bool(
            self.get_parameter("virtual_robot_enabled").value
        )
        selected_robot_source = str(
            self.get_parameter("robot_endpoint_source").value
        ).strip().lower()
        if selected_robot_source not in {"external", "virtual"}:
            raise ValueError("robot_endpoint_source must be external or virtual")
        if selected_robot_source == "virtual" and not self._virtual_robot_enabled:
            raise ValueError(
                "robot_endpoint_source virtual requires virtual_robot_enabled"
            )
        self._robot_endpoint_source = selected_robot_source
        self._external_retraction_service_name = external_retraction_service_name
        self._retraction_service_name = (
            self._virtual_retraction_service_name
            if selected_robot_source == "virtual"
            else self._external_retraction_service_name
        )
        self._config = load_config(config_path)
        # The policy is the only place a Debug request may select an endpoint
        # and ROS type.  These runtime values only replace an existing external
        # or virtual route; they cannot widen the catalog.
        self._typed_dispatcher = DispatchPolicy.from_dispatch_config(
            self._config["dispatch"]
        ).with_endpoint_overrides(
            {
                ("tool_handover", "external"): TOOL_HANDOVER_DEFAULT_NAME,
                ("tool_handover", "virtual"): self._virtual_tool_handover_name,
                ("retraction_service", "external"): (
                    self._external_retraction_service_name
                ),
                ("retraction_service", "virtual"): (
                    self._virtual_retraction_service_name
                ),
            }
        )
        self._lock = threading.RLock()
        self._log_lock = threading.Lock()
        self._auxiliary_lock = threading.RLock()
        self._callback_group = ReentrantCallbackGroup()
        self._monitor_window_sec = max(
            1.0, float(self._config.get("monitor_window_sec", 5.0))
        )
        self._heartbeat_timeout_sec = max(
            2.0, float(self._config.get("heartbeat_timeout_sec", 6.0))
        )
        self._action_watchdog_policy = load_action_watchdog_policy(self._config)
        self._armed = False
        self._manual_control_scope = "none"
        self._acknowledged_blocked_nodes: set[str] = set()
        self._fault_locked = False
        self._last_heartbeat_monotonic = 0.0
        self._last_error = ""
        self._active_route = ""
        self._active_command_id = ""
        self._active_goal_handle: Any | None = None
        self._action_status: dict[str, Any] = self._idle_action_status()
        self._voice_auto_execute = False
        self._retraction_state = RetractionState.IDLE
        self._last_retraction_rejection_reason = ""
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
        self._planner_coexistence_allowed = (
            os.environ.get("TASKPLANNER_DEBUG_ALLOW_PLANNER_COEXISTENCE", "")
            .strip()
            .lower()
            in {"1", "true", "yes", "on"}
        )
        self._network_locked_to_runtime = (
            os.environ.get("TASKPLANNER_DEBUG_LOCK_TO_RUNTIME_NETWORK", "")
            .strip()
            .lower()
            in {"1", "true", "yes", "on"}
        )
        self._operational_state_max_age_sec = max(
            0.5,
            float(
                os.environ.get(
                    "TASKPLANNER_DEBUG_OPERATIONAL_STATE_MAX_AGE_SEC", "3.0"
                )
                or 3.0
            ),
        )
        self._operational_state_expected_publisher = (
            os.environ.get(
                "TASKPLANNER_DEBUG_OPERATIONAL_STATE_PUBLISHER",
                "/or_digital_twin",
            ).strip()
            or "/or_digital_twin"
        )
        self._operational_state_received = False
        self._operational_state_received_monotonic = 0.0
        self._operational_active_bundle = ""
        self._operational_running = False
        self._operational_execution_state = "unknown"
        self._operational_active_robot_task_id = ""
        self._operational_robot_state = "unknown"
        self._operational_cleaner_busy = False
        self._restart_scheduled = False
        self._session_dir = run_root / "debug" / self._session_id
        self._session_dir.mkdir(parents=True, exist_ok=True)
        self._event_log_path = self._session_dir / "events.jsonl"
        # This is intentionally separate from both the Debug session event log
        # and the operational surgery record owner.  It is observer-only and
        # begins only after a real scenario run becomes active.
        self._scenario_debug_recorder: ScenarioDebugRecorder | None = None
        self._scenario_debug_signatures: dict[str, str] = {}
        if self._owner_role == "observer":
            self._scenario_debug_recorder = ScenarioDebugRecorder(
                run_root / "debug" / "scenario-recordings"
            )

        asr_config = dict(self._config.get("asr", {}))
        self._asr_topic = str(
            asr_config.get("topic", "/sensors/surgeon/sentence")
        ).strip()
        self._observed_utterance_topic = str(
            dict(self._config.get("voice", {})).get(
                "observed_utterance_topic", "/surgery/audio/observed_utterance"
            )
        ).strip()
        if (
            not self._observed_utterance_topic
            or self._observed_utterance_topic == self._asr_topic
        ):
            raise ValueError(
                "observed utterance topic must be distinct from the raw ASR topic"
            )
        self._asr_sentence_pub: Any | None = None
        # A Debug ASR session may coexist with the operational runtime for
        # monitoring, but its graph-visible sentence publisher must never make
        # the live preflight pass before Puzzle ASR is actually connected.
        self._asr_capture_requested = False
        self._manual_sentence_pub: Any | None = None
        self._typed_topic_publishers: dict[tuple[str, str], Any] = {}
        self._asr_cloud_url = ""
        self._asr_lan_url = ""
        self._asr_endpoint = "disabled"
        self._asr_server_url = ""
        self._asr: Any | None = None
        if self._capability_enabled("asr"):
            # Import only for the focused ASR owner: this module eventually
            # loads PipeWire/sounddevice probes on demand.
            from integration_debug.asr_runtime import AsrMicrophoneRuntime

            self._asr_cloud_url = os.environ.get("PUZZLE_ASR_URL") or str(
                asr_config.get(
                    "cloud_server_url",
                    asr_config.get("default_server_url", DEFAULT_CLOUD_SERVER_URL),
                )
            )
            self._asr_lan_url = os.environ.get("PUZZLE_ASR_LAN_URL") or str(
                asr_config.get("lan_server_url", DEFAULT_LAN_SERVER_URL)
            )
            requested_asr_endpoint = os.environ.get("PUZZLE_ASR_ENDPOINT") or str(
                asr_config.get("default_endpoint", DEFAULT_ASR_ENDPOINT)
            )
            self._asr_endpoint, self._asr_server_url = resolve_puzzle_asr_endpoint(
                requested_asr_endpoint,
                cloud_url=self._asr_cloud_url,
                lan_url=self._asr_lan_url,
            )

            self._asr = AsrMicrophoneRuntime(
                default_url=self._asr_server_url,
                topic=self._asr_topic,
                output_dir=self._session_dir / "asr",
                capture_lock_path=os.environ.get(
                    "TASKPLANNER_ASR_CAPTURE_LOCK",
                    "/taskplanner-runs/asr/microphone.lock",
                ),
            )
        self._operational_asr_status: dict[str, Any] = {}
        self._operational_asr_status_received_monotonic = 0.0
        self._surgery_record: Any | None = None
        if self._capability_enabled("record"):
            from integration_debug.surgery_record_runtime import SurgeryRecordRuntime

            record_config = dict(self._config.get("surgery_record", {}))

            self._surgery_record = SurgeryRecordRuntime(
                input_dir=os.environ.get(
                    "TASKPLANNER_SURGERY_RECORD_INPUT_DIR",
                    str(record_config.get("input_dir", "/surgery-record-inputs")),
                ),
                default_endpoint=os.environ.get(
                    "PUZZLE_SURGERY_RECORD_ENDPOINT",
                    str(
                        record_config.get(
                            "default_endpoint",
                            "https://192.168.1.5:6627/api/v1/surgery/img_texts",
                        )
                    ),
                ),
                api_key_file=os.environ.get(
                    "PUZZLE_SURGERY_RECORD_API_KEY_FILE",
                    "/run/taskplanner-secrets/puzzle-surgery-record-api-key",
                ),
                allowed_endpoints=tuple(
                    str(endpoint)
                    for endpoint in record_config.get(
                        "allowed_endpoints",
                        [
                            "https://192.168.1.5:6627/api/v1/surgery/img_texts",
                        ],
                    )
                ),
                timeout_sec=float(record_config.get("timeout_sec", 35.0)),
            )

        self._status_pub = self.create_publisher(String, self._status_topic, 10)
        self._event_pub = self.create_publisher(String, self._events_topic, 50)
        self._readiness_pub = self.create_publisher(
            String, self._readiness_topic, 10
        )
        self._control_owner_status: dict[str, Any] | None = None
        self._control_owner_status_received_monotonic = 0.0
        self._control_owner_status_subscription: Any | None = None
        if self._owner_role == "observer" and self._control_status_topic:
            self._control_owner_status_subscription = self.create_subscription(
                String,
                self._control_status_topic,
                self._on_control_owner_status,
                QoSProfile(
                    history=QoSHistoryPolicy.KEEP_LAST,
                    depth=1,
                    reliability=QoSReliabilityPolicy.RELIABLE,
                    durability=QoSDurabilityPolicy.VOLATILE,
                ),
                callback_group=self._callback_group,
            )
        self._command_service: Any | None = None
        if not self._capabilities.observer_only:
            self._command_service = self.create_service(
                IntegrationDebugCommand,
                "/integration/debug/command",
                self._handle_command,
                callback_group=self._callback_group,
            )
        self._operational_asr_client: Any | None = None
        if self._capability_enabled("asr"):
            self._operational_asr_client = self.create_client(
                AsrControl,
                OPERATIONAL_ASR_CONTROL_SERVICE,
                callback_group=self._callback_group,
            )
        self._operational_asr_status_subscription = self.create_subscription(
            String,
            OPERATIONAL_ASR_STATUS_TOPIC,
            self._on_operational_asr_status,
            QoSProfile(
                history=QoSHistoryPolicy.KEEP_LAST,
                depth=1,
                reliability=QoSReliabilityPolicy.RELIABLE,
                durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
            ),
            callback_group=self._callback_group,
        )
        self._heartbeat_subscription = self.create_subscription(
            String,
            "/integration/debug/heartbeat",
            self._on_heartbeat,
            QoSProfile(
                history=QoSHistoryPolicy.KEEP_LAST,
                depth=5,
                reliability=QoSReliabilityPolicy.RELIABLE,
                durability=QoSDurabilityPolicy.VOLATILE,
            ),
            callback_group=self._callback_group,
        )
        self._operational_state_subscription = self.create_subscription(
            SimulationState,
            "/simulation/state",
            self._on_operational_state,
            QoSProfile(
                history=QoSHistoryPolicy.KEEP_LAST,
                depth=5,
                reliability=QoSReliabilityPolicy.RELIABLE,
                durability=QoSDurabilityPolicy.VOLATILE,
            ),
            callback_group=self._callback_group,
        )
        self._scenario_debug_subscriptions: list[Any] = []
        if self._scenario_debug_recorder is not None:
            self._scenario_debug_subscriptions = [
                self.create_subscription(
                    BTDecision,
                    "/bt/decision",
                    self._on_scenario_debug_bt_decision,
                    50,
                    callback_group=self._callback_group,
                ),
                self.create_subscription(
                    WorldState,
                    "/twin/world_state",
                    self._on_scenario_debug_world_state,
                    50,
                    callback_group=self._callback_group,
                ),
                self.create_subscription(
                    TwinEvent,
                    "/twin/events",
                    self._on_scenario_debug_twin_event,
                    100,
                    callback_group=self._callback_group,
                ),
                self.create_subscription(
                    VLMResult,
                    "/vlm/result",
                    self._on_scenario_debug_vlm_result,
                    50,
                    callback_group=self._callback_group,
                ),
                self.create_subscription(
                    SkillStatus,
                    "/skill/status",
                    self._on_scenario_debug_skill_status,
                    100,
                    callback_group=self._callback_group,
                ),
                self.create_subscription(
                    ExecutionTrace,
                    "/surgery/execution_trace",
                    self._on_scenario_debug_execution_trace,
                    100,
                    callback_group=self._callback_group,
                ),
                self.create_subscription(
                    BedRobotArmGroupStatus,
                    "/bed_robot_arm_group/status",
                    self._on_scenario_debug_bed_robot_status,
                    100,
                    callback_group=self._callback_group,
                ),
            ]
        self._readiness_service = self.create_service(
            Trigger,
            self._readiness_service_name,
            self._handle_readiness,
            callback_group=self._callback_group,
        )

        self._input_stats: dict[str, InputStats] = {}
        self._input_subscriptions: list[Any] = []
        self._bed_robot_arm_status_received = False
        self._bed_robot_arm_status_received_monotonic = 0.0
        self._bed_robot_arm_status_source_stamp_sec = 0.0
        self._bed_robot_arm_status_revision: int | None = None
        self._bed_robot_arm_status_max_age_sec = max(
            0.1,
            float(self._config.get("bed_robot_arm_status_max_age_sec", 3.0)),
        )
        self._bed_robot_arm_status_summary: dict[str, Any] = {}
        self._bed_robot_arm_status_sources: dict[str, dict[str, Any]] = {
            "external": {},
            "virtual": {},
        }
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
                    _configured_string_input_qos(row.get("qos")),
                    callback_group=self._callback_group,
                )
            elif message_type == "surgical_msgs/msg/SpeechUtterance":
                subscription = self.create_subscription(
                    SpeechUtterance,
                    topic,
                    lambda msg, source_topic=topic: self._on_speech_utterance_input(
                        source_topic, msg
                    ),
                    _configured_string_input_qos(row.get("qos")),
                    callback_group=self._callback_group,
                )
            elif message_type == "surgical_msgs/msg/InputSourceStatus":
                subscription = self.create_subscription(
                    InputSourceStatus,
                    topic,
                    lambda msg, source_topic=topic: (
                        self._on_input_source_status(source_topic, msg)
                    ),
                    QoSProfile(
                        history=QoSHistoryPolicy.KEEP_LAST,
                        depth=10,
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
                    QoSProfile(
                        history=QoSHistoryPolicy.KEEP_LAST,
                        # Debug monitoring never needs an old-frame backlog,
                        # but FLIR JPEGs span enough UDP fragments that a
                        # BEST_EFFORT reader can lose every sample on a busy
                        # integration LAN. Match the provider's RELIABLE QoS
                        # while keeping only the newest delivered frame.
                        depth=1,
                        reliability=QoSReliabilityPolicy.RELIABLE,
                        durability=QoSDurabilityPolicy.VOLATILE,
                    ),
                    callback_group=self._callback_group,
                )
            else:
                raise ValueError(f"unsupported debug input type: {message_type}")
            self._input_subscriptions.append(subscription)

        # Legacy clients stay as compact adapters for existing clinical
        # payload codecs.  Every new catalog endpoint goes through the dynamic
        # rosidl client caches below, so adding an installed ROS type does not
        # require editing this node.
        self._typed_action_clients: dict[tuple[str, str], ActionClient] = {}
        self._typed_service_clients: dict[tuple[str, str], Any] = {}
        self._external_tool_client: ActionClient | None = None
        self._virtual_tool_client: ActionClient | None = None
        self._external_retraction_client: Any | None = None
        self._virtual_retraction_client: Any | None = None
        self._tool_client: ActionClient | None = None
        self._retraction_client: Any | None = None
        if self._capability_enabled("control"):
            self._external_tool_client = ActionClient(
                self,
                ExecuteToolHandover,
                self._typed_dispatcher.endpoint_for("tool_handover", "external"),
                callback_group=self._callback_group,
            )
            self._virtual_tool_client = ActionClient(
                self,
                ExecuteToolHandover,
                self._typed_dispatcher.endpoint_for("tool_handover", "virtual"),
                callback_group=self._callback_group,
            )
            self._external_retraction_client = self.create_client(
                ExecuteRetractionCommand,
                self._typed_dispatcher.endpoint_for("retraction_service", "external"),
                callback_group=self._callback_group,
            )
            self._virtual_retraction_client = self.create_client(
                ExecuteRetractionCommand,
                self._typed_dispatcher.endpoint_for("retraction_service", "virtual"),
                callback_group=self._callback_group,
            )
            self._tool_client = (
                self._virtual_tool_client
                if self._robot_endpoint_source == "virtual"
                else self._external_tool_client
            )
            self._retraction_client = (
                self._virtual_retraction_client
                if self._robot_endpoint_source == "virtual"
                else self._external_retraction_client
            )
        self._bed_robot_arm_status_subscriptions = [
            self.create_subscription(
                BedRobotArmStateArray,
                BED_ROBOT_STATUS_DEFAULT_TOPIC,
                lambda msg: self._on_bed_robot_arm_status(msg, "external"),
                _event_qos(),
                callback_group=self._callback_group,
            )
        ]
        self._bed_robot_arm_status_subscriptions.append(self.create_subscription(
            BedRobotArmStateArray,
            self._virtual_bed_robot_status_topic,
            lambda msg: self._on_bed_robot_arm_status(msg, "virtual"),
            _event_qos(),
            callback_group=self._callback_group,
        ))

        self._output_states: dict[str, OutputState] = {}
        self._output_publishers: dict[str, Any] = {}
        self._output_qos_profiles: dict[str, QoSProfile] = {}
        for row in self._config["outputs"]:
            topic = str(row["topic"])
            message_type = str(row["type"])
            message_class = PUBLIC_OUTPUT_TYPES.get(message_type)
            if message_class is None:
                raise ValueError(f"unsupported debug output type: {message_type}")
            qos = _event_qos() if str(row.get("qos")) == "event" else _snapshot_qos()
            self._output_qos_profiles[topic] = qos
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
        self.create_timer(
            0.1,
            self._drain_auxiliary_events,
            callback_group=self._callback_group,
        )
        self._record(
            "session_started",
            {
                "config_path": config_path,
                "ros_domain_id": os.environ.get("ROS_DOMAIN_ID", "0"),
                "robot_endpoint_source": self._robot_endpoint_source,
            },
        )

    @staticmethod
    def _idle_action_status() -> dict[str, Any]:
        return {
            "route": "",
            "command_id": "",
            "command": "",
            "response_semantics": "action",
            "endpoint": "",
            "type": "",
            "timeout_sec": 0.0,
            "request_accepted": None,
            "result_code": None,
            "response_message": "",
            "state": "idle",
            "progress": 0.0,
            "success": False,
            "terminal": True,
            "reason_code": "",
            "recovery_required": False,
            "started_monotonic": 0.0,
            "last_update_monotonic": 0.0,
            "server_unavailable_since_monotonic": 0.0,
            "recovery_detected_monotonic": 0.0,
        }

    def _capability_enabled(self, name: str) -> bool:
        capabilities = getattr(self, "_capabilities", None)
        # Narrow test harnesses intentionally instantiate only one method.
        # Keep their historical full-capability behavior unless they opt into
        # the explicit capability plan.
        return True if capabilities is None else capabilities.enabled(name)

    def _capability_unavailable_message(self, capability: str) -> str:
        return (
            f"Debug capability '{capability}' is not started; "
            f"restart only debug-{capability} or use a full Debug profile"
        )

    @staticmethod
    def _client_is_ready(client: Any | None, method: str) -> bool:
        if client is None:
            return False
        try:
            return bool(getattr(client, method)())
        except Exception:
            return False

    def _selected_tool_client_ready(self) -> bool:
        return IntegrationDebugNode._client_is_ready(
            getattr(self, "_tool_client", None), "server_is_ready"
        )

    def _selected_retraction_client_ready(self) -> bool:
        return IntegrationDebugNode._client_is_ready(
            getattr(self, "_retraction_client", None), "service_is_ready"
        )

    @staticmethod
    def _disabled_asr_snapshot() -> dict[str, Any]:
        return {
            "available": False,
            "dependency_error": "debug ASR capability is not started",
            "state": "UNAVAILABLE",
            "endpoint_id": "cloud",
            "server_url": "",
            "topic": "",
            "device_id": None,
            "device_name": "",
            "devices": [],
            "device_status": "HOST_AUDIO_UNAVAILABLE",
            "device_message": "observer profile does not open PipeWire/USB audio",
            "connected": False,
            "audio_level_dbfs": -120.0,
            "peak_level_dbfs": -120.0,
            "elapsed_sec": 0.0,
            "blocks_captured": 0,
            "input_dropped": 0,
            "partial_text": "",
            "finals": [],
            "last_error": "",
            "recording_path": "",
            "transcript_path": "",
            "sample_rate": 0,
            "channels": 0,
            "sample_width_bits": 0,
            "block_frames": 0,
            "wire_chunk_bytes": 0,
            "input_sample_rate": 0,
            "input_channels": 0,
            "input_block_frames": 0,
            "resampling": False,
            "sent_chunks": 0,
            "responses": 0,
            "dropped_chunks": 0,
            "sessions": 0,
            "padded_final_bytes": 0,
            "pending_chunks": 0,
        }

    @staticmethod
    def _disabled_surgery_record_snapshot() -> dict[str, Any]:
        return {
            "state": "IDLE",
            "active_request_id": "",
            "default_endpoint": "",
            "input_dir": "",
            "examples": [],
            "last_error": "debug record capability is not started",
            "last_result": {},
            "history": [],
            "api_key_configured": False,
            "contract": {
                "method": "POST",
                "content_type": "application/json",
                "auth_header": "X-API-Key",
                "max_text_characters": 0,
                "max_body_bytes": 0,
                "server_timeout_sec": 0,
                "generated_record_body_returned": False,
                "result_lookup_defined": False,
            },
        }

    def _session_state(self) -> str:
        if self._fault_locked:
            return "FAULT_LOCKED"
        if self._active_command_id:
            return "BUSY"
        if self._armed:
            return "ARMED"
        return "MONITOR_ONLY"

    def _disarm_locked(self) -> None:
        """Clear every session-scoped write authorization while holding the lock."""

        self._armed = False
        self._manual_control_scope = "none"
        self._voice_auto_execute = False
        self._acknowledged_blocked_nodes.clear()

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
            "payload": _bounded_event_summary(payload),
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
        stream_status = _parse_viplab_stream_status(text)
        source_delay: float | None = None
        if stream_status is not None:
            stamp = stream_status["source_stamp"]
            source_sec = float(stamp["sec"]) + float(stamp["nanosec"]) / 1e9
            try:
                ros_now_sec = self.get_clock().now().nanoseconds / 1e9
                source_delay = max(0.0, ros_now_sec - source_sec)
            except AttributeError:
                source_delay = None
        with self._lock:
            stats = self._input_stats[topic]
            stats.arrivals.append(now)
            stats.sizes.append((now, len(msg.data.encode("utf-8"))))
            stats.last_received_monotonic = now
            stats.message_count += 1
            if stream_status is not None:
                stats.source_delay_sec = source_delay
                stats.reported_rate_hz = stream_status["measured_hz"]
                stats.reported_payload_bytes = stream_status["payload_bytes"]
                stats.reported_source_topic = stream_status["source_topic"]
                stats.reported_published_count = stream_status["published_count"]
                stats.reported_dropped_count = stream_status["dropped_count"]
                qos = stream_status["qos"]
                stats.reported_qos = (
                    f"{qos['reliability']}/{qos['durability']}/"
                    f"keep_last({qos['depth']})"
                )
                stats.last_sample = (
                    f"{stream_status['stream_id']} · "
                    f"{stream_status['measured_hz']:.2f} Hz · "
                    f"{stream_status['payload_bytes']} bytes · "
                    f"dropped {stream_status['dropped_count']}"
                )
            else:
                stats.last_sample = text[:240]
                if topic.startswith("/synced/") and topic.endswith("/status"):
                    stats.reported_rate_hz = None
                    stats.reported_payload_bytes = None
                    stats.reported_source_topic = ""
                    stats.reported_published_count = None
                    stats.reported_dropped_count = None
                    stats.reported_qos = ""
        # String inputs are telemetry/status facts only.  Executable speech is
        # consumed once by CommandRouter; Debug never interprets or re-dispatches
        # a String topic as a second voice command path.

    def _on_speech_utterance_input(
        self, topic: str, msg: SpeechUtterance
    ) -> None:
        """Project CommandRouter's observed speech relay without re-admission.

        This callback intentionally has no dispatch, parser, selector, or control
        call.  A final ``SpeechUtterance`` is already an observation of the
        command-router boundary, not a new voice ingress for Debug.
        """

        now = time.monotonic()
        utterance_id = str(getattr(msg, "utterance_id", "") or "").strip()
        text = str(getattr(msg, "text", "") or "").strip()
        source = str(getattr(msg, "source", "") or "").strip()
        speaker_role = str(getattr(msg, "speaker_role", "") or "").strip()
        is_final = bool(getattr(msg, "is_final", False))
        has_confidence = bool(getattr(msg, "has_confidence", False))
        confidence = float(getattr(msg, "confidence", 0.0) or 0.0)
        byte_size = sum(
            len(value.encode("utf-8"))
            for value in (utterance_id, text, source, speaker_role)
        )
        with self._lock:
            stats = self._input_stats[topic]
            stats.arrivals.append(now)
            stats.sizes.append((now, byte_size))
            stats.last_received_monotonic = now
            stats.message_count += 1
            stats.last_sample = (
                f"{utterance_id[:64] or '-'} · {source[:48] or '-'} · "
                f"{'final' if is_final else 'partial'} · {text[:160]}"
            )
            if is_final and text:
                self._last_sentence = text
                # Keep the established status shape for older UI readers, but
                # explicitly avoid implying a Debug-owned command parse.
                self._last_voice_parse = {
                    "matched": False,
                    "ambiguous": False,
                    "operation": "",
                    "payload": {},
                    "reason": "observed_by_command_router",
                }

        self._record(
            "observed_speech_utterance",
            {
                "topic": topic,
                "utterance_id": utterance_id,
                "text": text[:500],
                "source": source,
                "speaker_role": speaker_role,
                "is_final": is_final,
                "has_confidence": has_confidence,
                "confidence": round(confidence, 4) if has_confidence else None,
            },
        )

    def _on_heartbeat(self, msg: String) -> None:
        if str(msg.data).strip() != self._session_id:
            return
        with self._lock:
            self._last_heartbeat_monotonic = time.monotonic()

    def _on_control_owner_status(self, msg: String) -> None:
        """Cache the private control projection for the public observer."""

        raw = str(msg.data or "")
        if not raw or len(raw) > 1_000_000:
            return
        try:
            parsed = control_owner_status(json.loads(raw))
        except (TypeError, ValueError):
            return
        if parsed is None:
            return
        with self._lock:
            self._control_owner_status = parsed
            self._control_owner_status_received_monotonic = time.monotonic()

    def _on_operational_asr_status(self, msg: String) -> None:
        raw = str(msg.data or "")
        if not raw or len(raw) > 1_000_000:
            return
        try:
            envelope = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(envelope, dict) or envelope.get("schema") != OPERATIONAL_ASR_STATUS_SCHEMA:
            return
        asr = envelope.get("asr")
        if not isinstance(asr, dict):
            return
        with self._lock:
            self._operational_asr_status = dict(asr)
            self._operational_asr_status_received_monotonic = time.monotonic()

    def _on_operational_state(self, msg: SimulationState) -> None:
        with self._lock:
            active_bundle = (
                str(msg.active_bundle).strip()
                or str(msg.procedure_id).strip()
            )
            self._operational_state_received = True
            self._operational_state_received_monotonic = time.monotonic()
            self._operational_active_bundle = active_bundle
            self._operational_running = bool(msg.running)
            self._operational_execution_state = (
                str(msg.execution_state).strip().lower() or "unknown"
            )
            self._operational_active_robot_task_id = str(
                msg.active_robot_task_id
            ).strip()
            self._operational_robot_state = (
                str(msg.robot_state).strip().lower() or "unknown"
            )
            self._operational_cleaner_busy = bool(msg.cleaner_busy)
        self._update_scenario_debug_lifecycle(msg)

    def _update_scenario_debug_lifecycle(self, msg: SimulationState) -> None:
        """Start/stop the observer-owned journal at scenario boundaries only."""

        recorder = self._scenario_debug_recorder
        if recorder is None:
            return
        execution_state = str(msg.execution_state or "").strip().casefold()
        run_id = str(msg.procedure_run_id or "").strip()
        running = bool(msg.running) and execution_state == "running"
        if running and run_id:
            previous_run_id = recorder.active_procedure_run_id
            recorder.start(
                procedure_run_id=run_id,
                context={
                    "procedure_id": str(msg.procedure_id),
                    "active_bundle": str(msg.active_bundle),
                    "execution_state": execution_state,
                    "filtered_phase": str(msg.filtered_phase),
                    "robot_state": str(msg.robot_state),
                },
                source_stamp=_message_source_stamp(msg),
            )
            if previous_run_id != run_id:
                with self._lock:
                    self._scenario_debug_signatures.clear()
            return
        if recorder.active_procedure_run_id and not bool(msg.running) and execution_state in {
            "idle",
            "completed",
            "failed",
            "stopped",
            "cancelled",
        }:
            recorder.stop(execution_state or "scenario_stopped")
            with self._lock:
                self._scenario_debug_signatures.clear()

    def _append_scenario_debug(
        self,
        record_type: str,
        payload: dict[str, Any],
        msg: Any,
        *,
        deduplicate: bool = False,
    ) -> None:
        """Append one read-only diagnostic sample to the active scenario journal."""

        recorder = self._scenario_debug_recorder
        if recorder is None or not recorder.active_procedure_run_id:
            return
        signature = ""
        if deduplicate:
            signature = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            with self._lock:
                if self._scenario_debug_signatures.get(record_type) == signature:
                    return
                self._scenario_debug_signatures[record_type] = signature
        recorder.append(
            record_type,
            payload,
            source_stamp=_message_source_stamp(msg),
        )

    def _scenario_debug_message_is_current_run(self, msg: Any) -> bool:
        """Accept only telemetry structurally owned by the active journal run."""

        recorder = self._scenario_debug_recorder
        active_run_id = (
            str(recorder.active_procedure_run_id or "").strip()
            if recorder is not None
            else ""
        )
        return bool(active_run_id) and str(
            getattr(msg, "procedure_run_id", "") or ""
        ).strip() == active_run_id

    def _on_scenario_debug_bt_decision(self, msg: BTDecision) -> None:
        if not self._scenario_debug_message_is_current_run(msg):
            return
        self._append_scenario_debug(
            "bt_decision",
            {
                "source_topic": "/bt/decision",
                "procedure_run_id": str(msg.procedure_run_id),
                "decision": str(msg.decision),
                "selected_tool": str(msg.selected_tool),
                "selected_tool_instance_id": str(msg.selected_tool_instance_id),
                "request_generation": int(msg.request_generation),
                "selected_tool_lifecycle": str(msg.selected_tool_lifecycle),
                "next_required_transition": str(msg.next_required_transition),
                "action": str(msg.action),
                "handover_allowed": bool(msg.handover_allowed),
                "rationale": str(msg.rationale),
                "decision_reason": str(msg.decision_reason),
                "blocking_guard": str(msg.blocking_guard),
            },
            msg,
            deduplicate=True,
        )

    def _on_scenario_debug_world_state(self, msg: WorldState) -> None:
        if not self._scenario_debug_message_is_current_run(msg):
            return
        source_run_id = str(msg.procedure_run_id or "").strip()
        instruments = [
            {
                "instrument_id": str(item.instrument_id),
                "instance_id": str(item.instance_id),
                "location_type": str(item.location_type),
                "location_id": str(item.location_id),
                "owner": str(item.owner),
                "status": str(item.status),
                "confidence": float(item.confidence),
                "cleanliness_state": str(item.cleanliness_state),
                "contaminated": bool(item.contaminated),
                "reserved_for": str(item.reserved_for),
                "lifecycle_stage": str(item.lifecycle_stage),
                "next_required_transition": str(item.next_required_transition),
                "procedure_future_use_expected": bool(
                    item.procedure_future_use_expected
                ),
                "mayo_placement_evidence": str(item.mayo_placement_evidence),
                "mayo_reuse_confidence": float(item.mayo_reuse_confidence),
                "mayo_recovery_confidence": float(item.mayo_recovery_confidence),
                "mayo_evidence_source": str(item.mayo_evidence_source),
            }
            for item in msg.instrument_states
        ]
        self._append_scenario_debug(
            "dt_world_state",
            {
                "source_topic": "/twin/world_state",
                "procedure_id": str(msg.procedure_id),
                "procedure_run_id": source_run_id,
                "execution_state": str(msg.execution_state),
                "phase": {
                    "filtered": str(msg.filtered_phase),
                    "confidence": float(msg.phase_confidence),
                    "uncertain": bool(msg.phase_uncertain),
                    "stability": float(msg.phase_stability),
                },
                "explicit_request_tool": str(msg.explicit_request_tool),
                "surgeon": {
                    "intent": str(msg.surgeon_intent),
                    "request_tool": str(msg.surgeon_request_tool),
                    "request_instance_id": str(msg.surgeon_request_instance_id),
                    "request_generation": int(msg.surgeon_request_generation),
                    "explicit_request_voice_backed": bool(msg.explicit_request_voice_backed),
                    "additional_instance_assumed": bool(
                        msg.surgeon_request_additional_instance_assumed
                    ),
                    "ready_for_handover": bool(msg.surgeon_ready_for_handover),
                    "ready_for_retrieval": bool(msg.surgeon_ready_for_retrieval),
                },
                "handover_allowed": bool(msg.handover_allowed),
                "recovery_required": bool(msg.recovery_required),
                "safety_flags": list(msg.safety_flags),
                "expected_instruments": list(msg.expected_instruments),
                "available_instruments": list(msg.available_instruments),
                "right_hand_tool": str(msg.right_hand_tool),
                "right_hand_tool_instance_id": str(msg.right_hand_tool_instance_id),
                "left_hand_tool": str(msg.left_hand_tool),
                "left_hand_tool_instance_id": str(msg.left_hand_tool_instance_id),
                "prepositioned_tool": str(msg.prepositioned_tool),
                "prepositioned_tool_instance_id": str(msg.prepositioned_tool_instance_id),
                "prediction": {
                    "tool": str(msg.predicted_tool),
                    "confidence": float(msg.predicted_tool_confidence),
                    "stability_sec": float(msg.predicted_tool_stability_sec),
                    "autonomous_preparation_ready": bool(
                        msg.autonomous_preparation_ready
                    ),
                    "ranked": [
                        {
                            "rank": int(item.rank),
                            "instrument_id": str(item.instrument_id),
                            "confidence": float(item.confidence),
                            "stability_sec": float(item.stability_sec),
                        }
                        for item in msg.ranked_tool_predictions
                    ],
                },
                "implicit_request": {
                    "visible": bool(msg.implicit_request_visible),
                    "tool": str(msg.implicit_request_tool),
                    "hand_pose": str(msg.implicit_request_hand_pose),
                    "confidence": float(msg.implicit_request_confidence),
                    "stability_sec": float(msg.implicit_request_stability_sec),
                    "generation": int(msg.implicit_request_generation),
                },
                "cam4_mayo_hand_present": bool(msg.cam4_mayo_hand_present),
                "cleaner": {
                    "busy": bool(msg.cleaner_busy),
                    "remaining_sec": float(msg.cleaner_remaining_sec),
                },
                "pending_transition_tools": list(msg.pending_transition_tools),
                "active_recovery_tools": list(msg.active_recovery_tools),
                "active_recovery_tool_instances": list(
                    msg.active_recovery_tool_instances
                ),
                "active_robot_task": {
                    "id": str(msg.active_robot_task_id),
                    "type": str(msg.active_robot_task_type),
                    "tool_id": str(msg.active_robot_task_tool_id),
                    "tool_instance_id": str(msg.active_robot_task_tool_instance_id),
                    "arm": str(msg.active_robot_task_arm),
                    "source_anchor": str(msg.active_robot_task_source_anchor),
                    "target_anchor": str(msg.active_robot_task_target_anchor),
                },
                "instrument_states": instruments,
                "recent_event_types": list(msg.recent_event_types),
            },
            msg,
            deduplicate=True,
        )

    def _on_scenario_debug_twin_event(self, msg: TwinEvent) -> None:
        if not self._scenario_debug_message_is_current_run(msg):
            return
        self._append_scenario_debug(
            "dt_event",
            {
                "source_topic": "/twin/events",
                "procedure_run_id": str(msg.procedure_run_id),
                "event_type": str(msg.event_type),
                "instrument_id": str(msg.instrument_id),
                "instance_id": str(msg.instance_id),
                "phase_id": str(msg.phase_id),
                "location_id": str(msg.location_id),
                "location_type": str(msg.location_type),
                "owner": str(msg.owner),
                "status": str(msg.status),
                "confidence": float(msg.confidence),
                "detail_json": str(msg.detail_json),
                "arm": str(msg.arm),
                "source_location_id": str(msg.source_location_id),
                "source_location_type": str(msg.source_location_type),
                "target_location_id": str(msg.target_location_id),
                "target_location_type": str(msg.target_location_type),
                "target_owner": str(msg.target_owner),
                "cleaning_required": bool(msg.cleaning_required),
                "mode": str(msg.mode),
            },
            msg,
        )

    def _on_scenario_debug_vlm_result(self, msg: VLMResult) -> None:
        if not self._scenario_debug_message_is_current_run(msg):
            return
        phase_count = min(len(msg.phase_ids), len(msg.phase_confidences))
        observation_count = min(
            len(msg.observed_tool_ids),
            len(msg.observed_location_ids),
            len(msg.observed_location_types),
            len(msg.observed_confidences),
        )
        self._append_scenario_debug(
            "recognition_vlm",
            {
                "source_topic": "/vlm/result",
                "procedure_run_id": str(msg.procedure_run_id),
                "source": str(msg.source),
                "source_epoch": int(msg.source_epoch),
                "source_sequence": int(msg.source_sequence),
                "correlation_id": str(msg.correlation_id),
                "schema_version": str(msg.schema_version),
                "summary": str(msg.summary),
                "phases": [
                    {
                        "id": str(msg.phase_ids[index]),
                        "confidence": float(msg.phase_confidences[index]),
                    }
                    for index in range(phase_count)
                ],
                "observations": [
                    {
                        "tool_id": str(msg.observed_tool_ids[index]),
                        "location_id": str(msg.observed_location_ids[index]),
                        "location_type": str(msg.observed_location_types[index]),
                        "confidence": float(msg.observed_confidences[index]),
                    }
                    for index in range(observation_count)
                ],
                "uncertainty": float(msg.uncertainty),
                "raw_json_excluded": True,
            },
            msg,
        )

    def _on_scenario_debug_skill_status(self, msg: SkillStatus) -> None:
        if not self._scenario_debug_message_is_current_run(msg):
            return
        self._append_scenario_debug(
            "action_skill_status",
            {
                "source_topic": "/skill/status",
                "procedure_run_id": str(msg.procedure_run_id),
                "command_id": str(msg.command_id),
                "action": str(msg.action),
                "instrument_id": str(msg.instrument_id),
                "state": str(msg.state),
                "success": bool(msg.success),
                "message": str(msg.message),
                "arm": str(msg.arm),
                "source_location_id": str(msg.source_location_id),
                "source_location_type": str(msg.source_location_type),
                "target_location_id": str(msg.target_location_id),
                "target_location_type": str(msg.target_location_type),
                "target_owner": str(msg.target_owner),
                "cleaning_required": bool(msg.cleaning_required),
                "mode": str(msg.mode),
                "progress": float(msg.progress),
                "elapsed_sec": float(msg.elapsed_sec),
                "remaining_sec": float(msg.remaining_sec),
            },
            msg,
        )

    def _on_scenario_debug_execution_trace(self, msg: ExecutionTrace) -> None:
        if not self._scenario_debug_message_is_current_run(msg):
            return
        self._append_scenario_debug(
            "action_service_trace",
            {
                "source_topic": "/surgery/execution_trace",
                "procedure_run_id": str(msg.procedure_run_id),
                "sequence": int(msg.sequence),
                "command_id": str(msg.command_id),
                "route": str(msg.route),
                "transport": str(msg.transport),
                "endpoint": str(msg.endpoint),
                "endpoint_source": str(msg.endpoint_source),
                "stage": str(msg.stage),
                "dispatch_submitted": bool(msg.dispatch_submitted),
                "terminal": bool(msg.terminal),
                "evidence": str(msg.evidence),
                "reason_code": str(msg.reason_code),
                "retraction_command": int(msg.retraction_command),
                "retraction_target_side": int(msg.retraction_target_side),
                "retraction_distance_m": float(msg.retraction_distance_m),
            },
            msg,
        )

    def _on_scenario_debug_bed_robot_status(
        self,
        msg: BedRobotArmGroupStatus,
    ) -> None:
        if not self._scenario_debug_message_is_current_run(msg):
            return
        self._append_scenario_debug(
            "retraction_service_status",
            {
                "source_topic": "/bed_robot_arm_group/status",
                "procedure_run_id": str(msg.procedure_run_id),
                "request_id": str(msg.request_id),
                "command_id": str(msg.command_id),
                "group_id": str(msg.group_id),
                "operation": str(msg.operation),
                "arm_id": str(msg.arm_id),
                "target_tool_id": str(msg.target_tool_id),
                "adjustment_mode": str(msg.adjustment_mode),
                "target_retractor_id": str(msg.target_retractor_id),
                "direction_frame": str(msg.direction_frame),
                "state": str(msg.state),
                "outcome": str(msg.outcome),
                "terminal": bool(msg.terminal),
                "success": bool(msg.success),
                "message": str(msg.message),
                "direction": str(msg.direction),
                "axis": str(msg.axis),
                "distance_mm": float(msg.distance_mm),
                "distance_origin": str(msg.distance_origin),
                "raw_distance_text": str(msg.raw_distance_text),
                "end_effector_profile": str(msg.end_effector_profile),
                "confidence": float(msg.confidence),
                "progress": float(msg.progress),
                "elapsed_sec": float(msg.elapsed_sec),
                "remaining_sec": float(msg.remaining_sec),
                "error_code": str(msg.error_code),
                "rejection_reason": str(msg.rejection_reason),
            },
            msg,
        )

    def _on_input_source_status(
        self,
        topic: str,
        msg: InputSourceStatus,
    ) -> None:
        """Monitor the shared speech admission boundary without dispatching."""

        now = time.monotonic()
        sample = json.dumps(
            {
                "source_id": str(msg.source_id),
                "modality": str(msg.modality),
                "state": str(msg.state),
                "healthy": bool(msg.healthy),
                "received_count": int(msg.received_count),
                "accepted_count": int(msg.accepted_count),
                "rejected_count": int(msg.rejected_count),
                "detail": str(msg.detail),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self._lock:
            stats = self._input_stats[topic]
            stats.arrivals.append(now)
            stats.sizes.append((now, len(sample.encode("utf-8"))))
            stats.last_received_monotonic = now
            stats.source_delay_sec = max(0.0, float(msg.age_sec))
            stats.last_sample = sample[:240]
            stats.message_count += 1

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

    def _on_bed_robot_arm_status(
        self,
        msg: BedRobotArmStateArray,
        source: str = "external",
    ) -> None:
        source = str(source).strip().lower()
        if source not in {"external", "virtual"}:
            return
        try:
            arms = validate_bed_robot_arm_status(msg.procedure_type, msg.arms)
        except ValueError as exc:
            with self._lock:
                records = getattr(self, "_bed_robot_arm_status_sources", None)
                if isinstance(records, dict):
                    records[source] = {
                        "received": False,
                        "summary": {"error": str(exc)},
                    }
                if source == getattr(self, "_robot_endpoint_source", "external"):
                    self._bed_robot_arm_status_received = False
                    self._bed_robot_arm_status_summary = {"error": str(exc)}
            self.get_logger().warning(f"ignored invalid bed robot arm status: {exc}")
            return
        source_stamp_sec = float(msg.stamp.sec) + float(msg.stamp.nanosec) / 1e9
        revision = int(msg.revision)
        records = getattr(self, "_bed_robot_arm_status_sources", None)
        current = records.get(source, {}) if isinstance(records, dict) else {}
        current_stamp = float(
            current.get(
                "source_stamp_sec",
                getattr(self, "_bed_robot_arm_status_source_stamp_sec", 0.0),
            )
        )
        current_revision = current.get(
            "revision",
            getattr(self, "_bed_robot_arm_status_revision", None),
        )
        ordered = bool(
            source_stamp_sec > 0.0
            and (
                source_stamp_sec > current_stamp
                or (
                    source_stamp_sec == current_stamp
                    and current_revision is not None
                    and revision > current_revision
                )
            )
        )
        if not ordered:
            self.get_logger().warning(
                "ignored stale bed robot arm status "
                f"stamp={source_stamp_sec:.9f} revision={revision}"
            )
            return
        received_monotonic = time.monotonic()
        summary = {
            "revision": revision,
            "procedure_type": str(msg.procedure_type),
            "arm_count": len(arms),
            "arms": arms,
        }
        with self._lock:
            if isinstance(records, dict):
                records[source] = {
                    "received": True,
                    "received_monotonic": received_monotonic,
                    "source_stamp_sec": source_stamp_sec,
                    "revision": revision,
                    "summary": summary,
                }
            if source == getattr(self, "_robot_endpoint_source", "external"):
                self._bed_robot_arm_status_received = True
                self._bed_robot_arm_status_received_monotonic = received_monotonic
                self._bed_robot_arm_status_source_stamp_sec = source_stamp_sec
                self._bed_robot_arm_status_revision = revision
                self._bed_robot_arm_status_summary = summary

    def _bed_robot_arm_source_ready(
        self,
        source: str,
    ) -> tuple[bool, float | None]:
        records = getattr(self, "_bed_robot_arm_status_sources", None)
        if not isinstance(records, dict):
            return self._bed_robot_arm_status_ready()
        record = records.get(str(source), {})
        if not record.get("received"):
            return False, None
        age_sec = time.monotonic() - float(record["received_monotonic"])
        return age_sec <= self._bed_robot_arm_status_max_age_sec, age_sec

    def _bed_robot_arm_status_ready(self) -> tuple[bool, float | None]:
        records = getattr(self, "_bed_robot_arm_status_sources", None)
        if isinstance(records, dict):
            return self._bed_robot_arm_source_ready(
                getattr(self, "_robot_endpoint_source", "external")
            )
        if not self._bed_robot_arm_status_received:
            return False, None
        age_sec = time.monotonic() - self._bed_robot_arm_status_received_monotonic
        return age_sec <= self._bed_robot_arm_status_max_age_sec, age_sec

    def _detected_planner_nodes(self) -> list[str]:
        expected = {str(value) for value in self._config.get("blocked_nodes", [])}
        try:
            discovered = {name for name, _namespace in self.get_node_names_and_namespaces()}
        except Exception:
            return []
        return sorted(expected & discovered)

    def _operational_runtime_status(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            received = self._operational_state_received
            received_at = self._operational_state_received_monotonic
            running = self._operational_running
            execution_state = self._operational_execution_state
            active_robot_task_id = self._operational_active_robot_task_id
            robot_state = self._operational_robot_state
            cleaner_busy = self._operational_cleaner_busy
        age_sec = max(0.0, now - received_at) if received and received_at else None
        try:
            publisher_infos = self.get_publishers_info_by_topic(
                "/simulation/state"
            )
        except Exception:
            publisher_infos = []
        publishers = sorted(
            _node_identity(str(info.node_namespace), str(info.node_name))
            for info in publisher_infos
        )
        publisher_trusted = operational_state_publisher_trusted(
            publishers,
            self._operational_state_expected_publisher,
        )
        stopped = operational_runtime_stopped(
            received=received,
            running=running,
            execution_state=execution_state,
            active_robot_task_id=active_robot_task_id,
            robot_state=robot_state,
            cleaner_busy=cleaner_busy,
            publisher_trusted=publisher_trusted,
            age_sec=age_sec,
            max_age_sec=self._operational_state_max_age_sec,
        )
        intervention_block_reason = operational_runtime_intervention_block_reason(
            received=received,
            running=running,
            execution_state=execution_state,
            active_robot_task_id=active_robot_task_id,
            robot_state=robot_state,
            cleaner_busy=cleaner_busy,
            publisher_trusted=publisher_trusted,
            age_sec=age_sec,
            max_age_sec=self._operational_state_max_age_sec,
        )
        control_window_block_reason = operational_runtime_intervention_block_reason(
            received=received,
            running=running,
            execution_state=execution_state,
            active_robot_task_id=active_robot_task_id,
            robot_state=robot_state,
            cleaner_busy=cleaner_busy,
            publisher_trusted=publisher_trusted,
            age_sec=age_sec,
            max_age_sec=self._operational_state_max_age_sec,
            require_idle_resources=False,
        )
        return {
            "received": received,
            "running": running,
            "execution_state": execution_state,
            "active_robot_task_id": active_robot_task_id,
            "robot_state": robot_state,
            "cleaner_busy": cleaner_busy,
            "publishers": publishers,
            "expected_publisher": self._operational_state_expected_publisher,
            "publisher_trusted": publisher_trusted,
            "age_sec": age_sec,
            "fresh": bool(
                received
                and age_sec is not None
                and age_sec <= self._operational_state_max_age_sec
            ),
            "stopped": stopped,
            "intervention_allowed": not intervention_block_reason,
            "intervention_block_reason": intervention_block_reason,
            "control_window_open": not control_window_block_reason,
            "control_window_block_reason": control_window_block_reason,
        }

    def _debug_asr_owned_by_operational_runtime(self) -> bool:
        """Return whether the live runtime must retain USB microphone ownership.

        The runtime-network lock follows the live profile's DDS settings; it is
        not, by itself, proof that the operational ASR process is capturing.
        Debug may use the shared microphone only while the same authoritative
        intervention gate used by every other manual write is open: a fresh,
        trusted paused or fully stopped state. Resource mirrors remain
        diagnostics, not a second admission gate.
        The capture lock still prevents two microphone owners. Missing or
        unavailable state therefore remains fail-closed.
        """

        if not self._network_locked_to_runtime:
            return False
        try:
            operational = self._operational_runtime_status()
        except Exception:
            return True
        return operational.get("intervention_allowed") is not True

    def _blocked_nodes(self) -> list[str]:
        detected = self._detected_planner_nodes()
        if not self._network_locked_to_runtime:
            return detected
        operational = self._operational_runtime_status()
        if operational["intervention_allowed"]:
            return []
        return ["operational_runtime_intervention_gate"]

    def _debug_write_block_reason(
        self,
        *,
        physical: bool,
        operation: str = "",
    ) -> str:
        """Check the one Debug write boundary immediately before transport.

        Every graph write requires fresh authoritative paused/stopped evidence
        while integrated with the operational runtime.  Only an endpoint marked
        ``physical`` in the typed-dispatch policy additionally needs a manual
        arm, fault lock and per-scope check.  This keeps harmless admitted-text
        injection from acquiring robot-control authority, while never allowing
        a physical Action/Service to bypass the controller-facing boundary.
        """

        if self._network_locked_to_runtime:
            operational = self._operational_runtime_status()
            with self._lock:
                if not operational["intervention_allowed"]:
                    return str(operational["intervention_block_reason"])
                if not physical:
                    return ""
                if self._fault_locked:
                    return "manual control is fault locked"
                if not self._armed:
                    return "manual control is not armed"
                scope = getattr(self, "_manual_control_scope", "all")
                if scope == "tool_handover" and operation != "tool_handover":
                    return "manual control is limited to tool handover"
                return ""

        blocked = self._blocked_nodes()
        with self._lock:
            if blocked:
                return "full Taskplanner nodes are active: " + ", ".join(blocked)
            if not physical:
                return ""
            scope = getattr(self, "_manual_control_scope", "all")
            reason = manual_write_block_reason(
                armed=self._armed,
                fault_locked=self._fault_locked,
                blocked_nodes=(),
                planner_coexistence_allowed=False,
                acknowledged_blocked_nodes=(),
            )
            if reason:
                return reason
            if scope == "tool_handover" and operation != "tool_handover":
                return "manual control is limited to tool handover"
            return ""

    def _manual_write_block_reason(self, operation: str = "") -> str:
        """Compatibility wrapper for legacy Debug physical-write callers."""

        return IntegrationDebugNode._debug_write_block_reason(
            self,
            physical=True,
            operation=operation,
        )

    @staticmethod
    def _active_command_operational_block_reason(
        operational: dict[str, Any], command_id: str
    ) -> str:
        """Keep Debug writes inside the paused/stopped lifecycle window.

        The controller owns the physical command state.  Do not revoke a
        Debug command merely because its own motion is reflected as ``moving``
        or because an unrelated mirrored status field is temporarily unknown.
        """

        del command_id
        if not operational.get("control_window_open"):
            return str(
                operational.get("control_window_block_reason")
                or "operational control window is closed"
            )
        return ""

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

    def _robot_source_snapshot(self) -> dict[str, Any]:
        external_bed_ready, _ = self._bed_robot_arm_source_ready("external")
        virtual_bed_ready, _ = self._bed_robot_arm_source_ready("virtual")
        selected = self._robot_endpoint_source
        external_tool_ready = IntegrationDebugNode._client_is_ready(
            self._external_tool_client, "server_is_ready"
        )
        virtual_tool_ready = IntegrationDebugNode._client_is_ready(
            self._virtual_tool_client, "server_is_ready"
        )
        external_retraction_ready = IntegrationDebugNode._client_is_ready(
            self._external_retraction_client, "service_is_ready"
        )
        virtual_retraction_ready = IntegrationDebugNode._client_is_ready(
            self._virtual_retraction_client, "service_is_ready"
        )
        return {
            "enabled": self._virtual_robot_enabled,
            "selected_source": selected,
            "profile_id": VIRTUAL_ROBOT_PROFILE_ID,
            "tool_handover_ready": (
                virtual_tool_ready if selected == "virtual" else external_tool_ready
            ),
            "retraction_service_ready": (
                virtual_retraction_ready
                if selected == "virtual"
                else external_retraction_ready
            ),
            "bed_status_ready": (
                virtual_bed_ready if selected == "virtual" else external_bed_ready
            ),
            "external_tool_handover_ready": external_tool_ready,
            "virtual_tool_handover_ready": virtual_tool_ready,
            "external_retraction_service_ready": external_retraction_ready,
            "virtual_retraction_service_ready": virtual_retraction_ready,
            "external_bed_status_ready": external_bed_ready,
            "virtual_bed_status_ready": virtual_bed_ready,
            "external": {
                "tool_handover": IntegrationDebugNode._dispatch_endpoint_name(self,
                    "tool_handover", "external"
                ),
                "retraction_service": IntegrationDebugNode._dispatch_endpoint_name(self,
                    "retraction_service", "external"
                ),
                "bed_status": BED_ROBOT_STATUS_DEFAULT_TOPIC,
            },
            "virtual": {
                "tool_handover": IntegrationDebugNode._dispatch_endpoint_name(self,
                    "tool_handover", "virtual"
                ),
                "retraction_service": IntegrationDebugNode._dispatch_endpoint_name(self,
                    "retraction_service", "virtual"
                ),
                "bed_status": self._virtual_bed_robot_status_topic,
            },
        }

    def _configure_robot_endpoint_source(
        self,
        payload: dict[str, Any],
    ) -> tuple[bool, str, str, dict[str, Any]]:
        source = str(payload.get("source", "")).strip().lower()
        if source not in {"external", "virtual"}:
            raise ValueError("source must be external or virtual")
        with self._lock:
            if self._active_command_id:
                return (
                    False,
                    self._active_command_id,
                    "wait for the active command before switching robot source",
                    self._robot_source_snapshot(),
                )
            if self._armed:
                return (
                    False,
                    "",
                    "disarm manual control before switching robot source",
                    self._robot_source_snapshot(),
                )
            if source == "virtual" and not self._virtual_robot_enabled:
                return (
                    False,
                    "",
                    "virtual robot emulator is disabled by launch configuration",
                    self._robot_source_snapshot(),
                )
            previous = self._robot_endpoint_source
            if previous == source:
                return (
                    True,
                    "",
                    f"robot endpoint source is already {source}",
                    self._robot_source_snapshot(),
                )
            self._robot_endpoint_source = source
            self._tool_client = (
                self._virtual_tool_client
                if source == "virtual"
                else self._external_tool_client
            )
            self._retraction_client = (
                self._virtual_retraction_client
                if source == "virtual"
                else self._external_retraction_client
            )
            self._retraction_service_name = (
                IntegrationDebugNode._dispatch_endpoint_name(
                    self, "retraction_service", source
                )
            )
            record = self._bed_robot_arm_status_sources.get(source, {})
            self._bed_robot_arm_status_received = bool(record.get("received"))
            self._bed_robot_arm_status_received_monotonic = float(
                record.get("received_monotonic", 0.0)
            )
            self._bed_robot_arm_status_source_stamp_sec = float(
                record.get("source_stamp_sec", 0.0)
            )
            self._bed_robot_arm_status_revision = record.get("revision")
            self._bed_robot_arm_status_summary = dict(record.get("summary", {}))
            # Admission state belongs to one selected endpoint.  Never carry it
            # between a physical controller and the emulator.
            self._retraction_state = RetractionState.IDLE
            self._last_retraction_rejection_reason = ""
        self._record(
            "robot_endpoint_source_changed",
            {"previous_source": previous, "selected_source": source},
        )
        return (
            True,
            "",
            f"robot endpoint source changed to {source}; retraction state reset to idle",
            self._robot_source_snapshot(),
        )

    def _handle_command(
        self,
        request: IntegrationDebugCommand.Request,
        response: IntegrationDebugCommand.Response,
    ) -> IntegrationDebugCommand.Response:
        operation = str(request.operation).strip().lower()
        result: dict[str, Any] = {}
        try:
            payload = decode_payload(request.payload_json)
            required_capability = capability_for_operation(operation)
            if operation != "heartbeat" and not IntegrationDebugNode._capability_enabled(
                self, required_capability
            ):
                accepted, command_id, message = (
                    False,
                    "",
                    IntegrationDebugNode._capability_unavailable_message(
                        self, required_capability
                    ),
                )
            elif operation == "apply_network_settings":
                accepted, command_id, message, result = self._apply_network_settings(
                    payload
                )
            elif operation == "ping_host":
                accepted, command_id, message, result = self._ping_host(payload)
            elif operation == "configure_robot_endpoint_source":
                accepted, command_id, message, result = (
                    self._configure_robot_endpoint_source(payload)
                )
            elif operation.startswith("asr_"):
                accepted, command_id, message, result = self._handle_asr_command(
                    operation, payload
                )
            elif operation.startswith("record_"):
                accepted, command_id, message, result = (
                    self._handle_surgery_record_command(operation, payload)
                )
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
            safe_result = result
            if operation == "record_submit":
                # Never retain the transient X-API-Key or request body in the
                # generic UI command audit trail.
                safe_result = {
                    "request_id": str(result.get("request_id", ""))
                }
            self._record(
                "ui_command",
                {
                    "operation": operation,
                    "accepted": accepted,
                    "command_id": command_id,
                    "message": message,
                    "result": safe_result,
                },
            )
        if operation in {"arm", "disarm", "reset_fault", "force_retraction_idle"}:
            # A session transition must be visible before the next periodic
            # status tick so the browser can start or stop its heartbeat from
            # the authoritative server state without an avoidable lockout.
            self._publish_status()
        return response

    def _ensure_asr_publisher(self) -> None:
        with self._auxiliary_lock:
            if self._asr_sentence_pub is not None:
                return
            self._asr_sentence_pub = self.create_publisher(
                String,
                self._asr_topic,
                QoSProfile(
                    history=QoSHistoryPolicy.KEEP_LAST,
                    depth=10,
                    reliability=QoSReliabilityPolicy.RELIABLE,
                    durability=QoSDurabilityPolicy.VOLATILE,
                ),
            )

    def _destroy_asr_publisher(self) -> None:
        with self._auxiliary_lock:
            publisher = self._asr_sentence_pub
            self._asr_sentence_pub = None
            if publisher is not None:
                self.destroy_publisher(publisher)

    def _sync_asr_publisher(self, connected: bool) -> None:
        """Expose Debug ASR readiness only for a requested, connected capture."""

        with self._auxiliary_lock:
            should_publish = bool(connected and self._asr_capture_requested)
            if should_publish:
                self._ensure_asr_publisher()
            else:
                self._destroy_asr_publisher()

    def _ensure_manual_sentence_publisher(self) -> Any:
        with self._auxiliary_lock:
            if self._manual_sentence_pub is None:
                self._manual_sentence_pub = self.create_publisher(
                    String,
                    self._asr_topic,
                    QoSProfile(
                        history=QoSHistoryPolicy.KEEP_LAST,
                        depth=1,
                        reliability=QoSReliabilityPolicy.RELIABLE,
                        durability=QoSDurabilityPolicy.VOLATILE,
                    ),
                )
            return self._manual_sentence_pub

    def _destroy_manual_sentence_publisher(self) -> None:
        with self._auxiliary_lock:
            publisher = self._manual_sentence_pub
            self._manual_sentence_pub = None
            if publisher is not None:
                self.destroy_publisher(publisher)

    def _ensure_typed_topic_publisher(self, request: ResolvedDispatch) -> Any:
        """Create one publisher for an explicitly configured generic Topic."""

        key = (request.ros_type, request.ros_endpoint)
        with self._auxiliary_lock:
            publisher = self._typed_topic_publishers.get(key)
            if publisher is None:
                publisher = self.create_publisher(
                    resolve_interface("topic", request.ros_type),
                    request.ros_endpoint,
                    QoSProfile(
                        history=QoSHistoryPolicy.KEEP_LAST,
                        depth=1,
                        reliability=QoSReliabilityPolicy.RELIABLE,
                        durability=QoSDurabilityPolicy.VOLATILE,
                    ),
                )
                self._typed_topic_publishers[key] = publisher
            return publisher

    def _typed_action_client_for(self, request: ResolvedDispatch) -> Any | None:
        """Return one catalog-owned Action client without a handwritten codec.

        The two historical clinical clients remain eagerly constructed because
        their operator UI already exposes endpoint-source readiness. New
        catalog entries lazily resolve their installed ROS action type here.
        """

        if not self._capability_enabled("control"):
            return None
        if request.endpoint.name == "tool_handover":
            return getattr(self, "_tool_client", None)
        key = (request.ros_type, request.ros_endpoint)
        with self._lock:
            clients = getattr(self, "_typed_action_clients", None)
            if clients is None:
                clients = {}
                self._typed_action_clients = clients
            client = clients.get(key)
            if client is None:
                client = ActionClient(
                    self,
                    resolve_interface("action", request.ros_type),
                    request.ros_endpoint,
                    callback_group=self._callback_group,
                )
                clients[key] = client
            return client

    def _typed_service_client_for(self, request: ResolvedDispatch) -> Any | None:
        """Return one catalog-owned Service client resolved from its ROS type."""

        if not self._capability_enabled("control"):
            return None
        if request.endpoint.name == "retraction_service":
            return getattr(self, "_retraction_client", None)
        key = (request.ros_type, request.ros_endpoint)
        with self._lock:
            clients = getattr(self, "_typed_service_clients", None)
            if clients is None:
                clients = {}
                self._typed_service_clients = clients
            client = clients.get(key)
            if client is None:
                client = self.create_client(
                    resolve_interface("service", request.ros_type),
                    request.ros_endpoint,
                    callback_group=self._callback_group,
                )
                clients[key] = client
            return client

    def _configured_dispatch_request(self, name: str) -> ResolvedDispatch | None:
        dispatcher = getattr(self, "_typed_dispatcher", None)
        if dispatcher is None:
            return None
        try:
            endpoint = dispatcher.endpoint(name)
            source = getattr(self, "_robot_endpoint_source", "external")
            return ResolvedDispatch(
                endpoint=endpoint,
                ros_endpoint=endpoint.endpoint_for(source),
                payload={},
                timeout_sec=endpoint.timeout_sec,
            )
        except ValueError:
            return None

    def _release_typed_topic_publishers(self) -> None:
        with self._auxiliary_lock:
            publishers = list(self._typed_topic_publishers.values())
            self._typed_topic_publishers.clear()
        for publisher in publishers:
            self.destroy_publisher(publisher)

    def _ensure_output_publisher(self, topic: str) -> Any:
        with self._lock:
            publisher = self._output_publishers.get(topic)
            if publisher is not None:
                return publisher
            state = self._output_states[topic]
            message_class = PUBLIC_OUTPUT_TYPES[state.message_type]
            publisher = self.create_publisher(
                message_class,
                topic,
                self._output_qos_profiles[topic],
            )
            self._output_publishers[topic] = publisher
            return publisher

    def _destroy_output_publisher(self, topic: str) -> None:
        with self._lock:
            publisher = self._output_publishers.pop(topic, None)
        if publisher is not None:
            self.destroy_publisher(publisher)

    def _release_manual_publishers(self) -> None:
        """Revoke every manual write path, including microphone capture."""

        with self._lock:
            for state in self._output_states.values():
                state.enabled = False
            publishers = list(self._output_publishers.values())
            self._output_publishers.clear()
        for publisher in publishers:
            self.destroy_publisher(publisher)
        self._destroy_manual_sentence_publisher()
        release_typed_publishers = getattr(
            self, "_release_typed_topic_publishers", None
        )
        if callable(release_typed_publishers):
            release_typed_publishers()
        with self._auxiliary_lock:
            self._asr_capture_requested = False
            self._destroy_asr_publisher()
            # Removing only the ROS publisher would leave privacy-sensitive
            # microphone audio streaming to the external ASR server invisibly.
            # The runtime stop is idempotent, so every authority-revocation path
            # may safely enforce it here.
            if self._asr is not None:
                self._asr.stop_async()

    def _asr_status_snapshot(self) -> dict[str, Any]:
        """Return Debug ASR status with its reviewed endpoint identity."""

        if self._asr is None:
            return IntegrationDebugNode._disabled_asr_snapshot()
        with self._auxiliary_lock:
            snapshot = dict(self._asr.snapshot())
            snapshot["endpoint_id"] = self._asr_endpoint
            return snapshot

    def _surgery_record_status_snapshot(self) -> dict[str, Any]:
        if self._surgery_record is None:
            return IntegrationDebugNode._disabled_surgery_record_snapshot()
        return self._surgery_record.snapshot()

    def _operational_asr_status_snapshot(self) -> dict[str, Any]:
        with self._lock:
            snapshot = dict(self._operational_asr_status)
            received_at = self._operational_asr_status_received_monotonic
        age_sec = max(0.0, time.monotonic() - received_at) if received_at else None
        snapshot["status_received"] = bool(received_at)
        snapshot["status_age_sec"] = round(age_sec, 3) if age_sec is not None else None
        snapshot["status_fresh"] = bool(age_sec is not None and age_sec <= 5.0)
        return snapshot

    def _proxy_operational_asr_control(
        self, operation: str
    ) -> tuple[bool, str, str, dict[str, Any]]:
        if self._operational_asr_client is None:
            return (
                False,
                "",
                self._capability_unavailable_message("asr"),
                self._operational_asr_status_snapshot(),
            )
        snapshot = self._operational_asr_status_snapshot()
        if snapshot.get("status_fresh") is not True:
            return False, "", "operational ASR status is unavailable or stale", snapshot
        if not self._operational_asr_client.service_is_ready():
            return False, "", "operational ASR control Service is not ready", snapshot
        request = AsrControl.Request()
        request.operation = operation
        request.device_id = -1
        request.server_url = ""
        request.route_policy = ""
        future = self._operational_asr_client.call_async(request)
        completed = threading.Event()
        future.add_done_callback(lambda _future: completed.set())
        if not completed.wait(timeout=5.0):
            future.cancel()
            return False, "", "operational ASR recording control timed out", snapshot
        try:
            response = future.result()
        except Exception as exc:
            return False, "", f"operational ASR recording control failed: {exc}", snapshot
        raw_result = str(getattr(response, "result_json", "") or "")
        if raw_result and len(raw_result) <= 1_000_000:
            try:
                envelope = json.loads(raw_result)
                if (
                    isinstance(envelope, dict)
                    and envelope.get("schema") == OPERATIONAL_ASR_STATUS_SCHEMA
                    and isinstance(envelope.get("asr"), dict)
                ):
                    with self._lock:
                        self._operational_asr_status = dict(envelope["asr"])
                        self._operational_asr_status_received_monotonic = time.monotonic()
            except (TypeError, ValueError):
                pass
        return (
            bool(response.accepted),
            "",
            str(response.message or "operational ASR recording control completed"),
            self._operational_asr_status_snapshot(),
        )

    def _handle_asr_command(
        self, operation: str, payload: dict[str, Any]
    ) -> tuple[bool, str, str, dict[str, Any]]:
        # These two operations only proxy the operational ASR recorder. They
        # do not need a Debug-owned PipeWire session and must remain usable
        # from the isolated observer/control topology.
        if operation == "asr_recording_start":
            return self._proxy_operational_asr_control("start_recording")
        if operation == "asr_recording_stop":
            return self._proxy_operational_asr_control("stop_recording")
        if self._asr is None:
            return (
                False,
                "",
                self._capability_unavailable_message("asr"),
                self._asr_status_snapshot(),
            )
        if operation == "asr_refresh_devices":
            devices = self._asr.refresh_devices()
            return True, "", f"found {len(devices)} microphone input device(s)", {
                "devices": devices
            }
        if operation == "asr_start":
            if self._debug_asr_owned_by_operational_runtime():
                return (
                    False,
                    "",
                    "integrated runtime owns USB ASR; use the live operating-screen ASR controls",
                    {},
                )
            blocked_reason = self._manual_write_block_reason()
            if blocked_reason:
                if blocked_reason == "manual control is not armed":
                    blocked_reason = "arm manual control before starting the microphone"
                return False, "", blocked_reason, {}
            requested_url = str(payload.get("server_url") or "").strip()
            if requested_url:
                return (
                    False,
                    "",
                    "server_url override is not allowed; select the cloud or lan ASR route",
                    self._asr_status_snapshot(),
                )
            requested_endpoint = payload.get("endpoint_id")
            if requested_endpoint is None or not str(requested_endpoint).strip():
                requested_endpoint = self._asr_endpoint
            try:
                endpoint, server_url = resolve_puzzle_asr_endpoint(
                    requested_endpoint,
                    cloud_url=self._asr_cloud_url,
                    lan_url=self._asr_lan_url,
                )
            except ValueError as exc:
                return False, "", str(exc), self._asr_status_snapshot()
            with self._auxiliary_lock:
                # Consume a previous session's terminal event before creating
                # readiness for this new session.
                self._drain_auxiliary_events()
                state = str(self._asr.snapshot().get("state", ""))
                if state not in {"STOPPED", "ERROR"}:
                    raise ValueError("ASR microphone session is already active")
                self._asr_capture_requested = False
                self._destroy_asr_publisher()
                try:
                    self._asr.start(
                        device_id=payload.get("device_id"),
                        server_url=server_url,
                    )
                except Exception:
                    self._asr_capture_requested = False
                    self._destroy_asr_publisher()
                    raise
                self._asr_endpoint = endpoint
                self._asr_server_url = server_url
                self._asr_capture_requested = True
                self._sync_asr_publisher(
                    bool(self._asr.snapshot().get("connected", False))
                )
            return (
                True,
                "",
                "USB microphone ASR session started",
                self._asr_status_snapshot(),
            )
        if operation == "asr_stop":
            with self._auxiliary_lock:
                self._asr_capture_requested = False
                self._destroy_asr_publisher()
                self._asr.stop_async()
                snapshot = self._asr_status_snapshot()
            return True, "", "USB microphone ASR stop requested", snapshot
        return False, "", "unknown ASR debug operation", {}

    def _handle_surgery_record_command(
        self, operation: str, payload: dict[str, Any]
    ) -> tuple[bool, str, str, dict[str, Any]]:
        if self._surgery_record is None:
            return (
                False,
                "",
                self._capability_unavailable_message("record"),
                self._surgery_record_status_snapshot(),
            )
        if operation == "record_refresh_cases":
            examples = self._surgery_record.refresh_cases()
            return True, "", f"found {len(examples)} surgery-record example(s)", {
                "examples": examples
            }
        if operation == "record_submit":
            request_id = self._surgery_record.submit_async(payload)
            return (
                True,
                request_id,
                "surgery-record API request submitted",
                {"request_id": request_id},
            )
        if operation == "record_clear_history":
            self._surgery_record.clear_history()
            return True, "", "surgery-record test history cleared", {}
        return False, "", "unknown surgery-record debug operation", {}

    def _drain_auxiliary_events(self) -> None:
        with self._auxiliary_lock:
            asr = self._asr
            for event in ([] if asr is None else asr.drain_events()):
                event_type = str(event.get("type", "asr_event"))
                if event_type.startswith("asr_"):
                    event = {**event, "endpoint_id": self._asr_endpoint}
                if event_type == "asr_connection":
                    self._sync_asr_publisher(bool(event.get("connected", False)))
                elif event_type == "asr_final":
                    text = str(event.get("text", "")).strip()
                    publisher = self._asr_sentence_pub
                    blocked_reason = self._manual_write_block_reason()
                    if (
                        text
                        and self._asr_capture_requested
                        and publisher is not None
                        and not blocked_reason
                    ):
                        message = String()
                        message.data = text
                        publisher.publish(message)
                    elif text and blocked_reason:
                        event = {
                            **event,
                            "publish_suppressed": True,
                            "publish_suppressed_reason": blocked_reason,
                        }
                elif event_type == "asr_stopped":
                    self._asr_capture_requested = False
                    self._destroy_asr_publisher()
                # Partial hypotheses are high-volume transient UI state. They stay
                # in the bounded ASR snapshot and are not duplicated in JSONL.
                if event_type != "asr_partial":
                    self._record(event_type, event)
            self._sync_asr_publisher(
                bool(asr is not None and asr.snapshot().get("connected", False))
            )
            record = self._surgery_record
            for event in ([] if record is None else record.drain_events()):
                self._record(str(event.get("type", "record_event")), event)

    def _execute_command(
        self, operation: str, payload: dict[str, Any]
    ) -> tuple[bool, str, str]:
        now = time.monotonic()
        if operation == "heartbeat":
            with self._lock:
                self._last_heartbeat_monotonic = now
            return True, "", "heartbeat accepted"
        if operation == "arm":
            scope = str(payload.get("manual_control_scope", "all")).strip().lower()
            if scope not in {"all", "tool_handover"}:
                return False, "", "manual_control_scope must be all or tool_handover"
            with self._lock:
                if self._fault_locked:
                    return False, "", "reset the fault lock before arming"
            operational_state = ""
            if self._network_locked_to_runtime:
                operational = self._operational_runtime_status()
                if not operational["intervention_allowed"]:
                    return False, "", str(operational["intervention_block_reason"])
                operational_state = str(operational["execution_state"])
            else:
                blocked = self._blocked_nodes()
                if blocked:
                    return (
                        False,
                        "",
                        "full Taskplanner nodes are active: " + ", ".join(blocked),
                    )
            with self._lock:
                if self._fault_locked:
                    return False, "", "reset the fault lock before arming"
                self._armed = True
                self._manual_control_scope = scope
                self._acknowledged_blocked_nodes.clear()
                self._last_heartbeat_monotonic = now
                self._last_error = ""
            if operational_state:
                return (
                    True,
                    "",
                    f"manual control armed while operational scenario is {operational_state}",
                )
            return True, "", "manual control armed"
        if operation == "disarm":
            with self._lock:
                self._disarm_locked()
            with self._auxiliary_lock:
                if self._asr is not None:
                    self._asr.stop_async()
            self._release_manual_publishers()
            if self._active_command_id:
                accepted, command_id, message = self._request_cancel()
                if accepted:
                    return True, command_id, "disarmed; active Action cancel requested"
                if self._active_route == "retraction_service":
                    return (
                        True,
                        command_id,
                        "disarmed; retraction Service response remains pending",
                    )
                return False, command_id, message
            return True, "", "manual control disarmed"
        if operation == "reset_fault":
            with self._lock:
                if self._active_command_id:
                    return False, "", "cannot reset while a command is active"
                self._fault_locked = False
                self._last_error = ""
                self._action_status = self._idle_action_status()
            return True, "", "fault lock reset"
        if operation in {"recover_action_client", "recover_command_client"}:
            return self._recover_command_client(payload)
        if operation == "force_retraction_idle":
            return self._force_retraction_idle(payload)
        if operation == "cancel_active":
            return self._request_cancel()
        if operation == "configure_output":
            if bool(payload.get("enabled", False)):
                blocked_reason = self._manual_write_block_reason()
                if blocked_reason:
                    return False, "", blocked_reason
            return self._configure_output(payload)
        if operation == "publish_once":
            topic = str(payload.get("topic", "")).strip()
            if topic not in self._output_states:
                return False, "", "unknown public output topic"
            blocked_reason = self._manual_write_block_reason()
            if blocked_reason:
                return False, "", blocked_reason
            conflicts = self._output_conflicts(topic)
            if conflicts:
                return False, "", "another publisher owns the topic: " + ", ".join(conflicts)
            self._publish_output(topic)
            return True, "", f"published one debug message on {topic}"
        if operation == "stop_outputs":
            self._release_output_publishers()
            return True, "", "all debug output publishers stopped"
        return self._dispatch_action(operation, payload, source="ui")

    def _force_retraction_idle(
        self, payload: dict[str, Any]
    ) -> tuple[bool, str, str]:
        """Reset only the Debug-side retraction admission state.

        This operation deliberately sends no robot Action or Service request.
        The explicit acknowledgement prevents the UI from presenting a local
        bookkeeping reset as proof that remote motion stopped.
        """

        if payload.get("remote_motion_stopped_confirmed") is not True:
            return (
                False,
                "",
                "confirm that remote retraction motion is stopped before forcing Debug idle",
            )

        with self._lock:
            if self._active_command_id:
                return (
                    False,
                    self._active_command_id,
                    "wait for the active command response before forcing Debug idle",
                )
            if not bool(self._action_status.get("terminal", True)):
                return (
                    False,
                    "",
                    "wait for the command lifecycle to become terminal before forcing Debug idle",
                )

            previous_state = self._retraction_state
            was_armed = self._armed
            self._disarm_locked()
            self._retraction_state = RetractionState.IDLE
            self._last_retraction_rejection_reason = ""
            self._action_status = self._idle_action_status()
            self._last_error = ""

        self._release_manual_publishers()
        self._record(
            "retraction_state_forced_idle",
            {
                "previous_state": previous_state.value,
                "state": RetractionState.IDLE.value,
                "manual_control_was_armed": was_armed,
                "remote_motion_stopped_confirmed": True,
                "robot_command_sent": False,
            },
        )
        return (
            True,
            "",
            "Debug retraction state forced to idle; manual control disarmed; "
            "no robot command was sent",
        )

    def _recover_command_client(
        self, payload: dict[str, Any]
    ) -> tuple[bool, str, str]:
        with self._lock:
            command_id = self._active_command_id
            if not command_id:
                return False, "", "there is no active command client state to recover"
            if not bool(self._action_status.get("recovery_required")):
                return False, command_id, "the active command does not require recovery"
            validate_action_recovery_acknowledgement(payload, command_id)
            route = self._active_route
            previous_state = str(self._action_status.get("state", ""))
            previous_reason = str(self._action_status.get("reason_code", ""))
            started = float(self._action_status.get("started_monotonic", 0.0))
            elapsed_sec = max(0.0, time.monotonic() - started) if started else 0.0
            retraction_state_reset = route == "retraction_service"
            self._disarm_locked()
            self._active_route = ""
            self._active_command_id = ""
            self._active_goal_handle = None
            self._fault_locked = False
            self._last_error = ""
            if retraction_state_reset:
                # A lost Service response makes the local admission state
                # unknown.  Recovery is permitted only after the operator
                # explicitly confirms the remote state/motion check above;
                # use that confirmation to establish a fresh Debug baseline.
                # This is local bookkeeping, not a claim that the arm moved.
                self._retraction_state = RetractionState.IDLE
                self._last_retraction_rejection_reason = ""
            self._action_status = self._idle_action_status()
        self._release_manual_publishers()
        self._record(
            "command_client_recovered",
            {
                "route": route,
                "command_id": command_id,
                "previous_state": previous_state,
                "previous_reason_code": previous_reason,
                "elapsed_sec": round(elapsed_sec, 3),
                "remote_motion_stopped_confirmed": True,
                "retraction_state_reset": (
                    RetractionState.IDLE.value if retraction_state_reset else ""
                ),
            },
        )
        return (
            True,
            command_id,
            (
                "retraction Service client recovered to Debug idle; "
                "manual control remains disarmed"
                if retraction_state_reset
                else "command client state recovered; manual control remains disarmed"
            ),
        )

    def _apply_network_settings(
        self, payload: dict[str, Any]
    ) -> tuple[bool, str, str, dict[str, Any]]:
        from integration_debug.networking import (
            validate_network_settings,
            write_network_settings,
        )

        if self._network_locked_to_runtime:
            return (
                False,
                "",
                "DDS settings are locked to the active Taskplanner runtime",
                {
                    "domain_id": int(os.environ.get("ROS_DOMAIN_ID", "0") or 0),
                    "discovery_range": os.environ.get(
                        "ROS_AUTOMATIC_DISCOVERY_RANGE", ""
                    ).strip().upper(),
                    "locked_to_runtime": True,
                },
            )
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
        if self._asr_status_snapshot().get("state") not in {
            "STOPPED",
            "ERROR",
            "UNAVAILABLE",
        }:
            return False, "", "stop the USB ASR session before changing DDS settings", {}
        if self._surgery_record_status_snapshot().get("state") == "SUBMITTING":
            return False, "", "wait for the surgery-record request before changing DDS settings", {}

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
            self._disarm_locked()
        self._release_manual_publishers()
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
        from integration_debug.networking import ping_ipv4

        result = ping_ipv4(payload.get("target_ip"), count=3, timeout_sec=1.0)
        message = (
            "ping reply received"
            if result["reachable"]
            else "ping completed without an ICMP reply"
        )
        return True, "", message, result

    def _resolve_typed_dispatch_request(
        self,
        operation: str,
        payload: dict[str, Any],
    ) -> ResolvedDispatch:
        """Resolve the generic request or one temporary browser operation alias."""

        if operation == "retraction_adjustment":
            raise ValueError(
                "legacy direction, axis, and multi-retractor adjustment fields are "
                "unsupported; use dispatch with retraction_service target_side and distance_m"
            )
        if operation == "tool_change":
            raise ValueError(
                "legacy arm_id and target_tool_id fields are unsupported; use dispatch "
                "with retraction_service command change_tool"
            )
        endpoint_source = getattr(self, "_robot_endpoint_source", "external")
        dispatcher = getattr(self, "_typed_dispatcher", None)
        if dispatcher is not None:
            if operation == "dispatch":
                return dispatcher.resolve(payload, endpoint_source=endpoint_source)
            return dispatcher.resolve_legacy_operation(
                operation,
                payload,
                endpoint_source=endpoint_source,
            )

        # Unit-test harnesses instantiate only the narrow old client fields.
        # Production always has ``_typed_dispatcher`` from YAML above; this
        # fallback keeps those pure transport tests focused on their lifecycle.
        if operation == "tool_handover":
            endpoint = DispatchEndpoint(
                name="tool_handover",
                kind="action",
                ros_type="surgical_interop_msgs/action/ExecuteToolHandover",
                endpoints=(
                    ("external", TOOL_HANDOVER_DEFAULT_NAME),
                    (
                        "virtual",
                        str(
                            getattr(
                                self,
                                "_virtual_tool_handover_name",
                                VIRTUAL_TOOL_HANDOVER_DEFAULT_NAME,
                            )
                        ),
                    ),
                ),
                payload_codec="tool_handover",
                physical=True,
                single_flight=True,
                timeout_sec=300.0,
            )
        elif operation == "retraction_command":
            endpoint = DispatchEndpoint(
                name="retraction_service",
                kind="service",
                ros_type="surgical_interop_msgs/srv/ExecuteRetractionCommand",
                endpoints=(
                    (
                        "external",
                        str(
                            getattr(
                                self,
                                "_external_retraction_service_name",
                                RETRACTION_SERVICE_DEFAULT_NAME,
                            )
                        ),
                    ),
                    (
                        "virtual",
                        str(
                            getattr(
                                self,
                                "_virtual_retraction_service_name",
                                VIRTUAL_RETRACTION_SERVICE_DEFAULT_NAME,
                            )
                        ),
                    ),
                ),
                payload_codec="retraction_command",
                physical=True,
                single_flight=True,
                timeout_sec=120.0,
                response_semantics="admission",
            )
        else:
            raise ValueError("unsupported integration debug operation")
        return ResolvedDispatch(
            endpoint=endpoint,
            ros_endpoint=endpoint.endpoint_for(endpoint_source),
            payload=dict(payload),
            timeout_sec=endpoint.timeout_sec,
        )

    def _dispatch_block_reason(self, request: ResolvedDispatch) -> str:
        if request.physical:
            # Preserve the legacy method as the physical boundary so external
            # callers/tests cannot accidentally bypass the final recheck.
            return self._manual_write_block_reason(request.endpoint.name)
        return self._debug_write_block_reason(
            physical=False,
            operation=request.endpoint.name,
        )

    def _final_physical_dispatch_block_reason(
        self, request: ResolvedDispatch
    ) -> str:
        """Recheck local physical-write authority inside the transport lock.

        This is intentionally narrower than :meth:`_dispatch_block_reason`:
        the latter refreshes the authoritative operational state immediately
        before I/O, while this closes the tiny interval before the client call.
        It does not inspect scenario, model result, receipt, or controller state.
        """

        if not request.physical:
            return ""
        if self._fault_locked:
            return "manual control is fault locked"
        if not self._armed:
            return "manual control is not armed"
        scope = getattr(self, "_manual_control_scope", "all")
        if scope == "tool_handover" and request.endpoint.name != "tool_handover":
            return "manual control is limited to tool handover"
        return ""

    def _dispatch_action(
        self, operation: str, payload: dict[str, Any], *, source: str
    ) -> tuple[bool, str, str]:
        try:
            request = IntegrationDebugNode._resolve_typed_dispatch_request(
                self, operation, payload
            )
            mapped = validate_dispatch_payload(
                request.endpoint.payload_codec,
                request.payload,
                request.endpoint.payload_contract,
            )
        except ValueError as exc:
            return False, "", str(exc)

        blocked_reason = IntegrationDebugNode._dispatch_block_reason(self, request)
        if blocked_reason:
            return False, "", blocked_reason
        if request.kind == "topic":
            return IntegrationDebugNode._dispatch_topic(
                self, request, mapped, source=source
            )
        if request.kind == "action":
            return IntegrationDebugNode._dispatch_typed_action(
                self, request, mapped, source=source
            )
        if request.kind == "service":
            return IntegrationDebugNode._dispatch_typed_service(
                self, request, mapped, source=source
            )
        return False, "", "unsupported configured dispatch kind"

    def _dispatch_topic(
        self,
        request: ResolvedDispatch,
        mapped: dict[str, Any],
        *,
        source: str,
    ) -> tuple[bool, str, str]:
        try:
            publisher = self._ensure_typed_topic_publisher(request)
            message = build_wire_payload(
                kind="topic",
                ros_type=request.ros_type,
                payload=mapped,
                fixed_payload=request.endpoint.fixed_payload,
            )
        except ValueError as exc:
            return False, "", str(exc)
        publisher.publish(message)
        self._record(
            "typed_topic_dispatched",
            {
                "endpoint": request.ros_endpoint,
                "type": request.ros_type,
                "source": source,
            },
        )
        return True, "", f"published typed Topic on {request.ros_endpoint}"

    def _dispatch_typed_action(
        self,
        request: ResolvedDispatch,
        mapped: dict[str, Any],
        *,
        source: str,
    ) -> tuple[bool, str, str]:
        if (
            request.endpoint.payload_codec != "tool_handover"
            or request.ros_type
            != "surgical_interop_msgs/action/ExecuteToolHandover"
        ):
            return IntegrationDebugNode._dispatch_generic_typed_action(
                self, request, mapped, source=source
            )
        with self._lock:
            if request.single_flight and self._active_command_id:
                return False, self._active_command_id, "another command is active"
        client = getattr(self, "_tool_client", None)
        if not IntegrationDebugNode._client_is_ready(client, "server_is_ready"):
            return False, "", f"{request.ros_endpoint} Action server is unavailable"
        command_id = f"debug-{uuid4()}"
        goal = ExecuteToolHandover.Goal()
        goal.command_id = command_id
        goal.instrument_id = str(mapped["instrument_id"])
        goal.instrument_instance_id = str(mapped["instrument_instance_id"])
        goal.source_location = str(mapped["source_location"])
        goal.target_location = str(mapped["target_location"])
        submit_error: Exception | None = None
        future: Any | None = None
        with self._lock:
            if request.single_flight and self._active_command_id:
                return False, self._active_command_id, "another command is active"
            blocked_reason = IntegrationDebugNode._dispatch_block_reason(self, request)
            if blocked_reason:
                return False, "", blocked_reason
            blocked_reason = IntegrationDebugNode._final_physical_dispatch_block_reason(
                self, request
            )
            if blocked_reason:
                return False, "", blocked_reason
            self._start_action_locked(
                request.endpoint.name,
                command_id,
                source,
                timeout_sec=request.timeout_sec,
                endpoint=request.ros_endpoint,
                ros_type=request.ros_type,
            )
            try:
                future = client.send_goal_async(
                    goal,
                    feedback_callback=lambda feedback: self._on_action_feedback(
                        request.endpoint.name,
                        command_id,
                        feedback,
                    ),
                )
            except Exception as exc:
                submit_error = exc
                started = float(self._action_status.get("started_monotonic", 0.0))
                self._action_status.update(
                    {
                        "state": "failed",
                        "success": False,
                        "terminal": True,
                        "reason_code": f"action_submit_error:{type(exc).__name__}",
                        "elapsed_sec": max(0.0, time.monotonic() - started),
                        "last_update_monotonic": time.monotonic(),
                    }
                )
                self._active_route = ""
                self._active_command_id = ""
                self._active_goal_handle = None
        if submit_error is not None:
            reason_code = f"action_submit_error:{type(submit_error).__name__}"
            self._record(
                "typed_action_submit_failed",
                {
                    "route": request.endpoint.name,
                    "command_id": command_id,
                    "reason_code": reason_code,
                },
            )
            return False, "", f"failed to submit Action Goal ({reason_code})"
        self._record(
            "command_started",
            {
                "route": request.endpoint.name,
                "endpoint": request.ros_endpoint,
                "type": request.ros_type,
                "command_id": command_id,
                "source": source,
            },
        )
        assert future is not None
        future.add_done_callback(
            lambda result: self._on_goal_response(
                request.endpoint.name,
                command_id,
                result,
            )
        )
        return True, command_id, "tool handover Goal submitted"

    def _dispatch_generic_typed_action(
        self,
        request: ResolvedDispatch,
        mapped: dict[str, Any],
        *,
        source: str,
    ) -> tuple[bool, str, str]:
        """Dispatch a catalog-approved installed Action without a new codec."""

        with self._lock:
            if request.single_flight and self._active_command_id:
                return False, self._active_command_id, "another command is active"
        try:
            client = self._typed_action_client_for(request)
        except (TypeError, ValueError) as exc:
            return False, "", str(exc)
        if not IntegrationDebugNode._client_is_ready(client, "server_is_ready"):
            return False, "", f"{request.ros_endpoint} Action server is unavailable"
        command_id = f"debug-{uuid4()}"
        try:
            goal = build_wire_payload(
                kind="action",
                ros_type=request.ros_type,
                payload=mapped,
                fixed_payload=request.endpoint.fixed_payload,
                command_id=command_id if request.physical else "",
                command_id_field=(
                    request.endpoint.command_id_field if request.physical else ""
                ),
            )
        except ValueError as exc:
            return False, "", str(exc)

        submit_error: Exception | None = None
        future: Any | None = None
        with self._lock:
            if request.single_flight and self._active_command_id:
                return False, self._active_command_id, "another command is active"
            blocked_reason = IntegrationDebugNode._dispatch_block_reason(self, request)
            if blocked_reason:
                return False, "", blocked_reason
            blocked_reason = IntegrationDebugNode._final_physical_dispatch_block_reason(
                self, request
            )
            if blocked_reason:
                return False, "", blocked_reason
            self._start_action_locked(
                request.endpoint.name,
                command_id,
                source,
                response_semantics=request.endpoint.response_semantics,
                timeout_sec=request.timeout_sec,
                endpoint=request.ros_endpoint,
                ros_type=request.ros_type,
            )
            try:
                future = client.send_goal_async(
                    goal,
                    feedback_callback=lambda feedback: self._on_action_feedback(
                        request.endpoint.name,
                        command_id,
                        feedback,
                    ),
                )
            except Exception as exc:
                submit_error = exc
                started = float(self._action_status.get("started_monotonic", 0.0))
                self._action_status.update(
                    {
                        "state": "failed",
                        "success": False,
                        "terminal": True,
                        "reason_code": f"action_submit_error:{type(exc).__name__}",
                        "elapsed_sec": max(0.0, time.monotonic() - started),
                        "last_update_monotonic": time.monotonic(),
                    }
                )
                self._active_route = ""
                self._active_command_id = ""
                self._active_goal_handle = None
        if submit_error is not None:
            reason_code = f"action_submit_error:{type(submit_error).__name__}"
            self._record(
                "typed_action_submit_failed",
                {
                    "route": request.endpoint.name,
                    "command_id": command_id,
                    "reason_code": reason_code,
                },
            )
            return False, "", f"failed to submit Action Goal ({reason_code})"
        self._record(
            "command_started",
            {
                "route": request.endpoint.name,
                "endpoint": request.ros_endpoint,
                "type": request.ros_type,
                "command_id": command_id,
                "source": source,
            },
        )
        assert future is not None
        future.add_done_callback(
            lambda result: self._on_goal_response(
                request.endpoint.name,
                command_id,
                result,
            )
        )
        return True, command_id, f"{request.endpoint.name} Action Goal submitted"

    def _dispatch_typed_service(
        self,
        request: ResolvedDispatch,
        mapped: dict[str, Any],
        *,
        source: str,
    ) -> tuple[bool, str, str]:
        if (
            request.endpoint.payload_codec != "retraction_command"
            or request.ros_type
            != "surgical_interop_msgs/srv/ExecuteRetractionCommand"
        ):
            return IntegrationDebugNode._dispatch_generic_typed_service(
                self, request, mapped, source=source
            )
        client = getattr(self, "_retraction_client", None)
        if not IntegrationDebugNode._client_is_ready(client, "service_is_ready"):
            with self._lock:
                self._last_retraction_rejection_reason = "retraction_service_unavailable"
            return False, "", f"{request.ros_endpoint} Service is unavailable"
        command_name = str(mapped["command"])
        command_id = f"debug-{uuid4()}"
        submit_error: Exception | None = None
        future: Any | None = None
        with self._lock:
            if request.single_flight and self._active_command_id:
                return False, self._active_command_id, "another command is active"
            # This state is observation only.  The external Service owns
            # command-order admission; Debug does not maintain a parallel
            # retraction allowlist.
            service_request = self._build_retraction_service_request(command_id, mapped)
            blocked_reason = IntegrationDebugNode._dispatch_block_reason(self, request)
            if blocked_reason:
                return False, "", blocked_reason
            blocked_reason = IntegrationDebugNode._final_physical_dispatch_block_reason(
                self, request
            )
            if blocked_reason:
                return False, "", blocked_reason
            self._start_action_locked(
                request.endpoint.name,
                command_id,
                source,
                command=command_name,
                response_semantics=request.endpoint.response_semantics,
                timeout_sec=request.timeout_sec,
                endpoint=request.ros_endpoint,
                ros_type=request.ros_type,
            )
            self._last_retraction_rejection_reason = ""
            try:
                future = client.call_async(service_request)
            except Exception as exc:
                submit_error = exc
        if submit_error is not None:
            reason_code = f"service_submit_error:{type(submit_error).__name__}"
            with self._lock:
                if self._active_command_id == command_id:
                    started = float(self._action_status.get("started_monotonic", 0.0))
                    self._action_status.update(
                        {
                            "state": "failed",
                            "progress": 0.0,
                            "success": False,
                            "terminal": True,
                            "request_accepted": False,
                            "result_code": None,
                            "reason_code": reason_code,
                            "response_message": "",
                            "elapsed_sec": max(0.0, time.monotonic() - started),
                            "last_update_monotonic": time.monotonic(),
                        }
                    )
                    self._active_route = ""
                    self._active_command_id = ""
                    self._active_goal_handle = None
                    self._last_retraction_rejection_reason = reason_code
            self._record(
                "typed_service_submit_failed",
                {
                    "route": request.endpoint.name,
                    "command_id": command_id,
                    "command": command_name,
                    "reason_code": reason_code,
                },
            )
            return False, "", f"failed to submit Service request ({reason_code})"
        self._record(
            "command_started",
            {
                "route": request.endpoint.name,
                "endpoint": request.ros_endpoint,
                "type": request.ros_type,
                "command_id": command_id,
                "source": source,
                "robot_endpoint_source": getattr(self, "_robot_endpoint_source", "external"),
            },
        )
        assert future is not None
        future.add_done_callback(
            lambda result: self._on_retraction_service_response(command_id, result)
        )
        return True, command_id, "retraction Service request submitted"

    def _dispatch_generic_typed_service(
        self,
        request: ResolvedDispatch,
        mapped: dict[str, Any],
        *,
        source: str,
    ) -> tuple[bool, str, str]:
        """Dispatch a declared ROS Service through the installed interface.

        A generic Service exposes transport/admission outcome only. It never
        guesses controller state or adds a Debug-side state machine.
        """

        with self._lock:
            if request.single_flight and self._active_command_id:
                return False, self._active_command_id, "another command is active"
        try:
            client = self._typed_service_client_for(request)
        except (TypeError, ValueError) as exc:
            return False, "", str(exc)
        if not IntegrationDebugNode._client_is_ready(client, "service_is_ready"):
            return False, "", f"{request.ros_endpoint} Service is unavailable"
        command_id = f"debug-{uuid4()}"
        try:
            service_request = build_wire_payload(
                kind="service",
                ros_type=request.ros_type,
                payload=mapped,
                fixed_payload=request.endpoint.fixed_payload,
                command_id=command_id if request.physical else "",
                command_id_field=(
                    request.endpoint.command_id_field if request.physical else ""
                ),
            )
        except ValueError as exc:
            return False, "", str(exc)

        submit_error: Exception | None = None
        future: Any | None = None
        with self._lock:
            if request.single_flight and self._active_command_id:
                return False, self._active_command_id, "another command is active"
            blocked_reason = IntegrationDebugNode._dispatch_block_reason(self, request)
            if blocked_reason:
                return False, "", blocked_reason
            blocked_reason = IntegrationDebugNode._final_physical_dispatch_block_reason(
                self, request
            )
            if blocked_reason:
                return False, "", blocked_reason
            self._start_action_locked(
                request.endpoint.name,
                command_id,
                source,
                response_semantics=request.endpoint.response_semantics,
                timeout_sec=request.timeout_sec,
                endpoint=request.ros_endpoint,
                ros_type=request.ros_type,
            )
            try:
                future = client.call_async(service_request)
            except Exception as exc:
                submit_error = exc
                started = float(self._action_status.get("started_monotonic", 0.0))
                self._action_status.update(
                    {
                        "state": "failed",
                        "progress": 0.0,
                        "success": False,
                        "terminal": True,
                        "request_accepted": False,
                        "result_code": None,
                        "reason_code": f"service_submit_error:{type(exc).__name__}",
                        "response_message": "",
                        "elapsed_sec": max(0.0, time.monotonic() - started),
                        "last_update_monotonic": time.monotonic(),
                    }
                )
                self._active_route = ""
                self._active_command_id = ""
                self._active_goal_handle = None
        if submit_error is not None:
            reason_code = f"service_submit_error:{type(submit_error).__name__}"
            self._record(
                "typed_service_submit_failed",
                {
                    "route": request.endpoint.name,
                    "command_id": command_id,
                    "reason_code": reason_code,
                },
            )
            return False, "", f"failed to submit Service request ({reason_code})"
        self._record(
            "command_started",
            {
                "route": request.endpoint.name,
                "endpoint": request.ros_endpoint,
                "type": request.ros_type,
                "command_id": command_id,
                "source": source,
            },
        )
        assert future is not None
        future.add_done_callback(
            lambda result: self._on_generic_service_response(
                request.endpoint.name,
                command_id,
                request.endpoint.response_semantics,
                result,
            )
        )
        return True, command_id, f"{request.endpoint.name} Service request submitted"

    def _route_server_ready(self, route: str) -> bool:
        request = self._configured_dispatch_request(route)
        if request is not None:
            if request.kind == "topic":
                return self._capability_enabled("control")
            try:
                client = (
                    self._typed_action_client_for(request)
                    if request.kind == "action"
                    else self._typed_service_client_for(request)
                )
            except (TypeError, ValueError):
                return False
            return IntegrationDebugNode._client_is_ready(
                client,
                "server_is_ready" if request.kind == "action" else "service_is_ready",
            )
        if route == "tool_handover":  # narrow legacy harness fallback
            return IntegrationDebugNode._selected_tool_client_ready(self)
        if route == "retraction_service":
            return IntegrationDebugNode._selected_retraction_client_ready(self)
        return False

    def _typed_dispatch_endpoint_ready(self, name: str) -> bool:
        """Return transport readiness without turning diagnostics into admission."""

        return self._route_server_ready(name)

    def _dispatch_endpoint_name(self, name: str, source: str) -> str:
        """Resolve a configured route, retaining small pure-test harness support."""

        dispatcher = getattr(self, "_typed_dispatcher", None)
        if dispatcher is not None:
            return dispatcher.endpoint_for(name, source)
        if name == "tool_handover":
            return (
                str(
                    getattr(
                        self,
                        "_virtual_tool_handover_name",
                        VIRTUAL_TOOL_HANDOVER_DEFAULT_NAME,
                    )
                )
                if source == "virtual"
                else TOOL_HANDOVER_DEFAULT_NAME
            )
        if name == "retraction_service":
            return (
                str(
                    getattr(
                        self,
                        "_virtual_retraction_service_name",
                        VIRTUAL_RETRACTION_SERVICE_DEFAULT_NAME,
                    )
                )
                if source == "virtual"
                else str(
                    getattr(
                        self,
                        "_external_retraction_service_name",
                        RETRACTION_SERVICE_DEFAULT_NAME,
                    )
                )
            )
        raise ValueError("unsupported typed dispatch endpoint")

    def _build_retraction_service_request(
        self, command_id: str, mapped: dict[str, Any]
    ) -> ExecuteRetractionCommand.Request:
        request = ExecuteRetractionCommand.Request()
        request.protocol_version = ExecuteRetractionCommand.Request.PROTOCOL_VERSION_V1
        request.source_id = RETRACTION_SERVICE_SOURCE_ID
        request.command_id = command_id
        request.command = getattr(
            ExecuteRetractionCommand.Request,
            RETRACTION_COMMAND_CONSTANTS[str(mapped["command"])],
        )
        # The deployed robot peer treats direct-teach completion as one
        # session-level operation and rejects arm selectors with
        # RESULT_INVALID_PARAMETER.  Keep the Debug UI's left/right/both
        # field choice as operator context, but project every finish request
        # to the peer-compatible TARGET_NONE wire value.  Adjustment commands
        # retain their selected-arm semantics (including the explicit BOTH
        # wire value).
        target_side = (
            "none"
            if str(mapped["command"]) == "finish_direct_teach"
            else str(mapped["target_side"])
        )
        request.target_side = getattr(
            ExecuteRetractionCommand.Request,
            RETRACTION_TARGET_SIDE_CONSTANTS[target_side],
        )
        request.distance_m = float(mapped["distance_m"])
        return request

    def _start_action(
        self,
        route: str,
        command_id: str,
        source: str,
        *,
        command: str = "",
        response_semantics: str = "action",
        timeout_sec: float = 0.0,
        endpoint: str = "",
        ros_type: str = "",
    ) -> None:
        with self._lock:
            self._start_action_locked(
                route,
                command_id,
                source,
                command=command,
                response_semantics=response_semantics,
                timeout_sec=timeout_sec,
                endpoint=endpoint,
                ros_type=ros_type,
            )
        self._record(
            "command_started",
            {"route": route, "command_id": command_id, "source": source},
        )

    def _start_action_locked(
        self,
        route: str,
        command_id: str,
        source: str,
        *,
        command: str = "",
        response_semantics: str = "action",
        timeout_sec: float = 0.0,
        endpoint: str = "",
        ros_type: str = "",
    ) -> None:
        """Reserve the single active command while ``self._lock`` is held."""

        now = time.monotonic()
        self._active_route = route
        self._active_command_id = command_id
        self._active_goal_handle = None
        self._action_status = {
            "route": route,
            "command_id": command_id,
            "command": command,
            "response_semantics": response_semantics,
            "endpoint": endpoint,
            "type": ros_type,
            "timeout_sec": float(timeout_sec),
            "request_accepted": None,
            "result_code": None,
            "response_message": "",
            "state": "submitting",
            "progress": 0.0,
            "success": False,
            "terminal": False,
            "reason_code": "",
            "recovery_required": False,
            "source": source,
            "robot_endpoint_source": getattr(
                self, "_robot_endpoint_source", "external"
            ),
            "started_monotonic": now,
            "last_update_monotonic": now,
            "server_unavailable_since_monotonic": 0.0,
            "recovery_detected_monotonic": 0.0,
        }

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
            self._action_status["last_update_monotonic"] = time.monotonic()
            if not self._action_status.get("recovery_required"):
                self._action_status["state"] = "accepted"
        result_future = goal_handle.get_result_async()
        result_future.add_done_callback(
            lambda result: self._on_action_result(route, command_id, result)
        )

    def _on_action_feedback(
        self, route: str, command_id: str, feedback_message: Any
    ) -> None:
        del route
        state, progress = action_feedback_fields(feedback_message)
        with self._lock:
            if self._active_command_id != command_id:
                return
            if not self._action_status.get("recovery_required"):
                self._action_status["state"] = state
            if progress is not None:
                self._action_status["progress"] = progress
            self._action_status["last_update_monotonic"] = time.monotonic()

    def _on_action_result(self, route: str, command_id: str, future: Any) -> None:
        try:
            wrapped = future.result()
            success, final_state, reason_code = action_result_fields(wrapped)
        except Exception as exc:
            success = False
            final_state = "failed"
            reason_code = f"result_error:{exc}"
        self._finish_action(route, command_id, success, final_state, reason_code)

    def _on_retraction_service_response(self, command_id: str, future: Any) -> None:
        """Record only the Service admission response, never physical completion."""

        try:
            response = future.result()
            response_command_id = str(response.command_id).strip()
            request_accepted = bool(response.request_accepted)
            result_code = int(response.result_code)
            response_message = str(response.message).strip()
        except Exception as exc:
            self._finish_retraction_service_admission(
                command_id,
                request_accepted=False,
                result_code=None,
                state="failed",
                reason_code=f"service_response_error:{exc}",
                response_message="",
            )
            return

        if response_command_id != command_id:
            self._finish_retraction_service_admission(
                command_id,
                request_accepted=False,
                result_code=result_code,
                state="failed",
                reason_code="response_command_id_mismatch",
                response_message=response_message,
            )
            return

        accepted_code = ExecuteRetractionCommand.Response.RESULT_ACCEPTED
        if request_accepted and result_code == accepted_code:
            state = "accepted"
            reason_code = "RESULT_ACCEPTED"
        elif request_accepted:
            state = "failed"
            reason_code = "response_contract_mismatch"
        else:
            state = "rejected"
            reason_code = self._retraction_result_code_name(result_code)

        self._finish_retraction_service_admission(
            command_id,
            request_accepted=request_accepted,
            result_code=result_code,
            state=state,
            reason_code=reason_code,
            response_message=response_message,
        )

    def _on_generic_service_response(
        self,
        route: str,
        command_id: str,
        response_semantics: str,
        future: Any,
    ) -> None:
        """Finish a generic typed Service call without endpoint-specific logic."""

        try:
            response = future.result()
            accepted = bool(
                getattr(
                    response,
                    "request_accepted",
                    getattr(response, "success", True),
                )
            )
            raw_result_code = getattr(response, "result_code", None)
            result_code = (
                int(raw_result_code) if raw_result_code is not None else None
            )
            response_message = str(
                getattr(response, "message", getattr(response, "detail", ""))
                or ""
            ).strip()
        except Exception as exc:
            accepted = False
            result_code = None
            response_message = ""
            reason_code = f"service_response_error:{type(exc).__name__}"
        else:
            reason_code = (
                "accepted" if accepted else "service_rejected"
            )
        state = (
            "accepted"
            if response_semantics == "admission" and accepted
            else "rejected"
            if response_semantics == "admission"
            else "completed"
            if accepted
            else "failed"
        )
        self._finish_generic_service_response(
            route,
            command_id,
            response_semantics=response_semantics,
            request_accepted=accepted,
            result_code=result_code,
            state=state,
            reason_code=reason_code,
            response_message=response_message,
        )

    def _finish_generic_service_response(
        self,
        route: str,
        command_id: str,
        *,
        response_semantics: str,
        request_accepted: bool,
        result_code: int | None,
        state: str,
        reason_code: str,
        response_message: str,
    ) -> None:
        with self._lock:
            if self._active_command_id != command_id:
                return
            started = float(self._action_status.get("started_monotonic", 0.0))
            self._action_status.update(
                {
                    "state": state,
                    "progress": 0.0,
                    # Admission only means a server received the request; it
                    # never claims physical completion.
                    "success": (
                        request_accepted if response_semantics != "admission" else False
                    ),
                    "terminal": True,
                    "request_accepted": request_accepted,
                    "result_code": result_code,
                    "reason_code": reason_code,
                    "response_message": response_message,
                    "recovery_required": False,
                    "elapsed_sec": max(0.0, time.monotonic() - started),
                    "last_update_monotonic": time.monotonic(),
                    "server_unavailable_since_monotonic": 0.0,
                    "recovery_detected_monotonic": 0.0,
                }
            )
            self._active_route = ""
            self._active_command_id = ""
            self._active_goal_handle = None
        self._record(
            "typed_service_response",
            {
                "route": route,
                "command_id": command_id,
                "response_semantics": response_semantics,
                "request_accepted": request_accepted,
                "result_code": result_code,
                "reason_code": reason_code,
                "message": response_message,
            },
        )

    @staticmethod
    def _retraction_result_code_name(result_code: int) -> str:
        names = {
            ExecuteRetractionCommand.Response.RESULT_INVALID_COMMAND: "RESULT_INVALID_COMMAND",
            ExecuteRetractionCommand.Response.RESULT_INVALID_PARAMETER: "RESULT_INVALID_PARAMETER",
            ExecuteRetractionCommand.Response.RESULT_REJECTED: "RESULT_REJECTED",
            ExecuteRetractionCommand.Response.RESULT_ERROR: "RESULT_ERROR",
        }
        return names.get(result_code, f"RESULT_CODE_{result_code}")

    def _finish_retraction_service_admission(
        self,
        command_id: str,
        *,
        request_accepted: bool,
        result_code: int | None,
        state: str,
        reason_code: str,
        response_message: str,
    ) -> None:
        with self._lock:
            if self._active_command_id != command_id:
                return
            started = float(self._action_status.get("started_monotonic", 0.0))
            command = str(self._action_status.get("command", ""))
            try:
                normalized_command = RetractionCommand(command)
            except ValueError:
                normalized_command = None
            state_before_admission = self._retraction_state
            if state == "accepted" and request_accepted:
                if command in RETRACTION_STATE_NEUTRAL_COMMANDS:
                    # Suction commands share the transport endpoint but do not
                    # change the retractor lifecycle state.
                    self._last_retraction_rejection_reason = ""
                elif normalized_command is None:
                    # A malformed local status projection is not a controller
                    # rejection. Keep it explicitly unknown rather than
                    # inventing a physical state or creating a second gate.
                    self._retraction_state = RetractionState.UNKNOWN
                    self._last_retraction_rejection_reason = "unknown_command_projection"
                else:
                    self._retraction_state = apply_retractor_service_admission(
                        state_before_admission,
                        normalized_command,
                        True,
                    )
                    self._last_retraction_rejection_reason = ""
            elif state == "failed":
                # A transport/contract failure leaves admission uncertain.  It
                # is intentionally not treated as a controller failure.
                self._retraction_state = RetractionState.UNKNOWN
                self._last_retraction_rejection_reason = reason_code
            else:
                self._last_retraction_rejection_reason = reason_code
            self._action_status.update(
                {
                    "state": state,
                    # A Service response is terminal only for the client call.
                    # It is not a physical-motion terminal state.
                    "progress": 0.0,
                    "success": False,
                    "terminal": True,
                    "request_accepted": request_accepted,
                    "result_code": result_code,
                    "reason_code": reason_code,
                    "response_message": response_message,
                    "recovery_required": False,
                    "elapsed_sec": max(0.0, time.monotonic() - started),
                    "last_update_monotonic": time.monotonic(),
                    "server_unavailable_since_monotonic": 0.0,
                    "recovery_detected_monotonic": 0.0,
                }
            )
            self._active_route = ""
            self._active_command_id = ""
            self._active_goal_handle = None
        self._record(
            "retraction_service_response",
            {
                "command_id": command_id,
                "command": command,
                "request_accepted": request_accepted,
                "result_code": result_code,
                "reason_code": reason_code,
                "message": response_message,
            },
        )

    def _finish_action(
        self,
        route: str,
        command_id: str,
        success: bool,
        final_state: str,
        reason_code: str,
    ) -> None:
        reconciled = False
        with self._lock:
            if self._active_command_id != command_id:
                return
            reconciled = bool(self._action_status.get("recovery_required"))
            started = float(self._action_status.get("started_monotonic", 0.0))
            self._action_status.update(
                {
                    "state": final_state,
                    "progress": 1.0,
                    "success": success,
                    "terminal": True,
                    "reason_code": reason_code,
                    "recovery_required": False,
                    "elapsed_sec": max(0.0, time.monotonic() - started),
                    "last_update_monotonic": time.monotonic(),
                    "server_unavailable_since_monotonic": 0.0,
                    "recovery_detected_monotonic": 0.0,
                }
            )
            self._active_route = ""
            self._active_command_id = ""
            self._active_goal_handle = None
            if reconciled:
                self._fault_locked = False
                self._last_error = ""
            if reason_code in {"cancel_recovery_failed", "cancel_rejected"}:
                self._fault_locked = True
                self._disarm_locked()
        if reason_code in {"cancel_recovery_failed", "cancel_rejected"}:
            self._release_manual_publishers()
        if reconciled:
            self._record(
                "action_late_result_reconciled",
                {
                    "route": route,
                    "command_id": command_id,
                    "success": success,
                    "final_state": final_state,
                    "reason_code": reason_code,
                },
            )
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
            if route == "retraction_service":
                return (
                    False,
                    command_id,
                    "the retraction Service request is non-cancellable; wait for the "
                    "response, then issue an explicit stop_retraction command if needed",
                )
            if goal_handle is None:
                return False, command_id, "Action Goal has not been accepted yet"
            self._action_status["state"] = "cancel_requested"
            self._action_status["last_update_monotonic"] = time.monotonic()
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
            reason_code = "cancel_rejected"
        except Exception:
            accepted = False
            reason_code = "cancel_response_error"
        if accepted:
            with self._lock:
                if self._active_command_id == command_id:
                    self._action_status["state"] = "cancel_accepted"
                    self._action_status["last_update_monotonic"] = time.monotonic()
            self._record(
                "cancel_accepted", {"route": route, "command_id": command_id}
            )
            return
        self._mark_action_recovery_required(
            reason_code,
            state="cancel_rejected" if reason_code == "cancel_rejected" else "remote_state_unknown",
        )
        self._record(
            reason_code, {"route": route, "command_id": command_id}
        )

    def _mark_action_recovery_required(
        self, reason_code: str, *, state: str = "remote_state_unknown"
    ) -> bool:
        now = time.monotonic()
        with self._lock:
            command_id = self._active_command_id
            route = self._active_route
            if not command_id or self._action_status.get("terminal"):
                return False
            if self._action_status.get("recovery_required"):
                return False
            started = float(self._action_status.get("started_monotonic", 0.0))
            admission_only = (
                self._action_status.get("response_semantics") == "admission"
            )
            self._fault_locked = True
            self._disarm_locked()
            if admission_only and route == "retraction_service":
                # No timeout/server-loss path can prove whether the request was
                # admitted. Preserve that uncertainty in the local voice
                # normalizer rather than guessing a state transition.
                self._retraction_state = RetractionState.UNKNOWN
                self._last_retraction_rejection_reason = reason_code
            self._action_status.update(
                {
                    "state": state,
                    "reason_code": reason_code,
                    "recovery_required": True,
                    "recovery_detected_monotonic": now,
                }
            )
            if admission_only:
                self._last_error = (
                    f"retraction Service request acceptance is uncertain ({reason_code}); "
                    "confirm the remote robot state before recovering the client"
                )
            else:
                self._last_error = (
                    f"remote command state is uncertain ({reason_code}); "
                    "confirm the remote robot state before recovering the client"
                )
            event = {
                "route": route,
                "command_id": command_id,
                "response_semantics": (
                    "admission" if admission_only else "action"
                ),
                "state": state,
                "reason_code": reason_code,
                "elapsed_sec": round(max(0.0, now - started), 3) if started else 0.0,
            }
        self._record(
            "service_admission_recovery_required"
            if admission_only
            else "action_recovery_required",
            event,
        )
        self._release_manual_publishers()
        return True

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
            self._ensure_output_publisher(topic)
        with self._lock:
            state.rate_hz = rate_hz
            state.enabled = enabled
            state.last_published_monotonic = 0.0
        if not enabled:
            self._destroy_output_publisher(topic)
        return True, "", f"{topic} {'enabled' if enabled else 'disabled'} at {rate_hz:.2f} Hz"

    def _release_output_publishers(self) -> None:
        with self._lock:
            for state in self._output_states.values():
                state.enabled = False
            publishers = list(self._output_publishers.values())
            self._output_publishers.clear()
        for publisher in publishers:
            self.destroy_publisher(publisher)

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
        if due:
            blocked_reason = self._manual_write_block_reason()
            if blocked_reason:
                with self._lock:
                    stopped = [
                        state.topic
                        for state in self._output_states.values()
                        if state.enabled
                    ]
                    self._last_error = blocked_reason
                self._release_output_publishers()
                self._record(
                    "outputs_stopped_by_runtime_gate",
                    {"topics": stopped, "reason": blocked_reason},
                )
                return
        for topic in due:
            if self._output_conflicts(topic):
                with self._lock:
                    self._output_states[topic].enabled = False
                    self._last_error = f"stopped {topic}: another publisher was discovered"
                self._record(
                    "output_conflict",
                    {"topic": topic, "publishers": self._output_conflicts(topic)},
                )
                self._destroy_output_publisher(topic)
                continue
            self._publish_output(topic)

    def _publish_output(self, topic: str) -> None:
        with self._lock:
            state = self._output_states[topic]
            state.sequence += 1
            sequence = state.sequence
        message = self._dummy_message(topic, sequence)
        self._ensure_output_publisher(topic).publish(message)
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
            msg.schema_version = "1.0.0"
            msg.catalog_version = "debug:none"
            msg.gateway_instance_id = f"debug:{self._session_id}"
            msg.procedure_run_id = f"debug:{self._session_id}"
            msg.procedure_type = "integration_debug"
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
                reported_rate_hz = stats.reported_rate_hz
                reported_payload_bytes = stats.reported_payload_bytes
                reported_source_topic = stats.reported_source_topic
                reported_published_count = stats.reported_published_count
                reported_dropped_count = stats.reported_dropped_count
                reported_qos = stats.reported_qos
            monitor_rate_hz, window_count = measured_rate(
                arrivals, now, self._monitor_window_sec
            )
            recent_sizes = [size for stamp, size in sizes if now - stamp <= self._monitor_window_sec]
            rate_hz = (
                reported_rate_hz
                if reported_rate_hz is not None
                else monitor_rate_hz
            )
            bandwidth = (
                reported_payload_bytes * rate_hz
                if reported_payload_bytes is not None
                else sum(recent_sizes) / self._monitor_window_sec
            )
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
                    "monitor_hz": round(monitor_rate_hz, 3),
                    "message_count": message_count,
                    "source_message_count": reported_published_count,
                    "source_dropped_count": reported_dropped_count,
                    "window_message_count": window_count,
                    "last_age_sec": round(age_sec, 3) if age_sec is not None else None,
                    "source_delay_sec": round(source_delay, 3) if source_delay is not None else None,
                    "bandwidth_bytes_sec": round(bandwidth, 1),
                    "source_topic": reported_source_topic,
                    "source_type": (
                        "sensor_msgs/msg/CompressedImage"
                        if reported_source_topic
                        else ""
                    ),
                    "source_qos": reported_qos,
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
            publisher_topics = set(self._output_publishers)
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
                    "publisher_active": state.topic in publisher_topics,
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
            last_update = float(action.get("last_update_monotonic", 0.0))
            action["last_update_age_sec"] = (
                round(max(0.0, now - last_update), 3) if last_update else None
            )
            recovery_detected = float(
                action.get("recovery_detected_monotonic", 0.0)
            )
            action["recovery_age_sec"] = (
                round(max(0.0, now - recovery_detected), 3)
                if recovery_detected
                else None
            )
            action["cancel_available"] = bool(
                self._active_command_id
                and self._active_route != "retraction_service"
                and self._active_goal_handle is not None
            )
            action["server_ready"] = self._route_server_ready(
                str(action.get("route", ""))
            )
            action.pop("started_monotonic", None)
            action.pop("last_update_monotonic", None)
            action.pop("server_unavailable_since_monotonic", None)
            action.pop("recovery_detected_monotonic", None)
            recent_events = list(self._recent_events)
            retraction_state = self._retraction_state
            retraction_in_flight = bool(
                self._active_command_id and self._active_route == "retraction_service"
            )
            voice = {
                "auto_execute": self._voice_auto_execute,
                "last_sentence": self._last_sentence,
                "last_parse": dict(self._last_voice_parse),
                "retraction": {
                    "mode": "direct_service_only",
                    "internal_state": retraction_state.value,
                    "allowed_commands": sorted(
                        command.value
                        for command in frozenset(RetractionCommand)
                    ),
                    "local_state_is_observation_only": True,
                    # The client checks DDS readiness outside this lock below.
                    "service_ready": False,
                    "service_source": self._robot_endpoint_source,
                    "service_endpoint": self._retraction_service_name,
                    "in_flight": retraction_in_flight,
                    "last_rejection_reason": self._last_retraction_rejection_reason,
                },
            }
            armed = self._armed
            acknowledged_blocked_nodes = sorted(self._acknowledged_blocked_nodes)
            last_error = self._last_error
        voice["retraction"]["service_ready"] = bool(
            self._selected_retraction_client_ready()
        )
        detected_planner_nodes = self._detected_planner_nodes()
        operational = self._operational_runtime_status()
        blocked = self._blocked_nodes()
        operational_runtime_is_stopped = bool(
            operational["stopped"]
            if self._network_locked_to_runtime
            else not detected_planner_nodes
        )
        intervention_allowed = bool(
            operational["intervention_allowed"]
            if self._network_locked_to_runtime
            else not detected_planner_nodes
        )
        manual_control_available = bool(
            self._capability_enabled("control")
            and
            intervention_allowed
            and not self._fault_locked
            and not self._active_command_id
        )
        if self._capability_enabled("network"):
            try:
                from integration_debug.networking import collect_network_status

                network = collect_network_status()
            except Exception as exc:
                network = {
                    "preferred_interface": os.environ.get(
                        "TASKPLANNER_DEBUG_NETWORK_INTERFACE", ""
                    ),
                    "primary_interface": "",
                    "primary_ipv4": "",
                    "prefix_length": 0,
                    "gateway_ipv4": "",
                    "multicast_capable": False,
                    "interface_present": False,
                    "interface_kind": "unknown",
                    "link_up": False,
                    "selection_source": "inspection_error",
                    "addresses": [],
                    "error": str(exc),
                }
        else:
            network = {
                "preferred_interface": "",
                "primary_interface": "",
                "primary_ipv4": "",
                "prefix_length": 0,
                "gateway_ipv4": "",
                "multicast_capable": False,
                "interface_present": False,
                "interface_kind": "not_started",
                "link_up": False,
                "selection_source": "capability_disabled",
                "addresses": [],
                "error": "debug network capability is not started",
            }
        network.update(
            {
                "settings_path": str(self._network_settings_path),
                "restart_supported": self._restart_supported,
                "restart_scheduled": self._restart_scheduled,
                "locked_to_runtime": self._network_locked_to_runtime,
                "locked_to_runtime_network": self._network_locked_to_runtime,
                "lock_reason": (
                    "DDS settings follow the active Taskplanner runtime"
                    if self._network_locked_to_runtime
                    else ""
                ),
                "active_domain_id": int(
                    os.environ.get("ROS_DOMAIN_ID", "0") or 0
                ),
                "active_discovery_range": os.environ.get(
                    "ROS_AUTOMATIC_DISCOVERY_RANGE", ""
                ).strip().upper(),
            }
        )
        bed_robot_ready, bed_robot_age_sec = self._bed_robot_arm_status_ready()
        robot_source = self._robot_source_snapshot()
        typed_dispatch = self._typed_dispatcher.public_policy(
            self._robot_endpoint_source
        )
        for row in typed_dispatch:
            row["ready"] = self._typed_dispatch_endpoint_ready(str(row["name"]))
            row["source"] = self._robot_endpoint_source
        endpoints = typed_dispatch + [
            {
                "name": "bed_robot_arm_status",
                "endpoint": (
                    self._virtual_bed_robot_status_topic
                    if self._robot_endpoint_source == "virtual"
                    else BED_ROBOT_STATUS_DEFAULT_TOPIC
                ),
                "kind": "topic",
                "ready": bed_robot_ready,
                "source": self._robot_endpoint_source,
                "age_sec": (
                    round(bed_robot_age_sec, 3)
                    if bed_robot_age_sec is not None
                    else None
                ),
                "detail": dict(self._bed_robot_arm_status_summary),
            },
        ]
        snapshot = {
            "schema": STATUS_SCHEMA,
            "capabilities": getattr(
                self, "_capabilities", DebugCapabilities.parse("full")
            ).status_rows(),
            "stamp_sec": round(self.get_clock().now().nanoseconds / 1e9, 6),
            "session": {
                "session_id": self._session_id,
                "state": self._session_state(),
                "armed": armed,
                "manual_control_scope": getattr(
                    self, "_manual_control_scope", "all" if armed else "none"
                ),
                "acknowledged_blocked_nodes": acknowledged_blocked_nodes,
                "planner_coexistence_active": bool(
                    armed and acknowledged_blocked_nodes
                ),
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
                "detected_planner_nodes": detected_planner_nodes,
                "operational_state": operational["execution_state"],
                "operational_running": operational["running"],
                "operational_active_robot_task_id": operational[
                    "active_robot_task_id"
                ],
                "operational_robot_state": operational["robot_state"],
                "operational_cleaner_busy": operational["cleaner_busy"],
                "operational_state_publishers": operational["publishers"],
                "operational_state_expected_publisher": operational[
                    "expected_publisher"
                ],
                "operational_state_publisher_trusted": operational[
                    "publisher_trusted"
                ],
                "operational_state_age_sec": (
                    round(float(operational["age_sec"]), 3)
                    if operational["age_sec"] is not None
                    else None
                ),
                "operational_state_fresh": operational["fresh"],
                "operational_runtime_stopped": operational_runtime_is_stopped,
                "operational_intervention_allowed": intervention_allowed,
                "operational_intervention_block_reason": (
                    operational["intervention_block_reason"]
                    if self._network_locked_to_runtime
                    else ""
                ),
                "operational_control_window_open": bool(
                    operational["control_window_open"]
                    if self._network_locked_to_runtime
                    else not detected_planner_nodes
                ),
                "manual_control_available": manual_control_available,
                "manual_control_gate": (
                    "operational_state"
                    if self._network_locked_to_runtime
                    else "planner_nodes"
                ),
                "planner_coexistence_allowed": self._planner_coexistence_allowed,
                "action_watchdog": dict(self._action_watchdog_policy),
                "network": network,
            },
            "inputs": self._input_status_rows(now),
            "endpoints": endpoints,
            "dispatch": {
                "shape": "kind_endpoint_type_payload_timeout",
                "owner": "typed_debug_dispatcher",
                "voice_execution_owner": (
                    "command_router"
                    if self._network_locked_to_runtime
                    else "debug_typed_dispatcher"
                ),
                "endpoints": typed_dispatch,
            },
            "action": action,
            "outputs": self._output_status_rows(now),
            "voice": voice,
            "virtual_robot": robot_source,
            "asr": self._asr_status_snapshot(),
            "operational_asr": self._operational_asr_status_snapshot(),
            "surgery_record": self._surgery_record_status_snapshot(),
            "recent_events": recent_events,
        }
        if self._owner_role != "observer":
            return snapshot
        with self._lock:
            control_status = self._control_owner_status
            control_received = self._control_owner_status_received_monotonic
        if (
            control_status is None
            or not control_received
            or now - control_received > CONTROL_OWNER_STATUS_MAX_AGE_SEC
        ):
            return snapshot
        return compose_observer_status(snapshot, control_status)

    def _readiness_snapshot(self) -> dict[str, Any]:
        sentence_external_publisher = False
        try:
            publisher_infos = self.get_publishers_info_by_topic(self._asr_topic)
        except Exception:
            publisher_infos = []
        for info in publisher_infos:
            if not (
                str(info.node_name) == self.get_name()
                and str(info.node_namespace) == self.get_namespace()
            ):
                sentence_external_publisher = True
                break
        asr = self._asr_status_snapshot()
        managed_asr_ready = bool(
            self._asr_capture_requested
            and self._asr_sentence_pub is not None
            and asr.get("state") == "LISTENING"
            and asr.get("connected")
        )
        bed_robot_ready, bed_robot_age_sec = self._bed_robot_arm_status_ready()
        # Readiness is scoped to the owners actually started in this process.
        # A status-only observer is useful even when no controller, microphone,
        # or record sidecar exists, so those optional dependencies must not
        # make its read-only health check fail.
        checks: dict[str, bool] = {"observer": True}
        if self._capability_enabled("asr"):
            checks["sentence_publisher"] = (
                sentence_external_publisher or managed_asr_ready
            )
        if self._capability_enabled("control"):
            checks["tool_handover_server"] = self._selected_tool_client_ready()
            checks["retraction_service"] = self._selected_retraction_client_ready()
            checks["bed_robot_arm_status"] = bed_robot_ready
        missing = [name for name, passed in checks.items() if not passed]
        return {
            "schema": "taskplanner.integration_debug.readiness.v1",
            "ready": not missing,
            "checks": checks,
            "missing": missing,
            "details": {
                "mode": "debug",
                "perception_required": False,
                "robot_endpoint_source": self._robot_endpoint_source,
                "capabilities": sorted(
                    getattr(
                        self, "_capabilities", DebugCapabilities.parse("full")
                    ).enabled_names
                ),
                "bed_robot_arm_status_age_sec": (
                    round(bed_robot_age_sec, 3)
                    if bed_robot_age_sec is not None
                    else -1.0
                ),
            },
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

    def _check_runtime_safety(self) -> None:
        now = time.monotonic()
        with self._lock:
            if not self._armed:
                return
        integrated = self._network_locked_to_runtime
        operational = self._operational_runtime_status() if integrated else None
        blocked = [] if integrated else self._blocked_nodes()
        with self._lock:
            expired = (
                self._armed
                and self._last_heartbeat_monotonic > 0.0
                and now - self._last_heartbeat_monotonic > self._heartbeat_timeout_sec
            )
            acknowledged = sorted(self._acknowledged_blocked_nodes)
            command_id = self._active_command_id
            if operational is not None:
                # A command admitted from an idle paused/stopped snapshot may
                # itself make the robot busy.  Preserve that activity only
                # while the task identity is empty or correlated to this
                # command; unrelated tasks and all cleaner activity revoke it.
                operational_gate_reason = (
                    IntegrationDebugNode._active_command_operational_block_reason(
                        operational, command_id
                    )
                    if command_id
                    else str(operational["intervention_block_reason"])
                )
                gate_open = not operational_gate_reason
                operational_gate_closed = self._armed and not gate_open
                planner_set_changed = False
            else:
                operational_gate_reason = ""
                operational_gate_closed = False
                planner_set_changed = self._armed and bool(blocked)
            if not expired and not planner_set_changed and not operational_gate_closed:
                return
            self._disarm_locked()
            if operational_gate_closed:
                self._last_error = (
                    operational_gate_reason
                    + "; manual control was disarmed"
                )
            elif planner_set_changed:
                self._last_error = (
                    "Taskplanner node discovered; standalone manual control was disarmed: "
                    + ", ".join(blocked or ["none"])
                )
        if operational_gate_closed:
            self._record(
                "operational_intervention_gate_closed",
                {
                    "active_command_id": command_id,
                    "execution_state": operational["execution_state"],
                    "running": operational["running"],
                    "active_robot_task_id": operational["active_robot_task_id"],
                    "robot_state": operational["robot_state"],
                    "cleaner_busy": operational["cleaner_busy"],
                    "reason": operational_gate_reason,
                },
            )
        elif planner_set_changed:
            self._record(
                "planner_coexistence_changed",
                {
                    "active_command_id": command_id,
                    "acknowledged_blocked_nodes": acknowledged,
                    "current_blocked_nodes": blocked,
                },
            )
        else:
            self._record(
                "heartbeat_timeout", {"active_command_id": command_id}
            )
        with self._auxiliary_lock:
            if self._asr is not None:
                self._asr.stop_async()
        self._release_manual_publishers()
        if command_id:
            self._request_cancel()

    def _check_action_watchdog(self) -> None:
        with self._lock:
            command_id = self._active_command_id
            route = self._active_route
            if (
                not command_id
                or self._action_status.get("terminal")
                or self._action_status.get("recovery_required")
            ):
                return
        server_ready = self._route_server_ready(route)
        now = time.monotonic()
        with self._lock:
            if (
                self._active_command_id != command_id
                or self._action_status.get("terminal")
                or self._action_status.get("recovery_required")
            ):
                return
            unavailable_since = float(
                self._action_status.get(
                    "server_unavailable_since_monotonic", 0.0
                )
            )
            if server_ready:
                unavailable_since = 0.0
                self._action_status["server_unavailable_since_monotonic"] = 0.0
            elif unavailable_since <= 0.0:
                unavailable_since = now
                self._action_status[
                    "server_unavailable_since_monotonic"
                ] = unavailable_since
            started = float(self._action_status.get("started_monotonic", now))
            last_update = float(
                self._action_status.get("last_update_monotonic", started)
            )
            watchdog_policy = self._action_watchdog_policy
            configured_timeout_sec = float(
                self._action_status.get("timeout_sec", 0.0) or 0.0
            )
            if configured_timeout_sec > 0.0:
                watchdog_policy = dict(watchdog_policy)
                if self._action_status.get("response_semantics") == "admission":
                    # A typed Service timeout is its admission-response bound,
                    # not a claim about physical completion.
                    watchdog_policy["goal_response_timeout_sec"] = (
                        configured_timeout_sec
                    )
                    watchdog_policy["max_duration_sec"] = max(
                        watchdog_policy["max_duration_sec"],
                        configured_timeout_sec + 1.0,
                    )
                else:
                    watchdog_policy["max_duration_sec"] = min(
                        watchdog_policy["max_duration_sec"],
                        configured_timeout_sec,
                    )
            if (
                route == "retraction_service"
                and str(self._action_status.get("command", "")) == "change_tool"
            ):
                # Preserve a caller's policy-bounded shorter timeout while
                # keeping the historical change-tool bound as an upper cap.
                watchdog_policy = dict(watchdog_policy)
                watchdog_policy["goal_response_timeout_sec"] = min(
                    float(watchdog_policy["goal_response_timeout_sec"]),
                    float(
                        self._action_watchdog_policy[
                            "change_tool_response_timeout_sec"
                        ]
                    ),
                )
            reason_code = action_watchdog_reason(
                terminal=bool(self._action_status.get("terminal")),
                recovery_required=bool(
                    self._action_status.get("recovery_required")
                ),
                state=str(self._action_status.get("state", "")),
                route=route,
                elapsed_sec=max(0.0, now - started),
                last_update_age_sec=max(0.0, now - last_update),
                server_ready=server_ready,
                server_unavailable_age_sec=(
                    max(0.0, now - unavailable_since)
                    if unavailable_since > 0.0
                    else 0.0
                ),
                policy=watchdog_policy,
            )
        if reason_code:
            self._mark_action_recovery_required(reason_code)

    def _publish_status(self) -> None:
        self._check_runtime_safety()
        self._check_action_watchdog()
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
            self._disarm_locked()
            for state in self._output_states.values():
                state.enabled = False
        if self._scenario_debug_recorder is not None:
            self._scenario_debug_recorder.stop("debug_observer_stopped")
        with self._auxiliary_lock:
            self._asr_capture_requested = False
            self._destroy_asr_publisher()
            if self._asr is not None:
                self._asr.close()
        self._release_manual_publishers()
        self._drain_auxiliary_events()
        self._record("session_stopped", {})


def _run(
    capability_override: str | None = None,
    owner_role: str | None = None,
) -> None:
    rclpy.init()
    node = IntegrationDebugNode(
        capability_override=capability_override,
        owner_role=owner_role,
    )
    # Automatic surgery-record submission is a dedicated Live owner.  Keeping
    # it out of this Debug process prevents a broad capability profile from
    # constructing a second uploader for the same procedure run.
    executor = MultiThreadedExecutor(num_threads=4)
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


def main() -> None:
    _run()


def observer_main() -> None:
    """Start the no-write/no-PipeWire Debug observer profile."""

    _run("observer", "observer")


def control_main() -> None:
    """Start the focused typed-dispatch Debug control profile."""

    _run("control", "control")
