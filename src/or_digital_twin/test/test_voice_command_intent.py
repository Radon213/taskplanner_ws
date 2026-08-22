from __future__ import annotations

from collections import deque
from pathlib import Path

from builtin_interfaces.msg import Time
from or_digital_twin.node import ORDigitalTwinNode
from or_digital_twin.twin import ORDigitalTwin
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
                    "urgency": "urgent",
                    "urgency_applied_to_execution": False,
                    "reason": "exact_tool_and_handover_verb",
                    "resolver_reason": "exact_tool_and_handover_verb",
                    "raw_text_present": True,
                    "normalized_text_present": True,
                    "provenance": "voice_intent_resolver",
                },
                "mode": "voice_command_intent",
            },
        )
    ]
    assert world_updates == [True]


def test_typed_handover_needing_confirmation_never_queues_request() -> None:
    node, events, world_updates = _node()

    node._on_voice_command_intent(
        _intent(requires_confirmation=True, reason="repaired_tool_name")
    )

    assert node._twin.request_queue_summary()["queue_length"] == 0
    assert events[0][1]["detail"]["accepted"] is False
    assert events[0][1]["detail"]["reason"] == "voice_intent_requires_confirmation"
    assert world_updates == []


def test_typed_handover_rejects_noncanonical_or_unknown_tool_id() -> None:
    node, events, world_updates = _node()

    node._on_voice_command_intent(_intent(tool_id="Bovie"))

    assert node._twin.request_queue_summary()["queue_length"] == 0
    assert events[0][1]["detail"]["accepted"] is False
    assert events[0][1]["detail"]["reason"] == "unknown_or_unavailable_canonical_tool_id"
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
