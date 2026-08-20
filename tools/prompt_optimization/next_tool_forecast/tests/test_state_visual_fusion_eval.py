from __future__ import annotations

import sys
from pathlib import Path


TASK_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK_DIR))

import state_visual_fusion_eval as fusion  # noqa: E402


def test_enhanced_surgeon_state_excludes_future_transfer() -> None:
    events = [
        {
            "event_type": "tool_transfer",
            "review_status": "confirmed",
            "time_sec": 2.0,
            "event_id": "past",
            "from": "scrub_nurse",
            "to": "surgeon",
            "tool": "bovie",
        },
        {
            "event_type": "tool_transfer",
            "review_status": "confirmed",
            "time_sec": 5.0,
            "event_id": "future",
            "from": "scrub_nurse",
            "to": "surgeon",
            "tool": "bipolar_forceps",
        },
    ]
    state = fusion.enhanced_surgeon_state(events, 3.0)
    assert state["recent_incoming_tools"] == ["bovie"]
    assert state["seconds_since_last_incoming"] == 1.0
    assert state["last_incoming_tool"] == "bovie"


def test_retrieval_excludes_same_case_and_returns_anonymized_neighbor() -> None:
    def context(phase: str, history: list[str], age: float | None) -> dict:
        return {
            "phase_id": phase,
            "retrieval_history": history,
            "last_incoming_tool": history[-1] if history else "",
            "recent_incoming_tools": history[-4:],
            "seconds_since_last_incoming": age,
        }

    contexts = {
        "q": context("P03", ["bovie"], 2.0),
        "same": context("P03", ["bovie"], 2.0),
        "other": context("P03", ["bovie"], 2.0),
        "other2": context("P03", ["adson_forceps"], 2.0),
        "other3": context("P04", ["bovie"], 1.0),
        "other4": context("P03", [], None),
        "other5": context("P05", ["bovie"], 2.0),
    }
    rows = [
        {"example_id": "same", "provenance": {"case_id": "0704_1", "cutoff_sec": 1.0}},
        {"example_id": "other", "provenance": {"case_id": "0704_2", "cutoff_sec": 1.0}},
        {"example_id": "other2", "provenance": {"case_id": "0704_3", "cutoff_sec": 1.0}},
        {"example_id": "other3", "provenance": {"case_id": "0704_4", "cutoff_sec": 1.0}},
        {"example_id": "other4", "provenance": {"case_id": "0704_5", "cutoff_sec": 1.0}},
        {"example_id": "other5", "provenance": {"case_id": "0704_6", "cutoff_sec": 1.0}},
    ]
    labels = {
        row["example_id"]: {"target": {"decision": "handover", "tool_id": "bovie"}}
        for row in rows
    }
    prior = fusion.retrieve_prior(
        query_id="q", query_case="0704_1", train_rows=rows, labels=labels, contexts=contexts
    )
    assert len(prior["neighbors"]) == 5
    assert prior["neighbors"][0]["observed_following_outcome"] == "bovie"
    assert "case_id" not in str(prior)


def test_fusion_messages_have_paired_images_without_source_identity() -> None:
    messages = fusion.build_messages(
        "SUPPLIED_STATE_AND_PATTERN_JSON:{\"phase_id\":\"P03\"}",
        [
            ("flir", "data:image/jpeg;base64,AA=="),
            ("cam4", "data:image/jpeg;base64,AA=="),
            ("flir", "data:image/jpeg;base64,AA=="),
            ("cam4", "data:image/jpeg;base64,AA=="),
        ],
    )
    assert [row["role"] for row in messages] == ["system", "user"]
    assert len(messages[1]["content"]) == 9
    assert "0704_" not in fusion.canonical_json(messages)


def test_visible_available_variant_requires_a_distinct_visible_instance() -> None:
    system, _developer = fusion.prompt_pair("state_visual_retrieval_v3_visible_available")
    assert "distinct available instance" in system
    assert "missing inventory information" in system
    state = fusion.state_text(
        context={
            "phase_id": "P03",
            "event_sourced_surgeon_owned": [{"tool_id": "adson_forceps", "count": 1}],
            "last_incoming_tool": "adson_forceps",
            "recent_incoming_tools": ["adson_forceps"],
            "seconds_since_last_incoming": 1.0,
            "seconds_since_any_transfer": 1.0,
        },
        protocol={
            "procedure_name": "Open Thyroidectomy Demonstration",
            "handover_paths": {"primary": [], "alternatives": []},
            "phase_transitions": {"P03": []},
        },
        retrieval={"vote_outcomes": [], "neighbors": []},
        variant="state_visual_retrieval_v3_visible_available",
    )
    assert "not instance IDs" in state
