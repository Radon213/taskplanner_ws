from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.real_surgery_annotation.run_shadow_replay import (
    CRITICAL_NODES,
    _build_parser,
    _model_preflight,
    _parse_shadow_state_json,
    _provider_api_key,
    _reference_authority,
    _resolve_case_dir,
    _resolve_start_phase_id,
    _select_groot2_port,
    _validate_public_input_trace,
    _validate_shadow_feedback_trace,
)


class _Response:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


class ShadowRunnerPreflightTest(unittest.TestCase):
    def test_generation_seed_is_explicit_and_overridable(self) -> None:
        parser = _build_parser(Path("/tmp/taskplanner"))

        default_args = parser.parse_args(["--source-bag", "/tmp/source"])
        disabled_args = parser.parse_args(
            [
                "--source-bag",
                "/tmp/source",
                "--vlm-generation-seed",
                "-1",
            ]
        )

        self.assertEqual(default_args.vlm_generation_seed, 0)
        self.assertEqual(disabled_args.vlm_generation_seed, -1)

    def test_case_dir_default_is_inferred_instead_of_pinned_to_old_case(self) -> None:
        parser = _build_parser(Path("/tmp/taskplanner"))

        args = parser.parse_args(["--source-bag", "/datasets/shadow/0704_6"])

        self.assertIsNone(args.case_dir)

    def test_case_dir_is_inferred_from_exact_source_path_component(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo_root = Path(temporary)
            expected = (
                repo_root
                / "annotations/observable_tool_events/cases/0704_6"
            )
            expected.mkdir(parents=True)

            resolved = _resolve_case_dir(
                repo_root,
                Path("/datasets/shadow/0704_6"),
                None,
            )

            self.assertEqual(resolved, expected.resolve())

    def test_case_dir_inference_handles_bag_file_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo_root = Path(temporary)
            expected = (
                repo_root
                / "annotations/observable_tool_events/cases/0704_6"
            )
            expected.mkdir(parents=True)

            resolved = _resolve_case_dir(
                repo_root,
                Path("/datasets/shadow/0704_6/0_0704_6.mcap"),
                None,
            )

            self.assertEqual(resolved, expected.resolve())

    def test_case_dir_inference_fails_closed_when_source_has_no_case(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo_root = Path(temporary)
            (
                repo_root
                / "annotations/observable_tool_events/cases/0704_6"
            ).mkdir(parents=True)

            with self.assertRaisesRegex(
                ValueError,
                "could not infer the annotation case",
            ):
                _resolve_case_dir(
                    repo_root,
                    Path("/datasets/shadow/unknown_case"),
                    None,
                )

    def test_explicit_case_dir_overrides_inference(self) -> None:
        requested = Path("/tmp/custom-case")

        resolved = _resolve_case_dir(
            Path("/tmp/taskplanner"),
            Path("/datasets/shadow/0704_6"),
            requested,
        )

        self.assertEqual(resolved, requested.resolve())

    def test_shadow_state_parser_handles_apostrophe_in_error_text(self) -> None:
        payload = {
            "state": "held",
            "vlm_last_error": "Input exceeds model's maximum context length",
        }
        encoded = repr(json.dumps(payload, separators=(",", ":")))
        output = (
            "surgical_msgs.srv.ControlShadowReplay_Response("
            f"success=True, message='ok', state_json={encoded})"
        )

        self.assertEqual(_parse_shadow_state_json(output), payload)

    def test_shadow_state_parser_handles_yaml_style_double_quotes(self) -> None:
        payload = {"state": "running", "pending_vlm_count": 1}
        encoded = json.dumps(json.dumps(payload, separators=(",", ":")))
        output = f"success: true\nstate_json: {encoded}\n"

        self.assertEqual(_parse_shadow_state_json(output), payload)

    def test_unscored_case_reports_no_reference_authority(self) -> None:
        manifest = {
            "annotation_adjudication": {
                "authority": "none",
                "confirmed_origin_counts": {},
            }
        }
        self.assertEqual(_reference_authority(manifest), "none")

    def test_empty_legacy_adjudication_is_not_reported_as_mixed(self) -> None:
        manifest = {
            "annotation_adjudication": {
                "confirmed_origin_counts": {},
            }
        }
        self.assertEqual(_reference_authority(manifest), "none")

    def test_provider_specific_api_key_precedes_legacy_key(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "VLM_API_KEY": "legacy-key",
                "VLLM_API_KEY": "vllm-key",
            },
            clear=True,
        ):
            self.assertEqual(_provider_api_key("vllm"), "vllm-key")

    def test_provider_api_key_falls_back_to_legacy_key(self) -> None:
        with patch.dict(
            "os.environ",
            {"VLM_API_KEY": "legacy-key"},
            clear=True,
        ):
            self.assertEqual(_provider_api_key("vllm"), "legacy-key")

    def test_case_manifest_supplies_non_ground_truth_phase_bootstrap(self) -> None:
        phase_id, source = _resolve_start_phase_id(
            "",
            {"shadow_replay": {"start_phase_id": "P03"}},
        )
        self.assertEqual(phase_id, "P03")
        self.assertEqual(source, "case_manifest")

    def test_explicit_start_phase_overrides_case_manifest(self) -> None:
        phase_id, source = _resolve_start_phase_id(
            "P04",
            {"shadow_replay": {"start_phase_id": "P03"}},
        )
        self.assertEqual(phase_id, "P04")
        self.assertEqual(source, "cli")

    def test_groot2_port_is_domain_derived_outside_ephemeral_range(self) -> None:
        with patch(
            "tools.real_surgery_annotation.run_shadow_replay._port_is_available",
            return_value=True,
        ):
            self.assertEqual(_select_groot2_port(0, 71), 20071)

    def test_groot2_port_falls_forward_when_preferred_port_is_busy(self) -> None:
        with patch(
            "tools.real_surgery_annotation.run_shadow_replay._port_is_available",
            side_effect=lambda port: port == 20072,
        ):
            self.assertEqual(_select_groot2_port(0, 71), 20072)

    def test_explicit_unavailable_groot2_port_fails_closed(self) -> None:
        with patch(
            "tools.real_surgery_annotation.run_shadow_replay._port_is_available",
            return_value=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                _select_groot2_port(20123, 71)

    def test_public_speech_nodes_are_runtime_critical(self) -> None:
        self.assertIn("/recorded_transcript_adapter", CRITICAL_NODES)
        self.assertIn("/speech_input_adapter", CRITICAL_NODES)

    def test_lmstudio_rejects_catalog_model_that_is_not_loaded(self) -> None:
        catalog = {"data": [{"id": "vision-model"}]}
        native = {
            "data": [
                {"id": "vision-model", "state": "not-loaded"},
                {"id": "other-model", "state": "loaded"},
            ]
        }
        with patch(
            "tools.real_surgery_annotation.run_shadow_replay.urlopen",
            side_effect=[_Response(catalog), _Response(native)],
        ):
            with self.assertRaisesRegex(RuntimeError, "is not loaded"):
                _model_preflight(
                    provider_id="lmstudio",
                    base_url="http://127.0.0.1:1234",
                    model_id="vision-model",
                    api_key="",
                    timeout_sec=1.0,
                )

    def test_lmstudio_reports_verified_loaded_models(self) -> None:
        catalog = {"data": [{"id": "vision-model"}, {"id": "other-model"}]}
        native = {
            "data": [
                {"id": "vision-model", "state": "loaded"},
                {"id": "other-model", "state": "not-loaded"},
            ]
        }
        with patch(
            "tools.real_surgery_annotation.run_shadow_replay.urlopen",
            side_effect=[_Response(catalog), _Response(native)],
        ):
            result = _model_preflight(
                provider_id="lmstudio",
                base_url="http://127.0.0.1:1234",
                model_id="vision-model",
                api_key="",
                timeout_sec=1.0,
            )
        self.assertTrue(result["selected_model_loaded"])
        self.assertTrue(result["load_state_verified"])
        self.assertEqual(result["loaded_models"], ["vision-model"])
        self.assertEqual(result["load_state_source"], "lmstudio_api_v0_models")

    def test_vllm_manager_rejects_catalog_model_that_is_not_loaded(self) -> None:
        catalog = {"data": [{"id": "vision-model"}, {"id": "other-model"}]}
        manager = {
            "state": "loaded",
            "model_id": "other-model",
        }
        with patch(
            "tools.real_surgery_annotation.run_shadow_replay.urlopen",
            side_effect=[_Response(catalog), _Response(manager)],
        ):
            with self.assertRaisesRegex(RuntimeError, "is not loaded"):
                _model_preflight(
                    provider_id="vllm",
                    base_url="http://127.0.0.1:8001",
                    model_id="vision-model",
                    api_key="",
                    timeout_sec=1.0,
                )

    def test_vllm_manager_reports_only_the_active_loaded_model(self) -> None:
        catalog = {"data": [{"id": "vision-model"}, {"id": "other-model"}]}
        manager = {
            "state": "loaded",
            "model_id": "vision-model",
        }
        with patch(
            "tools.real_surgery_annotation.run_shadow_replay.urlopen",
            side_effect=[_Response(catalog), _Response(manager)],
        ):
            result = _model_preflight(
                provider_id="vllm",
                base_url="http://127.0.0.1:8001",
                model_id="vision-model",
                api_key="",
                timeout_sec=1.0,
            )
        self.assertTrue(result["selected_model_loaded"])
        self.assertTrue(result["load_state_verified"])
        self.assertEqual(result["loaded_models"], ["vision-model"])
        self.assertEqual(result["load_state_source"], "vllm_manager_status")

    def test_public_input_integrity_requires_admitted_speech(self) -> None:
        bag_info = {
            "topics": {
                "/cam": {"message_count": 1},
                "/transcript": {"message_count": 1},
            }
        }
        records = [
            {"layer": "input_image", "topic": "/cam"},
            {"layer": "input_transcript", "topic": "/transcript"},
        ]
        result = _validate_public_input_trace(
            records,
            bag_info=bag_info,
            field_image_topic="/cam",
            source_transcript_topic="/transcript",
        )
        self.assertFalse(result["ok"])
        self.assertIn(
            "admitted_speech_count_mismatch:expected=1,recorded=0",
            result["errors"],
        )

    def test_public_input_integrity_accepts_complete_path(self) -> None:
        bag_info = {
            "topics": {
                "/cam": {"message_count": 1},
                "/transcript": {"message_count": 1},
            }
        }
        records = [
            {"layer": "input_image", "topic": "/cam"},
            {"layer": "input_transcript", "topic": "/transcript"},
            {
                "layer": "input_transcript",
                "topic": "/surgery/audio/request_text",
            },
        ]
        result = _validate_public_input_trace(
            records,
            bag_info=bag_info,
            field_image_topic="/cam",
            source_transcript_topic="/transcript",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["admitted_speech_count"], 1)

    def test_public_input_integrity_accepts_interactive_topic_remap(self) -> None:
        bag_info = {
            "topics": {
                "/source/cam": {"message_count": 1},
                "/transcript": {"message_count": 1},
            }
        }
        records = [
            {"layer": "input_image", "topic": "/runtime/cam"},
            {"layer": "input_transcript", "topic": "/transcript"},
            {
                "layer": "input_transcript",
                "topic": "/surgery/audio/request_text",
            },
        ]

        result = _validate_public_input_trace(
            records,
            bag_info=bag_info,
            field_image_topic="/source/cam",
            source_transcript_topic="/transcript",
            recorded_field_image_topic="/runtime/cam",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(
            "/runtime/cam",
            result["recorded_field_image_topic"],
        )

    def test_public_input_integrity_accepts_minor_best_effort_loss(self) -> None:
        bag_info = {
            "topics": {
                "/cam": {"message_count": 100},
                "/transcript": {"message_count": 0},
            }
        }
        records = [
            {"layer": "input_image", "topic": "/cam"}
            for _ in range(98)
        ]

        result = _validate_public_input_trace(
            records,
            bag_info=bag_info,
            field_image_topic="/cam",
            source_transcript_topic="/transcript",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["field_image_coverage"]["dropped"], 2)
        self.assertEqual(
            result["field_image_coverage"]["coverage_ratio"],
            0.98,
        )

    def test_public_input_integrity_rejects_material_best_effort_loss(
        self,
    ) -> None:
        bag_info = {
            "topics": {
                "/cam": {"message_count": 100},
                "/transcript": {"message_count": 0},
            }
        }
        records = [
            {"layer": "input_image", "topic": "/cam"}
            for _ in range(97)
        ]

        result = _validate_public_input_trace(
            records,
            bag_info=bag_info,
            field_image_topic="/cam",
            source_transcript_topic="/transcript",
        )

        self.assertFalse(result["ok"])
        self.assertIn(
            "field_image_coverage_below_minimum:"
            "expected=100,recorded=97,ratio=0.9700,minimum=0.9800",
            result["errors"],
        )

    def test_public_input_integrity_audits_multiview_and_bounded_perception(
        self,
    ) -> None:
        bag_info = {
            "topics": {
                "/source/flir": {"message_count": 1},
                "/source/cam4": {"message_count": 1},
                "/source/bboxes": {"message_count": 1},
                "/source/segmentation": {"message_count": 1},
                "/transcript": {"message_count": 1},
            }
        }
        context = {
            "visual_input": {
                "image_source": "flir_rfdetr_segmented",
                "sources": [
                    {"role": "flir", "stamp_sec": 44.05},
                ],
            },
            "observable_perception": {
                "source": "cam4_rfdetr_small",
                "cam4_image_forwarded_to_vlm": False,
                "ground_truth": False,
                "alignment": {"status": "aligned"},
                "tools": [
                    {
                        "class_name": "Bovie",
                        "bbox_xywh_norm": [0.465, 0.449, 0.163, 0.072],
                    }
                ],
            },
        }
        records = [
            {"layer": "input_image", "topic": "/runtime/field"},
            {"layer": "normalized_input_image", "topic": "/runtime/flir"},
            {"layer": "normalized_input_image", "topic": "/runtime/cam4"},
            {
                "layer": "normalized_perception",
                "topic": "/runtime/bboxes",
            },
            {
                "layer": "normalized_perception",
                "topic": "/runtime/segmentation",
            },
            {
                "layer": "vlm_model_input_image",
                "topic": "/runtime/composite",
            },
            {
                "layer": "vlm_preprocessed_input_image",
                "topic": "/runtime/segmented-flir",
            },
            {"layer": "input_transcript", "topic": "/transcript"},
            {
                "layer": "input_transcript",
                "topic": "/surgery/audio/request_text",
            },
            {
                "layer": "vlm_request",
                "payload": {"compact_json": json.dumps(context)},
            },
        ]

        result = _validate_public_input_trace(
            records,
            bag_info=bag_info,
            field_image_topic="/source/flir",
            source_transcript_topic="/transcript",
            recorded_field_image_topic="/runtime/field",
            cam4_image_topic="/source/cam4",
            perception_bboxes_topic="/source/bboxes",
            perception_segmentation_topic="/source/segmentation",
            recorded_flir_image_topic="/runtime/flir",
            recorded_cam4_image_topic="/runtime/cam4",
            recorded_perception_bboxes_topic="/runtime/bboxes",
            recorded_perception_segmentation_topic="/runtime/segmentation",
            composite_image_topic="/runtime/composite",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["recorded_vlm_composite_count"], 1)
        self.assertEqual(
            result["recorded_vlm_preprocessed_input_count"],
            1,
        )
        self.assertEqual(result["auditable_perception_context_count"], 1)

    def test_public_input_integrity_rejects_missing_segmented_flir_trace(
        self,
    ) -> None:
        bag_info = {
            "topics": {
                "/source/flir": {"message_count": 1},
                "/source/cam4": {"message_count": 1},
                "/source/bboxes": {"message_count": 1},
                "/source/segmentation": {"message_count": 1},
                "/transcript": {"message_count": 0},
            }
        }
        context = {
            "visual_input": {"image_source": "flir_rfdetr_segmented"},
            "observable_perception": {
                "source": "cam4_rfdetr_small",
                "cam4_image_forwarded_to_vlm": False,
                "ground_truth": False,
                "alignment": {"status": "aligned"},
                "tools": [],
            },
        }
        records = [
            {"layer": "input_image", "topic": "/runtime/field"},
            {"layer": "normalized_input_image", "topic": "/runtime/flir"},
            {"layer": "normalized_input_image", "topic": "/runtime/cam4"},
            {"layer": "normalized_perception", "topic": "/runtime/bboxes"},
            {
                "layer": "normalized_perception",
                "topic": "/runtime/segmentation",
            },
            {
                "layer": "vlm_model_input_image",
                "topic": "/runtime/composite",
            },
            {
                "layer": "vlm_request",
                "payload": {"compact_json": json.dumps(context)},
            },
        ]

        result = _validate_public_input_trace(
            records,
            bag_info=bag_info,
            field_image_topic="/source/flir",
            source_transcript_topic="/transcript",
            recorded_field_image_topic="/runtime/field",
            cam4_image_topic="/source/cam4",
            perception_bboxes_topic="/source/bboxes",
            perception_segmentation_topic="/source/segmentation",
            recorded_flir_image_topic="/runtime/flir",
            recorded_cam4_image_topic="/runtime/cam4",
            recorded_perception_bboxes_topic="/runtime/bboxes",
            recorded_perception_segmentation_topic="/runtime/segmentation",
            composite_image_topic="/runtime/composite",
        )

        self.assertFalse(result["ok"])
        self.assertIn(
            "vlm_preprocessed_input_image_missing",
            result["errors"],
        )

    def test_public_input_integrity_rejects_full_rle_in_prompt_context(
        self,
    ) -> None:
        bag_info = {
            "topics": {
                "/source/flir": {"message_count": 1},
                "/source/cam4": {"message_count": 1},
                "/source/bboxes": {"message_count": 1},
                "/source/segmentation": {"message_count": 1},
                "/transcript": {"message_count": 0},
            }
        }
        records = [
            {"layer": "input_image", "topic": "/runtime/field"},
            {"layer": "normalized_input_image", "topic": "/runtime/flir"},
            {"layer": "normalized_input_image", "topic": "/runtime/cam4"},
            {
                "layer": "normalized_perception",
                "topic": "/runtime/bboxes",
            },
            {
                "layer": "normalized_perception",
                "topic": "/runtime/segmentation",
            },
            {
                "layer": "vlm_model_input_image",
                "topic": "/runtime/composite",
            },
            {
                "layer": "vlm_request",
                "payload": {
                    "compact_json": json.dumps(
                        {
                            "visual_input": {
                                "image_source": "composite(cam4+flir)"
                            },
                            "observable_perception": {
                                "bboxes": {},
                                "segmentation": {
                                    "segmentation_rle": {
                                        "counts": "must-not-leak"
                                    }
                                },
                            },
                        }
                    )
                },
            },
        ]

        result = _validate_public_input_trace(
            records,
            bag_info=bag_info,
            field_image_topic="/source/flir",
            source_transcript_topic="/transcript",
            recorded_field_image_topic="/runtime/field",
            cam4_image_topic="/source/cam4",
            perception_bboxes_topic="/source/bboxes",
            perception_segmentation_topic="/source/segmentation",
            recorded_flir_image_topic="/runtime/flir",
            recorded_cam4_image_topic="/runtime/cam4",
            recorded_perception_bboxes_topic="/runtime/bboxes",
            recorded_perception_segmentation_topic="/runtime/segmentation",
            composite_image_topic="/runtime/composite",
        )

        self.assertFalse(result["ok"])
        self.assertIn(
            "auditable_perception_context_missing",
            result["errors"],
        )

    def test_counterfactual_feedback_integrity_requires_matching_audit_chain(self) -> None:
        command_id = "cmd-1"
        records = [
            {
                "layer": "shadow_sink",
                "payload": {
                    "command_id": command_id,
                    "status": "admissible",
                    "counterfactual_feedback_published": True,
                    "ground_truth_used": False,
                    "execution_attempted": False,
                },
            },
            {
                "layer": "skill_event",
                "payload": {
                    "mode": "shadow_counterfactual",
                    "detail_json": json.dumps(
                        {
                            "command_id": command_id,
                            "ground_truth_used": False,
                            "physical_execution_attempted": False,
                        }
                    ),
                },
            },
            {
                "layer": "skill_status",
                "payload": {
                    "mode": "shadow_counterfactual",
                    "command_id": command_id,
                    "state": "completed",
                    "success": True,
                },
            },
            {
                "layer": "reducer_event",
                "payload": {
                    "input_type": "shadow_state_assumption",
                    "detail_json": json.dumps(
                        {
                            "event_type": "ShadowRequestCapacityReconciled",
                            "ground_truth_used": False,
                        }
                    ),
                },
            },
        ]

        result = _validate_shadow_feedback_trace(records, enabled=True)

        self.assertTrue(result["ok"])
        self.assertEqual(result["completed_status_count"], 1)
        self.assertEqual(result["shadow_state_assumption_count"], 1)
        self.assertEqual(result["ground_truth_use_count"], 0)
        self.assertEqual(result["physical_execution_attempt_count"], 0)

    def test_counterfactual_feedback_integrity_rejects_gt_use(self) -> None:
        records = [
            {
                "layer": "shadow_sink",
                "payload": {
                    "command_id": "cmd-1",
                    "status": "admissible",
                    "counterfactual_feedback_published": True,
                    "ground_truth_used": True,
                    "execution_attempted": False,
                },
            }
        ]

        result = _validate_shadow_feedback_trace(records, enabled=True)

        self.assertFalse(result["ok"])
        self.assertIn(
            "counterfactual_feedback_used_ground_truth:1",
            result["errors"],
        )

    def test_shadow_state_assumption_integrity_rejects_gt_use(self) -> None:
        records = [
            {
                "layer": "reducer_event",
                "payload": {
                    "input_type": "shadow_state_assumption",
                    "detail_json": json.dumps(
                        {
                            "event_type": "ShadowRequestCapacityReconciled",
                            "ground_truth_used": True,
                        }
                    ),
                },
            }
        ]

        result = _validate_shadow_feedback_trace(records, enabled=False)

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["shadow_state_assumption_ground_truth_use_count"],
            1,
        )


if __name__ == "__main__":
    unittest.main()
