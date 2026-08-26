from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from procedure_spec import (
    ProcedurePriorScorer,
    compact_procedure_prompt,
    discover_prompt_bundle_dirs,
    load_bundle,
)
from procedure_spec.prompt_bundle import build_raw_bundle_from_prompt
from procedure_spec.validator import SpecValidationError, validate_raw_bundle


def _spec_root() -> Path:
    return Path(__file__).parents[1] / "procedure_spec" / "specs"


def _build_with_tool_voice_aliases(
    tmp_path: Path,
    tool_voice_aliases: object,
) -> dict:
    source_bundle = _spec_root() / "thyroidectomy_demo"
    prompt = yaml.safe_load(
        (source_bundle / "vlm_procedure_prompt.yaml").read_text(encoding="utf-8")
    )
    prompt["tool_voice_aliases"] = tool_voice_aliases
    candidate = tmp_path / "voice_alias_prompt"
    candidate.mkdir()
    (candidate / "vlm_procedure_prompt.yaml").write_text(
        yaml.safe_dump(prompt, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    display_catalog = yaml.safe_load(
        (_spec_root() / "display_catalog.yaml").read_text(encoding="utf-8")
    )
    return build_raw_bundle_from_prompt(candidate, display_catalog)


def test_demo_prompt_loads_as_video_derived_thyroidectomy_bundle() -> None:
    bundle_dir = _spec_root() / "thyroidectomy_demo"
    spec = load_bundle(bundle_dir)

    assert spec.procedure_id == "thyroidectomy_demo"
    assert spec.bundle.procedure_display_name == "Thyroidectomy"
    assert spec.bundle.procedure_display_name_ko == "갑상선절제술(시연)"
    assert spec.bundle.procedure_target_site == "Right Lobectomy"
    assert spec.bundle.procedure_target_site_ko == "Right Lobectomy"
    assert spec.bundle.procedure_approach == "Open"
    assert spec.bundle.procedure_approach_ko == "Open"
    assert spec.normal_phase_ids == [f"P{index:02d}" for index in range(1, 11)]
    assert spec.interrupt_phase_ids == []
    assert spec.default_phase_id == "P03"
    assert spec.get_allowed_next_phases("P01") == ["P02"]
    assert spec.get_allowed_next_phases("P03") == ["P04"]
    assert spec.get_allowed_next_phases("P09") == ["P10"]
    assert spec.get_allowed_next_phases("P10") == []
    assert spec.bundle.humanoid_policy is not None
    assert spec.bundle.humanoid_policy.return_unused_preposition_to_mayo is True
    assert [phase.display_name_ko for phase in spec.bundle.phases] == [
        "환자 체위 및 수술부위 준비",
        "피부 절개 및 피판 거상",
        "고정 견인 전 중앙 수술야 박리",
        "고정 견인 배치 및 노출 확립",
        "견인 유지 하 표적 조직 조작",
        "국소 표적 제어 및 처치",
        "갑상선 절제 및 검체 적출",
        "최종 지혈 및 수술야 확인",
        "창상 봉합",
        "수술 종료 및 기구 정리",
    ]
    standard_spec = load_bundle(_spec_root() / "thyroidectomy")
    assert standard_spec.default_phase_id == "P01"
    assert standard_spec.bundle.procedure_display_name == "Thyroidectomy"
    assert standard_spec.bundle.procedure_display_name_ko == "Thyroidectomy"
    assert standard_spec.bundle.procedure_target_site == "Right Lobectomy"
    assert standard_spec.bundle.procedure_target_site_ko == "Right Lobectomy"
    assert standard_spec.bundle.procedure_approach == "Open"
    assert standard_spec.bundle.procedure_approach_ko == "Open"


def test_prompt_mock_perception_excludes_and_rejects_vlm_hand_contract() -> None:
    bundle_dir = _spec_root() / "thyroidectomy_demo"
    display_catalog = yaml.safe_load(
        (bundle_dir.parent / "display_catalog.yaml").read_text(encoding="utf-8")
    )
    raw_bundle = build_raw_bundle_from_prompt(bundle_dir, display_catalog)

    assert all(
        "surgeon_gesture" not in stage
        for stage in raw_bundle["mock_perception"]["stages"]
    )

    raw_bundle["mock_perception"]["stages"][0]["surgeon_gesture"] = {
        "event_type": "request_tool",
        "requested_tool": "T02",
        "hand_pose": "open_palm",
        "confidence": 0.9,
    }
    with pytest.raises(SpecValidationError, match="retired field 'surgeon_gesture'"):
        validate_raw_bundle(raw_bundle)


def test_demo_prompt_contains_rack_inventory_without_bed_arm_retractors() -> None:
    spec = load_bundle(_spec_root() / "thyroidectomy_demo")

    assert spec.list_instrument_ids() == [
        "T02",
        "T03",
        "T04",
        "T07",
    ]
    assert spec.get_tool_inventory() == {
        "T02": 1,
        "T03": 2,
        "T04": 1,
        "T07": 1,
    }
    assert sum(spec.get_tool_inventory().values()) == 5
    assert spec.resolve_instrument_alias("Yankauer suction") is None
    assert spec.resolve_instrument_alias("Army navy retractor") is None
    assert spec.resolve_instrument_alias("thyroid retractor") is None
    assert spec.resolve_instrument_alias("Middeldorpf retractor") is None
    assert spec.resolve_instrument_alias("갑상선 리트랙터") is None
    assert [
        (placement.instrument_id, placement.location_id)
        for placement in spec.bundle.initial_placements
    ] == [
        ("T02", "main_tray_slot_1"),
        ("T03", "main_tray_slot_2"),
        ("T04", "main_tray_slot_3"),
        ("T07", "main_tray_slot_4"),
    ]
    requestable = {
        instrument.id
        for instrument in spec.bundle.instruments
        if instrument.requestable
    }
    assert requestable == {"T02", "T04", "T07"}
    assert spec.resolve_instrument_alias("아드손") == "T02"
    assert spec.resolve_instrument_alias("애드손") == "T02"
    assert [
        location.id
        for location in spec.bundle.locations
        if location.type == "tray_slot"
    ] == [f"main_tray_slot_{index}" for index in range(1, 5)]
    assert [
        (
            state.instance_id,
            state.location_id,
            state.lifecycle_stage,
        )
        for state in spec.get_initial_instrument_states()
    ] == [
        ("T02#1", "main_tray_slot_1", "home_rack"),
        ("T03#1", "field_region_procedure", "surgeon_owned"),
        ("T03#2", "field_region_procedure", "surgeon_owned"),
    ]
    deployed_instances = {
        state.instance_id for state in spec.get_initial_instrument_states()
    }
    rack_instances = {
        f"{tool_id}#{index}"
        for tool_id, count in spec.get_tool_inventory().items()
        for index in range(1, count + 1)
        if f"{tool_id}#{index}" not in deployed_instances
    }
    assert rack_instances == {"T04#1", "T07#1"}


@pytest.mark.parametrize("raw_values", [True, 1, {"alias": "아드손"}, "아드손"])
def test_tool_voice_aliases_require_a_list(
    tmp_path: Path,
    raw_values: object,
) -> None:
    with pytest.raises(ValueError, match="must be a list"):
        _build_with_tool_voice_aliases(tmp_path, {"T02": raw_values})


@pytest.mark.parametrize("raw_alias", [True, 1, {"alias": "아드손"}])
def test_tool_voice_aliases_reject_non_string_values(
    tmp_path: Path,
    raw_alias: object,
) -> None:
    with pytest.raises(ValueError, match="aliases must be strings"):
        _build_with_tool_voice_aliases(tmp_path, {"T02": [raw_alias]})


@pytest.mark.parametrize(
    "raw_values, message",
    [
        ([], "must be non-empty"),
        (["   "], "contains an empty alias"),
        (["Adson", "ＡＤＳＯＮ"], "duplicate aliases after normalization"),
    ],
)
def test_tool_voice_aliases_reject_empty_or_normalized_duplicates(
    tmp_path: Path,
    raw_values: list[object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _build_with_tool_voice_aliases(tmp_path, {"T02": raw_values})


@pytest.mark.parametrize(
    "reserved_alias",
    [
        "주세요",
        "줘",
        "please",
        "handover",
        "hand-over",
        "can you",
        "do not",
    ],
)
def test_tool_voice_aliases_reject_command_language(
    tmp_path: Path,
    reserved_alias: str,
) -> None:
    with pytest.raises(ValueError, match="contains reserved command cue"):
        _build_with_tool_voice_aliases(tmp_path, {"T02": [reserved_alias]})


def test_tool_voice_aliases_reject_cross_tool_normalized_duplicates(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="to both T02 and T04"):
        _build_with_tool_voice_aliases(
            tmp_path,
            {"T02": ["Custom Adson"], "T04": ["custom-adson"]},
        )


@pytest.mark.parametrize("unsafe_alias", ["a", "칼", "123"])
def test_tool_voice_aliases_require_a_distinctive_identifier(
    tmp_path: Path,
    unsafe_alias: str,
) -> None:
    with pytest.raises(ValueError, match="not a distinctive tool identifier"):
        _build_with_tool_voice_aliases(tmp_path, {"T02": [unsafe_alias]})


def test_tool_voice_aliases_cannot_impersonate_another_tools_builtin_name(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="collides with T04"):
        _build_with_tool_voice_aliases(tmp_path, {"T02": ["Bovie"]})


def test_demo_mock_bootstrap_does_not_overwrite_instance_level_field_setup() -> None:
    bundle_dir = _spec_root() / "thyroidectomy_demo"
    display_catalog = yaml.safe_load(
        (bundle_dir.parent / "display_catalog.yaml").read_text(encoding="utf-8")
    )
    raw_bundle = build_raw_bundle_from_prompt(bundle_dir, display_catalog)
    bootstrap = raw_bundle["mock_perception"]["stages"][0]["observations"]

    assert [row["instrument_id"] for row in bootstrap] == ["T02", "T04", "T07"]
    assert all(row["instrument_id"] != "T03" for row in bootstrap)
    summary = raw_bundle["mock_perception"]["stages"][0]["scene_summary"]
    assert "Only unambiguous home-rack instruments" in summary
    assert "authored non-home instance placements remain unchanged" in summary


@pytest.mark.parametrize(
    ("lifecycle_stage", "location_id", "message"),
    [
        ("home_rack", "field_region_procedure", "authored home location"),
        ("returned_home", "field_region_procedure", "authored home location"),
        ("mayo_reuse", "cleaner_slot", "mayo_stand"),
        ("mayo_recovery", "main_tray_slot_1", "mayo_stand"),
        ("prepositioned_right", "cleaner_slot", "robot_right_hand"),
        ("recovering_left", "main_tray_slot_1", "robot_left_hand"),
        ("cleaning_left", "main_tray_slot_1", "cleaner_slot"),
        ("cleaned_left", "main_tray_slot_1", "cleaner_slot"),
    ],
)
def test_initial_lifecycle_rejects_contradictory_location(
    lifecycle_stage: str,
    location_id: str,
    message: str,
) -> None:
    bundle_dir = _spec_root() / "thyroidectomy_demo"
    display_catalog = yaml.safe_load(
        (bundle_dir.parent / "display_catalog.yaml").read_text(encoding="utf-8")
    )
    raw_bundle = build_raw_bundle_from_prompt(bundle_dir, display_catalog)
    state = raw_bundle["scene_layout"]["initial_instrument_states"][0]
    state["lifecycle_stage"] = lifecycle_stage
    state["location_id"] = location_id

    with pytest.raises(SpecValidationError, match=message):
        validate_raw_bundle(raw_bundle)


def test_bundle_tool_placement_drives_home_state_and_mock_observations(
    tmp_path: Path,
) -> None:
    source_bundle = _spec_root() / "thyroidectomy"
    prompt = yaml.safe_load(
        (source_bundle / "vlm_procedure_prompt.yaml").read_text(encoding="utf-8")
    )
    rack_order = ["T10", "T02", "T03", "T04", "T05", "T06", "T07", "T08", "T09", "T01"]
    prompt["tool_placement"]["rack_order"] = rack_order
    candidate = tmp_path / "custom_thyroidectomy"
    candidate.mkdir()
    (candidate / "vlm_procedure_prompt.yaml").write_text(
        yaml.safe_dump(prompt, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    display_catalog = yaml.safe_load(
        (_spec_root() / "display_catalog.yaml").read_text(encoding="utf-8")
    )

    raw_bundle = build_raw_bundle_from_prompt(candidate, display_catalog)

    assert raw_bundle["scene_layout"]["initial_instrument_placement"] == [
        {"instrument_id": tool_id, "location_id": f"main_tray_slot_{index}"}
        for index, tool_id in enumerate(rack_order, start=1)
    ]
    assert [
        (row["instrument_id"], row["location_id"])
        for row in raw_bundle["mock_perception"]["stages"][0]["observations"]
    ] == [
        (tool_id, f"main_tray_slot_{index}")
        for index, tool_id in enumerate(rack_order, start=1)
    ]


@pytest.mark.parametrize(
    ("rack_order", "message"),
    [
        (["T01", "T01"], "must list every tool exactly once"),
        ("T01", "rack_order must be a list"),
    ],
)
def test_bundle_tool_placement_rejects_ambiguous_rack_ownership(
    tmp_path: Path,
    rack_order: object,
    message: str,
) -> None:
    source_bundle = _spec_root() / "thyroidectomy"
    prompt = yaml.safe_load(
        (source_bundle / "vlm_procedure_prompt.yaml").read_text(encoding="utf-8")
    )
    prompt["tool_placement"]["rack_order"] = rack_order
    candidate = tmp_path / "invalid_thyroidectomy"
    candidate.mkdir()
    (candidate / "vlm_procedure_prompt.yaml").write_text(
        yaml.safe_dump(prompt, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    display_catalog = yaml.safe_load(
        (_spec_root() / "display_catalog.yaml").read_text(encoding="utf-8")
    )

    with pytest.raises(ValueError, match=message):
        build_raw_bundle_from_prompt(candidate, display_catalog)


def test_demo_prompt_keeps_bed_arm_retraction_out_of_rack_handover_patterns() -> None:
    compact = compact_procedure_prompt(_spec_root() / "thyroidectomy_demo")
    serialized = json.dumps(compact, ensure_ascii=False, sort_keys=True)

    assert [
        "P03",
        "P04",
        "persistent bed-arm retraction, not a rack handover, can support this transition",
    ] in compact["flow"]
    assert all(row[0] != "*" for row in compact["flow"])
    assert compact["seq"]["P04"] == [
        [
            "T02",
            "T02",
            "a second fine forceps may reinforce traction after fixed exposure",
            "medium",
        ],
    ]
    assert compact["seq"]["P06"][:2] == [
        [
            "T07",
            "T04",
            "broader treatment commonly follows precise energy when further division is needed",
            "high",
        ],
        [
            "T03",
            "T07",
            "a visibly equivalent focal control can be followed by bipolar",
            "medium",
        ],
    ]
    assert compact["cues"]["P03"][0].startswith(
        "an open central neck field before stable bilateral"
    )
    assert compact["phase_policy"]["tool_order_role"].startswith(
        "Public voice, handover, and tool-recognition order are supportive"
    )
    assert compact["phase_groups"]["M02"]["members"] == ["P04", "P05"]
    assert compact["roles"]["P03"] == {
        "entry_handover": ["T02"],
        "tissue_handling": ["T02", "T03"],
        "dissection_or_hemostasis": ["T04", "T07"],
    }
    assert compact["roles"]["P06"] == {
        "focal_control": ["T03"],
        "localized_treatment_alternatives": ["T07", "T04"],
    }
    assert compact["bed_robot_arm_groups"]["groups"]["retraction"] == {
        "enabled": True,
        "initial_end_effector_profile": "thyroid_retractor",
        "allowed_operations": ["retraction"],
        "allowed_voice_commands": [
            "start_direct_teach",
            "finish_direct_teach",
            "start_retraction",
            "adjust_retraction",
            "change_tool",
            "stop_retraction",
        ],
    }
    assert compact["bed_robot_arm_groups"]["end_effector_transitions"] == []
    assert "T05" not in serialized
    assert "T11" not in serialized
    assert "energy tool is merely exchanged" in compact["exclude"]["P06"][0]
    assert "specimen is separated" in compact["cues"]["P07"][1]


def test_remaining_tool_use_includes_authored_phase_roles() -> None:
    spec = load_bundle(_spec_root() / "thyroidectomy_demo")

    assert set(spec.get_expected_instruments("P04")) == {"T02", "T03"}
    remaining = set(spec.get_remaining_expected_instruments("P03"))
    assert {"T04", "T07"}.issubset(remaining)
    assert {"T05", "T11"}.isdisjoint(remaining)
    assert "T01" not in remaining


def test_demo_compact_prompt_is_case_agnostic_and_forbids_time_shortcuts() -> None:
    compact = compact_procedure_prompt(_spec_root() / "thyroidectomy_demo")
    serialized = json.dumps(compact, ensure_ascii=False, sort_keys=True)

    assert compact["id"] == "thyroidectomy_demo_prompt_v4"
    assert compact["phase_policy"]["time_prior_role"] == "forbidden"
    assert compact["phase_policy"]["case_specific_timestamp_role"] == "forbidden"
    assert compact["phase_policy"]["degraded_mode_rule"].startswith(
        "Without a usable surgical-field image, do not separate P04 from P05."
    )
    assert compact["phase_policy"][
        "tool_only_detailed_phase_transition_allowed"
    ] is False
    assert compact["phase_policy"][
        "tool_sequence_open_set_anchor_allowed"
    ] is False
    assert "0704_" not in serialized
    assert "source_frame_idx" not in serialized
    assert "time_sec" not in serialized


def test_demo_prompt_encodes_cross_case_functional_handover_patterns() -> None:
    compact = compact_procedure_prompt(_spec_root() / "thyroidectomy_demo")

    assert [row[:2] for row in compact["seq"]["P03"]] == [
        ["T02", "T02"],
        ["T02", "T04"],
        ["T04", "T07"],
        ["T07", "T04"],
        ["T02", "T07"],
        ["T04", "T02"],
        ["T04", "T04"],
        ["T07", "T02"],
    ]
    assert [row[3] for row in compact["seq"]["P03"][:4]] == ["high"] * 4
    assert [row[:2] for row in compact["seq"]["P05"][:3]] == [
        ["T02", "T07"],
        ["T07", "T03"],
        ["T02", "T03"],
    ]
    assert compact["roles"]["P03"]["entry_handover"] == ["T02"]
    assert compact["handover_patterns"]["primary"] == [
        [
            "T02",
            "T02",
            "T04",
            "T07",
            "T04",
            "T02",
            "T07",
            "T07",
            "T04",
        ]
    ]
    assert compact["handover_patterns"]["alternatives"] == [
        ["T04", "T02"],
        ["T02", "T07", "T04"],
        ["T02", "T02", "T07"],
    ]


def _demo_prior() -> ProcedurePriorScorer:
    bundle_dir = _spec_root() / "thyroidectomy_demo"
    return ProcedurePriorScorer(
        load_bundle(bundle_dir),
        compact_procedure_prompt(bundle_dir),
    )


def _top_id(result: dict, key: str) -> str:
    ranked = result[key]
    return str(ranked[0][0]) if ranked else ""


def test_demo_prior_keeps_bed_arm_retraction_out_of_handover_ranking() -> None:
    result = _demo_prior().score({"current_phase": "P03"})

    assert _top_id(result, "tool") == "T02"


def test_demo_prior_preserves_repeated_same_tool_requests() -> None:
    scorer = _demo_prior()
    first = scorer.score(
        {
            "current_phase": "P03",
            "speech": [{"at": 8.0, "text": "Adson"}],
        }
    )
    second = scorer.score(
        {
            "current_phase": "P03",
            "speech": [
                {"at": 8.0, "text": "Adson"},
                {"at": 11.0, "text": "Adson one more"},
            ],
        }
    )

    assert _top_id(first, "tool") == "T02"
    assert _top_id(second, "tool") == "T04"


@pytest.mark.parametrize(
    ("history", "expected"),
    [
        ([], "T02"),
        (["T02"], "T02"),
        (["T02", "T02"], "T04"),
        (["T02", "T02", "T04"], "T07"),
        (["T02", "T02", "T04", "T07"], "T04"),
        (["T02", "T02", "T04", "T07", "T04"], "T02"),
    ],
)
def test_demo_prior_forecasts_next_handover_from_validated_request_suffix(
    history: list[str],
    expected: str,
) -> None:
    result = _demo_prior().score(
        {
            "current_phase": "P03",
            "tool_requests": [
                {"tool": tool_id, "at": float(index + 1)}
                for index, tool_id in enumerate(history)
            ],
        }
    )

    forecast = result["evidence"]["procedure_path_forecast"]
    assert forecast["tool"] == expected
    assert forecast["history"] == history
    assert forecast["confidence"] >= 0.85
    assert _top_id(result, "tool") == expected


def test_demo_prior_prefers_validated_requests_over_duplicate_completion_events() -> None:
    result = _demo_prior().score(
        {
            "current_phase": "P03",
            "tool_requests": ["T02", "T02", "T04"],
            "completed_handovers": ["T02", "T02", "T04", "T04"],
            "recent_tools": ["T02", "T02", "T04", "T04"],
        }
    )

    forecast = result["evidence"]["procedure_path_forecast"]
    assert forecast["history_source"] == "validated_requests"
    assert forecast["history"] == ["T02", "T02", "T04"]
    assert forecast["tool"] == "T07"


def test_demo_prior_does_not_advance_detailed_phase_from_tool_names_alone() -> None:
    scorer = _demo_prior()
    exposure = scorer.score(
        {
            "current_phase": "P03",
            "speech": [
                {"at": 78.0, "text": "Bovie"},
                {"at": 84.0, "text": "Bovie"},
            ],
        }
    )
    fine_dissection = scorer.score(
        {
            "current_phase": "P04",
            "speech": [{"at": 102.0, "text": "mosquito"}],
        }
    )
    vessel_control = scorer.score(
        {
            "current_phase": "P05",
            "speech": [{"at": 106.0, "text": "bipolar"}],
        }
    )

    assert _top_id(exposure, "phase") == "P03"
    assert _top_id(fine_dissection, "phase") == "P04"
    assert _top_id(vessel_control, "phase") == "P05"
    assert (
        exposure["evidence"]["tool_only_detailed_phase_transition_allowed"]
        is False
    )


def test_demo_prior_allows_explicit_public_speech_phase_advance_after_focal_control() -> None:
    result = _demo_prior().score(
        {
            "current_phase": "P05",
            "speech": [{"at": 106.0, "text": "P06"}],
        }
    )

    assert _top_id(result, "phase") == "P06"


def test_demo_open_set_prior_uses_public_tool_exchange_order() -> None:
    result = _demo_prior().score_open_set(
        {
            "speech": [
                {"text": "Adson"},
                {"text": "Adson 하나 더"},
                {"text": "Bovie"},
                {"text": "air suction"},
                {"text": "bipolar"},
            ],
        }
    )

    assert _top_id(result, "phase") == "P03"
    assert (
        result["evidence"]["tool_sequence_open_set_anchor_allowed"] is False
    )
    assert result["evidence"]["phase_search_mode"] == "open_set"


def test_demo_open_set_prior_prefers_longer_matching_prefix() -> None:
    result = _demo_prior().score_open_set(
        {
            "speech": [
                {"text": "Adson"},
                {"text": "Adson 하나 더"},
                {"text": "Bovie"},
            ],
        }
    )

    assert _top_id(result, "phase") == "P03"
    assert result["evidence"]["sequence_alignment"]["P03"] == {
        "matches": 3,
        "adjacent": 2,
    }


def test_demo_open_set_prior_waits_for_more_than_one_public_tool() -> None:
    result = _demo_prior().score_open_set(
        {"speech": [{"text": "Adson"}]}
    )

    assert result["phase"] == []
    assert result["tool"] == []


def test_demo_prompt_is_discovered_without_a_legacy_bundle() -> None:
    discovered = {
        path.name for path in discover_prompt_bundle_dirs(_spec_root())
    }

    assert "thyroidectomy_demo" in discovered
