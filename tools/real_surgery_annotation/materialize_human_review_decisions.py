#!/usr/bin/env python3
"""Materialize append-only human decisions into a validated point stream."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import re
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from .interaction_review_gui import canonical_json, sha256_value
from .validate_interaction_points import STREAM_EVENT_TYPES, validate


DECISION_SCHEMA = "taskplanner.human_review_decision.v1"
REPORT_SCHEMA = "taskplanner.human_review_materialization_report.v1"
REVIEW_STATUSES = {"confirmed", "ambiguous", "rejected"}
EVENT_PREFIXES = {
    "implicit_tool_request": "R",
    "tool_transfer": "T",
    "phase_start": "PH",
}
TRANSFER_ENDPOINTS = {"mayo_stand", "scrub_nurse", "surgeon"}
TOOL_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
PHASE_PATTERN = re.compile(r"^P[0-9]{2,}$")


class MaterializationError(Exception):
    """An input contract or create-only publication rule was violated."""


def reject_nonstandard_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON numeric constant: {value}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise MaterializationError(f"{path}: 파일을 읽을 수 없습니다: {exc}") from exc
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=reject_nonstandard_json_constant,
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise MaterializationError(f"{path}: JSON 오류: {exc}") from exc
    if not isinstance(value, dict):
        raise MaterializationError(f"{path}: JSON 객체가 필요합니다.")
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(
                        line,
                        parse_constant=reject_nonstandard_json_constant,
                    )
                except (json.JSONDecodeError, ValueError) as exc:
                    raise MaterializationError(
                        f"{path}:{line_number}: JSONL 오류: {exc}"
                    ) from exc
                if not isinstance(value, dict):
                    raise MaterializationError(
                        f"{path}:{line_number}: 객체 레코드가 필요합니다."
                    )
                records.append(value)
    except OSError as exc:
        raise MaterializationError(f"{path}: JSONL을 읽을 수 없습니다: {exc}") from exc
    return records


def load_tool_ids(path: Path) -> set[str]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise MaterializationError(f"{path}: tool catalog 오류: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("tools"), list):
        raise MaterializationError(f"{path}: tools 목록이 없습니다.")
    tool_ids = {
        str(tool["id"])
        for tool in payload["tools"]
        if isinstance(tool, dict) and tool.get("id")
    }
    if not tool_ids:
        raise MaterializationError(f"{path}: canonical tool ID가 없습니다.")
    return tool_ids


def validate_point_records(
    *,
    records: list[dict[str, Any]],
    schema: dict[str, Any],
    timeline: dict[str, Any],
    tool_ids: set[str],
    case_id: str,
    stream_kind: str,
    label: str,
) -> None:
    with_lines = [
        {**copy.deepcopy(record), "_line": line_number}
        for line_number, record in enumerate(records, 1)
    ]
    errors = validate(
        records=with_lines,
        schema=schema,
        timeline=timeline,
        tool_ids=tool_ids,
        case_id=case_id,
        stream_kind=stream_kind,
    )
    if errors:
        raise MaterializationError(
            f"{label} validation failed:\n" + "\n".join(errors[:24])
        )


def _require_string(record: dict[str, Any], key: str, context: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value.strip():
        raise MaterializationError(f"{context}: {key} 문자열이 필요합니다.")
    return value.strip()


def _validate_review(review: Any, context: str) -> dict[str, Any]:
    if not isinstance(review, dict):
        raise MaterializationError(f"{context}: review 객체가 필요합니다.")
    allowed = {"reviewer_kind", "reviewer_id", "reviewed_at", "notes"}
    unknown = set(review) - allowed
    if unknown:
        raise MaterializationError(
            f"{context}: review에 알 수 없는 필드: {sorted(unknown)}"
        )
    if review.get("reviewer_kind") != "human":
        raise MaterializationError(f"{context}: reviewer_kind는 human이어야 합니다.")
    _require_string(review, "reviewer_id", context)
    _require_string(review, "reviewed_at", context)
    if "notes" in review and not isinstance(review["notes"], str):
        raise MaterializationError(f"{context}: review.notes는 문자열이어야 합니다.")
    return copy.deepcopy(review)


def _validate_fields(
    *,
    fields: Any,
    context: str,
    timestamps: list[float],
    tool_ids: set[str],
    allowed_types: set[str],
) -> dict[str, Any]:
    if not isinstance(fields, dict):
        raise MaterializationError(
            f"{context}: adjudicated_fields 객체가 필요합니다."
        )
    allowed_keys = {
        "event_type",
        "source_frame_idx",
        "time_sec",
        "tool",
        "from",
        "to",
        "phase_id",
        "source_views",
    }
    unknown = set(fields) - allowed_keys
    if unknown:
        raise MaterializationError(
            f"{context}: adjudicated_fields에 알 수 없는 필드: {sorted(unknown)}"
        )
    event_type = _require_string(fields, "event_type", context)
    if event_type not in allowed_types:
        raise MaterializationError(
            f"{context}: {event_type}은 {sorted(allowed_types)}에 포함되지 않습니다."
        )
    source_frame_idx = fields.get("source_frame_idx")
    if isinstance(source_frame_idx, bool) or not isinstance(source_frame_idx, int):
        raise MaterializationError(f"{context}: source_frame_idx는 정수여야 합니다.")
    if not 0 <= source_frame_idx < len(timestamps):
        raise MaterializationError(
            f"{context}: source_frame_idx가 timeline 범위 밖입니다."
        )
    time_sec = fields.get("time_sec")
    expected_time = timestamps[source_frame_idx]
    if (
        isinstance(time_sec, bool)
        or not isinstance(time_sec, (int, float))
        or not math.isfinite(float(time_sec))
    ):
        raise MaterializationError(f"{context}: time_sec 숫자가 필요합니다.")
    if abs(float(time_sec) - expected_time) > 5e-10:
        raise MaterializationError(
            f"{context}: time_sec {time_sec}가 frame {source_frame_idx}의 "
            f"canonical timestamp {expected_time}와 다릅니다."
        )
    source_views = fields.get("source_views")
    if (
        not isinstance(source_views, list)
        or not source_views
        or len(source_views) != len(set(source_views))
        or any(view not in {"cam4", "flir"} for view in source_views)
    ):
        raise MaterializationError(
            f"{context}: source_views는 중복 없는 cam4/flir 목록이어야 합니다."
        )

    tool = fields.get("tool")
    from_location = fields.get("from")
    to_location = fields.get("to")
    phase_id = fields.get("phase_id")
    if event_type == "tool_transfer":
        if (
            not isinstance(tool, str)
            or TOOL_PATTERN.fullmatch(tool) is None
            or tool not in tool_ids
        ):
            raise MaterializationError(
                f"{context}: canonical tool ID가 필요합니다."
            )
        if from_location not in TRANSFER_ENDPOINTS:
            raise MaterializationError(f"{context}: from 위치가 올바르지 않습니다.")
        if to_location not in TRANSFER_ENDPOINTS:
            raise MaterializationError(f"{context}: to 위치가 올바르지 않습니다.")
        if from_location == to_location:
            raise MaterializationError(f"{context}: from과 to는 달라야 합니다.")
        if phase_id is not None:
            raise MaterializationError(
                f"{context}: tool_transfer에는 phase_id를 넣을 수 없습니다."
            )
    elif event_type == "phase_start":
        if not isinstance(phase_id, str) or PHASE_PATTERN.fullmatch(phase_id) is None:
            raise MaterializationError(
                f"{context}: P00 형식 phase_id가 필요합니다."
            )
        if any(value is not None for value in (tool, from_location, to_location)):
            raise MaterializationError(
                f"{context}: phase_start에는 tool/from/to를 넣을 수 없습니다."
            )
    else:
        if any(
            value is not None
            for value in (tool, from_location, to_location, phase_id)
        ):
            raise MaterializationError(
                f"{context}: implicit request에는 tool/from/to/phase_id를 "
                "넣을 수 없습니다."
            )

    return {
        "event_type": event_type,
        "source_frame_idx": source_frame_idx,
        "time_sec": expected_time,
        "tool": tool,
        "from": from_location,
        "to": to_location,
        "phase_id": phase_id,
        "source_views": list(source_views),
    }


def validate_decisions(
    *,
    decisions: list[dict[str, Any]],
    candidates_by_id: dict[str, dict[str, Any]],
    timeline: dict[str, Any],
    tool_ids: set[str],
    case_id: str,
    stream_kind: str,
) -> dict[str, dict[str, Any]]:
    timestamps = [float(value) for value in timeline["timestamps_sec"]]
    allowed_types = STREAM_EVENT_TYPES[stream_kind]
    by_candidate: dict[str, dict[str, Any]] = {}
    seen_decision_ids: set[str] = set()
    allowed_decision_keys = {
        "schema",
        "case_id",
        "decision_id",
        "candidate_id",
        "candidate_sha256",
        "request_sha256",
        "review_status",
        "resulting_label_origin",
        "adjudicated_fields",
        "review",
    }

    for line_number, decision in enumerate(decisions, 1):
        context = f"decision line {line_number}"
        unknown = set(decision) - allowed_decision_keys
        if unknown:
            raise MaterializationError(
                f"{context}: 알 수 없는 필드: {sorted(unknown)}"
            )
        if decision.get("schema") != DECISION_SCHEMA:
            raise MaterializationError(f"{context}: decision schema 불일치")
        if decision.get("case_id") != case_id:
            raise MaterializationError(f"{context}: case_id 불일치")
        decision_id = _require_string(decision, "decision_id", context)
        if decision_id in seen_decision_ids:
            raise MaterializationError(f"{context}: 중복 decision_id {decision_id}")
        seen_decision_ids.add(decision_id)
        candidate_id = _require_string(decision, "candidate_id", context)
        if candidate_id in by_candidate:
            raise MaterializationError(
                f"{context}: candidate {candidate_id}에 중복 판정"
            )
        candidate = candidates_by_id.get(candidate_id)
        if candidate is None:
            raise MaterializationError(
                f"{context}: candidate를 찾을 수 없습니다: {candidate_id}"
            )
        expected_digest = sha256_value(candidate)
        if decision.get("candidate_sha256") != expected_digest:
            raise MaterializationError(
                f"{context}: candidate digest mismatch for {candidate_id}"
            )
        review_status = decision.get("review_status")
        if review_status not in REVIEW_STATUSES:
            raise MaterializationError(f"{context}: review_status가 올바르지 않습니다.")
        expected_origin = (
            "human_video_review" if review_status == "confirmed" else None
        )
        if decision.get("resulting_label_origin") != expected_origin:
            raise MaterializationError(
                f"{context}: resulting_label_origin이 판정과 일치하지 않습니다."
            )
        review = _validate_review(decision.get("review"), context)
        fields = _validate_fields(
            fields=decision.get("adjudicated_fields"),
            context=context,
            timestamps=timestamps,
            tool_ids=tool_ids,
            allowed_types=allowed_types,
        )
        semantic_request = {
            "candidate_id": candidate_id,
            "candidate_sha256": expected_digest,
            "review_status": review_status,
            "reviewer_id": review["reviewer_id"],
            "notes": review.get("notes", ""),
            "adjudicated_fields": fields,
        }
        if decision.get("request_sha256") != sha256_value(semantic_request):
            raise MaterializationError(f"{context}: request_sha256 불일치")
        clean = copy.deepcopy(decision)
        clean["review"] = review
        clean["adjudicated_fields"] = fields
        by_candidate[candidate_id] = clean
    return by_candidate


def _apply_fields(
    *,
    candidate: dict[str, Any],
    decision: dict[str, Any],
) -> dict[str, Any]:
    record = copy.deepcopy(candidate)
    fields = decision["adjudicated_fields"]
    for key in ("tool", "from", "to", "phase_id", "review"):
        record.pop(key, None)
    record.update(
        {
            "event_type": fields["event_type"],
            "time_sec": fields["time_sec"],
            "source_frame_idx": fields["source_frame_idx"],
            "source_views": fields["source_views"],
            "review_status": decision["review_status"],
            "review": copy.deepcopy(decision["review"]),
        }
    )
    for key in ("tool", "from", "to", "phase_id"):
        if fields[key] is not None:
            record[key] = fields[key]
    if decision["review_status"] == "confirmed":
        record["label_origin"] = "human_video_review"
    # ambiguous/rejected deliberately retain the candidate's proposal origin.
    return record


def assign_event_ids(
    *,
    records: list[tuple[str, dict[str, Any], str]],
    all_candidate_ids: set[str],
    case_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    used = set(all_candidate_ids)
    mappings: list[dict[str, Any]] = []
    changed = [
        item
        for item in records
        if item[1]["event_type"] != item[2]
    ]
    changed.sort(
        key=lambda item: (
            float(item[1]["time_sec"]),
            item[0],
        )
    )
    new_ids: dict[str, str] = {}
    next_suffix = {prefix: 1 for prefix in EVENT_PREFIXES.values()}
    for original_id, record, _original_type in changed:
        prefix = EVENT_PREFIXES[record["event_type"]]
        suffix = next_suffix[prefix]
        while f"{case_id}-{prefix}{suffix:04d}" in used:
            suffix += 1
        assigned = f"{case_id}-{prefix}{suffix:04d}"
        next_suffix[prefix] = suffix + 1
        used.add(assigned)
        new_ids[original_id] = assigned

    output: list[dict[str, Any]] = []
    for original_id, record, original_type in records:
        assigned = new_ids.get(original_id, original_id)
        record["event_id"] = assigned
        output.append(record)
        mappings.append(
            {
                "candidate_id": original_id,
                "event_id": assigned,
                "event_type_changed": record["event_type"] != original_type,
                "original_event_type": original_type,
                "materialized_event_type": record["event_type"],
            }
        )
    output.sort(key=lambda item: (float(item["time_sec"]), item["event_id"]))
    mappings.sort(key=lambda item: item["candidate_id"])
    return output, mappings


def encode_jsonl(records: list[dict[str, Any]]) -> bytes:
    return "".join(canonical_json(record) + "\n" for record in records).encode(
        "utf-8"
    )


def encode_report(report: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


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
                raise MaterializationError(f"{path}: staging write 실패")
            offset += written
        os.fsync(descriptor)
        succeeded = True
    finally:
        os.close(descriptor)
        if not succeeded:
            temporary.unlink(missing_ok=True)
    return temporary


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish_create_only(
    *,
    output_path: Path,
    output_data: bytes,
    report_path: Path,
    report_data: bytes,
) -> None:
    if output_path == report_path:
        raise MaterializationError("output과 report 경로는 달라야 합니다.")
    if output_path.exists():
        raise MaterializationError(f"refusing to overwrite output: {output_path}")
    if report_path.exists():
        raise MaterializationError(f"refusing to overwrite report: {report_path}")
    output_tmp: Path | None = None
    report_tmp: Path | None = None
    output_published = False
    report_published = False
    try:
        output_tmp = _stage(output_path, output_data)
        report_tmp = _stage(report_path, report_data)
        os.link(output_tmp, output_path)
        output_published = True
        os.link(report_tmp, report_path)
        report_published = True
        _fsync_directory(output_path.parent)
        if report_path.parent != output_path.parent:
            _fsync_directory(report_path.parent)
    except FileExistsError as exc:
        raise MaterializationError(
            f"create-only target appeared during publish: {exc.filename}"
        ) from exc
    except OSError as exc:
        raise MaterializationError(f"atomic publish 실패: {exc}") from exc
    finally:
        if not report_published and output_published:
            try:
                if (
                    output_tmp is not None
                    and output_path.exists()
                    and os.path.samefile(output_path, output_tmp)
                ):
                    output_path.unlink()
            except OSError:
                pass
        if not output_published and report_published:
            try:
                if (
                    report_tmp is not None
                    and report_path.exists()
                    and os.path.samefile(report_path, report_tmp)
                ):
                    report_path.unlink()
            except OSError:
                pass
        if output_tmp is not None:
            output_tmp.unlink(missing_ok=True)
        if report_tmp is not None:
            report_tmp.unlink(missing_ok=True)


def materialize(
    *,
    candidates_path: Path,
    decisions_path: Path,
    schema_path: Path,
    timeline_path: Path,
    tools_path: Path,
    case_id: str,
    stream_kind: str,
    output_path: Path,
    report_path: Path,
    require_all: bool,
) -> dict[str, Any]:
    if stream_kind not in STREAM_EVENT_TYPES:
        raise MaterializationError(f"지원하지 않는 stream kind: {stream_kind}")
    if output_path.exists():
        raise MaterializationError(f"refusing to overwrite output: {output_path}")
    if report_path.exists():
        raise MaterializationError(f"refusing to overwrite report: {report_path}")

    schema = load_json(schema_path)
    timeline = load_json(timeline_path)
    if timeline.get("case_id") != case_id:
        raise MaterializationError("timeline case_id가 --case-id와 다릅니다.")
    timestamps = timeline.get("timestamps_sec")
    if not isinstance(timestamps, list) or not timestamps:
        raise MaterializationError("timeline timestamps_sec가 비어 있습니다.")
    tool_ids = load_tool_ids(tools_path)
    candidates = load_jsonl(candidates_path)
    decisions = load_jsonl(decisions_path)
    validate_point_records(
        records=candidates,
        schema=schema,
        timeline=timeline,
        tool_ids=tool_ids,
        case_id=case_id,
        stream_kind=stream_kind,
        label="candidate",
    )

    candidates_by_id: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        candidate_id = str(candidate.get("event_id", ""))
        if candidate_id in candidates_by_id:
            raise MaterializationError(f"중복 candidate event_id: {candidate_id}")
        candidates_by_id[candidate_id] = candidate
    decisions_by_candidate = validate_decisions(
        decisions=decisions,
        candidates_by_id=candidates_by_id,
        timeline=timeline,
        tool_ids=tool_ids,
        case_id=case_id,
        stream_kind=stream_kind,
    )
    unreviewed_ids = sorted(set(candidates_by_id) - set(decisions_by_candidate))
    if require_all and unreviewed_ids:
        raise MaterializationError(
            f"--require-all: {len(unreviewed_ids)}개 후보가 미검토 상태입니다: "
            + ", ".join(unreviewed_ids[:12])
        )

    materialized_with_context: list[tuple[str, dict[str, Any], str]] = []
    for candidate_id, decision in decisions_by_candidate.items():
        candidate = candidates_by_id[candidate_id]
        record = _apply_fields(candidate=candidate, decision=decision)
        materialized_with_context.append(
            (candidate_id, record, str(candidate["event_type"]))
        )
    output_records, event_id_mappings = assign_event_ids(
        records=materialized_with_context,
        all_candidate_ids=set(candidates_by_id),
        case_id=case_id,
    )
    validate_point_records(
        records=output_records,
        schema=schema,
        timeline=timeline,
        tool_ids=tool_ids,
        case_id=case_id,
        stream_kind=stream_kind,
        label="materialized output",
    )

    output_data = encode_jsonl(output_records)
    output_sha256 = hashlib.sha256(output_data).hexdigest()
    report = {
        "schema": REPORT_SCHEMA,
        "case_id": case_id,
        "stream_kind": stream_kind,
        "require_all": require_all,
        "inputs": {
            "candidates": str(candidates_path.resolve()),
            "candidates_sha256": sha256_file(candidates_path),
            "decisions": str(decisions_path.resolve()),
            "decisions_sha256": sha256_file(decisions_path),
            "point_schema": str(schema_path.resolve()),
            "point_schema_sha256": sha256_file(schema_path),
            "timeline": str(timeline_path.resolve()),
            "timeline_sha256": sha256_file(timeline_path),
            "tools": str(tools_path.resolve()),
            "tools_sha256": sha256_file(tools_path),
        },
        "output": {
            "path": str(output_path.resolve()),
            "sha256": output_sha256,
            "record_count": len(output_records),
        },
        "counts": {
            "candidate_count": len(candidates),
            "decision_count": len(decisions),
            "materialized_count": len(output_records),
            "unreviewed_count": len(unreviewed_ids),
            "review_status": dict(
                sorted(Counter(
                    record["review_status"] for record in output_records
                ).items())
            ),
            "label_origin": dict(
                sorted(Counter(
                    record["label_origin"] for record in output_records
                ).items())
            ),
            "event_type": dict(
                sorted(Counter(
                    record["event_type"] for record in output_records
                ).items())
            ),
        },
        "unreviewed_candidate_ids": unreviewed_ids,
        "event_id_mappings": event_id_mappings,
        "compatibility": {
            "legacy_evaluator_adapter_included": False,
            "note": (
                "Adapting this point schema to the legacy tool-event evaluator "
                "is outside this materializer."
            ),
        },
    }
    report_data = encode_report(report)
    publish_create_only(
        output_path=output_path,
        output_data=output_data,
        report_path=report_path,
        report_data=report_data,
    )
    return {
        "ok": True,
        "output": str(output_path),
        "output_sha256": output_sha256,
        "report": str(report_path),
        "report_sha256": hashlib.sha256(report_data).hexdigest(),
        "record_count": len(output_records),
        "unreviewed_count": len(unreviewed_ids),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create a reviewed interaction/phase point JSONL from immutable "
            "AI candidates and append-only human decisions."
        )
    )
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--timeline", type=Path, required=True)
    parser.add_argument("--tools", type=Path, required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument(
        "--stream-kind",
        choices=sorted(STREAM_EVENT_TYPES),
        required=True,
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--require-all", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        summary = materialize(
            candidates_path=args.candidates.resolve(),
            decisions_path=args.decisions.resolve(),
            schema_path=args.schema.resolve(),
            timeline_path=args.timeline.resolve(),
            tools_path=args.tools.resolve(),
            case_id=args.case_id,
            stream_kind=args.stream_kind,
            output_path=args.output.resolve(),
            report_path=args.report.resolve(),
            require_all=args.require_all,
        )
    except MaterializationError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
