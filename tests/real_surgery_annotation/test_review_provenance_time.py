from datetime import datetime, timedelta, timezone

import pytest

from tools.real_surgery_annotation.audit_policy02_final_batch import (
    effective_action_target_issues,
    projection_operation_issues,
    review_timestamp_issues,
)
from tools.real_surgery_annotation.finalize_assistant_interaction_review import (
    FinalizationError,
    validate_reviewed_at_not_future,
)
from tools.real_surgery_annotation.repair_future_review_provenance import (
    replace_future_reviewed_at,
)


def event(event_id: str, reviewed_at: object) -> dict:
    return {
        "event_id": event_id,
        "review": {"reviewed_at": reviewed_at},
    }


def test_repair_changes_only_future_review_timestamp() -> None:
    source = [event("E1", "2026-07-29T12:00:00+09:00")]
    repaired, old_values = replace_future_reviewed_at(
        source,
        reviewed_at="2026-07-29T03:25:00+09:00",
        now_utc=datetime(2026, 7, 28, 18, 25, tzinfo=timezone.utc),
        label="test",
    )

    assert old_values == ["2026-07-29T12:00:00+09:00"]
    assert source[0]["review"]["reviewed_at"] == (
        "2026-07-29T12:00:00+09:00"
    )
    assert repaired[0]["review"]["reviewed_at"] == (
        "2026-07-29T03:25:00+09:00"
    )


def test_repair_refuses_non_future_timestamp() -> None:
    with pytest.raises(ValueError, match="is not future-dated"):
        replace_future_reviewed_at(
            [event("E1", "2026-07-29T03:00:00+09:00")],
            reviewed_at="2026-07-29T03:25:00+09:00",
            now_utc=datetime(2026, 7, 28, 18, 25, tzinfo=timezone.utc),
            label="test",
        )


def test_batch_audit_rejects_bad_review_timestamp_shapes() -> None:
    issues = review_timestamp_issues(
        (
            (
                "observed",
                [
                    event("E1", "not-a-time"),
                    event("E2", "2026-07-29T03:00:00"),
                    event("E3", None),
                ],
            ),
        )
    )

    assert [item["reason"] for item in issues] == [
        "invalid_iso8601",
        "timezone_required",
        "missing",
    ]


def test_assistant_finalizer_rejects_future_review_timestamp() -> None:
    future = (
        datetime.now(timezone.utc) + timedelta(hours=2)
    ).isoformat()

    with pytest.raises(FinalizationError, match="미래"):
        validate_reviewed_at_not_future(future, context="test")


def test_projection_audit_accepts_explicit_keep_compound_collapse_and_exclude() -> None:
    observed = [
        {"event_id": event_id}
        for event_id in ("R1", "T1", "T2", "T3", "T4", "T5")
    ]
    dt_events = [
        {"event_id": event_id}
        for event_id in ("R1", "T1", "T2", "T4")
    ]
    projection = {
        "operations": [
            {
                "operation_id": "OP1",
                "operation": "keep",
                "source_event_ids": ["R1"],
            },
            {
                "operation_id": "OP2",
                "operation": "compound_handover_chain",
                "source_event_ids": ["T1", "T2"],
                "target_event_id": "T2",
            },
            {
                "operation_id": "OP3",
                "operation": "collapse_return_chain",
                "source_event_ids": ["T3", "T4"],
                "output_event_id": "T4",
                "timestamp_source_event_id": "T4",
            },
            {
                "operation_id": "OP4",
                "operation": "exclude_non_target_event",
                "source_event_ids": ["T5"],
            },
        ]
    }

    assert projection_operation_issues(observed, dt_events, projection) == []


def test_projection_audit_rejects_duplicate_missing_and_excluded_dt_source() -> None:
    observed = [{"event_id": event_id} for event_id in ("T1", "T2", "T3")]
    dt_events = [{"event_id": "T2"}]
    projection = {
        "operations": [
            {
                "operation_id": "OP1",
                "operation": "exclude_cleanup_chain",
                "source_event_ids": ["T1", "T2"],
            },
            {
                "operation_id": "OP2",
                "operation": "keep",
                "source_event_ids": ["T1"],
            },
        ]
    }

    issues = projection_operation_issues(observed, dt_events, projection)

    assert {issue["reason"] for issue in issues} == {
        "excluded_source_present_in_dt",
        "keep_source_must_be_single_dt_output",
        "observed_events_must_map_exactly_once",
    }


def test_effective_action_audit_catches_interval_precedence_veto() -> None:
    eligibility = {
        "action": False,
        "latency": False,
        "state": False,
        "physical": False,
        "reuse": False,
        "gesture_presence": False,
        "gesture_onset": False,
        "phase_accuracy": False,
        "actor_identity": False,
    }
    event = {
        "event_id": "T1",
        "event_type": "tool_transfer",
        "time_sec": 3.0,
        "from": "scrub_nurse",
        "to": "surgeon",
        "tool": "bovie",
    }
    masks = {
        "schema": "taskplanner.evaluation_masks.v1",
        "case_id": "demo",
        "default_metric_eligibility": eligibility,
        "event_roles": [
            {
                "event_id": "T1",
                "role": "action_target",
                "metric_eligibility": {
                    **eligibility,
                    "action": True,
                    "latency": True,
                },
                "reason": "visible handover",
            }
        ],
        "interval_masks": [
            {
                "mask_id": "voice_availability",
                "start_sec": 2.0,
                "end_sec": 4.0,
                "metric_eligibility": eligibility,
                "reason": "old voice-causal veto",
            }
        ],
        "cutoffs": {},
        "tool_metric_scopes": [],
    }

    assert effective_action_target_issues(masks, {"T1": event}, {"T1"}) == [
        {
            "event_id": "T1",
            "raw_action": True,
            "effective_action": False,
            "effective_latency": False,
        }
    ]

    masks["interval_masks"][0]["metric_eligibility"].update(
        {"action": True, "latency": True}
    )
    assert effective_action_target_issues(masks, {"T1": event}, {"T1"}) == []
