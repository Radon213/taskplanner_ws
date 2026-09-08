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
    SpeechInputAdapterNode,
    TTSEchoGuard,
    admitted_utterance,
    evaluate_utterance,
    normalize_sentence_text,
    normalize_tts_echo_text,
    parse_tagged_sentence,
    tagged_sentence_utterance,
)
from std_msgs.msg import String
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


def test_typed_output_preserves_source_timestamp_and_identity() -> None:
    source = _utterance()

    output = admitted_utterance(source, text="Bovie please")

    assert output.text == "Bovie please"
    assert output.utterance_id == source.utterance_id
    assert output.stamp == source.stamp
    assert output.end_stamp == source.end_stamp
    assert output.source == source.source
    assert output.is_final is True


def test_typed_source_is_required_when_live_policy_requests_it() -> None:
    result = evaluate_utterance(
        _utterance(source=""),
        now_sec=101.0,
        required_speaker_role="surgeon",
        min_confidence=0.55,
        accept_missing_confidence=True,
        require_timestamp=True,
        max_age_sec=3.0,
        max_future_skew_sec=1.0,
        require_source=True,
    )

    assert result.accepted is False
    assert result.reason == "missing_source"


def test_sentence_text_is_normalized_without_asr_metadata() -> None:
    assert normalize_sentence_text("  Bovie   please \n") == "Bovie please"


@pytest.mark.parametrize(
    ("raw", "expected_text", "expected_final"),
    [
        ("[partial]  보비   준비", "보비 준비", False),
        (" [FINAL] 보비 주세요 ", "보비 주세요", True),
    ],
)
def test_tagged_sentence_parser_removes_marker_and_preserves_finality(
    raw: str,
    expected_text: str,
    expected_final: bool,
) -> None:
    parsed = parse_tagged_sentence(raw)

    assert parsed is not None
    assert parsed.text == expected_text
    assert parsed.is_final is expected_final


def test_tagged_sentence_parser_does_not_guess_untagged_finality() -> None:
    assert parse_tagged_sentence("보비 주세요") is None


def test_tagged_sentence_builds_typed_receipt_envelope() -> None:
    parsed = parse_tagged_sentence("[final] 보비 주세요")
    assert parsed is not None

    message = tagged_sentence_utterance(
        parsed,
        stamp=Time(sec=42, nanosec=7),
        utterance_id="external-42-7-1",
        source="external_sentence_topic",
    )

    assert message.text == "보비 주세요"
    assert message.is_final is True
    assert message.stamp == Time(sec=42, nanosec=7)
    assert message.utterance_id == "external-42-7-1"
    assert message.speaker_role == "surgeon"
    assert message.has_confidence is False


def _tagged_adapter_harness() -> tuple[SimpleNamespace, list, list, list[str]]:
    finals: list[SpeechUtterance] = []
    partials: list[SpeechUtterance] = []
    rejected: list[str] = []
    adapter = SimpleNamespace(
        _input_mode="tagged_sentence",
        _received_count=0,
        _accepted_count=0,
        _last_source="",
        _last_observation_stamp=None,
        _last_accepted_monotonic=0.0,
        _last_detail="",
        _sentence_source_id="external_sentence_topic",
        _tagged_sequence=0,
        _recent_sentences=RecentSentences(retention_sec=1.0),
        _enable_tts_echo_guard=False,
        _tts_echo_guard=TTSEchoGuard(),
        _transcript_pub=SimpleNamespace(publish=finals.append),
        _partial_pub=SimpleNamespace(publish=partials.append),
        get_clock=lambda: SimpleNamespace(
            now=lambda: SimpleNamespace(to_msg=lambda: Time(sec=42, nanosec=7))
        ),
        _reject=rejected.append,
        _publish_status=lambda: None,
        _tts_echo_detail=SpeechInputAdapterNode._tts_echo_detail,
    )
    return adapter, finals, partials, rejected


def test_tagged_partial_is_observed_but_not_admitted_as_a_command() -> None:
    adapter, finals, partials, rejected = _tagged_adapter_harness()

    SpeechInputAdapterNode._on_sentence(
        adapter,
        String(data="[partial] 보비 준비"),
    )

    assert finals == []
    assert rejected == []
    assert len(partials) == 1
    assert partials[0].text == "보비 준비"
    assert partials[0].is_final is False
    assert adapter._last_detail == "accepted_partial_tagged_sentence"


def test_tagged_final_is_admitted_without_the_marker() -> None:
    adapter, finals, partials, rejected = _tagged_adapter_harness()

    SpeechInputAdapterNode._on_sentence(
        adapter,
        String(data="[final] 보비 주세요"),
    )

    assert partials == []
    assert rejected == []
    assert len(finals) == 1
    assert finals[0].text == "보비 주세요"
    assert finals[0].is_final is True
    assert adapter._last_detail == "accepted_final_tagged_sentence"


def test_untagged_sentence_is_rejected_in_tagged_mode() -> None:
    adapter, finals, partials, rejected = _tagged_adapter_harness()

    SpeechInputAdapterNode._on_sentence(adapter, String(data="보비 주세요"))

    assert finals == []
    assert partials == []
    assert rejected == ["missing_transcript_tag"]


def test_adapter_mode_status_identifies_external_and_local_ingress() -> None:
    external = SimpleNamespace(
        _input_mode="tagged_sentence",
        _sentence_source_id="external_sentence_topic",
    )
    local = SimpleNamespace(
        _input_mode="utterance",
        _sentence_source_id="external_sentence_topic",
    )

    assert SpeechInputAdapterNode._mode_source_id(external) == (
        "external_sentence_topic"
    )
    assert SpeechInputAdapterNode._mode_source_id(local) == "local_microphone"


def test_adapter_input_mode_can_switch_without_recreating_ingress() -> None:
    published_statuses: list[object] = []
    adapter = SimpleNamespace(
        _input_mode="tagged_sentence",
        _recent_ids=RecentUtteranceIds(retention_sec=120.0),
        _recent_sentences=RecentSentences(retention_sec=1.0),
        _last_source="external_sentence_topic",
        _last_observation_stamp=Time(sec=42),
        _last_accepted_monotonic=123.0,
        _last_detail="accepted_final_tagged_sentence",
        _publish_status=lambda: published_statuses.append(True),
        _waiting_detail=lambda: "waiting_for_final_surgeon_utterance",
    )

    result = SpeechInputAdapterNode._on_parameters_changed(
        adapter,
        [SimpleNamespace(name="input_mode", value="utterance")],
    )

    assert result.successful is True
    assert adapter._input_mode == "utterance"
    assert adapter._last_source == ""
    assert adapter._last_observation_stamp is None
    assert adapter._last_accepted_monotonic == 0.0
    assert adapter._last_detail == "waiting_for_final_surgeon_utterance"
    assert published_statuses == [True]

    rejected = SpeechInputAdapterNode._on_parameters_changed(
        adapter,
        [SimpleNamespace(name="input_mode", value="unknown")],
    )
    assert rejected.successful is False
    assert adapter._input_mode == "utterance"


def test_sentence_text_deduplication_is_short_and_case_insensitive() -> None:
    recent = RecentSentences(retention_sec=1.0)
    assert recent.accept("Bovie please", 100.0) is True
    assert recent.accept("  bovie   PLEASE ", 100.2) is False
    assert recent.accept("Bovie please", 101.1) is True


def test_tts_echo_normalization_keeps_only_korean_and_alphanumerics() -> None:
    assert normalize_tts_echo_text("  바이폴라, 전달! ABC-12 ") == (
        "바이폴라전달abc12"
    )


def test_playing_tts_exact_and_near_asr_echo_are_suppressed() -> None:
    guard = TTSEchoGuard(similarity_threshold=0.88)
    guard.observe_playback(
        reply_id="reply-1",
        text="바이폴라 전달드리겠습니다",
        state="playing",
        now_monotonic=100.0,
    )

    assert guard.matching_reply_id(
        "바이폴라 전달드리겠습니다",
        now_monotonic=100.1,
    ) == "reply-1"
    assert guard.matching_reply_id(
        "바이폴라를 전달 드리겠습니다",
        now_monotonic=100.2,
    ) == "reply-1"


@pytest.mark.parametrize(
    "terminal_state",
    ["played", "failed", "interrupted_unknown"],
)
def test_tts_echo_is_suppressed_during_terminal_tail_then_expires(
    terminal_state: str,
) -> None:
    guard = TTSEchoGuard(tail_sec=0.8)
    guard.observe_playback(
        reply_id="reply-2",
        text="도구 회수중입니다",
        state="playing",
        now_monotonic=200.0,
    )
    guard.observe_playback(
        reply_id="reply-2",
        text="",
        state=terminal_state,
        now_monotonic=201.0,
    )

    assert guard.matching_reply_id(
        "도구 회수 중입니다",
        now_monotonic=201.7,
    ) == "reply-2"
    assert guard.matching_reply_id(
        "도구 회수 중입니다",
        now_monotonic=201.81,
    ) is None


def test_retained_playing_status_restores_only_remaining_audio_window() -> None:
    guard = TTSEchoGuard(tail_sec=0.8, max_playing_sec=30.0)
    guard.observe_playback(
        reply_id="reply-restart",
        text="바이폴라 전달드리겠습니다",
        state="playing",
        now_monotonic=500.0,
        event_age_sec=1.25,
        audio_duration_sec=3.0,
    )

    assert guard.matching_reply_id(
        "바이폴라 전달드리겠습니다",
        now_monotonic=502.54,
    ) == "reply-restart"
    assert guard.matching_reply_id(
        "바이폴라 전달드리겠습니다",
        now_monotonic=502.56,
    ) is None


def test_old_retained_playing_and_terminal_history_never_reopens_echo_gate() -> None:
    guard = TTSEchoGuard(tail_sec=0.8, max_playing_sec=30.0)
    guard.observe_playback(
        reply_id="reply-old",
        text="도구 회수중입니다",
        state="playing",
        now_monotonic=600.0,
        event_age_sec=45.0,
        audio_duration_sec=2.0,
    )
    guard.observe_playback(
        reply_id="reply-old",
        text="도구 회수중입니다",
        state="played",
        now_monotonic=600.0,
        event_age_sec=43.0,
        audio_duration_sec=2.0,
    )

    assert guard.matching_reply_id(
        "도구 회수중입니다",
        now_monotonic=600.0,
    ) is None


def test_non_audible_terminal_failure_does_not_open_echo_gate() -> None:
    guard = TTSEchoGuard(tail_sec=0.8)
    guard.observe_playback(
        reply_id="reply-synth-failed",
        text="도구 회수중입니다",
        state="failed",
        now_monotonic=650.0,
        error_code="synthesis_failed",
    )

    assert guard.matching_reply_id(
        "도구 회수중입니다",
        now_monotonic=650.1,
    ) is None


def test_zero_duration_playing_status_still_has_a_bounded_ttl() -> None:
    guard = TTSEchoGuard(tail_sec=0.8, max_playing_sec=5.0)
    guard.observe_playback(
        reply_id="reply-bounded",
        text="도구 회수중입니다",
        state="playing",
        now_monotonic=700.0,
    )

    assert guard.matching_reply_id(
        "도구 회수중입니다",
        now_monotonic=705.79,
    ) == "reply-bounded"
    assert guard.matching_reply_id(
        "도구 회수중입니다",
        now_monotonic=705.81,
    ) is None


def test_tts_echo_guard_allows_unrelated_and_short_surgeon_speech() -> None:
    guard = TTSEchoGuard(similarity_threshold=0.88)
    guard.observe_playback(
        reply_id="reply-3",
        text="바이폴라 전달드리겠습니다",
        state="playing",
        now_monotonic=300.0,
    )

    assert guard.matching_reply_id(
        "흡인기 준비됐나요",
        now_monotonic=300.1,
    ) is None
    assert guard.matching_reply_id("네", now_monotonic=300.1) is None


def test_typed_echo_consumes_utterance_id_before_suppression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard = TTSEchoGuard()
    guard.observe_playback(
        reply_id="reply-loop",
        text="도구 회수중입니다",
        state="playing",
        now_monotonic=400.0,
    )
    rejected: list[str] = []
    published: list[SpeechUtterance] = []
    adapter = SimpleNamespace(
        _received_count=0,
        _last_source="",
        _last_observation_stamp=None,
        _require_utterance_id=True,
        _required_speaker_role="surgeon",
        _min_confidence=0.55,
        _accept_missing_confidence=True,
        _require_timestamp=True,
        _max_age_sec=3.0,
        _max_future_skew_sec=1.0,
        _require_source=True,
        _recent_ids=RecentUtteranceIds(retention_sec=120.0),
        _enable_tts_echo_guard=True,
        _tts_echo_guard=guard,
        _output_mode="typed_utterance",
        _transcript_pub=SimpleNamespace(publish=published.append),
        _now_sec=lambda: 101.0,
        _reject=rejected.append,
        _tts_echo_detail=SpeechInputAdapterNode._tts_echo_detail,
    )
    echo = _utterance(
        utterance_id="echo-1",
        text="도구 회수중입니다",
    )

    monkeypatch.setattr(
        "simulation_runtime.speech_input_adapter.time.monotonic",
        lambda: 400.1,
    )
    SpeechInputAdapterNode._on_utterance(adapter, echo)
    SpeechInputAdapterNode._on_utterance(adapter, echo)

    assert published == []
    assert rejected == [
        "tts_echo_suppressed:reply-loop",
        "duplicate_utterance_id",
    ]


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
        "retraction": {
            "request_id": "",
            "operation": "retraction",
            "speech": "왼쪽으로 조금 당겨줘",
        }
    }
    request = BedRobotArmGroupRequest()
    request.request_id = "voice-123"
    request.group_id = "retraction"
    request.operation = "retraction"
    request.source = "deterministic_voice_router"

    actor._on_bed_robot_arm_group_request(request)

    assert actor._pending_group_requests["retraction"]["request_id"] == "voice-123"
