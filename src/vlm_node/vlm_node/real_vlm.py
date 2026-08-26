"""Real/synthetic VLM node with LM Studio integration and compact state fan-out."""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
import hashlib
from io import BytesIO
import json
import math
import os
from pathlib import Path
import re
import threading
import time
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps
from procedure_spec import (
    BedRobotArmGroupNormalizationError,
    FrozenHandoverNgramPrior,
    ProcedurePriorScorer,
    compact_procedure_prompt,
    get_default_spec_dir,
    infer_retraction_direction,
    load_bundle,
    load_frozen_handover_ngram_prior,
    normalize_retraction_request,
)
import requests
import rclpy
from model_provider_registry import ModelProviderRegistry
from rcl_interfaces.msg import SetParametersResult
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup
from rclpy.clock import Clock, ClockType
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.parameter import Parameter
from rclpy.qos import (
    DurabilityPolicy,
    QoSProfile,
    ReliabilityPolicy,
    qos_profile_sensor_data,
)
from sensor_msgs.msg import CompressedImage
from std_msgs.msg import String
from surgical_perception_msgs.msg import ToolObservation2DArray
from surgical_interop_msgs.msg import GatewayInfo
from surgical_msgs.msg import (
    BedRobotArmGroupActionProposal,
    BedRobotArmGroupCommand,
    BedRobotArmGroupRequest,
    BedRobotArmGroupStatus,
    BTContextSnapshot,
    BTDecision,
    EventDigest,
    HumanoidReply,
    ModelCatalogEntry,
    ModelProviderStatus,
    PhaseEvidence,
    SkillStatus,
    SimulationState,
    SpeechUtterance,
    TTSPlaybackStatus,
    ToolObservation,
    TwinEvent,
    VLMHealth,
    VLMRequestContext,
    VLMResult,
    WorldState,
)
from surgical_msgs.srv import (
    ControlModelRuntime,
    ListModelCatalog,
    ListModels,
    SelectModelProvider,
)

from .common import compact_json
from .lmstudio_client import LMStudioClient
from .prompt_builder import PromptBuilder
from .reply_outbox import ReplyCollisionError, ReplyEnvelope, ReplyOutbox
from .rfdetr_contract import (
    RFDETR_FLIR_SEGMENTED_FRAME_MARKER,
    frame_id_has_rfdetr_marker,
    parse_cam4_semantics_json,
    summarize_rfdetr_tool_observations,
)
from .schema import (
    SchemaValidationError,
    compact_vlm_json_schema,
    normalize_mayo_semantics,
    normalize_raw_text,
    parse_json_payload,
)


PUBLIC_DIGITAL_TWIN_EVENT_TYPES = {
    "RobotTaskStarted",
    "RobotTaskCompleted",
    "RobotGraspedTool",
    "ToolPrepared",
    "ToolHandoverCompleted",
    "ToolReceivedFromSurgeon",
    "ToolSentToCleaner",
    "ToolCleaningProgress",
    "ToolCleaningCompleted",
    "ToolReturnedToTray",
    "PredictedToolReturnedToRack",
    "ProcedureCompleted",
    "InvariantViolation",
    "InvariantViolationIgnored",
}
VLM_PROMPT_MAX_CHARS = 16_000
VLM_TASK_PROFILE_FULL = "full"
VLM_TASK_PROFILE_TOOL_FORECAST_ONLY = "tool_forecast_only"
VLM_TASK_PROFILES = {
    VLM_TASK_PROFILE_FULL,
    VLM_TASK_PROFILE_TOOL_FORECAST_ONLY,
}
ACTOR_LOG_CONTEXT_MAX_CHARS = 2800
ACTOR_LOG_MIN_RUNTIME_CHARS = 512
ACTOR_LOG_EVIDENCE_LIMITS = {
    "speech": 4,
    "skill_status": 4,
}
ACTOR_LOG_EVENT_LIMIT = 4
HANDOVER_SKILL_ACTIONS = {
    "handover",
    "direct_handover",
    "pick_up_and_handover",
    "pick_up_from_mayo_and_handover",
    "put_down_and_handover",
}
NINFER_DIALOGUE_HANDOVER_MIN_CONFIDENCE = 0.5
NINFER_IGNORED_DIALOGUE_EXTENSION_FIELDS = frozenset(
    {"phase_alt", "tool_alt"}
)


def compact_prompt_json(data: dict[str, Any]) -> str:
    """Serialize model context without expanding Korean speech into escapes."""

    return json.dumps(
        data,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def normalize_ranked_tool_distribution(
    rows: list[list[Any]],
    *,
    limit: int = 3,
) -> list[list[Any]]:
    """Return unique ranked tool rows as a display-stable probability mass.

    The public stage rounds probabilities to integer percentages while the
    monitor uses one decimal place.  Allocating whole percentage points with a
    largest-remainder pass makes both views add to 100 (and keeps the wire
    values at a deterministic sum of 1.0) instead of exposing 99/101 rounding
    artifacts to the operator.
    """

    scores: dict[str, float] = {}
    order: dict[str, int] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, list) or len(row) < 2:
            continue
        tool_id = str(row[0]).strip()
        if not tool_id or isinstance(row[1], bool):
            continue
        try:
            confidence = float(row[1])
        except (TypeError, ValueError):
            continue
        if not math.isfinite(confidence) or confidence < 0.0:
            continue
        order.setdefault(tool_id, index)
        scores[tool_id] = max(scores.get(tool_id, 0.0), min(1.0, confidence))
    ranked = sorted(
        scores,
        key=lambda tool_id: (-scores[tool_id], order[tool_id], tool_id),
    )[: max(0, int(limit))]
    if not ranked:
        return []
    total = sum(scores[tool_id] for tool_id in ranked)
    raw_units = [
        (
            scores[tool_id] / total * 100.0
            if total > 0.0
            else 100.0 / len(ranked)
        )
        for tool_id in ranked
    ]
    units = [int(math.floor(value)) for value in raw_units]
    remainder = 100 - sum(units)
    allocation_order = sorted(
        range(len(ranked)),
        key=lambda index: (
            -(raw_units[index] - units[index]),
            index,
        ),
    )
    for index in allocation_order[:remainder]:
        units[index] += 1
    probabilities = [unit / 100.0 for unit in units]
    return [
        [tool_id, probability]
        for tool_id, probability in zip(ranked, probabilities, strict=True)
    ]


def compact_tool_forecast_json_schema() -> dict[str, Any]:
    """Return the constrained response shape for the isolated forecast test."""

    return {
        "type": "object",
        "properties": {
            "tool": {
                "type": "array",
                "items": {
                    "type": "array",
                    "prefixItems": [
                        {"type": "string", "minLength": 1},
                        {
                            "type": "number",
                            "minimum": 0.0,
                            "maximum": 1.0,
                        },
                    ],
                    "minItems": 2,
                    "maxItems": 2,
                },
                "minItems": 1,
                "maxItems": 3,
            },
            "u": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
            },
        },
        "required": ["tool", "u"],
        "additionalProperties": False,
    }


def _repair_ninfer_dialogue_envelope(
    payload: dict[str, Any],
    *,
    claimed_turn_id: str,
) -> dict[str, Any]:
    """Repair only transport-owned omissions before strict validation.

    NInfer does not support constrained JSON decoding. Keep the public schema
    strict, but adapt dialogue transport metadata at the provider boundary.
    Without a claimed turn all dialogue output is suppressed. With a claim,
    omission means null and existing objects inherit only system-owned fields.

    A high-confidence handover intent may be promoted to the equivalent typed
    function call. This does not itself admit an action: the downstream gate
    still requires the independent resolver proposal for the same utterance,
    exact tool arguments, active run scope, and TTL. Function-bound speech is
    delayed until that independent admission is accepted.

    Explicit non-empty turn ids and model-owned content remain untouched so a
    stale turn, unsupported function, or malformed reply still fails closed.
    The only semantic promotion is the bounded handover intent described above;
    delivery metadata is runtime-owned and may be completed or delayed.
    """

    if str(payload.get("v", "")) not in {"5", "6"}:
        return payload

    repaired = dict(payload)
    # NInfer occasionally emits explanatory alternatives beside the canonical
    # fields. They carry no downstream authority and are discarded explicitly;
    # every other unknown field still reaches strict schema validation.
    for field in NINFER_IGNORED_DIALOGUE_EXTENSION_FIELDS:
        repaired.pop(field, None)
    dialogue_fields = ("function_call", "humanoid_reply")
    for field in dialogue_fields:
        if field not in repaired:
            repaired[field] = None

    clean_turn_id = str(claimed_turn_id or "").strip()
    if not clean_turn_id:
        repaired["function_call"] = None
        repaired["humanoid_reply"] = None
        return repaired

    raw_reply = repaired.get("humanoid_reply")
    if isinstance(raw_reply, str):
        reply_text = " ".join(raw_reply.split())[:240]
        if reply_text and reply_text.lower() not in {"null", "none"}:
            repaired["humanoid_reply"] = {
                "turn_id": clean_turn_id,
                "text": reply_text,
            }
        else:
            repaired["humanoid_reply"] = None

    if repaired.get("function_call") is None:
        intent = repaired.get("intent")
        if isinstance(intent, list) and len(intent) == 3:
            intent_name = str(intent[0] or "").strip().lower()
            intent_tool_id = str(intent[1] or "").strip()
            intent_confidence = intent[2]
            try:
                confidence = float(intent_confidence)
            except (TypeError, ValueError):
                confidence = -1.0
            if (
                not isinstance(intent_confidence, bool)
                and math.isfinite(confidence)
                and NINFER_DIALOGUE_HANDOVER_MIN_CONFIDENCE
                <= confidence
                <= 1.0
                and intent_name == "handover"
                and intent_tool_id
            ):
                repaired["function_call"] = {
                    "turn_id": clean_turn_id,
                    "name": "request_tool_handover",
                    "arguments": {"tool_id": intent_tool_id},
                }

    for field in dialogue_fields:
        value = repaired.get(field)
        if not isinstance(value, dict):
            continue
        supplied_turn_id = value.get("turn_id")
        turn_id_is_missing = "turn_id" not in value
        turn_id_is_blank = supplied_turn_id is None or (
            isinstance(supplied_turn_id, str) and not supplied_turn_id.strip()
        )
        if not turn_id_is_missing and not turn_id_is_blank:
            continue
        bound_value = dict(value)
        bound_value["turn_id"] = clean_turn_id
        repaired[field] = bound_value

    reply = repaired.get("humanoid_reply")
    if isinstance(reply, dict):
        normalized_reply = dict(reply)
        if "speak" not in normalized_reply or normalized_reply["speak"] is None:
            normalized_reply["speak"] = True
        function_present = isinstance(repaired.get("function_call"), dict)
        timing = normalized_reply.get("timing")
        if timing is None or (
            isinstance(timing, str) and not timing.strip()
        ):
            normalized_reply["timing"] = (
                "on_function_accepted" if function_present else "immediate"
            )
        elif function_present and str(timing).strip() == "immediate":
            # Delivery timing is runtime policy, not model authority. Delaying
            # the acknowledgement is safer than speaking before admission.
            normalized_reply["timing"] = "on_function_accepted"
        repaired["humanoid_reply"] = normalized_reply
    return repaired


def normalize_tool_forecast_raw_text(
    raw_text: str,
) -> tuple[str, dict[str, Any]]:
    """Validate a tool-only response and adapt it to the public schema-v4 wire."""

    payload = parse_json_payload(raw_text)
    extra_fields = sorted(set(payload) - {"tool", "u"})
    if extra_fields:
        raise SchemaValidationError(
            "tool-only response has unsupported fields: " + ", ".join(extra_fields)
        )
    if "tool" not in payload or "u" not in payload:
        raise SchemaValidationError("tool-only response requires 'tool' and 'u'")

    raw_rows = payload["tool"]
    if not isinstance(raw_rows, list) or not 1 <= len(raw_rows) <= 3:
        raise SchemaValidationError("'tool' must contain 1-3 [tool_id, confidence] rows")
    rows: list[list[Any]] = []
    seen: set[str] = set()
    for item in raw_rows:
        if not isinstance(item, list) or len(item) != 2:
            raise SchemaValidationError(
                "each tool-only item must be [tool_id, confidence]"
            )
        tool_id = str(item[0]).strip()
        if not tool_id:
            raise SchemaValidationError("tool-only tool_id must be non-empty")
        if tool_id in seen:
            raise SchemaValidationError(
                f"tool-only response repeats tool_id {tool_id!r}"
            )
        if isinstance(item[1], bool):
            raise SchemaValidationError("tool-only confidence must be numeric")
        confidence = float(item[1])
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise SchemaValidationError(
                "tool-only confidence must be between 0 and 1"
            )
        seen.add(tool_id)
        rows.append([tool_id, confidence])

    if isinstance(payload["u"], bool):
        raise SchemaValidationError("tool-only uncertainty must be numeric")
    uncertainty = float(payload["u"])
    if not math.isfinite(uncertainty) or not 0.0 <= uncertainty <= 1.0:
        raise SchemaValidationError("tool-only uncertainty must be between 0 and 1")

    # Other schema-v4 fields are transport placeholders generated by code, not
    # model tasks. Empty phase evidence prevents this diagnostic profile from
    # fabricating phase observations while keeping existing ROS messages intact.
    adapted = {
        "v": "4",
        "phase": [],
        "tool": rows,
        "intent": ["none", "", 0.0],
        "mayo": [],
        "mayo_retrieve": ["", 0.0],
        "u": uncertainty,
        "sum": "",
        "bed_robot_arm_group": None,
    }
    normalized_raw, normalized = normalize_raw_text(
        json.dumps(adapted, separators=(",", ":"), sort_keys=True)
    )
    return normalized_raw, normalized


def _tool_count_rows(tool_ids: list[str]) -> list[list[Any]]:
    """Preserve first-seen order while making duplicate tool counts explicit."""

    counts: dict[str, int] = {}
    for raw_tool_id in tool_ids:
        tool_id = str(raw_tool_id or "").strip()
        if tool_id:
            counts[tool_id] = counts.get(tool_id, 0) + 1
    return [[tool_id, count] for tool_id, count in counts.items()]


def build_forecast_constraints(digital_twin: dict[str, Any]) -> dict[str, Any]:
    """Restate existing public DT facts in a forecast-specific vocabulary."""

    if not isinstance(digital_twin, dict):
        return {
            "currently_in_use": [],
            "available_for_next_handover": [],
            "prepositioned": [],
            "mayo_reusable": [],
            "unavailable_for_next_handover": [],
        }

    inventory = digital_twin.get("forecast_inventory", {})
    inventory = inventory if isinstance(inventory, dict) else {}
    tools = digital_twin.get("tools", [])
    tools = tools if isinstance(tools, list) else []
    hands = digital_twin.get("hands", {})
    hands = hands if isinstance(hands, dict) else {}

    currently_in_use = _tool_count_rows(
        [
            str(row.get("id", ""))
            for row in tools
            if isinstance(row, dict)
            and (
                str(row.get("lc", "")) == "surgeon_owned"
                or str(row.get("own", "")) == "surgeon"
            )
        ]
    )
    prepositioned = _tool_count_rows(
        [
            str(hands.get(key, ""))
            for key in ("pre", "rh", "lh")
            if str(hands.get(key, ""))
        ]
    )

    def count_rows(key: str) -> list[list[Any]]:
        rows: list[list[Any]] = []
        for row in inventory.get(key, []) or []:
            if not isinstance(row, list) or len(row) < 2 or not str(row[0]):
                continue
            try:
                count = max(0, int(row[1]))
            except (TypeError, ValueError):
                continue
            if count:
                rows.append([str(row[0]), count])
        return rows

    return {
        "currently_in_use": currently_in_use,
        "available_for_next_handover": count_rows("available"),
        "prepositioned": prepositioned,
        "mayo_reusable": count_rows("mayo_reuse"),
        "unavailable_for_next_handover": count_rows("unavailable"),
    }


def compact_actor_log_procedure_context(
    spec,
    procedure_prompt: dict[str, Any],
) -> dict[str, Any]:
    """Build a non-redundant VLM ontology from any procedure bundle."""
    phase_labels = procedure_prompt.get("phase_labels", {})
    phase_cues = procedure_prompt.get("cues", {})
    phase_exclusions = procedure_prompt.get("exclude", {})
    phase_sequences = procedure_prompt.get("seq", {})
    phase_roles = procedure_prompt.get("roles", {})

    phases: list[dict[str, Any]] = []
    for phase in spec.bundle.phases:
        phase_id = str(phase.id)
        item: dict[str, Any] = {
            "id": phase_id,
            "name": str(phase_labels.get(phase_id, phase_id)),
            "next": list(phase.possible_next),
            "tools": list(phase.expected_instruments),
        }
        cues = list(phase_cues.get(phase_id, []))
        if cues:
            # Adjacent phases are often separated by the second authored cue
            # (for example, stable exposure versus target manipulation).
            # Preserve the complete compact cue list across every procedure;
            # case timestamps and evaluation labels remain excluded.
            item["cue"] = cues
        exclusions = list(phase_exclusions.get(phase_id, []))
        if exclusions:
            item["not"] = exclusions
        sequences = list(phase_sequences.get(phase_id, []))
        if sequences:
            # Recurring edges are easier for a small model to follow as ordered
            # paths. Keep lower-strength branches separate so they cannot break
            # the main chain or masquerade as a case-specific fixed script.
            chains: list[list[str]] = []
            alternatives: list[list[str]] = []
            for row in sequences:
                if not isinstance(row, (list, tuple)) or len(row) < 2:
                    continue
                current_tool = str(row[0])
                next_tool = str(row[1])
                strength = (
                    str(row[3]).lower() if len(row) >= 4 else "medium"
                )
                if strength != "high":
                    alternatives.append([current_tool, next_tool])
                    continue
                if chains and chains[-1][-1] == current_tool:
                    chains[-1].append(next_tool)
                else:
                    chains.append([current_tool, next_tool])
            if chains:
                item["chain"] = chains
            if alternatives:
                item["alt"] = alternatives
        roles = phase_roles.get(phase_id, {})
        if roles:
            item["roles"] = roles
        phases.append(item)

    tools = [
        {
            "id": instrument.id,
            "name": instrument.display_name,
            "role": instrument.role,
        }
        for instrument in spec.bundle.instruments
    ]
    context = {
        "procedure": str(procedure_prompt.get("procedure", "")),
        "phases": phases,
        "tools": tools,
    }
    handover_patterns = procedure_prompt.get("handover_patterns")
    if handover_patterns:
        context["handover"] = handover_patterns
    # Flow and phase-policy prose repeat the per-phase cue/exclusion rows and
    # consume a large immutable prefix on every request. The generic policy is
    # expressed once in the developer instruction; coarse groups remain useful
    # when adjacent visual states cannot be distinguished.
    phase_groups = procedure_prompt.get("phase_groups")
    if phase_groups:
        context["groups"] = phase_groups
    return context


def actor_log_model_context(context: dict[str, Any]) -> dict[str, Any]:
    """Remove prediction-shaped feedback from the context sent to the model."""
    model_context = dict(context)
    # Procedure priors and previous VLM hypotheses are consumed by the
    # reducer/stabilizer. Sending their ranked pairs back to the model creates
    # a self-reinforcing loop and makes model-raw output cease to be an
    # independent observation.
    model_context.pop("candidates", None)
    model_context.pop("previous", None)
    model_context.pop("procedure_prompt_id", None)

    evidence = model_context.get("evidence_window")
    if isinstance(evidence, dict):
        model_context["evidence_window"] = {
            key: evidence[key]
            for key in ACTOR_LOG_EVIDENCE_LIMITS
            if key in evidence
        }
    else:
        model_context.pop("evidence_window", None)

    # The learned artifact may carry fit metadata and raw outcome counts on
    # disk. Only its compact, aggregated distribution is model-visible; case
    # IDs, timestamps, labels, paths, and other provenance never cross this
    # boundary.
    ngram_prior = model_context.get("frozen_ngram_prior")
    if isinstance(ngram_prior, dict):
        compact_candidates: list[list[Any]] = []
        raw_candidates = ngram_prior.get("candidates", [])
        if isinstance(raw_candidates, list):
            for row in raw_candidates[:4]:
                if (
                    not isinstance(row, (list, tuple))
                    or len(row) != 2
                    or not isinstance(row[0], str)
                    or not row[0].strip()
                    or isinstance(row[1], bool)
                    or not isinstance(row[1], (int, float))
                    or not math.isfinite(float(row[1]))
                    or not 0.0 <= float(row[1]) <= 1.0
                ):
                    continue
                compact_candidates.append([row[0], round(float(row[1]), 3)])
        support = ngram_prior.get("support")
        compact_prior: dict[str, Any] = {}
        if isinstance(ngram_prior.get("id"), str) and ngram_prior["id"].strip():
            compact_prior["id"] = ngram_prior["id"]
        if isinstance(ngram_prior.get("match"), str) and ngram_prior["match"].strip():
            compact_prior["match"] = ngram_prior["match"]
        if isinstance(support, int) and not isinstance(support, bool) and support > 0:
            compact_prior["support"] = support
        if compact_candidates:
            compact_prior["candidates"] = compact_candidates
        if {"id", "match", "support", "candidates"}.issubset(compact_prior):
            model_context["frozen_ngram_prior"] = compact_prior
        else:
            model_context.pop("frozen_ngram_prior", None)
    else:
        model_context.pop("frozen_ngram_prior", None)

    visual = model_context.get("visual_input")
    if isinstance(visual, dict):
        compact_visual = {
            key: visual[key]
            for key in (
                "image_source",
                "image_layout",
                "sources",
                "cam4_image_forwarded_to_vlm",
                "cam4_detector_overlay_forwarded_to_vlm",
                "detector_advisory",
                "input_error",
            )
            if key in visual and visual[key] is not None and visual[key] != ""
        }
        sources = visual.get("sources")
        if isinstance(sources, list):
            compact_sources = []
            for source in sources[-3:]:
                if not isinstance(source, dict):
                    continue
                compact_source = {
                    key: source[key]
                    for key in ("role", "stamp_sec", "offset_sec")
                    if key in source and source[key] is not None
                }
                if compact_source:
                    compact_sources.append(compact_source)
            if compact_sources:
                compact_visual["sources"] = compact_sources
        model_context["visual_input"] = compact_visual

    perception = model_context.get("observable_perception")
    if isinstance(perception, dict):
        compact_perception = {
            key: perception[key]
            for key in (
                "schema",
                "source",
                "ground_truth",
                "mask_rle_forwarded_to_vlm",
                "flir_reference_stamp_sec",
                "max_source_skew_sec",
                "tools",
            )
            if key in perception
        }
        # A fresh typed detector row remains useful as a *view-local* fact
        # even when the FLIR panel is absent or cannot be pixel-paired. Keep
        # freshness and visual-pairing diagnostics separate so a compact VLM
        # request cannot turn that fact into a false no-detection result.
        for status_key, allowed_keys in (
            ("freshness", ("status", "received_age_sec")),
            (
                "visual_alignment",
                ("status", "detector_stamp_sec", "offset_sec"),
            ),
        ):
            raw_statuses = perception.get(status_key)
            if not isinstance(raw_statuses, dict):
                continue
            bounded_statuses: dict[str, dict[str, Any]] = {}
            for view_name in ("cam_3", "cam_4"):
                raw_status = raw_statuses.get(view_name)
                if not isinstance(raw_status, dict):
                    continue
                bounded_status = {
                    key: raw_status[key]
                    for key in allowed_keys
                    if key in raw_status and raw_status[key] is not None
                }
                if bounded_status.get("status"):
                    bounded_statuses[view_name] = bounded_status
            if bounded_statuses:
                compact_perception[status_key] = bounded_statuses
        tool_rows = perception.get("tools")
        if isinstance(tool_rows, list):
            compact_tools = []
            for row in tool_rows[:12]:
                if not isinstance(row, dict):
                    continue
                compact_row = {
                    key: row[key]
                    for key in (
                        "name",
                        "id",
                        "count",
                        "max_confidence",
                        "confidence",
                        "stable_sample_count",
                        "stable_duration_sec",
                    )
                    if key in row and row[key] is not None
                }
                if compact_row:
                    compact_tools.append(compact_row)
            compact_perception["tools"] = compact_tools
        detection_views = perception.get("tool_detection_views")
        if isinstance(detection_views, list):
            compact_views: list[dict[str, Any]] = []
            for view in detection_views[:2]:
                if not isinstance(view, dict):
                    continue
                compact_view = {
                    key: view[key]
                    for key in (
                        "view",
                        "source_stamp_sec",
                        "sequence",
                        "model_version",
                        "ontology_version",
                        "detection_status",
                        "truncated",
                    )
                    if key in view and view[key] is not None
                }
                for status_key, allowed_keys in (
                    ("freshness", ("status", "received_age_sec")),
                    (
                        "visual_alignment",
                        ("status", "detector_stamp_sec", "offset_sec"),
                    ),
                ):
                    raw_status = view.get(status_key)
                    if not isinstance(raw_status, dict):
                        continue
                    bounded_status = {
                        key: raw_status[key]
                        for key in allowed_keys
                        if key in raw_status and raw_status[key] is not None
                    }
                    if bounded_status.get("status"):
                        compact_view[status_key] = bounded_status
                compact_instances: list[dict[str, Any]] = []
                instances = view.get("instances")
                if isinstance(instances, list):
                    for instance in instances[:12]:
                        if not isinstance(instance, dict):
                            continue
                        compact_instance = {
                            key: instance[key]
                            for key in (
                                "tool_id",
                                "class_name",
                                "confidence",
                                "bbox_xyxy_norm",
                                "center_uv_norm",
                                "observation_point_uv_norm",
                                "image_region",
                                "depth_m",
                            )
                            if key in instance and instance[key] is not None
                        }
                        if compact_instance:
                            compact_instances.append(compact_instance)
                compact_view["instances"] = compact_instances
                if compact_view:
                    compact_views.append(compact_view)
            compact_perception["tool_detection_views"] = compact_views
        model_context["observable_perception"] = compact_perception

    digital_twin = model_context.get("digital_twin")
    if isinstance(digital_twin, dict):
        compact_twin = dict(digital_twin)
        # The same rows are exposed at the top level when they are actionable.
        compact_twin.pop("bed_robot_arm_groups", None)
        model_context["digital_twin"] = compact_twin
        model_context["forecast_constraints"] = build_forecast_constraints(
            compact_twin
        )

    groups = model_context.get("bed_robot_arm_groups")
    if isinstance(groups, list):
        actionable_groups = [
            row
            for row in groups
            if isinstance(row, dict)
            and (
                not bool(row.get("connected", True))
                or bool(row.get("active_request_id"))
                or bool(row.get("error_code"))
                or bool(row.get("rejection_reason"))
                or str(row.get("state", "")).lower()
                not in {"", "standby", "idle", "ready"}
            )
        ]
        if actionable_groups:
            model_context["bed_robot_arm_groups"] = actionable_groups
        else:
            model_context.pop("bed_robot_arm_groups", None)

    if model_context.get("pending_bed_robot_arm_group_request") is None:
        model_context.pop("pending_bed_robot_arm_group_request", None)

    phase_floor = model_context.get("phase_start_floor")
    if isinstance(phase_floor, dict):
        model_context["phase_start_floor"] = {
            key: phase_floor[key]
            for key in (
                "id",
                "allowed_normal_phase_ids",
                "interrupt_phase_ids",
                "ground_truth",
            )
            if key in phase_floor
        }
    return model_context


def bound_actor_log_context(
    context: dict[str, Any],
    *,
    max_chars: int = ACTOR_LOG_CONTEXT_MAX_CHARS,
) -> dict[str, Any]:
    """Keep dynamic public evidence bounded below small-model context limits."""
    bounded = dict(context)

    # The same immutable ontology is already present in the system prompt.
    bounded.pop("phases", None)
    bounded.pop("tools", None)

    raw_evidence = bounded.get("evidence_window", {})
    evidence = {
        key: raw_evidence[key]
        for key in ACTOR_LOG_EVIDENCE_LIMITS
        if isinstance(raw_evidence, dict) and key in raw_evidence
    }
    for key, limit in ACTOR_LOG_EVIDENCE_LIMITS.items():
        rows = evidence.get(key, [])
        if isinstance(rows, list):
            evidence[key] = rows[-limit:]
    bounded["evidence_window"] = evidence

    digital_twin = dict(bounded.get("digital_twin", {}))
    events = digital_twin.get("events", [])
    if isinstance(events, list):
        digital_twin["events"] = events[-ACTOR_LOG_EVENT_LIMIT:]
    completed_handovers = digital_twin.get("completed_handovers", [])
    if isinstance(completed_handovers, list):
        digital_twin["completed_handovers"] = completed_handovers[-8:]
    tool_requests = digital_twin.get("tool_requests", [])
    if isinstance(tool_requests, list):
        digital_twin["tool_requests"] = tool_requests[-8:]
    bounded["digital_twin"] = digital_twin

    # Preserve the newest admitted speech cue until the end. Old skill/event
    # rows are less useful to the visual observer than current speech evidence.
    shrink_order = (
        (evidence, "skill_status"),
        (digital_twin, "events"),
        (evidence, "speech"),
    )
    while len(compact_prompt_json(actor_log_model_context(bounded))) > max_chars:
        removed = False
        for owner, key in shrink_order:
            rows = owner.get(key, [])
            if isinstance(rows, list) and len(rows) > 1:
                del rows[0]
                removed = True
                break
        if not removed:
            break
    return bounded


def _minimal_typed_tool_detection_context(
    perception: dict[str, Any],
    *,
    max_views: int = 2,
) -> dict[str, Any]:
    """Keep usable CAM3/CAM4 location facts when a Live prompt is tight.

    This is deliberately a *typed* reduction, not a detector-image fallback.
    A single highest-confidence box per view together with its freshness and
    source provenance is more useful to the VLM than a long ambient ASR
    transcript, while remaining below the smallest supported runtime budget.
    """

    if (
        perception.get("schema")
        != "taskplanner.rfdetr_multiview_tool_context.v1"
        or perception.get("source") != "rfdetr_tool_observation_2d"
        or perception.get("ground_truth") is not False
        or perception.get("mask_rle_forwarded_to_vlm") is not False
    ):
        return {}

    compact: dict[str, Any] = {
        key: perception[key]
        for key in (
            "schema",
            "source",
            "ground_truth",
            "mask_rle_forwarded_to_vlm",
            "flir_reference_stamp_sec",
            "max_source_skew_sec",
        )
        if key in perception
    }
    for status_key in ("freshness", "visual_alignment"):
        raw_statuses = perception.get(status_key)
        if not isinstance(raw_statuses, dict):
            continue
        bounded_statuses: dict[str, dict[str, Any]] = {}
        for view_name in ("cam_3", "cam_4"):
            raw_status = raw_statuses.get(view_name)
            if not isinstance(raw_status, dict):
                continue
            allowed = (
                ("status", "received_age_sec")
                if status_key == "freshness"
                else ("status", "detector_stamp_sec", "offset_sec")
            )
            status = {
                key: raw_status[key]
                for key in allowed
                if key in raw_status and raw_status[key] is not None
            }
            if status.get("status"):
                bounded_statuses[view_name] = status
        if bounded_statuses:
            compact[status_key] = bounded_statuses

    views: list[dict[str, Any]] = []
    raw_views = perception.get("tool_detection_views")
    if isinstance(raw_views, list):
        for raw_view in raw_views[: max(0, int(max_views))]:
            if not isinstance(raw_view, dict):
                continue
            view = {
                key: raw_view[key]
                for key in (
                    "view",
                    "source_stamp_sec",
                    "sequence",
                    "model_version",
                    "ontology_version",
                    "detection_status",
                    "truncated",
                    "freshness",
                    "visual_alignment",
                )
                if key in raw_view and raw_view[key] is not None
            }
            raw_instances = raw_view.get("instances")
            instances: list[dict[str, Any]] = []
            if isinstance(raw_instances, list):
                for raw_instance in raw_instances[:1]:
                    if not isinstance(raw_instance, dict):
                        continue
                    instance = {
                        key: raw_instance[key]
                        for key in (
                            "tool_id",
                            "class_name",
                            "confidence",
                            "bbox_xyxy_norm",
                            "center_uv_norm",
                            "observation_point_uv_norm",
                            "image_region",
                            "depth_m",
                        )
                        if key in raw_instance and raw_instance[key] is not None
                    }
                    if instance:
                        instances.append(instance)
            view["instances"] = instances
            if view.get("view") and view.get("detection_status"):
                views.append(view)
    compact["tool_detection_views"] = views
    # A one-view emergency reduction must not claim a second view is present
    # without its matching typed row.  Keep the status maps paired with the
    # exact views that crossed the bounded prompt boundary.
    included_views = {
        str(view.get("view", ""))
        for view in views
        if str(view.get("view", "")) in {"cam_3", "cam_4"}
    }
    for status_key in ("freshness", "visual_alignment"):
        statuses = compact.get(status_key)
        if not isinstance(statuses, dict):
            continue
        compact[status_key] = {
            view_name: status
            for view_name, status in statuses.items()
            if view_name in included_views
        }
    return compact


def actor_log_request_context(
    context: dict[str, Any],
    *,
    static_prompt_chars: int,
    total_prompt_chars_max: int = VLM_PROMPT_MAX_CHARS,
) -> dict[str, Any]:
    """Build a valid request context under the complete prompt budget."""

    available_chars = int(total_prompt_chars_max) - int(static_prompt_chars)
    if available_chars < ACTOR_LOG_MIN_RUNTIME_CHARS:
        raise RuntimeError(
            "static VLM prompt does not reserve enough runtime evidence space"
        )
    bounded = bound_actor_log_context(context, max_chars=available_chars)
    model_context = actor_log_model_context(bounded)
    pending_dialogue_turn = model_context.get("pending_dialogue_turn")
    if len(compact_prompt_json(model_context)) <= available_chars:
        return model_context

    # A very large inventory can exceed the budget even after history rows are
    # reduced to one. Keep current public request evidence and the smallest
    # state needed to interpret it, instead of truncating JSON or dropping the
    # latest speech cue.
    evidence = model_context.get("evidence_window", {})
    if not isinstance(evidence, dict):
        evidence = {}
    minimal: dict[str, Any] = {
        "proc": model_context.get("proc", ""),
        "phase_search_mode": model_context.get("phase_search_mode", ""),
        "evidence_window": {
            key: rows[-1:]
            for key in ("speech", "skill_status")
            if isinstance((rows := evidence.get(key)), list) and rows
        },
    }
    for key in (
        "phase_start_floor",
        "visual_input",
        "observable_perception",
        "forecast_constraints",
        "frozen_ngram_prior",
        "pending_bed_robot_arm_group_request",
        "pending_dialogue_turn",
    ):
        if key in model_context:
            minimal[key] = model_context[key]
    digital_twin = model_context.get("digital_twin")
    if isinstance(digital_twin, dict):
        minimal["digital_twin"] = {
            key: digital_twin[key]
            for key in (
                "hands",
                "forecast_inventory",
                "tools",
                "completed_handovers",
                "tool_requests",
            )
            if key in digital_twin
        }

    # Do this before shrinking structured perception.  Ambient or accidental
    # long-form speech is a weaker VLM cue than a fresh bounded RF-DETR box,
    # and should never evict CAM3/CAM4 typed location evidence from a request.
    for key, text_keys in (
        ("speech", ("text",)),
        ("skill_status", ("detail", "message", "text")),
    ):
        rows = evidence.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            for text_key in text_keys:
                if text_key in row:
                    row[text_key] = str(row[text_key])[:160]

    minimal_twin = minimal.get("digital_twin", {})
    if (
        len(compact_prompt_json(minimal)) > available_chars
        and isinstance(minimal_twin, dict)
    ):
        minimal_twin.pop("tools", None)

    optional_removal_order = (
        "pending_bed_robot_arm_group_request",
        "phase_start_floor",
    )
    for key in optional_removal_order:
        if len(compact_prompt_json(minimal)) <= available_chars:
            break
        minimal.pop(key, None)

    # Detector rows are advisory and can expand abruptly when many instances
    # enter CAM4. Preserve the pixels, newest speech/request evidence, and
    # detector provenance while reducing optional rows instead of failing the
    # latest-frame loop for every incoming frame.
    evidence = minimal.get("evidence_window", {})
    if not isinstance(evidence, dict):
        evidence = {}
        minimal["evidence_window"] = evidence
    perception = minimal.get("observable_perception", {})
    if not isinstance(perception, dict):
        perception = {}
    visual = minimal.get("visual_input", {})
    if not isinstance(visual, dict):
        visual = {}

    if len(compact_prompt_json(minimal)) > available_chars:
        evidence.pop("skill_status", None)

    tools = perception.get("tools")
    if isinstance(tools, list):
        while tools and len(compact_prompt_json(minimal)) > available_chars:
            tools.pop()

    detection_views = perception.get("tool_detection_views")
    if isinstance(detection_views, list):
        # Each view is confidence-sorted by the typed contract.  Drop the
        # weakest remaining row first, while retaining the view's alignment
        # state so a tight prompt never turns missing detector evidence into a
        # false absence claim.
        while len(compact_prompt_json(minimal)) > available_chars:
            candidates = [
                view.get("instances")
                for view in detection_views
                if isinstance(view, dict)
                and isinstance(view.get("instances"), list)
                # Preserve one actual typed location fact per observed view.
                # The final budget fallback can reduce whole views if needed,
                # but must not silently turn a fresh detection into an empty
                # detector frame.
                and len(view.get("instances")) > 1
            ]
            if not candidates:
                break
            min(candidates, key=len).pop()

    if len(compact_prompt_json(minimal)) > available_chars:
        visual.pop("sources", None)

    if len(compact_prompt_json(minimal)) > available_chars and perception:
        alignment = perception.get("alignment")
        reduced_perception = _minimal_typed_tool_detection_context(perception)
        if not reduced_perception:
            reduced_perception = {
                key: perception[key]
                for key in ("source", "ground_truth")
                if key in perception
            }
        if isinstance(alignment, dict) and alignment.get("status"):
            reduced_perception["alignment"] = {
                "status": alignment["status"]
            }
        minimal["observable_perception"] = reduced_perception

    if len(compact_prompt_json(minimal)) > available_chars and visual:
        minimal["visual_input"] = {
            key: visual[key]
            for key in (
                "image_source",
                "image_layout",
                "cam4_image_forwarded_to_vlm",
                "cam4_detector_overlay_forwarded_to_vlm",
                "detector_advisory",
                "input_error",
            )
            if key in visual and visual[key] is not None and visual[key] != ""
        }

    if isinstance(minimal_twin, dict):
        for history_key in ("completed_handovers", "tool_requests"):
            history = minimal_twin.get(history_key)
            while (
                isinstance(history, list)
                and len(history) > 2
                and len(compact_prompt_json(minimal)) > available_chars
            ):
                history.pop(0)

    # Keep the newest public cue but bound free-form ASR and status text. Exact
    # transcripts remain on their ROS topic and in trace logs for auditing.
    for key, text_keys in (("speech", ("text",)),):
        rows = evidence.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            for text_key in text_keys:
                if text_key in row:
                    row[text_key] = str(row[text_key])[:160]

    for key in ("phase_search_mode", "proc"):
        if len(compact_prompt_json(minimal)) <= available_chars:
            break
        minimal.pop(key, None)

    if len(compact_prompt_json(minimal)) > available_chars:
        # This final form prefers actual typed detector location evidence over
        # a transcript.  It remains valid JSON rather than a lossy string
        # slice, and keeps the CAM3/CAM4 facts that are supplied to the VLM.
        latest_evidence = {
            key: rows[-1:]
            for key in ("speech",)
            if isinstance((rows := evidence.get(key)), list) and rows
        }
        typed_perception = _minimal_typed_tool_detection_context(perception)
        minimal = (
            {"observable_perception": typed_perception}
            if typed_perception
            else {"evidence_window": latest_evidence}
        )
        if isinstance(pending_dialogue_turn, dict):
            minimal["pending_dialogue_turn"] = pending_dialogue_turn
        if (
            typed_perception
            and len(compact_prompt_json({**minimal, "evidence_window": latest_evidence}))
            <= available_chars
        ):
            minimal["evidence_window"] = latest_evidence
        if not typed_perception and "frozen_ngram_prior" in model_context:
            minimal["frozen_ngram_prior"] = model_context["frozen_ngram_prior"]
        for rows in latest_evidence.values():
            for row in rows:
                if isinstance(row, dict) and "text" in row:
                    row["text"] = str(row["text"])[:80]
    if len(compact_prompt_json(minimal)) > available_chars:
        typed_perception = _minimal_typed_tool_detection_context(
            perception,
            max_views=1,
        )
        if isinstance(pending_dialogue_turn, dict):
            minimal = {"pending_dialogue_turn": pending_dialogue_turn}
            perception_only = {"observable_perception": typed_perception}
            if typed_perception and len(
                compact_prompt_json({**minimal, **perception_only})
            ) <= available_chars:
                minimal.update(perception_only)
        else:
            minimal = (
                {"observable_perception": typed_perception}
                if typed_perception
                else {"evidence_window": {}}
            )
    return minimal
PUBLIC_REQUEST_MAX_AGE_SEC = 6.0
DEFAULT_CAM4_CROP_XYWH_NORM = (0.32, 0.18, 0.62, 0.78)
# A side-by-side FLIR + CAM4 frame needs enough pixels for the Mayo
# context to remain useful.  This stays bounded below the single-view limit.
DEFAULT_MULTIVIEW_IMAGE_MAX_SIDE_PX = 1024
MAX_PUBLIC_PERCEPTION_INSTANCES = 24
IMAGE_PAIR_BUFFER_LENGTH = 32
PERCEPTION_PAIR_BUFFER_LENGTH = 64
CLINICAL_ANALYSIS_MAX_CHARS = 320
CAM4_MAYO_MIN_CONFIDENCE = 0.65
CAM4_MAYO_MIN_STABLE_SAMPLES = 3
CAM4_MAYO_MIN_STABLE_DURATION_SEC = 0.25
CAM4_MAYO_STABILITY_WINDOW_SEC = 0.75
CANONICAL_MAYO_POLICY_LOCATION = "mayo_stand"
INFERENCE_TRIGGER_PERIODIC_LIVE = "periodic_live"
INFERENCE_TRIGGER_SPEECH = "speech"
DIALOGUE_AUTOMATIC_ATTEMPT_LIMIT = 2
INFERENCE_TRIGGER_REPLAY_FRAME = "replay_frame"
INFERENCE_TRIGGER_SOURCE_FRAME = "source_frame_live"
INFERENCE_TRIGGER_FORCED = "forced"
INFERENCE_FAILURE_HISTORY_LENGTH = 32
INFERENCE_FAILURE_LOG_REPEAT_SEC = 30.0


def normalize_clinical_analysis(value: Any) -> str:
    """Keep the public summary clinical and remove internal reducer markers."""

    text = " ".join(str(value or "").split())
    if not text:
        return (
            "Current visual evidence is insufficient for a definitive "
            "clinical interpretation."
        )
    internal_markers = (
        "candidate-stabilized",
        "public-sequence-anchor=",
        "phase-bootstrap-waiting-for-public-sequence",
        "public-tool-anchor=",
        "actor-log fallback",
    )
    clinical_parts = [
        part.strip()
        for part in text.split(";")
        if part.strip()
        and not any(marker in part for marker in internal_markers)
    ]
    cleaned = "; ".join(clinical_parts).strip()
    if not cleaned:
        cleaned = (
            "Current visual evidence is insufficient for a definitive "
            "clinical interpretation."
        )
    return cleaned[:CLINICAL_ANALYSIS_MAX_CHARS]


def is_model_ready_visual_source(image_source: str) -> bool:
    """Accept the fused visual contract and its FLIR-only fallback."""

    return str(image_source).strip() in {
        "flir_cam4_rfdetr_segmented",
        "flir_cam4_raw_fallback",
        "flir_rfdetr_segmented",
        "flir_raw_fallback",
    }


@dataclass(frozen=True, slots=True)
class ImageSample:
    received_monotonic: float
    stamp_sec: int
    stamp_nanosec: int
    frame_id: str
    data: bytes
    mime_type: str


@dataclass(frozen=True, slots=True)
class ModelImage:
    label: str
    data: bytes
    mime_type: str
    stamp_sec: int
    stamp_nanosec: int
    frame_id: str


def model_input_signature(
    *,
    runtime_epoch: int,
    request_config: dict[str, Any],
    system_prompt: str,
    developer_instruction: str,
    request_context_json: str,
    observation_metadata: Any,
    images: list[tuple[str, bytes, str]],
) -> str:
    """Hash the exact public model request while preserving source-time evidence."""

    digest = hashlib.sha256()

    def update_segment(name: str, value: bytes | str) -> None:
        name_bytes = name.encode("utf-8")
        value_bytes = value if isinstance(value, bytes) else value.encode("utf-8")
        digest.update(len(name_bytes).to_bytes(4, "big"))
        digest.update(name_bytes)
        digest.update(len(value_bytes).to_bytes(8, "big"))
        digest.update(value_bytes)

    update_segment("runtime_epoch", str(max(0, int(runtime_epoch))))
    update_segment(
        "request_config",
        json.dumps(
            request_config,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        ),
    )
    update_segment("system_prompt", system_prompt)
    update_segment("developer_instruction", developer_instruction)
    update_segment("request_context_json", request_context_json)
    update_segment(
        "observation_metadata",
        json.dumps(
            observation_metadata,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        ),
    )
    for index, (label, image_bytes, mime_type) in enumerate(images):
        update_segment(f"image[{index}].label", str(label))
        update_segment(f"image[{index}].mime_type", str(mime_type))
        update_segment(f"image[{index}].bytes", bytes(image_bytes))
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class InferenceAdmission:
    disposition: str
    trigger: str

    @property
    def started(self) -> bool:
        return self.disposition == "started"


@dataclass(frozen=True, slots=True)
class InferenceFailure:
    sequence: int
    trigger: str
    mode: str
    error: str
    image_source: str
    latency_sec: float
    retry_count: int
    recorded_monotonic: float


class InferenceBackpressure:
    """Run one inference while retaining one highest-priority pending request.

    Visual frame triggers are intentionally coalesced to the newest frame, but
    a queued speech trigger represents a FIFO dialogue obligation and must not
    be overwritten by the next camera frame.  Forced operator work remains the
    only trigger with a higher priority than speech.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._in_flight = False
        self._current_trigger = ""
        self._pending_trigger = ""
        self._coalesced_count = 0

    @staticmethod
    def _normalize_trigger(trigger: str) -> str:
        return str(trigger or INFERENCE_TRIGGER_FORCED).strip()

    @staticmethod
    def _trigger_priority(trigger: str) -> int:
        normalized = InferenceBackpressure._normalize_trigger(trigger)
        if normalized == INFERENCE_TRIGGER_FORCED:
            return 3
        if normalized == INFERENCE_TRIGGER_SPEECH:
            return 2
        if normalized == INFERENCE_TRIGGER_PERIODIC_LIVE:
            return 0
        return 1

    def _replace_pending(self, normalized: str) -> InferenceAdmission:
        """Merge one trigger into the occupied pending slot under the lock."""

        if not self._pending_trigger:
            self._pending_trigger = normalized
            return InferenceAdmission("queued", normalized)
        self._coalesced_count += 1
        if self._trigger_priority(normalized) < self._trigger_priority(
            self._pending_trigger
        ):
            return InferenceAdmission("preserved", self._pending_trigger)
        self._pending_trigger = normalized
        return InferenceAdmission("coalesced", normalized)

    def queue(self, trigger: str) -> InferenceAdmission:
        """Queue work without letting frame churn erase dialogue work."""

        normalized = self._normalize_trigger(trigger)
        with self._lock:
            return self._replace_pending(normalized)

    def queue_if_empty(self, trigger: str) -> InferenceAdmission:
        """Queue work unless an equal- or higher-priority trigger is pending."""

        normalized = self._normalize_trigger(trigger)
        with self._lock:
            if self._pending_trigger:
                if self._trigger_priority(normalized) > self._trigger_priority(
                    self._pending_trigger
                ):
                    return self._replace_pending(normalized)
                return InferenceAdmission("preserved", self._pending_trigger)
            self._pending_trigger = normalized
            return InferenceAdmission("queued", normalized)

    def begin(self) -> str | None:
        """Claim queued work for the single inference consumer."""

        with self._lock:
            if self._in_flight or not self._pending_trigger:
                return None
            self._in_flight = True
            self._current_trigger = self._pending_trigger
            self._pending_trigger = ""
            return self._current_trigger

    def request(self, trigger: str) -> InferenceAdmission:
        """Start immediately when idle, otherwise merge pending work."""

        normalized = self._normalize_trigger(trigger)
        with self._lock:
            if not self._in_flight:
                self._in_flight = True
                self._current_trigger = normalized
                self._pending_trigger = ""
                return InferenceAdmission("started", normalized)
            return self._replace_pending(normalized)

    def complete(self, *, drop_pending: bool = False) -> str | None:
        with self._lock:
            if not self._in_flight:
                return None
            if drop_pending:
                self._pending_trigger = ""
            if self._pending_trigger:
                self._current_trigger = self._pending_trigger
                self._pending_trigger = ""
                return self._current_trigger
            self._in_flight = False
            self._current_trigger = ""
            return None

    def defer_until_ready(self, fallback_trigger: str = "") -> str:
        """Release the consumer while retaining the newest retryable work."""

        normalized_fallback = (
            self._normalize_trigger(fallback_trigger)
            if str(fallback_trigger).strip()
            else ""
        )
        with self._lock:
            if normalized_fallback:
                if not self._pending_trigger:
                    self._pending_trigger = normalized_fallback
                elif self._trigger_priority(
                    normalized_fallback
                ) > self._trigger_priority(self._pending_trigger):
                    self._coalesced_count += 1
                    self._pending_trigger = normalized_fallback
            self._in_flight = False
            self._current_trigger = ""
            return self._pending_trigger

    def clear_pending(self) -> None:
        with self._lock:
            self._pending_trigger = ""

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "in_flight": self._in_flight,
                "current_trigger": self._current_trigger,
                "pending_trigger": self._pending_trigger,
                "coalesced_count": self._coalesced_count,
            }


@dataclass(frozen=True, slots=True)
class DialogueTurn:
    """One admitted final speech turn that may produce one humanoid reply."""

    epoch: int
    procedure_run_id: str
    utterance_id: str
    turn_id: str
    text: str
    received_at: float
    speaker_role: str = ""
    language: str = ""
    source: str = ""


@dataclass(frozen=True, slots=True)
class DialogueClaim:
    turn_id: str
    correlation_id: str
    epoch: int


def stable_dialogue_reply_id(
    procedure_run_id: str,
    utterance_id: str,
    *,
    gateway_instance_id: str = "",
) -> str:
    """Return the durable logical-turn key used by the TTS outbox.

    A VLM process epoch is intentionally excluded: the same admitted ASR
    utterance redelivered after a VLM restart must still map to one audible
    reply within the procedure run.
    """

    material = json.dumps(
        {
            "gateway_instance_id": str(gateway_instance_id or "").strip(),
            "procedure_run_id": str(procedure_run_id or "").strip(),
            "utterance_id": str(utterance_id or "").strip(),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return "humanoid-reply:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


class DialogueTurnGate:
    """FIFO admission plus an in-process at-most-once reply commit gate.

    Visual inference triggers may be coalesced, but admitted speech turns are
    never coalesced. A model result can commit only the FIFO head claimed by
    its own inference correlation id. Invalid results release the claim while
    keeping the turn pending for a later frame.
    """

    def __init__(self, *, epoch: int = 0, max_committed: int = 0) -> None:
        self._lock = threading.Lock()
        self._epoch = max(0, int(epoch))
        self._pending: deque[DialogueTurn] = deque()
        self._pending_ids: set[str] = set()
        self._claim: DialogueClaim | None = None
        self._claim_attempts: dict[str, int] = {}
        self._committed: dict[str, str] = {}
        # Zero keeps the complete current-run ledger. Gateway/run changes reset
        # the gate, so this stays naturally bounded by one procedure.
        self._max_committed = max(0, int(max_committed))

    @property
    def epoch(self) -> int:
        with self._lock:
            return self._epoch

    @staticmethod
    def _turn_id(epoch: int, procedure_run_id: str, utterance_id: str) -> str:
        run_scope = str(procedure_run_id or "no-run").strip() or "no-run"
        return f"{max(0, int(epoch))}:{run_scope}:{utterance_id}"

    def enqueue(
        self,
        *,
        utterance_id: str,
        text: str,
        procedure_run_id: str = "",
        received_at: float = 0.0,
        speaker_role: str = "",
        language: str = "",
        source: str = "",
    ) -> DialogueTurn | None:
        clean_utterance_id = str(utterance_id or "").strip()
        clean_text = " ".join(str(text or "").split())[:240]
        if not clean_utterance_id or not clean_text:
            return None
        with self._lock:
            turn_id = self._turn_id(
                self._epoch,
                procedure_run_id,
                clean_utterance_id,
            )
            if turn_id in self._pending_ids or turn_id in self._committed:
                return None
            turn = DialogueTurn(
                epoch=self._epoch,
                procedure_run_id=str(procedure_run_id or "").strip(),
                utterance_id=clean_utterance_id,
                turn_id=turn_id,
                text=clean_text,
                received_at=float(received_at or 0.0),
                speaker_role=str(speaker_role or "").strip(),
                language=str(language or "").strip(),
                source=str(source or "").strip(),
            )
            self._pending.append(turn)
            self._pending_ids.add(turn_id)
            return turn

    def next_pending(self) -> DialogueTurn | None:
        with self._lock:
            return self._pending[0] if self._pending else None

    def claim(self, *, turn_id: str, correlation_id: str) -> DialogueTurn | None:
        clean_turn_id = str(turn_id or "").strip()
        clean_correlation_id = str(correlation_id or "").strip()
        if not clean_turn_id or not clean_correlation_id:
            return None
        with self._lock:
            if not self._pending:
                return None
            turn = self._pending[0]
            if turn.epoch != self._epoch or turn.turn_id != clean_turn_id:
                return None
            if self._claim is not None:
                if (
                    self._claim.turn_id == clean_turn_id
                    and self._claim.correlation_id == clean_correlation_id
                    and self._claim.epoch == self._epoch
                ):
                    return turn
                return None
            self._claim = DialogueClaim(
                turn_id=clean_turn_id,
                correlation_id=clean_correlation_id,
                epoch=self._epoch,
            )
            self._claim_attempts[clean_turn_id] = (
                self._claim_attempts.get(clean_turn_id, 0) + 1
            )
            return turn

    def attempt_count(self, turn_id: str) -> int:
        clean_turn_id = str(turn_id or "").strip()
        with self._lock:
            return max(0, int(self._claim_attempts.get(clean_turn_id, 0)))

    def release(self, *, correlation_id: str) -> bool:
        clean_correlation_id = str(correlation_id or "").strip()
        with self._lock:
            if (
                self._claim is None
                or self._claim.correlation_id != clean_correlation_id
            ):
                return False
            self._claim = None
            return True

    def commit(
        self,
        *,
        turn_id: str,
        correlation_id: str,
        reply: str | None,
        valid: bool,
    ) -> bool:
        clean_turn_id = str(turn_id or "").strip()
        clean_correlation_id = str(correlation_id or "").strip()
        clean_reply = " ".join(str(reply or "").split())
        with self._lock:
            claim = self._claim
            if claim is None:
                return False
            if claim.correlation_id != clean_correlation_id:
                return False
            if (
                claim.epoch != self._epoch
                or claim.turn_id != clean_turn_id
                or not self._pending
                or self._pending[0].turn_id != clean_turn_id
            ):
                self._claim = None
                return False
            if not valid or not clean_reply:
                self._claim = None
                return False
            if clean_turn_id in self._committed:
                self._claim = None
                return False
            self._pending.popleft()
            self._pending_ids.discard(clean_turn_id)
            self._claim = None
            self._claim_attempts.pop(clean_turn_id, None)
            self._committed[clean_turn_id] = clean_reply[:240]
            while (
                self._max_committed > 0
                and len(self._committed) > self._max_committed
            ):
                oldest = next(iter(self._committed))
                self._committed.pop(oldest, None)
            return True

    def reset(self, *, epoch: int) -> None:
        with self._lock:
            self._epoch = max(0, int(epoch))
            self._pending.clear()
            self._pending_ids.clear()
            self._claim = None
            self._claim_attempts.clear()
            self._committed.clear()


class InferenceFailureBackoff:
    """Bound retries after a fast transport failure without blocking callbacks."""

    def __init__(self, initial_sec: float = 0.5, maximum_sec: float = 2.0) -> None:
        self._lock = threading.Lock()
        self._initial_sec = max(0.0, float(initial_sec))
        self._maximum_sec = max(self._initial_sec, float(maximum_sec))
        self._consecutive_failures = 0
        self._not_before_monotonic = 0.0

    def configure(self, *, initial_sec: float, maximum_sec: float) -> None:
        with self._lock:
            self._initial_sec = max(0.0, float(initial_sec))
            self._maximum_sec = max(self._initial_sec, float(maximum_sec))
            if self._not_before_monotonic > 0.0:
                self._not_before_monotonic = min(
                    self._not_before_monotonic,
                    time.monotonic() + self._maximum_sec,
                )

    def record_failure(self, *, now_monotonic: float | None = None) -> float:
        now = time.monotonic() if now_monotonic is None else float(now_monotonic)
        with self._lock:
            exponent = min(self._consecutive_failures, 30)
            delay = min(
                self._maximum_sec,
                self._initial_sec * (2.0**exponent),
            )
            self._consecutive_failures += 1
            self._not_before_monotonic = max(
                self._not_before_monotonic,
                now + delay,
            )
            return delay

    def record_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._not_before_monotonic = 0.0

    def remaining(self, *, now_monotonic: float | None = None) -> float:
        now = time.monotonic() if now_monotonic is None else float(now_monotonic)
        with self._lock:
            return max(0.0, self._not_before_monotonic - now)

    def snapshot(self, *, now_monotonic: float | None = None) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic() if now_monotonic is None else float(now_monotonic)
            return {
                "consecutive_failures": self._consecutive_failures,
                "remaining_sec": max(0.0, self._not_before_monotonic - now),
            }


def image_sample_stamp_sec(sample: ImageSample) -> float:
    return (
        float(sample.stamp_sec)
        + float(sample.stamp_nanosec) / 1_000_000_000.0
    )


def image_samples_are_aligned(
    first: ImageSample,
    second: ImageSample,
    *,
    max_skew_sec: float,
) -> bool:
    return (
        abs(image_sample_stamp_sec(first) - image_sample_stamp_sec(second))
        <= max(0.0, float(max_skew_sec)) + 1.0e-9
    )


def should_trigger_replay_frame(
    last_stamp_sec: float | None,
    current_stamp_sec: float,
    publish_period_sec: float,
) -> bool:
    """Use source-image time, rather than executor timing, for replay cadence."""

    if last_stamp_sec is None or current_stamp_sec < last_stamp_sec:
        return True
    return (
        current_stamp_sec - last_stamp_sec + 1.0e-9
        >= max(publish_period_sec, 0.0)
    )


def should_run_periodic_live_frame(
    last_stamp_sec: float | None,
    current_stamp_sec: float,
) -> bool:
    """Reject repeated timer inference over the same recorded frame."""

    if last_stamp_sec is None or current_stamp_sec < last_stamp_sec:
        return True
    return current_stamp_sec > last_stamp_sec + 1.0e-9


def should_trigger_source_time_live_frame(
    last_stamp_sec: float | None,
    current_stamp_sec: float,
    publish_period_sec: float,
) -> bool:
    """Tolerate camera cadence jitter around the requested live period."""

    if last_stamp_sec is None or current_stamp_sec < last_stamp_sec:
        return True
    period_sec = max(0.0, float(publish_period_sec))
    tolerance_sec = min(0.05, period_sec * 0.05)
    return (
        current_stamp_sec - last_stamp_sec + tolerance_sec + 1.0e-9
        >= period_sec
    )


def source_frame_is_fresh(
    now_sec: float,
    image_stamp_sec: float,
    max_lag_sec: float,
) -> bool:
    """Reject visual evidence that has fallen behind the replay source clock."""

    if max_lag_sec <= 0.0:
        return True
    return now_sec - image_stamp_sec <= max_lag_sec + 1.0e-9


def source_frame_timestamp_rejection_reason(
    now_sec: float,
    image_stamp_sec: float,
    max_lag_sec: float,
    max_future_skew_sec: float,
) -> str:
    """Return a fail-closed reason for a Live camera source timestamp.

    ``source_frame_is_fresh`` above is retained for replay compatibility: a
    replay source clock can legitimately reset and has historically treated a
    future image as fresh.  Live input cannot make that assumption.  It must
    have a finite, positive source stamp within both stale and future bounds.
    """

    values = (now_sec, image_stamp_sec, max_lag_sec, max_future_skew_sec)
    if not all(math.isfinite(float(value)) for value in values):
        return "non_finite_source_timestamp"
    if float(now_sec) <= 0.0:
        return "unavailable_source_clock"
    if float(image_stamp_sec) <= 0.0:
        return "missing_source_timestamp"
    if float(max_lag_sec) <= 0.0:
        return "invalid_source_max_lag"
    age_sec = float(now_sec) - float(image_stamp_sec)
    if age_sec > float(max_lag_sec) + 1.0e-9:
        return f"stale_source_frame:{age_sec:.3f}s"
    if age_sec < -max(0.0, float(max_future_skew_sec)) - 1.0e-9:
        return f"future_source_frame:{-age_sec:.3f}s"
    return ""


def should_use_open_set_phase_bootstrap(
    observation_count: int,
    max_observations: int,
    explicit_start_phase: bool,
) -> bool:
    """Temporarily remove temporal priors when the camera joins mid-procedure."""

    return (
        not explicit_start_phase
        and max(0, int(max_observations)) > 0
        and max(0, int(observation_count)) < max(0, int(max_observations))
    )


def explicit_phase_start_floor_context(
    phase_id: str,
    *,
    explicit_start_phase: bool,
    normal_phase_ids: list[str],
    interrupt_phase_ids: list[str],
) -> dict[str, Any] | None:
    """Expose the selected normal-phase floor without feeding back VLM output."""

    normalized_phase = str(phase_id or "").strip()
    normal_ids = [
        str(item or "").strip()
        for item in normal_phase_ids
        if str(item or "").strip()
    ]
    if not explicit_start_phase or normalized_phase not in normal_ids:
        return None
    start_index = normal_ids.index(normalized_phase)
    return {
        "id": normalized_phase,
        "source": "operator_or_procedure_selected_start",
        "ground_truth": False,
        "policy": "normal_phase_floor",
        "allowed_normal_phase_ids": normal_ids[start_index:],
        "interrupt_phase_ids": [
            str(item or "").strip()
            for item in interrupt_phase_ids
            if str(item or "").strip()
        ],
    }


def bound_image_for_model(
    image_bytes: bytes,
    mime_type: str,
    max_side_px: int,
) -> tuple[bytes, str]:
    """Bound vision-token cost while preserving the published source frame."""

    limit = max(0, int(max_side_px))
    if limit <= 0:
        return image_bytes, mime_type
    try:
        with Image.open(BytesIO(image_bytes)) as source:
            if max(source.size) <= limit:
                return image_bytes, mime_type
            resized = source.copy()
            resized.thumbnail((limit, limit), Image.Resampling.LANCZOS)
            output = BytesIO()
            if mime_type == "image/png":
                resized.save(output, format="PNG", optimize=True)
                return output.getvalue(), "image/png"
            if resized.mode not in {"RGB", "L"}:
                resized = resized.convert("RGB")
            resized.save(
                output,
                format="JPEG",
                quality=88,
                optimize=True,
            )
            return output.getvalue(), "image/jpeg"
    except (OSError, ValueError):
        return image_bytes, mime_type


def normalize_crop_xywh(
    crop_xywh_norm: tuple[float, float, float, float] | list[float],
) -> tuple[float, float, float, float]:
    """Clamp a normalized crop to a non-empty region inside the source image."""

    if len(crop_xywh_norm) != 4:
        raise ValueError("CAM4 crop must contain x, y, width, height")
    values = tuple(float(value) for value in crop_xywh_norm)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("CAM4 crop values must be finite")
    x, y, width, height = values
    x = min(max(x, 0.0), 1.0)
    y = min(max(y, 0.0), 1.0)
    width = min(max(width, 1.0e-6), 1.0 - x)
    height = min(max(height, 1.0e-6), 1.0 - y)
    if width <= 0.0 or height <= 0.0:
        raise ValueError("CAM4 crop must overlap the source image")
    return x, y, width, height


def crop_image_normalized(
    image: Image.Image,
    crop_xywh_norm: tuple[float, float, float, float] | list[float],
) -> Image.Image:
    """Crop an image using normalized coordinates with deterministic rounding."""

    x, y, width, height = normalize_crop_xywh(crop_xywh_norm)
    left = min(image.width - 1, max(0, math.floor(x * image.width)))
    top = min(image.height - 1, max(0, math.floor(y * image.height)))
    right = min(image.width, max(left + 1, math.ceil((x + width) * image.width)))
    bottom = min(
        image.height,
        max(top + 1, math.ceil((y + height) * image.height)),
    )
    return image.crop((left, top, right, bottom))


def dynamic_cam4_crop_xywh(
    perception_summaries: list[dict[str, Any]],
    *,
    fallback_xywh_norm: tuple[float, float, float, float] | list[float],
    padding_norm: float = 0.08,
    minimum_width_norm: float = 0.45,
    minimum_height_norm: float = 0.55,
) -> tuple[float, float, float, float]:
    """Frame a padded union of observable instances without using annotations."""

    boxes: list[tuple[float, float, float, float]] = []
    for summary in perception_summaries:
        if not isinstance(summary, dict):
            continue
        rows = summary.get("instances", [])
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            bbox = _bounded_number_list(
                row.get("bbox_xywh_norm"),
                length=4,
                minimum=0.0,
                maximum=1.0,
            )
            if not bbox:
                continue
            x, y, width, height = bbox
            if width <= 0.0 or height <= 0.0:
                continue
            boxes.append((x, y, min(1.0, x + width), min(1.0, y + height)))
    if not boxes:
        return normalize_crop_xywh(fallback_xywh_norm)

    padding = min(max(float(padding_norm), 0.0), 0.5)
    left = max(0.0, min(box[0] for box in boxes) - padding)
    top = max(0.0, min(box[1] for box in boxes) - padding)
    right = min(1.0, max(box[2] for box in boxes) + padding)
    bottom = min(1.0, max(box[3] for box in boxes) + padding)
    minimum_width = min(max(float(minimum_width_norm), 0.05), 1.0)
    minimum_height = min(max(float(minimum_height_norm), 0.05), 1.0)

    center_x = (left + right) / 2.0
    center_y = (top + bottom) / 2.0
    width = max(right - left, minimum_width)
    height = max(bottom - top, minimum_height)
    left = min(max(center_x - width / 2.0, 0.0), 1.0 - width)
    top = min(max(center_y - height / 2.0, 0.0), 1.0 - height)
    return normalize_crop_xywh((left, top, width, height))


def _resize_panel_to_shared_height(
    source: Image.Image,
    *,
    width: int,
    height: int,
) -> Image.Image:
    """Resize a panel whose width was derived from its native aspect ratio."""

    return source.convert("RGB").resize(
        (max(1, int(width)), max(1, int(height))),
        Image.Resampling.LANCZOS,
    )


def compose_flir_cam4_for_model(
    flir_bytes: bytes,
    flir_mime_type: str,
    cam4_bytes: bytes,
    cam4_mime_type: str,
    *,
    cam4_crop_xywh_norm: tuple[float, float, float, float] | list[float],
    max_side_px: int,
    cam4_overlay_bytes: bytes | None = None,
    cam4_overlay_mime_type: str = "",
) -> tuple[bytes, str]:
    """Build one bounded, labeled FLIR-left/CAM4-right model image.

    RF-DETR is intentionally *not* painted into this image.  The model gets
    its CAM3/CAM4 tool boxes through the bounded typed observation context, so
    a rendered overlay cannot silently become an unversioned detector-input
    proxy.  The optional overlay arguments remain accepted for API stability
    with old callers, but are deliberately ignored.
    """

    del (
        flir_mime_type,
        cam4_mime_type,
        cam4_overlay_bytes,
        cam4_overlay_mime_type,
    )
    limit = max(320, int(max_side_px or 0))
    with (
        Image.open(BytesIO(flir_bytes)) as flir_source,
        Image.open(BytesIO(cam4_bytes)) as cam4_source,
    ):
        flir = flir_source.convert("RGB")
        cam4 = cam4_source.convert("RGB")
        cam4_crop = crop_image_normalized(
            cam4.convert("RGB"),
            cam4_crop_xywh_norm,
        )
        canvas_width = limit
        label_height = max(24, min(42, canvas_width // 32))
        # Both observed views use their native aspect ratio at one shared
        # height.  The former fixed-width split letterboxed CAM4 below its
        # frame, reducing the Mayo evidence that reaches the VLM.
        flir_aspect = flir.width / max(flir.height, 1)
        cam4_aspect = cam4_crop.width / max(cam4_crop.height, 1)
        content_height = max(
            1,
            round(canvas_width / max(flir_aspect + cam4_aspect, 1.0e-6)),
        )
        flir_width = min(
            canvas_width - 1,
            max(1, round(content_height * flir_aspect)),
        )
        cam4_width = canvas_width - flir_width
        flir_panel = _resize_panel_to_shared_height(
            flir,
            width=flir_width,
            height=content_height,
        )
        cam4_panel = _resize_panel_to_shared_height(
            cam4_crop,
            width=cam4_width,
            height=content_height,
        )
        canvas = Image.new(
            "RGB",
            (canvas_width, label_height + content_height),
            "black",
        )
        canvas.paste(flir_panel, (0, label_height))
        canvas.paste(cam4_panel, (flir_width, label_height))
        draw = ImageDraw.Draw(canvas)
        try:
            font = ImageFont.truetype(
                "DejaVuSans-Bold.ttf",
                max(14, label_height - 12),
            )
        except OSError:
            font = ImageFont.load_default()
        draw.text((10, 5), "FLIR surgical field", fill="white", font=font)
        draw.text(
            (flir_width + 10, 5),
            "CAM4 Mayo instruments",
            fill="white",
            font=font,
        )
        output = BytesIO()
        canvas.save(output, format="JPEG", quality=88, optimize=True)
        return bound_image_for_model(
            output.getvalue(),
            "image/jpeg",
            limit,
        )


def crop_cam4_for_model(
    cam4_bytes: bytes,
    *,
    cam4_crop_xywh_norm: tuple[float, float, float, float] | list[float],
    max_side_px: int,
) -> tuple[bytes, str]:
    """Encode the configured CAM4 crop for single-view fallback."""

    with Image.open(BytesIO(cam4_bytes)) as source:
        cropped = crop_image_normalized(
            source.convert("RGB"),
            cam4_crop_xywh_norm,
        )
        # Keep JPEG dimensions aligned for stricter FFmpeg-backed vision
        # decoders while preserving every observed pixel in the crop.
        aligned_width = max(16, math.ceil(cropped.width / 16) * 16)
        aligned_height = max(16, math.ceil(cropped.height / 16) * 16)
        if (aligned_width, aligned_height) != cropped.size:
            cropped = ImageOps.expand(
                cropped,
                border=(
                    0,
                    0,
                    aligned_width - cropped.width,
                    aligned_height - cropped.height,
                ),
                fill="black",
            )
        output = BytesIO()
        cropped.save(output, format="JPEG", quality=88, optimize=True)
    return bound_image_for_model(
        output.getvalue(),
        "image/jpeg",
        max_side_px,
    )


def _finite_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _bounded_number_list(
    value: Any,
    *,
    length: int,
    minimum: float,
    maximum: float,
) -> list[float]:
    if not isinstance(value, list) or len(value) != length:
        return []
    numbers = [_finite_float(item) for item in value]
    if any(item is None for item in numbers):
        return []
    return [
        round(min(max(float(item), minimum), maximum), 6)
        for item in numbers
        if item is not None
    ]


def summarize_public_perception_json(
    raw_json: str,
    *,
    kind: str,
    max_instances: int = MAX_PUBLIC_PERCEPTION_INSTANCES,
) -> dict[str, Any]:
    """Bound public detector output and intentionally discard full mask RLE."""

    try:
        payload = json.loads(str(raw_json or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    frame = payload.get("frame", {})
    if not isinstance(frame, dict):
        frame = {}
    image = payload.get("image", {})
    if not isinstance(image, dict):
        image = {}
    width = max(0, int(_finite_float(image.get("width")) or 0))
    height = max(0, int(_finite_float(image.get("height")) or 0))
    rows = frame.get("instances", [])
    if not isinstance(rows, list):
        rows = []
    bounded_rows: list[dict[str, Any]] = []
    for row in rows[: max(0, int(max_instances))]:
        if not isinstance(row, dict):
            continue
        summary: dict[str, Any] = {}
        class_name = str(row.get("class_name", "")).strip()[:80]
        if class_name:
            summary["class_name"] = class_name
        for key in ("class_id", "track_id", "instance_id"):
            value = _finite_float(row.get(key))
            if value is not None:
                summary[key] = int(value)
        bbox = _bounded_number_list(
            row.get("bbox_xywh_norm"),
            length=4,
            minimum=0.0,
            maximum=1.0,
        )
        if bbox:
            summary["bbox_xywh_norm"] = bbox
        if kind == "segmentation":
            area_px = _finite_float(row.get("mask_area_px"))
            if area_px is not None and area_px >= 0.0:
                summary["mask_area_px"] = int(area_px)
                if width > 0 and height > 0:
                    summary["mask_area_norm"] = round(
                        min(max(area_px / float(width * height), 0.0), 1.0),
                        6,
                    )
            centroid = _bounded_number_list(
                row.get("mask_centroid_norm"),
                length=2,
                minimum=0.0,
                maximum=1.0,
            )
            if centroid:
                summary["mask_centroid_norm"] = centroid
        if summary:
            bounded_rows.append(summary)
    timestamp = _finite_float(payload.get("bag_timestamp_sec"))
    if timestamp is None:
        timestamp = _finite_float(frame.get("source_timestamp_sec"))
    result: dict[str, Any] = {
        "kind": kind,
        "source": "cam4_public_detector",
        "frame_id": str(frame.get("frame_id", "")).strip()[:120],
        "image": {"width": width, "height": height},
        "instances": bounded_rows,
        "truncated": len(rows) > len(bounded_rows),
    }
    if timestamp is not None:
        result["timestamp_sec"] = round(float(timestamp), 6)
    if kind == "bboxes":
        result["bbox_format"] = "xywh_norm"
        result["confidence_available"] = bool(
            payload.get("confidence_available", False)
        )
    else:
        result["segmentation_summary_only"] = True
        result["full_mask_rle_included"] = False
    return result


class RealVLMNode(Node):
    def _causal_now_sec(self) -> float:
        """Return ROS/source time for evidence ordering, wall time in unit stubs."""

        try:
            return float(self.get_clock().now().nanoseconds) / 1_000_000_000.0
        except Exception:
            return time.time()

    def __init__(self) -> None:
        super().__init__("real_vlm_node")
        self._state_callback_group = MutuallyExclusiveCallbackGroup()
        self._visual_callback_group = MutuallyExclusiveCallbackGroup()
        self._inference_callback_group = MutuallyExclusiveCallbackGroup()
        self._inference_backpressure = InferenceBackpressure()
        self._inference_wakeup = threading.Event()
        self._inference_shutdown = threading.Event()
        self._inference_worker: threading.Thread | None = None
        self._inference_retry_lock = threading.Lock()
        self._inference_retry_timer: threading.Timer | None = None
        self._inference_failures: deque[InferenceFailure] = deque(
            maxlen=INFERENCE_FAILURE_HISTORY_LENGTH
        )
        self._inference_failure_count = 0
        self.declare_parameter("spec_dir", str(get_default_spec_dir()))
        self.declare_parameter("base_url", "http://127.0.0.1:8001")
        self.declare_parameter("provider_id", os.environ.get("VLM_PROVIDER_ID", "vllm"))
        self.declare_parameter("api_key", "")
        self.declare_parameter("model_id", "unsloth/gemma-4-E4B-it-NVFP4")
        self.declare_parameter("api_mode", "openai_compat")
        self.declare_parameter("request_timeout_sec", 20.0)
        self.declare_parameter("max_output_tokens", 320)
        self.declare_parameter("temperature", 0.0)
        self.declare_parameter("top_p", 1.0)
        self.declare_parameter("generation_seed", 0)
        self.declare_parameter("response_format", "none")
        self.declare_parameter("reasoning_effort", "")
        self.declare_parameter("task_profile", VLM_TASK_PROFILE_FULL)
        self.declare_parameter("retry_count", 2)
        self.declare_parameter("transport_failure_backoff_initial_sec", 0.5)
        self.declare_parameter("transport_failure_backoff_max_sec", 2.0)
        self.declare_parameter("publish_period_sec", 2.0)
        self.declare_parameter("response_mode", "live")
        self.declare_parameter("source_time_triggered_live", True)
        self.declare_parameter("model_input_max_source_lag_sec", 0.0)
        self.declare_parameter("require_source_frame_timestamp", False)
        self.declare_parameter("model_input_max_source_future_skew_sec", 1.0)
        self.declare_parameter("replay_response_path", "")
        self.declare_parameter("output_prefix", "/vlm")
        self.declare_parameter("context_prefix", "/context")
        self.declare_parameter("field_image_topic", "/surgery/images/field/compressed")
        self.declare_parameter("raw_field_image_topic", "")
        self.declare_parameter("cam4_image_topic", "")
        # Retained only so existing launch files can continue to route the
        # operator overlay. RealVLM no longer subscribes to or composites this
        # raster; typed ToolObservation2DArray is the detector contract.
        self.declare_parameter("cam4_overlay_image_topic", "")
        self.declare_parameter("tray_image_topic", "/surgery/images/tray/compressed")
        self.declare_parameter("synthetic_image_topic", "/surgery/images/synthetic/compressed")
        self.declare_parameter(
            "composite_image_topic",
            "/surgery/images/vlm/composite/compressed",
        )
        self.declare_parameter("cam4_dynamic_crop", True)
        self.declare_parameter("require_cam4_image", False)
        self.declare_parameter("multiview_max_skew_sec", 0.1)
        self.declare_parameter("cam4_crop_x_norm", DEFAULT_CAM4_CROP_XYWH_NORM[0])
        self.declare_parameter("cam4_crop_y_norm", DEFAULT_CAM4_CROP_XYWH_NORM[1])
        self.declare_parameter("cam4_crop_width_norm", DEFAULT_CAM4_CROP_XYWH_NORM[2])
        self.declare_parameter("cam4_crop_height_norm", DEFAULT_CAM4_CROP_XYWH_NORM[3])
        self.declare_parameter("cam4_crop_padding_norm", 0.08)
        self.declare_parameter("cam4_crop_min_width_norm", 0.45)
        self.declare_parameter("cam4_crop_min_height_norm", 0.55)
        self.declare_parameter("perception_bboxes_topic", "")
        self.declare_parameter("perception_segmentation_topic", "")
        self.declare_parameter("cam4_semantics_topic", "")
        # These typed arrays are the primary instrument-location evidence for
        # Live VLM.  They carry RF-DETR boxes as structured data, not an
        # annotated image.  The legacy CAM4 semantic summary remains an
        # explicit fallback for older Debug/replay deployments.
        self.declare_parameter("cam3_tool_observations_topic", "")
        self.declare_parameter("cam4_tool_observations_topic", "")
        self.declare_parameter("cam3_tool_observations_expected_model_version", "")
        self.declare_parameter("cam4_tool_observations_expected_model_version", "")
        self.declare_parameter("allow_legacy_cam4_semantics_fallback", True)
        self.declare_parameter("perception_stale_sec", 3.0)
        self.declare_parameter("perception_image_max_skew_sec", 0.2)
        self.declare_parameter(
            "perception_max_instances",
            MAX_PUBLIC_PERCEPTION_INSTANCES,
        )
        self.declare_parameter("image_stale_sec", 5.0)
        self.declare_parameter("image_max_side_px", 1280)
        self.declare_parameter(
            "multiview_image_max_side_px",
            DEFAULT_MULTIVIEW_IMAGE_MAX_SIDE_PX,
        )
        self.declare_parameter("require_field_image", True)
        self.declare_parameter("enable_text_only_dialogue", False)
        self.declare_parameter("require_rfdetr_applied_field_image", False)
        # Deprecated visual-admission flag. It is accepted for Debug/replay
        # compatibility but never makes an overlay VLM detector evidence.
        self.declare_parameter("require_rfdetr_cam4_overlay", False)
        self.declare_parameter("context_mode", "world")
        self.declare_parameter("open_set_phase_bootstrap_observations", 0)
        self.declare_parameter("handover_ngram_prior_enabled", True)
        self.declare_parameter(
            "tts_reply_outbox_path",
            os.environ.get(
                "TASKPLANNER_TTS_REPLY_OUTBOX_PATH",
                f"/tmp/taskplanner-vlm-reply-outbox-{os.getuid()}.sqlite3",
            ),
        )
        self.declare_parameter("tts_playback_status_topic", "/tts/playback_status")
        self.declare_parameter("tts_reply_retry_period_sec", 1.0)
        self.declare_parameter("gateway_info_timeout_sec", 3.0)

        self._prompt_builder = PromptBuilder()
        self._active = False
        self._world: WorldState | None = None
        self._simulation: SimulationState | None = None
        self._latest_bt: BTDecision | None = None
        self._recent_events: deque[EventDigest] = deque(maxlen=6)
        self._completed_handover_history: deque[dict[str, Any]] = deque(maxlen=8)
        self._validated_tool_request_history: deque[dict[str, Any]] = deque(maxlen=8)
        self._latest_images: dict[str, ImageSample] = {}
        self._image_buffers: dict[str, deque[ImageSample]] = {}
        self._latest_perception: dict[str, tuple[float, dict[str, Any]]] = {}
        self._perception_buffers: dict[
            str,
            deque[tuple[float, dict[str, Any]]],
        ] = {}
        self._perception_enabled = True
        self._perception_generation = 0
        self._current_visual_input: dict[str, Any] = {}
        self._current_image_input_error = ""
        self._last_source_frame_rejection = ""
        self._current_perception_reference_stamp_sec: float | None = None
        self._last_good_raw = ""
        self._last_good_payload: dict[str, Any] | None = None
        self._replay_payload: dict[str, Any] | None = None
        self._recent_speech: deque[dict[str, Any]] = deque(maxlen=10)
        self._recent_skill_statuses: deque[dict[str, Any]] = deque(maxlen=8)
        self._latest_bed_robot_arm_group_request: BedRobotArmGroupRequest | None = None
        self._last_bed_robot_arm_group_proposal_request_id = ""
        self._last_simulation_bundle = ""
        self._last_vlm_phase = ""
        self._last_authoritative_phase = ""
        self._last_lifecycle_control_signature: tuple[str, str] | None = None
        self._phase_bootstrap_id = ""
        self._phase_bootstrap_observation_count = 0
        self._phase_bootstrap_explicit = False
        self._phase_entered_wall_sec = self._causal_now_sec()
        self._last_replay_image_stamp_sec: float | None = None
        self._last_periodic_live_image_stamp_sec: float | None = None
        self._last_submitted_live_image_stamp_sec: float | None = None
        self._last_source_live_trigger_stamp_sec: float | None = None
        self._source_live_trigger_pending = False
        self._source_live_trigger_lock = threading.Lock()
        self._visual_frame_generation = 0
        self._last_submitted_visual_generation = -1
        # A wall-clock boot epoch keeps observations from a restarted VLM
        # strictly newer than delayed results from its previous process.
        self._model_input_epoch = max(1, time.time_ns())
        self._vlm_result_sequence = 0
        self._gateway_instance_id = ""
        self._procedure_run_id = ""
        self._dialogue_turn_gate = DialogueTurnGate(epoch=self._model_input_epoch)
        self._last_submitted_model_input_key = ""
        self._exact_duplicate_suppressed_count = 0
        self._fast_cam4_mayo_published_tools: set[str] = set()
        self._fast_cam4_mayo_last_seen_stamp_sec: dict[str, float] = {}
        self._oracle_scenario = []
        self._oracle_scenario_length = 0
        self._oracle_bootstrap_tick = 0
        self._oracle_tick = 0
        self._developer_instruction = (
            "Return exactly one valid JSON object and nothing else. "
            "All object keys must be double-quoted strings: \"v\", \"ph\", \"to\", \"u\", \"sum\". "
            "Never omit quotes around keys. Never use true/false/null. "
            "Confidence values and u must be numeric floats between 0.0 and 1.0, not strings and not booleans. "
            "Use exact tool ids and location ids from context. "
            "If the image is black, blank, or text-only, keep the current context phase at low confidence, emit no tool observations, and set u to 1.0."
        )

        self._provider_model_selections: dict[str, str] = {}
        initial_base_url = str(self.get_parameter("base_url").value)
        initial_api_key = str(
            self.get_parameter("api_key").value or os.environ.get("VLM_API_KEY", "")
        )
        self._provider_registry = ModelProviderRegistry.from_environment(
            legacy_base_url=initial_base_url,
            legacy_api_key=initial_api_key,
        )
        self._load_parameters()
        self.add_on_set_parameters_callback(self._on_parameters_changed)
        self._reply_outbox = ReplyOutbox(
            str(self.get_parameter("tts_reply_outbox_path").value)
        )
        self._reply_retry_period_sec = float(
            self.get_parameter("tts_reply_retry_period_sec").value
        )
        if self._reply_retry_period_sec < 0.2:
            self._reply_outbox.close()
            raise ValueError("tts_reply_retry_period_sec must be at least 0.2")
        self._gateway_info_timeout_sec = float(
            self.get_parameter("gateway_info_timeout_sec").value
        )
        if self._gateway_info_timeout_sec <= 0.0:
            self._reply_outbox.close()
            raise ValueError("gateway_info_timeout_sec must be positive")
        self._gateway_info_last_seen_monotonic: float | None = None
        self._gateway_info_last_instance_id = ""
        self._gateway_info_last_revision = -1
        self._gateway_info_last_source_stamp_ns = 0

        self._phase_summary_pub = self.create_publisher(String, self._topic(self._context_prefix, "phase_summary"), 10)
        self._tool_summary_pub = self.create_publisher(String, self._topic(self._context_prefix, "tool_lifecycle_summary"), 10)
        self._event_digest_pub = self.create_publisher(EventDigest, self._topic(self._context_prefix, "event_digest"), 20)
        self._bt_snapshot_pub = self.create_publisher(BTContextSnapshot, self._topic(self._context_prefix, "bt_context_snapshot"), 10)
        self._request_context_pub = self.create_publisher(VLMRequestContext, self._topic(self._context_prefix, "vlm_request_context"), 10)
        self._result_pub = self.create_publisher(VLMResult, self._topic(self._output_prefix, "result"), 10)
        self._humanoid_reply_pub = self.create_publisher(
            HumanoidReply,
            self._topic(self._output_prefix, "humanoid_reply"),
            QoSProfile(
                depth=64,
                reliability=ReliabilityPolicy.RELIABLE,
                durability=DurabilityPolicy.TRANSIENT_LOCAL,
            ),
        )
        self._model_raw_result_pub = self.create_publisher(
            VLMResult,
            self._topic(self._output_prefix, "model_raw_result"),
            10,
        )
        self._health_pub = self.create_publisher(VLMHealth, self._topic(self._output_prefix, "health"), 10)
        self._phase_pub = self.create_publisher(PhaseEvidence, self._topic(self._output_prefix, "phase_evidence"), 10)
        self._tool_pub = self.create_publisher(ToolObservation, self._topic(self._output_prefix, "tool_observations"), 30)
        self._composite_image_pub = self.create_publisher(
            CompressedImage,
            self._composite_image_topic,
            4,
        )
        self._bed_robot_arm_group_proposal_pub = self.create_publisher(
            BedRobotArmGroupActionProposal,
            self._topic(self._output_prefix, "bed_robot_arm_group_proposal"),
            10,
        )
        self._model_catalog_service = self.create_service(ListModels, "~/list_models", self._on_list_models)
        self._provider_catalog_service = self.create_service(
            ListModelCatalog,
            "~/list_model_catalog",
            self._on_list_model_catalog,
        )
        self._provider_select_service = self.create_service(
            SelectModelProvider,
            "~/select_model_provider",
            self._on_select_model_provider,
        )
        self._provider_control_service = self.create_service(
            ControlModelRuntime,
            "~/control_model_runtime",
            self._on_control_model_runtime,
        )

        state_group = self._state_callback_group
        visual_group = self._visual_callback_group
        self.create_subscription(
            WorldState,
            "/twin/world_state",
            self._on_world,
            20,
            callback_group=state_group,
        )
        self.create_subscription(
            SimulationState,
            "/simulation/state",
            self._on_simulation,
            20,
            callback_group=state_group,
        )
        self.create_subscription(
            TwinEvent,
            "/twin/events",
            self._on_event,
            50,
            callback_group=state_group,
        )
        self.create_subscription(
            SkillStatus,
            "/skill/status",
            self._on_skill_status,
            50,
            callback_group=state_group,
        )
        self.create_subscription(
            GatewayInfo,
            "/surgery/gateway_info",
            self._on_gateway_info,
            QoSProfile(
                depth=1,
                reliability=ReliabilityPolicy.RELIABLE,
                durability=DurabilityPolicy.TRANSIENT_LOCAL,
            ),
            callback_group=state_group,
        )
        self.create_subscription(
            TTSPlaybackStatus,
            str(self.get_parameter("tts_playback_status_topic").value),
            self._on_tts_playback_status,
            QoSProfile(
                depth=32,
                reliability=ReliabilityPolicy.RELIABLE,
                durability=DurabilityPolicy.TRANSIENT_LOCAL,
            ),
            callback_group=state_group,
        )
        self.create_subscription(
            SpeechUtterance,
            "/surgery/audio/admitted_utterance",
            self._on_speech_utterance,
            20,
            callback_group=state_group,
        )
        self.create_subscription(
            String,
            "/surgery/audio/request_text",
            self._on_request_text,
            20,
            callback_group=state_group,
        )
        self.create_subscription(
            BedRobotArmGroupRequest,
            "/surgeon/bed_robot_arm_group_request",
            self._on_bed_robot_arm_group_request,
            20,
            callback_group=state_group,
        )
        self.create_subscription(
            BedRobotArmGroupStatus,
            "/bed_robot_arm_group/status",
            self._on_bed_robot_arm_group_status,
            20,
            callback_group=state_group,
        )
        self.create_subscription(
            CompressedImage,
            self._field_image_topic,
            self._make_image_cb("field"),
            qos_profile_sensor_data,
            callback_group=visual_group,
        )
        if self._raw_field_image_topic:
            self.create_subscription(
                CompressedImage,
                self._raw_field_image_topic,
                self._make_image_cb("raw_field"),
                qos_profile_sensor_data,
                callback_group=visual_group,
            )
        if self._cam4_image_topic:
            self.create_subscription(
                CompressedImage,
                self._cam4_image_topic,
                self._make_image_cb("cam4"),
                qos_profile_sensor_data,
                callback_group=visual_group,
            )
        self.create_subscription(
            CompressedImage,
            self._tray_image_topic,
            self._make_image_cb("tray"),
            qos_profile_sensor_data,
            callback_group=visual_group,
        )
        self.create_subscription(
            CompressedImage,
            self._synthetic_image_topic,
            self._make_image_cb("synthetic"),
            qos_profile_sensor_data,
            callback_group=visual_group,
        )
        if self._perception_bboxes_topic:
            self.create_subscription(
                String,
                self._perception_bboxes_topic,
                self._make_perception_cb("bboxes"),
                20,
                callback_group=visual_group,
            )
        if self._perception_segmentation_topic:
            self.create_subscription(
                String,
                self._perception_segmentation_topic,
                self._make_perception_cb("segmentation"),
                20,
                callback_group=visual_group,
            )
        if (
            self._allow_legacy_cam4_semantics_fallback
            and self._cam4_semantics_topic
        ):
            self.create_subscription(
                String,
                self._cam4_semantics_topic,
                self._make_perception_cb("cam4_semantics"),
                20,
                callback_group=visual_group,
            )
        if self._cam3_tool_observations_topic:
            self.create_subscription(
                ToolObservation2DArray,
                self._cam3_tool_observations_topic,
                self._make_rfdetr_tool_observation_cb("cam_3"),
                # The upstream may publish perception as reliable or sensor
                # best-effort. A best-effort request is compatible with both
                # and is correct for newest-frame location evidence.
                qos_profile_sensor_data,
                callback_group=visual_group,
            )
        if self._cam4_tool_observations_topic:
            self.create_subscription(
                ToolObservation2DArray,
                self._cam4_tool_observations_topic,
                self._make_rfdetr_tool_observation_cb("cam_4"),
                qos_profile_sensor_data,
                callback_group=visual_group,
            )
        self.create_subscription(
            String,
            "/surgery/perception/rfdetr/health",
            self._on_perception_health,
            20,
            callback_group=visual_group,
        )
        self.create_subscription(
            String,
            "/simulation/control_state",
            self._on_control,
            20,
            callback_group=state_group,
        )

        self._timer = self.create_timer(
            self._publish_period_sec,
            self._tick,
            callback_group=self._inference_callback_group,
            clock=Clock(clock_type=ClockType.STEADY_TIME),
        )
        self._reply_retry_timer = self.create_timer(
            self._reply_retry_period_sec,
            self._republish_pending_humanoid_replies,
            callback_group=state_group,
            clock=Clock(clock_type=ClockType.STEADY_TIME),
        )
        self._gateway_info_watchdog_timer = self.create_timer(
            min(0.5, max(0.1, self._gateway_info_timeout_sec / 4.0)),
            self._on_gateway_info_watchdog,
            callback_group=state_group,
            clock=Clock(clock_type=ClockType.STEADY_TIME),
        )
        self._inference_worker = threading.Thread(
            target=self._inference_worker_loop,
            name="real-vlm-latest-frame",
            daemon=True,
        )
        self._inference_worker.start()

    def _load_parameters(self, overrides: dict[str, Any] | None = None) -> None:
        override_values = overrides or {}

        def param_value(name: str) -> Any:
            return override_values.get(name, self.get_parameter(name).value)

        self._spec_dir = str(param_value("spec_dir"))
        self._spec = load_bundle(self._spec_dir)
        configured_base_url = str(param_value("base_url"))
        configured_api_key = str(
            param_value("api_key") or os.environ.get("VLM_API_KEY", "")
        )
        provider = self._provider_registry.resolve(
            str(param_value("provider_id")),
            fallback_base_url=configured_base_url,
            fallback_api_key=configured_api_key,
        )
        self._provider_id = provider.provider_id
        self._base_url = provider.base_url
        self._api_key = provider.api_key
        self._model_id = str(param_value("model_id"))
        self._provider_model_selections[self._provider_id] = self._model_id
        self._api_mode = str(param_value("api_mode"))
        self._request_timeout_sec = float(param_value("request_timeout_sec"))
        self._max_output_tokens = int(param_value("max_output_tokens"))
        self._temperature = float(param_value("temperature"))
        self._top_p = float(param_value("top_p"))
        generation_seed = int(param_value("generation_seed"))
        self._generation_seed = generation_seed if generation_seed >= 0 else None
        self._response_format = str(param_value("response_format")).strip().lower()
        self._reasoning_effort = str(param_value("reasoning_effort")).strip()
        self._task_profile = str(param_value("task_profile")).strip().lower()
        if self._task_profile not in VLM_TASK_PROFILES:
            raise ValueError(
                "task_profile must be one of " + ", ".join(sorted(VLM_TASK_PROFILES))
            )
        self._retry_count = int(param_value("retry_count"))
        self._transport_failure_backoff_initial_sec = max(
            0.0,
            float(param_value("transport_failure_backoff_initial_sec")),
        )
        self._transport_failure_backoff_max_sec = max(
            self._transport_failure_backoff_initial_sec,
            float(param_value("transport_failure_backoff_max_sec")),
        )
        failure_backoff = getattr(self, "_transport_failure_backoff", None)
        if failure_backoff is None:
            self._transport_failure_backoff = InferenceFailureBackoff(
                self._transport_failure_backoff_initial_sec,
                self._transport_failure_backoff_max_sec,
            )
        else:
            failure_backoff.configure(
                initial_sec=self._transport_failure_backoff_initial_sec,
                maximum_sec=self._transport_failure_backoff_max_sec,
            )
        self._publish_period_sec = float(param_value("publish_period_sec"))
        self._response_mode = str(param_value("response_mode"))
        self._source_time_triggered_live = bool(
            param_value("source_time_triggered_live")
        )
        self._model_input_max_source_lag_sec = max(
            0.0,
            float(param_value("model_input_max_source_lag_sec")),
        )
        self._require_source_frame_timestamp = bool(
            param_value("require_source_frame_timestamp")
        )
        self._model_input_max_source_future_skew_sec = max(
            0.0,
            float(param_value("model_input_max_source_future_skew_sec")),
        )
        if (
            self._require_source_frame_timestamp
            and self._model_input_max_source_lag_sec <= 0.0
        ):
            raise ValueError(
                "require_source_frame_timestamp needs "
                "model_input_max_source_lag_sec > 0"
            )
        self._replay_response_path = str(param_value("replay_response_path"))
        self._output_prefix = str(param_value("output_prefix")).rstrip("/")
        self._context_prefix = str(param_value("context_prefix")).rstrip("/")
        self._field_image_topic = str(param_value("field_image_topic"))
        self._raw_field_image_topic = str(
            param_value("raw_field_image_topic")
        ).strip()
        self._cam4_image_topic = str(param_value("cam4_image_topic")).strip()
        self._cam4_overlay_image_topic = str(
            param_value("cam4_overlay_image_topic")
        ).strip()
        self._tray_image_topic = str(param_value("tray_image_topic"))
        self._synthetic_image_topic = str(param_value("synthetic_image_topic"))
        self._composite_image_topic = str(param_value("composite_image_topic"))
        self._cam4_dynamic_crop = bool(param_value("cam4_dynamic_crop"))
        self._require_cam4_image = bool(param_value("require_cam4_image"))
        self._multiview_max_skew_sec = max(
            0.0,
            float(param_value("multiview_max_skew_sec")),
        )
        self._cam4_crop_xywh_norm = normalize_crop_xywh(
            (
                float(param_value("cam4_crop_x_norm")),
                float(param_value("cam4_crop_y_norm")),
                float(param_value("cam4_crop_width_norm")),
                float(param_value("cam4_crop_height_norm")),
            )
        )
        self._cam4_crop_padding_norm = float(
            param_value("cam4_crop_padding_norm")
        )
        self._cam4_crop_min_width_norm = float(
            param_value("cam4_crop_min_width_norm")
        )
        self._cam4_crop_min_height_norm = float(
            param_value("cam4_crop_min_height_norm")
        )
        self._perception_bboxes_topic = str(
            param_value("perception_bboxes_topic")
        ).strip()
        self._perception_segmentation_topic = str(
            param_value("perception_segmentation_topic")
        ).strip()
        self._cam4_semantics_topic = str(
            param_value("cam4_semantics_topic")
        ).strip()
        self._cam3_tool_observations_topic = str(
            param_value("cam3_tool_observations_topic")
        ).strip()
        self._cam4_tool_observations_topic = str(
            param_value("cam4_tool_observations_topic")
        ).strip()
        self._cam3_tool_observations_expected_model_version = str(
            param_value("cam3_tool_observations_expected_model_version")
        ).strip()
        self._cam4_tool_observations_expected_model_version = str(
            param_value("cam4_tool_observations_expected_model_version")
        ).strip()
        self._allow_legacy_cam4_semantics_fallback = bool(
            param_value("allow_legacy_cam4_semantics_fallback")
        )
        self._perception_stale_sec = max(
            0.0,
            float(param_value("perception_stale_sec")),
        )
        self._perception_image_max_skew_sec = max(
            0.0,
            float(param_value("perception_image_max_skew_sec")),
        )
        self._perception_max_instances = max(
            0,
            int(param_value("perception_max_instances")),
        )
        self._image_stale_sec = float(param_value("image_stale_sec"))
        self._image_max_side_px = max(
            0,
            int(param_value("image_max_side_px")),
        )
        self._multiview_image_max_side_px = max(
            320,
            int(param_value("multiview_image_max_side_px")),
        )
        self._require_field_image = bool(param_value("require_field_image"))
        self._enable_text_only_dialogue = bool(
            param_value("enable_text_only_dialogue")
        )
        self._require_rfdetr_applied_field_image = bool(
            param_value("require_rfdetr_applied_field_image")
        )
        self._require_rfdetr_cam4_overlay = bool(
            param_value("require_rfdetr_cam4_overlay")
        )
        self._context_mode = str(param_value("context_mode")).strip().lower()
        self._open_set_phase_bootstrap_observations = max(
            0,
            int(param_value("open_set_phase_bootstrap_observations")),
        )
        self._handover_ngram_prior_enabled = bool(
            param_value("handover_ngram_prior_enabled")
        )
        self._handover_ngram_prior: FrozenHandoverNgramPrior | None = None
        if self._context_mode != "actor_log" and self._response_mode == "live":
            self.get_logger().warning(
                "live VLM context is restricted to public evidence; "
                f"ignoring context_mode={self._context_mode!r}"
            )
            self._context_mode = "actor_log"
        if self._context_mode == "actor_log":
            self._procedure_prompt = compact_procedure_prompt(self._spec_dir)
            self._prior_scorer = ProcedurePriorScorer(self._spec, self._procedure_prompt)
            if self._handover_ngram_prior_enabled:
                self._handover_ngram_prior = load_frozen_handover_ngram_prior(
                    self._spec,
                    self._spec_dir,
                )
            if self._task_profile == VLM_TASK_PROFILE_TOOL_FORECAST_ONLY:
                self._system_prompt = self._tool_forecast_only_system_prompt()
                self._developer_instruction = (
                    self._tool_forecast_only_developer_instruction()
                )
                self._json_schema = compact_tool_forecast_json_schema()
            else:
                self._system_prompt = self._actor_log_system_prompt()
                self._developer_instruction = self._actor_log_developer_instruction()
                self._json_schema = compact_vlm_json_schema("6")
        else:
            if self._task_profile != VLM_TASK_PROFILE_FULL:
                raise ValueError(
                    "tool_forecast_only requires context_mode=actor_log"
                )
            self._context_mode = "world"
            self._procedure_prompt = compact_procedure_prompt(self._spec_dir)
            self._prior_scorer = ProcedurePriorScorer(self._spec, self._procedure_prompt)
            self._system_prompt = self._prompt_builder.build(self._spec_dir)
            self._developer_instruction = self._world_developer_instruction()
            self._json_schema = compact_vlm_json_schema("1")
        self._oracle_scenario = list(self._spec.get_mock_perception_stages())
        self._oracle_scenario_length = sum(stage.duration_ticks for stage in self._oracle_scenario)
        self._oracle_bootstrap_tick = int(self._spec.get_mock_perception_bootstrap_tick())
        self._client = LMStudioClient(
            base_url=self._base_url,
            timeout_sec=self._request_timeout_sec,
            api_key=self._api_key,
            provider_id=self._provider_id,
        )
        self._replay_payload = self._load_replay_payload(self._replay_response_path)

    def _load_replay_payload(self, replay_path: str) -> dict[str, Any] | None:
        if not replay_path.strip():
            return None
        path = Path(replay_path)
        if not path.is_file():
            self.get_logger().warning(f"Replay response path does not exist: {replay_path}")
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            self.get_logger().warning(f"Failed to read replay response payload: {exc}")
            return None

    def _topic(self, prefix: str, suffix: str) -> str:
        return f"{prefix}/{suffix}".replace("//", "/")

    def _world_developer_instruction(self) -> str:
        return (
            "Return exactly one valid JSON object and nothing else. "
            "All object keys must be double-quoted strings: \"v\", \"ph\", \"to\", \"u\", \"sum\". "
            "Never omit quotes around keys. Never use true/false/null. "
            "Confidence values and u must be numeric floats between 0.0 and 1.0, not strings and not booleans. "
            "Use exact tool ids and location ids from context. "
            "If the image is black, blank, or text-only, keep the current context phase at low confidence, emit no tool observations, and set u to 1.0."
        )

    def _actor_log_system_prompt(self) -> str:
        procedure_context = compact_actor_log_procedure_context(
            self._spec,
            self._procedure_prompt,
        )
        return (
            "You interpret public, externally observable evidence for a surgical task planner. "
            "Each image is a labeled composite: left FLIR surgical field and, when enabled, right CAM4 Mayo context. Read panels independently; never copy tools across panels and do not assume a fixed appearance, crop, or start phase. "
            "When observable_perception.tool_detection_views is present, it contains accepted typed RF-DETR CAM3/CAM4 tool boxes: view-relative class, confidence, normalized box/center, and sometimes depth. It is structured location evidence, not an annotated image. Use a row whose freshness.status is fresh to locate a visible tool within that named camera view, even if visual_alignment says the FLIR panel is unavailable or misaligned; that alignment only says whether it pixel-pairs with this request's FLIR image. Never treat a 2D region/depth as a world pose, ownership, Mayo placement, handover, or authority. Missing or stale views are unknown—not zero detections. Inspect raw pixels independently and let clear visual evidence outweigh conflicting detector rows. "
            "Inspect the left surgical field for visible anatomy, target tissue, instrument-to-tissue interaction, manipulation, and immediate tissue or field response. Independently inspect only instrument contents in any right Mayo panel. Then infer phase and next-request tool from visible anatomy/activity, public speech, public skill/twin events, and the procedure context. "
            "Use no hidden actor state, private plans, ground truth, or replay annotation. Previous predictions and procedure order are weak temporal priors, not facts. "
            "Your output is evidence with confidence only. It never authorizes handover, recovery, cleanup, ownership, lifecycle changes, or robot action; the digital twin validates facts and the Behavior Tree applies policy. "
            "Compare all phases: open_set is unanchored; temporal_prior favors current/next but never excludes strong later visual evidence. "
            "A currently held, visible, or Mayo tool is not automatically the next requested tool. "
            "Return exactly one schema-v6 JSON object and no other text. "
            "Procedure context keys: phases contain id/name/allowed next/tools, positive and exclusion cues, optional tool roles, ordered chain paths and alt transitions; handover contains procedure-wide primary and alternative request paths; groups describe coarse adjacent states; tools contain runtime id/name/role. "
            f"Procedure context: {json.dumps(procedure_context, separators=(',', ':'))}"
        )

    def _tool_forecast_only_system_prompt(self) -> str:
        procedure_context = compact_actor_log_procedure_context(
            self._spec,
            self._procedure_prompt,
        )
        return (
            "You solve one task only: forecast the first subsequent additional surgical "
            "instrument likely to be requested for handover, regardless of elapsed time. "
            "The image is a labeled composite with the FLIR surgical field and optional CAM4 "
            "Mayo context. Typed RF-DETR CAM3/CAM4 tool boxes, when present in "
            "observable_perception.tool_detection_views, are fresh view-relative location "
            "suggestions rather than pixels or world state. visual_alignment only "
            "describes FLIR pairing and does not invalidate a fresh named-view row; "
            "inspect raw pixels independently. "
            "Use only public image evidence, public speech, public "
            "skill/twin events, and the procedure context. Never use hidden actor state, "
            "private plans, ground truth, replay annotation, or case timing. "
            "The current instrument, visible inventory, Mayo contents, a spoken current "
            "request are context but are not themselves the next-tool "
            "answer. Return only ranked forecast candidates and uncertainty. "
            "Procedure context keys and values are exactly the same as the normal VLM: "
            "phases contain ids, cues, tool roles and chains; handover contains primary "
            "and alternative request paths; groups describe adjacent states; tools map "
            "runtime ids to names and roles. "
            f"Procedure context: {json.dumps(procedure_context, separators=(',', ':'))}"
        )

    def _tool_forecast_only_developer_instruction(self) -> str:
        return (
            "Return exactly one JSON object and no other text: "
            "{\"tool\":[[\"Txx\",0.0]],\"u\":0.0}. "
            "When forecast_constraints contains at least three eligible tools, tool must "
            "contain exactly three unique [runtime_tool_id,probability] candidates ranked "
            "highest first and their probabilities must sum to 1.0. If fewer than three "
            "tools are eligible, emit all eligible tools and set u to at least 0.80. "
            "Use exact ids from the supplied procedure context. "
            "Predict before a spoken request whenever the public trajectory "
            "supports it. Match completed_handover and tool_request history against all "
            "procedure chains, then combine that prior with visible operative activity. "
            "When frozen_ngram_prior is present, it is a fixed aggregate statistic from "
            "past reviewed demonstrations matched by public phase and completed-handover "
            "suffix. It is advisory only: use its candidate probabilities as one prior, "
            "never copy its first candidate mechanically, and never treat it as a request "
            "or authority to act. "
            "forecast_constraints is public DT context: currently_in_use and prepositioned "
            "are not additional handovers; available_for_next_handover is eligible stock; "
            "mayo_reusable is eligible only when imminent reuse is supported. A tool may "
            "be forecast only when a separate available instance exists. If speech already "
            "names the current request, forecast the following handover instead. Procedure "
            "start, phase, anatomy, continue, and finish speech are not tool requests. "
            "Use calibrated confidence even when uncertain rather than copying a current "
            "tool. u is overall forecast uncertainty from 0.0 to 1.0. Emit no phase, "
            "intent, Mayo, summary, lifecycle, or robot-action fields."
        )

    def _actor_log_developer_instruction(self) -> str:
        return (
            "Emit exactly this JSON shape, with nested candidate pairs: "
            "{\"v\":\"6\",\"phase\":[[\"Pxx\",0.0]],\"tool\":[[\"Txx\",0.0]],"
            "\"intent\":[\"none\",\"\",0.0],"
            "\"mayo\":[],\"mayo_retrieve\":[\"\",0.0],\"u\":0.50,"
            "\"sum\":\"one compact English clinical observation\",\"bed_robot_arm_group\":null,"
            "\"function_call\":null,\"humanoid_reply\":null}. "
            "IDs:0-1. phase:1-4 pairs. tool:3 eligible unique pairs,sum1; fewer:all,u>=.8. No unavailable/non-requestable ids or flat pairs. "
            "Independent passes: "
            "MAYO: scan only instrument contents in the right CAM4 panel. Emit one row per distinct visible instrument instance and preserve duplicates. Identify by visible morphology such as rings, hinge, shaft, jaws, blade, insulation/cable, or lumen; omit unidentifiable silhouettes instead of guessing from procedure likelihood. Detector rows may support a match but their absence does not erase clear pixels. Never copy instruments from the surgical-field image in the left FLIR panel, speech, candidates, memory, or procedure order into mayo. recover/reuse is only an advisory observation: use reuse when public evidence supports near-term reuse; otherwise keep recover confidence low. mayo_retrieve is at most the strongest advisory candidate. "
            "CLINICAL SUMMARY: sum is one compact English sentence about the left field: visible instrument, specific anatomy/tissue, manipulation, and immediate effect. If obscured, say so. Never invent injury, preserved anatomy, hemostasis, or completion; omit panel names, Mayo, request, phase id/name, forecasts, and reasoning. "
            "PHASE/NEXT TOOL: compare left-field pixels with every cue/exclusion/group/role. temporal_prior is a preference, not a candidate filter. Persistent anatomy/activity outranks public speech/events and priors; an exchange alone never proves phase. "
            "phase_start_floor limits phase only, never tool/intent. Not ground truth; use allowed_normal_phase_ids, never earlier. Interrupts need visible evidence. "
            "NEXT-TOOL FORECAST: tool is only a horizon-free forecast of the first subsequent new handover, regardless of elapsed time, not a label for the tool currently in use; it answers which additional instrument the assistant should prepare next and does not inventory visible instruments. Predict before a spoken request. Select the most plausible subsequent additional tool from visible task trajectory, broad procedure-role transitions, and public history; distinguish instruments already held or prepositioned. Unless public evidence specifically supports another instance, an already active type must stay below 0.65; forecast a plausible unused tool instead. forecast_constraints is public DT context: currently_in_use lists surgeon-held tools and counts; prepositioned is robot-held; available_for_next_handover comes from digital_twin.forecast_inventory.available: rack_available unused stock plus mayo_reuse surgeon-used tools expected later when trajectory supports imminent reuse. A tool type may appear in available and unavailable, but a forecast must have available count >0. This evidence never authorizes action; the reducer and BT, not the VLM, validate availability. Independently of your phase candidate, match the longest suffix against every procedure chain. frozen_ngram_prior is an advisory aggregate phase/history statistic, not a request. Do not choose the next tool solely from your phase output or the n-gram prior. No-history entry_handover is not a confirmed request; keep every weak candidate below 0.65. Spoken tool is the current request, so forecast the following one. Do not memorize case timing. "
            "INTENT: only current admitted public speech naming a runtime instrument may produce [\"handover\",tool_id,confidence]. Match obvious ASR near-homophones and Korean/English transliterations to the listed runtime tool names, but do not turn procedure-start, continue, phase, anatomy, or completion speech into a tool request. "
            "DIALOGUE: Only pending_dialogue_turn may be answered; speech history never may. Null pending means both new fields are null. Otherwise copy turn_id exactly and emit one concise same-language speak=true reply. Questions use null function_call and immediate timing. Supported direct requests only: request_tool_handover with arguments exactly {tool_id}, or adjust_retraction with arguments exactly {command:'adjust_retraction',target_side:'left|right|both',distance_m:positive meters}. Use only slots explicit in the pending utterance and procedure rules; otherwise ask an immediate clarification with null function_call. Pair valid calls with on_function_accepted future wording and never claim completion. For a Korean request_tool_handover with grounded tool_id T07, use exactly the reply text 바이폴라 전달드리겠습니다. Tool retrieval is not a dialogue function because the current typed execution contract cannot correlate it safely; return an immediate clarification with null function_call instead. "
            "BED RETRACTION: null unless a pending public fine-adjustment request has direction evidence. Copy request_id, adjustment_mode, target_retractor_id, and surgeon_view exactly. For thyroidectomy_demo only, a terse explicit-distance request with target_retractor_id right_malleable and no spoken direction uses the reviewed default direction right. For single, emit one of up/down/left/right with axis none. For multi, emit direction none with axis left_right or up_down. Preserve grounded distance text. Never propose tool change or any non-retraction operation. "
            "UNCERTAINTY: calculate u independently on every frame: 0.00-0.25 clear, 0.26-0.45 for usable adjacent-phase ambiguity, 0.46-0.79 weak/conflicting, and 0.80-1.00 only when the relevant view is unusable. Do not copy the structural 0.50 value. "
            "mayo is always an array of three-value rows: [[\"Txx\",\"reuse\",0.80]], never [\"Txx\"], [\"tool name\"], or [\"tool name (Txx)\"]. Empty Mayo is []. "
            "phase and tool use two-value rows: [[\"Pxx\",0.80]] and [[\"Txx\",0.70]], never flat pairs. Pxx/Txx are shape placeholders only; replace them with exact ids and never emit xx. "
            "Final audit: mayo must come only from Mayo pixels; phase/tool may combine public evidence; output only JSON."
        )

    def _on_parameters_changed(self, params):
        reload_required = False
        overrides = {parameter.name: parameter.value for parameter in params}
        spec_changed = "spec_dir" in overrides
        for parameter in params:
            if parameter.name in {
                "spec_dir",
                "base_url",
                "provider_id",
                "api_key",
                "model_id",
                "api_mode",
                "request_timeout_sec",
                "max_output_tokens",
                "temperature",
                "top_p",
                "generation_seed",
                "response_format",
                "reasoning_effort",
                "task_profile",
                "retry_count",
                "transport_failure_backoff_initial_sec",
                "transport_failure_backoff_max_sec",
                "publish_period_sec",
                "response_mode",
                "source_time_triggered_live",
                "model_input_max_source_lag_sec",
                "require_source_frame_timestamp",
                "model_input_max_source_future_skew_sec",
                "replay_response_path",
                "cam4_dynamic_crop",
                "require_cam4_image",
                "multiview_max_skew_sec",
                "cam4_crop_x_norm",
                "cam4_crop_y_norm",
                "cam4_crop_width_norm",
                "cam4_crop_height_norm",
                "cam4_crop_padding_norm",
                "cam4_crop_min_width_norm",
                "cam4_crop_min_height_norm",
                "perception_stale_sec",
                "perception_image_max_skew_sec",
                "perception_max_instances",
                "image_stale_sec",
                "image_max_side_px",
                "multiview_image_max_side_px",
                "require_field_image",
                "enable_text_only_dialogue",
                "require_rfdetr_applied_field_image",
                "require_rfdetr_cam4_overlay",
                "context_mode",
                "open_set_phase_bootstrap_observations",
                "handover_ngram_prior_enabled",
            }:
                reload_required = True
        if reload_required:
            try:
                self._load_parameters(overrides)
                self._reset_model_input_dedupe(advance_epoch=True)
                if spec_changed:
                    self._last_lifecycle_control_signature = None
                    self._recent_events.clear()
                    self._last_good_raw = ""
                    self._last_good_payload = None
                    self._recent_speech.clear()
                    self._recent_skill_statuses.clear()
                    self._latest_bed_robot_arm_group_request = None
                    self._last_bed_robot_arm_group_proposal_request_id = ""
                    self._last_vlm_phase = ""
                    self._last_authoritative_phase = ""
                    self._phase_bootstrap_id = ""
                    self._phase_bootstrap_observation_count = 0
                    self._phase_bootstrap_explicit = False
                    self._phase_entered_wall_sec = self._causal_now_sec()
                    self._oracle_tick = 0
            except Exception as exc:
                return SetParametersResult(successful=False, reason=str(exc))
        return SetParametersResult(successful=True)

    def _on_list_models(self, _request, response):
        try:
            model_ids = self._client.list_models()
            if self._model_id and self._model_id not in model_ids:
                model_ids.insert(0, self._model_id)
            response.model_ids = model_ids
            response.success = True
            response.message = f"{len(response.model_ids)} model(s) from {self._base_url}"
        except Exception as exc:
            response.success = False
            response.model_ids = []
            response.message = f"Model catalog unavailable at {self._base_url}: {exc}"
        return response

    def _on_list_model_catalog(self, _request, response):
        probes = self._provider_registry.probe_all()
        online_count = sum(1 for probe in probes if probe.reachable)
        model_count = 0
        response.active_provider_id = self._provider_id
        response.active_model_id = self._model_id
        response.providers = []
        response.models = []

        for probe in probes:
            provider_status = ModelProviderStatus()
            provider_status.provider_id = probe.provider.provider_id
            provider_status.provider_name = probe.provider.display_name
            provider_status.endpoint = probe.provider.base_url
            provider_status.reachable = probe.reachable
            provider_status.status = probe.status
            provider_status.detail = probe.detail
            provider_status.latency_sec = float(probe.latency_sec)
            provider_status.model_count = len(probe.models)
            response.providers.append(provider_status)

            for model in probe.models:
                entry = ModelCatalogEntry()
                entry.provider_id = model.provider_id
                entry.provider_name = model.provider_name
                entry.model_id = model.model_id
                entry.display_name = model.display_name
                entry.capability = model.capability
                entry.load_state = model.load_state
                entry.selectable = model.selectable
                entry.detail = model.detail
                entry.runtime_managed = model.runtime_managed
                entry.available_actions = list(model.available_actions)
                response.models.append(entry)
                model_count += 1

        advertised = {
            (str(entry.provider_id), str(entry.model_id))
            for entry in response.models
        }
        reachable_by_provider = {
            probe.provider.provider_id: probe.reachable for probe in probes
        }
        for remembered_provider_id, remembered_model_id in self._provider_model_selections.items():
            if not remembered_model_id or (
                remembered_provider_id,
                remembered_model_id,
            ) in advertised:
                continue
            provider = self._provider_registry.get_provider(remembered_provider_id)
            configured = ModelCatalogEntry()
            configured.provider_id = remembered_provider_id
            configured.provider_name = (
                provider.display_name
                if provider is not None
                else remembered_provider_id
            )
            configured.model_id = remembered_model_id
            configured.display_name = remembered_model_id
            configured.capability = "unknown"
            matching_entry = next(
                (
                    entry
                    for entry in response.models
                    if str(entry.provider_id) == remembered_provider_id
                    and self._provider_registry.canonical_model_id(
                        remembered_provider_id,
                        str(entry.model_id),
                    )
                    == self._provider_registry.canonical_model_id(
                        remembered_provider_id,
                        remembered_model_id,
                    )
                ),
                None,
            )
            override = self._provider_registry.runtime_state(
                remembered_provider_id,
                remembered_model_id,
            )
            if override is not None:
                configured.load_state, configured.detail = override
            elif matching_entry is not None:
                configured.load_state = str(matching_entry.load_state)
                configured.detail = str(matching_entry.detail)
            else:
                configured.load_state = "configured"
                configured.detail = "Configured model was not returned by /v1/models"
            configured.selectable = reachable_by_provider.get(
                remembered_provider_id,
                False,
            )
            configured.runtime_managed = bool(
                provider is not None and provider.runtime_commands
            )
            configured.available_actions = list(
                self._provider_registry.available_actions(
                    remembered_provider_id,
                    configured.load_state,
                )
            )
            response.models.insert(0, configured)
            model_count += 1

        response.success = online_count > 0 or model_count > 0
        response.message = (
            f"{online_count}/{len(probes)} providers online; {model_count} model(s)"
        )
        return response

    def _on_select_model_provider(self, request, response):
        provider_id = str(request.provider_id).strip().lower()
        model_id = str(request.model_id).strip()
        response.provider_id = self._provider_id
        response.model_id = self._model_id
        if not provider_id or not model_id:
            response.success = False
            response.message = "provider_id and model_id are required"
            return response
        provider = self._provider_registry.get_provider(provider_id)
        if provider is None:
            response.success = False
            response.message = f"Unknown model provider: {provider_id}"
            return response
        probe = self._provider_registry.probe(provider_id)
        available_models = {model.model_id: model for model in probe.models}
        remembered_model = self._provider_model_selections.get(provider_id, "")
        catalog_model = available_models.get(model_id)
        if catalog_model is None:
            catalog_model = self._provider_registry.matching_model(
                provider_id,
                model_id,
                probe.models,
            )
        if catalog_model is None and model_id != remembered_model:
            response.success = False
            response.message = (
                f"{model_id} is not advertised by {provider.display_name}"
            )
            return response
        runtime_note = ""
        can_start_offline = bool(
            catalog_model is not None
            and catalog_model.installed
            and catalog_model.available
            and catalog_model.runtime_managed
        )
        if not probe.reachable and not can_start_offline:
            response.success = False
            response.message = (
                f"{provider.display_name} is unavailable: {probe.status} "
                f"({probe.detail})"
            )
            return response
        if (
            provider.managed
            and catalog_model is not None
            and catalog_model.runtime_managed
        ):
            runtime = self._provider_registry.ensure_runtime_ready(
                provider_id,
                catalog_model,
                requested_model_id=model_id,
            )
            if not runtime.success:
                response.success = False
                response.message = runtime.message
                return response
            runtime_note = f"; runtime {runtime.state}"

        result = self.set_parameters_atomically(
            [
                Parameter("provider_id", value=provider_id),
                Parameter("model_id", value=model_id),
            ]
        )
        if not result.successful:
            response.success = False
            response.message = result.reason or "Provider selection was rejected"
            return response
        response.success = True
        response.provider_id = self._provider_id
        response.model_id = self._model_id
        response.message = (
            f"VLM provider set to {provider.display_name}; model set to "
            f"{model_id}{runtime_note}"
        )
        return response

    def _on_control_model_runtime(self, request, response):
        result = self._provider_registry.control_runtime(
            str(request.provider_id),
            str(request.model_id),
            str(request.command),
        )
        response.success = result.success
        response.provider_id = result.provider_id
        response.model_id = result.model_id
        response.state = result.state
        response.message = result.message
        if result.success:
            self._reset_model_input_dedupe(advance_epoch=True)
        return response

    def _make_image_cb(self, label: str):
        def _cb(msg: CompressedImage) -> None:
            image_format = str(msg.format or "").lower()
            if "webp" in image_format:
                mime_type = "image/webp"
            elif "png" in image_format:
                mime_type = "image/png"
            else:
                mime_type = "image/jpeg"
            sample = ImageSample(
                received_monotonic=self._causal_now_sec(),
                stamp_sec=int(msg.header.stamp.sec),
                stamp_nanosec=int(msg.header.stamp.nanosec),
                frame_id=str(msg.header.frame_id),
                data=bytes(msg.data),
                mime_type=mime_type,
            )
            rejection = self._source_frame_timestamp_rejection(sample)
            if rejection:
                self._last_source_frame_rejection = f"{label}:{rejection}"
                self.get_logger().warning(
                    f"dropped {label} image: {rejection}",
                    throttle_duration_sec=2.0,
                )
                return
            self._latest_images[label] = sample
            self._image_buffers.setdefault(
                label,
                deque(maxlen=IMAGE_PAIR_BUFFER_LENGTH),
            ).append(sample)
            trigger_labels = {"field", "raw_field", "cam4"}
            if bool(
                getattr(self, "_require_rfdetr_applied_field_image", False)
            ):
                # Store raw frames for exact source-time composition, but do
                # not let an unprocessed camera arrival schedule Live VLM
                # inference ahead of the locally rendered RF-DETR panel.
                trigger_labels.difference_update({"raw_field", "cam4"})
            if label in trigger_labels:
                self._visual_frame_generation += 1
                if self._response_mode == "replay":
                    self._maybe_trigger_replay_image_tick()
                else:
                    self._queue_source_time_live_frame(sample)

        return _cb

    def _source_frame_timestamp_rejection(
        self,
        sample: ImageSample,
        *,
        now_sec: float | None = None,
    ) -> str:
        if not bool(getattr(self, "_require_source_frame_timestamp", False)):
            return ""
        now = self._causal_now_sec() if now_sec is None else float(now_sec)
        return source_frame_timestamp_rejection_reason(
            now,
            image_sample_stamp_sec(sample),
            float(getattr(self, "_model_input_max_source_lag_sec", 0.0)),
            float(
                getattr(
                    self,
                    "_model_input_max_source_future_skew_sec",
                    1.0,
                )
            ),
        )

    def _queue_source_time_live_frame(self, sample: ImageSample) -> None:
        if (
            not getattr(self, "_source_time_triggered_live", False)
            or self._response_mode != "live"
            or not self._active
        ):
            return
        if self._source_frame_timestamp_rejection(sample):
            return
        stamp_sec = image_sample_stamp_sec(sample)
        with self._source_live_trigger_lock:
            self._last_source_live_trigger_stamp_sec = stamp_sec
            self._source_live_trigger_pending = True
        self._queue_inference(
            INFERENCE_TRIGGER_SOURCE_FRAME
        )

    def _drain_source_time_live_frame(self) -> None:
        """Wake the latest-frame consumer; retained for internal compatibility."""

        if not self._active:
            return
        with self._source_live_trigger_lock:
            self._source_live_trigger_pending = False
        wakeup = getattr(self, "_inference_wakeup", None)
        if wakeup is not None:
            wakeup.set()
            return
        current_trigger = self._inference_backpressure.begin()
        if current_trigger is not None:  # pragma: no cover - unit stub fallback
            self._run_inference_chain(current_trigger)

    def _queue_inference(self, trigger: str) -> InferenceAdmission:
        normalized = InferenceBackpressure._normalize_trigger(trigger)
        failure_backoff = getattr(self, "_transport_failure_backoff", None)
        backoff_remaining = (
            failure_backoff.remaining()
            if failure_backoff is not None
            else 0.0
        )
        if (
            normalized != INFERENCE_TRIGGER_FORCED
            and backoff_remaining > 0.0
        ):
            # Keep exactly the newest trigger. Source-time modes have no
            # periodic timer, so dropping it here could leave inference idle
            # forever after the provider recovers.
            self._inference_backpressure.queue(normalized)
            self._schedule_inference_retry(backoff_remaining)
            return InferenceAdmission("backoff", normalized)
        admission = self._inference_backpressure.queue(trigger)
        wakeup = getattr(self, "_inference_wakeup", None)
        if wakeup is not None:
            wakeup.set()
        return admission

    def _schedule_inference_retry(self, delay_sec: float) -> None:
        shutdown = getattr(self, "_inference_shutdown", None)
        if shutdown is not None and shutdown.is_set():
            return
        retry_lock = getattr(self, "_inference_retry_lock", None)
        if retry_lock is None:
            wakeup = getattr(self, "_inference_wakeup", None)
            if wakeup is not None:
                wakeup.set()
            return

        timer: threading.Timer | None = None

        def wake_worker() -> None:
            with retry_lock:
                if self._inference_retry_timer is timer:
                    self._inference_retry_timer = None
            shutdown = getattr(self, "_inference_shutdown", None)
            if shutdown is None or not shutdown.is_set():
                wakeup = getattr(self, "_inference_wakeup", None)
                if wakeup is not None:
                    wakeup.set()

        with retry_lock:
            if shutdown is not None and shutdown.is_set():
                return
            previous = getattr(self, "_inference_retry_timer", None)
            if previous is not None:
                previous.cancel()
            timer = threading.Timer(max(0.0, float(delay_sec)), wake_worker)
            timer.daemon = True
            self._inference_retry_timer = timer
            timer.start()

    def _cancel_inference_retry(self) -> None:
        retry_lock = getattr(self, "_inference_retry_lock", None)
        if retry_lock is None:
            return
        with retry_lock:
            timer = getattr(self, "_inference_retry_timer", None)
            self._inference_retry_timer = None
            if timer is not None:
                timer.cancel()

    def _inference_worker_loop(self) -> None:
        while not self._inference_shutdown.is_set():
            self._inference_wakeup.wait()
            self._inference_wakeup.clear()
            if self._inference_shutdown.is_set():
                break
            current_trigger = self._inference_backpressure.begin()
            if current_trigger is None:
                continue
            failure_backoff = getattr(self, "_transport_failure_backoff", None)
            backoff_remaining = (
                failure_backoff.remaining()
                if failure_backoff is not None
                and current_trigger != INFERENCE_TRIGGER_FORCED
                else 0.0
            )
            if backoff_remaining > 0.0:
                self._inference_backpressure.defer_until_ready(current_trigger)
                self._schedule_inference_retry(backoff_remaining)
                continue
            self._run_inference_chain(current_trigger)

    def destroy_node(self):
        shutdown = getattr(self, "_inference_shutdown", None)
        wakeup = getattr(self, "_inference_wakeup", None)
        worker = getattr(self, "_inference_worker", None)
        if shutdown is not None:
            shutdown.set()
        self._cancel_inference_retry()
        if wakeup is not None:
            wakeup.set()
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=2.0)
        reply_outbox = getattr(self, "_reply_outbox", None)
        if reply_outbox is not None:
            reply_outbox.close()
        return super().destroy_node()

    def _reset_source_time_live_trigger(self, *, reset_stamp: bool) -> None:
        lock = getattr(self, "_source_live_trigger_lock", None)
        if lock is None:
            self._source_live_trigger_pending = False
            if reset_stamp:
                self._last_source_live_trigger_stamp_sec = None
            return
        with lock:
            self._source_live_trigger_pending = False
            if reset_stamp:
                self._last_source_live_trigger_stamp_sec = None

    def _reset_fast_cam4_mayo_observations(self) -> None:
        getattr(
            self,
            "_fast_cam4_mayo_published_tools",
            set(),
        ).clear()
        getattr(
            self,
            "_fast_cam4_mayo_last_seen_stamp_sec",
            {},
        ).clear()

    def _maybe_trigger_replay_image_tick(self) -> None:
        if self._response_mode != "replay" or not self._active:
            return
        now = self._causal_now_sec()
        field = self._fresh_image("field", now)
        if field is None:
            return
        stamp_sec = image_sample_stamp_sec(field)
        self._last_replay_image_stamp_sec = stamp_sec
        self._queue_inference(
            INFERENCE_TRIGGER_REPLAY_FRAME
        )

    def _make_perception_cb(self, kind: str):
        def _cb(msg: String) -> None:
            if kind == "cam4_semantics":
                summary = parse_cam4_semantics_json(msg.data)
            else:
                summary = summarize_public_perception_json(
                    msg.data,
                    kind=kind,
                    max_instances=self._perception_max_instances,
                )
            if summary:
                sample = (
                    self._causal_now_sec(),
                    summary,
                )
                self._latest_perception[kind] = sample
                self._perception_buffers.setdefault(
                    kind,
                    deque(maxlen=PERCEPTION_PAIR_BUFFER_LENGTH),
                ).append(sample)

        return _cb

    def _make_rfdetr_tool_observation_cb(self, view: str):
        """Store a bounded typed RF-DETR frame for source-time alignment.

        The upstream message contains mask RLE and other high-volume fields.
        ``summarize_rfdetr_tool_observations`` projects it before it reaches
        either the request context or a VLM prompt, and rejects a message whose
        declared view/model provenance does not match this subscription.
        """

        kind = f"rfdetr_{view}_tools"
        expected_model_version = str(
            getattr(
                self,
                f"_{view.replace('_', '')}_tool_observations_expected_model_version",
                "",
            )
        ).strip()

        def _cb(msg: ToolObservation2DArray) -> None:
            summary = summarize_rfdetr_tool_observations(
                msg,
                expected_view=view,
                expected_model_version=expected_model_version,
                max_instances=self._perception_max_instances,
            )
            if not summary:
                return
            sample = (self._causal_now_sec(), summary)
            self._latest_perception[kind] = sample
            self._perception_buffers.setdefault(
                kind,
                deque(maxlen=PERCEPTION_PAIR_BUFFER_LENGTH),
            ).append(sample)

        return _cb

    def _publish_fast_cam4_mayo_observations(
        self,
        summary: dict[str, Any],
    ) -> None:
        """Publish stable fixed-camera Mayo presence without waiting for VLM."""

        if (
            not self._active
            or not getattr(self, "_perception_enabled", True)
            or summary.get("schema") != "taskplanner.cam4_semantics.v1"
            or summary.get("source") != "cam4_rfdetr_small"
        ):
            return
        source_stamp_sec = _finite_float(summary.get("source_stamp_sec"))
        if source_stamp_sec is None:
            return

        published = getattr(
            self,
            "_fast_cam4_mayo_published_tools",
            set(),
        )
        last_seen = getattr(
            self,
            "_fast_cam4_mayo_last_seen_stamp_sec",
            {},
        )
        self._fast_cam4_mayo_published_tools = published
        self._fast_cam4_mayo_last_seen_stamp_sec = last_seen

        for row in summary.get("tools", []):
            if not isinstance(row, dict):
                continue
            tool_id = self._canonical_tool_id(row.get("name", ""))
            if tool_id:
                last_seen[tool_id] = float(source_stamp_sec)
        for tool_id in tuple(published):
            if (
                float(source_stamp_sec) - last_seen.get(tool_id, -math.inf)
                > CAM4_MAYO_STABILITY_WINDOW_SEC
            ):
                published.discard(tool_id)

        enriched = self._enrich_cam4_tool_stability(
            summary,
            reference_stamp_sec=float(source_stamp_sec),
        )
        publisher = getattr(self, "_tool_pub", None)
        if publisher is None:
            return
        for row in enriched.get("tools", []):
            if not isinstance(row, dict):
                continue
            tool_id = self._canonical_tool_id(row.get("name", ""))
            confidence = _finite_float(row.get("max_confidence"))
            if (
                not tool_id
                or tool_id in published
                or confidence is None
                or confidence < CAM4_MAYO_MIN_CONFIDENCE
                or int(row.get("stable_sample_count", 0) or 0)
                < CAM4_MAYO_MIN_STABLE_SAMPLES
                or float(row.get("stable_duration_sec", 0.0) or 0.0)
                < CAM4_MAYO_MIN_STABLE_DURATION_SEC
            ):
                continue
            observation = ToolObservation()
            stamp_sec = int(math.floor(source_stamp_sec))
            stamp_nanosec = int(
                round((float(source_stamp_sec) - stamp_sec) * 1_000_000_000)
            )
            if stamp_nanosec >= 1_000_000_000:
                stamp_sec += 1
                stamp_nanosec -= 1_000_000_000
            observation.stamp.sec = stamp_sec
            observation.stamp.nanosec = stamp_nanosec
            observation.instrument_id = tool_id
            observation.location_id = "mayo_stand"
            observation.location_type = "mayo_stand"
            observation.confidence = float(confidence)
            observation.visible = True
            publisher.publish(observation)
            published.add(tool_id)

    def _on_perception_health(self, msg: String) -> None:
        try:
            payload = json.loads(msg.data)
        except (json.JSONDecodeError, TypeError):
            return
        if (
            not isinstance(payload, dict)
            or payload.get("schema") != "taskplanner.rfdetr_health.v1"
        ):
            return
        enabled = bool(payload.get("enabled"))
        if enabled == self._perception_enabled:
            return
        self._perception_enabled = enabled
        self._perception_generation += 1
        if enabled:
            return
        self._latest_images.pop("field", None)
        self._image_buffers.pop("field", None)
        self._latest_images.pop("cam4_overlay", None)
        self._image_buffers.pop("cam4_overlay", None)
        self._latest_perception.clear()
        getattr(self, "_perception_buffers", {}).clear()
        self._current_visual_input = {}
        self._current_perception_reference_stamp_sec = None
        self._last_good_raw = ""
        self._last_good_payload = None
        self._last_periodic_live_image_stamp_sec = None
        self._last_submitted_live_image_stamp_sec = None
        self._reset_source_time_live_trigger(reset_stamp=True)
        self._reset_fast_cam4_mayo_observations()
        self._publish_health(
            image_source="flir_raw_fallback",
            latency_sec=0.0,
            prompt_chars=0,
            output_chars=0,
            parse_retry_count=0,
            last_error="",
            mode="raw_visual_fallback_pending",
            healthy=True,
            connected=True,
        )

    def _oracle_stage_for_tick(self, tick: int):
        if not self._oracle_scenario:
            return None
        if self._oracle_scenario_length <= 0:
            return self._oracle_scenario[0]
        cycle_tick = tick % self._oracle_scenario_length
        for stage in self._oracle_scenario:
            if cycle_tick < stage.duration_ticks:
                return stage
            cycle_tick -= stage.duration_ticks
        return self._oracle_scenario[-1]

    def _on_world(self, msg: WorldState) -> None:
        self._world = msg
        self._track_authoritative_phase()
        self._publish_context_summaries()

    def _activate_lifecycle(self, start_phase_id: str = "") -> None:
        """Activate once for an admitted execution lifecycle.

        ``/simulation/control_state`` is an event topic, so a VLM process that
        comes up between the manager's start publications must be able to
        reconcile from the authoritative, continuously-published simulation
        state.  Keep the initialization in one place so that recovery has the
        same dedupe and evidence boundary as the normal control path.
        """

        if self._active:
            return
        self._reset_model_input_dedupe(advance_epoch=True)
        self._reset_public_evidence()
        self._last_replay_image_stamp_sec = None
        self._last_periodic_live_image_stamp_sec = None
        self._last_submitted_live_image_stamp_sec = None
        self._reset_source_time_live_trigger(reset_stamp=True)
        self._reset_fast_cam4_mayo_observations()
        self._phase_bootstrap_observation_count = 0
        self._phase_bootstrap_explicit = bool(
            start_phase_id and start_phase_id in self._spec.phase_ids
        )
        self._phase_bootstrap_id = (
            start_phase_id
            if start_phase_id in self._spec.phase_ids
            else self._spec.default_phase_id
        )
        self._last_authoritative_phase = self._phase_bootstrap_id
        self._active = True

    def _stop_lifecycle(self) -> None:
        """Deactivate once when authoritative execution has conclusively ended."""

        if not self._active:
            return
        self._reset_model_input_dedupe(advance_epoch=True)
        self._active = False
        self._inference_backpressure.clear_pending()
        self._last_replay_image_stamp_sec = None
        self._last_periodic_live_image_stamp_sec = None
        self._last_submitted_live_image_stamp_sec = None
        self._reset_source_time_live_trigger(reset_stamp=True)
        self._reset_fast_cam4_mayo_observations()

    def _on_simulation(self, msg: SimulationState) -> None:
        active_bundle = str(getattr(msg, "active_bundle", "") or "")
        if active_bundle and active_bundle != self._last_simulation_bundle:
            self._last_simulation_bundle = active_bundle
            self._reset_public_evidence()
        self._simulation = msg
        execution_state = str(
            getattr(msg, "execution_state", "") or ""
        ).strip().lower()
        if (
            bool(getattr(msg, "running", False))
            and execution_state == "running"
        ):
            # The state is authoritative and emitted continuously.  It is only
            # a recovery path for a missed transient start event; it does not
            # infer a start from an intermediate or unknown state.
            self._activate_lifecycle(
                str(getattr(msg, "filtered_phase", "") or "")
            )
        elif (
            not bool(getattr(msg, "running", False))
            and execution_state
            in {"idle", "stopped", "completed", "error", "failed"}
        ):
            self._stop_lifecycle()
        self._track_authoritative_phase()
        self._publish_context_summaries()

    def _on_event(self, msg: TwinEvent) -> None:
        digest = EventDigest()
        digest.stamp = msg.stamp
        digest.event_type = msg.event_type
        digest.instrument_id = msg.instrument_id
        digest.anchor_id = msg.target_location_id or msg.location_id or msg.target_location_type
        detail = {}
        if msg.detail_json:
            try:
                detail = json.loads(msg.detail_json)
            except json.JSONDecodeError:
                detail = {"detail_json": msg.detail_json}
        digest.reason = (
            str(
                detail.get("note")
                or detail.get("voice_text")
                or msg.mode
                or msg.status
                or msg.event_type
            )
        )
        digest.detail = compact_json(
            {
                "tool": msg.instrument_id,
                "loc": msg.location_id,
                "target": msg.target_location_id,
                "arm": msg.arm,
                "mode": msg.mode,
            }
        )
        self._recent_events.append(digest)
        if msg.event_type == "ToolHandoverCompleted" and msg.instrument_id:
            history = getattr(self, "_completed_handover_history", None)
            if history is not None:
                history.append(
                    {
                        "tool": msg.instrument_id,
                        "at": float(msg.stamp.sec)
                        + float(msg.stamp.nanosec) / 1_000_000_000.0,
                    }
                )
        self._ingest_public_twin_event(msg, detail)
        self._event_digest_pub.publish(digest)
        self._publish_context_summaries()

    def _on_bt_decision(self, msg: BTDecision) -> None:
        self._latest_bt = msg
        self._bt_snapshot_pub.publish(self._bt_snapshot_msg())
        self._publish_context_summaries()

    @staticmethod
    def _reply_envelope_from_message(msg: HumanoidReply) -> ReplyEnvelope:
        return ReplyEnvelope(
            stamp_sec=int(msg.stamp.sec),
            stamp_nanosec=int(msg.stamp.nanosec),
            source=str(msg.source),
            source_epoch=int(msg.source_epoch),
            source_sequence=int(msg.source_sequence),
            correlation_id=str(msg.correlation_id),
            schema_version=str(msg.schema_version),
            gateway_instance_id=str(msg.gateway_instance_id),
            procedure_run_id=str(msg.procedure_run_id),
            utterance_id=str(msg.utterance_id),
            turn_id=str(msg.turn_id),
            reply_id=str(msg.reply_id),
            text=str(msg.text),
            kind=str(msg.kind),
            timing=str(msg.timing),
            speak=bool(msg.speak),
            function_call_name=str(msg.function_call_name),
            function_arguments_json=str(msg.function_arguments_json),
            function_request_id=str(msg.function_request_id),
            valid=bool(msg.valid),
            validation_error=str(msg.validation_error),
        )

    @staticmethod
    def _reply_message_from_envelope(envelope: ReplyEnvelope) -> HumanoidReply:
        msg = HumanoidReply()
        msg.stamp.sec = envelope.stamp_sec
        msg.stamp.nanosec = envelope.stamp_nanosec
        msg.source = envelope.source
        msg.source_epoch = envelope.source_epoch
        msg.source_sequence = envelope.source_sequence
        msg.correlation_id = envelope.correlation_id
        msg.schema_version = envelope.schema_version
        msg.gateway_instance_id = envelope.gateway_instance_id
        msg.procedure_run_id = envelope.procedure_run_id
        msg.utterance_id = envelope.utterance_id
        msg.turn_id = envelope.turn_id
        msg.reply_id = envelope.reply_id
        msg.text = envelope.text
        msg.kind = envelope.kind
        msg.timing = envelope.timing
        msg.speak = envelope.speak
        msg.function_call_name = envelope.function_call_name
        msg.function_arguments_json = envelope.function_arguments_json
        msg.function_request_id = envelope.function_request_id
        msg.valid = envelope.valid
        msg.validation_error = envelope.validation_error
        return msg

    def _on_tts_playback_status(self, msg: TTSPlaybackStatus) -> None:
        reply_outbox = getattr(self, "_reply_outbox", None)
        if reply_outbox is None:
            return
        try:
            reply_outbox.mark_acked(
                str(msg.reply_id or "").strip(),
                str(msg.state or "").strip(),
            )
        except ValueError:
            # Other internal TTS producers (for example fixed feedback) have
            # no VLM outbox row and may use states outside this ACK contract.
            return
        except Exception as exc:
            self.get_logger().error(
                f"VLM reply outbox ACK persistence failed: {exc.__class__.__name__}"
            )

    def _republish_pending_humanoid_replies(self) -> None:
        gateway_instance_id = str(
            getattr(self, "_gateway_instance_id", "") or ""
        ).strip()
        active_run = str(getattr(self, "_procedure_run_id", "") or "").strip()
        reply_outbox = getattr(self, "_reply_outbox", None)
        if not gateway_instance_id or not active_run or reply_outbox is None:
            return
        try:
            pending = reply_outbox.pending_for_scope(
                gateway_instance_id,
                active_run,
                limit=64,
            )
        except Exception as exc:
            self.get_logger().error(
                f"VLM reply outbox recovery failed: {exc.__class__.__name__}"
            )
            return
        for envelope in pending:
            self._humanoid_reply_pub.publish(
                self._reply_message_from_envelope(envelope)
            )

    def _on_gateway_info(self, msg: GatewayInfo) -> None:
        gateway_instance_id = str(msg.gateway_instance_id or "").strip()
        raw_procedure_run_id = str(msg.procedure_run_id or "").strip()
        source_age_sec = self._gateway_info_source_age_sec(msg)
        revision = int(msg.revision)
        source_stamp_ns = self._gateway_stamp_ns(msg)
        if source_age_sec is None:
            self._fence_gateway_info("stale_or_invalid_gateway_heartbeat")
            return
        if not gateway_instance_id or (
            bool(msg.procedure_active) and not raw_procedure_run_id
        ):
            self._fence_gateway_info("malformed_gateway_scope")
            return
        if (
            gateway_instance_id
            == getattr(self, "_gateway_info_last_instance_id", "")
            and (
                revision <= getattr(self, "_gateway_info_last_revision", -1)
                or source_stamp_ns
                <= getattr(self, "_gateway_info_last_source_stamp_ns", 0)
            )
        ):
            return
        self._gateway_info_last_instance_id = gateway_instance_id
        self._gateway_info_last_revision = revision
        self._gateway_info_last_source_stamp_ns = source_stamp_ns
        self._gateway_info_last_seen_monotonic = (
            time.monotonic() - max(0.0, source_age_sec)
        )
        procedure_run_id = (
            raw_procedure_run_id if bool(msg.procedure_active) else ""
        )
        previous_scope = (
            str(getattr(self, "_gateway_instance_id", "")),
            str(getattr(self, "_procedure_run_id", "")),
        )
        next_scope = (gateway_instance_id, procedure_run_id)
        self._gateway_instance_id = gateway_instance_id
        self._procedure_run_id = procedure_run_id
        reply_outbox = getattr(self, "_reply_outbox", None)
        if previous_scope != next_scope and reply_outbox is not None:
            try:
                reply_outbox.mark_stale_outside_scope(
                    gateway_instance_id if procedure_run_id else "",
                    procedure_run_id,
                )
            except Exception as exc:
                self.get_logger().error(
                    "VLM reply outbox scope transition failed: "
                    f"{exc.__class__.__name__}"
                )
        if previous_scope != ("", "") and previous_scope != next_scope:
            # A delayed reply from a previous gateway process/procedure must
            # never be spoken in the new run.
            self._reset_model_input_dedupe(advance_epoch=True)
            self._reset_public_evidence()
        if procedure_run_id:
            self._republish_pending_humanoid_replies()

    def _on_gateway_info_watchdog(self) -> None:
        """Fence dialogue and durable replies when the gateway heartbeat dies."""

        last_seen = getattr(self, "_gateway_info_last_seen_monotonic", None)
        if last_seen is None:
            return
        if time.monotonic() - float(last_seen) <= self._gateway_info_timeout_sec:
            return

        self._fence_gateway_info("gateway_heartbeat_timeout")

    @staticmethod
    def _gateway_stamp_ns(msg: GatewayInfo) -> int:
        return int(msg.stamp.sec) * 1_000_000_000 + int(msg.stamp.nanosec)

    def _gateway_info_source_age_sec(self, msg: GatewayInfo) -> float | None:
        source_stamp_ns = self._gateway_stamp_ns(msg)
        revision = int(msg.revision)
        now_ns = int(self.get_clock().now().nanoseconds)
        if revision <= 0 or source_stamp_ns <= 0 or now_ns <= 0:
            return None
        age_sec = float(now_ns - source_stamp_ns) / 1_000_000_000.0
        if age_sec > self._gateway_info_timeout_sec or age_sec < -1.0:
            return None
        return age_sec

    def _fence_gateway_info(self, reason: str) -> None:
        if (
            str(getattr(self, "_gateway_instance_id", "")) == ""
            and str(getattr(self, "_procedure_run_id", "")) == ""
            and self._gateway_info_last_seen_monotonic is None
        ):
            return

        # Clear the receipt first so the watchdog is edge-triggered. A later
        # heartbeat, even with the same gateway/run IDs, establishes a new
        # authority boundary rather than silently continuing the stale one.
        self._gateway_info_last_seen_monotonic = None
        previous_scope = (
            str(getattr(self, "_gateway_instance_id", "")),
            str(getattr(self, "_procedure_run_id", "")),
        )
        self._gateway_instance_id = ""
        self._procedure_run_id = ""
        reply_outbox = getattr(self, "_reply_outbox", None)
        if reply_outbox is not None:
            try:
                reply_outbox.mark_stale_outside_scope("", "")
            except Exception as exc:
                self.get_logger().error(
                    "VLM reply outbox gateway-timeout fence failed: "
                    f"{exc.__class__.__name__}"
                )
        if previous_scope != ("", ""):
            self._reset_model_input_dedupe(advance_epoch=True)
            self._reset_public_evidence()
            self.get_logger().error(
                "GatewayInfo authority unavailable; VLM dialogue and TTS outbox "
                f"fenced ({reason})"
            )

    def _append_public_speech(
        self,
        text: str,
        *,
        utterance_id: str = "",
        responded: bool | None = None,
    ) -> bool:
        clean = str(text or "").strip()
        if not clean:
            return False
        clean_utterance_id = str(utterance_id or "").strip()
        now = round(self._causal_now_sec(), 2)
        if clean_utterance_id and any(
            str(row.get("utterance_id", "")) == clean_utterance_id
            for row in self._recent_speech
            if isinstance(row, dict)
        ):
            return False
        if not clean_utterance_id and self._recent_speech:
            last = self._recent_speech[-1]
            try:
                last_age = now - float(last.get("at", 0.0))
            except (TypeError, ValueError):
                last_age = 999.0
            if str(last.get("text", "")) == clean[:240] and last_age <= 1.0:
                return False
        row: dict[str, Any] = {"text": clean[:240], "at": now}
        if clean_utterance_id:
            row["utterance_id"] = clean_utterance_id
        if responded is not None:
            row["responded"] = bool(responded)
        self._recent_speech.append(row)
        return True

    def _on_speech_utterance(self, msg: SpeechUtterance) -> None:
        """Create one reply obligation from one admitted final ASR turn."""

        utterance_id = str(msg.utterance_id or "").strip()
        text = " ".join(str(msg.text or "").split())
        speaker_role = str(msg.speaker_role or "").strip().lower()
        if (
            not bool(msg.is_final)
            or not utterance_id
            or not text
            or (speaker_role and speaker_role != "surgeon")
            or not str(
                getattr(self, "_gateway_instance_id", "") or ""
            ).strip()
            or not str(getattr(self, "_procedure_run_id", "") or "").strip()
        ):
            return
        observation_stamp = msg.end_stamp
        if int(observation_stamp.sec) == 0 and int(observation_stamp.nanosec) == 0:
            observation_stamp = msg.stamp
        received_at = (
            float(observation_stamp.sec)
            + float(observation_stamp.nanosec) / 1_000_000_000.0
        )
        if received_at <= 0.0:
            received_at = self._causal_now_sec()
        turn = self._dialogue_turn_gate.enqueue(
            utterance_id=utterance_id,
            text=text,
            procedure_run_id=str(getattr(self, "_procedure_run_id", "")),
            received_at=received_at,
            speaker_role=speaker_role,
            language=str(msg.language or "").strip(),
            source=str(msg.source or "").strip(),
        )
        if turn is None:
            return
        self._append_public_speech(
            turn.text,
            utterance_id=turn.utterance_id,
            responded=False,
        )
        self._trigger_inference_for_public_speech()

    def _ingest_public_twin_event(self, msg: TwinEvent, detail: dict[str, Any]) -> None:
        event_type = str(msg.event_type or "")
        tool_id = str(msg.instrument_id or detail.get("requested_tool") or detail.get("tool") or "")
        voice_text = str(detail.get("voice_text") or detail.get("text") or "")
        if event_type == "VoiceTranscriptObserved":
            if tool_id:
                history = getattr(self, "_validated_tool_request_history", None)
                if history is not None:
                    history.append(
                        {
                            "tool": tool_id,
                            "at": float(msg.stamp.sec)
                            + float(msg.stamp.nanosec) / 1_000_000_000.0,
                        }
                    )
            if self._append_public_speech(voice_text):
                self._trigger_inference_for_public_speech()

    def _on_request_text(self, msg: String) -> None:
        if self._append_public_speech(msg.data):
            self._trigger_inference_for_public_speech()

    def _trigger_inference_for_public_speech(self) -> None:
        if not self._active or self._response_mode != "live":
            return
        # Speech remains in the public evidence window. Queue it for the same
        # single consumer as visual frames so callbacks never block on model
        # latency and the latest evidence is evaluated after the active call.
        self._queue_inference(INFERENCE_TRIGGER_SPEECH)

    def _on_bed_robot_arm_group_request(self, msg: BedRobotArmGroupRequest) -> None:
        """Track only VLM-routed fine retraction-adjustment requests."""
        if str(msg.group_id) != "retraction" or str(msg.operation) not in {
            "retraction",
            "retraction_adjustment",
        }:
            return
        if str(msg.direction_frame) != "surgeon_view":
            return
        if str(msg.adjustment_mode) == "single":
            if str(msg.target_retractor_id) not in {
                "left_malleable",
                "right_malleable",
                "left_army_navy",
                "right_army_navy",
            }:
                return
        elif str(msg.adjustment_mode) == "multi":
            if str(msg.target_retractor_id) not in {
                "both_malleable",
                "both_army_navy",
            }:
                return
        else:
            return
        self._append_public_speech(msg.voice_text)
        request_id = str(msg.request_id).strip()
        if not request_id:
            self.get_logger().warning("Ignoring retraction request without request_id")
            return
        if (
            self._latest_bed_robot_arm_group_request is not None
            and self._latest_bed_robot_arm_group_request.request_id == request_id
        ):
            return
        if self._latest_bed_robot_arm_group_request is not None:
            self.get_logger().warning(
                "Ignoring retraction request %s while request %s is still pending"
                % (
                    request_id,
                    self._latest_bed_robot_arm_group_request.request_id,
                )
            )
            return
        self._latest_bed_robot_arm_group_request = msg
        self._last_bed_robot_arm_group_proposal_request_id = ""

    def _on_bed_robot_arm_group_status(self, msg: BedRobotArmGroupStatus) -> None:
        if str(msg.group_id) != "retraction":
            return
        pending = self._latest_bed_robot_arm_group_request
        if pending is None or str(msg.request_id) != str(pending.request_id):
            return
        if bool(msg.terminal):
            self._latest_bed_robot_arm_group_request = None

    def _retraction_group_states(self) -> list[Any]:
        if self._world is None:
            return []
        return [
            state
            for state in getattr(self._world, "bed_robot_arm_groups", [])
            if str(getattr(state, "group_id", "")) == "retraction"
        ]

    def _bed_robot_arm_group_state_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for state in self._retraction_group_states():
            group_id = str(getattr(state, "group_id", ""))
            rows.append(
                {
                    "group_id": group_id,
                    "connected": bool(getattr(state, "connected", False)),
                    "state": str(getattr(state, "state", "")),
                    "operation": str(getattr(state, "operation", "")),
                    "arm_id": str(getattr(state, "arm_id", "")),
                    "target_tool_id": str(getattr(state, "target_tool_id", "")),
                    "adjustment_mode": str(getattr(state, "adjustment_mode", "")),
                    "target_retractor_id": str(
                        getattr(state, "target_retractor_id", "")
                    ),
                    "direction_frame": str(getattr(state, "direction_frame", "")),
                    "direction": str(getattr(state, "direction", "")),
                    "axis": str(getattr(state, "axis", "")),
                    "distance_mm": round(float(getattr(state, "distance_mm", 0.0)), 3),
                    "distance_origin": str(getattr(state, "distance_origin", "")),
                    "end_effector_profile": str(
                        getattr(state, "end_effector_profile", "")
                    ),
                    "active_request_id": str(getattr(state, "active_request_id", "")),
                    "progress": round(float(getattr(state, "progress", 0.0)), 3),
                    "error_code": str(getattr(state, "error_code", "")),
                    "rejection_reason": str(getattr(state, "rejection_reason", "")),
                }
            )
        return rows

    def _pending_bed_robot_arm_group_request_context(self) -> dict[str, Any] | None:
        request = self._latest_bed_robot_arm_group_request
        if request is None:
            return None
        return {
            "request_id": str(request.request_id),
            "group_id": str(request.group_id),
            "operation": str(request.operation),
            "voice_text": str(request.voice_text),
            "procedure_id": str(request.procedure_id),
            "adjustment_mode": str(request.adjustment_mode),
            "target_retractor_id": str(request.target_retractor_id),
            "direction_frame": str(request.direction_frame),
            "end_effector_profile": str(request.end_effector_profile),
            "source": str(request.source),
        }

    def _on_skill_status(self, msg: SkillStatus) -> None:
        if msg.state != "completed":
            return
        self._recent_skill_statuses.append(
            {
                "at": round(self._causal_now_sec(), 2),
                "action": msg.action,
                "tool": msg.instrument_id,
                "state": msg.state,
                "success": bool(msg.success),
                "message": msg.message,
            }
        )

    def _on_control(self, msg: String) -> None:
        command, _, start_phase_id = msg.data.strip().partition(":")
        command = command.strip().lower()
        start_phase_id = start_phase_id.strip()
        signature = (command, start_phase_id)
        if command in {
            "start",
            "start_actors",
            "pause",
            "resume",
            "stop",
        }:
            if signature == getattr(
                self, "_last_lifecycle_control_signature", None
            ):
                return
            self._last_lifecycle_control_signature = signature
        if command in {"start", "start_actors"}:
            # The interactive replay controller publishes a retained start
            # heartbeat while it is active.  Treat that heartbeat as
            # idempotent: resetting the epoch here would discard every
            # in-flight live inference whose latency exceeds the heartbeat
            # period.
            self._activate_lifecycle(start_phase_id)
        elif command == "pause":
            self._reset_model_input_dedupe(advance_epoch=True)
            self._active = False
            self._inference_backpressure.clear_pending()
            self._reset_source_time_live_trigger(reset_stamp=False)
        elif command == "resume":
            self._reset_model_input_dedupe(advance_epoch=True)
            self._active = True
        elif command == "stop":
            self._stop_lifecycle()
        elif command == "reset":
            self._last_lifecycle_control_signature = None
            self._reset_model_input_dedupe(advance_epoch=True)
            self._active = False
            self._inference_backpressure.clear_pending()
            self._world = None
            self._simulation = None
            self._last_vlm_phase = ""
            self._last_authoritative_phase = ""
            self._phase_bootstrap_id = ""
            self._phase_bootstrap_observation_count = 0
            self._phase_bootstrap_explicit = False
            self._phase_entered_wall_sec = self._causal_now_sec()
            self._last_good_raw = ""
            self._last_good_payload = None
            self._reset_public_evidence()
            self._oracle_tick = 0
            self._last_replay_image_stamp_sec = None
            self._last_periodic_live_image_stamp_sec = None
            self._last_submitted_live_image_stamp_sec = None
            self._reset_source_time_live_trigger(reset_stamp=True)
            self._reset_fast_cam4_mayo_observations()

    def _reset_model_input_dedupe(self, *, advance_epoch: bool) -> None:
        if advance_epoch:
            self._model_input_epoch = (
                max(0, int(getattr(self, "_model_input_epoch", 0))) + 1
            )
            self._vlm_result_sequence = 0
            dialogue_gate = getattr(self, "_dialogue_turn_gate", None)
            if dialogue_gate is not None:
                dialogue_gate.reset(epoch=self._model_input_epoch)
            failure_backoff = getattr(self, "_transport_failure_backoff", None)
            if failure_backoff is not None:
                failure_backoff.record_success()
            self._reset_inference_failure_log_throttle()
        self._last_submitted_model_input_key = ""

    def _release_dialogue_claim(self, correlation_id: str) -> bool:
        dialogue_gate = getattr(self, "_dialogue_turn_gate", None)
        if dialogue_gate is None:
            return False
        return dialogue_gate.release(correlation_id=correlation_id)

    def _next_visual_evidence_metadata(
        self,
        model_input_key: str,
    ) -> tuple[int, int, str]:
        epoch = max(0, int(getattr(self, "_model_input_epoch", 0)))
        sequence = max(0, int(getattr(self, "_vlm_result_sequence", 0))) + 1
        self._vlm_result_sequence = sequence
        correlation_id = f"vlm-{epoch}-{sequence}-{model_input_key[:12]}"
        return epoch, sequence, correlation_id

    @staticmethod
    def _set_visual_evidence_metadata(
        message,
        *,
        source_epoch: int,
        source_sequence: int,
        correlation_id: str,
        source: str = "",
    ) -> None:
        if hasattr(message, "source") and source:
            message.source = source
        message.source_epoch = max(0, int(source_epoch))
        message.source_sequence = max(0, int(source_sequence))
        message.correlation_id = str(correlation_id)

    def _current_model_input_signature(
        self,
        request_context_json: str,
        images: list[tuple[str, bytes, str]],
    ) -> str:
        return model_input_signature(
            runtime_epoch=int(getattr(self, "_model_input_epoch", 0)),
            request_config={
                "provider_id": str(getattr(self, "_provider_id", "")),
                "model_id": str(getattr(self, "_model_id", "")),
                "api_mode": str(getattr(self, "_api_mode", "")),
                "response_format": str(
                    getattr(self, "_response_format", "")
                ),
                "reasoning_effort": str(
                    getattr(self, "_reasoning_effort", "")
                ),
                "temperature": float(getattr(self, "_temperature", 0.0)),
                "top_p": float(getattr(self, "_top_p", 1.0)),
                "max_output_tokens": int(
                    getattr(self, "_max_output_tokens", 0)
                ),
                "generation_seed": getattr(
                    self,
                    "_generation_seed",
                    None,
                ),
                "json_schema": getattr(self, "_json_schema", {}),
            },
            system_prompt=str(getattr(self, "_system_prompt", "")),
            developer_instruction=str(
                getattr(self, "_developer_instruction", "")
            ),
            request_context_json=request_context_json,
            observation_metadata=dict(
                getattr(self, "_current_visual_input", {})
            ).get("sources", []),
            images=images,
        )

    def _reset_public_evidence(self) -> None:
        self._recent_events.clear()
        history = getattr(self, "_completed_handover_history", None)
        if history is not None:
            history.clear()
        request_history = getattr(self, "_validated_tool_request_history", None)
        if request_history is not None:
            request_history.clear()
        self._reset_transient_public_evidence()
        self._latest_bed_robot_arm_group_request = None
        self._last_bed_robot_arm_group_proposal_request_id = ""
        self._phase_entered_wall_sec = self._causal_now_sec()

    def _reset_transient_public_evidence(self) -> None:
        self._recent_speech.clear()
        self._recent_skill_statuses.clear()

    def _authoritative_runtime_phase(self) -> str:
        for runtime_state in (
            getattr(self, "_world", None),
            getattr(self, "_simulation", None),
        ):
            phase_id = str(getattr(runtime_state, "filtered_phase", "") or "")
            if phase_id in self._spec.phase_ids:
                return phase_id
        bootstrap_phase = str(getattr(self, "_phase_bootstrap_id", "") or "")
        if bootstrap_phase in self._spec.phase_ids:
            return bootstrap_phase
        return self._spec.default_phase_id

    def _track_authoritative_phase(self) -> None:
        phase_id = self._authoritative_runtime_phase()
        if phase_id == getattr(self, "_last_authoritative_phase", ""):
            return
        self._last_authoritative_phase = phase_id
        self._phase_entered_wall_sec = self._causal_now_sec()

    def _bt_snapshot_msg(self) -> BTContextSnapshot:
        snapshot = BTContextSnapshot()
        if self._latest_bt is None:
            return snapshot
        snapshot.stamp = self._latest_bt.stamp
        snapshot.procedure_id = self._world.procedure_id if self._world is not None else ""
        snapshot.filtered_phase = self._world.filtered_phase if self._world is not None else ""
        snapshot.decision = self._latest_bt.decision
        snapshot.selected_tool = self._latest_bt.selected_tool
        snapshot.selected_tool_lifecycle = self._latest_bt.selected_tool_lifecycle
        snapshot.next_required_transition = self._latest_bt.next_required_transition
        snapshot.blocking_guard = self._latest_bt.blocking_guard
        snapshot.decision_reason = self._latest_bt.decision_reason
        snapshot.rationale = self._latest_bt.rationale
        return snapshot

    def _publish_context_summaries(self) -> None:
        if self._world is None or self._simulation is None:
            return
        context_msg, context_dict = self._assemble_context()
        if self._context_mode != "actor_log":
            self._request_context_pub.publish(context_msg)

        phase_summary = String()
        phase_summary.data = compact_json(
            {
                "proc": self._world.procedure_id,
                "ph": self._world.filtered_phase,
                "conf": round(float(self._world.phase_confidence), 3),
                "unc": bool(self._world.phase_uncertain),
                "req": self._world.surgeon_request_tool or self._world.explicit_request_tool,
            }
        )
        self._phase_summary_pub.publish(phase_summary)

        tool_summary = String()
        tool_summary.data = compact_json(
            {
                "active": context_dict["tools"],
                "pending": context_dict["pending"],
            }
        )
        self._tool_summary_pub.publish(tool_summary)

    def _assemble_context(
        self,
        *,
        perception_reference_stamp_sec: float | None = None,
    ) -> tuple[VLMRequestContext, dict[str, Any]]:
        """Build the bounded context used by the default Live VLM request.

        The typed CAM3/CAM4 RF-DETR projection belongs in this exact request
        context, not only in the actor-log prompt variant.  The optional
        reference timestamp lets inference bind the detector facts to the
        model frame it is about to submit, while periodic observability uses
        the newest fresh view-local observations without inventing alignment.
        """

        assert self._world is not None
        assert self._simulation is not None
        active_tools: list[str] = []
        non_home_tools: list[str] = []
        tool_rows: list[dict[str, Any]] = []
        for instrument in self._world.instrument_states:
            at_home = (
                instrument.location_id == instrument.home_location_id
                and instrument.location_type == instrument.home_location_type
                and instrument.lifecycle_stage in {"home_rack", "returned_home"}
            )
            is_context_relevant = (
                not at_home
                or instrument.instrument_id in set(self._world.expected_instruments)
                or instrument.instrument_id in set(self._world.pending_transition_tools)
                or instrument.instrument_id
                in {
                    self._world.right_hand_tool,
                    self._world.left_hand_tool,
                    self._world.prepositioned_tool,
                    self._world.surgeon_request_tool,
                    self._world.explicit_request_tool,
                }
            )
            if at_home:
                continue
            non_home_tools.append(instrument.instrument_id)
            if is_context_relevant:
                active_tools.append(instrument.instrument_id)
                tool_rows.append(
                    {
                        "id": instrument.instrument_id,
                        "lc": instrument.lifecycle_stage,
                        "loc": instrument.location_id,
                        "lt": instrument.location_type,
                        "nx": instrument.next_required_transition,
                        "own": instrument.owner,
                    }
                )

        recent_events = list(self._recent_events)[-6:]
        bt_snapshot = self._bt_snapshot_msg()
        context_dict = {
            "proc": self._world.procedure_id,
            "ph": {
                "id": self._world.filtered_phase,
                "c": round(float(self._world.phase_confidence), 3),
                "u": bool(self._world.phase_uncertain),
            },
            "rq": {
                "exp": self._world.explicit_request_tool,
                "surgeon": self._world.surgeon_request_tool,
                "intent": self._world.surgeon_intent,
            },
            "hands": {
                "rh": self._world.right_hand_tool,
                "lh": self._world.left_hand_tool,
                "pre": self._world.prepositioned_tool,
                "cb": bool(self._world.cleaner_busy),
                "ct": round(float(self._world.cleaner_remaining_sec), 2),
            },
            "exp": list(self._world.expected_instruments),
            "tools": tool_rows,
            "pending": list(self._world.pending_transition_tools),
            "bed_robot_arm_groups": self._bed_robot_arm_group_state_rows(),
            "pending_bed_robot_arm_group_request": (
                self._pending_bed_robot_arm_group_request_context()
            ),
            "ev": [
                {
                    "t": event.event_type,
                    "tool": event.instrument_id,
                    "a": event.anchor_id,
                    "r": event.reason,
                }
                for event in recent_events
            ],
            "bt": {
                "d": bt_snapshot.decision,
                "tool": bt_snapshot.selected_tool,
                "lc": bt_snapshot.selected_tool_lifecycle,
                "nx": bt_snapshot.next_required_transition,
                "why": bt_snapshot.decision_reason or bt_snapshot.rationale,
                "blk": bt_snapshot.blocking_guard,
            },
            # This is a typed projection from ToolObservation2DArray, never a
            # detector raster.  It contains bounded box/point/depth metadata
            # only; masks and overlay/image bytes are rejected upstream.
            "observable_perception": self._public_perception_context(
                perception_reference_stamp_sec
            ),
        }

        msg = VLMRequestContext()
        msg.stamp = self._world.stamp
        msg.procedure_id = self._world.procedure_id
        msg.filtered_phase = self._world.filtered_phase
        msg.phase_confidence = float(self._world.phase_confidence)
        msg.phase_uncertain = bool(self._world.phase_uncertain)
        msg.explicit_request_tool = self._world.explicit_request_tool
        msg.surgeon_request_tool = self._world.surgeon_request_tool
        msg.surgeon_intent = self._world.surgeon_intent
        msg.right_hand_tool = self._world.right_hand_tool
        msg.left_hand_tool = self._world.left_hand_tool
        msg.prepositioned_tool = self._world.prepositioned_tool
        msg.cleaner_busy = bool(self._world.cleaner_busy)
        msg.cleaner_remaining_sec = float(self._world.cleaner_remaining_sec)
        msg.phase_expected_tools = list(self._world.expected_instruments)
        msg.active_tool_ids = active_tools
        msg.non_home_tool_ids = non_home_tools
        msg.pending_transition_tools = list(self._world.pending_transition_tools)
        msg.recent_events = recent_events
        msg.bt_snapshot = bt_snapshot
        msg.bed_robot_arm_groups = self._retraction_group_states()
        pending_group_request = self._latest_bed_robot_arm_group_request
        msg.has_pending_bed_robot_arm_group_request = pending_group_request is not None
        if pending_group_request is not None:
            msg.pending_bed_robot_arm_group_request = pending_group_request
        msg.compact_json = compact_json(context_dict)
        return msg, context_dict

    def _public_event_digests(self) -> list[EventDigest]:
        return [event for event in self._recent_events if event.event_type in PUBLIC_DIGITAL_TWIN_EVENT_TYPES][-6:]

    def _public_digital_twin_context(self) -> dict[str, Any]:
        if self._world is None or self._simulation is None:
            return {}
        _, world_context = self._assemble_context()
        public_events = [
            {
                "t": event.event_type,
                "tool": event.instrument_id,
                "anchor": event.anchor_id,
                "stamp_sec": float(event.stamp.sec) + float(event.stamp.nanosec) / 1_000_000_000.0,
            }
            for event in self._public_event_digests()
        ]
        return {
            "hands": world_context.get("hands", {}),
            "tools": world_context.get("tools", []),
            "forecast_inventory": self._public_forecast_inventory_context(),
            "completed_handovers": list(
                getattr(self, "_completed_handover_history", [])
            )[-8:],
            "tool_requests": list(
                getattr(self, "_validated_tool_request_history", [])
            )[-8:],
            "bed_robot_arm_groups": self._bed_robot_arm_group_state_rows(),
            "events": public_events[-6:],
        }

    def _public_forecast_inventory_context(self) -> dict[str, list[list[Any]]]:
        """Summarize which public DT instances could support a new handover."""

        if self._world is None:
            return {
                "available": [],
                "rack_available": [],
                "mayo_reuse": [],
                "unavailable": [],
            }
        requestable = set(self._spec.list_requestable_instrument_ids())
        counts = {
            tool_id: {
                "rack_available": 0,
                "mayo_reuse": 0,
                "unavailable": 0,
            }
            for tool_id in requestable
        }
        for instrument in getattr(self._world, "instrument_states", []):
            tool_id = str(getattr(instrument, "instrument_id", "") or "")
            if tool_id not in counts:
                continue
            lifecycle = str(
                getattr(instrument, "lifecycle_stage", "") or ""
            )
            owner = str(getattr(instrument, "owner", "") or "")
            contaminated = bool(
                getattr(instrument, "contaminated", False)
            )
            future_use_expected = bool(
                getattr(
                    instrument,
                    "procedure_future_use_expected",
                    False,
                )
            )
            if (
                lifecycle in {"home_rack", "returned_home"}
                and owner in {"", "none"}
                and not contaminated
            ):
                bucket = "rack_available"
            elif (
                lifecycle == "mayo_reuse"
                and owner in {"", "none"}
                and future_use_expected
                and str(getattr(instrument, "location_type", "") or "")
                == CANONICAL_MAYO_POLICY_LOCATION
                and str(getattr(instrument, "location_id", "") or "")
                == CANONICAL_MAYO_POLICY_LOCATION
            ):
                bucket = "mayo_reuse"
            else:
                bucket = "unavailable"
            counts[tool_id][bucket] += 1

        ordered_ids = [
            str(instrument.id)
            for instrument in self._spec.bundle.instruments
            if str(instrument.id) in counts
        ]
        by_source = {
            bucket: [
                [tool_id, counts[tool_id][bucket]]
                for tool_id in ordered_ids
                if counts[tool_id][bucket] > 0
            ]
            for bucket in (
                "rack_available",
                "mayo_reuse",
                "unavailable",
            )
        }
        by_source["available"] = [
            [
                tool_id,
                counts[tool_id]["rack_available"]
                + counts[tool_id]["mayo_reuse"],
            ]
            for tool_id in ordered_ids
            if (
                counts[tool_id]["rack_available"]
                + counts[tool_id]["mayo_reuse"]
            )
            > 0
        ]
        return by_source

    def _perception_samples(
        self,
        kind: str,
    ) -> list[tuple[float, dict[str, Any]]]:
        buffer = getattr(self, "_perception_buffers", {}).get(kind)
        samples = list(buffer) if buffer else []
        latest = self._latest_perception.get(kind)
        if latest is not None and (
            not samples or samples[-1] is not latest
        ):
            samples.append(latest)
        return samples

    def _closest_perception_sample(
        self,
        kind: str,
        *,
        reference_stamp_sec: float | None,
        stamp_key: str,
    ) -> tuple[float, dict[str, Any]] | None:
        now = self._causal_now_sec()
        fresh = [
            sample
            for sample in self._perception_samples(kind)
            if max(0.0, now - float(sample[0]))
            <= self._perception_stale_sec
        ]
        if not fresh:
            return None
        if reference_stamp_sec is None:
            return fresh[-1]

        stamped: list[tuple[float, dict[str, Any], float]] = []
        for received_at, summary in fresh:
            stamp_sec = _finite_float(summary.get(stamp_key))
            if stamp_sec is not None:
                stamped.append((received_at, summary, stamp_sec))
        if not stamped:
            return fresh[-1]
        received_at, summary, _ = min(
            stamped,
            key=lambda sample: abs(
                sample[2] - float(reference_stamp_sec)
            ),
        )
        return received_at, summary

    def _enrich_cam4_tool_stability(
        self,
        summary: dict[str, Any],
        *,
        reference_stamp_sec: float,
    ) -> dict[str, Any]:
        """Add bounded dwell evidence from source-aligned CAM4 summaries."""

        start_sec = (
            float(reference_stamp_sec) - CAM4_MAYO_STABILITY_WINDOW_SEC
        )
        histories: dict[str, list[tuple[float, float]]] = {}
        for _received_at, sample in self._perception_samples(
            "cam4_semantics"
        ):
            stamp_sec = _finite_float(sample.get("source_stamp_sec"))
            if (
                stamp_sec is None
                or stamp_sec < start_sec
                or stamp_sec
                > float(reference_stamp_sec)
                + self._perception_image_max_skew_sec
            ):
                continue
            for row in sample.get("tools", []):
                if not isinstance(row, dict):
                    continue
                name = str(row.get("name", "")).strip()
                confidence = _finite_float(
                    row.get("max_confidence")
                )
                if name and confidence is not None:
                    histories.setdefault(name, []).append(
                        (stamp_sec, confidence)
                    )

        enriched = dict(summary)
        # Retire the legacy detector-derived request field before this summary
        # can enter the VLM context. Dedicated perception owns that signal.
        enriched.pop("tool_request", None)
        enriched_tools: list[dict[str, Any]] = []
        for row in summary.get("tools", []):
            if not isinstance(row, dict):
                continue
            bounded = dict(row)
            history = histories.get(str(row.get("name", "")).strip(), [])
            bounded["stable_sample_count"] = len(history)
            bounded["stable_duration_sec"] = round(
                max(0.0, history[-1][0] - history[0][0])
                if history
                else 0.0,
                6,
            )
            enriched_tools.append(bounded)
        enriched["tools"] = enriched_tools
        return enriched

    def _structured_rfdetr_tool_context(
        self,
        *,
        reference_stamp_sec: float | None,
        now: float,
    ) -> dict[str, Any] | None:
        """Return fresh typed CAM3/CAM4 tool boxes and pairing diagnostics.

        This is intentionally a context projection rather than a Digital Twin
        mutation. A 2D image region can help the VLM identify where a visible
        tool is in its *named view*, but cannot prove ownership, Mayo
        placement, or a world/TCP pose. In particular, a fresh CAM3/CAM4
        observation is not discarded merely because the FLIR image is absent
        or timestamp-misaligned: that would erase current view-local location
        evidence. ``visual_alignment`` is therefore diagnostic only and is
        separate from fresh detector availability.
        """

        configured_topics = {
            "cam_3": str(
                getattr(self, "_cam3_tool_observations_topic", "")
            ).strip(),
            "cam_4": str(
                getattr(self, "_cam4_tool_observations_topic", "")
            ).strip(),
        }
        configured_views = [
            view for view, topic in configured_topics.items() if topic
        ]
        if not configured_views:
            return None

        per_view_limit = max(
            1,
            int(getattr(self, "_perception_max_instances", 0))
            // max(1, len(configured_views)),
        )
        freshness: dict[str, dict[str, Any]] = {}
        visual_alignment: dict[str, dict[str, Any]] = {}
        views: list[dict[str, Any]] = []
        for view in configured_views:
            payload = self._closest_perception_sample(
                f"rfdetr_{view}_tools",
                reference_stamp_sec=reference_stamp_sec,
                stamp_key="source_stamp_sec",
            )
            if payload is None:
                freshness[view] = {"status": "missing"}
                visual_alignment[view] = {
                    "status": "not_compared_no_fresh_detector",
                }
                continue
            received_monotonic, summary = payload
            received_age_sec = max(0.0, now - received_monotonic)
            if received_age_sec > self._perception_stale_sec:
                freshness[view] = {
                    "status": "stale",
                    "received_age_sec": round(received_age_sec, 3),
                }
                visual_alignment[view] = {
                    "status": "not_compared_stale_detector",
                }
                continue
            summary_stamp_sec = _finite_float(
                summary.get("source_stamp_sec")
            )
            freshness[view] = {
                "status": "fresh",
                "received_age_sec": round(received_age_sec, 3),
            }
            if summary_stamp_sec is None:
                visual_alignment[view] = {
                    "status": "not_compared_missing_source_timestamp",
                }
            elif reference_stamp_sec is None:
                visual_alignment[view] = {
                    "status": "not_compared_no_flir_reference",
                    "detector_stamp_sec": round(summary_stamp_sec, 6),
                }
            else:
                offset_sec = float(summary_stamp_sec) - float(
                    reference_stamp_sec
                )
                visual_alignment[view] = {
                    "status": (
                        "aligned"
                        if abs(offset_sec)
                        <= self._perception_image_max_skew_sec + 1.0e-9
                        else "misaligned"
                    ),
                    "detector_stamp_sec": round(summary_stamp_sec, 6),
                    "offset_sec": round(offset_sec, 6),
                }

            instances: list[dict[str, Any]] = []
            raw_instances = summary.get("instances", [])
            if isinstance(raw_instances, list):
                for raw_instance in raw_instances[:per_view_limit]:
                    if not isinstance(raw_instance, dict):
                        continue
                    instance = dict(raw_instance)
                    # The detector label may be a clinical name rather than a
                    # procedure runtime ID.  Only add an ID when the procedure
                    # aliases resolve it; never invent one from a class index.
                    tool_id = self._canonical_tool_id(
                        instance.get("class_name", "")
                    )
                    if tool_id:
                        instance["tool_id"] = tool_id
                    instances.append(instance)
            source_truncated = bool(summary.get("truncated", False))
            view_truncated = source_truncated or (
                len(raw_instances) > len(instances)
                if isinstance(raw_instances, list)
                else False
            )
            bounded_view: dict[str, Any] = {
                "view": view,
                "sequence": int(summary.get("sequence", 0)),
                "model_version": str(summary.get("model_version", ""))[:80],
                "ontology_version": str(
                    summary.get("ontology_version", "")
                )[:80],
                "instances": instances,
                "detection_status": str(
                    summary.get("detection_status", "")
                )[:40],
                "truncated": view_truncated,
                "freshness": dict(freshness[view]),
                "visual_alignment": dict(visual_alignment[view]),
            }
            if summary_stamp_sec is not None:
                bounded_view["source_stamp_sec"] = round(
                    summary_stamp_sec,
                    6,
                )
            views.append(bounded_view)

        return {
            "schema": "taskplanner.rfdetr_multiview_tool_context.v1",
            "source": "rfdetr_tool_observation_2d",
            "ground_truth": False,
            "flir_reference_stamp_sec": (
                round(float(reference_stamp_sec), 6)
                if reference_stamp_sec is not None
                else None
            ),
            "max_source_skew_sec": self._perception_image_max_skew_sec,
            "tool_detection_views": views,
            "freshness": freshness,
            "visual_alignment": visual_alignment,
            "mask_rle_forwarded_to_vlm": False,
        }

    def _public_perception_context(
        self,
        image_stamp_sec: float | None = None,
    ) -> dict[str, Any]:
        now = self._causal_now_sec()
        reference_stamp_sec = (
            image_stamp_sec
            if image_stamp_sec is not None
            else getattr(
                self,
                "_current_perception_reference_stamp_sec",
                None,
            )
        )
        structured_context = self._structured_rfdetr_tool_context(
            reference_stamp_sec=reference_stamp_sec,
            now=now,
        )
        if structured_context is not None:
            return structured_context
        cam4_semantics_topic = (
            str(getattr(self, "_cam4_semantics_topic", "")).strip()
            if bool(
                getattr(
                    self,
                    "_allow_legacy_cam4_semantics_fallback",
                    True,
                )
            )
            else ""
        )
        if cam4_semantics_topic:
            payload = self._closest_perception_sample(
                "cam4_semantics",
                reference_stamp_sec=reference_stamp_sec,
                stamp_key="source_stamp_sec",
            )
            if payload is None:
                return {
                    "source": "cam4_rfdetr_small",
                    "ground_truth": False,
                    "cam4_image_forwarded_to_vlm": False,
                    "alignment": {"status": "missing"},
                }
            received_monotonic, summary = payload
            received_age_sec = max(0.0, now - received_monotonic)
            if received_age_sec > self._perception_stale_sec:
                return {
                    "source": "cam4_rfdetr_small",
                    "ground_truth": False,
                    "cam4_image_forwarded_to_vlm": False,
                    "alignment": {
                        "status": "omitted_receive_stale",
                        "received_age_sec": round(received_age_sec, 3),
                    },
                }
            if reference_stamp_sec is None:
                return {
                    "source": "cam4_rfdetr_small",
                    "ground_truth": False,
                    "cam4_image_forwarded_to_vlm": False,
                    "alignment": {"status": "omitted_no_flir_reference"},
                }
            summary_stamp_sec = _finite_float(
                summary.get("source_stamp_sec")
            )
            if summary_stamp_sec is None:
                return {
                    "source": "cam4_rfdetr_small",
                    "ground_truth": False,
                    "cam4_image_forwarded_to_vlm": False,
                    "alignment": {
                        "status": "omitted_missing_source_timestamp"
                    },
                }
            offset_sec = float(summary_stamp_sec) - float(reference_stamp_sec)
            if (
                abs(offset_sec)
                > self._perception_image_max_skew_sec + 1.0e-9
            ):
                return {
                    "source": "cam4_rfdetr_small",
                    "ground_truth": False,
                    "cam4_image_forwarded_to_vlm": False,
                    "alignment": {
                        "status": "omitted_source_timestamp_misaligned",
                        "detector_stamp_sec": round(summary_stamp_sec, 6),
                        "offset_sec": round(offset_sec, 6),
                    },
                }
            aligned_summary = self._enrich_cam4_tool_stability(
                summary,
                reference_stamp_sec=float(reference_stamp_sec),
            )
            return {
                **aligned_summary,
                "flir_reference_stamp_sec": round(
                    float(reference_stamp_sec),
                    6,
                ),
                "max_source_skew_sec": (
                    self._perception_image_max_skew_sec
                ),
                "alignment": {
                    "status": "aligned",
                    "detector_stamp_sec": round(summary_stamp_sec, 6),
                    "offset_sec": round(offset_sec, 6),
                },
            }

        summaries: dict[str, Any] = {}
        alignment: dict[str, dict[str, Any]] = {}
        configured_topics = {
            "bboxes": str(
                getattr(self, "_perception_bboxes_topic", "")
            ).strip(),
            "segmentation": str(
                getattr(self, "_perception_segmentation_topic", "")
            ).strip(),
        }
        for kind in ("bboxes", "segmentation"):
            payload = self._closest_perception_sample(
                kind,
                reference_stamp_sec=reference_stamp_sec,
                stamp_key="timestamp_sec",
            )
            if payload is None:
                if configured_topics[kind]:
                    alignment[kind] = {"status": "missing"}
                continue
            received_monotonic, summary = payload
            received_age_sec = max(0.0, now - received_monotonic)
            if received_age_sec > self._perception_stale_sec:
                alignment[kind] = {
                    "status": "omitted_receive_stale",
                    "received_age_sec": round(received_age_sec, 3),
                }
                continue
            bounded_summary = dict(summary)
            if reference_stamp_sec is None:
                alignment[kind] = {
                    "status": "omitted_no_cam4_reference",
                }
                continue
            summary_stamp_sec = _finite_float(
                bounded_summary.get("timestamp_sec")
            )
            if summary_stamp_sec is None:
                alignment[kind] = {
                    "status": "omitted_missing_source_timestamp",
                }
                continue
            offset_sec = float(summary_stamp_sec) - float(reference_stamp_sec)
            skew_sec = abs(offset_sec)
            if skew_sec > self._perception_image_max_skew_sec + 1.0e-9:
                alignment[kind] = {
                    "status": "omitted_source_timestamp_misaligned",
                    "detector_stamp_sec": round(summary_stamp_sec, 6),
                    "offset_sec": round(offset_sec, 6),
                }
                continue
            bounded_summary["image_stamp_skew_sec"] = round(
                skew_sec,
                6,
            )
            summaries[kind] = bounded_summary
            alignment[kind] = {
                "status": "aligned",
                "detector_stamp_sec": round(summary_stamp_sec, 6),
                "offset_sec": round(offset_sec, 6),
            }
        if not summaries and not alignment:
            return {}
        return {
            "source": "cam4_public_detector",
            "bounded": True,
            "ground_truth": False,
            "cam4_reference_stamp_sec": (
                round(float(reference_stamp_sec), 6)
                if reference_stamp_sec is not None
                else None
            ),
            "max_source_skew_sec": self._perception_image_max_skew_sec,
            "alignment": alignment,
            **summaries,
        }

    def _actor_log_request_context_msg(
        self,
        context: dict[str, Any],
        compact_context: str,
        observation_stamp,
    ) -> VLMRequestContext:
        msg = VLMRequestContext()
        msg.stamp = observation_stamp
        msg.procedure_id = self._spec.procedure_id
        if self._world is not None:
            msg.filtered_phase = self._authoritative_runtime_phase()
            msg.phase_confidence = float(self._world.phase_confidence)
            msg.phase_uncertain = bool(self._world.phase_uncertain)
            msg.right_hand_tool = self._world.right_hand_tool
            msg.left_hand_tool = self._world.left_hand_tool
            msg.prepositioned_tool = self._world.prepositioned_tool
            msg.cleaner_busy = bool(self._world.cleaner_busy)
            msg.cleaner_remaining_sec = float(self._world.cleaner_remaining_sec)
            msg.phase_expected_tools = list(self._world.expected_instruments)
            msg.active_tool_ids = [str(row.get("id", "")) for row in context.get("digital_twin", {}).get("tools", []) if row.get("id")]
            msg.recent_events = self._public_event_digests()
            msg.bed_robot_arm_groups = self._retraction_group_states()
        pending_group_request = self._latest_bed_robot_arm_group_request
        msg.has_pending_bed_robot_arm_group_request = pending_group_request is not None
        if pending_group_request is not None:
            msg.pending_bed_robot_arm_group_request = pending_group_request
        msg.compact_json = compact_context
        return msg

    def _resolve_tool_mention(self, text: str) -> str:
        lowered = str(text or "").lower().replace("\xa0", " ").strip()
        if not lowered:
            return ""

        correction_parts = re.split(
            r"\b(?:not|instead(?:\s+of)?|rather(?:\s+than)?)\b|"
            r"(?:아니(?:야|고|라)?|말고)",
            lowered,
        )
        search_parts = (
            [correction_parts[-1]]
            if len(correction_parts) > 1
            else [lowered]
        )
        for part in search_parts:
            tokens = re.findall(r"[0-9a-z_가-힣#]+", part)
            spans = [
                (
                    start + width,
                    width,
                    " ".join(tokens[start : start + width]),
                )
                for width in range(1, len(tokens) + 1)
                for start in range(0, len(tokens) - width + 1)
            ]
            spans.sort(reverse=True)
            for _, _, candidate in spans:
                resolved = self._spec.resolve_instrument_alias(candidate)
                if resolved:
                    return resolved
        return ""

    def _tool_from_public_speech(self, speech_rows: list[dict[str, Any]]) -> str:
        now = self._causal_now_sec()
        for row in reversed(speech_rows):
            text = str(row.get("text", "")).strip()
            if not text:
                continue
            try:
                speech_at = float(row.get("at", 0.0))
            except (TypeError, ValueError):
                speech_at = 0.0
            if speech_at <= 0.0 or now - speech_at > PUBLIC_REQUEST_MAX_AGE_SEC:
                continue
            resolved = self._resolve_tool_mention(text)
            if not resolved:
                if re.search(
                    r"\b(?:cancel|no|never\s+mind)\b|"
                    r"(?:아니(?:야|요)?|취소)",
                    text.lower(),
                ):
                    return ""
                continue
            completed_after_request = any(
                str(status.get("state", "")) == "completed"
                and bool(status.get("success", False))
                and str(status.get("action", "")).lower()
                in HANDOVER_SKILL_ACTIONS
                and self._canonical_tool_id(status.get("tool", "")) == resolved
                and float(status.get("at", 0.0) or 0.0) >= speech_at
                for status in self._recent_skill_statuses
                if isinstance(status, dict)
            )
            if completed_after_request:
                return ""
            return resolved
        return ""

    def _tools_from_public_speech(self, speech_rows: list[dict[str, Any]]) -> list[str]:
        tools: list[str] = []
        for row in speech_rows:
            text = str(row.get("text", "")).strip()
            if not text:
                continue
            resolved = self._resolve_tool_mention(text)
            if resolved and resolved not in tools:
                tools.append(resolved)
        return tools

    def _canonical_tool_id(self, raw_tool: object) -> str:
        tool_id = str(raw_tool or "").strip()
        if not tool_id:
            return ""
        resolved = self._spec.resolve_instrument_alias(tool_id)
        if resolved:
            return resolved
        cleaned = tool_id.strip(" \t\r\n,;:'\"`[]{}()")
        resolved = self._spec.resolve_instrument_alias(cleaned)
        if resolved:
            return resolved
        match = re.search(r"(?i)\bt[\s_-]*0*\d+\b", cleaned)
        return self._spec.resolve_instrument_alias(match.group(0)) if match else ""

    def _canonical_phase_id(self, raw_phase: object) -> str:
        phase_id = str(raw_phase or "").strip()
        if not phase_id:
            return ""
        resolved = self._spec.resolve_phase_id(phase_id)
        if resolved:
            return resolved
        cleaned = phase_id.strip(" \t\r\n,;:'\"`[]{}()")
        resolved = self._spec.resolve_phase_id(cleaned)
        if resolved:
            return resolved
        match = re.search(r"(?i)\bp[\s_-]*0*\d+\b", cleaned)
        return self._spec.resolve_phase_id(match.group(0)) if match else ""

    @staticmethod
    def _canonical_intent_type(raw_intent: object) -> str:
        cleaned = str(raw_intent or "").strip().strip(" \t\r\n,;:'\"`[]{}()").lower()
        aliases = {
            "handover": "handover",
            "request": "request_tool",
            "request_tool": "request_tool",
            "voice_request": "request_tool",
            "return": "return_tool",
            "return_tool": "return_tool",
            "recover": "return_tool",
            "none": "none",
            "no_intent": "none",
            "": "none",
        }
        return aliases.get(cleaned, "none")

    def _canonicalize_payload_ids(self, payload: dict[str, Any]) -> dict[str, Any]:
        canonical = json.loads(json.dumps(payload))
        version = str(canonical.get("v", ""))
        if version in {"3", "4", "5", "6"}:
            canonical["phase"] = [
                [phase_id, float(row[1])]
                for row in canonical.get("phase", [])
                if isinstance(row, list)
                and len(row) == 2
                and (phase_id := self._canonical_phase_id(row[0]))
            ]
            canonical["tool"] = [
                [tool_id, float(row[1])]
                for row in canonical.get("tool", [])
                if isinstance(row, list)
                and len(row) == 2
                and (tool_id := self._canonical_tool_id(row[0]))
            ]
        elif version == "2":
            phase = canonical.get("phase", ["", 0.0])
            tool = canonical.get("tool", ["", 0.0])
            phase_id = (
                self._canonical_phase_id(phase[0])
                if isinstance(phase, list) and len(phase) == 2
                else ""
            )
            tool_id = (
                self._canonical_tool_id(tool[0])
                if isinstance(tool, list) and len(tool) == 2
                else ""
            )
            canonical["phase"] = [
                phase_id,
                float(phase[1]) if phase_id else 0.0,
            ]
            canonical["tool"] = [
                tool_id,
                float(tool[1]) if tool_id else 0.0,
            ]
        else:
            canonical["ph"] = [
                [phase_id, float(row[1])]
                for row in canonical.get("ph", [])
                if isinstance(row, list)
                and len(row) == 2
                and (phase_id := self._canonical_phase_id(row[0]))
            ]
            canonical["to"] = [
                [tool_id, str(row[1]), str(row[2]), float(row[3])]
                for row in canonical.get("to", [])
                if isinstance(row, list)
                and len(row) == 4
                and (tool_id := self._canonical_tool_id(row[0]))
            ]

        if version in {"2", "3", "4", "5", "6"}:
            intent = canonical.get("intent", ["none", "", 0.0])
        else:
            intent = None
        if isinstance(intent, list) and len(intent) == 3:
            intent_type = self._canonical_intent_type(intent[0])
            intent_tool = self._canonical_tool_id(intent[1])
            if intent_type == "none" or not intent_tool:
                canonical["intent"] = ["none", "", 0.0]
            else:
                canonical["intent"] = [
                    intent_type,
                    intent_tool,
                    float(intent[2]),
                ]

        mayo_rows = []
        for row in canonical.get("mayo", []):
            if not isinstance(row, list) or len(row) != 3:
                continue
            tool_id = self._canonical_tool_id(row[0])
            decision = str(row[1]).strip().strip(" \t\r\n,;:'\"`[]{}()").lower()
            if tool_id and decision in {"recover", "reuse"}:
                mayo_rows.append([tool_id, decision, float(row[2])])
        canonical["mayo"] = mayo_rows
        retrieve = canonical.get("mayo_retrieve", ["", 0.0])
        if isinstance(retrieve, list) and len(retrieve) == 2:
            retrieve_tool = self._canonical_tool_id(retrieve[0])
            canonical["mayo_retrieve"] = [
                retrieve_tool,
                float(retrieve[1]) if retrieve_tool else 0.0,
            ]
        canonical["mayo"], canonical["mayo_retrieve"] = normalize_mayo_semantics(
            canonical.get("mayo", []),
            canonical.get("mayo_retrieve", ["", 0.0]),
        )
        if version in {"5", "6"} and isinstance(canonical.get("function_call"), dict):
            function_call = dict(canonical["function_call"])
            function_name = str(function_call.get("name", "")).strip()
            arguments = function_call.get("arguments", {})
            if not isinstance(arguments, dict):
                arguments = {}
            if function_name == "request_tool_handover":
                tool_id = self._canonical_tool_id(arguments.get("tool_id", ""))
                if tool_id:
                    function_call["arguments"] = {"tool_id": tool_id}
                    canonical["function_call"] = function_call
                else:
                    canonical["function_call"] = None
            elif function_name == "adjust_retraction":
                command = str(arguments.get("command", "")).strip()
                target_side = str(arguments.get("target_side", "")).strip().lower()
                raw_distance = arguments.get("distance_m")
                if (
                    command == "adjust_retraction"
                    and target_side in {"left", "right", "both"}
                    and isinstance(raw_distance, (int, float))
                    and not isinstance(raw_distance, bool)
                    and math.isfinite(float(raw_distance))
                    and float(raw_distance) > 0.0
                ):
                    function_call["arguments"] = {
                        "command": command,
                        "distance_m": float(raw_distance),
                        "target_side": target_side,
                    }
                    canonical["function_call"] = function_call
                else:
                    canonical["function_call"] = None
            else:
                canonical["function_call"] = None
        return canonical

    def _actor_log_prior_evidence(
        self,
        *,
        open_set_phase_search: bool = False,
    ) -> dict[str, Any]:
        speech_rows = self._fresh_rows(
            self._recent_speech,
            max_age_sec=35.0 if open_set_phase_search else 18.0,
        )
        skill_statuses = self._fresh_rows(
            self._recent_skill_statuses,
            max_age_sec=35.0 if open_set_phase_search else 22.0,
        )
        public_events = [
            {
                "t": event.event_type,
                "tool": event.instrument_id,
                "anchor": event.anchor_id,
                "stamp_sec": float(event.stamp.sec) + float(event.stamp.nanosec) / 1_000_000_000.0,
            }
            for event in self._public_event_digests()
        ]
        hand_tools: list[str] = []
        recent_tools: list[str] = []
        # A model hypothesis is not a phase boundary. Only the authoritative
        # digital twin (or the operator-selected startup phase before its first
        # state arrives) may constrain the temporal candidate window.
        current_phase = self._authoritative_runtime_phase()
        mayo_tools: list[str] = []
        if self._world is not None:
            # For next-request prediction, only surgeon-owned tools count as
            # already progressed. Robot-held/prepositioned tools are visible
            # context, but the surgeon may still ask for them next.
            hand_tools = [
                instrument.instrument_id
                for instrument in self._world.instrument_states
                if instrument.lifecycle_stage == "surgeon_owned"
            ]
            mayo_tools = [
                instrument.instrument_id
                for instrument in self._world.instrument_states
                if instrument.lifecycle_stage in {"mayo_reuse", "mayo_recovery"}
                and instrument.location_type == CANONICAL_MAYO_POLICY_LOCATION
                and instrument.location_id == CANONICAL_MAYO_POLICY_LOCATION
            ]
        return {
            "current_phase": current_phase,
            # Model hypotheses are not phase boundaries. Until the open-set
            # bootstrap closes, keep the whole bounded evidence window even if
            # successive image-only guesses disagree.
            "phase_entered_sec": (
                0.0
                if open_set_phase_search
                else self._phase_entered_wall_sec
            ),
            "speech": speech_rows,
            "speech_tools": self._tools_from_public_speech([row for row in speech_rows if isinstance(row, dict)]),
            "recent_tools": recent_tools,
            "skill_status": skill_statuses,
            "mayo_tools": mayo_tools,
            "hand_tools": hand_tools,
            "events": public_events,
        }

    @staticmethod
    def _dialogue_turn_context(turn: DialogueTurn | None) -> dict[str, Any] | None:
        if turn is None:
            return None
        return {
            "turn_id": turn.turn_id,
            "utterance_id": turn.utterance_id,
            "text": turn.text,
            "speaker_role": turn.speaker_role or "surgeon",
            "language": turn.language,
            "reply_required": True,
        }

    def _fresh_rows(self, rows: deque[dict[str, Any]], *, max_age_sec: float) -> list[dict[str, Any]]:
        now = self._causal_now_sec()
        fresh: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                at = float(row.get("at", 0.0))
            except (TypeError, ValueError):
                at = 0.0
            if at <= 0.0 or now - at <= max_age_sec:
                fresh.append(dict(row))
        return fresh

    def _assemble_actor_log_context_dict(self) -> dict[str, Any]:
        open_set_phase_search = should_use_open_set_phase_bootstrap(
            getattr(self, "_phase_bootstrap_observation_count", 0),
            getattr(self, "_open_set_phase_bootstrap_observations", 0),
            getattr(self, "_phase_bootstrap_explicit", False),
        )
        evidence = self._actor_log_prior_evidence(
            open_set_phase_search=open_set_phase_search,
        )
        if open_set_phase_search:
            candidates = self._prior_scorer.score_open_set(evidence)
        else:
            candidates = self._prior_scorer.score(evidence)
            candidates = dict(candidates)
            candidate_evidence = dict(candidates.get("evidence", {}))
            candidate_evidence["phase_search_mode"] = "temporal_prior"
            candidates["evidence"] = candidate_evidence
        previous = {
            "phase": self._last_good_payload.get("phase", ["", 0.0]) if self._last_good_payload else ["", 0.0],
            "tool": self._last_good_payload.get("tool", ["", 0.0]) if self._last_good_payload else ["", 0.0],
        }
        if open_set_phase_search:
            previous = {"phase": [], "tool": []}
        digital_twin = self._public_digital_twin_context()
        frozen_ngram_prior = None
        handover_ngram_prior = getattr(self, "_handover_ngram_prior", None)
        if handover_ngram_prior is not None:
            frozen_ngram_prior = handover_ngram_prior.predict(
                phase_id=evidence.get("current_phase", ""),
                completed_handovers=digital_twin.get("completed_handovers", []),
            )
        dialogue_gate = getattr(self, "_dialogue_turn_gate", None)
        pending_dialogue_turn = (
            dialogue_gate.next_pending() if dialogue_gate is not None else None
        )
        requestable = set(self._spec.list_requestable_instrument_ids())
        context = {
            "proc": self._spec.procedure_id,
            "procedure_prompt_id": str(self._procedure_prompt.get("id", "")) if isinstance(self._procedure_prompt, dict) else "",
            "phase_search_mode": (
                "open_set" if open_set_phase_search else "temporal_prior"
            ),
            "phases": [
                {"id": phase.id, "tools": list(phase.expected_instruments), "next": list(phase.possible_next)}
                for phase in self._spec.bundle.phases
            ],
            "tools": [
                {
                    "id": instrument.id,
                    "role": instrument.role,
                    "requestable": instrument.id in requestable,
                }
                for instrument in self._spec.bundle.instruments
            ],
            "evidence_window": {
                "speech": list(evidence.get("speech", [])),
                "skill_status": list(evidence.get("skill_status", [])),
            },
            "pending_dialogue_turn": self._dialogue_turn_context(
                pending_dialogue_turn
            ),
            "visual_input": dict(self._current_visual_input),
            "observable_perception": self._public_perception_context(),
            "digital_twin": digital_twin,
            "forecast_constraints": build_forecast_constraints(digital_twin),
            "bed_robot_arm_groups": self._bed_robot_arm_group_state_rows(),
            "pending_bed_robot_arm_group_request": (
                self._pending_bed_robot_arm_group_request_context()
            ),
            "candidates": candidates,
            "previous": previous,
        }
        if frozen_ngram_prior is not None:
            context["frozen_ngram_prior"] = frozen_ngram_prior
        phase_start_floor = explicit_phase_start_floor_context(
            getattr(self, "_phase_bootstrap_id", ""),
            explicit_start_phase=bool(
                getattr(self, "_phase_bootstrap_explicit", False)
            ),
            normal_phase_ids=list(self._spec.normal_phase_ids),
            interrupt_phase_ids=list(self._spec.interrupt_phase_ids),
        )
        if phase_start_floor is not None:
            context["phase_start_floor"] = phase_start_floor
        return bound_actor_log_context(context)

    def _assemble_actor_log_context(self) -> str:
        return compact_json(self._assemble_actor_log_context_dict())

    def _primary_payload_phase(self, payload: dict[str, Any]) -> str:
        version = str(payload.get("v", ""))
        rows = payload.get("phase", []) if version in {"2", "3", "4", "5", "6"} else payload.get("ph", [])
        raw_phase = ""
        if version == "2" and isinstance(rows, list) and len(rows) == 2:
            raw_phase = rows[0]
        elif isinstance(rows, list) and rows and isinstance(rows[0], list):
            raw_phase = rows[0][0] if rows[0] else ""
        return self._canonical_phase_id(raw_phase)

    def _fresh_image(self, label: str, now: float) -> ImageSample | None:
        sample = self._latest_images.get(label)
        if sample is None:
            return None
        if now - sample.received_monotonic > self._image_stale_sec:
            return None
        if self._source_frame_timestamp_rejection(sample, now_sec=now):
            return None
        return sample

    def _fresh_images(self, label: str, now: float) -> list[ImageSample]:
        buffer = getattr(self, "_image_buffers", {}).get(label)
        samples = list(buffer) if buffer else []
        latest = self._latest_images.get(label)
        if latest is not None and (
            not samples or samples[-1] is not latest
        ):
            samples.append(latest)
        return [
            sample
            for sample in samples
            if (
                now - sample.received_monotonic <= self._image_stale_sec
                and not self._source_frame_timestamp_rejection(
                    sample,
                    now_sec=now,
                )
            )
        ]

    def _fresh_multiview_pair(
        self,
        now: float,
    ) -> tuple[ImageSample | None, ImageSample | None, float | None]:
        fields = self._fresh_images("field", now)
        cam4_samples = self._fresh_images("cam4", now)
        if not fields or not cam4_samples:
            return (
                fields[-1] if fields else None,
                cam4_samples[-1] if cam4_samples else None,
                None,
            )

        pairs = [
            (
                field,
                cam4,
                abs(
                    image_sample_stamp_sec(field)
                    - image_sample_stamp_sec(cam4)
                ),
            )
            for field in fields
            for cam4 in cam4_samples
        ]
        aligned = [
            pair
            for pair in pairs
            if pair[2] <= self._multiview_max_skew_sec + 1.0e-9
        ]
        if aligned:
            return max(
                aligned,
                key=lambda pair: (
                    min(
                        image_sample_stamp_sec(pair[0]),
                        image_sample_stamp_sec(pair[1]),
                    ),
                    -pair[2],
                ),
            )
        return min(pairs, key=lambda pair: pair[2])

    def _current_cam4_crop(
        self,
        cam4: ImageSample,
    ) -> tuple[float, float, float, float]:
        if not self._cam4_dynamic_crop:
            return self._cam4_crop_xywh_norm
        perception = self._public_perception_context(
            image_sample_stamp_sec(cam4)
        )
        summaries = [
            perception.get(kind, {})
            for kind in ("bboxes", "segmentation")
            if isinstance(perception.get(kind), dict)
        ]
        return dynamic_cam4_crop_xywh(
            summaries,
            fallback_xywh_norm=self._cam4_crop_xywh_norm,
            padding_norm=self._cam4_crop_padding_norm,
            minimum_width_norm=self._cam4_crop_min_width_norm,
            minimum_height_norm=self._cam4_crop_min_height_norm,
        )

    def _select_images(
        self,
    ) -> tuple[list[tuple[str, bytes, str]], str, ModelImage | None]:
        now = self._causal_now_sec()
        perception_enabled = bool(
            getattr(self, "_perception_enabled", True)
        )
        require_rfdetr_field = bool(
            getattr(self, "_require_rfdetr_applied_field_image", False)
        )
        segmented_candidate = (
            self._fresh_image("field", now)
            if perception_enabled
            else None
        )
        field_provenance_error = ""
        segmented_field = segmented_candidate
        if require_rfdetr_field:
            if not perception_enabled:
                segmented_field = None
                field_provenance_error = (
                    "local RF-DETR applied FLIR image is required but "
                    "perception is disabled"
                )
            elif segmented_candidate is None:
                segmented_field = None
                field_provenance_error = (
                    "missing fresh local RF-DETR applied FLIR image"
                )
            elif not frame_id_has_rfdetr_marker(
                segmented_candidate.frame_id,
                RFDETR_FLIR_SEGMENTED_FRAME_MARKER,
            ):
                segmented_field = None
                field_provenance_error = (
                    "field image lacks the local RF-DETR segmentation "
                    "provenance marker"
                )

        raw_field = (
            None
            if require_rfdetr_field
            else self._fresh_image("raw_field", now)
        )
        field = segmented_field or raw_field
        if (
            field is None
            and not require_rfdetr_field
            and not self._raw_field_image_topic
        ):
            # Simulation/no-image configurations may intentionally publish
            # their raw visual stream on the legacy field topic.
            field = self._fresh_image("field", now)
        self._current_image_input_error = ""
        if field is None:
            self._current_image_input_error = field_provenance_error or (
                "missing fresh FLIR image (segmented and raw fallback unavailable)"
            )
            self._current_perception_reference_stamp_sec = None
            self._current_visual_input = {
                "image_source": "missing(flir_visual)",
                "model_ready_topic": self._composite_image_topic,
                "sources": [],
                "preprocessing": "unavailable",
                "cam4_image_forwarded_to_vlm": False,
                "cam4_detector_overlay_forwarded_to_vlm": False,
                "detector_advisory": perception_enabled,
                "rfdetr_applied_field_required": require_rfdetr_field,
                "rfdetr_cam4_overlay_required": False,
                "structured_rfdetr_tool_observations": True,
                "input_error": self._current_image_input_error,
            }
            return [], "missing(flir_visual)", None

        cam4: ImageSample | None = None
        cam4_skew_sec: float | None = None
        cam4_samples = self._fresh_images("cam4", now)
        if cam4_samples:
            nearest_cam4 = min(
                cam4_samples,
                key=lambda sample: abs(
                    image_sample_stamp_sec(sample)
                    - image_sample_stamp_sec(field)
                ),
            )
            cam4_skew_sec = abs(
                image_sample_stamp_sec(nearest_cam4)
                - image_sample_stamp_sec(field)
            )
            if cam4_skew_sec <= self._multiview_max_skew_sec + 1.0e-9:
                cam4 = nearest_cam4

        cam4_fallback_reason = ""

        image_max_side_px = self._image_max_side_px
        if cam4 is not None:
            multiview_limit = getattr(
                self,
                "_multiview_image_max_side_px",
                DEFAULT_MULTIVIEW_IMAGE_MAX_SIDE_PX,
            )
            image_max_side_px = (
                min(image_max_side_px, multiview_limit)
                if image_max_side_px > 0
                else multiview_limit
            )

        using_segmented_field = field is segmented_field
        self._current_perception_reference_stamp_sec = image_sample_stamp_sec(
            field
        )
        sources = [
            {
                "role": (
                    "flir_segmented"
                    if using_segmented_field
                    else "flir_raw"
                ),
                "topic": (
                    self._field_image_topic
                    if using_segmented_field
                    else (
                        self._raw_field_image_topic
                        or self._field_image_topic
                    )
                ),
                "stamp_sec": round(
                    image_sample_stamp_sec(field),
                    9,
                ),
                "frame_id": field.frame_id,
            }
        ]
        cam4_forwarded = False
        cam4_overlay_forwarded = False
        image_layout = "flir_only"
        model_image: ModelImage | None = None
        image_source = ""
        if cam4 is not None:
            try:
                composite_bytes, composite_mime = compose_flir_cam4_for_model(
                    field.data,
                    field.mime_type,
                    cam4.data,
                    cam4.mime_type,
                    cam4_crop_xywh_norm=self._current_cam4_crop(cam4),
                    max_side_px=image_max_side_px,
                )
            except (OSError, ValueError) as exc:
                cam4_fallback_reason = (
                    "CAM4 composite unavailable; using FLIR-only fallback "
                    f"({type(exc).__name__})"
                )
            else:
                model_image = ModelImage(
                    label="Synchronized FLIR surgical field + CAM4 Mayo instruments",
                    data=composite_bytes,
                    mime_type=composite_mime,
                    stamp_sec=field.stamp_sec,
                    stamp_nanosec=field.stamp_nanosec,
                    frame_id=field.frame_id or cam4.frame_id,
                )
                image_source = (
                    "flir_cam4_rfdetr_segmented"
                    if using_segmented_field
                    else "flir_cam4_raw_fallback"
                )
                sources.append(
                    {
                        "role": "cam4_mayo_instrument_crop",
                        "topic": self._cam4_image_topic,
                        "stamp_sec": round(
                            image_sample_stamp_sec(cam4),
                            9,
                        ),
                        "frame_id": cam4.frame_id,
                        "offset_sec": round(
                            image_sample_stamp_sec(cam4)
                            - image_sample_stamp_sec(field),
                            9,
                        ),
                    }
                )
                cam4_forwarded = True
                image_layout = "flir_left_cam4_right"
        elif cam4_skew_sec is not None and not cam4_fallback_reason:
            cam4_fallback_reason = (
                "CAM4 frame is outside the multiview synchronization window"
            )
        elif not cam4_fallback_reason:
            cam4_fallback_reason = "CAM4 frame is unavailable"

        if model_image is None:
            field_bytes, field_mime = bound_image_for_model(
                field.data,
                field.mime_type,
                image_max_side_px,
            )
            model_image = ModelImage(
                label=(
                    "RFDETR-segmented FLIR surgical field"
                    if using_segmented_field
                    else "Raw FLIR surgical field (detector-independent fallback)"
                ),
                data=field_bytes,
                mime_type=field_mime,
                stamp_sec=field.stamp_sec,
                stamp_nanosec=field.stamp_nanosec,
                frame_id=field.frame_id,
            )
            image_source = (
                "flir_rfdetr_segmented"
                if using_segmented_field
                else "flir_raw_fallback"
            )
        images: list[tuple[str, bytes, str]] = [
            (
                model_image.label,
                model_image.data,
                model_image.mime_type,
            )
        ]
        if self._require_cam4_image and not cam4_forwarded:
            self._current_image_input_error = (
                "missing synchronized raw CAM4 Mayo image"
            )
        if cam4_forwarded:
            flir_preprocessing = (
                "RFDETRSegSmall FLIR"
                if using_segmented_field
                else "raw FLIR fallback"
            )
            cam4_preprocessing = "raw CAM4 Mayo instrument pixels"
            preprocessing = (
                "single side-by-side composite: "
                f"{flir_preprocessing} + {cam4_preprocessing}"
            )
        else:
            preprocessing = (
                "RFDETRSegSmall FLIR-only fallback"
                if using_segmented_field
                else "raw FLIR-only fallback"
            )
        self._current_visual_input = {
            "image_source": image_source,
            "model_ready_topic": self._composite_image_topic,
            "perception_image_max_skew_sec": (
                self._perception_image_max_skew_sec
            ),
            "sources": sources,
            "preprocessing": preprocessing,
            "image_layout": image_layout,
            "cam4_image_forwarded_to_vlm": cam4_forwarded,
            "cam4_alignment_skew_sec": (
                round(cam4_skew_sec, 9)
                if cam4_skew_sec is not None
                else None
            ),
            "cam4_detector_overlay_forwarded_to_vlm": (
                cam4_overlay_forwarded
            ),
            "detector_advisory": perception_enabled,
            "rfdetr_applied_field_required": require_rfdetr_field,
            "rfdetr_cam4_overlay_required": False,
            "structured_rfdetr_tool_observations": True,
            "cam4_fallback_reason": cam4_fallback_reason,
            "cam4_overlay_fallback_reason": (
                "not used for VLM; typed CAM3/CAM4 RF-DETR observations "
                "supply detector evidence"
            ),
            "input_error": self._current_image_input_error,
        }
        return images, image_source, model_image

    def _publish_model_ready_image(self, model_image: ModelImage | None) -> None:
        if model_image is None:
            return
        msg = CompressedImage()
        msg.header.stamp.sec = int(model_image.stamp_sec)
        msg.header.stamp.nanosec = int(model_image.stamp_nanosec)
        msg.header.frame_id = (
            f"{model_image.frame_id}|vlm_model_ready"
            if model_image.frame_id
            else "vlm_model_ready"
        )
        msg.format = (
            "jpeg"
            if model_image.mime_type == "image/jpeg"
            else "png"
        )
        msg.data = model_image.data
        self._composite_image_pub.publish(msg)

    def _oracle_payload(self, context_dict: dict[str, Any]) -> dict[str, Any]:
        assert self._world is not None
        stage = self._oracle_stage_for_tick(self._oracle_tick)
        if stage is None:
            phases = [
                [
                    self._world.filtered_phase,
                    round(float(self._world.phase_confidence or 0.9), 3),
                ]
            ]
            observations = []
            for instrument in self._world.instrument_states:
                include = (
                    instrument.instrument_id in context_dict["exp"]
                    or instrument.instrument_id in context_dict["pending"]
                    or instrument.instrument_id in {
                        self._world.right_hand_tool,
                        self._world.left_hand_tool,
                        self._world.prepositioned_tool,
                        self._world.surgeon_request_tool,
                        self._world.explicit_request_tool,
                    }
                    or instrument.location_id != instrument.home_location_id
                    or instrument.location_type != instrument.home_location_type
                )
                if not include:
                    continue
                observations.append(
                    [
                        instrument.instrument_id,
                        instrument.location_id,
                        instrument.location_type,
                        0.97,
                    ]
                )
            uncertainty = 0.34 if self._world.phase_uncertain else 0.08
            summary = (
                f"phase={self._world.filtered_phase}; "
                f"request={self._world.surgeon_request_tool or self._world.explicit_request_tool or 'none'}; "
                f"decision={self._latest_bt.decision if self._latest_bt else 'none'}"
            )
            return {
                "v": "1",
                "ph": phases,
                "to": observations,
                "u": uncertainty,
                "sum": summary,
            }

        phases = [
            [hypothesis.phase_id, round(float(hypothesis.confidence), 3)]
            for hypothesis in stage.phase_hypotheses
        ] or [[self._world.filtered_phase, round(float(self._world.phase_confidence or 0.9), 3)]]
        observations = [
            [
                observation.instrument_id,
                observation.location_id,
                observation.location_type,
                round(float(observation.confidence), 3),
            ]
            for observation in stage.observations
            if observation.visible
        ]
        uncertainty = float(stage.uncertainty)
        current_request = self._world.surgeon_request_tool or self._world.explicit_request_tool or "none"
        summary = (
            f"oracle_stage={stage.name}; "
            f"phase={phases[0][0] if phases else self._world.filtered_phase}; "
            f"request={current_request}; "
            f"decision={self._latest_bt.decision if self._latest_bt else 'none'}"
        )
        return {
            "v": "1",
            "ph": phases,
            "to": observations,
            "u": uncertainty,
            "sum": summary,
        }

    def _actor_log_fallback_payload(self, context_dict: dict[str, Any]) -> dict[str, Any]:
        evidence_window = context_dict.get("evidence_window", {}) if isinstance(context_dict, dict) else {}
        digital_twin = context_dict.get("digital_twin", {}) if isinstance(context_dict, dict) else {}
        digital_twin_tools = (
            digital_twin.get("tools", [])
            if isinstance(digital_twin, dict)
            else []
        )
        mayo = [
            str(row.get("id", ""))
            for row in digital_twin_tools
            if isinstance(row, dict)
            and str(row.get("lc", "")) in {"mayo_reuse", "mayo_recovery"}
            and str(row.get("lt", "")) == CANONICAL_MAYO_POLICY_LOCATION
            and str(row.get("loc", "")) == CANONICAL_MAYO_POLICY_LOCATION
            and str(row.get("id", ""))
        ]
        speech = evidence_window.get("speech", []) if isinstance(evidence_window, dict) else []
        speech_rows = speech if isinstance(speech, list) else []
        requested_tool = self._tool_from_public_speech([row for row in speech_rows if isinstance(row, dict)])
        intent_type = "handover" if requested_tool else "none"
        candidates = context_dict.get("candidates", {}) if isinstance(context_dict, dict) else {}
        phase_rows = list(candidates.get("phase", [])) if isinstance(candidates, dict) else []
        tool_rows = list(candidates.get("tool", [])) if isinstance(candidates, dict) else []
        if not phase_rows:
            phase_rows = [[self._spec.default_phase_id, 0.35]]
        if requested_tool and not any(row[0] == requested_tool for row in tool_rows if isinstance(row, list) and row):
            tool_rows = [[requested_tool, 0.72], *tool_rows[:3]]
        if not tool_rows:
            tool_rows = [["", 0.0]]
        immediate_reuse_tool = ""
        for row in tool_rows:
            if isinstance(row, list) and row and str(row[0]):
                immediate_reuse_tool = str(row[0])
                break
        mayo_entries: list[list[Any]] = []
        recover_candidates: list[tuple[str, float]] = []
        for tool_id in (mayo if isinstance(mayo, list) else []):
            tool_id = str(tool_id)
            if not tool_id:
                continue
            if tool_id == requested_tool or tool_id == immediate_reuse_tool:
                mayo_entries.append([tool_id, "reuse", 0.62])
            else:
                confidence = 0.62 if requested_tool else 0.56
                mayo_entries.append([tool_id, "recover", confidence])
                recover_candidates.append((tool_id, confidence))
        mayo_retrieve = ["", 0.0]
        if recover_candidates:
            mayo_retrieve = [recover_candidates[0][0], recover_candidates[0][1]]
        return {
            "v": "6",
            "phase": phase_rows[:4],
            "tool": tool_rows[:4],
            "intent": [intent_type, requested_tool if intent_type != "none" else "", 0.7 if intent_type != "none" else 0.0],
            "mayo": mayo_entries,
            "mayo_retrieve": mayo_retrieve,
            "u": 0.55,
            "sum": "actor-log fallback",
            "bed_robot_arm_group": self._fallback_bed_robot_arm_group_proposal(),
            "function_call": None,
            "humanoid_reply": None,
        }

    def _fallback_bed_robot_arm_group_proposal(self) -> dict[str, Any] | None:
        """Build a safe oracle fallback only when speech states direction.

        A missing spoken direction cannot be recovered from the fallback path,
        because that path has no trustworthy visual reasoning.  Returning null
        turns into an explicit invalid proposal and therefore no group action.
        """
        request = self._latest_bed_robot_arm_group_request
        if request is None:
            return None
        spoken_direction = infer_retraction_direction(request.voice_text)
        thyroid_default_right = bool(
            not spoken_direction
            and str(request.procedure_id) == "thyroidectomy_demo"
            and str(request.adjustment_mode) == "single"
            and str(request.target_retractor_id) == "right_malleable"
        )
        if not spoken_direction and not thyroid_default_right:
            return None
        try:
            normalized = normalize_retraction_request(
                request.voice_text,
                vlm_direction="RIGHT" if thyroid_default_right else "",
            )
        except BedRobotArmGroupNormalizationError:
            return None
        return {
            "request_id": str(request.request_id),
            "group_id": "retraction",
            "operation": "retraction",
            "adjustment_mode": str(request.adjustment_mode),
            "target_retractor_id": str(request.target_retractor_id),
            "direction_frame": str(request.direction_frame),
            "direction": (
                "none"
                if str(request.adjustment_mode) == "multi"
                else normalized.direction.lower()
            ),
            "axis": (
                normalized.direction.lower()
                if str(request.adjustment_mode) == "multi"
                else "none"
            ),
            "distance_mm": float(normalized.distance_mm),
            "distance_origin": normalized.distance_origin,
            "raw_distance_text": normalized.raw_distance_text,
            "end_effector_profile": str(request.end_effector_profile),
            "rationale": "direction and distance normalized from explicit speech",
            "confidence": 0.76,
        }

    def _fallback_payload(self, context_dict: dict[str, Any]) -> dict[str, Any]:
        if self._context_mode == "actor_log":
            return self._actor_log_fallback_payload(context_dict)
        return self._oracle_payload(context_dict)

    def _candidate_rows(self, context_dict: dict[str, Any], key: str) -> list[list[Any]]:
        candidates = context_dict.get("candidates", {}) if isinstance(context_dict, dict) else {}
        rows = candidates.get(key, []) if isinstance(candidates, dict) else []
        cleaned: list[list[Any]] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, list) or len(row) < 2:
                continue
            item_id = str(row[0])
            if not item_id:
                continue
            try:
                confidence = float(row[1])
            except (TypeError, ValueError):
                continue
            cleaned.append([item_id, max(0.0, min(1.0, confidence))])
        return cleaned

    def _merge_ranked_rows(
        self,
        primary: list[list[Any]],
        fallback: list[list[Any]],
        *,
        limit: int,
    ) -> list[list[Any]]:
        def confidence(row: list[Any]) -> float:
            try:
                return float(row[1])
            except (IndexError, TypeError, ValueError):
                return -1.0

        merged: list[list[Any]] = []
        seen: set[str] = set()
        # Model rows remain authoritative over procedure candidates, but an
        # OpenAI-compatible backend may return internally inconsistent array
        # order. Normalize each ranked set by its own confidence first.
        ranked_primary = sorted(primary, key=confidence, reverse=True)
        ranked_fallback = sorted(fallback, key=confidence, reverse=True)
        for row in [*ranked_primary, *ranked_fallback]:
            if not isinstance(row, list) or len(row) < 2:
                continue
            item_id = str(row[0])
            if not item_id or item_id in seen:
                continue
            try:
                confidence = float(row[1])
            except (TypeError, ValueError):
                continue
            seen.add(item_id)
            merged.append([item_id, round(max(0.0, min(1.0, confidence)), 3)])
            if len(merged) >= limit:
                break
        return merged

    def _normal_phase_rows(self, rows: list[list[Any]], *, fallback_phase: str = "") -> list[list[Any]]:
        normal_rows = [
            row
            for row in rows
            if isinstance(row, list)
            and row
            and not self._spec.is_interrupt_phase(str(row[0]))
        ]
        if normal_rows:
            return normal_rows
        if fallback_phase and not self._spec.is_interrupt_phase(fallback_phase):
            return [[fallback_phase, 0.35]]
        return []

    def _stabilize_actor_log_payload(self, payload: dict[str, Any], context_dict: dict[str, Any]) -> dict[str, Any]:
        if str(payload.get("v", "")) not in {"3", "4", "5", "6"}:
            return payload
        stabilized = dict(payload)
        evidence_window = context_dict.get("evidence_window", {}) if isinstance(context_dict, dict) else {}
        speech = evidence_window.get("speech", []) if isinstance(evidence_window, dict) else []
        speech_rows = speech if isinstance(speech, list) else []
        requested_tool = self._tool_from_public_speech([row for row in speech_rows if isinstance(row, dict)])

        model_phase_rows = [
            [str(row[0]), float(row[1])]
            for row in stabilized.get("phase", [])
            if isinstance(row, list) and len(row) >= 2 and str(row[0])
        ]
        # Preserve the model's observation ranking. Temporal legality,
        # persistence, and procedure priors are reducer responsibilities.
        stabilized["phase"] = self._normal_phase_rows(
            self._merge_ranked_rows(model_phase_rows, [], limit=4)
        )[:4]

        model_tool_rows = [
            [str(row[0]), float(row[1])]
            for row in stabilized.get("tool", [])
            if isinstance(row, list) and len(row) >= 2 and str(row[0])
        ]
        # An explicit request is handled independently by the speech/intent
        # path. It is not a next-tool forecast and must not overwrite one.
        # On live actor-log requests, forecast_constraints is the authoritative
        # public inventory boundary.  Remove unavailable/non-requestable model
        # guesses, then fill missing valid candidates from the frozen 0704
        # n-gram and authored procedure prior.  A tiny baseline is used only to
        # make every otherwise-unranked *eligible* tool representable; it can
        # never introduce a tool outside the current handover inventory.
        forecast_constraints = (
            context_dict.get("forecast_constraints")
            if isinstance(context_dict, dict)
            else None
        )
        if isinstance(forecast_constraints, dict):
            requestable_ids = {
                self._canonical_tool_id(row.get("id", ""))
                for row in context_dict.get("tools", [])
                if isinstance(row, dict)
                and bool(row.get("requestable", False))
                and self._canonical_tool_id(row.get("id", ""))
            }
            eligible_ids: list[str] = []
            for row in forecast_constraints.get(
                "available_for_next_handover", []
            ):
                if not isinstance(row, list) or len(row) < 2:
                    continue
                tool_id = self._canonical_tool_id(row[0])
                try:
                    available_count = int(row[1])
                except (TypeError, ValueError):
                    continue
                if (
                    available_count > 0
                    and tool_id in requestable_ids
                    and tool_id not in eligible_ids
                ):
                    eligible_ids.append(tool_id)
            eligible = set(eligible_ids)
            primary_rows = [
                [tool_id, confidence]
                for raw_tool_id, confidence in model_tool_rows
                if (tool_id := self._canonical_tool_id(raw_tool_id)) in eligible
            ]
            primary_floor = min(
                (float(row[1]) for row in primary_rows),
                default=2.0,
            )
            fallback_scale = min(1.0, max(0.001, primary_floor * 0.5))

            def fallback_row(row: list[Any]) -> list[Any]:
                try:
                    score = float(row[1])
                except (TypeError, ValueError):
                    score = 0.0
                return [
                    self._canonical_tool_id(row[0]),
                    max(0.001, min(1.0, score) * fallback_scale),
                ]

            fallback_rows: list[list[Any]] = []
            frozen_prior = context_dict.get("frozen_ngram_prior", {})
            if isinstance(frozen_prior, dict):
                fallback_rows.extend(
                    fallback_row(row)
                    for row in frozen_prior.get("candidates", [])
                    if isinstance(row, list)
                    and len(row) >= 2
                    and self._canonical_tool_id(row[0]) in eligible
                )
            procedure_candidates = context_dict.get("candidates", {})
            if isinstance(procedure_candidates, dict):
                fallback_rows.extend(
                    fallback_row(row)
                    for row in procedure_candidates.get("tool", [])
                    if isinstance(row, list)
                    and len(row) >= 2
                    and self._canonical_tool_id(row[0]) in eligible
                )
            fallback_rows.extend(
                [tool_id, 0.001] for tool_id in eligible_ids
            )
            stabilized["tool"] = normalize_ranked_tool_distribution(
                self._merge_ranked_rows(
                    primary_rows,
                    fallback_rows,
                    limit=3,
                ),
                limit=3,
            )
            if len(eligible_ids) < 3:
                try:
                    current_uncertainty = float(stabilized.get("u", 1.0))
                except (TypeError, ValueError):
                    current_uncertainty = 1.0
                stabilized["u"] = round(
                    max(0.8, min(1.0, current_uncertainty)),
                    3,
                )
        else:
            # Replay/unit contexts created before forecast_constraints keep
            # their evidence values, but the public ranking is bounded to the
            # new maximum of three rows.
            stabilized["tool"] = self._merge_ranked_rows(
                model_tool_rows,
                [],
                limit=3,
            )

        intent = stabilized.get("intent", ["none", "", 0.0])
        if not isinstance(intent, list) or len(intent) < 3:
            intent = ["none", "", 0.0]
        if requested_tool:
            intent = ["handover", requested_tool, max(0.72, min(1.0, float(intent[2]) if len(intent) > 2 else 0.0))]
        else:
            intent = ["none", "", 0.0]
        stabilized["intent"] = [str(intent[0]), str(intent[1]), round(max(0.0, min(1.0, float(intent[2]))), 3)]
        self._corroborate_mayo_with_cam4_semantics(
            stabilized,
            context_dict,
        )
        self._retain_mayo_policy_for_current_stand(stabilized, context_dict)
        self._suppress_non_mayo_recovery_candidates(stabilized, context_dict)

        stabilized["sum"] = normalize_clinical_analysis(
            stabilized.get("sum", "")
        )
        return stabilized

    def _current_mayo_policy_tool_ids(
        self,
        context_dict: dict[str, Any],
    ) -> set[str]:
        """Return only DT instances physically confirmed on canonical Mayo.

        A CAM4/RF-DETR observation is allowed to establish a later DT location,
        but the same VLM response must not both move a tool to Mayo and make a
        reuse/recovery policy decision for it.  This keeps policy evidence
        dependent on the pre-inference public DT fact.
        """

        digital_twin = (
            context_dict.get("digital_twin", {})
            if isinstance(context_dict, dict)
            else {}
        )
        tools = digital_twin.get("tools", []) if isinstance(digital_twin, dict) else []
        if not isinstance(tools, list):
            return set()
        current_tool_ids: set[str] = set()
        for row in tools:
            if not isinstance(row, dict):
                continue
            tool_id = self._canonical_tool_id(row.get("id", ""))
            if (
                tool_id
                and str(row.get("lc", "")) in {"mayo_reuse", "mayo_recovery"}
                and str(row.get("lt", "")) == CANONICAL_MAYO_POLICY_LOCATION
                and str(row.get("loc", "")) == CANONICAL_MAYO_POLICY_LOCATION
            ):
                current_tool_ids.add(tool_id)
        return current_tool_ids

    def _retain_mayo_policy_for_current_stand(
        self,
        payload: dict[str, Any],
        context_dict: dict[str, Any],
    ) -> None:
        """Fail closed unless the exact tool was already on the Mayo stand."""

        current_mayo_tool_ids = self._current_mayo_policy_tool_ids(context_dict)
        retained_rows: list[list[Any]] = []
        for row in payload.get("mayo", []):
            if not isinstance(row, list) or len(row) < 3:
                continue
            tool_id = self._canonical_tool_id(row[0])
            disposition = str(row[1]).strip().lower()
            try:
                confidence = float(row[2])
            except (TypeError, ValueError):
                continue
            if (
                not tool_id
                or tool_id not in current_mayo_tool_ids
                or disposition not in {"recover", "reuse"}
                or not math.isfinite(confidence)
            ):
                continue
            retained_rows.append(
                [
                    tool_id,
                    disposition,
                    round(max(0.0, min(1.0, confidence)), 4),
                ]
            )
        payload["mayo"] = retained_rows

        recovery_tools = {
            str(row[0])
            for row in retained_rows
            if str(row[1]) == "recover"
        }
        retrieve = payload.get("mayo_retrieve", ["", 0.0])
        retrieve_tool = (
            self._canonical_tool_id(retrieve[0])
            if isinstance(retrieve, list) and len(retrieve) >= 2
            else ""
        )
        if retrieve_tool not in recovery_tools:
            payload["mayo_retrieve"] = ["", 0.0]
        else:
            payload["mayo_retrieve"] = [retrieve_tool, retrieve[1]]

    def _corroborate_mayo_with_cam4_semantics(
        self,
        payload: dict[str, Any],
        context_dict: dict[str, Any],
    ) -> None:
        """Use CAM4 pixels as evidence and detector rows as optional support."""

        perception = (
            context_dict.get("observable_perception", {})
            if isinstance(context_dict, dict)
            else {}
        )
        if not isinstance(perception, dict):
            perception = {}
        alignment = perception.get("alignment", {})
        aligned = (
            perception.get("schema") == "taskplanner.cam4_semantics.v1"
            and perception.get("source") == "cam4_rfdetr_small"
            and isinstance(alignment, dict)
            and alignment.get("status") == "aligned"
        )
        visual_input = (
            context_dict.get("visual_input", {})
            if isinstance(context_dict, dict)
            else {}
        )
        direct_cam4 = bool(
            isinstance(visual_input, dict)
            and visual_input.get("cam4_image_forwarded_to_vlm")
        )
        if not aligned and direct_cam4:
            raw_rows: list[list[Any]] = []
            for row in payload.get("mayo", []):
                if not isinstance(row, list) or len(row) < 3:
                    continue
                tool_id = self._canonical_tool_id(row[0])
                if not tool_id or str(row[1]) not in {"recover", "reuse"}:
                    continue
                try:
                    confidence = float(row[2])
                except (TypeError, ValueError):
                    continue
                raw_rows.append(
                    [
                        tool_id,
                        str(row[1]),
                        round(max(0.0, min(1.0, confidence)), 4),
                    ]
                )
            payload["mayo"] = raw_rows
            raw_recovery_tools = {
                str(row[0])
                for row in raw_rows
                if str(row[1]) == "recover"
            }
            retrieve = payload.get("mayo_retrieve", ["", 0.0])
            retrieve_tool = (
                self._canonical_tool_id(retrieve[0])
                if isinstance(retrieve, list) and len(retrieve) >= 2
                else ""
            )
            if retrieve_tool not in raw_recovery_tools:
                payload["mayo_retrieve"] = ["", 0.0]
            else:
                payload["mayo_retrieve"] = [
                    retrieve_tool,
                    retrieve[1],
                ]
            return
        if not aligned:
            payload["mayo"] = []
            payload["mayo_retrieve"] = ["", 0.0]
            return

        detected: dict[str, dict[str, Any]] = {}
        rows = perception.get("tools", []) if aligned else []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            tool_id = self._canonical_tool_id(row.get("name", ""))
            try:
                count = int(row.get("count", 0))
            except (TypeError, ValueError):
                count = 0
            if tool_id and count > 0:
                try:
                    confidence = float(row.get("max_confidence", 0.0))
                    stable_samples = int(
                        row.get("stable_sample_count", 0)
                    )
                    stable_duration_sec = float(
                        row.get("stable_duration_sec", 0.0)
                    )
                except (TypeError, ValueError):
                    confidence = 0.0
                    stable_samples = 0
                    stable_duration_sec = 0.0
                previous = detected.get(tool_id, {})
                detected[tool_id] = {
                    "count": min(
                        16,
                        int(previous.get("count", 0))
                        + min(count, 16),
                    ),
                    "confidence": max(
                        float(previous.get("confidence", 0.0)),
                        min(1.0, max(0.0, confidence)),
                    ),
                    "stable": bool(
                        stable_samples >= CAM4_MAYO_MIN_STABLE_SAMPLES
                        and stable_duration_sec
                        >= CAM4_MAYO_MIN_STABLE_DURATION_SEC
                    ),
                }

        digital_twin = (
            context_dict.get("digital_twin", {})
            if isinstance(context_dict, dict)
            else {}
        )
        field_tools = {
            str(row.get("id", ""))
            for row in (
                digital_twin.get("tools", [])
                if isinstance(digital_twin, dict)
                else []
            )
            if isinstance(row, dict)
            and (
                str(row.get("lt", ""))
                in {"surgical_field", "bed_fixed_tool"}
                or str(row.get("lc", "")) == "surgeon_owned"
            )
        }

        remaining = {
            tool_id: int(evidence["count"])
            for tool_id, evidence in detected.items()
        }
        corroborated_rows: list[list[Any]] = []
        for row in payload.get("mayo", []):
            if not isinstance(row, list) or len(row) < 3:
                continue
            tool_id = self._canonical_tool_id(row[0])
            if not tool_id or remaining.get(tool_id, 0) <= 0:
                continue
            evidence = detected[tool_id]
            if (
                tool_id in field_tools
                and (
                    not bool(evidence["stable"])
                    or float(evidence["confidence"])
                    < CAM4_MAYO_MIN_CONFIDENCE
                )
            ):
                continue
            corroborated_rows.append(
                [
                    tool_id,
                    row[1],
                    min(float(row[2]), float(evidence["confidence"])),
                ]
            )
            remaining[tool_id] -= 1

        # CAM4 is a fixed Mayo view. Stable detector presence is sufficient to
        # update location, but not to order recovery. Missing model rows are
        # therefore inserted as fail-closed reuse observations.
        for tool_id, count in remaining.items():
            evidence = detected[tool_id]
            if (
                count <= 0
                or not bool(evidence["stable"])
                or float(evidence["confidence"])
                < CAM4_MAYO_MIN_CONFIDENCE
            ):
                continue
            corroborated_rows.append(
                [
                    tool_id,
                    "reuse",
                    round(float(evidence["confidence"]), 4),
                ]
            )
        payload["mayo"] = corroborated_rows

        corroborated_recovery_tools = {
            str(row[0])
            for row in corroborated_rows
            if len(row) >= 2 and str(row[1]) == "recover"
        }
        retrieve = payload.get("mayo_retrieve", ["", 0.0])
        retrieve_tool = (
            self._canonical_tool_id(retrieve[0])
            if isinstance(retrieve, list) and len(retrieve) >= 2
            else ""
        )
        if retrieve_tool not in corroborated_recovery_tools:
            payload["mayo_retrieve"] = ["", 0.0]
        else:
            payload["mayo_retrieve"] = [
                retrieve_tool,
                retrieve[1],
            ]

    def _suppress_non_mayo_recovery_candidates(
        self,
        payload: dict[str, Any],
        context_dict: dict[str, Any],
    ) -> None:
        # The DT fact is the hard admission boundary.  Keep this guard here in
        # addition to the stabilizer call so direct callers cannot bypass it.
        self._retain_mayo_policy_for_current_stand(payload, context_dict)
        candidates = (
            context_dict.get("candidates", {})
            if isinstance(context_dict, dict)
            else {}
        )
        candidate_evidence = (
            candidates.get("evidence", {})
            if isinstance(candidates, dict)
            else {}
        )
        current_phase = self._canonical_phase_id(
            candidate_evidence.get("current_phase", "")
            if isinstance(candidate_evidence, dict)
            else ""
        )
        if not current_phase:
            current_phase = self._authoritative_runtime_phase()
        remaining_procedure_tools = set(
            self._spec.get_remaining_expected_instruments(
                current_phase,
                include_current=True,
            )
        )

        # Recovery is irreversible from the planner's perspective. A visual
        # model may correctly see a tool on Mayo while guessing the wrong
        # lifecycle. Keep the location observation, but fail closed to reuse
        # whenever the authored procedure still references that tool.
        protected_mayo_rows: list[list[Any]] = []
        for row in payload.get("mayo", []):
            if not isinstance(row, list) or len(row) < 3:
                continue
            tool_id = self._canonical_tool_id(row[0])
            if not tool_id:
                continue
            semantic = str(row[1])
            if semantic == "recover" and tool_id in remaining_procedure_tools:
                semantic = "reuse"
            protected_mayo_rows.append([tool_id, semantic, row[2]])
        payload["mayo"] = protected_mayo_rows

        retrieve = payload.get("mayo_retrieve", ["", 0.0])
        retrieve_tool = (
            self._canonical_tool_id(retrieve[0])
            if isinstance(retrieve, list) and retrieve
            else ""
        )
        if retrieve_tool in remaining_procedure_tools:
            payload["mayo_retrieve"] = ["", 0.0]

        digital_twin = context_dict.get("digital_twin", {}) if isinstance(context_dict, dict) else {}
        hands = digital_twin.get("hands", {}) if isinstance(digital_twin, dict) else {}
        tools = digital_twin.get("tools", []) if isinstance(digital_twin, dict) else []
        stable_cam4_tools = {
            self._canonical_tool_id(row.get("name", ""))
            for row in (
                context_dict.get("observable_perception", {}).get(
                    "tools",
                    [],
                )
                if isinstance(context_dict, dict)
                and isinstance(
                    context_dict.get("observable_perception", {}),
                    dict,
                )
                else []
            )
            if isinstance(row, dict)
            and int(row.get("stable_sample_count", 0) or 0)
            >= CAM4_MAYO_MIN_STABLE_SAMPLES
            and float(row.get("stable_duration_sec", 0.0) or 0.0)
            >= CAM4_MAYO_MIN_STABLE_DURATION_SEC
            and float(row.get("max_confidence", 0.0) or 0.0)
            >= CAM4_MAYO_MIN_CONFIDENCE
        }
        stable_cam4_tools.discard("")
        blocked_tools = {
            str(hands.get(key, ""))
            for key in ("rh", "lh", "pre")
            if str(hands.get(key, ""))
        }
        for row in tools if isinstance(tools, list) else []:
            if not isinstance(row, dict):
                continue
            tool_id = str(row.get("id", ""))
            lifecycle = str(row.get("lc", ""))
            location_type = str(row.get("lt", ""))
            if not tool_id:
                continue
            if (
                lifecycle in {"home_rack", "returned_home"}
                and tool_id not in stable_cam4_tools
            ):
                blocked_tools.add(tool_id)
            if lifecycle in {
                "prepositioned_right",
                "recovering_left",
                "cleaning_left",
                "cleaned_left",
                "dropped_floor",
            }:
                blocked_tools.add(tool_id)
            if location_type in {
                "tray_slot",
                "robot_right_hand",
                "robot_left_hand",
                "cleaner_slot",
                "floor_zone",
            }:
                blocked_tools.add(tool_id)
            if (
                location_type in {"surgical_field", "bed_fixed_tool"}
                and tool_id not in stable_cam4_tools
            ):
                blocked_tools.add(tool_id)
        if not blocked_tools:
            return

        mayo_rows = []
        for row in payload.get("mayo", []):
            if not isinstance(row, list) or len(row) < 3:
                continue
            if str(row[0]) in blocked_tools:
                continue
            mayo_rows.append(row)
        payload["mayo"] = mayo_rows

        recovery_tools = {
            str(row[0])
            for row in mayo_rows
            if len(row) >= 2 and str(row[1]) == "recover"
        }
        retrieve = payload.get("mayo_retrieve", ["", 0.0])
        if (
            not isinstance(retrieve, list)
            or not retrieve
            or str(retrieve[0]) in blocked_tools
            or str(retrieve[0]) not in recovery_tools
        ):
            payload["mayo_retrieve"] = ["", 0.0]

    def _run_model(
        self,
        context_json: str,
        images: list[tuple[str, bytes, str]],
        claimed_dialogue_turn_id: str = "",
    ) -> tuple[str, dict[str, Any] | None, float, str, int, str]:
        retries_used = 0
        if self._response_mode == "replay":
            if self._replay_payload is None:
                raise RuntimeError("response_mode=replay requires replay_response_path")
            raw = json.dumps(self._replay_payload, separators=(",", ":"), sort_keys=True)
            normalized_raw, payload = self._normalize_model_raw_text(
                raw,
                claimed_dialogue_turn_id=claimed_dialogue_turn_id,
            )
            return normalized_raw, payload, 0.0, "replay", retries_used, ""
        if self._response_mode == "oracle":
            payload = self._fallback_payload(json.loads(context_json))
            raw = json.dumps(payload, separators=(",", ":"), sort_keys=True)
            normalized_raw, payload = self._normalize_model_raw_text(
                raw,
                claimed_dialogue_turn_id=claimed_dialogue_turn_id,
            )
            return normalized_raw, payload, 0.0, "oracle", retries_used, ""

        last_error = ""
        last_raw_text = ""
        transport_failed = False
        started_monotonic = time.monotonic()
        for attempt in range(self._retry_count + 1):
            developer_prompt = self._developer_instruction
            if attempt > 0:
                validation_error = last_error.split(";", 1)[0].strip()[:180]
                if (
                    getattr(self, "_task_profile", VLM_TASK_PROFILE_FULL)
                    == VLM_TASK_PROFILE_TOOL_FORECAST_ONLY
                ):
                    developer_prompt += (
                        " Previous response failed schema validation: "
                        f"{validation_error}. Re-emit only the complete two-field "
                        "tool/u JSON object with nested candidate rows."
                    )
                else:
                    context_mode = getattr(self, "_context_mode", "")
                    schema_version = (
                        "6"
                        if context_mode == "actor_log"
                        else ("1" if context_mode == "world" else "4")
                    )
                    developer_prompt += (
                        " Previous response failed schema validation: "
                        f"{validation_error}. Correct that field and re-emit the complete "
                        f"schema-v{schema_version} JSON object only; do not simplify any array shape."
                    )
            try:
                response = self._client.request_json(
                    system_prompt=self._system_prompt,
                    developer_prompt=developer_prompt,
                    user_context_json=context_json,
                    images=images,
                    model_id=self._model_id,
                    temperature=self._temperature,
                    top_p=self._top_p,
                    max_output_tokens=self._max_output_tokens,
                    api_mode=self._api_mode,
                    response_format=self._response_format,
                    json_schema=self._json_schema,
                    reasoning_effort=self._reasoning_effort,
                    generation_seed=self._generation_seed,
                )
                last_raw_text = response.raw_text
                normalized_raw, payload = self._normalize_model_raw_text(
                    response.raw_text,
                    claimed_dialogue_turn_id=claimed_dialogue_turn_id,
                )
                return normalized_raw, payload, response.latency_sec, response.mode, attempt, ""
            except (requests.RequestException, SchemaValidationError, json.JSONDecodeError, RuntimeError, ValueError) as exc:  # type: ignore[name-defined]
                last_error = str(exc)
                if last_raw_text and not isinstance(exc, requests.RequestException):
                    excerpt = re.sub(r"\s+", " ", last_raw_text).strip()[:320]
                    last_error = (
                        f"{last_error}; raw_response_chars={len(last_raw_text)}; "
                        f"raw_response_excerpt={excerpt!r}"
                    )
                transport_failed = transport_failed or isinstance(
                    exc,
                    requests.RequestException,
                )
                retries_used = attempt + 1
        failure_mode = (
            "inference_transport_failed"
            if transport_failed
            else "inference_response_failed"
        )
        return (
            last_raw_text,
            None,
            max(0.0, time.monotonic() - started_monotonic),
            failure_mode,
            retries_used,
            last_error or "VLM inference failed without an error message",
        )

    def _normalize_model_raw_text(
        self,
        raw_text: str,
        *,
        claimed_dialogue_turn_id: str = "",
    ) -> tuple[str, dict[str, Any]]:
        if (
            getattr(self, "_task_profile", VLM_TASK_PROFILE_FULL)
            == VLM_TASK_PROFILE_TOOL_FORECAST_ONLY
        ):
            return normalize_tool_forecast_raw_text(raw_text)
        if (
            str(getattr(self, "_response_mode", "")).strip().lower() == "live"
            and str(getattr(self, "_provider_id", "")).strip().lower()
            == "ninfer"
            and str(getattr(self, "_context_mode", "")).strip().lower()
            == "actor_log"
        ):
            payload = _repair_ninfer_dialogue_envelope(
                parse_json_payload(raw_text),
                claimed_turn_id=claimed_dialogue_turn_id,
            )
            raw_text = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        return normalize_raw_text(raw_text)

    def _cacheable_payload(
        self,
        raw_json: str,
        payload: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        """Remove one-shot commands and speech from temporal phase/tool memory."""
        version = str(payload.get("v", ""))
        has_one_shot = payload.get("bed_robot_arm_group") is not None
        if version in {"5", "6"}:
            has_one_shot = has_one_shot or any(
                payload.get(key) is not None
                for key in ("function_call", "humanoid_reply")
            )
        if version not in {"4", "5", "6"} or not has_one_shot:
            return raw_json, payload
        cache_payload = dict(payload)
        cache_payload["bed_robot_arm_group"] = None
        if version in {"5", "6"}:
            cache_payload["function_call"] = None
            cache_payload["humanoid_reply"] = None
        return (
            json.dumps(cache_payload, separators=(",", ":"), sort_keys=True),
            cache_payload,
        )

    def _tick(
        self,
        force: bool = False,
        inference_trigger: str = "",
    ) -> bool:
        trigger = str(inference_trigger).strip()
        if not trigger:
            trigger = (
                INFERENCE_TRIGGER_FORCED
                if force
                else INFERENCE_TRIGGER_PERIODIC_LIVE
            )
        if (
            trigger == INFERENCE_TRIGGER_PERIODIC_LIVE
            and (
                getattr(self, "_response_mode", "live") == "replay"
                or (
                    getattr(self, "_response_mode", "live") == "live"
                    and getattr(
                        self,
                        "_source_time_triggered_live",
                        False,
                    )
                )
            )
        ):
            return False
        failure_backoff = getattr(self, "_transport_failure_backoff", None)
        if (
            trigger != INFERENCE_TRIGGER_FORCED
            and failure_backoff is not None
            and failure_backoff.remaining() > 0.0
        ):
            return False
        admission = self._inference_backpressure.request(trigger)
        if not admission.started:
            return False
        self._run_inference_chain(trigger)
        return True

    def _run_inference_chain(self, initial_trigger: str) -> None:
        """Drain the newest pending frame immediately after each completion."""

        current_trigger: str | None = initial_trigger
        while current_trigger is not None:
            try:
                self._tick_once(
                    force=current_trigger != INFERENCE_TRIGGER_PERIODIC_LIVE,
                    inference_trigger=current_trigger,
                )
            except Exception as exc:  # pragma: no cover - final node boundary
                self._record_inference_failure(
                    trigger=current_trigger,
                    mode="unhandled_exception",
                    error=str(exc),
                    image_source="",
                    latency_sec=0.0,
                    prompt_chars=0,
                    retry_count=0,
                    connected=False,
                )
            finally:
                failure_backoff = getattr(
                    self,
                    "_transport_failure_backoff",
                    None,
                )
                backoff_remaining = (
                    failure_backoff.remaining()
                    if failure_backoff is not None
                    else 0.0
                )
                if backoff_remaining > 0.0:
                    retryable_current = (
                        current_trigger
                        if current_trigger != INFERENCE_TRIGGER_FORCED
                        else ""
                    )
                    pending_trigger = self._inference_backpressure.defer_until_ready(
                        retryable_current
                    )
                    if pending_trigger == INFERENCE_TRIGGER_FORCED:
                        # An operator request queued while ordinary inference
                        # was failing must retain the forced backoff bypass.
                        # Wake the single consumer immediately instead of
                        # inheriting the transport retry delay.
                        self._cancel_inference_retry()
                        wakeup = getattr(self, "_inference_wakeup", None)
                        if wakeup is not None:
                            wakeup.set()
                    elif pending_trigger:
                        self._schedule_inference_retry(backoff_remaining)
                    current_trigger = None
                else:
                    current_trigger = self._inference_backpressure.complete()

    def _tick_once(
        self,
        force: bool = False,
        inference_trigger: str = INFERENCE_TRIGGER_PERIODIC_LIVE,
    ) -> None:
        if not force and not self._active:
            return
        if self._response_mode == "replay" and not force:
            return
        perception_generation = getattr(
            self,
            "_perception_generation",
            0,
        )
        images, image_source, model_image = self._select_images()
        model_image_stamp_sec: float | None = None
        if model_image is not None:
            model_image_stamp_sec = (
                float(model_image.stamp_sec)
                + float(model_image.stamp_nanosec) / 1_000_000_000.0
            )
        context_stamp = self.get_clock().now().to_msg()
        if model_image is not None:
            context_stamp.sec = int(model_image.stamp_sec)
            context_stamp.nanosec = int(model_image.stamp_nanosec)
        submitted_dialogue_turn_id = ""
        if self._context_mode == "actor_log":
            context_dict = self._assemble_actor_log_context_dict()
            static_prompt_chars = len(self._system_prompt) + len(
                self._developer_instruction
            )
            model_context_dict = actor_log_request_context(
                context_dict,
                static_prompt_chars=static_prompt_chars,
            )
            pending_dialogue_turn = model_context_dict.get(
                "pending_dialogue_turn"
            )
            if isinstance(pending_dialogue_turn, dict):
                submitted_dialogue_turn_id = str(
                    pending_dialogue_turn.get("turn_id", "")
                ).strip()
            request_context_json = compact_prompt_json(model_context_dict)
            request_context_msg = self._actor_log_request_context_msg(
                context_dict,
                request_context_json,
                context_stamp,
            )
            context_stamp = request_context_msg.stamp
            self._request_context_pub.publish(request_context_msg)
        else:
            if self._world is None or self._simulation is None:
                return
            request_context, context_dict = self._assemble_context(
                perception_reference_stamp_sec=model_image_stamp_sec,
            )
            context_dict["visual_input"] = dict(self._current_visual_input)
            request_context.compact_json = compact_json(context_dict)
            request_context.stamp = context_stamp
            request_context_json = request_context.compact_json
            context_stamp = request_context.stamp
        dialogue_text_only = bool(
            getattr(self, "_enable_text_only_dialogue", False)
            and submitted_dialogue_turn_id
            and inference_trigger == INFERENCE_TRIGGER_SPEECH
            and not images
        )
        if dialogue_text_only:
            # A spoken turn may use the same model without manufacturing
            # visual evidence.  The result is confined below to the dialogue
            # envelope; phase, tool, Mayo, and action proposals are never
            # published from this image-free request.
            self._current_image_input_error = ""
            image_source = "dialogue_text_only"
        prompt_chars = len(self._system_prompt) + len(self._developer_instruction) + len(request_context_json)
        raw_json = ""
        payload: dict[str, Any] | None = None
        latency_sec = 0.0
        mode = self._response_mode
        parse_retry_count = 0
        last_error = ""
        healthy = True
        connected = True
        if self._current_image_input_error:
            self._publish_health(
                image_source=image_source,
                latency_sec=0.0,
                prompt_chars=prompt_chars,
                output_chars=0,
                parse_retry_count=0,
                last_error=self._current_image_input_error,
                mode="missing_visual_input",
                healthy=False,
                connected=True,
            )
            return
        if (
            self._response_mode == "live"
            and self._require_field_image
            and not dialogue_text_only
            and not is_model_ready_visual_source(image_source)
        ):
            self._publish_health(
                image_source=image_source,
                latency_sec=0.0,
                prompt_chars=prompt_chars,
                output_chars=0,
                parse_retry_count=0,
                last_error="no fresh segmented or raw FLIR image",
                mode="no_fresh_image",
                healthy=False,
                connected=True,
            )
            return
        model_input_key = self._current_model_input_signature(
            request_context_json,
            images,
        )
        if model_input_key == getattr(
            self,
            "_last_submitted_model_input_key",
            "",
        ):
            self._exact_duplicate_suppressed_count = (
                max(
                    0,
                    int(
                        getattr(
                            self,
                            "_exact_duplicate_suppressed_count",
                            0,
                        )
                    ),
                )
                + 1
            )
            return
        self._last_submitted_model_input_key = model_input_key
        if self._context_mode != "actor_log":
            # Publish the exact compact context that is about to be submitted
            # so the operations UI exposes the same typed RF-DETR facts the
            # Live VLM receives.  This remains a read-only observer topic.
            self._request_context_pub.publish(request_context)
        (
            source_epoch,
            source_sequence,
            correlation_id,
        ) = self._next_visual_evidence_metadata(model_input_key)
        if submitted_dialogue_turn_id:
            claimed_turn = self._dialogue_turn_gate.claim(
                turn_id=submitted_dialogue_turn_id,
                correlation_id=correlation_id,
            )
            if claimed_turn is None:
                submitted_dialogue_turn_id = ""
        if model_image_stamp_sec is not None:
            self._last_submitted_live_image_stamp_sec = model_image_stamp_sec
        self._publish_model_ready_image(model_image)
        try:
            raw_json, payload, latency_sec, mode, parse_retry_count, last_error = self._run_model(
                request_context_json,
                images,
                submitted_dialogue_turn_id,
            )
        except Exception as exc:  # pragma: no cover - safety net
            last_error = str(exc)
            healthy = False
            connected = False
            payload = None
            raw_json = ""
            mode = "unhandled_model_exception"
        if mode == "inference_transport_failed":
            connected = False
        if (
            self._require_field_image
            and perception_generation
            != getattr(self, "_perception_generation", 0)
        ):
            self._publish_health(
                image_source="",
                latency_sec=latency_sec,
                prompt_chars=prompt_chars,
                output_chars=0,
                parse_retry_count=parse_retry_count,
                last_error="visual assistance mode changed during inference",
                mode="visual_contract_changed",
                healthy=False,
                connected=connected,
            )
            self._release_dialogue_claim(correlation_id)
            return
        if source_epoch != max(
            0,
            int(getattr(self, "_model_input_epoch", 0)),
        ):
            self._publish_health(
                image_source=image_source,
                latency_sec=latency_sec,
                prompt_chars=prompt_chars,
                output_chars=0,
                parse_retry_count=parse_retry_count,
                last_error="inference completed after runtime epoch changed",
                mode="stale_epoch_result_discarded",
                healthy=False,
                connected=connected,
            )
            self._release_dialogue_claim(correlation_id)
            return
        if self._response_mode == "live" and (
            payload is None
            or not healthy
            or last_error
            or mode in {"last_good", "oracle_fallback"}
        ):
            self._record_inference_failure(
                trigger=inference_trigger,
                mode=mode,
                error=last_error or f"unsafe VLM fallback mode: {mode}",
                image_source=image_source,
                latency_sec=latency_sec,
                prompt_chars=prompt_chars,
                retry_count=parse_retry_count,
                connected=connected,
                output_chars=len(raw_json),
            )
            self._release_dialogue_claim(correlation_id)
            return
        if payload is None:
            self._release_dialogue_claim(correlation_id)
            return
        failure_backoff = getattr(self, "_transport_failure_backoff", None)
        if failure_backoff is not None:
            failure_backoff.record_success()
        self._reset_inference_failure_log_throttle()
        payload = self._canonicalize_payload_ids(payload)
        model_raw_json = json.dumps(
            payload,
            separators=(",", ":"),
            sort_keys=True,
        )
        published_mode = (
            f"{mode}:{inference_trigger}"
            if self._response_mode == "live"
            else mode
        )
        if dialogue_text_only:
            self._publish_humanoid_reply(
                payload,
                observation_stamp=context_stamp,
                source_epoch=source_epoch,
                source_sequence=source_sequence,
                correlation_id=correlation_id,
                claimed_turn_id=submitted_dialogue_turn_id,
            )
            self._publish_health(
                image_source=image_source,
                latency_sec=latency_sec,
                prompt_chars=prompt_chars,
                output_chars=len(model_raw_json),
                parse_retry_count=parse_retry_count,
                last_error="",
                mode=f"dialogue_text_only:{mode}",
                healthy=True,
                connected=connected,
            )
            return
        if mode not in {"last_good", "oracle_fallback"}:
            self._publish_model_raw_result(
                payload,
                model_raw_json,
                observation_stamp=context_stamp,
                mode=published_mode,
                source_epoch=source_epoch,
                source_sequence=source_sequence,
                correlation_id=correlation_id,
            )
        raw_json = model_raw_json
        if self._context_mode == "actor_log":
            payload = self._stabilize_actor_log_payload(payload, context_dict)
            payload["mayo"], payload["mayo_retrieve"] = normalize_mayo_semantics(
                payload.get("mayo", []),
                payload.get("mayo_retrieve", ["", 0.0]),
            )
            raw_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
            predicted_phase = self._primary_payload_phase(payload)
            if predicted_phase and predicted_phase != self._last_vlm_phase:
                self._last_vlm_phase = predicted_phase
        if mode not in {"last_good", "oracle_fallback"}:
            self._last_good_raw, self._last_good_payload = self._cacheable_payload(
                raw_json,
                payload,
            )

        dialogue_reply_committed = self._publish_humanoid_reply(
            payload,
            observation_stamp=context_stamp,
            source_epoch=source_epoch,
            source_sequence=source_sequence,
            correlation_id=correlation_id,
            claimed_turn_id=submitted_dialogue_turn_id,
        )
        if str(payload.get("v", "")) in {"5", "6"} and not dialogue_reply_committed:
            payload = dict(payload)
            payload["function_call"] = None
            payload["humanoid_reply"] = None
            raw_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)

        self._publish_vlm_outputs(
            payload,
            raw_json,
            observation_stamp=context_stamp,
            image_source=image_source,
            latency_sec=latency_sec,
            prompt_chars=prompt_chars,
            parse_retry_count=parse_retry_count,
            last_error=last_error,
            mode=published_mode,
            healthy=healthy,
            connected=connected,
            source_epoch=source_epoch,
            source_sequence=source_sequence,
            correlation_id=correlation_id,
        )
        if model_image_stamp_sec is not None:
            self._last_periodic_live_image_stamp_sec = model_image_stamp_sec
        if self._context_mode == "actor_log" and mode not in {
            "last_good",
            "oracle_fallback",
        }:
            self._phase_bootstrap_observation_count += 1
        if self._response_mode == "oracle":
            self._oracle_tick += 1

    def _mark_dialogue_responded(self, utterance_id: str) -> None:
        clean_id = str(utterance_id or "").strip()
        if not clean_id:
            return
        for row in list(self._recent_speech):
            if (
                isinstance(row, dict)
                and str(row.get("utterance_id", "")) == clean_id
            ):
                row["responded"] = True

    def _queue_dialogue_followup(
        self,
        turn_id: str,
        *,
        retry_invalid: bool,
    ) -> bool:
        """Queue one bounded retry or the next FIFO turn without a hot loop."""

        clean_turn_id = str(turn_id or "").strip()
        pending = self._dialogue_turn_gate.next_pending()
        if pending is None:
            return False
        if retry_invalid:
            if pending.turn_id != clean_turn_id:
                return False
            if (
                self._dialogue_turn_gate.attempt_count(clean_turn_id)
                >= DIALOGUE_AUTOMATIC_ATTEMPT_LIMIT
            ):
                return False
            # Permit one identical-context retry. Without this reset, the
            # exact-input suppressor would discard it before a new claim.
            self._last_submitted_model_input_key = ""
        admission = self._inference_backpressure.queue_if_empty(
            INFERENCE_TRIGGER_SPEECH
        )
        return admission.disposition in {"queued", "coalesced"}

    def _publish_humanoid_reply(
        self,
        payload: dict[str, Any],
        *,
        observation_stamp,
        source_epoch: int,
        source_sequence: int,
        correlation_id: str,
        claimed_turn_id: str,
    ) -> bool:
        """Commit and publish at most one reply for the claimed speech turn."""

        clean_claimed_turn_id = str(claimed_turn_id or "").strip()
        if not clean_claimed_turn_id:
            return False
        turn = self._dialogue_turn_gate.next_pending()
        reply_payload = payload.get("humanoid_reply")
        function_call = payload.get("function_call")
        if (
            str(payload.get("v", "")) not in {"5", "6"}
            or turn is None
            or turn.turn_id != clean_claimed_turn_id
            or not isinstance(reply_payload, dict)
        ):
            self._dialogue_turn_gate.commit(
                turn_id=clean_claimed_turn_id,
                correlation_id=correlation_id,
                reply=None,
                valid=False,
            )
            self._queue_dialogue_followup(
                clean_claimed_turn_id,
                retry_invalid=True,
            )
            return False

        reply_turn_id = str(reply_payload.get("turn_id", "")).strip()
        reply_text = " ".join(str(reply_payload.get("text", "")).split())[:240]
        timing = str(reply_payload.get("timing", "")).strip()
        speak = reply_payload.get("speak") is True
        function_call_name = ""
        function_arguments_json = ""
        function_request_id = ""
        function_is_valid = function_call is None
        if isinstance(function_call, dict):
            function_call_name = str(function_call.get("name", "")).strip()
            function_turn_id = str(function_call.get("turn_id", "")).strip()
            function_is_valid = bool(
                function_call_name
                in {"request_tool_handover", "adjust_retraction"}
                and function_turn_id == clean_claimed_turn_id
                and timing in {"on_function_accepted", "on_function_completed"}
            )
            if function_is_valid:
                function_arguments_json = json.dumps(
                    function_call.get("arguments", {}),
                    separators=(",", ":"),
                    sort_keys=True,
                )
                function_request_id = (
                    stable_dialogue_reply_id(
                        turn.procedure_run_id,
                        turn.utterance_id,
                        gateway_instance_id=str(
                            getattr(self, "_gateway_instance_id", "") or ""
                        ),
                    )
                    + ":function"
                )
        elif timing != "immediate":
            function_is_valid = False

        valid = bool(
            reply_turn_id == clean_claimed_turn_id
            and reply_text
            and speak
            and function_is_valid
        )
        if not valid:
            self._dialogue_turn_gate.commit(
                turn_id=reply_turn_id,
                correlation_id=correlation_id,
                reply=reply_text,
                valid=False,
            )
            self._queue_dialogue_followup(
                clean_claimed_turn_id,
                retry_invalid=True,
            )
            return False

        message = HumanoidReply()
        message.stamp = observation_stamp
        self._set_visual_evidence_metadata(
            message,
            source=f"real_vlm:{self._spec.procedure_id}:dialogue",
            source_epoch=source_epoch,
            source_sequence=source_sequence,
            correlation_id=correlation_id,
        )
        message.schema_version = str(payload.get("v", "6"))
        message.gateway_instance_id = str(
            getattr(self, "_gateway_instance_id", "") or ""
        ).strip()
        message.procedure_run_id = turn.procedure_run_id
        message.utterance_id = turn.utterance_id
        message.turn_id = turn.turn_id
        message.reply_id = stable_dialogue_reply_id(
            turn.procedure_run_id,
            turn.utterance_id,
            gateway_instance_id=message.gateway_instance_id,
        )
        message.text = reply_text
        message.kind = "acknowledgement" if function_call_name else "answer"
        message.timing = timing
        message.speak = True
        message.function_call_name = function_call_name
        message.function_arguments_json = function_arguments_json
        message.function_request_id = function_request_id
        message.valid = True
        message.validation_error = ""

        reply_outbox = getattr(self, "_reply_outbox", None)
        if reply_outbox is not None:
            try:
                reply_outbox.enqueue(
                    self._reply_envelope_from_message(message)
                )
            except ReplyCollisionError:
                # A durable first writer from this logical ASR turn already
                # owns the audible result (typically after VLM restart). Mark
                # the in-memory turn answered without publishing a second
                # model variant; the pending first writer is retried by timer.
                committed = self._dialogue_turn_gate.commit(
                    turn_id=reply_turn_id,
                    correlation_id=correlation_id,
                    reply=reply_text,
                    valid=True,
                )
                if committed:
                    self._mark_dialogue_responded(turn.utterance_id)
                    if self._dialogue_turn_gate.next_pending() is not None:
                        self._queue_dialogue_followup(
                            turn.turn_id,
                            retry_invalid=False,
                        )
                return committed
            except Exception as exc:
                self.get_logger().error(
                    "VLM reply outbox enqueue failed: "
                    f"{exc.__class__.__name__}"
                )
                self._dialogue_turn_gate.release(
                    correlation_id=correlation_id
                )
                self._queue_dialogue_followup(
                    clean_claimed_turn_id,
                    retry_invalid=True,
                )
                return False

        if not self._dialogue_turn_gate.commit(
            turn_id=reply_turn_id,
            correlation_id=correlation_id,
            reply=reply_text,
            valid=True,
        ):
            if reply_outbox is not None:
                try:
                    reply_outbox.mark_stale(
                        message.reply_id,
                        reason="dialogue_commit_rejected",
                    )
                except Exception as exc:
                    self.get_logger().error(
                        "VLM reply outbox stale transition failed: "
                        f"{exc.__class__.__name__}"
                    )
            self._queue_dialogue_followup(
                clean_claimed_turn_id,
                retry_invalid=True,
            )
            return False

        self._humanoid_reply_pub.publish(message)
        self._mark_dialogue_responded(turn.utterance_id)

        # Several utterances can arrive before the latest-frame worker begins.
        # Their inference triggers may coalesce, but their FIFO entries must not.
        if self._dialogue_turn_gate.next_pending() is not None:
            self._queue_dialogue_followup(
                turn.turn_id,
                retry_invalid=False,
            )
        return True

    def _validate_bed_robot_arm_group_proposal(
        self,
        proposal: dict[str, Any],
        request: BedRobotArmGroupRequest,
    ) -> str:
        if str(proposal.get("request_id", "")) != str(request.request_id):
            return "request_id does not match the pending retraction request"
        if str(proposal.get("group_id", "")) != "retraction":
            return "VLM proposal must target the retraction group"
        if str(proposal.get("operation", "")) != "retraction":
            return "VLM proposal operation must be retraction"

        adjustment_mode = str(proposal.get("adjustment_mode", ""))
        if adjustment_mode != str(request.adjustment_mode):
            return "adjustment_mode does not match the pending request"
        if str(proposal.get("target_retractor_id", "")) != str(
            request.target_retractor_id
        ):
            return "target_retractor_id does not match the pending request"
        if str(proposal.get("direction_frame", "")) != str(
            request.direction_frame
        ) or str(request.direction_frame) != "surgeon_view":
            return "direction_frame must match the surgeon_view request"
        direction = str(proposal.get("direction", "")).lower()
        axis = str(proposal.get("axis", "")).lower()
        if adjustment_mode == "single":
            if direction not in {"up", "down", "left", "right"} or axis != "none":
                return "single adjustment requires a cardinal direction and axis none"
        elif adjustment_mode == "multi":
            if direction != "none" or axis not in {"left_right", "up_down"}:
                return "multi adjustment requires direction none and a documented axis"
        else:
            return "unsupported adjustment_mode"
        proposal_direction = (
            axis.upper() if adjustment_mode == "multi" else direction.upper()
        )

        requested_profile = str(request.end_effector_profile).strip()
        proposed_profile = str(proposal.get("end_effector_profile", "")).strip()
        if requested_profile and proposed_profile != requested_profile:
            return "end_effector_profile does not match the pending request"

        distance_origin = str(proposal.get("distance_origin", ""))
        raw_distance_text = str(proposal.get("raw_distance_text", "")).strip()
        voice_text = str(request.voice_text).strip()
        if distance_origin == "defaulted" and raw_distance_text:
            return "defaulted distance must have empty raw_distance_text"
        if raw_distance_text:
            compact_raw = "".join(raw_distance_text.split())
            compact_voice = "".join(voice_text.split())
            if compact_raw not in compact_voice:
                return "raw_distance_text is not present in the source request"

        try:
            distance_mm = float(proposal.get("distance_mm", 0.0))
            normalized = normalize_retraction_request(
                voice_text,
                vlm_direction=proposal_direction,
                qualitative_distance_mm=(
                    distance_mm if distance_origin == "qualitative_inferred" else None
                ),
            )
        except (BedRobotArmGroupNormalizationError, TypeError, ValueError) as exc:
            return f"deterministic request normalization failed: {exc}"

        if normalized.direction != proposal_direction:
            return (
                "direction contradicts the source request: "
                f"expected {normalized.direction}"
            )
        if normalized.distance_origin != distance_origin:
            return (
                "distance_origin contradicts the source request: "
                f"expected {normalized.distance_origin}"
            )
        if abs(float(normalized.distance_mm) - distance_mm) > 1e-6:
            return (
                "distance_mm contradicts deterministic source normalization: "
                f"expected {normalized.distance_mm:g} mm"
            )
        return ""

    def _publish_bed_robot_arm_group_proposal(
        self,
        payload: dict[str, Any],
        raw_json: str,
        stamp,
        *,
        source: str = "",
        source_epoch: int = 0,
        source_sequence: int = 0,
        correlation_id: str = "",
    ) -> None:
        if str(payload.get("v", "")) not in {"4", "5", "6"}:
            return
        request = self._latest_bed_robot_arm_group_request
        if request is None:
            return
        request_id = str(request.request_id)
        if request_id == self._last_bed_robot_arm_group_proposal_request_id:
            return

        group_payload = payload.get("bed_robot_arm_group")
        message = BedRobotArmGroupActionProposal()
        message.stamp = stamp
        self._set_visual_evidence_metadata(
            message,
            source=source,
            source_epoch=source_epoch,
            source_sequence=source_sequence,
            correlation_id=correlation_id,
        )
        message.schema_version = str(payload.get("v", "4"))
        message.raw_json = raw_json
        command = BedRobotArmGroupCommand()
        command.stamp = stamp
        command.request_id = request_id
        command.command_id = f"vlm-{request_id}"
        command.group_id = "retraction"
        command.operation = "retraction"
        command.adjustment_mode = str(request.adjustment_mode)
        command.target_retractor_id = str(request.target_retractor_id)
        command.direction_frame = str(request.direction_frame)
        command.end_effector_profile = str(request.end_effector_profile)

        if not isinstance(group_payload, dict):
            message.valid = False
            message.validation_error = (
                "VLM declined retraction: insufficient direction evidence"
            )
        else:
            command.request_id = str(group_payload.get("request_id", request_id))
            command.command_id = f"vlm-{command.request_id}"
            command.group_id = str(group_payload.get("group_id", ""))
            command.operation = str(group_payload.get("operation", ""))
            command.direction = str(group_payload.get("direction", ""))
            command.axis = str(group_payload.get("axis", ""))
            command.adjustment_mode = str(
                group_payload.get("adjustment_mode", "")
            )
            command.target_retractor_id = str(
                group_payload.get("target_retractor_id", "")
            )
            command.direction_frame = str(
                group_payload.get("direction_frame", "")
            )
            command.distance_mm = float(group_payload.get("distance_mm", 0.0))
            command.distance_origin = str(group_payload.get("distance_origin", ""))
            command.raw_distance_text = str(group_payload.get("raw_distance_text", ""))
            command.end_effector_profile = str(
                group_payload.get("end_effector_profile", "")
            )
            command.rationale = str(group_payload.get("rationale", ""))
            command.confidence = float(group_payload.get("confidence", 0.0))
            message.validation_error = self._validate_bed_robot_arm_group_proposal(
                group_payload,
                request,
            )
            message.valid = not bool(message.validation_error)

        message.command = command
        self._bed_robot_arm_group_proposal_pub.publish(message)
        self._last_bed_robot_arm_group_proposal_request_id = request_id

    def _publish_health(
        self,
        *,
        image_source: str,
        latency_sec: float,
        prompt_chars: int,
        output_chars: int,
        parse_retry_count: int,
        last_error: str,
        mode: str,
        healthy: bool,
        connected: bool,
    ) -> None:
        health = VLMHealth()
        health.stamp = self.get_clock().now().to_msg()
        health.connected = bool(connected)
        health.healthy = bool(healthy and not last_error)
        health.model_id = self._model_id
        health.image_source = image_source
        health.latency_sec = float(latency_sec)
        health.prompt_chars = int(prompt_chars)
        health.output_chars = int(output_chars)
        health.parse_retry_count = int(parse_retry_count)
        health.last_error = last_error
        health.last_mode = mode
        self._health_pub.publish(health)

    def _record_inference_failure(
        self,
        *,
        trigger: str,
        mode: str,
        error: str,
        image_source: str,
        latency_sec: float,
        prompt_chars: int,
        retry_count: int,
        connected: bool,
        output_chars: int = 0,
    ) -> None:
        if not connected:
            failure_backoff = getattr(self, "_transport_failure_backoff", None)
            if failure_backoff is not None:
                failure_backoff.record_failure()
        self._inference_failure_count += 1
        failure = InferenceFailure(
            sequence=self._inference_failure_count,
            trigger=str(trigger),
            mode=str(mode),
            error=str(error),
            image_source=str(image_source),
            latency_sec=max(0.0, float(latency_sec)),
            retry_count=max(0, int(retry_count)),
            recorded_monotonic=time.monotonic(),
        )
        self._inference_failures.append(failure)
        self._log_inference_failure(failure)
        self._publish_health(
            image_source=failure.image_source,
            latency_sec=failure.latency_sec,
            prompt_chars=max(0, int(prompt_chars)),
            output_chars=max(0, int(output_chars)),
            parse_retry_count=failure.retry_count,
            last_error=failure.error,
            mode=(
                f"inference_failed:{failure.trigger}:{failure.mode}"
            ),
            healthy=False,
            connected=connected,
        )

    def _log_inference_failure(self, failure: InferenceFailure) -> None:
        signature = (failure.trigger, failure.mode, failure.error)
        previous_signature = getattr(
            self,
            "_last_inference_failure_log_signature",
            None,
        )
        previous_monotonic = float(
            getattr(self, "_last_inference_failure_log_monotonic", 0.0)
        )
        elapsed = failure.recorded_monotonic - previous_monotonic
        if (
            signature == previous_signature
            and elapsed < INFERENCE_FAILURE_LOG_REPEAT_SEC
        ):
            self._suppressed_inference_failure_log_count = (
                int(
                    getattr(
                        self,
                        "_suppressed_inference_failure_log_count",
                        0,
                    )
                )
                + 1
            )
            return

        suppressed = int(
            getattr(self, "_suppressed_inference_failure_log_count", 0)
        )
        suffix = (
            f" ({suppressed} identical failures suppressed)"
            if signature == previous_signature and suppressed > 0
            else ""
        )
        self.get_logger().error(
            "VLM inference failed "
            f"[{failure.trigger}/{failure.mode}]: {failure.error}{suffix}"
        )
        self._last_inference_failure_log_signature = signature
        self._last_inference_failure_log_monotonic = failure.recorded_monotonic
        self._suppressed_inference_failure_log_count = 0

    def _reset_inference_failure_log_throttle(self) -> None:
        self._last_inference_failure_log_signature = None
        self._last_inference_failure_log_monotonic = 0.0
        self._suppressed_inference_failure_log_count = 0

    def _publish_model_raw_result(
        self,
        payload: dict[str, Any],
        raw_json: str,
        *,
        observation_stamp,
        mode: str,
        source_epoch: int = 0,
        source_sequence: int = 0,
        correlation_id: str = "",
    ) -> None:
        """Publish the parsed model response before runtime stabilization.

        This audit-only topic must not feed the reducer or BT. IDs have passed
        canonical ontology normalization, but no speech anchoring, temporal
        prior merge, Mayo corroboration, or intent suppression has run.
        """
        publisher = getattr(self, "_model_raw_result_pub", None)
        if publisher is None:
            return

        schema_version = str(payload.get("v", "1"))
        phase_rows: list[list[Any]]
        observed_rows: list[list[Any]]
        if schema_version in {"3", "4", "5", "6"}:
            phase_rows = [
                [str(item[0]), float(item[1])]
                for item in payload.get("phase", [])
                if isinstance(item, list)
                and len(item) == 2
                and str(item[0])
            ]
            observed_rows = [
                [
                    str(item[0]),
                    "mayo_stand",
                    "mayo_stand",
                    float(item[2]),
                ]
                for item in payload.get("mayo", [])
                if isinstance(item, list)
                and len(item) == 3
                and str(item[0])
            ]
        elif schema_version == "2":
            phase = payload.get("phase", ["", 0.0])
            phase_rows = (
                [[str(phase[0]), float(phase[1])]]
                if isinstance(phase, list)
                and len(phase) == 2
                and str(phase[0])
                else []
            )
            observed_rows = [
                [
                    str(item[0]),
                    "mayo_stand",
                    "mayo_stand",
                    float(item[2]),
                ]
                for item in payload.get("mayo", [])
                if isinstance(item, list)
                and len(item) == 3
                and str(item[0])
            ]
        else:
            phase_rows = [
                list(item)
                for item in payload.get("ph", [])
                if isinstance(item, list) and len(item) == 2
            ]
            observed_rows = [
                list(item)
                for item in payload.get("to", [])
                if isinstance(item, list) and len(item) == 4
            ]

        result = VLMResult()
        result.stamp = observation_stamp
        procedure_id = str(
            getattr(getattr(self, "_spec", None), "procedure_id", "unknown")
        )
        self._set_visual_evidence_metadata(
            result,
            source=f"real_vlm_model_raw:{procedure_id}:{mode}",
            source_epoch=source_epoch,
            source_sequence=source_sequence,
            correlation_id=correlation_id,
        )
        result.schema_version = schema_version
        result.raw_json = raw_json
        result.summary = str(payload.get("sum", ""))
        result.phase_ids = [str(item[0]) for item in phase_rows]
        result.phase_confidences = [
            float(item[1]) for item in phase_rows
        ]
        result.observed_tool_ids = [
            str(item[0]) for item in observed_rows
        ]
        result.observed_location_ids = [
            str(item[1]) for item in observed_rows
        ]
        result.observed_location_types = [
            str(item[2]) for item in observed_rows
        ]
        result.observed_confidences = [
            float(item[3]) for item in observed_rows
        ]
        result.uncertainty = float(payload.get("u", 0.0))
        publisher.publish(result)

    def _publish_vlm_outputs(
        self,
        payload: dict[str, Any],
        raw_json: str,
        *,
        observation_stamp,
        image_source: str,
        latency_sec: float,
        prompt_chars: int,
        parse_retry_count: int,
        last_error: str,
        mode: str,
        healthy: bool,
        connected: bool,
        source_epoch: int = 0,
        source_sequence: int = 0,
        correlation_id: str = "",
    ) -> None:
        stamp = observation_stamp
        schema_version = str(payload.get("v", "1"))
        if schema_version in {"3", "4", "5", "6"}:
            phase_rows = [
                [str(item[0]), float(item[1])]
                for item in payload.get("phase", [])
                if isinstance(item, list) and len(item) == 2 and str(item[0])
            ]
            observed_rows = [
                [str(item[0]), "mayo_stand", "mayo_stand", float(item[2])]
                for item in payload.get("mayo", [])
                if isinstance(item, list) and len(item) == 3 and str(item[0])
            ]
            uncertainty = float(payload.get("u", 0.0))
            summary = str(payload.get("sum", ""))
        elif schema_version == "2":
            phase_rows = [[str(payload["phase"][0]), float(payload["phase"][1])]]
            observed_rows = [
                [str(item[0]), "mayo_stand", "mayo_stand", float(item[2])]
                for item in payload.get("mayo", [])
                if str(item[0])
            ]
            uncertainty = float(payload.get("u", 0.0))
            summary = str(payload.get("sum", ""))
        else:
            phase_rows = list(payload["ph"])
            observed_rows = list(payload["to"])
            uncertainty = float(payload.get("u", 0.0))
            summary = str(payload.get("sum", ""))

        summary = normalize_clinical_analysis(summary)
        phase_rows = [
            [self._canonical_phase_id(item[0]), item[1]]
            for item in phase_rows
            if item and self._canonical_phase_id(item[0])
        ]
        observed_rows = [
            [self._canonical_tool_id(item[0]), item[1], item[2], item[3]]
            for item in observed_rows
            if item and self._canonical_tool_id(item[0])
        ]
        phase_evidence = PhaseEvidence()
        phase_evidence.stamp = stamp
        evidence_source = f"real_vlm:{self._spec.procedure_id}:{mode}"
        self._set_visual_evidence_metadata(
            phase_evidence,
            source=evidence_source,
            source_epoch=source_epoch,
            source_sequence=source_sequence,
            correlation_id=correlation_id,
        )
        phase_evidence.phase_ids = [item[0] for item in phase_rows]
        phase_evidence.phase_confidences = [float(item[1]) for item in phase_rows]
        phase_evidence.visible_instrument_ids = [item[0] for item in observed_rows]
        phase_evidence.visible_instrument_confidences = [float(item[3]) for item in observed_rows]
        phase_evidence.scene_summary = summary
        phase_evidence.uncertainty = uncertainty
        self._phase_pub.publish(phase_evidence)

        # Schema v2+ Mayo rows have already passed CAM4 semantic
        # corroboration. Publish those public observations to the reducer just
        # like legacy v1 rows so DT Mayo state and confidence can actually
        # advance.
        for tool_id, location_id, location_type, confidence in observed_rows:
            observation = ToolObservation()
            observation.stamp = stamp
            self._set_visual_evidence_metadata(
                observation,
                source=evidence_source,
                source_epoch=source_epoch,
                source_sequence=source_sequence,
                correlation_id=correlation_id,
            )
            observation.instrument_id = tool_id
            observation.location_id = location_id
            observation.location_type = location_type
            observation.confidence = float(confidence)
            observation.visible = True
            self._tool_pub.publish(observation)

        result = VLMResult()
        result.stamp = stamp
        self._set_visual_evidence_metadata(
            result,
            source=phase_evidence.source,
            source_epoch=source_epoch,
            source_sequence=source_sequence,
            correlation_id=correlation_id,
        )
        result.schema_version = schema_version
        result.raw_json = raw_json
        result.summary = summary
        result.phase_ids = list(phase_evidence.phase_ids)
        result.phase_confidences = list(phase_evidence.phase_confidences)
        result.observed_tool_ids = [item[0] for item in observed_rows]
        result.observed_location_ids = [item[1] for item in observed_rows]
        result.observed_location_types = [item[2] for item in observed_rows]
        result.observed_confidences = [float(item[3]) for item in observed_rows]
        result.uncertainty = uncertainty
        self._result_pub.publish(result)

        self._publish_bed_robot_arm_group_proposal(
            payload,
            raw_json,
            stamp,
            source=evidence_source,
            source_epoch=source_epoch,
            source_sequence=source_sequence,
            correlation_id=correlation_id,
        )

        self._publish_health(
            image_source=image_source,
            latency_sec=latency_sec,
            prompt_chars=prompt_chars,
            output_chars=len(raw_json),
            parse_retry_count=parse_retry_count,
            last_error=last_error,
            mode=mode,
            healthy=healthy,
            connected=connected,
        )


def main() -> None:
    rclpy.init()
    node = RealVLMNode()
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            executor.remove_node(node)
            executor.shutdown()
            node.destroy_node()
        finally:
            if rclpy.ok():
                rclpy.shutdown()
