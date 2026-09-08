"""Collect one Live surgery timeline and submit it after true termination.

This owner never calls a simulation or robot-control endpoint.  Voice and UI
controls converge in SimulationManager, which publishes one run-bound terminal
receipt only after Stop or natural completion has settled.  Pause remains part
of the same active record and can never authorize an HTTPS POST.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
import hashlib
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
from rclpy.qos import (
    DurabilityPolicy,
    HistoryPolicy,
    QoSProfile,
    ReliabilityPolicy,
)
from std_msgs.msg import String
from surgical_msgs.msg import (
    SimulationEvent,
    SimulationState,
    SkillStatus,
    SpeechUtterance,
    SurgeonActorEvent,
    TwinEvent,
    VLMResult,
)

from integration_debug.surgery_record_runtime import SurgeryRecordRuntime


SEOUL = ZoneInfo("Asia/Seoul")
TERMINAL_RECEIPT_SCHEMA = "taskplanner.simulation.lifecycle_terminal.v1"
TERMINAL_RECEIPT_TOPIC = "/simulation/lifecycle_terminal"
STATUS_SCHEMA = "taskplanner.operational_surgery_record.status.v1"
STATUS_TOPIC = "/surgery/record/post_status"
PUBLIC_RECEIPT_SCHEMA = "taskplanner.surgery_record.receipt.v1"
PUBLIC_RECEIPT_TOPIC = "/surgery/record/receipt"
DEFAULT_ENDPOINT = (
    "https://192.168.1.5:6627/api/v1/surgery/img_texts"
)
MAX_EVENTS = 2_000
MAX_EVENT_TEXT = 700
# The remote record service returns a syntactically successful but empty
# summary for high-volume timelines.  Keep the generated live input below the
# verified-success boundary in *serialized UTF-8 bytes*, rather than relying
# on the much larger character-only transport limit.
MAX_GENERATED_RECORD_BYTES = 22_000
MAX_GENERATED_VLM_ROWS = 104
# When a run has many verbose VLM observations, retain a representative spread
# across the run and compact each retained observation before dropping it.
MAX_COMPACTED_VLM_DETAIL_BYTES = 240
MIN_COMPACTED_VLM_DETAIL_BYTES = 48
RECORD_BUDGET_OMISSION_MARKER = (
    "일부 시간순 관찰은 수술기록지 생성 예산에 맞춰 생략되었습니다."
)
# A compact retry is used only when the remote service explicitly accepts the
# request but returns an empty generated summary.  It never retries uncertain
# transport outcomes, where a duplicate remote record could have been created.
MAX_RETRY_RECORD_BYTES = 8_000
MAX_RETRY_PHASE_ROWS = 6
MAX_RETRY_SURGEON_ROWS = 10
MAX_RETRY_ASSISTANT_ROWS = 8
MAX_RETRY_VLM_ROWS = 20
MAX_RETRY_DETAIL_BYTES = 120
# The runtime keeps a larger redacted diagnostic internally, but the public
# observer topic must stay small enough for a browser to display immediately.
MAX_STATUS_RESPONSE_BYTES = 16_384
# The public rosbridge enforces this larger cap only for the one terminal
# receipt topic. It accommodates the full 65,535-character generated record
# after the String and rosbridge JSON envelopes have escaped it.
MAX_PUBLIC_RECEIPT_FRAME_BYTES = 1_024 * 1_024
TERMINAL_POST_STATES = frozenset({"SUCCEEDED", "FAILED", "REMOTE_STATE_UNKNOWN"})
PUBLIC_RECEIPT_QOS = QoSProfile(
    history=HistoryPolicy.KEEP_LAST,
    depth=1,
    reliability=ReliabilityPolicy.RELIABLE,
    durability=DurabilityPolicy.TRANSIENT_LOCAL,
)
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
OBSERVED_UTTERANCE_TOPIC = "/surgery/audio/observed_utterance"
TERMINAL_EXECUTION_STATE_BY_KIND = {
    "stop": "halted",
    "completed": "completed",
}
# SimulationState and the durable lifecycle receipt are separate ROS topics.
# A terminal-reset path may therefore deliver the following idle frame before
# this observer receives its matching receipt.  Keep a very short, run-bound
# grace window only after an actual terminal state was observed.
TERMINAL_RECEIPT_GRACE_SEC = 3.0


def _compact(value: Any, limit: int = MAX_EVENT_TEXT) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _utf8_bytes(value: str) -> int:
    """Return the serialized UTF-8 length used by the external service."""

    return len(value.encode("utf-8"))


def _truncate_utf8(value: str, max_bytes: int) -> str:
    """Trim one rendered detail without splitting a UTF-8 character."""

    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    if max_bytes <= 0:
        return ""
    suffix = "…"
    suffix_bytes = suffix.encode("utf-8")
    if max_bytes <= len(suffix_bytes):
        return encoded[:max_bytes].decode("utf-8", errors="ignore")
    prefix = encoded[: max_bytes - len(suffix_bytes)].decode("utf-8", errors="ignore")
    return f"{prefix}{suffix}"


def _representative_rows(
    rows: list[tuple[float, int, str, str]],
    maximum: int,
) -> list[tuple[float, int, str, str]]:
    """Keep an ordered, deterministic spread from a longer observation run."""

    if maximum <= 0:
        return []
    if len(rows) <= maximum:
        return list(rows)
    if maximum == 1:
        return [rows[len(rows) // 2]]
    last_index = len(rows) - 1
    return [
        rows[(slot * last_index) // (maximum - 1)]
        for slot in range(maximum)
    ]


_TIMELINE_ROW_RE = re.compile(
    r"^\[(?P<timestamp>\d{2}:\d{2}:\d{2}\.\d{3})\] "
    r"(?P<source>Phase|집도의|VLM|스크럽 널스|어시스턴트) \| "
    r"(?P<detail>.+)$"
)


def _retry_surgery_code(primary_code: str) -> str:
    """Return a fresh code so an automatic retry cannot be rejected as duplicate."""

    base = re.sub(r"[^A-Za-z0-9_-]", "", primary_code)[:37] or "THY"
    return f"{base}-r{uuid4().hex[:10]}"


def _compact_retry_record(primary_record: dict[str, str]) -> dict[str, str]:
    """Render a small, factual 0704 retry input from an immutable primary TXT.

    The original bounded TXT is always retained on disk.  This second form is
    only prepared for the proven semantic-empty response path and keeps a
    representative subset of observed rows; it never invents clinical facts.
    """

    primary_text = str(primary_record.get("text") or "")
    lines = primary_text.splitlines()
    timeline_index = next(
        (index for index, line in enumerate(lines) if line == "[시간순 기록]"),
        None,
    )
    if timeline_index is None:
        raise ValueError("primary surgery-record text has no 0704 timeline block")
    surgery_line = next((line for line in lines if line.startswith("수술: ")), "수술: 수술 미기록")
    run_line = next((line for line in lines if line.startswith("Run: ")), "Run: 미기록")
    retry_code = _retry_surgery_code(str(primary_record.get("surgery_code") or ""))

    rows: list[tuple[str, int, str, str]] = []
    for index, line in enumerate(lines[timeline_index + 1 :], start=timeline_index + 1):
        match = _TIMELINE_ROW_RE.fullmatch(line)
        if match is None:
            continue
        source = match.group("source")
        detail = match.group("detail")
        if detail == RECORD_BUDGET_OMISSION_MARKER:
            continue
        rows.append(
            (
                match.group("timestamp"),
                index,
                source,
                _truncate_utf8(detail, MAX_RETRY_DETAIL_BYTES),
            )
        )

    def select(source: str, maximum: int) -> list[tuple[str, int, str, str]]:
        selected = [row for row in rows if row[2] == source]
        return _representative_rows(selected, maximum)

    def render(
        phase_limit: int,
        surgeon_limit: int,
        assistant_limit: int,
        vlm_limit: int,
    ) -> str:
        selected = [
            *select("Phase", phase_limit),
            *select("집도의", surgeon_limit),
            *select("스크럽 널스", assistant_limit),
            *select("어시스턴트", assistant_limit),
            *select("VLM", vlm_limit),
        ]
        selected.sort(key=lambda row: (row[0], row[1]))
        phase_changes = sum(
            1
            for _, _, source, detail in selected
            if source == "Phase" and detail.startswith("수술 단계 전환 | ")
        )
        surgeon_count = sum(1 for _, _, source, _ in selected if source == "집도의")
        assistant_count = sum(
            1
            for _, _, source, _ in selected
            if source in {"스크럽 널스", "어시스턴트"}
        )
        vlm_count = sum(1 for _, _, source, _ in selected if source == "VLM")
        header = [
            "갑상선절제술 수술 타임라인",
            "=" * 40,
            surgery_line,
            f"Case: {retry_code}",
            run_line,
            "VLM: Taskplanner 관측 VLM",
            "",
            "[기록 요약]",
            f"집도의 발화: {surgeon_count}건",
            f"VLM 관찰: {vlm_count}건",
            f"Phase 변화: {phase_changes}건",
            f"스크럽 널스·어시스턴트 행동: {assistant_count}건",
            "",
            "[시간순 기록]",
        ]
        rendered_rows = [
            f"[{timestamp}] {source} | {detail}"
            for timestamp, _, source, detail in selected
        ]
        return "\n".join([*header, *rendered_rows]) + "\n"

    for limits in (
        (
            MAX_RETRY_PHASE_ROWS,
            MAX_RETRY_SURGEON_ROWS,
            MAX_RETRY_ASSISTANT_ROWS,
            MAX_RETRY_VLM_ROWS,
        ),
        (4, 6, 5, 12),
        (3, 3, 3, 6),
        (2, 2, 2, 2),
        (2, 0, 0, 0),
    ):
        text = render(*limits)
        if _utf8_bytes(text) <= MAX_RETRY_RECORD_BYTES:
            return {
                "surgery_code": retry_code,
                "date": str(primary_record.get("date") or ""),
                "text": text,
            }
    raise ValueError("compact surgery-record retry could not fit its UTF-8 budget")


def _format_relative_timestamp(elapsed_sec: Any) -> str:
    """Render a nonnegative relative timestamp in the 0704 TXT contract."""

    try:
        total_millis = max(0, int(round(float(elapsed_sec) * 1_000)))
    except (TypeError, ValueError):
        total_millis = 0
    hours, remainder = divmod(total_millis, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def _procedure_display_name(procedure_id: str, active_bundle: str) -> str:
    """Keep the Live thyroidectomy title aligned with the 0704 package."""

    identity = f"{procedure_id} {active_bundle}".casefold()
    if "thyroidectomy_demo" in identity:
        return "갑상선절제술(시연)"
    if "thyroidectomy" in identity:
        return "갑상선절제술"
    return _compact(active_bundle or procedure_id or "수술 미기록", 120)


def _content_before_metadata(text: str, marker: str) -> str:
    return _compact(text.split(marker, 1)[0])


def _vlm_summary(text: str) -> str:
    """Keep the observed VLM sentence, not its transport/debug metadata."""

    marker = "summary="
    if marker not in text:
        return ""
    summary = _compact(text.rsplit(marker, 1)[1], MAX_EVENT_TEXT)
    return "" if summary == "-" else summary


def _event_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for fragment in text.split(" | "):
        key, separator, value = fragment.partition("=")
        if separator:
            fields[key.strip()] = value.strip()
    return fields


def _assistant_detail(event_text: str) -> str:
    """Map only completed, human-meaningful tool actions into 0704 rows."""

    fields = _event_fields(event_text)
    event = fields.get("event", "")
    tool = fields.get("tool", "")
    route = fields.get("route", "")
    if not tool or tool == "-":
        return ""
    if event == "ToolHandoverCompleted":
        return f"집도의에게 도구 전달 | {tool} | 로봇 → 집도의"
    if event == "UnusedPrepositionReturned":
        return f"미사용 도구 원위치 | {tool} | 로봇 → 메이요 스탠드"
    if event == "ToolReturned":
        destination = "메이요 스탠드" if "mayo" in route.casefold() else "기구대"
        return f"도구 회수 | {tool} | 로봇 → {destination}"
    return ""


def _status_response_preview(result: dict[str, Any]) -> tuple[str, bool]:
    """Return a bounded, already-redacted server response for the UI.

    ``SurgeryRecordRuntime`` redacts credential-shaped fields before exposing
    ``response_json`` or ``response_text``.  This owner only serializes that
    safe representation and applies a second strict byte bound for rosbridge.
    The browser receives the original response shape when it fits, rather than
    a Taskplanner-specific interpretation of a server response that may evolve.
    """

    response_json = result.get("response_json")
    if response_json is not None:
        try:
            raw = json.dumps(
                response_json,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                default=str,
            )
        except (TypeError, ValueError):
            raw = str(response_json)
    else:
        raw = str(result.get("response_text") or "")
    encoded = raw.encode("utf-8")
    if len(encoded) <= MAX_STATUS_RESPONSE_BYTES:
        return raw, False
    suffix = "\n…"
    preview_limit = MAX_STATUS_RESPONSE_BYTES - len(suffix.encode("utf-8"))
    preview = encoded[:preview_limit].decode("utf-8", errors="ignore")
    return f"{preview}{suffix}", True


def _public_receipt_excludes_field(value: Any) -> bool:
    """Keep transport and credential-shaped response fields off the public topic."""

    normalized = re.sub(r"[^a-z0-9]", "", str(value).lower())
    if normalized in {
        "endpoint",
        "defaultendpoint",
        "allowedendpoints",
        "outputpath",
        "inputdir",
    }:
        return True
    return (
        normalized == "auth"
        or normalized.startswith("auth")
        or any(
            marker in normalized
            for marker in ("apikey", "token", "secret", "password", "credential")
        )
    )


def _public_receipt_safe_value(value: Any) -> Any:
    """Drop fields that must not cross the read-only receipt boundary."""

    if isinstance(value, dict):
        return {
            str(key): _public_receipt_safe_value(item)
            for key, item in value.items()
            if not _public_receipt_excludes_field(key)
        }
    if isinstance(value, list):
        return [_public_receipt_safe_value(item) for item in value]
    return value


_PUBLIC_RECEIPT_PRIVATE_TEXT_FIELD = re.compile(
    r"""(?ix)
    (?P<field>
        [\"']?(?:api[_-]?key|token|secret|password|credential|authorization|auth|
        endpoint|output[_-]?path)[\"']?
    )
    (?P<separator>\s*[:=]\s*)
    (?P<value>\"(?:\\.|[^\"])*\"|'(?:\\.|[^'])*'|[^\s,;}\]]+)
    """
)


def _public_receipt_safe_text(
    value: Any,
    *,
    endpoint: Any = "",
    output_path: Any = "",
) -> str:
    """Retain a plain-text response while removing known private values."""

    safe = str(value or "")
    for private_value in (str(endpoint or "").strip(), str(output_path or "").strip()):
        if private_value:
            safe = safe.replace(private_value, "[REDACTED]")
    return _PUBLIC_RECEIPT_PRIVATE_TEXT_FIELD.sub(
        lambda match: f'{match.group("field")}{match.group("separator")}"[REDACTED]"',
        safe,
    )


def _public_receipt_response_preview(
    result: dict[str, Any],
    *,
    output_path: Any = "",
) -> tuple[str, bool]:
    """Reuse the bounded status preview after receipt-specific field removal."""

    safe_result = dict(result)
    response_json = result.get("response_json")
    if response_json is not None:
        safe_result["response_json"] = _public_receipt_safe_value(response_json)
    else:
        safe_result["response_text"] = _public_receipt_safe_text(
            result.get("response_text"),
            endpoint=result.get("endpoint"),
            output_path=output_path,
        )
    return _status_response_preview(safe_result)


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


def parse_successful_terminal_receipt(value: Any) -> dict[str, Any] | None:
    """Return one settled, run-bound Stop/Completed lifecycle receipt."""

    payload = _json_object(value)
    if payload.get("schema") != TERMINAL_RECEIPT_SCHEMA:
        return None
    if payload.get("success") is not True:
        return None
    terminal_kind = str(payload.get("terminal_kind") or "").strip()
    expected_state = TERMINAL_EXECUTION_STATE_BY_KIND.get(terminal_kind)
    if expected_state is None or payload.get("execution_state") != expected_state:
        return None
    procedure_run_id = str(payload.get("procedure_run_id") or "").strip()
    procedure_id = str(payload.get("procedure_id") or "").strip()
    active_bundle = str(payload.get("active_bundle") or "").strip()
    if not procedure_run_id or not (procedure_id or active_bundle):
        return None
    return {
        "procedure_run_id": procedure_run_id[:128],
        "procedure_id": procedure_id[:64],
        "active_bundle": active_bundle[:64],
        "terminal_kind": terminal_kind,
        "execution_state": expected_state,
        "message": _compact(payload.get("message"), 300),
    }


class SurgeryRecordSession:
    """Bounded in-memory timeline for one running procedure."""

    def __init__(self, *, max_events: int = MAX_EVENTS) -> None:
        self._max_events = max(10, int(max_events))
        self.active = False
        self.procedure_id = ""
        self.procedure_run_id = ""
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
        procedure_run_id: str,
        active_bundle: str,
        phase_id: str,
        now: datetime,
        now_monotonic: float,
    ) -> None:
        self.active = True
        self.procedure_id = _compact(procedure_id, 64)
        self.procedure_run_id = _compact(procedure_run_id, 128)
        self.active_bundle = _compact(active_bundle, 64)
        run_digest = hashlib.sha256(self.procedure_run_id.encode("utf-8")).hexdigest()
        self.surgery_code = f"THY-{run_digest[:24]}"
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

    def abandon(self) -> str:
        """Close an interrupted/reset session without creating a record."""

        run_id = self.procedure_run_id
        self.active = False
        return run_id

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

    def observe_utterance(
        self, msg: Any, *, now_monotonic: float | None = None
    ) -> None:
        """Append a final observed utterance without deriving command intent.

        The record timeline is an observer.  It intentionally does not read
        resolver proposals or execution intents, so it cannot turn an old
        command contract into a second voice admission path.
        """

        if not self.active or not bool(getattr(msg, "is_final", False)):
            return
        utterance_id = _compact(getattr(msg, "utterance_id", ""), 128)
        text = _compact(getattr(msg, "text", ""), 500)
        if not utterance_id or not text:
            return
        source = _compact(getattr(msg, "source", ""), 80)
        speaker_role = _compact(getattr(msg, "speaker_role", ""), 80)
        confidence = (
            f"{float(getattr(msg, 'confidence', 0.0) or 0.0):.3f}"
            if bool(getattr(msg, "has_confidence", False))
            else "-"
        )
        self.append(
            "집도의 음성",
            (
                f"{text} | source={source or '-'} | "
                f"speaker={speaker_role or '-'} | confidence={confidence}"
            ),
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
        terminal_kind: str,
        execution_state: str,
        terminal_message: str,
    ) -> dict[str, str] | None:
        if not self.active or self.started_at is None or not self.surgery_code:
            return None
        self.append(
            "수술 종료",
            (
                f"{terminal_kind} confirmed | state={execution_state} | "
                f"{terminal_message or 'lifecycle settled'}"
            ),
            now_monotonic=ended_monotonic,
            event_key=("terminal", self.procedure_run_id, terminal_kind),
        )
        rows: list[tuple[float, int, str, str]] = []
        initial_phase_added = False
        for index, event in enumerate(self.events):
            elapsed = max(0.0, float(event["elapsed_sec"]))
            kind = str(event["kind"])
            event_text = str(event["text"])
            phase = _compact(event["phase_id"] or "미확인", 120)
            if kind == "수술 시작":
                rows.append((elapsed, index, "Phase", f"초기 수술 단계 | {phase}"))
                initial_phase_added = True
            elif kind == "단계 전환":
                rows.append(
                    (elapsed, index, "Phase", f"수술 단계 전환 | {_compact(event_text)}")
                )
            elif kind == "집도의 음성":
                speech = _content_before_metadata(event_text, " | source=")
                if speech:
                    rows.append((elapsed, index, "집도의", speech))
            elif kind == "집도의 이벤트":
                speech = _content_before_metadata(event_text, " | event=")
                if speech:
                    rows.append((elapsed, index, "집도의", speech))
            elif kind == "VLM 관측":
                summary = _vlm_summary(event_text)
                if summary:
                    rows.append((elapsed, index, "VLM", summary))
            elif kind == "도구/트윈 이벤트":
                detail = _assistant_detail(event_text)
                if detail:
                    rows.append((elapsed, index, "어시스턴트", detail))
            elif kind == "수술 종료":
                rows.append((elapsed, index, "Phase", f"수술 종료 | {_compact(event_text)}"))

        if not initial_phase_added:
            rows.append((0.0, -1, "Phase", f"초기 수술 단계 | {self.current_phase or '미확인'}"))
        rows.sort(key=lambda row: (row[0], row[1]))

        terminal_rows = [
            row
            for row in rows
            if row[2] == "Phase" and row[3].startswith("수술 종료 | ")
        ]
        non_terminal_rows = [row for row in rows if row not in terminal_rows]
        non_vlm_rows = [row for row in non_terminal_rows if row[2] != "VLM"]
        vlm_rows = [row for row in non_terminal_rows if row[2] == "VLM"]

        def render_rows(selected_rows: list[tuple[float, int, str, str]]) -> list[str]:
            return [
                f"[{_format_relative_timestamp(elapsed)}] {source} | {detail}"
                for elapsed, _, source, detail in sorted(
                    selected_rows,
                    key=lambda row: (row[0], row[1]),
                )
            ]

        def render_document(
            selected_rows: list[tuple[float, int, str, str]],
            marker_row: tuple[float, int, str, str] | None = None,
        ) -> str:
            # These counts intentionally use only retained factual rows.  The
            # omission marker is a transport note, not a VLM observation.
            phase_changes = sum(
                1
                for _, _, source, detail in selected_rows
                if source == "Phase" and detail.startswith("수술 단계 전환 | ")
            )
            surgeon_count = sum(
                1 for _, _, source, _ in selected_rows if source == "집도의"
            )
            vlm_count = sum(
                1 for _, _, source, _ in selected_rows if source == "VLM"
            )
            assistant_count = sum(
                1
                for _, _, source, _ in selected_rows
                if source in {"스크럽 널스", "어시스턴트"}
            )
            lines = [
                "갑상선절제술 수술 타임라인",
                "=" * 40,
                f"수술: {_procedure_display_name(self.procedure_id, self.active_bundle)}",
                f"Case: {self.surgery_code}",
                f"Run: {self.procedure_run_id}",
                "VLM: Taskplanner 관측 VLM",
                "",
                "[기록 요약]",
                f"집도의 발화: {surgeon_count}건",
                f"VLM 관찰: {vlm_count}건",
                f"Phase 변화: {phase_changes}건",
                f"스크럽 널스·어시스턴트 행동: {assistant_count}건",
                "",
                "[시간순 기록]",
            ]
            timeline_rows = [*selected_rows]
            if marker_row is not None:
                timeline_rows.append(marker_row)
            timeline_rows.extend(terminal_rows)
            return "\n".join([*lines, *render_rows(timeline_rows)]) + "\n"

        def fits_budget(
            selected_rows: list[tuple[float, int, str, str]],
            marker_row: tuple[float, int, str, str] | None = None,
        ) -> bool:
            return (
                _utf8_bytes(render_document(selected_rows, marker_row))
                <= MAX_GENERATED_RECORD_BYTES
            )

        def first_unselected(
            candidates: list[tuple[float, int, str, str]],
            selected_rows: list[tuple[float, int, str, str]],
        ) -> tuple[float, int, str, str] | None:
            selected_indexes = {row[1] for row in selected_rows}
            return next(
                (row for row in candidates if row[1] not in selected_indexes),
                None,
            )

        def omission_marker(
            omitted_row: tuple[float, int, str, str] | None,
        ) -> tuple[float, int, str, str] | None:
            if omitted_row is None:
                return None
            return (
                omitted_row[0],
                omitted_row[1],
                "VLM",
                RECORD_BUDGET_OMISSION_MARKER,
            )

        # Reserve the clear omission marker while retaining all non-VLM facts
        # that fit. The terminal Phase row is never a candidate and is always
        # included by render_document.
        marker_probe = (0.0, 0, "VLM", RECORD_BUDGET_OMISSION_MARKER)
        selected_non_vlm: list[tuple[float, int, str, str]] = []
        first_omitted: tuple[float, int, str, str] | None = None
        for candidate_row in non_vlm_rows:
            if fits_budget([*selected_non_vlm, candidate_row], marker_probe):
                selected_non_vlm.append(candidate_row)
            elif first_omitted is None:
                first_omitted = candidate_row

        # Prefer a deterministic spread of VLM observations, so a long run
        # retains evidence from its beginning, middle, and end rather than
        # merely its first observations.
        target_vlm_count = min(len(vlm_rows), MAX_GENERATED_VLM_ROWS)
        selected_vlm = _representative_rows(vlm_rows, target_vlm_count)
        selected_rows = [*selected_non_vlm, *selected_vlm]
        omitted_vlm = first_unselected(vlm_rows, selected_vlm)
        marker = omission_marker(first_omitted or omitted_vlm)

        if not fits_budget(selected_rows, marker):
            # The summary text itself can be verbose. Keep the largest useful
            # representative VLM sample that fits, compacting each retained
            # detail to a fair UTF-8 budget before reducing the sample size.
            selected_vlm = []
            marker = None
            for count in range(target_vlm_count, -1, -1):
                candidate_vlm = _representative_rows(vlm_rows, count)
                candidate_omitted = first_omitted or first_unselected(
                    vlm_rows,
                    candidate_vlm,
                )
                candidate_marker = omission_marker(candidate_omitted)
                base_rows = [*selected_non_vlm]
                if count == 0:
                    if fits_budget(base_rows, candidate_marker):
                        marker = candidate_marker
                        break
                    continue

                empty_detail_rows = [
                    (elapsed, index, source, "")
                    for elapsed, index, source, _ in candidate_vlm
                ]
                base_text = render_document(
                    [*base_rows, *empty_detail_rows],
                    candidate_marker,
                )
                available_detail_bytes = (
                    MAX_GENERATED_RECORD_BYTES - _utf8_bytes(base_text)
                )
                detail_budget = min(
                    MAX_COMPACTED_VLM_DETAIL_BYTES,
                    available_detail_bytes // count,
                )
                if detail_budget < MIN_COMPACTED_VLM_DETAIL_BYTES:
                    continue
                compacted_vlm = [
                    (
                        elapsed,
                        index,
                        source,
                        _truncate_utf8(detail, detail_budget),
                    )
                    for elapsed, index, source, detail in candidate_vlm
                ]
                candidate_rows = [*base_rows, *compacted_vlm]
                if fits_budget(candidate_rows, candidate_marker):
                    selected_vlm = compacted_vlm
                    marker = candidate_marker
                    break

        text = render_document([*selected_non_vlm, *selected_vlm], marker)
        if _utf8_bytes(text) > MAX_GENERATED_RECORD_BYTES:
            # The source field bounds make this defensive fallback unreachable
            # in normal operation. It preserves the 0704 header and terminal
            # row even if future input fields unexpectedly grow.
            text = render_document([], marker or marker_probe)
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
        self._response_archive_dir = Path(
            os.environ.get(
                "TASKPLANNER_SURGERY_RECORD_RESPONSE_DIR",
                str(self._output_dir.parent / "responses"),
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
            scan_examples=False,
            response_archive_dir=self._response_archive_dir,
            # A HTTP 201 receipt without a generated summary is a known
            # semantic failure for the operational record path.  The runtime
            # reports it distinctly so this owner can make one safe compact
            # retry below.
            require_nonempty_summary=True,
        )
        self._last_output_path = ""
        self._last_record_text = ""
        self._last_submitted_surgery_code = ""
        self._last_request_id = ""
        self._last_error = ""
        self._pending_compact_retry: dict[str, str] | None = None
        self._compact_retry_attempted = False
        self._last_runtime_event_state = ""
        self._last_public_receipt_key: tuple[str, str, str] | None = None
        self._closed_run_ids: deque[str] = deque(maxlen=32)
        self._pending_terminal_receipt_run_id = ""
        self._pending_terminal_receipt_deadline_monotonic = 0.0
        self.declare_parameter(
            "utterance_topic",
            os.environ.get(
                "TASKPLANNER_SURGERY_RECORD_UTTERANCE_TOPIC",
                OBSERVED_UTTERANCE_TOPIC,
            ),
        )
        self._utterance_topic = str(
            self.get_parameter("utterance_topic").value
        ).strip() or OBSERVED_UTTERANCE_TOPIC
        self.declare_parameter(
            "terminal_receipt_topic",
            os.environ.get(
                "TASKPLANNER_SURGERY_RECORD_TERMINAL_RECEIPT_TOPIC",
                TERMINAL_RECEIPT_TOPIC,
            ),
        )
        self._terminal_receipt_topic = str(
            self.get_parameter("terminal_receipt_topic").value
        ).strip() or TERMINAL_RECEIPT_TOPIC

        self._status_pub = self.create_publisher(
            String,
            STATUS_TOPIC,
            10,
        )
        self._public_receipt_pub = self.create_publisher(
            String,
            PUBLIC_RECEIPT_TOPIC,
            PUBLIC_RECEIPT_QOS,
        )
        self.create_subscription(
            SimulationState, "/simulation/state", self._on_simulation, 50
        )
        self.create_subscription(
            SpeechUtterance, self._utterance_topic, self._on_utterance, 50
        )
        terminal_receipt_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        self.create_subscription(
            String,
            self._terminal_receipt_topic,
            self._on_terminal_receipt,
            terminal_receipt_qos,
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
            "operational surgery-record owner enabled; pause is non-terminal; "
            "submission requires a settled stop/completed receipt; "
            f"terminal_topic={self._terminal_receipt_topic}; "
            "submission_source=observed timeline"
        )

    def _clear_pending_terminal_receipt(self) -> None:
        self._pending_terminal_receipt_run_id = ""
        self._pending_terminal_receipt_deadline_monotonic = 0.0

    def _abandon_active_session(self, reason: str) -> None:
        """Close an unreceipted run without POSTing it."""

        abandoned = self._session.abandon()
        self._clear_pending_terminal_receipt()
        if abandoned:
            self._closed_run_ids.append(abandoned)
        self._last_error = reason.format(run_id=abandoned)
        self.get_logger().warning(self._last_error)

    def _expire_pending_terminal_receipt(self, now_mono: float) -> None:
        run_id = self._pending_terminal_receipt_run_id
        if not run_id or now_mono < self._pending_terminal_receipt_deadline_monotonic:
            return
        if self._session.active and self._session.procedure_run_id == run_id:
            self._abandon_active_session(
                "active surgery record abandoned without POST because the "
                "terminal receipt did not arrive before reset/idle: {run_id}"
            )
            return
        self._clear_pending_terminal_receipt()

    def _await_matching_terminal_receipt(self, run_id: str, now_mono: float) -> None:
        self._pending_terminal_receipt_run_id = run_id
        self._pending_terminal_receipt_deadline_monotonic = (
            now_mono + TERMINAL_RECEIPT_GRACE_SEC
        )

    def _on_simulation(self, msg: SimulationState) -> None:
        now_mono = self._monotonic()
        with self._lock:
            self._expire_pending_terminal_receipt(now_mono)
            execution_state = str(msg.execution_state or "").strip().casefold()
            run_id = str(msg.procedure_run_id or "").strip()
            running = bool(msg.running) and execution_state == "running"
            procedure = str(msg.procedure_id or msg.active_bundle or "").strip()
            if self._session.active:
                if run_id and run_id != self._session.procedure_run_id:
                    self._abandon_active_session(
                        "active surgery record abandoned without POST because a new "
                        "procedure_run_id arrived: {run_id}"
                    )
                elif (
                    not bool(msg.running)
                    and execution_state in TERMINAL_EXECUTION_STATE_BY_KIND.values()
                    and run_id == self._session.procedure_run_id
                ):
                    if self._pending_terminal_receipt_run_id != run_id:
                        self._await_matching_terminal_receipt(run_id, now_mono)
                elif running and run_id == self._session.procedure_run_id:
                    # A resumed run cannot still be awaiting a terminal
                    # receipt from an earlier transient terminal frame.
                    self._clear_pending_terminal_receipt()
                elif not bool(msg.running) and execution_state == "idle":
                    if (
                        self._pending_terminal_receipt_run_id
                        == self._session.procedure_run_id
                        and now_mono
                        < self._pending_terminal_receipt_deadline_monotonic
                    ):
                        # A settled terminal state arrived first, so this idle
                        # frame may be the automatic reset after it.  Do not
                        # lose the record while its durable receipt is in
                        # flight on the separate topic.
                        pass
                    else:
                        self._abandon_active_session(
                            "active surgery record abandoned without POST by "
                            "reset/idle: {run_id}"
                        )
            if (
                running
                and not self._session.active
                and run_id
                and run_id not in self._closed_run_ids
                and (
                    procedure in self._allowed_procedures
                    or str(msg.active_bundle) in self._allowed_procedures
                )
            ):
                self._session.start(
                    procedure_id=procedure,
                    procedure_run_id=run_id,
                    active_bundle=str(msg.active_bundle),
                    phase_id=str(msg.filtered_phase),
                    now=self._now(),
                    now_monotonic=now_mono,
                )
                self._clear_pending_terminal_receipt()
                self._last_record_text = ""
                self._last_submitted_surgery_code = ""
                self._pending_compact_retry = None
                self._compact_retry_attempted = False
                self._last_error = ""
            elif running and not run_id:
                self._last_error = (
                    "running surgery state has no procedure_run_id; record not started"
                )
            self._session.observe_simulation(msg, now_monotonic=now_mono)

    def _on_utterance(self, msg: SpeechUtterance) -> None:
        with self._lock:
            self._session.observe_utterance(msg, now_monotonic=self._monotonic())

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

    def _on_terminal_receipt(self, msg: String) -> None:
        receipt = parse_successful_terminal_receipt(msg.data)
        if receipt is None:
            return
        now_mono = self._monotonic()
        with self._lock:
            run_id = receipt["procedure_run_id"]
            if run_id in self._closed_run_ids:
                return
            if not self._session.active or run_id != self._session.procedure_run_id:
                return
            receipt_procedures = {
                value
                for value in (receipt["procedure_id"], receipt["active_bundle"])
                if value
            }
            session_procedures = {
                value
                for value in (self._session.procedure_id, self._session.active_bundle)
                if value
            }
            if not receipt_procedures & session_procedures:
                return
            self._clear_pending_terminal_receipt()
            record = self._session.finalize(
                ended_at=self._now(),
                ended_monotonic=now_mono,
                terminal_kind=receipt["terminal_kind"],
                execution_state=receipt["execution_state"],
                terminal_message=receipt["message"],
            )
            if record is None:
                return
            self._closed_run_ids.append(run_id)
            try:
                retry_record = _compact_retry_record(record)
            except Exception as exc:
                # A normal primary request is still valuable if a future
                # formatting change makes the compact renderer unavailable.
                retry_record = None
                self.get_logger().error(
                    "surgery-record compact retry was not prepared: "
                    f"{_compact(exc, 300)}"
                )
            try:
                path = self._save_local_record(record)
                self._last_output_path = str(path)
            except Exception as exc:
                self._last_error = _compact(exc, 500)
                self.get_logger().error(
                    f"surgery record could not be saved locally: {self._last_error}"
                )
                return
            try:
                payload = {
                    "room_name": self._room_name,
                    "surgery_code": record["surgery_code"],
                    "date": record["date"],
                    "text": record["text"],
                }
                # The saved file, POST body, and public receipt must all
                # describe this terminal run's same observed 0704-format TXT.
                self._last_record_text = record["text"]
                self._last_submitted_surgery_code = record["surgery_code"]
                self._pending_compact_retry = retry_record
                self._compact_retry_attempted = False
                request_id = self._runtime.submit_async(
                    payload
                )
            except Exception as exc:
                self._last_error = _compact(exc, 500)
                self.get_logger().error(
                    "surgery record was saved locally but POST was not started: "
                    f"{self._last_error}"
                )
                return
            self._last_request_id = request_id
            self._last_error = ""
            self.get_logger().info(
                f"surgery record saved locally and submitted: {record['surgery_code']}"
            )

    def _save_local_record(self, record: dict[str, str]) -> Path:
        self._output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._output_dir, 0o700)
        final_path = self._output_dir / f"{record['surgery_code']}.txt"
        temporary = self._output_dir / f".{record['surgery_code']}.{uuid4().hex}.tmp"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(record["text"])
            os.replace(temporary, final_path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return final_path

    def _start_compact_retry_if_needed(self, snapshot: dict[str, Any]) -> bool:
        """Submit exactly one small retry for the known empty-summary response.

        HTTP timeouts and other transport failures are deliberately excluded:
        their remote state is unknown and retrying could create a duplicate
        record.  A 201/``result=success`` response with an empty ``summary`` is
        different: it is a confirmed semantic-generation failure, and the
        service requires a fresh surgery code for a follow-up request.
        """

        result = dict(snapshot.get("last_result") or {})
        if (
            str(snapshot.get("state") or "") != "FAILED"
            or str(result.get("error_code") or "")
            != "EMPTY_GENERATED_SUMMARY"
        ):
            return False
        with self._lock:
            retry_record = self._pending_compact_retry
            if self._compact_retry_attempted or retry_record is None:
                return False
            self._compact_retry_attempted = True

            try:
                retry_path = self._save_local_record(retry_record)
                request_id = self._runtime.submit_async(
                    {
                        "room_name": self._room_name,
                        "surgery_code": retry_record["surgery_code"],
                        "date": retry_record["date"],
                        "text": retry_record["text"],
                    }
                )
            except Exception as exc:
                self._last_error = (
                    "empty-summary compact retry could not be started: "
                    f"{_compact(exc, 400)}"
                )
                self.get_logger().error(self._last_error)
                return False

            self._pending_compact_retry = None
            self._last_output_path = str(retry_path)
            self._last_record_text = retry_record["text"]
            self._last_submitted_surgery_code = retry_record["surgery_code"]
            self._last_request_id = request_id
            self._last_error = ""
            self.get_logger().warning(
                "surgery-record primary response had an empty summary; "
                f"submitted one compact 0704 retry: {retry_record['surgery_code']}"
            )
            return True

    def _status_payload(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Project the HTTPS outcome once for read-only browser observers."""

        result = dict(snapshot.get("last_result") or {})
        state = str(snapshot.get("state") or "IDLE")
        response, response_truncated = _status_response_preview(result)
        with self._lock:
            return {
                "schema": STATUS_SCHEMA,
                "stamp_sec": round(
                    self.get_clock().now().nanoseconds / 1_000_000_000.0,
                    6,
                ),
                "recording": self._session.active,
                "procedure_id": self._session.procedure_id,
                "procedure_run_id": self._session.procedure_run_id,
                "surgery_code": _compact(
                    result.get("surgery_code")
                    or self._last_submitted_surgery_code
                    or self._session.surgery_code,
                    64,
                ),
                "event_count": len(self._session.events),
                "output_path": self._last_output_path,
                "request_id": self._last_request_id,
                "submit_state": state,
                "success": bool(result.get("success", False)),
                "http_status": int(result.get("http_status") or 0),
                "receipt_id": _compact(result.get("receipt_id"), 128),
                "received_at": _compact(result.get("received_at"), 128),
                "completed_at": _compact(result.get("completed_at"), 128),
                "server_result": _compact(
                    (result.get("response_json") or {}).get("result", "")
                    if isinstance(result.get("response_json"), dict)
                    else "",
                    80,
                ),
                "response": response,
                "response_truncated": response_truncated,
                "error": _compact(snapshot.get("last_error") or self._last_error, 500),
            }

    def _public_receipt_payload(
        self, snapshot: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Return the single terminal HTTPS result safe for the public bridge."""

        result = dict(snapshot.get("last_result") or {})
        state = str(snapshot.get("state") or "IDLE")
        if state not in TERMINAL_POST_STATES:
            return None
        with self._lock:
            output_path = self._last_output_path
            response, response_truncated = _public_receipt_response_preview(
                result,
                output_path=output_path,
            )
            return {
                "schema": PUBLIC_RECEIPT_SCHEMA,
                "request_id": _compact(
                    result.get("request_id") or self._last_request_id,
                    128,
                ),
                "procedure_run_id": self._session.procedure_run_id,
                "surgery_code": _compact(
                    result.get("surgery_code")
                    or self._last_submitted_surgery_code
                    or self._session.surgery_code,
                    64,
                ),
                "record_text": self._last_record_text,
                "submit_state": state,
                "success": bool(result.get("success", False)),
                "http_status": int(result.get("http_status") or 0),
                "receipt_id": _compact(result.get("receipt_id"), 128),
                "received_at": _compact(result.get("received_at"), 128),
                "completed_at": _compact(result.get("completed_at"), 128),
                "server_result": _compact(
                    (result.get("response_json") or {}).get("result", "")
                    if isinstance(result.get("response_json"), dict)
                    else "",
                    80,
                ),
                "response": response,
                "response_truncated": response_truncated,
                "error": _compact(
                    _public_receipt_safe_text(
                        snapshot.get("last_error") or self._last_error,
                        endpoint=result.get("endpoint"),
                        output_path=output_path,
                    ),
                    500,
                ),
            }

    def _publish_public_receipt(self, snapshot: dict[str, Any]) -> None:
        """Publish one durable public receipt only after the POST settles."""

        payload = self._public_receipt_payload(snapshot)
        if payload is None:
            return
        receipt_key = (
            str(payload["request_id"]),
            str(payload["submit_state"]),
            str(payload["completed_at"]),
        )
        if receipt_key == self._last_public_receipt_key:
            return
        self._public_receipt_pub.publish(
            String(data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        )
        self._last_public_receipt_key = receipt_key

    def _publish_status(self) -> None:
        with self._lock:
            self._expire_pending_terminal_receipt(self._monotonic())
        snapshot = self._runtime.snapshot()
        # Do this before publishing a terminal receipt.  Browser consumers
        # therefore see the compact retry's final result instead of a false
        # "successful" receipt whose summary body was empty.
        if self._start_compact_retry_if_needed(snapshot):
            return
        result = dict(snapshot.get("last_result") or {})
        state = str(snapshot.get("state") or "IDLE")
        if state != self._last_runtime_event_state:
            self._last_runtime_event_state = state
            archive_error = _compact(
                result.get("response_archive_error"),
                400,
            )
            if archive_error:
                self.get_logger().error(
                    "surgery-record response archive was not saved: "
                    f"{archive_error}"
                )
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
        payload = self._status_payload(snapshot)
        self._status_pub.publish(
            String(data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        )
        self._publish_public_receipt(snapshot)

    def destroy_node(self) -> bool:
        wait_for_idle = getattr(self._runtime, "wait_for_idle", None)
        if callable(wait_for_idle):
            timeout_sec = float(
                os.environ.get("PUZZLE_SURGERY_RECORD_SHUTDOWN_WAIT_SEC", "40.0")
            )
            if not wait_for_idle(timeout_sec):
                self.get_logger().error(
                    "surgery-record owner stopped before the in-flight POST returned"
                )
        return super().destroy_node()


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
