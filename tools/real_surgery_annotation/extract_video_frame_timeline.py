#!/usr/bin/env python3
"""Extract an exact frame-index to bag-time mapping for one video topic."""

from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .rosbag_compat import close_reader, read_next_record


def atomic_create_text(path: Path, text: str) -> None:
    """Publish a complete text file without replacing an existing path."""

    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(file_descriptor, 0o644)
        with os.fdopen(
            file_descriptor,
            "w",
            encoding="utf-8",
            newline="",
        ) as stream:
            file_descriptor = -1
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary_path, path)
        except FileExistsError as exc:
            raise FileExistsError(
                f"refusing to overwrite existing output: {path}"
            ) from exc
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        temporary_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Create a frame-index to corrected bag timestamp map without "
            "decoding image payloads."
        )
    )
    parser.add_argument("--source-bag", type=Path, required=True)
    parser.add_argument("--topic", required=True)
    parser.add_argument("--source-fps", type=float, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case-id", required=True)
    args = parser.parse_args()

    if args.output.exists():
        raise SystemExit(f"refusing to overwrite existing output: {args.output}")
    if not math.isfinite(args.source_fps) or args.source_fps <= 0:
        raise SystemExit("--source-fps must be finite and positive")

    import rosbag2_py

    reader = rosbag2_py.SequentialReader()
    reader.open(
        rosbag2_py.StorageOptions(
            uri=str(args.source_bag.resolve()),
            storage_id="mcap",
        ),
        rosbag2_py.ConverterOptions("cdr", "cdr"),
    )
    reader.set_filter(rosbag2_py.StorageFilter(topics=[args.topic]))

    timestamps_ns: list[int] = []
    while reader.has_next():
        topic, _payload, timestamp_ns = read_next_record(reader)
        if topic == args.topic:
            timestamps_ns.append(timestamp_ns)
    close_reader(reader)

    if not timestamps_ns:
        raise SystemExit(f"no messages found for topic: {args.topic}")
    if any(right <= left for left, right in zip(timestamps_ns, timestamps_ns[1:])):
        raise SystemExit("video timestamps are not strictly increasing")

    origin_ns = timestamps_ns[0]
    timestamps_sec = [
        round((timestamp_ns - origin_ns) / 1_000_000_000, 9)
        for timestamp_ns in timestamps_ns
    ]
    gaps = [
        {
            "before_frame_idx": index,
            "after_frame_idx": index + 1,
            "before_time_sec": timestamps_sec[index],
            "after_time_sec": timestamps_sec[index + 1],
            "delta_sec": round(timestamps_sec[index + 1] - timestamps_sec[index], 9),
        }
        for index in range(len(timestamps_sec) - 1)
        if timestamps_sec[index + 1] - timestamps_sec[index]
        > (1.5 / args.source_fps)
    ]

    payload = {
        "schema": "taskplanner.video_frame_timeline.v1",
        "case_id": args.case_id,
        "source_bag": str(args.source_bag.resolve()),
        "topic": args.topic,
        "timeline_origin": "first_topic_message",
        "source_fps": args.source_fps,
        "frame_count": len(timestamps_sec),
        "start_sec": timestamps_sec[0],
        "end_sec": timestamps_sec[-1],
        "gaps": gaps,
        "timestamps_sec": timestamps_sec,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    serialized = (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    )
    try:
        atomic_create_text(args.output, serialized)
    except FileExistsError as exc:
        raise SystemExit(str(exc)) from exc
    print(
        json.dumps(
            {
                "output": str(args.output),
                "frame_count": len(timestamps_sec),
                "end_sec": timestamps_sec[-1],
                "gap_count": len(gaps),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
