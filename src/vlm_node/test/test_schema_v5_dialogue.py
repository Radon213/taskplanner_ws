from __future__ import annotations

import copy

import pytest

from vlm_node.schema import SchemaValidationError, compact_vlm_json_schema, validate_payload


def _base_v5() -> dict:
    return {
        "v": "5",
        "phase": [["P04", 0.91]],
        "tool": [["T05", 0.84]],
        "intent": ["", "", 0.0],
        "mayo": [],
        "mayo_retrieve": ["", 0.0],
        "u": 0.09,
        "sum": "surgeon speech observed",
        "bed_robot_arm_group": None,
        "function_call": None,
        "humanoid_reply": None,
    }


def _base_v6() -> dict:
    payload = _base_v5()
    payload["v"] = "6"
    return payload


def _function_call(**overrides: object) -> dict:
    value = {
        "turn_id": "run-42:utterance-73",
        "name": "request_tool_handover",
        "arguments": {"tool_id": "T05"},
    }
    value.update(overrides)
    return value


def _humanoid_reply(**overrides: object) -> dict:
    value = {
        "turn_id": "run-42:utterance-73",
        "text": "바이폴라 전달드리겠습니다.",
        "speak": True,
        "timing": "on_function_accepted",
    }
    value.update(overrides)
    return value


def test_v5_extends_v4_and_accepts_explicit_null_dialogue_fields() -> None:
    normalized = validate_payload(_base_v5())

    assert normalized["v"] == "5"
    assert "gesture" not in normalized
    assert normalized["bed_robot_arm_group"] is None
    assert normalized["function_call"] is None
    assert normalized["humanoid_reply"] is None


def test_v5_normalizes_matching_function_call_and_humanoid_reply() -> None:
    payload = _base_v5()
    payload["function_call"] = _function_call(
        turn_id="  run-42:utterance-73  ",
        name="  request_tool_handover  ",
    )
    payload["humanoid_reply"] = _humanoid_reply(
        turn_id="  run-42:utterance-73  ",
        text="  바이폴라 전달드리겠습니다.  ",
    )

    normalized = validate_payload(payload)

    assert normalized["function_call"] == {
        "turn_id": "run-42:utterance-73",
        "name": "request_tool_handover",
        "arguments": {"tool_id": "T05"},
    }
    assert normalized["humanoid_reply"] == {
        "turn_id": "run-42:utterance-73",
        "text": "바이폴라 전달드리겠습니다.",
        "speak": True,
        "timing": "on_function_accepted",
    }


def test_v5_allows_independent_function_call_or_reply() -> None:
    function_only = _base_v5()
    function_only["function_call"] = _function_call()
    assert validate_payload(function_only)["humanoid_reply"] is None

    reply_only = _base_v5()
    reply_only["humanoid_reply"] = _humanoid_reply(timing="immediate")
    assert validate_payload(reply_only)["function_call"] is None


@pytest.mark.parametrize("field", ["function_call", "humanoid_reply"])
def test_v5_requires_explicit_nullable_dialogue_fields(field: str) -> None:
    payload = _base_v5()
    payload.pop(field)

    with pytest.raises(SchemaValidationError, match="schema v5 is missing fields"):
        validate_payload(payload)


@pytest.mark.parametrize(
    ("factory", "field"),
    [
        (_function_call, "turn_id"),
        (_function_call, "name"),
        (_humanoid_reply, "turn_id"),
    ],
)
def test_v5_rejects_empty_identifiers(factory, field: str) -> None:
    payload = _base_v5()
    target = "function_call" if factory is _function_call else "humanoid_reply"
    payload[target] = factory(**{field: "   "})

    with pytest.raises(SchemaValidationError, match="must be non-empty"):
        validate_payload(payload)


@pytest.mark.parametrize(
    ("target", "factory", "removed_field"),
    [
        ("function_call", _function_call, "arguments"),
        ("humanoid_reply", _humanoid_reply, "timing"),
    ],
)
def test_v5_rejects_missing_nested_fields(target: str, factory, removed_field: str) -> None:
    payload = _base_v5()
    value = factory()
    value.pop(removed_field)
    payload[target] = value

    with pytest.raises(SchemaValidationError, match="is missing fields"):
        validate_payload(payload)


@pytest.mark.parametrize(
    ("target", "factory"),
    [
        ("function_call", _function_call),
        ("humanoid_reply", _humanoid_reply),
    ],
)
def test_v5_rejects_extra_nested_fields(target: str, factory) -> None:
    payload = _base_v5()
    payload[target] = factory(unexpected="not allowed")

    with pytest.raises(SchemaValidationError, match="has unsupported fields"):
        validate_payload(payload)


def test_v5_rejects_non_object_function_arguments() -> None:
    payload = _base_v5()
    payload["function_call"] = _function_call(arguments=["T05"])

    with pytest.raises(SchemaValidationError, match="arguments must be an object"):
        validate_payload(payload)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"text": "가" * 241}, "at most 240"),
        ({"text": 123}, "text must be a string"),
        ({"speak": 1}, "speak must be a boolean"),
        ({"timing": "after_delay"}, "timing must be"),
    ],
)
def test_v5_rejects_invalid_humanoid_reply_fields(
    overrides: dict[str, object], message: str
) -> None:
    payload = _base_v5()
    payload["humanoid_reply"] = _humanoid_reply(**overrides)

    with pytest.raises(SchemaValidationError, match=message):
        validate_payload(payload)


def test_v5_rejects_different_turn_ids_in_coupled_outputs() -> None:
    payload = _base_v5()
    payload["function_call"] = _function_call(turn_id="turn-a")
    payload["humanoid_reply"] = _humanoid_reply(turn_id="turn-b")

    with pytest.raises(SchemaValidationError, match="turn_id must match"):
        validate_payload(payload)


def test_v5_rejects_unsupported_top_level_fields_without_changing_v4() -> None:
    payload = _base_v5()
    payload["unexpected"] = "not allowed"
    with pytest.raises(SchemaValidationError, match="unsupported fields"):
        validate_payload(payload)

    v4_payload = copy.deepcopy(_base_v5())
    v4_payload["v"] = "4"
    v4_payload.pop("function_call")
    v4_payload.pop("humanoid_reply")
    assert validate_payload(v4_payload)["v"] == "4"


def test_v5_json_schema_declares_strict_nullable_dialogue_contracts() -> None:
    schema = compact_vlm_json_schema("5")

    assert schema["properties"]["v"] == {"type": "string", "enum": ["5"]}
    assert schema["additionalProperties"] is False
    assert schema["required"][-2:] == ["function_call", "humanoid_reply"]

    function_schema = schema["properties"]["function_call"]["anyOf"][1]
    assert function_schema["required"] == ["turn_id", "name", "arguments"]
    assert function_schema["properties"]["arguments"] == {"type": "object"}
    assert function_schema["additionalProperties"] is False

    reply_schema = schema["properties"]["humanoid_reply"]["anyOf"][1]
    assert reply_schema["required"] == ["turn_id", "text", "speak", "timing"]
    assert reply_schema["properties"]["text"]["maxLength"] == 240
    assert reply_schema["properties"]["timing"]["enum"] == [
        "immediate",
        "on_function_accepted",
        "on_function_completed",
    ]
    assert reply_schema["additionalProperties"] is False


def test_v6_is_gesture_free_and_preserves_dialogue_contracts() -> None:
    normalized = validate_payload(_base_v6())
    schema = compact_vlm_json_schema("6")

    assert normalized["v"] == "6"
    assert "gesture" not in normalized
    assert schema["properties"]["v"] == {"type": "string", "enum": ["6"]}
    assert "gesture" not in schema["properties"]
    assert {"function_call", "humanoid_reply"}.issubset(schema["required"])


def test_v6_rejects_retired_gesture_field() -> None:
    payload = _base_v6()
    payload["gesture"] = ["request_tool", "", "open_receive", 0.9]

    with pytest.raises(
        SchemaValidationError,
        match="retired VLM hand output fields: gesture",
    ):
        validate_payload(payload)
