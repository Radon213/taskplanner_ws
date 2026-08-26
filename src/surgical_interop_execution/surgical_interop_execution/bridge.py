"""Bridge internal Taskplanner commands onto focused public robot endpoints."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any
import json
import math
import threading
import time
import uuid

import rclpy
from action_msgs.msg import GoalStatus
from procedure_spec import load_bundle
from rclpy.callback_groups import ReentrantCallbackGroup
from rcl_interfaces.msg import SetParametersResult
from rclpy.action import ActionClient
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import (
    QoSDurabilityPolicy,
    QoSHistoryPolicy,
    QoSProfile,
    QoSReliabilityPolicy,
)
from surgical_interop_msgs.action import (
    ExecuteToolHandover,
)
from surgical_interop_msgs.msg import BedRobotArmStateArray
from surgical_interop_msgs.srv import ExecuteRetractionCommand
from surgical_msgs.msg import BedRobotArmGroupCommand, BedRobotArmGroupStatus
from surgical_msgs.msg import (
    ExecutionTrace,
    SimulationState,
    SkillCommand,
    SkillStatus,
    TwinEvent,
)
from surgical_msgs.srv import ControlSimulation, IntegrationDebugCommand
from std_msgs.msg import String
from std_srvs.srv import Trigger

from .direct_hand_ledger import (
    DurableDirectHandLedger,
    valid_procedure_run_id,
)
from .mappings import (
    DispatchLedger,
    InternalGroupCommand,
    InternalSkillCommand,
    MappingFailure,
    RETRACTION_COMMAND_ADJUST_RETRACTION,
    RETRACTION_COMMAND_STOP_RETRACTION,
    RETRACTION_TARGET_BOTH,
    RETRACTION_TARGET_LEFT,
    RETRACTION_TARGET_RIGHT,
    RETURN_UNUSED_PREPOSITION_ALIASES,
    RetractionCommandRequest,
    ToolHandoverRequest,
    map_group_command,
    map_skill_to_tool_handover,
    public_instrument_instance_id,
)
from .controller_contract import (
    EIR_NUC_CAPABILITY_POLICY_ID,
    EIR_NUC_EXTERNAL_CONTRACT_ID,
    EIR_NUC_VIRTUAL_CONTRACT_ID,
    VIRTUAL_EMULATOR_CAPABILITY_POLICY_ID,
    validate_source_stamp,
)
from .virtual_endpoints import (
    EXECUTION_ROUTE_COMMAND_SERVICE,
    EXECUTION_ROUTE_PREFLIGHT_ACK_SERVICE,
    EXECUTION_ROUTE_STATE_SCHEMA,
    EXECUTION_ROUTE_STATE_TOPIC,
    EXTERNAL_CONTROLLER_CONTRACT_TOPIC,
    EXTERNAL_ENDPOINT_SOURCE,
    EXTERNAL_RETRACTION_SERVICE_ENDPOINT,
    EXTERNAL_TOOL_HANDOVER_ENDPOINT,
    VIRTUAL_ENDPOINT_SOURCE,
    VIRTUAL_CONTROLLER_CONTRACT_TOPIC,
    VIRTUAL_RETRACTION_SERVICE_ENDPOINT,
    VIRTUAL_TOOL_HANDOVER_ENDPOINT,
    normalize_robot_endpoint_source,
    validate_endpoint_source,
)


_INTEGRATION_READINESS_SCHEMA = "taskplanner.integration_readiness.v1"
_EXECUTION_TRACE_TOPIC = "/surgery/execution_trace"
_EXECUTION_TRACE_TRANSPORTS = frozenset({"action", "service"})
_EXECUTION_TRACE_STAGES = frozenset(
    {
        "sent",
        "accepted",
        "rejected",
        "completed",
        "failed",
        "canceled",
        "unknown",
    }
)
_EXECUTION_TRACE_EVIDENCE = frozenset(
    {
        "submission_only",
        "goal_response",
        "controller_result",
        "service_admission_only",
        "response_unavailable",
        "response_invalid",
        "not_dispatched",
    }
)
_EXECUTION_TRACE_MAX_COMMAND_ID_CHARS = 128
_EXECUTION_TRACE_MAX_ROUTE_CHARS = 48
_EXECUTION_TRACE_MAX_ENDPOINT_CHARS = 192
_EXECUTION_TRACE_MAX_REASON_CHARS = 128
_EXECUTION_TRACE_REASON_CODE_CHARACTERS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.:-"
)
_EXECUTION_ROUTE_RESULT_MAX_CHARS = 1024
_EXECUTION_ROUTE_SAFE_STOPPED_STATES = frozenset(
    # Keep this equal to simulation_manager's terminal transition set.  A
    # completed/terminated procedure has no live execution path and must be
    # switchable after the same active-request and manager checks as idle.
    {"idle", "halted", "completed", "terminated"}
)
_EXECUTION_ROUTE_MANAGER_TRANSITION_SERVICE = "/simulation/check_transition_ready"
_EXECUTION_ROUTE_MANAGER_CONTROL_SERVICE = "/simulation/control"
_EXECUTION_ROUTE_MANAGER_CHECK_TIMEOUT_SEC = 3.0
_EXECUTION_ROUTE_RESET_TIMEOUT_SEC = 10.0
_EXECUTION_ROUTE_PREFLIGHT_ACK_TIMEOUT_SEC = 4.0


_TOOL_TRANSFER_FEEDBACK_STATES = frozenset(
    {
        ExecuteToolHandover.Feedback.STATE_MOVING_TO_SOURCE,
        ExecuteToolHandover.Feedback.STATE_GRASPING,
        ExecuteToolHandover.Feedback.STATE_MOVING_TO_TARGET,
        ExecuteToolHandover.Feedback.STATE_WAITING_FOR_TAKEOVER,
        ExecuteToolHandover.Feedback.STATE_PLACING,
        ExecuteToolHandover.Feedback.STATE_HOLDING,
        ExecuteToolHandover.Feedback.STATE_STOPPING,
        ExecuteToolHandover.Feedback.STATE_RETREATING,
        ExecuteToolHandover.Feedback.STATE_RECOVERING_TO_TRAY,
    }
)
_TOOL_TRANSFER_CANCEL_REASONS = frozenset(
    {
        ExecuteToolHandover.Result.REASON_CANCELED_SOURCE_UNCHANGED,
        ExecuteToolHandover.Result.REASON_CANCELED_RECOVERED_TO_TRAY,
    }
)
_TOOL_TRANSFER_FINAL_STATES = frozenset(
    {
        ExecuteToolHandover.Result.FINAL_COMPLETED,
        ExecuteToolHandover.Result.FINAL_CANCELED,
        ExecuteToolHandover.Result.FINAL_FAILED,
    }
)

_BED_ROBOT_ARM_STATES = frozenset(
    {
        "standby",
        "direct_teach",
        "retracting",
        "changing_tool",
        "moving_to_standby",
        "fault",
        "protective_stop",
        "unknown",
    }
)
_RETRACTOR_ROLE_INSTANCES = frozenset(
    {
        "left_malleable",
        "right_malleable",
        "left_army_navy",
        "right_army_navy",
        "army_navy",
    }
)
_BED_ROBOT_PROCEDURE_LAYOUTS = {
    "thyroidectomy": frozenset({"army_navy"}),
    "nephrectomy": frozenset({"left_malleable", "right_malleable"}),
    "inguinal_hernia_repair": frozenset(
        {"left_army_navy", "right_army_navy"}
    ),
}


def procedure_retraction_distance_limit_mm(
    procedure_spec: object,
    *,
    configured_limit_mm: float,
) -> float:
    """Return the stricter of the bridge and loaded procedure limits.

    The execution bridge is the final public boundary. A generic deployment
    parameter must never loosen the active procedure's authored distance
    policy, even when an upstream planner has already validated the command.
    """

    configured = float(configured_limit_mm)
    if not math.isfinite(configured) or configured <= 0.0:
        raise ValueError("max_retraction_distance_mm must be positive and finite")
    getter = getattr(procedure_spec, "get_bed_robot_arm_group_spec", None)
    group_spec = getter() if callable(getter) else None
    try:
        procedure_limit = float(getattr(group_spec, "max_distance_mm", 0.0))
    except (TypeError, ValueError):
        procedure_limit = 0.0
    if math.isfinite(procedure_limit) and procedure_limit > 0.0:
        return min(configured, procedure_limit)
    return configured


@dataclass(slots=True)
class ActiveAction:
    route: str
    command: InternalSkillCommand | InternalGroupCommand
    goal_handle: Any | None = None
    cancelled: bool = False
    dispatched: bool = False
    semantic_leg: tuple[str, str] | None = None
    dispatch_epoch: int = 0


@dataclass(slots=True)
class ActiveService:
    route: str
    command: InternalGroupCommand
    # ROS services cannot be canceled after dispatch.  This flag means runtime
    # stop/reset was requested while the blocking controller call is in flight.
    cancelled: bool = False
    dispatched: bool = False
    future: Any | None = None


@dataclass(slots=True)
class QueuedVoiceToolTransfer:
    """One latest-wins voice correction waiting for a safe Action terminal."""

    command: InternalSkillCommand
    request: ToolHandoverRequest
    wait_for_predecessor_terminal: bool = False
    same_instrument_predecessor: bool = False
    original_semantic_leg: tuple[str, str] | None = None


class SurgicalInteropExecutionBridge(Node):
    """Translate internal commands while keeping internal policy off the wire."""

    def __init__(self) -> None:
        super().__init__("surgical_interop_execution_bridge")
        self._server_wait_timeout_sec = float(
            self.declare_parameter("server_wait_timeout_sec", 1.0).value
        )
        self._tool_transfer_endpoint = str(
            self.declare_parameter(
                "tool_handover_endpoint",
                "/surgery/tool_handover",
            ).value
        )
        self._tool_handover_enabled = bool(
            self.declare_parameter("tool_handover_enabled", True).value
        )
        self._spec_dir = str(self.declare_parameter("spec_dir", "").value).strip()
        self._procedure_spec = load_bundle(self._spec_dir or None)
        self._instrument_names = {
            instrument.id: instrument.display_name.strip()
            for instrument in self._procedure_spec.bundle.instruments
        }
        self._retraction_service_name = str(
            self.declare_parameter(
                "retraction_service_name", "/surgery/retraction/command"
            ).value
        )
        self._robot_endpoint_source = str(
            self.declare_parameter("robot_endpoint_source", "external").value
        )
        self._retraction_endpoint_source = str(
            self.declare_parameter(
                "retraction_endpoint_source", self._robot_endpoint_source
            ).value
        )
        self._retraction_source_id = str(
            self.declare_parameter("retraction_source_id", "taskplanner").value
        ).strip()
        self._bed_robot_status_endpoint = str(
            self.declare_parameter(
                "bed_robot_status_endpoint", "/external/bed_robot_arms/status"
            ).value
        )
        self._configured_max_retraction_distance_mm = float(
            self.declare_parameter("max_retraction_distance_mm", 50.0).value
        )
        try:
            self._max_retraction_distance_mm = procedure_retraction_distance_limit_mm(
                self._procedure_spec,
                configured_limit_mm=self._configured_max_retraction_distance_mm,
            )
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        self._require_bed_robot_status = bool(
            self.declare_parameter("require_bed_robot_status", True).value
        )
        try:
            self._robot_endpoint_source = normalize_robot_endpoint_source(
                self._robot_endpoint_source
            )
            self._retraction_endpoint_source = normalize_robot_endpoint_source(
                self._retraction_endpoint_source
            )
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        self._virtual_endpoint_mode = (
            self._retraction_endpoint_source == VIRTUAL_ENDPOINT_SOURCE
        )
        configured_retraction_suppression = bool(
            self.declare_parameter(
                "retraction_state_machine_suppressed", False
            ).value
        )
        if configured_retraction_suppression != (
            self._virtual_endpoint_mode
            or not self._retraction_workflow_state_enforced()
        ):
            raise RuntimeError(
                "retraction_state_machine_suppressed does not match the "
                "reviewed endpoint/procedure policy"
            )
        default_contract_topic = (
            "/integration/virtual/surgery/controller_contract"
            if self._virtual_endpoint_mode
            else "/surgery/controller_contract"
        )
        default_contract_id = (
            EIR_NUC_VIRTUAL_CONTRACT_ID
            if self._virtual_endpoint_mode
            else EIR_NUC_EXTERNAL_CONTRACT_ID
        )
        self._require_dispatch_admission_lease = bool(
            self.declare_parameter("require_dispatch_admission_lease", True).value
        )
        self._controller_contract_topic = str(
            self.declare_parameter(
                "controller_contract_topic", default_contract_topic
            ).value
        ).strip()
        self._expected_controller_contract_id = str(
            self.declare_parameter(
                "expected_controller_contract_id", default_contract_id
            ).value
        ).strip()
        self._expected_capability_policy_id = str(
            self.declare_parameter(
                "expected_capability_policy_id",
                EIR_NUC_CAPABILITY_POLICY_ID,
            ).value
        ).strip()
        self._integration_readiness_topic = str(
            self.declare_parameter(
                "integration_readiness_topic", "/integration/readiness"
            ).value
        ).strip()
        self._admission_lease_max_age_sec = max(
            0.1,
            float(self.declare_parameter("admission_lease_max_age_sec", 3.0).value),
        )
        self._admission_lease_source_future_tolerance_sec = max(
            0.0,
            float(
                self.declare_parameter(
                    "admission_lease_source_future_tolerance_sec", 0.5
                ).value
            ),
        )
        # A Service V1 receipt does not prove physical stop.  Require an
        # explicit controller declaration for an external Live route; virtual
        # exercise endpoints remain deliberately unknown rather than physical.
        self._require_physical_stop_confirmation = bool(
            self.declare_parameter(
                "require_physical_stop_confirmation",
                not self._virtual_endpoint_mode,
            ).value
        )
        # Keep both reviewed route descriptions resident.  The active client
        # is selected only by the stopped-state route-control service below;
        # the browser never provides an endpoint string.
        self._external_tool_handover_endpoint = str(
            self.declare_parameter(
                "external_tool_handover_endpoint",
                (
                    self._tool_transfer_endpoint
                    if self._robot_endpoint_source == EXTERNAL_ENDPOINT_SOURCE
                    else EXTERNAL_TOOL_HANDOVER_ENDPOINT
                ),
            ).value
        ).strip()
        self._virtual_tool_handover_endpoint = str(
            self.declare_parameter(
                "virtual_tool_handover_endpoint",
                (
                    self._tool_transfer_endpoint
                    if self._robot_endpoint_source == VIRTUAL_ENDPOINT_SOURCE
                    else VIRTUAL_TOOL_HANDOVER_ENDPOINT
                ),
            ).value
        ).strip()
        self._external_retraction_service_name = str(
            self.declare_parameter(
                "external_retraction_service_name",
                (
                    self._retraction_service_name
                    if self._retraction_endpoint_source == EXTERNAL_ENDPOINT_SOURCE
                    else EXTERNAL_RETRACTION_SERVICE_ENDPOINT
                ),
            ).value
        ).strip()
        self._virtual_retraction_service_name = str(
            self.declare_parameter(
                "virtual_retraction_service_name",
                (
                    self._retraction_service_name
                    if self._retraction_endpoint_source == VIRTUAL_ENDPOINT_SOURCE
                    else VIRTUAL_RETRACTION_SERVICE_ENDPOINT
                ),
            ).value
        ).strip()
        self._external_controller_contract_topic = str(
            self.declare_parameter(
                "external_controller_contract_topic",
                (
                    self._controller_contract_topic
                    if self._robot_endpoint_source == EXTERNAL_ENDPOINT_SOURCE
                    else EXTERNAL_CONTROLLER_CONTRACT_TOPIC
                ),
            ).value
        ).strip()
        self._virtual_controller_contract_topic = str(
            self.declare_parameter(
                "virtual_controller_contract_topic",
                (
                    self._controller_contract_topic
                    if self._robot_endpoint_source == VIRTUAL_ENDPOINT_SOURCE
                    else VIRTUAL_CONTROLLER_CONTRACT_TOPIC
                ),
            ).value
        ).strip()
        self._external_expected_controller_contract_id = str(
            self.declare_parameter(
                "external_expected_controller_contract_id",
                (
                    self._expected_controller_contract_id
                    if self._robot_endpoint_source == EXTERNAL_ENDPOINT_SOURCE
                    else EIR_NUC_EXTERNAL_CONTRACT_ID
                ),
            ).value
        ).strip()
        self._virtual_expected_controller_contract_id = str(
            self.declare_parameter(
                "virtual_expected_controller_contract_id",
                (
                    self._expected_controller_contract_id
                    if self._robot_endpoint_source == VIRTUAL_ENDPOINT_SOURCE
                    else EIR_NUC_VIRTUAL_CONTRACT_ID
                ),
            ).value
        ).strip()
        self._external_expected_capability_policy_id = str(
            self.declare_parameter(
                "external_expected_capability_policy_id",
                (
                    self._expected_capability_policy_id
                    if self._robot_endpoint_source == EXTERNAL_ENDPOINT_SOURCE
                    else EIR_NUC_CAPABILITY_POLICY_ID
                ),
            ).value
        ).strip()
        self._virtual_expected_capability_policy_id = str(
            self.declare_parameter(
                "virtual_expected_capability_policy_id",
                (
                    self._expected_capability_policy_id
                    if self._robot_endpoint_source == VIRTUAL_ENDPOINT_SOURCE
                    else VIRTUAL_EMULATOR_CAPABILITY_POLICY_ID
                ),
            ).value
        ).strip()
        self._external_require_bed_robot_status = bool(
            self.declare_parameter(
                "external_require_bed_robot_status",
                self._require_bed_robot_status,
            ).value
        )
        self._external_require_physical_stop_confirmation = bool(
            self.declare_parameter(
                "external_require_physical_stop_confirmation",
                self._require_physical_stop_confirmation,
            ).value
        )
        self._enable_runtime_route_control = bool(
            self.declare_parameter("enable_runtime_route_control", False).value
        )
        try:
            validate_endpoint_source(
                source=EXTERNAL_ENDPOINT_SOURCE,
                endpoint=self._external_tool_handover_endpoint,
                endpoint_kind="tool handover",
            )
            validate_endpoint_source(
                source=VIRTUAL_ENDPOINT_SOURCE,
                endpoint=self._virtual_tool_handover_endpoint,
                endpoint_kind="tool handover",
            )
            validate_endpoint_source(
                source=EXTERNAL_ENDPOINT_SOURCE,
                endpoint=self._external_retraction_service_name,
                endpoint_kind="retraction service",
            )
            validate_endpoint_source(
                source=VIRTUAL_ENDPOINT_SOURCE,
                endpoint=self._virtual_retraction_service_name,
                endpoint_kind="retraction service",
            )
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        self._bed_robot_status_timeout_sec = float(
            self.declare_parameter("bed_robot_status_timeout_sec", 2.0).value
        )
        self._bed_robot_source_max_age_sec = float(
            self.declare_parameter(
                "bed_robot_source_max_age_sec",
                self._bed_robot_status_timeout_sec,
            ).value
        )
        self._bed_robot_source_future_tolerance_sec = float(
            self.declare_parameter(
                "bed_robot_source_future_tolerance_sec",
                0.5,
            ).value
        )
        self._dispatch_lock = threading.RLock()
        # The stopped-only route coordinator waits for a reset state and a
        # preflight acknowledgement.  Keep those callbacks re-entrant so the
        # incoming SimulationState can advance while the Service handler is
        # waiting on the manager/preflight boundaries.
        self._route_control_callback_group = ReentrantCallbackGroup()
        self._simulation_state_condition = threading.Condition(
            self._dispatch_lock
        )
        self._runtime_accepting_commands = False
        self._dispatch_epoch = 0
        self._last_lifecycle_control_signature: tuple[str, str] | None = None
        self._dispatch_ledger = DispatchLedger(
            int(self.declare_parameter("dedupe_max_entries", 512).value)
        )
        self._direct_hand_state_max_age_sec = max(
            0.1,
            float(
                self.declare_parameter(
                    "direct_hand_state_max_age_sec", 1.0
                ).value
            ),
        )
        self._direct_hand_ledger_error = ""
        direct_hand_ledger_path = str(
            self.declare_parameter(
                "direct_hand_dispatch_ledger_path",
                "/tmp/taskplanner-direct-hand-dispatch.sqlite3",
            ).value
        ).strip()
        try:
            self._direct_hand_dispatch_ledger = DurableDirectHandLedger(
                direct_hand_ledger_path
            )
        except Exception as exc:
            # Explicit voice/recovery commands retain their existing route,
            # but direct-hand motion fails closed until durable reservation is
            # available. Never fall back to a volatile-only ledger.
            self._direct_hand_dispatch_ledger = None
            self._direct_hand_ledger_error = type(exc).__name__
            self.get_logger().error(
                "direct hand dispatch ledger unavailable; implicit handover disabled"
            )
        self._active_actions: dict[tuple[str, str], ActiveAction] = {}
        self._active_services: dict[tuple[str, str], ActiveService] = {}
        self._queued_voice_tool_transfer: QueuedVoiceToolTransfer | None = None
        self._bed_robot_revision: int | None = None
        self._bed_robot_source_stamp_ns: int | None = None
        self._bed_robot_epoch = 0
        self._bed_robot_signature: tuple[Any, ...] | None = None
        self._bed_robot_procedure_type = ""
        self._bed_robot_received_monotonic = 0.0
        self._bed_robot_states: dict[str, Any] = {}
        self._latest_controller_contract: dict[str, Any] | None = None
        self._latest_controller_contract_received_monotonic = 0.0
        self._latest_controller_contract_source_error = ""
        self._last_accepted_controller_contract_source_stamp_sec = 0.0
        # Keep each reviewed controller's lease separate. A mixed route must
        # not let a fresh virtual retraction contract stand in for the real
        # handover controller, or the reverse.
        self._controller_contract_leases: dict[str, dict[str, Any]] = {
            source: {
                "payload": None,
                "received_monotonic": 0.0,
                "source_error": "",
                "last_accepted_stamp_sec": 0.0,
            }
            for source in (EXTERNAL_ENDPOINT_SOURCE, VIRTUAL_ENDPOINT_SOURCE)
        }
        self._latest_integration_readiness: dict[str, Any] | None = None
        self._latest_integration_readiness_received_monotonic = 0.0
        self._latest_integration_readiness_source_error = ""
        self._last_accepted_integration_readiness_source_stamp_sec = 0.0
        self._dispatch_admission_armed = not self._require_dispatch_admission_lease
        self._dispatch_admission_disarmed_reason = (
            "" if self._dispatch_admission_armed else "admission_lease_missing"
        )
        self._execution_trace_sequence = 0
        self._route_revision = 0
        self._route_initialization_revision = 0
        self._route_initialization_state = "launch_default"
        self._run_endpoint_source = ""
        self._run_retraction_source = ""
        self._latest_simulation_state: SimulationState | None = None
        self._latest_simulation_state_received_monotonic = 0.0
        self._latest_simulation_state_generation = 0
        self._skill_status_pub = self.create_publisher(SkillStatus, "/skill/status", 20)
        self._skill_event_pub = self.create_publisher(TwinEvent, "/skill/events", 20)
        self._group_status_pub = self.create_publisher(
            BedRobotArmGroupStatus, "/bed_robot_arm_group/status", 20
        )
        # This is a producer-only, UI-observability topic.  It contains no
        # VLM proposal, planner rationale, free text, or command arguments.
        # ``dispatch_submitted`` says only that a local ROS client call returned;
        # it never claims remote receipt or physical completion.
        self._execution_trace_pub = self.create_publisher(
            ExecutionTrace, _EXECUTION_TRACE_TOPIC, 50
        )
        self._execution_route_state_pub = self.create_publisher(
            String,
            EXECUTION_ROUTE_STATE_TOPIC,
            QoSProfile(
                history=QoSHistoryPolicy.KEEP_LAST,
                depth=1,
                reliability=QoSReliabilityPolicy.RELIABLE,
                durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
            ),
        )
        self._external_tool_transfer_client = ActionClient(
            self,
            ExecuteToolHandover,
            self._external_tool_handover_endpoint,
        )
        self._virtual_tool_transfer_client = ActionClient(
            self,
            ExecuteToolHandover,
            self._virtual_tool_handover_endpoint,
        )
        self._external_retraction_service_client = self.create_client(
            ExecuteRetractionCommand, self._external_retraction_service_name
        )
        self._virtual_retraction_service_client = self.create_client(
            ExecuteRetractionCommand, self._virtual_retraction_service_name
        )
        self._route_transition_ready_client = None
        self._route_reset_client = None
        self._route_preflight_ack_client = None
        if self._enable_runtime_route_control:
            self._route_transition_ready_client = self.create_client(
                Trigger,
                _EXECUTION_ROUTE_MANAGER_TRANSITION_SERVICE,
                callback_group=self._route_control_callback_group,
            )
            self._route_reset_client = self.create_client(
                ControlSimulation,
                _EXECUTION_ROUTE_MANAGER_CONTROL_SERVICE,
                callback_group=self._route_control_callback_group,
            )
            self._route_preflight_ack_client = self.create_client(
                IntegrationDebugCommand,
                EXECUTION_ROUTE_PREFLIGHT_ACK_SERVICE,
                callback_group=self._route_control_callback_group,
            )
        with self._dispatch_lock:
            self._set_route_source_locked(
                self._robot_endpoint_source,
                retraction_source=self._retraction_endpoint_source,
                clear_admission=False,
            )
        self.create_subscription(SkillCommand, "/bt/skill_command", self._on_skill, 20)
        self.create_subscription(
            BedRobotArmGroupCommand,
            "/bt/bed_robot_arm_group_command",
            self._on_group,
            20,
        )
        self.create_subscription(
            String,
            "/simulation/control_state",
            self._on_control,
            20,
            callback_group=self._route_control_callback_group,
        )
        self.create_subscription(
            SimulationState,
            "/simulation/state",
            self._on_simulation_state,
            20,
            callback_group=self._route_control_callback_group,
        )
        self.create_subscription(
            String,
            self._external_controller_contract_topic,
            lambda msg: self._on_controller_contract(msg, EXTERNAL_ENDPOINT_SOURCE),
            10,
        )
        self.create_subscription(
            String,
            self._virtual_controller_contract_topic,
            lambda msg: self._on_controller_contract(msg, VIRTUAL_ENDPOINT_SOURCE),
            10,
        )
        self.create_subscription(
            String,
            self._integration_readiness_topic,
            self._on_integration_readiness,
            10,
        )
        # Service-only Live/virtual routes do not subscribe to a physical
        # controller status topic.  The direct callback stays available for
        # the explicit telemetry-required contract and its unit tests.
        self._bed_robot_status_subscription = None
        if self._external_require_bed_robot_status:
            self._bed_robot_status_subscription = self.create_subscription(
                BedRobotArmStateArray,
                self._bed_robot_status_endpoint,
                self._on_bed_robot_status,
                20,
            )
        self._route_command_service = None
        if self._enable_runtime_route_control:
            self._route_command_service = self.create_service(
                IntegrationDebugCommand,
                EXECUTION_ROUTE_COMMAND_SERVICE,
                self._handle_execution_route_command,
                callback_group=self._route_control_callback_group,
            )
        self.add_on_set_parameters_callback(
            self._on_endpoint_configuration_parameters_changed
        )
        self._publish_execution_route_state()
        self.create_timer(1.0, self._publish_execution_route_state)

    def _route_definition(
        self,
        tool_source: object,
        retraction_source: object | None = None,
    ) -> dict[str, object]:
        """Return independently selected, allow-listed Action and Service routes.

        The browser can choose only the source ID for each operation. Endpoint
        text, controller IDs, and capability policies remain launch-owned. A
        virtual retraction therefore cannot shadow an external handover Action,
        and vice versa.
        """

        normalized_tool = normalize_robot_endpoint_source(str(tool_source))
        normalized_retraction = normalize_robot_endpoint_source(
            str(retraction_source or normalized_tool)
        )
        tool_virtual = normalized_tool == VIRTUAL_ENDPOINT_SOURCE
        retraction_virtual = normalized_retraction == VIRTUAL_ENDPOINT_SOURCE
        return {
            # ``source`` remains the tool-handover source for older state
            # consumers. The explicit retraction field carries the other half
            # of a mixed route.
            "source": normalized_tool,
            "retraction_source": normalized_retraction,
            "tool_handover_endpoint": (
                self._virtual_tool_handover_endpoint
                if tool_virtual
                else self._external_tool_handover_endpoint
            ),
            "retraction_service_name": (
                self._virtual_retraction_service_name
                if retraction_virtual
                else self._external_retraction_service_name
            ),
            "controller_contract_topic": (
                self._virtual_controller_contract_topic
                if tool_virtual
                else self._external_controller_contract_topic
            ),
            "expected_controller_contract_id": (
                self._virtual_expected_controller_contract_id
                if tool_virtual
                else self._external_expected_controller_contract_id
            ),
            "expected_capability_policy_id": (
                self._virtual_expected_capability_policy_id
                if tool_virtual
                else self._external_expected_capability_policy_id
            ),
            "retraction_controller_contract_topic": (
                self._virtual_controller_contract_topic
                if retraction_virtual
                else self._external_controller_contract_topic
            ),
            "retraction_expected_controller_contract_id": (
                self._virtual_expected_controller_contract_id
                if retraction_virtual
                else self._external_expected_controller_contract_id
            ),
            "require_bed_robot_status": (
                self._external_require_bed_robot_status
                if not retraction_virtual
                else False
            ),
            "require_physical_stop_confirmation": (
                self._external_require_physical_stop_confirmation
                if not retraction_virtual
                else False
            ),
            "tool_client": (
                self._virtual_tool_transfer_client
                if tool_virtual
                else self._external_tool_transfer_client
            ),
            "retraction_client": (
                self._virtual_retraction_service_client
                if retraction_virtual
                else self._external_retraction_service_client
            ),
        }

    def _clear_route_admission_locked(self) -> None:
        """Invalidate leases and local dispatch history across route families."""

        self._dispatch_ledger.clear()
        self._queued_voice_tool_transfer = None
        self._latest_controller_contract = None
        self._latest_controller_contract_received_monotonic = 0.0
        self._latest_controller_contract_source_error = ""
        self._last_accepted_controller_contract_source_stamp_sec = 0.0
        for lease in self._controller_contract_leases.values():
            lease.update(
                {
                    "payload": None,
                    "received_monotonic": 0.0,
                    "source_error": "",
                    "last_accepted_stamp_sec": 0.0,
                }
            )
        self._latest_integration_readiness = None
        self._latest_integration_readiness_received_monotonic = 0.0
        self._latest_integration_readiness_source_error = ""
        self._last_accepted_integration_readiness_source_stamp_sec = 0.0
        self._dispatch_admission_armed = not self._require_dispatch_admission_lease
        self._dispatch_admission_disarmed_reason = (
            "" if self._dispatch_admission_armed else "route_reinitialized"
        )
        self._bed_robot_revision = None
        self._bed_robot_source_stamp_ns = None
        self._bed_robot_epoch = 0
        self._bed_robot_signature = None
        self._bed_robot_procedure_type = ""
        self._bed_robot_received_monotonic = 0.0
        self._bed_robot_states = {}

    def _set_route_source_locked(
        self,
        source: object,
        *,
        retraction_source: object | None = None,
        clear_admission: bool,
    ) -> None:
        """Atomically swap the reviewed Action and Service routes under one lock."""

        route = self._route_definition(source, retraction_source)
        self._robot_endpoint_source = str(route["source"])
        self._retraction_endpoint_source = str(route["retraction_source"])
        self._virtual_endpoint_mode = (
            self._retraction_endpoint_source == VIRTUAL_ENDPOINT_SOURCE
        )
        self._tool_transfer_endpoint = str(route["tool_handover_endpoint"])
        self._retraction_service_name = str(route["retraction_service_name"])
        self._controller_contract_topic = str(route["controller_contract_topic"])
        self._expected_controller_contract_id = str(
            route["expected_controller_contract_id"]
        )
        self._expected_capability_policy_id = str(
            route["expected_capability_policy_id"]
        )
        self._retraction_controller_contract_topic = str(
            route["retraction_controller_contract_topic"]
        )
        self._retraction_expected_controller_contract_id = str(
            route["retraction_expected_controller_contract_id"]
        )
        self._require_bed_robot_status = bool(route["require_bed_robot_status"])
        self._require_physical_stop_confirmation = bool(
            route["require_physical_stop_confirmation"]
        )
        self._tool_transfer_client = route["tool_client"]
        self._retraction_service_client = route["retraction_client"]
        if clear_admission:
            self._clear_route_admission_locked()

    def _retraction_workflow_state_enforced(self) -> bool:
        return bool(
            self._procedure_spec.get_scenario_runtime_requirements()
            .retraction_workflow_state_enforced
        )

    def _execution_route_state_snapshot_locked(self) -> dict[str, object]:
        """Build a bounded UI projection while the dispatch lock is held."""

        def client_ready(client: object, method: str) -> bool:
            try:
                return bool(getattr(client, method)())
            except Exception:
                return False

        return {
            "schema": EXECUTION_ROUTE_STATE_SCHEMA,
            "stamp_sec": round(time.time(), 6),
            "revision": int(self._route_revision),
            "initialization_revision": int(self._route_initialization_revision),
            "selected_source": self._robot_endpoint_source,
            "run_endpoint_source": self._run_endpoint_source,
            "retraction_source": self._retraction_endpoint_source,
            "run_retraction_source": getattr(
                self, "_run_retraction_source", ""
            ),
            "initialization_state": self._route_initialization_state,
            "tool_handover_endpoint": self._tool_transfer_endpoint,
            "retraction_service_name": self._retraction_service_name,
            "controller_contract_topic": self._controller_contract_topic,
            "expected_controller_contract_id": (
                self._expected_controller_contract_id
            ),
            "expected_capability_policy_id": (
                self._expected_capability_policy_id
            ),
            "retraction_controller_contract_topic": (
                self._retraction_controller_contract_topic
            ),
            "retraction_expected_controller_contract_id": (
                self._retraction_expected_controller_contract_id
            ),
            "require_bed_robot_status": bool(self._require_bed_robot_status),
            "require_physical_stop_confirmation": bool(
                self._require_physical_stop_confirmation
            ),
            # Scenario policy may disable only Taskplanner's local workflow
            # ordering; Service/controller validation remains authoritative.
            "retraction_state_machine_suppressed": bool(
                self._virtual_endpoint_mode
                or not self._retraction_workflow_state_enforced()
            ),
            "action_server_ready": client_ready(
                self._tool_transfer_client, "server_is_ready"
            ),
            "retraction_service_ready": client_ready(
                self._retraction_service_client, "service_is_ready"
            ),
            # A source-specific readiness projection lets the operator see
            # whether a stopped switch would select a live Action *and*
            # Service pair.  These are graph observations only; no Action
            # goal or Service request is sent while building this state.
            "source_readiness": {
                EXTERNAL_ENDPOINT_SOURCE: {
                    "action_server_ready": client_ready(
                        self._external_tool_transfer_client,
                        "server_is_ready",
                    ),
                    "retraction_service_ready": client_ready(
                        self._external_retraction_service_client,
                        "service_is_ready",
                    ),
                },
                VIRTUAL_ENDPOINT_SOURCE: {
                    "action_server_ready": client_ready(
                        self._virtual_tool_transfer_client,
                        "server_is_ready",
                    ),
                    "retraction_service_ready": client_ready(
                        self._virtual_retraction_service_client,
                        "service_is_ready",
                    ),
                },
            },
            "route_control_enabled": bool(self._enable_runtime_route_control),
            # The bridge owns the one public coordinator.  The manager is
            # consulted through its stopped-state/reset services, rather than
            # exposing a second mutating route endpoint.
            "route_command_service": EXECUTION_ROUTE_COMMAND_SERVICE,
            "route_command_service_enabled": bool(
                self._enable_runtime_route_control
            ),
            "route_command_service_ready": bool(
                getattr(self, "_route_command_service", None) is not None
            ),
            "preflight_ack_service": EXECUTION_ROUTE_PREFLIGHT_ACK_SERVICE,
            "preflight_ack_service_ready": client_ready(
                getattr(self, "_route_preflight_ack_client", None),
                "service_is_ready",
            ),
            "active_request_count": len(self._active_actions)
            + len(self._active_services),
        }

    def _execution_route_state_snapshot(self) -> dict[str, object]:
        with self._dispatch_lock:
            return self._execution_route_state_snapshot_locked()

    def _publish_execution_route_state(self) -> None:
        publisher = getattr(self, "_execution_route_state_pub", None)
        if publisher is None:
            return
        try:
            message = String()
            message.data = json.dumps(
                self._execution_route_state_snapshot(),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            publisher.publish(message)
        except Exception:  # pragma: no cover - UI observability only
            self.get_logger().warning("execution route state publish failed")

    def _on_simulation_state(self, msg: SimulationState) -> None:
        condition = getattr(self, "_simulation_state_condition", None)
        if condition is None:  # pragma: no cover - small legacy test doubles
            with self._dispatch_lock:
                self._latest_simulation_state = msg
                self._latest_simulation_state_received_monotonic = time.monotonic()
                self._latest_simulation_state_generation = int(
                    getattr(self, "_latest_simulation_state_generation", 0)
                ) + 1
            return
        with condition:
            self._latest_simulation_state = msg
            self._latest_simulation_state_received_monotonic = time.monotonic()
            self._latest_simulation_state_generation = int(
                getattr(self, "_latest_simulation_state_generation", 0)
            ) + 1
            condition.notify_all()

    def _execution_route_switch_guard_locked(self) -> str:
        """Require a fresh, explicit stopped DT before endpoint mutation."""

        if self._runtime_accepting_commands:
            return "runtime_is_accepting_commands"
        if self._active_actions or self._active_services:
            return "active_controller_request"
        state = self._latest_simulation_state
        received = self._latest_simulation_state_received_monotonic
        if state is None or received <= 0.0:
            return "simulation_state_missing"
        if time.monotonic() - received > 3.0:
            return "simulation_state_stale"
        if bool(getattr(state, "running", False)):
            return "simulation_running"
        execution_state = str(
            getattr(state, "execution_state", "") or ""
        ).strip().casefold()
        if execution_state not in _EXECUTION_ROUTE_SAFE_STOPPED_STATES:
            return "simulation_not_stopped"
        if str(getattr(state, "active_robot_task_id", "") or "").strip():
            return "active_robot_task"
        if bool(getattr(state, "cleaner_busy", False)):
            return "cleaner_busy"
        return ""

    @staticmethod
    def _decode_execution_route_payload(raw: object) -> dict[str, object]:
        text = str(raw or "")
        if len(text) > _EXECUTION_ROUTE_RESULT_MAX_CHARS:
            raise ValueError("execution route payload is too large")
        try:
            payload = json.loads(text or "{}")
        except (TypeError, ValueError) as exc:
            raise ValueError("execution route payload must be an object") from exc
        if not isinstance(payload, dict):
            raise ValueError("execution route payload must be an object")
        return payload

    @staticmethod
    def _wait_execution_route_future(future: object, timeout_sec: float):
        """Wait without recursively spinning the current callback thread."""

        deadline = time.monotonic() + max(0.01, float(timeout_sec))
        done = getattr(future, "done", None)
        result = getattr(future, "result", None)
        while time.monotonic() < deadline:
            try:
                if callable(done) and done():
                    return result() if callable(result) else None
                # Small unit-test doubles may expose only ``result``. The
                # real rclpy Future has ``done`` and is delivered by another
                # executor thread in the re-entrant route callback group.
                if not callable(done) and callable(result):
                    return result()
            except Exception:
                return None
            time.sleep(0.02)
        return None

    @staticmethod
    def _route_response_json(state: dict[str, object], *, reset: bool) -> str:
        """Serialize one bounded public result without controller detail."""

        payload = dict(state)
        payload["digital_twin_reset"] = bool(reset)
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if len(encoded) <= _EXECUTION_ROUTE_RESULT_MAX_CHARS * 2:
            return encoded
        return json.dumps(
            {
                "digital_twin_reset": bool(reset),
                "result_truncated": True,
            },
            separators=(",", ":"),
            sort_keys=True,
        )

    def _reject_execution_route_command(
        self,
        response: IntegrationDebugCommand.Response,
        reason: str,
        *,
        reset: bool,
    ) -> IntegrationDebugCommand.Response:
        response.accepted = False
        response.command_id = ""
        response.message = str(reason or "execution route switch rejected")[:256]
        response.result_json = self._route_response_json(
            self._execution_route_state_snapshot(),
            reset=reset,
        )
        return response

    def _manager_transition_ready_for_execution_route(self) -> str:
        client = getattr(self, "_route_transition_ready_client", None)
        if client is None or not client.wait_for_service(
            timeout_sec=_EXECUTION_ROUTE_MANAGER_CHECK_TIMEOUT_SEC
        ):
            return "simulation_transition_check_unavailable"
        result = self._wait_execution_route_future(
            client.call_async(Trigger.Request()),
            _EXECUTION_ROUTE_MANAGER_CHECK_TIMEOUT_SEC,
        )
        if result is None:
            return "simulation_transition_check_timeout"
        if not bool(getattr(result, "success", False)):
            return "simulation_transition_not_ready"
        return ""

    def _request_execution_route_reset(self) -> str:
        client = getattr(self, "_route_reset_client", None)
        if client is None or not client.wait_for_service(
            timeout_sec=_EXECUTION_ROUTE_MANAGER_CHECK_TIMEOUT_SEC
        ):
            return "simulation_reset_service_unavailable"
        request = ControlSimulation.Request()
        request.command = "reset"
        request.start_phase_id = ""
        result = self._wait_execution_route_future(
            client.call_async(request),
            _EXECUTION_ROUTE_MANAGER_CHECK_TIMEOUT_SEC,
        )
        if result is None:
            return "simulation_reset_request_timeout"
        if not bool(getattr(result, "success", False)):
            return "simulation_reset_rejected"
        return ""

    def _wait_for_execution_route_reset(
        self,
        *,
        after_generation: int,
    ) -> str:
        """Require the manager reset to yield a new, idle twin frame."""

        deadline = time.monotonic() + _EXECUTION_ROUTE_RESET_TIMEOUT_SEC
        condition = getattr(self, "_simulation_state_condition", None)
        if condition is None:  # pragma: no cover - partial test doubles
            with self._dispatch_lock:
                return self._execution_route_switch_guard_locked()
        with condition:
            while time.monotonic() < deadline:
                state = self._latest_simulation_state
                generation = int(
                    getattr(self, "_latest_simulation_state_generation", 0)
                )
                execution_state = str(
                    getattr(state, "execution_state", "") or ""
                ).strip().casefold()
                reason = self._execution_route_switch_guard_locked()
                if (
                    generation > after_generation
                    and execution_state == "idle"
                    and not reason
                ):
                    return ""
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    break
                condition.wait(timeout=min(0.1, remaining))
        return "simulation_reset_idle_state_timeout"

    def _wait_for_preflight_route_ack(
        self,
        state: dict[str, object],
        *,
        require_initialized: bool,
    ) -> str:
        """Wait until preflight has atomically applied this exact route.

        This is only an application barrier: controller readiness remains the
        separate preflight start gate.
        """

        client = getattr(self, "_route_preflight_ack_client", None)
        if client is None or not client.wait_for_service(timeout_sec=1.0):
            return "preflight_route_ack_unavailable"
        expected_source = str(state.get("selected_source", ""))
        expected_retraction_source = str(state.get("retraction_source", ""))
        expected_revision = int(state.get("revision", -1))
        expected_initialization_revision = int(
            state.get("initialization_revision", -1)
        )
        deadline = time.monotonic() + _EXECUTION_ROUTE_PREFLIGHT_ACK_TIMEOUT_SEC
        while time.monotonic() < deadline:
            request = IntegrationDebugCommand.Request()
            request.operation = "execution_route_preflight_ack"
            request.payload_json = json.dumps(
                {
                    "source": expected_source,
                    "retraction_source": expected_retraction_source,
                    "revision": expected_revision,
                    "initialization_revision": expected_initialization_revision,
                    "require_initialized": bool(require_initialized),
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            result = self._wait_execution_route_future(
                client.call_async(request),
                min(1.0, max(0.05, deadline - time.monotonic())),
            )
            if result is not None and bool(getattr(result, "accepted", False)):
                return ""
            time.sleep(0.05)
        return "preflight_route_ack_timeout"

    def _mark_execution_route_initializing_locked(self) -> dict[str, object]:
        self._route_initialization_state = "initializing"
        self._route_initialization_revision += 1
        return self._execution_route_state_snapshot_locked()

    def _fail_closed_execution_route_initialization(self) -> dict[str, object]:
        """Leave preflight explicitly blocked after acknowledgement loss."""

        with self._dispatch_lock:
            state = self._mark_execution_route_initializing_locked()
        self._publish_execution_route_state()
        return state

    def _handle_execution_route_command(
        self,
        request: IntegrationDebugCommand.Request,
        response: IntegrationDebugCommand.Response,
    ) -> IntegrationDebugCommand.Response:
        """Coordinate the only stopped-only Action/Service route mutation.

        This public boundary first proves the bridge has no active Action or
        Service request, asks simulation_manager for an independent stopped
        proof, requests its DT reset, then proves a new idle frame before
        swapping the reviewed Action *and* Service pair.  No second route
        mutation service exists that can bypass those reset steps.
        """

        reset_completed = False
        try:
            operation = str(getattr(request, "operation", "") or "").strip().lower()
            payload = self._decode_execution_route_payload(
                getattr(request, "payload_json", "")
            )
            if operation == "execution_route_status":
                response.accepted = True
                response.command_id = ""
                response.message = "execution route status"
                response.result_json = self._route_response_json(
                    self._execution_route_state_snapshot(),
                    reset=False,
                )
                return response
            if operation not in {
                "configure_robot_endpoint_source",
                "configure_execution_endpoints",
            }:
                raise ValueError("unsupported execution route operation")
            requested_source = normalize_robot_endpoint_source(
                str(
                    payload.get(
                        "tool_handover_source",
                        payload.get("source", ""),
                    )
                )
            )
            requested_retraction_source = normalize_robot_endpoint_source(
                str(payload.get("retraction_source", requested_source))
            )
            with self._dispatch_lock:
                reason = self._execution_route_switch_guard_locked()
                reset_generation = int(
                    getattr(self, "_latest_simulation_state_generation", 0)
                )
                if reason:
                    return self._reject_execution_route_command(
                        response,
                        reason,
                        reset=False,
                    )
            reason = self._manager_transition_ready_for_execution_route()
            if reason:
                return self._reject_execution_route_command(
                    response,
                    reason,
                    reset=False,
                )
            reason = self._request_execution_route_reset()
            if reason:
                return self._reject_execution_route_command(
                    response,
                    reason,
                    reset=False,
                )
            reset_completed = True
            reason = self._wait_for_execution_route_reset(
                after_generation=reset_generation
            )
            if reason:
                return self._reject_execution_route_command(
                    response,
                    reason,
                    reset=True,
                )
            with self._dispatch_lock:
                reason = self._execution_route_switch_guard_locked()
                if reason:
                    return self._reject_execution_route_command(
                        response,
                        reason,
                        reset=True,
                    )
                previous_source = self._robot_endpoint_source
                previous_retraction_source = getattr(
                    self, "_retraction_endpoint_source", self._robot_endpoint_source
                )
                try:
                    self._set_route_source_locked(
                        requested_source,
                        retraction_source=requested_retraction_source,
                        clear_admission=True,
                    )
                except TypeError:  # pragma: no cover - focused legacy doubles
                    self._set_route_source_locked(
                        requested_source,
                        clear_admission=True,
                    )
                self._run_endpoint_source = ""
                self._run_retraction_source = ""
                self._route_revision += 1
                state = self._mark_execution_route_initializing_locked()
            self._publish_execution_route_state()
            reason = self._wait_for_preflight_route_ack(
                state,
                require_initialized=False,
            )
            if reason:
                self._fail_closed_execution_route_initialization()
                return self._reject_execution_route_command(
                    response,
                    reason,
                    reset=True,
                )
            with self._dispatch_lock:
                reason = self._execution_route_switch_guard_locked()
                if reason:
                    state = self._mark_execution_route_initializing_locked()
                else:
                    self._route_initialization_state = "initialized"
                    state = self._execution_route_state_snapshot_locked()
            self._publish_execution_route_state()
            if reason:
                return self._reject_execution_route_command(
                    response,
                    reason,
                    reset=True,
                )
            reason = self._wait_for_preflight_route_ack(
                state,
                require_initialized=True,
            )
            if reason:
                self._fail_closed_execution_route_initialization()
                return self._reject_execution_route_command(
                    response,
                    reason,
                    reset=True,
                )
            response.accepted = True
            response.command_id = ""
            response.message = (
                "execution route initialized"
                if (
                    previous_source == requested_source
                    and previous_retraction_source == requested_retraction_source
                )
                else (
                    "execution route changed to "
                    f"tool={requested_source}, retraction={requested_retraction_source} "
                    "and initialized"
                )
            )
            response.result_json = self._route_response_json(
                state,
                reset=True,
            )
            return response
        except ValueError as exc:
            return self._reject_execution_route_command(
                response,
                str(exc),
                reset=reset_completed,
            )
        except Exception as exc:  # pragma: no cover - fail closed service boundary
            self.get_logger().error(f"execution route command failed: {exc}")
            if reset_completed:
                self._fail_closed_execution_route_initialization()
            return self._reject_execution_route_command(
                response,
                "execution route command failed",
                reset=reset_completed,
            )

    def _on_endpoint_configuration_parameters_changed(
        self, parameters: list[Any]
    ) -> SetParametersResult:
        """Keep endpoints fixed while safely accepting a stopped spec reload."""

        launch_lifetime_parameters = {
            "robot_endpoint_source",
            "retraction_endpoint_source",
            "tool_handover_endpoint",
            "retraction_service_name",
            "bed_robot_status_endpoint",
            "require_bed_robot_status",
            "require_dispatch_admission_lease",
            "controller_contract_topic",
            "expected_controller_contract_id",
            "expected_capability_policy_id",
            "integration_readiness_topic",
            "admission_lease_max_age_sec",
            "admission_lease_source_future_tolerance_sec",
            "require_physical_stop_confirmation",
            "external_tool_handover_endpoint",
            "virtual_tool_handover_endpoint",
            "external_retraction_service_name",
            "virtual_retraction_service_name",
            "external_controller_contract_topic",
            "virtual_controller_contract_topic",
            "external_expected_controller_contract_id",
            "virtual_expected_controller_contract_id",
            "external_expected_capability_policy_id",
            "virtual_expected_capability_policy_id",
            "external_require_bed_robot_status",
            "external_require_physical_stop_confirmation",
            "enable_runtime_route_control",
            "direct_hand_state_max_age_sec",
            "direct_hand_dispatch_ledger_path",
        }
        for parameter in parameters:
            if parameter.name in launch_lifetime_parameters:
                return SetParametersResult(
                    successful=False,
                    reason=(
                        f"{parameter.name} is launch-lifetime and cannot be "
                        "changed while the bridge is running"
                    ),
                )
        spec_update = next(
            (parameter for parameter in parameters if parameter.name == "spec_dir"),
            None,
        )
        if spec_update is None:
            return SetParametersResult(successful=True)

        next_spec_dir = str(spec_update.value).strip()
        if not next_spec_dir:
            return SetParametersResult(
                successful=False,
                reason="spec_dir must identify a procedure bundle",
            )
        try:
            next_spec = load_bundle(next_spec_dir)
            next_instrument_names = {
                instrument.id: instrument.display_name.strip()
                for instrument in next_spec.bundle.instruments
            }
            next_max_retraction_distance_mm = procedure_retraction_distance_limit_mm(
                next_spec,
                configured_limit_mm=self._configured_max_retraction_distance_mm,
            )
        except Exception as exc:
            return SetParametersResult(
                successful=False,
                reason=f"failed to reload procedure spec: {exc}",
            )

        # The manager first sends reset and closes the readiness lease.  Keep
        # this guard locally as well: an arbitrary parameter client must never
        # change the mapping while an Action/Service is in flight or the bridge
        # is eligible to dispatch one.
        with self._dispatch_lock:
            if self._runtime_accepting_commands:
                return SetParametersResult(
                    successful=False,
                    reason="spec_dir can change only while runtime dispatch is disabled",
                )
            if self._active_actions or self._active_services:
                return SetParametersResult(
                    successful=False,
                    reason="spec_dir can change only with no active controller request",
                )
            self._spec_dir = next_spec_dir
            self._procedure_spec = next_spec
            self._instrument_names = next_instrument_names
            self._max_retraction_distance_mm = next_max_retraction_distance_mm
            self._dispatch_ledger.clear()
            self._last_lifecycle_control_signature = None
        return SetParametersResult(successful=True)

    def _stamp(self):
        return self.get_clock().now().to_msg()

    @staticmethod
    def _action_key(route: str, command_id: str) -> tuple[str, str]:
        return route, command_id

    @staticmethod
    def _explicit_request_generation(command: InternalSkillCommand | InternalGroupCommand) -> int | None:
        if isinstance(command, InternalSkillCommand) and command.mode == "explicit_request":
            return int(command.request_generation)
        return None

    def _direct_hand_run_guard(self, command: InternalSkillCommand) -> str:
        """Bind every implicit command to one fresh authoritative run."""

        is_direct = command.mode == "implicit_request"
        if not is_direct:
            if command.procedure_run_id or command.implicit_request_generation:
                return "unexpected_direct_hand_identity"
            return ""
        if (
            not valid_procedure_run_id(command.procedure_run_id)
            or int(command.implicit_request_generation) <= 0
        ):
            return "direct_hand_episode_invalid"
        if getattr(self, "_direct_hand_dispatch_ledger", None) is None:
            return "direct_hand_ledger_unavailable"
        state = getattr(self, "_latest_simulation_state", None)
        received_at = float(
            getattr(self, "_latest_simulation_state_received_monotonic", 0.0)
        )
        if state is None or received_at <= 0.0:
            return "direct_hand_runtime_state_missing"
        if (
            time.monotonic() - received_at
            > float(getattr(self, "_direct_hand_state_max_age_sec", 1.0))
        ):
            return "direct_hand_runtime_state_stale"
        if (
            not bool(getattr(state, "running", False))
            or str(getattr(state, "execution_state", "")).strip().casefold()
            != "running"
        ):
            return "direct_hand_runtime_not_running"
        if (
            str(getattr(state, "procedure_run_id", "")).strip()
            != command.procedure_run_id
        ):
            return "direct_hand_run_mismatch"
        return ""

    @staticmethod
    def _tool_transfer_semantic_leg(
        request: ToolHandoverRequest,
    ) -> tuple[str, str]:
        """Return the public Goal semantics, excluding its transport ID.

        A single admitted request can require multiple Action Goals.  The
        canonical controller-facing source-to-target transition identifies one
        replay-safe leg; ``command_id`` remains independently at-most-once.
        """

        return (
            request.source_location.strip().casefold(),
            request.target_location.strip().casefold(),
        )

    @staticmethod
    def _same_instrument_instance(
        first: InternalSkillCommand,
        second: InternalSkillCommand,
    ) -> bool:
        """Compare the stable private identity without exposing it externally."""

        first_instance = first.instrument_instance_id.strip().casefold()
        second_instance = second.instrument_instance_id.strip().casefold()
        if first_instance and second_instance:
            return first_instance == second_instance
        first_instrument = first.instrument_id.strip().casefold()
        second_instrument = second.instrument_id.strip().casefold()
        return bool(first_instrument) and first_instrument == second_instrument

    @classmethod
    def _voice_waits_for_active_auto_return(
        cls,
        active: ActiveAction,
        command: InternalSkillCommand,
    ) -> bool:
        """Keep a same-tool voice request queued behind an in-flight return.

        Canceling an external robot-to-Mayo Goal merely to reverse it can leave
        the real tool location ambiguous when the controller's cancel recovery
        fails.  The voice request retains latest-wins priority, but waits for
        the already-dispatched return to report a safe terminal instead.
        """

        return (
            not active.cancelled
            and isinstance(active.command, InternalSkillCommand)
            and active.command.action in RETURN_UNUSED_PREPOSITION_ALIASES
            and cls._same_instrument_instance(active.command, command)
        )

    @staticmethod
    def _resolve_voice_after_predecessor(
        queued: QueuedVoiceToolTransfer,
        *,
        source_location: str,
    ) -> tuple[str, QueuedVoiceToolTransfer | None]:
        """Resolve a same-tool voice request from controller-confirmed state.

        ``satisfied`` means the predecessor already delivered the exact tool
        instance to the requested target. ``dispatch`` carries a source-rebased
        request. Anything else is intentionally rejected instead of replaying
        a stale planner source.
        """

        source = source_location.strip().casefold()
        target = queued.request.target_location.strip().casefold()
        if not source or not target:
            return "rejected", None
        if source == target:
            return "satisfied", queued
        if target != "surgeon":
            return "rejected", None
        if source == "tray":
            command = replace(
                queued.command,
                action="pick_up_and_handover",
                source_location_type="tray",
                source_location_id="tray",
            )
        elif source == "robot":
            command = replace(
                queued.command,
                action="direct_handover",
                source_location_type="robot_right_hand",
                source_location_id="robot_right_hand",
            )
        elif source == "mayo":
            # The completed automatic return is authoritative: the tool is now
            # on Mayo.  Rebase the queued voice request onto the supported
            # Mayo-to-robot preparation leg; the still-active DT request will
            # dispatch robot-to-surgeon after ToolPrepared is reconciled.
            command = replace(
                queued.command,
                action="prepare_tool",
                source_location_type="mayo_stand",
                source_location_id="mayo_stand",
            )
        else:
            return "rejected", None
        effective_target = "robot" if source == "mayo" else target
        return (
            "dispatch",
            QueuedVoiceToolTransfer(
                command=command,
                request=replace(
                    queued.request,
                    source_location=source,
                    target_location=effective_target,
                ),
                wait_for_predecessor_terminal=False,
                same_instrument_predecessor=True,
                original_semantic_leg=queued.original_semantic_leg,
            ),
        )

    @staticmethod
    def _controller_confirmed_tool_location(
        semantic_leg: tuple[str, str] | None,
        *,
        success: bool,
        final_state: str,
        reason_code: str,
    ) -> str:
        """Project the public terminal contract onto one confirmed location."""

        if semantic_leg is None:
            return ""
        source_location, target_location = semantic_leg
        if success and final_state == ExecuteToolHandover.Result.FINAL_COMPLETED:
            return target_location
        if final_state != ExecuteToolHandover.Result.FINAL_CANCELED:
            return ""
        if (
            reason_code
            == ExecuteToolHandover.Result.REASON_CANCELED_SOURCE_UNCHANGED
        ):
            return source_location
        if (
            reason_code
            == ExecuteToolHandover.Result.REASON_CANCELED_RECOVERED_TO_TRAY
        ):
            return "tray"
        return ""

    def _runtime_is_accepting(self) -> bool:
        with self._dispatch_lock:
            return self._runtime_accepting_commands

    def _record_admission_payload(
        self,
        payload: dict[str, Any],
        *,
        kind: str,
        controller_source: str | None = None,
    ) -> None:
        """Record readiness admission and compatibility telemetry leases.

        Controller-contract messages remain observable for route diagnostics and
        older integrations, but they are not execution authority.  Only an
        invalid integration-readiness lease may disarm dispatch admission.
        """

        if kind == "controller_contract":
            source = normalize_robot_endpoint_source(
                controller_source or self._robot_endpoint_source
            )
            with self._dispatch_lock:
                leases = getattr(self, "_controller_contract_leases", None)
                lease = (
                    leases[source]
                    if isinstance(leases, dict) and source in leases
                    else {
                        "last_accepted_stamp_sec": getattr(
                            self,
                            "_last_accepted_controller_contract_source_stamp_sec",
                            0.0,
                        )
                    }
                )
            max_age_sec = self._admission_lease_max_age_sec
            previous_stamp_sec = lease["last_accepted_stamp_sec"] or None
        else:
            max_age_sec = self._admission_lease_max_age_sec
            previous_stamp_sec = (
                self._last_accepted_integration_readiness_source_stamp_sec or None
            )
        stamp_sec, source_error = validate_source_stamp(
            payload,
            now_sec=time.time(),
            max_age_sec=max_age_sec,
            future_tolerance_sec=(
                self._admission_lease_source_future_tolerance_sec
            ),
            source_name=kind,
            previous_stamp_sec=previous_stamp_sec,
        )
        received_monotonic = time.monotonic()
        with self._dispatch_lock:
            if kind == "controller_contract":
                leases = getattr(self, "_controller_contract_leases", None)
                if isinstance(leases, dict) and source in leases:
                    lease = leases[source]
                    lease["payload"] = payload
                    lease["received_monotonic"] = received_monotonic
                    lease["source_error"] = source_error
                    if not source_error and stamp_sec is not None:
                        lease["last_accepted_stamp_sec"] = stamp_sec
                # Preserve the legacy inspection fields as the handover
                # contract. Existing diagnostics/tests remain meaningful,
                # while retraction checks use their own lease below.
                if source == self._robot_endpoint_source:
                    self._latest_controller_contract = payload
                    self._latest_controller_contract_received_monotonic = (
                        received_monotonic
                    )
                    self._latest_controller_contract_source_error = source_error
                    if not source_error and stamp_sec is not None:
                        self._last_accepted_controller_contract_source_stamp_sec = (
                            stamp_sec
                        )
            else:
                self._latest_integration_readiness = payload
                self._latest_integration_readiness_received_monotonic = (
                    received_monotonic
                )
                self._latest_integration_readiness_source_error = source_error
                if not source_error and stamp_sec is not None:
                    self._last_accepted_integration_readiness_source_stamp_sec = (
                        stamp_sec
                    )
            if source_error and kind != "controller_contract":
                self._dispatch_admission_armed = False
                self._dispatch_admission_disarmed_reason = source_error

    def _on_controller_contract(
        self,
        msg: String,
        source: str | None = None,
    ) -> None:
        if source is not None:
            try:
                normalized_source = normalize_robot_endpoint_source(source)
            except ValueError:
                return
            with self._dispatch_lock:
                if normalized_source not in {
                    self._robot_endpoint_source,
                    self._retraction_endpoint_source,
                }:
                    return
        try:
            payload = json.loads(msg.data)
        except (TypeError, ValueError):
            return
        if isinstance(payload, dict):
            self._record_admission_payload(
                payload,
                kind="controller_contract",
                controller_source=source,
            )

    def _on_integration_readiness(self, msg: String) -> None:
        try:
            payload = json.loads(msg.data)
        except (TypeError, ValueError):
            return
        if isinstance(payload, dict):
            self._record_admission_payload(payload, kind="integration_readiness")

    def _integration_readiness_lease_guard(self, route: str) -> str:
        with self._dispatch_lock:
            payload = self._latest_integration_readiness
            receipt_monotonic = self._latest_integration_readiness_received_monotonic
            source_error = self._latest_integration_readiness_source_error
        if not isinstance(payload, dict) or receipt_monotonic <= 0.0:
            return "integration_readiness_lease_missing"
        receipt_age_sec = time.monotonic() - receipt_monotonic
        if (
            receipt_age_sec < 0.0
            or receipt_age_sec > self._admission_lease_max_age_sec
        ):
            return "integration_readiness_lease_stale"
        if source_error:
            return source_error
        _, source_error = validate_source_stamp(
            payload,
            now_sec=time.time(),
            max_age_sec=self._admission_lease_max_age_sec,
            future_tolerance_sec=(
                self._admission_lease_source_future_tolerance_sec
            ),
            source_name="integration_readiness",
        )
        if source_error:
            return source_error
        if payload.get("schema") != _INTEGRATION_READINESS_SCHEMA:
            return "integration_readiness_schema_mismatch"
        details = payload.get("details")
        # Live runtime route control must never admit an observation produced
        # for the other endpoint family.  Keep the older static-route
        # readiness contract backward compatible: a legacy producer that has
        # no source field is still meaningful when the source cannot change.
        if bool(getattr(self, "_enable_runtime_route_control", False)):
            if not isinstance(details, dict):
                return "integration_readiness_details_missing"
            if (
                str(details.get("robot_endpoint_source", "")).strip().casefold()
                != self._robot_endpoint_source
            ):
                return "integration_readiness_endpoint_source_mismatch"
            if (
                str(details.get("retraction_endpoint_source", ""))
                .strip()
                .casefold()
                != self._retraction_endpoint_source
            ):
                return "integration_readiness_retraction_source_mismatch"
        elif isinstance(details, dict):
            observed_source = str(
                details.get("robot_endpoint_source", "")
            ).strip().casefold()
            if observed_source and observed_source != self._robot_endpoint_source:
                return "integration_readiness_endpoint_source_mismatch"
            observed_retraction_source = str(
                details.get("retraction_endpoint_source", "")
            ).strip().casefold()
            if (
                observed_retraction_source
                and observed_retraction_source != self._retraction_endpoint_source
            ):
                return "integration_readiness_retraction_source_mismatch"
        if payload.get("ready") is not True:
            return "integration_readiness_not_ready"
        checks = payload.get("checks")
        if not isinstance(checks, dict):
            return "integration_readiness_checks_missing"
        # Controller contracts are compatibility telemetry, not execution
        # authority.  Dispatch admission is based on the selected endpoint's
        # fresh readiness projection and the direct Action/Service availability
        # check performed by the caller.
        required_checks = {"contract_configuration"}
        if route == "tool_transfer":
            required_checks.add("tool_handover_action_server")
        if route == "retraction":
            required_checks.add("retraction_command_service")
        if any(checks.get(name) is not True for name in required_checks):
            return "integration_readiness_checks_not_ready"
        return ""

    def _dispatch_admission_guard(self, route: str, *, allow_stop: bool = False) -> str:
        """Require a fresh integration-readiness lease immediately before I/O.

        The sole exception is a retraction STOP command: suppressing a stop
        because telemetry/lease freshness is lost would make recovery less
        safe. A Service receipt still never claims physical stop completion.

        Controller-contract payloads remain available as diagnostics for
        backward compatibility, but their presence, freshness, and contents do
        not authorize or block dispatch.
        """

        if allow_stop or not bool(
            getattr(self, "_require_dispatch_admission_lease", False)
        ):
            return ""
        reason = self._integration_readiness_lease_guard(route)
        with self._dispatch_lock:
            self._dispatch_admission_armed = not bool(reason)
            self._dispatch_admission_disarmed_reason = reason
        return reason

    @staticmethod
    def _source_stamp_ns(msg: BedRobotArmStateArray) -> int | None:
        sec = int(msg.stamp.sec)
        nanosec = int(msg.stamp.nanosec)
        if sec < 0 or nanosec < 0 or nanosec >= 1_000_000_000:
            return None
        return sec * 1_000_000_000 + nanosec

    @staticmethod
    def _wall_time_ns() -> int:
        # The external controller contract uses wall-clock ROS time even when
        # Taskplanner itself is replaying against /clock.
        return time.time_ns()

    def _bed_robot_source_age_sec(self, source_stamp_ns: int) -> float:
        return (self._wall_time_ns() - source_stamp_ns) / 1_000_000_000.0

    @staticmethod
    def _bed_robot_snapshot_signature(
        procedure_type: str, states: dict[str, Any]
    ) -> tuple[Any, ...]:
        return (
            procedure_type,
            tuple(
                sorted(
                    (
                        arm_id,
                        arm.role.strip(),
                        arm.role_instance_id.strip(),
                        arm.state.strip(),
                        bool(arm.direct_teach_active),
                        arm.reason_code.strip(),
                    )
                    for arm_id, arm in states.items()
                )
            ),
        )

    def _on_bed_robot_status(self, msg: BedRobotArmStateArray) -> None:
        revision = int(msg.revision)
        source_stamp_ns = self._source_stamp_ns(msg)
        if source_stamp_ns is None or source_stamp_ns <= 0:
            self.get_logger().warning(
                "ignored bed robot arm status with invalid source stamp"
            )
            return
        source_age_sec = self._bed_robot_source_age_sec(source_stamp_ns)
        if (
            source_age_sec > self._bed_robot_source_max_age_sec
            or source_age_sec < -self._bed_robot_source_future_tolerance_sec
        ):
            self.get_logger().warning(
                "ignored stale or future-dated bed robot arm source stamp"
            )
            return
        procedure_type = msg.procedure_type.strip().casefold()
        expected_roles = _BED_ROBOT_PROCEDURE_LAYOUTS.get(procedure_type)
        if expected_roles is None:
            self.get_logger().warning(
                "ignored bed robot arm status with unsupported procedure_type"
            )
            return
        states: dict[str, Any] = {}
        for arm in msg.arms:
            arm_id = arm.arm_id.strip()
            role = arm.role.strip()
            role_instance_id = arm.role_instance_id.strip()
            state = arm.state.strip()
            if (
                not arm_id
                or arm_id not in {"arm_1", "arm_2"}
                or role != "retraction"
                or role_instance_id not in _RETRACTOR_ROLE_INSTANCES
                or state not in _BED_ROBOT_ARM_STATES
                or arm_id in states
            ):
                self.get_logger().warning(
                    "ignored invalid bed robot arm status snapshot"
                )
                return
            if bool(arm.direct_teach_active) != (state == "direct_teach"):
                self.get_logger().warning(
                    "ignored inconsistent direct-teach arm status"
                )
                return
            states[arm_id] = arm

        if not states:
            self.get_logger().warning("ignored empty bed robot arm status snapshot")
            return
        reported_roles = frozenset(
            arm.role_instance_id.strip() for arm in states.values()
        )
        if reported_roles != expected_roles or len(states) != len(expected_roles):
            self.get_logger().warning(
                "ignored bed robot arm status with invalid procedure layout"
            )
            return
        signature = self._bed_robot_snapshot_signature(procedure_type, states)
        restart_affected: list[ActiveAction | ActiveService] = []
        with self._dispatch_lock:
            previous_revision = self._bed_robot_revision
            previous_stamp_ns = self._bed_robot_source_stamp_ns
            if previous_stamp_ns is not None:
                if source_stamp_ns <= previous_stamp_ns:
                    self.get_logger().warning(
                        "ignored stale bed robot arm source stamp"
                    )
                    return
                if previous_revision is not None and revision == previous_revision:
                    if signature != self._bed_robot_signature:
                        self.get_logger().warning(
                            "ignored changed bed robot arm state without revision advance"
                        )
                        return
                    # A newer source stamp with an identical revision and payload
                    # is a valid controller heartbeat.
                elif previous_revision is not None and revision < previous_revision:
                    # The public message has no epoch field.  A strictly newer
                    # source stamp paired with a lower revision is the only
                    # document-field evidence available for a controller restart.
                    self._bed_robot_epoch += 1
                    self.get_logger().warning(
                        "accepted bed robot controller restart epoch from newer stamp"
                    )
                    restart_affected = [
                        active
                        for active in self._active_services.values()
                        if active.route == "retraction"
                    ]
                    if restart_affected:
                        self._runtime_accepting_commands = False
            self._bed_robot_revision = revision
            self._bed_robot_source_stamp_ns = source_stamp_ns
            self._bed_robot_signature = signature
            self._bed_robot_procedure_type = procedure_type
            self._bed_robot_received_monotonic = time.monotonic()
            self._bed_robot_states = states

        for active in restart_affected:
            self._publish_group_status(
                active.command,
                state="unknown",
                outcome="remote_state_unknown",
                terminal=False,
                success=False,
                reason_code="controller_restarted_during_command",
            )

    def _bed_robot_dispatch_guard(self, request: RetractionCommandRequest) -> str:
        # A stop must always be deliverable: stale telemetry is not a safe
        # reason to suppress a stop request.  The controller remains the final
        # authority for all Service command admission and execution.
        if request.command == RETRACTION_COMMAND_STOP_RETRACTION:
            return ""
        if not self._require_bed_robot_status:
            return ""
        with self._dispatch_lock:
            age = time.monotonic() - self._bed_robot_received_monotonic
            procedure_type = self._bed_robot_procedure_type
            states = dict(self._bed_robot_states)
            source_stamp_ns = self._bed_robot_source_stamp_ns
        if not states:
            return "bed_robot_status_missing"
        if age > self._bed_robot_status_timeout_sec:
            return "bed_robot_status_stale"
        if source_stamp_ns is None:
            return "bed_robot_source_stamp_missing"
        source_age_sec = self._bed_robot_source_age_sec(source_stamp_ns)
        if source_age_sec > self._bed_robot_source_max_age_sec:
            return "bed_robot_source_stamp_stale"
        if source_age_sec < -self._bed_robot_source_future_tolerance_sec:
            return "bed_robot_source_stamp_future"

        # The unified Service deliberately does not expose arm IDs, tool IDs,
        # direction vectors, or an execution state machine.  Only an adjust
        # request has enough public information for this client to identify a
        # target safely.  Other commands are admitted by the controller after
        # the generic fresh-telemetry check above.
        if request.command != RETRACTION_COMMAND_ADJUST_RETRACTION:
            return ""
        role_by_side_by_procedure = {
            "nephrectomy": {
                RETRACTION_TARGET_LEFT: "left_malleable",
                RETRACTION_TARGET_RIGHT: "right_malleable",
                RETRACTION_TARGET_BOTH: ("left_malleable", "right_malleable"),
            },
            "inguinal_hernia_repair": {
                RETRACTION_TARGET_LEFT: "left_army_navy",
                RETRACTION_TARGET_RIGHT: "right_army_navy",
                RETRACTION_TARGET_BOTH: ("left_army_navy", "right_army_navy"),
            },
        }
        role_by_side = role_by_side_by_procedure.get(procedure_type)
        if role_by_side is None:
            return "unsupported_procedure_operation"
        target_role = role_by_side.get(request.target_side)
        if not target_role:
            return "invalid_target_side"
        target_roles = (
            tuple(target_role)
            if isinstance(target_role, tuple)
            else (target_role,)
        )
        targets = [
            arm
            for arm in states.values()
            if arm.role_instance_id in target_roles
        ]
        ready_states = {"standby", "retracting"}

        if len(targets) != len(target_roles) or any(
            target is None for target in targets
        ):
            return "target_retractor_unavailable"
        for target in targets:
            if bool(target.direct_teach_active) or target.state == "direct_teach":
                return "direct_teach_active"
            if target.state not in ready_states:
                return f"arm_not_ready_{target.state}"
        return ""

    def _block_runtime_dispatch(self) -> None:
        with self._dispatch_lock:
            self._runtime_accepting_commands = False

    def _begin_action_dispatch(
        self,
        route: str,
        command: InternalSkillCommand | InternalGroupCommand,
        *,
        semantic_leg: tuple[str, str] | None = None,
        alternative_semantic_legs: tuple[tuple[str, str], ...] = (),
    ) -> str:
        with self._dispatch_lock:
            if not self._runtime_accepting_commands:
                return "runtime_not_accepting_commands"
            if any(
                active.command.command_id == command.command_id
                for active in self._active_actions.values()
            ) or any(
                active.command.command_id == command.command_id
                for active in self._active_services.values()
            ):
                return "duplicate_command"
            if route == "tool_transfer" and any(
                active.route == "tool_transfer"
                for active in self._active_actions.values()
            ):
                return "tool_transfer_busy"
            if not self._dispatch_ledger.reserve(
                command.command_id,
                explicit_request_generation=self._explicit_request_generation(command),
                semantic_leg=semantic_leg,
                alternative_semantic_legs=alternative_semantic_legs,
            ):
                return "duplicate_command"
            self._active_actions[self._action_key(route, command.command_id)] = ActiveAction(
                route=route,
                command=command,
                semantic_leg=semantic_leg,
                dispatch_epoch=int(getattr(self, "_dispatch_epoch", 0)),
            )
        return ""

    def _begin_service_dispatch(
        self,
        route: str,
        command: InternalGroupCommand,
        *,
        allow_when_runtime_not_accepting: bool = False,
    ) -> str:
        with self._dispatch_lock:
            if (
                not self._runtime_accepting_commands
                and not allow_when_runtime_not_accepting
            ):
                return "runtime_not_accepting_commands"
            if any(
                active.command.command_id == command.command_id
                for active in self._active_actions.values()
            ) or any(
                active.command.command_id == command.command_id
                for active in self._active_services.values()
            ):
                return "duplicate_command"
            if any(active.route == route for active in self._active_services.values()):
                return f"{route}_busy"
            if not self._dispatch_ledger.reserve(command.command_id):
                return "duplicate_command"
            self._active_services[self._action_key(route, command.command_id)] = ActiveService(
                route=route,
                command=command,
            )
        return ""

    def _action_is_cancelled(self, route: str, command_id: str) -> bool:
        with self._dispatch_lock:
            active = self._active_actions.get(self._action_key(route, command_id))
            return active is None or active.cancelled

    def _tool_transfer_action_state(self, command_id: str) -> tuple[bool, bool]:
        """Return whether the Goal is tracked and whether Cancel was requested."""

        with self._dispatch_lock:
            active = self._active_actions.get(
                self._action_key("tool_transfer", command_id)
            )
            if active is None:
                return False, False
            return True, bool(active.cancelled)

    def _tool_transfer_action_snapshot(
        self,
        command_id: str,
    ) -> tuple[bool, bool, tuple[str, str] | None, int]:
        """Return immutable dispatch metadata needed by terminal/I-O gates."""

        with self._dispatch_lock:
            active = self._active_actions.get(
                self._action_key("tool_transfer", command_id)
            )
            if active is None:
                return False, False, None, -1
            return (
                True,
                bool(active.cancelled),
                active.semantic_leg,
                int(active.dispatch_epoch),
            )

    def _tool_transfer_dispatch_is_current(
        self,
        command_id: str,
        *,
        expected_epoch: int,
    ) -> bool:
        """Fail closed when stop/pause invalidated a reserved dispatch."""

        with self._dispatch_lock:
            active = self._active_actions.get(
                self._action_key("tool_transfer", command_id)
            )
            return bool(
                active is not None
                and not active.cancelled
                and self._runtime_accepting_commands
                and int(active.dispatch_epoch) == int(expected_epoch)
                and int(getattr(self, "_dispatch_epoch", 0))
                == int(expected_epoch)
            )

    def _set_tool_transfer_goal_handle(
        self, command_id: str, goal_handle: Any
    ) -> tuple[bool, bool]:
        """Attach a handle even when Cancel arrived before goal acceptance."""

        with self._dispatch_lock:
            active = self._active_actions.get(
                self._action_key("tool_transfer", command_id)
            )
            if active is None:
                return False, False
            active.goal_handle = goal_handle
            return True, bool(active.cancelled)

    def _action_state(self, route: str, command_id: str) -> tuple[bool, bool]:
        with self._dispatch_lock:
            active = self._active_actions.get(self._action_key(route, command_id))
            if active is None:
                return False, False
            return True, bool(active.cancelled)

    def _set_action_goal_handle(
        self, route: str, command_id: str, goal_handle: Any
    ) -> tuple[bool, bool]:
        with self._dispatch_lock:
            active = self._active_actions.get(self._action_key(route, command_id))
            if active is None:
                return False, False
            active.goal_handle = goal_handle
            return True, bool(active.cancelled)

    def _clear_action(self, route: str, command_id: str) -> None:
        with self._dispatch_lock:
            self._active_actions.pop(self._action_key(route, command_id), None)

    def _clear_service(self, route: str, command_id: str) -> None:
        with self._dispatch_lock:
            self._active_services.pop(self._action_key(route, command_id), None)

    def _queue_voice_tool_transfer_preemption(
        self,
        command: InternalSkillCommand,
        request: ToolHandoverRequest,
    ) -> bool:
        """Queue a voice correction behind the active tool-transfer Goal.

        A same-tool automatic return is allowed to reach its natural terminal;
        other active work keeps the existing cancel-and-recover preemption.

        Returns ``True`` when the command was fully handled (queued, replaced,
        or duplicate-suppressed).  A non-voice command always returns ``False``
        and therefore follows the ordinary busy-lane path.
        """

        if not command.voice_backed:
            return False

        active_command: InternalSkillCommand | InternalGroupCommand | None = None
        replaced: QueuedVoiceToolTransfer | None = None
        cancel_handle: Any | None = None
        request_cancel = False
        duplicate = False
        wait_for_predecessor_terminal = False
        same_instrument_predecessor = False
        generation = self._explicit_request_generation(command)
        semantic_leg = self._tool_transfer_semantic_leg(request)
        with self._dispatch_lock:
            active = next(
                (
                    candidate
                    for candidate in self._active_actions.values()
                    if candidate.route == "tool_transfer"
                ),
                None,
            )
            if active is None:
                return False

            same_instrument_predecessor = bool(
                isinstance(active.command, InternalSkillCommand)
                and self._same_instrument_instance(active.command, command)
            )
            wait_for_predecessor_terminal = (
                same_instrument_predecessor
                and self._voice_waits_for_active_auto_return(active, command)
            )

            active_generation = self._explicit_request_generation(active.command)
            active_semantic_leg = active.semantic_leg
            if (
                generation is not None
                and generation > 0
                and generation == active_generation
                and active_semantic_leg is not None
                and semantic_leg != active_semantic_leg
            ):
                # This is the next leg of the same compound request, not a
                # newer voice correction.  Preserve the active Goal and let
                # the ordinary busy/retry path submit this leg after terminal.
                return False
            queued = getattr(self, "_queued_voice_tool_transfer", None)
            queued_generation = (
                self._explicit_request_generation(queued.command)
                if queued is not None
                else None
            )
            queued_semantic_leg = (
                self._tool_transfer_semantic_leg(queued.request)
                if queued is not None
                else None
            )
            duplicate = (
                active.command.command_id == command.command_id
                or self._dispatch_ledger.is_reserved(
                    command.command_id,
                    explicit_request_generation=generation,
                    semantic_leg=semantic_leg,
                )
                or (
                    queued is not None
                    and (
                        queued.command.command_id == command.command_id
                        or (
                            generation is not None
                            and generation > 0
                            and generation == queued_generation
                            and semantic_leg == queued_semantic_leg
                        )
                    )
                )
            )
            if not duplicate:
                replaced = queued
                self._queued_voice_tool_transfer = QueuedVoiceToolTransfer(
                    command=command,
                    request=request,
                    wait_for_predecessor_terminal=wait_for_predecessor_terminal,
                    same_instrument_predecessor=same_instrument_predecessor,
                    original_semantic_leg=semantic_leg,
                )
                active_command = active.command
                if not wait_for_predecessor_terminal and not active.cancelled:
                    active.cancelled = True
                    request_cancel = True
                    cancel_handle = active.goal_handle

        if duplicate:
            self._publish_skill_status(
                command,
                state="duplicate_suppressed",
                success=False,
                reason_code="duplicate_voice_preemption_request",
            )
            return True

        if replaced is not None:
            self._publish_skill_status(
                replaced.command,
                state="cancelled",
                success=False,
                reason_code="superseded_by_newer_voice_request",
            )
        self._publish_skill_status(
            command,
            state="queued",
            success=True,
            reason_code=(
                "waiting_for_active_auto_return_terminal"
                if wait_for_predecessor_terminal
                else "waiting_for_voice_preemption_recovery"
            ),
        )
        if request_cancel and isinstance(active_command, InternalSkillCommand):
            self._publish_skill_status(
                active_command,
                state="cancel_requested",
                success=False,
                reason_code="cancel_requested_by_voice_preemption",
            )
        if cancel_handle is not None:
            try:
                cancel_handle.cancel_goal_async()
            except Exception:  # pragma: no cover - ROS transport failure
                self.get_logger().warning(
                    "failed to cancel tool_transfer command for voice preemption"
                )
        return True

    def _finish_tool_transfer_action(
        self,
        command_id: str,
        *,
        safe_terminal: bool,
        controller_confirmed_tool_location: str = "",
    ) -> None:
        """Release one Goal and atomically reserve the latest queued voice.

        Only queue extraction and replacement lane registration happen under
        the lock. Server waits and Goal submission happen after unlock, with a
        dispatch-epoch check that gives stop/pause priority.
        """

        rejected: QueuedVoiceToolTransfer | None = None
        rejected_reason = ""
        rejected_state = "rejected"
        satisfied: QueuedVoiceToolTransfer | None = None
        reserved: tuple[QueuedVoiceToolTransfer, int] | None = None
        with self._dispatch_lock:
            self._active_actions.pop(
                self._action_key("tool_transfer", command_id), None
            )
            queued = getattr(self, "_queued_voice_tool_transfer", None)
            if queued is None:
                return
            if any(
                active.route == "tool_transfer"
                for active in self._active_actions.values()
            ):
                # A second active Goal would violate the lane invariant.  Keep
                # the latest voice queued and fail closed instead of guessing.
                return
            self._queued_voice_tool_transfer = None
            if not safe_terminal:
                rejected = queued
                rejected_reason = (
                    "deferred_voice_predecessor_not_safely_terminal"
                    if queued.wait_for_predecessor_terminal
                    else "voice_preemption_predecessor_not_safely_terminal"
                )
            elif not self._runtime_accepting_commands:
                rejected = queued
                rejected_reason = "runtime_not_accepting_commands"
                rejected_state = "cancelled"
            else:
                if queued.same_instrument_predecessor:
                    resolution, resolved = self._resolve_voice_after_predecessor(
                        queued,
                        source_location=controller_confirmed_tool_location,
                    )
                    if resolution == "satisfied":
                        original_leg = (
                            queued.original_semantic_leg
                            or self._tool_transfer_semantic_leg(queued.request)
                        )
                        self._dispatch_ledger.reserve(
                            queued.command.command_id,
                            explicit_request_generation=(
                                self._explicit_request_generation(queued.command)
                            ),
                            semantic_leg=original_leg,
                        )
                        satisfied = queued
                    elif resolution != "dispatch" or resolved is None:
                        rejected = queued
                        rejected_reason = (
                            "voice_predecessor_location_not_dispatchable"
                        )
                    else:
                        queued = resolved
                if rejected is None and satisfied is None:
                    effective_leg = self._tool_transfer_semantic_leg(
                        queued.request
                    )
                    original_leg = queued.original_semantic_leg or effective_leg
                    alternatives = (
                        (original_leg,)
                        if original_leg != effective_leg
                        else ()
                    )
                    dispatch_error = self._begin_action_dispatch(
                        "tool_transfer",
                        queued.command,
                        semantic_leg=effective_leg,
                        alternative_semantic_legs=alternatives,
                    )
                    if dispatch_error:
                        rejected = queued
                        rejected_reason = dispatch_error
                        if dispatch_error == "runtime_not_accepting_commands":
                            rejected_state = "cancelled"
                        elif dispatch_error == "duplicate_command":
                            rejected_state = "duplicate_suppressed"
                    else:
                        active = self._active_actions.get(
                            self._action_key(
                                "tool_transfer",
                                queued.command.command_id,
                            )
                        )
                        if active is None:
                            rejected = queued
                            rejected_reason = "reserved_dispatch_missing"
                        else:
                            reserved = (queued, int(active.dispatch_epoch))

        if rejected is not None:
            self._publish_skill_status(
                rejected.command,
                state=rejected_state,
                success=False,
                reason_code=rejected_reason,
            )
        if satisfied is not None:
            self._publish_skill_status(
                satisfied.command,
                state="duplicate_suppressed",
                success=True,
                reason_code="voice_request_satisfied_by_predecessor",
            )
        if reserved is not None:
            queued, expected_epoch = reserved
            self._dispatch_reserved_tool_transfer(
                queued.command,
                queued.request,
                expected_epoch=expected_epoch,
                server_ready=False,
            )

    def _finish_tool_transfer_before_send_if_cancelled(
        self,
        command: InternalSkillCommand,
        *,
        expected_epoch: int | None = None,
    ) -> bool:
        """Close a locally canceled lane before any Action Goal is submitted."""

        tracked, cancelled, semantic_leg, _active_epoch = (
            self._tool_transfer_action_snapshot(command.command_id)
        )
        dispatch_is_current = bool(
            tracked
            and not cancelled
            and (
                expected_epoch is None
                or self._tool_transfer_dispatch_is_current(
                    command.command_id,
                    expected_epoch=expected_epoch,
                )
            )
        )
        if dispatch_is_current:
            return False
        reason_code = ExecuteToolHandover.Result.REASON_CANCELED_SOURCE_UNCHANGED
        self._publish_skill_status(
            command,
            state=ExecuteToolHandover.Result.FINAL_CANCELED,
            success=False,
            reason_code=reason_code,
            progress=1.0,
        )
        self._publish_execution_trace(
            command_id=command.command_id,
            route="tool_transfer",
            transport="action",
            endpoint=self._execution_trace_endpoint("tool_transfer"),
            stage="canceled",
            dispatch_submitted=False,
            terminal=True,
            evidence="not_dispatched",
            reason_code=reason_code,
        )
        self._finish_tool_transfer_action(
            command.command_id,
            safe_terminal=True,
            controller_confirmed_tool_location=(
                semantic_leg[0] if semantic_leg is not None else ""
            ),
        )
        return True

    def _on_control(self, msg: String) -> None:
        control, _, detail = msg.data.partition(":")
        control = control.strip().lower()
        signature = (control, detail.strip())
        if control in {"start", "start_runtime", "start_actors", "pause", "resume", "stop"}:
            if signature == getattr(
                self, "_last_lifecycle_control_signature", None
            ):
                return
            self._last_lifecycle_control_signature = signature
        if control in {"start", "start_actors", "resume"}:
            with self._dispatch_lock:
                self._runtime_accepting_commands = True
                if not getattr(self, "_run_endpoint_source", ""):
                    self._run_endpoint_source = str(
                        getattr(
                            self,
                            "_robot_endpoint_source",
                            EXTERNAL_ENDPOINT_SOURCE,
                        )
                    )
                    self._run_retraction_source = str(
                        getattr(
                            self,
                            "_retraction_endpoint_source",
                            EXTERNAL_ENDPOINT_SOURCE,
                        )
                    )
                self._route_initialization_state = "running"
            self._publish_execution_route_state()
            return
        if control == "start_runtime":
            with self._dispatch_lock:
                self._runtime_accepting_commands = False
                # Start admission and the DT entering its initial running
                # frame both precede actor dispatch. Latch here so a failed
                # start cannot leave a virtual/real source ambiguous.
                if not getattr(self, "_run_endpoint_source", ""):
                    self._run_endpoint_source = str(
                        getattr(
                            self,
                            "_robot_endpoint_source",
                            EXTERNAL_ENDPOINT_SOURCE,
                        )
                    )
                    self._run_retraction_source = str(
                        getattr(
                            self,
                            "_retraction_endpoint_source",
                            EXTERNAL_ENDPOINT_SOURCE,
                        )
                    )
                self._route_initialization_state = "running"
            self._publish_execution_route_state()
            return
        if control not in {"pause", "stop", "reset"}:
            return

        if control == "reset":
            self._last_lifecycle_control_signature = None

        queued_voice: QueuedVoiceToolTransfer | None = None
        with self._dispatch_lock:
            self._runtime_accepting_commands = False
            self._dispatch_epoch = int(getattr(self, "_dispatch_epoch", 0)) + 1
            actions = [
                active
                for active in self._active_actions.values()
                if not active.cancelled
            ]
            services = [
                active
                for active in self._active_services.values()
                if not active.cancelled
            ]
            for active in actions:
                active.cancelled = True
            for active in services:
                active.cancelled = True
            queued_voice = getattr(self, "_queued_voice_tool_transfer", None)
            self._queued_voice_tool_transfer = None
            if control in {"stop", "reset"}:
                self._run_endpoint_source = ""
                self._run_retraction_source = ""
                self._route_initialization_state = (
                    "reset" if control == "reset" else "stopped"
                )
                self._route_initialization_revision = int(
                    getattr(self, "_route_initialization_revision", 0)
                ) + 1
            if control == "reset":
                self._dispatch_ledger.clear()
                self._bed_robot_revision = None
                self._bed_robot_source_stamp_ns = None
                self._bed_robot_epoch = 0
                self._bed_robot_signature = None
                self._bed_robot_procedure_type = ""
                self._bed_robot_received_monotonic = 0.0
                self._bed_robot_states = {}

        self._publish_execution_route_state()

        for active in actions:
            if isinstance(active.command, InternalSkillCommand):
                self._publish_skill_status(
                    active.command,
                    state="cancel_requested",
                    success=False,
                    reason_code="cancel_requested_by_runtime_control",
                )
            else:
                self._publish_group_status(
                    active.command,
                    state="cancel_requested",
                    outcome="cancel_requested",
                    terminal=False,
                    success=False,
                    reason_code="cancel_requested_by_runtime_control",
                )
            if active.goal_handle is not None:
                try:
                    active.goal_handle.cancel_goal_async()
                except Exception:  # pragma: no cover - ROS transport failure
                    self.get_logger().warning(
                        f"failed to cancel {active.route} command {active.command.command_id}"
                    )

        if queued_voice is not None:
            self._publish_skill_status(
                queued_voice.command,
                state="cancelled",
                success=False,
                reason_code="cancelled_by_runtime_control_before_dispatch",
            )

        # ROS services are not cancellable after dispatch.  Keep each call
        # tracked until its real response arrives.
        for active in services:
            self._publish_group_status(
                active.command,
                state=(
                    "unknown"
                    if active.dispatched
                    else "cancel_requested"
                ),
                outcome=(
                    "awaiting_service_admission_after_stop"
                    if active.dispatched
                    else "cancel_requested"
                ),
                terminal=False,
                success=False,
                reason_code=(
                    "service_not_cancellable_awaiting_response"
                    if active.dispatched
                    else "cancel_requested_before_service_dispatch"
                ),
            )

    @staticmethod
    def _skill_from_msg(msg: SkillCommand) -> InternalSkillCommand:
        return InternalSkillCommand(
            command_id=msg.command_id.strip() or uuid.uuid4().hex,
            action=msg.action.strip(),
            instrument_id=msg.instrument_id.strip(),
            instrument_instance_id=msg.instrument_instance_id.strip(),
            source_location_type=msg.source_location_type.strip(),
            source_location_id=msg.source_location_id.strip(),
            target_location_type=msg.target_location_type.strip(),
            target_location_id=msg.target_location_id.strip(),
            arm=msg.arm.strip(),
            request_generation=int(msg.request_generation),
            procedure_run_id=str(
                getattr(msg, "procedure_run_id", "")
            ).strip(),
            implicit_request_generation=int(
                getattr(msg, "implicit_request_generation", 0)
            ),
            rationale=msg.rationale,
            target_owner=msg.target_owner,
            cleaning_required=bool(msg.cleaning_required),
            mode=msg.mode,
            voice_backed=bool(getattr(msg, "voice_backed", False)),
        )

    @staticmethod
    def _group_from_msg(msg: BedRobotArmGroupCommand) -> InternalGroupCommand:
        command_id = msg.command_id.strip() or uuid.uuid4().hex
        return InternalGroupCommand(
            request_id=msg.request_id.strip() or f"request-{command_id}",
            command_id=command_id,
            group_id=msg.group_id.strip(),
            operation=msg.operation.strip(),
            arm_id=msg.arm_id.strip(),
            target_tool_id=msg.target_tool_id.strip(),
            adjustment_mode=msg.adjustment_mode.strip(),
            target_retractor_id=msg.target_retractor_id.strip(),
            direction_frame=msg.direction_frame.strip(),
            direction=msg.direction.strip(),
            axis=msg.axis.strip(),
            distance_mm=float(msg.distance_mm),
            end_effector_profile=msg.end_effector_profile.strip(),
            distance_origin=msg.distance_origin.strip(),
            raw_distance_text=msg.raw_distance_text,
            rationale=msg.rationale,
            confidence=float(msg.confidence),
        )

    def _publish_skill_status(
        self,
        command: InternalSkillCommand,
        *,
        state: str,
        success: bool,
        reason_code: str,
        progress: float = 0.0,
    ) -> None:
        status = SkillStatus()
        status.stamp = self._stamp()
        status.command_id = command.command_id
        status.action = command.action
        status.instrument_id = command.instrument_id
        status.state = state
        status.success = bool(success)
        status.message = reason_code
        status.arm = command.arm
        status.source_location_id = command.source_location_id
        status.source_location_type = command.source_location_type
        status.target_location_id = command.target_location_id
        status.target_location_type = command.target_location_type
        status.target_owner = command.target_owner
        status.cleaning_required = bool(command.cleaning_required)
        status.mode = command.mode
        status.progress = max(0.0, min(1.0, float(progress)))
        status.elapsed_sec = 0.0
        status.remaining_sec = 0.0
        self._skill_status_pub.publish(status)

    def _publish_group_status(
        self,
        command: InternalGroupCommand,
        *,
        state: str,
        outcome: str,
        terminal: bool,
        success: bool,
        reason_code: str,
        progress: float = 0.0,
    ) -> None:
        status = BedRobotArmGroupStatus()
        status.stamp = self._stamp()
        status.request_id = command.request_id
        status.command_id = command.command_id
        status.group_id = command.group_id
        status.operation = command.operation
        status.arm_id = command.arm_id
        status.target_tool_id = command.target_tool_id
        status.adjustment_mode = command.adjustment_mode
        status.target_retractor_id = command.target_retractor_id
        status.direction_frame = command.direction_frame
        status.state = state
        status.outcome = outcome
        status.terminal = bool(terminal)
        status.success = bool(success)
        status.message = reason_code
        status.direction = command.direction
        status.axis = command.axis
        status.distance_mm = float(command.distance_mm)
        status.distance_origin = command.distance_origin
        status.raw_distance_text = command.raw_distance_text
        # Unified-Service admission does not verify physical attachment, so
        # never expose the requested profile as a controller-confirmed fact.
        status.end_effector_profile = (
            ""
            if command.operation == "change_end_effector"
            else command.end_effector_profile
        )
        status.confidence = float(command.confidence)
        status.progress = max(0.0, min(1.0, float(progress)))
        status.elapsed_sec = 0.0
        status.remaining_sec = 0.0
        status.error_code = "" if success else reason_code
        status.rejection_reason = reason_code if outcome == "rejected" else ""
        self._group_status_pub.publish(status)

    @staticmethod
    def _bounded_trace_text(value: object, *, limit: int) -> str:
        """Return one-line, bounded trace metadata safe for an observer feed."""

        text = " ".join(str(value or "").split())
        return text[:limit]

    @staticmethod
    def _bounded_trace_reason_code(value: object) -> str:
        """Keep the observer channel code-only even for an untrusted result."""

        code = str(value or "").strip()
        if not code:
            return ""
        if not all(
            character in _EXECUTION_TRACE_REASON_CODE_CHARACTERS
            for character in code
        ):
            return "unrecognized_reason_code"
        return code[:_EXECUTION_TRACE_MAX_REASON_CHARS]

    def _execution_trace_endpoint(self, route: str) -> str:
        """Return a safe endpoint label even for a partial unit-test bridge."""

        if route == "tool_transfer":
            return str(
                getattr(self, "_tool_transfer_endpoint", "/surgery/tool_handover")
            )
        return str(
            getattr(
                self,
                "_retraction_service_name",
                "/surgery/retraction/command",
            )
        )

    def _publish_execution_trace(
        self,
        *,
        command_id: str,
        route: str,
        transport: str,
        endpoint: str,
        stage: str,
        dispatch_submitted: bool,
        terminal: bool,
        evidence: str,
        reason_code: str,
        retraction_request: RetractionCommandRequest | None = None,
    ) -> None:
        """Publish a bounded fact about an outbound Action/Service lifecycle.

        The event is deliberately not a command channel and carries no request
        parameters except for a bounded, typed copy of the retraction Service
        payload.  In particular, a Service ``accepted`` event represents a
        controller admission receipt only; consumers must not render it as
        physical completion.
        """

        publisher = getattr(self, "_execution_trace_pub", None)
        if publisher is None:
            return
        # Observability must never affect Action/Service dispatch.  In
        # particular, a transient UI-topic publisher failure must not prevent
        # the caller from registering a result callback after a real send.
        try:
            normalized_transport = str(transport).strip().lower()
            normalized_stage = str(stage).strip().lower()
            normalized_evidence = str(evidence).strip().lower()
            if normalized_transport not in _EXECUTION_TRACE_TRANSPORTS:
                normalized_transport = "service"
            if normalized_stage not in _EXECUTION_TRACE_STAGES:
                normalized_stage = "unknown"
            if normalized_evidence not in _EXECUTION_TRACE_EVIDENCE:
                normalized_evidence = "response_invalid"
            with self._dispatch_lock:
                sequence = int(getattr(self, "_execution_trace_sequence", 0)) + 1
                self._execution_trace_sequence = sequence
                endpoint_source = str(
                    getattr(self, "_run_endpoint_source", "")
                    or getattr(self, "_robot_endpoint_source", EXTERNAL_ENDPOINT_SOURCE)
                )
            trace = ExecutionTrace()
            trace.stamp = self._stamp()
            trace.sequence = sequence
            trace.command_id = self._bounded_trace_text(
                command_id, limit=_EXECUTION_TRACE_MAX_COMMAND_ID_CHARS
            )
            trace.route = self._bounded_trace_text(
                route, limit=_EXECUTION_TRACE_MAX_ROUTE_CHARS
            )
            trace.transport = normalized_transport
            trace.endpoint = self._bounded_trace_text(
                endpoint, limit=_EXECUTION_TRACE_MAX_ENDPOINT_CHARS
            )
            if hasattr(trace, "endpoint_source"):
                trace.endpoint_source = self._bounded_trace_text(
                    endpoint_source,
                    limit=16,
                )
            trace.stage = normalized_stage
            trace.dispatch_submitted = bool(dispatch_submitted)
            trace.terminal = bool(terminal)
            trace.evidence = normalized_evidence
            trace.reason_code = self._bounded_trace_reason_code(reason_code)
            # ``hasattr`` keeps source-level tests and mixed-overlay startup
            # compatible until surgical_msgs has been rebuilt.  Once the new
            # message is generated, every retraction lifecycle row carries the
            # same controller-facing request values.
            if hasattr(trace, "retraction_command"):
                trace.retraction_command = int(
                    retraction_request.command if retraction_request is not None else 0
                )
            if hasattr(trace, "retraction_target_side"):
                trace.retraction_target_side = int(
                    retraction_request.target_side
                    if retraction_request is not None
                    else 0
                )
            if hasattr(trace, "retraction_distance_m"):
                trace.retraction_distance_m = float(
                    retraction_request.distance_m
                    if retraction_request is not None
                    else 0.0
                )
            publisher.publish(trace)
        except Exception:  # pragma: no cover - ROS publisher transport failure
            logger = getattr(self, "get_logger", None)
            if callable(logger):
                try:
                    logger().warning("execution trace publish failed")
                except Exception:
                    pass

    @staticmethod
    def _tool_transfer_goal(
        request: ToolHandoverRequest,
    ) -> ExecuteToolHandover.Goal:
        goal = ExecuteToolHandover.Goal()
        goal.command_id = request.command_id
        goal.instrument_id = request.instrument_id
        goal.instrument_instance_id = request.instrument_instance_id
        goal.source_location = request.source_location
        goal.target_location = request.target_location
        return goal

    def _public_instrument_identity(
        self,
        command: InternalSkillCommand,
    ) -> tuple[str, str]:
        internal_id = self._procedure_spec.resolve_instrument_alias(
            command.instrument_id
        )
        if not internal_id:
            return "", ""
        instrument_name = self._instrument_names.get(internal_id, "").strip()
        instance_id = public_instrument_instance_id(
            internal_instrument_id=internal_id,
            internal_instance_id=command.instrument_instance_id,
            instrument_name=instrument_name,
        )
        return instrument_name, instance_id

    def _retraction_service_request(
        self,
        request: RetractionCommandRequest,
    ) -> ExecuteRetractionCommand.Request:
        service_request = ExecuteRetractionCommand.Request()
        service_request.protocol_version = (
            ExecuteRetractionCommand.Request.PROTOCOL_VERSION_V1
        )
        service_request.source_id = self._retraction_source_id
        service_request.command_id = request.command_id
        service_request.command = int(request.command)
        service_request.target_side = int(request.target_side)
        service_request.distance_m = float(request.distance_m)
        return service_request

    def _publish_tool_transfer_completed_events(
        self,
        command: InternalSkillCommand,
        *,
        final_state: str,
        reason_code: str,
        semantic_leg: tuple[str, str] | None = None,
    ) -> None:
        """Project every controller-confirmed public Action leg into the DT.

        The semantic leg comes from the exact admitted Goal retained in the
        active-action ledger. It is stronger completion evidence than the
        planner action label, which may already have been superseded locally.
        """

        leg = tuple(
            str(location).strip().casefold()
            for location in (semantic_leg or ("", ""))
        )

        if leg in {("tray", "robot"), ("mayo", "robot")}:
            self._publish_tool_prepared_event(
                command,
                final_state=final_state,
                reason_code=reason_code,
                semantic_leg=semantic_leg,
            )
            return

        if leg == ("robot", "mayo"):
            # ``return_unused_preposition`` is intentionally the fast
            # robot-to-Mayo leg used to free the single preparation hand before
            # selecting another tool.  Never rebase this completed Action onto
            # the slower home/tray route from stale planner metadata.
            target_location_id = "mayo_stand"
            target_location_type = "mayo_stand"
            returned = self._make_tool_transfer_event(
                command,
                event_type="UnusedPrepositionReturned",
                location_id=target_location_id,
                location_type=target_location_type,
                source_location_id="robot",
                source_location_type="robot",
                target_location_id=target_location_id,
                target_location_type=target_location_type,
                final_state=final_state,
                reason_code=reason_code,
                authoritative_controller_completion=True,
                controller_semantic_leg=semantic_leg,
                controller_projection_step="unused_preposition_returned",
                controller_projection_index=1,
                controller_projection_count=1,
            )
            self._skill_event_pub.publish(returned)
            return

        if leg == ("mayo", "tray"):
            retrieved = self._make_tool_transfer_event(
                command,
                event_type="ToolRetrievedFromMayo",
                location_id="robot_left_hand",
                location_type="robot_left_hand",
                source_location_id=command.source_location_id or "mayo",
                source_location_type=command.source_location_type or "mayo",
                target_location_id="robot_left_hand",
                target_location_type="robot_left_hand",
                final_state=final_state,
                reason_code=reason_code,
                authoritative_controller_completion=True,
                controller_semantic_leg=semantic_leg,
                controller_projection_step="retrieved_from_mayo",
                controller_projection_index=1,
                controller_projection_count=2,
            )
            self._skill_event_pub.publish(retrieved)
            returned = self._make_tool_transfer_event(
                command,
                event_type="ToolReturnedToTray",
                location_id=command.target_location_id or "tray",
                location_type=command.target_location_type or "tray",
                source_location_id="robot_left_hand",
                source_location_type="robot_left_hand",
                target_location_id=command.target_location_id or "tray",
                target_location_type=command.target_location_type or "tray",
                final_state=final_state,
                reason_code=reason_code,
                authoritative_controller_completion=True,
                controller_semantic_leg=semantic_leg,
                controller_projection_step="returned_to_tray",
                controller_projection_index=2,
                controller_projection_count=2,
            )
            self._skill_event_pub.publish(returned)
            return

        if leg == ("robot", "tray"):
            # This is the explicit controller recovery leg in the public
            # Action contract. It is not the normal unused-preposition policy,
            # which is always robot-to-Mayo above.
            returned = self._make_tool_transfer_event(
                command,
                event_type="ToolReturnedToTray",
                location_id=command.target_location_id or "tray",
                location_type=command.target_location_type or "tray",
                source_location_id="robot",
                source_location_type="robot",
                target_location_id=command.target_location_id or "tray",
                target_location_type=command.target_location_type or "tray",
                final_state=final_state,
                reason_code=reason_code,
                authoritative_controller_completion=True,
                controller_semantic_leg=leg,
                controller_projection_step="returned_to_tray",
                controller_projection_index=1,
                controller_projection_count=1,
            )
            self._skill_event_pub.publish(returned)
            return

        if leg not in {("tray", "surgeon"), ("robot", "surgeon")}:
            raise ValueError(f"unsupported completed tool-transfer leg: {leg!r}")

        event = self._make_tool_transfer_event(
            command,
            event_type="ToolHandoverCompleted",
            location_id=command.target_location_id,
            location_type=command.target_location_type,
            source_location_id=command.source_location_id,
            source_location_type=command.source_location_type,
            target_location_id=command.target_location_id,
            target_location_type=command.target_location_type,
            final_state=final_state,
            reason_code=reason_code,
            authoritative_controller_completion=True,
            controller_semantic_leg=semantic_leg,
            controller_projection_step="handover_completed",
            controller_projection_index=1,
            controller_projection_count=1,
        )
        self._skill_event_pub.publish(event)

    def _publish_tool_transfer_cancel_reconciliation(
        self,
        command: InternalSkillCommand,
        *,
        final_state: str,
        reason_code: str,
    ) -> None:
        """Reconcile a controller-verified Cancel outcome before lane release.

        ``canceled_source_unchanged`` deliberately emits no inventory event:
        the public Action contract guarantees that the instrument stayed at
        its source.  ``canceled_recovered_to_tray`` means the controller
        verified a compensating placement at its generic tray recovery pose,
        so publish the same robot-to-tray correction consumed by the Digital
        Twin for an unused held tool.  Any other reason is not a safe Cancel
        terminal and must never reach this helper.
        """

        if final_state != ExecuteToolHandover.Result.FINAL_CANCELED:
            raise ValueError("cancel reconciliation requires a canceled result")
        if (
            reason_code
            == ExecuteToolHandover.Result.REASON_CANCELED_SOURCE_UNCHANGED
        ):
            return
        if (
            reason_code
            != ExecuteToolHandover.Result.REASON_CANCELED_RECOVERED_TO_TRAY
        ):
            raise ValueError("unsupported tool-transfer cancel reconciliation")

        returned = self._make_tool_transfer_event(
            command,
            event_type="UnusedPrepositionReturned",
            location_id="tray",
            location_type="tray",
            source_location_id="robot",
            source_location_type="robot",
            target_location_id="tray",
            target_location_type="tray",
            final_state=final_state,
            reason_code=reason_code,
        )
        returned.status = "returned"
        self._skill_event_pub.publish(returned)

    def _publish_tool_prepared_event(
        self,
        command: InternalSkillCommand,
        *,
        final_state: str,
        reason_code: str,
        semantic_leg: tuple[str, str] | None = None,
    ) -> None:
        """Record a controller-confirmed stable hold without asserting an arm."""

        event = self._make_tool_transfer_event(
            command,
            event_type="ToolPrepared",
            location_id="robot",
            location_type="robot",
            source_location_id=command.source_location_id,
            source_location_type=command.source_location_type,
            target_location_id="robot",
            target_location_type="robot",
            final_state=final_state,
            reason_code=reason_code,
            authoritative_controller_completion=True,
            controller_semantic_leg=semantic_leg,
            controller_projection_step="prepared",
            controller_projection_index=1,
            controller_projection_count=1,
        )
        event.status = "prepared"
        self._skill_event_pub.publish(event)

    def _make_tool_transfer_event(
        self,
        command: InternalSkillCommand,
        *,
        event_type: str,
        location_id: str,
        location_type: str,
        source_location_id: str,
        source_location_type: str,
        target_location_id: str,
        target_location_type: str,
        final_state: str,
        reason_code: str,
        authoritative_controller_completion: bool = False,
        controller_semantic_leg: tuple[str, str] | None = None,
        controller_projection_step: str = "",
        controller_projection_index: int = 0,
        controller_projection_count: int = 0,
    ) -> TwinEvent:
        """Build a private DT event without exposing planner-only metadata."""

        event = TwinEvent()
        event.stamp = self._stamp()
        event.event_type = event_type
        event.instrument_id = command.instrument_id
        event.instance_id = command.instrument_instance_id
        event.phase_id = ""
        event.location_id = location_id
        event.location_type = location_type
        event.owner = ""
        event.status = "completed"
        event.confidence = 1.0
        # The external controller selects the arm. The public Result does not
        # report one, so Taskplanner must not assert the legacy planner choice.
        event.arm = ""
        event.source_location_id = source_location_id
        event.source_location_type = source_location_type
        event.target_location_id = target_location_id
        event.target_location_type = target_location_type
        event.target_owner = ""
        event.cleaning_required = False
        event.mode = ""
        detail = {
            "command_id": command.command_id,
            "controller_final_state": final_state,
            "controller_reason_code": reason_code,
            "request_generation": int(command.request_generation),
            "voice_backed": bool(command.voice_backed),
        }
        if authoritative_controller_completion:
            # This is read-only provenance for the reducer.  It is set only
            # after the Action callback has correlated a current Goal and
            # validated SUCCEEDED + FINAL_COMPLETED + success=true.
            detail["authoritative_controller_completion"] = True
            semantic_leg = controller_semantic_leg or ("", "")
            detail["controller_source_location"] = str(semantic_leg[0])
            detail["controller_target_location"] = str(semantic_leg[1])
            detail["controller_projection_step"] = str(
                controller_projection_step
            )
            detail["controller_projection_index"] = int(
                controller_projection_index
            )
            detail["controller_projection_count"] = int(
                controller_projection_count
            )
        event.detail_json = json.dumps(
            detail,
            separators=(",", ":"),
            sort_keys=True,
        )
        return event

    def _on_skill(self, msg: SkillCommand) -> None:
        command = self._skill_from_msg(msg)
        direct_hand_error = self._direct_hand_run_guard(command)
        if direct_hand_error:
            self._publish_skill_status(
                command,
                state="rejected",
                success=False,
                reason_code=direct_hand_error,
            )
            return
        if not self._tool_handover_enabled:
            self._publish_skill_status(
                command,
                state="rejected",
                success=False,
                reason_code="tool_handover_disabled_for_procedure",
            )
            return
        if not self._runtime_is_accepting():
            self._publish_skill_status(
                command,
                state="cancelled",
                success=False,
                reason_code="runtime_not_accepting_commands",
            )
            return
        instrument_name, instrument_instance_id = self._public_instrument_identity(
            command
        )
        try:
            transfer_request = map_skill_to_tool_handover(
                command,
                instrument_name=instrument_name,
                instrument_instance_id=instrument_instance_id,
            )
        except MappingFailure as exc:
            self._publish_skill_status(
                command,
                state="rejected",
                success=False,
                reason_code=exc.code,
            )
            return
        if self._queue_voice_tool_transfer_preemption(command, transfer_request):
            return
        self._dispatch_tool_transfer(command, transfer_request)

    def _dispatch_tool_transfer(
        self,
        command: InternalSkillCommand,
        request: ToolHandoverRequest,
    ) -> None:
        if not self._tool_transfer_client.wait_for_server(
            timeout_sec=self._server_wait_timeout_sec
        ):
            self._publish_skill_status(
                command,
                state="offline",
                success=False,
                reason_code="server_unavailable",
            )
            return
        admission_error = self._dispatch_admission_guard("tool_transfer")
        if admission_error:
            self._publish_skill_status(
                command,
                state="rejected",
                success=False,
                reason_code=admission_error,
            )
            return
        dispatch_error = self._begin_action_dispatch(
            "tool_transfer",
            command,
            semantic_leg=self._tool_transfer_semantic_leg(request),
        )
        if dispatch_error:
            if dispatch_error == "duplicate_command":
                status_state = "duplicate_suppressed"
            elif dispatch_error == "tool_transfer_busy":
                status_state = "busy"
            else:
                status_state = "cancelled"
            self._publish_skill_status(
                command,
                state=status_state,
                success=False,
                reason_code=dispatch_error,
            )
            return
        tracked, _cancelled, _semantic_leg, expected_epoch = (
            self._tool_transfer_action_snapshot(command.command_id)
        )
        if not tracked:
            self._publish_skill_status(
                command,
                state="rejected",
                success=False,
                reason_code="reserved_dispatch_missing",
            )
            return
        self._dispatch_reserved_tool_transfer(
            command,
            request,
            expected_epoch=expected_epoch,
            server_ready=True,
        )

    def _dispatch_reserved_tool_transfer(
        self,
        command: InternalSkillCommand,
        request: ToolHandoverRequest,
        *,
        expected_epoch: int,
        server_ready: bool,
    ) -> None:
        """Submit one already-reserved Goal without holding the dispatch lock."""

        if self._finish_tool_transfer_before_send_if_cancelled(
            command,
            expected_epoch=expected_epoch,
        ):
            return
        if not server_ready and not self._tool_transfer_client.wait_for_server(
            timeout_sec=self._server_wait_timeout_sec
        ):
            if self._finish_tool_transfer_before_send_if_cancelled(
                command,
                expected_epoch=expected_epoch,
            ):
                return
            self._publish_skill_status(
                command,
                state="offline",
                success=False,
                reason_code="server_unavailable",
            )
            # No Goal was submitted, so the controller-confirmed source is
            # unchanged. Release through the normal terminal path so a voice
            # request queued during the wait is rebased instead of stranded.
            _tracked, _cancelled, semantic_leg, _active_epoch = (
                self._tool_transfer_action_snapshot(command.command_id)
            )
            self._finish_tool_transfer_action(
                command.command_id,
                safe_terminal=True,
                controller_confirmed_tool_location=(
                    semantic_leg[0] if semantic_leg is not None else ""
                ),
            )
            return
        self._publish_skill_status(
            command,
            state="dispatching",
            success=True,
            reason_code="dispatching",
        )
        if self._finish_tool_transfer_before_send_if_cancelled(
            command,
            expected_epoch=expected_epoch,
        ):
            return
        # A source lease may age out while waiting for the Action server or
        # while a lifecycle callback is interleaved. Recheck at the I/O edge.
        admission_error = self._dispatch_admission_guard("tool_transfer")
        if admission_error:
            if self._finish_tool_transfer_before_send_if_cancelled(
                command,
                expected_epoch=expected_epoch,
            ):
                return
            self._publish_skill_status(
                command,
                state="rejected",
                success=False,
                reason_code=admission_error,
            )
            # Admission rejected before Goal submission; source is unchanged.
            _tracked, _cancelled, semantic_leg, _active_epoch = (
                self._tool_transfer_action_snapshot(command.command_id)
            )
            self._finish_tool_transfer_action(
                command.command_id,
                safe_terminal=True,
                controller_confirmed_tool_location=(
                    semantic_leg[0] if semantic_leg is not None else ""
                ),
            )
            return
        if self._finish_tool_transfer_before_send_if_cancelled(
            command,
            expected_epoch=expected_epoch,
        ):
            return
        direct_hand_error = self._direct_hand_run_guard(command)
        if direct_hand_error:
            self._publish_skill_status(
                command,
                state="rejected",
                success=False,
                reason_code=direct_hand_error,
            )
            self._finish_tool_transfer_action(
                command.command_id,
                safe_terminal=True,
            )
            return
        if command.mode == "implicit_request":
            ledger = self._direct_hand_dispatch_ledger
            try:
                reservation = ledger.reserve(
                    procedure_run_id=command.procedure_run_id,
                    episode_generation=command.implicit_request_generation,
                    command_id=command.command_id,
                    action=command.action,
                    instrument_id=request.instrument_id,
                    instrument_instance_id=request.instrument_instance_id,
                    source_location=request.source_location,
                    target_location=request.target_location,
                )
            except Exception:
                self._publish_skill_status(
                    command,
                    state="rejected",
                    success=False,
                    reason_code="direct_hand_ledger_unavailable",
                )
                self._finish_tool_transfer_action(
                    command.command_id,
                    safe_terminal=True,
                )
                return
            if not reservation.accepted:
                self._publish_skill_status(
                    command,
                    state=(
                        "duplicate_suppressed"
                        if reservation.reason == "duplicate_command"
                        else "rejected"
                    ),
                    success=False,
                    reason_code=reservation.reason,
                )
                self._finish_tool_transfer_action(
                    command.command_id,
                    safe_terminal=True,
                )
                return
        try:
            future = self._tool_transfer_client.send_goal_async(
                self._tool_transfer_goal(request),
                feedback_callback=lambda feedback, command=command: (
                    self._on_tool_transfer_feedback(command, feedback)
                ),
            )
        except Exception:  # pragma: no cover - ROS transport failure
            self._publish_execution_trace(
                command_id=command.command_id,
                route="tool_transfer",
                transport="action",
                endpoint=self._execution_trace_endpoint("tool_transfer"),
                stage="failed",
                dispatch_submitted=False,
                terminal=True,
                evidence="not_dispatched",
                reason_code="dispatch_failed",
            )
            self._publish_skill_status(
                command,
                state="fault",
                success=False,
                reason_code="dispatch_failed",
            )
            self._finish_tool_transfer_action(
                command.command_id,
                safe_terminal=False,
            )
            return
        if command.mode == "implicit_request":
            try:
                self._direct_hand_dispatch_ledger.mark_stage(
                    command.command_id,
                    "submitted",
                )
            except Exception:  # pragma: no cover - reservation still fences replay
                self.get_logger().error(
                    "failed to update direct hand dispatch ledger submitted stage"
                )
        self._publish_execution_trace(
            command_id=command.command_id,
            route="tool_transfer",
            transport="action",
            endpoint=self._execution_trace_endpoint("tool_transfer"),
            stage="sent",
            dispatch_submitted=True,
            terminal=False,
            evidence="submission_only",
            reason_code="goal_send_submitted",
        )
        future.add_done_callback(
            lambda result, command=command: self._on_tool_transfer_goal_response(
                command, result
            )
        )

    def _on_tool_transfer_feedback(
        self, command: InternalSkillCommand, feedback_message: Any
    ) -> None:
        tracked, cancel_requested = self._tool_transfer_action_state(
            command.command_id
        )
        if not tracked:
            return
        feedback = feedback_message.feedback
        feedback_state = str(feedback.state).strip()
        if feedback_state not in _TOOL_TRANSFER_FEEDBACK_STATES:
            self._publish_skill_status(
                command,
                state="fault",
                success=False,
                reason_code="invalid_controller_feedback_state",
                progress=float(feedback.progress),
            )
            return
        self._publish_skill_status(
            command,
            state=feedback_state,
            success=not cancel_requested,
            reason_code=("cancel_recovery" if cancel_requested else "executing"),
            progress=float(feedback.progress),
        )

    def _on_tool_transfer_goal_response(
        self, command: InternalSkillCommand, future: Any
    ) -> None:
        try:
            goal_handle = future.result()
        except Exception:  # pragma: no cover - ROS transport failure
            tracked, cancel_requested = self._tool_transfer_action_state(
                command.command_id
            )
            if not tracked:
                return
            self._block_runtime_dispatch()
            self._publish_skill_status(
                command,
                state="unknown",
                success=False,
                reason_code=(
                    "cancel_result_unavailable"
                    if cancel_requested
                    else "goal_response_unavailable"
                ),
                progress=0.0,
            )
            self._publish_execution_trace(
                command_id=command.command_id,
                route="tool_transfer",
                transport="action",
                endpoint=self._execution_trace_endpoint("tool_transfer"),
                stage="unknown",
                dispatch_submitted=True,
                terminal=False,
                evidence="response_unavailable",
                reason_code=(
                    "cancel_result_unavailable"
                    if cancel_requested
                    else "goal_response_unavailable"
                ),
            )
            return
        if goal_handle is None or not goal_handle.accepted:
            tracked, cancel_requested, semantic_leg, _dispatch_epoch = (
                self._tool_transfer_action_snapshot(command.command_id)
            )
            if not tracked:
                return
            if cancel_requested:
                self._publish_skill_status(
                    command,
                    state=ExecuteToolHandover.Result.FINAL_CANCELED,
                    success=False,
                    reason_code=(
                        ExecuteToolHandover.Result.REASON_CANCELED_SOURCE_UNCHANGED
                    ),
                    progress=1.0,
                )
                self._publish_execution_trace(
                    command_id=command.command_id,
                    route="tool_transfer",
                    transport="action",
                    endpoint=self._execution_trace_endpoint("tool_transfer"),
                    stage="canceled",
                    dispatch_submitted=True,
                    terminal=True,
                    evidence="goal_response",
                    reason_code=(
                        ExecuteToolHandover.Result.REASON_CANCELED_SOURCE_UNCHANGED
                    ),
                )
            else:
                self._publish_skill_status(
                    command,
                    state="rejected",
                    success=False,
                    reason_code="goal_rejected",
                )
                self._publish_execution_trace(
                    command_id=command.command_id,
                    route="tool_transfer",
                    transport="action",
                    endpoint=self._execution_trace_endpoint("tool_transfer"),
                    stage="rejected",
                    dispatch_submitted=True,
                    terminal=True,
                    evidence="goal_response",
                    reason_code="goal_rejected",
                )
            self._finish_tool_transfer_action(
                command.command_id,
                safe_terminal=bool(cancel_requested),
                controller_confirmed_tool_location=(
                    semantic_leg[0]
                    if cancel_requested and semantic_leg is not None
                    else ""
                ),
            )
            return
        tracked, cancel_requested = self._set_tool_transfer_goal_handle(
            command.command_id, goal_handle
        )
        if not tracked:
            try:
                goal_handle.cancel_goal_async()
            except Exception:  # pragma: no cover - ROS transport failure
                pass
            return
        if cancel_requested:
            try:
                goal_handle.cancel_goal_async()
            except Exception:  # pragma: no cover - ROS transport failure
                self.get_logger().warning(
                    f"failed to cancel tool_transfer command {command.command_id}"
                )
        else:
            self._publish_skill_status(
                command,
                state="accepted",
                success=True,
                reason_code="accepted",
            )
            self._publish_execution_trace(
                command_id=command.command_id,
                route="tool_transfer",
                transport="action",
                endpoint=self._execution_trace_endpoint("tool_transfer"),
                stage="accepted",
                dispatch_submitted=True,
                terminal=False,
                evidence="goal_response",
                reason_code="goal_accepted",
            )
        goal_handle.get_result_async().add_done_callback(
            lambda result, command=command: self._on_tool_transfer_result(
                command, result
            )
        )

    def _on_tool_transfer_result(
        self, command: InternalSkillCommand, future: Any
    ) -> None:
        tracked, cancel_requested, semantic_leg, _dispatch_epoch = (
            self._tool_transfer_action_snapshot(command.command_id)
        )
        if not tracked:
            return
        try:
            wrapped_result = future.result()
        except Exception:  # pragma: no cover - ROS transport failure
            wrapped_result = None
        if wrapped_result is None:
            self._block_runtime_dispatch()
            self._publish_skill_status(
                command,
                state="unknown",
                success=False,
                reason_code=(
                    "cancel_result_unavailable"
                    if cancel_requested
                    else "result_failed"
                ),
                progress=0.0,
            )
            self._publish_execution_trace(
                command_id=command.command_id,
                route="tool_transfer",
                transport="action",
                endpoint=self._execution_trace_endpoint("tool_transfer"),
                stage="unknown",
                dispatch_submitted=True,
                terminal=False,
                evidence="response_unavailable",
                reason_code=(
                    "cancel_result_unavailable"
                    if cancel_requested
                    else "result_failed"
                ),
            )
            return
        result = getattr(wrapped_result, "result", None)
        ros_status = int(getattr(wrapped_result, "status", GoalStatus.STATUS_UNKNOWN))
        if result is None:
            self._block_runtime_dispatch()
            self._publish_skill_status(
                command,
                state=ExecuteToolHandover.Result.FINAL_FAILED,
                success=False,
                reason_code="invalid_controller_result",
                progress=1.0,
            )
            self._publish_execution_trace(
                command_id=command.command_id,
                route="tool_transfer",
                transport="action",
                endpoint=self._execution_trace_endpoint("tool_transfer"),
                stage="unknown",
                dispatch_submitted=True,
                terminal=False,
                evidence="response_invalid",
                reason_code="invalid_controller_result",
            )
            self._finish_tool_transfer_action(
                command.command_id,
                safe_terminal=False,
            )
            return
        success = bool(result.success)
        final_state = str(result.final_state).strip()
        reason_code = str(result.reason_code).strip()
        expected_ros_status = {
            ExecuteToolHandover.Result.FINAL_COMPLETED: GoalStatus.STATUS_SUCCEEDED,
            ExecuteToolHandover.Result.FINAL_CANCELED: GoalStatus.STATUS_CANCELED,
            ExecuteToolHandover.Result.FINAL_FAILED: GoalStatus.STATUS_ABORTED,
        }.get(final_state)
        result_is_consistent = final_state in _TOOL_TRANSFER_FINAL_STATES and (
            (success and final_state == ExecuteToolHandover.Result.FINAL_COMPLETED)
            or (
                not success
                and final_state
                in {
                    ExecuteToolHandover.Result.FINAL_CANCELED,
                    ExecuteToolHandover.Result.FINAL_FAILED,
                }
            )
        ) and ros_status == expected_ros_status
        if (
            result_is_consistent
            and final_state == ExecuteToolHandover.Result.FINAL_CANCELED
            and reason_code not in _TOOL_TRANSFER_CANCEL_REASONS
        ):
            result_is_consistent = False
        if (
            result_is_consistent
            and final_state == ExecuteToolHandover.Result.FINAL_COMPLETED
            and reason_code
            not in {"", ExecuteToolHandover.Result.REASON_COMPLETED}
        ):
            result_is_consistent = False
        if not result_is_consistent:
            self._block_runtime_dispatch()
            success = False
            final_state = ExecuteToolHandover.Result.FINAL_FAILED
            reason_code = "invalid_controller_result"
        else:
            reason_code = reason_code or (
                "completed" if success else "execution_failed"
            )
        if (
            cancel_requested
            and final_state == ExecuteToolHandover.Result.FINAL_FAILED
        ):
            self._block_runtime_dispatch()
        if success:
            self._publish_tool_transfer_completed_events(
                command,
                final_state=final_state,
                reason_code=reason_code,
                semantic_leg=semantic_leg,
            )
        elif (
            result_is_consistent
            and final_state == ExecuteToolHandover.Result.FINAL_CANCELED
        ):
            # This inventory correction must be emitted before the active lane
            # is released below.  Otherwise a queued voice replacement could
            # dispatch while the DT still believes the predecessor is held.
            self._publish_tool_transfer_cancel_reconciliation(
                command,
                final_state=final_state,
                reason_code=reason_code,
            )
        self._publish_skill_status(
            command,
            state=final_state,
            success=success,
            reason_code=reason_code,
            progress=1.0,
        )
        self._publish_execution_trace(
            command_id=command.command_id,
            route="tool_transfer",
            transport="action",
            endpoint=self._execution_trace_endpoint("tool_transfer"),
            stage=final_state,
            dispatch_submitted=True,
            terminal=True,
            evidence=("controller_result" if result_is_consistent else "response_invalid"),
            reason_code=reason_code,
        )
        self._finish_tool_transfer_action(
            command.command_id,
            safe_terminal=(
                result_is_consistent
                and final_state
                in {
                    ExecuteToolHandover.Result.FINAL_COMPLETED,
                    ExecuteToolHandover.Result.FINAL_CANCELED,
                }
            ),
            controller_confirmed_tool_location=(
                self._controller_confirmed_tool_location(
                    semantic_leg,
                    success=success,
                    final_state=final_state,
                    reason_code=reason_code,
                )
                if result_is_consistent
                else ""
            ),
        )

    def _on_group(self, msg: BedRobotArmGroupCommand) -> None:
        command = self._group_from_msg(msg)
        try:
            request = map_group_command(
                command,
                max_retraction_distance_mm=self._max_retraction_distance_mm,
            )
        except MappingFailure as exc:
            self._publish_group_status(
                command,
                state="fault",
                outcome="rejected",
                terminal=True,
                success=False,
                reason_code=exc.code,
            )
            return
        is_stop_request = request.command == RETRACTION_COMMAND_STOP_RETRACTION
        if not self._runtime_is_accepting() and not is_stop_request:
            self._publish_group_status(
                command,
                state="standby",
                outcome="cancelled_by_runtime_control",
                terminal=True,
                success=False,
                reason_code="runtime_not_accepting_commands",
            )
            return
        guard_error = self._bed_robot_dispatch_guard(request)
        if guard_error:
            self._publish_group_status(
                command,
                state="fault",
                outcome="rejected",
                terminal=True,
                success=False,
                reason_code=guard_error,
            )
            return
        self._dispatch_retraction_service(command, request)

    def _dispatch_retraction_service(
        self,
        command: InternalGroupCommand,
        request: RetractionCommandRequest,
    ) -> None:
        is_stop_request = request.command == RETRACTION_COMMAND_STOP_RETRACTION
        if not self._retraction_service_client.wait_for_service(
            timeout_sec=self._server_wait_timeout_sec
        ):
            self._publish_group_status(
                command,
                state="offline",
                outcome="server_unavailable",
                terminal=True,
                success=False,
                reason_code="service_unavailable",
            )
            return
        admission_error = self._dispatch_admission_guard(
            "retraction", allow_stop=is_stop_request
        )
        if admission_error:
            self._publish_group_status(
                command,
                state="fault",
                outcome="rejected",
                terminal=True,
                success=False,
                reason_code=admission_error,
            )
            return
        dispatch_error = self._begin_service_dispatch(
            "retraction",
            command,
            allow_when_runtime_not_accepting=is_stop_request,
        )
        if dispatch_error:
            self._publish_group_status(
                command,
                state=(
                    "standby"
                    if dispatch_error == "runtime_not_accepting_commands"
                    else "fault"
                ),
                outcome=(
                    "cancelled_by_runtime_control"
                    if dispatch_error == "runtime_not_accepting_commands"
                    else "duplicate_suppressed"
                ),
                terminal=True,
                success=False,
                reason_code=dispatch_error,
            )
            return
        self._publish_group_status(
            command,
            state="dispatching",
            outcome="dispatching",
            terminal=False,
            success=False,
            reason_code="dispatching",
        )
        canceled_before_dispatch = False
        admission_error = self._dispatch_admission_guard(
            "retraction", allow_stop=is_stop_request
        )
        if admission_error:
            self._clear_service("retraction", command.command_id)
            self._publish_group_status(
                command,
                state="fault",
                outcome="rejected",
                terminal=True,
                success=False,
                reason_code=admission_error,
            )
            return
        try:
            with self._dispatch_lock:
                active = self._active_services.get(
                    self._action_key("retraction", command.command_id)
                )
                if active is None or active.cancelled:
                    canceled_before_dispatch = True
                    future = None
                else:
                    active.dispatched = True
                    future = self._retraction_service_client.call_async(
                        self._retraction_service_request(request)
                    )
                    active.future = future
        except Exception:  # pragma: no cover - ROS transport failure
            self._clear_service("retraction", command.command_id)
            self._publish_execution_trace(
                command_id=command.command_id,
                route="retraction",
                transport="service",
                endpoint=self._execution_trace_endpoint("retraction"),
                stage="failed",
                dispatch_submitted=False,
                terminal=True,
                evidence="not_dispatched",
                reason_code="dispatch_failed",
                retraction_request=request,
            )
            self._publish_group_status(
                command,
                state="fault",
                outcome="dispatch_failed",
                terminal=True,
                success=False,
                reason_code="dispatch_failed",
            )
            return
        if canceled_before_dispatch or future is None:
            self._clear_service("retraction", command.command_id)
            self._publish_group_status(
                command,
                state="canceled",
                outcome="canceled_before_dispatch",
                terminal=True,
                success=False,
                reason_code="canceled_before_service_dispatch",
            )
            return
        self._publish_execution_trace(
            command_id=command.command_id,
            route="retraction",
            transport="service",
            endpoint=self._execution_trace_endpoint("retraction"),
            stage="sent",
            dispatch_submitted=True,
            terminal=False,
            evidence="submission_only",
            reason_code="service_call_submitted",
            retraction_request=request,
        )
        future.add_done_callback(
            lambda result, command=command, request=request: self._on_retraction_service_result(
                command, result, request=request
            )
        )

    def _on_retraction_service_result(
        self,
        command: InternalGroupCommand,
        future: Any,
        *,
        request: RetractionCommandRequest | None = None,
    ) -> None:
        """Record Service admission without inventing physical completion.

        The controller's response is only a receipt for the request.  A
        successful receipt ends the *transport* transaction, not retraction,
        direct-teach, or tool-change execution.
        """

        with self._dispatch_lock:
            active = self._active_services.get(
                self._action_key("retraction", command.command_id)
            )
            cancel_requested = bool(active.cancelled) if active is not None else False
        if active is None:
            return
        try:
            result = future.result()
        except Exception:  # pragma: no cover - ROS transport failure
            result = None
        if result is None:
            self._block_runtime_dispatch()
            response_reason = (
                "service_response_unavailable_after_stop"
                if cancel_requested
                else "service_response_unavailable"
            )
            self._publish_group_status(
                command,
                state="unknown",
                outcome="remote_state_unknown",
                terminal=False,
                success=False,
                reason_code=response_reason,
            )
            self._publish_execution_trace(
                command_id=command.command_id,
                route="retraction",
                transport="service",
                endpoint=self._execution_trace_endpoint("retraction"),
                stage="unknown",
                dispatch_submitted=True,
                terminal=False,
                evidence="response_unavailable",
                reason_code=response_reason,
                retraction_request=request,
            )
            return

        accepted = bool(getattr(result, "request_accepted", False))
        response_command_id = str(getattr(result, "command_id", "")).strip()
        try:
            result_code = int(getattr(result, "result_code"))
        except (TypeError, ValueError):
            result_code = -1
        valid_rejection_codes = {
            ExecuteRetractionCommand.Response.RESULT_INVALID_COMMAND,
            ExecuteRetractionCommand.Response.RESULT_INVALID_PARAMETER,
            ExecuteRetractionCommand.Response.RESULT_REJECTED,
            ExecuteRetractionCommand.Response.RESULT_ERROR,
        }
        valid = response_command_id == command.command_id and (
            (
                accepted
                and result_code == ExecuteRetractionCommand.Response.RESULT_ACCEPTED
            )
            or (not accepted and result_code in valid_rejection_codes)
        )
        if not valid:
            self._block_runtime_dispatch()
            self._publish_group_status(
                command,
                state="unknown",
                outcome="remote_state_unknown",
                terminal=False,
                success=False,
                reason_code="invalid_service_response",
            )
            self._publish_execution_trace(
                command_id=command.command_id,
                route="retraction",
                transport="service",
                endpoint=self._execution_trace_endpoint("retraction"),
                stage="unknown",
                dispatch_submitted=True,
                terminal=False,
                evidence="response_invalid",
                reason_code="invalid_service_response",
                retraction_request=request,
            )
            return
        message = str(getattr(result, "message", "")).strip()
        self._clear_service("retraction", command.command_id)
        if accepted and cancel_requested:
            # The controller may already execute a request admitted after the
            # local runtime stopped.  The Service has no cancellation or
            # physical-state response, so fail closed and preserve uncertainty.
            self._block_runtime_dispatch()
            self._publish_group_status(
                command,
                state="unknown",
                outcome="accepted_after_stop",
                terminal=False,
                success=False,
                reason_code=message or "service_request_accepted_after_stop",
            )
            self._publish_execution_trace(
                command_id=command.command_id,
                route="retraction",
                transport="service",
                endpoint=self._execution_trace_endpoint("retraction"),
                stage="unknown",
                dispatch_submitted=True,
                terminal=False,
                evidence="service_admission_only",
                reason_code="service_request_accepted_after_stop",
                retraction_request=request,
            )
            return
        if accepted:
            self._publish_group_status(
                command,
                state="accepted",
                outcome="accepted",
                # ``terminal`` is intentionally the Service-call lifecycle,
                # not a claim that the robot completed physical work.
                terminal=True,
                success=True,
                reason_code=message or "request_accepted",
            )
            self._publish_execution_trace(
                command_id=command.command_id,
                route="retraction",
                transport="service",
                endpoint=self._execution_trace_endpoint("retraction"),
                stage="accepted",
                dispatch_submitted=True,
                terminal=True,
                evidence="service_admission_only",
                reason_code="request_accepted",
                retraction_request=request,
            )
            return
        self._publish_group_status(
            command,
            state="rejected",
            outcome="rejected_after_stop" if cancel_requested else "rejected",
            terminal=True,
            success=False,
            reason_code=message or f"service_rejected_{result_code}",
        )
        self._publish_execution_trace(
            command_id=command.command_id,
            route="retraction",
            transport="service",
            endpoint=self._execution_trace_endpoint("retraction"),
            stage="rejected",
            dispatch_submitted=True,
            terminal=True,
            evidence="service_admission_only",
            reason_code=f"service_rejected_{result_code}",
            retraction_request=request,
        )


def main() -> None:
    rclpy.init()
    node = SurgicalInteropExecutionBridge()
    # The stopped-only coordinator blocks briefly for manager/preflight
    # responses.  A second executor worker keeps its re-entrant simulation
    # state subscription live during that bounded wait; this does not create
    # another command path or dispatch any robot request.
    executor = MultiThreadedExecutor(num_threads=2)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        executor.shutdown()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
