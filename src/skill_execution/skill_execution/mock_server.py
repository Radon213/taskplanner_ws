"""Mock ROS 2 action server for surgical skill execution."""

from __future__ import annotations

import json
import time

import rclpy
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from std_msgs.msg import String
from surgical_msgs.action import ExecuteSkill
from surgical_msgs.msg import TwinEvent, WorldState


ALLOWED_ACTIONS = {
    "direct_handover",
    "pick_up_and_handover",
    "put_down_and_handover",
    "retrieve_from_hand",
    "retrieve_from_mayo",
    "predict_tool",
    "tool_handover",
    "tool_retrieve",
    "tool_predict",
    "predicted_tool_handover",
    "replace_and_handover",
}

ACTION_ALIASES = {
    "predict_tool": "tool_predict",
    "pick_up_and_handover": "tool_handover",
    "direct_handover": "predicted_tool_handover",
    "put_down_and_handover": "replace_and_handover",
    "retrieve_from_mayo": "tool_retrieve",
    "retrieve_from_hand": "tool_retrieve",
}


class SkillGoalInterrupted(Exception):
    pass


class MockSkillActionServer(Node):
    def __init__(self) -> None:
        super().__init__("mock_skill_server")
        self._action_name = str(self.declare_parameter("action_name", "/skill/execute").value)
        self._rack_pick_sec = float(self.declare_parameter("rack_pick_sec", 1.0).value)
        self._rack_to_handover_sec = float(self.declare_parameter("rack_to_handover_sec", 1.2).value)
        self._surgeon_handover_sec = float(self.declare_parameter("surgeon_handover_sec", 1.0).value)
        self._mayo_recovery_pickup_sec = float(self.declare_parameter("mayo_recovery_pickup_sec", 1.0).value)
        self._cleaner_insert_sec = float(self.declare_parameter("cleaner_insert_sec", 0.8).value)
        self._cleaning_hold_sec = float(self.declare_parameter("cleaning_hold_sec", 4.0).value)
        self._cleaner_to_rack_sec = float(self.declare_parameter("cleaner_to_rack_sec", 1.0).value)
        self._mayo_dwell_sec = float(self.declare_parameter("mayo_dwell_sec", 0.8).value)
        self._callback_group = ReentrantCallbackGroup()
        self._event_pub = self.create_publisher(TwinEvent, "/skill/events", 20)
        self._world: WorldState | None = None
        self._control_generation = 0
        self.create_subscription(
            WorldState,
            "/twin/world_state",
            self._on_world,
            20,
            callback_group=self._callback_group,
        )
        self.create_subscription(
            String,
            "/simulation/control_state",
            self._on_control,
            20,
            callback_group=self._callback_group,
        )
        self._server = ActionServer(
            self,
            ExecuteSkill,
            self._action_name,
            execute_callback=self._execute,
            goal_callback=self._on_goal,
            cancel_callback=self._on_cancel,
            callback_group=self._callback_group,
        )

    def destroy_node(self):
        self._server.destroy()
        return super().destroy_node()

    def _on_world(self, msg: WorldState) -> None:
        self._world = msg

    def _on_control(self, msg: String) -> None:
        command = msg.data.strip().lower()
        if command in {"stop", "reset"}:
            self._control_generation += 1

    def _find_instrument(self, instrument_id: str):
        if self._world is None:
            return None
        for instrument in self._world.instrument_states:
            if instrument.instrument_id == instrument_id:
                return instrument
        return None

    def _on_goal(self, goal_request: ExecuteSkill.Goal) -> GoalResponse:
        if goal_request.action not in ALLOWED_ACTIONS:
            self.get_logger().warning(
                f"rejected unsupported mock goal action={goal_request.action} tool={goal_request.instrument_id or 'none'}"
            )
            return GoalResponse.REJECT
        self.get_logger().info(
            f"accepted mock goal action={goal_request.action} tool={goal_request.instrument_id or 'none'} mode={goal_request.mode or 'default'}"
        )
        return GoalResponse.ACCEPT

    def _on_cancel(self, _goal_handle) -> CancelResponse:
        return CancelResponse.ACCEPT

    def _stamp(self):
        return self.get_clock().now().to_msg()

    def _publish_feedback(
        self, goal_handle, state: str, progress: float, message: str
    ) -> None:
        feedback = ExecuteSkill.Feedback()
        feedback.state = state
        feedback.progress = float(progress)
        feedback.message = message
        goal_handle.publish_feedback(feedback)

    def _guard_goal_active(self, goal_handle, generation: int) -> None:
        if goal_handle.is_cancel_requested or generation != self._control_generation:
            raise SkillGoalInterrupted()

    def _step_sleep(self, goal_handle, state: str, message: str, start_progress: float, end_progress: float, duration_sec: float, steps: int = 4, generation: int | None = None) -> None:
        if generation is not None:
            self._guard_goal_active(goal_handle, generation)
        if duration_sec <= 0.0:
            self._publish_feedback(goal_handle, state, end_progress, message)
            if generation is not None:
                self._guard_goal_active(goal_handle, generation)
            return
        total_steps = max(1, steps)
        for step in range(total_steps):
            if generation is not None:
                self._guard_goal_active(goal_handle, generation)
            progress = start_progress + ((step + 1) / total_steps) * (end_progress - start_progress)
            self._publish_feedback(goal_handle, state, progress, message)
            time.sleep(duration_sec / total_steps)
        if generation is not None:
            self._guard_goal_active(goal_handle, generation)

    def _publish_task_state(
        self,
        goal: ExecuteSkill.Goal,
        *,
        task_event_type: str,
        task_type: str,
        source_anchor_id: str,
        target_anchor_id: str,
        duration_sec: float,
    ) -> None:
        self._publish_event(
            self._make_event(
                goal,
                task_event_type,
                source_location_id=source_anchor_id,
                source_location_type=source_anchor_id,
                target_location_id=target_anchor_id,
                target_location_type=target_anchor_id,
                arm=goal.arm,
                detail={
                    "task_id": goal.command_id,
                    "task_type": task_type,
                    "duration_sec": float(duration_sec),
                    "source_anchor_id": source_anchor_id,
                    "target_anchor_id": target_anchor_id,
                },
            )
        )

    def _handover_duration_sec(self, goal: ExecuteSkill.Goal) -> float:
        if self._world is not None and self._world.right_hand_tool == goal.instrument_id:
            return self._surgeon_handover_sec
        return self._rack_pick_sec + self._rack_to_handover_sec + self._surgeon_handover_sec

    def _hold_in_cleaner(self, goal_handle, goal: ExecuteSkill.Goal, generation: int) -> None:
        steps = max(3, int(self._cleaning_hold_sec / 0.5))
        for step in range(steps):
            self._guard_goal_active(goal_handle, generation)
            remaining_sec = max(self._cleaning_hold_sec - ((step + 1) * (self._cleaning_hold_sec / steps)), 0.0)
            self._publish_event(
                self._make_event(
                    goal,
                    "ToolCleaningProgress",
                    instrument_id=goal.instrument_id,
                    location_id="cleaner_slot",
                    location_type="cleaner_slot",
                    owner="robot_left_hand",
                    status="cleaning",
                    source_location_id="robot_left_hand",
                    source_location_type="robot_left_hand",
                    target_location_id="cleaner_slot",
                    target_location_type="cleaner_slot",
                    target_owner="robot_left_hand",
                    arm=goal.arm or "left",
                    cleaning_required=True,
                    detail={"remaining_sec": round(remaining_sec, 2)},
                )
            )
            progress = 0.76 + ((step + 1) / steps) * 0.16
            self._publish_feedback(
                goal_handle,
                "cleaning",
                min(progress, 0.94),
                f"holding {goal.instrument_id} in cleaner with the {goal.arm or 'left'} arm ({remaining_sec:.1f}s left)",
            )
            time.sleep(self._cleaning_hold_sec / steps)
        self._guard_goal_active(goal_handle, generation)

    def _make_event(self, goal: ExecuteSkill.Goal, event_type: str, **kwargs) -> TwinEvent:
        event = TwinEvent()
        event.stamp = self._stamp()
        event.event_type = event_type
        event.instrument_id = kwargs.get("instrument_id", goal.instrument_id)
        event.phase_id = self._world.filtered_phase if self._world is not None else ""
        event.location_id = kwargs.get("location_id", "")
        event.location_type = kwargs.get("location_type", "")
        event.owner = kwargs.get("owner", "")
        event.status = kwargs.get("status", "")
        event.confidence = float(kwargs.get("confidence", 0.97))
        event.arm = kwargs.get("arm", goal.arm)
        event.source_location_id = kwargs.get("source_location_id", goal.source_location_id)
        event.source_location_type = kwargs.get("source_location_type", goal.source_location_type)
        event.target_location_id = kwargs.get("target_location_id", goal.target_location_id)
        event.target_location_type = kwargs.get("target_location_type", goal.target_location_type)
        event.target_owner = kwargs.get("target_owner", goal.target_owner)
        event.cleaning_required = bool(kwargs.get("cleaning_required", goal.cleaning_required))
        event.mode = kwargs.get("mode", goal.mode)
        detail = dict(kwargs.get("detail", {}))
        detail.setdefault("command_id", goal.command_id)
        detail.setdefault("transport", "ros2_action")
        detail.setdefault("rationale", goal.rationale)
        event.detail_json = json.dumps(detail, sort_keys=True)
        return event

    def _publish_event(self, event: TwinEvent) -> None:
        self._event_pub.publish(event)

    def _maybe_return_prepositioned_tool(
        self, goal: ExecuteSkill.Goal, event_type: str = "PredictedToolReturnedToRack"
    ) -> bool:
        if self._world is None:
            return False
        current_tool = self._world.right_hand_tool or self._world.prepositioned_tool
        if not current_tool or current_tool == goal.instrument_id:
            return False
        instrument = self._find_instrument(current_tool)
        if instrument is None:
            return False
        if instrument.location_type != "robot_right_hand" or instrument.status not in {"prepared", "held"}:
            return False
        self._publish_event(
            self._make_event(
                goal,
                event_type,
                instrument_id=current_tool,
                source_location_id="robot_right_hand",
                source_location_type="robot_right_hand",
                target_location_id=instrument.home_location_id,
                target_location_type=instrument.home_location_type,
                target_owner="none",
                location_id=instrument.home_location_id,
                location_type=instrument.home_location_type,
                owner="none",
                status="available",
                cleaning_required=False,
                mode="override_replace",
                detail={"reason": "prepositioned tool replaced before handover"},
            )
        )
        return True

    def _publish_pick_if_needed(self, goal: ExecuteSkill.Goal) -> None:
        if self._world is not None and self._world.right_hand_tool == goal.instrument_id:
            return
        instrument = self._find_instrument(goal.instrument_id)
        source_location_id = goal.source_location_id or (
            instrument.location_id if instrument is not None else "main_tray_slot_1"
        )
        source_location_type = goal.source_location_type or (
            instrument.location_type if instrument is not None else "tray_slot"
        )
        self._publish_event(
            self._make_event(
                goal,
                "RobotGraspedTool",
                source_location_id=source_location_id,
                source_location_type=source_location_type,
                target_location_id="robot_right_hand",
                target_location_type="robot_right_hand",
                target_owner="robot_right_hand",
                location_id="robot_right_hand",
                location_type="robot_right_hand",
                owner="robot_right_hand",
                status="held",
                arm=goal.arm or "right",
            )
        )

    def _execute(self, goal_handle):
        try:
            return self._execute_goal(goal_handle)
        except SkillGoalInterrupted:
            goal = goal_handle.request
            result = ExecuteSkill.Result()
            result.success = False
            if goal_handle.is_cancel_requested:
                result.state = "canceled"
                result.message = "skill goal canceled by runtime control"
                self._publish_feedback(goal_handle, result.state, 0.0, result.message)
                goal_handle.canceled()
            else:
                result.state = "aborted"
                result.message = "skill goal aborted because runtime was reset or stopped"
                self._publish_feedback(goal_handle, result.state, 0.0, result.message)
                goal_handle.abort()
            self.get_logger().info(
                f"{result.state} mock goal action={goal.action} tool={goal.instrument_id or 'none'}"
            )
            return result

    def _execute_goal(self, goal_handle):
        goal = goal_handle.request
        action = ACTION_ALIASES.get(goal.action, goal.action)
        generation = self._control_generation
        event = None
        message = ""

        if action == "tool_predict":
            duration_sec = self._rack_pick_sec + self._rack_to_handover_sec
            self._publish_task_state(
                goal,
                task_event_type="RobotTaskStarted",
                task_type=goal.action,
                source_anchor_id=goal.source_location_id or "main_tray_slot_1",
                target_anchor_id="robot_right_hand",
                duration_sec=duration_sec,
            )
            self._step_sleep(goal_handle, "picking_from_rack", f"picking {goal.instrument_id} from rack", 0.02, 0.38, self._rack_pick_sec, 4, generation)
            self._publish_pick_if_needed(goal)
            self._step_sleep(goal_handle, "ready_in_right_hand", f"holding {goal.instrument_id} ready in the right hand", 0.38, 0.94, self._rack_to_handover_sec, 4, generation)
            event = self._make_event(
                goal,
                "ToolPrepared",
                location_id="robot_right_hand",
                location_type="robot_right_hand",
                owner="robot_right_hand",
                status="prepared",
                target_location_id="robot_right_hand",
                target_location_type="robot_right_hand",
                target_owner="robot_right_hand",
                arm=goal.arm or "right",
                detail={"reserved_for": self._world.filtered_phase if self._world is not None else ""},
            )
            message = "tool prepositioned in the right hand"
        elif action == "tool_handover":
            duration_sec = self._handover_duration_sec(goal)
            self._publish_task_state(
                goal,
                task_event_type="RobotTaskStarted",
                task_type=goal.action,
                source_anchor_id=(goal.source_location_id or ("robot_right_hand" if self._world is not None and self._world.right_hand_tool == goal.instrument_id else "main_tray_slot_1")),
                target_anchor_id=goal.target_location_id or "surgeon_receive_zone",
                duration_sec=duration_sec,
            )
            if self._world is None or self._world.right_hand_tool != goal.instrument_id:
                self._step_sleep(goal_handle, "picking_from_rack", f"picking {goal.instrument_id} from rack", 0.02, 0.30, self._rack_pick_sec, 4, generation)
                self._publish_pick_if_needed(goal)
                self._step_sleep(goal_handle, "moving_to_handover", f"moving {goal.instrument_id} toward the handover zone", 0.30, 0.70, self._rack_to_handover_sec, 4, generation)
            else:
                self._step_sleep(goal_handle, "moving_to_handover", f"moving {goal.instrument_id} toward the handover zone", 0.02, 0.70, duration_sec - self._surgeon_handover_sec, 3, generation)
            self._step_sleep(goal_handle, "handover_to_surgeon", f"handing {goal.instrument_id} to the surgeon", 0.70, 0.96, self._surgeon_handover_sec, 4, generation)
            event = self._make_event(
                goal,
                "ToolHandoverCompleted",
                location_id="surgeon_hand",
                location_type="surgeon_hand",
                owner="surgeon",
                status="handed_over",
                source_location_id="robot_right_hand",
                source_location_type="robot_right_hand",
                target_location_id=goal.target_location_id or "surgeon_receive_zone",
                target_location_type=goal.target_location_type or "handover_zone",
                target_owner=goal.target_owner or "surgeon",
                arm=goal.arm or "right",
            )
            message = "tool handed over to surgeon"
        elif action == "predicted_tool_handover":
            duration_sec = self._surgeon_handover_sec
            self._publish_task_state(
                goal,
                task_event_type="RobotTaskStarted",
                task_type=goal.action,
                source_anchor_id=goal.source_location_id or "robot_right_hand",
                target_anchor_id=goal.target_location_id or "surgeon_receive_zone",
                duration_sec=duration_sec,
            )
            self._step_sleep(goal_handle, "handover_to_surgeon", f"handing predicted {goal.instrument_id} to the surgeon", 0.05, 0.96, duration_sec, 4, generation)
            event = self._make_event(
                goal,
                "ToolHandoverCompleted",
                location_id="surgeon_hand",
                location_type="surgeon_hand",
                owner="surgeon",
                status="handed_over",
                source_location_id="robot_right_hand",
                source_location_type="robot_right_hand",
                target_location_id=goal.target_location_id or "surgeon_receive_zone",
                target_location_type=goal.target_location_type or "handover_zone",
                target_owner=goal.target_owner or "surgeon",
                arm=goal.arm or "right",
            )
            message = "predicted tool handed over to surgeon"
        elif action == "replace_and_handover":
            duration_sec = self._cleaner_to_rack_sec + self._rack_pick_sec + self._rack_to_handover_sec + self._surgeon_handover_sec
            self._publish_task_state(
                goal,
                task_event_type="RobotTaskStarted",
                task_type=goal.action,
                source_anchor_id="robot_right_hand",
                target_anchor_id=goal.target_location_id or "surgeon_receive_zone",
                duration_sec=duration_sec,
            )
            current_tool = self._world.right_hand_tool if self._world is not None else ""
            if current_tool and current_tool != goal.instrument_id:
                self._step_sleep(
                    goal_handle,
                    "returning_prediction_to_rack",
                    f"returning predicted {current_tool} to the rack",
                    0.02,
                    0.24,
                    self._cleaner_to_rack_sec,
                    4,
                    generation,
                )
                self._maybe_return_prepositioned_tool(goal)
            self._step_sleep(goal_handle, "picking_from_rack", f"picking requested {goal.instrument_id} from rack", 0.24, 0.48, self._rack_pick_sec, 4, generation)
            self._publish_pick_if_needed(goal)
            self._step_sleep(goal_handle, "moving_to_handover", f"moving requested {goal.instrument_id} toward the handover zone", 0.48, 0.76, self._rack_to_handover_sec, 4, generation)
            self._step_sleep(goal_handle, "handover_to_surgeon", f"handing requested {goal.instrument_id} to the surgeon", 0.76, 0.96, self._surgeon_handover_sec, 4, generation)
            event = self._make_event(
                goal,
                "ToolHandoverCompleted",
                location_id="surgeon_hand",
                location_type="surgeon_hand",
                owner="surgeon",
                status="handed_over",
                source_location_id="robot_right_hand",
                source_location_type="robot_right_hand",
                target_location_id=goal.target_location_id or "surgeon_receive_zone",
                target_location_type=goal.target_location_type or "handover_zone",
                target_owner=goal.target_owner or "surgeon",
                arm=goal.arm or "right",
            )
            message = "predicted tool replaced and requested tool handed over"
        elif action == "tool_retrieve":
            instrument = self._find_instrument(goal.instrument_id)
            home_location_id = goal.target_location_id or (
                instrument.home_location_id if instrument is not None else "main_tray_slot_1"
            )
            home_location_type = goal.target_location_type or (
                instrument.home_location_type if instrument is not None else "tray_slot"
            )
            duration_sec = (
                self._mayo_recovery_pickup_sec +
                self._cleaner_insert_sec +
                self._cleaning_hold_sec +
                self._cleaner_to_rack_sec
            )
            self._publish_task_state(
                goal,
                task_event_type="RobotTaskStarted",
                task_type=goal.action,
                source_anchor_id=goal.source_location_id or "mayo_recovery_zone",
                target_anchor_id=home_location_id,
                duration_sec=duration_sec,
            )
            self._step_sleep(goal_handle, "retrieving_from_mayo", f"retrieving {goal.instrument_id} from the mayo recovery zone", 0.02, 0.20, self._mayo_recovery_pickup_sec, 4, generation)
            self._publish_event(
                self._make_event(
                    goal,
                    "ToolReceivedFromSurgeon",
                    location_id="robot_left_hand",
                    location_type="robot_left_hand",
                    owner="robot_left_hand",
                    status="received_return",
                    source_location_id=goal.source_location_id or "mayo_recovery_zone",
                    source_location_type=goal.source_location_type or "mayo_recovery_zone",
                    target_location_id="robot_left_hand",
                    target_location_type="robot_left_hand",
                    target_owner="robot_left_hand",
                    arm=goal.arm or "left",
                    cleaning_required=True,
                )
            )
            self._step_sleep(goal_handle, "inserting_into_cleaner", f"inserting {goal.instrument_id} into the cleaner", 0.20, 0.35, self._cleaner_insert_sec, 4, generation)
            self._publish_event(
                self._make_event(
                    goal,
                    "ToolSentToCleaner",
                    location_id="cleaner_slot",
                    location_type="cleaner_slot",
                    owner="none",
                    status="cleaning",
                    source_location_id="robot_left_hand",
                    source_location_type="robot_left_hand",
                    target_location_id="cleaner_slot",
                    target_location_type="cleaner_slot",
                    target_owner="none",
                    arm=goal.arm or "left",
                    cleaning_required=True,
                    detail={
                        "phase": "cleaning_started",
                        "remaining_sec": round(self._cleaning_hold_sec, 2),
                    },
                )
            )
            self._hold_in_cleaner(goal_handle, goal, generation)
            self._publish_event(
                self._make_event(
                    goal,
                    "ToolCleaningCompleted",
                    location_id="cleaner_slot",
                    location_type="cleaner_slot",
                    owner="none",
                    status="ready_to_return",
                    source_location_id="cleaner_slot",
                    source_location_type="cleaner_slot",
                    target_location_id="cleaner_slot",
                    target_location_type="cleaner_slot",
                    target_owner="none",
                    arm=goal.arm or "left",
                    cleaning_required=False,
                    detail={"phase": "cleaning_completed", "remaining_sec": 0.0},
                )
            )
            self._step_sleep(goal_handle, "returning_to_rack", f"returning {goal.instrument_id} to the rack", 0.92, 0.98, self._cleaner_to_rack_sec, 4, generation)
            event = self._make_event(
                goal,
                "ToolReturnedToTray",
                location_id=home_location_id,
                location_type=home_location_type,
                owner="none",
                status="available",
                source_location_id="cleaner_slot",
                source_location_type="cleaner_slot",
                target_location_id=home_location_id,
                target_location_type=home_location_type,
                target_owner="none",
                arm=goal.arm or "left",
                cleaning_required=False,
            )
            message = "tool retrieved, cleaned, and returned to rack"
        else:
            self._publish_feedback(goal_handle, "rejected", 0.0, f"unsupported action {goal.action}")
            goal_handle.abort()
            result = ExecuteSkill.Result()
            result.success = False
            result.state = "rejected"
            result.message = f"unsupported action {goal.action}"
            return result

        self._guard_goal_active(goal_handle, generation)
        self._publish_event(event)
        self._publish_task_state(
            goal,
            task_event_type="RobotTaskCompleted",
            task_type=goal.action,
            source_anchor_id=event.source_location_id or goal.source_location_id or "",
            target_anchor_id=event.target_location_id or goal.target_location_id or "",
            duration_sec=0.0,
        )
        self._publish_feedback(goal_handle, "completed", 1.0, message)
        goal_handle.succeed()
        result = ExecuteSkill.Result()
        result.success = True
        result.state = "completed"
        result.message = message
        result.resulting_event = event
        return result


def main() -> None:
    rclpy.init()
    node = MockSkillActionServer()
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
            try:
                if rclpy.ok():
                    rclpy.shutdown()
            except Exception:
                pass
