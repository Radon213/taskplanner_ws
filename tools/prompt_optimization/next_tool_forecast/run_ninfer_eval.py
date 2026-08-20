#!/usr/bin/env python3
"""Run the isolated next-tool benchmark against local NInfer.

Only ``inputs.jsonl`` is used to assemble model requests.  ``labels.jsonl`` is
joined after the request returns and is used only for scoring.  Raw responses
and scored results are written only to the caller-selected experiment directory.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import math
import os
import random
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

import cv2

from prompt_contract import (
    CALIBRATION_ONLY_VARIANTS,
    MODEL_ID,
    PROMPT_VARIANTS,
    SCHEMA_VERSION,
    build_messages,
    extract_json_object,
    asr_input_contract_name,
    output_contract_name,
    prompts,
    thresholded_decision,
    validate_prediction,
)


RUN_SCHEMA = "taskplanner.next_tool_forecast_ninfer_run.v1"
FROZEN_DIAGNOSTIC_LOCK_SCHEMA = "taskplanner.next_tool_forecast_failed_candidate_diagnostic.v1"
EPSILON = 1e-12
TASK_DIR = Path(__file__).resolve().parent
RUNS_ROOT = TASK_DIR / "runs"
DEFAULT_NINFER_LOCK_PATH = Path("/tmp/taskplanner-ninfer-eval.lock")
DEFAULT_MANAGER_BASE_URL = "http://127.0.0.1:8080"
DEFAULT_WORKER_BASE_URL = "http://127.0.0.1:8082"


class RunError(RuntimeError):
    """Raised for a fail-closed benchmark run error."""


class RunAborted(RunError):
    """Worker/transport failure: preserve evidence but never emit a score."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--variant",
        choices=PROMPT_VARIANTS,
        default="optimized_v1",
    )
    parser.add_argument(
        "--split", default="development_calibration",
    )
    parser.add_argument(
        "--regimes",
        default="",
        help="Optional comma-separated label regimes, e.g. anticipatory,clean_negative",
    )
    parser.add_argument("--max-examples", type=int, default=0)
    parser.add_argument("--sample-seed", type=int, default=20260818)
    parser.add_argument(
        "--base-url",
        default=DEFAULT_MANAGER_BASE_URL,
        help="Manager proxy URL used for OpenAI-compatible chat requests.",
    )
    parser.add_argument("--manager-base-url", default=DEFAULT_MANAGER_BASE_URL)
    parser.add_argument("--worker-base-url", default=DEFAULT_WORKER_BASE_URL)
    parser.add_argument("--api-key-env", default="NINFER_API_KEY")
    parser.add_argument("--model", default=MODEL_ID)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--top-p", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--max-tokens", type=int, default=128)
    parser.add_argument(
        "--enable-thinking",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="NInfer extension; off by default to isolate direct JSON prompting.",
    )
    parser.add_argument("--timeout-sec", type=float, default=120.0)
    parser.add_argument(
        "--lock-path",
        type=Path,
        default=DEFAULT_NINFER_LOCK_PATH,
        help="Shared advisory flock path for every NInfer GET/POST in this evaluator.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=3,
        help="Hard 1-3 POST cap per fresh manager-worker lifecycle batch.",
    )
    parser.add_argument("--lifecycle-timeout-sec", type=float, default=180.0)
    parser.add_argument("--threshold", type=float, default=0.65)
    parser.add_argument(
        "--frozen-candidate-lock",
        type=Path,
        default=None,
        help=(
            "Required only for the explicitly frozen, non-deployable optimized_v3 "
            "diagnostic on challenge/final holdout. It pins prompt, generation, "
            "input hashes, selected IDs, and the one allowed output path per split."
        ),
    )
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


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise RunError(f"missing benchmark file: {path}")
    rows = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise RunError(f"JSON object required: {path}:{line_number}")
            rows.append(value)
    return rows


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RunError(f"JSON object required: {path}")
    return value


@contextmanager
def ninfer_flock(lock_path: Path) -> Iterator[None]:
    """Serialize all NInfer traffic across prompt-optimization agents.

    ``fcntl.flock`` is intentionally compatible with shell ``flock`` on the
    same path.  A live run holds it for a complete fresh-worker batch: manager
    unload/load/readiness checks plus at most three POSTs.
    """

    if not lock_path.is_absolute():
        raise RunError("--lock-path must be absolute")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _request_json(
    *,
    base_url: str,
    path: str,
    timeout_sec: float,
    method: str = "GET",
    payload: Mapping[str, Any] | None = None,
    api_key: str = "",
) -> dict[str, Any]:
    body = canonical_json(dict(payload)).encode("utf-8") if payload is not None else None
    headers: dict[str, str] = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_sec) as response:
            value = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RunError(f"NInfer {method} {path} HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise RunError(f"NInfer {method} {path} failed: {exc}") from exc
    if not isinstance(value, dict):
        raise RunError(f"NInfer {method} {path} did not return an object")
    return value


def _manager_catalog_row(
    *, manager_base_url: str, model: str, timeout_sec: float, api_key: str
) -> dict[str, Any]:
    catalog = _request_json(
        base_url=manager_base_url,
        path="/v1/models",
        timeout_sec=timeout_sec,
        api_key=api_key,
    )
    rows = catalog.get("data")
    if not isinstance(rows, list):
        raise RunError("NInfer model catalog has no data list")
    match = next((row for row in rows if isinstance(row, dict) and row.get("id") == model), None)
    if match is None:
        raise RunError(f"NInfer model is absent from catalog: {model}")
    return match


def public_catalog_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Retain lifecycle proof without copying runtime configuration fields."""

    capabilities = entry.get("capabilities")
    vision = bool(capabilities.get("vision")) if isinstance(capabilities, Mapping) else False
    return {
        "id": str(entry.get("id", "")),
        "loaded": bool(entry.get("loaded")),
        "load_state": str(entry.get("load_state", "")),
        "vision_capable": vision,
    }


def _manager_is_loaded_vision(entry: Mapping[str, Any]) -> bool:
    return (
        bool(entry.get("loaded"))
        and str(entry.get("load_state", "")) == "loaded"
        and public_catalog_entry(entry)["vision_capable"]
    )


def _worker_catalog_status(
    *, worker_base_url: str, model: str, timeout_sec: float, api_key: str
) -> dict[str, Any]:
    try:
        catalog = _request_json(
            base_url=worker_base_url,
            path="/v1/models",
            timeout_sec=timeout_sec,
            api_key=api_key,
        )
    except RunError as exc:
        return {"reachable": False, "model_present": False, "error": str(exc)[:500]}
    rows = catalog.get("data")
    present = isinstance(rows, list) and any(
        isinstance(row, Mapping) and row.get("id") == model for row in rows
    )
    return {"reachable": True, "model_present": bool(present)}


def _wait_for_manager_state(
    *,
    manager_base_url: str,
    worker_base_url: str,
    model: str,
    api_key: str,
    expected: str,
    timeout_sec: float,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    deadline = time.monotonic() + timeout_sec
    last_error = ""
    while time.monotonic() < deadline:
        try:
            entry = _manager_catalog_row(
                manager_base_url=manager_base_url,
                model=model,
                timeout_sec=min(timeout_sec, 10.0),
                api_key=api_key,
            )
        except RunError as exc:
            last_error = str(exc)
            time.sleep(0.5)
            continue
        state = str(entry.get("load_state", ""))
        if state == "error":
            raise RunError(f"NInfer manager entered error state: {str(entry.get('detail', ''))[:500]}")
        if expected == "unloaded" and state == "unloaded" and not bool(entry.get("loaded")):
            return entry, None
        if expected == "loaded" and _manager_is_loaded_vision(entry):
            worker = _worker_catalog_status(
                worker_base_url=worker_base_url,
                model=model,
                timeout_sec=min(timeout_sec, 5.0),
                api_key=api_key,
            )
            if worker["reachable"] and worker["model_present"]:
                return entry, worker
            last_error = "direct worker /v1/models did not show the model"
        time.sleep(0.5)
    raise RunError(f"timed out waiting for NInfer {expected}: {last_error[:500]}")


def reload_worker_batch(
    *,
    manager_base_url: str,
    worker_base_url: str,
    model: str,
    api_key: str,
    timeout_sec: float,
) -> dict[str, Any]:
    """Unload then prove a fresh vision worker exists before one small batch."""

    _request_json(
        base_url=manager_base_url,
        path="/manager/unload",
        method="POST",
        payload={"model_id": model},
        timeout_sec=min(timeout_sec, 15.0),
        api_key=api_key,
    )
    unloaded, _unused = _wait_for_manager_state(
        manager_base_url=manager_base_url,
        worker_base_url=worker_base_url,
        model=model,
        api_key=api_key,
        expected="unloaded",
        timeout_sec=timeout_sec,
    )
    _request_json(
        base_url=manager_base_url,
        path="/manager/load",
        method="POST",
        payload={"model_id": model},
        timeout_sec=min(timeout_sec, 15.0),
        api_key=api_key,
    )
    loaded, worker = _wait_for_manager_state(
        manager_base_url=manager_base_url,
        worker_base_url=worker_base_url,
        model=model,
        api_key=api_key,
        expected="loaded",
        timeout_sec=timeout_sec,
    )
    assert worker is not None
    return {
        "manager_unloaded": public_catalog_entry(unloaded),
        "manager_loaded": public_catalog_entry(loaded),
        "direct_worker_catalog": worker,
    }


def verify_model_loaded(
    base_url: str, model: str, timeout_sec: float, lock_path: Path, api_key: str = ""
) -> dict[str, Any]:
    """Read-only preflight compatibility helper used by tests and callers."""

    with ninfer_flock(lock_path):
        entry = _manager_catalog_row(
            manager_base_url=base_url, model=model, timeout_sec=timeout_sec, api_key=api_key
        )
    if not _manager_is_loaded_vision(entry):
        raise RunError(f"NInfer model is not loaded with vision capability: {model}")
    return entry


def is_recoverable_transport_error(error: str) -> bool:
    """Legacy helper retained for unit compatibility; live runs never retry."""

    return error.startswith(("http_502:", "http_503:", "http_504:", "transport:"))


def select_rows(
    inputs: list[dict[str, Any]],
    labels: Mapping[str, dict[str, Any]],
    *,
    split: str,
    regimes: set[str],
    limit: int,
    seed: int,
) -> list[dict[str, Any]]:
    rows = []
    for row in inputs:
        example_id = str(row.get("example_id", ""))
        label = labels.get(example_id)
        if label is None:
            raise RunError(f"input has no matching label: {example_id}")
        target = label.get("target")
        if row.get("split") != split or label.get("split") != split or not isinstance(target, dict):
            continue
        if regimes and str(target.get("regime", "")) not in regimes:
            continue
        rows.append(row)
    rows.sort(key=lambda row: str(row["example_id"]))
    if limit <= 0 or len(rows) <= limit:
        return rows
    # Bounded A/B runs are balanced by outcome first, then by tool/regime. The
    # score metadata chooses the offline sample only; it never reaches a model
    # request. This avoids a seven-tool positive taxonomy crowding out `none`.
    outcome_buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        target = labels[str(row["example_id"])]["target"]
        outcome_buckets[str(target["decision"])].append(row)

    def choose_within_bucket(candidates: list[dict[str, Any]], quota: int, offset: int) -> list[dict[str, Any]]:
        groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for candidate in candidates:
            target = labels[str(candidate["example_id"])]["target"]
            groups[
                (
                    str(target["regime"]),
                    str(target["tool_id"]) if target["decision"] == "handover" else "none",
                )
            ].append(candidate)
        rng = random.Random(seed + offset)
        for group in groups.values():
            rng.shuffle(group)
        chosen: list[dict[str, Any]] = []
        keys = sorted(groups)
        while len(chosen) < quota and any(groups.values()):
            for key in keys:
                if groups[key] and len(chosen) < quota:
                    chosen.append(groups[key].pop())
        return chosen

    ordered_outcomes = sorted(outcome_buckets)
    base, remainder = divmod(limit, len(ordered_outcomes))
    chosen: list[dict[str, Any]] = []
    leftovers: list[dict[str, Any]] = []
    for index, decision in enumerate(ordered_outcomes):
        quota = base + int(index < remainder)
        bucket = choose_within_bucket(outcome_buckets[decision], quota, index)
        chosen.extend(bucket)
        selected_ids = {str(row["example_id"]) for row in bucket}
        leftovers.extend(
            row for row in outcome_buckets[decision] if str(row["example_id"]) not in selected_ids
        )
    if len(chosen) < limit:
        rng = random.Random(seed + 1000)
        rng.shuffle(leftovers)
        chosen.extend(leftovers[: limit - len(chosen)])
    return sorted(chosen, key=lambda row: str(row["example_id"]))


def _encode_jpeg(frame: Any) -> str:
    ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
    if not ok:
        raise RunError("OpenCV JPEG encoding failed")
    return "data:image/jpeg;base64," + base64.b64encode(encoded.tobytes()).decode("ascii")


def extract_exact_frames(video_path: Path, requested: set[int]) -> dict[int, str]:
    """Decode sequentially so VFR proxy frame indices stay canonical."""

    if not requested:
        return {}
    if min(requested) < 0:
        raise RunError(f"negative requested frame: {video_path}")
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise RunError(f"cannot open video: {video_path}")
    frames: dict[int, str] = {}
    index = 0
    maximum = max(requested)
    try:
        while index <= maximum:
            ok, frame = capture.read()
            if not ok:
                break
            if index in requested:
                frames[index] = _encode_jpeg(frame)
            index += 1
    finally:
        capture.release()
    missing = sorted(requested - set(frames))
    if missing:
        raise RunError(f"{video_path}: missing requested frames: {missing[:8]}")
    return frames


def decode_selected_media(rows: Iterable[Mapping[str, Any]]) -> dict[tuple[str, int], str]:
    requests: dict[str, set[int]] = defaultdict(set)
    for row in rows:
        media = row.get("media")
        if not isinstance(media, dict):
            raise RunError("input media object is missing")
        frames = media.get("frame_indices")
        if not isinstance(frames, list) or not frames:
            raise RunError("input frame_indices are missing")
        for path_key in ("flir_proxy", "cam4_proxy"):
            path = str(media.get(path_key, ""))
            if not path:
                raise RunError(f"input media missing {path_key}")
            requests[path].update(int(frame) for frame in frames)
    output: dict[tuple[str, int], str] = {}
    for path_string, frame_ids in sorted(requests.items()):
        path = Path(path_string)
        if not path.is_file():
            raise RunError(f"source video is missing: {path}")
        for frame_id, data_uri in extract_exact_frames(path, frame_ids).items():
            output[(path_string, frame_id)] = data_uri
    return output


def request_model(
    *,
    base_url: str,
    model: str,
    messages: list[dict[str, Any]],
    temperature: float,
    top_p: float,
    seed: int,
    max_tokens: int,
    enable_thinking: bool,
    timeout_sec: float,
    lock_path: Path | None = DEFAULT_NINFER_LOCK_PATH,
    api_key: str = "",
) -> tuple[dict[str, Any] | None, str, str]:
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "top_p": top_p,
        "seed": seed,
        "max_tokens": max_tokens,
        "reasoning_effort": "none",
        "enable_thinking": enable_thinking,
        "stream": False,
    }
    body = canonical_json(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(
        base_url.rstrip("/") + "/v1/chat/completions",
        data=body,
        method="POST",
        headers=headers,
    )
    try:
        if lock_path is None:
            with urllib.request.urlopen(request, timeout=timeout_sec) as response:
                raw = response.read().decode("utf-8")
        else:
            with ninfer_flock(lock_path):
                with urllib.request.urlopen(request, timeout=timeout_sec) as response:
                    raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return None, "", f"http_{exc.code}:{exc.read().decode('utf-8', errors='replace')[:500]}"
    except (urllib.error.URLError, TimeoutError) as exc:
        return None, "", f"transport:{exc}"
    try:
        response = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, raw, f"response_json:{exc.msg}"
    if not isinstance(response, dict):
        return None, raw, "response_not_object"
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return response, raw, "choices"
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return response, raw, "message"
    content = response_content(message)
    if not isinstance(content, str):
        return response, raw, "content"
    return response, raw, ""


def response_content(message: Mapping[str, Any]) -> str:
    content = message.get("content", "")
    if isinstance(content, list):
        content = "".join(
            str(item.get("text", "")) if isinstance(item, Mapping) else str(item)
            for item in content
        )
    return content if isinstance(content, str) else ""


def safe_content(response: Mapping[str, Any] | None) -> str:
    if not isinstance(response, Mapping):
        return ""
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
        return ""
    message = choices[0].get("message")
    if not isinstance(message, Mapping):
        return ""
    return response_content(message)


def binary_metrics(rows: Iterable[Mapping[str, Any]], threshold: float) -> dict[str, Any]:
    tp = fp = fn = tn = exact = positive = count = 0
    false_positive_on_none = wrong_tool_count = 0
    valid = 0
    for row in rows:
        count += 1
        target = row["target"]
        prediction = row.get("prediction")
        actual_handover = target["decision"] == "handover"
        if isinstance(prediction, dict):
            valid += 1
            predicted_handover = thresholded_decision(prediction, threshold) == "handover"
            predicted_tool = prediction["tool_id"] if predicted_handover else ""
        else:
            predicted_handover = False
            predicted_tool = ""
        if actual_handover:
            positive += 1
            if predicted_handover and predicted_tool == target["tool_id"]:
                tp += 1
                exact += 1
            else:
                fn += 1
                # A wrong tool is simultaneously a missed true class and an
                # unsupported prediction of a different tool class.
                if predicted_handover:
                    fp += 1
                    wrong_tool_count += 1
        elif predicted_handover:
            fp += 1
            false_positive_on_none += 1
        else:
            tn += 1
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    # ``count`` is the number of examples, while a wrong tool contributes one
    # FP and one FN in the class-sensitive precision/recall accounting.
    accuracy = (tp + tn) / count if count else 0.0
    # Specificity is only about rejecting actual no-handover windows. A
    # wrong-tool prediction on an actual handover is a top-1 error (and a
    # class-sensitive FP/FN), but it is not a false positive on `none`.
    specificity = tn / (tn + false_positive_on_none) if tn + false_positive_on_none else 0.0
    return {
        "count": count,
        "schema_valid_count": valid,
        "schema_valid_rate": valid / count if count else 0.0,
        "positive_count": positive,
        "exact_top1_correct": exact,
        "exact_top1_recall": exact / positive if positive else 0.0,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "accuracy": accuracy,
        "specificity": specificity,
        "balanced_accuracy": (recall + specificity) / 2.0,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "false_positive_on_none": false_positive_on_none,
        "wrong_tool_count": wrong_tool_count,
    }


def summarize(rows: list[dict[str, Any]], threshold: float) -> dict[str, Any]:
    latencies = sorted(float(row["latency_sec"]) for row in rows)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["target"]["regime"])].append(row)
    grid = {
        f"{value:.2f}": binary_metrics(rows, value)
        for value in (0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90)
    }
    confusion: Counter[str] = Counter()
    per_tool: dict[str, dict[str, int]] = defaultdict(lambda: {"support": 0, "correct": 0})
    for row in rows:
        target = row["target"]
        expected = str(target["tool_id"]) if target["decision"] == "handover" else "none"
        prediction = row.get("prediction")
        if isinstance(prediction, dict) and thresholded_decision(prediction, threshold) == "handover":
            predicted = str(prediction["tool_id"])
        else:
            predicted = "none"
        confusion[f"{expected}->{predicted}"] += 1
        per_tool[expected]["support"] += 1
        per_tool[expected]["correct"] += int(expected == predicted)
    return {
        "threshold": threshold,
        "overall": binary_metrics(rows, threshold),
        "by_regime": {key: binary_metrics(value, threshold) for key, value in sorted(grouped.items())},
        "threshold_grid": grid,
        "latency_sec": {
            "mean": sum(latencies) / len(latencies) if latencies else 0.0,
            "p95": latencies[max(0, math.ceil(len(latencies) * 0.95) - 1)] if latencies else 0.0,
        },
        "model_failure_count": sum(1 for row in rows if row.get("error")),
        "prediction_decisions": dict(
            sorted(
                Counter(
                    row["prediction"]["decision"] if isinstance(row.get("prediction"), dict) else "invalid"
                    for row in rows
                ).items()
            )
        ),
        "confusion": dict(sorted(confusion.items())),
        "per_expected_tool": {
            tool: values | {"recall": values["correct"] / values["support"] if values["support"] else 0.0}
            for tool, values in sorted(per_tool.items())
        },
    }


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(canonical_json(row) + "\n")


def _is_transport_failure(error: str) -> bool:
    return error.startswith(("http_", "transport:"))


def _http_status(error: str) -> int | None:
    if not error.startswith("http_"):
        return None
    value = error.removeprefix("http_").split(":", 1)[0]
    return int(value) if value.isdigit() else None


def validate_variant_split(variant: str, split: str) -> None:
    """Keep calibration-only hypotheses out of challenge and holdout scoring.

    The sole exception is intentionally not represented here: an explicit
    failed-candidate diagnostic is validated by
    :func:`validate_frozen_candidate_diagnostic` after its manifest, selected
    IDs, output path, prompt hashes, and generation settings are all available.
    Calling this normal guard can therefore never accidentally authorize a
    calibration-only prompt on a frozen split.
    """

    if variant in CALIBRATION_ONLY_VARIANTS and split != "development_calibration":
        raise RunError(
            f"{variant} is calibration-only and cannot run on {split}; "
            "an explicit failed-candidate diagnostic lock is required"
        )


def _frozen_lock_path(path: Path) -> Path:
    resolved = path.resolve()
    root = RUNS_ROOT.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise RunError(f"frozen candidate lock must be under {root}") from exc
    if not resolved.is_file():
        raise RunError(f"frozen candidate lock is missing: {resolved}")
    return resolved


def _expected_frozen_generation(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "temperature": args.temperature,
        "top_p": args.top_p,
        "seed": args.seed,
        "max_tokens": args.max_tokens,
        "enable_thinking": args.enable_thinking,
        "threshold": args.threshold,
    }


def validate_frozen_candidate_diagnostic(
    *,
    lock_path: Path,
    args: argparse.Namespace,
    output_dir: Path,
    inputs_path: Path,
    labels_path: Path,
    selected: list[dict[str, Any]],
) -> dict[str, Any]:
    """Fail closed around the one authorized non-deployable v3 evaluation.

    This is deliberately narrower than a generic calibration override.  It
    admits only the already-failed strict v3 candidate, only the two frozen
    post-calibration partitions, and only their predeclared output directories.
    It never turns the candidate into a selected or deployable model.
    """

    path = _frozen_lock_path(lock_path)
    document = read_json(path)
    if document.get("schema") != FROZEN_DIAGNOSTIC_LOCK_SCHEMA:
        raise RunError("unexpected frozen diagnostic lock schema")
    if document.get("candidate_status") != "failed_candidate_diagnostic":
        raise RunError("frozen lock is not a failed-candidate diagnostic")
    if document.get("deployment_status") != "non_deployable":
        raise RunError("frozen diagnostic lock must explicitly be non-deployable")
    config = document.get("frozen_config")
    targets = document.get("evaluation_targets")
    if not isinstance(config, Mapping) or not isinstance(targets, Mapping):
        raise RunError("frozen diagnostic lock lacks config or targets")
    config_digest = document.get("frozen_config_sha256")
    if not isinstance(config_digest, str) or config_digest != hashlib.sha256(
        canonical_json(config).encode("utf-8")
    ).hexdigest():
        raise RunError("frozen diagnostic config hash mismatch")
    if args.variant != "optimized_v3" or config.get("variant") != "optimized_v3":
        raise RunError("failed-candidate diagnostic is restricted to strict optimized_v3")
    if args.split not in {"development_challenge", "final_holdout"}:
        raise RunError("failed-candidate diagnostic is restricted to frozen challenge/final holdout")
    if config.get("input_contract") != asr_input_contract_name(args.variant):
        raise RunError("frozen input contract differs from optimized_v3")
    if config.get("output_contract") != output_contract_name(args.variant):
        raise RunError("frozen output contract differs from optimized_v3")
    if config.get("model") != args.model:
        raise RunError("frozen model differs from invocation")
    if config.get("generation") != _expected_frozen_generation(args):
        raise RunError("generation settings differ from frozen diagnostic config")
    expected_execution = {
        "batch_size": 1,
        "automatic_transport_retry": False,
        "manager_reload_before_each_batch": True,
        "manager_loaded_vision_check": True,
        "direct_worker_catalog_check": True,
    }
    if config.get("execution_guard") != expected_execution:
        raise RunError("frozen diagnostic execution guard is malformed")
    if args.batch_size != 1:
        raise RunError("failed-candidate diagnostic requires batch_size=1")
    if args.overwrite:
        raise RunError("failed-candidate diagnostic forbids overwrite or rerun")
    system_prompt, developer_prompt = prompts(args.variant)
    expected_prompt_hash = {
        "system": hashlib.sha256(system_prompt.encode("utf-8")).hexdigest(),
        "developer": hashlib.sha256(developer_prompt.encode("utf-8")).hexdigest(),
    }
    if config.get("prompt_sha256") != expected_prompt_hash:
        raise RunError("frozen diagnostic prompt hash differs from current source")
    target = targets.get(args.split)
    if not isinstance(target, Mapping):
        raise RunError(f"frozen lock has no authorized target for {args.split}")
    if target.get("output_dir") != str(output_dir.resolve()):
        raise RunError("output directory differs from the frozen diagnostic target")
    expected_input_hash = sha256_file(inputs_path)
    expected_label_hash = sha256_file(labels_path)
    selected_ids = [str(row.get("example_id", "")) for row in selected]
    if target.get("inputs_sha256") != expected_input_hash or target.get("labels_sha256") != expected_label_hash:
        raise RunError("frozen diagnostic manifest hash differs from invocation")
    if target.get("selected_example_ids") != selected_ids:
        raise RunError("frozen diagnostic selected IDs differ from invocation")
    if int(target.get("example_count", -1)) != len(selected_ids):
        raise RunError("frozen diagnostic example count differs from selected IDs")
    return {
        "candidate_status": "failed_candidate_diagnostic",
        "deployment_status": "non_deployable",
        "lock_path": str(path),
        "lock_sha256": sha256_file(path),
        "frozen_config_sha256": config_digest,
        "source_calibration_run": document.get("source_calibration_run"),
        "suitability": document.get("suitability"),
    }


def validate_input_contract(rows: Iterable[Mapping[str, Any]], variant: str) -> None:
    """Fail before media decoding if a variant sees the wrong ASR input shape."""

    expected = asr_input_contract_name(variant)
    expected_manifest_format = (
        "timestamped_relative" if expected == "timestamped_relative_asr" else "plain"
    )
    for row in rows:
        context = row.get("public_context")
        if not isinstance(context, Mapping):
            raise RunError("input public_context is missing")
        declared = context.get("asr_input_format", "plain")
        if declared != expected_manifest_format:
            raise RunError(
                f"{variant} requires {expected_manifest_format} ASR inputs, "
                f"but {row.get('example_id', '')} declares {declared!r}"
            )


def run(args: argparse.Namespace) -> dict[str, Any]:
    if not 0.0 <= args.threshold <= 1.0:
        raise RunError("--threshold must be in [0,1]")
    if args.max_examples < 0 or args.timeout_sec <= 0 or args.max_tokens <= 0:
        raise RunError("max examples/tokens and timeout must be positive")
    if not 1 <= args.batch_size <= 3:
        raise RunError("--batch-size must be in [1, 3]")
    if args.lifecycle_timeout_sec <= 0:
        raise RunError("--lifecycle-timeout-sec must be positive")
    # Normal calls retain the calibration-only restriction.  The only
    # post-calibration escape hatch is checked later, after the frozen lock can
    # be matched against the actual manifest and selected example IDs.
    if args.frozen_candidate_lock is None:
        validate_variant_split(args.variant, args.split)
    args.lock_path = args.lock_path.resolve()
    if not args.lock_path.is_absolute():
        raise RunError("--lock-path must be absolute")
    api_key = os.environ.get(args.api_key_env, "")
    benchmark_dir = args.benchmark_dir.resolve()
    inputs_path = benchmark_dir / "inputs.jsonl"
    labels_path = benchmark_dir / "labels.jsonl"
    inputs = read_jsonl(inputs_path)
    label_rows = read_jsonl(labels_path)
    labels = {str(row.get("example_id", "")): row for row in label_rows}
    if len(labels) != len(label_rows):
        raise RunError("duplicate label IDs")
    input_ids = {str(row.get("example_id", "")) for row in inputs}
    if not input_ids or "" in input_ids or input_ids != set(labels):
        raise RunError("benchmark input/label IDs are not a complete one-to-one match")
    regimes = {item.strip() for item in args.regimes.split(",") if item.strip()}
    selected = select_rows(
        inputs,
        labels,
        split=args.split,
        regimes=regimes,
        limit=args.max_examples,
        seed=args.sample_seed,
    )
    if not selected:
        raise RunError("no examples match the requested split/regime selection")
    validate_input_contract(selected, args.variant)
    output_dir = ensure_safe_output_dir(args.output_dir)
    frozen_candidate_diagnostic = None
    if args.frozen_candidate_lock is not None:
        frozen_candidate_diagnostic = validate_frozen_candidate_diagnostic(
            lock_path=args.frozen_candidate_lock,
            args=args,
            output_dir=output_dir,
            inputs_path=inputs_path,
            labels_path=labels_path,
            selected=selected,
        )
    if output_dir.exists():
        if not args.overwrite:
            raise RunError(f"output exists (use --overwrite): {output_dir}")
        import shutil

        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    try:
        media = decode_selected_media(selected)
        system_prompt, developer_prompt = prompts(args.variant)
        prepared: list[dict[str, Any]] = []
        for sequence, input_row in enumerate(selected, 1):
            example_id = str(input_row["example_id"])
            source_media = input_row["media"]
            frames = [int(value) for value in source_media["frame_indices"]]
            images: list[tuple[str, str]] = []
            for frame in frames:
                images.extend(
                    [
                        ("flir", media[(str(source_media["flir_proxy"]), frame)]),
                        ("cam4", media[(str(source_media["cam4_proxy"]), frame)]),
                    ]
                )
            messages = build_messages(
                variant=args.variant,
                frame_offsets_sec=source_media["frame_offsets_sec"],
                public_asr=input_row["public_context"]["asr"],
                images=images,
            )
            prepared.append(
                {
                    "sequence": sequence,
                    "input": input_row,
                    "messages": messages,
                    "request_digest": hashlib.sha256(
                        canonical_json(messages).encode("utf-8")
                    ).hexdigest(),
                }
            )

        def base_document() -> dict[str, Any]:
            document = {
                "schema": RUN_SCHEMA,
                "prompt_contract": SCHEMA_VERSION,
                "variant": args.variant,
                "output_contract": output_contract_name(args.variant),
                "input_contract": asr_input_contract_name(args.variant),
                "model": args.model,
                "benchmark": {
                    "path": str(benchmark_dir),
                    "inputs_sha256": sha256_file(inputs_path),
                    "labels_sha256": sha256_file(labels_path),
                    "split": args.split,
                    "regimes": sorted(regimes),
                    "selected_example_ids": [str(row["example_id"]) for row in selected],
                },
                "generation": {
                    "temperature": args.temperature,
                    "top_p": args.top_p,
                    "seed": args.seed,
                    "max_tokens": args.max_tokens,
                    "enable_thinking": args.enable_thinking,
                    "threshold": args.threshold,
                },
                "execution_guard": {
                    "serialized_lock_path": str(args.lock_path),
                    "batch_size": args.batch_size,
                    "manager_reload_before_each_batch": True,
                    "manager_loaded_vision_check": True,
                    "direct_worker_catalog_check": True,
                    "automatic_transport_retry": False,
                    "manager_base_url": args.manager_base_url.rstrip("/"),
                    "worker_base_url": args.worker_base_url.rstrip("/"),
                },
                "prompt_sha256": {
                    "system": hashlib.sha256(system_prompt.encode("utf-8")).hexdigest(),
                    "developer": hashlib.sha256(developer_prompt.encode("utf-8")).hexdigest(),
                },
            }
            if frozen_candidate_diagnostic is not None:
                document["frozen_candidate_diagnostic"] = frozen_candidate_diagnostic
            return document

        rows: list[dict[str, Any]] = []
        lifecycle_batches: list[dict[str, Any]] = []
        post_count = 0
        for batch_number, offset in enumerate(range(0, len(prepared), args.batch_size), 1):
            batch = prepared[offset : offset + args.batch_size]
            batch_record: dict[str, Any] = {
                "batch_number": batch_number,
                "example_ids": [str(item["input"]["example_id"]) for item in batch],
                "post_cap": args.batch_size,
                "status": "started",
            }
            try:
                with ninfer_flock(args.lock_path):
                    batch_record["fresh_worker"] = reload_worker_batch(
                        manager_base_url=args.manager_base_url,
                        worker_base_url=args.worker_base_url,
                        model=args.model,
                        api_key=api_key,
                        timeout_sec=args.lifecycle_timeout_sec,
                    )
                    for item in batch:
                        started = time.monotonic()
                        response, raw_response, response_error = request_model(
                            base_url=args.base_url,
                            model=args.model,
                            messages=item["messages"],
                            temperature=args.temperature,
                            top_p=args.top_p,
                            seed=args.seed,
                            max_tokens=args.max_tokens,
                            enable_thinking=args.enable_thinking,
                            timeout_sec=args.timeout_sec,
                            lock_path=None,
                            api_key=api_key,
                        )
                        post_count += 1
                        latency = time.monotonic() - started
                        raw_content = safe_content(response)
                        parsed, parse_error = (
                            extract_json_object(raw_content)
                            if not response_error
                            else (None, response_error)
                        )
                        prediction, validation_error = (
                            validate_prediction(parsed, variant=args.variant)
                            if parsed is not None
                            else (None, parse_error)
                        )
                        error = response_error or validation_error
                        example_id = str(item["input"]["example_id"])
                        record = {
                            "schema": RUN_SCHEMA,
                            "sequence": item["sequence"],
                            "request_attempts": 1,
                            "example_id": example_id,
                            "target": labels[example_id]["target"],
                            "prediction": prediction,
                            "error": error,
                            "http_status": _http_status(response_error),
                            "transport_error": response_error if _is_transport_failure(response_error) else "",
                            "contract_error": "" if _is_transport_failure(response_error) else error,
                            "latency_sec": round(latency, 6),
                            "request_digest": item["request_digest"],
                            "raw_content": raw_content,
                            "raw_response": raw_response,
                        }
                        rows.append(record)
                        if _is_transport_failure(response_error):
                            batch_record["status"] = "aborted_transport"
                            batch_record["failure"] = {
                                "example_id": example_id,
                                "http_status": _http_status(response_error),
                                "error": response_error[:500],
                            }
                            raise RunAborted(
                                f"batch {batch_number} transport failure at {example_id}: {response_error[:500]}"
                            )
                    manager_after = _manager_catalog_row(
                        manager_base_url=args.manager_base_url,
                        model=args.model,
                        timeout_sec=min(args.lifecycle_timeout_sec, 10.0),
                        api_key=api_key,
                    )
                    worker_after = _worker_catalog_status(
                        worker_base_url=args.worker_base_url,
                        model=args.model,
                        timeout_sec=5.0,
                        api_key=api_key,
                    )
                    batch_record["post_batch_health"] = {
                        "manager": public_catalog_entry(manager_after),
                        "direct_worker_catalog": worker_after,
                    }
                    if not _manager_is_loaded_vision(manager_after) or not (
                        worker_after["reachable"] and worker_after["model_present"]
                    ):
                        batch_record["status"] = "aborted_post_batch_health"
                        raise RunAborted(
                            f"batch {batch_number} failed post-batch manager/worker health proof"
                        )
                batch_record["status"] = "completed"
                lifecycle_batches.append(batch_record)
            except RunError as exc:
                if batch_record not in lifecycle_batches:
                    if batch_record.get("status") == "started":
                        batch_record["status"] = "aborted_lifecycle"
                    batch_record["failure_message"] = str(exc)[:500]
                    lifecycle_batches.append(batch_record)
                partial_path = output_dir / "partial_predictions.jsonl"
                write_jsonl(partial_path, rows)
                aborted = base_document() | {
                    "execution_status": "aborted",
                    "abort_reason": str(exc)[:500],
                    "no_partial_metrics_emitted": True,
                    "post_count": post_count,
                    "lifecycle_batches": lifecycle_batches,
                    "partial_raw_responses_location": str(partial_path),
                    "partial_raw_responses_sha256": sha256_file(partial_path),
                }
                (output_dir / "aborted_run.json").write_text(
                    json.dumps(aborted, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                    encoding="utf-8",
                )
                return {"output_dir": str(output_dir), "aborted": True, "abort": aborted}

        summary = summarize(rows, args.threshold)
        predictions_path = output_dir / "predictions.jsonl"
        write_jsonl(predictions_path, rows)
        run_document = base_document() | {
            "execution_status": "completed",
            "post_count": post_count,
            "lifecycle_batches": lifecycle_batches,
            "summary": summary,
            "predictions_sha256": sha256_file(predictions_path),
            "raw_responses_location": str(predictions_path),
        }
        (output_dir / "run.json").write_text(
            json.dumps(run_document, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        return {"output_dir": str(output_dir), "aborted": False, "run": run_document}
    except Exception:
        # A setup/manifest error must not leave a possibly valid-looking run.
        # Transport/lifecycle aborts take the explicit path above instead.
        import shutil

        shutil.rmtree(output_dir, ignore_errors=True)
        raise


def main() -> int:
    args = parse_args()
    try:
        result = run(args)
    except (RunError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if result.get("aborted"):
        abort = result["abort"]
        print(
            json.dumps(
                {
                    "output_dir": result["output_dir"],
                    "status": "aborted_no_partial_metrics",
                    "reason": abort["abort_reason"],
                    "post_count": abort["post_count"],
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    summary = result["run"]["summary"]
    print(
        json.dumps(
            {
                "output_dir": result["output_dir"],
                "overall": summary["overall"],
                "model_failure_count": summary["model_failure_count"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
