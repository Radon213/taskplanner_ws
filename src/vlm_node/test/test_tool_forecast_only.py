from __future__ import annotations

import json
from pathlib import Path

import pytest
from procedure_spec import compact_procedure_prompt, load_bundle
from vlm_node.real_vlm import (
    RealVLMNode,
    SchemaValidationError,
    compact_actor_log_procedure_context,
    compact_tool_forecast_json_schema,
    normalize_tool_forecast_raw_text,
)


def _demo_node() -> RealVLMNode:
    spec_dir = (
        Path(__file__).parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
        / "thyroidectomy_demo"
    )
    node = RealVLMNode.__new__(RealVLMNode)
    node._spec = load_bundle(spec_dir)
    node._procedure_prompt = compact_procedure_prompt(spec_dir)
    return node


def test_tool_only_prompt_reuses_exact_normal_procedure_context() -> None:
    node = _demo_node()
    expected = json.dumps(
        compact_actor_log_procedure_context(node._spec, node._procedure_prompt),
        separators=(",", ":"),
    )

    normal = node._actor_log_system_prompt()
    tool_only = node._tool_forecast_only_system_prompt()

    assert f"Procedure context: {expected}" in normal
    assert f"Procedure context: {expected}" in tool_only


def test_tool_only_instruction_has_only_forecast_response_fields() -> None:
    prompt = _demo_node()._tool_forecast_only_developer_instruction()

    assert '{"tool":[["Txx",0.0]],"u":0.0}' in prompt
    assert "Emit no phase, intent, gesture, Mayo, summary" in prompt
    assert "2-8 seconds" not in prompt or "2-8" in _demo_node()._tool_forecast_only_system_prompt()


def test_tool_only_response_is_adapted_without_fabricated_observations() -> None:
    raw, payload = normalize_tool_forecast_raw_text(
        '{"tool":[["T04",0.82],["T07",0.41]],"u":0.24}'
    )

    assert json.loads(raw) == payload
    assert payload["v"] == "4"
    assert payload["phase"] == []
    assert payload["tool"] == [["T04", 0.82], ["T07", 0.41]]
    assert payload["intent"] == ["none", "", 0.0]
    assert payload["gesture"] == ["", "", "", 0.0]
    assert payload["mayo"] == []
    assert payload["bed_robot_arm_group"] is None


@pytest.mark.parametrize(
    "raw",
    [
        '{"tool":[],"u":0.2}',
        '{"tool":[["T04",0.8],["T04",0.7]],"u":0.2}',
        '{"tool":[["T04",1.2]],"u":0.2}',
        '{"tool":[["T04",0.8]],"u":0.2,"phase":[]}',
    ],
)
def test_tool_only_response_rejects_non_contract_output(raw: str) -> None:
    with pytest.raises(SchemaValidationError):
        normalize_tool_forecast_raw_text(raw)


def test_tool_only_json_schema_is_two_field_contract() -> None:
    schema = compact_tool_forecast_json_schema()

    assert schema["required"] == ["tool", "u"]
    assert set(schema["properties"]) == {"tool", "u"}
    assert schema["additionalProperties"] is False
