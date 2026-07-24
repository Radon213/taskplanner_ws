"""Run a complete thyroidectomy simulation and write a compact Korean timeline."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import re
import sys
import time
from typing import Any
from zoneinfo import ZoneInfo

from procedure_spec import get_default_spec_dir, load_bundle
import rclpy
from rclpy.node import Node
from surgical_msgs.msg import (
    SimulationEvent,
    SimulationState,
    SkillStatus,
    SurgeonActorEvent,
    TwinEvent,
    VLMHealth,
    VLMResult,
    WorldState,
)
from surgical_msgs.srv import ControlSimulation, SelectSimulationBundle


TOOL_TIMELINE_EVENTS = {
    "RobotGraspedTool",
    "ToolPrepared",
    "PredictedToolReturnedToRack",
    "ToolHandoverCompleted",
    "ToolReceivedFromSurgeon",
    "ToolSentToCleaner",
    "ToolCleaningCompleted",
    "ToolReturnedToTray",
    "RecoveryTransactionOpened",
    "RecoveryTransactionClosed",
    "RecoveryTransactionPromotedMayoReuse",
}

HUMANOID_EVENTS = {
    "RobotTaskStarted",
    "RobotTaskCompleted",
}

BED_ROBOT_EVENTS = {
    "BedRobotArmGroupRequestObserved",
    "BedRobotArmGroupProposalObserved",
    "BedRobotArmGroupCommandApproved",
    "BedRobotArmGroupCommandCompleted",
    "BedRobotArmGroupCommandRejected",
    "BedRobotArmGroupCommandCancelled",
}

TERMINAL_SKILL_FAILURE_STATES = {
    "rejected",
    "dispatch_failed",
    "server_unavailable",
    "cancel_requested",
    "canceled",
    "aborted",
    "result_failed",
}

SEOUL_TIMEZONE = ZoneInfo("Asia/Seoul")


def _stamp_to_sec(stamp) -> float:
    return float(stamp.sec) + float(stamp.nanosec) / 1_000_000_000.0


def _compact_text(value: str, limit: int = 360) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _json_mapping(value: str) -> dict[str, Any]:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {"detail": _compact_text(value)}
    return payload if isinstance(payload, dict) else {"detail": payload}


class ThyroidectomyTimelineRecorder(Node):
    def __init__(
        self,
        *,
        spec_name: str,
        vlm_model_id: str,
        actor_model_id: str,
        visual_interval_sec: float,
    ) -> None:
        super().__init__("thyroidectomy_timeline_recorder")
        spec_dir = get_default_spec_dir().parent / spec_name
        self.spec = load_bundle(spec_dir)
        self.vlm_model_id = vlm_model_id
        self.actor_model_id = actor_model_id
        self.visual_interval_sec = max(1.0, float(visual_interval_sec))

        self.phase_names = {
            phase.id: phase.display_name_ko or phase.display_name or phase.id
            for phase in self.spec.bundle.phases
        }
        self.tool_names = {
            tool.id: tool.display_name_ko or tool.display_name or tool.id
            for tool in self.spec.bundle.instruments
        }
        event_catalog = self.spec.bundle.display_catalog.get("events", {})
        self.event_names = {
            event_id: str(payload.get("display_name_ko") or payload.get("display_name") or event_id)
            for event_id, payload in event_catalog.items()
            if isinstance(payload, dict)
        }

        self.events: list[dict[str, Any]] = []
        self.latest_world: WorldState | None = None
        self.latest_simulation: SimulationState | None = None
        self.current_phase = ""
        self.recording = False
        self.started_monotonic = 0.0
        self.started_datetime: datetime | None = None
        self.completed_datetime: datetime | None = None
        self.last_visual_monotonic = -1_000_000_000.0
        self.last_visual_structure: tuple[Any, ...] | None = None
        self.seen_event_keys: set[tuple[Any, ...]] = set()
        self.vlm_results = 0
        self.vlm_healthy_samples = 0
        self.vlm_errors: list[str] = []
        self.actor_speech_count = 0

        self.create_subscription(WorldState, "/twin/world_state", self._on_world, 50)
        self.create_subscription(SimulationState, "/simulation/state", self._on_simulation, 50)
        self.create_subscription(SurgeonActorEvent, "/surgeon/actor_event", self._on_actor_event, 50)
        self.create_subscription(VLMResult, "/vlm/result", self._on_vlm_result, 50)
        self.create_subscription(VLMHealth, "/vlm/health", self._on_vlm_health, 20)
        self.create_subscription(TwinEvent, "/twin/events", self._on_twin_event, 100)
        self.create_subscription(SimulationEvent, "/twin/important_event", self._on_important_event, 100)
        self.create_subscription(SkillStatus, "/skill/status", self._on_skill_status, 100)

        self.select_client = self.create_client(
            SelectSimulationBundle,
            "/simulation/select_bundle",
        )
        self.control_client = self.create_client(ControlSimulation, "/simulation/control")

    def _elapsed(self) -> float:
        if not self.started_monotonic:
            return 0.0
        return max(0.0, time.monotonic() - self.started_monotonic)

    def _phase_name(self, phase_id: str) -> str:
        if not phase_id:
            return "단계 미확인"
        return f"{self.phase_names.get(phase_id, phase_id)} ({phase_id})"

    def _tool_name(self, tool_id: str) -> str:
        if not tool_id:
            return ""
        return f"{self.tool_names.get(tool_id, tool_id)} ({tool_id})"

    def _event_name(self, event_type: str) -> str:
        return self.event_names.get(event_type, event_type)

    def _append(
        self,
        kind: str,
        text: str,
        *,
        phase_id: str = "",
        event_key: tuple[Any, ...] | None = None,
    ) -> None:
        if not self.recording:
            return
        text = _compact_text(text, 700)
        if not text:
            return
        if event_key is not None:
            if event_key in self.seen_event_keys:
                return
            self.seen_event_keys.add(event_key)
        self.events.append(
            {
                "elapsed_sec": self._elapsed(),
                "kind": kind,
                "phase_id": phase_id or self.current_phase,
                "text": text,
            }
        )

    def _on_world(self, msg: WorldState) -> None:
        self.latest_world = msg
        if not self.recording:
            return
        next_phase = str(msg.filtered_phase or "")
        if next_phase and next_phase != self.current_phase:
            previous_phase = self.current_phase
            self.current_phase = next_phase
            if previous_phase:
                text = f"{self._phase_name(previous_phase)} → {self._phase_name(next_phase)}"
            else:
                text = f"초기 단계: {self._phase_name(next_phase)}"
            self._append(
                "Phase 전환",
                text,
                phase_id=next_phase,
                event_key=("phase", previous_phase, next_phase),
            )

    def _on_simulation(self, msg: SimulationState) -> None:
        self.latest_simulation = msg
        if self.recording and msg.execution_state == "completed" and self.completed_datetime is None:
            self.completed_datetime = datetime.now(SEOUL_TIMEZONE)
            self._append("수술 종료", "디지털 트윈 실행 상태가 completed로 전환됨")

    def _on_actor_event(self, msg: SurgeonActorEvent) -> None:
        if msg.event_type == "field_event":
            self._append(
                "중요 수술 이벤트",
                f"{self._phase_name(msg.phase_id)} 시작",
                phase_id=self.current_phase,
                event_key=(
                    "field_event",
                    int(msg.stamp.sec),
                    int(msg.stamp.nanosec),
                    msg.phase_id,
                ),
            )
        elif msg.event_type == "field_event_resolved":
            self._append(
                "중요 수술 이벤트",
                (
                    f"{self._phase_name(msg.phase_id)} 해제"
                    f" | {self._phase_name(self.current_phase)} 수행 재개"
                ),
                phase_id=self.current_phase,
                event_key=(
                    "field_event_resolved",
                    int(msg.stamp.sec),
                    int(msg.stamp.nanosec),
                    msg.phase_id,
                ),
            )

        speech = _compact_text(msg.voice_text)
        if not speech:
            return
        tool = self._tool_name(msg.tool_id)
        suffix = f" | 관련 도구: {tool}" if tool else ""
        self.actor_speech_count += 1
        self._append(
            "집도의 발화",
            f"{speech}{suffix}",
            phase_id=msg.phase_id,
            event_key=(
                "speech",
                int(msg.stamp.sec),
                int(msg.stamp.nanosec),
                msg.event_type,
                speech,
            ),
        )

    def _on_vlm_result(self, msg: VLMResult) -> None:
        self.vlm_results += 1
        if not self.recording:
            return
        phase_rows = tuple(
            (phase_id, round(float(confidence), 2))
            for phase_id, confidence in zip(msg.phase_ids[:2], msg.phase_confidences[:2])
        )
        tool_rows = tuple(
            (
                tool_id,
                location_id,
                location_type,
                round(float(confidence), 2),
            )
            for tool_id, location_id, location_type, confidence in zip(
                msg.observed_tool_ids,
                msg.observed_location_ids,
                msg.observed_location_types,
                msg.observed_confidences,
            )
        )
        gesture = (
            str(msg.gesture_event_type),
            str(msg.gesture_requested_tool),
            str(msg.gesture_hand_pose),
            round(float(msg.gesture_confidence), 2),
        )
        structure = (
            tuple(phase_id for phase_id, _confidence in phase_rows),
            tuple(
                (tool_id, location_id, location_type)
                for tool_id, location_id, location_type, _confidence in tool_rows
            ),
            gesture[:3],
        )
        now = time.monotonic()
        if (
            structure == self.last_visual_structure
            and now - self.last_visual_monotonic < self.visual_interval_sec
        ):
            return
        self.last_visual_structure = structure
        self.last_visual_monotonic = now

        parts: list[str] = []
        if phase_rows:
            parts.append(
                "장면 단계 후보="
                + ", ".join(
                    f"{self._phase_name(phase_id)} {confidence:.2f}"
                    for phase_id, confidence in phase_rows
                )
            )
        if tool_rows:
            parts.append(
                "관찰 도구="
                + ", ".join(
                    f"{self._tool_name(tool_id)}@{location_id or location_type} {confidence:.2f}"
                    for tool_id, location_id, location_type, confidence in tool_rows
                )
            )
        if msg.gesture_event_type:
            gesture_tool = self._tool_name(msg.gesture_requested_tool)
            parts.append(
                "제스처="
                f"{msg.gesture_event_type}"
                f"{f'/{gesture_tool}' if gesture_tool else ''}"
                f" {float(msg.gesture_confidence):.2f}"
            )
        summary = _compact_text(msg.summary, 300)
        if summary:
            parts.append(f"요약={summary}")
        parts.append(f"불확실도={float(msg.uncertainty):.2f}")
        self._append("VLM 시각 단서", " | ".join(parts))

    def _on_vlm_health(self, msg: VLMHealth) -> None:
        if msg.connected and msg.healthy and not msg.last_error:
            self.vlm_healthy_samples += 1
            return
        error = _compact_text(msg.last_error or "VLM 연결 또는 상태 이상")
        if error and (not self.vlm_errors or error != self.vlm_errors[-1]):
            self.vlm_errors.append(error)
            self._append("시스템 경고", f"VLM: {error}")

    def _on_twin_event(self, msg: TwinEvent) -> None:
        if msg.event_type not in TOOL_TIMELINE_EVENTS:
            return
        detail = _json_mapping(msg.detail_json)
        tool = self._tool_name(msg.instrument_id)
        fields = [self._event_name(msg.event_type)]
        if tool:
            fields.append(f"도구={tool}")
        source = msg.source_location_id or detail.get("from") or detail.get("source", "")
        target = msg.target_location_id or detail.get("to") or detail.get("target", "")
        if source or target:
            fields.append(f"이동={source or '?'} → {target or '?'}")
        if msg.arm:
            fields.append(f"팔={msg.arm}")
        if msg.status:
            fields.append(f"상태={msg.status}")
        self._append(
            "도구 교환",
            " | ".join(str(value) for value in fields),
            phase_id=msg.phase_id,
            event_key=(
                "tool",
                int(msg.stamp.sec),
                int(msg.stamp.nanosec),
                msg.event_type,
                msg.instrument_id,
            ),
        )

    def _on_important_event(self, msg: SimulationEvent) -> None:
        event_type = str(msg.event_type)
        if event_type not in HUMANOID_EVENTS and event_type not in BED_ROBOT_EVENTS:
            return
        detail = _json_mapping(msg.detail)
        if event_type in BED_ROBOT_EVENTS:
            kind = "보조 로봇 중요 이벤트"
        else:
            kind = "휴머노이드 중요 이벤트"
        fields = [self._event_name(event_type)]
        tool = self._tool_name(msg.instrument_id)
        if tool:
            fields.append(f"도구={tool}")
        action = detail.get("action") or detail.get("task_type") or detail.get("operation")
        if action:
            fields.append(f"작업={action}")
        group_id = detail.get("group_id")
        if group_id:
            fields.append(f"그룹={group_id}")
        if msg.arm:
            fields.append(f"팔={msg.arm}")
        if msg.from_anchor or msg.to_anchor:
            fields.append(f"이동={msg.from_anchor or '?'} → {msg.to_anchor or '?'}")
        if msg.status:
            fields.append(f"상태={msg.status}")
        reason = detail.get("reason") or detail.get("rejection_reason") or detail.get("error_message")
        if reason:
            fields.append(f"설명={reason}")
        self._append(
            kind,
            " | ".join(str(value) for value in fields),
            event_key=(
                "important",
                int(msg.stamp.sec),
                int(msg.stamp.nanosec),
                event_type,
                msg.instrument_id,
            ),
        )

    def _on_skill_status(self, msg: SkillStatus) -> None:
        if msg.state not in TERMINAL_SKILL_FAILURE_STATES:
            return
        self._append(
            "휴머노이드 중요 이벤트",
            (
                f"휴머노이드 작업 실패 | 작업={msg.action} | "
                f"도구={self._tool_name(msg.instrument_id)} | 상태={msg.state} | "
                f"설명={_compact_text(msg.message)}"
            ),
            event_key=("skill_failure", msg.command_id, msg.state),
        )

    def wait_for_services(self, timeout_sec: float = 40.0) -> None:
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            if (
                self.select_client.wait_for_service(timeout_sec=0.2)
                and self.control_client.wait_for_service(timeout_sec=0.2)
            ):
                return
        raise RuntimeError("시뮬레이션 제어 서비스가 준비되지 않았습니다.")

    def _call_control(self, command: str, timeout_sec: float = 40.0) -> None:
        request = ControlSimulation.Request()
        request.command = command
        future = self.control_client.call_async(request)
        rclpy.spin_until_future_complete(self, future, timeout_sec=timeout_sec)
        response = future.result()
        if response is None or not response.success:
            raise RuntimeError(
                f"simulation control {command} 실패: "
                f"{response.message if response is not None else '응답 없음'}"
            )

    def _select_bundle(self, bundle_name: str, timeout_sec: float = 45.0) -> None:
        request = SelectSimulationBundle.Request()
        request.bundle_name = bundle_name
        request.restart_if_running = False
        future = self.select_client.call_async(request)
        rclpy.spin_until_future_complete(self, future, timeout_sec=timeout_sec)
        response = future.result()
        if response is None or not response.success:
            raise RuntimeError(
                f"bundle 선택 실패: {response.message if response is not None else '응답 없음'}"
            )

    def wait_until(self, predicate, timeout_sec: float, description: str) -> None:
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            rclpy.spin_once(self, timeout_sec=0.2)
            if predicate():
                return
        raise RuntimeError(f"{description} 대기 시간 초과")

    def prepare_and_start(self, spec_name: str) -> None:
        self.wait_for_services()
        self._call_control("reset")
        self.wait_until(
            lambda: self.latest_simulation is not None
            and self.latest_simulation.execution_state == "idle",
            35.0,
            "시뮬레이션 초기화",
        )
        self._select_bundle(spec_name)
        self.recording = True
        self.started_monotonic = time.monotonic()
        self.started_datetime = datetime.now(SEOUL_TIMEZONE)
        self._append("수술 시작", f"{self.spec.bundle.procedure_display_name_ko} 전체 사이클 시작")
        self._call_control("start")
        self.wait_until(
            lambda: self.latest_simulation is not None
            and self.latest_simulation.running
            and self.latest_simulation.execution_state == "running",
            45.0,
            "시뮬레이션 실행 상태",
        )

    def wait_for_completion(self, timeout_sec: float) -> None:
        self.wait_until(
            lambda: self.latest_simulation is not None
            and self.latest_simulation.execution_state == "completed",
            timeout_sec,
            "전체 수술 완료",
        )
        deadline = time.time() + 2.0
        while time.time() < deadline:
            rclpy.spin_once(self, timeout_sec=0.1)
        if self.completed_datetime is None:
            self.completed_datetime = datetime.now(SEOUL_TIMEZONE)

    def render_text(self, *, completed: bool, failure: str = "") -> str:
        end_datetime = self.completed_datetime or datetime.now(SEOUL_TIMEZONE)
        duration = self._elapsed()
        phase_sequence: list[str] = []
        for event in self.events:
            phase_id = str(event.get("phase_id") or "")
            if phase_id and (not phase_sequence or phase_id != phase_sequence[-1]):
                phase_sequence.append(phase_id)

        counts = Counter(str(event["kind"]) for event in self.events)
        lines = [
            "갑상선절제술 전체 수행 타임라인",
            "=" * 48,
            "문서 유형: 수술기록 생성 AI 입력용 시뮬레이션 타임라인",
            "주의: 본 문서는 디지털 트윈과 AI 관찰 결과를 기록한 것으로, 의료진이 확정한 임상 수술기록지가 아닙니다.",
            f"수술: {self.spec.bundle.procedure_display_name_ko} ({self.spec.procedure_id})",
            f"시작 시각: {self.started_datetime.isoformat(timespec='seconds') if self.started_datetime else '미확인'}",
            f"종료 시각: {end_datetime.isoformat(timespec='seconds')}",
            f"총 수행시간: {duration:.1f}초",
            f"완료 상태: {'completed' if completed else 'incomplete'}",
            f"VLM: {self.vlm_model_id}",
            f"집도의 LLM: {self.actor_model_id}",
            f"단계 경로: {' → '.join(self._phase_name(phase_id) for phase_id in phase_sequence) or '미확인'}",
            (
                "수집 건수: "
                f"집도의 발화 {counts['집도의 발화']}건, "
                f"VLM 시각 단서 {counts['VLM 시각 단서']}건, "
                f"도구 교환 {counts['도구 교환']}건, "
                f"Phase 전환 {counts['Phase 전환']}건, "
                f"중요 수술 이벤트 {counts['중요 수술 이벤트']}건, "
                f"휴머노이드 중요 이벤트 {counts['휴머노이드 중요 이벤트']}건, "
                f"보조 로봇 중요 이벤트 {counts['보조 로봇 중요 이벤트']}건"
            ),
            (
                "VLM 상태: "
                f"결과 {self.vlm_results}건, 정상 health {self.vlm_healthy_samples}건, "
                f"고유 오류 {len(self.vlm_errors)}건"
            ),
        ]
        if failure:
            lines.append(f"중단 원인: {_compact_text(failure, 500)}")

        lines.extend(["", "전체 타임라인", "-" * 48])
        rendered_phase = object()
        for event in sorted(self.events, key=lambda item: float(item["elapsed_sec"])):
            phase_id = str(event.get("phase_id") or "")
            if phase_id != rendered_phase:
                lines.extend(["", f"## {self._phase_name(phase_id)}"])
                rendered_phase = phase_id
            elapsed = float(event["elapsed_sec"])
            minutes = int(elapsed // 60)
            seconds = elapsed - minutes * 60
            lines.append(
                f"[{minutes:02d}:{seconds:06.3f}] "
                f"[{event['kind']}] {event['text']}"
            )

        lines.extend(["", "기록 종료"])
        return "\n".join(lines) + "\n"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec-name", default="thyroidectomy")
    parser.add_argument("--vlm-model-id", default="qwen3.6-35b-a3b-mtp@q2_k_xl")
    parser.add_argument("--actor-model-id", default="google/gemma-4-12b-qat")
    parser.add_argument("--timeout-sec", type=float, default=900.0)
    parser.add_argument("--visual-interval-sec", type=float, default=8.0)
    parser.add_argument(
        "--output",
        default="reports/thyroidectomy_surgery_timeline.txt",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    rclpy.init()
    recorder = ThyroidectomyTimelineRecorder(
        spec_name=args.spec_name,
        vlm_model_id=args.vlm_model_id,
        actor_model_id=args.actor_model_id,
        visual_interval_sec=args.visual_interval_sec,
    )
    completed = False
    failure = ""
    try:
        recorder.prepare_and_start(args.spec_name)
        recorder.wait_for_completion(float(args.timeout_sec))
        completed = True
        return_code = 0
    except Exception as exc:
        failure = str(exc)
        recorder.get_logger().error(failure)
        return_code = 1
    finally:
        output_path.write_text(
            recorder.render_text(completed=completed, failure=failure),
            encoding="utf-8",
        )
        print(f"TIMELINE_PATH={output_path}")
        print(f"TIMELINE_COMPLETED={str(completed).lower()}")
        recorder.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
