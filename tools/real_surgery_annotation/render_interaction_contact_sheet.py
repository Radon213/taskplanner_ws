#!/usr/bin/env python3
"""Render an exact-frame, multi-view interaction review contact sheet.

The authoritative coordinate is a CAM4 frame index from
``taskplanner.video_frame_timeline.v1``.  Every supplied AVI is decoded at the
same selected frame indices; corrected bag timestamps are used only to choose
and label those indices.  The generated image is a read-only review aid, never
an annotation or ground-truth artifact.
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from PIL import Image, ImageDraw, ImageFont

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from tools.real_surgery_annotation.validate_interaction_points import (
    validate_timeline,
)


TIMELINE_SCHEMA = "taskplanner.video_frame_timeline.v1"
VIEW_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
SUPPORTED_OUTPUT_SUFFIXES = {".png", ".jpg", ".jpeg"}

TILE_WIDTH = 400
FRAME_HEIGHT = 240
LABEL_HEIGHT = 44
HEADER_HEIGHT = 76
GUTTER = 8
SHEET_MARGIN = 12


class ContactSheetError(RuntimeError):
    """A review packet input or exact-frame decode failed validation."""


@dataclass(frozen=True)
class Timeline:
    case_id: str
    source_fps: float
    timestamps_sec: tuple[float, ...]

    @property
    def frame_count(self) -> int:
        return len(self.timestamps_sec)


@dataclass(frozen=True)
class ViewSpec:
    label: str
    path: Path


@dataclass(frozen=True)
class SamplePlan:
    frame_indices: tuple[int, ...]
    center_frame: int | None
    selection_mode: str


def _reject_nonstandard_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON numeric constant: {value}")


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContactSheetError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ContactSheetError(f"{label} must be finite")
    return result


def load_timeline(path: Path, *, case_id: str) -> Timeline:
    """Load and fully validate the corrected frame-to-bag-time mapping."""

    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=_reject_nonstandard_json_constant,
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise ContactSheetError(f"cannot read timeline {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContactSheetError(f"timeline must be a JSON object: {path}")
    timestamps, errors = validate_timeline(value, case_id=case_id)
    if errors:
        raise ContactSheetError(
            "invalid corrected timeline:\n" + "\n".join(errors)
        )
    return Timeline(
        case_id=case_id,
        source_fps=float(value["source_fps"]),
        timestamps_sec=tuple(timestamps),
    )


def nearest_frame_index(timestamps_sec: Sequence[float], target_sec: float) -> int:
    """Map corrected bag time to the nearest frame, preferring the earlier tie."""

    if not timestamps_sec:
        raise ContactSheetError("timeline has no timestamps")
    target = _finite_number(target_sec, "center bag time")
    index = bisect.bisect_left(timestamps_sec, target)
    if index <= 0:
        return 0
    if index >= len(timestamps_sec):
        return len(timestamps_sec) - 1
    before = index - 1
    return (
        before
        if abs(timestamps_sec[before] - target)
        <= abs(timestamps_sec[index] - target)
        else index
    )


def _all_or_none(values: Sequence[Any]) -> bool:
    return all(value is None for value in values) or all(
        value is not None for value in values
    )


def _validate_frame_index(value: Any, *, label: str, frame_count: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value < frame_count
    ):
        raise ContactSheetError(
            f"{label} must be an integer in [0, {frame_count - 1}]"
        )
    return value


def _time_targets(
    *,
    center_sec: float,
    before_sec: float,
    after_sec: float,
    step_sec: float,
    timeline_start: float,
    timeline_end: float,
) -> list[float]:
    before = _finite_number(before_sec, "before-sec")
    after = _finite_number(after_sec, "after-sec")
    step = _finite_number(step_sec, "step-sec")
    if before < 0 or after < 0:
        raise ContactSheetError("before-sec and after-sec must be non-negative")
    if step <= 0:
        raise ContactSheetError("step-sec must be positive")

    start = max(timeline_start, center_sec - before)
    end = min(timeline_end, center_sec + after)
    if start > end:
        raise ContactSheetError("time window does not overlap the timeline")

    targets: list[float] = []
    cursor = start
    while cursor <= end + 1e-12:
        targets.append(min(cursor, end))
        cursor += step
    if not targets or abs(targets[-1] - end) > 1e-12:
        targets.append(end)
    if start <= center_sec <= end:
        targets.append(center_sec)
    return sorted(set(targets))


def select_sample_plan(
    timeline: Timeline,
    *,
    center_bag_sec: float | None = None,
    center_frame: int | None = None,
    before_sec: float | None = None,
    after_sec: float | None = None,
    step_sec: float | None = None,
    first_frame: int | None = None,
    last_frame: int | None = None,
    frame_step: int | None = None,
) -> SamplePlan:
    """Resolve one valid time-window or explicit-frame sampling specification."""

    time_window = (before_sec, after_sec, step_sec)
    frame_window = (first_frame, last_frame, frame_step)
    if not _all_or_none(time_window):
        raise ContactSheetError(
            "--before-sec, --after-sec, and --step-sec must be supplied together"
        )
    if not _all_or_none(frame_window):
        raise ContactSheetError(
            "--first-frame, --last-frame, and --frame-step must be supplied together"
        )
    has_time_window = before_sec is not None
    has_frame_window = first_frame is not None
    has_center = center_bag_sec is not None or center_frame is not None

    if has_time_window and has_frame_window:
        raise ContactSheetError("time-window and frame-window modes are exclusive")
    if has_frame_window and has_center:
        raise ContactSheetError(
            "explicit frame-window mode cannot be combined with a center"
        )
    if not has_frame_window:
        if center_bag_sec is not None and center_frame is not None:
            raise ContactSheetError(
                "--center-bag-sec and --center-frame are mutually exclusive"
            )
        if not has_center:
            raise ContactSheetError(
                "time-window mode requires --center-bag-sec or --center-frame"
            )
        if not has_time_window:
            raise ContactSheetError(
                "center mode requires --before-sec, --after-sec, and --step-sec"
            )

    if has_frame_window:
        assert first_frame is not None
        assert last_frame is not None
        assert frame_step is not None
        first = _validate_frame_index(
            first_frame,
            label="first-frame",
            frame_count=timeline.frame_count,
        )
        last = _validate_frame_index(
            last_frame,
            label="last-frame",
            frame_count=timeline.frame_count,
        )
        if (
            isinstance(frame_step, bool)
            or not isinstance(frame_step, int)
            or frame_step <= 0
        ):
            raise ContactSheetError("frame-step must be a positive integer")
        if first > last:
            raise ContactSheetError("first-frame must not exceed last-frame")
        indices = list(range(first, last + 1, frame_step))
        if indices[-1] != last:
            indices.append(last)
        return SamplePlan(
            frame_indices=tuple(indices),
            center_frame=None,
            selection_mode="explicit_frame_window",
        )

    if center_frame is not None:
        canonical_center = _validate_frame_index(
            center_frame,
            label="center-frame",
            frame_count=timeline.frame_count,
        )
        center_sec = timeline.timestamps_sec[canonical_center]
    else:
        assert center_bag_sec is not None
        requested_center = _finite_number(
            center_bag_sec,
            "center-bag-sec",
        )
        if not (
            timeline.timestamps_sec[0]
            <= requested_center
            <= timeline.timestamps_sec[-1]
        ):
            raise ContactSheetError("center-bag-sec is outside the timeline")
        canonical_center = nearest_frame_index(
            timeline.timestamps_sec,
            requested_center,
        )
        # Sampling stays centered on the requested corrected bag time while the
        # highlighted center is the exact nearest canonical frame.
        center_sec = requested_center

    assert before_sec is not None
    assert after_sec is not None
    assert step_sec is not None
    targets = _time_targets(
        center_sec=center_sec,
        before_sec=before_sec,
        after_sec=after_sec,
        step_sec=step_sec,
        timeline_start=timeline.timestamps_sec[0],
        timeline_end=timeline.timestamps_sec[-1],
    )
    indices = {
        nearest_frame_index(timeline.timestamps_sec, target)
        for target in targets
    }
    indices.add(canonical_center)
    return SamplePlan(
        frame_indices=tuple(sorted(indices)),
        center_frame=canonical_center,
        selection_mode="corrected_bag_time_window",
    )


def parse_view_specs(values: Sequence[str]) -> tuple[ViewSpec, ...]:
    """Parse unique ``LABEL=AVI`` arguments without accepting missing files."""

    if not values:
        raise ContactSheetError("at least one --view LABEL=AVI is required")
    specs: list[ViewSpec] = []
    labels: set[str] = set()
    paths: set[Path] = set()
    for raw in values:
        if "=" not in raw:
            raise ContactSheetError(f"invalid --view {raw!r}; expected LABEL=AVI")
        label, path_text = raw.split("=", 1)
        label = label.strip()
        path_text = path_text.strip()
        if VIEW_LABEL_PATTERN.fullmatch(label) is None:
            raise ContactSheetError(
                f"invalid view label {label!r}; use letters, numbers, '_' or '-'"
            )
        if label in labels:
            raise ContactSheetError(f"duplicate view label: {label}")
        if not path_text:
            raise ContactSheetError(f"view {label} has an empty AVI path")
        path = Path(path_text).expanduser().resolve()
        if not path.is_file():
            raise ContactSheetError(f"view video does not exist: {path}")
        if path in paths:
            raise ContactSheetError(
                f"the same video path was supplied more than once: {path}"
            )
        labels.add(label)
        paths.add(path)
        specs.append(ViewSpec(label=label, path=path))
    return tuple(specs)


def decode_exact_frames(
    view: ViewSpec,
    frame_indices: Sequence[int],
    *,
    cv2_module: Any | None = None,
) -> tuple[dict[int, Image.Image], dict[str, Any]]:
    """Decode exact indices after verifying decoder position and frame count.

    One initial seek is followed by sequential decoding.  This avoids
    keyframe-dependent random seeks for every sample while making the decoder's
    reported frame position checkable after every read.
    """

    if not frame_indices:
        raise ContactSheetError("no frame indices were selected")
    if any(
        isinstance(index, bool) or not isinstance(index, int) or index < 0
        for index in frame_indices
    ):
        raise ContactSheetError("selected frame indices must be non-negative integers")
    ordered = tuple(sorted(set(frame_indices)))

    if cv2_module is None:
        try:
            import cv2 as cv2_module
        except ImportError as exc:
            raise ContactSheetError("OpenCV is required to decode AVI files") from exc

    capture = cv2_module.VideoCapture(str(view.path))
    if not capture.isOpened():
        capture.release()
        raise ContactSheetError(f"{view.label}: cannot open video {view.path}")
    try:
        reported_count_value = capture.get(cv2_module.CAP_PROP_FRAME_COUNT)
        if not math.isfinite(reported_count_value):
            raise ContactSheetError(
                f"{view.label}: decoder reported non-finite frame count"
            )
        reported_count = int(round(reported_count_value))
        if reported_count <= ordered[-1]:
            raise ContactSheetError(
                f"{view.label}: video has {reported_count} frames but "
                f"frame {ordered[-1]} was requested"
            )

        first = ordered[0]
        if not capture.set(cv2_module.CAP_PROP_POS_FRAMES, float(first)):
            raise ContactSheetError(
                f"{view.label}: decoder refused exact seek to frame {first}"
            )
        seek_position = capture.get(cv2_module.CAP_PROP_POS_FRAMES)
        if (
            not math.isfinite(seek_position)
            or abs(seek_position - first) > 0.25
        ):
            raise ContactSheetError(
                f"{view.label}: exact seek verification failed for frame "
                f"{first}; decoder reports {seek_position}"
            )

        requested = set(ordered)
        decoded: dict[int, Image.Image] = {}
        for frame_index in range(first, ordered[-1] + 1):
            ok, frame = capture.read()
            if not ok or frame is None:
                raise ContactSheetError(
                    f"{view.label}: failed to decode exact frame {frame_index}"
                )
            next_position = capture.get(cv2_module.CAP_PROP_POS_FRAMES)
            if (
                not math.isfinite(next_position)
                or abs(next_position - (frame_index + 1)) > 0.25
            ):
                raise ContactSheetError(
                    f"{view.label}: decoder position drift after frame "
                    f"{frame_index}; reports {next_position}"
                )
            if frame_index not in requested:
                continue
            if frame.ndim != 3 or frame.shape[2] != 3:
                raise ContactSheetError(
                    f"{view.label}: frame {frame_index} is not a 3-channel image"
                )
            rgb = cv2_module.cvtColor(frame, cv2_module.COLOR_BGR2RGB)
            decoded[frame_index] = Image.fromarray(rgb).copy()
        if set(decoded) != requested:
            missing = sorted(requested - set(decoded))
            raise ContactSheetError(
                f"{view.label}: exact decode omitted frames {missing}"
            )
        return decoded, {
            "label": view.label,
            "path": str(view.path),
            "reported_frame_count": reported_count,
            "first_decoded_frame": first,
            "last_decoded_frame": ordered[-1],
            "seek_verified": True,
            "decode_mode": "verified_initial_seek_then_sequential_decode",
        }
    finally:
        capture.release()


def _load_font(size: int, *, bold: bool = False) -> ImageFont.ImageFont:
    candidates = (
        (
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
            if bold
            else "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
        ),
        (
            "/usr/share/fonts/truetype/liberation2/LiberationMono-Bold.ttf"
            if bold
            else "/usr/share/fonts/truetype/liberation2/LiberationMono-Regular.ttf"
        ),
    )
    for candidate in candidates:
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default()


def format_bag_time(time_sec: float) -> str:
    minutes = int(time_sec // 60)
    seconds = time_sec - minutes * 60
    return f"{minutes:02d}:{seconds:09.6f}"


def _fit_frame(image: Image.Image) -> Image.Image:
    source = image.convert("RGB")
    scale = min(TILE_WIDTH / source.width, FRAME_HEIGHT / source.height)
    width = max(1, round(source.width * scale))
    height = max(1, round(source.height * scale))
    resized = source.resize((width, height), Image.Resampling.LANCZOS)
    panel = Image.new("RGB", (TILE_WIDTH, FRAME_HEIGHT), "#111820")
    panel.paste(
        resized,
        ((TILE_WIDTH - width) // 2, (FRAME_HEIGHT - height) // 2),
    )
    return panel


def compose_contact_sheet(
    *,
    case_id: str,
    timeline: Timeline,
    plan: SamplePlan,
    views: Sequence[ViewSpec],
    decoded_by_view: dict[str, dict[int, Image.Image]],
) -> Image.Image:
    """Compose samples as rows and synchronized views as columns."""

    if not views:
        raise ContactSheetError("no views were supplied for composition")
    if not plan.frame_indices:
        raise ContactSheetError("no sample frames were supplied for composition")

    tile_height = FRAME_HEIGHT + LABEL_HEIGHT
    sheet_width = (
        SHEET_MARGIN * 2
        + len(views) * TILE_WIDTH
        + (len(views) - 1) * GUTTER
    )
    sheet_height = (
        HEADER_HEIGHT
        + SHEET_MARGIN
        + len(plan.frame_indices) * tile_height
        + (len(plan.frame_indices) - 1) * GUTTER
        + SHEET_MARGIN
    )
    sheet = Image.new("RGB", (sheet_width, sheet_height), "#e9edf0")
    draw = ImageDraw.Draw(sheet)
    title_font = _load_font(24, bold=True)
    label_font = _load_font(18, bold=True)
    small_font = _load_font(15)

    first_frame = plan.frame_indices[0]
    last_frame = plan.frame_indices[-1]
    draw.rectangle((0, 0, sheet_width, HEADER_HEIGHT), fill="#17222c")
    draw.text(
        (SHEET_MARGIN, 10),
        f"{case_id} | exact-frame multi-view review packet",
        font=title_font,
        fill="#ffffff",
    )
    draw.text(
        (SHEET_MARGIN, 43),
        (
            f"{plan.selection_mode} | samples={len(plan.frame_indices)} | "
            f"frames={first_frame}-{last_frame} | corrected bag time"
        ),
        font=small_font,
        fill="#c7d1d8",
    )

    for row_index, frame_index in enumerate(plan.frame_indices):
        timestamp = timeline.timestamps_sec[frame_index]
        row_top = HEADER_HEIGHT + SHEET_MARGIN + row_index * (
            tile_height + GUTTER
        )
        for column_index, view in enumerate(views):
            column_left = SHEET_MARGIN + column_index * (TILE_WIDTH + GUTTER)
            decoded = decoded_by_view.get(view.label, {})
            source_frame = decoded.get(frame_index)
            if source_frame is None:
                raise ContactSheetError(
                    f"{view.label}: decoded frame {frame_index} is missing"
                )
            panel = _fit_frame(source_frame)
            sheet.paste(panel, (column_left, row_top))

            label_top = row_top + FRAME_HEIGHT
            is_center = plan.center_frame == frame_index
            fill = "#7a5d00" if is_center else "#24333e"
            draw.rectangle(
                (
                    column_left,
                    label_top,
                    column_left + TILE_WIDTH - 1,
                    label_top + LABEL_HEIGHT - 1,
                ),
                fill=fill,
            )
            draw.text(
                (column_left + 10, label_top + 3),
                f"{view.label} | f{frame_index:06d}",
                font=label_font,
                fill="#ffffff",
            )
            timestamp_text = format_bag_time(timestamp)
            text_box = draw.textbbox((0, 0), timestamp_text, font=small_font)
            text_width = text_box[2] - text_box[0]
            draw.text(
                (
                    column_left + TILE_WIDTH - text_width - 10,
                    label_top + 22,
                ),
                timestamp_text,
                font=small_font,
                fill="#dce5ea",
            )
            border = "#ffd34d" if is_center else "#637683"
            draw.rectangle(
                (
                    column_left,
                    row_top,
                    column_left + TILE_WIDTH - 1,
                    row_top + tile_height - 1,
                ),
                outline=border,
                width=3 if is_center else 1,
            )
    return sheet


def save_image_create_only(image: Image.Image, output: Path) -> None:
    """Atomically publish one image without replacing an existing path."""

    suffix = output.suffix.lower()
    if suffix not in SUPPORTED_OUTPUT_SUFFIXES:
        raise ContactSheetError(
            "output suffix must be .png, .jpg, or .jpeg"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise ContactSheetError(f"refusing to overwrite existing output: {output}")

    descriptor, temporary_name = tempfile.mkstemp(
        dir=output.parent,
        prefix=f".{output.name}.",
        suffix=suffix,
    )
    temporary = Path(temporary_name)
    os.close(descriptor)
    try:
        image.save(
            temporary,
            format="PNG" if suffix == ".png" else "JPEG",
            quality=95,
            subsampling=0,
        )
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        try:
            os.link(temporary, output)
        except FileExistsError as exc:
            raise ContactSheetError(
                f"refusing to overwrite existing output: {output}"
            ) from exc
        directory_descriptor = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def render_contact_sheet(
    *,
    case_id: str,
    timeline_path: Path,
    view_values: Sequence[str],
    output: Path,
    center_bag_sec: float | None = None,
    center_frame: int | None = None,
    before_sec: float | None = None,
    after_sec: float | None = None,
    step_sec: float | None = None,
    first_frame: int | None = None,
    last_frame: int | None = None,
    frame_step: int | None = None,
) -> dict[str, Any]:
    """Validate, decode, compose, and create one read-only review packet."""

    if output.exists():
        raise ContactSheetError(f"refusing to overwrite existing output: {output}")
    if VIEW_LABEL_PATTERN.fullmatch(case_id) is None:
        raise ContactSheetError("case-id contains unsupported characters")

    timeline = load_timeline(timeline_path, case_id=case_id)
    views = parse_view_specs(view_values)
    plan = select_sample_plan(
        timeline,
        center_bag_sec=center_bag_sec,
        center_frame=center_frame,
        before_sec=before_sec,
        after_sec=after_sec,
        step_sec=step_sec,
        first_frame=first_frame,
        last_frame=last_frame,
        frame_step=frame_step,
    )

    decoded_by_view: dict[str, dict[int, Image.Image]] = {}
    view_reports: list[dict[str, Any]] = []
    for view in views:
        decoded, report = decode_exact_frames(view, plan.frame_indices)
        decoded_by_view[view.label] = decoded
        view_reports.append(report)

    sheet = compose_contact_sheet(
        case_id=case_id,
        timeline=timeline,
        plan=plan,
        views=views,
        decoded_by_view=decoded_by_view,
    )
    save_image_create_only(sheet, output)
    return {
        "ok": True,
        "authority": "read_only_review_packet_not_annotation",
        "case_id": case_id,
        "timeline": str(timeline_path.resolve()),
        "output": str(output.resolve()),
        "selection_mode": plan.selection_mode,
        "center_frame": plan.center_frame,
        "sample_frame_indices": list(plan.frame_indices),
        "sample_bag_times_sec": [
            timeline.timestamps_sec[index] for index in plan.frame_indices
        ],
        "view_count": len(views),
        "views": view_reports,
        "image_size": [sheet.width, sheet.height],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Render the same exact canonical frame indices from multiple AVI "
            "views as a read-only contact sheet."
        )
    )
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--timeline", type=Path, required=True)
    parser.add_argument(
        "--view",
        action="append",
        required=True,
        metavar="LABEL=AVI",
        help="Repeat for cam4, flir, cam1, cam2, or another evidence view.",
    )
    parser.add_argument("--center-bag-sec", type=float)
    parser.add_argument("--center-frame", type=int)
    parser.add_argument("--before-sec", type=float)
    parser.add_argument("--after-sec", type=float)
    parser.add_argument("--step-sec", type=float)
    parser.add_argument("--first-frame", type=int)
    parser.add_argument("--last-frame", type=int)
    parser.add_argument("--frame-step", type=int)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary = render_contact_sheet(
            case_id=args.case_id,
            timeline_path=args.timeline,
            view_values=args.view,
            output=args.output,
            center_bag_sec=args.center_bag_sec,
            center_frame=args.center_frame,
            before_sec=args.before_sec,
            after_sec=args.after_sec,
            step_sec=args.step_sec,
            first_frame=args.first_frame,
            last_frame=args.last_frame,
            frame_step=args.frame_step,
        )
    except ContactSheetError as exc:
        print(
            json.dumps(
                {"ok": False, "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
