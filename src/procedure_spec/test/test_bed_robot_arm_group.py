from __future__ import annotations

from pathlib import Path

import pytest

from procedure_spec import (
    BedRobotArmGroupNormalizationError,
    compact_procedure_prompt,
    infer_retraction_direction,
    load_bundle,
    normalize_retraction_distance,
    normalize_retraction_request,
    validate_retraction_distance_proposal,
)


@pytest.mark.parametrize(
    ("utterance", "expected"),
    [
        ("위쪽으로 당겨줘", "UP"),
        ("위 당겨줘", "UP"),
        ("상방으로 당겨줘", "UP"),
        ("상으로 당겨줘", "UP"),
        ("아래로 당겨줘", "DOWN"),
        ("아래 당겨줘", "DOWN"),
        ("하방으로 당겨줘", "DOWN"),
        ("하로 당겨줘", "DOWN"),
        ("왼쪽으로 당겨줘", "LEFT"),
        ("좌측으로 당겨줘", "LEFT"),
        ("좌로 당겨줘", "LEFT"),
        ("오른쪽으로 당겨줘", "RIGHT"),
        ("우측으로 당겨줘", "RIGHT"),
        ("우로 당겨줘", "RIGHT"),
        ("오른쪽, 왼쪽 동시에 당겨줘", "LEFT_RIGHT"),
        ("좌우 동시에 당겨줘", "LEFT_RIGHT"),
        ("위, 아래 동시에 당겨줘", "UP_DOWN"),
        ("상하로 동시에 당겨줘", "UP_DOWN"),
    ],
)
def test_six_direction_aliases(utterance: str, expected: str) -> None:
    assert infer_retraction_direction(utterance) == expected


@pytest.mark.parametrize(
    ("utterance", "distance_mm", "origin", "raw"),
    [
        ("1 cm 더 당겨줘", 10.0, "explicit_with_unit", "1 cm"),
        ("1센치 더 당겨줘", 10.0, "explicit_with_unit", "1센치"),
        ("1씨엠 더 당겨줘", 10.0, "explicit_with_unit", "1씨엠"),
        ("2.5 cm 더 당겨줘", 25.0, "explicit_with_unit", "2.5 cm"),
        ("30 mm 더 당겨줘", 30.0, "explicit_with_unit", "30 mm"),
        ("0.5밀리미터 더 당겨줘", 0.5, "explicit_with_unit", "0.5밀리미터"),
    ],
)
def test_only_explicit_mm_or_cm_distances_are_normalized(
    utterance: str,
    distance_mm: float,
    origin: str,
    raw: str,
) -> None:
    result = normalize_retraction_distance(utterance)
    assert result.distance_mm == distance_mm
    assert result.distance_origin == origin
    assert result.raw_distance_text == raw


@pytest.mark.parametrize(
    "utterance",
    [
        "",
        "당겨줘",
        "견인해줘",
        "10 더 당겨줘",
        "조금 더 당겨줘",
        "살짝 당겨줘",
        "많이 당겨줘",
        "아주 많이 당겨줘",
        "최대한 당겨줘",
    ],
)
def test_missing_unit_or_qualitative_distance_is_rejected(utterance: str) -> None:
    with pytest.raises(BedRobotArmGroupNormalizationError, match="explicit numeric mm/cm"):
        normalize_retraction_distance(utterance)


def test_vlm_qualitative_distance_is_never_accepted() -> None:
    with pytest.raises(BedRobotArmGroupNormalizationError, match="not permitted"):
        normalize_retraction_distance("중간 정도 당겨줘", qualitative_distance_mm=17)


def test_explicit_distance_is_deterministically_rechecked() -> None:
    result = validate_retraction_distance_proposal(
        raw_distance_text="3 cm",
        distance_mm=30,
        distance_origin="explicit_with_unit",
    )
    assert result.distance_mm == 30

    with pytest.raises(BedRobotArmGroupNormalizationError, match="distance mismatch"):
        validate_retraction_distance_proposal(
            raw_distance_text="2 cm",
            distance_mm=10,
            distance_origin="explicit_with_unit",
        )


@pytest.mark.parametrize("utterance", ["-1 cm 당겨줘", "0 mm 더 당겨줘"])
def test_non_positive_explicit_distance_is_rejected(utterance: str) -> None:
    with pytest.raises(BedRobotArmGroupNormalizationError, match="positive finite"):
        normalize_retraction_distance(utterance)


@pytest.mark.parametrize("utterance", ["30.1 mm 당겨줘", "3.1 cm 당겨줘", "5 cm 당겨줘"])
def test_distance_above_contract_limit_is_rejected(utterance: str) -> None:
    with pytest.raises(BedRobotArmGroupNormalizationError, match="30 mm contract limit"):
        normalize_retraction_distance(utterance)


def test_multiple_explicit_distances_are_rejected() -> None:
    with pytest.raises(BedRobotArmGroupNormalizationError, match="exactly one"):
        normalize_retraction_distance("왼쪽은 10 mm, 오른쪽은 20 mm 당겨줘")


def test_direction_can_come_from_vlm_when_voice_does_not_state_it() -> None:
    result = normalize_retraction_request(
        "10 mm 당겨줘",
        vlm_direction="UP_DOWN",
    )
    assert result.direction == "UP_DOWN"
    assert result.distance_mm == 10


def test_nephrectomy_retraction_cues_match_reviewed_per_arm_semantics() -> None:
    spec = load_bundle(_spec_root() / "nephrectomy")
    cues = spec.get_bed_robot_arm_group_cues("P02")
    assert {cue.id for cue in cues} == {
        "left_malleable_adjustment",
        "right_malleable_adjustment",
        "bilateral_malleable_adjustment",
    }
    assert all(cue.group_id == "retraction" for cue in cues)
    assert all(cue.operation == "retraction" for cue in cues)
    assert all(cue.direction_frame == "surgeon_view" for cue in cues)
    assert all(
        infer_retraction_direction(text)
        for cue in cues
        for text in cue.utterances
    )


def _spec_root() -> Path:
    return Path(__file__).parents[1] / "procedure_spec" / "specs"


@pytest.mark.parametrize(
    (
        "procedure_id",
        "retraction_enabled",
        "initial_retraction_profile",
        "allowed_operations",
        "has_cues",
    ),
    [
        ("inguinal_hernia_repair", False, "", [], False),
        (
            "thyroidectomy",
            True,
            "thyroid_retractor",
            ["change_end_effector"],
            False,
        ),
        (
            "thyroidectomy_demo",
            True,
            "thyroid_retractor",
            ["change_end_effector"],
            False,
        ),
        ("nephrectomy", True, "", ["retraction"], True),
    ],
)
def test_procedure_bundles_load_group_scenarios(
    procedure_id: str,
    retraction_enabled: bool,
    initial_retraction_profile: str,
    allowed_operations: list[str],
    has_cues: bool,
) -> None:
    bundle_dir = _spec_root() / procedure_id
    spec = load_bundle(bundle_dir)
    group_spec = spec.get_bed_robot_arm_group_spec()
    assert group_spec is not None
    groups = {group.id: group for group in group_spec.groups}
    assert set(groups) == {"retraction"}
    assert groups["retraction"].enabled is retraction_enabled
    assert groups["retraction"].initial_end_effector_profile == initial_retraction_profile
    assert groups["retraction"].allowed_operations == allowed_operations
    assert group_spec.max_distance_mm == 30
    assert group_spec.cm_to_mm_multiplier == 10
    assert group_spec.require_explicit_unit is True
    assert group_spec.clamp_explicit_values is False
    assert group_spec.distance_precedence == ["explicit_with_unit"]
    assert bool(spec.get_bed_robot_arm_group_cues()) is has_cues

    compact = compact_procedure_prompt(bundle_dir)
    assert (
        compact["bed_robot_arm_groups"]["groups"]["retraction"]["enabled"]
        is retraction_enabled
    )


def test_expected_end_effector_transitions_are_loaded() -> None:
    for procedure_id in ("inguinal_hernia_repair", "nephrectomy", "thyroidectomy_demo"):
        spec = load_bundle(_spec_root() / procedure_id)
        assert spec.get_bed_robot_arm_end_effector_transitions() == []

    spec = load_bundle(_spec_root() / "thyroidectomy")
    transitions = spec.get_bed_robot_arm_end_effector_transitions()
    assert len(transitions) == 1
    transition = transitions[0]
    assert (transition.from_profile, transition.to_profile) == (
        "thyroid_retractor",
        "army_navy_retractor",
    )
    assert transition.arm_id == "arm_1"
    assert transition.target_tool_id == "army_navy_retractor"
