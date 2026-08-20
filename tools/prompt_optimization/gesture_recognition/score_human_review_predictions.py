#!/usr/bin/env python3
"""Score completed gesture predictions against the derived human-review target.

The derived target is strictly local evaluation data.  This script reads it
only after the model run has completed; no target row is ever passed to NInfer.
It refuses partial executions, transport failures, duplicate IDs, or missing
human-review labels instead of manufacturing a score.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
LABEL_SCHEMA = "taskplanner.empty_open_request_human_label.v1"
EXECUTION_SCHEMA = "taskplanner.gesture_prompt_eval_execution.v1"
PREDICTION_SCHEMA = "taskplanner.gesture_prompt_eval_prediction.v1"
REPORT_SCHEMA = "taskplanner.empty_open_request_prediction_score.v1"
VALID_LABELS = {"open_receive", "not_open_receive"}
VALID_PREDICTIONS = VALID_LABELS | {"uncertain"}


class HumanReviewScoreError(ValueError):
    """Evidence is incomplete or inconsistent and must not receive a score."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HumanReviewScoreError(f"{label} JSON을 읽을 수 없습니다: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise HumanReviewScoreError(f"{label}은 JSON object여야 합니다: {path}")
    return value


def _read_jsonl(path: Path, *, label: str) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise HumanReviewScoreError(f"{label} JSONL을 읽을 수 없습니다: {path}: {exc}") from exc
    values: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise HumanReviewScoreError(f"{path}:{line_number} JSON 오류: {exc}") from exc
        if not isinstance(value, dict):
            raise HumanReviewScoreError(f"{path}:{line_number}은 JSON object여야 합니다.")
        values.append(value)
    return values


def _root_file(root: Path, value: Path, *, label: str) -> Path:
    path = value.resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise HumanReviewScoreError(f"{label}가 repository 범위를 벗어납니다: {path}") from exc
    if not path.is_file():
        raise HumanReviewScoreError(f"{label}를 찾을 수 없습니다: {path}")
    return path


def _repo_path(root: Path, value: object, *, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise HumanReviewScoreError(f"{label} 경로가 없습니다.")
    return _root_file(root, root / value, label=label)


def _relative(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root).as_posix()


def _safe_ratio(numerator: int, denominator: int) -> float:
    return 0.0 if denominator == 0 else numerator / denominator


def _score_decided_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Score committed yes/no outputs only; never use this as coverage accuracy."""

    if not rows:
        return None
    tp = fp = tn = fn = 0
    for row in rows:
        actual = row["label"] == "open_receive"
        predicted = row["prediction"] == "open_receive"
        if actual and predicted:
            tp += 1
        elif actual:
            fn += 1
        elif predicted:
            fp += 1
        else:
            tn += 1
    count = tp + fp + tn + fn
    precision = _safe_ratio(tp, tp + fp)
    recall = _safe_ratio(tp, tp + fn)
    specificity = _safe_ratio(tn, tn + fp)
    f1 = _safe_ratio(2 * precision * recall, precision + recall)
    return {
        "sample_count": count,
        "confusion_matrix": {"tp": tp, "fp": fp, "tn": tn, "fn": fn},
        "accuracy": round(_safe_ratio(tp + tn, count), 6),
        "precision": round(precision, 6),
        "recall": round(recall, 6),
        "specificity": round(specificity, 6),
        "balanced_accuracy": round((recall + specificity) / 2.0, 6),
        "f1": round(f1, 6),
    }


def _score(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Report operational binary metrics plus abstention coverage separately.

    A VLM `uncertain` response cannot cause an instrument request, so it is
    deliberately routed to `not_open_receive` for the top-level operational
    metrics.  The committed-only result is included separately, preventing an
    abstention from being silently counted as a correct answer.
    """

    operational = _score_decided_rows(rows)
    if operational is None:
        raise ValueError("cannot score an empty prediction set")
    uncertain_rows = [row for row in rows if row["prediction"] == "uncertain"]
    decided_rows = [row for row in rows if row["prediction"] != "uncertain"]
    uncertain_by_label = Counter(str(row["label"]) for row in uncertain_rows)
    operational.update(
        {
            "operational_uncertain_policy": "uncertain_routes_to_not_open_receive_no_trigger",
            "uncertain_prediction_count": len(uncertain_rows),
            "uncertain_prediction_rate": round(
                _safe_ratio(len(uncertain_rows), len(rows)), 6
            ),
            "uncertain_label_counts": dict(sorted(uncertain_by_label.items())),
            "decided_sample_count": len(decided_rows),
            "decision_coverage": round(_safe_ratio(len(decided_rows), len(rows)), 6),
            "decided_metrics": _score_decided_rows(decided_rows),
        }
    )
    return operational


def score_completed_execution(
    *,
    repository_root: Path,
    labels_path: Path,
    execution_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    """Write an immutable score only when every execution record is valid."""

    root = repository_root.resolve()
    labels_file = _root_file(root, labels_path, label="human-review labels")
    execution_file = _root_file(root, execution_path, label="execution")
    output = output_path.resolve()
    try:
        output.relative_to(root)
    except ValueError as exc:
        raise HumanReviewScoreError("score output가 repository 범위를 벗어납니다.") from exc
    if output.exists():
        raise FileExistsError(f"refusing to overwrite existing score: {output}")

    labels_by_id: dict[str, dict[str, Any]] = {}
    for line_number, row in enumerate(_read_jsonl(labels_file, label="human-review labels"), 1):
        if row.get("schema") != LABEL_SCHEMA:
            raise HumanReviewScoreError(f"{labels_file}:{line_number} label schema가 올바르지 않습니다.")
        sample_id = row.get("sample_id")
        label = row.get("label")
        if not isinstance(sample_id, str) or not sample_id or label not in VALID_LABELS:
            raise HumanReviewScoreError(f"{labels_file}:{line_number} sample_id/label이 올바르지 않습니다.")
        if sample_id in labels_by_id:
            raise HumanReviewScoreError(f"human-review labels에 중복 sample_id가 있습니다: {sample_id}")
        labels_by_id[sample_id] = row
    if not labels_by_id:
        raise HumanReviewScoreError("human-review labels가 비어 있습니다.")

    execution = _read_json(execution_file, label="execution")
    if execution.get("schema") != EXECUTION_SCHEMA:
        raise HumanReviewScoreError("execution schema가 올바르지 않습니다.")
    if execution.get("status") != "completed" or execution.get("scoreable") is not True:
        raise HumanReviewScoreError("completed and scoreable execution만 score할 수 있습니다.")
    if execution.get("transport_failure_count") != 0:
        raise HumanReviewScoreError("transport failure이 있는 execution은 score할 수 없습니다.")
    prompt_version = execution.get("prompt_version")
    if not isinstance(prompt_version, str) or not prompt_version:
        raise HumanReviewScoreError("execution prompt version이 없습니다.")
    batches = execution.get("batches")
    if not isinstance(batches, list) or not batches:
        raise HumanReviewScoreError("execution batches가 없습니다.")

    scored_rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    prediction_hashes: dict[str, str] = {}
    observed_count = 0
    for batch_index, batch in enumerate(batches, 1):
        if not isinstance(batch, Mapping) or batch.get("status") not in {"completed", "reused"}:
            raise HumanReviewScoreError(f"execution batch {batch_index}가 완료되지 않았습니다.")
        if batch.get("transport_failure_count") != 0:
            raise HumanReviewScoreError(f"execution batch {batch_index}에 transport failure이 있습니다.")
        prediction_file = _repo_path(
            root,
            batch.get("prediction_path"),
            label=f"execution batch {batch_index} prediction",
        )
        prediction_hashes[_relative(root, prediction_file)] = _sha256(prediction_file)
        records = _read_jsonl(prediction_file, label="prediction")
        if batch.get("sample_count") != len(records):
            raise HumanReviewScoreError(f"execution batch {batch_index} sample_count가 prediction rows와 다릅니다.")
        for record_index, record in enumerate(records, 1):
            observed_count += 1
            if record.get("schema") != PREDICTION_SCHEMA:
                raise HumanReviewScoreError(f"{prediction_file}:{record_index} schema가 올바르지 않습니다.")
            if record.get("prompt_version") != prompt_version or record.get("transport_error"):
                raise HumanReviewScoreError(f"{prediction_file}:{record_index}는 valid completed prediction이 아닙니다.")
            sample = record.get("sample")
            prediction = record.get("prediction")
            if not isinstance(sample, Mapping) or not isinstance(prediction, Mapping):
                raise HumanReviewScoreError(f"{prediction_file}:{record_index} sample/prediction이 없습니다.")
            sample_id = sample.get("sample_id")
            gesture = prediction.get("gesture")
            if not isinstance(sample_id, str) or sample_id not in labels_by_id:
                raise HumanReviewScoreError(f"{prediction_file}:{record_index}에는 human-review label이 없습니다.")
            if sample_id in seen_ids:
                raise HumanReviewScoreError(f"execution predictions에 중복 sample_id가 있습니다: {sample_id}")
            if gesture not in VALID_PREDICTIONS or prediction.get("parse_error"):
                raise HumanReviewScoreError(f"{prediction_file}:{record_index} VLM output contract가 올바르지 않습니다.")
            human = labels_by_id[sample_id]
            if sample.get("case_id") != human.get("case_id") or sample.get("frame_idx") != human.get("frame_idx"):
                raise HumanReviewScoreError(f"{prediction_file}:{record_index} human label binding이 다릅니다.")
            seen_ids.add(sample_id)
            scored_rows.append(
                {
                    "sample_id": sample_id,
                    "partition": human["partition"],
                    "label": human["label"],
                    "prediction": gesture,
                }
            )
    if execution.get("total_sample_count") != observed_count:
        raise HumanReviewScoreError("execution total_sample_count가 prediction rows와 다릅니다.")

    by_partition: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in scored_rows:
        by_partition[str(row["partition"])].append(row)
    report = {
        "schema": REPORT_SCHEMA,
        "ground_truth_usage": "evaluation_only",
        "may_publish_runtime": False,
        "target_definition": (
            "Positive only for a visibly empty open palm held out by the upper-right surgeon; "
            "ambiguous / occupied / placement states are closed-negative for triggering."
        ),
        "labels": {
            "path": _relative(root, labels_file),
            "sha256": _sha256(labels_file),
            "available_sample_count": len(labels_by_id),
        },
        "execution": {
            "path": _relative(root, execution_file),
            "sha256": _sha256(execution_file),
            "prompt_version": prompt_version,
            "model_id": execution.get("model_id"),
            "input_variant": None,
            "prediction_file_sha256": dict(sorted(prediction_hashes.items())),
        },
        "scored_sample_count": len(scored_rows),
        "label_counts": dict(sorted(Counter(row["label"] for row in scored_rows).items())),
        "metrics": _score(scored_rows),
        "metrics_by_partition": {
            partition: _score(rows) for partition, rows in sorted(by_partition.items())
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    except FileExistsError:
        raise FileExistsError(f"refusing to overwrite existing score: {output}") from None
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--execution", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        report = score_completed_execution(
            repository_root=REPOSITORY_ROOT,
            labels_path=args.labels,
            execution_path=args.execution,
            output_path=args.output,
        )
    except (FileExistsError, HumanReviewScoreError) as exc:
        raise SystemExit(str(exc)) from exc
    print(
        json.dumps(
            {
                "prompt_version": report["execution"]["prompt_version"],
                "scored_sample_count": report["scored_sample_count"],
                "accuracy": report["metrics"]["accuracy"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
