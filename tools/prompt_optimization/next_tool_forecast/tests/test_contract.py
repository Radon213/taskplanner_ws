from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


TASK_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK_DIR))

from build_eval_manifest import (  # noqa: E402
    all_transfer_rows,
    build_case_rows,
    case_split,
    causal_asr_timestamped,
    first_future_transfer,
    parse_cases,
)
from prompt_contract import (  # noqa: E402
    build_messages,
    output_contract_name,
    prompts,
    validate_prediction,
)


def test_model_messages_exclude_labels_case_identity_and_absolute_time() -> None:
    messages = build_messages(
        variant="optimized_v1",
        frame_offsets_sec=(-6.0, -3.0, 0.0),
        public_asr=("public transcript",),
        images=(
            ("flir", "data:image/jpeg;base64,AA=="),
            ("cam4", "data:image/jpeg;base64,AA=="),
            ("flir", "data:image/jpeg;base64,AA=="),
            ("cam4", "data:image/jpeg;base64,AA=="),
            ("flir", "data:image/jpeg;base64,AA=="),
            ("cam4", "data:image/jpeg;base64,AA=="),
        ),
    )
    rendered = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
    assert "case_id" not in rendered
    assert "ground_truth" not in rendered
    assert "target_event" not in rendered
    assert "absolute" in rendered  # Explicitly prohibited, rather than supplied.
    assert [message["role"] for message in messages] == ["system", "user"]
    assert "OUTPUT CONTRACT" in messages[0]["content"]
    assert len(messages[-1]["content"]) == 13


def test_validation_is_strict_about_non_handover_tool_ids() -> None:
    valid, error = validate_prediction(
        {
            "decision": "handover",
            "tool_id": "bovie",
            "confidence": 0.8,
            "uncertainty": 0.2,
        }
    )
    assert error == ""
    assert valid is not None
    invalid, error = validate_prediction(
        {
            "decision": "none",
            "tool_id": "bovie",
            "confidence": 0.8,
            "uncertainty": 0.2,
        }
    )
    assert invalid is None
    assert error == "non_handover_tool_id"


def test_procedure_prior_variant_is_case_agnostic_and_not_a_fixed_sequence() -> None:
    system, developer = prompts("optimized_v2_prior")
    assert "army_navy_retractor" in system
    assert "never a required order" in system
    assert "0704" not in system
    assert "handover_patterns" not in system
    assert "decision" in developer


def test_v3_strict_and_diagnostic_contracts_preserve_the_causal_evidence_rule() -> None:
    strict_system, strict_developer = prompts("optimized_v3")
    diagnostic_system, diagnostic_developer = prompts("optimized_v3_diagnostic")
    assert strict_system == diagnostic_system
    assert "unfulfilled explicit request" in strict_developer
    assert "stale ASR request" in strict_developer
    assert "fresh_asr_visual" not in strict_developer
    assert "fresh_asr_visual" in diagnostic_developer
    assert output_contract_name("optimized_v3") == "deployable_four_key"
    assert output_contract_name("optimized_v3_diagnostic") == "diagnostic_five_key"


def test_v3_diagnostic_evidence_type_is_strictly_consistent_with_decision() -> None:
    valid, error = validate_prediction(
        {
            "decision": "handover",
            "tool_id": "bovie",
            "confidence": 0.8,
            "uncertainty": 0.2,
            "evidence_type": "fresh_asr_visual",
        },
        variant="optimized_v3_diagnostic",
    )
    assert error == ""
    assert valid is not None
    assert valid["evidence_type"] == "fresh_asr_visual"
    invalid, error = validate_prediction(
        {
            "decision": "none",
            "tool_id": "",
            "confidence": 0.8,
            "uncertainty": 0.2,
            "evidence_type": "visual_only",
        },
        variant="optimized_v3_diagnostic",
    )
    assert invalid is None
    assert error == "non_handover_evidence_type"


def test_timestamped_asr_request_exposes_only_relative_causal_offsets() -> None:
    messages = build_messages(
        variant="optimized_v3",
        frame_offsets_sec=(-6.0, -3.0, 0.0),
        public_asr=(
            {"text": "Adson", "available_offset_sec": -1.238},
            {"text": "Adson 하나 더", "available_offset_sec": -0.449},
        ),
        images=(
            ("flir", "data:image/jpeg;base64,AA=="),
            ("cam4", "data:image/jpeg;base64,AA=="),
            ("flir", "data:image/jpeg;base64,AA=="),
            ("cam4", "data:image/jpeg;base64,AA=="),
            ("flir", "data:image/jpeg;base64,AA=="),
            ("cam4", "data:image/jpeg;base64,AA=="),
        ),
    )
    user_text = messages[1]["content"][0]["text"]
    assert '"available_offset_sec":-0.449' in user_text
    assert '"available_sec":' not in user_text
    assert "case_id" not in user_text
    assert "cutoff_sec" not in user_text
    assert "0704_" not in user_text


def test_timestamped_asr_builder_sorts_and_removes_absolute_time() -> None:
    rows = causal_asr_timestamped(
        [
            {"text": "new", "available_sec": 9.551},
            {"text": "older", "available_sec": 8.762},
            {"text": "future", "available_sec": 10.1},
        ],
        cutoff_sec=10.0,
    )
    assert rows == [
        {"text": "older", "available_offset_sec": -1.238},
        {"text": "new", "available_offset_sec": -0.449},
    ]
    assert all(set(row) == {"text", "available_offset_sec"} for row in rows)


def test_unresolved_transfer_blocks_a_false_none_target() -> None:
    events = [
        {
            "case_id": "0704_6",
            "event_id": "0704_6-T0001",
            "event_type": "tool_transfer",
            "from": "scrub_nurse",
            "to": "surgeon",
            "review_status": "confirmed",
            "time_sec": 4.0,
            "tool": "retractor_bundle_unresolved",
        },
        {
            "case_id": "0704_6",
            "event_id": "0704_6-T0002",
            "event_type": "tool_transfer",
            "from": "scrub_nurse",
            "to": "surgeon",
            "review_status": "confirmed",
            "time_sec": 6.0,
            "tool": "bovie",
        },
    ]
    transfers = all_transfer_rows(events, "0704_6")
    assert [row["tool"] for row in transfers] == ["retractor_bundle_unresolved", "bovie"]
    assert first_future_transfer(transfers, 0.0)["tool"] == "retractor_bundle_unresolved"


def test_input_and_label_records_keep_future_target_separate() -> None:
    timestamps = [float(index) for index in range(21)]
    event_id = "0704_6-T0010"
    events = [
        {
            "case_id": "0704_6",
            "event_id": event_id,
            "event_type": "tool_transfer",
            "from": "scrub_nurse",
            "to": "surgeon",
            "review_status": "confirmed",
            "source_frame_idx": 10,
            "time_sec": 10.0,
            "tool": "bovie",
        }
    ]
    media = {
        "views": {
            "flir": {"path": "/tmp/flir.mp4", "sha256": "f" * 64},
            "cam4": {"path": "/tmp/cam4.mp4", "sha256": "c" * 64},
        }
    }
    inputs, labels = build_case_rows(
        case_id="0704_6",
        events=events,
        voices=[],
        timestamps=timestamps,
        gaps=[],
        media=media,
        lead_sec=4.0,
        negative_stride_sec=6.0,
        negative_ratio=0.0,
        seed=1,
    )
    assert len(inputs) == len(labels) == 1
    assert "target" not in inputs[0]
    assert event_id not in json.dumps(inputs[0])
    assert labels[0]["target"]["event_id"] == event_id
    assert labels[0]["target"]["tool_id"] == "bovie"


def test_development_temporal_partitions_have_a_no_touch_embargo() -> None:
    timestamps = [float(index) for index in range(101)]
    # Center boundary is 50 s.  Calibration's future label must finish by 46 s;
    # challenge's earliest image must start at or after 54 s.
    assert case_split("0704_6", 38.0, timestamps) == "development_calibration"
    assert case_split("0704_6", 60.0, timestamps) == "development_challenge"
    assert case_split("0704_6", 50.0, timestamps) == "development_embargoed"
    assert case_split("0704_15", 50.0, timestamps) == "final_holdout"


def test_incomplete_0704_5_cannot_enter_the_benchmark_builder() -> None:
    with pytest.raises(Exception, match="unsupported case IDs"):
        parse_cases("0704_5")
