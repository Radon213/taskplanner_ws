from __future__ import annotations

import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tools.real_surgery_annotation.import_rfdetr_overlays import (
    AUTHORITY,
    INDEX_SCHEMA,
    OUTPUT_SCHEMA,
    OverlayImportError,
    app_case_id,
    build_overlays,
    even_proxy_content_rect,
    source_case_id,
)


class RFDetrOverlayImportTest(unittest.TestCase):
    def make_fixture(self, root: Path) -> tuple[Path, Path, Path]:
        source_root = root / "rfdetr"
        case_dir = source_root / "cases/0704_06"
        timeline_root = root / "timelines"
        timeline_dir = timeline_root / "0704_6"
        timeline_dir.mkdir(parents=True)
        (case_dir).mkdir(parents=True)

        timestamps = [0, 100_000_000]
        (case_dir / "ros_image_timestamps.json").write_text(
            json.dumps(
                {
                    "schema": "arpa_h_ros_image_timestamps_v1",
                    "counts": {"cam4": 2, "flir": 2},
                    "bag_timestamps_ns": {
                        "cam4": timestamps,
                        "flir": timestamps,
                    },
                }
            ),
            encoding="utf-8",
        )
        (timeline_dir / "cam4_frame_timeline.v1.json").write_text(
            json.dumps(
                {
                    "case_id": "0704_6",
                    "frame_count": 2,
                    "timestamps_sec": [0.0, 0.1],
                }
            ),
            encoding="utf-8",
        )

        for view, width, height in (
            ("cam4", 1280, 720),
            ("flir", 2048, 1496),
        ):
            reconstruction_dir = case_dir / view / "reconstruction"
            reconstruction_dir.mkdir(parents=True)
            data_path = reconstruction_dir / "instances.jsonl.gz"
            instance = {
                "instance_id": 1,
                "render_order": 0,
                "tracker_id": 27 if view == "flir" else None,
                "class_id": 1,
                "class_name": "Adson forceps",
                "confidence": 0.81234567,
                "bbox_xyxy": [10.0, 20.0, 30.0, 40.0],
                "segmentation": {
                    "format": "coco_rle_compressed",
                    "size": [height, width],
                    "counts": "private-heavy-mask",
                }
                if view == "flir"
                else None,
            }
            with gzip.open(data_path, "wt", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {
                            "frame_index": 0,
                            "video_relative_timestamp_ns": timestamps[0],
                            "rosbag_timestamp_ns": timestamps[0],
                            "instances": [instance],
                        }
                    )
                    + "\n"
                )
                stream.write(
                    json.dumps(
                        {
                            "frame_index": 1,
                            "video_relative_timestamp_ns": timestamps[1],
                            "rosbag_timestamp_ns": timestamps[1],
                            "instances": [],
                        }
                    )
                    + "\n"
                )
            digest = hashlib.sha256(data_path.read_bytes()).hexdigest()
            (reconstruction_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema": "arpa_h_rfdetr_frame_instances_v1",
                        "status": "complete",
                        "view": view,
                        "video": {
                            "width": width,
                            "height": height,
                            "fps": 10.0,
                            "declared_frames": 2,
                        },
                        "data_file_sha256": digest,
                        "export": {
                            "model": (
                                "RFDETRSegSmall"
                                if view == "flir"
                                else "RFDETRSmall"
                            )
                        },
                    }
                ),
                encoding="utf-8",
            )
        return source_root, timeline_root, root / "output"

    def test_case_id_mapping_preserves_non_padded_app_ids(self) -> None:
        self.assertEqual("0704_6", app_case_id("0704_06"))
        self.assertEqual("0704_17", app_case_id("0704_17"))
        self.assertEqual("0704_06", source_case_id("0704_6"))
        self.assertEqual("0704_17", source_case_id("0704_17"))

    def test_proxy_transform_matches_cam4_and_flir_review_media(self) -> None:
        self.assertEqual([0, 0, 640, 360], even_proxy_content_rect(1280, 720))
        self.assertEqual([74, 0, 492, 360], even_proxy_content_rect(2048, 1496))

    def test_build_strips_masks_paths_and_preserves_frame_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root, timeline_root, output_dir = self.make_fixture(root)
            index = build_overlays(
                source_root=source_root,
                timeline_root=timeline_root,
                output_dir=output_dir,
            )
            self.assertEqual(INDEX_SCHEMA, index["schema"])
            self.assertEqual(AUTHORITY, index["authority"])
            self.assertEqual(1, index["case_count"])

            payload_text = (output_dir / "0704_6.json").read_text(
                encoding="utf-8"
            )
            payload = json.loads(payload_text)
            self.assertEqual(OUTPUT_SCHEMA, payload["schema"])
            self.assertEqual("0704_6", payload["case_id"])
            self.assertEqual("0704_06", payload["dataset_case_id"])
            self.assertEqual(2, payload["frame_count"])
            self.assertEqual(2, len(payload["views"]["cam4"]["frames"]))
            self.assertEqual(
                [74, 0, 492, 360],
                payload["views"]["flir"]["continuous_proxy"]["content_rect"],
            )
            flir_instance = payload["views"]["flir"]["frames"][0][0]
            self.assertEqual(27, flir_instance["tracker_id"])
            self.assertNotIn("segmentation", flir_instance)
            self.assertNotIn("mask", payload_text)
            self.assertNotIn("checkpoint", payload_text)
            self.assertNotIn(str(source_root), payload_text)

    def test_timestamp_mismatch_fails_closed_without_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root, timeline_root, output_dir = self.make_fixture(root)
            timeline_path = (
                timeline_root / "0704_6/cam4_frame_timeline.v1.json"
            )
            timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
            timeline["timestamps_sec"][1] = 0.2
            timeline_path.write_text(json.dumps(timeline), encoding="utf-8")

            with self.assertRaises(OverlayImportError):
                build_overlays(
                    source_root=source_root,
                    timeline_root=timeline_root,
                    output_dir=output_dir,
                )
            self.assertFalse((output_dir / "0704_6.json").exists())

    def test_reconstruction_record_timestamp_mismatch_fails_closed(self) -> None:
        for field in (
            "rosbag_timestamp_ns",
            "video_relative_timestamp_ns",
        ):
            with (
                self.subTest(field=field),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                source_root, timeline_root, output_dir = self.make_fixture(root)
                for view in ("cam4", "flir"):
                    reconstruction_dir = (
                        source_root / "cases/0704_06" / view / "reconstruction"
                    )
                    data_path = reconstruction_dir / "instances.jsonl.gz"
                    with gzip.open(data_path, "rt", encoding="utf-8") as stream:
                        records = [json.loads(line) for line in stream]
                    records[1][field] += 1
                    with gzip.open(data_path, "wt", encoding="utf-8") as stream:
                        for record in records:
                            stream.write(json.dumps(record) + "\n")
                    manifest_path = reconstruction_dir / "manifest.json"
                    manifest = json.loads(
                        manifest_path.read_text(encoding="utf-8")
                    )
                    manifest["data_file_sha256"] = hashlib.sha256(
                        data_path.read_bytes()
                    ).hexdigest()
                    manifest_path.write_text(
                        json.dumps(manifest),
                        encoding="utf-8",
                    )

                with self.assertRaises(OverlayImportError):
                    build_overlays(
                        source_root=source_root,
                        timeline_root=timeline_root,
                        output_dir=output_dir,
                    )
                self.assertFalse((output_dir / "0704_6.json").exists())


if __name__ == "__main__":
    unittest.main()
