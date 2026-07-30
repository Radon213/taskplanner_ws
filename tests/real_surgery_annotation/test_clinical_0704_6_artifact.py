from __future__ import annotations

import hashlib
import json
import re
import unittest
from collections import Counter
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from tools.real_surgery_annotation.clinical_review_store import (
    ClinicalReviewStore,
)


ROOT = Path(__file__).resolve().parents[2]
CLINICAL_ROOT = ROOT / "annotations" / "clinical_video"
CASE_ID = "0704_6"
CASE_DIR = CLINICAL_ROOT / "cases" / CASE_ID
OBSERVABLE_CASE_DIR = (
    ROOT / "annotations" / "observable_tool_events" / "cases" / CASE_ID
)
V1_CANDIDATES_PATH = (
    CASE_DIR / "clinical_candidates.codex_5_6_sol.v1.jsonl"
)
V1_MANIFEST_PATH = CASE_DIR / "clinical_manifest.v1.json"
V2_CANDIDATES_PATH = (
    CASE_DIR / "clinical_candidates.codex_5_6_sol.v2.jsonl"
)
V2_MANIFEST_PATH = CASE_DIR / "clinical_manifest.v2.json"
TIMELINE_PATH = OBSERVABLE_CASE_DIR / "cam4_frame_timeline.v1.json"
RFDETR_PATH = (
    ROOT
    / "tools"
    / "real_surgery_annotation"
    / "web_interaction_review"
    / "rfdetr_overlays"
    / "0704_6.json"
)
EXPECTED_V1_CANDIDATE_SHA256 = (
    "bc1ac56c810374e1029ae1685ee10e4468263372bf495ee45f15b6fbba976e27"
)
EXPECTED_V1_KIND_COUNTS = {
    "activity_segment": 14,
    "clinical_observation": 6,
    "state_change": 4,
    "unobservable_span": 5,
}
EXPECTED_V2_CANDIDATE_COUNT = 21
EXPECTED_RFDETR_SHA256 = (
    "e3e46b2ee4a300d27df21745f5e7ce7f03ba1d19e9d20c12109852ef94341479"
)


def load_json(path: Path) -> dict:
    return json.loads(
        path.read_text(encoding="utf-8"),
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(f"non-standard JSON number: {value}")
        ),
    )


def load_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(
            line,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-standard JSON number: {value}")
            ),
        )
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class Clinical07046ArtifactTest(unittest.TestCase):
    @staticmethod
    def sentence_count(value: str) -> int:
        return len(
            re.findall(
                r"""[.!?](?:["'”’)\]]+)?(?=\s|$)""",
                value,
            )
        )

    def test_legacy_v1_ai_draft_is_preserved(self) -> None:
        manifest = load_json(V1_MANIFEST_PATH)
        candidates = load_jsonl(V1_CANDIDATES_PATH)
        manifest_schema = load_json(
            CLINICAL_ROOT / "schema" / "clinical_manifest.v1.schema.json"
        )
        annotation_schema = load_json(
            CLINICAL_ROOT
            / "schema"
            / "clinical_video_annotation.v1.schema.json"
        )
        Draft202012Validator(
            manifest_schema,
            format_checker=FormatChecker(),
        ).validate(manifest)
        validator = Draft202012Validator(
            annotation_schema,
            format_checker=FormatChecker(),
        )
        for candidate in candidates:
            validator.validate(candidate)
        self.assertEqual(
            EXPECTED_V1_CANDIDATE_SHA256,
            hashlib.sha256(V1_CANDIDATES_PATH.read_bytes()).hexdigest(),
        )
        self.assertEqual(29, len(candidates))
        self.assertEqual(
            EXPECTED_V1_KIND_COUNTS,
            dict(Counter(item["annotation_kind"] for item in candidates)),
        )
        self.assertEqual(
            EXPECTED_V1_KIND_COUNTS,
            manifest["candidate_kind_counts"],
        )

    def test_v2_ai_draft_is_two_field_canonical_and_review_ready(self) -> None:
        manifest = load_json(V2_MANIFEST_PATH)
        timeline = load_json(TIMELINE_PATH)
        candidates = load_jsonl(V2_CANDIDATES_PATH)
        manifest_schema = load_json(
            CLINICAL_ROOT / "schema" / "clinical_manifest.v2.schema.json"
        )
        annotation_schema = load_json(
            CLINICAL_ROOT
            / "schema"
            / "clinical_video_annotation.v2.schema.json"
        )
        action_schema = load_json(
            CLINICAL_ROOT / "schema" / "clinical_review_action.v2.schema.json"
        )
        for schema in (manifest_schema, annotation_schema, action_schema):
            Draft202012Validator.check_schema(schema)
        Draft202012Validator(
            manifest_schema,
            format_checker=FormatChecker(),
        ).validate(manifest)
        annotation_validator = Draft202012Validator(
            annotation_schema,
            format_checker=FormatChecker(),
        )
        for candidate in candidates:
            annotation_validator.validate(candidate)

        candidate_sha256 = hashlib.sha256(
            V2_CANDIDATES_PATH.read_bytes()
        ).hexdigest()
        timeline_sha256 = hashlib.sha256(
            TIMELINE_PATH.read_bytes()
        ).hexdigest()
        self.assertEqual(candidate_sha256, manifest["candidate_sha256"])
        self.assertEqual(timeline_sha256, manifest["source_timeline"]["sha256"])
        self.assertEqual(
            ["observation", "interpretation"],
            manifest["content_fields"],
        )
        self.assertEqual(EXPECTED_V2_CANDIDATE_COUNT, len(candidates))
        self.assertEqual(EXPECTED_V2_CANDIDATE_COUNT, manifest["candidate_count"])
        self.assertNotIn("candidate_kind_counts", manifest)

        self.assertEqual(
            EXPECTED_RFDETR_SHA256,
            hashlib.sha256(RFDETR_PATH.read_bytes()).hexdigest(),
        )
        detector_sources = [
            source
            for source in manifest["context_sources"]
            if source.get("role")
            == "ai_inference_reference_not_ground_truth"
        ]
        self.assertEqual(1, len(detector_sources))
        detector_source = detector_sources[0]
        self.assertEqual(
            "ai_inference_reference_not_ground_truth",
            detector_source["authority"],
        )
        self.assertEqual(EXPECTED_RFDETR_SHA256, detector_source["sha256"])
        self.assertEqual(
            RFDETR_PATH.resolve(),
            (CASE_DIR / detector_source["file"]).resolve(),
        )

        timestamps = timeline["timestamps_sec"]
        self.assertEqual(1937, len(timestamps))
        self.assertEqual(0.0, timestamps[0])
        self.assertEqual(138.428400517, timestamps[-1])
        superseded_ids: set[str] = set()
        source_view_union: set[str] = set()
        old_semantic_fields = {
            "annotation_kind",
            "activity",
            "anatomy",
            "observable_findings",
            "clinical_interpretations",
            "observability",
            "field_confidence",
            "activity_start_sec",
            "activity_end_sec",
        }
        for index, candidate in enumerate(candidates, 1):
            self.assertEqual(
                f"{CASE_ID}-CLV2-{index:04d}",
                candidate["annotation_id"],
            )
            self.assertTrue(old_semantic_fields.isdisjoint(candidate))
            for narrative_field in ("observation", "interpretation"):
                value = candidate[narrative_field]
                self.assertEqual(value, " ".join(value.split()))
                self.assertLessEqual(len(value), 600)
                self.assertIn(
                    self.sentence_count(value),
                    (1, 2),
                    f"{candidate['annotation_id']} {narrative_field}",
                )
            self.assertEqual(
                {"observation", "interpretation"},
                set(candidate["confidence"]),
            )
            for frame_field, time_field in (
                ("anchor_source_frame_idx", "anchor_sec"),
                (
                    "evidence_start_source_frame_idx",
                    "evidence_start_sec",
                ),
                (
                    "evidence_end_source_frame_idx",
                    "evidence_end_sec",
                ),
            ):
                self.assertEqual(
                    timestamps[candidate[frame_field]],
                    candidate[time_field],
                )
            self.assertLessEqual(
                candidate["evidence_start_source_frame_idx"],
                candidate["anchor_source_frame_idx"],
            )
            self.assertLessEqual(
                candidate["anchor_source_frame_idx"],
                candidate["evidence_end_source_frame_idx"],
            )
            self.assertFalse(
                candidate["evidence_start_source_frame_idx"] <= 1050
                and candidate["evidence_end_source_frame_idx"] >= 1051,
                candidate["annotation_id"],
            )
            source_view_union.update(candidate["source_views"])
            audit_mapping = candidate.get(
                "supersedes_annotation_ids",
                candidate["provenance"].get("source_annotation_ids", []),
            )
            self.assertTrue(audit_mapping)
            superseded_ids.update(audit_mapping)
            self.assertEqual(
                "Codex 5.6 sol",
                candidate["provenance"]["model"],
            )
            self.assertEqual(
                "ai_draft",
                candidate["provenance"]["authority"],
            )
            self.assertEqual(
                "needs_surgeon_review",
                candidate["review_status"],
            )
        self.assertEqual({"cam4", "flir"}, source_view_union)
        self.assertEqual(
            {f"{CASE_ID}-CL{index:04d}" for index in range(1, 30)},
            superseded_ids,
        )

        store = ClinicalReviewStore(
            case_dir=CASE_DIR,
            case_id=CASE_ID,
            source_timeline=timeline,
            source_timeline_path=TIMELINE_PATH,
        )
        state = store.state()
        self.assertTrue(state["available"])
        self.assertEqual(
            "taskplanner.clinical_review_state.v2",
            state["schema"],
        )
        self.assertEqual(EXPECTED_V2_CANDIDATE_COUNT, state["progress"]["total"])
        self.assertEqual(0, state["progress"]["reviewed"])
        self.assertFalse(state["reference"]["ready"])
        self.assertEqual(
            ["observation", "interpretation"],
            state["policy"]["editable_annotation_fields"],
        )
        self.assertFalse(
            (CASE_DIR / "clinical_review_actions.v2.jsonl").exists()
        )
        self.assertFalse(
            (CASE_DIR / "clinical_reference.final.v2.jsonl").exists()
        )
