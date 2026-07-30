from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.vlm_finetuning.eval_vlm_sft import (
    evaluate_predictions,
    load_jsonl,
    main,
)


def prediction_row(
    example_id: str,
    task_type: str,
    target: dict[str, object],
    prediction: object,
    **extra: object,
) -> dict[str, object]:
    return {
        "example_id": example_id,
        "task_type": task_type,
        "target": target,
        "prediction": prediction,
        **extra,
    }


class EvalVlmSftTest(unittest.TestCase):
    def test_tool_macro_f1_and_exhaustive_hallucination(self) -> None:
        rows = [
            prediction_row(
                "tool-1",
                "tool_presence_at_transfer",
                {
                    "tool": "scalpel",
                    "exhaustive_visible_tool_inventory": True,
                },
                {"tool": "scalpel"},
            ),
            prediction_row(
                "tool-2",
                "tool_presence_at_transfer",
                {
                    "tool": "bovie",
                    "exhaustive_visible_tool_inventory": True,
                },
                {"tool": "mosquito"},
            ),
            prediction_row(
                "tool-3",
                "tool_presence_at_transfer",
                {
                    "tool": "adson_forceps",
                    "exhaustive_visible_tool_inventory": False,
                },
                {"tools": ["adson_forceps", "bipolar"]},
            ),
        ]

        report = evaluate_predictions(rows)
        metrics = report["metrics"]["tool"]

        self.assertTrue(report["ok"])
        self.assertAlmostEqual(
            1.0 / 3.0,
            metrics["primary_classification"]["macro_f1"],
        )
        self.assertEqual(2, metrics["hallucination"]["scorable_examples"])
        self.assertAlmostEqual(
            0.5, metrics["hallucination"]["label_rate"]
        )
        self.assertEqual(
            1, metrics["hallucination"]["unscored_non_exhaustive_examples"]
        )
        self.assertEqual(0.5, metrics["hallucination"]["example_rate"])

    def test_tool_primary_handles_singleton_and_empty_lists(self) -> None:
        rows = [
            prediction_row(
                "tool-list-singleton",
                "tool_presence_at_transfer",
                {
                    "event": "physical_tool_transfer",
                    "tool": "scalpel",
                    "from": "scrub_nurse",
                    "to": "surgeon",
                    "exhaustive_visible_tool_inventory": False,
                },
                {
                    "event": "physical_tool_transfer",
                    "tool": ["scalpel"],
                    "from": "scrub_nurse",
                    "to": "surgeon",
                    "exhaustive_visible_tool_inventory": False,
                },
            ),
            prediction_row(
                "tool-list-empty",
                "tool_presence_at_transfer",
                {
                    "event": "physical_tool_transfer",
                    "tool": "bovie",
                    "from": "scrub_nurse",
                    "to": "surgeon",
                    "exhaustive_visible_tool_inventory": False,
                },
                {
                    "event": "physical_tool_transfer",
                    "tool": [],
                    "from": "scrub_nurse",
                    "to": "surgeon",
                    "exhaustive_visible_tool_inventory": False,
                },
            ),
        ]

        report = evaluate_predictions(rows)
        classification = report["metrics"]["tool"]["primary_classification"]

        self.assertEqual(0.5, classification["accuracy"])
        self.assertEqual(0, classification["out_of_reference_class_predictions"])
        self.assertEqual(
            0,
            report["task_schema_compliance"]["per_task"]["tool"]["valid_count"],
        )

    def test_phase_macro_f1_and_transition_detection(self) -> None:
        rows = [
            prediction_row(
                "phase-1",
                "current_phase",
                {"phase_id": "P03", "state": "interior"},
                {"phase_id": "P03", "state": "interior"},
            ),
            prediction_row(
                "phase-2",
                "current_phase",
                {"phase_id": "P04", "state": "transition"},
                {"phase_id": "P03", "state": "interior"},
            ),
            prediction_row(
                "phase-3",
                "current_phase",
                {"phase_id": "P04", "state": "transition"},
                {"phase_id": "P04", "state": "transition"},
            ),
        ]

        report = evaluate_predictions(rows)
        metrics = report["metrics"]["phase"]

        self.assertAlmostEqual(
            2.0 / 3.0,
            metrics["phase_classification"]["macro_f1"],
        )
        self.assertAlmostEqual(
            2.0 / 3.0,
            metrics["transition_detection"]["f1"],
        )
        self.assertEqual(
            2, metrics["transition_detection"]["positive_support"]
        )

    def test_next_tool_top1_none_and_strata(self) -> None:
        rows = [
            prediction_row(
                "next-explicit",
                "next_physical_tool",
                {
                    "next_transfer_tool": "bovie",
                    "event": "scrub_nurse_to_surgeon",
                    "basis": "explicit_request",
                },
                {"next_transfer_tool": "bovie"},
            ),
            prediction_row(
                "next-implicit",
                "next_physical_tool",
                {
                    "next_transfer_tool": "mosquito",
                    "event": "scrub_nurse_to_surgeon",
                    "basis": "silent_request",
                },
                {"next_transfer_tool": "adson_forceps"},
            ),
            prediction_row(
                "next-anticipatory",
                "next_physical_tool",
                {
                    "next_transfer_tool": "bipolar",
                    "event": "scrub_nurse_to_surgeon",
                    "basis": "anticipatory_context",
                },
                {"next_transfer_tool": "bipolar"},
            ),
            prediction_row(
                "next-none",
                "next_physical_tool",
                {
                    "next_transfer_tool": "none",
                    "event": "none",
                    "basis": "none",
                },
                {"next_transfer_tool": "none", "event": "none"},
            ),
        ]

        report = evaluate_predictions(rows)
        metrics = report["metrics"]["next_tool"]

        self.assertEqual(0.75, metrics["top1_accuracy"])
        self.assertAlmostEqual(2.0 / 3.0, metrics["positive_top1_accuracy"])
        self.assertEqual(
            1.0, metrics["none_detection"]["gold_none_top1_accuracy"]
        )
        self.assertEqual(
            1.0, metrics["per_stratum"]["anticipatory"]["top1_accuracy"]
        )
        self.assertEqual(
            0.0, metrics["per_stratum"]["implicit"]["top1_accuracy"]
        )

    def test_clinical_slots_and_structured_entity_scaffold(self) -> None:
        rows = [
            prediction_row(
                "clinical-1",
                "clinical_observation_interpretation",
                {
                    "observation": "바이폴라가 중앙 조직면에 접촉한다.",
                    "interpretation": "미세 혈관점을 응고하는 단계다.",
                    "entities": {
                        "tool": ["bipolar"],
                        "action": ["coagulation"],
                    },
                },
                {
                    "observation": "바이폴라가 중앙 조직면에 접촉한다.",
                    "interpretation": "미세 혈관점을 응고하는 단계다.",
                    "entities": {
                        "tool": ["bipolar"],
                        "action": ["coagulation"],
                    },
                },
            ),
            prediction_row(
                "clinical-2",
                "clinical_observation_interpretation",
                {
                    "observation": "Adson이 조직 장력을 유지한다.",
                    "interpretation": "박리면을 노출하는 단계다.",
                    "entities": {
                        "tool": ["adson_forceps"],
                        "action": ["retraction"],
                    },
                },
                {
                    "observation": "Adson이 장력을 유지한다.",
                    "interpretation": "",
                    "entities": {
                        "tool": ["adson_forceps"],
                        "action": ["dissection"],
                    },
                },
            ),
        ]

        report = evaluate_predictions(rows)
        metrics = report["metrics"]["clinical"]

        self.assertEqual(0.5, metrics["joint_normalized_exact_match"])
        self.assertEqual(0.5, metrics["joint_slot_presence_rate"])
        self.assertEqual(2, metrics["entity_scaffold"]["annotated_support"])
        self.assertAlmostEqual(
            0.75, metrics["entity_scaffold"]["mean_set_f1"]
        )

    def test_invalid_prediction_json_counts_as_json_failure(self) -> None:
        rows = [
            prediction_row(
                "valid",
                "request_intent",
                {"intent": "receive_unspecified_tool"},
                '{"intent":"receive_unspecified_tool"}',
            ),
            prediction_row(
                "invalid",
                "request_intent",
                {"intent": "receive_unspecified_tool"},
                "```json\n{\"intent\":\"receive_unspecified_tool\"}\n```",
            ),
        ]

        report = evaluate_predictions(rows)

        self.assertTrue(report["ok"])
        self.assertEqual(0.5, report["json_validity"]["valid_rate"])
        self.assertEqual(1, report["metrics"]["intent"]["json_valid_support"])
        self.assertEqual(2, report["metrics"]["intent"]["total_support"])
        self.assertEqual(0.5, report["metrics"]["intent"]["exact_match"])
        self.assertEqual(
            0.5, report["metrics"]["intent"]["intent_label_accuracy"]
        )

    def test_invalid_raw_json_empties_preparsed_semantic_prediction(self) -> None:
        target = {"intent": "receive_unspecified_tool"}
        report = evaluate_predictions(
            [
                prediction_row(
                    "fenced-preparsed",
                    "request_intent",
                    target,
                    target,
                    prediction_text=(
                        "```json\n"
                        '{"intent":"receive_unspecified_tool"}'
                        "\n```"
                    ),
                )
            ]
        )

        self.assertEqual(0.0, report["metrics"]["intent"]["exact_match"])
        self.assertEqual(
            0.0, report["metrics"]["intent"]["intent_label_accuracy"]
        )

    def test_intent_label_accuracy_and_task_schema_compliance(self) -> None:
        target = {
            "event": "implicit_tool_request",
            "intent": "receive_unspecified_tool",
            "requested_tool": None,
            "tool_identity_inferred_from_later_transfer": False,
        }
        rows = [
            prediction_row(
                "intent-valid",
                "request_intent",
                target,
                {
                    **target,
                    "event": "hand_gesture",
                },
            ),
            prediction_row(
                "intent-invalid-type",
                "request_intent",
                target,
                {
                    **target,
                    "event": "hand_gesture",
                    "tool_identity_inferred_from_later_transfer": "false",
                },
            ),
        ]

        report = evaluate_predictions(rows)
        metrics = report["metrics"]["intent"]
        compliance = report["task_schema_compliance"]

        self.assertEqual(0.0, metrics["exact_match"])
        self.assertEqual(1.0, metrics["intent_label_accuracy"])
        self.assertEqual(1, metrics["schema_valid_support"])
        self.assertEqual(0.5, metrics["schema_valid_rate"])
        self.assertEqual(1, compliance["valid_count"])
        self.assertEqual(0.5, compliance["valid_rate"])

    def test_task_schema_compliance_covers_all_task_contracts(self) -> None:
        examples = [
            (
                "tool",
                "tool_presence_at_transfer",
                {
                    "event": "physical_tool_transfer",
                    "tool": "bovie",
                    "from": "scrub_nurse",
                    "to": "surgeon",
                    "exhaustive_visible_tool_inventory": False,
                },
            ),
            (
                "intent",
                "request_intent",
                {
                    "event": "implicit_tool_request",
                    "intent": "receive_unspecified_tool",
                    "requested_tool": None,
                    "tool_identity_inferred_from_later_transfer": False,
                },
            ),
            (
                "phase",
                "current_phase",
                {
                    "phase_id": "P04",
                    "phase_name_ko": "고정 견인 배치 및 노출 확립",
                    "state": "transition",
                    "transition_from": "P03",
                    "transition_to": "P04",
                },
            ),
            (
                "next-tool",
                "next_physical_tool",
                {
                    "next_transfer_tool": "none",
                    "event": "none",
                    "basis": "no_physical_transfer_within_horizon",
                },
            ),
            (
                "clinical",
                "clinical_observation_interpretation",
                {
                    "observation": "바이폴라가 조직면에 접촉한다.",
                    "interpretation": "국소 지혈 단계다.",
                    "confidence": {
                        "observation": "high",
                        "interpretation": "medium",
                    },
                },
            ),
        ]
        rows = [
            prediction_row(example_id, task_type, target, target)
            for example_id, task_type, target in examples
        ]

        report = evaluate_predictions(rows)

        self.assertEqual(5, report["task_schema_compliance"]["valid_count"])
        self.assertEqual(1.0, report["task_schema_compliance"]["valid_rate"])
        self.assertTrue(
            all(
                task_metrics["schema_valid_support"] == 1
                for task_metrics in report["metrics"].values()
            )
        )

    def test_invalid_reference_is_input_error(self) -> None:
        report = evaluate_predictions(
            [
                prediction_row(
                    "bad-reference",
                    "current_phase",
                    {},
                    {"phase_id": "P03"},
                )
                | {"target": "not-json"}
            ]
        )

        self.assertFalse(report["ok"])
        self.assertEqual(1, report["summary"]["invalid_reference_count"])
        self.assertEqual("reference_invalid", report["input_errors"][0]["code"])

    def test_jsonl_loader_and_cli_write_report(self) -> None:
        row = prediction_row(
            "phase-cli",
            "current_phase",
            {"phase_id": "P03", "state": "interior"},
            {"phase_id": "P03", "state": "interior"},
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            predictions_path = temp / "predictions.jsonl"
            report_path = temp / "metrics.json"
            predictions_path.write_text(
                json.dumps(row, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            loaded = load_jsonl(predictions_path)
            status = main(
                [
                    "--predictions",
                    str(predictions_path),
                    "--report",
                    str(report_path),
                ]
            )

            self.assertEqual(0, status)
            self.assertEqual(1, len(loaded))
            self.assertEqual(
                1.0,
                json.loads(report_path.read_text())["metrics"]["phase"][
                    "phase_classification"
                ]["accuracy"],
            )


if __name__ == "__main__":
    unittest.main()
