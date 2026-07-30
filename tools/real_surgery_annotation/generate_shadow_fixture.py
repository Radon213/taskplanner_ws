#!/usr/bin/env python3
"""Generate a short public-evidence MCAP and reference for shadow CI."""

from __future__ import annotations

import argparse
from io import BytesIO
import json
from pathlib import Path
import shutil
from typing import Any

from PIL import Image, ImageDraw
import rosbag2_py
from rclpy.serialization import serialize_message
from sensor_msgs.msg import CompressedImage
from std_msgs.msg import String

try:
    from .shadow_contract import sha256_file, utc_now
except ImportError:  # Support direct execution from this directory.
    from shadow_contract import sha256_file, utc_now


CASE_ID = "shadow_fixture"
IMAGE_TOPIC = "/surgery/cam4/color/image/compressed"
TRANSCRIPT_TOPIC = "/surgery/transcript"


def _jpeg_payload() -> bytes:
    image = Image.new("RGB", (640, 360), color=(6, 10, 14))
    draw = ImageDraw.Draw(image)
    draw.text((205, 165), "PUBLIC CAMERA FIXTURE", fill=(240, 245, 250))
    stream = BytesIO()
    image.save(stream, format="JPEG", quality=85)
    return stream.getvalue()


def _image_message(timestamp_ns: int, data: bytes) -> CompressedImage:
    message = CompressedImage()
    message.header.stamp.sec = timestamp_ns // 1_000_000_000
    message.header.stamp.nanosec = timestamp_ns % 1_000_000_000
    message.header.frame_id = "shadow_fixture_cam4"
    message.format = "jpeg"
    message.data = data
    return message


def _event(
    *,
    event_id: str,
    event_type: str,
    time_sec: float,
    source: dict[str, str] | None,
    target: dict[str, str],
    derived_action: str,
) -> dict[str, Any]:
    return {
        "schema": "taskplanner.observable_tool_event.v1",
        "case_id": CASE_ID,
        "event_id": event_id,
        "event_type": event_type,
        "time_sec": time_sec,
        "tool": {
            "id": "scalpel",
            "name": "Scalpel",
            "instance_id": f"{CASE_ID}-tool-scalpel-01",
        },
        "from": source,
        "to": target,
        "derived_action": derived_action,
        "source_views": ["cam4"],
        "visibility": "clear",
        "review_status": "confirmed",
        "label_origin": "human_video_review",
        "review": {
            "reviewer_kind": "human",
            "reviewer_id": "shadow-fixture",
            "reviewed_at": utc_now(),
            "notes": "Synthetic deterministic integration fixture.",
        },
    }


def generate_fixture(output_root: Path, catalog_source: Path) -> dict[str, Path]:
    if output_root.exists():
        raise FileExistsError(output_root)
    bag_dir = output_root / "bag"
    annotation_root = output_root / "annotations"
    case_dir = annotation_root / "cases" / CASE_ID
    catalog_dir = annotation_root / "catalogs"
    case_dir.mkdir(parents=True)
    catalog_dir.mkdir(parents=True)
    shutil.copy2(catalog_source, catalog_dir / "tools.yaml")
    schema_source = (
        Path(__file__).resolve().parents[2]
        / "annotations/observable_tool_events/schema/"
        "observable_tool_event.v1.schema.json"
    )

    writer = rosbag2_py.SequentialWriter()
    writer.open(
        rosbag2_py.StorageOptions(
            uri=str(bag_dir),
            storage_id="mcap",
            custom_data={
                "case_id": CASE_ID,
                "schema_version": "taskplanner_shadow_fixture_v1",
                "timeline_origin": "synthetic_t0",
            },
        ),
        rosbag2_py.ConverterOptions("cdr", "cdr"),
    )
    writer.create_topic(
        rosbag2_py.TopicMetadata(
            id=0,
            name=IMAGE_TOPIC,
            type="sensor_msgs/msg/CompressedImage",
            serialization_format="cdr",
        )
    )
    writer.create_topic(
        rosbag2_py.TopicMetadata(
            id=1,
            name=TRANSCRIPT_TOPIC,
            type="std_msgs/msg/String",
            serialization_format="cdr",
        )
    )
    jpeg = _jpeg_payload()
    records: list[tuple[int, str, Any]] = [
        (
            index * 200_000_000,
            IMAGE_TOPIC,
            _image_message(index * 200_000_000, jpeg),
        )
        for index in range(41)
    ]
    records.append(
        (
            1_000_000_000,
            TRANSCRIPT_TOPIC,
            String(
                data=json.dumps(
                    {
                        "start_sec": 0.8,
                        "end_sec": 1.0,
                        "text": "#15 scalpel please",
                        "source_wav": "synthetic",
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
            ),
        )
    )
    for timestamp_ns, topic, message in sorted(
        records,
        key=lambda item: (item[0], item[1]),
    ):
        writer.write(
            topic,
            serialize_message(message),
            timestamp_ns,
        )
    writer.close()

    bag_files = list(bag_dir.glob("*.mcap"))
    if len(bag_files) != 1:
        raise RuntimeError(f"expected one generated MCAP, found {len(bag_files)}")
    bag_file = bag_files[0]
    events = [
        _event(
            event_id=f"{CASE_ID}-I0001",
            event_type="initial_state",
            time_sec=0.0,
            source=None,
            target={"holder": "scrub_nurse", "location": "instrument_table"},
            derived_action="initial_state",
        ),
        _event(
            event_id=f"{CASE_ID}-E0002",
            event_type="tool_transfer",
            time_sec=4.5,
            source={"holder": "scrub_nurse", "location": "instrument_table"},
            target={"holder": "scrub_nurse", "location": "hand_unspecified"},
            derived_action="relocate",
        ),
        _event(
            event_id=f"{CASE_ID}-E0003",
            event_type="tool_transfer",
            time_sec=5.0,
            source={"holder": "scrub_nurse", "location": "hand_unspecified"},
            target={"holder": "surgeon", "location": "hand_unspecified"},
            derived_action="handover",
        ),
    ]
    event_path = case_dir / "tool_events.final.v1.jsonl"
    event_path.write_text(
        "".join(
            json.dumps(event, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            + "\n"
            for event in events
        ),
        encoding="utf-8",
    )
    candidate_path = case_dir / "candidate_events.v1.jsonl"
    candidate_path.write_text("", encoding="utf-8")
    manifest = {
        "schema": "taskplanner.observable_annotation_manifest.v1",
        "case_id": CASE_ID,
        "duration_sec": 8.0,
        "event_schema": "taskplanner.observable_tool_event.v1",
        "event_file": event_path.name,
        "candidate_file": candidate_path.name,
        "schema_path": str(schema_source),
        "schema_sha256": sha256_file(schema_source),
        "tool_catalog_path": "../../catalogs/tools.yaml",
        "tool_catalog_sha256": sha256_file(catalog_dir / "tools.yaml"),
        "source_bag": {
            "directory": str(bag_dir.resolve()),
            "mcap_file": bag_file.name,
            "mcap_sha256": sha256_file(bag_file),
            "metadata_sha256": sha256_file(bag_dir / "metadata.yaml"),
            "message_count": 42,
            "topic_count": 2,
        },
        "annotation_adjudication": {
            "authority": "human",
            "complete": True,
            "confirmed_event_count": 3,
            "confirmed_origin_counts": {"human_video_review": 3},
            "confirmed_reviewer_kind_counts": {"human": 3},
        },
        "review_status_counts": {
            "confirmed": 3,
            "ambiguous": 0,
            "proposed": 0,
            "rejected": 0,
        },
        "ground_truth_injection": {
            "consumer_policy": "evaluation_only_never_vlm_reducer_bt",
            "event_topic": "/evaluation/ground_truth/tool_events",
            "excluded_statuses": ["proposed", "rejected"],
            "included_statuses": ["confirmed", "ambiguous"],
            "manifest_topic": "/evaluation/ground_truth/annotation_manifest",
        },
    }
    (case_dir / "annotation_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    replay_response = {
        "v": "4",
        "phase": [["P01", 0.96]],
        "tool": [["T01", 0.94]],
        "intent": ["handover", "T01", 0.95],
        "mayo": [],
        "mayo_retrieve": ["", 0.0],
        "u": 0.04,
        "sum": "public speech requests the scalpel",
        "bed_robot_arm_group": None,
    }
    replay_path = output_root / "replay_response.v4.json"
    replay_path.write_text(
        json.dumps(replay_response, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "root": output_root,
        "bag_dir": bag_dir,
        "case_dir": case_dir,
        "replay_response": replay_path,
    }


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--tool-catalog",
        type=Path,
        default=repo_root / "annotations/observable_tool_events/catalogs/tools.yaml",
    )
    args = parser.parse_args()
    paths = generate_fixture(
        args.output.resolve(),
        args.tool_catalog.resolve(),
    )
    print(json.dumps({key: str(value) for key, value in paths.items()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
