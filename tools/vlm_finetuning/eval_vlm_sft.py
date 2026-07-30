#!/usr/bin/env python3
"""Offline metrics for causal surgical VLM SFT prediction JSONL.

No model runtime is imported here.  A prediction row contains ``task_type``, a
JSON object (or JSON string) under ``target``/``reference``, and a JSON object
(or JSON string) under ``prediction``.  Invalid model JSON remains a scored
failure and is included in the JSON-validity metric.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA = "taskplanner.vlm_sft_evaluation.v1"

TASK_ALIASES = {
    "tool_presence_at_transfer": "tool",
    "tool_presence_pseudo": "tool",
    "tool_recognition": "tool",
    "tool_detection": "tool",
    "current_tools": "tool",
    "request_intent": "intent",
    "surgeon_intent": "intent",
    "intent": "intent",
    "current_phase": "phase",
    "phase": "phase",
    "phase_classification": "phase",
    "next_physical_tool": "next_tool",
    "next_tool": "next_tool",
    "next_requested_tool": "next_tool",
    "next_transferred_tool": "next_tool",
    "clinical_observation_interpretation": "clinical",
    "clinical_analysis": "clinical",
}

NONE_LABELS = {"none", "null", "no_tool", "no_event", ""}
TRANSITION_LABELS = {"transition", "transition_candidate", "boundary"}


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, raw in enumerate(handle, start=1):
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                value = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{path}:{line_no}: invalid row JSON: {exc.msg}"
                ) from exc
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_no}: row must be an object")
            rows.append(dict(value))
    return rows


def _task(row: Mapping[str, Any]) -> str | None:
    raw = row.get("task_type")
    if not isinstance(raw, str):
        return None
    return TASK_ALIASES.get(raw.strip().lower())


def _reference_raw(row: Mapping[str, Any]) -> Any:
    for key in ("target", "reference", "expected", "gold"):
        if key in row:
            return row[key]
    return None


def _prediction_raw(row: Mapping[str, Any]) -> Any:
    for key in ("prediction", "predicted", "response", "output"):
        if key in row:
            return row[key]
    return None


def _json_object(value: Any) -> tuple[dict[str, Any] | None, str | None]:
    if isinstance(value, Mapping):
        return dict(value), None
    if not isinstance(value, str):
        return None, "not an object or JSON string"
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON: {exc.msg}"
    if not isinstance(parsed, dict):
        return None, "decoded JSON is not an object"
    return parsed, None


def _ratio(numerator: int | float, denominator: int | float) -> float | None:
    if denominator == 0:
        return None
    return float(numerator) / float(denominator)


def _f1(precision: float | None, recall: float | None) -> float | None:
    if precision is None or recall is None:
        return None
    if precision + recall == 0:
        return 0.0
    return 2.0 * precision * recall / (precision + recall)


def _binary_metrics(gold: Sequence[bool], predicted: Sequence[bool]) -> dict[str, Any]:
    tp = sum(g and p for g, p in zip(gold, predicted))
    fp = sum((not g) and p for g, p in zip(gold, predicted))
    fn = sum(g and (not p) for g, p in zip(gold, predicted))
    tn = sum((not g) and (not p) for g, p in zip(gold, predicted))
    precision = _ratio(tp, tp + fp)
    recall = _ratio(tp, tp + fn)
    if tp + fp + fn == 0:
        f1_value = None
    else:
        f1_value = _f1(precision or 0.0, recall or 0.0)
    return {
        "support": len(gold),
        "positive_support": sum(gold),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "accuracy": _ratio(tp + tn, len(gold)),
        "precision": precision,
        "recall": recall,
        "f1": f1_value,
    }


def _label(value: Any) -> str | None:
    if value is None:
        return None
    normalized = re.sub(r"[\s-]+", "_", str(value).strip().lower())
    return normalized or None


def _tool_set(value: Mapping[str, Any]) -> set[str]:
    result: set[str] = set()
    for key in (
        "tool",
        "tools",
        "visible_tools",
        "tool_ids",
        "present_tools",
        "active_tools",
    ):
        raw = value.get(key)
        if isinstance(raw, str):
            normalized = _label(raw)
            if normalized and normalized not in NONE_LABELS:
                result.add(normalized)
        elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
            for item in raw:
                normalized = _label(item)
                if normalized and normalized not in NONE_LABELS:
                    result.add(normalized)
    return result


def _primary_tool(value: Mapping[str, Any]) -> str | None:
    for key in ("tool", "primary_tool"):
        raw = value.get(key)
        if isinstance(raw, str):
            normalized = _label(raw)
            if normalized:
                return normalized
    tools = sorted(_tool_set(value))
    return tools[0] if len(tools) == 1 else None


def _multiclass_metrics(
    gold: Sequence[str | None], predicted: Sequence[str | None]
) -> dict[str, Any]:
    classes = sorted(
        {
            label
            for label in gold
            if label is not None
        }
    )
    per_class: dict[str, Any] = {}
    f1_values: list[float] = []
    recalls: list[float] = []
    for class_label in classes:
        binary = _binary_metrics(
            [value == class_label for value in gold],
            [value == class_label for value in predicted],
        )
        per_class[class_label] = binary
        if binary["f1"] is not None:
            f1_values.append(binary["f1"])
        if binary["recall"] is not None:
            recalls.append(binary["recall"])
    return {
        "support": len(gold),
        "classes": classes,
        "accuracy": _ratio(
            sum(g == p for g, p in zip(gold, predicted)), len(gold)
        ),
        "out_of_reference_class_predictions": sum(
            value is not None and value not in classes for value in predicted
        ),
        "macro_f1": (
            sum(f1_values) / len(f1_values) if f1_values else None
        ),
        "balanced_accuracy": (
            sum(recalls) / len(recalls) if recalls else None
        ),
        "per_class": per_class,
    }


def _set_macro_f1(
    gold: Sequence[set[str]], predicted: Sequence[set[str]]
) -> dict[str, Any]:
    classes = sorted(set().union(*gold) if gold else set())
    per_class: dict[str, Any] = {}
    f1_values: list[float] = []
    for class_label in classes:
        metrics = _binary_metrics(
            [class_label in labels for labels in gold],
            [class_label in labels for labels in predicted],
        )
        per_class[class_label] = metrics
        if metrics["f1"] is not None:
            f1_values.append(metrics["f1"])
    return {
        "support": len(gold),
        "classes": classes,
        "macro_f1": (
            sum(f1_values) / len(f1_values) if f1_values else None
        ),
        "exact_set_accuracy": _ratio(
            sum(g == p for g, p in zip(gold, predicted)), len(gold)
        ),
        "out_of_reference_class_predictions": sum(
            len(labels - set(classes)) for labels in predicted
        ),
        "per_class": per_class,
    }


def _evaluate_tools(
    examples: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]]
) -> dict[str, Any]:
    gold_sets = [_tool_set(reference) for reference, _ in examples]
    predicted_sets = [_tool_set(prediction) for _, prediction in examples]
    gold_primary = [_primary_tool(reference) for reference, _ in examples]
    predicted_primary = [_primary_tool(prediction) for _, prediction in examples]
    classification_indices = [
        index for index, value in enumerate(gold_primary) if value is not None
    ]
    classification = _multiclass_metrics(
        [gold_primary[index] for index in classification_indices],
        [predicted_primary[index] for index in classification_indices],
    )

    exhaustive_indices = [
        index
        for index, (reference, _) in enumerate(examples)
        if reference.get("exhaustive_presence") is True
        or reference.get("exhaustive_visible_tool_inventory") is True
    ]
    false_positive_count = 0
    predicted_label_count = 0
    hallucinated_examples = 0
    for index in exhaustive_indices:
        false_positives = predicted_sets[index] - gold_sets[index]
        false_positive_count += len(false_positives)
        predicted_label_count += len(predicted_sets[index])
        hallucinated_examples += bool(false_positives)

    return {
        "support": len(examples),
        "primary_classification": classification,
        "set_metrics": _set_macro_f1(gold_sets, predicted_sets),
        "hallucination": {
            "definition": (
                "Predicted tool labels absent from targets explicitly marked "
                "exhaustive_presence=true or "
                "exhaustive_visible_tool_inventory=true."
            ),
            "scorable_examples": len(exhaustive_indices),
            "false_positive_labels": false_positive_count,
            "predicted_labels": predicted_label_count,
            "label_rate": _ratio(
                false_positive_count, predicted_label_count
            ),
            "example_rate": _ratio(
                hallucinated_examples, len(exhaustive_indices)
            ),
            "unscored_non_exhaustive_examples": (
                len(examples) - len(exhaustive_indices)
            ),
        },
    }


def _phase_label(value: Mapping[str, Any]) -> str | None:
    for key in ("phase_id", "current_phase", "phase"):
        normalized = _label(value.get(key))
        if normalized:
            return normalized.upper()
    return None


def _transition(value: Mapping[str, Any]) -> bool | None:
    for key in ("is_transition", "transition"):
        raw = value.get(key)
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            normalized = _label(raw)
            if normalized in TRANSITION_LABELS:
                return True
            if normalized in {"false", "interior", "stable"}:
                return False
    state = _label(value.get("state") or value.get("phase_state"))
    if state in TRANSITION_LABELS:
        return True
    if state in {"interior", "stable"}:
        return False
    return None


def _evaluate_phases(
    examples: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]]
) -> dict[str, Any]:
    gold = [_phase_label(reference) for reference, _ in examples]
    predicted = [_phase_label(prediction) for _, prediction in examples]
    annotated = [index for index, value in enumerate(gold) if value is not None]
    classification = _multiclass_metrics(
        [gold[index] for index in annotated],
        [predicted[index] for index in annotated],
    )

    transition_gold: list[bool] = []
    transition_predicted: list[bool] = []
    missing_predictions = 0
    for reference, prediction in examples:
        gold_value = _transition(reference)
        if gold_value is None:
            continue
        predicted_value = _transition(prediction)
        if predicted_value is None:
            missing_predictions += 1
            predicted_value = not gold_value
        transition_gold.append(gold_value)
        transition_predicted.append(predicted_value)
    transition_metrics = _binary_metrics(
        transition_gold, transition_predicted
    )
    transition_metrics["missing_prediction_count"] = missing_predictions
    transition_metrics["definition"] = (
        "Binary transition-state detection on examples with an annotated "
        "reference state; this is not a boundary timing-error metric."
    )
    return {
        "support": len(examples),
        "phase_classification": classification,
        "transition_detection": transition_metrics,
    }


def _next_tool(value: Mapping[str, Any]) -> str:
    for key in ("next_transfer_tool", "next_tool", "tool"):
        normalized = _label(value.get(key))
        if normalized is not None:
            return "none" if normalized in NONE_LABELS else normalized
    event = _label(value.get("event"))
    return "none" if event in NONE_LABELS else "<missing>"


def _next_tool_stratum(
    row: Mapping[str, Any], reference: Mapping[str, Any]
) -> str:
    if _next_tool(reference) == "none":
        return "none"
    candidates = [
        row.get("stratum"),
        row.get("prediction_stratum"),
        row.get("prediction_regime"),
        reference.get("stratum"),
        reference.get("basis"),
    ]
    quality = row.get("quality")
    if isinstance(quality, Mapping):
        candidates.append(quality.get("stratum"))
    metadata = row.get("metadata")
    if isinstance(metadata, Mapping):
        candidates.append(metadata.get("stratum"))
    for candidate in candidates:
        normalized = _label(candidate)
        if not normalized:
            continue
        if "explicit" in normalized:
            return "explicit"
        if "implicit" in normalized or "silent" in normalized:
            return "implicit"
        if "anticip" in normalized or "context" in normalized:
            return "anticipatory"
        if normalized in NONE_LABELS:
            return "none"
        return normalized
    return "unspecified"


def _evaluate_next_tools(
    examples: Sequence[
        tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]
    ],
) -> dict[str, Any]:
    gold = [_next_tool(reference) for _, reference, _ in examples]
    predicted = [_next_tool(prediction) for _, _, prediction in examples]
    top1 = _ratio(sum(g == p for g, p in zip(gold, predicted)), len(gold))
    positive_indices = [
        index for index, value in enumerate(gold) if value != "none"
    ]
    none_indices = [index for index, value in enumerate(gold) if value == "none"]

    per_stratum_values: dict[str, list[bool]] = defaultdict(list)
    for (row, reference, _), gold_value, predicted_value in zip(
        examples, gold, predicted
    ):
        per_stratum_values[_next_tool_stratum(row, reference)].append(
            gold_value == predicted_value
        )
    per_stratum = {
        stratum: {
            "support": len(values),
            "top1_accuracy": _ratio(sum(values), len(values)),
        }
        for stratum, values in sorted(per_stratum_values.items())
    }
    gold_none = [value == "none" for value in gold]
    predicted_none = [
        (not gold_value) if predicted_value == "<missing>" else predicted_value == "none"
        for gold_value, predicted_value in zip(gold_none, predicted)
    ]
    none_binary = _binary_metrics(gold_none, predicted_none)
    none_binary["gold_none_top1_accuracy"] = _ratio(
        sum(gold[index] == predicted[index] for index in none_indices),
        len(none_indices),
    )
    return {
        "support": len(examples),
        "top1_accuracy": top1,
        "positive_top1_accuracy": _ratio(
            sum(gold[index] == predicted[index] for index in positive_indices),
            len(positive_indices),
        ),
        "none_detection": none_binary,
        "per_stratum": per_stratum,
        "classification": _multiclass_metrics(gold, predicted),
    }


def _normalize_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.strip().split())


def _token_f1(reference: str, prediction: str) -> float:
    reference_tokens = Counter(reference.lower().split())
    prediction_tokens = Counter(prediction.lower().split())
    overlap = sum((reference_tokens & prediction_tokens).values())
    precision = _ratio(overlap, sum(prediction_tokens.values()))
    recall = _ratio(overlap, sum(reference_tokens.values()))
    if precision is None and recall is None:
        return 1.0
    if precision is None or recall is None:
        return 0.0
    return _f1(precision, recall) or 0.0


def _flatten_entities(value: Any, prefix: str = "") -> set[str]:
    result: set[str] = set()
    if isinstance(value, str):
        normalized = _normalize_text(value).lower()
        if normalized:
            result.add(f"{prefix}:{normalized}" if prefix else normalized)
    elif isinstance(value, Mapping):
        for key, nested in value.items():
            nested_prefix = f"{prefix}.{key}" if prefix else str(key)
            result.update(_flatten_entities(nested, nested_prefix))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            result.update(_flatten_entities(item, prefix))
    return result


def _entity_set(value: Mapping[str, Any]) -> set[str] | None:
    for key in ("entities", "clinical_entities", "entity_slots"):
        if key in value:
            return _flatten_entities(value[key])
    return None


def _pair_set_f1(reference: set[str], prediction: set[str]) -> float:
    overlap = len(reference & prediction)
    precision = _ratio(overlap, len(prediction))
    recall = _ratio(overlap, len(reference))
    if precision is None and recall is None:
        return 1.0
    if precision is None or recall is None:
        return 0.0
    return _f1(precision, recall) or 0.0


def _evaluate_clinical(
    examples: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]]
) -> dict[str, Any]:
    slot_names = ("observation", "interpretation")
    slot_metrics: dict[str, Any] = {}
    joint_exact = 0
    joint_present = 0
    for slot in slot_names:
        exact = 0
        present = 0
        token_f1_values: list[float] = []
        for reference, prediction in examples:
            reference_text = _normalize_text(reference.get(slot))
            prediction_text = _normalize_text(prediction.get(slot))
            present += bool(prediction_text)
            exact += reference_text == prediction_text
            token_f1_values.append(_token_f1(reference_text, prediction_text))
        slot_metrics[slot] = {
            "support": len(examples),
            "prediction_present_rate": _ratio(present, len(examples)),
            "normalized_exact_match": _ratio(exact, len(examples)),
            "mean_whitespace_token_f1": (
                sum(token_f1_values) / len(token_f1_values)
                if token_f1_values
                else None
            ),
        }

    entity_f1_values: list[float] = []
    entity_exact = 0
    entity_prediction_missing = 0
    for reference, prediction in examples:
        reference_slots = tuple(
            _normalize_text(reference.get(slot)) for slot in slot_names
        )
        prediction_slots = tuple(
            _normalize_text(prediction.get(slot)) for slot in slot_names
        )
        joint_exact += reference_slots == prediction_slots
        joint_present += all(prediction_slots)
        reference_entities = _entity_set(reference)
        if reference_entities is None:
            continue
        prediction_entities = _entity_set(prediction)
        if prediction_entities is None:
            prediction_entities = set()
            entity_prediction_missing += 1
        entity_f1_values.append(
            _pair_set_f1(reference_entities, prediction_entities)
        )
        entity_exact += reference_entities == prediction_entities

    return {
        "support": len(examples),
        "slot_metrics": slot_metrics,
        "joint_slot_presence_rate": _ratio(joint_present, len(examples)),
        "joint_normalized_exact_match": _ratio(joint_exact, len(examples)),
        "entity_scaffold": {
            "definition": (
                "Exact/F1 matching over optional structured entities, "
                "clinical_entities, or entity_slots. No entities are inferred "
                "from free text."
            ),
            "annotated_support": len(entity_f1_values),
            "prediction_missing_count": entity_prediction_missing,
            "exact_match": _ratio(entity_exact, len(entity_f1_values)),
            "mean_set_f1": (
                sum(entity_f1_values) / len(entity_f1_values)
                if entity_f1_values
                else None
            ),
        },
    }


def _generic_exact(
    examples: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]]
) -> dict[str, Any]:
    return {
        "support": len(examples),
        "exact_match": _ratio(
            sum(reference == prediction for reference, prediction in examples),
            len(examples),
        ),
    }


def _evaluate_intents(
    examples: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]]
) -> dict[str, Any]:
    exact = _generic_exact(examples)
    gold = [_label(reference.get("intent")) for reference, _ in examples]
    predicted = [_label(prediction.get("intent")) for _, prediction in examples]
    annotated = [index for index, value in enumerate(gold) if value is not None]
    classification = _multiclass_metrics(
        [gold[index] for index in annotated],
        [predicted[index] for index in annotated],
    )
    return {
        **exact,
        "intent_label_accuracy": classification["accuracy"],
        "intent_classification": classification,
    }


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _string_or_none(value: Any) -> bool:
    return value is None or _nonempty_string(value)


def _task_schema_valid(
    task: str,
    reference: Mapping[str, Any],
    prediction: Mapping[str, Any],
) -> bool:
    """Validate the task contract separately from raw JSON syntax."""

    if task == "tool":
        required = {"event", "tool", "exhaustive_visible_tool_inventory"}
        if "from" in reference or "to" in reference:
            required.update({"from", "to"})
        if "view" in reference:
            required.add("view")
        string_fields = required - {"exhaustive_visible_tool_inventory"}
        return (
            required.issubset(prediction)
            and all(_nonempty_string(prediction[key]) for key in string_fields)
            and isinstance(
                prediction["exhaustive_visible_tool_inventory"], bool
            )
        )

    if task == "intent":
        required = {
            "event",
            "intent",
            "requested_tool",
            "tool_identity_inferred_from_later_transfer",
        }
        return (
            required.issubset(prediction)
            and _nonempty_string(prediction["event"])
            and _nonempty_string(prediction["intent"])
            and _string_or_none(prediction["requested_tool"])
            and isinstance(
                prediction["tool_identity_inferred_from_later_transfer"],
                bool,
            )
        )

    if task == "phase":
        required = {
            "phase_id",
            "phase_name_ko",
            "state",
            "transition_from",
            "transition_to",
        }
        if not required.issubset(prediction):
            return False
        state = _label(prediction["state"])
        if (
            not _nonempty_string(prediction["phase_id"])
            or not _nonempty_string(prediction["phase_name_ko"])
            or state not in {"interior", "transition"}
            or not _string_or_none(prediction["transition_from"])
            or not _string_or_none(prediction["transition_to"])
        ):
            return False
        if state == "interior":
            return (
                prediction["transition_from"] is None
                and prediction["transition_to"] is None
            )
        return (
            _nonempty_string(prediction["transition_from"])
            and _nonempty_string(prediction["transition_to"])
        )

    if task == "next_tool":
        required = {"next_transfer_tool", "event", "basis"}
        if not required.issubset(prediction) or not all(
            _nonempty_string(prediction[key]) for key in required
        ):
            return False
        predicted_tool = _next_tool(prediction)
        event = _label(prediction["event"])
        return (predicted_tool == "none") == (event in NONE_LABELS)

    if task == "clinical":
        required = {"observation", "interpretation", "confidence"}
        if (
            not required.issubset(prediction)
            or not _nonempty_string(prediction["observation"])
            or not _nonempty_string(prediction["interpretation"])
        ):
            return False
        confidence = prediction["confidence"]
        return (
            isinstance(confidence, Mapping)
            and _nonempty_string(confidence.get("observation"))
            and _nonempty_string(confidence.get("interpretation"))
        )

    return all(key in prediction for key in reference)


def evaluate_predictions(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Evaluate in-memory prediction rows and retain invalid JSON as failures."""

    input_errors: list[dict[str, Any]] = []
    scored_by_task: dict[
        str, list[tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]]
    ] = defaultdict(list)
    json_valid_by_task: Counter[str] = Counter()
    schema_valid_by_task: Counter[str] = Counter()
    row_count_by_task: Counter[str] = Counter()
    evaluable_count_by_task: Counter[str] = Counter()
    valid_predictions = 0
    invalid_predictions = 0
    schema_valid_predictions = 0
    invalid_references = 0

    for index, row in enumerate(rows):
        task = _task(row)
        example_id = row.get("example_id")
        identity = (
            example_id
            if isinstance(example_id, str) and example_id
            else f"<row:{index}>"
        )
        if task is None:
            input_errors.append(
                {
                    "code": "task_type_invalid",
                    "example_id": identity,
                    "message": f"Unsupported task_type {row.get('task_type')!r}.",
                }
            )
            continue
        row_count_by_task[task] += 1
        reference, reference_error = _json_object(_reference_raw(row))
        if reference_error:
            invalid_references += 1
            input_errors.append(
                {
                    "code": "reference_invalid",
                    "example_id": identity,
                    "message": reference_error,
                }
            )
            continue
        assert reference is not None
        evaluable_count_by_task[task] += 1
        prediction, semantic_error = _json_object(_prediction_raw(row))
        if semantic_error:
            prediction = {}
        assert prediction is not None

        raw_model_text = row.get("prediction_text")
        strict_source = (
            raw_model_text
            if isinstance(raw_model_text, str)
            else _prediction_raw(row)
        )
        _, strict_error = _json_object(strict_source)
        if strict_error:
            invalid_predictions += 1
            prediction = {}
        else:
            valid_predictions += 1
            json_valid_by_task[task] += 1
        if _task_schema_valid(task, reference, prediction):
            schema_valid_predictions += 1
            schema_valid_by_task[task] += 1
        scored_by_task[task].append((row, reference, prediction))

    total_scored_rows = sum(evaluable_count_by_task.values())
    json_validity = {
        "support": total_scored_rows,
        "valid_count": valid_predictions,
        "invalid_count": invalid_predictions,
        "valid_rate": _ratio(valid_predictions, total_scored_rows),
        "per_task": {
            task: {
                "support": evaluable_count_by_task[task],
                "valid_count": json_valid_by_task[task],
                "valid_rate": _ratio(
                    json_valid_by_task[task], evaluable_count_by_task[task]
                ),
            }
            for task in sorted(evaluable_count_by_task)
        },
    }
    task_schema_compliance = {
        "support": total_scored_rows,
        "valid_count": schema_valid_predictions,
        "invalid_count": total_scored_rows - schema_valid_predictions,
        "valid_rate": _ratio(schema_valid_predictions, total_scored_rows),
        "per_task": {
            task: {
                "support": evaluable_count_by_task[task],
                "valid_count": schema_valid_by_task[task],
                "valid_rate": _ratio(
                    schema_valid_by_task[task],
                    evaluable_count_by_task[task],
                ),
            }
            for task in sorted(evaluable_count_by_task)
        },
    }

    metrics: dict[str, Any] = {}
    for task, examples in sorted(scored_by_task.items()):
        pairs = [(reference, prediction) for _, reference, prediction in examples]
        if task == "tool":
            metrics[task] = _evaluate_tools(pairs)
        elif task == "phase":
            metrics[task] = _evaluate_phases(pairs)
        elif task == "next_tool":
            metrics[task] = _evaluate_next_tools(examples)
        elif task == "clinical":
            metrics[task] = _evaluate_clinical(pairs)
        elif task == "intent":
            metrics[task] = _evaluate_intents(pairs)
        else:
            metrics[task] = _generic_exact(pairs)
        metrics[task]["json_valid_support"] = json_valid_by_task[task]
        metrics[task]["schema_valid_support"] = schema_valid_by_task[task]
        metrics[task]["schema_valid_rate"] = _ratio(
            schema_valid_by_task[task], evaluable_count_by_task[task]
        )
        metrics[task]["total_support"] = evaluable_count_by_task[task]

    return {
        "schema": SCHEMA,
        "ok": not input_errors,
        "summary": {
            "rows": len(rows),
            "scored_rows": total_scored_rows,
            "task_counts": dict(sorted(row_count_by_task.items())),
            "invalid_reference_count": invalid_references,
            "input_error_count": len(input_errors),
        },
        "json_validity": json_validity,
        "task_schema_compliance": task_schema_compliance,
        "metrics": metrics,
        "input_errors": input_errors,
        "notes": [
            (
                "Invalid model JSON is scored as an empty prediction and is "
                "also reported explicitly in json_validity."
            ),
            (
                "task_schema_compliance checks required keys, value types, "
                "and task-specific state consistency independently of raw "
                "JSON validity."
            ),
            (
                "Clinical free-text token F1 is a diagnostic, not a clinical "
                "correctness metric. Structured entity scoring requires "
                "reference entity slots."
            ),
        ],
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compute offline metrics from causal VLM prediction JSONL."
    )
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        rows = load_jsonl(args.predictions)
        report = evaluate_predictions(rows)
    except (OSError, ValueError) as exc:
        print(f"eval_vlm_sft: {exc}", file=sys.stderr)
        return 2
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
