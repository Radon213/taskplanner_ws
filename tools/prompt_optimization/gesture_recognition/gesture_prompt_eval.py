#!/usr/bin/env python3
"""Build and run a leakage-safe CAM4 open-palm prompt evaluation.

The runner has a deliberately narrow authority boundary:

* labels are read only to create local evaluation samples and metrics;
* request payloads contain one CAM4 image and a task identifier only;
* no output is a ROS message, tool request, or physical-control instruction.

The multi-case runner uses the existing confirmed gesture references from
0704_6–0704_17 when available.  Development cases are split at predeclared
temporal boundaries and 0704_15–0704_17 remain case-level holdout.  A
positive-only mode preserves only existing confirmed open-hand samples when the
policy intentionally treats any visible open hand as positive.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from tools.prompt_optimization.gesture_recognition.prompts import (
    PROMPTS,
    TASK_ID,
    get_prompt,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CASE_ID = "0704_6"
DEFAULT_MODEL_ID = "qwen3.6-35b-a3b"
DEFAULT_BASE_URL = "http://127.0.0.1:8080"
DEFAULT_CALIBRATION_END_SEC = 90.0
DEFAULT_NEGATIVE_MARGIN_FRAMES = 12
# 0704_5 is deliberately part of the availability audit but not the default
# gesture benchmark: it has no complete confirmed gesture-target reference.
# A missing reference is never interpreted as a negative hand-pose example.
DEFAULT_AUDITED_CASE_IDS = tuple(f"0704_{index}" for index in range(5, 18))
DEFAULT_GESTURE_CASE_IDS = tuple(f"0704_{index}" for index in range(6, 18))
DEFAULT_DEVELOPMENT_CASE_IDS = tuple(f"0704_{index}" for index in range(6, 15))
DEFAULT_HOLDOUT_CASE_IDS = tuple(f"0704_{index}" for index in range(15, 18))
DEFAULT_CALIBRATION_EVENT_FRACTION = 0.60
MAX_NEGATIVE_BOUNDARY_SHIFT_FRAMES = 24
CAM4_PANEL_CROP = "crop=iw/2:ih:0:0"
# Keep the NInfer-tested 640x360 canvas even for a fixed detail view.  The
# square crop is enlarged without distortion, then padded horizontally; this
# avoids changing the backend's image geometry between experimental samples.
CAM4_RIGHT_DETAIL_CROP = (
    "crop=300:300:340:0,scale=360:360:flags=lanczos,pad=640:360:140:0:black"
)
INPUT_VARIANTS = frozenset(
    {
        "full_cam4",
        "right_detail_only",
        "full_plus_right_detail",
        "causal_right_detail_pair",
    }
)
CAUSAL_PRIOR_FRAMES = 12
SAMPLE_SCHEMA = "taskplanner.gesture_prompt_eval_sample.v1"
PREDICTION_SCHEMA = "taskplanner.gesture_prompt_eval_prediction.v1"
REPORT_SCHEMA = "taskplanner.gesture_prompt_eval_report.v1"
SOURCE_REFERENCE_AUTHORITY = "mixed_human_and_authorized_assistant_video_adjudication"
SOURCE_METRIC_INTERPRETATION = (
    "read-only historical human and authorized assistant video-review intervals; "
    "offline CAM4 frame agreement only, not a runtime handover or clinical-safety claim"
)
ALLOWED_GESTURES = frozenset(
    {"open_receive", "not_open_receive", "uncertain"}
)
MINIMAL_BINARY_PROMPT_VERSIONS = frozenset({"gesture-top-right-open-hand-v7"})
MINIMAL_BOOLEAN_PROMPT_VERSIONS = frozenset({"gesture-top-right-open-hand-v8"})
MINIMAL_EMPTY_OPEN_HAND_PROMPT_VERSIONS = frozenset(
    {
        "gesture-top-right-empty-open-hand-v9",
        "gesture-top-right-empty-open-hand-v10",
    }
)
MINIMAL_EMPTY_OPEN_HAND_UNCERTAIN_PROMPT_VERSIONS = frozenset(
    {"gesture-top-right-empty-open-hand-v11"}
)
MINIMAL_EMPTY_OPEN_HAND_NULLABLE_PROMPT_VERSIONS = frozenset(
    {
        "gesture-top-right-empty-open-hand-v12",
        "gesture-top-right-empty-open-hand-v14",
    }
)
MINIMAL_EMPTY_OPEN_HAND_NULLABLE_BARE_NULL_FALLBACK_PROMPT_VERSIONS = frozenset(
    {
        "gesture-top-right-empty-open-hand-v15",
        "gesture-top-right-empty-open-hand-v16-recovery",
        "gesture-top-right-empty-open-hand-v17-transition-guard",
        "gesture-top-right-empty-open-hand-v18-balanced-evidence",
    }
)
MINIMAL_EMPTY_OPEN_HAND_SCALAR_NULLABLE_PROMPT_VERSIONS = frozenset(
    {"gesture-top-right-empty-open-hand-v13"}
)


@dataclass(frozen=True)
class ParsedPrediction:
    """Strictly parsed model output; parse failures are explicit abstentions."""

    gesture: str
    confidence: float
    visual_evidence: str
    parse_error: str = ""


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


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


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number}: expected a JSON object")
        records.append(value)
    return records


def _versioned_file_sort_key(path: Path) -> tuple[int, str]:
    """Sort ``*.vN.*`` artifacts by numeric version, not lexical order."""

    match = re.search(r"\.v(\d+)\.", path.name)
    return (int(match.group(1)) if match else -1, path.name)


def _latest_case_artifact(case_id: str, pattern: str) -> Path:
    case_directory = (
        REPOSITORY_ROOT / "annotations/observable_tool_events/cases" / case_id
    )
    candidates = sorted(
        case_directory.glob(pattern), key=_versioned_file_sort_key
    )
    if not candidates:
        raise FileNotFoundError(
            f"{case_id}: no artifact matching {pattern!r} under {case_directory}"
        )
    return candidates[-1]


def resolve_case_sources(case_id: str) -> tuple[Path, Path, Path]:
    """Resolve the current confirmed event, mask, and CAM4 timeline artifacts."""

    events_path = _latest_case_artifact(
        case_id, "interaction_events.observed.final.v*.jsonl"
    )
    masks_path = _latest_case_artifact(case_id, "evaluation_masks.v*.json")
    timeline_path = _latest_case_artifact(case_id, "cam4_frame_timeline.v*.json")
    return events_path, masks_path, timeline_path


def gesture_target_event_order(
    *,
    events_path: Path,
    masks_path: Path,
    case_id: str,
) -> list[dict[str, Any]]:
    """Return only confirmed gesture targets in causal source-time order.

    This information stays in the evaluator manifest.  It is never inserted
    into an NInfer request.
    """

    masks = _gesture_target_masks(load_json(masks_path))
    selected: list[dict[str, Any]] = []
    for event in load_jsonl(events_path):
        event_id = str(event.get("event_id", "")).strip()
        if (
            event.get("event_type") != "implicit_tool_request"
            or event.get("review_status") != "confirmed"
            or str(event.get("case_id", "")) != case_id
            or event_id not in masks
        ):
            continue
        try:
            start_sec = float(event["start_sec"])
            start_frame = int(event["start_source_frame_idx"])
            end_frame = int(event["end_source_frame_idx"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"{events_path}: malformed gesture event {event_id}"
            ) from exc
        if not math.isfinite(start_sec) or end_frame < start_frame:
            raise ValueError(f"{events_path}: invalid gesture interval for {event_id}")
        selected.append(
            {
                "event_id": event_id,
                "start_sec": start_sec,
                "start_frame": start_frame,
                "end_frame": end_frame,
            }
        )
    selected.sort(key=lambda item: (float(item["start_sec"]), str(item["event_id"])))
    return selected


def _event_split_boundary(
    events: Sequence[Mapping[str, Any]], *, calibration_fraction: float
) -> tuple[int, float]:
    """Choose a predeclared between-event temporal boundary for one case."""

    if not 0.0 < calibration_fraction < 1.0:
        raise ValueError("calibration_fraction must be strictly between zero and one")
    if len(events) < 2:
        raise ValueError("at least two confirmed gesture events are required per case")
    calibration_event_count = max(
        1, min(len(events) - 1, math.ceil(len(events) * calibration_fraction))
    )
    left = float(events[calibration_event_count - 1]["start_sec"])
    right = float(events[calibration_event_count]["start_sec"])
    if not left < right:
        raise ValueError("gesture event timestamps must be strictly ordered")
    return calibration_event_count, (left + right) / 2.0


def build_multicase_manifest(
    *,
    case_ids: Sequence[str],
    development_case_ids: Sequence[str],
    calibration_event_fraction: float,
    negative_margin_frames: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Create an audit-backed, case-aware temporal prompt benchmark.

    Development cases are split chronologically per case.  The held-out cases
    are also split early/late for reporting, but neither half is used for
    prompt or threshold selection.  This permits all available 0704 gesture
    videos to be evaluated without leaking their labels into calibration.
    """

    ordered_case_ids = tuple(dict.fromkeys(str(case_id) for case_id in case_ids))
    development = frozenset(str(case_id) for case_id in development_case_ids)
    unknown_development = sorted(development.difference(ordered_case_ids))
    if unknown_development:
        raise ValueError(
            "development cases are absent from case_ids: "
            + ", ".join(unknown_development)
        )

    all_samples: list[dict[str, Any]] = []
    included_cases: list[dict[str, Any]] = []
    excluded_cases: list[dict[str, Any]] = []
    for case_id in ordered_case_ids:
        try:
            events_path, masks_path, timeline_path = resolve_case_sources(case_id)
            event_order = gesture_target_event_order(
                events_path=events_path,
                masks_path=masks_path,
                case_id=case_id,
            )
            if not event_order:
                raise ValueError("no confirmed gesture_target events")
            calibration_event_count, boundary_sec = _event_split_boundary(
                event_order,
                calibration_fraction=calibration_event_fraction,
            )
            case_samples = build_manifest(
                events_path=events_path,
                masks_path=masks_path,
                timeline_path=timeline_path,
                case_id=case_id,
                calibration_end_sec=boundary_sec,
                negative_margin_frames=negative_margin_frames,
            )
        except (FileNotFoundError, ValueError) as exc:
            excluded_cases.append({"case_id": case_id, "reason": str(exc)})
            continue

        event_ranks = {
            str(event["event_id"]): index + 1
            for index, event in enumerate(event_order)
        }
        for sample in case_samples:
            event_rank = event_ranks[str(sample["event_id"])]
            early = event_rank <= calibration_event_count
            if case_id in development:
                evaluation_group = (
                    "development_calibration"
                    if early
                    else "development_temporal_challenge"
                )
            else:
                evaluation_group = "case_holdout_early" if early else "case_holdout_late"
            enriched = dict(sample)
            # The existing runner/scorer recognizes these two values.  The
            # richer group field keeps the case-level holdout separate in the
            # report while still ensuring threshold selection sees development
            # calibration only.
            enriched["split"] = (
                "calibration"
                if evaluation_group == "development_calibration"
                else "within_case_challenge"
            )
            enriched["evaluation_group"] = evaluation_group
            enriched["event_rank_in_case"] = event_rank
            enriched["event_count_in_case"] = len(event_order)
            enriched["ground_truth_usage"] = "evaluation_only"
            enriched["may_publish_runtime"] = False
            all_samples.append(enriched)

        included_cases.append(
            {
                "case_id": case_id,
                "role": "development" if case_id in development else "case_holdout",
                "gesture_event_count": len(event_order),
                "calibration_event_count": calibration_event_count,
                "temporal_boundary_sec": round(boundary_sec, 9),
                "events_path": str(events_path),
                "masks_path": str(masks_path),
                "timeline_path": str(timeline_path),
                "sample_count": len(case_samples),
            }
        )

    missing_development = sorted(
        development.difference({entry["case_id"] for entry in included_cases})
    )
    if missing_development:
        raise ValueError(
            "required development case(s) lack a usable gesture reference: "
            + ", ".join(missing_development)
        )
    if not all_samples:
        raise ValueError("no usable gesture samples across the requested cases")
    all_samples.sort(
        key=lambda sample: (
            str(sample["split"]),
            str(sample["case_id"]),
            int(sample["frame_idx"]),
            str(sample["sample_id"]),
        )
    )
    coverage = {
        "schema": "taskplanner.gesture_prompt_eval_coverage.v1",
        "ground_truth_usage": "evaluation_only",
        "may_publish_runtime": False,
        "calibration_event_fraction": calibration_event_fraction,
        "included_cases": included_cases,
        "excluded_cases": excluded_cases,
        "sample_count": len(all_samples),
        "groups": {
            group: sum(1 for sample in all_samples if sample["evaluation_group"] == group)
            for group in (
                "development_calibration",
                "development_temporal_challenge",
                "case_holdout_early",
                "case_holdout_late",
            )
        },
    }
    return all_samples, coverage


def atomic_write_text(path: Path, text: str, *, overwrite: bool) -> None:
    """Write a complete artifact atomically without silently replacing one."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        raise FileExistsError(
            f"Refusing to overwrite {path}; choose a new run directory or use --force"
        )
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            descriptor = -1
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)


def write_jsonl(
    path: Path,
    records: Iterable[Mapping[str, Any]],
    *,
    overwrite: bool,
) -> None:
    payload = "".join(canonical_json(dict(record)) + "\n" for record in records)
    atomic_write_text(path, payload, overwrite=overwrite)


def write_json(path: Path, value: Mapping[str, Any], *, overwrite: bool) -> None:
    atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        overwrite=overwrite,
    )


def _gesture_target_masks(evaluation_masks: Mapping[str, Any]) -> dict[str, dict[str, bool]]:
    masks: dict[str, dict[str, bool]] = {}
    raw_roles = evaluation_masks.get("event_roles", [])
    if not isinstance(raw_roles, list):
        raise ValueError("evaluation_masks.event_roles must be a list")
    for raw_role in raw_roles:
        if not isinstance(raw_role, dict) or raw_role.get("role") != "gesture_target":
            continue
        event_id = str(raw_role.get("event_id", "")).strip()
        eligibility = raw_role.get("metric_eligibility", {})
        if not event_id or not isinstance(eligibility, dict):
            continue
        if bool(eligibility.get("gesture_presence")):
            masks[event_id] = {
                "gesture_presence": True,
                "gesture_onset": bool(eligibility.get("gesture_onset")),
            }
    return masks


def _valid_frame_index(frame_index: int, timestamps: Sequence[Any]) -> bool:
    return 0 <= frame_index < len(timestamps)


def _frame_time(frame_index: int, timestamps: Sequence[Any]) -> float:
    if not _valid_frame_index(frame_index, timestamps):
        raise ValueError(
            f"frame {frame_index} is outside the CAM4 timeline of {len(timestamps)} frames"
        )
    value = float(timestamps[frame_index])
    if not math.isfinite(value):
        raise ValueError(f"CAM4 timestamp at frame {frame_index} is not finite")
    return value


def _within_any_interval(frame_index: int, intervals: Sequence[tuple[int, int]]) -> bool:
    return any(start <= frame_index <= end for start, end in intervals)


def _nearest_nonpositive_boundary_frame(
    *,
    candidate_index: int,
    direction: int,
    intervals: Sequence[tuple[int, int]],
    timestamps: Sequence[Any],
) -> int | None:
    """Find the closest in-range frame outside every confirmed positive span.

    Back-to-back requests can make a nominal 12-frame boundary negative fall
    inside the next request's positive interval.  Skipping the candidate (not
    the entire case) preserves a valid negative without relabeling a positive
    frame as negative.
    """

    if direction not in {-1, 1}:
        raise ValueError("direction must be -1 or 1")
    frame_index = candidate_index
    while _valid_frame_index(frame_index, timestamps):
        if not _within_any_interval(frame_index, intervals):
            return frame_index
        frame_index += direction
    return None


def _make_sample(
    *,
    case_id: str,
    split: str,
    event_id: str,
    frame_index: int,
    timestamps: Sequence[Any],
    label: str,
    sample_kind: str,
    onset_scorable: bool,
) -> dict[str, Any]:
    if label not in {"open_receive", "not_open_receive"}:
        raise ValueError(f"unsupported evaluation label: {label}")
    return {
        "schema": SAMPLE_SCHEMA,
        "sample_id": f"{case_id}-{event_id}-{sample_kind}-f{frame_index:04d}",
        "case_id": case_id,
        "split": split,
        "event_id": event_id,
        "frame_idx": frame_index,
        "time_sec": round(_frame_time(frame_index, timestamps), 9),
        "label": label,
        "sample_kind": sample_kind,
        "onset_scorable": onset_scorable,
        "ground_truth_usage": "evaluation_only",
        "may_publish_runtime": False,
    }


def build_manifest(
    *,
    events_path: Path,
    masks_path: Path,
    timeline_path: Path,
    case_id: str,
    calibration_end_sec: float,
    negative_margin_frames: int,
) -> list[dict[str, Any]]:
    """Create balanced positive/boundary-negative samples without model input leakage."""

    if negative_margin_frames < 1:
        raise ValueError("negative_margin_frames must be at least 1")
    if not math.isfinite(calibration_end_sec) or calibration_end_sec <= 0.0:
        raise ValueError("calibration_end_sec must be a positive finite number")

    timeline = load_json(timeline_path)
    timestamps = timeline.get("timestamps_sec")
    if not isinstance(timestamps, list) or not timestamps:
        raise ValueError(f"{timeline_path}: timestamps_sec must be a nonempty list")
    masks = _gesture_target_masks(load_json(masks_path))

    raw_events = load_jsonl(events_path)
    selected_events: list[dict[str, Any]] = []
    for event in raw_events:
        event_id = str(event.get("event_id", "")).strip()
        if (
            event.get("event_type") != "implicit_tool_request"
            or event.get("review_status") != "confirmed"
            or event_id not in masks
            or str(event.get("case_id", "")) != case_id
        ):
            continue
        try:
            start_frame = int(event["start_source_frame_idx"])
            end_frame = int(event["end_source_frame_idx"])
            start_sec = float(event["start_sec"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{events_path}: malformed gesture event {event_id}") from exc
        if end_frame < start_frame:
            raise ValueError(f"{events_path}: inverted interval for {event_id}")
        _frame_time(start_frame, timestamps)
        _frame_time(end_frame, timestamps)
        selected_events.append(
            {
                "event_id": event_id,
                "start_frame": start_frame,
                "end_frame": end_frame,
                "start_sec": start_sec,
                "onset_scorable": masks[event_id]["gesture_onset"],
            }
        )
    selected_events.sort(key=lambda item: (item["start_frame"], item["event_id"]))
    if not selected_events:
        raise ValueError(f"No confirmed gesture targets found for case {case_id}")

    positive_intervals = [
        (int(event["start_frame"]), int(event["end_frame"]))
        for event in selected_events
    ]
    samples: list[dict[str, Any]] = []

    for event in selected_events:
        event_id = str(event["event_id"])
        start_frame = int(event["start_frame"])
        end_frame = int(event["end_frame"])
        split = (
            "calibration"
            if float(event["start_sec"]) < calibration_end_sec
            else "within_case_challenge"
        )
        onset_scorable = bool(event["onset_scorable"])

        if onset_scorable:
            samples.append(
                _make_sample(
                    case_id=case_id,
                    split=split,
                    event_id=event_id,
                    frame_index=start_frame,
                    timestamps=timestamps,
                    label="open_receive",
                    sample_kind="positive_onset",
                    onset_scorable=True,
                )
            )
        else:
            # The first available post-gap frame proves presence but cannot prove
            # the physical onset; retain it as a clearly marked diagnostic.
            samples.append(
                _make_sample(
                    case_id=case_id,
                    split=split,
                    event_id=event_id,
                    frame_index=start_frame,
                    timestamps=timestamps,
                    label="open_receive",
                    sample_kind="positive_left_censored_presence",
                    onset_scorable=False,
                )
            )

        midpoint = start_frame + (end_frame - start_frame) // 2
        if midpoint != start_frame:
            samples.append(
                _make_sample(
                    case_id=case_id,
                    split=split,
                    event_id=event_id,
                    frame_index=midpoint,
                    timestamps=timestamps,
                    label="open_receive",
                    sample_kind="positive_interior",
                    onset_scorable=onset_scorable,
                )
            )

        for sample_kind, candidate_index in (
            ("negative_pre_open_boundary", start_frame - negative_margin_frames),
            ("negative_post_contact_boundary", end_frame + negative_margin_frames),
        ):
            direction = -1 if sample_kind == "negative_pre_open_boundary" else 1
            resolved_index = _nearest_nonpositive_boundary_frame(
                candidate_index=candidate_index,
                direction=direction,
                intervals=positive_intervals,
                timestamps=timestamps,
            )
            if resolved_index is None:
                continue
            # A much later frame is not a meaningful local boundary negative.
            # Exclude that one candidate rather than silently turning a remote
            # scene into a pre/post-contact control.
            if abs(resolved_index - candidate_index) > MAX_NEGATIVE_BOUNDARY_SHIFT_FRAMES:
                continue
            negative = _make_sample(
                case_id=case_id,
                split=split,
                event_id=event_id,
                frame_index=resolved_index,
                timestamps=timestamps,
                label="not_open_receive",
                sample_kind=sample_kind,
                onset_scorable=False,
            )
            if resolved_index != candidate_index:
                negative["nominal_boundary_frame_idx"] = candidate_index
                negative["boundary_shift_frames"] = abs(
                    resolved_index - candidate_index
                )
            samples.append(negative)

    samples.sort(key=lambda item: (item["split"], item["frame_idx"], item["sample_id"]))
    return samples


def filter_confirmed_positive_samples(
    samples: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Keep only pre-existing confirmed open-hand samples for visual recall.

    Request-interval boundary controls are not independently labeled visual
    negatives: the hand can remain visibly open just before or after a semantic
    request interval.  This filter does not create or edit labels; it preserves
    only the existing ``open_receive`` samples from a source manifest.
    """

    selected: list[dict[str, Any]] = []
    for sample in samples:
        if sample.get("schema") != SAMPLE_SCHEMA:
            raise ValueError(
                f"unexpected manifest record schema: {sample.get('schema')!r}"
            )
        label = sample.get("label")
        if label not in {"open_receive", "not_open_receive"}:
            raise ValueError(f"unsupported source-manifest label: {label!r}")
        if label == "open_receive":
            selected.append(dict(sample))
    if not selected:
        raise ValueError("source manifest contains no confirmed open-hand samples")
    return selected


def _mime_for_image(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    raise ValueError(f"Unsupported image type for {path}")


def extract_cam4_frame(
    *,
    video_path: Path,
    frame_index: int,
    image_dir: Path,
) -> Path:
    """Extract the known left CAM4 panel from the reviewed two-panel video."""

    if not video_path.is_file():
        raise FileNotFoundError(f"review video does not exist: {video_path}")
    image_dir.mkdir(parents=True, exist_ok=True)
    output_path = image_dir / f"cam4_f{frame_index:04d}.jpg"
    if output_path.exists():
        return output_path
    temporary_path = image_dir / f".{output_path.stem}.{os.getpid()}.tmp.jpg"
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(video_path),
        "-vf",
        f"select=eq(n\\,{frame_index}),{CAM4_PANEL_CROP}",
        "-frames:v",
        "1",
        "-q:v",
        "2",
        "-y",
        str(temporary_path),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
        if not temporary_path.is_file() or temporary_path.stat().st_size == 0:
            raise RuntimeError(f"ffmpeg did not produce CAM4 frame {frame_index}")
        os.replace(temporary_path, output_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return output_path


def extract_cam4_right_detail(
    *,
    full_cam4_path: Path,
    frame_index: int,
    image_dir: Path,
) -> Path:
    """Make a fixed high-resolution right-side detail from the CAM4 image.

    The crop is a camera-layout transform, not a detector crop and not a
    per-label/per-frame ROI. It is identical for every sample in the run.
    """

    output_path = image_dir / f"cam4_right_detail_f{frame_index:04d}.jpg"
    if output_path.exists():
        return output_path
    temporary_path = image_dir / f".{output_path.stem}.{os.getpid()}.tmp.jpg"
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(full_cam4_path),
        "-vf",
        CAM4_RIGHT_DETAIL_CROP,
        "-frames:v",
        "1",
        "-q:v",
        "2",
        "-y",
        str(temporary_path),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
        if not temporary_path.is_file() or temporary_path.stat().st_size == 0:
            raise RuntimeError(
                f"ffmpeg did not produce CAM4 detail for frame {frame_index}"
            )
        os.replace(temporary_path, output_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return output_path


def extract_cam4_causal_detail_pair(
    *,
    video_path: Path,
    frame_index: int,
    image_dir: Path,
) -> Path:
    """Render prior/current fixed details as one causal 640x360 image.

    The left panel is a bounded past observation and the right panel is the
    current frame that the classifier must label. No future frame is supplied.
    """

    prior_index = max(0, frame_index - CAUSAL_PRIOR_FRAMES)
    output_path = image_dir / (
        f"cam4_causal_right_pair_f{frame_index:04d}_prior{prior_index:04d}.jpg"
    )
    if output_path.exists():
        return output_path
    prior_frame = extract_cam4_frame(
        video_path=video_path,
        frame_index=prior_index,
        image_dir=image_dir,
    )
    current_frame = extract_cam4_frame(
        video_path=video_path,
        frame_index=frame_index,
        image_dir=image_dir,
    )
    temporary_path = image_dir / f".{output_path.stem}.{os.getpid()}.tmp.jpg"
    panel_filter = "crop=300:300:340:0,scale=320:320:flags=lanczos,pad=320:360:0:20:black"
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(prior_frame),
        "-i",
        str(current_frame),
        "-filter_complex",
        f"[0:v]{panel_filter}[prior];[1:v]{panel_filter}[current];[prior][current]hstack=inputs=2",
        "-frames:v",
        "1",
        "-q:v",
        "2",
        "-y",
        str(temporary_path),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
        if not temporary_path.is_file() or temporary_path.stat().st_size == 0:
            raise RuntimeError(
                f"ffmpeg did not produce causal CAM4 detail for frame {frame_index}"
            )
        os.replace(temporary_path, output_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return output_path


def extract_cam4_inputs(
    *,
    video_path: Path,
    frame_index: int,
    image_dir: Path,
    input_variant: str,
) -> list[tuple[str, Path]]:
    if input_variant not in INPUT_VARIANTS:
        raise ValueError(
            "input_variant must be one of: " + ", ".join(sorted(INPUT_VARIANTS))
        )
    full_frame = extract_cam4_frame(
        video_path=video_path,
        frame_index=frame_index,
        image_dir=image_dir,
    )
    if input_variant == "full_cam4":
        return [("CAM4 full frame", full_frame)]
    if input_variant == "causal_right_detail_pair":
        return [
            (
                "CAM4 fixed right-side detail pair: prior on left, current on right",
                extract_cam4_causal_detail_pair(
                    video_path=video_path,
                    frame_index=frame_index,
                    image_dir=image_dir,
                ),
            )
        ]
    detail = extract_cam4_right_detail(
        full_cam4_path=full_frame,
        frame_index=frame_index,
        image_dir=image_dir,
    )
    if input_variant == "right_detail_only":
        return [("CAM4 fixed right-side hand detail", detail)]
    return [
        ("CAM4 full frame", full_frame),
        ("CAM4 fixed right-side hand detail (same instant)", detail),
    ]


def build_request_payload(
    *,
    images: Sequence[tuple[str, Path]],
    model_id: str,
    prompt_version: str,
    input_variant: str = "full_cam4",
) -> dict[str, Any]:
    """Build a request with no GT, case, timestamp, event, or runtime context."""

    if input_variant not in INPUT_VARIANTS:
        raise ValueError(
            "input_variant must be one of: " + ", ".join(sorted(INPUT_VARIANTS))
        )
    if not images:
        raise ValueError("at least one CAM4 image is required")
    user_context = {
        "task": TASK_ID,
        "input": {
            "full_cam4": "one full CAM4 still image",
            "right_detail_only": "one fixed right-side detail crop from one CAM4 still image",
            "full_plus_right_detail": "one full CAM4 still image plus a fixed right-side detail from the same instant",
            "causal_right_detail_pair": "one composite image: fixed right-side CAM4 detail from the past on left and current frame on right",
        }[input_variant],
        "authority": "offline_visual_classification_only",
    }
    user_content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": "Task context JSON:\n" + canonical_json(user_context),
        }
    ]
    for label, image_path in images:
        image_bytes = image_path.read_bytes()
        encoded = base64.b64encode(image_bytes).decode("ascii")
        mime_type = _mime_for_image(image_path)
        user_content.extend(
            [
                {"type": "text", "text": "Image label: " + label},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mime_type};base64,{encoded}",
                    },
                },
            ]
        )
    return {
        "model": model_id,
        "temperature": 0.0,
        "top_p": 1.0,
        "max_tokens": 96,
        "reasoning_effort": "none",
        "enable_thinking": False,
        "messages": [
            {"role": "system", "content": get_prompt(prompt_version)},
            {
                "role": "user",
                "content": user_content,
            },
        ],
    }


def _extract_completion_text(response_payload: Mapping[str, Any]) -> str:
    choices = response_payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("response has no choices")
    first = choices[0]
    if not isinstance(first, Mapping):
        raise ValueError("first response choice is not an object")
    message = first.get("message")
    if isinstance(message, Mapping):
        content = message.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = [
                str(item.get("text", ""))
                for item in content
                if isinstance(item, Mapping) and item.get("type") == "text"
            ]
            return "\n".join(texts)
    content = first.get("text")
    if isinstance(content, str):
        return content
    raise ValueError("response did not contain text content")


def request_completion(
    *,
    base_url: str,
    payload: Mapping[str, Any],
    api_key: str,
    timeout_sec: float,
) -> tuple[str, float]:
    url = base_url.rstrip("/") + "/v1/chat/completions"
    data = canonical_json(dict(payload)).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout_sec) as response:
            raw_body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"NInfer HTTP {exc.code}: {body[:400]}") from exc
    elapsed = time.perf_counter() - started
    response_payload = json.loads(raw_body)
    if not isinstance(response_payload, dict):
        raise RuntimeError("NInfer returned a non-object response")
    return _extract_completion_text(response_payload), elapsed


def _extract_first_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3].rstrip()
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("no JSON object found")
        value = json.loads(stripped[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("model output JSON must be an object")
    return value


def _extract_exact_json_value(text: str) -> Any:
    """Read a compact scalar contract without accepting explanatory prose."""

    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3].rstrip()
    return json.loads(stripped)


def parse_prediction(
    raw_text: str, *, prompt_version: str | None = None
) -> ParsedPrediction:
    """Parse only the exact output contract, exposing any format defect."""

    try:
        if prompt_version in MINIMAL_EMPTY_OPEN_HAND_SCALAR_NULLABLE_PROMPT_VERSIONS:
            empty_open_hand = _extract_exact_json_value(raw_text)
            if empty_open_hand is None:
                gesture = "uncertain"
            elif type(empty_open_hand) is bool:
                gesture = "open_receive" if empty_open_hand else "not_open_receive"
            else:
                raise ValueError("scalar empty-open-hand output must be a JSON boolean or null")
            return ParsedPrediction(
                gesture=gesture,
                confidence=1.0,
                visual_evidence="",
            )
        if prompt_version in MINIMAL_EMPTY_OPEN_HAND_NULLABLE_BARE_NULL_FALLBACK_PROMPT_VERSIONS:
            # A bare JSON null is a safe, semantically unambiguous no-trigger
            # fallback.  Do not broaden this exception to bare booleans or
            # strings: those remain object-contract violations.
            scalar_value = _extract_exact_json_value(raw_text)
            if scalar_value is None:
                return ParsedPrediction(
                    gesture="uncertain",
                    confidence=1.0,
                    visual_evidence="",
                )
        value = _extract_first_json_object(raw_text)
        gesture = value.get("gesture")
        if prompt_version in MINIMAL_BINARY_PROMPT_VERSIONS:
            if set(value) != {"gesture"}:
                raise ValueError("minimal binary output must contain only gesture")
            if gesture not in {"open_receive", "not_open_receive"}:
                raise ValueError("minimal binary gesture must be open_receive or not_open_receive")
            return ParsedPrediction(
                gesture=gesture,
                confidence=1.0,
                visual_evidence="",
            )
        if prompt_version in MINIMAL_BOOLEAN_PROMPT_VERSIONS:
            if set(value) != {"open_hand"}:
                raise ValueError("minimal boolean output must contain only open_hand")
            open_hand = value.get("open_hand")
            if type(open_hand) is not bool:
                raise ValueError("open_hand must be a JSON boolean")
            return ParsedPrediction(
                gesture="open_receive" if open_hand else "not_open_receive",
                confidence=1.0,
                visual_evidence="",
            )
        if prompt_version in MINIMAL_EMPTY_OPEN_HAND_PROMPT_VERSIONS:
            if set(value) != {"empty_open_hand"}:
                raise ValueError(
                    "minimal empty-open-hand output must contain only empty_open_hand"
                )
            empty_open_hand = value.get("empty_open_hand")
            if type(empty_open_hand) is not bool:
                raise ValueError("empty_open_hand must be a JSON boolean")
            return ParsedPrediction(
                gesture="open_receive" if empty_open_hand else "not_open_receive",
                confidence=1.0,
                visual_evidence="",
            )
        if prompt_version in MINIMAL_EMPTY_OPEN_HAND_UNCERTAIN_PROMPT_VERSIONS:
            if set(value) != {"empty_open_hand"}:
                raise ValueError(
                    "minimal empty-open-hand-with-uncertain output must contain only empty_open_hand"
                )
            empty_open_hand = value.get("empty_open_hand")
            if empty_open_hand not in {"yes", "no", "uncertain"}:
                raise ValueError(
                    "empty_open_hand must be exactly yes, no, or uncertain"
                )
            return ParsedPrediction(
                gesture={
                    "yes": "open_receive",
                    "no": "not_open_receive",
                    "uncertain": "uncertain",
                }[empty_open_hand],
                confidence=1.0,
                visual_evidence="",
            )
        if prompt_version in (
            MINIMAL_EMPTY_OPEN_HAND_NULLABLE_PROMPT_VERSIONS
            | MINIMAL_EMPTY_OPEN_HAND_NULLABLE_BARE_NULL_FALLBACK_PROMPT_VERSIONS
        ):
            if set(value) != {"empty_open_hand"}:
                raise ValueError(
                    "minimal nullable empty-open-hand output must contain only empty_open_hand"
                )
            empty_open_hand = value.get("empty_open_hand")
            if empty_open_hand is None:
                gesture = "uncertain"
            elif type(empty_open_hand) is bool:
                gesture = "open_receive" if empty_open_hand else "not_open_receive"
            else:
                raise ValueError("empty_open_hand must be a JSON boolean or null")
            return ParsedPrediction(
                gesture=gesture,
                confidence=1.0,
                visual_evidence="",
            )
        if not isinstance(gesture, str) or gesture not in ALLOWED_GESTURES:
            raise ValueError("gesture must be one of the exact contract enums")
        confidence = float(value.get("confidence"))
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be a finite number in [0, 1]")
        evidence = value.get("visual_evidence", "")
        if not isinstance(evidence, str):
            raise ValueError("visual_evidence must be a string")
        evidence = " ".join(evidence.split())
        if len(evidence.split()) > 24:
            raise ValueError("visual_evidence exceeds 24 words")
        return ParsedPrediction(
            gesture=gesture,
            confidence=round(confidence, 6),
            visual_evidence=evidence,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return ParsedPrediction(
            gesture="uncertain",
            confidence=0.0,
            visual_evidence="",
            parse_error=str(exc),
        )


def run_manifest(
    *,
    manifest_path: Path,
    video_path: Path,
    image_dir: Path,
    base_url: str,
    model_id: str,
    prompt_version: str,
    api_key: str,
    timeout_sec: float,
    split: str,
    case_id: str | None,
    input_variant: str,
    offset: int,
    limit: int | None,
) -> list[dict[str, Any]]:
    if prompt_version not in PROMPTS:
        get_prompt(prompt_version)
    if input_variant not in INPUT_VARIANTS:
        raise ValueError(
            "input_variant must be one of: " + ", ".join(sorted(INPUT_VARIANTS))
        )
    if timeout_sec <= 0.0:
        raise ValueError("timeout_sec must be positive")
    if split not in {"all", "calibration", "within_case_challenge"}:
        raise ValueError("split must be all, calibration, or within_case_challenge")
    if offset < 0:
        raise ValueError("offset must be zero or greater")
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive when supplied")

    samples = load_jsonl(manifest_path)
    selected = [
        sample
        for sample in samples
        if split == "all" or sample.get("split") == split
    ]
    if case_id:
        selected = [sample for sample in selected if sample.get("case_id") == case_id]
    selected = selected[offset:]
    if limit is not None:
        selected = selected[:limit]
    if not selected:
        raise ValueError("no samples selected from manifest")

    records: list[dict[str, Any]] = []
    for sample in selected:
        if sample.get("schema") != SAMPLE_SCHEMA:
            raise ValueError(f"unexpected manifest record schema: {sample.get('schema')!r}")
        frame_index = int(sample["frame_idx"])
        images = extract_cam4_inputs(
            video_path=video_path,
            frame_index=frame_index,
            image_dir=image_dir,
            input_variant=input_variant,
        )
        payload = build_request_payload(
            images=images,
            model_id=model_id,
            prompt_version=prompt_version,
            input_variant=input_variant,
        )
        raw_text = ""
        latency_sec: float | None = None
        transport_error = ""
        try:
            raw_text, latency_sec = request_completion(
                base_url=base_url,
                payload=payload,
                api_key=api_key,
                timeout_sec=timeout_sec,
            )
        except (OSError, RuntimeError, json.JSONDecodeError, urllib.error.URLError) as exc:
            transport_error = str(exc)
        parsed = parse_prediction(
            raw_text, prompt_version=prompt_version
        ) if raw_text else ParsedPrediction(
            gesture="uncertain",
            confidence=0.0,
            visual_evidence="",
            parse_error="no model response",
        )
        records.append(
            {
                "schema": PREDICTION_SCHEMA,
                "sample": sample,
                "prompt_version": prompt_version,
                "model_id": model_id,
                "image_sha256": {
                    label: sha256_file(image_path) for label, image_path in images
                },
                "input_policy": {
                    "full_cam4": "CAM4-only full panel; no labels, case, timestamp, or context supplied",
                    "right_detail_only": "fixed right-side CAM4 detail only; no labels, case, timestamp, or context supplied",
                    "full_plus_right_detail": "CAM4 full panel plus fixed right-side detail; no labels, case, timestamp, or context supplied",
                    "causal_right_detail_pair": "one causal CAM4 composite with fixed right-side prior/current details; no labels, case, timestamp, or context supplied",
                }[input_variant],
                "output_contract": (
                    "minimal_boolean_open_hand"
                    if prompt_version in MINIMAL_BOOLEAN_PROMPT_VERSIONS
                    else (
                        "minimal_boolean_empty_open_hand"
                        if prompt_version in MINIMAL_EMPTY_OPEN_HAND_PROMPT_VERSIONS
                        else (
                            "minimal_enum_empty_open_hand_with_uncertain"
                            if prompt_version
                            in MINIMAL_EMPTY_OPEN_HAND_UNCERTAIN_PROMPT_VERSIONS
                            else (
                                "minimal_nullable_boolean_empty_open_hand"
                                if prompt_version
                                in MINIMAL_EMPTY_OPEN_HAND_NULLABLE_PROMPT_VERSIONS
                                else (
                                    "minimal_nullable_boolean_empty_open_hand_scalar"
                                    if prompt_version
                                    in MINIMAL_EMPTY_OPEN_HAND_SCALAR_NULLABLE_PROMPT_VERSIONS
                                    else (
                                        "minimal_nullable_empty_open_hand_object_or_bare_null_fallback"
                                        if prompt_version
                                        in MINIMAL_EMPTY_OPEN_HAND_NULLABLE_BARE_NULL_FALLBACK_PROMPT_VERSIONS
                                        else (
                                            "minimal_binary_gesture"
                                            if prompt_version in MINIMAL_BINARY_PROMPT_VERSIONS
                                            else "gesture_confidence_evidence"
                                        )
                                    )
                                )
                            )
                        )
                    )
                ),
                "raw_model_text": raw_text,
                "prediction": {
                    "gesture": parsed.gesture,
                    "confidence": parsed.confidence,
                    "visual_evidence": parsed.visual_evidence,
                    "parse_error": parsed.parse_error,
                },
                "latency_sec": None if latency_sec is None else round(latency_sec, 6),
                "transport_error": transport_error,
                "ground_truth_usage": "evaluation_only",
                "may_publish_runtime": False,
            }
        )
    return records


def _prediction_positive(record: Mapping[str, Any], threshold: float) -> bool:
    prediction = record.get("prediction", {})
    if not isinstance(prediction, Mapping):
        return False
    try:
        confidence = float(prediction.get("confidence", 0.0))
    except (TypeError, ValueError):
        return False
    return prediction.get("gesture") == "open_receive" and confidence >= threshold


def _safe_ratio(numerator: int, denominator: int) -> float:
    return 0.0 if denominator == 0 else numerator / denominator


def score_records(
    records: Sequence[Mapping[str, Any]],
    *,
    threshold: float,
) -> dict[str, Any]:
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("threshold must be in [0, 1]")
    tp = fp = tn = fn = 0
    parse_failures = transport_failures = uncertain_predictions = 0
    onset_total = onset_detected = 0
    by_kind: dict[str, list[Mapping[str, Any]]] = {}
    for record in records:
        sample = record.get("sample", {})
        if not isinstance(sample, Mapping):
            raise ValueError("prediction record has no sample object")
        label = sample.get("label")
        if label not in {"open_receive", "not_open_receive"}:
            raise ValueError(f"unsupported label in prediction record: {label!r}")
        predicted_positive = _prediction_positive(record, threshold)
        actual_positive = label == "open_receive"
        if actual_positive and predicted_positive:
            tp += 1
        elif actual_positive:
            fn += 1
        elif predicted_positive:
            fp += 1
        else:
            tn += 1
        prediction = record.get("prediction", {})
        if isinstance(prediction, Mapping):
            if prediction.get("parse_error"):
                parse_failures += 1
            if prediction.get("gesture") == "uncertain":
                uncertain_predictions += 1
        if record.get("transport_error"):
            transport_failures += 1
        kind = str(sample.get("sample_kind", "unknown"))
        by_kind.setdefault(kind, []).append(record)
        if kind == "positive_onset":
            onset_total += 1
            onset_detected += int(predicted_positive)

    precision = _safe_ratio(tp, tp + fp)
    recall = _safe_ratio(tp, tp + fn)
    specificity = _safe_ratio(tn, tn + fp)
    f1 = _safe_ratio(2 * precision * recall, precision + recall)
    return {
        "sample_count": len(records),
        "threshold": threshold,
        "confusion_matrix": {"tp": tp, "fp": fp, "tn": tn, "fn": fn},
        "accuracy": round(_safe_ratio(tp + tn, tp + fp + tn + fn), 6),
        "precision": round(precision, 6),
        "recall": round(recall, 6),
        "specificity": round(specificity, 6),
        "balanced_accuracy": round((recall + specificity) / 2.0, 6),
        "f1": round(f1, 6),
        "onset_recall": round(_safe_ratio(onset_detected, onset_total), 6),
        "onset_sample_count": onset_total,
        "format_failure_count": parse_failures,
        "transport_failure_count": transport_failures,
        "uncertain_prediction_count": uncertain_predictions,
        "sample_kinds": {
            kind: len(items) for kind, items in sorted(by_kind.items())
        },
    }


def score_confirmed_positive_records(
    records: Sequence[Mapping[str, Any]], *, threshold: float = 1.0
) -> dict[str, Any]:
    """Score recall on existing confirmed open-hand samples only.

    This is the appropriate metric when the policy is visual-pose-triggered
    and the available out-of-interval frames are not independently adjudicated
    visual negatives.  It deliberately does not manufacture an accuracy,
    specificity, or false-positive rate from those frames.
    """

    if not records:
        raise ValueError("cannot score an empty positive-only record set")
    detected = format_failures = transport_failures = uncertain_predictions = 0
    by_kind: dict[str, list[Mapping[str, Any]]] = {}
    for record in records:
        sample = record.get("sample", {})
        if not isinstance(sample, Mapping) or sample.get("label") != "open_receive":
            raise ValueError("positive-only report received a non-positive sample")
        detected += int(_prediction_positive(record, threshold))
        prediction = record.get("prediction", {})
        if isinstance(prediction, Mapping):
            format_failures += int(bool(prediction.get("parse_error")))
            uncertain_predictions += int(prediction.get("gesture") == "uncertain")
        transport_failures += int(bool(record.get("transport_error")))
        by_kind.setdefault(str(sample.get("sample_kind", "unknown")), []).append(record)
    total = len(records)
    return {
        "sample_count": total,
        "threshold": threshold,
        "detected_open_hand_count": detected,
        "missed_open_hand_count": total - detected,
        "positive_recall": round(_safe_ratio(detected, total), 6),
        "format_failure_count": format_failures,
        "transport_failure_count": transport_failures,
        "uncertain_prediction_count": uncertain_predictions,
        "sample_kinds": {kind: len(items) for kind, items in sorted(by_kind.items())},
    }


def select_threshold(calibration_records: Sequence[Mapping[str, Any]]) -> float:
    """Select a conservative threshold on calibration data only.

    Balanced accuracy is primary; F1, precision, and then the higher threshold
    break ties.  The latter makes a tie fail more safely in a future shadow path.
    """

    if not calibration_records:
        raise ValueError("cannot select a threshold without calibration records")
    thresholds = {0.0, 0.35, 0.50, 0.65, 0.75, 0.85, 0.95, 1.0}
    for record in calibration_records:
        prediction = record.get("prediction", {})
        if isinstance(prediction, Mapping) and prediction.get("gesture") == "open_receive":
            try:
                confidence = float(prediction.get("confidence"))
            except (TypeError, ValueError):
                continue
            if math.isfinite(confidence):
                thresholds.add(max(0.0, min(1.0, confidence)))
    ranked: list[tuple[tuple[float, float, float, float], float]] = []
    for threshold in sorted(thresholds):
        metrics = score_records(calibration_records, threshold=threshold)
        key = (
            float(metrics["balanced_accuracy"]),
            float(metrics["f1"]),
            float(metrics["precision"]),
            threshold,
        )
        ranked.append((key, threshold))
    return max(ranked, key=lambda item: item[0])[1]


def _validate_execution_records(execution_paths: Sequence[Path]) -> None:
    """Reject a report when its supplied runner evidence is partial or halted.

    Prediction JSONL rows are intentionally persisted before a runtime failure
    so the raw transport evidence can be inspected.  They must not, however,
    be accidentally turned into a metric.  Callers that used the guarded batch
    runner provide its execution files here and get an explicit integrity gate.
    """

    for path in execution_paths:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read execution evidence {path}: {exc}") from exc
        if not isinstance(value, Mapping):
            raise ValueError(f"execution evidence is not an object: {path}")
        if value.get("schema") != "taskplanner.gesture_prompt_eval_execution.v1":
            raise ValueError(f"unexpected execution schema: {path}")
        if value.get("status", "completed") != "completed" or not bool(
            value.get("scoreable", True)
        ):
            raise ValueError(
                f"refusing to score halted or non-scoreable execution: {path}"
            )
        if int(value.get("transport_failure_count", 0)) != 0:
            raise ValueError(f"refusing to score transport-failed execution: {path}")


def score_prediction_files(
    predictions_paths: Sequence[Path], *, execution_paths: Sequence[Path] = ()
) -> dict[str, Any]:
    if not predictions_paths:
        raise ValueError("at least one prediction file is required")
    _validate_execution_records(execution_paths)
    records = [
        record
        for predictions_path in predictions_paths
        for record in load_jsonl(predictions_path)
    ]
    calibration = [
        record
        for record in records
        if isinstance(record.get("sample"), dict)
        and record["sample"].get("split") == "calibration"
    ]
    challenge = [
        record
        for record in records
        if isinstance(record.get("sample"), dict)
        and record["sample"].get("split") == "within_case_challenge"
    ]
    threshold = select_threshold(calibration)
    prompt_versions = sorted({str(record.get("prompt_version", "")) for record in records})
    model_ids = sorted({str(record.get("model_id", "")) for record in records})
    input_policies = sorted({str(record.get("input_policy", "")) for record in records})
    groups = sorted(
        {
            str(record["sample"].get("evaluation_group"))
            for record in records
            if isinstance(record.get("sample"), dict)
            and record["sample"].get("evaluation_group")
        }
    )
    cases = sorted(
        {
            str(record["sample"].get("case_id"))
            for record in records
            if isinstance(record.get("sample"), dict)
            and record["sample"].get("case_id")
        }
    )
    grouped_metrics = {
        group: score_records(
            [
                record
                for record in records
                if isinstance(record.get("sample"), dict)
                and record["sample"].get("evaluation_group") == group
            ],
            threshold=threshold,
        )
        for group in groups
    }
    holdout_records = [
        record
        for record in records
        if isinstance(record.get("sample"), dict)
        and str(record["sample"].get("evaluation_group", "")).startswith(
            "case_holdout_"
        )
    ]
    scope: dict[str, Any]
    if len(cases) > 1:
        scope = {
            "cases": cases,
            "classification": (
                "development temporal calibration/challenge plus frozen "
                "case-level holdout"
            ),
            "generalization_claim": (
                "case_holdout metrics are the only cross-case estimate; all "
                "results remain internal 0704 evaluation"
            ),
        }
    else:
        scope = {
            "case": DEFAULT_CASE_ID,
            "classification": "development_calibration plus time-separated within-case challenge",
            "generalization_claim": "not permitted; no separately labeled gesture case is available",
        }
    return {
        "schema": REPORT_SCHEMA,
        "task": TASK_ID,
        "ground_truth_usage": "evaluation_only",
        "may_publish_runtime": False,
        "reference_authority": SOURCE_REFERENCE_AUTHORITY,
        "metric_interpretation": SOURCE_METRIC_INTERPRETATION,
        "scope": scope,
        "prompt_versions": prompt_versions,
        "model_ids": model_ids,
        "input_policies": input_policies,
        "selected_threshold": threshold,
        "threshold_selection_split": "calibration",
        "metrics": {
            "calibration": score_records(calibration, threshold=threshold),
            "within_case_challenge": score_records(challenge, threshold=threshold),
            "by_evaluation_group": grouped_metrics,
            "case_holdout_all": score_records(holdout_records, threshold=threshold),
            "all_selected_samples": score_records(records, threshold=threshold),
        },
    }


def score_confirmed_positive_prediction_files(
    predictions_paths: Sequence[Path], *, execution_paths: Sequence[Path] = ()
) -> dict[str, Any]:
    """Build a visual-positive-only report from complete runner evidence."""

    if not predictions_paths:
        raise ValueError("at least one prediction file is required")
    _validate_execution_records(execution_paths)
    records = [
        record
        for predictions_path in predictions_paths
        for record in load_jsonl(predictions_path)
    ]
    threshold = 1.0
    groups = sorted(
        {
            str(record["sample"].get("evaluation_group"))
            for record in records
            if isinstance(record.get("sample"), dict)
            and record["sample"].get("evaluation_group")
        }
    )
    by_group = {
        group: score_confirmed_positive_records(
            [
                record
                for record in records
                if isinstance(record.get("sample"), dict)
                and record["sample"].get("evaluation_group") == group
            ],
            threshold=threshold,
        )
        for group in groups
    }
    return {
        "schema": REPORT_SCHEMA,
        "task": TASK_ID,
        "ground_truth_usage": "evaluation_only",
        "may_publish_runtime": False,
        "reference_authority": SOURCE_REFERENCE_AUTHORITY,
        "metric_interpretation": (
            "confirmed-open-hand positive recall only; no visual-negative metric "
            "is claimed from semantic request-interval boundary controls"
        ),
        "evaluation_protocol": "existing_confirmed_positive_samples_only",
        "prompt_versions": sorted(
            {str(record.get("prompt_version", "")) for record in records}
        ),
        "model_ids": sorted({str(record.get("model_id", "")) for record in records}),
        "input_policies": sorted(
            {str(record.get("input_policy", "")) for record in records}
        ),
        "threshold": threshold,
        "by_evaluation_group": by_group,
        "all_confirmed_positive_samples": score_confirmed_positive_records(
            records, threshold=threshold
        ),
    }


def _default_events_path(case_id: str) -> Path:
    return (
        REPOSITORY_ROOT
        / "annotations/observable_tool_events/cases"
        / case_id
        / "interaction_events.observed.final.v6.jsonl"
    )


def _default_masks_path(case_id: str) -> Path:
    return (
        REPOSITORY_ROOT
        / "annotations/observable_tool_events/cases"
        / case_id
        / "evaluation_masks.v2.json"
    )


def _default_timeline_path(case_id: str) -> Path:
    return (
        REPOSITORY_ROOT
        / "annotations/observable_tool_events/cases"
        / case_id
        / "cam4_frame_timeline.v1.json"
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build-manifest")
    build.add_argument("--case", default=DEFAULT_CASE_ID)
    build.add_argument("--events", type=Path)
    build.add_argument("--masks", type=Path)
    build.add_argument("--timeline", type=Path)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument(
        "--calibration-end-sec",
        type=float,
        default=DEFAULT_CALIBRATION_END_SEC,
    )
    build.add_argument(
        "--negative-margin-frames",
        type=int,
        default=DEFAULT_NEGATIVE_MARGIN_FRAMES,
    )
    build.add_argument("--force", action="store_true")

    build_multi = subparsers.add_parser("build-multicase-manifest")
    build_multi.add_argument(
        "--cases",
        nargs="+",
        default=list(DEFAULT_AUDITED_CASE_IDS),
        help="cases to audit; incomplete references are reported and excluded",
    )
    build_multi.add_argument(
        "--development-cases",
        nargs="+",
        default=list(DEFAULT_DEVELOPMENT_CASE_IDS),
        help="cases eligible for prompt/threshold selection",
    )
    build_multi.add_argument(
        "--calibration-event-fraction",
        type=float,
        default=DEFAULT_CALIBRATION_EVENT_FRACTION,
    )
    build_multi.add_argument(
        "--negative-margin-frames",
        type=int,
        default=DEFAULT_NEGATIVE_MARGIN_FRAMES,
    )
    build_multi.add_argument("--output", type=Path, required=True)
    build_multi.add_argument("--coverage-report", type=Path, required=True)
    build_multi.add_argument("--force", action="store_true")

    positive_only = subparsers.add_parser("filter-confirmed-positive-manifest")
    positive_only.add_argument("--manifest", type=Path, required=True)
    positive_only.add_argument("--output", type=Path, required=True)
    positive_only.add_argument("--force", action="store_true")

    run = subparsers.add_parser("run")
    run.add_argument("--manifest", type=Path, required=True)
    run.add_argument("--video", type=Path, required=True)
    run.add_argument(
        "--case",
        help="optional manifest case filter; evaluation metadata is never sent to NInfer",
    )
    run.add_argument("--image-dir", type=Path, required=True)
    run.add_argument("--predictions", type=Path, required=True)
    run.add_argument("--prompt-version", choices=sorted(PROMPTS), required=True)
    run.add_argument(
        "--input-variant",
        choices=sorted(INPUT_VARIANTS),
        default="full_cam4",
    )
    run.add_argument("--base-url", default=DEFAULT_BASE_URL)
    run.add_argument("--model-id", default=DEFAULT_MODEL_ID)
    run.add_argument("--api-key-env", default="NINFER_API_KEY")
    run.add_argument("--timeout-sec", type=float, default=180.0)
    run.add_argument(
        "--split",
        choices=("all", "calibration", "within_case_challenge"),
        default="all",
    )
    run.add_argument("--offset", type=int, default=0)
    run.add_argument("--limit", type=int)
    run.add_argument("--force", action="store_true")

    score = subparsers.add_parser("score")
    score.add_argument("--predictions", type=Path, action="append", required=True)
    score.add_argument(
        "--execution",
        type=Path,
        action="append",
        default=[],
        help=(
            "Optional guarded-run execution evidence; every supplied run must be "
            "completed, scoreable, and transport-error free."
        ),
    )
    score.add_argument("--report", type=Path, required=True)
    score.add_argument("--force", action="store_true")

    score_positive = subparsers.add_parser("score-confirmed-positive")
    score_positive.add_argument("--predictions", type=Path, action="append", required=True)
    score_positive.add_argument("--execution", type=Path, action="append", default=[])
    score_positive.add_argument("--report", type=Path, required=True)
    score_positive.add_argument("--force", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "build-manifest":
        events = args.events or _default_events_path(args.case)
        masks = args.masks or _default_masks_path(args.case)
        timeline = args.timeline or _default_timeline_path(args.case)
        samples = build_manifest(
            events_path=events,
            masks_path=masks,
            timeline_path=timeline,
            case_id=args.case,
            calibration_end_sec=args.calibration_end_sec,
            negative_margin_frames=args.negative_margin_frames,
        )
        write_jsonl(args.output, samples, overwrite=args.force)
        print(canonical_json({"manifest": str(args.output), "sample_count": len(samples)}))
        return 0
    if args.command == "build-multicase-manifest":
        samples, coverage = build_multicase_manifest(
            case_ids=args.cases,
            development_case_ids=args.development_cases,
            calibration_event_fraction=args.calibration_event_fraction,
            negative_margin_frames=args.negative_margin_frames,
        )
        write_jsonl(args.output, samples, overwrite=args.force)
        write_json(args.coverage_report, coverage, overwrite=args.force)
        print(
            canonical_json(
                {
                    "manifest": str(args.output),
                    "coverage_report": str(args.coverage_report),
                    "sample_count": len(samples),
                    "included_cases": len(coverage["included_cases"]),
                    "excluded_cases": len(coverage["excluded_cases"]),
                }
            )
        )
        return 0
    if args.command == "filter-confirmed-positive-manifest":
        samples = filter_confirmed_positive_samples(load_jsonl(args.manifest))
        write_jsonl(args.output, samples, overwrite=args.force)
        print(canonical_json({"manifest": str(args.output), "sample_count": len(samples)}))
        return 0
    if args.command == "run":
        api_key = os.environ.get(args.api_key_env, "")
        records = run_manifest(
            manifest_path=args.manifest,
            video_path=args.video,
            image_dir=args.image_dir,
            base_url=args.base_url,
            model_id=args.model_id,
            prompt_version=args.prompt_version,
            api_key=api_key,
            timeout_sec=args.timeout_sec,
            split=args.split,
            case_id=args.case,
            input_variant=args.input_variant,
            offset=args.offset,
            limit=args.limit,
        )
        write_jsonl(args.predictions, records, overwrite=args.force)
        print(canonical_json({"predictions": str(args.predictions), "sample_count": len(records)}))
        return 0
    if args.command == "score":
        report = score_prediction_files(args.predictions, execution_paths=args.execution)
        write_json(args.report, report, overwrite=args.force)
        print(canonical_json({"report": str(args.report), "selected_threshold": report["selected_threshold"]}))
        return 0
    if args.command == "score-confirmed-positive":
        report = score_confirmed_positive_prediction_files(
            args.predictions, execution_paths=args.execution
        )
        write_json(args.report, report, overwrite=args.force)
        print(canonical_json({"report": str(args.report), "positive_recall": report["all_confirmed_positive_samples"]["positive_recall"]}))
        return 0
    raise AssertionError(f"Unhandled command {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
