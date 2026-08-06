"""Bridge internal Taskplanner commands onto focused public robot endpoints."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import json
import threading
import uuid

import rclpy
from procedure_spec import load_bundle
from rclpy.action import ActionClient
from rclpy.node import Node
from surgical_interop_msgs.action import (
    ExecuteRetraction,
    ExecuteToolHandover,
)
from surgical_interop_msgs.srv import SetSuction
from surgical_msgs.msg import BedRobotArmGroupCommand, BedRobotArmGroupStatus
from surgical_msgs.msg import SkillCommand, SkillStatus, TwinEvent
from std_msgs.msg import String

from .mappings import (
    DispatchLedger,
    InternalGroupCommand,
    InternalSkillCommand,
    MappingFailure,
    PREPARE_ALIASES,
    RETRIEVE_ALIASES,
    RETURN_TO_TRAY_ALIASES,
    RetractionRequest,
    SuctionRequest,
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


@dataclass(slots=True)
class ActiveAction:
    route: str
    command: InternalSkillCommand | InternalGroupCommand
    goal_handle: Any | None = None
    cancelled: bool = False


@dataclass(slots=True)
class ActiveService:
    route: str
    command: InternalGroupCommand
    cancelled: bool = False


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
        self._retraction_endpoint = str(
            self.declare_parameter("retraction_endpoint", "/surgery/retraction").value
        )
        self._suction_endpoint = str(
            self.declare_parameter("suction_service", "/surgery/suction/set").value
        )
        self._dispatch_lock = threading.RLock()
        self._runtime_accepting_commands = False
        self._dispatch_ledger = DispatchLedger(
            int(self.declare_parameter("dedupe_max_entries", 512).value)
        )
        self._active_actions: dict[tuple[str, str], ActiveAction] = {}
        self._active_services: dict[tuple[str, str], ActiveService] = {}
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
        self._retraction_client = ActionClient(
            self, ExecuteRetraction, self._retraction_endpoint
        )
        self._suction_client = self.create_client(SetSuction, self._suction_endpoint)
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

    def _set_action_goal_handle(self, route: str, command_id: str, goal_handle: Any) -> bool:
        with self._dispatch_lock:
            active = self._active_actions.get(self._action_key(route, command_id))
            if active is None or active.cancelled:
                return False
            active.goal_handle = goal_handle
            return True

    def _clear_action(self, route: str, command_id: str) -> None:
        with self._dispatch_lock:
            self._active_actions.pop(self._action_key(route, command_id), None)

    def _service_is_cancelled(self, route: str, command_id: str) -> bool:
        with self._dispatch_lock:
            active = self._active_services.get(self._action_key(route, command_id))
            return active is None or active.cancelled

    def _clear_service(self, route: str, command_id: str) -> None:
        with self._dispatch_lock:
            self._active_services.pop(self._action_key(route, command_id), None)

    def _on_control(self, msg: String) -> None:
        control = msg.data.partition(":")[0].strip().lower()
        if control in {"start", "start_actors"}:
            with self._dispatch_lock:
                self._runtime_accepting_commands = True
            return
        if control == "start_runtime":
            with self._dispatch_lock:
                self._runtime_accepting_commands = False
            return
        if control not in {"stop", "reset"}:
            return

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
                    state="standby",
                    outcome="cancelled_by_runtime_control",
                    terminal=True,
                    success=False,
                    reason_code="cancelled_by_runtime_control",
                )
            if active.goal_handle is not None:
                try:
                    active.goal_handle.cancel_goal_async()
                except Exception:  # pragma: no cover - ROS transport failure
                    self.get_logger().warning(
                        f"failed to cancel {active.route} command {active.command.command_id}"
                    )

        # ROS services are not cancellable after dispatch.  Mark their future
        # result as ignored and publish a terminal compatibility status now.
        for active in services:
            self._publish_group_status(
                active.command,
                state="standby",
                outcome="cancelled_by_runtime_control",
                terminal=True,
                success=False,
                reason_code="cancelled_by_runtime_control",
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
            direction=msg.direction.strip(),
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
        status.state = state
        status.outcome = outcome
        status.terminal = bool(terminal)
        status.success = bool(success)
        status.message = reason_code
        status.direction = command.direction
        status.distance_mm = float(command.distance_mm)
        status.distance_origin = command.distance_origin
        status.raw_distance_text = command.raw_distance_text
        status.end_effector_profile = command.end_effector_profile
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

    @staticmethod
    def _retraction_goal(request: RetractionRequest) -> ExecuteRetraction.Goal:
        goal = ExecuteRetraction.Goal()
        goal.command_id = request.command_id
        goal.operation = request.operation
        goal.direction = request.direction
        goal.distance_mm = float(request.distance_mm)
        goal.end_effector_profile = request.end_effector_profile
        return goal

    @staticmethod
    def _suction_request(request: SuctionRequest) -> SetSuction.Request:
        service_request = SetSuction.Request()
        service_request.command_id = request.command_id
        service_request.enabled = bool(request.enabled)
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
            if cancel_requested:
                self._block_runtime_dispatch()
            self._clear_action("tool_transfer", command.command_id)
            self._publish_skill_status(
                command,
                state=ExecuteToolHandover.Result.FINAL_FAILED,
                success=False,
                reason_code=(
                    "cancel_result_unavailable"
                    if cancel_requested
                    else "dispatch_failed"
                ),
                progress=1.0,
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
            result = wrapped_result.result if wrapped_result is not None else None
        except Exception:  # pragma: no cover - ROS transport failure
            result = None
        if result is None:
            if cancel_requested:
                self._block_runtime_dispatch()
            self._clear_action("tool_transfer", command.command_id)
            self._publish_skill_status(
                command,
                state=ExecuteToolHandover.Result.FINAL_FAILED,
                success=False,
                reason_code=(
                    "cancel_result_unavailable"
                    if cancel_requested
                    else "result_failed"
                ),
                progress=1.0,
            )
            return
        success = bool(result.success)
        final_state = str(result.final_state).strip()
        reason_code = str(result.reason_code).strip()
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
        )
        if (
            result_is_consistent
            and final_state == ExecuteToolHandover.Result.FINAL_CANCELED
            and reason_code not in _TOOL_TRANSFER_CANCEL_REASONS
        ):
            result_is_consistent = False
        if not result_is_consistent:
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
            request = map_group_command(command)
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
        if isinstance(request, SuctionRequest):
            self._dispatch_suction(command, request)
            return
        self._dispatch_retraction(command, request)

    def _dispatch_suction(self, command: InternalGroupCommand, request: SuctionRequest) -> None:
        if not self._suction_client.wait_for_service(
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
        dispatch_error = self._begin_service_dispatch("suction", command)
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
            success=True,
            reason_code="dispatching",
        )
        if self._service_is_cancelled("suction", command.command_id):
            self._clear_service("suction", command.command_id)
            return
        try:
            future = self._suction_client.call_async(self._suction_request(request))
        except Exception:  # pragma: no cover - ROS transport failure
            self._clear_service("suction", command.command_id)
            self._publish_group_status(
                command,
                state="fault",
                outcome="dispatch_failed",
                terminal=True,
                success=False,
                reason_code="dispatch_failed",
            )
            return
        future.add_done_callback(
            lambda result, command=command: self._on_suction_result(command, result)
        )

    def _on_suction_result(self, command: InternalGroupCommand, future: Any) -> None:
        if self._service_is_cancelled("suction", command.command_id):
            self._clear_service("suction", command.command_id)
            return
        try:
            result = future.result()
        except Exception:  # pragma: no cover - ROS transport failure
            result = None
        if result is None:
            self._clear_service("suction", command.command_id)
            self._publish_group_status(
                command,
                state="fault",
                outcome="result_failed",
                terminal=True,
                success=False,
                reason_code="result_failed",
            )
            return
        success = bool(result.success)
        self._clear_service("suction", command.command_id)
        self._publish_group_status(
            command,
            state=result.state or ("completed" if success else "fault"),
            outcome="completed" if success else "failed",
            terminal=True,
            success=success,
            reason_code=result.reason_code or ("completed" if success else "execution_failed"),
            progress=1.0 if success else 0.0,
        )

    def _dispatch_retraction(
        self, command: InternalGroupCommand, request: RetractionRequest
    ) -> None:
        if not self._retraction_client.wait_for_server(
            timeout_sec=self._server_wait_timeout_sec
        ):
            self._publish_group_status(
                command,
                state="offline",
                outcome="server_unavailable",
                terminal=True,
                success=False,
                reason_code="server_unavailable",
            )
            return
        dispatch_error = self._begin_action_dispatch("retraction", command)
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
            success=True,
            reason_code="dispatching",
        )
        if self._action_is_cancelled("retraction", command.command_id):
            self._clear_action("retraction", command.command_id)
            return
        try:
            future = self._retraction_client.send_goal_async(
                self._retraction_goal(request),
                feedback_callback=lambda feedback, command=command: self._on_retraction_feedback(
                    command, feedback
                ),
            )
        except Exception:  # pragma: no cover - ROS transport failure
            self._clear_action("retraction", command.command_id)
            self._publish_group_status(
                command,
                state="fault",
                outcome="dispatch_failed",
                terminal=True,
                success=False,
                reason_code="dispatch_failed",
            )
            return
        future.add_done_callback(
            lambda result, command=command: self._on_retraction_goal_response(command, result)
        )

    def _on_retraction_feedback(self, command: InternalGroupCommand, feedback_message: Any) -> None:
        if self._action_is_cancelled("retraction", command.command_id):
            return
        feedback = feedback_message.feedback
        self._publish_group_status(
            command,
            state=feedback.state or "executing",
            outcome="executing",
            terminal=False,
            success=True,
            reason_code="executing",
            progress=float(feedback.progress),
        )

    def _on_retraction_goal_response(self, command: InternalGroupCommand, future: Any) -> None:
        try:
            goal_handle = future.result()
        except Exception:  # pragma: no cover - ROS transport failure
            if self._action_is_cancelled("retraction", command.command_id):
                self._clear_action("retraction", command.command_id)
                return
            self._clear_action("retraction", command.command_id)
            self._publish_group_status(
                command,
                state="fault",
                outcome="dispatch_failed",
                terminal=True,
                success=False,
                reason_code="dispatch_failed",
            )
            return
        if goal_handle is None or not goal_handle.accepted:
            if self._action_is_cancelled("retraction", command.command_id):
                self._clear_action("retraction", command.command_id)
                return
            self._clear_action("retraction", command.command_id)
            self._publish_group_status(
                command,
                state="fault",
                outcome="rejected",
                terminal=True,
                success=False,
                reason_code="goal_rejected",
            )
            return
        if not self._set_action_goal_handle("retraction", command.command_id, goal_handle):
            try:
                goal_handle.cancel_goal_async()
            except Exception:  # pragma: no cover - ROS transport failure
                pass
            self._clear_action("retraction", command.command_id)
            return
        if self._action_is_cancelled("retraction", command.command_id):
            try:
                goal_handle.cancel_goal_async()
            except Exception:  # pragma: no cover - ROS transport failure
                pass
            self._clear_action("retraction", command.command_id)
            return
        self._publish_group_status(
            command,
            state="accepted",
            outcome="accepted",
            terminal=False,
            success=True,
            reason_code="accepted",
        )
        goal_handle.get_result_async().add_done_callback(
            lambda result, command=command: self._on_retraction_result(command, result)
        )

    def _on_retraction_result(self, command: InternalGroupCommand, future: Any) -> None:
        if self._action_is_cancelled("retraction", command.command_id):
            self._clear_action("retraction", command.command_id)
            return
        try:
            wrapped_result = future.result()
            result = wrapped_result.result if wrapped_result is not None else None
        except Exception:  # pragma: no cover - ROS transport failure
            result = None
        if result is None:
            self._clear_action("retraction", command.command_id)
            self._publish_group_status(
                command,
                state="fault",
                outcome="result_failed",
                terminal=True,
                success=False,
                reason_code="result_failed",
            )
            return
        success = bool(result.success)
        self._clear_action("retraction", command.command_id)
        self._publish_group_status(
            command,
            state=result.final_state or ("completed" if success else "fault"),
            outcome="completed" if success else "failed",
            terminal=True,
            success=success,
            reason_code=result.reason_code or ("completed" if success else "execution_failed"),
            progress=1.0 if success else 0.0,
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
