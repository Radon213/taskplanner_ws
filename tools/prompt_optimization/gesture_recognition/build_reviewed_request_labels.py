#!/usr/bin/env python3
"""Derive an evaluation-only empty-palm request target from human review.

This tool does not edit the historical event labels or the append-only visual
review ledger.  It validates the completed visual-review queue, then writes a
separate target layer for the user-defined operational policy:

* positive only for an explicitly adjudicated open hand;
* a human ``ambiguous`` decision is closed-negative for triggering, because an
  unclear / occupied / tool-placement hand must not be treated as a request.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

from tools.prompt_optimization.gesture_recognition.visual_review_gui.server import (
    ReviewCatalog,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
LABEL_SCHEMA = "taskplanner.empty_open_request_human_label.v1"
REPORT_SCHEMA = "taskplanner.empty_open_request_human_label_report.v1"


class HumanReviewLabelError(ValueError):
    """A completed-review or immutable-output condition is not satisfied."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_ratio(numerator: int, denominator: int) -> float:
    return 0.0 if denominator == 0 else numerator / denominator


def _metrics(rows: Sequence[Mapping[str, Any]], *, prediction_key: str) -> dict[str, Any]:
    tp = fp = tn = fn = 0
    for row in rows:
        label = row["label"]
        prediction = row[prediction_key]
        actual_positive = label == "open_receive"
        predicted_positive = prediction == "open_receive"
        if actual_positive and predicted_positive:
            tp += 1
        elif actual_positive:
            fn += 1
        elif predicted_positive:
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


def _metric_rows(
    labels: Sequence[Mapping[str, Any]], *, source_key: str
) -> list[dict[str, str]]:
    """Make the exact label/prediction pair explicit before scoring it."""

    rows: list[dict[str, str]] = []
    for label in labels:
        source = label.get("source")
        if not isinstance(source, Mapping):
            raise HumanReviewLabelError("derived label source가 올바르지 않습니다.")
        target = label.get("label")
        prediction = source.get(source_key)
        if target not in {"open_receive", "not_open_receive"} or prediction not in {
            "open_receive",
            "not_open_receive",
        }:
            raise HumanReviewLabelError("derived target/prediction 값이 올바르지 않습니다.")
        rows.append({"label": str(target), "prediction": str(prediction)})
    return rows


def _relative(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root).as_posix()


def _line_count(path: Path) -> int:
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def build_request_labels(
    *,
    repository_root: Path,
    review_index_path: Path,
    decisions_path: Path,
    seed_decision_paths: Sequence[Path],
    labels_output_path: Path,
    report_output_path: Path,
) -> dict[str, Any]:
    """Validate a complete visual review and create immutable derived labels."""

    root = repository_root.resolve()
    review_index = review_index_path.resolve()
    decisions = decisions_path.resolve()
    seed_decisions = tuple(path.resolve() for path in seed_decision_paths)
    labels_output = labels_output_path.resolve()
    report_output = report_output_path.resolve()
    for path, label in ((review_index, "review index"), (decisions, "current decision ledger")):
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise HumanReviewLabelError(f"{label}가 repository 범위를 벗어납니다.") from exc
        if not path.is_file():
            raise HumanReviewLabelError(f"{label}를 찾을 수 없습니다: {path}")
    for seed_path in seed_decisions:
        try:
            seed_path.relative_to(root)
        except ValueError as exc:
            raise HumanReviewLabelError("seed decision ledger가 repository 범위를 벗어납니다.") from exc
        if not seed_path.is_file():
            raise HumanReviewLabelError(f"seed decision ledger를 찾을 수 없습니다: {seed_path}")
    for path in (labels_output, report_output):
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise HumanReviewLabelError(f"output path가 repository 범위를 벗어납니다.") from exc
        if path.exists():
            raise FileExistsError(f"refusing to overwrite existing output: {path}")

    catalog = ReviewCatalog(
        repository_root=root,
        review_index_path=review_index,
        decisions_path=decisions,
        seed_decision_paths=seed_decisions,
    )
    session = catalog.session()
    samples = session["samples"]
    decision_by_id = session["decisions"]
    if not isinstance(samples, list) or not isinstance(decision_by_id, Mapping):
        raise HumanReviewLabelError("validated review session shape가 올바르지 않습니다.")
    if len(decision_by_id) != len(samples):
        raise HumanReviewLabelError(
            f"visual review가 완료되지 않았습니다: {len(decision_by_id)}/{len(samples)}"
        )

    labels: list[dict[str, Any]] = []
    for sample in samples:
        if not isinstance(sample, Mapping):
            raise HumanReviewLabelError("review session sample shape가 올바르지 않습니다.")
        sample_id = sample.get("sample_id")
        if not isinstance(sample_id, str):
            raise HumanReviewLabelError("review session sample_id가 올바르지 않습니다.")
        decision = decision_by_id.get(sample_id)
        if not isinstance(decision, Mapping):
            raise HumanReviewLabelError(f"{sample_id} human decision이 없습니다.")
        human_decision = decision.get("decision")
        if human_decision not in {"open_hand", "not_open_hand", "ambiguous"}:
            raise HumanReviewLabelError(f"{sample_id} human decision이 올바르지 않습니다.")
        # Closed-negative is intentional operational policy, not a claim that
        # every ambiguous frame has an objectively absent hand pose.
        target_label = "open_receive" if human_decision == "open_hand" else "not_open_receive"
        labels.append(
            {
                "schema": LABEL_SCHEMA,
                "sample_id": sample_id,
                "case_id": sample["case_id"],
                "event_id": sample["event_id"],
                "frame_idx": sample["frame_idx"],
                "time_sec": sample["time_sec"],
                "partition": sample["partition"],
                "sample_kind": sample["sample_kind"],
                "label": target_label,
                "human_visual_decision": human_decision,
                "human_decision_id": decision["decision_id"],
                "human_recorded_at": decision["recorded_at"],
                "human_decision_origin": decision["origin"],
                "human_note": decision["note"],
                "ambiguity_resolution": (
                    "not_applicable"
                    if human_decision != "ambiguous"
                    else "closed_negative_no_request_trigger"
                ),
                "target_definition": (
                    "Positive only when the upper-right surgeon visibly presents an empty "
                    "open palm; an occupied, tool-placement, unclear, or ambiguous hand must "
                    "not trigger an instrument request."
                ),
                "source": {
                    "review_index_sha256": session["review_index_sha256"],
                    "existing_event_proxy_label": sample["existing_event_proxy_label"],
                    "vlm_v8_prediction": sample["vlm_predicted_gesture"],
                },
            }
        )

    by_partition: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in labels:
        by_partition[str(row["partition"])].append(row)
    decision_counts = Counter(str(row["human_visual_decision"]) for row in labels)
    origin_counts = Counter(str(row["human_decision_origin"]) for row in labels)
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "ground_truth_usage": "evaluation_only",
        "may_publish_runtime": False,
        "target_definition": (
            "Instrument-request visual trigger: the upper-right surgeon must visibly hold out "
            "an empty, open palm. A hand holding, receiving, returning, or placing an object is "
            "negative even if its palm appears open/upward. Ambiguous visual-review decisions are "
            "closed-negative for this no-trigger policy."
        ),
        "source_integrity": {
            "review_index": _relative(root, review_index),
            "review_index_sha256": session["review_index_sha256"],
            "current_decision_ledger": _relative(root, decisions),
            "current_decision_ledger_sha256": _sha256(decisions),
            "current_decision_record_count": _line_count(decisions),
            "seed_decision_ledgers": [
                {
                    "path": _relative(root, path),
                    "sha256": _sha256(path),
                    "record_count": _line_count(path),
                }
                for path in seed_decisions
            ],
            "reviewed_sample_count": len(labels),
            "unreviewed_sample_count": len(samples) - len(labels),
            "validated_by": "ReviewCatalog hash-checked append-only ledger validation",
        },
        "human_decision_counts": dict(sorted(decision_counts.items())),
        "human_decision_origin_counts": dict(sorted(origin_counts.items())),
        "ambiguous_closed_negative_count": decision_counts["ambiguous"],
        "target_label_counts": dict(sorted(Counter(row["label"] for row in labels).items())),
        "metrics": {
            "v8_against_human_empty_open_request_target": _metrics(
                _metric_rows(labels, source_key="vlm_v8_prediction"),
                prediction_key="prediction",
            ),
            "existing_event_proxy_against_human_target": _metrics(
                _metric_rows(labels, source_key="existing_event_proxy_label"),
                prediction_key="prediction",
            ),
        },
        "metrics_by_partition": {
            partition: _metrics(
                _metric_rows(rows, source_key="vlm_v8_prediction"),
                prediction_key="prediction",
            )
            for partition, rows in sorted(by_partition.items())
        },
    }
    report["label_file_sha256_after_write"] = None

    labels_output.parent.mkdir(parents=True, exist_ok=True)
    report_output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with labels_output.open("x", encoding="utf-8") as stream:
            for row in labels:
                stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        report["label_file"] = _relative(root, labels_output)
        report["label_file_sha256_after_write"] = _sha256(labels_output)
        with report_output.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    except FileExistsError:
        raise FileExistsError("refusing to overwrite an existing human-review target output") from None
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-index", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--seed-decisions", type=Path, action="append", default=[])
    parser.add_argument("--labels-output", type=Path, required=True)
    parser.add_argument("--report-output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        report = build_request_labels(
            repository_root=REPOSITORY_ROOT,
            review_index_path=args.review_index,
            decisions_path=args.decisions,
            seed_decision_paths=tuple(args.seed_decisions),
            labels_output_path=args.labels_output,
            report_output_path=args.report_output,
        )
    except (FileExistsError, HumanReviewLabelError) as exc:
        raise SystemExit(str(exc)) from exc
    print(
        json.dumps(
            {
                "reviewed_sample_count": report["source_integrity"]["reviewed_sample_count"],
                "ambiguous_closed_negative_count": report["ambiguous_closed_negative_count"],
                "v8_accuracy": report["metrics"]["v8_against_human_empty_open_request_target"]["accuracy"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
