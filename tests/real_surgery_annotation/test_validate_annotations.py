from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.real_surgery_annotation.event_model import load_jsonl
from tools.real_surgery_annotation.validate_annotations import validate_records


ROOT = Path(__file__).resolve().parents[2]
SCHEMA = json.loads(
    (
        ROOT
        / "annotations/observable_tool_events/schema/"
        "observable_tool_event.v1.schema.json"
    ).read_text(encoding="utf-8")
)
import yaml

TOOLS = yaml.safe_load(
    (
        ROOT / "annotations/observable_tool_events/catalogs/tools.yaml"
    ).read_text(encoding="utf-8")
)


def confirmed_initial() -> dict:
    return {
        "schema": "taskplanner.observable_tool_event.v1",
        "case_id": "0704_5",
        "event_id": "0704_5-I0001",
        "event_type": "initial_state",
        "time_sec": 0.0,
        "tool": {
            "id": "scalpel",
            "name": "Scalpel",
            "instance_id": "0704_5-tool-001",
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
            "reviewer_id": "reviewer-1",
            "reviewed_at": "2026-07-27T10:00:00+09:00",
        },
    }


class AnnotationValidationTest(unittest.TestCase):
    def validate(self, records: list[dict]) -> list[str]:
        return validate_records(
            records,
            schema=SCHEMA,
            tool_catalog=TOOLS,
            case_id="0704_5",
            duration_sec=163.1,
        )

    def test_valid_initial_and_pickup_sequence(self) -> None:
        initial = confirmed_initial()
        pickup = {
            **initial,
            "event_id": "0704_5-E0002",
            "event_type": "pickup_from_mayo",
            "time_sec": 1.25,
            "from": {"holder": "none", "location": "mayo_stand"},
            "to": {"holder": "surgeon", "location": "right_hand"},
            "derived_action": "pickup_from_mayo",
        }
        self.assertEqual([], self.validate([initial, pickup]))

    def test_confirmed_requires_review(self) -> None:
        event = confirmed_initial()
        del event["review"]
        errors = self.validate([event])
        self.assertTrue(any("review" in error for error in errors), errors)

    def test_human_video_review_origin_requires_review_for_any_status(self) -> None:
        event = confirmed_initial()
        event["review_status"] = "proposed"
        event["proposal"] = {"generator": "fixture"}
        del event["review"]
        errors = self.validate([event])
        self.assertTrue(any("review" in error for error in errors), errors)

    def test_assistant_confirmed_event_preserves_assistant_provenance(self) -> None:
        event = confirmed_initial()
        event["label_origin"] = "assistant_video_adjudication"
        event["review"] = {
            "reviewer_kind": "ai_assistant",
            "reviewer_id": "codex-video-adjudicator",
            "authorized_by": "문종찬",
            "reviewed_at": "2026-07-27T10:00:00+09:00",
        }
        self.assertEqual([], self.validate([event]))

        event["label_origin"] = "human_video_review"
        errors = self.validate([event])
        self.assertTrue(any("label_origin" in error for error in errors), errors)

    def test_assistant_confirmed_event_requires_authorization(self) -> None:
        event = confirmed_initial()
        event["label_origin"] = "assistant_video_adjudication"
        event["review"] = {
            "reviewer_kind": "ai_assistant",
            "reviewer_id": "codex-video-adjudicator",
            "reviewed_at": "2026-07-27T10:00:00+09:00",
        }
        errors = self.validate([event])
        self.assertTrue(any("authorized_by" in error for error in errors), errors)

    def test_derived_action_is_recomputed(self) -> None:
        event = confirmed_initial()
        event["derived_action"] = "relocate"
        errors = self.validate([event])
        self.assertTrue(any("derived_action" in error for error in errors), errors)

    def test_state_discontinuity_is_rejected(self) -> None:
        initial = confirmed_initial()
        transfer = {
            **initial,
            "event_id": "0704_5-E0002",
            "event_type": "tool_transfer",
            "time_sec": 2.0,
            "from": {"holder": "scrub_nurse", "location": "right_hand"},
            "to": {"holder": "surgeon", "location": "left_hand"},
            "derived_action": "handover",
        }
        errors = self.validate([initial, transfer])
        self.assertTrue(any("state discontinuity" in error for error in errors), errors)

    def test_first_known_from_without_initial_state_is_allowed(self) -> None:
        event = confirmed_initial()
        event.update(
            {
                "event_id": "0704_5-E0001",
                "event_type": "tool_transfer",
                "time_sec": 4.0,
                "from": {"holder": "scrub_nurse", "location": "right_hand"},
                "to": {"holder": "surgeon", "location": "left_hand"},
                "derived_action": "handover",
            }
        )
        self.assertEqual([], self.validate([event]))

    def test_unspecified_hand_still_derives_handover(self) -> None:
        event = confirmed_initial()
        event.update(
            {
                "event_id": "0704_5-E0001",
                "event_type": "tool_transfer",
                "time_sec": 4.0,
                "from": {
                    "holder": "scrub_nurse",
                    "location": "hand_unspecified",
                },
                "to": {"holder": "surgeon", "location": "hand_unspecified"},
                "derived_action": "handover",
            }
        )
        self.assertEqual([], self.validate([event]))

    def test_operative_recipient_preserves_handover_without_false_role_precision(
        self,
    ) -> None:
        event = confirmed_initial()
        event.update(
            {
                "event_id": "0704_5-E0001",
                "event_type": "tool_transfer",
                "time_sec": 4.0,
                "from": {
                    "holder": "scrub_nurse",
                    "location": "hand_unspecified",
                },
                "to": {
                    "holder": "operative_recipient",
                    "location": "hand_unspecified",
                },
                "derived_action": "handover",
            }
        )
        self.assertEqual([], self.validate([event]))

    def test_hand_location_requires_known_human_holder(self) -> None:
        event = confirmed_initial()
        event["to"] = {"holder": "none", "location": "hand_unspecified"}
        errors = self.validate([event])
        self.assertTrue(
            any("hand location requires" in error for error in errors),
            errors,
        )

    def test_non_monotonic_time_is_rejected(self) -> None:
        first = confirmed_initial()
        first["time_sec"] = 2.0
        second = confirmed_initial()
        second["event_id"] = "0704_5-I0002"
        second["tool"] = {
            "id": "bovie",
            "name": "Bovie surgical cautery",
            "instance_id": "0704_5-tool-002",
        }
        second["time_sec"] = 1.0
        errors = self.validate([first, second])
        self.assertTrue(any("earlier" in error for error in errors), errors)

    def test_unknown_state_cannot_be_confirmed(self) -> None:
        event = confirmed_initial()
        event["to"] = {"holder": "unknown", "location": "mayo_stand"}
        errors = self.validate([event])
        self.assertTrue(any("use ambiguous" in error for error in errors), errors)

    def test_jsonl_parse_error_reports_line(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bad.jsonl"
            path.write_text('{"ok": true}\n{bad}\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, r":2: invalid JSON"):
                load_jsonl(path)


if __name__ == "__main__":
    unittest.main()
