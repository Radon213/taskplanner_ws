"""Long-run BT audit for taskplanner bundles."""

from __future__ import annotations

import argparse
import copy
from collections import deque
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import sys
import time

from procedure_spec import get_default_spec_dir
import rclpy
from surgical_msgs.msg import BTDecision, SurgeonActorEvent, SurgeonGestureEvidence, SurgeonRequest, WorldState

from .smoke_test import ManagedProcess, SmokeHarness


def _stamp_to_sec(stamp) -> float:
    return float(stamp.sec) + float(stamp.nanosec) / 1_000_000_000.0


def _recoverable_tools(world: WorldState) -> list[str]:
    return [
        instrument.instrument_id
        for instrument in world.instrument_states
        if instrument.lifecycle_stage
        in {
            "surgeon_owned",
            "mayo_recovery",
            "recovering_left",
            "cleaning_left",
            "cleaned_left",
        }
    ]


def _unused_preposition_tools(world: WorldState) -> list[str]:
    return [
        instrument.instrument_id
        for instrument in world.instrument_states
        if instrument.lifecycle_stage == "prepositioned_right"
        or (instrument.instrument_id == world.right_hand_tool and instrument.owner == "robot_right_hand")
    ]


def _active_recovery_context(world: WorldState) -> tuple[bool, bool, list[str]]:
    recoverable = _recoverable_tools(world)
    unused_preposition = _unused_preposition_tools(world)
    pending_recovery = bool(
        world.surgeon_ready_for_retrieval
        or world.surgeon_intent in {"return_tool", "extend_hand_for_retrieval"}
        or world.active_recovery_tools
    )
    pipeline_active = bool(
        world.left_hand_tool
        or world.cleaner_busy
        or any(
            instrument.next_required_transition in {"recover_left", "clean_left", "return_home"}
            for instrument in world.instrument_states
        )
        or unused_preposition
    )
    return pending_recovery, pipeline_active, recoverable


@dataclass
class AuditFinding:
    severity: str
    code: str
    bundle: str
    decision: str
    selected_tool: str
    phase: str
    surgeon_intent: str
    rationale: str
    detail: str
    timestamp_sec: float


class BTAuditHarness(SmokeHarness):
    def __init__(self) -> None:
        super().__init__()
        self._bundle_name = ""
        self._gesture_log: list[tuple[float, SurgeonGestureEvidence]] = []
        self._request_log: list[tuple[float, SurgeonRequest]] = []
        self._actor_event_log: list[tuple[float, SurgeonActorEvent]] = []
        self._world_log: deque[tuple[float, WorldState]] = deque(maxlen=240)
        self._findings: list[AuditFinding] = []
        self._decision_count: dict[str, int] = {}
        self._observation_violation_detected = False
        self._observation_violation_samples: list[dict[str, object]] = []
        self._vlm_rejected_proposal_detected = False
        self._vlm_rejection_samples: list[dict[str, object]] = []
        self._last_observed_phase = ""
        self.create_subscription(
            SurgeonGestureEvidence,
            "/vlm/surgeon_gesture_evidence",
            self._on_gesture,
            20,
        )
        self.create_subscription(
            SurgeonActorEvent,
            "/surgeon/actor_event",
            self._on_actor_event,
            20,
        )

    def reset_run(self, bundle_name: str) -> None:
        self._bundle_name = bundle_name
        self._decision_records.clear()
        self._decision_set.clear()
        self._seen_decision_names.clear()
        self._skill_commands.clear()
        self._skill_command_set.clear()
        self._skill_statuses.clear()
        self._skill_status_set.clear()
        self._skill_event_types.clear()
        self._skill_event_records.clear()
        self._cleaned_tools.clear()
        self._returned_tools.clear()
        self._surgeon_requests.clear()
        self._latest_world = None
        self._world_invariant_violations.clear()
        self._gesture_log.clear()
        self._request_log.clear()
        self._actor_event_log.clear()
        self._world_log.clear()
        self._findings.clear()
        self._decision_count.clear()
        self._observation_violation_detected = False
        self._observation_violation_samples.clear()
        self._vlm_rejected_proposal_detected = False
        self._vlm_rejection_samples.clear()
        self._last_observed_phase = ""

    def _on_gesture(self, msg: SurgeonGestureEvidence) -> None:
        self._gesture_log.append((_stamp_to_sec(msg.stamp), msg))
        self._gesture_log = self._gesture_log[-60:]

    def _on_surgeon_request(self, msg):  # type: ignore[override]
        super()._on_surgeon_request(msg)
        self._request_log.append((_stamp_to_sec(msg.stamp), msg))
        self._request_log = self._request_log[-60:]

    def _on_actor_event(self, msg: SurgeonActorEvent) -> None:
        self._actor_event_log.append((_stamp_to_sec(msg.stamp), msg))
        self._actor_event_log = self._actor_event_log[-60:]

    def _on_world_state(self, msg):  # type: ignore[override]
        if self._bundle_name and msg.procedure_id and msg.procedure_id != self._bundle_name:
            # Bundle switching can leave a final state sample from the previous
            # run in the DDS queue. Do not compare phases across procedures.
            self._last_observed_phase = ""
            return
        super()._on_world_state(msg)
        self._world_log.append((_stamp_to_sec(msg.stamp), copy.deepcopy(msg)))
        previous_phase = self._last_observed_phase
        current_phase = msg.filtered_phase
        self._last_observed_phase = current_phase
        if previous_phase and current_phase and previous_phase != current_phase:
            world_sec = _stamp_to_sec(msg.stamp)
            has_actor_phase_event = any(
                actor_msg.event_type in {"advance_phase", "advance_phase_cue"}
                and actor_msg.phase_id == current_phase
                and abs(world_sec - actor_sec) <= 20.0
                for actor_sec, actor_msg in reversed(self._actor_event_log)
            )
            if not has_actor_phase_event:
                self._world_invariant_violations.append(
                    f"phase changed without surgeon actor advance event: {previous_phase}->{current_phase}"
                )

    def _on_decision(self, msg: BTDecision) -> None:  # type: ignore[override]
        super()._on_decision(msg)
        self._decision_count[msg.decision] = self._decision_count.get(msg.decision, 0) + 1
        self._audit_decision(msg)

    def _world_for_decision(self, decision: BTDecision) -> WorldState | None:
        if not self._world_log:
            return self._latest_world
        target_sec = _stamp_to_sec(decision.stamp)
        best_world: WorldState | None = None
        best_diff = float("inf")
        for stamp_sec, world in reversed(self._world_log):
            diff = abs(target_sec - stamp_sec)
            if diff < best_diff:
                best_diff = diff
                best_world = world
            if stamp_sec < target_sec - 1.5:
                break
        if best_world is not None and best_diff <= 1.5:
            return best_world
        return self._latest_world

    def _recent_request(self, world: WorldState) -> tuple[float, SurgeonRequest] | None:
        now_sec = _stamp_to_sec(world.stamp)
        for stamp_sec, request in reversed(self._request_log):
            if now_sec - stamp_sec <= 4.0:
                return (stamp_sec, request)
        return None

    def _recent_gesture(self, world: WorldState) -> SurgeonGestureEvidence | None:
        now_sec = _stamp_to_sec(world.stamp)
        for stamp_sec, evidence in reversed(self._gesture_log):
            if now_sec - stamp_sec <= 4.0:
                return evidence
        return None

    def _push_finding(
        self,
        *,
        severity: str,
        code: str,
        decision: BTDecision,
        world: WorldState,
        detail: str,
    ) -> None:
        self._findings.append(
            AuditFinding(
                severity=severity,
                code=code,
                bundle=self._bundle_name,
                decision=decision.decision,
                selected_tool=decision.selected_tool,
                phase=world.filtered_phase,
                surgeon_intent=world.surgeon_intent,
                rationale=decision.rationale,
                detail=detail,
                timestamp_sec=_stamp_to_sec(decision.stamp),
            )
        )

    def _audit_decision(self, decision: BTDecision) -> None:
        world = self._world_for_decision(decision)
        if world is None:
            return
        observation_violation_flags = {
            flag
            for flag in world.safety_flags
            if flag in {
                "illegal_observation_transition",
                "observation_direct_rebase_forbidden",
                "observation_recovery_without_return_context",
                "observation_direct_home_snap_forbidden",
            }
        }
        if observation_violation_flags or "ObservationIllegalTransitionIgnored" in set(world.recent_event_types):
            self._observation_violation_detected = True
            sample = {
                "decision": decision.decision,
                "selected_tool": decision.selected_tool,
                "phase": world.filtered_phase,
                "safety_flags": sorted(observation_violation_flags),
                "recent_events": list(world.recent_event_types),
                "timestamp_sec": _stamp_to_sec(decision.stamp),
            }
            if sample not in self._observation_violation_samples:
                self._observation_violation_samples.append(sample)
        if "VLMProposalRejected" in set(world.recent_event_types):
            self._vlm_rejected_proposal_detected = True
            sample = {
                "decision": decision.decision,
                "selected_tool": decision.selected_tool,
                "phase": world.filtered_phase,
                "recent_events": list(world.recent_event_types),
                "timestamp_sec": _stamp_to_sec(decision.stamp),
            }
            if sample not in self._vlm_rejection_samples:
                self._vlm_rejection_samples.append(sample)
        request_tuple = self._recent_request(world)
        request = request_tuple[1] if request_tuple is not None else None
        gesture = self._recent_gesture(world)
        state_by_tool = {
            instrument.instrument_id: instrument for instrument in world.instrument_states
        }
        selected_state = state_by_tool.get(decision.selected_tool)
        pending_recovery, pipeline_active, recoverable = _active_recovery_context(world)
        explicit_tool = world.explicit_request_tool or world.surgeon_request_tool

        if not decision.rationale:
            self._push_finding(
                severity="suspicious",
                code="missing_rationale",
                decision=decision,
                world=world,
                detail="BTDecision rationale was empty.",
            )

        if decision.decision == "explicit_request":
            if not explicit_tool:
                self._push_finding(
                    severity="blocker",
                    code="explicit_without_request",
                    decision=decision,
                    world=world,
                    detail="explicit_request fired without an active explicit or stabilized surgeon request.",
                )
            if explicit_tool and decision.selected_tool != explicit_tool:
                self._push_finding(
                    severity="suspicious",
                    code="explicit_tool_mismatch",
                    decision=decision,
                    world=world,
                    detail=f"selected_tool={decision.selected_tool} but active request was {explicit_tool}.",
                )
            if selected_state is not None:
                if selected_state.lifecycle_stage == "surgeon_owned":
                    if decision.action not in {"go_idle_pose", "retract_arm"}:
                        self._push_finding(
                            severity="blocker",
                            code="explicit_fulfilled_should_not_rehandover",
                            decision=decision,
                            world=world,
                            detail=f"{decision.selected_tool} was already surgeon-side but action was {decision.action}.",
                        )
                if selected_state.lifecycle_stage in {"mayo_reuse", "mayo_recovery"}:
                    if decision.action != "pick_up_from_mayo_and_handover":
                        self._push_finding(
                            severity="blocker",
                            code="mayo_request_wrong_action",
                            decision=decision,
                            world=world,
                            detail=(
                                f"{decision.selected_tool} was on Mayo but action was "
                                f"{decision.action or 'none'}."
                            ),
                        )
                if selected_state.contaminated:
                    if selected_state.lifecycle_stage not in {
                        "surgeon_owned",
                        "mayo_reuse",
                        "mayo_recovery",
                    }:
                        self._push_finding(
                            severity="blocker",
                            code="explicit_contaminated_tool",
                            decision=decision,
                            world=world,
                            detail=f"{decision.selected_tool} was contaminated during explicit handover.",
                        )
                if (
                    selected_state.lifecycle_stage
                    not in {
                        "home_rack",
                        "returned_home",
                        "prepositioned_right",
                        "surgeon_owned",
                        "mayo_reuse",
                        "mayo_recovery",
                    }
                    and world.right_hand_tool != decision.selected_tool
                ):
                    self._push_finding(
                        severity="blocker",
                        code="explicit_unavailable_tool",
                        decision=decision,
                        world=world,
                        detail=f"{decision.selected_tool} was not in an explicit-requestable lifecycle ({selected_state.lifecycle_stage}).",
                    )
                if selected_state.owner == "surgeon" or selected_state.status in {"handed_over", "in_use"}:
                    if selected_state.lifecycle_stage != "surgeon_owned":
                        self._push_finding(
                            severity="blocker",
                            code="explicit_tool_already_with_surgeon",
                            decision=decision,
                            world=world,
                            detail=f"{decision.selected_tool} was already with the surgeon.",
                        )
            if not world.handover_allowed and not bool(decision.handover_allowed):
                if selected_state is None or selected_state.lifecycle_stage != "surgeon_owned":
                    self._push_finding(
                        severity="blocker",
                        code="explicit_when_guard_blocked",
                        decision=decision,
                        world=world,
                        detail="explicit_request executed while handover was disallowed.",
                    )
        elif decision.decision == "recovery":
            recovery_context_present = bool(pending_recovery or pipeline_active or recoverable)
            if not recovery_context_present:
                self._push_finding(
                    severity="blocker",
                    code="recovery_without_recoverable_tool",
                    decision=decision,
                    world=world,
                    detail="recovery branch fired with no tool present in surgeon/return/mayo-recovery/cleaner space.",
                )
            elif (
                decision.selected_tool
                and decision.next_required_transition
                not in {"recover_left", "clean_left", "return_home", "return_unused_preposition"}
                and decision.selected_tool_lifecycle
                not in {
                    "surgeon_owned",
                    "mayo_recovery",
                    "recovering_left",
                    "cleaning_left",
                    "cleaned_left",
                }
                and decision.selected_tool not in recoverable
            ):
                self._push_finding(
                    severity="suspicious",
                    code="recovery_tool_mismatch",
                    decision=decision,
                    world=world,
                    detail=f"selected_tool={decision.selected_tool} but recoverable tools were {recoverable}.",
                )
        elif decision.decision == "anticipatory_handover":
            recent_explicit_request = bool(
                request_tuple is not None
                and request is not None
                and request.event_type in {"request_tool", "voice_request", "extend_hand_for_handover"}
                and request.requested_tool == decision.selected_tool
                and (_stamp_to_sec(world.stamp) - request_tuple[0]) <= 4.0
            )
            explicit_conflict = bool(
                explicit_tool
                and (
                    decision.selected_tool != explicit_tool
                    or (world.surgeon_ready_for_handover and not recent_explicit_request)
                )
            )
            if explicit_conflict or pending_recovery:
                self._push_finding(
                    severity="suspicious",
                    code="anticipatory_with_stronger_branch",
                    decision=decision,
                    world=world,
                    detail=(
                        f"anticipatory branch won while explicit_tool={explicit_tool or 'none'} "
                        f"and pending_recovery={pending_recovery} recoverable={recoverable} "
                        f"selected_tool={decision.selected_tool} handover_ready={world.surgeon_ready_for_handover}."
                    ),
                )
            if world.phase_uncertain or not world.handover_allowed:
                self._push_finding(
                    severity="blocker",
                    code="anticipatory_under_guard_block",
                    decision=decision,
                    world=world,
                    detail="anticipatory branch fired while phase was uncertain or handover guard was blocked.",
                )
            if decision.selected_tool not in world.expected_instruments:
                self._push_finding(
                    severity="suspicious",
                    code="anticipatory_unexpected_tool",
                    decision=decision,
                    world=world,
                    detail=(
                        f"selected_tool={decision.selected_tool} not in expected instruments "
                        f"{world.expected_instruments}."
                    ),
                )
            if selected_state is not None and selected_state.contaminated:
                self._push_finding(
                    severity="blocker",
                    code="anticipatory_contaminated_tool",
                    decision=decision,
                    world=world,
                    detail=f"{decision.selected_tool} was contaminated during anticipatory selection.",
                )
            if selected_state is not None and selected_state.lifecycle_stage not in {"home_rack", "returned_home", "prepositioned_right"}:
                self._push_finding(
                    severity="blocker",
                    code="anticipatory_invalid_lifecycle",
                    decision=decision,
                    world=world,
                    detail=f"anticipatory selected {decision.selected_tool} in lifecycle {selected_state.lifecycle_stage}.",
                )
        elif decision.decision == "hold":
            if not world.phase_uncertain and world.handover_allowed and not explicit_tool and not recoverable:
                self._push_finding(
                    severity="suspicious",
                    code="hold_without_guard_reason",
                    decision=decision,
                    world=world,
                    detail="hold/retract_arm fired without uncertainty or another stronger guard reason.",
                )
        elif decision.decision == "idle":
            if explicit_tool or pending_recovery or world.pending_transition_tools:
                self._push_finding(
                    severity="suspicious",
                    code="idle_while_request_pending",
                    decision=decision,
                    world=world,
                    detail=(
                        f"idle branch fired while explicit_tool={explicit_tool or 'none'} "
                        f"and pending_recovery={pending_recovery} recoverable={recoverable} "
                        f"pending_transition_tools={list(world.pending_transition_tools)}."
                    ),
                )

        if request is not None and request.event_type == "cancel_request" and decision.decision == "explicit_request":
            if world.surgeon_intent in {"request_tool", "voice_request", "extend_hand_for_handover"}:
                return
            request_sec = request_tuple[0] if request_tuple is not None else 0.0
            decision_sec = _stamp_to_sec(decision.stamp)
            actor_request_after_cancel = any(
                actor_msg.event_type in {"request_tool", "voice_request"}
                and actor_msg.tool_id == decision.selected_tool
                and decision_sec - actor_sec <= 4.0
                for actor_sec, actor_msg in reversed(self._actor_event_log)
            )
            if actor_request_after_cancel:
                return
            self._push_finding(
                severity="suspicious",
                code="explicit_after_cancel",
                decision=decision,
                world=world,
                detail="explicit_request fired immediately after a cancel_request transition.",
            )

        if (
            gesture is not None
            and gesture.event_type == "return_tool"
            and decision.decision == "anticipatory_handover"
            and pending_recovery
        ):
            self._push_finding(
                severity="suspicious",
                code="anticipatory_during_return_cue",
                decision=decision,
                world=world,
                detail="anticipatory handover fired while a recent strong return cue was still active.",
            )

    def wait_for_running(self, timeout_sec: float = 15.0) -> None:
        self.wait_until(
            lambda: self._latest_world is not None and self._latest_world.procedure_id and self._latest_world.robot_state != "",
            timeout_sec,
            "world state to become available",
        )
        self.wait_until(
            lambda: self._latest_world is not None and self._latest_world.filtered_phase != "",
            timeout_sec,
            "world phase to become available",
        )

    def spin_for(self, duration_sec: float) -> None:
        deadline = time.time() + duration_sec
        while time.time() < deadline:
            rclpy.spin_once(self, timeout_sec=0.2)

    def report_for(self, duration_sec: float) -> dict[str, object]:
        blocker_count = sum(1 for finding in self._findings if finding.severity == "blocker")
        suspicious_count = sum(1 for finding in self._findings if finding.severity == "suspicious")
        return {
            "bundle": self._bundle_name,
            "duration_sec": duration_sec,
            "decision_counts": dict(self._decision_count),
            "world_invariant_violations": sorted(set(self._world_invariant_violations)),
            "observation_violation_detected": self._observation_violation_detected,
            "observation_violation_samples": self._observation_violation_samples[:5],
            "vlm_rejected_proposal_detected": self._vlm_rejected_proposal_detected,
            "vlm_rejection_samples": self._vlm_rejection_samples[:5],
            "findings": [asdict(finding) for finding in self._findings],
            "blocker_count": blocker_count + len(set(self._world_invariant_violations)),
            "suspicious_count": suspicious_count,
            "recoverable_events": sorted(self._skill_event_types),
        }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--duration-sec",
        type=float,
        default=310.0,
        help="Per-bundle real-time audit duration in seconds.",
    )
    parser.add_argument(
        "--report-dir",
        default="/home/arl/taskplanner_ws/reports",
        help="Directory where JSON audit reports should be written.",
    )
    parser.add_argument("--vlm-mode", default="mock", choices=["mock", "real", "dual"])
    parser.add_argument("--vlm-response-mode", default="live")
    parser.add_argument("--vlm-base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--vlm-model-id", default="unsloth/gemma-4-E4B-it-NVFP4")
    parser.add_argument(
        "--spec-name",
        default="all",
        choices=["all", "thyroidectomy", "nephrectomy"],
        help="Run a single bundle audit or all bundles.",
    )
    return parser.parse_args(argv)


def _write_report(report_dir: Path, bundle_name: str, report: dict[str, object]) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"bt_audit_{bundle_name}.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return report_path


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    default_spec = get_default_spec_dir()
    runtime = ManagedProcess(
        name="taskplanner_audit_runtime",
        command=[
            "ros2",
            "launch",
            "bringup",
            "taskplanner_mock.launch.py",
            f"spec_dir:={default_spec}",
            "enable_rosbridge:=false",
            f"vlm_mode:={args.vlm_mode}",
            f"vlm_response_mode:={args.vlm_response_mode}",
            f"vlm_base_url:={args.vlm_base_url}",
            f"vlm_model_id:={args.vlm_model_id}",
        ],
    )
    bundles = ["thyroidectomy", "nephrectomy"] if args.spec_name == "all" else [args.spec_name]

    rclpy.init()
    harness = BTAuditHarness()
    try:
        runtime.start()
        harness.wait_for_services()
        harness.wait_for_catalog_entry()
        report_paths: list[Path] = []

        for bundle_name in bundles:
            harness.reset_run(bundle_name)
            harness.select_bundle(bundle_name)
            time.sleep(1.0)
            harness.control("start")
            harness.wait_for_running(timeout_sec=18.0)
            harness.spin_for(args.duration_sec)
            harness.control("stop")
            harness.spin_for(3.0)
            report = harness.report_for(args.duration_sec)
            report_paths.append(_write_report(Path(args.report_dir), bundle_name, report))

        blocker_total = 0
        suspicious_total = 0
        for path in report_paths:
            report = json.loads(path.read_text(encoding="utf-8"))
            blocker_total += int(report["blocker_count"])
            suspicious_total += int(report["suspicious_count"])
            print(f"{path.name}: blockers={report['blocker_count']} suspicious={report['suspicious_count']}")

        if blocker_total > 0 or suspicious_total > 0:
            print(
                f"BT audit failed: blockers={blocker_total}, suspicious={suspicious_total}",
                file=sys.stderr,
            )
            return 1
        print("BT audit passed.")
        return 0
    except Exception as exc:
        print(f"BT audit failed: {exc}", file=sys.stderr)
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
