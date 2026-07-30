from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import jsonschema

from tools.real_surgery_annotation.clinical_review_store import (
    ClinicalConflictError,
    ClinicalInputError,
    ClinicalReviewStore,
    canonical_json,
    sha256_file,
)
from tools.real_surgery_annotation.interaction_review_gui import (
    ReviewCaseRuntime,
    ReviewStore,
    make_handler,
)


SCHEMA_ROOT = (
    Path(__file__).resolve().parents[2]
    / "annotations"
    / "clinical_video"
    / "schema"
)


class ClinicalReviewStoreTest(unittest.TestCase):
    @staticmethod
    def timeline(case_id: str) -> dict:
        return {
            "schema": "taskplanner.video_frame_timeline.v1",
            "case_id": case_id,
            "source_bag": f"/fixture/{case_id}",
            "frame_count": 6,
            "timestamps_sec": [0.0, 0.1, 0.2, 0.3, 0.4, 0.5],
            "gaps": [],
        }

    @staticmethod
    def candidate(
        case_id: str,
        *,
        number: int = 1,
        anchor_frame: int = 2,
    ) -> dict:
        return {
            "schema": "taskplanner.clinical_video_annotation.v2",
            "annotation_id": f"{case_id}-CLV2-{number:04d}",
            "case_id": case_id,
            "anchor_source_frame_idx": anchor_frame,
            "anchor_sec": anchor_frame / 10,
            "evidence_start_source_frame_idx": anchor_frame - 1,
            "evidence_end_source_frame_idx": anchor_frame + 1,
            "evidence_start_sec": (anchor_frame - 1) / 10,
            "evidence_end_sec": (anchor_frame + 1) / 10,
            "observation": (
                "Bipolar forceps contact the visible soft tissue. "
                "A local color change is visible at the contact site."
            ),
            "interpretation": (
                "The maneuver may represent coagulation, but the effect "
                "requires surgeon review."
            ),
            "supersedes_annotation_ids": [f"{case_id}-CL{number:04d}"],
            "confidence": {
                "observation": 0.8,
                "interpretation": 0.3,
            },
            "source_views": ["cam4", "flir"],
            "provenance": {
                "generator": "codex",
                "model": "gpt-5.6-sol",
                "generated_at": "2026-07-29T00:00:00+00:00",
                "authority": "ai_draft",
            },
            "review_status": "needs_surgeon_review",
        }

    def make_store(
        self,
        root: Path,
        *,
        case_id: str = "0704_6",
        candidates: list[dict] | None = None,
        timeline: dict | None = None,
        context_sources: list[dict] | None = None,
    ) -> tuple[ClinicalReviewStore, Path, Path, Path]:
        clinical_case_dir = root / "clinical_video" / "cases" / case_id
        clinical_case_dir.mkdir(parents=True)
        timeline = timeline or self.timeline(case_id)
        timeline_path = root / f"{case_id}.timeline.json"
        timeline_path.write_text(
            canonical_json(timeline) + "\n",
            encoding="utf-8",
        )
        candidate_records = candidates or [self.candidate(case_id)]
        candidates_path = (
            clinical_case_dir
            / "clinical_candidates.codex_5_6_sol.v2.jsonl"
        )
        candidates_path.write_text(
            "".join(canonical_json(record) + "\n" for record in candidate_records),
            encoding="utf-8",
        )
        manifest = {
            "schema": "taskplanner.clinical_video_manifest.v2",
            "case_id": case_id,
            "authority": "ai_draft",
            "candidate_file": candidates_path.name,
            "candidate_sha256": sha256_file(candidates_path),
            "candidate_count": len(candidate_records),
            "review_actions_file": "clinical_review_actions.v2.jsonl",
            "final_reference_file": "clinical_reference.final.v2.jsonl",
            "content_fields": ["observation", "interpretation"],
            "source_timeline": {
                "file": os.path.relpath(
                    timeline_path,
                    clinical_case_dir,
                ),
                "sha256": sha256_file(timeline_path),
            },
        }
        if context_sources is not None:
            manifest["context_sources"] = context_sources
        manifest_path = clinical_case_dir / "clinical_manifest.v2.json"
        manifest_path.write_text(
            canonical_json(manifest) + "\n",
            encoding="utf-8",
        )
        store = ClinicalReviewStore(
            case_dir=clinical_case_dir,
            case_id=case_id,
            source_timeline=timeline,
            source_timeline_path=timeline_path,
        )
        return (
            store,
            candidates_path,
            clinical_case_dir / "clinical_review_actions.v2.jsonl",
            clinical_case_dir / "clinical_reference.final.v2.jsonl",
        )

    @staticmethod
    def action_payload(
        state: dict,
        *,
        status: str = "confirmed",
        supersedes_action_id: str | None = None,
        client_request_id: str = "clinical-fixture-action-1",
    ) -> dict:
        candidate = state["candidates"][0]
        annotation = copy.deepcopy({
            key: value
            for key, value in candidate.items()
            if key not in ("_clinical_review", "_review_ui")
        })
        annotation["observation"] = (
            "Fine forceps grasp and reposition the visible tissue. "
            "No sharp cutting is visible in the evidence window."
        )
        return {
            "case_id": state["case_id"],
            "revision": state["revision"],
            "annotation_id": candidate["annotation_id"],
            "candidate_sha256": candidate["_clinical_review"][
                "candidate_sha256"
            ],
            "supersedes_action_id": supersedes_action_id,
            "client_request_id": client_request_id,
            "review_status": status,
            "reviewer_id": "fixture-clinical-reviewer",
            "reviewer_role": "clinician",
            "notes": "fixture clinical review",
            "adjudicated_annotation": annotation,
        }

    def test_schema_candidate_action_and_derived_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, candidates_path, actions_path, reference_path = (
                self.make_store(Path(temporary))
            )
            original_candidates = candidates_path.read_bytes()
            annotation_schema = json.loads(
                (
                    SCHEMA_ROOT
                    / "clinical_video_annotation.v2.schema.json"
                ).read_text(encoding="utf-8")
            )
            action_schema = json.loads(
                (
                    SCHEMA_ROOT
                    / "clinical_review_action.v2.schema.json"
                ).read_text(encoding="utf-8")
            )
            manifest_schema = json.loads(
                (
                    SCHEMA_ROOT
                    / "clinical_manifest.v2.schema.json"
                ).read_text(encoding="utf-8")
            )
            for schema in (
                annotation_schema,
                action_schema,
                manifest_schema,
            ):
                jsonschema.Draft202012Validator.check_schema(schema)
            jsonschema.validate(store.candidates()[0], annotation_schema)
            jsonschema.validate(store.manifest, manifest_schema)

            initial = store.state()
            self.assertTrue(initial["available"])
            self.assertEqual(1, initial["progress"]["total"])
            self.assertEqual(0, initial["progress"]["reviewed"])
            self.assertFalse(reference_path.exists())
            result = store.save_action(self.action_payload(initial))

            self.assertFalse(result["idempotent"])
            self.assertEqual(original_candidates, candidates_path.read_bytes())
            self.assertEqual(1, len(actions_path.read_text().splitlines()))
            jsonschema.validate(result["action"], action_schema)
            completed = result["state"]
            self.assertEqual(1, completed["progress"]["confirmed"])
            self.assertTrue(completed["reference"]["ready"])
            self.assertTrue(reference_path.is_file())
            derived = json.loads(reference_path.read_text().splitlines()[0])
            jsonschema.validate(derived, annotation_schema)
            self.assertEqual(
                (
                    "Fine forceps grasp and reposition the visible tissue. "
                    "No sharp cutting is visible in the evidence window."
                ),
                derived["observation"],
            )
            self.assertEqual(
                "human_reviewed_ai_draft_not_automatic_ground_truth",
                derived["clinical_review"]["resulting_authority"],
            )
            self.assertEqual(
                (
                    "The maneuver may represent coagulation, but the effect "
                    "requires surgeon review."
                ),
                derived["interpretation"],
            )

            retry = store.save_action(self.action_payload(initial))
            self.assertTrue(retry["idempotent"])
            self.assertEqual(1, len(actions_path.read_text().splitlines()))

    def test_correction_is_append_only_and_rewrites_only_derived_reference(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, candidates_path, actions_path, reference_path = (
                self.make_store(Path(temporary))
            )
            original_candidates = candidates_path.read_bytes()
            first = store.save_action(
                self.action_payload(store.state())
            )
            second_payload = self.action_payload(
                first["state"],
                status="ambiguous",
                supersedes_action_id=first["action"]["action_id"],
                client_request_id="clinical-fixture-action-2",
            )
            second = store.save_action(second_payload)

            self.assertEqual(2, len(actions_path.read_text().splitlines()))
            self.assertEqual(original_candidates, candidates_path.read_bytes())
            self.assertEqual(
                [
                    first["action"]["action_id"],
                    second["action"]["action_id"],
                ],
                [
                    action["action_id"]
                    for action in second["state"]["action_history"][
                        "0704_6-CLV2-0001"
                    ]
                ],
            )
            self.assertEqual(1, second["state"]["progress"]["ambiguous"])
            derived = json.loads(reference_path.read_text().splitlines()[0])
            self.assertEqual("ambiguous", derived["review_status"])
            self.assertEqual(
                second["action"]["action_id"],
                derived["clinical_review"]["action_id"],
            )

    def test_missing_complete_reference_is_recovered_from_action_log(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, _candidates_path, _actions_path, reference_path = (
                self.make_store(Path(temporary))
            )
            completed = store.save_action(
                self.action_payload(store.state())
            )
            expected_reference = reference_path.read_bytes()
            self.assertTrue(completed["state"]["reference"]["ready"])
            reference_path.unlink()

            recovered_store = ClinicalReviewStore(
                case_dir=store.case_dir,
                case_id=store.case_id,
                source_timeline=store.source_timeline,
                source_timeline_path=store.source_timeline_path,
            )
            recovered = recovered_store.state()
            self.assertTrue(recovered["reference"]["ready"])
            self.assertTrue(recovered["reference"]["review_complete"])
            self.assertEqual(expected_reference, reference_path.read_bytes())

    def test_stale_revision_and_wrong_candidate_hash_do_not_append(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, _candidates_path, actions_path, _reference_path = (
                self.make_store(Path(temporary))
            )
            initial = store.state()
            bad_hash = self.action_payload(initial)
            bad_hash["candidate_sha256"] = "0" * 64
            with self.assertRaisesRegex(
                ClinicalConflictError,
                "candidate 내용",
            ):
                store.save_action(bad_hash)
            self.assertEqual("", actions_path.read_text(encoding="utf-8"))

            first = store.save_action(self.action_payload(initial))
            stale = self.action_payload(
                first["state"],
                status="ambiguous",
                supersedes_action_id=first["action"]["action_id"],
                client_request_id="clinical-fixture-stale",
            )
            stale["revision"] = initial["revision"]
            with self.assertRaisesRegex(
                ClinicalConflictError,
                "먼저 추가",
            ):
                store.save_action(stale)
            self.assertEqual(1, len(actions_path.read_text().splitlines()))

    def test_candidate_file_change_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, candidates_path, _actions_path, _reference_path = (
                self.make_store(Path(temporary))
            )
            candidates_path.write_text(
                candidates_path.read_text(encoding="utf-8") + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ClinicalConflictError,
                "manifest snapshot 이후 변경",
            ):
                store.state()

    def test_missing_case_is_explicitly_unavailable_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            case_dir = root / "clinical_video" / "cases" / "0704_7"
            store = ClinicalReviewStore(
                case_dir=case_dir,
                case_id="0704_7",
                source_timeline=self.timeline("0704_7"),
            )
            state = store.state()
            self.assertFalse(state["available"])
            self.assertEqual("0704_7", state["case_id"])
            self.assertEqual([], state["candidates"])
            self.assertFalse(case_dir.exists())
            with self.assertRaisesRegex(
                ClinicalInputError,
                "candidate가 없어",
            ):
                store.save_action({})

    def test_evidence_window_cannot_cross_canonical_gap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            timeline = self.timeline("0704_6")
            timeline["gaps"] = [
                {
                    "before_frame_idx": 1,
                    "after_frame_idx": 2,
                    "before_time_sec": 0.1,
                    "after_time_sec": 0.2,
                    "delta_sec": 0.1,
                }
            ]
            gap_store, _candidates_path, _actions_path, _reference_path = (
                self.make_store(root, timeline=timeline)
            )
            with self.assertRaisesRegex(ClinicalInputError, "gap"):
                gap_store.state()

    def test_narratives_are_one_or_two_complete_sentences(self) -> None:
        mutations = {
            "empty_observation": ("observation", "", "한 문장"),
            "unterminated_observation": (
                "observation",
                "The operative field is partly visible",
                "1~2문장",
            ),
            "three_sentence_interpretation": (
                "interpretation",
                "One hypothesis is possible. Another is possible. "
                "Surgeon review is still required.",
                "1~2문장",
            ),
            "over_600_characters": (
                "observation",
                "가" * 600 + ".",
                "600자",
            ),
        }
        for label, (field, value, error_pattern) in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                candidate = self.candidate("0704_6")
                candidate[field] = value
                store, *_ = self.make_store(
                    Path(temporary),
                    candidates=[candidate],
                )
                with self.assertRaisesRegex(
                    ClinicalInputError,
                    error_pattern,
                ):
                    store.state()

    def test_cam4_only_evidence_is_valid_without_forcing_flir(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            candidate = self.candidate("0704_6")
            candidate["source_views"] = ["cam4"]
            store, *_ = self.make_store(
                Path(temporary),
                candidates=[candidate],
            )
            self.assertEqual(["cam4"], store.candidates()[0]["source_views"])
        with tempfile.TemporaryDirectory() as temporary:
            candidate = self.candidate("0704_6")
            candidate["source_views"] = ["flir", "cam4"]
            store, *_ = self.make_store(
                Path(temporary),
                candidates=[candidate],
            )
            self.assertEqual(
                ["flir", "cam4"],
                store.candidates()[0]["source_views"],
            )

    def test_rfdetr_context_source_requires_authority_and_matching_sha(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            overlay_path = root / "0704_6.rfdetr.json"
            overlay_path.write_text('{"authority":"ai_inference_reference_not_ground_truth"}\n')
            descriptor = {
                "file": "../../../0704_6.rfdetr.json",
                "role": "ai_inference_reference_not_ground_truth",
                "authority": "ai_inference_reference_not_ground_truth",
                "sha256": sha256_file(overlay_path),
            }
            store, *_ = self.make_store(
                root / "valid",
                context_sources=[
                    {
                        **descriptor,
                        "file": "../../../../0704_6.rfdetr.json",
                    }
                ],
            )
            self.assertTrue(store.state()["available"])

            bad_sha = {**descriptor, "sha256": "0" * 64}
            with self.assertRaisesRegex(
                ClinicalInputError,
                "context source.*SHA-256",
            ):
                self.make_store(
                    root,
                    context_sources=[bad_sha],
                )

            missing_authority = dict(descriptor)
            missing_authority.pop("authority")
            with self.assertRaisesRegex(
                ClinicalInputError,
                "manifest JSON Schema",
            ):
                self.make_store(
                    root / "missing-authority",
                    context_sources=[
                        {
                            **missing_authority,
                            "file": "../../../../0704_6.rfdetr.json",
                        }
                    ],
                )

    def test_adjudicated_annotation_requires_full_annotation_schema(self) -> None:
        mutations = {
            "unknown_top_level": lambda annotation: annotation.__setitem__(
                "unexpected_field",
                "not allowed",
            ),
            "observation_type": lambda annotation: annotation.__setitem__(
                "observation",
                42,
            ),
            "missing_interpretation": lambda annotation: annotation.pop(
                "interpretation"
            ),
            "confidence_range": lambda annotation: annotation[
                "confidence"
            ].__setitem__(
                "observation",
                99,
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                store, _candidates_path, actions_path, _reference_path = (
                    self.make_store(Path(temporary))
                )
                payload = self.action_payload(store.state())
                mutate(payload["adjudicated_annotation"])
                with self.assertRaisesRegex(
                    ClinicalInputError,
                    "annotation JSON Schema|observation|interpretation",
                ):
                    store.save_action(payload)
                self.assertEqual("", actions_path.read_text(encoding="utf-8"))

    def test_non_finite_confidence_is_rejected_before_append(self) -> None:
        mutations = {
            "observation_nan": lambda annotation: annotation[
                "confidence"
            ].__setitem__(
                "observation",
                float("nan"),
            ),
            "interpretation_positive_infinity": lambda annotation: annotation[
                "confidence"
            ].__setitem__(
                "interpretation",
                float("inf"),
            ),
            "interpretation_negative_infinity": lambda annotation: annotation[
                "confidence"
            ].__setitem__(
                "interpretation",
                float("-inf"),
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                store, _candidates_path, actions_path, _reference_path = (
                    self.make_store(Path(temporary))
                )
                payload = self.action_payload(store.state())
                mutate(payload["adjudicated_annotation"])
                with self.assertRaisesRegex(
                    ClinicalInputError,
                    "NaN/Infinity",
                ):
                    store.save_action(payload)
                self.assertEqual("", actions_path.read_text(encoding="utf-8"))

    def test_review_can_edit_only_two_narratives_without_mutating_candidate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, candidates_path, _actions_path, _reference_path = (
                self.make_store(Path(temporary))
            )
            original_candidates = candidates_path.read_bytes()
            payload = self.action_payload(store.state())
            annotation = payload["adjudicated_annotation"]
            annotation["interpretation"] = (
                "The maneuver may be blunt dissection, but the target anatomy "
                "requires surgeon review."
            )
            result = store.save_action(payload)
            self.assertEqual(
                (
                    "The maneuver may be blunt dissection, but the target "
                    "anatomy requires surgeon review."
                ),
                result["action"]["adjudicated_annotation"]["interpretation"],
            )
            self.assertEqual(original_candidates, candidates_path.read_bytes())

    def test_review_cannot_change_evidence_or_ai_metadata(self) -> None:
        mutations = {
            "anchor": lambda annotation: annotation.update(
                {
                    "anchor_source_frame_idx": 3,
                    "anchor_sec": 0.3,
                }
            ),
            "evidence_start": lambda annotation: annotation.update(
                {
                    "evidence_start_source_frame_idx": 0,
                    "evidence_start_sec": 0.0,
                }
            ),
            "source_views": lambda annotation: annotation.__setitem__(
                "source_views",
                ["cam4"],
            ),
            "supersedes_annotation_ids": lambda annotation: annotation.__setitem__(
                "supersedes_annotation_ids",
                ["0704_6-CL0002"],
            ),
            "confidence": lambda annotation: annotation["confidence"].__setitem__(
                "observation",
                0.1,
            ),
            "provenance": lambda annotation: annotation["provenance"].__setitem__(
                "model",
                "other-model",
            ),
        }
        for field, mutate in mutations.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                store, _candidates_path, actions_path, _reference_path = (
                    self.make_store(Path(temporary))
                )
                payload = self.action_payload(store.state())
                mutate(payload["adjudicated_annotation"])
                with self.assertRaisesRegex(
                    ClinicalInputError,
                    "변경할 수 없습니다",
                ):
                    store.save_action(payload)
                self.assertEqual("", actions_path.read_text(encoding="utf-8"))

    def test_candidate_id_and_generated_at_are_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            wrong_case_id = self.candidate("0704_6")
            wrong_case_id["annotation_id"] = "0704_7-CLV2-0001"
            id_store, *_ = self.make_store(
                Path(temporary) / "id",
                candidates=[wrong_case_id],
            )
            with self.assertRaisesRegex(
                ClinicalInputError,
                "annotation_id.*case_id",
            ):
                id_store.state()

            invalid_time = self.candidate("0704_6")
            invalid_time["provenance"]["generated_at"] = "not-a-date"
            time_store, *_ = self.make_store(
                Path(temporary) / "time",
                candidates=[invalid_time],
            )
            with self.assertRaisesRegex(
                ClinicalInputError,
                "ISO 8601 date-time",
            ):
                time_store.state()

    def test_existing_action_log_requires_full_action_schema(self) -> None:
        mutations = {
            "root": lambda action: action.__setitem__(
                "unexpected_root",
                True,
            ),
            "review": lambda action: action["review"].__setitem__(
                "unexpected_review_field",
                True,
            ),
            "reviewed_at": lambda action: action["review"].__setitem__(
                "reviewed_at",
                "not-a-date",
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                store, _candidates_path, actions_path, _reference_path = (
                    self.make_store(Path(temporary))
                )
                store.save_action(self.action_payload(store.state()))
                action = json.loads(actions_path.read_text(encoding="utf-8"))
                mutate(action)
                actions_path.write_text(
                    canonical_json(action) + "\n",
                    encoding="utf-8",
                )
                expected = (
                    "ISO 8601 date-time"
                    if label == "reviewed_at"
                    else "action JSON Schema"
                )
                with self.assertRaisesRegex(ClinicalInputError, expected):
                    store.actions()


class ClinicalReviewApiTest(ClinicalReviewStoreTest):
    class Frames:
        def frame(self, view: str, source_frame_idx: int):
            del view, source_frame_idx
            return b"fixture", "image/jpeg", 0

    def make_legacy_store(
        self,
        root: Path,
        *,
        case_id: str,
    ) -> ReviewStore:
        case_dir = root / "observable" / "cases" / case_id
        case_dir.mkdir(parents=True)
        timeline_path = case_dir / "cam4_frame_timeline.v1.json"
        timeline = self.timeline(case_id)
        timeline_path.write_text(
            canonical_json(timeline) + "\n",
            encoding="utf-8",
        )
        candidate_path = (
            case_dir / "interaction_candidates.ai_review.v1.jsonl"
        )
        candidate_path.write_text("", encoding="utf-8")
        return ReviewStore(
            case_dir=case_dir,
            candidates_path=candidate_path,
            timeline_path=timeline_path,
            decisions_path=case_dir / "human_review_decisions.v1.jsonl",
            stream_kind="interaction",
            media_duration_sec=0.5,
        )

    def test_multi_case_api_isolated_get_post_and_media_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            clinical_6, candidates_path, actions_path, _reference_path = (
                self.make_store(root, case_id="0704_6")
            )
            clinical_7 = ClinicalReviewStore(
                case_dir=root / "clinical_video" / "cases" / "0704_7",
                case_id="0704_7",
                source_timeline=self.timeline("0704_7"),
            )
            legacy_6 = self.make_legacy_store(root, case_id="0704_6")
            legacy_7 = self.make_legacy_store(root, case_id="0704_7")
            media_path = root / "review_corrected.mp4"
            media_path.write_bytes(b"fixture-cam4-video")
            composite_media_path = root / "clinical-composite.mp4"
            composite_media_path.write_bytes(b"fixture-composite-video")
            runtimes = {
                "0704_6": ReviewCaseRuntime.build(
                    store=legacy_6,
                    frames=self.Frames(),
                    media_path=media_path,
                    composite_media_path=composite_media_path,
                    clinical_store=clinical_6,
                ),
                "0704_7": ReviewCaseRuntime.build(
                    store=legacy_7,
                    frames=self.Frames(),
                    clinical_store=clinical_7,
                ),
            }
            static_dir = root / "static"
            static_dir.mkdir()
            (static_dir / "index.html").write_text(
                "<!doctype html><title>fixture</title>",
                encoding="utf-8",
            )
            handler = make_handler(
                store=legacy_6,
                frames=self.Frames(),
                static_dir=static_dir,
                case_runtimes=runtimes,
                default_case_id="0704_6",
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_port}"
            original_candidates = candidates_path.read_bytes()
            try:
                with urllib.request.urlopen(
                    f"{base_url}/api/clinical-review?case=0704_6"
                ) as response:
                    state_6 = json.load(response)
                self.assertEqual("0704_6", state_6["case_id"])
                self.assertTrue(state_6["available"])
                self.assertEqual(
                    "taskplanner.clinical_review_state.v2",
                    state_6["schema"],
                )
                self.assertEqual(
                    ["observation", "interpretation"],
                    state_6["policy"]["editable_annotation_fields"],
                )
                self.assertFalse(
                    state_6["policy"]["annotation_kind_enabled"]
                )
                self.assertFalse(
                    state_6["policy"]["separate_unobservable_type"]
                )
                self.assertEqual(
                    "/api/media/composite.mp4?case=0704_6",
                    state_6["media"]["composite_video_url"],
                )
                self.assertEqual("flir", state_6["media"]["default_view"])
                self.assertEqual(
                    "/api/state?case=0704_6",
                    state_6["context_api"]["timeline_state_url"],
                )
                with urllib.request.urlopen(
                    f"{base_url}/api/media/composite.mp4?case=0704_6"
                ) as response:
                    self.assertEqual(
                        b"fixture-composite-video",
                        response.read(),
                    )
                    composite_etag = response.headers["ETag"]
                head_request = urllib.request.Request(
                    f"{base_url}/api/media/composite.mp4?case=0704_6",
                    method="HEAD",
                )
                with urllib.request.urlopen(head_request) as response:
                    self.assertEqual(
                        len(b"fixture-composite-video"),
                        int(response.headers["Content-Length"]),
                    )
                    self.assertEqual(composite_etag, response.headers["ETag"])
                with urllib.request.urlopen(
                    f"{base_url}/api/media/review.mp4?case=0704_6"
                ) as response:
                    self.assertEqual(b"fixture-cam4-video", response.read())

                with urllib.request.urlopen(
                    f"{base_url}/api/clinical-review?case=0704_7"
                ) as response:
                    state_7 = json.load(response)
                self.assertEqual("0704_7", state_7["case_id"])
                self.assertFalse(state_7["available"])
                self.assertEqual([], state_7["candidates"])
                self.assertEqual(
                    "clinical_candidates.codex_5_6_sol.v2.jsonl",
                    state_7["candidate_source"]["file"],
                )
                self.assertEqual(
                    "taskplanner.clinical_reference.v2",
                    state_7["reference"]["schema"],
                )
                self.assertNotIn("0704_6-CLV2-0001", json.dumps(state_7))

                non_finite_payload = self.action_payload(state_6)
                non_finite_payload["adjudicated_annotation"]["confidence"][
                    "observation"
                ] = float("nan")
                non_finite_request = urllib.request.Request(
                    f"{base_url}/api/clinical-action?case=0704_6",
                    data=json.dumps(non_finite_payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(non_finite_request)
                self.assertEqual(400, context.exception.code)
                self.assertIn(
                    "비표준 JSON 숫자",
                    context.exception.read().decode("utf-8"),
                )
                self.assertFalse(actions_path.exists())

                post_payload = self.action_payload(state_6)
                request = urllib.request.Request(
                    f"{base_url}/api/clinical-action?case=0704_6",
                    data=json.dumps(post_payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request) as response:
                    result = json.load(response)
                self.assertTrue(result["ok"])
                self.assertEqual("0704_6", result["state"]["case_id"])
                self.assertEqual(original_candidates, candidates_path.read_bytes())
                self.assertEqual(1, len(actions_path.read_text().splitlines()))

                unavailable_request = urllib.request.Request(
                    f"{base_url}/api/clinical-action?case=0704_7",
                    data=json.dumps(
                        {
                            "case_id": "0704_7",
                            "revision": state_7["revision"],
                        }
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(unavailable_request)
                self.assertEqual(400, context.exception.code)
                self.assertIn(
                    "candidate",
                    context.exception.read().decode("utf-8"),
                )
                self.assertEqual(1, len(actions_path.read_text().splitlines()))

                missing_case_request = urllib.request.Request(
                    f"{base_url}/api/clinical-action",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as context:
                    urllib.request.urlopen(missing_case_request)
                self.assertEqual(400, context.exception.code)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
