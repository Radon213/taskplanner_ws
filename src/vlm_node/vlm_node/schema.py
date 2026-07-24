"""Compact schema validation and normalization for real VLM responses."""

from __future__ import annotations

import json
import math
from typing import Any

from procedure_spec import (
    DISTANCE_ORIGINS,
    RETRACTION_DIRECTIONS,
    BedRobotArmGroupNormalizationError,
    validate_retraction_distance_proposal,
)


class SchemaValidationError(ValueError):
    """Raised when the compact VLM schema is invalid."""


def _strip_markdown_fence(raw_text: str) -> str:
    text = raw_text.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if len(lines) >= 2 and lines[0].startswith("```"):
        if lines[-1].strip().startswith("```"):
            return "\n".join(lines[1:-1]).strip()
        return "\n".join(lines[1:]).strip()
    return text


def _decode_json_object(text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    payload, _ = decoder.raw_decode(text)
    if not isinstance(payload, dict):
        raise SchemaValidationError("top-level payload must be an object")
    return payload


def _trim_to_last_complete_top_level_field(text: str) -> str:
    start = text.find("{")
    if start < 0:
        return text
    depth = 0
    in_string = False
    escaped = False
    last_top_level_comma = -1
    for index, char in enumerate(text[start:], start=start):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
        elif char == "," and depth == 1:
            last_top_level_comma = index
    if last_top_level_comma > start:
        return text[start:last_top_level_comma] + "}"
    return text


def parse_json_payload(raw_text: str) -> dict[str, Any]:
    text = _strip_markdown_fence(raw_text)
    try:
        return _decode_json_object(text)
    except json.JSONDecodeError:
        repaired = _trim_to_last_complete_top_level_field(text)
        if repaired != text:
            return _decode_json_object(repaired)
        raise


def _validate_v1_payload(payload: dict[str, Any]) -> dict[str, Any]:
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


def _validate_v2_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if str(payload.get("v", "")) != "2":
        raise SchemaValidationError("missing or unsupported schema version")

    phase = payload.get("phase", ["", 0.0])
    if not isinstance(phase, list) or len(phase) != 2:
        raise SchemaValidationError("'phase' must be [phase_id, confidence]")
    normalized_phase = [str(phase[0]), float(phase[1])]

    tool = payload.get("tool", ["", 0.0])
    if not isinstance(tool, list) or len(tool) != 2:
        raise SchemaValidationError("'tool' must be [tool_id, confidence]")
    normalized_tool = [str(tool[0]), float(tool[1])]

    intent = payload.get("intent", ["", "", 0.0])
    if not isinstance(intent, list) or len(intent) != 3:
        raise SchemaValidationError("'intent' must be [intent_type, tool_id, confidence]")
    normalized_intent = [str(intent[0]), str(intent[1]), float(intent[2])]

    mayo = payload.get("mayo", [])
    if not isinstance(mayo, list):
        raise SchemaValidationError("'mayo' must be a list")
    normalized_mayo: list[list[Any]] = []
    for item in mayo:
        if not isinstance(item, list) or len(item) != 3:
            raise SchemaValidationError("each mayo item must be [tool_id, recover_or_reuse, confidence]")
        decision = str(item[1])
        if decision not in {"recover", "reuse"}:
            raise SchemaValidationError("mayo decision must be recover or reuse")
        normalized_mayo.append([str(item[0]), decision, float(item[2])])

    mayo_retrieve = payload.get("mayo_retrieve", ["", 0.0])
    if not isinstance(mayo_retrieve, list) or len(mayo_retrieve) != 2:
        raise SchemaValidationError("'mayo_retrieve' must be [tool_id, confidence]")
    normalized_mayo_retrieve = [str(mayo_retrieve[0]), float(mayo_retrieve[1])]

    return {
        "v": "2",
        "phase": normalized_phase,
        "tool": normalized_tool,
        "intent": normalized_intent,
        "mayo": normalized_mayo,
        "mayo_retrieve": normalized_mayo_retrieve,
        "u": float(payload.get("u", 0.0)),
        "sum": str(payload.get("sum", payload.get("summary", ""))),
    }


def _normalize_pair_rows(value: Any, label: str) -> list[list[Any]]:
    if not isinstance(value, list):
        raise SchemaValidationError(f"'{label}' must be a list")
    rows: list[list[Any]] = []
    for item in value:
        if not isinstance(item, list) or len(item) != 2:
            raise SchemaValidationError(f"each {label} item must be [id, confidence]")
        rows.append([str(item[0]), float(item[1])])
    return rows


def _validate_v3_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if str(payload.get("v", "")) != "3":
        raise SchemaValidationError("missing or unsupported schema version")

    phases = _normalize_pair_rows(payload.get("phase", []), "phase")
    tools = _normalize_pair_rows(payload.get("tool", []), "tool")

    intent = payload.get("intent", ["", "", 0.0])
    if not isinstance(intent, list) or len(intent) != 3:
        raise SchemaValidationError("'intent' must be [intent_type, tool_id, confidence]")
    normalized_intent = [str(intent[0]), str(intent[1]), float(intent[2])]

    mayo = payload.get("mayo", [])
    if not isinstance(mayo, list):
        raise SchemaValidationError("'mayo' must be a list")
    normalized_mayo: list[list[Any]] = []
    for item in mayo:
        if not isinstance(item, list) or len(item) != 3:
            raise SchemaValidationError("each mayo item must be [tool_id, recover_or_reuse, confidence]")
        decision = str(item[1])
        if decision not in {"recover", "reuse"}:
            raise SchemaValidationError("mayo decision must be recover or reuse")
        normalized_mayo.append([str(item[0]), decision, float(item[2])])

    mayo_retrieve = payload.get("mayo_retrieve", ["", 0.0])
    if not isinstance(mayo_retrieve, list) or len(mayo_retrieve) != 2:
        raise SchemaValidationError("'mayo_retrieve' must be [tool_id, confidence]")
    normalized_mayo_retrieve = [str(mayo_retrieve[0]), float(mayo_retrieve[1])]

    return {
        "v": "3",
        "phase": phases,
        "tool": tools,
        "intent": normalized_intent,
        "mayo": normalized_mayo,
        "mayo_retrieve": normalized_mayo_retrieve,
        "u": float(payload.get("u", 0.0)),
        "sum": str(payload.get("sum", payload.get("summary", ""))),
    }


def _validate_v4_bed_robot_arm_group(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise SchemaValidationError("'bed_robot_arm_group' must be an object or null")

    required = {
        "request_id",
        "group_id",
        "operation",
        "direction",
        "distance_mm",
        "distance_origin",
        "raw_distance_text",
        "end_effector_profile",
        "rationale",
        "confidence",
    }
    missing = sorted(required - set(value))
    extra = sorted(set(value) - required)
    if missing:
        raise SchemaValidationError(
            "bed_robot_arm_group is missing fields: " + ", ".join(missing)
        )
    if extra:
        raise SchemaValidationError(
            "bed_robot_arm_group has unsupported fields: " + ", ".join(extra)
        )

    request_id = str(value["request_id"]).strip()
    group_id = str(value["group_id"]).strip()
    operation = str(value["operation"]).strip()
    direction = str(value["direction"]).strip().upper()
    distance_origin = str(value["distance_origin"]).strip()
    raw_distance_text = str(value["raw_distance_text"]).strip()
    confidence = float(value["confidence"])
    distance_mm = float(value["distance_mm"])

    if not request_id:
        raise SchemaValidationError("bed_robot_arm_group request_id must be non-empty")
    # Suction is routed deterministically and does not come from VLM.  Schema
    # v4 therefore carries only a retraction proposal, still at group level.
    if group_id != "retraction":
        raise SchemaValidationError("bed_robot_arm_group group_id must be retraction")
    if operation != "retraction":
        raise SchemaValidationError("bed_robot_arm_group operation must be retraction")
    if direction not in RETRACTION_DIRECTIONS:
        raise SchemaValidationError(
            "bed_robot_arm_group direction must be one of "
            + ", ".join(RETRACTION_DIRECTIONS)
        )
    if distance_origin not in DISTANCE_ORIGINS:
        raise SchemaValidationError(
            "bed_robot_arm_group distance_origin must be one of " + ", ".join(DISTANCE_ORIGINS)
        )
    if not math.isfinite(confidence) or confidence < 0.0 or confidence > 1.0:
        raise SchemaValidationError("bed_robot_arm_group confidence must be between 0 and 1")
    if distance_origin.startswith("explicit_") and not raw_distance_text:
        raise SchemaValidationError(
            "bed_robot_arm_group raw_distance_text is required for an explicit distance"
        )
    try:
        normalized_distance = validate_retraction_distance_proposal(
            raw_distance_text=raw_distance_text,
            distance_mm=distance_mm,
            distance_origin=distance_origin,
        )
    except BedRobotArmGroupNormalizationError as exc:
        raise SchemaValidationError(f"invalid bed_robot_arm_group distance: {exc}") from exc

    return {
        "request_id": request_id,
        "group_id": group_id,
        "operation": operation,
        "direction": direction,
        "distance_mm": normalized_distance.distance_mm,
        "distance_origin": normalized_distance.distance_origin,
        "raw_distance_text": raw_distance_text,
        "end_effector_profile": str(value["end_effector_profile"]).strip(),
        "rationale": str(value["rationale"]).strip(),
        "confidence": confidence,
    }


def _validate_v4_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if str(payload.get("v", "")) != "4":
        raise SchemaValidationError("missing or unsupported schema version")
    v3_payload = dict(payload)
    v3_payload["v"] = "3"
    v3_payload.pop("bed_robot_arm_group", None)
    normalized = _validate_v3_payload(v3_payload)
    normalized["v"] = "4"
    normalized["bed_robot_arm_group"] = _validate_v4_bed_robot_arm_group(
        payload.get("bed_robot_arm_group")
    )
    return normalized


def validate_payload(payload: dict[str, Any]) -> dict[str, Any]:
    version = str(payload.get("v", ""))
    if version == "1":
        return _validate_v1_payload(payload)
    if version == "2":
        return _validate_v2_payload(payload)
    if version == "3":
        return _validate_v3_payload(payload)
    if version == "4":
        return _validate_v4_payload(payload)
    raise SchemaValidationError("missing or unsupported schema version")


def normalize_raw_text(raw_text: str) -> tuple[str, dict[str, Any]]:
    payload = validate_payload(parse_json_payload(raw_text))
    return json.dumps(payload, separators=(",", ":"), sort_keys=True), payload


def compact_vlm_json_schema(version: str = "1") -> dict[str, Any]:
    if str(version) == "4":
        schema = compact_vlm_json_schema("3")
        schema["properties"] = dict(schema["properties"])
        schema["properties"]["v"] = {"type": "string", "enum": ["4"]}
        schema["properties"]["bed_robot_arm_group"] = {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "properties": {
                        "request_id": {"type": "string", "minLength": 1},
                        "group_id": {"type": "string", "enum": ["retraction"]},
                        "operation": {"type": "string", "enum": ["retraction"]},
                        "direction": {
                            "type": "string",
                            "enum": list(RETRACTION_DIRECTIONS),
                        },
                        "distance_mm": {"type": "number", "exclusiveMinimum": 0.0},
                        "distance_origin": {
                            "type": "string",
                            "enum": list(DISTANCE_ORIGINS),
                        },
                        "raw_distance_text": {"type": "string"},
                        "end_effector_profile": {"type": "string"},
                        "rationale": {"type": "string"},
                        "confidence": {
                            "type": "number",
                            "minimum": 0.0,
                            "maximum": 1.0,
                        },
                    },
                    "required": [
                        "request_id",
                        "group_id",
                        "operation",
                        "direction",
                        "distance_mm",
                        "distance_origin",
                        "raw_distance_text",
                        "end_effector_profile",
                        "rationale",
                        "confidence",
                    ],
                    "additionalProperties": False,
                },
            ]
        }
        schema["required"] = [*schema["required"], "bed_robot_arm_group"]
        return schema
    if str(version) == "3":
        pair_array = {
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
            "minItems": 1,
            "maxItems": 4,
        }
        return {
            "type": "object",
            "properties": {
                "v": {"type": "string", "enum": ["3"]},
                "phase": pair_array,
                "tool": pair_array,
                "intent": {
                    "type": "array",
                    "prefixItems": [
                        {"type": "string"},
                        {"type": "string"},
                        {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    ],
                    "minItems": 3,
                    "maxItems": 3,
                },
                "mayo": {
                    "type": "array",
                    "items": {
                        "type": "array",
                        "prefixItems": [
                            {"type": "string"},
                            {"type": "string", "enum": ["recover", "reuse"]},
                            {"type": "number", "minimum": 0.0, "maximum": 1.0},
                        ],
                        "minItems": 3,
                        "maxItems": 3,
                    },
                },
                "mayo_retrieve": {
                    "type": "array",
                    "prefixItems": [
                        {"type": "string"},
                        {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    ],
                    "minItems": 2,
                    "maxItems": 2,
                },
                "u": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "sum": {"type": "string"},
            },
            "required": ["v", "phase", "tool", "intent", "mayo", "mayo_retrieve", "u", "sum"],
            "additionalProperties": False,
        }
    if str(version) == "2":
        return {
            "type": "object",
            "properties": {
                "v": {"type": "string", "enum": ["2"]},
                "phase": {
                    "type": "array",
                    "prefixItems": [
                        {"type": "string"},
                        {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    ],
                    "minItems": 2,
                    "maxItems": 2,
                },
                "tool": {
                    "type": "array",
                    "prefixItems": [
                        {"type": "string"},
                        {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    ],
                    "minItems": 2,
                    "maxItems": 2,
                },
                "intent": {
                    "type": "array",
                    "prefixItems": [
                        {"type": "string"},
                        {"type": "string"},
                        {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    ],
                    "minItems": 3,
                    "maxItems": 3,
                },
                "mayo": {
                    "type": "array",
                    "items": {
                        "type": "array",
                        "prefixItems": [
                            {"type": "string"},
                            {"type": "string", "enum": ["recover", "reuse"]},
                            {"type": "number", "minimum": 0.0, "maximum": 1.0},
                        ],
                        "minItems": 3,
                        "maxItems": 3,
                    },
                },
                "mayo_retrieve": {
                    "type": "array",
                    "prefixItems": [
                        {"type": "string"},
                        {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    ],
                    "minItems": 2,
                    "maxItems": 2,
                },
                "u": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "sum": {"type": "string"},
            },
            "required": ["v", "phase", "tool", "intent", "mayo", "mayo_retrieve", "u", "sum"],
            "additionalProperties": False,
        }
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
