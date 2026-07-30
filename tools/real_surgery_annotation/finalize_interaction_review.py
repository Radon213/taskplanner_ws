#!/usr/bin/env python3
"""Finalize reviewed interactions and derive a Taskplanner-DT reference.

The append-only human action log remains the audit source of truth.  This
module materializes only confirmed observable interactions, then applies a
separate deterministic policy projection for evaluation.  It never rewrites
candidate or human-review inputs.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from .interaction_review_gui import InputError, ReviewStore, canonical_json
from .validate_interaction_points import validate_timeline


POLICY_SCHEMAS = frozenset(
    (
        "taskplanner.dt_interaction_projection_policy.v1",
        "taskplanner.dt_interaction_projection_policy.v2",
    )
)
REPORT_SCHEMA = "taskplanner.dt_interaction_projection_report.v1"
POINT_SCHEMA = "taskplanner.observable_interaction_point.v1"
POINT_SCHEMA_V2 = "taskplanner.observable_interaction_point.v2"
INTERVAL_SCHEMA = "taskplanner.observable_interaction_interval.v1"
ASSISTANT_CORRECTION_SCHEMAS = frozenset(
    (
        "taskplanner.assistant_annotation_adjudication.v1",
        "taskplanner.assistant_annotation_adjudication.v2",
    )
)
INTERACTION_TYPES = {"implicit_tool_request", "tool_transfer"}
PHASE_TYPE = "phase_start"


class FinalizationError(Exception):
    """The reviewed source or projection policy is not safe to finalize."""


def reject_nonstandard_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON numeric constant: {value}")


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=reject_nonstandard_json_constant,
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise FinalizationError(f"{path}: JSON 오류: {exc}") from exc
    if not isinstance(value, dict):
        raise FinalizationError(f"{path}: JSON 객체가 필요합니다.")
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise FinalizationError(f"{path}: 파일을 읽을 수 없습니다: {exc}") from exc
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(
                line,
                parse_constant=reject_nonstandard_json_constant,
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise FinalizationError(
                f"{path}:{line_number}: JSON 오류: {exc}"
            ) from exc
        if not isinstance(value, dict):
            raise FinalizationError(
                f"{path}:{line_number}: JSON 객체가 필요합니다."
            )
        records.append(value)
    return records


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise FinalizationError(f"{path}: 파일을 읽을 수 없습니다: {exc}") from exc
    return digest.hexdigest()


def encode_jsonl(records: list[dict[str, Any]]) -> bytes:
    return "".join(canonical_json(record) + "\n" for record in records).encode(
        "utf-8"
    )


def encode_json(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _clean_fields(fields: dict[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(value)
        for key, value in fields.items()
        if value is not None
    }


def _effective_decision_map(
    state: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    effective: dict[str, dict[str, Any]] = {}
    for candidate in state["candidates"]:
        decision = candidate["_review_ui"]["effective_decision"]
        if decision is not None:
            effective[str(candidate["_review_ui"]["candidate_id"])] = decision
    for annotation in state["human_annotations"]:
        effective[str(annotation["event_id"])] = annotation[
            "_review_ui"
        ]["effective_decision"]
    return effective


def apply_assistant_corrections(
    *,
    state: dict[str, Any],
    store: ReviewStore,
    corrections: list[dict[str, Any]],
    correction_schema: dict[str, Any],
    effective_decisions: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Overlay authorized assistant corrections without mutating human logs."""

    case_id = str(state["case_id"])
    correction_schema_id = str(correction_schema.get("$id", ""))
    if correction_schema_id not in ASSISTANT_CORRECTION_SCHEMAS:
        raise FinalizationError(
            "assistant correction schema $id가 일치하지 않습니다."
        )
    effective = copy.deepcopy(
        effective_decisions
        if effective_decisions is not None
        else _effective_decision_map(state)
    )
    validator = jsonschema.Draft202012Validator(
        correction_schema,
        format_checker=jsonschema.FormatChecker(),
    )
    errors: list[str] = []
    seen_correction_ids: set[str] = set()
    seen_annotation_ids: set[str] = set()
    applied: list[dict[str, Any]] = []

    for line_number, correction in enumerate(corrections, 1):
        for violation in validator.iter_errors(correction):
            path = ".".join(str(part) for part in violation.absolute_path)
            errors.append(
                f"correction line {line_number}: schema "
                f"{path or '<record>'}: {violation.message}"
            )
        if correction.get("schema") != correction_schema_id:
            errors.append(
                f"correction line {line_number}: schema ID 불일치"
            )
        if correction.get("case_id") != case_id:
            errors.append(
                f"correction line {line_number}: case_id 불일치"
            )
        correction_id = str(correction.get("correction_id", ""))
        annotation_id = str(correction.get("annotation_id", ""))
        if correction_id in seen_correction_ids:
            errors.append(
                f"correction line {line_number}: duplicate correction_id "
                f"{correction_id}"
            )
        seen_correction_ids.add(correction_id)
        if annotation_id in seen_annotation_ids:
            errors.append(
                f"correction line {line_number}: annotation별 correction은 "
                f"한 개만 허용됩니다: {annotation_id}"
            )
        seen_annotation_ids.add(annotation_id)

        source_decision = effective.get(annotation_id)
        if source_decision is None:
            errors.append(
                f"correction line {line_number}: 원본 annotation을 찾을 수 "
                f"없습니다: {annotation_id}"
            )
            continue
        source_action_id = str(
            source_decision.get("action_id")
            or source_decision.get("decision_id")
            or ""
        )
        if correction.get("supersedes_action_id") != source_action_id:
            errors.append(
                f"correction line {line_number}: supersedes_action_id가 "
                f"현재 결정 {source_action_id}와 다릅니다."
            )
        source_sha256 = hashlib.sha256(
            canonical_json(source_decision).encode("utf-8")
        ).hexdigest()
        if correction.get("source_action_sha256") != source_sha256:
            errors.append(
                f"correction line {line_number}: source action hash가 "
                "현재 결정과 다릅니다."
            )

        raw_fields = correction.get("adjudicated_fields")
        request_interval = (
            isinstance(raw_fields, dict)
            and raw_fields.get("event_type") == "implicit_tool_request"
            and (
                "start_source_frame_idx" in raw_fields
                or "end_source_frame_idx" in raw_fields
            )
        )
        try:
            canonical_fields = store._validated_fields(
                raw_fields,
                request_interval=request_interval,
            )
        except InputError as exc:
            errors.append(
                f"correction line {line_number}: adjudicated_fields 오류: {exc}"
            )
            continue
        if canonical_json(raw_fields) != canonical_json(canonical_fields):
            errors.append(
                f"correction line {line_number}: adjudicated_fields는 "
                "timeline에서 정규화된 값과 정확히 일치해야 합니다."
            )
            continue
        presentation = correction.get("review_presentation")
        if presentation is not None:
            evidence_start = presentation.get(
                "evidence_start_source_frame_idx"
            )
            evidence_end = presentation.get(
                "evidence_end_source_frame_idx"
            )
            if (
                isinstance(evidence_start, bool)
                or not isinstance(evidence_start, int)
                or isinstance(evidence_end, bool)
                or not isinstance(evidence_end, int)
                or not 0 <= evidence_start <= evidence_end < len(store.timestamps)
            ):
                errors.append(
                    f"correction line {line_number}: review_presentation "
                    "evidence frame 범위가 올바르지 않습니다."
                )
                continue
            anchor_frame = canonical_fields["source_frame_idx"]
            if not evidence_start <= anchor_frame <= evidence_end:
                errors.append(
                    f"correction line {line_number}: adjudicated anchor가 "
                    "review_presentation evidence 범위 밖입니다."
                )
                continue

        overlaid = {
            "action_id": correction_id,
            "annotation_id": annotation_id,
            "adjudicated_fields": copy.deepcopy(canonical_fields),
            "review_status": correction["review_status"],
            "resulting_label_origin": correction["resulting_label_origin"],
            "review": copy.deepcopy(correction["review"]),
        }
        if correction.get("review_presentation") is not None:
            overlaid["review_presentation"] = copy.deepcopy(
                correction["review_presentation"]
            )
        effective[annotation_id] = overlaid
        applied.append(
            {
                "correction_id": correction_id,
                "annotation_id": annotation_id,
                "supersedes_action_id": source_action_id,
                "source_action_sha256": source_sha256,
                "review_status": correction["review_status"],
            }
        )

    if errors:
        raise FinalizationError(
            "assistant correction validation failed:\n"
            + "\n".join(errors[:40])
        )
    return effective, {
        "applied": applied,
        "status_counts": dict(
            sorted(Counter(item["review_status"] for item in corrections).items())
        ),
    }


def materialize_review_attention(
    *,
    case_id: str,
    effective_decisions: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Expose bounded ambiguous assistant re-audits without promoting them."""

    records: list[dict[str, Any]] = []
    for annotation_id, decision in effective_decisions.items():
        presentation = decision.get("review_presentation")
        if decision.get("review_status") != "ambiguous" or presentation is None:
            continue
        fields = _clean_fields(decision["adjudicated_fields"])
        records.append(
            {
                "schema": "taskplanner.assistant_review_attention.v1",
                "case_id": case_id,
                "event_id": annotation_id,
                **fields,
                "review_status": "ambiguous",
                "label_origin": None,
                "review": copy.deepcopy(decision["review"]),
                "review_presentation": copy.deepcopy(presentation),
                "scoring_role": "context_only_not_ground_truth",
            }
        )
    records.sort(
        key=lambda item: (float(item["time_sec"]), str(item["event_id"]))
    )
    return records


def materialize_effective_interactions(
    state: dict[str, Any],
    *,
    effective_decisions: dict[str, dict[str, Any]] | None = None,
    point_schema_id: str = POINT_SCHEMA,
    interval_schema_id: str = INTERVAL_SCHEMA,
) -> tuple[list[dict[str, Any]], dict[str, int], list[str]]:
    """Return confirmed interactions and counts for every reviewed interaction."""

    case_id = str(state["case_id"])
    decision_map = effective_decisions or _effective_decision_map(state)
    effective = list(decision_map.items())

    interaction_effective = [
        (annotation_id, decision)
        for annotation_id, decision in effective
        if decision["adjudicated_fields"]["event_type"] in INTERACTION_TYPES
    ]
    status_counts = Counter(
        str(decision["review_status"])
        for _, decision in interaction_effective
    )
    confirmed: list[dict[str, Any]] = []
    confirmed_action_ids: list[str] = []
    for annotation_id, decision in interaction_effective:
        if decision["review_status"] != "confirmed":
            continue
        fields = _clean_fields(decision["adjudicated_fields"])
        is_interval = (
            fields["event_type"] == "implicit_tool_request"
            and "start_source_frame_idx" in fields
            and "end_source_frame_idx" in fields
        )
        record = {
            "schema": interval_schema_id if is_interval else point_schema_id,
            "case_id": case_id,
            "event_id": annotation_id,
            **fields,
            "review_status": "confirmed",
            "label_origin": decision.get(
                "resulting_label_origin",
                "human_video_review",
            ),
            "review": copy.deepcopy(decision["review"]),
        }
        presentation = decision.get("review_presentation")
        if presentation is not None:
            if is_interval:
                raise FinalizationError(
                    f"{annotation_id}: request interval에는 "
                    "review_presentation을 materialize할 수 없습니다."
                )
            record["review_presentation"] = copy.deepcopy(presentation)
        confirmed.append(record)
        confirmed_action_ids.append(
            str(decision.get("action_id") or decision.get("decision_id"))
        )

    confirmed.sort(key=lambda item: (float(item["time_sec"]), item["event_id"]))
    return confirmed, dict(sorted(status_counts.items())), confirmed_action_ids


def materialize_provisional_phases(
    state: dict[str, Any],
    *,
    effective_decisions: dict[str, dict[str, Any]] | None = None,
    point_schema_id: str = POINT_SCHEMA,
) -> tuple[list[dict[str, Any]], dict[str, int], list[str]]:
    """Materialize reviewed, non-rejected phase starts as provisional context.

    Phase candidates remain explicitly ambiguous until a later cross-case phase
    optimization pass.  They are published beside, not promoted into, the
    confirmed interaction target set so evaluators cannot silently count them
    as phase accuracy ground truth.
    """

    case_id = str(state["case_id"])
    decision_map = effective_decisions or _effective_decision_map(state)
    candidates_by_id = {
        str(candidate["_review_ui"]["candidate_id"]): candidate
        for candidate in state["candidates"]
    }
    phase_decisions = [
        (annotation_id, decision)
        for annotation_id, decision in decision_map.items()
        if decision["adjudicated_fields"]["event_type"] == PHASE_TYPE
    ]
    status_counts = Counter(
        str(decision["review_status"])
        for _, decision in phase_decisions
    )
    records: list[dict[str, Any]] = []
    action_ids: list[str] = []
    for annotation_id, decision in phase_decisions:
        if decision["review_status"] == "rejected":
            continue
        fields = _clean_fields(decision["adjudicated_fields"])
        candidate = candidates_by_id.get(annotation_id, {})
        boundary_kind = candidate.get("phase_boundary_kind")
        if boundary_kind not in {
            "clip_initial_state",
            "observed_transition",
            "uncertain_transition",
        }:
            raise FinalizationError(
                f"{annotation_id}: phase_boundary_kind를 복원할 수 없습니다."
            )
        review = copy.deepcopy(decision["review"])
        reviewer_kind = str(review.get("reviewer_kind", ""))
        label_origin = (
            "human_video_review"
            if reviewer_kind == "human"
            else "assistant_video_adjudication"
        )
        records.append(
            {
                "schema": point_schema_id,
                "case_id": case_id,
                "event_id": annotation_id,
                **fields,
                "phase_boundary_kind": boundary_kind,
                "review_status": str(decision["review_status"]),
                "label_origin": label_origin,
                "review": review,
            }
        )
        action_ids.append(
            str(decision.get("action_id") or decision.get("decision_id"))
        )
    records.sort(key=lambda item: (float(item["time_sec"]), item["event_id"]))
    return records, dict(sorted(status_counts.items())), action_ids


def _validate_policy(policy: dict[str, Any], *, case_id: str) -> float:
    policy_schema = str(policy.get("schema", ""))
    if policy_schema not in POLICY_SCHEMAS:
        raise FinalizationError(
            "지원하지 않는 projection policy schema입니다."
        )
    if policy.get("case_id") != case_id:
        raise FinalizationError("projection policy case_id가 일치하지 않습니다.")
    if policy.get("raw_observations_preserved") is not True:
        raise FinalizationError("raw_observations_preserved=true가 필요합니다.")
    rules = policy.get("rules")
    if not isinstance(rules, dict):
        raise FinalizationError("projection policy rules 객체가 필요합니다.")
    required_rules = {
        "exclude_mayo_scrub_mayo_roundtrip",
        "collapse_surgeon_scrub_mayo_return",
        "exclude_unclosed_direct_return",
        "keep_mayo_scrub_surgeon_handover",
    }
    if policy_schema.endswith(".v2"):
        required_rules.update(
            {
                "normalize_unresolved_operative_recipient",
                "exclude_unresolved_retractor_bundle",
            }
        )
    for name in required_rules:
        rule = rules.get(name)
        if not isinstance(rule, dict) or rule.get("enabled") is not True:
            raise FinalizationError(f"projection policy rule {name}이 활성화되어야 합니다.")
    max_gap = policy.get("max_continuous_chain_gap_sec")
    if (
        isinstance(max_gap, bool)
        or not isinstance(max_gap, (int, float))
        or not math.isfinite(float(max_gap))
        or not 0.0 < float(max_gap) <= 10.0
    ):
        raise FinalizationError(
            "max_continuous_chain_gap_sec는 0보다 크고 10 이하인 수여야 합니다."
        )
    return float(max_gap)


def project_dt_records(
    records: list[dict[str, Any]],
    *,
    max_chain_gap_sec: float,
    policy: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Project confirmed observations into DT-relevant interaction events."""

    rules = policy.get("rules", {}) if isinstance(policy, dict) else {}
    normalize_rule = rules.get("normalize_unresolved_operative_recipient", {})
    exclude_bundle_rule = rules.get("exclude_unresolved_retractor_bundle", {})
    normalize_ids = {
        str(value)
        for value in normalize_rule.get("source_event_ids", [])
    } if normalize_rule.get("enabled") is True else set()
    exclude_bundle_ids = {
        str(value)
        for value in exclude_bundle_rule.get("source_event_ids", [])
    } if exclude_bundle_rule.get("enabled") is True else set()
    observed_unresolved_endpoint = str(
        normalize_rule.get(
            "observed_endpoint",
            "operative_person_role_unresolved",
        )
    )
    projected_normalized_endpoint = str(
        normalize_rule.get("projected_endpoint", "surgeon")
    )
    unresolved_bundle_tool = str(
        exclude_bundle_rule.get("tool", "retractor_bundle_unresolved")
    )

    requests = [
        copy.deepcopy(record)
        for record in records
        if record["event_type"] == "implicit_tool_request"
    ]
    transfers = [
        copy.deepcopy(record)
        for record in records
        if record["event_type"] == "tool_transfer"
    ]
    by_tool: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in transfers:
        by_tool[str(record["tool"])].append(record)
    for group in by_tool.values():
        group.sort(key=lambda item: (float(item["time_sec"]), item["event_id"]))

    excluded_ids: set[str] = set()
    collapsed_first_ids: set[str] = set()
    unclosed_direct_return_ids: set[str] = set()
    unresolved_bundle_ids: set[str] = set()
    collapsed_by_output_id: dict[str, dict[str, Any]] = {}
    normalized_by_output_id: dict[str, dict[str, Any]] = {}
    excluded_roundtrips: list[dict[str, Any]] = []
    excluded_unclosed_direct_returns: list[dict[str, Any]] = []
    excluded_unresolved_transfers: list[dict[str, Any]] = []
    collapsed_returns: list[dict[str, Any]] = []
    normalized_recipients: list[dict[str, Any]] = []

    transfer_ids = {str(record["event_id"]) for record in transfers}
    missing_normalize = sorted(normalize_ids - transfer_ids)
    missing_bundle = sorted(exclude_bundle_ids - transfer_ids)
    if missing_normalize or missing_bundle:
        raise FinalizationError(
            "projection policy가 존재하지 않는 transfer를 참조합니다: "
            + ", ".join(missing_normalize + missing_bundle)
        )

    for record in transfers:
        event_id = str(record["event_id"])
        if event_id in exclude_bundle_ids:
            if record.get("tool") != unresolved_bundle_tool:
                raise FinalizationError(
                    f"{event_id}: unresolved bundle projection rule의 "
                    "tool과 observed record가 다릅니다."
                )
            unresolved_bundle_ids.add(event_id)
            excluded_unresolved_transfers.append(
                {
                    "tool": record["tool"],
                    "source_event_id": event_id,
                    "time_sec": record["time_sec"],
                    "observed_edge": [record["from"], record["to"]],
                    "reason": str(
                        exclude_bundle_rule.get(
                            "reason",
                            "unresolved bundle is observation-only",
                        )
                    ),
                }
            )
        if event_id in normalize_ids:
            if record.get("to") != observed_unresolved_endpoint:
                raise FinalizationError(
                    f"{event_id}: recipient normalization rule의 observed "
                    "endpoint와 record가 다릅니다."
                )
            projected = copy.deepcopy(record)
            projected["to"] = projected_normalized_endpoint
            normalized_by_output_id[event_id] = projected
            normalized_recipients.append(
                {
                    "source_event_id": event_id,
                    "output_event_id": event_id,
                    "tool": record["tool"],
                    "time_sec": record["time_sec"],
                    "observed_edge": [record["from"], record["to"]],
                    "projected_edge": [
                        record["from"],
                        projected_normalized_endpoint,
                    ],
                    "reason": str(
                        normalize_rule.get(
                            "reason",
                            "recipient normalized only for the DT contract",
                        )
                    ),
                }
            )

    for tool, group in sorted(by_tool.items()):
        index = 0
        while index + 1 < len(group):
            first = group[index]
            second = group[index + 1]
            delta = float(second["time_sec"]) - float(first["time_sec"])
            continuous = 0.0 <= delta <= max_chain_gap_sec
            first_edge = (first["from"], first["to"])
            second_edge = (second["from"], second["to"])
            if (
                continuous
                and first_edge == ("mayo_stand", "scrub_nurse")
                and second_edge == ("scrub_nurse", "mayo_stand")
            ):
                source_ids = [first["event_id"], second["event_id"]]
                excluded_ids.update(source_ids)
                excluded_roundtrips.append(
                    {
                        "tool": tool,
                        "source_event_ids": source_ids,
                        "start_sec": first["time_sec"],
                        "end_sec": second["time_sec"],
                        "duration_sec": delta,
                        "reason": (
                            "scrub-only Mayo pickup/replacement without a "
                            "surgeon handover"
                        ),
                    }
                )
                index += 2
                continue
            if (
                continuous
                and first_edge == ("surgeon", "scrub_nurse")
                and second_edge == ("scrub_nurse", "mayo_stand")
            ):
                projected = copy.deepcopy(second)
                projected["from"] = "surgeon"
                collapsed_first_ids.add(str(first["event_id"]))
                collapsed_by_output_id[str(second["event_id"])] = projected
                collapsed_returns.append(
                    {
                        "tool": tool,
                        "source_event_ids": [
                            first["event_id"],
                            second["event_id"],
                        ],
                        "output_event_id": second["event_id"],
                        "output_time_sec": second["time_sec"],
                        "output_edge": ["surgeon", "mayo_stand"],
                        "duration_sec": delta,
                        "reason": (
                            "continuous surgeon return completed on the Mayo "
                            "stand through the scrub hand"
                        ),
                    }
                )
                index += 2
                continue
            index += 1

    for record in transfers:
        event_id = str(record["event_id"])
        if (
            event_id not in collapsed_first_ids
            and event_id not in excluded_ids
            and event_id not in unresolved_bundle_ids
            and (record["from"], record["to"])
            == ("surgeon", "scrub_nurse")
        ):
            unclosed_direct_return_ids.add(event_id)
            excluded_unclosed_direct_returns.append(
                {
                    "tool": record["tool"],
                    "source_event_id": event_id,
                    "time_sec": record["time_sec"],
                    "reason": (
                        "observable direct return without an observed Mayo "
                        "placement is not scoreable by the current DT/BT "
                        "action contract"
                    ),
                }
            )

    projected_transfers: list[dict[str, Any]] = []
    source_mapping: list[dict[str, Any]] = [
        {
            "output_event_id": record["event_id"],
            "operation": "identity",
            "source_event_ids": [record["event_id"]],
        }
        for record in requests
    ]
    for record in transfers:
        event_id = str(record["event_id"])
        if (
            event_id in excluded_ids
            or event_id in collapsed_first_ids
            or event_id in unclosed_direct_return_ids
            or event_id in unresolved_bundle_ids
        ):
            continue
        projected = collapsed_by_output_id.get(
            event_id,
            normalized_by_output_id.get(event_id, record),
        )
        projected_transfers.append(copy.deepcopy(projected))
        collapsed = next(
            (
                item
                for item in collapsed_returns
                if item["output_event_id"] == event_id
            ),
            None,
        )
        normalized = next(
            (
                item
                for item in normalized_recipients
                if item["output_event_id"] == event_id
            ),
            None,
        )
        source_mapping.append(
            {
                "output_event_id": event_id,
                "operation": (
                    "collapse_surgeon_scrub_mayo"
                    if collapsed is not None
                    else (
                        "normalize_unresolved_operative_recipient"
                        if normalized is not None
                        else "identity"
                    )
                ),
                "source_event_ids": (
                    collapsed["source_event_ids"]
                    if collapsed is not None
                    else [event_id]
                ),
            }
        )

    output = requests + projected_transfers
    output.sort(key=lambda item: (float(item["time_sec"]), item["event_id"]))
    source_mapping.sort(key=lambda item: item["output_event_id"])
    return output, {
        "excluded_roundtrips": sorted(
            excluded_roundtrips,
            key=lambda item: (float(item["start_sec"]), item["source_event_ids"]),
        ),
        "collapsed_returns": sorted(
            collapsed_returns,
            key=lambda item: (
                float(item["output_time_sec"]),
                item["output_event_id"],
            ),
        ),
        "excluded_unclosed_direct_returns": sorted(
            excluded_unclosed_direct_returns,
            key=lambda item: (float(item["time_sec"]), item["source_event_id"]),
        ),
        "excluded_unresolved_transfers": sorted(
            excluded_unresolved_transfers,
            key=lambda item: (float(item["time_sec"]), item["source_event_id"]),
        ),
        "normalized_recipients": sorted(
            normalized_recipients,
            key=lambda item: (float(item["time_sec"]), item["output_event_id"]),
        ),
        "source_mapping": source_mapping,
    }


def derive_compound_action_episodes(
    records: list[dict[str, Any]],
    *,
    max_chain_gap_sec: float,
) -> list[dict[str, Any]]:
    """Group Mayo pickup plus surgeon arrival into one BT handover episode."""

    transfers = [
        record
        for record in records
        if record["event_type"] == "tool_transfer"
    ]
    episodes: list[dict[str, Any]] = []
    for first, second in zip(transfers, transfers[1:]):
        if first["tool"] != second["tool"]:
            continue
        if (first["from"], first["to"]) != ("mayo_stand", "scrub_nurse"):
            continue
        if (second["from"], second["to"]) != ("scrub_nurse", "surgeon"):
            continue
        duration = float(second["time_sec"]) - float(first["time_sec"])
        if not 0.0 <= duration <= max_chain_gap_sec:
            continue
        episodes.append(
            {
                "episode_id": f"{second['event_id']}-handover",
                "tool": second["tool"],
                "start_event_id": first["event_id"],
                "target_event_id": second["event_id"],
                "source_event_ids": [first["event_id"], second["event_id"]],
                "start_sec": first["time_sec"],
                "target_sec": second["time_sec"],
                "duration_sec": duration,
                "scoring_rule": (
                    "score the surgeon-arrival target once; the Mayo pickup is "
                    "a physical substep of the same BT action"
                ),
            }
        )
    return episodes


def projection_provenance(
    operations: dict[str, Any],
) -> list[dict[str, Any]]:
    """Describe projected outputs that are not direct edge observations."""

    provenance: list[dict[str, Any]] = []
    for item in operations["collapsed_returns"]:
        provenance.append(
            {
                "output_event_id": item["output_event_id"],
                "direct_observation": False,
                "projection": "collapse_surgeon_scrub_mayo_return",
                "source_event_ids": list(item["source_event_ids"]),
                "observed_output_edge": ["scrub_nurse", "mayo_stand"],
                "projected_output_edge": ["surgeon", "mayo_stand"],
                "reason": item["reason"],
            }
        )
    for item in operations.get("normalized_recipients", []):
        provenance.append(
            {
                "output_event_id": item["output_event_id"],
                "direct_observation": False,
                "projection": "normalize_unresolved_operative_recipient",
                "source_event_ids": [item["source_event_id"]],
                "observed_output_edge": list(item["observed_edge"]),
                "projected_output_edge": list(item["projected_edge"]),
                "reason": item["reason"],
            }
        )
    provenance.sort(key=lambda item: str(item["output_event_id"]))
    return provenance


def _load_tool_ids(path: Path) -> set[str]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise FinalizationError(f"{path}: tool catalog 오류: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("tools"), list):
        raise FinalizationError(f"{path}: tools 목록이 없습니다.")
    return {
        str(item["id"])
        for item in payload["tools"]
        if isinstance(item, dict) and item.get("id")
    }


def validate_records(
    records: list[dict[str, Any]],
    *,
    case_id: str,
    timeline: dict[str, Any],
    point_schema: dict[str, Any],
    interval_schema: dict[str, Any],
    tool_ids: set[str],
) -> None:
    timestamps, timeline_errors = validate_timeline(timeline, case_id=case_id)
    errors = list(timeline_errors)
    point_schema_id = str(point_schema.get("$id", ""))
    interval_schema_id = str(interval_schema.get("$id", ""))
    if point_schema_id not in (POINT_SCHEMA, POINT_SCHEMA_V2):
        raise FinalizationError("지원하지 않는 point schema입니다.")
    if interval_schema_id != INTERVAL_SCHEMA:
        raise FinalizationError("지원하지 않는 interval schema입니다.")
    validators = {
        point_schema_id: jsonschema.Draft202012Validator(
            point_schema,
            format_checker=jsonschema.FormatChecker(),
        ),
        interval_schema_id: jsonschema.Draft202012Validator(
            interval_schema,
            format_checker=jsonschema.FormatChecker(),
        ),
    }
    seen_ids: set[str] = set()
    previous_key: tuple[float, str] | None = None
    gaps = timeline.get("gaps", [])
    for line_number, record in enumerate(records, 1):
        schema_name = record.get("schema")
        validator = validators.get(str(schema_name))
        if validator is None:
            errors.append(f"line {line_number}: 지원하지 않는 record schema")
            continue
        for violation in validator.iter_errors(record):
            path = ".".join(str(part) for part in violation.absolute_path)
            errors.append(
                f"line {line_number}: schema {path or '<record>'}: "
                f"{violation.message}"
            )
        event_id = str(record.get("event_id", ""))
        if event_id in seen_ids:
            errors.append(f"line {line_number}: duplicate event_id {event_id}")
        seen_ids.add(event_id)
        if record.get("case_id") != case_id:
            errors.append(f"line {line_number}: case_id 불일치")
        frame_idx = record.get("source_frame_idx")
        if (
            isinstance(frame_idx, bool)
            or not isinstance(frame_idx, int)
            or not 0 <= frame_idx < len(timestamps)
        ):
            errors.append(f"line {line_number}: source_frame_idx 범위 오류")
            continue
        expected_time = timestamps[frame_idx]
        if abs(float(record["time_sec"]) - expected_time) > 5e-10:
            errors.append(f"line {line_number}: time_sec가 timeline과 다릅니다.")
        key = (float(record["time_sec"]), event_id)
        if previous_key is not None and key < previous_key:
            errors.append(f"line {line_number}: records are not time sorted")
        previous_key = key
        if record["event_type"] == "tool_transfer":
            if record.get("tool") not in tool_ids:
                errors.append(f"line {line_number}: canonical tool ID 오류")
            if record.get("from") == record.get("to"):
                errors.append(f"line {line_number}: from과 to가 같습니다.")
        if schema_name == interval_schema_id:
            start_idx = record.get("start_source_frame_idx")
            end_idx = record.get("end_source_frame_idx")
            if start_idx != frame_idx:
                errors.append(
                    f"line {line_number}: source_frame_idx/start frame 불일치"
                )
            if (
                isinstance(end_idx, bool)
                or not isinstance(end_idx, int)
                or not frame_idx <= end_idx < len(timestamps)
            ):
                errors.append(f"line {line_number}: interval end frame 오류")
                continue
            if abs(float(record["start_sec"]) - expected_time) > 5e-10:
                errors.append(f"line {line_number}: start_sec 불일치")
            if abs(float(record["end_sec"]) - timestamps[end_idx]) > 5e-10:
                errors.append(f"line {line_number}: end_sec 불일치")
            for gap in gaps:
                before = float(gap["before_time_sec"])
                after = float(gap["after_time_sec"])
                if float(record["start_sec"]) < after and float(record["end_sec"]) > before:
                    errors.append(f"line {line_number}: interval이 gap을 가로지릅니다.")
    if errors:
        raise FinalizationError(
            "interaction validation failed:\n" + "\n".join(errors[:40])
        )


def canonical_singleton_warnings(
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Flag transitions that require more than one physical instance per type."""

    state_by_tool: dict[str, str] = {}
    warnings: list[dict[str, Any]] = []
    for record in records:
        if record["event_type"] != "tool_transfer":
            continue
        tool = str(record["tool"])
        expected_from = state_by_tool.get(tool)
        if expected_from is not None and expected_from != record["from"]:
            warnings.append(
                {
                    "event_id": record["event_id"],
                    "tool": tool,
                    "single_instance_expected_from": expected_from,
                    "observed_from": record["from"],
                    "interpretation": (
                        "multiple physical instances or an unobserved "
                        "transition are required"
                    ),
                }
            )
        state_by_tool[tool] = str(record["to"])
    return warnings


def _stage(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    succeeded = False
    try:
        os.fchmod(descriptor, 0o640)
        offset = 0
        while offset < len(data):
            written = os.write(descriptor, data[offset:])
            if written <= 0:
                raise FinalizationError(f"{path}: staging write 실패")
            offset += written
        os.fsync(descriptor)
        succeeded = True
    finally:
        os.close(descriptor)
        if not succeeded:
            temporary.unlink(missing_ok=True)
    return temporary


def publish_create_only(outputs: dict[Path, bytes]) -> None:
    if len(outputs) != len(set(outputs)):
        raise FinalizationError("output 경로가 중복되었습니다.")
    existing = [path for path in outputs if path.exists()]
    if existing:
        raise FinalizationError(
            "refusing to overwrite output: " + ", ".join(map(str, existing))
        )
    staged: dict[Path, Path] = {}
    published: list[Path] = []
    succeeded = False
    try:
        for path, data in outputs.items():
            staged[path] = _stage(path, data)
        for path, temporary in staged.items():
            os.link(temporary, path)
            published.append(path)
        for directory in {path.parent for path in outputs}:
            descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        succeeded = True
    except FileExistsError as exc:
        raise FinalizationError(
            f"create-only target appeared during publish: {exc.filename}"
        ) from exc
    except OSError as exc:
        raise FinalizationError(f"atomic publish 실패: {exc}") from exc
    finally:
        if not succeeded:
            rollback_directories: set[Path] = set()
            for path in reversed(published):
                temporary = staged.get(path)
                try:
                    if (
                        temporary is not None
                        and path.exists()
                        and os.path.samefile(path, temporary)
                    ):
                        path.unlink()
                        rollback_directories.add(path.parent)
                except OSError:
                    pass
            for directory in rollback_directories:
                try:
                    descriptor = os.open(directory, os.O_RDONLY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                except OSError:
                    pass
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)


def finalize(
    *,
    case_dir: Path,
    interaction_candidates_path: Path,
    phase_candidates_path: Path,
    timeline_path: Path,
    decisions_path: Path,
    timeline_actions_path: Path,
    policy_path: Path,
    point_schema_path: Path,
    interval_schema_path: Path,
    assistant_corrections_path: Path,
    assistant_correction_schema_path: Path,
    tools_path: Path,
    observed_output_path: Path,
    dt_output_path: Path,
    phase_output_path: Path,
    report_output_path: Path,
    assistant_reaudit_path: Path | None = None,
    assistant_reaudit_schema_path: Path | None = None,
) -> dict[str, Any]:
    policy = load_json(policy_path)
    timeline = load_json(timeline_path)
    case_id = str(timeline.get("case_id", ""))
    max_chain_gap_sec = _validate_policy(policy, case_id=case_id)
    point_schema = load_json(point_schema_path)
    point_schema_id = str(point_schema.get("$id", ""))
    interval_schema = load_json(interval_schema_path)
    interval_schema_id = str(interval_schema.get("$id", ""))
    assistant_correction_schema = load_json(
        assistant_correction_schema_path
    )
    corrections = load_jsonl(assistant_corrections_path)
    tool_ids = _load_tool_ids(tools_path)
    store = ReviewStore(
        case_dir=case_dir,
        candidates_path=interaction_candidates_path,
        additional_candidates_paths=(phase_candidates_path,),
        timeline_path=timeline_path,
        decisions_path=decisions_path,
        timeline_actions_path=timeline_actions_path,
        stream_kind="timeline",
    )
    state = store.state()
    if state["remaining_count"] != 0:
        raise FinalizationError(
            f"{state['remaining_count']}개 candidate가 아직 미검토 상태입니다."
        )
    effective_decisions, correction_summary = apply_assistant_corrections(
        state=state,
        store=store,
        corrections=corrections,
        correction_schema=assistant_correction_schema,
    )
    reaudit_summary: dict[str, Any] = {
        "applied": [],
        "status_counts": {},
    }
    if (assistant_reaudit_path is None) != (
        assistant_reaudit_schema_path is None
    ):
        raise FinalizationError(
            "assistant reaudit file과 schema는 함께 지정해야 합니다."
        )
    if (
        assistant_reaudit_path is not None
        and assistant_reaudit_schema_path is not None
    ):
        effective_decisions, reaudit_summary = apply_assistant_corrections(
            state=state,
            store=store,
            corrections=load_jsonl(assistant_reaudit_path),
            correction_schema=load_json(assistant_reaudit_schema_path),
            effective_decisions=effective_decisions,
        )
    observed, interaction_status_counts, confirmed_action_ids = (
        materialize_effective_interactions(
            state,
            effective_decisions=effective_decisions,
            point_schema_id=point_schema_id,
            interval_schema_id=interval_schema_id,
        )
    )
    provisional_phases, phase_status_counts, phase_action_ids = (
        materialize_provisional_phases(
            state,
            effective_decisions=effective_decisions,
            point_schema_id=point_schema_id,
        )
    )
    review_attention = materialize_review_attention(
        case_id=case_id,
        effective_decisions=effective_decisions,
    )
    validate_records(
        observed,
        case_id=case_id,
        timeline=timeline,
        point_schema=point_schema,
        interval_schema=interval_schema,
        tool_ids=tool_ids,
    )
    projected, operations = project_dt_records(
        observed,
        max_chain_gap_sec=max_chain_gap_sec,
        policy=policy,
    )
    validate_records(
        projected,
        case_id=case_id,
        timeline=timeline,
        point_schema=point_schema,
        interval_schema=interval_schema,
        tool_ids=tool_ids,
    )
    validate_records(
        provisional_phases,
        case_id=case_id,
        timeline=timeline,
        point_schema=point_schema,
        interval_schema=interval_schema,
        tool_ids=tool_ids,
    )

    observed_data = encode_jsonl(observed)
    dt_data = encode_jsonl(projected)
    phase_data = encode_jsonl(provisional_phases)
    source_hashes = {
        "interaction_candidates": sha256_file(interaction_candidates_path),
        "phase_candidates": sha256_file(phase_candidates_path),
        "legacy_decisions": sha256_file(decisions_path),
        "timeline_actions": sha256_file(timeline_actions_path),
        "timeline": sha256_file(timeline_path),
        "projection_policy": sha256_file(policy_path),
        "point_schema": sha256_file(point_schema_path),
        "interval_schema": sha256_file(interval_schema_path),
        "assistant_corrections": sha256_file(assistant_corrections_path),
        "assistant_correction_schema": sha256_file(
            assistant_correction_schema_path
        ),
        "tool_catalog": sha256_file(tools_path),
    }
    if (
        assistant_reaudit_path is not None
        and assistant_reaudit_schema_path is not None
    ):
        source_hashes["assistant_reaudit"] = sha256_file(
            assistant_reaudit_path
        )
        source_hashes["assistant_reaudit_schema"] = sha256_file(
            assistant_reaudit_schema_path
        )
    observed_label_origin = dict(
        sorted(Counter(item["label_origin"] for item in observed).items())
    )
    observed_reviewer_kind = dict(
        sorted(Counter(item["review"]["reviewer_kind"] for item in observed).items())
    )
    adjudication_revision = hashlib.sha256(
        (
            str(state["revision"])
            + ":"
            + source_hashes["assistant_corrections"]
            + ":"
            + source_hashes.get("assistant_reaudit", "")
        ).encode("utf-8")
    ).hexdigest()[:16]
    report = {
        "schema": REPORT_SCHEMA,
        "case_id": case_id,
        "source_revision": state["revision"],
        "adjudication_revision": adjudication_revision,
        "authority": {
            "observed_reference": (
                "mixed_direct_human_and_authorized_assistant_video_review"
            ),
            "dt_reference": (
                "deterministic_policy_projection_of_mixed_review"
            ),
            "phase_reference": (
                "human_reviewed_provisional_ambiguous_context"
            ),
            "ground_truth_consumers": ["evaluation_only"],
        },
        "inputs": source_hashes,
        "policy": copy.deepcopy(policy),
        "counts": {
            "candidate_count": len(state["candidates"]),
            "candidate_review_status": state["review_status_counts"],
            "interaction_review_status": interaction_status_counts,
            "human_created_annotation_count": len(state["human_annotations"]),
            "assistant_correction_count": len(corrections),
            "assistant_correction_status": correction_summary[
                "status_counts"
            ],
            "assistant_reaudit_count": len(
                reaudit_summary["applied"]
            ),
            "assistant_reaudit_status": reaudit_summary["status_counts"],
            "review_attention_count": len(review_attention),
            "observed_confirmed_count": len(observed),
            "observed_event_type": dict(
                sorted(Counter(item["event_type"] for item in observed).items())
            ),
            "observed_label_origin": observed_label_origin,
            "observed_reviewer_kind": observed_reviewer_kind,
            "dt_confirmed_count": len(projected),
            "dt_event_type": dict(
                sorted(Counter(item["event_type"] for item in projected).items())
            ),
            "phase_review_status": phase_status_counts,
            "phase_provisional_count": len(provisional_phases),
            "excluded_cleanup_source_event_count": sum(
                len(item["source_event_ids"])
                for item in operations["excluded_roundtrips"]
            ),
            "excluded_unclosed_direct_return_count": len(
                operations["excluded_unclosed_direct_returns"]
            ),
            "excluded_unresolved_transfer_count": len(
                operations["excluded_unresolved_transfers"]
            ),
            "collapsed_source_event_count": sum(
                len(item["source_event_ids"])
                for item in operations["collapsed_returns"]
            ),
            "collapsed_output_event_count": len(operations["collapsed_returns"]),
        },
        "operations": operations,
        "compound_action_episodes": derive_compound_action_episodes(
            projected,
            max_chain_gap_sec=max_chain_gap_sec,
        ),
        "projection_provenance": projection_provenance(operations),
        "confirmed_source_action_ids": sorted(confirmed_action_ids),
        "provisional_phase_action_ids": sorted(phase_action_ids),
        "assistant_corrections": correction_summary["applied"],
        "assistant_reaudit": reaudit_summary["applied"],
        "review_attention": review_attention,
        "canonical_singleton_warnings": canonical_singleton_warnings(projected),
        "outputs": {
            "observed": {
                "path": str(observed_output_path.resolve()),
                "sha256": hashlib.sha256(observed_data).hexdigest(),
                "record_count": len(observed),
            },
            "dt_reference": {
                "path": str(dt_output_path.resolve()),
                "sha256": hashlib.sha256(dt_data).hexdigest(),
                "record_count": len(projected),
            },
            "phase_reference": {
                "path": str(phase_output_path.resolve()),
                "sha256": hashlib.sha256(phase_data).hexdigest(),
                "record_count": len(provisional_phases),
                "status": "provisional_ambiguous_not_scored",
            },
        },
        "information_boundary": {
            "runtime_input_allowed": False,
            "vlm_input_allowed": False,
            "reducer_input_allowed": False,
            "bt_input_allowed": False,
        },
    }
    report_data = encode_json(report)
    publish_create_only(
        {
            observed_output_path: observed_data,
            dt_output_path: dt_data,
            phase_output_path: phase_data,
            report_output_path: report_data,
        }
    )
    return {
        "ok": True,
        "case_id": case_id,
        "source_revision": state["revision"],
        "adjudication_revision": adjudication_revision,
        "observed_count": len(observed),
        "dt_count": len(projected),
        "phase_count": len(provisional_phases),
        "review_attention_count": len(review_attention),
        "observed_sha256": hashlib.sha256(observed_data).hexdigest(),
        "dt_sha256": hashlib.sha256(dt_data).hexdigest(),
        "phase_sha256": hashlib.sha256(phase_data).hexdigest(),
        "report_sha256": hashlib.sha256(report_data).hexdigest(),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Materialize confirmed timeline review and a DT-compatible "
            "evaluation projection."
        )
    )
    parser.add_argument("case_dir", type=Path)
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--point-schema", type=Path)
    parser.add_argument("--interval-schema", type=Path)
    parser.add_argument("--assistant-corrections", type=Path)
    parser.add_argument("--assistant-correction-schema", type=Path)
    parser.add_argument("--assistant-reaudit", type=Path)
    parser.add_argument("--assistant-reaudit-schema", type=Path)
    parser.add_argument("--tools", type=Path)
    parser.add_argument("--observed-output", type=Path)
    parser.add_argument("--dt-output", type=Path)
    parser.add_argument("--phase-output", type=Path)
    parser.add_argument("--report-output", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    case_dir = args.case_dir.resolve()
    annotation_root = case_dir.parents[1]
    try:
        summary = finalize(
            case_dir=case_dir,
            interaction_candidates_path=(
                case_dir / "interaction_candidates.ai_review.v1.jsonl"
            ),
            phase_candidates_path=(
                case_dir / "phase_candidates.ai_review.v1.jsonl"
            ),
            timeline_path=case_dir / "cam4_frame_timeline.v1.json",
            decisions_path=case_dir / "human_review_decisions.v1.jsonl",
            timeline_actions_path=case_dir / "human_timeline_actions.v1.jsonl",
            policy_path=(
                args.policy.resolve()
                if args.policy
                else case_dir / "dt_projection_policy.v1.json"
            ),
            point_schema_path=(
                args.point_schema.resolve()
                if args.point_schema
                else annotation_root
                / "schema/observable_interaction_point.v1.schema.json"
            ),
            interval_schema_path=(
                args.interval_schema.resolve()
                if args.interval_schema
                else annotation_root
                / "schema/observable_interaction_interval.v1.schema.json"
            ),
            assistant_corrections_path=(
                args.assistant_corrections.resolve()
                if args.assistant_corrections
                else case_dir
                / "assistant_annotation_adjudications.final.v4.jsonl"
            ),
            assistant_correction_schema_path=(
                args.assistant_correction_schema.resolve()
                if args.assistant_correction_schema
                else annotation_root
                / "schema/assistant_annotation_adjudication.v1.schema.json"
            ),
            tools_path=(
                args.tools.resolve()
                if args.tools
                else annotation_root / "catalogs/tools.yaml"
            ),
            observed_output_path=(
                args.observed_output.resolve()
                if args.observed_output
                else case_dir / "interaction_events.observed.final.v5.jsonl"
            ),
            dt_output_path=(
                args.dt_output.resolve()
                if args.dt_output
                else case_dir
                / "interaction_events.dt_reference.final.v5.jsonl"
            ),
            phase_output_path=(
                args.phase_output.resolve()
                if args.phase_output
                else case_dir / "phase_events.provisional.final.v1.jsonl"
            ),
            report_output_path=(
                args.report_output.resolve()
                if args.report_output
                else annotation_root
                / f"reports/{case_dir.name}_dt_projection.final.v5.json"
            ),
            assistant_reaudit_path=(
                args.assistant_reaudit.resolve()
                if args.assistant_reaudit
                else None
            ),
            assistant_reaudit_schema_path=(
                args.assistant_reaudit_schema.resolve()
                if args.assistant_reaudit_schema
                else None
            ),
        )
    except FinalizationError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
