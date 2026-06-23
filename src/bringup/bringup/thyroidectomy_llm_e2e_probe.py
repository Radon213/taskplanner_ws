"""Thyroidectomy-focused LLM surgeon actor + real VLM end-to-end probe."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import statistics
import sys
import time

from btops_interfaces.msg import CatalogSnapshot
from procedure_spec import get_default_spec_dir, load_bundle
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import CompressedImage
from std_msgs.msg import String
from surgical_msgs.msg import (
    BTDecision,
    ReducerDecisionEvent,
    SkillCommand,
    SkillStatus,
    SurgeonLLMDecision,
    SurgeonState,
    TwinEvent,
    VLMHealth,
    VLMResult,
    WorldState,
)
from surgical_msgs.srv import ControlSimulation, SelectSimulationBundle

from .smoke_test import HANDOVER_ACTIONS, ManagedProcess, RESOURCE_ID


RECOVERY_ACTIONS = {
    "retrieve_from_mayo",
    "retrieve_from_hand",
    "tool_retrieve",
    "return_unused_preposition",
}
TOOL_PREDICTION_MIN_LEAD_SEC = 3.0


def _stamp_to_sec(stamp) -> float:
    return float(stamp.sec) + float(stamp.nanosec) / 1_000_000_000.0


def _alignment_scoreboard(samples: list[dict]) -> dict:
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
        "requested_view": {
            "format": "system_correct / vlm_proposed / evaluable",
            "system_correct": system_correct,
            "vlm_proposed": vlm_proposed,
            "evaluable": evaluable,
            "display": f"{system_correct} / {vlm_proposed} / {evaluable}",
        },
    }


class ThyroidectomyLLME2EProbe(Node):
    def __init__(self) -> None:
        super().__init__("thyroidectomy_llm_e2e_probe")
        self.world: WorldState | None = None
        self.world_states: list[WorldState] = []
        self.catalog: CatalogSnapshot | None = None
        self.actor_decisions: list[SurgeonLLMDecision] = []
        self.surgeon_states: list[SurgeonState] = []
        self.vlm_results: list[VLMResult] = []
        self.vlm_health: list[VLMHealth] = []
        self.skill_commands: list[SkillCommand] = []
        self.skill_statuses: list[SkillStatus] = []
        self.skill_events: list[TwinEvent] = []
        self.bt_decisions: list[BTDecision] = []
        self.reducer_events: list[ReducerDecisionEvent] = []
        self.overlay_samples: list[dict] = []
        self.image_frames = 0
        self.world_invariant_violations: list[str] = []

        self.create_subscription(WorldState, "/twin/world_state", self._on_world, 20)
        self.create_subscription(CatalogSnapshot, "/btops/catalog", self._on_catalog, 10)
        self.create_subscription(SurgeonLLMDecision, "/surgeon/llm_decision", self.actor_decisions.append, 50)
        self.create_subscription(SurgeonState, "/surgeon/state", self._on_surgeon_state, 50)
        self.create_subscription(VLMResult, "/vlm/result", self.vlm_results.append, 50)
        self.create_subscription(VLMHealth, "/vlm/health", self.vlm_health.append, 50)
        self.create_subscription(SkillCommand, "/bt/skill_command", self.skill_commands.append, 50)
        self.create_subscription(SkillStatus, "/skill/status", self.skill_statuses.append, 50)
        self.create_subscription(TwinEvent, "/skill/events", self.skill_events.append, 50)
        self.create_subscription(BTDecision, "/bt/decision", self.bt_decisions.append, 50)
        self.create_subscription(ReducerDecisionEvent, "/twin/reducer_decisions", self.reducer_events.append, 50)
        self.create_subscription(String, "/surgeon/actor_overlay", self._on_overlay, 50)
        self.create_subscription(CompressedImage, "/surgery/images/field/compressed", self._on_image, 20)

        self._select_bundle_client = self.create_client(SelectSimulationBundle, "/simulation/select_bundle")
        self._control_client = self.create_client(ControlSimulation, "/simulation/control")

    def _on_world(self, msg: WorldState) -> None:
        self.world = msg
        self.world_states.append(msg)
        self.world_states = self.world_states[-300:]
        self._record_world_invariants(msg)

    def _on_surgeon_state(self, msg: SurgeonState) -> None:
        self.surgeon_states.append(msg)
        self.surgeon_states = self.surgeon_states[-300:]

    def _on_catalog(self, msg: CatalogSnapshot) -> None:
        self.catalog = msg

    def _on_overlay(self, msg: String) -> None:
        try:
            payload = json.loads(msg.data)
        except json.JSONDecodeError:
            return
        if isinstance(payload, dict):
            self.overlay_samples.append(payload)
            self.overlay_samples = self.overlay_samples[-80:]

    def _on_image(self, _msg: CompressedImage) -> None:
        self.image_frames += 1

    def _record_world_invariants(self, msg: WorldState) -> None:
        holder_by_tool: dict[str, set[str]] = {}
        surgeon_owned = []
        for instrument in msg.instrument_states:
            holders = holder_by_tool.setdefault(instrument.instrument_id, set())
            owner = instrument.owner
            location_type = instrument.location_type
            if owner == "surgeon" or location_type == "surgeon_hand":
                holders.add("surgeon")
            elif owner == "robot_right_hand" or location_type == "robot_right_hand":
                holders.add("robot_right_hand")
            elif owner == "robot_left_hand" or location_type == "robot_left_hand":
                holders.add("robot_left_hand")
            elif location_type == "cleaner_slot":
                holders.add("cleaner")
            elif owner and owner != "none":
                holders.add(owner)
            if instrument.lifecycle_stage == "surgeon_owned":
                surgeon_owned.append(instrument.instrument_id)
        if len(surgeon_owned) > 2:
            self.world_invariant_violations.append(f"surgeon owned >2 tools: {sorted(surgeon_owned)}")
        for tool_id, holders in holder_by_tool.items():
            physical = holders.intersection({"surgeon", "surgeon_hand", "robot_right_hand", "robot_left_hand", "cleaner_slot"})
            if len(physical) > 1:
                self.world_invariant_violations.append(f"{tool_id} appears in multiple holders: {sorted(physical)}")

    def wait_for_services(self, timeout_sec: float = 30.0) -> None:
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            if self._select_bundle_client.wait_for_service(timeout_sec=0.2) and self._control_client.wait_for_service(timeout_sec=0.2):
                return
        raise RuntimeError("simulation services were not ready")

    def select_bundle(self, bundle_name: str) -> None:
        request = SelectSimulationBundle.Request()
        request.bundle_name = bundle_name
        request.restart_if_running = False
        future = self._select_bundle_client.call_async(request)
        rclpy.spin_until_future_complete(self, future, timeout_sec=30.0)
        response = future.result()
        if response is None or not response.success:
            raise RuntimeError(f"select bundle failed: {response.message if response else 'no response'}")

    def control(self, command: str) -> None:
        request = ControlSimulation.Request()
        request.command = command
        future = self._control_client.call_async(request)
        rclpy.spin_until_future_complete(self, future, timeout_sec=35.0)
        response = future.result()
        if response is None or not response.success:
            raise RuntimeError(f"control {command} failed: {response.message if response else 'no response'}")

    def wait_until(self, predicate, timeout_sec: float, description: str) -> None:
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            rclpy.spin_once(self, timeout_sec=0.2)
            if predicate():
                return
        raise RuntimeError(f"timed out waiting for {description}")

    def wait_for_catalog_entry(self, timeout_sec: float = 25.0) -> None:
        self.wait_until(
            lambda: self.catalog is not None
            and any(behavior.identity == RESOURCE_ID for behavior in self.catalog.behaviors),
            timeout_sec,
            f"catalog entry {RESOURCE_ID}",
        )

    def spin_for(self, duration_sec: float) -> None:
        deadline = time.time() + duration_sec
        while time.time() < deadline:
            rclpy.spin_once(self, timeout_sec=0.2)

    def _phase_at(self, rows, stamp_sec: float, attr: str) -> str:
        latest = ""
        for row in rows:
            row_stamp = _stamp_to_sec(row.stamp)
            if row_stamp <= stamp_sec + 0.5:
                latest = str(getattr(row, attr, "") or latest)
            elif row_stamp > stamp_sec + 0.5:
                break
        return latest

    def _phase_alignment(self) -> dict:
        samples = []
        for result in self.vlm_results:
            stamp_sec = _stamp_to_sec(result.stamp)
            ground = self._phase_at(self.surgeon_states, stamp_sec, "phase_id")
            system = self._phase_at(self.world_states, stamp_sec, "filtered_phase")
            vlm_phase = result.phase_ids[0] if result.phase_ids else ""
            if not ground:
                continue
            samples.append(
                {
                    "ground": ground,
                    "vlm": vlm_phase,
                    "system": system,
                    "vlm_match": vlm_phase == ground,
                    "system_match": bool(system and system == ground),
                }
            )
        return {
            "samples": len(samples),
            "vlm_matches_ground": sum(1 for item in samples if item["vlm_match"]),
            "vlm_coverage": sum(1 for item in samples if item["vlm"]),
            "system_matches_ground": sum(1 for item in samples if item["system_match"]),
            "system_coverage": sum(1 for item in samples if item["system"]),
            "scoreboard": _alignment_scoreboard(samples),
            "last": samples[-1] if samples else {},
        }

    def _vlm_tool_prediction(self, result: VLMResult) -> str:
        try:
            payload = json.loads(result.raw_json)
        except json.JSONDecodeError:
            return ""
        raw_tool = payload.get("tool", [])
        if isinstance(raw_tool, list) and raw_tool and isinstance(raw_tool[0], list):
            return str(raw_tool[0][0])
        if isinstance(raw_tool, list) and len(raw_tool) == 2:
            return str(raw_tool[0])
        return ""

    def _system_prediction_after(
        self,
        stamp_sec: float,
        horizon_sec: float = 20.0,
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
                world.active_robot_task_type in {"predict_tool", "tool_predict"}
                and world.active_robot_task_tool_id
            ):
                return world.active_robot_task_tool_id
            if world.prepositioned_tool:
                prepositioned_state = next(
                    (
                        instrument
                        for instrument in world.instrument_states
                        if instrument.instrument_id == world.prepositioned_tool
                    ),
                    None,
                )
                if (
                    prepositioned_state is None
                    or prepositioned_state.lifecycle_stage == "prepositioned_right"
                    or not prepositioned_state.next_required_transition
                ):
                    return world.prepositioned_tool
            if world.predicted_tool and float(world.predicted_tool_confidence) >= best_confidence:
                best_tool = world.predicted_tool
                best_confidence = float(world.predicted_tool_confidence)
        for event in self.skill_events:
            event_sec = _stamp_to_sec(event.stamp)
            if event_sec < stamp_sec:
                continue
            if until_sec is not None and event_sec >= until_sec:
                break
            if event_sec - stamp_sec > horizon_sec:
                break
            if event.event_type == "ToolPrepared" and event.instrument_id:
                return event.instrument_id
        return best_tool

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

    def _next_request_after(self, stamp_sec: float, horizon_sec: float = 20.0) -> str:
        tool, _ = self._next_request_after_with_time(stamp_sec, horizon_sec)
        return tool

    def _tool_alignment(self) -> dict:
        samples = []
        insufficient_system_lead_skipped = 0
        for result in self.vlm_results:
            stamp_sec = _stamp_to_sec(result.stamp)
            ground, ground_sec = self._next_request_after_with_time(stamp_sec)
            if not ground:
                continue
            lead_sec = ground_sec - stamp_sec
            if lead_sec < TOOL_PREDICTION_MIN_LEAD_SEC:
                insufficient_system_lead_skipped += 1
                continue
            raw_tool = self._vlm_tool_prediction(result)
            system_tool = self._system_prediction_after(stamp_sec, until_sec=ground_sec)
            samples.append(
                {
                    "lead_sec": round(lead_sec, 2),
                    "ground": ground,
                    "vlm": raw_tool,
                    "system": system_tool,
                    "vlm_match": raw_tool == ground,
                    "system_match": system_tool == ground,
                }
            )
        confusion = Counter(f"{item['ground']}->{item['vlm'] or 'none'}" for item in samples)
        return {
            "samples": len(samples),
            "vlm_coverage": sum(1 for item in samples if item["vlm"]),
            "system_coverage": sum(1 for item in samples if item["system"]),
            "vlm_matches_ground": sum(1 for item in samples if item["vlm_match"]),
            "system_matches_ground": sum(1 for item in samples if item["system_match"]),
            "insufficient_system_lead_skipped": insufficient_system_lead_skipped,
            "scoreboard": _alignment_scoreboard(samples),
            "confusion_top": dict(confusion.most_common(8)),
            "recent_mismatches": [item for item in samples if not item["vlm_match"] or not item["system_match"]][-6:],
        }

    def report(self, duration_sec: float) -> dict:
        accepted_actor = [msg for msg in self.actor_decisions if msg.accepted]
        rejected_actor = [msg for msg in self.actor_decisions if not msg.accepted]
        health_latencies = [float(msg.latency_sec) for msg in self.vlm_health if float(msg.latency_sec) > 0.0]
        actor_latencies = [float(msg.latency_sec) for msg in accepted_actor if float(msg.latency_sec) > 0.0]
        vlm_schema_versions = Counter(str(msg.schema_version) for msg in self.vlm_results)
        skill_actions = Counter(msg.action for msg in self.skill_commands)
        skill_events = Counter(msg.event_type for msg in self.skill_events)
        reducer_reasons = Counter(msg.reason for msg in self.reducer_events)
        actor_actions = Counter(msg.action for msg in accepted_actor)
        request_modes = Counter(msg.request_mode for msg in accepted_actor)
        overlays_with_hand = sum(1 for item in self.overlay_samples if item.get("hand"))
        overlays_with_mayo = sum(1 for item in self.overlay_samples if item.get("mayo"))
        overlay_leaks = [
            item
            for item in self.overlay_samples
            if any(str(value).lower() in {"recover", "reuse"} for value in item.get("mayo", []))
        ]
        healthy_samples = [msg for msg in self.vlm_health if bool(msg.connected and msg.healthy) and not msg.last_error]
        prompt_chars = [int(msg.prompt_chars) for msg in self.vlm_health if int(msg.prompt_chars) > 0]
        phase_alignment = self._phase_alignment()
        tool_alignment = self._tool_alignment()

        def stats(values: list[float]) -> dict:
            if not values:
                return {"count": 0}
            return {
                "count": len(values),
                "avg": round(statistics.mean(values), 3),
                "p95": round(sorted(values)[max(0, int(len(values) * 0.95) - 1)], 3),
                "max": round(max(values), 3),
            }

        return {
            "duration_sec": duration_sec,
            "world_procedure": self.world.procedure_id if self.world else "",
            "image_frames": self.image_frames,
            "image_hz_est": round(self.image_frames / max(duration_sec, 1.0), 2),
            "actor": {
                "accepted": len(accepted_actor),
                "rejected": len(rejected_actor),
                "actions": dict(actor_actions),
                "request_modes": dict(request_modes),
                "latency_sec": stats(actor_latencies),
                "reject_reasons": dict(Counter(msg.reject_reason for msg in rejected_actor)),
            },
            "vlm": {
                "results": len(self.vlm_results),
                "schema_versions": dict(vlm_schema_versions),
                "healthy_samples": len(healthy_samples),
                "health_samples": len(self.vlm_health),
                "phase_alignment": phase_alignment,
                "tool_alignment": tool_alignment,
                "latency_sec": stats(health_latencies),
                "prompt_chars": {
                    "min": min(prompt_chars) if prompt_chars else 0,
                    "max": max(prompt_chars) if prompt_chars else 0,
                },
                "last_error_samples": [msg.last_error for msg in self.vlm_health if msg.last_error][-5:],
            },
            "bt": {
                "decisions": dict(Counter(msg.decision for msg in self.bt_decisions)),
                "skill_actions": dict(skill_actions),
                "skill_events": dict(skill_events),
                "reducer_reasons": dict(reducer_reasons),
            },
            "overlay": {
                "samples": len(self.overlay_samples),
                "with_hand": overlays_with_hand,
                "with_mayo": overlays_with_mayo,
                "leak_count": len(overlay_leaks),
            },
            "world_invariant_violations": sorted(set(self.world_invariant_violations))[:12],
        }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration-sec", type=float, default=120.0)
    parser.add_argument("--spec-name", default="thyroidectomy")
    parser.add_argument("--vlm-base-url", default="http://127.0.0.1:1234")
    parser.add_argument("--vlm-model-id", default="qwen3.6-35b-a3b-mtp@q2_k_xl")
    parser.add_argument("--actor-model-id", default="google/gemma-4-12b-qat")
    parser.add_argument("--report-path", default="reports/thyroidectomy_llm_e2e_report.json")
    return parser.parse_args(argv)


def _assert_report(report: dict) -> None:
    if report["world_procedure"] != "thyroidectomy":
        raise RuntimeError(f"wrong procedure: {report['world_procedure']}")
    if report["image_hz_est"] < 20.0:
        raise RuntimeError(f"no-image camera rate too low: {report['image_hz_est']} Hz")
    if report["actor"]["accepted"] < 3:
        raise RuntimeError("LLM actor did not produce enough accepted decisions")
    if "request_tool" not in report["actor"]["actions"]:
        raise RuntimeError("LLM actor never requested a tool")
    if report["overlay"]["with_hand"] < 1:
        raise RuntimeError("No-image overlay never showed hand extension")
    if report["overlay"]["with_mayo"] < 1:
        raise RuntimeError("No-image overlay never showed Mayo stand contents")
    if report["overlay"]["leak_count"] != 0:
        raise RuntimeError("Mayo overlay leaked recover/reuse labels")
    schema_samples = report["vlm"]["schema_versions"].get("3", 0) + report["vlm"]["schema_versions"].get("2", 0)
    if schema_samples < 5:
        raise RuntimeError("real VLM did not produce enough schema v2/v3 results")
    if report["vlm"]["healthy_samples"] < max(3, int(report["vlm"]["health_samples"] * 0.6)):
        raise RuntimeError("real VLM health was not stable enough")
    if not any(action in HANDOVER_ACTIONS for action in report["bt"]["skill_actions"]):
        raise RuntimeError("BT never dispatched a handover action")
    if "retrieve_from_mayo" not in report["bt"]["skill_actions"]:
        raise RuntimeError("BT never dispatched retrieve_from_mayo")
    if "retrieve_from_hand" in report["bt"]["skill_actions"]:
        raise RuntimeError("legacy retrieve_from_hand was dispatched in normal flow")
    if "ToolReturnedToTray" not in report["bt"]["skill_events"]:
        raise RuntimeError("recovered Mayo tool never returned to rack")
    if report["world_invariant_violations"]:
        raise RuntimeError("world invariant violations observed: " + "; ".join(report["world_invariant_violations"]))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    spec_dir = get_default_spec_dir().parent / str(args.spec_name)
    load_bundle(spec_dir)
    runtime = ManagedProcess(
        name="thyroidectomy_llm_e2e_runtime",
        command=[
            "ros2",
            "launch",
            "bringup",
            "taskplanner_mock.launch.py",
            f"spec_dir:={spec_dir}",
            "enable_rosbridge:=false",
            "vlm_mode:=real",
            f"vlm_base_url:={args.vlm_base_url}",
            f"vlm_model_id:={args.vlm_model_id}",
            "vlm_api_mode:=openai_compat",
            "vlm_publish_period_sec:=1.0",
            "vlm_response_format:=json_schema",
            "vlm_reasoning_effort:=none",
            "vlm_context_mode:=actor_log",
            "surgeon_actor_mode:=llm",
            f"actor_base_url:={args.vlm_base_url}",
            f"actor_model_id:={args.actor_model_id}",
            "actor_response_format:=json_schema",
            "actor_reasoning_effort:=none",
            "enable_no_image_camera:=true",
            "enable_synthetic_scene_camera:=false",
        ],
    )

    rclpy.init()
    probe = ThyroidectomyLLME2EProbe()
    try:
        runtime.start()
        probe.wait_for_services()
        probe.wait_for_catalog_entry()
        if args.spec_name != "thyroidectomy":
            probe.select_bundle(args.spec_name)
        probe.control("start")
        probe.wait_until(lambda: probe.world is not None and probe.world.running, 25.0, "running world")
        probe.spin_for(float(args.duration_sec))
        report = probe.report(float(args.duration_sec))
        report_path = Path(args.report_path)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        _assert_report(report)
        print("Thyroidectomy LLM/VLM E2E probe passed.")
        phase_scoreboard = report["vlm"]["phase_alignment"]["scoreboard"]
        tool_scoreboard = report["vlm"]["tool_alignment"]["scoreboard"]
        print("Alignment scoreboard format: correct / proposed / evaluable")
        print(f"Phase VLM: {phase_scoreboard['vlm']['display']}")
        print(f"Phase system final: {phase_scoreboard['system']['display']}")
        print(
            "Phase requested view "
            f"(system_correct / vlm_proposed / evaluable): {phase_scoreboard['requested_view']['display']}"
        )
        print(f"Tool VLM: {tool_scoreboard['vlm']['display']}")
        print(f"Tool system final: {tool_scoreboard['system']['display']}")
        print(
            "Tool requested view "
            f"(system_correct / vlm_proposed / evaluable): {tool_scoreboard['requested_view']['display']}"
        )
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"Thyroidectomy LLM/VLM E2E probe failed: {exc}", file=sys.stderr)
        try:
            report = probe.report(float(args.duration_sec))
            print(json.dumps(report, indent=2, sort_keys=True), file=sys.stderr)
        except Exception:
            pass
        if runtime.log_path:
            print(runtime.tail(100), file=sys.stderr)
        return 1
    finally:
        probe.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
        runtime.stop()


if __name__ == "__main__":
    raise SystemExit(main())
