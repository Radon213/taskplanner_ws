#!/usr/bin/env python3
"""Build a proposal-only review index from the two policy02 Marlin passes.

The index is a lossless navigation aid for later visual adjudication.  It
clusters only by corrected bag time, preserves every model proposal and raw
query response, and never creates a confirmed event, tool identity, transfer
direction, or request boundary.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Any

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from tools.real_surgery_annotation.run_marlin2_policy02_batch import (
    PASS_SPECS,
    POLICY_VERSION,
)
from tools.real_surgery_annotation.run_marlin2_proposals import (
    MODEL_QUERIES,
    MODEL_QUERY_POLICY_ID,
    atomic_create_text,
    canonical_json_sha256,
    nearest_frame_index_in_range,
    observability_segments,
    sha256_file,
)


PASS_SPEC_BY_NAME = {item.name: item for item in PASS_SPECS}


def reject_nonstandard_json(token: str) -> None:
    raise ValueError(f"non-standard JSON numeric token: {token}")


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(
        path.read_text(encoding="utf-8"),
        parse_constant=reject_nonstandard_json,
    )
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        1,
    ):
        if not line.strip():
            continue
        value = json.loads(line, parse_constant=reject_nonstandard_json)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number}: expected a JSON object")
        value["_source_line"] = line_number
        records.append(value)
    if not records:
        raise ValueError(f"{path}: expected at least one JSONL record")
    return records


def finite_number(value: Any, location: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ValueError(f"{location}: expected a finite number")
    return float(value)


def validate_timeline(
    timeline: dict[str, Any],
    *,
    case_id: str,
) -> tuple[list[float], float, list[dict[str, Any]], list[str]]:
    if timeline.get("case_id") != case_id:
        raise ValueError("timeline case_id mismatch")
    timestamps = [
        finite_number(value, "timeline.timestamps_sec")
        for value in timeline.get("timestamps_sec", [])
    ]
    source_fps = finite_number(timeline.get("source_fps"), "timeline.source_fps")
    if (
        not timestamps
        or source_fps <= 0
        or any(right <= left for left, right in zip(timestamps, timestamps[1:]))
    ):
        raise ValueError("timeline timestamps/fps are invalid")
    if int(timeline.get("frame_count", -1)) != len(timestamps):
        raise ValueError("timeline frame_count mismatch")
    segments, frame_segments = observability_segments(
        timestamps,
        source_fps=source_fps,
    )
    declared = [
        (int(item["before_frame_idx"]), int(item["after_frame_idx"]))
        for item in timeline.get("gaps", [])
    ]
    detected = [
        (
            int(segments[index]["last_frame_idx"]),
            int(segments[index + 1]["first_frame_idx"]),
        )
        for index in range(len(segments) - 1)
    ]
    if declared != detected:
        raise ValueError(
            f"timeline declared gaps do not match timestamps: {declared} != {detected}"
        )
    return timestamps, source_fps, segments, frame_segments


def validate_child_run(
    *,
    case_id: str,
    pass_name: str,
    proposal_path: Path,
    report_path: Path,
    timeline_path: Path,
) -> dict[str, Any]:
    spec = PASS_SPEC_BY_NAME[pass_name]
    report = load_json(report_path)
    errors: list[str] = []
    if report.get("schema") != "taskplanner.marlin2_proposal_run.v1":
        errors.append("unexpected child report schema")
    if report.get("status") != "completed":
        errors.append("child report is not completed")
    if report.get("authority") != "proposal_only_not_ground_truth":
        errors.append("child authority is not proposal-only")
    if report.get("case_id") != case_id:
        errors.append("child case_id mismatch")
    if report.get("output") != str(proposal_path.resolve()):
        errors.append("child output path mismatch")
    proposal_sha256 = sha256_file(proposal_path)
    if report.get("output_sha256") != proposal_sha256:
        errors.append("child output SHA-256 mismatch")

    inputs = report.get("inputs", {})
    timeline_sha256 = sha256_file(timeline_path)
    if inputs.get("timeline") != str(timeline_path.resolve()):
        errors.append("child timeline path mismatch")
    if inputs.get("timeline_sha256") != timeline_sha256:
        errors.append("child timeline SHA-256 mismatch")
    for field in ("video_sha256", "anchors_sha256"):
        value = inputs.get(field)
        if not isinstance(value, str) or len(value) != 64:
            errors.append(f"child {field} is missing")

    model = report.get("model", {})
    for field in ("id", "revision", "local_path"):
        if not isinstance(model.get(field), str) or not model[field]:
            errors.append(f"child model.{field} is missing")

    expected_queries = {
        event_type: MODEL_QUERIES[event_type]
        for event_type in spec.event_types
    }
    settings = report.get("settings", {})
    if settings.get("query_policy_id") != MODEL_QUERY_POLICY_ID:
        errors.append("child query policy mismatch")
    if settings.get("event_types") != list(spec.event_types):
        errors.append("child event types mismatch")
    if settings.get("queries") != expected_queries:
        errors.append("child query text mismatch")
    expected_prompt_sha256 = canonical_json_sha256(expected_queries)
    if settings.get("query_prompt_sha256") != expected_prompt_sha256:
        errors.append("child prompt SHA-256 mismatch")
    if settings.get("skip_caption") is not True:
        errors.append("child pass unexpectedly generated captions")
    if finite_number(
        settings.get("clip_before_sec"),
        f"{pass_name}.clip_before_sec",
    ) != spec.clip_before_sec:
        errors.append("child clip_before_sec mismatch")
    if finite_number(
        settings.get("clip_after_sec"),
        f"{pass_name}.clip_after_sec",
    ) != spec.clip_after_sec:
        errors.append("child clip_after_sec mismatch")
    if errors:
        raise ValueError(f"{pass_name} child validation failed: {'; '.join(errors)}")
    return {
        "pass": pass_name,
        "proposal_file": str(proposal_path.resolve()),
        "proposal_file_sha256": proposal_sha256,
        "report_file": str(report_path.resolve()),
        "report_file_sha256": sha256_file(report_path),
        "model": model,
        "query_policy_id": settings["query_policy_id"],
        "query_prompt_sha256": expected_prompt_sha256,
        "video_sha256": inputs["video_sha256"],
        "timeline_sha256": timeline_sha256,
        "anchor_file_sha256": inputs["anchors_sha256"],
        "settings": {
            "event_types": list(spec.event_types),
            "clip_before_sec": spec.clip_before_sec,
            "clip_after_sec": spec.clip_after_sec,
            "skip_caption": True,
        },
        "reported_counts": report.get("counts", {}),
    }


def validate_cross_pass_identity(
    transcript: dict[str, Any],
    scan: dict[str, Any],
) -> None:
    errors: list[str] = []
    if transcript["model"] != scan["model"]:
        errors.append("model identity differs between passes")
    if transcript["video_sha256"] != scan["video_sha256"]:
        errors.append("video SHA-256 differs between passes")
    if transcript["timeline_sha256"] != scan["timeline_sha256"]:
        errors.append("timeline SHA-256 differs between passes")
    if transcript["query_policy_id"] != scan["query_policy_id"]:
        errors.append("query policy differs between passes")
    if errors:
        raise ValueError("; ".join(errors))


def validate_voice_records(
    records: list[dict[str, Any]],
    *,
    case_id: str,
) -> list[dict[str, Any]]:
    seen_ids: set[str] = set()
    validated: list[dict[str, Any]] = []
    for record in records:
        line = int(record["_source_line"])
        if record.get("schema") != "taskplanner.observable_voice_point.v2":
            raise ValueError(f"voice:{line}: unexpected schema")
        if record.get("case_id") != case_id:
            raise ValueError(f"voice:{line}: case_id mismatch")
        event_id = record.get("event_id")
        if not isinstance(event_id, str) or not event_id or event_id in seen_ids:
            raise ValueError(f"voice:{line}: invalid or duplicate event_id")
        seen_ids.add(event_id)
        start_sec = finite_number(record.get("time_sec"), f"voice:{line}.time_sec")
        end_sec = finite_number(record.get("end_sec"), f"voice:{line}.end_sec")
        available_sec = finite_number(
            record.get("available_sec"),
            f"voice:{line}.available_sec",
        )
        if end_sec < start_sec or available_sec < end_sec:
            raise ValueError(f"voice:{line}: non-causal time ordering")
        clean = {key: value for key, value in record.items() if key != "_source_line"}
        validated.append(
            {
                "event": clean,
                "source_line": line,
                "time_sec": start_sec,
                "end_sec": end_sec,
                "available_sec": available_sec,
            }
        )
    return validated


def validate_candidate_time(
    candidate: dict[str, Any],
    *,
    timestamps: list[float],
    frame_segments: list[str],
    location: str,
) -> tuple[float, str]:
    time_record = candidate.get("time")
    if not isinstance(time_record, dict):
        raise ValueError(f"{location}: missing candidate time")
    bag_time_sec = finite_number(
        time_record.get("bag_time_sec"),
        f"{location}.time.bag_time_sec",
    )
    frame_idx = int(time_record.get("source_frame_idx", -1))
    if frame_idx < 0 or frame_idx >= len(timestamps):
        raise ValueError(f"{location}: source frame index out of range")
    if abs(timestamps[frame_idx] - bag_time_sec) > 5e-10:
        raise ValueError(f"{location}: corrected bag time/frame mismatch")
    segment_id = str(time_record.get("observability_segment_id", ""))
    if segment_id != frame_segments[frame_idx]:
        raise ValueError(f"{location}: observability segment mismatch")
    return bag_time_sec, segment_id


def parse_proposal_records(
    *,
    case_id: str,
    pass_name: str,
    proposal_path: Path,
    proposal_sha256: str,
    report_path: Path,
    report_sha256: str,
    records: list[dict[str, Any]],
    timestamps: list[float],
    frame_segments: list[str],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, int],
]:
    spec = PASS_SPEC_BY_NAME[pass_name]
    candidates: list[dict[str, Any]] = []
    raw_queries: list[dict[str, Any]] = []
    clip_intervals: list[dict[str, Any]] = []
    skipped_gap_count = 0
    for record in records:
        line = int(record["_source_line"])
        location = f"{pass_name}:{line}"
        if record.get("schema") != "taskplanner.marlin2_anchor_evidence.v1":
            raise ValueError(f"{location}: unexpected schema")
        if record.get("case_id") != case_id:
            raise ValueError(f"{location}: case_id mismatch")
        anchor = record.get("anchor")
        if not isinstance(anchor, dict) or not isinstance(anchor.get("anchor_id"), str):
            raise ValueError(f"{location}: missing anchor identity")
        anchor_id = anchor["anchor_id"]
        status = record.get("processing_status")
        if status not in {
            "completed",
            "skipped_anchor_inside_observability_gap",
        }:
            raise ValueError(f"{location}: invalid processing_status")
        if status == "skipped_anchor_inside_observability_gap":
            skipped_gap_count += 1

        query_refs_by_type: dict[str, list[str]] = {}
        find_results = record.get("find_results", [])
        if not isinstance(find_results, list):
            raise ValueError(f"{location}: find_results must be an array")
        actual_query_pairs: list[tuple[str, str]] = []
        for query_index, query in enumerate(find_results, 1):
            if not isinstance(query, dict):
                raise ValueError(f"{location}: raw query must be an object")
            event_type = query.get("event_type")
            if not isinstance(event_type, str):
                raise ValueError(f"{location}: raw query event type missing")
            query_text = query.get("query")
            if not isinstance(query_text, str):
                raise ValueError(f"{location}: raw query text missing")
            actual_query_pairs.append((event_type, query_text))
            ref_id = (
                f"{case_id}:{pass_name}:L{line:04d}:Q{query_index:02d}"
            )
            query_refs_by_type.setdefault(event_type, []).append(ref_id)
            raw_queries.append(
                {
                    "raw_query_ref_id": ref_id,
                    "source": {
                        "pass": pass_name,
                        "proposal_file": str(proposal_path.resolve()),
                        "proposal_file_sha256": proposal_sha256,
                        "proposal_line": line,
                        "report_file": str(report_path.resolve()),
                        "report_file_sha256": report_sha256,
                        "anchor_id": anchor_id,
                        "query_index": query_index,
                    },
                    "model_query_evidence": query,
                }
            )

        expected_query_pairs = [
            (event_type, query_text)
            for event_type in spec.event_types
            for query_text in MODEL_QUERIES[event_type]
        ]
        if status == "completed" and actual_query_pairs != expected_query_pairs:
            raise ValueError(f"{location}: raw query sequence/prompt mismatch")
        if (
            status == "skipped_anchor_inside_observability_gap"
            and actual_query_pairs
        ):
            raise ValueError(f"{location}: skipped gap anchor contains raw queries")

        consensus_candidates = record.get("consensus_candidates", [])
        if not isinstance(consensus_candidates, list):
            raise ValueError(
                f"{location}: consensus_candidates must be an array"
            )
        if (
            status == "skipped_anchor_inside_observability_gap"
            and consensus_candidates
        ):
            raise ValueError(
                f"{location}: skipped gap anchor contains candidates"
            )
        for candidate_index, candidate in enumerate(consensus_candidates, 1):
            if not isinstance(candidate, dict):
                raise ValueError(f"{location}: consensus candidate must be an object")
            event_type = candidate.get("event_type")
            if not isinstance(event_type, str):
                raise ValueError(f"{location}: candidate event type missing")
            if event_type not in spec.event_types:
                raise ValueError(f"{location}: candidate event type not in pass")
            raw_ref_ids = query_refs_by_type.get(event_type, [])
            if not raw_ref_ids:
                raise ValueError(
                    f"{location}: candidate has no matching raw query evidence"
                )
            bag_time_sec, segment_id = validate_candidate_time(
                candidate,
                timestamps=timestamps,
                frame_segments=frame_segments,
                location=f"{location}:candidate:{candidate_index}",
            )
            candidates.append(
                {
                    "candidate_ref_id": (
                        f"{case_id}:{pass_name}:L{line:04d}:"
                        f"C{candidate_index:02d}"
                    ),
                    "corrected_bag_time_sec": bag_time_sec,
                    "observability_segment_id": segment_id,
                    "source": {
                        "pass": pass_name,
                        "proposal_file": str(proposal_path.resolve()),
                        "proposal_file_sha256": proposal_sha256,
                        "proposal_line": line,
                        "report_file": str(report_path.resolve()),
                        "report_file_sha256": report_sha256,
                        "anchor_id": anchor_id,
                        "candidate_index": candidate_index,
                        "model_proposed_event_type": event_type,
                        "raw_query_ref_ids": raw_ref_ids,
                    },
                    "model_consensus_candidate": candidate,
                }
            )

        clip = record.get("clip")
        if status == "completed" and isinstance(clip, dict):
            start = clip.get("start", {})
            end = clip.get("end", {})
            clip_intervals.append(
                {
                    "pass": pass_name,
                    "proposal_line": line,
                    "anchor_id": anchor_id,
                    "observability_segment_id": str(
                        clip.get("observability_segment_id", "")
                    ),
                    "start_sec": finite_number(
                        start.get("bag_time_sec"),
                        f"{location}.clip.start",
                    ),
                    "end_sec": finite_number(
                        end.get("bag_time_sec"),
                        f"{location}.clip.end",
                    ),
                }
            )
        elif status == "completed":
            raise ValueError(f"{location}: completed anchor is missing clip")
        elif clip is not None:
            raise ValueError(f"{location}: skipped gap anchor contains a clip")
    return (
        candidates,
        raw_queries,
        clip_intervals,
        {
            "record_count": len(records),
            "skipped_anchor_inside_gap_count": skipped_gap_count,
        },
    )


def merge_intervals(
    intervals: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    merged: list[list[float]] = []
    for start, end in sorted(intervals):
        if end < start:
            raise ValueError("interval end precedes start")
        if not merged or start > merged[-1][1] + 1e-9:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(item[0], item[1]) for item in merged]


def interval_duration(intervals: list[tuple[float, float]]) -> float:
    return sum(max(0.0, end - start) for start, end in intervals)


def segment_window(
    *,
    segment: dict[str, Any],
    requested_start_sec: float,
    requested_end_sec: float,
    timestamps: list[float],
    source_fps: float,
) -> dict[str, Any] | None:
    start_sec = max(requested_start_sec, float(segment["start_bag_time_sec"]))
    end_sec = min(requested_end_sec, float(segment["end_bag_time_sec"]))
    if end_sec < start_sec:
        return None
    first_frame_idx = int(segment["first_frame_idx"])
    last_frame_idx = int(segment["last_frame_idx"])
    start_frame_idx = nearest_frame_index_in_range(
        timestamps,
        start_sec,
        first_frame_idx=first_frame_idx,
        last_frame_idx=last_frame_idx,
    )
    end_frame_idx = nearest_frame_index_in_range(
        timestamps,
        end_sec,
        first_frame_idx=first_frame_idx,
        last_frame_idx=last_frame_idx,
    )
    return {
        "observability_segment_id": segment["id"],
        "start": {
            "bag_time_sec": timestamps[start_frame_idx],
            "source_frame_idx": start_frame_idx,
            "source_time_sec": round(start_frame_idx / source_fps, 9),
        },
        "end": {
            "bag_time_sec": timestamps[end_frame_idx],
            "source_frame_idx": end_frame_idx,
            "source_time_sec": round(end_frame_idx / source_fps, 9),
        },
    }


def gap_intersections(
    *,
    requested_start_sec: float,
    requested_end_sec: float,
    gaps: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    intersections: list[dict[str, Any]] = []
    for gap_index, gap in enumerate(gaps, 1):
        gap_start = float(gap["before_time_sec"])
        gap_end = float(gap["after_time_sec"])
        start = max(requested_start_sec, gap_start)
        end = min(requested_end_sec, gap_end)
        if end > start:
            intersections.append(
                {
                    "gap_id": f"gap_{gap_index:04d}",
                    "start_sec": start,
                    "end_sec": end,
                    "duration_sec": round(end - start, 9),
                }
            )
    return intersections


def make_review_window(
    *,
    requested_start_sec: float,
    requested_end_sec: float,
    timestamps: list[float],
    source_fps: float,
    segments: list[dict[str, Any]],
    gaps: list[dict[str, Any]],
    only_segment_id: str | None = None,
) -> dict[str, Any]:
    original_start_sec = requested_start_sec
    original_end_sec = requested_end_sec
    clipped_start_sec = max(timestamps[0], requested_start_sec)
    clipped_end_sec = min(timestamps[-1], requested_end_sec)
    outside_timeline = clipped_end_sec < clipped_start_sec
    if outside_timeline:
        clipped_start_sec = None
        clipped_end_sec = None
    eligible_segments = [
        segment
        for segment in segments
        if only_segment_id is None or segment["id"] == only_segment_id
    ]
    if outside_timeline:
        observable: list[dict[str, Any]] = []
        gaps_in_window: list[dict[str, Any]] = []
    else:
        assert clipped_start_sec is not None
        assert clipped_end_sec is not None
        observable = [
            window
            for segment in eligible_segments
            if (
                window := segment_window(
                    segment=segment,
                    requested_start_sec=clipped_start_sec,
                    requested_end_sec=clipped_end_sec,
                    timestamps=timestamps,
                    source_fps=source_fps,
                )
            )
            is not None
        ]
        gaps_in_window = gap_intersections(
            requested_start_sec=clipped_start_sec,
            requested_end_sec=clipped_end_sec,
            gaps=gaps,
        )
    return {
        "clock": "corrected_bag_time",
        "requested_start_sec": round(original_start_sec, 9),
        "requested_end_sec": round(original_end_sec, 9),
        "timeline_clipped_start_sec": (
            round(clipped_start_sec, 9)
            if clipped_start_sec is not None
            else None
        ),
        "timeline_clipped_end_sec": (
            round(clipped_end_sec, 9)
            if clipped_end_sec is not None
            else None
        ),
        "outside_video_timeline": outside_timeline,
        "observable_segments": observable,
        "gap_intersections": gaps_in_window,
        "evaluation_possible": bool(observable),
        "fully_observable": bool(observable) and not gaps_in_window,
        "no_inference_across_gap": True,
    }


def cluster_candidates(
    candidates: list[dict[str, Any]],
    *,
    threshold_sec: float,
) -> list[list[dict[str, Any]]]:
    ordered = sorted(
        candidates,
        key=lambda item: (
            item["corrected_bag_time_sec"],
            item["candidate_ref_id"],
        ),
    )
    clusters: list[list[dict[str, Any]]] = []
    for candidate in ordered:
        if not clusters:
            clusters.append([candidate])
            continue
        current = clusters[-1]
        same_segment = (
            current[0]["observability_segment_id"]
            == candidate["observability_segment_id"]
        )
        bounded_span = (
            candidate["corrected_bag_time_sec"]
            - current[0]["corrected_bag_time_sec"]
            <= threshold_sec + 1e-12
        )
        if same_segment and bounded_span:
            current.append(candidate)
        else:
            clusters.append([candidate])
    return clusters


def causal_voice_for_cluster(
    voice_records: list[dict[str, Any]],
    *,
    candidates: list[dict[str, Any]],
    lookback_sec: float,
) -> list[dict[str, Any]]:
    candidate_times = [
        (
            candidate["candidate_ref_id"],
            candidate["corrected_bag_time_sec"],
        )
        for candidate in candidates
    ]
    latest_candidate_sec = max(time_sec for _ref_id, time_sec in candidate_times)
    nearby: list[dict[str, Any]] = []
    for voice in voice_records:
        available_sec = voice["available_sec"]
        causal_candidate_ref_ids = [
            ref_id
            for ref_id, time_sec in candidate_times
            if available_sec <= time_sec + 1e-12
        ]
        if causal_candidate_ref_ids and (
            latest_candidate_sec - available_sec
            <= lookback_sec + 1e-12
        ):
            nearby.append(
                {
                    "voice_event": voice["event"],
                    "source_line": voice["source_line"],
                    "causal_availability": (
                        "all_cluster_candidates"
                        if len(causal_candidate_ref_ids)
                        == len(candidate_times)
                        else "some_cluster_candidates"
                    ),
                    "causally_available_for_candidate_ref_ids": (
                        causal_candidate_ref_ids
                    ),
                    "available_to_latest_candidate_delta_sec": round(
                        latest_candidate_sec - available_sec,
                        9,
                    ),
                }
            )
    return nearby


def build_candidate_clusters(
    *,
    case_id: str,
    candidates: list[dict[str, Any]],
    voice_records: list[dict[str, Any]],
    cluster_threshold_sec: float,
    review_pad_before_sec: float,
    review_pad_after_sec: float,
    voice_lookback_sec: float,
    timestamps: list[float],
    source_fps: float,
    segments: list[dict[str, Any]],
    gaps: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], set[str]]:
    clusters: list[dict[str, Any]] = []
    voice_ids_with_candidate: set[str] = set()
    for cluster_index, grouped in enumerate(
        cluster_candidates(candidates, threshold_sec=cluster_threshold_sec),
        1,
    ):
        times = [item["corrected_bag_time_sec"] for item in grouped]
        center_sec = statistics.median(times)
        nearby_voice = causal_voice_for_cluster(
            voice_records,
            candidates=grouped,
            lookback_sec=voice_lookback_sec,
        )
        voice_ids_with_candidate.update(
            item["voice_event"]["event_id"] for item in nearby_voice
        )
        segment_id = grouped[0]["observability_segment_id"]
        clusters.append(
            {
                "review_item_id": f"{case_id}-PC{cluster_index:04d}",
                "review_item_type": "event_agnostic_proposal_cluster",
                "authority": "proposal_index_only_not_ground_truth",
                "cluster_clock": "corrected_bag_time",
                "cluster_time_sec": round(center_sec, 9),
                "cluster_min_time_sec": round(min(times), 9),
                "cluster_max_time_sec": round(max(times), 9),
                "observability_segment_id": segment_id,
                "source_proposals": grouped,
                "nearby_causally_available_voice": nearby_voice,
                "review_window": make_review_window(
                    requested_start_sec=(
                        min(times) - review_pad_before_sec
                    ),
                    requested_end_sec=max(times) + review_pad_after_sec,
                    timestamps=timestamps,
                    source_fps=source_fps,
                    segments=segments,
                    gaps=gaps,
                    only_segment_id=segment_id,
                ),
                "adjudication": None,
            }
        )
    return clusters, voice_ids_with_candidate


def build_voice_only_windows(
    *,
    case_id: str,
    voice_records: list[dict[str, Any]],
    voice_ids_with_candidate: set[str],
    before_sec: float,
    after_sec: float,
    timestamps: list[float],
    source_fps: float,
    segments: list[dict[str, Any]],
    gaps: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    for voice in voice_records:
        event = voice["event"]
        if event["event_id"] in voice_ids_with_candidate:
            continue
        windows.append(
            {
                "review_item_id": (
                    f"{case_id}-VF{len(windows) + 1:04d}"
                ),
                "review_item_type": "voice_only_false_negative_search",
                "authority": "proposal_index_only_not_ground_truth",
                "voice_event": event,
                "source_line": voice["source_line"],
                "window_anchor_policy": {
                    "start_from": "utterance_start",
                    "end_from": "causal_available_sec",
                },
                "review_window": make_review_window(
                    requested_start_sec=voice["time_sec"] - before_sec,
                    requested_end_sec=voice["available_sec"] + after_sec,
                    timestamps=timestamps,
                    source_fps=source_fps,
                    segments=segments,
                    gaps=gaps,
                ),
                "no_marlin_candidate_linked": True,
                "adjudication": None,
            }
        )
    return windows


def subtract_intervals(
    container: tuple[float, float],
    covered: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    cursor = container[0]
    result: list[tuple[float, float]] = []
    for start, end in covered:
        start = max(start, container[0])
        end = min(end, container[1])
        if end < container[0] or start > container[1]:
            continue
        if start > cursor + 1e-9:
            result.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < container[1] - 1e-9:
        result.append((cursor, container[1]))
    return result


def calculate_scan_coverage(
    *,
    scan_clip_intervals: list[dict[str, Any]],
    scan_counts: dict[str, int],
    segments: list[dict[str, Any]],
    gaps: list[dict[str, Any]],
    timeline_start_sec: float,
    timeline_end_sec: float,
) -> dict[str, Any]:
    segments_by_id = {str(item["id"]): item for item in segments}
    intervals_by_segment: dict[str, list[tuple[float, float]]] = {
        segment_id: [] for segment_id in segments_by_id
    }
    for clip in scan_clip_intervals:
        segment_id = clip["observability_segment_id"]
        if segment_id not in segments_by_id:
            raise ValueError("scan clip references unknown observability segment")
        segment = segments_by_id[segment_id]
        start = clip["start_sec"]
        end = clip["end_sec"]
        if (
            end < start
            or start < float(segment["start_bag_time_sec"]) - 1e-9
            or end > float(segment["end_bag_time_sec"]) + 1e-9
        ):
            raise ValueError("scan clip crosses an observability boundary")
        intervals_by_segment[segment_id].append((start, end))

    coverage_segments: list[dict[str, Any]] = []
    observable_duration_sec = 0.0
    covered_observable_sec = 0.0
    for segment in segments:
        segment_id = str(segment["id"])
        container = (
            float(segment["start_bag_time_sec"]),
            float(segment["end_bag_time_sec"]),
        )
        merged = merge_intervals(intervals_by_segment[segment_id])
        duration = max(0.0, container[1] - container[0])
        covered = interval_duration(merged)
        observable_duration_sec += duration
        covered_observable_sec += covered
        coverage_segments.append(
            {
                "observability_segment_id": segment_id,
                "segment_start_sec": container[0],
                "segment_end_sec": container[1],
                "observable_duration_sec": round(duration, 9),
                "covered_intervals": [
                    {"start_sec": start, "end_sec": end}
                    for start, end in merged
                ],
                "uncovered_intervals": [
                    {"start_sec": start, "end_sec": end}
                    for start, end in subtract_intervals(container, merged)
                ],
                "covered_sec": round(covered, 9),
                "coverage_ratio": (
                    round(covered / duration, 9)
                    if duration > 0
                    else 1.0
                ),
            }
        )

    gap_records = [
        {
            "gap_id": f"gap_{index:04d}",
            "start_sec": float(gap["before_time_sec"]),
            "end_sec": float(gap["after_time_sec"]),
            "duration_sec": round(
                float(gap["after_time_sec"])
                - float(gap["before_time_sec"]),
                9,
            ),
            "scan_coverage_sec": 0.0,
            "evaluation_possible": False,
        }
        for index, gap in enumerate(gaps, 1)
    ]
    wall_clock_duration_sec = max(
        0.0,
        timeline_end_sec - timeline_start_sec,
    )
    gap_duration_sec = sum(item["duration_sec"] for item in gap_records)
    return {
        "clock": "corrected_bag_time",
        "coverage_source": "full_scan_completed_clip_intervals",
        "gap_policy": "gaps_are_explicitly_unobservable_and_never_inferred",
        "scan_anchor_record_count": scan_counts["record_count"],
        "scan_anchor_skipped_inside_gap_count": scan_counts[
            "skipped_anchor_inside_gap_count"
        ],
        "scan_clip_interval_count": len(scan_clip_intervals),
        "wall_clock_duration_sec": round(wall_clock_duration_sec, 9),
        "observable_duration_sec": round(observable_duration_sec, 9),
        "covered_observable_sec": round(covered_observable_sec, 9),
        "observable_coverage_ratio": (
            round(covered_observable_sec / observable_duration_sec, 9)
            if observable_duration_sec > 0
            else 1.0
        ),
        "wall_clock_coverage_ratio": (
            round(covered_observable_sec / wall_clock_duration_sec, 9)
            if wall_clock_duration_sec > 0
            else 1.0
        ),
        "gap_count": len(gap_records),
        "gap_duration_sec": round(gap_duration_sec, 9),
        "gap_coverage_sec": 0.0,
        "segments": coverage_segments,
        "gaps": gap_records,
    }


def build_index(
    *,
    case_id: str,
    transcript_proposals_path: Path,
    transcript_report_path: Path,
    scan_proposals_path: Path,
    scan_report_path: Path,
    voice_path: Path,
    timeline_path: Path,
    cluster_threshold_sec: float,
    review_pad_before_sec: float,
    review_pad_after_sec: float,
    voice_lookback_sec: float,
    voice_window_before_sec: float,
    voice_window_after_sec: float,
) -> dict[str, Any]:
    for name, value in (
        ("cluster_threshold_sec", cluster_threshold_sec),
        ("review_pad_before_sec", review_pad_before_sec),
        ("review_pad_after_sec", review_pad_after_sec),
        ("voice_lookback_sec", voice_lookback_sec),
        ("voice_window_before_sec", voice_window_before_sec),
        ("voice_window_after_sec", voice_window_after_sec),
    ):
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} must be finite and non-negative")

    timeline = load_json(timeline_path)
    timestamps, source_fps, segments, frame_segments = validate_timeline(
        timeline,
        case_id=case_id,
    )
    transcript_run = validate_child_run(
        case_id=case_id,
        pass_name="transcript",
        proposal_path=transcript_proposals_path,
        report_path=transcript_report_path,
        timeline_path=timeline_path,
    )
    scan_run = validate_child_run(
        case_id=case_id,
        pass_name="scan",
        proposal_path=scan_proposals_path,
        report_path=scan_report_path,
        timeline_path=timeline_path,
    )
    validate_cross_pass_identity(transcript_run, scan_run)

    all_candidates: list[dict[str, Any]] = []
    all_raw_queries: list[dict[str, Any]] = []
    pass_counts: dict[str, dict[str, int]] = {}
    scan_clip_intervals: list[dict[str, Any]] = []
    for pass_name, proposal_path, report_path, run in (
        (
            "transcript",
            transcript_proposals_path,
            transcript_report_path,
            transcript_run,
        ),
        ("scan", scan_proposals_path, scan_report_path, scan_run),
    ):
        candidates, raw_queries, clips, counts = parse_proposal_records(
            case_id=case_id,
            pass_name=pass_name,
            proposal_path=proposal_path,
            proposal_sha256=run["proposal_file_sha256"],
            report_path=report_path,
            report_sha256=run["report_file_sha256"],
            records=load_jsonl(proposal_path),
            timestamps=timestamps,
            frame_segments=frame_segments,
        )
        reported_counts = run["reported_counts"]
        expected_counts = {
            "anchor_count": counts["record_count"],
            "raw_query_count": len(raw_queries),
            "consensus_candidate_count": len(candidates),
            "skipped_anchor_inside_gap_count": counts[
                "skipped_anchor_inside_gap_count"
            ],
        }
        for field, expected in expected_counts.items():
            if reported_counts.get(field) != expected:
                raise ValueError(
                    f"{pass_name} report {field} does not match JSONL"
                )
        all_candidates.extend(candidates)
        all_raw_queries.extend(raw_queries)
        pass_counts[pass_name] = counts
        if pass_name == "scan":
            scan_clip_intervals = clips

    voice_records = validate_voice_records(
        load_jsonl(voice_path),
        case_id=case_id,
    )
    gaps = timeline.get("gaps", [])
    candidate_clusters, linked_voice_ids = build_candidate_clusters(
        case_id=case_id,
        candidates=all_candidates,
        voice_records=voice_records,
        cluster_threshold_sec=cluster_threshold_sec,
        review_pad_before_sec=review_pad_before_sec,
        review_pad_after_sec=review_pad_after_sec,
        voice_lookback_sec=voice_lookback_sec,
        timestamps=timestamps,
        source_fps=source_fps,
        segments=segments,
        gaps=gaps,
    )
    voice_only_windows = build_voice_only_windows(
        case_id=case_id,
        voice_records=voice_records,
        voice_ids_with_candidate=linked_voice_ids,
        before_sec=voice_window_before_sec,
        after_sec=voice_window_after_sec,
        timestamps=timestamps,
        source_fps=source_fps,
        segments=segments,
        gaps=gaps,
    )
    raw_query_ref_ids = {
        item["raw_query_ref_id"] for item in all_raw_queries
    }
    referenced_raw_query_ids = {
        ref_id
        for candidate in all_candidates
        for ref_id in candidate["source"]["raw_query_ref_ids"]
    }
    if not referenced_raw_query_ids <= raw_query_ref_ids:
        raise AssertionError("candidate references an unknown raw query")

    coverage = calculate_scan_coverage(
        scan_clip_intervals=scan_clip_intervals,
        scan_counts=pass_counts["scan"],
        segments=segments,
        gaps=gaps,
        timeline_start_sec=timestamps[0],
        timeline_end_sec=timestamps[-1],
    )
    return {
        "schema": "taskplanner.policy02_review_index.v1",
        "case_id": case_id,
        "authority": "proposal_index_only_not_ground_truth",
        "policy_version": POLICY_VERSION,
        "query_policy_id": MODEL_QUERY_POLICY_ID,
        "prohibitions": {
            "auto_confirmation_forbidden": True,
            "auto_tool_identity_forbidden": True,
            "auto_transfer_direction_forbidden": True,
            "auto_request_boundary_forbidden": True,
            "ground_truth_runtime_exposure_forbidden": True,
        },
        "source_validation": {
            "transcript_run": transcript_run,
            "scan_run": scan_run,
            "voice_file": str(voice_path.resolve()),
            "voice_file_sha256": sha256_file(voice_path),
            "timeline_file": str(timeline_path.resolve()),
            "timeline_file_sha256": sha256_file(timeline_path),
            "cross_pass_identity_valid": True,
        },
        "clustering_policy": {
            "clock": "corrected_bag_time",
            "event_agnostic": True,
            "method": "bounded_cluster_time_span_within_observability_segment",
            "max_cluster_span_sec": cluster_threshold_sec,
            "review_pad_before_sec": review_pad_before_sec,
            "review_pad_after_sec": review_pad_after_sec,
            "causal_voice_lookback_sec": voice_lookback_sec,
        },
        "counts": {
            "consensus_candidate_count": len(all_candidates),
            "candidate_cluster_count": len(candidate_clusters),
            "raw_query_evidence_count": len(all_raw_queries),
            "raw_query_refs_linked_to_candidates_count": len(
                referenced_raw_query_ids
            ),
            "voice_event_count": len(voice_records),
            "voice_only_false_negative_window_count": len(voice_only_windows),
        },
        "candidate_clusters": candidate_clusters,
        "voice_only_false_negative_windows": voice_only_windows,
        "raw_query_evidence": all_raw_queries,
        "full_scan_temporal_coverage": coverage,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a create-only policy02 proposal review index."
    )
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--transcript-proposals", type=Path, required=True)
    parser.add_argument("--transcript-report", type=Path, required=True)
    parser.add_argument("--scan-proposals", type=Path, required=True)
    parser.add_argument("--scan-report", type=Path, required=True)
    parser.add_argument("--voice", type=Path, required=True)
    parser.add_argument("--timeline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cluster-threshold-sec", type=float, default=1.0)
    parser.add_argument("--review-pad-before-sec", type=float, default=2.0)
    parser.add_argument("--review-pad-after-sec", type=float, default=2.0)
    parser.add_argument("--voice-lookback-sec", type=float, default=8.0)
    parser.add_argument("--voice-window-before-sec", type=float, default=1.25)
    parser.add_argument("--voice-window-after-sec", type=float, default=4.25)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit(f"refusing to overwrite existing output: {args.output}")
    inputs = (
        args.transcript_proposals,
        args.transcript_report,
        args.scan_proposals,
        args.scan_report,
        args.voice,
        args.timeline,
    )
    missing = [str(path) for path in inputs if not path.is_file()]
    if missing:
        raise SystemExit(f"missing input files: {missing}")
    try:
        index = build_index(
            case_id=args.case_id,
            transcript_proposals_path=args.transcript_proposals,
            transcript_report_path=args.transcript_report,
            scan_proposals_path=args.scan_proposals,
            scan_report_path=args.scan_report,
            voice_path=args.voice,
            timeline_path=args.timeline,
            cluster_threshold_sec=args.cluster_threshold_sec,
            review_pad_before_sec=args.review_pad_before_sec,
            review_pad_after_sec=args.review_pad_after_sec,
            voice_lookback_sec=args.voice_lookback_sec,
            voice_window_before_sec=args.voice_window_before_sec,
            voice_window_after_sec=args.voice_window_after_sec,
        )
        payload = (
            json.dumps(
                index,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        )
        atomic_create_text(args.output, payload)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(str(exc)) from exc
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "counts": index["counts"],
                "coverage": {
                    "observable_coverage_ratio": index[
                        "full_scan_temporal_coverage"
                    ]["observable_coverage_ratio"],
                    "gap_count": index["full_scan_temporal_coverage"][
                        "gap_count"
                    ],
                },
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
