from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tools.real_surgery_annotation.publish_clinical_draft import (
    ClinicalDraftPublishError,
    publish_case,
)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


class PublishClinicalDraftTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.case_id = "case_demo"
        self.observable_case_dir = (
            self.root
            / "annotations/observable_tool_events/cases"
            / self.case_id
        )
        self.clinical_case_dir = (
            self.root / "annotations/clinical_video/cases" / self.case_id
        )
        self.media_root = self.root / "review-media"
        self.observable_case_dir.mkdir(parents=True)
        self.clinical_case_dir.mkdir(parents=True)

        timeline = {
            "schema": "taskplanner.video_frame_timeline.v1",
            "case_id": self.case_id,
            "frame_count": 3,
            "start_sec": 0.0,
            "end_sec": 2.0,
            "timestamps_sec": [0.0, 1.0, 2.0],
            "gaps": [],
        }
        timeline_path = self.observable_case_dir / "cam4_frame_timeline.v1.json"
        write_json(timeline_path, timeline)

        context_files: dict[str, Path] = {}
        for name in (
            "voice_events.source.v2.jsonl",
            "interaction_events.observed.final.v1.jsonl",
            "interaction_events.dt_reference.final.v1.jsonl",
            "phase_events.provisional.final.v1.jsonl",
        ):
            path = self.observable_case_dir / name
            path.write_text("{}\n", encoding="utf-8")
            context_files[name] = path

        write_json(
            self.observable_case_dir / "annotation_manifest.json",
            {
                "case_id": self.case_id,
                "minimal_interaction_annotation": {
                    "timeline_file": timeline_path.name,
                    "timeline_sha256": sha256_file(timeline_path),
                },
                "speech_timeline": {
                    "file": "voice_events.source.v2.jsonl",
                    "sha256": sha256_file(
                        context_files["voice_events.source.v2.jsonl"]
                    ),
                },
                "evaluation_reference": {
                    "observed_reference": {
                        "file": "interaction_events.observed.final.v1.jsonl",
                        "sha256": sha256_file(
                            context_files[
                                "interaction_events.observed.final.v1.jsonl"
                            ]
                        ),
                    },
                    "dt_reference": {
                        "file": (
                            "interaction_events.dt_reference.final.v1.jsonl"
                        ),
                        "sha256": sha256_file(
                            context_files[
                                "interaction_events.dt_reference.final.v1.jsonl"
                            ]
                        ),
                    },
                    "phase_reference": {
                        "file": "phase_events.provisional.final.v1.jsonl",
                        "sha256": sha256_file(
                            context_files[
                                "phase_events.provisional.final.v1.jsonl"
                            ]
                        ),
                    },
                },
            },
        )

        candidate_path = (
            self.clinical_case_dir
            / "clinical_candidates.codex_5_6_sol.v2.jsonl"
        )
        candidate_path.write_text(
            json.dumps(
                {
                    "schema": "taskplanner.clinical_video_annotation.v2",
                    "annotation_id": f"{self.case_id}-CLV2-0001",
                    "case_id": self.case_id,
                    "anchor_source_frame_idx": 1,
                    "anchor_sec": 1.0,
                    "evidence_start_source_frame_idx": 0,
                    "evidence_end_source_frame_idx": 2,
                    "evidence_start_sec": 0.0,
                    "evidence_end_sec": 2.0,
                    "observation": "절개창 양쪽이 견인되어 중앙 조직면이 보인다.",
                    "interpretation": "정중선 박리 시야를 확보하는 단계로 해석된다.",
                    "confidence": {
                        "observation": "high",
                        "interpretation": "medium",
                    },
                    "source_views": ["cam4", "flir"],
                    "provenance": {
                        "generator": "Codex",
                        "model": "Codex 5.6 sol",
                        "generated_at": "2026-07-29T12:00:00+09:00",
                        "authority": "ai_draft",
                    },
                    "review_status": "needs_surgeon_review",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

        overlay_path = (
            self.root
            / "tools/real_surgery_annotation/web_interaction_review/"
            "rfdetr_overlays"
            / f"{self.case_id}.json"
        )
        write_json(
            overlay_path,
            {
                "schema": "taskplanner.rfdetr_overlay_bundle.v1",
                "case_id": self.case_id,
                "frame_count": 3,
                "authority": "ai_inference_reference_not_ground_truth",
            },
        )

        media_dir = self.media_root / self.case_id
        media_dir.mkdir(parents=True)
        media_path = media_dir / "review_corrected.mp4"
        media_path.write_bytes(b"review-media-fixture")
        write_json(
            media_dir / "review_corrected.mp4.manifest.json",
            {
                "case_id": self.case_id,
                "output": {
                    "path": str(media_path),
                    "sha256": sha256_file(media_path),
                    "media_probe": {
                        "container_duration_sec": "2.25",
                        "video": {
                            "nb_frames": "3",
                            "width": 1280,
                            "height": 360,
                        },
                    },
                },
            },
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def publish(self, *, generated_at: str) -> dict[str, object]:
        return publish_case(
            repo_root=self.root,
            case_id=self.case_id,
            generated_at=generated_at,
            review_media_root=self.media_root,
        )

    def test_publishes_and_validates_string_media_frame_count(self) -> None:
        result = self.publish(generated_at="2026-07-29T12:30:00+09:00")
        self.assertTrue(result["ok"])
        self.assertFalse(result["already_published"])
        self.assertEqual(1, result["candidate_count"])
        self.assertEqual("ready", result["candidate_source_status"])

    def test_identical_rerun_is_idempotent_across_cli_timestamps(self) -> None:
        self.publish(generated_at="2026-07-29T12:30:00+09:00")
        result = self.publish(generated_at="2026-07-29T12:31:00+09:00")
        self.assertTrue(result["already_published"])

    def test_changed_candidate_fails_create_only(self) -> None:
        self.publish(generated_at="2026-07-29T12:30:00+09:00")
        candidate_path = (
            self.clinical_case_dir
            / "clinical_candidates.codex_5_6_sol.v2.jsonl"
        )
        candidate_path.write_text(
            candidate_path.read_text(encoding="utf-8").replace(
                "중앙 조직면",
                "중앙 연부조직면",
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            ClinicalDraftPublishError,
            "create-only",
        ):
            self.publish(generated_at="2026-07-29T12:32:00+09:00")


if __name__ == "__main__":
    unittest.main()
