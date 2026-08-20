from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.prompt_optimization.gesture_recognition.score_human_review_predictions import (
    HumanReviewScoreError,
    score_completed_execution,
)


def _write_fixture(
    root: Path,
    *,
    execution_status: str = "completed",
    predictions_by_id: tuple[tuple[str, str], ...] = (
        ("sample-1", "open_receive"),
        ("sample-2", "not_open_receive"),
    ),
) -> tuple[Path, Path]:
    labels = root / "output" / "labels.jsonl"
    labels.parent.mkdir(parents=True)
    labels.write_text(
        "\n".join(
            json.dumps(
                {
                    "schema": "taskplanner.empty_open_request_human_label.v1",
                    "sample_id": sample_id,
                    "case_id": "0704_6",
                    "frame_idx": index,
                    "partition": "calibration",
                    "label": label,
                }
            )
            for index, (sample_id, label) in enumerate(
                (("sample-1", "open_receive"), ("sample-2", "not_open_receive")), 1
            )
        )
        + "\n",
        encoding="utf-8",
    )
    predictions = root / "output" / "run" / "predictions.jsonl"
    predictions.parent.mkdir(parents=True)
    predictions.write_text(
        "\n".join(
            json.dumps(
                {
                    "schema": "taskplanner.gesture_prompt_eval_prediction.v1",
                    "prompt_version": "gesture-top-right-empty-open-hand-v9",
                    "transport_error": "",
                    "sample": {"sample_id": sample_id, "case_id": "0704_6", "frame_idx": index},
                    "prediction": {"gesture": prediction, "parse_error": ""},
                }
            )
            for index, (sample_id, prediction) in enumerate(predictions_by_id, 1)
        )
        + "\n",
        encoding="utf-8",
    )
    execution = root / "output" / "run" / "execution.json"
    execution.write_text(
        json.dumps(
            {
                "schema": "taskplanner.gesture_prompt_eval_execution.v1",
                "status": execution_status,
                "scoreable": execution_status == "completed",
                "transport_failure_count": 0,
                "prompt_version": "gesture-top-right-empty-open-hand-v9",
                "model_id": "qwen3.6-35b-a3b",
                "total_sample_count": 2,
                "batches": [
                    {
                        "status": "completed",
                        "transport_failure_count": 0,
                        "sample_count": 2,
                        "prediction_path": "output/run/predictions.jsonl",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return labels, execution


def test_scores_completed_v9_against_human_review_labels(tmp_path: Path) -> None:
    labels, execution = _write_fixture(tmp_path)
    output = tmp_path / "output" / "score.json"
    report = score_completed_execution(
        repository_root=tmp_path,
        labels_path=labels,
        execution_path=execution,
        output_path=output,
    )
    assert report["metrics"]["accuracy"] == 1.0
    assert report["scored_sample_count"] == 2
    assert output.is_file()


def test_refuses_non_scoreable_execution(tmp_path: Path) -> None:
    labels, execution = _write_fixture(tmp_path, execution_status="halted")
    with pytest.raises(HumanReviewScoreError, match="completed and scoreable"):
        score_completed_execution(
            repository_root=tmp_path,
            labels_path=labels,
            execution_path=execution,
            output_path=tmp_path / "output" / "score.json",
        )


def test_reports_uncertain_coverage_without_counting_it_as_a_correct_negative(
    tmp_path: Path,
) -> None:
    labels, execution = _write_fixture(
        tmp_path,
        predictions_by_id=(
            ("sample-1", "uncertain"),
            ("sample-2", "not_open_receive"),
        ),
    )
    report = score_completed_execution(
        repository_root=tmp_path,
        labels_path=labels,
        execution_path=execution,
        output_path=tmp_path / "output" / "score.json",
    )
    metrics = report["metrics"]
    assert metrics["accuracy"] == 0.5
    assert metrics["uncertain_prediction_count"] == 1
    assert metrics["uncertain_label_counts"] == {"open_receive": 1}
    assert metrics["decision_coverage"] == 0.5
    assert metrics["decided_metrics"]["accuracy"] == 1.0
