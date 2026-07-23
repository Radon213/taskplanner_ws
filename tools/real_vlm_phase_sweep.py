#!/usr/bin/env python3
"""Sweep every declared procedure phase against a running real-VLM runtime."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
from typing import Any

from ament_index_python.packages import get_package_share_directory
from procedure_spec import load_bundle
import rclpy
from rclpy.node import Node
from surgical_msgs.msg import SimulationState, VLMHealth, VLMResult, WorldState
from surgical_msgs.srv import ControlSimulation, SelectSimulationBundle


DEFAULT_BUNDLES = ("thyroidectomy", "nephrectomy", "inguinal_hernia_repair")


def stamp_sec(stamp) -> float:
    return float(stamp.sec) + float(stamp.nanosec) / 1_000_000_000.0


class PhaseSweep(Node):
    def __init__(self) -> None:
        super().__init__("real_vlm_phase_sweep")
        self.world: WorldState | None = None
        self.simulation: SimulationState | None = None
        self.health_samples: list[tuple[float, VLMHealth]] = []
        self.results: list[tuple[float, VLMResult]] = []
        self.select_client = self.create_client(
            SelectSimulationBundle, "/simulation/select_bundle"
        )
        self.control_client = self.create_client(
            ControlSimulation, "/simulation/control"
        )
        self.create_subscription(WorldState, "/twin/world_state", self.on_world, 50)
        self.create_subscription(
            SimulationState, "/simulation/state", self.on_simulation, 50
        )
        self.create_subscription(VLMHealth, "/vlm/health", self.on_health, 50)
        self.create_subscription(VLMResult, "/vlm/result", self.on_result, 50)

    def on_world(self, msg: WorldState) -> None:
        self.world = msg

    def on_simulation(self, msg: SimulationState) -> None:
        self.simulation = msg

    def on_health(self, msg: VLMHealth) -> None:
        self.health_samples.append((time.monotonic(), msg))

    def on_result(self, msg: VLMResult) -> None:
        self.results.append((time.monotonic(), msg))

    def wait_until(self, predicate, timeout_sec: float, description: str) -> None:
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            rclpy.spin_once(self, timeout_sec=0.2)
            if predicate():
                return
        raise TimeoutError(description)

    def wait_for_services(self, timeout_sec: float = 30.0) -> None:
        self.wait_until(
            lambda: self.select_client.wait_for_service(timeout_sec=0.1)
            and self.control_client.wait_for_service(timeout_sec=0.1),
            timeout_sec,
            "simulation services unavailable",
        )

    def select_bundle(self, bundle: str) -> None:
        request = SelectSimulationBundle.Request()
        request.bundle_name = bundle
        request.restart_if_running = False
        future = self.select_client.call_async(request)
        rclpy.spin_until_future_complete(self, future, timeout_sec=30.0)
        response = future.result()
        if response is None or not response.success:
            raise RuntimeError(
                f"select {bundle}: {response.message if response else 'no response'}"
            )

    def control(
        self, command: str, start_phase_id: str = "", allow_failure: bool = False
    ) -> None:
        request = ControlSimulation.Request()
        request.command = command
        request.start_phase_id = start_phase_id
        future = self.control_client.call_async(request)
        rclpy.spin_until_future_complete(self, future, timeout_sec=30.0)
        response = future.result()
        if response is None:
            if allow_failure:
                return
            raise RuntimeError(f"{command}: no response")
        if not response.success and not allow_failure:
            raise RuntimeError(f"{command}: {response.message}")

    def run_phase(
        self,
        bundle: str,
        phase: str,
        allowed_phases: set[str],
        timeout_sec: float,
    ) -> dict[str, Any]:
        self.control("stop", allow_failure=True)
        self.control("start", start_phase_id=phase)
        self.wait_until(
            lambda: self.world is not None
            and self.world.running
            and self.world.procedure_id == bundle
            and self.world.filtered_phase == phase,
            20.0,
            f"{bundle}/{phase}: expected running world not observed",
        )

        observation_started = time.monotonic()
        self.wait_until(
            lambda: any(received >= observation_started for received, _ in self.results),
            timeout_sec,
            f"{bundle}/{phase}: VLM result timeout",
        )
        self.wait_until(
            lambda: any(
                received >= observation_started for received, _ in self.health_samples
            ),
            timeout_sec,
            f"{bundle}/{phase}: VLM health timeout",
        )

        result = next(
            msg
            for received, msg in reversed(self.results)
            if received >= observation_started
        )
        health = next(
            msg
            for received, msg in reversed(self.health_samples)
            if received >= observation_started
        )
        reported_phases = [str(value) for value in result.phase_ids]
        out_of_scope = sorted(set(reported_phases).difference(allowed_phases))
        top_phase = reported_phases[0] if reported_phases else ""
        world_phase = str(self.world.filtered_phase if self.world else "")
        checks = {
            "world_phase_matches": world_phase == phase,
            "vlm_connected": bool(health.connected),
            "vlm_healthy": bool(health.healthy),
            "vlm_error_empty": not bool(str(health.last_error)),
            "vlm_result_present": bool(result.raw_json),
            "vlm_phase_in_scope": not out_of_scope,
            "vlm_top_phase_matches": top_phase == phase,
        }
        return {
            "bundle": bundle,
            "phase": phase,
            "passed": all(checks.values()),
            "checks": checks,
            "world_phase": world_phase,
            "vlm_top_phase": top_phase,
            "vlm_phase_ids": reported_phases,
            "vlm_phase_confidences": [
                round(float(value), 4) for value in result.phase_confidences
            ],
            "vlm_uncertainty": round(float(result.uncertainty), 4),
            "out_of_scope_phases": out_of_scope,
            "model_id": str(health.model_id),
            "image_source": str(health.image_source),
            "latency_sec": round(float(health.latency_sec), 3),
            "parse_retry_count": int(health.parse_retry_count),
            "last_mode": str(health.last_mode),
            "last_error": str(health.last_error),
            "result_stamp_sec": round(stamp_sec(result.stamp), 6),
        }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundles", nargs="+", default=list(DEFAULT_BUNDLES))
    parser.add_argument("--phase-timeout-sec", type=float, default=25.0)
    parser.add_argument(
        "--report-path", default="reports/real_vlm_phase_sweep_latest.json"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    spec_root = (
        Path(get_package_share_directory("procedure_spec")) / "specs"
    )
    rclpy.init()
    probe = PhaseSweep()
    rows: list[dict[str, Any]] = []
    failures: list[str] = []
    started = time.time()
    try:
        probe.wait_for_services()
        for bundle in args.bundles:
            spec = load_bundle(spec_root / bundle)
            phases = [str(value) for value in spec.phase_ids]
            allowed = set(phases)
            probe.control("stop", allow_failure=True)
            probe.select_bundle(bundle)
            probe.wait_until(
                lambda: probe.simulation is not None
                and probe.simulation.active_bundle == bundle
                and not probe.simulation.running,
                20.0,
                f"{bundle}: idle state after selection not observed",
            )
            for phase in phases:
                print(f"[RUN] {bundle}/{phase}", flush=True)
                try:
                    row = probe.run_phase(
                        bundle, phase, allowed, float(args.phase_timeout_sec)
                    )
                except Exception as exc:
                    row = {
                        "bundle": bundle,
                        "phase": phase,
                        "passed": False,
                        "error": str(exc),
                    }
                rows.append(row)
                if not row["passed"]:
                    failures.append(f"{bundle}/{phase}")
                print(
                    f"[{'PASS' if row['passed'] else 'FAIL'}] "
                    f"{bundle}/{phase} "
                    f"world={row.get('world_phase', '-')} "
                    f"vlm={row.get('vlm_top_phase', '-')} "
                    f"latency={row.get('latency_sec', '-')} "
                    f"error={row.get('last_error', row.get('error', ''))}",
                    flush=True,
                )
        probe.control("stop", allow_failure=True)
    finally:
        probe.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()

    report = {
        "mode": "real_vlm_no_isaac_no_image_camera",
        "model_id": "qwen2.5-vl-7b-instruct",
        "generated_at": time.time(),
        "elapsed_sec": round(time.time() - started, 2),
        "total": len(rows),
        "passed": sum(1 for row in rows if row["passed"]),
        "failed": len(failures),
        "failures": failures,
        "phases": rows,
    }
    output = Path(args.report_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
