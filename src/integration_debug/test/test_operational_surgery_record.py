from __future__ import annotations

from datetime import datetime
import json
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import rclpy
from std_msgs.msg import String
from surgical_msgs.msg import SimulationState, VoiceCommandIntent

from integration_debug.operational_surgery_record import (
    CONTROL_RESULT_SCHEMA,
    OperationalSurgeryRecordNode,
    SurgeryRecordSession,
    parse_successful_voice_pause,
)
from integration_debug.surgery_record_runtime import MAX_TEXT_CHARS


SEOUL = ZoneInfo("Asia/Seoul")


class FakeRuntime:
    def __init__(self) -> None:
        self.submissions: list[dict[str, str]] = []

    def submit_async(self, payload: dict[str, str]) -> str:
        self.submissions.append(dict(payload))
        return "record-test-1"

    def snapshot(self) -> dict[str, object]:
        return {
            "state": "SUBMITTING" if self.submissions else "IDLE",
            "last_result": {},
            "last_error": "",
        }


def _control_result(**overrides) -> str:
    payload = {
        "schema": CONTROL_RESULT_SCHEMA,
        "utterance_id": "voice-end-1",
        "procedure_id": "thyroidectomy_demo",
        "requested_command": "pause",
        "success": True,
        "message": "simulation paused",
        "running": True,
        "execution_state": "paused",
    }
    payload.update(overrides)
    return json.dumps(payload)


def test_only_successful_pause_receipt_can_trigger_submission() -> None:
    assert parse_successful_voice_pause(_control_result()) == {
        "utterance_id": "voice-end-1",
        "procedure_id": "thyroidectomy_demo",
        "message": "simulation paused",
    }
    assert parse_successful_voice_pause(_control_result(success=False)) is None
    assert parse_successful_voice_pause(_control_result(requested_command="stop")) is None
    assert parse_successful_voice_pause(_control_result(execution_state="running")) is None
    assert parse_successful_voice_pause("not-json") is None


def test_session_collects_live_facts_and_renders_api_text() -> None:
    session = SurgeryRecordSession()
    started = datetime(2026, 8, 24, 9, 30, tzinfo=SEOUL)
    session.start(
        procedure_id="thyroidectomy_demo",
        active_bundle="thyroidectomy_demo",
        phase_id="phase_1",
        now=started,
        now_monotonic=100.0,
    )
    session.observe_simulation(
        SimpleNamespace(
            filtered_phase="phase_2",
            robot_state="idle",
            right_hand_tool="Adson forceps",
            left_hand_tool="",
            active_robot_task_type="handover",
            active_robot_task_tool_id="Adson forceps",
        ),
        now_monotonic=105.0,
    )
    session.observe_voice(
        SimpleNamespace(
            source_is_final=True,
            utterance_id="voice-1",
            raw_text="애드슨 포셉 주세요",
            intent="tool_handover",
            disposition="propose",
        ),
        now_monotonic=106.0,
    )
    session.observe_twin_event(
        SimpleNamespace(
            stamp=SimpleNamespace(sec=10, nanosec=20),
            event_type="ToolHandoverCompleted",
            instrument_id="Adson forceps",
            instance_id="Adson forceps#1",
            phase_id="phase_2",
            detail_json='{"from":"mayo","to":"surgeon"}',
            source_location_id="",
            target_location_id="",
            status="completed",
        ),
        now_monotonic=107.0,
    )

    record = session.finalize(
        ended_at=datetime(2026, 8, 24, 9, 45, tzinfo=SEOUL),
        ended_monotonic=1_000.0,
        utterance_id="voice-end-1",
        control_message="simulation paused",
    )

    assert record is not None
    assert record["surgery_code"].startswith("THY-20260824-093000-")
    assert record["date"] == "2026-08-24"
    assert "갑상선절제술 수술기록 생성 AI 입력용 타임라인" in record["text"]
    assert "phase_1 -> phase_2" in record["text"]
    assert "애드슨 포셉 주세요" in record["text"]
    assert "mayo -> surgeon" in record["text"]
    assert "음성 종료/일시정지 시각" in record["text"]
    assert len(record["text"]) <= MAX_TEXT_CHARS
    assert session.active is False


def test_rendered_record_is_bounded_by_the_documented_api_limit() -> None:
    session = SurgeryRecordSession(max_events=2_000)
    started = datetime(2026, 8, 24, 10, 0, tzinfo=SEOUL)
    session.start(
        procedure_id="thyroidectomy",
        active_bundle="thyroidectomy",
        phase_id="phase_1",
        now=started,
        now_monotonic=0.0,
    )
    for index in range(1_000):
        session.append(
            "관측",
            f"{index}:" + ("가" * 700),
            now_monotonic=float(index + 1),
            event_key=("event", index),
        )

    record = session.finalize(
        ended_at=started,
        ended_monotonic=1_001.0,
        utterance_id="voice-end-2",
        control_message="simulation paused",
    )

    assert record is not None
    assert len(record["text"]) <= MAX_TEXT_CHARS
    assert "API 문자 수 제한" in record["text"]


def test_node_saves_and_submits_once_only_after_successful_voice_pause(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("TASKPLANNER_SURGERY_RECORD_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("PUZZLE_SURGERY_RECORD_ROOM_NAME", "Preclinical Center")
    runtime = FakeRuntime()
    monotonic_values = iter((100.0, 101.0, 102.0, 103.0))
    rclpy.init(args=[])
    node = OperationalSurgeryRecordNode(
        runtime=runtime,
        now=lambda: datetime(2026, 8, 24, 11, 0, tzinfo=SEOUL),
        monotonic=lambda: next(monotonic_values),
    )
    try:
        state = SimulationState()
        state.procedure_id = "thyroidectomy_demo"
        state.active_bundle = "thyroidectomy_demo"
        state.running = True
        state.execution_state = "running"
        state.filtered_phase = "phase_1"
        node._on_simulation(state)

        voice = VoiceCommandIntent()
        voice.source_is_final = True
        voice.utterance_id = "voice-end-1"
        voice.raw_text = "수술 종료"
        voice.intent = "procedure_stop"
        voice.disposition = "propose"
        node._on_voice(voice)

        node._on_voice_control_result(String(data=_control_result()))
        node._on_voice_control_result(String(data=_control_result()))

        assert len(runtime.submissions) == 1
        payload = runtime.submissions[0]
        assert payload["room_name"] == "Preclinical Center"
        assert payload["surgery_code"].startswith("THY-20260824-110000-")
        assert payload["date"] == "2026-08-24"
        assert "수술 종료" in payload["text"]
        local_files = list(tmp_path.glob("THY-*.txt"))
        assert len(local_files) == 1
        assert local_files[0].stat().st_mode & 0o777 == 0o600
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
