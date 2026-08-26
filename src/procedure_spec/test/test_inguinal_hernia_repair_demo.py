from __future__ import annotations

from pathlib import Path

import pytest

from procedure_spec import (
    RetractionCommand,
    RetractionState,
    RetractionTargetSide,
    load_bundle,
    load_voice_command_catalog,
    normalize_retractor_command,
)


SPEC_DIR = (
    Path(__file__).parents[1]
    / "procedure_spec"
    / "specs"
    / "inguinal_hernia_repair_demo"
)


def test_demo_is_voice_retraction_only_with_plausible_manual_context() -> None:
    spec = load_bundle(SPEC_DIR)

    assert spec.procedure_id == "inguinal_hernia_repair_demo"
    assert spec.default_phase_id == "P02"
    assert [phase.id for phase in spec.bundle.phases] == [
        "P01",
        "P02",
        "P03",
        "P04",
        "P05",
    ]
    assert len(spec.bundle.instruments) == 9
    assert all(not instrument.requestable for instrument in spec.bundle.instruments)
    assert spec.bundle.mock_surgeon.stages == []
    assert spec.bundle.mock_perception is not None
    assert all(
        not stage.explicit_request
        for stage in spec.bundle.mock_perception.stages
    )
    assert all(
        not hasattr(stage, "surgeon_gesture")
        for stage in spec.bundle.mock_perception.stages
    )

    catalog = load_voice_command_catalog(SPEC_DIR)
    assert catalog.procedure_id == "inguinal_hernia_repair_demo"
    assert catalog.tool_aliases == {}
    assert catalog.ambiguous_aliases == {}
    assert catalog.catalog_id.startswith("sha256:")


def test_demo_exposes_only_reviewed_voice_retraction_commands() -> None:
    spec = load_bundle(SPEC_DIR)
    group = spec.get_bed_robot_arm_group_spec()
    assert group is not None
    assert group.max_distance_mm == 30
    assert group.require_explicit_unit is True
    assert len(group.groups) == 1
    retraction = group.groups[0]
    assert retraction.id == "retraction"
    assert retraction.enabled is True
    assert retraction.initial_end_effector_profile == "army_navy_retractor"
    assert retraction.allowed_operations == ["retraction"]
    assert retraction.allowed_voice_commands == [
        "start_direct_teach",
        "finish_direct_teach",
        "start_retraction",
        "adjust_retraction",
        "stop_retraction",
    ]
    assert "change_tool" not in retraction.allowed_voice_commands

    cues = spec.get_bed_robot_arm_group_cues("P02")
    assert {cue.target_retractor_id for cue in cues} == {
        "left_army_navy",
        "right_army_navy",
        "both_army_navy",
    }
    assert {
        cue.adjustment_mode for cue in cues
    } == {"single", "multi"}


@pytest.mark.parametrize(
    ("transcript", "side", "distance_m"),
    [
        ("아미를 오른쪽으로 1센치 더 당겨줘", RetractionTargetSide.RIGHT, 0.01),
        ("아미를 왼쪽으로 1씨엠 더 당겨줘", RetractionTargetSide.LEFT, 0.01),
        ("양쪽으로 1mm씩 당겨줘", RetractionTargetSide.BOTH, 0.001),
    ],
)
def test_source_sheet_one_centimeter_commands_are_grounded(
    transcript: str,
    side: RetractionTargetSide,
    distance_m: float,
) -> None:
    normalized = normalize_retractor_command(
        transcript,
        RetractionState.RETRACTION_ACTIVE,
    )

    assert normalized.command is RetractionCommand.ADJUST_RETRACTION
    assert normalized.target_side is side
    assert normalized.distance_m == pytest.approx(distance_m)


def test_contradictory_bilateral_adjustment_is_rejected_instead_of_split() -> None:
    normalized = normalize_retractor_command(
        "아미 양쪽 오른쪽을 동시에 1씨엠 더 당겨줘",
        RetractionState.RETRACTION_ACTIVE,
    )

    assert normalized.command is None
    assert normalized.reason == "ambiguous_target_side"
