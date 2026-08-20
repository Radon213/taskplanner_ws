from __future__ import annotations

import sys
from pathlib import Path


TASK_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK_DIR))

from select_calibration import SUITABILITY_GUARD, suitability_assessment  # noqa: E402


def _metrics(*, recall: float, f1: float, specificity: float) -> dict:
    return {
        "accuracy": 0.80,
        "exact_top1_recall": recall,
        "f1": f1,
        "specificity": specificity,
        "balanced_accuracy": 0.50,
    }


def test_suitability_gate_rejects_accuracy_only_none_degeneracy() -> None:
    gate = suitability_assessment(_metrics(recall=0.02, f1=0.03, specificity=1.0))
    assert gate["status"] == "fail"
    assert set(gate["failed_criteria"]) == {"exact_top1_recall", "f1"}
    assert gate["criteria"]["specificity"] == SUITABILITY_GUARD["minimum_none_specificity"]


def test_suitability_gate_requires_specificity_alongside_recall_and_f1() -> None:
    gate = suitability_assessment(_metrics(recall=0.20, f1=0.20, specificity=0.89))
    assert gate["status"] == "fail"
    assert gate["failed_criteria"] == ["specificity"]
    passed = suitability_assessment(_metrics(recall=0.20, f1=0.20, specificity=0.95))
    assert passed["status"] == "pass"
