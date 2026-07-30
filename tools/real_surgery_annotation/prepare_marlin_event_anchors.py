#!/usr/bin/env python3
"""Create transcript-guided and full-video Marlin search anchors.

The generated records are proposal-search inputs only. They do not assert that
an interaction happened, and they never become ground truth without later
video review.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

from rclpy.serialization import deserialize_message
from rosidl_runtime_py.utilities import get_message

from .extract_video_frame_timeline import atomic_create_text
from .rosbag_compat import close_reader, read_next_record


CAM4_TOPIC = "/surgery/cam4/color/image/compressed"
TRANSCRIPT_TOPIC = "/surgery/transcript"

TOOL_PATTERNS = (
    ("adson", "adson_forceps"),
    ("bovie", "bovie"),
    ("bipolar", "bipolar_forceps"),
    ("bipol", "bipolar_forceps"),
    ("mosquito", "mosquito_forceps"),
    ("army", "army_navy_retractor"),
    ("air suction", "yankauer_suction"),
    ("suction", "yankauer_suction"),
)


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def serialize_json(value: dict[str, Any]) -> str:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    )


def tool_mentions(text: str) -> list[tuple[int, str]]:
    lowered = text.lower()
    mentions: list[tuple[int, str]] = []
    occupied: list[tuple[int, int]] = []
    for pattern, tool_id in TOOL_PATTERNS:
        start = 0
        while True:
            index = lowered.find(pattern, start)
            if index < 0:
                break
            end = index + len(pattern)
            if not any(index < right and end > left for left, right in occupied):
                mentions.append((index, tool_id))
                occupied.append((index, end))
            start = index + 1
    mentions.sort()
    return mentions


def transcript_semantic(text: str) -> str:
    lowered = text.lower()
    if "닦" in text:
        return "tool_maintenance_context"
    if "빼" in text:
        return "tool_return_or_remove_context"
    if "아니" in text:
        return "corrected_tool_request"
    if "받고" in text or "받아" in text:
        return "tool_request"
    return "tool_request"


def dense_times(start_sec: float, end_sec: float) -> list[float]:
    """Cover long transcript segments without pretending to have word timing."""

    if end_sec <= start_sec + 5.0:
        return [start_sec]
    times = [start_sec]
    cursor = start_sec + 4.0
    while cursor < end_sec - 1.5:
        times.append(cursor)
        cursor += 4.0
    if end_sec - times[-1] > 2.0:
        times.append(end_sec)
    return times


def read_transcripts(source_bag: Path) -> tuple[int, list[dict[str, Any]]]:
    import rosbag2_py

    reader = rosbag2_py.SequentialReader()
    reader.open(
        rosbag2_py.StorageOptions(
            uri=str(source_bag.resolve()),
            storage_id="mcap",
        ),
        rosbag2_py.ConverterOptions("cdr", "cdr"),
    )
    reader.set_filter(
        rosbag2_py.StorageFilter(topics=[CAM4_TOPIC, TRANSCRIPT_TOPIC])
    )
    string_type = get_message("std_msgs/msg/String")
    cam4_origin_ns: int | None = None
    transcripts: list[dict[str, Any]] = []
    while reader.has_next():
        topic, payload, timestamp_ns = read_next_record(reader)
        if topic == CAM4_TOPIC and cam4_origin_ns is None:
            cam4_origin_ns = timestamp_ns
        elif topic == TRANSCRIPT_TOPIC:
            raw = deserialize_message(payload, string_type).data
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                value = {"text": raw}
            if not isinstance(value, dict):
                value = {"text": str(value)}
            value["_record_timestamp_ns"] = timestamp_ns
            transcripts.append(value)
    close_reader(reader)
    if cam4_origin_ns is None:
        raise RuntimeError(f"no CAM4 messages found in {source_bag}")
    return cam4_origin_ns, transcripts


def build_transcript_anchors(
    *,
    case_id: str,
    source_bag: Path,
    timeline_end_sec: float,
) -> dict[str, Any]:
    cam4_origin_ns, transcripts = read_transcripts(source_bag)
    anchors: list[dict[str, Any]] = []
    for transcript in transcripts:
        text = str(transcript.get("text", "")).strip()
        mentions = tool_mentions(text)
        lowered = text.lower()
        include_without_named_tool = (
            "받고" in text
            or "받아" in text
            or lowered.strip() in {"받고", "받아"}
        )
        if not mentions and not include_without_named_tool:
            continue

        start_sec = (
            int(transcript["_record_timestamp_ns"]) - cam4_origin_ns
        ) / 1_000_000_000
        try:
            transcript_duration = max(
                0.0,
                float(transcript.get("end_sec", 0.0))
                - float(transcript.get("start_sec", 0.0)),
            )
        except (TypeError, ValueError):
            transcript_duration = 0.0
        end_sec = start_sec + transcript_duration
        if start_sec > timeline_end_sec:
            continue
        end_sec = min(end_sec, timeline_end_sec)

        tool_hint = mentions[-1][1] if mentions else None
        for search_sec in dense_times(start_sec, end_sec):
            anchor_number = len(anchors) + 1
            anchors.append(
                {
                    "anchor_id": f"{case_id}-A{anchor_number:03d}",
                    "end_sec": round(end_sec, 9),
                    "semantic": transcript_semantic(text),
                    "text": text,
                    "time_sec": round(search_sec, 9),
                    "tool_hint": tool_hint,
                    "transcript_start_sec": round(start_sec, 9),
                    "transcript_tool_mentions": [
                        tool_id for _, tool_id in mentions
                    ],
                }
            )
    if not anchors:
        raise RuntimeError(f"no event-search transcript anchors found for {case_id}")
    return {
        "schema": "taskplanner.marlin2_search_anchors.v1",
        "case_id": case_id,
        "authority": "public_transcript_search_only_not_ground_truth",
        "notes": [
            "These records define Marlin video-search windows and tool hints only.",
            "A transcript mention is not evidence that a visible gesture or transfer occurred.",
            "Long transcript spans receive multiple search points because word timing is unavailable.",
            "Corrections, removals, and maintenance speech require independent visual review.",
        ],
        "anchors": anchors,
    }


def build_scan_anchors(
    *,
    case_id: str,
    timestamps: list[float],
    source_fps: float,
) -> dict[str, Any]:
    source_duration_sec = len(timestamps) / source_fps
    centers: list[float] = []
    center = min(7.0, source_duration_sec / 2.0)
    while center <= max(0.0, source_duration_sec - 7.0):
        centers.append(center)
        center += 12.0
    final_center = max(0.0, source_duration_sec - 6.0)
    if not centers or final_center - centers[-1] > 1.0:
        centers.append(final_center)

    anchors: list[dict[str, Any]] = []
    for index, source_time_sec in enumerate(centers, 1):
        frame_idx = min(
            len(timestamps) - 1,
            max(0, round(source_time_sec * source_fps)),
        )
        anchors.append(
            {
                "anchor_id": f"{case_id}-S{index:03d}",
                "semantic": "video_scan",
                "text": (
                    "source-time window centered at "
                    f"{source_time_sec:.3f} s"
                ),
                "time_sec": timestamps[frame_idx],
            }
        )
    return {
        "schema": "taskplanner.marlin2_search_anchors.v1",
        "case_id": case_id,
        "authority": "video_search_windows_only_not_ground_truth",
        "notes": [
            "The windows cover the full encoded CAM4 source with adjacent overlap.",
            "They are used only to search for transcript-independent tool movements.",
        ],
        "anchors": anchors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create proposal-only Marlin event search anchors."
    )
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--source-bag", type=Path, required=True)
    parser.add_argument("--timeline", type=Path, required=True)
    parser.add_argument("--transcript-output", type=Path, required=True)
    parser.add_argument("--scan-output", type=Path, required=True)
    args = parser.parse_args()

    for output in (args.transcript_output, args.scan_output):
        if output.exists():
            raise SystemExit(f"refusing to overwrite existing output: {output}")
    timeline = load_json(args.timeline)
    if timeline.get("case_id") != args.case_id:
        raise SystemExit("timeline case_id does not match")
    try:
        timestamps = [float(value) for value in timeline["timestamps_sec"]]
        source_fps = float(timeline["source_fps"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SystemExit(f"invalid timeline: {exc}") from exc
    if (
        not timestamps
        or not math.isfinite(source_fps)
        or source_fps <= 0
        or any(not math.isfinite(value) for value in timestamps)
    ):
        raise SystemExit("invalid timeline numeric data")

    transcript_doc = build_transcript_anchors(
        case_id=args.case_id,
        source_bag=args.source_bag,
        timeline_end_sec=timestamps[-1],
    )
    scan_doc = build_scan_anchors(
        case_id=args.case_id,
        timestamps=timestamps,
        source_fps=source_fps,
    )
    atomic_create_text(args.transcript_output, serialize_json(transcript_doc))
    try:
        atomic_create_text(args.scan_output, serialize_json(scan_doc))
    except Exception:
        args.transcript_output.unlink(missing_ok=True)
        raise
    print(
        json.dumps(
            {
                "case_id": args.case_id,
                "transcript_anchor_count": len(transcript_doc["anchors"]),
                "scan_anchor_count": len(scan_doc["anchors"]),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
