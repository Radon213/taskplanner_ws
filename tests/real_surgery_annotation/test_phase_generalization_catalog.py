from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[2]
ANNOTATION_ROOT = ROOT / "annotations/observable_tool_events"
CATALOG_PATH = (
    ANNOTATION_ROOT / "procedure_phases.cross_case_provisional.v3.yaml"
)
MIGRATION_PATH = ANNOTATION_ROOT / "phase_ontology_migration.v2_to_v3.json"
PROMPT_PATH = (
    ROOT
    / "src/procedure_spec/procedure_spec/specs/thyroidectomy_demo/"
    "vlm_procedure_prompt.yaml"
)


def _load_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _collect_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            keys.add(str(key))
            keys.update(_collect_keys(child))
    elif isinstance(value, list):
        for child in value:
            keys.update(_collect_keys(child))
    return keys


def test_v3_catalog_is_development_only_and_has_no_case_boundaries() -> None:
    catalog = _load_yaml(CATALOG_PATH)
    expected_cases = [f"0704_{index}" for index in range(6, 18)]

    assert catalog["schema"] == "taskplanner.demo_procedure_phase_catalog.v3"
    assert catalog["runtime_status"] == (
        "evaluation_only_provisional_not_frozen"
    )
    assert catalog["development_scope"] == {
        "calibration_cases": expected_cases,
        "case_count": len(expected_cases),
        "held_out_eligible": False,
        "reason": (
            "All twelve videos were used to refine the ontology and boundary "
            "rules. Generalization must be measured on future videos that did "
            "not influence this catalog, prompt, or reference boundaries.\n"
        ),
        "case_timestamps_in_this_catalog": False,
        "case_phase_accuracy_enabled": False,
    }
    assert catalog["information_boundary"][
        "ground_truth_runtime_input_allowed"
    ] is False
    assert catalog["information_boundary"][
        "case_specific_phase_events_runtime_input_allowed"
    ] is False
    assert catalog["information_boundary"]["catalog_runtime_input_allowed"] is False
    assert "case number" in catalog["optimization_objective"]["anti_overfit"]
    assert "normalized clip time" in catalog["optimization_objective"][
        "anti_overfit"
    ]
    assert {"time_sec", "source_frame_idx", "boundary_frame"}.isdisjoint(
        _collect_keys(catalog)
    )


def test_v3_catalog_keeps_visual_states_detailed_and_degrades_safely() -> None:
    catalog = _load_yaml(CATALOG_PATH)
    granularity = catalog["granularity_policy"]
    assert granularity["full_multimodal"]["detailed_phases"] == [
        "P03",
        "P04",
        "P05",
        "P06",
    ]
    assert granularity["degraded_without_surgical_field_image"][
        "detailed_phase_claim_allowed"
    ] is False
    assert granularity["degraded_without_surgical_field_image"][
        "coarse_groups"
    ] == {
        "M01": ["P03"],
        "M02": ["P04", "P05"],
        "M03": ["P06"],
    }
    assert {
        row["group_id"]: row["members"]
        for row in catalog["coarse_phase_groups"]
    } == {
        "M01": ["P03"],
        "M02": ["P04", "P05"],
        "M03": ["P06"],
    }

    phase_by_id = {row["phase_id"]: row for row in catalog["phases"]}
    for phase_id in ("P03", "P04", "P05", "P06"):
        phase = phase_by_id[phase_id]
        assert phase["definition_source"] == "cross_case_video_observed"
        assert phase["observed_in_current_calibration_clips"] is True
        assert phase["positive_cues"]
        assert phase["negative_cues"]
        assert phase["tool_role_examples"]
        assert phase["boundary_rule"]

    assert "P10" not in catalog["phase_order"]
    assert "P10" not in phase_by_id
    assert "nonclinical workflow tail" in catalog["assignment_rules"]["cleanup"]
    assert any(
        "broad Allis traction" in cue
        for cue in phase_by_id["P06"]["negative_cues"]
    )
    assert "future energy event" in phase_by_id["P06"]["boundary_rule"]


def test_v3_migration_hashes_and_semantic_mapping_are_current() -> None:
    migration = json.loads(MIGRATION_PATH.read_text(encoding="utf-8"))
    source_path = ANNOTATION_ROOT / migration["source_catalog"]["file"]
    target_path = ANNOTATION_ROOT / migration["target_catalog"]["file"]

    assert migration["source_catalog"]["sha256"] == _sha256(source_path)
    assert migration["target_catalog"]["sha256"] == _sha256(target_path)
    assert migration["scope"]["calibration_cases"] == [
        f"0704_{index}" for index in range(6, 18)
    ]
    assert migration["scope"]["held_out_eligible"] is False
    assert [
        (row["source_phase_id"], row["target_phase_id"])
        for row in migration["id_mapping"]
    ] == [(f"P{index:02d}", f"P{index:02d}") for index in range(1, 10)]
    assert migration["nonclinical_workflow_state"] == {
        "phase_id": "P10",
        "target_role": "runtime_workflow_terminal_only",
        "clinical_phase_reference_allowed": False,
        "reason": (
            "Instrument clearance is cleanup, not a distinct clinical "
            "resection Phase."
        ),
    }
    assert migration["scoring_policy"]["phase_accuracy_enabled"] is False


def test_case_agnostic_v4_prompt_matches_v3_granularity_contract() -> None:
    catalog = _load_yaml(CATALOG_PATH)
    prompt = _load_yaml(PROMPT_PATH)
    serialized_prompt = json.dumps(
        prompt,
        ensure_ascii=False,
        sort_keys=True,
    )

    assert prompt["id"] == "thyroidectomy_demo_prompt_v4"
    assert prompt["phase_inference_policy"]["time_prior_role"] == "forbidden"
    assert prompt["phase_inference_policy"][
        "case_specific_timestamp_role"
    ] == "forbidden"
    assert prompt["phase_inference_policy"][
        "tool_only_detailed_phase_transition_allowed"
    ] is False
    assert prompt["phase_inference_policy"][
        "tool_sequence_open_set_anchor_allowed"
    ] is False
    assert prompt["phase_groups"] == {
        row["group_id"]: {
            key: value
            for key, value in row.items()
            if key != "group_id"
        }
        for row in catalog["coarse_phase_groups"]
    } | {
        "M02": {
            "name": "retraction_supported_target_work",
            "name_ko": "고정 견인 하 표적 조직 작업",
            "members": ["P04", "P05"],
            "use_when": (
                "surgical-field image cannot distinguish exposure "
                "establishment from target manipulation"
            ),
        }
    }
    assert "0704_" not in serialized_prompt
    assert "source_frame_idx" not in serialized_prompt
    assert "time_sec" not in serialized_prompt
