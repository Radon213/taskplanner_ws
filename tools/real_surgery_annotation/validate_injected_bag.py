#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from . import EVENT_TOPIC, MANIFEST_TOPIC
from .event_model import canonical_json, load_jsonl, records_for_injection, sha256_file
from .rosbag_compat import close_reader, read_next_record


def _open_reader(path: Path):
    import rosbag2_py

    reader = rosbag2_py.SequentialReader()
    reader.open(
        rosbag2_py.StorageOptions(uri=str(path), storage_id="mcap"),
        rosbag2_py.ConverterOptions("cdr", "cdr"),
    )
    return reader


def _deserialize_string(payload: bytes) -> str:
    from rclpy.serialization import deserialize_message
    from std_msgs.msg import String

    return deserialize_message(payload, String).data


def validate_bag(
    *,
    source_bag_dir: Path,
    derived_bag_dir: Path,
    case_dir: Path,
) -> dict[str, Any]:
    errors: list[str] = []
    manifest = json.loads(
        (case_dir / "annotation_manifest.json").read_text(encoding="utf-8")
    )
    source_mcap = source_bag_dir / manifest["source_bag"]["mcap_file"]
    source_checksums = {
        "mcap": sha256_file(source_mcap),
        "metadata": sha256_file(source_bag_dir / "metadata.yaml"),
    }
    expected_source_checksums = {
        "mcap": manifest["source_bag"]["mcap_sha256"],
        "metadata": manifest["source_bag"]["metadata_sha256"],
    }
    if source_checksums != expected_source_checksums:
        errors.append("source checksum does not match immutable manifest baseline")

    allowed = set(manifest["ground_truth_injection"]["included_statuses"])
    expected_events = records_for_injection(
        load_jsonl(case_dir / manifest["event_file"]), allowed
    )
    expected_event_json = [canonical_json(event) for event in expected_events]

    source_reader = _open_reader(source_bag_dir)
    derived_reader = _open_reader(derived_bag_dir)
    source_topics = {
        item.name: item.type for item in source_reader.get_all_topics_and_types()
    }
    derived_topics = {
        item.name: item.type for item in derived_reader.get_all_topics_and_types()
    }
    if len(source_topics) != int(manifest["source_bag"]["topic_count"]):
        errors.append("source topic count differs from manifest")
    expected_derived_topics = dict(source_topics)
    expected_derived_topics[MANIFEST_TOPIC] = "std_msgs/msg/String"
    expected_derived_topics[EVENT_TOPIC] = "std_msgs/msg/String"
    if derived_topics != expected_derived_topics:
        errors.append("derived topic metadata differs from source plus two GT topics")

    source_counts: Counter[str] = Counter()
    derived_counts: Counter[str] = Counter()
    ranges: dict[str, list[int]] = defaultdict(list)
    manifest_payloads: list[dict[str, Any]] = []
    actual_event_json: list[str] = []
    source_exhausted_early = False
    payload_mismatch_count = 0
    topic_or_time_mismatch_count = 0
    order_error_count = 0
    previous_timestamp = -1
    previous_priority = -1

    while derived_reader.has_next():
        topic, payload, timestamp_ns = read_next_record(derived_reader)
        derived_counts[topic] += 1
        ranges[topic].append(timestamp_ns)
        if timestamp_ns < previous_timestamp:
            order_error_count += 1
        if timestamp_ns != previous_timestamp:
            previous_priority = -1

        if topic == MANIFEST_TOPIC:
            priority = 0
            try:
                manifest_payloads.append(json.loads(_deserialize_string(payload)))
            except Exception as exc:
                errors.append(f"invalid manifest JSON payload: {exc}")
        elif topic == EVENT_TOPIC:
            try:
                event_text = _deserialize_string(payload)
                event = json.loads(event_text)
                actual_event_json.append(canonical_json(event))
                priority = 1 if event["event_type"] == "initial_state" else 3
            except Exception as exc:
                priority = 3
                errors.append(f"invalid event JSON payload: {exc}")
        else:
            priority = 2
            if not source_reader.has_next():
                source_exhausted_early = True
            else:
                source_topic, source_payload, source_timestamp_ns = read_next_record(
                    source_reader
                )
                source_counts[source_topic] += 1
                if (source_topic, source_timestamp_ns) != (topic, timestamp_ns):
                    topic_or_time_mismatch_count += 1
                if source_payload != payload:
                    payload_mismatch_count += 1

        if timestamp_ns == previous_timestamp and priority < previous_priority:
            order_error_count += 1
        previous_timestamp = timestamp_ns
        previous_priority = priority

    remaining_source_messages = 0
    while source_reader.has_next():
        source_topic, _, _ = read_next_record(source_reader)
        source_counts[source_topic] += 1
        remaining_source_messages += 1
    close_reader(source_reader)
    close_reader(derived_reader)

    if source_exhausted_early or remaining_source_messages:
        errors.append(
            "derived bag does not contain exactly one copy of every source message"
        )
    if topic_or_time_mismatch_count:
        errors.append(
            f"{topic_or_time_mismatch_count} original topic/timestamp records differ"
        )
    if payload_mismatch_count:
        errors.append(f"{payload_mismatch_count} original payloads differ")
    if order_error_count:
        errors.append(f"{order_error_count} replay-order violations")
    if len(manifest_payloads) != 1:
        errors.append(f"expected one annotation manifest, got {len(manifest_payloads)}")
    elif not manifest_payloads[0].get("injection", {}).get("evaluation_only"):
        errors.append("injected manifest does not enforce evaluation_only")
    if actual_event_json != expected_event_json:
        errors.append("ground-truth event payloads differ from eligible JSONL records")
    if ranges.get(MANIFEST_TOPIC) != [0]:
        errors.append("annotation manifest timestamp must be exactly 0")
    expected_event_timestamps = [
        round(float(event["time_sec"]) * 1_000_000_000)
        for event in expected_events
    ]
    if ranges.get(EVENT_TOPIC, []) != expected_event_timestamps:
        errors.append("ground-truth event timestamps differ from JSONL time_sec")

    info = subprocess.run(
        ["ros2", "bag", "info", "-s", "mcap", str(derived_bag_dir)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if info.returncode != 0:
        errors.append(f"ros2 bag info failed with exit code {info.returncode}")

    source_message_count = sum(source_counts.values())
    derived_message_count = sum(derived_counts.values())
    expected_derived_count = source_message_count + 1 + len(expected_events)
    if source_message_count != int(manifest["source_bag"]["message_count"]):
        errors.append("source message count differs from manifest")
    if derived_message_count != expected_derived_count:
        errors.append(
            f"derived count {derived_message_count} != expected {expected_derived_count}"
        )

    return {
        "schema": "taskplanner.observable_gt_bag_validation.v1",
        "case_id": manifest["case_id"],
        "ok": not errors,
        "errors": errors,
        "source": {
            "topic_count": len(source_topics),
            "message_count": source_message_count,
            "checksums": source_checksums,
        },
        "derived": {
            "topic_count": len(derived_topics),
            "message_count": derived_message_count,
            "original_payloads_byte_identical": payload_mismatch_count == 0,
            "original_topic_timestamps_identical": (
                topic_or_time_mismatch_count == 0
            ),
            "original_replay_order_preserved": (
                not source_exhausted_early and remaining_source_messages == 0
            ),
            "global_timestamp_and_tie_order_valid": order_error_count == 0,
        },
        "new_topics": {
            MANIFEST_TOPIC: {
                "count": derived_counts[MANIFEST_TOPIC],
                "timestamps_ns": ranges.get(MANIFEST_TOPIC, []),
            },
            EVENT_TOPIC: {
                "count": derived_counts[EVENT_TOPIC],
                "timestamps_ns": ranges.get(EVENT_TOPIC, []),
                "included_statuses": sorted(allowed),
            },
        },
        "source_topic_counts": dict(sorted(source_counts.items())),
        "ros2_bag_info": {
            "exit_code": info.returncode,
            "output": info.stdout,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-bag", type=Path, required=True)
    parser.add_argument("--derived-bag", type=Path, required=True)
    parser.add_argument("--case-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = validate_bag(
        source_bag_dir=args.source_bag,
        derived_bag_dir=args.derived_bag,
        case_dir=args.case_dir,
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
