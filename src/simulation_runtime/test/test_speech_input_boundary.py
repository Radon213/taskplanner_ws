from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from builtin_interfaces.msg import Time
import pytest

from procedure_spec import load_bundle
from simulation_runtime.llm_surgeon_actor import LLMSurgeonActorNode
from simulation_runtime.speech_input_adapter import (
    RecentSentences,
    RecentUtteranceIds,
    evaluate_utterance,
    normalize_sentence_text,
)
from surgical_msgs.msg import BedRobotArmGroupRequest, SpeechUtterance


def _utterance(**overrides) -> SpeechUtterance:
    msg = SpeechUtterance()
    msg.stamp = Time(sec=100)
    msg.start_stamp = Time(sec=99)
    msg.end_stamp = Time(sec=100)
    msg.utterance_id = "utt-1"
    msg.text = "Bovie surgical cautery please"
    msg.is_final = True
    msg.has_confidence = True
    msg.confidence = 0.91
    msg.speaker_role = "surgeon"
    msg.language = "en"
    msg.source = "test_asr"
    for name, value in overrides.items():
        setattr(msg, name, value)
    return msg


def _evaluate(msg: SpeechUtterance):
    return evaluate_utterance(
        msg,
        now_sec=101.0,
        required_speaker_role="surgeon",
        min_confidence=0.55,
        accept_missing_confidence=True,
        require_timestamp=True,
        max_age_sec=3.0,
        max_future_skew_sec=1.0,
    )


def test_final_fresh_surgeon_utterance_is_admitted() -> None:
    result = _evaluate(_utterance())
    assert result.accepted is True
    assert result.text == "Bovie surgical cautery please"


def test_fresh_envelope_stamp_accepts_future_interval_end() -> None:
    result = _evaluate(
        _utterance(
            stamp=Time(sec=100),
            start_stamp=Time(sec=100),
            end_stamp=Time(sec=103),
        )
    )
    assert result.accepted is True


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"is_final": False}, "interim_transcript"),
        ({"confidence": 0.2}, "low_confidence"),
        ({"speaker_role": "nurse"}, "unexpected_speaker_role"),
        ({"end_stamp": Time(sec=90), "stamp": Time(sec=90)}, "stale"),
        (
            {
                "end_stamp": Time(),
                "stamp": Time(),
                "start_stamp": Time(),
            },
            "missing_timestamp",
        ),
    ],
)
def test_untrusted_speech_is_rejected(overrides: dict, reason: str) -> None:
    result = _evaluate(_utterance(**overrides))
    assert result.accepted is False
    assert result.reason.startswith(reason)


def test_utterance_ids_are_deduplicated_and_expire() -> None:
    recent = RecentUtteranceIds(retention_sec=10.0)
    assert recent.accept("utt-1", 100.0) is True
    assert recent.accept("utt-1", 101.0) is False
    assert recent.accept("utt-1", 111.0) is True


def test_sentence_text_is_normalized_without_asr_metadata() -> None:
    assert normalize_sentence_text("  Bovie   please \n") == "Bovie please"


def test_sentence_text_deduplication_is_short_and_case_insensitive() -> None:
    recent = RecentSentences(retention_sec=1.0)
    assert recent.accept("Bovie please", 100.0) is True
    assert recent.accept("  bovie   PLEASE ", 100.2) is False
    assert recent.accept("Bovie please", 101.1) is True


def test_llm_actor_publishes_sensor_contract_not_legacy_string() -> None:
    published: list[SpeechUtterance] = []
    actor = LLMSurgeonActorNode.__new__(LLMSurgeonActorNode)
    actor._speech_pub = SimpleNamespace(publish=published.append)
    actor._stamp = lambda: Time(sec=42)

    actor._publish_voice("Suction, please")

    assert len(published) == 1
    msg = published[0]
    assert msg.text == "Suction, please"
    assert msg.is_final is True
    assert msg.speaker_role == "surgeon"
    assert msg.source == "llm_surgeon_actor"
    assert msg.utterance_id.startswith("actor-")


def test_actor_binds_group_request_created_by_public_voice_router() -> None:
    spec_dir = (
        Path(__file__).parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
        / "thyroidectomy"
    )
    actor = LLMSurgeonActorNode.__new__(LLMSurgeonActorNode)
    actor._spec = load_bundle(spec_dir)
    actor._pending_group_requests = {
        "suction": {
            "request_id": "",
            "operation": "suction_start",
            "speech": "석션 시작",
        }
    }
    request = BedRobotArmGroupRequest()
    request.request_id = "voice-123"
    request.group_id = "suction"
    request.operation = "suction_start"
    request.source = "deterministic_voice_router"

    actor._on_bed_robot_arm_group_request(request)

    assert actor._pending_group_requests["suction"]["request_id"] == "voice-123"
