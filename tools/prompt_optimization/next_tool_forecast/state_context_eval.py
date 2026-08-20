#!/usr/bin/env python3
"""Evaluate next-tool forecasting from supplied procedure state only.

This is deliberately an *oracle-state ablation*, not a replacement for the
image/ASR VLM benchmark.  It gives the model no images and no ASR.  Instead it
receives three externally supplied, causally clipped fields:

* a provisional functional phase;
* an event-sourced, last-known surgeon tool inventory; and
* a thyroidectomy-demo exchange-pattern prior.

Those sources are evaluation-only annotations today.  In particular, phase
events are explicitly provisional and the observed transfer log is an
offline-reviewed source.  The resulting score is therefore an upper-bound for
this *state-input condition*, never a pure VLM or deployable-runtime score.

Two prompt variants are supported:

``procedure_pattern_v1``
    Authored thyroidectomy-demo protocol plus the supplied current state.

``procedure_pattern_v2_calibration``
    The same protocol plus a transition distribution learned only from the
    0704_6--14 calibration labels.  During calibration, the current case is
    excluded from that distribution; during final holdout all 0704_6--14 rows
    are used.  This prevents the target row, any future row of its case, and
    every 0704_15--17 target from entering its model request.

The output contract and exact-top-1 metric match ``run_ninfer_eval.py``.  No
label, case ID, absolute time, frame, file path, or image/ASR payload is sent
to the model.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

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
    binary_metrics,
    canonical_json,
    ninfer_flock,
    public_catalog_entry,
    reload_worker_batch,
    request_model,
    safe_content,
    sha256_file,
    summarize,
    write_jsonl,
)


TASK_DIR = Path(__file__).resolve().parent
REPO_ROOT = TASK_DIR.parents[2]
RUNS_ROOT = TASK_DIR / "runs"
CASES_ROOT = REPO_ROOT / "annotations/observable_tool_events/cases"
TOOL_CATALOG_PATH = REPO_ROOT / "annotations/observable_tool_events/catalogs/tools.yaml"
PROCEDURE_PATH = (
    REPO_ROOT / "src/procedure_spec/procedure_spec/specs/thyroidectomy_demo/vlm_procedure_prompt.yaml"
)

SCHEMA = "taskplanner.next_tool_forecast_state_context_eval.v1"
VARIANTS = (
    "procedure_pattern_v1",
    "procedure_pattern_v2_calibration",
    "procedure_pattern_v3_authored_state_only",
)
CALIBRATION_SPLIT = "development_calibration"
DEVELOPMENT_CHALLENGE_SPLIT = "development_challenge"
FINAL_HOLDOUT_SPLIT = "final_holdout"
INITIAL_SURGEON_OWNED = {"allis_forceps": 2}


class StateContextError(RuntimeError):
    """Raised when the state-only oracle input cannot be built safely."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calibration-dir", required=True, type=Path)
    parser.add_argument("--benchmark-dir", required=True, type=Path)
    parser.add_argument(
        "--split",
        choices=(CALIBRATION_SPLIT, DEVELOPMENT_CHALLENGE_SPLIT, FINAL_HOLDOUT_SPLIT),
        required=True,
    )
    parser.add_argument("--variant", choices=VARIANTS, required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
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
    parser.add_argument(
        "--frozen-selection",
        type=Path,
        default=None,
        help="Required for a final-holdout live run; pins the selected prompt and manifest.",
    )
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateContextError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise StateContextError(f"JSON object required: {path}")
    return value


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise StateContextError(f"missing JSONL: {path}")
    values: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as stream:
            for number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise StateContextError(f"JSON object required: {path}:{number}")
                values.append(value)
    except (OSError, json.JSONDecodeError) as exc:
        raise StateContextError(f"cannot read JSONL {path}: {exc}") from exc
    return values


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


def resolve_bound_file(base: Path, descriptor: Mapping[str, Any], label: str) -> tuple[Path, str]:
    relative = descriptor.get("file")
    expected = descriptor.get("sha256")
    if not isinstance(relative, str) or not relative or not isinstance(expected, str) or len(expected) != 64:
        raise StateContextError(f"{label}: invalid source binding")
    path = (base / relative).resolve()
    root = REPO_ROOT.resolve()
    if path != root and root not in path.parents:
        raise StateContextError(f"{label}: source path escapes workspace")
    if not path.is_file():
        raise StateContextError(f"{label}: source is missing: {path}")
    actual = sha256_file(path)
    if actual != expected:
        raise StateContextError(f"{label}: SHA-256 mismatch")
    return path, actual


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise StateContextError(f"cannot read YAML {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise StateContextError(f"YAML object required: {path}")
    return value


def tool_ref_mapping() -> dict[str, str]:
    catalog = load_yaml(TOOL_CATALOG_PATH)
    rows = catalog.get("tools")
    if not isinstance(rows, list):
        raise StateContextError("tool catalog has no tools list")
    result: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            continue
        for reference in row.get("procedure_refs", []):
            if isinstance(reference, str):
                result[reference] = str(row["id"])
    if set(result.values()) - set(TOOL_ID_SET):
        raise StateContextError("procedure tool mapping contains an unsupported output tool")
    return result


def canonical_tool(value: Any, refs: Mapping[str, str]) -> str:
    if not isinstance(value, str):
        raise StateContextError("procedure tool reference must be text")
    tool = refs.get(value)
    if tool is None:
        raise StateContextError(f"unmapped procedure tool reference: {value}")
    return tool


def compact_protocol() -> dict[str, Any]:
    """Return the authored pattern as safe canonical IDs, with no case data."""

    source = load_yaml(PROCEDURE_PATH)
    refs = tool_ref_mapping()
    procedure = source.get("procedure")
    patterns = source.get("handover_patterns")
    phase_details = source.get("phase_details")
    if not isinstance(procedure, dict) or not isinstance(patterns, dict) or not isinstance(phase_details, dict):
        raise StateContextError("thyroidectomy-demo procedure specification is incomplete")

    sequence_paths: dict[str, list[list[str]]] = {}
    for name in ("primary", "alternatives"):
        raw_paths = patterns.get(name)
        if not isinstance(raw_paths, list):
            raise StateContextError(f"procedure handover pattern {name} missing")
        sequence_paths[name] = [
            [canonical_tool(item, refs) for item in path]
            for path in raw_paths
            if isinstance(path, list)
        ]
    phase_transitions: dict[str, list[dict[str, str]]] = {}
    for phase_id, detail in phase_details.items():
        if not isinstance(phase_id, str) or not isinstance(detail, dict):
            continue
        rows: list[dict[str, str]] = []
        for item in detail.get("expected_tool_sequence", []):
            if not isinstance(item, dict) or not isinstance(item.get("current"), str) or not isinstance(item.get("next"), str):
                continue
            rows.append(
                {
                    "current": canonical_tool(item["current"], refs),
                    "next": canonical_tool(item["next"], refs),
                    "strength": str(item.get("strength", "")),
                }
            )
        phase_transitions[phase_id] = rows
    phase_labels = procedure.get("name")
    return {
        "procedure_name": str(phase_labels),
        "default_phase_id": str(procedure.get("default_phase_id", "P03")),
        "handover_paths": sequence_paths,
        "phase_transitions": phase_transitions,
        "source_sha256": sha256_file(PROCEDURE_PATH),
    }


def load_benchmark(directory: Path, split: str) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, str]]:
    directory = directory.resolve()
    inputs_path = directory / "inputs.jsonl"
    labels_path = directory / "labels.jsonl"
    inputs = read_jsonl(inputs_path)
    labels = read_jsonl(labels_path)
    by_id = {str(row.get("example_id", "")): row for row in labels}
    if not inputs or len(by_id) != len(labels):
        raise StateContextError(f"{directory}: empty or duplicate labels")
    selected = [row for row in inputs if row.get("split") == split]
    input_ids = {str(row.get("example_id", "")) for row in selected}
    if not input_ids or "" in input_ids or input_ids != set(by_id):
        raise StateContextError(f"{directory}: input/label one-to-one mismatch")
    if any(row.get("split") != split for row in by_id.values()):
        raise StateContextError(f"{directory}: label split mismatch")
    return selected, by_id, {
        "inputs_path": str(inputs_path),
        "labels_path": str(labels_path),
        "inputs_sha256": sha256_file(inputs_path),
        "labels_sha256": sha256_file(labels_path),
    }


def source_for_case(case_id: str) -> dict[str, Any]:
    case_dir = CASES_ROOT / case_id
    manifest_path = case_dir / "annotation_manifest.json"
    manifest = read_json(manifest_path)
    if manifest.get("case_id") != case_id:
        raise StateContextError(f"{case_id}: manifest case mismatch")
    evaluation = manifest.get("evaluation_reference")
    if not isinstance(evaluation, dict) or evaluation.get("complete") is not True:
        raise StateContextError(f"{case_id}: complete evaluation reference required")
    observed_descriptor = evaluation.get("observed_reference")
    phase_descriptor = evaluation.get("phase_reference")
    if not isinstance(observed_descriptor, dict) or not isinstance(phase_descriptor, dict):
        raise StateContextError(f"{case_id}: observed/phase source missing")
    if phase_descriptor.get("scoring_role") != "context_only_not_ground_truth":
        raise StateContextError(f"{case_id}: phase source is not explicitly context-only")
    observed_path, observed_hash = resolve_bound_file(case_dir, observed_descriptor, f"{case_id} observed")
    phase_path, phase_hash = resolve_bound_file(case_dir, phase_descriptor, f"{case_id} phase")
    return {
        "events": read_jsonl(observed_path),
        "phases": read_jsonl(phase_path),
        "integrity": {
            "annotation_manifest": {"path": str(manifest_path), "sha256": sha256_file(manifest_path)},
            "observed_reference": {"path": str(observed_path), "sha256": observed_hash},
            "phase_reference": {
                "path": str(phase_path),
                "sha256": phase_hash,
                "status": str(phase_descriptor.get("status", "")),
                "scoring_role": str(phase_descriptor.get("scoring_role", "")),
            },
        },
    }


def current_phase(phases: Iterable[Mapping[str, Any]], cutoff_sec: float, default: str) -> str:
    choices = [
        event
        for event in phases
        if event.get("event_type") == "phase_start"
        and isinstance(event.get("phase_id"), str)
        and float(event.get("time_sec", float("inf"))) <= cutoff_sec
    ]
    if not choices:
        return default
    current = max(choices, key=lambda event: (float(event["time_sec"]), str(event.get("event_id", ""))))
    return str(current["phase_id"])


def surgeon_state(events: Iterable[Mapping[str, Any]], cutoff_sec: float) -> dict[str, Any]:
    """Replay only confirmed transfers at or before cutoff into a surrogate state."""

    counts: Counter[str] = Counter(INITIAL_SURGEON_OWNED)
    arrivals: list[str] = []
    rows = sorted(
        (
            event
            for event in events
            if event.get("event_type") == "tool_transfer"
            and event.get("review_status") == "confirmed"
            and isinstance(event.get("tool"), str)
            and event.get("tool") in TOOL_ID_SET
            and float(event.get("time_sec", float("inf"))) <= cutoff_sec
        ),
        key=lambda event: (float(event["time_sec"]), str(event.get("event_id", ""))),
    )
    for event in rows:
        tool = str(event["tool"])
        if event.get("to") == "surgeon":
            counts[tool] += 1
            arrivals.append(tool)
        if event.get("from") == "surgeon":
            counts[tool] = max(0, counts[tool] - 1)
    owned = [{"tool_id": tool, "count": count} for tool, count in sorted(counts.items()) if count > 0]
    return {
        "event_sourced_surgeon_owned": owned,
        "last_incoming_tool": arrivals[-1] if arrivals else "",
        "recent_incoming_tools": arrivals[-4:],
    }


def input_provenance(row: Mapping[str, Any]) -> tuple[str, float]:
    provenance = row.get("provenance")
    if not isinstance(provenance, dict):
        raise StateContextError("base benchmark input has no local provenance")
    case_id = provenance.get("case_id")
    cutoff = provenance.get("cutoff_sec")
    if not isinstance(case_id, str) or not case_id.startswith("0704_"):
        raise StateContextError("base benchmark input has invalid local case provenance")
    try:
        cutoff_sec = float(cutoff)
    except (TypeError, ValueError) as exc:
        raise StateContextError("base benchmark input has invalid cutoff provenance") from exc
    return case_id, cutoff_sec


def build_contexts(rows: Iterable[Mapping[str, Any]], protocol: Mapping[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    cache: dict[str, dict[str, Any]] = {}
    contexts: dict[str, dict[str, Any]] = {}
    source_integrity: dict[str, Any] = {}
    default_phase = str(protocol["default_phase_id"])
    for row in rows:
        example_id = str(row.get("example_id", ""))
        case_id, cutoff_sec = input_provenance(row)
        if case_id not in cache:
            cache[case_id] = source_for_case(case_id)
        source = cache[case_id]
        phase_id = current_phase(source["phases"], cutoff_sec, default_phase)
        if phase_id not in protocol["phase_transitions"]:
            raise StateContextError(f"{example_id}: unknown phase {phase_id} in procedure spec")
        state = surgeon_state(source["events"], cutoff_sec)
        # This dictionary becomes model-facing after the pattern distribution is
        # added below.  It intentionally contains no identifiers or timestamps.
        contexts[example_id] = {"phase_id": phase_id} | state
        source_integrity[case_id] = source["integrity"]
    return contexts, source_integrity


def outcome_token(label: Mapping[str, Any]) -> str:
    target = label.get("target")
    if not isinstance(target, dict):
        raise StateContextError("label target missing")
    if target.get("decision") == "none":
        return "none"
    tool = target.get("tool_id")
    if target.get("decision") != "handover" or tool not in TOOL_ID_SET:
        raise StateContextError("unsupported label target")
    return str(tool)


def context_key(context: Mapping[str, Any], depth: int) -> tuple[Any, ...]:
    phase = str(context["phase_id"])
    history = tuple(str(value) for value in context.get("recent_incoming_tools", []))
    if depth == 2:
        return phase, history[-2:]
    if depth == 1:
        return phase, history[-1:]
    if depth == 0:
        return (phase,)
    return tuple()


def build_pattern_library(
    calibration_rows: Iterable[Mapping[str, Any]],
    calibration_labels: Mapping[str, Mapping[str, Any]],
    contexts: Mapping[str, Mapping[str, Any]],
    *,
    excluded_case: str | None,
) -> dict[int, dict[tuple[Any, ...], Counter[str]]]:
    table: dict[int, dict[tuple[Any, ...], Counter[str]]] = {
        2: defaultdict(Counter),
        1: defaultdict(Counter),
        0: defaultdict(Counter),
        -1: defaultdict(Counter),
    }
    for row in calibration_rows:
        example_id = str(row["example_id"])
        case_id, _cutoff = input_provenance(row)
        if excluded_case is not None and case_id == excluded_case:
            continue
        context = contexts[example_id]
        token = outcome_token(calibration_labels[example_id])
        for depth in (2, 1, 0):
            table[depth][context_key(context, depth)][token] += 1
        table[-1][tuple()][token] += 1
    if not table[-1][tuple()]:
        raise StateContextError("calibration pattern has no training rows")
    return table


def candidate_distribution(context: Mapping[str, Any], table: Mapping[int, Mapping[tuple[Any, ...], Counter[str]]]) -> dict[str, Any]:
    labels = {
        2: "phase + two most recent incoming tools",
        1: "phase + most recent incoming tool",
        0: "phase only",
        -1: "all calibration rows",
    }
    for depth in (2, 1, 0, -1):
        key = context_key(context, depth)
        counts = table[depth].get(key)
        if counts:
            total = sum(counts.values())
            ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
            return {
                "matching_rule": labels[depth],
                "support": total,
                "outcomes": [
                    {"outcome": outcome, "count": count, "rate": round(count / total, 6)}
                    for outcome, count in ranked
                ],
            }
    raise StateContextError("no transition distribution available")


def state_user_text(
    context: Mapping[str, Any],
    protocol: Mapping[str, Any],
    variant: str,
    distribution: Mapping[str, Any] | None,
) -> str:
    phase_id = str(context["phase_id"])
    phase_transitions = list(protocol["phase_transitions"].get(phase_id, []))
    # The complete protocol paths are intentionally shown: this is the
    # user-requested explicit procedure-pattern condition, not visual evidence.
    supplied = {
        "procedure": "Open Thyroidectomy Demonstration",
        "current_functional_phase": phase_id,
        "surgeon_state_source": "event-sourced last-known inventory; it is not a visual claim",
        "event_sourced_surgeon_owned": context["event_sourced_surgeon_owned"],
        "last_incoming_tool": context["last_incoming_tool"],
        "recent_incoming_tools": context["recent_incoming_tools"],
        "authored_protocol_exchange_paths": protocol["handover_paths"],
        "authored_phase_conditioned_transitions": phase_transitions,
    }
    if variant == "procedure_pattern_v2_calibration":
        if distribution is None:
            raise StateContextError("v2 requires a calibration-only distribution")
        supplied["cross_case_calibration_transition_prior"] = distribution
        decision_policy = (
            "For this v2 condition, treat cross_case_calibration_transition_prior as the "
            "quantitative decision rule: choose its first (highest-count) outcome, including "
            "none. Do not replace a top none outcome with a guessed tool. Only return uncertain "
            "if the supplied state itself is malformed or contradictory."
        )
    elif variant == "procedure_pattern_v3_authored_state_only":
        decision_policy = (
            "This is an information-only forecast: no visual or audio confirmation will arrive. "
            "Use the supplied current phase, surgeon-held state, authored phase transition, and "
            "authored exchange paths as the complete decision basis. When the state matches a "
            "phase-conditioned current-to-next relation, choose that next tool. Otherwise use the "
            "most coherent next tool in an authored exchange path that follows the recent incoming "
            "history. Do not require visual confirmation, a visible request, or an inventory claim. "
            "Return none only when the supplied structured state makes a near-term additional "
            "handover unsupported; do not return none merely because images or ASR are absent."
        )
    elif variant != "procedure_pattern_v1":
        raise StateContextError(f"unknown state prompt variant: {variant}")
    else:
        decision_policy = (
            "Use the authored exchange paths only as a procedure prior. They are alternatives, "
            "not a fixed timeline."
        )
    return (
        "No images and no ASR are supplied in this request. The following is an externally "
        "supplied structured state for an evaluation-only thyroidectomy demonstration. "
        "It is causally clipped at the decision point, but it is not independently visual "
        "evidence. Do not invent missing evidence or an unlisted tool. Predict the first new "
        "scrub-nurse-to-surgeon handover 2 to 8 seconds after that point. A path is a prior, "
        "not a mandatory sequence; select none when the supplied state/pattern does not support "
        "a near-term additional handover.\n\nSUPPLIED_STATE_JSON:\n"
        + json.dumps(supplied, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n\nDECISION_POLICY:\n"
        + decision_policy
    )


def prompts() -> tuple[str, str]:
    system = (
        "You forecast one task only: the next additional scrub-nurse-to-surgeon instrument "
        "handover, 2 to 8 seconds after the supplied decision state. This request deliberately "
        "contains no images and no ASR. Use only the supplied structured phase, event-sourced "
        "last-known surgeon state, and thyroidectomy-demo exchange priors. Do not infer case "
        "identity, time, annotations, labels, or unprovided visual facts. Allowed tool_id values "
        "are: " + ", ".join(sorted(TOOL_ID_SET)) + "."
    )
    developer = (
        "Return exactly one JSON object and no markdown or explanation. The exact keys are "
        "decision, tool_id, confidence, uncertainty. decision is handover, none, or uncertain. "
        "tool_id is exactly one allowed canonical ID only for handover and is otherwise the empty "
        "string. confidence and uncertainty are finite numbers in [0,1]. Treat a missing or "
        "conflicting state as uncertain or none, never as a forced tool guess."
    )
    return system, developer


def build_messages(user_text: str) -> list[dict[str, Any]]:
    system, developer = prompts()
    messages = [
        {"role": "system", "content": system + "\n\nOUTPUT CONTRACT:\n" + developer},
        {"role": "user", "content": user_text},
    ]
    rendered = canonical_json(messages)
    forbidden = ("case_id", "cutoff", "frame", "target", "ground_truth", "annotation_manifest", "ASR", "image")
    # The word "images" occurs in the instructional preamble so validate only
    # provenance/label leakage here; source IDs/paths are checked independently.
    forbidden = tuple(item for item in forbidden if item not in {"ASR", "image"})
    if any(token in rendered for token in forbidden):
        raise StateContextError("label/provenance token leaked into model message")
    return messages


def deterministic_prediction(distribution: Mapping[str, Any]) -> dict[str, Any]:
    outcomes = distribution.get("outcomes")
    if not isinstance(outcomes, list) or not outcomes or not isinstance(outcomes[0], dict):
        raise StateContextError("invalid deterministic distribution")
    token = str(outcomes[0].get("outcome", ""))
    if token == "none":
        return {"decision": "none", "tool_id": "", "confidence": 1.0, "uncertainty": 0.0}
    if token not in TOOL_ID_SET:
        raise StateContextError("deterministic prior output is not an allowed tool")
    return {"decision": "handover", "tool_id": token, "confidence": 1.0, "uncertainty": 0.0}


def make_prepared(
    *,
    selected: list[dict[str, Any]],
    calibration_rows: list[dict[str, Any]],
    calibration_labels: Mapping[str, Mapping[str, Any]],
    all_contexts: Mapping[str, Mapping[str, Any]],
    protocol: Mapping[str, Any],
    split: str,
    variant: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    libraries: dict[str | None, dict[int, dict[tuple[Any, ...], Counter[str]]]] = {}

    def library_for(case_id: str) -> dict[int, dict[tuple[Any, ...], Counter[str]]]:
        # The evaluation-only learned prior is case-disjoint for calibration;
        # it uses the complete 0704_6--14 set for the external holdout.
        excluded = case_id if split == CALIBRATION_SPLIT else None
        if excluded not in libraries:
            libraries[excluded] = build_pattern_library(
                calibration_rows, calibration_labels, all_contexts, excluded_case=excluded
            )
        return libraries[excluded]

    prepared: list[dict[str, Any]] = []
    deterministic_rows: list[dict[str, Any]] = []
    context_artifact: list[dict[str, Any]] = []
    for sequence, row in enumerate(selected, 1):
        example_id = str(row["example_id"])
        case_id, _cutoff = input_provenance(row)
        context = all_contexts[example_id]
        distribution = candidate_distribution(context, library_for(case_id))
        sent_distribution = distribution if variant == "procedure_pattern_v2_calibration" else None
        text = state_user_text(context, protocol, variant, sent_distribution)
        messages = build_messages(text)
        prepared.append(
            {
                "sequence": sequence,
                "example_id": example_id,
                "messages": messages,
                "request_digest": hashlib.sha256(canonical_json(messages).encode("utf-8")).hexdigest(),
            }
        )
        deterministic_rows.append(
            {
                "example_id": example_id,
                "prediction": deterministic_prediction(distribution),
            }
        )
        # ``example_id`` remains local for matching only.  The context object
        # has no case/time/label fields and is exactly what informs user text.
        context_artifact.append(
            {
                "example_id": example_id,
                "model_context": {
                    "phase_id": context["phase_id"],
                    "event_sourced_surgeon_owned": context["event_sourced_surgeon_owned"],
                    "last_incoming_tool": context["last_incoming_tool"],
                    "recent_incoming_tools": context["recent_incoming_tools"],
                    "distribution": sent_distribution,
                },
                "request_digest": prepared[-1]["request_digest"],
            }
        )
    return prepared, deterministic_rows, {
        "context_rows": context_artifact,
        "library_scope": (
            "leave-one-case-out 0704_6-14" if split == CALIBRATION_SPLIT else "0704_6-14 only"
        ),
    }


def state_summary(rows: list[dict[str, Any]], threshold: float) -> dict[str, Any]:
    return summarize(rows, threshold)


def selection_config(args: argparse.Namespace, protocol: Mapping[str, Any]) -> dict[str, Any]:
    """Return the exact configuration that a holdout selection must pin."""

    system, developer = prompts()
    return {
        "variant": args.variant,
        "model": args.model,
        "input_contract": {
            "images": "absent",
            "asr": "absent",
            "externally_supplied_phase": "provisional_context_only",
            "externally_supplied_current_tool_state": "event_sourced_last_known_not_visual",
            "authored_procedure_pattern": "thyroidectomy_demo",
            "calibration_transition_prior": args.variant == "procedure_pattern_v2_calibration",
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


def validate_frozen_selection(
    *,
    path: Path,
    args: argparse.Namespace,
    output_dir: Path,
    selected: list[dict[str, Any]],
    benchmark_files: Mapping[str, str],
    protocol: Mapping[str, Any],
) -> dict[str, Any]:
    """Fail closed if a final run drifts after calibration selection."""

    resolved = path.resolve()
    root = RUNS_ROOT.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise StateContextError("frozen selection must be under the run root") from exc
    lock = read_json(resolved)
    if lock.get("schema") != "taskplanner.next_tool_forecast_state_context_selection.v1":
        raise StateContextError("unexpected frozen selection schema")
    if lock.get("candidate_status") != "selected_for_one_final_holdout_run":
        raise StateContextError("frozen selection does not authorize a final run")
    expected_config = selection_config(args, protocol)
    if lock.get("frozen_config") != expected_config:
        raise StateContextError("frozen selection configuration does not match this invocation")
    target = lock.get("final_holdout")
    if not isinstance(target, dict):
        raise StateContextError("frozen selection final-holdout target missing")
    actual_ids = [str(row["example_id"]) for row in selected]
    actual_ids_sha = hashlib.sha256(canonical_json(actual_ids).encode("utf-8")).hexdigest()
    expected = {
        "output_dir": str(output_dir),
        "inputs_sha256": benchmark_files["inputs_sha256"],
        "labels_sha256": benchmark_files["labels_sha256"],
        "selected_example_ids_sha256": actual_ids_sha,
        "example_count": len(actual_ids),
    }
    if target != expected:
        raise StateContextError("frozen selection target does not match the holdout manifest")
    return {
        "selection_path": str(resolved),
        "selection_sha256": sha256_file(resolved),
        "frozen_config_sha256": hashlib.sha256(canonical_json(expected_config).encode("utf-8")).hexdigest(),
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    if not 1 <= args.batch_size <= 3:
        raise StateContextError("--batch-size must be 1..3")
    if not 0.0 <= args.threshold <= 1.0:
        raise StateContextError("--threshold must be in [0,1]")
    if not args.lock_path.is_absolute():
        raise StateContextError("--lock-path must be absolute")
    output_dir = ensure_output_dir(args.output_dir)
    calibration_rows, calibration_labels, calibration_files = load_benchmark(
        args.calibration_dir, CALIBRATION_SPLIT
    )
    selected, labels, benchmark_files = load_benchmark(args.benchmark_dir, args.split)
    if args.split == CALIBRATION_SPLIT:
        calibration_ids = {str(row["example_id"]) for row in calibration_rows}
        selected_ids = {str(row["example_id"]) for row in selected}
        if calibration_ids != selected_ids:
            raise StateContextError("calibration evaluation must use the frozen calibration IDs")
    protocol = compact_protocol()
    if args.split == FINAL_HOLDOUT_SPLIT and args.frozen_selection is None:
        raise StateContextError("final holdout requires --frozen-selection")
    if args.split != FINAL_HOLDOUT_SPLIT and args.frozen_selection is not None:
        raise StateContextError("--frozen-selection is only valid for final holdout")
    frozen_selection = (
        validate_frozen_selection(
            path=args.frozen_selection,
            args=args,
            output_dir=output_dir,
            selected=selected,
            benchmark_files=benchmark_files,
            protocol=protocol,
        )
        if args.frozen_selection is not None
        else None
    )
    # Contexts are created for calibration + evaluation rows before any label is
    # used to build a learned prior.  This cleanly separates causal state
    # reconstruction from target lookup.
    all_rows = calibration_rows + [
        row for row in selected if str(row["example_id"]) not in {str(item["example_id"]) for item in calibration_rows}
    ]
    all_contexts, source_integrity = build_contexts(all_rows, protocol)
    prepared, deterministic_unjoined, context_bundle = make_prepared(
        selected=selected,
        calibration_rows=calibration_rows,
        calibration_labels=calibration_labels,
        all_contexts=all_contexts,
        protocol=protocol,
        split=args.split,
        variant=args.variant,
    )
    if not prepared:
        raise StateContextError("no prepared state-context requests")
    output_dir.mkdir(parents=True, exist_ok=False)
    context_path = output_dir / "input_contexts.jsonl"
    write_jsonl(context_path, context_bundle["context_rows"])

    system, developer = prompts()
    common = {
        "schema": SCHEMA,
        "experiment_kind": "evaluation_only_oracle_state_ablation",
        "interpretation": (
            "No image or ASR was sent. Provisional phase and offline event-sourced state are "
            "externally supplied evaluation context; this is not pure VLM or deployable-runtime performance."
        ),
        "variant": args.variant,
        "model": args.model,
        "input_contract": {
            "images": "absent",
            "asr": "absent",
            "externally_supplied_phase": "provisional_context_only",
            "externally_supplied_current_tool_state": "event_sourced_last_known_not_visual",
            "authored_procedure_pattern": "thyroidectomy_demo",
            "calibration_transition_prior": args.variant == "procedure_pattern_v2_calibration",
        },
        "prompt_sha256": {
            "system": hashlib.sha256(system.encode("utf-8")).hexdigest(),
            "developer": hashlib.sha256(developer.encode("utf-8")).hexdigest(),
            "procedure_spec": str(protocol["source_sha256"]),
        },
        "benchmark": {
            "split": args.split,
            "selected_example_ids": [str(row["example_id"]) for row in selected],
            "files": benchmark_files,
            "calibration_files": calibration_files,
            "context_input_sha256": sha256_file(context_path),
            "transition_prior_scope": context_bundle["library_scope"],
        },
        "source_integrity": source_integrity,
        "generation": {
            "temperature": args.temperature,
            "top_p": args.top_p,
            "seed": args.seed,
            "max_tokens": args.max_tokens,
            "enable_thinking": False,
            "threshold": args.threshold,
        },
        "execution_guard": {
            "serialized_lock_path": str(args.lock_path),
            "batch_size": args.batch_size,
            "manager_reload_before_each_batch": True,
            "manager_loaded_vision_check": True,
            "direct_worker_catalog_check": True,
            "automatic_transport_retry": False,
        },
    }
    if frozen_selection is not None:
        common["frozen_selection"] = frozen_selection
    deterministic_by_id = {row["example_id"]: row["prediction"] for row in deterministic_unjoined}
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
    write_jsonl(output_dir / "deterministic_transition_baseline.jsonl", deterministic_rows)

    api_key = os.environ.get(args.api_key_env, "")
    result_rows: list[dict[str, Any]] = []
    batches: list[dict[str, Any]] = []
    post_count = 0
    try:
        for batch_number, offset in enumerate(range(0, len(prepared), args.batch_size), 1):
            batch = prepared[offset : offset + args.batch_size]
            record: dict[str, Any] = {
                "batch_number": batch_number,
                "example_ids": [str(item["example_id"]) for item in batch],
                "post_cap": args.batch_size,
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
                            record["status"] = "aborted_transport"
                            record["failure"] = {"example_id": example_id, "error": response_error[:500]}
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
                    record["post_batch_health"] = {
                        "manager": public_catalog_entry(manager_after),
                        "direct_worker_catalog": worker_after,
                    }
                    if not _manager_is_loaded_vision(manager_after) or not (
                        worker_after["reachable"] and worker_after["model_present"]
                    ):
                        record["status"] = "aborted_post_batch_health"
                        raise RunAborted(f"batch {batch_number} failed post-batch health proof")
                record["status"] = "completed"
                batches.append(record)
            except RunError as exc:
                if record not in batches:
                    if record.get("status") == "started":
                        record["status"] = "aborted_lifecycle"
                    record["failure_message"] = str(exc)[:500]
                    batches.append(record)
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
        summary = state_summary(result_rows, args.threshold)
        deterministic_summary = state_summary(deterministic_rows, args.threshold)
        run_document = common | {
            "execution_status": "completed",
            "post_count": post_count,
            "lifecycle_batches": batches,
            "summary": summary,
            "deterministic_transition_baseline": deterministic_summary,
            "predictions_sha256": sha256_file(predictions_path),
            "deterministic_baseline_sha256": sha256_file(output_dir / "deterministic_transition_baseline.jsonl"),
        }
        (output_dir / "run.json").write_text(
            json.dumps(run_document, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        return {"aborted": False, "output_dir": str(output_dir), "run": run_document}
    except Exception:
        # Never leave an incomplete non-abort run looking valid.
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
    overall = result["run"]["summary"]["overall"]
    print(json.dumps({"output_dir": result["output_dir"], "overall": overall}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
