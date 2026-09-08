#!/usr/bin/env python3
"""Exhaustively audit the DT-owned thyroidectomy n-gram tool policy.

This is a read-only analysis aid.  It enumerates every stable location
combination for the four requestable thyroidectomy tools (tray, Mayo, surgeon)
against every distinct lookup context in the frozen 0704 handover n-gram
asset.  It models only automatic policy after its DT dwell requirement has
already been satisfied:

* identify the highest-probability handover-capable preparation and the
  lowest-probability Mayo recovery independently;
* resolve simultaneous eligibility by comparing preparation ``p`` with
  recovery ``1-p``, using the same DT arbitration as the runtime; or
* wait.

Explicit voice requests, validated direct-hand requests, active robot Actions,
cleaning, safety flags, and CAM4 Mayo occupancy are intentionally external to
the location-only enumeration.  ``--mayo-hand-present`` models the autonomous
Mayo block; the narrow explicit-request exception is not automatic policy.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from itertools import product
from pathlib import Path
from typing import Iterable

from procedure_spec import load_bundle, load_frozen_handover_ngram_prior
from or_digital_twin.ngram_policy import arbitrate_ngram_intent


LOCATIONS = ("tray", "mayo", "surgeon")
DEFAULT_SPEC_DIR = (
    Path(__file__).resolve().parents[3]
    / "src/procedure_spec/procedure_spec/specs/thyroidectomy_demo"
)


@dataclass(frozen=True)
class Context:
    phase_id: str
    history: tuple[str, ...]
    match: str
    support: int
    probabilities: dict[str, float]


@dataclass(frozen=True)
class Decision:
    action: str
    tool_id: str = ""


def location_states(tool_ids: tuple[str, ...]) -> Iterable[dict[str, str]]:
    for values in product(LOCATIONS, repeat=len(tool_ids)):
        yield dict(zip(tool_ids, values, strict=True))


def contexts(prior, tool_ids: tuple[str, ...]) -> list[Context]:
    """Return one probe per distinct most-specific frozen lookup result."""

    result: list[Context] = []
    seen: set[tuple[str, tuple[str, ...], str]] = set()
    for match, phase_id, history in prior._lookup:  # offline audit only
        probe_phase = (
            "" if match == "global" else phase_id if match.startswith("phase") else "P03"
        )
        prediction = prior.predict(
            phase_id=probe_phase,
            completed_handovers=history,
        )
        if prediction is None:
            continue
        key = (
            probe_phase,
            tuple(history),
            str(prediction["match"]),
        )
        if key in seen:
            continue
        seen.add(key)
        probabilities = {tool_id: 0.0 for tool_id in tool_ids}
        probabilities.update(
            {
                str(tool_id): float(probability)
                for tool_id, probability in prediction["candidates"]
                if str(tool_id) in probabilities
            }
        )
        result.append(
            Context(
                phase_id=probe_phase,
                history=tuple(history),
                match=str(prediction["match"]),
                support=int(prediction["support"]),
                probabilities=probabilities,
            )
        )
    return result


def decide(
    locations: dict[str, str],
    probabilities: dict[str, float],
    tool_ids: tuple[str, ...],
    *,
    prepare_threshold: float,
    recovery_threshold: float,
    recovery_enabled_tools: frozenset[str],
    mayo_hand_present: bool,
) -> Decision:
    """Mirror DT policy order after probability dwell is already satisfied."""

    recovery_tool = ""
    if not mayo_hand_present:
        recoverable = [
            tool_id
            for tool_id in tool_ids
            if tool_id in recovery_enabled_tools
            and locations[tool_id] == "mayo"
            and probabilities[tool_id] <= recovery_threshold
        ]
        if recoverable:
            recovery_tool = min(
                recoverable,
                key=lambda tool_id: (
                    probabilities[tool_id],
                    tool_ids.index(tool_id),
                ),
            )

    available = [
        tool_id
        for tool_id in tool_ids
        if locations[tool_id] in {"tray", "mayo"}
    ]
    preparation_tool = ""
    if available:
        selected = max(
            available,
            key=lambda tool_id: (
                probabilities[tool_id],
                -tool_ids.index(tool_id),
            ),
        )
        if probabilities[selected] >= prepare_threshold:
            # The runtime source chooser still prefers Mayo over tray for the
            # same tool; this census counts the type-level preparation branch.
            preparation_tool = selected

    intent = arbitrate_ngram_intent(
        preparation_tool_id=preparation_tool,
        recovery_tool_id=recovery_tool,
        preparation_probability=(
            probabilities[preparation_tool] if preparation_tool else 0.0
        ),
        recovery_probability=(
            probabilities[recovery_tool] if recovery_tool else 1.0
        ),
    )
    return Decision(intent.action, intent.tool_id)


def summarize(
    context_rows: list[Context],
    tool_ids: tuple[str, ...],
    *,
    prepare_threshold: float,
    recovery_threshold: float,
    recovery_enabled_tools: frozenset[str],
    mayo_hand_present: bool,
) -> tuple[Counter[str], Counter[tuple[str, str]], int]:
    actions: Counter[str] = Counter()
    by_tool: Counter[tuple[str, str]] = Counter()
    conflicts = 0
    for context in context_rows:
        for locations in location_states(tool_ids):
            preparation_exists = any(
                locations[tool_id] in {"tray", "mayo"}
                and context.probabilities[tool_id] >= prepare_threshold
                for tool_id in tool_ids
            )
            recovery_exists = bool(
                not mayo_hand_present
                and any(
                    locations[tool_id] == "mayo"
                    and tool_id in recovery_enabled_tools
                    and context.probabilities[tool_id] <= recovery_threshold
                    for tool_id in tool_ids
                )
            )
            if preparation_exists and recovery_exists:
                conflicts += 1
            decision = decide(
                locations,
                context.probabilities,
                tool_ids,
                prepare_threshold=prepare_threshold,
                recovery_threshold=recovery_threshold,
                recovery_enabled_tools=recovery_enabled_tools,
                mayo_hand_present=mayo_hand_present,
            )
            actions[decision.action] += 1
            if decision.tool_id:
                by_tool[(decision.action, decision.tool_id)] += 1
    return actions, by_tool, conflicts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec-dir", type=Path, default=DEFAULT_SPEC_DIR)
    parser.add_argument("--prepare-threshold", type=float, default=0.125)
    parser.add_argument("--recovery-threshold", type=float, default=0.391)
    parser.add_argument(
        "--recovery-enabled-tools",
        nargs="+",
        default=["T02", "T08"],
    )
    parser.add_argument("--mayo-hand-present", action="store_true")
    args = parser.parse_args()

    spec = load_bundle(args.spec_dir)
    prior = load_frozen_handover_ngram_prior(spec, args.spec_dir)
    if prior is None:
        raise RuntimeError("thyroidectomy bundle has no frozen handover n-gram")
    tool_ids = tuple(spec.list_requestable_instrument_ids())
    context_rows = contexts(prior, tool_ids)
    recovery_enabled_tools = frozenset(args.recovery_enabled_tools)
    unknown_enabled_tools = sorted(recovery_enabled_tools - set(tool_ids))
    if unknown_enabled_tools:
        raise ValueError(
            "unknown recovery-enabled tools: "
            + ", ".join(unknown_enabled_tools)
        )
    actions, by_tool, conflicts = summarize(
        context_rows,
        tool_ids,
        prepare_threshold=args.prepare_threshold,
        recovery_threshold=args.recovery_threshold,
        recovery_enabled_tools=recovery_enabled_tools,
        mayo_hand_present=args.mayo_hand_present,
    )
    total = len(context_rows) * len(LOCATIONS) ** len(tool_ids)

    print(
        f"contexts={len(context_rows)} locations={len(LOCATIONS) ** len(tool_ids)} "
        f"total={total} prepare>={args.prepare_threshold:.3f} "
        f"recover<={args.recovery_threshold:.3f} "
        f"recovery_enabled={','.join(sorted(recovery_enabled_tools))}"
    )
    print("| Automatic outcome | Count | Share |")
    print("|---|---:|---:|")
    for action in ("prepare", "recover", "wait"):
        count = actions[action]
        print(f"| {action} | {count} | {count / total * 100:.1f}% |")
    print(
        f"\nconflicts={conflicts} ({conflicts / total * 100:.1f}%) "
        "arbitration=max(prepare_p,recover_1_minus_p);tie=prepare"
    )
    print("\n| Tool | Prepare | Recover |")
    print("|---|---:|---:|")
    for tool_id in tool_ids:
        print(
            f"| {tool_id} | {by_tool[('prepare', tool_id)]} | "
            f"{by_tool[('recover', tool_id)]} |"
        )
    print("\n| Context | Match/support | " + " | ".join(tool_ids) + " |")
    print("|---|---|" + "---|" * len(tool_ids))
    for context in context_rows:
        label = f"{context.phase_id or 'global'} {list(context.history)}"
        probabilities = " | ".join(
            f"{context.probabilities[tool_id]:.3f}" for tool_id in tool_ids
        )
        print(
            f"| {label} | {context.match}/{context.support} | {probabilities} |"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
