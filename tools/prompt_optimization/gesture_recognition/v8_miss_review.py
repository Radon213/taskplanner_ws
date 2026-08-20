#!/usr/bin/env python3
"""Render an auditable visual review bundle for every V8 open-hand miss.

This is an offline review-only tool.  It reads completed evaluation records and
their pre-extracted CAM4 frames, then renders each false negative with both the
full source scene and the exact fixed detail crop sent to the VLM.  Ground truth
and the raw VLM answer are drawn only after inference, for a human reviewer.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from PIL import Image, ImageDraw, ImageFont


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SCHEMA = "taskplanner.gesture_prompt_v8_miss_review.v1"
FULL_WIDTH = 800
IMAGE_HEIGHT = 450
DETAIL_WIDTH = 800
HEADER_HEIGHT = 72
CAPTION_HEIGHT = 170
ITEM_WIDTH = FULL_WIDTH + DETAIL_WIDTH
ITEM_HEIGHT = HEADER_HEIGHT + IMAGE_HEIGHT + CAPTION_HEIGHT
FONT_PATHS = (
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/truetype/nanum/NanumGothic.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
)

# This box is the source-space crop used by ``right_detail_only``.  It is drawn
# on the full source frame solely for review; the exact transformed detail image
# appears beside it.
SOURCE_CROP = (340, 0, 640, 300)


@dataclass(frozen=True)
class Miss:
    """A completed, positive-label V8 false negative and its source metadata."""

    partition: str
    execution_path: Path
    prediction_path: Path
    sample: Mapping[str, Any]
    prediction: Mapping[str, Any]
    raw_model_text: str

    @property
    def sample_id(self) -> str:
        return str(self.sample["sample_id"])

    @property
    def case_id(self) -> str:
        return str(self.sample["case_id"])

    @property
    def frame_idx(self) -> int:
        return int(self.sample["frame_idx"])


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def load_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number}: expected a JSON object")
        yield value


def _resolve_repository_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else REPOSITORY_ROOT / path


def _raw_answer(raw_model_text: str) -> str:
    """Return a compact display form without changing the stored raw output."""

    answer = raw_model_text.strip()
    answer = re.sub(r"^```(?:json)?\s*", "", answer, flags=re.IGNORECASE)
    answer = re.sub(r"\s*```$", "", answer).strip()
    return " ".join(answer.split()) or "(empty output)"


def collect_misses(*, partition: str, execution_path: Path) -> list[Miss]:
    """Collect only actual existing-label positives that V8 predicted false."""

    execution = load_json(execution_path)
    if execution.get("status") != "completed" or not execution.get("scoreable"):
        raise ValueError(f"{execution_path}: execution is not complete and scoreable")
    if execution.get("prompt_version") != "gesture-top-right-open-hand-v8":
        raise ValueError(f"{execution_path}: not a V8 prediction run")

    misses: list[Miss] = []
    for batch in execution.get("batches", []):
        if not isinstance(batch, Mapping):
            raise ValueError(f"{execution_path}: malformed batch")
        prediction_path = _resolve_repository_path(str(batch["prediction_path"]))
        for record in load_jsonl(prediction_path):
            sample = record.get("sample")
            prediction = record.get("prediction")
            if not isinstance(sample, Mapping) or not isinstance(prediction, Mapping):
                raise ValueError(f"{prediction_path}: missing sample or prediction")
            if sample.get("label") != "open_receive":
                raise ValueError(
                    f"{prediction_path}: positive-only review received non-positive label"
                )
            if prediction.get("gesture") == "open_receive":
                continue
            if record.get("transport_error"):
                raise ValueError(
                    f"{prediction_path}: transport error is not a VLM visual decision"
                )
            misses.append(
                Miss(
                    partition=partition,
                    execution_path=execution_path,
                    prediction_path=prediction_path,
                    sample=sample,
                    prediction=prediction,
                    raw_model_text=str(record.get("raw_model_text", "")),
                )
            )
    return misses


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in FONT_PATHS:
        if path.is_file():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def _fit_image(image: Image.Image, width: int, height: int) -> Image.Image:
    canvas = Image.new("RGB", (width, height), "#020617")
    copy = image.convert("RGB")
    copy.thumbnail((width, height), Image.Resampling.LANCZOS)
    canvas.paste(copy, ((width - copy.width) // 2, (height - copy.height) // 2))
    return canvas


def _source_image_paths(miss: Miss) -> tuple[Path, Path]:
    run_root = miss.execution_path.parents[2]
    image_dir = run_root / "images" / miss.case_id
    frame_name = f"cam4_f{miss.frame_idx:04d}.jpg"
    detail_name = f"cam4_right_detail_f{miss.frame_idx:04d}.jpg"
    full_path = image_dir / frame_name
    detail_path = image_dir / detail_name
    if not full_path.is_file() or not detail_path.is_file():
        raise FileNotFoundError(
            f"missing source pair for {miss.sample_id}: {full_path}, {detail_path}"
        )
    return full_path, detail_path


def _draw_source_crop_box(
    image: Image.Image, *, display_width: int, display_height: int
) -> None:
    """Draw the fixed source crop accurately after image-letterboxing."""

    source_width, source_height = image.size
    scale = min(display_width / source_width, display_height / source_height)
    shown_width = round(source_width * scale)
    shown_height = round(source_height * scale)
    left = (display_width - shown_width) // 2
    top = (display_height - shown_height) // 2
    x0, y0, x1, y1 = SOURCE_CROP
    draw = ImageDraw.Draw(image)
    draw.rectangle(
        (
            left + round(x0 * scale),
            top + round(y0 * scale),
            left + round(x1 * scale),
            top + round(y1 * scale),
        ),
        outline="#facc15",
        width=5,
    )


def _text(draw: ImageDraw.ImageDraw, xy: tuple[int, int], value: str, *, size: int, fill: str) -> None:
    draw.text(xy, value, font=_font(size), fill=fill)


def render_item(miss: Miss) -> Image.Image:
    """Render a full scene + exact VLM crop comparison for one false negative."""

    full_path, detail_path = _source_image_paths(miss)
    with Image.open(full_path) as source:
        full = _fit_image(source, FULL_WIDTH, IMAGE_HEIGHT)
    _draw_source_crop_box(full, display_width=FULL_WIDTH, display_height=IMAGE_HEIGHT)
    with Image.open(detail_path) as source:
        detail = _fit_image(source, DETAIL_WIDTH, IMAGE_HEIGHT)

    item = Image.new("RGB", (ITEM_WIDTH, ITEM_HEIGHT), "#020617")
    draw = ImageDraw.Draw(item)
    draw.rectangle((0, 0, ITEM_WIDTH, HEADER_HEIGHT), fill="#7f1d1d")
    header = (
        f"V8 미검출 (False Negative)  |  {miss.partition}  |  "
        f"{miss.case_id}, frame {miss.frame_idx}, t={float(miss.sample['time_sec']):.3f}s"
    )
    _text(draw, (20, 17), header, size=29, fill="#fff7ed")

    item.paste(full, (0, HEADER_HEIGHT))
    item.paste(detail, (FULL_WIDTH, HEADER_HEIGHT))
    draw = ImageDraw.Draw(item)
    _text(draw, (20, HEADER_HEIGHT + 12), "원본 CAM4 장면  (노란 상자 = VLM 입력 영역)", size=22, fill="#fef08a")
    _text(draw, (FULL_WIDTH + 20, HEADER_HEIGHT + 12), "VLM에 실제 입력된 오른쪽 위 crop", size=22, fill="#bfdbfe")

    caption_top = HEADER_HEIGHT + IMAGE_HEIGHT
    draw.rectangle((0, caption_top, ITEM_WIDTH, ITEM_HEIGHT), fill="#111827")
    _text(
        draw,
        (20, caption_top + 14),
        "정답 (기존 검토 라벨): open_hand = true  /  open_receive",
        size=27,
        fill="#86efac",
    )
    _text(
        draw,
        (20, caption_top + 57),
        f"VLM 원문 응답: {_raw_answer(miss.raw_model_text)}  →  내부 판정: {miss.prediction.get('gesture', '')}",
        size=25,
        fill="#fecaca",
    )
    metadata = (
        f"event={miss.sample.get('event_id', '')} | sample={miss.sample.get('sample_kind', '')} | "
        f"confidence={miss.prediction.get('confidence', '')} | id={miss.sample_id}"
    )
    for row, text in enumerate(textwrap.wrap(metadata, width=125)[:2]):
        _text(draw, (20, caption_top + 103 + row * 26), text, size=18, fill="#cbd5e1")
    return item


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_readme(path: Path, misses: Sequence[Miss]) -> None:
    counts: dict[str, int] = {}
    for miss in misses:
        counts[miss.partition] = counts.get(miss.partition, 0) + 1
    lines = [
        "# V8 open-hand misses — reviewer bundle",
        "",
        "This bundle contains every V8 false negative from the completed positive-only evaluation.",
        "Each `items/` image shows the original CAM4 frame at left and the exact fixed crop sent to the VLM at right.",
        "The green caption is the existing read-only ground truth; the red caption is the VLM's raw response.",
        "",
        "## Counts",
        "",
    ]
    for partition, count in counts.items():
        lines.append(f"- {partition}: {count}")
    lines.extend(
        [
            f"- total: {len(misses)}",
            "",
            "`pages/` groups four misses per page within each evaluation partition. `items/` contains one full-resolution comparison per miss.",
            "No label, prediction, or input image was modified to create this review bundle.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_review_index_markdown(path: Path, entries: Sequence[Mapping[str, Any]]) -> None:
    """Write a reviewer-friendly link list for the full-resolution images."""

    lines = [
        "# V8 open-hand misses — individual review index",
        "",
        "Every row links one full-resolution comparison: original CAM4 scene at left, exact VLM input crop at right, then the existing GT and raw VLM response below.",
        "",
    ]
    for partition in sorted({str(entry["partition"]) for entry in entries}):
        partition_entries = [entry for entry in entries if entry["partition"] == partition]
        lines.extend(
            [
                f"## {partition} ({len(partition_entries)} misses)",
                "",
                "| # | Case | Frame | Time (s) | GT | VLM answer | Image |",
                "| ---: | --- | ---: | ---: | --- | --- | --- |",
            ]
        )
        for entry in partition_entries:
            image_path = Path(str(entry["review_image"])).relative_to(path.parent)
            raw = _raw_answer(str(entry["vlm"]["raw_model_text"]))
            lines.append(
                "| {index} | {case_id} | {frame_idx} | {time_sec:.3f} | "
                "`open_hand=true` | `{raw}` | [열기]({image_path}) |".format(
                    index=entry["index"],
                    case_id=entry["case_id"],
                    frame_idx=entry["frame_idx"],
                    time_sec=float(entry["time_sec"]),
                    raw=raw,
                    image_path=image_path.as_posix(),
                )
            )
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def render_bundle(*, misses: Sequence[Miss], output_dir: Path) -> dict[str, Any]:
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite existing review bundle: {output_dir}")
    output_dir.mkdir(parents=True)
    items_dir = output_dir / "items"
    pages_dir = output_dir / "pages"
    items_dir.mkdir()
    pages_dir.mkdir()

    item_paths: list[Path] = []
    entries: list[dict[str, Any]] = []
    items: list[Image.Image] = []
    try:
        for index, miss in enumerate(misses, start=1):
            item = render_item(miss)
            safe_sample_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", miss.sample_id)
            item_path = items_dir / f"{index:02d}_{safe_sample_id}.jpg"
            item.save(item_path, quality=95, subsampling=0)
            item_paths.append(item_path)
            items.append(item)
            full_path, detail_path = _source_image_paths(miss)
            entries.append(
                {
                    "index": index,
                    "partition": miss.partition,
                    "sample_id": miss.sample_id,
                    "case_id": miss.case_id,
                    "frame_idx": miss.frame_idx,
                    "time_sec": miss.sample["time_sec"],
                    "event_id": miss.sample.get("event_id", ""),
                    "sample_kind": miss.sample.get("sample_kind", ""),
                    "ground_truth": {
                        "existing_label": miss.sample["label"],
                        "visual_policy_value": {"open_hand": True},
                    },
                    "vlm": {
                        "raw_model_text": miss.raw_model_text,
                        "parsed_gesture": miss.prediction.get("gesture", ""),
                        "confidence": miss.prediction.get("confidence"),
                    },
                    "original_cam4_image": str(full_path),
                    "vlm_input_image": str(detail_path),
                    "review_image": str(item_path),
                    "prediction_record_source": str(miss.prediction_path),
                }
            )

        page_paths: list[Path] = []
        pages_by_partition: dict[str, list[Path]] = {}
        columns, rows = 2, 2
        per_page = columns * rows
        for partition in sorted({miss.partition for miss in misses}):
            partition_items = [
                item for miss, item in zip(misses, items) if miss.partition == partition
            ]
            partition_pages: list[Path] = []
            partition_dir = pages_dir / partition
            partition_dir.mkdir()
            for page_number, start in enumerate(
                range(0, len(partition_items), per_page), start=1
            ):
                page = Image.new(
                    "RGB", (ITEM_WIDTH, ITEM_HEIGHT * rows), "#020617"
                )
                for index, item in enumerate(partition_items[start : start + per_page]):
                    scaled = item.resize(
                        (ITEM_WIDTH // columns, ITEM_HEIGHT // rows),
                        Image.Resampling.LANCZOS,
                    )
                    x = (index % columns) * (ITEM_WIDTH // columns)
                    y = (index // columns) * (ITEM_HEIGHT // rows)
                    page.paste(scaled, (x, y))
                page_path = partition_dir / f"misses-page-{page_number:02d}.jpg"
                page.save(page_path, quality=95, subsampling=0)
                partition_pages.append(page_path)
                page_paths.append(page_path)
            pages_by_partition[partition] = partition_pages
    finally:
        for item in items:
            item.close()

    report = {
        "schema": SCHEMA,
        "ground_truth_usage": "evaluation_only",
        "may_publish_runtime": False,
        "metric_scope": "existing confirmed open-hand positives only",
        "failure_type": "false_negative",
        "failure_count": len(entries),
        "page_paths": [str(path) for path in page_paths],
        "pages_by_partition": {
            partition: [str(path) for path in paths]
            for partition, paths in pages_by_partition.items()
        },
        "entries": entries,
    }
    _write_json(output_dir / "review_index.json", report)
    _write_readme(output_dir / "README.md", misses)
    _write_review_index_markdown(output_dir / "REVIEW_INDEX.md", entries)
    return report


def _execution_argument(value: str) -> tuple[str, Path]:
    try:
        partition, path_text = value.split("=", 1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected PARTITION=/path/to/execution.json") from exc
    if not partition.strip() or not path_text.strip():
        raise argparse.ArgumentTypeError("partition and path must both be non-empty")
    return partition.strip(), Path(path_text.strip())


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execution",
        type=_execution_argument,
        action="append",
        required=True,
        help="PARTITION=/path/to/completed V8 execution JSON; repeat per partition",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    misses: list[Miss] = []
    for partition, execution_path in args.execution:
        misses.extend(collect_misses(partition=partition, execution_path=execution_path))
    if not misses:
        raise ValueError("no V8 misses found")
    misses.sort(key=lambda miss: (miss.partition, miss.case_id, miss.frame_idx, miss.sample_id))
    report = render_bundle(misses=misses, output_dir=args.output_dir)
    print(f"rendered {report['failure_count']} V8 false negatives to {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
