from __future__ import annotations

import hashlib
import json

import pytest

from tts_runtime.feedback import (
    RETRIEVAL_ACTIONS,
    RETRIEVAL_PROGRESS_STATES,
    RETRIEVAL_STARTED_SEMANTIC_KEY,
    RetrievalFeedbackAnnouncer,
    retrieval_feedback_digest,
)


def activate(
    announcer: RetrievalFeedbackAnnouncer,
    *,
    gateway: str = "gateway-1",
    run: str = "run-1",
    procedure: str = "thyroidectomy_demo",
    active: bool = True,
    stamp_ns: int = 0,
) -> None:
    announcer.observe_gateway_info(
        gateway_instance_id=gateway,
        procedure_run_id=run,
        procedure_type=procedure,
        procedure_active=active,
        stamp_ns=stamp_ns,
    )


def status(
    announcer: RetrievalFeedbackAnnouncer,
    *,
    command_id: str = "command-1",
    action: str = "retrieve_from_mayo",
    state: str = "moving_to_source",
    success: bool = True,
    message: str = "executing",
    stamp_ns: int = 0,
):
    return announcer.observe_skill_status(
        command_id=command_id,
        action=action,
        state=state,
        success=success,
        message=message,
        stamp_ns=stamp_ns,
    )


@pytest.mark.parametrize("action", sorted(RETRIEVAL_ACTIONS))
@pytest.mark.parametrize("state", sorted(RETRIEVAL_PROGRESS_STATES))
def test_every_allowed_action_and_controller_progress_state_emits(
    action: str, state: str
) -> None:
    announcer = RetrievalFeedbackAnnouncer()
    activate(announcer)

    request = status(announcer, action=action, state=state)

    assert request is not None
    assert request.text == "도구 회수중입니다"
    assert request.timing == "immediate"
    assert request.gateway_instance_id == "gateway-1"
    assert request.procedure_run_id == "run-1"
    assert request.function_call_name == ""
    assert request.function_arguments_json == ""
    assert request.function_request_id == ""


def test_reply_identity_is_canonical_stable_and_run_scoped() -> None:
    material = {
        "command_id": "command-1",
        "gateway_instance_id": "gateway-1",
        "procedure_run_id": "run-1",
        "schema": "taskplanner.tts.fixed-feedback.v1",
        "semantic_key": RETRIEVAL_STARTED_SEMANTIC_KEY,
    }
    expected = hashlib.sha256(
        json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    first = RetrievalFeedbackAnnouncer()
    second = RetrievalFeedbackAnnouncer()
    activate(first)
    activate(second)
    first_request = status(first)
    second_request = status(second)

    assert expected == retrieval_feedback_digest(
        gateway_instance_id="gateway-1",
        procedure_run_id="run-1",
        command_id="command-1",
    )
    assert first_request == second_request
    assert first_request.reply_id == f"fixed-feedback:{expected}"
    assert first_request.turn_id == f"fixed-turn:{expected}"
    assert first_request.utterance_id == f"fixed-utterance:{expected}"


def test_repeated_feedback_and_later_progress_states_emit_once_per_command() -> None:
    announcer = RetrievalFeedbackAnnouncer()
    activate(announcer)

    first = status(announcer)
    duplicate = status(announcer)
    later = status(announcer, state="holding")

    assert first is not None
    assert duplicate is None
    assert later is None


def test_distinct_commands_each_emit_once() -> None:
    announcer = RetrievalFeedbackAnnouncer()
    activate(announcer)

    first = status(announcer, command_id="command-1")
    second = status(announcer, command_id="command-2")

    assert first is not None
    assert second is not None
    assert first.reply_id != second.reply_id


@pytest.mark.parametrize(
    "overrides",
    [
        {"command_id": ""},
        {"action": "pick_up_and_handover"},
        {"action": "Retrieve_From_Mayo"},
        {"state": "accepted"},
        {"state": "completed"},
        {"state": "Moving_To_Source"},
        {"success": False},
        {"message": "queued"},
        {"message": " executing"},
        {"message": "executing "},
    ],
)
def test_non_controller_or_nonexact_feedback_never_emits(overrides: dict) -> None:
    announcer = RetrievalFeedbackAnnouncer()
    activate(announcer)

    assert status(announcer, **overrides) is None


def test_invalid_feedback_does_not_poison_later_exact_feedback() -> None:
    announcer = RetrievalFeedbackAnnouncer()
    activate(announcer)

    assert status(announcer, state="accepted") is None
    assert status(announcer, success=False) is None
    assert status(announcer) is not None


@pytest.mark.parametrize(
    "scope",
    [
        {},
        {"gateway": ""},
        {"run": ""},
        {"active": False},
        {"procedure": "inguinal_hernia_repair_demo"},
        {"procedure": "thyroidectomy"},
    ],
)
def test_unestablished_inactive_or_disallowed_scope_never_emits(scope: dict) -> None:
    announcer = RetrievalFeedbackAnnouncer()
    if scope:
        activate(announcer, **scope)

    assert status(announcer) is None


def test_inguinal_hernia_demo_never_announces_tool_retrieval() -> None:
    announcer = RetrievalFeedbackAnnouncer()
    activate(announcer, procedure="inguinal_hernia_repair_demo")

    for action in RETRIEVAL_ACTIONS:
        for state in RETRIEVAL_PROGRESS_STATES:
            assert status(announcer, action=action, state=state) is None


def test_scope_change_resets_process_dedupe_but_changes_durable_identity() -> None:
    announcer = RetrievalFeedbackAnnouncer()
    activate(announcer, run="run-1")
    first = status(announcer)

    activate(announcer, run="run-2")
    second = status(announcer)

    assert first is not None and second is not None
    assert second.procedure_run_id == "run-2"
    assert first.reply_id != second.reply_id


def test_gateway_change_resets_process_dedupe_and_identity() -> None:
    announcer = RetrievalFeedbackAnnouncer()
    activate(announcer, gateway="gateway-1")
    first = status(announcer)

    activate(announcer, gateway="gateway-2")
    second = status(announcer)

    assert first is not None and second is not None
    assert first.reply_id != second.reply_id


def test_inactive_and_other_procedure_transitions_reset_noise_dedupe() -> None:
    announcer = RetrievalFeedbackAnnouncer()
    activate(announcer)
    first = status(announcer)

    activate(announcer, active=False)
    assert status(announcer) is None
    activate(announcer, procedure="inguinal_hernia_repair_demo")
    assert status(announcer) is None
    activate(announcer)
    replay = status(announcer)

    # Same scope material deliberately produces the same durable reply ID.
    # PlaybackStore is the final authority and will suppress this redelivery.
    assert first is not None and replay is not None
    assert replay.reply_id == first.reply_id


def test_repeated_gateway_heartbeat_does_not_reset_dedupe_or_move_boundary() -> None:
    announcer = RetrievalFeedbackAnnouncer()
    activate(announcer, stamp_ns=100)
    first = status(announcer, stamp_ns=101)

    activate(announcer, stamp_ns=1_000)
    duplicate = status(announcer, stamp_ns=102)

    assert first is not None
    assert duplicate is None
    assert announcer.scope.started_stamp_ns == 100


@pytest.mark.parametrize("status_stamp_ns", [0, 99, 100])
def test_stale_or_unstamped_retained_feedback_is_rejected(
    status_stamp_ns: int,
) -> None:
    announcer = RetrievalFeedbackAnnouncer()
    activate(announcer, stamp_ns=100)

    assert status(announcer, stamp_ns=status_stamp_ns) is None


def test_fresh_feedback_after_scope_boundary_emits() -> None:
    announcer = RetrievalFeedbackAnnouncer()
    activate(announcer, stamp_ns=100)

    assert status(announcer, stamp_ns=101) is not None


def test_status_observed_before_scope_is_not_buffered_or_relabelled() -> None:
    announcer = RetrievalFeedbackAnnouncer()

    assert status(announcer, stamp_ns=50) is None
    activate(announcer, run="run-2", stamp_ns=100)
    assert status(announcer, stamp_ns=50) is None
    assert status(announcer, stamp_ns=101) is not None


def test_whitespace_is_normalized_for_ids_but_message_remains_exact() -> None:
    announcer = RetrievalFeedbackAnnouncer()
    activate(
        announcer,
        gateway=" gateway-1 ",
        run=" run-1 ",
        procedure=" thyroidectomy_demo ",
    )

    request = status(
        announcer,
        command_id=" command-1 ",
        action=" retrieve_from_mayo ",
        state=" moving_to_source ",
    )

    assert request is not None
    assert request.gateway_instance_id == "gateway-1"
    assert request.procedure_run_id == "run-1"
