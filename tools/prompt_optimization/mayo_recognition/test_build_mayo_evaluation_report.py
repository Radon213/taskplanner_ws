from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest


MODULE_PATH = Path(__file__).with_name("build_mayo_evaluation_report.py")
SPEC = importlib.util.spec_from_file_location("mayo_report_builder", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


def _result(*, prediction: str, complete: bool = True) -> dict:
    records = []
    for sample_id in sorted(module.FROZEN_SAMPLE_IDS):
        records.append(
            {
                "input": {"sample_id": sample_id, "mode": "arrival"},
                "evaluation_reference": "bovie",
                "request_error": "",
                "score": {
                    "valid_json": True,
                    "contract_valid": True,
                    "transport_error": False,
                    "not_inferred": False,
                    "target_recalled": prediction == "bovie",
                    "false_positives": [],
                    "exact": prediction == "bovie",
                    "predicted": [prediction],
                },
            }
        )
    return {
        "suite": "frozen_arrival",
        "dry_run": False,
        "source": {"event_reference_sha256": "same-reference"},
        "execution": {
            "status": "completed" if complete else "halted",
            "unexecuted_sample_ids": [] if complete else ["remaining"],
            "lifecycle_invoked": complete,
            "batches": (
                [
                    {
                        "status": "completed",
                        "sample_ids": sorted(module.FROZEN_SAMPLE_IDS)[:3],
                        "inference_http_request_count": 3,
                        "lifecycle": {"status": "ready"},
                        "post_batch_readiness": {"manager_loaded": True, "direct_worker_ready": True},
                    },
                    {
                        "status": "completed",
                        "sample_ids": sorted(module.FROZEN_SAMPLE_IDS)[3:],
                        "inference_http_request_count": 2,
                        "lifecycle": {"status": "ready"},
                        "post_batch_readiness": {"manager_loaded": True, "direct_worker_ready": True},
                    },
                ]
                if complete
                else []
            ),
        },
        "records": records,
    }


def test_complete_frozen_pair_builds_report():
    baseline = _result(prediction="bovie")
    optimized = _result(prediction="bipolar_forceps")
    report = module.build_report(
        coverage={"summary": {"case_count": 13, "accuracy_eligible_cases": ["0704_5"], "cross_case_holdout_possible": False}},
        baseline=baseline,
        optimized=optimized,
        baseline_path=Path("baseline.json"),
        optimized_path=Path("optimized.json"),
        baseline_review="",
        optimized_review="",
    )
    assert "Mayo prompt frozen-challenge report" in report
    assert "0704_5-challenge-arrival-0704_5-E0016" in report


def test_halted_or_incomplete_result_is_not_reportable():
    with pytest.raises(module.ReportError, match="did not complete"):
        module.validate_comparison(_result(prediction="bovie", complete=False), _result(prediction="bovie"))


def test_frozen_result_without_fresh_worker_evidence_is_not_reportable():
    baseline = _result(prediction="bovie")
    baseline["execution"]["lifecycle_invoked"] = False
    with pytest.raises(module.ReportError, match="fresh-worker lifecycle guard"):
        module.validate_comparison(baseline, _result(prediction="bovie"))
