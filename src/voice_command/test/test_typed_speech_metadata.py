from __future__ import annotations

import itertools
from pathlib import Path
from types import SimpleNamespace

import pytest

from builtin_interfaces.msg import Time
from surgical_msgs.msg import SpeechUtterance

from voice_command.contracts import DISPOSITION_PROPOSE, VoiceIntentProposal
from voice_command.node import VoiceIntentResolverNode


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


def test_typed_resolver_copies_asr_metadata_into_direct_voice_intent() -> None:
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
    assert output.gateway_instance_id == ""
    assert output.procedure_run_id == ""
    assert output.function_request_id == ""
    assert not hasattr(output, "command_id")


def test_adjust_proposal_stays_on_the_direct_typed_lane() -> None:
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
            intent="retractor_command",
            tool_id="",
            retractor_command="adjust_retraction",
            target_side="right",
            distance_m=0.005,
            urgency="routine",
            provenance="exact_adjustment",
            requires_confirmation=False,
            disposition="propose",
            reason="exact_adjustment",
            evidence_spans=(),
        )
    )
    source = _utterance(text="오른쪽 5 mm 더 당겨줘")

    node._publish_resolved(
        source.text,
        source=source,
        source_stamp=source.stamp,
    )

    output = published[0]
    assert output.function_request_id == ""
    assert not hasattr(output, "command_id")
    assert output.intent == "retractor_command"
    assert output.retractor_command == "adjust_retraction"


def test_no_command_proposal_uses_the_same_typed_lane() -> None:
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
            intent="",
            tool_id="",
            retractor_command="",
            target_side="none",
            distance_m=0.0,
            urgency="",
            provenance="no_match",
            requires_confirmation=False,
            disposition="no_command",
            reason="no_match",
            evidence_spans=(),
        )
    )

    node._publish_resolved(
        "오늘 날씨가 어때",
        source=_utterance(text="오늘 날씨가 어때"),
        source_stamp=Time(sec=100),
    )

    output = published[0]
    assert not hasattr(output, "command_id")
    assert output.disposition == "no_command"
    assert output.function_request_id == ""


def test_node_projects_direct_semantic_proposal_to_single_typed_intent() -> None:
    published = []
    node = VoiceIntentResolverNode.__new__(VoiceIntentResolverNode)
    node._publish_no_command = True
    node._utterance_counter = itertools.count(1)
    node._publisher = SimpleNamespace(publish=published.append)
    node.get_clock = lambda: SimpleNamespace(
        now=lambda: SimpleNamespace(to_msg=lambda: Time(sec=101))
    )
    proposal = VoiceIntentProposal(
        raw_text="수술장 조명 설정",
        normalized_text="수술장 조명 설정",
        procedure_id="thyroidectomy_demo",
        catalog_id="sha256:test",
        intent="lighting_command",
        disposition=DISPOSITION_PROPOSE,
        reason="reviewed_generic_light_command",
    )
    node._resolver = SimpleNamespace(
        resolve=lambda _text: proposal
    )

    node._publish_resolved(
        proposal.raw_text,
        source=_utterance(text=proposal.raw_text),
        source_stamp=Time(sec=100),
    )

    assert published[0].intent == "lighting_command"
    assert published[0].tool_id == ""
    assert published[0].function_request_id == ""


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
