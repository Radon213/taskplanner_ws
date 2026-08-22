#!/usr/bin/env python3
"""Build the immutable n-gram prompt prior used by the live thyroid demo.

This is an offline-only artifact builder.  It aggregates reviewed,
scrub-nurse-to-surgeon handover transitions from the 0704 calibration cases
into a procedure-level table.  The live VLM node reads only the generated
table once at startup and exposes only a selected match, support, and
probabilities; it never receives source cases, timestamps, labels, or counts.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
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
DEFAULT_OUTPUT_PATH = PROCEDURE_PROMPT_PATH.with_name(
    "tool_handover_ngram_prior.yaml"
)
SCHEMA = "taskplanner.frozen_handover_ngram_prior.v1"
ARTIFACT_ID = "thyroidectomy_demo_handover_ngram_calibration_v1"

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


class RuntimeNgramPriorBuildError(RuntimeError):
    """Raised when the reviewed calibration source cannot form a safe asset."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail unless --output already equals the deterministic generated asset",
    )
    return parser.parse_args()


def _runtime_tool_mapping() -> dict[str, str]:
    """Map observable-tool catalog IDs to runtime Txx IDs for this bundle."""

    prompt = load_yaml(PROCEDURE_PROMPT_PATH)
    raw_tools = prompt.get("tools")
    if not isinstance(raw_tools, Mapping):
        raise RuntimeNgramPriorBuildError("procedure prompt has no tools mapping")
    runtime_ids = {str(tool_id) for tool_id in raw_tools}
    refs = tool_ref_mapping()  # Runtime Txx -> observable catalog ID.
    result = {
        observable_id: runtime_id
        for runtime_id, observable_id in refs.items()
        if runtime_id in runtime_ids
    }
    if not result:
        raise RuntimeNgramPriorBuildError("no observable-to-runtime tool mapping")
    return result


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


def build_payload(
    cases: Iterable[str] = DEVELOPMENT_CASES,
) -> dict[str, Any]:
    """Build a phase-and-suffix count table from the fixed calibration split."""

    runtime_by_observable = _runtime_tool_mapping()
    protocol = compact_protocol()
    counters: dict[tuple[str, str, tuple[str, ...]], Counter[str]] = defaultdict(
        Counter
    )
    eligible_transition_count = 0
    unsupported_transition_count = 0

    for case_id in cases:
        source = source_for_case(case_id)
        handovers = confirmed_handover_events(source["events"])
        if not handovers:
            raise RuntimeNgramPriorBuildError(
                f"{case_id}: no confirmed handover events"
            )

        # Only a contiguous suffix of runtime-supported transfers is safe to
        # match.  A physical transfer outside the demo's candidate inventory
        # resets the suffix instead of being silently removed and joining two
        # unrelated handovers together.
        runtime_history: list[str] = []
        for index, event in enumerate(handovers):
            runtime_tool = runtime_by_observable.get(str(event.get("tool", "")), "")
            cutoff_sec = (
                0.0
                if index == 0
                else float(handovers[index - 1]["time_sec"])
            )
            phase_id = current_phase(
                source["phases"],
                cutoff_sec,
                str(protocol["default_phase_id"]),
            )
            if runtime_tool:
                for match, uses_phase, depth in MATCH_RULES:
                    key = _rule_key(
                        match=match,
                        uses_phase=uses_phase,
                        depth=depth,
                        phase_id=phase_id,
                        history=runtime_history,
                    )
                    counters[key][runtime_tool] += 1
                eligible_transition_count += 1
                runtime_history.append(runtime_tool)
            else:
                unsupported_transition_count += 1
                runtime_history.clear()

    if not eligible_transition_count:
        raise RuntimeNgramPriorBuildError("no runtime-supported calibration transitions")

    rules: list[dict[str, Any]] = []
    for match, uses_phase, _depth in MATCH_RULES:
        matching_keys = [key for key in counters if key[0] == match]
        for _match, phase_id, history in sorted(
            matching_keys,
            key=lambda key: (key[1], key[2]),
        ):
            outcomes = counters[(match, phase_id, history)]
            rule: dict[str, Any] = {"match": match}
            if uses_phase:
                rule["phase"] = phase_id
            if history:
                rule["history"] = list(history)
            else:
                rule["history"] = []
            rule["outcomes"] = [
                {"tool": tool_id, "count": count}
                for tool_id, count in sorted(
                    outcomes.items(), key=lambda item: (-item[1], item[0])
                )
            ]
            rules.append(rule)

    return {
        "schema": SCHEMA,
        "id": ARTIFACT_ID,
        "procedure_id": "thyroidectomy_demo",
        "target": (
            "first_subsequent_supported_scrub_nurse_to_surgeon_handover_"
            "regardless_of_elapsed_time"
        ),
        "metadata": {
            "fit_partition": "development_calibration",
            "fit_source": "reviewed_confirmed_scrub_nurse_to_surgeon_transitions",
            "eligible_transition_count": eligible_transition_count,
            "unsupported_transition_count": unsupported_transition_count,
            "history_boundary": "unsupported_or_unknown_handover_resets_suffix",
            "model_visible_fields": ["id", "match", "support", "candidates"],
            "runtime_role": "advisory_prompt_prior_only",
        },
        "rules": rules,
    }


def render_payload(payload: Mapping[str, Any]) -> str:
    header = (
        "# Generated by tools/prompt_optimization/next_tool_forecast/"
        "build_runtime_ngram_prior.py.\n"
        "# Runtime loads this once; only aggregated candidates are sent to the VLM.\n"
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
            raise RuntimeNgramPriorBuildError(
                f"cannot read generated asset {output}: {exc}"
            ) from exc
        if actual != rendered:
            raise RuntimeNgramPriorBuildError(
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
    except RuntimeNgramPriorBuildError as exc:
        raise SystemExit(f"error: {exc}") from exc
