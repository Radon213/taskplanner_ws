from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import jsonschema

from tools.real_surgery_annotation.interaction_review_gui import (
    ConflictError,
    FinalReviewBundle,
    InputError,
    ReviewCaseRuntime,
    ReviewStore,
    make_handler,
    parse_single_byte_range,
    resolve_source_bag_directory,
    sha256_file,
    sha256_value,
    validate_review_multiview_proxy,
)


class SourceBagResolutionTest(unittest.TestCase):
    def test_accepts_verified_known_collection_relocation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            case_id = "0704_6"
            declared = (
                root
                / "0704_멀티모달_ROS2_MCAP_v1.0.0"
                / "bags"
                / case_id
            )
            operational = root / "0704_rosbag2" / "bags" / case_id
            operational.mkdir(parents=True)
            metadata_path = operational / "metadata.yaml"
            metadata_path.write_text("rosbag2_bagfile_information: {}\n")
            mcap_path = operational / "source.mcap"
            mcap_path.write_bytes(b"mcap")
            manifest = {
                "source_bag": {
                    "directory": str(declared),
                    "mcap_file": "source.mcap",
                    "mcap_sha256": sha256_file(mcap_path),
                    "metadata_sha256": sha256_file(metadata_path),
                }
            }

            resolved = resolve_source_bag_directory(
                declared_source_bag=declared,
                case_id=case_id,
                annotation_manifest=manifest,
            )

            self.assertEqual(operational.resolve(), resolved)

    def test_rejects_relocated_bag_with_different_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            case_id = "0704_6"
            declared = (
                root
                / "0704_멀티모달_ROS2_MCAP_v1.0.0"
                / "bags"
                / case_id
            )
            operational = root / "0704_rosbag2" / "bags" / case_id
            operational.mkdir(parents=True)
            (operational / "metadata.yaml").write_text("different\n")
            (operational / "source.mcap").write_bytes(b"mcap")
            manifest = {
                "source_bag": {
                    "directory": str(declared),
                    "mcap_file": "source.mcap",
                    "mcap_sha256": sha256_file(operational / "source.mcap"),
                    "metadata_sha256": "a" * 64,
                }
            }

            with self.assertRaisesRegex(InputError, "metadata 해시"):
                resolve_source_bag_directory(
                    declared_source_bag=declared,
                    case_id=case_id,
                    annotation_manifest=manifest,
                )

    def test_rejects_manifest_and_timeline_identity_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            declared = root / "collection" / "bags" / "0704_6"
            manifest = {
                "source_bag": {
                    "directory": str(root / "other" / "bags" / "0704_6"),
                    "mcap_file": "source.mcap",
                    "metadata_sha256": "a" * 64,
                }
            }

            with self.assertRaisesRegex(InputError, "timeline"):
                resolve_source_bag_directory(
                    declared_source_bag=declared,
                    case_id="0704_6",
                    annotation_manifest=manifest,
                )

    def test_rejects_relocated_bag_with_different_mcap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            case_id = "0704_6"
            declared = (
                root
                / "0704_멀티모달_ROS2_MCAP_v1.0.0"
                / "bags"
                / case_id
            )
            operational = root / "0704_rosbag2" / "bags" / case_id
            operational.mkdir(parents=True)
            metadata_path = operational / "metadata.yaml"
            metadata_path.write_text("metadata\n")
            (operational / "source.mcap").write_bytes(b"tampered")
            manifest = {
                "source_bag": {
                    "directory": str(declared),
                    "mcap_file": "source.mcap",
                    "mcap_sha256": sha256_value("expected source"),
                    "metadata_sha256": sha256_file(metadata_path),
                }
            }

            with self.assertRaisesRegex(InputError, "MCAP 해시"):
                resolve_source_bag_directory(
                    declared_source_bag=declared,
                    case_id=case_id,
                    annotation_manifest=manifest,
                )

    def test_rejects_source_mcap_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            case_id = "0704_6"
            declared = root / "collection" / "bags" / case_id
            declared.mkdir(parents=True)
            metadata_path = declared / "metadata.yaml"
            metadata_path.write_text("metadata\n")
            manifest = {
                "source_bag": {
                    "directory": str(declared),
                    "mcap_file": "../outside.mcap",
                    "mcap_sha256": "a" * 64,
                    "metadata_sha256": sha256_file(metadata_path),
                }
            }

            with self.assertRaisesRegex(InputError, "basename"):
                resolve_source_bag_directory(
                    declared_source_bag=declared,
                    case_id=case_id,
                    annotation_manifest=manifest,
                )

    def test_rejects_unknown_collection_relocation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            case_id = "0704_6"
            declared = root / "unknown_collection" / "bags" / case_id
            manifest = {
                "source_bag": {
                    "directory": str(declared),
                    "mcap_file": "source.mcap",
                    "mcap_sha256": "a" * 64,
                    "metadata_sha256": "b" * 64,
                }
            }

            with self.assertRaisesRegex(InputError, "directory가 없습니다"):
                resolve_source_bag_directory(
                    declared_source_bag=declared,
                    case_id=case_id,
                    annotation_manifest=manifest,
                )

    def test_prefers_existing_declared_source_bag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            case_id = "0704_6"
            declared = (
                root
                / "0704_멀티모달_ROS2_MCAP_v1.0.0"
                / "bags"
                / case_id
            )
            declared.mkdir(parents=True)
            metadata_path = declared / "metadata.yaml"
            metadata_path.write_text("metadata\n")
            mcap_path = declared / "source.mcap"
            mcap_path.write_bytes(b"mcap")
            relocated = root / "0704_rosbag2" / "bags" / case_id
            relocated.mkdir(parents=True)
            (relocated / "metadata.yaml").write_text("other metadata\n")
            (relocated / "source.mcap").write_bytes(b"other mcap")
            manifest = {
                "source_bag": {
                    "directory": str(declared),
                    "mcap_file": "source.mcap",
                    "mcap_sha256": sha256_file(mcap_path),
                    "metadata_sha256": sha256_file(metadata_path),
                }
            }

            resolved = resolve_source_bag_directory(
                declared_source_bag=declared,
                case_id=case_id,
                annotation_manifest=manifest,
            )

            self.assertEqual(declared.resolve(), resolved)


class ReviewStoreTest(unittest.TestCase):
    def make_case(
        self,
        root: Path,
        *,
        candidates: list[dict] | None,
        stream_kind: str = "interaction",
        gaps: list[dict] | None = None,
    ) -> tuple[ReviewStore, Path, Path]:
        case_dir = root / "0704_6"
        case_dir.mkdir()
        timeline_path = case_dir / "cam4_frame_timeline.v1.json"
        timeline_path.write_text(
            json.dumps(
                {
                    "schema": "taskplanner.video_frame_timeline.v1",
                    "case_id": "0704_6",
                    "source_bag": "/fixture/0704_6",
                    "source_fps": 10.0,
                    "frame_count": 3,
                    "start_sec": 0.0,
                    "end_sec": 0.2,
                    "timestamps_sec": [0.0, 0.1, 0.2],
                    "gaps": gaps or [],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        candidates_path = (
            case_dir / "interaction_candidates.ai_review.v1.jsonl"
        )
        if candidates is not None:
            candidates_path.write_text(
                "".join(
                    json.dumps(record, sort_keys=True) + "\n"
                    for record in candidates
                ),
                encoding="utf-8",
            )
        decisions_path = case_dir / "human_review_decisions.v1.jsonl"
        store = ReviewStore(
            case_dir=case_dir,
            candidates_path=candidates_path,
            timeline_path=timeline_path,
            decisions_path=decisions_path,
            stream_kind=stream_kind,
        )
        return store, candidates_path, decisions_path

    @staticmethod
    def transfer_candidate() -> dict:
        return {
            "schema": "taskplanner.observable_interaction_point.v1",
            "case_id": "0704_6",
            "event_id": "0704_6-T0001",
            "event_type": "tool_transfer",
            "time_sec": 0.1,
            "source_frame_idx": 1,
            "source_views": ["cam4", "flir"],
            "tool": "bipolar_forceps",
            "from": "scrub_nurse",
            "to": "surgeon",
            "review_status": "proposed",
            "label_origin": "assistant_visual_proposal",
            "ai_review": {
                "reviewer_model": "gpt-5.6-sol",
                "decision": "recommend",
                "reviewed_at": "2026-07-28T00:00:00+00:00",
                "evidence": "fixture evidence",
            },
        }

    @staticmethod
    def request_candidate() -> dict:
        return {
            "schema": "taskplanner.observable_interaction_point.v1",
            "case_id": "0704_6",
            "event_id": "0704_6-R0001",
            "event_type": "implicit_tool_request",
            "time_sec": 0.1,
            "source_frame_idx": 1,
            "source_views": ["cam4", "flir"],
            "tool": None,
            "from": None,
            "to": None,
            "review_status": "proposed",
            "label_origin": "assistant_visual_proposal",
            "ai_review": {
                "reviewer_model": "NemoStation/Marlin-2B",
                "decision": "recommend",
                "reviewed_at": "2026-07-28T00:00:00+00:00",
                "evidence": "fixture evidence",
            },
        }

    def decision_payload(
        self,
        store: ReviewStore,
        *,
        status: str = "confirmed",
        frame_idx: int = 2,
    ) -> dict:
        state = store.state()
        candidate = state["candidates"][0]
        return {
            "revision": state["revision"],
            "candidate_id": candidate["_review_ui"]["candidate_id"],
            "candidate_sha256": candidate["_review_ui"]["candidate_sha256"],
            "reviewer_id": "fixture-reviewer",
            "review_status": status,
            "notes": "fixture decision",
            "adjudicated_fields": {
                "event_type": "tool_transfer",
                "source_frame_idx": frame_idx,
                "tool": "bipolar_forceps",
                "from": "scrub_nurse",
                "to": "surgeon",
                "phase_id": None,
            },
        }

    def test_missing_candidate_file_is_empty_and_does_not_create_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, _candidates_path, decisions_path = self.make_case(
                Path(temporary),
                candidates=None,
            )
            state = store.state()
            self.assertEqual("missing", state["candidate_source"]["status"])
            self.assertEqual([], state["candidates"])
            self.assertFalse(decisions_path.exists())

    def test_confirm_is_append_only_human_origin_and_canonical_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, candidates_path, decisions_path = self.make_case(
                Path(temporary),
                candidates=[self.transfer_candidate()],
            )
            original_candidates = candidates_path.read_bytes()
            payload = self.decision_payload(store)
            result = store.save_decision(payload)

            self.assertFalse(result["idempotent"])
            decision = result["decision"]
            self.assertEqual("confirmed", decision["review_status"])
            self.assertEqual(
                "human_video_review",
                decision["resulting_label_origin"],
            )
            self.assertEqual("human", decision["review"]["reviewer_kind"])
            self.assertEqual(
                0.2,
                decision["adjudicated_fields"]["time_sec"],
            )
            self.assertEqual(original_candidates, candidates_path.read_bytes())
            self.assertEqual(1, len(decisions_path.read_text().splitlines()))

    def test_actual_source_view_subset_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, _candidates_path, decisions_path = self.make_case(
                Path(temporary),
                candidates=[self.transfer_candidate()],
            )
            payload = self.decision_payload(store)
            payload["adjudicated_fields"]["source_views"] = ["cam2", "cam1"]
            decision = store.save_decision(payload)["decision"]
            self.assertEqual(
                ["cam1", "cam2"],
                decision["adjudicated_fields"]["source_views"],
            )

            retry = store.save_decision(payload)
            self.assertTrue(retry["idempotent"])
            self.assertEqual(
                decision["decision_id"],
                retry["decision"]["decision_id"],
            )
            self.assertEqual(1, len(decisions_path.read_text().splitlines()))

    def test_different_second_decision_is_rejected_without_append(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, _candidates_path, decisions_path = self.make_case(
                Path(temporary),
                candidates=[self.transfer_candidate()],
            )
            store.save_decision(self.decision_payload(store))
            changed = self.decision_payload(store, status="rejected")
            with self.assertRaisesRegex(ConflictError, "이미 다른 판정"):
                store.save_decision(changed)
            self.assertEqual(1, len(decisions_path.read_text().splitlines()))

    def test_nonconfirmed_decision_has_no_resulting_label_origin(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, _candidates_path, _decisions_path = self.make_case(
                Path(temporary),
                candidates=[self.transfer_candidate()],
            )
            result = store.save_decision(
                self.decision_payload(store, status="ambiguous")
            )
            self.assertIsNone(result["decision"]["resulting_label_origin"])

    def test_invalid_transfer_is_not_appended(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, _candidates_path, decisions_path = self.make_case(
                Path(temporary),
                candidates=[self.transfer_candidate()],
            )
            payload = self.decision_payload(store)
            payload["adjudicated_fields"]["to"] = "scrub_nurse"
            with self.assertRaisesRegex(InputError, "from과 to"):
                store.save_decision(payload)
            self.assertFalse(decisions_path.exists())

    def test_phase_stream_rejects_nonphase_candidate_and_decision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(InputError, "phase stream"):
                store, _candidates_path, _decisions_path = self.make_case(
                    Path(temporary),
                    candidates=[self.transfer_candidate()],
                    stream_kind="phase",
                )
                store.state()

    def test_tampered_decision_digest_is_rejected_on_reload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, _candidates_path, decisions_path = self.make_case(
                Path(temporary),
                candidates=[self.transfer_candidate()],
            )
            store.save_decision(self.decision_payload(store))
            decision = json.loads(decisions_path.read_text(encoding="utf-8"))
            decision["candidate_sha256"] = "0" * 64
            decisions_path.write_text(
                json.dumps(decision) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(InputError, "candidate digest"):
                store.state()

    def test_timeline_action_supersedes_legacy_without_overwriting_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, _candidates_path, decisions_path = self.make_case(
                Path(temporary),
                candidates=[self.transfer_candidate()],
            )
            legacy_payload = self.decision_payload(
                store,
                status="rejected",
                frame_idx=1,
            )
            legacy = store.save_decision(legacy_payload)["decision"]
            original_legacy_bytes = decisions_path.read_bytes()
            state = store.state()
            candidate = state["candidates"][0]
            result = store.save_timeline_action(
                {
                    "operation": "review_candidate",
                    "revision": state["revision"],
                    "candidate_id": candidate["_review_ui"]["candidate_id"],
                    "candidate_sha256": candidate["_review_ui"][
                        "candidate_sha256"
                    ],
                    "supersedes_action_id": legacy["decision_id"],
                    "reviewer_id": "fixture-reviewer",
                    "review_status": "confirmed",
                    "notes": "actual event is later",
                    "adjudicated_fields": {
                        "event_type": "tool_transfer",
                        "source_frame_idx": 2,
                        "tool": "bipolar_forceps",
                        "from": "scrub_nurse",
                        "to": "surgeon",
                        "phase_id": None,
                    },
                }
            )

            self.assertEqual(original_legacy_bytes, decisions_path.read_bytes())
            self.assertEqual(
                legacy["decision_id"],
                result["action"]["supersedes_action_id"],
            )
            reviewed = result["state"]["candidates"][0]["_review_ui"]
            self.assertEqual("rejected", reviewed["legacy_decision"]["review_status"])
            self.assertEqual(
                "confirmed",
                reviewed["effective_decision"]["review_status"],
            )
            self.assertEqual(
                0.2,
                reviewed["effective_decision"]["adjudicated_fields"]["time_sec"],
            )
            self.assertEqual(2, len(reviewed["action_history"]))

    def test_create_human_request_interval_is_idempotent_and_renderable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, _candidates_path, _decisions_path = self.make_case(
                Path(temporary),
                candidates=[self.transfer_candidate()],
            )
            payload = {
                "operation": "create_annotation",
                "client_request_id": "fixture-create-0001",
                "revision": store.state()["revision"],
                "reviewer_id": "fixture-reviewer",
                "review_status": "confirmed",
                "notes": "missed open-hand request",
                "adjudicated_fields": {
                    "event_type": "implicit_tool_request",
                    "source_frame_idx": 1,
                    "start_source_frame_idx": 1,
                    "end_source_frame_idx": 2,
                },
            }
            result = store.save_timeline_action(payload)
            annotation_id = result["action"]["annotation_id"]
            self.assertEqual("0704_6-R0001", annotation_id)
            self.assertEqual(
                annotation_id,
                result["state"]["human_annotations"][0]["event_id"],
            )
            self.assertEqual(
                "taskplanner.observable_interaction_interval.v1",
                result["state"]["human_annotations"][0]["schema"],
            )
            self.assertEqual(
                0.1,
                result["state"]["human_annotations"][0]["time_sec"],
            )
            self.assertEqual(
                0.2,
                result["state"]["human_annotations"][0]["end_sec"],
            )

            retry = store.save_timeline_action(payload)
            self.assertTrue(retry["idempotent"])
            self.assertEqual(
                result["action"]["action_id"],
                retry["action"]["action_id"],
            )
            self.assertEqual(
                1,
                len(store.timeline_actions_path.read_text().splitlines()),
            )

    def test_human_annotation_rejected_revision_is_append_only_withdrawal(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, _candidates_path, _decisions_path = self.make_case(
                Path(temporary),
                candidates=[self.transfer_candidate()],
            )
            create_result = store.save_timeline_action(
                {
                    "operation": "create_annotation",
                    "client_request_id": "fixture-create-withdraw-0001",
                    "revision": store.state()["revision"],
                    "reviewer_id": "fixture-reviewer",
                    "review_status": "confirmed",
                    "notes": "human-created transfer",
                    "adjudicated_fields": {
                        "event_type": "tool_transfer",
                        "source_frame_idx": 2,
                        "tool": "bipolar_forceps",
                        "from": "scrub_nurse",
                        "to": "surgeon",
                        "phase_id": None,
                    },
                }
            )
            annotation_id = create_result["action"]["annotation_id"]
            create_action_id = create_result["action"]["action_id"]
            original_first_line = store.timeline_actions_path.read_bytes()

            withdraw_result = store.save_timeline_action(
                {
                    "operation": "revise_annotation",
                    "revision": create_result["state"]["revision"],
                    "annotation_id": annotation_id,
                    "supersedes_action_id": create_action_id,
                    "reviewer_id": "fixture-reviewer",
                    "review_status": "rejected",
                    "notes": "withdraw human-created event",
                    "adjudicated_fields": {
                        "event_type": "tool_transfer",
                        "source_frame_idx": 2,
                        "tool": "bipolar_forceps",
                        "from": "scrub_nurse",
                        "to": "surgeon",
                        "phase_id": None,
                    },
                }
            )

            self.assertTrue(
                store.timeline_actions_path.read_bytes().startswith(
                    original_first_line
                )
            )
            self.assertEqual(
                2,
                len(store.timeline_actions_path.read_text().splitlines()),
            )
            withdrawn = withdraw_result["state"]["human_annotations"][0]
            self.assertEqual(annotation_id, withdrawn["event_id"])
            self.assertEqual("rejected", withdrawn["review_status"])
            self.assertIsNone(withdrawn["label_origin"])
            self.assertEqual(
                2,
                len(withdrawn["_review_ui"]["action_history"]),
            )

    def test_request_candidate_review_canonicalizes_start_and_end(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, _candidates_path, _decisions_path = self.make_case(
                Path(temporary),
                candidates=[self.request_candidate()],
            )
            state = store.state()
            candidate = state["candidates"][0]
            result = store.save_timeline_action(
                {
                    "operation": "review_candidate",
                    "revision": state["revision"],
                    "candidate_id": candidate["_review_ui"]["candidate_id"],
                    "candidate_sha256": candidate["_review_ui"][
                        "candidate_sha256"
                    ],
                    "supersedes_action_id": None,
                    "reviewer_id": "fixture-reviewer",
                    "review_status": "confirmed",
                    "notes": "open palm remains extended",
                    "adjudicated_fields": {
                        "event_type": "implicit_tool_request",
                        "source_frame_idx": 1,
                        "start_source_frame_idx": 1,
                        "end_source_frame_idx": 2,
                    },
                }
            )
            fields = result["action"]["adjudicated_fields"]
            self.assertEqual(1, fields["source_frame_idx"])
            self.assertEqual(0.1, fields["time_sec"])
            self.assertEqual(1, fields["start_source_frame_idx"])
            self.assertEqual(2, fields["end_source_frame_idx"])
            self.assertEqual(0.1, fields["start_sec"])
            self.assertEqual(0.2, fields["end_sec"])
            self.assertIsNone(fields["tool"])

    def test_request_interval_supersedes_legacy_point_decision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, _candidates_path, decisions_path = self.make_case(
                Path(temporary),
                candidates=[self.request_candidate()],
            )
            candidate = store.state()["candidates"][0]
            legacy = store.save_decision(
                {
                    "revision": store.state()["revision"],
                    "candidate_id": candidate["_review_ui"]["candidate_id"],
                    "candidate_sha256": candidate["_review_ui"][
                        "candidate_sha256"
                    ],
                    "reviewer_id": "fixture-reviewer",
                    "review_status": "rejected",
                    "notes": "proposal point is before the request",
                    "adjudicated_fields": {
                        "event_type": "implicit_tool_request",
                        "source_frame_idx": 1,
                    },
                }
            )["decision"]
            original_legacy_bytes = decisions_path.read_bytes()
            state = store.state()
            candidate = state["candidates"][0]
            result = store.save_timeline_action(
                {
                    "operation": "review_candidate",
                    "revision": state["revision"],
                    "candidate_id": candidate["_review_ui"]["candidate_id"],
                    "candidate_sha256": candidate["_review_ui"][
                        "candidate_sha256"
                    ],
                    "supersedes_action_id": legacy["decision_id"],
                    "reviewer_id": "fixture-reviewer",
                    "review_status": "confirmed",
                    "notes": "visible request interval starts later",
                    "adjudicated_fields": {
                        "event_type": "implicit_tool_request",
                        "start_source_frame_idx": 1,
                        "end_source_frame_idx": 2,
                    },
                }
            )
            self.assertEqual(original_legacy_bytes, decisions_path.read_bytes())
            review_ui = result["state"]["candidates"][0]["_review_ui"]
            self.assertEqual(
                "rejected",
                review_ui["legacy_decision"]["review_status"],
            )
            self.assertEqual(
                2,
                review_ui["effective_decision"]["adjudicated_fields"][
                    "end_source_frame_idx"
                ],
            )
            self.assertEqual(2, len(review_ui["action_history"]))

    def test_request_interval_end_cannot_precede_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, _candidates_path, _decisions_path = self.make_case(
                Path(temporary),
                candidates=[self.request_candidate()],
            )
            with self.assertRaisesRegex(InputError, "종료는 시작보다"):
                store.save_timeline_action(
                    {
                        "operation": "create_annotation",
                        "client_request_id": "fixture-reversed-request",
                        "revision": store.state()["revision"],
                        "reviewer_id": "fixture-reviewer",
                        "review_status": "confirmed",
                        "notes": "",
                        "adjudicated_fields": {
                            "event_type": "implicit_tool_request",
                            "start_source_frame_idx": 2,
                            "end_source_frame_idx": 1,
                        },
                    }
                )
            self.assertFalse(store.timeline_actions_path.exists())

    def test_request_interval_cannot_span_camera_gap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, _candidates_path, _decisions_path = self.make_case(
                Path(temporary),
                candidates=[self.request_candidate()],
                gaps=[
                    {
                        "before_frame_idx": 0,
                        "before_time_sec": 0.0,
                        "after_frame_idx": 2,
                        "after_time_sec": 0.2,
                        "delta_sec": 0.2,
                    }
                ],
            )
            with self.assertRaisesRegex(InputError, "gap을 가로지를"):
                store.save_timeline_action(
                    {
                        "operation": "create_annotation",
                        "client_request_id": "fixture-spanning-request",
                        "revision": store.state()["revision"],
                        "reviewer_id": "fixture-reviewer",
                        "review_status": "confirmed",
                        "notes": "",
                        "adjudicated_fields": {
                            "event_type": "implicit_tool_request",
                            "start_source_frame_idx": 0,
                            "end_source_frame_idx": 2,
                        },
                    }
                )
            self.assertFalse(store.timeline_actions_path.exists())

    def test_tool_transfer_rejects_interval_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, _candidates_path, _decisions_path = self.make_case(
                Path(temporary),
                candidates=[self.transfer_candidate()],
            )
            state = store.state()
            candidate = state["candidates"][0]
            with self.assertRaisesRegex(
                InputError,
                "implicit_tool_request에만",
            ):
                store.save_timeline_action(
                    {
                        "operation": "review_candidate",
                        "revision": state["revision"],
                        "candidate_id": candidate["_review_ui"]["candidate_id"],
                        "candidate_sha256": candidate["_review_ui"][
                            "candidate_sha256"
                        ],
                        "supersedes_action_id": None,
                        "reviewer_id": "fixture-reviewer",
                        "review_status": "confirmed",
                        "notes": "",
                        "adjudicated_fields": {
                            "event_type": "tool_transfer",
                            "source_frame_idx": 1,
                            "start_source_frame_idx": 1,
                            "end_source_frame_idx": 2,
                            "tool": "bipolar_forceps",
                            "from": "scrub_nurse",
                            "to": "surgeon",
                        },
                    }
                )
            self.assertFalse(store.timeline_actions_path.exists())

    def test_request_interval_schema_accepts_confirmed_record(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        schema = json.loads(
            (
                repo_root
                / "annotations/observable_tool_events/schema"
                / "observable_interaction_interval.v1.schema.json"
            ).read_text(encoding="utf-8")
        )
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.Draft202012Validator(
            schema,
            format_checker=jsonschema.FormatChecker(),
        ).validate(
            {
                "schema": "taskplanner.observable_interaction_interval.v1",
                "case_id": "0704_6",
                "event_id": "0704_6-R0001",
                "event_type": "implicit_tool_request",
                "source_frame_idx": 1,
                "time_sec": 0.1,
                "start_source_frame_idx": 1,
                "end_source_frame_idx": 2,
                "start_sec": 0.1,
                "end_sec": 0.2,
                "source_views": ["cam4", "flir"],
                "review_status": "confirmed",
                "label_origin": "human_video_review",
                "review": {
                    "reviewer_kind": "human",
                    "reviewer_id": "fixture-reviewer",
                    "reviewed_at": "2026-07-28T00:00:00+00:00",
                    "notes": "visible open-hand interval",
                },
            }
        )

    def test_wrong_supersede_is_rejected_without_action_append(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, _candidates_path, _decisions_path = self.make_case(
                Path(temporary),
                candidates=[self.transfer_candidate()],
            )
            state = store.state()
            candidate = state["candidates"][0]
            with self.assertRaisesRegex(ConflictError, "최신 판정"):
                store.save_timeline_action(
                    {
                        "operation": "review_candidate",
                        "revision": state["revision"],
                        "candidate_id": candidate["_review_ui"]["candidate_id"],
                        "candidate_sha256": candidate["_review_ui"][
                            "candidate_sha256"
                        ],
                        "supersedes_action_id": "0704_6-H9999",
                        "reviewer_id": "fixture-reviewer",
                        "review_status": "confirmed",
                        "notes": "",
                        "adjudicated_fields": {
                            "event_type": "tool_transfer",
                            "source_frame_idx": 1,
                            "tool": "bipolar_forceps",
                            "from": "scrub_nurse",
                            "to": "surgeon",
                        },
                    }
                )
            self.assertEqual(
                "",
                store.timeline_actions_path.read_text(encoding="utf-8"),
            )

    def test_gap_playhead_cannot_create_visual_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, _candidates_path, _decisions_path = self.make_case(
                Path(temporary),
                candidates=[self.transfer_candidate()],
                gaps=[
                    {
                        "before_frame_idx": 0,
                        "before_time_sec": 0.0,
                        "after_frame_idx": 1,
                        "after_time_sec": 0.1,
                        "delta_sec": 0.1,
                    }
                ],
            )
            with self.assertRaisesRegex(InputError, "gap"):
                store.save_timeline_action(
                    {
                        "operation": "create_annotation",
                        "client_request_id": "fixture-gap-0001",
                        "revision": store.state()["revision"],
                        "playhead_time_sec": 0.05,
                        "reviewer_id": "fixture-reviewer",
                        "review_status": "confirmed",
                        "notes": "",
                        "adjudicated_fields": {
                            "event_type": "implicit_tool_request",
                            "source_frame_idx": 1,
                        },
                    }
                )
            self.assertFalse(store.timeline_actions_path.exists())

    def test_single_byte_range_parser(self) -> None:
        self.assertIsNone(parse_single_byte_range(None, size=10))
        self.assertEqual((2, 5), parse_single_byte_range("bytes=2-5", size=10))
        self.assertEqual((7, 9), parse_single_byte_range("bytes=-3", size=10))
        self.assertEqual((8, 9), parse_single_byte_range("bytes=8-", size=10))
        with self.assertRaises(InputError):
            parse_single_byte_range("bytes=20-30", size=10)
        with self.assertRaises(InputError):
            parse_single_byte_range("bytes=0-1,4-5", size=10)

    def test_media_endpoint_supports_range_head_and_416(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store, _candidates_path, _decisions_path = self.make_case(
                root,
                candidates=[self.transfer_candidate()],
            )
            static_dir = root / "static"
            static_dir.mkdir()
            (static_dir / "index.html").write_text("fixture", encoding="utf-8")
            media_path = root / "review.mp4"
            media_path.write_bytes(bytes(range(100)))
            handler = make_handler(
                store=store,
                frames=object(),  # /api/frame is not used by this test.
                static_dir=static_dir,
                media_path=media_path,
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = (
                f"http://127.0.0.1:{server.server_address[1]}"
                "/api/media/review.mp4"
            )
            try:
                request = urllib.request.Request(
                    url,
                    headers={"Range": "bytes=10-19"},
                )
                with urllib.request.urlopen(request) as response:
                    self.assertEqual(206, response.status)
                    self.assertEqual("bytes 10-19/100", response.headers["Content-Range"])
                    self.assertEqual(bytes(range(10, 20)), response.read())

                request = urllib.request.Request(url, method="HEAD")
                with urllib.request.urlopen(request) as response:
                    self.assertEqual(200, response.status)
                    self.assertEqual("100", response.headers["Content-Length"])
                    self.assertEqual(b"", response.read())

                request = urllib.request.Request(
                    url,
                    headers={"Range": "bytes=1000-"},
                )
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(request)
                self.assertEqual(416, context.exception.code)
                self.assertEqual(
                    "bytes */100",
                    context.exception.headers["Content-Range"],
                )
                context.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_multi_case_catalog_routes_state_media_and_rejects_unsafe_writes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def make_runtime(case_id: str, media: bytes) -> ReviewCaseRuntime:
                case_dir = root / case_id
                case_dir.mkdir()
                timeline_path = case_dir / "cam4_frame_timeline.v1.json"
                timeline_path.write_text(
                    json.dumps(
                        {
                            "schema": "taskplanner.video_frame_timeline.v1",
                            "case_id": case_id,
                            "source_bag": f"/fixture/{case_id}",
                            "source_fps": 10.0,
                            "frame_count": 3,
                            "start_sec": 0.0,
                            "end_sec": 0.2,
                            "timestamps_sec": [0.0, 0.1, 0.2],
                            "gaps": [],
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                candidates_path = (
                    case_dir / "interaction_candidates.ai_review.v1.jsonl"
                )
                candidates_path.write_text("", encoding="utf-8")
                media_path = case_dir / "review.mp4"
                media_path.write_bytes(media)
                store = ReviewStore(
                    case_dir=case_dir,
                    candidates_path=candidates_path,
                    timeline_path=timeline_path,
                    decisions_path=case_dir / "human_review_decisions.v1.jsonl",
                    timeline_actions_path=(
                        case_dir / "human_timeline_actions.v1.jsonl"
                    ),
                    review_media_path=media_path,
                    stream_kind="interaction",
                )
                return ReviewCaseRuntime.build(
                    store=store,
                    frames=object(),
                    media_path=media_path,
                )

            first = make_runtime("0704_6", b"first-case")
            second = make_runtime("0704_7", b"second-case-media")
            static_dir = root / "static"
            static_dir.mkdir()
            (static_dir / "index.html").write_text("fixture", encoding="utf-8")
            handler = make_handler(
                store=first.store,
                frames=first.frames,
                static_dir=static_dir,
                media_path=first.media_path,
                case_runtimes={
                    first.store.case_id: first,
                    second.store.case_id: second,
                },
                default_case_id="0704_6",
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with urllib.request.urlopen(f"{base_url}/api/cases") as response:
                    catalog = json.load(response)
                self.assertEqual(2, catalog["case_count"])
                self.assertEqual(
                    ["0704_6", "0704_7"],
                    [item["case_id"] for item in catalog["cases"]],
                )

                with urllib.request.urlopen(
                    f"{base_url}/api/state?case=0704_7"
                ) as response:
                    state = json.load(response)
                self.assertEqual("0704_7", state["case_id"])
                self.assertEqual("0704_7", state["active_case_id"])
                self.assertEqual(2, len(state["available_cases"]))
                self.assertEqual(
                    "/api/media/review.mp4?case=0704_7",
                    state["media"]["video_url"],
                )

                media_request = urllib.request.Request(
                    f"{base_url}/api/media/review.mp4?case=0704_7",
                    headers={"Range": "bytes=0-5"},
                )
                with urllib.request.urlopen(media_request) as response:
                    self.assertEqual(206, response.status)
                    self.assertEqual(b"second", response.read())

                for target in (
                    f"{base_url}/api/state?case=missing_case",
                    f"{base_url}/api/media/review.mp4?case=missing_case",
                ):
                    with self.assertRaises(urllib.error.HTTPError) as context:
                        urllib.request.urlopen(target)
                    self.assertEqual(404, context.exception.code)
                    context.exception.close()

                unsafe_requests = (
                    urllib.request.Request(
                        f"{base_url}/api/annotation-action",
                        data=b"{}",
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    ),
                    urllib.request.Request(
                        (
                            f"{base_url}/api/annotation-action"
                            "?case=0704_7"
                        ),
                        data=json.dumps({"case_id": "0704_6"}).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    ),
                )
                for request in unsafe_requests:
                    with self.assertRaises(urllib.error.HTTPError) as context:
                        urllib.request.urlopen(request)
                    self.assertEqual(400, context.exception.code)
                    context.exception.close()
                self.assertFalse(
                    (first.store.case_dir / "human_timeline_actions.v1.jsonl").exists()
                )
                self.assertFalse(
                    (second.store.case_dir / "human_timeline_actions.v1.jsonl").exists()
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_independent_multiview_media_routes_and_range_requests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store, _candidates_path, _decisions_path = self.make_case(
                root,
                candidates=[],
            )
            media_paths: dict[str, Path] = {}
            expected_payloads = {
                "cam4": b"cam4-master-audio",
                "flir": b"flir-independent",
                "cam2": b"cam2-independent",
                "cam3": b"cam3-independent",
            }
            for view, payload in expected_payloads.items():
                path = root / f"review_{view}.mp4"
                path.write_bytes(payload)
                media_paths[view] = path
            runtime = ReviewCaseRuntime.build(
                store=store,
                frames=object(),
                media_paths=media_paths,
            )
            self.assertTrue(runtime.multiview_available)
            static_dir = root / "static"
            static_dir.mkdir()
            (static_dir / "index.html").write_text(
                "fixture",
                encoding="utf-8",
            )
            handler = make_handler(
                store=store,
                frames=object(),
                static_dir=static_dir,
                case_runtimes={store.case_id: runtime},
                default_case_id=store.case_id,
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with urllib.request.urlopen(f"{base_url}/api/state") as response:
                    payload = json.load(response)
                self.assertEqual("cam4", payload["media"]["master_view"])
                self.assertTrue(payload["media"]["multiview_available"])
                self.assertEqual(
                    {
                        "cam4": "/api/media/cam4.mp4",
                        "flir": "/api/media/flir.mp4",
                        "cam2": "/api/media/cam2.mp4",
                        "cam3": "/api/media/cam3.mp4",
                    },
                    {
                        view: descriptor["video_url"]
                        for view, descriptor in payload["media"][
                            "video_views"
                        ].items()
                    },
                )
                for view, expected in expected_payloads.items():
                    request = urllib.request.Request(
                        f"{base_url}/api/media/{view}.mp4",
                        headers={"Range": "bytes=0-3"},
                    )
                    with urllib.request.urlopen(request) as response:
                        self.assertEqual(206, response.status)
                        self.assertEqual(expected[:4], response.read())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


class ReviewMultiviewManifestTest(unittest.TestCase):
    def test_validates_four_independent_outputs_and_rejects_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            case_id = "0704_6"
            case_dir = root / case_id
            media_dir = root / "media" / case_id
            bag_dir = root / "bag"
            case_dir.mkdir()
            media_dir.mkdir(parents=True)
            bag_dir.mkdir()
            timeline_path = case_dir / "cam4_frame_timeline.v1.json"
            timeline_path.write_text(
                json.dumps({"case_id": case_id, "timestamps_sec": [0.0, 0.1]})
                + "\n",
                encoding="utf-8",
            )
            mcap_path = bag_dir / "source.mcap"
            mcap_path.write_bytes(b"mcap")
            outputs: dict[str, dict] = {}
            for view in ("cam4", "flir", "cam2", "cam3"):
                path = media_dir / f"review_{view}.mp4"
                path.write_bytes(f"{view}-video".encode())
                outputs[view] = {
                    "path": str(path),
                    "size_bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                    "has_audio": view == "cam4",
                    "media_probe": {"container_duration_sec": 12.5},
                }
            manifest_path = media_dir / "review_multiview.manifest.json"
            manifest = {
                "schema": "taskplanner.review_multiview_proxy_manifest.v1",
                "case_id": case_id,
                "master_view": "cam4",
                "view_order": ["cam4", "flir", "cam2", "cam3"],
                "inputs": {
                    "timeline": {
                        "path": str(timeline_path),
                        "sha256": sha256_file(timeline_path),
                    },
                    "source_mcap": {
                        "path": str(mcap_path),
                        "sha256": sha256_file(mcap_path),
                    },
                },
                "outputs": outputs,
            }
            manifest_path.write_text(
                json.dumps(manifest) + "\n",
                encoding="utf-8",
            )
            annotation_manifest = {
                "source_bag": {
                    "directory": str(bag_dir),
                    "mcap_file": mcap_path.name,
                    "mcap_sha256": sha256_file(mcap_path),
                }
            }
            duration, paths = validate_review_multiview_proxy(
                manifest_path=manifest_path,
                case_id=case_id,
                timeline_path=timeline_path,
                source_bag=bag_dir,
                annotation_manifest=annotation_manifest,
            )
            self.assertEqual(12.5, duration)
            self.assertEqual(
                set(("cam4", "flir", "cam2", "cam3")),
                set(paths),
            )

            (media_dir / "review_cam3.mp4").write_bytes(b"tampered")
            with self.assertRaises(InputError):
                validate_review_multiview_proxy(
                    manifest_path=manifest_path,
                    case_id=case_id,
                    timeline_path=timeline_path,
                    source_bag=bag_dir,
                    annotation_manifest=annotation_manifest,
                )


class FinalReviewBundleTest(unittest.TestCase):
    @staticmethod
    def _write_json(path: Path, value: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _write_jsonl(path: Path, records: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(
                json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
                for record in records
            ),
            encoding="utf-8",
        )

    def make_final_fixture(
        self,
        root: Path,
    ) -> tuple[FinalReviewBundle, Path, Path, Path, Path]:
        event_root = root / "observable_tool_events"
        case_dir = event_root / "cases" / "0704_6"
        report_dir = event_root / "reports"
        schema_dir = event_root / "schema"
        case_dir.mkdir(parents=True)
        report_dir.mkdir()
        schema_dir.mkdir()

        timeline_path = case_dir / "cam4_frame_timeline.v1.json"
        self._write_json(
            timeline_path,
            {
                "schema": "taskplanner.video_frame_timeline.v1",
                "case_id": "0704_6",
                "source_bag": "/fixture/0704_6",
                "source_fps": 10.0,
                "frame_count": 3,
                "start_sec": 0.0,
                "end_sec": 0.2,
                "timestamps_sec": [0.0, 0.1, 0.2],
                "gaps": [],
            },
        )
        adjudication_path = (
            case_dir / "assistant_interaction_adjudications.final.v1.jsonl"
        )
        self._write_jsonl(
            adjudication_path,
            [{"schema": "fixture.assistant_adjudication.v1"}],
        )
        projection_policy_path = case_dir / "dt_projection.explicit.v1.json"
        self._write_json(
            projection_policy_path,
            {"schema": "fixture.explicit_projection.v1"},
        )
        masks_path = case_dir / "evaluation_masks.v1.json"
        self._write_json(masks_path, {"schema": "fixture.evaluation_masks.v1"})
        reconciliation_path = (
            case_dir / "policy02_reconciliation_audit.final.v1.json"
        )
        self._write_json(
            reconciliation_path,
            {"schema": "fixture.policy02_reconciliation.v1"},
        )
        observed_path = case_dir / "interaction_events.observed.final.v3.jsonl"
        dt_path = case_dir / "interaction_events.dt_reference.final.v3.jsonl"
        observed = [
            {
                "schema": "taskplanner.observable_interaction_interval.v1",
                "case_id": "0704_6",
                "event_id": "0704_6-R0001",
                "event_type": "implicit_tool_request",
                "source_frame_idx": 0,
                "start_source_frame_idx": 0,
                "end_source_frame_idx": 1,
                "time_sec": 0.0,
                "start_sec": 0.0,
                "end_sec": 0.1,
                "source_views": ["cam4", "flir"],
                "review_status": "confirmed",
                "label_origin": "human_video_review",
            },
            {
                "schema": "taskplanner.observable_interaction_point.v1",
                "case_id": "0704_6",
                "event_id": "0704_6-T0001",
                "event_type": "tool_transfer",
                "source_frame_idx": 2,
                "time_sec": 0.2,
                "source_views": ["cam4", "flir"],
                "tool": "bovie",
                "from": "scrub_nurse",
                "to": "surgeon",
                "review_status": "confirmed",
                "label_origin": "assistant_video_adjudication",
            },
        ]
        projected = [dict(record) for record in observed]
        self._write_jsonl(observed_path, observed)
        self._write_jsonl(dt_path, projected)
        speech_schema_path = (
            schema_dir / "observable_voice_point.v1.schema.json"
        )
        self._write_json(
            speech_schema_path,
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": "taskplanner.observable_voice_point.v1",
                "type": "object",
            },
        )
        speech_path = case_dir / "voice_events.source.v1.jsonl"
        self._write_jsonl(
            speech_path,
            [
                {
                    "schema": "taskplanner.observable_voice_point.v1",
                    "case_id": "0704_6",
                    "event_id": "0704_6-V0001",
                    "event_type": "voice_utterance",
                    "time_sec": 0.15,
                    "end_sec": 0.18,
                    "text": "Adson 하나 더",
                    "source_topic": "/surgery/transcript",
                    "source_record_timestamp_ns": 150_000_000,
                    "source_message_index": 1,
                    "source_wav": "fixture.wav",
                    "source_authority": "public_runtime_transcript",
                    "scoring_role": "context_only_not_ground_truth",
                }
            ],
        )
        phase_catalog_path = case_dir / "procedure_phases.ai_review.v1.yaml"
        phase_catalog_path.write_text(
            "schema: fixture.phase_catalog.v1\n",
            encoding="utf-8",
        )
        phase_candidates_path = (
            case_dir / "phase_candidates.ai_review.v1.jsonl"
        )
        phase_candidates = [
            {
                "schema": "taskplanner.observable_interaction_point.v1",
                "case_id": "0704_6",
                "event_id": "0704_6-PH0001",
                "event_type": "phase_start",
                "source_frame_idx": 0,
                "time_sec": 0.0,
                "source_views": ["cam4", "flir"],
                "phase_id": "P03",
                "phase_boundary_kind": "clip_initial_state",
                "review_status": "proposed",
                "label_origin": "assistant_visual_proposal",
            },
            {
                "schema": "taskplanner.observable_interaction_point.v1",
                "case_id": "0704_6",
                "event_id": "0704_6-PH0002",
                "event_type": "phase_start",
                "source_frame_idx": 2,
                "time_sec": 0.2,
                "source_views": ["cam4", "flir"],
                "phase_id": "P04",
                "phase_boundary_kind": "uncertain_transition",
                "review_status": "ambiguous",
                "label_origin": "assistant_visual_proposal",
            },
        ]
        self._write_jsonl(phase_candidates_path, phase_candidates)
        phase_actions = []
        for action_number, (candidate, status) in enumerate(
            zip(phase_candidates, ("ambiguous", "rejected")),
            1,
        ):
            fields = {
                "event_type": "phase_start",
                "source_frame_idx": candidate["source_frame_idx"],
                "time_sec": candidate["time_sec"],
                "tool": None,
                "from": None,
                "to": None,
                "phase_id": candidate["phase_id"],
                "source_views": candidate["source_views"],
            }
            review = {
                "reviewer_kind": "human",
                "reviewer_id": "fixture-reviewer",
                "reviewed_at": "2026-07-28T00:00:00+00:00",
                "notes": "",
            }
            semantic_request = {
                "operation": "review_candidate",
                "annotation_id": candidate["event_id"],
                "candidate_id": candidate["event_id"],
                "candidate_sha256": sha256_value(candidate),
                "supersedes_action_id": None,
                "client_request_id": None,
                "review_status": status,
                "reviewer_id": review["reviewer_id"],
                "notes": "",
                "adjudicated_fields": fields,
            }
            phase_actions.append(
                {
                    "schema": "taskplanner.timeline_annotation_action.v1",
                    "case_id": "0704_6",
                    "action_id": f"0704_6-A{action_number:04d}",
                    "operation": "review_candidate",
                    "annotation_id": candidate["event_id"],
                    "candidate_id": candidate["event_id"],
                    "candidate_sha256": sha256_value(candidate),
                    "supersedes_action_id": None,
                    "client_request_id": None,
                    "review_status": status,
                    "resulting_label_origin": None,
                    "review": review,
                    "adjudicated_fields": fields,
                    "request_sha256": sha256_value(semantic_request),
                }
            )
        phase_actions_path = (
            case_dir / "phase_human_timeline_actions.fixture.v1.jsonl"
        )
        self._write_jsonl(phase_actions_path, phase_actions)
        provisional_phase_path = (
            case_dir / "phase_events.provisional.final.v1.jsonl"
        )
        self._write_jsonl(
            provisional_phase_path,
            [
                {
                    "schema": "taskplanner.observable_interaction_point.v1",
                    "case_id": "0704_6",
                    "event_id": "0704_6-PH0001",
                    "event_type": "phase_start",
                    "source_frame_idx": 0,
                    "time_sec": 0.0,
                    "source_views": ["cam4", "flir"],
                    "phase_id": "P03",
                    "phase_boundary_kind": "clip_initial_state",
                    "review_status": "ambiguous",
                    "label_origin": "human_video_review",
                    "review": phase_actions[0]["review"],
                }
            ],
        )

        report_path = report_dir / "0704_6_dt_projection.final.v3.json"
        report = {
            "schema": "taskplanner.dt_interaction_projection_report.v1",
            "case_id": "0704_6",
            "source_revision": "source-fixture",
            "adjudication_revision": "adjudication-fixture",
            "inputs": {
                "adjudications": sha256_file(adjudication_path),
                "projection": sha256_file(projection_policy_path),
            },
            "counts": {
                "observed_confirmed_count": 2,
                "dt_confirmed_count": 2,
            },
            "outputs": {
                "observed": {
                    "path": str(observed_path.resolve()),
                    "record_count": 2,
                    "sha256": sha256_file(observed_path),
                },
                "dt_reference": {
                    "path": str(dt_path.resolve()),
                    "record_count": 2,
                    "sha256": sha256_file(dt_path),
                },
            },
            "operations": {
                "collapsed_returns": [],
                "excluded_roundtrips": [],
                "excluded_unclosed_direct_returns": [],
                "source_mapping": [
                    {
                        "operation": "identity",
                        "source_event_ids": ["0704_6-R0001"],
                        "output_event_id": "0704_6-R0001",
                    },
                    {
                        "operation": "identity",
                        "source_event_ids": ["0704_6-T0001"],
                        "output_event_id": "0704_6-T0001",
                    },
                ],
            },
        }
        self._write_json(report_path, report)

        manifest_path = case_dir / "annotation_manifest.json"
        manifest = {
            "schema": "taskplanner.observable_annotation_manifest.v1",
            "case_id": "0704_6",
            "minimal_interaction_annotation": {
                "timeline_file": timeline_path.name,
                "timeline_sha256": sha256_file(timeline_path),
                "assistant_adjudication_file": adjudication_path.name,
                "assistant_adjudication_sha256": sha256_file(
                    adjudication_path
                ),
                "explicit_projection_file": projection_policy_path.name,
                "explicit_projection_sha256": sha256_file(
                    projection_policy_path
                ),
                "policy02_reconciliation_file": reconciliation_path.name,
                "policy02_reconciliation_sha256": sha256_file(
                    reconciliation_path
                ),
            },
            "evaluation_reference": {
                "complete": True,
                "phase_reference_included": True,
                "information_boundary": (
                    "evaluation_only_never_vlm_reducer_bt_runtime_input"
                ),
                "source_revision": "source-fixture",
                "adjudication_revision": "adjudication-fixture",
                "assistant_adjudication": {
                    "file": adjudication_path.name,
                    "sha256": sha256_file(adjudication_path),
                },
                "projection_policy_file": projection_policy_path.name,
                "projection_policy_sha256": sha256_file(
                    projection_policy_path
                ),
                "evaluation_masks": {
                    "file": masks_path.name,
                    "sha256": sha256_file(masks_path),
                },
                "observed_reference": {
                    "file": observed_path.name,
                    "sha256": sha256_file(observed_path),
                    "confirmed_event_count": 2,
                    "event_type_counts": {
                        "implicit_tool_request": 1,
                        "tool_transfer": 1,
                    },
                    "label_origin_counts": {
                        "assistant_video_adjudication": 1,
                        "human_video_review": 1,
                    },
                },
                "dt_reference": {
                    "file": dt_path.name,
                    "sha256": sha256_file(dt_path),
                    "confirmed_event_count": 2,
                    "event_type_counts": {
                        "implicit_tool_request": 1,
                        "tool_transfer": 1,
                    },
                },
                "projection_report_file": (
                    "../../reports/0704_6_dt_projection.final.v3.json"
                ),
                "projection_report_sha256": sha256_file(report_path),
            },
            "speech_timeline": {
                "authority": (
                    "source_bag_public_transcript_not_evaluation_ground_truth"
                ),
                "event_count": 1,
                "file": speech_path.name,
                "schema_file": (
                    "../../schema/observable_voice_point.v1.schema.json"
                ),
                "schema_sha256": sha256_file(speech_schema_path),
                "scoring_role": "context_only_not_ground_truth",
                "sha256": sha256_file(speech_path),
                "source_topic": "/surgery/transcript",
                "timeline_geometry": "point_at_source_timestamp",
            },
            "phase_annotation": {
                "authority": (
                    "direct_human_review_provisional_context_not_scoring_ground_truth"
                ),
                "candidate_file": phase_candidates_path.name,
                "candidate_sha256": sha256_file(phase_candidates_path),
                "complete": True,
                "effective_review_status_counts": {
                    "ambiguous": 1,
                    "confirmed": 0,
                    "rejected": 1,
                },
                "human_decision_file": phase_actions_path.name,
                "human_decision_sha256": sha256_file(phase_actions_path),
                "procedure_catalog_file": phase_catalog_path.name,
                "procedure_catalog_sha256": sha256_file(phase_catalog_path),
                "procedure_catalog_runtime_status": (
                    "evaluation_only_draft_not_frozen"
                ),
                "provisional_reference_file": provisional_phase_path.name,
                "provisional_reference_sha256": sha256_file(
                    provisional_phase_path
                ),
                "reference_included_in_final_layers": True,
                "review_status_counts": {
                    "ambiguous": 1,
                    "confirmed": 0,
                    "proposed": 1,
                    "rejected": 0,
                },
                "review_complete": True,
                "scoring_reference_ready": False,
            },
        }
        self._write_json(manifest_path, manifest)
        bundle = FinalReviewBundle(
            manifest_path=manifest_path,
            expected_timeline_path=timeline_path,
        )
        return bundle, manifest_path, timeline_path, observed_path, report_path

    def test_manifest_loads_direct_assistant_provisional_phase_context(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            (
                _bundle,
                manifest_path,
                timeline_path,
                _observed,
                _report,
            ) = self.make_final_fixture(Path(temporary))
            case_dir = manifest_path.parent
            phase_path = (
                case_dir / "phase_events.provisional.assistant.final.v1.jsonl"
            )
            assistant_phase = {
                "schema": "taskplanner.observable_interaction_point.v1",
                "case_id": "0704_6",
                "event_id": "0704_6-PH0001",
                "event_type": "phase_start",
                "source_frame_idx": 0,
                "time_sec": 0.0,
                "source_views": ["cam4", "flir"],
                "phase_id": "P03",
                "phase_boundary_kind": "clip_initial_state",
                "review_status": "ambiguous",
                "label_origin": "assistant_video_adjudication",
                "review": {
                    "reviewer_kind": "ai_assistant",
                    "reviewer_id": "codex-5.6-sol",
                    "authorized_by": "workspace_user",
                    "reviewed_at": "2026-07-29T00:00:00+00:00",
                    "notes": "Provisional context only.",
                },
            }
            self._write_jsonl(phase_path, [assistant_phase])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            old_phase = manifest["phase_annotation"]
            manifest["phase_annotation"] = {
                "authority": (
                    "user_authorized_ai_assistant_video_adjudication_"
                    "provisional_context_not_scoring_ground_truth"
                ),
                "complete": True,
                "event_count": 1,
                "procedure_catalog_file": old_phase[
                    "procedure_catalog_file"
                ],
                "procedure_catalog_sha256": old_phase[
                    "procedure_catalog_sha256"
                ],
                "procedure_catalog_runtime_status": (
                    "evaluation_only_draft_not_frozen"
                ),
                "provisional_reference_file": phase_path.name,
                "provisional_reference_sha256": sha256_file(phase_path),
                "reference_included_in_final_layers": True,
                "review_authority": {
                    "reviewer_kind": "ai_assistant",
                    "reviewer_ids": ["codex-5.6-sol"],
                    "authorized_by": "workspace_user",
                },
                "review_complete": True,
                "review_status_counts": {
                    "ambiguous": 1,
                    "confirmed": 0,
                    "rejected": 0,
                },
                "scoring_reference_ready": False,
            }
            self._write_json(manifest_path, manifest)

            bundle = FinalReviewBundle(
                manifest_path=manifest_path,
                expected_timeline_path=timeline_path,
            )
            phase = bundle.state()["context_tracks"]["phase"]
            self.assertEqual(1, phase["event_count"])
            self.assertIsNone(phase["candidate_sha256"])
            self.assertIsNone(phase["human_decision_sha256"])
            self.assertEqual(
                "assistant_video_adjudication",
                phase["events"][0]["label_origin"],
            )
            self.assertTrue(phase["events"][0]["_final_review"]["read_only"])
            self.assertEqual(
                "context_only_not_ground_truth",
                phase["events"][0]["_final_review"]["scoring_role"],
            )

            assistant_phase["review"]["authorized_by"] = "someone_else"
            self._write_jsonl(phase_path, [assistant_phase])
            manifest["phase_annotation"][
                "provisional_reference_sha256"
            ] = sha256_file(phase_path)
            self._write_json(manifest_path, manifest)
            with self.assertRaisesRegex(
                InputError,
                "assistant review provenance",
            ):
                FinalReviewBundle(
                    manifest_path=manifest_path,
                    expected_timeline_path=timeline_path,
                )

    def test_manifest_loads_independent_layers_dispositions_and_overlay_cues(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bundle, _manifest, _timeline, _observed, _report = (
                self.make_final_fixture(Path(temporary))
            )
            state = bundle.state()
            self.assertTrue(state["read_only"])
            self.assertEqual(2, len(state["layers"]["observed"]["events"]))
            self.assertEqual(2, len(state["layers"]["dt_reference"]["events"]))
            request = state["layers"]["observed"]["events"][0]
            transfer = state["layers"]["observed"]["events"][1]
            self.assertEqual(
                {
                    "layer": "observed",
                    "read_only": True,
                    "disposition": {
                        "kind": "identity",
                        "label": "DT 평가에 그대로 포함",
                        "reason": "관측 이벤트와 DT 평가 이벤트가 동일합니다.",
                        "source_event_ids": ["0704_6-R0001"],
                        "output_event_id": "0704_6-R0001",
                    },
                    "overlay_cue": {"geometry": "interval"},
                },
                request["_final_review"],
            )
            self.assertEqual(
                "point",
                transfer["_final_review"]["overlay_cue"]["geometry"],
            )
            self.assertEqual(
                "dt_reference",
                state["layers"]["dt_reference"]["events"][0]["_final_review"][
                    "layer"
                ],
            )
            self.assertFalse(state["policy"]["write_api_enabled"])
            speech = state["context_tracks"]["speech"]
            self.assertTrue(speech["available"])
            self.assertEqual(1, speech["event_count"])
            self.assertEqual(
                "Adson 하나 더",
                speech["events"][0]["text"],
            )
            self.assertEqual(
                0.15,
                speech["events"][0]["time_sec"],
            )
            self.assertEqual(
                1,
                speech["events"][0]["_review_ui"][
                    "nearest_source_frame_idx"
                ],
            )
            self.assertNotIn(
                "source_frame_idx",
                speech["events"][0],
            )
            phase = state["context_tracks"]["phase"]
            self.assertTrue(phase["available"])
            self.assertEqual("provisional_ambiguous", phase["status"])
            self.assertEqual(1, phase["event_count"])
            self.assertEqual(0, phase["confirmed_interaction_count_contribution"])
            self.assertEqual(
                ["0704_6-PH0001"],
                [event["event_id"] for event in phase["events"]],
            )
            self.assertEqual("ambiguous", phase["events"][0]["review_status"])
            self.assertTrue(
                phase["events"][0]["_final_review"]["context_only"]
            )
            self.assertEqual(
                2,
                state["layers"]["observed"]["confirmed_event_count"],
            )

    def test_manifest_hash_tampering_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            (
                _bundle,
                manifest_path,
                timeline_path,
                observed_path,
                _report,
            ) = self.make_final_fixture(Path(temporary))
            with observed_path.open("a", encoding="utf-8") as stream:
                stream.write("{}\n")
            with self.assertRaisesRegex(InputError, "SHA-256 불일치"):
                FinalReviewBundle(
                    manifest_path=manifest_path,
                    expected_timeline_path=timeline_path,
                )

    def test_manifest_bound_policy_artifact_tampering_fails_closed(self) -> None:
        artifact_names = (
            "assistant_interaction_adjudications.final.v1.jsonl",
            "dt_projection.explicit.v1.json",
            "evaluation_masks.v1.json",
            "policy02_reconciliation_audit.final.v1.json",
        )
        for artifact_name in artifact_names:
            with self.subTest(artifact=artifact_name):
                with tempfile.TemporaryDirectory() as temporary:
                    (
                        _bundle,
                        manifest_path,
                        timeline_path,
                        _observed,
                        _report,
                    ) = self.make_final_fixture(Path(temporary))
                    artifact_path = manifest_path.parent / artifact_name
                    artifact_path.write_text(
                        artifact_path.read_text(encoding="utf-8") + "\n",
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(
                        InputError,
                        "SHA-256 불일치",
                    ):
                        FinalReviewBundle(
                            manifest_path=manifest_path,
                            expected_timeline_path=timeline_path,
                        )

    def test_duplicate_policy_descriptor_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            (
                _bundle,
                manifest_path,
                timeline_path,
                _observed,
                _report,
            ) = self.make_final_fixture(Path(temporary))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["minimal_interaction_annotation"][
                "explicit_projection_sha256"
            ] = "0" * 64
            self._write_json(manifest_path, manifest)
            with self.assertRaisesRegex(
                InputError,
                "중복 manifest 선언",
            ):
                FinalReviewBundle(
                    manifest_path=manifest_path,
                    expected_timeline_path=timeline_path,
                )

    def test_speech_context_hash_tampering_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            (
                _bundle,
                manifest_path,
                timeline_path,
                _observed,
                _report,
            ) = self.make_final_fixture(Path(temporary))
            speech_path = manifest_path.parent / "voice_events.source.v1.jsonl"
            speech_path.write_text(
                speech_path.read_text(encoding="utf-8") + "{}\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                InputError,
                "speech timeline SHA-256 불일치",
            ):
                FinalReviewBundle(
                    manifest_path=manifest_path,
                    expected_timeline_path=timeline_path,
                )

    def test_voice_v2_preserves_source_geometry_and_exposes_availability(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            (
                _bundle,
                manifest_path,
                timeline_path,
                _observed,
                _report,
            ) = self.make_final_fixture(Path(temporary))
            case_dir = manifest_path.parent
            schema_path = (
                case_dir.parent.parent
                / "schema"
                / "observable_voice_point.v2.schema.json"
            )
            self._write_json(
                schema_path,
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "$id": "taskplanner.observable_voice_point.v2",
                    "type": "object",
                },
            )
            speech_path = case_dir / "voice_events.source.v2.jsonl"
            self._write_jsonl(
                speech_path,
                [
                    {
                        "schema": "taskplanner.observable_voice_point.v2",
                        "case_id": "0704_6",
                        "event_id": "0704_6-V0001",
                        "event_type": "voice_utterance",
                        "time_sec": 0.05,
                        "end_sec": 0.15,
                        "available_sec": 0.18,
                        "text": "Adson 하나 더",
                        "source_topic": "/surgery/transcript",
                        "source_record_timestamp_ns": 170_000_000,
                        "source_message_index": 1,
                        "source_wav": "fixture.wav",
                        "source_authority": "public_runtime_transcript",
                        "scoring_role": "context_only_not_ground_truth",
                        "availability_policy": "not_before_utterance_end",
                    }
                ],
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["speech_timeline"].update(
                {
                    "file": speech_path.name,
                    "sha256": sha256_file(speech_path),
                    "schema_file": (
                        "../../schema/observable_voice_point.v2.schema.json"
                    ),
                    "schema_sha256": sha256_file(schema_path),
                }
            )
            self._write_json(manifest_path, manifest)

            state = FinalReviewBundle(
                manifest_path=manifest_path,
                expected_timeline_path=timeline_path,
            ).state()
            speech = state["context_tracks"]["speech"]
            event = speech["events"][0]
            self.assertEqual(
                "taskplanner.observable_voice_point.v2",
                speech["schema"],
            )
            self.assertEqual("point_at_source_timestamp", speech["timeline_geometry"])
            self.assertEqual(0.05, event["time_sec"])
            self.assertEqual(0.18, event["available_sec"])
            self.assertEqual(
                0.18,
                event["_review_ui"]["complete_text_available_sec"],
            )

    def test_voice_v2_rejects_text_available_before_utterance_end(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            (
                _bundle,
                manifest_path,
                timeline_path,
                _observed,
                _report,
            ) = self.make_final_fixture(Path(temporary))
            case_dir = manifest_path.parent
            schema_path = (
                case_dir.parent.parent
                / "schema"
                / "observable_voice_point.v2.schema.json"
            )
            self._write_json(
                schema_path,
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "$id": "taskplanner.observable_voice_point.v2",
                    "type": "object",
                },
            )
            speech_path = case_dir / "voice_events.source.v2.jsonl"
            self._write_jsonl(
                speech_path,
                [
                    {
                        "schema": "taskplanner.observable_voice_point.v2",
                        "case_id": "0704_6",
                        "event_id": "0704_6-V0001",
                        "event_type": "voice_utterance",
                        "time_sec": 0.05,
                        "end_sec": 0.15,
                        "available_sec": 0.14,
                        "text": "Adson 하나 더",
                        "source_topic": "/surgery/transcript",
                        "source_record_timestamp_ns": 140_000_000,
                        "source_message_index": 1,
                        "source_wav": "fixture.wav",
                        "source_authority": "public_runtime_transcript",
                        "scoring_role": "context_only_not_ground_truth",
                        "availability_policy": "not_before_utterance_end",
                    }
                ],
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["speech_timeline"].update(
                {
                    "file": speech_path.name,
                    "sha256": sha256_file(speech_path),
                    "schema_file": (
                        "../../schema/observable_voice_point.v2.schema.json"
                    ),
                    "schema_sha256": sha256_file(schema_path),
                }
            )
            self._write_json(manifest_path, manifest)
            with self.assertRaisesRegex(
                InputError,
                "complete text availability",
            ):
                FinalReviewBundle(
                    manifest_path=manifest_path,
                    expected_timeline_path=timeline_path,
                )

    def test_manifest_reference_path_cannot_escape_case_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            (
                _bundle,
                manifest_path,
                timeline_path,
                observed_path,
                _report,
            ) = self.make_final_fixture(Path(temporary))
            escaped = observed_path.parent.parent / "escaped.jsonl"
            escaped.write_bytes(observed_path.read_bytes())
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["evaluation_reference"]["observed_reference"].update(
                {
                    "file": "../escaped.jsonl",
                    "sha256": sha256_file(escaped),
                }
            )
            self._write_json(manifest_path, manifest)
            with self.assertRaisesRegex(InputError, "허용된 디렉터리 밖"):
                FinalReviewBundle(
                    manifest_path=manifest_path,
                    expected_timeline_path=timeline_path,
                )

    def test_projection_dangling_reference_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            (
                _bundle,
                manifest_path,
                timeline_path,
                _observed,
                report_path,
            ) = self.make_final_fixture(Path(temporary))
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["operations"]["source_mapping"][0]["source_event_ids"] = [
                "0704_6-R9999"
            ]
            self._write_json(report_path, report)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["evaluation_reference"]["projection_report_sha256"] = (
                sha256_file(report_path)
            )
            self._write_json(manifest_path, manifest)
            with self.assertRaisesRegex(InputError, "observed source event"):
                FinalReviewBundle(
                    manifest_path=manifest_path,
                    expected_timeline_path=timeline_path,
                )

    def test_manifest_case_and_count_mismatches_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            (
                _bundle,
                manifest_path,
                timeline_path,
                _observed,
                _report,
            ) = self.make_final_fixture(Path(temporary))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["evaluation_reference"]["observed_reference"][
                "confirmed_event_count"
            ] = 999
            self._write_json(manifest_path, manifest)
            with self.assertRaisesRegex(InputError, "manifest event count"):
                FinalReviewBundle(
                    manifest_path=manifest_path,
                    expected_timeline_path=timeline_path,
                )

            manifest["evaluation_reference"]["observed_reference"][
                "confirmed_event_count"
            ] = 2
            manifest["case_id"] = "0704_7"
            self._write_json(manifest_path, manifest)
            with self.assertRaisesRegex(InputError, "case 디렉터리 이름"):
                FinalReviewBundle(
                    manifest_path=manifest_path,
                    expected_timeline_path=timeline_path,
                )

    def test_noncanonical_event_time_fails_closed_after_valid_hash_update(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            (
                _bundle,
                manifest_path,
                timeline_path,
                observed_path,
                _report,
            ) = self.make_final_fixture(Path(temporary))
            records = [
                json.loads(line)
                for line in observed_path.read_text(encoding="utf-8").splitlines()
            ]
            records[1]["time_sec"] = 0.199
            self._write_jsonl(observed_path, records)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["evaluation_reference"]["observed_reference"]["sha256"] = (
                sha256_file(observed_path)
            )
            self._write_json(manifest_path, manifest)
            with self.assertRaisesRegex(InputError, "canonical timeline"):
                FinalReviewBundle(
                    manifest_path=manifest_path,
                    expected_timeline_path=timeline_path,
                )

    def test_http_final_review_is_available_and_write_methods_are_blocked(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                bundle,
                _manifest_path,
                timeline_path,
                _observed,
                _report,
            ) = self.make_final_fixture(root)
            case_dir = timeline_path.parent
            candidates_path = case_dir / "interaction_candidates.ai_review.v1.jsonl"
            candidates_path.write_text("", encoding="utf-8")
            decisions_path = case_dir / "human_review_decisions.v1.jsonl"
            actions_path = case_dir / "human_timeline_actions.v1.jsonl"
            store = ReviewStore(
                case_dir=case_dir,
                candidates_path=candidates_path,
                timeline_path=timeline_path,
                decisions_path=decisions_path,
                timeline_actions_path=actions_path,
                stream_kind="interaction",
            )
            static_dir = root / "static"
            static_dir.mkdir()
            (static_dir / "index.html").write_text("fixture", encoding="utf-8")
            handler = make_handler(
                store=store,
                frames=object(),
                static_dir=static_dir,
                final_review=bundle,
                default_review_mode="final_observed",
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with urllib.request.urlopen(f"{base_url}/api/state") as response:
                    state = json.load(response)
                self.assertTrue(state["final_review_available"])
                self.assertEqual(
                    "/api/final-review",
                    state["final_review_url"],
                )
                self.assertEqual("final_observed", state["default_review_mode"])

                with urllib.request.urlopen(
                    f"{base_url}/api/final-review"
                ) as response:
                    final_state = json.load(response)
                self.assertTrue(final_state["read_only"])
                self.assertEqual(
                    2,
                    len(final_state["layers"]["observed"]["events"]),
                )

                request = urllib.request.Request(
                    f"{base_url}/api/annotation-action",
                    data=b"not-json",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(request)
                self.assertEqual(405, context.exception.code)
                self.assertEqual(
                    "GET, HEAD",
                    context.exception.headers["Allow"],
                )
                error = json.loads(context.exception.read())
                context.exception.close()
                self.assertEqual("read_only_final_review", error["code"])

                request = urllib.request.Request(
                    (
                        f"{base_url}/api/decision"
                        "?review_mode=final_dt"
                    ),
                    data=b"not-json",
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(request)
                self.assertEqual(405, context.exception.code)
                context.exception.close()

                request = urllib.request.Request(
                    f"{base_url}/api/annotation-action",
                    data=b"",
                    method="DELETE",
                )
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(request)
                self.assertEqual(405, context.exception.code)
                context.exception.close()

                self.assertFalse(decisions_path.exists())
                self.assertFalse(actions_path.exists())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_json_payload_final_mode_is_rejected_before_store_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (
                bundle,
                _manifest_path,
                timeline_path,
                _observed,
                _report,
            ) = self.make_final_fixture(root)
            case_dir = timeline_path.parent
            candidates_path = case_dir / "interaction_candidates.ai_review.v1.jsonl"
            candidates_path.write_text("", encoding="utf-8")
            decisions_path = case_dir / "human_review_decisions.v1.jsonl"
            actions_path = case_dir / "human_timeline_actions.v1.jsonl"
            store = ReviewStore(
                case_dir=case_dir,
                candidates_path=candidates_path,
                timeline_path=timeline_path,
                decisions_path=decisions_path,
                timeline_actions_path=actions_path,
                stream_kind="interaction",
            )
            static_dir = root / "static"
            static_dir.mkdir()
            (static_dir / "index.html").write_text("fixture", encoding="utf-8")
            handler = make_handler(
                store=store,
                frames=object(),
                static_dir=static_dir,
                final_review=bundle,
                default_review_mode="edit",
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = (
                f"http://127.0.0.1:{server.server_address[1]}"
                "/api/annotation-action"
            )
            try:
                request = urllib.request.Request(
                    url,
                    data=json.dumps({"review_mode": "final_dt"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(request)
                self.assertEqual(405, context.exception.code)
                error = json.loads(context.exception.read())
                context.exception.close()
                self.assertEqual("read_only_final_review", error["code"])
                self.assertFalse(decisions_path.exists())
                self.assertFalse(actions_path.exists())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


class InteractionReviewWebContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        web_dir = (
            Path(__file__).resolve().parents[2]
            / "tools"
            / "real_surgery_annotation"
            / "web_interaction_review"
        )
        cls.html = (web_dir / "index.html").read_text(encoding="utf-8")
        cls.javascript = (web_dir / "app.js").read_text(encoding="utf-8")
        cls.styles = (web_dir / "styles.css").read_text(encoding="utf-8")
        cls.phase_catalog_0704_6 = json.loads(
            (
                web_dir
                / "phase_catalogs"
                / "0704_6.json"
            ).read_text(encoding="utf-8")
        )
        cls.legacy_clinical_html = (
            web_dir / "clinical_review.html"
        ).read_text(encoding="utf-8")
        cls.clinical_redirect_javascript = (
            web_dir / "clinical_review_redirect.js"
        ).read_text(encoding="utf-8")

    def test_video_overlay_has_four_independent_regions_and_one_announcer(
        self,
    ) -> None:
        for region_id in (
            "phase-event-overlay",
            "speech-event-overlay",
            "request-event-overlay",
            "transfer-event-overlay",
        ):
            self.assertEqual(1, self.html.count(f'id="{region_id}"'))
        self.assertIn('class="event-overlay-grid"', self.html)
        self.assertIn('id="event-overlay-announcer"', self.html)
        self.assertIn('aria-live="polite"', self.html)
        self.assertEqual(
            1,
            self.html.count(
                'class="event-overlay-rail event-overlay-rail-left"'
            ),
        )
        self.assertEqual(
            1,
            self.html.count(
                'class="event-overlay-rail event-overlay-rail-right"'
            ),
        )

    def test_overlay_alerts_use_video_side_rails_with_wrapped_narrow_fallback(
        self,
    ) -> None:
        overlay_start = self.html.index('id="event-overlay-stack"')
        video_start = self.html.index('id="video-shell"')
        left_rail = self.html.index("event-overlay-rail-left")
        right_rail = self.html.index("event-overlay-rail-right")
        self.assertLess(overlay_start, left_rail)
        self.assertLess(left_rail, video_start)
        self.assertLess(video_start, right_rail)
        overlay_styles = self.styles[
            self.styles.index(".event-overlay-grid {") :
            self.styles.index(".event-overlay-card {")
        ]
        self.assertNotIn("position: absolute;", overlay_styles)
        self.assertIn('"video"', overlay_styles)
        self.assertIn('"left"', overlay_styles)
        self.assertIn('"right"', overlay_styles)
        self.assertIn("flex-wrap: wrap;", overlay_styles)
        self.assertIn("@container (min-width: 640px)", overlay_styles)
        self.assertIn(
            'grid-template-areas: "left video right";',
            overlay_styles,
        )
        self.assertIn("minmax(132px, 1fr)", overlay_styles)
        self.assertIn("max-height: 100%;", overlay_styles)
        self.assertIn("align-self: center;", overlay_styles)
        self.assertIn("grid-template-rows: auto;", overlay_styles)
        self.assertIn("container-type: size;", self.styles)
        self.assertIn("width: min(100%, 177.7778cqh);", self.styles)
        self.assertIn("grid-area: video;", self.styles)
        self.assertIn("pointer-events: auto;", self.styles)
        self.assertIn(
            "동기화 수술 영상과 주변의 수술 단계, 음성, 도구 요청, 도구 이동, 임상 근거 알림",
            self.javascript,
        )

    def test_clinical_selection_preserves_source_review_overlays(
        self,
    ) -> None:
        selection_chrome = self.javascript[
            self.javascript.index("function renderSelectionChrome(") :
            self.javascript.index("function renderModeChrome(")
        ]
        self.assertNotIn("$(selector).hidden = clinical", selection_chrome)
        self.assertIn("if (region) region.hidden = false;", selection_chrome)
        for region_id in (
            "phase-event-overlay",
            "speech-event-overlay",
            "request-event-overlay",
            "transfer-event-overlay",
        ):
            self.assertIn(f'"#{region_id}"', selection_chrome)
        self.assertIn("event-overlay-rail-left", self.html)
        self.assertIn("event-overlay-rail-right", self.html)

    def test_overlay_toggle_controls_surgical_and_clinical_overlays(
        self,
    ) -> None:
        self.assertEqual(1, self.html.count('id="event-overlay-enabled"'))
        self.assertIn("<span>오버레이 표시</span>", self.html)
        clinical_overlay = self.javascript[
            self.javascript.index("function renderClinicalOverlay(") :
            self.javascript.index("function renderSelectionChrome(")
        ]
        self.assertIn("!state.overlayEnabled", clinical_overlay)
        toggle_handler = self.javascript[
            self.javascript.index(
                '$("#event-overlay-enabled").addEventListener("change"'
            ) :
            self.javascript.index(
                '$("#playback-rate").addEventListener("change"'
            )
        ]
        self.assertIn("clearVideoEventOverlay();", toggle_handler)
        self.assertEqual(
            2,
            toggle_handler.count("renderClinicalOverlay();"),
        )
        self.assertIn("영상 오버레이를 껐습니다.", toggle_handler)
        self.assertIn("영상 오버레이를 켰습니다.", toggle_handler)
        self.assertIn(
            '"surgery-review-event-overlay"',
            toggle_handler,
        )
        bootstrap = self.javascript[
            self.javascript.index(
                'const savedOverlay = localStorage.getItem('
            ) :
        ]
        self.assertIn(
            '"surgery-review-event-overlay"',
            bootstrap,
        )
        self.assertIn(
            'state.overlayEnabled = savedOverlay !== "false";',
            bootstrap,
        )
        self.assertIn(
            '$("#event-overlay-enabled").checked = state.overlayEnabled;',
            bootstrap,
        )

    def test_rfdetr_overlay_is_case_scoped_optional_and_frame_indexed(
        self,
    ) -> None:
        self.assertEqual(
            1,
            self.html.count('id="recognition-overlay-enabled"'),
        )
        self.assertEqual(
            1,
            self.html.count('id="cam4-recognition-overlay"'),
        )
        self.assertEqual(
            1,
            self.html.count('id="flir-recognition-overlay"'),
        )
        self.assertNotIn('id="cam2-recognition-overlay"', self.html)
        self.assertNotIn('id="cam3-recognition-overlay"', self.html)
        recognition_input = self.html[
            self.html.index('id="recognition-overlay-enabled"') :
            self.html.index(
                'id="recognition-overlay-description"',
            )
        ]
        self.assertIn("disabled", recognition_input)
        self.assertNotIn("checked", recognition_input)
        self.assertIn(
            "AI 추론 참고용이며 사람 검수 정답이 아닙니다.",
            self.html,
        )
        self.assertIn(
            "`surgery-review-rfdetr-overlay:${caseId}`",
            self.javascript,
        )
        self.assertIn(
            "`/rfdetr_overlays/${encodeURIComponent(caseId)}.json`",
            self.javascript,
        )
        self.assertIn(
            "viewData.frames[state.currentFrame]",
            self.javascript,
        )
        self.assertIn(
            'image.dataset.sourceFrameIndex !== String(state.currentFrame)',
            self.javascript,
        )
        self.assertIn(
            "proxy.content_rect",
            self.javascript,
        )
        self.assertIn(
            "gapAt(state.currentTimeSec)",
            self.javascript,
        )
        self.assertIn(
            "function frameIndexAtOrBefore(timeSec)",
            self.javascript,
        )
        self.assertIn(
            'frameSelection: "floor"',
            self.javascript,
        )
        self.assertIn(
            "function frameSelectionForVisibleMedia()",
            self.javascript,
        )
        self.assertGreaterEqual(
            self.javascript.count(
                "frameSelection: frameSelectionForVisibleMedia(),"
            ),
            1,
        )
        self.assertIn(
            "state.pendingSeekFrameSelection = frameSelection;",
            self.javascript,
        )
        self.assertIn(
            "const requestedFrame = state.currentFrame;",
            self.javascript,
        )
        self.assertIn(
            "requestedFrame === state.currentFrame",
            self.javascript,
        )
        self.assertIn(
            "assignBlobImage(fallbackImage(view), blob, requestedFrame);",
            self.javascript,
        )
        self.assertIn(
            "pointer-events: none;",
            self.styles[
                self.styles.index(".recognition-overlay {") :
                self.styles.index(".camera-tile-label {")
            ],
        )

    def test_background_return_reconnects_video_without_reloading_review_state(
        self,
    ) -> None:
        self.assertIn('id="video-loading-message"', self.html)
        video_loading = self.html[
            self.html.index('id="video-loading"') :
            self.html.index('id="video-error"')
        ]
        self.assertIn('role="status"', video_loading)
        self.assertIn('aria-live="polite"', video_loading)
        self.assertIn("function markVideoPageAway()", self.javascript)
        self.assertIn("function scheduleVideoRecovery(", self.javascript)
        self.assertIn(
            'document.addEventListener("visibilitychange"',
            self.javascript,
        )
        self.assertIn(
            'window.addEventListener("focus"',
            self.javascript,
        )
        self.assertIn(
            'window.addEventListener("pageshow"',
            self.javascript,
        )
        recovery = self.javascript[
            self.javascript.index("function scheduleVideoRecovery(") :
            self.javascript.index("function pausePlayback(")
        ]
        self.assertIn("configureVideo({ recovery: true });", recovery)
        self.assertNotIn("loadState(", recovery)
        configure = self.javascript[
            self.javascript.index("function configureVideo(") :
            self.javascript.index("function markVideoPageAway(")
        ]
        self.assertIn('element.removeAttribute("src");', configure)
        self.assertIn("element.load();", configure)
        self.assertIn(
            "setVideoViewTime(view, state.currentTimeSec",
            self.javascript,
        )

    def test_phase_catalog_exposes_full_names_order_and_current_video_map(
        self,
    ) -> None:
        for element_id in (
            "phase-catalog-panel",
            "phase-catalog-current",
            "phase-catalog-count",
            "phase-catalog-list",
            "phase-catalog-note",
        ):
            self.assertEqual(1, self.html.count(f'id="{element_id}"'))
        self.assertNotIn('id="phase-track-current"', self.html)
        self.assertNotIn('$("#phase-track-current")', self.javascript)
        self.assertIn("수술 단계", self.html)
        self.assertIn("전체 수술 단계 구성", self.html)
        self.assertIn("function loadPhaseCatalog(", self.javascript)
        self.assertIn("function phaseDisplayLabel(", self.javascript)
        self.assertIn("function renderPhaseCatalog(", self.javascript)
        self.assertIn(
            "renderPhaseCatalog(phaseEntries);",
            self.javascript,
        )
        self.assertIn(
            '"(min-width: 1261px) and (max-height: 820px)"',
            self.javascript,
        )
        self.assertIn(
            "title.textContent = phaseDisplayLabel(fields.phase_id);",
            self.javascript,
        )
        self.assertIn(
            "return phaseDisplayLabel(fields.phase_id);",
            self.javascript,
        )
        self.assertIn(".phase-catalog-panel {", self.styles)
        self.assertIn(
            '.phase-catalog-card[data-current="true"]',
            self.styles,
        )
        self.assertEqual(
            [f"P{index:02d}" for index in range(1, 11)],
            self.phase_catalog_0704_6["phase_order"],
        )
        phases = self.phase_catalog_0704_6["phases"]
        self.assertEqual(10, len(phases))
        self.assertEqual(
            "정중선 절개 및 피대근 박리",
            phases[2]["name_ko"],
        )
        self.assertEqual(
            "수술 종료 및 기구 정리",
            phases[-1]["name_ko"],
        )

    def test_case_selector_uses_case_scoped_api_urls_and_safe_write_identity(
        self,
    ) -> None:
        self.assertEqual(1, self.html.count('id="case-selector"'))
        self.assertIn('aria-label="검수 영상 선택"', self.html)
        self.assertIn("function apiUrl(", self.javascript)
        self.assertIn('url.searchParams.set("case", caseId)', self.javascript)
        self.assertIn('apiUrl("/api/state")', self.javascript)
        self.assertIn('apiUrl("/api/frame"', self.javascript)
        self.assertIn('apiUrl("/api/annotation-action")', self.javascript)
        self.assertIn("case_id: state.data.case_id", self.javascript)
        self.assertIn("function switchCase(", self.javascript)
        self.assertIn("guardDirtyDraft()", self.javascript)
        self.assertIn(".case-picker select", self.styles)
        self.assertIn("min-height: 44px", self.styles)

    def test_header_controls_share_height_and_review_modes_use_requested_order(
        self,
    ) -> None:
        observed_index = self.html.index('data-review-mode="final_observed"')
        edit_index = self.html.index('data-review-mode="edit"')
        final_index = self.html.index('data-review-mode="final_dt"')
        self.assertLess(observed_index, edit_index)
        self.assertLess(edit_index, final_index)
        self.assertIn("--header-control-height: 52px;", self.styles)
        self.assertGreaterEqual(
            self.styles.count("height: var(--header-control-height);"),
            3,
        )
        self.assertIn("grid-row: 1 / -1;", self.styles)
        self.assertIn("display: contents;", self.styles)

    def test_final_ui_is_version_neutral_and_voice_availability_aware(
        self,
    ) -> None:
        combined = self.html + self.javascript
        for stale_label in ("최종 DT v4", "최종 v3", "FINAL V4"):
            self.assertNotIn(stale_label, combined)
        self.assertIn('id="speech-event-available"', self.html)
        self.assertIn("speechAvailabilitySec", self.javascript)
        self.assertIn("function speechCompletionSec(", self.javascript)
        self.assertIn("Math.max(startSec, endSec)", self.javascript)
        self.assertIn("function nearestSpeechEventToTime(", self.javascript)
        self.assertIn(
            "clientXToTimelineTime(clickEvent.clientX)",
            self.javascript,
        )
        self.assertIn(
            "seekToTime(speechCompletionSec(event))",
            self.javascript,
        )
        self.assertIn(
            "const timeSec = speechCompletionSec(event);",
            self.javascript,
        )
        self.assertIn("발화 완료 ${formatExactSpeechTime(timeSec)}", self.javascript)
        self.assertNotIn("<small>발화 완료</small>", self.html)
        self.assertIn(
            'data-track-filter="request" type="checkbox" checked /> 도구 요청',
            self.html,
        )
        self.assertIn(
            '<span class="track-symbol point-symbol">○</span> 도구 요청',
            self.html,
        )
        self.assertIn("event?.available_sec", self.javascript)
        self.assertIn("function speechOverlayStage(", self.javascript)
        self.assertIn('return "speaking"', self.javascript)
        self.assertIn('return "awaiting_text"', self.javascript)
        self.assertIn('return "text_available"', self.javascript)
        self.assertIn("now + 1e-7 >= startSec", self.javascript)
        self.assertIn("원문은 발화 종료 후 표시됩니다.", self.javascript)
        self.assertIn(
            "`speech:${speechEventKey(speech)}:${speechStage}`",
            self.javascript,
        )
        self.assertIn("phaseContextEvents", self.javascript)
        self.assertIn(
            "const items = isFinalMode() ? finalInteractionItems() : sourceItems();",
            self.javascript,
        )

    def test_clinical_review_uses_one_combined_screen_without_workspace_toggle(
        self,
    ) -> None:
        self.assertNotIn('id="workspace-mode-control"', self.html)
        self.assertNotIn("data-workspace-mode", self.html + self.javascript)
        self.assertNotIn('id="clinical-review-mode-control"', self.html)
        self.assertNotIn("data-clinical-review-mode", self.html + self.javascript)
        self.assertEqual(1, self.html.count('id="event-list"'))
        self.assertNotIn('id="clinical-candidate-list"', self.html)
        self.assertEqual(1, self.html.count('data-track-filter="clinical"'))
        self.assertIn("수술 이벤트와 임상 어노테이션 목록", self.html)
        self.assertEqual(1, self.html.count('id="interaction-inspector"'))
        self.assertEqual(1, self.html.count('id="clinical-inspector"'))
        self.assertNotIn('id="clinical-review-entry"', self.html)
        self.assertNotIn("openClinicalReview", self.javascript)
        self.assertNotIn("/clinical_review.html", self.javascript)
        self.assertIn('apiUrl("/api/clinical-review")', self.javascript)
        self.assertIn('apiUrl("/api/clinical-action")', self.javascript)
        self.assertIn("case_id: activeCaseId()", self.javascript)
        self.assertIn("function combinedNavigatorEntries(", self.javascript)
        self.assertIn("function clinicalNavigatorListItem(", self.javascript)
        self.assertIn("function renderSelectionChrome(", self.javascript)
        self.assertNotIn("function switchWorkspaceMode(", self.javascript)
        self.assertNotIn("isClinicalWorkspace", self.javascript)
        self.assertIn(
            'src="/clinical_review_redirect.js"',
            self.legacy_clinical_html,
        )
        self.assertNotIn(
            'src="/clinical_review.js"',
            self.legacy_clinical_html,
        )
        self.assertIn(
            'target.searchParams.delete("workspace")',
            self.clinical_redirect_javascript,
        )
        self.assertIn(
            "window.location.replace(target)",
            self.clinical_redirect_javascript,
        )

    def test_clinical_labels_use_two_text_fields_and_one_timeline_track(
        self,
    ) -> None:
        self.assertEqual(1, self.html.count('data-track="clinical"'))
        self.assertEqual(1, self.html.count('id="clinical-track"'))
        self.assertNotIn('data-track="clinical-unobservable"', self.html)
        self.assertNotIn('id="clinical-unobservable-track"', self.html)
        self.assertEqual(1, self.html.count('id="clinical-observation"'))
        self.assertEqual(1, self.html.count('id="clinical-interpretation"'))
        self.assertIn('maxlength="600"', self.html)
        for legacy_track in (
            'id="activity-track"',
            'id="observation-track"',
            'id="change-track"',
        ):
            self.assertNotIn(legacy_track, self.html)
        for removed_control in (
            'id="clinical-annotation-kind"',
            'id="clinical-instrument"',
            'id="clinical-anatomy-label"',
            'id="clinical-observable-findings"',
            'id="clinical-interpretations"',
        ):
            self.assertNotIn(removed_control, self.html)
        self.assertNotIn("CLINICAL_KIND_LABELS", self.javascript)
        self.assertNotIn("marker.dataset.kind", self.javascript)
        self.assertIn("function clinicalSentenceCount(", self.javascript)
        self.assertIn(
            "reviewed + clinical.reviewed",
            self.javascript,
        )
        self.assertIn(
            "total + clinical.total",
            self.javascript,
        )
        self.assertIn("전체 검토 진행률", self.javascript)
        self.assertIn(
            'state.clinical.draft.observation =',
            self.javascript,
        )
        self.assertIn(
            'state.clinical.draft.interpretation =',
            self.javascript,
        )
        self.assertIn(
            'kindSymbol.append(createTimelineSemanticIcon("stethoscope"))',
            self.javascript,
        )
        self.assertNotIn('kindSymbol.textContent = "임"', self.javascript)
        self.assertIn(
            'kindSymbol.append(createTimelineSemanticIcon("speaker"))',
            self.javascript,
        )
        self.assertIn(
            'icon.append(createTimelineSemanticIcon("stethoscope"))',
            self.javascript,
        )
        self.assertIn('data-icon="speaker"', self.html)
        self.assertIn('data-icon="stethoscope"', self.html)
        self.assertIn("임상 · ${formatClinicalRange(", self.javascript)
        self.assertIn(
            "모든 임상 항목이 함께 표시되는 단일 타임라인",
            self.html,
        )
        self.assertIn(
            ".event-marker.speech::before {\n"
            "  width: 18px;\n"
            "  height: 18px;\n"
            "  position: absolute;",
            self.styles,
        )
        self.assertIn(
            ".speech-marker-kind {\n"
            "  position: absolute;\n"
            "  inset: 0;",
            self.styles,
        )
        render_timeline = self.javascript[
            self.javascript.index("function renderTimeline(") :
            self.javascript.index("function focusedEventControl(")
        ]
        self.assertIn("renderClinicalTimelineTracks();", render_timeline)

    def test_combined_selection_dirty_guards_cover_domain_mode_case_and_unload(
        self,
    ) -> None:
        self.assertIn("function hasUnsavedWork()", self.javascript)
        self.assertIn(
            "return hasDirtyDraft() || hasDirtyClinicalDraft();",
            self.javascript,
        )
        self.assertIn("function isAnySaving()", self.javascript)
        self.assertIn("function guardAnyNavigation()", self.javascript)
        switch_case = self.javascript[
            self.javascript.index("function switchCase(") :
            self.javascript.index("function isFinalMode(")
        ]
        self.assertIn("guardAnyNavigation()", switch_case)
        clinical_selection = self.javascript[
            self.javascript.index("function selectClinicalItem(") :
            self.javascript.index("function discardClinicalDraft(")
        ]
        self.assertIn("guardAnyNavigation()", clinical_selection)
        self.assertEqual(
            2,
            clinical_selection.count(
                "seekToClinicalAnchor(state.clinical.draft);"
            ),
        )
        self.assertNotIn(
            "seekToTime(clinicalEvidenceStartSec(state.clinical.draft));",
            clinical_selection,
        )
        self.assertIn(
            "function clinicalAnchorFrameIndex(value)",
            self.javascript,
        )
        self.assertIn(
            "value?.anchor_source_frame_idx",
            self.javascript,
        )
        self.assertIn(
            'seekToTime(clinicalAnchorSec(value), { frameSelection: "nearest" });',
            self.javascript,
        )
        self.assertIn(
            'state.pendingSeekFrameSelection = "nearest";',
            self.javascript,
        )
        self.assertIn(
            "state.pendingSeekFrameSelection ||",
            self.javascript,
        )
        event_selection = self.javascript[
            self.javascript.index("function selectCandidate(") :
            self.javascript.index("function selectSpeechEvent(")
        ]
        self.assertIn("guardAnyNavigation()", event_selection)
        review_mode = self.javascript[
            self.javascript.index("function setReviewMode(") :
            self.javascript.index("function renderAll(")
        ]
        self.assertIn("guardAnyNavigation()", review_mode)
        self.assertIn("function navigateCombinedItem(", self.javascript)
        self.assertIn(
            "if (!hasUnsavedWork() && !isAnySaving()) return;",
            self.javascript,
        )
        self.assertIn(
            "String(payload.case_id) !== expectedCase",
            self.javascript,
        )
        self.assertIn(
            'window.history.replaceState({}, "", url)',
            self.javascript,
        )
        self.assertIn(
            'const legacyKeys = ["workspace", "layer", "review_mode", "clinical_mode"]',
            self.javascript,
        )


if __name__ == "__main__":
    unittest.main()
