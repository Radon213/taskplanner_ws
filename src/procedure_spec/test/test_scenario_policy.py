from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from procedure_spec import (
    PRESERVED_RUNTIME_INTERLOCKS,
    SCENARIO_RUNTIME_REQUIREMENT_KEYS,
    discover_prompt_bundle_dirs,
    load_bundle,
)
from procedure_spec.prompt_bundle import build_raw_bundle_from_prompt
from procedure_spec.validator import SpecValidationError, validate_raw_bundle


def _spec_root() -> Path:
    return Path(__file__).parents[1] / "procedure_spec" / "specs"


def _raw_demo_bundle() -> dict:
    root = _spec_root()
    display_catalog = yaml.safe_load(
        (root / "display_catalog.yaml").read_text(encoding="utf-8")
    )
    return build_raw_bundle_from_prompt(
        root / "thyroidectomy_demo",
        display_catalog,
    )


def test_scenario_policy_is_the_single_query_boundary_for_authored_choices() -> None:
    spec = load_bundle(_spec_root() / "thyroidectomy_demo")
    policy = spec.get_scenario_policy()

    assert policy.check_instrument_request("T02").allowed is True
    assert policy.check_instrument_request("T03").allowed is False
    assert policy.check_group_operation("retraction", "retraction").allowed is True
    assert policy.check_group_operation("retraction", "change_end_effector").allowed is False
    assert policy.check_group_voice_command("retraction", "change_tool").allowed is True
    assert policy.unused_preposition_destination == "mayo"
    runtime = spec.get_scenario_runtime_requirements()
    assert runtime.procedure_type == "thyroidectomy"
    assert runtime.tool_handover_action_required is True
    assert runtime.retraction_service_required is True
    assert runtime.rfdetr_tool_observations_required is True
    assert runtime.retraction_workflow_state_enforced is False


def test_voice_retraction_only_demo_changes_capabilities_in_yaml_not_guards() -> None:
    spec = load_bundle(_spec_root() / "inguinal_hernia_repair_demo")
    policy = spec.get_scenario_policy()

    assert policy.requestable_instrument_ids == frozenset()
    assert policy.check_group_voice_command("retraction", "stop_retraction").allowed
    denied = policy.check_group_voice_command("retraction", "change_tool")
    assert denied.allowed is False
    assert "scenario policy" in denied.reason
    assert policy.spec.allow_anticipatory_hold is False
    assert policy.unused_preposition_destination == "retain"
    runtime = spec.get_scenario_runtime_requirements()
    assert runtime.procedure_type == "inguinal_hernia_repair"
    assert runtime.tool_handover_action_required is False
    assert runtime.retraction_service_required is True
    assert runtime.image_vlm_enabled is False
    assert runtime.perception_enabled is False
    assert runtime.voice_intent_resolver_enabled is True
    assert runtime.retraction_workflow_state_enforced is False


@pytest.mark.parametrize(
    ("bundle_name", "procedure_type", "retraction_required"),
    (
        ("thyroidectomy", "thyroidectomy", True),
        ("nephrectomy", "nephrectomy", True),
        ("inguinal_hernia_repair", "", False),
    ),
)
def test_packaged_runtime_requirements_are_authored_in_each_bundle(
    bundle_name: str,
    procedure_type: str,
    retraction_required: bool,
) -> None:
    bundle_dir = _spec_root() / bundle_name
    prompt = yaml.safe_load(
        (bundle_dir / "vlm_procedure_prompt.yaml").read_text(encoding="utf-8")
    )
    authored_runtime = prompt["scenario_policy"]["runtime_requirements"]
    runtime = load_bundle(bundle_dir).get_scenario_runtime_requirements()

    assert set(authored_runtime) == SCENARIO_RUNTIME_REQUIREMENT_KEYS
    assert runtime.procedure_type == procedure_type
    assert runtime.retraction_service_required is retraction_required
    assert runtime.retraction_workflow_state_enforced is True


def test_every_packaged_prompt_authors_policy_and_tool_placement() -> None:
    for bundle_dir in discover_prompt_bundle_dirs(_spec_root()):
        prompt = yaml.safe_load(
            (bundle_dir / "vlm_procedure_prompt.yaml").read_text(encoding="utf-8")
        )
        tool_ids = list(prompt["tools"])
        assert set(prompt["scenario_policy"]["runtime_requirements"]) == (
            SCENARIO_RUNTIME_REQUIREMENT_KEYS
        )
        rack_order = prompt["tool_placement"]["rack_order"]
        assert len(rack_order) == len(tool_ids)
        assert set(rack_order) == set(tool_ids)
        assert isinstance(prompt["tool_placement"]["initial_states"], list)


def test_mutable_scenario_destination_is_not_validated_as_a_safety_invariant() -> None:
    raw = _raw_demo_bundle()

    raw["policy"]["scenario_policy"]["unused_preposition_destination"] = "rack"
    validate_raw_bundle(raw)


def test_runtime_requirement_contract_is_complete_and_typed() -> None:
    raw = _raw_demo_bundle()
    runtime = raw["policy"]["scenario_policy"]["runtime_requirements"]
    runtime.pop("perception_enabled")

    with pytest.raises(SpecValidationError, match="complete contract"):
        validate_raw_bundle(raw)

    raw = _raw_demo_bundle()
    raw["policy"]["scenario_policy"]["runtime_requirements"][
        "phase_inference_enabled"
    ] = "false"
    with pytest.raises(SpecValidationError, match="must be boolean"):
        validate_raw_bundle(raw)


def test_admission_interlock_remains_fail_closed() -> None:
    raw = _raw_demo_bundle()
    raw["policy"]["action_guard"]["require_multi_evidence_for_handover"] = False

    with pytest.raises(SpecValidationError, match="must remain true"):
        validate_raw_bundle(raw)

    assert PRESERVED_RUNTIME_INTERLOCKS == {
        "controller_admission",
        "service_admission",
        "execution_state",
        "stopped_state_route_change",
        "preflight_ack",
        "freshness",
        "idempotency",
    }
