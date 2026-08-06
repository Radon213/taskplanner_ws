from __future__ import annotations

import json
from pathlib import Path
import re

import pytest

from tools.real_surgery_annotation.render_surgery_record_timeline import (
    TRACE_SCHEMA,
    build_surgery_record_timeline,
    render_surgery_record_timeline,
)


def _manifest(*, status: str = "complete") -> dict:
    return {
        "schema": "taskplanner.shadow_run_manifest.v1",
        "run_id": "test-run",
        "case_id": "0704_6",
        "mode": "strict",
        "status": status,
        "source_bag": {"path": "/datasets/shadow/0704_6/test.mcap", "sha256": "abc"},
        "runtime": {
            "bundle": "thyroidectomy_demo",
            "shadow_execution": {"physical_execution_enabled": False},
            "vlm": {
                "provider_id": "ninfer",
                "base_url": "http://127.0.0.1:8080",
                "model_id": "qwen3.6-35b-a3b",
            },
        },
    }


def _record(
    layer: str,
    payload: dict,
    *,
    time_sec: float,
    sequence: int,
    topic: str = "",
) -> dict:
    return {
        "schema": TRACE_SCHEMA,
        "run_id": "test-run",
        "mode": "strict",
        "layer": layer,
        "topic": topic,
        "message_type": "test/msg/Type",
        "payload": payload,
        "ros_time_sec": time_sec,
        "sequence": sequence,
    }


def test_timeline_uses_only_source_transcript_and_schema_v4_clinical_summary() -> None:
    clinical = "The recurrent laryngeal nerve is visible beside the thyroid tissue."
    records = [
        _record(
            "input_transcript",
            {"data": json.dumps({"start_sec": 3.2, "text": "Adson 주세요"})},
            time_sec=3.3,
            sequence=1,
            topic="/surgery/transcript",
        ),
        _record(
            "input_transcript",
            {"data": "Adson 주세요"},
            time_sec=3.5,
            sequence=2,
            topic="/surgery/audio/request_text",
        ),
        _record(
            "vlm_raw",
            {
                "schema_version": "4",
                "summary": clinical,
                "raw_json": "FORBIDDEN_RAW_JSON",
                "phase_ids": ["FORBIDDEN_PHASE_CANDIDATE"],
                "observed_tool_ids": ["FORBIDDEN_TOOL_CANDIDATE"],
                "gesture_event_type": "FORBIDDEN_GESTURE",
                "uncertainty": 0.9,
            },
            time_sec=4.0,
            sequence=3,
            topic="/vlm/result",
        ),
        _record(
            "vlm_raw",
            {"schema_version": "3", "summary": "FORBIDDEN_SCHEMA_V3"},
            time_sec=4.5,
            sequence=4,
            topic="/vlm/result",
        ),
        _record(
            "vlm_model_raw",
            {"response_text": "FORBIDDEN_MODEL_RAW"},
            time_sec=4.7,
            sequence=5,
            topic="/vlm/model_raw_result",
        ),
        _record(
            "evaluation_ground_truth",
            {"secret": "FORBIDDEN_GROUND_TRUTH"},
            time_sec=5.0,
            sequence=6,
            topic="/shadow/ground_truth/events",
        ),
    ]

    text = build_surgery_record_timeline(_manifest(), records)

    assert text.count("Adson 주세요") == 1
    assert clinical in text
    for forbidden in (
        "FORBIDDEN_RAW_JSON",
        "FORBIDDEN_PHASE_CANDIDATE",
        "FORBIDDEN_TOOL_CANDIDATE",
        "FORBIDDEN_GESTURE",
        "FORBIDDEN_SCHEMA_V3",
        "FORBIDDEN_MODEL_RAW",
        "FORBIDDEN_GROUND_TRUTH",
    ):
        assert forbidden not in text


def test_timeline_deduplicates_exact_clinical_repeat_and_phase_state() -> None:
    summary = "The thyroid tissue is exposed under fixed retraction."
    records = [
        _record(
            "vlm_raw",
            {"schema_version": "4", "summary": summary},
            time_sec=1.0,
            sequence=1,
            topic="/vlm/result",
        ),
        _record(
            "vlm_raw",
            {"schema_version": "4", "summary": summary},
            time_sec=10.0,
            sequence=2,
            topic="/vlm/result",
        ),
        _record(
            "vlm_raw",
            {"schema_version": "4", "summary": summary},
            time_sec=35.0,
            sequence=3,
            topic="/vlm/result",
        ),
        _record(
            "reducer_fused",
            {"filtered_phase": "P03"},
            time_sec=0.0,
            sequence=4,
        ),
        _record(
            "reducer_fused",
            {"filtered_phase": "P03"},
            time_sec=2.0,
            sequence=5,
        ),
        _record(
            "reducer_fused",
            {"filtered_phase": "P04"},
            time_sec=20.0,
            sequence=6,
        ),
    ]

    text = build_surgery_record_timeline(
        _manifest(),
        records,
        phase_labels={
            "P03": "Central-field dissection before fixed retraction",
            "P04": "Fixed retraction and exposure establishment",
        },
        phase_display_labels={
            "P03": "중앙 수술야 박리",
            "P04": "고정 견인 및 노출",
        },
    )

    assert text.count(summary) == 2
    assert text.count("초기 수술 단계 | 중앙 수술야 박리") == 1
    assert text.count("수술 단계 전환 | 중앙 수술야 박리 → 고정 견인 및 노출") == 1
    assert "P03" not in text
    assert "P04" not in text
    assert "VLM 관찰: 2건" in text


def test_exported_roles_hide_robot_implementation_and_internal_codes() -> None:
    records = [
        _record(
            "vlm_raw",
            {
                "schema_version": "4",
                "summary": "T04 is active during P03 while T02#2 holds tissue.",
            },
            time_sec=6.0,
            sequence=1,
            topic="/vlm/result",
        ),
        _record(
            "skill_event",
            {
                "event_type": "ToolHandoverCompleted",
                "mode": "shadow_counterfactual",
                "instrument_id": "T02",
                "instance_id": "T02#1",
                "source_location_id": "robot_right_hand",
                "target_location_id": "surgeon_receive_zone",
            },
            time_sec=8.0,
            sequence=2,
            topic="/skill/events",
        ),
        _record(
            "bed_robot_arm_group_command",
            {"group_id": "suction", "operation": "suction_start"},
            time_sec=12.0,
            sequence=3,
            topic="/bt/bed_robot_arm_group_command",
        ),
    ]

    text = build_surgery_record_timeline(
        _manifest(),
        records,
        phase_labels={"P03": "Central-field dissection before fixed retraction"},
        tool_labels={
            "T02": "Adson forceps",
            "T04": "Bovie surgical cautery",
        },
    )

    assert "스크럽 널스 | 집도의에게 도구 전달 | Adson forceps 1번" in text
    assert "스크럽 널스 → 집도의" in text
    assert "어시스턴트 | 행동 | 석션 시작" in text
    assert (
        "Bovie surgical cautery is active during "
        "Central-field dissection before fixed retraction"
    ) in text
    assert "Adson forceps, instance 2 holds tissue" in text
    vlm_lines = [line for line in text.splitlines() if "] VLM |" in line]
    assert all(not re.search(r"[가-힣]", line) for line in vlm_lines)
    assert not re.search(r"\b[TP]\d{2}(?:#\d+)?\b", text)
    assert "robot" not in text.casefold()
    assert "로봇" not in text
    assert "도구 이벤트" not in text
    assert "보조로봇" not in text


def test_incomplete_replay_is_rejected() -> None:
    with pytest.raises(ValueError, match="completed shadow replay"):
        build_surgery_record_timeline(_manifest(status="failed"), [])


def test_renderer_writes_utf8_text_from_completed_artifacts(tmp_path: Path) -> None:
    manifest_path = tmp_path / "run_manifest.json"
    trace_path = tmp_path / "shadow_trace.v1.jsonl"
    output_path = tmp_path / "surgery_record_input.txt"
    prompt_path = tmp_path / "vlm_procedure_prompt.yaml"
    manifest_path.write_text(
        json.dumps(_manifest(), ensure_ascii=False), encoding="utf-8"
    )
    trace_path.write_text(
        json.dumps(
            _record(
                "input_transcript",
                {"data": json.dumps({"start_sec": 1.0, "text": "수술 시작"})},
                time_sec=1.0,
                sequence=1,
                topic="/surgery/transcript",
            ),
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    prompt_path.write_text(
        "procedure:\n  ko: 갑상선절제술(시연)\nphase_labels_ko:\n  normal: {}\ntools: {}\n",
        encoding="utf-8",
    )

    rendered = render_surgery_record_timeline(
        manifest_path=manifest_path,
        trace_path=trace_path,
        output_path=output_path,
        procedure_prompt_path=prompt_path,
    )

    assert rendered == output_path
    assert "갑상선절제술(시연)" in output_path.read_text(encoding="utf-8")
    assert "수술 시작" in output_path.read_text(encoding="utf-8")
