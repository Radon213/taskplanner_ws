from __future__ import annotations

import pytest

from surgical_interop_execution.controller_contract import (
    EIR_NUC_CAPABILITY_POLICY_ID,
    EIR_NUC_VIRTUAL_CONTRACT_ID,
    TOOL_HANDOVER_ACTION_ABI_FINGERPRINT,
    VIRTUAL_EMULATOR_CAPABILITY_POLICY_ID,
    build_controller_contract,
    controller_contract_mismatches,
    validate_source_stamp,
)


def _virtual_contract() -> dict[str, object]:
    return build_controller_contract(
        contract_id=EIR_NUC_VIRTUAL_CONTRACT_ID,
        endpoint_source="virtual",
        execution_mode="virtual",
        tool_handover_endpoint="/integration/virtual/surgery/tool_handover",
        retraction_service_name="/integration/virtual/surgery/retraction/command",
        capability_policy_id=EIR_NUC_CAPABILITY_POLICY_ID,
        stamp_sec=1.0,
    )


def _mismatches(payload: object, **overrides) -> tuple[str, ...]:
    values = {
        "expected_contract_id": EIR_NUC_VIRTUAL_CONTRACT_ID,
        "expected_endpoint_source": "virtual",
        "expected_execution_mode": "virtual",
        "expected_tool_handover_endpoint": "/integration/virtual/surgery/tool_handover",
        "expected_retraction_service_name": "/integration/virtual/surgery/retraction/command",
        "expected_capability_policy_id": EIR_NUC_CAPABILITY_POLICY_ID,
        "require_tool_handover": True,
        "require_retraction_service": True,
        "require_retraction_profile_identity": False,
        "required_tool_instances": (("Bovie surgical cautery", "Bovie surgical cautery#1"),),
    }
    values.update(overrides)
    return controller_contract_mismatches(payload, **values)


def test_exact_virtual_contract_matches_before_any_goal_is_sent() -> None:
    assert _mismatches(_virtual_contract()) == ()


def test_action_abi_mismatch_fails_closed_even_when_endpoint_names_match() -> None:
    payload = _virtual_contract()
    tool = dict(payload["tool_handover"])
    tool["abi_fingerprint"] = "sha256:stale-eir-action"
    payload["tool_handover"] = tool

    assert "tool_handover_abi_mismatch" in _mismatches(payload)
    assert TOOL_HANDOVER_ACTION_ABI_FINGERPRINT != "sha256:stale-eir-action"


def test_eir_contract_rejects_procedure_tool_outside_partner_policy() -> None:
    mismatches = _mismatches(
        _virtual_contract(),
        required_tool_instances=(
            ("Bovie surgical cautery", "Bovie surgical cautery#1"),
            ("Allis clamp forceps", "Allis clamp forceps#1"),
        ),
    )

    assert any(
        item.startswith("tool_handover_capability_missing:Allis clamp forceps")
        for item in mismatches
    )


def test_virtual_contract_supports_demo_inventory_without_widening_eir_policy() -> None:
    payload = build_controller_contract(
        contract_id=EIR_NUC_VIRTUAL_CONTRACT_ID,
        endpoint_source="virtual",
        execution_mode="virtual",
        tool_handover_endpoint="/integration/virtual/surgery/tool_handover",
        retraction_service_name="/integration/virtual/surgery/retraction/command",
        capability_policy_id=VIRTUAL_EMULATOR_CAPABILITY_POLICY_ID,
        stamp_sec=1.0,
    )

    assert _mismatches(
        payload,
        expected_capability_policy_id=VIRTUAL_EMULATOR_CAPABILITY_POLICY_ID,
        required_tool_instances=(
            ("#15 Scalpel", "#15 Scalpel#1"),
            ("Allis clamp forceps", "Allis clamp forceps#1"),
            ("Harmonic shears", "Harmonic shears#1"),
            ("Senn-Miller retractor", "Senn-Miller retractor#1"),
            ("Yankauer suction", "Yankauer suction#1"),
            ("Army navy retractor", "Army navy retractor#1"),
        ),
    ) == ()


def test_v1_retraction_contract_cannot_claim_profile_identity_support() -> None:
    assert "retraction_profile_identity_unsupported" in _mismatches(
        _virtual_contract(),
        require_retraction_profile_identity=True,
    )


def test_external_v1_retraction_requires_explicit_physical_stop_contract() -> None:
    assert "retraction_physical_stop_confirmation_unavailable" in _mismatches(
        _virtual_contract(),
        require_physical_stop_confirmation=True,
    )


def test_source_stamp_accepts_subsecond_progress_and_rejects_replay() -> None:
    accepted, reason = validate_source_stamp(
        {"stamp_sec": 100, "stamp_nanosec": 100},
        now_sec=100.1,
        max_age_sec=3.0,
        future_tolerance_sec=0.5,
        source_name="controller_contract",
    )

    assert accepted == pytest.approx(100.0000001)
    assert reason == ""
    replayed, replay_reason = validate_source_stamp(
        {"stamp_sec": 100, "stamp_nanosec": 100},
        now_sec=100.2,
        max_age_sec=3.0,
        future_tolerance_sec=0.5,
        source_name="controller_contract",
        previous_stamp_sec=accepted,
    )
    assert replayed == accepted
    assert replay_reason == "controller_contract_source_stamp_not_monotonic"
