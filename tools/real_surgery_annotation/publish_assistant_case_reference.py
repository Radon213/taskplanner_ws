#!/usr/bin/env python3
"""Publish one assistant-primary case manifest after strict final-artifact QA.

This tool does not infer labels, Phase boundaries, scoring roles, or DT chains.
It only verifies already authored assistant adjudications, an explicit
event-ID projection, provisional Phase context, voice context, and evaluation
masks before publishing a create-only manifest with a closed legacy injection
gate.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
from collections import Counter
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from .finalize_interaction_review import publish_create_only


MANIFEST_SCHEMA = "taskplanner.observable_annotation_manifest.v1"
ASSISTANT_PHASE_AUTHORITY = (
    "user_authorized_ai_assistant_video_adjudication_"
    "provisional_context_not_scoring_ground_truth"
)
INFORMATION_BOUNDARY = (
    "evaluation_only_never_vlm_reducer_bt_runtime_input"
)
METRIC_KEYS = (
    "action",
    "latency",
    "state",
    "physical",
    "reuse",
    "gesture_presence",
    "gesture_onset",
    "phase_accuracy",
    "actor_identity",
)


class PublicationError(ValueError):
    """A finalized artifact set is incomplete or internally inconsistent."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublicationError(f"{path}: JSON을 읽을 수 없습니다: {exc}") from exc
    if not isinstance(value, dict):
        raise PublicationError(f"{path}: JSON object가 필요합니다.")
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise PublicationError(
                        f"{path}:{line_number}: JSON object가 필요합니다."
                    )
                records.append(value)
    except (OSError, json.JSONDecodeError) as exc:
        raise PublicationError(f"{path}: JSONL을 읽을 수 없습니다: {exc}") from exc
    return records


def encode_json(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _require_file(path: Path, *, label: str) -> Path:
    path = path.resolve()
    if not path.is_file():
        raise PublicationError(f"{label} 파일이 없습니다: {path}")
    return path


def _relative(path: Path, *, case_dir: Path) -> str:
    return os.path.relpath(path.resolve(), case_dir.resolve())


def _validate_case_records(
    records: list[dict[str, Any]],
    *,
    case_id: str,
    label: str,
) -> None:
    seen: set[str] = set()
    for index, record in enumerate(records, 1):
        if record.get("case_id") != case_id:
            raise PublicationError(f"{label}:{index}: case_id 불일치")
        event_id = str(
            record.get("event_id", record.get("adjudication_id", ""))
        )
        if not event_id or event_id in seen:
            raise PublicationError(f"{label}:{index}: ID가 없거나 중복입니다.")
        seen.add(event_id)


def _require_hash(
    path: Path,
    expected: Any,
    *,
    label: str,
) -> None:
    actual = sha256_file(path)
    if not isinstance(expected, str) or actual != expected:
        raise PublicationError(
            f"{label} SHA256 불일치: expected={expected!r}, actual={actual}"
        )


def _validated_output(
    report: dict[str, Any],
    *,
    output_key: str,
    expected_path: Path,
    expected_records: list[dict[str, Any]],
) -> None:
    descriptor = report.get("outputs", {}).get(output_key)
    if not isinstance(descriptor, dict):
        raise PublicationError(f"projection report output {output_key}가 없습니다.")
    if Path(str(descriptor.get("path", ""))).resolve() != expected_path.resolve():
        raise PublicationError(f"projection report {output_key} 경로가 다릅니다.")
    if descriptor.get("record_count") != len(expected_records):
        raise PublicationError(f"projection report {output_key} count가 다릅니다.")
    _require_hash(
        expected_path,
        descriptor.get("sha256"),
        label=f"projection report {output_key}",
    )


def _validate_metric_contract(value: Any, *, location: str) -> None:
    if (
        not isinstance(value, dict)
        or set(value) != set(METRIC_KEYS)
        or any(not isinstance(value[key], bool) for key in METRIC_KEYS)
    ):
        raise PublicationError(f"{location}: metric eligibility가 불완전합니다.")


def validate_evaluation_masks(
    *,
    masks: dict[str, Any],
    mask_schema: dict[str, Any],
    case_id: str,
    observed: list[dict[str, Any]],
    phases: list[dict[str, Any]],
    voice: list[dict[str, Any]],
    timeline: dict[str, Any],
) -> None:
    errors = sorted(
        jsonschema.Draft202012Validator(mask_schema).iter_errors(masks),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        first = errors[0]
        path = ".".join(str(part) for part in first.absolute_path)
        raise PublicationError(
            f"evaluation mask schema {path or '<root>'}: {first.message}"
        )
    if masks.get("case_id") != case_id:
        raise PublicationError("evaluation mask case_id 불일치")
    scope = masks["evaluation_scope"]
    if (
        scope.get("classification") != "development_calibration"
        or scope.get("held_out_eligible") is not False
    ):
        raise PublicationError(
            "현재 사례군은 development_calibration/held_out=false여야 합니다."
        )
    _validate_metric_contract(
        masks["default_metric_eligibility"],
        location="default_metric_eligibility",
    )
    if any(masks["default_metric_eligibility"].values()):
        raise PublicationError("evaluation mask default는 모두 false여야 합니다.")

    target_ids = {
        str(record["event_id"]) for record in [*observed, *phases]
    }
    role_ids: list[str] = []
    for index, role in enumerate(masks["event_roles"], 1):
        role_ids.append(str(role["event_id"]))
        _validate_metric_contract(
            role["metric_eligibility"],
            location=f"event_roles[{index}]",
        )
    if len(role_ids) != len(set(role_ids)):
        raise PublicationError("evaluation mask event role ID가 중복입니다.")
    if set(role_ids) != target_ids:
        missing = sorted(target_ids - set(role_ids))
        extra = sorted(set(role_ids) - target_ids)
        raise PublicationError(
            f"evaluation mask event roles가 exhaustive하지 않습니다: "
            f"missing={missing}, extra={extra}"
        )

    voice_ids = [str(item["event_id"]) for item in voice]
    mask_voice_ids = [
        str(item["event_id"]) for item in masks["voice_context_roles"]
    ]
    if (
        len(mask_voice_ids) != len(set(mask_voice_ids))
        or set(mask_voice_ids) != set(voice_ids)
    ):
        raise PublicationError(
            "evaluation mask voice roles가 source voice를 정확히 한 번 "
            "포함하지 않습니다."
        )

    visual_end = float(timeline["end_sec"])
    cutoffs = masks["cutoffs"]
    if not math.isclose(
        float(cutoffs["visual_end_sec"]),
        visual_end,
        rel_tol=0,
        abs_tol=5e-10,
    ):
        raise PublicationError("evaluation mask visual_end_sec 불일치")
    expected_voice_end = max(
        (
            float(record.get("available_sec", record["end_sec"]))
            for record in voice
        ),
        default=visual_end,
    )
    if not math.isclose(
        float(cutoffs["voice_context_end_sec"]),
        expected_voice_end,
        rel_tol=0,
        abs_tol=5e-10,
    ):
        raise PublicationError("evaluation mask voice_context_end_sec 불일치")
    if float(cutoffs["action_and_next_tool_end_sec"]) > visual_end + 5e-10:
        raise PublicationError("action cutoff가 visual end 이후입니다.")
    if float(cutoffs["state_audit_end_sec"]) > visual_end + 5e-10:
        raise PublicationError("state audit cutoff가 visual end 이후입니다.")
    for index, interval in enumerate(masks["interval_masks"], 1):
        _validate_metric_contract(
            interval["metric_eligibility"],
            location=f"interval_masks[{index}]",
        )
        start_sec = float(interval["start_sec"])
        end_sec = float(interval["end_sec"])
        if end_sec < start_sec:
            raise PublicationError(
                f"interval_masks[{index}] start/end 순서가 잘못됐습니다."
            )

    observed_tools = {
        str(record["tool"])
        for record in observed
        if record.get("event_type") == "tool_transfer"
    }
    scoped_tools = [
        str(item["tool"]) for item in masks["tool_metric_scopes"]
    ]
    if (
        len(scoped_tools) != len(set(scoped_tools))
        or set(scoped_tools) != observed_tools
    ):
        raise PublicationError(
            "tool_metric_scopes가 observed tool type을 정확히 한 번 "
            "포함하지 않습니다."
        )
    for item in masks["tool_metric_scopes"]:
        if item["state"] or item["physical"] or item["reuse"]:
            raise PublicationError(
                "instance-resolved inventory가 없는 현재 reference에서는 "
                "state/physical/reuse를 열 수 없습니다."
            )


def _source_bag_descriptor(
    *,
    bag_dir: Path,
) -> tuple[dict[str, Any], float]:
    metadata_path = _require_file(bag_dir / "metadata.yaml", label="rosbag metadata")
    try:
        metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise PublicationError(f"rosbag metadata를 읽을 수 없습니다: {exc}") from exc
    info = metadata.get("rosbag2_bagfile_information")
    if not isinstance(info, dict):
        raise PublicationError("rosbag metadata information이 없습니다.")
    files = sorted(bag_dir.glob("*.mcap"))
    if len(files) != 1:
        raise PublicationError(
            f"source bag에는 MCAP 하나가 필요합니다: {files}"
        )
    duration_ns = info.get("duration", {}).get("nanoseconds")
    if not isinstance(duration_ns, int) or duration_ns <= 0:
        raise PublicationError("rosbag duration이 올바르지 않습니다.")
    topics = info.get("topics_with_message_count")
    if not isinstance(topics, list):
        raise PublicationError("rosbag topic metadata가 없습니다.")
    return (
        {
            "directory": str(bag_dir.resolve()),
            "mcap_file": files[0].name,
            "mcap_sha256": sha256_file(files[0]),
            "message_count": info.get("message_count"),
            "metadata_sha256": sha256_file(metadata_path),
            "topic_count": len(topics),
        },
        duration_ns / 1_000_000_000,
    )


def build_manifest(
    *,
    case_dir: Path,
    report_path: Path,
    information_boundary_report_path: Path,
    phase_reference_path: Path | None = None,
    evaluation_masks_path: Path | None = None,
    adjudications_path: Path | None = None,
    projection_path: Path | None = None,
    observed_reference_path: Path | None = None,
    dt_reference_path: Path | None = None,
    reconciliation_path: Path | None = None,
) -> dict[str, Any]:
    case_dir = case_dir.resolve()
    case_id = case_dir.name
    annotation_root = case_dir.parents[1]
    schema_root = annotation_root / "schema"
    catalog_root = annotation_root / "catalogs"

    timeline_path = _require_file(
        case_dir / "cam4_frame_timeline.v1.json",
        label="timeline",
    )
    adjudications_path = _require_file(
        (
            adjudications_path.resolve()
            if adjudications_path is not None
            else case_dir / "assistant_interaction_adjudications.final.v1.jsonl"
        ),
        label="assistant adjudications",
    )
    projection_path = _require_file(
        (
            projection_path.resolve()
            if projection_path is not None
            else case_dir / "dt_projection.explicit.v1.json"
        ),
        label="explicit projection",
    )
    observed_path = _require_file(
        (
            observed_reference_path.resolve()
            if observed_reference_path is not None
            else case_dir / "interaction_events.observed.final.v1.jsonl"
        ),
        label="observed reference",
    )
    dt_path = _require_file(
        (
            dt_reference_path.resolve()
            if dt_reference_path is not None
            else case_dir / "interaction_events.dt_reference.final.v1.jsonl"
        ),
        label="DT reference",
    )
    for label, path in (
        ("assistant adjudications", adjudications_path),
        ("explicit projection", projection_path),
        ("observed reference", observed_path),
        ("DT reference", dt_path),
    ):
        if path.parent != case_dir:
            raise PublicationError(f"{label}는 case directory 안에 있어야 합니다.")
    phase_path = _require_file(
        (
            phase_reference_path.resolve()
            if phase_reference_path is not None
            else case_dir / "phase_events.provisional.final.v1.jsonl"
        ),
        label="provisional Phase",
    )
    if phase_path.parent != case_dir:
        raise PublicationError(
            "provisional Phase reference는 case directory 안에 있어야 합니다."
        )
    phase_catalog_path = _require_file(
        case_dir / "procedure_phases.ai_review.v1.yaml",
        label="Phase catalog",
    )
    voice_path = _require_file(
        case_dir / "voice_events.source.v2.jsonl",
        label="voice timeline",
    )
    masks_path = _require_file(
        (
            evaluation_masks_path.resolve()
            if evaluation_masks_path is not None
            else case_dir / "evaluation_masks.v1.json"
        ),
        label="evaluation masks",
    )
    if masks_path.parent != case_dir:
        raise PublicationError(
            "evaluation masks는 case directory 안에 있어야 합니다."
        )
    review_index_path = _require_file(
        case_dir / "policy02_review_index.v1.json",
        label="Policy02 review index",
    )
    reconciliation_path = _require_file(
        (
            reconciliation_path.resolve()
            if reconciliation_path is not None
            else case_dir / "policy02_reconciliation_audit.final.v1.json"
        ),
        label="Policy02 Codex reconciliation",
    )
    if reconciliation_path.parent != case_dir:
        raise PublicationError(
            "Policy02 reconciliation은 case directory 안에 있어야 합니다."
        )
    report_path = _require_file(report_path, label="projection report")
    information_boundary_report_path = _require_file(
        information_boundary_report_path,
        label="information boundary report",
    )

    point_schema_path = _require_file(
        schema_root / "observable_interaction_point.v1.schema.json",
        label="point schema",
    )
    interval_schema_path = _require_file(
        schema_root / "observable_interaction_interval.v1.schema.json",
        label="interval schema",
    )
    adjudication_schema_path = _require_file(
        schema_root / "assistant_interaction_adjudication.v1.schema.json",
        label="assistant adjudication schema",
    )
    projection_schema_path = _require_file(
        schema_root / "explicit_dt_interaction_projection.v1.schema.json",
        label="explicit projection schema",
    )
    voice_schema_path = _require_file(
        schema_root / "observable_voice_point.v2.schema.json",
        label="voice schema",
    )
    mask_schema_path = _require_file(
        schema_root / "evaluation_masks.v1.schema.json",
        label="evaluation mask schema",
    )
    legacy_schema_path = _require_file(
        schema_root / "observable_tool_event.v1.schema.json",
        label="legacy event schema",
    )
    tool_catalog_path = _require_file(
        catalog_root / "tools.yaml",
        label="tool catalog",
    )

    timeline = load_json(timeline_path)
    report = load_json(report_path)
    projection = load_json(projection_path)
    masks = load_json(masks_path)
    review_index = load_json(review_index_path)
    reconciliation = load_json(reconciliation_path)
    boundary_report = load_json(information_boundary_report_path)
    adjudications = load_jsonl(adjudications_path)
    observed = load_jsonl(observed_path)
    dt = load_jsonl(dt_path)
    phases = load_jsonl(phase_path)
    voice = load_jsonl(voice_path)

    for label, payload in (
        ("timeline", timeline),
        ("projection report", report),
        ("projection", projection),
        ("evaluation masks", masks),
        ("Policy02 review index", review_index),
        ("Policy02 reconciliation", reconciliation),
    ):
        if payload.get("case_id") != case_id:
            raise PublicationError(f"{label} case_id 불일치")
    for label, records in (
        ("adjudications", adjudications),
        ("observed", observed),
        ("DT", dt),
        ("Phase", phases),
        ("voice", voice),
    ):
        _validate_case_records(records, case_id=case_id, label=label)

    if (
        reconciliation.get("schema")
        != "taskplanner.policy02_reconciliation_audit.v1"
        or reconciliation.get("authority")
        != "user_authorized_ai_assistant_full_video_and_exact_frame_review"
    ):
        raise PublicationError("Policy02 reconciliation authority가 올바르지 않습니다.")
    coverage = reconciliation.get("coverage")
    review_counts = review_index.get("counts")
    if not isinstance(coverage, dict) or not isinstance(review_counts, dict):
        raise PublicationError("Policy02 review coverage가 없습니다.")
    if (
        coverage.get("coarse_review_complete") is not True
        or coverage.get("source_frame_first") != 0
        or coverage.get("source_frame_last") != timeline.get("frame_count") - 1
        or coverage.get("frame_count") != timeline.get("frame_count")
        or coverage.get("policy02_candidate_clusters_reviewed")
        != review_counts.get("candidate_cluster_count")
        or coverage.get("policy02_candidate_clusters_total")
        != review_counts.get("candidate_cluster_count")
        or coverage.get("voice_only_false_negative_windows_reviewed")
        != review_counts.get("voice_only_false_negative_window_count")
        or coverage.get("voice_only_false_negative_windows_total")
        != review_counts.get("voice_only_false_negative_window_count")
    ):
        raise PublicationError(
            "Policy02 reconciliation이 전체 frame/cluster/voice-FN을 "
            "검토하지 않았습니다."
        )
    continuity = reconciliation.get("physical_continuity")
    if (
        not isinstance(continuity, dict)
        or not str(continuity.get("result", "")).startswith("pass")
        or not str(continuity.get("teleportation_check", "")).strip()
    ):
        raise PublicationError("물리 연속성/teleportation 검수가 통과하지 않았습니다.")
    materialization = reconciliation.get("materialization")
    if not isinstance(materialization, dict):
        raise PublicationError("Policy02 materialization audit가 없습니다.")
    if (
        materialization.get("adjudication_file") != adjudications_path.name
        or materialization.get("adjudication_sha256")
        != sha256_file(adjudications_path)
        or materialization.get("projection_file") != projection_path.name
        or materialization.get("projection_sha256") != sha256_file(projection_path)
        or materialization.get("confirmed_observed_event_count") != len(observed)
        or materialization.get("ambiguous_candidate_count")
        != sum(
            item.get("review_status") == "ambiguous"
            for item in adjudications
        )
        or materialization.get("no_label_items_materialized_as_adjudications")
        is not False
    ):
        raise PublicationError("Policy02 materialization hash/count가 다릅니다.")

    if report.get("schema") != "taskplanner.dt_interaction_projection_report.v1":
        raise PublicationError("지원하지 않는 projection report schema입니다.")
    if report.get("information_boundary") != {
        "runtime_input_allowed": False,
        "vlm_input_allowed": False,
        "reducer_input_allowed": False,
        "bt_input_allowed": False,
    }:
        raise PublicationError("projection report information boundary가 닫히지 않았습니다.")
    _validated_output(
        report,
        output_key="observed",
        expected_path=observed_path,
        expected_records=observed,
    )
    _validated_output(
        report,
        output_key="dt_reference",
        expected_path=dt_path,
        expected_records=dt,
    )
    _validated_output(
        report,
        output_key="phase_reference",
        expected_path=phase_path,
        expected_records=phases,
    )
    if boundary_report.get("ok") is not True or boundary_report.get("violations"):
        raise PublicationError("information boundary scan이 clean하지 않습니다.")

    mask_schema = load_json(mask_schema_path)
    validate_evaluation_masks(
        masks=masks,
        mask_schema=mask_schema,
        case_id=case_id,
        observed=observed,
        phases=phases,
        voice=voice,
        timeline=timeline,
    )
    try:
        catalog = yaml.safe_load(phase_catalog_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise PublicationError(f"Phase catalog를 읽을 수 없습니다: {exc}") from exc
    if (
        not isinstance(catalog, dict)
        or catalog.get("runtime_status") != "evaluation_only_draft_not_frozen"
    ):
        raise PublicationError("Phase catalog runtime status가 올바르지 않습니다.")

    review_ids: set[str] = set()
    authorized_by: set[str] = set()
    for index, phase in enumerate(phases, 1):
        if (
            phase.get("event_type") != "phase_start"
            or phase.get("review_status") != "ambiguous"
            or phase.get("label_origin") != "assistant_video_adjudication"
        ):
            raise PublicationError(
                f"Phase:{index}: assistant provisional context가 아닙니다."
            )
        review = phase.get("review")
        if (
            not isinstance(review, dict)
            or review.get("reviewer_kind") != "ai_assistant"
            or not str(review.get("reviewer_id", "")).strip()
            or not str(review.get("authorized_by", "")).strip()
        ):
            raise PublicationError(f"Phase:{index}: provenance가 올바르지 않습니다.")
        review_ids.add(str(review["reviewer_id"]))
        authorized_by.add(str(review["authorized_by"]))
    if len(authorized_by) != 1:
        raise PublicationError("Phase authorized_by가 단일하지 않습니다.")

    evaluation_reference = copy.deepcopy(
        report.get("manifest_handoff", {}).get("evaluation_reference")
    )
    if not isinstance(evaluation_reference, dict):
        raise PublicationError("projection report manifest handoff가 없습니다.")
    evaluation_reference["projection_report_file"] = _relative(
        report_path,
        case_dir=case_dir,
    )
    evaluation_reference["projection_report_sha256"] = sha256_file(report_path)
    evaluation_reference["assistant_adjudication"] = {
        "file": adjudications_path.name,
        "sha256": sha256_file(adjudications_path),
        "schema_file": _relative(
            adjudication_schema_path,
            case_dir=case_dir,
        ),
        "schema_sha256": sha256_file(adjudication_schema_path),
        "review_status_counts": dict(
            sorted(Counter(item["review_status"] for item in adjudications).items())
        ),
    }
    evaluation_reference["projection_policy_file"] = projection_path.name
    evaluation_reference["projection_policy_sha256"] = sha256_file(projection_path)
    evaluation_reference["evaluation_masks"] = {
        "file": masks_path.name,
        "sha256": sha256_file(masks_path),
        "schema_file": _relative(mask_schema_path, case_dir=case_dir),
        "schema_sha256": sha256_file(mask_schema_path),
    }
    evaluation_reference["evaluation_scope"] = copy.deepcopy(
        masks["evaluation_scope"]
    )
    evaluation_reference["information_boundary_report_file"] = _relative(
        information_boundary_report_path,
        case_dir=case_dir,
    )
    evaluation_reference["information_boundary_report_sha256"] = sha256_file(
        information_boundary_report_path
    )
    phase_reference = evaluation_reference.get("phase_reference")
    if not isinstance(phase_reference, dict):
        raise PublicationError("evaluation reference Phase descriptor가 없습니다.")
    phase_reference["file"] = phase_path.name
    phase_reference["sha256"] = sha256_file(phase_path)
    phase_reference["event_count"] = len(phases)
    phase_reference["event_type_counts"] = dict(
        sorted(Counter(item["event_type"] for item in phases).items())
    )
    phase_reference["review_status_counts"] = dict(
        sorted(Counter(item["review_status"] for item in phases).items())
    )
    phase_reference["schema_file"] = _relative(
        point_schema_path,
        case_dir=case_dir,
    )
    phase_reference["schema_sha256"] = sha256_file(point_schema_path)

    bag_dir = Path(str(timeline.get("source_bag", ""))).resolve()
    if not bag_dir.is_dir():
        raise PublicationError(f"timeline source bag가 없습니다: {bag_dir}")
    source_bag, duration_sec = _source_bag_descriptor(bag_dir=bag_dir)

    adjudication_status = dict(
        sorted(Counter(item["review_status"] for item in adjudications).items())
    )
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "case_id": case_id,
        "duration_sec": duration_sec,
        "event_file": "tool_events.v1.jsonl",
        "candidate_file": "candidate_events.v1.jsonl",
        "event_schema": "taskplanner.observable_tool_event.v1",
        "schema_path": _relative(legacy_schema_path, case_dir=case_dir),
        "schema_sha256": sha256_file(legacy_schema_path),
        "review_status_counts": {
            "proposed": 0,
            "confirmed": 0,
            "ambiguous": 0,
            "rejected": 0,
        },
        "annotation_adjudication": {
            "authority": "legacy_tool_event_gate_intentionally_closed",
            "complete": False,
            "confirmed_event_count": 0,
            "confirmed_origin_counts": {},
            "confirmed_reviewer_kind_counts": {},
            "method": (
                "The legacy observable_tool_event.v1 injection gate remains "
                "closed. Final minimal interactions are evaluation-only."
            ),
        },
        "minimal_interaction_annotation": {
            "authority": (
                "user_authorized_ai_assistant_exact_frame_adjudication"
            ),
            "complete": True,
            "timeline_file": timeline_path.name,
            "timeline_sha256": sha256_file(timeline_path),
            "policy02_review_index_file": review_index_path.name,
            "policy02_review_index_sha256": sha256_file(review_index_path),
            "policy02_reconciliation_file": reconciliation_path.name,
            "policy02_reconciliation_sha256": sha256_file(reconciliation_path),
            "assistant_adjudication_file": adjudications_path.name,
            "assistant_adjudication_sha256": sha256_file(adjudications_path),
            "assistant_adjudication_schema_path": _relative(
                adjudication_schema_path,
                case_dir=case_dir,
            ),
            "assistant_adjudication_schema_sha256": sha256_file(
                adjudication_schema_path
            ),
            "explicit_projection_file": projection_path.name,
            "explicit_projection_sha256": sha256_file(projection_path),
            "explicit_projection_schema_path": _relative(
                projection_schema_path,
                case_dir=case_dir,
            ),
            "explicit_projection_schema_sha256": sha256_file(
                projection_schema_path
            ),
            "final_observed_reference_file": observed_path.name,
            "final_observed_reference_sha256": sha256_file(observed_path),
            "final_dt_reference_file": dt_path.name,
            "final_dt_reference_sha256": sha256_file(dt_path),
            "provisional_phase_reference_file": phase_path.name,
            "provisional_phase_reference_sha256": sha256_file(phase_path),
            "adjudication_review_status_counts": adjudication_status,
            "event_schema": "taskplanner.observable_interaction_point.v1",
            "event_schema_path": _relative(point_schema_path, case_dir=case_dir),
            "event_schema_sha256": sha256_file(point_schema_path),
            "interval_schema": "taskplanner.observable_interaction_interval.v1",
            "interval_schema_path": _relative(
                interval_schema_path,
                case_dir=case_dir,
            ),
            "interval_schema_sha256": sha256_file(interval_schema_path),
            "visual_coverage": {
                "start_sec": timeline["start_sec"],
                "end_sec": timeline["end_sec"],
                "frame_count": timeline["frame_count"],
                "gaps": timeline["gaps"],
            },
        },
        "evaluation_reference": evaluation_reference,
        "speech_timeline": {
            "authority": (
                "source_bag_public_transcript_not_evaluation_ground_truth"
            ),
            "event_count": len(voice),
            "file": voice_path.name,
            "sha256": sha256_file(voice_path),
            "schema_file": _relative(voice_schema_path, case_dir=case_dir),
            "schema_sha256": sha256_file(voice_schema_path),
            "source_topic": "/surgery/transcript",
            "timeline_geometry": "point_at_source_timestamp",
            "availability_field": "available_sec",
            "availability_policy": "not_before_utterance_end",
            "scoring_role": "context_only_not_ground_truth",
        },
        "phase_annotation": {
            "authority": ASSISTANT_PHASE_AUTHORITY,
            "complete": True,
            "review_complete": True,
            "scoring_reference_ready": False,
            "reference_included_in_final_layers": True,
            "event_count": len(phases),
            "review_status_counts": {
                status: sum(
                    phase.get("review_status") == status for phase in phases
                )
                for status in ("confirmed", "ambiguous", "rejected")
            },
            "review_authority": {
                "reviewer_kind": "ai_assistant",
                "reviewer_ids": sorted(review_ids),
                "authorized_by": next(iter(authorized_by)),
            },
            "procedure_catalog_file": phase_catalog_path.name,
            "procedure_catalog_sha256": sha256_file(phase_catalog_path),
            "procedure_catalog_runtime_status": (
                "evaluation_only_draft_not_frozen"
            ),
            "provisional_reference_file": phase_path.name,
            "provisional_reference_sha256": sha256_file(phase_path),
        },
        "source_bag": source_bag,
        "tool_catalog_path": _relative(tool_catalog_path, case_dir=case_dir),
        "tool_catalog_sha256": sha256_file(tool_catalog_path),
        "shadow_replay": {
            "authority": "procedure_default_only_no_case_annotation_bootstrap",
            "start_phase_id": "",
        },
        "policy02": {
            "authority": "proposal_only_not_ground_truth",
            "review_index_file": review_index_path.name,
            "review_index_sha256": sha256_file(review_index_path),
            "query_policy_id": review_index.get("query_policy_id"),
            "policy_version": review_index.get("policy_version"),
            "model": review_index.get("source_validation", {})
            .get("scan_run", {})
            .get("model"),
            "cross_pass_identity_valid": review_index.get(
                "source_validation",
                {},
            ).get("cross_pass_identity_valid"),
        },
        "notes": [
            (
                "Interaction labels are exact-frame, user-authorized "
                "assistant adjudications; Marlin outputs are proposal-only."
            ),
            (
                "Phase is provisional ambiguous context and contributes "
                "nothing to phase or interaction scoring."
            ),
            (
                "Ground truth, DT projection, Phase, and masks are "
                "evaluation-only and never VLM/reducer/BT runtime input."
            ),
            (
                "This case is development/calibration, not held-out, because "
                "the procedure ontology and annotation policy are being "
                "optimized across 0704_6 through 0704_17."
            ),
        ],
    }
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case_dir", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--information-boundary-report", type=Path)
    parser.add_argument(
        "--phase-reference",
        type=Path,
        help=(
            "Validated provisional Phase JSONL. Defaults to "
            "phase_events.provisional.final.v1.jsonl."
        ),
    )
    parser.add_argument(
        "--evaluation-masks",
        type=Path,
        help=(
            "Validated evaluation mask JSON. Defaults to "
            "evaluation_masks.v1.json."
        ),
    )
    parser.add_argument(
        "--adjudications",
        type=Path,
        help=(
            "Versioned assistant adjudication JSONL. Defaults to "
            "assistant_interaction_adjudications.final.v1.jsonl."
        ),
    )
    parser.add_argument(
        "--projection",
        type=Path,
        help=(
            "Versioned explicit DT projection JSON. Defaults to "
            "dt_projection.explicit.v1.json."
        ),
    )
    parser.add_argument(
        "--observed-reference",
        type=Path,
        help=(
            "Versioned finalized observed JSONL. Defaults to "
            "interaction_events.observed.final.v1.jsonl."
        ),
    )
    parser.add_argument(
        "--dt-reference",
        type=Path,
        help=(
            "Versioned finalized DT JSONL. Defaults to "
            "interaction_events.dt_reference.final.v1.jsonl."
        ),
    )
    parser.add_argument(
        "--reconciliation",
        type=Path,
        help=(
            "Versioned Policy02 reconciliation JSON. Defaults to "
            "policy02_reconciliation_audit.final.v1.json."
        ),
    )
    parser.add_argument("--output", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    case_dir = args.case_dir.resolve()
    annotation_root = case_dir.parents[1]
    report_path = (
        args.report.resolve()
        if args.report
        else annotation_root
        / f"reports/{case_dir.name}_assistant_dt_projection.final.v1.json"
    )
    boundary_path = (
        args.information_boundary_report.resolve()
        if args.information_boundary_report
        else annotation_root
        / "reports/information_boundary.final.v2.json"
    )
    output_path = (
        args.output.resolve()
        if args.output
        else case_dir / "annotation_manifest.json"
    )
    try:
        manifest = build_manifest(
            case_dir=case_dir,
            report_path=report_path,
            information_boundary_report_path=boundary_path,
            phase_reference_path=(
                args.phase_reference.resolve()
                if args.phase_reference
                else None
            ),
            evaluation_masks_path=(
                args.evaluation_masks.resolve()
                if args.evaluation_masks
                else None
            ),
            adjudications_path=(
                args.adjudications.resolve()
                if args.adjudications
                else None
            ),
            projection_path=(
                args.projection.resolve()
                if args.projection
                else None
            ),
            observed_reference_path=(
                args.observed_reference.resolve()
                if args.observed_reference
                else None
            ),
            dt_reference_path=(
                args.dt_reference.resolve()
                if args.dt_reference
                else None
            ),
            reconciliation_path=(
                args.reconciliation.resolve()
                if args.reconciliation
                else None
            ),
        )
        payloads: dict[Path, bytes] = {output_path: encode_json(manifest)}
        for legacy_name in ("tool_events.v1.jsonl", "candidate_events.v1.jsonl"):
            legacy_path = case_dir / legacy_name
            if legacy_path.exists():
                if legacy_path.read_bytes():
                    raise PublicationError(
                        f"legacy closed-gate 파일이 비어 있지 않습니다: {legacy_path}"
                    )
            else:
                payloads[legacy_path] = b""
        publish_create_only(payloads)
    except (PublicationError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "case_id": case_dir.name,
                "manifest": str(output_path),
                "manifest_sha256": sha256_file(output_path),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
