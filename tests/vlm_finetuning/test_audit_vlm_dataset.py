from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.vlm_finetuning.audit_vlm_dataset import (
    audit_dataset,
    load_jsonl,
    main,
)


def make_master(
    *,
    example_id: str,
    task_type: str,
    target: dict[str, object],
    cutoff: float = 10.0,
    case_id: str = "0704_7",
    split: str = "train",
    authority: str = "user_authorized_assistant",
    media_time: float = 10.0,
) -> dict[str, object]:
    return {
        "schema": "taskplanner.causal_vlm_sft_example.v1",
        "example_id": example_id,
        "case_id": case_id,
        "split_group_id": f"case:{case_id}",
        "split": {"fold_id": "fold_0", "role": split},
        "task_type": task_type,
        "time": {
            "causal_cutoff_sec": cutoff,
            "window_start_sec": max(0.0, cutoff - 5.0),
            "window_end_sec": cutoff,
        },
        "media": [
            {
                "view": "flir",
                "source_frame_idx": round(media_time * 15),
                "time_sec": media_time,
                "path": f"/media/{case_id}/{media_time:.3f}.jpg",
            }
        ],
        "causal_context": {
            "voice": [
                {
                    "event_id": f"{example_id}-voice",
                    "text": "바이폴라 주세요",
                    "available_sec": cutoff,
                }
            ],
            "prior_events": [
                {
                    "event_id": f"{example_id}-prior",
                    "event_sec": cutoff - 1.0,
                }
            ],
        },
        "target": target,
        "authority": {
            "tier": authority,
            "label": authority,
            "source_ids": [f"{example_id}-source"],
        },
        "quality": {
            "gap_safe": True,
            "no_future_input": True,
            "scoring_role": "train",
        },
    }


def make_message(master: dict[str, object]) -> dict[str, object]:
    media = master["media"]
    assert isinstance(media, list)
    target = master["target"]
    assert isinstance(target, dict)
    assistant_target = target
    if master["task_type"] == "next_physical_tool":
        assistant_target = {
            key: target[key]
            for key in ("next_transfer_tool", "event", "basis")
            if key in target
        }
    media_path = media[0]["path"]
    return {
        "example_id": master["example_id"],
        "task_type": master["task_type"],
        "split_group_id": master["split_group_id"],
        "split": master["split"],
        "messages": [
            {
                "role": "system",
                "content": "JSON 객체만 출력하세요.",
            },
            {
                "role": "user",
                "content": [
                    {"type": "image", "image": media_path},
                    {"type": "text", "text": "현재 상태를 판정하세요."},
                ],
            },
            {
                "role": "assistant",
                "content": json.dumps(
                    assistant_target, ensure_ascii=False, sort_keys=True
                ),
            },
        ],
    }


class AuditVlmDatasetTest(unittest.TestCase):
    def test_valid_master_and_unsloth_messages_pass(self) -> None:
        rows = [
            make_master(
                example_id="tool-1",
                task_type="tool_presence_at_transfer",
                target={
                    "tool": "bovie",
                    "event": "physical_transfer",
                    "exhaustive_visible_tool_inventory": False,
                },
                media_time=6.0,
            ),
            make_master(
                example_id="intent-1",
                task_type="request_intent",
                target={
                    "intent": "receive_unspecified_tool",
                    "requested_tool": None,
                },
                cutoff=12.0,
                media_time=12.0,
            ),
            make_master(
                example_id="phase-1",
                task_type="current_phase",
                target={"phase_id": "P04", "state": "transition"},
                cutoff=14.0,
                media_time=14.0,
            ),
            make_master(
                example_id="next-1",
                task_type="next_physical_tool",
                target={
                    "next_transfer_tool": "bipolar",
                    "event": "scrub_nurse_to_surgeon",
                    "basis": "anticipatory_context",
                    "target_event_id": "transfer-17",
                    "target_time_sec": 17.0,
                    "prediction_regime": "anticipatory_context",
                },
                cutoff=16.0,
                media_time=16.0,
            ),
            make_master(
                example_id="clinical-1",
                task_type="clinical_observation_interpretation",
                target={
                    "observation": "바이폴라가 중앙 조직면에 접촉한다.",
                    "interpretation": "미세 출혈점을 선택 응고하는 단계다.",
                    "confidence": {
                        "observation": "high",
                        "interpretation": "medium",
                    },
                },
                cutoff=18.0,
                media_time=18.0,
                authority="ai_draft",
            ),
        ]
        messages = [make_message(row) for row in rows]

        report = audit_dataset(rows, messages)

        self.assertTrue(report["ok"])
        self.assertEqual(0, report["summary"]["error_count"])
        self.assertEqual(5, report["summary"]["master_rows"])
        self.assertEqual(
            4,
            report["summary"]["authority_tier_counts"][
                "authorized_silver"
            ],
        )
        self.assertEqual(
            1,
            report["summary"]["authority_tier_counts"]["draft_silver"],
        )
        self.assertTrue(report["checks"]["causal_inputs_only"])
        warning_codes = {item["code"] for item in report["warnings"]}
        self.assertIn("authority_draft_silver_present", warning_codes)

    def test_detects_split_and_future_information_leakage(self) -> None:
        first = make_master(
            example_id="next-leak",
            task_type="next_physical_tool",
            target={
                "next_transfer_tool": "bovie",
                "event": "scrub_nurse_to_surgeon",
                "event_id": "target-transfer",
            },
            cutoff=10.0,
            split="train",
            media_time=11.0,
            authority="mystery_model",
        )
        context = first["causal_context"]
        assert isinstance(context, dict)
        voice = context["voice"]
        assert isinstance(voice, list)
        voice[0]["available_sec"] = 10.5
        prior_events = context["prior_events"]
        assert isinstance(prior_events, list)
        prior_events[0] = {
            "event_id": "target-transfer",
            "event_sec": 10.2,
        }
        second = make_master(
            example_id="phase-leak",
            task_type="current_phase",
            target={"phase_id": "P04", "state": "interior"},
            cutoff=9.0,
            split="validation",
            media_time=9.0,
        )
        second_split = second["split"]
        assert isinstance(second_split, dict)
        second_split["fold_id"] = "fold_1"
        second["media"] = first["media"]
        message = make_message(first)
        messages = message["messages"]
        assert isinstance(messages, list)
        messages[-1]["content"] = '{"next_transfer_tool":"mosquito"}'

        report = audit_dataset(
            [first, second], [message, make_message(second)]
        )
        codes = {item["code"] for item in report["errors"]}

        self.assertFalse(report["ok"])
        self.assertIn("authority_unknown", codes)
        self.assertIn("causal_voice_future_leakage", codes)
        self.assertIn("causal_event_future_leakage", codes)
        self.assertIn("target_event_in_input", codes)
        self.assertIn("media_future_leakage", codes)
        self.assertIn("case_split_leakage", codes)
        self.assertIn("split_group_leakage", codes)
        self.assertIn("case_fold_leakage", codes)
        self.assertIn("split_group_fold_leakage", codes)
        self.assertIn("media_split_leakage", codes)
        self.assertIn("assistant_target_mismatch", codes)

    def test_rejects_invalid_task_targets_and_duplicate_media_task(self) -> None:
        row_a = make_master(
            example_id="clinical-a",
            task_type="clinical_observation_interpretation",
            target={"observation": "보이는 사실만 기록했다."},
        )
        row_b = make_master(
            example_id="clinical-b",
            task_type="clinical_observation_interpretation",
            target={
                "observation": "보이는 사실만 기록했다.",
                "interpretation": "임상적 해석이다.",
            },
        )
        row_b["media"] = row_a["media"]
        report = audit_dataset([row_a, row_b])

        error_codes = {item["code"] for item in report["errors"]}
        warning_codes = {item["code"] for item in report["warnings"]}
        self.assertIn("target_clinical_interpretation_missing", error_codes)
        self.assertIn("duplicate_media_task_examples", warning_codes)

    def test_real_builder_authority_tiers_are_classified(self) -> None:
        labels = [
            "reviewed_human",
            "silver_user_authorized_ai_assistant",
            "derived_from_reviewed_human",
            "derived_from_silver_user_authorized_ai_assistant",
            "silver_unreviewed_or_other",
            "provisional_ai_phase_not_scoring_ground_truth",
            "silver_ai_draft_needs_surgeon_review",
            "derived_from_complete_dt_reference",
            "derived_from_custom_source",
        ]
        rows = [
            make_master(
                example_id=f"authority-{index}",
                task_type="current_phase",
                target={"phase_id": "P03", "state": "interior"},
                case_id=f"case-{index}",
                cutoff=10.0 + index,
                media_time=10.0 + index,
                authority=label,
            )
            for index, label in enumerate(labels)
        ]

        report = audit_dataset(rows)

        self.assertTrue(report["ok"])
        self.assertEqual(
            {
                "authorized_silver": 1,
                "derived_authorized_silver": 1,
                "derived_gold": 1,
                "derived_silver": 2,
                "draft_silver": 3,
                "gold": 1,
            },
            report["summary"]["authority_tier_counts"],
        )

    def test_pseudo_labels_are_train_only(self) -> None:
        row = make_master(
            example_id="pseudo-validation",
            task_type="tool_presence_pseudo",
            target={
                "tool": "bipolar_forceps",
                "exhaustive_visible_tool_inventory": False,
            },
            split="validation",
            authority="pseudo",
        )

        report = audit_dataset([row])

        self.assertFalse(report["ok"])
        self.assertIn(
            "pseudo_nontrain_split",
            {item["code"] for item in report["errors"]},
        )

    def test_jsonl_loader_and_cli_write_report(self) -> None:
        row = make_master(
            example_id="phase-cli",
            task_type="current_phase",
            target={"phase_id": "P03", "state": "interior"},
        )
        row["messages"] = make_message(row)["messages"]
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            master_path = temp / "master.jsonl"
            report_path = temp / "audit.json"
            master_path.write_text(
                json.dumps(row, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            loaded = load_jsonl(master_path)
            status = main(
                [
                    "--master",
                    str(master_path),
                    "--report",
                    str(report_path),
                ]
            )

            self.assertEqual(0, status)
            self.assertEqual(1, len(loaded))
            self.assertTrue(json.loads(report_path.read_text())["ok"])


if __name__ == "__main__":
    unittest.main()
