"""Frozen, phase-aware handover n-gram lookup for VLM prompt context.

The artifact is generated offline from reviewed demonstrations.  At runtime it
is loaded once with the procedure bundle and queried only from public completed
handover history plus the currently authoritative phase.  It is deliberately
advisory: this module neither changes a model result nor authorizes a robot
action.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from .query_api import ProcedureSpec


FROZEN_HANDOVER_NGRAM_PRIOR_FILENAME = "tool_handover_ngram_prior.yaml"
FROZEN_HANDOVER_NGRAM_PRIOR_SCHEMA = "taskplanner.frozen_handover_ngram_prior.v1"

# Ordered from the most specific suffix to the unconditional fallback.  The
# same priority is used in the offline artifact generator, so lookup does not
# need to inspect or score a dataset on the live path.
_MATCH_RULES: tuple[tuple[str, bool, int], ...] = (
    ("phase+last3", True, 3),
    ("phase+last2", True, 2),
    ("phase+last1", True, 1),
    ("phase", True, 0),
    ("last3", False, 3),
    ("last2", False, 2),
    ("last1", False, 1),
    ("global", False, 0),
)
_MATCH_RULE_BY_NAME = {name: (uses_phase, depth) for name, uses_phase, depth in _MATCH_RULES}


class HandoverNgramPriorError(ValueError):
    """Raised when a frozen handover-prior asset is malformed or mismatched."""


class FrozenHandoverNgramPrior:
    """Immutable O(1) n-gram lookup built from one validated YAML asset.

    ``predict`` returns only a compact, model-visible statistical summary.  It
    never returns artifact provenance, raw training rows, event timestamps, or
    unnormalized outcome counts.
    """

    def __init__(self, spec: ProcedureSpec, payload: Mapping[str, Any]) -> None:
        self._spec = spec
        self._requestable_tool_ids = {
            instrument.id
            for instrument in spec.bundle.instruments
            if bool(getattr(instrument, "requestable", True))
        }
        self._phase_ids = set(spec.phase_ids)
        self._artifact_id, self._lookup = self._validate_and_compile(payload)

    @classmethod
    def from_path(
        cls,
        spec: ProcedureSpec,
        path: str | Path,
    ) -> "FrozenHandoverNgramPrior":
        artifact_path = Path(path)
        try:
            with artifact_path.open("r", encoding="utf-8") as handle:
                payload = yaml.safe_load(handle) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise HandoverNgramPriorError(
                f"cannot read frozen handover n-gram prior {artifact_path}: {exc}"
            ) from exc
        if not isinstance(payload, Mapping):
            raise HandoverNgramPriorError(
                f"{artifact_path} must contain a YAML mapping"
            )
        return cls(spec, payload)

    @property
    def artifact_id(self) -> str:
        """Stable ID suitable for a compact runtime/model context."""

        return self._artifact_id

    def predict(
        self,
        *,
        phase_id: Any,
        completed_handovers: Iterable[Any],
    ) -> dict[str, Any] | None:
        """Return the most-specific precomputed distribution in constant time.

        An unknown handover acts as a boundary rather than being silently
        deleted and joined across.  This avoids fabricating a suffix when an
        unmodeled instrument was physically exchanged between known tools.
        """

        history = self._normalized_history(completed_handovers)
        normalized_phase = str(phase_id or "").strip()
        if normalized_phase not in self._phase_ids:
            normalized_phase = ""

        for match, uses_phase, depth in _MATCH_RULES:
            key = (
                match,
                normalized_phase if uses_phase else "",
                tuple(history[-depth:]) if depth else tuple(),
            )
            compiled = self._lookup.get(key)
            if compiled is None:
                continue
            # Copy the small fixed result so a caller cannot mutate the frozen
            # table shared by subsequent 1 Hz requests.
            return {
                "id": self._artifact_id,
                "match": match,
                "support": compiled["support"],
                "candidates": [list(row) for row in compiled["candidates"]],
            }
        return None

    def _normalized_history(self, completed_handovers: Iterable[Any]) -> list[str]:
        history: list[str] = []
        for item in completed_handovers:
            if isinstance(item, Mapping):
                raw_tool = item.get("tool", "")
            else:
                raw_tool = item
            tool_id = str(raw_tool or "").strip()
            if tool_id not in self._requestable_tool_ids:
                history.clear()
                continue
            history.append(tool_id)
        return history

    def _validate_and_compile(
        self,
        payload: Mapping[str, Any],
    ) -> tuple[str, dict[tuple[str, str, tuple[str, ...]], dict[str, Any]]]:
        if payload.get("schema") != FROZEN_HANDOVER_NGRAM_PRIOR_SCHEMA:
            raise HandoverNgramPriorError(
                "unsupported frozen handover n-gram prior schema"
            )
        artifact_id = str(payload.get("id", "")).strip()
        if not artifact_id:
            raise HandoverNgramPriorError("frozen handover n-gram prior needs id")
        if str(payload.get("procedure_id", "")).strip() != self._spec.procedure_id:
            raise HandoverNgramPriorError(
                "frozen handover n-gram prior procedure_id does not match bundle"
            )

        raw_rules = payload.get("rules")
        if not isinstance(raw_rules, list) or not raw_rules:
            raise HandoverNgramPriorError("frozen handover n-gram prior needs rules")

        lookup: dict[tuple[str, str, tuple[str, ...]], dict[str, Any]] = {}
        for index, raw_rule in enumerate(raw_rules):
            if not isinstance(raw_rule, Mapping):
                raise HandoverNgramPriorError(f"rule {index} must be a mapping")
            match = str(raw_rule.get("match", "")).strip()
            rule_spec = _MATCH_RULE_BY_NAME.get(match)
            if rule_spec is None:
                raise HandoverNgramPriorError(f"rule {index} has unsupported match")
            uses_phase, depth = rule_spec

            phase = str(raw_rule.get("phase", "")).strip()
            if uses_phase:
                if phase not in self._phase_ids:
                    raise HandoverNgramPriorError(
                        f"rule {index} has unknown phase {phase!r}"
                    )
            elif phase:
                raise HandoverNgramPriorError(
                    f"rule {index} must not specify phase for {match}"
                )

            raw_history = raw_rule.get("history", [])
            if not isinstance(raw_history, list) or len(raw_history) > depth:
                raise HandoverNgramPriorError(
                    f"rule {index} history may contain at most {depth} tools"
                )
            history = tuple(str(tool_id).strip() for tool_id in raw_history)
            if any(tool_id not in self._requestable_tool_ids for tool_id in history):
                raise HandoverNgramPriorError(
                    f"rule {index} history contains a non-requestable tool"
                )

            raw_outcomes = raw_rule.get("outcomes")
            if not isinstance(raw_outcomes, list) or not raw_outcomes:
                raise HandoverNgramPriorError(f"rule {index} needs outcomes")
            counts: dict[str, int] = {}
            for outcome in raw_outcomes:
                if not isinstance(outcome, Mapping):
                    raise HandoverNgramPriorError(
                        f"rule {index} outcome must be a mapping"
                    )
                tool_id = str(outcome.get("tool", "")).strip()
                count = outcome.get("count")
                if tool_id not in self._requestable_tool_ids:
                    raise HandoverNgramPriorError(
                        f"rule {index} outcome has a non-requestable tool"
                    )
                if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
                    raise HandoverNgramPriorError(
                        f"rule {index} outcome count must be a positive integer"
                    )
                if tool_id in counts:
                    raise HandoverNgramPriorError(
                        f"rule {index} repeats outcome tool {tool_id}"
                    )
                counts[tool_id] = count

            support = sum(counts.values())
            candidates = [
                [tool_id, round(count / support, 3)]
                for tool_id, count in sorted(
                    counts.items(), key=lambda item: (-item[1], item[0])
                )[:4]
            ]
            key = (match, phase if uses_phase else "", history)
            if key in lookup:
                raise HandoverNgramPriorError(
                    f"frozen handover n-gram prior repeats rule {match}"
                )
            lookup[key] = {"support": support, "candidates": candidates}

        if ("global", "", tuple()) not in lookup:
            raise HandoverNgramPriorError(
                "frozen handover n-gram prior needs a global fallback"
            )
        return artifact_id, lookup


def load_frozen_handover_ngram_prior(
    spec: ProcedureSpec,
    bundle_dir: str | Path,
) -> FrozenHandoverNgramPrior | None:
    """Load the optional per-procedure artifact once, or return no prior."""

    path = Path(bundle_dir) / FROZEN_HANDOVER_NGRAM_PRIOR_FILENAME
    if not path.is_file():
        return None
    return FrozenHandoverNgramPrior.from_path(spec, path)
