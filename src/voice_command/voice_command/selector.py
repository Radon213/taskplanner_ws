"""Bounded candidate selection abstractions.

Selectors receive candidates generated and grounded locally.  They return an
identifier from that supplied list or ``REJECT``; they never produce a tool,
command, side, distance, ROS payload, or execution decision.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class CandidateSelection:
    candidate_id: str | None
    provenance: str
    reason: str = ""
    unavailable: bool = False


class CandidateLike(Protocol):
    candidate_id: str

    def selector_payload(self) -> dict[str, object]: ...


class CandidateSelector(Protocol):
    """Choose an existing candidate or reject it."""

    def select(
        self,
        *,
        raw_text: str,
        normalized_text: str,
        candidates: Sequence[CandidateLike],
    ) -> CandidateSelection: ...


class DeterministicCandidateSelector:
    """Select exactly one locally-grounded candidate; reject ambiguity."""

    def select(
        self,
        *,
        raw_text: str,
        normalized_text: str,
        candidates: Sequence[CandidateLike],
    ) -> CandidateSelection:
        del raw_text, normalized_text
        if len(candidates) == 1:
            return CandidateSelection(
                candidate_id=candidates[0].candidate_id,
                provenance="deterministic_selector",
            )
        if not candidates:
            return CandidateSelection(
                candidate_id=None,
                provenance="deterministic_selector",
                reason="no_candidates",
            )
        return CandidateSelection(
            candidate_id=None,
            provenance="deterministic_selector",
            reason="ambiguous_candidates",
        )


class OpenAICompatibleCandidateSelector:
    """Optional local-model selector with a strict candidate-ID boundary.

    The endpoint is deliberately optional and disabled by default in the ROS
    node.  A malformed answer, unknown candidate ID, or explicit ``REJECT``
    never turns into a command.  Transport failures are marked ``unavailable``
    so the resolver may use its one-candidate deterministic fallback.
    """

    _SYSTEM_PROMPT = (
        "You are a bounded selector for Korean surgical voice intents. "
        "Select only one candidate_id supplied in CANDIDATES, or REJECT. "
        "Never invent a tool, command, side, distance, ROS payload, or action. "
        'Return exactly JSON: {"candidate_id":"<id-or-REJECT>"}.'
    )

    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        timeout_sec: float = 0.35,
    ) -> None:
        self._endpoint = str(endpoint).strip()
        self._model = str(model).strip()
        self._timeout_sec = max(0.05, float(timeout_sec))

    def select(
        self,
        *,
        raw_text: str,
        normalized_text: str,
        candidates: Sequence[CandidateLike],
    ) -> CandidateSelection:
        if not self._endpoint or not self._model:
            return CandidateSelection(
                candidate_id=None,
                provenance="openai_candidate_selector",
                reason="selector_not_configured",
                unavailable=True,
            )

        payload = {
            "model": self._model,
            "temperature": 0,
            "max_tokens": 24,
            "messages": [
                {"role": "system", "content": self._SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "raw_text": raw_text,
                            "normalized_text": normalized_text,
                            "candidates": [
                                candidate.selector_payload()
                                for candidate in candidates
                            ],
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            ],
        }
        request = Request(
            self._endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self._timeout_sec) as response:
                decoded = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, OSError, TimeoutError, ValueError) as exc:
            return CandidateSelection(
                candidate_id=None,
                provenance="openai_candidate_selector",
                reason=f"selector_transport_error:{type(exc).__name__}",
                unavailable=True,
            )

        try:
            content = decoded["choices"][0]["message"]["content"]
            selected = str(json.loads(str(content))["candidate_id"]).strip()
        except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return CandidateSelection(
                candidate_id=None,
                provenance="openai_candidate_selector",
                reason="selector_invalid_response",
                unavailable=True,
            )

        if selected.upper() == "REJECT":
            return CandidateSelection(
                candidate_id=None,
                provenance="openai_candidate_selector",
                reason="selector_rejected",
            )
        allowed = {candidate.candidate_id for candidate in candidates}
        if selected not in allowed:
            return CandidateSelection(
                candidate_id=None,
                provenance="openai_candidate_selector",
                reason="selector_unknown_candidate",
            )
        return CandidateSelection(
            candidate_id=selected,
            provenance="openai_candidate_selector",
        )
