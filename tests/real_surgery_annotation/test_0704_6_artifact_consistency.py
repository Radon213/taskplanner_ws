from __future__ import annotations

import hashlib
import json
import unittest
from collections import Counter
from pathlib import Path

import jsonschema
import yaml

from tools.real_surgery_annotation.interaction_review_gui import (
    FinalReviewBundle,
)

ROOT = Path(__file__).resolve().parents[2]
CASE_DIR = (
    ROOT / "annotations/observable_tool_events/cases/0704_6"
)


def load_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Case07046ArtifactConsistencyTest(unittest.TestCase):
    def test_final_bundle_exposes_reaudit_without_ground_truth_promotion(
        self,
    ) -> None:
        state = FinalReviewBundle(
            manifest_path=CASE_DIR / "annotation_manifest.json"
        ).state()
        attention = state["context_tracks"]["review_attention"]
        self.assertTrue(attention["available"])
        self.assertEqual("context_only_not_ground_truth", attention["scoring_role"])
        self.assertEqual(1, attention["event_count"])
        event = attention["events"][0]
        self.assertEqual("0704_6-T0025", event["event_id"])
        self.assertEqual("ambiguous", event["review_status"])
        self.assertTrue(event["_final_review"]["review_attention"])
        self.assertTrue(event["_final_review"]["read_only"])
        self.assertEqual(
            (1849, 1852),
            (
                event["_final_review"]["overlay_cue"][
                    "start_source_frame_idx"
                ],
                event["_final_review"]["overlay_cue"][
                    "end_source_frame_idx"
                ],
            ),
        )

    def test_manifest_hashes_and_candidate_counts_match(self) -> None:
        manifest = json.loads(
            (CASE_DIR / "annotation_manifest.json").read_text(encoding="utf-8")
        )
        interaction = manifest["minimal_interaction_annotation"]
        phase = manifest["phase_annotation"]
        prior_human_phase = manifest["phase_reference_history"][-1][
            "phase_annotation"
        ]

        interaction_path = CASE_DIR / interaction["ai_review_candidate_file"]
        phase_candidate_path = CASE_DIR / prior_human_phase["candidate_file"]
        phase_path = CASE_DIR / phase["provisional_reference_file"]
        timeline_path = CASE_DIR / interaction["timeline_file"]
        point_schema_path = (CASE_DIR / interaction["event_schema_path"]).resolve()
        catalog_path = CASE_DIR / phase["procedure_catalog_file"]

        self.assertEqual(
            interaction["ai_review_candidate_sha256"],
            sha256_file(interaction_path),
        )
        self.assertEqual(
            prior_human_phase["candidate_sha256"],
            sha256_file(phase_candidate_path),
        )
        self.assertEqual(
            "51f8ea285bf37fa629ce1b689ac56c181048fc8fd1ec4417ccc955d79a9397e2",
            sha256_file(phase_candidate_path),
        )
        self.assertEqual(
            phase["provisional_reference_sha256"],
            sha256_file(phase_path),
        )
        self.assertEqual(interaction["timeline_sha256"], sha256_file(timeline_path))
        self.assertEqual(
            interaction["event_schema_sha256"],
            sha256_file(point_schema_path),
        )
        interval_schema_path = (
            CASE_DIR / interaction["interval_schema_path"]
        ).resolve()
        self.assertEqual(
            interaction["interval_schema_sha256"],
            sha256_file(interval_schema_path),
        )
        correction_path = (
            CASE_DIR / interaction["assistant_adjudication_file"]
        )
        correction_schema_path = (
            CASE_DIR / interaction["assistant_adjudication_schema_path"]
        ).resolve()
        self.assertEqual(
            interaction["assistant_adjudication_sha256"],
            sha256_file(correction_path),
        )
        self.assertEqual(
            interaction["assistant_adjudication_schema_sha256"],
            sha256_file(correction_schema_path),
        )
        reaudit_path = CASE_DIR / interaction["assistant_reaudit_file"]
        reaudit_schema_path = (
            CASE_DIR / interaction["assistant_reaudit_schema_path"]
        ).resolve()
        self.assertEqual(
            interaction["assistant_reaudit_sha256"],
            sha256_file(reaudit_path),
        )
        self.assertEqual(
            interaction["assistant_reaudit_schema_sha256"],
            sha256_file(reaudit_schema_path),
        )
        actions_path = CASE_DIR / interaction["human_decision_file"]
        self.assertEqual(
            interaction["human_decision_sha256"],
            sha256_file(actions_path),
        )
        self.assertEqual(
            phase["procedure_catalog_sha256"],
            sha256_file(catalog_path),
        )

        interaction_counts = Counter(
            record["review_status"] for record in load_jsonl(interaction_path)
        )
        phase_counts = Counter(
            record["review_status"] for record in load_jsonl(phase_path)
        )
        self.assertEqual(
            interaction["review_status_counts"],
            {
                "ambiguous": interaction_counts["ambiguous"],
                "confirmed": interaction_counts["confirmed"],
                "proposed": interaction_counts["proposed"],
                "rejected": interaction_counts["rejected"],
            },
        )
        self.assertEqual(
            phase["review_status_counts"],
            {
                "ambiguous": phase_counts["ambiguous"],
                "confirmed": phase_counts["confirmed"],
                "rejected": phase_counts["rejected"],
            },
        )
        self.assertTrue(interaction["complete"])
        self.assertTrue(phase["complete"])
        self.assertTrue(phase["review_complete"])
        self.assertFalse(phase["scoring_reference_ready"])
        self.assertEqual(
            {"ambiguous": 4, "confirmed": 0, "rejected": 1},
            prior_human_phase["effective_review_status_counts"],
        )
        self.assertEqual(
            (
                "user_authorized_ai_assistant_video_adjudication_"
                "provisional_context_not_scoring_ground_truth"
            ),
            phase["authority"],
        )
        self.assertEqual(
            "explicit_user_override",
            phase["review_authority"]["authority"],
        )
        phase_records = load_jsonl(phase_path)
        self.assertEqual(
            [0, 1137, 1324, 1465],
            [record["source_frame_idx"] for record in phase_records],
        )
        for record in phase_records:
            self.assertEqual(
                "assistant_video_adjudication",
                record["label_origin"],
            )
            self.assertEqual(
                "task_owner_explicit_user_override_2026-07-29",
                record["review"]["authorized_by"],
            )
        self.assertFalse(manifest["annotation_adjudication"]["complete"])
        self.assertEqual("", manifest["shadow_replay"]["start_phase_id"])
        self.assertEqual(
            "procedure_default_only_no_case_annotation_bootstrap",
            manifest["shadow_replay"]["authority"],
        )
        speech = manifest["speech_timeline"]
        speech_path = CASE_DIR / speech["file"]
        speech_schema_path = (CASE_DIR / speech["schema_file"]).resolve()
        voice_events = load_jsonl(speech_path)
        self.assertEqual(22, speech["event_count"])
        self.assertEqual(22, len(voice_events))
        self.assertEqual(speech["sha256"], sha256_file(speech_path))
        self.assertEqual(
            speech["schema_sha256"],
            sha256_file(speech_schema_path),
        )
        self.assertEqual(
            "source_bag_public_transcript_not_evaluation_ground_truth",
            speech["authority"],
        )
        self.assertEqual(
            "context_only_not_ground_truth",
            speech["scoring_role"],
        )
        second_adson_voice = voice_events[2]
        self.assertEqual("0704_6-V0003", second_adson_voice["event_id"])
        self.assertEqual(10.55, second_adson_voice["time_sec"])
        self.assertEqual("Adson 하나 더", second_adson_voice["text"])
        self.assertEqual(11.22, second_adson_voice["available_sec"])
        self.assertGreaterEqual(
            second_adson_voice["available_sec"],
            second_adson_voice["end_sec"],
        )
        self.assertNotIn("source_frame_idx", second_adson_voice)

    def test_final_interaction_and_dt_projection_are_consistent(self) -> None:
        manifest = json.loads(
            (CASE_DIR / "annotation_manifest.json").read_text(encoding="utf-8")
        )
        reference = manifest["evaluation_reference"]
        observed_info = reference["observed_reference"]
        dt_info = reference["dt_reference"]
        phase_info = reference["phase_reference"]
        masks_info = reference["evaluation_masks"]
        observed_path = CASE_DIR / observed_info["file"]
        dt_path = CASE_DIR / dt_info["file"]
        phase_path = CASE_DIR / phase_info["file"]
        masks_path = CASE_DIR / masks_info["file"]
        masks_schema_path = (CASE_DIR / masks_info["schema_file"]).resolve()
        policy_path = CASE_DIR / reference["projection_policy_file"]
        report_path = (
            CASE_DIR / reference["projection_report_file"]
        ).resolve()
        boundary_path = (
            CASE_DIR / reference["information_boundary_report_file"]
        ).resolve()

        self.assertTrue(reference["complete"])
        self.assertTrue(reference["phase_reference_included"])
        self.assertEqual(observed_info["sha256"], sha256_file(observed_path))
        self.assertEqual(dt_info["sha256"], sha256_file(dt_path))
        self.assertEqual(phase_info["sha256"], sha256_file(phase_path))
        self.assertEqual(masks_info["sha256"], sha256_file(masks_path))
        self.assertEqual(
            masks_info["schema_sha256"],
            sha256_file(masks_schema_path),
        )
        masks = json.loads(masks_path.read_text(encoding="utf-8"))
        masks_schema = json.loads(
            masks_schema_path.read_text(encoding="utf-8")
        )
        jsonschema.Draft202012Validator(masks_schema).validate(masks)
        self.assertFalse(masks["evaluation_scope"]["held_out_eligible"])
        self.assertEqual(
            reference["projection_policy_sha256"],
            sha256_file(policy_path),
        )
        self.assertEqual(
            reference["projection_report_sha256"],
            sha256_file(report_path),
        )
        self.assertEqual(
            reference["information_boundary_report_sha256"],
            sha256_file(boundary_path),
        )
        boundary = json.loads(boundary_path.read_text(encoding="utf-8"))
        self.assertTrue(boundary["ok"])
        self.assertEqual([], boundary["violations"])

        observed = load_jsonl(observed_path)
        projected = load_jsonl(dt_path)
        phases = load_jsonl(phase_path)
        self.assertEqual(36, len(observed))
        self.assertEqual(28, len(projected))
        self.assertEqual(
            {"implicit_tool_request": 10, "tool_transfer": 26},
            dict(Counter(record["event_type"] for record in observed)),
        )
        self.assertEqual(
            {"implicit_tool_request": 10, "tool_transfer": 18},
            dict(Counter(record["event_type"] for record in projected)),
        )
        self.assertEqual(
            {
                "assistant_video_adjudication": 17,
                "human_video_review": 19,
            },
            dict(Counter(record["label_origin"] for record in observed)),
        )
        self.assertEqual(4, len(phases))
        self.assertEqual(
            ["P03", "P04", "P05", "P06"],
            [record["phase_id"] for record in phases],
        )
        self.assertEqual(
            {"ambiguous": 4},
            dict(Counter(record["review_status"] for record in phases)),
        )

        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual("9e206b3326cab341", report["source_revision"])
        self.assertEqual(
            reference["adjudication_revision"],
            report["adjudication_revision"],
        )
        self.assertEqual(
            [
                ["0704_6-T0006", "0704_6-T0029"],
                ["0704_6-T0013", "0704_6-T0014"],
            ],
            [
                item["source_event_ids"]
                for item in report["operations"]["excluded_roundtrips"]
            ],
        )
        self.assertEqual(
            [
                ["0704_6-T0003", "0704_6-T0004"],
                ["0704_6-T0018", "0704_6-T0019"],
                ["0704_6-T0022", "0704_6-T0031"],
            ],
            [
                item["source_event_ids"]
                for item in report["operations"]["collapsed_returns"]
            ],
        )
        self.assertEqual(
            [
                ["0704_6-T0008", "0704_6-T0009"],
                ["0704_6-T0016", "0704_6-T0017"],
                ["0704_6-T0020", "0704_6-T0021"],
            ],
            [
                item["source_event_ids"]
                for item in report["compound_action_episodes"]
            ],
        )
        self.assertTrue(
            all(
                item["direct_observation"] is False
                for item in report["projection_provenance"]
            )
        )
        for event_id in ("0704_6-T0004", "0704_6-T0019", "0704_6-T0031"):
            collapsed = next(
                record
                for record in projected
                if record["event_id"] == event_id
            )
            self.assertEqual("surgeon", collapsed["from"])
            self.assertEqual("mayo_stand", collapsed["to"])

        observed_by_id = {record["event_id"]: record for record in observed}
        projected_by_id = {
            record["event_id"]: record for record in projected
        }
        self.assertEqual(
            (
                "adson_forceps",
                "scrub_nurse",
                "operative_person_role_unresolved",
                176,
                ["cam1", "cam2"],
            ),
            (
                observed_by_id["0704_6-T0032"]["tool"],
                observed_by_id["0704_6-T0032"]["from"],
                observed_by_id["0704_6-T0032"]["to"],
                observed_by_id["0704_6-T0032"]["source_frame_idx"],
                observed_by_id["0704_6-T0032"]["source_views"],
            ),
        )
        self.assertEqual(
            "surgeon",
            projected_by_id["0704_6-T0032"]["to"],
        )
        self.assertEqual(
            "CAM1/CAM2에서 두 번째 Adson 포셉이 스크럽 인력의 손에서 화면에 보이는 수술측 보조 인력의 손으로 넘어가며 f176에서 스크럽 손이 처음 분리된다. 수령자의 정확한 역할은 영상만으로 구분되지 않는다.",
            observed_by_id["0704_6-T0032"]["review_presentation"][
                "observation_ko"
            ],
        )
        self.assertEqual(
            ("surgeon", "scrub_nurse", 614),
            (
                observed_by_id["0704_6-T0003"]["from"],
                observed_by_id["0704_6-T0003"]["to"],
                observed_by_id["0704_6-T0003"]["source_frame_idx"],
            ),
        )
        self.assertEqual(
            {
                "0704_6-R0001": (87, 135),
                "0704_6-R0002": (196, 239),
                "0704_6-R0003": (617, 653),
                "0704_6-R0004": (809, 833),
                "0704_6-R0011": (1051, 1090),
                "0704_6-R0006": (1146, 1180),
                "0704_6-R0007": (1252, 1309),
                "0704_6-R0008": (1351, 1447),
                "0704_6-R0009": (1622, 1648),
                "0704_6-R0010": (1724, 1746),
            },
            {
                event_id: (
                    record["start_source_frame_idx"],
                    record["end_source_frame_idx"],
                )
                for event_id, record in observed_by_id.items()
                if record["event_type"] == "implicit_tool_request"
            },
        )
        for removed_id in (
            "0704_6-T0028",
            "0704_6-T0023",
            "0704_6-T0027",
            "0704_6-R0012",
            "0704_6-T0025",
        ):
            self.assertNotIn(removed_id, observed_by_id)
        self.assertEqual(
            (
                "retractor_bundle_unresolved",
                "operative_person_role_unresolved",
                "scrub_nurse",
                1883,
            ),
            (
                observed_by_id["0704_6-T0026"]["tool"],
                observed_by_id["0704_6-T0026"]["from"],
                observed_by_id["0704_6-T0026"]["to"],
                observed_by_id["0704_6-T0026"]["source_frame_idx"],
            ),
        )
        self.assertNotIn(
            "0704_6-T0026",
            {record["event_id"] for record in projected},
        )
        self.assertEqual(
            [],
            report["operations"]["excluded_unclosed_direct_returns"],
        )
        self.assertEqual(
            ["0704_6-T0026"],
            [
                item["source_event_id"]
                for item in report["operations"][
                    "excluded_unresolved_transfers"
                ]
            ],
        )
        self.assertEqual(
            ["0704_6-T0032"],
            [
                item["source_event_id"]
                for item in report["operations"]["normalized_recipients"]
            ],
        )
        self.assertEqual(
            ("0704_6-T0025", 1849, 1852, 1852),
            (
                report["review_attention"][0]["event_id"],
                report["review_attention"][0]["review_presentation"][
                    "evidence_start_source_frame_idx"
                ],
                report["review_attention"][0]["review_presentation"][
                    "evidence_end_source_frame_idx"
                ],
                report["review_attention"][0]["source_frame_idx"],
            ),
        )

        for superseded in reference["superseded_references"]:
            for file_key, hash_key in (
                ("observed_file", "observed_sha256"),
                ("dt_file", "dt_sha256"),
                ("report_file", "report_sha256"),
            ):
                path = (CASE_DIR / superseded[file_key]).resolve()
                self.assertEqual(
                    superseded[hash_key],
                    sha256_file(path),
                )

    def test_phase_catalog_is_video_derived_and_unfrozen(self) -> None:
        catalog = yaml.safe_load(
            (CASE_DIR / "procedure_phases.ai_review.v1.yaml").read_text(
                encoding="utf-8"
            )
        )
        phase_candidates = load_jsonl(
            CASE_DIR / "phase_candidates.ai_review.v1.jsonl"
        )
        phases = catalog["phases"]
        phases_by_id = {phase["phase_id"]: phase for phase in phases}

        self.assertEqual(
            catalog["phase_order"],
            [phase["phase_id"] for phase in phases],
        )
        self.assertEqual(len(phases), len(phases_by_id))
        self.assertEqual("0704_6", catalog["development_policy"]["baseline_video"])
        self.assertEqual(
            "superseded_not_phase_evidence",
            catalog["development_policy"]["prior_procedure_document_role"],
        )
        self.assertEqual(
            [f"P{index:02d}" for index in range(1, 11)],
            catalog["phase_order"],
        )
        self.assertEqual(
            "evaluation_only_draft_not_frozen",
            catalog["runtime_status"],
        )
        self.assertFalse(
            catalog["information_boundary"]["ground_truth_runtime_input_allowed"]
        )
        self.assertFalse(
            catalog["information_boundary"]["case_timestamps_in_this_document"]
        )

        observed_ids = {
            phase["phase_id"]
            for phase in phases
            if phase["observed_in_0704_6"]
        }
        candidate_ids = {
            record["phase_id"]
            for record in phase_candidates
            if record["event_type"] == "phase_start"
        }
        self.assertEqual(observed_ids, candidate_ids)

        for phase in phases:
            if phase["observed_in_0704_6"]:
                self.assertEqual(
                    "0704_6_video_observed",
                    phase["definition_source"],
                )
            else:
                self.assertEqual("demo_authored", phase["definition_source"])
                self.assertNotIn(phase["phase_id"], candidate_ids)
            self.assertNotIn("start_sec", phase)
            self.assertNotIn("end_sec", phase)

        first = min(phase_candidates, key=lambda record: record["time_sec"])
        self.assertEqual(0, first["source_frame_idx"])
        self.assertEqual("clip_initial_state", first["phase_boundary_kind"])


if __name__ == "__main__":
    unittest.main()
