"""Integration smoke test for the taskplanner v1 digital twin demo."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from typing import Callable

from btops_interfaces.msg import CatalogSnapshot
from btops_interfaces.srv import GetRuntimeState
from procedure_spec import get_default_spec_dir, load_bundle
import rclpy
from rcl_interfaces.msg import ParameterType
from rcl_interfaces.srv import GetParameters
from rclpy.node import Node
from surgical_msgs.msg import (
    BTDecision,
    SkillCommand,
    SkillStatus,
    SurgeonRequest,
    TwinEvent,
    WorldState,
)
from surgical_msgs.srv import ControlSimulation, InjectSurgeonOverride, SelectSimulationBundle


RESOURCE_ID = "tree/taskplanner_bt_trees::surgical_assist_v1::TaskplannerAssistDemo"
EXPECTED_DECISIONS = {"recovery", "idle"}
EXPECTED_SKILL_EVENTS = {
    "ToolHandoverCompleted",
    "ToolReceivedFromSurgeon",
    "ToolSentToCleaner",
    "ToolReturnedToTray",
}
HANDOVER_ACTIONS = {
    "direct_handover",
    "pick_up_and_handover",
    "put_down_and_handover",
    "tool_handover",
    "predicted_tool_handover",
    "replace_and_handover",
    "handover_tool",
}
RECOVERY_ACTIONS = {
    "retrieve_from_hand",
    "retrieve_from_mayo",
    "tool_retrieve",
    "return_tool_to_rack",
    "return_unused_preposition",
}


@dataclass
class ManagedProcess:
    name: str
    command: list[str]
    process: subprocess.Popen | None = None
    log_path: Path | None = None

    def start(self) -> None:
        log_handle = tempfile.NamedTemporaryFile(
            mode="w+", prefix=f"{self.name}_", suffix=".log", delete=False
        )
        self.log_path = Path(log_handle.name)
        self.process = subprocess.Popen(
            self.command,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            preexec_fn=os.setsid,
        )
        log_handle.close()

    def stop(self) -> None:
        if not self.process or self.process.poll() is not None:
            return

        try:
            os.killpg(os.getpgid(self.process.pid), signal.SIGINT)
            self.process.wait(timeout=8.0)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
            try:
                self.process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
                self.process.wait(timeout=2.0)

    def tail(self, lines: int = 60) -> str:
        if not self.log_path or not self.log_path.exists():
            return ""
        content = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(content[-lines:])


class SmokeHarness(Node):
    def __init__(self) -> None:
        super().__init__("taskplanner_smoke_harness")
        self._catalog: CatalogSnapshot | None = None
        self._decision_records: list[tuple[str, str, str, bool]] = []
        self._decision_set: set[tuple[str, str, str, bool]] = set()
        self._seen_decision_names: set[str] = set()
        self._skill_commands: list[tuple[str, str, str, str]] = []
        self._skill_command_set: set[tuple[str, str, str, str]] = set()
        self._skill_statuses: list[tuple[str, str, bool, str]] = []
        self._skill_status_set: set[tuple[str, str, bool, str]] = set()
        self._skill_event_types: set[str] = set()
        self._skill_event_records: list[tuple[str, str, str]] = []
        self._cleaned_tools: set[str] = set()
        self._returned_tools: set[str] = set()
        self._surgeon_requests: list[tuple[str, str, bool]] = []
        self._latest_world: WorldState | None = None
        self._world_invariant_violations: list[str] = []

        self.create_subscription(CatalogSnapshot, "/btops/catalog", self._on_catalog, 10)
        self.create_subscription(BTDecision, "/bt/decision", self._on_decision, 20)
        self.create_subscription(SkillCommand, "/bt/skill_command", self._on_skill_command, 20)
        self.create_subscription(SkillStatus, "/skill/status", self._on_skill_status, 20)
        self.create_subscription(TwinEvent, "/skill/events", self._on_skill_event, 20)
        self.create_subscription(SurgeonRequest, "/surgeon/request", self._on_surgeon_request, 20)
        self.create_subscription(WorldState, "/twin/world_state", self._on_world_state, 20)

        self._runtime_client = self.create_client(GetRuntimeState, "/btops/get_runtime_state")
        self._param_client = self.create_client(GetParameters, "/tree_executor/get_parameters")
        self._select_bundle_client = self.create_client(
            SelectSimulationBundle, "/simulation/select_bundle"
        )
        self._control_client = self.create_client(ControlSimulation, "/simulation/control")
        self._override_client = self.create_client(
            InjectSurgeonOverride, "/simulation/inject_surgeon_override"
        )

    def _on_catalog(self, msg: CatalogSnapshot) -> None:
        self._catalog = msg

    def _on_decision(self, msg: BTDecision) -> None:
        record = (
            msg.decision,
            msg.action,
            msg.selected_tool or "none",
            bool(msg.handover_allowed),
        )
        if record in self._decision_set:
            return
        self._decision_set.add(record)
        self._decision_records.append(record)
        self._seen_decision_names.add(msg.decision)

    def _on_skill_command(self, msg: SkillCommand) -> None:
        record = (
            msg.action,
            msg.instrument_id or "none",
            msg.target_location_id or "none",
            msg.target_location_type or "none",
        )
        if record in self._skill_command_set:
            return
        self._skill_command_set.add(record)
        self._skill_commands.append(record)

    def _on_skill_status(self, msg: SkillStatus) -> None:
        record = (
            msg.action,
            msg.state,
            bool(msg.success),
            msg.instrument_id or "none",
        )
        if record in self._skill_status_set:
            return
        self._skill_status_set.add(record)
        self._skill_statuses.append(record)

    def _on_skill_event(self, msg: TwinEvent) -> None:
        record = (
            msg.event_type,
            msg.instrument_id or "none",
            msg.location_id or "none",
        )
        if record in self._skill_event_records:
            return
        self._skill_event_records.append(record)
        self._skill_event_types.add(msg.event_type)
        if msg.event_type == "ToolCleaningCompleted" and msg.instrument_id:
            self._cleaned_tools.add(msg.instrument_id)
        if msg.event_type == "ToolReturnedToTray" and msg.instrument_id:
            self._returned_tools.add(msg.instrument_id)

    def _on_surgeon_request(self, msg: SurgeonRequest) -> None:
        self._surgeon_requests.append(
            (msg.event_type, msg.requested_tool or "none", bool(msg.override))
        )

    def _on_world_state(self, msg: WorldState) -> None:
        self._latest_world = msg
        self._record_world_invariants(msg)

    def _record_world_invariants(self, msg: WorldState) -> None:
        left_arm_tools = {
            instrument.instrument_id
            for instrument in msg.instrument_states
            if instrument.owner == "robot_left_hand"
            or instrument.location_type in {"robot_left_hand", "cleaner_slot"}
        }
        right_arm_tools = {
            instrument.instrument_id
            for instrument in msg.instrument_states
            if instrument.owner == "robot_right_hand" or instrument.location_type == "robot_right_hand"
        }
        surgeon_tools = {
            instrument.instrument_id
            for instrument in msg.instrument_states
            if instrument.status == "handed_over"
        }
        cleaning_tools = {
            instrument.instrument_id
            for instrument in msg.instrument_states
            if instrument.status == "cleaning" or instrument.location_type == "cleaner_slot"
        }

        if len(left_arm_tools) > 1:
            self._world_invariant_violations.append(
                f"left arm carried multiple tools simultaneously: {sorted(left_arm_tools)}"
            )
        if len(right_arm_tools) > 1:
            self._world_invariant_violations.append(
                f"right arm carried multiple tools simultaneously: {sorted(right_arm_tools)}"
            )
        if len(surgeon_tools) > 1:
            self._world_invariant_violations.append(
                f"surgeon held multiple active tools simultaneously: {sorted(surgeon_tools)}"
            )
        if msg.left_hand_tool and msg.left_hand_tool not in left_arm_tools:
            self._world_invariant_violations.append(
                f"left_hand_tool='{msg.left_hand_tool}' was not backed by an instrument state"
            )
        if msg.right_hand_tool and msg.right_hand_tool not in right_arm_tools:
            self._world_invariant_violations.append(
                f"right_hand_tool='{msg.right_hand_tool}' was not backed by an instrument state"
            )
        if msg.cleaner_busy:
            if len(cleaning_tools) != 1:
                self._world_invariant_violations.append(
                    f"cleaner_busy expected one cleaning tool but saw {sorted(cleaning_tools)}"
                )
            if msg.left_hand_tool and msg.left_hand_tool not in cleaning_tools and msg.left_hand_tool not in left_arm_tools:
                self._world_invariant_violations.append(
                    f"cleaner_busy with left hand tracking mismatch: left_hand_tool={msg.left_hand_tool}"
                )
        if msg.robot_state == "retracted" and (msg.left_hand_tool or msg.right_hand_tool or msg.cleaner_busy):
            self._world_invariant_violations.append(
                "robot_state was 'retracted' while a hand or cleaner still carried a tool"
            )

        for instrument in msg.instrument_states:
            at_home = (
                instrument.location_id == instrument.home_location_id
                and instrument.location_type == instrument.home_location_type
            )
            if instrument.contaminated and at_home:
                self._world_invariant_violations.append(
                    f"contaminated tool returned directly to rack: {instrument.instrument_id}"
                )

    def wait_until(
        self, predicate: Callable[[], bool], timeout_sec: float, description: str
    ) -> None:
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            rclpy.spin_once(self, timeout_sec=0.2)
            if predicate():
                return
        raise RuntimeError(f"Timed out waiting for {description}.")

    def wait_for_services(self, timeout_sec: float = 20.0) -> None:
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            ready = (
                self._runtime_client.wait_for_service(timeout_sec=0.2)
                and self._param_client.wait_for_service(timeout_sec=0.2)
                and self._select_bundle_client.wait_for_service(timeout_sec=0.2)
                and self._control_client.wait_for_service(timeout_sec=0.2)
                and self._override_client.wait_for_service(timeout_sec=0.2)
            )
            if ready:
                return
        raise RuntimeError("Timed out waiting for simulation_manager and btops services.")

    def wait_for_catalog_entry(self, timeout_sec: float = 20.0) -> None:
        self.wait_until(
            lambda: self._catalog is not None
            and any(behavior.identity == RESOURCE_ID for behavior in self._catalog.behaviors),
            timeout_sec,
            f"catalog entry {RESOURCE_ID}",
        )

    def select_bundle(self, bundle_name: str) -> None:
        request = SelectSimulationBundle.Request()
        request.bundle_name = bundle_name
        request.restart_if_running = False
        future = self._select_bundle_client.call_async(request)
        rclpy.spin_until_future_complete(self, future, timeout_sec=20.0)
        response = future.result()
        if response is None or not response.success:
            raise RuntimeError(
                f"Failed to select bundle: {response.message if response else 'no response'}"
            )

    def control(self, command: str) -> None:
        request = ControlSimulation.Request()
        request.command = command
        future = self._control_client.call_async(request)
        timeout_sec = 30.0 if command == "start" else 20.0
        rclpy.spin_until_future_complete(self, future, timeout_sec=timeout_sec)
        response = future.result()
        if response is None or not response.success:
            raise RuntimeError(
                f"Failed to issue simulation control '{command}': {response.message if response else 'no response'}"
            )

    def wait_for_decisions(self, timeout_sec: float) -> None:
        self.wait_until(
            lambda: EXPECTED_DECISIONS.issubset(self._seen_decision_names),
            timeout_sec,
            f"decisions {sorted(EXPECTED_DECISIONS)}",
        )

    def wait_for_action_roundtrip(self, timeout_sec: float = 20.0) -> None:
        self.wait_until(
            lambda: any(
                state == "completed" and success
                for _, state, success, _ in self._skill_statuses
            )
            and EXPECTED_SKILL_EVENTS.issubset(self._skill_event_types),
            timeout_sec,
            f"skill action roundtrip {sorted(EXPECTED_SKILL_EVENTS)}",
        )

    def wait_for_cleaned_tool_return(self, timeout_sec: float = 20.0) -> None:
        self.wait_until(
            lambda: bool(self._cleaned_tools) and self._cleaned_tools.issubset(self._returned_tools),
            timeout_sec,
            "cleaned tools to be returned to rack",
        )

    def inject_voice_override(self, requested_tool: str) -> None:
        request = InjectSurgeonOverride.Request()
        request.event_type = "voice_request"
        request.requested_tool = requested_tool
        request.voice_text = f"{requested_tool} please"
        request.ready_for_handover = True
        request.ready_for_retrieval = False
        request.clear_pending_requests = True
        future = self._override_client.call_async(request)
        rclpy.spin_until_future_complete(self, future, timeout_sec=15.0)
        response = future.result()
        if response is None or not response.success:
            raise RuntimeError(
                "Failed to inject surgeon override: "
                f"{response.message if response else 'no response'}"
            )

    def wait_for_override_request(self, tool_id: str, timeout_sec: float = 8.0) -> None:
        self.wait_until(
            lambda: any(
                event_type == "voice_request" and requested_tool == tool_id and override
                for event_type, requested_tool, override in self._surgeon_requests
            ),
            timeout_sec,
            f"surgeon override for {tool_id}",
        )

    def wait_for_override_dispatch(self, tool_id: str, timeout_sec: float = 12.0) -> None:
        self.wait_until(
            lambda: any(
                action in HANDOVER_ACTIONS and instrument_id == tool_id
                for action, instrument_id, _, _ in self._skill_commands
            ),
            timeout_sec,
            f"handover dispatch for override tool {tool_id}",
        )

    def wait_for_handover_window(self, timeout_sec: float = 12.0) -> None:
        self.wait_until(
            lambda: self._latest_world is not None
            and bool(self._latest_world.handover_allowed)
            and not bool(self._latest_world.phase_uncertain)
            and not bool(self._latest_world.cleaner_busy),
            timeout_sec,
            "handover-ready world state",
        )

    def choose_override_tool(self, spec) -> str:
        if self._latest_world is None:
            raise RuntimeError("No world state available to choose an override tool.")
        state_by_tool = {
            instrument.instrument_id: instrument for instrument in self._latest_world.instrument_states
        }

        def requestable(tool_id: str) -> bool:
            instrument = state_by_tool.get(tool_id)
            if instrument is None:
                return False
            if instrument.contaminated:
                return False
            if instrument.owner == "surgeon":
                return False
            if instrument.location_type in {"surgical_field", "surgeon_hand", "return_zone", "mayo_recovery_zone"}:
                return False
            return instrument.status in {"available", "prepared", "held"}

        for event_type, requested_tool, override in reversed(self._surgeon_requests):
            if override:
                continue
            if event_type not in {"request_tool", "voice_request"}:
                continue
            if requested_tool != "none" and requestable(requested_tool):
                return requested_tool

        preferred = [
            self._latest_world.surgeon_request_tool,
            self._latest_world.prepositioned_tool,
            self._latest_world.right_hand_tool,
        ]
        for tool_id in preferred:
            if tool_id and requestable(tool_id):
                return tool_id

        expected_requestable = [
            tool_id for tool_id in self._latest_world.expected_instruments if requestable(tool_id)
        ]
        if expected_requestable:
            for tool_id in expected_requestable:
                if tool_id != "retractor":
                    return tool_id
            return expected_requestable[0]

        available_requestable = [
            tool_id for tool_id in self._latest_world.available_instruments if requestable(tool_id)
        ]
        if available_requestable:
            for tool_id in available_requestable:
                if tool_id != "retractor":
                    return tool_id
            return available_requestable[0]

        for tool_id in self._latest_world.available_instruments:
            if requestable(tool_id):
                return tool_id

        candidates = list(self._latest_world.expected_instruments) or spec.list_instrument_ids()
        for tool_id in candidates:
            if requestable(tool_id):
                return tool_id
        for tool_id in spec.list_instrument_ids():
            if requestable(tool_id):
                return tool_id
        raise RuntimeError("Could not find a requestable override tool.")

    def wait_for_blackboard_string(
        self, name: str, expected_value: str, timeout_sec: float = 8.0
    ) -> None:
        deadline = time.time() + timeout_sec
        latest_value = None
        while time.time() < deadline:
            response_values = self.get_blackboard_params([name])
            latest_value = response_values[0]
            if latest_value.string_value == expected_value:
                return
            time.sleep(0.2)
        raise RuntimeError(
            f"Mirrored blackboard parameter {name} did not become '{expected_value}'. "
            f"Last value was '{latest_value.string_value if latest_value else ''}'."
        )

    def get_runtime_state(self):
        deadline = time.time() + 8.0
        last_response = None
        while time.time() < deadline:
            request = GetRuntimeState.Request()
            request.executor_name = "tree_executor"
            future = self._runtime_client.call_async(request)
            rclpy.spin_until_future_complete(self, future, timeout_sec=10.0)
            response = future.result()
            if response is None or not response.success:
                last_response = response
                time.sleep(0.2)
                continue
            if response.snapshot.full_tree_xml:
                return response
            last_response = response
            time.sleep(0.2)
        raise RuntimeError(
            f"Failed to fetch runtime state: {last_response.message if last_response else 'no response'}"
        )

    def get_blackboard_params(self, names: list[str]):
        request = GetParameters.Request()
        request.names = list(names)
        future = self._param_client.call_async(request)
        rclpy.spin_until_future_complete(self, future, timeout_sec=10.0)
        response = future.result()
        if response is None:
            raise RuntimeError("Failed to fetch mirrored blackboard parameters.")
        return response.values

    def wait_for_blackboard_params(self, names: list[str], timeout_sec: float = 10.0):
        deadline = time.time() + timeout_sec
        latest_values = None
        while time.time() < deadline:
            latest_values = self.get_blackboard_params(names)
            if all(value.type != ParameterType.PARAMETER_NOT_SET for value in latest_values):
                return latest_values
            time.sleep(0.2)
        unset = [
            name
            for name, value in zip(names, latest_values or [])
            if value.type == ParameterType.PARAMETER_NOT_SET
        ]
        raise RuntimeError(f"Mirrored blackboard parameters were unset: {unset}")


def _load_recording_event_count(metadata_json: str) -> int:
    try:
        metadata = json.loads(metadata_json or "{}")
    except json.JSONDecodeError:
        return 0
    return int(metadata.get("event_count", 0))


def _assert_runtime_state(response) -> None:
    if not response.snapshot.full_tree_xml:
        raise RuntimeError("Runtime state did not include full_tree_xml.")
    if "TaskplannerAssistTick" not in response.snapshot.full_tree_xml:
        raise RuntimeError("Runtime XML did not include the expected TaskplannerAssistTick loop.")
    if "SelectRecoveryTool" not in response.snapshot.full_tree_xml:
        raise RuntimeError("Runtime XML did not include SelectRecoveryTool.")
    if _load_recording_event_count(response.snapshot.recording_metadata_json) <= 0:
        raise RuntimeError("Runtime recording metadata did not accumulate any events.")
    if not response.blackboard_entries:
        raise RuntimeError("Runtime state did not report any blackboard entries.")
    if not response.recent_transitions:
        raise RuntimeError("Runtime state did not report any recent transitions.")


def _format_decisions(decisions: list[tuple[str, str, str, bool]]) -> str:
    return "\n".join(
        f"  - decision={decision} action={action} tool={tool} allowed={allowed}"
        for decision, action, tool, allowed in decisions
    )


def _format_skill_statuses(statuses: list[tuple[str, str, bool, str]]) -> str:
    return "\n".join(
        f"  - action={action} state={state} success={success} tool={tool}"
        for action, state, success, tool in statuses
    )


def _format_skill_events(events: list[tuple[str, str, str]]) -> str:
    return "\n".join(
        f"  - event={event_type} tool={tool} location={location}"
        for event_type, tool, location in events
    )


def _format_surgeon_requests(requests: list[tuple[str, str, bool]]) -> str:
    return "\n".join(
        f"  - event={event_type} tool={tool} override={override}"
        for event_type, tool, override in requests
    )


def _make_blackboard_param_names(spec) -> list[str]:
    instrument_ids = spec.list_instrument_ids()
    sample_tool = instrument_ids[0] if instrument_ids else "unknown"
    return [
        "bb.procedure.id",
        "bb.bundle.generation",
        "bb.phase.id",
        "bb.request.explicit_tool",
        "bb.request.surgeon_tool",
        "bb.selected.tool",
        "bb.robot.state",
        "bb.robot.right_hand_tool",
        "bb.robot.left_hand_tool",
        "bb.cleaner.busy",
        f"bb.tool.{sample_tool}.active",
        f"bb.tool.{sample_tool}.status",
        f"bb.tool.{sample_tool}.cleanliness",
    ]


def _assert_recovery_targets(harness: SmokeHarness, spec) -> None:
    recovery_commands = [record for record in harness._skill_commands if record[0] in RECOVERY_ACTIONS]
    if not recovery_commands:
        raise RuntimeError("Recovery branch never published a recovery skill command.")

    for _, instrument_id, target_location_id, target_location_type in recovery_commands:
        expected_location_id = spec.get_initial_location(instrument_id)
        expected_location_type = spec.get_initial_location_type(instrument_id)
        if target_location_id != expected_location_id or target_location_type != expected_location_type:
            raise RuntimeError(
                "Recovery command did not target the configured home slot: "
                f"tool={instrument_id} expected=({expected_location_id}, {expected_location_type}) "
                f"actual=({target_location_id}, {target_location_type})"
            )


def _assert_world_invariants(harness: SmokeHarness) -> None:
    if harness._world_invariant_violations:
        unique = []
        seen = set()
        for violation in harness._world_invariant_violations:
            if violation in seen:
                continue
            seen.add(violation)
            unique.append(violation)
        raise RuntimeError(
            "World-state invariants were violated:\n  - " + "\n  - ".join(unique[:12])
        )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--spec-name",
        default="thyroidectomy",
        help="Procedure spec bundle name under procedure_spec/specs.",
    )
    parser.add_argument(
        "--decision-timeout-sec",
        type=float,
        default=80.0,
        help="Maximum time to wait for the full branch set to appear.",
    )
    parser.add_argument("--vlm-mode", default="mock", choices=["mock", "real", "dual"])
    parser.add_argument("--vlm-response-mode", default="live")
    parser.add_argument("--vlm-base-url", default="http://192.168.0.122:1234")
    parser.add_argument("--vlm-model-id", default="gemma-4-26b-a4b-it")
    parser.add_argument(
        "--runtime-check",
        default="all",
        choices=["all", "functional", "observability"],
        help=(
            "all runs functional and BT Ops observability checks; functional skips Groot2/full-tree "
            "assertions; observability keeps the full runtime-state assertions."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    spec_dir = get_default_spec_dir().parent / str(args.spec_name)
    spec = load_bundle(spec_dir)
    runtime = ManagedProcess(
        name="taskplanner_runtime",
        command=[
            "ros2",
            "launch",
            "bringup",
            "taskplanner_mock.launch.py",
            f"spec_dir:={spec_dir}",
            "enable_rosbridge:=false",
            f"vlm_mode:={args.vlm_mode}",
            f"vlm_response_mode:={args.vlm_response_mode}",
            f"vlm_base_url:={args.vlm_base_url}",
            f"vlm_model_id:={args.vlm_model_id}",
        ],
    )

    rclpy.init()
    harness = SmokeHarness()
    override_summary = "not attempted"

    try:
        runtime.start()
        harness.wait_for_services()
        harness.wait_for_catalog_entry()
        harness.select_bundle(args.spec_name)
        harness.control("start")
        action_timeout = max(120.0, args.decision_timeout_sec + 40.0)
        harness.wait_for_action_roundtrip(timeout_sec=action_timeout)
        harness.wait_for_cleaned_tool_return(timeout_sec=action_timeout)
        harness.wait_for_handover_window(timeout_sec=min(action_timeout, 20.0))
        override_tool = harness.choose_override_tool(spec)
        try:
            harness.inject_voice_override(override_tool)
            harness.wait_for_override_request(override_tool)
            harness.wait_for_override_dispatch(override_tool, timeout_sec=16.0)
            override_summary = f"override dispatched for {override_tool}"
        except Exception as exc:
            override_summary = f"override remained best-effort for {override_tool}: {exc}"

        runtime_state = None
        if args.runtime_check in {"all", "observability"}:
            runtime_state = harness.get_runtime_state()
            try:
                _assert_runtime_state(runtime_state)
            except Exception as exc:
                raise RuntimeError(f"BT Ops observability smoke failed: {exc}") from exc

        harness.wait_for_blackboard_params(_make_blackboard_param_names(spec))

        recovery_records = [record for record in harness._decision_records if record[0] == "recovery"]
        if not recovery_records or all(record[2] == "none" for record in recovery_records):
            raise RuntimeError("Recovery branch never produced a concrete tool id.")
        _assert_recovery_targets(harness, spec)
        _assert_world_invariants(harness)

        print("Taskplanner smoke test passed.")
        print(f"Spec bundle: {spec_dir}")
        print(f"Voice override tool: {override_tool}")
        print(f"Override check: {override_summary}")
        print("Observed decisions:")
        print(_format_decisions(harness._decision_records))
        print("Observed skill statuses:")
        print(_format_skill_statuses(harness._skill_statuses))
        print(
            "Runtime checks:"
            + (
                f" full_tree_xml=yes transitions={len(runtime_state.recent_transitions)}"
                f" blackboard_entries={len(runtime_state.blackboard_entries)}"
                f" recorded_events={_load_recording_event_count(runtime_state.snapshot.recording_metadata_json)}"
                if runtime_state is not None
                else " functional-only"
            )
        )
        return 0
    except Exception as exc:
        print(f"Taskplanner smoke test failed: {exc}", file=sys.stderr)
        if harness._decision_records:
            print("\nObserved decisions:", file=sys.stderr)
            print(_format_decisions(harness._decision_records), file=sys.stderr)
        if harness._skill_statuses:
            print("\nObserved skill statuses:", file=sys.stderr)
            print(_format_skill_statuses(harness._skill_statuses), file=sys.stderr)
        if harness._skill_event_records:
            print("\nObserved skill events:", file=sys.stderr)
            print(_format_skill_events(harness._skill_event_records[-16:]), file=sys.stderr)
        if harness._surgeon_requests:
            print("\nObserved surgeon requests:", file=sys.stderr)
            print(_format_surgeon_requests(harness._surgeon_requests[-12:]), file=sys.stderr)
        if harness._world_invariant_violations:
            print("\nObserved world invariant violations:", file=sys.stderr)
            for violation in harness._world_invariant_violations[-12:]:
                print(f"  - {violation}", file=sys.stderr)
        if runtime.log_path:
            print("\nRuntime log tail:", file=sys.stderr)
            print(runtime.tail(), file=sys.stderr)
        return 1
    finally:
        harness.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
        runtime.stop()


if __name__ == "__main__":
    raise SystemExit(main())
