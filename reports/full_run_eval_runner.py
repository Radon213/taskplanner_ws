from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import time
import traceback

import rclpy

from bringup.bt_audit import BTAuditHarness
from bringup.multi_bundle_runtime_probe import MultiBundleRuntimeProbe


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run full start-to-completion taskplanner evaluation.")
    parser.add_argument("--report-dir", required=True)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--timeout-sec", type=float, default=420.0)
    parser.add_argument("--base-seed", type=int, default=240624)
    parser.add_argument(
        "--bundles",
        nargs="+",
        default=["thyroidectomy", "nephrectomy", "inguinal_hernia_repair"],
    )
    return parser.parse_args()


def spin_both(probe: MultiBundleRuntimeProbe, audit: BTAuditHarness, timeout_sec: float = 0.2) -> None:
    rclpy.spin_once(probe, timeout_sec=timeout_sec)
    rclpy.spin_once(audit, timeout_sec=0.0)


def wait_until_both(
    probe: MultiBundleRuntimeProbe,
    audit: BTAuditHarness,
    predicate,
    timeout_sec: float,
    description: str,
) -> None:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        spin_both(probe, audit, timeout_sec=0.2)
        if predicate():
            return
    raise RuntimeError(f"timed out waiting for {description}")


def set_actor_seed(seed: int) -> None:
    completed = subprocess.run(
        ["ros2", "param", "set", "/surgeon_actor", "seed", str(seed)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"failed to set actor seed {seed}: stdout={completed.stdout!r} stderr={completed.stderr!r}"
        )


def write_snapshot(
    path: Path,
    *,
    bundles: list[str],
    repeats: int,
    timeout_sec: float,
    runs: list[dict],
    failures: list[dict],
) -> None:
    payload = {
        "generated_at": time.time(),
        "mode": "full_start_to_completion",
        "bundles": bundles,
        "repeats_per_bundle": repeats,
        "timeout_sec": timeout_sec,
        "runs": runs,
        "failures": failures,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def run_one(
    *,
    probe: MultiBundleRuntimeProbe,
    audit: BTAuditHarness,
    bundle: str,
    repeat_index: int,
    seed: int,
    timeout_sec: float,
) -> dict:
    print(f"RUN_START bundle={bundle} repeat={repeat_index} seed={seed}", flush=True)
    probe.control("stop", allow_failure=True, timeout_sec=20.0)
    select_latency = probe.select_bundle(bundle)
    wait_until_both(
        probe,
        audit,
        lambda: probe.simulation is not None
        and probe.simulation.active_bundle == bundle
        and not probe.simulation.running,
        25.0,
        f"{bundle} idle frame after bundle select",
    )
    set_actor_seed(seed)
    probe.control("reset", allow_failure=True, timeout_sec=30.0)
    wait_until_both(
        probe,
        audit,
        lambda: probe.simulation is not None
        and probe.simulation.active_bundle == bundle
        and not probe.simulation.running,
        25.0,
        f"{bundle} idle frame after reset",
    )
    audit.reset_run(bundle)
    probe._reset_window()
    start_latency, start_message = probe.control("start", timeout_sec=35.0)
    wait_until_both(
        probe,
        audit,
        lambda: probe.world is not None
        and probe.world.procedure_id == bundle
        and probe.world.running
        and probe.world.execution_state == "running",
        35.0,
        f"{bundle} running world",
    )
    run_started = time.perf_counter()
    completed = False
    while time.perf_counter() - run_started < timeout_sec:
        spin_both(probe, audit, timeout_sec=0.2)
        world_state = probe.world.execution_state if probe.world is not None else ""
        sim_state = probe.simulation.execution_state if probe.simulation is not None else ""
        if world_state == "completed" or sim_state == "completed":
            completed = True
            break
    duration = time.perf_counter() - run_started
    end_spin_deadline = time.perf_counter() + 2.0
    while time.perf_counter() < end_spin_deadline:
        spin_both(probe, audit, timeout_sec=0.1)

    report = probe.report_bundle(bundle, duration, select_latency, start_latency)
    bt_report = audit.report_for(duration)
    report.update(
        {
            "repeat_index": repeat_index,
            "seed": seed,
            "completed": completed,
            "timeout_sec": timeout_sec,
            "run_duration_sec": round(duration, 3),
            "start_message": start_message,
            "bt_audit": bt_report,
        }
    )
    if not completed:
        probe.control("stop", allow_failure=True, timeout_sec=25.0)
    print(
        "RUN_DONE "
        f"bundle={bundle} repeat={repeat_index} completed={completed} duration={duration:.1f}s "
        f"final={report.get('final_execution_state')} phase={report.get('final_phase')} "
        f"phase={report['vlm']['phase_alignment']['scoreboard']['vlm']['display']} "
        f"tool={report['vlm']['tool_alignment']['scoreboard']['vlm']['display']} "
        f"bt_blockers={bt_report['blocker_count']} bt_suspicious={bt_report['suspicious_count']}",
        flush=True,
    )
    return report


def main() -> int:
    args = parse_args()
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    raw_path = report_dir / "full_run_raw.json"
    runs: list[dict] = []
    failures: list[dict] = []

    rclpy.init()
    probe = MultiBundleRuntimeProbe()
    audit = BTAuditHarness()
    try:
        probe.wait_for_services(timeout_sec=45.0)
        audit.wait_for_services(timeout_sec=45.0)
        audit.wait_for_catalog_entry(timeout_sec=45.0)
        write_snapshot(
            raw_path,
            bundles=list(args.bundles),
            repeats=args.repeats,
            timeout_sec=args.timeout_sec,
            runs=runs,
            failures=failures,
        )
        for bundle_index, bundle in enumerate(args.bundles):
            for repeat in range(1, args.repeats + 1):
                seed = args.base_seed + bundle_index * 100 + repeat
                try:
                    result = run_one(
                        probe=probe,
                        audit=audit,
                        bundle=bundle,
                        repeat_index=repeat,
                        seed=seed,
                        timeout_sec=args.timeout_sec,
                    )
                    runs.append(result)
                except Exception as exc:
                    failures.append(
                        {
                            "bundle": bundle,
                            "repeat_index": repeat,
                            "seed": seed,
                            "error": str(exc),
                            "traceback": traceback.format_exc(),
                        }
                    )
                    print(f"RUN_FAILED bundle={bundle} repeat={repeat} error={exc}", flush=True)
                    try:
                        probe.control("stop", allow_failure=True, timeout_sec=25.0)
                    except Exception:
                        pass
                write_snapshot(
                    raw_path,
                    bundles=list(args.bundles),
                    repeats=args.repeats,
                    timeout_sec=args.timeout_sec,
                    runs=runs,
                    failures=failures,
                )
    finally:
        try:
            probe.control("stop", allow_failure=True, timeout_sec=25.0)
        except Exception:
            pass
        write_snapshot(
            raw_path,
            bundles=list(args.bundles),
            repeats=args.repeats,
            timeout_sec=args.timeout_sec,
            runs=runs,
            failures=failures,
        )
        probe.destroy_node()
        audit.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    print(f"FULL_EVAL_RAW={raw_path}", flush=True)
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
