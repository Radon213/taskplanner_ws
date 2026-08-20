from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


MODULE_PATH = Path(__file__).with_name("audit_0704_mayo_coverage.py")
SPEC = importlib.util.spec_from_file_location("mayo_coverage_audit", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


def test_only_labeled_case_with_local_cam4_is_accuracy_eligible():
    report = module.audit()
    assert report["summary"]["accuracy_eligible_cases"] == ["0704_5"]
    assert report["summary"]["cross_case_holdout_possible"] is False
    assert report["summary"]["raw_cam4_video_covered_case_count"] >= 1
    eligible = next(case for case in report["cases"] if case["case_id"] == "0704_5")
    assert eligible["exact_source_frame_mapping_valid"] is True
    assert eligible["unmapped_confirmed_mayo_source_frames"] == []
    excluded = [case for case in report["cases"] if case["case_id"] != "0704_5"]
    assert all(case["accuracy_eligible"] is False for case in excluded)
    assert all(case["negative_eligible"] is False for case in excluded)
