"""Compact schema validation and normalization for real VLM responses."""

from __future__ import annotations

import json
from typing import Any


class SchemaValidationError(ValueError):
    """Raised when the compact VLM schema is invalid."""


def parse_json_payload(raw_text: str) -> dict[str, Any]:
    payload = json.loads(raw_text)
    if not isinstance(payload, dict):
        raise SchemaValidationError("top-level payload must be an object")
    return payload


def validate_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if str(payload.get("v", "")) != "1":
        raise SchemaValidationError("missing or unsupported schema version")

    phases = payload.get("ph", [])
    if not isinstance(phases, list):
        raise SchemaValidationError("'ph' must be a list")
    normalized_phases: list[list[Any]] = []
    for item in phases:
        if not isinstance(item, list) or len(item) != 2:
            raise SchemaValidationError("each ph item must be [phase_id, confidence]")
        normalized_phases.append([str(item[0]), float(item[1])])

    tools = payload.get("to", [])
    if not isinstance(tools, list):
        raise SchemaValidationError("'to' must be a list")
    normalized_tools: list[list[Any]] = []
    for item in tools:
        if not isinstance(item, list) or len(item) != 4:
            raise SchemaValidationError("each to item must be [tool_id, location_id, location_type, confidence]")
        normalized_tools.append([str(item[0]), str(item[1]), str(item[2]), float(item[3])])

    gesture = payload.get("sg", ["", "", "", 0.0])
    if not isinstance(gesture, list) or len(gesture) != 4:
        raise SchemaValidationError("'sg' must be [event_type, requested_tool, hand_pose, confidence]")
    normalized_gesture = [str(gesture[0]), str(gesture[1]), str(gesture[2]), float(gesture[3])]

    uncertainty = float(payload.get("u", 0.0))
    summary = str(payload.get("sum", ""))

    return {
        "v": "1",
        "ph": normalized_phases,
        "to": normalized_tools,
        "sg": normalized_gesture,
        "u": uncertainty,
        "sum": summary,
    }


def normalize_raw_text(raw_text: str) -> tuple[str, dict[str, Any]]:
    payload = validate_payload(parse_json_payload(raw_text))
    return json.dumps(payload, separators=(",", ":"), sort_keys=True), payload


def compact_vlm_json_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "v": {"type": "string", "enum": ["1"]},
            "ph": {
                "type": "array",
                "items": {
                    "type": "array",
                    "prefixItems": [
                        {"type": "string"},
                        {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    ],
                    "minItems": 2,
                    "maxItems": 2,
                },
            },
            "to": {
                "type": "array",
                "items": {
                    "type": "array",
                    "prefixItems": [
                        {"type": "string"},
                        {"type": "string"},
                        {"type": "string"},
                        {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    ],
                    "minItems": 4,
                    "maxItems": 4,
                },
            },
            "sg": {
                "type": "array",
                "prefixItems": [
                    {"type": "string"},
                    {"type": "string"},
                    {"type": "string"},
                    {"type": "number", "minimum": 0.0, "maximum": 1.0},
                ],
                "minItems": 4,
                "maxItems": 4,
            },
            "u": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "sum": {"type": "string"},
        },
        "required": ["v", "ph", "to", "sg", "u", "sum"],
        "additionalProperties": False,
    }
