#!/usr/bin/env python3
"""Render all clear-frame V8 proxy disagreements for direct image review.

This creates review evidence only.  It reads immutable completed evaluation
records and draws the local reference and VLM response after inference; none of
that metadata is ever sent to the model.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from PIL import Image, ImageDraw, ImageFont

from tools.prompt_optimization.gesture_recognition import gesture_prompt_eval as gesture


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SCHEMA = "taskplanner.gesture_clear_frame_failure_review.v1"
ITEM_WIDTH = 1600
HEADER_HEIGHT = 72
IMAGE_HEIGHT = 450
CAPTION_HEIGHT = 188
ITEM_HEIGHT = HEADER_HEIGHT + IMAGE_HEIGHT + CAPTION_HEIGHT
SOURCE_CROP = (340, 0, 640, 300)
FONT_PATHS = (
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/truetype/nanum/NanumGothic.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
)


@dataclass(frozen=True)
class Disagreement:
    partition: str
    execution_path: Path
    prediction_path: Path
    record: Mapping[str, Any]
    failure_type: str

    @property
    def sample(self) -> Mapping[str, Any]:
        value = self.record["sample"]
        if not isinstance(value, Mapping):
            raise ValueError("missing sample")
        return value

    @property
    def prediction(self) -> Mapping[str, Any]:
        value = self.record["prediction"]
        if not isinstance(value, Mapping):
            raise ValueError("missing prediction")
        return value

    @property
    def case_id(self) -> str:
        return str(self.sample["case_id"])

    @property
    def frame_idx(self) -> int:
        return int(self.sample["frame_idx"])


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected JSON object")
    return value


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number}: expected JSON object")
        values.append(value)
    return values


def _repo_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else REPOSITORY_ROOT / path


def _raw_answer(value: str) -> str:
    answer = value.strip()
    answer = re.sub(r"^```(?:json)?\s*", "", answer, flags=re.IGNORECASE)
    answer = re.sub(r"\s*```$", "", answer).strip()
    return " ".join(answer.split()) or "(empty output)"


def collect_disagreements(
    *, partition: str, execution_path: Path
) -> list[Disagreement]:
    execution = _load_json(execution_path)
    if execution.get("status") != "completed" or not execution.get("scoreable"):
        raise ValueError(f"{execution_path}: execution must be completed and scoreable")
    if execution.get("prompt_version") != "gesture-top-right-open-hand-v8":
        raise ValueError(f"{execution_path}: expected V8 records")

    selected: list[Disagreement] = []
    for batch in execution.get("batches", []):
        if not isinstance(batch, Mapping):
            raise ValueError(f"{execution_path}: malformed batch")
        prediction_path = _repo_path(str(batch["prediction_path"]))
        for record in _load_jsonl(prediction_path):
            sample = record.get("sample")
            prediction = record.get("prediction")
            if not isinstance(sample, Mapping) or not isinstance(prediction, Mapping):
                raise ValueError(f"{prediction_path}: record missing sample/prediction")
            if record.get("transport_error") or prediction.get("parse_error"):
                raise ValueError(f"{prediction_path}: non-decision output in completed run")
            actual = str(sample.get("label", ""))
            predicted = str(prediction.get("gesture", ""))
            if actual == "open_receive" and predicted != "open_receive":
                failure_type = "FN"
            elif actual == "not_open_receive" and predicted == "open_receive":
                failure_type = "FP"
            else:
                continue
            selected.append(
                Disagreement(
                    partition=partition,
                    execution_path=execution_path,
                    prediction_path=prediction_path,
                    record=record,
                    failure_type=failure_type,
                )
            )
    return selected


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in FONT_PATHS:
        if path.is_file():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def _fit(image: Image.Image, width: int, height: int) -> Image.Image:
    canvas = Image.new("RGB", (width, height), "#020617")
    copy = image.convert("RGB")
    copy.thumbnail((width, height), Image.Resampling.LANCZOS)
    canvas.paste(copy, ((width - copy.width) // 2, (height - copy.height) // 2))
    return canvas


def _source_paths(item: Disagreement) -> tuple[Path, Path]:
    run_root = item.execution_path.parents[2]
    image_dir = run_root / "images" / item.case_id
    full = image_dir / f"cam4_f{item.frame_idx:04d}.jpg"
    detail = image_dir / f"cam4_right_detail_f{item.frame_idx:04d}.jpg"
    if not full.is_file() or not detail.is_file():
        raise FileNotFoundError(f"missing source images: {full}, {detail}")
    return full, detail


def _draw_crop_box(image: Image.Image) -> None:
    # The review full image has its CAM4 content letterboxed inside 800x450.
    source_width, source_height = 640, 360
    display_width, display_height = 800, IMAGE_HEIGHT
    scale = min(display_width / source_width, display_height / source_height)
    shown_width, shown_height = round(source_width * scale), round(source_height * scale)
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


def _draw_text(
    draw: ImageDraw.ImageDraw, xy: tuple[int, int], value: str, *, size: int, fill: str
) -> None:
    draw.text(xy, value, font=_font(size), fill=fill)


def _ground_truth_caption(item: Disagreement) -> str:
    if item.sample["label"] == "open_receive":
        return "정답 (기존 open-hand 이벤트 중앙): open_hand = true / open_receive"
    clearance = int(item.sample["nearest_open_hand_boundary_frames"])
    seconds = float(item.sample["nearest_open_hand_boundary_sec"])
    return (
        "정답 (고여유 비이벤트 gap 중앙): open_hand = false / not_open_receive "
        f"(가장 가까운 open-hand 경계 {clearance}f = {seconds:.2f}s)"
    )


def render_item(item: Disagreement) -> Image.Image:
    full_path, detail_path = _source_paths(item)
    with Image.open(full_path) as source:
        full = _fit(source, 800, IMAGE_HEIGHT)
    _draw_crop_box(full)
    with Image.open(detail_path) as source:
        detail = _fit(source, 800, IMAGE_HEIGHT)

    page = Image.new("RGB", (ITEM_WIDTH, ITEM_HEIGHT), "#020617")
    draw = ImageDraw.Draw(page)
    color = "#991b1b" if item.failure_type == "FN" else "#9a3412"
    draw.rectangle((0, 0, ITEM_WIDTH, HEADER_HEIGHT), fill=color)
    _draw_text(
        draw,
        (20, 17),
        (
            f"V8 {item.failure_type}  |  {item.partition}  |  {item.case_id}, "
            f"frame {item.frame_idx}, t={float(item.sample['time_sec']):.3f}s"
        ),
        size=29,
        fill="#fff7ed",
    )
    page.paste(full, (0, HEADER_HEIGHT))
    page.paste(detail, (800, HEADER_HEIGHT))
    draw = ImageDraw.Draw(page)
    _draw_text(draw, (20, HEADER_HEIGHT + 12), "원본 CAM4 (노란 상자 = VLM 입력 영역)", size=22, fill="#fef08a")
    _draw_text(draw, (820, HEADER_HEIGHT + 12), "VLM에 실제 입력된 오른쪽 위 crop", size=22, fill="#bfdbfe")

    top = HEADER_HEIGHT + IMAGE_HEIGHT
    draw.rectangle((0, top, ITEM_WIDTH, ITEM_HEIGHT), fill="#111827")
    _draw_text(draw, (20, top + 13), _ground_truth_caption(item), size=25, fill="#86efac")
    raw = _raw_answer(str(item.record.get("raw_model_text", "")))
    _draw_text(
        draw,
        (20, top + 54),
        f"VLM 원문 응답: {raw}  →  내부 판정: {item.prediction.get('gesture', '')}",
        size=24,
        fill="#fecaca",
    )
    meta = (
        f"event={item.sample.get('event_id', '')} | sample={item.sample.get('sample_kind', '')} | "
        f"confidence={item.prediction.get('confidence', '')} | id={item.sample.get('sample_id', '')}"
    )
    for line_number, line in enumerate(textwrap.wrap(meta, width=126)[:2]):
        _draw_text(draw, (20, top + 103 + 26 * line_number), line, size=18, fill="#cbd5e1")
    return page


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_index(path: Path, entries: Sequence[Mapping[str, Any]]) -> None:
    lines = [
        "# Clear-frame V8 disagreements — individual review index",
        "",
        "Each image includes the original CAM4 scene, exact VLM crop, event-derived reference, and raw VLM response.",
        "",
    ]
    for partition in sorted({str(entry["partition"]) for entry in entries}):
        subset = [entry for entry in entries if entry["partition"] == partition]
        lines.extend(
            [
                f"## {partition} ({len(subset)} disagreements)",
                "",
                "| # | Type | Case | Frame | GT | VLM | Image |",
                "| ---: | --- | --- | ---: | --- | --- | --- |",
            ]
        )
        for entry in subset:
            image = Path(str(entry["review_image"])).relative_to(path.parent).as_posix()
            lines.append(
                "| {index} | {failure_type} | {case_id} | {frame_idx} | `{actual}` | `{predicted}` | [열기]({image}) |".format(
                    index=entry["index"],
                    failure_type=entry["failure_type"],
                    case_id=entry["case_id"],
                    frame_idx=entry["frame_idx"],
                    actual=entry["actual_label"],
                    predicted=entry["predicted_gesture"],
                    image=image,
                )
            )
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def render_bundle(*, items: Sequence[Disagreement], output_dir: Path) -> dict[str, Any]:
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite {output_dir}")
    output_dir.mkdir(parents=True)
    item_dir, page_dir = output_dir / "items", output_dir / "pages"
    item_dir.mkdir()
    page_dir.mkdir()
    rendered: list[Image.Image] = []
    entries: list[dict[str, Any]] = []
    try:
        for index, item in enumerate(items, start=1):
            image = render_item(item)
            safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(item.sample["sample_id"]))
            item_path = item_dir / f"{index:02d}_{item.failure_type}_{safe_id}.jpg"
            image.save(item_path, quality=95, subsampling=0)
            rendered.append(image)
            full, detail = _source_paths(item)
            entries.append(
                {
                    "index": index,
                    "partition": item.partition,
                    "failure_type": item.failure_type,
                    "case_id": item.case_id,
                    "frame_idx": item.frame_idx,
                    "time_sec": item.sample["time_sec"],
                    "sample_id": item.sample["sample_id"],
                    "sample_kind": item.sample["sample_kind"],
                    "event_id": item.sample["event_id"],
                    "actual_label": item.sample["label"],
                    "predicted_gesture": item.prediction["gesture"],
                    "raw_model_text": item.record.get("raw_model_text", ""),
                    "original_cam4_image": str(full),
                    "vlm_input_image": str(detail),
                    "review_image": str(item_path),
                    "prediction_record_source": str(item.prediction_path),
                }
            )

        pages_by_partition: dict[str, list[str]] = {}
        page_paths: list[str] = []
        for partition in sorted({item.partition for item in items}):
            subset = [image for item, image in zip(items, rendered) if item.partition == partition]
            partition_dir = page_dir / partition
            partition_dir.mkdir()
            partition_paths: list[str] = []
            for number, start in enumerate(range(0, len(subset), 4), start=1):
                page = Image.new("RGB", (ITEM_WIDTH, ITEM_HEIGHT * 2), "#020617")
                for offset, image in enumerate(subset[start : start + 4]):
                    scaled = image.resize((ITEM_WIDTH // 2, ITEM_HEIGHT // 2), Image.Resampling.LANCZOS)
                    page.paste(scaled, ((offset % 2) * (ITEM_WIDTH // 2), (offset // 2) * (ITEM_HEIGHT // 2)))
                page_path = partition_dir / f"disagreements-page-{number:02d}.jpg"
                page.save(page_path, quality=95, subsampling=0)
                partition_paths.append(str(page_path))
                page_paths.append(str(page_path))
            pages_by_partition[partition] = partition_paths
    finally:
        for image in rendered:
            image.close()

    report = {
        "schema": SCHEMA,
        "ground_truth_usage": "evaluation_only",
        "may_publish_runtime": False,
        "reference_interpretation": "clear-frame event-derived proxy; not new frame-level human labels",
        "disagreement_count": len(entries),
        "by_failure_type": {
            failure_type: sum(entry["failure_type"] == failure_type for entry in entries)
            for failure_type in sorted({str(entry["failure_type"]) for entry in entries})
        },
        "pages_by_partition": pages_by_partition,
        "page_paths": page_paths,
        "entries": entries,
    }
    _write_json(output_dir / "review_index.json", report)
    _write_index(output_dir / "REVIEW_INDEX.md", entries)
    return report


def _execution_arg(value: str) -> tuple[str, Path]:
    try:
        partition, path = value.split("=", 1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected PARTITION=/path/to/execution.json") from exc
    return partition, Path(path)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execution", type=_execution_arg, action="append", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    items: list[Disagreement] = []
    for partition, execution in args.execution:
        items.extend(collect_disagreements(partition=partition, execution_path=execution))
    if not items:
        raise ValueError("no clear-frame disagreements")
    items.sort(key=lambda item: (item.partition, item.failure_type, item.case_id, item.frame_idx))
    report = render_bundle(items=items, output_dir=args.output_dir)
    print(json.dumps({"disagreement_count": report["disagreement_count"], "output_dir": str(args.output_dir)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
