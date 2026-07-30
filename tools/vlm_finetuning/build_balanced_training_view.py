#!/usr/bin/env python3
"""Build the deterministic balanced-v1 training view.

The source dataset is never modified. Validation and test rows are copied
unchanged, while train rows are deterministically sampled with replacement to
the task- and class-conditional quotas defined below.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import random
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA = "taskplanner.vlm_balanced_training_view.v1"
AUDIT_SCHEMA = "taskplanner.vlm_balanced_training_view_audit.v1"
DEFAULT_SEED = 3407

TASK_QUOTAS: dict[str, int] = {
    "clinical_observation_interpretation": 99,
    "request_intent": 63,
    "current_phase": 117,
    "next_physical_tool": 118,
    "tool_presence_at_transfer": 117,
    "tool_presence_pseudo": 60,
}

STRATUM_QUOTAS: dict[str, dict[str, int]] = {
    "current_phase": {
        "interior:P03": 15,
        "interior:P04": 15,
        "interior:P05": 14,
        "interior:P06": 14,
        "transition:P04": 20,
        "transition:P05": 20,
        "transition:P06": 19,
    },
    "next_physical_tool": {
        "none": 59,
        "positive:adson_forceps": 12,
        "positive:army_navy_retractor": 9,
        "positive:bipolar_forceps": 11,
        "positive:bovie": 14,
        "positive:allis_forceps": 1,
        "positive:kocher_retractor": 1,
        "positive:mosquito_forceps": 4,
        "positive:yankauer_suction": 7,
    },
    "tool_presence_at_transfer": {
        "adson_forceps": 20,
        "army_navy_retractor": 20,
        "bipolar_forceps": 20,
        "bovie": 20,
        "yankauer_suction": 20,
        "mosquito_forceps": 13,
        "allis_forceps": 2,
        "kocher_retractor": 2,
    },
    "tool_presence_pseudo": {
        "adson_forceps": 10,
        "army_navy_retractor": 10,
        "bipolar_forceps": 10,
        "bovie": 10,
        "mosquito_forceps": 10,
        "yankauer_suction": 10,
    },
}

EXCLUDED_STRATA: dict[str, set[str]] = {
    "tool_presence_pseudo": {"allis_forceps", "kocher_retractor"},
}

TASK_ORDER = {task: index for index, task in enumerate(TASK_QUOTAS)}


class BalanceError(RuntimeError):
    """Raised when the source dataset cannot satisfy the balanced-v1 contract."""


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
                raise BalanceError(
                    f"{path}:{line_number}: invalid JSON: {exc}"
                ) from exc
            if not isinstance(row, dict):
                raise BalanceError(f"{path}:{line_number}: row is not an object")
            rows.append(row)
    return rows


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(
                json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                + "\n"
            )


def split_role(row: Mapping[str, Any]) -> str:
    value = row.get("split", "train")
    if isinstance(value, Mapping):
        value = value.get("role", "train")
    return str(value)


def assistant_target(row: Mapping[str, Any]) -> dict[str, Any]:
    messages = row.get("messages")
    if not isinstance(messages, list) or not messages:
        raise BalanceError(f"{row.get('example_id')}: missing messages")
    assistant = messages[-1]
    if not isinstance(assistant, Mapping) or assistant.get("role") != "assistant":
        raise BalanceError(f"{row.get('example_id')}: final message is not assistant")
    content = assistant.get("content")
    if not isinstance(content, list) or not content:
        raise BalanceError(f"{row.get('example_id')}: assistant content is empty")
    item = content[0]
    if not isinstance(item, Mapping) or not isinstance(item.get("text"), str):
        raise BalanceError(f"{row.get('example_id')}: assistant target text is missing")
    try:
        target = json.loads(item["text"])
    except json.JSONDecodeError as exc:
        raise BalanceError(
            f"{row.get('example_id')}: assistant target is invalid JSON"
        ) from exc
    if not isinstance(target, dict):
        raise BalanceError(f"{row.get('example_id')}: assistant target is not an object")
    return target


def row_stratum(row: Mapping[str, Any]) -> str:
    task = str(row.get("task_type", ""))
    if task in {"clinical_observation_interpretation", "request_intent"}:
        return "all"
    target = assistant_target(row)
    if task == "current_phase":
        return f"{target.get('state')}:{target.get('phase_id')}"
    if task == "next_physical_tool":
        tool = str(target.get("next_transfer_tool"))
        return "none" if tool == "none" else f"positive:{tool}"
    if task in {"tool_presence_at_transfer", "tool_presence_pseudo"}:
        return str(target.get("tool"))
    raise BalanceError(f"{row.get('example_id')}: unsupported task_type {task!r}")


def _stable_seed(seed: int, task: str, stratum: str, cycle: int) -> int:
    material = f"{seed}\0{task}\0{stratum}\0{cycle}".encode()
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big")


def _sample_stratum(
    rows: Sequence[dict[str, Any]],
    *,
    quota: int,
    seed: int,
    task: str,
    stratum: str,
) -> list[dict[str, Any]]:
    if quota < 0:
        raise BalanceError(f"{task}/{stratum}: negative quota")
    if quota and not rows:
        raise BalanceError(f"{task}/{stratum}: no source rows for quota {quota}")
    ordered = sorted(rows, key=lambda row: str(row.get("example_id", "")))
    selected: list[dict[str, Any]] = []
    cycle = 0
    while len(selected) < quota:
        shuffled = list(ordered)
        random.Random(_stable_seed(seed, task, stratum, cycle)).shuffle(shuffled)
        selected.extend(shuffled[: quota - len(selected)])
        cycle += 1
    return selected


def _task_plan(task: str) -> dict[str, int]:
    if task in STRATUM_QUOTAS:
        plan = STRATUM_QUOTAS[task]
    else:
        plan = {"all": TASK_QUOTAS[task]}
    if sum(plan.values()) != TASK_QUOTAS[task]:
        raise BalanceError(
            f"{task}: stratum quota sum {sum(plan.values())} "
            f"!= task quota {TASK_QUOTAS[task]}"
        )
    return plan


def select_balanced_train_rows(
    rows: Sequence[dict[str, Any]],
    *,
    seed: int = DEFAULT_SEED,
) -> list[dict[str, Any]]:
    source_train = [row for row in rows if split_role(row) == "train"]
    unexpected_tasks = sorted(
        {str(row.get("task_type", "")) for row in source_train} - TASK_QUOTAS.keys()
    )
    if unexpected_tasks:
        raise BalanceError(f"unsupported train tasks: {unexpected_tasks}")

    by_task_stratum: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in source_train:
        task = str(row.get("task_type", ""))
        by_task_stratum[(task, row_stratum(row))].append(row)

    selected_with_strata: list[tuple[str, str, dict[str, Any]]] = []
    for task in TASK_QUOTAS:
        plan = _task_plan(task)
        known_strata = {
            stratum
            for candidate_task, stratum in by_task_stratum
            if candidate_task == task
        }
        unexpected_strata = sorted(
            known_strata - plan.keys() - EXCLUDED_STRATA.get(task, set())
        )
        if unexpected_strata:
            raise BalanceError(f"{task}: unsupported strata {unexpected_strata}")
        for stratum, quota in plan.items():
            sampled = _sample_stratum(
                by_task_stratum.get((task, stratum), []),
                quota=quota,
                seed=seed,
                task=task,
                stratum=stratum,
            )
            selected_with_strata.extend((task, stratum, row) for row in sampled)

    selected_with_strata.sort(
        key=lambda item: (
            TASK_ORDER[item[0]],
            item[1],
            str(item[2].get("example_id", "")),
        )
    )
    replica_counts: Counter[str] = Counter()
    balanced: list[dict[str, Any]] = []
    for task, stratum, row in selected_with_strata:
        source_example_id = str(row.get("example_id", ""))
        if not source_example_id:
            raise BalanceError("source train row has an empty example_id")
        replica_counts[source_example_id] += 1
        replica_index = replica_counts[source_example_id]
        clone = copy.deepcopy(row)
        clone["source_example_id"] = source_example_id
        clone["example_id"] = f"{source_example_id}::bal{replica_index:02d}"
        clone["balanced_view"] = {
            "schema": SCHEMA,
            "seed": seed,
            "stratum": stratum,
            "replica_index": replica_index,
        }
        if clone.get("messages") != row.get("messages"):
            raise AssertionError("balanced sampling changed messages")
        balanced.append(clone)
    return balanced


def _counter(values: Iterable[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def _canonical_rows(rows: Iterable[Mapping[str, Any]]) -> list[str]:
    return [
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for row in rows
    ]


def build_balanced_training_view(
    *,
    input_path: Path,
    output_dir: Path,
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    input_path = input_path.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    if not input_path.is_file():
        raise FileNotFoundError(input_path)

    source_rows = read_jsonl(input_path)
    source_ids = [str(row.get("example_id", "")) for row in source_rows]
    if "" in source_ids or len(source_ids) != len(set(source_ids)):
        raise BalanceError("source example_id values must be non-empty and unique")

    balanced_train = select_balanced_train_rows(source_rows, seed=seed)
    unchanged_nontrain = [
        copy.deepcopy(row) for row in source_rows if split_role(row) != "train"
    ]
    combined = balanced_train + unchanged_nontrain
    combined_ids = [str(row.get("example_id", "")) for row in combined]

    source_validation = [
        row for row in source_rows if split_role(row) == "validation"
    ]
    source_test = [row for row in source_rows if split_role(row) == "test"]
    output_validation = [row for row in combined if split_role(row) == "validation"]
    output_test = [row for row in combined if split_role(row) == "test"]
    source_by_id = {str(row["example_id"]): row for row in source_rows}
    messages_unchanged = all(
        row.get("messages")
        == source_by_id[str(row["source_example_id"])].get("messages")
        for row in balanced_train
    )

    observed_task_counts = _counter(
        str(row.get("task_type", "")) for row in balanced_train
    )
    observed_stratum_counts: dict[str, dict[str, int]] = {}
    for task in TASK_QUOTAS:
        observed_stratum_counts[task] = _counter(
            row_stratum(row)
            for row in balanced_train
            if row.get("task_type") == task
        )
    expected_stratum_counts = {
        task: dict(sorted(_task_plan(task).items())) for task in TASK_QUOTAS
    }

    checks = {
        "combined_example_id_unique": len(combined_ids) == len(set(combined_ids)),
        "messages_unchanged": messages_unchanged,
        "source_example_ids_resolve": all(
            str(row.get("source_example_id")) in source_by_id
            for row in balanced_train
        ),
        "task_quotas_exact": observed_task_counts == dict(sorted(TASK_QUOTAS.items())),
        "stratum_quotas_exact": observed_stratum_counts == expected_stratum_counts,
        "train_count_exact": len(balanced_train) == sum(TASK_QUOTAS.values()),
        "validation_unchanged": (
            _canonical_rows(output_validation) == _canonical_rows(source_validation)
        ),
        "test_unchanged": _canonical_rows(output_test) == _canonical_rows(source_test),
    }
    if not all(checks.values()):
        failed = sorted(name for name, passed in checks.items() if not passed)
        raise BalanceError(f"balanced view validation failed: {failed}")

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "unsloth_messages.jsonl"
    write_jsonl(output_path, combined)
    audit = {
        "schema": AUDIT_SCHEMA,
        "ok": True,
        "seed": seed,
        "source": {
            "path": str(input_path),
            "sha256": sha256_file(input_path),
            "rows": len(source_rows),
            "split_counts": _counter(split_role(row) for row in source_rows),
        },
        "output": {
            "path": str(output_path),
            "sha256": sha256_file(output_path),
            "rows": len(combined),
            "split_counts": _counter(split_role(row) for row in combined),
        },
        "plan": {
            "task_quotas": dict(sorted(TASK_QUOTAS.items())),
            "stratum_quotas": expected_stratum_counts,
            "sampling": (
                "sorted source IDs; deterministic per-stratum shuffled cycles "
                "with replacement; unique ::balNN IDs"
            ),
        },
        "observed": {
            "train_task_counts": observed_task_counts,
            "train_stratum_counts": observed_stratum_counts,
            "train_instances": len(balanced_train),
            "unique_train_sources": len(
                {str(row["source_example_id"]) for row in balanced_train}
            ),
            "replicated_train_instances": sum(
                1
                for row in balanced_train
                if int(row["balanced_view"]["replica_index"]) > 1
            ),
        },
        "checks": checks,
    }
    audit_path = output_dir / "audit.json"
    audit_path.write_text(
        json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return audit


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    audit = build_balanced_training_view(
        input_path=args.input,
        output_dir=args.output_dir,
        seed=args.seed,
    )
    print(json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
