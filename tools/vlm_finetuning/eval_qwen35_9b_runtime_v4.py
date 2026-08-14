#!/usr/bin/env python3
"""Evaluate a Qwen3.5 model/adapter on held-out Taskplanner schema-v4 rows."""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


DEFAULT_PROCESSOR = "/run/media/arl/42AEF80BAEF7F4EF/qwen35_9b_posttrained_official"
VALID_PHASES = {f"P{index:02d}" for index in range(1, 13)}
VALID_TOOLS = {"T01", "T02", "T03", "T04", "T05", "T07", "T08", "T11"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--processor-model", default=DEFAULT_PROCESSOR)
    parser.add_argument("--split", default="test")
    parser.add_argument(
        "--tasks",
        default="forecast,gesture,intent,phase,summary",
        help="Comma-separated task rows to evaluate.",
    )
    parser.add_argument("--max-per-task", type=int, default=0)
    parser.add_argument("--seed", type=int, default=3509)
    parser.add_argument("--max-length", type=int, default=8192)
    parser.add_argument("--max-new-tokens", type=int, default=320)
    parser.add_argument("--min-pixels", type=int, default=65536)
    parser.add_argument("--max-pixels", type=int, default=196608)
    parser.add_argument("--trigger-threshold", type=float, default=0.65)
    return parser.parse_args()


def read_rows(path: Path, split: str, tasks: set[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("split") == split and row.get("task") in tasks:
                image_path = Path(str(row["image_path"]))
                if not image_path.is_absolute():
                    image_path = (path.parent / image_path).resolve()
                row["image_path"] = str(image_path)
                for message in row.get("prompt_messages", []):
                    for item in message.get("content", []):
                        if isinstance(item, dict) and item.get("type") == "image":
                            item["image"] = str(image_path)
                rows.append(row)
    return rows


def select_balanced(rows: list[dict[str, Any]], limit: int, seed: int) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["task"])].append(row)
    selected: list[dict[str, Any]] = []
    for offset, task in enumerate(sorted(grouped)):
        task_rows = sorted(grouped[task], key=lambda row: str(row["example_id"]))
        if limit > 0 and len(task_rows) > limit:
            strata: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
            for row in task_rows:
                target = row.get("completion_json", {})
                if task == "forecast":
                    tool = target.get("tool", [[""]])
                    tool_id = str(tool[0][0]) if isinstance(tool, list) and tool and isinstance(tool[0], list) and tool[0] else ""
                    # Candidate anchors can overlap an earlier transfer.  In that
                    # case the serialized target correctly becomes an
                    # outside-window negative even though the original anchor
                    # tag still says positive.  Stratify on the resolved target
                    # semantics, not the pre-resolution candidate tag.
                    key = (str(row.get("semantic", {}).get("derived_forecast_kind", "")), tool_id)
                elif task in {"gesture", "intent"}:
                    key = (str(row.get("semantic", {}).get("anchor_kind", "")),)
                elif task == "phase":
                    phase = target.get("phase", [[""]])
                    phase_id = str(phase[0][0]) if isinstance(phase, list) and phase and isinstance(phase[0], list) and phase[0] else ""
                    key = (phase_id,)
                else:
                    key = (str(row.get("case_id", "")),)
                strata[key].append(row)
            rng = random.Random(seed + offset)
            for stratum_rows in strata.values():
                rng.shuffle(stratum_rows)
            task_rows = []
            ordered_keys = sorted(strata)
            while len(task_rows) < limit and any(strata.values()):
                for key in ordered_keys:
                    if strata[key] and len(task_rows) < limit:
                        task_rows.append(strata[key].pop())
        selected.extend(task_rows)
    return sorted(selected, key=lambda row: (str(row["task"]), str(row["example_id"])))


def extract_json(text: str) -> tuple[dict[str, Any] | None, str]:
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    start = cleaned.find("{")
    if start < 0:
        return None, "no_json_object"
    try:
        value, _ = json.JSONDecoder().raw_decode(cleaned[start:])
    except json.JSONDecodeError as exc:
        return None, f"json_decode:{exc.msg}"
    if not isinstance(value, dict):
        return None, "json_not_object"
    return value, ""


def finite_confidence(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)) and 0.0 <= float(value) <= 1.0


def validate_shape(value: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    required = {"v", "phase", "tool", "intent", "gesture", "mayo", "mayo_retrieve", "u", "sum", "bed_robot_arm_group"}
    if set(value) != required:
        errors.append("keys")
    if value.get("v") != "4":
        errors.append("v")
    for field, valid_ids in (("phase", VALID_PHASES), ("tool", VALID_TOOLS)):
        rows = value.get(field)
        if not isinstance(rows, list) or not (1 <= len(rows) <= 4):
            errors.append(field)
            continue
        if any(not isinstance(row, list) or len(row) != 2 or row[0] not in valid_ids or not finite_confidence(row[1]) for row in rows):
            errors.append(field)
    intent = value.get("intent")
    if not isinstance(intent, list) or len(intent) != 3 or not finite_confidence(intent[-1] if isinstance(intent, list) and intent else None):
        errors.append("intent")
    gesture = value.get("gesture")
    if not isinstance(gesture, list) or len(gesture) != 4 or not finite_confidence(gesture[-1] if isinstance(gesture, list) and gesture else None):
        errors.append("gesture")
    mayo = value.get("mayo")
    if not isinstance(mayo, list) or any(not isinstance(row, list) or len(row) != 3 or row[0] not in VALID_TOOLS or not finite_confidence(row[2]) for row in (mayo if isinstance(mayo, list) else [])):
        errors.append("mayo")
    retrieve = value.get("mayo_retrieve")
    if not isinstance(retrieve, list) or len(retrieve) != 2 or not finite_confidence(retrieve[-1] if isinstance(retrieve, list) and retrieve else None):
        errors.append("mayo_retrieve")
    if not finite_confidence(value.get("u")):
        errors.append("u")
    if not isinstance(value.get("sum"), str):
        errors.append("sum")
    bed = value.get("bed_robot_arm_group")
    if bed is not None and not isinstance(bed, dict):
        errors.append("bed_robot_arm_group")
    return sorted(set(errors))


def top_candidate(value: Any) -> tuple[str, float]:
    if not isinstance(value, list) or not value:
        return "", 0.0
    first = value[0]
    if not isinstance(first, list) or len(first) != 2:
        return "", 0.0
    return str(first[0]), float(first[1]) if finite_confidence(first[1]) else 0.0


def candidate_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(row[0]) for row in value if isinstance(row, list) and len(row) == 2]


def binary_prf(tp: int, fp: int, fn: int) -> dict[str, float | int]:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall, "f1": f1}


def multiset_f1(expected: list[str], predicted: list[str]) -> float:
    expected_counts, predicted_counts = Counter(expected), Counter(predicted)
    matched = sum((expected_counts & predicted_counts).values())
    if not expected and not predicted:
        return 1.0
    precision = matched / len(predicted) if predicted else 0.0
    recall = matched / len(expected) if expected else 0.0
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def lcs_length(left: list[str], right: list[str]) -> int:
    previous = [0] * (len(right) + 1)
    for token in left:
        current = [0]
        for index, other in enumerate(right, 1):
            current.append(previous[index - 1] + 1 if token == other else max(previous[index], current[-1]))
        previous = current
    return previous[-1]


def rouge_l_f1(expected: str, predicted: str) -> float:
    left = re.findall(r"[a-z0-9]+", expected.lower())
    right = re.findall(r"[a-z0-9]+", predicted.lower())
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    common = lcs_length(left, right)
    precision = common / len(right)
    recall = common / len(left)
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def safe_mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def summarize(predictions: list[dict[str, Any]], threshold: float) -> dict[str, Any]:
    parsed = [row for row in predictions if row["parsed"] is not None]
    valid = [row for row in parsed if not row["shape_errors"]]
    summary: dict[str, Any] = {
        "count": len(predictions),
        "parse_rate": len(parsed) / len(predictions) if predictions else 0.0,
        "schema_valid_rate": len(valid) / len(predictions) if predictions else 0.0,
        "latency_sec_mean": safe_mean([float(row["latency_sec"]) for row in predictions]),
        "latency_sec_p95": sorted(float(row["latency_sec"]) for row in predictions)[max(0, math.ceil(len(predictions) * 0.95) - 1)] if predictions else 0.0,
        "bed_null_rate": safe_mean([1.0 if row["parsed"].get("bed_robot_arm_group") is None else 0.0 for row in parsed]),
    }

    forecast_rows = [row for row in parsed if row["task"] == "forecast"]
    positive_top1 = []
    positive_topk = []
    tp = fp = fn = tn = 0
    for row in forecast_rows:
        expected = row["expected"]
        predicted = row["parsed"]
        is_positive = row["derived_forecast_kind"] == "imminent_2_8_sec"
        expected_id, _ = top_candidate(expected.get("tool"))
        predicted_id, confidence = top_candidate(predicted.get("tool"))
        triggered = confidence >= threshold
        if is_positive:
            correct_trigger = triggered and predicted_id == expected_id
            tp += int(correct_trigger)
            fn += int(not correct_trigger)
            positive_top1.append(1.0 if predicted_id == expected_id else 0.0)
            positive_topk.append(1.0 if expected_id in candidate_ids(predicted.get("tool")) else 0.0)
        else:
            fp += int(triggered)
            tn += int(not triggered)
    summary["forecast"] = {
        "count": len(forecast_rows),
        "positive_count": len(positive_top1),
        "positive_top1_accuracy": safe_mean(positive_top1),
        "positive_topk_recall": safe_mean(positive_topk),
        "threshold": threshold,
        "trigger": binary_prf(tp, fp, fn) | {"tn": tn},
    }

    gesture_rows = [row for row in parsed if row["task"] == "gesture"]
    gtp = gfp = gfn = gtn = 0
    for row in gesture_rows:
        expected_positive = row["expected"].get("gesture", [""])[0] == "request_tool"
        gesture = row["parsed"].get("gesture")
        predicted_positive = bool(
            isinstance(gesture, list)
            and len(gesture) == 4
            and gesture[0] == "request_tool"
            and finite_confidence(gesture[3])
            and float(gesture[3]) >= threshold
        )
        gtp += int(expected_positive and predicted_positive)
        gfp += int(not expected_positive and predicted_positive)
        gfn += int(expected_positive and not predicted_positive)
        gtn += int(not expected_positive and not predicted_positive)
    summary["gesture"] = {"count": len(gesture_rows), "trigger": binary_prf(gtp, gfp, gfn) | {"tn": gtn}}

    intent_rows = [row for row in parsed if row["task"] == "intent"]
    intent_exact = []
    for row in intent_rows:
        expected = row["expected"].get("intent", [])
        predicted = row["parsed"].get("intent", [])
        expected_key = tuple(expected[:2]) if isinstance(expected, list) else ()
        predicted_key = tuple(predicted[:2]) if isinstance(predicted, list) else ()
        intent_exact.append(1.0 if expected_key == predicted_key else 0.0)
    summary["intent"] = {"count": len(intent_rows), "semantic_exact_accuracy": safe_mean(intent_exact)}

    phase_rows = [row for row in parsed if row["task"] == "phase"]
    phase_scores = []
    for row in phase_rows:
        expected_id, _ = top_candidate(row["expected"].get("phase"))
        predicted_id, _ = top_candidate(row["parsed"].get("phase"))
        phase_scores.append(1.0 if expected_id == predicted_id else 0.0)
    summary["phase"] = {"count": len(phase_rows), "top1_accuracy": safe_mean(phase_scores)}

    summary_rows = [row for row in parsed if row["task"] == "summary"]
    rouge = [rouge_l_f1(str(row["expected"].get("sum", "")), str(row["parsed"].get("sum", ""))) for row in summary_rows]
    summary["summary_teacher_agreement"] = {"count": len(summary_rows), "rouge_l_f1_mean": safe_mean(rouge)}

    mayo_scores = []
    for row in parsed:
        expected_ids = [str(item[0]) for item in row["expected"].get("mayo", []) if isinstance(item, list) and item]
        predicted_ids = [str(item[0]) for item in row["parsed"].get("mayo", []) if isinstance(item, list) and item]
        mayo_scores.append(multiset_f1(expected_ids, predicted_ids))
    summary["mayo_rfdetr_agreement_not_ground_truth"] = {
        "count": len(mayo_scores),
        "multiset_f1_mean": safe_mean(mayo_scores),
    }
    return summary


def main() -> int:
    args = parse_args()
    args.dataset = args.dataset.resolve()
    args.output_dir = args.output_dir.resolve()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    tasks = {part.strip() for part in args.tasks.split(",") if part.strip()}
    rows = select_balanced(read_rows(args.dataset, args.split, tasks), args.max_per_task, args.seed)
    if not rows:
        raise ValueError("no evaluation rows selected")

    import unsloth  # noqa: F401
    import torch
    from PIL import Image
    from transformers import AutoProcessor
    from unsloth import FastVisionModel

    model, _ = FastVisionModel.from_pretrained(
        model_name=args.model,
        max_seq_length=args.max_length,
        dtype=torch.bfloat16,
        load_in_4bit=False,
        load_in_16bit=True,
        full_finetuning=False,
        fast_inference=False,
        local_files_only=True,
    )
    processor = AutoProcessor.from_pretrained(
        args.processor_model,
        min_pixels=args.min_pixels,
        max_pixels=args.max_pixels,
        local_files_only=True,
    )
    FastVisionModel.for_inference(model)
    model.eval()

    output_path = args.output_dir / "predictions.jsonl"
    predictions: list[dict[str, Any]] = []
    with output_path.open("w", encoding="utf-8") as handle:
        for index, row in enumerate(rows, 1):
            prompt = processor.apply_chat_template(
                row["prompt_messages"],
                tokenize=False,
                add_generation_prompt=True,
                enable_thinking=False,
            )
            with Image.open(row["image_path"]) as source:
                inputs = processor(
                    text=[prompt],
                    images=[source.convert("RGB")],
                    padding=False,
                    return_tensors="pt",
                )
            inputs = inputs.to(model.device)
            started = time.perf_counter()
            with torch.inference_mode():
                generated = model.generate(
                    **inputs,
                    max_new_tokens=args.max_new_tokens,
                    do_sample=False,
                    use_cache=True,
                    eos_token_id=processor.tokenizer.eos_token_id,
                    pad_token_id=processor.tokenizer.pad_token_id,
                )
            latency = time.perf_counter() - started
            generated_ids = generated[0, inputs["input_ids"].shape[1] :]
            raw_text = processor.tokenizer.decode(generated_ids, skip_special_tokens=False)
            parsed, parse_error = extract_json(raw_text)
            shape_errors = validate_shape(parsed) if parsed is not None else []
            record = {
                "example_id": row["example_id"],
                "case_id": row["case_id"],
                "split": row["split"],
                "task": row["task"],
                "anchor_kind": row.get("semantic", {}).get("anchor_kind", ""),
                "derived_forecast_kind": row.get("semantic", {}).get("derived_forecast_kind", ""),
                "expected": row["completion_json"],
                "raw_text": raw_text,
                "parsed": parsed,
                "parse_error": parse_error,
                "shape_errors": shape_errors,
                "latency_sec": latency,
            }
            predictions.append(record)
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            print(f"[{index}/{len(rows)}] {row['task']} {row['example_id']} parse={parsed is not None} latency={latency:.2f}s", flush=True)

    summary = summarize(predictions, args.trigger_threshold)
    summary.update({
        "schema": "taskplanner.qwen35_9b_runtime_v4_evaluation.v1",
        "model": args.model,
        "processor_model": args.processor_model,
        "dataset": str(args.dataset),
        "split": args.split,
        "tasks": sorted(tasks),
        "max_per_task": args.max_per_task,
        "seed": args.seed,
        "predictions": str(output_path),
    })
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
