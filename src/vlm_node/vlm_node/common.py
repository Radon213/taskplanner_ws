"""Shared helpers for mock/real VLM implementations."""

from __future__ import annotations

import json
from typing import Any


TOOL_DISPLAY_NAMES = {
    "retractor": "Army-Navy retractor",
    "cautery": "Cautery (Bovie)",
    "metzenbaum": "Metzenbaum scissors",
    "suction": "Suction tip",
    "right_angle": "Right-angle clamp",
    "forceps": "Tissue forceps",
    "grasper": "Atraumatic grasper",
    "bipolar": "Bipolar forceps",
    "scissors": "Curved scissors",
    "suction_irrigator": "Suction irrigator",
    "clip_applier": "Clip applier",
    "needle_driver": "Needle driver",
}


def tool_display_name(tool_id: str) -> str:
    return TOOL_DISPLAY_NAMES.get(tool_id, tool_id.replace("_", " ").strip())


def compact_json(data: dict[str, Any]) -> str:
    return json.dumps(data, separators=(",", ":"), sort_keys=True)


def _entity_center(entity: dict[str, Any]) -> tuple[float, float]:
    return (
        float(entity.get("x", 0.0)) + float(entity.get("width", 0.0)) / 2.0,
        float(entity.get("y", 0.0)) + float(entity.get("height", 0.0)) / 2.0,
    )


def anchor_positions(layout_bundle: dict[str, Any]) -> dict[str, tuple[float, float]]:
    positions: dict[str, tuple[float, float]] = {}
    entities = {entity["id"]: entity for entity in layout_bundle.get("entities", [])}
    for anchor in layout_bundle.get("anchors", []):
        anchor_id = str(anchor.get("id", ""))
        if not anchor_id:
            continue
        positions[anchor_id] = (float(anchor.get("x", 0.0)), float(anchor.get("y", 0.0)))
        attached_to = str(anchor.get("attached_to", ""))
        if attached_to and attached_to in entities and attached_to not in positions:
            positions[attached_to] = _entity_center(entities[attached_to])
    for entity_id, entity in entities.items():
        positions.setdefault(entity_id, _entity_center(entity))
    return positions


def instrument_anchor_id(instrument, layout_bundle: dict[str, Any]) -> str:
    anchors = {anchor["id"] for anchor in layout_bundle.get("anchors", [])}
    if getattr(instrument, "location_id", "") in anchors:
        return str(getattr(instrument, "location_id", ""))
    lifecycle_stage = str(getattr(instrument, "lifecycle_stage", ""))
    if lifecycle_stage in {"mayo_recovery", "mayo_reuse"} and "mayo_stand" in anchors:
        return "mayo_stand"
    if lifecycle_stage == "prepositioned_right" and "robot_right_hand" in anchors:
        return "robot_right_hand"
    if lifecycle_stage in {"recovering_left", "cleaning_left", "cleaned_left"}:
        if getattr(instrument, "location_id", "") == "cleaner_slot" and "cleaner_slot" in anchors:
            return "cleaner_slot"
        if "robot_left_hand" in anchors:
            return "robot_left_hand"
    if lifecycle_stage == "surgeon_owned":
        location_id = str(getattr(instrument, "location_id", ""))
        if location_id in anchors:
            return location_id
        if str(getattr(instrument, "location_type", "")) == "surgical_field":
            field_anchor = next((anchor for anchor in anchors if anchor.startswith("field_region_")), "")
            if field_anchor:
                return field_anchor
        if "surgeon_hand" in anchors:
            return "surgeon_hand"
    if str(getattr(instrument, "home_location_id", "")) in anchors:
        return str(getattr(instrument, "home_location_id", ""))
    return "unknown_zone_anchor" if "unknown_zone_anchor" in anchors else next(iter(anchors), "")
