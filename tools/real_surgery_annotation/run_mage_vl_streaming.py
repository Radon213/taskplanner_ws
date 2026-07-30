#!/usr/bin/env python3
"""Run the official Mage-VL streaming demo and preserve evidence-only output.

This wrapper deliberately stops at raw segment/gate evidence.  It does not
create observable-tool events, infer physical state, or publish ground truth.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import math
import platform
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from .event_model import canonical_json, sha256_file


DEFAULT_MAGE_REPO = Path("/home/arl/.cache/codex/mage")
MAGE_CHECKPOINT = "microsoft/Mage-VL"

_NUMBER = r"[+-]?(?:(?:\d+(?:\.\d*)?)|(?:\.\d+))(?:[eE][+-]?\d+)?"
_GATE_LINE = re.compile(
    rf"""
    ^\s*
    \[t=(?P<start>{_NUMBER})\s*-\s*(?P<end>{_NUMBER})s\]
    \s+gate=(?P<gate>silence|response)
    \s+\(p\s*=\s*(?P<probability>{_NUMBER})\)
    (?:\s*->\s*(?P<response>.*))?
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)
_SKIP_LINE = re.compile(
    rf"""
    ^\s*
    \[t=(?P<start>{_NUMBER})\s*-\s*(?P<end>{_NUMBER})s\]
    \s+skip\s+\(segment\s+unusable\)
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

_DISTRIBUTIONS = (
    "torch",
    "torchvision",
    "transformers",
    "accelerate",
    "safetensors",
    "huggingface-hub",
    "decord",
    "codec-video-prep",
    "mamba-ssm",
    "flash-attn",
)


def _finite_number(value: str, *, label: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    return number


def _validated_bounds(start_text: str, end_text: str) -> tuple[float, float]:
    start = _finite_number(start_text, label="segment start")
    end = _finite_number(end_text, label="segment end")
    if start < 0 or end < start:
        raise ValueError(f"invalid Mage-VL segment bounds: [{start}, {end}]")
    return start, end


def parse_segment_line(line: str) -> dict[str, Any] | None:
    """Parse one official Mage-VL stdout segment line.

    Unknown lines return ``None`` so callers can preserve them in the run
    report without turning logs or warnings into model evidence.
    """

    gate_match = _GATE_LINE.fullmatch(line)
    if gate_match:
        start, end = _validated_bounds(
            gate_match.group("start"),
            gate_match.group("end"),
        )
        probability = _finite_number(
            gate_match.group("probability"),
            label="gate probability",
        )
        if not 0 <= probability <= 1:
            raise ValueError(f"gate probability outside [0,1]: {probability}")
        gate = gate_match.group("gate").lower()
        response = gate_match.group("response")
        return {
            "segment_start_sec": start,
            "segment_end_sec": end,
            "segment_status": "gate_evaluated",
            "gate_decision": gate,
            "gate_probability": probability,
            "response_text": response,
            "raw_stdout": line,
        }

    skip_match = _SKIP_LINE.fullmatch(line)
    if skip_match:
        start, end = _validated_bounds(
            skip_match.group("start"),
            skip_match.group("end"),
        )
        return {
            "segment_start_sec": start,
            "segment_end_sec": end,
            "segment_status": "skipped_unusable",
            "gate_decision": None,
            "gate_probability": None,
            "response_text": None,
            "raw_stdout": line,
        }
    return None


def parse_streaming_stdout(
    stdout: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Parse segment evidence while retaining non-segment stdout separately."""

    parsed: list[dict[str, Any]] = []
    unparsed: list[str] = []
    for line in stdout.splitlines():
        candidate_line = line
        marker = line.find("[t=")
        if marker > 0:
            # Transformers' trust-remote-code confirmation can be printed
            # without a trailing newline when stdin is non-interactive, so
            # the first official segment record may share that same line.
            prefix = line[:marker].rstrip()
            if prefix:
                unparsed.append(prefix)
            candidate_line = line[marker:]
        try:
            record = parse_segment_line(candidate_line)
        except ValueError:
            unparsed.append(line)
            continue
        if record is not None:
            parsed.append(record)
            continue

        # Generated text can contain embedded newlines.  The official script
        # prints those continuation lines without a segment prefix.
        if (
            parsed
            and parsed[-1]["gate_decision"] == "response"
            and parsed[-1]["response_text"] is not None
            and not candidate_line.lstrip().startswith("[")
        ):
            parsed[-1]["response_text"] += "\n" + line
            parsed[-1]["raw_stdout"] += "\n" + line
        elif line.strip():
            unparsed.append(line)
    return parsed, unparsed


def make_evidence_records(
    parsed_segments: list[dict[str, Any]],
    *,
    case_id: str,
    video: Path,
    gate_threshold: float,
) -> list[dict[str, Any]]:
    """Add provenance and an explicit non-authoritative boundary."""

    video_path = str(video.resolve())
    records: list[dict[str, Any]] = []
    for index, segment in enumerate(parsed_segments, 1):
        records.append(
            {
                "schema": "taskplanner.mage_vl_streaming_segment_evidence.v1",
                "case_id": case_id,
                "evidence_id": f"{case_id}-MAGE-{index:06d}",
                "source": "microsoft_mage_vl_official_stdout",
                "model": MAGE_CHECKPOINT,
                "video": video_path,
                "gate_threshold": gate_threshold,
                **segment,
                "authority": "non_authoritative_model_evidence",
                "human_confirmation_required": True,
                "may_publish_ground_truth": False,
                "observable_event_created": False,
            }
        )
    return records


def _package_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for distribution in _DISTRIBUTIONS:
        try:
            versions[distribution] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            versions[distribution] = None
    return versions


def model_environment(mage_repo: Path, script: Path) -> dict[str, Any]:
    """Collect reproducibility metadata without copying process environment."""

    return {
        "checkpoint": MAGE_CHECKPOINT,
        # Keep the venv entry point rather than resolving its symlink to a
        # bare base interpreter that cannot see this environment's packages.
        "python_executable": sys.executable,
        "python_version": platform.python_version(),
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "mage_repo": str(mage_repo.resolve()),
        "official_script": str(script.resolve()),
        "official_script_sha256": sha256_file(script),
        "package_versions": _package_versions(),
        "environment_variables_recorded": False,
    }


def resolve_official_script(mage_repo: Path) -> Path:
    script = mage_repo.resolve() / "mage_vl" / "inference_streaming.py"
    if not script.is_file():
        raise FileNotFoundError(
            f"official Mage-VL streaming script not found: {script}"
        )
    return script


def build_command(args: argparse.Namespace, script: Path) -> list[str]:
    """Return an argv list; no shell or credential-bearing options are used."""

    return [
        sys.executable,
        str(script.resolve()),
        "--video",
        str(args.video.resolve()),
        "--video_backend",
        "codec",
        "--segment_sec",
        str(args.segment_sec),
        "--max_segments",
        str(args.max_segments),
        "--gate_threshold",
        str(args.threshold),
        "--max_new_tokens",
        str(args.max_new_tokens),
        "--attn_impl",
        args.attn_impl,
    ]


def _write_new_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(text)


def _validate_paths(args: argparse.Namespace) -> Path:
    if not args.video.is_file():
        raise FileNotFoundError(args.video)
    if args.output.resolve() == args.report.resolve():
        raise ValueError("--output and --report must be different paths")
    for path in (args.output, args.report):
        if path.exists():
            raise FileExistsError(f"refusing to overwrite existing output: {path}")
    return resolve_official_script(args.mage_repo)


def run(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Execute Mage-VL once and return evidence plus a complete run report."""

    script = _validate_paths(args)
    command = build_command(args, script)
    started = time.perf_counter()
    completed = subprocess.run(
        command,
        cwd=str(args.mage_repo.resolve()),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    elapsed_sec = time.perf_counter() - started

    parsed, unparsed = parse_streaming_stdout(completed.stdout)
    evidence = make_evidence_records(
        parsed,
        case_id=args.case_id,
        video=args.video,
        gate_threshold=args.threshold,
    )
    gate_counts = {
        "response": sum(
            item["gate_decision"] == "response"
            for item in evidence
        ),
        "silence": sum(
            item["gate_decision"] == "silence"
            for item in evidence
        ),
        "skipped_unusable": sum(
            item["segment_status"] == "skipped_unusable"
            for item in evidence
        ),
    }
    return_code = int(completed.returncode)
    report = {
        "schema": "taskplanner.mage_vl_streaming_run_report.v1",
        "case_id": args.case_id,
        "ok": return_code == 0,
        "status": "completed" if return_code == 0 else "subprocess_failed",
        "video": str(args.video.resolve()),
        "output": str(args.output.resolve()),
        "gate_threshold": args.threshold,
        "segment_sec": args.segment_sec,
        "max_segments": args.max_segments,
        "max_new_tokens": args.max_new_tokens,
        "attention_implementation": args.attn_impl,
        "elapsed_sec": round(elapsed_sec, 6),
        "return_code": return_code,
        "command": command,
        "command_uses_shell": False,
        "credentials_in_command": False,
        "working_directory": str(args.mage_repo.resolve()),
        "model_environment": model_environment(args.mage_repo, script),
        "segment_evidence_count": len(evidence),
        "gate_counts": gate_counts,
        "unparsed_stdout_lines": unparsed,
        "raw_stdout": completed.stdout,
        "raw_stderr": completed.stderr,
        "ground_truth_event_count": 0,
        "observable_event_count": 0,
        "human_confirmation_required": True,
        "may_publish_ground_truth": False,
    }
    return evidence, report


def _probability(text: str) -> float:
    value = float(text)
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise argparse.ArgumentTypeError("must be a finite number in [0,1]")
    return value


def _positive_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than 0")
    return value


def _nonnegative_int(text: str) -> int:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError("must be greater than or equal to 0")
    return value


def _positive_int(text: str) -> int:
    value = int(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return value


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run official Mage-VL streaming inference and preserve raw, "
            "non-ground-truth segment/gate evidence."
        )
    )
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--case", "--case-id", dest="case_id", required=True)
    parser.add_argument("--mage-repo", type=Path, default=DEFAULT_MAGE_REPO)
    parser.add_argument("--threshold", type=_probability, default=0.5)
    parser.add_argument("--segment-sec", type=_positive_float, default=8.0)
    parser.add_argument("--max-segments", type=_nonnegative_int, default=0)
    parser.add_argument("--max-new-tokens", type=_positive_int, default=80)
    parser.add_argument(
        "--attn-impl",
        choices=("sdpa", "flash_attention_2", "eager"),
        default="sdpa",
        help="Use SDPA by default so flash-attn is not a runtime prerequisite.",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    evidence, report = run(args)
    _write_new_text(
        args.output,
        "".join(canonical_json(item) + "\n" for item in evidence),
    )
    _write_new_text(
        args.report,
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return_code = int(report["return_code"])
    return return_code if 0 <= return_code <= 255 else 1


if __name__ == "__main__":
    raise SystemExit(main())
