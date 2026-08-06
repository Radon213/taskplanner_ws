from __future__ import annotations

import csv
import importlib.util
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "evaluate_release_metrics",
    ROOT / "scripts" / "evaluate_release_metrics.py",
)
assert SPEC is not None and SPEC.loader is not None
metrics = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = metrics
SPEC.loader.exec_module(metrics)
CORE_METRICS = metrics.CORE_METRICS


def _write_aggregate(path: Path, accuracy: float) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=(
                "metric",
                "layer",
                "correct_count",
                "evaluated_count",
                "micro_accuracy",
                "macro_accuracy",
                "case_count",
            ),
        )
        writer.writeheader()
        for metric, layer in CORE_METRICS:
            writer.writerow(
                {
                    "metric": metric,
                    "layer": layer,
                    "correct_count": 98,
                    "evaluated_count": 100,
                    "micro_accuracy": accuracy,
                    "macro_accuracy": accuracy,
                    "case_count": 1,
                }
            )


def _write_case(path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=(
                "status",
                "strict_boundary_ok",
                "input_integrity_ok",
                "invariant_violation_count",
                "commands_after_completion",
                "command_correct",
                "command_total",
            ),
        )
        writer.writeheader()
        writer.writerow(
            {
                "status": "complete",
                "strict_boundary_ok": True,
                "input_integrity_ok": True,
                "invariant_violation_count": 0,
                "commands_after_completion": 0,
                "command_correct": 3,
                "command_total": 3,
            }
        )


def _fixture(tmp_path: Path, *, candidate_accuracy: float = 0.98) -> tuple[Path, Path]:
    candidate = tmp_path / "candidate" / "report"
    baseline = tmp_path / "baseline" / "report"
    candidate.mkdir(parents=True)
    baseline.mkdir(parents=True)
    _write_aggregate(candidate / "aggregate_metrics.csv", candidate_accuracy)
    _write_aggregate(baseline / "aggregate_metrics.csv", 0.98)
    _write_case(candidate / "case_metrics.csv")
    trace_dir = candidate.parent / "runs" / "case-a"
    trace_dir.mkdir(parents=True)
    trace = {
        "layer": "vlm_health",
        "payload": {
            "healthy": True,
            "prompt_chars": 15_900,
            "latency_sec": 0.8,
        },
    }
    (trace_dir / "shadow_trace.v1.jsonl").write_text(
        json.dumps(trace) + "\n",
        encoding="utf-8",
    )
    return candidate, baseline


def test_release_metric_gate_passes_clean_candidate(tmp_path: Path) -> None:
    candidate, baseline = _fixture(tmp_path)

    results, health = metrics.evaluate(
        candidate_report_dir=candidate,
        baseline_report_dir=baseline,
        max_regression_pp=2.0,
        prompt_chars_max=16_000,
        vlm_p95_max_sec=1.0,
    )

    assert all(result.status == "passed" for result in results if result.required)
    assert health["unhealthy_count"] == 0


def test_release_metric_gate_fails_regression_and_unhealthy_vlm(tmp_path: Path) -> None:
    candidate, baseline = _fixture(tmp_path, candidate_accuracy=0.90)
    trace_path = candidate.parent / "runs" / "case-a" / "shadow_trace.v1.jsonl"
    trace_path.write_text(
        json.dumps(
            {
                "layer": "vlm_health",
                "payload": {
                    "healthy": False,
                    "last_mode": "inference_failed:periodic:transport",
                    "prompt_chars": 16_001,
                    "latency_sec": 1.2,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    results, _ = metrics.evaluate(
        candidate_report_dir=candidate,
        baseline_report_dir=baseline,
        max_regression_pp=2.0,
        prompt_chars_max=16_000,
        vlm_p95_max_sec=1.0,
    )

    failed = {result.gate for result in results if result.status == "failed"}
    assert "vlm:provider_or_inference_failures" in failed
    assert "vlm:prompt_chars_max" in failed
    assert any(gate.startswith("accuracy:") for gate in failed)


def test_release_metric_gate_reports_visual_input_degradation_separately(
    tmp_path: Path,
) -> None:
    candidate, baseline = _fixture(tmp_path)
    trace_path = candidate.parent / "runs" / "case-a" / "shadow_trace.v1.jsonl"
    records = [
        {
            "layer": "vlm_health",
            "payload": {
                "healthy": True,
                "last_mode": "json_schema",
                "prompt_chars": 15_900,
                "latency_sec": 0.8,
            },
        },
        {
            "layer": "fault_injection_status",
            "payload": {
                "scenario_id": "camera-gap",
                "seed": 7,
                "counters": {
                    "flir": {"received": 9, "dropped": 2}
                },
            },
        },
        {
            "layer": "vlm_health",
            "payload": {
                "healthy": False,
                "last_mode": "missing_visual_input",
                "prompt_chars": 15_900,
                "latency_sec": 0.0,
            },
        },
    ]
    trace_path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )

    results, health = metrics.evaluate(
        candidate_report_dir=candidate,
        baseline_report_dir=baseline,
        max_regression_pp=2.0,
        prompt_chars_max=16_000,
        vlm_p95_max_sec=1.0,
    )

    assert all(result.status == "passed" for result in results if result.required)
    assert health["unhealthy_count"] == 1
    assert health["visual_input_unavailable_count"] == 1
    assert health["inference_unhealthy_count"] == 0
    assert health["fault_scenario_ids"] == ["camera-gap"]
    assert health["fault_counters"]["flir"]["dropped"] == 2


def test_release_metric_gate_writes_machine_and_visual_reports(tmp_path: Path) -> None:
    candidate, baseline = _fixture(tmp_path)
    results, health = metrics.evaluate(
        candidate_report_dir=candidate,
        baseline_report_dir=baseline,
        max_regression_pp=2.0,
        prompt_chars_max=16_000,
        vlm_p95_max_sec=1.0,
    )
    output = tmp_path / "gate"

    payload = metrics.write_outputs(
        output_dir=output,
        candidate_report_dir=candidate,
        baseline_report_dir=baseline,
        results=results,
        trace_health=health,
    )

    assert payload["status"] == "passed"
    for name in (
        "release_metric_gate.json",
        "release_metric_gate.csv",
        "release_metric_gate.md",
        "release_metric_gate.svg",
    ):
        assert (output / name).is_file()


def test_safety_only_keeps_accuracy_and_inference_faults_advisory(
    tmp_path: Path,
) -> None:
    candidate, baseline = _fixture(tmp_path, candidate_accuracy=0.70)
    trace_path = candidate.parent / "runs" / "case-a" / "shadow_trace.v1.jsonl"
    trace_path.write_text(
        json.dumps(
            {
                "layer": "vlm_health",
                "payload": {
                    "healthy": False,
                    "last_mode": "vlm_timeout",
                    "prompt_chars": 15_900,
                    "latency_sec": 0.0,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    results, _ = metrics.evaluate(
        candidate_report_dir=candidate,
        baseline_report_dir=baseline,
        max_regression_pp=10.0,
        prompt_chars_max=16_000,
        vlm_p95_max_sec=1.0,
        safety_only=True,
    )

    assert all(result.status == "passed" for result in results if result.required)
    assert any(
        result.gate.startswith("accuracy:") and result.status == "warning"
        for result in results
    )
    assert any(
        result.gate == "vlm:provider_or_inference_failures"
        and result.status == "warning"
        for result in results
    )
