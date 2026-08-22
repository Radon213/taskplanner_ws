"""Typed, proposal-only contract for spoken-command interpretation.

The resolver deliberately stops at :class:`VoiceIntentProposal`.  A proposal
is not a ROS Service/Action request and must still be evaluated by the
Digital Twin and behavior-tree policy.  This module has no ROS dependency so
its grounding rules can be replayed in ordinary unit tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final


DISPOSITION_PROPOSE: Final = "propose"
DISPOSITION_CLARIFY: Final = "clarify"
DISPOSITION_REJECT: Final = "reject"
DISPOSITION_NO_COMMAND: Final = "no_command"

INTENT_TOOL_HANDOVER: Final = "tool_handover"
INTENT_RETRACTOR_COMMAND: Final = "retractor_command"

TARGET_SIDE_NONE: Final = "none"


@dataclass(frozen=True)
class VoiceIntentProposal:
    """A bounded semantic interpretation of one final STT utterance.

    ``target_side`` and ``distance_m`` are deliberately populated with
    fail-closed neutral values for every result.  This resolver does not infer
    physical slots; future retraction adjustment support must provide and
    ground those slots explicitly.
    """

    raw_text: str
    normalized_text: str
    procedure_id: str = ""
    catalog_id: str = ""
    intent: str = ""
    tool_id: str = ""
    retractor_command: str = ""
    target_side: str = TARGET_SIDE_NONE
    distance_m: float = 0.0
    urgency: str = ""
    provenance: str = "deterministic"
    requires_confirmation: bool = False
    disposition: str = DISPOSITION_NO_COMMAND
    reason: str = ""
    evidence_spans: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_executable_proposal(self) -> bool:
        """Whether this is a complete *proposal*, not execution permission."""

        return self.disposition == DISPOSITION_PROPOSE

    @classmethod
    def no_command(
        cls,
        raw_text: str,
        normalized_text: str,
        *,
        reason: str,
        evidence_spans: tuple[str, ...] = (),
        procedure_id: str = "",
        catalog_id: str = "",
    ) -> "VoiceIntentProposal":
        return cls(
            raw_text=raw_text,
            normalized_text=normalized_text,
            procedure_id=procedure_id,
            catalog_id=catalog_id,
            disposition=DISPOSITION_NO_COMMAND,
            reason=reason,
            evidence_spans=evidence_spans,
        )

    @classmethod
    def reject(
        cls,
        raw_text: str,
        normalized_text: str,
        *,
        reason: str,
        evidence_spans: tuple[str, ...] = (),
        intent: str = "",
        procedure_id: str = "",
        catalog_id: str = "",
    ) -> "VoiceIntentProposal":
        return cls(
            raw_text=raw_text,
            normalized_text=normalized_text,
            procedure_id=procedure_id,
            catalog_id=catalog_id,
            intent=intent,
            disposition=DISPOSITION_REJECT,
            reason=reason,
            evidence_spans=evidence_spans,
        )

    @classmethod
    def clarify_tool(
        cls,
        raw_text: str,
        normalized_text: str,
        *,
        evidence_spans: tuple[str, ...],
        reason: str = "missing_tool_id",
        procedure_id: str = "",
        catalog_id: str = "",
    ) -> "VoiceIntentProposal":
        return cls(
            raw_text=raw_text,
            normalized_text=normalized_text,
            procedure_id=procedure_id,
            catalog_id=catalog_id,
            intent=INTENT_TOOL_HANDOVER,
            disposition=DISPOSITION_CLARIFY,
            reason=reason,
            evidence_spans=evidence_spans,
        )
