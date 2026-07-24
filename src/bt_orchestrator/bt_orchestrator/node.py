"""Decision bridge for BT summaries and external blackboard mirroring."""

from __future__ import annotations

import time
from typing import Any

import rclpy
from rclpy.node import Node
from rclpy.parameter import Parameter
from rclpy.parameter_client import AsyncParameterClient
from std_msgs.msg import String
from surgical_msgs.msg import BTDecision, SkillStatus, WorldState

from .guards import should_allow_handover
from .selectors import select_expected_tool


def _is_available_status(status: str) -> bool:
    return status in {"available", "prepared", "held"}


class BlackboardMirror:
    def __init__(self, node: Node, target_node_name: str, period_sec: float) -> None:
        self._node = node
        self._client = AsyncParameterClient(node, target_node_name)
        self._target_node_name = target_node_name
        self._desired: dict[str, Any] = {}
        self._dirty_keys: set[str] = set()
        self._request_inflight = False
        self._service_ready_logged = False
        self._timer = node.create_timer(period_sec, self._flush)

    def queue(self, key: str, value: Any, *, force: bool = False) -> None:
        if not force and self._desired.get(key) == value:
            return
        self._desired[key] = value
        self._dirty_keys.add(key)

    def queue_many(self, values: dict[str, Any], *, force_keys: set[str] | None = None) -> None:
        forced = force_keys or set()
        for key, value in values.items():
            self.queue(key, value, force=key in forced)

    def _flush(self) -> None:
        if not self._dirty_keys or self._request_inflight:
            return

        if not self._client.services_are_ready():
            return

        if not self._service_ready_logged:
            self._node.get_logger().info(
                f"blackboard mirror connected to parameter service on {self._target_node_name}"
            )
            self._service_ready_logged = True

        keys = sorted(self._dirty_keys)
        snapshot = {key: self._desired[key] for key in keys}
        parameters = [Parameter(name=f"bb.{key}", value=value) for key, value in snapshot.items()]

        self._request_inflight = True
        future = self._client.set_parameters(parameters)
        future.add_done_callback(lambda result, snapshot=snapshot: self._on_flush_done(result, snapshot))

    def _on_flush_done(self, future, snapshot: dict[str, Any]) -> None:
        self._request_inflight = False
        try:
            response = future.result()
        except Exception as exc:  # pragma: no cover - transport failures are runtime-only
            self._node.get_logger().warning(f"blackboard mirror flush failed: {exc}")
            return

        results = getattr(response, "results", [])
        failed_reasons = [result.reason for result in results if not result.successful]
        if failed_reasons:
            self._node.get_logger().warning(
                "blackboard mirror rejected parameters: " + "; ".join(reason or "unknown" for reason in failed_reasons)
            )
            return

        for key, sent_value in snapshot.items():
            if self._desired.get(key) == sent_value:
                self._dirty_keys.discard(key)


class DecisionBridgeNode(Node):
    def __init__(self) -> None:
        super().__init__("bt_decision_bridge")
        target_node_name = self.declare_parameter("target_node_name", "/tree_executor").value
        mirror_period_sec = float(self.declare_parameter("mirror_period_sec", 0.2).value)
        self._summary_pub = self.create_publisher(String, "/bt/decision_summary", 10)
        self._latest_world: WorldState | None = None
        self._last_summary_by_channel: dict[str, str] = {}
        self._seen_tool_ids: set[str] = set()
        self._last_procedure_id = ""
        self._last_running = False
        self._last_full_refresh_sec = 0.0
        self._bundle_generation = 0
        self._mirror = BlackboardMirror(self, str(target_node_name), mirror_period_sec)
        self.create_subscription(WorldState, "/twin/world_state", self._on_world, 20)
        self.create_subscription(BTDecision, "/bt/decision", self._on_decision, 20)
        self.create_subscription(SkillStatus, "/skill/status", self._on_status, 20)

    def _publish_summary(self, channel: str, text: str) -> None:
        if self._last_summary_by_channel.get(channel) == text:
            return
        self._last_summary_by_channel[channel] = text
        msg = String()
        msg.data = text
        self._summary_pub.publish(msg)
        self.get_logger().info(text)

    def _on_world(self, msg: WorldState) -> None:
        self._latest_world = msg
        procedure_changed = msg.procedure_id != self._last_procedure_id
        runtime_started = bool(msg.running and not self._last_running)
        now_sec = time.monotonic()
        periodic_refresh = now_sec - self._last_full_refresh_sec >= 1.0
        if periodic_refresh:
            self._last_full_refresh_sec = now_sec
        if procedure_changed:
            self._last_procedure_id = msg.procedure_id
            self._bundle_generation += 1
        self._last_running = bool(msg.running)
        active_tool_ids = {instrument.instrument_id for instrument in msg.instrument_states}
        force_keys = {"procedure.id", "bundle.generation"}
        mirrored = {
            "procedure.id": msg.procedure_id,
            "bundle.generation": int(self._bundle_generation),
            "phase.id": msg.filtered_phase,
            "phase.confidence": float(msg.phase_confidence),
            "phase.uncertain": bool(msg.phase_uncertain),
            "phase.stability": float(msg.phase_stability),
            "request.explicit_tool": msg.explicit_request_tool,
            "request.surgeon_tool": msg.surgeon_request_tool,
            "surgeon.intent": msg.surgeon_intent,
            "surgeon.ready_handover": bool(msg.surgeon_ready_for_handover),
            "surgeon.ready_retrieval": bool(msg.surgeon_ready_for_retrieval),
            "robot.state": msg.robot_state,
            "robot.right_hand_tool": msg.right_hand_tool,
            "robot.left_hand_tool": msg.left_hand_tool,
            "robot.prepositioned_tool": msg.prepositioned_tool,
            "cleaner.busy": bool(msg.cleaner_busy),
            "action.guard.handover_allowed": bool(msg.handover_allowed),
            "recovery.required": bool(msg.recovery_required),
            "safety.flags.csv": ",".join(msg.safety_flags),
            "expected_tools.csv": ",".join(msg.expected_instruments),
            "available_tools.csv": ",".join(msg.available_instruments),
        }
        for inactive_tool_id in self._seen_tool_ids.difference(active_tool_ids):
            active_key = f"tool.{inactive_tool_id}.active"
            mirrored[active_key] = False
            force_keys.add(active_key)
            mirrored[f"tool.{inactive_tool_id}.available"] = False
        for instrument in msg.instrument_states:
            active_key = f"tool.{instrument.instrument_id}.active"
            mirrored[active_key] = True
            force_keys.add(active_key)
            mirrored[f"tool.{instrument.instrument_id}.home_location"] = instrument.home_location_id
            mirrored[f"tool.{instrument.instrument_id}.home_type"] = instrument.home_location_type
            mirrored[f"tool.{instrument.instrument_id}.status"] = instrument.status
            mirrored[f"tool.{instrument.instrument_id}.location"] = instrument.location_id
            mirrored[f"tool.{instrument.instrument_id}.location_type"] = instrument.location_type
            mirrored[f"tool.{instrument.instrument_id}.owner"] = instrument.owner
            mirrored[f"tool.{instrument.instrument_id}.contaminated"] = bool(instrument.contaminated)
            mirrored[f"tool.{instrument.instrument_id}.cleanliness"] = instrument.cleanliness_state
            mirrored[f"tool.{instrument.instrument_id}.lifecycle"] = instrument.lifecycle_stage
            mirrored[f"tool.{instrument.instrument_id}.next_required_transition"] = instrument.next_required_transition
            mirrored[f"tool.{instrument.instrument_id}.available"] = _is_available_status(instrument.status)
        if procedure_changed or runtime_started or periodic_refresh:
            force_keys.update(mirrored)
        self._seen_tool_ids.update(active_tool_ids)
        self._mirror.queue_many(mirrored, force_keys=force_keys)
        suggestion = select_expected_tool(msg)
        self._publish_summary(
            "world",
            f"world phase={msg.filtered_phase} uncertain={msg.phase_uncertain} "
            f"handover={should_allow_handover(msg)} suggested={suggestion or 'none'} "
            f"hands=({msg.left_hand_tool or 'none'},{msg.right_hand_tool or 'none'}) cleaner={msg.cleaner_busy}"
        )

    def _on_decision(self, msg: BTDecision) -> None:
        self._mirror.queue_many(
            {
                "bt.decision": msg.decision,
                "bt.action": msg.action,
                "bt.rationale": msg.rationale,
                "bt.decision_reason": msg.decision_reason,
                "bt.blocking_guard": msg.blocking_guard,
                "selected.tool": msg.selected_tool,
                "bt.selected_tool_lifecycle": msg.selected_tool_lifecycle,
                "bt.next_required_transition": msg.next_required_transition,
                "action.guard.handover_allowed": bool(msg.handover_allowed),
            }
        )
        self._publish_summary(
            "decision",
            f"bt decision={msg.decision} action={msg.action} tool={msg.selected_tool or 'none'} "
            f"lifecycle={msg.selected_tool_lifecycle or 'none'} "
            f"pending={msg.next_required_transition or 'none'} allowed={msg.handover_allowed}"
        )

    def _on_status(self, msg: SkillStatus) -> None:
        self._publish_summary(
            "skill",
            f"skill action={msg.action} state={msg.state} success={msg.success} "
            f"tool={msg.instrument_id or 'none'}"
        )


def main() -> None:
    rclpy.init()
    node = DecisionBridgeNode()
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
