"""Bridge internal Taskplanner commands onto focused public robot endpoints."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import json
import threading
import time
import uuid

import rclpy
from action_msgs.msg import GoalStatus
from procedure_spec import load_bundle
from rclpy.action import ActionClient
from rclpy.node import Node
from surgical_interop_msgs.action import (
    ExecuteToolHandover,
)
from surgical_interop_msgs.msg import BedRobotArmStateArray
from surgical_interop_msgs.srv import ExecuteRetractionCommand
from surgical_msgs.msg import BedRobotArmGroupCommand, BedRobotArmGroupStatus
from surgical_msgs.msg import SkillCommand, SkillStatus, TwinEvent
from std_msgs.msg import String

from .mappings import (
    DispatchLedger,
    InternalGroupCommand,
    InternalSkillCommand,
    MappingFailure,
    PREPARE_ALIASES,
    RETRACTION_COMMAND_ADJUST_RETRACTION,
    RETRACTION_COMMAND_STOP_RETRACTION,
    RETRACTION_TARGET_LEFT,
    RETRACTION_TARGET_RIGHT,
    RETRIEVE_ALIASES,
    RETURN_TO_TRAY_ALIASES,
    RetractionCommandRequest,
    ToolHandoverRequest,
    map_group_command,
    map_skill_to_tool_handover,
    public_instrument_instance_id,
)


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
    {"left_malleable", "right_malleable", "army_navy"}
)
_BED_ROBOT_PROCEDURE_LAYOUTS = {
    "thyroidectomy": frozenset({"army_navy"}),
    "nephrectomy": frozenset({"left_malleable", "right_malleable"}),
}


@dataclass(slots=True)
class ActiveAction:
    route: str
    command: InternalSkillCommand | InternalGroupCommand
    goal_handle: Any | None = None
    cancelled: bool = False
    dispatched: bool = False


@dataclass(slots=True)
class ActiveService:
    route: str
    command: InternalGroupCommand
    # ROS services cannot be canceled after dispatch.  This flag means runtime
    # stop/reset was requested while the blocking controller call is in flight.
    cancelled: bool = False
    dispatched: bool = False
    future: Any | None = None


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
        spec_dir = str(self.declare_parameter("spec_dir", "").value).strip()
        self._procedure_spec = load_bundle(spec_dir or None)
        self._instrument_names = {
            instrument.id: instrument.display_name.strip()
            for instrument in self._procedure_spec.bundle.instruments
        }
        self._retraction_service_name = str(
            self.declare_parameter(
                "retraction_service_name", "/surgery/retraction/command"
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
        self._max_retraction_distance_mm = float(
            self.declare_parameter("max_retraction_distance_mm", 50.0).value
        )
        self._require_bed_robot_status = bool(
            self.declare_parameter("require_bed_robot_status", True).value
        )
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
        self._runtime_accepting_commands = False
        self._last_lifecycle_control_signature: tuple[str, str] | None = None
        self._dispatch_ledger = DispatchLedger(
            int(self.declare_parameter("dedupe_max_entries", 512).value)
        )
        self._active_actions: dict[tuple[str, str], ActiveAction] = {}
        self._active_services: dict[tuple[str, str], ActiveService] = {}
        self._bed_robot_revision: int | None = None
        self._bed_robot_source_stamp_ns: int | None = None
        self._bed_robot_epoch = 0
        self._bed_robot_signature: tuple[Any, ...] | None = None
        self._bed_robot_procedure_type = ""
        self._bed_robot_received_monotonic = 0.0
        self._bed_robot_states: dict[str, Any] = {}
        self._skill_status_pub = self.create_publisher(SkillStatus, "/skill/status", 20)
        self._skill_event_pub = self.create_publisher(TwinEvent, "/skill/events", 20)
        self._group_status_pub = self.create_publisher(
            BedRobotArmGroupStatus, "/bed_robot_arm_group/status", 20
        )
        self._tool_transfer_client = ActionClient(
            self,
            ExecuteToolHandover,
            self._tool_transfer_endpoint,
        )
        self._retraction_service_client = self.create_client(
            ExecuteRetractionCommand, self._retraction_service_name
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
        )
        self.create_subscription(
            BedRobotArmStateArray,
            self._bed_robot_status_endpoint,
            self._on_bed_robot_status,
            20,
        )

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

    def _runtime_is_accepting(self) -> bool:
        with self._dispatch_lock:
            return self._runtime_accepting_commands

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
        if procedure_type != "nephrectomy":
            return "unsupported_procedure_operation"
        role_by_side = {
            RETRACTION_TARGET_LEFT: "left_malleable",
            RETRACTION_TARGET_RIGHT: "right_malleable",
        }
        target_role = role_by_side.get(request.target_side)
        if not target_role:
            return "invalid_target_side"
        targets = [
            arm
            for arm in states.values()
            if arm.role_instance_id == target_role
        ]
        ready_states = {"standby", "retracting"}

        if not targets or any(target is None for target in targets):
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
        self, route: str, command: InternalSkillCommand | InternalGroupCommand
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
            ):
                return "duplicate_command"
            self._active_actions[self._action_key(route, command.command_id)] = ActiveAction(
                route=route,
                command=command,
            )
        return ""

    def _begin_service_dispatch(self, route: str, command: InternalGroupCommand) -> str:
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
        if control in {"start", "start_actors"}:
            with self._dispatch_lock:
                self._runtime_accepting_commands = True
            return
        if control == "resume":
            with self._dispatch_lock:
                self._runtime_accepting_commands = True
            return
        if control == "start_runtime":
            with self._dispatch_lock:
                self._runtime_accepting_commands = False
            return
        if control not in {"pause", "stop", "reset"}:
            return

        if control == "reset":
            self._last_lifecycle_control_signature = None

        with self._dispatch_lock:
            self._runtime_accepting_commands = False
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
            if control == "reset":
                self._dispatch_ledger.clear()
                self._bed_robot_revision = None
                self._bed_robot_source_stamp_ns = None
                self._bed_robot_epoch = 0
                self._bed_robot_signature = None
                self._bed_robot_procedure_type = ""
                self._bed_robot_received_monotonic = 0.0
                self._bed_robot_states = {}

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
            rationale=msg.rationale,
            target_owner=msg.target_owner,
            cleaning_required=bool(msg.cleaning_required),
            mode=msg.mode,
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
        self, command: InternalSkillCommand, *, final_state: str, reason_code: str
    ) -> None:
        """Reconcile only controller-confirmed transfer outcomes into the DT."""

        if command.action in PREPARE_ALIASES:
            self._publish_tool_prepared_event(
                command,
                final_state=final_state,
                reason_code=reason_code,
            )
            return

        if command.action in RETURN_TO_TRAY_ALIASES:
            target_hint = " ".join(
                (command.target_location_type, command.target_location_id)
            ).casefold()
            target_location_id = (
                command.target_location_id
                if "tray" in target_hint or "rack" in target_hint
                else "tray"
            )
            target_location_type = (
                command.target_location_type
                if "tray" in target_hint or "rack" in target_hint
                else "tray"
            )
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
            )
            self._skill_event_pub.publish(returned)
            return

        if command.action in RETRIEVE_ALIASES:
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
            )
            self._skill_event_pub.publish(returned)
            return

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
        )
        self._skill_event_pub.publish(event)

    def _publish_tool_prepared_event(
        self, command: InternalSkillCommand, *, final_state: str, reason_code: str
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
        event.detail_json = json.dumps(
            {
                "command_id": command.command_id,
                "controller_final_state": final_state,
                "controller_reason_code": reason_code,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        return event

    def _on_skill(self, msg: SkillCommand) -> None:
        command = self._skill_from_msg(msg)
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
        dispatch_error = self._begin_action_dispatch("tool_transfer", command)
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
        self._publish_skill_status(
            command,
            state="dispatching",
            success=True,
            reason_code="dispatching",
        )
        if self._action_is_cancelled("tool_transfer", command.command_id):
            self._clear_action("tool_transfer", command.command_id)
            self._publish_skill_status(
                command,
                state=ExecuteToolHandover.Result.FINAL_CANCELED,
                success=False,
                reason_code=(
                    ExecuteToolHandover.Result.REASON_CANCELED_SOURCE_UNCHANGED
                ),
                progress=1.0,
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
            self._clear_action("tool_transfer", command.command_id)
            self._publish_skill_status(
                command,
                state="fault",
                success=False,
                reason_code="dispatch_failed",
            )
            return
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
            return
        if goal_handle is None or not goal_handle.accepted:
            tracked, cancel_requested = self._tool_transfer_action_state(
                command.command_id
            )
            if not tracked:
                return
            self._clear_action("tool_transfer", command.command_id)
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
            else:
                self._publish_skill_status(
                    command,
                    state="rejected",
                    success=False,
                    reason_code="goal_rejected",
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
        goal_handle.get_result_async().add_done_callback(
            lambda result, command=command: self._on_tool_transfer_result(
                command, result
            )
        )

    def _on_tool_transfer_result(
        self, command: InternalSkillCommand, future: Any
    ) -> None:
        tracked, cancel_requested = self._tool_transfer_action_state(
            command.command_id
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
            return
        result = getattr(wrapped_result, "result", None)
        ros_status = int(getattr(wrapped_result, "status", GoalStatus.STATUS_UNKNOWN))
        if result is None:
            self._block_runtime_dispatch()
            self._clear_action("tool_transfer", command.command_id)
            self._publish_skill_status(
                command,
                state=ExecuteToolHandover.Result.FINAL_FAILED,
                success=False,
                reason_code="invalid_controller_result",
                progress=1.0,
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
            )
        self._clear_action("tool_transfer", command.command_id)
        self._publish_skill_status(
            command,
            state=final_state,
            success=success,
            reason_code=reason_code,
            progress=1.0,
        )

    def _on_group(self, msg: BedRobotArmGroupCommand) -> None:
        command = self._group_from_msg(msg)
        if not self._runtime_is_accepting():
            self._publish_group_status(
                command,
                state="standby",
                outcome="cancelled_by_runtime_control",
                terminal=True,
                success=False,
                reason_code="runtime_not_accepting_commands",
            )
            return
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
        dispatch_error = self._begin_service_dispatch("retraction", command)
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
        future.add_done_callback(
            lambda result, command=command: self._on_retraction_service_result(
                command, result
            )
        )

    def _on_retraction_service_result(
        self, command: InternalGroupCommand, future: Any
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
            self._publish_group_status(
                command,
                state="unknown",
                outcome="remote_state_unknown",
                terminal=False,
                success=False,
                reason_code=(
                    "service_response_unavailable_after_stop"
                    if cancel_requested
                    else "service_response_unavailable"
                ),
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
            return
        self._publish_group_status(
            command,
            state="rejected",
            outcome="rejected_after_stop" if cancel_requested else "rejected",
            terminal=True,
            success=False,
            reason_code=message or f"service_rejected_{result_code}",
        )


def main() -> None:
    rclpy.init()
    node = SurgicalInteropExecutionBridge()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
