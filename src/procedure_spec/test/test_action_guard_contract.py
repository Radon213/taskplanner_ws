from __future__ import annotations

from dataclasses import fields
from pathlib import Path

from procedure_spec import load_bundle
from procedure_spec.models import ActionGuardPolicy, ScenarioPolicySpec
from procedure_spec.prompt_bundle import _build_policy


def test_action_guard_contract_has_no_phase_uncertainty_handover_veto() -> None:
    policy = _build_policy()["action_guard"]

    assert set(policy) == {
        "require_multi_evidence_for_handover",
    }
    assert {field.name for field in fields(ActionGuardPolicy)} == {
        "require_multi_evidence_for_handover",
    }
    assert {field.name for field in fields(ScenarioPolicySpec)} == {
        "handover_arm",
        "recovery_arm",
        "require_cleaning_after_surgeon_use",
        "allow_anticipatory_hold",
        "voice_override_preempts_preposition",
        "allow_prepositioning_when_uncertain",
        "explicit_request_priority",
        "unused_preposition_destination",
        "runtime_requirements",
        "extensions",
    }


def test_prompt_derived_bundle_loads_without_obsolete_phase_handover_guard() -> None:
    spec_dir = (
        Path(__file__).parents[1]
        / "procedure_spec"
        / "specs"
        / "thyroidectomy_demo"
    )

    spec = load_bundle(spec_dir)

    assert spec.bundle.action_guard is not None
    assert spec.bundle.action_guard.require_multi_evidence_for_handover is True
    assert spec.bundle.scenario_policy is not None
    assert spec.bundle.scenario_policy.allow_prepositioning_when_uncertain is False
