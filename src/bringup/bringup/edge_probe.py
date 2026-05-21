"""Edge-case probe for fail-closed taskplanner runtime behavior."""

from __future__ import annotations

import argparse
import sys
import time

from btops_interfaces.srv import GetRuntimeState
from procedure_spec import get_default_spec_dir
import rclpy
from rcl_interfaces.msg import ParameterType
from rcl_interfaces.srv import GetParameters
from rclpy.node import Node
from surgical_msgs.msg import SurgeonRequest, ToolObservation, VLMReducerDecision, WorldState
from surgical_msgs.srv import ControlSimulation, InjectSurgeonOverride, SelectSimulationBundle

from .smoke_test import ManagedProcess


class EdgeProbe(Node):
    def __init__(self) -> None:
        super().__init__("taskplanner_edge_probe")
        self._latest_world: WorldState | None = None
        self._surgeon_requests: list[SurgeonRequest] = []
        self._vlm_reducer_decisions: list[VLMReducerDecision] = []
        self.create_subscription(WorldState, "/twin/world_state", self._on_world, 20)
        self.create_subscription(SurgeonRequest, "/surgeon/request", self._on_request, 20)
        self.create_subscription(VLMReducerDecision, "/vlm/reducer_decisions", self._on_vlm_reducer_decision, 20)
        self._tool_observation_pub = self.create_publisher(ToolObservation, "/vlm/tool_observations", 20)
        self._control_client = self.create_client(ControlSimulation, "/simulation/control")
        self._override_client = self.create_client(InjectSurgeonOverride, "/simulation/inject_surgeon_override")
        self._select_bundle_client = self.create_client(SelectSimulationBundle, "/simulation/select_bundle")
        self._runtime_client = self.create_client(GetRuntimeState, "/btops/get_runtime_state")
        self._param_client = self.create_client(GetParameters, "/tree_executor/get_parameters")

    def _on_world(self, msg: WorldState) -> None:
        self._latest_world = msg

    def _on_request(self, msg: SurgeonRequest) -> None:
        self._surgeon_requests.append(msg)

    def _on_vlm_reducer_decision(self, msg: VLMReducerDecision) -> None:
        self._vlm_reducer_decisions.append(msg)
        self._vlm_reducer_decisions = self._vlm_reducer_decisions[-40:]

    def wait_for_services(self, timeout_sec: float = 25.0) -> None:
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            ready = (
                self._control_client.wait_for_service(timeout_sec=0.2)
                and self._override_client.wait_for_service(timeout_sec=0.2)
                and self._select_bundle_client.wait_for_service(timeout_sec=0.2)
                and self._runtime_client.wait_for_service(timeout_sec=0.2)
                and self._param_client.wait_for_service(timeout_sec=0.2)
            )
            if ready:
                return
        raise RuntimeError("Timed out waiting for edge-probe services.")

    def wait_until(self, predicate, timeout_sec: float, description: str) -> None:
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            rclpy.spin_once(self, timeout_sec=0.1)
            if predicate():
                return
        raise RuntimeError(f"Timed out waiting for {description}.")

    def control_response(self, command: str):
        request = ControlSimulation.Request()
        request.command = command
        future = self._control_client.call_async(request)
        rclpy.spin_until_future_complete(self, future, timeout_sec=45.0 if command == "start" else 25.0)
        response = future.result()
        if response is None:
            raise RuntimeError(f"control {command} failed: no response")
        return response

    def control(self, command: str) -> None:
        response = self.control_response(command)
        if response is None or not response.success:
            raise RuntimeError(f"control {command} failed: {response.message if response else 'no response'}")

    def select_bundle(self, bundle_name: str, restart_if_running: bool):
        request = SelectSimulationBundle.Request()
        request.bundle_name = bundle_name
        request.restart_if_running = bool(restart_if_running)
        future = self._select_bundle_client.call_async(request)
        rclpy.spin_until_future_complete(self, future, timeout_sec=45.0)
        response = future.result()
        if response is None:
            raise RuntimeError("select_bundle returned no response")
        return response

    def inject_override(
        self,
        *,
        event_type: str,
        requested_tool: str,
        ready_for_handover: bool = True,
        ready_for_retrieval: bool = False,
    ):
        request = InjectSurgeonOverride.Request()
        request.event_type = event_type
        request.requested_tool = requested_tool
        request.voice_text = f"{requested_tool} please" if event_type == "voice_request" else ""
        request.ready_for_handover = bool(ready_for_handover)
        request.ready_for_retrieval = bool(ready_for_retrieval)
        request.clear_pending_requests = True
        future = self._override_client.call_async(request)
        rclpy.spin_until_future_complete(self, future, timeout_sec=15.0)
        response = future.result()
        if response is None:
            raise RuntimeError("inject_surgeon_override returned no response")
        return response

    def wait_running_bundle(self, bundle_name: str, timeout_sec: float = 25.0) -> None:
        self.wait_until(
            lambda: self._latest_world is not None
            and self._latest_world.procedure_id == bundle_name
            and self._latest_world.running
            and self._latest_world.execution_state == "running",
            timeout_sec,
            f"running world state for {bundle_name}",
        )

    def wait_idle_bundle(self, bundle_name: str, timeout_sec: float = 15.0) -> None:
        self.wait_until(
            lambda: self._latest_world is not None
            and self._latest_world.procedure_id == bundle_name
            and not self._latest_world.running
            and self._latest_world.execution_state == "idle",
            timeout_sec,
            f"idle world state for {bundle_name}",
        )

    def wait_control_state(self, expected: str, timeout_sec: float = 20.0) -> None:
        deadline = time.time() + timeout_sec
        latest = None
        while time.time() < deadline:
            response = self.control_response("status")
            latest = response.execution_state
            if response.success and response.execution_state == expected:
                return
            rclpy.spin_once(self, timeout_sec=0.1)
            time.sleep(0.1)
        raise RuntimeError(f"simulation manager did not report {expected}; latest={latest}")

    def wait_blackboard_bool(self, name: str, expected: bool, timeout_sec: float = 12.0) -> None:
        deadline = time.time() + timeout_sec
        latest = None
        while time.time() < deadline:
            request = GetParameters.Request()
            request.names = [name]
            future = self._param_client.call_async(request)
            rclpy.spin_until_future_complete(self, future, timeout_sec=5.0)
            response = future.result()
            if response and response.values:
                latest = response.values[0]
                if latest.type == ParameterType.PARAMETER_BOOL and bool(latest.bool_value) == expected:
                    return
            time.sleep(0.2)
        raise RuntimeError(f"blackboard parameter {name} did not become {expected}; latest={latest}")

    def instrument_lifecycle(self, tool_id: str) -> str:
        if self._latest_world is None:
            return ""
        for instrument in self._latest_world.instrument_states:
            if instrument.instrument_id == tool_id:
                return instrument.lifecycle_stage
        return ""

    def publish_impossible_observation(self, tool_id: str) -> None:
        observation = ToolObservation()
        observation.stamp = self.get_clock().now().to_msg()
        observation.instrument_id = tool_id
        observation.location_id = "surgeon_hand"
        observation.location_type = "surgeon_hand"
        observation.confidence = 0.99
        observation.visible = True
        for _ in range(3):
            self._tool_observation_pub.publish(observation)
            rclpy.spin_once(self, timeout_sec=0.1)
            time.sleep(0.1)

    def wait_vlm_rejection(self, tool_id: str, reason: str, timeout_sec: float = 8.0) -> None:
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            rclpy.spin_once(self, timeout_sec=0.1)
            for decision in reversed(self._vlm_reducer_decisions):
                if (
                    decision.instrument_id == tool_id
                    and not decision.accepted
                    and decision.reducer_result == "rejected"
                    and reason in decision.reducer_reason
                ):
                    return
            time.sleep(0.1)
        raise RuntimeError(f"VLM rejection for {tool_id}/{reason} was not observed")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vlm-mode", default="mock", choices=["mock", "real", "dual"])
    parser.add_argument("--vlm-response-mode", default="live")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    thyroidectomy_dir = get_default_spec_dir().parent / "thyroidectomy"
    runtime = ManagedProcess(
        name="taskplanner_edge_runtime",
        command=[
            "ros2",
            "launch",
            "bringup",
            "taskplanner_mock.launch.py",
            f"spec_dir:={thyroidectomy_dir}",
            "enable_rosbridge:=false",
            f"vlm_mode:={args.vlm_mode}",
            f"vlm_response_mode:={args.vlm_response_mode}",
        ],
    )
    rclpy.init()
    probe = EdgeProbe()
    try:
        runtime.start()
        probe.wait_for_services()
        response = probe.select_bundle("thyroidectomy", restart_if_running=False)
        if not response.success:
            raise RuntimeError(f"initial bundle selection failed: {response.message}")
        probe.wait_idle_bundle("thyroidectomy")
        if probe.instrument_lifecycle("retractor") != "home_rack":
            raise RuntimeError(
                f"retractor did not start at home_rack: {probe.instrument_lifecycle('retractor')}"
            )
        probe.publish_impossible_observation("retractor")
        probe.wait_vlm_rejection("retractor", "observation_direct_rebase_forbidden")
        if probe.instrument_lifecycle("retractor") != "home_rack":
            raise RuntimeError(
                "impossible VLM observation changed world state: "
                f"retractor lifecycle={probe.instrument_lifecycle('retractor')}"
            )
        probe.control("start")
        probe.wait_running_bundle("thyroidectomy")
        probe.wait_control_state("running")

        response = probe.inject_override(event_type="voice_request", requested_tool="bone_saw")
        if response.success or "unknown tool" not in response.message:
            raise RuntimeError(f"unknown tool override was not rejected: {response}")
        time.sleep(0.5)
        rclpy.spin_once(probe, timeout_sec=0.1)
        if any(request.requested_tool == "bone_saw" for request in probe._surgeon_requests):
            raise RuntimeError("unknown tool propagated to /surgeon/request")
        if probe._latest_world and probe._latest_world.surgeon_request_tool == "bone_saw":
            raise RuntimeError("unknown tool propagated to world state")

        response = probe.inject_override(event_type="dance", requested_tool="retractor")
        if response.success or "unsupported surgeon override event_type" not in response.message:
            raise RuntimeError(f"invalid event type was not rejected: {response}")

        probe.control("pause")
        response = probe.inject_override(event_type="voice_request", requested_tool="retractor")
        if response.success or response.message != "simulation paused; resume before injecting surgeon override":
            raise RuntimeError(f"pause-state override was not rejected: {response}")
        probe.control("resume")
        probe.wait_running_bundle("thyroidectomy")

        response = probe.select_bundle("nephrectomy", restart_if_running=False)
        if response.success or "cannot switch bundle while simulation is running" not in response.message:
            raise RuntimeError(f"running bundle switch without restart was not rejected: {response}")

        response = probe.select_bundle("nephrectomy", restart_if_running=True)
        if not response.success:
            raise RuntimeError(f"running bundle restart failed: {response.message}")
        probe.wait_running_bundle("nephrectomy", timeout_sec=35.0)
        probe.wait_blackboard_bool("bb.tool.retractor.active", False)
        probe.wait_blackboard_bool("bb.tool.cautery.active", False)

        print("Taskplanner edge probe passed.")
        return 0
    except Exception as exc:
        print(f"Taskplanner edge probe failed: {exc}", file=sys.stderr)
        if runtime.log_path:
            print(runtime.tail(), file=sys.stderr)
        return 1
    finally:
        probe.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
        runtime.stop()


if __name__ == "__main__":
    raise SystemExit(main())
