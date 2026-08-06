from __future__ import annotations

import unittest

from tools.real_surgery_annotation.run_shadow_replay import (
    _behavior_quality_manifest_summary,
)
from tools.real_surgery_annotation.shadow_contract import (
    BEHAVIOR_QUALITY_SCHEMA,
    BEHAVIOR_QUALITY_SCHEMA_V1,
    validate_behavior_quality_report,
)
from tools.real_surgery_annotation.shadow_evaluate import evaluate_shadow


class ShadowBehaviorQualityTest(unittest.TestCase):
    @staticmethod
    def _request(event_id: str, start_sec: float) -> dict:
        return {
            "event_id": event_id,
            "event_type": "implicit_tool_request",
            "review_status": "confirmed",
            "start_sec": start_sec,
            "end_sec": start_sec + 0.5,
            "time_sec": start_sec,
        }

    @staticmethod
    def _handover(event_id: str, time_sec: float, tool_id: str) -> dict:
        return {
            "event_id": event_id,
            "event_type": "tool_transfer",
            "review_status": "confirmed",
            "time_sec": time_sec,
            "tool": tool_id,
            "from": "scrub_nurse",
            "to": "surgeon",
        }

    @staticmethod
    def _record(
        layer: str,
        time_sec: float,
        payload: dict,
        sequence: int,
        *,
        wall_time_sec: float | None = None,
    ) -> dict:
        record = {
            "layer": layer,
            "ros_time_sec": time_sec,
            "sequence": sequence,
            "payload": payload,
        }
        if wall_time_sec is not None:
            record["wall_time_sec"] = wall_time_sec
        return record

    @classmethod
    def _active_request_record(
        cls,
        *,
        sequence: int,
        event_id: str,
        source_time_sec: float,
        wall_time_sec: float,
    ) -> dict:
        return cls._record(
            "evaluation_ground_truth",
            source_time_sec,
            {
                "schema": "taskplanner.shadow_ground_truth.v2",
                "evaluation_only": True,
                "source_time_sec": source_time_sec,
                "implicit_tool_request": {
                    "active": True,
                    "available": True,
                    "event_id": event_id,
                    "start_sec": source_time_sec,
                    "end_sec": source_time_sec + 0.5,
                },
            },
            sequence,
            wall_time_sec=wall_time_sec,
        )

    @classmethod
    def _completed_command(
        cls,
        *,
        sequence: int,
        command_id: str,
        action: str,
        tool_id: str,
        command_time_sec: float,
        completion_time_sec: float,
        event_type: str,
        instance_id: str = "",
        arm: str = "",
        command_wall_time_sec: float | None = None,
        completion_wall_time_sec: float | None = None,
    ) -> list[dict]:
        command_payload = {
            "command_id": command_id,
            "action": action,
            "instrument_id": tool_id,
        }
        event_payload = {
            "event_type": event_type,
            "instrument_id": tool_id,
            "detail_json": (
                '{"command_id":"' + command_id + '"}'
            ),
        }
        if instance_id:
            command_payload["instrument_instance_id"] = instance_id
            event_payload["instance_id"] = instance_id
        if arm:
            command_payload["arm"] = arm
            event_payload["arm"] = arm
        return [
            cls._record(
                "skill_command",
                command_time_sec,
                command_payload,
                sequence,
                wall_time_sec=command_wall_time_sec,
            ),
            cls._record(
                "skill_event",
                completion_time_sec,
                event_payload,
                sequence + 1,
                wall_time_sec=completion_wall_time_sec,
            ),
            cls._record(
                "skill_status",
                completion_time_sec,
                {
                    "command_id": command_id,
                    "action": action,
                    "instrument_id": tool_id,
                    "state": "completed",
                    "success": True,
                },
                sequence + 2,
                wall_time_sec=completion_wall_time_sec,
            ),
        ]

    def test_behavior_metrics_reward_recovery_not_perfect_prediction(self) -> None:
        ground_truth = [
            self._request("request-a", 10.0),
            self._handover("handover-a", 12.0, "tool_alpha"),
            self._request("request-b", 20.0),
            self._handover("handover-b", 22.0, "tool_beta"),
            self._handover("handover-c", 30.0, "tool_gamma"),
        ]
        trace = [
            *self._completed_command(
                sequence=0,
                command_id="prepare-a",
                action="predict_tool",
                tool_id="tool_alpha",
                command_time_sec=5.0,
                completion_time_sec=6.0,
                event_type="ToolPrepared",
            ),
            *self._completed_command(
                sequence=3,
                command_id="handover-a",
                action="direct_handover",
                tool_id="tool_alpha",
                command_time_sec=10.2,
                completion_time_sec=10.5,
                event_type="ToolHandoverCompleted",
            ),
            *self._completed_command(
                sequence=6,
                command_id="prepare-wrong",
                action="prepare_tool",
                tool_id="tool_delta",
                command_time_sec=15.0,
                completion_time_sec=16.0,
                event_type="ToolPrepared",
            ),
            *self._completed_command(
                sequence=9,
                command_id="handover-b",
                action="pick_up_and_handover",
                tool_id="tool_beta",
                command_time_sec=20.2,
                completion_time_sec=20.75,
                event_type="ToolHandoverCompleted",
            ),
            *self._completed_command(
                sequence=12,
                command_id="return-wrong",
                action="return_unused_preposition",
                tool_id="tool_delta",
                command_time_sec=20.5,
                completion_time_sec=21.0,
                event_type="PredictedToolReturnedToRack",
            ),
            self._record(
                "reducer_event",
                25.0,
                {
                    "input_id": "invariant-1",
                    "input_type": "invariant_violation",
                    "reason": "holder_exclusivity_invariant",
                },
                15,
            ),
        ]

        report = evaluate_shadow(
            ground_truth=ground_truth,
            decisions=trace,
            lead_window_sec=10.0,
        )
        behavior = report["behavior_quality"]
        summary = behavior["summary"]

        self.assertEqual(BEHAVIOR_QUALITY_SCHEMA, behavior["schema"])
        self.assertAlmostEqual(1.0 / 3.0, summary["preparation_coverage"])
        self.assertEqual(
            2,
            summary["request_to_handover_latency_sec"]["count"],
        )
        self.assertEqual(
            0.625,
            summary["request_to_handover_latency_sec"]["mean"],
        )
        self.assertEqual(
            1.0,
            summary["wrong_preposition_release_latency_sec"]["mean"],
        )
        self.assertEqual(
            0,
            summary[
                "request_to_handover_wall_clock_latency_sec"
            ]["count"],
        )
        self.assertEqual(
            0,
            summary[
                "wrong_preposition_release_wall_clock_latency_sec"
            ]["count"],
        )
        self.assertEqual(1, summary["unnecessary_preparation_count"])
        self.assertEqual(0.5, summary["unnecessary_preparation_rate"])
        self.assertEqual(1, summary["invariant_violation_count"])
        readiness = behavior["request_readiness"]
        self.assertEqual(2, readiness["evaluable_request_count"])
        self.assertEqual(1, readiness["ready_before_request_count"])
        self.assertEqual(1, readiness["prepared_at_request_count"])
        self.assertEqual(0, readiness["early_handover_count"])
        self.assertEqual(0.5, readiness["coverage"])
        self.assertEqual([], validate_behavior_quality_report(behavior))
        self.assertIs(
            behavior,
            report["scorecard"]["behavior_quality"],
        )

    def test_returned_prediction_is_measured_even_when_next_target_matches(
        self,
    ) -> None:
        ground_truth = [
            self._request("request-a", 20.0),
            self._handover("handover-a", 21.0, "tool_alpha"),
        ]
        trace = [
            *self._completed_command(
                sequence=0,
                command_id="prepare-a",
                action="predict_tool",
                tool_id="tool_alpha",
                command_time_sec=10.0,
                completion_time_sec=11.0,
                event_type="ToolPrepared",
                instance_id="tool_alpha#1",
                command_wall_time_sec=100.0,
                completion_wall_time_sec=101.0,
            ),
            *self._completed_command(
                sequence=3,
                command_id="return-a",
                action="return_unused_preposition",
                tool_id="tool_alpha",
                command_time_sec=12.0,
                completion_time_sec=13.0,
                event_type="PredictedToolReturnedToRack",
                instance_id="tool_alpha#1",
                command_wall_time_sec=102.0,
                completion_wall_time_sec=103.0,
            ),
        ]

        report = evaluate_shadow(
            ground_truth=ground_truth,
            decisions=trace,
            lead_window_sec=10.0,
        )
        behavior = report["behavior_quality"]
        summary = behavior["summary"]

        self.assertEqual(
            0,
            behavior["wrong_preposition_release"]["wrong_preposition_count"],
        )
        self.assertEqual(1, summary["abandoned_preposition_count"])
        self.assertEqual(
            2.0,
            summary["abandoned_preposition_hold_duration_sec"]["mean"],
        )
        self.assertEqual(
            2.0,
            summary[
                "abandoned_preposition_wall_clock_hold_duration_sec"
            ]["mean"],
        )
        self.assertEqual(
            "tool_alpha#1",
            behavior["abandoned_preposition"]["episodes"][0][
                "prepared_instance_id"
            ],
        )
        self.assertEqual(
            [],
            validate_behavior_quality_report(behavior),
        )

    def test_request_readiness_counts_matching_early_handover(self) -> None:
        ground_truth = [
            self._request("request-a", 10.0),
            self._handover("handover-a", 11.0, "tool_alpha"),
        ]
        trace = self._completed_command(
            sequence=0,
            command_id="handover-a",
            action="direct_handover",
            tool_id="tool_alpha",
            command_time_sec=8.8,
            completion_time_sec=9.2,
            event_type="ToolHandoverCompleted",
        )

        report = evaluate_shadow(
            ground_truth=ground_truth,
            decisions=trace,
            lead_window_sec=10.0,
        )
        readiness = report["behavior_quality"]["request_readiness"]

        self.assertEqual(1, readiness["evaluable_request_count"])
        self.assertEqual(1, readiness["ready_before_request_count"])
        self.assertEqual(0, readiness["prepared_at_request_count"])
        self.assertEqual(1, readiness["early_handover_count"])
        self.assertEqual("early_handover", readiness["requests"][0]["readiness_mode"])
        self.assertEqual(1.0, readiness["coverage"])

    def test_elastic_replay_reports_source_and_wall_clock_latency(
        self,
    ) -> None:
        trace = [
            self._active_request_record(
                sequence=0,
                event_id="request-a",
                source_time_sec=10.0,
                wall_time_sec=100.0,
            ),
            self._record(
                "reducer_fused",
                10.2,
                {
                    "explicit_request_tool": "tool_alpha",
                    "surgeon_request_tool": "tool_alpha",
                    "surgeon_request_generation": 1,
                },
                1,
                wall_time_sec=102.0,
            ),
            self._record(
                "bt_context_ingress",
                10.21,
                {
                    "explicit_request_tool": "tool_alpha",
                    "surgeon_request_tool": "tool_alpha",
                    "surgeon_request_generation": 1,
                },
                2,
                wall_time_sec=102.1,
            ),
            self._record(
                "bt_decision",
                10.25,
                {
                    "decision": "hold",
                    "action": "",
                    "selected_tool": "",
                    "request_generation": 1,
                    "blocking_guard": "pending_transition",
                },
                3,
                wall_time_sec=103.0,
            ),
            self._record(
                "bt_decision",
                10.3,
                {
                    "decision": "explicit_request",
                    "action": "pick_up_and_handover",
                    "selected_tool": "tool_alpha",
                    "request_generation": 1,
                },
                4,
                wall_time_sec=104.0,
            ),
            *self._completed_command(
                sequence=5,
                command_id="handover-a",
                action="pick_up_and_handover",
                tool_id="tool_alpha",
                command_time_sec=10.1,
                completion_time_sec=10.5,
                command_wall_time_sec=104.0,
                completion_wall_time_sec=108.0,
                event_type="ToolHandoverCompleted",
            ),
        ]
        report = evaluate_shadow(
            ground_truth=[
                self._request("request-a", 10.0),
                self._handover("handover-a", 12.0, "tool_alpha"),
            ],
            decisions=trace,
            lead_window_sec=10.0,
        )
        behavior = report["behavior_quality"]
        summary = behavior["summary"]
        episode = behavior["request_to_handover"]["episodes"][0]

        self.assertEqual(
            0.5,
            summary["request_to_handover_latency_sec"]["mean"],
        )
        self.assertEqual(
            8.0,
            summary[
                "request_to_handover_wall_clock_latency_sec"
            ]["mean"],
        )
        self.assertEqual(100.0, episode["request_wall_time_sec"])
        self.assertEqual(
            108.0,
            episode["system_handover_wall_time_sec"],
        )
        self.assertEqual(8.0, episode["wall_clock_latency_sec"])
        pipeline = summary["request_pipeline_latency"]
        self.assertEqual(
            0.2,
            pipeline[
                "ground_truth_to_dt_request_fact_latency_sec"
            ]["mean"],
        )
        self.assertEqual(
            2.0,
            pipeline[
                "ground_truth_to_dt_request_fact_wall_clock_latency_sec"
            ]["mean"],
        )
        self.assertEqual(
            0.01,
            pipeline[
                "dt_request_fact_to_bt_ingress_latency_sec"
            ]["mean"],
        )
        self.assertEqual(
            0.1,
            pipeline[
                "dt_request_fact_to_bt_ingress_wall_clock_latency_sec"
            ]["mean"],
        )
        self.assertEqual(
            0.05,
            pipeline[
                "dt_request_fact_to_bt_evaluation_latency_sec"
            ]["mean"],
        )
        self.assertEqual(
            1.0,
            pipeline[
                "dt_request_fact_to_bt_evaluation_wall_clock_latency_sec"
            ]["mean"],
        )
        self.assertEqual(
            0.1,
            pipeline[
                "dt_request_fact_to_bt_acceptance_latency_sec"
            ]["mean"],
        )
        self.assertEqual(
            2.0,
            pipeline[
                "dt_request_fact_to_bt_acceptance_wall_clock_latency_sec"
            ]["mean"],
        )
        self.assertEqual(
            0.2,
            pipeline[
                "bt_acceptance_to_handover_latency_sec"
            ]["mean"],
        )
        self.assertEqual(
            4.0,
            pipeline[
                "bt_acceptance_to_handover_wall_clock_latency_sec"
            ]["mean"],
        )
        self.assertEqual("explicit_request", episode["dt_request_fact_source"])
        self.assertEqual(
            "explicit_request",
            episode["bt_request_acceptance_source"],
        )
        self.assertEqual("hold", episode["bt_request_evaluation_decision"])

    def test_pipeline_links_bt_by_request_generation(self) -> None:
        trace = [
            self._active_request_record(
                sequence=0,
                event_id="request-a",
                source_time_sec=10.0,
                wall_time_sec=100.0,
            ),
            self._record(
                "reducer_fused",
                10.2,
                {
                    "explicit_request_tool": "tool_alpha",
                    "surgeon_request_generation": 7,
                },
                1,
                wall_time_sec=102.0,
            ),
            self._record(
                "bt_context_ingress",
                10.21,
                {
                    "explicit_request_tool": "tool_alpha",
                    "surgeon_request_generation": 7,
                },
                2,
                wall_time_sec=102.1,
            ),
            self._record(
                "bt_decision",
                10.25,
                {
                    "decision": "explicit_request",
                    "action": "direct_handover",
                    "selected_tool": "tool_alpha",
                    "request_generation": 8,
                },
                3,
                wall_time_sec=102.5,
            ),
            self._record(
                "bt_decision",
                10.3,
                {
                    "decision": "hold",
                    "action": "",
                    "selected_tool": "",
                    "request_generation": 7,
                },
                4,
                wall_time_sec=103.0,
            ),
            self._record(
                "bt_decision",
                10.4,
                {
                    "decision": "explicit_request",
                    "action": "direct_handover",
                    "selected_tool": "tool_alpha",
                    "request_generation": 7,
                },
                5,
                wall_time_sec=104.0,
            ),
            *self._completed_command(
                sequence=6,
                command_id="handover-a",
                action="direct_handover",
                tool_id="tool_alpha",
                command_time_sec=10.4,
                completion_time_sec=10.6,
                command_wall_time_sec=104.0,
                completion_wall_time_sec=105.0,
                event_type="ToolHandoverCompleted",
            ),
        ]
        report = evaluate_shadow(
            ground_truth=[
                self._request("request-a", 10.0),
                self._handover("handover-a", 11.0, "tool_alpha"),
            ],
            decisions=trace,
            lead_window_sec=10.0,
        )
        episode = report["behavior_quality"]["request_to_handover"][
            "episodes"
        ][0]

        self.assertEqual(10.3, episode["bt_request_evaluation_time_sec"])
        self.assertEqual(10.4, episode["bt_request_acceptance_time_sec"])
        self.assertEqual(10.21, episode["bt_context_ingress_time_sec"])
        self.assertEqual(
            0.1,
            episode["dt_request_fact_to_bt_evaluation_latency_sec"],
        )
        self.assertEqual(
            0.2,
            episode["dt_request_fact_to_bt_acceptance_latency_sec"],
        )

    def test_pipeline_latency_preserves_early_visual_fact_as_zero_delay(
        self,
    ) -> None:
        trace = [
            self._active_request_record(
                sequence=0,
                event_id="request-a",
                source_time_sec=10.0,
                wall_time_sec=100.0,
            ),
            self._record(
                "reducer_fused",
                9.8,
                {
                    "implicit_request_visible": True,
                    "implicit_request_hand_pose": "open_receive",
                    "implicit_request_confidence": 0.9,
                    "implicit_request_generation": 1,
                    "predicted_tool": "tool_alpha",
                    "predicted_tool_confidence": 0.8,
                },
                1,
                wall_time_sec=99.5,
            ),
            self._record(
                "bt_decision",
                10.4,
                {
                    "decision": "implicit_request",
                    "action": "direct_handover",
                    "selected_tool": "tool_alpha",
                },
                2,
                wall_time_sec=103.0,
            ),
            *self._completed_command(
                sequence=3,
                command_id="handover-a",
                action="direct_handover",
                tool_id="tool_alpha",
                command_time_sec=10.4,
                completion_time_sec=10.6,
                command_wall_time_sec=103.0,
                completion_wall_time_sec=105.0,
                event_type="ToolHandoverCompleted",
            ),
        ]
        report = evaluate_shadow(
            ground_truth=[
                self._request("request-a", 10.0),
                self._handover("handover-a", 11.0, "tool_alpha"),
            ],
            decisions=trace,
            lead_window_sec=10.0,
        )

        pipeline = report["behavior_quality"]["summary"][
            "request_pipeline_latency"
        ]
        episode = report["behavior_quality"]["request_to_handover"][
            "episodes"
        ][0]
        self.assertEqual(0.0, episode[
            "ground_truth_to_dt_request_fact_latency_sec"
        ])
        self.assertEqual(-0.2, episode[
            "ground_truth_to_dt_request_fact_offset_sec"
        ])
        self.assertEqual(1, pipeline["early_dt_request_fact_count"])
        self.assertEqual(
            "visual_implicit_request",
            episode["dt_request_fact_source"],
        )

    def test_tool_forecasts_are_split_by_confirmed_request_boundary(
        self,
    ) -> None:
        ground_truth = [
            self._request("request-a", 10.0),
            self._handover("handover-a", 12.0, "tool_alpha"),
            self._request("request-b", 20.0),
            self._handover("handover-b", 22.0, "tool_beta"),
            self._handover("handover-c", 30.0, "tool_gamma"),
        ]
        trace = [
            self._record(
                "vlm_raw",
                9.0,
                {
                    "tool": [["tool_alpha", 0.9]],
                    "intent": ["none", "", 0.0],
                },
                0,
            ),
            self._record(
                "vlm_raw",
                21.0,
                {
                    "tool": [["tool_beta", 0.9]],
                    "intent": ["none", "", 0.0],
                },
                1,
            ),
            self._record(
                "vlm_raw",
                28.0,
                {
                    "tool": [["tool_gamma", 0.9]],
                    "intent": ["none", "", 0.0],
                },
                2,
            ),
        ]

        report = evaluate_shadow(
            ground_truth=ground_truth,
            decisions=trace,
            lead_window_sec=10.0,
        )
        layer = report["layers"]["vlm_raw"]
        by_event = {
            row["event_id"]: row
            for row in layer["events"]
        }

        self.assertEqual(3, layer["outcomes"]["exact_match"])
        self.assertEqual(2, layer["proactive_exact_count"])
        self.assertEqual(1, layer["pre_request_proactive_exact_count"])
        self.assertEqual(
            1,
            layer["unrequested_pre_handover_exact_count"],
        )
        self.assertEqual(1, layer["post_request_visual_exact_count"])
        self.assertEqual(
            "pre_request_proactive",
            by_event["handover-a"]["decision_timing"],
        )
        self.assertEqual(
            "post_request_visual",
            by_event["handover-b"]["decision_timing"],
        )
        self.assertEqual(
            "unrequested_pre_handover",
            by_event["handover-c"]["decision_timing"],
        )
        score = report["scorecard"]["next_tool_prediction"]["layers"][
            "vlm_raw"
        ]
        self.assertAlmostEqual(2.0 / 3.0, score["proactive_target_recall"])
        self.assertAlmostEqual(
            1.0 / 3.0,
            score["post_request_visual_target_recall"],
        )
        self.assertEqual(1.0, score["combined_action_selection_accuracy"])

    def test_near_boundary_early_handover_is_an_explicit_zero_latency_match(
        self,
    ) -> None:
        report = evaluate_shadow(
            ground_truth=[
                self._request("request-a", 10.0),
                self._handover("handover-a", 11.0, "tool_alpha"),
            ],
            decisions=self._completed_command(
                sequence=0,
                command_id="handover-a",
                action="direct_handover",
                tool_id="tool_alpha",
                command_time_sec=8.8,
                completion_time_sec=9.0,
                event_type="ToolHandoverCompleted",
            ),
            lead_window_sec=10.0,
        )

        request = report["behavior_quality"]["request_to_handover"]
        episode = request["episodes"][0]
        self.assertEqual(1, request["completed_count"])
        self.assertEqual(0.0, episode["latency_sec"])
        self.assertEqual(-1.0, episode["response_offset_sec"])
        self.assertEqual(1.0, episode["early_lead_sec"])
        self.assertTrue(episode["early_match"])

    def test_handover_before_early_match_tolerance_remains_missed(
        self,
    ) -> None:
        report = evaluate_shadow(
            ground_truth=[
                self._request("request-a", 10.0),
                self._handover("handover-a", 11.0, "tool_alpha"),
            ],
            decisions=self._completed_command(
                sequence=0,
                command_id="handover-a",
                action="direct_handover",
                tool_id="tool_alpha",
                command_time_sec=7.7,
                completion_time_sec=8.0,
                event_type="ToolHandoverCompleted",
            ),
            lead_window_sec=10.0,
        )

        request = report["behavior_quality"]["request_to_handover"]
        self.assertEqual(0, request["completed_count"])
        self.assertEqual(1, request["missed_count"])

    def test_wrong_preposition_wall_clock_includes_skill_hold(
        self,
    ) -> None:
        trace = [
            *self._completed_command(
                sequence=0,
                command_id="prepare-wrong",
                action="predict_tool",
                tool_id="tool_wrong",
                command_time_sec=2.0,
                completion_time_sec=3.0,
                command_wall_time_sec=90.0,
                completion_wall_time_sec=91.0,
                event_type="ToolPrepared",
            ),
            self._active_request_record(
                sequence=3,
                event_id="request-next",
                source_time_sec=20.0,
                wall_time_sec=100.0,
            ),
            *self._completed_command(
                sequence=4,
                command_id="return-wrong",
                action="return_unused_preposition",
                tool_id="tool_wrong",
                command_time_sec=20.2,
                completion_time_sec=21.0,
                command_wall_time_sec=103.0,
                completion_wall_time_sec=106.0,
                event_type="PredictedToolReturnedToRack",
            ),
        ]
        report = evaluate_shadow(
            ground_truth=[
                self._request("request-next", 20.0),
                self._handover("handover-next", 22.0, "tool_next"),
            ],
            decisions=trace,
            lead_window_sec=10.0,
        )
        behavior = report["behavior_quality"]
        summary = behavior["summary"]
        episode = behavior["wrong_preposition_release"]["episodes"][0]

        self.assertEqual(
            1.0,
            summary["wrong_preposition_release_latency_sec"]["mean"],
        )
        self.assertEqual(
            6.0,
            summary[
                "wrong_preposition_release_wall_clock_latency_sec"
            ]["mean"],
        )
        self.assertEqual(100.0, episode["contradiction_wall_time_sec"])
        self.assertEqual(106.0, episode["release_wall_time_sec"])
        self.assertEqual(
            6.0,
            episode["wall_clock_release_latency_sec"],
        )

    def test_wrong_preposition_without_release_is_reported(self) -> None:
        report = evaluate_shadow(
            ground_truth=[
                self._request("request-next", 8.0),
                self._handover("handover-next", 10.0, "tool_next"),
            ],
            decisions=self._completed_command(
                sequence=0,
                command_id="prepare-other",
                action="predict_tool",
                tool_id="tool_other",
                command_time_sec=2.0,
                completion_time_sec=3.0,
                event_type="ToolPrepared",
            ),
            lead_window_sec=10.0,
        )

        wrong = report["behavior_quality"]["wrong_preposition_release"]
        self.assertEqual(1, wrong["wrong_preposition_count"])
        self.assertEqual(0, wrong["released_count"])
        self.assertEqual(1, wrong["unreleased_count"])
        self.assertEqual(0, wrong["latency_sec"]["count"])

    def test_consumed_preparation_is_stale_at_later_request(
        self,
    ) -> None:
        trace = [
            *self._completed_command(
                sequence=0,
                command_id="prepare-early",
                action="predict_tool",
                tool_id="tool_same",
                instance_id="tool_same#2",
                command_time_sec=2.0,
                completion_time_sec=3.0,
                event_type="ToolPrepared",
            ),
            *self._completed_command(
                sequence=3,
                command_id="handover-early",
                action="direct_handover",
                tool_id="tool_same",
                instance_id="tool_same#2",
                command_time_sec=4.0,
                completion_time_sec=5.0,
                event_type="ToolHandoverCompleted",
            ),
        ]
        report = evaluate_shadow(
            ground_truth=[
                self._request("request-later", 10.0),
                self._handover(
                    "handover-later",
                    12.0,
                    "tool_same",
                ),
            ],
            decisions=trace,
            lead_window_sec=10.0,
        )

        coverage = report["behavior_quality"][
            "preparation_coverage"
        ]
        target = coverage["targets"][0]
        self.assertEqual(0, coverage["prepared_before_request_count"])
        self.assertEqual(1, coverage["missed_preparation_count"])
        self.assertFalse(target["prepared"])
        self.assertIsNone(target["preparation_outcome_id"])
        self.assertEqual(1, target["stale_preparation_count"])
        self.assertEqual(
            "consumed",
            target["stale_preparations"][0]["invalidation_reason"],
        )
        self.assertEqual(
            "handovers:handover-early",
            target["stale_preparations"][0][
                "invalidated_by_outcome_id"
            ],
        )

    def test_later_same_arm_preparation_supersedes_old_preparation(
        self,
    ) -> None:
        trace = [
            *self._completed_command(
                sequence=0,
                command_id="prepare-target",
                action="predict_tool",
                tool_id="tool_target",
                instance_id="tool_target#1",
                arm="right",
                command_time_sec=2.0,
                completion_time_sec=3.0,
                event_type="ToolPrepared",
            ),
            *self._completed_command(
                sequence=3,
                command_id="prepare-other",
                action="predict_tool",
                tool_id="tool_other",
                instance_id="tool_other#1",
                arm="right",
                command_time_sec=5.0,
                completion_time_sec=6.0,
                event_type="ToolPrepared",
            ),
        ]
        report = evaluate_shadow(
            ground_truth=[
                self._request("request-target", 10.0),
                self._handover(
                    "handover-target",
                    12.0,
                    "tool_target",
                ),
            ],
            decisions=trace,
            lead_window_sec=10.0,
        )

        target = report["behavior_quality"]["preparation_coverage"][
            "targets"
        ][0]
        self.assertFalse(target["prepared"])
        self.assertEqual(
            "superseded",
            target["stale_preparations"][0]["invalidation_reason"],
        )

    def test_duplicate_tool_return_does_not_release_other_instance(
        self,
    ) -> None:
        trace = [
            *self._completed_command(
                sequence=0,
                command_id="prepare-kept",
                action="predict_tool",
                tool_id="tool_same",
                instance_id="tool_same#2",
                command_time_sec=2.0,
                completion_time_sec=3.0,
                event_type="ToolPrepared",
            ),
            *self._completed_command(
                sequence=3,
                command_id="prepare-returned",
                action="predict_tool",
                tool_id="tool_same",
                instance_id="tool_same#1",
                command_time_sec=3.5,
                completion_time_sec=4.0,
                event_type="ToolPrepared",
            ),
            *self._completed_command(
                sequence=6,
                command_id="return-one",
                action="return_unused_preposition",
                tool_id="tool_same",
                instance_id="tool_same#1",
                command_time_sec=7.0,
                completion_time_sec=8.0,
                event_type="PredictedToolReturnedToRack",
            ),
        ]
        report = evaluate_shadow(
            ground_truth=[
                self._request("request-same", 10.0),
                self._handover(
                    "handover-same", 12.0, "tool_same"
                ),
            ],
            decisions=trace,
            lead_window_sec=10.0,
        )

        coverage = report["behavior_quality"][
            "preparation_coverage"
        ]
        self.assertEqual(1, coverage["prepared_before_request_count"])
        self.assertEqual(1.0, coverage["coverage"])

    def test_replace_and_handover_counts_embedded_old_tool_release(
        self,
    ) -> None:
        trace = [
            *self._completed_command(
                sequence=0,
                command_id="prepare-old",
                action="predict_tool",
                tool_id="tool_old",
                instance_id="tool_old#1",
                command_time_sec=2.0,
                completion_time_sec=3.0,
                event_type="ToolPrepared",
            ),
            self._record(
                "skill_command",
                10.1,
                {
                    "command_id": "replace",
                    "action": "put_down_and_handover",
                    "instrument_id": "tool_new",
                    "instrument_instance_id": "tool_new#1",
                },
                3,
            ),
            self._record(
                "skill_event",
                10.4,
                {
                    "event_type": "PredictedToolReturnedToRack",
                    "instrument_id": "tool_old",
                    "instance_id": "tool_old#1",
                    "detail_json": '{"command_id":"replace"}',
                },
                4,
            ),
            self._record(
                "skill_event",
                10.8,
                {
                    "event_type": "ToolHandoverCompleted",
                    "instrument_id": "tool_new",
                    "instance_id": "tool_new#1",
                    "detail_json": '{"command_id":"replace"}',
                },
                5,
            ),
            self._record(
                "skill_status",
                10.8,
                {
                    "command_id": "replace",
                    "action": "put_down_and_handover",
                    "instrument_id": "tool_new",
                    "state": "completed",
                    "success": True,
                },
                6,
            ),
        ]
        report = evaluate_shadow(
            ground_truth=[
                self._request("request-new", 10.0),
                self._handover(
                    "handover-new", 12.0, "tool_new"
                ),
            ],
            decisions=trace,
            lead_window_sec=10.0,
        )

        wrong = report["behavior_quality"][
            "wrong_preposition_release"
        ]
        self.assertEqual(1, wrong["released_count"])
        self.assertAlmostEqual(
            0.4,
            wrong["latency_sec"]["mean"],
        )

    def test_repeated_bt_invariant_guard_is_one_violation_episode(self) -> None:
        trace = [
            self._record(
                "bt_decision",
                1.0,
                {
                    "decision": "hold",
                    "blocking_guard": "blocked_invariant",
                    "decision_reason": "same tool has multiple holders",
                },
                0,
            ),
            self._record(
                "bt_decision",
                1.1,
                {
                    "decision": "hold",
                    "blocking_guard": "blocked_invariant",
                    "decision_reason": "same tool has multiple holders",
                },
                1,
            ),
            self._record(
                "bt_decision",
                2.0,
                {"decision": "hold", "blocking_guard": ""},
                2,
            ),
        ]
        report = evaluate_shadow(
            ground_truth=[
                self._handover("handover", 5.0, "tool_target")
            ],
            decisions=trace,
            lead_window_sec=10.0,
        )

        self.assertEqual(
            1,
            report["behavior_quality"]["summary"][
                "invariant_violation_count"
            ],
        )

    def test_wall_clock_distributions_exist_on_empty_and_partial_paths(
        self,
    ) -> None:
        reports = [
            evaluate_shadow(
                ground_truth=[],
                decisions=[],
                lead_window_sec=10.0,
            ),
            evaluate_shadow(
                ground_truth=[
                    self._handover(
                        "handover-only",
                        5.0,
                        "tool_target",
                    )
                ],
                decisions=[],
                lead_window_sec=10.0,
            ),
        ]

        for report in reports:
            summary = report["behavior_quality"]["summary"]
            for key in (
                "request_to_handover_wall_clock_latency_sec",
                "wrong_preposition_release_wall_clock_latency_sec",
            ):
                self.assertIn(key, summary)
                self.assertEqual(
                    {
                        "count": 0,
                        "mean": None,
                        "median": None,
                        "p95": None,
                        "max": None,
                    },
                    summary[key],
                )
            self.assertEqual(
                [],
                validate_behavior_quality_report(
                    report["behavior_quality"]
                ),
            )

    def test_manifest_keeps_only_valid_evaluation_summary(self) -> None:
        report = evaluate_shadow(
            ground_truth=[
                self._handover("handover", 5.0, "tool_target")
            ],
            decisions=[],
            lead_window_sec=10.0,
        )

        summary = _behavior_quality_manifest_summary(report)

        self.assertTrue(summary["evaluation_only"])
        self.assertEqual(BEHAVIOR_QUALITY_SCHEMA, summary["schema"])
        self.assertIn("preparation_coverage", summary)
        self.assertIn(
            "request_to_handover_wall_clock_latency_sec",
            summary,
        )
        self.assertIn(
            "wrong_preposition_release_wall_clock_latency_sec",
            summary,
        )
        self.assertIn("latency_clock_semantics", summary)
        self.assertNotIn("targets", summary)

        legacy_behavior = dict(report["behavior_quality"])
        legacy_behavior["schema"] = BEHAVIOR_QUALITY_SCHEMA_V1
        legacy_behavior["summary"] = dict(legacy_behavior["summary"])
        legacy_behavior["summary"].pop(
            "request_to_handover_wall_clock_latency_sec"
        )
        legacy_behavior["summary"].pop(
            "wrong_preposition_release_wall_clock_latency_sec"
        )
        legacy_summary = _behavior_quality_manifest_summary(
            {"behavior_quality": legacy_behavior}
        )
        self.assertEqual(
            0,
            legacy_summary[
                "request_to_handover_wall_clock_latency_sec"
            ]["count"],
        )
        self.assertEqual(
            0,
            legacy_summary[
                "wrong_preposition_release_wall_clock_latency_sec"
            ]["count"],
        )

        invalid = {
            "behavior_quality": {
                "schema": BEHAVIOR_QUALITY_SCHEMA,
                "summary": {"preparation_coverage": 1.5},
            }
        }
        with self.assertRaisesRegex(
            ValueError,
            "behavior-quality contract validation failed",
        ):
            _behavior_quality_manifest_summary(invalid)


if __name__ == "__main__":
    unittest.main()
