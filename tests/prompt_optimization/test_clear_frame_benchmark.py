from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.prompt_optimization.gesture_recognition import clear_frame_benchmark as clear


class ClearFrameBenchmarkTest(unittest.TestCase):
    def _write_json(self, path: Path, value: object) -> None:
        path.write_text(json.dumps(value), encoding="utf-8")

    def _write_jsonl(self, path: Path, values: list[object]) -> None:
        path.write_text(
            "\n".join(json.dumps(value) for value in values) + "\n",
            encoding="utf-8",
        )

    def test_uses_one_event_midpoint_and_only_clear_internal_gap_midpoints(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            events = root / "events.jsonl"
            masks = root / "masks.json"
            timeline = root / "timeline.json"
            self._write_jsonl(
                events,
                [
                    {
                        "case_id": "0704_6",
                        "event_id": event_id,
                        "event_type": "implicit_tool_request",
                        "review_status": "confirmed",
                        "start_source_frame_idx": start,
                        "end_source_frame_idx": end,
                        "start_sec": float(start),
                    }
                    for event_id, start, end in (
                        ("R1", 20, 40),
                        ("R2", 100, 140),
                        ("R3", 200, 240),
                    )
                ],
            )
            self._write_json(
                masks,
                {
                    "event_roles": [
                        {
                            "event_id": event_id,
                            "role": "gesture_target",
                            "metric_eligibility": {
                                "gesture_presence": True,
                                "gesture_onset": True,
                            },
                        }
                        for event_id in ("R1", "R2", "R3")
                    ]
                },
            )
            self._write_json(
                timeline,
                {"timestamps_sec": [index / 15.0 for index in range(300)]},
            )

            with patch.object(
                clear.gesture,
                "resolve_case_sources",
                return_value=(events, masks, timeline),
            ):
                samples, coverage = clear.build_clear_frame_manifest(
                    case_ids=("0704_6",),
                    development_case_ids=("0704_6",),
                    calibration_event_fraction=0.60,
                    min_negative_clearance_frames=30,
                )

        positives = [sample for sample in samples if sample["label"] == "open_receive"]
        negatives = [sample for sample in samples if sample["label"] == "not_open_receive"]
        self.assertEqual([30, 120, 220], [sample["frame_idx"] for sample in positives])
        self.assertEqual([70, 170], [sample["frame_idx"] for sample in negatives])
        self.assertTrue(
            all(
                sample["nearest_open_hand_boundary_frames"] >= 30
                for sample in negatives
            )
        )
        self.assertTrue(
            all(
                sample["sample_kind"] == "negative_inter_event_gap_midpoint"
                for sample in negatives
            )
        )
        self.assertEqual(3, coverage["labels"]["open_receive"])
        self.assertEqual(2, coverage["labels"]["not_open_receive"])
        self.assertEqual("development_calibration", positives[0]["evaluation_group"])
        self.assertEqual(
            "development_temporal_challenge", positives[-1]["evaluation_group"]
        )

    def test_short_gap_is_omitted_instead_of_becoming_a_boundary_control(self) -> None:
        retained, omitted = clear._internal_gap_midpoints(
            merged_intervals=(
                {"start_frame": 10, "end_frame": 20, "event_ids": ["R1"]},
                {"start_frame": 50, "end_frame": 60, "event_ids": ["R2"]},
                {"start_frame": 70, "end_frame": 80, "event_ids": ["R3"]},
            ),
            min_clearance_frames=15,
        )
        self.assertEqual(1, len(retained))
        self.assertEqual(1, len(omitted))
        self.assertGreaterEqual(retained[0]["nearest_open_hand_boundary_frames"], 15)
        self.assertLess(omitted[0]["nearest_open_hand_boundary_frames"], 15)


if __name__ == "__main__":
    unittest.main()
