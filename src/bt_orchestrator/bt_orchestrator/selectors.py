"""Shared selection helpers for summaries and tests."""

from __future__ import annotations

from surgical_msgs.msg import WorldState


def select_expected_tool(world: WorldState) -> str:
    for instrument_id in world.expected_instruments:
        if instrument_id in world.available_instruments:
            return instrument_id
    return ""
