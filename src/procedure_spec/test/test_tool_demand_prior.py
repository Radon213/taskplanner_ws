from __future__ import annotations

from pathlib import Path

import pytest

from procedure_spec import (
    FrozenToolDemandPrior,
    ToolDemandPriorError,
    load_bundle,
    load_frozen_tool_demand_prior,
)


def _spec_root() -> Path:
    return Path(__file__).parents[1] / "procedure_spec" / "specs"


def _demo_prior() -> FrozenToolDemandPrior:
    bundle_dir = _spec_root() / "thyroidectomy_demo"
    prior = load_frozen_tool_demand_prior(load_bundle(bundle_dir), bundle_dir)
    assert prior is not None
    return prior


def test_demo_prior_returns_phase_and_suffix_conditioned_type_probabilities() -> None:
    prediction = _demo_prior().predict(
        phase_id="P04",
        completed_handovers=["T02"],
    )

    assert prediction == {
        "id": "thyroidectomy_demo_future_tool_demand_calibration_v1",
        "match": "phase+last3",
        "support": 9,
        "probabilities": [
            ["T02", 0.1818],
            ["T04", 0.9091],
            ["T07", 0.8182],
            ["T08", 0.8182],
        ],
    }


def test_unknown_handover_is_a_boundary_and_start_state_is_preserved() -> None:
    prior = _demo_prior()

    start = prior.predict(phase_id="P03", completed_handovers=[])
    after_unknown = prior.predict(
        phase_id="P03",
        completed_handovers=["T02", "T04", "T07", "T10"],
    )

    assert after_unknown == start
    assert start == {
        "id": "thyroidectomy_demo_future_tool_demand_calibration_v1",
        "match": "phase+last3",
        "support": 25,
        "probabilities": [
            ["T02", 0.963],
            ["T04", 0.963],
            ["T07", 0.8889],
            ["T08", 0.8519],
        ],
    }


def test_prediction_is_compact_and_cannot_mutate_the_frozen_lookup() -> None:
    prior = _demo_prior()
    first = prior.predict(phase_id="P04", completed_handovers=["T02"])
    assert first is not None
    first["probabilities"][0][1] = 0.0

    second = prior.predict(phase_id="P04", completed_handovers=["T02"])

    assert second is not None
    assert second["probabilities"][0] == ["T02", 0.1818]
    assert set(second) == {"id", "match", "support", "probabilities"}


def test_loader_is_optional_for_procedures_without_an_artifact() -> None:
    bundle_dir = _spec_root() / "thyroidectomy"
    assert load_frozen_tool_demand_prior(load_bundle(bundle_dir), bundle_dir) is None


def test_prior_rejects_wrong_target_and_incomplete_probability_coverage() -> None:
    spec = load_bundle(_spec_root() / "thyroidectomy_demo")
    payload = {
        "schema": "taskplanner.frozen_tool_future_demand_prior.v1",
        "id": "test",
        "procedure_id": "thyroidectomy_demo",
        "target": "wrong",
        "smoothing": {"method": "beta_binomial_laplace", "alpha": 1, "beta": 1},
        "rules": [
            {
                "match": "global",
                "history": [],
                "support": 1,
                "probabilities": [
                    {"tool": "T02", "probability": 0.5},
                ],
            }
        ],
    }

    with pytest.raises(ToolDemandPriorError, match="target"):
        FrozenToolDemandPrior(spec, payload)
    payload["target"] = (
        "at_least_one_future_supported_scrub_nurse_to_surgeon_handover_"
        "before_procedure_end"
    )
    with pytest.raises(ToolDemandPriorError, match="cover every requestable"):
        FrozenToolDemandPrior(spec, payload)
