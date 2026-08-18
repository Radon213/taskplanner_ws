from __future__ import annotations

import importlib.util
from pathlib import Path

from launch.actions import ExecuteProcess, Shutdown
from launch.conditions import IfCondition
from launch_ros.actions import Node


def _load_launch_description():
    launch_path = (
        Path(__file__).resolve().parents[1]
        / "launch"
        / "taskplanner_debug.launch.py"
    )
    spec = importlib.util.spec_from_file_location(
        "taskplanner_debug_launch_under_test", launch_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.generate_launch_description()


def _shutdown_reason(action: ExecuteProcess | Node) -> str | None:
    on_exit = getattr(action, "_ExecuteLocal__on_exit", None)
    if not isinstance(on_exit, Shutdown):
        return None
    event = getattr(on_exit, "_EmitEvent__event")
    return str(getattr(event, "_Shutdown__reason"))


def test_critical_debug_children_shutdown_the_launch() -> None:
    entities = list(_load_launch_description().entities)

    bridges = [entity for entity in entities if type(entity) is ExecuteProcess]
    assert len(bridges) == 1
    bridge = bridges[0]
    assert isinstance(bridge.condition, IfCondition)
    assert _shutdown_reason(bridge) == "integration debug rosbridge stopped"

    gateways = [
        entity
        for entity in entities
        if isinstance(entity, Node)
        and getattr(entity, "_Node__node_name", None)
        == "integration_debug_gateway"
    ]
    assert len(gateways) == 1
    assert _shutdown_reason(gateways[0]) == "integration debug gateway stopped"
