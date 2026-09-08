from __future__ import annotations

import importlib.util
from pathlib import Path

from launch.actions import DeclareLaunchArgument, ExecuteProcess
from launch_ros.actions import Node


def _description(name: str):
    path = Path(__file__).resolve().parents[1] / "launch" / name
    spec = importlib.util.spec_from_file_location(name.replace(".", "_"), path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.generate_launch_description()


def _node_executables(description) -> list[str]:
    return [
        str(getattr(entity, "_Node__node_executable", ""))
        for entity in description.entities
        if isinstance(entity, Node)
    ]


def test_observer_launch_contains_only_observer_and_secured_bridge() -> None:
    description = _description("debug_observer.launch.py")
    processes = [
        entity
        for entity in description.entities
        if isinstance(entity, ExecuteProcess) and not isinstance(entity, Node)
    ]
    argument_names = {
        entity.name
        for entity in description.entities
        if isinstance(entity, DeclareLaunchArgument)
    }

    assert _node_executables(description) == ["integration_debug_observer"]
    assert len(processes) == 1
    assert {
        "rosbridge_port",
        "rosbridge_address",
        "rosbridge_service_timeout",
        "rosbridge_executable",
        "run_root",
    }.issubset(argument_names)


def test_control_launch_contains_no_rosbridge_or_optional_sidecar() -> None:
    description = _description("debug_control.launch.py")

    assert _node_executables(description) == ["integration_debug_control"]
    assert not [
        entity
        for entity in description.entities
        if isinstance(entity, ExecuteProcess) and not isinstance(entity, Node)
    ]


def test_control_launch_requires_explicit_virtual_endpoint_enablement() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "launch" / "debug_control.launch.py"
    ).read_text(encoding="utf-8")

    assert 'DeclareLaunchArgument("virtual_robot_enabled", default_value="false")' in source


def test_virtual_launch_contains_only_fault_action_emulator() -> None:
    description = _description("debug_virtual.launch.py")

    assert _node_executables(description) == ["fault_action_emulator"]
    assert not [
        entity
        for entity in description.entities
        if isinstance(entity, ExecuteProcess) and not isinstance(entity, Node)
    ]
