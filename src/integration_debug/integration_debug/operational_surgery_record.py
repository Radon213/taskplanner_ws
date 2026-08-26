"""Collect one live surgery timeline and submit it after a spoken pause.

This observer never calls a simulation or robot control endpoint.  The
simulation manager remains the only lifecycle authority and publishes a
transcript-free receipt after it has processed a spoken command through the
normal ``/simulation/control`` path.  Only a successful ``pause`` receipt can
finalize and submit a record.
"""

from __future__ import annotations

from collections import Counter, deque
from datetime import datetime
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable
from uuid import uuid4
from zoneinfo import ZoneInfo

import rclpy
from rclpy.node import Node
from std_msgs.msg import String
from surgical_msgs.msg import (
    SimulationEvent,
    SimulationState,
    SkillStatus,
    SurgeonActorEvent,
    TwinEvent,
    VLMResult,
    VoiceCommandIntent,
)

from integration_debug.surgery_record_runtime import (
    MAX_TEXT_CHARS,
    SurgeryRecordRuntime,
)


SEOUL = ZoneInfo("Asia/Seoul")
CONTROL_RESULT_SCHEMA = "taskplanner.voice_procedure_control.result.v1"
STATUS_SCHEMA = "taskplanner.operational_surgery_record.status.v1"
DEFAULT_ENDPOINT = (
    "https://dev.puzzle-ai.com:6627/api/v1/surgery/img_texts"
)
MAX_EVENTS = 2_000
MAX_EVENT_TEXT = 700
FINAL_SKILL_STATES = frozenset(
    {
        "succeeded",
        "completed",
        "rejected",
        "dispatch_failed",
        "server_unavailable",
        "canceled",
        "aborted",
        "result_failed",
    }
)


def _compact(value: Any, limit: int = MAX_EVENT_TEXT) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _stamp_key(stamp: Any) -> tuple[int, int]:
    return (
        int(getattr(stamp, "sec", 0) or 0),
        int(getattr(stamp, "nanosec", 0) or 0),
    )


def _json_object(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or ""))
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def parse_successful_voice_pause(value: Any) -> dict[str, Any] | None:
    """Return a validated lifecycle receipt only after pause succeeded."""

    payload = _json_object(value)
    if payload.get("schema") != CONTROL_RESULT_SCHEMA:
        return None
    if payload.get("requested_command") != "pause":
        return None
    if payload.get("success") is not True:
        return None
    if payload.get("execution_state") != "paused":
        return None
    utterance_id = str(payload.get("utterance_id") or "").strip()
    procedure_id = str(payload.get("procedure_id") or "").strip()
    if not utterance_id or not procedure_id:
        return None
    return {
        "utterance_id": utterance_id[:128],
        "procedure_id": procedure_id[:64],
        "message": _compact(payload.get("message"), 300),
    }


class SurgeryRecordSession:
    """Bounded in-memory timeline for one running procedure."""

    def __init__(self, *, max_events: int = MAX_EVENTS) -> None:
        self._max_events = max(10, int(max_events))
        self.active = False
        self.procedure_id = ""
        self.active_bundle = ""
        self.surgery_code = ""
        self.started_at: datetime | None = None
        self.started_monotonic = 0.0
        self.current_phase = ""
        self.events: deque[dict[str, Any]] = deque(maxlen=self._max_events)
        self.dropped_events = 0
        self._seen: set[tuple[Any, ...]] = set()
        self._last_runtime_signature: tuple[Any, ...] | None = None
        self._last_vlm_signature: tuple[Any, ...] | None = None

    def start(
        self,
        *,
        procedure_id: str,
        active_bundle: str,
        phase_id: str,
        now: datetime,
        now_monotonic: float,
    ) -> None:
        self.active = True
        self.procedure_id = _compact(procedure_id, 64)
        self.active_bundle = _compact(active_bundle, 64)
        self.surgery_code = (
            f"THY-{now.astimezone(SEOUL).strftime('%Y%m%d-%H%M%S')}-"
            f"{uuid4().hex[:6]}"
        )
        self.started_at = now.astimezone(SEOUL)
        self.started_monotonic = float(now_monotonic)
        self.current_phase = _compact(phase_id, 100)
        self.events.clear()
        self.dropped_events = 0
        self._seen.clear()
        self._last_runtime_signature = None
        self._last_vlm_signature = None
        self.append(
            "수술 시작",
            f"{self.active_bundle or self.procedure_id} 실행 시작",
            phase_id=self.current_phase,
            now_monotonic=now_monotonic,
            event_key=("start", self.surgery_code),
        )

    def append(
        self,
        kind: str,
        text: Any,
        *,
        phase_id: str = "",
        now_monotonic: float | None = None,
        event_key: tuple[Any, ...] | None = None,
    ) -> None:
        if not self.active:
            return
        compact_text = _compact(text)
        if not compact_text:
            return
        if event_key is not None:
            if event_key in self._seen:
                return
            if len(self._seen) >= self._max_events * 3:
                self._seen.clear()
            self._seen.add(event_key)
        if len(self.events) == self.events.maxlen:
            self.dropped_events += 1
        now_value = time.monotonic() if now_monotonic is None else now_monotonic
        self.events.append(
            {
                "elapsed_sec": max(0.0, float(now_value) - self.started_monotonic),
                "kind": _compact(kind, 80),
                "phase_id": _compact(phase_id or self.current_phase, 100),
                "text": compact_text,
            }
        )

    def observe_simulation(
        self,
        msg: Any,
        *,
        now_monotonic: float | None = None,
    ) -> None:
        if not self.active:
            return
        now_value = time.monotonic() if now_monotonic is None else now_monotonic
        phase = _compact(getattr(msg, "filtered_phase", ""), 100)
        if phase and phase != self.current_phase:
            previous = self.current_phase or "미확인"
            self.current_phase = phase
            self.append(
                "단계 전환",
                f"{previous} -> {phase}",
                phase_id=phase,
                now_monotonic=now_value,
                event_key=("phase", previous, phase),
            )
        signature = (
            _compact(getattr(msg, "robot_state", ""), 80),
            _compact(getattr(msg, "right_hand_tool", ""), 100),
            _compact(getattr(msg, "left_hand_tool", ""), 100),
            _compact(getattr(msg, "active_robot_task_type", ""), 100),
            _compact(getattr(msg, "active_robot_task_tool_id", ""), 100),
        )
        if signature != self._last_runtime_signature:
            self._last_runtime_signature = signature
            self.append(
                "상태 변화",
                (
                    f"robot={signature[0] or '-'} | right={signature[1] or '-'} | "
                    f"left={signature[2] or '-'} | task={signature[3] or '-'} | "
                    f"tool={signature[4] or '-'}"
                ),
                phase_id=phase,
                now_monotonic=now_value,
            )

    def observe_voice(self, msg: Any, *, now_monotonic: float | None = None) -> None:
        if not self.active or not bool(getattr(msg, "source_is_final", False)):
            return
        utterance_id = _compact(getattr(msg, "utterance_id", ""), 128)
        text = _compact(getattr(msg, "raw_text", ""), 500)
        if not utterance_id or not text:
            return
        intent = _compact(getattr(msg, "intent", ""), 80)
        disposition = _compact(getattr(msg, "disposition", ""), 40)
        self.append(
            "집도의 음성",
            f"{text} | intent={intent or '-'} | disposition={disposition or '-'}",
            now_monotonic=now_monotonic,
            event_key=("voice", utterance_id),
        )

    def observe_twin_event(self, msg: Any, *, now_monotonic: float | None = None) -> None:
        if not self.active:
            return
        detail = _json_object(getattr(msg, "detail_json", ""))
        source = (
            _compact(getattr(msg, "source_location_id", ""), 100)
            or _compact(detail.get("from") or detail.get("source"), 100)
        )
        target = (
            _compact(getattr(msg, "target_location_id", ""), 100)
            or _compact(detail.get("to") or detail.get("target"), 100)
        )
        fields = [
            f"event={_compact(getattr(msg, 'event_type', ''), 100)}",
            f"tool={_compact(getattr(msg, 'instrument_id', ''), 100) or '-'}",
        ]
        if source or target:
            fields.append(f"route={source or '?'} -> {target or '?'}")
        status = _compact(getattr(msg, "status", ""), 80)
        if status:
            fields.append(f"status={status}")
        self.append(
            "도구/트윈 이벤트",
            " | ".join(fields),
            phase_id=_compact(getattr(msg, "phase_id", ""), 100),
            now_monotonic=now_monotonic,
            event_key=(
                "twin",
                *_stamp_key(getattr(msg, "stamp", None)),
                getattr(msg, "event_type", ""),
                getattr(msg, "instance_id", ""),
            ),
        )

    def observe_important_event(
        self, msg: Any, *, now_monotonic: float | None = None
    ) -> None:
        if not self.active:
            return
        detail = _compact(getattr(msg, "detail", ""), 350)
        self.append(
            "중요 이벤트",
            (
                f"event={_compact(getattr(msg, 'event_type', ''), 100)} | "
                f"tool={_compact(getattr(msg, 'instrument_id', ''), 100) or '-'} | "
                f"arm={_compact(getattr(msg, 'arm', ''), 40) or '-'} | "
                f"status={_compact(getattr(msg, 'status', ''), 80) or '-'}"
                f"{f' | detail={detail}' if detail else ''}"
            ),
            now_monotonic=now_monotonic,
            event_key=(
                "important",
                *_stamp_key(getattr(msg, "stamp", None)),
                getattr(msg, "event_type", ""),
                getattr(msg, "instrument_id", ""),
            ),
        )

    def observe_skill(self, msg: Any, *, now_monotonic: float | None = None) -> None:
        state = _compact(getattr(msg, "state", ""), 80)
        if not self.active or state not in FINAL_SKILL_STATES:
            return
        self.append(
            "로봇 작업 결과",
            (
                f"action={_compact(getattr(msg, 'action', ''), 100) or '-'} | "
                f"tool={_compact(getattr(msg, 'instrument_id', ''), 100) or '-'} | "
                f"state={state} | success={bool(getattr(msg, 'success', False))} | "
                f"message={_compact(getattr(msg, 'message', ''), 300) or '-'}"
            ),
            now_monotonic=now_monotonic,
            event_key=(
                "skill",
                _compact(getattr(msg, "command_id", ""), 128),
                state,
            ),
        )

    def observe_actor(self, msg: Any, *, now_monotonic: float | None = None) -> None:
        if not self.active:
            return
        speech = _compact(getattr(msg, "voice_text", ""), 500)
        if not speech:
            return
        self.append(
            "집도의 이벤트",
            (
                f"{speech} | event={_compact(getattr(msg, 'event_type', ''), 80) or '-'} | "
                f"tool={_compact(getattr(msg, 'tool_id', ''), 100) or '-'}"
            ),
            phase_id=_compact(getattr(msg, "phase_id", ""), 100),
            now_monotonic=now_monotonic,
            event_key=(
                "actor",
                *_stamp_key(getattr(msg, "stamp", None)),
                getattr(msg, "event_type", ""),
                speech,
            ),
        )

    def observe_vlm(self, msg: Any, *, now_monotonic: float | None = None) -> None:
        if not self.active:
            return
        phases = tuple(
            (str(phase_id), round(float(confidence), 3))
            for phase_id, confidence in zip(
                list(getattr(msg, "phase_ids", []))[:3],
                list(getattr(msg, "phase_confidences", []))[:3],
            )
        )
        tools = tuple(
            (str(tool_id), str(location), round(float(confidence), 3))
            for tool_id, location, confidence in zip(
                list(getattr(msg, "observed_tool_ids", []))[:12],
                list(getattr(msg, "observed_location_ids", []))[:12],
                list(getattr(msg, "observed_confidences", []))[:12],
            )
        )
        signature = (phases, tools)
        if signature == self._last_vlm_signature:
            return
        self._last_vlm_signature = signature
        self.append(
            "VLM 관측",
            (
                f"phases={phases or '-'} | tools={tools or '-'} | uncertainty="
                f"{float(getattr(msg, 'uncertainty', 0.0)):.3f} | "
                f"summary={_compact(getattr(msg, 'summary', ''), 250) or '-'}"
            ),
            now_monotonic=now_monotonic,
        )

    def finalize(
        self,
        *,
        ended_at: datetime,
        ended_monotonic: float,
        utterance_id: str,
        control_message: str,
    ) -> dict[str, str] | None:
        if not self.active or self.started_at is None or not self.surgery_code:
            return None
        self.append(
            "음성 종료",
            f"pause accepted | {control_message or 'simulation paused'}",
            now_monotonic=ended_monotonic,
            event_key=("pause", utterance_id),
        )
        ended = ended_at.astimezone(SEOUL)
        counts = Counter(str(event["kind"]) for event in self.events)
        lines = [
            "갑상선절제술 수술기록 생성 AI 입력용 타임라인",
            "=" * 52,
            "주의: 본문은 Taskplanner가 수집한 시스템 관측 기록이며 의료진이 확정한 임상 기록지가 아닙니다.",
            f"procedureId: {self.procedure_id}",
            f"activeBundle: {self.active_bundle}",
            f"surgeryCode: {self.surgery_code}",
            f"시작 시각: {self.started_at.isoformat(timespec='seconds')}",
            f"음성 종료/일시정지 시각: {ended.isoformat(timespec='seconds')}",
            f"수집 시간: {max(0.0, ended_monotonic - self.started_monotonic):.1f}초",
            f"종료 음성 utteranceId: {utterance_id}",
            f"수집 이벤트: {len(self.events)}건 (용량 제한으로 제외 {self.dropped_events}건)",
            "종류별 건수: "
            + ", ".join(f"{key} {value}" for key, value in sorted(counts.items())),
            "",
            "전체 타임라인",
            "-" * 52,
        ]
        rendered_phase = object()
        for event in self.events:
            phase = str(event["phase_id"] or "")
            if phase != rendered_phase:
                lines.append(f"\n[단계: {phase or '미확인'}]")
                rendered_phase = phase
            lines.append(
                f"+{float(event['elapsed_sec']):08.1f}s "
                f"[{event['kind']}] {event['text']}"
            )
        text = "\n".join(lines).strip() + "\n"
        if len(text) > MAX_TEXT_CHARS:
            suffix = "\n[일부 후반 이벤트는 API 문자 수 제한으로 생략되었습니다.]\n"
            text = text[: MAX_TEXT_CHARS - len(suffix)] + suffix
        result = {
            "surgery_code": self.surgery_code,
            "date": self.started_at.date().isoformat(),
            "text": text,
        }
        self.active = False
        return result


class OperationalSurgeryRecordNode(Node):
    """Read-only ROS observer plus secured asynchronous HTTP submitter."""

    def __init__(
        self,
        *,
        runtime: SurgeryRecordRuntime | None = None,
        now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__("operational_surgery_record")
        self._now = now or (lambda: datetime.now(SEOUL))
        self._monotonic = monotonic
        self._lock = threading.RLock()
        self._session = SurgeryRecordSession()
        self._room_name = os.environ.get(
            "PUZZLE_SURGERY_RECORD_ROOM_NAME",
            "Preclinical Center",
        ).strip()
        self._output_dir = Path(
            os.environ.get(
                "TASKPLANNER_SURGERY_RECORD_OUTPUT_DIR",
                "/taskplanner-runs/surgery-records",
            )
        )
        allowed = os.environ.get(
            "TASKPLANNER_SURGERY_RECORD_PROCEDURES",
            "thyroidectomy,thyroidectomy_demo",
        )
        self._allowed_procedures = {
            item.strip() for item in allowed.split(",") if item.strip()
        }
        endpoint = os.environ.get("PUZZLE_SURGERY_RECORD_ENDPOINT", DEFAULT_ENDPOINT)
        self._runtime = runtime or SurgeryRecordRuntime(
            input_dir=os.environ.get(
                "TASKPLANNER_SURGERY_RECORD_INPUT_DIR",
                "/surgery-record-inputs",
            ),
            default_endpoint=endpoint,
            api_key_file=os.environ.get(
                "PUZZLE_SURGERY_RECORD_API_KEY_FILE",
                "/run/taskplanner-secrets/puzzle-surgery-record-api-key",
            ),
            allowed_endpoints=(endpoint,),
            timeout_sec=float(
                os.environ.get("PUZZLE_SURGERY_RECORD_TIMEOUT_SEC", "35.0")
            ),
        )
        self._last_output_path = ""
        self._last_request_id = ""
        self._last_error = ""
        self._last_runtime_event_state = ""
        self._ignore_running_until = 0.0

        self._status_pub = self.create_publisher(
            String,
            "/surgery/record/post_status",
            10,
        )
        self.create_subscription(
            SimulationState, "/simulation/state", self._on_simulation, 50
        )
        self.create_subscription(
            VoiceCommandIntent, "/surgery/voice/intent", self._on_voice, 50
        )
        self.create_subscription(
            String,
            "/simulation/voice_procedure_control/result",
            self._on_voice_control_result,
            20,
        )
        self.create_subscription(TwinEvent, "/twin/events", self._on_twin, 100)
        self.create_subscription(
            SimulationEvent,
            "/twin/important_event",
            self._on_important,
            100,
        )
        self.create_subscription(SkillStatus, "/skill/status", self._on_skill, 100)
        self.create_subscription(
            SurgeonActorEvent,
            "/surgeon/actor_event",
            self._on_actor,
            50,
        )
        self.create_subscription(VLMResult, "/vlm/result", self._on_vlm, 50)
        self._timer = self.create_timer(0.5, self._publish_status)
        self.get_logger().info(
            "operational surgery-record auto-post enabled; "
            "submission requires a successful spoken pause receipt"
        )

    def _on_simulation(self, msg: SimulationState) -> None:
        now_mono = self._monotonic()
        with self._lock:
            running = bool(msg.running) and str(msg.execution_state) == "running"
            procedure = str(msg.procedure_id or msg.active_bundle or "").strip()
            if (
                running
                and not self._session.active
                and now_mono >= self._ignore_running_until
                and (
                    procedure in self._allowed_procedures
                    or str(msg.active_bundle) in self._allowed_procedures
                )
            ):
                self._session.start(
                    procedure_id=procedure,
                    active_bundle=str(msg.active_bundle),
                    phase_id=str(msg.filtered_phase),
                    now=self._now(),
                    now_monotonic=now_mono,
                )
                self._last_error = ""
            self._session.observe_simulation(msg, now_monotonic=now_mono)

    def _on_voice(self, msg: VoiceCommandIntent) -> None:
        with self._lock:
            self._session.observe_voice(msg, now_monotonic=self._monotonic())

    def _on_twin(self, msg: TwinEvent) -> None:
        with self._lock:
            self._session.observe_twin_event(msg, now_monotonic=self._monotonic())

    def _on_important(self, msg: SimulationEvent) -> None:
        with self._lock:
            self._session.observe_important_event(
                msg, now_monotonic=self._monotonic()
            )

    def _on_skill(self, msg: SkillStatus) -> None:
        with self._lock:
            self._session.observe_skill(msg, now_monotonic=self._monotonic())

    def _on_actor(self, msg: SurgeonActorEvent) -> None:
        with self._lock:
            self._session.observe_actor(msg, now_monotonic=self._monotonic())

    def _on_vlm(self, msg: VLMResult) -> None:
        with self._lock:
            self._session.observe_vlm(msg, now_monotonic=self._monotonic())

    def _on_voice_control_result(self, msg: String) -> None:
        receipt = parse_successful_voice_pause(msg.data)
        if receipt is None:
            return
        now_mono = self._monotonic()
        with self._lock:
            if receipt["procedure_id"] not in {
                self._session.procedure_id,
                self._session.active_bundle,
            }:
                return
            record = self._session.finalize(
                ended_at=self._now(),
                ended_monotonic=now_mono,
                utterance_id=receipt["utterance_id"],
                control_message=receipt["message"],
            )
            if record is None:
                return
            # Ignore any in-flight running heartbeat emitted just before the
            # pause frame; a later explicit resume can begin a new record.
            self._ignore_running_until = now_mono + 1.0
            try:
                path = self._save_local_record(record)
                request_id = self._runtime.submit_async(
                    {
                        "room_name": self._room_name,
                        "surgery_code": record["surgery_code"],
                        "date": record["date"],
                        "text": record["text"],
                    }
                )
            except Exception as exc:
                self._last_error = _compact(exc, 500)
                self.get_logger().error(
                    f"surgery-record submission was not started: {self._last_error}"
                )
                return
            self._last_output_path = str(path)
            self._last_request_id = request_id
            self._last_error = ""
            self.get_logger().info(
                f"surgery record saved locally and submitted: {record['surgery_code']}"
            )

    def _save_local_record(self, record: dict[str, str]) -> Path:
        self._output_dir.mkdir(parents=True, exist_ok=True)
        final_path = self._output_dir / f"{record['surgery_code']}.txt"
        temporary = self._output_dir / f".{record['surgery_code']}.{uuid4().hex}.tmp"
        temporary.write_text(record["text"], encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, final_path)
        return final_path

    def _publish_status(self) -> None:
        snapshot = self._runtime.snapshot()
        result = dict(snapshot.get("last_result") or {})
        state = str(snapshot.get("state") or "IDLE")
        if state != self._last_runtime_event_state:
            self._last_runtime_event_state = state
            if state in {"FAILED", "REMOTE_STATE_UNKNOWN"}:
                self.get_logger().error(
                    "surgery-record POST finished without a confirmed success: "
                    f"{_compact(snapshot.get('last_error'), 400)}"
                )
            elif state == "SUCCEEDED":
                self.get_logger().info(
                    "surgery-record POST confirmed: "
                    f"receipt={_compact(result.get('receipt_id'), 128)}"
                )
        with self._lock:
            payload = {
                "schema": STATUS_SCHEMA,
                "stamp_sec": round(
                    self.get_clock().now().nanoseconds / 1_000_000_000.0,
                    6,
                ),
                "recording": self._session.active,
                "procedure_id": self._session.procedure_id,
                "surgery_code": self._session.surgery_code,
                "event_count": len(self._session.events),
                "output_path": self._last_output_path,
                "request_id": self._last_request_id,
                "submit_state": state,
                "success": bool(result.get("success", False)),
                "http_status": int(result.get("http_status") or 0),
                "receipt_id": _compact(result.get("receipt_id"), 128),
                "error": _compact(snapshot.get("last_error") or self._last_error, 500),
            }
        self._status_pub.publish(
            String(data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        )


def main() -> None:
    rclpy.init()
    node = OperationalSurgeryRecordNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
