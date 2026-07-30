#!/usr/bin/env python3
"""Extract the public transcript topic as a causal review-context track.

The output deliberately contains no frame index and no inferred tool or
semantic label.  Source utterance start is preserved for display, while
``available_sec`` prevents complete text from becoming runtime-visible before
the utterance ended.  It is public runtime context, not evaluation ground
truth.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .extract_video_frame_timeline import atomic_create_text
from .rosbag_compat import close_reader, read_next_record


VOICE_SCHEMA = "taskplanner.observable_voice_point.v2"
VOICE_EVENT_TYPE = "voice_utterance"
TRANSCRIPT_TOPIC = "/surgery/transcript"
TRANSCRIPT_TYPE = "std_msgs/msg/String"
SOURCE_AUTHORITY = "public_runtime_transcript"
SCORING_ROLE = "context_only_not_ground_truth"
AVAILABILITY_POLICY = "not_before_utterance_end"
CASE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


class VoiceTimelineError(Exception):
    """The source transcript or create-only publication contract is invalid."""


@dataclass(frozen=True)
class TranscriptRecord:
    """One serialized transcript message and its MCAP record timestamp."""

    timestamp_ns: int
    raw_text: str


def _reject_nonstandard_json_constant(value: str) -> None:
    raise ValueError(f"non-standard numeric constant: {value}")


def _finite_number(value: Any, field: str, message_index: int) -> int | float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise VoiceTimelineError(
            f"transcript message {message_index}: {field} must be a finite number"
        )
    if value < 0:
        raise VoiceTimelineError(
            f"transcript message {message_index}: {field} must be non-negative"
        )
    return value


def parse_transcript_payload(
    record: TranscriptRecord,
    *,
    message_index: int,
) -> dict[str, Any]:
    """Parse one source message without normalizing its text or timestamps."""

    try:
        payload = json.loads(
            record.raw_text,
            parse_constant=_reject_nonstandard_json_constant,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise VoiceTimelineError(
            f"transcript message {message_index}: payload is not valid JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise VoiceTimelineError(
            f"transcript message {message_index}: payload must be a JSON object"
        )

    missing = [
        field
        for field in ("start_sec", "end_sec", "text", "source_wav")
        if field not in payload
    ]
    if missing:
        raise VoiceTimelineError(
            f"transcript message {message_index}: missing fields: "
            + ", ".join(missing)
        )

    start_sec = _finite_number(
        payload["start_sec"],
        "start_sec",
        message_index,
    )
    end_sec = _finite_number(payload["end_sec"], "end_sec", message_index)
    if end_sec < start_sec:
        raise VoiceTimelineError(
            f"transcript message {message_index}: end_sec precedes start_sec"
        )

    text = payload["text"]
    if not isinstance(text, str) or not text:
        raise VoiceTimelineError(
            f"transcript message {message_index}: text must be a non-empty string"
        )
    source_wav = payload["source_wav"]
    if not isinstance(source_wav, str) or not source_wav:
        raise VoiceTimelineError(
            "transcript message "
            f"{message_index}: source_wav must be a non-empty string"
        )

    source_record_sec = record.timestamp_ns / 1_000_000_000
    if source_record_sec + 5e-10 < start_sec:
        raise VoiceTimelineError(
            f"transcript message {message_index}: MCAP record timestamp "
            f"{record.timestamp_ns} ns precedes start_sec {start_sec}"
        )
    explicit_available_sec = payload.get("available_sec")
    if explicit_available_sec is None:
        available_sec = max(end_sec, source_record_sec)
    else:
        available_sec = _finite_number(
            explicit_available_sec,
            "available_sec",
            message_index,
        )
        if available_sec < end_sec:
            raise VoiceTimelineError(
                f"transcript message {message_index}: available_sec "
                "precedes end_sec"
            )
        available_sec = max(available_sec, source_record_sec)
    return {
        "start_sec": start_sec,
        "end_sec": end_sec,
        "available_sec": available_sec,
        "text": text,
        "source_wav": source_wav,
    }


def build_voice_events(
    *,
    case_id: str,
    records: Iterable[TranscriptRecord],
    expected_count: int | None = None,
    topic: str = TRANSCRIPT_TOPIC,
) -> list[dict[str, Any]]:
    """Convert source-order transcript records to immutable point events."""

    if not CASE_ID_PATTERN.fullmatch(case_id):
        raise VoiceTimelineError(
            "case_id must contain only alphanumeric, underscore, or hyphen characters"
        )
    if topic != TRANSCRIPT_TOPIC:
        raise VoiceTimelineError(
            f"source topic must be {TRANSCRIPT_TOPIC}, got {topic}"
        )
    if expected_count is not None and expected_count < 1:
        raise VoiceTimelineError("expected_count must be positive")

    materialized = list(records)
    if expected_count is not None and len(materialized) != expected_count:
        raise VoiceTimelineError(
            f"expected {expected_count} transcript messages, "
            f"found {len(materialized)}"
        )
    if not materialized:
        raise VoiceTimelineError("source transcript topic has no messages")

    events: list[dict[str, Any]] = []
    previous_timestamp_ns: int | None = None
    for message_index, record in enumerate(materialized, 1):
        if (
            isinstance(record.timestamp_ns, bool)
            or not isinstance(record.timestamp_ns, int)
            or record.timestamp_ns < 0
        ):
            raise VoiceTimelineError(
                f"transcript message {message_index}: "
                "record timestamp must be a non-negative integer"
            )
        if (
            previous_timestamp_ns is not None
            and record.timestamp_ns <= previous_timestamp_ns
        ):
            raise VoiceTimelineError(
                "transcript record timestamps must be strictly increasing; "
                f"message {message_index} is out of order"
            )
        payload = parse_transcript_payload(
            record,
            message_index=message_index,
        )
        events.append(
            {
                "schema": VOICE_SCHEMA,
                "case_id": case_id,
                "event_id": f"{case_id}-V{message_index:04d}",
                "event_type": VOICE_EVENT_TYPE,
                "time_sec": payload["start_sec"],
                "end_sec": payload["end_sec"],
                "available_sec": payload["available_sec"],
                "text": payload["text"],
                "source_topic": topic,
                "source_record_timestamp_ns": record.timestamp_ns,
                "source_message_index": message_index,
                "source_wav": payload["source_wav"],
                "source_authority": SOURCE_AUTHORITY,
                "scoring_role": SCORING_ROLE,
                "availability_policy": AVAILABILITY_POLICY,
            }
        )
        previous_timestamp_ns = record.timestamp_ns
    return events


def read_transcript_records(
    source_bag: Path,
    *,
    topic: str = TRANSCRIPT_TOPIC,
) -> list[TranscriptRecord]:
    """Read every std_msgs/String record from the selected source topic."""

    try:
        import rosbag2_py
        from rclpy.serialization import deserialize_message
        from std_msgs.msg import String
    except ImportError as exc:
        raise VoiceTimelineError(
            "ROS 2 Python packages are unavailable; source the ROS environment"
        ) from exc

    reader = rosbag2_py.SequentialReader()
    try:
        reader.open(
            rosbag2_py.StorageOptions(
                uri=str(source_bag.resolve()),
                storage_id="mcap",
            ),
            rosbag2_py.ConverterOptions("cdr", "cdr"),
        )
        topic_types = {
            item.name: item.type
            for item in reader.get_all_topics_and_types()
        }
        actual_type = topic_types.get(topic)
        if actual_type != TRANSCRIPT_TYPE:
            raise VoiceTimelineError(
                f"{topic} type is {actual_type!r}; expected {TRANSCRIPT_TYPE!r}"
            )
        reader.set_filter(rosbag2_py.StorageFilter(topics=[topic]))

        records: list[TranscriptRecord] = []
        while reader.has_next():
            record_topic, payload, timestamp_ns = read_next_record(reader)
            if record_topic != topic:
                continue
            message = deserialize_message(payload, String)
            records.append(
                TranscriptRecord(
                    timestamp_ns=timestamp_ns,
                    raw_text=str(message.data),
                )
            )
        return records
    finally:
        close_reader(reader)


def serialize_jsonl(events: Iterable[dict[str, Any]]) -> str:
    """Serialize deterministically as one compact JSON object per line."""

    return "".join(
        json.dumps(
            event,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
        for event in events
    )


def extract_voice_timeline(
    *,
    source_bag: Path,
    output: Path,
    case_id: str,
    expected_count: int | None = 22,
) -> list[dict[str, Any]]:
    """Read, validate, and atomically publish a create-only JSONL track."""

    if output.exists():
        raise VoiceTimelineError(
            f"refusing to overwrite existing output: {output}"
        )
    records = read_transcript_records(source_bag)
    events = build_voice_events(
        case_id=case_id,
        records=records,
        expected_count=expected_count,
    )
    try:
        atomic_create_text(output, serialize_jsonl(events))
    except FileExistsError as exc:
        raise VoiceTimelineError(str(exc)) from exc
    return events


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Create a causal, read-only /surgery/transcript context timeline "
            "without frame alignment or semantic inference."
        )
    )
    parser.add_argument("--source-bag", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument(
        "--expected-count",
        type=int,
        default=22,
        help="fail unless this many transcript records are present (default: 22)",
    )
    args = parser.parse_args()

    try:
        events = extract_voice_timeline(
            source_bag=args.source_bag,
            output=args.output,
            case_id=args.case_id,
            expected_count=args.expected_count,
        )
    except VoiceTimelineError as exc:
        raise SystemExit(str(exc)) from exc
    print(
        json.dumps(
            {
                "output": str(args.output),
                "event_count": len(events),
                "source_topic": TRANSCRIPT_TOPIC,
                "source_authority": SOURCE_AUTHORITY,
                "scoring_role": SCORING_ROLE,
                "schema": VOICE_SCHEMA,
                "availability_policy": AVAILABILITY_POLICY,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
