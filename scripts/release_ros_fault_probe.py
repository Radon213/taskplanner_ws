#!/usr/bin/env python3
"""Exercise source degradation, voice-only fallback, and robot admission on ROS.

The probe intentionally starts only deterministic test nodes in an isolated ROS
domain. It does not load a VLM, open a video, or talk to a physical robot.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass
from io import BytesIO
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
from typing import Any, Callable

from PIL import Image
import rclpy
from rclpy.action import ActionClient
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from sensor_msgs.msg import CompressedImage
from std_msgs.msg import String
from surgical_interop_msgs.action import (
    ExecuteToolHandover,
)
from surgical_interop_msgs.msg import BedRobotArmStateArray
from surgical_interop_msgs.srv import ExecuteRetractionCommand
from surgical_msgs.msg import (
    InputSourceStatus,
    ReducerDecisionEvent,
    VLMHealth,
    VLMResult,
    WorldState,
)


ROOT = Path(__file__).resolve().parents[1]
SPEC_DIR = (
    ROOT
    / "src"
    / "procedure_spec"
    / "procedure_spec"
    / "specs"
    / "thyroidectomy_demo"
)
SCENARIO = ROOT / "config" / "fault_scenarios" / "release_smoke.yaml"
ROBOT_CONTRACT_PROFILE = (
    ROOT / "config" / "fault_scenarios" / "action_mixed_failures.yaml"
)


@dataclass(frozen=True)
class AssertionResult:
    name: str
    passed: bool
    detail: str


@dataclass
class ChildProcess:
    name: str
    process: subprocess.Popen[str]
    log_stream: Any
    log_path: Path


def _wait_future(future, timeout_sec: float):
    deadline = time.monotonic() + timeout_sec
    while not future.done() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not future.done():
        raise TimeoutError(f"ROS future did not complete within {timeout_sec:.1f}s")
    return future.result()


def _jpeg_bytes() -> bytes:
    image = Image.new("RGB", (96, 64), (24, 48, 72))
    output = BytesIO()
    image.save(output, format="JPEG", quality=82)
    return output.getvalue()


class ReleaseProbeNode(Node):
    def __init__(self, started_monotonic: float) -> None:
        super().__init__("taskplanner_release_ros_fault_probe")
        self.started_monotonic = started_monotonic
        self.source_history: dict[str, list[dict[str, Any]]] = {
            source: [] for source in ("flir", "cam4", "vlm", "speech")
        }
        self.world_history: list[dict[str, Any]] = []
        self.reducer_history: list[dict[str, Any]] = []
        self.fault_reports: list[dict[str, Any]] = []
        self.bed_robot_status_history: list[dict[str, Any]] = []
        self._jpeg = _jpeg_bytes()
        self._sequence = 0
        self._procedure_start_sent = False
        self._procedure_start_sent_at = 0.0
        self._explicit_request_sent = False
        self._explicit_request_sent_at = 0.0

        self.raw_flir_pub = self.create_publisher(
            CompressedImage, "/test/fault/raw/flir/compressed", 10
        )
        self.raw_cam4_pub = self.create_publisher(
            CompressedImage, "/test/fault/raw/cam4/compressed", 10
        )
        self.raw_vlm_result_pub = self.create_publisher(
            VLMResult, "/test/fault/raw/vlm/result", 10
        )
        self.raw_vlm_health_pub = self.create_publisher(
            VLMHealth, "/test/fault/raw/vlm/health", 10
        )
        self.raw_sentence_pub = self.create_publisher(
            String, "/test/fault/raw/speech/sentence", 10
        )
        self.control_pub = self.create_publisher(
            String, "/simulation/control_state", 10
        )

        for source in self.source_history:
            self.create_subscription(
                InputSourceStatus,
                f"/input/{source}/status",
                self._source_callback(source),
                20,
            )
        self.create_subscription(
            WorldState, "/twin/world_state", self._on_world, 20
        )
        self.create_subscription(
            ReducerDecisionEvent,
            "/twin/reducer_decisions",
            self._on_reducer,
            50,
        )
        self.create_subscription(
            String, "/test/fault/status", self._on_fault_report, 10
        )
        self.action_client = ActionClient(
            self, ExecuteToolHandover, "/surgery/tool_handover"
        )
        self.retraction_client = self.create_client(
            ExecuteRetractionCommand,
            "/surgery/retraction/command",
        )
        self.create_subscription(
            BedRobotArmStateArray,
            "/external/bed_robot_arms/status",
            self._on_bed_robot_status,
            20,
        )

    def elapsed(self) -> float:
        return time.monotonic() - self.started_monotonic

    def _source_callback(self, source: str) -> Callable[[InputSourceStatus], None]:
        def callback(message: InputSourceStatus) -> None:
            self.source_history[source].append(
                {
                    "at_sec": round(self.elapsed(), 4),
                    "state": str(message.state),
                    "healthy": bool(message.healthy),
                    "age_sec": round(float(message.age_sec), 4),
                    "epoch": int(message.epoch),
                    "received": int(message.received_count),
                    "accepted": int(message.accepted_count),
                    "rejected": int(message.rejected_count),
                    "dropped": int(message.dropped_count),
                    "error_code": str(message.error_code),
                    "detail": str(message.detail),
                }
            )

        return callback

    def _on_world(self, message: WorldState) -> None:
        self.world_history.append(
            {
                "at_sec": round(self.elapsed(), 4),
                "running": bool(message.running),
                "execution_state": str(message.execution_state),
                "surgeon_request_tool": str(message.surgeon_request_tool),
                "implicit_request_visible": bool(message.implicit_request_visible),
                "implicit_request_tool": str(message.implicit_request_tool),
                "safety_flags": list(message.safety_flags),
            }
        )

    def _on_reducer(self, message: ReducerDecisionEvent) -> None:
        self.reducer_history.append(
            {
                "at_sec": round(self.elapsed(), 4),
                "input_type": str(message.input_type),
                "input_id": str(message.input_id),
                "accepted": bool(message.accepted),
                "reason": str(message.reason),
                "affected_tool": str(message.affected_tool),
            }
        )

    def _on_fault_report(self, message: String) -> None:
        try:
            payload = json.loads(message.data)
        except json.JSONDecodeError:
            return
        if isinstance(payload, dict):
            payload["at_sec"] = round(self.elapsed(), 4)
            self.fault_reports.append(payload)

    def _on_bed_robot_status(self, message: BedRobotArmStateArray) -> None:
        self.bed_robot_status_history.append(
            {
                "at_sec": round(self.elapsed(), 4),
                "revision": int(message.revision),
                "procedure_type": str(message.procedure_type),
                "arms": [
                    {
                        "arm_id": str(arm.arm_id),
                        "role": str(arm.role),
                        "role_instance_id": str(arm.role_instance_id),
                        "state": str(arm.state),
                        "direct_teach_active": bool(arm.direct_teach_active),
                        "reason_code": str(arm.reason_code),
                    }
                    for arm in message.arms
                ],
            }
        )

    def publish_control(self, value: str) -> None:
        message = String()
        message.data = value
        self.control_pub.publish(message)

    def publish_inputs(self, fault_elapsed: float) -> None:
        now = self.get_clock().now().to_msg()
        for publisher in (self.raw_flir_pub, self.raw_cam4_pub):
            image = CompressedImage()
            image.header.stamp = now
            image.format = "jpeg"
            image.data = self._jpeg
            publisher.publish(image)

        health = VLMHealth()
        health.stamp = now
        health.connected = True
        health.healthy = True
        health.model_id = "release-probe"
        health.image_source = "fault-probe"
        health.latency_sec = 0.01
        health.last_mode = "real"
        self.raw_vlm_health_pub.publish(health)

        self._sequence += 1
        intent = (
            ["handover", "T04", 0.99]
            if 1.95 <= fault_elapsed <= 3.05
            else ["none", "", 0.0]
        )
        result = VLMResult()
        result.stamp = now
        result.source = "real_vlm:release-probe"
        result.source_epoch = 9_000_001
        result.source_sequence = self._sequence
        result.correlation_id = f"release-vlm-9000001-{self._sequence}"
        result.schema_version = "4"
        result.raw_json = json.dumps(
            {
                "v": "4",
                "phase": [["P03", 0.91]],
                "tool": [],
                "intent": intent,
                "mayo": [],
                "mayo_retrieve": ["", 0.0],
                "bed_robot_arm_group": None,
                "clinical_observation": "Stable exposed operative field.",
            },
            separators=(",", ":"),
        )
        result.summary = "Stable exposed operative field."
        result.phase_ids = ["P03"]
        result.phase_confidences = [0.91]
        result.gesture_event_type = str(intent[0])
        result.gesture_requested_tool = str(intent[1])
        result.gesture_hand_pose = "open_palm" if intent[0] == "handover" else ""
        result.gesture_confidence = float(intent[2])
        self.raw_vlm_result_pub.publish(result)

        if fault_elapsed >= 0.45 and not self._procedure_start_sent:
            sentence = String()
            sentence.data = "갑상선 절제술 시작하자"
            self.raw_sentence_pub.publish(sentence)
            self._procedure_start_sent = True
            self._procedure_start_sent_at = self.elapsed()
        if fault_elapsed >= 2.15 and not self._explicit_request_sent:
            sentence = String()
            sentence.data = "Adsen forceps please"
            self.raw_sentence_pub.publish(sentence)
            self._explicit_request_sent = True
            self._explicit_request_sent_at = self.elapsed()

    def run_action_contract(self) -> list[dict[str, Any]]:
        if not self.action_client.wait_for_server(timeout_sec=8.0):
            raise RuntimeError("/surgery/tool_handover Action server did not appear")
        rows: list[dict[str, Any]] = []

        def send(
            command_id: str,
            source: str,
            target: str,
            *,
            cancel_after_sec: float | None = None,
        ) -> dict[str, Any]:
            goal = ExecuteToolHandover.Goal()
            goal.command_id = command_id
            goal.instrument_id = "Adson forceps"
            goal.instrument_instance_id = "T02-01"
            goal.source_location = source
            goal.target_location = target
            started = time.monotonic()
            handle = _wait_future(
                self.action_client.send_goal_async(goal), timeout_sec=5.0
            )
            row: dict[str, Any] = {
                "command_id": command_id,
                "source": source,
                "target": target,
                "accepted": bool(handle.accepted),
                "goal_acceptance_latency_sec": round(
                    time.monotonic() - started,
                    6,
                ),
            }
            if not handle.accepted:
                row["duration_sec"] = round(time.monotonic() - started, 4)
                return row
            if cancel_after_sec is not None:
                time.sleep(cancel_after_sec)
                cancel_response = _wait_future(
                    handle.cancel_goal_async(), timeout_sec=5.0
                )
                row["cancel_return_code"] = int(cancel_response.return_code)
            wrapped = _wait_future(handle.get_result_async(), timeout_sec=8.0)
            row.update(
                {
                    "status": int(wrapped.status),
                    "success": bool(wrapped.result.success),
                    "final_state": str(wrapped.result.final_state),
                    "reason_code": str(wrapped.result.reason_code),
                    "duration_sec": round(time.monotonic() - started, 4),
                }
            )
            return row

        rows.append(send("release-tool-success", "tray", "robot"))
        rows.append(send("release-tool-success", "tray", "robot"))
        rows.append(send("release-tool-invalid", "surgeon", "robot"))
        rows.append(send("release-tool-partial", "tray", "surgeon"))
        rows.append(
            send(
                "release-tool-cancel-recovery",
                "robot",
                "tray",
                cancel_after_sec=0.15,
            )
        )
        return rows

    def run_bed_robot_contract(self) -> dict[str, Any]:
        if not self.retraction_client.wait_for_service(timeout_sec=8.0):
            raise RuntimeError(
                "/surgery/retraction/command Service did not appear"
            )
        deadline = time.monotonic() + 3.0
        while not self.bed_robot_status_history and time.monotonic() < deadline:
            time.sleep(0.02)
        if not self.bed_robot_status_history:
            raise RuntimeError(
                "/external/bed_robot_arms/status did not publish a snapshot"
            )

        def command(
            command_id: str,
            *,
            command_code: int,
            target_side: int,
            distance_m: float,
        ) -> dict[str, Any]:
            request = ExecuteRetractionCommand.Request()
            request.protocol_version = ExecuteRetractionCommand.Request.PROTOCOL_VERSION_V1
            request.source_id = "release_ros_fault_probe"
            request.command_id = command_id
            request.command = command_code
            request.target_side = target_side
            request.distance_m = distance_m
            started = time.monotonic()
            response = _wait_future(
                self.retraction_client.call_async(request), timeout_sec=5.0
            )
            return {
                "command_id": command_id,
                "command": int(command_code),
                "target_side": int(target_side),
                "distance_m": float(distance_m),
                "request_accepted": bool(response.request_accepted),
                "result_code": int(response.result_code),
                "response_command_id": str(response.command_id),
                "message": str(response.message),
                "duration_sec": round(time.monotonic() - started, 4),
            }

        return {
            "status_snapshot": self.bed_robot_status_history[-1],
            # The prior direction/axis and target-tool forms have no equivalent
            # fields in ExecuteRetractionCommand.  These calls intentionally
            # exercise only documented admission payloads, not motion outcome.
            "adjust_retraction_admission": command(
                "release-retraction-adjust",
                command_code=ExecuteRetractionCommand.Request.COMMAND_ADJUST_RETRACTION,
                target_side=ExecuteRetractionCommand.Request.TARGET_LEFT,
                distance_m=0.005,
            ),
            "change_tool_admission": command(
                "release-retraction-tool-change",
                command_code=ExecuteRetractionCommand.Request.COMMAND_CHANGE_TOOL,
                target_side=ExecuteRetractionCommand.Request.TARGET_NONE,
                distance_m=0.0,
            ),
        }


def _launch(
    name: str,
    command: list[str],
    *,
    environment: dict[str, str],
    logs_dir: Path,
) -> ChildProcess:
    log_path = logs_dir / f"{name}.log"
    stream = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        env=environment,
        stdout=stream,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    return ChildProcess(name, process, stream, log_path)


def _stop_children(children: list[ChildProcess]) -> None:
    for child in reversed(children):
        if child.process.poll() is None:
            try:
                os.killpg(child.process.pid, signal.SIGINT)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + 5.0
    for child in reversed(children):
        remaining = max(0.0, deadline - time.monotonic())
        if child.process.poll() is None:
            try:
                child.process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    child.process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(child.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    child.process.wait(timeout=2.0)
        child.log_stream.close()


def _states(history: list[dict[str, Any]]) -> set[str]:
    return {str(row["state"]).upper() for row in history}


def _wait_for_speech_relay_path(
    node: ReleaseProbeNode,
    *,
    timeout_sec: float = 5.0,
) -> None:
    """Wait until both sides of the one-shot sentence relay are discovered."""

    deadline = time.monotonic() + max(0.1, float(timeout_sec))
    while time.monotonic() < deadline:
        raw_relay_ready = node.raw_sentence_pub.get_subscription_count() > 0
        public_adapter_ready = bool(
            node.get_subscriptions_info_by_topic("/sensors/surgeon/sentence")
        )
        if raw_relay_ready and public_adapter_ready:
            return
        time.sleep(0.05)
    raise RuntimeError(
        "speech fault relay did not discover both raw input and public adapter"
    )


def _assertions(
    node: ReleaseProbeNode,
    action_rows: list[dict[str, Any]],
    bed_robot_results: dict[str, Any],
    child_failures: list[str],
) -> list[AssertionResult]:
    checks: list[AssertionResult] = []

    def add(name: str, passed: bool, detail: str) -> None:
        checks.append(AssertionResult(name, bool(passed), detail))

    for source in ("flir", "cam4"):
        states = _states(node.source_history[source])
        add(
            f"{source}_stale_and_recovers",
            {"STALE", "READY"}.issubset(states),
            f"states={sorted(states)}",
        )
    vlm_states = _states(node.source_history["vlm"])
    add(
        "vlm_error_and_recovers",
        {"ERROR", "READY"}.issubset(vlm_states),
        f"states={sorted(vlm_states)}",
    )
    speech_states = _states(node.source_history["speech"])
    add(
        "speech_contract_ready",
        "READY" in speech_states,
        f"states={sorted(speech_states)}",
    )
    voice_world = [
        row
        for row in node.world_history
        if row["surgeon_request_tool"] == "T02"
    ]
    pre_request_world = [
        row
        for row in node.world_history
        if node._procedure_start_sent_at <= row["at_sec"]
        < node._explicit_request_sent_at
    ]
    procedure_start_false_requests = [
        row for row in pre_request_world if row["surgeon_request_tool"]
    ]
    add(
        "procedure_start_speech_creates_no_tool_request",
        bool(pre_request_world) and not procedure_start_false_requests,
        (
            f"observed={len(pre_request_world)}, "
            f"false_requests={len(procedure_start_false_requests)}"
        ),
    )
    add(
        "voice_only_request_reaches_dt",
        bool(voice_world),
        f"T02_samples={len(voice_world)}",
    )
    voice_to_dt_latency = (
        min(row["at_sec"] for row in voice_world) - node._explicit_request_sent_at
        if voice_world
        else None
    )
    add(
        "voice_to_dt_p95_target",
        voice_to_dt_latency is not None and 0.0 <= voice_to_dt_latency <= 0.25,
        (
            "latency_sec=missing"
            if voice_to_dt_latency is None
            else f"latency_sec={voice_to_dt_latency:.6f}, threshold=0.250000"
        ),
    )
    blocked_world = [
        row
        for row in node.world_history
        if "vlm_unhealthy" in row["safety_flags"]
    ]
    add(
        "vlm_fault_sets_fail_closed_flag",
        bool(blocked_world),
        f"blocked_samples={len(blocked_world)}",
    )
    leaked_visual = [
        row
        for row in blocked_world
        if row["implicit_request_visible"]
        or row["implicit_request_tool"] == "T04"
    ]
    add(
        "unhealthy_visual_evidence_not_promoted",
        not leaked_visual,
        f"leaked_samples={len(leaked_visual)}",
    )
    correlations = {
        row["input_id"]
        for row in node.reducer_history
        if str(row["input_id"]).startswith("release-vlm-")
    }
    add(
        "vlm_correlation_ids_reach_reducer",
        bool(correlations),
        f"correlation_ids={len(correlations)}",
    )

    success, replay, invalid, partial, cancel = action_rows
    add(
        "action_success",
        success.get("accepted") is True
        and success.get("success") is True
        and success.get("final_state") == "completed",
        json.dumps(success, sort_keys=True),
    )
    add(
        "action_goal_acceptance_p95_target",
        float(success.get("goal_acceptance_latency_sec", 999.0)) <= 0.1,
        (
            f"latency_sec={success.get('goal_acceptance_latency_sec')}, "
            "threshold=0.100000"
        ),
    )
    add(
        "action_command_id_idempotent",
        replay.get("accepted") is True
        and replay.get("success") is True
        and replay.get("reason_code") == success.get("reason_code"),
        json.dumps(replay, sort_keys=True),
    )
    add(
        "action_invalid_transition_rejected",
        invalid.get("accepted") is False,
        json.dumps(invalid, sort_keys=True),
    )
    add(
        "action_partial_failure_aborts",
        partial.get("accepted") is True
        and partial.get("success") is False
        and partial.get("final_state") == "failed",
        json.dumps(partial, sort_keys=True),
    )
    add(
        "ambiguous_cancel_recovery_fails_closed",
        cancel.get("accepted") is True
        and cancel.get("success") is False
        and cancel.get("reason_code") == "cancel_recovery_failed",
        json.dumps(cancel, sort_keys=True),
    )

    status_snapshot = bed_robot_results.get("status_snapshot", {})
    arms = status_snapshot.get("arms", [])
    add(
        "bed_robot_status_matches_nephrectomy_contract",
        status_snapshot.get("procedure_type") == "nephrectomy"
        and [row.get("arm_id") for row in arms] == ["arm_1", "arm_2"]
        and {row.get("role_instance_id") for row in arms}
        == {"left_malleable", "right_malleable"}
        and all(row.get("role") == "retraction" for row in arms),
        json.dumps(status_snapshot, sort_keys=True),
    )
    retraction_rejected = bed_robot_results.get("adjust_retraction_admission", {})
    retraction_accepted = bed_robot_results.get("change_tool_admission", {})
    add(
        "retraction_command_rejection_is_admission_only",
        retraction_rejected.get("request_accepted") is False
        and retraction_rejected.get("result_code")
        == ExecuteRetractionCommand.Response.RESULT_REJECTED
        and retraction_rejected.get("response_command_id")
        == retraction_rejected.get("command_id"),
        json.dumps(retraction_rejected, sort_keys=True),
    )
    add(
        "retraction_command_acceptance_is_admission_only",
        retraction_accepted.get("request_accepted") is True
        and retraction_accepted.get("result_code")
        == ExecuteRetractionCommand.Response.RESULT_ACCEPTED
        and retraction_accepted.get("response_command_id")
        == retraction_accepted.get("command_id"),
        json.dumps(retraction_accepted, sort_keys=True),
    )
    add(
        "all_ros_children_survived_campaign",
        not child_failures,
        f"unexpected_exits={child_failures}",
    )
    return checks


def _write_reports(
    output_dir: Path,
    node: ReleaseProbeNode,
    assertions: list[AssertionResult],
    action_rows: list[dict[str, Any]],
    bed_robot_results: dict[str, Any],
    child_failures: list[str],
    domain_id: int,
) -> None:
    passed = all(item.passed for item in assertions)
    payload = {
        "schema": "taskplanner.ros_fault_probe.v1",
        "status": "passed" if passed else "failed",
        "ros_domain_id": domain_id,
        "models_loaded": False,
        "scenario": str(SCENARIO.relative_to(ROOT)),
        "robot_contract_profile": str(ROBOT_CONTRACT_PROFILE.relative_to(ROOT)),
        "assertions": [asdict(item) for item in assertions],
        "source_history": node.source_history,
        "world_history": node.world_history,
        "reducer_history": node.reducer_history,
        "fault_reports": node.fault_reports,
        "action_results": action_rows,
        "bed_robot_contract_results": bed_robot_results,
        "bed_robot_status_history": node.bed_robot_status_history,
        "unexpected_child_exits": child_failures,
        "procedure_start_false_tool_actions": sum(
            1
            for row in node.world_history
            if node._procedure_start_sent_at <= row["at_sec"]
            < node._explicit_request_sent_at
            and row["surgeon_request_tool"]
        ),
    }
    (output_dir / "ros_fault_probe.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    with (output_dir / "assertions.csv").open(
        "w", encoding="utf-8", newline=""
    ) as stream:
        writer = csv.DictWriter(stream, fieldnames=["name", "passed", "detail"])
        writer.writeheader()
        for item in assertions:
            writer.writerow(asdict(item))

    width = 1120
    height = 90 + len(assertions) * 34
    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        '<text x="24" y="34" font-family="sans-serif" font-size="22" font-weight="700">ROS fault and robot admission probe</text>',
        f'<text x="24" y="58" font-family="sans-serif" font-size="13">domain {domain_id} | models loaded: false</text>',
    ]
    for index, item in enumerate(assertions):
        y = 90 + index * 34
        color = "#11825b" if item.passed else "#c83d4b"
        label = item.name.replace("&", "and").replace("<", "[").replace(">", "]")
        svg.extend(
            [
                f'<rect x="24" y="{y - 17}" width="14" height="14" rx="2" fill="{color}"/>',
                f'<text x="48" y="{y - 5}" font-family="sans-serif" font-size="14">{label}</text>',
            ]
        )
    svg.append("</svg>")
    (output_dir / "ros_fault_probe.svg").write_text(
        "\n".join(svg) + "\n", encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--ros-domain-id", type=int, default=197)
    parser.add_argument("--duration-sec", type=float, default=5.8)
    args = parser.parse_args()
    if not 0 <= args.ros_domain_id <= 232:
        raise SystemExit("ROS domain id must be between 0 and 232")

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    logs_dir = output_dir / "logs"
    logs_dir.mkdir()
    environment = {
        **os.environ,
        "ROS_DOMAIN_ID": str(args.ros_domain_id),
        "ROS_AUTOMATIC_DISCOVERY_RANGE": "LOCALHOST",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    os.environ.update(
        {
            "ROS_DOMAIN_ID": str(args.ros_domain_id),
            "ROS_AUTOMATIC_DISCOVERY_RANGE": "LOCALHOST",
        }
    )
    started = time.monotonic()
    children: list[ChildProcess] = []
    node: ReleaseProbeNode | None = None
    executor: MultiThreadedExecutor | None = None
    spin_thread: threading.Thread | None = None
    action_rows: list[dict[str, Any]] = []
    bed_robot_results: dict[str, Any] = {}
    child_failures: list[str] = []
    assertions: list[AssertionResult] = []

    rclpy.init()
    try:
        node = ReleaseProbeNode(started)
        executor = MultiThreadedExecutor(num_threads=4)
        executor.add_node(node)
        spin_thread = threading.Thread(target=executor.spin, daemon=True)
        spin_thread.start()

        children.extend(
            [
                _launch(
                    "source_health_monitor",
                    [
                        "ros2",
                        "run",
                        "simulation_runtime",
                        "source_health_monitor",
                        "--ros-args",
                        "-p",
                        "camera_stale_after_sec:=0.35",
                        "-p",
                        "vlm_stale_after_sec:=0.6",
                        "-p",
                        "recovery_samples:=2",
                    ],
                    environment=environment,
                    logs_dir=logs_dir,
                ),
                _launch(
                    "speech_input_adapter",
                    [
                        "ros2",
                        "run",
                        "simulation_runtime",
                        "speech_input_adapter",
                        "--ros-args",
                        "-p",
                        "input_mode:=sentence_text",
                        "-p",
                        "sentence_source_id:=release_probe_sentence",
                        "-p",
                        "sentence_dedupe_sec:=0.15",
                        "-p",
                        "source_timeout_sec:=2.0",
                    ],
                    environment=environment,
                    logs_dir=logs_dir,
                ),
                _launch(
                    "or_digital_twin",
                    [
                        "ros2",
                        "run",
                        "or_digital_twin",
                        "or_digital_twin",
                        "--ros-args",
                        "-p",
                        f"spec_dir:={SPEC_DIR}",
                        "-p",
                        "vlm_mode:=real",
                        "-p",
                        "vlm_health_timeout_sec:=0.6",
                        "-p",
                        "vlm_implicit_request_stability_sec:=0.35",
                    ],
                    environment=environment,
                    logs_dir=logs_dir,
                ),
                _launch(
                    "fault_action_emulator",
                    [
                        "ros2",
                        "run",
                        "surgical_interop_execution",
                        "fault_action_emulator",
                        "--ros-args",
                        "-p",
                        f"profile_path:={ROBOT_CONTRACT_PROFILE}",
                        "-p",
                        "procedure_type:=nephrectomy",
                    ],
                    environment=environment,
                    logs_dir=logs_dir,
                ),
                _launch(
                    "fault_injector",
                    [
                        "ros2",
                        "run",
                        "simulation_runtime",
                        "fault_injector",
                        "--ros-args",
                        "-p",
                        "enabled:=true",
                        "-p",
                        f"scenario_path:={SCENARIO}",
                    ],
                    environment=environment,
                    logs_dir=logs_dir,
                ),
            ]
        )
        _wait_for_speech_relay_path(node)
        node.publish_control("start_runtime:P03")
        time.sleep(0.3)
        injector_started = time.monotonic()

        while time.monotonic() - injector_started < args.duration_sec:
            fault_elapsed = time.monotonic() - injector_started
            node.publish_inputs(fault_elapsed)
            for child in children:
                if child.process.poll() is not None and child.name not in child_failures:
                    child_failures.append(child.name)
            time.sleep(0.05)
        time.sleep(0.7)
        action_rows = node.run_action_contract()
        bed_robot_results = node.run_bed_robot_contract()
        assertions = _assertions(
            node,
            action_rows,
            bed_robot_results,
            child_failures,
        )
    except Exception as exc:
        assertions.append(
            AssertionResult("probe_completed_without_exception", False, repr(exc))
        )
    finally:
        if node is not None:
            node.publish_control("stop")
            time.sleep(0.1)
        _stop_children(children)
        if executor is not None:
            executor.shutdown(timeout_sec=2.0)
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
        if spin_thread is not None:
            spin_thread.join(timeout=2.0)

    if node is None:
        raise SystemExit("probe node was not created")
    _write_reports(
        output_dir,
        node,
        assertions,
        action_rows,
        bed_robot_results,
        child_failures,
        args.ros_domain_id,
    )
    for item in assertions:
        marker = "PASS" if item.passed else "FAIL"
        print(f"[{marker}] {item.name}: {item.detail}")
    return 0 if assertions and all(item.passed for item in assertions) else 1


if __name__ == "__main__":
    raise SystemExit(main())
