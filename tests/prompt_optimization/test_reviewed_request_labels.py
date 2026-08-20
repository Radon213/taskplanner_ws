from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.prompt_optimization.gesture_recognition.build_reviewed_request_labels import (
    HumanReviewLabelError,
    build_request_labels,
)
from tools.prompt_optimization.gesture_recognition.visual_review_gui.server import ReviewCatalog


def _catalog(tmp_path: Path) -> ReviewCatalog:
    asset_dir = tmp_path / "output"
    asset_dir.mkdir()
    (asset_dir / "original-1.jpg").write_bytes(b"original-1")
    (asset_dir / "crop-1.jpg").write_bytes(b"crop-1")
    (asset_dir / "original-2.jpg").write_bytes(b"original-2")
    (asset_dir / "crop-2.jpg").write_bytes(b"crop-2")
    index = tmp_path / "review_index.json"
    index.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "index": 1,
                        "sample_id": "sample-1",
                        "case_id": "0704_6",
                        "event_id": "R1",
                        "frame_idx": 1,
                        "time_sec": 0.1,
                        "partition": "calibration",
                        "failure_type": "TP",
                        "sample_kind": "positive_event_midpoint",
                        "actual_label": "open_receive",
                        "predicted_gesture": "open_receive",
                        "raw_model_text": '{"open_hand":true}',
                        "original_cam4_image": "output/original-1.jpg",
                        "vlm_input_image": "output/crop-1.jpg",
                    },
                    {
                        "index": 2,
                        "sample_id": "sample-2",
                        "case_id": "0704_6",
                        "event_id": "G1",
                        "frame_idx": 2,
                        "time_sec": 0.2,
                        "partition": "frozen_temporal_challenge",
                        "failure_type": "FP",
                        "sample_kind": "negative_inter_event_gap_midpoint",
                        "actual_label": "not_open_receive",
                        "predicted_gesture": "open_receive",
                        "raw_model_text": '{"open_hand":true}',
                        "original_cam4_image": "output/original-2.jpg",
                        "vlm_input_image": "output/crop-2.jpg",
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return ReviewCatalog(
        repository_root=tmp_path,
        review_index_path=index,
        decisions_path=tmp_path / "decisions" / "decisions.jsonl",
    )


def test_build_request_labels_maps_ambiguous_to_closed_negative(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    catalog.append_decision({"sample_id": "sample-1", "decision": "open_hand", "note": ""})
    catalog.append_decision({"sample_id": "sample-2", "decision": "ambiguous", "note": "placement"})
    labels = tmp_path / "derived" / "labels.jsonl"
    report = tmp_path / "derived" / "report.json"

    result = build_request_labels(
        repository_root=tmp_path,
        review_index_path=catalog.review_index_path,
        decisions_path=catalog.decisions_path,
        seed_decision_paths=(),
        labels_output_path=labels,
        report_output_path=report,
    )

    rows = [json.loads(line) for line in labels.read_text(encoding="utf-8").splitlines()]
    assert [row["label"] for row in rows] == ["open_receive", "not_open_receive"]
    assert rows[1]["ambiguity_resolution"] == "closed_negative_no_request_trigger"
    assert result["ambiguous_closed_negative_count"] == 1
    assert result["metrics"]["v8_against_human_empty_open_request_target"]["accuracy"] == 0.5
    assert json.loads(report.read_text(encoding="utf-8"))["source_integrity"]["reviewed_sample_count"] == 2


def test_build_request_labels_requires_all_samples_to_be_reviewed(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    catalog.append_decision({"sample_id": "sample-1", "decision": "open_hand", "note": ""})
    with pytest.raises(HumanReviewLabelError, match="완료되지 않았습니다"):
        build_request_labels(
            repository_root=tmp_path,
            review_index_path=catalog.review_index_path,
            decisions_path=catalog.decisions_path,
            seed_decision_paths=(),
            labels_output_path=tmp_path / "derived" / "labels.jsonl",
            report_output_path=tmp_path / "derived" / "report.json",
        )
