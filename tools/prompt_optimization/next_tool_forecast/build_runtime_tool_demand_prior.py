#!/usr/bin/env python3
"""Build the frozen thyroid-demo future tool-demand prior offline.

The generated asset estimates, for every supported tool type,
``P(at least one later confirmed scrub-nurse-to-surgeon handover before the
procedure ends | functional phase, completed supported-handover suffix)``.
It is intentionally a historical demand prior only: camera detection, Mayo
placement, tool availability, and robot eligibility never enter this builder.

All source events come from the reviewed 0704_6--14 calibration references.
The runtime asset contains only aggregate support and Laplace-smoothed
probabilities, never a case identifier, event timestamp, or raw review row.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from next_event_state_eval import DEVELOPMENT_CASES, confirmed_handover_events
from state_context_eval import (
    compact_protocol,
    current_phase,
    load_yaml,
    source_for_case,
    tool_ref_mapping,
)


TASK_DIR = Path(__file__).resolve().parent
REPO_ROOT = TASK_DIR.parents[2]
PROCEDURE_PROMPT_PATH = (
    REPO_ROOT
    / "src/procedure_spec/procedure_spec/specs/thyroidectomy_demo"
    / "vlm_procedure_prompt.yaml"
)
DEFAULT_OUTPUT_PATH = PROCEDURE_PROMPT_PATH.with_name("tool_future_demand_prior.yaml")
SCHEMA = "taskplanner.frozen_tool_future_demand_prior.v1"
ARTIFACT_ID = "thyroidectomy_demo_future_tool_demand_calibration_v1"
TARGET = (
    "at_least_one_future_supported_scrub_nurse_to_surgeon_handover_"
    "before_procedure_end"
)
SMOOTHING_METHOD = "beta_binomial_laplace"
SMOOTHING_ALPHA = 1.0
SMOOTHING_BETA = 1.0

MATCH_RULES: tuple[tuple[str, bool, int], ...] = (
    ("phase+last3", True, 3),
    ("phase+last2", True, 2),
    ("phase+last1", True, 1),
    ("phase", True, 0),
    ("last3", False, 3),
    ("last2", False, 2),
    ("last1", False, 1),
    ("global", False, 0),
)


class RuntimeToolDemandPriorBuildError(RuntimeError):
    """Raised when reviewed calibration records cannot form the frozen asset."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail unless --output equals the deterministic generated asset",
    )
    return parser.parse_args()


def _requestable_runtime_ids(prompt: Mapping[str, Any]) -> tuple[str, ...]:
    raw_tools = prompt.get("tools")
    if not isinstance(raw_tools, Mapping):
        raise RuntimeToolDemandPriorBuildError("procedure prompt has no tools mapping")
    policy = prompt.get("scenario_policy")
    raw_requestable = (
        policy.get("requestable_tools") if isinstance(policy, Mapping) else None
    )
    if not isinstance(raw_requestable, list):
        # The older prompt layout kept this editable list at top level.  The
        # generated semantics remain identical, so accepting it keeps the
        # offline tool usable for a one-time scenario migration.
        raw_requestable = prompt.get("requestable_tools")
    if not isinstance(raw_requestable, list):
        raise RuntimeToolDemandPriorBuildError(
            "procedure prompt has no requestable_tools list"
        )
    requestable_ids = tuple(sorted({str(item).strip() for item in raw_requestable}))
    if not requestable_ids or "" in requestable_ids:
        raise RuntimeToolDemandPriorBuildError(
            "procedure prompt requestable_tools must contain runtime IDs"
        )
    unknown_requestable = sorted(set(requestable_ids) - {str(key) for key in raw_tools})
    if unknown_requestable:
        raise RuntimeToolDemandPriorBuildError(
            "procedure prompt requestable_tools contains unknown runtime IDs: "
            + ", ".join(unknown_requestable)
        )
    return requestable_ids


def _runtime_tool_mapping() -> tuple[tuple[str, ...], dict[str, str]]:
    """Return requestable runtime IDs and observable-to-runtime conversion."""

    prompt = load_yaml(PROCEDURE_PROMPT_PATH)
    requestable_ids = _requestable_runtime_ids(prompt)
    refs = tool_ref_mapping()  # Runtime Txx -> reviewed observable tool ID.
    runtime_by_observable = {
        observable_id: runtime_id
        for runtime_id, observable_id in refs.items()
        if runtime_id in set(requestable_ids)
    }
    if set(requestable_ids) - set(runtime_by_observable.values()):
        raise RuntimeToolDemandPriorBuildError(
            "one or more requestable tools have no observable catalog mapping"
        )
    return requestable_ids, runtime_by_observable


def _rule_key(
    *,
    match: str,
    uses_phase: bool,
    depth: int,
    phase_id: str,
    history: list[str],
) -> tuple[str, str, tuple[str, ...]]:
    return (
        match,
        phase_id if uses_phase else "",
        tuple(history[-depth:]) if depth else tuple(),
    )


def _future_supported_sets(runtime_events: Iterable[str]) -> list[frozenset[str]]:
    """Return the supported future-demand label for every pre-event state."""

    rows = list(runtime_events)
    result: list[frozenset[str]] = [frozenset() for _ in range(len(rows) + 1)]
    future: set[str] = set()
    for index in range(len(rows) - 1, -1, -1):
        if rows[index]:
            future.add(rows[index])
        result[index] = frozenset(future)
    return result


def _smoothed_probability(successes: int, support: int) -> float:
    return round(
        (successes + SMOOTHING_ALPHA)
        / (support + SMOOTHING_ALPHA + SMOOTHING_BETA),
        4,
    )


def build_payload(
    cases: Iterable[str] = DEVELOPMENT_CASES,
) -> dict[str, Any]:
    """Build phase/suffix-conditioned future-demand probabilities.

    Each reviewed handover contributes one causal pre-handover state.  A final
    terminal state after the last reviewed handover contributes the all-false
    label, which prevents every supported tool from appearing perpetually
    reusable.  Unsupported handovers reset the suffix but remain part of the
    temporal sequence; they do not become candidate tool types.
    """

    requestable_ids, runtime_by_observable = _runtime_tool_mapping()
    protocol = compact_protocol()
    counters: dict[
        tuple[str, str, tuple[str, ...]],
        dict[str, Any],
    ] = defaultdict(lambda: {"support": 0, "successes": defaultdict(int)})
    raw_handover_count = 0
    supported_handover_count = 0
    unsupported_handover_count = 0
    state_sample_count = 0

    for case_id in cases:
        source = source_for_case(case_id)
        handovers = confirmed_handover_events(source["events"])
        if not handovers:
            raise RuntimeToolDemandPriorBuildError(
                f"{case_id}: no confirmed handover events"
            )
        runtime_events = [
            runtime_by_observable.get(str(event.get("tool", "")), "")
            for event in handovers
        ]
        raw_handover_count += len(handovers)
        supported_handover_count += sum(bool(tool_id) for tool_id in runtime_events)
        unsupported_handover_count += sum(not bool(tool_id) for tool_id in runtime_events)
        future_sets = _future_supported_sets(runtime_events)
        history: list[str] = []

        # One state before every reviewed handover plus one state after the
        # last event gives this boolean demand target both positive and final
        # negative examples without using images or future runtime data.
        for index in range(len(handovers) + 1):
            cutoff_sec = 0.0 if index == 0 else float(handovers[index - 1]["time_sec"])
            phase_id = current_phase(
                source["phases"],
                cutoff_sec,
                str(protocol["default_phase_id"]),
            )
            if phase_id not in protocol["phase_transitions"]:
                raise RuntimeToolDemandPriorBuildError(
                    f"{case_id}: phase {phase_id} absent from authored protocol"
                )
            future_tools = future_sets[index]
            for match, uses_phase, depth in MATCH_RULES:
                key = _rule_key(
                    match=match,
                    uses_phase=uses_phase,
                    depth=depth,
                    phase_id=phase_id,
                    history=history,
                )
                row = counters[key]
                row["support"] += 1
                for tool_id in future_tools:
                    row["successes"][tool_id] += 1
            state_sample_count += 1

            if index == len(handovers):
                continue
            runtime_tool = runtime_events[index]
            if runtime_tool:
                history.append(runtime_tool)
            else:
                history.clear()

    if not supported_handover_count:
        raise RuntimeToolDemandPriorBuildError(
            "no runtime-supported calibration handovers"
        )

    rules: list[dict[str, Any]] = []
    for match, uses_phase, _depth in MATCH_RULES:
        matching_keys = [key for key in counters if key[0] == match]
        for _match, phase_id, history in sorted(
            matching_keys,
            key=lambda key: (key[1], key[2]),
        ):
            counter = counters[(match, phase_id, history)]
            support = int(counter["support"])
            successes = counter["successes"]
            rule: dict[str, Any] = {
                "match": match,
                "history": list(history),
                "support": support,
                "probabilities": [
                    {
                        "tool": tool_id,
                        "probability": _smoothed_probability(
                            int(successes.get(tool_id, 0)),
                            support,
                        ),
                    }
                    for tool_id in requestable_ids
                ],
            }
            if uses_phase:
                rule["phase"] = phase_id
                # Keep fields in a readable, conventional order.
                rule = {
                    "match": match,
                    "phase": phase_id,
                    "history": list(history),
                    "support": support,
                    "probabilities": rule["probabilities"],
                }
            rules.append(rule)

    return {
        "schema": SCHEMA,
        "id": ARTIFACT_ID,
        "procedure_id": "thyroidectomy_demo",
        "target": TARGET,
        "smoothing": {
            "method": SMOOTHING_METHOD,
            "alpha": SMOOTHING_ALPHA,
            "beta": SMOOTHING_BETA,
        },
        "metadata": {
            "fit_partition": "development_calibration",
            "fit_source": "reviewed_confirmed_scrub_nurse_to_surgeon_transitions",
            "conditioning": ["functional_phase", "completed_supported_handover_suffix"],
            "label": "any_future_supported_handover_before_procedure_end",
            "camera_observation_role": "excluded",
            "mayo_location_role": "excluded",
            "robot_dispatch_role": "advisory_only",
            "raw_handover_count": raw_handover_count,
            "supported_handover_count": supported_handover_count,
            "unsupported_handover_count": unsupported_handover_count,
            "state_sample_count": state_sample_count,
            "history_boundary": "unsupported_or_unknown_handover_resets_suffix",
            "runtime_fields": ["id", "match", "support", "probabilities"],
        },
        "rules": rules,
    }


def render_payload(payload: Mapping[str, Any]) -> str:
    header = (
        "# Generated by tools/prompt_optimization/next_tool_forecast/"
        "build_runtime_tool_demand_prior.py.\n"
        "# Camera/Mayo observations are excluded; this is a historical future-demand prior.\n"
    )
    return header + yaml.safe_dump(
        dict(payload),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )


def main() -> int:
    args = parse_args()
    rendered = render_payload(build_payload())
    output = args.output.resolve()
    if args.check:
        try:
            actual = output.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeToolDemandPriorBuildError(
                f"cannot read generated asset {output}: {exc}"
            ) from exc
        if actual != rendered:
            raise RuntimeToolDemandPriorBuildError(
                f"generated asset is stale: run with --output {output}"
            )
        print(f"verified {output}")
        return 0

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")
    print(f"wrote {output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeToolDemandPriorBuildError as exc:
        raise SystemExit(f"error: {exc}") from exc
