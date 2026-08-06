"""Manual strong hand-cue probe for VLM-driven surgeon requests."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import sys
import time

from procedure_spec import get_default_spec_dir, load_bundle
import rclpy
from rclpy.parameter import Parameter
from rclpy.parameter_client import AsyncParameterClient
from surgical_msgs.msg import (
    BTDecision,
    PhaseEvidence,
    SkillCommand,
    SurgeonGestureEvidence,
    TwinEvent,
    VLMResult,
)

from .smoke_test import ManagedProcess, SmokeHarness


def _stamp_to_sec(stamp) -> float:
    return float(stamp.sec) + float(stamp.nanosec) / 1_000_000_000.0


@dataclass
class ProbeResult:
    bundle: str
    implicit_request_tool_id: str
    initial_preposition_tool_id: str
    replacement_preposition_tool_id: str
    explicit_request_tool_id: str
    visual_evidence_did_not_create_explicit_request: bool
    implicit_handover_completed: bool
    initial_preposition_completed: bool
    prediction_evidence_withdrawal_returned: bool
    wrong_preposition_returned: bool
    replacement_preposition_completed: bool
    explicit_handover_completed: bool
    implicit_request_to_handover_sec: float
    prediction_to_preposition_sec: float
    prediction_evidence_withdrawal_release_sec: float
    wrong_preposition_release_sec: float
    replacement_prediction_to_preposition_sec: float
    invariant_violation_count: int


class ManualProbeHarness(SmokeHarness):
    def __init__(self) -> None:
        super().__init__()
        self._gesture_pub = self.create_publisher(
            SurgeonGestureEvidence, "/vlm/surgeon_gesture_evidence", 20
        )
        self._parameter_client = AsyncParameterClient(self, "/surgeon_actor")
        self._decision_log: list[BTDecision] = []
        self._event_log: list[TwinEvent] = []
        self._skill_command_log: list[SkillCommand] = []
        self._surgeon_request_log: list[tuple[float, str, str]] = []
        self._vlm_result_pub = self.create_publisher(VLMResult, "/vlm/result", 20)
        self._phase_evidence_pub = self.create_publisher(
            PhaseEvidence,
            "/vlm/phase_evidence",
            20,
        )
        self.create_subscription(BTDecision, "/bt/decision", self._on_probe_decision, 20)
        self.create_subscription(SkillCommand, "/bt/skill_command", self._skill_command_log.append, 20)
        self.create_subscription(TwinEvent, "/skill/events", self._on_probe_event, 50)

    def _on_probe_decision(self, msg: BTDecision) -> None:
        self._decision_log.append(msg)

    def _on_probe_event(self, msg: TwinEvent) -> None:
        self._event_log.append(msg)

    def _on_surgeon_request(self, msg):  # type: ignore[override]
        super()._on_surgeon_request(msg)
        self._surgeon_request_log.append(
            (_stamp_to_sec(msg.stamp), msg.event_type, msg.requested_tool)
        )

    def wait_for_probe_subscribers(self, timeout_sec: float = 10.0) -> None:
        self.wait_until(
            lambda: self._phase_evidence_pub.get_subscription_count() > 0
            and self._vlm_result_pub.get_subscription_count() > 0,
            timeout_sec,
            "probe evidence subscribers",
        )

    def set_random_voice_enabled(self, enabled: bool) -> bool:
        if not self._parameter_client.services_are_ready():
            if not self._parameter_client.wait_for_services(timeout_sec=3.0):
                return False
        future = self._parameter_client.set_parameters(
            [Parameter(name="random_voice_enabled", value=enabled)]
        )
        rclpy.spin_until_future_complete(self, future, timeout_sec=10.0)
        response = future.result()
        if response is None:
            return False
        results = getattr(response, "results", [])
        if not all(result.successful for result in results):
            reasons = "; ".join(result.reason or "unknown" for result in results)
            raise RuntimeError(f"Failed to set random_voice_enabled: {reasons}")
        return bool(results)

    def choose_probe_tool(self) -> str:
        if self._latest_world is None:
            raise RuntimeError("No world state available yet for probe.")
        expected = set(self._latest_world.expected_instruments)

        def requestable(instrument) -> bool:
            return (
                not instrument.contaminated
                and instrument.lifecycle_stage in {"home_rack", "returned_home", "prepositioned_right"}
                and instrument.owner in {"", "none", "robot_right_hand"}
                and instrument.location_type not in {"mayo_reuse_zone", "mayo_recovery_zone", "surgical_field", "surgeon_hand"}
            )

        instruments = list(self._latest_world.instrument_states)

        for instrument in instruments:
            if requestable(instrument) and instrument.instrument_id == self._latest_world.surgeon_request_tool:
                return instrument.instrument_id
        for instrument in instruments:
            if requestable(instrument) and instrument.instrument_id in expected:
                return instrument.instrument_id
        for instrument in instruments:
            if requestable(instrument) and instrument.instrument_id == self._latest_world.prepositioned_tool:
                return instrument.instrument_id

        for instrument in instruments:
            if requestable(instrument) and instrument.instrument_id not in expected:
                return instrument.instrument_id
        for instrument in instruments:
            if requestable(instrument):
                return instrument.instrument_id
        raise RuntimeError("Could not find a requestable tool for the current phase.")

    def choose_recovery_probe_tool(self, preferred_tool: str) -> str:
        if self._latest_world is None:
            raise RuntimeError("No world state available for recovery probe.")
        recoverable = [
            instrument.instrument_id
            for instrument in self._latest_world.instrument_states
            if instrument.lifecycle_stage in {"surgeon_owned", "mayo_recovery"}
        ]
        if preferred_tool in recoverable:
            return preferred_tool
        if recoverable:
            return recoverable[0]
        raise RuntimeError("Could not find a recoverable surgeon tool for the return probe.")

    def choose_override_probe_tool(self, excluded_tool: str) -> str:
        if self._latest_world is None:
            raise RuntimeError("No world state available for override probe.")
        expected = set(self._latest_world.expected_instruments)

        def requestable(instrument) -> bool:
            return (
                instrument.instrument_id != excluded_tool
                and not instrument.contaminated
                and instrument.lifecycle_stage in {"home_rack", "returned_home"}
                and instrument.owner in {"", "none"}
                and instrument.location_type not in {"mayo_reuse_zone", "mayo_recovery_zone", "surgical_field", "surgeon_hand"}
            )

        instruments = list(self._latest_world.instrument_states)
        for instrument in instruments:
            if requestable(instrument) and instrument.instrument_id not in expected:
                return instrument.instrument_id
        for instrument in instruments:
            if requestable(instrument):
                return instrument.instrument_id
        raise RuntimeError("Could not find an alternate explicit-request tool for override probe.")

    def emit_gesture_probe(self, *, phase_id: str, event_type: str, tool_id: str, note: str) -> None:
        hand_pose = "open_receive" if event_type == "request_tool" else "present_used_tool"
        for _ in range(5):
            stamp = self.get_clock().now().to_msg()
            evidence = SurgeonGestureEvidence()
            evidence.stamp = stamp
            evidence.procedure_id = (
                self._latest_world.procedure_id if self._latest_world else ""
            )
            evidence.phase_id = phase_id
            evidence.event_type = event_type
            evidence.requested_tool = tool_id
            evidence.hand_pose = hand_pose
            evidence.confidence = 0.98
            evidence.note = note
            self._gesture_pub.publish(evidence)

            payload = {
                "v": "4",
                "phase": [[phase_id, 0.95]],
                "tool": [],
                "intent": ["", "", 0.0],
                "gesture": [event_type, tool_id, hand_pose, 0.98],
                "mayo": [],
                "mayo_retrieve": ["", 0.0],
                "u": 0.05,
                "sum": note,
                "bed_robot_arm_group": None,
            }
            result = VLMResult()
            result.stamp = stamp
            result.source = "manual_probe"
            result.schema_version = "4"
            result.raw_json = json.dumps(
                payload,
                separators=(",", ":"),
                sort_keys=True,
            )
            result.summary = note
            result.phase_ids = [phase_id]
            result.phase_confidences = [0.95]
            result.observed_tool_ids = []
            result.observed_location_ids = []
            result.observed_location_types = []
            result.observed_confidences = []
            result.gesture_event_type = event_type
            result.gesture_requested_tool = tool_id
            result.gesture_hand_pose = hand_pose
            result.gesture_confidence = 0.98
            result.uncertainty = 0.05
            self._vlm_result_pub.publish(result)

            end_time = time.time() + 0.25
            while time.time() < end_time:
                rclpy.spin_once(self, timeout_sec=0.05)

    def emit_stable_tool_prediction(
        self,
        *,
        phase_id: str,
        tool_id: str,
        confidence: float = 0.92,
        duration_sec: float = 6.5,
    ) -> None:
        end_time = time.time() + duration_sec
        while time.time() < end_time:
            stamp = self.get_clock().now().to_msg()
            evidence = PhaseEvidence()
            evidence.stamp = stamp
            evidence.source = "manual_probe"
            evidence.phase_ids = [phase_id]
            evidence.phase_confidences = [0.95]
            evidence.visible_instrument_ids = []
            evidence.visible_instrument_confidences = []
            evidence.uncertainty = 1.0 - confidence
            evidence.scene_summary = "manual stable phase observation"
            self._phase_evidence_pub.publish(evidence)

            payload = {
                "v": "2",
                "phase": [phase_id, 0.95],
                "tool": [tool_id, confidence],
                "intent": ["none", "", 0.0],
                "mayo": [],
                "mayo_retrieve": ["", 0.0],
                "u": 0.05,
                "sum": f"manual stable next-tool prediction for {tool_id}",
            }
            msg = VLMResult()
            msg.stamp = stamp
            msg.source = "manual_probe"
            msg.schema_version = "2"
            msg.raw_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
            msg.summary = payload["sum"]
            msg.phase_ids = [phase_id]
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
            self._vlm_result_pub.publish(msg)
            rclpy.spin_once(self, timeout_sec=0.05)
            time.sleep(0.45)

    def emit_stable_phase_observation(
        self,
        *,
        phase_id: str,
        confidence: float = 0.95,
        duration_sec: float = 2.0,
    ) -> None:
        end_time = time.time() + duration_sec
        while time.time() < end_time:
            stamp = self.get_clock().now().to_msg()
            evidence = PhaseEvidence()
            evidence.stamp = stamp
            evidence.source = "manual_probe"
            evidence.phase_ids = [phase_id]
            evidence.phase_confidences = [confidence]
            evidence.visible_instrument_ids = []
            evidence.visible_instrument_confidences = []
            evidence.uncertainty = 1.0 - confidence
            evidence.scene_summary = "manual stable phase observation"
            self._phase_evidence_pub.publish(evidence)

            payload = {
                "v": "2",
                "phase": [phase_id, confidence],
                "tool": ["", 0.0],
                "intent": ["none", "", 0.0],
                "mayo": [],
                "mayo_retrieve": ["", 0.0],
                "u": 1.0 - confidence,
                "sum": "manual stable phase observation",
            }
            msg = VLMResult()
            msg.stamp = stamp
            msg.source = "manual_probe"
            msg.schema_version = "2"
            msg.raw_json = json.dumps(
                payload,
                separators=(",", ":"),
                sort_keys=True,
            )
            msg.summary = payload["sum"]
            msg.phase_ids = [phase_id]
            msg.phase_confidences = [confidence]
            msg.observed_tool_ids = []
            msg.observed_location_ids = []
            msg.observed_location_types = []
            msg.observed_confidences = []
            msg.gesture_event_type = ""
            msg.gesture_requested_tool = ""
            msg.gesture_hand_pose = ""
            msg.gesture_confidence = 0.0
            msg.uncertainty = 1.0 - confidence
            self._vlm_result_pub.publish(msg)
            rclpy.spin_once(self, timeout_sec=0.05)
            time.sleep(0.2)

    def wait_for_request_transition(self, event_type: str, tool_id: str, timeout_sec: float = 10.0) -> None:
        self.wait_until(
            lambda: any(
                request_event == event_type and requested_tool == tool_id
                for _, request_event, requested_tool in self._surgeon_request_log
            ),
            timeout_sec,
            f"surgeon request transition {event_type}:{tool_id}",
        )

    def wait_for_decision(
        self, decision: str, tool_id: str, timeout_sec: float = 12.0
    ) -> BTDecision:
        self.wait_until(
            lambda: any(
                record.decision == decision and record.selected_tool == tool_id
                for record in self._decision_log
            ),
            timeout_sec,
            f"bt decision {decision}:{tool_id}",
        )
        return next(
            record
            for record in reversed(self._decision_log)
            if record.decision == decision and record.selected_tool == tool_id
        )

    def wait_for_skill_event(
        self, event_type: str, tool_id: str, timeout_sec: float = 16.0
    ) -> TwinEvent:
        self.wait_until(
            lambda: any(
                event.event_type == event_type and event.instrument_id == tool_id
                for event in self._event_log
            ),
            timeout_sec,
            f"skill event {event_type}:{tool_id}",
        )
        return next(
            event
            for event in reversed(self._event_log)
            if event.event_type == event_type and event.instrument_id == tool_id
        )

    def wait_for_skill_command(
        self, action: str, tool_id: str, timeout_sec: float = 12.0
    ) -> SkillCommand:
        self.wait_until(
            lambda: any(
                command.action == action and command.instrument_id == tool_id
                for command in self._skill_command_log
            ),
            timeout_sec,
            f"skill command {action}:{tool_id}",
        )
        return next(
            command
            for command in reversed(self._skill_command_log)
            if command.action == action and command.instrument_id == tool_id
        )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec-name", default="thyroidectomy")
    parser.add_argument(
        "--vlm-mode",
        default="none",
        choices=["none", "mock", "real", "dual"],
        help=(
            "External VLM source for the probe. The default disables competing "
            "publishers; the probe still injects public VLM evidence itself."
        ),
    )
    parser.add_argument("--vlm-response-mode", default="live")
    parser.add_argument("--vlm-base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--vlm-model-id", default="unsloth/gemma-4-E4B-it-NVFP4")
    parser.add_argument(
        "--surgeon-actor-mode",
        default="rule",
        choices=["rule", "llm", "none"],
        help="Actor used by the probe runtime; rule is deterministic after muting.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    spec_dir = get_default_spec_dir().parent / str(args.spec_name)
    load_bundle(spec_dir)
    runtime = ManagedProcess(
        name="taskplanner_probe_runtime",
        command=[
            "ros2",
            "launch",
            "bringup",
            "taskplanner_mock.launch.py",
            f"spec_dir:={spec_dir}",
            f"default_bundle:={args.spec_name}",
            "enable_rosbridge:=false",
            f"vlm_mode:={args.vlm_mode}",
            f"vlm_response_mode:={args.vlm_response_mode}",
            f"vlm_base_url:={args.vlm_base_url}",
            f"vlm_model_id:={args.vlm_model_id}",
            f"surgeon_actor_mode:={args.surgeon_actor_mode}",
        ],
    )

    rclpy.init()
    harness = ManualProbeHarness()
    result = None
    try:
        runtime.start()
        harness.wait_for_services()
        harness.wait_for_catalog_entry()
        harness.wait_for_probe_subscribers()
        harness.select_bundle(args.spec_name)
        if (
            args.surgeon_actor_mode != "none"
            and not harness.set_random_voice_enabled(False)
        ):
            raise RuntimeError(
                "selected surgeon actor does not expose random_voice_enabled"
            )
        time.sleep(1.0)
        harness.control("start")
        harness.wait_until(
            lambda: harness._latest_world is not None
            and harness._latest_world.execution_state == "running",
            18.0,
            "running world state",
        )
        if harness._latest_world is None:
            raise RuntimeError("No world state available after start.")
        tool_id = harness.choose_probe_tool()
        phase_id = harness._latest_world.filtered_phase
        harness.emit_stable_phase_observation(phase_id=phase_id)
        harness.wait_for_handover_window(timeout_sec=12.0)

        request_log_start = len(harness._surgeon_request_log)
        implicit_started_at = time.monotonic()
        harness.emit_gesture_probe(
            phase_id=phase_id,
            event_type="request_tool",
            tool_id=tool_id,
            note=f"manual strong request cue for {tool_id}",
        )
        harness.wait_for_decision("implicit_request", tool_id)
        harness.wait_for_skill_event("ToolHandoverCompleted", tool_id)
        implicit_handover_sec = time.monotonic() - implicit_started_at
        leaked_visual_request = any(
            event_type in {"request_tool", "voice_request"}
            and requested_tool == tool_id
            for _, event_type, requested_tool in harness._surgeon_request_log[
                request_log_start:
            ]
        )
        if leaked_visual_request:
            raise RuntimeError(
                "visual gesture evidence leaked into the explicit surgeon request path"
            )

        harness.wait_until(
            lambda: harness._latest_world is not None
            and any(
                instrument.instrument_id == tool_id and instrument.owner == "surgeon"
                for instrument in harness._latest_world.instrument_states
            ),
            10.0,
            f"{tool_id} to appear with surgeon",
        )

        prediction_tool = harness.choose_probe_tool()
        prediction_phase = harness._latest_world.filtered_phase if harness._latest_world else phase_id
        prediction_started_at = time.monotonic()
        harness.emit_stable_tool_prediction(
            phase_id=prediction_phase,
            tool_id=prediction_tool,
            duration_sec=1.4,
        )
        harness.wait_for_skill_command("predict_tool", prediction_tool)
        harness.wait_until(
            lambda: harness._latest_world is not None
            and harness._latest_world.prepositioned_tool == prediction_tool,
            16.0,
            "stable VLM-predicted tool to become prepositioned",
        )
        prediction_to_preposition_sec = time.monotonic() - prediction_started_at
        if harness._latest_world is None:
            raise RuntimeError("No world state available for replacement probe.")
        prepositioned_tool = harness._latest_world.prepositioned_tool

        withdrawal_started_at = time.monotonic()
        harness.emit_stable_phase_observation(
            phase_id=harness._latest_world.filtered_phase,
            duration_sec=2.0,
        )
        harness.wait_for_skill_event(
            "PredictedToolReturnedToRack",
            prepositioned_tool,
        )
        prediction_evidence_withdrawal_release_sec = (
            time.monotonic() - withdrawal_started_at
        )
        harness.wait_until(
            lambda: harness._latest_world is not None
            and not harness._latest_world.prepositioned_tool,
            8.0,
            "expired prediction preparation slot to become empty",
        )

        replacement_tool = harness.choose_override_probe_tool(prepositioned_tool)
        replacement_started_at = time.monotonic()
        harness.emit_stable_tool_prediction(
            phase_id=harness._latest_world.filtered_phase,
            tool_id=replacement_tool,
            duration_sec=1.4,
        )
        harness.wait_for_skill_command("predict_tool", replacement_tool)
        harness.wait_until(
            lambda: harness._latest_world is not None
            and harness._latest_world.prepositioned_tool == replacement_tool,
            16.0,
            "replacement prediction to become prepositioned",
        )
        replacement_prediction_to_preposition_sec = (
            time.monotonic() - replacement_started_at
        )

        harness.inject_voice_override(replacement_tool)
        harness.wait_for_override_request(replacement_tool)
        harness.wait_for_decision("explicit_request", replacement_tool)
        harness.wait_for_skill_event("ToolHandoverCompleted", replacement_tool)

        result = ProbeResult(
            bundle=args.spec_name,
            implicit_request_tool_id=tool_id,
            initial_preposition_tool_id=prepositioned_tool,
            replacement_preposition_tool_id=replacement_tool,
            explicit_request_tool_id=replacement_tool,
            visual_evidence_did_not_create_explicit_request=True,
            implicit_handover_completed=True,
            initial_preposition_completed=True,
            prediction_evidence_withdrawal_returned=True,
            wrong_preposition_returned=True,
            replacement_preposition_completed=True,
            explicit_handover_completed=True,
            implicit_request_to_handover_sec=round(implicit_handover_sec, 3),
            prediction_to_preposition_sec=round(
                prediction_to_preposition_sec,
                3,
            ),
            prediction_evidence_withdrawal_release_sec=round(
                prediction_evidence_withdrawal_release_sec,
                3,
            ),
            wrong_preposition_release_sec=round(
                prediction_evidence_withdrawal_release_sec,
                3,
            ),
            replacement_prediction_to_preposition_sec=round(
                replacement_prediction_to_preposition_sec,
                3,
            ),
            invariant_violation_count=len(harness._world_invariant_violations),
        )
        print(json.dumps(asdict(result), indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"Manual probe failed: {exc}", file=sys.stderr)
        if harness._surgeon_request_log:
            print("\nRecent surgeon requests:", file=sys.stderr)
            for _, event_type, requested_tool in harness._surgeon_request_log[-8:]:
                print(f"  - {event_type}:{requested_tool}", file=sys.stderr)
        if harness._decision_log:
            print("\nRecent BT decisions:", file=sys.stderr)
            for record in harness._decision_log[-12:]:
                print(
                    f"  - decision={record.decision} tool={record.selected_tool} action={record.action}",
                    file=sys.stderr,
                )
        if harness._event_log:
            print("\nRecent skill events:", file=sys.stderr)
            for event in harness._event_log[-12:]:
                print(
                    f"  - event={event.event_type} tool={event.instrument_id} "
                    f"instance={event.instance_id or 'none'} "
                    f"target={event.target_location_id or event.location_id}",
                    file=sys.stderr,
                )
        if harness._skill_statuses:
            print("\nRecent skill statuses:", file=sys.stderr)
            for action, state, success, message in harness._skill_statuses[-16:]:
                print(
                    f"  - action={action} state={state} "
                    f"success={success} message={message}",
                    file=sys.stderr,
                )
        if harness._latest_world is not None:
            print("\nLatest world summary:", file=sys.stderr)
            print(
                f"  - phase={harness._latest_world.filtered_phase} "
                f"state={harness._latest_world.execution_state} "
                f"uncertain={harness._latest_world.phase_uncertain} "
                f"expected={list(harness._latest_world.expected_instruments)} "
                f"prepositioned={harness._latest_world.prepositioned_tool} "
                f"surgeon_request={harness._latest_world.surgeon_request_tool} "
                f"active_task={harness._latest_world.active_robot_task_id}",
                file=sys.stderr,
            )
            for instrument in harness._latest_world.instrument_states:
                print(
                    "  - tool="
                    f"{instrument.instrument_id} lifecycle={instrument.lifecycle_stage} "
                    f"loc=({instrument.location_type},{instrument.location_id}) "
                    f"owner={instrument.owner} contaminated={instrument.contaminated} "
                    f"next={instrument.next_required_transition}",
                    file=sys.stderr,
                )
        if runtime.log_path:
            print("\nRuntime log tail:", file=sys.stderr)
            print(runtime.tail(), file=sys.stderr)
        return 1
    finally:
        try:
            harness.control("stop")
        except Exception:
            pass
        harness.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
        runtime.stop()


if __name__ == "__main__":
    raise SystemExit(main())
