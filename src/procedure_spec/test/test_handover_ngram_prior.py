from __future__ import annotations

from pathlib import Path

import pytest

from procedure_spec import (
    FrozenHandoverNgramPrior,
    HandoverNgramPriorError,
    load_bundle,
    load_frozen_handover_ngram_prior,
)


def _spec_root() -> Path:
    return Path(__file__).parents[1] / "procedure_spec" / "specs"


def _demo_prior() -> FrozenHandoverNgramPrior:
    bundle_dir = _spec_root() / "thyroidectomy_demo"
    prior = load_frozen_handover_ngram_prior(load_bundle(bundle_dir), bundle_dir)
    assert prior is not None
    return prior


def test_demo_artifact_returns_phase_and_suffix_matched_distribution() -> None:
    prediction = _demo_prior().predict(
        phase_id="P03",
        completed_handovers=[
            {"tool": "T02", "at": 1.0},
            {"tool": "T02", "at": 2.0},
            {"tool": "T04", "at": 3.0},
        ],
    )

    assert prediction == {
        "id": "thyroidectomy_demo_handover_ngram_calibration_v1",
        "match": "phase+last3",
        "support": 3,
        "candidates": [["T07", 1.0]],
    }


def test_demo_artifact_preserves_start_state_and_unknown_boundary() -> None:
    prior = _demo_prior()

    start = prior.predict(phase_id="P03", completed_handovers=[])
    after_unknown = prior.predict(
        phase_id="P03",
        completed_handovers=["T02", "T04", "T07", "T10"],
    )

    assert start == after_unknown
    assert start == {
        "id": "thyroidectomy_demo_handover_ngram_calibration_v1",
        "match": "phase+last3",
        "support": 15,
        "candidates": [["T02", 0.6], ["T07", 0.333], ["T05", 0.067]],
    }


def test_prediction_is_compact_and_cannot_mutate_the_precompiled_lookup() -> None:
    prior = _demo_prior()
    first = prior.predict(phase_id="P04", completed_handovers=["T05", "T05", "T02"])
    assert first is not None
    first["candidates"][0][0] = "T01"

    second = prior.predict(phase_id="P04", completed_handovers=["T05", "T05", "T02"])

    assert second == {
        "id": "thyroidectomy_demo_handover_ngram_calibration_v1",
        "match": "phase+last3",
        "support": 9,
        "candidates": [
            ["T08", 0.444],
            ["T07", 0.333],
            ["T02", 0.111],
            ["T03", 0.111],
        ],
    }
    assert set(second) == {"id", "match", "support", "candidates"}


def test_loader_is_optional_for_procedures_without_an_artifact() -> None:
    bundle_dir = _spec_root() / "thyroidectomy"
    assert load_frozen_handover_ngram_prior(load_bundle(bundle_dir), bundle_dir) is None


def test_prior_rejects_a_wrong_procedure_or_nonrequestable_tool() -> None:
    spec = load_bundle(_spec_root() / "thyroidectomy_demo")
    payload = {
        "schema": "taskplanner.frozen_handover_ngram_prior.v1",
        "id": "test",
        "procedure_id": "another_procedure",
        "rules": [
            {
                "match": "global",
                "history": [],
                "outcomes": [{"tool": "T02", "count": 1}],
            }
        ],
    }

    with pytest.raises(HandoverNgramPriorError, match="procedure_id"):
        FrozenHandoverNgramPrior(spec, payload)
    payload["procedure_id"] = "thyroidectomy_demo"
    payload["rules"][0]["outcomes"][0]["tool"] = "T10"
    with pytest.raises(HandoverNgramPriorError, match="non-requestable"):
        FrozenHandoverNgramPrior(spec, payload)
