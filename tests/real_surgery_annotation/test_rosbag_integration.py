from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

try:
    import rosbag2_py
    from rclpy.serialization import serialize_message
    from std_msgs.msg import String

    ROSBAG_AVAILABLE = True
except ImportError:
    ROSBAG_AVAILABLE = False

from tools.real_surgery_annotation.event_model import sha256_file
from tools.real_surgery_annotation.inject_annotations import inject
from tools.real_surgery_annotation.validate_injected_bag import validate_bag


ROOT = Path(__file__).resolve().parents[2]
SCHEMA = (
    ROOT
    / "annotations/observable_tool_events/schema/"
    "observable_tool_event.v1.schema.json"
)
TOOLS = ROOT / "annotations/observable_tool_events/catalogs/tools.yaml"


@unittest.skipUnless(ROSBAG_AVAILABLE, "ROS 2 Python bindings are not sourced")
class RosbagIntegrationTest(unittest.TestCase):
    def test_injects_events_at_exact_timestamp_without_changing_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            case_dir = root / "fixture"
            output = root / "derived"
            case_dir.mkdir()

            writer = rosbag2_py.SequentialWriter()
            writer.open(
                rosbag2_py.StorageOptions(uri=str(source), storage_id="mcap"),
                rosbag2_py.ConverterOptions("cdr", "cdr"),
            )
            writer.create_topic(
                rosbag2_py.TopicMetadata(
                    id=0,
                    name="/source",
                    type="std_msgs/msg/String",
                    serialization_format="cdr",
                )
            )
            for timestamp_ns in (0, 1_000_000_000, 2_000_000_000):
                writer.write(
                    "/source",
                    serialize_message(String(data=f"source-{timestamp_ns}")),
                    timestamp_ns,
                )
            writer.close()

            source_mcap = next(source.glob("*.mcap"))
            initial = {
                "schema": "taskplanner.observable_tool_event.v1",
                "case_id": "fixture",
                "event_id": "fixture-I0001",
                "event_type": "initial_state",
                "time_sec": 0.0,
                "tool": {
                    "id": "scalpel",
                    "name": "Scalpel",
                    "instance_id": "fixture-tool-001",
                },
                "from": None,
                "to": {"holder": "none", "location": "mayo_stand"},
                "derived_action": "initial_state",
                "source_views": ["cam4"],
                "visibility": "clear",
                "review_status": "confirmed",
                "label_origin": "human_video_review",
                "review": {
                    "reviewer_kind": "human",
                    "reviewer_id": "fixture-reviewer",
                    "reviewed_at": "2026-07-27T10:00:00+09:00",
                },
            }
            pickup = {
                **initial,
                "event_id": "fixture-E0002",
                "event_type": "pickup_from_mayo",
                "time_sec": 1.0,
                "from": {"holder": "none", "location": "mayo_stand"},
                "to": {"holder": "surgeon", "location": "right_hand"},
                "derived_action": "pickup_from_mayo",
            }
            (case_dir / "tool_events.v1.jsonl").write_text(
                "\n".join(
                    json.dumps(item, separators=(",", ":"))
                    for item in (initial, pickup)
                )
                + "\n",
                encoding="utf-8",
            )
            (case_dir / "candidate_events.v1.jsonl").write_text("", encoding="utf-8")
            manifest = {
                "schema": "taskplanner.observable_annotation_manifest.v1",
                "case_id": "fixture",
                "event_schema": "taskplanner.observable_tool_event.v1",
                "event_file": "tool_events.v1.jsonl",
                "candidate_file": "candidate_events.v1.jsonl",
                "schema_sha256": sha256_file(SCHEMA),
                "tool_catalog_sha256": sha256_file(TOOLS),
                "duration_sec": 2.0,
                "source_bag": {
                    "directory": str(source),
                    "mcap_file": source_mcap.name,
                    "mcap_sha256": sha256_file(source_mcap),
                    "metadata_sha256": sha256_file(source / "metadata.yaml"),
                    "message_count": 3,
                    "topic_count": 1,
                },
                "review_status_counts": {
                    "proposed": 0,
                    "confirmed": 2,
                    "ambiguous": 0,
                    "rejected": 0,
                },
                "human_annotation": {
                    "complete": True,
                    "confirmed_event_count": 2,
                },
                "ground_truth_injection": {
                    "included_statuses": ["confirmed", "ambiguous"],
                    "excluded_statuses": ["proposed", "rejected"],
                },
            }
            (case_dir / "annotation_manifest.json").write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )

            source_hash_before = sha256_file(source_mcap)
            inject(
                source_bag_dir=source,
                case_dir=case_dir,
                schema_path=SCHEMA,
                tools_path=TOOLS,
                output_dir=output,
            )
            report = validate_bag(
                source_bag_dir=source,
                derived_bag_dir=output,
                case_dir=case_dir,
            )

            self.assertTrue(report["ok"], report["errors"])
            self.assertEqual(source_hash_before, sha256_file(source_mcap))
            self.assertEqual(
                [0, 1_000_000_000],
                report["new_topics"][
                    "/evaluation/ground_truth/tool_events"
                ]["timestamps_ns"],
            )
            self.assertEqual(6, report["derived"]["message_count"])


if __name__ == "__main__":
    unittest.main()
