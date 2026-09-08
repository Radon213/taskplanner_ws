"""Atomic YAML tuning client for the running tool-belief tracker."""

from __future__ import annotations

import argparse
import math
from pathlib import Path
import sys
from typing import Callable, Mapping

import rclpy
from rclpy.node import Node
from rclpy.parameter import Parameter
from rclpy.parameter_client import AsyncParameterClient
import yaml


def load_ros_parameter_mapping(path: str | Path, target_node: str) -> dict[str, object]:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError("tuning YAML must contain a mapping")
    names = (
        target_node,
        target_node.lstrip("/"),
        target_node.rstrip("/").rsplit("/", 1)[-1],
    )
    section = next((payload[name] for name in names if name in payload), None)
    if not isinstance(section, dict):
        raise ValueError(f"tuning YAML has no section for {target_node}")
    parameters = section.get("ros__parameters")
    if not isinstance(parameters, dict) or not parameters:
        raise ValueError("tuning YAML ros__parameters must be a non-empty mapping")
    return {str(name): value for name, value in parameters.items()}


def request_atomic_update(
    client,
    parameters: Mapping[str, object],
    wait_for_response: Callable[[object], object],
):
    request = [Parameter(name=name, value=value) for name, value in parameters.items()]
    future = client.set_parameters_atomically(request)
    response = wait_for_response(future)
    result = getattr(response, "result", None)
    if result is None:
        raise RuntimeError("atomic parameter service returned no result")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Atomically apply a tool_belief_tracker tuning YAML",
    )
    parser.add_argument("--file", required=True, help="ROS parameter YAML path")
    parser.add_argument("--node", default="/tool_belief_tracker", help="target node")
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()

    node = None
    initialized = False
    try:
        timeout_sec = float(args.timeout)
        if not math.isfinite(timeout_sec) or timeout_sec <= 0.0:
            raise ValueError("--timeout must be finite and positive")
        parameters = load_ros_parameter_mapping(args.file, args.node)
        rclpy.init()
        initialized = True
        node = Node("tool_belief_tune")
        client = AsyncParameterClient(node, args.node)
        if not client.wait_for_services(timeout_sec=timeout_sec):
            raise RuntimeError(f"parameter service unavailable for {args.node}")

        def wait_for_response(future):
            rclpy.spin_until_future_complete(
                node,
                future,
                timeout_sec=timeout_sec,
            )
            if not future.done():
                raise TimeoutError("atomic parameter update timed out")
            return future.result()

        result = request_atomic_update(client, parameters, wait_for_response)
        if not bool(result.successful):
            raise RuntimeError(result.reason or "atomic parameter update rejected")
        print(f"Applied {len(parameters)} parameters atomically to {args.node}")
        return 0
    except Exception as exc:
        print(f"Atomic tuning failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if node is not None:
            node.destroy_node()
        if initialized and rclpy.ok():
            rclpy.shutdown()
