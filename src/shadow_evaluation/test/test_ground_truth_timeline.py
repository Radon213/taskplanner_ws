import json
from pathlib import Path

from shadow_evaluation.interactive_replay_controller import (
    ImplicitRequestInterval,
    PhaseGroundTruthEvent,
    ShadowCaseAsset,
    ground_truth_phase_state,
    ground_truth_request_state,
    ground_truth_state_payload,
    load_implicit_request_intervals,
    load_phase_ground_truth_events,
    resolve_ground_truth_events_path,
    resolve_ground_truth_phase_path,
    resolve_shadow_case_catalog,
    validate_shadow_case_selection,
)


def test_ground_truth_loader_keeps_only_confirmed_implicit_requests(
    tmp_path,
) -> None:
    path = tmp_path / "events.jsonl"
    rows = [
        {
            "event_id": "R2",
            "event_type": "implicit_tool_request",
            "review_status": "confirmed",
            "start_sec": 4.0,
            "end_sec": 5.0,
            "tool": "must_not_leak",
        },
        {
            "event_id": "T1",
            "event_type": "tool_transfer",
            "review_status": "confirmed",
            "time_sec": 2.0,
        },
        {
            "event_id": "R1",
            "event_type": "implicit_tool_request",
            "review_status": "confirmed",
            "start_sec": 1.0,
            "end_sec": 2.0,
        },
        {
            "event_id": "R3",
            "event_type": "implicit_tool_request",
            "review_status": "provisional",
            "start_sec": 7.0,
            "end_sec": 8.0,
        },
    ]
    path.write_text(
        "\n".join(json.dumps(row) for row in rows),
        encoding="utf-8",
    )

    intervals = load_implicit_request_intervals(path)

    assert [interval.event_id for interval in intervals] == ["R1", "R2"]
    assert not hasattr(intervals[0], "tool")


def test_ground_truth_state_preserves_most_recent_interval() -> None:
    intervals = (
        ImplicitRequestInterval("R1", 1.0, 2.0),
        ImplicitRequestInterval("R2", 4.0, 5.0),
    )

    assert ground_truth_request_state(intervals, 0.5) == (None, False)
    assert ground_truth_request_state(intervals, 1.5) == (
        intervals[0],
        True,
    )
    assert ground_truth_request_state(intervals, 3.0) == (
        intervals[0],
        False,
    )
    assert ground_truth_request_state(intervals, 4.5) == (
        intervals[1],
        True,
    )


def test_resolver_selects_newest_reviewed_version(tmp_path) -> None:
    case_dir = (
        tmp_path
        / "annotations"
        / "observable_tool_events"
        / "cases"
        / "0704_6"
    )
    case_dir.mkdir(parents=True)
    old = case_dir / "interaction_events.observed.final.v2.jsonl"
    newest = case_dir / "interaction_events.observed.final.v12.jsonl"
    old.write_text("", encoding="utf-8")
    newest.write_text("", encoding="utf-8")

    resolved = resolve_ground_truth_events_path(
        case_id="0704_6",
        workspace_path=str(tmp_path),
    )

    assert resolved == newest


def test_phase_resolver_prefers_newest_provisional_final(tmp_path) -> None:
    case_dir = (
        tmp_path
        / "annotations"
        / "observable_tool_events"
        / "cases"
        / "0704_6"
    )
    case_dir.mkdir(parents=True)
    proposed = case_dir / "phase_events.generalization.proposed.v99.jsonl"
    older = case_dir / "phase_events.provisional.final.v2.jsonl"
    newest = case_dir / "phase_events.provisional.final.v12.jsonl"
    for path in (proposed, older, newest):
        path.write_text("", encoding="utf-8")

    resolved = resolve_ground_truth_phase_path(
        case_id="0704_6",
        workspace_path=str(tmp_path),
    )

    assert resolved == newest


def test_phase_loader_keeps_ambiguous_evaluation_context(tmp_path) -> None:
    path = tmp_path / "phase.jsonl"
    rows = [
        {
            "event_id": "P2",
            "event_type": "phase_start",
            "phase_id": "P04",
            "time_sec": 8.0,
            "review_status": "ambiguous",
            "phase_boundary_kind": "observed_transition",
            "tool": "must_not_leak",
        },
        {
            "event_id": "P1",
            "event_type": "phase_start",
            "phase_id": "P03",
            "time_sec": 0.0,
            "review_status": "ambiguous",
            "phase_boundary_kind": "clip_initial_state",
        },
        {
            "event_id": "P3",
            "event_type": "phase_start",
            "phase_id": "P99",
            "time_sec": 12.0,
            "review_status": "rejected",
        },
    ]
    path.write_text(
        "\n".join(json.dumps(row) for row in rows),
        encoding="utf-8",
    )

    events = load_phase_ground_truth_events(path)

    assert [event.phase_id for event in events] == ["P03", "P04"]
    assert events[1].review_status == "ambiguous"
    assert not hasattr(events[1], "tool")
    assert ground_truth_phase_state(events, 7.9) == events[0]
    assert ground_truth_phase_state(events, 8.0) == events[1]


def test_ground_truth_payload_contains_phase_and_never_tool_identity() -> None:
    payload = ground_truth_state_payload(
        run_id="run-a",
        case_id="0704_6",
        source_time_sec=4.5,
        duration_sec=12.0,
        request_intervals=(
            ImplicitRequestInterval("R1", 4.0, 5.0),
        ),
        phase_events=(
            PhaseGroundTruthEvent(
                "P1",
                "P03",
                0.0,
                "ambiguous",
                "clip_initial_state",
            ),
        ),
    )

    assert payload["evaluation_only"] is True
    assert payload["implicit_tool_request"]["active"] is True
    assert payload["phase"]["phase_id"] == "P03"
    assert payload["phase"]["active"] is True
    assert payload["phase"]["end_sec"] == 12.0
    assert payload["phase"]["review_status"] == "ambiguous"
    serialized = json.dumps(payload)
    assert "requested_tool" not in serialized
    assert "instrument_id" not in serialized
    assert "tool_id" not in serialized


def _write_case_annotations(cases_root, case_id, *, version=1) -> None:
    case_dir = cases_root / case_id
    case_dir.mkdir(parents=True)
    (case_dir / f"interaction_events.observed.final.v{version}.jsonl").write_text(
        json.dumps(
            {
                "event_id": f"{case_id}-R1",
                "event_type": "implicit_tool_request",
                "review_status": "confirmed",
                "start_sec": 1.0,
                "end_sec": 2.0,
            }
        ),
        encoding="utf-8",
    )
    (case_dir / f"phase_events.provisional.final.v{version}.jsonl").write_text(
        json.dumps(
            {
                "event_id": f"{case_id}-P1",
                "event_type": "phase_start",
                "review_status": "ambiguous",
                "phase_id": "P03",
                "phase_boundary_kind": "clip_initial_state",
                "time_sec": 0.0,
            }
        ),
        encoding="utf-8",
    )


def test_case_catalog_requires_allowlisted_bag_and_both_annotations(
    tmp_path,
) -> None:
    bags = tmp_path / "bags"
    for case_id in ("0704_6", "0704_7", "0704_18"):
        bag = bags / case_id
        bag.mkdir(parents=True)
        (bag / "metadata.yaml").write_text("rosbag2_bagfile_information: {}\n")
    cases_root = tmp_path / "annotation_cases"
    _write_case_annotations(cases_root, "0704_6", version=2)
    _write_case_annotations(cases_root, "0704_18")

    catalog = resolve_shadow_case_catalog(
        current_bag_path=bags / "0704_6",
        annotation_cases_root=str(cases_root),
    )

    assert list(catalog) == ["0704_6"]
    assert catalog["0704_6"].request_events_path.name.endswith("v2.jsonl")


def test_case_selection_rejects_running_unknown_and_traversal() -> None:
    asset = ShadowCaseAsset(
        case_id="0704_6",
        bag_path=Path("/bags/0704_6"),
        request_events_path=Path("/gt/request.jsonl"),
        phase_events_path=Path("/gt/phase.jsonl"),
    )
    catalog = {"0704_6": asset}

    for state in ("running", "held", "draining", "paused"):
        try:
            validate_shadow_case_selection(
                case_id="0704_6",
                replay_state=state,
                catalog=catalog,
            )
        except ValueError as exc:
            assert "not active or paused" in str(exc)
        else:
            raise AssertionError(f"selection unexpectedly allowed in {state}")

    for case_id in ("../0704_6", "0704_18", ""):
        try:
            validate_shadow_case_selection(
                case_id=case_id,
                replay_state="ready",
                catalog=catalog,
            )
        except ValueError as exc:
            assert "allow-listed" in str(exc)
        else:
            raise AssertionError(f"selection unexpectedly allowed: {case_id}")

    assert (
        validate_shadow_case_selection(
            case_id="0704_6",
            replay_state="completed",
            catalog=catalog,
        )
        is asset
    )
