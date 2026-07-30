from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
import unittest
from pathlib import Path

import pytest

from tools.real_surgery_annotation import finalize_interaction_review
from tools.real_surgery_annotation.finalize_interaction_review import (
    FinalizationError,
    apply_assistant_corrections,
    derive_compound_action_episodes,
    project_dt_records,
    projection_provenance,
    publish_create_only,
)
from tools.real_surgery_annotation.interaction_review_gui import canonical_json


ROOT = Path(__file__).resolve().parents[2]


def test_create_only_rolls_back_links_after_directory_fsync_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outputs = {
        tmp_path / "first.json": b'{"first":true}\n',
        tmp_path / "second.json": b'{"second":true}\n',
    }
    original_fsync = os.fsync

    def fail_directory_fsync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("injected directory fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(
        finalize_interaction_review.os,
        "fsync",
        fail_directory_fsync,
    )

    with pytest.raises(FinalizationError, match="directory fsync failure"):
        publish_create_only(outputs)

    assert all(not path.exists() for path in outputs)


def transfer(
    event_id: str,
    time_sec: float,
    tool: str,
    from_location: str,
    to_location: str,
) -> dict:
    return {
        "schema": "taskplanner.observable_interaction_point.v1",
        "case_id": "fixture",
        "event_id": event_id,
        "event_type": "tool_transfer",
        "time_sec": time_sec,
        "source_frame_idx": int(time_sec * 10),
        "source_views": ["cam4", "flir"],
        "tool": tool,
        "from": from_location,
        "to": to_location,
        "review_status": "confirmed",
        "label_origin": "human_video_review",
        "review": {
            "reviewer_kind": "human",
            "reviewer_id": "fixture",
            "reviewed_at": "2026-07-28T00:00:00+00:00",
        },
    }


class DTInteractionProjectionTest(unittest.TestCase):
    def test_normalizes_unresolved_recipient_and_excludes_unresolved_bundle(
        self,
    ) -> None:
        recipient = transfer(
            "fixture-T0032",
            1.0,
            "adson_forceps",
            "scrub_nurse",
            "operative_person_role_unresolved",
        )
        recipient["schema"] = "taskplanner.observable_interaction_point.v2"
        bundle = transfer(
            "fixture-T0026",
            2.0,
            "retractor_bundle_unresolved",
            "operative_person_role_unresolved",
            "scrub_nurse",
        )
        bundle["schema"] = "taskplanner.observable_interaction_point.v2"
        policy = {
            "rules": {
                "normalize_unresolved_operative_recipient": {
                    "enabled": True,
                    "observed_endpoint": "operative_person_role_unresolved",
                    "projected_endpoint": "surgeon",
                    "source_event_ids": ["fixture-T0032"],
                    "reason": "DT-only normalization",
                },
                "exclude_unresolved_retractor_bundle": {
                    "enabled": True,
                    "tool": "retractor_bundle_unresolved",
                    "source_event_ids": ["fixture-T0026"],
                    "reason": "observation-only unresolved bundle",
                },
            }
        }

        projected, operations = project_dt_records(
            [recipient, bundle],
            max_chain_gap_sec=3.0,
            policy=policy,
        )

        self.assertEqual(["fixture-T0032"], [item["event_id"] for item in projected])
        self.assertEqual("surgeon", projected[0]["to"])
        self.assertEqual(
            "operative_person_role_unresolved",
            recipient["to"],
        )
        self.assertEqual(
            ["fixture-T0032"],
            [
                item["source_event_id"]
                for item in operations["normalized_recipients"]
            ],
        )
        self.assertEqual(
            ["fixture-T0026"],
            [
                item["source_event_id"]
                for item in operations["excluded_unresolved_transfers"]
            ],
        )

    def test_excludes_cleanup_collapses_return_and_keeps_handover(self) -> None:
        request = {
            "schema": "taskplanner.observable_interaction_interval.v1",
            "case_id": "fixture",
            "event_id": "fixture-R0001",
            "event_type": "implicit_tool_request",
            "time_sec": 0.0,
            "source_frame_idx": 0,
            "start_sec": 0.0,
            "start_source_frame_idx": 0,
            "end_sec": 0.1,
            "end_source_frame_idx": 1,
            "source_views": ["cam4", "flir"],
            "review_status": "confirmed",
            "label_origin": "human_video_review",
            "review": {
                "reviewer_kind": "human",
                "reviewer_id": "fixture",
                "reviewed_at": "2026-07-28T00:00:00+00:00",
            },
        }
        records = [
            request,
            transfer(
                "fixture-T0001",
                1.0,
                "bovie",
                "mayo_stand",
                "scrub_nurse",
            ),
            transfer(
                "fixture-T0002",
                1.5,
                "bovie",
                "scrub_nurse",
                "mayo_stand",
            ),
            transfer(
                "fixture-T0003",
                3.0,
                "bipolar_forceps",
                "surgeon",
                "scrub_nurse",
            ),
            transfer(
                "fixture-T0004",
                3.4,
                "bipolar_forceps",
                "scrub_nurse",
                "mayo_stand",
            ),
            transfer(
                "fixture-T0005",
                5.0,
                "adson_forceps",
                "mayo_stand",
                "scrub_nurse",
            ),
            transfer(
                "fixture-T0006",
                5.5,
                "adson_forceps",
                "scrub_nurse",
                "surgeon",
            ),
        ]
        original = copy.deepcopy(records)

        projected, operations = project_dt_records(
            records,
            max_chain_gap_sec=2.0,
        )

        self.assertEqual(original, records)
        self.assertEqual(
            [
                "fixture-R0001",
                "fixture-T0004",
                "fixture-T0005",
                "fixture-T0006",
            ],
            [record["event_id"] for record in projected],
        )
        collapsed = projected[1]
        self.assertEqual("surgeon", collapsed["from"])
        self.assertEqual("mayo_stand", collapsed["to"])
        self.assertEqual(3.4, collapsed["time_sec"])
        self.assertEqual(
            [["fixture-T0001", "fixture-T0002"]],
            [
                item["source_event_ids"]
                for item in operations["excluded_roundtrips"]
            ],
        )
        self.assertEqual(
            [["fixture-T0003", "fixture-T0004"]],
            [
                item["source_event_ids"]
                for item in operations["collapsed_returns"]
            ],
        )
        self.assertEqual(
            [["fixture-T0005", "fixture-T0006"]],
            [
                item["source_event_ids"]
                for item in derive_compound_action_episodes(
                    projected,
                    max_chain_gap_sec=2.0,
                )
            ],
        )
        self.assertEqual(
            [
                {
                    "output_event_id": "fixture-T0004",
                    "direct_observation": False,
                    "projection": "collapse_surgeon_scrub_mayo_return",
                    "source_event_ids": ["fixture-T0003", "fixture-T0004"],
                    "observed_output_edge": ["scrub_nurse", "mayo_stand"],
                    "projected_output_edge": ["surgeon", "mayo_stand"],
                    "reason": (
                        "continuous surgeon return completed on the Mayo "
                        "stand through the scrub hand"
                    ),
                }
            ],
            projection_provenance(operations),
        )

    def test_does_not_project_noncontinuous_roundtrip(self) -> None:
        records = [
            transfer(
                "fixture-T0001",
                1.0,
                "bovie",
                "mayo_stand",
                "scrub_nurse",
            ),
            transfer(
                "fixture-T0002",
                4.0,
                "bovie",
                "scrub_nurse",
                "mayo_stand",
            ),
        ]

        projected, operations = project_dt_records(
            records,
            max_chain_gap_sec=2.0,
        )

        self.assertEqual(2, len(projected))
        self.assertEqual([], operations["excluded_roundtrips"])

    def test_unclosed_direct_return_stays_observed_but_is_not_scored(self) -> None:
        records = [
            transfer(
                "fixture-T0001",
                1.0,
                "army_navy_retractor",
                "surgeon",
                "scrub_nurse",
            )
        ]

        projected, operations = project_dt_records(
            records,
            max_chain_gap_sec=2.0,
        )

        self.assertEqual([], projected)
        self.assertEqual(
            ["fixture-T0001"],
            [
                item["source_event_id"]
                for item in operations["excluded_unclosed_direct_returns"]
            ],
        )


class _IdentityStore:
    @staticmethod
    def _validated_fields(fields, *, request_interval=False):
        del request_interval
        return copy.deepcopy(fields)


class AssistantCorrectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.decision = {
            "action_id": "fixture-A0001",
            "adjudicated_fields": {
                "event_type": "tool_transfer",
                "source_frame_idx": 1,
                "time_sec": 1.0,
                "tool": "bovie",
                "from": "surgeon",
                "to": "mayo_stand",
                "phase_id": None,
                "source_views": ["cam4", "flir"],
            },
            "review_status": "confirmed",
            "resulting_label_origin": "human_video_review",
            "review": {
                "reviewer_kind": "human",
                "reviewer_id": "fixture-human",
                "reviewed_at": "2026-07-28T00:00:00+00:00",
            },
        }
        self.state = {
            "case_id": "fixture",
            "candidates": [
                {
                    "_review_ui": {
                        "candidate_id": "fixture-T0001",
                        "effective_decision": self.decision,
                    }
                }
            ],
            "human_annotations": [],
        }
        self.schema = json.loads(
            (
                ROOT
                / "annotations/observable_tool_events/schema/"
                "assistant_annotation_adjudication.v1.schema.json"
            ).read_text(encoding="utf-8")
        )
        self.correction = {
            "schema": "taskplanner.assistant_annotation_adjudication.v1",
            "case_id": "fixture",
            "correction_id": "fixture-C0001",
            "annotation_id": "fixture-T0001",
            "supersedes_action_id": "fixture-A0001",
            "source_action_sha256": hashlib.sha256(
                canonical_json(self.decision).encode("utf-8")
            ).hexdigest(),
            "review_status": "confirmed",
            "resulting_label_origin": "assistant_video_adjudication",
            "adjudicated_fields": {
                **self.decision["adjudicated_fields"],
                "source_frame_idx": 2,
                "time_sec": 2.0,
                "to": "scrub_nurse",
            },
            "review": {
                "reviewer_kind": "ai_assistant",
                "reviewer_id": "codex-gpt-5.6-sol",
                "authorized_by": "fixture-human",
                "reviewed_at": "2026-07-28T01:00:00+00:00",
                "notes": "Dense-frame correction.",
            },
        }

    def test_applies_hash_anchored_authorized_correction(self) -> None:
        effective, summary = apply_assistant_corrections(
            state=self.state,
            store=_IdentityStore(),
            corrections=[self.correction],
            correction_schema=self.schema,
        )

        corrected = effective["fixture-T0001"]
        self.assertEqual("fixture-C0001", corrected["action_id"])
        self.assertEqual("scrub_nurse", corrected["adjudicated_fields"]["to"])
        self.assertEqual({"confirmed": 1}, summary["status_counts"])

    def test_rejects_stale_source_action_hash(self) -> None:
        correction = copy.deepcopy(self.correction)
        correction["source_action_sha256"] = "0" * 64

        with self.assertRaisesRegex(
            FinalizationError,
            "source action hash",
        ):
            apply_assistant_corrections(
                state=self.state,
                store=_IdentityStore(),
                corrections=[correction],
                correction_schema=self.schema,
            )


if __name__ == "__main__":
    unittest.main()
