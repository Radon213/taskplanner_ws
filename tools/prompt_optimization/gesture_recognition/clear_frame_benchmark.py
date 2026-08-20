#!/usr/bin/env python3
"""Build a conservative, boundary-clear CAM4 open-hand benchmark.

The legacy gesture references are interaction intervals, not per-frame pose
annotations.  This builder does not alter them.  Instead it constructs a
clearly documented evaluation proxy requested by the task owner:

* exactly one positive is the temporal midpoint of every confirmed open-hand
  interval; and
* negative controls are the midpoints of internal gaps between confirmed
  open-hand intervals, retained only when they are at least a fixed clearance
  from both neighboring interval boundaries.

The result remains evaluation-only.  Case, event, timestamp, and labels live
only in the manifest and are never included in a VLM request.
"""

from __future__ import annotations

import argparse
import hashlib
import math
from pathlib import Path
from statistics import median
from typing import Any, Mapping, Sequence

from tools.prompt_optimization.gesture_recognition import gesture_prompt_eval as gesture
from tools.prompt_optimization.gesture_recognition.prompts import PROMPTS, get_prompt


DEFAULT_CASE_IDS = tuple(f"0704_{index}" for index in range(6, 18))
DEFAULT_DEVELOPMENT_CASE_IDS = tuple(f"0704_{index}" for index in range(6, 15))
DEFAULT_CALIBRATION_EVENT_FRACTION = 0.60
DEFAULT_MIN_NEGATIVE_CLEARANCE_FRAMES = 45
DEFAULT_MODEL_ID = "qwen3.6-35b-a3b"
DEFAULT_PROMPT_VERSION = "gesture-top-right-open-hand-v8"
DEFAULT_INPUT_VARIANT = "right_detail_only"
COVERAGE_SCHEMA = "taskplanner.gesture_clear_frame_coverage.v1"
PROTOCOL_SCHEMA = "taskplanner.gesture_clear_frame_frozen_protocol.v1"


def _frame_interval_seconds(timestamps: Sequence[Any]) -> float:
    deltas = [
        float(right) - float(left)
        for left, right in zip(timestamps, timestamps[1:])
        if math.isfinite(float(left))
        and math.isfinite(float(right))
        and float(right) > float(left)
    ]
    if not deltas:
        raise ValueError("CAM4 timeline has no positive timestamp deltas")
    return float(median(deltas))


def _merge_intervals(
    events: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Merge overlapping/adjacent targets only for gap construction."""

    merged: list[dict[str, Any]] = []
    for event in events:
        start = int(event["start_frame"])
        end = int(event["end_frame"])
        if end < start:
            raise ValueError(f"inverted interval: {event['event_id']}")
        if not merged or start > int(merged[-1]["end_frame"]) + 1:
            merged.append(
                {
                    "start_frame": start,
                    "end_frame": end,
                    "event_ids": [str(event["event_id"])],
                }
            )
            continue
        merged[-1]["end_frame"] = max(int(merged[-1]["end_frame"]), end)
        merged[-1]["event_ids"].append(str(event["event_id"]))
    return merged


def _internal_gap_midpoints(
    *, merged_intervals: Sequence[Mapping[str, Any]], min_clearance_frames: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return retained and omitted inter-event gap-center negative candidates.

    Video heads/tails are intentionally excluded: they can be non-operative
    easy negatives.  A midpoint between two intervals maximizes its minimum
    distance to the two adjacent gesture boundaries.
    """

    retained: list[dict[str, Any]] = []
    omitted: list[dict[str, Any]] = []
    for ordinal, (left, right) in enumerate(
        zip(merged_intervals, merged_intervals[1:]), start=1
    ):
        gap_start = int(left["end_frame"]) + 1
        gap_end = int(right["start_frame"]) - 1
        if gap_end < gap_start:
            continue
        midpoint = gap_start + (gap_end - gap_start) // 2
        clearance = min(midpoint - int(left["end_frame"]), int(right["start_frame"]) - midpoint)
        candidate = {
            "gap_ordinal": ordinal,
            "gap_start_frame": gap_start,
            "gap_end_frame": gap_end,
            "frame_idx": midpoint,
            "nearest_open_hand_boundary_frames": clearance,
            "left_event_ids": list(left["event_ids"]),
            "right_event_ids": list(right["event_ids"]),
        }
        if clearance >= min_clearance_frames:
            retained.append(candidate)
        else:
            omitted.append(candidate)
    return retained, omitted


def _evaluation_group(
    *, case_id: str, development_case_ids: frozenset[str], early: bool
) -> str:
    if case_id in development_case_ids:
        return "development_calibration" if early else "development_temporal_challenge"
    return "case_holdout_early" if early else "case_holdout_late"


def _runtime_split(evaluation_group: str) -> str:
    return "calibration" if evaluation_group == "development_calibration" else "within_case_challenge"


def _sample(
    *,
    case_id: str,
    event_id: str,
    frame_idx: int,
    timestamps: Sequence[Any],
    label: str,
    sample_kind: str,
    evaluation_group: str,
    event_count_in_case: int,
) -> dict[str, Any]:
    if label not in {"open_receive", "not_open_receive"}:
        raise ValueError(f"unexpected label {label!r}")
    return {
        "schema": gesture.SAMPLE_SCHEMA,
        "sample_id": f"{case_id}-{event_id}-{sample_kind}-f{frame_idx:04d}",
        "case_id": case_id,
        "split": _runtime_split(evaluation_group),
        "evaluation_group": evaluation_group,
        "event_id": event_id,
        "frame_idx": frame_idx,
        "time_sec": round(gesture._frame_time(frame_idx, timestamps), 9),  # type: ignore[attr-defined]
        "label": label,
        "sample_kind": sample_kind,
        "onset_scorable": False,
        "event_count_in_case": event_count_in_case,
        "ground_truth_usage": "evaluation_only",
        "may_publish_runtime": False,
    }


def build_clear_frame_manifest(
    *,
    case_ids: Sequence[str] = DEFAULT_CASE_IDS,
    development_case_ids: Sequence[str] = DEFAULT_DEVELOPMENT_CASE_IDS,
    calibration_event_fraction: float = DEFAULT_CALIBRATION_EVENT_FRACTION,
    min_negative_clearance_frames: int = DEFAULT_MIN_NEGATIVE_CLEARANCE_FRAMES,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build the reproducible one-midpoint/high-clearance-control manifest."""

    if not 0.0 < calibration_event_fraction < 1.0:
        raise ValueError("calibration_event_fraction must be strictly between 0 and 1")
    if min_negative_clearance_frames < 1:
        raise ValueError("min_negative_clearance_frames must be positive")
    ordered_cases = tuple(dict.fromkeys(str(case_id) for case_id in case_ids))
    development = frozenset(str(case_id) for case_id in development_case_ids)
    if not development.issubset(ordered_cases):
        missing = sorted(development.difference(ordered_cases))
        raise ValueError("development cases absent from case list: " + ", ".join(missing))

    samples: list[dict[str, Any]] = []
    included_cases: list[dict[str, Any]] = []
    excluded_cases: list[dict[str, Any]] = []
    for case_id in ordered_cases:
        try:
            events_path, masks_path, timeline_path = gesture.resolve_case_sources(case_id)
            events = gesture.gesture_target_event_order(
                events_path=events_path, masks_path=masks_path, case_id=case_id
            )
            if len(events) < 2:
                raise ValueError("requires at least two confirmed open-hand intervals")
            event_count = len(events)
            calibration_count, temporal_boundary_sec = gesture._event_split_boundary(  # type: ignore[attr-defined]
                events, calibration_fraction=calibration_event_fraction
            )
            timeline = gesture.load_json(timeline_path)
            timestamps = timeline.get("timestamps_sec")
            if not isinstance(timestamps, list) or not timestamps:
                raise ValueError("timeline lacks a nonempty timestamps_sec list")
            frame_interval_sec = _frame_interval_seconds(timestamps)
        except (FileNotFoundError, ValueError) as exc:
            excluded_cases.append({"case_id": case_id, "reason": str(exc)})
            continue

        event_rank_by_id = {str(event["event_id"]): index for index, event in enumerate(events, start=1)}
        case_samples: list[dict[str, Any]] = []
        for event in events:
            event_id = str(event["event_id"])
            frame_idx = int(event["start_frame"]) + (
                int(event["end_frame"]) - int(event["start_frame"])
            ) // 2
            event_rank = event_rank_by_id[event_id]
            group = _evaluation_group(
                case_id=case_id,
                development_case_ids=development,
                early=event_rank <= calibration_count,
            )
            positive = _sample(
                case_id=case_id,
                event_id=event_id,
                frame_idx=frame_idx,
                timestamps=timestamps,
                label="open_receive",
                sample_kind="positive_event_midpoint",
                evaluation_group=group,
                event_count_in_case=event_count,
            )
            positive.update(
                {
                    "event_rank_in_case": event_rank,
                    "source_interval_start_frame": int(event["start_frame"]),
                    "source_interval_end_frame": int(event["end_frame"]),
                    "midpoint_offset_frames": frame_idx - int(event["start_frame"]),
                }
            )
            case_samples.append(positive)

        merged = _merge_intervals(events)
        negative_candidates, omitted_candidates = _internal_gap_midpoints(
            merged_intervals=merged,
            min_clearance_frames=min_negative_clearance_frames,
        )
        for candidate in negative_candidates:
            frame_idx = int(candidate["frame_idx"])
            time_sec = gesture._frame_time(frame_idx, timestamps)  # type: ignore[attr-defined]
            group = _evaluation_group(
                case_id=case_id,
                development_case_ids=development,
                early=time_sec < temporal_boundary_sec,
            )
            event_id = f"{case_id}-CLEAR_NEG_G{int(candidate['gap_ordinal']):04d}"
            negative = _sample(
                case_id=case_id,
                event_id=event_id,
                frame_idx=frame_idx,
                timestamps=timestamps,
                label="not_open_receive",
                sample_kind="negative_inter_event_gap_midpoint",
                evaluation_group=group,
                event_count_in_case=event_count,
            )
            negative.update(candidate)
            negative["nearest_open_hand_boundary_sec"] = round(
                int(candidate["nearest_open_hand_boundary_frames"]) * frame_interval_sec,
                9,
            )
            case_samples.append(negative)

        frame_ids = [int(sample["frame_idx"]) for sample in case_samples]
        if len(frame_ids) != len(set(frame_ids)):
            raise ValueError(f"{case_id}: duplicate selected clear-frame index")
        samples.extend(case_samples)
        included_cases.append(
            {
                "case_id": case_id,
                "role": "development" if case_id in development else "case_holdout",
                "events_path": str(events_path),
                "masks_path": str(masks_path),
                "timeline_path": str(timeline_path),
                "gesture_event_count": event_count,
                "positive_midpoint_count": event_count,
                "merged_interval_count": len(merged),
                "internal_gap_count": len(merged) - 1,
                "selected_clear_negative_count": len(negative_candidates),
                "omitted_short_gap_count": len(omitted_candidates),
                "calibration_event_count": calibration_count,
                "temporal_boundary_sec": round(temporal_boundary_sec, 9),
                "frame_interval_sec": round(frame_interval_sec, 9),
                "selected_clear_negative_candidates": negative_candidates,
                "omitted_short_gap_candidates": omitted_candidates,
            }
        )

    missing_development = sorted(
        development.difference({entry["case_id"] for entry in included_cases})
    )
    if missing_development:
        raise ValueError("required development case(s) unusable: " + ", ".join(missing_development))
    if not samples:
        raise ValueError("no clear-frame samples available")
    samples.sort(
        key=lambda sample: (
            str(sample["split"]),
            str(sample["case_id"]),
            int(sample["frame_idx"]),
            str(sample["sample_id"]),
        )
    )
    coverage = {
        "schema": COVERAGE_SCHEMA,
        "ground_truth_usage": "evaluation_only",
        "may_publish_runtime": False,
        "reference_authority": gesture.SOURCE_REFERENCE_AUTHORITY,
        "reference_interpretation": (
            "one midpoint per existing confirmed interaction interval and high-clearance "
            "inter-event non-request controls; a conservative event-derived proxy, not "
            "new frame-level human visual annotation"
        ),
        "selection_policy": {
            "positive": "one floor temporal midpoint from every confirmed open-hand interval",
            "negative": (
                "one midpoint from each internal inter-event gap; exclude video heads/tails "
                "and gaps whose midpoint is closer than the configured clearance to either "
                "neighboring open-hand interval"
            ),
            "min_negative_clearance_frames": min_negative_clearance_frames,
        },
        "calibration_event_fraction": calibration_event_fraction,
        "included_cases": included_cases,
        "excluded_cases": excluded_cases,
        "sample_count": len(samples),
        "groups": {
            group: sum(1 for sample in samples if sample["evaluation_group"] == group)
            for group in (
                "development_calibration",
                "development_temporal_challenge",
                "case_holdout_early",
                "case_holdout_late",
            )
        },
        "labels": {
            "open_receive": sum(sample["label"] == "open_receive" for sample in samples),
            "not_open_receive": sum(sample["label"] == "not_open_receive" for sample in samples),
        },
    }
    return samples, coverage


def _prompt_sha256(prompt_version: str) -> str:
    return hashlib.sha256(get_prompt(prompt_version).encode("utf-8")).hexdigest()


def write_frozen_protocol(
    *,
    path: Path,
    manifest_path: Path,
    coverage_path: Path,
    prompt_version: str,
    model_id: str,
    input_variant: str,
    min_negative_clearance_frames: int,
    overwrite: bool,
) -> None:
    protocol = {
        "schema": PROTOCOL_SCHEMA,
        "status": "frozen_before_inference",
        "ground_truth_usage": "evaluation_only",
        "may_publish_runtime": False,
        "manifest": str(manifest_path),
        "manifest_sha256": gesture.sha256_file(manifest_path),
        "coverage": str(coverage_path),
        "coverage_sha256": gesture.sha256_file(coverage_path),
        "model_id": model_id,
        "prompt_version": prompt_version,
        "prompt_sha256": _prompt_sha256(prompt_version),
        "input_variant": input_variant,
        "input_transform": "fixed CAM4 x=340..640, y=0..300 crop; no per-frame ROI",
        "positive_selection": "one temporal midpoint per confirmed open-hand interval",
        "negative_selection": "internal inter-event gap midpoint",
        "min_negative_clearance_frames": min_negative_clearance_frames,
        "runtime_policy": "fresh worker batches, retry=0, transport failures halt without scoring",
    }
    gesture.write_json(path, protocol, overwrite=overwrite)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", nargs="+", default=list(DEFAULT_CASE_IDS))
    parser.add_argument(
        "--development-cases", nargs="+", default=list(DEFAULT_DEVELOPMENT_CASE_IDS)
    )
    parser.add_argument(
        "--calibration-event-fraction", type=float, default=DEFAULT_CALIBRATION_EVENT_FRACTION
    )
    parser.add_argument(
        "--min-negative-clearance-frames",
        type=int,
        default=DEFAULT_MIN_NEGATIVE_CLEARANCE_FRAMES,
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--coverage", type=Path, required=True)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--prompt-version", choices=sorted(PROMPTS), default=DEFAULT_PROMPT_VERSION)
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID)
    parser.add_argument("--input-variant", default=DEFAULT_INPUT_VARIANT)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    if args.input_variant not in gesture.INPUT_VARIANTS:
        parser.error("unsupported input variant")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    samples, coverage = build_clear_frame_manifest(
        case_ids=args.cases,
        development_case_ids=args.development_cases,
        calibration_event_fraction=args.calibration_event_fraction,
        min_negative_clearance_frames=args.min_negative_clearance_frames,
    )
    gesture.write_jsonl(args.manifest, samples, overwrite=args.force)
    gesture.write_json(args.coverage, coverage, overwrite=args.force)
    write_frozen_protocol(
        path=args.protocol,
        manifest_path=args.manifest,
        coverage_path=args.coverage,
        prompt_version=args.prompt_version,
        model_id=args.model_id,
        input_variant=args.input_variant,
        min_negative_clearance_frames=args.min_negative_clearance_frames,
        overwrite=args.force,
    )
    print(
        gesture.canonical_json(
            {
                "manifest": str(args.manifest),
                "coverage": str(args.coverage),
                "protocol": str(args.protocol),
                "sample_count": len(samples),
                "labels": coverage["labels"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
