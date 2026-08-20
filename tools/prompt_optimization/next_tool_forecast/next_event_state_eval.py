#!/usr/bin/env python3
"""Evaluate horizon-free next-handover tool prediction from supplied state.

This evaluator intentionally differs from the historical 2--8 second frame
benchmark.  It creates exactly one target for the procedure start and one
target after every confirmed scrub-nurse-to-surgeon handover except the last:
the first future scrub-nurse-to-surgeon tool, regardless of elapsed time.

The model receives no image, ASR, timestamp, case identity, target label, or
learned transition counts.  It receives only the causal functional phase,
event-sourced surgeon state, complete past handover history, and the authored
thyroidectomy demonstration exchange patterns.  Every evaluated row has a
future handover, so ``none`` is neither an output nor a scored class.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

from prompt_contract import MODEL_ID, TOOL_ID_SET, extract_json_object
from run_ninfer_eval import (
    DEFAULT_MANAGER_BASE_URL,
    DEFAULT_NINFER_LOCK_PATH,
    DEFAULT_WORKER_BASE_URL,
    RunAborted,
    RunError,
    _http_status,
    _is_transport_failure,
    _manager_catalog_row,
    _manager_is_loaded_vision,
    _worker_catalog_status,
    canonical_json,
    ninfer_flock,
    public_catalog_entry,
    reload_worker_batch,
    request_model,
    safe_content,
    sha256_file,
    write_jsonl,
)
from state_context_eval import (
    RUNS_ROOT,
    StateContextError,
    compact_protocol,
    current_phase,
    source_for_case,
    surgeon_state,
)


TASK_DIR = Path(__file__).resolve().parent
SCHEMA = "taskplanner.next_tool_forecast_next_event_state.v1"
DEVELOPMENT_PARTITION = "development_case_leave_one_out"
POSTHOC_PARTITION = "posthoc_case_disjoint"
PARTITIONS = (DEVELOPMENT_PARTITION, POSTHOC_PARTITION)
DEVELOPMENT_CASES = tuple(f"0704_{index}" for index in range(6, 15))
POSTHOC_CASES = tuple(f"0704_{index}" for index in range(15, 18))


class NextEventError(RuntimeError):
    """Raised when a horizon-free next-event run violates its contract."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--partition", choices=PARTITIONS, required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--model", default=MODEL_ID)
    parser.add_argument("--base-url", default=DEFAULT_MANAGER_BASE_URL)
    parser.add_argument("--manager-base-url", default=DEFAULT_MANAGER_BASE_URL)
    parser.add_argument("--worker-base-url", default=DEFAULT_WORKER_BASE_URL)
    parser.add_argument("--api-key-env", default="NINFER_API_KEY")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--top-p", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--max-tokens", type=int, default=96)
    parser.add_argument("--timeout-sec", type=float, default=120.0)
    parser.add_argument("--lifecycle-timeout-sec", type=float, default=180.0)
    parser.add_argument("--lock-path", type=Path, default=DEFAULT_NINFER_LOCK_PATH)
    return parser.parse_args()


def ensure_output_dir(path: Path) -> Path:
    output = path.resolve()
    root = RUNS_ROOT.resolve()
    try:
        output.relative_to(root)
    except ValueError as exc:
        raise NextEventError(f"output directory must be under {root}") from exc
    if output == root or output.exists():
        raise NextEventError(f"output directory must be a new run subdirectory: {output}")
    return output


def confirmed_handover_events(events: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows = [
        dict(event)
        for event in events
        if event.get("event_type") == "tool_transfer"
        and event.get("review_status") == "confirmed"
        and event.get("from") == "scrub_nurse"
        and event.get("to") == "surgeon"
        and event.get("tool") in TOOL_ID_SET
    ]
    return sorted(rows, key=lambda event: (float(event["time_sec"]), str(event.get("event_id", ""))))


def build_partition_rows(
    cases: Iterable[str], protocol: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Build separated model inputs and labels for one target per next event."""

    inputs: list[dict[str, Any]] = []
    labels: list[dict[str, Any]] = []
    integrity: dict[str, Any] = {}
    default_phase = str(protocol["default_phase_id"])
    for case_id in cases:
        source = source_for_case(case_id)
        handovers = confirmed_handover_events(source["events"])
        if not handovers:
            raise NextEventError(f"{case_id}: no confirmed handover events")
        integrity[case_id] = source["integrity"] | {"confirmed_handover_count": len(handovers)}
        for target_index, target_event in enumerate(handovers):
            if target_index == 0:
                cutoff_sec = 0.0
                history: list[str] = []
                query_kind = "procedure_start"
            else:
                cutoff_sec = float(handovers[target_index - 1]["time_sec"])
                history = [str(event["tool"]) for event in handovers[:target_index]]
                query_kind = "after_confirmed_handover"
            target_time = float(target_event["time_sec"])
            if target_time <= cutoff_sec:
                raise NextEventError(f"{case_id}: non-future target at index {target_index}")
            phase_id = current_phase(source["phases"], cutoff_sec, default_phase)
            if phase_id not in protocol["phase_transitions"]:
                raise NextEventError(f"{case_id}: phase {phase_id} absent from authored protocol")
            state = surgeon_state(source["events"], cutoff_sec)
            example_id = f"next-event:{case_id}:target-{target_index + 1:03d}"
            model_context = {
                "task": "first future scrub-nurse-to-surgeon handover tool; elapsed time is irrelevant",
                "procedure": "Open Thyroidectomy Demonstration",
                "current_functional_phase": phase_id,
                "completed_handover_count": len(history),
                "complete_handover_history": history,
                "event_sourced_surgeon_owned": state["event_sourced_surgeon_owned"],
                "last_incoming_tool": state["last_incoming_tool"],
                "authored_protocol_exchange_paths": protocol["handover_paths"],
                "authored_phase_conditioned_transitions": list(
                    protocol["phase_transitions"].get(phase_id, [])
                ),
            }
            inputs.append(
                {
                    "example_id": example_id,
                    "partition_role": "model_input",
                    "query_kind": query_kind,
                    "model_context": model_context,
                    "provenance": {"case_id": case_id, "cutoff_sec": cutoff_sec},
                }
            )
            labels.append(
                {
                    "example_id": example_id,
                    "partition_role": "offline_label_only",
                    "target_tool_id": str(target_event["tool"]),
                    "target_event_id": str(target_event.get("event_id", "")),
                    "target_time_sec": target_time,
                    "delay_to_next_event_sec": round(target_time - cutoff_sec, 6),
                    "case_id": case_id,
                    "target_index": target_index,
                }
            )
    if len(inputs) != len(labels) or {row["example_id"] for row in inputs} != {
        row["example_id"] for row in labels
    }:
        raise NextEventError("input/label one-to-one boundary failed")
    return inputs, labels, integrity


def prompt_pair() -> tuple[str, str]:
    allowed = ", ".join(sorted(TOOL_ID_SET))
    system = (
        "Predict exactly one thing: the tool_id of the first future confirmed "
        "scrub-nurse-to-surgeon handover after the supplied state. A future handover is "
        "guaranteed for every request. It may occur seconds or minutes later; do not predict "
        "when it happens and do not require an immediate request or transfer cue. Use only the "
        "supplied current phase, complete past handover history, current surgeon-held state, and "
        "authored thyroidectomy exchange patterns. Never output none, uncertain, an end state, "
        "or a tool outside this allowed set: "
        + allowed
        + "."
    )
    developer = (
        "Return exactly one JSON object with exactly two keys: tool_id and confidence. tool_id "
        "must be one allowed canonical ID. confidence must be a finite number in [0,1]. Return "
        "no markdown, explanation, timing, decision field, alternatives, or additional keys."
    )
    return system, developer


def build_messages(model_context: Mapping[str, Any]) -> list[dict[str, Any]]:
    system, developer = prompt_pair()
    messages = [
        {"role": "system", "content": system + "\n\nOUTPUT CONTRACT:\n" + developer},
        {
            "role": "user",
            "content": "SUPPLIED_STATE_JSON:\n"
            + json.dumps(model_context, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        },
    ]
    rendered = canonical_json(messages)
    forbidden = (
        "0704_",
        "case_id",
        "cutoff_sec",
        "target_tool_id",
        "target_event_id",
        "target_time_sec",
        "delay_to_next_event_sec",
        "image_url",
        "data:image",
    )
    if any(token in rendered for token in forbidden):
        raise NextEventError("provenance, target, or media leaked into model request")
    return messages


def validate_prediction(value: Any) -> tuple[dict[str, Any] | None, str]:
    if not isinstance(value, dict):
        return None, "prediction is not a JSON object"
    if set(value) != {"tool_id", "confidence"}:
        return None, "prediction must have exactly tool_id and confidence"
    tool_id = value.get("tool_id")
    confidence = value.get("confidence")
    if tool_id not in TOOL_ID_SET:
        return None, "tool_id is not an allowed canonical tool"
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        return None, "confidence is not numeric"
    if not math.isfinite(float(confidence)) or not 0.0 <= float(confidence) <= 1.0:
        return None, "confidence is outside [0,1]"
    return {"tool_id": str(tool_id), "confidence": float(confidence)}, ""


def row_case(row: Mapping[str, Any]) -> str:
    provenance = row.get("provenance")
    if not isinstance(provenance, Mapping) or not isinstance(provenance.get("case_id"), str):
        raise NextEventError("input row has no case provenance")
    return str(provenance["case_id"])


def ngram_key(context: Mapping[str, Any], use_phase: bool, depth: int) -> tuple[Any, ...]:
    history = tuple(str(tool) for tool in context.get("complete_handover_history", []))
    suffix = history[-depth:] if depth else tuple()
    return ((str(context["current_functional_phase"]),) if use_phase else tuple()) + suffix


def ngram_prediction(
    query: Mapping[str, Any],
    training_inputs: Iterable[Mapping[str, Any]],
    training_labels: Mapping[str, Mapping[str, Any]],
    *,
    excluded_case: str | None,
) -> dict[str, Any]:
    candidates = [row for row in training_inputs if row_case(row) != excluded_case]
    if not candidates:
        raise NextEventError("n-gram baseline has no training cases")
    query_context = query["model_context"]
    rules = [
        (True, 3, "phase+last3"),
        (True, 2, "phase+last2"),
        (True, 1, "phase+last1"),
        (True, 0, "phase"),
        (False, 3, "last3"),
        (False, 2, "last2"),
        (False, 1, "last1"),
        (False, 0, "global"),
    ]
    for use_phase, depth, name in rules:
        key = ngram_key(query_context, use_phase, depth)
        counts: Counter[str] = Counter()
        for row in candidates:
            context = row["model_context"]
            if ngram_key(context, use_phase, depth) != key:
                continue
            label = training_labels[str(row["example_id"])]
            counts[str(label["target_tool_id"])] += 1
        if counts:
            tool_id, count = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0]
            return {
                "tool_id": tool_id,
                "confidence": round(count / sum(counts.values()), 6),
                "matching_rule": name,
                "support": sum(counts.values()),
                "counts": dict(sorted(counts.items())),
            }
    raise NextEventError("n-gram baseline failed to produce a prediction")


def score_rows(
    predictions: Iterable[Mapping[str, Any]], labels: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    rows = list(predictions)
    case_counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    tool_counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    query_counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    gap_counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    confusion: Counter[str] = Counter()
    correct = 0
    valid = 0
    for row in rows:
        label = labels[str(row["example_id"])]
        expected = str(label["target_tool_id"])
        prediction = row.get("prediction")
        predicted = str(prediction.get("tool_id")) if isinstance(prediction, Mapping) else "invalid"
        is_valid = isinstance(prediction, Mapping) and predicted in TOOL_ID_SET and not row.get("error")
        is_correct = is_valid and predicted == expected
        valid += int(is_valid)
        correct += int(is_correct)
        case = str(label["case_id"])
        kind = "initial" if int(label["target_index"]) == 0 else "transition"
        gap = "delay_gt_8s" if float(label["delay_to_next_event_sec"]) > 8.0 else "delay_le_8s"
        for bucket, key in ((case_counts, case), (tool_counts, expected), (query_counts, kind), (gap_counts, gap)):
            bucket[key][0] += int(is_correct)
            bucket[key][1] += 1
        confusion[f"{expected}->{predicted}"] += 1

    def rates(source: Mapping[str, list[int]]) -> dict[str, Any]:
        return {
            key: {"correct": value[0], "count": value[1], "accuracy": value[0] / value[1]}
            for key, value in sorted(source.items())
        }

    by_case = rates(case_counts)
    return {
        "count": len(rows),
        "correct": correct,
        "accuracy": correct / len(rows) if rows else 0.0,
        "schema_valid_count": valid,
        "schema_valid_rate": valid / len(rows) if rows else 0.0,
        "macro_case_accuracy": (
            sum(value["accuracy"] for value in by_case.values()) / len(by_case) if by_case else 0.0
        ),
        "by_case": by_case,
        "by_expected_tool": rates(tool_counts),
        "by_query_kind": rates(query_counts),
        "by_delay": rates(gap_counts),
        "confusion": dict(sorted(confusion.items())),
    }


def config(args: argparse.Namespace, protocol: Mapping[str, Any]) -> dict[str, Any]:
    system, developer = prompt_pair()
    return {
        "task": "horizon_free_first_future_handover_tool",
        "model": args.model,
        "input_contract": {
            "images": "absent",
            "asr": "absent",
            "time_horizon": "none",
            "future_handover_guaranteed": True,
            "none_class": "absent",
            "current_phase": "offline_reviewed_context_only",
            "surgeon_state": "event_sourced_causal_context",
            "handover_history": "complete_past_only",
            "procedure_pattern": "authored_thyroidectomy_demo",
            "learned_transition_counts": "absent",
        },
        "prompt_sha256": {
            "system": hashlib.sha256(system.encode("utf-8")).hexdigest(),
            "developer": hashlib.sha256(developer.encode("utf-8")).hexdigest(),
            "procedure_spec": str(protocol["source_sha256"]),
        },
        "generation": {
            "temperature": args.temperature,
            "top_p": args.top_p,
            "seed": args.seed,
            "max_tokens": args.max_tokens,
            "enable_thinking": False,
        },
        "execution_guard": {
            "batch_size": 1,
            "manager_reload_before_every_request": True,
            "automatic_retry": False,
            "serialized_lock_path": str(args.lock_path),
        },
        "evaluator_sha256": sha256_file(Path(__file__)),
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    if not args.lock_path.is_absolute():
        raise NextEventError("--lock-path must be absolute")
    output_dir = ensure_output_dir(args.output_dir)
    protocol = compact_protocol()
    development_inputs, development_label_rows, development_integrity = build_partition_rows(
        DEVELOPMENT_CASES, protocol
    )
    development_labels = {str(row["example_id"]): row for row in development_label_rows}
    if args.partition == DEVELOPMENT_PARTITION:
        selected_inputs = development_inputs
        selected_label_rows = development_label_rows
        source_integrity = development_integrity
        interpretation = "development leave-one-case-out benchmark"
    else:
        selected_inputs, selected_label_rows, source_integrity = build_partition_rows(
            POSTHOC_CASES, protocol
        )
        interpretation = (
            "post-hoc case-disjoint diagnostic; these cases were inspected by earlier experiments"
        )
    selected_labels = {str(row["example_id"]): row for row in selected_label_rows}
    prepared: list[dict[str, Any]] = []
    input_artifact: list[dict[str, Any]] = []
    for sequence, row in enumerate(selected_inputs, 1):
        messages = build_messages(row["model_context"])
        prepared.append(
            {
                "sequence": sequence,
                "example_id": str(row["example_id"]),
                "messages": messages,
                "request_digest": hashlib.sha256(canonical_json(messages).encode("utf-8")).hexdigest(),
            }
        )
        input_artifact.append(row | {"request_digest": prepared[-1]["request_digest"]})

    baseline_rows: list[dict[str, Any]] = []
    for row in selected_inputs:
        case = row_case(row)
        prediction = ngram_prediction(
            row,
            development_inputs,
            development_labels,
            excluded_case=case if args.partition == DEVELOPMENT_PARTITION else None,
        )
        baseline_rows.append(
            {
                "example_id": str(row["example_id"]),
                "prediction": prediction,
                "error": "",
            }
        )

    output_dir.mkdir(parents=True, exist_ok=False)
    inputs_path = output_dir / "inputs.jsonl"
    labels_path = output_dir / "labels.jsonl"
    baseline_path = output_dir / "ngram_baseline_predictions.jsonl"
    write_jsonl(inputs_path, input_artifact)
    write_jsonl(labels_path, selected_label_rows)
    write_jsonl(baseline_path, baseline_rows)

    api_key = os.environ.get(args.api_key_env, "")
    results: list[dict[str, Any]] = []
    lifecycle: list[dict[str, Any]] = []
    common = {
        "schema": SCHEMA,
        "execution_status": "started",
        "partition": args.partition,
        "interpretation": interpretation,
        "cases": list(DEVELOPMENT_CASES if args.partition == DEVELOPMENT_PARTITION else POSTHOC_CASES),
        "model": args.model,
        "frozen_config": config(args, protocol),
        "source_integrity": source_integrity,
        "artifacts": {
            "inputs": {"path": str(inputs_path), "sha256": sha256_file(inputs_path)},
            "labels": {"path": str(labels_path), "sha256": sha256_file(labels_path)},
            "ngram_baseline": {
                "path": str(baseline_path),
                "sha256": sha256_file(baseline_path),
            },
        },
    }
    try:
        for item in prepared:
            record: dict[str, Any] = {
                "sequence": item["sequence"],
                "example_id": item["example_id"],
                "status": "started",
            }
            try:
                with ninfer_flock(args.lock_path):
                    record["fresh_worker"] = reload_worker_batch(
                        manager_base_url=args.manager_base_url,
                        worker_base_url=args.worker_base_url,
                        model=args.model,
                        api_key=api_key,
                        timeout_sec=args.lifecycle_timeout_sec,
                    )
                    started = time.monotonic()
                    response, raw_response, response_error = request_model(
                        base_url=args.base_url,
                        model=args.model,
                        messages=item["messages"],
                        temperature=args.temperature,
                        top_p=args.top_p,
                        seed=args.seed,
                        max_tokens=args.max_tokens,
                        enable_thinking=False,
                        timeout_sec=args.timeout_sec,
                        lock_path=None,
                        api_key=api_key,
                    )
                    raw_content = safe_content(response)
                    parsed, parse_error = (
                        extract_json_object(raw_content)
                        if not response_error
                        else (None, response_error)
                    )
                    prediction, validation_error = (
                        validate_prediction(parsed) if parsed is not None else (None, parse_error)
                    )
                    error = response_error or validation_error
                    results.append(
                        {
                            "schema": SCHEMA,
                            "sequence": item["sequence"],
                            "example_id": item["example_id"],
                            "prediction": prediction,
                            "error": error,
                            "http_status": _http_status(response_error),
                            "transport_error": (
                                response_error if _is_transport_failure(response_error) else ""
                            ),
                            "latency_sec": round(time.monotonic() - started, 6),
                            "request_digest": item["request_digest"],
                            "raw_content": raw_content,
                            "raw_response": raw_response,
                        }
                    )
                    if _is_transport_failure(response_error):
                        record["status"] = "aborted_transport"
                        raise RunAborted(
                            f"request {item['sequence']} transport failure: {response_error[:500]}"
                        )
                    manager_after = _manager_catalog_row(
                        manager_base_url=args.manager_base_url,
                        model=args.model,
                        timeout_sec=min(args.lifecycle_timeout_sec, 10.0),
                        api_key=api_key,
                    )
                    worker_after = _worker_catalog_status(
                        worker_base_url=args.worker_base_url,
                        model=args.model,
                        timeout_sec=5.0,
                        api_key=api_key,
                    )
                    record["post_request_health"] = {
                        "manager": public_catalog_entry(manager_after),
                        "direct_worker_catalog": worker_after,
                    }
                    if not _manager_is_loaded_vision(manager_after) or not (
                        worker_after["reachable"] and worker_after["model_present"]
                    ):
                        record["status"] = "aborted_post_request_health"
                        raise RunAborted(f"request {item['sequence']} failed post-request health")
                record["status"] = "completed"
                lifecycle.append(record)
            except RunError as exc:
                if record not in lifecycle:
                    if record["status"] == "started":
                        record["status"] = "aborted_lifecycle"
                    record["failure_message"] = str(exc)[:500]
                    lifecycle.append(record)
                partial_path = output_dir / "partial_predictions.jsonl"
                write_jsonl(partial_path, results)
                aborted = common | {
                    "execution_status": "aborted",
                    "abort_reason": str(exc)[:500],
                    "completed_request_count": len(results),
                    "no_partial_metrics_emitted": True,
                    "lifecycle": lifecycle,
                    "partial_predictions": {
                        "path": str(partial_path),
                        "sha256": sha256_file(partial_path),
                    },
                }
                (output_dir / "aborted_run.json").write_text(
                    json.dumps(aborted, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                raise
    except RunError:
        raise

    if len(results) != len(selected_inputs):
        raise NextEventError("complete run has an unexpected result count")
    predictions_path = output_dir / "predictions.jsonl"
    write_jsonl(predictions_path, results)
    model_summary = score_rows(results, selected_labels)
    baseline_summary = score_rows(baseline_rows, selected_labels)
    document = common | {
        "execution_status": "completed",
        "post_count": len(results),
        "lifecycle": lifecycle,
        "predictions": {
            "path": str(predictions_path),
            "sha256": sha256_file(predictions_path),
        },
        "summary": model_summary,
        "ngram_baseline_summary": baseline_summary,
    }
    run_path = output_dir / "run.json"
    run_path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return document


def main() -> int:
    try:
        document = run(parse_args())
    except (NextEventError, StateContextError, RunError) as exc:
        print(f"ERROR: {exc}", file=os.sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "partition": document["partition"],
                "model": document["summary"],
                "ngram_baseline": document["ngram_baseline_summary"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
