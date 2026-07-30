#!/usr/bin/env python3
"""Train a BF16 LoRA adapter for Qwen3.5-4B on causal surgical VLM rows.

The input JSONL is expected to contain Unsloth/OpenAI-style ``messages`` rows
produced by ``build_causal_sft_dataset.py``. Original annotations and media are
never modified.
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


DEFAULT_MODEL = (
    "/home/arl/.cache/huggingface/hub/models--unsloth--Qwen3.5-4B/"
    "snapshots/3764fa359b9082ea5a1e4a5e3ac3aaf6e9671636"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--split", default="train")
    parser.add_argument("--eval-split", default="validation")
    parser.add_argument("--max-examples", type=int)
    parser.add_argument("--max-eval-examples", type=int, default=128)
    parser.add_argument("--max-length", type=int, default=2048)
    parser.add_argument("--epochs", type=float, default=1.0)
    parser.add_argument("--max-steps", type=int, default=-1)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--eval-batch-size", type=int, default=1)
    parser.add_argument("--gradient-accumulation-steps", type=int, default=8)
    parser.add_argument("--learning-rate", type=float, default=1.0e-4)
    parser.add_argument(
        "--warmup-steps",
        type=int,
        default=5,
        help="Integer optimizer warmup steps; matches the official Unsloth notebook.",
    )
    parser.add_argument("--weight-decay", type=float, default=0.001)
    parser.add_argument("--lora-rank", type=int, default=16)
    parser.add_argument("--lora-alpha", type=int, default=16)
    parser.add_argument("--logging-steps", type=int, default=1)
    parser.add_argument("--eval-steps", type=int, default=50)
    parser.add_argument("--save-steps", type=int, default=50)
    parser.add_argument("--save-total-limit", type=int, default=2)
    parser.add_argument("--seed", type=int, default=3407)
    parser.add_argument(
        "--resume-from-checkpoint",
        nargs="?",
        const="auto",
        help=(
            "Resume optimizer/scheduler/RNG state from a Trainer checkpoint. "
            "Pass without a value to select the latest checkpoint in output-dir."
        ),
    )
    parser.add_argument(
        "--resize",
        default="max",
        choices=("min", "max"),
        help="Unsloth collator resize policy. 'max' preserves the 640x360 proxy.",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Run one optimizer step on at most two examples.",
    )
    parser.add_argument(
        "--train-on-responses-only",
        action=argparse.BooleanOptionalAction,
        default=True,
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
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
            if not isinstance(row, dict) or not isinstance(row.get("messages"), list):
                raise ValueError(f"{path}:{line_number}: missing messages array")
            rows.append(row)
    return rows


def image_paths(messages: Iterable[dict[str, Any]]) -> Iterable[Path]:
    for message in messages:
        content = message.get("content", [])
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "image":
                continue
            value = item.get("image")
            if isinstance(value, str) and not value.startswith(("http://", "https://")):
                yield Path(value)


def validate_rows(rows: list[dict[str, Any]]) -> None:
    seen: set[str] = set()
    missing_images: list[str] = []
    for index, row in enumerate(rows):
        example_id = str(row.get("example_id", f"row-{index}"))
        if example_id in seen:
            raise ValueError(f"duplicate example_id: {example_id}")
        seen.add(example_id)
        roles = [message.get("role") for message in row["messages"]]
        if roles[-2:] != ["user", "assistant"]:
            raise ValueError(f"{example_id}: final roles must be user, assistant")
        for path in image_paths(row["messages"]):
            if not path.is_file():
                missing_images.append(f"{example_id}:{path}")
    if missing_images:
        sample = "\n".join(missing_images[:20])
        raise FileNotFoundError(
            f"{len(missing_images)} referenced images do not exist; first entries:\n{sample}"
        )


def select_rows(
    rows: list[dict[str, Any]],
    split: str,
    limit: int | None,
    seed: int,
) -> list[dict[str, Any]]:
    def split_role(row: dict[str, Any]) -> str:
        value = row.get("split", "train")
        if isinstance(value, dict):
            value = value.get("role", "train")
        return str(value)

    wanted = {part.strip() for part in split.split(",") if part.strip()}
    selected = [
        row
        for row in rows
        if "all" in wanted or split_role(row) in wanted
    ]
    selected.sort(key=lambda row: str(row.get("example_id", "")))
    if limit is not None and len(selected) > limit:
        random.Random(seed).shuffle(selected)
        selected = selected[:limit]
        selected.sort(key=lambda row: str(row.get("example_id", "")))
    return selected


def compact_messages(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"messages": row["messages"]} for row in rows]


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


def task_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    return dict(sorted(Counter(str(row.get("task_type", "unknown")) for row in rows).items()))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def resolve_resume_checkpoint(output_dir: Path, value: str | None) -> Path | None:
    if value is None:
        return None
    if value == "auto":
        checkpoints_dir = output_dir / "checkpoints"
        candidates: list[tuple[int, Path]] = []
        for path in checkpoints_dir.glob("checkpoint-*"):
            try:
                step = int(path.name.rsplit("-", 1)[1])
            except (IndexError, ValueError):
                continue
            if path.is_dir():
                candidates.append((step, path))
        if not candidates:
            raise FileNotFoundError(
                f"no checkpoint-* directories found under {checkpoints_dir}"
            )
        checkpoint = max(candidates)[1]
    else:
        checkpoint = Path(value).expanduser()
        if not checkpoint.is_absolute():
            checkpoint = (Path.cwd() / checkpoint).resolve()
    if not (checkpoint / "trainer_state.json").is_file():
        raise FileNotFoundError(
            f"checkpoint lacks trainer_state.json: {checkpoint}"
        )
    return checkpoint.resolve()


def validate_resume_manifest(
    output_dir: Path,
    *,
    args: argparse.Namespace,
    dataset_sha256: str,
) -> None:
    manifest_path = output_dir / "run_manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(
            f"cannot validate resume without prior run manifest: {manifest_path}"
        )
    previous = json.loads(manifest_path.read_text(encoding="utf-8"))
    if previous.get("dataset", {}).get("sha256") != dataset_sha256:
        raise ValueError("resume refused: dataset SHA-256 differs from prior run")
    if previous.get("model", {}).get("path_or_id") != args.model:
        raise ValueError("resume refused: base model differs from prior run")

    previous_args = previous.get("arguments", {})
    stable_keys = (
        "split",
        "eval_split",
        "max_examples",
        "max_eval_examples",
        "max_length",
        "epochs",
        "max_steps",
        "batch_size",
        "eval_batch_size",
        "gradient_accumulation_steps",
        "learning_rate",
        "warmup_steps",
        "weight_decay",
        "lora_rank",
        "lora_alpha",
        "seed",
        "resize",
        "train_on_responses_only",
        "finetune_vision_layers",
        "finetune_language_layers",
    )
    mismatches = [
        key
        for key in stable_keys
        if previous_args.get(key) != getattr(args, key)
    ]
    if mismatches:
        raise ValueError(
            "resume refused: hyperparameters differ for "
            + ", ".join(mismatches)
        )


def main() -> int:
    args = parse_args()
    if not args.dataset.is_file():
        raise FileNotFoundError(args.dataset)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    all_rows = read_jsonl(args.dataset)
    validate_rows(all_rows)

    if args.smoke:
        if args.resume_from_checkpoint is not None:
            raise ValueError("--smoke cannot be combined with resume")
        args.max_steps = 2
        args.max_examples = min(args.max_examples or 2, 2)
        args.max_eval_examples = 0
        args.save_steps = 2
        args.eval_steps = 2
        args.warmup_steps = 0

    train_rows = select_rows(
        all_rows,
        split=args.split,
        limit=args.max_examples,
        seed=args.seed,
    )
    eval_rows = select_rows(
        all_rows,
        split=args.eval_split,
        limit=args.max_eval_examples if args.max_eval_examples > 0 else 0,
        seed=args.seed + 1,
    )
    if not train_rows:
        raise ValueError(f"no rows selected for split={args.split!r}")
    if args.max_eval_examples == 0:
        eval_rows = []

    dataset_sha256 = sha256_file(args.dataset)
    resume_checkpoint = resolve_resume_checkpoint(
        args.output_dir,
        args.resume_from_checkpoint,
    )
    if resume_checkpoint is not None:
        validate_resume_manifest(
            args.output_dir,
            args=args,
            dataset_sha256=dataset_sha256,
        )

    # Unsloth must be imported before torch and transformers.
    import unsloth  # noqa: F401
    import torch
    import transformers
    import trl
    from trl import SFTConfig, SFTTrainer
    from unsloth import FastVisionModel
    from unsloth.trainer import UnslothVisionDataCollator

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is unavailable; refusing to start GPU LoRA training")
    if not torch.cuda.is_bf16_supported():
        raise RuntimeError("CUDA device does not report BF16 support")

    model_path = Path(args.model)
    model_sha = None
    if model_path.is_dir():
        config_path = model_path / "config.json"
        if config_path.is_file():
            model_sha = sha256_file(config_path)

    run_manifest = {
        "schema": "taskplanner.qwen35_lora_run.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "command": sys.argv,
        "arguments": vars(args) | {
            "dataset": str(args.dataset.resolve()),
            "output_dir": str(args.output_dir.resolve()),
        },
        "dataset": {
            "path": str(args.dataset.resolve()),
            "sha256": dataset_sha256,
            "all_count": len(all_rows),
            "train_count": len(train_rows),
            "eval_count": len(eval_rows),
            "train_tasks": task_counts(train_rows),
            "eval_tasks": task_counts(eval_rows),
        },
        "model": {"path_or_id": args.model, "config_sha256": model_sha},
        "resume_from_checkpoint": (
            str(resume_checkpoint) if resume_checkpoint is not None else None
        ),
        "host": {
            "python": sys.version,
            "platform": platform.platform(),
            "nvidia_smi": run_command(
                [
                    "nvidia-smi",
                    "--query-gpu=name,memory.total,memory.used,memory.free,driver_version",
                    "--format=csv,noheader",
                ]
            ),
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
    write_json(args.output_dir / "run_manifest.json", run_manifest)

    torch.manual_seed(args.seed)
    torch.cuda.manual_seed_all(args.seed)
    torch.cuda.reset_peak_memory_stats()

    model, processor = FastVisionModel.from_pretrained(
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

    collator_kwargs: dict[str, Any] = {
        "model": model,
        "processor": processor,
        "max_seq_length": args.max_length,
        "resize": args.resize,
        "completion_only_loss": True,
    }
    if args.train_on_responses_only:
        collator_kwargs.update(
            {
                "train_on_responses_only": True,
                "instruction_part": "<|im_start|>user\n",
                "response_part": "<|im_start|>assistant\n",
                "force_match": True,
            }
        )
    data_collator = UnslothVisionDataCollator(**collator_kwargs)

    eval_enabled = bool(eval_rows) and not args.smoke
    trainer = SFTTrainer(
        model=model,
        processing_class=processor,
        data_collator=data_collator,
        train_dataset=compact_messages(train_rows),
        eval_dataset=compact_messages(eval_rows) if eval_enabled else None,
        args=SFTConfig(
            output_dir=str(args.output_dir / "checkpoints"),
            per_device_train_batch_size=args.batch_size,
            per_device_eval_batch_size=args.eval_batch_size,
            gradient_accumulation_steps=args.gradient_accumulation_steps,
            num_train_epochs=args.epochs,
            max_steps=args.max_steps,
            learning_rate=args.learning_rate,
            warmup_steps=args.warmup_steps,
            weight_decay=args.weight_decay,
            optim="adamw_8bit",
            lr_scheduler_type="linear",
            logging_steps=args.logging_steps,
            logging_first_step=True,
            eval_strategy="steps" if eval_enabled else "no",
            eval_steps=args.eval_steps if eval_enabled else None,
            save_strategy="steps",
            save_steps=args.save_steps,
            save_total_limit=args.save_total_limit,
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
            completion_only_loss=True,
            gradient_checkpointing=True,
            dataloader_num_workers=0,
        ),
    )

    # Verify masking before committing to a long run.
    first_batch = data_collator([compact_messages(train_rows[:1])[0]])
    labels = first_batch["labels"]
    supervised_tokens = int((labels != -100).sum().item())
    ignored_tokens = int((labels == -100).sum().item())
    if supervised_tokens <= 0:
        raise RuntimeError("first collated batch has zero supervised assistant tokens")
    masking_audit = {
        "supervised_tokens": supervised_tokens,
        "ignored_tokens": ignored_tokens,
        "input_shape": list(first_batch["input_ids"].shape),
        "pixel_values_shape": list(first_batch["pixel_values"].shape)
        if "pixel_values" in first_batch
        else None,
    }
    write_json(args.output_dir / "first_batch_masking_audit.json", masking_audit)
    del first_batch, labels
    torch.cuda.empty_cache()

    train_result = trainer.train(
        resume_from_checkpoint=(
            str(resume_checkpoint) if resume_checkpoint is not None else None
        )
    )
    final_adapter = args.output_dir / "final_adapter"
    final_adapter.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(str(final_adapter))
    processor.save_pretrained(str(final_adapter))
    trainer.save_state()

    summary = {
        "schema": "taskplanner.qwen35_lora_training_summary.v1",
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "train_metrics": train_result.metrics,
        "train_count": len(train_rows),
        "eval_count": len(eval_rows),
        "resumed_from_checkpoint": (
            str(resume_checkpoint) if resume_checkpoint is not None else None
        ),
        "peak_reserved_gib": round(
            torch.cuda.max_memory_reserved() / 1024**3,
            3,
        ),
        "peak_allocated_gib": round(
            torch.cuda.max_memory_allocated() / 1024**3,
            3,
        ),
        "adapter_dir": str(final_adapter.resolve()),
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
