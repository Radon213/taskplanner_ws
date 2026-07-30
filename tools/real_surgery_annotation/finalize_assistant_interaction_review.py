#!/usr/bin/env python3
"""Finalize assistant-primary interaction review with an explicit DT projection.

This workflow is intentionally independent from the 0704_6 human-review
finalizer.  Its source of authority is a create-only JSONL of user-authorized
assistant adjudications.  The DT layer is not inferred from time proximity or
tool-type matching: every confirmed observed event must be named exactly once
by an explicit projection operation.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import jsonschema

from .finalize_interaction_review import (
    FinalizationError,
    _load_tool_ids,
    encode_json,
    encode_jsonl,
    load_json,
    load_jsonl,
    publish_create_only,
    sha256_file,
    validate_records,
)


ADJUDICATION_SCHEMA = "taskplanner.assistant_interaction_adjudication.v1"
PROJECTION_SCHEMA = "taskplanner.explicit_dt_interaction_projection.v1"
POINT_SCHEMA = "taskplanner.observable_interaction_point.v1"
INTERVAL_SCHEMA = "taskplanner.observable_interaction_interval.v1"
REPORT_SCHEMA = "taskplanner.dt_interaction_projection_report.v1"
ASSISTANT_LABEL_ORIGIN = "assistant_video_adjudication"


def validate_reviewed_at_not_future(value: Any, *, context: str) -> None:
    if not isinstance(value, str) or not value:
        raise FinalizationError(f"{context}: reviewed_at이 없습니다.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise FinalizationError(
            f"{context}: reviewed_at ISO-8601 형식이 아닙니다."
        ) from exc
    if parsed.tzinfo is None:
        raise FinalizationError(f"{context}: reviewed_at timezone이 필요합니다.")
    if parsed.astimezone(timezone.utc) > (
        datetime.now(timezone.utc) + timedelta(minutes=1)
    ):
        raise FinalizationError(
            f"{context}: reviewed_at이 실제 현재 시각보다 미래입니다."
        )


def _validator(schema: dict[str, Any], expected_id: str) -> jsonschema.Validator:
    if schema.get("$id") != expected_id:
        raise FinalizationError(
            f"schema $id가 {expected_id!r}와 일치하지 않습니다."
        )
    return jsonschema.Draft202012Validator(
        schema,
        format_checker=jsonschema.FormatChecker(),
    )


def _schema_errors(
    validator: jsonschema.Validator,
    value: Any,
    *,
    context: str,
) -> list[str]:
    errors: list[str] = []
    for violation in validator.iter_errors(value):
        path = ".".join(str(part) for part in violation.absolute_path)
        errors.append(
            f"{context}: schema {path or '<record>'}: {violation.message}"
        )
    return errors


def _resolve_reference(path_text: str, *, source_path: Path) -> Path:
    candidate = Path(path_text)
    if candidate.is_absolute():
        return candidate.resolve()
    cwd_candidate = candidate.resolve()
    if cwd_candidate.exists():
        return cwd_candidate
    return (source_path.parent / candidate).resolve()


def _verified_reference_hash(
    *,
    path_text: str,
    expected_sha256: str,
    source_path: Path,
    hash_cache: dict[Path, str],
    context: str,
) -> Path:
    path = _resolve_reference(path_text, source_path=source_path)
    if not path.is_file():
        raise FinalizationError(f"{context}: 참조 파일이 없습니다: {path}")
    actual = hash_cache.get(path)
    if actual is None:
        actual = sha256_file(path)
        hash_cache[path] = actual
    if actual != expected_sha256:
        raise FinalizationError(
            f"{context}: 참조 파일 hash 불일치: {path}; "
            f"expected={expected_sha256}, actual={actual}"
        )
    return path


def _validate_evidence_references(
    *,
    adjudication: dict[str, Any],
    line_number: int,
    adjudications_path: Path,
    timeline_path: Path,
    timeline_sha256: str,
    frame_count: int,
    hash_cache: dict[Path, str],
) -> None:
    fields = adjudication["adjudicated_fields"]
    event_id = str(fields["event_id"])
    event_start = int(
        fields.get("start_source_frame_idx", fields["source_frame_idx"])
    )
    event_end = int(
        fields.get("end_source_frame_idx", fields["source_frame_idx"])
    )
    context = f"adjudication line {line_number} ({event_id})"

    for index, proposal_ref in enumerate(adjudication["proposal_refs"], 1):
        _verified_reference_hash(
            path_text=str(proposal_ref["raw_evidence_file"]),
            expected_sha256=str(proposal_ref["raw_evidence_sha256"]),
            source_path=adjudications_path,
            hash_cache=hash_cache,
            context=f"{context} proposal_refs[{index}]",
        )
        start = proposal_ref.get("candidate_start_sec")
        end = proposal_ref.get("candidate_end_sec")
        if start is not None and end is not None and float(end) < float(start):
            raise FinalizationError(
                f"{context} proposal_refs[{index}]: candidate span 역전"
            )

    for index, evidence_ref in enumerate(adjudication["evidence_refs"], 1):
        evidence_context = f"{context} evidence_refs[{index}]"
        resolved_timeline = _verified_reference_hash(
            path_text=str(evidence_ref["timeline_file"]),
            expected_sha256=str(evidence_ref["timeline_sha256"]),
            source_path=adjudications_path,
            hash_cache=hash_cache,
            context=evidence_context,
        )
        if resolved_timeline != timeline_path.resolve():
            raise FinalizationError(
                f"{evidence_context}: 현재 case timeline이 아닌 파일을 "
                f"참조합니다: {resolved_timeline}"
            )
        if evidence_ref["timeline_sha256"] != timeline_sha256:
            raise FinalizationError(
                f"{evidence_context}: 현재 timeline hash와 다릅니다."
            )
        evidence_start = int(evidence_ref["start_source_frame_idx"])
        evidence_end = int(evidence_ref["end_source_frame_idx"])
        if (
            evidence_start < 0
            or evidence_end < evidence_start
            or evidence_end >= frame_count
        ):
            raise FinalizationError(
                f"{evidence_context}: evidence frame 범위 오류"
            )
        if evidence_start > event_start or evidence_end < event_end:
            raise FinalizationError(
                f"{evidence_context}: adjudicated event frame 범위를 "
                "포함하지 않습니다."
            )
        if evidence_ref["evidence_kind"] == "video_and_voice":
            _verified_reference_hash(
                path_text=str(evidence_ref["voice_file"]),
                expected_sha256=str(evidence_ref["voice_file_sha256"]),
                source_path=adjudications_path,
                hash_cache=hash_cache,
                context=evidence_context,
            )


def materialize_observed(
    *,
    adjudications: list[dict[str, Any]],
    adjudications_path: Path,
    adjudication_schema: dict[str, Any],
    case_id: str,
    timeline: dict[str, Any],
    timeline_path: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    validator = _validator(adjudication_schema, ADJUDICATION_SCHEMA)
    errors: list[str] = []
    seen_adjudication_ids: set[str] = set()
    seen_event_ids: set[str] = set()
    observed: list[dict[str, Any]] = []
    source_mapping: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    reviewer_ids: set[str] = set()
    authorized_by: set[str] = set()
    timeline_sha256 = sha256_file(timeline_path)
    timestamps = timeline.get("timestamps_sec")
    if not isinstance(timestamps, list):
        raise FinalizationError("timeline timestamps_sec 목록이 없습니다.")
    hash_cache: dict[Path, str] = {timeline_path.resolve(): timeline_sha256}

    for line_number, adjudication in enumerate(adjudications, 1):
        record_errors = _schema_errors(
            validator,
            adjudication,
            context=f"adjudication line {line_number}",
        )
        errors.extend(record_errors)
        if adjudication.get("schema") != ADJUDICATION_SCHEMA:
            message = f"adjudication line {line_number}: schema ID 불일치"
            record_errors.append(message)
            errors.append(message)
        if adjudication.get("case_id") != case_id:
            message = f"adjudication line {line_number}: case_id 불일치"
            record_errors.append(message)
            errors.append(message)
        adjudication_id = str(adjudication.get("adjudication_id", ""))
        if not adjudication_id.startswith(f"{case_id}-AJ"):
            message = (
                f"adjudication line {line_number}: "
                "adjudication_id/case 불일치"
            )
            record_errors.append(message)
            errors.append(message)
        if adjudication_id in seen_adjudication_ids:
            message = (
                f"adjudication line {line_number}: duplicate adjudication_id "
                f"{adjudication_id}"
            )
            record_errors.append(message)
            errors.append(message)
        seen_adjudication_ids.add(adjudication_id)

        fields = adjudication.get("adjudicated_fields")
        if not isinstance(fields, dict):
            continue
        event_id = str(fields.get("event_id", ""))
        event_type = str(fields.get("event_type", ""))
        expected_prefix = (
            f"{case_id}-R"
            if event_type == "implicit_tool_request"
            else f"{case_id}-T"
        )
        if not event_id.startswith(expected_prefix):
            message = (
                f"adjudication line {line_number}: event_id/case/type 불일치"
            )
            record_errors.append(message)
            errors.append(message)
        if event_id in seen_event_ids:
            message = (
                f"adjudication line {line_number}: duplicate event_id "
                f"{event_id}"
            )
            record_errors.append(message)
            errors.append(message)
        seen_event_ids.add(event_id)

        review = adjudication.get("review")
        if isinstance(review, dict):
            reviewer_ids.add(str(review.get("reviewer_id", "")))
            authorized_by.add(str(review.get("authorized_by", "")))
            try:
                validate_reviewed_at_not_future(
                    review.get("reviewed_at"),
                    context=f"adjudication line {line_number}",
                )
            except FinalizationError as exc:
                record_errors.append(str(exc))
                errors.append(str(exc))
        status = str(adjudication.get("review_status", ""))
        status_counts[status] += 1

        if not record_errors:
            try:
                _validate_evidence_references(
                    adjudication=adjudication,
                    line_number=line_number,
                    adjudications_path=adjudications_path,
                    timeline_path=timeline_path,
                    timeline_sha256=timeline_sha256,
                    frame_count=len(timestamps),
                    hash_cache=hash_cache,
                )
            except FinalizationError as exc:
                record_errors.append(str(exc))
                errors.append(str(exc))

        if status != "confirmed" or record_errors:
            continue
        schema_name = (
            INTERVAL_SCHEMA
            if event_type == "implicit_tool_request"
            else POINT_SCHEMA
        )
        record = {
            "schema": schema_name,
            "case_id": case_id,
            **copy.deepcopy(fields),
            "review_status": "confirmed",
            "label_origin": ASSISTANT_LABEL_ORIGIN,
            "review": copy.deepcopy(adjudication["review"]),
        }
        observed.append(record)
        source_mapping.append(
            {
                "event_id": event_id,
                "adjudication_id": adjudication_id,
                "adjudication_line": line_number,
                "review_status": status,
                "proposal_refs": copy.deepcopy(
                    adjudication["proposal_refs"]
                ),
                "evidence_refs": copy.deepcopy(
                    adjudication["evidence_refs"]
                ),
            }
        )

    if errors:
        raise FinalizationError(
            "assistant adjudication validation failed:\n"
            + "\n".join(errors[:60])
        )
    observed.sort(key=lambda item: (float(item["time_sec"]), item["event_id"]))
    source_mapping.sort(key=lambda item: item["event_id"])
    return observed, {
        "status_counts": dict(sorted(status_counts.items())),
        "reviewer_ids": sorted(value for value in reviewer_ids if value),
        "authorized_by": sorted(value for value in authorized_by if value),
        "source_mapping": source_mapping,
    }


def _require_transfer(
    record: dict[str, Any],
    *,
    event_id: str,
    expected_from: str,
    expected_to: str,
    context: str,
) -> None:
    if record.get("event_type") != "tool_transfer":
        raise FinalizationError(
            f"{context}: {event_id}는 tool_transfer가 아닙니다."
        )
    if (
        record.get("from") != expected_from
        or record.get("to") != expected_to
    ):
        raise FinalizationError(
            f"{context}: {event_id} edge는 "
            f"{expected_from}->{expected_to}여야 합니다."
        )


def _validate_two_event_chain(
    *,
    records: list[dict[str, Any]],
    source_event_ids: list[str],
    first_edge: tuple[str, str],
    second_edge: tuple[str, str],
    context: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    first, second = records
    _require_transfer(
        first,
        event_id=source_event_ids[0],
        expected_from=first_edge[0],
        expected_to=first_edge[1],
        context=context,
    )
    _require_transfer(
        second,
        event_id=source_event_ids[1],
        expected_from=second_edge[0],
        expected_to=second_edge[1],
        context=context,
    )
    if first.get("tool") != second.get("tool"):
        raise FinalizationError(
            f"{context}: 명시된 physical chain의 tool이 다릅니다."
        )
    if float(first["time_sec"]) > float(second["time_sec"]):
        raise FinalizationError(
            f"{context}: source_event_ids가 물리적 시간 순서가 아닙니다."
        )
    return first, second


def apply_explicit_projection(
    *,
    observed: list[dict[str, Any]],
    projection: dict[str, Any],
    projection_schema: dict[str, Any],
    case_id: str,
    projection_path: Path,
    adjudications_path: Path,
    adjudications_sha256: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    validator = _validator(projection_schema, PROJECTION_SCHEMA)
    errors = _schema_errors(
        validator,
        projection,
        context="projection",
    )
    if projection.get("schema") != PROJECTION_SCHEMA:
        errors.append("projection schema ID 불일치")
    if projection.get("case_id") != case_id:
        errors.append("projection case_id 불일치")
    source_reference = projection.get("source_adjudications")
    if isinstance(source_reference, dict):
        reference_path = _resolve_reference(
            str(source_reference.get("path", "")),
            source_path=projection_path,
        )
        if reference_path != adjudications_path.resolve():
            errors.append(
                "projection source_adjudications.path가 현재 adjudication "
                "입력과 다릅니다."
            )
        if source_reference.get("sha256") != adjudications_sha256:
            errors.append(
                "projection source_adjudications.sha256이 현재 입력과 "
                "다릅니다."
            )
    if errors:
        raise FinalizationError(
            "explicit projection validation failed:\n"
            + "\n".join(errors[:60])
        )

    observed_by_id = {str(item["event_id"]): item for item in observed}
    mapped_source_ids: set[str] = set()
    seen_operation_ids: set[str] = set()
    seen_chain_ids: set[str] = set()
    projected: list[dict[str, Any]] = []
    source_mapping: list[dict[str, Any]] = []
    compound_episodes: list[dict[str, Any]] = []
    derived_outputs: list[dict[str, Any]] = []
    excluded_sources: list[dict[str, Any]] = []

    for operation_index, operation in enumerate(projection["operations"], 1):
        operation_id = str(operation["operation_id"])
        context = f"projection operation {operation_index} ({operation_id})"
        if operation_id in seen_operation_ids:
            raise FinalizationError(
                f"{context}: duplicate operation_id"
            )
        seen_operation_ids.add(operation_id)
        operation_kind = str(operation["operation"])
        source_ids = [str(value) for value in operation["source_event_ids"]]
        missing = [value for value in source_ids if value not in observed_by_id]
        if missing:
            raise FinalizationError(
                f"{context}: confirmed observed event가 아닙니다: {missing}"
            )
        duplicate = [value for value in source_ids if value in mapped_source_ids]
        if duplicate:
            raise FinalizationError(
                f"{context}: source event가 두 번 매핑되었습니다: {duplicate}"
            )
        source_records = [observed_by_id[value] for value in source_ids]

        chain_id = operation.get("physical_chain_id")
        if chain_id is not None:
            chain_id = str(chain_id)
            if chain_id in seen_chain_ids:
                raise FinalizationError(
                    f"{context}: duplicate physical_chain_id {chain_id}"
                )
            seen_chain_ids.add(chain_id)

        output_ids: list[str] = []
        direct_observation = True
        if operation_kind == "keep":
            projected.append(copy.deepcopy(source_records[0]))
            output_ids = [source_ids[0]]
        elif operation_kind == "exclude_cleanup_chain":
            _validate_two_event_chain(
                records=source_records,
                source_event_ids=source_ids,
                first_edge=("mayo_stand", "scrub_nurse"),
                second_edge=("scrub_nurse", "mayo_stand"),
                context=context,
            )
            excluded_sources.append(
                {
                    "operation_id": operation_id,
                    "operation": operation_kind,
                    "physical_chain_id": chain_id,
                    "source_event_ids": source_ids,
                    "reason": operation["reason"],
                }
            )
        elif operation_kind == "exclude_non_target_event":
            excluded_sources.append(
                {
                    "operation_id": operation_id,
                    "operation": operation_kind,
                    "source_event_ids": source_ids,
                    "reason": operation["reason"],
                }
            )
        elif operation_kind == "collapse_return_chain":
            first, second = _validate_two_event_chain(
                records=source_records,
                source_event_ids=source_ids,
                first_edge=("surgeon", "scrub_nurse"),
                second_edge=("scrub_nurse", "mayo_stand"),
                context=context,
            )
            timestamp_source_id = str(
                operation["timestamp_source_event_id"]
            )
            output_event_id = str(operation["output_event_id"])
            if timestamp_source_id not in source_ids:
                raise FinalizationError(
                    f"{context}: timestamp_source_event_id가 chain에 없습니다."
                )
            if output_event_id != timestamp_source_id:
                raise FinalizationError(
                    f"{context}: output_event_id는 명시된 timestamp source "
                    "event ID를 재사용해야 합니다."
                )
            output = copy.deepcopy(observed_by_id[timestamp_source_id])
            output["event_id"] = output_event_id
            output["from"] = "surgeon"
            output["to"] = "mayo_stand"
            projected.append(output)
            output_ids = [output_event_id]
            direct_observation = False
            derived_outputs.append(
                {
                    "operation_id": operation_id,
                    "physical_chain_id": chain_id,
                    "output_event_id": output_event_id,
                    "timestamp_source_event_id": timestamp_source_id,
                    "source_event_ids": source_ids,
                    "tool": first["tool"],
                    "projected_edge": ["surgeon", "mayo_stand"],
                    "reason": operation["reason"],
                }
            )
        elif operation_kind == "compound_handover_chain":
            first, second = _validate_two_event_chain(
                records=source_records,
                source_event_ids=source_ids,
                first_edge=("mayo_stand", "scrub_nurse"),
                second_edge=("scrub_nurse", "surgeon"),
                context=context,
            )
            target_event_id = str(operation["target_event_id"])
            if target_event_id != source_ids[1]:
                raise FinalizationError(
                    f"{context}: target_event_id는 surgeon 도착 event여야 "
                    "합니다."
                )
            projected.extend(copy.deepcopy(source_records))
            output_ids = list(source_ids)
            compound_episodes.append(
                {
                    "episode_id": chain_id,
                    "operation_id": operation_id,
                    "source_event_ids": source_ids,
                    "start_event_id": source_ids[0],
                    "target_event_id": target_event_id,
                    "start_sec": first["time_sec"],
                    "target_sec": second["time_sec"],
                    "duration_sec": round(
                        float(second["time_sec"])
                        - float(first["time_sec"]),
                        9,
                    ),
                    "tool": first["tool"],
                    "scoring_rule": (
                        "score the explicitly named surgeon-arrival target "
                        "once; keep both physical substeps in the DT reference"
                    ),
                }
            )
        else:
            raise FinalizationError(
                f"{context}: 지원하지 않는 operation {operation_kind}"
            )

        mapped_source_ids.update(source_ids)
        source_mapping.append(
            {
                "operation_id": operation_id,
                "operation": operation_kind,
                "physical_chain_id": chain_id,
                "source_event_ids": source_ids,
                "output_event_ids": output_ids,
                "direct_observation": direct_observation,
                "reason": operation["reason"],
            }
        )

    unmapped = sorted(set(observed_by_id) - mapped_source_ids)
    if unmapped:
        raise FinalizationError(
            "explicit projection이 모든 confirmed observed event를 "
            f"포함하지 않습니다: {unmapped}"
        )
    projected.sort(key=lambda item: (float(item["time_sec"]), item["event_id"]))
    return projected, {
        "source_mapping": source_mapping,
        "compound_action_episodes": compound_episodes,
        "derived_outputs": derived_outputs,
        "excluded_sources": excluded_sources,
        "chain_matching": "explicit_event_ids_only",
        "time_gap_heuristic_used": False,
        "tool_type_pairing_heuristic_used": False,
    }


def final_review_compatible_operations(
    projection_summary: dict[str, Any],
) -> dict[str, Any]:
    """Expose the explicit projection through the read-only GUI contract.

    This is a representation adapter only. It never finds or pairs chains:
    all source IDs and dispositions have already been validated by
    :func:`apply_explicit_projection`.
    """

    source_mapping: list[dict[str, Any]] = []
    excluded_roundtrips: list[dict[str, Any]] = []
    excluded_non_target: list[dict[str, Any]] = []
    for item in projection_summary["source_mapping"]:
        operation = str(item["operation"])
        source_ids = [str(value) for value in item["source_event_ids"]]
        output_ids = [str(value) for value in item["output_event_ids"]]
        if operation == "keep":
            source_mapping.append(
                {
                    "operation": "identity",
                    "source_event_ids": source_ids,
                    "output_event_id": output_ids[0],
                    "explicit_operation_id": item["operation_id"],
                }
            )
        elif operation == "compound_handover_chain":
            for event_id in output_ids:
                source_mapping.append(
                    {
                        "operation": "identity",
                        "source_event_ids": [event_id],
                        "output_event_id": event_id,
                        "explicit_operation_id": item["operation_id"],
                        "physical_chain_id": item["physical_chain_id"],
                    }
                )
        elif operation == "collapse_return_chain":
            source_mapping.append(
                {
                    "operation": "collapse_surgeon_scrub_mayo",
                    "source_event_ids": source_ids,
                    "output_event_id": output_ids[0],
                    "explicit_operation_id": item["operation_id"],
                    "physical_chain_id": item["physical_chain_id"],
                }
            )
        elif operation == "exclude_cleanup_chain":
            excluded_roundtrips.append(
                {
                    "source_event_ids": source_ids,
                    "operation_id": item["operation_id"],
                    "physical_chain_id": item["physical_chain_id"],
                    "reason": item["reason"],
                }
            )
        elif operation == "exclude_non_target_event":
            excluded_non_target.append(
                {
                    "source_event_ids": source_ids,
                    "operation_id": item["operation_id"],
                    "reason": item["reason"],
                }
            )
        else:  # pragma: no cover - guarded by the projection schema
            raise FinalizationError(
                f"GUI 호환 projection operation을 만들 수 없습니다: {operation}"
            )
    return {
        "collapsed_returns": copy.deepcopy(
            projection_summary["derived_outputs"]
        ),
        "excluded_roundtrips": excluded_roundtrips,
        # The existing FinalReviewBundle uses this key for explicit observed
        # events intentionally excluded from the DT scoring layer.
        "excluded_unclosed_direct_returns": excluded_non_target,
        "source_mapping": source_mapping,
    }


def _relative_artifact_path(*, case_dir: Path, artifact_path: Path) -> str:
    return os.path.relpath(artifact_path.resolve(), case_dir.resolve())


def _layer_descriptor(
    *,
    case_dir: Path,
    output_path: Path,
    data: bytes,
    records: list[dict[str, Any]],
    include_label_origins: bool,
) -> dict[str, Any]:
    descriptor: dict[str, Any] = {
        "file": _relative_artifact_path(
            case_dir=case_dir,
            artifact_path=output_path,
        ),
        "sha256": hashlib.sha256(data).hexdigest(),
        "confirmed_event_count": len(records),
        "event_type_counts": dict(
            sorted(Counter(item["event_type"] for item in records).items())
        ),
    }
    if include_label_origins:
        descriptor["label_origin_counts"] = dict(
            sorted(Counter(item["label_origin"] for item in records).items())
        )
    return descriptor


def manifest_evaluation_reference_descriptor(
    *,
    case_dir: Path,
    source_revision: str,
    adjudication_revision: str,
    observed_output_path: Path,
    observed_data: bytes,
    observed: list[dict[str, Any]],
    dt_output_path: Path,
    dt_data: bytes,
    projected: list[dict[str, Any]],
    report_output_path: Path,
    report_sha256: str | None,
    phase_output_path: Path | None,
    phase_data: bytes | None,
    phases: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the exact evaluation_reference fields for manifest assembly.

    ``report_sha256`` is ``None`` while the report body is being composed and
    is the published report digest in the finalizer return value.
    """

    reference: dict[str, Any] = {
        "authority": (
            "user_authorized_ai_assistant_review_plus_"
            "explicit_event_id_dt_projection"
        ),
        "complete": True,
        "phase_reference_included": phase_data is not None,
        "information_boundary": (
            "evaluation_only_never_vlm_reducer_bt_runtime_input"
        ),
        "source_revision": source_revision,
        "adjudication_revision": adjudication_revision,
        "observed_reference": _layer_descriptor(
            case_dir=case_dir,
            output_path=observed_output_path,
            data=observed_data,
            records=observed,
            include_label_origins=True,
        ),
        "dt_reference": _layer_descriptor(
            case_dir=case_dir,
            output_path=dt_output_path,
            data=dt_data,
            records=projected,
            include_label_origins=True,
        ),
        "projection_report_file": _relative_artifact_path(
            case_dir=case_dir,
            artifact_path=report_output_path,
        ),
    }
    if report_sha256 is not None:
        reference["projection_report_sha256"] = report_sha256
    if (
        phase_output_path is not None
        and phase_data is not None
    ):
        reference["phase_reference"] = {
            "file": _relative_artifact_path(
                case_dir=case_dir,
                artifact_path=phase_output_path,
            ),
            "sha256": hashlib.sha256(phase_data).hexdigest(),
            "event_count": len(phases),
            "event_type_counts": dict(
                sorted(Counter(item["event_type"] for item in phases).items())
            ),
            "review_status_counts": dict(
                sorted(
                    Counter(item["review_status"] for item in phases).items()
                )
            ),
            "status": "provisional_ambiguous",
            "scoring_role": "context_only_not_ground_truth",
        }
    return reference


def load_phase_context(
    *,
    phase_context_path: Path,
    case_id: str,
    timeline: dict[str, Any],
    point_schema: dict[str, Any],
    interval_schema: dict[str, Any],
    tool_ids: set[str],
) -> list[dict[str, Any]]:
    phases = load_jsonl(phase_context_path)
    for line_number, phase in enumerate(phases, 1):
        if phase.get("event_type") != "phase_start":
            raise FinalizationError(
                f"phase context line {line_number}: phase_start만 허용됩니다."
            )
        if phase.get("review_status") != "ambiguous":
            raise FinalizationError(
                f"phase context line {line_number}: provisional phase는 "
                "review_status=ambiguous여야 합니다."
            )
        validate_reviewed_at_not_future(
            phase.get("review", {}).get("reviewed_at"),
            context=f"phase context line {line_number}",
        )
    phases.sort(key=lambda item: (float(item["time_sec"]), item["event_id"]))
    validate_records(
        phases,
        case_id=case_id,
        timeline=timeline,
        point_schema=point_schema,
        interval_schema=interval_schema,
        tool_ids=tool_ids,
    )
    return phases


def finalize(
    *,
    case_dir: Path,
    adjudications_path: Path,
    projection_path: Path,
    timeline_path: Path,
    adjudication_schema_path: Path,
    projection_schema_path: Path,
    point_schema_path: Path,
    interval_schema_path: Path,
    tools_path: Path,
    observed_output_path: Path,
    dt_output_path: Path,
    report_output_path: Path,
    phase_context_path: Path | None = None,
    phase_output_path: Path | None = None,
) -> dict[str, Any]:
    if (phase_context_path is None) != (phase_output_path is None):
        raise FinalizationError(
            "phase context input과 phase output은 함께 지정해야 합니다."
        )
    timeline = load_json(timeline_path)
    case_id = str(timeline.get("case_id", ""))
    if not case_id or case_dir.name != case_id:
        raise FinalizationError("case directory와 timeline case_id가 다릅니다.")
    adjudications = load_jsonl(adjudications_path)
    adjudication_schema = load_json(adjudication_schema_path)
    projection = load_json(projection_path)
    projection_schema = load_json(projection_schema_path)
    point_schema = load_json(point_schema_path)
    interval_schema = load_json(interval_schema_path)
    tool_ids = _load_tool_ids(tools_path)

    observed, adjudication_summary = materialize_observed(
        adjudications=adjudications,
        adjudications_path=adjudications_path,
        adjudication_schema=adjudication_schema,
        case_id=case_id,
        timeline=timeline,
        timeline_path=timeline_path,
    )
    validate_records(
        observed,
        case_id=case_id,
        timeline=timeline,
        point_schema=point_schema,
        interval_schema=interval_schema,
        tool_ids=tool_ids,
    )
    adjudications_sha256 = sha256_file(adjudications_path)
    projected, projection_summary = apply_explicit_projection(
        observed=observed,
        projection=projection,
        projection_schema=projection_schema,
        case_id=case_id,
        projection_path=projection_path,
        adjudications_path=adjudications_path,
        adjudications_sha256=adjudications_sha256,
    )
    validate_records(
        projected,
        case_id=case_id,
        timeline=timeline,
        point_schema=point_schema,
        interval_schema=interval_schema,
        tool_ids=tool_ids,
    )

    phases: list[dict[str, Any]] = []
    if phase_context_path is not None:
        phases = load_phase_context(
            phase_context_path=phase_context_path,
            case_id=case_id,
            timeline=timeline,
            point_schema=point_schema,
            interval_schema=interval_schema,
            tool_ids=tool_ids,
        )

    observed_data = encode_jsonl(observed)
    dt_data = encode_jsonl(projected)
    phase_data = encode_jsonl(phases) if phase_context_path is not None else None
    input_hashes = {
        "adjudications": sha256_file(adjudications_path),
        "projection": sha256_file(projection_path),
        "timeline": sha256_file(timeline_path),
        "adjudication_schema": sha256_file(adjudication_schema_path),
        "projection_schema": sha256_file(projection_schema_path),
        "point_schema": sha256_file(point_schema_path),
        "interval_schema": sha256_file(interval_schema_path),
        "tool_catalog": sha256_file(tools_path),
    }
    if phase_context_path is not None:
        input_hashes["phase_context"] = sha256_file(phase_context_path)
    source_revision = input_hashes["timeline"][:16]
    adjudication_revision = hashlib.sha256(
        (
            input_hashes["adjudications"]
            + ":"
            + input_hashes["projection"]
        ).encode("ascii")
    ).hexdigest()[:16]
    compatible_operations = final_review_compatible_operations(
        projection_summary
    )
    manifest_handoff = manifest_evaluation_reference_descriptor(
        case_dir=case_dir,
        source_revision=source_revision,
        adjudication_revision=adjudication_revision,
        observed_output_path=observed_output_path,
        observed_data=observed_data,
        observed=observed,
        dt_output_path=dt_output_path,
        dt_data=dt_data,
        projected=projected,
        report_output_path=report_output_path,
        report_sha256=None,
        phase_output_path=phase_output_path,
        phase_data=phase_data,
        phases=phases,
    )

    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "case_id": case_id,
        "source_revision": source_revision,
        "adjudication_revision": adjudication_revision,
        "authority": {
            "observed_reference": (
                "user_authorized_ai_assistant_video_adjudication"
            ),
            "dt_reference": (
                "explicit_event_id_projection_of_assistant_adjudication"
            ),
            "phase_reference": (
                "provisional_ambiguous_context_only"
                if phase_context_path is not None
                else "not_included"
            ),
            "ground_truth_consumers": ["evaluation_only"],
        },
        "inputs": input_hashes,
        "counts": {
            "adjudication_count": len(adjudications),
            "adjudication_review_status": adjudication_summary[
                "status_counts"
            ],
            "observed_confirmed_count": len(observed),
            "observed_event_type": dict(
                sorted(Counter(item["event_type"] for item in observed).items())
            ),
            "dt_confirmed_count": len(projected),
            "dt_event_type": dict(
                sorted(Counter(item["event_type"] for item in projected).items())
            ),
            "phase_provisional_count": len(phases),
            "phase_review_status": dict(
                sorted(Counter(item["review_status"] for item in phases).items())
            ),
        },
        "review_authority": {
            "reviewer_kind": "ai_assistant",
            "reviewer_ids": adjudication_summary["reviewer_ids"],
            "authorized_by": adjudication_summary["authorized_by"],
        },
        "observed_source_mapping": adjudication_summary["source_mapping"],
        "projection": {
            "projection_id": projection["projection_id"],
            **projection_summary,
        },
        "operations": compatible_operations,
        "compound_action_episodes": projection_summary[
            "compound_action_episodes"
        ],
        "projection_provenance": projection_summary["derived_outputs"],
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
        },
        "information_boundary": {
            "runtime_input_allowed": False,
            "vlm_input_allowed": False,
            "reducer_input_allowed": False,
            "bt_input_allowed": False,
        },
        "manifest_handoff": {
            "evaluation_reference": manifest_handoff,
            "projection_report_sha256": (
                "populate from finalizer summary.report_sha256"
            ),
            "required_manifest_siblings": {
                "minimal_interaction_annotation": [
                    "timeline_file",
                    "timeline_sha256",
                ],
                "phase_annotation_when_phase_reference_included": (
                    "A separately validated provisional phase descriptor is "
                    "required by FinalReviewBundle; this finalizer does not "
                    "invent phase candidates or review actions."
                ),
            },
        },
    }
    if phase_data is not None and phase_output_path is not None:
        report["outputs"]["phase_reference"] = {
            "path": str(phase_output_path.resolve()),
            "sha256": hashlib.sha256(phase_data).hexdigest(),
            "record_count": len(phases),
            "status": "provisional_ambiguous_context_only_not_scored",
        }

    report_data = encode_json(report)
    outputs = {
        observed_output_path: observed_data,
        dt_output_path: dt_data,
        report_output_path: report_data,
    }
    if phase_data is not None and phase_output_path is not None:
        outputs[phase_output_path] = phase_data
    report_sha256 = hashlib.sha256(report_data).hexdigest()
    publish_create_only(outputs)
    manifest_reference = manifest_evaluation_reference_descriptor(
        case_dir=case_dir,
        source_revision=source_revision,
        adjudication_revision=adjudication_revision,
        observed_output_path=observed_output_path,
        observed_data=observed_data,
        observed=observed,
        dt_output_path=dt_output_path,
        dt_data=dt_data,
        projected=projected,
        report_output_path=report_output_path,
        report_sha256=report_sha256,
        phase_output_path=phase_output_path,
        phase_data=phase_data,
        phases=phases,
    )
    return {
        "ok": True,
        "case_id": case_id,
        "adjudication_count": len(adjudications),
        "observed_count": len(observed),
        "dt_count": len(projected),
        "phase_count": len(phases),
        "observed_sha256": hashlib.sha256(observed_data).hexdigest(),
        "dt_sha256": hashlib.sha256(dt_data).hexdigest(),
        "report_sha256": report_sha256,
        "manifest_evaluation_reference": manifest_reference,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Materialize user-authorized assistant interaction adjudications "
            "and an explicit event-ID DT projection."
        )
    )
    parser.add_argument("case_dir", type=Path)
    parser.add_argument("--adjudications", type=Path)
    parser.add_argument("--projection", type=Path)
    parser.add_argument("--timeline", type=Path)
    parser.add_argument("--adjudication-schema", type=Path)
    parser.add_argument("--projection-schema", type=Path)
    parser.add_argument("--point-schema", type=Path)
    parser.add_argument("--interval-schema", type=Path)
    parser.add_argument("--tools", type=Path)
    parser.add_argument("--phase-context", type=Path)
    parser.add_argument("--observed-output", type=Path)
    parser.add_argument("--dt-output", type=Path)
    parser.add_argument("--phase-output", type=Path)
    parser.add_argument("--report-output", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    case_dir = args.case_dir.resolve()
    annotation_root = case_dir.parents[1]
    phase_context_path = (
        args.phase_context.resolve() if args.phase_context else None
    )
    phase_output_path = (
        args.phase_output.resolve()
        if args.phase_output
        else (
            case_dir / "phase_events.provisional.final.v1.jsonl"
            if phase_context_path is not None
            else None
        )
    )
    if args.phase_output and phase_context_path is None:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "--phase-output에는 --phase-context가 필요합니다.",
                },
                ensure_ascii=False,
            )
        )
        return 1
    try:
        summary = finalize(
            case_dir=case_dir,
            adjudications_path=(
                args.adjudications.resolve()
                if args.adjudications
                else case_dir
                / "assistant_interaction_adjudications.final.v1.jsonl"
            ),
            projection_path=(
                args.projection.resolve()
                if args.projection
                else case_dir / "dt_projection.explicit.v1.json"
            ),
            timeline_path=(
                args.timeline.resolve()
                if args.timeline
                else case_dir / "cam4_frame_timeline.v1.json"
            ),
            adjudication_schema_path=(
                args.adjudication_schema.resolve()
                if args.adjudication_schema
                else annotation_root
                / "schema/assistant_interaction_adjudication.v1.schema.json"
            ),
            projection_schema_path=(
                args.projection_schema.resolve()
                if args.projection_schema
                else annotation_root
                / "schema/explicit_dt_interaction_projection.v1.schema.json"
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
            tools_path=(
                args.tools.resolve()
                if args.tools
                else annotation_root / "catalogs/tools.yaml"
            ),
            observed_output_path=(
                args.observed_output.resolve()
                if args.observed_output
                else case_dir / "interaction_events.observed.final.v1.jsonl"
            ),
            dt_output_path=(
                args.dt_output.resolve()
                if args.dt_output
                else case_dir
                / "interaction_events.dt_reference.final.v1.jsonl"
            ),
            phase_context_path=phase_context_path,
            phase_output_path=phase_output_path,
            report_output_path=(
                args.report_output.resolve()
                if args.report_output
                else annotation_root
                / f"reports/{case_dir.name}_assistant_dt_projection.final.v1.json"
            ),
        )
    except (FinalizationError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {"ok": False, "error": str(exc)},
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
