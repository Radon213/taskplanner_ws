"""Bridge group-level bed robot-arm commands onto independent ROS actions."""

from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Any
import uuid

import rclpy
from rclpy.action import ActionClient
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from std_msgs.msg import String
from surgical_msgs.action import ExecuteBedRobotArmGroup
from surgical_msgs.msg import BedRobotArmGroupCommand, BedRobotArmGroupStatus

from .group_contract import (
    GROUP_RETRACTION,
    GROUP_STATES,
    GROUP_SUCTION,
    OPERATION_CHANGE_END_EFFECTOR,
    OPERATION_RELEASE_RETRACTION,
    OPERATION_RETRACTION,
    OPERATION_SUCTION_START,
    OPERATION_SUCTION_STOP,
    validate_command_values,
)


@dataclass(frozen=True, slots=True)
class GroupCommandEnvelope:
    request_id: str
    command_id: str
    group_id: str
    operation: str
    direction: str
    distance_mm: float
    distance_origin: str
    raw_distance_text: str
    end_effector_profile: str
    rationale: str
    confidence: float


@dataclass(slots=True)
class ActiveGroupGoal:
    command: GroupCommandEnvelope
    signature: tuple[Any, ...]
    goal_handle: Any | None = None
    last_activity_ns: int = 0


class BedRobotArmGroupActionBridge(Node):
    """Dispatch at most one in-flight goal per logical group.

    Suction and retraction have independent action clients and active-goal
    slots, so either group can run while the other is still executing.
    """

    def __init__(self) -> None:
        super().__init__("bed_robot_arm_group_action_bridge")
        self._command_topic = str(
            self.declare_parameter(
                "command_topic", "/bt/bed_robot_arm_group_command"
            ).value
        )
        self._status_topic = str(
            self.declare_parameter(
                "status_topic", "/bed_robot_arm_group/status"
            ).value
        )
        suction_action_name = str(
            self.declare_parameter(
                "suction_action_name", "/bed_robot_arm_group/suction/execute"
            ).value
        )
        retraction_action_name = str(
            self.declare_parameter(
                "retraction_action_name", "/bed_robot_arm_group/retraction/execute"
            ).value
        )
        self._action_names = {
            GROUP_SUCTION: suction_action_name,
            GROUP_RETRACTION: retraction_action_name,
        }
        self._min_repeat_interval_sec = float(
            self.declare_parameter("min_repeat_interval_sec", 2.0).value
        )
        self._server_wait_timeout_sec = float(
            self.declare_parameter("server_wait_timeout_sec", 3.0).value
        )
        self._feedback_stale_timeout_sec = float(
            self.declare_parameter("feedback_stale_timeout_sec", 15.0).value
        )
        self._goal_timeout_sec = {
            OPERATION_SUCTION_START: float(
                self.declare_parameter("suction_goal_timeout_sec", 10.0).value
            ),
            OPERATION_SUCTION_STOP: float(
                self.get_parameter("suction_goal_timeout_sec").value
            ),
            OPERATION_RETRACTION: float(
                self.declare_parameter("retraction_goal_timeout_sec", 20.0).value
            ),
            OPERATION_RELEASE_RETRACTION: float(
                self.declare_parameter("release_goal_timeout_sec", 15.0).value
            ),
            OPERATION_CHANGE_END_EFFECTOR: float(
                self.declare_parameter(
                    "change_end_effector_goal_timeout_sec", 60.0
                ).value
            ),
        }
        self._callback_group = ReentrantCallbackGroup()
        self._lock = threading.RLock()
        self._active: dict[str, ActiveGroupGoal] = {}
        self._group_states = {
            GROUP_SUCTION: "standby",
            GROUP_RETRACTION: "standby",
        }
        self._last_signatures: dict[str, tuple[tuple[Any, ...], int]] = {}
        self._cancelled_command_ids: set[str] = set()
        self._timed_out_command_ids: set[str] = set()
        self._command_started_ns: dict[str, int] = {}
        self._server_ready: dict[str, bool | None] = {
            GROUP_SUCTION: None,
            GROUP_RETRACTION: None,
        }
        self._runtime_accepting_commands = False
        self._status_pub = self.create_publisher(
            BedRobotArmGroupStatus, self._status_topic, 20
        )
        self._action_clients = {
            group_id: ActionClient(
                self,
                ExecuteBedRobotArmGroup,
                action_name,
                callback_group=self._callback_group,
            )
            for group_id, action_name in self._action_names.items()
        }
        self.create_subscription(
            BedRobotArmGroupCommand,
            self._command_topic,
            self._on_command,
            20,
            callback_group=self._callback_group,
        )
        self.create_timer(
            0.5,
            self._watch_action_health,
            callback_group=self._callback_group,
        )
        self.create_subscription(
            String,
            "/simulation/control_state",
            self._on_control,
            20,
            callback_group=self._callback_group,
        )

    def _stamp(self):
        return self.get_clock().now().to_msg()

    @staticmethod
    def _coerce_command(msg: BedRobotArmGroupCommand) -> GroupCommandEnvelope:
        command_id = msg.command_id.strip() or uuid.uuid4().hex
        request_id = msg.request_id.strip() or f"request-{command_id}"
        return GroupCommandEnvelope(
            request_id=request_id,
            command_id=command_id,
            group_id=msg.group_id.strip(),
            operation=msg.operation.strip(),
            direction=msg.direction.strip().upper(),
            distance_mm=float(msg.distance_mm),
            distance_origin=msg.distance_origin.strip(),
            raw_distance_text=msg.raw_distance_text,
            end_effector_profile=msg.end_effector_profile.strip(),
            rationale=msg.rationale,
            confidence=float(msg.confidence),
        )

    @staticmethod
    def _signature(command: GroupCommandEnvelope) -> tuple[Any, ...]:
        return (
            command.request_id,
            command.operation,
            command.direction,
            command.distance_mm,
            command.distance_origin,
            command.raw_distance_text,
            command.end_effector_profile,
        )

    def _is_duplicate_locked(
        self, command: GroupCommandEnvelope, signature: tuple[Any, ...]
    ) -> bool:
        previous = self._last_signatures.get(command.group_id)
        now_ns = self.get_clock().now().nanoseconds
        if previous is not None:
            previous_signature, previous_ns = previous
            elapsed = (now_ns - previous_ns) / 1_000_000_000.0
            if (
                signature == previous_signature
                and elapsed < self._min_repeat_interval_sec
            ):
                return True
        self._last_signatures[command.group_id] = (signature, now_ns)
        return False

    @staticmethod
    def _expected_duration_sec(command: GroupCommandEnvelope) -> float:
        return {
            OPERATION_SUCTION_START: 0.4,
            OPERATION_SUCTION_STOP: 0.4,
            OPERATION_RETRACTION: 1.2,
            OPERATION_RELEASE_RETRACTION: 0.6,
            OPERATION_CHANGE_END_EFFECTOR: 1.8,
        }.get(command.operation, 1.0)

    def _publish_status(
        self,
        command: GroupCommandEnvelope,
        *,
        state: str,
        outcome: str,
        terminal: bool,
        success: bool,
        message: str,
        progress: float | None = None,
        error_code: str = "",
        rejection_reason: str = "",
        end_effector_profile: str | None = None,
        update_group_state: bool = True,
    ) -> None:
        status = BedRobotArmGroupStatus()
        status.stamp = self._stamp()
        status.request_id = command.request_id
        status.command_id = command.command_id
        status.group_id = command.group_id
        status.operation = command.operation
        with self._lock:
            if update_group_state and state in GROUP_STATES:
                self._group_states[command.group_id] = state
            elif state not in GROUP_STATES:
                state = self._group_states.get(command.group_id, "standby")
        status.state = state
        status.outcome = outcome
        status.terminal = bool(terminal)
        status.success = bool(success)
        status.message = message
        status.direction = command.direction
        status.distance_mm = float(command.distance_mm)
        status.distance_origin = command.distance_origin
        status.raw_distance_text = command.raw_distance_text
        status.end_effector_profile = (
            command.end_effector_profile
            if end_effector_profile is None
            else end_effector_profile
        )
        status.confidence = float(command.confidence)
        with self._lock:
            started_ns = self._command_started_ns.get(command.command_id, 0)
        now_ns = self.get_clock().now().nanoseconds
        elapsed_sec = (
            max((now_ns - started_ns) / 1_000_000_000.0, 0.0)
            if started_ns
            else 0.0
        )
        expected_sec = self._expected_duration_sec(command)
        normalized_progress = float(progress if progress is not None else 0.0)
        normalized_progress = max(0.0, min(1.0, normalized_progress))
        if terminal and success:
            normalized_progress = 1.0
        status.progress = normalized_progress
        status.elapsed_sec = float(elapsed_sec)
        status.remaining_sec = float(
            0.0 if terminal else max(expected_sec - elapsed_sec, 0.0)
        )
        status.error_code = error_code
        status.rejection_reason = rejection_reason
        self._status_pub.publish(status)

    def _current_state(self, group_id: str) -> str:
        with self._lock:
            return self._group_states.get(group_id, "standby")

    def _to_action_goal(
        self, command: GroupCommandEnvelope
    ) -> ExecuteBedRobotArmGroup.Goal:
        wire_command = BedRobotArmGroupCommand()
        wire_command.stamp = self._stamp()
        wire_command.request_id = command.request_id
        wire_command.command_id = command.command_id
        wire_command.group_id = command.group_id
        wire_command.operation = command.operation
        wire_command.direction = command.direction
        wire_command.distance_mm = float(command.distance_mm)
        wire_command.distance_origin = command.distance_origin
        wire_command.raw_distance_text = command.raw_distance_text
        wire_command.end_effector_profile = command.end_effector_profile
        wire_command.rationale = command.rationale
        wire_command.confidence = float(command.confidence)
        goal = ExecuteBedRobotArmGroup.Goal()
        goal.command = wire_command
        return goal

    def _clear_active(self, command: GroupCommandEnvelope) -> None:
        with self._lock:
            active = self._active.get(command.group_id)
            if active is not None and active.command.command_id == command.command_id:
                self._active.pop(command.group_id, None)
            self._command_started_ns.pop(command.command_id, None)

    def _on_feedback(self, command: GroupCommandEnvelope, feedback_message) -> None:
        feedback = feedback_message.feedback
        with self._lock:
            if (
                command.command_id in self._cancelled_command_ids
                or command.command_id in self._timed_out_command_ids
            ):
                return
            active = self._active.get(command.group_id)
            if active is None or active.command.command_id != command.command_id:
                return
            active.last_activity_ns = self.get_clock().now().nanoseconds
            # Publish while holding the re-entrant state lock so reset cannot
            # emit a terminal cancellation and then be followed by stale
            # feedback from this command.
            self._publish_status(
                command,
                state=feedback.state or "executing",
                outcome="executing",
                terminal=False,
                success=True,
                message=feedback.message,
                progress=float(feedback.progress),
            )

    def _on_goal_response(self, command: GroupCommandEnvelope, future) -> None:
        try:
            goal_handle = future.result()
        except Exception as exc:  # pragma: no cover - ROS transport failure
            with self._lock:
                was_cancelled = command.command_id in self._cancelled_command_ids
                was_timed_out = command.command_id in self._timed_out_command_ids
                active = self._active.get(command.group_id)
                still_active = (
                    active is not None
                    and active.command.command_id == command.command_id
                )
                if was_cancelled or was_timed_out:
                    self._cancelled_command_ids.discard(command.command_id)
                    self._timed_out_command_ids.discard(command.command_id)
                    return
                if not still_active:
                    return
                self._publish_status(
                    command,
                    state="fault",
                    outcome="dispatch_failed",
                    terminal=True,
                    success=False,
                    message=f"action dispatch failed: {exc}",
                    error_code="action_dispatch_failed",
                )
                self._clear_active(command)
            return

        if goal_handle is None or not goal_handle.accepted:
            with self._lock:
                was_cancelled = command.command_id in self._cancelled_command_ids
                was_timed_out = command.command_id in self._timed_out_command_ids
                active = self._active.get(command.group_id)
                still_active = (
                    active is not None
                    and active.command.command_id == command.command_id
                )
                if was_cancelled or was_timed_out:
                    self._cancelled_command_ids.discard(command.command_id)
                    self._timed_out_command_ids.discard(command.command_id)
                    return
                if not still_active:
                    return
                self._publish_status(
                    command,
                    state=self._current_state(command.group_id),
                    outcome="rejected",
                    terminal=True,
                    success=False,
                    message="group action server rejected goal",
                    error_code="goal_rejected",
                    rejection_reason="group action server rejected the command envelope",
                )
                self._clear_active(command)
            return

        with self._lock:
            was_cancelled = command.command_id in self._cancelled_command_ids
            was_timed_out = command.command_id in self._timed_out_command_ids
            active = self._active.get(command.group_id)
            still_active = (
                active is not None
                and active.command.command_id == command.command_id
            )
            if still_active:
                active.goal_handle = goal_handle
                active.last_activity_ns = self.get_clock().now().nanoseconds
            if not was_cancelled and not was_timed_out and still_active:
                self._publish_status(
                    command,
                    state=self._current_state(command.group_id),
                    outcome="accepted",
                    terminal=False,
                    success=True,
                    message=f"goal accepted by {self._action_names[command.group_id]}",
                )
                result_future = goal_handle.get_result_async()
                result_future.add_done_callback(
                    lambda result, command=command: self._on_result(command, result)
                )
                return

        try:
            goal_handle.cancel_goal_async()
        except Exception as exc:  # pragma: no cover - ROS transport failure
            self.get_logger().warning(
                f"failed to cancel stale {command.group_id} goal: {exc}"
            )
        if not (was_cancelled or was_timed_out):
            return
        try:
            result_future = goal_handle.get_result_async()
            result_future.add_done_callback(
                lambda result, command=command: self._on_result(command, result)
            )
        except Exception:  # pragma: no cover - destroyed action transport
            with self._lock:
                self._cancelled_command_ids.discard(command.command_id)
                self._timed_out_command_ids.discard(command.command_id)

    def _on_result(self, command: GroupCommandEnvelope, future) -> None:
        try:
            wrapped_result = future.result()
            result = wrapped_result.result if wrapped_result is not None else None
        except Exception as exc:  # pragma: no cover - ROS transport failure
            result = None
            failure_message = f"action result failed: {exc}"
        else:
            failure_message = "group action result payload was empty"

        with self._lock:
            if command.command_id in self._cancelled_command_ids:
                self._cancelled_command_ids.discard(command.command_id)
                self._clear_active(command)
                return
            if command.command_id in self._timed_out_command_ids:
                self._timed_out_command_ids.discard(command.command_id)
                self._clear_active(command)
                return
            active = self._active.get(command.group_id)
            if active is None or active.command.command_id != command.command_id:
                return
            if result is None:
                self._publish_status(
                    command,
                    state="fault",
                    outcome="result_failed",
                    terminal=True,
                    success=False,
                    message=failure_message,
                    error_code="action_result_failed",
                )
                self._clear_active(command)
                return
            self._publish_status(
                command,
                state=result.final_state or ("standby" if result.success else "fault"),
                outcome=result.outcome or ("completed" if result.success else "failed"),
                terminal=True,
                success=bool(result.success),
                message=result.message,
                progress=1.0 if result.success else None,
                error_code=result.error_code,
                rejection_reason=result.rejection_reason,
                end_effector_profile=result.end_effector_profile,
            )
            self._clear_active(command)

    def _dispatch(self, command: GroupCommandEnvelope) -> None:
        client = self._action_clients[command.group_id]
        action_name = self._action_names[command.group_id]
        if not client.wait_for_server(timeout_sec=self._server_wait_timeout_sec):
            with self._lock:
                self._server_ready[command.group_id] = False
                active = self._active.get(command.group_id)
                if active is None or active.command.command_id != command.command_id:
                    self._cancelled_command_ids.discard(command.command_id)
                    self._timed_out_command_ids.discard(command.command_id)
                    return
                if (
                    command.command_id in self._cancelled_command_ids
                    or command.command_id in self._timed_out_command_ids
                ):
                    return
                self._publish_status(
                    command,
                    state="offline",
                    outcome="server_unavailable",
                    terminal=True,
                    success=False,
                    message=f"group action server {action_name} not available",
                    error_code="server_unavailable",
                )
                self._clear_active(command)
            return
        with self._lock:
            self._server_ready[command.group_id] = True
            active = self._active.get(command.group_id)
            if active is None or active.command.command_id != command.command_id:
                self._cancelled_command_ids.discard(command.command_id)
                self._timed_out_command_ids.discard(command.command_id)
                return
            if (
                command.command_id in self._cancelled_command_ids
                or command.command_id in self._timed_out_command_ids
            ):
                return
            self._publish_status(
                command,
                state=self._current_state(command.group_id),
                outcome="dispatching",
                terminal=False,
                success=True,
                message=f"dispatching group goal to {action_name}",
            )
            try:
                send_future = client.send_goal_async(
                    self._to_action_goal(command),
                    feedback_callback=lambda feedback, command=command: self._on_feedback(
                        command, feedback
                    ),
                )
            except Exception as exc:  # pragma: no cover - ROS transport failure
                self._publish_status(
                    command,
                    state="fault",
                    outcome="dispatch_failed",
                    terminal=True,
                    success=False,
                    message=f"action dispatch failed: {exc}",
                    error_code="action_dispatch_failed",
                )
                self._clear_active(command)
                return
            send_future.add_done_callback(
                lambda result, command=command: self._on_goal_response(command, result)
            )

    def _on_command(self, msg: BedRobotArmGroupCommand) -> None:
        command = self._coerce_command(msg)
        with self._lock:
            if not self._runtime_accepting_commands:
                self._publish_status(
                    command,
                    state="standby",
                    outcome="cancelled_by_runtime_control",
                    terminal=True,
                    success=False,
                    message="simulation runtime is not accepting group commands",
                    progress=1.0,
                    end_effector_profile="",
                )
                return
        validation_error = validate_command_values(
            group_id=command.group_id,
            operation=command.operation,
            direction=command.direction,
            distance_mm=command.distance_mm,
            distance_origin=command.distance_origin,
            confidence=command.confidence,
        )
        if validation_error:
            self._publish_status(
                command,
                state=self._current_state(command.group_id),
                outcome="rejected",
                terminal=True,
                success=False,
                message=validation_error,
                error_code="invalid_group_command",
                rejection_reason=validation_error,
            )
            return

        signature = self._signature(command)
        with self._lock:
            # Re-check under the same lock that creates the active slot.  A
            # reset racing validation must win before any post-reset goal can
            # appear.
            if not self._runtime_accepting_commands:
                self._publish_status(
                    command,
                    state="standby",
                    outcome="cancelled_by_runtime_control",
                    terminal=True,
                    success=False,
                    message="simulation runtime stopped while command was being validated",
                    progress=1.0,
                    end_effector_profile="",
                )
                return
            active = self._active.get(command.group_id)
            if active is not None:
                if signature == active.signature:
                    self.get_logger().debug(
                        f"suppressing in-flight duplicate {command.group_id} goal"
                    )
                    return
                busy_message = (
                    f"{command.group_id} group is busy with command "
                    f"{active.command.command_id}"
                )
            else:
                busy_message = ""

            if not busy_message and self._is_duplicate_locked(command, signature):
                self.get_logger().debug(
                    f"suppressing repeated {command.group_id} group goal"
                )
                return

            if not busy_message:
                self._active[command.group_id] = ActiveGroupGoal(
                    command=command,
                    signature=signature,
                    last_activity_ns=self.get_clock().now().nanoseconds,
                )
                self._command_started_ns[
                    command.command_id
                ] = self.get_clock().now().nanoseconds

        if busy_message:
            self._publish_status(
                command,
                state=self._current_state(command.group_id),
                outcome="skipped_while_busy",
                terminal=True,
                success=False,
                message=busy_message,
                error_code="group_busy",
                rejection_reason=busy_message,
            )
            return
        self._dispatch(command)

    def _cancel_active_goals(self, reason: str) -> None:
        handles: list[tuple[GroupCommandEnvelope, Any]] = []
        with self._lock:
            for active in list(self._active.values()):
                command = active.command
                current = self._active.get(command.group_id)
                if current is None or current.command.command_id != command.command_id:
                    continue
                goal_handle = current.goal_handle
                self._cancelled_command_ids.add(command.command_id)
                self._active.pop(command.group_id, None)
                self._command_started_ns.pop(command.command_id, None)
                self._publish_status(
                    command,
                    state="standby",
                    outcome="cancelled_by_runtime_control",
                    terminal=True,
                    success=False,
                    message=f"group command cancelled by simulation {reason}",
                    progress=1.0,
                    end_effector_profile="",
                )
                if goal_handle is not None:
                    handles.append((command, goal_handle))
        for command, goal_handle in handles:
            if goal_handle is not None:
                try:
                    goal_handle.cancel_goal_async()
                except Exception as exc:  # pragma: no cover - ROS transport failure
                    self.get_logger().warning(
                        f"failed to cancel {command.group_id} goal: {exc}"
                    )

    def _availability_envelope(self, group_id: str) -> GroupCommandEnvelope:
        return GroupCommandEnvelope(
            request_id=f"health-{group_id}",
            command_id="",
            group_id=group_id,
            operation="",
            direction="",
            distance_mm=0.0,
            distance_origin="",
            raw_distance_text="",
            end_effector_profile="",
            rationale="action server readiness probe",
            confidence=1.0,
        )

    def _watch_action_health(self) -> None:
        now_ns = self.get_clock().now().nanoseconds
        for group_id, client in self._action_clients.items():
            ready = bool(client.server_is_ready())
            with self._lock:
                active = self._active.get(group_id)
                if active is None:
                    previous = self._server_ready[group_id]
                    self._server_ready[group_id] = ready
                    if (
                        ready
                        and previous is False
                        and self._group_states.get(group_id) == "offline"
                    ):
                        # A controller that disappeared while dispatching has
                        # no trustworthy held pose after reconnection.
                        self._group_states[group_id] = "standby"
                    command = self._availability_envelope(group_id)
                    health_state = (
                        self._group_states.get(group_id, "standby")
                        if ready
                        else "offline"
                    )
                    self._publish_status(
                        command,
                        state=health_state,
                        outcome="available" if ready else "server_unavailable",
                        terminal=True,
                        success=ready,
                        message=(
                            f"group action server {self._action_names[group_id]} is available"
                            if ready
                            else f"group action server {self._action_names[group_id]} is unavailable"
                        ),
                        progress=1.0,
                        error_code="" if ready else "server_unavailable",
                        update_group_state=False,
                    )

        with self._lock:
            active_goals = list(self._active.values())
        for active in active_goals:
            command = active.command
            with self._lock:
                started_ns = self._command_started_ns.get(command.command_id, now_ns)
                current = self._active.get(command.group_id)
                if current is None or current.command.command_id != command.command_id:
                    continue
                last_activity_ns = current.last_activity_ns or started_ns
            total_elapsed = max((now_ns - started_ns) / 1_000_000_000.0, 0.0)
            stale_elapsed = max((now_ns - last_activity_ns) / 1_000_000_000.0, 0.0)
            deadline = self._goal_timeout_sec.get(command.operation, 30.0)
            timed_out = total_elapsed > deadline
            feedback_stale = (
                current.goal_handle is not None
                and stale_elapsed > self._feedback_stale_timeout_sec
            )
            if not timed_out and not feedback_stale:
                continue
            reason = (
                f"group goal exceeded {deadline:g} second operation deadline"
                if timed_out
                else (
                    "group controller produced no feedback/result for "
                    f"{self._feedback_stale_timeout_sec:g} seconds"
                )
            )
            with self._lock:
                latest = self._active.get(command.group_id)
                if latest is None or latest.command.command_id != command.command_id:
                    continue
                goal_handle = latest.goal_handle
                self._timed_out_command_ids.add(command.command_id)
                self._active.pop(command.group_id, None)
                self._command_started_ns.pop(command.command_id, None)
                self._publish_status(
                    command,
                    state="fault",
                    outcome="controller_timeout",
                    terminal=True,
                    success=False,
                    message=reason,
                    error_code="controller_timeout",
                    rejection_reason=reason,
                )
            if goal_handle is not None:
                try:
                    goal_handle.cancel_goal_async()
                except Exception as exc:  # pragma: no cover - ROS transport failure
                    self.get_logger().warning(
                        f"failed to cancel timed-out {command.group_id} goal: {exc}"
                    )

    def _on_control(self, msg: String) -> None:
        command = msg.data.partition(":")[0].strip().lower()
        if command in {"start", "start_actors"}:
            with self._lock:
                self._runtime_accepting_commands = True
            self._watch_action_health()
            return
        if command == "start_runtime":
            # Runtime state may be reset to optimistic defaults by the twin,
            # but group dispatch remains gated until start_actors. Re-announce
            # actual controller readiness immediately and via the heartbeat.
            with self._lock:
                self._runtime_accepting_commands = False
            self._watch_action_health()
            return
        if command in {"stop", "reset"}:
            with self._lock:
                self._runtime_accepting_commands = False
            self._cancel_active_goals(command)
            with self._lock:
                self._group_states[GROUP_SUCTION] = "standby"
                self._group_states[GROUP_RETRACTION] = "standby"
                self._last_signatures.clear()
            self._watch_action_health()


def main() -> None:
    rclpy.init()
    node = BedRobotArmGroupActionBridge()
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            executor.remove_node(node)
            node.destroy_node()
        finally:
            if rclpy.ok():
                rclpy.shutdown()
