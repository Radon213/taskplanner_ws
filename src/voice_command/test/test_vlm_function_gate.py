from __future__ import annotations

from dataclasses import replace
from itertools import permutations
import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from voice_command.vlm_function_gate import (
    ADJUST_RETRACTION_FUNCTION,
    HANDOVER_FUNCTION,
    DurableAdmission,
    FunctionGateLedger,
    GatewayScope,
    LedgerCollisionError,
    ProposalFact,
    ReplyFact,
    SpeechFact,
    VLMFunctionAdmissionGate,
    VLMFunctionAdmissionNode,
    _first_positive_stamp_ns,
    _json_load_object,
)


TEXT = "바이폴라 전달해 주세요"


def scope(
    *,
    gateway: str = "gateway-a",
    run: str = "run-1",
    procedure: str = "thyroidectomy_demo",
    catalog: str = "catalog-v1",
    stamp_ns: int = 1_000_000_000,
) -> GatewayScope:
    return GatewayScope(
        gateway_instance_id=gateway,
        procedure_run_id=run,
        procedure_type=procedure,
        catalog_version=catalog,
        source_stamp_ns=stamp_ns,
    )


def ledger_at(root: Path, name: str = "gate") -> FunctionGateLedger:
    directory = root / name
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    return FunctionGateLedger(directory / "admissions.sqlite3")


def proposal_payload(
    active_scope: GatewayScope,
    *,
    utterance_id: str = "utt-1",
    command: str = "handover",
    disposition: str = "propose",
    requires_confirmation: bool = False,
) -> dict[str, object]:
    is_adjust = command == "adjust"
    return {
        "header": {
            "stamp_sec": 1,
            "stamp_nanosec": 1,
            "frame_id": "",
        },
        "utterance_id": utterance_id,
        # Resolver-only proposals do not claim a gateway scope or function ID.
        "gateway_instance_id": "",
        "procedure_run_id": "",
        "function_request_id": "",
        "source": "operational_asr",
        "source_is_final": True,
        "source_speaker_role": "surgeon",
        "source_has_confidence": True,
        "source_confidence": 0.98,
        "raw_text": TEXT,
        "normalized_text": TEXT,
        "procedure_id": active_scope.procedure_type,
        "catalog_id": "voice-catalog-v1",
        "intent": "retractor_command" if is_adjust else "tool_handover",
        "tool_id": "" if is_adjust else "T04",
        "retractor_command": "adjust_retraction" if is_adjust else "",
        "target_side": "right" if is_adjust else "none",
        "distance_m": 0.005 if is_adjust else 0.0,
        "urgency": "routine",
        "provenance": "exact_alias",
        "requires_confirmation": requires_confirmation,
        "disposition": disposition,
        "reason": "grounded",
        "evidence_spans": ["T04"] if not is_adjust else ["right", "5mm"],
    }


def reply_payload(
    active_scope: GatewayScope,
    *,
    utterance_id: str = "utt-1",
    function_name: str = HANDOVER_FUNCTION,
    arguments: object | None = None,
    arguments_json: str | None = None,
    timing: str = "on_function_accepted",
    reply_id: str = "reply-gateway-a-1",
    function_request_id: str = "function-gateway-a-1",
) -> dict[str, object]:
    if arguments is None:
        arguments = {"tool_id": "T04"}
    if arguments_json is None:
        arguments_json = json.dumps(arguments, separators=(",", ":"))
    return {
        "stamp_sec": 1,
        "stamp_nanosec": 2,
        "source": "real_vlm",
        "source_epoch": 1,
        "source_sequence": 1,
        "correlation_id": "corr-1",
        "schema_version": "schema-v6",
        "gateway_instance_id": active_scope.gateway_instance_id,
        "procedure_run_id": active_scope.procedure_run_id,
        "utterance_id": utterance_id,
        "turn_id": "turn-gateway-a-1",
        "reply_id": reply_id,
        "text": "바이폴라를 전달하겠습니다.",
        "kind": "acknowledgement",
        "timing": timing,
        "speak": True,
        "function_call_name": function_name,
        "function_arguments_json": arguments_json,
        "function_request_id": function_request_id,
        "valid": True,
        "validation_error": "",
    }


def facts(
    active_scope: GatewayScope,
    *,
    utterance_id: str = "utt-1",
    command: str = "handover",
    reply_overrides: dict[str, object] | None = None,
    proposal_overrides: dict[str, object] | None = None,
) -> tuple[SpeechFact, ProposalFact, ReplyFact]:
    source_stamp_ns = active_scope.source_stamp_ns + 1
    proposal = proposal_payload(
        active_scope,
        utterance_id=utterance_id,
        command=command,
    )
    if proposal_overrides:
        proposal.update(proposal_overrides)
    if command == "adjust":
        reply = reply_payload(
            active_scope,
            utterance_id=utterance_id,
            function_name=ADJUST_RETRACTION_FUNCTION,
            arguments={
                "command": "adjust_retraction",
                "target_side": "right",
                "distance_m": 0.005,
            },
        )
    else:
        reply = reply_payload(active_scope, utterance_id=utterance_id)
    if reply_overrides:
        reply.update(reply_overrides)
    return (
        SpeechFact(
            scope=active_scope,
            utterance_id=utterance_id,
            source_stamp_ns=source_stamp_ns,
            source="operational_asr",
            is_final=True,
            speaker_role="surgeon",
            text=TEXT,
            has_confidence=True,
            confidence=0.98,
        ),
        ProposalFact(
            scope=active_scope,
            utterance_id=utterance_id,
            source_stamp_ns=source_stamp_ns,
            payload=proposal,
        ),
        ReplyFact(
            gateway_instance_id=active_scope.gateway_instance_id,
            procedure_run_id=active_scope.procedure_run_id,
            utterance_id=utterance_id,
            source_stamp_ns=source_stamp_ns + 1,
            received_at_ns=source_stamp_ns + 1,
            payload=reply,
        ),
    )


def observe_all(
    gate: VLMFunctionAdmissionGate,
    all_facts: tuple[SpeechFact, ProposalFact, ReplyFact],
) -> object:
    decision = gate.observe_speech(all_facts[0])
    assert decision is None
    decision = gate.observe_proposal(all_facts[1])
    assert decision is None
    return gate.observe_reply(all_facts[2])


@pytest.mark.parametrize("order", list(permutations((0, 1, 2))))
def test_exact_handover_join_is_order_independent_and_durable(
    tmp_path: Path,
    order: tuple[int, int, int],
) -> None:
    active_scope = scope()
    ledger = ledger_at(tmp_path, "order-" + "".join(map(str, order)))
    gate = VLMFunctionAdmissionGate(ledger)
    gate.activate_scope(active_scope)
    all_facts = facts(active_scope)
    observers = (
        gate.observe_speech,
        gate.observe_proposal,
        gate.observe_reply,
    )

    decision = None
    for index in order:
        decision = observers[index](all_facts[index])

    assert decision is not None and decision.admitted
    admission = decision.admission
    assert admission is not None
    assert admission.scope == active_scope.identity
    assert admission.intent_payload()["gateway_instance_id"] == "gateway-a"
    assert admission.intent_payload()["procedure_run_id"] == "run-1"
    assert admission.intent_payload()["function_request_id"] == (
        "function-gateway-a-1"
    )
    assert ledger.pending_replies(active_scope) == [admission]
    assert ledger.pending_intents(active_scope) == [admission]
    # Exact redelivery does not create a second decision or durable row.
    assert gate.observe_reply(all_facts[2]) is None
    assert gate.observe_reply(
        replace(
            all_facts[2],
            received_at_ns=all_facts[2].received_at_ns + 5_000_000_000,
        )
    ) is None
    ledger.close()


def test_exact_adjust_retraction_arguments_admit(tmp_path: Path) -> None:
    active_scope = scope()
    ledger = ledger_at(tmp_path)
    gate = VLMFunctionAdmissionGate(ledger)
    gate.activate_scope(active_scope)

    decision = observe_all(gate, facts(active_scope, command="adjust"))

    assert decision is not None and decision.admitted
    assert decision.admission is not None
    assert decision.admission.function_call_name == ADJUST_RETRACTION_FUNCTION
    ledger.close()


@pytest.mark.parametrize(
    ("reply_overrides", "proposal_overrides", "reason"),
    [
        (
            {"function_arguments_json": '{"tool_id":"T04","extra":1}'},
            {},
            "handover_arguments_not_exact",
        ),
        (
            {"function_arguments_json": '{"tool_id":"T99"}'},
            {},
            "handover_tool_mismatch",
        ),
        (
            {"function_arguments_json": '{"tool_id":NaN}'},
            {},
            "function_arguments_invalid_json",
        ),
        (
            {"timing": "immediate"},
            {},
            "function_reply_timing_invalid",
        ),
        (
            {},
            {"requires_confirmation": True},
            "proposal_not_executable",
        ),
        (
            {"function_call_name": "unknown_function"},
            {},
            "unsupported_function_name",
        ),
        (
            {},
            {"gateway_instance_id": "gateway-old"},
            "resolver_proposal_claims_gateway_scope",
        ),
        (
            {},
            {"procedure_run_id": "run-old"},
            "resolver_proposal_claims_procedure_run",
        ),
    ],
)
def test_handover_join_rejects_nonexact_or_unscoped_contracts(
    tmp_path: Path,
    reply_overrides: dict[str, object],
    proposal_overrides: dict[str, object],
    reason: str,
) -> None:
    active_scope = scope()
    ledger = ledger_at(tmp_path)
    gate = VLMFunctionAdmissionGate(ledger)
    gate.activate_scope(active_scope)

    decision = observe_all(
        gate,
        facts(
            active_scope,
            reply_overrides=reply_overrides,
            proposal_overrides=proposal_overrides,
        ),
    )

    assert decision is not None and not decision.admitted
    assert decision.reason_code == reason
    assert ledger.pending_replies(active_scope) == []
    assert ledger.pending_intents(active_scope) == []
    ledger.close()


@pytest.mark.parametrize(
    ("arguments", "reason"),
    [
        (
            {
                "command": "adjust_retraction",
                "target_side": "right",
                "distance_m": 0.005,
                "extra": 1,
            },
            "adjust_retraction_arguments_not_exact",
        ),
        (
            {
                "command": "adjust_retraction",
                "target_side": "left",
                "distance_m": 0.005,
            },
            "adjust_retraction_target_side_mismatch",
        ),
        (
            {
                "command": "adjust_retraction",
                "target_side": "right",
                "distance_m": 0.006,
            },
            "adjust_retraction_distance_mismatch",
        ),
        (
            {
                "command": "adjust_retraction",
                "target_side": "right",
                "distance_m": True,
            },
            "adjust_retraction_distance_invalid",
        ),
    ],
)
def test_adjust_retraction_requires_exact_semantic_arguments(
    tmp_path: Path,
    arguments: dict[str, object],
    reason: str,
) -> None:
    active_scope = scope()
    ledger = ledger_at(tmp_path)
    gate = VLMFunctionAdmissionGate(ledger)
    gate.activate_scope(active_scope)
    all_facts = facts(
        active_scope,
        command="adjust",
        reply_overrides={
            "function_arguments_json": json.dumps(
                arguments, separators=(",", ":")
            )
        },
    )

    decision = observe_all(gate, all_facts)

    assert decision is not None and decision.reason_code == reason
    ledger.close()


def test_adjust_retraction_cannot_claim_physical_completion(tmp_path: Path) -> None:
    active_scope = scope()
    ledger = ledger_at(tmp_path)
    gate = VLMFunctionAdmissionGate(ledger)
    gate.activate_scope(active_scope)
    all_facts = facts(
        active_scope,
        command="adjust",
        reply_overrides={"timing": "on_function_completed"},
    )

    decision = observe_all(gate, all_facts)

    assert decision is not None
    assert decision.reason_code == "adjust_retraction_completion_timing_unsupported"
    ledger.close()


def test_slow_function_reply_past_source_ttl_is_not_persisted_or_spoken(
    tmp_path: Path,
) -> None:
    active_scope = scope()
    ledger = ledger_at(tmp_path)
    gate = VLMFunctionAdmissionGate(ledger, intent_ttl_sec=3.0)
    gate.activate_scope(active_scope)
    all_facts = facts(active_scope)
    slow_reply = replace(
        all_facts[2],
        received_at_ns=all_facts[0].source_stamp_ns + 4_000_000_000,
    )

    assert gate.observe_speech(all_facts[0]) is None
    assert gate.observe_proposal(all_facts[1]) is None
    decision = gate.observe_reply(slow_reply)

    assert decision is not None
    assert decision.reason_code == "function_reply_missed_intent_ttl"
    assert ledger.pending_intents(active_scope) == []
    assert ledger.pending_replies(active_scope) == []
    ledger.close()


def test_immediate_answer_persists_only_reply_delivery(tmp_path: Path) -> None:
    active_scope = scope()
    ledger = ledger_at(tmp_path)
    gate = VLMFunctionAdmissionGate(ledger)
    gate.activate_scope(active_scope)
    all_facts = facts(
        active_scope,
        reply_overrides={
            "function_call_name": "",
            "function_arguments_json": "",
            "function_request_id": "",
            "timing": "immediate",
            "kind": "answer",
        },
        proposal_overrides={
            "intent": "",
            "tool_id": "",
            "disposition": "no_command",
        },
    )

    decision = observe_all(gate, all_facts)

    assert decision is not None and decision.admitted
    assert decision.admission is not None
    assert decision.admission.has_intent is False
    assert ledger.pending_intents(active_scope) == []
    assert ledger.acknowledgement_state(decision.admission.delivery_id) == (
        False,
        True,
        False,
    )
    ledger.close()


def test_slow_immediate_answer_can_join_after_executable_intent_ttl(
    tmp_path: Path,
) -> None:
    active_scope = scope()
    ledger = ledger_at(tmp_path)
    gate = VLMFunctionAdmissionGate(ledger, intent_ttl_sec=3.0)
    gate.activate_scope(active_scope)
    all_facts = facts(
        active_scope,
        reply_overrides={
            "function_call_name": "",
            "function_arguments_json": "",
            "function_request_id": "",
            "timing": "immediate",
            "kind": "answer",
        },
        proposal_overrides={
            "intent": "",
            "tool_id": "",
            "disposition": "no_command",
        },
    )
    slow_answer = replace(
        all_facts[2],
        received_at_ns=all_facts[0].source_stamp_ns + 10_000_000_000,
    )

    assert gate.observe_speech(all_facts[0]) is None
    assert gate.observe_proposal(all_facts[1]) is None
    decision = gate.observe_reply(slow_answer)

    assert decision is not None and decision.admitted
    assert decision.admission is not None
    assert decision.admission.has_intent is False
    assert ledger.pending_replies(active_scope)
    ledger.close()


def test_same_epoch_heartbeat_keeps_original_fence_and_partial_join(
    tmp_path: Path,
) -> None:
    initial = scope()
    heartbeat = replace(initial, source_stamp_ns=initial.source_stamp_ns + 500)
    ledger = ledger_at(tmp_path)
    gate = VLMFunctionAdmissionGate(ledger)
    gate.activate_scope(initial)
    all_facts = facts(initial)
    assert gate.observe_speech(all_facts[0]) is None

    assert gate.activate_scope(heartbeat) == 0
    assert gate.scope == initial
    assert gate.observe_proposal(all_facts[1]) is None
    decision = gate.observe_reply(all_facts[2])

    assert decision is not None and decision.admitted
    ledger.close()


def test_same_epoch_metadata_mutation_stays_poisoned_until_identity_changes(
    tmp_path: Path,
) -> None:
    initial = scope()
    mutated = replace(initial, procedure_type="different_procedure")
    ledger = ledger_at(tmp_path)
    gate = VLMFunctionAdmissionGate(ledger)
    gate.activate_scope(initial)

    gate.activate_scope(mutated)
    assert gate.scope is None
    gate.activate_scope(replace(mutated, source_stamp_ns=2_000_000_000))
    assert gate.scope is None

    replacement = scope(gateway="gateway-b", stamp_ns=3_000_000_000)
    gate.activate_scope(replacement)
    assert gate.scope == replacement
    ledger.close()


def test_watchdog_fence_stales_outputs_and_requires_post_boundary_facts(
    tmp_path: Path,
) -> None:
    initial = scope()
    ledger = ledger_at(tmp_path)
    gate = VLMFunctionAdmissionGate(ledger)
    gate.activate_scope(initial)
    decision = observe_all(gate, facts(initial))
    assert decision is not None and decision.admitted
    assert decision.admission is not None

    assert gate.activate_scope(None) == 1
    assert ledger.acknowledgement_state(decision.admission.delivery_id) == (
        False,
        False,
        True,
    )
    renewed = replace(initial, source_stamp_ns=initial.source_stamp_ns + 100)
    gate.activate_scope(renewed)
    old_facts = facts(initial, utterance_id="old-after-timeout")
    assert gate.observe_speech(old_facts[0]) is None
    assert gate.observe_proposal(old_facts[1]) is None
    rejected = gate.observe_reply(old_facts[2])
    assert rejected is not None
    assert rejected.reason_code == "speech_precedes_gateway_scope"
    assert ledger.pending_replies(renewed) == []
    assert ledger.pending_intents(renewed) == []
    ledger.close()


def test_inactive_scope_stales_fully_acknowledged_tombstone(tmp_path: Path) -> None:
    active_scope = scope()
    ledger = ledger_at(tmp_path)
    gate = VLMFunctionAdmissionGate(ledger)
    gate.activate_scope(active_scope)
    decision = observe_all(gate, facts(active_scope))
    assert decision is not None and decision.admission is not None
    admission = decision.admission
    assert ledger.ack_reply(admission.reply_id, "queued", active_scope)
    assert ledger.ack_intent(
        admission.function_request_id,
        "handover_intent_accepted",
        active_scope,
    )
    assert ledger.acknowledgement_state(admission.delivery_id) == (
        True,
        True,
        False,
    )

    assert gate.activate_scope(None) == 1
    assert ledger.acknowledgement_state(admission.delivery_id) == (
        True,
        True,
        True,
    )
    stored = ledger.get_by_reply_id(admission.reply_id)
    assert stored is not None
    assert stored.reply_json == "{}"
    assert stored.intent_json == "{}"
    ledger.close()


def test_same_run_new_gateway_epoch_never_recovers_old_row(tmp_path: Path) -> None:
    first = scope(gateway="gateway-a")
    second = scope(gateway="gateway-b", stamp_ns=2_000_000_000)
    ledger = ledger_at(tmp_path)
    gate = VLMFunctionAdmissionGate(ledger)
    gate.activate_scope(first)
    admitted = observe_all(gate, facts(first))
    assert admitted is not None and admitted.admission is not None

    assert gate.activate_scope(second) == 1
    assert ledger.acknowledgement_state(admitted.admission.delivery_id) == (
        False,
        False,
        True,
    )
    assert ledger.pending_replies(second) == []
    assert ledger.pending_intents(second) == []
    assert not ledger.ack_reply(admitted.admission.reply_id, "queued", second)

    second_facts = facts(
        second,
        reply_overrides={
            "reply_id": "reply-gateway-b-1",
            "turn_id": "turn-gateway-b-1",
            "function_request_id": "function-gateway-b-1",
        },
    )
    second_decision = observe_all(gate, second_facts)
    assert second_decision is not None and second_decision.admitted

    # A delayed retained heartbeat from the replaced epoch cannot displace the
    # active gateway or erase its partial/input facts.
    gate.activate_scope(replace(first, source_stamp_ns=3_000_000_000))
    assert gate.scope == second
    ledger.close()


def test_explicit_inactive_scope_retires_identity_but_watchdog_does_not(
    tmp_path: Path,
) -> None:
    first = scope()
    ledger = ledger_at(tmp_path)
    gate = VLMFunctionAdmissionGate(ledger)
    gate.activate_scope(first)

    gate.activate_scope(None)
    watchdog_recovery = replace(first, source_stamp_ns=2_000_000_000)
    gate.activate_scope(watchdog_recovery)
    assert gate.scope == watchdog_recovery

    gate.activate_scope(None, retire_current=True)
    gate.activate_scope(replace(first, source_stamp_ns=3_000_000_000))
    assert gate.scope is None
    ledger.close()


def test_gateway_adapter_ignores_replayed_and_replaced_epochs(
    tmp_path: Path,
) -> None:
    ledger = ledger_at(tmp_path)
    gate = VLMFunctionAdmissionGate(ledger)
    node = VLMFunctionAdmissionNode.__new__(VLMFunctionAdmissionNode)
    node._ledger = ledger
    node._gate = gate
    node._gateway_received_monotonic = 0.0
    node._gateway_revision_by_instance = {}
    node._gateway_stamp_by_instance = {}
    node._gateway_source_stamp_floor_ns = 0
    node._now_ns = lambda: 20_000_000_000
    node._retry_pending = lambda: None

    def heartbeat(
        gateway_id: str,
        *,
        revision: int,
        stamp_ns: int,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            stamp=SimpleNamespace(
                sec=stamp_ns // 1_000_000_000,
                nanosec=stamp_ns % 1_000_000_000,
            ),
            revision=revision,
            gateway_instance_id=gateway_id,
            procedure_run_id="run-1",
            procedure_type="thyroidectomy_demo",
            catalog_version="catalog-v1",
            procedure_active=True,
        )

    node._on_gateway(
        heartbeat("gateway-a", revision=1, stamp_ns=18_000_000_000)
    )
    assert gate.scope is not None
    assert gate.scope.identity == ("gateway-a", "run-1")
    node._on_gateway(
        heartbeat("gateway-b", revision=1, stamp_ns=19_000_000_000)
    )
    assert gate.scope is not None
    assert gate.scope.identity == ("gateway-b", "run-1")
    lease_received = node._gateway_received_monotonic

    # A newer per-A revision cannot revive the replaced A identity, and an
    # unseen epoch with an older source stamp cannot displace B either.
    node._on_gateway(
        heartbeat("gateway-a", revision=2, stamp_ns=18_500_000_000)
    )
    node._on_gateway(
        heartbeat("gateway-c", revision=1, stamp_ns=18_750_000_000)
    )
    assert gate.scope is not None
    assert gate.scope.identity == ("gateway-b", "run-1")
    assert node._gateway_received_monotonic == lease_received

    # An exact retained B heartbeat is not liveness evidence.
    node._on_gateway(
        heartbeat("gateway-b", revision=1, stamp_ns=19_000_000_000)
    )
    assert node._gateway_received_monotonic == lease_received
    ledger.close()


def test_reply_gateway_must_exactly_match_active_epoch(tmp_path: Path) -> None:
    active_scope = scope()
    ledger = ledger_at(tmp_path)
    gate = VLMFunctionAdmissionGate(ledger)
    gate.activate_scope(active_scope)
    all_facts = facts(active_scope)
    wrong_reply = replace(all_facts[2], gateway_instance_id="gateway-old")

    assert gate.observe_speech(all_facts[0]) is None
    assert gate.observe_proposal(all_facts[1]) is None
    decision = gate.observe_reply(wrong_reply)

    assert decision is not None
    assert decision.reason_code == "reply_outside_active_scope"
    ledger.close()


def test_intent_and_reply_acknowledgements_are_independent_and_restart_safe(
    tmp_path: Path,
) -> None:
    active_scope = scope()
    ledger = ledger_at(tmp_path)
    gate = VLMFunctionAdmissionGate(ledger)
    gate.activate_scope(active_scope)
    decision = observe_all(gate, facts(active_scope))
    assert decision is not None and decision.admission is not None
    admission = decision.admission

    assert ledger.ack_reply(admission.reply_id, "waiting_function_accepted", active_scope)
    assert not ledger.ack_reply(admission.reply_id, "queued", active_scope)
    assert ledger.pending_replies(active_scope) == []
    pending_intents = ledger.pending_intents(active_scope)
    assert len(pending_intents) == 1
    assert pending_intents[0].delivery_id == admission.delivery_id
    assert pending_intents[0].reply_json == "{}"
    ledger_path = ledger.path
    ledger.close()

    recovered = FunctionGateLedger(ledger_path)
    assert recovered.pending_replies(active_scope) == []
    recovered_pending = recovered.pending_intents(active_scope)
    assert len(recovered_pending) == 1
    assert recovered_pending[0].delivery_id == admission.delivery_id
    assert recovered.ack_intent(
        admission.function_request_id,
        "handover_intent_accepted",
        active_scope,
    )
    assert recovered.acknowledgement_state(admission.delivery_id) == (
        True,
        True,
        False,
    )
    recovered.close()


def test_durable_first_writer_cannot_be_overwritten(tmp_path: Path) -> None:
    active_scope = scope()
    ledger = ledger_at(tmp_path)
    gate = VLMFunctionAdmissionGate(ledger)
    gate.activate_scope(active_scope)
    decision = observe_all(gate, facts(active_scope))
    assert decision is not None and decision.admission is not None
    admission = decision.admission
    conflicting = replace(admission, intent_json='{"different":true}')

    with pytest.raises(LedgerCollisionError):
        ledger.admit(conflicting)

    assert ledger.get_by_function_request_id(admission.function_request_id) == admission
    ledger.close()


def test_admission_fsync_failure_rolls_back_before_any_row_is_visible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_scope = scope()
    ledger = ledger_at(tmp_path)
    gate = VLMFunctionAdmissionGate(ledger)
    gate.activate_scope(active_scope)
    all_facts = facts(active_scope)
    assert gate.observe_speech(all_facts[0]) is None
    assert gate.observe_proposal(all_facts[1]) is None

    def fail_fsync() -> None:
        raise OSError("injected fsync failure")

    monkeypatch.setattr(ledger, "_fsync_storage", fail_fsync)
    decision = gate.observe_reply(all_facts[2])

    assert decision is not None
    assert decision.reason_code == "durable_persistence_unavailable"
    assert ledger.get_by_reply_id("reply-gateway-a-1") is None
    assert ledger.get_by_function_request_id("function-gateway-a-1") is None
    ledger.close()


def test_reply_id_collision_is_converted_to_fail_closed_ledger_collision(
    tmp_path: Path,
) -> None:
    active_scope = scope()
    ledger = ledger_at(tmp_path)
    gate = VLMFunctionAdmissionGate(ledger)
    gate.activate_scope(active_scope)
    decision = observe_all(gate, facts(active_scope))
    assert decision is not None and decision.admission is not None
    first = decision.admission
    conflicting = replace(
        first,
        utterance_id="utt-2",
        function_request_id="function-gateway-a-2",
        intent_json=first.intent_json.replace("utt-1", "utt-2"),
    )

    with pytest.raises(LedgerCollisionError):
        ledger.admit(conflicting)
    ledger.close()


def test_negative_ack_outcome_never_becomes_successful_receipt_completion(
    tmp_path: Path,
) -> None:
    active_scope = scope()
    ledger = ledger_at(tmp_path)
    gate = VLMFunctionAdmissionGate(ledger)
    gate.activate_scope(active_scope)
    decision = observe_all(gate, facts(active_scope))
    assert decision is not None and decision.admission is not None
    admission = decision.admission

    assert ledger.ack_reply(
        admission.reply_id,
        "failed",
        active_scope,
        success=False,
    )
    assert ledger.ack_intent(
        admission.function_request_id,
        "handover_intent_rejected",
        active_scope,
        success=False,
    )
    assert ledger.acknowledgement_state(admission.delivery_id) == (
        True,
        True,
        False,
    )
    assert ledger.acknowledgement_outcomes(admission.delivery_id) == (
        False,
        False,
    )
    ledger.close()


def test_provisional_tts_receipt_can_become_terminal_failure_but_not_recover(
    tmp_path: Path,
) -> None:
    active_scope = scope()
    ledger = ledger_at(tmp_path)
    gate = VLMFunctionAdmissionGate(ledger)
    gate.activate_scope(active_scope)
    decision = observe_all(gate, facts(active_scope))
    assert decision is not None and decision.admission is not None
    admission = decision.admission

    assert ledger.ack_reply(
        admission.reply_id,
        "queued",
        active_scope,
        success=None,
    )
    assert ledger.ack_intent(
        admission.function_request_id,
        "handover_intent_accepted",
        active_scope,
        success=True,
    )
    assert ledger.acknowledgement_outcomes(admission.delivery_id) == (
        None,
        True,
    )

    assert ledger.ack_reply(
        admission.reply_id,
        "failed",
        active_scope,
        success=False,
    )
    assert ledger.acknowledgement_outcomes(admission.delivery_id) == (
        False,
        True,
    )
    assert not ledger.ack_reply(
        admission.reply_id,
        "played",
        active_scope,
        success=True,
    )
    assert ledger.acknowledgement_outcomes(admission.delivery_id) == (
        False,
        True,
    )
    ledger.close()


def test_provisional_tts_receipt_becomes_terminal_success_only_after_played(
    tmp_path: Path,
) -> None:
    active_scope = scope()
    ledger = ledger_at(tmp_path)
    gate = VLMFunctionAdmissionGate(ledger)
    gate.activate_scope(active_scope)
    decision = observe_all(gate, facts(active_scope))
    assert decision is not None and decision.admission is not None
    admission = decision.admission

    assert ledger.ack_reply(
        admission.reply_id,
        "waiting_function_accepted",
        active_scope,
        success=None,
    )
    assert ledger.acknowledgement_outcomes(admission.delivery_id) == (
        None,
        None,
    )
    assert ledger.ack_reply(
        admission.reply_id,
        "played",
        active_scope,
        success=True,
    )
    assert ledger.acknowledgement_outcomes(admission.delivery_id) == (
        True,
        None,
    )
    ledger.close()


def test_source_stamp_based_intent_ttl_stops_retry_and_redacts_intent(
    tmp_path: Path,
) -> None:
    active_scope = scope()
    ledger = ledger_at(tmp_path)
    gate = VLMFunctionAdmissionGate(ledger, intent_ttl_sec=1.0)
    gate.activate_scope(active_scope)
    decision = observe_all(gate, facts(active_scope))
    assert decision is not None and decision.admission is not None
    admission = decision.admission
    assert admission.intent_expires_at_ns == active_scope.source_stamp_ns + 1 + 1_000_000_000

    expired = ledger.expire_intents(
        active_scope,
        now_ns=admission.intent_expires_at_ns,
    )

    assert [item.delivery_id for item in expired] == [admission.delivery_id]
    assert ledger.pending_intents(active_scope) == []
    assert ledger.pending_replies(active_scope)
    assert ledger.acknowledgement_outcomes(admission.delivery_id) == (
        None,
        False,
    )
    stored = ledger.get_by_function_request_id(admission.function_request_id)
    assert stored is not None and stored.intent_json == "{}"
    ledger.close()


def test_ledger_permissions_are_private_and_insecure_parent_is_not_chmodded(
    tmp_path: Path,
) -> None:
    ledger = ledger_at(tmp_path)
    assert stat.S_IMODE(ledger.path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(ledger.path.stat().st_mode) == 0o600
    ledger.close()

    insecure = tmp_path / "shared"
    insecure.mkdir(mode=0o755)
    insecure.chmod(0o755)
    with pytest.raises(PermissionError):
        FunctionGateLedger(insecure / "must-not-open.sqlite3")
    assert stat.S_IMODE(insecure.stat().st_mode) == 0o755


def test_ledger_refuses_symbolic_link_target(tmp_path: Path) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    private.chmod(0o700)
    target = private / "target.sqlite3"
    target.touch(mode=0o600)
    link = private / "link.sqlite3"
    link.symlink_to(target)

    with pytest.raises(ValueError):
        FunctionGateLedger(link)


def test_sensitive_plaintext_is_absent_from_fact_and_admission_repr(
    tmp_path: Path,
) -> None:
    active_scope = scope()
    ledger = ledger_at(tmp_path)
    gate = VLMFunctionAdmissionGate(ledger)
    gate.activate_scope(active_scope)
    all_facts = facts(active_scope)
    decision = observe_all(gate, all_facts)
    assert decision is not None and decision.admission is not None

    for value in (*all_facts, decision, decision.admission):
        rendered = repr(value)
        assert TEXT not in rendered
        assert "바이폴라를 전달하겠습니다" not in rendered
    ledger.close()


def test_json_parser_rejects_nonobject_and_nonfinite_values() -> None:
    assert _json_load_object("[]") is None
    assert _json_load_object('{"distance_m":NaN}') is None
    assert _json_load_object('{"distance_m":Infinity}') is None
    assert _json_load_object('{"distance_m":0.005}') == {"distance_m": 0.005}


def test_speech_source_stamp_order_matches_resolver_envelope_priority() -> None:
    message = SimpleNamespace(
        stamp=SimpleNamespace(sec=10, nanosec=1),
        end_stamp=SimpleNamespace(sec=11, nanosec=2),
        start_stamp=SimpleNamespace(sec=9, nanosec=3),
    )

    assert _first_positive_stamp_ns(
        message,
        "stamp",
        "end_stamp",
        "start_stamp",
    ) == 10_000_000_001


def test_partial_fact_memory_is_bounded_and_source_prunable(tmp_path: Path) -> None:
    active_scope = scope()
    ledger = ledger_at(tmp_path)
    gate = VLMFunctionAdmissionGate(ledger)
    gate.activate_scope(active_scope)
    base_speech = facts(active_scope)[0]

    for index in range(1100):
        assert gate.observe_speech(
            replace(
                base_speech,
                utterance_id=f"pending-{index}",
                source_stamp_ns=active_scope.source_stamp_ns + index + 1,
            )
        ) is None

    assert len(gate._speech) == 1024
    removed = gate.prune_before_source_stamp(active_scope.source_stamp_ns + 1000)
    assert removed > 0
    assert all(
        fact.source_stamp_ns >= active_scope.source_stamp_ns + 1000
        for fact in gate._speech.values()
    )
    ledger.close()


def test_node_contract_has_no_direct_action_or_service_authority() -> None:
    package_root = Path(__file__).parents[1]
    source = (package_root / "voice_command" / "vlm_function_gate.py").read_text(
        encoding="utf-8"
    )
    setup_text = (package_root / "setup.py").read_text(encoding="utf-8")
    package_xml = (package_root / "package.xml").read_text(encoding="utf-8")

    assert "create_client(" not in source
    assert "ActionClient" not in source
    assert "vlm_function_admission_gate = voice_command.vlm_function_gate:main" in (
        setup_text
    )
    assert "<exec_depend>surgical_interop_msgs</exec_depend>" in package_xml
    for parameter in (
        "admitted_speech_topic",
        "vlm_reply_topic",
        "intent_output_topic",
        "reply_output_topic",
        "gateway_topic",
        "join_timeout_sec",
        "intent_ttl_sec",
    ):
        assert f'"{parameter}"' in source
    assert "ClockType.STEADY_TIME" in source
    assert "str(message.procedure_run_id" in source
    assert "str(message.utterance_id" in source


def test_direct_admission_requires_epoch_scoped_identifiers(tmp_path: Path) -> None:
    ledger = ledger_at(tmp_path)
    invalid = DurableAdmission(
        gateway_instance_id="gateway-a",
        procedure_run_id="run-1",
        utterance_id="utt-1",
        reply_id="",
        function_request_id="",
        function_call_name="",
        intent_expires_at_ns=0,
        reply_json="{}",
        intent_json="",
    )

    with pytest.raises(ValueError):
        ledger.admit(invalid)
    ledger.close()
