from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent


def _load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


evaluator = _load_module("mayo_prompt_eval", "mayo_prompt_eval.py")
selection_builder = _load_module("mayo_frozen_selection", "mayo_frozen_selection.py")
report_builder = _load_module("build_mayo_selected_frozen_report", "build_mayo_selected_frozen_report.py")


def _samples():
    return [
        evaluator.Sample(
            sample_id=f"0704_5-challenge-arrival-{event_id}",
            mode="arrival",
            frame_indices=(ordinal, ordinal + 1),
            expected="bovie",
        )
        for ordinal, event_id in enumerate(evaluator.FROZEN_CHALLENGE_EVENT_IDS)
    ]


def _calibration(*, variant: str, event_hash: str, crop_correct: int, arrival_recall: float, arrival_fp: int):
    return {
        "suite": "calibration",
        "variant": variant,
        "model": "qwen3.6-35b-a3b",
        "prompt_version": evaluator.prompt_version_for(variant),
        "source": {"event_reference_sha256": event_hash},
        "execution": {"status": "completed", "unexecuted_sample_ids": [], "inference_http_request_count": 14},
        "scoring": {"performed": True},
        "normalizer_validation": {"all_request_image_integrity_checks_passed": True},
        "image_policy": {
            "preprocessor": evaluator.image_preprocess_policy(evaluator.IMAGE_PREPROCESS_LETTERBOX_512_Q95)
        },
        "summary": {
            "arrival": {
                "target_recall": arrival_recall,
                "false_positive_total": arrival_fp,
                "accepted_target_recall": arrival_recall,
            },
            "crop": {"correct": crop_correct, "accuracy": crop_correct / 11},
        },
        "records": [{"score": {"contract_valid": True}} for _ in range(14)],
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_selection_records_arrival_choice_and_explicit_crop_regression(tmp_path):
    event_hash = "event-reference-hash"
    baseline_path = tmp_path / "baseline.json"
    v4_path = tmp_path / "v4.json"
    baseline = _calibration(
        variant="baseline", event_hash=event_hash, crop_correct=7, arrival_recall=0.0, arrival_fp=1
    )
    v4 = _calibration(
        variant="optimized_v4", event_hash=event_hash, crop_correct=5, arrival_recall=0.5, arrival_fp=0
    )
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    v4_path.write_text(json.dumps(v4), encoding="utf-8")
    selection = selection_builder.build_selection(
        baseline=baseline,
        v4=v4,
        baseline_path=baseline_path,
        v4_path=v4_path,
        frozen_samples=_samples(),
        event_reference_sha256=event_hash,
    )
    evidence = selection["calibration_selection_evidence"]
    assert selection["selection_status"] == "locked"
    assert evidence["temporal_arrival_gain"]["selected_v4_target_recall"] == 0.5
    assert evidence["temporal_arrival_gain"]["selected_v4_false_positive_total"] == 0
    assert evidence["explicit_crop_semantic_regression"] == {
        "baseline_correct": 7,
        "baseline_accuracy": 7 / 11,
        "selected_v4_correct": 5,
        "selected_v4_accuracy": 5 / 11,
        "acknowledged": True,
        "selection_rationale": "Frozen objective is temporal arrival; crop regression is recorded and not hidden.",
    }


def test_selected_frozen_report_refuses_unlocked_or_partial_runs_and_keeps_scope_guard(tmp_path):
    event_hash = "event-reference-hash"
    baseline_path = tmp_path / "baseline.json"
    v4_path = tmp_path / "v4.json"
    baseline = _calibration(
        variant="baseline", event_hash=event_hash, crop_correct=7, arrival_recall=0.0, arrival_fp=1
    )
    v4 = _calibration(
        variant="optimized_v4", event_hash=event_hash, crop_correct=5, arrival_recall=0.5, arrival_fp=0
    )
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    v4_path.write_text(json.dumps(v4), encoding="utf-8")
    selection = selection_builder.build_selection(
        baseline=baseline,
        v4=v4,
        baseline_path=baseline_path,
        v4_path=v4_path,
        frozen_samples=_samples(),
        event_reference_sha256=event_hash,
    )
    selection_path = tmp_path / "selection.json"
    selection_path.write_text(json.dumps(selection), encoding="utf-8")
    ids = [sample.sample_id for sample in _samples()]
    result = {
        "suite": "frozen_arrival",
        "variant": "optimized_v4",
        "prompt_version": "mayo-recognition-v4",
        "dry_run": False,
        "source": {"event_reference_sha256": event_hash},
        "execution": {
            "status": "completed",
            "unexecuted_sample_ids": [],
            "inference_http_request_count": 5,
            "max_inference_requests_per_fresh_worker_batch": 1,
            "batches": [
                {
                    "status": "completed",
                    "inference_http_request_count": 1,
                    "sample_ids": [sample_id],
                    "lifecycle": {"status": "ready"},
                    "post_batch_readiness": {"manager_loaded": True, "direct_worker_ready": True},
                }
                for sample_id in ids
            ],
        },
        "frozen_selection_lock": {
            "sha256": _sha256(selection_path),
            "selection_id": selection["selection_id"],
        },
        "image_policy": {"preprocessor": selection["frozen_config"]["image_preprocess_policy"]},
        "normalizer_validation": {"all_request_image_integrity_checks_passed": True},
        "summary": {
            "arrival": {
                "attempted": 5,
                "model_outputs": 5,
                "transport_errors": 0,
                "valid_json": 5,
                "contract_valid": 5,
                "target_recall": 0.6,
                "exact_match": 0.6,
                "accepted_target_recall": 0.6,
                "accepted_exact_match": 0.6,
                "false_positive_total": 0,
            }
        },
        "records": [
            {
                "input": {"sample_id": sample_id, "mode": "arrival"},
                "evaluation_reference": "bovie",
                "score": {
                    "transport_error": False,
                    "not_inferred": False,
                    "contract_valid": True,
                    "target_recalled": True,
                    "exact": True,
                    "false_positives": [],
                    "predicted": ["bovie"],
                },
            }
            for sample_id in ids
        ],
    }
    coverage = {"summary": {"accuracy_eligible_cases": ["0704_5"]}}
    report = report_builder.build_report(
        coverage=coverage,
        result=result,
        selection=selection,
        result_path=tmp_path / "result.json",
        selection_path=selection_path,
        source_review="source.jpg",
        normalized_review="normalized.jpg",
    )
    assert "no cross-case or clinical-generalization claim" in report
    assert "`7/11` to `5/11` correct" in report
    result["execution"]["status"] = "halted"
    try:
        report_builder.validate_selected_run(result=result, selection=selection, selection_path=selection_path)
    except report_builder.ReportError as exc:
        assert "incomplete" in str(exc)
    else:  # pragma: no cover - protective assertion
        raise AssertionError("partial frozen run was incorrectly accepted")
