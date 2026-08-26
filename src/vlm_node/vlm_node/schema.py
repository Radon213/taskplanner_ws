"""Compact schema validation and normalization for real VLM responses."""

from __future__ import annotations

import json
import math
from typing import Any

from procedure_spec import (
    DISTANCE_ORIGINS,
    BedRobotArmGroupNormalizationError,
    validate_retraction_distance_proposal,
)


class SchemaValidationError(ValueError):
    """Raised when the compact VLM schema is invalid."""


RETIRED_VLM_HAND_OUTPUT_FIELDS = frozenset(
    {
        "gesture",
        "hand_pose",
        "handedness",
        "palm_facing",
        "sg",
        "surgeon_gesture",
    }
)


def _reject_retired_vlm_hand_output_fields(
    payload: dict[str, Any],
    *,
    version: str,
) -> None:
    retired = sorted(RETIRED_VLM_HAND_OUTPUT_FIELDS.intersection(payload))
    if retired:
        raise SchemaValidationError(
            f"schema v{version} no longer accepts retired VLM hand output fields: "
            + ", ".join(retired)
        )


def _mayo_confidence(value: Any) -> float:
    confidence = float(value)
    if not math.isfinite(confidence) or confidence < 0.0 or confidence > 1.0:
        raise SchemaValidationError("mayo confidence must be between 0 and 1")
    return confidence


def _uncertainty(value: Any) -> float:
    """Normalize malformed uncertainty conservatively without enabling actions."""

    try:
        uncertainty = float(value)
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(uncertainty) or uncertainty < 0.0 or uncertainty > 1.0:
        return 1.0
    return uncertainty


def normalize_mayo_semantics(
    mayo: Any,
    mayo_retrieve: Any,
) -> tuple[list[list[Any]], list[Any]]:
    """Make per-tool Mayo votes and the selected recovery candidate consistent."""

    if not isinstance(mayo, list):
        raise SchemaValidationError("'mayo' must be a list")
    if not isinstance(mayo_retrieve, list) or len(mayo_retrieve) != 2:
        raise SchemaValidationError("'mayo_retrieve' must be [tool_id, confidence]")
    _mayo_confidence(mayo_retrieve[1])

    decisions: dict[str, tuple[str, float, int]] = {}
    for index, item in enumerate(mayo):
        if not isinstance(item, list) or len(item) != 3:
            raise SchemaValidationError(
                "each mayo item must be [tool_id, recover_or_reuse, confidence]"
            )
        tool_id = str(item[0]).strip()
        decision = str(item[1]).strip().lower()
        if decision not in {"recover", "reuse"}:
            raise SchemaValidationError("mayo decision must be recover or reuse")
        confidence = _mayo_confidence(item[2])
        if not tool_id:
            continue

        previous = decisions.get(tool_id)
        should_replace = previous is None or confidence > previous[1]
        if (
            previous is not None
            and math.isclose(confidence, previous[1])
            and decision == "reuse"
            and previous[0] == "recover"
        ):
            # Equal-confidence disagreement must fail closed against recovery.
            should_replace = True
        if should_replace:
            decisions[tool_id] = (decision, confidence, index)

    normalized_mayo = [
        [tool_id, decision, confidence]
        for tool_id, (decision, confidence, _index) in sorted(
            decisions.items(),
            key=lambda item: item[1][2],
        )
    ]
    recover_candidates = [
        (tool_id, confidence, index)
        for tool_id, (decision, confidence, index) in decisions.items()
        if decision == "recover"
    ]
    if not recover_candidates:
        return normalized_mayo, ["", 0.0]

    recover_tool, recover_confidence, _index = min(
        recover_candidates,
        key=lambda item: (-item[1], item[2], item[0]),
    )
    return normalized_mayo, [recover_tool, recover_confidence]


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

    uncertainty = _uncertainty(payload.get("u", 0.0))
    summary = str(payload.get("sum", ""))

    return {
        "v": "1",
        "ph": normalized_phases,
        "to": normalized_tools,
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

    normalized_mayo, normalized_mayo_retrieve = normalize_mayo_semantics(
        payload.get("mayo", []),
        payload.get("mayo_retrieve", ["", 0.0]),
    )

    return {
        "v": "2",
        "phase": normalized_phase,
        "tool": normalized_tool,
        "intent": normalized_intent,
        "mayo": normalized_mayo,
        "mayo_retrieve": normalized_mayo_retrieve,
        "u": _uncertainty(payload.get("u", 0.0)),
        "sum": str(payload.get("sum", payload.get("summary", ""))),
    }


def _normalize_pair_rows(value: Any, label: str) -> list[list[Any]]:
    if not isinstance(value, list):
        raise SchemaValidationError(f"'{label}' must be a list")
    # Some unconstrained OpenAI-compatible backends collapse a one-candidate
    # array from [["T01", 0.8]] to ["T01", 0.8]. The meaning is unique, so
    # restore only this exact shape; malformed or ambiguous rows still fail.
    if (
        len(value) == 2
        and isinstance(value[0], str)
        and not isinstance(value[1], (list, dict, bool))
    ):
        try:
            float(value[1])
        except (TypeError, ValueError):
            pass
        else:
            value = [value]
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

    normalized_mayo, normalized_mayo_retrieve = normalize_mayo_semantics(
        payload.get("mayo", []),
        payload.get("mayo_retrieve", ["", 0.0]),
    )

    return {
        "v": "3",
        "phase": phases,
        "tool": tools,
        "intent": normalized_intent,
        "mayo": normalized_mayo,
        "mayo_retrieve": normalized_mayo_retrieve,
        "u": _uncertainty(payload.get("u", 0.0)),
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
        "adjustment_mode",
        "target_retractor_id",
        "direction_frame",
        "direction",
        "axis",
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
    adjustment_mode = str(value["adjustment_mode"]).strip().lower()
    target_retractor_id = str(value["target_retractor_id"]).strip().lower()
    direction_frame = str(value["direction_frame"]).strip().lower()
    direction = str(value["direction"]).strip().lower()
    axis = str(value["axis"]).strip().lower()
    distance_origin = str(value["distance_origin"]).strip()
    raw_distance_text = str(value["raw_distance_text"]).strip()
    confidence = float(value["confidence"])
    distance_mm = float(value["distance_mm"])

    if not request_id:
        raise SchemaValidationError("bed_robot_arm_group request_id must be non-empty")
    # Schema v4 carries only fine retraction-adjustment evidence. Tool change
    # remains a separate deterministic request path.
    if group_id != "retraction":
        raise SchemaValidationError("bed_robot_arm_group group_id must be retraction")
    if operation != "retraction":
        raise SchemaValidationError("bed_robot_arm_group operation must be retraction")
    if adjustment_mode == "single":
        if target_retractor_id not in {
            "left_malleable",
            "right_malleable",
            "left_army_navy",
            "right_army_navy",
        }:
            raise SchemaValidationError(
                "single adjustment requires a supported left/right retractor target"
            )
        if direction not in {"up", "down", "left", "right"} or axis != "none":
            raise SchemaValidationError(
                "single adjustment requires a cardinal direction and axis none"
            )
    elif adjustment_mode == "multi":
        if target_retractor_id not in {"both_malleable", "both_army_navy"}:
            raise SchemaValidationError(
                "multi adjustment requires a supported bilateral retractor target"
            )
        if direction != "none" or axis not in {"left_right", "up_down"}:
            raise SchemaValidationError(
                "multi adjustment requires direction none and one documented axis"
            )
    else:
        raise SchemaValidationError(
            "bed_robot_arm_group adjustment_mode must be single or multi"
        )
    if direction_frame != "surgeon_view":
        raise SchemaValidationError(
            "bed_robot_arm_group direction_frame must be surgeon_view"
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
        "adjustment_mode": adjustment_mode,
        "target_retractor_id": target_retractor_id,
        "direction_frame": direction_frame,
        "direction": direction,
        "axis": axis,
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


def _validate_v5_object_fields(
    value: dict[str, Any],
    *,
    label: str,
    required: set[str],
) -> None:
    missing = sorted(required - set(value))
    extra = sorted(set(value) - required)
    if missing:
        raise SchemaValidationError(f"{label} is missing fields: " + ", ".join(missing))
    if extra:
        raise SchemaValidationError(
            f"{label} has unsupported fields: " + ", ".join(extra)
        )


def _validate_v5_nonempty_string(value: Any, *, label: str) -> str:
    if not isinstance(value, str):
        raise SchemaValidationError(f"{label} must be a string")
    normalized = value.strip()
    if not normalized:
        raise SchemaValidationError(f"{label} must be non-empty")
    return normalized


def _validate_v5_function_call(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise SchemaValidationError("'function_call' must be an object or null")

    required = {"turn_id", "name", "arguments"}
    _validate_v5_object_fields(value, label="function_call", required=required)

    arguments = value["arguments"]
    if not isinstance(arguments, dict):
        raise SchemaValidationError("function_call arguments must be an object")

    return {
        "turn_id": _validate_v5_nonempty_string(
            value["turn_id"], label="function_call turn_id"
        ),
        "name": _validate_v5_nonempty_string(
            value["name"], label="function_call name"
        ),
        "arguments": dict(arguments),
    }


def _validate_v5_humanoid_reply(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise SchemaValidationError("'humanoid_reply' must be an object or null")

    required = {"turn_id", "text", "speak", "timing"}
    _validate_v5_object_fields(value, label="humanoid_reply", required=required)

    text = value["text"]
    if not isinstance(text, str):
        raise SchemaValidationError("humanoid_reply text must be a string")
    if len(text) > 240:
        raise SchemaValidationError("humanoid_reply text must be at most 240 characters")

    speak = value["speak"]
    if not isinstance(speak, bool):
        raise SchemaValidationError("humanoid_reply speak must be a boolean")

    timing = value["timing"]
    if not isinstance(timing, str):
        raise SchemaValidationError("humanoid_reply timing must be a string")
    timing = timing.strip()
    allowed_timings = {
        "immediate",
        "on_function_accepted",
        "on_function_completed",
    }
    if timing not in allowed_timings:
        raise SchemaValidationError(
            "humanoid_reply timing must be immediate, on_function_accepted, "
            "or on_function_completed"
        )

    return {
        "turn_id": _validate_v5_nonempty_string(
            value["turn_id"], label="humanoid_reply turn_id"
        ),
        "text": text.strip(),
        "speak": speak,
        "timing": timing,
    }


def _validate_v5_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if str(payload.get("v", "")) != "5":
        raise SchemaValidationError("missing or unsupported schema version")

    required = {
        "v",
        "phase",
        "tool",
        "intent",
        "mayo",
        "mayo_retrieve",
        "u",
        "sum",
        "bed_robot_arm_group",
        "function_call",
        "humanoid_reply",
    }
    allowed = required
    missing = sorted(required - set(payload))
    extra = sorted(set(payload) - allowed)
    if missing:
        raise SchemaValidationError("schema v5 is missing fields: " + ", ".join(missing))
    if extra:
        raise SchemaValidationError(
            "schema v5 has unsupported fields: " + ", ".join(extra)
        )

    v4_payload = dict(payload)
    v4_payload["v"] = "4"
    v4_payload.pop("function_call")
    v4_payload.pop("humanoid_reply")
    normalized = _validate_v4_payload(v4_payload)
    normalized["v"] = "5"
    normalized["function_call"] = _validate_v5_function_call(
        payload["function_call"]
    )
    normalized["humanoid_reply"] = _validate_v5_humanoid_reply(
        payload["humanoid_reply"]
    )

    function_call = normalized["function_call"]
    humanoid_reply = normalized["humanoid_reply"]
    if (
        function_call is not None
        and humanoid_reply is not None
        and function_call["turn_id"] != humanoid_reply["turn_id"]
    ):
        raise SchemaValidationError(
            "function_call and humanoid_reply turn_id must match"
        )
    return normalized


def _validate_v6_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate the gesture-free actor-log and dialogue response contract."""

    if str(payload.get("v", "")) != "6":
        raise SchemaValidationError("missing or unsupported schema version")
    v5_payload = dict(payload)
    v5_payload["v"] = "5"
    normalized = _validate_v5_payload(v5_payload)
    normalized["v"] = "6"
    return normalized


def validate_payload(payload: dict[str, Any]) -> dict[str, Any]:
    version = str(payload.get("v", ""))
    _reject_retired_vlm_hand_output_fields(payload, version=version or "unknown")
    if version == "1":
        return _validate_v1_payload(payload)
    if version == "2":
        return _validate_v2_payload(payload)
    if version == "3":
        return _validate_v3_payload(payload)
    if version == "4":
        return _validate_v4_payload(payload)
    if version == "5":
        return _validate_v5_payload(payload)
    if version == "6":
        return _validate_v6_payload(payload)
    raise SchemaValidationError("missing or unsupported schema version")


def normalize_raw_text(raw_text: str) -> tuple[str, dict[str, Any]]:
    payload = validate_payload(parse_json_payload(raw_text))
    return json.dumps(payload, separators=(",", ":"), sort_keys=True), payload


def compact_vlm_json_schema(version: str = "1") -> dict[str, Any]:
    if str(version) == "6":
        schema = compact_vlm_json_schema("5")
        schema["properties"] = dict(schema["properties"])
        schema["properties"]["v"] = {"type": "string", "enum": ["6"]}
        return schema
    if str(version) == "5":
        schema = compact_vlm_json_schema("4")
        schema["properties"] = dict(schema["properties"])
        schema["properties"]["v"] = {"type": "string", "enum": ["5"]}
        schema["properties"]["function_call"] = {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "properties": {
                        "turn_id": {"type": "string", "minLength": 1},
                        "name": {"type": "string", "minLength": 1},
                        "arguments": {"type": "object"},
                    },
                    "required": ["turn_id", "name", "arguments"],
                    "additionalProperties": False,
                },
            ]
        }
        schema["properties"]["humanoid_reply"] = {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "properties": {
                        "turn_id": {"type": "string", "minLength": 1},
                        "text": {"type": "string", "maxLength": 240},
                        "speak": {"type": "boolean"},
                        "timing": {
                            "type": "string",
                            "enum": [
                                "immediate",
                                "on_function_accepted",
                                "on_function_completed",
                            ],
                        },
                    },
                    "required": ["turn_id", "text", "speak", "timing"],
                    "additionalProperties": False,
                },
            ]
        }
        schema["required"] = [
            *schema["required"],
            "function_call",
            "humanoid_reply",
        ]
        return schema
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
                        "adjustment_mode": {
                            "type": "string",
                            "enum": ["single", "multi"],
                        },
                        "target_retractor_id": {
                            "type": "string",
                            "enum": [
                                "left_malleable",
                                "right_malleable",
                                "both_malleable",
                                "left_army_navy",
                                "right_army_navy",
                                "both_army_navy",
                            ],
                        },
                        "direction_frame": {
                            "type": "string",
                            "enum": ["surgeon_view"],
                        },
                        "direction": {
                            "type": "string",
                            "enum": ["up", "down", "left", "right", "none"],
                        },
                        "axis": {
                            "type": "string",
                            "enum": ["left_right", "up_down", "none"],
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
                        "adjustment_mode",
                        "target_retractor_id",
                        "direction_frame",
                        "direction",
                        "axis",
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
        phase_pair_array = {
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
        tool_pair_array = {
            **phase_pair_array,
            "items": dict(phase_pair_array["items"]),
            "maxItems": 3,
        }
        return {
            "type": "object",
            "properties": {
                "v": {"type": "string", "enum": ["3"]},
                "phase": phase_pair_array,
                "tool": tool_pair_array,
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
                "sum": {"type": "string", "maxLength": 320},
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
            "u": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "sum": {"type": "string"},
        },
        "required": ["v", "ph", "to", "u", "sum"],
        "additionalProperties": False,
    }
