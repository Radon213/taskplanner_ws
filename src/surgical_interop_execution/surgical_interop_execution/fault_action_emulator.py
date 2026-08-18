"""Deterministic public Action/Service emulator for release fault campaigns."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from math import isfinite
from pathlib import Path
import threading
import time
from typing import Any

import rclpy
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from std_msgs.msg import String
from surgical_interop_msgs.action import (
    ExecuteRetractionAdjustment,
    ExecuteToolHandover,
)
from surgical_interop_msgs.msg import BedRobotArmState, BedRobotArmStateArray
from surgical_interop_msgs.srv import RequestToolChange
import yaml


SUPPORTED_OUTCOMES = {
    "success",
    "reject",
    "abort",
    "partial_failure",
    "timeout",
    "cancel_recovery_failed",
    "failed",
    "canceled",
    "protective_stop",
    "unknown",
}

_RETRACTION_FINAL_BY_OUTCOME = {
    "success": "completed",
    "cached": "completed",
    "failed": "fault",
    "abort": "fault",
    "partial_failure": "fault",
    "cancel_recovery_failed": "fault",
    "canceled": "canceled",
    "protective_stop": "protective_stop",
    "unknown": "unknown",
    "timeout": "unknown",
}

_TOOL_CHANGE_RESULT_BY_OUTCOME = {
    "success": "completed",
    "cached": "completed",
    "failed": "failed",
    "abort": "failed",
    "partial_failure": "failed",
    "cancel_recovery_failed": "failed",
    "timeout": "unknown",
    "canceled": "canceled",
    "protective_stop": "protective_stop",
    "unknown": "unknown",
    "reject": "failed",
}

_BED_ROBOT_STATUS_PERIOD_SEC = 0.5


@dataclass(frozen=True, slots=True)
class Outcome:
    outcome: str = "success"
    duration_sec: float = 0.4
    fail_progress: float = 0.5
    reason_code: str = ""


@dataclass
class RouteProfile:
    available: bool = True
    default: Outcome = field(default_factory=Outcome)
    sequence: list[Outcome] = field(default_factory=list)
    consumed: int = 0

    def next(self) -> Outcome:
        if self.consumed < len(self.sequence):
            value = self.sequence[self.consumed]
            self.consumed += 1
            return value
        return self.default


@dataclass
class EmulatorProfile:
    profile_id: str
    routes: dict[str, RouteProfile]

    @staticmethod
    def _outcome(payload: Any) -> Outcome:
        row = payload if isinstance(payload, dict) else {}
        name = str(row.get("outcome", "success"))
        if name not in SUPPORTED_OUTCOMES:
            raise ValueError(f"unsupported emulator outcome: {name}")
        return Outcome(
            outcome=name,
            duration_sec=max(0.0, float(row.get("duration_sec", 0.4))),
            fail_progress=min(1.0, max(0.0, float(row.get("fail_progress", 0.5)))),
            reason_code=str(row.get("reason_code", "")),
        )

    @classmethod
    def load(cls, path: str | Path) -> "EmulatorProfile":
        payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema") != "taskplanner.action_emulator.v1":
            raise ValueError("action emulator profile schema is invalid")
        routes: dict[str, RouteProfile] = {}
        for route in ("tool_handover", "retraction_adjustment", "tool_change"):
            row = payload.get("routes", {}).get(route, {})
            routes[route] = RouteProfile(
                available=bool(row.get("available", True)),
                default=cls._outcome(row.get("default", {})),
                sequence=[cls._outcome(item) for item in row.get("sequence", [])],
            )
        return cls(str(payload.get("profile_id", Path(path).stem)), routes)


def valid_tool_transition(source: str, target: str) -> bool:
    return (source.strip().lower(), target.strip().lower()) in {
        ("tray", "robot"),
        ("tray", "surgeon"),
        ("robot", "surgeon"),
        ("robot", "tray"),
        ("mayo", "robot"),
        ("mayo", "tray"),
    }


def validate_retraction_adjustment(
    request: Any, *, max_distance_mm: float = 30.0
) -> str:
    """Return a document-contract reason code, or an empty string when valid."""

    if not str(getattr(request, "command_id", "")).strip():
        return "missing_command_id"
    mode = str(getattr(request, "adjustment_mode", "")).strip()
    target = str(getattr(request, "target_retractor_id", "")).strip()
    frame = str(getattr(request, "direction_frame", "")).strip()
    direction = str(getattr(request, "direction", "")).strip()
    axis = str(getattr(request, "axis", "")).strip()
    try:
        distance_mm = float(getattr(request, "distance_mm"))
        maximum = float(max_distance_mm)
    except (TypeError, ValueError):
        return "invalid_distance_mm"
    if frame != "surgeon_view":
        return "invalid_direction_frame"
    if (
        not isfinite(distance_mm)
        or not isfinite(maximum)
        or maximum <= 0.0
        or distance_mm <= 0.0
        or distance_mm > maximum
    ):
        return "invalid_distance_mm"
    if mode == "single":
        if target not in {"left_malleable", "right_malleable"}:
            return "invalid_target_retractor_id"
        if direction not in {"up", "down", "left", "right"} or axis != "none":
            return "invalid_single_adjustment"
        return ""
    if mode == "multi":
        if target != "both_malleable":
            return "invalid_target_retractor_id"
        if direction != "none" or axis not in {"left_right", "up_down"}:
            return "invalid_multi_adjustment"
        return ""
    return "invalid_adjustment_mode"


class FaultActionEmulator(Node):
    def __init__(self) -> None:
        super().__init__("fault_action_emulator")
        self.declare_parameter("profile_path", "")
        profile_path = str(self.get_parameter("profile_path").value).strip()
        if not profile_path:
            raise RuntimeError("fault action emulator requires profile_path")
        self._profile = EmulatorProfile.load(profile_path)
        self._lock = threading.RLock()
        self._active_ids: set[str] = set()
        self._selected_outcomes: dict[tuple[str, str], Outcome] = {}
        self._completed: dict[tuple[str, str], dict[str, Any]] = {}
        self._route_counts: dict[str, dict[str, int]] = {}
        self._bed_robot_revision = 0
        self._max_retraction_distance_mm = float(
            self.declare_parameter("max_retraction_distance_mm", 30.0).value
        )
        self._procedure_type = str(
            self.declare_parameter("procedure_type", "nephrectomy").value
        ).strip()
        self._status_pub = self.create_publisher(String, "/test/action_emulator/status", 10)
        callback_group = ReentrantCallbackGroup()
        self._servers: list[Any] = []

        if self._profile.routes["tool_handover"].available:
            self._servers.append(
                ActionServer(
                    self,
                    ExecuteToolHandover,
                    "/surgery/tool_handover",
                    goal_callback=lambda request: self._goal("tool_handover", request),
                    cancel_callback=self._cancel,
                    execute_callback=lambda handle: self._execute_tool(handle),
                    callback_group=callback_group,
                )
            )
        if self._profile.routes["retraction_adjustment"].available:
            self._servers.append(
                ActionServer(
                    self,
                    ExecuteRetractionAdjustment,
                    "/surgery/retraction/adjust",
                    goal_callback=lambda request: self._goal(
                        "retraction_adjustment", request
                    ),
                    cancel_callback=self._cancel,
                    execute_callback=lambda handle: self._execute_retraction(handle),
                    callback_group=callback_group,
                )
            )
        if self._profile.routes["tool_change"].available:
            self.create_service(
                RequestToolChange,
                "/surgery/tool_change/request",
                self._request_tool_change,
                callback_group=callback_group,
            )
        self._bed_robot_status_pub = self.create_publisher(
            BedRobotArmStateArray, "/external/bed_robot_arms/status", 10
        )
        self._start_bed_robot_status_heartbeat()
        self.create_timer(1.0, self._publish_status)

    def _start_bed_robot_status_heartbeat(self) -> None:
        """Publish the initial snapshot now, then refresh it at the safe cadence."""

        self._publish_bed_robot_status()
        self._bed_robot_status_timer = self.create_timer(
            _BED_ROBOT_STATUS_PERIOD_SEC,
            self._publish_bed_robot_status,
        )

    def _count(self, route: str, outcome: str) -> None:
        values = self._route_counts.setdefault(route, {})
        values[outcome] = values.get(outcome, 0) + 1

    @staticmethod
    def _command_id(request: Any) -> str:
        return str(getattr(request, "command_id", "")).strip()

    def _goal(self, route: str, request: Any) -> GoalResponse:
        command_id = self._command_id(request)
        if not command_id:
            self._count(route, "rejected_missing_command_id")
            return GoalResponse.REJECT
        if route == "tool_handover" and not valid_tool_transition(
            request.source_location, request.target_location
        ):
            self._count(route, "rejected_invalid_transition")
            return GoalResponse.REJECT
        if route == "retraction_adjustment":
            reason = validate_retraction_adjustment(
                request,
                max_distance_mm=self._max_retraction_distance_mm,
            )
            if reason:
                self._count(route, f"rejected_{reason}")
                return GoalResponse.REJECT
        with self._lock:
            if command_id in self._active_ids:
                self._count(route, "rejected_duplicate_active")
                return GoalResponse.REJECT
            cached_result = self._completed.get((route, command_id))
            outcome = (
                Outcome(
                    outcome="cached",
                    duration_sec=0.0,
                    reason_code=str(cached_result.get("reason_code", "completed")),
                )
                if cached_result is not None
                else self._profile.routes[route].next()
            )
            if outcome.outcome == "reject":
                self._count(route, "rejected_profile")
                return GoalResponse.REJECT
            self._active_ids.add(command_id)
            self._selected_outcomes[(route, command_id)] = outcome
        return GoalResponse.ACCEPT

    @staticmethod
    def _cancel(_goal_handle) -> CancelResponse:
        return CancelResponse.ACCEPT

    def _selected_outcome(self, route: str, command_id: str) -> Outcome:
        with self._lock:
            cached = self._completed.get((route, command_id))
        if cached is not None:
            return Outcome(
                outcome=str(cached["outcome"]),
                duration_sec=0.0,
                reason_code=str(cached.get("reason_code", "")),
            )
        with self._lock:
            return self._selected_outcomes.get(
                (route, command_id), self._profile.routes[route].default
            )

    def _finish(self, route: str, command_id: str, outcome: str, reason_code: str) -> None:
        with self._lock:
            self._active_ids.discard(command_id)
            self._selected_outcomes.pop((route, command_id), None)
            self._completed[(route, command_id)] = {
                "outcome": outcome,
                "reason_code": reason_code,
            }
        self._count(route, outcome)

    def _run_action(self, route: str, goal_handle: Any, feedback_type: Any) -> tuple[str, str, float]:
        command_id = self._command_id(goal_handle.request)
        outcome = self._selected_outcome(route, command_id)
        duration = outcome.duration_sec
        if outcome.outcome == "timeout":
            duration = max(duration, 30.0)
        started = time.monotonic()
        progress = 0.0
        while progress < 1.0:
            elapsed = time.monotonic() - started
            progress = 1.0 if duration <= 0.0 else min(1.0, elapsed / duration)
            feedback = feedback_type()
            if hasattr(feedback, "progress"):
                feedback.progress = float(progress)
            if hasattr(feedback, "command_id"):
                feedback.command_id = command_id
            feedback.state = (
                "adjusting" if route == "retraction_adjustment" else "moving_to_target"
            )
            goal_handle.publish_feedback(feedback)
            if goal_handle.is_cancel_requested:
                if route == "retraction_adjustment":
                    recovery_feedback = feedback_type()
                    recovery_feedback.state = "recovering"
                    if hasattr(recovery_feedback, "command_id"):
                        recovery_feedback.command_id = command_id
                    goal_handle.publish_feedback(recovery_feedback)
                if outcome.outcome == "cancel_recovery_failed":
                    goal_handle.abort()
                    return "fault", "cancel_recovery_failed", progress
                goal_handle.canceled()
                if route == "retraction_adjustment":
                    return "canceled", outcome.reason_code or "canceled", 1.0
                source = str(getattr(goal_handle.request, "source_location", ""))
                reason = (
                    "canceled_recovered_to_tray"
                    if source == "robot" or progress >= 0.35
                    else "canceled_source_unchanged"
                )
                return "canceled", reason, 1.0
            if outcome.outcome == "partial_failure" and progress >= outcome.fail_progress:
                goal_handle.abort()
                return (
                    "fault" if route == "retraction_adjustment" else "failed",
                    outcome.reason_code or "partial_failure",
                    progress,
                )
            time.sleep(0.02)
        if route == "retraction_adjustment":
            final_state = _RETRACTION_FINAL_BY_OUTCOME[outcome.outcome]
            if final_state == "completed":
                goal_handle.succeed()
            elif final_state == "canceled":
                goal_handle.canceled()
            else:
                goal_handle.abort()
            return (
                final_state,
                outcome.reason_code or final_state,
                1.0 if final_state in {"completed", "canceled"} else progress,
            )
        if outcome.outcome in {
            "abort",
            "timeout",
            "failed",
            "protective_stop",
            "unknown",
            "canceled",
        }:
            if outcome.outcome == "canceled":
                goal_handle.canceled()
                return "canceled", outcome.reason_code or "canceled", 1.0
            goal_handle.abort()
            return "failed", outcome.reason_code or f"emulated_{outcome.outcome}", progress
        goal_handle.succeed()
        return "completed", outcome.reason_code or "completed", 1.0

    def _execute_tool(self, goal_handle):
        command_id = self._command_id(goal_handle.request)
        state, reason, _ = self._run_action(
            "tool_handover", goal_handle, ExecuteToolHandover.Feedback
        )
        result = ExecuteToolHandover.Result()
        result.success = state == "completed"
        result.final_state = state
        result.reason_code = reason
        self._finish("tool_handover", command_id, state, reason)
        return result

    def _execute_retraction(self, goal_handle):
        command_id = self._command_id(goal_handle.request)
        state, reason, _ = self._run_action(
            "retraction_adjustment", goal_handle, ExecuteRetractionAdjustment.Feedback
        )
        result = ExecuteRetractionAdjustment.Result()
        result.success = state == "completed"
        result.final_state = state
        result.reason_code = reason
        self._finish("retraction_adjustment", command_id, state, reason)
        return result

    def _request_tool_change(self, request, response):
        command_id = self._command_id(request)
        if not command_id:
            response.success = False
            response.result = "failed"
            response.reason_code = "missing_command_id"
            self._count("tool_change", "rejected_missing_command_id")
            return response
        if request.arm_id not in {"arm_1", "arm_2"}:
            response.success = False
            response.result = "failed"
            response.reason_code = "invalid_arm_id"
            self._count("tool_change", "rejected_invalid_arm_id")
            return response
        if request.target_tool_id not in {
            "thyroid_retractor",
            "army_navy_retractor",
        }:
            response.success = False
            response.result = "failed"
            response.reason_code = "invalid_target_tool"
            self._count("tool_change", "rejected_invalid_target_tool")
            return response
        with self._lock:
            cached = self._completed.get(("tool_change", command_id))
        if cached is None:
            with self._lock:
                if command_id in self._active_ids:
                    response.success = False
                    response.result = "failed"
                    response.reason_code = "duplicate_active_command_id"
                    self._count("tool_change", "rejected_duplicate_active")
                    return response
                outcome = self._profile.routes["tool_change"].next()
                self._active_ids.add(command_id)
            delay_sec = outcome.duration_sec
            if outcome.outcome == "timeout":
                delay_sec = max(delay_sec, 30.0)
            if delay_sec > 0.0:
                time.sleep(delay_sec)
            state = _TOOL_CHANGE_RESULT_BY_OUTCOME[outcome.outcome]
            reason = outcome.reason_code or (
                "sequence_completed_attachment_unverified"
                if state == "completed"
                else state
            )
            self._finish("tool_change", command_id, state, reason)
        else:
            state = str(cached["outcome"])
            reason = str(cached["reason_code"])
            self._count("tool_change", "idempotent_replay")
        response.success = state == "completed"
        response.result = state
        response.reason_code = reason
        return response

    def _publish_bed_robot_status(self) -> None:
        message = BedRobotArmStateArray()
        message.stamp = self.get_clock().now().to_msg()
        self._bed_robot_revision += 1
        message.revision = self._bed_robot_revision
        message.procedure_type = self._procedure_type
        configured_arms = (
            (("arm_1", "army_navy"),)
            if "thyroid" in self._procedure_type.casefold()
            else (
                ("arm_1", "left_malleable"),
                ("arm_2", "right_malleable"),
            )
        )
        for arm_id, role_instance_id in configured_arms:
            arm = BedRobotArmState()
            arm.arm_id = arm_id
            arm.role = "retraction"
            arm.role_instance_id = role_instance_id
            arm.state = "standby"
            arm.direct_teach_active = False
            arm.reason_code = "ok"
            message.arms.append(arm)
        self._bed_robot_status_pub.publish(message)

    def _publish_status(self) -> None:
        message = String()
        with self._lock:
            payload = {
                "schema": "taskplanner.action_emulator_status.v1",
                "profile_id": self._profile.profile_id,
                "active_command_ids": sorted(self._active_ids),
                "counts": self._route_counts,
            }
        message.data = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        self._status_pub.publish(message)


def main() -> None:
    rclpy.init()
    node = FaultActionEmulator()
    executor = MultiThreadedExecutor(num_threads=4)
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
