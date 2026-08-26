from __future__ import annotations

from types import SimpleNamespace

import pytest

from simulation_runtime.simulation_manager import (
    RecentVoiceProcedureIntentIds,
    evaluate_voice_procedure_intent,
)


def _intent(**overrides):
    message = SimpleNamespace(
        header=SimpleNamespace(stamp=SimpleNamespace(sec=100, nanosec=0)),
        utterance_id="utterance-1",
        source="taskplanner_asr",
        source_is_final=True,
        source_speaker_role="surgeon",
        source_has_confidence=True,
        source_confidence=0.91,
        disposition="propose",
        requires_confirmation=False,
        procedure_id="thyroidectomy",
        intent="procedure_start",
    )
    for name, value in overrides.items():
        setattr(message, name, value)
    return message


@pytest.mark.parametrize(
    ("intent", "command"),
    [("procedure_start", "start"), ("procedure_stop", "pause")],
)
def test_final_fresh_active_surgeon_lifecycle_intent_is_admitted(
    intent: str,
    command: str,
) -> None:
    admission = evaluate_voice_procedure_intent(
        _intent(intent=intent),
        active_procedure_id="thyroidectomy",
        now_sec=101.0,
        min_confidence=0.55,
        max_age_sec=3.0,
        max_future_skew_sec=1.0,
        running=intent == "procedure_stop",
        execution_state="running" if intent == "procedure_stop" else "idle",
    )

    assert admission.accepted is True
    assert admission.command == command


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"disposition": "no_command"}, "voice_intent_not_proposed"),
        ({"requires_confirmation": True}, "voice_intent_requires_confirmation"),
        ({"procedure_id": "nephrectomy"}, "voice_intent_procedure_mismatch"),
        ({"source_is_final": False}, "interim_transcript"),
        ({"source_speaker_role": "nurse"}, "unexpected_speaker_role"),
        ({"source_has_confidence": False}, "missing_confidence"),
        ({"source_confidence": 0.2}, "low_confidence"),
        (
            {"header": SimpleNamespace(stamp=SimpleNamespace(sec=90, nanosec=0))},
            "stale",
        ),
    ],
)
def test_lifecycle_intent_rechecks_live_asr_authority(
    overrides: dict,
    reason: str,
) -> None:
    admission = evaluate_voice_procedure_intent(
        _intent(**overrides),
        active_procedure_id="thyroidectomy",
        now_sec=101.0,
        min_confidence=0.55,
        max_age_sec=3.0,
        max_future_skew_sec=1.0,
        running=False,
        execution_state="idle",
    )

    assert admission.accepted is False
    assert admission.reason.startswith(reason)


def test_missing_confidence_is_admitted_only_with_explicit_live_opt_in() -> None:
    admission = evaluate_voice_procedure_intent(
        _intent(source_has_confidence=False, source_confidence=0.0),
        active_procedure_id="thyroidectomy",
        now_sec=101.0,
        min_confidence=0.55,
        accept_missing_confidence=True,
        max_age_sec=3.0,
        max_future_skew_sec=1.0,
        running=False,
        execution_state="idle",
    )

    assert admission.accepted is True
    assert admission.command == "start"


def test_supplied_low_confidence_is_rejected_even_with_live_opt_in() -> None:
    admission = evaluate_voice_procedure_intent(
        _intent(source_has_confidence=True, source_confidence=0.2),
        active_procedure_id="thyroidectomy",
        now_sec=101.0,
        min_confidence=0.55,
        accept_missing_confidence=True,
        max_age_sec=3.0,
        max_future_skew_sec=1.0,
        running=False,
        execution_state="idle",
    )

    assert admission.accepted is False
    assert admission.reason == "low_confidence"


@pytest.mark.parametrize(
    ("intent", "running", "execution_state", "reason"),
    [
        ("procedure_start", True, "running", "procedure_not_idle"),
        ("procedure_start", True, "paused", "procedure_not_idle"),
        ("procedure_start", False, "halted", "procedure_not_idle"),
        ("procedure_stop", False, "idle", "procedure_not_running"),
        ("procedure_stop", True, "paused", "procedure_not_running"),
    ],
)
def test_lifecycle_intent_requires_the_matching_runtime_state(
    intent: str,
    running: bool,
    execution_state: str,
    reason: str,
) -> None:
    admission = evaluate_voice_procedure_intent(
        _intent(intent=intent),
        active_procedure_id="thyroidectomy",
        now_sec=101.0,
        min_confidence=0.55,
        max_age_sec=3.0,
        max_future_skew_sec=1.0,
        running=running,
        execution_state=execution_state,
    )

    assert admission.accepted is False
    assert admission.reason == reason


def test_lifecycle_utterance_identity_is_deduplicated_and_expires() -> None:
    recent = RecentVoiceProcedureIntentIds(retention_sec=10.0)

    assert recent.accept("utterance-1", 100.0) is True
    assert recent.accept("utterance-1", 101.0) is False
    assert recent.accept("utterance-1", 111.0) is True
