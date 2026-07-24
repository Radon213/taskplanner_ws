#!/usr/bin/env python3
"""Record /simulation/event as flush-safe JSON Lines."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import rclpy
from rclpy.node import Node
from rosidl_runtime_py.convert import message_to_ordereddict
from surgical_msgs.msg import SimulationEvent, SurgeonLLMDecision, VLMHealth


class EventRecorder(Node):
    def __init__(
        self,
        output: Path,
        metadata_output: Path,
        duration_sec: float,
        metadata: dict[str, object],
    ) -> None:
        super().__init__("simulation_event_file_recorder")
        output.parent.mkdir(parents=True, exist_ok=True)
        self.output = output
        self.metadata_output = metadata_output
        self.metadata = metadata
        self.file = output.open("w", encoding="utf-8")
        self.count = 0
        self.started_monotonic = time.monotonic()
        self.deadline = time.monotonic() + duration_sec if duration_sec > 0 else None
        self.create_subscription(
            SimulationEvent, "/simulation/event", self.on_event, 100
        )
        self.create_subscription(VLMHealth, "/vlm/health", self.on_vlm_health, 20)
        self.create_subscription(
            SurgeonLLMDecision,
            "/surgeon/llm_decision",
            self.on_surgeon_decision,
            20,
        )

    def on_event(self, msg: SimulationEvent) -> None:
        payload = dict(message_to_ordereddict(msg))
        self.file.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.file.flush()
        self.count += 1
        print(
            f"[EVENT {self.count}] {msg.event_type} "
            f"tool={msg.instrument_id or '-'} status={msg.status or '-'}",
            flush=True,
        )

    def on_vlm_health(self, msg: VLMHealth) -> None:
        self.metadata["observed_vlm"] = {
            "model_id": msg.model_id,
            "image_source": msg.image_source,
            "last_mode": msg.last_mode,
            "connected": bool(msg.connected),
            "healthy": bool(msg.healthy),
        }

    def on_surgeon_decision(self, msg: SurgeonLLMDecision) -> None:
        self.metadata["observed_surgeon_actor"] = {
            "model_id": msg.model_id,
            "accepted": bool(msg.accepted),
        }

    def expired(self) -> bool:
        return self.deadline is not None and time.monotonic() >= self.deadline

    def close(self) -> None:
        self.file.flush()
        self.file.close()
        self.metadata["event_count"] = self.count
        self.metadata["elapsed_sec"] = round(
            time.monotonic() - self.started_monotonic,
            3,
        )
        self.metadata["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        self.metadata_output.parent.mkdir(parents=True, exist_ok=True)
        self.metadata_output.write_text(
            json.dumps(self.metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--duration-sec", type=float, default=600.0)
    parser.add_argument("--metadata-output", type=Path)
    return parser.parse_args(argv)


def git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def runtime_metadata(output: Path, duration_sec: float) -> dict[str, object]:
    environment_keys = (
        "ROS_DOMAIN_ID",
        "VLM_MODE",
        "VLM_MODEL_ID",
        "VLM_PUBLISH_PERIOD_SEC",
        "VLM_IMAGE_STALE_SEC",
        "SURGEON_ACTOR_MODE",
        "ACTOR_MODEL_ID",
        "FIELD_SNAPSHOT_URL",
    )
    return {
        "schema": "taskplanner.event_recording.v1",
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "git_commit": git_commit(),
        "event_output": str(output),
        "requested_duration_sec": duration_sec,
        "environment": {
            key: os.environ[key]
            for key in environment_keys
            if key in os.environ
        },
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    output = args.output.expanduser().resolve()
    metadata_output = (
        args.metadata_output.expanduser().resolve()
        if args.metadata_output is not None
        else output.with_name(output.name + ".metadata.json")
    )
    duration_sec = float(args.duration_sec)
    rclpy.init()
    recorder = EventRecorder(
        output,
        metadata_output,
        duration_sec,
        runtime_metadata(output, duration_sec),
    )
    print(f"[RECORDING] {output}", flush=True)
    try:
        while rclpy.ok() and not recorder.expired():
            rclpy.spin_once(recorder, timeout_sec=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        count = recorder.count
        recorder.close()
        recorder.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    print(
        f"[RECORDED] events={count} output={output} metadata={metadata_output}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
