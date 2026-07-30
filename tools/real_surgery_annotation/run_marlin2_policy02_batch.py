#!/usr/bin/env python3
"""Run the policy02 Marlin proposal sweep with at most two GPU processes.

This is a proposal-only, create-only orchestrator.  It never turns Marlin
output into ground truth.  A resumed invocation validates and reuses complete
per-pass artifacts, while missing passes are scheduled again.  Existing,
partial, or invalid artifact pairs are never overwritten.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from tools.real_surgery_annotation.run_marlin2_proposals import (
    MODEL_QUERIES,
    MODEL_QUERY_POLICY_ID,
    atomic_create_text,
    canonical_json_sha256,
    sha256_file,
)


ANNOTATION_ROOT = WORKSPACE_ROOT / "annotations/observable_tool_events"
DEFAULT_VIDEO_ROOT = Path(
    "/mnt/arl/NAS관리/백업/업무/ARPA-H/SurgeryData/"
    "갑상샘/0704_갑상선절제술_원본영상"
)
DEFAULT_MODEL = Path(
    "/home/arl/.cache/huggingface/hub/models--NemoStation--Marlin-2B/"
    "snapshots/fd111fca4fc7897876fb0d7e9df22ca5ac8ab965"
)
DEFAULT_MODEL_REVISION = "fd111fca4fc7897876fb0d7e9df22ca5ac8ab965"
DEFAULT_PYTHON = Path("/home/arl/.cache/codex/venvs/marlin-2b/bin/python")
RUNNER = Path(__file__).resolve().with_name("run_marlin2_proposals.py")
POLICY_VERSION = "policy02.v1"
TARGET_CASE_IDS = tuple(f"0704_{index}" for index in range(7, 18))
CUDA_VISIBLE_DEVICES = "0"
GPU_INDEX = 0
DEFAULT_MIN_FREE_VRAM_MIB = 20_000


@dataclass(frozen=True)
class PassSpec:
    name: str
    anchor_filename: str
    event_types: tuple[str, ...]
    clip_before_sec: float
    clip_after_sec: float


PASS_SPECS = (
    PassSpec(
        name="transcript",
        anchor_filename="transcript_tool_anchors.v1.json",
        event_types=(
            "implicit_tool_request",
            "scrub_nurse_to_surgeon",
        ),
        clip_before_sec=1.25,
        clip_after_sec=4.25,
    ),
    PassSpec(
        name="scan",
        anchor_filename="marlin_full_scan_anchors.v1.json",
        event_types=(
            "implicit_tool_request",
            "mayo_stand_to_scrub_nurse",
            "surgeon_to_scrub_nurse",
            "scrub_nurse_to_mayo_stand",
            "scrub_nurse_to_surgeon",
            "surgeon_to_mayo_stand",
        ),
        clip_before_sec=7.0,
        clip_after_sec=7.0,
    ),
)


@dataclass(frozen=True)
class Job:
    case_id: str
    pass_spec: PassSpec
    video: Path
    timeline: Path
    anchors: Path
    output: Path
    report: Path
    command: tuple[str, ...]
    prompt_sha256: str

    @property
    def job_id(self) -> str:
        return f"{self.case_id}:{self.pass_spec.name}"


class PreflightFailure(RuntimeError):
    def __init__(self, message: str, evidence: dict[str, Any]) -> None:
        super().__init__(message)
        self.evidence = evidence


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def tail_lines(text: str, count: int) -> list[str]:
    return text.splitlines()[-count:]


def is_marlin_runner_argv(argv: list[str]) -> bool:
    """Match an actual runner argv token, not a shell command substring."""

    return any(Path(argument).name == RUNNER.name for argument in argv[1:])


def find_existing_marlin_runners(
    *,
    proc_root: Path = Path("/proc"),
    excluded_pids: set[int] | None = None,
) -> list[dict[str, Any]]:
    excluded = set(excluded_pids or ())
    matches: list[dict[str, Any]] = []
    try:
        process_dirs = list(proc_root.iterdir())
    except OSError as exc:
        raise PreflightFailure(
            f"cannot inspect process table: {exc}",
            {
                "process_scan": {
                    "proc_root": str(proc_root),
                    "status": "failed",
                    "error": str(exc),
                }
            },
        ) from exc
    for process_dir in process_dirs:
        if not process_dir.name.isdigit():
            continue
        pid = int(process_dir.name)
        if pid in excluded:
            continue
        try:
            raw = (process_dir / "cmdline").read_bytes()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        argv = [
            part.decode("utf-8", errors="replace")
            for part in raw.split(b"\0")
            if part
        ]
        if is_marlin_runner_argv(argv):
            matches.append({"pid": pid, "argv": argv})
    return sorted(matches, key=lambda item: int(item["pid"]))


def run_execution_preflight(
    *,
    min_free_vram_mib: int,
    proc_root: Path = Path("/proc"),
    excluded_pids: set[int] | None = None,
) -> dict[str, Any]:
    if min_free_vram_mib <= 0:
        raise ValueError("min_free_vram_mib must be positive")
    excluded = {os.getpid(), *(excluded_pids or set())}
    conflicts = find_existing_marlin_runners(
        proc_root=proc_root,
        excluded_pids=excluded,
    )
    command = [
        "nvidia-smi",
        f"--id={GPU_INDEX}",
        "--query-gpu=memory.free",
        "--format=csv,noheader,nounits",
    ]
    evidence: dict[str, Any] = {
        "checked_at": utc_now(),
        "gpu_index": GPU_INDEX,
        "query_command": command,
        "min_free_vram_mib": min_free_vram_mib,
        "free_vram_mib": None,
        "conflicting_marlin_processes": conflicts,
    }
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        evidence["nvidia_smi_error"] = str(exc)
        raise PreflightFailure(
            f"nvidia-smi preflight could not start: {exc}",
            evidence,
        ) from exc
    evidence["nvidia_smi_exit_code"] = completed.returncode
    evidence["nvidia_smi_stderr"] = tail_lines(completed.stderr, 20)
    lines = [
        line.strip()
        for line in completed.stdout.splitlines()
        if line.strip()
    ]
    if completed.returncode != 0 or len(lines) != 1 or not lines[0].isdigit():
        raise PreflightFailure(
            "nvidia-smi preflight did not return one integer memory value",
            evidence,
        )
    free_vram_mib = int(lines[0])
    evidence["free_vram_mib"] = free_vram_mib
    if conflicts:
        raise PreflightFailure(
            "another run_marlin2_proposals.py process is already active",
            evidence,
        )
    if free_vram_mib < min_free_vram_mib:
        raise PreflightFailure(
            (
                f"free VRAM {free_vram_mib} MiB is below required "
                f"{min_free_vram_mib} MiB"
            ),
            evidence,
        )
    evidence["status"] = "passed"
    return evidence


def hash_model_tree(model_root: Path) -> tuple[str, list[dict[str, Any]]]:
    """Return a content hash for the exact local model tree.

    Hugging Face snapshots are usually symlinks into the content-addressed
    blob cache.  Hashing through those symlinks verifies the bytes actually
    loaded instead of trusting only the snapshot directory name.
    """

    entries: list[dict[str, Any]] = []
    for path in sorted(
        (item for item in model_root.rglob("*") if item.is_file()),
        key=lambda item: item.relative_to(model_root).as_posix(),
    ):
        entries.append(
            {
                "path": path.relative_to(model_root).as_posix(),
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    if not entries:
        raise ValueError(f"model tree contains no files: {model_root}")
    return canonical_json_sha256(entries), entries


def prompt_hash(pass_spec: PassSpec) -> str:
    return canonical_json_sha256(
        {
            event_type: MODEL_QUERIES[event_type]
            for event_type in pass_spec.event_types
        }
    )


def build_command(
    *,
    python_executable: Path,
    runner: Path,
    case_id: str,
    video: Path,
    timeline: Path,
    anchors: Path,
    model: Path,
    model_revision: str,
    output: Path,
    report: Path,
    pass_spec: PassSpec,
) -> tuple[str, ...]:
    return (
        # Preserve a venv's interpreter symlink.  Resolving it to the base uv
        # interpreter can change sys.prefix and hide the venv site-packages.
        str(python_executable.expanduser().absolute()),
        str(runner.resolve()),
        "--case-id",
        case_id,
        "--video",
        str(video.resolve()),
        "--timeline",
        str(timeline.resolve()),
        "--anchors",
        str(anchors.resolve()),
        "--model",
        str(model.resolve()),
        "--model-revision",
        model_revision,
        "--output",
        str(output.resolve()),
        "--report",
        str(report.resolve()),
        "--clip-before-sec",
        str(pass_spec.clip_before_sec),
        "--clip-after-sec",
        str(pass_spec.clip_after_sec),
        "--event-types",
        ",".join(pass_spec.event_types),
        "--skip-caption",
        "--device",
        "cuda",
    )


def build_jobs(
    *,
    case_ids: tuple[str, ...],
    annotation_root: Path,
    video_root: Path,
    model: Path,
    model_revision: str,
    python_executable: Path,
    runner: Path,
) -> list[Job]:
    jobs: list[Job] = []
    for case_id in case_ids:
        case_root = annotation_root / "cases" / case_id
        video = video_root / case_id / "cam_4/rgb.avi"
        timeline = case_root / "cam4_frame_timeline.v1.json"
        for pass_spec in PASS_SPECS:
            output = (
                annotation_root
                / "proposals"
                / (
                    f"{case_id}_marlin2_{pass_spec.name}."
                    f"{POLICY_VERSION}.jsonl"
                )
            )
            report = (
                annotation_root
                / "reports"
                / (
                    f"{case_id}_marlin2_{pass_spec.name}."
                    f"{POLICY_VERSION}.json"
                )
            )
            anchors = case_root / pass_spec.anchor_filename
            jobs.append(
                Job(
                    case_id=case_id,
                    pass_spec=pass_spec,
                    video=video,
                    timeline=timeline,
                    anchors=anchors,
                    output=output,
                    report=report,
                    command=build_command(
                        python_executable=python_executable,
                        runner=runner,
                        case_id=case_id,
                        video=video,
                        timeline=timeline,
                        anchors=anchors,
                        model=model,
                        model_revision=model_revision,
                        output=output,
                        report=report,
                        pass_spec=pass_spec,
                    ),
                    prompt_sha256=prompt_hash(pass_spec),
                )
            )
    return jobs


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected JSON object")
    return value


def validate_completed_pair(
    job: Job,
    *,
    model: Path,
    model_revision: str,
    input_hash_cache: dict[Path, str] | None = None,
    verify_input_contents: bool = True,
) -> dict[str, Any]:
    """Validate an existing child output/report pair before reusing it."""

    report = load_json(job.report)
    errors: list[str] = []
    if report.get("status") != "completed":
        errors.append("child report status is not completed")
    if report.get("case_id") != job.case_id:
        errors.append("child report case_id mismatch")
    if report.get("output") != str(job.output.resolve()):
        errors.append("child report output path mismatch")
    if report.get("output_sha256") != sha256_file(job.output):
        errors.append("child output hash mismatch")

    report_model = report.get("model", {})
    if report_model.get("revision") != model_revision:
        errors.append("child model revision mismatch")
    if report_model.get("local_path") != str(model.resolve()):
        errors.append("child model path mismatch")

    inputs = report.get("inputs", {})
    expected_inputs = {
        "video": job.video,
        "timeline": job.timeline,
        "anchors": job.anchors,
    }
    for key, path in expected_inputs.items():
        if inputs.get(key) != str(path.resolve()):
            errors.append(f"child {key} path mismatch")
        expected_hash = inputs.get(f"{key}_sha256")
        if not isinstance(expected_hash, str) or len(expected_hash) != 64:
            errors.append(f"child {key} hash missing")
        elif verify_input_contents:
            resolved = path.resolve()
            if input_hash_cache is not None and resolved in input_hash_cache:
                actual_hash = input_hash_cache[resolved]
            else:
                actual_hash = sha256_file(path)
                if input_hash_cache is not None:
                    input_hash_cache[resolved] = actual_hash
            if actual_hash != expected_hash:
                errors.append(f"child {key} input content hash mismatch")

    settings = report.get("settings", {})
    if settings.get("query_policy_id") != MODEL_QUERY_POLICY_ID:
        errors.append("child query policy mismatch")
    if settings.get("query_prompt_sha256") != job.prompt_sha256:
        errors.append("child prompt hash mismatch")
    if settings.get("event_types") != list(job.pass_spec.event_types):
        errors.append("child event type list mismatch")
    if settings.get("clip_before_sec") != job.pass_spec.clip_before_sec:
        errors.append("child clip_before_sec mismatch")
    if settings.get("clip_after_sec") != job.pass_spec.clip_after_sec:
        errors.append("child clip_after_sec mismatch")
    if settings.get("skip_caption") is not True:
        errors.append("child skip_caption mismatch")
    if errors:
        raise ValueError("; ".join(errors))
    return report


def base_job_record(job: Job) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "case_id": job.case_id,
        "pass": job.pass_spec.name,
        "command": list(job.command),
        "command_sha256": canonical_json_sha256(list(job.command)),
        "environment": {"CUDA_VISIBLE_DEVICES": CUDA_VISIBLE_DEVICES},
        "prompt_sha256": job.prompt_sha256,
        "inputs": {
            "video": str(job.video.resolve()),
            "timeline": str(job.timeline.resolve()),
            "anchors": str(job.anchors.resolve()),
        },
        "output": str(job.output.resolve()),
        "report": str(job.report.resolve()),
    }


def inspect_job(
    job: Job,
    *,
    model: Path,
    model_revision: str,
    input_hash_cache: dict[Path, str] | None = None,
) -> tuple[str, dict[str, Any] | None, str | None]:
    output_exists = job.output.exists()
    report_exists = job.report.exists()
    if output_exists != report_exists:
        return (
            "blocked_partial_artifact",
            None,
            (
                "exactly one child artifact exists; create-only safety forbids "
                "overwriting the partial pair"
            ),
        )
    if not output_exists:
        missing = [
            str(path)
            for path in (job.video, job.timeline, job.anchors)
            if not path.is_file()
        ]
        if missing:
            return "blocked_missing_input", None, f"missing inputs: {missing}"
        return "pending", None, None
    try:
        report = validate_completed_pair(
            job,
            model=model,
            model_revision=model_revision,
            input_hash_cache=input_hash_cache,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return "blocked_invalid_existing_artifact", None, str(exc)
    return "reused_completed", report, None


def child_hashes(report: dict[str, Any]) -> dict[str, str]:
    inputs = report["inputs"]
    return {
        "video_sha256": inputs["video_sha256"],
        "timeline_sha256": inputs["timeline_sha256"],
        "anchors_sha256": inputs["anchors_sha256"],
        "output_sha256": report["output_sha256"],
    }


def run_job(
    job: Job,
    *,
    workspace_root: Path,
    model: Path,
    model_revision: str,
) -> dict[str, Any]:
    record = base_job_record(job)
    record["status"] = "running"
    record["started_at"] = utc_now()
    started = time.monotonic()
    print(
        json.dumps(
            {"job_id": job.job_id, "status": "started"},
            ensure_ascii=False,
        ),
        flush=True,
    )
    environment = os.environ.copy()
    environment["CUDA_VISIBLE_DEVICES"] = CUDA_VISIBLE_DEVICES
    try:
        completed = subprocess.run(
            list(job.command),
            cwd=workspace_root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        record.update(
            {
                "status": "failed_to_start",
                "error": str(exc),
                "exit_code": None,
                "stdout_tail": [],
                "stderr_tail": [],
            }
        )
    else:
        record.update(
            {
                "exit_code": completed.returncode,
                "stdout_tail": tail_lines(completed.stdout, 40),
                "stderr_tail": tail_lines(completed.stderr, 80),
            }
        )
        if completed.returncode == 0:
            try:
                child_report = validate_completed_pair(
                    job,
                    model=model,
                    model_revision=model_revision,
                    verify_input_contents=False,
                )
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                record.update(
                    {
                        "status": "failed_missing_or_invalid_child_report",
                        "error": str(exc),
                    }
                )
            else:
                record.update(
                    {
                        "status": "completed",
                        "child_artifact_sha256": child_hashes(child_report),
                    }
                )
        else:
            record["status"] = "failed"
    record["duration_sec"] = round(time.monotonic() - started, 6)
    record["finished_at"] = utc_now()
    print(
        json.dumps(
            {
                "job_id": job.job_id,
                "status": record["status"],
                "duration_sec": record["duration_sec"],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return record


def execute_pending_jobs(
    pending_jobs: list[Job],
    *,
    max_workers: int,
    workspace_root: Path,
    model: Path,
    model_revision: str,
) -> dict[str, dict[str, Any]]:
    """Execute pending child processes while enforcing the hard limit."""

    if max_workers not in (1, 2):
        raise ValueError("max_workers must be one or two")
    records: dict[str, dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=max_workers
    ) as executor:
        future_jobs = {
            executor.submit(
                run_job,
                job,
                workspace_root=workspace_root,
                model=model,
                model_revision=model_revision,
            ): job
            for job in pending_jobs
        }
        for future in concurrent.futures.as_completed(future_jobs):
            job = future_jobs[future]
            try:
                records[job.job_id] = future.result()
            except Exception as exc:  # Preserve other completed jobs for resume.
                failed = base_job_record(job)
                failed.update(
                    {
                        "status": "orchestrator_exception",
                        "error": f"{type(exc).__name__}: {exc}",
                        "finished_at": utc_now(),
                    }
                )
                records[job.job_id] = failed
    return records


def pass_spec_document() -> list[dict[str, Any]]:
    return [
        {
            "name": item.name,
            "anchor_filename": item.anchor_filename,
            "event_types": list(item.event_types),
            "clip_before_sec": item.clip_before_sec,
            "clip_after_sec": item.clip_after_sec,
            "skip_caption": True,
        }
        for item in PASS_SPECS
    ]


def make_batch_report(
    *,
    status: str,
    max_workers: int,
    case_ids: tuple[str, ...],
    model: Path,
    model_revision: str,
    model_manifest_sha256: str | None,
    model_manifest_file_count: int | None,
    runner: Path,
    min_free_vram_mib: int,
    execution_preflight: dict[str, Any] | None,
    jobs: list[dict[str, Any]],
    started_at: str,
    generated_at: str,
) -> dict[str, Any]:
    return {
        "schema": "taskplanner.marlin2_policy_batch.v1",
        "policy_version": POLICY_VERSION,
        "query_policy_id": MODEL_QUERY_POLICY_ID,
        "status": status,
        "authority": "proposal_only_not_ground_truth",
        "phase_annotation_performed": False,
        "settings": {
            "min_free_vram_mib": min_free_vram_mib,
            "gpu_index": GPU_INDEX,
        },
        "execution_preflight": execution_preflight,
        "concurrency": {
            "max_processes": max_workers,
            "hard_limit": 2,
            "cuda_visible_devices_per_worker": CUDA_VISIBLE_DEVICES,
        },
        "cases": list(case_ids),
        "pass_specs": pass_spec_document(),
        "pass_specs_sha256": canonical_json_sha256(pass_spec_document()),
        "all_prompts_sha256": canonical_json_sha256(MODEL_QUERIES),
        "runner": {
            "path": str(runner.resolve()),
            "sha256": sha256_file(runner),
        },
        "model": {
            "id": "NemoStation/Marlin-2B",
            "revision": model_revision,
            "local_path": str(model.resolve()),
            "manifest_sha256": model_manifest_sha256,
            "manifest_file_count": model_manifest_file_count,
        },
        "counts": {
            "job_count": len(jobs),
            "completed_count": sum(
                item.get("status") == "completed" for item in jobs
            ),
            "reused_completed_count": sum(
                item.get("status") == "reused_completed" for item in jobs
            ),
            "failed_or_blocked_count": sum(
                item.get("status")
                not in {"completed", "reused_completed", "planned"}
                for item in jobs
            ),
        },
        "jobs": jobs,
        "started_at": started_at,
        "generated_at": generated_at,
    }


def unique_attempt_report(annotation_root: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return (
        annotation_root
        / "reports"
        / f"marlin2_{POLICY_VERSION}.batch_attempt.{stamp}.{os.getpid()}.json"
    )


def canonical_batch_report(
    annotation_root: Path,
    case_ids: tuple[str, ...],
) -> Path:
    if case_ids == TARGET_CASE_IDS:
        suffix = ""
    else:
        suffix = "." + "-".join(case_id.removeprefix("0704_") for case_id in case_ids)
    return (
        annotation_root
        / "reports"
        / f"marlin2_{POLICY_VERSION}.batch{suffix}.json"
    )


def publish_json(path: Path, value: dict[str, Any]) -> None:
    atomic_create_text(
        path,
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n",
    )


def parse_case_ids(value: str) -> tuple[str, ...]:
    case_ids = tuple(item.strip() for item in value.split(",") if item.strip())
    invalid = sorted(set(case_ids) - set(TARGET_CASE_IDS))
    if not case_ids or invalid or len(set(case_ids)) != len(case_ids):
        raise argparse.ArgumentTypeError(
            f"case ids must be a unique subset of {TARGET_CASE_IDS}; invalid={invalid}"
        )
    return case_ids


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run policy02 Marlin proposals for 0704_7..0704_17 with a "
            "hard concurrency limit of two."
        )
    )
    parser.add_argument(
        "--case-ids",
        type=parse_case_ids,
        default=TARGET_CASE_IDS,
        help="Comma-separated subset; defaults to 0704_7 through 0704_17.",
    )
    parser.add_argument("--annotation-root", type=Path, default=ANNOTATION_ROOT)
    parser.add_argument("--video-root", type=Path, default=DEFAULT_VIDEO_ROOT)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument(
        "--model-revision",
        default=DEFAULT_MODEL_REVISION,
    )
    parser.add_argument("--python", type=Path, default=DEFAULT_PYTHON)
    parser.add_argument("--runner", type=Path, default=RUNNER)
    parser.add_argument(
        "--max-workers",
        type=int,
        choices=(1, 2),
        default=2,
        help="Number of Marlin processes; hard limited to one or two.",
    )
    parser.add_argument(
        "--min-free-vram-mib",
        type=int,
        default=DEFAULT_MIN_FREE_VRAM_MIB,
        help=(
            "Fail closed before starting new jobs when GPU 0 has less free "
            "memory than this value."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and print the resume plan without model hashing or writes.",
    )
    args = parser.parse_args()
    if args.min_free_vram_mib <= 0:
        parser.error("--min-free-vram-mib must be positive")

    started_at = utc_now()
    canonical_report = canonical_batch_report(
        args.annotation_root,
        args.case_ids,
    )
    if not args.python.is_file():
        raise SystemExit(f"Python executable does not exist: {args.python}")
    if not args.runner.is_file():
        raise SystemExit(f"proposal runner does not exist: {args.runner}")
    if not args.model.is_dir():
        raise SystemExit(f"model directory does not exist: {args.model}")

    jobs = build_jobs(
        case_ids=args.case_ids,
        annotation_root=args.annotation_root,
        video_root=args.video_root,
        model=args.model,
        model_revision=args.model_revision,
        python_executable=args.python,
        runner=args.runner,
    )
    input_hash_cache: dict[Path, str] = {}
    inspected: dict[str, tuple[str, dict[str, Any] | None, str | None]] = {
        job.job_id: inspect_job(
            job,
            model=args.model,
            model_revision=args.model_revision,
            input_hash_cache=input_hash_cache,
        )
        for job in jobs
    }

    plan_records: list[dict[str, Any]] = []
    for job in jobs:
        status, child_report, error = inspected[job.job_id]
        record = base_job_record(job)
        record["status"] = "planned" if status == "pending" else status
        if child_report is not None:
            record["child_artifact_sha256"] = child_hashes(child_report)
        if error is not None:
            record["error"] = error
        plan_records.append(record)

    if args.dry_run:
        print(
            json.dumps(
                {
                    "policy_version": POLICY_VERSION,
                    "max_workers": args.max_workers,
                    "min_free_vram_mib": args.min_free_vram_mib,
                    "jobs": plan_records,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return (
            2
            if any(
                record["status"].startswith("blocked")
                for record in plan_records
            )
            else 0
        )

    if canonical_report.exists():
        existing = load_json(canonical_report)
        if (
            existing.get("status") == "completed"
            and all(
                status == "reused_completed"
                for status, _report, _error in inspected.values()
            )
        ):
            print(
                json.dumps(
                    {
                        "status": "already_completed",
                        "report": str(canonical_report.resolve()),
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        raise SystemExit(
            "canonical batch report already exists but current child artifacts "
            f"are not a complete valid set: {canonical_report}"
        )

    blocked = [
        record
        for record in plan_records
        if record["status"].startswith("blocked")
    ]
    if blocked:
        batch_report = make_batch_report(
            status="incomplete_blocked",
            max_workers=args.max_workers,
            case_ids=args.case_ids,
            model=args.model,
            model_revision=args.model_revision,
            model_manifest_sha256=None,
            model_manifest_file_count=None,
            runner=args.runner,
            min_free_vram_mib=args.min_free_vram_mib,
            execution_preflight={
                "status": "skipped",
                "reason": "blocked_child_artifacts_or_inputs",
            },
            jobs=plan_records,
            started_at=started_at,
            generated_at=utc_now(),
        )
        attempt_report = unique_attempt_report(args.annotation_root)
        publish_json(attempt_report, batch_report)
        print(
            json.dumps(
                {
                    "status": batch_report["status"],
                    "attempt_report": str(attempt_report.resolve()),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2

    pending_jobs = [
        job for job in jobs if inspected[job.job_id][0] == "pending"
    ]
    if pending_jobs:
        try:
            execution_preflight = run_execution_preflight(
                min_free_vram_mib=args.min_free_vram_mib,
            )
        except PreflightFailure as exc:
            execution_preflight = dict(exc.evidence)
            execution_preflight.update(
                {
                    "status": "failed",
                    "error": str(exc),
                }
            )
            failed_plan_records: list[dict[str, Any]] = []
            for record in plan_records:
                failed_record = dict(record)
                if failed_record["status"] == "planned":
                    failed_record["status"] = "blocked_preflight"
                    failed_record["error"] = str(exc)
                failed_plan_records.append(failed_record)
            batch_report = make_batch_report(
                status="incomplete_preflight",
                max_workers=args.max_workers,
                case_ids=args.case_ids,
                model=args.model,
                model_revision=args.model_revision,
                model_manifest_sha256=None,
                model_manifest_file_count=None,
                runner=args.runner,
                min_free_vram_mib=args.min_free_vram_mib,
                execution_preflight=execution_preflight,
                jobs=failed_plan_records,
                started_at=started_at,
                generated_at=utc_now(),
            )
            attempt_report = unique_attempt_report(args.annotation_root)
            publish_json(attempt_report, batch_report)
            print(
                json.dumps(
                    {
                        "status": batch_report["status"],
                        "attempt_report": str(attempt_report.resolve()),
                        "error": str(exc),
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            return 2
    else:
        execution_preflight = {
            "status": "skipped",
            "reason": "no_pending_jobs",
        }

    model_manifest_sha256, model_entries = hash_model_tree(args.model)
    run_records: dict[str, dict[str, Any]] = {
        record["job_id"]: record
        for record in plan_records
        if record["status"] == "reused_completed"
    }
    run_records.update(
        execute_pending_jobs(
            pending_jobs,
            max_workers=args.max_workers,
            workspace_root=WORKSPACE_ROOT,
            model=args.model,
            model_revision=args.model_revision,
        )
    )

    ordered_records = [run_records[job.job_id] for job in jobs]
    complete = all(
        record["status"] in {"completed", "reused_completed"}
        for record in ordered_records
    )
    batch_report = make_batch_report(
        status="completed" if complete else "incomplete_failed",
        max_workers=args.max_workers,
        case_ids=args.case_ids,
        model=args.model,
        model_revision=args.model_revision,
        model_manifest_sha256=model_manifest_sha256,
        model_manifest_file_count=len(model_entries),
        runner=args.runner,
        min_free_vram_mib=args.min_free_vram_mib,
        execution_preflight=execution_preflight,
        jobs=ordered_records,
        started_at=started_at,
        generated_at=utc_now(),
    )
    destination = (
        canonical_report
        if complete
        else unique_attempt_report(args.annotation_root)
    )
    publish_json(destination, batch_report)
    print(
        json.dumps(
            {
                "status": batch_report["status"],
                "report": str(destination.resolve()),
                "counts": batch_report["counts"],
            },
            ensure_ascii=False,
        )
    )
    return 0 if complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
