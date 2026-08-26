from __future__ import annotations

from collections import deque
from pathlib import Path
import threading
import time

import pytest

from builtin_interfaces.msg import Time
from or_digital_twin.node import ORDigitalTwinNode
from or_digital_twin.twin import ORDigitalTwin
from or_digital_twin.voice_intent_receipt import (
    GatewayLeaseAuthority,
    VoiceIntentReceiptLedger,
)
from procedure_spec import load_bundle, load_voice_command_catalog
from std_msgs.msg import String
from surgical_msgs.msg import VoiceCommandIntent


def _bundle_dir(name: str = "thyroidectomy") -> Path:
    return (
        Path(__file__).parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
        / name
    )


def _spec(name: str = "thyroidectomy"):
    return load_bundle(_bundle_dir(name))


def _node(*, spec_name: str = "thyroidectomy"):
    node = ORDigitalTwinNode.__new__(ORDigitalTwinNode)
    node._twin = ORDigitalTwin(_spec(spec_name))
    node._voice_command_catalog = load_voice_command_catalog(
        _bundle_dir(spec_name)
    )
    node._tool_predict_stability = {}
    node._tool_prediction_last_sample_by_source = {}
    node._validated_tool_request_history = deque(maxlen=12)
    node._recent_voice_intent_ids = {}
    node._require_voice_intent_source_metadata = False
    node._voice_intent_max_age_sec = 3.0
    node._voice_intent_future_tolerance_sec = 1.0
    node._voice_intent_dedupe_retention_sec = 120.0
    node._voice_intent_receipt_ledger = VoiceIntentReceiptLedger(":memory:")
    node._voice_gateway_authority = GatewayLeaseAuthority(timeout_sec=3.0)
    node._voice_gated_admission_lock = threading.RLock()
    node._voice_gated_mutation_dirty = False
    node._voice_gated_mutation_fatal = False
    events: list[tuple[str, dict]] = []
    world_updates: list[bool] = []
    node._publish_event = lambda event_type, **kwargs: events.append(
        (event_type, kwargs)
    )
    node._publish_world_state = lambda: world_updates.append(True)
    node._stamp = lambda: Time()
    return node, events, world_updates


def _intent(**overrides):
    catalog = load_voice_command_catalog(_bundle_dir())
    payload = {
        "procedure_id": catalog.procedure_id,
        "catalog_id": catalog.catalog_id,
        "intent": "tool_handover",
        "tool_id": "T04",
        "disposition": "propose",
        "requires_confirmation": False,
        "raw_text": "보비 내놔, 빨리",
        "normalized_text": "보비 내놔 빨리",
        "urgency": "urgent",
        "provenance": "voice_intent_resolver",
        "reason": "exact_tool_and_handover_verb",
    }
    payload.update(overrides)
    message = VoiceCommandIntent()
    for field, value in payload.items():
        setattr(message, field, value)
    return message


def test_typed_handover_proposal_queues_only_the_canonical_tool_id() -> None:
    node, events, world_updates = _node()

    node._on_voice_command_intent(_intent())

    assert node._twin.state.surgeon_request_tool == "T04"
    assert node._twin.request_queue_summary()["queued_tools"] == ["T04"]
    assert events == [
        (
            "VoiceCommandIntentObserved",
            {
                "instrument_id": "T04",
                "detail": {
                    "intent": "tool_handover",
                    "disposition": "propose",
                    "requires_confirmation": False,
                    "tool_id": "T04",
                    "procedure_id": "thyroidectomy",
                    "catalog_id": node._voice_command_catalog.catalog_id,
                    "resolved_tool": "T04",
                    "accepted": True,
                    "request_generation": (
                        node._twin.state.surgeon_request_generation
                    ),
                    "urgency": "urgent",
                    "urgency_applied_to_execution": False,
                    "reason": "exact_tool_and_handover_verb",
                    "resolver_reason": "exact_tool_and_handover_verb",
                    "raw_text_present": True,
                    "normalized_text_present": True,
                    "provenance": "voice_intent_resolver",
                    "function_request_id": "",
                    "gateway_instance_id": "",
                    "procedure_run_id": "",
                    "utterance_id": "",
                    "source": "",
                    "source_is_final": False,
                },
                "mode": "voice_command_intent",
            },
        )
    ]
    assert world_updates == [True]


def test_accepted_voice_intent_maps_utterance_to_active_request_generation() -> None:
    node, events, world_updates = _node()

    node._on_voice_command_intent(
        _intent(utterance_id="asr-voice-request-42")
    )

    detail = events[0][1]["detail"]
    assert detail["accepted"] is True
    assert detail["utterance_id"] == "asr-voice-request-42"
    assert detail["request_generation"] > 0
    assert detail["request_generation"] == (
        node._twin.state.surgeon_request_generation
    )
    assert world_updates == [True]


def test_function_scope_is_preserved_in_handover_admission_event() -> None:
    node, events, world_updates = _node()
    node._twin.state.procedure_run_id = "run-1"
    assert node._voice_gateway_authority.observe(
        gateway_instance_id="gateway-1",
        procedure_run_id="run-1",
        procedure_type="thyroidectomy",
        catalog_version="catalog-v1",
        schema_version="surgery-state/v1",
        interface_version="0.4.0",
        procedure_active=True,
        revision=1,
        source_stamp_ns=1,
        now_ns=1,
        received_monotonic=time.monotonic(),
        expected_procedure_type="thyroidectomy",
    ) == "gateway_scope_active"

    node._on_voice_command_intent(
        _intent(
            utterance_id="asr-1",
            gateway_instance_id="gateway-1",
            procedure_run_id="run-1",
            function_request_id="function-1",
        )
    )

    detail = events[0][1]["detail"]
    assert detail["accepted"] is True
    assert detail["gateway_instance_id"] == "gateway-1"
    assert detail["procedure_run_id"] == "run-1"
    assert detail["function_request_id"] == "function-1"
    assert world_updates == [True]


def _live_source_intent(**overrides) -> VoiceCommandIntent:
    message = _intent()
    message.header.stamp = Time(sec=100)
    message.utterance_id = "asr-100-1"
    message.source = "taskplanner_asr:cloud"
    message.source_is_final = True
    message.source_speaker_role = "surgeon"
    message.source_has_confidence = False
    message.source_confidence = 0.0
    for field, value in overrides.items():
        setattr(message, field, value)
    return message


def test_live_voice_intent_metadata_admission_rejects_bad_or_replayed_source() -> None:
    node, events, _world_updates = _node()
    node._require_voice_intent_source_metadata = True
    node._stamp = lambda: Time(sec=101)

    node._on_voice_command_intent(_live_source_intent())
    node._on_voice_command_intent(_live_source_intent())
    node._on_voice_command_intent(
        _live_source_intent(
            utterance_id="asr-100-2",
            source_is_final=False,
        )
    )
    missing_stamp = _live_source_intent(utterance_id="asr-100-2a")
    missing_stamp.header.stamp = Time()
    node._on_voice_command_intent(missing_stamp)
    stale = _live_source_intent(utterance_id="asr-100-3")
    stale.header.stamp = Time(sec=97)
    node._on_voice_command_intent(stale)
    future = _live_source_intent(utterance_id="asr-100-4")
    future.header.stamp = Time(sec=103)
    node._on_voice_command_intent(future)

    assert node._twin.request_queue_summary()["queued_tools"] == ["T04"]
    assert [event[1]["detail"]["reason"] for event in events] == [
        "exact_tool_and_handover_verb",
        "voice_intent_duplicate_utterance_id",
        "voice_intent_source_not_final",
        "voice_intent_missing_source_timestamp",
        "voice_intent_stale:4.000s",
        "voice_intent_future_timestamp:2.000s",
    ]


def test_typed_handover_needing_confirmation_never_queues_request() -> None:
    node, events, world_updates = _node()

    node._on_voice_command_intent(
        _intent(requires_confirmation=True, reason="repaired_tool_name")
    )

    assert node._twin.request_queue_summary()["queue_length"] == 0
    assert events[0][1]["detail"]["accepted"] is False
    assert events[0][1]["detail"]["request_generation"] == 0
    assert events[0][1]["detail"]["reason"] == "voice_intent_requires_confirmation"
    assert world_updates == []


def test_typed_handover_rejects_noncanonical_or_unknown_tool_id() -> None:
    node, events, world_updates = _node()

    node._on_voice_command_intent(_intent(tool_id="Bovie"))

    assert node._twin.request_queue_summary()["queue_length"] == 0
    assert events[0][1]["detail"]["accepted"] is False
    assert events[0][1]["detail"]["reason"] == "unknown_or_unavailable_canonical_tool_id"
    assert world_updates == []


@pytest.mark.parametrize("tool_id", ["T05", "T11"])
def test_typed_handover_rejects_demo_controller_owned_retractor_ids(
    tool_id: str,
) -> None:
    node, events, world_updates = _node(spec_name="thyroidectomy_demo")

    node._on_voice_command_intent(
        _intent(
            procedure_id=node._voice_command_catalog.procedure_id,
            catalog_id=node._voice_command_catalog.catalog_id,
            tool_id=tool_id,
        )
    )

    assert node._twin.request_queue_summary()["queue_length"] == 0
    assert events[0][1]["detail"]["accepted"] is False
    assert events[0][1]["detail"]["reason"] == (
        "unknown_or_unavailable_canonical_tool_id"
    )
    assert world_updates == []


def test_typed_handover_does_not_reparse_raw_text_for_additional_instance() -> None:
    node, _events, _world_updates = _node(spec_name="thyroidectomy_demo")

    node._on_voice_command_intent(
        _intent(
            procedure_id=node._voice_command_catalog.procedure_id,
            catalog_id=node._voice_command_catalog.catalog_id,
            tool_id="T02",
            raw_text="애드슨 하나 더 내놔",
            normalized_text="애드슨 하나 더 내놔",
        )
    )

    queued = list(node._twin.state.surgeon_request_queue)
    assert len(queued) == 1
    assert queued[0].instrument_id == "T02"
    assert queued[0].instance_id == "T02#1"
    assert queued[0].voice_text == ""


def test_resolved_twin_handover_refuses_alias_text() -> None:
    twin = ORDigitalTwin(_spec())

    assert twin.update_resolved_voice_tool_handover("Bovie") == ""
    assert twin.request_queue_summary()["queue_length"] == 0


def test_typed_handover_rejects_procedure_or_catalog_binding_mismatch() -> None:
    node, events, world_updates = _node()

    node._on_voice_command_intent(_intent(procedure_id="nephrectomy"))
    node._on_voice_command_intent(_intent(catalog_id="sha256:stale"))

    assert node._twin.request_queue_summary()["queue_length"] == 0
    assert [event[1]["detail"]["reason"] for event in events] == [
        "voice_intent_procedure_id_mismatch",
        "voice_intent_catalog_id_mismatch",
    ]
    assert world_updates == []


def test_raw_handover_text_is_observation_only_without_compatibility_switch() -> None:
    node, events, world_updates = _node()
    raw = String()
    raw.data = "Bovie 주세요"

    node._on_request(raw)

    assert node._twin.request_queue_summary()["queue_length"] == 0
    assert events[0][0] == "VoiceTranscriptObserved"
    assert events[0][1]["detail"]["command_type"] == "observation"
    assert world_updates == []


def test_raw_completion_text_is_observation_only_without_compatibility_switch() -> None:
    node, events, world_updates = _node()
    node._twin.state.running = True
    node._twin.state.execution_state = "running"
    raw = String()
    raw.data = "수술을 마치겠습니다"

    node._on_request(raw)

    assert node._twin.state.execution_state == "running"
    assert events[0][0] == "VoiceTranscriptObserved"
    assert events[0][1]["detail"]["command_type"] == "observation"
    assert world_updates == []
