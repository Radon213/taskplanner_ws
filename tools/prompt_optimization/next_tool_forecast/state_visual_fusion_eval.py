#!/usr/bin/env python3
"""Evaluate a case-disjoint thyroidectomy state-pattern + image fusion prompt.

The model receives chronological FLIR/CAM4 frames and an externally supplied
state prior.  The prior consists of a provisional phase, a causal replay of
previous confirmed transfers, the authored thyroidectomy-demo exchange
protocol, and five anonymized similar states from *other* development cases.
No ASR is supplied.

This is evaluation-only.  Phase and transfer-state inputs are offline
annotations, so it is not a pure VLM nor a deployable runtime measurement.
The development run is leave-one-case-out across 0704_6--14.  Any 0704_15--17
run is deliberately marked post-hoc because a previous experiment already
examined that partition.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

from prompt_contract import MODEL_ID, TOOL_ID_SET, extract_json_object, validate_prediction
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
    decode_selected_media,
    ninfer_flock,
    public_catalog_entry,
    reload_worker_batch,
    request_model,
    safe_content,
    sha256_file,
    summarize,
    write_jsonl,
)
from state_context_eval import (
    FINAL_HOLDOUT_SPLIT,
    INITIAL_SURGEON_OWNED,
    RUNS_ROOT,
    StateContextError,
    compact_protocol,
    current_phase,
    input_provenance,
    load_benchmark,
    source_for_case,
)


TASK_DIR = Path(__file__).resolve().parent
SCHEMA = "taskplanner.next_tool_forecast_state_visual_fusion.v1"
DEVELOPMENT_SPLIT = "development_case_leave_one_case_out"
POSTHOC_SPLIT = "posthoc_final_holdout"
VARIANTS = (
    "state_visual_retrieval_v1",
    "state_visual_retrieval_v2_policy",
    "state_visual_retrieval_v3_visible_available",
)

# These values were selected within the 0704_6--14 case-LOO development set.
# They are a small, interpretable retrieval metric rather than a case/time key.
RETRIEVAL_CONFIG = {
    "history_tail": 1,
    "phase_mismatch_penalty": 1.0,
    "last_arrival_age_weight": 0.1,
    "max_age_delta_sec": 12.0,
    "neighbor_count": 5,
    "weighted_vote": True,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--development-calibration-dir", required=True, type=Path)
    parser.add_argument("--development-challenge-dir", required=True, type=Path)
    parser.add_argument("--benchmark-dir", type=Path, default=None)
    parser.add_argument("--mode", choices=(DEVELOPMENT_SPLIT, POSTHOC_SPLIT), required=True)
    parser.add_argument("--variant", choices=VARIANTS, required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--posthoc-selection", type=Path, default=None)
    parser.add_argument("--model", default=MODEL_ID)
    parser.add_argument("--base-url", default=DEFAULT_MANAGER_BASE_URL)
    parser.add_argument("--manager-base-url", default=DEFAULT_MANAGER_BASE_URL)
    parser.add_argument("--worker-base-url", default=DEFAULT_WORKER_BASE_URL)
    parser.add_argument("--api-key-env", default="NINFER_API_KEY")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--top-p", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--max-tokens", type=int, default=128)
    parser.add_argument("--timeout-sec", type=float, default=120.0)
    parser.add_argument("--lifecycle-timeout-sec", type=float, default=180.0)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--threshold", type=float, default=0.65)
    parser.add_argument("--lock-path", type=Path, default=DEFAULT_NINFER_LOCK_PATH)
    return parser.parse_args()


def ensure_output_dir(path: Path) -> Path:
    output = path.resolve()
    root = RUNS_ROOT.resolve()
    try:
        output.relative_to(root)
    except ValueError as exc:
        raise StateContextError(f"output directory must be under {root}") from exc
    if output == root or output.exists():
        raise StateContextError(f"output directory must be a new run subdirectory: {output}")
    return output


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateContextError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise StateContextError(f"JSON object required: {path}")
    return value


def full_development(
    calibration_dir: Path, challenge_dir: Path
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]]:
    calibration_rows, calibration_labels, calibration_files = load_benchmark(
        calibration_dir, "development_calibration"
    )
    challenge_rows, challenge_labels, challenge_files = load_benchmark(
        challenge_dir, "development_challenge"
    )
    rows = calibration_rows + challenge_rows
    labels = calibration_labels | challenge_labels
    identifiers = [str(row["example_id"]) for row in rows]
    if len(identifiers) != len(set(identifiers)) or set(identifiers) != set(labels):
        raise StateContextError("development partitions do not form a one-to-one merged benchmark")
    return rows, labels, {
        "calibration": calibration_files,
        "challenge": challenge_files,
        "merged_example_count": len(rows),
        "merged_example_ids_sha256": hashlib.sha256(
            canonical_json(sorted(identifiers)).encode("utf-8")
        ).hexdigest(),
    }


def enhanced_surgeon_state(events: Iterable[Mapping[str, Any]], cutoff_sec: float) -> dict[str, Any]:
    """Causally replay surgeon state; preserve no absolute timestamp in output."""

    counts: Counter[str] = Counter(INITIAL_SURGEON_OWNED)
    eligible = sorted(
        (
            event
            for event in events
            if event.get("event_type") == "tool_transfer"
            and event.get("review_status") == "confirmed"
            and event.get("tool") in TOOL_ID_SET
            and float(event.get("time_sec", math.inf)) <= cutoff_sec
        ),
        key=lambda event: (float(event["time_sec"]), str(event.get("event_id", ""))),
    )
    incoming: list[Mapping[str, Any]] = []
    for event in eligible:
        tool = str(event["tool"])
        if event.get("to") == "surgeon":
            counts[tool] += 1
            incoming.append(event)
        if event.get("from") == "surgeon":
            counts[tool] = max(0, counts[tool] - 1)
    incoming_tools = [str(event["tool"]) for event in incoming]
    last_incoming_sec = float(incoming[-1]["time_sec"]) if incoming else None
    last_transfer_sec = float(eligible[-1]["time_sec"]) if eligible else None
    return {
        "event_sourced_surgeon_owned": [
            {"tool_id": tool, "count": count}
            for tool, count in sorted(counts.items())
            if count > 0
        ],
        "last_incoming_tool": incoming_tools[-1] if incoming_tools else "",
        "recent_incoming_tools": incoming_tools[-4:],
        "retrieval_history": incoming_tools[-12:],
        "seconds_since_last_incoming": (
            round(max(0.0, cutoff_sec - last_incoming_sec), 3)
            if last_incoming_sec is not None
            else None
        ),
        "seconds_since_any_transfer": (
            round(max(0.0, cutoff_sec - last_transfer_sec), 3)
            if last_transfer_sec is not None
            else None
        ),
    }


def build_contexts(
    rows: Iterable[Mapping[str, Any]], protocol: Mapping[str, Any]
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    sources: dict[str, dict[str, Any]] = {}
    contexts: dict[str, dict[str, Any]] = {}
    integrity: dict[str, Any] = {}
    default = str(protocol["default_phase_id"])
    for row in rows:
        example_id = str(row["example_id"])
        case_id, cutoff_sec = input_provenance(row)
        source = sources.setdefault(case_id, source_for_case(case_id))
        phase_id = current_phase(source["phases"], cutoff_sec, default)
        if phase_id not in protocol["phase_transitions"]:
            raise StateContextError(f"{example_id}: phase absent from procedure specification: {phase_id}")
        contexts[example_id] = {"phase_id": phase_id} | enhanced_surgeon_state(
            source["events"], cutoff_sec
        )
        integrity[case_id] = source["integrity"]
    return contexts, integrity


def outcome_token(label: Mapping[str, Any]) -> str:
    target = label.get("target")
    if not isinstance(target, Mapping):
        raise StateContextError("label target missing")
    if target.get("decision") == "none":
        return "none"
    tool = target.get("tool_id")
    if target.get("decision") != "handover" or tool not in TOOL_ID_SET:
        raise StateContextError("unsupported target tool")
    return str(tool)


def edit_distance(first: Iterable[str], second: Iterable[str]) -> int:
    left, right = list(first), list(second)
    row = list(range(len(right) + 1))
    for index, value in enumerate(left, 1):
        previous, row[0] = row[0], index
        for column, other in enumerate(right, 1):
            old = row[column]
            row[column] = min(row[column] + 1, row[column - 1] + 1, previous + int(value != other))
            previous = old
    return row[-1]


def neighbor_distance(first: Mapping[str, Any], second: Mapping[str, Any]) -> float:
    tail = int(RETRIEVAL_CONFIG["history_tail"])
    distance = float(
        edit_distance(first["retrieval_history"][-tail:], second["retrieval_history"][-tail:])
    )
    if first["phase_id"] != second["phase_id"]:
        distance += float(RETRIEVAL_CONFIG["phase_mismatch_penalty"])
    first_age, second_age = first["seconds_since_last_incoming"], second["seconds_since_last_incoming"]
    if first_age is None or second_age is None:
        age_delta = float(RETRIEVAL_CONFIG["max_age_delta_sec"])
    else:
        age_delta = min(
            float(RETRIEVAL_CONFIG["max_age_delta_sec"]), abs(float(first_age) - float(second_age))
        )
    return distance + float(RETRIEVAL_CONFIG["last_arrival_age_weight"]) * age_delta


def retrieve_prior(
    *,
    query_id: str,
    query_case: str,
    train_rows: Iterable[Mapping[str, Any]],
    labels: Mapping[str, Mapping[str, Any]],
    contexts: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    query = contexts[query_id]
    ranked: list[tuple[float, str]] = []
    for row in train_rows:
        candidate_id = str(row["example_id"])
        candidate_case, _ignored = input_provenance(row)
        if candidate_case == query_case:
            continue
        ranked.append((neighbor_distance(query, contexts[candidate_id]), candidate_id))
    ranked.sort(key=lambda item: (item[0], item[1]))
    top = ranked[: int(RETRIEVAL_CONFIG["neighbor_count"])]
    if len(top) != int(RETRIEVAL_CONFIG["neighbor_count"]):
        raise StateContextError("not enough cross-case neighbors")
    votes: Counter[str] = Counter()
    examples: list[dict[str, Any]] = []
    for rank, (distance, candidate_id) in enumerate(top, 1):
        outcome = outcome_token(labels[candidate_id])
        weight = 1.0 / (1.0 + distance) if RETRIEVAL_CONFIG["weighted_vote"] else 1.0
        votes[outcome] += weight
        state = contexts[candidate_id]
        examples.append(
            {
                "rank": rank,
                "relative_distance": round(distance, 6),
                "similar_state": {
                    "phase_id": state["phase_id"],
                    "last_incoming_tool": state["last_incoming_tool"],
                    "recent_incoming_tools": state["recent_incoming_tools"][-2:],
                    "seconds_since_last_incoming": state["seconds_since_last_incoming"],
                },
                "observed_following_outcome": outcome,
            }
        )
    total = sum(votes.values())
    outcomes = [
        {"outcome": token, "weight": round(weight, 6), "rate": round(weight / total, 6)}
        for token, weight in sorted(votes.items(), key=lambda item: (-item[1], item[0]))
    ]
    return {"neighbors": examples, "vote_outcomes": outcomes}


def phase_transitions_for_context(context: Mapping[str, Any], protocol: Mapping[str, Any]) -> list[dict[str, str]]:
    all_rows = list(protocol["phase_transitions"].get(str(context["phase_id"]), []))
    last = str(context["last_incoming_tool"])
    selected = [row for row in all_rows if row.get("current") == last]
    return (selected or all_rows)[:8]


def state_text(
    *,
    context: Mapping[str, Any],
    protocol: Mapping[str, Any],
    retrieval: Mapping[str, Any],
    variant: str,
) -> str:
    supplied = {
        "procedure": "Open Thyroidectomy Demonstration",
        "current_functional_phase": context["phase_id"],
        "surgeon_state_source": "event-sourced last-known state; not a visual claim",
        "event_sourced_surgeon_owned": context["event_sourced_surgeon_owned"],
        "last_incoming_tool": context["last_incoming_tool"],
        "recent_incoming_tools": context["recent_incoming_tools"],
        "seconds_since_last_incoming": context["seconds_since_last_incoming"],
        "seconds_since_any_transfer": context["seconds_since_any_transfer"],
        "authored_protocol_paths": protocol["handover_paths"],
        "phase_conditioned_transitions": phase_transitions_for_context(context, protocol),
        "cross_case_similar_states": retrieval,
    }
    if variant == "state_visual_retrieval_v1":
        decision_policy = (
            "Use the images and the state prior together. A pattern is only a prior and does not "
            "by itself force a handover."
        )
    elif variant == "state_visual_retrieval_v2_policy":
        decision_policy = (
            "For this v2 policy condition, use the first entry in vote_outcomes as the baseline "
            "decision and return that outcome, including none. Override it only when the current "
            "image sequence gives unmistakable contrary evidence of a different, identifiable "
            "immediate handover or of no such trajectory. Do not lower a baseline handover to "
            "uncertain/none merely because the visual evidence is incomplete."
        )
    elif variant == "state_visual_retrieval_v3_visible_available":
        decision_policy = (
            "First form a visual candidate set from the latest CAM4 image: a handover tool is "
            "eligible only when a distinct physical instance of that tool is visibly resting on "
            "the Mayo stand or instrument stand. A tool held by the surgeon, being used in the "
            "field, or merely carried by the scrub nurse is not an available-stand candidate. "
            "The supplied surgeon-owned counts are type-level event history, not instance IDs: "
            "do not reject a tool type merely because another instance is held, but require a "
            "separate visible tray/stand instance before selecting that type. Do not assume a "
            "tool is available because it is absent from the surgeon state. If the tray/stand "
            "view does not clearly establish an available candidate, return none or uncertain "
            "rather than guessing. Among the visually eligible candidates, use the phase, "
            "exchange pattern, cross-case prior, and chronological receiving trajectory to "
            "choose the first additional handover."
        )
    else:
        raise StateContextError(f"unknown fusion variant: {variant}")
    return (
        "The following structured state is supplied by an external evaluation context. "
        "It contains no future outcome and no source identity. The five analogous outcomes are "
        "from other development cases, not from this case. Treat them as a prior, not as a fixed "
        "sequence. The image sequence remains required to decide whether the proposed next "
        "handover is actually beginning or whether no near-term handover is supported.\n\n"
        "SUPPLIED_STATE_AND_PATTERN_JSON:\n"
        + json.dumps(supplied, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n\nDECISION_POLICY:\n"
        + decision_policy
    )


def prompt_pair(variant: str) -> tuple[str, str]:
    system = (
        "Forecast exactly one task: the first additional scrub-nurse-to-surgeon instrument handover "
        "2 to 8 seconds after the final image. The images are chronological FLIR/CAM4 pairs; the "
        "last CAM4 frame is the causal cutoff. Use the supplied structured thyroidectomy state and "
        "cross-case pattern as a prior, but do not treat it as a label or a fixed timeline. A clear "
        "unfulfilled CAM4 receiving/request trajectory or tool-specific approach can support a "
        "handover. A tool already held, in use, returned, or visibly fulfilled is not a future "
        "handover. If visual evidence and state prior do not support a specific additional tool, "
        "choose none. Never infer case identity, absolute time, annotation provenance, or an "
        "unlisted tool. Allowed tool_id values are: " + ", ".join(sorted(TOOL_ID_SET)) + "."
    )
    if variant == "state_visual_retrieval_v3_visible_available":
        system += (
            " For this available-candidate condition, make a handover prediction only for a "
            "tool whose distinct available instance you can see on the Mayo/instrument stand in "
            "the latest CAM4 image. Do not turn missing inventory information into an assumed "
            "candidate list."
        )
    developer = (
        "Return exactly one JSON object and no markdown or explanation. The exact keys are "
        "decision, tool_id, confidence, uncertainty. decision is handover, none, or uncertain. "
        "tool_id is one allowed canonical ID only for handover and otherwise the empty string. "
        "confidence and uncertainty are finite numbers in [0,1]."
    )
    if variant not in VARIANTS:
        raise StateContextError(f"unknown fusion variant: {variant}")
    return system, developer


def build_messages(
    state: str, images: Iterable[tuple[str, str]], *, variant: str = "state_visual_retrieval_v1"
) -> list[dict[str, Any]]:
    system, developer = prompt_pair(variant)
    image_rows = list(images)
    if len(image_rows) < 4 or len(image_rows) % 2:
        raise StateContextError("expected at least two chronological FLIR/CAM4 image pairs")
    expected = ["flir", "cam4"] * (len(image_rows) // 2)
    if [view for view, _uri in image_rows] != expected:
        raise StateContextError("image order must alternate FLIR then CAM4")
    content: list[dict[str, Any]] = [{"type": "text", "text": state}]
    for view, uri in image_rows:
        if not uri.startswith("data:image/"):
            raise StateContextError("image must be a data URI")
        content.extend(
            [
                {"type": "text", "text": f"VIEW: {view.upper()}"},
                {"type": "image_url", "image_url": {"url": uri}},
            ]
        )
    messages = [
        {"role": "system", "content": system + "\n\nOUTPUT CONTRACT:\n" + developer},
        {"role": "user", "content": content},
    ]
    rendered = canonical_json(messages)
    forbidden = ("0704_", "case_id", "cutoff_sec", "event_id", "annotation_manifest")
    if any(token in rendered for token in forbidden):
        raise StateContextError("source identity/provenance leaked into model request")
    return messages


def deterministic_prediction(retrieval: Mapping[str, Any]) -> dict[str, Any]:
    outcomes = retrieval.get("vote_outcomes")
    if not isinstance(outcomes, list) or not outcomes or not isinstance(outcomes[0], Mapping):
        raise StateContextError("retrieval vote has no outcome")
    token = str(outcomes[0].get("outcome", ""))
    if token == "none":
        return {"decision": "none", "tool_id": "", "confidence": 1.0, "uncertainty": 0.0}
    if token not in TOOL_ID_SET:
        raise StateContextError("retrieval vote selected an invalid tool")
    return {"decision": "handover", "tool_id": token, "confidence": 1.0, "uncertainty": 0.0}


def prepare(
    *,
    selected: list[dict[str, Any]],
    train_rows: list[dict[str, Any]],
    labels: Mapping[str, Mapping[str, Any]],
    contexts: Mapping[str, Mapping[str, Any]],
    protocol: Mapping[str, Any],
    variant: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    media = decode_selected_media(selected)
    prepared: list[dict[str, Any]] = []
    deterministic_rows: list[dict[str, Any]] = []
    context_rows: list[dict[str, Any]] = []
    for sequence, row in enumerate(selected, 1):
        example_id = str(row["example_id"])
        case_id, _cutoff = input_provenance(row)
        context = contexts[example_id]
        retrieval = retrieve_prior(
            query_id=example_id,
            query_case=case_id,
            train_rows=train_rows,
            labels=labels,
            contexts=contexts,
        )
        source_media = row.get("media")
        if not isinstance(source_media, Mapping):
            raise StateContextError("selected input media is missing")
        images: list[tuple[str, str]] = []
        for frame in source_media["frame_indices"]:
            images.extend(
                [
                    ("flir", media[(str(source_media["flir_proxy"]), int(frame))]),
                    ("cam4", media[(str(source_media["cam4_proxy"]), int(frame))]),
                ]
            )
        messages = build_messages(
            state_text(context=context, protocol=protocol, retrieval=retrieval, variant=variant),
            images,
            variant=variant,
        )
        prepared.append(
            {
                "sequence": sequence,
                "example_id": example_id,
                "messages": messages,
                "request_digest": hashlib.sha256(canonical_json(messages).encode("utf-8")).hexdigest(),
            }
        )
        deterministic_rows.append({"example_id": example_id, "prediction": deterministic_prediction(retrieval)})
        context_rows.append(
            {
                "example_id": example_id,
                "model_context": {
                    "phase_id": context["phase_id"],
                    "event_sourced_surgeon_owned": context["event_sourced_surgeon_owned"],
                    "last_incoming_tool": context["last_incoming_tool"],
                    "recent_incoming_tools": context["recent_incoming_tools"],
                    "seconds_since_last_incoming": context["seconds_since_last_incoming"],
                    "seconds_since_any_transfer": context["seconds_since_any_transfer"],
                    "retrieval": retrieval,
                },
                "request_digest": prepared[-1]["request_digest"],
            }
        )
    return prepared, deterministic_rows, context_rows


def frozen_config(args: argparse.Namespace, protocol: Mapping[str, Any]) -> dict[str, Any]:
    system, developer = prompt_pair(args.variant)
    return {
        "variant": args.variant,
        "model": args.model,
        "input_contract": {
            "images": "three chronological FLIR/CAM4 pairs",
            "asr": "absent",
            "externally_supplied_phase": "provisional_context_only",
            "externally_supplied_current_tool_state": "event_sourced_last_known_not_visual",
            "authored_procedure_pattern": "thyroidectomy_demo",
            "cross_case_retrieval": RETRIEVAL_CONFIG,
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
            "threshold": args.threshold,
        },
        "execution_guard": {
            "batch_size": args.batch_size,
            "manager_reload_before_each_batch": True,
            "manager_loaded_vision_check": True,
            "direct_worker_catalog_check": True,
            "automatic_transport_retry": False,
        },
        "evaluator_sha256": sha256_file(Path(__file__)),
    }


def validate_posthoc_selection(
    *,
    path: Path,
    args: argparse.Namespace,
    output_dir: Path,
    selected: list[dict[str, Any]],
    benchmark_files: Mapping[str, Any],
    protocol: Mapping[str, Any],
) -> dict[str, Any]:
    resolved = path.resolve()
    try:
        resolved.relative_to(RUNS_ROOT.resolve())
    except ValueError as exc:
        raise StateContextError("post-hoc selection must be under the run root") from exc
    lock = read_json(resolved)
    if lock.get("schema") != "taskplanner.next_tool_forecast_state_visual_posthoc_selection.v1":
        raise StateContextError("unexpected post-hoc selection schema")
    if lock.get("status") != "authorized_single_posthoc_diagnostic":
        raise StateContextError("post-hoc selection does not authorize this diagnostic")
    if lock.get("frozen_config") != frozen_config(args, protocol):
        raise StateContextError("post-hoc selection configuration mismatch")
    ids = [str(row["example_id"]) for row in selected]
    expected = {
        "output_dir": str(output_dir),
        "inputs_sha256": benchmark_files["inputs_sha256"],
        "labels_sha256": benchmark_files["labels_sha256"],
        "selected_example_ids_sha256": hashlib.sha256(canonical_json(ids).encode("utf-8")).hexdigest(),
        "example_count": len(ids),
    }
    if lock.get("posthoc_final_holdout") != expected:
        raise StateContextError("post-hoc selection benchmark mismatch")
    return {
        "selection_path": str(resolved),
        "selection_sha256": sha256_file(resolved),
        "frozen_config_sha256": hashlib.sha256(
            canonical_json(frozen_config(args, protocol)).encode("utf-8")
        ).hexdigest(),
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    if not 1 <= args.batch_size <= 3:
        raise StateContextError("--batch-size must be 1..3")
    if not 0 <= args.threshold <= 1:
        raise StateContextError("--threshold must be in [0,1]")
    if not args.lock_path.is_absolute():
        raise StateContextError("--lock-path must be absolute")
    output_dir = ensure_output_dir(args.output_dir)
    development_rows, development_labels, development_files = full_development(
        args.development_calibration_dir, args.development_challenge_dir
    )
    if args.mode == DEVELOPMENT_SPLIT:
        if args.benchmark_dir is not None or args.posthoc_selection is not None:
            raise StateContextError("development case-LOO does not accept benchmark-dir or posthoc selection")
        selected, labels, benchmark_files = development_rows, development_labels, development_files
        interpretation = "development case leave-one-case-out evaluation"
    else:
        if args.benchmark_dir is None or args.posthoc_selection is None:
            raise StateContextError("post-hoc final holdout requires benchmark-dir and posthoc selection")
        selected, labels, benchmark_files = load_benchmark(args.benchmark_dir, FINAL_HOLDOUT_SPLIT)
        interpretation = "post-hoc diagnostic only; prior final-holdout inspection prevents independent generalization claim"
    protocol = compact_protocol()
    all_rows = development_rows + [
        row for row in selected if str(row["example_id"]) not in {str(value["example_id"]) for value in development_rows}
    ]
    contexts, source_integrity = build_contexts(all_rows, protocol)
    selection = (
        validate_posthoc_selection(
            path=args.posthoc_selection,
            args=args,
            output_dir=output_dir,
            selected=selected,
            benchmark_files=benchmark_files,
            protocol=protocol,
        )
        if args.mode == POSTHOC_SPLIT
        else None
    )
    prepared, deterministic_unjoined, context_rows = prepare(
        selected=selected,
        train_rows=development_rows,
        labels=development_labels,
        contexts=contexts,
        protocol=protocol,
        variant=args.variant,
    )
    output_dir.mkdir(parents=True, exist_ok=False)
    context_path = output_dir / "input_contexts.jsonl"
    write_jsonl(context_path, context_rows)
    deterministic_by_id = {str(row["example_id"]): row["prediction"] for row in deterministic_unjoined}
    deterministic_rows = [
        {
            "example_id": str(row["example_id"]),
            "target": labels[str(row["example_id"])]["target"],
            "prediction": deterministic_by_id[str(row["example_id"])],
            "error": "",
            "latency_sec": 0.0,
        }
        for row in selected
    ]
    deterministic_path = output_dir / "deterministic_retrieval_baseline.jsonl"
    write_jsonl(deterministic_path, deterministic_rows)
    config = frozen_config(args, protocol)
    common: dict[str, Any] = {
        "schema": SCHEMA,
        "experiment_kind": "evaluation_only_state_visual_fusion",
        "interpretation": interpretation,
        "variant": args.variant,
        "model": args.model,
        "frozen_config": config,
        "benchmark": {
            "mode": args.mode,
            "selected_example_ids": [str(row["example_id"]) for row in selected],
            "files": benchmark_files,
            "development_pattern_files": development_files,
            "context_input_sha256": sha256_file(context_path),
        },
        "source_integrity": source_integrity,
    }
    if selection is not None:
        common["posthoc_selection"] = selection

    api_key = os.environ.get(args.api_key_env, "")
    result_rows: list[dict[str, Any]] = []
    batches: list[dict[str, Any]] = []
    post_count = 0
    try:
        for batch_number, offset in enumerate(range(0, len(prepared), args.batch_size), 1):
            batch = prepared[offset : offset + args.batch_size]
            batch_record: dict[str, Any] = {
                "batch_number": batch_number,
                "example_ids": [str(item["example_id"]) for item in batch],
                "post_cap": args.batch_size,
                "status": "started",
            }
            try:
                with ninfer_flock(args.lock_path):
                    batch_record["fresh_worker"] = reload_worker_batch(
                        manager_base_url=args.manager_base_url,
                        worker_base_url=args.worker_base_url,
                        model=args.model,
                        api_key=api_key,
                        timeout_sec=args.lifecycle_timeout_sec,
                    )
                    for item in batch:
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
                        post_count += 1
                        raw_content = safe_content(response)
                        parsed, parse_error = (
                            extract_json_object(raw_content)
                            if not response_error
                            else (None, response_error)
                        )
                        prediction, validation_error = (
                            validate_prediction(parsed, variant="baseline_v0")
                            if parsed is not None
                            else (None, parse_error)
                        )
                        error = response_error or validation_error
                        example_id = str(item["example_id"])
                        result_rows.append(
                            {
                                "schema": SCHEMA,
                                "sequence": item["sequence"],
                                "request_attempts": 1,
                                "example_id": example_id,
                                "target": labels[example_id]["target"],
                                "prediction": prediction,
                                "error": error,
                                "http_status": _http_status(response_error),
                                "transport_error": response_error if _is_transport_failure(response_error) else "",
                                "contract_error": "" if _is_transport_failure(response_error) else error,
                                "latency_sec": round(time.monotonic() - started, 6),
                                "request_digest": item["request_digest"],
                                "raw_content": raw_content,
                                "raw_response": raw_response,
                            }
                        )
                        if _is_transport_failure(response_error):
                            batch_record["status"] = "aborted_transport"
                            batch_record["failure"] = {"example_id": example_id, "error": response_error[:500]}
                            raise RunAborted(f"batch {batch_number} transport failure at {example_id}: {response_error[:500]}")
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
                    batch_record["post_batch_health"] = {
                        "manager": public_catalog_entry(manager_after),
                        "direct_worker_catalog": worker_after,
                    }
                    if not _manager_is_loaded_vision(manager_after) or not (
                        worker_after["reachable"] and worker_after["model_present"]
                    ):
                        batch_record["status"] = "aborted_post_batch_health"
                        raise RunAborted(f"batch {batch_number} failed post-batch health proof")
                batch_record["status"] = "completed"
                batches.append(batch_record)
            except RunError as exc:
                if batch_record not in batches:
                    if batch_record.get("status") == "started":
                        batch_record["status"] = "aborted_lifecycle"
                    batch_record["failure_message"] = str(exc)[:500]
                    batches.append(batch_record)
                partial_path = output_dir / "partial_predictions.jsonl"
                write_jsonl(partial_path, result_rows)
                aborted = common | {
                    "execution_status": "aborted",
                    "abort_reason": str(exc)[:500],
                    "no_partial_metrics_emitted": True,
                    "post_count": post_count,
                    "lifecycle_batches": batches,
                    "partial_raw_responses_location": str(partial_path),
                    "partial_raw_responses_sha256": sha256_file(partial_path),
                }
                (output_dir / "aborted_run.json").write_text(
                    json.dumps(aborted, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8"
                )
                return {"aborted": True, "output_dir": str(output_dir), "run": aborted}

        predictions_path = output_dir / "predictions.jsonl"
        write_jsonl(predictions_path, result_rows)
        run_document = common | {
            "execution_status": "completed",
            "post_count": post_count,
            "lifecycle_batches": batches,
            "summary": summarize(result_rows, args.threshold),
            "deterministic_retrieval_baseline": summarize(deterministic_rows, args.threshold),
            "predictions_sha256": sha256_file(predictions_path),
            "deterministic_retrieval_baseline_sha256": sha256_file(deterministic_path),
        }
        (output_dir / "run.json").write_text(
            json.dumps(run_document, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        return {"aborted": False, "output_dir": str(output_dir), "run": run_document}
    except Exception:
        shutil.rmtree(output_dir, ignore_errors=True)
        raise


def main() -> int:
    args = parse_args()
    try:
        result = run(args)
    except (StateContextError, RunError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if result["aborted"]:
        print(json.dumps({"status": "aborted_no_partial_metrics", "output_dir": result["output_dir"]}), file=sys.stderr)
        return 2
    print(
        json.dumps(
            {"output_dir": result["output_dir"], "overall": result["run"]["summary"]["overall"]},
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
