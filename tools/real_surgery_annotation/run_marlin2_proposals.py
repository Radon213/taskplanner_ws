#!/usr/bin/env python3
"""Run Marlin-2B as a proposal-only temporal-grounding annotator.

The output deliberately preserves every model response and never writes a
confirmed annotation. Public transcript events may define search windows and
tool hints, but the model must still locate the visible event in video.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MODEL_QUERY_POLICY_ID = "observable_tool_interaction_policy02.v1"

MODEL_QUERIES = {
    "implicit_tool_request": [
        (
            "the full visible interval when the operating person's empty palm is "
            "clearly fully open, facing upward, and held out while waiting for a "
            "surgical instrument; start at the first unmistakable fully open-palm "
            "frame and end before tool contact"
        ),
        (
            "an empty upturned hand is visibly held fully open as a tool request; "
            "exclude reaching, a partly opened hand, a hand already holding or "
            "returning a tool, and every frame after contact with the tool"
        ),
    ],
    "scrub_nurse_to_surgeon": [
        (
            "the exact handover boundary when the operating person first has "
            "secure control of a surgical instrument passed by the scrub nurse; "
            "exclude approach and pre-contact frames"
        ),
        (
            "the first visible moment the surgeon or surgical assistant has taken "
            "stable control of the offered tool from the scrub nurse, not while "
            "the tool is merely approaching or still controlled only by the nurse"
        ),
    ],
    "mayo_stand_to_scrub_nurse": [
        (
            "the first visible frame when the scrub nurse controls a surgical "
            "instrument lifted clear of the Mayo stand; exclude hand approach, "
            "touching, or sliding while the tool remains on the stand"
        ),
        (
            "the exact pickup boundary when a tool visibly separates from the "
            "Mayo tray in the scrub nurse's hand, not the reach toward the tool"
        ),
    ],
    "surgeon_to_scrub_nurse": [
        (
            "the exact return-handover boundary when the scrub nurse first has "
            "stable control of a surgical instrument from the operating person; "
            "exclude approach and frames where the surgeon still solely controls it"
        ),
        (
            "the first visible moment the scrub nurse has taken control of the "
            "returned tool from the surgeon or surgical assistant, not merely "
            "reaching for or touching it"
        ),
    ],
    "scrub_nurse_to_mayo_stand": [
        (
            "the exact placement boundary when the scrub nurse releases a surgical "
            "instrument and it is visibly supported by the Mayo stand; exclude "
            "approach, hovering, or continued hand control"
        ),
        (
            "the first visible frame when a returned tool has settled on the Mayo "
            "tray after release from the scrub nurse's hand, not while it is still "
            "being carried above the tray"
        ),
    ],
    "surgeon_to_mayo_stand": [
        (
            "the exact direct-return boundary when the surgeon or surgical "
            "assistant releases a surgical instrument and it is visibly supported "
            "by the Mayo stand; exclude carrying, approach, and hovering"
        ),
        (
            "the first visible frame when a tool placed directly by the operating "
            "person has settled on the Mayo tray after hand release, without an "
            "intervening scrub-nurse handover"
        ),
    ],
}


def canonical_json_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def atomic_create_text(path: Path, text: str) -> None:
    """Publish a complete text file without replacing an existing path."""

    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(file_descriptor, 0o644)
        with os.fdopen(
            file_descriptor,
            "w",
            encoding="utf-8",
            newline="",
        ) as stream:
            file_descriptor = -1
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary_path, path)
        except FileExistsError as exc:
            raise FileExistsError(
                f"refusing to overwrite existing output: {path}"
            ) from exc
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        temporary_path.unlink(missing_ok=True)


def require_distinct_output_paths(output: Path, report: Path) -> None:
    if output.resolve() == report.resolve():
        raise ValueError("--output and --report must be different paths")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def nearest_frame_index(timestamps: list[float], target: float) -> int:
    import bisect

    index = bisect.bisect_left(timestamps, target)
    if index <= 0:
        return 0
    if index >= len(timestamps):
        return len(timestamps) - 1
    before = index - 1
    return (
        before
        if abs(timestamps[before] - target) <= abs(timestamps[index] - target)
        else index
    )


def nearest_frame_index_in_range(
    timestamps: list[float],
    target: float,
    *,
    first_frame_idx: int,
    last_frame_idx: int,
) -> int:
    import bisect

    index = bisect.bisect_left(
        timestamps,
        target,
        lo=first_frame_idx,
        hi=last_frame_idx + 1,
    )
    if index <= first_frame_idx:
        return first_frame_idx
    if index > last_frame_idx:
        return last_frame_idx
    before = index - 1
    return (
        before
        if abs(timestamps[before] - target) <= abs(timestamps[index] - target)
        else index
    )


def map_source_time(
    source_time_sec: float,
    *,
    source_fps: float,
    timestamps: list[float],
) -> dict[str, Any]:
    frame_idx = min(
        len(timestamps) - 1,
        max(0, round(source_time_sec * source_fps)),
    )
    return {
        "source_time_sec": round(frame_idx / source_fps, 9),
        "source_frame_idx": frame_idx,
        "bag_time_sec": timestamps[frame_idx],
    }


def map_clip_time(
    local_time_sec: float,
    *,
    clip_first_frame_idx: int,
    clip_last_frame_idx: int,
    source_fps: float,
    timestamps: list[float],
    observability_segment_id: str,
) -> dict[str, Any]:
    frame_offset = round(local_time_sec * source_fps)
    frame_idx = min(
        clip_last_frame_idx,
        max(clip_first_frame_idx, clip_first_frame_idx + frame_offset),
    )
    return {
        "source_time_sec": round(frame_idx / source_fps, 9),
        "source_frame_idx": frame_idx,
        "bag_time_sec": timestamps[frame_idx],
        "observability_segment_id": observability_segment_id,
    }


def detected_gap_boundaries(
    timestamps: list[float],
    *,
    source_fps: float,
) -> list[tuple[int, int]]:
    threshold_sec = 1.5 / source_fps
    return [
        (index, index + 1)
        for index in range(len(timestamps) - 1)
        if timestamps[index + 1] - timestamps[index] > threshold_sec
    ]


def observability_segments(
    timestamps: list[float],
    *,
    source_fps: float,
) -> tuple[list[dict[str, Any]], list[str]]:
    boundaries = detected_gap_boundaries(
        timestamps,
        source_fps=source_fps,
    )
    segments: list[dict[str, Any]] = []
    first_frame_idx = 0
    for segment_index, (before_frame_idx, after_frame_idx) in enumerate(
        [*boundaries, (len(timestamps) - 1, len(timestamps))],
        1,
    ):
        last_frame_idx = before_frame_idx
        segment_id = f"segment_{segment_index:04d}"
        segments.append(
            {
                "id": segment_id,
                "first_frame_idx": first_frame_idx,
                "last_frame_idx": last_frame_idx,
                "start_bag_time_sec": timestamps[first_frame_idx],
                "end_bag_time_sec": timestamps[last_frame_idx],
            }
        )
        first_frame_idx = after_frame_idx
    frame_segments = [""] * len(timestamps)
    for segment in segments:
        for frame_idx in range(
            int(segment["first_frame_idx"]),
            int(segment["last_frame_idx"]) + 1,
        ):
            frame_segments[frame_idx] = str(segment["id"])
    return segments, frame_segments


def containing_gap(
    timestamps: list[float],
    *,
    source_fps: float,
    target_bag_time_sec: float,
) -> dict[str, Any] | None:
    for before_frame_idx, after_frame_idx in detected_gap_boundaries(
        timestamps,
        source_fps=source_fps,
    ):
        before_time_sec = timestamps[before_frame_idx]
        after_time_sec = timestamps[after_frame_idx]
        if before_time_sec < target_bag_time_sec < after_time_sec:
            return {
                "before_frame_idx": before_frame_idx,
                "after_frame_idx": after_frame_idx,
                "before_time_sec": before_time_sec,
                "after_time_sec": after_time_sec,
                "delta_sec": round(after_time_sec - before_time_sec, 9),
            }
    return None


def clip_frame_bounds(
    *,
    anchor_frame_idx: int,
    segment_first_frame_idx: int,
    segment_last_frame_idx: int,
    clip_before_sec: float,
    clip_after_sec: float,
    source_fps: float,
) -> tuple[int, int]:
    first_frame_idx = max(
        segment_first_frame_idx,
        min(
            anchor_frame_idx,
            math.floor(anchor_frame_idx - clip_before_sec * source_fps),
        ),
    )
    last_frame_idx = min(
        segment_last_frame_idx,
        max(
            anchor_frame_idx,
            math.ceil(anchor_frame_idx + clip_after_sec * source_fps) - 1,
        ),
    )
    return first_frame_idx, last_frame_idx


def normalize_model_span(
    span: Any,
    *,
    clip_duration_sec: float,
) -> tuple[list[float] | None, list[str]]:
    errors: list[str] = []
    if span is None:
        return None, errors
    if not isinstance(span, (list, tuple)) or len(span) != 2:
        return None, ["span_must_have_two_numeric_endpoints"]
    normalized: list[float] = []
    for value in span:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            return None, ["span_endpoints_must_be_finite_numbers"]
        normalized.append(float(value))
    start_sec, end_sec = normalized
    if start_sec < 0:
        errors.append("span_start_before_clip")
    if end_sec < start_sec:
        errors.append("span_end_before_start")
    if end_sec > clip_duration_sec + 1e-6:
        errors.append("span_end_after_clip")
    return normalized, errors


def make_clip(
    source: Path,
    start_sec: float,
    end_sec: float,
    output: Path,
) -> float:
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{start_sec:.6f}",
            "-to",
            f"{end_sec:.6f}",
            "-i",
            str(source),
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "18",
            str(output),
        ],
        check=True,
    )
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    duration = float(json.loads(probe.stdout)["format"]["duration"])
    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"invalid generated clip duration: {duration}")
    return duration


def consensus(
    results: list[dict[str, Any]],
    *,
    timestamps: list[float],
    max_midpoint_delta_sec: float,
    segment_ranges: dict[str, tuple[int, int]],
    source_fps: float,
) -> dict[str, Any] | None:
    spans = [
        result["local_span_sec"]
        for result in results
        if result["format_ok"] and result["local_span_sec"] is not None
    ]
    if len(spans) != len(results):
        return None
    midpoint_mappings = [result.get("midpoint_mapping") for result in results]
    if any(mapping is None for mapping in midpoint_mappings):
        return None
    segment_ids = {
        str(mapping["observability_segment_id"])
        for mapping in midpoint_mappings
    }
    if len(segment_ids) != 1:
        return None
    midpoint_bag_times = [
        float(mapping["bag_time_sec"]) for mapping in midpoint_mappings
    ]
    corrected_delta_sec = max(midpoint_bag_times) - min(midpoint_bag_times)
    if corrected_delta_sec > max_midpoint_delta_sec:
        return None
    segment_id = next(iter(segment_ids))
    first_frame_idx, last_frame_idx = segment_ranges[segment_id]
    consensus_bag_time_sec = statistics.median(midpoint_bag_times)
    consensus_frame_idx = nearest_frame_index_in_range(
        timestamps,
        consensus_bag_time_sec,
        first_frame_idx=first_frame_idx,
        last_frame_idx=last_frame_idx,
    )
    local_midpoints = [(span[0] + span[1]) / 2 for span in spans]
    return {
        "method": "median_of_query_span_midpoints_in_corrected_bag_time",
        "query_count": len(results),
        "max_midpoint_delta_sec": round(corrected_delta_sec, 6),
        "source_local_max_midpoint_delta_sec": round(
            max(local_midpoints) - min(local_midpoints),
            6,
        ),
        "local_time_sec": round(statistics.median(local_midpoints), 6),
        "source_time_sec": round(consensus_frame_idx / source_fps, 9),
        "source_frame_idx": consensus_frame_idx,
        "bag_time_sec": timestamps[consensus_frame_idx],
        "observability_segment_id": segment_id,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate proposal-only Marlin-2B evidence for tool interactions."
    )
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--timeline", type=Path, required=True)
    parser.add_argument("--anchors", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--model-id", default="NemoStation/Marlin-2B")
    parser.add_argument("--model-revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--clip-before-sec", type=float, default=1.25)
    parser.add_argument("--clip-after-sec", type=float, default=4.25)
    parser.add_argument("--max-midpoint-delta-sec", type=float, default=1.5)
    parser.add_argument("--caption-tokens", type=int, default=384)
    parser.add_argument(
        "--event-types",
        default=",".join(MODEL_QUERIES),
        help="Comma-separated subset of proposal event types.",
    )
    parser.add_argument(
        "--skip-caption",
        action="store_true",
        help="Skip dense caption generation for a focused fallback batch.",
    )
    parser.add_argument(
        "--device",
        choices=("cuda", "cpu"),
        default="cuda",
    )
    parser.add_argument(
        "--anchor-start-index",
        type=int,
        default=1,
        help="One-based inclusive anchor index.",
    )
    parser.add_argument(
        "--anchor-end-index",
        type=int,
        help="One-based inclusive anchor index; defaults to the final anchor.",
    )
    args = parser.parse_args()

    try:
        require_distinct_output_paths(args.output, args.report)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    for output in (args.output, args.report):
        if output.exists():
            raise SystemExit(f"refusing to overwrite existing output: {output}")
    if not args.video.is_file():
        raise SystemExit(f"video does not exist: {args.video}")
    if (
        not math.isfinite(args.clip_before_sec)
        or not math.isfinite(args.clip_after_sec)
        or args.clip_before_sec < 0
        or args.clip_after_sec < 0
        or args.clip_before_sec + args.clip_after_sec <= 0
    ):
        raise SystemExit("clip windows must be finite, non-negative, and non-empty")
    if (
        not math.isfinite(args.max_midpoint_delta_sec)
        or args.max_midpoint_delta_sec < 0
    ):
        raise SystemExit("--max-midpoint-delta-sec must be finite and non-negative")
    if args.caption_tokens <= 0:
        raise SystemExit("--caption-tokens must be positive")

    timeline = load_json(args.timeline)
    anchors_doc = load_json(args.anchors)
    if timeline.get("case_id") != args.case_id:
        raise SystemExit("timeline case_id does not match")
    if anchors_doc.get("case_id") != args.case_id:
        raise SystemExit("anchor case_id does not match")
    try:
        timestamps = [float(value) for value in timeline["timestamps_sec"]]
        source_fps = float(timeline["source_fps"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SystemExit(f"invalid timeline numeric data: {exc}") from exc
    if (
        not timestamps
        or any(not math.isfinite(value) for value in timestamps)
        or any(
            right <= left
            for left, right in zip(timestamps, timestamps[1:])
        )
    ):
        raise SystemExit("timeline timestamps must be finite and strictly increasing")
    if not math.isfinite(source_fps) or source_fps <= 0:
        raise SystemExit("timeline source_fps must be finite and positive")
    if len(timestamps) != int(timeline["frame_count"]):
        raise SystemExit("timeline frame_count does not match timestamp array")
    detected_boundaries = detected_gap_boundaries(
        timestamps,
        source_fps=source_fps,
    )
    declared_boundaries = [
        (
            int(gap["before_frame_idx"]),
            int(gap["after_frame_idx"]),
        )
        for gap in timeline.get("gaps", [])
    ]
    if declared_boundaries != detected_boundaries:
        raise SystemExit(
            "timeline gaps do not match timestamp discontinuities: "
            f"declared={declared_boundaries}, detected={detected_boundaries}"
        )
    segments, frame_segments = observability_segments(
        timestamps,
        source_fps=source_fps,
    )
    segments_by_id = {str(item["id"]): item for item in segments}
    segment_ranges = {
        str(item["id"]): (
            int(item["first_frame_idx"]),
            int(item["last_frame_idx"]),
        )
        for item in segments
    }
    all_anchors = anchors_doc["anchors"]
    if not isinstance(all_anchors, list) or not all_anchors:
        raise SystemExit("anchor document must contain a non-empty anchors list")
    end_index = (
        args.anchor_end_index
        if args.anchor_end_index is not None
        else len(all_anchors)
    )
    if (
        args.anchor_start_index < 1
        or end_index < args.anchor_start_index
        or end_index > len(all_anchors)
    ):
        raise SystemExit("invalid anchor index range")
    selected_anchors = all_anchors[args.anchor_start_index - 1 : end_index]
    selected_event_types = [
        value.strip()
        for value in args.event_types.split(",")
        if value.strip()
    ]
    unknown_event_types = set(selected_event_types) - set(MODEL_QUERIES)
    if (
        not selected_event_types
        or unknown_event_types
        or len(set(selected_event_types)) != len(selected_event_types)
    ):
        raise SystemExit(
            f"invalid --event-types: {sorted(unknown_event_types)}"
        )

    import torch
    from transformers import AutoModelForCausalLM

    started = time.monotonic()
    model_started = time.monotonic()
    model = AutoModelForCausalLM.from_pretrained(
        str(args.model),
        trust_remote_code=True,
        dtype=torch.bfloat16,
        device_map={"": args.device},
    )
    model_load_sec = time.monotonic() - model_started

    records: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix=f"{args.case_id}_marlin2_") as temp_dir:
        temp_root = Path(temp_dir)
        for anchor_index, anchor in enumerate(
            selected_anchors,
            args.anchor_start_index,
        ):
            anchor_bag_sec = float(anchor["time_sec"])
            if not math.isfinite(anchor_bag_sec):
                raise RuntimeError(
                    f"anchor {anchor.get('anchor_id')} has non-finite time_sec"
                )
            gap = containing_gap(
                timestamps,
                source_fps=source_fps,
                target_bag_time_sec=anchor_bag_sec,
            )
            if gap is not None:
                record = {
                    "schema": "taskplanner.marlin2_anchor_evidence.v1",
                    "case_id": args.case_id,
                    "processing_status": "skipped_anchor_inside_observability_gap",
                    "anchor": anchor,
                    "anchor_mapping": {
                        "requested_bag_time_sec": anchor_bag_sec,
                        "observable": False,
                        "gap": gap,
                    },
                    "clip": None,
                    "caption": {
                        "skipped": True,
                        "skip_reason": "anchor_inside_observability_gap",
                        "raw": None,
                        "scene": None,
                        "events": [],
                        "inference_sec": None,
                    },
                    "find_results": [],
                    "consensus_candidates": [],
                }
                records.append(record)
                print(
                    json.dumps(
                        {
                            "anchor_index": anchor_index,
                            "anchor_id": anchor["anchor_id"],
                            "processing_status": record["processing_status"],
                            "consensus_candidate_count": 0,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                continue

            anchor_frame_idx = nearest_frame_index(timestamps, anchor_bag_sec)
            anchor_source_sec = anchor_frame_idx / source_fps
            segment_id = frame_segments[anchor_frame_idx]
            segment = segments_by_id[segment_id]
            segment_first_frame_idx = int(segment["first_frame_idx"])
            segment_last_frame_idx = int(segment["last_frame_idx"])
            clip_first_frame_idx, clip_last_frame_idx = clip_frame_bounds(
                anchor_frame_idx=anchor_frame_idx,
                segment_first_frame_idx=segment_first_frame_idx,
                segment_last_frame_idx=segment_last_frame_idx,
                clip_before_sec=args.clip_before_sec,
                clip_after_sec=args.clip_after_sec,
                source_fps=source_fps,
            )
            clip_start = clip_first_frame_idx / source_fps
            clip_end = (clip_last_frame_idx + 1) / source_fps
            clip_path = temp_root / f"anchor_{anchor_index:03d}.mp4"
            clip_duration_sec = make_clip(
                args.video,
                clip_start,
                clip_end,
                clip_path,
            )

            caption = None
            caption_sec = None
            if not args.skip_caption:
                caption_started = time.monotonic()
                caption = model.caption(
                    str(clip_path),
                    max_new_tokens=args.caption_tokens,
                    do_sample=False,
                )
                caption_sec = time.monotonic() - caption_started

            query_records: list[dict[str, Any]] = []
            candidate_events: list[dict[str, Any]] = []
            for event_type in selected_event_types:
                queries = MODEL_QUERIES[event_type]
                event_results: list[dict[str, Any]] = []
                for query in queries:
                    query_started = time.monotonic()
                    result = model.find(
                        str(clip_path),
                        event=query,
                        max_new_tokens=64,
                        do_sample=False,
                    )
                    query_sec = time.monotonic() - query_started
                    model_format_ok = bool(result.get("format_ok"))
                    span, span_errors = normalize_model_span(
                        result.get("span"),
                        clip_duration_sec=clip_duration_sec,
                    )
                    if model_format_ok and span is None and not span_errors:
                        span_errors.append("format_ok_result_missing_span")
                    if not model_format_ok:
                        span_errors.append("model_format_not_ok")
                    format_ok = model_format_ok and span is not None and not span_errors
                    mapped_span = None
                    midpoint_mapping = None
                    if span is not None and not span_errors:
                        mapped_span = {
                            "start": map_clip_time(
                                span[0],
                                clip_first_frame_idx=clip_first_frame_idx,
                                clip_last_frame_idx=clip_last_frame_idx,
                                source_fps=source_fps,
                                timestamps=timestamps,
                                observability_segment_id=segment_id,
                            ),
                            "end": map_clip_time(
                                span[1],
                                clip_first_frame_idx=clip_first_frame_idx,
                                clip_last_frame_idx=clip_last_frame_idx,
                                source_fps=source_fps,
                                timestamps=timestamps,
                                observability_segment_id=segment_id,
                            ),
                        }
                        midpoint_mapping = map_clip_time(
                            (span[0] + span[1]) / 2,
                            clip_first_frame_idx=clip_first_frame_idx,
                            clip_last_frame_idx=clip_last_frame_idx,
                            source_fps=source_fps,
                            timestamps=timestamps,
                            observability_segment_id=segment_id,
                        )
                    query_record = {
                        "event_type": event_type,
                        "query": query,
                        "raw": result.get("raw"),
                        "model_format_ok": model_format_ok,
                        "format_ok": format_ok,
                        "validation_errors": span_errors,
                        "local_span_sec": span,
                        "mapped_span": mapped_span,
                        "midpoint_mapping": midpoint_mapping,
                        "clip_source_start_sec": round(clip_start, 9),
                        "inference_sec": round(query_sec, 6),
                    }
                    event_results.append(query_record)
                    query_records.append(query_record)
                event_consensus = consensus(
                    event_results,
                    source_fps=source_fps,
                    timestamps=timestamps,
                    max_midpoint_delta_sec=args.max_midpoint_delta_sec,
                    segment_ranges=segment_ranges,
                )
                if event_consensus is not None:
                    candidate_events.append(
                        {
                            "event_type": event_type,
                            "review_status": "proposed",
                            "label_origin": "temporal_grounding_model",
                            "tool_hint": anchor.get("tool_hint"),
                            "time": event_consensus,
                        }
                    )

            record = {
                "schema": "taskplanner.marlin2_anchor_evidence.v1",
                "case_id": args.case_id,
                "processing_status": "completed",
                "anchor": anchor,
                "anchor_mapping": {
                    "requested_bag_time_sec": anchor_bag_sec,
                    "source_frame_idx": anchor_frame_idx,
                    "source_time_sec": round(anchor_source_sec, 9),
                    "actual_bag_time_sec": timestamps[anchor_frame_idx],
                    "observable": True,
                    "observability_segment_id": segment_id,
                },
                "clip": {
                    "source_start_sec": round(clip_start, 9),
                    "source_end_sec": round(clip_end, 9),
                    "duration_sec": round(clip_duration_sec, 9),
                    "gap_trimmed": (
                        clip_first_frame_idx
                        > math.floor(
                            anchor_frame_idx
                            - args.clip_before_sec * source_fps
                        )
                        or clip_last_frame_idx
                        < math.ceil(
                            anchor_frame_idx
                            + args.clip_after_sec * source_fps
                        )
                        - 1
                    ),
                    "observability_segment_id": segment_id,
                    "start": map_clip_time(
                        0.0,
                        clip_first_frame_idx=clip_first_frame_idx,
                        clip_last_frame_idx=clip_last_frame_idx,
                        source_fps=source_fps,
                        timestamps=timestamps,
                        observability_segment_id=segment_id,
                    ),
                    "end": map_clip_time(
                        clip_duration_sec,
                        clip_first_frame_idx=clip_first_frame_idx,
                        clip_last_frame_idx=clip_last_frame_idx,
                        source_fps=source_fps,
                        timestamps=timestamps,
                        observability_segment_id=segment_id,
                    ),
                },
                "caption": {
                    "skipped": args.skip_caption,
                    "raw": caption.get("raw") if caption is not None else None,
                    "scene": caption.get("scene") if caption is not None else None,
                    "events": caption.get("events") if caption is not None else [],
                    "inference_sec": (
                        round(caption_sec, 6)
                        if caption_sec is not None
                        else None
                    ),
                },
                "find_results": query_records,
                "consensus_candidates": candidate_events,
            }
            records.append(record)
            print(
                json.dumps(
                    {
                        "anchor_index": anchor_index,
                        "anchor_id": anchor["anchor_id"],
                        "processing_status": record["processing_status"],
                        "consensus_candidate_count": len(candidate_events),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    output_text = "".join(
        json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
        for record in records
    )
    output_sha256 = hashlib.sha256(output_text.encode("utf-8")).hexdigest()
    report = {
        "schema": "taskplanner.marlin2_proposal_run.v1",
        "case_id": args.case_id,
        "status": "completed",
        "authority": "proposal_only_not_ground_truth",
        "phase_annotation_performed": False,
        "model": {
            "id": args.model_id,
            "revision": args.model_revision,
            "local_path": str(Path(args.model).resolve()),
        },
        "inputs": {
            "video": str(args.video.resolve()),
            "video_sha256": sha256_file(args.video),
            "timeline": str(args.timeline.resolve()),
            "timeline_sha256": sha256_file(args.timeline),
            "anchors": str(args.anchors.resolve()),
            "anchors_sha256": sha256_file(args.anchors),
        },
        "settings": {
            "query_policy_id": MODEL_QUERY_POLICY_ID,
            "query_prompt_sha256": canonical_json_sha256(
                {
                    event_type: MODEL_QUERIES[event_type]
                    for event_type in selected_event_types
                }
            ),
            "anchor_start_index": args.anchor_start_index,
            "anchor_end_index": end_index,
            "clip_before_sec": args.clip_before_sec,
            "clip_after_sec": args.clip_after_sec,
            "max_midpoint_delta_sec": args.max_midpoint_delta_sec,
            "midpoint_delta_clock": "corrected_bag_time",
            "gap_policy": {
                "anchor_inside_gap": "skip_with_explicit_record",
                "clip_crossing_gap": "trim_to_single_observability_segment",
                "consensus_requires_same_observability_segment": True,
            },
            "caption_tokens": args.caption_tokens,
            "skip_caption": args.skip_caption,
            "event_types": selected_event_types,
            "queries": {
                event_type: MODEL_QUERIES[event_type]
                for event_type in selected_event_types
            },
        },
        "counts": {
            "anchor_count": len(records),
            "completed_anchor_count": sum(
                item.get("processing_status") == "completed"
                for item in records
            ),
            "skipped_anchor_inside_gap_count": sum(
                item.get("processing_status")
                == "skipped_anchor_inside_observability_gap"
                for item in records
            ),
            "raw_query_count": sum(len(item["find_results"]) for item in records),
            "invalid_query_span_count": sum(
                bool(result.get("validation_errors"))
                for item in records
                for result in item["find_results"]
            ),
            "consensus_candidate_count": sum(
                len(item["consensus_candidates"]) for item in records
            ),
        },
        "runtime": {
            "model_load_sec": round(model_load_sec, 6),
            "total_sec": round(time.monotonic() - started, 6),
            "torch_version": torch.__version__,
            "cuda_version": torch.version.cuda,
            "inference_device": args.device,
            "gpu": (
                torch.cuda.get_device_name(0)
                if torch.cuda.is_available()
                else None
            ),
        },
        "output": str(args.output.resolve()),
        "output_sha256": output_sha256,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    report_text = (
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    )
    try:
        atomic_create_text(args.output, output_text)
        atomic_create_text(args.report, report_text)
    except FileExistsError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(report["counts"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
