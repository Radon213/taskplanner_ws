from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from launch import LaunchContext
from launch.actions import DeclareLaunchArgument, ExecuteProcess, Shutdown
from launch.conditions import IfCondition
from launch_ros.actions import Node
from launch_ros.utilities import evaluate_parameters


def _load_launch_module():
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
    return module


def _load_launch_description():
    return _load_launch_module().generate_launch_description()


_PNU_LAUNCH_ARGUMENTS = {
    "enable_pnu_perception",
    "perception_provider",
    "perception_location",
    "perception_endpoint",
    "pnu_service_url",
    "pnu_api_token_file",
    "pnu_allow_insecure_remote_http",
    "pnu_allow_unauthenticated_remote",
    "pnu_requested_algorithms",
    "pnu_expected_model_digests_json",
    "pnu_expected_tool_support_plane_config_version",
    "pnu_depth_scale_m_per_unit",
    "pnu_depth_scale_validated",
    "pnu_depth_alignment_validated",
    "pnu_depth_alignment_id",
    "pnu_rgb_input_topic",
    "pnu_color_camera_info_topic",
    "pnu_depth_input_topic",
    "pnu_depth_camera_info_topic",
    "pnu_overlay_topic",
    "pnu_pose_overlay_topic",
    "pnu_max_rate_hz",
}


def _context_with_pnu_defaults(entities) -> LaunchContext:
    context = LaunchContext()
    for entity in entities:
        if (
            isinstance(entity, DeclareLaunchArgument)
            and entity.name in _PNU_LAUNCH_ARGUMENTS
        ):
            entity.execute(context)
    return context


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


def test_debug_pnu_bridge_is_disabled_unless_the_profile_explicitly_enables_it(
    monkeypatch,
) -> None:
    monkeypatch.delenv("ENABLE_PNU_DEBUG_PERCEPTION", raising=False)
    module = _load_launch_module()
    description = module.generate_launch_description()
    context = _context_with_pnu_defaults(description.entities)

    assert module._launch_debug_pnu_bridge(context) == []


def test_debug_pnu_bridge_uses_live_cam4_rgbd_and_all_pinned_models(
    monkeypatch,
) -> None:
    configured = {
        "ENABLE_PNU_DEBUG_PERCEPTION": "true",
        "PERCEPTION_PROVIDER": "pnu_hand_blood",
        "PERCEPTION_LOCATION": "local",
        "PERCEPTION_ENDPOINT": "http://127.0.0.1:8020",
        "PNU_DEBUG_REQUESTED_ALGORITHMS": "tool,blood,hand",
        "PNU_EXPECTED_MODEL_DIGESTS_JSON": (
            '{"tool":"253617aa5337fec219d694ca50537e4867fb8c403ce60f3a6945bbe15fecf430",'
            '"blood":"f4967b2b8c7ab63921f8aa9b2ea0a4e3324243a9b98253da3ea4b9ecd6df6f75",'
            '"hand":"fbc2a30080c3c557093b5ddfc334698132eb341044ccee322ccf8bcf3607cde1"}'
        ),
        "PNU_DEPTH_SCALE_M_PER_UNIT": "0.001",
        "PNU_DEPTH_SCALE_VALIDATED": "true",
        "PNU_DEPTH_ALIGNMENT_VALIDATED": "true",
        "PNU_DEPTH_ALIGNMENT_ID": "viplab-cam4-rgbd-align-test",
        "PNU_EXPECTED_TOOL_SUPPORT_PLANE_CONFIG_VERSION": (
            "viplab_cam4_146222251000_support_plane_v1_sha256_test"
        ),
    }
    for name, value in configured.items():
        monkeypatch.setenv(name, value)
    module = _load_launch_module()
    description = module.generate_launch_description()
    context = _context_with_pnu_defaults(description.entities)

    nodes = module._launch_debug_pnu_bridge(context)
    assert len(nodes) == 1
    node = nodes[0]
    assert getattr(node, "_Node__package") == "vlm_node"
    assert getattr(node, "_Node__node_executable") == "pnu_perception_bridge"
    assert getattr(node, "_Node__node_name") == "debug_pnu_perception_bridge"
    assert _shutdown_reason(node) == "debug PNU perception bridge stopped"
    parameters = evaluate_parameters(context, node._Node__parameters)[0]
    assert parameters["service_url"] == "http://127.0.0.1:8020"
    assert parameters["requested_algorithms"] == ("tool", "blood", "hand")
    assert parameters["rgb_input_topic"] == (
        "/synced/cam_4/color/image_raw/compressed"
    )
    assert parameters["color_camera_info_topic"] == (
        "/synced/cam_4/color/camera_info"
    )
    assert parameters["depth_input_topic"] == (
        "/synced/cam_4/aligned_depth_to_color/image_raw/compressedDepth"
    )
    assert parameters["depth_camera_info_topic"] == (
        "/synced/cam_4/aligned_depth_to_color/camera_info"
    )
    assert parameters["cam4_overlay_topic"] == (
        "/surgery/images/cam4/detection_overlay/compressed"
    )
    assert parameters["cam4_pose_overlay_topic"] == (
        "/surgery/images/cam4/pose_overlay/compressed"
    )
    assert parameters["expected_model_digests_json"] == (
        '{"tool":"253617aa5337fec219d694ca50537e4867fb8c403ce60f3a6945bbe15fecf430",'
        '"blood":"f4967b2b8c7ab63921f8aa9b2ea0a4e3324243a9b98253da3ea4b9ecd6df6f75",'
        '"hand":"fbc2a30080c3c557093b5ddfc334698132eb341044ccee322ccf8bcf3607cde1"}'
    )
    assert parameters["depth_scale_m_per_unit"] == 0.001
    assert parameters["depth_scale_validated"] is True
    assert parameters["depth_alignment_validated"] is True
    assert parameters["expected_tool_support_plane_config_version"] == (
        "viplab_cam4_146222251000_support_plane_v1_sha256_test"
    )


def test_debug_pnu_remote_placement_uses_endpoint_and_token_without_local_fallback(
    monkeypatch,
) -> None:
    configured = {
        "ENABLE_PNU_DEBUG_PERCEPTION": "true",
        "PERCEPTION_PROVIDER": "pnu_hand_blood",
        "PERCEPTION_LOCATION": "remote",
        "PERCEPTION_ENDPOINT": "https://192.168.1.20:8020",
        "PNU_CLIENT_API_TOKEN_FILE": "/run/taskplanner/perception/token",
        "PNU_DEBUG_REQUESTED_ALGORITHMS": "tool,blood,hand",
    }
    for name, value in configured.items():
        monkeypatch.setenv(name, value)
    module = _load_launch_module()
    description = module.generate_launch_description()
    context = _context_with_pnu_defaults(description.entities)

    node = module._launch_debug_pnu_bridge(context)[0]
    parameters = evaluate_parameters(context, node._Node__parameters)[0]
    assert parameters["service_url"] == "https://192.168.1.20:8020"
    assert parameters["api_token_file"] == "/run/taskplanner/perception/token"
    assert parameters["allow_insecure_remote_http"] is False
    assert parameters["allow_unauthenticated_remote"] is False


def test_debug_pnu_rejects_remote_loopback_and_invalid_requested_subset(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ENABLE_PNU_DEBUG_PERCEPTION", "true")
    monkeypatch.setenv("PERCEPTION_PROVIDER", "pnu_hand_blood")
    monkeypatch.setenv("PERCEPTION_LOCATION", "remote")
    monkeypatch.setenv("PERCEPTION_ENDPOINT", "http://127.0.0.1:8020")
    module = _load_launch_module()
    description = module.generate_launch_description()
    context = _context_with_pnu_defaults(description.entities)
    with pytest.raises(RuntimeError, match="non-loopback"):
        module._launch_debug_pnu_bridge(context)

    context.launch_configurations["perception_location"] = "local"
    context.launch_configurations["pnu_requested_algorithms"] = "blood,blood"
    with pytest.raises(RuntimeError, match="unique, nonempty CSV subset"):
        module._launch_debug_pnu_bridge(context)
