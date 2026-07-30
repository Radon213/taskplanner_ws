from __future__ import annotations

import json
import unittest
from pathlib import Path

from tools.real_surgery_annotation.publish_assistant_case_reference import (
    PublicationError,
    validate_evaluation_masks,
)


ROOT = Path(__file__).resolve().parents[2]
MASK_SCHEMA_PATH = (
    ROOT
    / "annotations/observable_tool_events/schema/evaluation_masks.v1.schema.json"
)
METRIC_KEYS = (
    "action",
    "latency",
    "state",
    "physical",
    "reuse",
    "gesture_presence",
    "gesture_onset",
    "phase_accuracy",
    "actor_identity",
)


def eligibility(**enabled: bool) -> dict[str, bool]:
    value = {key: False for key in METRIC_KEYS}
    value.update(enabled)
    return value


class EvaluationMaskPublicationGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = json.loads(MASK_SCHEMA_PATH.read_text(encoding="utf-8"))
        self.observed = [
            {
                "event_id": "case_demo-R0001",
                "event_type": "implicit_tool_request",
            },
            {
                "event_id": "case_demo-T0001",
                "event_type": "tool_transfer",
                "tool": "bovie",
            },
        ]
        self.phases = [
            {
                "event_id": "case_demo-PH0001",
                "event_type": "phase_start",
            }
        ]
        self.voice = [
            {
                "event_id": "case_demo-V0001",
                "end_sec": 1.2,
                "available_sec": 1.2,
            }
        ]
        self.timeline = {"end_sec": 2.0}
        self.masks = {
            "schema": "taskplanner.evaluation_masks.v1",
            "case_id": "case_demo",
            "evaluation_scope": {
                "classification": "development_calibration",
                "held_out_eligible": False,
                "reason": "Policy and Phase ontology calibration case.",
            },
            "default_metric_eligibility": eligibility(),
            "event_roles": [
                {
                    "event_id": "case_demo-R0001",
                    "role": "gesture_target",
                    "metric_eligibility": eligibility(
                        gesture_presence=True,
                        gesture_onset=True,
                    ),
                    "reason": "Exact-frame open-palm interval.",
                },
                {
                    "event_id": "case_demo-T0001",
                    "role": "action_target",
                    "metric_eligibility": eligibility(
                        action=True,
                        latency=True,
                    ),
                    "reason": "Completed surgeon handover.",
                },
                {
                    "event_id": "case_demo-PH0001",
                    "role": "context_only_not_ground_truth",
                    "metric_eligibility": eligibility(),
                    "reason": "Provisional Phase context.",
                },
            ],
            "interval_masks": [],
            "cutoffs": {
                "action_and_next_tool_end_sec": 2.0,
                "state_audit_end_sec": 2.0,
                "visual_end_sec": 2.0,
                "voice_context_end_sec": 1.2,
            },
            "tool_metric_scopes": [
                {
                    "tool": "bovie",
                    "instance_resolution": "initial_inventory_unavailable",
                    "state": False,
                    "physical": False,
                    "reuse": False,
                    "reason": "No instance-resolved initial inventory.",
                }
            ],
            "voice_context_roles": [
                {
                    "event_id": "case_demo-V0001",
                    "role": "tool_request_context",
                    "handover_target": True,
                    "reason": "Visible handover follows the utterance.",
                }
            ],
        }

    def validate(self) -> None:
        validate_evaluation_masks(
            masks=self.masks,
            mask_schema=self.schema,
            case_id="case_demo",
            observed=self.observed,
            phases=self.phases,
            voice=self.voice,
            timeline=self.timeline,
        )

    def test_complete_default_deny_mask_passes(self) -> None:
        self.validate()

    def test_missing_observed_role_fails_closed(self) -> None:
        self.masks["event_roles"] = self.masks["event_roles"][:-2] + [
            self.masks["event_roles"][-1]
        ]
        with self.assertRaisesRegex(PublicationError, "exhaustive"):
            self.validate()

    def test_missing_voice_role_fails_closed(self) -> None:
        self.masks["voice_context_roles"] = []
        with self.assertRaisesRegex(PublicationError, "voice roles"):
            self.validate()

    def test_instance_metrics_cannot_be_opened(self) -> None:
        self.masks["tool_metric_scopes"][0]["state"] = True
        with self.assertRaisesRegex(PublicationError, "state/physical/reuse"):
            self.validate()

    def test_case_cannot_claim_held_out(self) -> None:
        self.masks["evaluation_scope"].update(
            {
                "classification": "held_out",
                "held_out_eligible": True,
            }
        )
        with self.assertRaisesRegex(PublicationError, "development_calibration"):
            self.validate()


if __name__ == "__main__":
    unittest.main()
