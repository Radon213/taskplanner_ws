#!/usr/bin/env python3
"""Audit causal VLM SFT master rows and rendered Unsloth messages.

The audit is deliberately independent from the dataset builder.  It validates
the information boundary (all model inputs must be available at the causal
cutoff), split grouping, label authority, task targets, and the final
assistant-only JSON answer used by Unsloth.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA = "taskplanner.vlm_sft_dataset_audit.v1"
EPSILON_SEC = 1e-6

TASK_ALIASES = {
    "tool_presence_at_transfer": "tool",
    "tool_presence_pseudo": "tool",
    "tool_recognition": "tool",
    "tool_detection": "tool",
    "current_tools": "tool",
    "request_intent": "intent",
    "surgeon_intent": "intent",
    "intent": "intent",
    "current_phase": "phase",
    "phase": "phase",
    "phase_classification": "phase",
    "next_physical_tool": "next_tool",
    "next_tool": "next_tool",
    "next_requested_tool": "next_tool",
    "next_transferred_tool": "next_tool",
    "clinical_observation_interpretation": "clinical",
    "clinical_analysis": "clinical",
}

SPLIT_ALIASES = {
    "train": "train",
    "training": "train",
    "val": "validation",
    "valid": "validation",
    "validation": "validation",
    "dev": "validation",
    "test": "test",
    "stress_test": "test",
}

AUTHORITY_TIERS = {
    "gold": "gold",
    "human": "gold",
    "human_verified": "gold",
    "surgeon_verified": "gold",
    "human_overwrite": "gold",
    "reviewed_human": "gold",
    "authorized_silver": "authorized_silver",
    "user_authorized_assistant": "authorized_silver",
    "authorized_assistant": "authorized_silver",
    "mixed_human_ai": "authorized_silver",
    "silver_user_authorized_ai_assistant": "authorized_silver",
    "silver": "draft_silver",
    "draft_silver": "draft_silver",
    "ai_draft": "draft_silver",
    "assistant_draft": "draft_silver",
    "silver_unreviewed_or_other": "draft_silver",
    "provisional_ai_phase_not_scoring_ground_truth": "draft_silver",
    "silver_ai_draft_needs_surgeon_review": "draft_silver",
    "derived_from_complete_dt_reference": "derived_silver",
    "derived_from_reviewed_human": "derived_gold",
    "derived_from_silver_user_authorized_ai_assistant": (
        "derived_authorized_silver"
    ),
    "pseudo": "pseudo",
    "pseudo_label": "pseudo",
    "ai_inference_reference_not_ground_truth": "pseudo",
    "unverified": "pseudo",
}

MEDIA_BLOCK_TYPES = {"image", "image_url", "video"}
TEXT_BLOCK_TYPES = {"text", "input_text", "output_text"}


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Load an object-per-line JSONL file with actionable line errors."""

    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, raw in enumerate(handle, start=1):
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                value = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{path}:{line_no}: invalid JSON: {exc.msg}"
                ) from exc
            if not isinstance(value, dict):
                raise ValueError(
                    f"{path}:{line_no}: each JSONL row must be an object"
                )
            row = dict(value)
            row.setdefault("_audit_source_line", line_no)
            rows.append(row)
    return rows


def _nested(mapping: Mapping[str, Any], *path: str) -> Any:
    current: Any = mapping
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return None
        current = current[key]
    return current


def _first(mapping: Mapping[str, Any], paths: Sequence[Sequence[str]]) -> Any:
    for path in paths:
        value = _nested(mapping, *path)
        if value is not None:
            return value
    return None


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _normalized_split(row: Mapping[str, Any]) -> str | None:
    raw = _first(
        row,
        (
            ("split", "role"),
            ("split",),
            ("data_split",),
            ("metadata", "split"),
        ),
    )
    if isinstance(raw, Mapping):
        raw = raw.get("role")
    if raw is None:
        return None
    return SPLIT_ALIASES.get(str(raw).strip().lower())


def _raw_split(row: Mapping[str, Any]) -> Any:
    return _first(
        row,
        (
            ("split", "role"),
            ("split",),
            ("data_split",),
            ("metadata", "split"),
        ),
    )


def _fold_id(row: Mapping[str, Any]) -> str | None:
    raw = _first(
        row,
        (
            ("split", "fold_id"),
            ("fold_id",),
            ("metadata", "fold_id"),
        ),
    )
    return raw.strip() if isinstance(raw, str) and raw.strip() else None


def _canonical_task(row: Mapping[str, Any]) -> tuple[str | None, str | None]:
    raw = row.get("task_type")
    if not isinstance(raw, str) or not raw.strip():
        return None, None
    normalized = raw.strip().lower()
    return TASK_ALIASES.get(normalized), normalized


def _parse_json_object(value: Any) -> tuple[dict[str, Any] | None, str | None]:
    if isinstance(value, Mapping):
        return dict(value), None
    if not isinstance(value, str):
        return None, "must be a JSON object or a JSON-encoded object string"
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON: {exc.msg}"
    if not isinstance(parsed, dict):
        return None, "decoded JSON must be an object"
    return parsed, None


def _finding(
    report: dict[str, Any],
    severity: str,
    code: str,
    message: str,
    *,
    example_id: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> None:
    entry: dict[str, Any] = {"code": code, "message": message}
    if example_id is not None:
        entry["example_id"] = example_id
    if details:
        entry["details"] = dict(details)
    report["errors" if severity == "error" else "warnings"].append(entry)


def _extract_authority(row: Mapping[str, Any]) -> tuple[str | None, str | None]:
    authority = row.get("authority")
    if isinstance(authority, Mapping):
        label = authority.get("label")
        tier = authority.get("tier")
        label_normalized = (
            label.strip().lower()
            if isinstance(label, str) and label.strip()
            else None
        )
        tier_normalized = (
            tier.strip().lower()
            if isinstance(tier, str) and tier.strip()
            else None
        )
        if tier_normalized in AUTHORITY_TIERS:
            return AUTHORITY_TIERS[tier_normalized], tier_normalized
        if tier_normalized and tier_normalized.startswith("derived_from_"):
            return "derived_silver", tier_normalized
        if label_normalized in AUTHORITY_TIERS:
            return AUTHORITY_TIERS[label_normalized], label_normalized
        if label_normalized and label_normalized.startswith("derived_from_"):
            return "derived_silver", label_normalized
        raw = label_normalized or tier_normalized
    else:
        raw = _first(
            row,
            (
                ("authority",),
                ("quality", "authority"),
                ("provenance", "authority"),
                ("target", "authority"),
            ),
        )
        if isinstance(raw, Mapping):
            raw = raw.get("label") or raw.get("tier")
    if not isinstance(raw, str) or not raw.strip():
        return None, None
    normalized = raw.strip().lower()
    return AUTHORITY_TIERS.get(normalized), normalized


def _target_for(row: Mapping[str, Any]) -> Any:
    for key in ("target", "reference", "expected"):
        if key in row:
            return row[key]
    return None


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _tool_values(target: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for key in (
        "tool",
        "tools",
        "visible_tools",
        "tool_ids",
        "present_tools",
        "active_tools",
    ):
        value = target.get(key)
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            values.extend(str(item) for item in value if _nonempty_string(item))
    return [value.strip() for value in values if value.strip()]


def _validate_target(
    report: dict[str, Any],
    example_id: str,
    task: str | None,
    raw_target: Any,
) -> dict[str, Any] | None:
    target, error = _parse_json_object(raw_target)
    if error:
        _finding(
            report,
            "error",
            "target_invalid_json",
            f"Target {error}.",
            example_id=example_id,
        )
        return None
    assert target is not None
    if not target:
        _finding(
            report,
            "error",
            "target_empty",
            "Target object must not be empty.",
            example_id=example_id,
        )
        return target

    if task == "tool":
        if not _tool_values(target):
            _finding(
                report,
                "error",
                "target_tool_missing",
                "Tool target requires a non-empty tool label or tool list.",
                example_id=example_id,
            )
        for key in (
            "exhaustive_presence",
            "exhaustive_visible_tool_inventory",
        ):
            exhaustive = target.get(key)
            if exhaustive is not None and not isinstance(exhaustive, bool):
                _finding(
                    report,
                    "error",
                    "target_tool_exhaustive_invalid",
                    f"{key} must be boolean when supplied.",
                    example_id=example_id,
                )
    elif task == "intent":
        intent = target.get("intent") or target.get("action")
        if not _nonempty_string(intent):
            _finding(
                report,
                "error",
                "target_intent_missing",
                "Intent target requires a non-empty intent or action.",
                example_id=example_id,
            )
    elif task == "phase":
        phase = (
            target.get("phase_id")
            or target.get("current_phase")
            or target.get("phase")
        )
        if not _nonempty_string(phase):
            _finding(
                report,
                "error",
                "target_phase_missing",
                "Phase target requires a non-empty phase_id.",
                example_id=example_id,
            )
        state = target.get("state")
        if state is not None and state not in {"interior", "transition"}:
            _finding(
                report,
                "error",
                "target_phase_state_invalid",
                "Phase state must be 'interior' or 'transition'.",
                example_id=example_id,
            )
    elif task == "next_tool":
        tool = (
            target.get("next_transfer_tool")
            or target.get("next_tool")
            or target.get("tool")
        )
        event = target.get("event")
        if not _nonempty_string(tool):
            _finding(
                report,
                "error",
                "target_next_tool_missing",
                "Next-tool target requires next_transfer_tool (use 'none' for no event).",
                example_id=example_id,
            )
        if event is not None and event not in {
            "scrub_nurse_to_surgeon",
            "none",
        }:
            _finding(
                report,
                "error",
                "target_next_tool_event_invalid",
                "Next-tool event must be scrub_nurse_to_surgeon or none.",
                example_id=example_id,
            )
    elif task == "clinical":
        for key in ("observation", "interpretation"):
            if not _nonempty_string(target.get(key)):
                _finding(
                    report,
                    "error",
                    f"target_clinical_{key}_missing",
                    f"Clinical target requires a non-empty {key}.",
                    example_id=example_id,
                )
        confidence = target.get("confidence")
        if confidence is not None and not isinstance(confidence, Mapping):
            _finding(
                report,
                "error",
                "target_clinical_confidence_invalid",
                "Clinical confidence must be an object when supplied.",
                example_id=example_id,
            )
    return target


def _content_text(content: Any) -> tuple[str | None, int, str | None]:
    """Return concatenated text, media block count, and validation error."""

    if isinstance(content, str):
        return content, 0, None
    if not isinstance(content, Sequence) or isinstance(content, (str, bytes)):
        return None, 0, "content must be a string or a list of content blocks"
    text_parts: list[str] = []
    media_count = 0
    for block in content:
        if not isinstance(block, Mapping):
            return None, media_count, "content blocks must be objects"
        block_type = block.get("type")
        if block_type in TEXT_BLOCK_TYPES:
            text_value = block.get("text")
            if not isinstance(text_value, str):
                return None, media_count, "text block requires a string text field"
            text_parts.append(text_value)
        elif block_type in MEDIA_BLOCK_TYPES:
            media_count += 1
            if block_type == "image":
                locator = (
                    block.get("image")
                    or block.get("path")
                    or block.get("url")
                )
            elif block_type == "video":
                locator = (
                    block.get("video")
                    or block.get("path")
                    or block.get("url")
                )
            else:
                locator = block.get("image_url") or block.get("url")
            if not isinstance(locator, (str, Mapping)):
                return (
                    None,
                    media_count,
                    f"{block_type} block requires a media locator",
                )
        else:
            return (
                None,
                media_count,
                f"unsupported content block type: {block_type!r}",
            )
    return "".join(text_parts), media_count, None


def _validate_messages(
    report: dict[str, Any],
    example_id: str,
    row: Mapping[str, Any],
    task: str | None,
    target: Mapping[str, Any] | None,
) -> None:
    messages = row.get("messages")
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
        _finding(
            report,
            "error",
            "messages_invalid",
            "Unsloth row requires a messages list.",
            example_id=example_id,
        )
        return
    if len(messages) < 2:
        _finding(
            report,
            "error",
            "messages_too_short",
            "Messages require at least a user prompt and assistant answer.",
            example_id=example_id,
        )
        return

    roles: list[str] = []
    user_media_count = 0
    assistant_text: str | None = None
    for index, message in enumerate(messages):
        if not isinstance(message, Mapping):
            _finding(
                report,
                "error",
                "message_not_object",
                f"Message {index} must be an object.",
                example_id=example_id,
            )
            continue
        role = message.get("role")
        if role not in {"system", "user", "assistant"}:
            _finding(
                report,
                "error",
                "message_role_invalid",
                f"Message {index} has invalid role {role!r}.",
                example_id=example_id,
            )
            continue
        roles.append(str(role))
        text, media_count, content_error = _content_text(message.get("content"))
        if content_error:
            _finding(
                report,
                "error",
                "message_content_invalid",
                f"Message {index}: {content_error}.",
                example_id=example_id,
            )
        if role == "user":
            user_media_count += media_count
        if role == "assistant" and index == len(messages) - 1:
            assistant_text = text

    if "user" not in roles:
        _finding(
            report,
            "error",
            "messages_user_missing",
            "Messages require a user turn.",
            example_id=example_id,
        )
    if not roles or roles[-1] != "assistant":
        _finding(
            report,
            "error",
            "messages_final_assistant_missing",
            "The final message must be the supervised assistant answer.",
            example_id=example_id,
        )
        return
    if assistant_text is None:
        return
    rendered_target, assistant_error = _parse_json_object(assistant_text)
    if assistant_error:
        _finding(
            report,
            "error",
            "assistant_answer_invalid_json",
            f"Final assistant answer {assistant_error}.",
            example_id=example_id,
        )
    expected_target = target
    if task == "next_tool" and target is not None:
        expected_target = {
            key: target[key]
            for key in ("next_transfer_tool", "event", "basis")
            if key in target
        }
    if (
        rendered_target is not None
        and expected_target is not None
        and rendered_target != expected_target
    ):
        _finding(
            report,
            "error",
            "assistant_target_mismatch",
            "Rendered assistant JSON does not exactly match the master target.",
            example_id=example_id,
        )

    declared_media = row.get("media")
    if isinstance(declared_media, Sequence) and not isinstance(
        declared_media, (str, bytes)
    ):
        if user_media_count != len(declared_media):
            _finding(
                report,
                "warning",
                "message_media_count_mismatch",
                "Rendered user media count differs from the row media count.",
                example_id=example_id,
                details={
                    "rendered_media_count": user_media_count,
                    "declared_media_count": len(declared_media),
                },
            )


def _event_available_sec(event: Mapping[str, Any]) -> float | None:
    for key in (
        "available_sec",
        "end_sec",
        "event_sec",
        "time_sec",
        "timestamp_sec",
        "sec",
        "start_sec",
    ):
        if key in event:
            return _finite_number(event[key])
    return None


def _event_id(event: Mapping[str, Any]) -> str | None:
    for key in ("event_id", "id", "source_event_id", "annotation_id"):
        value = event.get(key)
        if _nonempty_string(value):
            return value.strip()
    return None


def _target_event_ids(target: Mapping[str, Any] | None) -> set[str]:
    if not target:
        return set()
    result: set[str] = set()
    for key in (
        "event_id",
        "source_event_id",
        "target_event_id",
        "label_event_id",
    ):
        value = target.get(key)
        if _nonempty_string(value):
            result.add(value.strip())
    for key in ("source_ids", "event_ids"):
        value = target.get(key)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            result.update(str(item).strip() for item in value if str(item).strip())
    return result


def _iter_contexts(row: Mapping[str, Any]) -> Iterable[tuple[str, Mapping[str, Any]]]:
    for key in ("causal_context", "input_context", "context"):
        value = row.get(key)
        if isinstance(value, Mapping):
            yield key, value


def _audit_causality(
    report: dict[str, Any],
    row: Mapping[str, Any],
    example_id: str,
    target: Mapping[str, Any] | None,
) -> None:
    cutoff = _first(
        row,
        (
            ("time", "causal_cutoff_sec"),
            ("causal_cutoff_sec",),
            ("metadata", "causal_cutoff_sec"),
        ),
    )
    cutoff_sec = _finite_number(cutoff)
    if cutoff_sec is None:
        _finding(
            report,
            "error",
            "causal_cutoff_missing",
            "A finite causal_cutoff_sec is required.",
            example_id=example_id,
        )
        return

    window_end = _finite_number(_nested(row, "time", "window_end_sec"))
    if window_end is not None and window_end > cutoff_sec + EPSILON_SEC:
        _finding(
            report,
            "error",
            "window_future_leakage",
            "Input window_end_sec occurs after causal_cutoff_sec.",
            example_id=example_id,
            details={"window_end_sec": window_end, "cutoff_sec": cutoff_sec},
        )

    target_ids = _target_event_ids(target)
    for context_name, context in _iter_contexts(row):
        for forbidden_key in (
            "future_events",
            "next_event",
            "target_event",
            "future_context",
            "label",
            "target",
        ):
            if forbidden_key in context and context[forbidden_key] not in (
                None,
                "",
                (),
                [],
                {},
            ):
                _finding(
                    report,
                    "error",
                    "future_context_field_present",
                    f"{context_name}.{forbidden_key} is not an allowed model input.",
                    example_id=example_id,
                )

        voice = context.get("voice", [])
        if voice is None:
            voice = []
        if not isinstance(voice, Sequence) or isinstance(voice, (str, bytes)):
            _finding(
                report,
                "error",
                "causal_voice_invalid",
                f"{context_name}.voice must be a list.",
                example_id=example_id,
            )
        else:
            for index, event in enumerate(voice):
                if not isinstance(event, Mapping):
                    _finding(
                        report,
                        "error",
                        "causal_voice_event_invalid",
                        f"{context_name}.voice[{index}] must be an object.",
                        example_id=example_id,
                    )
                    continue
                available = _finite_number(event.get("available_sec"))
                if available is None:
                    _finding(
                        report,
                        "error",
                        "causal_voice_available_missing",
                        f"{context_name}.voice[{index}] requires finite available_sec.",
                        example_id=example_id,
                    )
                elif available > cutoff_sec + EPSILON_SEC:
                    _finding(
                        report,
                        "error",
                        "causal_voice_future_leakage",
                        "Voice is exposed before it becomes available.",
                        example_id=example_id,
                        details={
                            "voice_index": index,
                            "available_sec": available,
                            "cutoff_sec": cutoff_sec,
                        },
                    )

        for event_key in ("prior_events", "history", "observed_events"):
            events = context.get(event_key, [])
            if events is None:
                continue
            if not isinstance(events, Sequence) or isinstance(
                events, (str, bytes)
            ):
                _finding(
                    report,
                    "error",
                    "causal_events_invalid",
                    f"{context_name}.{event_key} must be a list.",
                    example_id=example_id,
                )
                continue
            for index, event in enumerate(events):
                if not isinstance(event, Mapping):
                    _finding(
                        report,
                        "error",
                        "causal_event_invalid",
                        f"{context_name}.{event_key}[{index}] must be an object.",
                        example_id=example_id,
                    )
                    continue
                available = _event_available_sec(event)
                if available is None:
                    _finding(
                        report,
                        "warning",
                        "causal_event_time_unverifiable",
                        f"{context_name}.{event_key}[{index}] has no auditable time.",
                        example_id=example_id,
                    )
                elif available > cutoff_sec + EPSILON_SEC:
                    _finding(
                        report,
                        "error",
                        "causal_event_future_leakage",
                        "A prior event is not available at the causal cutoff.",
                        example_id=example_id,
                        details={
                            "event_index": index,
                            "available_sec": available,
                            "cutoff_sec": cutoff_sec,
                        },
                    )
                current_event_id = _event_id(event)
                if current_event_id and current_event_id in target_ids:
                    _finding(
                        report,
                        "error",
                        "target_event_in_input",
                        "The target event id is present in causal prior_events.",
                        example_id=example_id,
                        details={"event_id": current_event_id},
                    )

    media = row.get("media", [])
    if not isinstance(media, Sequence) or isinstance(media, (str, bytes)):
        _finding(
            report,
            "error",
            "media_invalid",
            "media must be a list.",
            example_id=example_id,
        )
        return
    if not media:
        _finding(
            report,
            "warning",
            "media_empty",
            "No visual media is attached to this VLM example.",
            example_id=example_id,
        )
    for index, item in enumerate(media):
        if not isinstance(item, Mapping):
            _finding(
                report,
                "error",
                "media_item_invalid",
                f"media[{index}] must be an object.",
                example_id=example_id,
            )
            continue
        relative_sec = _finite_number(item.get("relative_sec"))
        if relative_sec is not None and relative_sec > EPSILON_SEC:
            _finding(
                report,
                "error",
                "media_future_leakage",
                "A media frame has positive relative_sec.",
                example_id=example_id,
                details={"media_index": index, "relative_sec": relative_sec},
            )
        absolute_sec = None
        for key in ("time_sec", "timestamp_sec", "sample_sec", "sec"):
            if key in item:
                absolute_sec = _finite_number(item[key])
                break
        if absolute_sec is not None and absolute_sec > cutoff_sec + EPSILON_SEC:
            _finding(
                report,
                "error",
                "media_future_leakage",
                "A media frame occurs after causal_cutoff_sec.",
                example_id=example_id,
                details={
                    "media_index": index,
                    "media_sec": absolute_sec,
                    "cutoff_sec": cutoff_sec,
                },
            )


def _media_signature(row: Mapping[str, Any]) -> str | None:
    explicit = _first(
        row,
        (
            ("media_group_id",),
            ("metadata", "media_group_id"),
            ("quality", "media_group_id"),
        ),
    )
    if _nonempty_string(explicit):
        return f"id:{explicit.strip()}"
    media = row.get("media")
    if not isinstance(media, Sequence) or isinstance(media, (str, bytes)):
        return None
    normalized: list[dict[str, Any]] = []
    for item in media:
        if not isinstance(item, Mapping):
            continue
        normalized.append(
            {
                key: item[key]
                for key in (
                    "view",
                    "source_frame_idx",
                    "time_sec",
                    "timestamp_sec",
                    "relative_sec",
                    "path",
                    "ref",
                    "sha256",
                )
                if key in item
            }
        )
    if not normalized:
        return None
    return "media:" + json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def audit_dataset(
    master_rows: Sequence[Mapping[str, Any]],
    message_rows: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Audit in-memory master and optional rendered-message rows."""

    report: dict[str, Any] = {
        "schema": SCHEMA,
        "ok": False,
        "summary": {},
        "checks": {},
        "errors": [],
        "warnings": [],
    }
    task_counts: Counter[str] = Counter()
    split_counts: Counter[str] = Counter()
    authority_counts: Counter[str] = Counter()
    raw_authority_counts: Counter[str] = Counter()
    seen_examples: dict[str, int] = {}
    split_group_roles: dict[str, set[str]] = defaultdict(set)
    split_group_folds: dict[str, set[str]] = defaultdict(set)
    case_roles: dict[str, set[str]] = defaultdict(set)
    case_folds: dict[str, set[str]] = defaultdict(set)
    media_roles: dict[str, set[str]] = defaultdict(set)
    media_examples: dict[str, list[tuple[str, str, str | None]]] = defaultdict(
        list
    )
    validated_targets: dict[str, dict[str, Any] | None] = {}

    message_by_id: dict[str, Mapping[str, Any]] = {}
    if message_rows is not None:
        for index, message_row in enumerate(message_rows):
            raw_id = message_row.get("example_id")
            if not _nonempty_string(raw_id):
                _finding(
                    report,
                    "error",
                    "message_example_id_missing",
                    f"Rendered message row {index} has no example_id.",
                )
                continue
            message_id = raw_id.strip()
            if message_id in message_by_id:
                _finding(
                    report,
                    "error",
                    "message_example_id_duplicate",
                    "Rendered messages contain a duplicate example_id.",
                    example_id=message_id,
                )
            else:
                message_by_id[message_id] = message_row

    for index, raw_row in enumerate(master_rows):
        row = dict(raw_row)
        raw_id = row.get("example_id")
        if not _nonempty_string(raw_id):
            example_id = f"<row:{index}>"
            _finding(
                report,
                "error",
                "example_id_missing",
                "Master row requires a non-empty example_id.",
                example_id=example_id,
            )
        else:
            example_id = raw_id.strip()
            if example_id in seen_examples:
                _finding(
                    report,
                    "error",
                    "example_id_duplicate",
                    "Master rows contain a duplicate example_id.",
                    example_id=example_id,
                    details={
                        "first_index": seen_examples[example_id],
                        "duplicate_index": index,
                    },
                )
            else:
                seen_examples[example_id] = index

        task, raw_task = _canonical_task(row)
        if raw_task is None:
            _finding(
                report,
                "error",
                "task_type_missing",
                "Master row requires task_type.",
                example_id=example_id,
            )
            task_counts["<missing>"] += 1
        elif task is None:
            task_counts[raw_task] += 1
            _finding(
                report,
                "warning",
                "task_type_unknown",
                f"Unknown task_type {raw_task!r}; only generic target checks ran.",
                example_id=example_id,
            )
        else:
            task_counts[raw_task] += 1

        split = _normalized_split(row)
        fold_id = _fold_id(row)
        raw_split = _raw_split(row)
        if split is None:
            _finding(
                report,
                "error",
                "split_invalid",
                f"Split role is missing or unsupported: {raw_split!r}.",
                example_id=example_id,
            )
        else:
            split_counts[split] += 1

        split_group = row.get("split_group_id")
        if not _nonempty_string(split_group):
            split_group = _nested(row, "metadata", "split_group_id")
        if not _nonempty_string(split_group):
            _finding(
                report,
                "error",
                "split_group_missing",
                "split_group_id is required to prevent related-sample leakage.",
                example_id=example_id,
            )
        elif split is not None:
            split_group_roles[split_group.strip()].add(split)
            if fold_id is not None:
                split_group_folds[split_group.strip()].add(fold_id)

        case_id = row.get("case_id") or _nested(row, "metadata", "case_id")
        if not _nonempty_string(case_id):
            _finding(
                report,
                "error",
                "case_id_missing",
                "case_id is required for case-group leakage checks.",
                example_id=example_id,
            )
        elif split is not None:
            case_roles[case_id.strip()].add(split)
            if fold_id is not None:
                case_folds[case_id.strip()].add(fold_id)

        authority_tier, raw_authority = _extract_authority(row)
        if raw_authority is None:
            authority_counts["<missing>"] += 1
            _finding(
                report,
                "error",
                "authority_missing",
                "Each target requires an explicit authority tier.",
                example_id=example_id,
            )
        elif authority_tier is None:
            raw_authority_counts[raw_authority] += 1
            authority_counts["unknown"] += 1
            _finding(
                report,
                "error",
                "authority_unknown",
                f"Unsupported authority label {raw_authority!r}.",
                example_id=example_id,
            )
        else:
            raw_authority_counts[raw_authority] += 1
            authority_counts[authority_tier] += 1
            if authority_tier == "pseudo" and split != "train":
                _finding(
                    report,
                    "error",
                    "pseudo_nontrain_split",
                    "Pseudo-label targets are allowed only in the train split.",
                    example_id=example_id,
                    details={"split": split},
                )

        target = _validate_target(
            report, example_id, task, _target_for(row)
        )
        validated_targets[example_id] = target
        _audit_causality(report, row, example_id, target)

        media_signature = _media_signature(row)
        if media_signature and split is not None:
            media_roles[media_signature].add(split)
            media_examples[media_signature].append(
                (example_id, split, task)
            )

        if message_rows is None and "messages" in row:
            _validate_messages(report, example_id, row, task, target)
        elif message_rows is not None:
            rendered = message_by_id.get(example_id)
            if rendered is None:
                _finding(
                    report,
                    "error",
                    "messages_row_missing",
                    "No rendered Unsloth message row exists for the master row.",
                    example_id=example_id,
                )
            else:
                merged = dict(rendered)
                if "media" not in merged and "media" in row:
                    merged["media"] = row["media"]
                _validate_messages(report, example_id, merged, task, target)
                rendered_task = rendered.get("task_type")
                if (
                    isinstance(rendered_task, str)
                    and raw_task is not None
                    and rendered_task.strip().lower() != raw_task
                ):
                    _finding(
                        report,
                        "error",
                        "messages_task_mismatch",
                        "Rendered task_type differs from the master row.",
                        example_id=example_id,
                    )
                rendered_split = _normalized_split(rendered)
                if rendered_split is not None and split != rendered_split:
                    _finding(
                        report,
                        "error",
                        "messages_split_mismatch",
                        "Rendered split differs from the master row.",
                        example_id=example_id,
                    )
                rendered_fold = _fold_id(rendered)
                if rendered_fold is not None and fold_id != rendered_fold:
                    _finding(
                        report,
                        "error",
                        "messages_fold_mismatch",
                        "Rendered fold_id differs from the master row.",
                        example_id=example_id,
                    )

    master_ids = set(seen_examples)
    if message_rows is not None:
        for extra_id in sorted(set(message_by_id) - master_ids):
            _finding(
                report,
                "error",
                "messages_orphan_row",
                "Rendered messages contain an example absent from master rows.",
                example_id=extra_id,
            )

    for split_group, roles in sorted(split_group_roles.items()):
        if len(roles) > 1:
            _finding(
                report,
                "error",
                "split_group_leakage",
                "One split_group_id occurs in multiple split roles.",
                details={"split_group_id": split_group, "roles": sorted(roles)},
            )
    for split_group, fold_ids in sorted(split_group_folds.items()):
        if len(fold_ids) > 1:
            _finding(
                report,
                "error",
                "split_group_fold_leakage",
                "One split_group_id is assigned to multiple fold_id values.",
                details={
                    "split_group_id": split_group,
                    "fold_ids": sorted(fold_ids),
                },
            )
    for case_id, roles in sorted(case_roles.items()):
        if len(roles) > 1:
            _finding(
                report,
                "error",
                "case_split_leakage",
                "One case_id occurs in multiple split roles.",
                details={"case_id": case_id, "roles": sorted(roles)},
            )
    for case_id, fold_ids in sorted(case_folds.items()):
        if len(fold_ids) > 1:
            _finding(
                report,
                "error",
                "case_fold_leakage",
                "One case_id is assigned to multiple fold_id values.",
                details={"case_id": case_id, "fold_ids": sorted(fold_ids)},
            )
    for signature, roles in media_roles.items():
        if len(roles) > 1:
            _finding(
                report,
                "error",
                "media_split_leakage",
                "Identical media occurs in multiple split roles.",
                details={
                    "roles": sorted(roles),
                    "examples": [
                        example_id
                        for example_id, _, _ in media_examples[signature]
                    ],
                },
            )
        groups: Counter[tuple[str, str | None]] = Counter(
            (split, task) for _, split, task in media_examples[signature]
        )
        for (split, task), count in groups.items():
            if count > 1:
                _finding(
                    report,
                    "warning",
                    "duplicate_media_task_examples",
                    "Identical media is reused for the same task in one split.",
                    details={
                        "split": split,
                        "task": task,
                        "count": count,
                        "examples": [
                            example_id
                            for example_id, example_split, example_task in media_examples[
                                signature
                            ]
                            if example_split == split and example_task == task
                        ],
                    },
                )

    for tier in (
        "draft_silver",
        "derived_gold",
        "derived_authorized_silver",
        "derived_silver",
        "pseudo",
    ):
        count = authority_counts.get(tier, 0)
        if count:
            _finding(
                report,
                "warning",
                f"authority_{tier}_present",
                (
                    f"{count} target(s) use {tier}; report metrics separately "
                    "from human-verified evaluation labels."
                ),
                details={"count": count},
            )

    report["summary"] = {
        "master_rows": len(master_rows),
        "message_rows": len(message_rows) if message_rows is not None else None,
        "unique_example_ids": len(seen_examples),
        "task_counts": dict(sorted(task_counts.items())),
        "split_counts": dict(sorted(split_counts.items())),
        "authority_tier_counts": dict(sorted(authority_counts.items())),
        "authority_label_counts": dict(sorted(raw_authority_counts.items())),
        "error_count": len(report["errors"]),
        "warning_count": len(report["warnings"]),
    }
    report["checks"] = {
        "example_id_unique": not any(
            item["code"] == "example_id_duplicate"
            for item in report["errors"]
        ),
        "split_group_isolated": not any(
            item["code"]
            in {
                "split_group_leakage",
                "split_group_fold_leakage",
                "case_split_leakage",
                "case_fold_leakage",
                "media_split_leakage",
            }
            for item in report["errors"]
        ),
        "causal_inputs_only": not any(
            "future_leakage" in item["code"]
            or item["code"]
            in {"future_context_field_present", "target_event_in_input"}
            for item in report["errors"]
        ),
        "targets_valid_json": not any(
            item["code"].startswith("target_") and "input" not in item["code"]
            for item in report["errors"]
        ),
        "messages_valid": not any(
            item["code"].startswith(("message", "messages", "assistant_"))
            for item in report["errors"]
        ),
        "authority_known": not any(
            item["code"].startswith("authority_")
            for item in report["errors"]
        ),
    }
    report["ok"] = not report["errors"]
    return report


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit causal VLM SFT master and Unsloth JSONL rows."
    )
    parser.add_argument("--master", type=Path, required=True)
    parser.add_argument("--messages", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument(
        "--strict-warnings",
        action="store_true",
        help="Return a non-zero status when warnings are present.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        master_rows = load_jsonl(args.master)
        message_rows = load_jsonl(args.messages) if args.messages else None
        report = audit_dataset(master_rows, message_rows)
    except (OSError, ValueError) as exc:
        print(f"audit_vlm_dataset: {exc}", file=sys.stderr)
        return 2

    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    if not report["ok"]:
        return 2
    if args.strict_warnings and report["warnings"]:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
