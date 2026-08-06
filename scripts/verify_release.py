#!/usr/bin/env python3
"""Run Taskplanner release gates and always emit an auditable report bundle."""

from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import html
import json
import os
from pathlib import Path
import platform
import shlex
import subprocess
import sys
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "release" / "verification.json"
TIERS = {"quick": 0, "rc": 1, "full": 2}


@dataclass(frozen=True)
class CheckSpec:
    name: str
    command: str
    classification: str = "product"
    minimum_tier: str = "quick"
    required: bool = True
    timeout_sec: int = 900


@dataclass
class CheckResult:
    name: str
    classification: str
    required: bool
    status: str
    return_code: int | None
    duration_sec: float
    command: str
    log_path: str
    detail: str = ""


def shell_join(parts: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in parts)


def compose_prefix(*, mode: str | None = None) -> list[str]:
    parts = [
        "docker",
        "compose",
        "--project-directory",
        str(ROOT),
        "-f",
        str(ROOT / "docker-compose.yml"),
        "--env-file",
        str(ROOT / ".env.example"),
    ]
    if mode:
        parts.extend(
            ["--env-file", str(ROOT / "docker" / "orchestration" / f"{mode}.env")]
        )
    return parts


def selected(tier: str, minimum_tier: str) -> bool:
    return TIERS[tier] >= TIERS[minimum_tier]


def product_build_command(config: dict[str, Any], run_id: str) -> str:
    packages = " ".join(shlex.quote(value) for value in config["product_packages"])
    artifact_root = f"/workspaces/taskplanner_ws/test_outputs/release-build/{run_id}"
    inner = f"""
set -eo pipefail
source /opt/ros/jazzy/setup.bash
source /opt/btops_ws/install/setup.bash
export PYTHONDONTWRITEBYTECODE=1
rm -rf {artifact_root}
mkdir -p {artifact_root}
colcon --log-base {artifact_root}/log build \
  --base-paths /workspaces/taskplanner_ws/src \
  --build-base {artifact_root}/build \
  --install-base {artifact_root}/install \
  --symlink-install \
  --packages-select {packages}
source {artifact_root}/install/setup.bash
set -u
colcon --log-base {artifact_root}/test-log test \
  --build-base {artifact_root}/build \
  --install-base {artifact_root}/install \
  --packages-select {packages} \
  --event-handlers console_direct+
colcon test-result --test-result-base {artifact_root}/build --verbose
""".strip()
    return shell_join(
        compose_prefix()
        + [
            "--profile",
            "dev",
            "run",
            "--rm",
            "--no-deps",
            "taskplanner-dev",
            "bash",
            "-lc",
            inner,
        ]
    )


def top_level_test_command(config: dict[str, Any], run_id: str) -> str:
    tests = " ".join(shlex.quote(value) for value in config["top_level_contract_tests"])
    install_root = f"/workspaces/taskplanner_ws/test_outputs/release-build/{run_id}/install"
    inner = f"""
set -eo pipefail
source /opt/ros/jazzy/setup.bash
source /opt/btops_ws/install/setup.bash
source {install_root}/setup.bash
set -u
cd /workspaces/taskplanner_ws
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH=/workspaces/taskplanner_ws:${{PYTHONPATH:-}}
pytest -q {tests}
""".strip()
    return shell_join(
        compose_prefix()
        + [
            "--profile",
            "dev",
            "run",
            "--rm",
            "--no-deps",
            "taskplanner-dev",
            "bash",
            "-lc",
            inner,
        ]
    )


def fault_campaign_command(run_id: str, report_dir: Path) -> str:
    install_root = f"/workspaces/taskplanner_ws/test_outputs/release-build/{run_id}/install"
    host_output_dir = report_dir / "fault_campaign"
    host_output_dir.mkdir(parents=True, exist_ok=True)
    output_dir = "/release-output"
    inner = f"""
set -eo pipefail
source /opt/ros/jazzy/setup.bash
source /opt/btops_ws/install/setup.bash
source {install_root}/setup.bash
set -u
cd /workspaces/taskplanner_ws
export PYTHONDONTWRITEBYTECODE=1
python3 scripts/release_fault_campaign.py --output-dir {shlex.quote(output_dir)}
""".strip()
    return shell_join(
        compose_prefix()
        + [
            "--profile",
            "dev",
            "run",
            "--rm",
            "--no-deps",
            "--volume",
            f"{host_output_dir}:{output_dir}",
            "taskplanner-dev",
            "bash",
            "-lc",
            inner,
        ]
    )


def ros_fault_probe_command(run_id: str, report_dir: Path) -> str:
    install_root = f"/workspaces/taskplanner_ws/test_outputs/release-build/{run_id}/install"
    host_output_dir = report_dir / "ros_fault_probe"
    host_output_dir.mkdir(parents=True, exist_ok=True)
    output_dir = "/release-output/run"
    inner = f"""
set -eo pipefail
source /opt/ros/jazzy/setup.bash
source /opt/btops_ws/install/setup.bash
source {install_root}/setup.bash
set -u
cd /workspaces/taskplanner_ws
export PYTHONDONTWRITEBYTECODE=1
python3 scripts/release_ros_fault_probe.py \
  --output-dir {shlex.quote(output_dir)} \
  --ros-domain-id 197
""".strip()
    return shell_join(
        compose_prefix()
        + [
            "--profile",
            "dev",
            "run",
            "--rm",
            "--no-deps",
            "--volume",
            f"{host_output_dir}:/release-output",
            "taskplanner-dev",
            "bash",
            "-lc",
            inner,
        ]
    )


def shadow_campaign_command(
    *,
    config: dict[str, Any],
    run_id: str,
    report_dir: Path,
    options: dict[str, Any],
) -> str:
    install_root = f"/workspaces/taskplanner_ws/test_outputs/release-build/{run_id}/install"
    cases = " ".join(shlex.quote(case_id) for case_id in config["cases"])
    fault_argument = ""
    if options.get("fault_scenario_path") is not None:
        fault_argument = " \\\n  --fault-scenario-path /release-fault-scenario.yaml"
    inner = f"""
set -eo pipefail
source /opt/ros/jazzy/setup.bash
source /opt/btops_ws/install/setup.bash
source {install_root}/setup.bash
set -u
cd /workspaces/taskplanner_ws
export PYTHONDONTWRITEBYTECODE=1
python3 scripts/release_shadow_campaign.py \
  --dataset-root /release-shadow-dataset \
  --annotation-root /release-shadow-annotations \
  --output-dir /release-shadow-output/shadow_campaign \
  --cases {cases} \
  --provider-id {shlex.quote(options['provider_id'])} \
  --base-url {shlex.quote(options['base_url'])} \
  --model-id {shlex.quote(options['model_id'])} \
  --request-timeout-sec {float(options['request_timeout_sec'])} \
  --retry-count {int(options['retry_count'])} \
  --vlm-wait-timeout-sec {float(options['wait_timeout_sec'])}{fault_argument}
""".strip()
    command = compose_prefix() + [
            "--profile",
            "dev",
            "run",
            "--rm",
            "--no-deps",
            "--volume",
            f"{options['dataset_root']}:/release-shadow-dataset:ro",
            "--volume",
            f"{options['annotation_root']}:/release-shadow-annotations:ro",
            "--volume",
            f"{report_dir}:/release-shadow-output",
    ]
    if options.get("fault_scenario_path") is not None:
        command.extend(
            [
                "--volume",
                f"{options['fault_scenario_path']}:/release-fault-scenario.yaml:ro",
            ]
        )
    command.extend(
        [
            "taskplanner-dev",
            "bash",
            "-lc",
            inner,
        ]
    )
    return shell_join(command)


def shadow_metric_gate_command(
    *,
    report_dir: Path,
    baseline_report_dir: Path,
    config: dict[str, Any],
    options: dict[str, Any],
) -> str:
    thresholds = config["thresholds"]
    command = [
            sys.executable,
            str(ROOT / "scripts" / "evaluate_release_metrics.py"),
            "--candidate-report-dir",
            str(report_dir / "shadow_campaign" / "report"),
            "--baseline-report-dir",
            str(baseline_report_dir),
            "--output-dir",
            str(report_dir / "shadow_metric_gate"),
            "--max-regression-pp",
            str(options["max_regression_pp"]),
            "--prompt-chars-max",
            str(thresholds["prompt_chars_max"]),
            "--vlm-p95-max-sec",
            str(float(thresholds["vlm_fresh_frame_p95_ms"]) / 1000.0),
    ]
    if options.get("safety_only"):
        command.append("--safety-only")
    return shell_join(command)


def quick_contract_command() -> str:
    python_paths = [
        "/workspaces/taskplanner_ws/src/simulation_runtime",
        "/workspaces/taskplanner_ws/src/procedure_spec",
        "/workspaces/taskplanner_ws/src/vlm_node",
    ]
    inner = f"""
set -eo pipefail
source /opt/ros/jazzy/setup.bash
source /opt/btops_ws/install/setup.bash
source /workspaces/taskplanner_ws/install/setup.bash
set -u
cd /workspaces/taskplanner_ws
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH={':'.join(python_paths)}:${{PYTHONPATH:-}}
pytest -q \
  src/simulation_runtime/test/test_fault_scenario.py \
  src/simulation_runtime/test/test_source_health_monitor.py \
  src/vlm_node/test/test_public_input_boundary.py \
  src/vlm_node/test/test_mayo_cam4_corroboration.py \
  -k 'fault or source or token_bounded or prompt_budget'
""".strip()
    return shell_join(
        compose_prefix()
        + [
            "--profile",
            "dev",
            "run",
            "--rm",
            "--no-deps",
            "taskplanner-dev",
            "bash",
            "-lc",
            inner,
        ]
    )


def build_checks(
    *,
    config: dict[str, Any],
    run_id: str,
    report_dir: Path,
    tier: str,
    verify_dataset_payloads: bool,
    restart_iterations: int,
    soak_hours: float,
    shadow_options: dict[str, Any] | None,
) -> list[CheckSpec]:
    compose_config_checks = [
        CheckSpec(
            name=f"compose_config_{mode}",
            classification="infrastructure",
            command=shell_join(
                compose_prefix(mode=mode) + ["--profile", mode, "config", "--quiet"]
            ),
            timeout_sec=120,
        )
        for mode in ("live", "llm-surgeon", "replay")
    ]
    dataset_args = [
        sys.executable,
        str(ROOT / "scripts" / "create_release_asset_manifest.py"),
        "--output",
        str(report_dir / "external_assets.json"),
    ]
    if verify_dataset_payloads:
        dataset_args.append("--verify-payloads")

    shadow_checks: list[CheckSpec] = []
    if shadow_options is not None:
        shadow_checks = [
            CheckSpec(
                name="shadow_12case_campaign",
                command=shadow_campaign_command(
                    config=config,
                    run_id=run_id,
                    report_dir=report_dir,
                    options=shadow_options,
                ),
                classification="performance",
                minimum_tier="rc",
                timeout_sec=7200,
            ),
            CheckSpec(
                name="shadow_metric_gate",
                command=shadow_metric_gate_command(
                    report_dir=report_dir,
                    baseline_report_dir=shadow_options["baseline_report_dir"],
                    config=config,
                    options=shadow_options,
                ),
                classification="performance",
                minimum_tier="rc",
                timeout_sec=300,
            ),
        ]

    checks = [
        CheckSpec(
            name="workspace_diff_safety",
            command="git diff --check",
            classification="source",
            timeout_sec=60,
        ),
        *compose_config_checks,
        CheckSpec(
            name="fault_scenario_schemas",
            command=(
                "PYTHONPATH=src/simulation_runtime python3 -c "
                + shlex.quote(
                    "from pathlib import Path; "
                    "from simulation_runtime.fault_scenario import FaultScenario; "
                    "paths=[p for p in sorted(Path('config/fault_scenarios').glob('*.yaml')) "
                    "if p.read_text().startswith('schema: taskplanner.fault_scenario.v1')]; "
                    "assert paths; [FaultScenario.load(path) for path in paths]; "
                    "print(f'validated {len(paths)} fault scenarios')"
                )
            ),
            classification="product",
            timeout_sec=60,
        ),
        CheckSpec(
            name="web_domain_contract",
            command="npm --prefix webapp run check:domain-hardcoding",
            classification="product",
            timeout_sec=120,
        ),
        CheckSpec(
            name="web_build",
            command=(
                "rm -rf "
                + shlex.quote(str(ROOT / "test_outputs" / "release-web" / run_id))
                + " && npm --prefix webapp run build -- --outDir "
                + shlex.quote(str(ROOT / "test_outputs" / "release-web" / run_id))
            ),
            classification="product",
            timeout_sec=300,
        ),
        CheckSpec(
            name="docker_image_build",
            command=shell_join(
                compose_prefix()
                + ["--profile", "dev", "build", "taskplanner-dev"]
            ),
            classification="infrastructure",
            minimum_tier="rc",
            timeout_sec=3600,
        ),
        CheckSpec(
            name="current_image_available",
            command="docker image inspect taskplanner-ws:dev >/dev/null",
            classification="infrastructure",
            timeout_sec=60,
        ),
        CheckSpec(
            name="quick_safety_contracts",
            command=quick_contract_command(),
            classification="product",
            timeout_sec=600,
        ),
        CheckSpec(
            name="external_asset_manifest",
            command=shell_join(dataset_args),
            classification="data",
            timeout_sec=7200 if verify_dataset_payloads else 120,
        ),
        CheckSpec(
            name="product_colcon_build_and_test",
            command=product_build_command(config, run_id),
            classification="product",
            minimum_tier="rc",
            timeout_sec=3600,
        ),
        CheckSpec(
            name="annotation_and_shadow_contracts",
            command=top_level_test_command(config, run_id),
            classification="product",
            minimum_tier="rc",
            timeout_sec=1800,
        ),
        CheckSpec(
            name="deterministic_fault_campaign",
            command=fault_campaign_command(run_id, report_dir),
            classification="fault_recovery",
            minimum_tier="rc",
            timeout_sec=900,
        ),
        CheckSpec(
            name="live_ros_fault_and_action_probe",
            command=ros_fault_probe_command(run_id, report_dir),
            classification="fault_recovery",
            minimum_tier="rc",
            timeout_sec=180,
        ),
        CheckSpec(
            name="web_playwright",
            command=(
                "rm -rf "
                + shlex.quote(str(report_dir / "playwright"))
                + " && PLAYWRIGHT_OUTPUT_DIR="
                + shlex.quote(str(report_dir / "playwright"))
                + " npm --prefix webapp run test:e2e"
            ),
            classification="product",
            minimum_tier="rc",
            timeout_sec=900,
        ),
        CheckSpec(
            name="release_sbom",
            command=shell_join(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "generate_sbom.py"),
                    "--image",
                    "taskplanner-ws:dev",
                    "--output",
                    str(report_dir / "sbom.spdx.json"),
                ]
            ),
            classification="supply_chain",
            minimum_tier="rc",
            timeout_sec=600,
        ),
        *shadow_checks,
        CheckSpec(
            name="restart_and_soak_campaign",
            command=shell_join(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "release_runtime_campaign.py"),
                    "--output-dir",
                    str(report_dir / "runtime_campaign"),
                    "--restart-iterations",
                    str(restart_iterations),
                    "--soak-hours",
                    str(soak_hours),
                ]
            ),
            classification="durability",
            minimum_tier="full",
            timeout_sec=max(3600, int(soak_hours * 3600 + restart_iterations * 180)),
        ),
    ]
    return [check for check in checks if selected(tier, check.minimum_tier)]


def run_check(spec: CheckSpec, logs_dir: Path) -> CheckResult:
    log_path = logs_dir / f"{spec.name}.log"
    started = time.monotonic()
    return_code: int | None = None
    detail = ""
    print(f"[{spec.name}] running", flush=True)
    try:
        with log_path.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                ["bash", "-lc", spec.command],
                cwd=ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            try:
                return_code = process.wait(timeout=spec.timeout_sec)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                return_code = 124
                detail = f"timeout after {spec.timeout_sec}s"
    except OSError as exc:
        return_code = 127
        detail = str(exc)
        log_path.write_text(detail + "\n", encoding="utf-8")
    duration = time.monotonic() - started
    status = "passed" if return_code == 0 else "failed"
    print(f"[{spec.name}] {status} ({duration:.1f}s)", flush=True)
    return CheckResult(
        name=spec.name,
        classification=spec.classification,
        required=spec.required,
        status=status,
        return_code=return_code,
        duration_sec=round(duration, 3),
        command=spec.command,
        log_path=str(log_path.relative_to(ROOT)),
        detail=detail,
    )


def git_metadata() -> dict[str, Any]:
    def output(command: str) -> str:
        return subprocess.check_output(
            ["bash", "-lc", command], cwd=ROOT, text=True
        ).strip()

    status = output("git status --porcelain --untracked-files=normal")
    return {
        "commit": output("git rev-parse HEAD"),
        "branch": output("git branch --show-current"),
        "clean": not bool(status),
        "changed_paths": len(status.splitlines()) if status else 0,
    }


def render_summary(report: dict[str, Any], report_dir: Path) -> None:
    results = report["checks"]
    passed = sum(item["status"] == "passed" for item in results)
    failed = sum(item["status"] == "failed" for item in results)
    lines = [
        "# Taskplanner release verification",
        "",
        f"- Run: `{report['run_id']}`",
        f"- Tier: `{report['tier']}`",
        f"- Status: **{report['status']}**",
        f"- Checks: {passed} passed, {failed} failed",
        f"- Git commit: `{report['git']['commit']}`",
        f"- Clean worktree: `{report['git']['clean']}`",
        "",
        "| Check | Class | Status | Seconds | Log |",
        "|---|---|---:|---:|---|",
    ]
    for item in results:
        lines.append(
            f"| {item['name']} | {item['classification']} | {item['status']} | "
            f"{item['duration_sec']:.1f} | `{item['log_path']}` |"
        )
    lines.extend(
        [
            "",
            "## Release boundary",
            "",
            "This is the software-stage gate. Physical robot pose, grasp, collision, "
            "E-stop, calibration, and site-network acceptance remain a separate stage-2 gate.",
        ]
    )
    (report_dir / "SUMMARY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    with (report_dir / "checks.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=[
                "name",
                "classification",
                "required",
                "status",
                "return_code",
                "duration_sec",
                "log_path",
                "detail",
            ],
        )
        writer.writeheader()
        for item in results:
            writer.writerow({key: item[key] for key in writer.fieldnames})

    width, height = 960, 52 + len(results) * 34
    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        '<text x="20" y="28" font-family="sans-serif" font-size="20" font-weight="700">Taskplanner release gates</text>',
    ]
    for index, item in enumerate(results):
        y = 52 + index * 34
        color = "#12805c" if item["status"] == "passed" else "#c53b45"
        svg.append(f'<rect x="20" y="{y - 18}" width="14" height="14" rx="2" fill="{color}"/>')
        svg.append(
            f'<text x="44" y="{y - 6}" font-family="sans-serif" font-size="14">'
            f'{html.escape(item["name"])} ({html.escape(item["classification"])})</text>'
        )
        svg.append(
            f'<text x="920" y="{y - 6}" text-anchor="end" font-family="monospace" font-size="13">'
            f'{item["duration_sec"]:.1f}s</text>'
        )
    svg.append("</svg>")
    (report_dir / "checks.svg").write_text("\n".join(svg) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tier", choices=sorted(TIERS, key=TIERS.get), default="rc")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--require-clean", action="store_true")
    parser.add_argument("--verify-dataset-payloads", action="store_true")
    parser.add_argument("--restart-iterations", type=int)
    parser.add_argument("--soak-hours", type=float)
    parser.add_argument("--shadow-dataset-root", type=Path)
    parser.add_argument(
        "--shadow-annotation-root",
        type=Path,
        default=ROOT / "annotations" / "observable_tool_events",
    )
    parser.add_argument("--shadow-baseline-report-dir", type=Path)
    parser.add_argument("--shadow-provider-id", default="ninfer")
    parser.add_argument("--shadow-base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--shadow-model-id", default="qwen3.6-35b-a3b")
    parser.add_argument("--shadow-request-timeout-sec", type=float, default=60.0)
    parser.add_argument("--shadow-retry-count", type=int, default=1)
    parser.add_argument("--shadow-wait-timeout-sec", type=float, default=130.0)
    parser.add_argument("--shadow-fault-scenario", type=Path)
    parser.add_argument("--shadow-max-regression-pp", type=float)
    parser.add_argument(
        "--shadow-safety-only",
        action="store_true",
        help="Require safety and command gates while treating noisy-set accuracy as advisory.",
    )
    args = parser.parse_args()

    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if config.get("schema") != "taskplanner.release_verification.v1":
        raise SystemExit("unsupported release verification config")
    now = datetime.now(timezone.utc)
    run_id = now.strftime("%Y%m%dT%H%M%SZ") + f"-{args.tier}"
    report_dir = (args.output_dir or ROOT / "reports" / "release" / run_id).resolve()
    logs_dir = report_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=False)

    git = git_metadata()
    restart_iterations = args.restart_iterations or int(
        config["full_gate"]["restart_iterations"]
    )
    soak_hours = args.soak_hours if args.soak_hours is not None else float(
        config["full_gate"]["soak_hours"]
    )
    shadow_options: dict[str, Any] | None = None
    if args.shadow_fault_scenario is not None and args.shadow_dataset_root is None:
        parser.error("--shadow-fault-scenario requires --shadow-dataset-root")
    if args.shadow_dataset_root is not None:
        if args.shadow_baseline_report_dir is None:
            parser.error(
                "--shadow-baseline-report-dir is required with "
                "--shadow-dataset-root"
            )
        for label, path in (
            ("shadow dataset", args.shadow_dataset_root),
            ("shadow annotations", args.shadow_annotation_root),
            ("shadow baseline report", args.shadow_baseline_report_dir),
            ("shadow fault scenario", args.shadow_fault_scenario),
        ):
            if path is not None and not path.exists():
                parser.error(f"{label} path does not exist: {path}")
        shadow_options = {
            "dataset_root": args.shadow_dataset_root.resolve(),
            "annotation_root": args.shadow_annotation_root.resolve(),
            "baseline_report_dir": args.shadow_baseline_report_dir.resolve(),
            "provider_id": args.shadow_provider_id,
            "base_url": args.shadow_base_url,
            "model_id": args.shadow_model_id,
            "request_timeout_sec": args.shadow_request_timeout_sec,
            "retry_count": args.shadow_retry_count,
            "wait_timeout_sec": args.shadow_wait_timeout_sec,
            "fault_scenario_path": (
                args.shadow_fault_scenario.resolve()
                if args.shadow_fault_scenario is not None
                else None
            ),
            "max_regression_pp": (
                args.shadow_max_regression_pp
                if args.shadow_max_regression_pp is not None
                else config["thresholds"][
                    "clean_accuracy_regression_max_percentage_points"
                ]
            ),
            "safety_only": bool(args.shadow_safety_only),
        }
    specs = build_checks(
        config=config,
        run_id=run_id,
        report_dir=report_dir,
        tier=args.tier,
        verify_dataset_payloads=args.verify_dataset_payloads,
        restart_iterations=restart_iterations,
        soak_hours=soak_hours,
        shadow_options=shadow_options,
    )
    results: list[CheckResult] = []
    if args.require_clean and not git["clean"]:
        results.append(
            CheckResult(
                name="clean_worktree",
                classification="source",
                required=True,
                status="failed",
                return_code=1,
                duration_sec=0.0,
                command="git status --porcelain",
                log_path="",
                detail=f"{git['changed_paths']} changed paths",
            )
        )
    for spec in specs:
        results.append(run_check(spec, logs_dir))

    failed_required = [item for item in results if item.required and item.status != "passed"]
    report = {
        "schema": "taskplanner.release_verification_result.v1",
        "run_id": run_id,
        "created_at_utc": now.isoformat(),
        "tier": args.tier,
        "status": "passed" if not failed_required else "failed",
        "software_stage_only": True,
        "git": git,
        "host": {
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "thresholds": config["thresholds"],
        "full_gate": {
            "restart_iterations": restart_iterations,
            "soak_hours": soak_hours,
        },
        "shadow_gate_enabled": shadow_options is not None,
        "checks": [asdict(item) for item in results],
    }
    (report_dir / "result.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    render_summary(report, report_dir)
    print(f"release report: {report_dir}")
    print(f"status: {report['status']}")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
