#!/usr/bin/env python3
"""Build an immutable, full-sample visual-review queue for V8 gesture results.

The queue is evidence only: it combines the frozen evaluation manifest with
already-completed V8 prediction records.  It does not call a model, regenerate
labels, or alter the existing event-derived reference labels.  Every entry is
validated against its source crop hash before the UI can serve it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
INDEX_SCHEMA = "taskplanner.gesture_full_visual_review_index.v1"
MANIFEST_SCHEMA = "taskplanner.gesture_prompt_eval_sample.v1"
EXECUTION_SCHEMA = "taskplanner.gesture_prompt_eval_execution.v1"
PREDICTION_SCHEMA = "taskplanner.gesture_prompt_eval_prediction.v1"
V8_PROMPT_VERSION = "gesture-top-right-open-hand-v8"
VALID_GESTURES = {"open_receive", "not_open_receive"}


class ReviewIndexError(ValueError):
    """A frozen-input or source-integrity error that makes the queue unsafe."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReviewIndexError(f"{label} JSON을 읽을 수 없습니다: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReviewIndexError(f"{label}은 JSON object여야 합니다: {path}")
    return value


def _jsonl_objects(path: Path, *, label: str) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ReviewIndexError(f"{label} JSONL을 읽을 수 없습니다: {path}: {exc}") from exc
    values: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ReviewIndexError(f"{path}:{line_number} JSON 오류: {exc}") from exc
        if not isinstance(value, dict):
            raise ReviewIndexError(f"{path}:{line_number}는 JSON object여야 합니다.")
        values.append(value)
    return values


def _within_root(repository_root: Path, path: Path, *, label: str) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(repository_root)
    except ValueError as exc:
        raise ReviewIndexError(f"{label}가 저장소 범위를 벗어납니다: {resolved}") from exc
    if not resolved.is_file():
        raise ReviewIndexError(f"{label} 파일을 찾을 수 없습니다: {resolved}")
    return resolved


def _repository_path(repository_root: Path, value: object, *, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ReviewIndexError(f"{label} 경로가 없습니다.")
    return _within_root(repository_root, repository_root / value, label=label)


def _relative_to_root(repository_root: Path, path: Path) -> str:
    return path.resolve().relative_to(repository_root).as_posix()


def _outcome(actual_label: str, predicted_gesture: str) -> str:
    if actual_label == "open_receive" and predicted_gesture == "open_receive":
        return "TP"
    if actual_label == "not_open_receive" and predicted_gesture == "not_open_receive":
        return "TN"
    if actual_label == "open_receive":
        return "FN"
    return "FP"


def _load_manifest(manifest_path: Path) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    rows = _jsonl_objects(manifest_path, label="manifest")
    if not rows:
        raise ReviewIndexError("manifest가 비어 있습니다.")
    by_sample_id: dict[str, dict[str, Any]] = {}
    for position, sample in enumerate(rows, 1):
        if sample.get("schema") != MANIFEST_SCHEMA:
            raise ReviewIndexError(f"manifest {position}행 schema가 올바르지 않습니다.")
        sample_id = sample.get("sample_id")
        if not isinstance(sample_id, str) or not sample_id:
            raise ReviewIndexError(f"manifest {position}행 sample_id가 없습니다.")
        if sample_id in by_sample_id:
            raise ReviewIndexError(f"manifest sample_id가 중복됩니다: {sample_id}")
        if sample.get("label") not in VALID_GESTURES:
            raise ReviewIndexError(f"manifest {sample_id} label이 올바르지 않습니다.")
        by_sample_id[sample_id] = sample
    return rows, by_sample_id


def _execution_records(
    *,
    repository_root: Path,
    manifest_path: Path,
    partition: str,
    execution_path: Path,
    manifest_by_sample_id: Mapping[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], str]:
    execution_path = _within_root(repository_root, execution_path, label=f"{partition} execution")
    execution = _json_object(execution_path, label=f"{partition} execution")
    if execution.get("schema") != EXECUTION_SCHEMA:
        raise ReviewIndexError(f"{execution_path}: execution schema가 올바르지 않습니다.")
    if execution.get("status") != "completed" or execution.get("scoreable") is not True:
        raise ReviewIndexError(f"{execution_path}: completed/scoreable execution만 사용할 수 있습니다.")
    if execution.get("prompt_version") != V8_PROMPT_VERSION:
        raise ReviewIndexError(f"{execution_path}: V8 prompt 결과만 사용할 수 있습니다.")
    if execution.get("transport_failure_count") != 0:
        raise ReviewIndexError(f"{execution_path}: transport failure이 있는 execution은 사용할 수 없습니다.")
    expected_manifest = _relative_to_root(repository_root, manifest_path)
    if execution.get("manifest") != expected_manifest:
        raise ReviewIndexError(f"{execution_path}: manifest binding이 현재 manifest와 다릅니다.")
    batches = execution.get("batches")
    if not isinstance(batches, list) or not batches:
        raise ReviewIndexError(f"{execution_path}: batches가 없습니다.")

    run_root = execution_path.parents[2]
    records: dict[str, dict[str, Any]] = {}
    observed_count = 0
    for batch_number, batch in enumerate(batches, 1):
        if not isinstance(batch, Mapping) or batch.get("status") != "completed":
            raise ReviewIndexError(f"{execution_path}: {batch_number}번째 batch가 완료되지 않았습니다.")
        if batch.get("transport_failure_count") != 0:
            raise ReviewIndexError(f"{execution_path}: {batch_number}번째 batch에 transport failure이 있습니다.")
        prediction_path = _repository_path(
            repository_root,
            batch.get("prediction_path"),
            label=f"{partition} batch {batch_number} prediction",
        )
        for record_number, record in enumerate(
            _jsonl_objects(prediction_path, label="prediction"), 1
        ):
            observed_count += 1
            if record.get("schema") != PREDICTION_SCHEMA:
                raise ReviewIndexError(f"{prediction_path}:{record_number} prediction schema가 올바르지 않습니다.")
            if record.get("prompt_version") != V8_PROMPT_VERSION:
                raise ReviewIndexError(f"{prediction_path}:{record_number} prompt version이 V8과 다릅니다.")
            if record.get("transport_error"):
                raise ReviewIndexError(f"{prediction_path}:{record_number} transport error가 있습니다.")
            sample = record.get("sample")
            prediction = record.get("prediction")
            if not isinstance(sample, dict) or not isinstance(prediction, Mapping):
                raise ReviewIndexError(f"{prediction_path}:{record_number} sample/prediction이 올바르지 않습니다.")
            sample_id = sample.get("sample_id")
            if not isinstance(sample_id, str) or sample_id not in manifest_by_sample_id:
                raise ReviewIndexError(f"{prediction_path}:{record_number} sample_id가 manifest에 없습니다.")
            if sample != manifest_by_sample_id[sample_id]:
                raise ReviewIndexError(f"{prediction_path}:{record_number} sample이 frozen manifest와 다릅니다.")
            gesture = prediction.get("gesture")
            if gesture not in VALID_GESTURES or prediction.get("parse_error"):
                raise ReviewIndexError(f"{prediction_path}:{record_number}가 유효한 VLM 판정이 아닙니다.")
            if not isinstance(record.get("raw_model_text"), str):
                raise ReviewIndexError(f"{prediction_path}:{record_number} raw_model_text가 없습니다.")
            image_hashes = record.get("image_sha256")
            if not isinstance(image_hashes, Mapping):
                raise ReviewIndexError(f"{prediction_path}:{record_number} VLM input hash가 없습니다.")

            try:
                frame_idx = int(sample["frame_idx"])
                case_id = str(sample["case_id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ReviewIndexError(f"{prediction_path}:{record_number} frame/case 값이 올바르지 않습니다.") from exc
            source_dir = run_root / "images" / case_id
            full_path = _within_root(
                repository_root,
                source_dir / f"cam4_f{frame_idx:04d}.jpg",
                label=f"{sample_id} original CAM4",
            )
            crop_path = _within_root(
                repository_root,
                source_dir / f"cam4_right_detail_f{frame_idx:04d}.jpg",
                label=f"{sample_id} VLM input crop",
            )
            recorded_crop_sha = image_hashes.get("CAM4 fixed right-side hand detail")
            if not isinstance(recorded_crop_sha, str) or sha256_file(crop_path) != recorded_crop_sha:
                raise ReviewIndexError(f"{prediction_path}:{record_number} VLM input crop hash가 다릅니다.")
            if sample_id in records:
                raise ReviewIndexError(f"{partition} execution에 중복 sample_id가 있습니다: {sample_id}")
            records[sample_id] = {
                "prediction": record,
                "prediction_path": prediction_path,
                "full_path": full_path,
                "crop_path": crop_path,
            }

    if execution.get("total_sample_count") != observed_count:
        raise ReviewIndexError(f"{execution_path}: total_sample_count가 prediction records와 다릅니다.")
    return records, sha256_file(execution_path)


def build_full_review_index(
    *,
    repository_root: Path,
    manifest_path: Path,
    executions: Mapping[str, Path],
    output_path: Path,
) -> dict[str, Any]:
    """Validate the complete run, then atomically create one immutable review index."""

    root = repository_root.resolve()
    manifest = _within_root(root, manifest_path, label="manifest")
    output = output_path.resolve()
    try:
        output.relative_to(root)
    except ValueError as exc:
        raise ReviewIndexError(f"output path가 저장소 범위를 벗어납니다: {output}") from exc
    if output.exists():
        raise FileExistsError(f"refusing to overwrite existing review index: {output}")
    if not executions:
        raise ReviewIndexError("최소 하나의 completed execution이 필요합니다.")

    manifest_rows, manifest_by_sample_id = _load_manifest(manifest)
    by_sample_id: dict[str, tuple[str, dict[str, Any]]] = {}
    execution_hashes: dict[str, str] = {}
    for partition, execution_path in executions.items():
        if not isinstance(partition, str) or not partition:
            raise ReviewIndexError("execution partition 이름이 올바르지 않습니다.")
        records, execution_hash = _execution_records(
            repository_root=root,
            manifest_path=manifest,
            partition=partition,
            execution_path=execution_path,
            manifest_by_sample_id=manifest_by_sample_id,
        )
        execution_hashes[partition] = execution_hash
        for sample_id, details in records.items():
            if sample_id in by_sample_id:
                raise ReviewIndexError(f"여러 execution에 중복된 sample_id가 있습니다: {sample_id}")
            by_sample_id[sample_id] = (partition, details)
    missing = set(manifest_by_sample_id).difference(by_sample_id)
    unexpected = set(by_sample_id).difference(manifest_by_sample_id)
    if missing or unexpected:
        raise ReviewIndexError(
            "completed executions가 frozen manifest 전체를 정확히 덮지 않습니다: "
            f"missing={len(missing)}, unexpected={len(unexpected)}"
        )

    entries: list[dict[str, Any]] = []
    for index, sample in enumerate(manifest_rows, 1):
        sample_id = str(sample["sample_id"])
        partition, details = by_sample_id[sample_id]
        record = details["prediction"]
        prediction = record["prediction"]
        actual_label = str(sample["label"])
        predicted_gesture = str(prediction["gesture"])
        failure_type = _outcome(actual_label, predicted_gesture)
        entries.append(
            {
                "index": index,
                "sample_id": sample_id,
                "case_id": str(sample["case_id"]),
                "event_id": str(sample["event_id"]),
                "frame_idx": int(sample["frame_idx"]),
                "time_sec": float(sample["time_sec"]),
                "partition": partition,
                "failure_type": failure_type,
                "comparison_group": "agreement" if failure_type in {"TP", "TN"} else "disagreement",
                "sample_kind": str(sample["sample_kind"]),
                "actual_label": actual_label,
                "predicted_gesture": predicted_gesture,
                "raw_model_text": record["raw_model_text"],
                "original_cam4_image": _relative_to_root(root, details["full_path"]),
                "vlm_input_image": _relative_to_root(root, details["crop_path"]),
                "prediction_record_source": _relative_to_root(root, details["prediction_path"]),
            }
        )

    by_outcome = Counter(entry["failure_type"] for entry in entries)
    by_partition = Counter(entry["partition"] for entry in entries)
    payload: dict[str, Any] = {
        "schema": INDEX_SCHEMA,
        "ground_truth_usage": "evaluation_only",
        "may_publish_runtime": False,
        "reference_interpretation": (
            "Existing event-derived reference for comparison only; the separate visual review "
            "ledger is the human frame-level adjudication layer."
        ),
        "manifest": _relative_to_root(root, manifest),
        "manifest_sha256": sha256_file(manifest),
        "execution_sha256": dict(sorted(execution_hashes.items())),
        "sample_count": len(entries),
        "by_partition": dict(sorted(by_partition.items())),
        "by_outcome": {outcome: by_outcome.get(outcome, 0) for outcome in ("TP", "TN", "FP", "FN")},
        "metadata": {
            "title": "전체 open-hand 시각 검토",
            "subtitle": (
                "오른쪽 위 집도의의 보이는 손만 판정합니다. 기존 이벤트 참조와 VLM 출력은 "
                "비교용이며, 기존 이벤트 라벨은 수정되지 않습니다."
            ),
            "completion_title": "전체 평가 샘플의 시각 검토를 완료했습니다.",
        },
        "entries": entries,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    except FileExistsError:
        raise FileExistsError(f"refusing to overwrite existing review index: {output}") from None
    return payload


def _parse_execution_spec(value: str) -> tuple[str, Path]:
    partition, separator, raw_path = value.partition("=")
    if not separator or not partition.strip() or not raw_path.strip():
        raise argparse.ArgumentTypeError("--execution은 PARTITION=PATH 형식이어야 합니다.")
    return partition.strip(), Path(raw_path.strip())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True, help="immutable full evaluation manifest")
    parser.add_argument(
        "--execution",
        action="append",
        type=_parse_execution_spec,
        required=True,
        metavar="PARTITION=PATH",
        help="completed V8 execution JSON; pass once for every evaluation partition",
    )
    parser.add_argument("--output", type=Path, required=True, help="new immutable review index")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    executions: dict[str, Path] = {}
    for partition, path in args.execution:
        if partition in executions:
            raise SystemExit(f"duplicate --execution partition: {partition}")
        executions[partition] = path
    try:
        payload = build_full_review_index(
            repository_root=REPOSITORY_ROOT,
            manifest_path=args.manifest,
            executions=executions,
            output_path=args.output,
        )
    except (FileExistsError, ReviewIndexError) as exc:
        raise SystemExit(str(exc)) from exc
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "sample_count": payload["sample_count"],
                "by_outcome": payload["by_outcome"],
                "by_partition": payload["by_partition"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
