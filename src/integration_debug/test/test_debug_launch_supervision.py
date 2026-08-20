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


def test_debug_retractor_vlm_identity_does_not_inherit_visual_vlm_environment(
    monkeypatch,
) -> None:
    fixed = {
        "retraction_voice_vlm_base_url": (
            "RETRACTOR_VOICE_VLM_BASE_URL",
            "VLM_BASE_URL",
            "http://127.0.0.1:8080",
        ),
        "retraction_voice_vlm_model_id": (
            "RETRACTOR_VOICE_VLM_MODEL_ID",
            "VLM_MODEL_ID",
            "qwen3.6-35b-a3b",
        ),
    }
    entities = list(_load_launch_description().entities)

    for launch_name, (specific_env, shared_env, expected) in fixed.items():
        monkeypatch.delenv(specific_env, raising=False)
        monkeypatch.setenv(shared_env, "must-not-override-retractor")
        context = LaunchContext()
        argument = next(
            entity
            for entity in entities
            if isinstance(entity, DeclareLaunchArgument)
            and entity.name == launch_name
        )
        argument.execute(context)
        assert context.launch_configurations[launch_name] == expected


def test_debug_retractor_vlm_api_key_may_inherit_shared_secret(monkeypatch) -> None:
    monkeypatch.delenv("RETRACTOR_VOICE_VLM_API_KEY", raising=False)
    monkeypatch.setenv("VLM_API_KEY", "test-key")
    entities = list(_load_launch_description().entities)
    context = LaunchContext()
    argument = next(
        entity
        for entity in entities
        if isinstance(entity, DeclareLaunchArgument)
        and entity.name == "retraction_voice_vlm_api_key"
    )
    argument.execute(context)
    assert context.launch_configurations[argument.name] == "test-key"


def test_debug_launch_uses_admitted_speech_path_and_isolated_virtual_robot() -> None:
    entities = list(_load_launch_description().entities)
    nodes = {
        getattr(entity, "_Node__node_name", None): entity
        for entity in entities
        if isinstance(entity, Node)
    }

    speech = nodes["debug_speech_input_adapter"]
    assert getattr(speech, "_Node__package") == "simulation_runtime"
    assert getattr(speech, "_Node__node_executable") == "speech_input_adapter"
    assert _shutdown_reason(speech) == "debug speech input adapter stopped"

    virtual = nodes["integration_debug_virtual_robot"]
    assert getattr(virtual, "_Node__package") == "surgical_interop_execution"
    assert getattr(virtual, "_Node__node_executable") == "fault_action_emulator"
    assert isinstance(virtual.condition, IfCondition)
    assert _shutdown_reason(virtual) == "integration debug virtual robot stopped"


def test_debug_launch_defaults_to_external_source_with_virtual_available() -> None:
    entities = list(_load_launch_description().entities)
    expected = {
        "robot_endpoint_source": "external",
        "enable_virtual_robot": "true",
        "virtual_retraction_service_name": (
            "/integration/debug/virtual/retraction/command"
        ),
        "virtual_tool_handover_name": (
            "/integration/debug/virtual/tool_handover"
        ),
        "virtual_bed_robot_status_topic": (
            "/integration/debug/virtual/bed_robot_arms/status"
        ),
    }
    context = LaunchContext()
    for name, value in expected.items():
        argument = next(
            entity
            for entity in entities
            if isinstance(entity, DeclareLaunchArgument) and entity.name == name
        )
        argument.execute(context)
        assert context.launch_configurations[name] == value
