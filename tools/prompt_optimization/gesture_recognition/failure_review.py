#!/usr/bin/env python3
"""Render every gesture FP/FN as an auditable contact-sheet review artifact.

The images are local evaluation evidence only.  Labels and predictions are
drawn after inference for human review and are never returned to NInfer.
"""

from __future__ import annotations

import argparse
import os
import textwrap
from pathlib import Path
from typing import Any, Mapping, Sequence

from PIL import Image, ImageDraw, ImageFont

from tools.prompt_optimization.gesture_recognition import gesture_prompt_eval as gesture


REVIEW_SCHEMA = "taskplanner.gesture_prompt_failure_review.v1"
FONT_PATH = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
TILE_WIDTH = 640
TILE_HEIGHT = 360
CAPTION_HEIGHT = 92


def _prediction_is_positive(record: Mapping[str, Any], threshold: float) -> bool:
    return gesture._prediction_positive(record, threshold)  # type: ignore[attr-defined]


def _failure_type(record: Mapping[str, Any], threshold: float) -> str | None:
    sample = record.get("sample", {})
    if not isinstance(sample, Mapping):
        raise ValueError("prediction record has no sample object")
    actual_positive = sample.get("label") == "open_receive"
    predicted_positive = _prediction_is_positive(record, threshold)
    if actual_positive == predicted_positive:
        return None
    if actual_positive:
        prediction = record.get("prediction", {})
        if isinstance(prediction, Mapping) and prediction.get("parse_error"):
            return "FN_format"
        return "FN"
    return "FP"


def _input_image_path(
    image_root: Path, sample: Mapping[str, Any], *, image_kind: str
) -> Path:
    case_id = str(sample["case_id"])
    frame_index = int(sample["frame_idx"])
    full_path = image_root / case_id / f"cam4_f{frame_index:04d}.jpg"
    if image_kind == "full":
        if full_path.is_file():
            return full_path
        raise FileNotFoundError(
            f"missing extracted full CAM4 frame for {case_id} frame {frame_index}: "
            f"expected {full_path}"
        )
    if image_kind == "right_detail":
        detail_path = image_root / case_id / f"cam4_right_detail_f{frame_index:04d}.jpg"
        if detail_path.is_file():
            return detail_path
        raise FileNotFoundError(
            f"missing fixed upper-right CAM4 detail for {case_id} frame {frame_index}: "
            f"expected {detail_path}"
        )
    if image_kind != "causal":
        raise ValueError("image_kind must be causal, full, or right_detail")
    prior_index = max(0, frame_index - gesture.CAUSAL_PRIOR_FRAMES)
    causal_path = image_root / case_id / (
        f"cam4_causal_right_pair_f{frame_index:04d}_prior{prior_index:04d}.jpg"
    )
    if causal_path.is_file():
        return causal_path
    if full_path.is_file():
        return full_path
    raise FileNotFoundError(
        f"missing extracted review image for {case_id} frame {frame_index}: "
        f"expected {causal_path} or {full_path}"
    )


def collect_failures(
    *,
    predictions_paths: Sequence[Path],
    image_root: Path,
    threshold: float,
    image_kind: str,
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for predictions_path in predictions_paths:
        for record in gesture.load_jsonl(predictions_path):
            failure_type = _failure_type(record, threshold)
            if failure_type is None:
                continue
            sample = record.get("sample", {})
            prediction = record.get("prediction", {})
            if not isinstance(sample, Mapping) or not isinstance(prediction, Mapping):
                raise ValueError("prediction record is missing sample or prediction object")
            sample_id = str(sample["sample_id"])
            if sample_id in seen_ids:
                raise ValueError(f"duplicate failed sample across inputs: {sample_id}")
            seen_ids.add(sample_id)
            try:
                image_path = _input_image_path(
                    image_root, sample, image_kind=image_kind
                )
                image_error = ""
            except FileNotFoundError as exc:
                image_path = Path()
                image_error = str(exc)
            failures.append(
                {
                    "failure_type": failure_type,
                    "sample_id": sample_id,
                    "case_id": str(sample["case_id"]),
                    "frame_idx": int(sample["frame_idx"]),
                    "time_sec": sample.get("time_sec"),
                    "sample_kind": str(sample["sample_kind"]),
                    "actual_label": str(sample["label"]),
                    "predicted_gesture": str(prediction.get("gesture", "")),
                    "confidence": prediction.get("confidence"),
                    "visual_evidence": str(prediction.get("visual_evidence", "")),
                    "parse_error": str(prediction.get("parse_error", "")),
                    "transport_error": str(record.get("transport_error", "")),
                    "input_image": str(image_path) if image_path else "",
                    "image_error": image_error,
                }
            )
    failures.sort(
        key=lambda item: (
            str(item["failure_type"]),
            str(item["sample_kind"]),
            str(item["case_id"]),
            int(item["frame_idx"]),
        )
    )
    return failures


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if FONT_PATH.is_file():
        return ImageFont.truetype(str(FONT_PATH), size=size)
    return ImageFont.load_default()


def _draw_tile(failure: Mapping[str, Any]) -> Image.Image:
    tile = Image.new("RGB", (TILE_WIDTH, TILE_HEIGHT + CAPTION_HEIGHT), "#111827")
    image_path = Path(str(failure.get("input_image", "")))
    if image_path.is_file():
        with Image.open(image_path) as raw:
            image = raw.convert("RGB")
        image.thumbnail((TILE_WIDTH, TILE_HEIGHT), Image.Resampling.LANCZOS)
        left = (TILE_WIDTH - image.width) // 2
        top = (TILE_HEIGHT - image.height) // 2
        tile.paste(image, (left, top))
    else:
        draw = ImageDraw.Draw(tile)
        draw.text((12, 12), "MISSING IMAGE", fill="#fecaca", font=_font(22))

    draw = ImageDraw.Draw(tile)
    failure_type = str(failure["failure_type"])
    color = "#fb7185" if failure_type.startswith("FP") else "#fbbf24"
    header = (
        f"{failure_type}  {failure['case_id']}  f{failure['frame_idx']}  "
        f"{failure['sample_kind']}"
    )
    decision = (
        f"GT={failure['actual_label']}  pred={failure['predicted_gesture']}  "
        f"conf={failure['confidence']}"
    )
    evidence = str(failure.get("visual_evidence", "")) or str(
        failure.get("parse_error", "")
    )
    draw.rectangle(
        (0, TILE_HEIGHT, TILE_WIDTH, TILE_HEIGHT + CAPTION_HEIGHT), fill="#111827"
    )
    draw.text((8, TILE_HEIGHT + 5), header, fill=color, font=_font(16))
    draw.text((8, TILE_HEIGHT + 27), decision, fill="#e5e7eb", font=_font(14))
    evidence_lines = textwrap.wrap(evidence, width=76)[:2]
    for index, line in enumerate(evidence_lines):
        draw.text(
            (8, TILE_HEIGHT + 47 + index * 18),
            line,
            fill="#cbd5e1",
            font=_font(13),
        )
    return tile


def render_pages(
    *, failures: Sequence[Mapping[str, Any]], output_dir: Path, columns: int, rows: int
) -> list[Path]:
    if columns < 1 or rows < 1:
        raise ValueError("columns and rows must be positive")
    output_dir.mkdir(parents=True, exist_ok=True)
    per_page = columns * rows
    page_paths: list[Path] = []
    for page_number, start in enumerate(range(0, len(failures), per_page), start=1):
        page_failures = failures[start : start + per_page]
        page = Image.new(
            "RGB",
            (columns * TILE_WIDTH, rows * (TILE_HEIGHT + CAPTION_HEIGHT)),
            "#020617",
        )
        for index, failure in enumerate(page_failures):
            x = (index % columns) * TILE_WIDTH
            y = (index // columns) * (TILE_HEIGHT + CAPTION_HEIGHT)
            page.paste(_draw_tile(failure), (x, y))
        output_path = output_dir / f"failure-page-{page_number:02d}.jpg"
        temporary_path = output_dir / f".{output_path.stem}.{os.getpid()}.tmp.jpg"
        try:
            page.save(temporary_path, quality=94)
            os.replace(temporary_path, output_path)
        finally:
            temporary_path.unlink(missing_ok=True)
        page_paths.append(output_path)
    return page_paths


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions", type=Path, action="append", required=True)
    parser.add_argument("--image-root", type=Path, required=True)
    parser.add_argument(
        "--image-kind",
        choices=("causal", "full", "right_detail"),
        default="causal",
        help="render the image sent to VLM or the full current CAM4 audit frame",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--threshold", type=float, required=True)
    parser.add_argument("--columns", type=int, default=2)
    parser.add_argument("--rows", type=int, default=2)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    if not 0.0 <= args.threshold <= 1.0:
        parser.error("--threshold must be in [0, 1]")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    failures = collect_failures(
        predictions_paths=args.predictions,
        image_root=args.image_root,
        threshold=args.threshold,
        image_kind=args.image_kind,
    )
    report_path = args.output_dir / "failures.json"
    if report_path.exists() and not args.force:
        raise FileExistsError(f"refusing to overwrite {report_path}; use --force")
    pages = render_pages(
        failures=failures,
        output_dir=args.output_dir / "pages",
        columns=args.columns,
        rows=args.rows,
    )
    by_type = {
        failure_type: sum(
            failure["failure_type"] == failure_type for failure in failures
        )
        for failure_type in sorted({str(failure["failure_type"]) for failure in failures})
    }
    report = {
        "schema": REVIEW_SCHEMA,
        "ground_truth_usage": "evaluation_only",
        "may_publish_runtime": False,
        "threshold": args.threshold,
        "image_kind": args.image_kind,
        "failure_count": len(failures),
        "by_failure_type": by_type,
        "page_paths": [str(path) for path in pages],
        "failures": failures,
    }
    gesture.write_json(report_path, report, overwrite=True)
    print(
        gesture.canonical_json(
            {
                "report": str(report_path),
                "failure_count": len(failures),
                "page_count": len(pages),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
