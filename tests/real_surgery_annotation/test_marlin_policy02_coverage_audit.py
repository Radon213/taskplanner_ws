from pathlib import Path

from tools.real_surgery_annotation.audit_marlin_policy02_coverage import (
    DEFAULT_CASES,
    build_report,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_actual_full_scan_clip_union_covers_every_observable_segment() -> None:
    report = build_report(REPO_ROOT, DEFAULT_CASES)

    assert report["ok"] is True
    assert report["counts"] == {
        "case_count": 11,
        "passed_case_count": 11,
        "failed_case_count": 0,
        "scan_run_count": 15,
        "completed_anchor_count": 115,
    }
    by_case = {item["case_id"]: item for item in report["cases"]}
    assert by_case["0704_8"]["run_count"] == 3
    assert by_case["0704_14"]["run_count"] == 3
    for case in by_case.values():
        assert case["ok"] is True
        assert case["coverage"]["observable_coverage_ratio"] == 1.0
        assert case["uncovered_intervals"] == []
