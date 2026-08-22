#!/usr/bin/env python3
"""Exercise clean runtime restart and bounded soak behavior without loading models."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import statistics
import subprocess
import time
from urllib.error import URLError
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
WEBAPP_BASE_URL = "http://127.0.0.1:4173"
WEBAPP_STATIC_PROBES = (
    ("/", "text/html", b'id="root"'),
    ("/src/App.tsx", "text/javascript", None),
    ("/src/hooks/useRosBridge.ts", "text/javascript", None),
)
WEBAPP_MAX_PROBE_BYTES = 512 * 1024
RUNTIME_STATUS_PATH = "/api/runtime/status"
RUNTIME_STATUS_MAX_BYTES = 128 * 1024
RUNTIME_STATUS_MAX_MESSAGE_CHARS = 4_096
RUNTIME_PHASES = frozenset({"idle", "starting", "failed"})
RUNTIME_MODES = frozenset({"live", "llm-surgeon", "replay", "debug"})
MEMORY_PATTERN = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([KMGT]?i?B)\b", re.IGNORECASE)
MEMORY_MULTIPLIERS = {
    "B": 1,
    "KB": 1000,
    "MB": 1000**2,
    "GB": 1000**3,
    "TB": 1000**4,
    "KIB": 1024,
    "MIB": 1024**2,
    "GIB": 1024**3,
    "TIB": 1024**4,
}


def command_environment() -> dict[str, str]:
    return {
        **os.environ,
        "VLM_BASE_URL": "http://127.0.0.1:9",
        "VLM_PROVIDER_ID": "release-unavailable",
        "VLM_MODEL_ID": "",
        "SURGEON_ACTOR_MODE": "mock",
        "TASKPLANNER_REBUILD_ON_START": "false",
        "WEBAPP_INSTALL_ON_START": "false",
    }


def run(command: list[str], *, timeout: float = 600.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=ROOT,
        env=command_environment(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )


def _read_probe_body(response: object, *, limit: int) -> bytes:
    body = response.read(limit + 1)
    if len(body) > limit:
        raise ValueError("response exceeds readiness size limit")
    return body


def _response_content_type(response: object) -> str:
    return str(response.headers.get("Content-Type", "")).partition(";")[0].strip().lower()


def _valid_runtime_status(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    required = {"phase", "active_mode", "requested_mode", "retryable"}
    if not required.issubset(payload):
        return False
    message = payload.get("message")
    active_mode = payload["active_mode"]
    requested_mode = payload["requested_mode"]
    return (
        isinstance(payload["phase"], str)
        and payload["phase"] in RUNTIME_PHASES
        and (
            active_mode is None
            or (isinstance(active_mode, str) and active_mode in RUNTIME_MODES)
        )
        and (
            requested_mode is None
            or (isinstance(requested_mode, str) and requested_mode in RUNTIME_MODES)
        )
        and type(payload["retryable"]) is bool
        and (
            message is None
            or (
                isinstance(message, str)
                and len(message) <= RUNTIME_STATUS_MAX_MESSAGE_CHARS
            )
        )
    )


def web_readiness() -> tuple[bool, str]:
    """Validate the browser entry, critical transforms, and runtime proxy contract."""

    try:
        for path, expected_type, marker in WEBAPP_STATIC_PROBES:
            with urlopen(f"{WEBAPP_BASE_URL}{path}", timeout=2.0) as response:
                if response.status != 200:
                    return False, f"{path}: HTTP {response.status}"
                content_type = _response_content_type(response)
                if content_type != expected_type:
                    return False, f"{path}: unexpected content type {content_type or 'missing'}"
                body = _read_probe_body(response, limit=WEBAPP_MAX_PROBE_BYTES)
                if not body:
                    return False, f"{path}: empty response"
                if marker is not None and marker not in body:
                    return False, f"{path}: app root marker missing"

        with urlopen(
            f"{WEBAPP_BASE_URL}{RUNTIME_STATUS_PATH}", timeout=2.0
        ) as response:
            if response.status != 200:
                return False, f"{RUNTIME_STATUS_PATH}: HTTP {response.status}"
            content_type = _response_content_type(response)
            if content_type != "application/json":
                return False, (
                    f"{RUNTIME_STATUS_PATH}: unexpected content type "
                    f"{content_type or 'missing'}"
                )
            body = _read_probe_body(response, limit=RUNTIME_STATUS_MAX_BYTES)
            payload = json.loads(body.decode("utf-8"))
            if not _valid_runtime_status(payload):
                return False, f"{RUNTIME_STATUS_PATH}: invalid runtime status schema"
        return True, ""
    except (OSError, URLError, UnicodeError, ValueError):
        return False, "readiness request failed"


def web_ready() -> bool:
    ready, _ = web_readiness()
    return ready


def running_services() -> list[str]:
    result = run(
        [
            "docker",
            "compose",
            "--project-directory",
            str(ROOT),
            "-f",
            str(ROOT / "docker-compose.yml"),
            "--env-file",
            str(ROOT / ".env.example"),
            "--env-file",
            str(ROOT / "docker/orchestration/llm-surgeon.env"),
            "--profile",
            "llm-surgeon",
            "ps",
            "--status",
            "running",
            "--services",
        ],
        timeout=30,
    )
    return sorted(line.strip() for line in result.stdout.splitlines() if line.strip())


def parse_memory_bytes(value: str) -> int:
    """Parse the used side of Docker's ``MemUsage`` column."""

    used = value.split("/", 1)[0].strip()
    match = MEMORY_PATTERN.match(used)
    if match is None:
        raise ValueError(f"unsupported Docker memory value: {value!r}")
    amount = float(match.group(1))
    multiplier = MEMORY_MULTIPLIERS[match.group(2).upper()]
    return int(amount * multiplier)


def memory_samples() -> list[dict[str, object]]:
    result = run(
        [
            "docker",
            "stats",
            "--no-stream",
            "--format",
            "{{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}",
        ],
        timeout=30,
    )
    rows = []
    for line in result.stdout.splitlines():
        name, separator, remainder = line.partition("\t")
        if not separator or "taskplanner" not in name:
            continue
        memory, _, cpu = remainder.partition("\t")
        try:
            memory_used_bytes = parse_memory_bytes(memory)
        except ValueError:
            memory_used_bytes = None
        rows.append(
            {
                "container": name,
                "memory": memory,
                "memory_used_bytes": memory_used_bytes,
                "cpu": cpu,
            }
        )
    return rows


def summarize_memory_growth(
    samples: list[dict[str, object]],
    *,
    warmup_sec: float,
    limit_percent: float,
) -> dict[str, object]:
    """Compare stable early/late windows instead of noisy single samples."""

    by_container: dict[str, list[tuple[float, int]]] = {}
    for sample in samples:
        elapsed = float(sample.get("elapsed_sec", 0.0))
        if elapsed < warmup_sec:
            continue
        for row in sample.get("containers", []):
            if not isinstance(row, dict):
                continue
            name = str(row.get("container", "")).strip()
            value = row.get("memory_used_bytes")
            if not name or not isinstance(value, int) or value < 0:
                continue
            by_container.setdefault(name, []).append((elapsed, value))

    containers: list[dict[str, object]] = []
    evaluated = 0
    violating = 0
    for name in sorted(by_container):
        values = by_container[name]
        row: dict[str, object] = {
            "container": name,
            "sample_count": len(values),
            "first_elapsed_sec": values[0][0],
            "last_elapsed_sec": values[-1][0],
            "minimum_bytes": min(value for _, value in values),
            "maximum_bytes": max(value for _, value in values),
        }
        if len(values) < 3:
            row.update(
                {
                    "status": "insufficient_samples",
                    "growth_percent": None,
                }
            )
            containers.append(row)
            continue
        window = max(1, min(5, math.ceil(len(values) * 0.2)))
        early = statistics.median(value for _, value in values[:window])
        late = statistics.median(value for _, value in values[-window:])
        growth = 0.0 if early <= 0 else 100.0 * (late - early) / early
        status = "passed" if growth <= limit_percent else "failed"
        evaluated += 1
        violating += int(status == "failed")
        row.update(
            {
                "status": status,
                "window_size": window,
                "early_median_bytes": int(early),
                "late_median_bytes": int(late),
                "growth_percent": round(growth, 3),
            }
        )
        containers.append(row)

    return {
        "status": (
            "not_evaluated"
            if not containers or evaluated == 0
            else ("failed" if violating else "passed")
        ),
        "warmup_sec": warmup_sec,
        "limit_percent": limit_percent,
        "evaluated_container_count": evaluated,
        "violating_container_count": violating,
        "containers": containers,
    }


def wait_until_ready(timeout_sec: float) -> tuple[bool, list[str], str]:
    deadline = time.monotonic() + timeout_sec
    last_services: list[str] = []
    last_web_error = "web readiness not checked"
    while time.monotonic() < deadline:
        last_services = running_services()
        web_is_ready, last_web_error = web_readiness()
        if web_is_ready and {"webapp", "taskplanner-runtime"}.issubset(last_services):
            return True, last_services, ""
        time.sleep(1.0)
    return False, last_services, last_web_error


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--restart-iterations", type=int, default=100)
    parser.add_argument("--soak-hours", type=float, default=24.0)
    parser.add_argument("--startup-timeout-sec", type=float, default=120.0)
    parser.add_argument("--sample-period-sec", type=float, default=30.0)
    parser.add_argument("--memory-warmup-sec", type=float, default=300.0)
    parser.add_argument("--memory-growth-max-percent", type=float, default=10.0)
    args = parser.parse_args()
    if args.restart_iterations < 0 or args.soak_hours < 0:
        raise SystemExit("restart iterations and soak hours must be non-negative")

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    restart_rows: list[dict[str, object]] = []
    soak_rows: list[dict[str, object]] = []
    failure = False

    run([str(ROOT / "scripts" / "taskplanner"), "down"], timeout=180)
    try:
        for iteration in range(1, args.restart_iterations + 1):
            started = time.monotonic()
            up = run(
                [str(ROOT / "scripts" / "taskplanner"), "up", "llm-surgeon", "--no-build"],
                timeout=args.startup_timeout_sec + 120,
            )
            ready, services, web_error = (
                wait_until_ready(args.startup_timeout_sec)
                if up.returncode == 0
                else (False, [], "launcher failed before readiness checks")
            )
            row = {
                "iteration": iteration,
                "ready": ready,
                "up_return_code": up.returncode,
                "startup_sec": round(time.monotonic() - started, 3),
                "services": services,
                "web_error": web_error,
            }
            restart_rows.append(row)
            if not ready:
                failure = True
                (output_dir / f"restart-{iteration:03d}.log").write_text(
                    up.stdout, encoding="utf-8"
                )
            down = run([str(ROOT / "scripts" / "taskplanner"), "down"], timeout=180)
            if down.returncode != 0:
                failure = True

        if args.soak_hours > 0:
            up = run(
                [str(ROOT / "scripts" / "taskplanner"), "up", "llm-surgeon", "--no-build"],
                timeout=args.startup_timeout_sec + 120,
            )
            ready, _, web_error = (
                wait_until_ready(args.startup_timeout_sec)
                if up.returncode == 0
                else (False, [], "launcher failed before readiness checks")
            )
            if not ready:
                failure = True
                (output_dir / "soak-start.log").write_text(up.stdout, encoding="utf-8")
            else:
                soak_started = time.monotonic()
                soak_duration = args.soak_hours * 3600.0
                while time.monotonic() - soak_started < soak_duration:
                    services = running_services()
                    web_is_ready, web_error = web_readiness()
                    healthy = web_is_ready and {
                        "webapp",
                        "taskplanner-runtime",
                    }.issubset(services)
                    soak_rows.append(
                        {
                            "sampled_at_utc": datetime.now(timezone.utc).isoformat(),
                            "elapsed_sec": round(time.monotonic() - soak_started, 3),
                            "healthy": healthy,
                            "web_error": web_error,
                            "services": services,
                            "containers": memory_samples(),
                        }
                    )
                    if not healthy:
                        failure = True
                    time.sleep(min(args.sample_period_sec, max(0.1, soak_duration - (time.monotonic() - soak_started))))
    finally:
        run([str(ROOT / "scripts" / "taskplanner"), "down"], timeout=180)

    memory_summary = summarize_memory_growth(
        soak_rows,
        warmup_sec=max(0.0, args.memory_warmup_sec),
        limit_percent=max(0.0, args.memory_growth_max_percent),
    )
    if memory_summary["status"] == "failed":
        failure = True

    with (output_dir / "restart_cycles.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=[
                "iteration",
                "ready",
                "up_return_code",
                "startup_sec",
                "services",
                "web_error",
            ],
        )
        writer.writeheader()
        for row in restart_rows:
            writer.writerow({**row, "services": ",".join(row["services"])})
    with (output_dir / "soak_samples.csv").open(
        "w", encoding="utf-8", newline=""
    ) as stream:
        fields = [
            "sampled_at_utc",
            "elapsed_sec",
            "healthy",
            "web_error",
            "services",
            "container",
            "memory",
            "memory_used_bytes",
            "cpu",
        ]
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for sample in soak_rows:
            containers = sample.get("containers", []) or [{}]
            for container in containers:
                writer.writerow(
                    {
                        "sampled_at_utc": sample.get("sampled_at_utc", ""),
                        "elapsed_sec": sample.get("elapsed_sec", ""),
                        "healthy": sample.get("healthy", ""),
                        "web_error": sample.get("web_error", ""),
                        "services": ",".join(sample.get("services", [])),
                        **container,
                    }
                )
    report = {
        "schema": "taskplanner.runtime_campaign.v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "failed" if failure else "passed",
        "models_loaded_by_campaign": False,
        "restart_iterations": args.restart_iterations,
        "restart_failures": sum(not bool(row["ready"]) for row in restart_rows),
        "soak_hours": args.soak_hours,
        "soak_samples": soak_rows,
        "memory_growth": memory_summary,
    }
    (output_dir / "result.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 1 if failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
