"""Probe VLM v2 stable next-tool prediction gating for thyroidectomy."""

from __future__ import annotations

import argparse
import json
import sys
import time

from btops_interfaces.msg import CatalogSnapshot
from procedure_spec import get_default_spec_dir, load_bundle
import rclpy
from rclpy.node import Node
from surgical_msgs.msg import PhaseEvidence, ReducerDecisionEvent, SkillCommand, VLMResult, WorldState
from surgical_msgs.srv import ControlSimulation, SelectSimulationBundle

from .smoke_test import ManagedProcess, RESOURCE_ID


class PredictionProbe(Node):
    def __init__(self) -> None:
        super().__init__("thyroidectomy_prediction_probe")
        self.world: WorldState | None = None
        self.catalog: CatalogSnapshot | None = None
        self.skill_commands: list[SkillCommand] = []
        self.reducer_events: list[ReducerDecisionEvent] = []
        self.result_echoes: list[VLMResult] = []
        self._phase_pub = self.create_publisher(PhaseEvidence, "/vlm/phase_evidence", 20)
        self._result_pub = self.create_publisher(VLMResult, "/vlm/result", 20)
        self.create_subscription(CatalogSnapshot, "/btops/catalog", self._on_catalog, 10)
        self.create_subscription(WorldState, "/twin/world_state", self._on_world, 20)
        self.create_subscription(SkillCommand, "/bt/skill_command", self.skill_commands.append, 20)
        self.create_subscription(ReducerDecisionEvent, "/twin/reducer_decisions", self.reducer_events.append, 50)
        self.create_subscription(VLMResult, "/vlm/result", self.result_echoes.append, 50)
        self._select_bundle_client = self.create_client(SelectSimulationBundle, "/simulation/select_bundle")
        self._control_client = self.create_client(ControlSimulation, "/simulation/control")

    def _on_world(self, msg: WorldState) -> None:
        self.world = msg

    def _on_catalog(self, msg: CatalogSnapshot) -> None:
        self.catalog = msg

    def wait_for_services(self, timeout_sec: float = 25.0) -> None:
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

    def publish_phase(self, phase_id: str = "exposure") -> None:
        msg = PhaseEvidence()
        msg.stamp = self.get_clock().now().to_msg()
        msg.source = "prediction_probe"
        msg.phase_ids = [phase_id]
        msg.phase_confidences = [0.95]
        msg.visible_instrument_ids = []
        msg.visible_instrument_confidences = []
        msg.scene_summary = "stable thyroidectomy phase evidence"
        msg.uncertainty = 0.05
        self._phase_pub.publish(msg)

    def publish_prediction(self, tool_id: str, confidence: float) -> None:
        payload = {
            "v": "2",
            "phase": ["exposure", 0.95],
            "tool": [tool_id, confidence],
            "intent": ["none", "", 0.0],
            "mayo": [],
            "mayo_retrieve": ["", 0.0],
            "u": 0.05,
            "sum": "stable prediction probe",
        }
        msg = VLMResult()
        msg.stamp = self.get_clock().now().to_msg()
        msg.source = "prediction_probe"
        msg.schema_version = "2"
        msg.raw_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        msg.summary = payload["sum"]
        msg.phase_ids = ["exposure"]
        msg.phase_confidences = [0.95]
        msg.observed_tool_ids = []
        msg.observed_location_ids = []
        msg.observed_location_types = []
        msg.observed_confidences = []
        msg.gesture_event_type = ""
        msg.gesture_requested_tool = ""
        msg.gesture_hand_pose = ""
        msg.gesture_confidence = 0.0
        msg.uncertainty = 0.05
        self._result_pub.publish(msg)

    def spin_for(self, duration_sec: float) -> None:
        deadline = time.time() + duration_sec
        while time.time() < deadline:
            rclpy.spin_once(self, timeout_sec=0.1)

    def wait_until(self, predicate, timeout_sec: float, description: str) -> None:
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            rclpy.spin_once(self, timeout_sec=0.1)
            if predicate():
                return
        raise RuntimeError(f"timed out waiting for {description}")

    def wait_for_topic_links(self, timeout_sec: float = 10.0) -> None:
        self.wait_until(
            lambda: self._phase_pub.get_subscription_count() > 0 and self._result_pub.get_subscription_count() > 1,
            timeout_sec,
            "/vlm phase/result subscribers",
        )

    def wait_for_catalog_entry(self, timeout_sec: float = 25.0) -> None:
        self.wait_until(
            lambda: self.catalog is not None
            and any(behavior.identity == RESOURCE_ID for behavior in self.catalog.behaviors),
            timeout_sec,
            f"catalog entry {RESOURCE_ID}",
        )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec-name", default="thyroidectomy")
    parser.add_argument("--tool-id", default="metzenbaum")
    parser.add_argument("--confidence", type=float, default=0.92)
    parser.add_argument("--duration-sec", type=float, default=7.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    spec_dir = get_default_spec_dir().parent / str(args.spec_name)
    load_bundle(spec_dir)
    runtime = ManagedProcess(
        name="thyroidectomy_prediction_runtime",
        command=[
            "ros2",
            "launch",
            "bringup",
            "taskplanner_mock.launch.py",
            f"spec_dir:={spec_dir}",
            "enable_rosbridge:=false",
            "vlm_mode:=mock",
            "surgeon_actor_mode:=none",
            "enable_no_image_camera:=false",
            "enable_synthetic_scene_camera:=false",
        ],
    )
    rclpy.init()
    probe = PredictionProbe()
    try:
        runtime.start()
        probe.wait_for_services()
        probe.wait_for_catalog_entry()
        if args.spec_name != "thyroidectomy":
            probe.select_bundle(args.spec_name)
        probe.control("start")
        probe.wait_until(lambda: probe.world is not None and probe.world.running, 25.0, "running world")
        probe.wait_for_topic_links()

        no_prediction_deadline = time.time() + 3.0
        while time.time() < no_prediction_deadline:
            probe.publish_phase()
            rclpy.spin_once(probe, timeout_sec=0.1)
            time.sleep(0.2)
        if any(command.action == "predict_tool" for command in probe.skill_commands):
            raise RuntimeError("predict_tool dispatched before stable VLM prediction")

        end_time = time.time() + float(args.duration_sec)
        while time.time() < end_time:
            probe.publish_phase()
            probe.publish_prediction(args.tool_id, float(args.confidence))
            rclpy.spin_once(probe, timeout_sec=0.1)
            time.sleep(0.5)

        probe.wait_until(
            lambda: any(command.action == "predict_tool" and command.instrument_id == args.tool_id for command in probe.skill_commands),
            12.0,
            f"predict_tool dispatch for {args.tool_id}",
        )
        if not any(command.action == "predict_tool" and command.instrument_id == args.tool_id for command in probe.skill_commands):
            raise RuntimeError(
                f"predict_tool command for {args.tool_id} was not retained in command log"
            )
        print("Thyroidectomy prediction probe passed.")
        print(
            json.dumps(
                {
                    "tool": args.tool_id,
                    "commands": [
                        {"action": command.action, "tool": command.instrument_id}
                        for command in probe.skill_commands
                    ],
                    "latest_world": {
                        "predicted_tool": probe.world.predicted_tool if probe.world else "",
                        "prepositioned_tool": probe.world.prepositioned_tool if probe.world else "",
                        "predicted_tool_confidence": probe.world.predicted_tool_confidence if probe.world else 0.0,
                        "predicted_tool_stability_sec": probe.world.predicted_tool_stability_sec if probe.world else 0.0,
                    },
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except Exception as exc:
        print(f"Thyroidectomy prediction probe failed: {exc}", file=sys.stderr)
        print(
            json.dumps(
                {
                    "latest_world": {
                        "procedure": probe.world.procedure_id if probe.world else "",
                        "running": bool(probe.world.running) if probe.world else False,
                        "phase_uncertain": bool(probe.world.phase_uncertain) if probe.world else True,
                        "predicted_tool": probe.world.predicted_tool if probe.world else "",
                        "predicted_tool_confidence": probe.world.predicted_tool_confidence if probe.world else 0.0,
                        "predicted_tool_stability_sec": probe.world.predicted_tool_stability_sec if probe.world else 0.0,
                    },
                    "skill_commands": [
                        {"action": command.action, "tool": command.instrument_id}
                        for command in probe.skill_commands[-12:]
                    ],
                    "result_echo_count": len(probe.result_echoes),
                    "publisher_subscription_count": probe._result_pub.get_subscription_count(),
                    "reducer_events": [
                        {
                            "type": event.input_type,
                            "tool": event.affected_tool,
                            "accepted": bool(event.accepted),
                            "reason": event.reason,
                            "detail": event.detail_json,
                        }
                        for event in probe.reducer_events[-12:]
                    ],
                },
                indent=2,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
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
