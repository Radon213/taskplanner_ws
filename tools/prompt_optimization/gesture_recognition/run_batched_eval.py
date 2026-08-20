#!/usr/bin/env python3
"""Run a multi-case gesture manifest safely against the shared NInfer worker.

The native worker has shown a process crash after several vision requests in a
single lifetime.  This runner intentionally trades speed for measurement
integrity: it holds the common evaluation lock, reloads the worker before each
small batch, verifies both manager and worker readiness, and records a failed
transport rather than converting it into a model score.

It is evaluation-only.  No manifest label, event, case, or timestamp is sent
to the VLM; those fields remain local to the evaluator.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from tools.prompt_optimization.gesture_recognition import gesture_prompt_eval as gesture
from tools.prompt_optimization.gesture_recognition.prompts import PROMPTS


DEFAULT_MODEL_ID = "qwen3.6-35b-a3b"
DEFAULT_MANAGER_BASE_URL = "http://127.0.0.1:8080"
DEFAULT_WORKER_BASE_URL = "http://127.0.0.1:8082"
DEFAULT_LOCK_PATH = Path("/tmp/taskplanner-ninfer-eval.lock")
EXECUTION_SCHEMA = "taskplanner.gesture_prompt_eval_execution.v1"


@contextmanager
def evaluation_lock(path: Path) -> Iterator[None]:
    """Use the same advisory lock as the other prompt evaluators."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _request_json(
    *,
    url: str,
    api_key: str,
    method: str = "GET",
    payload: Mapping[str, Any] | None = None,
    timeout_sec: float = 15.0,
) -> dict[str, Any]:
    body = None
    headers: dict[str, str] = {}
    if payload is not None:
        body = gesture.canonical_json(dict(payload)).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout_sec) as response:
            decoded = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"NInfer HTTP {exc.code} at {url}: {detail}") from exc
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"NInfer request failed at {url}: {exc}") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError(f"NInfer returned a non-object JSON payload at {url}")
    return decoded


def _catalog_row(
    *, manager_base_url: str, model_id: str, api_key: str
) -> dict[str, Any] | None:
    payload = _request_json(
        url=manager_base_url.rstrip("/") + "/v1/models", api_key=api_key
    )
    data = payload.get("data", [])
    if not isinstance(data, list):
        raise RuntimeError("NInfer manager catalog data must be a list")
    for item in data:
        if isinstance(item, dict) and item.get("id") == model_id:
            return item
    return None


def _worker_has_model(
    *, worker_base_url: str, model_id: str, api_key: str
) -> bool:
    try:
        payload = _request_json(
            url=worker_base_url.rstrip("/") + "/v1/models",
            api_key=api_key,
            timeout_sec=3.0,
        )
    except RuntimeError:
        return False
    data = payload.get("data", [])
    return isinstance(data, list) and any(
        isinstance(item, dict) and item.get("id") == model_id for item in data
    )


def _wait_for_state(
    *,
    manager_base_url: str,
    worker_base_url: str,
    model_id: str,
    api_key: str,
    expected_state: str,
    timeout_sec: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_sec
    last_detail = ""
    while time.monotonic() < deadline:
        row = _catalog_row(
            manager_base_url=manager_base_url,
            model_id=model_id,
            api_key=api_key,
        )
        if row is None:
            raise RuntimeError(f"model vanished from NInfer manager catalog: {model_id}")
        state = str(row.get("load_state", ""))
        last_detail = str(row.get("detail", ""))
        if state == "error":
            raise RuntimeError(f"NInfer manager entered error state: {last_detail}")
        if expected_state == "unloaded":
            if state == "unloaded" and not bool(row.get("loaded")):
                return row
        elif (
            state == "loaded"
            and bool(row.get("loaded"))
            and _worker_has_model(
                worker_base_url=worker_base_url,
                model_id=model_id,
                api_key=api_key,
            )
        ):
            return row
        time.sleep(0.5)
    raise TimeoutError(
        f"timed out waiting for NInfer {expected_state}: {model_id}; {last_detail}"
    )


def reload_worker(
    *,
    manager_base_url: str,
    worker_base_url: str,
    model_id: str,
    api_key: str,
    timeout_sec: float,
) -> dict[str, Any]:
    """Start a known-fresh worker and prove that the proxy target is ready."""

    unload_url = manager_base_url.rstrip("/") + "/manager/unload"
    load_url = manager_base_url.rstrip("/") + "/manager/load"
    _request_json(
        url=unload_url,
        method="POST",
        payload={"model_id": model_id},
        api_key=api_key,
    )
    _wait_for_state(
        manager_base_url=manager_base_url,
        worker_base_url=worker_base_url,
        model_id=model_id,
        api_key=api_key,
        expected_state="unloaded",
        timeout_sec=timeout_sec,
    )
    _request_json(
        url=load_url,
        method="POST",
        payload={"model_id": model_id},
        api_key=api_key,
    )
    return _wait_for_state(
        manager_base_url=manager_base_url,
        worker_base_url=worker_base_url,
        model_id=model_id,
        api_key=api_key,
        expected_state="loaded",
        timeout_sec=timeout_sec,
    )


def _selected_by_case(
    *, manifest_path: Path, split: str, case_ids: Sequence[str]
) -> dict[str, list[dict[str, Any]]]:
    samples = gesture.load_jsonl(manifest_path)
    requested = frozenset(case_ids)
    selected: dict[str, list[dict[str, Any]]] = {}
    for sample in samples:
        if sample.get("schema") != gesture.SAMPLE_SCHEMA:
            raise ValueError(f"unexpected manifest schema: {sample.get('schema')!r}")
        case_id = str(sample.get("case_id", ""))
        if requested and case_id not in requested:
            continue
        if split != "all" and sample.get("split") != split:
            continue
        selected.setdefault(case_id, []).append(sample)
    if not selected:
        raise ValueError("no samples selected from manifest")
    return {case_id: selected[case_id] for case_id in sorted(selected)}


def _prediction_path(
    *, output_root: Path, prompt_version: str, split: str, case_id: str, offset: int
) -> Path:
    return (
        output_root
        / "predictions"
        / prompt_version
        / split
        / case_id
        / f"offset-{offset:04d}.jsonl"
    )


def _load_completed_batch(path: Path, expected_count: int) -> list[dict[str, Any]] | None:
    if not path.is_file():
        return None
    records = gesture.load_jsonl(path)
    if len(records) != expected_count:
        raise ValueError(
            f"refusing to reuse incomplete batch {path}: {len(records)} != {expected_count}"
        )
    return records


def _run_one_batch(
    *,
    args: argparse.Namespace,
    case_id: str,
    offset: int,
    count: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Reload, request up to three samples, and record any transport failures."""

    started = time.time()
    with evaluation_lock(args.lock_path):
        ready_row = reload_worker(
            manager_base_url=args.manager_base_url,
            worker_base_url=args.worker_base_url,
            model_id=args.model_id,
            api_key=args.api_key,
            timeout_sec=args.lifecycle_timeout_sec,
        )
        records = gesture.run_manifest(
            manifest_path=args.manifest,
            video_path=args.video_root / case_id / "review_corrected.mp4",
            image_dir=args.output_root / "images" / case_id,
            base_url=args.base_url,
            model_id=args.model_id,
            prompt_version=args.prompt_version,
            api_key=args.api_key,
            timeout_sec=args.timeout_sec,
            split=args.split,
            case_id=case_id,
            input_variant=args.input_variant,
            offset=offset,
            limit=count,
        )
        post_row = _catalog_row(
            manager_base_url=args.manager_base_url,
            model_id=args.model_id,
            api_key=args.api_key,
        )
        worker_ready = _worker_has_model(
            worker_base_url=args.worker_base_url,
            model_id=args.model_id,
            api_key=args.api_key,
        )
    runtime = {
        "ready_load_state": ready_row.get("load_state"),
        "post_load_state": None if post_row is None else post_row.get("load_state"),
        "post_worker_ready": worker_ready,
        "wall_time_sec": round(time.time() - started, 3),
    }
    return records, runtime


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--video-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--prompt-version", choices=sorted(PROMPTS), required=True)
    parser.add_argument(
        "--input-variant",
        choices=sorted(gesture.INPUT_VARIANTS),
        default="causal_right_detail_pair",
    )
    parser.add_argument(
        "--split",
        choices=("all", "calibration", "within_case_challenge"),
        default="all",
    )
    parser.add_argument("--case", action="append", dest="cases", default=[])
    parser.add_argument("--batch-size", type=int, default=3)
    parser.add_argument("--base-url", default=DEFAULT_MANAGER_BASE_URL)
    parser.add_argument("--manager-base-url", default=DEFAULT_MANAGER_BASE_URL)
    parser.add_argument("--worker-base-url", default=DEFAULT_WORKER_BASE_URL)
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID)
    parser.add_argument("--api-key-env", default="NINFER_API_KEY")
    parser.add_argument("--timeout-sec", type=float, default=180.0)
    parser.add_argument("--lifecycle-timeout-sec", type=float, default=180.0)
    parser.add_argument("--lock-path", type=Path, default=DEFAULT_LOCK_PATH)
    parser.add_argument("--max-batches", type=int)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.batch_size <= 3:
        parser.error("--batch-size must be between 1 and 3 for the worker lifecycle guard")
    if args.timeout_sec <= 0.0 or args.lifecycle_timeout_sec <= 0.0:
        parser.error("timeouts must be positive")
    if args.max_batches is not None and args.max_batches < 1:
        parser.error("--max-batches must be positive when supplied")
    args.api_key = os.environ.get(args.api_key_env, "")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    selected_by_case = _selected_by_case(
        manifest_path=args.manifest, split=args.split, case_ids=args.cases
    )
    execution_batches: list[dict[str, Any]] = []
    completed_batches = 0
    halted_reason = ""
    for case_id, samples in selected_by_case.items():
        for offset in range(0, len(samples), args.batch_size):
            if args.max_batches is not None and completed_batches >= args.max_batches:
                break
            count = min(args.batch_size, len(samples) - offset)
            prediction_path = _prediction_path(
                output_root=args.output_root,
                prompt_version=args.prompt_version,
                split=args.split,
                case_id=case_id,
                offset=offset,
            )
            existing = None if args.force else _load_completed_batch(prediction_path, count)
            if existing is None:
                records, runtime = _run_one_batch(
                    args=args, case_id=case_id, offset=offset, count=count
                )
                if len(records) != count:
                    raise RuntimeError(
                        f"batch returned {len(records)} records; expected {count}"
                    )
                for record in records:
                    record["retry_count"] = 0
                gesture.write_jsonl(prediction_path, records, overwrite=True)
                transport_failures = sum(bool(record.get("transport_error")) for record in records)
                status = "completed"
            else:
                records = existing
                runtime = {"reused": True}
                transport_failures = sum(bool(record.get("transport_error")) for record in records)
                status = "reused"
            execution_batches.append(
                {
                    "case_id": case_id,
                    "offset": offset,
                    "sample_count": count,
                    "prediction_path": str(prediction_path),
                    "transport_failure_count": transport_failures,
                    "status": status,
                    "runtime": runtime,
                }
            )
            completed_batches += 1
            print(
                gesture.canonical_json(
                    {
                        "case_id": case_id,
                        "offset": offset,
                        "sample_count": count,
                        "status": status,
                        "transport_failure_count": transport_failures,
                    }
                ),
                flush=True,
            )
            # A request-level 502 or a vanished direct worker is not a model
            # prediction.  Preserve the per-batch evidence for debugging, but
            # stop before another fresh lifecycle can turn a partial run into
            # an apparently scoreable evaluation.  The report builder rejects
            # this execution status.
            if transport_failures:
                halted_reason = (
                    f"transport failure in {case_id} offset {offset}; "
                    "partial execution is not scoreable"
                )
            elif not bool(runtime.get("post_worker_ready", True)):
                halted_reason = (
                    f"direct worker lost after {case_id} offset {offset}; "
                    "partial execution is not scoreable"
                )
            elif runtime.get("post_load_state") not in {None, "loaded"}:
                halted_reason = (
                    f"manager was not loaded after {case_id} offset {offset}; "
                    "partial execution is not scoreable"
                )
            if halted_reason:
                break
        if halted_reason:
            break
        if args.max_batches is not None and completed_batches >= args.max_batches:
            break

    execution = {
        "schema": EXECUTION_SCHEMA,
        "ground_truth_usage": "evaluation_only",
        "may_publish_runtime": False,
        "manifest": str(args.manifest),
        "prompt_version": args.prompt_version,
        "model_id": args.model_id,
        "split": args.split,
        "batch_size": args.batch_size,
        "status": "halted" if halted_reason else "completed",
        "halt_reason": halted_reason,
        "scoreable": not bool(halted_reason),
        "batches": execution_batches,
        "total_sample_count": sum(batch["sample_count"] for batch in execution_batches),
        "transport_failure_count": sum(
            batch["transport_failure_count"] for batch in execution_batches
        ),
    }
    gesture.write_json(
        args.output_root / "execution" / args.prompt_version / f"{args.split}.json",
        execution,
        overwrite=True,
    )
    if halted_reason:
        print(gesture.canonical_json({"status": "halted", "reason": halted_reason}), flush=True)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
