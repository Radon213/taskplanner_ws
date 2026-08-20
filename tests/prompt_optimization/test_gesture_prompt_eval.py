from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.prompt_optimization.gesture_recognition import gesture_prompt_eval as gesture
from tools.prompt_optimization.gesture_recognition.prompts import (
    GESTURE_FULL_FRAME_V6,
    GESTURE_ONLY_V1,
    GESTURE_TOP_RIGHT_EMPTY_OPEN_HAND_V9,
    GESTURE_TOP_RIGHT_EMPTY_OPEN_HAND_V10,
    GESTURE_TOP_RIGHT_EMPTY_OPEN_HAND_V11,
    GESTURE_TOP_RIGHT_EMPTY_OPEN_HAND_V12,
    GESTURE_TOP_RIGHT_EMPTY_OPEN_HAND_V13,
    GESTURE_TOP_RIGHT_EMPTY_OPEN_HAND_V14,
    GESTURE_TOP_RIGHT_EMPTY_OPEN_HAND_V16,
    GESTURE_TOP_RIGHT_EMPTY_OPEN_HAND_V17,
    GESTURE_TOP_RIGHT_EMPTY_OPEN_HAND_V18,
    GESTURE_TOP_RIGHT_OPEN_HAND_V7,
    GESTURE_TOP_RIGHT_OPEN_HAND_V8,
)


class GesturePromptContractTest(unittest.TestCase):
    def test_request_payload_contains_no_ground_truth_or_case_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "frame.jpg"
            image.write_bytes(b"not-a-real-image-but-base64-safe")
            payload = gesture.build_request_payload(
                images=[("CAM4 full frame", image)],
                model_id="qwen3.6-35b-a3b",
                prompt_version="gesture-only-v1",
            )

        serialized = gesture.canonical_json(payload)
        self.assertNotIn("0704_6", serialized)
        self.assertNotIn("event_id", serialized)
        self.assertNotIn('"label"', serialized)
        self.assertNotIn("ground_truth", serialized)
        self.assertFalse(payload["enable_thinking"])
        self.assertEqual("none", payload["reasoning_effort"])
        self.assertIn("pixel-only hand-pose recognition", GESTURE_ONLY_V1)
        self.assertIn('"gesture"', GESTURE_ONLY_V1)

    def test_full_frame_v6_keeps_the_same_strict_machine_contract(self) -> None:
        self.assertIn("scan the entire CAM4 frame", GESTURE_FULL_FRAME_V6)
        self.assertIn("resting lightly on skin or a drape", GESTURE_FULL_FRAME_V6)
        self.assertIn('"gesture"', GESTURE_FULL_FRAME_V6)
        self.assertIn("visual_evidence", GESTURE_FULL_FRAME_V6)
        self.assertIn("gesture-full-frame-v6", gesture.PROMPTS)

    def test_top_right_v7_is_a_minimal_binary_target_hand_prompt(self) -> None:
        self.assertIn("upper-right", GESTURE_TOP_RIGHT_OPEN_HAND_V7)
        self.assertIn("Look only at that surgeon's gloved hand", GESTURE_TOP_RIGHT_OPEN_HAND_V7)
        self.assertIn('"gesture":"open_receive" | "not_open_receive"', GESTURE_TOP_RIGHT_OPEN_HAND_V7)
        self.assertNotIn("confidence", GESTURE_TOP_RIGHT_OPEN_HAND_V7)
        self.assertIn("gesture-top-right-open-hand-v7", gesture.PROMPTS)

    def test_top_right_v8_uses_a_boolean_not_a_semantic_gesture_name(self) -> None:
        self.assertIn('"open_hand":true | false', GESTURE_TOP_RIGHT_OPEN_HAND_V8)
        self.assertNotIn("open_receive", GESTURE_TOP_RIGHT_OPEN_HAND_V8)
        self.assertIn("gesture-top-right-open-hand-v8", gesture.PROMPTS)

    def test_top_right_v9_requires_an_empty_open_hand_for_request_trigger(self) -> None:
        self.assertIn("empty and open", GESTURE_TOP_RIGHT_EMPTY_OPEN_HAND_V9)
        self.assertIn("putting down any object", GESTURE_TOP_RIGHT_EMPTY_OPEN_HAND_V9)
        self.assertIn('"empty_open_hand":true | false', GESTURE_TOP_RIGHT_EMPTY_OPEN_HAND_V9)
        self.assertIn("gesture-top-right-empty-open-hand-v9", gesture.PROMPTS)

    def test_top_right_v10_requires_an_object_in_that_hand_for_the_occupied_negative(self) -> None:
        self.assertIn("same hand visibly holds", GESTURE_TOP_RIGHT_EMPTY_OPEN_HAND_V10)
        self.assertIn("nearby cable", GESTURE_TOP_RIGHT_EMPTY_OPEN_HAND_V10)
        self.assertIn("gesture-top-right-empty-open-hand-v10", gesture.PROMPTS)

    def test_top_right_v11_allows_a_strict_visual_uncertain_result(self) -> None:
        self.assertIn("material blur, occlusion, or", GESTURE_TOP_RIGHT_EMPTY_OPEN_HAND_V11)
        self.assertIn('"empty_open_hand":"yes" | "no" | "uncertain"', GESTURE_TOP_RIGHT_EMPTY_OPEN_HAND_V11)
        self.assertIn("gesture-top-right-empty-open-hand-v11", gesture.PROMPTS)

    def test_top_right_v12_preserves_boolean_contract_with_null_for_unidentifiable(self) -> None:
        self.assertIn("Do not use false merely because", GESTURE_TOP_RIGHT_EMPTY_OPEN_HAND_V12)
        self.assertIn('"empty_open_hand":true | false | null', GESTURE_TOP_RIGHT_EMPTY_OPEN_HAND_V12)
        self.assertIn("gesture-top-right-empty-open-hand-v12", gesture.PROMPTS)

    def test_top_right_v13_uses_an_exact_nullable_json_scalar(self) -> None:
        self.assertIn("Return exactly one JSON value", GESTURE_TOP_RIGHT_EMPTY_OPEN_HAND_V13)
        self.assertIn("true | false | null", GESTURE_TOP_RIGHT_EMPTY_OPEN_HAND_V13)
        self.assertIn("gesture-top-right-empty-open-hand-v13", gesture.PROMPTS)

    def test_top_right_v14_requires_a_complete_object_for_null(self) -> None:
        self.assertIn("never a bare null value", GESTURE_TOP_RIGHT_EMPTY_OPEN_HAND_V14)
        self.assertIn('`{"empty_open_hand":null}`', GESTURE_TOP_RIGHT_EMPTY_OPEN_HAND_V14)
        self.assertIn("gesture-top-right-empty-open-hand-v14", gesture.PROMPTS)

    def test_top_right_v15_preserves_v12_prompt_and_declares_a_distinct_decoder(self) -> None:
        self.assertEqual(
            gesture.get_prompt("gesture-top-right-empty-open-hand-v12"),
            gesture.get_prompt("gesture-top-right-empty-open-hand-v15"),
        )

    def test_targeted_v16_to_v18_keep_nullable_contract_and_distinct_visual_rules(self) -> None:
        self.assertIn("gently cupped", GESTURE_TOP_RIGHT_EMPTY_OPEN_HAND_V16)
        self.assertIn("tool-placement or release transition", GESTURE_TOP_RIGHT_EMPTY_OPEN_HAND_V17)
        self.assertIn("direct pixels", GESTURE_TOP_RIGHT_EMPTY_OPEN_HAND_V18)
        for version in (
            "gesture-top-right-empty-open-hand-v16-recovery",
            "gesture-top-right-empty-open-hand-v17-transition-guard",
            "gesture-top-right-empty-open-hand-v18-balanced-evidence",
        ):
            self.assertIn(version, gesture.PROMPTS)
            parsed = gesture.parse_prediction("null", prompt_version=version)
            self.assertEqual("uncertain", parsed.gesture)
            self.assertFalse(parsed.parse_error)

    def test_parser_requires_the_exact_compact_contract(self) -> None:
        parsed = gesture.parse_prediction(
            '```json\n{"gesture":"open_receive","confidence":0.82,'
            '"visual_evidence":"empty palm and extended fingers visible"}\n```'
        )
        self.assertEqual("open_receive", parsed.gesture)
        self.assertAlmostEqual(0.82, parsed.confidence)
        self.assertFalse(parsed.parse_error)

        malformed = gesture.parse_prediction(
            '{"gesture":"open palm","confidence":0.9}'
        )
        self.assertEqual("uncertain", malformed.gesture)
        self.assertTrue(malformed.parse_error)

    def test_minimal_boolean_parser_maps_only_a_json_boolean(self) -> None:
        parsed = gesture.parse_prediction(
            '{"open_hand":true}',
            prompt_version="gesture-top-right-open-hand-v8",
        )
        self.assertEqual("open_receive", parsed.gesture)
        self.assertFalse(parsed.parse_error)
        malformed = gesture.parse_prediction(
            '{"open_hand":"true"}',
            prompt_version="gesture-top-right-open-hand-v8",
        )
        self.assertEqual("uncertain", malformed.gesture)
        self.assertTrue(malformed.parse_error)

    def test_minimal_empty_open_hand_parser_maps_only_the_v9_boolean(self) -> None:
        parsed = gesture.parse_prediction(
            '{"empty_open_hand":false}',
            prompt_version="gesture-top-right-empty-open-hand-v9",
        )
        self.assertEqual("not_open_receive", parsed.gesture)
        self.assertFalse(parsed.parse_error)
        malformed = gesture.parse_prediction(
            '{"open_hand":false}',
            prompt_version="gesture-top-right-empty-open-hand-v9",
        )
        self.assertEqual("uncertain", malformed.gesture)
        self.assertTrue(malformed.parse_error)

    def test_minimal_empty_open_hand_uncertain_parser_accepts_only_the_v11_enum(self) -> None:
        parsed = gesture.parse_prediction(
            '{"empty_open_hand":"uncertain"}',
            prompt_version="gesture-top-right-empty-open-hand-v11",
        )
        self.assertEqual("uncertain", parsed.gesture)
        self.assertFalse(parsed.parse_error)
        malformed = gesture.parse_prediction(
            '{"empty_open_hand":"maybe"}',
            prompt_version="gesture-top-right-empty-open-hand-v11",
        )
        self.assertEqual("uncertain", malformed.gesture)
        self.assertTrue(malformed.parse_error)

    def test_minimal_empty_open_hand_nullable_parser_maps_null_to_uncertain(self) -> None:
        parsed = gesture.parse_prediction(
            '{"empty_open_hand":null}',
            prompt_version="gesture-top-right-empty-open-hand-v12",
        )
        self.assertEqual("uncertain", parsed.gesture)
        self.assertFalse(parsed.parse_error)
        parsed_true = gesture.parse_prediction(
            '{"empty_open_hand":true}',
            prompt_version="gesture-top-right-empty-open-hand-v12",
        )
        self.assertEqual("open_receive", parsed_true.gesture)
        malformed = gesture.parse_prediction(
            '{"empty_open_hand":"null"}',
            prompt_version="gesture-top-right-empty-open-hand-v12",
        )
        self.assertEqual("uncertain", malformed.gesture)
        self.assertTrue(malformed.parse_error)

    def test_minimal_empty_open_hand_scalar_nullable_parser_maps_null_to_uncertain(self) -> None:
        parsed = gesture.parse_prediction(
            "null",
            prompt_version="gesture-top-right-empty-open-hand-v13",
        )
        self.assertEqual("uncertain", parsed.gesture)
        self.assertFalse(parsed.parse_error)
        parsed_true = gesture.parse_prediction(
            "true",
            prompt_version="gesture-top-right-empty-open-hand-v13",
        )
        self.assertEqual("open_receive", parsed_true.gesture)
        malformed = gesture.parse_prediction(
            '{"empty_open_hand":null}',
            prompt_version="gesture-top-right-empty-open-hand-v13",
        )
        self.assertEqual("uncertain", malformed.gesture)
        self.assertTrue(malformed.parse_error)

    def test_v15_accepts_only_bare_null_as_the_safe_scalar_fallback(self) -> None:
        parsed = gesture.parse_prediction(
            "null",
            prompt_version="gesture-top-right-empty-open-hand-v15",
        )
        self.assertEqual("uncertain", parsed.gesture)
        self.assertFalse(parsed.parse_error)
        parsed_object = gesture.parse_prediction(
            '{"empty_open_hand":true}',
            prompt_version="gesture-top-right-empty-open-hand-v15",
        )
        self.assertEqual("open_receive", parsed_object.gesture)
        malformed = gesture.parse_prediction(
            "true",
            prompt_version="gesture-top-right-empty-open-hand-v15",
        )
        self.assertEqual("uncertain", malformed.gesture)
        self.assertTrue(malformed.parse_error)

    def test_minimal_binary_parser_accepts_only_the_binary_v7_contract(self) -> None:
        parsed = gesture.parse_prediction(
            '{"gesture":"open_receive"}',
            prompt_version="gesture-top-right-open-hand-v7",
        )
        self.assertEqual("open_receive", parsed.gesture)
        self.assertEqual(1.0, parsed.confidence)
        self.assertFalse(parsed.parse_error)

        malformed = gesture.parse_prediction(
            '{"gesture":"open_receive","confidence":0.9}',
            prompt_version="gesture-top-right-open-hand-v7",
        )
        self.assertEqual("uncertain", malformed.gesture)
        self.assertTrue(malformed.parse_error)


class GestureManifestTest(unittest.TestCase):
    def _write_json(self, path: Path, value: object) -> None:
        path.write_text(json.dumps(value), encoding="utf-8")

    def _write_jsonl(self, path: Path, values: list[object]) -> None:
        path.write_text(
            "\n".join(json.dumps(value) for value in values) + "\n",
            encoding="utf-8",
        )

    def test_manifest_is_time_split_and_boundary_negatives_never_overlap_targets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            events = root / "events.jsonl"
            masks = root / "masks.json"
            timeline = root / "timeline.json"
            self._write_jsonl(
                events,
                [
                    {
                        "case_id": "0704_6",
                        "event_id": "R1",
                        "event_type": "implicit_tool_request",
                        "review_status": "confirmed",
                        "start_source_frame_idx": 20,
                        "end_source_frame_idx": 30,
                        "start_sec": 20.0,
                    },
                    {
                        "case_id": "0704_6",
                        "event_id": "R2",
                        "event_type": "implicit_tool_request",
                        "review_status": "confirmed",
                        "start_source_frame_idx": 120,
                        "end_source_frame_idx": 130,
                        "start_sec": 120.0,
                    },
                ],
            )
            self._write_json(
                masks,
                {
                    "event_roles": [
                        {
                            "event_id": "R1",
                            "role": "gesture_target",
                            "metric_eligibility": {
                                "gesture_presence": True,
                                "gesture_onset": True,
                            },
                        },
                        {
                            "event_id": "R2",
                            "role": "gesture_target",
                            "metric_eligibility": {
                                "gesture_presence": True,
                                "gesture_onset": False,
                            },
                        },
                    ]
                },
            )
            self._write_json(
                timeline,
                {"timestamps_sec": [float(index) for index in range(200)]},
            )

            samples = gesture.build_manifest(
                events_path=events,
                masks_path=masks,
                timeline_path=timeline,
                case_id="0704_6",
                calibration_end_sec=90.0,
                negative_margin_frames=5,
            )

        self.assertEqual(8, len(samples))
        self.assertEqual(
            {"calibration", "within_case_challenge"},
            {sample["split"] for sample in samples},
        )
        self.assertEqual(1, sum(sample["sample_kind"] == "positive_onset" for sample in samples))
        self.assertEqual(
            1,
            sum(
                sample["sample_kind"] == "positive_left_censored_presence"
                for sample in samples
            ),
        )
        positive_ranges = ((20, 30), (120, 130))
        for sample in samples:
            if sample["label"] == "not_open_receive":
                self.assertFalse(
                    any(start <= sample["frame_idx"] <= end for start, end in positive_ranges)
                )
            self.assertEqual("evaluation_only", sample["ground_truth_usage"])
            self.assertFalse(sample["may_publish_runtime"])

    def test_adjacent_targets_shift_only_the_overlapping_boundary_negative(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            events = root / "events.jsonl"
            masks = root / "masks.json"
            timeline = root / "timeline.json"
            self._write_jsonl(
                events,
                [
                    {
                        "case_id": "0704_16",
                        "event_id": "R1",
                        "event_type": "implicit_tool_request",
                        "review_status": "confirmed",
                        "start_source_frame_idx": 20,
                        "end_source_frame_idx": 30,
                        "start_sec": 20.0,
                    },
                    {
                        "case_id": "0704_16",
                        "event_id": "R2",
                        "event_type": "implicit_tool_request",
                        "review_status": "confirmed",
                        "start_source_frame_idx": 35,
                        "end_source_frame_idx": 45,
                        "start_sec": 35.0,
                    },
                ],
            )
            self._write_json(
                masks,
                {
                    "event_roles": [
                        {
                            "event_id": event_id,
                            "role": "gesture_target",
                            "metric_eligibility": {
                                "gesture_presence": True,
                                "gesture_onset": True,
                            },
                        }
                        for event_id in ("R1", "R2")
                    ]
                },
            )
            self._write_json(
                timeline,
                {"timestamps_sec": [float(index) for index in range(100)]},
            )
            samples = gesture.build_manifest(
                events_path=events,
                masks_path=masks,
                timeline_path=timeline,
                case_id="0704_16",
                calibration_end_sec=100.0,
                negative_margin_frames=10,
            )

        positive_ranges = ((20, 30), (35, 45))
        negatives = [sample for sample in samples if sample["label"] == "not_open_receive"]
        self.assertTrue(negatives)
        for sample in negatives:
            self.assertFalse(
                any(start <= sample["frame_idx"] <= end for start, end in positive_ranges)
            )
        self.assertTrue(any("boundary_shift_frames" in sample for sample in negatives))

    def test_confirmed_positive_filter_preserves_only_existing_positive_rows(self) -> None:
        samples = [
            {
                "schema": gesture.SAMPLE_SCHEMA,
                "sample_id": "positive",
                "label": "open_receive",
            },
            {
                "schema": gesture.SAMPLE_SCHEMA,
                "sample_id": "boundary-control",
                "label": "not_open_receive",
            },
        ]
        filtered = gesture.filter_confirmed_positive_samples(samples)
        self.assertEqual(["positive"], [sample["sample_id"] for sample in filtered])


class GestureScoringTest(unittest.TestCase):
    def _record(self, label: str, predicted: str, confidence: float) -> dict[str, object]:
        return {
            "sample": {
                "label": label,
                "sample_kind": "positive_onset" if label == "open_receive" else "negative_pre_open_boundary",
            },
            "prediction": {
                "gesture": predicted,
                "confidence": confidence,
                "parse_error": "",
            },
            "transport_error": "",
        }

    def test_threshold_is_selected_using_calibration_only(self) -> None:
        records = [
            self._record("open_receive", "open_receive", 0.70),
            self._record("open_receive", "open_receive", 0.90),
            self._record("not_open_receive", "open_receive", 0.65),
            self._record("not_open_receive", "not_open_receive", 0.99),
        ]
        threshold = gesture.select_threshold(records)
        self.assertEqual(0.70, threshold)
        metrics = gesture.score_records(records, threshold=threshold)
        self.assertEqual({"tp": 2, "fp": 0, "tn": 2, "fn": 0}, metrics["confusion_matrix"])
        self.assertEqual(1.0, metrics["onset_recall"])

    def test_report_marks_assistant_source_as_event_alignment_only(self) -> None:
        records = [
            {
                "sample": {
                    "case_id": "0704_6",
                    "split": "calibration",
                    "evaluation_group": "development_calibration",
                    "label": "open_receive",
                    "sample_kind": "positive_onset",
                },
                "prompt_version": "gesture-full-frame-v6",
                "model_id": "qwen3.6-35b-a3b",
                "input_policy": "CAM4 only",
                "prediction": {
                    "gesture": "open_receive",
                    "confidence": 0.95,
                    "parse_error": "",
                },
                "transport_error": "",
            },
            {
                "sample": {
                    "case_id": "0704_6",
                    "split": "calibration",
                    "evaluation_group": "development_calibration",
                    "label": "not_open_receive",
                    "sample_kind": "negative_pre_open_boundary",
                },
                "prompt_version": "gesture-full-frame-v6",
                "model_id": "qwen3.6-35b-a3b",
                "input_policy": "CAM4 only",
                "prediction": {
                    "gesture": "not_open_receive",
                    "confidence": 0.95,
                    "parse_error": "",
                },
                "transport_error": "",
            },
        ]
        with tempfile.TemporaryDirectory() as temporary:
            predictions = Path(temporary) / "predictions.jsonl"
            predictions.write_text(
                "\n".join(json.dumps(record) for record in records) + "\n",
                encoding="utf-8",
            )
            report = gesture.score_prediction_files([predictions])
        self.assertEqual(
            "mixed_human_and_authorized_assistant_video_adjudication",
            report["reference_authority"],
        )
        self.assertIn("read-only historical human", report["metric_interpretation"])

    def test_execution_gate_refuses_a_halted_batch_run(self) -> None:
        records = [
            {
                "sample": {
                    "case_id": "0704_6",
                    "split": "calibration",
                    "evaluation_group": "development_calibration",
                    "label": "open_receive",
                    "sample_kind": "positive_onset",
                },
                "prompt_version": "gesture-full-frame-v6",
                "model_id": "qwen3.6-35b-a3b",
                "input_policy": "CAM4 only",
                "prediction": {
                    "gesture": "open_receive",
                    "confidence": 0.95,
                    "parse_error": "",
                },
                "transport_error": "",
            }
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            predictions = root / "predictions.jsonl"
            execution = root / "execution.json"
            predictions.write_text(json.dumps(records[0]) + "\n", encoding="utf-8")
            execution.write_text(
                json.dumps(
                    {
                        "schema": "taskplanner.gesture_prompt_eval_execution.v1",
                        "status": "halted",
                        "scoreable": False,
                        "transport_failure_count": 1,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "non-scoreable"):
                gesture.score_prediction_files([predictions], execution_paths=[execution])

    def test_positive_only_score_does_not_invent_negative_metrics(self) -> None:
        metrics = gesture.score_confirmed_positive_records(
            [
                self._record("open_receive", "open_receive", 1.0),
                self._record("open_receive", "not_open_receive", 1.0),
            ]
        )
        self.assertEqual(0.5, metrics["positive_recall"])
        self.assertNotIn("accuracy", metrics)
        self.assertNotIn("specificity", metrics)


if __name__ == "__main__":
    unittest.main()
