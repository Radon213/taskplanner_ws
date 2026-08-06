from __future__ import annotations

import unittest

from tools.real_surgery_annotation.render_shadow_report import (
    render_markdown,
    render_timeline_svg,
)


class ShadowReportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = {
            "case_id": "fixture",
            "run_id": "run-1",
            "mode": "strict",
            "status": "complete",
            "source_bag": {"sha256": "a" * 64},
            "reference": {"runtime_visible": False},
            "runtime": {
                "source_duration_sec": 10.0,
                "input_integrity": {
                    "field_image_coverage": {
                        "coverage_ratio": 0.99,
                        "dropped": 1,
                    },
                    "flir_image_coverage": {
                        "coverage_ratio": 1.0,
                        "dropped": 0,
                    },
                    "cam4_image_coverage": {
                        "coverage_ratio": 0.98,
                        "dropped": 2,
                    },
                    "bbox_coverage": {
                        "coverage_ratio": 1.0,
                        "dropped": 0,
                    },
                    "segmentation_coverage": {
                        "coverage_ratio": 1.0,
                        "dropped": 0,
                    },
                },
            },
        }
        self.evaluation = {
            "reference_authority": "human_video_review",
            "runtime": {
                "input_image_count": 10,
                "input_transcript_count": 1,
                "vlm_result_count": 5,
                "vlm_unhealthy_count": 0,
                "vlm_latency_sec": {"median": 1.0, "p95": 1.2},
                "replay_source_duration_sec": 10.0,
                "replay_wall_elapsed_sec": 13.0,
                "replay_realtime_factor": 10 / 13,
                "replay_elastic_hold_sec": 3.0,
                "replay_hold_breakdown_sec": {"skill_execution": 3.0},
                "trace_contract_error_count": 0,
                "skill_command_count": 3,
                "skill_command_semantic_admission_count": 1,
                "skill_command_duplicate_suppressed_count": 2,
                "skill_command_duplicate_rate": 2 / 3,
                "shadow_state_assumption_count": 2,
                "shadow_state_assumption_counts": {
                    "ShadowRequestCapacityReconciled": 2,
                },
                "shadow_state_assumption_ground_truth_use_count": 0,
                "skill_command_after_completion_count": 0,
            },
            "phase": {"status": "not_available"},
            "behavior_quality": {
                "summary": {
                    "preparation_coverage": 0.5,
                    "request_to_handover_latency_sec": {
                        "count": 2,
                        "mean": 0.6,
                        "median": 0.5,
                        "p95": 0.8,
                        "max": 0.8,
                    },
                    "request_to_handover_wall_clock_latency_sec": {
                        "count": 2,
                        "mean": 3.5,
                        "median": 3.0,
                        "p95": 5.0,
                        "max": 5.0,
                    },
                    "wrong_preposition_release_latency_sec": {
                        "count": 1,
                        "mean": 0.2,
                        "median": 0.2,
                        "p95": 0.2,
                        "max": 0.2,
                    },
                    "wrong_preposition_release_wall_clock_latency_sec": {
                        "count": 1,
                        "mean": 2.0,
                        "median": 2.0,
                        "p95": 2.0,
                        "max": 2.0,
                    },
                },
                "preparation_coverage": {
                    "prepared_before_request_count": 1,
                    "eligible_handover_count": 2,
                },
            },
            "scorecard": {
                "next_tool_prediction": {
                    "reference_quality": "confirmed handover targets",
                    "layers": {
                        "vlm_raw": {
                            "correct_count": 1,
                            "evaluated_count": 2,
                            "accuracy": 0.5,
                            "combined_action_selection_accuracy": 0.5,
                            "anticipatory_correct_count": 1,
                            "anticipatory_target_recall": 0.5,
                            "request_backed_correct_count": 0,
                            "request_backed_target_recall": 0.0,
                        }
                    },
                },
                "dt_tool_management": {
                    "correct_count": 0,
                    "evaluated_count": 0,
                    "endpoint_accuracy": None,
                    "status": "inventory_conservation_only",
                    "instance_inventory_accuracy": 1.0,
                    "instance_inventory_status": (
                        "complete_declared_inventory_conservation"
                    ),
                    "inventory_contract": {
                        "physical_instance_count": 12,
                    },
                }
            },
            "layers": {
                layer: {
                    "target_count": 1,
                    "outcomes": {
                        "exact_match": 1,
                        "wrong_prediction": 0,
                        "missed_opportunity": 0,
                        "unsafe_or_impossible": 0,
                    },
                    "top1_exact_rate": 1.0,
                    "stable_exact_rate": 0.0,
                    "request_backed_exact_count": 0,
                    "anticipatory_exact_count": 1,
                    "false_positive_count": 0,
                    "first_correct_lead_sec": {"median": 2.0},
                    "events": [
                        {
                            "time_sec": 5.0,
                            "outcome": "exact_match",
                            "target_tool_id": "scalpel",
                            "predicted_tool_id": "scalpel",
                            "first_correct_lead_sec": 2.0,
                        }
                    ],
                }
                for layer in (
                    "vlm_raw",
                    "reducer_fused",
                    "bt_decision",
                    "skill_command",
                )
            },
        }

    def test_markdown_has_layer_table_and_phase_limitation(self) -> None:
        output = render_markdown(
            manifest=self.manifest,
            evaluation=self.evaluation,
        )
        self.assertIn("Tool Decision Layers", output)
        self.assertIn("Phase ground truth is not available", output)
        self.assertIn(
            "Skill commands / semantic admissions / duplicate-suppressed: 3 / 1 / 2",
            output,
        )
        self.assertIn("Shadow state reconciliations from public requests: 2", output)
        self.assertIn("Replay source / wall time: 10.000 / 13.000 s", output)
        self.assertIn("Elastic hold total / breakdown: 3.000 s", output)
        self.assertIn("Behavior Quality", output)
        self.assertIn(
            "Request-to-handover latency, source clock "
            "(median / p95 / max): 0.500 / 0.800 / 0.800 (n=2) s",
            output,
        )
        self.assertIn(
            "Request-to-handover latency, wall clock "
            "(median / p95 / max): 3.000 / 5.000 / 5.000 (n=2) s",
            output,
        )
        self.assertIn("Wall-clock distributions include elastic replay holds", output)
        self.assertIn(
            "Trace input coverage (field / FLIR / CAM4 / bbox / segmentation): "
            "99.0% (1 dropped) / 100.0% (0 dropped) / "
            "98.0% (2 dropped)",
            output,
        )
        self.assertIn(
            "DT inventory conservation | Reducer fused | 12 / 12 | 100.0%",
            output,
        )
        self.assertIn("Tool Decision Timing", output)
        self.assertIn(
            "VLM operational (publish time) | 1 / 2 | 50.0% | "
            "0 | 0 / 2 | 0.0% | 50.0%",
            output,
        )
        self.assertIn(
            "Combined tool action selection | "
            "VLM operational (publish time) | 1 / 2 | 50.0%",
            output,
        )

    def test_svg_contains_all_layers_and_escapes_labels(self) -> None:
        self.manifest["case_id"] = "fixture<&"
        output = render_timeline_svg(
            manifest=self.manifest,
            evaluation=self.evaluation,
        )
        self.assertIn("<svg", output)
        self.assertIn("VLM operational (publish time)", output)
        self.assertIn("fixture&lt;&amp;", output)


if __name__ == "__main__":
    unittest.main()
