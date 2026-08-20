#!/usr/bin/env python3
"""Run one label-free fresh-worker probe for a previously halted Mayo sample.

This is a transport/reproducibility diagnostic, not an accuracy evaluation.  It
rebuilds the exact request body from the immutable local frame and refuses to
POST unless its SHA-256 matches the selected input record from the halted run.
The output deliberately contains no reviewed target label or score.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import mayo_prompt_eval as evaluator
import mayo_pixel_preprocess as pixels


DEFAULT_OUTPUT_ROOT = Path(__file__).resolve().parent / "runs"
DEFAULT_TARGET_SAMPLE_ID = "0704_5-initial-crop-03"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _image_metadata(label: str, image_bytes: bytes, mime_type: str) -> dict[str, Any]:
    try:
        import cv2
        import numpy as np
    except ImportError as exc:  # pragma: no cover - environment-specific
        raise evaluator.EvaluationError("OpenCV is required for probe image dimensions") from exc
    image = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise evaluator.EvaluationError("could not decode probe JPEG")
    height, width = image.shape[:2]
    return {
        "label": label,
        "mime_type": mime_type,
        "byte_length": len(image_bytes),
        "sha256": sha256_bytes(image_bytes),
        "width_px": int(width),
        "height_px": int(height),
    }


def _non_image_request_sha256(body: dict[str, Any]) -> str:
    """Hash the request after replacing only data-URL image bytes."""

    normalized = json.loads(json.dumps(body, ensure_ascii=False, sort_keys=True))
    messages = normalized.get("messages") if isinstance(normalized, dict) else None
    if not isinstance(messages, list):
        raise evaluator.EvaluationError("request has no messages for non-image hash")
    image_count = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "image_url":
                continue
            image_url = block.get("image_url")
            if not isinstance(image_url, dict) or not isinstance(image_url.get("url"), str):
                raise evaluator.EvaluationError("request image block is malformed")
            image_url["url"] = f"<image-data-url-{image_count}>"
            image_count += 1
    if not image_count:
        raise evaluator.EvaluationError("request has no image for non-image hash")
    return sha256_bytes(
        json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def _transform_images(
    images: list[tuple[str, bytes, str]],
    *,
    pixel_variant: str,
    square_size: int,
) -> tuple[list[tuple[str, bytes, str]], list[dict[str, Any]]]:
    transformed: list[tuple[str, bytes, str]] = []
    artifacts: list[dict[str, Any]] = []
    for label, image_bytes, mime_type in images:
        if pixel_variant == "reencode_q95":
            result = pixels.deterministic_jpeg_reencode(image_bytes, jpeg_quality=95)
        elif pixel_variant == "letterbox_512":
            result = pixels.fixed_square_letterbox_jpeg(
                image_bytes,
                square_size=square_size,
                jpeg_quality=95,
                padding_bgr=(0, 0, 0),
            )
        else:  # pragma: no cover - argparse keeps this closed
            raise evaluator.EvaluationError(f"unsupported pixel variant: {pixel_variant}")
        transformed.append((label, result.image_bytes, mime_type))
        artifacts.append({"label": label, **result.metadata})
    return transformed, artifacts


def _write_pixel_artifacts(
    *,
    output_dir: Path,
    original_images: list[tuple[str, bytes, str]],
    transformed_images: list[tuple[str, bytes, str]],
) -> dict[str, Any]:
    """Save source/normalized JPEGs and a visual comparison outside model input."""

    try:
        import cv2
    except ImportError as exc:  # pragma: no cover - environment-specific
        raise evaluator.EvaluationError("OpenCV is required for visual comparison") from exc
    original_paths: list[dict[str, str]] = []
    transformed_paths: list[dict[str, str]] = []
    panels = []
    for index, ((label, original, _mime), (_label2, transformed, _mime2)) in enumerate(
        zip(original_images, transformed_images), 1
    ):
        original_name = f"source_{index:02d}_{label}.jpg"
        transformed_name = f"normalized_{index:02d}_{label}.jpg"
        (output_dir / original_name).write_bytes(original)
        (output_dir / transformed_name).write_bytes(transformed)
        original_paths.append({"label": label, "image": original_name})
        transformed_paths.append({"label": label, "image": transformed_name})
        source_image = pixels.decode_jpeg_bgr(original)
        normalized_image = pixels.decode_jpeg_bgr(transformed)
        panel_height = max(source_image.shape[0], normalized_image.shape[0])
        source_panel = cv2.copyMakeBorder(
            source_image,
            0,
            panel_height - source_image.shape[0],
            0,
            0,
            cv2.BORDER_CONSTANT,
            value=(0, 0, 0),
        )
        normalized_panel = cv2.copyMakeBorder(
            normalized_image,
            0,
            panel_height - normalized_image.shape[0],
            0,
            0,
            cv2.BORDER_CONSTANT,
            value=(0, 0, 0),
        )
        panels.append(cv2.hconcat([source_panel, normalized_panel]))
    if not panels:
        raise evaluator.EvaluationError("no images available for visual comparison")
    comparison = cv2.vconcat(panels)
    comparison_name = "pixel_comparison.jpg"
    if not cv2.imwrite(str(output_dir / comparison_name), comparison):
        raise evaluator.EvaluationError("could not write pixel comparison")
    return {
        "source_images": original_paths,
        "normalized_images": transformed_paths,
        "visual_comparison": comparison_name,
        "boundary": "offline review only; never model input",
    }


def worker_process_snapshot(model_id: str) -> list[dict[str, Any]]:
    """Capture only non-secret worker identity/state evidence from /proc."""

    rows: list[dict[str, Any]] = []
    for proc_dir in Path("/proc").glob("[0-9]*"):
        try:
            pid = int(proc_dir.name)
            command_line = (proc_dir / "cmdline").read_bytes()
            argv = [part.decode("utf-8", errors="replace") for part in command_line.split(b"\0") if part]
        except (OSError, ValueError):
            continue
        if not argv or not any(Path(part).name == "ninfer-serve" for part in argv):
            continue
        if "--model-id" not in argv:
            continue
        model_index = argv.index("--model-id")
        if model_index + 1 >= len(argv) or argv[model_index + 1] != model_id:
            continue
        state = ""
        try:
            for line in (proc_dir / "status").read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("State:"):
                    state = line.split(":", 1)[1].strip()
                    break
        except OSError:
            state = "unreadable"
        executable_sha256 = ""
        executable_name = ""
        executable_error = ""
        try:
            executable_link = proc_dir / "exe"
            executable_name = Path(os.readlink(executable_link)).name
            # Keep the /proc path while reading: resolving it first can point
            # outside this mount namespace even though the running executable
            # itself remains readable through /proc/<pid>/exe.
            executable_sha256 = evaluator.sha256_file(executable_link)
        except OSError as exc:
            executable_error = str(exc)[:300]
        rows.append(
            {
                "pid": pid,
                "state": state,
                "command_line_sha256": sha256_bytes(command_line),
                "executable_name": executable_name,
                "executable_sha256": executable_sha256,
                "executable_error": executable_error,
            }
        )
    return sorted(rows, key=lambda row: int(row["pid"]))


def _sanitized_catalog(session: evaluator.NInferEvalSession, reason: str) -> dict[str, Any]:
    """Read manager catalog under the session lock without persisting secrets."""

    record = session._manager_catalog_record(reason, require_loaded=False)
    return {
        key: record.get(key)
        for key in (
            "reason",
            "catalog_valid",
            "model_present",
            "model_loaded",
            "capability",
            "load_state",
            "action",
            "error",
        )
        if key in record
    }


def _find_prior_request_sha256(path: Path, sample_id: str) -> str:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise evaluator.EvaluationError(f"cannot read halted result: {path}") from exc
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        raise evaluator.EvaluationError("halted result has no records")
    for record in records:
        if not isinstance(record, dict):
            continue
        input_record = record.get("input")
        if isinstance(input_record, dict) and input_record.get("sample_id") == sample_id:
            request_sha256 = input_record.get("request_sha256")
            if isinstance(request_sha256, str) and request_sha256:
                return request_sha256
    raise evaluator.EvaluationError(f"halted result has no input hash for {sample_id}")


def _write_new(path: Path, payload: dict[str, Any]) -> None:
    if path.exists():
        raise evaluator.EvaluationError(f"refusing to overwrite probe artifact: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    events = evaluator.load_events(args.events)
    samples = evaluator.make_calibration_samples(events)
    sample = next((row for row in samples if row.sample_id == args.sample_id), None)
    if sample is None:
        raise evaluator.EvaluationError(f"probe sample is not in calibration suite: {args.sample_id}")
    wanted_indices = set(sample.frame_indices)
    frames = evaluator._decode_cam4_frames(args.bag, wanted_indices)
    original_images = evaluator.images_for(sample, frames)
    original_body, original_context, original_prompt = evaluator.build_request_body(
        sample=sample,
        variant=args.variant,
        images=original_images,
        model_id=args.model_id,
    )
    original_request_sha256 = hashlib.sha256(
        json.dumps(original_body, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    prior_request_sha256 = _find_prior_request_sha256(args.halted_result, sample.sample_id)
    if original_request_sha256 != prior_request_sha256:
        raise evaluator.EvaluationError(
            "rebuilt original request hash differs from halted crop input; refusing live POST"
        )
    transformed_images, pixel_transforms = _transform_images(
        original_images,
        pixel_variant=args.pixel_variant,
        square_size=args.square_size,
    )
    body, context, prompt = evaluator.build_request_body(
        sample=sample,
        variant=args.variant,
        images=transformed_images,
        model_id=args.model_id,
    )
    if context != original_context or prompt != original_prompt:
        raise evaluator.EvaluationError("pixel probe changed task context or system prompt")
    original_non_image_sha256 = _non_image_request_sha256(original_body)
    transformed_non_image_sha256 = _non_image_request_sha256(body)
    if original_non_image_sha256 != transformed_non_image_sha256:
        raise evaluator.EvaluationError("pixel probe changed a non-image request field")
    transformed_request_sha256 = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_path = args.output_root / f"{run_id}_mayo_repro_probe" / "probe.json"
    if output_path.parent.exists():
        raise evaluator.EvaluationError(f"refusing to overwrite probe artifact directory: {output_path.parent}")
    output_path.parent.mkdir(parents=True)
    pixel_artifacts = _write_pixel_artifacts(
        output_dir=output_path.parent,
        original_images=original_images,
        transformed_images=transformed_images,
    )
    artifact: dict[str, Any] = {
        "schema": "taskplanner.mayo_repro_probe.v2",
        "purpose": "one_post_transport_reproducibility_only_no_accuracy_scoring",
        "status": "pending",
        "model": args.model_id,
        "variant": args.variant,
        "pixel_variant": args.pixel_variant,
        "input": {
            "sample_id": sample.sample_id,
            "mode": sample.mode,
            "request_context": context,
            "system_prompt": prompt,
            "original_request_sha256": original_request_sha256,
            "matches_halted_original_request_sha256": True,
            "transformed_request_sha256": transformed_request_sha256,
            "original_non_image_request_sha256": original_non_image_sha256,
            "transformed_non_image_request_sha256": transformed_non_image_sha256,
            "non_image_request_hash_equal": True,
            "original_images": [
                _image_metadata(label, image_bytes, mime_type)
                for label, image_bytes, mime_type in original_images
            ],
            "transformed_images": [
                _image_metadata(label, image_bytes, mime_type)
                for label, image_bytes, mime_type in transformed_images
            ],
            "pixel_transforms": pixel_transforms,
        },
        "pixel_artifacts": pixel_artifacts,
        "scoring": {"performed": False, "reason": "transport probe has no GT or metric"},
        "runtime": {
            "shared_lock_path": str(args.lock_path),
            "batch_size": 1,
            "manager_endpoint": args.base_url.rstrip("/"),
            "direct_worker_endpoint": args.worker_base_url.rstrip("/"),
            "snapshots": {},
        },
        "response": {
            "raw_model_response": "",
            "parsed_semantic_output": None,
            "latency_sec": 0.0,
            "request_error": "",
        },
    }
    if args.dry_run:
        artifact["status"] = "validated_no_post"
        artifact["halt_reason"] = ""
        artifact["runtime"]["dry_run"] = True
        _write_new(output_path, artifact)
        return {
            "output": str(output_path),
            "status": artifact["status"],
            "inference_http_request_count": 0,
        }, 0
    api_key = os.environ.get(args.api_key_env, "")
    session = evaluator.NInferEvalSession(
        base_url=args.base_url,
        worker_base_url=args.worker_base_url,
        api_key=api_key,
        model_id=args.model_id,
        timeout_sec=args.timeout_sec,
        lifecycle_timeout_sec=args.lifecycle_timeout_sec,
        lock_path=args.lock_path,
        batch_size=1,
    )
    exit_code = 0
    try:
        session.require_manager_catalog("probe_preflight_manager_catalog")
        artifact["runtime"]["snapshots"]["pre_lifecycle_worker_processes"] = worker_process_snapshot(args.model_id)
        with session.fresh_batch(batch_index=1, sample_ids=[sample.sample_id]):
            artifact["runtime"]["snapshots"]["post_lifecycle_pre_post_manager"] = _sanitized_catalog(
                session, "probe_post_lifecycle_pre_post_manager_catalog"
            )
            artifact["runtime"]["snapshots"]["post_lifecycle_pre_post_direct_worker"] = session.check_direct_worker(
                "probe_post_lifecycle_pre_post_direct_worker"
            )
            artifact["runtime"]["snapshots"]["post_lifecycle_pre_post_worker_processes"] = worker_process_snapshot(args.model_id)
            try:
                raw_text, latency_sec, _retry_count = session.request_json(body, retries=0)
                artifact["response"].update(
                    {
                        "raw_model_response": raw_text,
                        "parsed_semantic_output": evaluator.parse_model_json(raw_text),
                        "latency_sec": round(latency_sec, 6),
                    }
                )
                artifact["runtime"]["snapshots"]["post_success_worker_processes"] = worker_process_snapshot(args.model_id)
            except evaluator.EvaluationError as exc:
                artifact["response"]["request_error"] = str(exc)
                artifact["runtime"]["snapshots"]["post_failure_worker_processes"] = worker_process_snapshot(args.model_id)
                raise
    except evaluator.EvaluationError as exc:
        artifact["status"] = "halted"
        artifact["halt_reason"] = str(exc)
        exit_code = 2
    else:
        artifact["status"] = "completed"
        artifact["halt_reason"] = ""
    artifact["runtime"].update(
        {
            "health_history": session.health_history,
            "direct_worker_health_history": session.worker_health_history,
            "batches": session.batch_history,
            "inference_http_request_count": session.total_inference_requests,
            "post_run_worker_processes": worker_process_snapshot(args.model_id),
        }
    )
    _write_new(output_path, artifact)
    return {
        "output": str(output_path),
        "status": artifact["status"],
        "inference_http_request_count": session.total_inference_requests,
    }, exit_code


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--halted-result", type=Path, required=True)
    parser.add_argument("--sample-id", default=DEFAULT_TARGET_SAMPLE_ID)
    parser.add_argument("--variant", choices=("optimized_v2",), default="optimized_v2")
    parser.add_argument(
        "--pixel-variant",
        choices=("reencode_q95", "letterbox_512"),
        required=True,
    )
    parser.add_argument("--square-size", type=int, default=512)
    parser.add_argument("--bag", type=Path, default=evaluator.DEFAULT_BAG)
    parser.add_argument("--events", type=Path, default=evaluator.DEFAULT_EVENTS)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--model-id", default="qwen3.6-35b-a3b")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--worker-base-url", default=evaluator.DEFAULT_WORKER_BASE_URL)
    parser.add_argument("--api-key-env", default="NINFER_API_KEY")
    parser.add_argument("--timeout-sec", type=float, default=180.0)
    parser.add_argument("--lifecycle-timeout-sec", type=float, default=180.0)
    parser.add_argument("--lock-path", type=Path, default=evaluator.DEFAULT_LOCK_PATH)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.timeout_sec <= 0 or args.lifecycle_timeout_sec <= 0:
        parser.error("timeouts must be positive")
    if args.square_size <= 0:
        parser.error("--square-size must be positive")
    if args.pixel_variant == "letterbox_512" and args.square_size != 512:
        parser.error("letterbox_512 requires --square-size 512")
    return args


def main(argv: list[str] | None = None) -> int:
    try:
        summary, exit_code = run(parse_args(list(argv) if argv is not None else sys.argv[1:]))
    except evaluator.EvaluationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
