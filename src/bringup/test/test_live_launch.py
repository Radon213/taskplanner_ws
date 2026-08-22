import importlib.util
from pathlib import Path
import sys

_SRC_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_SRC_ROOT / "simulation_runtime"))
sys.path.insert(0, str(_SRC_ROOT / "bringup"))

from launch import LaunchContext
from launch.actions import (
    DeclareLaunchArgument,
    ExecuteProcess,
    IncludeLaunchDescription,
    SetLaunchConfiguration,
)
from launch_ros.actions import Node
from launch_ros.utilities import evaluate_parameters
from launch.utilities import perform_substitutions
import pytest


def _load_launch_module(filename: str):
    launch_path = Path(__file__).resolve().parents[1] / "launch" / filename
    spec = importlib.util.spec_from_file_location(filename.replace(".", "_"), launch_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_base_launch_conditions_mock_execution_servers() -> None:
    module = _load_launch_module("taskplanner_mock.launch.py")
    description = module.generate_launch_description()
    arguments = {
        action.name
        for action in description.entities
        if isinstance(action, DeclareLaunchArgument)
    }
    assert {
        "execution_backend",
        "default_bundle",
        "publish_shared_state",
        "publish_shared_free_text",
        "speech_input_mode",
        "sentence_input_topic",
        "retractor_voice_normalization_enabled",
        "retractor_voice_interpreter_mode",
        "retractor_voice_vlm_base_url",
        "retractor_voice_vlm_model_id",
        "enable_rfdetr_perception",
        "perception_backend",
        "perception_provider",
        "perception_location",
        "perception_endpoint",
        "pnu_allow_insecure_remote_http",
        "pnu_depth_scale_m_per_unit",
        "pnu_depth_scale_validated",
        "pnu_expected_tool_support_plane_config_version",
        "cv_contract_status_topic",
        "cv_cam4_rgb_topic",
        "cv_handover_tray_rgb_topic",
        "require_integration_preflight",
    }.issubset(arguments)

    gateway = next(
        entity
        for entity in description.entities
        if isinstance(entity, Node)
        and entity.node_executable == "surgical_interop_gateway"
    )
    assert gateway.condition is not None
    context = LaunchContext()
    declaration = next(
        entity
        for entity in description.entities
        if isinstance(entity, DeclareLaunchArgument)
        and entity.name == "publish_shared_state"
    )
    assert perform_substitutions(
        context, declaration._DeclareLaunchArgument__default_value
    ) == "true"

    free_text_declaration = next(
        entity
        for entity in description.entities
        if isinstance(entity, DeclareLaunchArgument)
        and entity.name == "publish_shared_free_text"
    )
    assert perform_substitutions(
        context, free_text_declaration._DeclareLaunchArgument__default_value
    ) == "false"
    context.launch_configurations.update(
        {
            "default_bundle": "thyroidectomy",
            "publish_shared_free_text": "false",
        }
    )
    gateway_parameters = evaluate_parameters(
        context, gateway._Node__parameters
    )[0]
    assert gateway_parameters["publish_free_text"] is False

    mock_nodes = [
        entity
        for entity in description.entities
        if isinstance(entity, Node)
        and entity.node_executable
        in {"mock_skill_server", "fault_action_emulator"}
    ]
    assert len(mock_nodes) == 2
    assert all(node.condition is not None for node in mock_nodes)

    removed_nodes = {
        entity.node_executable
        for entity in description.entities
        if isinstance(entity, Node)
        and entity.node_executable
        in {
            "mock_bed_robot_arm_group_server",
            "bed_robot_arm_group_action_bridge",
        }
    }
    assert not removed_nodes

    rosapi_nodes = [
        entity
        for entity in description.entities
        if isinstance(entity, Node)
        and entity.node_package == "rosapi"
        and entity.node_executable == "rosapi_node"
    ]
    assert len(rosapi_nodes) == 1
    assert rosapi_nodes[0].condition is not None


def test_voice_intent_resolver_is_bound_to_the_active_procedure_bundle() -> None:
    module = _load_launch_module("taskplanner_mock.launch.py")
    description = module.generate_launch_description()
    resolver = next(
        entity
        for entity in description.entities
        if isinstance(entity, Node)
        and entity.node_package == "voice_command"
        and entity.node_executable == "voice_intent_resolver"
    )
    parameters = {
        _parameter_name(key): value
        for key, value in resolver._Node__parameters[0].items()
    }

    assert {
        "input_topic",
        "output_topic",
        "procedure_bundle",
        "selector_mode",
        "selector_endpoint",
        "selector_model",
        "selector_timeout_sec",
    }.issubset(parameters)
    context = LaunchContext()
    context.launch_configurations.update(
        {
            "spec_dir": "/tmp/test-procedure-bundle",
            "voice_command_selector_mode": "deterministic",
        }
    )
    assert perform_substitutions(context, parameters["procedure_bundle"]) == (
        "/tmp/test-procedure-bundle"
    )
    assert perform_substitutions(context, parameters["selector_mode"]) == (
        "deterministic"
    )


def test_perception_provider_keeps_one_local_ros_adapter() -> None:
    module = _load_launch_module("taskplanner_mock.launch.py")
    description = module.generate_launch_description()
    rfdetr = next(
        entity
        for entity in description.entities
        if isinstance(entity, Node)
        and entity.node_executable == "rfdetr_perception_bridge"
    )
    pnu = next(
        entity
        for entity in description.entities
        if isinstance(entity, Node)
        and entity.node_executable == "pnu_perception_bridge"
    )
    monitor = next(
        entity
        for entity in description.entities
        if isinstance(entity, Node)
        and entity.node_executable == "cv_contract_monitor"
    )
    parameters = {
        _parameter_name(key): value
        for key, value in monitor._Node__parameters[0].items()
    }
    assert {
        "perception_backend",
        "perception_provider",
        "perception_location",
        "perception_endpoint",
        "status_topic",
        "cam4_rgb_topic",
        "cam4_camera_info_topic",
        "cam4_native_depth_compressed_topic",
        "cam4_depth_camera_info_topic",
        "cam4_depth_to_color_extrinsics_topic",
        "cam4_aligned_depth_compressed_topic",
        "cam4_aligned_depth_camera_info_topic",
        "handover_tray_rgb_topic",
    }.issubset(parameters)

    context = LaunchContext()
    context.launch_configurations.update(
        {
            "perception_provider": "builtin_rfdetr",
            "perception_location": "local",
            "enable_rfdetr_perception": "true",
        }
    )
    assert rfdetr.condition.evaluate(context) is True
    assert pnu.condition.evaluate(context) is False
    context.launch_configurations["perception_location"] = "remote"
    assert rfdetr.condition.evaluate(context) is True
    context.launch_configurations["perception_provider"] = "pnu_hand_blood"
    assert rfdetr.condition.evaluate(context) is False
    assert pnu.condition.evaluate(context) is True
    pnu_parameters = {
        _parameter_name(key): value
        for key, value in pnu._Node__parameters[0].items()
    }
    assert {
        "service_url",
        "rgb_input_topic",
        "color_camera_info_topic",
        "depth_input_topic",
        "depth_camera_info_topic",
        "cam4_semantics_topic",
        "cam4_mayo_observation_topic",
        "diagnostics_topic",
        "health_topic",
        "requested_algorithms",
        "expected_model_digests_json",
        "expected_tool_support_plane_config_version",
        "api_token_file",
        "allow_insecure_remote_http",
        "allow_unauthenticated_remote",
        "depth_scale_m_per_unit",
        "depth_scale_validated",
    }.issubset(pnu_parameters)
    context.launch_configurations["perception_provider"] = "disabled"
    assert rfdetr.condition.evaluate(context) is False
    assert pnu.condition.evaluate(context) is False


def test_perception_launch_aliases_and_remote_endpoint_are_resolved() -> None:
    module = _load_launch_module("taskplanner_mock.launch.py")
    context = LaunchContext()
    context.launch_configurations.update(
        {
            "perception_provider": "",
            "perception_location": "",
            "perception_endpoint": "",
            "perception_backend": "local",
            "rfdetr_service_url": "http://127.0.0.1:8010",
        }
    )
    for action in module.resolve_launch_perception(context):
        action.visit(context)
    assert context.launch_configurations["perception_provider"] == "builtin_rfdetr"
    assert context.launch_configurations["perception_location"] == "local"
    assert context.launch_configurations["perception_endpoint"] == (
        "http://127.0.0.1:8010"
    )

    context = LaunchContext()
    context.launch_configurations.update(
        {
            "perception_provider": "builtin_rfdetr",
            "perception_location": "remote",
            "perception_endpoint": "http://192.168.1.20:8010",
            "perception_backend": "local",
            "rfdetr_service_url": "http://127.0.0.1:8010",
        }
    )
    for action in module.resolve_launch_perception(context):
        action.visit(context)
    assert context.launch_configurations["perception_location"] == "remote"
    assert context.launch_configurations["perception_endpoint"] == (
        "http://192.168.1.20:8010"
    )


def test_pnu_provider_resolves_its_versioned_worker_without_rfdetr_bridge() -> None:
    module = _load_launch_module("taskplanner_mock.launch.py")
    context = LaunchContext()
    context.launch_configurations.update(
        {
            "perception_provider": "pnu_hand_blood",
            "perception_location": "remote",
            "perception_endpoint": "https://192.168.1.20:8020",
            "perception_backend": "local",
            "rfdetr_service_url": "http://127.0.0.1:8010",
            "pnu_service_url": "",
        }
    )
    for action in module.resolve_launch_perception(context):
        action.visit(context)
    assert context.launch_configurations["perception_provider"] == "pnu_hand_blood"
    assert context.launch_configurations["perception_location"] == "remote"
    assert context.launch_configurations["perception_endpoint"] == (
        "https://192.168.1.20:8020"
    )


def test_local_pnu_provider_has_a_distinct_loopback_default() -> None:
    module = _load_launch_module("taskplanner_mock.launch.py")
    context = LaunchContext()
    context.launch_configurations.update(
        {
            "perception_provider": "pnu_hand_blood",
            "perception_location": "local",
            "perception_endpoint": "",
            "perception_backend": "local",
            "rfdetr_service_url": "http://127.0.0.1:8010",
            "pnu_service_url": "",
        }
    )
    for action in module.resolve_launch_perception(context):
        action.visit(context)
    assert context.launch_configurations["perception_endpoint"] == (
        "http://127.0.0.1:8020"
    )


def test_rosbridge_process_restarts_after_failure() -> None:
    module = _load_launch_module("taskplanner_mock.launch.py")
    description = module.generate_launch_description()
    processes = [
        entity
        for entity in description.entities
        if isinstance(entity, ExecuteProcess) and not isinstance(entity, Node)
    ]

    assert len(processes) == 1
    rosbridge_process = processes[0]
    assert rosbridge_process._ExecuteLocal__respawn is True
    assert rosbridge_process._ExecuteLocal__respawn_delay == 5.0


def _bed_robot_config(module, bundle_id: str) -> dict[str, str]:
    context = LaunchContext()
    context.launch_configurations["default_bundle"] = bundle_id
    actions = module._bed_robot_contract_configuration(context)
    assert all(isinstance(action, SetLaunchConfiguration) for action in actions)
    result: dict[str, str] = {}
    for action in actions:
        action.visit(context)
    result.update(context.launch_configurations)
    return result


def _parameter_name(key) -> str:
    return "".join(part.text for part in key)


def _preflight_requirements(module, bundle_id: str) -> dict[str, bool]:
    description = module.generate_launch_description()
    preflight = next(
        entity
        for entity in description.entities
        if isinstance(entity, Node)
        and entity.node_package == "simulation_runtime"
        and entity.node_executable == "integration_preflight"
    )
    parameters = {
        _parameter_name(key): value
        for key, value in preflight._Node__parameters[0].items()
    }
    context = LaunchContext()
    context.launch_configurations.update(
        {
            "default_bundle": bundle_id,
            "preflight_require_perception": "false",
        }
    )
    for action in module._bed_robot_contract_configuration(context):
        action.visit(context)
    return {
        name: bool(parameters[name].evaluate(context))
        for name in (
            "require_retraction_service",
            "require_bed_robot_arm_status",
        )
    }


def _bed_contract_nodes_enabled(module, bundle_id: str) -> dict[str, bool]:
    description = module.generate_launch_description()
    nodes = {
        entity.node_executable: entity
        for entity in description.entities
        if isinstance(entity, Node)
        and entity.node_executable
        in {
            "fault_action_emulator",
            "bed_robot_arm_group_orchestrator",
        }
    }
    context = LaunchContext()
    context.launch_configurations.update(
        {
            "default_bundle": bundle_id,
            "execution_backend": "mock",
            "execution_contract": "direct",
        }
    )
    for action in module._bed_robot_contract_configuration(context):
        action.visit(context)
    return {
        name: bool(node.condition.evaluate(context))
        for name, node in nodes.items()
    }


def test_bed_robot_contract_bundle_mapping_is_explicit() -> None:
    module = _load_launch_module("taskplanner_mock.launch.py")

    thyroid = _bed_robot_config(module, "thyroidectomy")
    thyroid_demo = _bed_robot_config(module, "thyroidectomy_demo")
    kidney = _bed_robot_config(module, "nephrectomy")
    inguinal = _bed_robot_config(module, "inguinal_hernia_repair")

    assert thyroid["bed_robot_contract_enabled"] == "true"
    assert thyroid["bed_robot_contract_procedure_type"] == "thyroidectomy"
    assert thyroid_demo["bed_robot_contract_procedure_type"] == "thyroidectomy"
    assert kidney["bed_robot_contract_procedure_type"] == "nephrectomy"
    assert inguinal["bed_robot_contract_enabled"] == "false"
    assert inguinal["bed_robot_contract_procedure_type"] == ""


def test_preflight_requirements_follow_external_procedure_contract() -> None:
    module = _load_launch_module("taskplanner_mock.launch.py")

    assert _preflight_requirements(module, "thyroidectomy") == {
        "require_retraction_service": True,
        "require_bed_robot_arm_status": True,
    }
    assert _preflight_requirements(module, "thyroidectomy_demo") == {
        "require_retraction_service": True,
        "require_bed_robot_arm_status": True,
    }
    assert _preflight_requirements(module, "nephrectomy") == {
        "require_retraction_service": True,
        "require_bed_robot_arm_status": True,
    }
    assert _preflight_requirements(module, "inguinal_hernia_repair") == {
        "require_retraction_service": False,
        "require_bed_robot_arm_status": False,
    }


def test_preflight_receives_documented_procedure_type() -> None:
    module = _load_launch_module("taskplanner_mock.launch.py")
    for bundle_id, expected in (
        ("thyroidectomy", "thyroidectomy"),
        ("thyroidectomy_demo", "thyroidectomy"),
        ("nephrectomy", "nephrectomy"),
        ("inguinal_hernia_repair", ""),
    ):
        description = module.generate_launch_description()
        preflight = next(
            entity
            for entity in description.entities
            if isinstance(entity, Node)
            and entity.node_package == "simulation_runtime"
            and entity.node_executable == "integration_preflight"
        )
        parameters = {
            _parameter_name(key): value
            for key, value in preflight._Node__parameters[0].items()
        }
        context = LaunchContext()
        context.launch_configurations["default_bundle"] = bundle_id
        for action in module._bed_robot_contract_configuration(context):
            action.visit(context)
        assert perform_substitutions(context, parameters["active_bundle"]) == bundle_id
        assert parameters["procedure_type"].evaluate(context) == expected


def test_non_retraction_bundle_starts_no_bed_contract_publishers() -> None:
    module = _load_launch_module("taskplanner_mock.launch.py")

    assert _bed_contract_nodes_enabled(module, "thyroidectomy_demo") == {
        "fault_action_emulator": True,
        "bed_robot_arm_group_orchestrator": True,
    }
    assert _bed_contract_nodes_enabled(module, "inguinal_hernia_repair") == {
        "fault_action_emulator": False,
        "bed_robot_arm_group_orchestrator": False,
    }


def test_direct_bridge_requires_bed_status_only_for_configured_procedures() -> None:
    module = _load_launch_module("taskplanner_mock.launch.py")
    for bundle_id, expected in (
        ("thyroidectomy_demo", True),
        ("nephrectomy", True),
        ("inguinal_hernia_repair", False),
    ):
        # ParameterValue caches its first evaluation, while each real launch
        # resolves exactly one bundle. Recreate the description per case.
        description = module.generate_launch_description()
        bridge = next(
            entity
            for entity in description.entities
            if isinstance(entity, Node)
            and entity.node_executable == "surgical_interop_execution_bridge"
        )
        parameters = {
            _parameter_name(key): value
            for key, value in bridge._Node__parameters[0].items()
        }
        context = LaunchContext()
        context.launch_configurations["default_bundle"] = bundle_id
        for action in module._bed_robot_contract_configuration(context):
            action.visit(context)
        assert bool(parameters["require_bed_robot_status"].evaluate(context)) is expected


def test_emulator_receives_only_documented_procedure_type() -> None:
    module = _load_launch_module("taskplanner_mock.launch.py")
    for bundle_id, expected in (
        ("thyroidectomy", "thyroidectomy"),
        ("thyroidectomy_demo", "thyroidectomy"),
        ("nephrectomy", "nephrectomy"),
        ("inguinal_hernia_repair", ""),
    ):
        description = module.generate_launch_description()
        emulator = next(
            entity
            for entity in description.entities
            if isinstance(entity, Node)
            and entity.node_executable == "fault_action_emulator"
        )
        parameters = {
            _parameter_name(key): value
            for key, value in emulator._Node__parameters[0].items()
        }
        context = LaunchContext()
        context.launch_configurations["default_bundle"] = bundle_id
        for action in module._bed_robot_contract_configuration(context):
            action.visit(context)
        assert parameters["procedure_type"].evaluate(context) == expected


def test_live_launch_wraps_external_runtime_contract() -> None:
    module = _load_launch_module("taskplanner_live.launch.py")
    description = module.generate_launch_description()
    includes = [
        entity
        for entity in description.entities
        if isinstance(entity, IncludeLaunchDescription)
    ]
    assert len(includes) == 1
    arguments = dict(includes[0].launch_arguments)
    assert arguments["input_profile"] == "external"
    assert (
        arguments["default_bundle"]._LaunchConfiguration__variable_name[0].text
        == "default_bundle"
    )
    assert arguments["execution_backend"] == "external"
    assert arguments["speech_input_mode"] == "sentence_text"
    assert arguments["retractor_voice_interpreter_mode"].name[0].text == (
        "RETRACTOR_VOICE_INTERPRETER_MODE"
    )
    assert arguments["retractor_voice_interpreter_mode"].default_value[0].text == (
        "vlm_with_fallback"
    )
    assert arguments["retractor_voice_vlm_base_url"].name[0].text == (
        "RETRACTOR_VOICE_VLM_BASE_URL"
    )
    assert (
        arguments["perception_backend"]._LaunchConfiguration__variable_name[0].text
        == "perception_backend"
    )
    assert (
        arguments["perception_provider"]._LaunchConfiguration__variable_name[0].text
        == "perception_provider"
    )
    assert (
        arguments["perception_location"]._LaunchConfiguration__variable_name[0].text
        == "perception_location"
    )
    assert (
        arguments["perception_endpoint"]._LaunchConfiguration__variable_name[0].text
        == "perception_endpoint"
    )
    assert (
        arguments["rfdetr_service_url"]._LaunchConfiguration__variable_name[0].text
        == "perception_endpoint"
    )
    assert (
        arguments[
            "pnu_allow_insecure_remote_http"
        ]._LaunchConfiguration__variable_name[0].text
        == "pnu_allow_insecure_remote_http"
    )
    assert arguments["surgeon_actor_mode"] == "none"
    assert arguments["require_integration_preflight"] == "true"
    assert arguments["vlm_mode"].name[0].text == "VLM_MODE"
    assert arguments["vlm_mode"].default_value[0].text == "real"
    assert arguments["preflight_require_perception"].name[0].text == (
        "REQUIRE_PERCEPTION_ON_START"
    )
    assert (
        arguments["preflight_require_perception"].default_value[0].text
        == "false"
    )
    assert arguments["preflight_require_metric_3d"].name[0].text == (
        "PNU_REQUIRE_METRIC_3D_ON_START"
    )
    assert (
        arguments["preflight_require_metric_3d"].default_value[0].text
        == "false"
    )


def test_live_retractor_text_vlm_inherits_the_loaded_vlm_endpoint(
    monkeypatch,
) -> None:
    monkeypatch.delenv("RETRACTOR_VOICE_VLM_BASE_URL", raising=False)
    monkeypatch.delenv("RETRACTOR_VOICE_VLM_MODEL_ID", raising=False)
    monkeypatch.delenv("RETRACTOR_VOICE_VLM_API_KEY", raising=False)
    monkeypatch.setenv("VLM_BASE_URL", "http://127.0.0.1:8080")
    monkeypatch.setenv("VLM_MODEL_ID", "qwen3.6-35b-a3b")
    monkeypatch.setenv("VLM_API_KEY", "test-key")

    module = _load_launch_module("taskplanner_live.launch.py")
    description = module.generate_launch_description()
    include = next(
        entity
        for entity in description.entities
        if isinstance(entity, IncludeLaunchDescription)
    )
    arguments = dict(include.launch_arguments)
    context = LaunchContext()

    assert perform_substitutions(
        context, [arguments["retractor_voice_vlm_base_url"]]
    ) == "http://127.0.0.1:8080"
    assert perform_substitutions(
        context, [arguments["retractor_voice_vlm_model_id"]]
    ) == "qwen3.6-35b-a3b"
    assert perform_substitutions(
        context, [arguments["retractor_voice_vlm_api_key"]]
    ) == "test-key"


def test_live_voice_selector_inherits_the_loaded_vlm_endpoint(monkeypatch) -> None:
    monkeypatch.delenv("VOICE_COMMAND_SELECTOR_ENDPOINT", raising=False)
    monkeypatch.delenv("VOICE_COMMAND_SELECTOR_MODEL", raising=False)
    monkeypatch.setenv("VLM_BASE_URL", "http://127.0.0.1:8080")
    monkeypatch.setenv("VLM_MODEL_ID", "qwen3.6-35b-a3b")

    module = _load_launch_module("taskplanner_live.launch.py")
    description = module.generate_launch_description()
    include = next(
        entity
        for entity in description.entities
        if isinstance(entity, IncludeLaunchDescription)
    )
    arguments = dict(include.launch_arguments)
    context = LaunchContext()

    assert perform_substitutions(
        context, [arguments["voice_command_selector_endpoint"]]
    ) == "http://127.0.0.1:8080/v1/chat/completions"
    assert perform_substitutions(
        context, [arguments["voice_command_selector_model"]]
    ) == "qwen3.6-35b-a3b"


def test_live_public_contract_is_enabled_and_loop_safe_by_default() -> None:
    module = _load_launch_module("taskplanner_live.launch.py")
    description = module.generate_launch_description()
    declared = {
        entity.name: entity
        for entity in description.entities
        if isinstance(entity, DeclareLaunchArgument)
    }
    context = LaunchContext()
    for name in ("publish_shared_state", "publish_camera_aliases"):
        default_value = declared[name]._DeclareLaunchArgument__default_value
        assert perform_substitutions(context, default_value) == "true"
    free_text_default = declared[
        "publish_shared_free_text"
    ]._DeclareLaunchArgument__default_value
    assert perform_substitutions(context, free_text_default) == "false"
    idle_flir_default = declared[
        "publish_flir_while_idle"
    ]._DeclareLaunchArgument__default_value
    assert perform_substitutions(context, idle_flir_default) == "false"

    nodes = {
        entity.node_executable: entity
        for entity in description.entities
        if isinstance(entity, Node)
        and entity.node_executable
        in {"surgical_interop_gateway", "camera_alias_relay"}
    }
    # Gateway ownership lives in the included base runtime, avoiding duplicate
    # publishers across simulation and live profiles.
    assert set(nodes) == {"camera_alias_relay"}
    assert all(node.condition is not None for node in nodes.values())

    context.launch_configurations["default_bundle"] = "thyroidectomy_demo"
    include = next(
        entity
        for entity in description.entities
        if isinstance(entity, IncludeLaunchDescription)
    )
    included_arguments = dict(include._IncludeLaunchDescription__launch_arguments)
    assert (
        included_arguments["default_bundle"].perform(context)
        == "thyroidectomy_demo"
    )
    context.launch_configurations["publish_shared_state"] = "false"
    assert included_arguments["publish_shared_state"].perform(context) == "false"
    context.launch_configurations["publish_shared_free_text"] = "true"
    assert included_arguments["publish_shared_free_text"].perform(context) == "true"
    context.launch_configurations["publish_flir_while_idle"] = "false"

    alias_parameters = evaluate_parameters(
        context, nodes["camera_alias_relay"]._Node__parameters
    )[0]
    assert alias_parameters["flir_public_topic"] == "/surgery/images/flir/compressed"
    assert alias_parameters["cam4_public_topic"] == "/surgery/images/cam4/compressed"
    assert alias_parameters["default_bundle"] == "thyroidectomy_demo"
    assert alias_parameters["publish_flir_while_idle"] is False
