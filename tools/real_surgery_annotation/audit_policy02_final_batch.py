#!/usr/bin/env python3
"""Fail-closed cross-case audit for finalized Policy02 evaluation references."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from tools.real_surgery_annotation.interaction_review_gui import FinalReviewBundle
from tools.real_surgery_annotation.shadow_evaluate import EvaluationMask


DEFAULT_CASES = tuple(f"0704_{index}" for index in range(7, 18))
ALLOWED_DT_EDGES = {
    ("mayo_stand", "scrub_nurse"),
    ("scrub_nurse", "surgeon"),
    ("surgeon", "mayo_stand"),
}
METRIC_KEYS = {
    "action",
    "actor_identity",
    "gesture_onset",
    "gesture_presence",
    "latency",
    "phase_accuracy",
    "physical",
    "reuse",
    "state",
}
ALLOWED_PROJECTION_OPERATIONS = {
    "keep",
    "collapse_return_chain",
    "compound_handover_chain",
    "exclude_cleanup_chain",
    "exclude_non_target_event",
}


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: JSON object required")
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        1,
    ):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number}: JSON object required")
        rows.append(value)
    return rows


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def event_sort_key(event: dict[str, Any]) -> tuple[float, int, str]:
    return (
        float(event["time_sec"]),
        int(event.get("source_frame_idx", -1)),
        str(event["event_id"]),
    )


def review_timestamp_issues(
    layers: Iterable[tuple[str, list[dict[str, Any]]]],
) -> list[dict[str, Any]]:
    """Return malformed, timezone-naive, or future review timestamps."""

    issues: list[dict[str, Any]] = []
    latest_allowed = datetime.now(timezone.utc) + timedelta(minutes=1)
    for layer, events in layers:
        for event in events:
            review = event.get("review")
            value = review.get("reviewed_at") if isinstance(review, dict) else None
            if not isinstance(value, str) or not value:
                issues.append(
                    {
                        "layer": layer,
                        "event_id": event.get("event_id"),
                        "reviewed_at": value,
                        "reason": "missing",
                    }
                )
                continue
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError:
                parsed = None
            if parsed is None:
                issues.append(
                    {
                        "layer": layer,
                        "event_id": event.get("event_id"),
                        "reviewed_at": value,
                        "reason": "invalid_iso8601",
                    }
                )
            elif parsed.tzinfo is None:
                issues.append(
                    {
                        "layer": layer,
                        "event_id": event.get("event_id"),
                        "reviewed_at": value,
                        "reason": "timezone_required",
                    }
                )
            elif parsed.astimezone(timezone.utc) > latest_allowed:
                issues.append(
                    {
                        "layer": layer,
                        "event_id": event.get("event_id"),
                        "reviewed_at": value,
                        "reason": "future_timestamp",
                    }
                )
    return issues


def add_check(
    checks: list[dict[str, Any]],
    *,
    name: str,
    ok: bool,
    details: Any,
) -> None:
    checks.append({"name": name, "ok": bool(ok), "details": details})


def effective_action_target_issues(
    masks: dict[str, Any],
    events_by_id: dict[str, dict[str, Any]],
    raw_action_target_ids: set[str],
) -> list[dict[str, Any]]:
    """Return raw action targets disabled by interval/cutoff precedence."""

    effective_mask = EvaluationMask(masks)
    issues: list[dict[str, Any]] = []
    for event_id in sorted(raw_action_target_ids):
        event = events_by_id.get(event_id)
        if event is None:
            issues.append(
                {
                    "event_id": event_id,
                    "raw_action": True,
                    "effective_action": False,
                    "effective_latency": False,
                    "reason": "event_not_found",
                }
            )
            continue
        effective = effective_mask.event_policy(event)["metric_eligibility"]
        if (
            effective["action"] is not True
            or effective["latency"] is not True
        ):
            issues.append(
                {
                    "event_id": event_id,
                    "raw_action": True,
                    "effective_action": effective["action"],
                    "effective_latency": effective["latency"],
                }
            )
    return issues


def paired_mayo_pickup_issues(
    transfers: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for index, event in enumerate(transfers):
        if (event.get("from"), event.get("to")) != (
            "mayo_stand",
            "scrub_nurse",
        ):
            continue
        later_same_tool = [
            candidate
            for candidate in transfers[index + 1 :]
            if candidate.get("tool") == event.get("tool")
            and float(candidate["time_sec"]) >= float(event["time_sec"])
        ]
        target = later_same_tool[0] if later_same_tool else None
        if (
            target is None
            or (target.get("from"), target.get("to"))
            != ("scrub_nurse", "surgeon")
            or float(target["time_sec"]) - float(event["time_sec"]) > 5.0
        ):
            issues.append(
                {
                    "pickup_event_id": event["event_id"],
                    "next_same_tool_event_id": (
                        target["event_id"] if target is not None else None
                    ),
                }
            )
    return issues


def projection_operation_issues(
    observed: list[dict[str, Any]],
    dt_events: list[dict[str, Any]],
    projection: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return explicit-projection coverage and output-semantics violations."""

    observed_ids = {str(event["event_id"]) for event in observed}
    dt_ids = {str(event["event_id"]) for event in dt_events}
    source_occurrences: Counter[str] = Counter()
    issues: list[dict[str, Any]] = []

    operations = projection.get("operations")
    if not isinstance(operations, list):
        return [{"reason": "operations_array_required"}]

    for index, operation in enumerate(operations):
        if not isinstance(operation, dict):
            issues.append(
                {
                    "operation_index": index,
                    "reason": "operation_object_required",
                }
            )
            continue
        operation_id = operation.get("operation_id")
        kind = operation.get("operation")
        source_ids = operation.get("source_event_ids")
        if kind not in ALLOWED_PROJECTION_OPERATIONS:
            issues.append(
                {
                    "operation_id": operation_id,
                    "reason": "unsupported_operation",
                    "operation": kind,
                }
            )
            continue
        if (
            not isinstance(source_ids, list)
            or not source_ids
            or not all(isinstance(event_id, str) and event_id for event_id in source_ids)
        ):
            issues.append(
                {
                    "operation_id": operation_id,
                    "reason": "nonempty_source_event_ids_required",
                }
            )
            continue

        source_occurrences.update(source_ids)
        source_set = set(source_ids)
        if kind == "keep":
            if len(source_ids) != 1 or not source_set.issubset(dt_ids):
                issues.append(
                    {
                        "operation_id": operation_id,
                        "reason": "keep_source_must_be_single_dt_output",
                        "source_event_ids": source_ids,
                    }
                )
        elif kind == "compound_handover_chain":
            target_id = operation.get("target_event_id")
            if (
                len(source_ids) < 2
                or not source_set.issubset(dt_ids)
                or target_id not in source_set
                or target_id not in dt_ids
            ):
                issues.append(
                    {
                        "operation_id": operation_id,
                        "reason": "compound_chain_sources_and_target_must_remain_in_dt",
                        "source_event_ids": source_ids,
                        "target_event_id": target_id,
                    }
                )
        elif kind == "collapse_return_chain":
            output_id = operation.get("output_event_id")
            timestamp_source_id = operation.get("timestamp_source_event_id")
            if (
                len(source_ids) < 2
                or output_id not in source_set
                or output_id not in dt_ids
                or timestamp_source_id not in source_set
                or (source_set - {str(output_id)}) & dt_ids
            ):
                issues.append(
                    {
                        "operation_id": operation_id,
                        "reason": "collapse_must_keep_only_declared_output",
                        "source_event_ids": source_ids,
                        "output_event_id": output_id,
                        "timestamp_source_event_id": timestamp_source_id,
                    }
                )
        elif source_set & dt_ids:
            issues.append(
                {
                    "operation_id": operation_id,
                    "reason": "excluded_source_present_in_dt",
                    "source_event_ids": source_ids,
                }
            )

    mapped_ids = set(source_occurrences)
    missing = sorted(observed_ids - mapped_ids)
    extra = sorted(mapped_ids - observed_ids)
    duplicates = sorted(
        event_id for event_id, count in source_occurrences.items() if count != 1
    )
    if missing or extra or duplicates:
        issues.append(
            {
                "reason": "observed_events_must_map_exactly_once",
                "missing": missing,
                "extra": extra,
                "non_singleton_source_ids": duplicates,
            }
        )
    return issues


def audit_case(
    repo_root: Path,
    case_id: str,
    *,
    manifest_name: str = "annotation_manifest.json",
) -> dict[str, Any]:
    case_dir = (
        repo_root
        / "annotations"
        / "observable_tool_events"
        / "cases"
        / case_id
    )
    if Path(manifest_name).name != manifest_name:
        raise ValueError("manifest_name must be one case-local file name")
    manifest_path = case_dir / manifest_name
    checks: list[dict[str, Any]] = []
    errors: list[str] = []

    try:
        manifest_sha256 = sha256_file(manifest_path)
        bundle = FinalReviewBundle(manifest_path=manifest_path)
    except Exception as exc:  # fail-closed report must preserve the cause
        return {
            "case_id": case_id,
            "ok": False,
            "manifest": str(manifest_path.relative_to(repo_root)),
            "checks": [],
            "errors": [f"FinalReviewBundle: {type(exc).__name__}: {exc}"],
        }

    manifest = bundle.manifest
    reference = bundle.reference
    observed = bundle.observed
    dt_events = bundle.dt_reference
    phase_events = bundle.phase_events
    voice_events = bundle.speech_events

    add_check(
        checks,
        name="manifest_complete_evaluation_only",
        ok=(
            reference.get("complete") is True
            and reference.get("information_boundary")
            == "evaluation_only_never_vlm_reducer_bt_runtime_input"
        ),
        details={
            "complete": reference.get("complete"),
            "information_boundary": reference.get("information_boundary"),
        },
    )

    scope = reference.get("evaluation_scope", {})
    add_check(
        checks,
        name="development_not_held_out",
        ok=(
            isinstance(scope, dict)
            and scope.get("classification") == "development_calibration"
            and scope.get("held_out_eligible") is False
        ),
        details=scope,
    )

    all_ids = [str(event["event_id"]) for event in observed]
    add_check(
        checks,
        name="observed_ids_unique_and_sorted",
        ok=(
            len(all_ids) == len(set(all_ids))
            and observed == sorted(observed, key=event_sort_key)
        ),
        details={"event_count": len(observed), "unique_count": len(set(all_ids))},
    )
    timestamp_issues = review_timestamp_issues(
        (
            ("observed", observed),
            ("phase", phase_events),
        )
    )
    add_check(
        checks,
        name="review_provenance_timestamp_not_future",
        ok=not timestamp_issues,
        details=timestamp_issues,
    )
    dt_ids = [str(event["event_id"]) for event in dt_events]
    add_check(
        checks,
        name="dt_ids_unique_and_sorted",
        ok=(
            len(dt_ids) == len(set(dt_ids))
            and dt_events == sorted(dt_events, key=event_sort_key)
        ),
        details={"event_count": len(dt_events), "unique_count": len(set(dt_ids))},
    )

    observed_types = Counter(str(event["event_type"]) for event in observed)
    dt_types = Counter(str(event["event_type"]) for event in dt_events)
    allowed_event_types = {"implicit_tool_request", "tool_transfer"}
    add_check(
        checks,
        name="minimal_event_types_only",
        ok=(
            set(observed_types).issubset(allowed_event_types)
            and set(dt_types).issubset(allowed_event_types)
        ),
        details={
            "observed": dict(sorted(observed_types.items())),
            "dt": dict(sorted(dt_types.items())),
        },
    )

    dt_transfers = [
        event for event in dt_events if event["event_type"] == "tool_transfer"
    ]
    dt_edges = Counter(
        (str(event["from"]), str(event["to"])) for event in dt_transfers
    )
    invalid_edges = [
        {
            "event_id": event["event_id"],
            "from": event["from"],
            "to": event["to"],
        }
        for event in dt_transfers
        if (str(event["from"]), str(event["to"])) not in ALLOWED_DT_EDGES
    ]
    add_check(
        checks,
        name="dt_edges_match_taskplanner_contract",
        ok=not invalid_edges,
        details={
            "edge_counts": {
                f"{source}->{destination}": count
                for (source, destination), count in sorted(dt_edges.items())
            },
            "invalid": invalid_edges,
        },
    )

    scrub_roundtrips: list[dict[str, str]] = []
    for first, second in zip(dt_transfers, dt_transfers[1:]):
        if (
            first.get("tool") == second.get("tool")
            and (
                first.get("from"),
                first.get("to"),
                second.get("from"),
                second.get("to"),
            )
            == (
                "mayo_stand",
                "scrub_nurse",
                "scrub_nurse",
                "mayo_stand",
            )
        ):
            scrub_roundtrips.append(
                {
                    "pickup_event_id": str(first["event_id"]),
                    "replacement_event_id": str(second["event_id"]),
                }
            )
    add_check(
        checks,
        name="no_scrub_only_mayo_roundtrip_in_dt",
        ok=not scrub_roundtrips,
        details=scrub_roundtrips,
    )

    mayo_pair_issues = paired_mayo_pickup_issues(dt_transfers)
    add_check(
        checks,
        name="mayo_pickups_close_as_compound_handover",
        ok=not mayo_pair_issues,
        details=mayo_pair_issues,
    )

    phase_ids = [str(event.get("phase_id")) for event in phase_events]
    add_check(
        checks,
        name="phase_is_provisional_ambiguous_context",
        ok=(
            reference.get("phase_reference_included") is True
            and phase_ids == ["P03", "P04", "P05", "P06"]
            and all(
                event.get("review_status") == "ambiguous"
                and event.get("event_type") == "phase_start"
                for event in phase_events
            )
            and reference.get("phase_reference", {}).get("scoring_role")
            == "context_only_not_ground_truth"
        ),
        details={
            "phase_ids": phase_ids,
            "review_statuses": [
                event.get("review_status") for event in phase_events
            ],
            "scoring_role": reference.get("phase_reference", {}).get(
                "scoring_role"
            ),
        },
    )

    invalid_voice = [
        str(event.get("event_id"))
        for event in voice_events
        if event.get("scoring_role") != "context_only_not_ground_truth"
        or event.get("source_authority") != "public_runtime_transcript"
        or float(event.get("available_sec", -1))
        < float(event.get("end_sec", 0))
    ]
    add_check(
        checks,
        name="voice_is_causal_context_only",
        ok=not invalid_voice,
        details={"voice_count": len(voice_events), "invalid_event_ids": invalid_voice},
    )

    mask_descriptor = reference.get("evaluation_masks", {})
    mask_path = case_dir / str(mask_descriptor.get("file", ""))
    try:
        if sha256_file(mask_path) != mask_descriptor.get("sha256"):
            raise ValueError("manifest-declared evaluation mask SHA-256 mismatch")
        masks = load_json(mask_path)
    except Exception as exc:
        masks = {}
        errors.append(f"evaluation masks: {type(exc).__name__}: {exc}")
    default_metrics = masks.get("default_metric_eligibility", {})
    mask_roles = masks.get("event_roles", [])
    role_ids = {
        str(role.get("event_id"))
        for role in mask_roles
        if isinstance(role, dict)
    }
    voice_role_ids = {
        str(role.get("event_id"))
        for role in masks.get("voice_context_roles", [])
        if isinstance(role, dict)
    }
    required_event_role_ids = (
        set(all_ids)
        | set(dt_ids)
        | {str(event["event_id"]) for event in phase_events}
    )
    required_voice_role_ids = {
        str(event["event_id"]) for event in voice_events
    }
    add_check(
        checks,
        name="evaluation_masks_default_deny_and_cover_all_layers",
        ok=(
            isinstance(default_metrics, dict)
            and set(default_metrics) == METRIC_KEYS
            and all(value is False for value in default_metrics.values())
            and required_event_role_ids.issubset(role_ids)
            and required_voice_role_ids.issubset(voice_role_ids)
            and masks.get("evaluation_scope", {}).get("held_out_eligible") is False
        ),
        details={
            "default_metric_eligibility": default_metrics,
            "required_event_role_count": len(required_event_role_ids),
            "covered_event_role_count": len(required_event_role_ids & role_ids),
            "required_voice_role_count": len(required_voice_role_ids),
            "covered_voice_role_count": len(
                required_voice_role_ids & voice_role_ids
            ),
        },
    )

    events_by_id = {
        str(event["event_id"]): event
        for event in [*observed, *dt_events, *phase_events]
    }
    invalid_action_roles: list[dict[str, Any]] = []
    for role in mask_roles:
        if not isinstance(role, dict):
            continue
        eligibility = role.get("metric_eligibility")
        if not isinstance(eligibility, dict):
            continue
        if not (
            eligibility.get("action") is True
            or eligibility.get("latency") is True
        ):
            continue
        event_id = str(role.get("event_id"))
        event = events_by_id.get(event_id, {})
        if (
            role.get("role") != "action_target"
            or event.get("event_type") != "tool_transfer"
            or (event.get("from"), event.get("to"))
            != ("scrub_nurse", "surgeon")
        ):
            invalid_action_roles.append(
                {
                    "event_id": event_id,
                    "role": role.get("role"),
                    "action": eligibility.get("action"),
                    "latency": eligibility.get("latency"),
                    "event_type": event.get("event_type"),
                    "from": event.get("from"),
                    "to": event.get("to"),
                }
            )
    add_check(
        checks,
        name="action_targets_are_robot_handover_arrivals_only",
        ok=not invalid_action_roles,
        details=invalid_action_roles,
    )
    raw_action_target_ids = {
        str(role.get("event_id"))
        for role in mask_roles
        if isinstance(role, dict)
        and role.get("metric_eligibility", {}).get("action") is True
    }
    try:
        ineffective_action_targets = effective_action_target_issues(
            masks,
            events_by_id,
            raw_action_target_ids,
        )
    except Exception as exc:
        ineffective_action_targets = [
            {
                "reason": "effective_mask_evaluation_failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
        ]
    add_check(
        checks,
        name="action_targets_survive_effective_mask_precedence",
        ok=not ineffective_action_targets,
        details={
            "raw_action_target_count": len(raw_action_target_ids),
            "ineffective_action_targets": ineffective_action_targets,
        },
    )

    interaction_anchor_frames: dict[int, list[dict[str, str]]] = {}
    for event in observed:
        anchors: list[tuple[str, int]] = []
        if event.get("event_type") == "implicit_tool_request":
            anchors.extend(
                (
                    (
                        "request_start",
                        int(
                            event.get(
                                "start_source_frame_idx",
                                event["source_frame_idx"],
                            )
                        ),
                    ),
                    (
                        "request_end",
                        int(
                            event.get(
                                "end_source_frame_idx",
                                event["source_frame_idx"],
                            )
                        ),
                    ),
                )
            )
        elif event.get("event_type") == "tool_transfer":
            anchors.append(("transfer", int(event["source_frame_idx"])))
        for anchor_kind, frame in anchors:
            interaction_anchor_frames.setdefault(frame, []).append(
                {
                    "event_id": str(event["event_id"]),
                    "anchor_kind": anchor_kind,
                }
            )

    invalid_phase_boundaries: list[dict[str, Any]] = []
    forbidden_mask_tokens = (
        "correction",
        "gap",
        "cleanup",
        "off_screen",
        "offscreen",
    )
    interval_masks = masks.get("interval_masks", [])
    for phase in phase_events:
        if phase.get("phase_id") == "P03":
            continue
        frame = int(phase["source_frame_idx"])
        time_sec = float(phase["time_sec"])
        collisions = interaction_anchor_frames.get(frame, [])
        forbidden_masks = [
            str(mask.get("mask_id"))
            for mask in interval_masks
            if isinstance(mask, dict)
            and any(
                token in str(mask.get("mask_id", "")).lower()
                for token in forbidden_mask_tokens
            )
            and float(mask.get("start_sec", float("inf")))
            <= time_sec
            <= float(mask.get("end_sec", float("-inf")))
        ]
        if collisions or forbidden_masks:
            invalid_phase_boundaries.append(
                {
                    "phase_id": phase.get("phase_id"),
                    "source_frame_idx": frame,
                    "time_sec": time_sec,
                    "interaction_anchor_collisions": collisions,
                    "forbidden_interval_masks": forbidden_masks,
                }
            )
    add_check(
        checks,
        name="phase_boundaries_not_event_or_mask_anchors",
        ok=not invalid_phase_boundaries,
        details=invalid_phase_boundaries,
    )

    projection_descriptor = reference.get("projection_policy_file")
    projection_path = case_dir / str(projection_descriptor or "")
    try:
        if sha256_file(projection_path) != reference.get(
            "projection_policy_sha256"
        ):
            raise ValueError(
                "manifest-declared explicit projection SHA-256 mismatch"
            )
        projection = load_json(projection_path)
        projection_issues = projection_operation_issues(
            observed,
            dt_events,
            projection,
        )
    except Exception as exc:
        projection_issues = [
            {
                "reason": "projection_load_failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
        ]
    add_check(
        checks,
        name="explicit_projection_maps_each_observed_once",
        ok=not projection_issues,
        details=projection_issues,
    )

    reconciliation_name = (
        manifest.get("minimal_interaction_annotation", {}).get(
            "policy02_reconciliation_file",
            "policy02_reconciliation_audit.final.v1.json",
        )
    )
    if (
        not isinstance(reconciliation_name, str)
        or Path(reconciliation_name).name != reconciliation_name
    ):
        reconciliation_name = ""
        errors.append(
            "reconciliation audit: manifest reference must be one case-local file name"
        )
    audit_path = case_dir / reconciliation_name
    try:
        reconciliation_sha256 = manifest.get(
            "minimal_interaction_annotation",
            {},
        ).get("policy02_reconciliation_sha256")
        if sha256_file(audit_path) != reconciliation_sha256:
            raise ValueError(
                "manifest-declared reconciliation SHA-256 mismatch"
            )
        reconciliation = load_json(audit_path)
    except Exception as exc:
        reconciliation = {}
        errors.append(f"reconciliation audit: {type(exc).__name__}: {exc}")
    coverage = reconciliation.get("coverage", {})
    continuity = reconciliation.get("physical_continuity", {})
    add_check(
        checks,
        name="proposal_and_voice_review_exhaustive",
        ok=(
            coverage.get("coarse_review_complete") is True
            and coverage.get("policy02_candidate_clusters_reviewed")
            == coverage.get("policy02_candidate_clusters_total")
            and coverage.get("voice_only_false_negative_windows_reviewed")
            == coverage.get("voice_only_false_negative_windows_total")
        ),
        details={
            key: coverage.get(key)
            for key in (
                "coarse_review_complete",
                "policy02_candidate_clusters_reviewed",
                "policy02_candidate_clusters_total",
                "voice_only_false_negative_windows_reviewed",
                "voice_only_false_negative_windows_total",
            )
        },
    )
    add_check(
        checks,
        name="physical_continuity_fail_closed",
        ok=(
            isinstance(continuity, dict)
            and str(continuity.get("result", "")).startswith("pass")
            and bool(str(continuity.get("teleportation_check", "")).strip())
        ),
        details=continuity,
    )

    observed_confirmed = observed_types.get("implicit_tool_request", 0) + (
        observed_types.get("tool_transfer", 0)
    )
    dt_confirmed = dt_types.get("implicit_tool_request", 0) + dt_types.get(
        "tool_transfer",
        0,
    )
    add_check(
        checks,
        name="manifest_counts_match_loaded_layers",
        ok=(
            reference.get("observed_reference", {}).get("confirmed_event_count")
            == observed_confirmed
            and reference.get("dt_reference", {}).get("confirmed_event_count")
            == dt_confirmed
            and reference.get("phase_reference", {}).get("event_count")
            == len(phase_events)
        ),
        details={
            "loaded_observed": observed_confirmed,
            "loaded_dt": dt_confirmed,
            "loaded_phase": len(phase_events),
        },
    )
    artifact_stability: list[dict[str, Any]] = []
    for label, path, expected in (
        (
            "assistant_adjudication",
            bundle.adjudication_path,
            bundle.adjudication_sha256,
        ),
        (
            "explicit_projection",
            bundle.projection_policy_path,
            bundle.projection_policy_sha256,
        ),
        (
            "evaluation_masks",
            bundle.evaluation_masks_path,
            bundle.evaluation_masks_sha256,
        ),
        (
            "policy02_reconciliation",
            bundle.reconciliation_path,
            bundle.reconciliation_sha256,
        ),
    ):
        try:
            actual = sha256_file(path)
        except OSError as exc:
            actual = ""
            errors.append(
                f"{label} stability check: {type(exc).__name__}: {exc}"
            )
        artifact_stability.append(
            {
                "artifact": label,
                "path": str(path.relative_to(repo_root)),
                "expected_sha256": expected,
                "actual_sha256": actual,
                "ok": actual == expected,
            }
        )
    add_check(
        checks,
        name="manifest_bound_policy_artifacts_stable_during_audit",
        ok=all(item["ok"] for item in artifact_stability),
        details=artifact_stability,
    )
    try:
        final_manifest_sha256 = sha256_file(manifest_path)
    except OSError as exc:
        final_manifest_sha256 = ""
        errors.append(
            f"manifest stability check: {type(exc).__name__}: {exc}"
        )
    add_check(
        checks,
        name="manifest_stable_during_audit",
        ok=final_manifest_sha256 == manifest_sha256,
        details={
            "manifest_sha256_at_load": manifest_sha256,
            "manifest_sha256_at_completion": final_manifest_sha256,
        },
    )

    case_ok = not errors and all(check["ok"] for check in checks)
    return {
        "case_id": case_id,
        "ok": case_ok,
        "manifest": str(manifest_path.relative_to(repo_root)),
        "manifest_sha256": manifest_sha256,
        "bundle_revision": bundle.revision,
        "counts": {
            "observed": len(observed),
            "dt_reference": len(dt_events),
            "phase": len(phase_events),
            "voice": len(voice_events),
            "gesture_targets": sum(
                1
                for role in mask_roles
                if isinstance(role, dict)
                and role.get("metric_eligibility", {}).get("gesture_presence")
                is True
            ),
            "action_targets": sum(
                1
                for role in mask_roles
                if isinstance(role, dict)
                and role.get("metric_eligibility", {}).get("action") is True
            ),
            "effective_action_targets": (
                len(raw_action_target_ids) - len(ineffective_action_targets)
                if not (
                    len(ineffective_action_targets) == 1
                    and ineffective_action_targets[0].get("reason")
                    == "effective_mask_evaluation_failed"
                )
                else 0
            ),
        },
        "checks": checks,
        "errors": errors,
    }


def audit_batch(
    repo_root: Path,
    case_ids: Iterable[str],
    *,
    manifest_name: str = "annotation_manifest.json",
) -> dict[str, Any]:
    cases = [
        audit_case(repo_root, case_id, manifest_name=manifest_name)
        for case_id in case_ids
    ]
    return {
        "schema": "taskplanner.policy02_final_batch_audit.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "authority": "deterministic_read_only_validation",
        "information_boundary": "evaluation_only",
        "cases": cases,
        "counts": {
            "case_count": len(cases),
            "passed_case_count": sum(case["ok"] for case in cases),
            "failed_case_count": sum(not case["ok"] for case in cases),
            "observed_event_count": sum(
                int(case.get("counts", {}).get("observed", 0)) for case in cases
            ),
            "dt_reference_event_count": sum(
                int(case.get("counts", {}).get("dt_reference", 0))
                for case in cases
            ),
            "phase_event_count": sum(
                int(case.get("counts", {}).get("phase", 0)) for case in cases
            ),
            "voice_event_count": sum(
                int(case.get("counts", {}).get("voice", 0)) for case in cases
            ),
        },
        "ok": all(case["ok"] for case in cases),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument(
        "--cases",
        nargs="+",
        default=list(DEFAULT_CASES),
        help="Case IDs; defaults to 0704_7 through 0704_17.",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--manifest-name",
        default="annotation_manifest.json",
        help=(
            "Case-local manifest to audit. Versioned references can be checked "
            "before the canonical annotation_manifest.json pointer is switched."
        ),
    )
    args = parser.parse_args()

    repo_root = args.repo.resolve()
    report = audit_batch(
        repo_root,
        args.cases,
        manifest_name=args.manifest_name,
    )
    rendered = json.dumps(
        report,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    if args.output is not None:
        output = args.output.resolve()
        if output.exists():
            raise SystemExit(f"create-only output already exists: {output}")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
