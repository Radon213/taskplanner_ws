#!/usr/bin/env python3
"""Run an isolated Taskplanner shadow replay and produce auditable artifacts."""

from __future__ import annotations

import argparse
import ast
from contextlib import ExitStack
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import signal
import socket
import subprocess
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import uuid

import yaml

try:
    from .shadow_contract import (
        GROUND_TRUTH_PREFIX,
        RUN_MANIFEST_SCHEMA,
        RUN_MODES,
        hashed_artifact,
        load_jsonl,
        resolve_case_reference,
        sha256_file,
        utc_now,
        validate_behavior_quality_report,
        validate_trace_records,
    )
except ImportError:  # Support direct execution from the repository root.
    from shadow_contract import (
        GROUND_TRUTH_PREFIX,
        RUN_MANIFEST_SCHEMA,
        RUN_MODES,
        hashed_artifact,
        load_jsonl,
        resolve_case_reference,
        sha256_file,
        utc_now,
        validate_behavior_quality_report,
        validate_trace_records,
    )


DEFAULT_REPLAY_TOPICS = (
    "/surgery/flir/image/compressed",
    "/surgery/transcript",
)
DEFAULT_CAM4_IMAGE_TOPIC = "/surgery/cam4/color/image/compressed"
DEFAULT_CAM4_BBOXES_TOPIC = "/surgery/cam4/tools/bboxes/json"
DEFAULT_CAM4_SEGMENTATION_TOPIC = "/surgery/cam4/tools/segmentation/json"
CRITICAL_NODES = (
    "/recorded_transcript_adapter",
    "/speech_input_adapter",
    "/real_vlm_node",
    "/or_digital_twin",
    "/bt_decision_bridge",
    "/bed_robot_arm_group_orchestrator",
    "/tree_executor",
    "/shadow_skill_sink",
    "/shadow_trace_recorder",
    "/simulation_manager",
)
STRICT_FORBIDDEN_NODE_MARKERS = (
    "reference_reconciler",
    "llm_surgeon_actor",
    "mock_surgeon",
    "surgeon_actor",
    "no_image_camera",
    "skill_bridge",
    "mock_skill",
)
TARGET_BEST_EFFORT_TRACE_COVERAGE = 0.98
MIN_BEST_EFFORT_TRACE_COVERAGE = 0.95
SHADOW_GROUND_TRUTH_PREFIX = "/shadow/ground_truth"
PROTECTED_GROUND_TRUTH_PREFIXES = (
    GROUND_TRUTH_PREFIX,
    SHADOW_GROUND_TRUTH_PREFIX,
)
GROUND_TRUTH_EVALUATION_SINK_NODES = {
    "/shadow_trace_recorder",
}
EVALUATION_SINK_ALLOWED_PUBLISHER_TOPICS = {
    "/parameter_events",
    "/rosout",
}
RUNTIME_LOCK_SCHEMA = "taskplanner.shadow_runtime_lock.v1"
RUNTIME_LOCK_DIR_ENV = "TASKPLANNER_SHADOW_RUNTIME_LOCK_DIR"


def _provider_api_key(provider_id: str) -> str:
    provider_key_names = {
        "lmstudio": "LMSTUDIO_API_KEY",
        "unsloth": "UNSLOTH_API_KEY",
        "vllm": "VLLM_API_KEY",
    }
    provider_key_name = provider_key_names.get(
        str(provider_id or "").strip().lower()
    )
    if provider_key_name:
        provider_key = os.environ.get(provider_key_name, "").strip()
        if provider_key:
            return provider_key
    return os.environ.get("VLM_API_KEY", "").strip()


def _resolve_start_phase_id(
    explicit_phase_id: str,
    annotation_manifest: dict[str, Any],
    procedure_payload: dict[str, Any] | None = None,
) -> tuple[str, str]:
    explicit = str(explicit_phase_id or "").strip()
    if explicit:
        return explicit, "cli"
    replay_profile = annotation_manifest.get("shadow_replay", {})
    if isinstance(replay_profile, dict):
        configured = str(replay_profile.get("start_phase_id", "") or "").strip()
        if configured:
            return configured, "case_manifest"
    procedure = (
        procedure_payload.get("procedure", {})
        if isinstance(procedure_payload, dict)
        else {}
    )
    configured = (
        str(procedure.get("default_phase_id", "") or "").strip()
        if isinstance(procedure, dict)
        else ""
    )
    if configured:
        return configured, "procedure_default"
    return "", "unspecified"


def _resolve_case_dir(
    repo_root: Path,
    source_bag: Path,
    requested_case_dir: Path | None,
) -> Path:
    if requested_case_dir is not None:
        return requested_case_dir.expanduser().resolve()

    cases_root = (
        repo_root / "annotations/observable_tool_events/cases"
    ).resolve()
    source_path = source_bag.expanduser().resolve()
    matches: list[Path] = []
    for component in reversed(source_path.parts):
        if not component or component == source_path.anchor:
            continue
        candidate = cases_root / component
        if candidate.is_dir() and candidate not in matches:
            matches.append(candidate)

    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(
            "source bag path matches multiple annotation cases; "
            "pass --case-dir explicitly: "
            + ", ".join(str(path) for path in matches)
        )
    raise ValueError(
        "could not infer the annotation case from the source bag path "
        f"{source_path}; place the bag under a directory named after the "
        "case or pass --case-dir explicitly"
    )


def _validate_start_phase_id(phase_id: str, procedure_path: Path) -> None:
    if not phase_id:
        return
    procedure = yaml.safe_load(procedure_path.read_text(encoding="utf-8"))
    labels = procedure.get("phase_labels", {}) if isinstance(procedure, dict) else {}
    allowed: set[str] = set()
    if isinstance(labels, dict):
        for group_name in ("normal", "interrupt"):
            group = labels.get(group_name, {})
            if isinstance(group, dict):
                allowed.update(str(candidate) for candidate in group)
    if phase_id not in allowed:
        raise ValueError(
            f"unknown shadow start phase '{phase_id}'; allowed: "
            + ", ".join(sorted(allowed))
        )


def _json_dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _command_text(command: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in command)


def _port_is_available(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", int(port)))
    except OSError:
        return False
    return True


def _select_groot2_port(requested_port: int, ros_domain_id: int) -> int:
    requested = int(requested_port)
    if requested:
        if not 1 <= requested <= 65535:
            raise ValueError("--groot2-port must be between 1 and 65535")
        if not _port_is_available(requested):
            raise RuntimeError(f"requested Groot2 port is unavailable: {requested}")
        return requested

    preferred = 20_000 + int(ros_domain_id)
    if not _port_is_available(preferred):
        raise RuntimeError(
            "derived Groot2 port is unavailable: "
            f"{preferred} (ROS_DOMAIN_ID={ros_domain_id}); refusing to "
            "fall forward because another shadow runtime may own this domain"
        )
    return preferred


def _runtime_lock_dir() -> Path:
    configured = os.environ.get(RUNTIME_LOCK_DIR_ENV, "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    uid = getattr(os, "getuid", lambda: 0)()
    return Path("/tmp") / f"taskplanner-shadow-runtime-{uid}"


def _runtime_resource_claims(
    *,
    ros_domain_id: int,
    groot2_port: int,
    rosbridge_port: int | None,
) -> list[dict[str, Any]]:
    claims = [
        {
            "key": f"ros-domain-{int(ros_domain_id)}",
            "kind": "ros_domain",
            "value": int(ros_domain_id),
            "role": "shadow_runtime",
        },
        {
            "key": f"tcp-port-{int(groot2_port)}",
            "kind": "tcp_port",
            "value": int(groot2_port),
            "role": "groot2",
        },
    ]
    if rosbridge_port is not None:
        if int(rosbridge_port) == int(groot2_port):
            raise ValueError(
                "Groot2 and rosbridge cannot share TCP port "
                f"{groot2_port}"
            )
        claims.append(
            {
                "key": f"tcp-port-{int(rosbridge_port)}",
                "kind": "tcp_port",
                "value": int(rosbridge_port),
                "role": "rosbridge",
            }
        )
    return claims


class _RuntimeResourceLock:
    """Hold host-local ROS domain and TCP resources for one shadow runtime."""

    def __init__(
        self,
        *,
        lock_dir: Path,
        claims: list[dict[str, Any]],
        owner: dict[str, Any],
    ) -> None:
        self.lock_dir = lock_dir
        self.claims = sorted(claims, key=lambda row: str(row["key"]))
        self.owner = {
            "schema": RUNTIME_LOCK_SCHEMA,
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "acquired_at": utc_now(),
            **owner,
            "resources": self.claims,
        }
        self._handles: list[tuple[Path, int]] = []

    @staticmethod
    def _read_owner(fd: int) -> dict[str, Any] | str | None:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            raw = os.read(fd, 64 * 1024).decode("utf-8").strip()
        except OSError:
            return None
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return raw
        return value if isinstance(value, dict) else raw

    def acquire(self) -> "_RuntimeResourceLock":
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        try:
            for claim in self.claims:
                lock_path = self.lock_dir / f"{claim['key']}.lock"
                fd = os.open(
                    lock_path,
                    os.O_RDWR | os.O_CREAT,
                    0o600,
                )
                try:
                    fcntl.flock(
                        fd,
                        fcntl.LOCK_EX | fcntl.LOCK_NB,
                    )
                except BlockingIOError as exc:
                    current_owner = self._read_owner(fd)
                    os.close(fd)
                    owner_text = json.dumps(
                        current_owner,
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    raise RuntimeError(
                        "shadow runtime resource is already locked: "
                        f"{claim['kind']}={claim['value']} "
                        f"(role={claim['role']}, lock={lock_path}, "
                        f"owner={owner_text}). "
                        "Use a different ROS domain/port or stop the owning "
                        "shadow replay."
                    ) from exc
                payload = {
                    **self.owner,
                    "locked_resource": claim,
                }
                encoded = (
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n"
                ).encode("utf-8")
                os.ftruncate(fd, 0)
                os.lseek(fd, 0, os.SEEK_SET)
                os.write(fd, encoded)
                os.fsync(fd)
                self._handles.append((lock_path, fd))
        except Exception:
            self.release()
            raise
        return self

    def release(self) -> None:
        while self._handles:
            _path, fd = self._handles.pop()
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def __enter__(self) -> "_RuntimeResourceLock":
        return self.acquire()

    def __exit__(self, *_args: Any) -> None:
        self.release()


def _run(
    command: list[str],
    *,
    env: dict[str, str],
    timeout_sec: float = 30.0,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout_sec,
        check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {_command_text(command)}\n"
            f"{result.stdout.strip()}"
        )
    return result


def _terminate_process(process: subprocess.Popen[Any] | None, timeout_sec: float = 15.0) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGINT)
        process.wait(timeout=timeout_sec)
        return
    except (ProcessLookupError, subprocess.TimeoutExpired):
        pass
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5.0)
        return
    except (ProcessLookupError, subprocess.TimeoutExpired):
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    process.wait(timeout=5.0)


def _load_bag_metadata(bag_dir: Path) -> dict[str, Any]:
    metadata_path = bag_dir / "metadata.yaml"
    if not metadata_path.is_file():
        raise FileNotFoundError(f"ROS bag metadata not found: {metadata_path}")
    raw = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
    info = raw.get("rosbag2_bagfile_information", {})
    if not isinstance(info, dict):
        raise ValueError(f"{metadata_path}: invalid rosbag metadata")
    relative_files = list(info.get("relative_file_paths", []))
    if len(relative_files) != 1:
        raise ValueError(
            f"{metadata_path}: shadow runner requires exactly one bag file, "
            f"found {len(relative_files)}"
        )
    bag_file = (bag_dir / str(relative_files[0])).resolve()
    if bag_file.parent != bag_dir.resolve() or not bag_file.is_file():
        raise FileNotFoundError(bag_file)
    topics: dict[str, dict[str, Any]] = {}
    for entry in info.get("topics_with_message_count", []):
        metadata = entry.get("topic_metadata", {})
        name = str(metadata.get("name", ""))
        if name:
            topics[name] = {
                "type": str(metadata.get("type", "")),
                "message_count": int(entry.get("message_count", 0)),
            }
    return {
        "metadata_path": metadata_path.resolve(),
        "bag_file": bag_file,
        "storage_identifier": str(info.get("storage_identifier", "")),
        "duration_sec": float(
            info.get("duration", {}).get("nanoseconds", 0)
        )
        / 1_000_000_000.0,
        "message_count": int(info.get("message_count", 0)),
        "topics": topics,
    }


def _reference_authority(annotation_manifest: dict[str, Any]) -> str:
    authority = str(
        annotation_manifest.get("annotation_adjudication", {}).get(
            "authority",
            "",
        )
    )
    if authority in {
        "human",
        "assistant_video_adjudication",
        "mixed",
        "none",
    }:
        return authority
    origin_counts = (
        annotation_manifest.get("annotation_adjudication", {}).get(
            "confirmed_origin_counts",
            {},
        )
    )
    origins = {key for key, value in origin_counts.items() if int(value) > 0}
    if origins == {"human_video_review"}:
        return "human"
    if origins == {"assistant_video_adjudication"}:
        return "assistant_video_adjudication"
    return "mixed" if origins else "none"


def _git_snapshot(repo_root: Path) -> dict[str, Any]:
    git = ["git", "-c", f"safe.directory={repo_root}"]
    revision = _run(
        [*git, "rev-parse", "HEAD"],
        env=os.environ.copy(),
        timeout_sec=10.0,
    ).stdout.strip()
    status = _run(
        [*git, "status", "--porcelain"],
        env=os.environ.copy(),
        timeout_sec=10.0,
    ).stdout
    return {
        "commit": revision,
        "dirty": bool(status.strip()),
        "dirty_path_count": len([line for line in status.splitlines() if line]),
    }


def _model_preflight(
    *,
    provider_id: str,
    base_url: str,
    model_id: str,
    api_key: str,
    timeout_sec: float,
) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    def fetch(url: str) -> Any:
        request = Request(url, headers=headers)
        with urlopen(request, timeout=timeout_sec) as response:
            return json.loads(response.read().decode("utf-8"))

    base_url = base_url.rstrip("/")
    url = base_url + "/v1/models"
    started = time.perf_counter()
    try:
        payload = fetch(url)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"model provider preflight failed at {url}: {exc}") from exc
    entries = payload.get("data", []) if isinstance(payload, dict) else []
    catalog_models = sorted(
        {
            str(entry.get("id", ""))
            for entry in entries
            if isinstance(entry, dict) and entry.get("id")
        }
    )

    loaded_models = list(catalog_models)
    load_state_source = "openai_models"
    load_state_verified = provider_id not in {"lmstudio"}
    if provider_id in {"auto", "vllm"}:
        manager_url = base_url + "/manager/status"
        try:
            manager_payload = fetch(manager_url)
            if isinstance(manager_payload, dict) and "state" in manager_payload:
                active_model = str(
                    manager_payload.get(
                        "model_id",
                        manager_payload.get("model", ""),
                    )
                ).strip()
                loaded_models = (
                    [active_model]
                    if (
                        str(manager_payload.get("state", "")).lower() == "loaded"
                        and active_model
                    )
                    else []
                )
                load_state_source = "vllm_manager_status"
                load_state_verified = True
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
            if provider_id == "vllm" and len(catalog_models) > 1:
                raise RuntimeError(
                    "vLLM manager load-state preflight failed at "
                    f"{manager_url}; refusing to treat catalog entries as loaded"
                )
    if provider_id in {"auto", "lmstudio"}:
        native_url = base_url + "/api/v0/models"
        try:
            native_payload = fetch(native_url)
            native_entries = (
                native_payload.get("data", [])
                if isinstance(native_payload, dict)
                else []
            )
            if any(
                isinstance(entry, dict) and "state" in entry
                for entry in native_entries
            ):
                loaded_models = sorted(
                    {
                        str(entry.get("id", ""))
                        for entry in native_entries
                        if isinstance(entry, dict)
                        and entry.get("id")
                        and str(entry.get("state", "")).lower() == "loaded"
                    }
                )
                catalog_models = sorted(
                    set(catalog_models)
                    | {
                        str(entry.get("id", ""))
                        for entry in native_entries
                        if isinstance(entry, dict) and entry.get("id")
                    }
                )
                load_state_source = "lmstudio_api_v0_models"
                load_state_verified = True
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
            if provider_id == "lmstudio":
                raise RuntimeError(
                    "LM Studio load-state preflight failed at "
                    f"{native_url}; refusing to treat catalog entries as loaded"
                )

    if model_id and model_id not in loaded_models:
        raise RuntimeError(
            f"selected model {model_id!r} is not loaded at {url}; "
            f"loaded={loaded_models}; catalog={catalog_models}"
        )
    return {
        "url": url,
        "latency_sec": round(time.perf_counter() - started, 6),
        "selected_model_loaded": model_id in loaded_models,
        "available_models": catalog_models,
        "loaded_models": loaded_models,
        "load_state_source": load_state_source,
        "load_state_verified": load_state_verified,
    }


def _validate_public_input_trace(
    trace_records: list[dict[str, Any]],
    *,
    bag_info: dict[str, Any],
    field_image_topic: str,
    source_transcript_topic: str,
    recorded_field_image_topic: str | None = None,
    cam4_image_topic: str | None = None,
    perception_bboxes_topic: str | None = None,
    perception_segmentation_topic: str | None = None,
    recorded_flir_image_topic: str | None = None,
    recorded_cam4_image_topic: str | None = None,
    recorded_perception_bboxes_topic: str | None = None,
    recorded_perception_segmentation_topic: str | None = None,
    composite_image_topic: str | None = None,
) -> dict[str, Any]:
    trace_image_topic = recorded_field_image_topic or field_image_topic
    topics = bag_info.get("topics", {})
    expected_images = int(
        topics.get(field_image_topic, {}).get("message_count", 0)
    )
    expected_transcripts = int(
        topics.get(source_transcript_topic, {}).get("message_count", 0)
    )
    recorded_images = sum(
        record.get("layer") == "input_image"
        and record.get("topic") == trace_image_topic
        for record in trace_records
    )
    recorded_source_transcripts = sum(
        record.get("layer") == "input_transcript"
        and record.get("topic") == source_transcript_topic
        for record in trace_records
    )
    admitted_speech = sum(
        record.get("layer") == "input_transcript"
        and record.get("topic") == "/surgery/audio/request_text"
        for record in trace_records
    )
    expected_flir_images = expected_images
    expected_cam4_images = int(
        topics.get(cam4_image_topic or "", {}).get("message_count", 0)
    )
    expected_bboxes = int(
        topics.get(perception_bboxes_topic or "", {}).get("message_count", 0)
    )
    expected_segmentations = int(
        topics.get(perception_segmentation_topic or "", {}).get(
            "message_count",
            0,
        )
    )

    def count_topic(topic: str | None, layers: set[str]) -> int:
        if not topic:
            return 0
        return sum(
            record.get("layer") in layers
            and record.get("topic") == topic
            for record in trace_records
        )

    recorded_flir_images = count_topic(
        recorded_flir_image_topic,
        {"normalized_input_image"},
    )
    recorded_cam4_images = count_topic(
        recorded_cam4_image_topic,
        {"normalized_input_image"},
    )
    recorded_bboxes = count_topic(
        recorded_perception_bboxes_topic,
        {"normalized_perception"},
    )
    recorded_segmentations = count_topic(
        recorded_perception_segmentation_topic,
        {"normalized_perception"},
    )
    recorded_composites = count_topic(
        composite_image_topic,
        {"vlm_model_input_image"},
    )
    recorded_preprocessed_images = sum(
        record.get("layer") == "vlm_preprocessed_input_image"
        for record in trace_records
    )
    auditable_perception_context_count = 0
    segmented_flir_context_count = 0
    for record in trace_records:
        if record.get("layer") != "vlm_request":
            continue
        compact_context = str(
            record.get("payload", {}).get("compact_json", "")
        )
        if (
            "segmentation_rle" in compact_context
            or '"counts"' in compact_context
        ):
            continue
        try:
            context = json.loads(compact_context)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        visual = context.get("visual_input", {})
        perception = context.get("observable_perception", {})
        legacy_multiview_context = (
            isinstance(visual, dict)
            and visual.get("image_source") == "composite(cam4+flir)"
            and isinstance(perception, dict)
            and isinstance(perception.get("bboxes"), dict)
            and isinstance(perception.get("segmentation"), dict)
        )
        current_fused_context = (
            isinstance(visual, dict)
            and visual.get("image_source")
            == "flir_cam4_rfdetr_segmented"
            and visual.get("image_layout") == "flir_left_cam4_right"
            and visual.get("cam4_image_forwarded_to_vlm") is True
            and isinstance(perception, dict)
            and perception.get("source") == "cam4_rfdetr_small"
            and perception.get("ground_truth") is False
            and isinstance(perception.get("alignment"), dict)
            and perception["alignment"].get("status") == "aligned"
            and isinstance(perception.get("tools"), list)
        )
        current_flir_only_context = (
            isinstance(visual, dict)
            and visual.get("image_source") == "flir_rfdetr_segmented"
            and isinstance(perception, dict)
            and perception.get("source") == "cam4_rfdetr_small"
            and perception.get("cam4_image_forwarded_to_vlm") is False
            and perception.get("ground_truth") is False
            and isinstance(perception.get("alignment"), dict)
            and perception["alignment"].get("status") == "aligned"
            and isinstance(perception.get("tools"), list)
        )
        if current_fused_context or current_flir_only_context:
            segmented_flir_context_count += 1
        if (
            legacy_multiview_context
            or current_fused_context
            or current_flir_only_context
        ):
            auditable_perception_context_count += 1

    def coverage_summary(
        expected: int,
        recorded: int,
    ) -> dict[str, int | float]:
        if expected <= 0:
            ratio = 1.0
        else:
            ratio = min(recorded, expected) / expected
        return {
            "expected": expected,
            "recorded": recorded,
            "dropped": max(expected - recorded, 0),
            "extra": max(recorded - expected, 0),
            "coverage_ratio": ratio,
        }

    def require_best_effort_coverage(
        *,
        label: str,
        expected: int,
        recorded: int,
        errors: list[str],
        warnings: list[str],
    ) -> dict[str, int | float]:
        summary = coverage_summary(expected, recorded)
        if (
            expected > 0
            and summary["coverage_ratio"]
            < MIN_BEST_EFFORT_TRACE_COVERAGE
        ):
            errors.append(
                f"{label}_coverage_below_minimum:"
                f"expected={expected},recorded={recorded},"
                f"ratio={summary['coverage_ratio']:.4f},"
                f"minimum={MIN_BEST_EFFORT_TRACE_COVERAGE:.4f}"
            )
        elif (
            expected > 0
            and summary["coverage_ratio"]
            < TARGET_BEST_EFFORT_TRACE_COVERAGE
        ):
            warnings.append(
                f"{label}_coverage_below_target:"
                f"expected={expected},recorded={recorded},"
                f"ratio={summary['coverage_ratio']:.4f},"
                f"target={TARGET_BEST_EFFORT_TRACE_COVERAGE:.4f}"
            )
        return summary

    errors: list[str] = []
    warnings: list[str] = []
    field_image_coverage = require_best_effort_coverage(
        label="field_image",
        expected=expected_images,
        recorded=recorded_images,
        errors=errors,
        warnings=warnings,
    )
    if recorded_source_transcripts != expected_transcripts:
        errors.append(
            "source_transcript_count_mismatch:"
            f"expected={expected_transcripts},"
            f"recorded={recorded_source_transcripts}"
        )
    if expected_transcripts and admitted_speech != expected_transcripts:
        errors.append(
            "admitted_speech_count_mismatch:"
            f"expected={expected_transcripts},recorded={admitted_speech}"
        )
    perception_pipeline_requested = bool(
        cam4_image_topic
        or perception_bboxes_topic
        or perception_segmentation_topic
    )
    flir_image_coverage = coverage_summary(
        expected_flir_images,
        recorded_flir_images,
    )
    if recorded_flir_image_topic:
        flir_image_coverage = require_best_effort_coverage(
            label="flir_image",
            expected=expected_flir_images,
            recorded=recorded_flir_images,
            errors=errors,
            warnings=warnings,
        )
    cam4_image_coverage = coverage_summary(
        expected_cam4_images,
        recorded_cam4_images,
    )
    if recorded_cam4_image_topic:
        cam4_image_coverage = require_best_effort_coverage(
            label="cam4_image",
            expected=expected_cam4_images,
            recorded=recorded_cam4_images,
            errors=errors,
            warnings=warnings,
        )
    bbox_coverage = coverage_summary(expected_bboxes, recorded_bboxes)
    if recorded_perception_bboxes_topic:
        bbox_coverage = require_best_effort_coverage(
            label="bbox",
            expected=expected_bboxes,
            recorded=recorded_bboxes,
            errors=errors,
            warnings=warnings,
        )
    segmentation_coverage = coverage_summary(
        expected_segmentations,
        recorded_segmentations,
    )
    if recorded_perception_segmentation_topic:
        segmentation_coverage = require_best_effort_coverage(
            label="segmentation",
            expected=expected_segmentations,
            recorded=recorded_segmentations,
            errors=errors,
            warnings=warnings,
        )
    if composite_image_topic and expected_flir_images and recorded_composites <= 0:
        errors.append("vlm_model_input_image_missing")
    if (
        segmented_flir_context_count > 0
        and recorded_composites > 0
        and recorded_preprocessed_images <= 0
    ):
        errors.append("vlm_preprocessed_input_image_missing")
    if (
        perception_pipeline_requested
        and expected_bboxes
        and auditable_perception_context_count <= 0
    ):
        errors.append("auditable_perception_context_missing")
    return {
        "ok": not errors,
        "expected_field_image_count": expected_images,
        "recorded_field_image_count": recorded_images,
        "field_image_coverage": field_image_coverage,
        "source_field_image_topic": field_image_topic,
        "recorded_field_image_topic": trace_image_topic,
        "expected_flir_image_count": expected_flir_images,
        "recorded_flir_image_count": recorded_flir_images,
        "flir_image_coverage": flir_image_coverage,
        "expected_cam4_image_count": expected_cam4_images,
        "recorded_cam4_image_count": recorded_cam4_images,
        "cam4_image_coverage": cam4_image_coverage,
        "expected_bbox_count": expected_bboxes,
        "recorded_bbox_count": recorded_bboxes,
        "bbox_coverage": bbox_coverage,
        "expected_segmentation_count": expected_segmentations,
        "recorded_segmentation_count": recorded_segmentations,
        "segmentation_coverage": segmentation_coverage,
        "minimum_best_effort_trace_coverage": (
            MIN_BEST_EFFORT_TRACE_COVERAGE
        ),
        "target_best_effort_trace_coverage": (
            TARGET_BEST_EFFORT_TRACE_COVERAGE
        ),
        "recorded_vlm_composite_count": recorded_composites,
        "recorded_vlm_preprocessed_input_count": (
            recorded_preprocessed_images
        ),
        "auditable_perception_context_count": (
            auditable_perception_context_count
        ),
        # Kept for reading older report consumers.
        "auditable_multiview_context_count": (
            auditable_perception_context_count
        ),
        "expected_source_transcript_count": expected_transcripts,
        "recorded_source_transcript_count": recorded_source_transcripts,
        "admitted_speech_count": admitted_speech,
        "errors": errors,
        "warnings": warnings,
    }


def _validate_shadow_feedback_trace(
    trace_records: list[dict[str, Any]],
    *,
    enabled: bool,
) -> dict[str, Any]:
    sink_records = [
        record.get("payload", {})
        for record in trace_records
        if record.get("layer") == "shadow_sink"
        and isinstance(record.get("payload"), dict)
    ]
    admissible_ids = {
        str(payload.get("command_id", "") or "").strip()
        for payload in sink_records
        if payload.get("status")
        in {"admissible", "instance_resolution_assumed"}
    }
    admissible_ids.discard("")
    published_ids = {
        str(payload.get("command_id", "") or "").strip()
        for payload in sink_records
        if bool(payload.get("counterfactual_feedback_published"))
    }
    published_ids.discard("")
    status_records = [
        record.get("payload", {})
        for record in trace_records
        if record.get("layer") == "skill_status"
        and isinstance(record.get("payload"), dict)
        and record.get("payload", {}).get("mode") == "shadow_counterfactual"
    ]
    completed_ids = {
        str(payload.get("command_id", "") or "").strip()
        for payload in status_records
        if payload.get("state") == "completed"
        and bool(payload.get("success"))
    }
    completed_ids.discard("")
    event_records = [
        record.get("payload", {})
        for record in trace_records
        if record.get("layer") == "skill_event"
        and isinstance(record.get("payload"), dict)
        and record.get("payload", {}).get("mode") == "shadow_counterfactual"
    ]
    event_command_ids: set[str] = set()
    ground_truth_use_count = 0
    physical_execution_attempt_count = 0
    for payload in event_records:
        detail: dict[str, Any] = {}
        try:
            parsed = json.loads(str(payload.get("detail_json", "") or "{}"))
            if isinstance(parsed, dict):
                detail = parsed
        except json.JSONDecodeError:
            pass
        command_id = str(detail.get("command_id", "") or "").strip()
        if command_id:
            event_command_ids.add(command_id)
        ground_truth_use_count += int(bool(detail.get("ground_truth_used")))
        physical_execution_attempt_count += int(
            bool(detail.get("physical_execution_attempted"))
        )
    ground_truth_use_count += sum(
        bool(payload.get("ground_truth_used"))
        for payload in sink_records
    )
    physical_execution_attempt_count += sum(
        bool(payload.get("execution_attempted"))
        for payload in sink_records
    )
    state_assumptions: list[dict[str, Any]] = []
    for record in trace_records:
        payload = record.get("payload", {})
        if (
            record.get("layer") != "reducer_event"
            or not isinstance(payload, dict)
            or payload.get("input_type") != "shadow_state_assumption"
        ):
            continue
        try:
            detail = json.loads(str(payload.get("detail_json", "") or "{}"))
        except json.JSONDecodeError:
            detail = {}
        if isinstance(detail, dict):
            state_assumptions.append(detail)
    state_assumption_ground_truth_use_count = sum(
        bool(payload.get("ground_truth_used"))
        for payload in state_assumptions
    )
    ground_truth_use_count += state_assumption_ground_truth_use_count

    errors: list[str] = []
    if enabled:
        if published_ids != admissible_ids:
            errors.append(
                "counterfactual_publish_set_mismatch:"
                f"admissible={sorted(admissible_ids)},"
                f"published={sorted(published_ids)}"
            )
        if completed_ids != admissible_ids:
            errors.append(
                "counterfactual_completion_set_mismatch:"
                f"admissible={sorted(admissible_ids)},"
                f"completed={sorted(completed_ids)}"
            )
        if not admissible_ids.issubset(event_command_ids):
            errors.append(
                "counterfactual_event_set_missing:"
                f"admissible={sorted(admissible_ids)},"
                f"events={sorted(event_command_ids)}"
            )
    elif published_ids or completed_ids or event_records:
        errors.append("counterfactual_feedback_present_while_disabled")
    if ground_truth_use_count:
        errors.append(
            f"counterfactual_feedback_used_ground_truth:{ground_truth_use_count}"
        )
    if physical_execution_attempt_count:
        errors.append(
            "shadow_sink_attempted_physical_execution:"
            f"{physical_execution_attempt_count}"
        )
    return {
        "ok": not errors,
        "enabled": enabled,
        "admissible_command_count": len(admissible_ids),
        "feedback_published_count": len(published_ids),
        "completed_status_count": len(completed_ids),
        "counterfactual_event_count": len(event_records),
        "shadow_state_assumption_count": len(state_assumptions),
        "shadow_state_assumption_ground_truth_use_count": (
            state_assumption_ground_truth_use_count
        ),
        "ground_truth_use_count": ground_truth_use_count,
        "physical_execution_attempt_count": physical_execution_attempt_count,
        "errors": errors,
    }


def _wait_for_service(
    service_name: str,
    *,
    env: dict[str, str],
    timeout_sec: float,
) -> None:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        result = _run(
            ["ros2", "service", "list"],
            env=env,
            timeout_sec=10.0,
            check=False,
        )
        if service_name in result.stdout.splitlines():
            return
        time.sleep(0.25)
    raise TimeoutError(f"timed out waiting for ROS service {service_name}")


def _control_simulation(
    command: str,
    *,
    env: dict[str, str],
    start_phase_id: str = "",
) -> str:
    request = (
        "{command: "
        + json.dumps(command)
        + ", start_phase_id: "
        + json.dumps(start_phase_id)
        + "}"
    )
    result = _run(
        [
            "ros2",
            "service",
            "call",
            "/simulation/control",
            "surgical_msgs/srv/ControlSimulation",
            request,
        ],
        env=env,
        timeout_sec=35.0,
    )
    if "success=True" not in result.stdout and "success: true" not in result.stdout:
        raise RuntimeError(
            f"simulation control {command!r} was rejected:\n{result.stdout.strip()}"
        )
    return result.stdout


def _control_shadow_replay(
    command: str,
    *,
    env: dict[str, str],
    mode: str = "",
    playback_rate: float = 0.0,
) -> dict[str, Any]:
    request = {
        "command": command,
        "mode": mode,
        "playback_rate": float(playback_rate),
        "seek_sec": 0.0,
    }
    result = _run(
        [
            "ros2",
            "service",
            "call",
            "/shadow/control_replay",
            "surgical_msgs/srv/ControlShadowReplay",
            json.dumps(request, separators=(",", ":")),
        ],
        env=env,
        timeout_sec=35.0,
    )
    if (
        "success=True" not in result.stdout
        and "success: true" not in result.stdout
    ):
        raise RuntimeError(
            f"shadow replay control {command!r} was rejected:\n"
            f"{result.stdout.strip()}"
        )
    state = _parse_shadow_state_json(result.stdout)
    if state is None:
        raise RuntimeError(
            "shadow replay service response did not contain state_json:\n"
            + result.stdout.strip()
        )
    return state


def _parse_shadow_state_json(output: str) -> dict[str, Any] | None:
    quoted_match = re.search(
        r"""state_json(?:=|:\s*)(?P<quoted>'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*")""",
        output,
        re.DOTALL,
    )
    payload = ""
    if quoted_match is not None:
        try:
            decoded = ast.literal_eval(quoted_match.group("quoted"))
            if isinstance(decoded, str):
                payload = decoded
        except (SyntaxError, ValueError):
            payload = ""

    if not payload:
        fallback = re.search(
            r"""state_json(?:=|:\s*)['"](?P<payload>\{.*\})['"]""",
            output,
            re.DOTALL,
        )
        if fallback is None:
            return None
        payload = (
            fallback.group("payload")
            .replace("\\'", "'")
            .replace('\\"', '"')
        )

    try:
        state = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "shadow replay returned invalid state_json:\n" + payload
        ) from exc
    if not isinstance(state, dict):
        raise RuntimeError("shadow replay state_json must be an object")
    return state


def _wait_for_shadow_completion(
    *,
    env: dict[str, str],
    timeout_sec: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_sec
    last_state: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last_state = _control_shadow_replay("status", env=env)
        state = _clean_shadow_state(last_state.get("state"))
        if state == "completed":
            return last_state
        if state in {"blocked", "error", "failed", "timed_out"}:
            raise RuntimeError(
                f"interactive shadow replay entered {state} state: "
                + str(last_state.get("last_error") or "unknown error")
            )
        time.sleep(0.75)
    raise TimeoutError(
        "interactive shadow replay did not complete; last state="
        + json.dumps(last_state, ensure_ascii=False, sort_keys=True)
    )


def _clean_shadow_state(value: Any) -> str:
    return str(value or "").strip().lower()


def _wait_for_running(*, env: dict[str, str], timeout_sec: float) -> None:
    deadline = time.monotonic() + timeout_sec
    last_output = ""
    while time.monotonic() < deadline:
        last_output = _control_simulation("status", env=env)
        running = bool(
            re.search(r"running(?:=|:\\s*)True|running:\\s*true", last_output)
        )
        state_running = bool(
            re.search(
                r"execution_state(?:=|:\\s*)['\"]?running",
                last_output,
                re.IGNORECASE,
            )
        )
        if running and state_running:
            return
        time.sleep(0.35)
    raise TimeoutError(
        "simulation did not enter running state before replay; "
        f"last response: {last_output.strip()}"
    )


def _runtime_boundary_audit(
    *,
    env: dict[str, str],
    mode: str,
) -> dict[str, Any]:
    # The runner already owns this process environment. Avoid one-shot
    # ros2cli processes here: short-lived DDS participants can miss graph
    # entities and produce false boundary failures.
    for key in (
        "ROS_DOMAIN_ID",
        "ROS_AUTOMATIC_DISCOVERY_RANGE",
        "RMW_IMPLEMENTATION",
        "FASTRTPS_DEFAULT_PROFILES_FILE",
    ):
        if key in env:
            os.environ[key] = env[key]
    import rclpy
    from rclpy.node import Node

    rclpy.init(args=None)
    audit_node = Node(f"shadow_runtime_audit_{os.getpid()}")
    graph: list[tuple[str, str]] = []
    try:
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            rclpy.spin_once(audit_node, timeout_sec=0.2)
            graph = audit_node.get_node_names_and_namespaces()
            visible = {
                (
                    f"/{name}"
                    if namespace == "/"
                    else f"{namespace.rstrip('/')}/{name}"
                )
                for name, namespace in graph
            }
            if all(node in visible for node in CRITICAL_NODES):
                break
        nodes = sorted(
            {
                (
                    f"/{name}"
                    if namespace == "/"
                    else f"{namespace.rstrip('/')}/{name}"
                )
                for name, namespace in graph
            }
        )
        topic_lines = sorted(
            f"{name} [{','.join(types)}]"
            for name, types in audit_node.get_topic_names_and_types()
        )
        subscriptions: dict[str, list[str]] = {}
        publishers: dict[str, list[str]] = {}
        info_errors: dict[str, str] = {}
        for full_name in CRITICAL_NODES:
            namespace, _, name = full_name.rpartition("/")
            namespace = namespace or "/"
            try:
                values = audit_node.get_subscriber_names_and_types_by_node(
                    name,
                    namespace,
                )
                subscriptions[full_name] = sorted(
                    topic for topic, _types in values
                )
                published_values = (
                    audit_node.get_publisher_names_and_types_by_node(
                        name,
                        namespace,
                    )
                )
                publishers[full_name] = sorted(
                    topic for topic, _types in published_values
                )
            except Exception as exc:
                subscriptions[full_name] = []
                publishers[full_name] = []
                info_errors[full_name] = str(exc)
    finally:
        audit_node.destroy_node()
        rclpy.shutdown()

    forbidden_topics = [
        line.split(" ", 1)[0]
        for line in topic_lines
        if line.split(" ", 1)[0].startswith(GROUND_TRUTH_PREFIX)
    ]
    protected_ground_truth_topics = [
        line.split(" ", 1)[0]
        for line in topic_lines
        if any(
            line.split(" ", 1)[0].startswith(prefix)
            for prefix in PROTECTED_GROUND_TRUTH_PREFIXES
        )
    ]
    protected_subscriptions = {
        node: [
            topic
            for topic in values
            if any(
                topic.startswith(prefix)
                for prefix in PROTECTED_GROUND_TRUTH_PREFIXES
            )
        ]
        for node, values in subscriptions.items()
        if any(
            any(
                topic.startswith(prefix)
                for prefix in PROTECTED_GROUND_TRUTH_PREFIXES
            )
            for topic in values
        )
    }
    evaluation_sink_ground_truth_subscriptions = {
        node: topics
        for node, topics in protected_subscriptions.items()
        if node in GROUND_TRUTH_EVALUATION_SINK_NODES
    }
    leaked_subscriptions = {
        node: topics
        for node, topics in protected_subscriptions.items()
        if node not in GROUND_TRUTH_EVALUATION_SINK_NODES
    }
    evaluation_sink_runtime_publishers = {
        node: [
            topic
            for topic in publishers.get(node, [])
            if topic not in EVALUATION_SINK_ALLOWED_PUBLISHER_TOPICS
        ]
        for node in GROUND_TRUTH_EVALUATION_SINK_NODES
        if any(
            topic not in EVALUATION_SINK_ALLOWED_PUBLISHER_TOPICS
            for topic in publishers.get(node, [])
        )
    }
    forbidden_nodes = [
        node
        for node in nodes
        if any(marker in node for marker in STRICT_FORBIDDEN_NODE_MARKERS)
    ]
    missing_critical_nodes = [node for node in CRITICAL_NODES if node not in nodes]
    strict_errors = []
    if mode == "strict":
        if forbidden_topics:
            strict_errors.append("ground_truth_topics_visible")
        if leaked_subscriptions:
            strict_errors.append("critical_node_ground_truth_subscription")
        if evaluation_sink_runtime_publishers:
            strict_errors.append("evaluation_sink_has_runtime_publishers")
        if forbidden_nodes:
            strict_errors.append("forbidden_runtime_node_present")
        if "/reference_reconciler" in nodes:
            strict_errors.append("reference_reconciler_present")
    if missing_critical_nodes:
        strict_errors.append("critical_nodes_missing")
    if info_errors:
        strict_errors.append("critical_node_info_unavailable")
    return {
        "schema": "taskplanner.shadow_runtime_boundary_audit.v1",
        "mode": mode,
        "ok": not strict_errors,
        "nodes": nodes,
        "topics": topic_lines,
        "critical_node_subscriptions": subscriptions,
        "critical_node_publishers": publishers,
        "missing_critical_nodes": missing_critical_nodes,
        "forbidden_topics": forbidden_topics,
        "protected_ground_truth_topics": protected_ground_truth_topics,
        "evaluation_sink_ground_truth_subscriptions": (
            evaluation_sink_ground_truth_subscriptions
        ),
        "evaluation_sink_runtime_publishers": (
            evaluation_sink_runtime_publishers
        ),
        "leaked_subscriptions": leaked_subscriptions,
        "forbidden_nodes": forbidden_nodes,
        "node_info_errors": info_errors,
        "errors": strict_errors,
    }


def _static_boundary_audit(
    *,
    repo_root: Path,
    report_path: Path,
    env: dict[str, str],
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(repo_root / "tools/real_surgery_annotation/check_information_boundary.py"),
        "--repo",
        str(repo_root),
        "--report",
        str(report_path),
    ]
    result = _run(command, env=env, timeout_sec=60.0, check=False)
    if not report_path.is_file():
        raise RuntimeError(
            "static information-boundary audit did not produce a report:\n"
            + result.stdout.strip()
        )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if result.returncode != 0 or not report.get("ok"):
        raise RuntimeError("static information-boundary audit failed")
    return report


def _validate_reference(
    *,
    annotation_manifest: dict[str, Any],
    event_path: Path,
    bag_info: dict[str, Any],
) -> dict[str, Any]:
    expected_case = str(annotation_manifest.get("case_id", ""))
    records = load_jsonl(event_path)
    mismatched_cases = sorted(
        {
            str(record.get("case_id", ""))
            for record in records
            if str(record.get("case_id", "")) != expected_case
        }
    )
    if mismatched_cases:
        raise ValueError(f"reference contains mismatched case ids: {mismatched_cases}")
    expected_sha = str(
        annotation_manifest.get("source_bag", {}).get("mcap_sha256", "")
    )
    actual_sha = sha256_file(bag_info["bag_file"])
    if expected_sha and expected_sha != actual_sha:
        raise ValueError(
            "source bag hash does not match annotation manifest: "
            f"expected={expected_sha} actual={actual_sha}"
        )
    confirmed = [record for record in records if record.get("review_status") == "confirmed"]
    return {
        "record_count": len(records),
        "confirmed_count": len(confirmed),
        "source_bag_sha256": actual_sha,
    }


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    required = {
        "schema",
        "run_id",
        "case_id",
        "mode",
        "status",
        "created_at",
        "source_bag",
        "reference",
        "procedure",
        "runtime",
        "topics",
        "artifacts",
        "information_boundary",
    }
    missing = sorted(required - manifest.keys())
    if missing:
        raise ValueError(f"run manifest missing required fields: {missing}")
    if manifest["schema"] != RUN_MANIFEST_SCHEMA:
        raise ValueError("invalid run manifest schema")
    if manifest["mode"] not in RUN_MODES:
        raise ValueError("invalid run manifest mode")
    _json_dump(path, manifest)


def _artifact_if_present(path: Path) -> dict[str, str] | None:
    return hashed_artifact(path) if path.is_file() else None


def _behavior_quality_manifest_summary(
    evaluation: dict[str, Any],
) -> dict[str, Any]:
    behavior_quality = evaluation.get("behavior_quality")
    errors = validate_behavior_quality_report(behavior_quality)
    if errors:
        raise ValueError(
            "offline behavior-quality contract validation failed: "
            + "; ".join(errors)
        )
    assert isinstance(behavior_quality, dict)
    raw_summary = behavior_quality["summary"]
    assert isinstance(raw_summary, dict)
    summary = dict(raw_summary)
    empty_latency_distribution = {
        "count": 0,
        "mean": None,
        "median": None,
        "p95": None,
        "max": None,
    }
    for key in (
        "request_to_handover_wall_clock_latency_sec",
        "wrong_preposition_release_wall_clock_latency_sec",
        "abandoned_preposition_hold_duration_sec",
        "abandoned_preposition_wall_clock_hold_duration_sec",
    ):
        summary.setdefault(key, dict(empty_latency_distribution))
    return {
        "schema": behavior_quality["schema"],
        "status": behavior_quality.get("status"),
        "reference_quality": behavior_quality.get("reference_quality"),
        "evaluation_only": True,
        "latency_clock_semantics": behavior_quality.get(
            "latency_clock_semantics",
            {},
        ),
        **summary,
    }


def _build_parser(repo_root: Path) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--case-dir",
        type=Path,
        default=None,
        help=(
            "Annotation case directory. When omitted, infer an exact case "
            "directory name from the source bag path; fail if ambiguous."
        ),
    )
    parser.add_argument(
        "--source-bag",
        type=Path,
        required=True,
        help=(
            "ROS 2 bag directory containing metadata.yaml and exactly one "
            "bag data file; do not pass the .mcap file directly."
        ),
    )
    parser.add_argument(
        "--spec-dir",
        type=Path,
        default=None,
        help=(
            "Procedure bundle directory. By default this is resolved from "
            "--bundle under src/procedure_spec/procedure_spec/specs."
        ),
    )
    parser.add_argument("--bundle", default="thyroidectomy")
    parser.add_argument("--mode", choices=sorted(RUN_MODES), default="strict")
    parser.add_argument(
        "--output-root",
        type=Path,
        default=repo_root / "output/shadow_runs",
    )
    parser.add_argument("--run-id", default="")
    parser.add_argument(
        "--ros-domain-id",
        type=int,
        default=171,
        help=(
            "Isolated ROS domain for this evaluator. The default deliberately "
            "differs from the interactive shadow runtime domain (71)."
        ),
    )
    parser.add_argument(
        "--groot2-port",
        type=int,
        default=0,
        help=(
            "Fixed Groot2 publisher port. Zero selects an available, "
            "domain-derived port outside the OS ephemeral range."
        ),
    )
    parser.add_argument("--rate", type=float, default=1.0)
    parser.add_argument(
        "--interactive-replay",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Publish the filtered bag through the GUI-controllable shadow "
            "replay node instead of ros2 bag play."
        ),
    )
    parser.add_argument(
        "--replay-mode",
        choices=("elastic_demo", "realtime_1x"),
        default="elastic_demo",
    )
    parser.add_argument(
        "--fault-scenario-path",
        type=Path,
        default=None,
        help=(
            "Optional deterministic public-input fault timeline. The source "
            "bag remains unchanged; replayed FLIR, CAM4, and transcript "
            "messages are relayed through the fault injector."
        ),
    )
    parser.add_argument(
        "--rosbridge-port",
        type=int,
        default=9191,
        help=(
            "Evaluator rosbridge port. The default deliberately differs from "
            "the interactive shadow runtime port (9099)."
        ),
    )
    parser.add_argument(
        "--score-provisional-phase",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    parser.add_argument("--startup-timeout-sec", type=float, default=90.0)
    parser.add_argument("--post-roll-sec", type=float, default=5.0)
    parser.add_argument("--provider-id", default=os.environ.get("VLM_PROVIDER_ID", "vllm"))
    parser.add_argument(
        "--base-url",
        default=os.environ.get("VLM_BASE_URL", "http://127.0.0.1:8001"),
    )
    parser.add_argument(
        "--model-id",
        default=os.environ.get(
            "VLM_MODEL_ID",
            "AxionML/Qwen3.5-4B-NVFP4",
        ),
    )
    parser.add_argument("--api-mode", default=os.environ.get("VLM_API_MODE", "openai_compat"))
    parser.add_argument("--publish-period-sec", type=float, default=1.0)
    parser.add_argument("--response-format", default="json_schema")
    parser.add_argument("--reasoning-effort", default="none")
    parser.add_argument(
        "--vlm-task-profile",
        choices=("full", "tool_forecast_only"),
        default="full",
        help=(
            "Use tool_forecast_only only for an isolated raw next-tool "
            "prediction benchmark."
        ),
    )
    parser.add_argument("--max-output-tokens", type=int, default=320)
    parser.add_argument(
        "--vlm-generation-seed",
        type=int,
        default=0,
        help="Inference seed recorded in the run manifest; negative disables it.",
    )
    parser.add_argument("--vlm-request-timeout-sec", type=float, default=6.0)
    parser.add_argument("--vlm-retry-count", type=int, default=1)
    parser.add_argument(
        "--replay-vlm-health-timeout-sec",
        type=float,
        default=15.0,
    )
    parser.add_argument(
        "--replay-vlm-wait-timeout-sec",
        type=float,
        default=20.0,
    )
    parser.add_argument(
        "--replay-drain-timeout-sec",
        type=float,
        default=45.0,
    )
    parser.add_argument(
        "--response-mode",
        choices=("live", "replay"),
        default="live",
    )
    parser.add_argument("--replay-response-path", type=Path)
    parser.add_argument(
        "--field-image-topic",
        default=DEFAULT_REPLAY_TOPICS[0],
        help="Source-bag FLIR image topic; the launch remaps it internally.",
    )
    parser.add_argument(
        "--cam4-image-topic",
        default=DEFAULT_CAM4_IMAGE_TOPIC,
        help="Source-bag CAM4 image topic; the launch remaps it internally.",
    )
    parser.add_argument(
        "--perception-bboxes-topic",
        default=DEFAULT_CAM4_BBOXES_TOPIC,
    )
    parser.add_argument(
        "--perception-segmentation-topic",
        default=DEFAULT_CAM4_SEGMENTATION_TOPIC,
    )
    parser.add_argument("--cam4-crop-x-norm", type=float, default=0.32)
    parser.add_argument("--cam4-crop-y-norm", type=float, default=0.18)
    parser.add_argument("--cam4-crop-width-norm", type=float, default=0.62)
    parser.add_argument("--cam4-crop-height-norm", type=float, default=0.78)
    parser.add_argument("--multiview-max-skew-sec", type=float, default=0.1)
    parser.add_argument(
        "--perception-image-max-skew-sec",
        type=float,
        default=0.2,
    )
    parser.add_argument(
        "--source-transcript-topic",
        default=DEFAULT_REPLAY_TOPICS[1],
        help="Source-bag timestamped transcript topic.",
    )
    parser.add_argument("--start-phase-id", default="")
    parser.add_argument(
        "--counterfactual-feedback",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Publish GT-independent shadow-only success events/status so the "
            "production reducer and BT can advance without physical execution."
        ),
    )
    parser.add_argument(
        "--type-instance-assumption",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "In shadow-only semantic evaluation, continue repeated handovers "
            "of a tool type while recording the missing instance inventory "
            "model as an explicit assumption."
        ),
    )
    parser.add_argument("--skip-model-preflight", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    args = _build_parser(repo_root).parse_args()
    if not 0 <= args.ros_domain_id <= 232:
        raise ValueError("ROS_DOMAIN_ID must be between 0 and 232")
    if args.rate <= 0.0:
        raise ValueError("--rate must be positive")
    if args.vlm_request_timeout_sec <= 0.0:
        raise ValueError("--vlm-request-timeout-sec must be positive")
    if args.vlm_retry_count < 0:
        raise ValueError("--vlm-retry-count must be non-negative")
    if args.replay_vlm_health_timeout_sec < args.vlm_request_timeout_sec:
        raise ValueError(
            "--replay-vlm-health-timeout-sec must be at least "
            "--vlm-request-timeout-sec"
        )
    minimum_wait_timeout = (
        args.vlm_request_timeout_sec * (args.vlm_retry_count + 1) + 5.0
    )
    if (
        args.interactive_replay
        and args.replay_vlm_wait_timeout_sec < minimum_wait_timeout
    ):
        raise ValueError(
            "--replay-vlm-wait-timeout-sec must cover every VLM attempt "
            f"plus 5s slack (minimum {minimum_wait_timeout:.1f}s)"
        )
    if args.replay_drain_timeout_sec <= 0.0:
        raise ValueError("--replay-drain-timeout-sec must be positive")
    if args.multiview_max_skew_sec < 0.0:
        raise ValueError("--multiview-max-skew-sec must be non-negative")
    if args.perception_image_max_skew_sec < 0.0:
        raise ValueError(
            "--perception-image-max-skew-sec must be non-negative"
        )
    if not 1 <= args.rosbridge_port <= 65535:
        raise ValueError("--rosbridge-port must be between 1 and 65535")
    groot2_port = _select_groot2_port(
        args.groot2_port,
        args.ros_domain_id,
    )
    if (
        args.interactive_replay
        and not _port_is_available(args.rosbridge_port)
    ):
        raise RuntimeError(
            "requested rosbridge port is unavailable: "
            f"{args.rosbridge_port}"
        )
    runtime_lock_dir = _runtime_lock_dir()
    runtime_resource_claims = _runtime_resource_claims(
        ros_domain_id=args.ros_domain_id,
        groot2_port=groot2_port,
        rosbridge_port=(
            args.rosbridge_port if args.interactive_replay else None
        ),
    )
    source_bag = args.source_bag.resolve()
    case_dir = _resolve_case_dir(repo_root, source_bag, args.case_dir)
    spec_dir = (
        args.spec_dir.resolve()
        if args.spec_dir is not None
        else (
            repo_root
            / "src/procedure_spec/procedure_spec/specs"
            / args.bundle
        ).resolve()
    )
    if not spec_dir.is_dir():
        raise FileNotFoundError(spec_dir)
    procedure_path = spec_dir / "vlm_procedure_prompt.yaml"
    if not procedure_path.is_file():
        raise FileNotFoundError(procedure_path)
    procedure_payload = yaml.safe_load(
        procedure_path.read_text(encoding="utf-8")
    ) or {}
    declared_bundle = str(
        (
            procedure_payload.get("procedure", {})
            if isinstance(procedure_payload, dict)
            else {}
        ).get("id", "")
    ).strip()
    if declared_bundle and declared_bundle != args.bundle:
        raise ValueError(
            "procedure bundle mismatch: "
            f"--bundle={args.bundle!r}, spec procedure.id={declared_bundle!r}"
        )
    annotation_manifest, event_path = resolve_case_reference(case_dir)
    args.start_phase_id, start_phase_source = _resolve_start_phase_id(
        args.start_phase_id,
        annotation_manifest,
        procedure_payload,
    )
    _validate_start_phase_id(args.start_phase_id, procedure_path)
    annotation_root = case_dir.parents[1]
    tool_catalog_path = (
        case_dir / str(annotation_manifest.get("tool_catalog_path", ""))
    ).resolve()
    if (
        not tool_catalog_path.is_file()
        or annotation_root.resolve() not in tool_catalog_path.parents
    ):
        raise FileNotFoundError(
            "annotation manifest tool_catalog_path is missing or escapes "
            f"the annotation tree: {tool_catalog_path}"
        )
    bag_info = _load_bag_metadata(source_bag)
    reference_validation = _validate_reference(
        annotation_manifest=annotation_manifest,
        event_path=event_path,
        bag_info=bag_info,
    )
    case_id = str(annotation_manifest.get("case_id", "")).strip()
    if not case_id:
        raise ValueError("annotation manifest case_id is required")
    replay_topics = [
        args.field_image_topic,
        args.cam4_image_topic,
        args.perception_bboxes_topic,
        args.perception_segmentation_topic,
        args.source_transcript_topic,
    ]
    missing_topics = [
        topic for topic in replay_topics if topic not in bag_info["topics"]
    ]
    if missing_topics:
        raise ValueError(f"source bag is missing replay topics: {missing_topics}")
    ground_truth_topics = sorted(
        topic
        for topic in bag_info["topics"]
        if topic.startswith(GROUND_TRUTH_PREFIX)
    )
    if args.mode == "strict" and ground_truth_topics:
        raise ValueError(
            "strict replay refuses source bags containing runtime ground truth; "
            f"use the original source bag, found {ground_truth_topics}"
        )
    if args.response_mode == "replay":
        if args.replay_response_path is None:
            raise ValueError("--response-mode replay requires --replay-response-path")
        replay_response_path = args.replay_response_path.resolve()
        if not replay_response_path.is_file():
            raise FileNotFoundError(replay_response_path)
    else:
        replay_response_path = None
    if args.fault_scenario_path is not None:
        if not args.interactive_replay:
            raise ValueError(
                "--fault-scenario-path requires --interactive-replay so the "
                "public source topics can be relayed without mutating the bag"
            )
        fault_scenario_path = args.fault_scenario_path.resolve()
        if not fault_scenario_path.is_file():
            raise FileNotFoundError(fault_scenario_path)
    else:
        fault_scenario_path = None

    run_id = args.run_id.strip() or (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + uuid.uuid4().hex[:8]
    )
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", run_id):
        raise ValueError("run_id may contain only letters, digits, dot, dash, underscore")
    run_dir = (args.output_root.resolve() / run_id)
    run_dir.mkdir(parents=True, exist_ok=False)
    manifest_path = run_dir / "run_manifest.json"
    trace_path = run_dir / "shadow_trace.v1.jsonl"
    evaluation_path = run_dir / "shadow_evaluation.v2.json"
    csv_path = run_dir / "shadow_layers.csv"
    scorecard_csv_path = run_dir / "shadow_scorecard.csv"
    markdown_path = run_dir / "shadow_report.md"
    timeline_path = run_dir / "shadow_timeline.svg"
    surgery_record_path = run_dir / "surgery_record_input.txt"
    static_boundary_path = run_dir / "static_boundary.json"
    runtime_boundary_path = run_dir / "runtime_boundary.json"
    launch_log_path = run_dir / "launch.log"
    bag_log_path = run_dir / "bag_play.log"
    model_preflight_path = run_dir / "model_preflight.json"

    env = os.environ.copy()
    env["ROS_DOMAIN_ID"] = str(args.ros_domain_id)
    env["ROS_AUTOMATIC_DISCOVERY_RANGE"] = "LOCALHOST"
    env["ROS2CLI_NO_DAEMON"] = "1"
    git = _git_snapshot(repo_root)
    source_artifact = hashed_artifact(bag_info["bag_file"])
    manifest: dict[str, Any] = {
        "schema": RUN_MANIFEST_SCHEMA,
        "run_id": run_id,
        "case_id": case_id,
        "mode": args.mode,
        "status": "prepared",
        "created_at": utc_now(),
        "source_bag": source_artifact,
        "reference": {
            "annotation_manifest": hashed_artifact(
                case_dir / "annotation_manifest.json"
            ),
            "event_file": hashed_artifact(event_path),
            "tool_catalog": hashed_artifact(tool_catalog_path),
            "authority": _reference_authority(annotation_manifest),
            "runtime_visible": args.mode != "strict",
        },
        "procedure": hashed_artifact(procedure_path),
        "runtime": {
            "hostname": socket.gethostname(),
            "ros_domain_id": args.ros_domain_id,
            "groot2_port": groot2_port,
            "git": git,
            "bundle": args.bundle,
            "spec_dir": str(spec_dir),
            "rate": args.rate,
            "interactive_replay": bool(args.interactive_replay),
            "replay_mode": args.replay_mode,
            "fault_injection": {
                "enabled": fault_scenario_path is not None,
                "scenario": (
                    hashed_artifact(fault_scenario_path)
                    if fault_scenario_path is not None
                    else None
                ),
            },
            "rosbridge_port": (
                args.rosbridge_port if args.interactive_replay else None
            ),
            "resource_lock": {
                "schema": RUNTIME_LOCK_SCHEMA,
                "lock_dir": str(runtime_lock_dir),
                "claims": runtime_resource_claims,
                "status": (
                    "not_required_dry_run"
                    if args.dry_run
                    else "pending"
                ),
            },
            "score_provisional_phase": bool(
                args.score_provisional_phase
            ),
            "source_duration_sec": bag_info["duration_sec"],
            "source_message_count": bag_info["message_count"],
            "reference_validation": reference_validation,
            "phase_bootstrap": {
                "requested_phase_id": args.start_phase_id,
                "source": start_phase_source,
                "phase_ground_truth": False,
            },
            "shadow_execution": {
                "physical_execution_enabled": False,
                "counterfactual_feedback_enabled": bool(
                    args.counterfactual_feedback
                ),
                "type_instance_assumption_enabled": bool(
                    args.type_instance_assumption
                ),
                "ground_truth_used_for_feedback": False,
            },
            "vlm": {
                "provider_id": args.provider_id,
                "base_url": args.base_url,
                "model_id": args.model_id,
                "api_mode": args.api_mode,
                "response_mode": args.response_mode,
                "publish_period_sec": args.publish_period_sec,
                "response_format": args.response_format,
                "reasoning_effort": args.reasoning_effort,
                "task_profile": args.vlm_task_profile,
                "max_output_tokens": args.max_output_tokens,
                "generation_seed": args.vlm_generation_seed,
                "request_timeout_sec": args.vlm_request_timeout_sec,
                "retry_count": args.vlm_retry_count,
                "replay_response": (
                    hashed_artifact(replay_response_path)
                    if replay_response_path is not None
                    else None
                ),
            },
            "replay_sync": {
                "vlm_health_timeout_sec": (
                    args.replay_vlm_health_timeout_sec
                ),
                "vlm_wait_timeout_sec": args.replay_vlm_wait_timeout_sec,
                "drain_timeout_sec": args.replay_drain_timeout_sec,
            },
            "code_artifacts": {
                "vlm_node": hashed_artifact(
                    repo_root / "src/vlm_node/vlm_node/real_vlm.py"
                ),
                "digital_twin": hashed_artifact(
                    repo_root / "src/or_digital_twin/or_digital_twin/node.py"
                ),
                "digital_twin_core": hashed_artifact(
                    repo_root / "src/or_digital_twin/or_digital_twin/twin.py"
                ),
                "bt_nodes": hashed_artifact(
                    repo_root
                    / "src/taskplanner_bt_nodes/src/taskplanner_bt_nodes.cpp"
                ),
                "shadow_runner": hashed_artifact(Path(__file__).resolve()),
                "shadow_evaluator": hashed_artifact(
                    repo_root
                    / "tools/real_surgery_annotation/shadow_evaluate.py"
                ),
                "shadow_contract": hashed_artifact(
                    repo_root
                    / "tools/real_surgery_annotation/shadow_contract.py"
                ),
                "shadow_report_renderer": hashed_artifact(
                    repo_root
                    / "tools/real_surgery_annotation/render_shadow_report.py"
                ),
                "surgery_record_renderer": hashed_artifact(
                    repo_root
                    / "tools/real_surgery_annotation/render_surgery_record_timeline.py"
                ),
                "shadow_determinism_comparator": hashed_artifact(
                    repo_root
                    / "tools/real_surgery_annotation/compare_shadow_determinism.py"
                ),
                "shadow_skill_sink": hashed_artifact(
                    repo_root
                    / "src/shadow_evaluation/shadow_evaluation/shadow_skill_sink.py"
                ),
                "shadow_trace_recorder": hashed_artifact(
                    repo_root
                    / "src/shadow_evaluation/shadow_evaluation/trace_recorder.py"
                ),
                "recorded_transcript_adapter": hashed_artifact(
                    repo_root
                    / "src/shadow_evaluation/shadow_evaluation/recorded_transcript_adapter.py"
                ),
                "speech_input_adapter": hashed_artifact(
                    repo_root
                    / "src/simulation_runtime/simulation_runtime/speech_input_adapter.py"
                ),
                "bt_orchestrator": hashed_artifact(
                    repo_root
                    / "src/bt_orchestrator/bt_orchestrator/node.py"
                ),
                "simulation_manager": hashed_artifact(
                    repo_root
                    / "src/simulation_runtime/simulation_runtime/simulation_manager.py"
                ),
                "shadow_launch": hashed_artifact(
                    repo_root
                    / "src/bringup/launch/taskplanner_shadow.launch.py"
                ),
            },
        },
        "topics": {
            "replayed": sorted(replay_topics),
            "excluded": sorted(
                set(bag_info["topics"]) - set(replay_topics)
            ),
            "remappings": (
                {
                    args.field_image_topic: "/surgery/images/flir/compressed",
                    args.cam4_image_topic: [
                        "/surgery/images/cam4/compressed",
                        "/surgery/images/field/compressed",
                    ],
                    args.perception_bboxes_topic: (
                        "/surgery/perception/cam4/tools/bboxes/json"
                    ),
                    args.perception_segmentation_topic: (
                        "/surgery/perception/cam4/tools/segmentation/json"
                    ),
                }
                if args.interactive_replay
                else {}
            ),
            "vlm_model_input": (
                "/surgery/images/vlm/composite/compressed"
            ),
        },
        "artifacts": {},
        "information_boundary": {
            "strict": args.mode == "strict",
            "ground_truth_runtime_visible": args.mode != "strict",
            "checked": False,
            "surgery_record_input": {
                "emitted_only_after_status": "complete",
                "source_transcript_topic": args.source_transcript_topic,
                "vlm_source": "vlm_raw.schema_v4.summary",
                "evaluation_ground_truth_included": False,
                "shadow_events_marked_non_physical": True,
            },
        },
    }
    _write_manifest(manifest_path, manifest)

    launch_process: subprocess.Popen[Any] | None = None
    bag_process: subprocess.Popen[Any] | None = None
    runtime_lock: _RuntimeResourceLock | None = None
    return_code = 1
    try:
        if not args.dry_run:
            pending_runtime_lock = _RuntimeResourceLock(
                lock_dir=runtime_lock_dir,
                claims=runtime_resource_claims,
                owner={
                    "run_id": run_id,
                    "case_id": case_id,
                    "ros_domain_id": args.ros_domain_id,
                    "groot2_port": groot2_port,
                    "rosbridge_port": (
                        args.rosbridge_port
                        if args.interactive_replay
                        else None
                    ),
                },
            )
            try:
                runtime_lock = pending_runtime_lock.acquire()
            except RuntimeError:
                manifest["runtime"]["resource_lock"]["status"] = "contended"
                _write_manifest(manifest_path, manifest)
                raise
            manifest["runtime"]["resource_lock"]["status"] = "acquired"
            manifest["runtime"]["resource_lock"]["owner_pid"] = os.getpid()
            _write_manifest(manifest_path, manifest)
        static_boundary = _static_boundary_audit(
            repo_root=repo_root,
            report_path=static_boundary_path,
            env=env,
        )
        model_preflight: dict[str, Any] = {
            "skipped": args.skip_model_preflight or args.response_mode != "live",
            "reason": (
                "replay_response_mode"
                if args.response_mode != "live"
                else "explicit_skip"
            ),
        }
        if args.response_mode == "live" and not args.skip_model_preflight:
            model_preflight = _model_preflight(
                provider_id=args.provider_id,
                base_url=args.base_url,
                model_id=args.model_id,
                api_key=_provider_api_key(args.provider_id),
                timeout_sec=10.0,
            )
        _json_dump(model_preflight_path, model_preflight)
        manifest["runtime"]["model_preflight"] = model_preflight
        manifest["information_boundary"]["report"] = str(static_boundary_path)
        manifest["information_boundary"]["static_ok"] = bool(
            static_boundary.get("ok")
        )
        manifest["information_boundary"]["checked"] = bool(
            static_boundary.get("ok")
        )
        if args.dry_run:
            manifest["artifacts"] = {
                "static_boundary": hashed_artifact(static_boundary_path),
                "model_preflight": hashed_artifact(model_preflight_path),
            }
            _write_manifest(manifest_path, manifest)
            return 0

        runtime_field_image_topic = (
            "/surgery/images/field/compressed"
            if args.interactive_replay
            else args.field_image_topic
        )
        runtime_flir_image_topic = (
            "/surgery/images/flir/compressed"
            if args.interactive_replay
            else args.field_image_topic
        )
        runtime_cam4_image_topic = (
            "/surgery/images/cam4/compressed"
            if args.interactive_replay
            else args.cam4_image_topic
        )
        runtime_perception_bboxes_topic = (
            "/surgery/perception/cam4/tools/bboxes/json"
            if args.interactive_replay
            else args.perception_bboxes_topic
        )
        runtime_perception_segmentation_topic = (
            "/surgery/perception/cam4/tools/segmentation/json"
            if args.interactive_replay
            else args.perception_segmentation_topic
        )
        launch_command = [
            "ros2",
            "launch",
            "bringup",
            "taskplanner_shadow.launch.py",
            f"mode:={args.mode}",
            f"run_id:={run_id}",
            f"case_id:={case_id}",
            f"trace_path:={trace_path}",
            f"spec_dir:={spec_dir}",
            f"default_bundle:={args.bundle}",
            f"field_image_topic:={runtime_field_image_topic}",
            f"source_field_image_topic:={args.cam4_image_topic}",
            f"source_cam4_topic:={args.cam4_image_topic}",
            f"flir_image_topic:={runtime_flir_image_topic}",
            f"cam4_image_topic:={runtime_cam4_image_topic}",
            "composite_image_topic:=/surgery/images/vlm/composite/compressed",
            f"perception_bboxes_topic:={runtime_perception_bboxes_topic}",
            "perception_segmentation_topic:="
            f"{runtime_perception_segmentation_topic}",
            f"source_flir_topic:={args.field_image_topic}",
            f"source_bbox_topic:={args.perception_bboxes_topic}",
            f"source_segmentation_topic:={args.perception_segmentation_topic}",
            f"cam4_crop_x_norm:={args.cam4_crop_x_norm}",
            f"cam4_crop_y_norm:={args.cam4_crop_y_norm}",
            f"cam4_crop_width_norm:={args.cam4_crop_width_norm}",
            f"cam4_crop_height_norm:={args.cam4_crop_height_norm}",
            f"vlm_multiview_max_skew_sec:={args.multiview_max_skew_sec}",
            (
                "vlm_perception_image_max_skew_sec:="
                f"{args.perception_image_max_skew_sec}"
            ),
            f"source_transcript_topic:={args.source_transcript_topic}",
            f"vlm_base_url:={args.base_url}",
            f"vlm_provider_id:={args.provider_id}",
            f"vlm_model_id:={args.model_id}",
            f"vlm_api_mode:={args.api_mode}",
            f"vlm_publish_period_sec:={args.publish_period_sec}",
            f"vlm_response_format:={args.response_format}",
            f"vlm_reasoning_effort:={args.reasoning_effort}",
            f"vlm_task_profile:={args.vlm_task_profile}",
            f"vlm_max_output_tokens:={args.max_output_tokens}",
            f"vlm_generation_seed:={args.vlm_generation_seed}",
            f"vlm_request_timeout_sec:={args.vlm_request_timeout_sec}",
            f"vlm_retry_count:={args.vlm_retry_count}",
            f"vlm_response_mode:={args.response_mode}",
            "replay_vlm_health_timeout_sec:="
            f"{args.replay_vlm_health_timeout_sec}",
            "replay_vlm_wait_timeout_sec:="
            f"{args.replay_vlm_wait_timeout_sec}",
            f"replay_drain_timeout_sec:={args.replay_drain_timeout_sec}",
            "counterfactual_success_feedback:="
            + ("true" if args.counterfactual_feedback else "false"),
            "allow_type_instance_assumption:="
            + ("true" if args.type_instance_assumption else "false"),
            f"groot2_port:={groot2_port}",
            "enable_rosbridge:="
            + ("true" if args.interactive_replay else "false"),
            f"rosbridge_port:={args.rosbridge_port}",
            "interactive_replay:="
            + ("true" if args.interactive_replay else "false"),
            f"replay_mode:={args.replay_mode}",
            f"replay_rate:={args.rate}",
            f"image_duration_sec:={annotation_manifest.get('minimal_interaction_annotation', {}).get('visual_coverage', {}).get('end_sec', 138.4284)}",
        ]
        if args.interactive_replay:
            launch_command.append(f"source_bag_path:={source_bag}")
        if fault_scenario_path is not None:
            launch_command.append(
                f"fault_scenario_path:={fault_scenario_path}"
            )
        if args.mode != "strict":
            launch_command.append(f"reference_path:={event_path}")
            launch_command.append(f"tool_catalog_path:={tool_catalog_path}")
        if replay_response_path is not None:
            launch_command.append(
                f"vlm_replay_response_path:={replay_response_path}"
            )
        bag_command = [
            "ros2",
            "bag",
            "play",
            str(source_bag),
            "--storage",
            bag_info["storage_identifier"] or "mcap",
            "--rate",
            str(args.rate),
            "--clock",
            "30",
            "--start-paused",
            "--disable-keyboard-controls",
            "--topics",
            *replay_topics,
        ]
        manifest["runtime"]["commands"] = {
            "launch": _command_text(launch_command),
            "bag_play": (
                None
                if args.interactive_replay
                else _command_text(bag_command)
            ),
        }
        manifest["status"] = "running"
        manifest["started_at"] = utc_now()
        _write_manifest(manifest_path, manifest)

        with ExitStack() as stack:
            launch_log = stack.enter_context(launch_log_path.open("w", encoding="utf-8"))
            bag_log = stack.enter_context(bag_log_path.open("w", encoding="utf-8"))
            launch_process = subprocess.Popen(
                launch_command,
                env=env,
                stdout=launch_log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                text=True,
            )
            _wait_for_service(
                "/simulation/control",
                env=env,
                timeout_sec=args.startup_timeout_sec,
            )
            if args.interactive_replay:
                _wait_for_service(
                    "/shadow/control_replay",
                    env=env,
                    timeout_sec=args.startup_timeout_sec,
                )
                bag_log.write(
                    "interactive replay controller owns filtered bag playback\n"
                )
                bag_log.flush()
            else:
                bag_process = subprocess.Popen(
                    bag_command,
                    env=env,
                    stdout=bag_log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    text=True,
                )
                _wait_for_service(
                    "/rosbag2_player/resume",
                    env=env,
                    timeout_sec=30.0,
                )
            _control_simulation(
                "start",
                env=env,
                start_phase_id=args.start_phase_id,
            )
            _wait_for_running(env=env, timeout_sec=args.startup_timeout_sec)
            runtime_boundary = _runtime_boundary_audit(env=env, mode=args.mode)
            _json_dump(runtime_boundary_path, runtime_boundary)
            if not runtime_boundary["ok"]:
                raise RuntimeError(
                    "runtime information-boundary audit failed: "
                    + ", ".join(runtime_boundary["errors"])
                )
            manifest["information_boundary"]["runtime_ok"] = True
            manifest["information_boundary"]["checked"] = True
            _write_manifest(manifest_path, manifest)
            replay_timeout = (
                bag_info["duration_sec"] / args.rate
                + args.startup_timeout_sec
                + (900.0 if args.interactive_replay else 60.0)
            )
            if args.interactive_replay:
                _control_shadow_replay(
                    "start",
                    env=env,
                    mode=args.replay_mode,
                    playback_rate=args.rate,
                )
                final_replay_state = _wait_for_shadow_completion(
                    env=env,
                    timeout_sec=replay_timeout,
                )
                manifest["runtime"]["interactive_replay_final_state"] = (
                    final_replay_state
                )
            else:
                _run(
                    [
                        "ros2",
                        "service",
                        "call",
                        "/rosbag2_player/resume",
                        "rosbag2_interfaces/srv/Resume",
                        "{}",
                    ],
                    env=env,
                    timeout_sec=20.0,
                )
                assert bag_process is not None
                bag_return_code = bag_process.wait(timeout=replay_timeout)
                if bag_return_code != 0:
                    raise RuntimeError(
                        "rosbag replay exited with status "
                        f"{bag_return_code}"
                    )
            time.sleep(max(0.0, args.post_roll_sec))
            _control_simulation("stop", env=env)
            time.sleep(0.5)
            _terminate_process(launch_process)
            launch_process = None
            bag_process = None
        if runtime_lock is not None:
            runtime_lock.release()
            runtime_lock = None
            manifest["runtime"]["resource_lock"]["status"] = "released"
            manifest["runtime"]["resource_lock"]["released_at"] = utc_now()

        if not trace_path.is_file():
            raise RuntimeError("shadow trace was not created")
        trace_records = load_jsonl(trace_path)
        trace_errors = validate_trace_records(trace_records)
        if trace_errors:
            raise RuntimeError(
                "shadow trace contract validation failed: "
                + "; ".join(trace_errors[:10])
            )
        input_integrity = _validate_public_input_trace(
            trace_records,
            bag_info=bag_info,
            field_image_topic=args.field_image_topic,
            source_transcript_topic=args.source_transcript_topic,
            recorded_field_image_topic=runtime_field_image_topic,
            cam4_image_topic=args.cam4_image_topic,
            perception_bboxes_topic=args.perception_bboxes_topic,
            perception_segmentation_topic=args.perception_segmentation_topic,
            recorded_flir_image_topic=runtime_flir_image_topic,
            recorded_cam4_image_topic=runtime_cam4_image_topic,
            recorded_perception_bboxes_topic=(
                runtime_perception_bboxes_topic
            ),
            recorded_perception_segmentation_topic=(
                runtime_perception_segmentation_topic
            ),
            composite_image_topic=(
                "/surgery/images/vlm/composite/compressed"
            ),
        )
        manifest["runtime"]["input_integrity"] = input_integrity
        if not input_integrity["ok"]:
            raise RuntimeError(
                "shadow public-input integrity failed: "
                + "; ".join(input_integrity["errors"])
            )
        feedback_integrity = _validate_shadow_feedback_trace(
            trace_records,
            enabled=bool(args.counterfactual_feedback),
        )
        manifest["runtime"]["shadow_feedback_integrity"] = feedback_integrity
        if not feedback_integrity["ok"]:
            raise RuntimeError(
                "shadow counterfactual-feedback integrity failed: "
                + "; ".join(feedback_integrity["errors"])
            )
        evaluation_command = [
            sys.executable,
            "-m",
            "tools.real_surgery_annotation.shadow_evaluate",
            "--case-dir",
            str(case_dir),
            "--trace",
            str(trace_path),
            "--mode",
            args.mode,
            "--tool-catalog",
            str(tool_catalog_path),
            "--procedure-prompt",
            str(procedure_path),
            "--output",
            str(evaluation_path),
            "--csv",
            str(csv_path),
            "--scorecard-csv",
            str(scorecard_csv_path),
        ]
        if args.score_provisional_phase:
            evaluation_command.append("--score-provisional-phase")
        _run(evaluation_command, env=env, timeout_sec=120.0)
        evaluation = json.loads(evaluation_path.read_text(encoding="utf-8"))
        if evaluation.get("runtime", {}).get("trace_contract_error_count") != 0:
            raise RuntimeError("offline evaluator reported trace contract errors")
        manifest["evaluation"] = {
            "status": evaluation.get("status"),
            "behavior_quality": _behavior_quality_manifest_summary(
                evaluation
            ),
        }
        manifest["status"] = "complete"
        manifest["completed_at"] = utc_now()
        manifest["runtime"]["trace_record_count"] = len(trace_records)
        manifest["runtime"]["evaluation_status"] = evaluation.get("status")
        _write_manifest(manifest_path, manifest)
        report_command = [
            sys.executable,
            "-m",
            "tools.real_surgery_annotation.render_shadow_report",
            "--manifest",
            str(manifest_path),
            "--evaluation",
            str(evaluation_path),
            "--markdown",
            str(markdown_path),
            "--svg",
            str(timeline_path),
        ]
        _run(report_command, env=env, timeout_sec=30.0)
        surgery_record_command = [
            sys.executable,
            "-m",
            "tools.real_surgery_annotation.render_surgery_record_timeline",
            "--manifest",
            str(manifest_path),
            "--trace",
            str(trace_path),
            "--procedure-prompt",
            str(procedure_path),
            "--output",
            str(surgery_record_path),
        ]
        _run(surgery_record_command, env=env, timeout_sec=30.0)
        manifest["artifacts"] = {
            "trace": hashed_artifact(trace_path),
            "evaluation": hashed_artifact(evaluation_path),
            "layer_csv": hashed_artifact(csv_path),
            "scorecard_csv": hashed_artifact(scorecard_csv_path),
            "markdown_report": hashed_artifact(markdown_path),
            "timeline_svg": hashed_artifact(timeline_path),
            "surgery_record_input": hashed_artifact(surgery_record_path),
            "static_boundary": hashed_artifact(static_boundary_path),
            "runtime_boundary": hashed_artifact(runtime_boundary_path),
            "model_preflight": hashed_artifact(model_preflight_path),
            "launch_log": hashed_artifact(launch_log_path),
            "bag_log": hashed_artifact(bag_log_path),
        }
        _write_manifest(manifest_path, manifest)
        return_code = 0
    except KeyboardInterrupt:
        manifest["status"] = "interrupted"
        manifest["error"] = "interrupted by user"
        raise
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["error"] = str(exc)
        print(f"shadow replay failed: {exc}", file=sys.stderr)
    finally:
        _terminate_process(bag_process)
        _terminate_process(launch_process)
        if runtime_lock is not None:
            runtime_lock.release()
            runtime_lock = None
            manifest["runtime"]["resource_lock"]["status"] = "released"
            manifest["runtime"]["resource_lock"]["released_at"] = utc_now()
        if manifest["status"] in {"failed", "interrupted"}:
            manifest["completed_at"] = utc_now()
            artifacts: dict[str, Any] = {}
            for name, path in (
                ("trace", trace_path),
                ("evaluation", evaluation_path),
                ("layer_csv", csv_path),
                ("scorecard_csv", scorecard_csv_path),
                ("markdown_report", markdown_path),
                ("timeline_svg", timeline_path),
                ("static_boundary", static_boundary_path),
                ("runtime_boundary", runtime_boundary_path),
                ("model_preflight", model_preflight_path),
                ("launch_log", launch_log_path),
                ("bag_log", bag_log_path),
            ):
                artifact = _artifact_if_present(path)
                if artifact is not None:
                    artifacts[name] = artifact
            manifest["artifacts"] = artifacts
            _write_manifest(manifest_path, manifest)
        print(str(run_dir))
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
