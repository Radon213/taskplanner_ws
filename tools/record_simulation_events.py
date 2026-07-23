#!/usr/bin/env python3
"""Record /simulation/event as flush-safe JSON Lines."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

import rclpy
from rclpy.node import Node
from rosidl_runtime_py.convert import message_to_ordereddict
from surgical_msgs.msg import SimulationEvent


class EventRecorder(Node):
    def __init__(self, output: Path, duration_sec: float) -> None:
        super().__init__("simulation_event_file_recorder")
        output.parent.mkdir(parents=True, exist_ok=True)
        self.output = output
        self.file = output.open("w", encoding="utf-8")
        self.count = 0
        self.deadline = time.monotonic() + duration_sec if duration_sec > 0 else None
        self.create_subscription(
            SimulationEvent, "/simulation/event", self.on_event, 100
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

    def expired(self) -> bool:
        return self.deadline is not None and time.monotonic() >= self.deadline

    def close(self) -> None:
        self.file.flush()
        self.file.close()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--duration-sec", type=float, default=600.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    rclpy.init()
    recorder = EventRecorder(args.output, float(args.duration_sec))
    print(f"[RECORDING] {args.output}", flush=True)
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
    print(f"[RECORDED] events={count} output={args.output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
