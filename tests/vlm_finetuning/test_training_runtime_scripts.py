from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

from tools.vlm_finetuning.run_qwen35_predictions import select_rows as select_eval_rows
from tools.vlm_finetuning.train_qwen35_4b_lora import (
    resolve_resume_checkpoint,
    select_rows as select_train_rows,
    validate_resume_manifest,
)


def _row(
    example_id: str,
    *,
    case_id: str,
    split: str,
    task: str,
    target: dict[str, object],
) -> dict[str, object]:
    return {
        "example_id": example_id,
        "case_id": case_id,
        "split": split,
        "task_type": task,
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "prompt"}]},
            {
                "role": "assistant",
                "content": [{"type": "text", "text": json.dumps(target)}],
            },
        ],
    }


def test_training_selector_accepts_nested_split_and_all() -> None:
    rows = [
        {**_row("a", case_id="c1", split="train", task="request_intent", target={}), "split": {"role": "train"}},
        _row("b", case_id="c2", split="validation", task="request_intent", target={}),
    ]
    assert [row["example_id"] for row in select_train_rows(rows, "train", None, 1)] == ["a"]
    assert [row["example_id"] for row in select_train_rows(rows, "all", None, 1)] == ["a", "b"]


def test_eval_selector_round_robins_cases_and_tool_targets() -> None:
    rows = [
        _row(
            f"{case}-{tool}",
            case_id=case,
            split="test",
            task="tool_presence_at_transfer",
            target={"tool": tool},
        )
        for case in ("c1", "c2")
        for tool in ("adson_forceps", "bovie")
    ]
    selected = select_eval_rows(
        rows,
        split="test",
        max_per_task=4,
        selection_manifest=None,
        limit=None,
    )
    assert {row["case_id"] for row in selected} == {"c1", "c2"}
    answers = {
        json.loads(row["messages"][-1]["content"][0]["text"])["tool"]
        for row in selected
    }
    assert answers == {"adson_forceps", "bovie"}


def test_resume_checkpoint_selects_latest_numeric_step(tmp_path: Path) -> None:
    for step in (2, 36, 9):
        checkpoint = tmp_path / "checkpoints" / f"checkpoint-{step}"
        checkpoint.mkdir(parents=True)
        (checkpoint / "trainer_state.json").write_text("{}", encoding="utf-8")
    assert resolve_resume_checkpoint(tmp_path, "auto") == (
        tmp_path / "checkpoints" / "checkpoint-36"
    ).resolve()


def test_resume_manifest_rejects_hyperparameter_drift(tmp_path: Path) -> None:
    stable = {
        "split": "train",
        "eval_split": "validation",
        "max_examples": None,
        "max_eval_examples": 48,
        "max_length": 2048,
        "epochs": 1.0,
        "max_steps": -1,
        "batch_size": 1,
        "eval_batch_size": 1,
        "gradient_accumulation_steps": 8,
        "learning_rate": 1.0e-4,
        "warmup_steps": 5,
        "weight_decay": 0.001,
        "lora_rank": 16,
        "lora_alpha": 16,
        "seed": 3407,
        "resize": "max",
        "train_on_responses_only": True,
        "finetune_vision_layers": True,
        "finetune_language_layers": True,
    }
    manifest = {
        "dataset": {"sha256": "dataset-sha"},
        "model": {"path_or_id": "base-model"},
        "arguments": stable,
    }
    (tmp_path / "run_manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    args = Namespace(**(stable | {"model": "base-model"}))
    validate_resume_manifest(
        tmp_path,
        args=args,
        dataset_sha256="dataset-sha",
    )

    args.learning_rate = 2.0e-4
    try:
        validate_resume_manifest(
            tmp_path,
            args=args,
            dataset_sha256="dataset-sha",
        )
    except ValueError as exc:
        assert "learning_rate" in str(exc)
    else:
        raise AssertionError("resume validation accepted changed learning rate")
