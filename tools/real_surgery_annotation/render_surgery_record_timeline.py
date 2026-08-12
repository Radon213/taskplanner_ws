#!/usr/bin/env python3
"""Render a completed shadow replay trace as operative-note AI input text.

The output is an evidence timeline, not an operative note. It deliberately
keeps source surgeon speech, schema-v4 VLM clinical analysis, system phase
bookmarks, and counterfactual assistance events at separate authority levels.
Evaluation ground truth is never read into the output.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Iterable

import yaml


TRACE_SCHEMA = "taskplanner.shadow_trace.v1"
RUN_MANIFEST_SCHEMA = "taskplanner.shadow_run_manifest.v1"
VLM_CLINICAL_SCHEMA_VERSION = "4"
SOURCE_TRANSCRIPT_TOPIC = "/surgery/transcript"
VLM_RESULT_TOPIC = "/vlm/result"

NO_INFORMATION_CLINICAL_TEXTS = {
    "current visual evidence is insufficient for a definitive clinical interpretation",
}

SHADOW_TOOL_EVENT_LABELS = {
    "ToolPrepared": "도구 준비",
    "RobotGraspedTool": "도구 선택 및 전달 준비",
    "ToolHandoverCompleted": "집도의에게 도구 전달",
    "UnusedPrepositionReturned": "미사용 도구 원위치",
    "ToolReceivedFromSurgeon": "집도의에게서 도구 회수",
    "ToolSentToCleaner": "도구 세척 의뢰",
    "ToolCleaningCompleted": "도구 세척 완료",
    "ToolReturnedToTray": "도구 기구대 복귀",
}

FAILED_SKILL_STATES = {
    "aborted",
    "canceled",
    "dispatch_failed",
    "rejected",
    "result_failed",
    "server_unavailable",
}

GROUP_NAMES_KO = {"retraction": "리트랙션"}

GROUP_OPERATION_NAMES_KO = {
    "retraction": "조정",
    "change_end_effector": "도구 교환",
}

TERMINAL_STATE_LABELS_KO = {
    "aborted": "중단",
    "canceled": "취소",
    "dispatch_failed": "전달 실패",
    "rejected": "거절",
    "result_failed": "실패",
    "server_unavailable": "서비스 연결 실패",
}

DISPLAY_CATEGORY = {
    "집도의 원본 발화": "집도의",
    "VLM 임상 관찰 (모델 관찰·의료진 미확정)": "VLM",
    "Phase 상태 (시스템 추정·의료진 미확정)": "Phase",
    "Shadow 가상 도구 이벤트 (물리 실행 아님)": "스크럽 널스",
    "Shadow 가상 보조로봇 이벤트 (물리 실행 아님)": "어시스턴트",
    "Shadow 가상 실행 오류 (물리 실행 아님)": "실행 오류",
}

LOCATION_LABELS_KO = {
    "robot_left_hand": "스크럽 널스",
    "robot_right_hand": "스크럽 널스",
    "surgeon_hand": "집도의",
    "surgeon_receive_zone": "집도의",
    "operative_recipient": "집도의",
    "mayo_stand": "메이요 스탠드",
    "mayo_reuse_zone": "메이요 스탠드",
    "mayo_recovery_zone": "메이요 스탠드",
    "cleaner_slot": "세척 구역",
}

INTERNAL_CODE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])(?P<code>[TP]\d{2})(?:#(?P<instance>\d+))?(?![A-Za-z0-9_])"
)


@dataclass(frozen=True)
class TimelineItem:
    time_sec: float
    sequence: int
    category: str
    detail: str


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _compact_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _text_key(value: Any) -> str:
    return re.sub(r"[^\w가-힣]+", " ", _compact_text(value).casefold()).strip()


def _record_time(record: dict[str, Any]) -> float:
    source_stamp = record.get("source_stamp_sec")
    if isinstance(source_stamp, (int, float)) and float(source_stamp) > 0.0:
        return float(source_stamp)
    ros_time = record.get("ros_time_sec", 0.0)
    return float(ros_time) if isinstance(ros_time, (int, float)) else 0.0


def _sequence(record: dict[str, Any]) -> int:
    value = record.get("sequence", 0)
    return int(value) if isinstance(value, (int, float)) else 0


def _json_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def load_trace(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as source:
        for line_number, raw_line in enumerate(source, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"invalid trace JSON at {path}:{line_number}: {exc}"
                ) from exc
            if not isinstance(record, dict):
                raise ValueError(
                    f"trace row must be an object at {path}:{line_number}"
                )
            if record.get("schema") != TRACE_SCHEMA:
                raise ValueError(
                    f"unsupported trace schema at {path}:{line_number}: "
                    f"{record.get('schema')!r}"
                )
            records.append(record)
    return records


def load_procedure_labels(
    path: Path | None,
) -> tuple[str, dict[str, str], dict[str, str], dict[str, str]]:
    if path is None:
        return "", {}, {}, {}
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"procedure prompt must be a mapping: {path}")
    procedure = _mapping(payload.get("procedure"))
    phase_labels_en = _mapping(_mapping(payload.get("phase_labels")).get("normal"))
    phase_labels_ko = _mapping(_mapping(payload.get("phase_labels_ko")).get("normal"))
    tool_labels = _mapping(payload.get("tools"))
    return (
        _compact_text(procedure.get("ko") or procedure.get("name")),
        {str(key): _compact_text(value) for key, value in phase_labels_en.items()},
        {str(key): _compact_text(value) for key, value in phase_labels_ko.items()},
        {str(key): _compact_text(value) for key, value in tool_labels.items()},
    )


def _transcript_item(record: dict[str, Any]) -> TimelineItem | None:
    if (
        record.get("layer") != "input_transcript"
        or record.get("topic") != SOURCE_TRANSCRIPT_TOPIC
    ):
        return None
    raw_data = _mapping(record.get("payload")).get("data")
    transcript = _json_mapping(raw_data)
    text = _compact_text(transcript.get("text") or raw_data)
    if not text:
        return None
    start_sec = transcript.get("start_sec")
    time_sec = (
        float(start_sec)
        if isinstance(start_sec, (int, float))
        else _record_time(record)
    )
    return TimelineItem(
        time_sec=time_sec,
        sequence=_sequence(record),
        category="집도의 원본 발화",
        detail=text,
    )


def _display_text_without_internal_codes(
    value: Any,
    *,
    phase_labels: dict[str, str],
    tool_labels: dict[str, str],
) -> str:
    """Expand Taskplanner-only Pxx/Txx identifiers for the exported TXT."""

    text = _compact_text(value)

    def replacement(match: re.Match[str]) -> str:
        code = match.group("code")
        instance = match.group("instance")
        if code.startswith("T"):
            label = tool_labels.get(code, "Unregistered surgical instrument")
            return f"{label}, instance {instance}" if instance else label
        return phase_labels.get(code, "Unregistered surgical phase")

    return INTERNAL_CODE_PATTERN.sub(replacement, text)


def _vlm_clinical_item(
    record: dict[str, Any],
    *,
    phase_labels: dict[str, str],
    tool_labels: dict[str, str],
) -> TimelineItem | None:
    if record.get("layer") != "vlm_raw" or record.get("topic") != VLM_RESULT_TOPIC:
        return None
    payload = _mapping(record.get("payload"))
    if str(payload.get("schema_version", "")).strip() != VLM_CLINICAL_SCHEMA_VERSION:
        return None
    analysis = _display_text_without_internal_codes(
        payload.get("summary"),
        phase_labels=phase_labels,
        tool_labels=tool_labels,
    )
    if not analysis or _text_key(analysis) in NO_INFORMATION_CLINICAL_TEXTS:
        return None
    return TimelineItem(
        time_sec=_record_time(record),
        sequence=_sequence(record),
        category="VLM 임상 관찰 (모델 관찰·의료진 미확정)",
        detail=analysis,
    )


def _display_location(value: Any) -> str:
    location = _compact_text(value)
    if not location:
        return ""
    if location.startswith("main_tray_slot_"):
        return "메인 기구대"
    return LOCATION_LABELS_KO.get(location, location.replace("_", " "))


def _location_transition(payload: dict[str, Any]) -> str:
    source = _display_location(
        payload.get("source_location_id") or payload.get("source_location_type")
    )
    target = _display_location(
        payload.get("target_location_id")
        or payload.get("location_id")
        or payload.get("target_location_type")
        or payload.get("location_type")
    )
    if source and target and source != target:
        return f" | {source} → {target}"
    if target:
        return f" | 위치={target}"
    return ""


def _display_tool_identity(
    tool_id: str,
    instance_id: str,
    tool_labels: dict[str, str],
) -> str:
    tool_name = tool_labels.get(tool_id, "미등록 수술도구")
    instance_match = re.fullmatch(rf"{re.escape(tool_id)}#(\d+)", instance_id)
    if instance_match:
        return f"{tool_name} {instance_match.group(1)}번"
    return tool_name


def _shadow_tool_item(
    record: dict[str, Any],
    tool_labels: dict[str, str],
) -> TimelineItem | None:
    if record.get("layer") != "skill_event":
        return None
    payload = _mapping(record.get("payload"))
    event_type = _compact_text(payload.get("event_type"))
    if (
        event_type not in SHADOW_TOOL_EVENT_LABELS
        or _compact_text(payload.get("mode")) != "shadow_counterfactual"
    ):
        return None
    tool_id = _compact_text(payload.get("instrument_id"))
    instance_id = _compact_text(payload.get("instance_id"))
    identity = _display_tool_identity(tool_id, instance_id, tool_labels)
    detail = (
        f"{SHADOW_TOOL_EVENT_LABELS[event_type]} | {identity}"
        f"{_location_transition(payload)}"
    )
    return TimelineItem(
        time_sec=_record_time(record),
        sequence=_sequence(record),
        category="Shadow 가상 도구 이벤트 (물리 실행 아님)",
        detail=detail,
    )


def _shadow_failure_item(
    record: dict[str, Any],
    tool_labels: dict[str, str],
) -> TimelineItem | None:
    if record.get("layer") != "skill_status":
        return None
    payload = _mapping(record.get("payload"))
    state = _compact_text(payload.get("state")).casefold()
    if (
        state not in FAILED_SKILL_STATES
        or _compact_text(payload.get("mode")) != "shadow_counterfactual"
    ):
        return None
    action = _compact_text(payload.get("action")) or "작업"
    tool_id = _compact_text(payload.get("instrument_id"))
    message = _compact_text(payload.get("message"))
    detail = f"{action} 실패 | 상태={TERMINAL_STATE_LABELS_KO.get(state, state)}"
    if tool_id:
        detail += f" | 도구={tool_labels.get(tool_id, '미등록 수술도구')}"
    if message:
        detail += f" | {message}"
    return TimelineItem(
        time_sec=_record_time(record),
        sequence=_sequence(record),
        category="Shadow 가상 실행 오류 (물리 실행 아님)",
        detail=detail,
    )


def _group_command_item(record: dict[str, Any]) -> TimelineItem | None:
    if record.get("layer") != "bed_robot_arm_group_command":
        return None
    payload = _mapping(record.get("payload"))
    group_id = _compact_text(payload.get("group_id"))
    operation = _compact_text(payload.get("operation"))
    if group_id != "retraction" or not operation:
        return None
    group_name = GROUP_NAMES_KO.get(group_id, group_id)
    operation_name = GROUP_OPERATION_NAMES_KO.get(operation, operation)
    details = [f"행동 | {group_name} {operation_name}"]
    direction = _compact_text(payload.get("direction"))
    if direction:
        details.append(f"방향={direction}")
    distance = payload.get("distance_mm")
    if isinstance(distance, (int, float)) and float(distance) != 0.0:
        details.append(f"거리={float(distance):g} mm")
    return TimelineItem(
        time_sec=_record_time(record),
        sequence=_sequence(record),
        category="Shadow 가상 보조로봇 이벤트 (물리 실행 아님)",
        detail=" | ".join(details),
    )


def _group_terminal_item(record: dict[str, Any]) -> TimelineItem | None:
    if record.get("layer") != "bed_robot_arm_group_status":
        return None
    payload = _mapping(record.get("payload"))
    command_id = _compact_text(payload.get("command_id"))
    if not command_id or not bool(payload.get("terminal")):
        return None
    group_id = _compact_text(payload.get("group_id"))
    operation = _compact_text(payload.get("operation"))
    if group_id != "retraction":
        return None
    group_name = GROUP_NAMES_KO.get(group_id, "수술 보조")
    operation_name = GROUP_OPERATION_NAMES_KO.get(operation, operation or "작업")
    success = bool(payload.get("success"))
    outcome = _compact_text(payload.get("outcome"))
    state = _compact_text(payload.get("state"))
    result = "완료" if success else "실패"
    detail = f"행동 결과 | {group_name} {operation_name} {result}"
    terminal_state = (outcome or state).casefold()
    if terminal_state and terminal_state not in {"success", "succeeded", "completed"}:
        detail += f" | 상태={TERMINAL_STATE_LABELS_KO.get(terminal_state, terminal_state)}"
    return TimelineItem(
        time_sec=_record_time(record),
        sequence=_sequence(record),
        category="Shadow 가상 보조로봇 이벤트 (물리 실행 아님)",
        detail=detail,
    )


def collect_timeline_items(
    records: Iterable[dict[str, Any]],
    *,
    phase_labels: dict[str, str],
    phase_display_labels: dict[str, str],
    tool_labels: dict[str, str],
    clinical_repeat_window_sec: float = 30.0,
) -> tuple[list[TimelineItem], Counter[str], Counter[str]]:
    items: list[TimelineItem] = []
    exclusions: Counter[str] = Counter()
    last_phase = ""
    last_clinical_at: dict[str, float] = {}
    seen_terminal_group: set[tuple[str, str, str]] = set()
    seen_skill_failures: set[tuple[str, str]] = set()

    for record in records:
        layer = _compact_text(record.get("layer"))

        transcript = _transcript_item(record)
        if transcript is not None:
            items.append(transcript)
            continue
        if layer == "input_transcript":
            exclusions["non_source_transcript"] += 1
            continue

        if layer == "vlm_raw":
            clinical = _vlm_clinical_item(
                record,
                phase_labels=phase_labels,
                tool_labels=tool_labels,
            )
            if clinical is None:
                payload = _mapping(record.get("payload"))
                if str(payload.get("schema_version", "")).strip() != VLM_CLINICAL_SCHEMA_VERSION:
                    exclusions["vlm_non_v4"] += 1
                else:
                    exclusions["vlm_empty_or_no_information"] += 1
                continue
            key = _text_key(clinical.detail)
            previous = last_clinical_at.get(key)
            if previous is not None and clinical.time_sec - previous < clinical_repeat_window_sec:
                exclusions["vlm_exact_repeat"] += 1
                continue
            last_clinical_at[key] = clinical.time_sec
            items.append(clinical)
            continue

        if layer == "reducer_fused":
            phase_id = _compact_text(_mapping(record.get("payload")).get("filtered_phase"))
            if phase_id and phase_id != last_phase:
                label = phase_display_labels.get(phase_id, "미등록 수술 단계")
                previous_label = phase_display_labels.get(
                    last_phase, "미등록 수술 단계"
                )
                transition = (
                    f"수술 단계 전환 | {previous_label} → {label}"
                    if last_phase
                    else f"초기 수술 단계 | {label}"
                )
                items.append(
                    TimelineItem(
                        time_sec=_record_time(record),
                        sequence=_sequence(record),
                        category="Phase 상태 (시스템 추정·의료진 미확정)",
                        detail=transition,
                    )
                )
                last_phase = phase_id
            continue

        tool_item = _shadow_tool_item(record, tool_labels)
        if tool_item is not None:
            items.append(tool_item)
            continue

        failure_item = _shadow_failure_item(record, tool_labels)
        if failure_item is not None:
            payload = _mapping(record.get("payload"))
            key = (
                _compact_text(payload.get("command_id")),
                _compact_text(payload.get("state")),
            )
            if key not in seen_skill_failures:
                seen_skill_failures.add(key)
                items.append(failure_item)
            continue

        group_command = _group_command_item(record)
        if group_command is not None:
            items.append(group_command)
            continue

        group_terminal = _group_terminal_item(record)
        if group_terminal is not None:
            payload = _mapping(record.get("payload"))
            key = (
                _compact_text(payload.get("command_id")),
                _compact_text(payload.get("outcome")),
                _compact_text(payload.get("state")),
            )
            if key not in seen_terminal_group:
                seen_terminal_group.add(key)
                items.append(group_terminal)

    items.sort(key=lambda item: (item.time_sec, item.sequence, item.category))
    counts = Counter(item.category for item in items)
    return items, counts, exclusions


def _format_time(seconds: float) -> str:
    milliseconds = max(0, int(round(float(seconds) * 1000.0)))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def _artifact_identity(value: Any) -> str:
    artifact = _mapping(value)
    path = _compact_text(artifact.get("path"))
    digest = _compact_text(artifact.get("sha256"))
    if path and digest:
        return f"{path} (sha256={digest})"
    return path or digest or "미기록"


def build_surgery_record_timeline(
    manifest: dict[str, Any],
    records: Iterable[dict[str, Any]],
    *,
    procedure_name: str = "",
    phase_labels: dict[str, str] | None = None,
    phase_display_labels: dict[str, str] | None = None,
    tool_labels: dict[str, str] | None = None,
    clinical_repeat_window_sec: float = 30.0,
) -> str:
    if manifest.get("schema") != RUN_MANIFEST_SCHEMA:
        raise ValueError(f"unsupported run manifest schema: {manifest.get('schema')!r}")
    if manifest.get("status") != "complete":
        raise ValueError(
            "operative-note input is emitted only for a completed shadow replay"
        )
    runtime = _mapping(manifest.get("runtime"))
    vlm = _mapping(runtime.get("vlm"))
    shadow_execution = _mapping(runtime.get("shadow_execution"))
    if bool(shadow_execution.get("physical_execution_enabled")):
        raise ValueError("expected a non-physical shadow replay manifest")

    items, counts, exclusions = collect_timeline_items(
        records,
        phase_labels=phase_labels or {},
        phase_display_labels=phase_display_labels or phase_labels or {},
        tool_labels=tool_labels or {},
        clinical_repeat_window_sec=max(0.0, float(clinical_repeat_window_sec)),
    )
    run_id = _compact_text(manifest.get("run_id")) or "미기록"
    case_id = _compact_text(manifest.get("case_id")) or "미기록"
    bundle = _compact_text(runtime.get("bundle")) or "미기록"
    procedure = procedure_name or bundle
    vlm_count = counts["VLM 임상 관찰 (모델 관찰·의료진 미확정)"]
    transcript_count = counts["집도의 원본 발화"]
    phase_count = counts["Phase 상태 (시스템 추정·의료진 미확정)"]
    shadow_count = sum(
        count for category, count in counts.items() if category.startswith("Shadow ")
    )

    lines = [
        "갑상선절제술 수술 타임라인",
        "=" * 40,
        f"수술: {procedure}",
        f"Case: {case_id}",
        f"Run: {run_id}",
        f"VLM: {_compact_text(vlm.get('model_id')) or '미기록'}",
        "",
        "[기록 요약]",
        f"집도의 발화: {transcript_count}건",
        f"VLM 관찰: {vlm_count}건",
        f"Phase 변화: {phase_count}건",
        f"스크럽 널스·어시스턴트 행동: {shadow_count}건",
        "",
        "[시간순 기록]",
    ]
    lines.extend(
        (
            f"[{_format_time(item.time_sec)}] "
            f"{DISPLAY_CATEGORY.get(item.category, item.category)} | {item.detail}"
        )
        for item in items
    )
    lines.append("")
    return "\n".join(lines)


def render_surgery_record_timeline(
    *,
    manifest_path: Path,
    trace_path: Path,
    output_path: Path,
    procedure_prompt_path: Path | None = None,
    clinical_repeat_window_sec: float = 30.0,
) -> Path:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError(f"run manifest must be an object: {manifest_path}")
    records = load_trace(trace_path)
    procedure_name, phase_labels, phase_display_labels, tool_labels = (
        load_procedure_labels(procedure_prompt_path)
    )
    text = build_surgery_record_timeline(
        manifest,
        records,
        procedure_name=procedure_name,
        phase_labels=phase_labels,
        phase_display_labels=phase_display_labels,
        tool_labels=tool_labels,
        clinical_repeat_window_sec=clinical_repeat_window_sec,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(output_path)
    return output_path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--trace", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--procedure-prompt", type=Path)
    parser.add_argument("--clinical-repeat-window-sec", type=float, default=30.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    path = render_surgery_record_timeline(
        manifest_path=args.manifest,
        trace_path=args.trace,
        output_path=args.output,
        procedure_prompt_path=args.procedure_prompt,
        clinical_repeat_window_sec=args.clinical_repeat_window_sec,
    )
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
