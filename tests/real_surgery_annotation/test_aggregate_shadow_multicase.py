import importlib.util
from pathlib import Path
import sys


def _load_module():
    path = (
        Path(__file__).resolve().parents[2]
        / "tools"
        / "real_surgery_annotation"
        / "aggregate_shadow_multicase.py"
    )
    spec = importlib.util.spec_from_file_location("aggregate_shadow_multicase", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_distribution_uses_nearest_rank_p95():
    module = _load_module()

    result = module._distribution([0.1, 0.2, 0.3, 0.4, 1.0])

    assert result == {
        "count": 5,
        "mean": 0.4,
        "median": 0.3,
        "p95": 1.0,
        "max": 1.0,
    }


def test_aggregate_score_reports_micro_and_macro():
    module = _load_module()
    score_a = module.Score("metric", "layer", 1, 1, 1.0, "complete", "ref")
    score_b = module.Score("metric", "layer", 1, 3, 1 / 3, "complete", "ref")

    class Case:
        def __init__(self, score):
            self._score = score

        def score(self, _metric, _layer):
            return self._score

    result = module.aggregate_score([Case(score_a), Case(score_b)], "metric", "layer")

    assert result["correct_count"] == 2
    assert result["evaluated_count"] == 4
    assert result["micro_accuracy"] == 0.5
    assert result["macro_accuracy"] == 2 / 3


def test_report_template_does_not_hardcode_twelve_case_results():
    source = (
        Path(__file__).resolve().parents[2]
        / "tools"
        / "real_surgery_annotation"
        / "aggregate_shadow_multicase.py"
    ).read_text(encoding="utf-8")

    assert "실행 성공: **12/12**" not in source
    assert "inventory_ok}/12 run" not in source
