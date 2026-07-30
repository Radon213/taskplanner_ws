from __future__ import annotations

import unittest

from tools.real_surgery_annotation import EVENT_TOPIC, MANIFEST_TOPIC
from tools.real_surgery_annotation.inject_annotations import (
    require_complete_annotation,
    stable_merge_records,
)


def event(event_id: str, event_type: str, time_sec: float) -> dict:
    return {
        "event_id": event_id,
        "event_type": event_type,
        "time_sec": time_sec,
    }


class StableMergeTest(unittest.TestCase):
    def test_gt_injection_requires_review_completion_and_empty_queue(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "proposed candidate"):
            require_complete_annotation(
                {"human_annotation": {"complete": True}},
                {"review_status_counts": {"proposed": 1}},
            )
        with self.assertRaisesRegex(RuntimeError, "complete must be true"):
            require_complete_annotation(
                {"human_annotation": {"complete": False}},
                {"review_status_counts": {"proposed": 0}},
            )
        require_complete_annotation(
            {"human_annotation": {"complete": True}},
            {"review_status_counts": {"proposed": 0}},
        )
        require_complete_annotation(
            {
                "annotation_adjudication": {"complete": True},
                "human_annotation": {"complete": False},
            },
            {"review_status_counts": {"proposed": 0}},
        )

    def test_tie_order_is_manifest_initial_original_general(self) -> None:
        originals = [
            ("/source/a", b"a0", 0),
            ("/source/b", b"b0", 0),
            ("/source/a", b"a1", 1_000_000_000),
        ]
        events = [
            (event("I1", "initial_state", 0.0), b"initial"),
            (event("E1", "tool_transfer", 0.0), b"general"),
            (event("E2", "tool_transfer", 0.5), b"half"),
            (event("E3", "tool_transfer", 1.0), b"one"),
        ]
        merged = list(
            stable_merge_records(
                originals,
                manifest_payload=b"manifest",
                event_payloads=events,
            )
        )
        self.assertEqual(
            [
                (MANIFEST_TOPIC, 0, "manifest"),
                (EVENT_TOPIC, 0, "initial_state"),
                ("/source/a", 0, "original"),
                ("/source/b", 0, "original"),
                (EVENT_TOPIC, 0, "event"),
                (EVENT_TOPIC, 500_000_000, "event"),
                ("/source/a", 1_000_000_000, "original"),
                (EVENT_TOPIC, 1_000_000_000, "event"),
            ],
            [(topic, stamp, origin) for topic, _, stamp, origin in merged],
        )


if __name__ == "__main__":
    unittest.main()
