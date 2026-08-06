"""Bridge BT skill commands onto a ROS 2 action interface."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Tuple
import uuid

import rclpy
from rclpy.action import ActionClient
from rclpy.node import Node
from std_msgs.msg import String
from surgical_msgs.action import ExecuteSkill
from surgical_msgs.msg import SkillCommand, SkillStatus


ALLOWED_ACTIONS = {
    "direct_handover",
    "pick_up_and_handover",
    "pick_up_from_mayo_and_handover",
    "put_down_and_handover",
    "retrieve_from_hand",
    "retrieve_from_mayo",
    "predict_tool",
    "tool_handover",
    "tool_retrieve",
    "tool_predict",
    "predicted_tool_handover",
    "replace_and_handover",
    "return_unused_preposition",
}


@dataclass(slots=True)
class CommandEnvelope:
    command_id: str
    action: str
    instrument_id: str
    instrument_instance_id: str
    request_generation: int
    target_location_id: str
    target_location_type: str
    rationale: str
    arm: str
    source_location_id: str
    source_location_type: str
    target_owner: str
    cleaning_required: bool
    mode: str


class RequestDispatchLedger:
    """Bounded at-most-once ledger for explicit surgeon request generations."""

    def __init__(self, max_entries: int = 512) -> None:
        self._max_entries = max(1, int(max_entries))
        self._seen: set[int] = set()
        self._order: deque[int] = deque()

    def consume(self, request_generation: int) -> bool:
        generation = int(request_generation)
        if generation <= 0:
            return True
        if generation in self._seen:
            return False
        self._seen.add(generation)
        self._order.append(generation)
        while len(self._order) > self._max_entries:
            self._seen.discard(self._order.popleft())
        return True

    def clear(self) -> None:
        self._seen.clear()
        self._order.clear()


class SkillActionBridge(Node):
    def __init__(self) -> None:
        super().__init__("skill_action_bridge")
        self._action_name = str(self.declare_parameter("action_name", "/skill/execute").value)
        self._min_repeat_interval_sec = float(
            self.declare_parameter("min_repeat_interval_sec", 2.0).value
        )
        self._server_wait_timeout_sec = float(
            self.declare_parameter("server_wait_timeout_sec", 3.0).value
        )
        self._last_signature: (
            Tuple[str, str, str, int, str, str, str, str, str, str, str, bool] | None
        ) = None
        self._last_signature_time = self.get_clock().now()
        self._active_signature: (
            Tuple[str, str, str, int, str, str, str, str, str, str, str, bool] | None
        ) = None
        self._request_dispatch_ledger = RequestDispatchLedger()
        self._active_command_id = ""
        self._active_command: CommandEnvelope | None = None
        self._active_goal_handle = None
        self._cancelled_command_ids: set[str] = set()
        self._command_started_ns: dict[str, int] = {}
        self._status_pub = self.create_publisher(SkillStatus, "/skill/status", 20)
        self._action_client = ActionClient(self, ExecuteSkill, self._action_name)
        self.create_subscription(SkillCommand, "/bt/skill_command", self._on_command, 20)
        self.create_subscription(String, "/simulation/control_state", self._on_control, 20)

    def _stamp(self):
        return self.get_clock().now().to_msg()

    def _signature(
        self, command: CommandEnvelope
    ) -> Tuple[str, str, str, int, str, str, str, str, str, str, str, bool]:
        return (
            command.action,
            command.instrument_id,
            command.instrument_instance_id,
            int(command.request_generation),
            command.target_location_id,
            command.target_location_type,
            command.arm,
            command.mode,
            command.source_location_id,
            command.source_location_type,
            command.target_owner,
            bool(command.cleaning_required),
        )

    def _is_duplicate(self, command: CommandEnvelope) -> bool:
        if (
            command.mode == "explicit_request"
            and not self._request_dispatch_ledger.consume(command.request_generation)
        ):
            return True
        signature = self._signature(command)
        now = self.get_clock().now()
        elapsed = (now - self._last_signature_time).nanoseconds / 1_000_000_000.0
        if signature == self._last_signature and elapsed < self._min_repeat_interval_sec:
            return True
        self._last_signature = signature
        self._last_signature_time = now
        return False

    def _coerce_command(self, msg: SkillCommand) -> CommandEnvelope:
        return CommandEnvelope(
            command_id=msg.command_id or uuid.uuid4().hex,
            action=msg.action,
            instrument_id=msg.instrument_id,
            instrument_instance_id=msg.instrument_instance_id,
            request_generation=int(msg.request_generation),
            target_location_id=msg.target_location_id,
            target_location_type=msg.target_location_type,
            rationale=msg.rationale,
            arm=msg.arm,
            source_location_id=msg.source_location_id,
            source_location_type=msg.source_location_type,
            target_owner=msg.target_owner,
            cleaning_required=bool(msg.cleaning_required),
            mode=msg.mode,
        )

    def _publish_status(
        self, command: CommandEnvelope, state: str, success: bool, message: str, progress: float | None = None
    ) -> None:
        status = SkillStatus()
        status.stamp = self._stamp()
        status.command_id = command.command_id
        status.action = command.action
        status.instrument_id = command.instrument_id
        status.state = state
        status.success = success
        status.message = message
        status.arm = command.arm
        status.source_location_id = command.source_location_id
        status.source_location_type = command.source_location_type
        status.target_location_id = command.target_location_id
        status.target_location_type = command.target_location_type
        status.target_owner = command.target_owner
        status.cleaning_required = bool(command.cleaning_required)
        status.mode = command.mode
        started_ns = self._command_started_ns.get(command.command_id, 0)
        now_ns = self.get_clock().now().nanoseconds
        elapsed_sec = max((now_ns - started_ns) / 1_000_000_000.0, 0.0) if started_ns else 0.0
        expected_sec = self._expected_duration_sec(command)
        normalized_progress = float(progress if progress is not None else (1.0 if state == "completed" else 0.0))
        normalized_progress = max(0.0, min(1.0, normalized_progress))
        remaining_sec = max(expected_sec - elapsed_sec, 0.0) if expected_sec > 0.0 else 0.0
        if state in {"completed", "result_failed", "dispatch_failed", "server_unavailable", "rejected"}:
            normalized_progress = 1.0 if success else normalized_progress
            remaining_sec = 0.0
        status.progress = normalized_progress
        status.elapsed_sec = float(elapsed_sec)
        status.remaining_sec = float(remaining_sec)
        self._status_pub.publish(status)

    def _expected_duration_sec(self, command: CommandEnvelope) -> float:
        action = {
            "predict_tool": "tool_predict",
            "pick_up_and_handover": "tool_handover",
            "pick_up_from_mayo_and_handover": "mayo_handover",
            "direct_handover": "predicted_tool_handover",
            "put_down_and_handover": "replace_and_handover",
            "retrieve_from_mayo": "tool_retrieve",
            "retrieve_from_hand": "tool_retrieve",
            "return_unused_preposition": "return_unused_preposition",
        }.get(command.action, command.action)
        if action == "tool_predict":
            return 3.2
        if action == "tool_handover":
            return 3.2
        if action == "mayo_handover":
            return 4.2
        if action == "predicted_tool_handover":
            return 1.0
        if action == "replace_and_handover":
            return 4.2
        if action == "tool_retrieve":
            return 6.8
        if action == "return_unused_preposition":
            return 1.0
        return 1.0

    def _on_feedback(
        self, command: CommandEnvelope, feedback_message: ExecuteSkill.Impl.FeedbackMessage
    ) -> None:
        feedback = feedback_message.feedback
        self._publish_status(
            command,
            feedback.state or "executing",
            True,
            feedback.message,
            progress=float(feedback.progress),
        )

    def _on_goal_response(self, command: CommandEnvelope, future) -> None:
        try:
            goal_handle = future.result()
        except Exception as exc:  # pragma: no cover - transport failures are runtime-only
            self._active_signature = None
            self._active_command_id = ""
            self._active_command = None
            self._active_goal_handle = None
            self._command_started_ns.pop(command.command_id, None)
            self._publish_status(command, "dispatch_failed", False, f"action dispatch failed: {exc}")
            return

        if goal_handle is None or not goal_handle.accepted:
            self._active_signature = None
            self._active_command_id = ""
            self._active_command = None
            self._active_goal_handle = None
            self._command_started_ns.pop(command.command_id, None)
            self._publish_status(command, "rejected", False, "action server rejected goal")
            return

        if command.command_id in self._cancelled_command_ids:
            self._cancelled_command_ids.discard(command.command_id)
            try:
                goal_handle.cancel_goal_async()
            except Exception as exc:  # pragma: no cover - transport failures are runtime-only
                self.get_logger().warn(f"failed to cancel late-accepted skill goal: {exc}")
            self._active_signature = None
            self._active_command_id = ""
            self._active_command = None
            self._active_goal_handle = None
            self._command_started_ns.pop(command.command_id, None)
            self._publish_status(command, "cancel_requested", False, "goal accepted after runtime stop/reset")
            return

        self._active_goal_handle = goal_handle
        self._publish_status(command, "accepted", True, f"goal accepted by {self._action_name}")
        result_future = goal_handle.get_result_async()
        result_future.add_done_callback(lambda result, command=command: self._on_result(command, result))

    def _on_result(self, command: CommandEnvelope, future) -> None:
        try:
            wrapped_result = future.result()
        except Exception as exc:  # pragma: no cover - transport failures are runtime-only
            self._active_signature = None
            self._active_command_id = ""
            self._active_command = None
            self._active_goal_handle = None
            self._command_started_ns.pop(command.command_id, None)
            self._publish_status(command, "result_failed", False, f"action result failed: {exc}")
            return

        if wrapped_result is None:
            self._active_signature = None
            self._active_command_id = ""
            self._active_command = None
            self._active_goal_handle = None
            self._command_started_ns.pop(command.command_id, None)
            self._publish_status(command, "result_failed", False, "action result was empty")
            return

        result = wrapped_result.result
        if result is None:
            self._active_signature = None
            self._active_command_id = ""
            self._active_command = None
            self._active_goal_handle = None
            self._command_started_ns.pop(command.command_id, None)
            self._publish_status(command, "result_failed", False, "action result payload was empty")
            return

        self._publish_status(
            command,
            result.state or "completed",
            bool(result.success),
            result.message or f"completed via {self._action_name}",
        )
        self._active_signature = None
        self._active_command_id = ""
        self._active_command = None
        self._active_goal_handle = None
        self._command_started_ns.pop(command.command_id, None)

    def _dispatch_command(self, command: CommandEnvelope) -> None:
        if not self._action_client.wait_for_server(timeout_sec=self._server_wait_timeout_sec):
            self._publish_status(
                command,
                "server_unavailable",
                False,
                f"skill action server {self._action_name} not available",
            )
            return

        self._publish_status(
            command,
            "dispatching",
            True,
            f"dispatching goal to {self._action_name}",
        )
        self._active_signature = self._signature(command)
        self._active_command_id = command.command_id
        self._active_command = command
        self._active_goal_handle = None
        self._command_started_ns[command.command_id] = self.get_clock().now().nanoseconds
        goal = ExecuteSkill.Goal()
        goal.command_id = command.command_id
        goal.action = command.action
        goal.instrument_id = command.instrument_id
        goal.instrument_instance_id = command.instrument_instance_id
        goal.target_location_id = command.target_location_id
        goal.target_location_type = command.target_location_type
        goal.rationale = command.rationale
        goal.arm = command.arm
        goal.source_location_id = command.source_location_id
        goal.source_location_type = command.source_location_type
        goal.target_owner = command.target_owner
        goal.cleaning_required = bool(command.cleaning_required)
        goal.mode = command.mode

        send_future = self._action_client.send_goal_async(
            goal,
            feedback_callback=lambda feedback_message, command=command: self._on_feedback(
                command, feedback_message
            ),
        )
        send_future.add_done_callback(
            lambda result, command=command: self._on_goal_response(command, result)
        )

    def _cancel_active_goal(self, reason: str) -> None:
        command = self._active_command
        goal_handle = self._active_goal_handle
        if command is None:
            self._active_signature = None
            self._active_command_id = ""
            return
        self._publish_status(command, "cancel_requested", False, f"cancel requested by simulation {reason}")
        if goal_handle is None:
            self._cancelled_command_ids.add(command.command_id)
            self._active_signature = None
            self._active_command_id = ""
            self._active_command = None
            self._command_started_ns.pop(command.command_id, None)
            return
        try:
            goal_handle.cancel_goal_async()
        except Exception as exc:  # pragma: no cover - transport failures are runtime-only
            self.get_logger().warn(f"failed to cancel active skill goal: {exc}")

    def _on_control(self, msg: String) -> None:
        command = msg.data.strip().lower()
        if command in {"stop", "reset"}:
            self._cancel_active_goal(command)
        if command == "reset":
            self._request_dispatch_ledger.clear()

    def _on_command(self, msg: SkillCommand) -> None:
        command = self._coerce_command(msg)
        if command.action not in ALLOWED_ACTIONS:
            self._publish_status(
                command,
                "rejected",
                False,
                f"unsupported humanoid action '{command.action}'",
            )
            return
        signature = self._signature(command)
        if self._active_signature is not None:
            if signature == self._active_signature:
                self.get_logger().debug(
                    f"suppressing in-flight duplicate skill goal action={command.action} tool={command.instrument_id or 'none'}"
                )
                return
            self._publish_status(
                command,
                "skipped_while_busy",
                False,
                (
                    "ignored because the current action is still in flight; "
                    "BT will re-evaluate once the runtime state updates"
                ),
            )
            return
        if self._is_duplicate(command):
            self.get_logger().debug(
                f"suppressing duplicate skill goal action={command.action} tool={command.instrument_id or 'none'}"
            )
            return
        self._dispatch_command(command)


def main() -> None:
    rclpy.init()
    node = SkillActionBridge()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            node.destroy_node()
        finally:
            try:
                if rclpy.ok():
                    rclpy.shutdown()
            except Exception:
                pass
