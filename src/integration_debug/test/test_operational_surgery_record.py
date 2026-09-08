from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import re
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import rclpy
from rclpy.qos import DurabilityPolicy, HistoryPolicy, ReliabilityPolicy
from std_msgs.msg import String
from surgical_msgs.msg import SimulationState, SpeechUtterance

from integration_debug.operational_surgery_record import (
    MAX_GENERATED_RECORD_BYTES,
    MAX_GENERATED_VLM_ROWS,
    MAX_RETRY_RECORD_BYTES,
    MAX_STATUS_RESPONSE_BYTES,
    MAX_PUBLIC_RECEIPT_FRAME_BYTES,
    PUBLIC_RECEIPT_QOS,
    PUBLIC_RECEIPT_SCHEMA,
    PUBLIC_RECEIPT_TOPIC,
    RECORD_BUDGET_OMISSION_MARKER,
    STATUS_SCHEMA,
    STATUS_TOPIC,
    TERMINAL_POST_STATES,
    TERMINAL_RECEIPT_SCHEMA,
    TERMINAL_RECEIPT_TOPIC,
    OperationalSurgeryRecordNode,
    SurgeryRecordSession,
    _compact_retry_record,
    _status_response_preview,
    parse_successful_terminal_receipt,
)
from integration_debug.surgery_record_runtime import MAX_TEXT_CHARS


SEOUL = ZoneInfo("Asia/Seoul")


class FakeRuntime:
    def __init__(self, *, fail: bool = False, case_text: str = "") -> None:
        self.submissions: list[dict[str, str]] = []
        self.fail = fail
        self.case_text = case_text
        self.loaded_case_ids: list[str] = []
        self.snapshot_payload: dict[str, object] = {
            "state": "IDLE",
            "last_result": {},
            "last_error": "",
        }

    def submit_async(self, payload: dict[str, str]) -> str:
        if self.fail:
            raise ValueError("test API key unavailable")
        self.submissions.append(dict(payload))
        return "record-test-1"

    def load_case(self, case_id: str) -> tuple[str, str]:
        self.loaded_case_ids.append(case_id)
        if not self.case_text:
            raise ValueError("test fixture is unavailable")
        return self.case_text, f"{case_id}_surgery_record.txt"

    def snapshot(self) -> dict[str, object]:
        if self.submissions and self.snapshot_payload["state"] == "IDLE":
            return {
                "state": "SUBMITTING",
                "last_result": {},
                "last_error": "",
            }
        return dict(self.snapshot_payload)


def test_operational_owner_archives_responses_beside_sent_records(
    tmp_path,
    monkeypatch,
) -> None:
    output_dir = tmp_path / "records"
    secret_file = tmp_path / "puzzle-surgery-record-api-key"
    secret_file.write_text("test-only-api-key\n", encoding="utf-8")
    secret_file.chmod(0o600)
    monkeypatch.setenv("TASKPLANNER_SURGERY_RECORD_OUTPUT_DIR", str(output_dir))
    monkeypatch.delenv("TASKPLANNER_SURGERY_RECORD_RESPONSE_DIR", raising=False)
    monkeypatch.setenv("PUZZLE_SURGERY_RECORD_API_KEY_FILE", str(secret_file))

    rclpy.init(args=[])
    node = OperationalSurgeryRecordNode()
    try:
        assert node._response_archive_dir == tmp_path / "responses"
        assert node._runtime._response_archive_dir == tmp_path / "responses"
        assert node._runtime._require_nonempty_summary is True
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


def _terminal_receipt(**overrides) -> str:
    payload = {
        "schema": TERMINAL_RECEIPT_SCHEMA,
        "procedure_id": "thyroidectomy_demo",
        "active_bundle": "thyroidectomy_demo",
        "procedure_run_id": "run-1",
        "terminal_kind": "stop",
        "success": True,
        "message": "simulation stopped and executor settled",
        "execution_state": "halted",
    }
    payload.update(overrides)
    return json.dumps(payload)


def _state(
    *,
    run_id: str = "run-1",
    running: bool,
    execution_state: str,
    phase: str = "phase_1",
) -> SimulationState:
    state = SimulationState()
    state.procedure_id = "thyroidectomy_demo"
    state.procedure_run_id = run_id
    state.active_bundle = "thyroidectomy_demo"
    state.running = running
    state.execution_state = execution_state
    state.filtered_phase = phase
    return state


def test_only_settled_stop_or_completed_receipt_can_authorize_submission() -> None:
    assert parse_successful_terminal_receipt(_terminal_receipt()) == {
        "procedure_run_id": "run-1",
        "procedure_id": "thyroidectomy_demo",
        "active_bundle": "thyroidectomy_demo",
        "terminal_kind": "stop",
        "execution_state": "halted",
        "message": "simulation stopped and executor settled",
    }
    assert parse_successful_terminal_receipt(
        _terminal_receipt(
            terminal_kind="completed",
            execution_state="completed",
        )
    ) is not None
    assert parse_successful_terminal_receipt(_terminal_receipt(success=False)) is None
    assert parse_successful_terminal_receipt(
        _terminal_receipt(terminal_kind="pause", execution_state="paused")
    ) is None
    assert parse_successful_terminal_receipt(
        _terminal_receipt(execution_state="running")
    ) is None
    assert parse_successful_terminal_receipt(
        _terminal_receipt(procedure_run_id="")
    ) is None
    assert parse_successful_terminal_receipt("not-json") is None


def test_record_owner_observes_common_lifecycle_receipt_not_voice_intent() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "integration_debug"
        / "operational_surgery_record.py"
    ).read_text(encoding="utf-8")

    assert f'TERMINAL_RECEIPT_TOPIC = "{TERMINAL_RECEIPT_TOPIC}"' in source
    assert 'OBSERVED_UTTERANCE_TOPIC = "/surgery/audio/observed_utterance"' in source
    assert "VoiceCommandIntent" not in source
    assert '"/surgery/voice/intent"' not in source
    assert '"/simulation/voice_procedure_control/result"' not in source


def test_post_status_keeps_the_actual_redacted_server_response_bounded(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TASKPLANNER_SURGERY_RECORD_OUTPUT_DIR", str(tmp_path))
    runtime = FakeRuntime()
    runtime.snapshot_payload = {
        "state": "SUCCEEDED",
        "last_error": "",
        "last_result": {
            "success": True,
            "http_status": 201,
            "receipt_id": "receipt-42",
            "received_at": "2026-08-29T02:03:04Z",
            "completed_at": "2026-08-29T02:03:05Z",
            "response_json": {
                "result": "success",
                "data": {"id": "receipt-42", "receivedAt": "2026-08-29T02:03:04Z"},
            },
        },
    }
    rclpy.init(args=[])
    node = OperationalSurgeryRecordNode(runtime=runtime)
    try:
        node._last_request_id = "record-test-1"
        node._session.procedure_run_id = "run-1"
        node._session.surgery_code = "THY-test"
        payload = node._status_payload(runtime.snapshot())

        assert payload["schema"] == STATUS_SCHEMA
        assert STATUS_TOPIC == "/surgery/record/post_status"
        assert payload["request_id"] == "record-test-1"
        assert payload["completed_at"] == "2026-08-29T02:03:05Z"
        assert payload["received_at"] == "2026-08-29T02:03:04Z"
        assert payload["server_result"] == "success"
        assert '"receipt-42"' in payload["response"]
        assert payload["response_truncated"] is False
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


def test_server_response_preview_has_a_strict_public_topic_boundary() -> None:
    preview, truncated = _status_response_preview(
        {"response_text": "가" * 20_000}
    )

    assert truncated is True
    assert preview.endswith("\n…")
    assert len(preview.encode("utf-8")) <= MAX_STATUS_RESPONSE_BYTES


def test_public_receipt_has_exact_terminal_schema_and_no_private_transport_fields(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("TASKPLANNER_SURGERY_RECORD_OUTPUT_DIR", str(tmp_path))
    endpoint = "https://private.example.test/api/v1/records"
    output_path = str(tmp_path / "THY-test.txt")
    runtime = FakeRuntime()
    runtime.snapshot_payload = {
        "state": "FAILED",
        "last_error": (
            f"POST to {endpoint} failed: token=token-value; "
            f"output_path={output_path}"
        ),
        "last_result": {
            "request_id": "record-test-2",
            "endpoint": endpoint,
            "success": False,
            "http_status": 500,
            "receipt_id": "",
            "received_at": "",
            "completed_at": "2026-08-31T01:02:03Z",
            "response_json": {
                "result": "failed",
                "record": "서버가 반환한 원문 수술기록",
                "endpoint": endpoint,
                "output_path": output_path,
                "api_key": "api-key-value",
                "nested": {"secret": "secret-value"},
            },
        },
    }
    rclpy.init(args=[])
    node = OperationalSurgeryRecordNode(runtime=runtime)
    try:
        node._last_output_path = output_path
        node._last_record_text = "생성된 수술기록 원문"
        node._session.procedure_run_id = "run-2"
        node._session.surgery_code = "THY-test"
        payload = node._public_receipt_payload(runtime.snapshot())

        assert payload is not None
        assert set(payload) == {
            "schema",
            "request_id",
            "procedure_run_id",
            "surgery_code",
            "record_text",
            "submit_state",
            "success",
            "http_status",
            "receipt_id",
            "received_at",
            "completed_at",
            "server_result",
            "response",
            "response_truncated",
            "error",
        }
        assert payload["schema"] == PUBLIC_RECEIPT_SCHEMA
        assert payload["request_id"] == "record-test-2"
        assert payload["procedure_run_id"] == "run-2"
        assert payload["surgery_code"] == "THY-test"
        assert payload["record_text"] == "생성된 수술기록 원문"
        assert payload["submit_state"] == "FAILED"
        assert payload["server_result"] == "failed"
        assert "서버가 반환한 원문 수술기록" in payload["response"]
        assert payload["response_truncated"] is False

        serialized = json.dumps(payload, ensure_ascii=False)
        for private_value in (
            endpoint,
            output_path,
            "api-key-value",
            "secret-value",
            "token-value",
        ):
            assert private_value not in serialized
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


@pytest.mark.parametrize("state", sorted(TERMINAL_POST_STATES))
def test_public_receipt_publishes_once_for_each_terminal_post_outcome(
    tmp_path,
    monkeypatch,
    state: str,
) -> None:
    monkeypatch.setenv("TASKPLANNER_SURGERY_RECORD_OUTPUT_DIR", str(tmp_path))
    runtime = FakeRuntime()
    runtime.snapshot_payload = {
        "state": state,
        "last_error": "" if state == "SUCCEEDED" else "server outcome unknown",
        "last_result": {
            "request_id": f"record-{state.lower()}",
            "success": state == "SUCCEEDED",
            "http_status": 201 if state == "SUCCEEDED" else 0,
            "completed_at": "2026-08-31T01:02:03Z",
        },
    }

    class CapturePublisher:
        def __init__(self) -> None:
            self.messages: list[String] = []

        def publish(self, message: String) -> None:
            self.messages.append(message)

    rclpy.init(args=[])
    node = OperationalSurgeryRecordNode(runtime=runtime)
    try:
        node._last_request_id = f"record-{state.lower()}"
        node._last_record_text = "terminal record text"
        node._session.procedure_run_id = "run-terminal"
        node._session.surgery_code = "THY-terminal"
        capture = CapturePublisher()
        node._public_receipt_pub = capture

        node._publish_public_receipt(
            {"state": "SUBMITTING", "last_result": {}, "last_error": ""}
        )
        node._publish_public_receipt(runtime.snapshot())
        node._publish_public_receipt(runtime.snapshot())

        assert len(capture.messages) == 1
        published = json.loads(capture.messages[0].data)
        assert published["submit_state"] == state
        assert published["request_id"] == f"record-{state.lower()}"
        assert published["record_text"] == "terminal record text"
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


def test_public_receipt_qos_is_durable_reliable_and_latest_only() -> None:
    assert PUBLIC_RECEIPT_TOPIC == "/surgery/record/receipt"
    assert PUBLIC_RECEIPT_QOS.history == HistoryPolicy.KEEP_LAST
    assert PUBLIC_RECEIPT_QOS.depth == 1
    assert PUBLIC_RECEIPT_QOS.reliability == ReliabilityPolicy.RELIABLE
    assert PUBLIC_RECEIPT_QOS.durability == DurabilityPolicy.TRANSIENT_LOCAL


def test_full_generated_record_fits_the_dedicated_public_receipt_frame_limit(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("TASKPLANNER_SURGERY_RECORD_OUTPUT_DIR", str(tmp_path))
    record_text = "😀" * MAX_TEXT_CHARS
    runtime = FakeRuntime()
    runtime.snapshot_payload = {
        "state": "SUCCEEDED",
        "last_error": "",
        "last_result": {
            "request_id": "record-full-text",
            "success": True,
            "http_status": 201,
            "completed_at": "2026-08-31T01:02:03Z",
            "response_text": "x" * MAX_STATUS_RESPONSE_BYTES,
        },
    }
    rclpy.init(args=[])
    node = OperationalSurgeryRecordNode(runtime=runtime)
    try:
        node._last_request_id = "record-full-text"
        node._last_record_text = record_text
        node._session.procedure_run_id = "run-full-text"
        node._session.surgery_code = "THY-full-text"
        payload = node._public_receipt_payload(runtime.snapshot())

        assert payload is not None
        assert payload["record_text"] == record_text
        string_data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        rosbridge_frame = json.dumps(
            {
                "op": "publish",
                "topic": PUBLIC_RECEIPT_TOPIC,
                "msg": {"data": string_data},
            },
            separators=(",", ":"),
        )
        assert len(rosbridge_frame.encode("utf-8")) <= MAX_PUBLIC_RECEIPT_FRAME_BYTES
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


def test_session_collects_live_facts_and_renders_terminal_reason() -> None:
    session = SurgeryRecordSession()
    started = datetime(2026, 8, 24, 9, 30, tzinfo=SEOUL)
    session.start(
        procedure_id="thyroidectomy_demo",
        procedure_run_id="run-1",
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
    session.observe_utterance(
        SimpleNamespace(
            is_final=True,
            utterance_id="voice-1",
            text="애드슨 포셉 주세요",
            source="taskplanner_asr",
            speaker_role="surgeon",
            has_confidence=True,
            confidence=0.91,
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
    session.observe_vlm(
        SimpleNamespace(
            phase_ids=["phase_2"],
            phase_confidences=[0.8],
            observed_tool_ids=["Adson forceps"],
            observed_location_ids=["surgeon"],
            observed_confidences=[0.9],
            uncertainty=0.1,
            summary="An Adson forceps is visible in the central neck field.",
        ),
        now_monotonic=108.0,
    )

    record = session.finalize(
        ended_at=datetime(2026, 8, 24, 9, 45, tzinfo=SEOUL),
        ended_monotonic=1_000.0,
        terminal_kind="stop",
        execution_state="halted",
        terminal_message="executor settled",
    )

    assert record is not None
    assert record["surgery_code"].startswith("THY-")
    assert record["date"] == "2026-08-24"
    text = record["text"]
    assert text.startswith(
        "갑상선절제술 수술 타임라인\n"
        "========================================\n"
        "수술: 갑상선절제술(시연)\n"
    )
    assert f"Case: {record['surgery_code']}" in text
    assert "Run: run-1" in text
    assert "VLM: Taskplanner 관측 VLM" in text
    assert "[기록 요약]" in text
    assert "집도의 발화: 1건" in text
    assert "VLM 관찰: 1건" in text
    assert "Phase 변화: 1건" in text
    assert "스크럽 널스·어시스턴트 행동: 1건" in text
    assert "[시간순 기록]" in text
    rows = [line for line in text.splitlines() if line.startswith("[") and "] " in line]
    row_pattern = re.compile(
        r"^\[(\d{2}):(\d{2}):(\d{2}\.\d{3})\] "
        r"(Phase|집도의|VLM|스크럽 널스|어시스턴트) \| .+$"
    )
    assert rows
    assert all(row_pattern.fullmatch(line) for line in rows)
    timestamps = [
        tuple(map(float, row_pattern.fullmatch(line).group(1, 2, 3)))
        for line in rows
    ]
    assert timestamps == sorted(timestamps)
    assert "집도의 | 애드슨 포셉 주세요" in text
    assert "어시스턴트 | 집도의에게 도구 전달 | Adson forceps | 로봇 → 집도의" in text
    assert "VLM | An Adson forceps is visible in the central neck field." in text
    assert "Phase | 수술 종료 | stop confirmed | state=halted | executor settled" in text
    assert "procedureRunId:" not in text
    assert "+000" not in text
    assert "[단계:" not in text
    assert "source=" not in text
    assert "confidence=" not in text
    assert "uncertainty=" not in text
    assert RECORD_BUDGET_OMISSION_MARKER not in text
    assert len(text.encode("utf-8")) <= MAX_GENERATED_RECORD_BYTES
    assert len(text) <= MAX_TEXT_CHARS
    assert session.active is False


def test_rendered_record_uses_utf8_budget_and_keeps_terminal_row() -> None:
    session = SurgeryRecordSession(max_events=2_000)
    started = datetime(2026, 8, 24, 10, 0, tzinfo=SEOUL)
    session.start(
        procedure_id="thyroidectomy",
        procedure_run_id="run-bounded",
        active_bundle="thyroidectomy",
        phase_id="phase_1",
        now=started,
        now_monotonic=0.0,
    )
    for index in range(1_000):
        session.append(
            "VLM 관측",
            f"phases=- | tools=- | uncertainty=0.000 | summary={index}:" + ("가" * 700),
            now_monotonic=float(index + 1),
            event_key=("event", index),
        )

    record = session.finalize(
        ended_at=started,
        ended_monotonic=1_001.0,
        terminal_kind="completed",
        execution_state="completed",
        terminal_message="executor settled",
    )

    assert record is not None
    text = record["text"]
    assert len(text.encode("utf-8")) <= MAX_GENERATED_RECORD_BYTES
    assert len(text) <= MAX_TEXT_CHARS
    assert RECORD_BUDGET_OMISSION_MARKER in text
    rows = [
        line
        for line in text.splitlines()
        if line.startswith("[") and "] " in line
    ]
    assert all(
        re.fullmatch(
            r"^\[\d{2}:\d{2}:\d{2}\.\d{3}\] "
            r"(Phase|집도의|VLM|스크럽 널스|어시스턴트) \| .+$",
            line,
        )
        for line in rows
    )
    retained_vlm_rows = [
        line
        for line in rows
        if "] VLM | " in line and RECORD_BUDGET_OMISSION_MARKER not in line
    ]
    assert len(retained_vlm_rows) <= MAX_GENERATED_VLM_ROWS
    assert f"VLM 관찰: {len(retained_vlm_rows)}건" in text
    assert any("Phase | 수술 종료 | completed confirmed" in line for line in rows)


def test_vlm_budget_keeps_a_representative_spread_and_summary_matches_rows() -> None:
    session = SurgeryRecordSession(max_events=2_000)
    started = datetime(2026, 8, 24, 10, 0, tzinfo=SEOUL)
    session.start(
        procedure_id="thyroidectomy_demo",
        procedure_run_id="run-vlm-budget",
        active_bundle="thyroidectomy_demo",
        phase_id="phase_1",
        now=started,
        now_monotonic=0.0,
    )
    for index in range(MAX_GENERATED_VLM_ROWS + 80):
        session.append(
            "VLM 관측",
            (
                "phases=- | tools=- | uncertainty=0.000 | "
                f"summary=observation-{index}"
            ),
            now_monotonic=float(index + 1),
            event_key=("representative-vlm", index),
        )

    record = session.finalize(
        ended_at=started,
        ended_monotonic=500.0,
        terminal_kind="stop",
        execution_state="halted",
        terminal_message="executor settled",
    )

    assert record is not None
    text = record["text"]
    rows = [
        line
        for line in text.splitlines()
        if line.startswith("[") and "] " in line
    ]
    retained_vlm_rows = [
        line
        for line in rows
        if "] VLM | " in line and RECORD_BUDGET_OMISSION_MARKER not in line
    ]
    assert len(text.encode("utf-8")) <= MAX_GENERATED_RECORD_BYTES
    assert len(retained_vlm_rows) == MAX_GENERATED_VLM_ROWS
    assert f"VLM 관찰: {len(retained_vlm_rows)}건" in text
    assert "observation-0" in text
    assert f"observation-{MAX_GENERATED_VLM_ROWS + 79}" in text
    assert RECORD_BUDGET_OMISSION_MARKER in text
    assert any("Phase | 수술 종료 | stop confirmed" in line for line in rows)


def test_empty_summary_retry_text_is_small_0704_and_keeps_the_terminal_fact() -> None:
    source_rows = [
        "[00:00:00.000] Phase | 수술 단계 전환 | phase_1",
        *[
            f"[00:00:{index:02d}.000] VLM | 관찰-{index}: " + ("가" * 500)
            for index in range(1, 31)
        ],
        "[00:01:00.000] 집도의 | 견인 시작",
        "[00:02:00.000] Phase | 수술 종료 | stop confirmed | state=halted",
    ]
    primary = {
        "surgery_code": "THY-primary-code",
        "date": "2026-09-02",
        "text": "\n".join(
            [
                "갑상선절제술 수술 타임라인",
                "========================================",
                "수술: 갑상선절제술(시연)",
                "Case: THY-primary-code",
                "Run: retry-run",
                "VLM: Taskplanner 관측 VLM",
                "",
                "[기록 요약]",
                "집도의 발화: 1건",
                "VLM 관찰: 30건",
                "Phase 변화: 1건",
                "스크럽 널스·어시스턴트 행동: 0건",
                "",
                "[시간순 기록]",
                *source_rows,
            ]
        )
        + "\n",
    }

    retry = _compact_retry_record(primary)
    retry_rows = [
        line for line in retry["text"].splitlines() if "] VLM | " in line
    ]

    assert retry["surgery_code"] != primary["surgery_code"]
    assert f"Case: {retry['surgery_code']}" in retry["text"]
    assert retry["date"] == primary["date"]
    assert len(retry["text"].encode("utf-8")) <= MAX_RETRY_RECORD_BYTES
    assert f"VLM 관찰: {len(retry_rows)}건" in retry["text"]
    assert "Phase | 수술 종료 | stop confirmed | state=halted" in retry["text"]
    assert RECORD_BUDGET_OMISSION_MARKER not in retry["text"]


def test_surgery_code_is_deterministic_for_one_procedure_run() -> None:
    codes = []
    for hour in (9, 10):
        session = SurgeryRecordSession()
        session.start(
            procedure_id="thyroidectomy_demo",
            procedure_run_id="stable-run-id",
            active_bundle="thyroidectomy_demo",
            phase_id="phase_1",
            now=datetime(2026, 8, 24, hour, 0, tzinfo=SEOUL),
            now_monotonic=0.0,
        )
        codes.append(session.surgery_code)

    assert codes[0] == codes[1]
    assert codes[0].startswith("THY-")


def test_pause_and_raw_halted_state_do_not_submit_then_receipt_submits_once(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("TASKPLANNER_SURGERY_RECORD_OUTPUT_DIR", str(tmp_path))
    runtime = FakeRuntime()
    tick = iter(float(value) for value in range(100, 130))
    rclpy.init(args=[])
    node = OperationalSurgeryRecordNode(
        runtime=runtime,
        now=lambda: datetime(2026, 8, 24, 11, 0, tzinfo=SEOUL),
        monotonic=lambda: next(tick),
    )
    try:
        node._on_simulation(_state(running=True, execution_state="running"))

        voice = SpeechUtterance()
        voice.is_final = True
        voice.utterance_id = "voice-end-1"
        voice.text = "수술 종료"
        voice.source = "taskplanner_asr"
        voice.speaker_role = "surgeon"
        voice.has_confidence = True
        voice.confidence = 0.9
        node._on_utterance(voice)

        node._on_simulation(_state(running=True, execution_state="paused"))
        node._on_simulation(_state(running=False, execution_state="halted"))
        assert runtime.submissions == []

        receipt = String(data=_terminal_receipt())
        node._on_terminal_receipt(receipt)
        node._on_terminal_receipt(receipt)

        assert len(runtime.submissions) == 1
        payload = runtime.submissions[0]
        assert payload["surgery_code"].startswith("THY-")
        assert "수술 종료" in payload["text"]
        assert "Phase | 수술 종료 | stop confirmed | state=halted" in payload["text"]
        local_files = list(tmp_path.glob("THY-*.txt"))
        assert len(local_files) == 1
        assert tmp_path.stat().st_mode & 0o777 == 0o700
        assert local_files[0].stat().st_mode & 0o777 == 0o600
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


def test_terminal_submission_and_public_receipt_use_the_same_saved_actual_text(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("TASKPLANNER_SURGERY_RECORD_OUTPUT_DIR", str(tmp_path))
    # A stale shell setting must not restore the retired automatic fixture path.
    monkeypatch.setenv("TASKPLANNER_SURGERY_RECORD_SUBMISSION_CASE_ID", "0704_6")
    runtime = FakeRuntime(
        case_text=(
            "0704_6 clinical fixture\n"
            "[00:00:00.000] Phase | 절개\n"
        )
    )
    tick = iter(float(value) for value in range(140, 170))
    rclpy.init(args=[])
    node = OperationalSurgeryRecordNode(
        runtime=runtime,
        now=lambda: datetime(2026, 8, 24, 11, 30, tzinfo=SEOUL),
        monotonic=lambda: next(tick),
    )
    try:
        node._on_simulation(_state(running=True, execution_state="running"))
        node._on_terminal_receipt(String(data=_terminal_receipt()))

        assert len(runtime.submissions) == 1
        payload = runtime.submissions[0]
        local_record = next(tmp_path.glob("THY-*.txt")).read_text(encoding="utf-8")
        assert runtime.loaded_case_ids == []
        assert "case_id" not in payload
        assert payload["text"] == local_record
        assert node._last_record_text == local_record
        assert local_record.startswith("갑상선절제술 수술 타임라인\n")
        assert "Case: " + payload["surgery_code"] in local_record

        runtime.snapshot_payload = {
            "state": "SUCCEEDED",
            "last_result": {"request_id": "record-observed"},
            "last_error": "",
        }
        receipt = node._public_receipt_payload(runtime.snapshot())
        assert receipt is not None
        assert receipt["record_text"] == local_record
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


def test_empty_summary_result_submits_one_compact_retry_before_public_receipt(
    tmp_path,
    monkeypatch,
) -> None:
    """The browser never receives the known semantic-empty primary receipt."""

    monkeypatch.setenv("TASKPLANNER_SURGERY_RECORD_OUTPUT_DIR", str(tmp_path))
    runtime = FakeRuntime()
    tick = iter(float(value) for value in range(600, 640))

    class CapturePublisher:
        def __init__(self) -> None:
            self.messages: list[String] = []

        def publish(self, message: String) -> None:
            self.messages.append(message)

    rclpy.init(args=[])
    node = OperationalSurgeryRecordNode(
        runtime=runtime,
        now=lambda: datetime(2026, 9, 2, 15, 0, tzinfo=SEOUL),
        monotonic=lambda: next(tick),
    )
    try:
        capture = CapturePublisher()
        node._public_receipt_pub = capture
        node._on_simulation(_state(running=True, execution_state="running"))
        node._on_terminal_receipt(String(data=_terminal_receipt()))

        assert len(runtime.submissions) == 1
        primary = runtime.submissions[0]
        assert node._pending_compact_retry is not None

        runtime.snapshot_payload = {
            "state": "FAILED",
            "last_error": "201 response is missing a nonempty generated summary",
            "last_result": {
                "request_id": "record-primary",
                "surgery_code": primary["surgery_code"],
                "success": False,
                "http_status": 201,
                "error_code": "EMPTY_GENERATED_SUMMARY",
                "completed_at": "2026-09-02T15:00:01+09:00",
            },
        }
        node._publish_status()

        assert len(runtime.submissions) == 2
        retry = runtime.submissions[1]
        assert retry["surgery_code"] != primary["surgery_code"]
        assert f"Case: {retry['surgery_code']}" in retry["text"]
        assert len(retry["text"].encode("utf-8")) <= MAX_RETRY_RECORD_BYTES
        assert capture.messages == []
        assert node._start_compact_retry_if_needed(runtime.snapshot()) is False
        assert len(runtime.submissions) == 2

        runtime.snapshot_payload = {
            "state": "SUCCEEDED",
            "last_error": "",
            "last_result": {
                "request_id": "record-compact-retry",
                "surgery_code": retry["surgery_code"],
                "success": True,
                "http_status": 201,
                "receipt_id": "txt_compact_retry",
                "received_at": "2026-09-02T15:00:02+09:00",
                "completed_at": "2026-09-02T15:00:02+09:00",
                "response_json": {
                    "result": "success",
                    "data": {
                        "id": "txt_compact_retry",
                        "receivedAt": "2026-09-02T15:00:02+09:00",
                        "summary": "1.수술명: 갑상선절제술(시연)",
                    },
                },
            },
        }
        node._publish_status()

        assert len(capture.messages) == 1
        published = json.loads(capture.messages[0].data)
        assert published["submit_state"] == "SUCCEEDED"
        assert published["surgery_code"] == retry["surgery_code"]
        assert published["record_text"] == retry["text"]
        assert len(list(tmp_path.glob("THY-*.txt"))) == 2
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


def test_completed_receipt_submits_and_reset_never_does(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("TASKPLANNER_SURGERY_RECORD_OUTPUT_DIR", str(tmp_path))
    runtime = FakeRuntime()
    tick = iter(float(value) for value in range(200, 240))
    rclpy.init(args=[])
    node = OperationalSurgeryRecordNode(
        runtime=runtime,
        now=lambda: datetime(2026, 8, 24, 12, 0, tzinfo=SEOUL),
        monotonic=lambda: next(tick),
    )
    try:
        node._on_simulation(
            _state(run_id="reset-run", running=True, execution_state="running")
        )
        node._on_simulation(
            _state(run_id="reset-run", running=False, execution_state="idle")
        )
        node._on_terminal_receipt(
            String(data=_terminal_receipt(procedure_run_id="reset-run"))
        )
        assert runtime.submissions == []

        node._on_simulation(
            _state(run_id="run-2", running=True, execution_state="running")
        )
        node._on_simulation(
            _state(run_id="run-2", running=False, execution_state="completed")
        )
        node._on_terminal_receipt(
            String(
                data=_terminal_receipt(
                    procedure_run_id="run-2",
                    terminal_kind="completed",
                    execution_state="completed",
                    message="procedure completed and executor settled",
                )
            )
        )

        assert len(runtime.submissions) == 1
        assert (
            "Phase | 수술 종료 | completed confirmed | state=completed"
            in runtime.submissions[0]["text"]
        )
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


def test_terminal_then_automatic_idle_reset_keeps_record_until_receipt_arrives(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("TASKPLANNER_SURGERY_RECORD_OUTPUT_DIR", str(tmp_path))
    runtime = FakeRuntime()
    tick = iter(float(value) for value in range(500, 540))
    rclpy.init(args=[])
    node = OperationalSurgeryRecordNode(
        runtime=runtime,
        now=lambda: datetime(2026, 8, 24, 14, 0, tzinfo=SEOUL),
        monotonic=lambda: next(tick),
    )
    try:
        node._on_simulation(
            _state(run_id="run-auto-reset", running=True, execution_state="running")
        )
        # State and terminal receipt are independent topics.  Model the
        # conservative ordering where automatic reset reaches this observer
        # before the durable receipt.
        node._on_simulation(
            _state(
                run_id="run-auto-reset",
                running=False,
                execution_state="completed",
            )
        )
        node._on_simulation(
            _state(run_id="", running=False, execution_state="idle")
        )

        assert node._session.active is True
        assert runtime.submissions == []

        node._on_terminal_receipt(
            String(
                data=_terminal_receipt(
                    procedure_run_id="run-auto-reset",
                    terminal_kind="completed",
                    execution_state="completed",
                    message="procedure completed and executor settled",
                )
            )
        )

        assert len(runtime.submissions) == 1
        assert node._session.active is False
        assert (
            "Phase | 수술 종료 | completed confirmed | state=completed"
            in runtime.submissions[0]["text"]
        )
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


def test_unreceipted_terminal_idle_eventually_abandons_after_grace(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("TASKPLANNER_SURGERY_RECORD_OUTPUT_DIR", str(tmp_path))
    runtime = FakeRuntime()
    now = [700.0]
    rclpy.init(args=[])
    node = OperationalSurgeryRecordNode(
        runtime=runtime,
        now=lambda: datetime(2026, 8, 24, 14, 30, tzinfo=SEOUL),
        monotonic=lambda: now[0],
    )
    try:
        node._on_simulation(
            _state(run_id="raw-reset-run", running=True, execution_state="running")
        )
        node._on_simulation(
            _state(
                run_id="raw-reset-run",
                running=False,
                execution_state="halted",
            )
        )
        node._on_simulation(
            _state(run_id="", running=False, execution_state="idle")
        )

        assert node._session.active is True
        now[0] += 3.1
        node._publish_status()

        assert node._session.active is False
        assert runtime.submissions == []
        assert "terminal receipt did not arrive" in node._last_error
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


def test_local_record_survives_when_post_cannot_start(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TASKPLANNER_SURGERY_RECORD_OUTPUT_DIR", str(tmp_path))
    runtime = FakeRuntime(fail=True)
    tick = iter(float(value) for value in range(300, 320))
    rclpy.init(args=[])
    node = OperationalSurgeryRecordNode(
        runtime=runtime,
        now=lambda: datetime(2026, 8, 24, 13, 0, tzinfo=SEOUL),
        monotonic=lambda: next(tick),
    )
    try:
        node._on_simulation(_state(running=True, execution_state="running"))
        node._on_terminal_receipt(String(data=_terminal_receipt()))

        local_files = list(tmp_path.glob("THY-*.txt"))
        assert len(local_files) == 1
        assert node._last_output_path == str(local_files[0])
        assert "test API key unavailable" in node._last_error
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
