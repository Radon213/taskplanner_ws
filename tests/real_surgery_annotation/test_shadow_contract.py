from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from tools.real_surgery_annotation.shadow_contract import (
    TRACE_LAYERS,
    TraceRecord,
    TraceWriter,
    resolve_case_evaluation_mask,
    resolve_case_phase_context,
    resolve_case_reference,
    resolve_case_tool_catalog,
    validate_trace_records,
)


class ShadowContractTest(unittest.TestCase):
    def test_trace_contract_includes_shadow_retraction_arm_lane(self) -> None:
        self.assertTrue(
            {
                "bed_robot_arm_group_request",
                "bed_robot_arm_group_command",
                "bed_robot_arm_group_status",
                "shadow_bed_robot_arm_group_sink",
            }.issubset(TRACE_LAYERS)
        )

    def test_trace_contract_includes_external_bed_arm_status(self) -> None:
        record = TraceRecord(
            run_id="run-1",
            sequence=0,
            mode="strict",
            layer="bed_robot_arm_status",
            topic="/external/bed_robot_arms/status",
            message_type="surgical_interop_msgs/msg/BedRobotArmStateArray",
            ros_time_sec=1.0,
            wall_time_sec=100.0,
            payload={"arms": [], "procedure_type": "thyroidectomy"},
        ).as_dict()

        self.assertEqual([], validate_trace_records([record]))

    def test_trace_contract_includes_replay_clock_observability(self) -> None:
        self.assertIn("shadow_replay_state", TRACE_LAYERS)

    def test_trace_contract_includes_fault_injection_observability(self) -> None:
        self.assertIn("fault_injection_status", TRACE_LAYERS)

    def test_trace_contract_includes_bt_context_ingress(self) -> None:
        record = TraceRecord(
            run_id="run-1",
            sequence=0,
            mode="strict",
            layer="bt_context_ingress",
            topic="/bt/context_ingress",
            message_type="surgical_msgs/msg/WorldState",
            ros_time_sec=1.0,
            wall_time_sec=100.0,
            payload={"surgeon_request_generation": 3},
        ).as_dict()

        self.assertEqual([], validate_trace_records([record]))

    def test_fault_injection_status_is_topic_and_schema_bound(self) -> None:
        record = TraceRecord(
            run_id="run-1",
            sequence=0,
            mode="strict",
            layer="fault_injection_status",
            topic="/test/fault/status",
            message_type="std_msgs/msg/String",
            ros_time_sec=1.0,
            wall_time_sec=100.0,
            payload={
                "schema": "taskplanner.fault_report.v1",
                "scenario_id": "noise",
                "seed": 7,
                "counters": {"flir": {"dropped": 2}},
            },
        ).as_dict()

        self.assertEqual([], validate_trace_records([record]))

    def test_trace_contract_separates_model_raw_from_operational_vlm(self) -> None:
        self.assertIn("vlm_model_raw", TRACE_LAYERS)
        self.assertIn("vlm_raw", TRACE_LAYERS)

    def test_trace_contract_includes_evidence_and_evaluation_only_layers(
        self,
    ) -> None:
        self.assertIn("vlm_tool_observation", TRACE_LAYERS)
        self.assertIn("evaluation_ground_truth", TRACE_LAYERS)

    def test_trace_contract_includes_normalized_perception_pipeline(self) -> None:
        self.assertTrue(
            {
                "normalized_input_image",
                "vlm_preprocessed_input_image",
                "vlm_model_input_image",
                "normalized_perception",
                "cam4_semantic_perception",
                "rfdetr_health",
                "rfdetr_diagnostics",
            }.issubset(TRACE_LAYERS)
        )

    def test_trace_writer_is_append_only_and_validates_full_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "trace.jsonl"
            with TraceWriter(path, run_id="run-1", mode="strict") as writer:
                writer.append(
                    layer="vlm_raw",
                    topic="/vlm/result",
                    message_type="surgical_msgs/msg/VLMResult",
                    ros_time_sec=1.0,
                    wall_time_sec=100.0,
                    payload={"raw_json": "{}"},
                )
                writer.append(
                    layer="bt_decision",
                    topic="/bt/decision",
                    message_type="surgical_msgs/msg/BTDecision",
                    ros_time_sec=2.0,
                    wall_time_sec=101.0,
                    payload={"action": "hold"},
                )
            records = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([], validate_trace_records(records))
            with self.assertRaises(FileExistsError):
                TraceWriter(path, run_id="run-2", mode="strict")

    def test_trace_payload_tampering_is_detected(self) -> None:
        record = TraceRecord(
            run_id="run-1",
            sequence=0,
            mode="strict",
            layer="vlm_raw",
            topic="/vlm/result",
            message_type="surgical_msgs/msg/VLMResult",
            ros_time_sec=1.0,
            wall_time_sec=100.0,
            payload={"raw_json": "{}"},
        ).as_dict()
        record["payload"]["raw_json"] = '{"changed":true}'
        self.assertTrue(
            any(
                "payload_sha256 mismatch" in error
                for error in validate_trace_records([record])
            )
        )

    def test_evaluation_ground_truth_layer_is_topic_and_payload_bound(
        self,
    ) -> None:
        record = TraceRecord(
            run_id="run-1",
            sequence=0,
            mode="strict",
            layer="evaluation_ground_truth",
            topic="/twin/world_state",
            message_type="std_msgs/msg/String",
            ros_time_sec=1.0,
            wall_time_sec=100.0,
            payload={
                "schema": "taskplanner.shadow_ground_truth.v2",
                "evaluation_only": False,
            },
        ).as_dict()

        errors = validate_trace_records([record])

        self.assertTrue(any("not valid for topic" in error for error in errors))
        self.assertTrue(
            any("evaluation_only=true" in error for error in errors)
        )

    def test_vlm_tool_observation_layer_rejects_wrong_topic(self) -> None:
        record = TraceRecord(
            run_id="run-1",
            sequence=0,
            mode="strict",
            layer="vlm_tool_observation",
            topic="/surgeon/request",
            message_type="surgical_msgs/msg/ToolObservation",
            ros_time_sec=1.0,
            wall_time_sec=100.0,
            payload={"instrument_id": "T04", "visible": True},
        ).as_dict()

        errors = validate_trace_records([record])

        self.assertTrue(any("not valid for topic" in error for error in errors))

    def test_trace_sequence_and_time_must_be_monotonic(self) -> None:
        first = TraceRecord(
            run_id="run-1",
            sequence=0,
            mode="strict",
            layer="vlm_raw",
            topic="/vlm/result",
            message_type="test/msg/Test",
            ros_time_sec=2.0,
            wall_time_sec=100.0,
            payload={},
        ).as_dict()
        second = TraceRecord(
            run_id="run-1",
            sequence=2,
            mode="strict",
            layer="bt_decision",
            topic="/bt/decision",
            message_type="test/msg/Test",
            ros_time_sec=1.0,
            wall_time_sec=101.0,
            payload={},
        ).as_dict()
        errors = validate_trace_records([first, second])
        self.assertTrue(any("sequence must be 1" in error for error in errors))
        self.assertTrue(any("is earlier than" in error for error in errors))

    def test_case_reference_uses_manifest_event_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            case_dir = Path(temp_dir) / "case"
            case_dir.mkdir()
            (case_dir / "old.jsonl").write_text("", encoding="utf-8")
            (case_dir / "final.jsonl").write_text("", encoding="utf-8")
            (case_dir / "annotation_manifest.json").write_text(
                json.dumps({"event_file": "final.jsonl"}),
                encoding="utf-8",
            )
            _manifest, event_path = resolve_case_reference(case_dir)
            self.assertEqual((case_dir / "final.jsonl").resolve(), event_path)

    def test_complete_dt_reference_precedes_legacy_event_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            case_dir = Path(temp_dir) / "case"
            case_dir.mkdir()
            (case_dir / "legacy.jsonl").write_text("", encoding="utf-8")
            (case_dir / "dt-v5.jsonl").write_text(
                '{"event_id":"v5"}\n',
                encoding="utf-8",
            )
            (case_dir / "annotation_manifest.json").write_text(
                json.dumps(
                    {
                        "event_file": "legacy.jsonl",
                        "evaluation_reference": {
                            "complete": True,
                            "dt_reference": {"file": "dt-v5.jsonl"},
                        },
                    }
                ),
                encoding="utf-8",
            )

            _manifest, event_path = resolve_case_reference(case_dir)

            self.assertEqual((case_dir / "dt-v5.jsonl").resolve(), event_path)

    def test_incomplete_evaluation_reference_keeps_legacy_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            case_dir = Path(temp_dir) / "case"
            case_dir.mkdir()
            (case_dir / "legacy.jsonl").write_text("", encoding="utf-8")
            (case_dir / "draft.jsonl").write_text("", encoding="utf-8")
            (case_dir / "annotation_manifest.json").write_text(
                json.dumps(
                    {
                        "event_file": "legacy.jsonl",
                        "evaluation_reference": {
                            "complete": False,
                            "dt_reference": {"file": "draft.jsonl"},
                        },
                    }
                ),
                encoding="utf-8",
            )

            _manifest, event_path = resolve_case_reference(case_dir)

            self.assertEqual((case_dir / "legacy.jsonl").resolve(), event_path)

    def test_conventional_evaluation_mask_sidecar_is_discovered(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            case_dir = Path(temp_dir) / "case"
            case_dir.mkdir()
            (case_dir / "events.jsonl").write_text("", encoding="utf-8")
            mask_path = case_dir / "evaluation_masks.v1.json"
            mask_path.write_text(
                json.dumps({"schema": "taskplanner.evaluation_masks.v1"}),
                encoding="utf-8",
            )
            manifest = {"event_file": "events.jsonl"}
            (case_dir / "annotation_manifest.json").write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )

            resolved = resolve_case_evaluation_mask(case_dir, manifest)

            self.assertEqual(mask_path.resolve(), resolved)

    def test_nested_evaluation_masks_descriptor_is_discovered(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            case_dir = Path(temp_dir) / "case"
            case_dir.mkdir()
            (case_dir / "events.jsonl").write_text("", encoding="utf-8")
            mask_path = case_dir / "scoring-mask.json"
            mask_path.write_text(
                json.dumps({"schema": "taskplanner.evaluation_masks.v1"}),
                encoding="utf-8",
            )
            manifest = {
                "event_file": "events.jsonl",
                "evaluation_reference": {
                    "evaluation_masks": {"file": "scoring-mask.json"}
                },
            }
            (case_dir / "annotation_manifest.json").write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )

            resolved = resolve_case_evaluation_mask(case_dir, manifest)

            self.assertEqual(mask_path.resolve(), resolved)

    def test_manifest_tool_catalog_and_provisional_phase_are_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            annotation_root = Path(temp_dir) / "observable_tool_events"
            case_dir = annotation_root / "cases" / "case"
            catalog_dir = annotation_root / "catalogs"
            case_dir.mkdir(parents=True)
            catalog_dir.mkdir()
            catalog_path = catalog_dir / "tools.yaml"
            phase_path = case_dir / "phase.jsonl"
            catalog_path.write_text("schema: test\n", encoding="utf-8")
            phase_path.write_text(
                '{"event_type":"phase_start","review_status":"ambiguous"}\n',
                encoding="utf-8",
            )
            manifest = {
                "tool_catalog_path": "../../catalogs/tools.yaml",
                "evaluation_reference": {
                    "complete": True,
                    "phase_reference_included": True,
                    "phase_reference": {
                        "file": "phase.jsonl",
                        "scoring_role": "context_only_not_ground_truth",
                        "status": "provisional_ambiguous",
                    },
                },
            }
            (case_dir / "annotation_manifest.json").write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )

            self.assertEqual(
                catalog_path.resolve(),
                resolve_case_tool_catalog(case_dir, manifest),
            )
            self.assertEqual(
                phase_path.resolve(),
                resolve_case_phase_context(case_dir, manifest),
            )


if __name__ == "__main__":
    unittest.main()
