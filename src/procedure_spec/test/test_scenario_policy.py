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
from procedure_spec.prompt_bundle import (
    _prompt_scenario_policy,
    build_raw_bundle_from_prompt,
)
from procedure_spec.validator import SpecValidationError, validate_raw_bundle


def _spec_root() -> Path:
    return Path(__file__).parents[1] / "procedure_spec" / "specs"


def _raw_demo_bundle() -> dict:
    root = _spec_root()
    display_catalog = yaml.safe_load(
        (root / "display_catalog.yaml").read_text(encoding="utf-8")
    )
    return build_raw_bundle_from_prompt(root / "thyroidectomy_demo", display_catalog)


def test_scenario_policy_keeps_only_scenario_owned_choices() -> None:
    spec = load_bundle(_spec_root() / "thyroidectomy_demo")
    policy = spec.get_scenario_policy()

    assert policy.check_instrument_request("T02").allowed
    assert not policy.check_instrument_request("T03").allowed
    assert policy.check_group_operation("retraction", "retraction").allowed
    assert not policy.check_group_operation(
        "retraction", "change_end_effector"
    ).allowed
    assert policy.check_group_voice_command("retraction", "change_tool").allowed
    assert policy.unused_preposition_destination == "mayo"
    assert policy.completion_cleanup_excluded_tools() == frozenset({"T04", "T07"})

    runtime = spec.get_scenario_runtime_requirements()
    assert runtime.procedure_type == "thyroidectomy"
    assert runtime.tool_handover_action_required
    assert runtime.retraction_service_required
    assert runtime.rfdetr_tool_observations_required
    assert not runtime.retraction_workflow_state_enforced

    # Command discovery and admission live in the command owner, not a
    # procedure bundle or its scenario policy facade.
    assert not hasattr(policy, "enabled_voice_command_ids")
    assert not hasattr(policy, "check_voice_command_id")
    assert not hasattr(policy, "check_voice_command")
    assert not hasattr(policy.spec, "allowed_voice_command_ids")
    assert not hasattr(policy.spec, "voice_command_policy_version")


def test_retired_voice_command_fields_are_unvalidated_extensions() -> None:
    retired = {
        "voice_command_policy_version": False,
        "allowed_voice_command_ids": {"not": "a command list"},
    }

    parsed = _prompt_scenario_policy({"scenario_policy": retired})

    assert "voice_command_policy_version" not in parsed
    assert "allowed_voice_command_ids" not in parsed
    assert parsed["extensions"] == retired

    raw = _raw_demo_bundle()
    raw["policy"]["scenario_policy"].update(retired)
    validate_raw_bundle(raw)


def test_retraction_demo_keeps_per_scenario_retraction_vocabulary() -> None:
    spec = load_bundle(_spec_root() / "inguinal_hernia_repair_demo")
    policy = spec.get_scenario_policy()

    assert policy.requestable_instrument_ids == frozenset()
    assert policy.check_group_voice_command("retraction", "stop_retraction").allowed
    denied = policy.check_group_voice_command("retraction", "change_tool")
    assert not denied.allowed
    assert "scenario policy" in denied.reason
    assert not policy.spec.allow_anticipatory_hold
    assert policy.unused_preposition_destination == "retain"

    runtime = spec.get_scenario_runtime_requirements()
    assert runtime.procedure_type == "inguinal_hernia_repair"
    assert not runtime.tool_handover_action_required
    assert runtime.retraction_service_required
    assert not runtime.image_vlm_enabled
    assert not runtime.perception_enabled
    assert runtime.voice_intent_resolver_enabled
    assert not runtime.retraction_workflow_state_enforced


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
    assert runtime.retraction_workflow_state_enforced


def test_every_packaged_prompt_omits_static_command_policy() -> None:
    for bundle_dir in discover_prompt_bundle_dirs(_spec_root()):
        prompt = yaml.safe_load(
            (bundle_dir / "vlm_procedure_prompt.yaml").read_text(encoding="utf-8")
        )
        scenario_policy = prompt["scenario_policy"]
        tool_ids = list(prompt["tools"])

        assert set(scenario_policy["runtime_requirements"]) == (
            SCENARIO_RUNTIME_REQUIREMENT_KEYS
        )
        assert "voice_command_policy_version" not in scenario_policy
        assert "allowed_voice_command_ids" not in scenario_policy
        rack_order = prompt["tool_placement"]["rack_order"]
        assert len(rack_order) == len(tool_ids)
        assert set(rack_order) == set(tool_ids)
        assert isinstance(prompt["tool_placement"]["initial_states"], list)


def test_mutable_scenario_destination_is_not_validated_as_a_safety_invariant() -> None:
    raw = _raw_demo_bundle()
    raw["policy"]["scenario_policy"]["unused_preposition_destination"] = "rack"

    validate_raw_bundle(raw)


def test_runtime_requirement_preferences_can_be_partial_and_remain_typed() -> None:
    raw = _raw_demo_bundle()
    runtime = raw["policy"]["scenario_policy"]["runtime_requirements"]
    runtime.pop("perception_enabled")
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
        "idempotency",
    }
