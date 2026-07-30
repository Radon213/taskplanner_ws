"""Evaluation-only confirmed-event adapter for reconciled and oracle modes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import rclpy
from rclpy.node import Node
from std_msgs.msg import String
from surgical_msgs.msg import ToolObservation, WorldState
import yaml

from .message_conversion import message_payload


NON_STRICT_MODES = {"reconciled", "oracle"}
OPERATIVE_HOLDERS = {"surgeon", "operative_recipient", "assistant"}
TABLE_HOLDERS = {"scrub_nurse", "circulating_nurse", "none"}
HAND_LOCATIONS = {"left_hand", "right_hand", "both_hands", "hand_unspecified"}
FLAT_ENDPOINT_STATES = {
    "mayo_stand": ("none", "mayo_stand"),
    "scrub_nurse": ("scrub_nurse", "hand_unspecified"),
    "surgeon": ("surgeon", "hand_unspecified"),
}


def load_confirmed_events(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: expected a JSON object")
            if value.get("review_status") == "confirmed":
                records.append(value)
    return sorted(
        records,
        key=lambda item: (float(item["time_sec"]), str(item["event_id"])),
    )


def load_runtime_tool_map(path: Path) -> dict[str, str]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    rows = payload.get("tools", []) if isinstance(payload, dict) else []
    result: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        canonical = str(row.get("id", "")).strip()
        refs = [
            str(value).strip()
            for value in (row.get("procedure_refs", []) or [])
            if str(value).strip()
        ]
        if canonical and refs:
            result[canonical] = refs[0]
    return result


def map_reference_location(
    event: dict[str, Any],
    world: dict[str, Any] | None,
    runtime_tool_id: str = "",
) -> tuple[str, str, str]:
    raw_target = event.get("to")
    flat_target = isinstance(raw_target, str)
    if isinstance(raw_target, dict):
        holder = str(raw_target.get("holder", ""))
        location = str(raw_target.get("location", ""))
    elif flat_target:
        holder, location = FLAT_ENDPOINT_STATES.get(
            raw_target.strip(),
            ("", ""),
        )
    else:
        holder, location = ("", "")

    raw_tool = event.get("tool")
    reference_tool_id = (
        str(raw_tool.get("id", ""))
        if isinstance(raw_tool, dict)
        else str(raw_tool or "")
    )
    tool_id = runtime_tool_id or reference_tool_id
    if holder in OPERATIVE_HOLDERS and (
        location in HAND_LOCATIONS or location == "surgical_field"
    ):
        return ("surgeon_hand", "surgeon_hand", "operative_recipient")
    if holder == "none" and location == "mayo_stand":
        return ("mayo_reuse_zone", "mayo_reuse_zone", "mayo")
    if flat_target and holder == "scrub_nurse" and location in HAND_LOCATIONS:
        raw_source = event.get("from")
        source_name = raw_source.strip() if isinstance(raw_source, str) else ""
        if source_name == "surgeon":
            return (
                "robot_left_hand",
                "robot_left_hand",
                "humanoid_recovery_hand",
            )
        if source_name == "mayo_stand":
            return (
                "robot_right_hand",
                "robot_right_hand",
                "humanoid_handover_hand",
            )
        return ("", "", "ambiguous_humanoid_hand")
    if holder in TABLE_HOLDERS and (
        location in HAND_LOCATIONS
        or location in {"instrument_table", "off_screen"}
    ):
        if world is not None:
            for instrument in world.get("instrument_states", []):
                if str(instrument.get("instrument_id", "")) == tool_id:
                    return (
                        str(instrument.get("home_location_type", "")),
                        str(instrument.get("home_location_id", "")),
                        "runtime_home_anchor",
                    )
        return ("", "", "missing_runtime_home_anchor")
    if location == "surgical_field":
        return ("surgical_field", "surgical_field", "operative_field")
    return ("", "", "unsupported_observable_location")


class ReferenceReconcilerNode(Node):
    def __init__(self) -> None:
        super().__init__("reference_reconciler")
        self.declare_parameter("reference_path", "")
        self.declare_parameter("tool_catalog_path", "")
        self.declare_parameter("mode", "reconciled")
        self.declare_parameter("post_event_delay_sec", 0.001)
        reference_path = Path(str(self.get_parameter("reference_path").value))
        self._mode = str(self.get_parameter("mode").value).strip()
        if self._mode not in NON_STRICT_MODES:
            raise ValueError(
                "reference_reconciler is forbidden in strict mode"
            )
        if not reference_path.is_file():
            raise FileNotFoundError(reference_path)
        tool_catalog_path = Path(
            str(self.get_parameter("tool_catalog_path").value)
        )
        if not tool_catalog_path.is_file():
            raise FileNotFoundError(tool_catalog_path)
        self._events = load_confirmed_events(reference_path)
        self._runtime_tool_map = load_runtime_tool_map(tool_catalog_path)
        self._index = 0
        configured_delay = max(
            0.0,
            float(self.get_parameter("post_event_delay_sec").value),
        )
        self._delay_sec = 0.0 if self._mode == "oracle" else configured_delay
        self._world: dict[str, Any] | None = None
        self._observation_pub = self.create_publisher(
            ToolObservation,
            "/shadow/evaluation_observation",
            50,
        )
        self._audit_pub = self.create_publisher(
            String,
            "/shadow/reconciliation_event",
            50,
        )
        self.create_subscription(
            WorldState,
            "/twin/world_state",
            self._on_world,
            20,
        )
        self.create_timer(0.01, self._release_due_events)
        self.get_logger().warning(
            f"{self._mode} evaluation adapter loaded {len(self._events)} "
            "confirmed reference events; these observations are not strict metrics"
        )

    def _on_world(self, msg: WorldState) -> None:
        self._world = message_payload(msg)

    def _now_sec(self) -> float:
        return self.get_clock().now().nanoseconds / 1_000_000_000.0

    def _release_due_events(self) -> None:
        now_sec = self._now_sec()
        while self._index < len(self._events):
            event = self._events[self._index]
            event_time = float(event["time_sec"])
            if now_sec + 1e-9 < event_time + self._delay_sec:
                break
            self._index += 1
            self._publish_event(event, now_sec)

    def _publish_event(
        self,
        event: dict[str, Any],
        published_at_sec: float,
    ) -> None:
        raw_tool = event.get("tool")
        reference_tool_id = (
            str(raw_tool.get("id", ""))
            if isinstance(raw_tool, dict)
            else str(raw_tool or "")
        )
        runtime_tool_id = self._runtime_tool_map.get(
            reference_tool_id,
            reference_tool_id,
        )
        location_type, location_id, mapping = map_reference_location(
            event,
            self._world,
            runtime_tool_id,
        )
        audit = {
            "event_id": str(event.get("event_id", "")),
            "reference_time_sec": float(event.get("time_sec", 0.0)),
            "published_at_sec": round(float(published_at_sec), 9),
            "mode": self._mode,
            "reference_tool_id": reference_tool_id,
            "runtime_tool_id": runtime_tool_id,
            "location_type": location_type,
            "location_id": location_id,
            "mapping": mapping,
            "published": bool(location_type and location_id),
        }
        audit_msg = String()
        audit_msg.data = json.dumps(audit, separators=(",", ":"), sort_keys=True)
        self._audit_pub.publish(audit_msg)
        if not location_type or not location_id:
            return
        observation = ToolObservation()
        observation.stamp = self.get_clock().now().to_msg()
        observation.instrument_id = runtime_tool_id
        observation.location_type = location_type
        observation.location_id = location_id
        observation.confidence = 1.0
        observation.visible = True
        self._observation_pub.publish(observation)


def main() -> None:
    rclpy.init()
    node = ReferenceReconcilerNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
