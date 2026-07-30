from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from tools.vlm_finetuning.build_balanced_training_view import (
    STRATUM_QUOTAS,
    TASK_QUOTAS,
    build_balanced_training_view,
    read_jsonl,
    row_stratum,
)


def _row(
    example_id: str,
    *,
    split: str,
    task: str,
    target: dict[str, object],
) -> dict[str, object]:
    return {
        "example_id": example_id,
        "case_id": "fixture",
        "split": split,
        "task_type": task,
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "prompt"}]},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            target,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    }
                ],
            },
        ],
    }


def _fixture_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = [
        _row(
            "clinical-1",
            split="train",
            task="clinical_observation_interpretation",
            target={"observation": "o", "interpretation": "i"},
        ),
        _row(
            "intent-1",
            split="train",
            task="request_intent",
            target={"intent": "receive_unspecified_tool"},
        ),
    ]
    for stratum in STRATUM_QUOTAS["current_phase"]:
        state, phase = stratum.split(":")
        rows.append(
            _row(
                f"phase-{state}-{phase}",
                split="train",
                task="current_phase",
                target={"state": state, "phase_id": phase},
            )
        )
    for stratum in STRATUM_QUOTAS["next_physical_tool"]:
        tool = "none" if stratum == "none" else stratum.split(":", 1)[1]
        rows.append(
            _row(
                f"next-{tool}",
                split="train",
                task="next_physical_tool",
                target={"next_transfer_tool": tool},
            )
        )
    for task in ("tool_presence_at_transfer", "tool_presence_pseudo"):
        for tool in STRATUM_QUOTAS[task]:
            rows.append(
                _row(
                    f"{task}-{tool}",
                    split="train",
                    task=task,
                    target={"tool": tool},
                )
            )
    rows.append(
        _row(
            "tool_presence_pseudo-excluded-allis",
            split="train",
            task="tool_presence_pseudo",
            target={"tool": "allis_forceps"},
        )
    )
    rows.extend(
        [
            _row(
                "validation-1",
                split="validation",
                task="current_phase",
                target={"state": "transition", "phase_id": "P04"},
            ),
            _row(
                "test-1",
                split="test",
                task="tool_presence_at_transfer",
                target={"tool": "bovie"},
            ),
        ]
    )
    return rows


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )


def test_balanced_view_exact_quotas_and_preserves_nontrain(tmp_path: Path) -> None:
    source = tmp_path / "source.jsonl"
    source_rows = _fixture_rows()
    _write_jsonl(source, source_rows)
    output_dir = tmp_path / "balanced"

    audit = build_balanced_training_view(
        input_path=source,
        output_dir=output_dir,
        seed=3407,
    )
    output_rows = read_jsonl(output_dir / "unsloth_messages.jsonl")
    train = [row for row in output_rows if row["split"] == "train"]
    nontrain = [row for row in output_rows if row["split"] != "train"]

    assert audit["ok"]
    assert len(train) == 574
    assert Counter(row["task_type"] for row in train) == Counter(TASK_QUOTAS)
    for task, quotas in STRATUM_QUOTAS.items():
        assert Counter(
            row_stratum(row) for row in train if row["task_type"] == task
        ) == Counter(quotas)
    assert nontrain == [row for row in source_rows if row["split"] != "train"]


def test_balanced_view_ids_and_messages_preserve_source(tmp_path: Path) -> None:
    source = tmp_path / "source.jsonl"
    source_rows = _fixture_rows()
    _write_jsonl(source, source_rows)
    output_dir = tmp_path / "balanced"
    build_balanced_training_view(
        input_path=source,
        output_dir=output_dir,
        seed=3407,
    )
    output_rows = read_jsonl(output_dir / "unsloth_messages.jsonl")
    source_by_id = {row["example_id"]: row for row in source_rows}
    train = [row for row in output_rows if row["split"] == "train"]

    assert len({row["example_id"] for row in output_rows}) == len(output_rows)
    for row in train:
        assert row["example_id"].startswith(f"{row['source_example_id']}::bal")
        assert row["source_example_id"] in source_by_id
        assert row["messages"] == source_by_id[row["source_example_id"]]["messages"]


def test_balanced_view_is_byte_deterministic(tmp_path: Path) -> None:
    source = tmp_path / "source.jsonl"
    _write_jsonl(source, _fixture_rows())
    first = tmp_path / "first"
    second = tmp_path / "second"
    first_audit = build_balanced_training_view(
        input_path=source,
        output_dir=first,
        seed=3407,
    )
    second_audit = build_balanced_training_view(
        input_path=source,
        output_dir=second,
        seed=3407,
    )

    assert (
        (first / "unsloth_messages.jsonl").read_bytes()
        == (second / "unsloth_messages.jsonl").read_bytes()
    )
    assert first_audit["output"]["sha256"] == second_audit["output"]["sha256"]
