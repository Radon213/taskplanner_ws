"""Shared guard helpers for runtime summaries."""

from __future__ import annotations

from surgical_msgs.msg import WorldState


def should_allow_handover(world: WorldState) -> bool:
    # Phase uncertainty is observation metadata only.  The authoritative
    # handover decision already includes the remaining runtime guards, and a
    # low-confidence phase must not reintroduce a second veto here.
    return bool(world.handover_allowed and not world.recovery_required)
