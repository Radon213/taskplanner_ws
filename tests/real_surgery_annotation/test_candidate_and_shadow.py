from __future__ import annotations

import unittest
import json
from pathlib import Path

from tools.real_surgery_annotation.generate_timelens2_candidates import (
    normalize_candidates,
)
from tools.real_surgery_annotation.run_timelens2 import extract_interval_pairs
from tools.real_surgery_annotation.shadow_evaluate import (
    _provisional_phase_context_report,
    _vlm_prediction,
    evaluate_shadow,
    load_tool_identity_map,
    normalize_tool_id,
)
from tools.real_surgery_annotation.shadow_contract import TraceRecord


QUERY_SPEC = {
    "queries": [
        {
            "id": "handover",
            "text": "The scrub nurse hands a surgical instrument to the surgeon.",
            "suggested_action": "handover",
        }
    ]
}
ROOT = Path(__file__).resolve().parents[2]


class CandidateAndShadowTest(unittest.TestCase):
    @staticmethod
    def _event(
        *,
        event_id: str,
        time_sec: float,
        tool_id: str,
    ) -> dict:
        return {
            "review_status": "confirmed",
            "label_origin": "human_video_review",
            "review": {"reviewer_kind": "human"},
            "event_id": event_id,
            "event_type": "tool_transfer",
            "time_sec": time_sec,
            "tool": {
                "id": tool_id,
                "name": tool_id,
                "instance_id": f"fixture-tool-{event_id}",
            },
            "from": {
                "holder": "scrub_nurse",
                "location": "hand_unspecified",
            },
            "to": {
                "holder": "surgeon",
                "location": "hand_unspecified",
            },
            "visibility": "clear",
        }

    @staticmethod
    def _trace(
        *,
        sequence: int,
        time_sec: float,
        layer: str,
        payload: dict,
        mode: str = "strict",
        source_stamp_sec: float | None = None,
    ) -> dict:
        return TraceRecord(
            run_id="fixture-run",
            sequence=sequence,
            mode=mode,
            layer=layer,
            topic={
                "vlm_model_raw": "/vlm/model_raw_result",
                "vlm_raw": "/vlm/result",
                "reducer_fused": "/twin/world_state",
                "bt_decision": "/bt/decision",
                "skill_command": "/bt/skill_command",
                "bed_robot_arm_group_request": (
                    "/bed_robot_arm_group/request"
                ),
                "bed_robot_arm_group_command": (
                    "/bed_robot_arm_group/command"
                ),
                "bed_robot_arm_group_status": (
                    "/bed_robot_arm_group/status"
                ),
                "shadow_bed_robot_arm_group_sink": (
                    "/shadow/bed_robot_arm_group_sink"
                ),
            }[layer],
            message_type="fixture/msg/Test",
            ros_time_sec=time_sec,
            wall_time_sec=1000.0 + time_sec,
            payload=payload,
            source_stamp_sec=source_stamp_sec,
        ).as_dict()

    def test_tool_catalog_maps_procedure_refs_to_dataset_ids(self) -> None:
        identity_map = load_tool_identity_map(
            ROOT / "annotations/observable_tool_events/catalogs/tools.yaml"
        )
        self.assertEqual("scalpel", normalize_tool_id("T01", identity_map))
        self.assertEqual(
            "army_navy_retractor",
            normalize_tool_id("Army-Navy Retractor", identity_map),
        )
        self.assertEqual(
            "senn_miller_retractor",
            normalize_tool_id("T06", identity_map),
        )
        self.assertEqual(
            "harmonic_shears",
            normalize_tool_id("T09", identity_map),
        )
        self.assertEqual(
            "yankauer_suction",
            normalize_tool_id("T10", identity_map),
        )

    def test_provisional_phase_context_is_reported_but_not_scored(self) -> None:
        report = _provisional_phase_context_report(
            [
                {
                    "event_id": "case-PH0001",
                    "event_type": "phase_start",
                    "phase_id": "P03",
                    "source_frame_idx": 0,
                    "time_sec": 0.0,
                    "review_status": "ambiguous",
                },
                {
                    "event_id": "case-PH0002",
                    "event_type": "phase_start",
                    "phase_id": "P04",
                    "source_frame_idx": 100,
                    "time_sec": 8.0,
                    "review_status": "ambiguous",
                },
            ]
        )

        self.assertEqual("provisional_context_only", report["status"])
        self.assertFalse(report["scoring_ready"])
        self.assertEqual({}, report["layers"])
        self.assertEqual(8.0, report["boundaries"][0]["end_sec"])
        self.assertIsNone(report["boundaries"][1]["end_sec"])

    def test_procedure_ref_prediction_matches_dataset_canonical_truth(self) -> None:
        event = self._event(
            event_id="fixture-E0001",
            time_sec=5.0,
            tool_id="scalpel",
        )
        trace = [
            self._trace(
                sequence=0,
                time_sec=2.0,
                layer="vlm_raw",
                payload={
                    "raw_json": json.dumps(
                        {
                            "v": "4",
                            "phase": [["P01", 0.9]],
                            "tool": [["T01", 0.9]],
                            "intent": ["handover", "T01", 0.9],
                        }
                    )
                },
            )
        ]
        identity_map = load_tool_identity_map(
            ROOT / "annotations/observable_tool_events/catalogs/tools.yaml"
        )
        report = evaluate_shadow(
            ground_truth=[event],
            decisions=trace,
            lead_window_sec=10.0,
            tool_identity_map=identity_map,
        )
        self.assertEqual(
            1,
            report["layers"]["vlm_raw"]["outcomes"]["exact_match"],
        )
        event_result = report["layers"]["vlm_raw"]["events"][0]
        self.assertEqual("T01", event_result["predicted_raw_tool_id"])

    def test_bed_robot_group_action_is_scored_outside_ordinary_handover(
        self,
    ) -> None:
        report = evaluate_shadow(
            ground_truth=[
                self._event(
                    event_id="fixture-army-navy",
                    time_sec=10.0,
                    tool_id="army_navy_retractor",
                ),
                self._event(
                    event_id="fixture-scalpel",
                    time_sec=20.0,
                    tool_id="scalpel",
                ),
            ],
            decisions=[
                self._trace(
                    sequence=0,
                    time_sec=0.0,
                    layer="bed_robot_arm_group_status",
                    payload={
                        "group_id": "retraction",
                        "end_effector_profile": "army_navy_retractor",
                        "state": "standby",
                    },
                ),
                self._trace(
                    sequence=1,
                    time_sec=8.0,
                    layer="bed_robot_arm_group_command",
                    payload={
                        "command_id": "group-command-1",
                        "request_id": "group-request-1",
                        "group_id": "retraction",
                        "arm_id": "arm_1",
                        "target_tool_id": "army_navy_retractor",
                        "end_effector_profile": "army_navy_retractor",
                        "operation": "change_end_effector",
                    },
                ),
                self._trace(
                    sequence=2,
                    time_sec=8.1,
                    layer="shadow_bed_robot_arm_group_sink",
                    payload={
                        "command_id": "group-command-1",
                        "request_id": "group-request-1",
                        "group_id": "retraction",
                        "arm_id": "arm_1",
                        "target_tool_id": "army_navy_retractor",
                        "operation": "change_end_effector",
                    },
                ),
                self._trace(
                    sequence=3,
                    time_sec=8.5,
                    layer="bed_robot_arm_group_status",
                    payload={
                        "command_id": "group-command-1",
                        "request_id": "group-request-1",
                        "group_id": "retraction",
                        "arm_id": "arm_1",
                        "target_tool_id": "army_navy_retractor",
                        "end_effector_profile": "army_navy_retractor",
                        "operation": "change_end_effector",
                        "terminal": True,
                        "success": True,
                        "state": "standby",
                        "outcome": "succeeded",
                    },
                ),
                self._trace(
                    sequence=4,
                    time_sec=12.0,
                    layer="bed_robot_arm_group_command",
                    payload={
                        "command_id": "group-command-2",
                        "request_id": "group-request-2",
                        "group_id": "retraction",
                        "adjustment_mode": "single",
                        "target_retractor_id": "left_malleable",
                        "direction_frame": "surgeon_view",
                        "direction": "left",
                        "axis": "none",
                        "distance_mm": 10.0,
                        "end_effector_profile": "army_navy_retractor",
                        "operation": "retraction",
                    },
                ),
                self._trace(
                    sequence=5,
                    time_sec=18.0,
                    layer="bt_decision",
                    payload={
                        "selected_tool": "scalpel",
                        "action": "handover",
                        "confidence": 1.0,
                    },
                ),
            ],
            lead_window_sec=10.0,
        )

        self.assertEqual(2, report["confirmed_handover_count"])
        self.assertEqual(1, report["confirmed_ordinary_handover_count"])
        self.assertEqual(
            1,
            report["confirmed_specialized_group_action_count"],
        )
        bt_layer = report["layers"]["bt_decision"]
        self.assertEqual(1, bt_layer["target_count"])
        self.assertEqual(1, bt_layer["outcomes"]["exact_match"])
        self.assertEqual(0, bt_layer["outcomes"]["missed_opportunity"])

        specialized = report["specialized_group_actions"]
        self.assertEqual("complete", specialized["status"])
        self.assertEqual(1, specialized["target_count"])
        self.assertEqual(1, specialized["exact_match_count"])
        self.assertEqual(1.0, specialized["command_recall"])
        self.assertEqual(0, specialized["false_positive_command_count"])
        self.assertEqual(1, specialized["non_activation_command_count"])
        self.assertEqual(1, specialized["terminal_success_count"])
        self.assertEqual(1.0, specialized["execution_fulfillment_rate"])
        self.assertTrue(specialized["events"][0]["sink_observed"])
        self.assertEqual(
            1.0,
            report["scorecard"]["specialized_group_action"]["accuracy"],
        )

    def test_ambiguous_group_profile_does_not_reclassify_targets(self) -> None:
        report = evaluate_shadow(
            ground_truth=[
                self._event(
                    event_id="fixture-army-navy",
                    time_sec=10.0,
                    tool_id="army_navy_retractor",
                ),
                self._event(
                    event_id="fixture-kocher",
                    time_sec=20.0,
                    tool_id="kocher_retractor",
                ),
            ],
            decisions=[
                self._trace(
                    sequence=0,
                    time_sec=0.0,
                    layer="bed_robot_arm_group_status",
                    payload={
                        "group_id": "retraction",
                        "end_effector_profile": "retractor",
                        "state": "standby",
                    },
                ),
                self._trace(
                    sequence=1,
                    time_sec=8.0,
                    layer="bed_robot_arm_group_command",
                    payload={
                        "command_id": "group-command-1",
                        "group_id": "retraction",
                        "end_effector_profile": "retractor",
                        "operation": "retraction",
                    },
                ),
            ],
            lead_window_sec=10.0,
        )

        self.assertEqual(2, report["confirmed_ordinary_handover_count"])
        self.assertEqual(
            0,
            report["confirmed_specialized_group_action_count"],
        )
        self.assertEqual(
            2,
            report["layers"]["bt_decision"]["target_count"],
        )
        specialized = report["specialized_group_actions"]
        self.assertEqual("ambiguous_capabilities", specialized["status"])
        self.assertEqual(1, len(specialized["capabilities"]["ambiguous"]))

    def test_declared_group_target_remains_specialized_when_command_missing(
        self,
    ) -> None:
        report = evaluate_shadow(
            ground_truth=[
                self._event(
                    event_id="fixture-army-navy",
                    time_sec=10.0,
                    tool_id="army_navy_retractor",
                )
            ],
            decisions=[
                self._trace(
                    sequence=0,
                    time_sec=0.0,
                    layer="bed_robot_arm_group_status",
                    payload={
                        "group_id": "retraction",
                        "end_effector_profile": "army_navy_retractor",
                        "state": "standby",
                    },
                )
            ],
            lead_window_sec=10.0,
        )

        self.assertEqual(0, report["confirmed_ordinary_handover_count"])
        self.assertEqual(0, report["layers"]["bt_decision"]["target_count"])
        specialized = report["specialized_group_actions"]
        self.assertEqual(1, specialized["target_count"])
        self.assertEqual(0, specialized["exact_match_count"])
        self.assertEqual(1, specialized["missed_opportunity_count"])
        self.assertEqual(0.0, specialized["command_recall"])

    def test_group_activation_without_reference_target_is_unscorable(
        self,
    ) -> None:
        report = evaluate_shadow(
            ground_truth=[
                self._event(
                    event_id="fixture-scalpel",
                    time_sec=20.0,
                    tool_id="scalpel",
                )
            ],
            decisions=[
                self._trace(
                    sequence=0,
                    time_sec=0.0,
                    layer="bed_robot_arm_group_status",
                    payload={
                        "group_id": "retraction",
                        "end_effector_profile": "army_navy_retractor",
                        "state": "standby",
                    },
                ),
                self._trace(
                    sequence=1,
                    time_sec=8.0,
                    layer="bed_robot_arm_group_command",
                    payload={
                        "command_id": "group-command-1",
                        "group_id": "retraction",
                        "arm_id": "arm_1",
                        "target_tool_id": "army_navy_retractor",
                        "end_effector_profile": "army_navy_retractor",
                        "operation": "change_end_effector",
                    },
                ),
            ],
            lead_window_sec=10.0,
            tool_identity_map={"army_navy_retractor": "army_navy_retractor"},
        )

        specialized = report["specialized_group_actions"]
        self.assertEqual(
            "unscorable_reference_gap",
            specialized["status"],
        )
        self.assertEqual(0, specialized["target_count"])
        self.assertEqual(0, specialized["false_positive_command_count"])
        self.assertEqual(
            1,
            specialized["unscorable_activation_command_count"],
        )
        self.assertEqual(
            1,
            report["scorecard"]["specialized_group_action"][
                "unscorable_command_count"
            ],
        )

    def test_retired_arm_group_is_not_scored_as_clinical_suction(self) -> None:
        retired_group = "suc" + "tion"
        report = evaluate_shadow(
            ground_truth=[
                self._event(
                    event_id="fixture-yankauer",
                    time_sec=10.0,
                    tool_id="yankauer_suction",
                )
            ],
            decisions=[
                self._trace(
                    sequence=0,
                    time_sec=0.0,
                    layer="bed_robot_arm_group_status",
                    payload={
                        "group_id": retired_group,
                        "end_effector_profile": retired_group,
                        "state": "standby",
                    },
                ),
                self._trace(
                    sequence=1,
                    time_sec=8.0,
                    layer="bed_robot_arm_group_command",
                    payload={
                        "command_id": "retired-command",
                        "group_id": retired_group,
                        "end_effector_profile": retired_group,
                        "operation": retired_group + "_start",
                    },
                ),
                self._trace(
                    sequence=2,
                    time_sec=8.5,
                    layer="bt_decision",
                    payload={
                        "selected_tool": "yankauer_suction",
                        "action": "handover",
                        "confidence": 1.0,
                    },
                ),
            ],
            lead_window_sec=10.0,
        )

        self.assertEqual(1, report["confirmed_ordinary_handover_count"])
        self.assertEqual(
            0,
            report["confirmed_specialized_group_action_count"],
        )
        self.assertEqual(
            1,
            report["layers"]["bt_decision"]["outcomes"]["exact_match"],
        )
        specialized = report["specialized_group_actions"]
        self.assertEqual("no_declared_capabilities", specialized["status"])
        self.assertEqual(0, specialized["activation_command_count"])

    def test_model_raw_scores_frozen_input_time_not_publication_time(
        self,
    ) -> None:
        event = self._event(
            event_id="fixture-E0001",
            time_sec=5.0,
            tool_id="scalpel",
        )
        trace = [
            self._trace(
                sequence=0,
                time_sec=20.0,
                source_stamp_sec=2.0,
                layer="vlm_model_raw",
                payload={
                    "raw_json": json.dumps(
                        {
                            "v": "4",
                            "phase": [["P01", 0.9]],
                            "tool": [["scalpel", 0.9]],
                            "intent": ["handover", "scalpel", 0.9],
                        }
                    )
                },
            )
        ]

        report = evaluate_shadow(
            ground_truth=[event],
            decisions=trace,
            lead_window_sec=10.0,
        )

        self.assertEqual(
            1,
            report["layers"]["vlm_model_raw"]["outcomes"][
                "exact_match"
            ],
        )
        self.assertEqual(
            1.0,
            report["scorecard"]["model_raw_intent_recognition"][
                "accuracy"
            ],
        )
        model_event = report["layers"]["vlm_model_raw"]["events"][0]
        self.assertEqual(3.0, model_event["lead_time_sec"])

    def test_catalog_tool_absent_from_events_is_wrong_not_impossible(self) -> None:
        identity_map = load_tool_identity_map(
            ROOT / "annotations/observable_tool_events/catalogs/tools.yaml"
        )
        event = self._event(
            event_id="fixture-E0001",
            time_sec=5.0,
            tool_id="scalpel",
        )
        trace = [
            self._trace(
                sequence=0,
                time_sec=2.0,
                layer="vlm_raw",
                payload={
                    "raw_json": json.dumps(
                        {
                            "v": "4",
                            "phase": [["P01", 0.9]],
                            "tool": [["T04", 0.9]],
                            "intent": ["handover", "T04", 0.9],
                        }
                    )
                },
            )
        ]
        report = evaluate_shadow(
            ground_truth=[event],
            decisions=trace,
            lead_window_sec=10.0,
            tool_identity_map=identity_map,
        )
        outcomes = report["layers"]["vlm_raw"]["outcomes"]
        self.assertEqual(1, outcomes["wrong_prediction"])
        self.assertEqual(0, outcomes["unsafe_or_impossible"])

    def test_ranked_tool_is_prediction_when_intent_names_different_tool(self) -> None:
        prediction = _vlm_prediction(
            self._trace(
                sequence=0,
                time_sec=2.0,
                layer="vlm_raw",
                payload={
                    "raw_json": json.dumps(
                        {
                            "v": "4",
                            "phase": [["P03", 0.9]],
                            "tool": [["T05", 0.85]],
                            "intent": ["handover", "T02", 0.95],
                        }
                    )
                },
            )
        )

        self.assertEqual("T05", prediction["tool_id"])
        self.assertEqual("predict_tool", prediction["action"])
        self.assertEqual(0.85, prediction["tool_confidence"])
        self.assertEqual("predicted_tool", prediction["prediction_source"])

    def test_matching_handover_intent_is_preserved_as_observed_request(self) -> None:
        prediction = _vlm_prediction(
            self._trace(
                sequence=0,
                time_sec=2.0,
                layer="vlm_raw",
                payload={
                    "raw_json": json.dumps(
                        {
                            "v": "4",
                            "phase": [["P03", 0.9]],
                            "tool": [["T05", 1.0]],
                            "intent": ["handover", "T05", 0.95],
                        }
                    )
                },
            )
        )

        self.assertEqual("T05", prediction["tool_id"])
        self.assertEqual("handover", prediction["action"])
        self.assertEqual(1.0, prediction["tool_confidence"])
        self.assertEqual("explicit_request", prediction["prediction_source"])

    def test_reducer_explicit_request_is_a_request_backed_system_choice(self) -> None:
        identity_map = load_tool_identity_map(
            ROOT / "annotations/observable_tool_events/catalogs/tools.yaml"
        )
        report = evaluate_shadow(
            ground_truth=[
                self._event(
                    event_id="fixture-E0001",
                    time_sec=5.0,
                    tool_id="scalpel",
                )
            ],
            decisions=[
                self._trace(
                    sequence=0,
                    time_sec=2.0,
                    layer="reducer_fused",
                    payload={
                        "filtered_phase": "P01",
                        "explicit_request_tool": "T01",
                        "predicted_tool": "",
                    },
                )
            ],
            lead_window_sec=10.0,
            tool_identity_map=identity_map,
        )
        layer = report["layers"]["reducer_fused"]
        self.assertEqual(1, layer["outcomes"]["exact_match"])
        self.assertEqual(1, layer["request_backed_exact_count"])
        self.assertEqual(0, layer["anticipatory_exact_count"])
        score = report["scorecard"]["next_tool_prediction"]["layers"][
            "reducer_fused"
        ]
        self.assertEqual(1.0, score["combined_action_selection_accuracy"])
        self.assertEqual(0.0, score["anticipatory_target_recall"])
        self.assertEqual(1.0, score["request_backed_target_recall"])

    def test_interval_parser_accepts_fenced_json_and_multiple_spans(self) -> None:
        self.assertEqual(
            [(1.25, 3.5), (9.0, 11.0)],
            extract_interval_pairs("```json\n[[1.25, 3.5], [9, 11]]\n```"),
        )

    def test_interval_parser_rejects_non_interval_json(self) -> None:
        with self.assertRaises(ValueError):
            extract_interval_pairs('{"start": 1, "end": 2}')

    def test_timelens2_output_remains_proposed_and_unknown(self) -> None:
        candidates = normalize_candidates(
            raw_records=[
                {
                    "query_id": "handover",
                    "candidate_start_sec": 4.0,
                    "candidate_end_sec": 5.2,
                    "confidence": 0.91,
                    "model_version": "fixture",
                }
            ],
            query_spec=QUERY_SPEC,
            case_id="0704_5",
            duration_sec=163.1,
        )
        self.assertEqual(1, len(candidates))
        self.assertEqual("proposed", candidates[0]["review_status"])
        self.assertEqual("unknown", candidates[0]["from"]["holder"])
        self.assertEqual("unknown", candidates[0]["to"]["location"])

    def test_timelens2_missing_confidence_is_not_fabricated(self) -> None:
        candidates = normalize_candidates(
            raw_records=[
                {
                    "query_id": "handover",
                    "candidate_start_sec": 4.0,
                    "candidate_end_sec": 5.2,
                    "model_version": "fixture",
                }
            ],
            query_spec=QUERY_SPEC,
            case_id="0704_5",
            duration_sec=163.1,
        )
        self.assertNotIn("confidence", candidates[0]["proposal"])

    def test_proposals_do_not_create_shadow_metrics(self) -> None:
        report = evaluate_shadow(
            ground_truth=[
                {
                    "review_status": "proposed",
                    "event_type": "tool_transfer",
                    "from": {"holder": "scrub_nurse", "location": "right_hand"},
                    "to": {"holder": "surgeon", "location": "left_hand"},
                }
            ],
            decisions=[],
            lead_window_sec=10.0,
        )
        self.assertEqual("awaiting_confirmed_reference", report["status"])
        self.assertIsNone(report["metrics"])

    def test_initial_state_alone_does_not_complete_shadow_evaluation(self) -> None:
        report = evaluate_shadow(
            ground_truth=[
                {
                    "review_status": "confirmed",
                    "event_type": "initial_state",
                    "from": None,
                    "to": {"holder": "none", "location": "mayo_stand"},
                }
            ],
            decisions=[],
            lead_window_sec=10.0,
        )
        self.assertEqual("awaiting_handover_ground_truth", report["status"])
        self.assertIsNone(report["metrics"])

    def test_exact_match_uses_only_prior_shadow_decision(self) -> None:
        event = {
            "review_status": "confirmed",
            "event_id": "fixture-E0001",
            "event_type": "tool_transfer",
            "time_sec": 20.0,
            "tool": {"id": "scalpel"},
            "from": {"holder": "scrub_nurse", "location": "right_hand"},
            "to": {"holder": "surgeon", "location": "left_hand"},
        }
        report = evaluate_shadow(
            ground_truth=[event],
            decisions=[
                {
                    "time_sec": 18.0,
                    "predicted_tool_id": "scalpel",
                    "predicted_action": "handover",
                    "safety_status": "safe",
                },
                {
                    "time_sec": 21.0,
                    "predicted_tool_id": "bovie",
                    "predicted_action": "handover",
                    "safety_status": "safe",
                },
            ],
            lead_window_sec=10.0,
        )
        self.assertEqual(1, report["metrics"]["exact_match_count"])
        self.assertEqual(2.0, report["events"][0]["lead_time_sec"])

    def test_assistant_reference_provenance_is_reported(self) -> None:
        event = {
            "review_status": "confirmed",
            "label_origin": "assistant_video_adjudication",
            "review": {"reviewer_kind": "ai_assistant"},
            "event_id": "fixture-E0001",
            "event_type": "tool_transfer",
            "time_sec": 20.0,
            "tool": {"id": "scalpel"},
            "from": {
                "holder": "scrub_nurse",
                "location": "hand_unspecified",
            },
            "to": {"holder": "surgeon", "location": "hand_unspecified"},
        }
        report = evaluate_shadow(
            ground_truth=[event],
            decisions=[],
            lead_window_sec=10.0,
        )
        self.assertEqual(
            "assistant_video_adjudication",
            report["reference_authority"],
        )
        self.assertEqual(
            {"ai_assistant": 1},
            report["confirmed_reviewer_kind_counts"],
        )
        self.assertEqual(
            "assistant_video_adjudication",
            report["events"][0]["target_label_origin"],
        )

    def test_one_prediction_episode_cannot_score_two_targets(self) -> None:
        ground_truth = [
            self._event(event_id="fixture-E0001", time_sec=20.0, tool_id="adson"),
            self._event(event_id="fixture-E0002", time_sec=22.0, tool_id="adson"),
        ]
        decisions = [
            {
                "time_sec": 18.0,
                "predicted_tool_id": "adson",
                "predicted_action": "handover",
            },
            {
                "time_sec": 19.0,
                "predicted_tool_id": "adson",
                "predicted_action": "handover",
            },
            {
                "time_sec": 21.0,
                "predicted_tool_id": "adson",
                "predicted_action": "handover",
            },
        ]
        report = evaluate_shadow(
            ground_truth=ground_truth,
            decisions=decisions,
            lead_window_sec=10.0,
        )
        self.assertEqual(1, report["metrics"]["exact_match_count"])
        self.assertEqual(1, report["metrics"]["missed_opportunity_count"])

    def test_post_event_prediction_is_never_used_for_target(self) -> None:
        report = evaluate_shadow(
            ground_truth=[
                self._event(
                    event_id="fixture-E0001",
                    time_sec=20.0,
                    tool_id="scalpel",
                )
            ],
            decisions=[
                {
                    "time_sec": 20.0,
                    "predicted_tool_id": "scalpel",
                    "predicted_action": "handover",
                },
                {
                    "time_sec": 21.0,
                    "predicted_tool_id": "scalpel",
                    "predicted_action": "handover",
                },
            ],
            lead_window_sec=10.0,
        )
        self.assertEqual(0, report["metrics"]["exact_match_count"])
        self.assertEqual(1, report["metrics"]["missed_opportunity_count"])

    def test_request_backed_action_uses_bounded_reaction_lag(self) -> None:
        event = self._event(
            event_id="fixture-E0001",
            time_sec=20.0,
            tool_id="scalpel",
        )
        trace = [
            self._trace(
                sequence=0,
                time_sec=20.4,
                layer="skill_command",
                payload={
                    "instrument_id": "scalpel",
                    "action": "pick_up_and_handover",
                    "request_generation": 1,
                },
            )
        ]

        report = evaluate_shadow(
            ground_truth=[event],
            decisions=trace,
            lead_window_sec=10.0,
            request_reaction_window_sec=1.0,
        )

        layer = report["layers"]["skill_command"]
        scored = layer["events"][0]
        self.assertEqual(1, layer["outcomes"]["exact_match"])
        self.assertEqual(1, layer["request_backed_exact_count"])
        self.assertEqual(1, layer["request_reactive_exact_count"])
        self.assertEqual("request_reactive", scored["match_timing"])
        self.assertEqual(0.4, scored["request_reaction_lag_sec"])
        self.assertIsNone(scored["lead_time_sec"])

    def test_request_reaction_window_does_not_admit_stale_action(self) -> None:
        event = self._event(
            event_id="fixture-E0001",
            time_sec=20.0,
            tool_id="scalpel",
        )
        trace = [
            self._trace(
                sequence=0,
                time_sec=22.1,
                layer="skill_command",
                payload={
                    "instrument_id": "scalpel",
                    "action": "pick_up_and_handover",
                    "request_generation": 1,
                },
            )
        ]

        report = evaluate_shadow(
            ground_truth=[event],
            decisions=trace,
            lead_window_sec=10.0,
            request_reaction_window_sec=2.0,
        )

        layer = report["layers"]["skill_command"]
        self.assertEqual(0, layer["outcomes"]["exact_match"])
        self.assertEqual(1, layer["outcomes"]["missed_opportunity"])
        self.assertEqual(1, layer["false_positive_count"])

    def test_reactive_wrong_tool_is_left_for_later_matching_target(self) -> None:
        events = [
            self._event(
                event_id="fixture-E0001",
                time_sec=20.0,
                tool_id="scalpel",
            ),
            self._event(
                event_id="fixture-E0002",
                time_sec=21.5,
                tool_id="bovie",
            ),
        ]
        trace = [
            self._trace(
                sequence=0,
                time_sec=20.5,
                layer="skill_command",
                payload={
                    "instrument_id": "bovie",
                    "action": "pick_up_and_handover",
                    "request_generation": 1,
                },
            )
        ]

        report = evaluate_shadow(
            ground_truth=events,
            decisions=trace,
            lead_window_sec=10.0,
            request_reaction_window_sec=2.0,
        )

        layer = report["layers"]["skill_command"]
        self.assertEqual("missed_opportunity", layer["events"][0]["outcome"])
        self.assertEqual("exact_match", layer["events"][1]["outcome"])
        self.assertEqual("bovie", layer["events"][1]["predicted_tool_id"])

    def test_trace_reports_each_decision_layer_separately(self) -> None:
        event = self._event(
            event_id="fixture-E0001",
            time_sec=20.0,
            tool_id="bovie",
        )
        trace = [
            self._trace(
                sequence=0,
                time_sec=17.0,
                layer="vlm_raw",
                payload={
                    "raw_json": json.dumps(
                        {
                            "v": "4",
                            "phase": [["P01", 0.8]],
                            "tool": [["bovie", 0.9]],
                            "intent": ["none", "", 0.0],
                        }
                    )
                },
            ),
            self._trace(
                sequence=1,
                time_sec=17.0,
                layer="reducer_fused",
                payload={
                    "predicted_tool": "bovie",
                    "predicted_tool_confidence": 0.88,
                    "filtered_phase": "P01",
                    "phase_confidence": 0.75,
                },
            ),
            self._trace(
                sequence=2,
                time_sec=18.0,
                layer="bt_decision",
                payload={
                    "selected_tool": "adson",
                    "action": "pick_up_and_handover",
                },
            ),
            self._trace(
                sequence=3,
                time_sec=19.0,
                layer="skill_command",
                payload={
                    "instrument_id": "adson",
                    "action": "pick_up_and_handover",
                },
            ),
        ]
        report = evaluate_shadow(
            ground_truth=[event],
            decisions=trace,
            lead_window_sec=10.0,
        )
        self.assertEqual(
            1,
            report["layers"]["vlm_raw"]["outcomes"]["exact_match"],
        )
        self.assertEqual(
            1,
            report["layers"]["reducer_fused"]["outcomes"]["exact_match"],
        )
        self.assertEqual(
            1,
            report["layers"]["bt_decision"]["outcomes"][
                "unsafe_or_impossible"
            ],
        )
        self.assertEqual(
            1,
            report["layers"]["skill_command"]["outcomes"][
                "unsafe_or_impossible"
            ],
        )
        self.assertEqual("bt_decision", report["primary_layer"])

    def test_discrete_bt_and_skill_events_remain_valid_for_full_lead_window(self) -> None:
        event = self._event(
            event_id="fixture-E0001",
            time_sec=20.0,
            tool_id="bovie",
        )
        trace = [
            self._trace(
                sequence=0,
                time_sec=14.0,
                layer="bt_decision",
                payload={
                    "selected_tool": "bovie",
                    "action": "pick_up_and_handover",
                },
            ),
            self._trace(
                sequence=1,
                time_sec=14.0,
                layer="skill_command",
                payload={
                    "instrument_id": "bovie",
                    "action": "pick_up_and_handover",
                },
            ),
        ]

        report = evaluate_shadow(
            ground_truth=[event],
            decisions=trace,
            lead_window_sec=10.0,
            max_prediction_age_sec=3.0,
        )

        self.assertEqual(
            1,
            report["layers"]["bt_decision"]["outcomes"]["exact_match"],
        )
        self.assertEqual(
            1,
            report["layers"]["skill_command"]["outcomes"]["exact_match"],
        )

    def test_request_generation_keeps_adjacent_same_tool_commands_distinct(self) -> None:
        events = [
            self._event(
                event_id="fixture-E0001",
                time_sec=12.0,
                tool_id="army_navy_retractor",
            ),
            self._event(
                event_id="fixture-E0002",
                time_sec=15.0,
                tool_id="army_navy_retractor",
            ),
        ]
        trace = [
            self._trace(
                sequence=0,
                time_sec=10.0,
                layer="skill_command",
                payload={
                    "instrument_id": "army_navy_retractor",
                    "action": "pick_up_and_handover",
                    "request_generation": 8,
                },
            ),
            self._trace(
                sequence=1,
                time_sec=10.1,
                layer="skill_command",
                payload={
                    "instrument_id": "army_navy_retractor",
                    "action": "pick_up_and_handover",
                    "request_generation": 9,
                },
            ),
        ]

        report = evaluate_shadow(
            ground_truth=events,
            decisions=trace,
            lead_window_sec=10.0,
        )

        layer = report["layers"]["skill_command"]
        self.assertEqual(2, layer["prediction_episode_count"])
        self.assertEqual(2, layer["outcomes"]["exact_match"])
        self.assertEqual(0, layer["outcomes"]["missed_opportunity"])
        self.assertEqual(2, layer["request_backed_exact_count"])

    def test_recovery_is_audited_separately_from_handover_accuracy(self) -> None:
        initial = {
            "review_status": "confirmed",
            "label_origin": "human_video_review",
            "event_id": "fixture-E0000",
            "event_type": "initial_state",
            "time_sec": 0.0,
            "tool": {
                "id": "bovie",
                "name": "bovie",
                "instance_id": "fixture-bovie-1",
            },
            "from": None,
            "to": {"holder": "none", "location": "mayo_stand"},
            "visibility": "clear",
        }
        handover = self._event(
            event_id="fixture-E0001",
            time_sec=20.0,
            tool_id="bovie",
        )
        handover["tool"]["instance_id"] = "fixture-bovie-1"
        trace = [
            self._trace(
                sequence=0,
                time_sec=10.0,
                layer="skill_command",
                payload={
                    "instrument_id": "bovie",
                    "action": "retrieve_from_mayo",
                },
            )
        ]

        report = evaluate_shadow(
            ground_truth=[initial, handover],
            decisions=trace,
            lead_window_sec=10.0,
        )

        layer = report["layers"]["skill_command"]
        self.assertEqual(0, layer["false_positive_count"])
        self.assertEqual(0, layer["prediction_episode_count"])
        self.assertEqual(1, layer["non_handover_action_episode_count"])
        audit = report["recovery_audit"]
        self.assertEqual(1, audit["recovery_action_count"])
        self.assertEqual({"suspicious": 1}, audit["severity_counts"])
        self.assertEqual(
            "reuse_observed_within_guard_window",
            audit["actions"][0]["outcome"],
        )

    def test_recovery_from_observably_held_tool_is_a_blocker(self) -> None:
        initial = {
            "review_status": "confirmed",
            "label_origin": "human_video_review",
            "event_id": "fixture-E0000",
            "event_type": "initial_state",
            "time_sec": 0.0,
            "tool": {
                "id": "bovie",
                "name": "bovie",
                "instance_id": "fixture-bovie-1",
            },
            "from": None,
            "to": {"holder": "none", "location": "mayo_stand"},
            "visibility": "clear",
        }
        bovie_handover = self._event(
            event_id="fixture-E0001",
            time_sec=5.0,
            tool_id="bovie",
        )
        bovie_handover["tool"]["instance_id"] = "fixture-bovie-1"
        later_handover = self._event(
            event_id="fixture-E0002",
            time_sec=20.0,
            tool_id="scalpel",
        )
        trace = [
            self._trace(
                sequence=0,
                time_sec=10.0,
                layer="skill_command",
                payload={
                    "instrument_id": "bovie",
                    "action": "retrieve_from_mayo",
                },
            )
        ]

        report = evaluate_shadow(
            ground_truth=[initial, bovie_handover, later_handover],
            decisions=trace,
            lead_window_sec=10.0,
        )

        audit = report["recovery_audit"]
        self.assertEqual({"blocker": 1}, audit["severity_counts"])
        self.assertEqual(
            "observable_state_conflict",
            audit["actions"][0]["outcome"],
        )

    def test_explicit_reducer_request_is_discrete_not_a_stale_prediction(self) -> None:
        event = self._event(
            event_id="fixture-E0001",
            time_sec=20.0,
            tool_id="bovie",
        )
        trace = [
            self._trace(
                sequence=0,
                time_sec=14.0,
                layer="reducer_fused",
                payload={
                    "explicit_request_tool": "bovie",
                    "predicted_tool": "",
                    "filtered_phase": "P01",
                },
            )
        ]

        report = evaluate_shadow(
            ground_truth=[event],
            decisions=trace,
            lead_window_sec=10.0,
            max_prediction_age_sec=3.0,
        )

        reducer = report["layers"]["reducer_fused"]
        self.assertEqual(1, reducer["outcomes"]["exact_match"])
        self.assertEqual(1, reducer["request_backed_exact_count"])

    def test_stable_lead_requires_repeated_prediction_before_target(self) -> None:
        event = self._event(
            event_id="fixture-E0001",
            time_sec=20.0,
            tool_id="bovie",
        )
        trace = [
            self._trace(
                sequence=index,
                time_sec=time_sec,
                layer="vlm_raw",
                payload={
                    "raw_json": json.dumps(
                        {
                            "v": "4",
                            "phase": [["P01", 0.8]],
                            "tool": [["bovie", 0.9]],
                            "intent": ["none", "", 0.0],
                        }
                    )
                },
            )
            for index, time_sec in enumerate((14.0, 15.0, 16.0, 17.0, 18.0))
        ]
        report = evaluate_shadow(
            ground_truth=[event],
            decisions=trace,
            lead_window_sec=10.0,
            stable_sec=3.0,
        )
        layer = report["layers"]["vlm_raw"]
        self.assertEqual(1, layer["stable_exact_count"])
        self.assertEqual(3.0, layer["events"][0]["stable_correct_lead_sec"])

    def test_phase_is_explicitly_unavailable_without_interval_truth(self) -> None:
        report = evaluate_shadow(
            ground_truth=[
                self._event(
                    event_id="fixture-E0001",
                    time_sec=20.0,
                    tool_id="bovie",
                )
            ],
            decisions=[],
            lead_window_sec=10.0,
        )
        self.assertEqual("not_available", report["phase"]["status"])

    def test_phase_raw_and_reducer_metrics_use_confirmed_intervals(self) -> None:
        trace = [
            self._trace(
                sequence=0,
                time_sec=5.0,
                layer="vlm_raw",
                payload={
                    "raw_json": json.dumps(
                        {
                            "v": "4",
                            "phase": [["P01", 0.9]],
                            "tool": [["bovie", 0.8]],
                            "intent": ["none", "", 0.0],
                        }
                    )
                },
            ),
            self._trace(
                sequence=1,
                time_sec=6.0,
                layer="reducer_fused",
                payload={
                    "filtered_phase": "P02",
                    "phase_confidence": 0.9,
                    "predicted_tool": "bovie",
                },
            ),
        ]
        report = evaluate_shadow(
            ground_truth=[
                self._event(
                    event_id="fixture-E0001",
                    time_sec=20.0,
                    tool_id="bovie",
                )
            ],
            decisions=trace,
            lead_window_sec=20.0,
            phase_ground_truth=[
                {
                    "phase_id": "P01",
                    "start_sec": 0.0,
                    "end_sec": 10.0,
                    "review_status": "confirmed",
                }
            ],
        )
        self.assertEqual(1.0, report["phase"]["layers"]["vlm_raw"]["accuracy"])
        self.assertEqual(0.0, report["phase"]["layers"]["reducer_fused"]["accuracy"])

    def test_provisional_phase_boundaries_can_be_scored_explicitly(self) -> None:
        trace = [
            self._trace(
                sequence=0,
                time_sec=2.0,
                layer="vlm_raw",
                payload={
                    "raw_json": json.dumps(
                        {
                            "v": "4",
                            "phase": [["P03", 0.9]],
                            "tool": [["bovie", 0.8]],
                            "intent": ["none", "", 0.0],
                        }
                    )
                },
            ),
            self._trace(
                sequence=1,
                time_sec=12.0,
                layer="reducer_fused",
                payload={
                    "filtered_phase": "P04",
                    "phase_confidence": 0.9,
                    "predicted_tool": "bovie",
                },
            ),
        ]
        report = evaluate_shadow(
            ground_truth=[
                self._event(
                    event_id="fixture-E0001",
                    time_sec=20.0,
                    tool_id="bovie",
                )
            ],
            decisions=trace,
            lead_window_sec=20.0,
            phase_ground_truth=[
                {
                    "event_id": "fixture-PH0001",
                    "event_type": "phase_start",
                    "phase_id": "P03",
                    "time_sec": 0.0,
                    "review_status": "ambiguous",
                },
                {
                    "event_id": "fixture-PH0002",
                    "event_type": "phase_start",
                    "phase_id": "P04",
                    "time_sec": 10.0,
                    "review_status": "ambiguous",
                },
            ],
            allow_provisional_phase=True,
        )

        self.assertEqual("complete_provisional", report["phase"]["status"])
        self.assertEqual(
            "provisional_ambiguous",
            report["phase"]["reference_quality"],
        )
        self.assertEqual(1.0, report["phase"]["layers"]["vlm_raw"]["accuracy"])
        self.assertEqual(
            1.0,
            report["phase"]["layers"]["reducer_fused"]["accuracy"],
        )

    def test_scorecard_separates_intent_dt_and_command_fulfillment(self) -> None:
        event = self._event(
            event_id="fixture-E0001",
            time_sec=5.0,
            tool_id="bovie",
        )
        trace = [
            self._trace(
                sequence=0,
                time_sec=4.0,
                layer="vlm_raw",
                payload={
                    "raw_json": json.dumps(
                        {
                            "v": "4",
                            "phase": [["P03", 0.9]],
                            "tool": [["bovie", 0.8]],
                            "intent": ["handover", "bovie", 0.9],
                        }
                    )
                },
            ),
            self._trace(
                sequence=1,
                time_sec=5.2,
                layer="reducer_fused",
                payload={
                    "filtered_phase": "P03",
                    "instrument_states": [
                        {
                            "instrument_id": "bovie",
                            "instance_id": "bovie#1",
                            "owner": "surgeon",
                            "location_type": "surgeon_hand",
                            "location_id": "surgeon_hand",
                            "lifecycle_stage": "surgeon_owned",
                        }
                    ],
                },
            ),
            self._trace(
                sequence=2,
                time_sec=4.2,
                layer="skill_command",
                payload={
                    "command_id": "cmd-1",
                    "instrument_id": "bovie",
                    "action": "pick_up_and_handover",
                },
            ),
            {
                "layer": "skill_event",
                "ros_time_sec": 4.8,
                "payload": {
                    "event_type": "ToolHandoverCompleted",
                    "detail_json": json.dumps({"command_id": "cmd-1"}),
                },
            },
            {
                "layer": "skill_status",
                "ros_time_sec": 4.9,
                "payload": {
                    "command_id": "cmd-1",
                    "state": "completed",
                    "success": True,
                },
            },
        ]
        report = evaluate_shadow(
            ground_truth=[event],
            decisions=trace,
            lead_window_sec=10.0,
            tool_inventory={"bovie": 1},
        )

        scorecard = report["scorecard"]
        self.assertEqual(
            1.0,
            scorecard["intent_recognition"]["accuracy"],
        )
        self.assertEqual(
            1.0,
            scorecard["dt_tool_management"]["endpoint_accuracy"],
        )
        self.assertEqual(
            1.0,
            scorecard["dt_tool_management"]["instance_inventory_accuracy"],
        )
        self.assertEqual(
            "complete_declared_inventory_conservation",
            scorecard["dt_tool_management"]["instance_inventory_status"],
        )
        self.assertEqual(
            1.0,
            scorecard["command_fulfillment"]["fulfillment_rate"],
        )

    def test_dt_endpoint_scoring_respects_state_mask(self) -> None:
        event = self._event(
            event_id="fixture-E0001",
            time_sec=5.0,
            tool_id="bovie",
        )
        trace = [
            self._trace(
                sequence=0,
                time_sec=5.2,
                layer="reducer_fused",
                payload={
                    "instrument_states": [
                        {
                            "instrument_id": "bovie",
                            "instance_id": "bovie#1",
                            "owner": "surgeon",
                            "location_type": "surgeon_hand",
                            "location_id": "surgeon_hand",
                            "lifecycle_stage": "surgeon_owned",
                        }
                    ]
                },
            )
        ]
        report = evaluate_shadow(
            ground_truth=[event],
            decisions=trace,
            lead_window_sec=10.0,
            tool_inventory={"bovie": 1},
            evaluation_mask={
                "schema": "taskplanner.evaluation_masks.v2",
                "case_id": "",
                "default_metric_eligibility": {
                    "action": True,
                    "latency": True,
                    "state": False,
                    "physical": False,
                    "reuse": False,
                },
            },
        )

        dt_score = report["scorecard"]["dt_tool_management"]
        self.assertEqual(0, dt_score["evaluated_count"])
        self.assertIsNone(dt_score["endpoint_accuracy"])
        self.assertEqual(
            1,
            dt_score["skipped_counts"]["event_masked_state"],
        )
        self.assertEqual(1.0, dt_score["instance_inventory_accuracy"])

    def test_flat_v5_transfer_is_scored_without_nested_tool_state(self) -> None:
        event = {
            "review_status": "confirmed",
            "event_id": "fixture-flat-E0001",
            "event_type": "tool_transfer",
            "time_sec": 20.0,
            "tool": "bovie",
            "from": "scrub_nurse",
            "to": "surgeon",
        }

        report = evaluate_shadow(
            ground_truth=[event],
            decisions=[
                {
                    "time_sec": 18.0,
                    "predicted_tool_id": "bovie",
                    "predicted_action": "handover",
                }
            ],
            lead_window_sec=10.0,
        )

        self.assertEqual(1, report["confirmed_handover_count"])
        self.assertEqual(1, report["metrics"]["exact_match_count"])
        self.assertEqual("bovie", report["events"][0]["target_tool_id"])

    def test_phase_and_ambiguous_transfer_never_become_handover_truth(self) -> None:
        handover = {
            "review_status": "confirmed",
            "event_id": "fixture-flat-E0001",
            "event_type": "tool_transfer",
            "time_sec": 20.0,
            "tool": "bovie",
            "from": "scrub_nurse",
            "to": "surgeon",
        }
        ambiguous = {
            **handover,
            "review_status": "ambiguous",
            "event_id": "fixture-flat-E0002",
            "time_sec": 30.0,
        }
        phase = {
            "review_status": "confirmed",
            "event_id": "fixture-phase-P01",
            "event_type": "phase_start",
            "phase_id": "P01",
            "time_sec": 0.0,
        }

        report = evaluate_shadow(
            ground_truth=[phase, handover, ambiguous],
            decisions=[],
            lead_window_sec=10.0,
        )

        self.assertEqual(1, report["confirmed_handover_count"])
        self.assertEqual(1, report["confirmed_phase_start_count"])
        self.assertEqual(
            1,
            report["excluded_ground_truth_counts"]["ambiguous"],
        )
        self.assertEqual(1, report["state_audit"]["state_event_count"])

    def test_evaluation_mask_roles_intervals_and_cutoff_bound_action_window(
        self,
    ) -> None:
        def flat_event(event_id: str, time_sec: float, tool: str) -> dict:
            return {
                "review_status": "confirmed",
                "event_id": event_id,
                "event_type": "tool_transfer",
                "time_sec": time_sec,
                "tool": tool,
                "from": "scrub_nurse",
                "to": "surgeon",
            }

        mask = {
            "schema": "taskplanner.evaluation_masks.v2",
            "case_id": "fixture",
            "default_metric_eligibility": {
                "action": False,
                "latency": False,
                "state": False,
                "physical": False,
                "reuse": False,
            },
            "event_roles": [
                {"event_id": "target", "role": "action_target"},
                {
                    "event_id": "compound",
                    "role": "compound_action_substep",
                },
                {"event_id": "after-cutoff", "role": "action_target"},
            ],
            "interval_masks": [
                {
                    "mask_id": "compound-window",
                    "start_sec": 25.0,
                    "end_sec": 35.0,
                    "metric_eligibility": {
                        "action": False,
                        "latency": False,
                    },
                    "reason": "compound transition is context only",
                }
            ],
            "cutoffs": {"action_and_next_tool_end_sec": 100.0},
        }

        report = evaluate_shadow(
            ground_truth=[
                flat_event("target", 20.0, "bovie"),
                flat_event("compound", 30.0, "scalpel"),
                flat_event("after-cutoff", 140.0, "mosquito_forceps"),
            ],
            decisions=[
                {
                    "time_sec": 18.0,
                    "predicted_tool_id": "bovie",
                    "predicted_action": "handover",
                },
                {
                    "time_sec": 28.0,
                    "predicted_tool_id": "scalpel",
                    "predicted_action": "handover",
                },
                {
                    "time_sec": 139.0,
                    "predicted_tool_id": "mosquito_forceps",
                    "predicted_action": "handover",
                },
            ],
            lead_window_sec=10.0,
            evaluation_mask=mask,
        )

        self.assertEqual(1, report["confirmed_handover_count"])
        self.assertEqual(2, report["masked_confirmed_handover_count"])
        self.assertEqual(1, report["metrics"]["exact_match_count"])
        self.assertEqual(0, report["metrics"]["false_positive_count"])
        self.assertEqual(1, report["evaluation_mask"]["interval_mask_count"])

    def test_latency_mask_keeps_action_match_but_suppresses_lead_metric(
        self,
    ) -> None:
        event = {
            "review_status": "confirmed",
            "event_id": "target",
            "event_type": "tool_transfer",
            "time_sec": 20.0,
            "tool": "bovie",
            "from": "scrub_nurse",
            "to": "surgeon",
        }
        mask = {
            "schema": "taskplanner.evaluation_masks.v1",
            "event_roles": [
                {"event_id": "target", "role": "action_target"}
            ],
            "interval_masks": [
                {
                    "mask_id": "latency-gap",
                    "start_sec": 17.0,
                    "end_sec": 19.0,
                    "metric_eligibility": {"latency": False},
                }
            ],
        }

        report = evaluate_shadow(
            ground_truth=[event],
            decisions=[
                {
                    "time_sec": 18.0,
                    "predicted_tool_id": "bovie",
                    "predicted_action": "handover",
                }
            ],
            lead_window_sec=10.0,
            evaluation_mask=mask,
        )

        self.assertEqual(1, report["metrics"]["exact_match_count"])
        self.assertIsNone(report["events"][0]["lead_time_sec"])
        self.assertEqual(
            0,
            report["layers"]["bt_decision"]["first_correct_lead_sec"]["count"],
        )

    def test_type_level_discontinuity_makes_physical_and_reuse_not_scorable(
        self,
    ) -> None:
        def flat_event(
            event_id: str,
            time_sec: float,
            tool: str,
            source: str,
            target: str,
        ) -> dict:
            return {
                "review_status": "confirmed",
                "event_id": event_id,
                "event_type": "tool_transfer",
                "time_sec": time_sec,
                "tool": tool,
                "from": source,
                "to": target,
            }

        mask = {
            "schema": "taskplanner.evaluation_masks.v1",
            "event_roles": [
                {
                    "event_id": "bovie-handover",
                    "role": "state_observation_only",
                    "metric_eligibility": {
                        "state": True,
                        "physical": True,
                        "reuse": True,
                    },
                },
                {
                    "event_id": "bovie-pickup",
                    "role": "state_observation_only",
                    "metric_eligibility": {
                        "state": True,
                        "physical": True,
                        "reuse": True,
                    },
                },
                {"event_id": "scalpel-target", "role": "action_target"},
            ],
            "tool_metric_scopes": [
                {
                    "tool": "bovie",
                    "instance_resolution": "unresolved_multiple_instances",
                    "state": False,
                    "physical": False,
                    "reuse": False,
                    "reason": "fixture has unresolved physical instances",
                }
            ],
        }
        report = evaluate_shadow(
            ground_truth=[
                flat_event(
                    "bovie-handover",
                    5.0,
                    "bovie",
                    "scrub_nurse",
                    "surgeon",
                ),
                flat_event(
                    "bovie-pickup",
                    10.0,
                    "bovie",
                    "mayo_stand",
                    "scrub_nurse",
                ),
                flat_event(
                    "scalpel-target",
                    20.0,
                    "scalpel",
                    "scrub_nurse",
                    "surgeon",
                ),
            ],
            decisions=[
                self._trace(
                    sequence=0,
                    time_sec=12.0,
                    layer="skill_command",
                    payload={
                        "instrument_id": "bovie",
                        "action": "retrieve_from_mayo",
                    },
                )
            ],
            lead_window_sec=10.0,
            evaluation_mask=mask,
        )

        self.assertEqual(
            "not_scorable_type_instance_assumption",
            report["state_audit"]["status"],
        )
        self.assertEqual(
            ["bovie"],
            report["state_audit"]["duplicate_type_instance_tools"],
        )
        self.assertEqual(
            "unresolved_multiple_instances",
            report["state_audit"]["type_instance_assumptions"][0][
                "instance_resolution"
            ],
        )
        self.assertEqual(
            0,
            report["state_audit"]["physical_scorable_event_count"],
        )
        self.assertEqual(
            0,
            report["state_audit"]["reuse_scorable_event_count"],
        )
        self.assertEqual(
            "not_scorable",
            report["recovery_audit"]["actions"][0]["severity"],
        )
        self.assertEqual(
            "not_scorable_reference",
            report["recovery_audit"]["actions"][0]["outcome"],
        )

    def test_unknown_scoring_role_fails_closed(self) -> None:
        event = {
            "review_status": "confirmed",
            "event_id": "unknown-role",
            "event_type": "tool_transfer",
            "time_sec": 20.0,
            "tool": "bovie",
            "from": "scrub_nurse",
            "to": "surgeon",
        }
        mask = {
            "schema": "taskplanner.evaluation_masks.v1",
            "event_roles": [
                {
                    "event_id": "unknown-role",
                    "role": "future_role",
                    "metric_eligibility": {
                        "action": True,
                        "latency": True,
                        "state": True,
                        "physical": True,
                        "reuse": True,
                    },
                }
            ],
        }

        report = evaluate_shadow(
            ground_truth=[event],
            decisions=[],
            lead_window_sec=10.0,
            evaluation_mask=mask,
        )

        self.assertEqual("not_scorable_action_masked", report["status"])
        self.assertEqual(0, report["confirmed_handover_count"])
        self.assertTrue(report["evaluation_mask"]["issues"])


if __name__ == "__main__":
    unittest.main()
