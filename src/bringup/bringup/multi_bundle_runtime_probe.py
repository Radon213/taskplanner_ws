"""Attach to a running taskplanner runtime and audit all procedure bundles."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import statistics
import sys
import time
from typing import Any

from ament_index_python.packages import get_package_share_directory
from procedure_spec import load_bundle
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import CompressedImage
from std_msgs.msg import String
from surgical_msgs.msg import (
    BTDecision,
    ReducerDecisionEvent,
    SimulationState,
    SkillCommand,
    SkillStatus,
    SurgeonActorEvent,
    SurgeonLLMDecision,
    SurgeonOutwardSignal,
    SurgeonState,
    TwinEvent,
    VLMHealth,
    VLMRequestContext,
    VLMResult,
    WorldState,
)
from surgical_msgs.srv import ControlSimulation, SelectSimulationBundle


DEFAULT_BUNDLES = ("thyroidectomy", "nephrectomy", "inguinal_hernia_repair")
HANDOVER_ACTIONS = {
    "direct_handover",
    "pick_up_and_handover",
    "put_down_and_handover",
    "tool_handover",
    "predicted_tool_handover",
    "replace_and_handover",
}
RECOVERY_ACTIONS = {
    "retrieve_from_mayo",
    "retrieve_from_hand",
    "tool_retrieve",
    "return_unused_preposition",
}
RECOVERY_STARTED_STATES = {
    "dispatching",
    "accepted",
    "executing",
    "retrieving_from_mayo",
    "inserting_into_cleaner",
    "cleaning",
    "returning_to_rack",
    "completed",
}
RECOVERY_FAILURE_STATES = {
    "rejected",
    "dispatch_failed",
    "server_unavailable",
    "skipped_while_busy",
    "cancel_requested",
    "canceled",
    "aborted",
    "result_failed",
}
TOOL_PREDICTION_MIN_LEAD_SEC = 3.0
TOOL_PREDICTION_MIN_CONFIDENCE = 0.8
PHASE_TRANSITION_CONTEXT_GRACE_SEC = 10.0


def _stamp_to_sec(stamp) -> float:
    return float(stamp.sec) + float(stamp.nanosec) / 1_000_000_000.0


def _stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0}
    ordered = sorted(values)
    return {
        "count": len(values),
        "avg": round(statistics.mean(values), 3),
        "p95": round(ordered[max(0, int(len(ordered) * 0.95) - 1)], 3),
        "max": round(max(values), 3),
    }


def _parse_json(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _alignment_scoreboard(samples: list[dict[str, Any]]) -> dict[str, Any]:
    evaluable = len(samples)
    vlm_proposed = sum(1 for item in samples if item.get("vlm"))
    system_proposed = sum(1 for item in samples if item.get("system"))
    vlm_correct = sum(1 for item in samples if item.get("vlm_match"))
    system_correct = sum(1 for item in samples if item.get("system_match"))
    return {
        "format": "correct / proposed / evaluable",
        "vlm": {
            "correct": vlm_correct,
            "proposed": vlm_proposed,
            "evaluable": evaluable,
            "display": f"{vlm_correct} / {vlm_proposed} / {evaluable}",
        },
        "system": {
            "correct": system_correct,
            "proposed": system_proposed,
            "evaluable": evaluable,
            "display": f"{system_correct} / {system_proposed} / {evaluable}",
        },
    }


class MultiBundleRuntimeProbe(Node):
    def __init__(self) -> None:
        super().__init__("multi_bundle_runtime_probe")
        self.world: WorldState | None = None
        self.simulation: SimulationState | None = None
        self._select_bundle_client = self.create_client(SelectSimulationBundle, "/simulation/select_bundle")
        self._control_client = self.create_client(ControlSimulation, "/simulation/control")
        self._spec_root = Path(get_package_share_directory("procedure_spec")) / "specs"
        self._spec_cache: dict[str, Any] = {}

        self.create_subscription(WorldState, "/twin/world_state", self._on_world, 50)
        self.create_subscription(SimulationState, "/simulation/state", self._on_simulation, 50)
        self.create_subscription(SurgeonState, "/surgeon/state", self._on_surgeon_state, 50)
        self.create_subscription(SurgeonLLMDecision, "/surgeon/llm_decision", self._on_actor_decision, 50)
        self.create_subscription(SurgeonActorEvent, "/surgeon/actor_event", self._on_actor_event, 50)
        self.create_subscription(SurgeonOutwardSignal, "/surgeon/outward_signal", self._on_outward_signal, 50)
        self.create_subscription(String, "/surgeon/actor_overlay", self._on_overlay, 50)
        self.create_subscription(VLMResult, "/vlm/result", self._on_vlm_result, 50)
        self.create_subscription(VLMHealth, "/vlm/health", self._on_vlm_health, 50)
        self.create_subscription(VLMRequestContext, "/context/vlm_request_context", self._on_vlm_context, 20)
        self.create_subscription(BTDecision, "/bt/decision", self._on_bt_decision, 50)
        self.create_subscription(SkillCommand, "/bt/skill_command", self._on_skill_command, 50)
        self.create_subscription(SkillStatus, "/skill/status", self._on_skill_status, 50)
        self.create_subscription(TwinEvent, "/skill/events", self._on_skill_event, 50)
        self.create_subscription(TwinEvent, "/twin/events", self._on_twin_event, 50)
        self.create_subscription(ReducerDecisionEvent, "/twin/reducer_decisions", self._on_reducer_event, 50)
        self.create_subscription(CompressedImage, "/surgery/images/field/compressed", self._on_image, 20)

        self._reset_window()

    def _reset_window(self) -> None:
        self.world_states: list[WorldState] = []
        self.simulation_states: list[SimulationState] = []
        self.surgeon_states: list[SurgeonState] = []
        self.actor_decisions: list[SurgeonLLMDecision] = []
        self.actor_events: list[SurgeonActorEvent] = []
        self.outward_signals: list[SurgeonOutwardSignal] = []
        self.overlay_samples: list[dict[str, Any]] = []
        self.vlm_results: list[VLMResult] = []
        self.vlm_health: list[VLMHealth] = []
        self.vlm_contexts: list[VLMRequestContext] = []
        self.bt_decisions: list[BTDecision] = []
        self.skill_commands: list[SkillCommand] = []
        self.skill_statuses: list[SkillStatus] = []
        self.skill_events: list[TwinEvent] = []
        self.twin_events: list[TwinEvent] = []
        self.reducer_events: list[ReducerDecisionEvent] = []
        self.world_invariant_violations: list[str] = []
        self.image_frames = 0
        self._window_started_sec = self._now()

    def _now(self) -> float:
        stamp = self.get_clock().now().to_msg()
        return _stamp_to_sec(stamp)

    def _on_world(self, msg: WorldState) -> None:
        self.world = msg
        self.world_states.append(msg)
        self._record_world_invariants(msg)

    def _on_simulation(self, msg: SimulationState) -> None:
        self.simulation = msg
        self.simulation_states.append(msg)

    def _on_surgeon_state(self, msg: SurgeonState) -> None:
        self.surgeon_states.append(msg)

    def _on_actor_decision(self, msg: SurgeonLLMDecision) -> None:
        self.actor_decisions.append(msg)

    def _on_actor_event(self, msg: SurgeonActorEvent) -> None:
        self.actor_events.append(msg)

    def _on_outward_signal(self, msg: SurgeonOutwardSignal) -> None:
        self.outward_signals.append(msg)

    def _on_overlay(self, msg: String) -> None:
        payload = _parse_json(msg.data)
        if payload:
            self.overlay_samples.append(payload)

    def _on_vlm_result(self, msg: VLMResult) -> None:
        self.vlm_results.append(msg)

    def _on_vlm_health(self, msg: VLMHealth) -> None:
        self.vlm_health.append(msg)

    def _on_vlm_context(self, msg: VLMRequestContext) -> None:
        self.vlm_contexts.append(msg)

    def _on_bt_decision(self, msg: BTDecision) -> None:
        self.bt_decisions.append(msg)

    def _on_skill_command(self, msg: SkillCommand) -> None:
        self.skill_commands.append(msg)

    def _on_skill_status(self, msg: SkillStatus) -> None:
        self.skill_statuses.append(msg)

    def _on_skill_event(self, msg: TwinEvent) -> None:
        self.skill_events.append(msg)

    def _on_twin_event(self, msg: TwinEvent) -> None:
        self.twin_events.append(msg)

    def _on_reducer_event(self, msg: ReducerDecisionEvent) -> None:
        self.reducer_events.append(msg)

    def _on_image(self, _msg: CompressedImage) -> None:
        self.image_frames += 1

    def _record_world_invariants(self, msg: WorldState) -> None:
        holder_by_tool: dict[str, set[str]] = {}
        surgeon_owned: list[str] = []
        for instrument in msg.instrument_states:
            holders = holder_by_tool.setdefault(instrument.instrument_id, set())
            owner = str(instrument.owner)
            location_type = str(instrument.location_type)
            if owner == "surgeon" or location_type == "surgeon_hand":
                holders.add("surgeon")
            if owner == "robot_right_hand" or location_type == "robot_right_hand":
                holders.add("robot_right_hand")
            if owner == "robot_left_hand" or location_type == "robot_left_hand":
                holders.add("robot_left_hand")
            if location_type == "cleaner_slot":
                holders.add("cleaner")
            if instrument.lifecycle_stage == "surgeon_owned":
                surgeon_owned.append(instrument.instrument_id)
        if len(surgeon_owned) > 2:
            self.world_invariant_violations.append(f"surgeon owned >2 tools: {sorted(surgeon_owned)}")
        for tool_id, holders in holder_by_tool.items():
            if len(holders) > 1:
                self.world_invariant_violations.append(f"{tool_id} appears in multiple holders: {sorted(holders)}")

    def wait_for_services(self, timeout_sec: float = 30.0) -> None:
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            if self._select_bundle_client.wait_for_service(timeout_sec=0.2) and self._control_client.wait_for_service(timeout_sec=0.2):
                return
        raise RuntimeError("simulation services were not ready")

    def select_bundle(self, bundle_name: str) -> float:
        request = SelectSimulationBundle.Request()
        request.bundle_name = bundle_name
        request.restart_if_running = False
        start = time.perf_counter()
        future = self._select_bundle_client.call_async(request)
        rclpy.spin_until_future_complete(self, future, timeout_sec=35.0)
        elapsed = time.perf_counter() - start
        response = future.result()
        if response is None or not response.success:
            raise RuntimeError(f"select bundle failed: {response.message if response else 'no response'}")
        return elapsed

    def control(self, command: str, *, start_phase_id: str = "", timeout_sec: float = 35.0, allow_failure: bool = False) -> tuple[float, str]:
        request = ControlSimulation.Request()
        request.command = command
        request.start_phase_id = start_phase_id
        start = time.perf_counter()
        future = self._control_client.call_async(request)
        rclpy.spin_until_future_complete(self, future, timeout_sec=timeout_sec)
        elapsed = time.perf_counter() - start
        response = future.result()
        if response is None:
            if allow_failure:
                return elapsed, "no response"
            raise RuntimeError(f"control {command} failed: no response")
        if not response.success and not allow_failure:
            raise RuntimeError(f"control {command} failed: {response.message}")
        return elapsed, str(response.message)

    def wait_until(self, predicate, timeout_sec: float, description: str) -> None:
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            rclpy.spin_once(self, timeout_sec=0.2)
            if predicate():
                return
        raise RuntimeError(f"timed out waiting for {description}")

    def spin_for(self, duration_sec: float) -> None:
        deadline = time.time() + duration_sec
        while time.time() < deadline:
            rclpy.spin_once(self, timeout_sec=0.2)

    def _phase_at(self, rows, stamp_sec: float, attr: str) -> str:
        latest = ""
        for row in rows:
            row_stamp = _stamp_to_sec(row.stamp)
            if row_stamp <= stamp_sec:
                latest = str(getattr(row, attr, "") or latest)
            elif row_stamp > stamp_sec:
                break
        return latest

    def _world_at(self, stamp_sec: float) -> WorldState | None:
        latest = None
        for world in self.world_states:
            world_sec = _stamp_to_sec(world.stamp)
            if world_sec <= stamp_sec:
                latest = world
            elif world_sec > stamp_sec:
                break
        return latest

    def _next_request_after(self, stamp_sec: float, horizon_sec: float = 20.0) -> str:
        tool, _stamp = self._next_request_after_with_time(stamp_sec, horizon_sec=horizon_sec)
        return tool

    def _next_request_after_with_time(self, stamp_sec: float, horizon_sec: float = 20.0) -> tuple[str, float]:
        for decision in self.actor_decisions:
            if not decision.accepted or decision.action != "request_tool":
                continue
            decision_sec = _stamp_to_sec(decision.stamp)
            if decision_sec < stamp_sec:
                continue
            if decision_sec - stamp_sec > horizon_sec:
                return "", 0.0
            return decision.tool, decision_sec
        return "", 0.0

    def _field_event_between(self, start_sec: float, end_sec: float) -> bool:
        for decision in self.actor_decisions:
            if not decision.accepted or decision.action != "field_event":
                continue
            decision_sec = _stamp_to_sec(decision.stamp)
            if start_sec < decision_sec <= end_sec:
                return True
            if decision_sec > end_sec:
                break
        return False

    def _phase_advance_between(self, start_sec: float, end_sec: float) -> bool:
        for decision in self.actor_decisions:
            if not decision.accepted or decision.action != "advance_phase":
                continue
            decision_sec = _stamp_to_sec(decision.stamp)
            if start_sec < decision_sec <= end_sec:
                return True
            if decision_sec > end_sec:
                break
        return False

    def _recent_field_event_before(self, stamp_sec: float, grace_sec: float = 4.0) -> bool:
        for decision in reversed(self.actor_decisions):
            if not decision.accepted or decision.action != "field_event":
                continue
            decision_sec = _stamp_to_sec(decision.stamp)
            if decision_sec > stamp_sec:
                continue
            return stamp_sec - decision_sec < grace_sec
        return False

    def _recent_phase_advance_before(self, stamp_sec: float, grace_sec: float = 5.0) -> bool:
        for decision in reversed(self.actor_decisions):
            if not decision.accepted or decision.action != "advance_phase":
                continue
            decision_sec = _stamp_to_sec(decision.stamp)
            if decision_sec > stamp_sec:
                continue
            return stamp_sec - decision_sec < grace_sec
        return False

    def _previous_request_before(self, stamp_sec: float) -> str:
        latest = ""
        for decision in self.actor_decisions:
            if not decision.accepted or decision.action != "request_tool":
                continue
            decision_sec = _stamp_to_sec(decision.stamp)
            if decision_sec <= stamp_sec:
                latest = decision.tool
            else:
                break
        return latest

    def _previous_request_before_with_time(self, stamp_sec: float) -> tuple[str, float]:
        latest = ""
        latest_sec = 0.0
        for decision in self.actor_decisions:
            if not decision.accepted or decision.action != "request_tool":
                continue
            decision_sec = _stamp_to_sec(decision.stamp)
            if decision_sec <= stamp_sec:
                latest = decision.tool
                latest_sec = decision_sec
            else:
                break
        return latest, latest_sec

    def _system_prediction_after(
        self,
        stamp_sec: float,
        horizon_sec: float = 20.0,
        ignore_tool: str = "",
        until_sec: float | None = None,
    ) -> str:
        best_tool = ""
        best_confidence = 0.0
        for world in self.world_states:
            world_sec = _stamp_to_sec(world.stamp)
            if world_sec < stamp_sec:
                continue
            if until_sec is not None and world_sec >= until_sec:
                break
            if world_sec - stamp_sec > horizon_sec:
                break
            if (
                world.predicted_tool
                and world.predicted_tool != ignore_tool
                and float(world.predicted_tool_confidence) >= best_confidence
            ):
                best_tool = world.predicted_tool
                best_confidence = float(world.predicted_tool_confidence)
        if best_tool and best_confidence >= TOOL_PREDICTION_MIN_CONFIDENCE:
            return best_tool
        return ""

    def _context_at(self, stamp_sec: float) -> dict[str, Any]:
        latest = {}
        for context in self.vlm_contexts:
            context_sec = _stamp_to_sec(context.stamp)
            if context_sec <= stamp_sec:
                latest = _parse_json(context.compact_json)
            elif context_sec > stamp_sec:
                break
        return latest if isinstance(latest, dict) else {}

    def _context_phase_at(self, stamp_sec: float) -> str:
        evidence = self._context_debug_at(stamp_sec).get("candidate_evidence", {})
        phase_id = evidence.get("current_phase", "") if isinstance(evidence, dict) else ""
        return str(phase_id or "")

    def _context_phase_stale_at(self, stamp_sec: float) -> bool:
        context_phase = self._context_phase_at(stamp_sec)
        if not context_phase:
            return False
        world_phase = self._phase_at(self.world_states, stamp_sec, "filtered_phase")
        return bool(world_phase and context_phase != world_phase)

    def _context_candidates_at(self, stamp_sec: float, key: str) -> list:
        latest = self._context_at(stamp_sec)
        candidates = latest.get("candidates", {}) if isinstance(latest, dict) else {}
        rows = candidates.get(key, []) if isinstance(candidates, dict) else []
        return rows if isinstance(rows, list) else []

    def _context_debug_at(self, stamp_sec: float) -> dict[str, Any]:
        latest = self._context_at(stamp_sec)
        candidates = latest.get("candidates", {}) if isinstance(latest, dict) else {}
        digital_twin = latest.get("digital_twin", {}) if isinstance(latest, dict) else {}
        evidence_window = latest.get("evidence_window", {}) if isinstance(latest, dict) else {}
        return {
            "candidate_evidence": candidates.get("evidence", {}) if isinstance(candidates, dict) else {},
            "evidence_window": {
                "speech": evidence_window.get("speech", [])[-4:] if isinstance(evidence_window.get("speech", []), list) else [],
                "observed_signals": evidence_window.get("observed_signals", [])[-4:]
                if isinstance(evidence_window.get("observed_signals", []), list)
                else [],
                "skill_status": evidence_window.get("skill_status", [])[-4:]
                if isinstance(evidence_window.get("skill_status", []), list)
                else [],
                "visual": evidence_window.get("visual", {}) if isinstance(evidence_window, dict) else {},
            },
            "digital_twin": {
                "hands": digital_twin.get("hands", {}) if isinstance(digital_twin, dict) else {},
                "events": digital_twin.get("events", [])[-6:]
                if isinstance(digital_twin, dict) and isinstance(digital_twin.get("events", []), list)
                else [],
            },
        }

    def _vlm_tool_prediction(self, result: VLMResult) -> str:
        payload = _parse_json(result.raw_json)
        raw_tool = payload.get("tool", [])
        tool_id = ""
        if isinstance(raw_tool, list) and raw_tool and isinstance(raw_tool[0], list):
            tool_id = str(raw_tool[0][0])
        elif isinstance(raw_tool, list) and len(raw_tool) >= 2:
            tool_id = str(raw_tool[0])
        return self._resolve_tool_id(tool_id)

    def _phase_alignment(self) -> dict[str, Any]:
        samples: list[dict[str, Any]] = []
        transition_grace_skipped = 0
        stale_context_phase_skipped = 0
        for result in self.vlm_results:
            stamp_sec = _stamp_to_sec(result.stamp)
            if self._recent_phase_advance_before(stamp_sec):
                transition_grace_skipped += 1
                continue
            if self._context_phase_stale_at(stamp_sec) and self._recent_phase_advance_before(
                stamp_sec, grace_sec=PHASE_TRANSITION_CONTEXT_GRACE_SEC
            ):
                stale_context_phase_skipped += 1
                continue
            ground = self._phase_at(self.surgeon_states, stamp_sec, "phase_id")
            system = self._phase_at(self.world_states, stamp_sec, "filtered_phase")
            vlm_phase = result.phase_ids[0] if result.phase_ids else ""
            if not ground:
                continue
            vlm_candidates = [
                [phase_id, round(float(confidence), 3)]
                for phase_id, confidence in zip(result.phase_ids, result.phase_confidences)
            ][:4]
            samples.append(
                {
                    "t": round(stamp_sec - self._window_started_sec, 2),
                    "ground": ground,
                    "vlm": vlm_phase,
                    "system": system,
                    "candidate_phase": self._context_candidates_at(stamp_sec, "phase")[:4],
                    "vlm_candidates": vlm_candidates,
                    "context_debug": self._context_debug_at(stamp_sec),
                    "vlm_match": bool(vlm_phase and vlm_phase == ground),
                    "system_match": bool(system and system == ground),
                }
            )
        return {
            "samples": len(samples),
            "transition_grace_skipped": transition_grace_skipped,
            "stale_context_phase_skipped": stale_context_phase_skipped,
            "scoreboard": _alignment_scoreboard(samples),
            "recent_mismatches": [item for item in samples if not item["vlm_match"] or not item["system_match"]][-8:],
        }

    def _tool_alignment(self) -> dict[str, Any]:
        samples: list[dict[str, Any]] = []
        phase_advance_skipped = 0
        transition_grace_skipped = 0
        duplicate_request_skipped = 0
        mayo_visible_ground_skipped = 0
        field_event_skipped = 0
        visible_field_event_skipped = 0
        recent_field_event_skipped = 0
        insufficient_system_lead_skipped = 0
        stale_context_phase_skipped = 0
        for result in self.vlm_results:
            stamp_sec = _stamp_to_sec(result.stamp)
            if self._recent_phase_advance_before(stamp_sec):
                transition_grace_skipped += 1
                continue
            previous_request, previous_request_sec = self._previous_request_before_with_time(stamp_sec)
            if not previous_request:
                continue
            if stamp_sec - previous_request_sec < 2.0:
                continue
            ground, ground_sec = self._next_request_after_with_time(stamp_sec)
            if not ground:
                continue
            lead_sec = ground_sec - stamp_sec
            if ground == previous_request:
                duplicate_request_skipped += 1
                continue
            context_debug = self._context_debug_at(stamp_sec)
            visual = context_debug.get("evidence_window", {}).get("visual", {})
            visible_field_event = bool(
                isinstance(visual, dict)
                and isinstance(visual.get("field_event", []), list)
                and visual.get("field_event", [])
            )
            if visible_field_event:
                visible_field_event_skipped += 1
                continue
            if self._context_phase_stale_at(stamp_sec) and self._recent_phase_advance_before(
                stamp_sec, grace_sec=PHASE_TRANSITION_CONTEXT_GRACE_SEC
            ):
                stale_context_phase_skipped += 1
                continue
            visible_mayo = set(visual.get("mayo", []) if isinstance(visual, dict) and isinstance(visual.get("mayo", []), list) else [])
            if ground in visible_mayo:
                mayo_visible_ground_skipped += 1
                continue
            if self._phase_advance_between(stamp_sec, ground_sec):
                phase_advance_skipped += 1
                continue
            if self._field_event_between(stamp_sec, ground_sec):
                field_event_skipped += 1
                continue
            if self._recent_field_event_before(stamp_sec):
                recent_field_event_skipped += 1
                continue
            if lead_sec < TOOL_PREDICTION_MIN_LEAD_SEC:
                insufficient_system_lead_skipped += 1
                continue
            vlm_tool = self._vlm_tool_prediction(result)
            system_tool = self._system_prediction_after(
                stamp_sec,
                ignore_tool=previous_request,
                until_sec=ground_sec,
            )
            samples.append(
                {
                    "t": round(stamp_sec - self._window_started_sec, 2),
                    "lead_sec": round(lead_sec, 2),
                    "previous_request": previous_request,
                    "ground": ground,
                    "vlm": vlm_tool,
                    "system": system_tool,
                    "candidate_tool": self._context_candidates_at(stamp_sec, "tool")[:4],
                    "context_debug": context_debug,
                    "vlm_match": bool(vlm_tool and vlm_tool == ground),
                    "system_match": bool(system_tool and system_tool == ground),
                }
            )
        confusion = Counter(f"{item['ground']}->{item['vlm'] or 'none'}" for item in samples)
        return {
            "samples": len(samples),
            "phase_advance_skipped": phase_advance_skipped,
            "transition_grace_skipped": transition_grace_skipped,
            "duplicate_request_skipped": duplicate_request_skipped,
            "mayo_visible_ground_skipped": mayo_visible_ground_skipped,
            "field_event_skipped": field_event_skipped,
            "visible_field_event_skipped": visible_field_event_skipped,
            "recent_field_event_skipped": recent_field_event_skipped,
            "insufficient_system_lead_skipped": insufficient_system_lead_skipped,
            "stale_context_phase_skipped": stale_context_phase_skipped,
            "scoreboard": _alignment_scoreboard(samples),
            "confusion_top": dict(confusion.most_common(10)),
            "recent_mismatches": [item for item in samples if not item["vlm_match"] or not item["system_match"]][-8:],
        }

    def _context_leaks(self) -> list[str]:
        leaks: list[str] = []
        forbidden_fragments = (
            "hidden_phase",
            "held_tool",
            "held_tools",
            "phase_tool_coverage",
            "phase_elapsed_sec",
            "episode_style",
            "random_hint",
            "recent_events\":[{\"t\":\"phase_entered\"",
        )
        for context in self.vlm_contexts[-12:]:
            compact = context.compact_json
            for fragment in forbidden_fragments:
                if fragment in compact:
                    leaks.append(fragment)
        return sorted(set(leaks))

    def _overlay_leaks(self) -> int:
        return sum(
            1
            for item in self.overlay_samples
            if any(str(value).lower() in {"recover", "reuse"} for value in item.get("mayo", []))
        )

    def _mayo_visible_tools(self) -> set[str]:
        if not self.overlay_samples:
            return set()
        mayo = self.overlay_samples[-1].get("mayo", [])
        if not isinstance(mayo, list):
            return set()
        return {str(tool) for tool in mayo if str(tool)}

    def _mayo_recent_visible_tools(self) -> set[str]:
        tools: set[str] = set()
        for item in self.overlay_samples[-10:]:
            mayo = item.get("mayo", [])
            if isinstance(mayo, list):
                tools.update(str(tool) for tool in mayo if str(tool))
        return tools

    def _final_world_mayo_tools(self) -> set[str]:
        if self.world is None:
            return set()
        return {
            instrument.instrument_id
            for instrument in self.world.instrument_states
            if instrument.location_type in {"mayo_reuse_zone", "mayo_recovery_zone", "mayo_stand"}
        }

    def _recovery_hidden_mayo_tools(self) -> set[str]:
        last_mayo_place_sec: dict[str, float] = {}
        for event in self.actor_events:
            if str(event.event_type) not in {"place_on_mayo", "place_on_mayo_reuse", "place_on_mayo_recovery"}:
                continue
            tool_id = str(event.tool_id)
            if tool_id:
                last_mayo_place_sec[tool_id] = max(last_mayo_place_sec.get(tool_id, 0.0), _stamp_to_sec(event.stamp))

        latest_recovery: dict[str, tuple[float, bool]] = {}
        for status in self.skill_statuses:
            action = str(status.action).strip()
            tool_id = str(status.instrument_id).strip()
            state = str(status.state).strip()
            if not tool_id or action not in RECOVERY_ACTIONS:
                continue
            stamp_sec = _stamp_to_sec(status.stamp)
            if stamp_sec < last_mayo_place_sec.get(tool_id, 0.0):
                continue
            if state in RECOVERY_FAILURE_STATES:
                latest_recovery[tool_id] = (stamp_sec, False)
                continue
            if state in RECOVERY_STARTED_STATES and (state != "completed" or bool(status.success)):
                latest_recovery[tool_id] = (stamp_sec, True)
        return {tool_id for tool_id, (_, hidden) in latest_recovery.items() if hidden}

    def _bundle_spec(self, bundle: str):
        if bundle not in self._spec_cache:
            self._spec_cache[bundle] = load_bundle(self._spec_root / bundle)
        return self._spec_cache[bundle]

    def _resolve_tool_id(self, tool_id: str, bundle: str = "") -> str:
        raw_tool = str(tool_id or "")
        if not raw_tool:
            return ""
        bundle_name = bundle
        if not bundle_name and self.world is not None:
            bundle_name = str(self.world.procedure_id)
        if not bundle_name and self.simulation is not None:
            bundle_name = str(self.simulation.active_bundle)
        if not bundle_name:
            return raw_tool
        try:
            spec = self._bundle_spec(bundle_name)
        except Exception:
            return raw_tool
        return spec.resolve_instrument_alias(raw_tool) or raw_tool

    def _resolve_phase_id(self, phase_id: str, bundle: str = "") -> str:
        raw_phase = str(phase_id or "")
        if not raw_phase:
            return ""
        bundle_name = bundle
        if not bundle_name and self.world is not None:
            bundle_name = str(self.world.procedure_id)
        if not bundle_name and self.simulation is not None:
            bundle_name = str(self.simulation.active_bundle)
        if not bundle_name:
            return raw_phase
        try:
            spec = self._bundle_spec(bundle_name)
        except Exception:
            return raw_phase
        return spec.resolve_phase_id(raw_phase) or raw_phase

    def _scope_report(self, bundle: str) -> dict[str, Any]:
        spec = self._bundle_spec(bundle)
        phase_ids = set(spec.phase_ids)
        tool_ids = set(spec.list_instrument_ids())

        actor_phase_ids = {
            self._resolve_phase_id(str(msg.phase_id), bundle)
            for msg in self.surgeon_states
            if str(msg.phase_id)
        }
        actor_hidden_phase_ids = {
            self._resolve_phase_id(str(msg.hidden_phase), bundle)
            for msg in self.actor_decisions
            if str(msg.hidden_phase)
        }
        actor_event_phase_ids = {
            self._resolve_phase_id(str(msg.phase_id), bundle)
            for msg in self.actor_events
            if str(msg.phase_id)
        }
        vlm_phase_ids = {
            self._resolve_phase_id(str(phase_id), bundle)
            for msg in self.vlm_results
            for phase_id in msg.phase_ids
            if str(phase_id)
        }

        actor_tools = {
            self._resolve_tool_id(str(tool_id), bundle)
            for tool_id in [
                *(msg.requested_tool for msg in self.surgeon_states),
                *(msg.tool for msg in self.actor_decisions),
                *(msg.tool_id for msg in self.actor_events),
            ]
            if str(tool_id)
        }
        vlm_tools = {
            self._resolve_tool_id(str(tool_id), bundle)
            for msg in self.vlm_results
            for tool_id in [
                *list(msg.observed_tool_ids),
                msg.gesture_requested_tool,
                self._vlm_tool_prediction(msg),
            ]
            if str(tool_id)
        }
        world_tools = {
            str(instrument.instrument_id)
            for world in self.world_states[-10:]
            for instrument in world.instrument_states
            if str(instrument.instrument_id)
        }
        simulation_tools = {
            str(instrument.instrument_id)
            for simulation in self.simulation_states[-10:]
            for instrument in simulation.instrument_states
            if str(instrument.instrument_id)
        }
        return {
            "phase_ids": sorted(phase_ids),
            "tool_ids": sorted(tool_ids),
            "actor_out_of_scope_phase_ids": sorted(
                (actor_phase_ids | actor_hidden_phase_ids | actor_event_phase_ids).difference(phase_ids)
            ),
            "vlm_out_of_scope_phase_ids": sorted(vlm_phase_ids.difference(phase_ids)),
            "actor_out_of_scope_tools": sorted(actor_tools.difference(tool_ids)),
            "vlm_out_of_scope_tools": sorted(vlm_tools.difference(tool_ids)),
            "world_out_of_scope_tools": sorted(world_tools.difference(tool_ids)),
            "simulation_out_of_scope_tools": sorted(simulation_tools.difference(tool_ids)),
        }

    def report_bundle(self, bundle: str, duration_sec: float, select_latency_sec: float, start_latency_sec: float) -> dict[str, Any]:
        accepted_actor = [msg for msg in self.actor_decisions if msg.accepted]
        rejected_actor = [msg for msg in self.actor_decisions if not msg.accepted]
        actor_latencies = [float(msg.latency_sec) for msg in accepted_actor if float(msg.latency_sec) > 0.0]
        health_latencies = [float(msg.latency_sec) for msg in self.vlm_health if float(msg.latency_sec) > 0.0]
        healthy = [msg for msg in self.vlm_health if bool(msg.connected and msg.healthy) and not msg.last_error]
        schemas = Counter(str(msg.schema_version) for msg in self.vlm_results)
        vlm_modes = Counter(msg.last_mode for msg in self.vlm_health)
        vlm_errors = Counter(msg.last_error for msg in self.vlm_health if msg.last_error)
        action_counts = Counter(msg.action for msg in self.skill_commands)
        actor_action_counts = Counter(msg.action for msg in accepted_actor)
        actor_reject_counts = Counter(msg.reject_reason for msg in rejected_actor)
        actor_modes = Counter(msg.request_mode for msg in accepted_actor)
        outward_types = Counter(msg.signal_type for msg in self.outward_signals)
        field_events = Counter(event.phase_id for event in self.actor_events if event.event_type == "field_event")
        reducer_reasons = Counter(msg.reason for msg in self.reducer_events)
        final_mayo = self._final_world_mayo_tools()
        visible_mayo = self._mayo_visible_tools()
        recovery_hidden_mayo = self._recovery_hidden_mayo_tools()
        final_mayo_expected_visible = final_mayo.difference(recovery_hidden_mayo)
        return {
            "bundle": bundle,
            "duration_sec": duration_sec,
            "select_latency_sec": round(select_latency_sec, 3),
            "start_latency_sec": round(start_latency_sec, 3),
            "world_procedure": self.world.procedure_id if self.world else "",
            "simulation_bundle": self.simulation.active_bundle if self.simulation else "",
            "final_execution_state": self.world.execution_state if self.world else "",
            "final_phase": self.world.filtered_phase if self.world else "",
            "image_frames": self.image_frames,
            "image_hz_est": round(self.image_frames / max(duration_sec, 1.0), 2),
            "actor": {
                "accepted": len(accepted_actor),
                "rejected": len(rejected_actor),
                "actions": dict(actor_action_counts),
                "request_modes": dict(actor_modes),
                "reject_reasons": dict(actor_reject_counts),
                "latency_sec": _stats(actor_latencies),
                "phase_ground_last": self.surgeon_states[-1].phase_id if self.surgeon_states else "",
                "interrupt_phase_cues": dict(field_events),
                "recent_accepted_decisions": [
                    {
                        "t": round(_stamp_to_sec(msg.stamp) - self._window_started_sec, 2),
                        "action": msg.action,
                        "tool": msg.tool,
                        "mode": msg.request_mode,
                        "hidden_phase": msg.hidden_phase,
                        "reason": msg.reject_reason or "",
                        "speech": msg.speech[:100],
                    }
                    for msg in accepted_actor[-12:]
                ],
                "recent_rejected_decisions": [
                    {
                        "t": round(_stamp_to_sec(msg.stamp) - self._window_started_sec, 2),
                        "action": msg.action,
                        "tool": msg.tool,
                        "hidden_phase": msg.hidden_phase,
                        "reason": msg.reject_reason,
                    }
                    for msg in rejected_actor[-8:]
                ],
            },
            "vlm": {
                "results": len(self.vlm_results),
                "schema_versions": dict(schemas),
                "health_samples": len(self.vlm_health),
                "healthy_samples": len(healthy),
                "modes": dict(vlm_modes),
                "errors": dict(vlm_errors.most_common(5)),
                "latency_sec": _stats(health_latencies),
                "prompt_chars": {
                    "min": min([int(msg.prompt_chars) for msg in self.vlm_health if int(msg.prompt_chars) > 0], default=0),
                    "max": max([int(msg.prompt_chars) for msg in self.vlm_health if int(msg.prompt_chars) > 0], default=0),
                },
                "phase_alignment": self._phase_alignment(),
                "tool_alignment": self._tool_alignment(),
                "context_forbidden_fragments": self._context_leaks(),
            },
            "bt": {
                "decisions": dict(Counter(msg.decision for msg in self.bt_decisions)),
                "blocking_guards": dict(Counter(msg.blocking_guard for msg in self.bt_decisions if msg.blocking_guard)),
                "skill_actions": dict(action_counts),
                "handover_actions": sum(count for action, count in action_counts.items() if action in HANDOVER_ACTIONS),
                "recovery_actions": {action: count for action, count in action_counts.items() if action in RECOVERY_ACTIONS},
                "reducer_reasons": dict(reducer_reasons),
            },
            "events": {
                "skill": dict(Counter(msg.event_type for msg in self.skill_events)),
                "twin": dict(Counter(msg.event_type for msg in self.twin_events)),
                "outward": dict(outward_types),
            },
            "overlay": {
                "samples": len(self.overlay_samples),
                "with_hand": sum(1 for item in self.overlay_samples if item.get("hand")),
                "with_mayo": sum(1 for item in self.overlay_samples if item.get("mayo")),
                "leak_count": self._overlay_leaks(),
                "final_visible_mayo_tools": sorted(visible_mayo),
                "recent_visible_mayo_tools": sorted(self._mayo_recent_visible_tools()),
                "final_world_mayo_tools": sorted(final_mayo),
                "recovery_hidden_world_mayo_tools": sorted(final_mayo.intersection(recovery_hidden_mayo)),
                "visible_minus_world_mayo": sorted(visible_mayo.difference(final_mayo)),
                "world_minus_visible_mayo": sorted(final_mayo_expected_visible.difference(visible_mayo)),
            },
            "world_invariant_violations": sorted(set(self.world_invariant_violations))[:20],
            "spec_scope": self._scope_report(bundle),
        }

    def run_bundle(self, bundle: str, duration_sec: float, start_phase_id: str = "") -> dict[str, Any]:
        self.control("stop", allow_failure=True, timeout_sec=20.0)
        select_latency = self.select_bundle(bundle)
        self.wait_until(
            lambda: self.simulation is not None and self.simulation.active_bundle == bundle and not self.simulation.running,
            20.0,
            f"{bundle} idle frame after bundle select",
        )
        self._reset_window()
        start_latency, _message = self.control("start", start_phase_id=start_phase_id, timeout_sec=35.0)
        self.wait_until(
            lambda: self.world is not None
            and self.world.procedure_id == bundle
            and self.world.running
            and self.world.execution_state == "running",
            35.0,
            f"{bundle} running world",
        )
        self.spin_for(duration_sec)
        report = self.report_bundle(bundle, duration_sec, select_latency, start_latency)
        self.control("stop", allow_failure=True, timeout_sec=25.0)
        return report


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundles", nargs="+", default=list(DEFAULT_BUNDLES))
    parser.add_argument("--duration-sec", type=float, default=45.0)
    parser.add_argument("--report-path", default="reports/multi_bundle_runtime_probe_latest.json")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    rclpy.init()
    probe = MultiBundleRuntimeProbe()
    reports: list[dict[str, Any]] = []
    failures: list[str] = []
    try:
        probe.wait_for_services()
        for bundle in args.bundles:
            try:
                report = probe.run_bundle(str(bundle), float(args.duration_sec))
                reports.append(report)
                scope = report.get("spec_scope", {})
                scope_errors = [
                    key
                    for key in (
                        "actor_out_of_scope_phase_ids",
                        "vlm_out_of_scope_phase_ids",
                        "actor_out_of_scope_tools",
                        "vlm_out_of_scope_tools",
                        "world_out_of_scope_tools",
                        "simulation_out_of_scope_tools",
                    )
                    if scope.get(key)
                ]
                if scope_errors:
                    failures.append(f"{bundle}: out-of-scope runtime values: {scope_errors}")
                print(
                    f"{bundle}: actor={report['actor']['accepted']} "
                    f"vlm={report['vlm']['results']} "
                    f"skills={report['bt']['skill_actions']} "
                    f"phase={report['vlm']['phase_alignment']['scoreboard']['vlm']['display']} "
                    f"tool={report['vlm']['tool_alignment']['scoreboard']['vlm']['display']}"
                )
            except Exception as exc:
                failures.append(f"{bundle}: {exc}")
                print(f"{bundle}: failed: {exc}", file=sys.stderr)
        output = {"bundles": reports, "failures": failures, "generated_at": time.time()}
        report_path = Path(args.report_path)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(output, indent=2, sort_keys=True), encoding="utf-8")
        if failures:
            print(json.dumps(output, indent=2, sort_keys=True), file=sys.stderr)
            return 1
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0
    finally:
        probe.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
