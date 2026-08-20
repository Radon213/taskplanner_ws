from __future__ import annotations

import json
from io import BytesIO
from urllib.error import URLError
from urllib.error import HTTPError

import pytest

from procedure_spec import RetractionCommand, RetractionState, RetractionTargetSide

from bt_orchestrator.retractor_voice_interpreter import (
    TextOnlyRetractionVLMInterpreter,
    is_retractor_voice_protocol_candidate,
)


def _response(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


@pytest.mark.parametrize(
    "transcript",
    [
        "리트렉터 직접 가르치기 모드 켜줘",
        "가르치기 이제 다 됐어",
        "이제 견인 들어가자",
        "오른쪽으로 한 번 더 당겨",
        "장비 다른 걸로 바꿔줘",
        "견인은 여기서 끝내",
    ],
)
def test_operational_candidate_gate_covers_fuzzy_demo_corpus(
    transcript: str,
) -> None:
    assert is_retractor_voice_protocol_candidate(transcript) is True


@pytest.mark.parametrize(
    "transcript",
    ["석션 주세요", "봉합은 여기서 끝내", "오른쪽 절개 부위를 확인해"],
)
def test_operational_candidate_gate_rejects_unrelated_speech(
    transcript: str,
) -> None:
    assert is_retractor_voice_protocol_candidate(transcript) is False


def test_text_vlm_interpreter_uses_closed_json_result() -> None:
    captured = {}

    def request_json(url, body, timeout_sec, headers):
        captured.update(
            url=url,
            body=body,
            timeout_sec=timeout_sec,
            headers=headers,
        )
        return _response(
            '{"v":"1","command":"adjust_retraction",'
            '"target_side":"right","distance_m":0.05}'
        )

    result = TextOnlyRetractionVLMInterpreter(
        base_url="http://127.0.0.1:8001",
        model_id="local-text-vlm",
        timeout_sec=1.5,
        request_json=request_json,
    ).interpret("오른쪽 5cm 더", RetractionState.RETRACTION_ACTIVE)

    assert result.interpreter_source == "text_vlm"
    assert result.vlm_invoked is True
    assert result.normalized.command == RetractionCommand.ADJUST_RETRACTION
    assert result.normalized.target_side == RetractionTargetSide.RIGHT
    assert result.normalized.distance_m == 0.05
    assert captured["url"] == "http://127.0.0.1:8001/v1/chat/completions"
    assert captured["body"]["response_format"] == {"type": "text"}
    assert captured["body"]["messages"][1]["role"] == "user"
    context = json.loads(captured["body"]["messages"][1]["content"])
    assert context["allowed_commands"] == [
        "adjust_retraction",
        "change_tool",
        "stop_retraction",
    ]
    assert context["default_adjustment_distance_m"] == 0.05


def test_text_vlm_grounded_single_side_adjustment_uses_documented_default() -> None:
    result = TextOnlyRetractionVLMInterpreter(
        base_url="http://127.0.0.1:8001",
        model_id="local-text-vlm",
        request_json=lambda *_args, **_kwargs: _response(
            '{"v":"1","command":"adjust_retraction",'
            '"target_side":"left","distance_m":0.05}'
        ),
    ).interpret("왼쪽 더", RetractionState.RETRACTION_ACTIVE)

    assert result.interpreter_source == "text_vlm"
    assert result.vlm_invoked is True
    assert result.normalized.command == RetractionCommand.ADJUST_RETRACTION
    assert result.normalized.target_side == RetractionTargetSide.LEFT
    assert result.normalized.distance_m == 0.05


@pytest.mark.parametrize(
    ("transcript", "state", "model_json", "expected_command"),
    [
        (
            "리트렉터 직접 가르치기 모드 켜줘",
            RetractionState.IDLE,
            '{"v":"1","command":"start_direct_teach",'
            '"target_side":"none","distance_m":0}',
            RetractionCommand.START_DIRECT_TEACH,
        ),
        (
            "가르치기 이제 다 됐어",
            RetractionState.DIRECT_TEACHING,
            '{"v":"1","command":"finish_direct_teach",'
            '"target_side":"none","distance_m":0}',
            RetractionCommand.FINISH_DIRECT_TEACH,
        ),
        (
            "직접 교시 다 됐어",
            RetractionState.DIRECT_TEACHING,
            '{"v":"1","command":"finish_direct_teach",'
            '"target_side":"none","distance_m":0}',
            RetractionCommand.FINISH_DIRECT_TEACH,
        ),
        (
            "이제 견인 들어가자",
            RetractionState.TAUGHT_READY,
            '{"v":"1","command":"start_retraction",'
            '"target_side":"none","distance_m":0}',
            RetractionCommand.START_RETRACTION,
        ),
        (
            "오른쪽으로 한 번만 더 당겨",
            RetractionState.RETRACTION_ACTIVE,
            '{"v":"1","command":"adjust_retraction",'
            '"target_side":"right","distance_m":0.05}',
            RetractionCommand.ADJUST_RETRACTION,
        ),
        (
            "장비 다른 걸로 바꿔줘",
            RetractionState.RETRACTION_ACTIVE,
            '{"v":"1","command":"change_tool",'
            '"target_side":"none","distance_m":0}',
            RetractionCommand.CHANGE_TOOL,
        ),
        (
            "견인은 여기서 끝내",
            RetractionState.RETRACTION_ACTIVE,
            '{"v":"1","command":"stop_retraction",'
            '"target_side":"none","distance_m":0}',
            RetractionCommand.STOP_RETRACTION,
        ),
    ],
)
def test_text_vlm_accepts_grounded_demo_paraphrase_corpus(
    transcript: str,
    state: RetractionState,
    model_json: str,
    expected_command: RetractionCommand,
) -> None:
    result = TextOnlyRetractionVLMInterpreter(
        base_url="http://127.0.0.1:8080",
        model_id="qwen3.6-35b-a3b",
        request_json=lambda *_args, **_kwargs: _response(model_json),
    ).interpret(transcript, state)

    assert result.interpreter_source == "text_vlm"
    assert result.vlm_invoked is True
    assert result.normalized.command == expected_command
    if expected_command == RetractionCommand.ADJUST_RETRACTION:
        assert result.normalized.target_side == RetractionTargetSide.RIGHT
        assert result.normalized.distance_m == pytest.approx(0.050)


def test_ninfer_fenced_json_numeric_version_and_null_non_adjustment_fields() -> None:
    result = TextOnlyRetractionVLMInterpreter(
        base_url="http://127.0.0.1:8080",
        model_id="qwen3.6-35b-a3b",
        request_json=lambda *_args, **_kwargs: _response(
            "```json\n"
            '{"v":1,"command":"start_direct_teach",'
            '"target_side":null,"distance_m":null}\n'
            "```"
        ),
    ).interpret("리트렉터 직접 가르치기 모드 켜줘", RetractionState.IDLE)

    assert result.interpreter_source == "text_vlm"
    assert result.normalized.command == RetractionCommand.START_DIRECT_TEACH
    assert result.normalized.target_side == RetractionTargetSide.NONE
    assert result.normalized.distance_m == 0.0


def test_text_response_format_retries_once_without_hint_on_compatibility_error() -> None:
    calls = []

    def request_json(url, body, timeout_sec, headers):
        calls.append(dict(body))
        if len(calls) == 1:
            raise HTTPError(
                url,
                400,
                "response_format_not_supported",
                hdrs=None,
                fp=BytesIO(b'{"error":"response_format_not_supported"}'),
            )
        return _response(
            '{"v":1,"command":"start_direct_teach",'
            '"target_side":null,"distance_m":null}'
        )

    result = TextOnlyRetractionVLMInterpreter(
        base_url="http://127.0.0.1:8080",
        model_id="qwen3.6-35b-a3b",
        request_json=request_json,
    ).interpret("리트렉터 직접 가르치기 모드 켜줘", RetractionState.IDLE)

    assert result.interpreter_source == "text_vlm"
    assert calls[0]["response_format"] == {"type": "text"}
    assert "response_format" not in calls[1]


def test_text_vlm_unavailable_falls_back_without_claiming_model_output() -> None:
    def unavailable(*_args, **_kwargs):
        raise URLError("offline")

    result = TextOnlyRetractionVLMInterpreter(
        base_url="http://127.0.0.1:8001",
        model_id="local-text-vlm",
        request_json=unavailable,
    ).interpret("직접 교시 시작", RetractionState.IDLE)

    assert result.interpreter_source == "deterministic_fallback"
    assert result.vlm_invoked is True
    assert result.normalized.command == RetractionCommand.START_DIRECT_TEACH
    assert result.detail == "text_vlm_unavailable:URLError"


def test_invalid_text_vlm_schema_uses_safe_deterministic_fallback() -> None:
    result = TextOnlyRetractionVLMInterpreter(
        base_url="http://127.0.0.1:8001",
        model_id="local-text-vlm",
        request_json=lambda *_args, **_kwargs: _response(
            '{"v":"1","command":"start_retraction",'
            '"target_side":"right","distance_m":0.01}'
        ),
    ).interpret("직접 교시 시작", RetractionState.IDLE)

    assert result.interpreter_source == "deterministic_fallback"
    assert result.vlm_invoked is True
    assert result.normalized.command == RetractionCommand.START_DIRECT_TEACH
    assert result.detail == "command_not_allowed_in_idle"


def test_text_vlm_cannot_invent_an_adjustment_side_or_distance() -> None:
    result = TextOnlyRetractionVLMInterpreter(
        base_url="http://127.0.0.1:8001",
        model_id="local-text-vlm",
        request_json=lambda *_args, **_kwargs: _response(
            '{"v":"1","command":"adjust_retraction",'
            '"target_side":"right","distance_m":0.04}'
        ),
    ).interpret("리트랙션 5cm 더", RetractionState.RETRACTION_ACTIVE)

    assert result.interpreter_source == "deterministic_fallback"
    assert result.vlm_invoked is True
    assert result.normalized.command is None
    assert result.detail.startswith("text_vlm_transcript_not_grounded:")


@pytest.mark.parametrize(
    ("model_side", "model_distance", "detail"),
    [
        ("left", 0.05, "text_vlm_side_conflicts_with_transcript"),
        ("right", 0.04, "text_vlm_distance_conflicts_with_transcript"),
    ],
)
def test_text_vlm_adjustment_parameters_must_match_raw_stt(
    model_side: str,
    model_distance: float,
    detail: str,
) -> None:
    result = TextOnlyRetractionVLMInterpreter(
        base_url="http://127.0.0.1:8080",
        model_id="qwen3.6-35b-a3b",
        request_json=lambda *_args, **_kwargs: _response(
            json.dumps(
                {
                    "v": 1,
                    "command": "adjust_retraction",
                    "target_side": model_side,
                    "distance_m": model_distance,
                }
            )
        ),
    ).interpret("오른쪽 5cm 더", RetractionState.RETRACTION_ACTIVE)

    assert result.interpreter_source == "deterministic_fallback"
    assert result.normalized.command == RetractionCommand.ADJUST_RETRACTION
    assert result.detail == detail


def test_text_vlm_cannot_invent_command_intent_for_unrelated_stt() -> None:
    result = TextOnlyRetractionVLMInterpreter(
        base_url="http://127.0.0.1:8001",
        model_id="local-text-vlm",
        request_json=lambda *_args, **_kwargs: _response(
            '{"v":"1","command":"start_direct_teach",'
            '"target_side":"none","distance_m":0.0}'
        ),
    ).interpret("석션 주세요", RetractionState.IDLE)

    assert result.interpreter_source == "deterministic_fallback"
    assert result.vlm_invoked is True
    assert result.normalized.command is None
    assert result.detail == "text_vlm_command_family_not_grounded"


@pytest.mark.parametrize(
    ("transcript", "model_command", "target_side", "distance_m"),
    [
        ("봉합은 여기서 끝내", "stop_retraction", "none", 0.0),
        ("장비 상태 알려줘", "change_tool", "none", 0.0),
        ("오른쪽 절개 부위 5cm", "adjust_retraction", "right", 0.05),
    ],
)
def test_text_vlm_rejects_state_constrained_but_unrelated_hallucination(
    transcript: str,
    model_command: str,
    target_side: str,
    distance_m: float,
) -> None:
    result = TextOnlyRetractionVLMInterpreter(
        base_url="http://127.0.0.1:8080",
        model_id="qwen3.6-35b-a3b",
        request_json=lambda *_args, **_kwargs: _response(
            json.dumps(
                {
                    "v": 1,
                    "command": model_command,
                    "target_side": target_side,
                    "distance_m": distance_m,
                }
            )
        ),
    ).interpret(transcript, RetractionState.RETRACTION_ACTIVE)

    assert result.interpreter_source == "deterministic_fallback"
    assert result.normalized.command is None
    assert result.detail == "text_vlm_command_family_not_grounded"


def test_text_vlm_cannot_override_a_different_deterministic_command() -> None:
    result = TextOnlyRetractionVLMInterpreter(
        base_url="http://127.0.0.1:8080",
        model_id="qwen3.6-35b-a3b",
        request_json=lambda *_args, **_kwargs: _response(
            '{"v":1,"command":"start_retraction",'
            '"target_side":null,"distance_m":null}'
        ),
    ).interpret("직접 교시 시작", RetractionState.TAUGHT_READY)

    assert result.interpreter_source == "deterministic_fallback"
    assert result.normalized.command == RetractionCommand.START_DIRECT_TEACH
    assert result.detail == "text_vlm_command_conflicts_with_transcript"


def test_unconfigured_text_vlm_is_honest_deterministic_mode() -> None:
    result = TextOnlyRetractionVLMInterpreter(
        base_url="",
        model_id="",
    ).interpret("직접 교시 시작", RetractionState.IDLE)

    assert result.interpreter_source == "deterministic_fallback"
    assert result.vlm_invoked is False
    assert result.normalized.command == RetractionCommand.START_DIRECT_TEACH
    assert result.detail == "text_vlm_not_configured"
