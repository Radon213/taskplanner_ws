from __future__ import annotations

import importlib.util
from pathlib import Path

from launch import LaunchContext
from launch.actions import DeclareLaunchArgument, ExecuteProcess, Shutdown
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


def test_debug_launch_exposes_the_single_retraction_service_name() -> None:
    entities = list(_load_launch_description().entities)
    context = LaunchContext()
    retraction_argument = next(
        entity
        for entity in entities
        if isinstance(entity, DeclareLaunchArgument)
        and entity.name == "retraction_service_name"
    )
    retraction_argument.execute(context)

    assert context.launch_configurations["retraction_service_name"] == (
        "/surgery/retraction/command"
    )


def test_debug_retractor_vlm_settings_inherit_shared_vlm_environment(
    monkeypatch,
) -> None:
    inherited = {
        "retraction_voice_vlm_base_url": (
            "RETRACTOR_VOICE_VLM_BASE_URL",
            "VLM_BASE_URL",
            "http://127.0.0.1:8123",
        ),
        "retraction_voice_vlm_model_id": (
            "RETRACTOR_VOICE_VLM_MODEL_ID",
            "VLM_MODEL_ID",
            "local/retractor-test-model",
        ),
        "retraction_voice_vlm_api_key": (
            "RETRACTOR_VOICE_VLM_API_KEY",
            "VLM_API_KEY",
            "test-key",
        ),
    }
    entities = list(_load_launch_description().entities)

    for launch_name, (specific_env, shared_env, expected) in inherited.items():
        monkeypatch.delenv(specific_env, raising=False)
        monkeypatch.setenv(shared_env, expected)
        context = LaunchContext()
        argument = next(
            entity
            for entity in entities
            if isinstance(entity, DeclareLaunchArgument)
            and entity.name == launch_name
        )
        argument.execute(context)
        assert context.launch_configurations[launch_name] == expected
