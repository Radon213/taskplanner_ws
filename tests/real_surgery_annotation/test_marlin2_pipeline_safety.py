from __future__ import annotations

import concurrent.futures
import contextlib
import io
import json
import math
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

import yaml

from tools.real_surgery_annotation import extract_video_frame_timeline
from tools.real_surgery_annotation import run_marlin2_proposals
from tools.real_surgery_annotation import validate_interaction_points


ROOT = Path(__file__).resolve().parents[2]
INTERACTION_SCHEMA = json.loads(
    (
        ROOT
        / "annotations/observable_tool_events/schema/"
        "observable_interaction_point.v1.schema.json"
    ).read_text(encoding="utf-8")
)


def fixture_timeline(case_id: str = "0704_6") -> dict:
    return {
        "schema": "taskplanner.video_frame_timeline.v1",
        "case_id": case_id,
        "source_fps": 10.0,
        "frame_count": 5,
        "start_sec": 0.0,
        "end_sec": 5.4,
        "timestamps_sec": [0.0, 0.1, 0.2, 5.3, 5.4],
        "gaps": [
            {
                "before_frame_idx": 2,
                "after_frame_idx": 3,
                "before_time_sec": 0.2,
                "after_time_sec": 5.3,
                "delta_sec": 5.1,
            }
        ],
    }


def fixture_request(**updates: object) -> dict:
    record = {
        "_line": 1,
        "schema": "taskplanner.observable_interaction_point.v1",
        "case_id": "0704_6",
        "event_id": "0704_6-R0001",
        "event_type": "implicit_tool_request",
        "time_sec": 0.1,
        "source_frame_idx": 1,
        "source_views": ["cam4"],
        "review_status": "proposed",
        "label_origin": "assistant_visual_proposal",
        "ai_review": {
            "reviewer_model": "gpt-5.6-sol",
            "decision": "recommend",
            "reviewed_at": "2026-07-28T00:00:00Z",
            "evidence": "fixture",
        },
    }
    record.update(updates)
    return record


class AtomicCreateOnlyTest(unittest.TestCase):
    def test_existing_file_is_never_replaced(self) -> None:
        helpers = (
            extract_video_frame_timeline.atomic_create_text,
            run_marlin2_proposals.atomic_create_text,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, helper in enumerate(helpers):
                with self.subTest(helper=helper.__module__):
                    path = root / f"existing-{index}.json"
                    path.write_text("original", encoding="utf-8")
                    with self.assertRaises(FileExistsError):
                        helper(path, "replacement")
                    self.assertEqual("original", path.read_text(encoding="utf-8"))

    def test_parallel_publish_has_exactly_one_complete_winner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "race.json"
            barrier = threading.Barrier(2)

            def publish(value: str) -> tuple[str, str]:
                barrier.wait()
                try:
                    run_marlin2_proposals.atomic_create_text(output, value)
                except FileExistsError:
                    return "collision", value
                return "created", value

            values = ("A" * 100_000, "B" * 100_000)
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(publish, values))

            self.assertEqual(1, sum(status == "created" for status, _ in results))
            self.assertEqual(
                1,
                sum(status == "collision" for status, _ in results),
            )
            self.assertIn(output.read_text(encoding="utf-8"), values)
            self.assertEqual([], list(output.parent.glob(f".{output.name}.*.tmp")))

    def test_output_and_report_must_be_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "same.json"
            with self.assertRaisesRegex(ValueError, "different paths"):
                run_marlin2_proposals.require_distinct_output_paths(path, path)

    def test_timeline_extractor_rejects_non_finite_fps_before_ros_import(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            argv = [
                "extract_video_frame_timeline.py",
                "--source-bag",
                str(root / "bag"),
                "--topic",
                "/camera",
                "--source-fps",
                "nan",
                "--output",
                str(root / "timeline.json"),
                "--case-id",
                "0704_6",
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                self.assertRaisesRegex(SystemExit, "finite and positive"),
            ):
                extract_video_frame_timeline.main()


class GapAndSpanSafetyTest(unittest.TestCase):
    def test_anchor_inside_gap_is_detected(self) -> None:
        timestamps = fixture_timeline()["timestamps_sec"]
        gap = run_marlin2_proposals.containing_gap(
            timestamps,
            source_fps=10.0,
            target_bag_time_sec=2.0,
        )
        self.assertIsNotNone(gap)
        self.assertEqual(2, gap["before_frame_idx"])
        self.assertEqual(3, gap["after_frame_idx"])
        self.assertIsNone(
            run_marlin2_proposals.containing_gap(
                timestamps,
                source_fps=10.0,
                target_bag_time_sec=0.2,
            )
        )

    def test_clip_bounds_never_cross_observability_segment(self) -> None:
        self.assertEqual(
            (0, 2),
            run_marlin2_proposals.clip_frame_bounds(
                anchor_frame_idx=2,
                segment_first_frame_idx=0,
                segment_last_frame_idx=2,
                clip_before_sec=1.0,
                clip_after_sec=1.0,
                source_fps=10.0,
            ),
        )
        self.assertEqual(
            (3, 4),
            run_marlin2_proposals.clip_frame_bounds(
                anchor_frame_idx=3,
                segment_first_frame_idx=3,
                segment_last_frame_idx=4,
                clip_before_sec=1.0,
                clip_after_sec=1.0,
                source_fps=10.0,
            ),
        )

    def test_consensus_requires_corrected_time_and_same_segment(self) -> None:
        timestamps = fixture_timeline()["timestamps_sec"]
        segment_ranges = {"segment_0001": (0, 2), "segment_0002": (3, 4)}

        def result(
            *,
            segment_id: str,
            bag_time_sec: float,
            frame_idx: int,
        ) -> dict:
            return {
                "format_ok": True,
                "local_span_sec": [0.0, 0.2],
                "midpoint_mapping": {
                    "observability_segment_id": segment_id,
                    "bag_time_sec": bag_time_sec,
                    "source_frame_idx": frame_idx,
                },
            }

        self.assertIsNone(
            run_marlin2_proposals.consensus(
                [
                    result(
                        segment_id="segment_0001",
                        bag_time_sec=0.2,
                        frame_idx=2,
                    ),
                    result(
                        segment_id="segment_0002",
                        bag_time_sec=5.3,
                        frame_idx=3,
                    ),
                ],
                timestamps=timestamps,
                max_midpoint_delta_sec=10.0,
                segment_ranges=segment_ranges,
                source_fps=10.0,
            )
        )
        self.assertIsNone(
            run_marlin2_proposals.consensus(
                [
                    result(
                        segment_id="segment_0002",
                        bag_time_sec=0.2,
                        frame_idx=2,
                    ),
                    result(
                        segment_id="segment_0002",
                        bag_time_sec=5.3,
                        frame_idx=3,
                    ),
                ],
                timestamps=timestamps,
                max_midpoint_delta_sec=1.5,
                segment_ranges=segment_ranges,
                source_fps=10.0,
            )
        )
        accepted = run_marlin2_proposals.consensus(
            [
                result(
                    segment_id="segment_0002",
                    bag_time_sec=5.3,
                    frame_idx=3,
                ),
                result(
                    segment_id="segment_0002",
                    bag_time_sec=5.4,
                    frame_idx=4,
                ),
            ],
            timestamps=timestamps,
            max_midpoint_delta_sec=0.2,
            segment_ranges=segment_ranges,
            source_fps=10.0,
        )
        self.assertIsNotNone(accepted)
        self.assertEqual("segment_0002", accepted["observability_segment_id"])
        self.assertAlmostEqual(0.1, accepted["max_midpoint_delta_sec"])

    def test_invalid_model_spans_are_rejected(self) -> None:
        valid, errors = run_marlin2_proposals.normalize_model_span(
            [0.2, 0.8],
            clip_duration_sec=1.0,
        )
        self.assertEqual([0.2, 0.8], valid)
        self.assertEqual([], errors)

        cases = (
            ([0.8, 0.2], "span_end_before_start"),
            ([-0.1, 0.2], "span_start_before_clip"),
            ([0.2, 1.1], "span_end_after_clip"),
            ([0.2, math.nan], "span_endpoints_must_be_finite_numbers"),
            ([0.2], "span_must_have_two_numeric_endpoints"),
        )
        for span, expected_error in cases:
            with self.subTest(span=span):
                _, errors = run_marlin2_proposals.normalize_model_span(
                    span,
                    clip_duration_sec=1.0,
                )
                self.assertIn(expected_error, errors)


class MarlinMainIntegrationTest(unittest.TestCase):
    @staticmethod
    def run_main(
        root: Path,
        *,
        anchor_time_sec: float,
        model_span: list[float] | None = None,
    ) -> tuple[list[dict], dict]:
        video = root / "source.avi"
        timeline = root / "timeline.json"
        anchors = root / "anchors.json"
        output = root / "proposals.jsonl"
        report = root / "report.json"
        video.write_bytes(b"fixture-video")
        timeline.write_text(
            json.dumps(fixture_timeline()),
            encoding="utf-8",
        )
        anchors.write_text(
            json.dumps(
                {
                    "case_id": "0704_6",
                    "anchors": [
                        {
                            "anchor_id": "A001",
                            "time_sec": anchor_time_sec,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        class FakeModel:
            def find(self, *_args: object, **_kwargs: object) -> dict:
                return {
                    "format_ok": True,
                    "span": model_span,
                    "raw": "fixture-model-response",
                }

        class FakeAutoModel:
            @staticmethod
            def from_pretrained(*_args: object, **_kwargs: object) -> FakeModel:
                return FakeModel()

        fake_torch = types.ModuleType("torch")
        fake_torch.bfloat16 = "bfloat16"
        fake_torch.__version__ = "fixture"
        fake_torch.version = types.SimpleNamespace(cuda=None)
        fake_torch.cuda = types.SimpleNamespace(
            is_available=lambda: False,
            get_device_name=lambda _index: None,
        )
        fake_transformers = types.ModuleType("transformers")
        fake_transformers.AutoModelForCausalLM = FakeAutoModel
        argv = [
            "run_marlin2_proposals.py",
            "--case-id",
            "0704_6",
            "--video",
            str(video),
            "--timeline",
            str(timeline),
            "--anchors",
            str(anchors),
            "--model",
            str(root / "model"),
            "--model-revision",
            "fixture-revision",
            "--output",
            str(output),
            "--report",
            str(report),
            "--event-types",
            "implicit_tool_request",
            "--skip-caption",
            "--device",
            "cpu",
        ]
        with (
            mock.patch.dict(
                sys.modules,
                {
                    "torch": fake_torch,
                    "transformers": fake_transformers,
                },
            ),
            mock.patch.object(sys, "argv", argv),
        ):
            return_code = run_marlin2_proposals.main()

        if return_code != 0:
            raise AssertionError(f"unexpected return code: {return_code}")
        records = [
            json.loads(line)
            for line in output.read_text(encoding="utf-8").splitlines()
        ]
        report_record = json.loads(report.read_text(encoding="utf-8"))
        self_policy = run_marlin2_proposals.MODEL_QUERY_POLICY_ID
        if report_record["settings"]["query_policy_id"] != self_policy:
            raise AssertionError("proposal report lost the query policy id")
        expected_prompt_hash = run_marlin2_proposals.canonical_json_sha256(
            {
                "implicit_tool_request": run_marlin2_proposals.MODEL_QUERIES[
                    "implicit_tool_request"
                ]
            }
        )
        if (
            report_record["settings"]["query_prompt_sha256"]
            != expected_prompt_hash
        ):
            raise AssertionError("proposal report lost the exact prompt hash")
        return records, report_record

    def test_main_explicitly_skips_anchor_inside_gap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            records, report = self.run_main(
                Path(temporary),
                anchor_time_sec=2.0,
            )

        self.assertEqual(1, len(records))
        self.assertEqual(
            "skipped_anchor_inside_observability_gap",
            records[0]["processing_status"],
        )
        self.assertFalse(records[0]["anchor_mapping"]["observable"])
        self.assertIsNone(records[0]["clip"])
        self.assertEqual([], records[0]["find_results"])
        self.assertEqual(0, report["counts"]["completed_anchor_count"])
        self.assertEqual(
            1,
            report["counts"]["skipped_anchor_inside_gap_count"],
        )

    def test_main_keeps_invalid_span_as_evidence_not_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(
                run_marlin2_proposals,
                "make_clip",
                return_value=0.5,
            ):
                records, report = self.run_main(
                    Path(temporary),
                    anchor_time_sec=0.1,
                    model_span=[0.0, 0.6],
                )

        self.assertEqual([], records[0]["consensus_candidates"])
        self.assertEqual(2, len(records[0]["find_results"]))
        for result in records[0]["find_results"]:
            self.assertFalse(result["format_ok"])
            self.assertIn(
                "span_end_after_clip",
                result["validation_errors"],
            )
            self.assertIsNone(result["mapped_span"])
        self.assertEqual(2, report["counts"]["invalid_query_span_count"])
        self.assertEqual(0, report["counts"]["consensus_candidate_count"])


class InteractionPointValidationSafetyTest(unittest.TestCase):
    def validate(
        self,
        records: list[dict],
        *,
        timeline: dict | None = None,
    ) -> list[str]:
        return validate_interaction_points.validate(
            records=records,
            schema=INTERACTION_SCHEMA,
            timeline=timeline or fixture_timeline(),
            tool_ids={"bovie", "adson_forceps"},
            case_id="0704_6",
            stream_kind="interaction",
        )

    def test_nan_is_rejected_by_strict_json_loader(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "invalid.jsonl"
            path.write_text('{"time_sec": NaN}\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "non-standard JSON"):
                validate_interaction_points.load_jsonl(path)

    def test_schema_invalid_time_type_does_not_crash(self) -> None:
        errors = self.validate([fixture_request(time_sec="bad")])
        self.assertTrue(any("schema time_sec" in error for error in errors), errors)

    def test_schema_invalid_hash_fields_do_not_crash(self) -> None:
        bad_event_type = self.validate(
            [fixture_request(event_type=["implicit_tool_request"])]
        )
        self.assertTrue(
            any("is not allowed" in error for error in bad_event_type),
            bad_event_type,
        )
        bad_tool = self.validate(
            [
                fixture_request(
                    event_id="0704_6-T0001",
                    event_type="tool_transfer",
                    tool=["bovie"],
                    **{"from": "scrub_nurse", "to": "surgeon"},
                )
            ]
        )
        self.assertTrue(
            any("unknown canonical tool" in error for error in bad_tool),
            bad_tool,
        )
        self.assertEqual(
            {"<invalid-or-missing>": 1},
            validate_interaction_points.count_string_field(
                [{"event_type": ["tool_transfer"]}],
                "event_type",
            ),
        )

    def test_cli_reports_invalid_array_event_type_without_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_path = root / "invalid.jsonl"
            timeline_path = root / "timeline.json"
            record = fixture_request(event_type=["implicit_tool_request"])
            record.pop("_line")
            input_path.write_text(
                json.dumps(record) + "\n",
                encoding="utf-8",
            )
            timeline_path.write_text(
                json.dumps(fixture_timeline()),
                encoding="utf-8",
            )
            argv = [
                "validate_interaction_points.py",
                str(input_path),
                "--schema",
                str(
                    ROOT
                    / "annotations/observable_tool_events/schema/"
                    "observable_interaction_point.v1.schema.json"
                ),
                "--timeline",
                str(timeline_path),
                "--tools",
                str(
                    ROOT
                    / "annotations/observable_tool_events/catalogs/tools.yaml"
                ),
                "--case-id",
                "0704_6",
                "--stream-kind",
                "interaction",
            ]
            output = io.StringIO()
            with (
                mock.patch.object(sys, "argv", argv),
                contextlib.redirect_stdout(output),
            ):
                return_code = validate_interaction_points.main()

        self.assertEqual(1, return_code)
        summary = json.loads(output.getvalue())
        self.assertFalse(summary["ok"])
        self.assertEqual(
            {"<invalid-or-missing>": 1},
            summary["event_type_counts"],
        )

    def test_timeline_identity_shape_order_and_finiteness_are_checked(self) -> None:
        wrong_case = fixture_timeline("0704_99")
        errors = self.validate([fixture_request()], timeline=wrong_case)
        self.assertTrue(any("timeline case_id" in error for error in errors), errors)

        wrong_count = fixture_timeline()
        wrong_count["frame_count"] = 99
        errors = self.validate([fixture_request()], timeline=wrong_count)
        self.assertTrue(any("frame_count" in error for error in errors), errors)

        non_finite = fixture_timeline()
        non_finite["timestamps_sec"][1] = math.inf
        errors = self.validate([fixture_request()], timeline=non_finite)
        self.assertTrue(any("finite numbers" in error for error in errors), errors)

        unsorted = fixture_timeline()
        unsorted["timestamps_sec"] = [0.0, 0.2, 0.1, 5.3, 5.4]
        errors = self.validate([fixture_request()], timeline=unsorted)
        self.assertTrue(any("strictly increasing" in error for error in errors), errors)

        missing_gap = fixture_timeline()
        missing_gap["gaps"] = []
        errors = self.validate([fixture_request()], timeline=missing_gap)
        self.assertTrue(
            any("timestamp discontinuities" in error for error in errors),
            errors,
        )

    def test_event_id_is_bound_to_case_and_event_type(self) -> None:
        wrong_case = self.validate(
            [fixture_request(event_id="0704_99-R0001")]
        )
        self.assertTrue(
            any("does not match case/event type" in error for error in wrong_case),
            wrong_case,
        )
        wrong_type = self.validate([fixture_request(event_id="0704_6-T0001")])
        self.assertTrue(
            any("does not match case/event type" in error for error in wrong_type),
            wrong_type,
        )

    def test_current_ai_review_stream_remains_compatible(self) -> None:
        case_dir = (
            ROOT / "annotations/observable_tool_events/cases/0704_6"
        )
        records = validate_interaction_points.load_jsonl(
            case_dir / "interaction_candidates.ai_review.v1.jsonl"
        )
        timeline = validate_interaction_points.load_json_object(
            case_dir / "cam4_frame_timeline.v1.json"
        )
        catalog = yaml.safe_load(
            (
                ROOT / "annotations/observable_tool_events/catalogs/tools.yaml"
            ).read_text(encoding="utf-8")
        )
        errors = validate_interaction_points.validate(
            records=records,
            schema=INTERACTION_SCHEMA,
            timeline=timeline,
            tool_ids={str(tool["id"]) for tool in catalog["tools"]},
            case_id="0704_6",
            stream_kind="interaction",
        )
        self.assertEqual([], errors)


if __name__ == "__main__":
    unittest.main()
