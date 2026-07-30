#!/usr/bin/env python3
"""Validate minimal observable interaction and phase point streams."""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any

import jsonschema
import yaml


STREAM_EVENT_TYPES = {
    "interaction": {"implicit_tool_request", "tool_transfer"},
    "request": {"implicit_tool_request"},
    "transfer": {"tool_transfer"},
    "phase": {"phase_start"},
}
EVENT_ID_PREFIX = {
    "implicit_tool_request": "R",
    "tool_transfer": "T",
    "phase_start": "PH",
}
TIMELINE_SCHEMA = "taskplanner.video_frame_timeline.v1"


def reject_nonstandard_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON numeric constant: {value}")


def load_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=reject_nonstandard_json_constant,
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(
                    line,
                    parse_constant=reject_nonstandard_json_constant,
                )
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: record must be an object")
            value["_line"] = line_number
            records.append(value)
    return records


def finite_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def count_string_field(
    records: list[dict[str, Any]],
    field: str,
) -> dict[str, int]:
    return dict(
        Counter(
            value if isinstance(value, str) else "<invalid-or-missing>"
            for value in (record.get(field) for record in records)
        )
    )


def validate_timeline(
    timeline: dict[str, Any],
    *,
    case_id: str,
) -> tuple[list[float], list[str]]:
    errors: list[str] = []
    if timeline.get("schema") != TIMELINE_SCHEMA:
        errors.append(f"timeline schema must be {TIMELINE_SCHEMA}")
    if timeline.get("case_id") != case_id:
        errors.append(f"timeline case_id must be {case_id}")

    raw_timestamps = timeline.get("timestamps_sec")
    if not isinstance(raw_timestamps, list) or not raw_timestamps:
        errors.append("timeline timestamps_sec must be a non-empty array")
        return [], errors
    if any(not finite_number(value) for value in raw_timestamps):
        errors.append("timeline timestamps_sec must contain only finite numbers")
        return [], errors
    timestamps = [float(value) for value in raw_timestamps]
    if any(right <= left for left, right in zip(timestamps, timestamps[1:])):
        errors.append("timeline timestamps_sec must be strictly increasing")

    frame_count = timeline.get("frame_count")
    if (
        isinstance(frame_count, bool)
        or not isinstance(frame_count, int)
        or frame_count != len(timestamps)
    ):
        errors.append(
            "timeline frame_count must equal the timestamps_sec length"
        )
    source_fps_value = timeline.get("source_fps")
    source_fps: float | None = None
    if not finite_number(source_fps_value) or float(source_fps_value) <= 0:
        errors.append("timeline source_fps must be finite and positive")
    else:
        source_fps = float(source_fps_value)
    for field, expected in (
        ("start_sec", timestamps[0]),
        ("end_sec", timestamps[-1]),
    ):
        value = timeline.get(field)
        if not finite_number(value) or abs(float(value) - expected) > 5e-10:
            errors.append(f"timeline {field} does not match timestamps_sec")

    gaps = timeline.get("gaps")
    if not isinstance(gaps, list):
        errors.append("timeline gaps must be an array")
        return timestamps, errors
    previous_before_idx = -1
    declared_boundaries: list[tuple[int, int]] = []
    for gap_index, gap in enumerate(gaps, 1):
        if not isinstance(gap, dict):
            errors.append(f"timeline gap {gap_index} must be an object")
            continue
        before_idx = gap.get("before_frame_idx")
        after_idx = gap.get("after_frame_idx")
        if (
            isinstance(before_idx, bool)
            or isinstance(after_idx, bool)
            or not isinstance(before_idx, int)
            or not isinstance(after_idx, int)
            or before_idx < 0
            or after_idx != before_idx + 1
            or after_idx >= len(timestamps)
        ):
            errors.append(
                f"timeline gap {gap_index} has invalid adjacent frame indices"
            )
            continue
        declared_boundaries.append((before_idx, after_idx))
        if before_idx <= previous_before_idx:
            errors.append("timeline gaps must be frame sorted")
        previous_before_idx = before_idx
        expected_before = timestamps[before_idx]
        expected_after = timestamps[after_idx]
        expected_delta = expected_after - expected_before
        for field, expected in (
            ("before_time_sec", expected_before),
            ("after_time_sec", expected_after),
            ("delta_sec", expected_delta),
        ):
            value = gap.get(field)
            if not finite_number(value) or abs(float(value) - expected) > 5e-10:
                errors.append(
                    f"timeline gap {gap_index} {field} does not match timestamps"
                )
    if source_fps is not None:
        threshold_sec = 1.5 / source_fps
        detected_boundaries = [
            (index, index + 1)
            for index, (left, right) in enumerate(
                zip(timestamps, timestamps[1:])
            )
            if right - left > threshold_sec
        ]
        if declared_boundaries != detected_boundaries:
            errors.append(
                "timeline gaps do not match timestamp discontinuities"
            )
    return timestamps, errors


def validate(
    *,
    records: list[dict[str, Any]],
    schema: dict[str, Any],
    timeline: dict[str, Any],
    tool_ids: set[str],
    case_id: str,
    stream_kind: str,
) -> list[str]:
    timestamps, timeline_errors = validate_timeline(
        timeline,
        case_id=case_id,
    )
    errors: list[str] = list(timeline_errors)
    validator = jsonschema.Draft202012Validator(
        schema,
        format_checker=jsonschema.FormatChecker(),
    )
    allowed_event_types = STREAM_EVENT_TYPES[stream_kind]
    seen_ids: set[str] = set()
    previous_key: tuple[float, str] | None = None

    for record in records:
        line_number = int(record["_line"])
        clean = {key: value for key, value in record.items() if key != "_line"}
        for violation in sorted(
            validator.iter_errors(clean),
            key=lambda item: list(item.absolute_path),
        ):
            path = ".".join(str(part) for part in violation.absolute_path)
            errors.append(
                f"line {line_number}: schema {path or '<record>'}: "
                f"{violation.message}"
            )

        event_id_value = record.get("event_id")
        event_id = event_id_value if isinstance(event_id_value, str) else ""
        if event_id:
            if event_id in seen_ids:
                errors.append(
                    f"line {line_number}: duplicate event_id {event_id}"
                )
            seen_ids.add(event_id)
        if record.get("case_id") != case_id:
            errors.append(f"line {line_number}: case_id must be {case_id}")
        event_type_value = record.get("event_type")
        event_type = (
            event_type_value
            if isinstance(event_type_value, str)
            else None
        )
        if event_type not in allowed_event_types:
            errors.append(
                f"line {line_number}: {event_type_value} is not allowed "
                f"in {stream_kind} stream"
            )
        if event_type in EVENT_ID_PREFIX and event_id:
            expected_pattern = re.compile(
                rf"^{re.escape(case_id)}-"
                rf"{EVENT_ID_PREFIX[event_type]}[0-9]{{4,}}$"
            )
            if expected_pattern.fullmatch(event_id) is None:
                errors.append(
                    f"line {line_number}: event_id {event_id} does not match "
                    f"case/event type {case_id}/{event_type}"
                )

        frame_idx = record.get("source_frame_idx")
        time_sec = record.get("time_sec")
        valid_frame_idx = (
            not isinstance(frame_idx, bool)
            and isinstance(frame_idx, int)
            and 0 <= frame_idx < len(timestamps)
        )
        valid_time_sec = finite_number(time_sec)
        if valid_frame_idx:
            expected = timestamps[frame_idx]
            if valid_time_sec and abs(float(time_sec) - expected) > 5e-10:
                errors.append(
                    f"line {line_number}: time_sec {time_sec} does not match "
                    f"frame {frame_idx} timestamp {expected}"
                )
        elif frame_idx is not None:
            errors.append(
                f"line {line_number}: source_frame_idx outside timeline"
            )

        if event_type == "tool_transfer":
            tool = record.get("tool")
            if not isinstance(tool, str) or tool not in tool_ids:
                errors.append(
                    f"line {line_number}: unknown canonical tool "
                    f"{tool}"
                )
            if record.get("from") == record.get("to"):
                errors.append(
                    f"line {line_number}: transfer from and to must differ"
                )

        if valid_time_sec and event_id:
            key = (float(time_sec), event_id)
            if previous_key is not None and key < previous_key:
                errors.append(f"line {line_number}: records are not time sorted")
            previous_key = key

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate minimal request/transfer/phase point JSONL."
    )
    parser.add_argument("input", type=Path)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--timeline", type=Path, required=True)
    parser.add_argument("--tools", type=Path, required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument(
        "--stream-kind",
        choices=sorted(STREAM_EVENT_TYPES),
        required=True,
    )
    args = parser.parse_args()

    try:
        schema = load_json_object(args.schema)
        timeline = load_json_object(args.timeline)
        catalog = yaml.safe_load(args.tools.read_text(encoding="utf-8"))
        records = load_jsonl(args.input)
        tool_ids = {str(tool["id"]) for tool in catalog["tools"]}
    except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError) as exc:
        summary = {
            "ok": False,
            "input": str(args.input),
            "record_count": 0,
            "event_type_counts": {},
            "review_status_counts": {},
            "errors": [str(exc)],
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        return 1
    errors = validate(
        records=records,
        schema=schema,
        timeline=timeline,
        tool_ids=tool_ids,
        case_id=args.case_id,
        stream_kind=args.stream_kind,
    )
    summary = {
        "ok": not errors,
        "input": str(args.input),
        "record_count": len(records),
        "event_type_counts": count_string_field(records, "event_type"),
        "review_status_counts": count_string_field(records, "review_status"),
        "errors": errors,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
