from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tools.prompt_optimization.gesture_recognition.build_full_visual_review_index import (
    ReviewIndexError,
    build_full_review_index,
)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_fixture(root: Path, *, crop_hash: str | None = None) -> tuple[Path, Path]:
    manifest = root / "output" / "evaluation" / "manifest.jsonl"
    manifest.parent.mkdir(parents=True)
    sample = {
        "schema": "taskplanner.gesture_prompt_eval_sample.v1",
        "sample_id": "0704_6-R0001-positive_event_midpoint-f0001",
        "case_id": "0704_6",
        "event_id": "0704_6-R0001",
        "frame_idx": 1,
        "time_sec": 0.1,
        "label": "open_receive",
        "sample_kind": "positive_event_midpoint",
        "split": "calibration",
    }
    manifest.write_text(json.dumps(sample) + "\n", encoding="utf-8")

    run_root = root / "output" / "run"
    image_dir = run_root / "images" / "0704_6"
    image_dir.mkdir(parents=True)
    (image_dir / "cam4_f0001.jpg").write_bytes(b"original-image")
    crop = image_dir / "cam4_right_detail_f0001.jpg"
    crop.write_bytes(b"crop-image")
    prediction_path = run_root / "predictions" / "one.jsonl"
    prediction_path.parent.mkdir()
    prediction_path.write_text(
        json.dumps(
            {
                "schema": "taskplanner.gesture_prompt_eval_prediction.v1",
                "prompt_version": "gesture-top-right-open-hand-v8",
                "transport_error": "",
                "sample": sample,
                "prediction": {
                    "gesture": "open_receive",
                    "parse_error": "",
                },
                "raw_model_text": '{"open_hand":true}',
                "image_sha256": {
                    "CAM4 fixed right-side hand detail": crop_hash or _sha256(b"crop-image")
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    execution = run_root / "execution" / "gesture-top-right-open-hand-v8" / "calibration.json"
    execution.parent.mkdir(parents=True)
    execution.write_text(
        json.dumps(
            {
                "schema": "taskplanner.gesture_prompt_eval_execution.v1",
                "manifest": "output/evaluation/manifest.jsonl",
                "prompt_version": "gesture-top-right-open-hand-v8",
                "status": "completed",
                "scoreable": True,
                "transport_failure_count": 0,
                "total_sample_count": 1,
                "batches": [
                    {
                        "status": "completed",
                        "transport_failure_count": 0,
                        "prediction_path": "output/run/predictions/one.jsonl",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return manifest, execution


def test_build_full_review_index_covers_manifest_and_validates_crop_hash(tmp_path: Path) -> None:
    manifest, execution = _write_fixture(tmp_path)
    output = tmp_path / "output" / "review" / "FULL_REVIEW_INDEX.json"

    payload = build_full_review_index(
        repository_root=tmp_path,
        manifest_path=manifest,
        executions={"calibration": execution},
        output_path=output,
    )

    assert output.is_file()
    assert payload["sample_count"] == 1
    assert payload["by_outcome"] == {"TP": 1, "TN": 0, "FP": 0, "FN": 0}
    entry = payload["entries"][0]
    assert entry["comparison_group"] == "agreement"
    assert entry["original_cam4_image"] == "output/run/images/0704_6/cam4_f0001.jpg"
    assert entry["vlm_input_image"] == "output/run/images/0704_6/cam4_right_detail_f0001.jpg"
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        build_full_review_index(
            repository_root=tmp_path,
            manifest_path=manifest,
            executions={"calibration": execution},
            output_path=output,
        )


def test_build_full_review_index_rejects_mismatched_vlm_crop_hash(tmp_path: Path) -> None:
    manifest, execution = _write_fixture(tmp_path, crop_hash="0" * 64)
    with pytest.raises(ReviewIndexError, match="crop hash"):
        build_full_review_index(
            repository_root=tmp_path,
            manifest_path=manifest,
            executions={"calibration": execution},
            output_path=tmp_path / "output" / "review" / "FULL_REVIEW_INDEX.json",
        )
