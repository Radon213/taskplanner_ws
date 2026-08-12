#!/usr/bin/env python3
"""Exercise every deterministic fault scenario and emit release evidence."""

from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass
from io import BytesIO
import json
from pathlib import Path
from typing import Any

from PIL import Image

from simulation_runtime.fault_scenario import (
    FaultScenario,
    transform_image_bytes,
    transform_speech_text,
)
from simulation_runtime.source_health_monitor import READY, SourceTracker
from surgical_interop_execution.fault_action_emulator import (
    EmulatorProfile,
    valid_tool_transition,
)


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ALIASES = {
    "flir": ("flir",),
    "cam4": ("cam4",),
    "speech": ("speech", "sentence"),
    "vlm": ("vlm_result", "vlm_health"),
}


@dataclass(frozen=True)
class ScenarioResult:
    scenario_id: str
    seed: int
    passed: bool
    duration_sec: float
    max_recovery_sec: float
    received: int
    accepted: int
    rejected: int
    dropped: int
    event_effects: dict[str, int]
    final_states: dict[str, str]
    transitions: dict[str, list[dict[str, Any]]]
    errors: tuple[str, ...]


def _sample_jpeg() -> bytes:
    image = Image.new("RGB", (96, 64), (72, 96, 118))
    for x in range(image.width):
        for y in range(image.height):
            image.putpixel(
                (x, y),
                ((72 + x) % 255, (96 + y * 2) % 255, (118 + x + y) % 255),
            )
    output = BytesIO()
    image.save(output, format="JPEG", quality=88)
    return output.getvalue()


def _events_for(
    scenario: FaultScenario,
    source_id: str,
    elapsed_sec: float,
):
    aliases = SOURCE_ALIASES[source_id]
    return tuple(
        event
        for event in scenario.events
        if event.source in {*aliases, "*"} and event.active(elapsed_sec)
    )


def _run_scenario_once(scenario: FaultScenario) -> ScenarioResult:
    step_sec = 0.1
    event_end = max(
        (event.start_sec + event.duration_sec for event in scenario.events),
        default=0.0,
    )
    end_sec = event_end + 1.2
    trackers = {
        source_id: SourceTracker(
            source_id,
            "image" if source_id in {"flir", "cam4"} else source_id,
            stale_after_sec=0.35,
            recovery_samples=2 if source_id != "vlm" else 1,
        )
        for source_id in SOURCE_ALIASES
    }
    last_stamp = {source_id: None for source_id in trackers}
    delayed: dict[str, list[tuple[float, float]]] = {
        source_id: [] for source_id in trackers
    }
    transitions: dict[str, list[dict[str, Any]]] = {
        source_id: [] for source_id in trackers
    }
    last_state: dict[str, str] = {}
    event_effects = {event.event_id: 0 for event in scenario.events}
    first_ready_after_event: dict[str, float] = {}
    sample_jpeg = _sample_jpeg()

    step_count = int(round(end_sec / step_sec)) + 1
    for index in range(step_count):
        elapsed = round(index * step_sec, 6)
        for source_id, tracker in trackers.items():
            pending = delayed[source_id]
            ready = [item for item in pending if item[0] <= elapsed + 1e-9]
            delayed[source_id] = [item for item in pending if item[0] > elapsed + 1e-9]
            for _release_at, source_stamp in ready:
                tracker.observe(
                    now_monotonic_sec=elapsed,
                    source_stamp_sec=source_stamp,
                )

            events = _events_for(scenario, source_id, elapsed)
            for event in events:
                event_effects[event.event_id] += 1
            kinds = {event.kind for event in events}
            source_stamp = elapsed + 1.0

            if source_id in {"flir", "cam4"}:
                transform_image_bytes(
                    sample_jpeg,
                    events=events,
                    scenario=scenario,
                    source=source_id,
                    sequence=index + 1,
                )
            elif source_id == "speech":
                transform_speech_text("Adson forceps please", events)

            if source_id == "vlm" and kinds.intersection(
                {"vlm_unhealthy", "vlm_timeout", "vlm_http_500", "vlm_restart"}
            ):
                fault_kind = sorted(
                    kinds.intersection(
                        {"vlm_unhealthy", "vlm_timeout", "vlm_http_500", "vlm_restart"}
                    )
                )[0]
                tracker.set_error(
                    f"fault_injected_{fault_kind}",
                    "deterministic release campaign",
                )
            elif "drop" in kinds:
                tracker.dropped_count += 1
            elif "delay" in kinds:
                delay_sec = max(
                    float(event.params.get("delay_sec", 0.5))
                    for event in events
                    if event.kind == "delay"
                )
                delayed[source_id].append((elapsed + delay_sec, source_stamp))
            else:
                delivered_stamp = source_stamp
                if "freeze" in kinds and last_stamp[source_id] is not None:
                    delivered_stamp = float(last_stamp[source_id])
                elif "reorder" in kinds:
                    delivered_stamp = max(0.001, source_stamp - 0.25)
                tracker.observe(
                    now_monotonic_sec=elapsed,
                    source_stamp_sec=delivered_stamp,
                )
                last_stamp[source_id] = delivered_stamp
                if "duplicate" in kinds:
                    tracker.observe(
                        now_monotonic_sec=elapsed,
                        source_stamp_sec=delivered_stamp,
                    )

            state, _healthy, _age = tracker.snapshot(elapsed)
            if last_state.get(source_id) != state:
                transitions[source_id].append(
                    {"at_sec": elapsed, "state": state}
                )
                last_state[source_id] = state

        for event in scenario.events:
            event_complete = event.start_sec + event.duration_sec
            source_id = next(
                (
                    canonical
                    for canonical, aliases in SOURCE_ALIASES.items()
                    if event.source in {*aliases, "*"}
                ),
                None,
            )
            if source_id is None or event.event_id in first_ready_after_event:
                continue
            if elapsed >= event_complete and last_state.get(source_id) == READY:
                first_ready_after_event[event.event_id] = elapsed

    final_states = {
        source_id: tracker.snapshot(end_sec)[0]
        for source_id, tracker in trackers.items()
    }
    errors: list[str] = []
    for event_id, count in event_effects.items():
        if count <= 0:
            errors.append(f"fault_not_exercised:{event_id}")
    for source_id, state in final_states.items():
        if state != READY:
            errors.append(f"source_not_recovered:{source_id}:{state}")
    recovery_values = []
    for event in scenario.events:
        ready_at = first_ready_after_event.get(event.event_id)
        if ready_at is None:
            errors.append(f"no_ready_recovery:{event.event_id}")
            continue
        recovery_values.append(
            max(0.0, ready_at - event.start_sec - event.duration_sec)
        )

    return ScenarioResult(
        scenario_id=scenario.scenario_id,
        seed=scenario.seed,
        passed=not errors,
        duration_sec=round(end_sec, 3),
        max_recovery_sec=round(max(recovery_values, default=0.0), 3),
        received=sum(tracker.received_count for tracker in trackers.values()),
        accepted=sum(tracker.accepted_count for tracker in trackers.values()),
        rejected=sum(tracker.rejected_count for tracker in trackers.values()),
        dropped=sum(tracker.dropped_count for tracker in trackers.values()),
        event_effects=event_effects,
        final_states=final_states,
        transitions=transitions,
        errors=tuple(errors),
    )


def _action_contract(profile_path: Path) -> dict[str, Any]:
    profile = EmulatorProfile.load(profile_path)
    expected_routes = {
        "tool_handover",
        "retraction_adjustment",
        "tool_change",
    }
    outcomes = {
        route: [item.outcome for item in route_profile.sequence]
        + [route_profile.default.outcome]
        for route, route_profile in profile.routes.items()
    }
    transitions = {
        "tray_to_robot": valid_tool_transition("tray", "robot"),
        "robot_to_surgeon": valid_tool_transition("robot", "surgeon"),
        "mayo_to_tray": valid_tool_transition("mayo", "tray"),
        "surgeon_to_robot": valid_tool_transition("surgeon", "robot"),
    }
    passed = (
        set(profile.routes) == expected_routes
        and transitions["tray_to_robot"]
        and transitions["robot_to_surgeon"]
        and transitions["mayo_to_tray"]
        and not transitions["surgeon_to_robot"]
    )
    return {
        "profile_id": profile.profile_id,
        "passed": passed,
        "public_contract": {
            "tool_handover_action": "/surgery/tool_handover",
            "tool_change_service": "/surgery/tool_change/request",
            "retraction_adjustment_action": "/surgery/retraction/adjust",
            "bed_robot_arm_status": "/external/bed_robot_arms/status",
        },
        "routes": sorted(profile.routes),
        "route_outcomes": outcomes,
        "transition_contract": transitions,
    }


def _write_svg(results: list[ScenarioResult], output_path: Path) -> None:
    width = 960
    height = 78 + len(results) * 54
    max_recovery = max((item.max_recovery_sec for item in results), default=1.0)
    max_recovery = max(max_recovery, 0.1)
    rows = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        '<text x="24" y="31" font-family="sans-serif" font-size="20" font-weight="700">Taskplanner deterministic fault recovery</text>',
        '<text x="24" y="54" font-family="sans-serif" font-size="12" fill="#526170">Max time from fault end to READY</text>',
    ]
    for index, item in enumerate(results):
        y = 83 + index * 54
        bar_width = int(540 * item.max_recovery_sec / max_recovery)
        color = "#12805c" if item.passed else "#c53b45"
        rows.append(
            f'<text x="24" y="{y}" font-family="sans-serif" font-size="14">{item.scenario_id}</text>'
        )
        rows.append(
            f'<rect x="260" y="{y - 17}" width="{max(3, bar_width)}" height="20" rx="3" fill="{color}"/>'
        )
        rows.append(
            f'<text x="820" y="{y}" font-family="monospace" font-size="13">{item.max_recovery_sec:.2f}s</text>'
        )
        rows.append(
            f'<text x="890" y="{y}" font-family="sans-serif" font-size="12" fill="{color}">{"PASS" if item.passed else "FAIL"}</text>'
        )
    rows.append("</svg>")
    output_path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--scenario-dir",
        type=Path,
        default=ROOT / "config" / "fault_scenarios",
    )
    parser.add_argument(
        "--action-profile",
        type=Path,
        default=ROOT
        / "config"
        / "fault_scenarios"
        / "action_mixed_failures.yaml",
    )
    args = parser.parse_args()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    paths = [
        path
        for path in sorted(args.scenario_dir.glob("*.yaml"))
        if path.read_text(encoding="utf-8").startswith(
            "schema: taskplanner.fault_scenario.v1"
        )
    ]
    if not paths:
        raise SystemExit("no fault scenarios found")
    results: list[ScenarioResult] = []
    deterministic = True
    for path in paths:
        scenario = FaultScenario.load(path)
        first = _run_scenario_once(scenario)
        second = _run_scenario_once(scenario)
        if asdict(first) != asdict(second):
            deterministic = False
            first = ScenarioResult(
                **{
                    **asdict(first),
                    "passed": False,
                    "errors": tuple(first.errors)
                    + ("nondeterministic_replay",),
                }
            )
        results.append(first)

    action_contract = _action_contract(args.action_profile)
    passed = (
        deterministic
        and bool(action_contract["passed"])
        and all(result.passed for result in results)
    )
    report = {
        "schema": "taskplanner.release_fault_campaign.v1",
        "passed": passed,
        "deterministic": deterministic,
        "scenario_count": len(results),
        "action_contract": action_contract,
        "scenarios": [asdict(result) for result in results],
    }
    (output_dir / "fault_results.json").write_text(
        json.dumps(report, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )
    with (output_dir / "fault_results.csv").open(
        "w", encoding="utf-8", newline=""
    ) as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=[
                "scenario_id",
                "seed",
                "passed",
                "duration_sec",
                "max_recovery_sec",
                "received",
                "accepted",
                "rejected",
                "dropped",
                "errors",
            ],
        )
        writer.writeheader()
        for result in results:
            row = asdict(result)
            row["errors"] = ";".join(result.errors)
            writer.writerow({key: row[key] for key in writer.fieldnames})
    _write_svg(results, output_dir / "fault_recovery.svg")
    print(json.dumps(report, ensure_ascii=True, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
