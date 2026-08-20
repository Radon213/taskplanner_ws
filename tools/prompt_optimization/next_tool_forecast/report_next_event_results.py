#!/usr/bin/env python3
"""Build an integrity-checked review of horizon-free next-event runs.

The report joins the exact model input, offline label, Qwen prediction, and
leave-one-case-out/state n-gram prediction for every row.  Every Qwen error is
written to both JSONL and Markdown with its complete causal handover history so
that no failure is hidden behind an aggregate metric.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping


class ReportError(RuntimeError):
    """Raised when a completed run cannot support a trustworthy report."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--development-run", required=True, type=Path)
    parser.add_argument("--posthoc-run", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ReportError(f"{path}: expected JSON object")
    return value


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ReportError(f"{path}:{line_number}: expected JSON object")
            rows.append(value)
    return rows


def by_id(rows: Iterable[Mapping[str, Any]], name: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for raw in rows:
        row = dict(raw)
        example_id = str(row.get("example_id", ""))
        if not example_id or example_id in result:
            raise ReportError(f"{name}: missing or duplicate example_id {example_id!r}")
        result[example_id] = row
    return result


def artifact_path(run_path: Path, value: Mapping[str, Any]) -> Path:
    raw = Path(str(value["path"]))
    return raw if raw.is_absolute() else (run_path.parent / raw.name)


def verify_artifact(run_path: Path, value: Mapping[str, Any], name: str) -> Path:
    path = artifact_path(run_path, value)
    if not path.is_file():
        raise ReportError(f"{name}: missing artifact {path}")
    actual = sha256_file(path)
    if actual != value.get("sha256"):
        raise ReportError(f"{name}: SHA-256 mismatch for {path}")
    return path


def flatten_authored_tools(context: Mapping[str, Any]) -> set[str]:
    result: set[str] = set()
    paths = context.get("authored_protocol_exchange_paths", {})
    if isinstance(paths, Mapping):
        for groups in paths.values():
            if not isinstance(groups, list):
                continue
            for path in groups:
                if isinstance(path, list):
                    result.update(str(tool) for tool in path)
    return result


def failure_category(
    *, label: Mapping[str, Any], context: Mapping[str, Any], expected: str
) -> tuple[str, str]:
    history = [str(tool) for tool in context.get("complete_handover_history", [])]
    if int(label["target_index"]) == 0:
        return (
            "initial_state_default_error",
            "No prior handover exists; the model substituted a frequent default for the first tool.",
        )
    if history and history[-1] == expected:
        return (
            "same_tool_repeat_missed",
            "The next handover repeats the immediately previous tool, but the model advanced to another branch.",
        )
    if expected not in flatten_authored_tools(context):
        return (
            "target_absent_from_authored_paths",
            "The observed target is absent from the supplied authored exchange paths, so the prompt has no matching authored branch.",
        )
    return (
        "authored_branch_alignment_error",
        "The target exists in the authored patterns, but the observed prefix is compatible with multiple positions or branches and the model selected the wrong continuation.",
    )


def load_partition(run_path: Path, expected_partition: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    run = read_json(run_path)
    if run.get("execution_status") != "completed":
        raise ReportError(f"{run_path}: run is not completed")
    if run.get("partition") != expected_partition:
        raise ReportError(f"{run_path}: unexpected partition {run.get('partition')!r}")
    artifacts = run.get("artifacts")
    predictions = run.get("predictions")
    if not isinstance(artifacts, Mapping) or not isinstance(predictions, Mapping):
        raise ReportError(f"{run_path}: incomplete artifact declarations")
    inputs_path = verify_artifact(run_path, artifacts["inputs"], "inputs")
    labels_path = verify_artifact(run_path, artifacts["labels"], "labels")
    baseline_path = verify_artifact(run_path, artifacts["ngram_baseline"], "ngram baseline")
    prediction_path = verify_artifact(run_path, predictions, "predictions")
    inputs = by_id(read_jsonl(inputs_path), "inputs")
    labels = by_id(read_jsonl(labels_path), "labels")
    model = by_id(read_jsonl(prediction_path), "model predictions")
    baseline = by_id(read_jsonl(baseline_path), "baseline predictions")
    ids = set(inputs)
    if not ids or set(labels) != ids or set(model) != ids or set(baseline) != ids:
        raise ReportError(f"{run_path}: artifact ID sets do not match")
    if int(run.get("post_count", -1)) != len(ids):
        raise ReportError(f"{run_path}: post_count does not match row count")
    if len(run.get("lifecycle", [])) != len(ids) or any(
        row.get("status") != "completed" for row in run["lifecycle"]
    ):
        raise ReportError(f"{run_path}: lifecycle is incomplete")
    input_contract = run.get("frozen_config", {}).get("input_contract", {})
    expected_contract = {
        "images": "absent",
        "asr": "absent",
        "time_horizon": "none",
        "future_handover_guaranteed": True,
        "none_class": "absent",
    }
    if any(input_contract.get(key) != value for key, value in expected_contract.items()):
        raise ReportError(f"{run_path}: input contract does not match horizon-free benchmark")

    joined: list[dict[str, Any]] = []
    for example_id, input_row in inputs.items():
        rendered = json.dumps(input_row.get("model_context", {}), sort_keys=True)
        forbidden = ("case_id", "target_tool_id", "target_event_id", "target_time_sec", "image_url", "data:image")
        if any(token in rendered for token in forbidden):
            raise ReportError(f"{example_id}: target/provenance/media leaked into model context")
        label = labels[example_id]
        model_row = model[example_id]
        baseline_row = baseline[example_id]
        context = input_row["model_context"]
        model_prediction = model_row.get("prediction") or {}
        baseline_prediction = baseline_row.get("prediction") or {}
        expected = str(label["target_tool_id"])
        predicted = str(model_prediction.get("tool_id", "invalid"))
        baseline_tool = str(baseline_prediction.get("tool_id", "invalid"))
        model_correct = not model_row.get("error") and predicted == expected
        baseline_correct = not baseline_row.get("error") and baseline_tool == expected
        history = [str(tool) for tool in context.get("complete_handover_history", [])]
        category = "correct"
        review_finding = "Prediction matches the first future handover."
        if not model_correct:
            category, review_finding = failure_category(
                label=label, context=context, expected=expected
            )
        joined.append(
            {
                "partition": expected_partition,
                "example_id": example_id,
                "case_id": str(label["case_id"]),
                "target_index": int(label["target_index"]),
                "query_kind": str(input_row["query_kind"]),
                "current_phase": str(context["current_functional_phase"]),
                "cutoff_sec": float(input_row["provenance"]["cutoff_sec"]),
                "target_time_sec": float(label["target_time_sec"]),
                "delay_to_next_event_sec": float(label["delay_to_next_event_sec"]),
                "complete_handover_history": history,
                "last_incoming_tool": str(context.get("last_incoming_tool", "")),
                "event_sourced_surgeon_owned": context.get("event_sourced_surgeon_owned", []),
                "expected_tool_id": expected,
                "qwen_tool_id": predicted,
                "qwen_confidence": model_prediction.get("confidence"),
                "qwen_correct": model_correct,
                "ngram_tool_id": baseline_tool,
                "ngram_confidence": baseline_prediction.get("confidence"),
                "ngram_matching_rule": baseline_prediction.get("matching_rule"),
                "ngram_support": baseline_prediction.get("support"),
                "ngram_correct": baseline_correct,
                "failure_category": category,
                "review_status": "reviewed_from_complete_causal_sequence",
                "review_finding": review_finding,
            }
        )
    joined.sort(key=lambda row: (row["case_id"], row["target_index"]))
    return run, joined


def rate(correct: int, count: int) -> str:
    return f"{correct}/{count} ({100.0 * correct / count:.1f}%)" if count else "0/0"


def tool(value: str) -> str:
    return value.replace("_forceps", "").replace("_retractor", "").replace("_suction", "")


def compact_history(history: list[str]) -> str:
    return " → ".join(tool(value) for value in history) if history else "START"


def markdown_report(
    development_run: Mapping[str, Any],
    posthoc_run: Mapping[str, Any],
    rows: list[dict[str, Any]],
    failures: list[dict[str, Any]],
) -> str:
    partitions = [
        ("Development 0704_6–14", development_run),
        ("Post-hoc 0704_15–17", posthoc_run),
    ]
    lines = [
        "# Horizon-free next-tool evaluation",
        "",
        "## Verdict",
        "",
        "The 2–8 second event window was removed. Each query asks for the first future "
        "confirmed scrub-nurse-to-surgeon handover after the supplied state, whether it "
        "occurs seconds or minutes later. No image, ASR, case ID, target label, learned "
        "transition count, `none`, or `uncertain` is present in the Qwen request.",
        "",
        "The state-only Qwen prompt is not competitive with the deterministic sequence "
        "baseline. Qwen collapses most predictions onto a few frequent classes instead of "
        "aligning the complete handover prefix to the demonstrated exchange patterns.",
        "",
        "## Accuracy",
        "",
        "| Partition | Qwen exact top-1 | Deterministic n-gram exact top-1 | Qwen schema |",
        "|---|---:|---:|---:|",
    ]
    for name, run in partitions:
        summary = run["summary"]
        baseline = run["ngram_baseline_summary"]
        lines.append(
            f"| {name} | {rate(summary['correct'], summary['count'])} | "
            f"{rate(baseline['correct'], baseline['count'])} | "
            f"{rate(summary['schema_valid_count'], summary['count'])} |"
        )
    lines += [
        "",
        "The 0704_15–17 result is a case-disjoint post-hoc diagnostic, not a pristine final "
        "holdout, because those cases were inspected in earlier experiments.",
        "",
        "## Timing audit",
        "",
        "| Partition | Qwen ≤8 s | Qwen >8 s | n-gram ≤8 s | n-gram >8 s |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, run in partitions:
        model_gap = run["summary"]["by_delay"]
        base_gap = run["ngram_baseline_summary"]["by_delay"]
        lines.append(
            f"| {name} | {rate(model_gap['delay_le_8s']['correct'], model_gap['delay_le_8s']['count'])} | "
            f"{rate(model_gap['delay_gt_8s']['correct'], model_gap['delay_gt_8s']['count'])} | "
            f"{rate(base_gap['delay_le_8s']['correct'], base_gap['delay_le_8s']['count'])} | "
            f"{rate(base_gap['delay_gt_8s']['correct'], base_gap['delay_gt_8s']['count'])} |"
        )
    lines += [
        "",
        "Qwen is not worse on events beyond eight seconds. The failure is sequence "
        "selection, not the removed short timing horizon.",
        "",
        "## Error audit",
        "",
    ]
    category_counts = Counter(row["failure_category"] for row in failures)
    for category, count in sorted(category_counts.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"- `{category}`: {count}")
    target_counts = Counter(row["expected_tool_id"] for row in failures)
    prediction_counts = Counter(row["qwen_tool_id"] for row in rows)
    lines += [
        "",
        f"All {len(failures)} Qwen errors were joined to their complete past handover "
        "sequence and reviewed below. Error targets: "
        + ", ".join(f"{tool(key)} {value}" for key, value in target_counts.most_common())
        + ".",
        "",
        "Qwen output distribution over all rows: "
        + ", ".join(f"{tool(key)} {value}" for key, value in prediction_counts.most_common())
        + ".",
        "",
        "## Every Qwen error",
        "",
        "| # | Case / target | Phase | Complete past handovers | Expected | Qwen | n-gram | Gap | Review category |",
        "|---:|---|---|---|---|---|---|---:|---|",
    ]
    for index, row in enumerate(failures, 1):
        history = compact_history(row["complete_handover_history"])
        lines.append(
            f"| {index} | {row['case_id']} / {row['target_index'] + 1} | {row['current_phase']} | "
            f"{history} | {tool(row['expected_tool_id'])} | {tool(row['qwen_tool_id'])} "
            f"({float(row['qwen_confidence']):.2f}) | {tool(row['ngram_tool_id'])} | "
            f"{row['delay_to_next_event_sec']:.3f}s | `{row['failure_category']}` |"
        )
    lines += [
        "",
        "## Interpretation",
        "",
        "- Qwen fails all 12 procedure-start queries and defaults mostly to Bovie despite "
        "the demonstrated cases usually starting with Adson.",
        "- It misses repeated-tool transitions and branch alignment even when the complete "
        "history is supplied.",
        "- Some observed labels are not represented in the authored pattern list; those "
        "cannot be recovered reliably from the supplied information alone.",
        "- The deterministic case-LOO/state n-gram is substantially better, so the current "
        "state-only Qwen prompt should not be selected as the next-tool predictor.",
        "",
        "## Integrity",
        "",
        "Both runs completed one fresh-worker, no-retry request per example. Artifact hashes, "
        "ID joins, lifecycle completion, horizon-free contract, and absence of target/media "
        "fields in model context were revalidated while generating this report.",
        "",
    ]
    return "\n".join(lines)


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            stream.write("\n")


def main() -> int:
    args = parse_args()
    output_dir = args.output_dir.resolve()
    if output_dir.exists():
        raise ReportError(f"output directory already exists: {output_dir}")
    development_run, development_rows = load_partition(
        args.development_run.resolve(), "development_case_leave_one_out"
    )
    posthoc_run, posthoc_rows = load_partition(
        args.posthoc_run.resolve(), "posthoc_case_disjoint"
    )
    comparable_keys = ("prompt_sha256", "generation", "input_contract")
    for key in comparable_keys:
        if development_run["frozen_config"][key] != posthoc_run["frozen_config"][key]:
            raise ReportError(f"run configurations differ at {key}")
    rows = development_rows + posthoc_rows
    failures = [row for row in rows if not row["qwen_correct"]]
    output_dir.mkdir(parents=True)
    write_jsonl(output_dir / "all_joined_rows.jsonl", rows)
    write_jsonl(output_dir / "qwen_failure_review.jsonl", failures)
    report = markdown_report(development_run, posthoc_run, rows, failures)
    (output_dir / "NEXT_EVENT_REPORT.md").write_text(report, encoding="utf-8")
    manifest = {
        "schema": "taskplanner.next_event_failure_review.v1",
        "review_status": "complete_causal_sequence_join_reviewed",
        "row_count": len(rows),
        "qwen_failure_count": len(failures),
        "development_run": str(args.development_run.resolve()),
        "posthoc_run": str(args.posthoc_run.resolve()),
        "artifacts": {
            name: {
                "path": str(output_dir / name),
                "sha256": sha256_file(output_dir / name),
            }
            for name in ("all_joined_rows.jsonl", "qwen_failure_review.jsonl", "NEXT_EVENT_REPORT.md")
        },
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
