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
        ("5 cm 더 당겨줘", 50.0, "explicit_with_unit", "5 cm"),
        ("10 더 당겨줘", 10.0, "explicit_unit_inferred", "10"),
        ("아주 살짝 당겨줘", 1.0, "qualitative_inferred", "아주 살짝"),
        ("미세하게 당겨줘", 1.0, "qualitative_inferred", "미세하게"),
        ("살짝 당겨줘", 5.0, "qualitative_inferred", "살짝"),
        ("조금 더 당겨줘", 10.0, "qualitative_inferred", "조금 더"),
        ("많이 당겨줘", 20.0, "qualitative_inferred", "많이"),
        ("아주 많이 당겨줘", 30.0, "qualitative_inferred", "아주 많이"),
        ("최대한 당겨줘", 30.0, "qualitative_inferred", "최대한"),
        ("당겨줘", 10.0, "defaulted", ""),
    ],
)
def test_distance_precedence_and_anchors(
    utterance: str,
    distance_mm: float,
    origin: str,
    raw: str,
) -> None:
    result = normalize_retraction_distance(utterance)
    assert result.distance_mm == distance_mm
    assert result.distance_origin == origin
    assert result.raw_distance_text == raw


def test_non_anchor_vlm_qualitative_value_is_limited_to_one_through_thirty() -> None:
    result = normalize_retraction_distance("중간 정도 당겨줘", qualitative_distance_mm=17)
    assert result.distance_mm == 17
    assert result.distance_origin == "qualitative_inferred"

    with pytest.raises(BedRobotArmGroupNormalizationError, match="between 1 and 30"):
        normalize_retraction_distance("중간 정도 당겨줘", qualitative_distance_mm=31)
    with pytest.raises(BedRobotArmGroupNormalizationError, match="integer"):
        normalize_retraction_distance("중간 정도 당겨줘", qualitative_distance_mm=17.5)


@pytest.mark.parametrize("raw_text", ["", "당겨줘", "견인해줘"])
def test_vlm_cannot_invent_qualitative_distance_without_spoken_intensity(
    raw_text: str,
) -> None:
    with pytest.raises(BedRobotArmGroupNormalizationError, match="intensity expression"):
        normalize_retraction_distance(raw_text, qualitative_distance_mm=15)


def test_explicit_distance_is_not_clamped_but_is_deterministically_rechecked() -> None:
    result = validate_retraction_distance_proposal(
        raw_distance_text="5 cm",
        distance_mm=50,
        distance_origin="explicit_with_unit",
    )
    assert result.distance_mm == 50

    with pytest.raises(BedRobotArmGroupNormalizationError, match="distance mismatch"):
        validate_retraction_distance_proposal(
            raw_distance_text="5 cm",
            distance_mm=30,
            distance_origin="explicit_with_unit",
        )


@pytest.mark.parametrize("utterance", ["-1 cm 당겨줘", "0 더 당겨줘"])
def test_non_positive_explicit_distance_is_rejected(utterance: str) -> None:
    with pytest.raises(BedRobotArmGroupNormalizationError, match="positive finite"):
        normalize_retraction_distance(utterance)


def test_direction_can_come_from_vlm_when_voice_does_not_state_it() -> None:
    result = normalize_retraction_request(
        "조금 당겨줘",
        vlm_direction="UP_DOWN",
    )
    assert result.direction == "UP_DOWN"
    assert result.distance_mm == 10


def test_nephrectomy_initial_mayo_cues_are_mock_vlm_groundable() -> None:
    spec = load_bundle(_spec_root() / "nephrectomy")
    cues = [
        cue
        for cue in spec.get_bed_robot_arm_group_cues("P01")
        if cue.id == "mayo_muscle_exposure"
    ]
    assert cues
    assert all(infer_retraction_direction(text) for text in cues[0].utterances)


def _spec_root() -> Path:
    return Path(__file__).parents[1] / "procedure_spec" / "specs"


@pytest.mark.parametrize(
    ("procedure_id", "suction_enabled", "initial_retraction_profile"),
    [
        ("inguinal_hernia_repair", False, "army_navy"),
        ("thyroidectomy", True, "thyroid_retractor"),
        ("nephrectomy", False, "mayo"),
    ],
)
def test_procedure_bundles_load_group_scenarios(
    procedure_id: str,
    suction_enabled: bool,
    initial_retraction_profile: str,
) -> None:
    bundle_dir = _spec_root() / procedure_id
    spec = load_bundle(bundle_dir)
    group_spec = spec.get_bed_robot_arm_group_spec()
    assert group_spec is not None
    groups = {group.id: group for group in group_spec.groups}
    assert set(groups) == {"suction", "retraction"}
    assert groups["suction"].enabled is suction_enabled
    assert groups["retraction"].initial_end_effector_profile == initial_retraction_profile
    assert group_spec.default_distance_mm == 10
    assert group_spec.qualitative_min_mm == 1
    assert group_spec.qualitative_max_mm == 30
    assert group_spec.cm_to_mm_multiplier == 10
    assert group_spec.unitless_numeric_unit == "mm"
    assert group_spec.clamp_explicit_values is False
    assert group_spec.qualitative_integer_mm is True
    assert group_spec.distance_precedence == [
        "explicit_with_unit",
        "explicit_unit_inferred",
        "qualitative_inferred",
        "defaulted",
    ]
    assert spec.get_bed_robot_arm_group_cues()

    compact = compact_procedure_prompt(bundle_dir)
    assert compact["bed_robot_arm_groups"]["groups"]["retraction"]["enabled"] is True


def test_expected_end_effector_transitions_are_loaded() -> None:
    expected = {
        "inguinal_hernia_repair": ("army_navy", "mosquito"),
        "thyroidectomy": ("thyroid_retractor", "army"),
        "nephrectomy": ("mayo", "malleable"),
    }
    for procedure_id, profiles in expected.items():
        spec = load_bundle(_spec_root() / procedure_id)
        transitions = spec.get_bed_robot_arm_end_effector_transitions()
        assert any(
            (transition.from_profile, transition.to_profile) == profiles
            for transition in transitions
        )
