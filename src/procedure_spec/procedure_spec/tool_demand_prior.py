"""Frozen future tool-demand lookup for procedure-aware reuse forecasts.

The prior answers one narrowly scoped, advisory question: for each supported
tool type, has that type appeared in at least one *future* confirmed
scrub-nurse-to-surgeon handover in reviewed demonstrations with the same
functional phase and completed-handover suffix?  It is deliberately separate
from camera observations, Digital Twin placement, inventory, and command
admission.  Those owners answer different questions.

The YAML asset is generated offline.  Runtime loads it once with the procedure
bundle and performs a small immutable lookup; it never receives case IDs,
timestamps, source events, or raw labels.
"""

from __future__ import annotations

import math
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping

import yaml

from .query_api import ProcedureSpec


FROZEN_TOOL_DEMAND_PRIOR_FILENAME = "tool_future_demand_prior.yaml"
FROZEN_TOOL_DEMAND_PRIOR_SCHEMA = "taskplanner.frozen_tool_future_demand_prior.v1"
FROZEN_TOOL_DEMAND_PRIOR_TARGET = (
    "at_least_one_future_supported_scrub_nurse_to_surgeon_handover_"
    "before_procedure_end"
)

# Match the existing handover n-gram lookup semantics so each owner gives the
# same meaning to a completed-handover suffix.  Specific context wins; a
# global smoothed fallback keeps the advisory view available at procedure start.
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


class ToolDemandPriorError(ValueError):
    """Raised when a frozen future tool-demand asset is malformed."""


class FrozenToolDemandPrior:
    """Immutable O(1) future-demand lookup from one validated YAML asset.

    ``predict`` returns a compact type-level probability table only.  It does
    not claim that a tool is visible, present on the Mayo stand, available to a
    robot, or eligible for dispatch.
    """

    def __init__(self, spec: ProcedureSpec, payload: Mapping[str, Any]) -> None:
        self._spec = spec
        self._requestable_tool_ids = frozenset(
            spec.get_scenario_policy().requestable_instrument_ids
        )
        self._ordered_tool_ids = tuple(sorted(self._requestable_tool_ids))
        self._phase_ids = frozenset(spec.phase_ids)
        self._artifact_id, self._lookup = self._validate_and_compile(payload)

    @classmethod
    def from_path(
        cls,
        spec: ProcedureSpec,
        path: str | Path,
    ) -> "FrozenToolDemandPrior":
        artifact_path = Path(path)
        try:
            with artifact_path.open("r", encoding="utf-8") as handle:
                payload = yaml.safe_load(handle) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise ToolDemandPriorError(
                f"cannot read frozen tool-demand prior {artifact_path}: {exc}"
            ) from exc
        if not isinstance(payload, Mapping):
            raise ToolDemandPriorError(
                f"{artifact_path} must contain a YAML mapping"
            )
        return cls(spec, payload)

    @property
    def artifact_id(self) -> str:
        """Stable identifier suitable for state/UI provenance."""

        return self._artifact_id

    def predict(
        self,
        *,
        phase_id: Any,
        completed_handovers: Iterable[Any],
    ) -> dict[str, Any] | None:
        """Return the most-specific precomputed future-demand table.

        Unknown or unsupported handovers reset the suffix instead of being
        silently removed and joining two otherwise unrelated exchanges.
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
            support, probabilities = compiled
            # Return a fresh, fixed-shape object; the frozen lookup itself is
            # never exposed for caller mutation.
            return {
                "id": self._artifact_id,
                "match": match,
                "support": support,
                "probabilities": [list(row) for row in probabilities],
            }
        return None

    def _normalized_history(self, completed_handovers: Iterable[Any]) -> list[str]:
        history: list[str] = []
        for item in completed_handovers:
            raw_tool = item.get("tool", "") if isinstance(item, Mapping) else item
            tool_id = str(raw_tool or "").strip()
            if tool_id not in self._requestable_tool_ids:
                history.clear()
                continue
            history.append(tool_id)
        return history

    def _validate_and_compile(
        self,
        payload: Mapping[str, Any],
    ) -> tuple[
        str,
        Mapping[
            tuple[str, str, tuple[str, ...]],
            tuple[int, tuple[tuple[str, float], ...]],
        ],
    ]:
        if payload.get("schema") != FROZEN_TOOL_DEMAND_PRIOR_SCHEMA:
            raise ToolDemandPriorError("unsupported frozen tool-demand prior schema")
        artifact_id = str(payload.get("id", "")).strip()
        if not artifact_id:
            raise ToolDemandPriorError("frozen tool-demand prior needs id")
        if str(payload.get("procedure_id", "")).strip() != self._spec.procedure_id:
            raise ToolDemandPriorError(
                "frozen tool-demand prior procedure_id does not match bundle"
            )
        if payload.get("target") != FROZEN_TOOL_DEMAND_PRIOR_TARGET:
            raise ToolDemandPriorError("frozen tool-demand prior target is unsupported")
        self._validate_smoothing(payload.get("smoothing"))

        raw_rules = payload.get("rules")
        if not isinstance(raw_rules, list) or not raw_rules:
            raise ToolDemandPriorError("frozen tool-demand prior needs rules")

        lookup: dict[
            tuple[str, str, tuple[str, ...]],
            tuple[int, tuple[tuple[str, float], ...]],
        ] = {}
        for index, raw_rule in enumerate(raw_rules):
            if not isinstance(raw_rule, Mapping):
                raise ToolDemandPriorError(f"rule {index} must be a mapping")
            match = str(raw_rule.get("match", "")).strip()
            rule_spec = _MATCH_RULE_BY_NAME.get(match)
            if rule_spec is None:
                raise ToolDemandPriorError(f"rule {index} has unsupported match")
            uses_phase, depth = rule_spec

            phase = str(raw_rule.get("phase", "")).strip()
            if uses_phase:
                if phase not in self._phase_ids:
                    raise ToolDemandPriorError(
                        f"rule {index} has unknown phase {phase!r}"
                    )
            elif phase:
                raise ToolDemandPriorError(
                    f"rule {index} must not specify phase for {match}"
                )

            raw_history = raw_rule.get("history", [])
            if not isinstance(raw_history, list) or len(raw_history) > depth:
                raise ToolDemandPriorError(
                    f"rule {index} history may contain at most {depth} tools"
                )
            history = tuple(str(tool_id).strip() for tool_id in raw_history)
            if any(tool_id not in self._requestable_tool_ids for tool_id in history):
                raise ToolDemandPriorError(
                    f"rule {index} history contains a non-requestable tool"
                )

            support = raw_rule.get("support")
            if isinstance(support, bool) or not isinstance(support, int) or support <= 0:
                raise ToolDemandPriorError(
                    f"rule {index} support must be a positive integer"
                )

            raw_probabilities = raw_rule.get("probabilities")
            if not isinstance(raw_probabilities, list):
                raise ToolDemandPriorError(f"rule {index} needs probabilities")
            probabilities: dict[str, float] = {}
            for item in raw_probabilities:
                if not isinstance(item, Mapping):
                    raise ToolDemandPriorError(
                        f"rule {index} probability must be a mapping"
                    )
                tool_id = str(item.get("tool", "")).strip()
                probability = item.get("probability")
                if tool_id not in self._requestable_tool_ids:
                    raise ToolDemandPriorError(
                        f"rule {index} probability has a non-requestable tool"
                    )
                if tool_id in probabilities:
                    raise ToolDemandPriorError(
                        f"rule {index} repeats probability tool {tool_id}"
                    )
                if (
                    isinstance(probability, bool)
                    or not isinstance(probability, (int, float))
                    or not math.isfinite(float(probability))
                    or not 0.0 <= float(probability) <= 1.0
                ):
                    raise ToolDemandPriorError(
                        f"rule {index} probability must be finite in [0, 1]"
                    )
                probabilities[tool_id] = float(probability)
            if set(probabilities) != self._requestable_tool_ids:
                raise ToolDemandPriorError(
                    f"rule {index} probabilities must cover every requestable tool"
                )

            key = (match, phase if uses_phase else "", history)
            if key in lookup:
                raise ToolDemandPriorError(
                    f"frozen tool-demand prior repeats rule {match}"
                )
            lookup[key] = (
                support,
                tuple((tool_id, probabilities[tool_id]) for tool_id in self._ordered_tool_ids),
            )

        if ("global", "", tuple()) not in lookup:
            raise ToolDemandPriorError(
                "frozen tool-demand prior needs a global fallback"
            )
        return artifact_id, MappingProxyType(lookup)

    @staticmethod
    def _validate_smoothing(raw_smoothing: Any) -> None:
        if not isinstance(raw_smoothing, Mapping):
            raise ToolDemandPriorError("frozen tool-demand prior needs smoothing")
        if raw_smoothing.get("method") != "beta_binomial_laplace":
            raise ToolDemandPriorError("unsupported frozen tool-demand prior smoothing")
        for key in ("alpha", "beta"):
            value = raw_smoothing.get(key)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) <= 0.0
            ):
                raise ToolDemandPriorError(
                    f"frozen tool-demand prior smoothing {key} must be positive"
                )


def load_frozen_tool_demand_prior(
    spec: ProcedureSpec,
    bundle_dir: str | Path,
) -> FrozenToolDemandPrior | None:
    """Load the optional per-procedure demand prior once, or return no prior."""

    path = Path(bundle_dir) / FROZEN_TOOL_DEMAND_PRIOR_FILENAME
    if not path.is_file():
        return None
    return FrozenToolDemandPrior.from_path(spec, path)
