from __future__ import annotations

import sys
from pathlib import Path


TASK_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK_DIR))

import build_runtime_tool_demand_prior as demand_prior  # noqa: E402


def test_runtime_tool_demand_artifact_is_reproducible_without_case_rows() -> None:
    rendered = demand_prior.render_payload(demand_prior.build_payload())
    artifact = (
        TASK_DIR.parents[2]
        / "src/procedure_spec/procedure_spec/specs/thyroidectomy_demo"
        / "tool_future_demand_prior.yaml"
    )

    assert artifact.read_text(encoding="utf-8") == rendered
    assert "0704_" not in rendered
    assert "time_sec" not in rendered
    assert "case_id" not in rendered


def test_runtime_tool_demand_builder_is_camera_independent_and_smoothed() -> None:
    payload = demand_prior.build_payload()
    metadata = payload["metadata"]

    assert payload["target"] == demand_prior.TARGET
    assert payload["smoothing"] == {
        "method": "beta_binomial_laplace",
        "alpha": 1.0,
        "beta": 1.0,
    }
    assert metadata["camera_observation_role"] == "excluded"
    assert metadata["mayo_location_role"] == "excluded"
    assert metadata["raw_handover_count"] == 108
    assert metadata["supported_handover_count"] == 82
    assert metadata["unsupported_handover_count"] == 26
    assert metadata["state_sample_count"] == 117


def test_runtime_tool_demand_builder_emits_all_requestable_type_probabilities() -> None:
    payload = demand_prior.build_payload()
    expected_tools = {"T02", "T04", "T07", "T08"}

    assert len(payload["rules"]) == 103
    for rule in payload["rules"]:
        assert set(rule["history"]).issubset(expected_tools)
        probabilities = rule["probabilities"]
        assert {item["tool"] for item in probabilities} == expected_tools
        assert all(0.0 < item["probability"] < 1.0 for item in probabilities)
