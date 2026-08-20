from __future__ import annotations

import sys
from pathlib import Path


TASK_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK_DIR))

import next_event_state_eval as next_event  # noqa: E402


def test_confirmed_handover_events_filters_and_sorts() -> None:
    events = [
        {
            "event_type": "tool_transfer",
            "review_status": "confirmed",
            "time_sec": 2.0,
            "from": "scrub_nurse",
            "to": "surgeon",
            "tool": "bovie",
        },
        {
            "event_type": "tool_transfer",
            "review_status": "confirmed",
            "time_sec": 1.0,
            "from": "scrub_nurse",
            "to": "surgeon",
            "tool": "adson_forceps",
        },
        {
            "event_type": "tool_transfer",
            "review_status": "confirmed",
            "time_sec": 3.0,
            "from": "surgeon",
            "to": "scrub_nurse",
            "tool": "bovie",
        },
    ]
    rows = next_event.confirmed_handover_events(events)
    assert [row["tool"] for row in rows] == ["adson_forceps", "bovie"]


def test_message_has_no_time_horizon_none_class_or_media_payload() -> None:
    context = {
        "task": "first future scrub-nurse-to-surgeon handover tool; elapsed time is irrelevant",
        "procedure": "Open Thyroidectomy Demonstration",
        "current_functional_phase": "P03",
        "completed_handover_count": 1,
        "complete_handover_history": ["adson_forceps"],
        "event_sourced_surgeon_owned": [{"tool_id": "adson_forceps", "count": 1}],
        "last_incoming_tool": "adson_forceps",
        "authored_protocol_exchange_paths": {"primary": [["adson_forceps", "bovie"]]},
        "authored_phase_conditioned_transitions": [
            {"current": "adson_forceps", "next": "bovie", "strength": "high"}
        ],
    }
    messages = next_event.build_messages(context)
    rendered = next_event.canonical_json(messages)
    assert [message["role"] for message in messages] == ["system", "user"]
    assert "future handover is guaranteed" in rendered
    assert "image_url" not in rendered
    assert "0704_" not in rendered
    assert "target_tool_id" not in rendered


def test_prediction_contract_rejects_none_and_extra_keys() -> None:
    valid, error = next_event.validate_prediction({"tool_id": "bovie", "confidence": 0.8})
    assert not error and valid == {"tool_id": "bovie", "confidence": 0.8}
    invalid, error = next_event.validate_prediction({"tool_id": "", "confidence": 0.8})
    assert invalid is None and "allowed" in error
    invalid, error = next_event.validate_prediction(
        {"tool_id": "bovie", "confidence": 0.8, "decision": "handover"}
    )
    assert invalid is None and "exactly" in error


def test_ngram_baseline_excludes_query_case() -> None:
    def row(identifier: str, case: str, history: list[str]) -> dict:
        return {
            "example_id": identifier,
            "provenance": {"case_id": case},
            "model_context": {
                "current_functional_phase": "P03",
                "complete_handover_history": history,
            },
        }

    query = row("q", "0704_6", ["adson_forceps"])
    training = [
        row("same", "0704_6", ["adson_forceps"]),
        row("other", "0704_7", ["adson_forceps"]),
    ]
    labels = {
        "same": {"target_tool_id": "bovie"},
        "other": {"target_tool_id": "bipolar_forceps"},
    }
    prediction = next_event.ngram_prediction(
        query, training, labels, excluded_case="0704_6"
    )
    assert prediction["tool_id"] == "bipolar_forceps"


def test_real_partition_has_one_target_per_handover_and_no_terminal_none() -> None:
    protocol = next_event.compact_protocol()
    inputs, labels, _integrity = next_event.build_partition_rows(["0704_6"], protocol)
    assert len(inputs) == len(labels) == 10
    assert inputs[0]["query_kind"] == "procedure_start"
    assert inputs[-1]["model_context"]["completed_handover_count"] == 9
    assert all(label["target_tool_id"] in next_event.TOOL_ID_SET for label in labels)
    assert all(label["delay_to_next_event_sec"] > 0 for label in labels)
    for input_row, label in zip(inputs, labels):
        history = input_row["model_context"]["complete_handover_history"]
        assert label["target_tool_id"] not in str(input_row.get("target_tool_id", ""))
        assert len(history) == label["target_index"]
