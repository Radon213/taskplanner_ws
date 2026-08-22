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
    INTENT_TOOL_HANDOVER,
    TARGET_SIDE_NONE,
)
from voice_command.resolver import VoiceIntentResolver
from voice_command.selector import CandidateSelection


_ALIASES = {
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
    "utterance",
    [
        "교시 시작",
        "직접교시 시작",
        "자 이제 교시를 시작해보자",
    ],
)
def test_natural_direct_teach_start_is_short_and_grounded(
    resolver: VoiceIntentResolver,
    utterance: str,
) -> None:
    proposal = resolver.resolve(utterance)

    assert proposal.disposition == DISPOSITION_PROPOSE
    assert proposal.intent == INTENT_RETRACTOR_COMMAND
    assert proposal.retractor_command == "start_direct_teach"
    assert proposal.tool_id == ""
    assert proposal.target_side == TARGET_SIDE_NONE
    assert proposal.distance_m == 0.0
    assert proposal.requires_confirmation is False


@pytest.mark.parametrize("utterance", ["직접 교실 시작", "교시시 시작"])
def test_observed_asr_repair_is_local_and_confirmation_required(
    resolver: VoiceIntentResolver,
    utterance: str,
) -> None:
    proposal = resolver.resolve(utterance)

    assert proposal.raw_text == utterance
    assert proposal.disposition == DISPOSITION_PROPOSE
    assert proposal.retractor_command == "start_direct_teach"
    assert proposal.provenance.startswith("observed_asr_repair")
    assert proposal.requires_confirmation is True


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
    assert proposal.provenance.endswith("deterministic_strong_anchor")


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
def test_selector_only_natural_variant_is_confirmation_required(
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

    assert proposal.disposition == DISPOSITION_PROPOSE
    assert proposal.requires_confirmation is True
    assert proposal.provenance.startswith("vlm_anchored_natural_variant")


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


def test_catalog_hash_is_deterministic_and_alias_changes_are_visible() -> None:
    left = {"T02": ("애드슨", "adson"), "T01": ("메스",)}
    reordered = {"T01": ("메스",), "T02": ("adson", "애드슨")}
    changed = {"T01": ("메스",), "T02": ("애드슨", "adson", "포셉")}

    assert voice_catalog_id_for("case", left) == voice_catalog_id_for("case", reordered)
    assert voice_catalog_id_for("case", left) != voice_catalog_id_for("case", changed)


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
