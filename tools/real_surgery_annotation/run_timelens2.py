#!/usr/bin/env python3
"""Run TimeLens2 locally and persist non-ground-truth temporal proposals.

The runner intentionally writes model-native interval records only.  Physical
tool identity, holder, location, and exact event completion remain unresolved
until synchronized-video review.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from .event_model import canonical_json, load_yaml


DEFAULT_MODEL = "MCG-NJU/TimeLens2-4B"


def _json_arrays(text: str) -> list[Any]:
    """Return JSON values decoded at every plausible array start."""

    decoder = json.JSONDecoder()
    values: list[Any] = []
    for match in re.finditer(r"\[", text):
        try:
            value, _ = decoder.raw_decode(text[match.start() :])
        except json.JSONDecodeError:
            continue
        values.append(value)
    return values


def extract_interval_pairs(text: str) -> list[tuple[float, float]]:
    """Extract the first valid JSON array of ``[start, end]`` pairs."""

    for value in _json_arrays(text):
        if not isinstance(value, list):
            continue
        pairs: list[tuple[float, float]] = []
        valid = True
        for item in value:
            if (
                not isinstance(item, list)
                or len(item) != 2
                or isinstance(item[0], bool)
                or isinstance(item[1], bool)
            ):
                valid = False
                break
            try:
                start = float(item[0])
                end = float(item[1])
            except (TypeError, ValueError):
                valid = False
                break
            if not math.isfinite(start) or not math.isfinite(end) or start > end:
                valid = False
                break
            pairs.append((start, end))
        if valid:
            return pairs
    raise ValueError(f"TimeLens2 response contains no valid interval array: {text!r}")


def probe_duration_sec(video_path: Path) -> float:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(completed.stdout.strip())


def build_prompt(query: str) -> str:
    return (
        f'Given the query: "{query}", return ALL time spans (in seconds) '
        "where the query is directly visible. The video is a synchronized "
        "side-by-side thyroidectomy view: CAM4 is on the left and FLIR RGB is "
        "on the right. Do not infer an event that is not visually observable.\n"
        "Output format MUST be only a JSON array of [start, end] pairs. "
        "Return [] when there is no visible evidence.\n"
    )


def _select_queries(
    query_spec: dict[str, Any], selected_ids: list[str]
) -> list[dict[str, Any]]:
    queries = list(query_spec["queries"])
    if not selected_ids:
        return queries
    by_id = {item["id"]: item for item in queries}
    unknown = sorted(set(selected_ids) - set(by_id))
    if unknown:
        raise ValueError(f"unknown query ids: {', '.join(unknown)}")
    return [by_id[query_id] for query_id in selected_ids]


def run_inference(args: argparse.Namespace) -> tuple[list[dict], dict]:
    import torch
    from qwen_vl_utils import process_vision_info
    from transformers import AutoModelForImageTextToText, AutoProcessor

    query_spec = load_yaml(args.queries)
    queries = _select_queries(query_spec, args.query_id)
    duration_sec = probe_duration_sec(args.video)

    load_started = time.perf_counter()
    model = AutoModelForImageTextToText.from_pretrained(
        args.model,
        torch_dtype="auto",
        device_map="auto",
        attn_implementation=args.attn_implementation,
        local_files_only=args.local_files_only,
    )
    processor = AutoProcessor.from_pretrained(
        args.model,
        local_files_only=args.local_files_only,
    )
    load_sec = time.perf_counter() - load_started

    records: list[dict] = []
    responses: list[dict] = []
    torch.cuda.reset_peak_memory_stats()
    # qwen-vl-utils accepts both URI and local paths.  Keep this as a plain
    # Unicode path because Decord does not unquote percent-encoded file:// URIs
    # for NAS directories containing Korean characters.
    video_path = str(args.video.resolve())

    for query in queries:
        prompt = build_prompt(query["text"])
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "video",
                        "video": video_path,
                        "fps": args.fps,
                        "min_pixels": args.min_pixels,
                        "max_pixels": args.max_pixels,
                        "total_pixels": args.total_pixels,
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ]
        text = processor.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
        )
        images, videos, video_kwargs = process_vision_info(
            messages,
            image_patch_size=16,
            return_video_kwargs=True,
            return_video_metadata=True,
        )
        if videos is not None:
            videos, video_metadatas = zip(*videos)
            videos = list(videos)
            video_metadatas = list(video_metadatas)
        else:
            video_metadatas = None
        inputs = processor(
            text=text,
            images=images,
            videos=videos,
            video_metadata=video_metadatas,
            do_resize=False,
            return_tensors="pt",
            **video_kwargs,
        ).to(model.device)

        started = time.perf_counter()
        with torch.inference_mode():
            output_ids = model.generate(
                **inputs,
                max_new_tokens=args.max_new_tokens,
                do_sample=False,
            )
        elapsed_sec = time.perf_counter() - started
        generated_ids = [
            output[len(input_ids) :]
            for input_ids, output in zip(inputs.input_ids, output_ids)
        ]
        response = processor.batch_decode(
            generated_ids,
            skip_special_tokens=True,
            clean_up_tokenization_spaces=False,
        )[0]
        intervals = extract_interval_pairs(response)
        clipped: list[tuple[float, float]] = []
        for start, end in intervals:
            if end < 0 or start > duration_sec:
                continue
            clipped.append(
                (
                    round(max(0.0, start), 6),
                    round(min(duration_sec, end), 6),
                )
            )
        for start, end in clipped:
            records.append(
                {
                    "query_id": query["id"],
                    "candidate_start_sec": start,
                    "candidate_end_sec": end,
                    "model_version": args.model,
                    "source_views": ["cam4", "flir"],
                }
            )
        responses.append(
            {
                "query_id": query["id"],
                "query": query["text"],
                "response": response,
                "interval_count": len(clipped),
                "inference_sec": round(elapsed_sec, 6),
                "input_tokens": int(inputs.input_ids.shape[-1]),
                "generated_tokens": int(generated_ids[0].shape[-1]),
            }
        )
        del inputs, output_ids, generated_ids, images, videos
        torch.cuda.empty_cache()

    report = {
        "schema": "taskplanner.timelens2_local_run_report.v1",
        "case_id": args.case_id,
        "model": args.model,
        "video": str(args.video.resolve()),
        "video_duration_sec": duration_sec,
        "sampling_fps": args.fps,
        "query_count": len(queries),
        "raw_interval_count": len(records),
        "model_load_sec": round(load_sec, 6),
        "peak_cuda_allocated_bytes": int(torch.cuda.max_memory_allocated()),
        "ground_truth_event_count": 0,
        "review_status": "proposed",
        "confidence_available": False,
        "responses": responses,
    }
    return records, report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run TimeLens2 locally and write proposed temporal intervals."
    )
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--queries", type=Path, required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--query-id", action="append", default=[])
    parser.add_argument("--fps", type=float, default=2.0)
    parser.add_argument("--min-pixels", type=int, default=32 * 32)
    parser.add_argument("--max-pixels", type=int, default=480 * 480)
    parser.add_argument("--total-pixels", type=int, default=128000 * 32 * 32)
    parser.add_argument("--max-new-tokens", type=int, default=256)
    parser.add_argument(
        "--attn-implementation",
        choices=("sdpa", "flash_attention_2", "eager"),
        default="sdpa",
    )
    parser.add_argument("--local-files-only", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.video.is_file():
        raise FileNotFoundError(args.video)
    for path in (args.output, args.report):
        if path.exists():
            raise FileExistsError(f"refusing to overwrite existing output: {path}")
    records, report = run_inference(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(canonical_json(record) + "\n" for record in records),
        encoding="utf-8",
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
