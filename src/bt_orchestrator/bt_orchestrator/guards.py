"""Shared guard helpers for runtime summaries."""

from __future__ import annotations

from surgical_msgs.msg import WorldState


def should_allow_handover(world: WorldState) -> bool:
    return bool(world.handover_allowed and not world.phase_uncertain and not world.recovery_required)
