from __future__ import annotations

from collections import deque
import json
from types import SimpleNamespace

import pytest
import vlm_node.real_vlm as real_vlm_module
from vlm_node.real_vlm import (
    DialogueTurnGate,
    INFERENCE_TRIGGER_SOURCE_FRAME,
    InferenceBackpressure,
    RealVLMNode,
    actor_log_request_context,
    stable_dialogue_reply_id,
)
from vlm_node.reply_outbox import ReplyCollisionError
from vlm_node.schema import SchemaValidationError


def _stamp(sec: int = 10):
    return SimpleNamespace(sec=sec, nanosec=0)


def _utterance(utterance_id: str, text: str):
    return SimpleNamespace(
        stamp=_stamp(),
        start_stamp=_stamp(9),
        end_stamp=_stamp(),
        utterance_id=utterance_id,
        text=text,
        is_final=True,
        speaker_role="surgeon",
        language="ko-KR",
        source="operational_asr",
    )


def _v6_dialogue_payload(
    *,
    function_call,
    humanoid_reply,
) -> dict:
    return {
        "v": "6",
        "phase": [["P03", 0.85]],
        "tool": [["T02", 1.0]],
        "intent": ["handover", "T02", 0.95],
        "mayo": [],
        "mayo_retrieve": ["", 0.0],
        "u": 0.25,
        "sum": "The surgeon requested Adson forceps.",
        "bed_robot_arm_group": None,
        "function_call": function_call,
        "humanoid_reply": humanoid_reply,
    }


def _dialogue_node() -> RealVLMNode:
    node = RealVLMNode.__new__(RealVLMNode)
    node._dialogue_turn_gate = DialogueTurnGate(epoch=41)
    node._gateway_instance_id = "gateway-1"
    node._procedure_run_id = "run-1"
    node._recent_speech = deque(maxlen=10)
    node._active = True
    node._response_mode = "live"
    node._causal_now_sec = lambda: 10.0
    node.get_clock = lambda: SimpleNamespace(
        now=lambda: SimpleNamespace(nanoseconds=10_000_000_000)
    )
    node._gateway_info_last_instance_id = ""
    node._gateway_info_last_revision = -1
    node._gateway_info_last_source_stamp_ns = 0
    node._gateway_info_timeout_sec = 3.0
    # Focused dialogue tests do not load a ProcedureSpec; production resolves
    # this through the active procedure bundle before accepting a hint.
    node._canonical_tool_id = lambda value: str(value or "").strip()
    node._trigger_count = 0
    node._trigger_inference_for_public_speech = lambda: setattr(
        node,
        "_trigger_count",
        node._trigger_count + 1,
    )
    return node


def _gateway_info(
    *,
    gateway_instance_id: str = "gateway-1",
    procedure_run_id: str = "run-1",
    procedure_active: bool = True,
    revision: int = 1,
    stamp_sec: int = 10,
):
    return SimpleNamespace(
        stamp=_stamp(stamp_sec),
        revision=revision,
        gateway_instance_id=gateway_instance_id,
        procedure_run_id=procedure_run_id,
        procedure_active=procedure_active,
    )


def test_typed_final_utterance_creates_one_fifo_turn_by_id() -> None:
    node = _dialogue_node()

    node._on_speech_utterance(_utterance("u-1", "바이폴라 주세요"))
    node._on_speech_utterance(_utterance("u-1", "바이폴라 주세요"))
    node._on_speech_utterance(_utterance("u-2", "바이폴라 주세요"))

    first = node._dialogue_turn_gate.next_pending()
    assert first is not None
    assert first.utterance_id == "u-1"
    assert first.procedure_run_id == "run-1"
    assert node._trigger_count == 2
    assert [row["utterance_id"] for row in node._recent_speech] == ["u-1", "u-2"]


def test_reply_id_is_stable_across_vlm_process_epochs() -> None:
    assert stable_dialogue_reply_id(
        "run-1", "u-1", gateway_instance_id="gateway-1"
    ) == stable_dialogue_reply_id(
        "run-1", "u-1", gateway_instance_id="gateway-1"
    )
    assert stable_dialogue_reply_id("run-1", "u-1").startswith(
        "humanoid-reply:"
    )
    assert stable_dialogue_reply_id("run-1", "u-1") != stable_dialogue_reply_id(
        "run-2", "u-1"
    )
    assert stable_dialogue_reply_id("run-1", "u-1") != stable_dialogue_reply_id(
        "run-1", "u-2"
    )
    assert stable_dialogue_reply_id(
        "run-1", "u-1", gateway_instance_id="gateway-1"
    ) != stable_dialogue_reply_id(
        "run-1", "u-1", gateway_instance_id="gateway-2"
    )


def test_nonfinal_or_non_surgeon_utterance_never_creates_reply_turn() -> None:
    node = _dialogue_node()
    partial = _utterance("u-partial", "아직 말하는 중")
    partial.is_final = False
    other = _utterance("u-other", "바이폴라 주세요")
    other.speaker_role = "observer"

    node._on_speech_utterance(partial)
    node._on_speech_utterance(other)

    assert node._dialogue_turn_gate.next_pending() is None
    assert node._trigger_count == 0


def test_valid_claimed_v6_reply_is_published_once() -> None:
    node = _dialogue_node()
    node._spec = SimpleNamespace(procedure_id="thyroidectomy_demo")
    node._inference_backpressure = InferenceBackpressure()
    published = []
    node._humanoid_reply_pub = SimpleNamespace(publish=published.append)
    node._on_speech_utterance(_utterance("u-1", "현재 단계가 무엇인가요?"))
    turn = node._dialogue_turn_gate.next_pending()
    assert turn is not None
    assert node._dialogue_turn_gate.claim(
        turn_id=turn.turn_id,
        correlation_id="vlm-41-1",
    ) == turn
    payload = {
        "v": "6",
        "function_call": None,
        "humanoid_reply": {
            "turn_id": turn.turn_id,
            "text": "현재 수술 단계를 확인하고 있습니다.",
            "speak": True,
            "timing": "immediate",
        },
    }

    assert node._publish_humanoid_reply(
        payload,
        observation_stamp=_stamp(),
        source_epoch=41,
        source_sequence=1,
        correlation_id="vlm-41-1",
        claimed_turn_id=turn.turn_id,
    )
    assert not node._publish_humanoid_reply(
        payload,
        observation_stamp=_stamp(),
        source_epoch=41,
        source_sequence=1,
        correlation_id="vlm-41-1",
        claimed_turn_id=turn.turn_id,
    )

    assert len(published) == 1
    assert published[0].utterance_id == "u-1"
    assert published[0].reply_id == stable_dialogue_reply_id(
        "run-1", "u-1", gateway_instance_id="gateway-1"
    )
    assert published[0].text == "현재 수술 단계를 확인하고 있습니다."
    assert node._recent_speech[0]["responded"] is True


def test_ninfer_missing_turn_ids_bind_to_claimed_turn_and_publish_once() -> None:
    node = _dialogue_node()
    node._provider_id = "ninfer"
    node._context_mode = "actor_log"
    node._spec = SimpleNamespace(procedure_id="thyroidectomy_demo")
    node._inference_backpressure = InferenceBackpressure()
    published = []
    node._humanoid_reply_pub = SimpleNamespace(publish=published.append)
    node._on_speech_utterance(_utterance("u-adson", "Adson 주세요."))
    turn = node._dialogue_turn_gate.next_pending()
    assert turn is not None
    assert node._dialogue_turn_gate.claim(
        turn_id=turn.turn_id,
        correlation_id="vlm-41-adson",
    ) == turn

    raw_payload = _v6_dialogue_payload(
        function_call={
            "name": "request_tool_handover",
            "arguments": {"tool_id": "T02"},
        },
        humanoid_reply={
            "text": "Adson을 전달드리겠습니다.",
            "speak": True,
            "timing": "on_function_accepted",
        },
    )
    _raw, normalized = node._normalize_model_raw_text(
        json.dumps(raw_payload),
        claimed_dialogue_turn_id=turn.turn_id,
    )

    assert normalized["function_call"]["turn_id"] == turn.turn_id
    assert normalized["humanoid_reply"]["turn_id"] == turn.turn_id
    assert node._publish_humanoid_reply(
        normalized,
        observation_stamp=_stamp(),
        source_epoch=41,
        source_sequence=1,
        correlation_id="vlm-41-adson",
        claimed_turn_id=turn.turn_id,
    )
    assert len(published) == 1
    assert published[0].function_call_name == "request_tool_handover"
    assert published[0].function_arguments_json == '{"tool_id":"T02"}'


def test_ninfer_handover_intent_never_creates_a_presentation_hint() -> None:
    node = _dialogue_node()
    node._provider_id = "ninfer"
    node._context_mode = "actor_log"
    node._spec = SimpleNamespace(procedure_id="thyroidectomy_demo")
    node._inference_backpressure = InferenceBackpressure()
    published = []
    node._humanoid_reply_pub = SimpleNamespace(publish=published.append)
    node._on_speech_utterance(_utterance("u-adson-intent", "Adson 주세요."))
    turn = node._dialogue_turn_gate.next_pending()
    assert turn is not None
    assert node._dialogue_turn_gate.claim(
        turn_id=turn.turn_id,
        correlation_id="vlm-41-adson-intent",
    ) == turn

    raw_payload = _v6_dialogue_payload(
        function_call=None,
        humanoid_reply={
            "text": "Adson 전달드리겠습니다.",
            "timing": "immediate",
        },
    )
    _raw, normalized = node._normalize_model_raw_text(
        json.dumps(raw_payload),
        claimed_dialogue_turn_id=turn.turn_id,
    )

    assert normalized["function_call"] is None
    assert normalized["humanoid_reply"] == {
        "turn_id": turn.turn_id,
        "text": "Adson 전달드리겠습니다.",
        "speak": True,
        "timing": "immediate",
    }
    assert node._publish_humanoid_reply(
        normalized,
        observation_stamp=_stamp(),
        source_epoch=41,
        source_sequence=1,
        correlation_id="vlm-41-adson-intent",
        claimed_turn_id=turn.turn_id,
    )
    assert len(published) == 1
    assert published[0].function_call_name == ""
    assert published[0].timing == "immediate"


def test_ninfer_dialogue_adapter_adds_only_action_neutral_top_level_nulls() -> None:
    node = RealVLMNode.__new__(RealVLMNode)
    node._response_mode = "live"
    node._provider_id = "ninfer"
    node._context_mode = "actor_log"
    payload = _v6_dialogue_payload(
        function_call=None,
        humanoid_reply=None,
    )
    payload.pop("function_call")
    payload.pop("humanoid_reply")
    payload.pop("bed_robot_arm_group")
    payload["intent"] = ["none", "", 0.0]

    _raw, normalized = node._normalize_model_raw_text(
        json.dumps(payload),
        claimed_dialogue_turn_id="41:run-1:u-1",
    )

    assert normalized["function_call"] is None
    assert normalized["humanoid_reply"] is None
    assert normalized["bed_robot_arm_group"] is None


def test_ninfer_live_adapter_normalizes_missing_retraction_proposal_to_null() -> None:
    node = RealVLMNode.__new__(RealVLMNode)
    node._response_mode = "live"
    node._provider_id = "ninfer"
    node._context_mode = "actor_log"
    payload = _v6_dialogue_payload(
        function_call=None,
        humanoid_reply=None,
    )
    payload.pop("bed_robot_arm_group")

    _raw, normalized = node._normalize_model_raw_text(
        json.dumps(payload),
        claimed_dialogue_turn_id="41:run-1:u-1",
    )

    assert normalized["bed_robot_arm_group"] is None


def test_ninfer_live_adapter_discards_incomplete_retraction_proposal() -> None:
    node = RealVLMNode.__new__(RealVLMNode)
    node._response_mode = "live"
    node._provider_id = "ninfer"
    node._context_mode = "actor_log"
    payload = _v6_dialogue_payload(
        function_call=None,
        humanoid_reply=None,
    )
    payload["bed_robot_arm_group"] = {
        "request_id": "retraction-1",
        "group_id": "retraction",
    }

    _raw, normalized = node._normalize_model_raw_text(
        json.dumps(payload),
        claimed_dialogue_turn_id="41:run-1:u-1",
    )

    assert normalized["bed_robot_arm_group"] is None


def test_non_ninfer_live_adapter_keeps_missing_retraction_proposal_strict() -> None:
    node = RealVLMNode.__new__(RealVLMNode)
    node._response_mode = "live"
    node._provider_id = "lmstudio"
    node._context_mode = "actor_log"
    payload = _v6_dialogue_payload(
        function_call=None,
        humanoid_reply=None,
    )
    payload.pop("bed_robot_arm_group")

    with pytest.raises(
        SchemaValidationError,
        match="schema v5 is missing fields: bed_robot_arm_group",
    ):
        node._normalize_model_raw_text(
            json.dumps(payload),
            claimed_dialogue_turn_id="41:run-1:u-1",
        )


def test_ninfer_dialogue_adapter_discards_only_known_extension_fields() -> None:
    node = RealVLMNode.__new__(RealVLMNode)
    node._response_mode = "live"
    node._provider_id = "ninfer"
    node._context_mode = "actor_log"
    payload = _v6_dialogue_payload(
        function_call=None,
        humanoid_reply=None,
    )
    payload["phase_alt"] = [["P04", 0.1]]
    payload["tool_alt"] = [["T03", 0.1]]
    payload["intent"] = ["none", "", 0.0]

    _raw, normalized = node._normalize_model_raw_text(
        json.dumps(payload),
        claimed_dialogue_turn_id="41:run-1:u-1",
    )

    assert "phase_alt" not in normalized
    assert "tool_alt" not in normalized


def test_ninfer_string_reply_is_bounded_and_bound_to_claimed_turn() -> None:
    node = RealVLMNode.__new__(RealVLMNode)
    node._response_mode = "live"
    node._provider_id = "ninfer"
    node._context_mode = "actor_log"
    payload = _v6_dialogue_payload(
        function_call=None,
        humanoid_reply="  Adson   전달드리겠습니다.  ",
    )

    _raw, normalized = node._normalize_model_raw_text(
        json.dumps(payload),
        claimed_dialogue_turn_id="41:run-1:u-1",
    )

    assert normalized["function_call"] is None
    assert normalized["humanoid_reply"] == {
        "turn_id": "41:run-1:u-1",
        "text": "Adson 전달드리겠습니다.",
        "speak": True,
        "timing": "immediate",
    }


def test_ninfer_explicit_false_speak_is_never_overwritten() -> None:
    node = RealVLMNode.__new__(RealVLMNode)
    node._response_mode = "live"
    node._provider_id = "ninfer"
    node._context_mode = "actor_log"
    payload = _v6_dialogue_payload(
        function_call=None,
        humanoid_reply={
            "text": "발화하지 않습니다.",
            "speak": False,
        },
    )

    _raw, normalized = node._normalize_model_raw_text(
        json.dumps(payload),
        claimed_dialogue_turn_id="41:run-1:u-1",
    )

    assert normalized["humanoid_reply"]["speak"] is False
    assert normalized["humanoid_reply"]["timing"] == "immediate"


def test_ninfer_low_confidence_intent_never_creates_a_function_call() -> None:
    node = RealVLMNode.__new__(RealVLMNode)
    node._response_mode = "live"
    node._provider_id = "ninfer"
    node._context_mode = "actor_log"
    payload = _v6_dialogue_payload(
        function_call=None,
        humanoid_reply={
            "text": "도구 요청을 다시 확인해 주세요.",
            "speak": True,
            "timing": "immediate",
        },
    )
    payload["intent"] = ["handover", "T02", 0.49]

    _raw, normalized = node._normalize_model_raw_text(
        json.dumps(payload),
        claimed_dialogue_turn_id="41:run-1:u-1",
    )

    assert normalized["function_call"] is None
    assert normalized["humanoid_reply"]["timing"] == "immediate"


def test_ninfer_truncated_summary_keeps_complete_mayo_observation_without_actions() -> None:
    """A tail-only NInfer omission must not erase earlier observational facts."""

    node = RealVLMNode.__new__(RealVLMNode)
    node._response_mode = "live"
    node._provider_id = "ninfer"
    node._context_mode = "actor_log"
    payload = _v6_dialogue_payload(function_call=None, humanoid_reply=None)
    payload["mayo"] = [["T04", "reuse", 0.8]]
    # Match the production failure shape: a complete object through `u`, then
    # the beginning of the final clinical-summary field.
    prefix = {
        key: value
        for key, value in payload.items()
        if key not in {"sum", "bed_robot_arm_group", "function_call", "humanoid_reply"}
    }
    raw_text = json.dumps(prefix, separators=(",", ":"))[:-1] + ',"sum":'

    _raw, normalized = node._normalize_model_raw_text(raw_text)

    assert normalized["sum"] == ""
    assert normalized["mayo"] == [["T04", "reuse", 0.8]]
    assert normalized["bed_robot_arm_group"] is None
    assert normalized["function_call"] is None
    assert normalized["humanoid_reply"] is None


@pytest.mark.parametrize("confidence", [0.5, 1.0])
def test_ninfer_handover_intent_never_promotes_at_any_confidence(
    confidence,
) -> None:
    payload = _v6_dialogue_payload(
        function_call=None,
        humanoid_reply=None,
    )
    payload["intent"] = ["handover", "T02", confidence]

    repaired = real_vlm_module._repair_ninfer_dialogue_envelope(
        payload,
        claimed_turn_id="41:run-1:u-1",
    )

    assert repaired["function_call"] is None


@pytest.mark.parametrize(
    "confidence",
    [0.499999, 1.000001, float("nan"), float("inf"), -float("inf"), True],
)
def test_ninfer_handover_promotion_rejects_out_of_contract_confidence(
    confidence,
) -> None:
    payload = _v6_dialogue_payload(
        function_call=None,
        humanoid_reply=None,
    )
    payload["intent"] = ["handover", "T02", confidence]

    repaired = real_vlm_module._repair_ninfer_dialogue_envelope(
        payload,
        claimed_turn_id="41:run-1:u-1",
    )

    assert repaired["function_call"] is None


def test_ninfer_dialogue_adapter_never_overwrites_explicit_turn_id() -> None:
    node = RealVLMNode.__new__(RealVLMNode)
    node._response_mode = "live"
    node._provider_id = "ninfer"
    node._context_mode = "actor_log"
    payload = _v6_dialogue_payload(
        function_call={
            "turn_id": "stale-turn",
            "name": "request_tool_handover",
            "arguments": {"tool_id": "T02"},
        },
        humanoid_reply={
            "turn_id": "stale-turn",
            "text": "지연된 응답",
            "speak": True,
            "timing": "on_function_accepted",
        },
    )

    _raw, normalized = node._normalize_model_raw_text(
        json.dumps(payload),
        claimed_dialogue_turn_id="41:run-1:u-1",
    )

    assert normalized["function_call"]["turn_id"] == "stale-turn"
    assert normalized["humanoid_reply"]["turn_id"] == "stale-turn"


def test_ninfer_dialogue_adapter_suppresses_output_without_a_claimed_turn() -> None:
    node = RealVLMNode.__new__(RealVLMNode)
    node._response_mode = "live"
    node._provider_id = "ninfer"
    node._context_mode = "actor_log"
    payload = _v6_dialogue_payload(
        function_call={
            "name": "request_tool_handover",
            "arguments": {"tool_id": "T02"},
        },
        humanoid_reply={
            "text": "Adson을 전달드리겠습니다.",
            "speak": True,
            "timing": "on_function_accepted",
        },
    )

    _raw, normalized = node._normalize_model_raw_text(
        json.dumps(payload),
        claimed_dialogue_turn_id="",
    )

    assert normalized["function_call"] is None
    assert normalized["humanoid_reply"] is None


def test_ninfer_replay_keeps_fixture_schema_strict() -> None:
    node = RealVLMNode.__new__(RealVLMNode)
    node._response_mode = "replay"
    node._provider_id = "ninfer"
    node._context_mode = "actor_log"
    payload = _v6_dialogue_payload(
        function_call=None,
        humanoid_reply=None,
    )
    payload.pop("function_call")
    payload.pop("humanoid_reply")

    with pytest.raises(
        SchemaValidationError,
        match="schema v5 is missing fields: function_call, humanoid_reply",
    ):
        node._normalize_model_raw_text(
            json.dumps(payload),
            claimed_dialogue_turn_id="41:run-1:u-1",
        )


def test_non_ninfer_provider_keeps_strict_missing_turn_id_validation() -> None:
    node = RealVLMNode.__new__(RealVLMNode)
    node._response_mode = "live"
    node._provider_id = "lmstudio"
    node._context_mode = "actor_log"
    payload = _v6_dialogue_payload(
        function_call={
            "name": "request_tool_handover",
            "arguments": {"tool_id": "T02"},
        },
        humanoid_reply={
            "text": "Adson을 전달드리겠습니다.",
            "speak": True,
            "timing": "on_function_accepted",
        },
    )

    with pytest.raises(SchemaValidationError, match="missing fields: turn_id"):
        node._normalize_model_raw_text(
            json.dumps(payload),
            claimed_dialogue_turn_id="41:run-1:u-1",
        )


def test_valid_reply_is_durable_before_dialogue_commit_and_publish() -> None:
    node = _dialogue_node()
    node._spec = SimpleNamespace(procedure_id="thyroidectomy_demo")
    node._inference_backpressure = InferenceBackpressure()
    ordering: list[str] = []
    captured = []

    class _Outbox:
        def enqueue(self, envelope):
            ordering.append("persist")
            captured.append(envelope)
            return True

    node._reply_outbox = _Outbox()
    original_commit = node._dialogue_turn_gate.commit

    def _commit(**kwargs):
        ordering.append("commit")
        return original_commit(**kwargs)

    node._dialogue_turn_gate.commit = _commit
    node._humanoid_reply_pub = SimpleNamespace(
        publish=lambda message: ordering.append("publish")
    )
    node._on_speech_utterance(_utterance("u-1", "현재 단계가 무엇인가요?"))
    turn = node._dialogue_turn_gate.next_pending()
    assert turn is not None
    assert node._dialogue_turn_gate.claim(
        turn_id=turn.turn_id,
        correlation_id="vlm-41-1",
    ) == turn

    assert node._publish_humanoid_reply(
        {
            "v": "6",
            "function_call": None,
            "humanoid_reply": {
                "turn_id": turn.turn_id,
                "text": "현재 수술 단계를 확인하고 있습니다.",
                "speak": True,
                "timing": "immediate",
            },
        },
        observation_stamp=_stamp(),
        source_epoch=41,
        source_sequence=1,
        correlation_id="vlm-41-1",
        claimed_turn_id=turn.turn_id,
    )

    assert ordering == ["persist", "commit", "publish"]
    assert captured[0].reply_id == stable_dialogue_reply_id(
        "run-1", "u-1", gateway_instance_id="gateway-1"
    )
    assert captured[0].text == "현재 수술 단계를 확인하고 있습니다."


def test_durable_collision_commits_turn_and_advances_fifo_without_republish() -> None:
    node = _dialogue_node()
    node._spec = SimpleNamespace(procedure_id="thyroidectomy_demo")
    node._inference_backpressure = InferenceBackpressure()
    published = []
    followups = []

    class _CollidingOutbox:
        def enqueue(self, envelope):
            raise ReplyCollisionError(
                reply_id=envelope.reply_id,
                procedure_run_id=envelope.procedure_run_id,
                utterance_id=envelope.utterance_id,
            )

    node._reply_outbox = _CollidingOutbox()
    node._humanoid_reply_pub = SimpleNamespace(publish=published.append)
    node._queue_dialogue_followup = lambda turn_id, retry_invalid: followups.append(
        (turn_id, retry_invalid)
    )
    node._on_speech_utterance(_utterance("u-1", "첫 번째 질문"))
    node._on_speech_utterance(_utterance("u-2", "두 번째 질문"))
    turn = node._dialogue_turn_gate.next_pending()
    assert turn is not None
    assert node._dialogue_turn_gate.claim(
        turn_id=turn.turn_id,
        correlation_id="vlm-41-1",
    ) == turn

    assert node._publish_humanoid_reply(
        {
            "v": "6",
            "function_call": None,
            "humanoid_reply": {
                "turn_id": turn.turn_id,
                "text": "첫 번째 답변",
                "speak": True,
                "timing": "immediate",
            },
        },
        observation_stamp=_stamp(),
        source_epoch=41,
        source_sequence=1,
        correlation_id="vlm-41-1",
        claimed_turn_id=turn.turn_id,
    )

    assert published == []
    assert node._dialogue_turn_gate.next_pending().utterance_id == "u-2"
    assert followups == [(turn.turn_id, False)]


def test_function_call_reply_carries_same_turn_and_canonical_arguments() -> None:
    node = _dialogue_node()
    node._spec = SimpleNamespace(procedure_id="thyroidectomy_demo")
    node._inference_backpressure = InferenceBackpressure()
    published = []
    node._humanoid_reply_pub = SimpleNamespace(publish=published.append)
    node._on_speech_utterance(_utterance("u-1", "바이폴라 주세요"))
    turn = node._dialogue_turn_gate.next_pending()
    assert turn is not None
    assert node._dialogue_turn_gate.claim(
        turn_id=turn.turn_id,
        correlation_id="vlm-41-1",
    ) == turn

    assert node._publish_humanoid_reply(
        {
            "v": "6",
            "function_call": {
                "turn_id": turn.turn_id,
                "name": "request_tool_handover",
                "arguments": {"tool_id": "T07"},
            },
            "humanoid_reply": {
                "turn_id": turn.turn_id,
                "text": "바이폴라 전달 요청을 접수하겠습니다.",
                "speak": True,
                "timing": "on_function_accepted",
            },
        },
        observation_stamp=_stamp(),
        source_epoch=41,
        source_sequence=1,
        correlation_id="vlm-41-1",
        claimed_turn_id=turn.turn_id,
    )

    assert len(published) == 1
    message = published[0]
    assert message.turn_id == turn.turn_id
    assert message.function_call_name == "request_tool_handover"
    assert message.function_arguments_json == '{"tool_id":"T07"}'
    assert message.function_request_id == ""
    assert message.timing == "on_function_accepted"


def test_unimplemented_retrieval_function_never_publishes_a_spoken_ack() -> None:
    node = _dialogue_node()
    node._spec = SimpleNamespace(procedure_id="thyroidectomy_demo")
    node._inference_backpressure = InferenceBackpressure()
    published = []
    node._humanoid_reply_pub = SimpleNamespace(publish=published.append)
    node._on_speech_utterance(_utterance("u-1", "도구를 회수해 주세요"))
    turn = node._dialogue_turn_gate.next_pending()
    assert turn is not None
    assert node._dialogue_turn_gate.claim(
        turn_id=turn.turn_id,
        correlation_id="vlm-41-1",
    ) == turn

    assert not node._publish_humanoid_reply(
        {
            "v": "6",
            "function_call": {
                "turn_id": turn.turn_id,
                "name": "request_tool_retrieval",
                "arguments": {"tool_id": "T07"},
            },
            "humanoid_reply": {
                "turn_id": turn.turn_id,
                "text": "도구를 회수하겠습니다.",
                "speak": True,
                "timing": "on_function_accepted",
            },
        },
        observation_stamp=_stamp(),
        source_epoch=41,
        source_sequence=1,
        correlation_id="vlm-41-1",
        claimed_turn_id=turn.turn_id,
    )
    assert published == []


def test_wrong_turn_id_is_not_published_and_remains_retryable() -> None:
    node = _dialogue_node()
    node._spec = SimpleNamespace(procedure_id="thyroidectomy_demo")
    node._inference_backpressure = InferenceBackpressure()
    node._last_submitted_model_input_key = "unchanged-input"
    published = []
    node._humanoid_reply_pub = SimpleNamespace(publish=published.append)
    node._on_speech_utterance(_utterance("u-1", "질문입니다"))
    turn = node._dialogue_turn_gate.next_pending()
    assert turn is not None
    assert node._dialogue_turn_gate.claim(
        turn_id=turn.turn_id,
        correlation_id="vlm-41-1",
    ) == turn

    assert not node._publish_humanoid_reply(
        {
            "v": "6",
            "function_call": None,
            "humanoid_reply": {
                "turn_id": "stale-turn",
                "text": "지연된 응답",
                "speak": True,
                "timing": "immediate",
            },
        },
        observation_stamp=_stamp(),
        source_epoch=41,
        source_sequence=1,
        correlation_id="vlm-41-1",
        claimed_turn_id=turn.turn_id,
    )

    assert published == []
    assert node._dialogue_turn_gate.next_pending() == turn
    assert node._last_submitted_model_input_key == ""
    assert (
        node._inference_backpressure.snapshot()["pending_trigger"]
        == "speech"
    )
    assert node._inference_backpressure.begin() == "speech"
    assert node._dialogue_turn_gate.claim(
        turn_id=turn.turn_id,
        correlation_id="vlm-41-2",
    ) == turn

    assert not node._publish_humanoid_reply(
        {
            "v": "6",
            "function_call": None,
            "humanoid_reply": {
                "turn_id": "still-stale-turn",
                "text": "두 번째 잘못된 응답",
                "speak": True,
                "timing": "immediate",
            },
        },
        observation_stamp=_stamp(),
        source_epoch=41,
        source_sequence=2,
        correlation_id="vlm-41-2",
        claimed_turn_id=turn.turn_id,
    )
    assert node._dialogue_turn_gate.attempt_count(turn.turn_id) == 2
    assert node._inference_backpressure.snapshot()["pending_trigger"] == ""
    assert node._inference_backpressure.complete() is None


def test_invalid_reply_upgrades_pending_visual_frame_to_speech_retry() -> None:
    node = _dialogue_node()
    node._spec = SimpleNamespace(procedure_id="thyroidectomy_demo")
    node._inference_backpressure = InferenceBackpressure()
    node._last_submitted_model_input_key = "submitted-input"
    node._humanoid_reply_pub = SimpleNamespace(publish=lambda _message: None)
    node._on_speech_utterance(_utterance("u-priority", "Adson 주세요"))
    node._inference_backpressure.queue("speech")
    assert node._inference_backpressure.begin() == "speech"
    turn = node._dialogue_turn_gate.next_pending()
    assert turn is not None
    assert node._dialogue_turn_gate.claim(
        turn_id=turn.turn_id,
        correlation_id="vlm-41-priority",
    ) == turn
    node._inference_backpressure.queue(INFERENCE_TRIGGER_SOURCE_FRAME)

    assert not node._publish_humanoid_reply(
        {
            "v": "6",
            "function_call": None,
            "humanoid_reply": None,
        },
        observation_stamp=_stamp(),
        source_epoch=41,
        source_sequence=1,
        correlation_id="vlm-41-priority",
        claimed_turn_id=turn.turn_id,
    )

    assert node._last_submitted_model_input_key == ""
    assert node._inference_backpressure.complete() == "speech"
    assert node._inference_backpressure.complete() is None


def test_gateway_run_change_clears_stale_dialogue_and_retraction_request() -> None:
    node = _dialogue_node()
    node._gateway_instance_id = "gateway-1"
    node._procedure_run_id = "run-1"
    node._model_input_epoch = 41
    node._vlm_result_sequence = 3
    node._last_submitted_model_input_key = "old-input"
    node._recent_events = deque([SimpleNamespace(event_type="old")])
    node._recent_skill_statuses = deque([{"state": "old"}], maxlen=8)
    node._completed_handover_history = deque([{"tool": "T07"}])
    node._validated_tool_request_history = deque([{"tool": "T07"}])
    node._latest_bed_robot_arm_group_request = SimpleNamespace(
        request_id="old-request"
    )
    node._last_bed_robot_arm_group_proposal_request_id = "old-request"
    node._phase_entered_wall_sec = 1.0
    node._on_speech_utterance(_utterance("u-old", "이전 run 질문"))

    node._on_gateway_info(
        _gateway_info(procedure_run_id="run-2", revision=2)
    )

    assert node._procedure_run_id == "run-2"
    assert node._model_input_epoch == 42
    assert node._dialogue_turn_gate.next_pending() is None
    assert node._latest_bed_robot_arm_group_request is None
    assert node._last_bed_robot_arm_group_proposal_request_id == ""
    assert list(node._recent_speech) == []
    assert list(node._recent_events) == []


def test_gateway_heartbeat_timeout_fences_once_and_same_scope_reactivates(
    monkeypatch,
) -> None:
    node = _dialogue_node()
    node._gateway_instance_id = "gateway-1"
    node._procedure_run_id = "run-1"
    node._gateway_info_timeout_sec = 3.0
    node._gateway_info_last_seen_monotonic = 10.0
    node._gateway_info_last_instance_id = "gateway-1"
    node._gateway_info_last_revision = 1
    node._gateway_info_last_source_stamp_ns = 9_000_000_000
    stale_calls = []
    reset_calls = []
    published = []

    class _Outbox:
        def mark_stale_outside_scope(self, gateway_instance_id, active_run):
            stale_calls.append((gateway_instance_id, active_run))
            return 1

        def pending_for_scope(self, gateway_instance_id, active_run, *, limit):
            assert gateway_instance_id == "gateway-1"
            assert active_run == "run-1"
            assert limit == 64
            return []

    node._reply_outbox = _Outbox()
    node._humanoid_reply_pub = SimpleNamespace(publish=published.append)
    node._reset_model_input_dedupe = lambda *, advance_epoch: reset_calls.append(
        ("dedupe", advance_epoch)
    )
    node._reset_public_evidence = lambda: reset_calls.append(("evidence", True))
    node.get_logger = lambda: SimpleNamespace(error=lambda *_args, **_kwargs: None)
    monkeypatch.setattr(real_vlm_module.time, "monotonic", lambda: 13.1)

    node._on_gateway_info_watchdog()
    node._on_gateway_info_watchdog()

    assert node._gateway_instance_id == ""
    assert node._procedure_run_id == ""
    assert node._gateway_info_last_seen_monotonic is None
    assert stale_calls == [("", "")]
    assert reset_calls == [("dedupe", True), ("evidence", True)]

    # The old logical IDs do not silently continue: this is a fresh receipt
    # boundary and only non-stale outbox work could be republished.
    node._on_gateway_info(_gateway_info(revision=2))
    assert node._gateway_instance_id == "gateway-1"
    assert node._procedure_run_id == "run-1"
    assert stale_calls == [("", ""), ("gateway-1", "run-1")]
    assert published == []


def test_stale_retained_or_malformed_gateway_never_opens_vlm_reply_scope() -> None:
    node = _dialogue_node()
    node._gateway_instance_id = ""
    node._procedure_run_id = ""
    node._gateway_info_timeout_sec = 3.0
    node._gateway_info_last_seen_monotonic = None
    published = []
    stale_calls = []

    class _Outbox:
        def mark_stale_outside_scope(self, gateway_instance_id, active_run):
            stale_calls.append((gateway_instance_id, active_run))
            return 0

        def pending_for_scope(
            self, _gateway_instance_id, _active_run, *, limit
        ):
            assert limit == 64
            raise AssertionError("stale/malformed scope must not recover replies")

    node._reply_outbox = _Outbox()
    node._humanoid_reply_pub = SimpleNamespace(publish=published.append)
    node._reset_model_input_dedupe = lambda **_kwargs: None
    node._reset_public_evidence = lambda: None
    node.get_logger = lambda: SimpleNamespace(error=lambda *_args, **_kwargs: None)

    node._on_gateway_info(_gateway_info(revision=1, stamp_sec=1))
    assert node._procedure_run_id == ""
    assert published == []

    node._gateway_instance_id = "gateway-old"
    node._procedure_run_id = "run-old"
    node._gateway_info_last_seen_monotonic = 9.0
    node._on_gateway_info(
        _gateway_info(
            gateway_instance_id="",
            procedure_run_id="run-1",
            revision=2,
        )
    )
    assert node._gateway_instance_id == ""
    assert node._procedure_run_id == ""
    assert stale_calls == [("", "")]
    assert published == []


def test_same_run_new_gateway_never_recovers_old_epoch_reply() -> None:
    node = _dialogue_node()
    node._gateway_instance_id = "gateway-old"
    node._procedure_run_id = "run-reused"
    node._gateway_info_timeout_sec = 3.0
    node._gateway_info_last_seen_monotonic = 9.0
    node._gateway_info_last_instance_id = "gateway-old"
    node._gateway_info_last_revision = 4
    node._gateway_info_last_source_stamp_ns = 9_000_000_000
    stale_calls = []
    recovery_calls = []

    class _Outbox:
        def mark_stale_outside_scope(self, gateway_instance_id, active_run):
            stale_calls.append((gateway_instance_id, active_run))
            return 1

        def pending_for_scope(self, gateway_instance_id, active_run, *, limit):
            recovery_calls.append((gateway_instance_id, active_run, limit))
            return []

    node._reply_outbox = _Outbox()
    node._humanoid_reply_pub = SimpleNamespace(publish=lambda _message: None)
    node._reset_model_input_dedupe = lambda **_kwargs: None
    node._reset_public_evidence = lambda: None
    node.get_logger = lambda: SimpleNamespace(error=lambda *_args, **_kwargs: None)

    node._on_gateway_info(
        _gateway_info(
            gateway_instance_id="gateway-new",
            procedure_run_id="run-reused",
            revision=1,
        )
    )

    assert stale_calls == [("gateway-new", "run-reused")]
    assert recovery_calls == [("gateway-new", "run-reused", 64)]


def test_pending_turn_survives_tight_request_context_reduction() -> None:
    pending = {
        "turn_id": "41:run-1:u-1",
        "utterance_id": "u-1",
        "text": "바이폴라 주세요",
        "speaker_role": "surgeon",
        "language": "ko-KR",
        "reply_required": True,
    }
    context = {
        "proc": "thyroidectomy_demo",
        "pending_dialogue_turn": pending,
        "evidence_window": {
            "speech": [{"text": "ambient " * 4000}],
            "skill_status": [],
        },
        "digital_twin": {"tools": [{"id": f"T{i:02d}"} for i in range(200)]},
    }

    reduced = actor_log_request_context(
        context,
        static_prompt_chars=15_000,
    )

    assert reduced["pending_dialogue_turn"] == pending


def test_v6_cache_removes_all_one_shot_outputs() -> None:
    node = RealVLMNode.__new__(RealVLMNode)
    payload = {
        "v": "6",
        "bed_robot_arm_group": {"request_id": "r-1"},
        "function_call": {"turn_id": "t-1", "name": "x", "arguments": {}},
        "humanoid_reply": {
            "turn_id": "t-1",
            "text": "답변",
            "speak": True,
            "timing": "immediate",
        },
    }

    _, cached = node._cacheable_payload("{}", payload)

    assert cached["bed_robot_arm_group"] is None
    assert cached["function_call"] is None
    assert cached["humanoid_reply"] is None
