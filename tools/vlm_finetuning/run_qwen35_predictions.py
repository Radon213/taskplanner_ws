#!/usr/bin/env python3
"""Run deterministic Qwen3.5 surgical VLM predictions for base/LoRA comparison."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_MODEL = (
    "/home/arl/.cache/huggingface/hub/models--unsloth--Qwen3.5-4B/"
    "snapshots/3764fa359b9082ea5a1e4a5e3ac3aaf6e9671636"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--split", default="test")
    parser.add_argument("--max-per-task", type=int, default=24)
    parser.add_argument("--max-new-tokens", type=int, default=384)
    parser.add_argument("--seed", type=int, default=3407)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--selection-manifest", type=Path)
    parser.add_argument("--limit", type=int)
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row.get("messages"), list):
                raise ValueError(f"{path}:{line_number}: missing messages")
            rows.append(row)
    return rows


def get_answer_text(row: dict[str, Any]) -> str:
    assistant = row["messages"][-1]
    if assistant.get("role") != "assistant":
        raise ValueError(f"{row.get('example_id')}: final message is not assistant")
    content = assistant.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        )
    raise ValueError(f"{row.get('example_id')}: invalid assistant content")


def strip_thinking(text: str) -> str:
    if "</think>" in text:
        text = text.split("</think>", 1)[1]
    text = re.sub(r"^\s*<think>.*?</think>\s*", "", text, flags=re.DOTALL)
    return text.strip()


def parse_json_answer(text: str) -> tuple[Any | None, str | None]:
    clean = strip_thinking(text)
    candidates = [clean]
    fenced = re.search(r"```(?:json)?\s*(.*?)```", clean, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        candidates.insert(0, fenced.group(1).strip())
    first = clean.find("{")
    last = clean.rfind("}")
    if first >= 0 and last > first:
        candidates.append(clean[first : last + 1])
    errors: list[str] = []
    for candidate in candidates:
        try:
            return json.loads(candidate), None
        except json.JSONDecodeError as exc:
            errors.append(str(exc))
    return None, "; ".join(errors[:3])


def select_rows(
    rows: list[dict[str, Any]],
    split: str,
    max_per_task: int,
    selection_manifest: Path | None,
    limit: int | None,
) -> list[dict[str, Any]]:
    def split_role(row: dict[str, Any]) -> str:
        value = row.get("split", "train")
        if isinstance(value, dict):
            value = value.get("role", "train")
        return str(value)

    candidates = [row for row in rows if split_role(row) == split]
    candidates.sort(key=lambda row: str(row.get("example_id", "")))
    if selection_manifest:
        wanted = set(json.loads(selection_manifest.read_text(encoding="utf-8"))["example_ids"])
        selected = [row for row in candidates if row.get("example_id") in wanted]
        missing = wanted - {row.get("example_id") for row in selected}
        if missing:
            raise ValueError(f"selection manifest references {len(missing)} missing rows")
    else:
        per_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in candidates:
            per_task[str(row.get("task_type", "unknown"))].append(row)
        selected = []
        for task in sorted(per_task):
            # Round-robin over case and target strata instead of taking the
            # lexicographically first rows. This keeps the locked subset from
            # collapsing onto one case or only common tool/phase labels.
            groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
            for row in per_task[task]:
                gold, _ = parse_json_answer(get_answer_text(row))
                label = "unstratified"
                if isinstance(gold, dict):
                    if task in {"tool_presence_at_transfer", "tool_presence_pseudo"}:
                        label = str(gold.get("tool", label))
                    elif task == "current_phase":
                        label = (
                            f"{gold.get('phase_id', 'unknown')}:"
                            f"{gold.get('state', 'unknown')}"
                        )
                    elif task == "next_physical_tool":
                        label = str(gold.get("next_transfer_tool", label))
                    elif task == "request_intent":
                        label = str(gold.get("intent", label))
                groups[(str(row.get("case_id", "unknown")), label)].append(row)
            task_selected: list[dict[str, Any]] = []
            group_keys = sorted(groups)
            offset = 0
            while len(task_selected) < max_per_task:
                added = False
                for key in group_keys:
                    if offset < len(groups[key]):
                        task_selected.append(groups[key][offset])
                        added = True
                        if len(task_selected) >= max_per_task:
                            break
                if not added:
                    break
                offset += 1
            selected.extend(task_selected)
        selected.sort(key=lambda row: str(row.get("example_id", "")))
    if limit is not None:
        selected = selected[:limit]
    return selected


def main() -> int:
    args = parse_args()
    rows = read_jsonl(args.dataset)
    selected = select_rows(
        rows,
        split=args.split,
        max_per_task=args.max_per_task,
        selection_manifest=args.selection_manifest,
        limit=args.limit,
    )
    if not selected:
        raise ValueError(f"no examples selected for split={args.split!r}")

    # Unsloth must be imported before torch/transformers.
    import unsloth  # noqa: F401
    import torch
    from unsloth import FastVisionModel

    torch.manual_seed(args.seed)
    torch.cuda.manual_seed_all(args.seed)
    torch.cuda.reset_peak_memory_stats()

    model, processor = FastVisionModel.from_pretrained(
        model_name=args.model,
        max_seq_length=2048,
        dtype=torch.bfloat16,
        load_in_4bit=False,
        load_in_16bit=True,
        full_finetuning=False,
        use_gradient_checkpointing=False,
        fast_inference=False,
        random_state=args.seed,
        local_files_only=True,
    )
    FastVisionModel.for_inference(model)
    model.eval()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    selection_path = args.output.with_suffix(args.output.suffix + ".selection.json")
    selection_path.write_text(
        json.dumps(
            {
                "schema": "taskplanner.vlm_eval_selection.v1",
                "dataset": str(args.dataset.resolve()),
                "dataset_sha256": sha256_file(args.dataset),
                "split": args.split,
                "selection_strategy": (
                    "provided_manifest"
                    if args.selection_manifest
                    else "round_robin_case_and_target_stratum"
                ),
                "max_per_task": args.max_per_task,
                "example_ids": [row.get("example_id") for row in selected],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    started = datetime.now(timezone.utc)
    latencies: list[float] = []
    valid_json = 0
    with args.output.open("w", encoding="utf-8") as handle:
        for index, row in enumerate(selected, 1):
            prompt_messages = row["messages"][:-1]
            inputs = processor.apply_chat_template(
                prompt_messages,
                add_generation_prompt=True,
                tokenize=True,
                return_dict=True,
                return_tensors="pt",
                enable_thinking=False,
            )
            inputs = {
                key: value.to(model.device) if hasattr(value, "to") else value
                for key, value in inputs.items()
            }
            input_length = int(inputs["input_ids"].shape[-1])
            tokenizer = getattr(processor, "tokenizer", processor)
            generation_kwargs: dict[str, Any] = {
                "max_new_tokens": args.max_new_tokens,
                "use_cache": True,
                "pad_token_id": tokenizer.pad_token_id,
                "eos_token_id": tokenizer.eos_token_id,
            }
            if args.temperature > 0:
                generation_kwargs.update(
                    {
                        "do_sample": True,
                        "temperature": args.temperature,
                        "top_p": 0.8,
                    }
                )
            else:
                generation_kwargs["do_sample"] = False

            torch.cuda.synchronize()
            tick = time.perf_counter()
            with torch.inference_mode():
                output_ids = model.generate(**inputs, **generation_kwargs)
            torch.cuda.synchronize()
            latency = time.perf_counter() - tick
            latencies.append(latency)
            generated_ids = output_ids[0, input_length:]
            raw_prediction = processor.decode(
                generated_ids,
                skip_special_tokens=True,
                clean_up_tokenization_spaces=False,
            )
            parsed_prediction, parse_error = parse_json_answer(raw_prediction)
            if parsed_prediction is not None:
                valid_json += 1
            gold_text = get_answer_text(row)
            gold_value, gold_error = parse_json_answer(gold_text)
            result = {
                "schema": "taskplanner.vlm_prediction.v1",
                "example_id": row.get("example_id"),
                "case_id": row.get("case_id"),
                "task_type": row.get("task_type"),
                "prediction_regime": row.get("prediction_regime"),
                "split": (
                    row.get("split", {}).get("role")
                    if isinstance(row.get("split"), dict)
                    else row.get("split")
                ),
                "authority": row.get("authority"),
                "gold": gold_value if gold_value is not None else gold_text,
                "gold_parse_error": gold_error,
                "prediction": parsed_prediction,
                "prediction_text": strip_thinking(raw_prediction),
                "prediction_parse_error": parse_error,
                "latency_sec": latency,
                "input_tokens": input_length,
                "output_tokens": int(generated_ids.numel()),
                "model_path_or_id": args.model,
            }
            handle.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            print(
                f"[{index}/{len(selected)}] {row.get('task_type')} "
                f"{row.get('example_id')} {latency:.3f}s json={parse_error is None}",
                flush=True,
            )

    summary = {
        "schema": "taskplanner.vlm_prediction_summary.v1",
        "started_at": started.isoformat(),
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "dataset": str(args.dataset.resolve()),
        "dataset_sha256": sha256_file(args.dataset),
        "model_path_or_id": args.model,
        "split": args.split,
        "example_count": len(selected),
        "valid_json_count": valid_json,
        "valid_json_rate": valid_json / len(selected),
        "latency_mean_sec": sum(latencies) / len(latencies),
        "latency_max_sec": max(latencies),
        "peak_reserved_gib": round(torch.cuda.max_memory_reserved() / 1024**3, 3),
        "peak_allocated_gib": round(torch.cuda.max_memory_allocated() / 1024**3, 3),
        "predictions_sha256": sha256_file(args.output),
        "selection_manifest": str(selection_path.resolve()),
    }
    summary_path = args.output.with_suffix(args.output.suffix + ".summary.json")
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
