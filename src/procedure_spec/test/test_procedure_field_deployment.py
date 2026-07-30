from __future__ import annotations

from pathlib import Path

from procedure_spec import load_bundle
from procedure_spec.prompt_bundle import _phase_field_deployed_tools


def _demo_spec():
    return load_bundle(
        Path(__file__).parents[1]
        / "procedure_spec"
        / "specs"
        / "thyroidectomy_demo"
    )


def test_field_deployed_tools_are_derived_from_semantic_tool_roles() -> None:
    prompt = {
        "phase_details": {
            "P01": {
                "tool_roles": {
                    "fixed_retraction": ["T01"],
                    "field_deployed": ["T02"],
                    "tissue_handling": ["T03"],
                }
            }
        }
    }

    assert _phase_field_deployed_tools(
        prompt,
        {"T01", "T02", "T03"},
    ) == {"P01": ["T01", "T02"]}


def test_demo_bundle_exposes_fixed_retractors_as_field_deployed() -> None:
    spec = _demo_spec()

    assert spec.get_field_deployed_instruments("P03") == []
    assert spec.get_field_deployed_instruments("P04") == ["T05", "T11"]
    assert spec.is_field_deployed_instrument("P04", "T05#2") is True
    assert spec.is_field_deployed_instrument("P04", "T02#1") is False
