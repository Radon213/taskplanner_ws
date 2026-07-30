from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.real_surgery_annotation import build_policy02_review_index as indexer
from tools.real_surgery_annotation.run_marlin2_policy02_batch import (
    PASS_SPECS,
)
from tools.real_surgery_annotation.run_marlin2_proposals import (
    MODEL_QUERIES,
    MODEL_QUERY_POLICY_ID,
    canonical_json_sha256,
    sha256_file,
)


CASE_ID = "0704_7"
SPECS = {item.name: item for item in PASS_SPECS}


def timeline_fixture() -> dict:
    return {
        "schema": "taskplanner.video_frame_timeline.v1",
        "case_id": CASE_ID,
        "source_fps": 1.0,
        "frame_count": 6,
        "start_sec": 0.0,
        "end_sec": 12.0,
        "timestamps_sec": [0.0, 1.0, 2.0, 10.0, 11.0, 12.0],
        "gaps": [
            {
                "before_frame_idx": 2,
                "after_frame_idx": 3,
                "before_time_sec": 2.0,
                "after_time_sec": 10.0,
                "delta_sec": 8.0,
            }
        ],
    }


def find_results(pass_name: str) -> list[dict]:
    return [
        {
            "event_type": event_type,
            "query": query,
            "raw": f"raw:{pass_name}:{event_type}:{query_index}",
            "model_format_ok": False,
            "format_ok": False,
            "validation_errors": ["model_format_not_ok"],
            "local_span_sec": None,
            "mapped_span": None,
            "midpoint_mapping": None,
        }
        for event_type in SPECS[pass_name].event_types
        for query_index, query in enumerate(MODEL_QUERIES[event_type], 1)
    ]


def candidate(
    *,
    event_type: str,
    bag_time_sec: float,
    source_frame_idx: int,
    segment_id: str,
) -> dict:
    return {
        "event_type": event_type,
        "review_status": "proposed",
        "label_origin": "temporal_grounding_model",
        "tool_hint": "fixture_tool_hint",
        "time": {
            "method": "fixture",
            "bag_time_sec": bag_time_sec,
            "source_frame_idx": source_frame_idx,
            "source_time_sec": float(source_frame_idx),
            "observability_segment_id": segment_id,
        },
    }


def anchor_record(
    *,
    pass_name: str,
    anchor_id: str,
    clip_start_sec: float,
    clip_end_sec: float,
    segment_id: str,
    candidates: list[dict] | None = None,
) -> dict:
    return {
        "schema": "taskplanner.marlin2_anchor_evidence.v1",
        "case_id": CASE_ID,
        "processing_status": "completed",
        "anchor": {
            "anchor_id": anchor_id,
            "time_sec": clip_start_sec,
            "semantic": "fixture",
        },
        "anchor_mapping": {
            "observable": True,
            "observability_segment_id": segment_id,
        },
        "clip": {
            "observability_segment_id": segment_id,
            "start": {"bag_time_sec": clip_start_sec},
            "end": {"bag_time_sec": clip_end_sec},
        },
        "caption": {"skipped": True},
        "find_results": find_results(pass_name),
        "consensus_candidates": candidates or [],
    }


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(record, sort_keys=True) + "\n" for record in records
        ),
        encoding="utf-8",
    )


def write_child_report(
    *,
    pass_name: str,
    proposal_path: Path,
    report_path: Path,
    timeline_path: Path,
    record_count: int,
    raw_query_count: int,
    candidate_count: int,
    model_revision: str = "fixture-revision",
) -> None:
    spec = SPECS[pass_name]
    queries = {
        event_type: MODEL_QUERIES[event_type]
        for event_type in spec.event_types
    }
    report = {
        "schema": "taskplanner.marlin2_proposal_run.v1",
        "case_id": CASE_ID,
        "status": "completed",
        "authority": "proposal_only_not_ground_truth",
        "model": {
            "id": "NemoStation/Marlin-2B",
            "revision": model_revision,
            "local_path": "/fixture/model",
        },
        "inputs": {
            "video": "/fixture/video.avi",
            "video_sha256": "a" * 64,
            "timeline": str(timeline_path.resolve()),
            "timeline_sha256": sha256_file(timeline_path),
            "anchors": f"/fixture/{pass_name}-anchors.json",
            "anchors_sha256": (
                "b" * 64 if pass_name == "transcript" else "c" * 64
            ),
        },
        "settings": {
            "query_policy_id": MODEL_QUERY_POLICY_ID,
            "query_prompt_sha256": canonical_json_sha256(queries),
            "event_types": list(spec.event_types),
            "queries": queries,
            "clip_before_sec": spec.clip_before_sec,
            "clip_after_sec": spec.clip_after_sec,
            "skip_caption": True,
        },
        "counts": {
            "anchor_count": record_count,
            "raw_query_count": raw_query_count,
            "consensus_candidate_count": candidate_count,
            "skipped_anchor_inside_gap_count": 0,
        },
        "output": str(proposal_path.resolve()),
        "output_sha256": sha256_file(proposal_path),
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, sort_keys=True),
        encoding="utf-8",
    )


def write_voice(path: Path) -> None:
    records = [
        {
            "schema": "taskplanner.observable_voice_point.v2",
            "case_id": CASE_ID,
            "event_id": f"{CASE_ID}-V0001",
            "event_type": "voice_utterance",
            "time_sec": 8.0,
            "end_sec": 8.5,
            "available_sec": 9.0,
            "text": "causally available before cluster",
        },
        {
            "schema": "taskplanner.observable_voice_point.v2",
            "case_id": CASE_ID,
            "event_id": f"{CASE_ID}-V0002",
            "event_type": "voice_utterance",
            "time_sec": 9.5,
            "end_sec": 10.2,
            "available_sec": 10.6,
            "text": "not available at the cluster median",
        },
        {
            "schema": "taskplanner.observable_voice_point.v2",
            "case_id": CASE_ID,
            "event_id": f"{CASE_ID}-V0003",
            "event_type": "voice_utterance",
            "time_sec": 11.0,
            "end_sec": 11.2,
            "available_sec": 11.2,
            "text": "voice without a later proposal",
        },
        {
            "schema": "taskplanner.observable_voice_point.v2",
            "case_id": CASE_ID,
            "event_id": f"{CASE_ID}-V0004",
            "event_type": "voice_utterance",
            "time_sec": 20.0,
            "end_sec": 20.5,
            "available_sec": 21.0,
            "text": "voice after the observable video",
        },
    ]
    write_jsonl(path, records)


def build_fixture(root: Path) -> dict[str, Path]:
    timeline_path = root / "timeline.json"
    timeline_path.write_text(
        json.dumps(timeline_fixture(), sort_keys=True),
        encoding="utf-8",
    )
    transcript_path = root / "transcript.jsonl"
    transcript_records = [
        anchor_record(
            pass_name="transcript",
            anchor_id=f"{CASE_ID}-A001",
            clip_start_sec=10.0,
            clip_end_sec=12.0,
            segment_id="segment_0002",
            candidates=[
                candidate(
                    event_type="implicit_tool_request",
                    bag_time_sec=10.0,
                    source_frame_idx=3,
                    segment_id="segment_0002",
                )
            ],
        )
    ]
    write_jsonl(transcript_path, transcript_records)
    transcript_report = root / "transcript.report.json"
    write_child_report(
        pass_name="transcript",
        proposal_path=transcript_path,
        report_path=transcript_report,
        timeline_path=timeline_path,
        record_count=1,
        raw_query_count=len(find_results("transcript")),
        candidate_count=1,
    )

    scan_path = root / "scan.jsonl"
    scan_records = [
        anchor_record(
            pass_name="scan",
            anchor_id=f"{CASE_ID}-S001",
            clip_start_sec=0.0,
            clip_end_sec=2.0,
            segment_id="segment_0001",
        ),
        anchor_record(
            pass_name="scan",
            anchor_id=f"{CASE_ID}-S002",
            clip_start_sec=10.0,
            clip_end_sec=12.0,
            segment_id="segment_0002",
            candidates=[
                candidate(
                    event_type="scrub_nurse_to_surgeon",
                    bag_time_sec=11.0,
                    source_frame_idx=4,
                    segment_id="segment_0002",
                )
            ],
        ),
    ]
    write_jsonl(scan_path, scan_records)
    scan_report = root / "scan.report.json"
    write_child_report(
        pass_name="scan",
        proposal_path=scan_path,
        report_path=scan_report,
        timeline_path=timeline_path,
        record_count=2,
        raw_query_count=2 * len(find_results("scan")),
        candidate_count=1,
    )
    voice_path = root / "voice.jsonl"
    write_voice(voice_path)
    return {
        "timeline": timeline_path,
        "transcript": transcript_path,
        "transcript_report": transcript_report,
        "scan": scan_path,
        "scan_report": scan_report,
        "voice": voice_path,
    }


def call_build(paths: dict[str, Path]) -> dict:
    return indexer.build_index(
        case_id=CASE_ID,
        transcript_proposals_path=paths["transcript"],
        transcript_report_path=paths["transcript_report"],
        scan_proposals_path=paths["scan"],
        scan_report_path=paths["scan_report"],
        voice_path=paths["voice"],
        timeline_path=paths["timeline"],
        cluster_threshold_sec=1.0,
        review_pad_before_sec=2.0,
        review_pad_after_sec=2.0,
        voice_lookback_sec=8.0,
        voice_window_before_sec=1.25,
        voice_window_after_sec=4.25,
    )


class Policy02ReviewIndexTest(unittest.TestCase):
    def test_event_agnostic_cluster_preserves_all_model_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            index = call_build(build_fixture(Path(temporary)))

        self.assertEqual(
            "proposal_index_only_not_ground_truth",
            index["authority"],
        )
        self.assertEqual(2, index["counts"]["consensus_candidate_count"])
        self.assertEqual(1, index["counts"]["candidate_cluster_count"])
        self.assertEqual(28, index["counts"]["raw_query_evidence_count"])
        cluster = index["candidate_clusters"][0]
        self.assertEqual(
            "event_agnostic_proposal_cluster",
            cluster["review_item_type"],
        )
        self.assertEqual(2, len(cluster["source_proposals"]))
        self.assertEqual(10.5, cluster["cluster_time_sec"])
        self.assertIsNone(cluster["adjudication"])
        proposed_types = {
            item["source"]["model_proposed_event_type"]
            for item in cluster["source_proposals"]
        }
        self.assertEqual(
            {"implicit_tool_request", "scrub_nurse_to_surgeon"},
            proposed_types,
        )
        raw_ids = {
            item["raw_query_ref_id"]
            for item in index["raw_query_evidence"]
        }
        for proposal in cluster["source_proposals"]:
            self.assertTrue(
                set(proposal["source"]["raw_query_ref_ids"]) <= raw_ids
            )
        self.assertNotIn("tool", cluster)
        self.assertNotIn("direction", cluster)
        self.assertNotIn("request_boundary", cluster)
        self.assertTrue(
            index["prohibitions"]["auto_confirmation_forbidden"]
        )

    def test_voice_linking_uses_available_sec_and_keeps_false_negative_windows(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            index = call_build(build_fixture(Path(temporary)))

        nearby = index["candidate_clusters"][0][
            "nearby_causally_available_voice"
        ]
        self.assertEqual(
            [f"{CASE_ID}-V0001", f"{CASE_ID}-V0002"],
            [item["voice_event"]["event_id"] for item in nearby],
        )
        self.assertEqual(
            "all_cluster_candidates",
            nearby[0]["causal_availability"],
        )
        self.assertEqual(
            "some_cluster_candidates",
            nearby[1]["causal_availability"],
        )
        voice_only = index["voice_only_false_negative_windows"]
        self.assertEqual(2, len(voice_only))
        self.assertEqual(
            {f"{CASE_ID}-V0003", f"{CASE_ID}-V0004"},
            {item["voice_event"]["event_id"] for item in voice_only},
        )
        self.assertTrue(
            all(item["no_marlin_candidate_linked"] for item in voice_only)
        )
        post_video = next(
            item
            for item in voice_only
            if item["voice_event"]["event_id"] == f"{CASE_ID}-V0004"
        )
        self.assertTrue(
            post_video["review_window"]["outside_video_timeline"]
        )
        self.assertFalse(
            post_video["review_window"]["evaluation_possible"]
        )

    def test_scan_coverage_excludes_gap_and_reports_wall_clock_ratio(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            index = call_build(build_fixture(Path(temporary)))

        coverage = index["full_scan_temporal_coverage"]
        self.assertEqual(1.0, coverage["observable_coverage_ratio"])
        self.assertAlmostEqual(1 / 3, coverage["wall_clock_coverage_ratio"])
        self.assertEqual(1, coverage["gap_count"])
        self.assertEqual(8.0, coverage["gap_duration_sec"])
        self.assertEqual(0.0, coverage["gap_coverage_sec"])
        self.assertFalse(coverage["gaps"][0]["evaluation_possible"])
        cluster_window = index["candidate_clusters"][0]["review_window"]
        self.assertTrue(cluster_window["no_inference_across_gap"])
        self.assertFalse(cluster_window["fully_observable"])

    def test_tampered_child_output_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = build_fixture(Path(temporary))
            with paths["scan"].open("a", encoding="utf-8") as stream:
                stream.write("\n")
            with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                call_build(paths)

    def test_cross_pass_model_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = build_fixture(Path(temporary))
            report = json.loads(
                paths["scan_report"].read_text(encoding="utf-8")
            )
            report["model"]["revision"] = "different-revision"
            paths["scan_report"].write_text(
                json.dumps(report),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "model identity differs"):
                call_build(paths)

    def test_query_policy_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = build_fixture(Path(temporary))
            report = json.loads(
                paths["scan_report"].read_text(encoding="utf-8")
            )
            report["settings"]["query_policy_id"] = "wrong-policy"
            paths["scan_report"].write_text(
                json.dumps(report),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "query policy mismatch"):
                call_build(paths)

    def test_cli_is_create_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = build_fixture(root)
            output = root / "index.json"
            argv = [
                "build_policy02_review_index.py",
                "--case-id",
                CASE_ID,
                "--transcript-proposals",
                str(paths["transcript"]),
                "--transcript-report",
                str(paths["transcript_report"]),
                "--scan-proposals",
                str(paths["scan"]),
                "--scan-report",
                str(paths["scan_report"]),
                "--voice",
                str(paths["voice"]),
                "--timeline",
                str(paths["timeline"]),
                "--output",
                str(output),
            ]
            with mock.patch.object(sys, "argv", argv):
                self.assertEqual(0, indexer.main())
            original = output.read_bytes()
            with (
                mock.patch.object(sys, "argv", argv),
                self.assertRaisesRegex(SystemExit, "refusing to overwrite"),
            ):
                indexer.main()
            self.assertEqual(original, output.read_bytes())


if __name__ == "__main__":
    unittest.main()
