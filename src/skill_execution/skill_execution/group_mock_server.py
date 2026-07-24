"""Mock action servers for suction and retraction robot-arm groups."""

from __future__ import annotations

import threading
import time

from procedure_spec import get_default_spec_dir, load_bundle
import rclpy
from rcl_interfaces.msg import SetParametersResult
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from std_msgs.msg import String
from surgical_msgs.action import ExecuteBedRobotArmGroup

from .group_contract import (
    GROUP_RETRACTION,
    GROUP_SUCTION,
    OPERATION_CHANGE_END_EFFECTOR,
    OPERATION_RELEASE_RETRACTION,
    OPERATION_RETRACTION,
    OPERATION_SUCTION_START,
    OPERATION_SUCTION_STOP,
    mock_safety_rejection,
    validate_command_values,
)


class GroupGoalInterrupted(Exception):
    pass


class MockBedRobotArmGroupActionServer(Node):
    """Host two independent group endpoints without modelling individual arms."""

    def __init__(self) -> None:
        super().__init__("mock_bed_robot_arm_group_server")
        self.declare_parameter("spec_dir", str(get_default_spec_dir()))
        self._spec_dir = str(self.get_parameter("spec_dir").value)
        profile_defaults = self._profiles_from_spec(self._spec_dir)
        self._action_names = {
            GROUP_SUCTION: str(
                self.declare_parameter(
                    "suction_action_name",
                    "/bed_robot_arm_group/suction/execute",
                ).value
            ),
            GROUP_RETRACTION: str(
                self.declare_parameter(
                    "retraction_action_name",
                    "/bed_robot_arm_group/retraction/execute",
                ).value
            ),
        }
        self._max_retraction_mm = float(
            self.declare_parameter("max_retraction_mm", 30.0).value
        )
        if self._max_retraction_mm <= 0.0:
            raise ValueError("max_retraction_mm must be greater than zero")
        self._suction_transition_sec = float(
            self.declare_parameter("suction_transition_sec", 0.4).value
        )
        self._retraction_sec = float(
            self.declare_parameter("retraction_sec", 1.2).value
        )
        self._release_sec = float(
            self.declare_parameter("release_sec", 0.6).value
        )
        self._end_effector_change_sec = float(
            self.declare_parameter("end_effector_change_sec", 1.2).value
        )
        self._approach_sec = float(
            self.declare_parameter("approach_sec", 0.6).value
        )
        self._initial_profiles = {
            GROUP_SUCTION: str(
                self.declare_parameter(
                    "suction_initial_end_effector_profile",
                    profile_defaults.get(GROUP_SUCTION, "suction"),
                ).value
            ),
            GROUP_RETRACTION: str(
                self.declare_parameter(
                    "retraction_initial_end_effector_profile",
                    profile_defaults.get(GROUP_RETRACTION, ""),
                ).value
            ),
        }
        self._callback_group = ReentrantCallbackGroup()
        self._lock = threading.RLock()
        self._active_groups: set[str] = set()
        self._control_generation = 0
        self._accepting_commands = False
        self._accepted_generations: dict[str, int] = {}
        self._states = {
            GROUP_SUCTION: "standby",
            GROUP_RETRACTION: "standby",
        }
        self._end_effector_profiles = dict(self._initial_profiles)
        self.add_on_set_parameters_callback(self._on_parameters_changed)
        self.create_subscription(
            String,
            "/simulation/control_state",
            self._on_control,
            20,
            callback_group=self._callback_group,
        )
        self._servers = [
            ActionServer(
                self,
                ExecuteBedRobotArmGroup,
                action_name,
                execute_callback=(
                    lambda goal_handle, expected_group=group_id: self._execute(
                        expected_group, goal_handle
                    )
                ),
                goal_callback=(
                    lambda goal_request, expected_group=group_id: self._on_goal(
                        expected_group, goal_request
                    )
                ),
                cancel_callback=self._on_cancel,
                callback_group=self._callback_group,
            )
            for group_id, action_name in self._action_names.items()
        ]

    @staticmethod
    def _profiles_from_spec(spec_dir: str) -> dict[str, str]:
        spec = load_bundle(spec_dir)
        group_spec = spec.get_bed_robot_arm_group_spec()
        if group_spec is None:
            return {}
        return {
            group.id: group.initial_end_effector_profile
            for group in group_spec.groups
        }

    def _on_parameters_changed(self, params):
        for parameter in params:
            if parameter.name != "spec_dir":
                continue
            try:
                spec_dir = str(parameter.value)
                profiles = self._profiles_from_spec(spec_dir)
            except Exception as exc:
                return SetParametersResult(
                    successful=False,
                    reason=f"failed to reload group controller spec: {exc}",
                )
            with self._lock:
                self._spec_dir = spec_dir
                self._initial_profiles = {
                    GROUP_SUCTION: profiles.get(GROUP_SUCTION, ""),
                    GROUP_RETRACTION: profiles.get(GROUP_RETRACTION, ""),
                }
                self._end_effector_profiles = dict(self._initial_profiles)
                self._states[GROUP_SUCTION] = "standby"
                self._states[GROUP_RETRACTION] = "standby"
                self._control_generation += 1
        return SetParametersResult(successful=True)

    def destroy_node(self):
        for server in self._servers:
            server.destroy()
        return super().destroy_node()

    def _on_control(self, msg: String) -> None:
        control = msg.data.partition(":")[0].strip().lower()
        if control in {"start", "start_actors"}:
            with self._lock:
                self._accepting_commands = True
            return
        if control not in {"stop", "reset"}:
            return
        with self._lock:
            self._accepting_commands = False
            self._control_generation += 1
            self._states[GROUP_SUCTION] = "standby"
            self._states[GROUP_RETRACTION] = "standby"
            if control == "reset":
                self._end_effector_profiles = dict(self._initial_profiles)

    def _on_goal(
        self, expected_group: str, goal_request: ExecuteBedRobotArmGroup.Goal
    ) -> GoalResponse:
        command = goal_request.command
        validation_error = validate_command_values(
            group_id=command.group_id,
            operation=command.operation,
            direction=command.direction,
            distance_mm=float(command.distance_mm),
            distance_origin=command.distance_origin,
            confidence=float(command.confidence),
        )
        if command.group_id != expected_group:
            validation_error = (
                f"command group '{command.group_id}' does not match endpoint "
                f"'{expected_group}'"
            )
        if validation_error:
            self.get_logger().warning(
                f"rejected invalid {expected_group} group goal: {validation_error}"
            )
            return GoalResponse.REJECT
        with self._lock:
            if not self._accepting_commands:
                self.get_logger().warning(
                    f"rejected {expected_group} group goal while runtime is stopped"
                )
                return GoalResponse.REJECT
            if expected_group in self._active_groups:
                self.get_logger().warning(
                    f"rejected {expected_group} group goal because the group is busy"
                )
                return GoalResponse.REJECT
            self._active_groups.add(expected_group)
            self._accepted_generations[self._goal_key(command)] = (
                self._control_generation
            )
        self.get_logger().info(
            f"accepted {expected_group} group goal operation={command.operation} "
            f"direction={command.direction or 'none'} "
            f"distance_mm={float(command.distance_mm):g}"
        )
        return GoalResponse.ACCEPT

    @staticmethod
    def _on_cancel(_goal_handle) -> CancelResponse:
        return CancelResponse.ACCEPT

    @staticmethod
    def _goal_key(command) -> str:
        return f"{command.request_id}:{command.command_id}:{command.group_id}"

    def _state(self, group_id: str) -> str:
        with self._lock:
            return self._states[group_id]

    def _set_state(self, group_id: str, state: str) -> None:
        with self._lock:
            self._states[group_id] = state

    def _profile(self, group_id: str) -> str:
        with self._lock:
            return self._end_effector_profiles[group_id]

    def _set_profile(self, group_id: str, profile: str) -> None:
        with self._lock:
            self._end_effector_profiles[group_id] = profile

    def _guard_active(self, goal_handle, generation: int) -> None:
        with self._lock:
            generation_changed = generation != self._control_generation
        if goal_handle.is_cancel_requested or generation_changed:
            raise GroupGoalInterrupted()

    def _publish_feedback(
        self, goal_handle, state: str, progress: float, message: str
    ) -> None:
        feedback = ExecuteBedRobotArmGroup.Feedback()
        feedback.state = state
        feedback.progress = float(max(0.0, min(1.0, progress)))
        feedback.message = message
        goal_handle.publish_feedback(feedback)

    def _run_step(
        self,
        goal_handle,
        *,
        group_id: str,
        state: str,
        message: str,
        start_progress: float,
        end_progress: float,
        duration_sec: float,
        generation: int,
        steps: int = 4,
    ) -> None:
        self._guard_active(goal_handle, generation)
        self._set_state(group_id, state)
        total_steps = max(1, steps)
        if duration_sec <= 0.0:
            self._guard_active(goal_handle, generation)
            self._publish_feedback(goal_handle, state, end_progress, message)
            return
        for step in range(total_steps):
            self._guard_active(goal_handle, generation)
            progress = start_progress + (
                ((step + 1) / total_steps) * (end_progress - start_progress)
            )
            self._publish_feedback(goal_handle, state, progress, message)
            time.sleep(duration_sec / total_steps)
        self._guard_active(goal_handle, generation)

    def _result(
        self,
        *,
        group_id: str,
        success: bool,
        outcome: str,
        message: str,
        error_code: str = "",
        rejection_reason: str = "",
    ) -> ExecuteBedRobotArmGroup.Result:
        result = ExecuteBedRobotArmGroup.Result()
        result.success = bool(success)
        result.outcome = outcome
        result.final_state = self._state(group_id)
        result.message = message
        result.error_code = error_code
        result.rejection_reason = rejection_reason
        result.end_effector_profile = self._profile(group_id)
        return result

    def _execute(self, expected_group: str, goal_handle):
        try:
            return self._execute_goal(expected_group, goal_handle)
        except GroupGoalInterrupted:
            self._set_state(expected_group, "standby")
            if goal_handle.is_cancel_requested:
                outcome = "canceled"
                message = "group goal canceled by runtime control"
                goal_handle.canceled()
            else:
                outcome = "aborted"
                message = "group goal aborted because runtime was reset or stopped"
                goal_handle.abort()
            return self._result(
                group_id=expected_group,
                success=False,
                outcome=outcome,
                message=message,
                error_code=f"goal_{outcome}",
            )
        finally:
            with self._lock:
                self._active_groups.discard(expected_group)

    def _execute_goal(self, group_id: str, goal_handle):
        command = goal_handle.request.command
        with self._lock:
            generation = self._accepted_generations.pop(
                self._goal_key(command),
                -1,
            )
        self._guard_active(goal_handle, generation)

        error_code, rejection_reason = mock_safety_rejection(
            operation=command.operation,
            distance_mm=float(command.distance_mm),
            max_retraction_mm=self._max_retraction_mm,
        )
        if error_code:
            message = f"downstream safety controller rejected command: {rejection_reason}"
            self._publish_feedback(goal_handle, self._state(group_id), 0.0, message)
            goal_handle.abort()
            return self._result(
                group_id=group_id,
                success=False,
                outcome="rejected",
                message=message,
                error_code=error_code,
                rejection_reason=rejection_reason,
            )

        if command.operation == OPERATION_SUCTION_START:
            if self._state(group_id) == "suctioning":
                message = "suction group is already suctioning"
            else:
                self._run_step(
                    goal_handle,
                    group_id=group_id,
                    state="suctioning",
                    message="starting suction",
                    start_progress=0.0,
                    end_progress=0.95,
                    duration_sec=self._suction_transition_sec,
                    generation=generation,
                )
                message = "suction started"
            final_state = "suctioning"
        elif command.operation == OPERATION_SUCTION_STOP:
            if self._state(group_id) == "standby":
                message = "suction group is already stopped"
            else:
                self._run_step(
                    goal_handle,
                    group_id=group_id,
                    state="stopping",
                    message="stopping suction",
                    start_progress=0.0,
                    end_progress=0.95,
                    duration_sec=self._suction_transition_sec,
                    generation=generation,
                )
                message = "suction stopped"
            final_state = "standby"
        elif command.operation == OPERATION_RETRACTION:
            self._run_step(
                goal_handle,
                group_id=group_id,
                state="retracting",
                message=(
                    f"retracting {command.direction} by "
                    f"{float(command.distance_mm):g} mm"
                ),
                start_progress=0.0,
                end_progress=0.95,
                duration_sec=self._retraction_sec,
                generation=generation,
            )
            final_state = "holding"
            message = (
                f"holding retraction after {command.direction} "
                f"{float(command.distance_mm):g} mm increment"
            )
        elif command.operation == OPERATION_RELEASE_RETRACTION:
            if self._state(group_id) == "standby":
                message = "retraction group is already released"
            else:
                self._run_step(
                    goal_handle,
                    group_id=group_id,
                    state="releasing",
                    message="releasing retraction",
                    start_progress=0.0,
                    end_progress=0.95,
                    duration_sec=self._release_sec,
                    generation=generation,
                )
                message = "retraction released"
            final_state = "standby"
        elif command.operation == OPERATION_CHANGE_END_EFFECTOR:
            if not command.end_effector_profile.strip():
                rejection_reason = (
                    "change_end_effector requires a non-empty end_effector_profile"
                )
                self._publish_feedback(
                    goal_handle, self._state(group_id), 0.0, rejection_reason
                )
                goal_handle.abort()
                return self._result(
                    group_id=group_id,
                    success=False,
                    outcome="rejected",
                    message=rejection_reason,
                    error_code="end_effector_profile_required",
                    rejection_reason=rejection_reason,
                )
            start_progress = 0.0
            if self._state(group_id) == "holding":
                self._run_step(
                    goal_handle,
                    group_id=group_id,
                    state="releasing",
                    message="releasing before end-effector change",
                    start_progress=0.0,
                    end_progress=0.2,
                    duration_sec=self._release_sec,
                    generation=generation,
                )
                start_progress = 0.2
            self._run_step(
                goal_handle,
                group_id=group_id,
                state="changing_end_effector",
                message=(
                    f"changing group end-effector profile to "
                    f"{command.end_effector_profile}"
                ),
                start_progress=start_progress,
                end_progress=0.72,
                duration_sec=self._end_effector_change_sec,
                generation=generation,
            )
            self._set_profile(group_id, command.end_effector_profile)
            self._run_step(
                goal_handle,
                group_id=group_id,
                state="approaching",
                message="returning retraction group to the approach position",
                start_progress=0.72,
                end_progress=0.95,
                duration_sec=self._approach_sec,
                generation=generation,
            )
            final_state = "standby"
            message = (
                f"end-effector profile changed to {command.end_effector_profile}"
            )
        else:  # guarded by validate_command_values
            raise RuntimeError(f"unreachable operation {command.operation}")

        self._guard_active(goal_handle, generation)
        self._set_state(group_id, final_state)
        self._publish_feedback(goal_handle, final_state, 1.0, message)
        goal_handle.succeed()
        return self._result(
            group_id=group_id,
            success=True,
            outcome="completed",
            message=message,
        )


def main() -> None:
    rclpy.init()
    node = MockBedRobotArmGroupActionServer()
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
