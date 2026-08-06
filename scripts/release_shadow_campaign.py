#!/usr/bin/env python3
"""Run a reproducible multi-case shadow campaign and aggregate its results."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = tuple(f"0704_{number}" for number in range(6, 18))


def effective_vlm_timeouts(args: argparse.Namespace) -> dict[str, float]:
    """Return timeout values that cover health checks and every retry."""

    request_timeout = max(0.1, float(args.request_timeout_sec))
    retry_count = max(0, int(args.retry_count))
    wait_timeout = max(
        float(args.vlm_wait_timeout_sec),
        request_timeout * (retry_count + 1) + 5.0,
    )
    return {
        "request": request_timeout,
        "health": max(request_timeout, float(args.vlm_wait_timeout_sec)),
        "wait": wait_timeout,
        "drain": wait_timeout,
    }


def load_workspace_environment(
    setup_path: Path | None = None,
    *,
    base_environment: dict[str, str] | None = None,
    required_package: str | None = "bringup",
) -> dict[str, str]:
    """Load the built ROS workspace without depending on the caller's shell."""

    environment = dict(base_environment or os.environ)
    if required_package:
        available = subprocess.run(
            ["ros2", "pkg", "prefix", required_package],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if available.returncode == 0:
            return environment
    setup = setup_path or (ROOT / "install" / "setup.bash")
    if not setup.is_file():
        raise RuntimeError(
            f"built workspace setup not found: {setup}; run the release build first"
        )
    completed = subprocess.run(
        [
            "bash",
            "-c",
            f"source {shlex.quote(str(setup))} >/dev/null && env -0",
        ],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"failed to load workspace setup: {detail}")
    for entry in completed.stdout.split(b"\0"):
        if not entry or b"=" not in entry:
            continue
        key, value = entry.split(b"=", 1)
        environment[key.decode()] = value.decode(errors="surrogateescape")
    if required_package:
        available = subprocess.run(
            ["ros2", "pkg", "prefix", required_package],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if available.returncode != 0:
            raise RuntimeError(
                f"workspace setup does not provide required ROS package: {required_package}"
            )
    return environment


def build_case_command(args: argparse.Namespace, case_id: str, index: int) -> list[str]:
    timeouts = effective_vlm_timeouts(args)
    command = [
        sys.executable,
        str(ROOT / "tools" / "real_surgery_annotation" / "run_shadow_replay.py"),
        "--case-dir",
        str(args.annotation_root / "cases" / case_id),
        "--source-bag",
        str(args.dataset_root / case_id),
        "--bundle",
        args.bundle,
        "--mode",
        "strict",
        "--output-root",
        str(args.output_dir / "runs"),
        "--run-id",
        f"case-{case_id}",
        "--ros-domain-id",
        str(args.ros_domain_base + index),
        "--groot2-port",
        str(args.groot2_port_base + index),
        "--rosbridge-port",
        str(args.rosbridge_port_base + index),
        "--rate",
        str(args.rate),
        "--interactive-replay",
        "--replay-mode",
        args.replay_mode,
        "--score-provisional-phase",
        "--provider-id",
        args.provider_id,
        "--base-url",
        args.base_url,
        "--model-id",
        args.model_id,
        "--api-mode",
        args.api_mode,
        "--publish-period-sec",
        str(args.publish_period_sec),
        "--response-format",
        args.response_format,
        "--reasoning-effort",
        args.reasoning_effort,
        "--max-output-tokens",
        str(args.max_output_tokens),
        "--vlm-generation-seed",
        str(args.seed),
        "--vlm-request-timeout-sec",
        str(timeouts["request"]),
        "--vlm-retry-count",
        str(args.retry_count),
        "--replay-vlm-health-timeout-sec",
        str(timeouts["health"]),
        "--replay-vlm-wait-timeout-sec",
        str(timeouts["wait"]),
        "--replay-drain-timeout-sec",
        str(timeouts["drain"]),
        "--response-mode",
        "live",
        "--counterfactual-feedback",
        "--type-instance-assumption",
    ]
    fault_scenario_path = getattr(args, "fault_scenario_path", None)
    if fault_scenario_path is not None:
        command.extend(
            ["--fault-scenario-path", str(fault_scenario_path)]
        )
    return command


def validate_inputs(args: argparse.Namespace) -> None:
    errors = []
    maximum_domain_id = args.ros_domain_base + len(args.cases) - 1
    if not 0 <= args.ros_domain_base <= 232 or maximum_domain_id > 232:
        errors.append(
            "ROS domain range must stay within 0..232: "
            f"requested {args.ros_domain_base}..{maximum_domain_id}"
        )
    if args.request_timeout_sec <= 0.0:
        errors.append("--request-timeout-sec must be positive")
    if args.retry_count < 0:
        errors.append("--retry-count must be non-negative")
    if args.vlm_wait_timeout_sec <= 0.0:
        errors.append("--vlm-wait-timeout-sec must be positive")
    if (
        args.fault_scenario_path is not None
        and not args.fault_scenario_path.is_file()
    ):
        errors.append(
            f"missing fault scenario: {args.fault_scenario_path}"
        )
    for case_id in args.cases:
        bag = args.dataset_root / case_id / "metadata.yaml"
        annotation = args.annotation_root / "cases" / case_id
        if not bag.is_file():
            errors.append(f"missing bag metadata: {bag}")
        if not annotation.is_dir():
            errors.append(f"missing annotation case: {annotation}")
    if errors:
        raise SystemExit("\n".join(errors))


def write_status(path: Path, payload: dict[str, object]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-root", type=Path, required=True)
    parser.add_argument("--annotation-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--cases", nargs="+", default=list(DEFAULT_CASES))
    parser.add_argument("--bundle", default="thyroidectomy_demo")
    parser.add_argument("--provider-id", default="ninfer")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--model-id", default="qwen3.6-35b-a3b")
    parser.add_argument("--api-mode", default="openai_compat")
    parser.add_argument("--response-format", default="json_schema")
    parser.add_argument("--reasoning-effort", default="none")
    parser.add_argument("--publish-period-sec", type=float, default=1.0)
    parser.add_argument("--max-output-tokens", type=int, default=320)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--request-timeout-sec", type=float, default=60.0)
    parser.add_argument("--retry-count", type=int, default=1)
    parser.add_argument("--vlm-wait-timeout-sec", type=float, default=130.0)
    parser.add_argument("--rate", type=float, default=1.0)
    parser.add_argument(
        "--replay-mode", choices=("elastic_demo", "realtime_1x"), default="elastic_demo"
    )
    parser.add_argument("--fault-scenario-path", type=Path)
    parser.add_argument("--ros-domain-base", type=int, default=193)
    parser.add_argument("--groot2-port-base", type=int, default=20193)
    parser.add_argument("--rosbridge-port-base", type=int, default=9293)
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    args.dataset_root = args.dataset_root.resolve()
    args.annotation_root = args.annotation_root.resolve()
    args.output_dir = args.output_dir.resolve()
    if args.fault_scenario_path is not None:
        args.fault_scenario_path = args.fault_scenario_path.resolve()
    validate_inputs(args)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    (args.output_dir / "runs").mkdir()
    (args.output_dir / "logs").mkdir()

    status_path = args.output_dir / "campaign_status.json"
    started_at = datetime.now(timezone.utc)
    results: list[dict[str, object]] = []
    runtime_environment = None if args.dry_run else load_workspace_environment()
    for index, case_id in enumerate(args.cases):
        command = build_case_command(args, case_id, index)
        if args.dry_run:
            print(subprocess.list2cmdline(command))
            results.append({"case_id": case_id, "status": "dry_run", "return_code": 0})
            continue
        print(f"[{index + 1}/{len(args.cases)}] {case_id}: running", flush=True)
        started = time.monotonic()
        with (args.output_dir / "logs" / f"{case_id}.log").open(
            "w", encoding="utf-8"
        ) as log:
            completed = subprocess.run(
                command,
                cwd=ROOT,
                env=runtime_environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
        row = {
            "case_id": case_id,
            "status": "passed" if completed.returncode == 0 else "failed",
            "return_code": completed.returncode,
            "duration_sec": round(time.monotonic() - started, 3),
            "run_dir": str(args.output_dir / "runs" / f"case-{case_id}"),
        }
        results.append(row)
        write_status(
            status_path,
            {
                "schema": "taskplanner.release_shadow_campaign.v1",
                "started_at_utc": started_at.isoformat(),
                "cases": list(args.cases),
                "results": results,
            },
        )
        print(f"[{index + 1}/{len(args.cases)}] {case_id}: {row['status']}", flush=True)
        if completed.returncode != 0 and not args.continue_on_error:
            break

    failures = [row for row in results if row["status"] == "failed"]
    completed_ids = [row["case_id"] for row in results if row["status"] == "passed"]
    if not failures and completed_ids == list(args.cases) and not args.dry_run:
        aggregate_command = [
            sys.executable,
            str(
                ROOT
                / "tools"
                / "real_surgery_annotation"
                / "aggregate_shadow_multicase.py"
            ),
            "--runs-root",
            str(args.output_dir / "runs"),
            "--output-dir",
            str(args.output_dir / "report"),
            "--expected-cases",
            *args.cases,
        ]
        aggregate = subprocess.run(aggregate_command, cwd=ROOT, check=False)
        if aggregate.returncode != 0:
            failures.append(
                {"case_id": "aggregate", "status": "failed", "return_code": aggregate.returncode}
            )

    final = {
        "schema": "taskplanner.release_shadow_campaign.v1",
        "started_at_utc": started_at.isoformat(),
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "failed" if failures else ("dry_run" if args.dry_run else "passed"),
        "cases": list(args.cases),
        "model": {
            "provider_id": args.provider_id,
            "base_url": args.base_url,
            "model_id": args.model_id,
            "seed": args.seed,
        },
        "fault_scenario_path": (
            str(args.fault_scenario_path)
            if args.fault_scenario_path is not None
            else ""
        ),
        "results": results,
    }
    write_status(status_path, final)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
