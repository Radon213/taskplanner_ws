from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path


TASK_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK_DIR))

import state_context_eval as state  # noqa: E402


def test_surgeon_state_replays_only_past_confirmed_transfers() -> None:
    events = [
        {
            "event_type": "tool_transfer",
            "review_status": "confirmed",
            "time_sec": 1.0,
            "event_id": "a",
            "from": "scrub_nurse",
            "to": "surgeon",
            "tool": "bovie",
        },
        {
            "event_type": "tool_transfer",
            "review_status": "confirmed",
            "time_sec": 2.0,
            "event_id": "b",
            "from": "surgeon",
            "to": "mayo_stand",
            "tool": "bovie",
        },
        {
            "event_type": "tool_transfer",
            "review_status": "confirmed",
            "time_sec": 3.0,
            "event_id": "future",
            "from": "scrub_nurse",
            "to": "surgeon",
            "tool": "bipolar_forceps",
        },
    ]
    context = state.surgeon_state(events, 2.5)
    assert context["last_incoming_tool"] == "bovie"
    assert context["recent_incoming_tools"] == ["bovie"]
    assert context["event_sourced_surgeon_owned"] == [{"tool_id": "allis_forceps", "count": 2}]


def test_candidate_distribution_uses_specific_then_fallback() -> None:
    context = {"phase_id": "P03", "recent_incoming_tools": ["adson_forceps", "bovie"]}
    table = {
        2: {("P03", ("adson_forceps", "bovie")): Counter({"bipolar_forceps": 2, "none": 1})},
        1: {("P03", ("bovie",)): Counter({"none": 9})},
        0: {("P03",): Counter({"adson_forceps": 8})},
        -1: {tuple(): Counter({"none": 10})},
    }
    selected = state.candidate_distribution(context, table)
    assert selected["matching_rule"] == "phase + two most recent incoming tools"
    assert selected["support"] == 3
    assert selected["outcomes"][0]["outcome"] == "bipolar_forceps"


def test_model_messages_cannot_contain_case_or_target_provenance() -> None:
    messages = state.build_messages("SUPPLIED_STATE_JSON:{\"phase_id\":\"P03\"}")
    rendered = state.canonical_json(messages)
    assert "case_id" not in rendered
    assert "cutoff" not in rendered
    assert "target" not in rendered
    assert [message["role"] for message in messages] == ["system", "user"]


def test_authored_state_only_policy_has_no_visual_or_calibration_requirement() -> None:
    text = state.state_user_text(
        context={
            "phase_id": "P03",
            "event_sourced_surgeon_owned": [{"tool_id": "adson_forceps", "count": 1}],
            "last_incoming_tool": "adson_forceps",
            "recent_incoming_tools": ["adson_forceps"],
        },
        protocol={
            "handover_paths": {"primary": [["adson_forceps", "bovie"]], "alternatives": []},
            "phase_transitions": {"P03": [{"current": "adson_forceps", "next": "bovie"}]},
        },
        variant="procedure_pattern_v3_authored_state_only",
        distribution=None,
    )
    assert "cross_case_calibration_transition_prior" not in text
    assert "Do not require visual confirmation" in text
    messages = state.build_messages(text)
    rendered = state.canonical_json(messages)
    assert "image_url" not in rendered
    assert "data:image" not in rendered


def test_deterministic_baseline_preserves_none_and_tool_outcomes() -> None:
    none = state.deterministic_prediction({"outcomes": [{"outcome": "none", "count": 2}]})
    tool = state.deterministic_prediction({"outcomes": [{"outcome": "bovie", "count": 2}]})
    assert none["decision"] == "none"
    assert tool["decision"] == "handover"
    assert tool["tool_id"] == "bovie"
