"""Pure arbitration for the DT-owned autonomous n-gram tool policy."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NgramPolicyIntent:
    """One mutually exclusive autonomous intent selected by the DT."""

    action: str
    tool_id: str = ""
    conflicted: bool = False


def arbitrate_ngram_intent(
    *,
    preparation_tool_id: str = "",
    recovery_tool_id: str = "",
    preparation_probability: float = 0.0,
    recovery_probability: float = 1.0,
) -> NgramPolicyIntent:
    """Choose one autonomous action from the frozen n-gram probabilities.

    Preparation and recovery can be simultaneously eligible when a likely
    next tool is available while an unlikely tool is parked on Mayo. Compare
    the preparation evidence ``p(next tool)`` with the recovery evidence
    ``1 - p(next tool)`` for the Mayo candidate.  A tie stays preparation-first
    because cleanup can be retried after the preparation Action terminates.
    Explicit/direct requests and active-Action non-preemption remain at the
    surrounding reducer boundary.
    """

    preparation_tool_id = str(preparation_tool_id or "")
    recovery_tool_id = str(recovery_tool_id or "")
    conflicted = bool(preparation_tool_id and recovery_tool_id)
    preparation_probability = max(
        0.0, min(1.0, float(preparation_probability))
    )
    recovery_probability = max(
        0.0, min(1.0, float(recovery_probability))
    )
    if (
        conflicted
        and (1.0 - recovery_probability) > preparation_probability
    ):
        return NgramPolicyIntent(
            action="recover",
            tool_id=recovery_tool_id,
            conflicted=True,
        )
    if preparation_tool_id:
        return NgramPolicyIntent(
            action="prepare",
            tool_id=preparation_tool_id,
            conflicted=conflicted,
        )
    if recovery_tool_id:
        return NgramPolicyIntent(
            action="recover",
            tool_id=recovery_tool_id,
            conflicted=conflicted,
        )
    return NgramPolicyIntent(action="wait")
