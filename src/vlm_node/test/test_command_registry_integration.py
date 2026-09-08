from __future__ import annotations

from types import SimpleNamespace

from vlm_node.real_vlm import (
    DialogueTurnGate,
    RealVLMNode,
    _repair_ninfer_dialogue_envelope,
)


def _function_payload(name: str = "request_tool_handover") -> dict:
    return {
        "v": "6",
        "phase": [],
        "tool": [],
        "intent": ["none", "", 0.0],
        "mayo": [],
        "mayo_retrieve": ["", 0.0],
        "function_call": {
            "turn_id": "8:run-1:u-1",
            "name": name,
            "arguments": {"tool_id": "alias"},
        },
    }


def test_ninfer_intent_never_promotes_to_a_presentation_hint() -> None:
    repaired = _repair_ninfer_dialogue_envelope(
        {
            "v": "6",
            "intent": ["handover", "T04", 0.99],
            "function_call": None,
            "humanoid_reply": None,
        },
        claimed_turn_id="8:run-1:u-1",
    )

    assert repaired["function_call"] is None


def test_actor_log_prompt_describes_non_executable_presentation_hints() -> None:
    instruction = RealVLMNode.__new__(
        RealVLMNode
    )._actor_log_developer_instruction()

    assert "non-executable presentation hint" in instruction
    assert "Typed ASR" in instruction


def test_actor_log_prompt_and_hint_canonicalization_ignore_scenario_command_policy() -> None:
    node = RealVLMNode.__new__(RealVLMNode)
    node._spec = SimpleNamespace(
        get_scenario_policy=lambda: (_ for _ in ()).throw(
            AssertionError("dialogue prompt must not read command policy")
        ),
    )
    node._canonical_tool_id = lambda value: str(value).strip()

    instruction = node._actor_log_developer_instruction()
    assert "non-executable presentation hint" in instruction

    payload = _function_payload("request_tool_handover")
    payload["function_call"]["arguments"] = {"tool_id": "T07"}
    canonical = node._canonicalize_payload_ids(payload)
    assert canonical["function_call"]["arguments"] == {"tool_id": "T07"}


def test_payload_handover_hint_canonicalizes_only_tool_id() -> None:
    node = RealVLMNode.__new__(RealVLMNode)
    node._canonical_tool_id = lambda value: "T07" if value == "alias" else ""

    canonical = node._canonicalize_payload_ids(_function_payload())

    assert canonical["function_call"]["arguments"] == {"tool_id": "T07"}


def test_payload_drops_unknown_presentation_hint() -> None:
    node = RealVLMNode.__new__(RealVLMNode)
    node._canonical_tool_id = lambda _value: "T07"

    canonical = node._canonicalize_payload_ids(_function_payload("unknown"))

    assert canonical["function_call"] is None


def test_reply_validation_uses_local_presentation_timing() -> None:
    node = RealVLMNode.__new__(RealVLMNode)
    node._dialogue_turn_gate = DialogueTurnGate(epoch=8)
    turn = node._dialogue_turn_gate.enqueue(
        utterance_id="u-1",
        text="demo",
        procedure_run_id="run-1",
    )
    assert turn is not None
    assert node._dialogue_turn_gate.claim(
        turn_id=turn.turn_id,
        correlation_id="vlm-8-1",
    ) == turn
    published = []
    node._gateway_instance_id = "gateway-1"
    node._spec = SimpleNamespace(procedure_id="demo")
    node._humanoid_reply_pub = SimpleNamespace(publish=published.append)
    node._set_visual_evidence_metadata = lambda *_args, **_kwargs: None
    node._mark_dialogue_responded = lambda _utterance_id: None
    node._queue_dialogue_followup = lambda *_args, **_kwargs: None

    accepted = node._publish_humanoid_reply(
        {
            "v": "6",
            "function_call": {
                "turn_id": turn.turn_id,
                "name": "request_tool_handover",
                "arguments": {"tool_id": "T07"},
            },
            "humanoid_reply": {
                "turn_id": turn.turn_id,
                "text": "completed",
                "speak": True,
                "timing": "on_function_accepted",
            },
        },
        observation_stamp=SimpleNamespace(sec=1, nanosec=0),
        source_epoch=8,
        source_sequence=1,
        correlation_id="vlm-8-1",
        claimed_turn_id=turn.turn_id,
    )

    assert accepted is True
    assert len(published) == 1
    assert published[0].function_call_name == "request_tool_handover"
    assert published[0].function_request_id == ""
