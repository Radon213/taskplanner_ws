from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from tools.real_surgery_annotation.annotation_gui import (
    AnnotationStore,
    ConflictError,
    InputError,
)


ROOT = Path(__file__).resolve().parents[2]
SOURCE_CASE = ROOT / "annotations/observable_tool_events/cases/0704_5"
SCHEMA = (
    ROOT
    / "annotations/observable_tool_events/schema/"
    "observable_tool_event.v1.schema.json"
)
TOOLS = ROOT / "annotations/observable_tool_events/catalogs/tools.yaml"
SOURCE_BAG = Path(
    "/mnt/arl/NAS관리/백업/업무/ARPA-H/SurgeryData/갑상샘/"
    "0704_멀티모달_ROS2_MCAP_v1.0.0/bags/0704_5"
)
FIXTURE_PROPOSAL = {
    "schema": "taskplanner.observable_tool_event.v1",
    "case_id": "0704_5",
    "event_id": "0704_5-P9000",
    "event_type": "initial_state",
    "time_sec": 0.0,
    "tool": {
        "id": "scalpel",
        "name": "Scalpel",
        "instance_id": "0704_5-tool-fixture",
    },
    "from": None,
    "to": {"holder": "none", "location": "mayo_stand"},
    "derived_action": "initial_state",
    "source_views": ["cam4"],
    "visibility": "clear",
    "review_status": "proposed",
    "label_origin": "legacy_perception_seed",
}


class AnnotationStoreTest(unittest.TestCase):
    def make_store(self, root: Path) -> AnnotationStore:
        case_dir = root / "0704_5"
        shutil.copytree(SOURCE_CASE, case_dir)
        manifest_path = case_dir / "annotation_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        (case_dir / manifest["event_file"]).write_text("", encoding="utf-8")
        (case_dir / manifest["candidate_file"]).write_text(
            json.dumps(FIXTURE_PROPOSAL, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        manifest["review_status_counts"] = {
            "proposed": 1,
            "confirmed": 0,
            "ambiguous": 0,
            "rejected": 0,
        }
        manifest["human_annotation"]["confirmed_event_count"] = 0
        manifest["annotation_adjudication"].update(
            {
                "complete": False,
                "confirmed_event_count": 0,
                "confirmed_origin_counts": {},
                "confirmed_reviewer_kind_counts": {},
            }
        )
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return AnnotationStore(
            case_dir=case_dir,
            schema_path=SCHEMA,
            tools_path=TOOLS,
            source_bag_dir=SOURCE_BAG,
        )

    def test_confirmed_review_moves_proposal_to_event_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self.make_store(Path(temporary))
            before = store.state()
            proposal = before["candidates"][0]
            result = store.save_review(
                {
                    "revision": before["revision"],
                    "reviewer_id": "fixture-reviewer",
                    "review_status": "confirmed",
                    "review_notes": "fixture only",
                    "event": proposal,
                }
            )
            after = result["state"]
            self.assertEqual(len(before["candidates"]) - 1, len(after["candidates"]))
            self.assertEqual(1, len(after["events"]))
            self.assertEqual(1, after["review_status_counts"]["confirmed"])
            saved = after["events"][0]
            self.assertEqual("human_video_review", saved["label_origin"])
            self.assertEqual("human", saved["review"]["reviewer_kind"])

    def test_stale_revision_is_rejected_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self.make_store(Path(temporary))
            state = store.state()
            with self.assertRaises(ConflictError):
                store.save_review(
                    {
                        "revision": "stale",
                        "reviewer_id": "fixture-reviewer",
                        "review_status": "confirmed",
                        "event": state["candidates"][0],
                    }
                )
            self.assertEqual(
                len(state["candidates"]),
                len(store.state()["candidates"]),
            )

    def test_unknown_confirmed_is_guided_to_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self.make_store(Path(temporary))
            state = store.state()
            proposal = dict(state["candidates"][0])
            proposal["to"] = {"holder": "unknown", "location": "mayo_stand"}
            with self.assertRaisesRegex(InputError, "use ambiguous"):
                store.save_review(
                    {
                        "revision": state["revision"],
                        "reviewer_id": "fixture-reviewer",
                        "review_status": "confirmed",
                        "event": proposal,
                    }
                )
            self.assertEqual(
                len(state["candidates"]),
                len(store.state()["candidates"]),
            )

    def test_completed_adjudication_is_locked_against_gui_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self.make_store(Path(temporary))
            manifest = json.loads(store.manifest_path.read_text(encoding="utf-8"))
            manifest["annotation_adjudication"]["complete"] = True
            store.manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            state = store.state()
            with self.assertRaisesRegex(InputError, "최종 승격이 완료"):
                store.save_review(
                    {
                        "revision": state["revision"],
                        "reviewer_id": "fixture-reviewer",
                        "review_status": "confirmed",
                        "event": state["candidates"][0],
                    }
                )


if __name__ == "__main__":
    unittest.main()
