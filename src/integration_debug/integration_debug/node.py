"""Minimal scenario-free ROS runtime backing the Taskplanner Debug Mode UI."""

from __future__ import annotations

from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
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
from surgical_msgs.msg import InputSourceStatus, SimulationState
from surgical_msgs.srv import IntegrationDebugCommand

from procedure_spec import (
    NormalizedRetractionCommand,
    RetractionCommand,
    RetractionState,
    allowed_retractor_commands,
    apply_retractor_service_admission,
    normalize_retractor_command,
)
from bt_orchestrator.retractor_voice_interpreter import (
    RetractionVoiceInterpretation,
    TextOnlyRetractionVLMInterpreter,
)

from integration_debug.asr_runtime import AsrMicrophoneRuntime
from integration_debug.contracts import (
    action_watchdog_reason,
    decode_payload,
    load_action_watchdog_policy,
    load_config,
    manual_write_block_reason,
    measured_rate,
    operational_runtime_stopped,
    operational_state_publisher_trusted,
    parse_voice_command,
    validate_action_recovery_acknowledgement,
    validate_planner_coexistence_acknowledgement,
    validate_bed_robot_arm_status,
    validate_tool_handover,
    validate_retraction_command,
)
from integration_debug.surgery_record_runtime import SurgeryRecordRuntime
from integration_debug.networking import (
    collect_network_status,
    ping_ipv4,
    validate_network_settings,
    write_network_settings,
)
from integration_debug.retractor_health import (
    FixedVLMRuntimeClient,
    RetractionVLMHealthResult,
    VLMRuntimeStatus,
    run_retraction_vlm_health_probe,
)


STATUS_SCHEMA = "taskplanner.integration_debug.status.v1"
EVENT_SCHEMA = "taskplanner.integration_debug.event.v1"
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
RETRACTION_SERVICE_SOURCE_ID = "taskplanner_debug"
RETRACTION_COMMAND_CONSTANTS = {
    "start_direct_teach": "COMMAND_START_DIRECT_TEACH",
    "finish_direct_teach": "COMMAND_FINISH_DIRECT_TEACH",
    "start_retraction": "COMMAND_START_RETRACTION",
    "adjust_retraction": "COMMAND_ADJUST_RETRACTION",
    "change_tool": "COMMAND_CHANGE_TOOL",
    "stop_retraction": "COMMAND_STOP_RETRACTION",
}
RETRACTION_TARGET_SIDE_CONSTANTS = {
    "none": "TARGET_NONE",
    "left": "TARGET_LEFT",
    "right": "TARGET_RIGHT",
}
PUBLIC_OUTPUT_TYPES: dict[str, type[Any]] = {
    "surgical_interop_msgs/msg/SurgeryContext": SurgeryContext,
    "surgical_interop_msgs/msg/InstrumentStateArray": InstrumentStateArray,
    "surgical_interop_msgs/msg/RobotStateArray": RobotStateArray,
    "surgical_interop_msgs/msg/SurgeryEvent": SurgeryEvent,
    "surgical_interop_msgs/msg/ClinicalObservationArray": ClinicalObservationArray,
    "surgical_interop_msgs/msg/SurgeryHealth": SurgeryHealth,
}


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
        return {
            str(key): _bounded_event_summary(item)
            for key, item in list(value.items())[:MAX_EVENT_SUMMARY_ITEMS]
        }
    if isinstance(value, (list, tuple)):
        items = [
            _bounded_event_summary(item)
            for item in list(value)[:MAX_EVENT_SUMMARY_ITEMS]
        ]
        if len(value) > MAX_EVENT_SUMMARY_ITEMS:
            items.append(f"[{len(value) - MAX_EVENT_SUMMARY_ITEMS} items omitted]")
        return items
    return value


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


@dataclass(slots=True)
class PendingDebugRetractionInterpretation:
    """One asynchronous text-only VLM request owned by Debug Mode."""

    transcript: str
    current_state: RetractionState
    voice_generation: int
    submitted_monotonic: float
    future: Future[RetractionVoiceInterpretation]


@dataclass(slots=True)
class PendingDebugVLMObservation:
    """One non-commanding manager refresh plus optional model micro-test."""

    submitted_monotonic: float
    run_micro_test: bool
    future: Future[tuple[VLMRuntimeStatus, RetractionVLMHealthResult | None]]


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
        self.declare_parameter(
            "retraction_service_name", RETRACTION_SERVICE_DEFAULT_NAME
        )
        self.declare_parameter(
            "robot_endpoint_source",
            os.environ.get("TASKPLANNER_DEBUG_ROBOT_ENDPOINT_SOURCE", "external"),
        )
        self.declare_parameter("virtual_robot_enabled", True)
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
        self.declare_parameter(
            "retraction_voice_interpreter_mode",
            os.environ.get(
                "RETRACTOR_VOICE_INTERPRETER_MODE", "vlm_with_fallback"
            ),
        )
        self.declare_parameter(
            "retraction_voice_vlm_base_url",
            os.environ.get("RETRACTOR_VOICE_VLM_BASE_URL", "").strip()
            or os.environ.get("VLM_BASE_URL", "").strip()
            or "http://127.0.0.1:8080",
        )
        self.declare_parameter(
            "retraction_voice_vlm_model_id",
            os.environ.get("RETRACTOR_VOICE_VLM_MODEL_ID", "").strip()
            or os.environ.get("VLM_MODEL_ID", "").strip()
            or "qwen3.6-35b-a3b",
        )
        self.declare_parameter(
            "retraction_voice_vlm_api_key",
            os.environ.get("RETRACTOR_VOICE_VLM_API_KEY", "").strip()
            or os.environ.get("VLM_API_KEY", "").strip(),
        )
        self.declare_parameter(
            "retraction_voice_vlm_timeout_sec",
            float(os.environ.get("RETRACTOR_VOICE_VLM_TIMEOUT_SEC", "2.0")),
        )
        self.declare_parameter(
            "retraction_voice_vlm_probe_interval_sec",
            float(os.environ.get("RETRACTOR_VOICE_VLM_PROBE_INTERVAL_SEC", "15.0")),
        )
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
        requested_retraction_voice_interpreter_mode = str(
            self.get_parameter("retraction_voice_interpreter_mode").value
        ).strip().lower()
        if requested_retraction_voice_interpreter_mode not in {
            "deterministic",
            "vlm_with_fallback",
        }:
            raise ValueError(
                "retraction_voice_interpreter_mode must be deterministic or "
                "vlm_with_fallback"
            )
        self._retraction_voice_interpreter_mode = (
            requested_retraction_voice_interpreter_mode
        )
        self._retraction_voice_vlm_base_url = str(
            self.get_parameter("retraction_voice_vlm_base_url").value
        ).strip()
        self._retraction_voice_vlm_model_id = str(
            self.get_parameter("retraction_voice_vlm_model_id").value
        ).strip()
        retraction_voice_vlm_api_key = str(
            self.get_parameter("retraction_voice_vlm_api_key").value
        ).strip()
        retraction_voice_vlm_timeout_sec = float(
            self.get_parameter("retraction_voice_vlm_timeout_sec").value
        )
        self._retraction_voice_interpreter = TextOnlyRetractionVLMInterpreter(
            base_url=self._retraction_voice_vlm_base_url,
            model_id=self._retraction_voice_vlm_model_id,
            api_key=retraction_voice_vlm_api_key,
            timeout_sec=retraction_voice_vlm_timeout_sec,
        )
        self._vlm_runtime = FixedVLMRuntimeClient(
            base_url=self._retraction_voice_vlm_base_url,
            model_id=self._retraction_voice_vlm_model_id,
            api_key=retraction_voice_vlm_api_key,
            timeout_sec=retraction_voice_vlm_timeout_sec,
        )
        self._vlm_probe_interval_sec = max(
            2.0,
            float(
                self.get_parameter(
                    "retraction_voice_vlm_probe_interval_sec"
                ).value
            ),
        )
        self._vlm_status: dict[str, Any] = {
            "base_url": self._retraction_voice_vlm_base_url,
            "model_id": self._retraction_voice_vlm_model_id,
            "manager_reachable": False,
            "catalog_reachable": False,
            "load_state": "not_checked",
            "loaded": False,
            "available": False,
            "runtime_managed": False,
            "detail": "waiting_for_vlm_probe",
            "last_probe_monotonic": 0.0,
            "micro_test": {
                "state": "not_checked",
                "transcript": "리트렉터 직접 가르치기 모드 켜줘",
                "interpretation": {},
                "latency_ms": None,
                "error": "",
            },
        }
        self._vlm_observation_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="integration_debug_vlm_observation",
        )
        self._pending_vlm_observation: PendingDebugVLMObservation | None = None
        self._last_vlm_observation_submitted_monotonic = 0.0
        self._vlm_explicit_probe_requested = False
        self._retraction_voice_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="integration_debug_retractor_voice_vlm",
        )
        self._pending_retraction_voice_interpretation: (
            PendingDebugRetractionInterpretation | None
        ) = None
        self._config = load_config(config_path)
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
        self._acknowledged_blocked_nodes: set[str] = set()
        self._fault_locked = False
        self._last_heartbeat_monotonic = 0.0
        self._last_error = ""
        self._active_route = ""
        self._active_command_id = ""
        self._active_goal_handle: Any | None = None
        self._action_status: dict[str, Any] = self._idle_action_status()
        self._voice_auto_execute = False
        # This is deliberately independent of USB microphone ownership.  It
        # gates only retractor normalization and dispatch of final strings that
        # have passed the shared speech admission boundary.
        self._retraction_voice_auto_dispatch = False
        # Incremented whenever voice dispatch authority is revoked or its mode
        # is changed.  Async interpretations carry the generation that was
        # current at submission so an old result cannot dispatch after a
        # buttons-only -> voice-enabled toggle cycle.
        self._retraction_voice_generation = 0
        self._retraction_state = RetractionState.IDLE
        self._last_retraction_interpretation = self._retraction_interpretation(
            "", normalize_retractor_command("", self._retraction_state)
        )
        self._last_retraction_rejection_reason = ""
        self._last_retraction_voice_dispatch_text = ""
        self._last_retraction_voice_dispatch_monotonic = 0.0
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
        self._operational_running = False
        self._operational_execution_state = "unknown"
        self._operational_active_robot_task_id = ""
        self._operational_robot_state = "unknown"
        self._operational_cleaner_busy = False
        self._restart_scheduled = False
        self._session_dir = run_root / "debug" / self._session_id
        self._session_dir.mkdir(parents=True, exist_ok=True)
        self._event_log_path = self._session_dir / "events.jsonl"

        asr_config = dict(self._config.get("asr", {}))
        self._asr_topic = str(
            asr_config.get("topic", "/sensors/surgeon/sentence")
        ).strip()
        self._voice_request_topic = str(
            dict(self._config.get("voice", {})).get(
                "request_topic", "/surgery/audio/request_text"
            )
        ).strip()
        if not self._voice_request_topic or self._voice_request_topic == self._asr_topic:
            raise ValueError(
                "voice request topic must be distinct from the raw ASR topic"
            )
        self._asr_sentence_pub: Any | None = None
        # A Debug ASR session may coexist with the operational runtime for
        # monitoring, but its graph-visible sentence publisher must never make
        # the live preflight pass before Puzzle ASR is actually connected.
        self._asr_capture_requested = False
        self._manual_sentence_pub: Any | None = None
        self._asr = AsrMicrophoneRuntime(
            default_url=os.environ.get(
                "PUZZLE_ASR_URL",
                str(
                    asr_config.get(
                        "default_server_url",
                        "wss://arpa.worker-02.puzzle-ai.com",
                    )
                ),
            ),
            topic=self._asr_topic,
            output_dir=self._session_dir / "asr",
            capture_lock_path=os.environ.get(
                "TASKPLANNER_ASR_CAPTURE_LOCK",
                "/taskplanner-runs/asr/microphone.lock",
            ),
        )
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
                        "https://dev.puzzle-ai.com:6627/api/v1/surgery/img_texts",
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
                        "https://dev.puzzle-ai.com:6627/api/v1/surgery/img_texts",
                    ],
                )
            ),
            timeout_sec=float(record_config.get("timeout_sec", 35.0)),
        )

        self._status_pub = self.create_publisher(
            String, "/integration/debug/status", 10
        )
        self._event_pub = self.create_publisher(
            String, "/integration/debug/events", 50
        )
        self._readiness_pub = self.create_publisher(
            String, "/integration/debug/readiness", 10
        )
        self._command_service = self.create_service(
            IntegrationDebugCommand,
            "/integration/debug/command",
            self._handle_command,
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
        self._readiness_service = self.create_service(
            Trigger,
            "/integration/debug/check_readiness",
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

        self._external_tool_client = ActionClient(
            self,
            ExecuteToolHandover,
            TOOL_HANDOVER_DEFAULT_NAME,
            callback_group=self._callback_group,
        )
        self._virtual_tool_client = ActionClient(
            self,
            ExecuteToolHandover,
            self._virtual_tool_handover_name,
            callback_group=self._callback_group,
        )
        self._external_retraction_client = self.create_client(
            ExecuteRetractionCommand,
            self._external_retraction_service_name,
            callback_group=self._callback_group,
        )
        self._virtual_retraction_client = self.create_client(
            ExecuteRetractionCommand,
            self._virtual_retraction_service_name,
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
        self.create_timer(
            0.25,
            self._maintain_vlm_observation,
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
        self._voice_auto_execute = False
        self._retraction_voice_auto_dispatch = False
        self._retraction_voice_generation += 1
        self._acknowledged_blocked_nodes.clear()

    @staticmethod
    def _retraction_interpretation(
        transcript: str,
        normalized: NormalizedRetractionCommand,
        *,
        interpreter_source: str = "shared_deterministic",
        vlm_invoked: bool = False,
        detail: str = "",
    ) -> dict[str, Any]:
        """Serialize one grounded text interpretation for Debug status."""

        return {
            "transcript": str(transcript),
            "command": normalized.command.value if normalized.command else None,
            "target_side": normalized.target_side.value,
            "distance_m": float(normalized.distance_m),
            "confidence": float(normalized.confidence),
            "reason": str(normalized.reason),
            "interpreter_source": str(interpreter_source),
            "vlm_invoked": bool(vlm_invoked),
            "detail": str(detail),
        }

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

    def _apply_retraction_voice_interpretation(
        self,
        transcript: str,
        interpretation: RetractionVoiceInterpretation,
        *,
        expected_state: RetractionState | None = None,
        expected_voice_generation: int | None = None,
    ) -> None:
        """Gate and dispatch one completed, locally grounded interpretation."""

        now = time.monotonic()
        normalized = interpretation.normalized
        serialized = self._retraction_interpretation(
            transcript,
            normalized,
            interpreter_source=interpretation.interpreter_source,
            vlm_invoked=interpretation.vlm_invoked,
            detail=interpretation.detail,
        )
        retraction_service_ready = self._retraction_client.service_is_ready()
        should_dispatch = False
        with self._lock:
            self._last_retraction_interpretation = serialized
            if (
                expected_voice_generation is not None
                and expected_voice_generation
                != self._retraction_voice_generation
            ):
                self._last_retraction_rejection_reason = (
                    "retraction_voice_authority_changed_while_interpreting"
                )
            elif (
                expected_state is not None
                and expected_state != self._retraction_state
            ):
                self._last_retraction_rejection_reason = (
                    "retraction_state_changed_while_interpreting"
                )
            elif normalized.command is None:
                self._last_retraction_rejection_reason = normalized.reason
            elif not self._retraction_voice_auto_dispatch:
                self._last_retraction_rejection_reason = "voice_mode_buttons_only"
            elif not self._armed:
                self._last_retraction_rejection_reason = "manual_control_not_armed"
            elif self._active_command_id:
                self._last_retraction_rejection_reason = (
                    "retraction_command_in_flight"
                )
            elif not retraction_service_ready:
                self._last_retraction_rejection_reason = (
                    "retraction_service_unavailable"
                )
            elif normalized.command not in allowed_retractor_commands(
                self._retraction_state
            ):
                self._last_retraction_rejection_reason = (
                    "retraction_command_not_allowed_in_debug_state"
                )
            elif (
                transcript != self._last_retraction_voice_dispatch_text
                or now - self._last_retraction_voice_dispatch_monotonic > 2.0
            ):
                should_dispatch = True
                self._last_retraction_voice_dispatch_text = transcript
                self._last_retraction_voice_dispatch_monotonic = now
                self._last_retraction_rejection_reason = ""

        self._record(
            "retraction_voice_interpretation",
            {"interpretation": serialized},
        )
        if not should_dispatch or normalized.command is None:
            return
        retraction_payload = {
            "command": normalized.command.value,
            "target_side": normalized.target_side.value,
            "distance_m": normalized.distance_m,
        }
        accepted, command_id, message = self._dispatch_action(
            "retraction_command", retraction_payload, source="voice"
        )
        if not accepted:
            with self._lock:
                self._last_retraction_rejection_reason = message
        self._record(
            "retraction_voice_dispatch",
            {
                "accepted": accepted,
                "command_id": command_id,
                "message": message,
                "interpretation": serialized,
            },
        )

    def _submit_retraction_voice_interpretation(
        self,
        transcript: str,
        current_state: RetractionState,
        voice_generation: int | None = None,
    ) -> None:
        """Submit one non-blocking VLM request or use the shared normalizer."""

        deterministic = normalize_retractor_command(transcript, current_state)
        if voice_generation is None:
            with self._lock:
                voice_generation = self._retraction_voice_generation
        if (
            getattr(self, "_retraction_voice_interpreter_mode", "deterministic")
            != "vlm_with_fallback"
            or not getattr(self, "_retraction_voice_auto_dispatch", False)
        ):
            IntegrationDebugNode._apply_retraction_voice_interpretation(
                self,
                transcript,
                RetractionVoiceInterpretation(
                    normalized=deterministic,
                    interpreter_source="shared_deterministic",
                    vlm_invoked=False,
                    detail="deterministic_normalizer",
                ),
                expected_state=current_state,
                expected_voice_generation=voice_generation,
            )
            return

        interpreter = getattr(self, "_retraction_voice_interpreter", None)
        executor = getattr(self, "_retraction_voice_executor", None)
        if interpreter is None or executor is None:
            IntegrationDebugNode._apply_retraction_voice_interpretation(
                self,
                transcript,
                RetractionVoiceInterpretation(
                    normalized=deterministic,
                    interpreter_source="deterministic_fallback",
                    vlm_invoked=False,
                    detail="text_vlm_runtime_unavailable",
                ),
                expected_state=current_state,
                expected_voice_generation=voice_generation,
            )
            return
        submit_error: Exception | None = None
        with self._lock:
            pending = getattr(
                self, "_pending_retraction_voice_interpretation", None
            )
            if pending is not None:
                self._last_retraction_interpretation = (
                    self._retraction_interpretation(
                        transcript,
                        deterministic,
                        interpreter_source="text_vlm_busy",
                        vlm_invoked=False,
                        detail="previous_text_vlm_request_pending",
                    )
                )
                self._last_retraction_rejection_reason = (
                    "retraction_interpreter_busy"
                )
                return
            try:
                future = executor.submit(
                    interpreter.interpret,
                    transcript,
                    current_state,
                )
            except Exception as exc:  # pragma: no cover - executor failure
                submit_error = exc
            else:
                self._pending_retraction_voice_interpretation = (
                    PendingDebugRetractionInterpretation(
                        transcript=transcript,
                        current_state=current_state,
                        voice_generation=voice_generation,
                        submitted_monotonic=time.monotonic(),
                        future=future,
                    )
                )
                self._last_retraction_interpretation = (
                    self._retraction_interpretation(
                        transcript,
                        deterministic,
                        interpreter_source="text_vlm_pending",
                        vlm_invoked=False,
                        detail="text_vlm_request_submitted",
                    )
                )
                self._last_retraction_rejection_reason = ""
        if submit_error is not None:
            IntegrationDebugNode._apply_retraction_voice_interpretation(
                self,
                transcript,
                RetractionVoiceInterpretation(
                    normalized=deterministic,
                    interpreter_source="deterministic_fallback",
                    vlm_invoked=False,
                    detail=(
                        "text_vlm_submit_error:"
                        f"{type(submit_error).__name__}"
                    ),
                ),
                expected_state=current_state,
                expected_voice_generation=voice_generation,
            )
            return
        self._record(
            "retraction_voice_interpreter_submitted",
            {
                "interpreter_source": "text_vlm_pending",
                "vlm_invoked": False,
            },
        )

    def _drain_retraction_voice_interpretation(self) -> None:
        pending = getattr(self, "_pending_retraction_voice_interpretation", None)
        if pending is None or not pending.future.done():
            return
        with self._lock:
            if self._pending_retraction_voice_interpretation is not pending:
                return
            self._pending_retraction_voice_interpretation = None
        try:
            interpretation = pending.future.result()
        except Exception as exc:  # pragma: no cover - executor boundary
            interpretation = RetractionVoiceInterpretation(
                normalized=normalize_retractor_command(
                    pending.transcript, pending.current_state
                ),
                interpreter_source="deterministic_fallback",
                vlm_invoked=False,
                detail=f"text_vlm_executor_error:{type(exc).__name__}",
            )
        IntegrationDebugNode._apply_retraction_voice_interpretation(
            self,
            pending.transcript,
            interpretation,
            expected_state=pending.current_state,
            expected_voice_generation=pending.voice_generation,
        )

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
        # The Debug monitor also receives structured JSON status topics.  They
        # are not speech: do not parse them as voice commands, overwrite the
        # last surgeon sentence, or append the full JSON every second to the
        # recent-event snapshot.
        if topic != getattr(self, "_voice_request_topic", self._asr_topic):
            return
        parsed = parse_voice_command(text, dict(self._config.get("voice", {})))
        with self._lock:
            self._last_sentence = text
            self._last_voice_parse = parsed.as_dict()
            retraction_state = self._retraction_state
            retraction_voice_generation = self._retraction_voice_generation
            retraction_voice_enabled = bool(
                self._retraction_voice_auto_dispatch
            )
            # The legacy generic voice router continues to own tool handover,
            # but it must never bypass the dedicated retractor voice gate.
            should_generic_dispatch = (
                self._voice_auto_execute
                and self._armed
                and not self._active_command_id
                and parsed.matched
                and parsed.operation != "retraction_command"
                and (
                    text != self._last_voice_dispatch_text
                    or now - self._last_voice_dispatch_monotonic > 2.0
                )
            )
            if should_generic_dispatch:
                self._last_voice_dispatch_text = text
                self._last_voice_dispatch_monotonic = now

        event_payload: dict[str, Any] = {
            "topic": topic,
            "text": text,
            "parse": parsed.as_dict(),
            "retraction_voice_enabled": retraction_voice_enabled,
            "retraction_parse": None,
        }
        if retraction_voice_enabled:
            deterministic_preview = normalize_retractor_command(
                text, retraction_state
            )
            event_payload["retraction_parse"] = self._retraction_interpretation(
                text,
                deterministic_preview,
                interpreter_source=(
                    "text_vlm_pending"
                    if getattr(
                        self,
                        "_retraction_voice_interpreter_mode",
                        "deterministic",
                    )
                    == "vlm_with_fallback"
                    else "shared_deterministic"
                ),
                vlm_invoked=False,
            )
        self._record(
            "admitted_voice_request_received",
            event_payload,
        )
        if retraction_voice_enabled:
            IntegrationDebugNode._submit_retraction_voice_interpretation(
                self,
                text,
                retraction_state,
                retraction_voice_generation,
            )
        if should_generic_dispatch and parsed.payload is not None:
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

    def _on_heartbeat(self, msg: String) -> None:
        if str(msg.data).strip() != self._session_id:
            return
        with self._lock:
            self._last_heartbeat_monotonic = time.monotonic()

    def _on_operational_state(self, msg: SimulationState) -> None:
        with self._lock:
            self._operational_state_received = True
            self._operational_state_received_monotonic = time.monotonic()
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
        }

    def _blocked_nodes(self) -> list[str]:
        detected = self._detected_planner_nodes()
        if not self._network_locked_to_runtime:
            return detected
        operational = self._operational_runtime_status()
        if operational["stopped"]:
            return []
        if detected:
            return detected
        if not operational["received"] or not operational["fresh"]:
            return ["simulation_runtime_state_unavailable"]
        return ["simulation_runtime_active"]

    def _manual_write_block_reason(self) -> str:
        """Evaluate current graph/session state immediately before a ROS write."""

        blocked = self._blocked_nodes()
        with self._lock:
            return manual_write_block_reason(
                armed=self._armed,
                fault_locked=self._fault_locked,
                blocked_nodes=blocked,
                planner_coexistence_allowed=self._planner_coexistence_allowed,
                acknowledged_blocked_nodes=self._acknowledged_blocked_nodes,
            )

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

    def _observe_vlm_runtime(
        self,
        *,
        run_micro_test: bool = False,
    ) -> tuple[VLMRuntimeStatus, RetractionVLMHealthResult | None]:
        runtime = self._vlm_runtime.refresh()
        health = (
            run_retraction_vlm_health_probe(self._retraction_voice_interpreter)
            if run_micro_test and runtime.loaded
            else None
        )
        return runtime, health

    def _apply_vlm_observation(
        self,
        runtime: VLMRuntimeStatus,
        health: RetractionVLMHealthResult | None,
    ) -> None:
        now = time.monotonic()
        completed_micro_test = (
            {
                "state": "passed" if health.healthy else "failed",
                "transcript": "리트렉터 직접 가르치기 모드 켜줘",
                "interpretation": {
                    "command": health.actual_command or None,
                    "interpreter_source": health.interpreter_source,
                    "vlm_invoked": health.vlm_invoked,
                    "detail": health.detail,
                },
                "latency_ms": round(health.latency_ms, 3),
                "error": health.error_type,
            }
            if health is not None
            else None
        )
        with self._lock:
            micro_test = (
                completed_micro_test
                if completed_micro_test is not None
                else dict(self._vlm_status.get("micro_test", {}))
            )
            self._vlm_status = {
                "base_url": self._retraction_voice_vlm_base_url,
                "model_id": self._retraction_voice_vlm_model_id,
                **runtime.as_dict(),
                "last_probe_monotonic": now,
                "micro_test": micro_test,
            }

    def _submit_vlm_observation(
        self,
        *,
        force: bool = False,
        run_micro_test: bool = False,
    ) -> bool:
        now = time.monotonic()
        with self._lock:
            if self._pending_vlm_observation is not None:
                if (
                    force
                    and run_micro_test
                    and not self._pending_vlm_observation.run_micro_test
                ):
                    self._vlm_explicit_probe_requested = True
                return False
            if (
                not force
                and self._last_vlm_observation_submitted_monotonic > 0.0
                and now - self._last_vlm_observation_submitted_monotonic
                < self._vlm_probe_interval_sec
            ):
                return False
        try:
            future = self._vlm_observation_executor.submit(
                self._observe_vlm_runtime,
                run_micro_test=run_micro_test,
            )
        except Exception as exc:
            with self._lock:
                self._vlm_status["detail"] = (
                    f"vlm_probe_submit_error:{type(exc).__name__}"
                )
            return False
        with self._lock:
            self._pending_vlm_observation = PendingDebugVLMObservation(
                submitted_monotonic=now,
                run_micro_test=run_micro_test,
                future=future,
            )
            self._last_vlm_observation_submitted_monotonic = now
            self._vlm_status["detail"] = "vlm_probe_pending"
        return True

    def _maintain_vlm_observation(self) -> None:
        with self._lock:
            pending = self._pending_vlm_observation
        if pending is None:
            self._submit_vlm_observation()
            return
        if not pending.future.done():
            return
        try:
            runtime, health = pending.future.result()
        except Exception as exc:
            runtime = VLMRuntimeStatus(
                manager_reachable=False,
                catalog_reachable=False,
                load_state="error",
                loaded=False,
                available=False,
                runtime_managed=False,
                detail=f"vlm_probe_error:{type(exc).__name__}",
            )
            health = None
        with self._lock:
            if self._pending_vlm_observation is not pending:
                return
            self._pending_vlm_observation = None
            explicit_probe_requested = self._vlm_explicit_probe_requested
            self._vlm_explicit_probe_requested = False
        self._apply_vlm_observation(runtime, health)
        if explicit_probe_requested:
            self._submit_vlm_observation(
                force=True,
                run_micro_test=True,
            )

    def _vlm_status_snapshot(self, now: float | None = None) -> dict[str, Any]:
        current = time.monotonic() if now is None else float(now)
        with self._lock:
            payload = dict(self._vlm_status)
            payload["micro_test"] = dict(
                self._vlm_status.get("micro_test", {})
            )
            pending = self._pending_vlm_observation is not None
            explicit_probe_queued = self._vlm_explicit_probe_requested
        last_probe = float(payload.pop("last_probe_monotonic", 0.0))
        payload["last_probe_age_sec"] = (
            round(max(0.0, current - last_probe), 3) if last_probe else None
        )
        payload["probe_pending"] = pending
        payload["explicit_probe_queued"] = explicit_probe_queued
        return payload

    def _handle_vlm_command(
        self,
        operation: str,
        payload: dict[str, Any],
    ) -> tuple[bool, str, str, dict[str, Any]]:
        """Model diagnostics never dispatch ROS commands or change robot authority."""

        if operation == "vlm_refresh":
            submitted = self._submit_vlm_observation(
                force=True,
                run_micro_test=True,
            )
            return (
                True,
                "",
                "VLM refresh submitted" if submitted else "VLM refresh already pending",
                self._vlm_status_snapshot(),
            )
        if operation == "vlm_load":
            runtime = self._vlm_runtime.load()
            with self._lock:
                stale_probe = self._pending_vlm_observation
                self._pending_vlm_observation = None
                self._vlm_explicit_probe_requested = False
            if stale_probe is not None:
                stale_probe.future.cancel()
            self._apply_vlm_observation(runtime, None)
            self._last_vlm_observation_submitted_monotonic = 0.0
            accepted = runtime.load_state in {"loading", "loaded"}
            return (
                accepted,
                "",
                runtime.detail or runtime.load_state,
                self._vlm_status_snapshot(),
            )
        if operation == "vlm_interpret":
            text = str(payload.get("text", "")).strip()
            if not text:
                raise ValueError("text is required")
            if len(text) > 512:
                raise ValueError("text must be at most 512 characters")
            state_value = str(payload.get("state", "idle")).strip().lower()
            try:
                state = RetractionState(state_value)
            except ValueError as exc:
                raise ValueError("state is not a supported retraction state") from exc
            started = time.monotonic()
            interpretation = self._retraction_voice_interpreter.interpret(
                text,
                state,
            )
            latency_ms = (time.monotonic() - started) * 1_000.0
            result = self._retraction_interpretation(
                text,
                interpretation.normalized,
                interpreter_source=interpretation.interpreter_source,
                vlm_invoked=interpretation.vlm_invoked,
                detail=interpretation.detail,
            )
            result.update(
                {
                    "state": "completed",
                    "latency_ms": round(max(0.0, latency_ms), 3),
                    "dispatch_performed": False,
                }
            )
            return (
                True,
                "",
                "VLM interpretation completed without ROS dispatch",
                result,
            )
        return False, "", "unknown VLM debug operation", {}

    def _robot_source_snapshot(self) -> dict[str, Any]:
        external_bed_ready, _ = self._bed_robot_arm_source_ready("external")
        virtual_bed_ready, _ = self._bed_robot_arm_source_ready("virtual")
        selected = self._robot_endpoint_source
        external_tool_ready = self._external_tool_client.server_is_ready()
        virtual_tool_ready = self._virtual_tool_client.server_is_ready()
        external_retraction_ready = (
            self._external_retraction_client.service_is_ready()
        )
        virtual_retraction_ready = (
            self._virtual_retraction_client.service_is_ready()
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
                "tool_handover": TOOL_HANDOVER_DEFAULT_NAME,
                "retraction_service": self._external_retraction_service_name,
                "bed_status": BED_ROBOT_STATUS_DEFAULT_TOPIC,
            },
            "virtual": {
                "tool_handover": self._virtual_tool_handover_name,
                "retraction_service": self._virtual_retraction_service_name,
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
                self._virtual_retraction_service_name
                if source == "virtual"
                else self._external_retraction_service_name
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
            self._retraction_voice_auto_dispatch = False
            self._retraction_voice_generation += 1
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
            if operation == "apply_network_settings":
                accepted, command_id, message, result = self._apply_network_settings(
                    payload
                )
            elif operation == "ping_host":
                accepted, command_id, message, result = self._ping_host(payload)
            elif operation.startswith("vlm_"):
                accepted, command_id, message, result = self._handle_vlm_command(
                    operation, payload
                )
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
        if operation in {"arm", "disarm", "reset_fault"}:
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
        with self._auxiliary_lock:
            self._asr_capture_requested = False
            self._destroy_asr_publisher()
            # Removing only the ROS publisher would leave privacy-sensitive
            # microphone audio streaming to the external ASR server invisibly.
            # The runtime stop is idempotent, so every authority-revocation path
            # may safely enforce it here.
            self._asr.stop_async()

    def _handle_asr_command(
        self, operation: str, payload: dict[str, Any]
    ) -> tuple[bool, str, str, dict[str, Any]]:
        if operation == "asr_refresh_devices":
            devices = self._asr.refresh_devices()
            return True, "", f"found {len(devices)} microphone input device(s)", {
                "devices": devices
            }
        if operation == "asr_start":
            if self._network_locked_to_runtime:
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
                        server_url=payload.get("server_url"),
                    )
                except Exception:
                    self._asr_capture_requested = False
                    self._destroy_asr_publisher()
                    raise
                self._asr_capture_requested = True
                self._sync_asr_publisher(
                    bool(self._asr.snapshot().get("connected", False))
                )
            return True, "", "USB microphone ASR session started", self._asr.snapshot()
        if operation == "asr_stop":
            with self._auxiliary_lock:
                self._asr_capture_requested = False
                self._destroy_asr_publisher()
                self._asr.stop_async()
                snapshot = self._asr.snapshot()
            return True, "", "USB microphone ASR stop requested", snapshot
        return False, "", "unknown ASR debug operation", {}

    def _handle_surgery_record_command(
        self, operation: str, payload: dict[str, Any]
    ) -> tuple[bool, str, str, dict[str, Any]]:
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

    def _configure_retraction_voice(
        self, payload: dict[str, Any]
    ) -> tuple[bool, str, str]:
        """Configure only the final-transcript dispatch gate for the retractor.

        This method deliberately does not inspect, start, stop, or otherwise
        acquire the USB ASR runtime.  The ASR tab remains the sole microphone
        capture owner; this switch is safe to change even while the Service is
        not discovered because it only controls future final transcripts.
        """

        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            return False, "", "enabled must be a boolean"
        if enabled:
            blocked_reason = self._manual_write_block_reason()
            if blocked_reason:
                if blocked_reason == "manual control is not armed":
                    blocked_reason = (
                        "arm manual control before enabling retraction voice dispatch"
                    )
                return False, "", blocked_reason
        with self._lock:
            if self._retraction_voice_auto_dispatch != enabled:
                self._retraction_voice_generation += 1
            self._retraction_voice_auto_dispatch = enabled
        return (
            True,
            "",
            "retraction final-transcript dispatch enabled"
            if enabled
            else "retraction final-transcript dispatch disabled",
        )

    def _drain_auxiliary_events(self) -> None:
        IntegrationDebugNode._drain_retraction_voice_interpretation(self)
        with self._auxiliary_lock:
            for event in self._asr.drain_events():
                event_type = str(event.get("type", "asr_event"))
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
                bool(self._asr.snapshot().get("connected", False))
            )
            for event in self._surgery_record.drain_events():
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
            blocked = self._blocked_nodes()
            acknowledged: list[str] = []
            with self._lock:
                if self._fault_locked:
                    return False, "", "reset the fault lock before arming"
            if blocked:
                if not self._planner_coexistence_allowed:
                    return False, "", "full Taskplanner nodes are active: " + ", ".join(blocked)
                acknowledged = validate_planner_coexistence_acknowledgement(
                    payload, blocked
                )
                if self._blocked_nodes() != blocked:
                    return (
                        False,
                        "",
                        "planner node set changed; refresh the status and acknowledge it again",
                    )
            with self._lock:
                if self._fault_locked:
                    return False, "", "reset the fault lock before arming"
                self._armed = True
                self._acknowledged_blocked_nodes = set(acknowledged)
                self._last_heartbeat_monotonic = now
                self._last_error = ""
            if acknowledged:
                return (
                    True,
                    "",
                    "manual control armed with planner coexistence acknowledgement",
                )
            return True, "", "manual control armed"
        if operation == "disarm":
            with self._lock:
                self._disarm_locked()
            with self._auxiliary_lock:
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
        if operation == "cancel_active":
            return self._request_cancel()
        if operation == "configure_voice":
            enabled = bool(payload.get("enabled", False))
            if enabled:
                blocked_reason = self._manual_write_block_reason()
                if blocked_reason:
                    if blocked_reason == "manual control is not armed":
                        blocked_reason = (
                            "arm manual control before enabling voice dispatch"
                        )
                    return False, "", blocked_reason
            with self._lock:
                self._voice_auto_execute = enabled
            return True, "", "voice auto-dispatch enabled" if enabled else "voice auto-dispatch disabled"
        if operation == "configure_retraction_voice":
            return self._configure_retraction_voice(payload)
        if operation == "publish_voice_command":
            blocked_reason = self._manual_write_block_reason()
            if blocked_reason:
                return False, "", blocked_reason
            text = str(payload.get("text", "")).strip()
            if not text:
                return False, "", "text is required"
            if len(text) > 1000:
                return False, "", "text must be at most 1000 characters"
            publisher = self._ensure_manual_sentence_publisher()
            message = String()
            message.data = text
            publisher.publish(message)
            return True, "", f"published manual sentence on {self._asr_topic}"
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
        if self._asr.snapshot().get("state") not in {
            "STOPPED",
            "ERROR",
            "UNAVAILABLE",
        }:
            return False, "", "stop the USB ASR session before changing DDS settings", {}
        if self._surgery_record.snapshot().get("state") == "SUBMITTING":
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
        # Reject the old Debug API before the arm/interlock check so callers get
        # an explicit migration error rather than a misleading safety error.
        if operation == "retraction_adjustment":
            return (
                False,
                "",
                "legacy direction, axis, and multi-retractor adjustment fields are "
                "unsupported; use retraction_command with target_side and distance_m",
            )
        if operation == "tool_change":
            return (
                False,
                "",
                "legacy arm_id and target_tool_id fields are unsupported; use "
                "retraction_command with command change_tool",
            )
        retraction_command = (
            validate_retraction_command(payload)
            if operation == "retraction_command"
            else None
        )
        normalized_retraction_command: RetractionCommand | None = None
        if retraction_command is not None:
            # ``validate_retraction_command`` owns the public wire vocabulary;
            # enum conversion makes the local admission-state check explicit.
            normalized_retraction_command = RetractionCommand(
                str(retraction_command["command"])
            )
        blocked_reason = self._manual_write_block_reason()
        if blocked_reason:
            return False, "", blocked_reason
        if operation == "tool_handover":
            with self._lock:
                if self._active_command_id:
                    return False, self._active_command_id, "another command is active"
            command_id = f"debug-{uuid4()}"
            mapped = validate_tool_handover(payload)
            if not self._tool_client.server_is_ready():
                tool_endpoint = (
                    self._virtual_tool_handover_name
                    if getattr(self, "_robot_endpoint_source", "external")
                    == "virtual"
                    else TOOL_HANDOVER_DEFAULT_NAME
                )
                return False, "", f"{tool_endpoint} Action server is unavailable"
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
        if operation == "retraction_command":
            assert retraction_command is not None
            assert normalized_retraction_command is not None
            if not self._retraction_client.service_is_ready():
                with self._lock:
                    self._last_retraction_rejection_reason = (
                        "retraction_service_unavailable"
                    )
                return (
                    False,
                    "",
                    f"{self._retraction_service_name} Service is unavailable",
                )
            command_id = f"debug-{uuid4()}"
            with self._lock:
                # The graph-level interlock was checked immediately above,
                # but arm/voice authority can change on another executor
                # thread while Service readiness is inspected.  Revalidate
                # the session-scoped gates atomically with reservation of the
                # one active command slot.
                if not self._armed:
                    return False, "", "manual control is not armed"
                if self._fault_locked:
                    return False, "", "reset the fault lock before manual control"
                if source == "voice" and not self._retraction_voice_auto_dispatch:
                    self._last_retraction_rejection_reason = (
                        "voice_mode_buttons_only"
                    )
                    return False, "", "retraction voice dispatch is disabled"
                if self._active_command_id:
                    return False, self._active_command_id, "another command is active"
                state_before_dispatch = self._retraction_state
                if normalized_retraction_command not in allowed_retractor_commands(
                    state_before_dispatch
                ):
                    self._last_retraction_rejection_reason = (
                        "retraction_command_not_allowed_in_debug_state"
                    )
                    return (
                        False,
                        "",
                        f"{normalized_retraction_command.value} is not allowed in "
                        f"Debug retraction state {state_before_dispatch.value}",
                    )
                # This is only ROS-message serialization, not a transport
                # write.  Keep it after the state guard so invalid direct
                # API/UI commands cannot even progress toward a Service call.
                request = self._build_retraction_service_request(
                    command_id, retraction_command
                )
                self._start_action_locked(
                    "retraction_service",
                    command_id,
                    source,
                    command=normalized_retraction_command.value,
                    response_semantics="admission",
                )
                self._last_retraction_rejection_reason = ""
            self._record(
                "command_started",
                {
                    "route": "retraction_service",
                    "command_id": command_id,
                    "source": source,
                    "robot_endpoint_source": getattr(
                        self, "_robot_endpoint_source", "external"
                    ),
                },
            )
            try:
                future = self._retraction_client.call_async(request)
            except Exception as exc:
                # ``call_async`` raising means the client could not enqueue
                # the request.  Release the reservation without advancing or
                # invalidating the local state; there was no admission result
                # to apply and no physical-completion claim is made.
                reason_code = f"service_submit_error:{type(exc).__name__}"
                with self._lock:
                    if self._active_command_id == command_id:
                        started = float(
                            self._action_status.get("started_monotonic", 0.0)
                        )
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
                                "elapsed_sec": max(
                                    0.0, time.monotonic() - started
                                ),
                                "last_update_monotonic": time.monotonic(),
                            }
                        )
                        self._active_route = ""
                        self._active_command_id = ""
                        self._active_goal_handle = None
                        self._last_retraction_rejection_reason = reason_code
                self._record(
                    "retraction_service_submit_failed",
                    {
                        "command_id": command_id,
                        "command": normalized_retraction_command.value,
                        "reason_code": reason_code,
                    },
                )
                return (
                    False,
                    "",
                    f"failed to submit retraction Service request ({reason_code})",
                )
            future.add_done_callback(
                lambda result: self._on_retraction_service_response(
                    command_id, result
                )
            )
            return True, command_id, "retraction Service request submitted"
        return False, "", "unsupported integration debug operation"

    def _route_server_ready(self, route: str) -> bool:
        if route == "tool_handover":
            return self._tool_client.server_is_ready()
        if route == "retraction_service":
            return self._retraction_client.service_is_ready()
        return False

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
        request.target_side = getattr(
            ExecuteRetractionCommand.Request,
            RETRACTION_TARGET_SIDE_CONSTANTS[str(mapped["target_side"])],
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
    ) -> None:
        with self._lock:
            self._start_action_locked(
                route,
                command_id,
                source,
                command=command,
                response_semantics=response_semantics,
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
        feedback = feedback_message.feedback
        with self._lock:
            if self._active_command_id != command_id:
                return
            if not self._action_status.get("recovery_required"):
                self._action_status["state"] = str(feedback.state or "executing")
            if hasattr(feedback, "progress"):
                self._action_status["progress"] = min(
                    1.0, max(0.0, float(feedback.progress))
                )
            self._action_status["last_update_monotonic"] = time.monotonic()

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
                if (
                    normalized_command is None
                    or normalized_command
                    not in allowed_retractor_commands(state_before_admission)
                ):
                    # The peer admitted a request that this local state cannot
                    # represent.  Do not invent a physical state; block future
                    # voice normalization until an operator resolves it.
                    self._retraction_state = RetractionState.UNKNOWN
                    self._last_retraction_rejection_reason = (
                        "admission_not_allowed_in_debug_state"
                    )
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
            pending_retraction_interpretation = getattr(
                self,
                "_pending_retraction_voice_interpretation",
                None,
            )
            voice = {
                "auto_execute": self._voice_auto_execute,
                "last_sentence": self._last_sentence,
                "last_parse": dict(self._last_voice_parse),
                "retraction": {
                    "mode": (
                        "voice_and_buttons"
                        if self._retraction_voice_auto_dispatch
                        else "buttons_only"
                    ),
                    "internal_state": retraction_state.value,
                    "interpreter_mode": getattr(
                        self,
                        "_retraction_voice_interpreter_mode",
                        "deterministic",
                    ),
                    "interpreter_pending": bool(
                        pending_retraction_interpretation
                    ),
                    "interpreter_pending_age_sec": (
                        round(
                            max(
                                0.0,
                                now
                                - (
                                    pending_retraction_interpretation
                                    .submitted_monotonic
                                ),
                            ),
                            3,
                        )
                        if pending_retraction_interpretation is not None
                        else None
                    ),
                    "allowed_commands": sorted(
                        command.value
                        for command in allowed_retractor_commands(retraction_state)
                    ),
                    # The client checks DDS readiness outside this lock below.
                    "service_ready": False,
                    "service_source": self._robot_endpoint_source,
                    "service_endpoint": self._retraction_service_name,
                    "in_flight": retraction_in_flight,
                    "last_interpretation": dict(
                        self._last_retraction_interpretation
                    ),
                    "last_rejection_reason": self._last_retraction_rejection_reason,
                },
            }
            armed = self._armed
            acknowledged_blocked_nodes = sorted(self._acknowledged_blocked_nodes)
            last_error = self._last_error
        voice["retraction"]["service_ready"] = bool(
            self._retraction_client.service_is_ready()
        )
        detected_planner_nodes = self._detected_planner_nodes()
        operational = self._operational_runtime_status()
        blocked = self._blocked_nodes()
        operational_runtime_is_stopped = bool(
            operational["stopped"]
            if self._network_locked_to_runtime
            else not detected_planner_nodes
        )
        manual_control_available = bool(
            operational_runtime_is_stopped
            and not blocked
            and not self._fault_locked
            and not self._active_command_id
        )
        try:
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
        endpoints = [
            {
                "name": "tool_handover",
                "endpoint": (
                    self._virtual_tool_handover_name
                    if self._robot_endpoint_source == "virtual"
                    else TOOL_HANDOVER_DEFAULT_NAME
                ),
                "kind": "action",
                "ready": self._tool_client.server_is_ready(),
                "source": self._robot_endpoint_source,
            },
            {
                "name": "retraction_service",
                "endpoint": self._retraction_service_name,
                "kind": "service",
                "ready": self._retraction_client.service_is_ready(),
                "source": self._robot_endpoint_source,
                "admission_only": True,
            },
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
        return {
            "schema": STATUS_SCHEMA,
            "stamp_sec": round(self.get_clock().now().nanoseconds / 1e9, 6),
            "session": {
                "session_id": self._session_id,
                "state": self._session_state(),
                "armed": armed,
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
                "manual_control_available": manual_control_available,
                "planner_coexistence_allowed": self._planner_coexistence_allowed,
                "action_watchdog": dict(self._action_watchdog_policy),
                "network": network,
            },
            "inputs": self._input_status_rows(now),
            "endpoints": endpoints,
            "action": action,
            "outputs": self._output_status_rows(now),
            "voice": voice,
            "vlm": self._vlm_status_snapshot(now),
            "virtual_robot": robot_source,
            "asr": self._asr.snapshot(),
            "surgery_record": self._surgery_record.snapshot(),
            "recent_events": recent_events,
        }

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
        asr = self._asr.snapshot()
        managed_asr_ready = bool(
            self._asr_capture_requested
            and self._asr_sentence_pub is not None
            and asr.get("state") == "LISTENING"
            and asr.get("connected")
        )
        bed_robot_ready, bed_robot_age_sec = self._bed_robot_arm_status_ready()
        checks = {
            "sentence_publisher": (
                sentence_external_publisher or managed_asr_ready
            ),
            "tool_handover_server": self._tool_client.server_is_ready(),
            "retraction_service": self._retraction_client.service_is_ready(),
            "bed_robot_arm_status": bed_robot_ready,
        }
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
        blocked = self._blocked_nodes()
        with self._lock:
            expired = (
                self._armed
                and self._last_heartbeat_monotonic > 0.0
                and now - self._last_heartbeat_monotonic > self._heartbeat_timeout_sec
            )
            acknowledged = sorted(self._acknowledged_blocked_nodes)
            planner_set_changed = self._armed and blocked != acknowledged
            if not expired and not planner_set_changed:
                return
            command_id = self._active_command_id
            self._disarm_locked()
            if planner_set_changed:
                self._last_error = (
                    "planner node set changed; manual control was disarmed: "
                    + ", ".join(blocked or ["none"])
                )
        if planner_set_changed:
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
                policy=self._action_watchdog_policy,
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
        with self._auxiliary_lock:
            self._asr_capture_requested = False
            self._destroy_asr_publisher()
            self._asr.close()
        pending = getattr(self, "_pending_retraction_voice_interpretation", None)
        if pending is not None:
            pending.future.cancel()
        self._pending_retraction_voice_interpretation = None
        executor = getattr(self, "_retraction_voice_executor", None)
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
        pending_vlm = getattr(self, "_pending_vlm_observation", None)
        if pending_vlm is not None:
            pending_vlm.future.cancel()
        self._pending_vlm_observation = None
        vlm_executor = getattr(self, "_vlm_observation_executor", None)
        if vlm_executor is not None:
            vlm_executor.shutdown(wait=False, cancel_futures=True)
        self._release_manual_publishers()
        self._drain_auxiliary_events()
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
        executor.shutdown()
        node.close()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
