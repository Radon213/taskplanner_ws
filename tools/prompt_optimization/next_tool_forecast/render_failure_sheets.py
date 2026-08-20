#!/usr/bin/env python3
"""Render original FLIR/CAM4 evidence for every scored FP/FN window.

The renderer only reads an already-completed offline run and its benchmark. It
never sends frames, labels, or review text to NInfer.  Every output stays under
this prompt experiment's ``runs/`` tree so human review can trace a failure to
the exact original proxy frame indices used for its request.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

import cv2
import numpy as np

from prompt_contract import thresholded_decision
from run_ninfer_eval import RUNS_ROOT, RunError, read_jsonl


SHEET_SCHEMA = "taskplanner.next_tool_forecast_failure_sheets.v1"
PANEL_WIDTH = 360
PANEL_HEIGHT = 202
TITLE_HEIGHT = 102


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--benchmark-dir",
        type=Path,
        action="append",
        required=True,
        help="Benchmark directory with inputs.jsonl; may be supplied more than once for a disjoint union.",
    )
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--threshold", type=float, default=None)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def ensure_safe_output_dir(path: Path) -> Path:
    output_dir = path.resolve()
    runs_root = RUNS_ROOT.resolve()
    try:
        output_dir.relative_to(runs_root)
    except ValueError as exc:
        raise RunError(f"output directory must be under {runs_root}") from exc
    if output_dir == runs_root:
        raise RunError("output directory must be a run subdirectory")
    return output_dir


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RunError(f"JSON object required: {path}")
    return value


def error_kind(row: Mapping[str, Any], threshold: float) -> str | None:
    target = row.get("target")
    if not isinstance(target, Mapping):
        raise RunError("prediction row target missing")
    actual = str(target.get("tool_id", "")) if target.get("decision") == "handover" else "none"
    prediction = row.get("prediction")
    predicted = "none"
    if isinstance(prediction, Mapping) and thresholded_decision(prediction, threshold) == "handover":
        predicted = str(prediction.get("tool_id", ""))
    if actual == predicted:
        return None
    if actual == "none":
        return "fp"
    if predicted == "none":
        return "fn"
    return "wrong_tool_fp_fn"


def decode_exact_frames(video_path: Path, requested: set[int]) -> dict[int, np.ndarray]:
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise RunError(f"cannot open source proxy: {video_path}")
    frames: dict[int, np.ndarray] = {}
    index, maximum = 0, max(requested)
    try:
        while index <= maximum:
            ok, frame = capture.read()
            if not ok:
                break
            if index in requested:
                frames[index] = frame
            index += 1
    finally:
        capture.release()
    missing = sorted(requested - set(frames))
    if missing:
        raise RunError(f"{video_path}: missing source frame(s): {missing[:8]}")
    return frames


def panel(frame: np.ndarray, label: str) -> np.ndarray:
    image = cv2.resize(frame, (PANEL_WIDTH, PANEL_HEIGHT), interpolation=cv2.INTER_AREA)
    cv2.rectangle(image, (0, 0), (PANEL_WIDTH, 24), (0, 0, 0), thickness=-1)
    cv2.putText(image, label, (8, 17), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (255, 255, 255), 1, cv2.LINE_AA)
    return image


def prediction_name(row: Mapping[str, Any], threshold: float) -> str:
    prediction = row.get("prediction")
    if isinstance(prediction, Mapping) and thresholded_decision(prediction, threshold) == "handover":
        return str(prediction.get("tool_id", "invalid"))
    return "none"


def sheet(
    *,
    frames: list[tuple[np.ndarray, np.ndarray]],
    offsets: list[float],
    failure_kind: str,
    target: str,
    predicted: str,
    example_id: str,
    case_id: str,
    cutoff_sec: float,
    target_event_id: str | None,
    target_delta_sec: float | None,
) -> np.ndarray:
    canvas = np.full((TITLE_HEIGHT + PANEL_HEIGHT * len(frames), PANEL_WIDTH * 2, 3), 24, dtype=np.uint8)
    title = f"{failure_kind.upper()} | target={target} | predicted={predicted}"
    cv2.putText(canvas, title, (10, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.68, (255, 255, 255), 2, cv2.LINE_AA)
    cv2.putText(canvas, example_id, (10, 54), cv2.FONT_HERSHEY_SIMPLEX, 0.44, (180, 220, 255), 1, cv2.LINE_AA)
    event_text = target_event_id or "no future transfer event (none target)"
    timing = f"case={case_id} cutoff={cutoff_sec:.3f}s | event={event_text}"
    if target_delta_sec is not None:
        timing += f" delta={target_delta_sec:+.3f}s"
    cv2.putText(canvas, timing[:118], (10, 82), cv2.FONT_HERSHEY_SIMPLEX, 0.38, (180, 255, 180), 1, cv2.LINE_AA)
    for index, ((flir, cam4), offset) in enumerate(zip(frames, offsets)):
        y = TITLE_HEIGHT + index * PANEL_HEIGHT
        canvas[y : y + PANEL_HEIGHT, 0:PANEL_WIDTH] = panel(flir, f"FLIR  t={offset:+.3f}s")
        canvas[y : y + PANEL_HEIGHT, PANEL_WIDTH : PANEL_WIDTH * 2] = panel(
            cam4, f"CAM4  t={offset:+.3f}s"
        )
    return canvas


def make_montage(
    images: Iterable[np.ndarray], columns: int = 3, thumb_width: int = 360
) -> np.ndarray | None:
    rows = list(images)
    if not rows:
        return None
    thumbs = [
        cv2.resize(image, (thumb_width, max(1, round(image.shape[0] * thumb_width / image.shape[1]))), interpolation=cv2.INTER_AREA)
        for image in rows
    ]
    thumb_height = max(image.shape[0] for image in thumbs)
    padded: list[np.ndarray] = []
    for image in thumbs:
        bottom = thumb_height - image.shape[0]
        padded.append(cv2.copyMakeBorder(image, 0, bottom, 0, 0, cv2.BORDER_CONSTANT, value=(24, 24, 24)))
    while len(padded) % columns:
        padded.append(np.full((thumb_height, thumb_width, 3), 24, dtype=np.uint8))
    return np.vstack([np.hstack(padded[index : index + columns]) for index in range(0, len(padded), columns)])


def review_markdown(records: list[Mapping[str, Any]], threshold: float) -> str:
    lines = [
        "# Direct failure review bundle",
        "",
        f"Scored threshold: `{threshold:.2f}`. Each sheet contains the exact original FLIR/CAM4 proxy frames sent at the causal cutoff.",
        "Event fields and ASR below are evaluation-side evidence only and were never sent to NInfer.",
        "",
    ]
    for record in records:
        event = record["target_event"]
        causal = record["causal_evidence"]
        lines.extend(
            [
                f"## {record['example_id']} — {record['failure_kind']}",
                "",
                f"- Prediction: `{record['predicted']}`; target: `{record['target']}`.",
                f"- Case/cutoff: `{causal['case_id']}` at `{causal['cutoff_sec']:.6f}s`.",
                f"- Target event: `{event['event_id'] or 'none'}` at `{event['event_time_sec'] if event['event_time_sec'] is not None else 'none'}`; delta `{event['delta_sec'] if event['delta_sec'] is not None else 'n/a'}`.",
                f"- Causal ASR: `{json.dumps(causal['public_asr'], ensure_ascii=False)}`.",
                f"- Original-frame sheet: `{record['sheet']}`.",
                "- Direct visual review: pending.",
                "",
            ]
        )
    return "\n".join(lines)


def run(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = args.run_dir.resolve()
    benchmark_dirs = [path.resolve() for path in args.benchmark_dir]
    run_document = read_json(run_dir / "run.json")
    threshold = float(args.threshold if args.threshold is not None else run_document.get("generation", {}).get("threshold", 0.65))
    if not 0.0 <= threshold <= 1.0:
        raise RunError("threshold must be in [0,1]")
    predictions = read_jsonl(run_dir / "predictions.jsonl")
    inputs: dict[str, dict[str, Any]] = {}
    for benchmark_dir in benchmark_dirs:
        for row in read_jsonl(benchmark_dir / "inputs.jsonl"):
            example_id = str(row.get("example_id", ""))
            if not example_id or example_id in inputs:
                raise RunError(f"benchmark inputs have missing or duplicate example ID: {example_id!r}")
            inputs[example_id] = dict(row)
    if not inputs or "" in inputs:
        raise RunError("benchmark inputs have invalid example IDs")
    failures: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    for row in predictions:
        example_id = str(row.get("example_id", ""))
        if example_id not in inputs:
            raise RunError(f"prediction input is absent from benchmark: {example_id}")
        failure = error_kind(row, threshold)
        if failure is not None:
            failures.append((dict(row), dict(inputs[example_id]), failure))

    output_dir = ensure_safe_output_dir(args.output_dir)
    if output_dir.exists():
        if not args.overwrite:
            raise RunError(f"output exists (use --overwrite): {output_dir}")
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    try:
        needed: dict[str, set[int]] = defaultdict(set)
        for _prediction, input_row, _kind in failures:
            media = input_row.get("media")
            if not isinstance(media, Mapping):
                raise RunError("benchmark input media is missing")
            frame_indices = {int(value) for value in media.get("frame_indices", [])}
            if not frame_indices:
                raise RunError("benchmark input has no frame indices")
            for key in ("flir_proxy", "cam4_proxy"):
                path = str(media.get(key, ""))
                if not path:
                    raise RunError(f"benchmark input media has no {key}")
                needed[path].update(frame_indices)
        decoded = {
            path: decode_exact_frames(Path(path), requested)
            for path, requested in sorted(needed.items())
        }
        records: list[dict[str, Any]] = []
        sheets: list[np.ndarray] = []
        sheet_dir = output_dir / "sheets"
        sheet_dir.mkdir()
        for order, (prediction, input_row, kind) in enumerate(failures, 1):
            media = input_row["media"]
            frame_indices = [int(value) for value in media["frame_indices"]]
            offsets = [float(value) for value in media["frame_offsets_sec"]]
            pairs = [
                (
                    decoded[str(media["flir_proxy"])][frame],
                    decoded[str(media["cam4_proxy"])][frame],
                )
                for frame in frame_indices
            ]
            target_data = prediction["target"]
            target = str(target_data["tool_id"]) if target_data["decision"] == "handover" else "none"
            predicted = prediction_name(prediction, threshold)
            provenance = input_row.get("provenance")
            public_context = input_row.get("public_context")
            if not isinstance(provenance, Mapping) or not isinstance(public_context, Mapping):
                raise RunError("benchmark input causal provenance/context is missing")
            case_id = str(provenance.get("case_id", ""))
            cutoff_sec = float(provenance.get("cutoff_sec"))
            event_id = target_data.get("event_id")
            event_time = target_data.get("event_time_sec")
            event_time_sec = float(event_time) if event_time is not None else None
            target_delta_sec = (
                round(event_time_sec - cutoff_sec, 9) if event_time_sec is not None else None
            )
            image = sheet(
                frames=pairs,
                offsets=offsets,
                failure_kind=kind,
                target=target,
                predicted=predicted,
                example_id=str(prediction["example_id"]),
                case_id=case_id,
                cutoff_sec=cutoff_sec,
                target_event_id=str(event_id) if event_id is not None else None,
                target_delta_sec=target_delta_sec,
            )
            filename = f"{order:03d}_{kind}_{str(prediction['example_id']).replace(':', '_')}.jpg"
            path = sheet_dir / filename
            if not cv2.imwrite(str(path), image, [int(cv2.IMWRITE_JPEG_QUALITY), 94]):
                raise RunError(f"cannot write failure sheet: {path}")
            sheets.append(image)
            records.append(
                {
                    "example_id": prediction["example_id"],
                    "failure_kind": kind,
                    "target": target,
                    "predicted": predicted,
                    "sheet": str(path),
                    "source_frame_indices": frame_indices,
                    "source_frame_offsets_sec": offsets,
                    "source_proxies": {
                        "flir": str(media["flir_proxy"]),
                        "cam4": str(media["cam4_proxy"]),
                    },
                    "target_event": {
                        "event_id": str(event_id) if event_id is not None else None,
                        "event_time_sec": event_time_sec,
                        "delta_sec": target_delta_sec,
                    },
                    "causal_evidence": {
                        "case_id": case_id,
                        "cutoff_sec": cutoff_sec,
                        "public_asr": [str(value) for value in public_context.get("asr", [])],
                    },
                    "direct_visual_review": {"status": "pending", "notes": ""},
                }
            )
        montage = make_montage(sheets)
        montage_path = output_dir / "all_failures_montage.jpg"
        if montage is not None and not cv2.imwrite(str(montage_path), montage, [int(cv2.IMWRITE_JPEG_QUALITY), 92]):
            raise RunError(f"cannot write failure montage: {montage_path}")
        review_page_dir = output_dir / "review_pages"
        review_page_dir.mkdir()
        review_pages: list[str] = []
        # Four full-resolution evidence sheets per page make it practical to
        # inspect every scored failure directly, without reading a tiny global
        # thumbnail montage as if it were source evidence.
        for page_number, start in enumerate(range(0, len(sheets), 4), 1):
            page = make_montage(sheets[start : start + 4], columns=2, thumb_width=PANEL_WIDTH * 2)
            assert page is not None
            page_path = review_page_dir / f"page_{page_number:03d}.jpg"
            if not cv2.imwrite(str(page_path), page, [int(cv2.IMWRITE_JPEG_QUALITY), 94]):
                raise RunError(f"cannot write review page: {page_path}")
            review_pages.append(str(page_path))
        index = {
            "schema": SHEET_SCHEMA,
            "run_dir": str(run_dir),
            "benchmark_dirs": [str(path) for path in benchmark_dirs],
            "threshold": threshold,
            "failure_count": len(records),
            "by_failure_kind": dict(sorted(Counter(row["failure_kind"] for row in records).items())),
            "montage": str(montage_path) if montage is not None else None,
            "review_pages": review_pages,
            "failures": records,
        }
        (output_dir / "failure_index.json").write_text(
            json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (output_dir / "failure_review.md").write_text(
            review_markdown(records, threshold), encoding="utf-8"
        )
        return {"output_dir": str(output_dir), "index": index}
    except Exception:
        shutil.rmtree(output_dir, ignore_errors=True)
        raise


def main() -> int:
    args = parse_args()
    try:
        result = run(args)
    except (RunError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    index = result["index"]
    print(
        json.dumps(
            {
                "output_dir": result["output_dir"],
                "failure_count": index["failure_count"],
                "by_failure_kind": index["by_failure_kind"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
