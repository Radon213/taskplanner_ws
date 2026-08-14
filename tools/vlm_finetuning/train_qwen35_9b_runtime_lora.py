#!/usr/bin/env python3
"""Train a BF16 Qwen3.5-9B LoRA on the exact Taskplanner schema-v4 contract.

Unlike the older causal trainer, this script respects per-field supervision
spans.  It therefore learns reviewed gesture/forecast labels without turning
unreviewed placeholder fields into accidental targets.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import random
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_MODEL = os.environ.get(
    "TASKPLANNER_QWEN35_9B_MODEL",
    "/run/media/arl/42AEF80BAEF7F4EF/qwen35_9b_posttrained_official",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--split", default="train")
    parser.add_argument("--eval-split", default="validation")
    parser.add_argument(
        "--train-tasks",
        default="",
        help="Optional comma-separated task allow-list applied only to training rows.",
    )
    parser.add_argument("--max-examples", type=int)
    parser.add_argument("--max-eval-examples", type=int, default=128)
    parser.add_argument(
        "--task-repeat",
        action="append",
        default=[],
        metavar="TASK=N",
        help=(
            "Repeat selected training tasks N times without changing validation. "
            "May be passed more than once, for example forecast=3."
        ),
    )
    parser.add_argument(
        "--forecast-tool-balance-target",
        type=int,
        default=0,
        help=(
            "When positive, deterministically resample each supported forecast "
            "tool to this many positive rows and keep an equal number of "
            "stratified outside-window negatives. Validation is unchanged."
        ),
    )
    parser.add_argument(
        "--forecast-tool-balance-min-source",
        type=int,
        default=2,
        help="Do not expand a positive tool class with fewer source rows than this.",
    )
    # The checked-out actor-log prompt plus one 196608-pixel composite reaches
    # about 4.34k tokens; 4608 fits every audited row while staying below the
    # deployed vLLM manager's 8192-token context.
    parser.add_argument("--max-length", type=int, default=4608)
    parser.add_argument("--min-pixels", type=int, default=65536)
    parser.add_argument("--max-pixels", type=int, default=196608)
    parser.add_argument("--epochs", type=float, default=1.0)
    parser.add_argument("--max-steps", type=int, default=-1)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--eval-batch-size", type=int, default=1)
    parser.add_argument("--gradient-accumulation-steps", type=int, default=8)
    parser.add_argument("--learning-rate", type=float, default=5.0e-5)
    parser.add_argument("--warmup-ratio", type=float, default=0.05)
    parser.add_argument("--weight-decay", type=float, default=0.01)
    parser.add_argument("--lora-rank", type=int, default=8)
    parser.add_argument("--lora-alpha", type=int, default=16)
    parser.add_argument("--logging-steps", type=int, default=1)
    parser.add_argument("--eval-steps", type=int, default=25)
    parser.add_argument("--save-steps", type=int, default=25)
    parser.add_argument("--save-total-limit", type=int, default=4)
    parser.add_argument("--seed", type=int, default=3509)
    parser.add_argument(
        "--resume-from-checkpoint",
        nargs="?",
        const="auto",
        help="Resume from a checkpoint, or select the latest with no value.",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Run two optimizer steps on four rows and do not evaluate.",
    )
    parser.add_argument(
        "--finetune-vision-layers",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument(
        "--finetune-language-layers",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
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
            required = (
                "example_id",
                "split",
                "task",
                "image_path",
                "prompt_messages",
                "completion",
                "supervision_char_spans",
            )
            missing = [key for key in required if key not in row]
            if missing:
                raise ValueError(f"{path}:{line_number}: missing {missing}")
            rows.append(row)
    return rows


def validate_rows(rows: list[dict[str, Any]]) -> None:
    seen: set[str] = set()
    train_cases: set[str] = set()
    eval_cases: set[str] = set()
    for row in rows:
        example_id = str(row["example_id"])
        if example_id in seen:
            raise ValueError(f"duplicate example_id: {example_id}")
        seen.add(example_id)
        path = Path(row["image_path"])
        if not path.is_file():
            raise FileNotFoundError(f"{example_id}: missing image {path}")
        completion = str(row["completion"])
        json.loads(completion)
        for span in row["supervision_char_spans"]:
            if not (
                isinstance(span, list)
                and len(span) == 2
                and 0 <= int(span[0]) < int(span[1]) <= len(completion)
            ):
                raise ValueError(f"{example_id}: invalid supervision span {span}")
        if row["split"] == "train":
            train_cases.add(str(row["case_id"]))
        else:
            eval_cases.add(str(row["case_id"]))
        if row["task"] == "mayo" and row["split"] != "train":
            raise ValueError(f"{example_id}: Mayo pseudo label outside train")
    overlap = train_cases & eval_cases
    if overlap:
        raise ValueError(f"case-group leakage: {sorted(overlap)}")


def resolve_image_paths(rows: list[dict[str, Any]], dataset_dir: Path) -> None:
    """Resolve portable dataset-relative media paths for the current host."""

    for row in rows:
        image_path = Path(str(row["image_path"]))
        if not image_path.is_absolute():
            image_path = (dataset_dir / image_path).resolve()
        row["image_path"] = str(image_path)
        for message in row.get("prompt_messages", []):
            for item in message.get("content", []):
                if isinstance(item, dict) and item.get("type") == "image":
                    item["image"] = str(image_path)


def select_rows(
    rows: list[dict[str, Any]], split: str, limit: int | None, seed: int
) -> list[dict[str, Any]]:
    selected = [row for row in rows if row["split"] == split]
    selected.sort(key=lambda row: str(row["example_id"]))
    if limit is not None and len(selected) > limit:
        random.Random(seed).shuffle(selected)
        selected = selected[:limit]
        selected.sort(key=lambda row: str(row["example_id"]))
    return selected


def parse_task_repeats(values: list[str]) -> dict[str, int]:
    repeats: dict[str, int] = {}
    for value in values:
        task, separator, raw_count = value.partition("=")
        task = task.strip()
        if not separator or not task:
            raise ValueError(f"invalid --task-repeat {value!r}; expected TASK=N")
        try:
            count = int(raw_count)
        except ValueError as exc:
            raise ValueError(f"invalid --task-repeat count in {value!r}") from exc
        if count < 1 or count > 20:
            raise ValueError(f"--task-repeat count must be 1..20: {value!r}")
        repeats[task] = count
    return repeats


def apply_task_repeats(
    rows: list[dict[str, Any]], repeats: dict[str, int], seed: int
) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    for row in rows:
        expanded.extend([row] * repeats.get(str(row["task"]), 1))
    random.Random(seed).shuffle(expanded)
    return expanded


def _forecast_tool_id(row: dict[str, Any]) -> str:
    tool = row.get("completion_json", {}).get("tool", [])
    if not isinstance(tool, list) or not tool or not isinstance(tool[0], list) or not tool[0]:
        return ""
    return str(tool[0][0])


def _resample_group(
    rows: list[dict[str, Any]], target: int, rng: random.Random
) -> list[dict[str, Any]]:
    if target <= 0 or not rows:
        return []
    ordered = sorted(rows, key=lambda row: str(row["example_id"]))
    rng.shuffle(ordered)
    if len(ordered) >= target:
        return ordered[:target]
    return [ordered[index % len(ordered)] for index in range(target)]


def balance_forecast_tools(
    rows: list[dict[str, Any]], target: int, min_source: int, seed: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Balance forecast classes without consulting validation or test labels."""

    if target <= 0:
        return rows, {"enabled": False}
    if min_source < 1:
        raise ValueError("--forecast-tool-balance-min-source must be positive")
    non_forecast = [row for row in rows if str(row["task"]) != "forecast"]
    forecast = [row for row in rows if str(row["task"]) == "forecast"]
    positives: dict[str, list[dict[str, Any]]] = {}
    negatives: dict[str, list[dict[str, Any]]] = {}
    for row in forecast:
        kind = str(row.get("semantic", {}).get("derived_forecast_kind", ""))
        tool_id = _forecast_tool_id(row)
        grouped = positives if kind == "imminent_2_8_sec" else negatives
        grouped.setdefault(tool_id, []).append(row)
    rng = random.Random(seed)
    balanced_positives: list[dict[str, Any]] = []
    under_supported: dict[str, int] = {}
    for tool_id, group in sorted(positives.items()):
        if len(group) < min_source:
            balanced_positives.extend(group)
            under_supported[tool_id] = len(group)
        else:
            balanced_positives.extend(_resample_group(group, target, rng))
    negative_target = len(balanced_positives)
    balanced_negatives: list[dict[str, Any]] = []
    negative_keys = sorted(key for key, group in negatives.items() if group)
    if negative_keys and negative_target:
        base, remainder = divmod(negative_target, len(negative_keys))
        for index, tool_id in enumerate(negative_keys):
            count = base + int(index < remainder)
            balanced_negatives.extend(_resample_group(negatives[tool_id], count, rng))
    balanced = [*non_forecast, *balanced_positives, *balanced_negatives]
    rng.shuffle(balanced)
    audit = {
        "enabled": True,
        "target_per_supported_positive_tool": target,
        "min_source_rows_for_expansion": min_source,
        "source_positive_counts": {key: len(value) for key, value in sorted(positives.items())},
        "source_negative_counts": {key: len(value) for key, value in sorted(negatives.items())},
        "under_supported_positive_tools_not_expanded": under_supported,
        "balanced_positive_count": len(balanced_positives),
        "balanced_negative_count": len(balanced_negatives),
        "balanced_forecast_count": len(balanced_positives) + len(balanced_negatives),
    }
    return balanced, audit


def run_command(command: list[str]) -> str:
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return completed.stdout.strip()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def task_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    return dict(sorted(Counter(str(row["task"]) for row in rows).items()))


def resolve_resume(output_dir: Path, value: str | None) -> Path | None:
    if value is None:
        return None
    if value != "auto":
        path = Path(value).expanduser().resolve()
    else:
        candidates: list[tuple[int, Path]] = []
        for path in (output_dir / "checkpoints").glob("checkpoint-*"):
            try:
                candidates.append((int(path.name.rsplit("-", 1)[1]), path))
            except (IndexError, ValueError):
                continue
        if not candidates:
            raise FileNotFoundError("no checkpoint-* directory to resume")
        path = max(candidates)[1]
    if not (path / "trainer_state.json").is_file():
        raise FileNotFoundError(f"invalid Trainer checkpoint: {path}")
    return path


def find_subsequence(haystack: list[int], needle: list[int]) -> int:
    if not needle:
        return -1
    first = needle[0]
    for index, value in enumerate(haystack):
        if value == first and haystack[index : index + len(needle)] == needle:
            return index
    return -1


@dataclass
class MaskingStats:
    input_tokens: int
    completion_tokens: int
    supervised_tokens: int
    ignored_tokens: int
    image_tokens: int


class RuntimeFieldMaskingCollator:
    """Collate one multimodal row and mask non-authoritative JSON fields."""

    def __init__(self, processor: Any, max_length: int) -> None:
        self.processor = processor
        self.tokenizer = processor.tokenizer
        self.max_length = max_length
        self.last_stats: MaskingStats | None = None

    def __call__(self, features: list[dict[str, Any]]) -> dict[str, Any]:
        import torch
        from PIL import Image

        if len(features) != 1:
            raise ValueError(
                "RuntimeFieldMaskingCollator requires batch size 1; use gradient accumulation"
            )
        row = features[0]
        completion = str(row["completion"])
        prompt = self.processor.apply_chat_template(
            row["prompt_messages"],
            tokenize=False,
            add_generation_prompt=True,
            enable_thinking=False,
        )
        end_marker = "<|im_end|>\n"
        full_text = prompt + completion + end_marker
        with Image.open(row["image_path"]) as source:
            image = source.convert("RGB")
            batch = self.processor(
                text=[full_text],
                images=[image],
                padding=False,
                truncation=True,
                max_length=self.max_length,
                return_tensors="pt",
            )

        input_ids = batch["input_ids"][0].tolist()
        completion_encoding = self.tokenizer(
            completion,
            add_special_tokens=False,
            return_offsets_mapping=True,
        )
        completion_ids = list(completion_encoding["input_ids"])
        offsets = list(completion_encoding["offset_mapping"])
        completion_start = find_subsequence(input_ids, completion_ids)
        if completion_start < 0:
            raise RuntimeError(
                f"{row['example_id']}: exact completion token sequence not found; "
                "the sample may have been truncated"
            )
        if completion_start + len(completion_ids) >= self.max_length:
            raise RuntimeError(f"{row['example_id']}: completion reaches max_length truncation")

        labels = torch.full_like(batch["input_ids"], -100)
        supervised_positions: set[int] = set()
        spans = [(int(start), int(end)) for start, end in row["supervision_char_spans"]]
        for local_index, (token_start, token_end) in enumerate(offsets):
            if token_end <= token_start:
                continue
            if any(token_end > span_start and token_start < span_end for span_start, span_end in spans):
                supervised_positions.add(completion_start + local_index)
        for position in supervised_positions:
            labels[0, position] = batch["input_ids"][0, position]

        # Always teach the assistant turn terminator after a valid object.
        end_ids = self.tokenizer(end_marker, add_special_tokens=False)["input_ids"]
        end_start = completion_start + len(completion_ids)
        if input_ids[end_start : end_start + len(end_ids)] == list(end_ids):
            for position in range(end_start, end_start + len(end_ids)):
                labels[0, position] = batch["input_ids"][0, position]

        if not supervised_positions:
            raise RuntimeError(f"{row['example_id']}: zero supervised field tokens")
        batch["labels"] = labels
        image_token_id = getattr(self.tokenizer, "image_token_id", None)
        if image_token_id is None:
            image_token_id = self.tokenizer.convert_tokens_to_ids("<|image_pad|>")
        self.last_stats = MaskingStats(
            input_tokens=len(input_ids),
            completion_tokens=len(completion_ids),
            supervised_tokens=int((labels != -100).sum().item()),
            ignored_tokens=int((labels == -100).sum().item()),
            image_tokens=sum(1 for token_id in input_ids if token_id == image_token_id),
        )
        return batch


def main() -> int:
    args = parse_args()
    args.dataset = args.dataset.resolve()
    args.output_dir = args.output_dir.resolve()
    if not args.dataset.is_file():
        raise FileNotFoundError(args.dataset)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    all_rows = read_jsonl(args.dataset)
    resolve_image_paths(all_rows, args.dataset.parent)
    validate_rows(all_rows)
    if args.smoke:
        if args.resume_from_checkpoint:
            raise ValueError("--smoke cannot resume")
        args.max_steps = 2
        args.max_examples = min(args.max_examples or 4, 4)
        args.max_eval_examples = 0
        args.save_steps = 2
        args.eval_steps = 2
        args.warmup_ratio = 0.0

    train_rows = select_rows(all_rows, args.split, args.max_examples, args.seed)
    train_tasks = {part.strip() for part in args.train_tasks.split(",") if part.strip()}
    if train_tasks:
        train_rows = [row for row in train_rows if str(row["task"]) in train_tasks]
    eval_rows = select_rows(
        all_rows,
        args.eval_split,
        args.max_eval_examples if args.max_eval_examples > 0 else 0,
        args.seed + 1,
    )
    if args.max_eval_examples == 0:
        eval_rows = []
    if not train_rows:
        raise ValueError(f"no training rows for split {args.split!r}")
    task_repeats = parse_task_repeats(args.task_repeat)
    unknown_repeat_tasks = sorted(
        set(task_repeats) - {str(row["task"]) for row in train_rows}
    )
    if unknown_repeat_tasks:
        raise ValueError(f"--task-repeat names absent training tasks: {unknown_repeat_tasks}")
    original_train_count = len(train_rows)
    train_rows, forecast_balance = balance_forecast_tools(
        train_rows,
        args.forecast_tool_balance_target,
        args.forecast_tool_balance_min_source,
        args.seed + 13,
    )
    balanced_train_count = len(train_rows)
    train_rows = apply_task_repeats(train_rows, task_repeats, args.seed + 17)
    resume_checkpoint = resolve_resume(args.output_dir, args.resume_from_checkpoint)

    # Unsloth must be imported before torch/transformers.
    import unsloth
    import torch
    import transformers
    import trl
    from transformers import AutoProcessor
    from trl import SFTConfig, SFTTrainer
    from unsloth import FastVisionModel

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is unavailable")
    if not torch.cuda.is_bf16_supported():
        raise RuntimeError("GPU does not support BF16")

    dataset_sha = sha256_file(args.dataset)
    model_path = Path(args.model)
    model_config_sha = sha256_file(model_path / "config.json") if (model_path / "config.json").is_file() else None
    manifest = {
        "schema": "taskplanner.qwen35_9b_runtime_lora_run.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "command": sys.argv,
        "arguments": vars(args) | {
            "dataset": str(args.dataset),
            "output_dir": str(args.output_dir),
            "resume_from_checkpoint": str(resume_checkpoint) if resume_checkpoint else None,
        },
        "dataset": {
            "path": str(args.dataset),
            "sha256": dataset_sha,
            "all_count": len(all_rows),
            "original_train_count": original_train_count,
            "balanced_train_count": balanced_train_count,
            "train_count": len(train_rows),
            "eval_count": len(eval_rows),
            "train_tasks": task_counts(train_rows),
            "eval_tasks": task_counts(eval_rows),
            "train_cases": sorted({str(row["case_id"]) for row in train_rows}),
            "eval_cases": sorted({str(row["case_id"]) for row in eval_rows}),
            "forecast_balance": forecast_balance,
        },
        "model": {"path_or_id": args.model, "config_sha256": model_config_sha},
        "host": {
            "python": sys.version,
            "platform": platform.platform(),
            "gpu": run_command([
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.used,memory.free,driver_version",
                "--format=csv,noheader",
            ]),
            "git_head": run_command(["git", "rev-parse", "HEAD"]),
            "git_status": run_command(["git", "status", "--short"]),
        },
        "libraries": {
            "torch": torch.__version__,
            "cuda": torch.version.cuda,
            "transformers": transformers.__version__,
            "trl": trl.__version__,
            "unsloth": getattr(unsloth, "__version__", "unknown"),
        },
    }
    write_json(args.output_dir / "run_manifest.json", manifest)

    torch.manual_seed(args.seed)
    torch.cuda.manual_seed_all(args.seed)
    torch.cuda.reset_peak_memory_stats()
    input_model_is_adapter = (Path(args.model) / "adapter_config.json").is_file()
    model, _ = FastVisionModel.from_pretrained(
        model_name=args.model,
        max_seq_length=args.max_length,
        dtype=torch.bfloat16,
        load_in_4bit=False,
        load_in_16bit=True,
        full_finetuning=False,
        use_gradient_checkpointing="unsloth",
        random_state=args.seed,
        fast_inference=False,
        local_files_only=True,
    )
    processor = AutoProcessor.from_pretrained(
        args.model,
        min_pixels=args.min_pixels,
        max_pixels=args.max_pixels,
        local_files_only=True,
    )
    if not input_model_is_adapter:
        model = FastVisionModel.get_peft_model(
            model,
            finetune_vision_layers=args.finetune_vision_layers,
            finetune_language_layers=args.finetune_language_layers,
            finetune_attention_modules=True,
            finetune_mlp_modules=True,
            r=args.lora_rank,
            lora_alpha=args.lora_alpha,
            lora_dropout=0,
            bias="none",
            random_state=args.seed,
            use_rslora=False,
            loftq_config=None,
            use_gradient_checkpointing="unsloth",
        )
    FastVisionModel.for_training(model)

    collator = RuntimeFieldMaskingCollator(processor, args.max_length)
    first_batch = collator([train_rows[0]])
    first_stats = collator.last_stats
    if first_stats is None or first_stats.supervised_tokens <= 0:
        raise RuntimeError("masking audit failed")
    masking_audit = vars(first_stats) | {
        "example_id": train_rows[0]["example_id"],
        "input_shape": list(first_batch["input_ids"].shape),
        "pixel_values_shape": list(first_batch["pixel_values"].shape)
        if "pixel_values" in first_batch
        else None,
    }
    write_json(args.output_dir / "first_batch_masking_audit.json", masking_audit)
    del first_batch
    torch.cuda.empty_cache()

    eval_enabled = bool(eval_rows) and not args.smoke
    trainer = SFTTrainer(
        model=model,
        processing_class=processor,
        data_collator=collator,
        train_dataset=train_rows,
        eval_dataset=eval_rows if eval_enabled else None,
        args=SFTConfig(
            output_dir=str(args.output_dir / "checkpoints"),
            per_device_train_batch_size=args.batch_size,
            per_device_eval_batch_size=args.eval_batch_size,
            gradient_accumulation_steps=args.gradient_accumulation_steps,
            num_train_epochs=args.epochs,
            max_steps=args.max_steps,
            learning_rate=args.learning_rate,
            warmup_ratio=args.warmup_ratio,
            weight_decay=args.weight_decay,
            optim="adamw_8bit",
            lr_scheduler_type="cosine",
            logging_steps=args.logging_steps,
            logging_first_step=True,
            eval_strategy="steps" if eval_enabled else "no",
            eval_steps=args.eval_steps if eval_enabled else None,
            save_strategy="steps",
            save_steps=args.save_steps,
            save_total_limit=args.save_total_limit,
            save_only_model=False,
            seed=args.seed,
            data_seed=args.seed,
            bf16=True,
            fp16=False,
            tf32=True,
            report_to="none",
            remove_unused_columns=False,
            dataset_text_field="",
            dataset_kwargs={"skip_prepare_dataset": True},
            dataset_num_proc=1,
            max_length=args.max_length,
            packing=False,
            completion_only_loss=False,
            gradient_checkpointing=True,
            dataloader_num_workers=0,
        ),
    )
    train_result = trainer.train(
        resume_from_checkpoint=str(resume_checkpoint) if resume_checkpoint else None
    )
    final_adapter = args.output_dir / "final_adapter"
    final_adapter.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(str(final_adapter), safe_serialization=True)
    processor.save_pretrained(str(final_adapter))
    trainer.save_state()

    summary = {
        "schema": "taskplanner.qwen35_9b_runtime_lora_training_summary.v1",
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "train_metrics": train_result.metrics,
        "train_count": len(train_rows),
        "original_train_count": original_train_count,
        "balanced_train_count": balanced_train_count,
        "forecast_balance": forecast_balance,
        "task_repeats": task_repeats,
        "eval_count": len(eval_rows),
        "peak_reserved_gib": round(torch.cuda.max_memory_reserved() / 1024**3, 3),
        "peak_allocated_gib": round(torch.cuda.max_memory_allocated() / 1024**3, 3),
        "adapter_dir": str(final_adapter),
        "adapter_files": {
            path.name: {"bytes": path.stat().st_size, "sha256": sha256_file(path)}
            for path in sorted(final_adapter.iterdir())
            if path.is_file()
        },
        "first_batch_masking": masking_audit,
    }
    write_json(args.output_dir / "training_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
