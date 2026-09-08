from __future__ import annotations

import json

import pytest

from tts_runtime.announcements import (
    DEFAULT_TOOL_ALIASES,
    EXECUTION_ANNOUNCEMENT_SCHEMA,
    ExecutionAnnouncementResolver,
    PROCEDURE_FINISHING_TEXT,
    PROCEDURE_START_TEXT,
    PROCEDURE_STOP_TEXT,
    SURGERY_RECORD_COMPLETED_TEXT,
    default_prewarm_texts,
    execution_announcement_request,
    parse_execution_announcement_fact,
    lifecycle_announcement_request,
    parse_tool_aliases,
    parse_successful_surgery_record_status,
    retraction_adjustment_text,
    short_tool_name,
    tool_action_text,
)


def _active(resolver: ExecutionAnnouncementResolver) -> None:
    resolver.observe_gateway_info(
        gateway_instance_id="gateway-1",
        procedure_run_id="run-1",
        procedure_active=True,
    )


def _tool_command(
    resolver: ExecutionAnnouncementResolver,
    *,
    command_id: str = "tool-1",
    action: str = "pick_up_and_handover",
    instrument_id: str = "T04",
    request_generation: int = 0,
    voice_backed: bool = False,
):
    return resolver.observe_skill_command(
        command_id=command_id,
        action=action,
        instrument_id=instrument_id,
        request_generation=request_generation,
        voice_backed=voice_backed,
    )


def _tool_submission(
    resolver: ExecutionAnnouncementResolver,
    *,
    command_id: str = "tool-1",
):
    return resolver.observe_execution_trace(
        command_id=command_id,
        route="tool_transfer",
        transport="action",
        stage="sent",
        evidence="submission_only",
        dispatch_submitted=True,
    )


def _tool_accepted(
    resolver: ExecutionAnnouncementResolver,
    *,
    command_id: str = "tool-1",
):
    return resolver.observe_execution_trace(
        command_id=command_id,
        route="tool_transfer",
        transport="action",
        stage="accepted",
        evidence="goal_response",
        dispatch_submitted=True,
    )


@pytest.mark.parametrize(
    ("instrument_id", "expected"),
    [
        ("T04", "보비"),
        ("Bovie surgical cautery#2", "보비"),
        ("T07", "바이폴라"),
        ("Bipolar Forceps", "바이폴라"),
        ("T02", "애드슨"),
        ("T08", "모스키토"),
    ],
)
def test_short_tool_names_are_korean_and_instance_agnostic(
    instrument_id: str, expected: str
) -> None:
    aliases = parse_tool_aliases(DEFAULT_TOOL_ALIASES)
    assert short_tool_name(instrument_id, aliases) == expected


def test_execution_owned_fact_builds_one_stable_tool_announcement() -> None:
    run_id = "a" * 32
    payload = {
        "schema": EXECUTION_ANNOUNCEMENT_SCHEMA,
        "command_id": "handover-42",
        "procedure_run_id": run_id,
        "route": "tool_transfer",
        "action": "pick_up_and_handover",
        "instrument_id": "T04",
        "request_generation": 42,
        "voice_backed": True,
    }
    fact = parse_execution_announcement_fact(json.dumps(payload))

    assert fact is not None
    first = execution_announcement_request(
        fact,
        gateway_instance_id="gateway-1",
        procedure_run_id=run_id,
        aliases=parse_tool_aliases(DEFAULT_TOOL_ALIASES),
    )
    second = execution_announcement_request(
        fact,
        gateway_instance_id="gateway-1",
        procedure_run_id=run_id,
        aliases=parse_tool_aliases(DEFAULT_TOOL_ALIASES),
    )

    assert first is not None
    assert first.text == "보비를 전달드리겠습니다."
    assert second is not None
    assert first.reply_id == second.reply_id


def test_completion_cleanup_fact_has_a_finish_safe_durable_identity() -> None:
    run_id = "a" * 32
    fact = parse_execution_announcement_fact(
        json.dumps(
            {
                "schema": EXECUTION_ANNOUNCEMENT_SCHEMA,
                "command_id": "finish-retrieve-adson",
                "procedure_run_id": run_id,
                "route": "tool_transfer",
                "action": "retrieve_from_mayo",
                "instrument_id": "T02",
                "request_generation": 0,
                "voice_backed": False,
                "completion_cleanup": True,
            }
        )
    )

    assert fact is not None
    assert fact.completion_cleanup is True
    request = execution_announcement_request(
        fact,
        gateway_instance_id="gateway-1",
        procedure_run_id=run_id,
        aliases=parse_tool_aliases(DEFAULT_TOOL_ALIASES),
    )
    assert request is not None
    assert request.text == "애드슨을 회수하겠습니다."
    assert request.reply_id.startswith("completion-cleanup-announcement:")


def test_only_autonomous_retrieval_can_claim_completion_cleanup() -> None:
    payload = {
        "schema": EXECUTION_ANNOUNCEMENT_SCHEMA,
        "command_id": "late-handover",
        "procedure_run_id": "a" * 32,
        "route": "tool_transfer",
        "action": "pick_up_and_handover",
        "instrument_id": "T02",
        "request_generation": 1,
        "voice_backed": True,
        "completion_cleanup": True,
    }

    assert parse_execution_announcement_fact(json.dumps(payload)) is None


def test_voice_replacement_legs_share_one_durable_prepare_announcement() -> None:
    run_id = "a" * 32
    shared = f"voice-prepare:{run_id}:7"
    returned = parse_execution_announcement_fact(
        json.dumps(
            {
                "schema": EXECUTION_ANNOUNCEMENT_SCHEMA,
                "command_id": "return-bovie",
                "procedure_run_id": run_id,
                "route": "tool_transfer",
                "action": "prepare_tool",
                "instrument_id": "T08",
                "request_generation": 7,
                "voice_backed": True,
                "announcement_key": shared,
            }
        )
    )
    prepared = parse_execution_announcement_fact(
        json.dumps(
            {
                "schema": EXECUTION_ANNOUNCEMENT_SCHEMA,
                "command_id": "prepare-mosquito",
                "procedure_run_id": run_id,
                "route": "tool_transfer",
                "action": "prepare_tool",
                "instrument_id": "T08",
                "request_generation": 7,
                "voice_backed": True,
                "announcement_key": shared,
            }
        )
    )

    assert returned is not None
    assert prepared is not None
    aliases = parse_tool_aliases(DEFAULT_TOOL_ALIASES)
    first = execution_announcement_request(
        returned,
        gateway_instance_id="gateway-1",
        procedure_run_id=run_id,
        aliases=aliases,
    )
    second = execution_announcement_request(
        prepared,
        gateway_instance_id="gateway-1",
        procedure_run_id=run_id,
        aliases=aliases,
    )

    assert first is not None
    assert second is not None
    assert first.text == "모스키토를 준비하겠습니다."
    assert first.reply_id == second.reply_id


def test_execution_fact_never_crosses_a_procedure_run_boundary() -> None:
    fact = parse_execution_announcement_fact(
        json.dumps(
            {
                "schema": EXECUTION_ANNOUNCEMENT_SCHEMA,
                "command_id": "retraction-1",
                "procedure_run_id": "b" * 32,
                "route": "retraction",
                "retraction_command": 3,
            }
        )
    )

    assert fact is not None
    assert execution_announcement_request(
        fact,
        gateway_instance_id="gateway-1",
        procedure_run_id="a" * 32,
        aliases=parse_tool_aliases(DEFAULT_TOOL_ALIASES),
    ) is None


@pytest.mark.parametrize(
    ("action", "instrument_id", "expected"),
    [
        ("pick_up_and_handover", "T04", "보비를 전달드리겠습니다."),
        ("pick_up_from_mayo_and_handover", "T04", "보비를 전달드리겠습니다."),
        ("direct_handover", "T07", "바이폴라를 전달드리겠습니다."),
        ("predict_tool", "T02", "애드슨을 준비하겠습니다."),
        ("prepare_tool", "T08", "모스키토를 준비하겠습니다."),
        ("retrieve_from_mayo", "T04", "보비를 회수하겠습니다."),
        ("return_unused_preposition", "T08", ""),
        ("retrieve_from_hand", "T08", ""),
        ("tool_retrieve", "T08", ""),
    ],
)
def test_tool_action_catalog_uses_reviewed_korean_phrases(
    action: str, instrument_id: str, expected: str
) -> None:
    aliases = parse_tool_aliases(DEFAULT_TOOL_ALIASES)
    assert tool_action_text(action, instrument_id, aliases) == expected


def test_tool_announcement_waits_for_actual_action_endpoint_acceptance() -> None:
    resolver = ExecutionAnnouncementResolver()
    _active(resolver)

    assert _tool_command(resolver) == ()
    # The local ROS client returned, but the endpoint has not acknowledged a
    # Goal.  This must be silent.
    assert _tool_submission(resolver) == ()
    announced = _tool_accepted(resolver)

    assert len(announced) == 1
    assert announced[0].text == "보비를 전달드리겠습니다."
    assert announced[0].timing == "immediate"
    assert announced[0].gateway_instance_id == "gateway-1"
    assert announced[0].procedure_run_id == "run-1"


def test_trace_before_skill_command_is_joined_without_losing_the_announcement() -> None:
    resolver = ExecutionAnnouncementResolver()
    _active(resolver)

    assert _tool_accepted(resolver) == ()
    announced = _tool_command(resolver, action="predict_tool", instrument_id="T07")

    assert [request.text for request in announced] == ["바이폴라를 준비하겠습니다."]


def test_tool_announcement_is_once_per_command_even_when_trace_repeats() -> None:
    resolver = ExecutionAnnouncementResolver()
    _active(resolver)
    _tool_command(resolver)

    first = _tool_accepted(resolver)
    duplicate = _tool_accepted(resolver)

    assert len(first) == 1
    assert duplicate == ()


def test_voice_handover_reply_is_recognized_as_a_duplicate_execution_announcement() -> None:
    resolver = ExecutionAnnouncementResolver()
    _active(resolver)
    resolver.observe_voice_intent(
        utterance_id="utterance-7",
        request_generation=7,
        accepted=True,
    )
    _tool_command(
        resolver,
        command_id="tool-voice-7",
        action="pick_up_and_handover",
        instrument_id="T08",
        request_generation=7,
        voice_backed=True,
    )
    assert _tool_accepted(resolver, command_id="tool-voice-7")

    # The VLM may spell the same tool in English while the deterministic
    # observer uses the concise Korean alias. Both must represent one audible
    # acknowledgement for the same typed voice turn.
    assert resolver.is_duplicate_voice_reply(
        utterance_id="utterance-7",
        text="Mosquito 전달드리겠습니다.",
    )


def test_direct_typed_voice_proposal_suppresses_the_matching_late_vlm_reply() -> None:
    resolver = ExecutionAnnouncementResolver()
    _active(resolver)
    resolver.observe_typed_voice_tool_intent(
        utterance_id="asr-typed-2",
        intent="tool_handover",
        tool_id="T02",
        accepted=True,
    )
    # The VLM must not make a success-like announcement while this typed
    # request has not reached an Action endpoint yet.
    assert resolver.is_duplicate_voice_reply(
        utterance_id="asr-typed-2",
        text="Adson 전달드리겠습니다.",
    )
    _tool_command(
        resolver,
        command_id="tool-typed-2",
        action="pick_up_and_handover",
        instrument_id="T02",
        request_generation=12,
        voice_backed=True,
    )
    assert _tool_accepted(resolver, command_id="tool-typed-2")

    assert resolver.is_duplicate_voice_reply(
        utterance_id="asr-typed-2",
        text="Adson 전달드리겠습니다.",
    )


def test_only_autonomous_mayo_recovery_gets_recovery_speech() -> None:
    resolver = ExecutionAnnouncementResolver()
    _active(resolver)

    _tool_command(
        resolver,
        command_id="auto-retrieve",
        action="retrieve_from_mayo",
        instrument_id="T04",
    )
    assert [request.text for request in _tool_accepted(resolver, command_id="auto-retrieve")] == [
        "보비를 회수하겠습니다."
    ]

    resolver.observe_explicit_retrieval_request(tool_id="T08", accepted=True)
    _tool_command(
        resolver,
        command_id="manual-retrieve",
        action="retrieve_from_mayo",
        instrument_id="T08",
    )
    assert _tool_accepted(resolver, command_id="manual-retrieve") == ()

    _tool_command(
        resolver,
        command_id="park-preparation",
        action="return_unused_preposition",
        instrument_id="T07",
    )
    assert _tool_accepted(resolver, command_id="park-preparation") == ()


def test_voice_request_replacing_preposition_speaks_when_return_is_accepted() -> None:
    resolver = ExecutionAnnouncementResolver()
    _active(resolver)

    # The typed request and TwinEvent share an utterance but may arrive on
    # separate DDS paths.  Together they identify this exact explicit request.
    assert resolver.observe_typed_voice_tool_intent(
        utterance_id="asr-bovie-17",
        intent="tool_handover",
        tool_id="T04",
        accepted=True,
    ) == ()
    assert resolver.observe_voice_intent(
        utterance_id="asr-bovie-17",
        request_generation=17,
        accepted=True,
    ) == ()

    # T07 was the already-held prediction.  Parking it remains silent until
    # the real Action endpoint accepts it, then announces the requested T04.
    assert _tool_command(
        resolver,
        command_id="return-predicted-t07",
        action="return_unused_preposition",
        instrument_id="T07",
        request_generation=17,
        voice_backed=False,
    ) == ()
    announced = _tool_accepted(resolver, command_id="return-predicted-t07")
    assert [request.text for request in announced] == ["보비를 전달드리겠습니다."]

    # The second Action is the actual requested pickup.  It must not duplicate
    # the wording that was released by the preceding accepted return Action.
    assert _tool_command(
        resolver,
        command_id="pickup-requested-t04",
        action="pick_up_and_handover",
        instrument_id="T04",
        request_generation=17,
        voice_backed=True,
    ) == ()
    assert _tool_accepted(resolver, command_id="pickup-requested-t04") == ()


def test_late_voice_evidence_releases_already_accepted_replacement_return() -> None:
    resolver = ExecutionAnnouncementResolver()
    _active(resolver)

    _tool_command(
        resolver,
        command_id="return-predicted-t07",
        action="return_unused_preposition",
        instrument_id="T07",
        request_generation=23,
    )
    assert _tool_accepted(resolver, command_id="return-predicted-t07") == ()

    assert resolver.observe_voice_intent(
        utterance_id="asr-adson-23",
        request_generation=23,
        accepted=True,
    ) == ()
    announced = resolver.observe_typed_voice_tool_intent(
        utterance_id="asr-adson-23",
        intent="tool_handover",
        tool_id="T02",
        accepted=True,
    )

    assert [request.text for request in announced] == ["애드슨을 전달드리겠습니다."]


def test_typed_manual_retrieval_suppresses_vlm_wording_until_real_execution() -> None:
    resolver = ExecutionAnnouncementResolver()
    _active(resolver)
    resolver.observe_typed_voice_tool_intent(
        utterance_id="asr-retrieve-1",
        intent="tool_retrieve",
        tool_id="T08",
        accepted=True,
    )

    assert resolver.is_duplicate_voice_reply(
        utterance_id="asr-retrieve-1",
        text="모스키토를 회수하겠습니다.",
    )


def test_unrelated_or_non_voice_tool_reply_is_not_suppressed() -> None:
    resolver = ExecutionAnnouncementResolver()
    _active(resolver)
    resolver.observe_voice_intent(
        utterance_id="utterance-7",
        request_generation=7,
        accepted=True,
    )
    _tool_command(
        resolver,
        command_id="tool-autonomous-7",
        action="pick_up_and_handover",
        instrument_id="T08",
        request_generation=7,
        voice_backed=False,
    )

    assert not resolver.is_duplicate_voice_reply(
        utterance_id="utterance-7",
        text="모스키토를 전달드리겠습니다.",
    )
    assert not resolver.is_duplicate_voice_reply(
        utterance_id="utterance-7",
        text="바이폴라를 전달드리겠습니다.",
    )


@pytest.mark.parametrize(
    ("command", "expected", "target_side", "distance_m"),
    [
        (1, "직접교시를 시작합니다.", 0, 0.0),
        (2, "직접교시를 종료합니다.", 0, 0.0),
        (3, "리트랙션을 시작합니다.", 0, 0.0),
        (4, "오른쪽으로 1 센티 더 당기겠습니다.", 2, 0.01),
        (5, "도구를 교체합니다.", 0, 0.0),
        (6, "리트랙션을 종료합니다.", 0, 0.0),
        (7, "석션 들어가겠습니다.", 0, 0.0),
        (8, "석션 빼겠습니다.", 0, 0.0),
    ],
)
def test_retraction_service_catalog_announces_when_endpoint_accepts(
    command: int, expected: str, target_side: int, distance_m: float
) -> None:
    resolver = ExecutionAnnouncementResolver()
    _active(resolver)

    announced = resolver.observe_execution_trace(
        command_id=f"retraction-{command}",
        route="retraction",
        transport="service",
        stage="accepted",
        evidence="service_admission_only",
        dispatch_submitted=True,
        retraction_command=command,
        retraction_target_side=target_side,
        retraction_distance_m=distance_m,
    )

    assert [request.text for request in announced] == [expected]


@pytest.mark.parametrize(
    ("target_side", "distance_m", "expected"),
    [
        (1, 0.005, "왼쪽으로 0.5 센티 더 당기겠습니다."),
        (2, -0.01, "오른쪽으로 1 센티 덜 당기겠습니다."),
        (3, 0.02, "양쪽으로 2 센티 더 당기겠습니다."),
        (0, -0.005, "0.5 센티 덜 당기겠습니다."),
    ],
)
def test_retraction_adjustment_text_uses_received_values(
    target_side: int, distance_m: float, expected: str
) -> None:
    assert retraction_adjustment_text(target_side, distance_m) == expected


def test_retraction_adjustment_text_rejects_missing_distance() -> None:
    assert retraction_adjustment_text(2, 0.0) == ""


def test_dynamic_retraction_fact_uses_string_none_side_without_prefix() -> None:
    run_id = "a" * 32
    fact = parse_execution_announcement_fact(
        json.dumps(
            {
                "schema": EXECUTION_ANNOUNCEMENT_SCHEMA,
                "command_id": "retraction-4-none",
                "procedure_run_id": run_id,
                "route": "retraction",
                "retraction_command": 4,
                "target_side": "none",
                "distance_m": "0.005",
            }
        )
    )

    assert fact is not None
    request = execution_announcement_request(
        fact,
        gateway_instance_id="gateway-1",
        procedure_run_id=run_id,
        aliases=parse_tool_aliases(DEFAULT_TOOL_ALIASES),
    )
    assert request is not None
    assert request.text == "0.5 센티 더 당기겠습니다."


@pytest.mark.parametrize(
    "overrides",
    [
        {"stage": "sent"},
        {"dispatch_submitted": False},
        {"route": "other"},
        {"transport": "action"},
        {"evidence": "submission_only"},
        {"retraction_command": 0},
        {"retraction_command": 255},
    ],
)
def test_non_accepted_or_unknown_service_facts_never_announce(overrides: dict) -> None:
    resolver = ExecutionAnnouncementResolver()
    _active(resolver)
    payload = {
        "command_id": "retraction-1",
        "route": "retraction",
        "transport": "service",
        "stage": "accepted",
        "evidence": "service_admission_only",
        "dispatch_submitted": True,
        "retraction_command": 3,
    }
    payload.update(overrides)

    assert resolver.observe_execution_trace(**payload) == ()


def test_scope_change_prevents_old_command_facts_from_crossing_to_new_run() -> None:
    resolver = ExecutionAnnouncementResolver()
    _active(resolver)
    _tool_command(resolver)
    resolver.observe_gateway_info(
        gateway_instance_id="gateway-2",
        procedure_run_id="run-2",
        procedure_active=True,
    )

    assert _tool_accepted(resolver) == ()


def test_no_active_gateway_scope_means_no_audio_request() -> None:
    resolver = ExecutionAnnouncementResolver()

    assert _tool_command(resolver) == ()
    assert _tool_accepted(resolver) == ()
    assert resolver.observe_execution_trace(
        command_id="retraction-1",
        route="retraction",
        transport="service",
        stage="accepted",
        evidence="service_admission_only",
        dispatch_submitted=True,
        retraction_command=7,
    ) == ()


def test_tts_owner_aliases_can_be_overridden_without_a_planner_change() -> None:
    resolver = ExecutionAnnouncementResolver(tool_aliases=("T04=전기메스",))
    _active(resolver)
    _tool_command(resolver)

    announced = _tool_accepted(resolver)

    assert [request.text for request in announced] == ["전기메스를 전달드리겠습니다."]


def test_invalid_alias_configuration_is_explicit() -> None:
    with pytest.raises(ValueError, match="instrument=spoken_name"):
        ExecutionAnnouncementResolver(tool_aliases=("T04",))


def test_prewarm_catalog_covers_common_tools_and_every_service_phrase() -> None:
    texts = default_prewarm_texts()

    assert "보비를 전달드리겠습니다." in texts
    assert "바이폴라를 준비하겠습니다." in texts
    assert "애드슨을 회수하겠습니다." in texts
    assert "직접교시를 시작합니다." in texts
    assert "석션 들어가겠습니다." in texts
    assert "석션 빼겠습니다." in texts
    assert PROCEDURE_START_TEXT in texts
    assert PROCEDURE_FINISHING_TEXT in texts
    assert PROCEDURE_STOP_TEXT in texts
    assert SURGERY_RECORD_COMPLETED_TEXT in texts


def test_lifecycle_announcements_are_deterministic_and_run_scoped() -> None:
    started = lifecycle_announcement_request(
        event="procedure_start",
        gateway_instance_id="gateway-1",
        procedure_run_id="run-1",
    )
    repeated = lifecycle_announcement_request(
        event="procedure_start",
        gateway_instance_id="gateway-1",
        procedure_run_id="run-1",
    )
    stopped = lifecycle_announcement_request(
        event="procedure_stop",
        gateway_instance_id="gateway-1",
        procedure_run_id="run-1",
    )

    assert started is not None
    assert repeated is not None
    assert stopped is not None
    assert started.reply_id == repeated.reply_id
    assert started.reply_id != stopped.reply_id
    assert started.text == PROCEDURE_START_TEXT
    finishing = lifecycle_announcement_request(
        event="procedure_finishing",
        gateway_instance_id="gateway-1",
        procedure_run_id="run-1",
    )
    assert finishing is not None
    assert finishing.text == PROCEDURE_FINISHING_TEXT
    assert stopped.text == PROCEDURE_STOP_TEXT


def test_surgery_record_completion_requires_confirmed_bounded_success() -> None:
    payload = {
        "schema": "taskplanner.operational_surgery_record.status.v1",
        "request_id": "record-1",
        "procedure_run_id": "run-1",
        "submit_state": "SUCCEEDED",
        "success": True,
        "completed_at": "2026-08-29T16:00:00+09:00",
    }

    completion = parse_successful_surgery_record_status(json.dumps(payload))

    assert completion is not None
    assert completion.request_id == "record-1"
    assert completion.procedure_run_id == "run-1"
    assert parse_successful_surgery_record_status(
        json.dumps({**payload, "success": False})
    ) is None
    assert parse_successful_surgery_record_status(
        json.dumps({**payload, "submit_state": "FAILED"})
    ) is None
    assert parse_successful_surgery_record_status("not-json") is None
