from __future__ import annotations

from pathlib import Path

import pytest

from procedure_spec import load_voice_command_catalog, voice_catalog_id_for
from voice_command.contracts import (
    DISPOSITION_CLARIFY,
    DISPOSITION_NO_COMMAND,
    DISPOSITION_PROPOSE,
    DISPOSITION_REJECT,
    INTENT_RETRACTOR_COMMAND,
    INTENT_PROCEDURE_START,
    INTENT_PROCEDURE_STOP,
    INTENT_TOOL_HANDOVER,
    INTENT_TOOL_RETRIEVE,
    TARGET_SIDE_NONE,
)
from voice_command.resolver import VoiceIntentResolver
from voice_command.selector import CandidateSelection


_ALIASES = {
    "T02": ("t02", "adson forceps", "adson", "애드슨"),
    "T04": ("보비", "bovie"),
    "T05": ("아미 네이비", "아미"),
}
_PROCEDURE_ID = "thyroidectomy"
_CATALOG_ID = "sha256:test-catalog"


@pytest.fixture
def resolver() -> VoiceIntentResolver:
    return VoiceIntentResolver(
        tool_aliases=_ALIASES,
        procedure_id=_PROCEDURE_ID,
        catalog_id=_CATALOG_ID,
    )


@pytest.mark.parametrize(
    "utterance, urgency",
    [
        ("보비 줘", "routine"),
        ("보비를 주세요", "routine"),
        ("보비 전달", "routine"),
        ("보비 내놔", "routine"),
        ("보비 내놔 빨리", "urgent"),
        ("보비 서둘러", "urgent"),
    ],
)
def test_natural_explicit_tool_handover_is_grounded(
    resolver: VoiceIntentResolver,
    utterance: str,
    urgency: str,
) -> None:
    proposal = resolver.resolve(utterance)

    assert proposal.disposition == DISPOSITION_PROPOSE
    assert proposal.intent == INTENT_TOOL_HANDOVER
    assert proposal.tool_id == "T04"
    assert proposal.retractor_command == ""
    assert proposal.urgency == urgency
    assert proposal.requires_confirmation is False
    # Urgency is audit language, not a physical movement instruction.
    assert proposal.target_side == TARGET_SIDE_NONE
    assert proposal.distance_m == 0.0
    assert proposal.procedure_id == _PROCEDURE_ID
    assert proposal.catalog_id == _CATALOG_ID


@pytest.mark.parametrize(
    ("utterance", "tool_id"),
    [
        ("애드슨 줘", "T02"),
        ("보비 줘", "T04"),
        ("아미 줘", "T05"),
    ],
)
def test_catalog_tool_name_with_give_cue_is_an_explicit_handover(
    resolver: VoiceIntentResolver,
    utterance: str,
    tool_id: str,
) -> None:
    proposal = resolver.resolve(utterance)

    assert proposal.disposition == DISPOSITION_PROPOSE
    assert proposal.intent == INTENT_TOOL_HANDOVER
    assert proposal.tool_id == tool_id
    assert proposal.requires_confirmation is False


@pytest.mark.parametrize(
    "utterance",
    [
        "보비 회수",
        "보비 회수해",
        "보비 회수해줘",
        "보비 치워줘",
        "보비 정리해줘",
        "Adson forceps 회수 해 줘",
    ],
)
def test_explicit_tool_retrieval_is_grounded_without_spacing_sensitivity(
    resolver: VoiceIntentResolver,
    utterance: str,
) -> None:
    proposal = resolver.resolve(utterance)

    assert proposal.disposition == DISPOSITION_PROPOSE
    assert proposal.intent == INTENT_TOOL_RETRIEVE
    assert proposal.tool_id in {"T02", "T04"}
    assert proposal.requires_confirmation is False


@pytest.mark.parametrize(
    ("utterance", "tool_id"),
    [
        ("보비", "T04"),
        ("Bovie", "T04"),
        ("애드슨", "T02"),
        ("Adson forceps", "T02"),
    ],
)
def test_bare_catalog_tool_name_is_an_immediate_handover_request(
    resolver: VoiceIntentResolver,
    utterance: str,
    tool_id: str,
) -> None:
    proposal = resolver.resolve(utterance)

    assert proposal.disposition == DISPOSITION_PROPOSE
    assert proposal.intent == INTENT_TOOL_HANDOVER
    assert proposal.tool_id == tool_id
    assert proposal.requires_confirmation is False


@pytest.mark.parametrize(
    "utterance, tool_id",
    [
        ("보비 부탁합니다", "T04"),
        ("보비 부탁드립니다", "T04"),
        ("Bovie handover", "T04"),
        ("Bovie hand over", "T04"),
        ("Adson forceps 부탁합니다", "T02"),
        ("애드슨 부탁드립니다", "T02"),
        ("Adson handover", "T02"),
        ("T02 hand over", "T02"),
    ],
)
def test_explicit_formal_or_english_handover_is_grounded(
    resolver: VoiceIntentResolver,
    utterance: str,
    tool_id: str,
) -> None:
    proposal = resolver.resolve(utterance)

    assert proposal.disposition == DISPOSITION_PROPOSE
    assert proposal.intent == INTENT_TOOL_HANDOVER
    assert proposal.tool_id == tool_id
    assert proposal.requires_confirmation is False


@pytest.mark.parametrize(
    "utterance",
    [
        "보비 부탁합니다라고 말했어요",
        "회의에서 보비 부탁합니다라는 표현을 쓰세요",
        "we call this Bovie handover training",
        "Bovie handover is disabled",
        "is this a Bovie handover",
    ],
)
def test_formal_handover_words_in_background_speech_do_not_propose(
    resolver: VoiceIntentResolver,
    utterance: str,
) -> None:
    proposal = resolver.resolve(utterance)

    assert not proposal.is_executable_proposal


@pytest.mark.parametrize(
    "utterance",
    [
        "교시 시작",
        "직접교시 시작",
        "자 이제 교시를 시작해보자",
    ],
)
def test_direct_teach_requires_procedure_retraction_vocabulary(
    resolver: VoiceIntentResolver,
    utterance: str,
) -> None:
    proposal = resolver.resolve(utterance)

    assert not proposal.is_executable_proposal
    assert proposal.retractor_command == ""
    assert proposal.tool_id == ""
    assert proposal.target_side == TARGET_SIDE_NONE
    assert proposal.distance_m == 0.0
    assert proposal.requires_confirmation is False


@pytest.mark.parametrize(
    "utterance, intent",
    [
        ("갑상선 절제술 시작", INTENT_PROCEDURE_START),
        ("갑상선 수술 시작", INTENT_PROCEDURE_START),
        ("thyroidectomy 시작", INTENT_PROCEDURE_START),
        ("thyroidectomy 스타트", INTENT_PROCEDURE_START),
        ("갑상선절제술 시작하겠습니다", INTENT_PROCEDURE_START),
        ("thyroidectomy 시작하겠습니다", INTENT_PROCEDURE_START),
        ("갑상선 절제술 종료", INTENT_PROCEDURE_STOP),
        ("갑상선 수술 종료", INTENT_PROCEDURE_STOP),
        ("thyroidectomy 종료", INTENT_PROCEDURE_STOP),
        ("thyroidectomy 스탑", INTENT_PROCEDURE_STOP),
        ("수술 종료", INTENT_PROCEDURE_STOP),
        ("갑상선절제술 종료하겠습니다", INTENT_PROCEDURE_STOP),
        ("갑상선절제술 마무리하겠습니다", INTENT_PROCEDURE_STOP),
        ("갑상선절제술 끝내겠습니다", INTENT_PROCEDURE_STOP),
    ],
)
def test_explicit_active_procedure_lifecycle_is_grounded(
    resolver: VoiceIntentResolver,
    utterance: str,
    intent: str,
) -> None:
    proposal = resolver.resolve(utterance)

    assert proposal.disposition == DISPOSITION_PROPOSE
    assert proposal.intent == intent
    assert proposal.procedure_id == _PROCEDURE_ID
    assert proposal.requires_confirmation is False


@pytest.mark.parametrize(
    "utterance",
    [
        "갑상선절제술 시작할까요?",
        "갑상선절제술 시작하지 마",
        "오늘 갑상선절제술 시작하겠습니다 라고 말했어",
        '"갑상선절제술 시작하겠습니다"라고 읽어 줘',
        "갑상선절제술 종료하겠다고 말했어",
        "should we start thyroidectomy?",
        "do not start thyroidectomy",
        "시작하겠습니다",
        "수술 시작",
    ],
)
def test_procedure_lifecycle_requires_a_short_nonnegated_command(
    resolver: VoiceIntentResolver,
    utterance: str,
) -> None:
    proposal = resolver.resolve(utterance)

    assert proposal.disposition != DISPOSITION_PROPOSE


@pytest.mark.parametrize("utterance", ["직접 교실 시작", "교시시 시작"])
def test_asr_repair_requires_procedure_retraction_vocabulary(
    resolver: VoiceIntentResolver,
    utterance: str,
) -> None:
    proposal = resolver.resolve(utterance)

    assert proposal.raw_text == utterance
    assert not proposal.is_executable_proposal


@pytest.mark.parametrize("utterance", ["도구 줘", "기구 내놔"])
def test_generic_tool_request_clarifies_missing_tool(
    resolver: VoiceIntentResolver,
    utterance: str,
) -> None:
    proposal = resolver.resolve(utterance)

    assert proposal.disposition == DISPOSITION_CLARIFY
    assert proposal.intent == INTENT_TOOL_HANDOVER
    assert proposal.tool_id == ""
    assert proposal.reason == "missing_tool_id"


@pytest.mark.parametrize(
    "utterance",
    [
        "교시 시작할까?",
        "교시 시작하지 마",
        "보비 안 줘",
        "보비 안줘",
        "보비 주지 마",
        "보비 finished",
        "보비 줘 그리고 교시 시작",
    ],
)
def test_question_negation_non_handover_and_compound_speech_do_not_propose(
    resolver: VoiceIntentResolver,
    utterance: str,
) -> None:
    proposal = resolver.resolve(utterance)

    assert proposal.disposition == DISPOSITION_REJECT
    assert not proposal.is_executable_proposal


def test_long_conversational_tail_is_not_a_direct_teach_command(
    resolver: VoiceIntentResolver,
) -> None:
    utterance = (
        "결론에 적어 주세요 네 한 번만 하나 한 번만 더 해 볼게요 "
        "그러면 직접 교시시 시작"
    )
    proposal = resolver.resolve(utterance)

    assert proposal.disposition == DISPOSITION_REJECT
    assert proposal.reason == "direct_teach_not_a_standalone_command"


def test_named_tool_without_request_anchor_is_background_not_handover(
    resolver: VoiceIntentResolver,
) -> None:
    proposal = resolver.resolve("보비는 준비되어 있어")

    assert proposal.disposition == DISPOSITION_NO_COMMAND
    assert proposal.reason == "tool_named_without_handover_anchor"


class _UnavailableSelector:
    def select(self, **_: object) -> CandidateSelection:
        return CandidateSelection(
            candidate_id=None,
            provenance="test_selector",
            reason="test_selector_unavailable",
            unavailable=True,
        )


class _FirstCandidateSelector:
    def select(self, **kwargs: object) -> CandidateSelection:
        candidates = kwargs["candidates"]
        return CandidateSelection(
            candidate_id=candidates[0].candidate_id,
            provenance="test_selector",
        )


class _MustNotSelect:
    def select(self, **_: object) -> CandidateSelection:
        raise AssertionError("strong deterministic candidates must bypass VLM")


def test_strong_short_command_bypasses_selector_latency() -> None:
    resolver = VoiceIntentResolver(
        tool_aliases=_ALIASES,
        procedure_id=_PROCEDURE_ID,
        catalog_id=_CATALOG_ID,
        selector=_MustNotSelect(),
    )

    proposal = resolver.resolve("보비 내놔 빨리")

    assert proposal.disposition == DISPOSITION_PROPOSE
    assert proposal.tool_id == "T04"
    assert proposal.provenance.endswith("|deterministic_strong_anchor")


def test_selector_only_natural_variant_never_falls_back_on_model_unavailability() -> None:
    resolver = VoiceIntentResolver(
        tool_aliases=_ALIASES,
        procedure_id=_PROCEDURE_ID,
        catalog_id=_CATALOG_ID,
        selector=_UnavailableSelector(),
        allow_selector_natural_variants=True,
    )

    proposal = resolver.resolve("보비 좀 부탁해")

    assert proposal.disposition == DISPOSITION_REJECT
    assert proposal.reason == "test_selector_unavailable"


@pytest.mark.parametrize("utterance", ["보비 좀 부탁해", "교시를 해보자"])
def test_selector_only_natural_variant_never_autoexecutes(
    utterance: str,
) -> None:
    resolver = VoiceIntentResolver(
        tool_aliases=_ALIASES,
        procedure_id=_PROCEDURE_ID,
        catalog_id=_CATALOG_ID,
        selector=_FirstCandidateSelector(),
        allow_selector_natural_variants=True,
    )

    proposal = resolver.resolve(utterance)

    if utterance == "보비 좀 부탁해":
        assert proposal.disposition == DISPOSITION_PROPOSE
        assert proposal.tool_id == "T04"
        assert proposal.requires_confirmation is True
    else:
        assert not proposal.is_executable_proposal


def test_selector_only_natural_variant_is_disabled_by_default(
    resolver: VoiceIntentResolver,
) -> None:
    assert resolver.resolve("보비 좀 부탁해").disposition == DISPOSITION_NO_COMMAND
    assert resolver.resolve("교시를 해보자").disposition == DISPOSITION_NO_COMMAND


def test_missing_procedure_or_catalog_binding_fails_closed() -> None:
    no_procedure = VoiceIntentResolver(tool_aliases=_ALIASES)
    no_catalog = VoiceIntentResolver(
        procedure_id=_PROCEDURE_ID,
        catalog_id="",
        tool_aliases={},
    )

    assert no_procedure.resolve("보비 줘").disposition == DISPOSITION_NO_COMMAND
    catalog_proposal = no_catalog.resolve("도구 줘")
    assert catalog_proposal.disposition == DISPOSITION_CLARIFY
    assert catalog_proposal.reason == "tool_catalog_unavailable"
    assert no_catalog.resolve("보비 줘").disposition == DISPOSITION_NO_COMMAND


def test_catalog_loader_scopes_aliases_to_active_procedure() -> None:
    specs = (
        Path(__file__).resolve().parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
    )
    thyroid = load_voice_command_catalog(specs / "thyroidectomy")
    nephrectomy = load_voice_command_catalog(specs / "nephrectomy")
    thyroid_resolver = VoiceIntentResolver(
        tool_aliases=thyroid.tool_aliases,
        procedure_id=thyroid.procedure_id,
        catalog_id=thyroid.catalog_id,
    )
    nephrectomy_resolver = VoiceIntentResolver(
        tool_aliases=nephrectomy.tool_aliases,
        procedure_id=nephrectomy.procedure_id,
        catalog_id=nephrectomy.catalog_id,
    )

    thyroid_proposal = thyroid_resolver.resolve("보비 줘")
    assert thyroid_proposal.disposition == DISPOSITION_PROPOSE
    assert thyroid_proposal.tool_id == "T04"
    assert thyroid_proposal.catalog_id == thyroid.catalog_id
    # In nephrectomy, T04 means Richardson rather than a globally assumed Bovie.
    assert nephrectomy_resolver.resolve("보비 줘").disposition == DISPOSITION_NO_COMMAND
    nephrectomy_proposal = nephrectomy_resolver.resolve("리처드슨 줘")
    assert nephrectomy_proposal.disposition == DISPOSITION_PROPOSE
    assert nephrectomy_proposal.tool_id == "T04"
    assert nephrectomy_proposal.catalog_id == nephrectomy.catalog_id
    assert thyroid.catalog_id != nephrectomy.catalog_id


@pytest.mark.parametrize(
    "utterance",
    ["아미 네이비 줘", "Army navy retractor please", "갑상선 리트랙터 주세요"],
)
def test_demo_catalog_does_not_turn_bed_arm_retractors_into_handover_intents(
    utterance: str,
) -> None:
    specs = (
        Path(__file__).resolve().parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
    )
    demo = load_voice_command_catalog(specs / "thyroidectomy_demo")
    resolver = VoiceIntentResolver(
        tool_aliases=demo.tool_aliases,
        procedure_id=demo.procedure_id,
        catalog_id=demo.catalog_id,
        retractor_commands=demo.retractor_commands,
        retractor_max_distance_m=demo.retractor_max_distance_m,
        retractor_require_explicit_unit=demo.retractor_require_explicit_unit,
    )

    proposal = resolver.resolve(utterance)

    assert "T05" not in demo.tool_aliases
    assert "T11" not in demo.tool_aliases
    assert proposal.disposition == DISPOSITION_NO_COMMAND
    assert proposal.intent != INTENT_TOOL_HANDOVER
    assert proposal.tool_id == ""


@pytest.mark.parametrize(
    "utterance, command, target_side, distance_m",
    [
        ("직접 교시 시작", "start_direct_teach", "none", 0.0),
        ("직접 교시 종료", "finish_direct_teach", "none", 0.0),
        ("리트랙션 시작", "start_retraction", "none", 0.0),
        ("retration 시작", "start_retraction", "none", 0.0),
        (
            "오른쪽 리트랙션을 1 센치 더 당겨줘",
            "adjust_retraction",
            "right",
            0.01,
        ),
        (
            "오른쪽으로 1cm 더 당겨줘",
            "adjust_retraction",
            "right",
            0.01,
        ),
        (
            "왼쪽으로 1cm 더 당겨줘",
            "adjust_retraction",
            "left",
            0.01,
        ),
        (
            "오른쪽으로 1cm만 더 당겨줘",
            "adjust_retraction",
            "right",
            0.01,
        ),
        (
            "왼쪽 1 센치 만큼 더 당겨주세요",
            "adjust_retraction",
            "left",
            0.01,
        ),
        (
            "오른쪽으로 1cm만 더 당겨줄래",
            "adjust_retraction",
            "right",
            0.01,
        ),
        (
            "1cm 정도 오른쪽을 더 땡겨줘",
            "adjust_retraction",
            "right",
            0.01,
        ),
        (
            "좌측 팔을 5mm만 살짝 땡겨줄래",
            "adjust_retraction",
            "left",
            0.005,
        ),
        (
            "오른쪽 1cm 덜 당겨줘",
            "adjust_retraction",
            "right",
            -0.01,
        ),
        (
            "1cm만 덜 당겨",
            "adjust_retraction",
            "left",
            -0.01,
        ),
        (
            "왼쪽 5mm만 조금 덜 땡겨줘",
            "adjust_retraction",
            "left",
            -0.005,
        ),
        ("덜 당겨줘", "adjust_retraction", "both", -0.005),
        ("당겨줄래", "adjust_retraction", "both", 0.005),
        ("땡겨줘", "adjust_retraction", "both", 0.005),
        ("도구 교체", "change_tool", "none", 0.0),
        ("리트랙션 종료", "stop_retraction", "none", 0.0),
        ("retration 종료", "stop_retraction", "none", 0.0),
    ],
)
def test_demo_catalog_resolves_all_six_typed_retractor_commands(
    utterance: str,
    command: str,
    target_side: str,
    distance_m: float,
) -> None:
    specs = (
        Path(__file__).resolve().parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
    )
    demo = load_voice_command_catalog(specs / "thyroidectomy_demo")
    resolver = VoiceIntentResolver(
        tool_aliases=demo.tool_aliases,
        procedure_id=demo.procedure_id,
        catalog_id=demo.catalog_id,
        retractor_commands=demo.retractor_commands,
        retractor_max_distance_m=demo.retractor_max_distance_m,
        retractor_require_explicit_unit=demo.retractor_require_explicit_unit,
    )

    proposal = resolver.resolve(utterance)

    assert demo.retractor_commands == (
        "start_direct_teach",
        "finish_direct_teach",
        "start_retraction",
        "adjust_retraction",
        "change_tool",
        "stop_retraction",
    )
    assert proposal.disposition == DISPOSITION_PROPOSE
    assert proposal.intent == INTENT_RETRACTOR_COMMAND
    assert proposal.retractor_command == command
    assert proposal.target_side == target_side
    assert proposal.distance_m == pytest.approx(distance_m)
    assert proposal.tool_id == ""


@pytest.mark.parametrize(
    "utterance, expected_distance_m, expected_side",
    [
        ("ㄹ머ㅏㅇ럼ㄻㄴㅇ. 1cm 더 당겨줘", 0.01, "left"),
        ("앞 문장은 질문이었나요? 1cm만 덜 당겨줘", -0.01, "left"),
        ("앞 문장입니다! 오른쪽 5mm 더 땡겨줘", 0.005, "right"),
    ],
)
def test_demo_catalog_resolves_a_bounded_final_adjustment_clause(
    utterance: str,
    expected_distance_m: float,
    expected_side: str,
) -> None:
    specs = (
        Path(__file__).resolve().parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
    )
    demo = load_voice_command_catalog(specs / "thyroidectomy_demo")
    resolver = VoiceIntentResolver(
        tool_aliases=demo.tool_aliases,
        procedure_id=demo.procedure_id,
        catalog_id=demo.catalog_id,
        retractor_commands=demo.retractor_commands,
        retractor_max_distance_m=demo.retractor_max_distance_m,
        retractor_require_explicit_unit=demo.retractor_require_explicit_unit,
    )

    proposal = resolver.resolve(utterance)

    assert proposal.disposition == DISPOSITION_PROPOSE
    assert proposal.retractor_command == "adjust_retraction"
    assert proposal.target_side == expected_side
    assert proposal.distance_m == pytest.approx(expected_distance_m)
    assert proposal.raw_text == utterance


@pytest.mark.parametrize(
    "utterance, intent, command, tool_id",
    [
        (
            "안녕하세요 저는 누구 입니다 갑상선절제술 시작",
            INTENT_PROCEDURE_START,
            "",
            "",
        ),
        (
            "안녕하세요 저는 누구 입니다 직접 교시 시작",
            INTENT_RETRACTOR_COMMAND,
            "start_direct_teach",
            "",
        ),
        (
            "안녕하세요 저는 누구 입니다 리트랙션 시작",
            INTENT_RETRACTOR_COMMAND,
            "start_retraction",
            "",
        ),
        (
            "안녕하세요 저는 누구 입니다 보비",
            INTENT_TOOL_HANDOVER,
            "",
            "T04",
        ),
    ],
)
def test_demo_catalog_resolves_trailing_command_after_unpunctuated_speech(
    utterance: str,
    intent: str,
    command: str,
    tool_id: str,
) -> None:
    specs = (
        Path(__file__).resolve().parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
    )
    demo = load_voice_command_catalog(specs / "thyroidectomy_demo")
    resolver = VoiceIntentResolver(
        tool_aliases=demo.tool_aliases,
        procedure_id=demo.procedure_id,
        catalog_id=demo.catalog_id,
        retractor_commands=demo.retractor_commands,
        retractor_max_distance_m=demo.retractor_max_distance_m,
        retractor_require_explicit_unit=demo.retractor_require_explicit_unit,
    )

    proposal = resolver.resolve(utterance)

    assert proposal.disposition == DISPOSITION_PROPOSE
    assert proposal.intent == intent
    assert proposal.retractor_command == command
    assert proposal.tool_id == tool_id
    assert proposal.raw_text == utterance


def test_mixed_adjustment_keeps_direction_and_distance_from_longest_suffix() -> None:
    specs = (
        Path(__file__).resolve().parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
    )
    demo = load_voice_command_catalog(specs / "thyroidectomy_demo")
    resolver = VoiceIntentResolver(
        tool_aliases=demo.tool_aliases,
        procedure_id=demo.procedure_id,
        catalog_id=demo.catalog_id,
        retractor_commands=demo.retractor_commands,
        retractor_max_distance_m=demo.retractor_max_distance_m,
        retractor_require_explicit_unit=demo.retractor_require_explicit_unit,
    )

    proposal = resolver.resolve(
        "안녕하세요 저는 누구 입니다 오른쪽으로 1cm만 더 당겨줘"
    )

    assert proposal.disposition == DISPOSITION_PROPOSE
    assert proposal.retractor_command == "adjust_retraction"
    assert proposal.target_side == "right"
    assert proposal.distance_m == pytest.approx(0.01)


@pytest.mark.parametrize(
    "utterance",
    [
        "아까 왼쪽 5cm 이야기했어 오른쪽 1cm 더 당겨줘",
        "이전에 왼쪽으로 움직였어 오른쪽으로 1cm 더 당겨줘",
        "이전 숫자는 5cm야 오른쪽으로 1cm 더 당겨줘",
        "아까 리트랙션 시작했어 오른쪽으로 1cm 더 당겨줘",
    ],
)
def test_mixed_adjustment_discards_stale_prefix_slots(utterance: str) -> None:
    specs = (
        Path(__file__).resolve().parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
    )
    demo = load_voice_command_catalog(specs / "thyroidectomy_demo")
    resolver = VoiceIntentResolver(
        tool_aliases=demo.tool_aliases,
        procedure_id=demo.procedure_id,
        catalog_id=demo.catalog_id,
        retractor_commands=demo.retractor_commands,
        retractor_max_distance_m=demo.retractor_max_distance_m,
        retractor_require_explicit_unit=demo.retractor_require_explicit_unit,
    )

    proposal = resolver.resolve(utterance)

    assert proposal.disposition == DISPOSITION_PROPOSE
    assert proposal.retractor_command == "adjust_retraction"
    assert proposal.target_side == "right"
    assert proposal.distance_m == pytest.approx(0.01)


@pytest.mark.parametrize(
    "utterance",
    [
        '"리트랙션 시작"',
        "리트랙션 시작이라고 말했어",
        "도구 교체라고 말했어",
        '"오른쪽으로 1cm 더 당겨줘"',
        '안녕하세요 "오른쪽으로 1cm 더 당겨줘"',
        '앞 문장입니다. "보비 주세요"',
        "앞 문장입니다. “갑상선절제술 시작”.",
        "오른쪽으로 1cm만 더 당겨줘라고 말했어",
        "왜 오른쪽으로 1cm 더 당겨줘",
        "오른쪽으로 1cm 더 당겨도 돼",
    ],
)
def test_retractor_quotes_reports_and_questions_are_not_executable(
    utterance: str,
) -> None:
    specs = (
        Path(__file__).resolve().parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
    )
    demo = load_voice_command_catalog(specs / "thyroidectomy_demo")
    resolver = VoiceIntentResolver(
        tool_aliases=demo.tool_aliases,
        procedure_id=demo.procedure_id,
        catalog_id=demo.catalog_id,
        retractor_commands=demo.retractor_commands,
        retractor_max_distance_m=demo.retractor_max_distance_m,
        retractor_require_explicit_unit=demo.retractor_require_explicit_unit,
    )

    assert not resolver.resolve(utterance).is_executable_proposal


def test_two_independent_commands_in_one_delivery_are_rejected() -> None:
    specs = (
        Path(__file__).resolve().parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
    )
    demo = load_voice_command_catalog(specs / "thyroidectomy_demo")
    resolver = VoiceIntentResolver(
        tool_aliases=demo.tool_aliases,
        procedure_id=demo.procedure_id,
        catalog_id=demo.catalog_id,
        retractor_commands=demo.retractor_commands,
        retractor_max_distance_m=demo.retractor_max_distance_m,
        retractor_require_explicit_unit=demo.retractor_require_explicit_unit,
    )

    proposal = resolver.resolve("갑상선절제술 시작 그리고 리트랙션 시작")

    assert proposal.disposition == DISPOSITION_REJECT
    assert proposal.reason == "multiple_command_clauses"


@pytest.mark.parametrize(
    "utterance",
    (
        "보비 주세요. 애드슨 주세요",
        "보비 주세요, 애드슨 주세요",
        "리트랙션 시작. 리트랙션 종료",
    ),
)
def test_explicit_sentence_boundaries_reject_same_family_commands(
    utterance: str,
) -> None:
    """A final with two executable sentences must execute neither sentence."""

    specs = (
        Path(__file__).resolve().parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
    )
    demo = load_voice_command_catalog(specs / "thyroidectomy_demo")
    resolver = VoiceIntentResolver(
        tool_aliases=demo.tool_aliases,
        procedure_id=demo.procedure_id,
        catalog_id=demo.catalog_id,
        retractor_commands=demo.retractor_commands,
        retractor_max_distance_m=demo.retractor_max_distance_m,
        retractor_require_explicit_unit=demo.retractor_require_explicit_unit,
    )

    proposal = resolver.resolve(utterance)

    assert proposal.disposition == DISPOSITION_REJECT
    assert proposal.reason == "multiple_command_clauses"


def test_two_unseparated_parameterized_retraction_adjustments_are_rejected() -> None:
    """One final must not silently keep only the later pull adjustment."""

    specs = (
        Path(__file__).resolve().parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
    )
    demo = load_voice_command_catalog(specs / "thyroidectomy_demo")
    resolver = VoiceIntentResolver(
        tool_aliases=demo.tool_aliases,
        procedure_id=demo.procedure_id,
        catalog_id=demo.catalog_id,
        retractor_commands=demo.retractor_commands,
        retractor_max_distance_m=demo.retractor_max_distance_m,
        retractor_require_explicit_unit=demo.retractor_require_explicit_unit,
    )

    proposal = resolver.resolve(
        "오른쪽 1cm 더 당겨줘 왼쪽 1cm 덜 당겨줘"
    )

    assert proposal.disposition == DISPOSITION_REJECT
    assert proposal.reason == "multiple_command_clauses"


@pytest.mark.parametrize(
    "utterance",
    [
        "앞 문장입니다. 1cm 더 당겨줘라고 말했어",
        "앞 문장입니다. 1cm 더 당겨줘?",
        "앞 문장입니다. 1cm 더 당기지 마",
    ],
)
def test_final_clause_keeps_background_question_and_negation_non_executable(
    utterance: str,
) -> None:
    specs = (
        Path(__file__).resolve().parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
    )
    demo = load_voice_command_catalog(specs / "thyroidectomy_demo")
    resolver = VoiceIntentResolver(
        tool_aliases=demo.tool_aliases,
        procedure_id=demo.procedure_id,
        catalog_id=demo.catalog_id,
        retractor_commands=demo.retractor_commands,
        retractor_max_distance_m=demo.retractor_max_distance_m,
        retractor_require_explicit_unit=demo.retractor_require_explicit_unit,
    )

    assert not resolver.resolve(utterance).is_executable_proposal


@pytest.mark.parametrize(
    "utterance",
    [
        "리트랙션 시작할까요?",
        "도구 교체하지 마",
        "리트랙션을 1 센치 더 당겨줘",
        "오른쪽 리트랙션 더 당겨줘",
        "오른쪽 리트랙션을 4 센치 더 당겨줘",
        "오른쪽 리트랙션을 4 센치 덜 당겨줘",
        "안녕하세요 4cm 더 당겨줘",
        "안녕하세요 99cm 더 당겨줘",
        "안녕하세요 0cm 더 당겨줘",
        "이전 숫자는 5cm야 오른쪽으로 4cm 더 당겨줘",
        "이전 숫자는 5cm야 오른쪽으로 4cm 덜 당겨줘",
        "이전에 왼쪽으로 움직였어 오른쪽으로 4cm 더 당겨줘",
    ],
)
def test_demo_catalog_retractor_questions_negation_and_ungrounded_adjustment_fail_closed(
    utterance: str,
) -> None:
    specs = (
        Path(__file__).resolve().parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
    )
    demo = load_voice_command_catalog(specs / "thyroidectomy_demo")
    resolver = VoiceIntentResolver(
        tool_aliases=demo.tool_aliases,
        procedure_id=demo.procedure_id,
        catalog_id=demo.catalog_id,
        retractor_commands=demo.retractor_commands,
        retractor_max_distance_m=demo.retractor_max_distance_m,
        retractor_require_explicit_unit=demo.retractor_require_explicit_unit,
    )

    assert resolver.resolve(utterance).disposition != DISPOSITION_PROPOSE


def test_catalog_hash_is_deterministic_and_alias_changes_are_visible() -> None:
    left = {"T02": ("애드슨", "adson"), "T01": ("메스",)}
    reordered = {"T01": ("메스",), "T02": ("adson", "애드슨")}
    changed = {"T01": ("메스",), "T02": ("애드슨", "adson", "포셉")}

    assert voice_catalog_id_for("case", left) == voice_catalog_id_for("case", reordered)
    assert voice_catalog_id_for("case", left) != voice_catalog_id_for("case", changed)
    assert voice_catalog_id_for(
        "case",
        left,
        retractor_commands=("start_retraction", "stop_retraction"),
    ) == voice_catalog_id_for(
        "case",
        reordered,
        retractor_commands=("stop_retraction", "start_retraction"),
    )
    assert voice_catalog_id_for(
        "case",
        left,
        retractor_commands=("start_retraction",),
    ) != voice_catalog_id_for(
        "case",
        left,
        retractor_commands=("start_retraction", "stop_retraction"),
    )


def test_ambiguous_bundle_aliases_are_dropped_not_guessed() -> None:
    specs = (
        Path(__file__).resolve().parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
    )
    catalog = load_voice_command_catalog(specs / "inguinal_hernia_repair")

    assert "박리" in catalog.ambiguous_aliases
    assert all("박리" not in aliases for aliases in catalog.tool_aliases.values())
