from __future__ import annotations

import json

from tts_runtime.core import PlaybackEvent
from tts_runtime.correlation import (
    AuthoritativeTimingCorrelator,
    filter_waiting_for_active_run,
)


def waiting_job(
    *,
    reply_id: str = "reply-1",
    utterance_id: str = "utterance-1",
    function_name: str = "request_tool_handover",
    arguments: dict[str, object] | None = None,
    timing: str = "on_function_accepted",
) -> PlaybackEvent:
    if arguments is None:
        arguments = {"tool_id": "T07"}
    return PlaybackEvent(
        sequence=1,
        reply_id=reply_id,
        turn_id=f"turn:{utterance_id}",
        utterance_id=utterance_id,
        gateway_instance_id="gateway-1",
        procedure_run_id="run-1",
        function_call_name=function_name,
        function_arguments_json=json.dumps(arguments, separators=(",", ":")),
        function_request_id=f"function:{reply_id}",
        state=f"waiting_function_{timing.removeprefix('on_function_')}",
        timing=timing,
        text="확인했습니다",
        text_sha256="0" * 64,
        voice_id="F1",
        output_device="default",
        cache_key="1" * 64,
        synth_latency_ms=0.0,
        audio_duration_sec=0.0,
        playback_latency_ms=0.0,
        terminal=False,
        success=False,
        error_code="",
        message="waiting_for_release",
    )


def harness(*jobs: PlaybackEvent):
    pending = {job.reply_id: job for job in jobs}
    released: list[str] = []

    def release(reply_id: str):
        value = pending.pop(reply_id, None)
        if value is not None:
            released.append(reply_id)
        return value

    correlator = AuthoritativeTimingCorrelator(
        waiting_provider=lambda: list(pending.values()),
        release_waiting=release,
    )
    return correlator, pending, released


def add_voice_and_handover_command(
    correlator: AuthoritativeTimingCorrelator,
    *,
    utterance_id: str = "utterance-1",
    generation: int = 7,
    command_id: str = "command-1",
    action: str = "pick_up_and_handover",
    tool_id: str = "T07",
    voice_backed: bool = True,
) -> None:
    correlator.observe_voice_intent(
        utterance_id=utterance_id,
        request_generation=generation,
        accepted=True,
    )
    correlator.observe_skill_command(
        command_id=command_id,
        request_generation=generation,
        voice_backed=voice_backed,
        action=action,
        instrument_id=tool_id,
    )


def test_handover_acceptance_requires_status_and_goal_response_trace() -> None:
    correlator, pending, released = harness(waiting_job())
    add_voice_and_handover_command(correlator)
    correlator.observe_skill_status(
        command_id="command-1", state="accepted", success=True
    )
    assert released == []

    result = correlator.observe_execution_trace(
        command_id="command-1",
        stage="accepted",
        terminal=False,
        evidence="goal_response",
    )
    assert result == ["reply-1"]
    assert released == ["reply-1"]
    assert not pending


def test_accepted_fact_survives_later_feedback_and_topic_reordering() -> None:
    correlator, _pending, released = harness(waiting_job())
    add_voice_and_handover_command(correlator)
    correlator.observe_skill_status(
        command_id="command-1", state="accepted", success=True
    )
    correlator.observe_skill_status(
        command_id="command-1", state="moving_to_target", success=True
    )
    correlator.observe_execution_trace(
        command_id="command-1",
        stage="accepted",
        terminal=False,
        evidence="goal_response",
    )
    assert released == ["reply-1"]


def test_command_and_execution_evidence_may_arrive_before_twin_event() -> None:
    correlator, _pending, released = harness(waiting_job())
    correlator.observe_skill_command(
        command_id="command-1",
        request_generation=7,
        voice_backed=True,
        action="direct_handover",
        instrument_id="T07",
    )
    correlator.observe_skill_status(
        command_id="command-1", state="accepted", success=True
    )
    correlator.observe_execution_trace(
        command_id="command-1",
        stage="accepted",
        terminal=False,
        evidence="goal_response",
    )
    assert released == []
    correlator.observe_voice_intent(
        utterance_id="utterance-1", request_generation=7, accepted=True
    )
    assert released == ["reply-1"]


def test_handover_completion_requires_successful_terminal_controller_result() -> None:
    job = waiting_job(timing="on_function_completed")
    correlator, _pending, released = harness(job)
    add_voice_and_handover_command(correlator)
    correlator.observe_skill_status(
        command_id="command-1", state="accepted", success=True
    )
    correlator.observe_execution_trace(
        command_id="command-1",
        stage="accepted",
        terminal=False,
        evidence="goal_response",
    )
    assert released == []
    correlator.observe_execution_trace(
        command_id="command-1",
        stage="completed",
        terminal=True,
        evidence="controller_result",
    )
    assert released == []
    correlator.observe_skill_status(
        command_id="command-1", state="completed", success=True
    )
    assert released == ["reply-1"]


def test_wrong_tool_and_return_preposition_never_release_handover() -> None:
    correlator, _pending, released = harness(waiting_job())
    add_voice_and_handover_command(
        correlator,
        command_id="return-command",
        action="return_unused_preposition",
    )
    correlator.observe_skill_status(
        command_id="return-command", state="accepted", success=True
    )
    correlator.observe_execution_trace(
        command_id="return-command",
        stage="accepted",
        terminal=False,
        evidence="goal_response",
    )
    add_voice_and_handover_command(
        correlator,
        command_id="wrong-tool",
        tool_id="T04",
    )
    correlator.observe_skill_status(
        command_id="wrong-tool", state="accepted", success=True
    )
    correlator.observe_execution_trace(
        command_id="wrong-tool",
        stage="accepted",
        terminal=False,
        evidence="goal_response",
    )
    assert released == []


def test_non_voice_command_and_nonpositive_generation_fail_closed() -> None:
    correlator, _pending, released = harness(waiting_job())
    correlator.observe_voice_intent(
        utterance_id="utterance-1", request_generation=0, accepted=True
    )
    correlator.observe_skill_command(
        command_id="command-1",
        request_generation=7,
        voice_backed=False,
        action="direct_handover",
        instrument_id="T07",
    )
    correlator.observe_skill_status(
        command_id="command-1", state="accepted", success=True
    )
    correlator.observe_execution_trace(
        command_id="command-1",
        stage="accepted",
        terminal=False,
        evidence="goal_response",
    )
    assert released == []


def test_generation_collision_with_two_utterances_fails_closed() -> None:
    correlator, _pending, released = harness(waiting_job())
    correlator.observe_voice_intent(
        utterance_id="old-utterance", request_generation=7, accepted=True
    )
    correlator.observe_voice_intent(
        utterance_id="utterance-1", request_generation=7, accepted=True
    )
    correlator.observe_skill_command(
        command_id="command-1",
        request_generation=7,
        voice_backed=True,
        action="direct_handover",
        instrument_id="T07",
    )
    correlator.observe_skill_status(
        command_id="command-1", state="accepted", success=True
    )
    correlator.observe_execution_trace(
        command_id="command-1",
        stage="accepted",
        terminal=False,
        evidence="goal_response",
    )
    assert released == []


def test_retraction_acceptance_joins_exact_request_id() -> None:
    job = waiting_job(
        function_name="adjust_retraction",
        arguments={
            "command": "adjust_retraction",
            "target_side": "left",
            "distance_m": 0.005,
        },
    )
    correlator, _pending, released = harness(job)
    correlator.observe_retraction_status(
        request_id="other",
        state="accepted",
        outcome="accepted",
        terminal=True,
        success=True,
    )
    assert released == []
    correlator.observe_retraction_status(
        request_id="function:reply-1",
        state="accepted",
        outcome="accepted",
        terminal=True,
        success=True,
    )
    assert released == ["reply-1"]


def test_retraction_completion_and_tool_retrieval_remain_fail_closed() -> None:
    retraction = waiting_job(
        reply_id="retraction",
        function_name="adjust_retraction",
        arguments={
            "command": "adjust_retraction",
            "target_side": "both",
            "distance_m": 0.001,
        },
        timing="on_function_completed",
    )
    retrieval = waiting_job(
        reply_id="retrieval",
        function_name="request_tool_retrieval",
        arguments={"tool_id": "T07"},
        timing="on_function_accepted",
    )
    correlator, pending, released = harness(retraction, retrieval)
    correlator.observe_retraction_status(
        request_id="function:retraction",
        state="accepted",
        outcome="accepted",
        terminal=True,
        success=True,
    )
    add_voice_and_handover_command(correlator)
    correlator.observe_skill_status(
        command_id="command-1", state="completed", success=True
    )
    correlator.observe_execution_trace(
        command_id="command-1",
        stage="completed",
        terminal=True,
        evidence="controller_result",
    )
    assert released == []
    assert set(pending) == {"retraction", "retrieval"}


def test_invalid_function_arguments_fail_closed() -> None:
    job = waiting_job()
    job = PlaybackEvent(**{**job.__dict__, "function_arguments_json": "not-json"})
    correlator, _pending, released = harness(job)
    add_voice_and_handover_command(correlator)
    correlator.observe_skill_status(
        command_id="command-1", state="accepted", success=True
    )
    correlator.observe_execution_trace(
        command_id="command-1",
        stage="accepted",
        terminal=False,
        evidence="goal_response",
    )
    assert released == []


def test_waiting_filter_requires_exact_active_procedure_run() -> None:
    current = waiting_job(reply_id="current")
    old = PlaybackEvent(
        **{**waiting_job(reply_id="old").__dict__, "procedure_run_id": "run-old"}
    )
    old_gateway = PlaybackEvent(
        **{
            **waiting_job(reply_id="old-gateway").__dict__,
            "gateway_instance_id": "gateway-old",
        }
    )
    assert filter_waiting_for_active_run(
        [current, old], procedure_active=False, procedure_run_id="run-1"
    ) == []
    assert filter_waiting_for_active_run(
        [current, old], procedure_active=True, procedure_run_id=""
    ) == []
    assert filter_waiting_for_active_run(
        [current, old, old_gateway],
        procedure_active=True,
        procedure_run_id="run-1",
        gateway_instance_id="gateway-1",
    ) == [current]
