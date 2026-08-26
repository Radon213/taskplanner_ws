from __future__ import annotations

import itertools
from pathlib import Path
from types import SimpleNamespace

import pytest

from builtin_interfaces.msg import Time
from surgical_msgs.msg import SpeechUtterance

from voice_command.node import (
    RecentUtteranceIds,
    VoiceIntentResolverNode,
    evaluate_typed_speech_utterance,
)


def _utterance(**overrides) -> SpeechUtterance:
    message = SpeechUtterance()
    message.stamp = Time(sec=100)
    message.end_stamp = Time(sec=100)
    message.utterance_id = "asr-100-1"
    message.text = "보비 주세요"
    message.is_final = True
    message.speaker_role = "surgeon"
    message.source = "taskplanner_asr:cloud"
    for key, value in overrides.items():
        setattr(message, key, value)
    return message


def _admit(message: SpeechUtterance):
    return evaluate_typed_speech_utterance(
        message,
        now_sec=101.0,
        max_age_sec=3.0,
        max_future_skew_sec=1.0,
    )


def test_live_typed_speech_requires_final_source_timestamp_and_id() -> None:
    accepted = _admit(_utterance())

    assert accepted.accepted is True
    assert accepted.stamp.sec == 100


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"is_final": False}, "interim_transcript"),
        ({"utterance_id": ""}, "missing_utterance_id"),
        ({"source": ""}, "missing_source"),
        ({"stamp": Time(), "end_stamp": Time()}, "missing_timestamp"),
        ({"stamp": Time(sec=97), "end_stamp": Time(sec=97)}, "stale"),
        ({"stamp": Time(sec=103), "end_stamp": Time(sec=103)}, "future_timestamp"),
    ],
)
def test_live_typed_speech_fails_closed_for_bad_metadata(
    overrides: dict,
    reason: str,
) -> None:
    result = _admit(_utterance(**overrides))

    assert result.accepted is False
    assert result.reason.startswith(reason)


def test_live_typed_speech_replay_id_is_suppressed() -> None:
    recent = RecentUtteranceIds(retention_sec=10.0)

    assert recent.accept("asr-100-1", 100.0) is True
    assert recent.accept("asr-100-1", 101.0) is False
    assert recent.accept("asr-100-1", 111.0) is True


def test_node_catalog_threads_procedure_retractor_vocabulary() -> None:
    bundle = (
        Path(__file__).resolve().parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
        / "thyroidectomy_demo"
    )

    (
        procedure_id,
        _,
        _,
        _,
        retractor_commands,
        max_distance_m,
        require_explicit_unit,
    ) = VoiceIntentResolverNode._catalog_for_bundle(str(bundle))

    assert procedure_id == "thyroidectomy_demo"
    assert retractor_commands == (
        "start_direct_teach",
        "finish_direct_teach",
        "start_retraction",
        "adjust_retraction",
        "change_tool",
        "stop_retraction",
    )
    assert max_distance_m == pytest.approx(0.03)
    assert require_explicit_unit is True


def test_typed_resolver_copies_asr_metadata_into_voice_intent() -> None:
    published = []
    node = VoiceIntentResolverNode.__new__(VoiceIntentResolverNode)
    node._publish_no_command = True
    node._utterance_counter = itertools.count(1)
    node._publisher = SimpleNamespace(publish=published.append)
    node.get_clock = lambda: SimpleNamespace(
        now=lambda: SimpleNamespace(to_msg=lambda: Time(sec=101))
    )
    node._resolver = SimpleNamespace(
        resolve=lambda text: SimpleNamespace(
            raw_text=text,
            normalized_text=text,
            procedure_id="thyroidectomy_demo",
            catalog_id="sha256:test",
            intent="tool_handover",
            tool_id="T04",
            retractor_command="",
            target_side="none",
            distance_m=0.0,
            urgency="routine",
            provenance="exact_tool_and_handover_verb",
            requires_confirmation=False,
            disposition="propose",
            reason="exact_tool_and_handover_verb",
            evidence_spans=(),
        )
    )
    source = _utterance()

    node._publish_resolved(
        source.text,
        source=source,
        source_stamp=source.stamp,
    )

    assert len(published) == 1
    output = published[0]
    assert output.header.stamp == source.stamp
    assert output.utterance_id == source.utterance_id
    assert output.source == source.source
    assert output.source_is_final is True
    assert output.source_speaker_role == "surgeon"


def test_legacy_string_resolver_output_cannot_satisfy_live_metadata_gate() -> None:
    """A Debug/replay String must not acquire executable ASR authority."""

    published = []
    node = VoiceIntentResolverNode.__new__(VoiceIntentResolverNode)
    node._publish_no_command = True
    node._utterance_counter = itertools.count(1)
    node._publisher = SimpleNamespace(publish=published.append)
    node.get_clock = lambda: SimpleNamespace(
        now=lambda: SimpleNamespace(to_msg=lambda: Time(sec=101))
    )
    node._resolver = SimpleNamespace(
        resolve=lambda text: SimpleNamespace(
            raw_text=text,
            normalized_text=text,
            procedure_id="thyroidectomy_demo",
            catalog_id="sha256:test",
            intent="tool_handover",
            tool_id="T04",
            retractor_command="",
            target_side="none",
            distance_m=0.0,
            urgency="routine",
            provenance="exact_tool_and_handover_verb",
            requires_confirmation=False,
            disposition="propose",
            reason="exact_tool_and_handover_verb",
            evidence_spans=(),
        )
    )

    node._publish_resolved(
        "보비 주세요",
        source=None,
        source_stamp=None,
    )

    output = published[0]
    assert output.source == "legacy_sentence_text_compatibility"
    assert output.source_is_final is False
