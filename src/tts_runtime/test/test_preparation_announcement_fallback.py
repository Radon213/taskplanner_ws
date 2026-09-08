from __future__ import annotations

from types import SimpleNamespace

from tts_runtime.announcements import parse_tool_aliases
from tts_runtime.node import TTSRuntimeNode


def _node() -> tuple[TTSRuntimeNode, list[object]]:
    node = TTSRuntimeNode.__new__(TTSRuntimeNode)
    node._gateway_scope = ("gateway-1", "run-1")
    node._dispatcher_started = True
    node._procedure_active = True
    node._announcement_aliases = parse_tool_aliases(["T02=애드슨"])
    node._preparation_fallback_commands = {}
    node._preparation_fallback_accepted = {}
    node._run_scoped_evidence_is_current = lambda message: (
        message.procedure_run_id == "run-1"
    )
    submitted: list[object] = []
    node._submit_request = submitted.append
    return node, submitted


def _skill_command(command_id: str = "predict-adson-1") -> SimpleNamespace:
    return SimpleNamespace(
        procedure_run_id="run-1",
        command_id=command_id,
        action="predict_tool",
        instrument_id="T02",
        request_generation=0,
        voice_backed=False,
    )


def _accepted_trace(command_id: str = "predict-adson-1") -> SimpleNamespace:
    return SimpleNamespace(
        procedure_run_id="run-1",
        command_id=command_id,
        route="tool_transfer",
        transport="action",
        stage="accepted",
        evidence="goal_response",
        dispatch_submitted=True,
    )


def test_predict_tool_fallback_waits_for_endpoint_acceptance() -> None:
    node, submitted = _node()

    TTSRuntimeNode._on_preparation_fallback_skill_command(node, _skill_command())
    assert submitted == []

    TTSRuntimeNode._on_preparation_fallback_execution_trace(node, _accepted_trace())

    assert [request.text for request in submitted] == ["애드슨을 준비하겠습니다."]


def test_predict_tool_fallback_handles_trace_before_command() -> None:
    node, submitted = _node()

    TTSRuntimeNode._on_preparation_fallback_execution_trace(node, _accepted_trace())
    assert submitted == []
    TTSRuntimeNode._on_preparation_fallback_skill_command(node, _skill_command())

    assert [request.text for request in submitted] == ["애드슨을 준비하겠습니다."]


def test_predict_tool_fallback_ignores_voice_backed_or_unaccepted_work() -> None:
    node, submitted = _node()
    voiced = _skill_command()
    voiced.voice_backed = True
    TTSRuntimeNode._on_preparation_fallback_skill_command(node, voiced)
    TTSRuntimeNode._on_preparation_fallback_execution_trace(node, _accepted_trace())

    rejected = _accepted_trace("predict-adson-2")
    rejected.stage = "sent"
    TTSRuntimeNode._on_preparation_fallback_skill_command(
        node,
        _skill_command("predict-adson-2"),
    )
    TTSRuntimeNode._on_preparation_fallback_execution_trace(node, rejected)

    assert submitted == []
