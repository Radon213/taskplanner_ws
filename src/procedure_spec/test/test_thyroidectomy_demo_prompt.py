from __future__ import annotations

import json
from pathlib import Path

import pytest

from procedure_spec import (
    ProcedurePriorScorer,
    compact_procedure_prompt,
    discover_prompt_bundle_dirs,
    load_bundle,
)


def _spec_root() -> Path:
    return Path(__file__).parents[1] / "procedure_spec" / "specs"


def test_demo_prompt_loads_as_video_derived_thyroidectomy_bundle() -> None:
    bundle_dir = _spec_root() / "thyroidectomy_demo"
    spec = load_bundle(bundle_dir)

    assert spec.procedure_id == "thyroidectomy_demo"
    assert spec.bundle.procedure_display_name_ko == "갑상선절제술(시연)"
    assert spec.normal_phase_ids == [f"P{index:02d}" for index in range(1, 11)]
    assert spec.interrupt_phase_ids == []
    assert spec.default_phase_id == "P03"
    assert spec.get_allowed_next_phases("P01") == ["P02"]
    assert spec.get_allowed_next_phases("P03") == ["P04"]
    assert spec.get_allowed_next_phases("P09") == ["P10"]
    assert spec.get_allowed_next_phases("P10") == []
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
    assert load_bundle(_spec_root() / "thyroidectomy").default_phase_id == "P01"


def test_demo_prompt_contains_confirmed_demo_inventory() -> None:
    spec = load_bundle(_spec_root() / "thyroidectomy_demo")

    assert spec.list_instrument_ids() == [
        "T01",
        "T02",
        "T03",
        "T04",
        "T05",
        "T07",
        "T08",
        "T11",
    ]
    assert spec.get_tool_inventory() == {
        "T01": 1,
        "T02": 2,
        "T03": 2,
        "T04": 1,
        "T05": 2,
        "T07": 1,
        "T08": 1,
        "T11": 2,
    }
    assert sum(spec.get_tool_inventory().values()) == 12
    assert spec.resolve_instrument_alias("Yankauer suction") is None
    assert spec.resolve_instrument_alias("thyroid retractor") == "T11"
    assert spec.resolve_instrument_alias("Middeldorpf retractor") == "T11"
    assert spec.resolve_instrument_alias("갑상선 리트랙터") == "T11"
    assert (
        spec.bundle.instruments[-1].display_name_ko
        == "갑상선 리트랙터(미들돌프)"
    )
    assert [
        (
            state.instance_id,
            state.location_id,
            state.lifecycle_stage,
        )
        for state in spec.get_initial_instrument_states()
    ] == [
        ("T03#1", "field_region_procedure", "surgeon_owned"),
        ("T03#2", "field_region_procedure", "surgeon_owned"),
    ]


def test_demo_prompt_keeps_retractor_and_vessel_control_patterns() -> None:
    compact = compact_procedure_prompt(_spec_root() / "thyroidectomy_demo")

    assert [
        "P03",
        "P04",
        (
            "T05 is common, but any visibly equivalent fixed retractor can "
            "support this transition"
        ),
    ] in compact["flow"]
    assert all(row[0] != "*" for row in compact["flow"])
    assert compact["seq"]["P04"] == [
        [
            "T05",
            "T05",
            "a second fixed retractor is common but not required",
            "high",
        ],
        [
            "T05",
            "T02",
            "fine target handling may follow once exposure is stable",
            "high",
        ],
        [
            "T02",
            "T02",
            "a second fine forceps may reinforce traction after fixed exposure",
            "medium",
        ],
        [
            "T05",
            "T03",
            "Allis can substitute when firmer target handling is needed",
            "medium",
        ],
        [
            "T11",
            "T02",
            "fine target handling may follow an equivalent fixed thyroid retractor",
            "medium",
        ],
    ]
    assert compact["seq"]["P06"][:2] == [
        [
            "T08",
            "T07",
            "bipolar is one treatment option for the controlled point",
            "high",
        ],
        [
            "T07",
            "T04",
            "broader treatment commonly follows precise energy when further division is needed",
            "high",
        ],
    ]
    assert compact["cues"]["P03"][0].startswith(
        "an open central neck field before stable bilateral"
    )
    assert compact["phase_policy"]["tool_order_role"].startswith(
        "Public voice, handover, and tool-recognition order are supportive"
    )
    assert compact["phase_groups"]["M02"]["members"] == ["P04", "P05"]
    assert compact["roles"]["P06"] == {
        "focal_control": ["T08", "T03"],
        "localized_treatment_alternatives": ["T07", "T04"],
    }
    assert "energy tool is merely exchanged" in compact["exclude"]["P06"][0]
    assert "specimen is separated" in compact["cues"]["P07"][1]


def test_remaining_tool_use_includes_authored_phase_roles() -> None:
    spec = load_bundle(_spec_root() / "thyroidectomy_demo")

    assert "T11" in spec.get_expected_instruments("P04")
    remaining = set(spec.get_remaining_expected_instruments("P03"))
    assert {"T04", "T07", "T11"}.issubset(remaining)
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

    assert [row[:2] for row in compact["seq"]["P03"][:7]] == [
        ["T02", "T02"],
        ["T02", "T04"],
        ["T04", "T07"],
        ["T07", "T04"],
        ["T04", "T05"],
        ["T05", "T05"],
        ["T02", "T05"],
    ]
    assert [row[3] for row in compact["seq"]["P03"][:6]] == ["high"] * 6
    assert [row[:2] for row in compact["seq"]["P05"][:3]] == [
        ["T02", "T07"],
        ["T07", "T08"],
        ["T02", "T08"],
    ]
    assert compact["roles"]["P03"]["upcoming_fixed_retraction"] == [
        "T05",
        "T11",
    ]
    assert compact["roles"]["P03"]["entry_handover"] == ["T02"]
    assert compact["handover_patterns"]["primary"] == [
        [
            "T02",
            "T02",
            "T04",
            "T07",
            "T04",
            "T05",
            "T05",
            "T02",
            "T07",
            "T08",
            "T07",
            "T04",
        ]
    ]
    assert compact["handover_patterns"]["alternatives"] == [
        ["T04", "T02", "T05"],
        ["T02", "T08", "T07", "T04"],
        ["T05", "T05", "T02", "T02", "T07"],
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


def test_demo_prior_treats_existing_thyroid_retractor_as_context() -> None:
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
        (["T02", "T02", "T04", "T07", "T04"], "T05"),
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
                {"at": 78.0, "text": "army"},
                {"at": 84.0, "text": "army"},
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


def test_demo_prior_allows_explicit_visual_phase_advance_after_focal_control() -> None:
    result = _demo_prior().score(
        {
            "current_phase": "P05",
            "observed_signals": [
                {
                    "type": "advance_phase_cue",
                    "at": 106.0,
                    "speech": "P06",
                }
            ],
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
