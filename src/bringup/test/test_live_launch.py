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


@pytest.fixture(autouse=True)
def _isolate_process_launch_defaults(monkeypatch) -> None:
    """Keep declaration-default tests independent of container mode env."""

    for name in (
        "PUBLISH_SHARED_FREE_TEXT",
        "PUBLISH_FLIR_WHILE_IDLE",
        "RETRACTOR_VOICE_VLM_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


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
        "enable_tts_echo_guard",
        "tts_playback_status_topic",
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
        "flir_overlay_image_topic",
        "rfdetr_flir_output_topic",
        "cam3_tool_observations_topic",
        "cam4_tool_observations_topic",
        "require_rfdetr_applied_field_image",
        "require_rfdetr_cam4_overlay",
        "require_integration_preflight",
        "robot_endpoint_source",
        "enable_runtime_route_control",
        "dispatch_readiness_max_age_sec",
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
            "spec_dir": "/tmp/test-procedure-bundle",
            "publish_shared_free_text": "false",
        }
    )
    gateway_parameters = evaluate_parameters(
        context, gateway._Node__parameters
    )[0]
    assert gateway_parameters["publish_free_text"] is False
    assert gateway_parameters["spec_dir"] == "/tmp/test-procedure-bundle"

    mock_nodes = [
        entity
        for entity in description.entities
        if isinstance(entity, Node)
        and entity.node_executable
        in {"mock_skill_server", "fault_action_emulator"}
    ]
    # The direct bridge keeps the isolated virtual Action/Service emulator
    # resident even when the initial route is external.  It is separately
    # namespaced and never shadows the external controller endpoint.
    assert len(mock_nodes) == 3
    assert all(node.condition is not None for node in mock_nodes)
    assert {
        node._Node__node_name  # noqa: SLF001 - launch action inspection
        for node in mock_nodes
    } == {
        "mock_skill_server",
        "robot_contract_emulator",
        "virtual_robot_contract_emulator",
    }

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


def test_base_launch_aligns_direct_hand_policy_with_bt_thresholds() -> None:
    module = _load_launch_module("taskplanner_mock.launch.py")
    description = module.generate_launch_description()
    twin = next(
        entity
        for entity in description.entities
        if isinstance(entity, Node)
        and entity.node_package == "or_digital_twin"
        and entity.node_executable == "or_digital_twin"
    )
    parameters = {
        _parameter_name(key): value
        for key, value in twin._Node__parameters[0].items()
    }

    assert "vlm_implicit_request_confidence_threshold" not in parameters
    assert "vlm_implicit_request_stability_sec" not in parameters
    assert parameters["hand_handover_dwell_sec"] == 0.300
    assert parameters["hand_handover_minimum_positive_samples"] == 4
    assert parameters["tool_predict_confidence_threshold"] == 0.55
    assert parameters["tool_predict_stability_sec"] == 0.30


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
            "procedure_perception_enabled": "true",
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
    assert [
        "".join(part.text for part in item).splitlines()[0]
        for item in pnu_parameters["requested_algorithms"]
    ] == ["tool", "blood"]
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
            "require_tool_handover_action_server",
            "require_retraction_service",
            "require_bed_robot_arm_status",
        )
    }


def _bed_contract_nodes_enabled(module, bundle_id: str) -> dict[str, bool]:
    description = module.generate_launch_description()
    nodes = {
        entity._Node__node_name: entity  # noqa: SLF001 - launch inspection
        for entity in description.entities
        if isinstance(entity, Node)
        and entity._Node__node_name  # noqa: SLF001 - launch inspection
        in {
            "robot_contract_emulator",
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
    inguinal_demo = _bed_robot_config(module, "inguinal_hernia_repair_demo")

    assert thyroid["bed_robot_contract_enabled"] == "true"
    assert thyroid["bed_robot_contract_procedure_type"] == "thyroidectomy"
    assert thyroid_demo["bed_robot_contract_procedure_type"] == "thyroidectomy"
    assert kidney["bed_robot_contract_procedure_type"] == "nephrectomy"
    assert inguinal["bed_robot_contract_enabled"] == "false"
    assert inguinal["bed_robot_contract_procedure_type"] == ""
    assert inguinal_demo["bed_robot_contract_enabled"] == "true"
    assert inguinal_demo["bed_robot_contract_procedure_type"] == (
        "inguinal_hernia_repair"
    )
    assert inguinal_demo["tool_handover_contract_enabled"] == "false"
    assert inguinal_demo["procedure_image_vlm_enabled"] == "false"
    assert inguinal_demo["procedure_dialogue_vlm_enabled"] == "true"
    assert inguinal_demo["procedure_perception_enabled"] == "false"
    assert inguinal_demo["voice_intent_resolver_enabled"] == "true"
    assert inguinal_demo["procedure_surgeon_actor_enabled"] == "false"
    assert inguinal_demo["procedure_phase_inference_enabled"] == "false"
    assert thyroid["retraction_workflow_state_enforced"] == "true"
    assert thyroid_demo["retraction_workflow_state_enforced"] == "false"
    assert kidney["retraction_workflow_state_enforced"] == "true"
    assert inguinal_demo["retraction_workflow_state_enforced"] == "false"
    assert inguinal_demo["retractor_legacy_raw_voice_enabled"] == "false"


def test_preflight_requirements_follow_external_procedure_contract() -> None:
    module = _load_launch_module("taskplanner_mock.launch.py")

    assert _preflight_requirements(module, "thyroidectomy") == {
        "require_tool_handover_action_server": True,
        "require_retraction_service": True,
        "require_bed_robot_arm_status": False,
    }
    assert _preflight_requirements(module, "thyroidectomy_demo") == {
        "require_tool_handover_action_server": True,
        "require_retraction_service": True,
        "require_bed_robot_arm_status": False,
    }
    assert _preflight_requirements(module, "nephrectomy") == {
        "require_tool_handover_action_server": True,
        "require_retraction_service": True,
        "require_bed_robot_arm_status": False,
    }
    assert _preflight_requirements(module, "inguinal_hernia_repair") == {
        "require_tool_handover_action_server": True,
        "require_retraction_service": False,
        "require_bed_robot_arm_status": False,
    }
    assert _preflight_requirements(module, "inguinal_hernia_repair_demo") == {
        "require_tool_handover_action_server": False,
        "require_retraction_service": True,
        "require_bed_robot_arm_status": False,
    }


def test_controller_contract_is_not_an_admission_gate() -> None:
    module = _load_launch_module("taskplanner_mock.launch.py")
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

    assert parameters["require_controller_contract"] is False

    bridge = next(
        entity
        for entity in description.entities
        if isinstance(entity, Node)
        and entity.node_package == "surgical_interop_execution"
        and entity.node_executable == "surgical_interop_execution_bridge"
    )
    bridge_parameters = {
        _parameter_name(key): value
        for key, value in bridge._Node__parameters[0].items()
    }
    assert "require_controller_contract" not in bridge_parameters
    assert "controller_contract_max_age_sec" not in bridge_parameters

    # Keep the dispatch admission lease armed for both Live route families.
    # The bridge uses it for integration-readiness; controller-contract
    # observations remain non-gating.
    for endpoint_source in ("external", "virtual"):
        context = LaunchContext()
        context.launch_configurations.update(
            {
                "execution_backend": "external",
                "robot_endpoint_source": endpoint_source,
            }
        )
        assert (
            bridge_parameters["require_dispatch_admission_lease"].evaluate(context)
            is True
        )

    # Controller diagnostics and dispatch readiness have independent clocks.
    # A controller-contract TTL must never become a dispatch gate indirectly.
    context = LaunchContext()
    context.launch_configurations.update(
        {
            "controller_contract_max_age_sec": "0.01",
            "dispatch_readiness_max_age_sec": "7.25",
        }
    )
    assert (
        bridge_parameters["admission_lease_max_age_sec"].evaluate(context) == 7.25
    )


def test_preflight_receives_documented_procedure_type() -> None:
    module = _load_launch_module("taskplanner_mock.launch.py")
    for bundle_id, expected in (
        ("thyroidectomy", "thyroidectomy"),
        ("thyroidectomy_demo", "thyroidectomy"),
        ("nephrectomy", "nephrectomy"),
        ("inguinal_hernia_repair", ""),
        ("inguinal_hernia_repair_demo", "inguinal_hernia_repair"),
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
        "robot_contract_emulator": True,
        "bed_robot_arm_group_orchestrator": True,
    }
    assert _bed_contract_nodes_enabled(module, "inguinal_hernia_repair") == {
        "robot_contract_emulator": False,
        "bed_robot_arm_group_orchestrator": False,
    }
    assert _bed_contract_nodes_enabled(
        module, "inguinal_hernia_repair_demo"
    ) == {
        "robot_contract_emulator": True,
        "bed_robot_arm_group_orchestrator": True,
    }


def test_direct_bridge_uses_service_only_retraction_for_all_procedures() -> None:
    module = _load_launch_module("taskplanner_mock.launch.py")
    for bundle_id, expected in (
        ("thyroidectomy_demo", False),
        ("nephrectomy", False),
        ("inguinal_hernia_repair", False),
        ("inguinal_hernia_repair_demo", False),
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


def test_live_virtual_endpoints_are_isolated_and_statusless() -> None:
    module = _load_launch_module("taskplanner_mock.launch.py")
    description = module.generate_launch_description()
    nodes = {
        entity.node_executable: entity
        for entity in description.entities
        if isinstance(entity, Node)
        and entity.node_executable
        in {
            "fault_action_emulator",
            "surgical_interop_execution_bridge",
            "bed_robot_arm_group_orchestrator",
            "integration_preflight",
        }
    }
    context = LaunchContext()
    context.launch_configurations.update(
        {
            "default_bundle": "thyroidectomy_demo",
            "execution_backend": "external",
            "execution_contract": "direct",
            "robot_endpoint_source": "virtual",
        }
    )
    for action in module._bed_robot_contract_configuration(context):
        action.visit(context)

    parameters = {
        name: {
            _parameter_name(key): value
            for key, value in node._Node__parameters[0].items()
        }
        for name, node in nodes.items()
    }
    virtual_emulator_parameters = evaluate_parameters(
        context,
        [
            {
                key: value
                for key, value in nodes["fault_action_emulator"]._Node__parameters[
                    0
                ].items()
                if _parameter_name(key)
                in {
                    "tool_handover_endpoint",
                    "retraction_service_name",
                    "publish_bed_robot_status",
                }
            }
        ],
    )[0]
    bridge_static_route_parameters = evaluate_parameters(
        context,
        [
            {
                key: value
                for key, value in nodes[
                    "surgical_interop_execution_bridge"
                ]._Node__parameters[0].items()
                if _parameter_name(key)
                in {
                    "require_physical_stop_confirmation",
                    "external_require_physical_stop_confirmation",
                }
            }
        ],
    )[0]
    assert nodes["fault_action_emulator"].condition.evaluate(context) is True
    assert nodes["surgical_interop_execution_bridge"].condition.evaluate(context) is True
    assert virtual_emulator_parameters["tool_handover_endpoint"] == (
        "/integration/virtual/surgery/tool_handover"
    )
    assert virtual_emulator_parameters["retraction_service_name"] == (
        "/integration/virtual/surgery/retraction/command"
    )
    assert perform_substitutions(
        context,
        parameters["surgical_interop_execution_bridge"]["tool_handover_endpoint"],
    ) == "/integration/virtual/surgery/tool_handover"
    assert perform_substitutions(
        context,
        parameters["surgical_interop_execution_bridge"]["retraction_service_name"],
    ) == "/integration/virtual/surgery/retraction/command"
    assert (
        virtual_emulator_parameters["publish_bed_robot_status"] is False
    )
    assert (
        parameters["surgical_interop_execution_bridge"]["require_bed_robot_status"].evaluate(
            context
        )
        is False
    )
    # A Live process booted virtual must still restore the external physical
    # stop-confirmation requirement if the operator later selects external.
    assert bridge_static_route_parameters["require_physical_stop_confirmation"] is False
    assert (
        bridge_static_route_parameters[
            "external_require_physical_stop_confirmation"
        ]
        is True
    )
    assert (
        parameters["bed_robot_arm_group_orchestrator"][
            "require_bed_robot_status"
        ].evaluate(context)
        is False
    )
    assert (
        parameters["bed_robot_arm_group_orchestrator"][
            "suppress_retraction_state_machine"
        ].evaluate(context)
        is True
    )
    assert perform_substitutions(
        context,
        parameters["integration_preflight"]["robot_endpoint_source"],
    ) == "virtual"
    assert (
        parameters["integration_preflight"][
            "retraction_state_machine_suppressed"
        ].evaluate(context)
        is True
    )


def test_authored_workflow_suppression_is_independent_of_execution_route() -> None:
    module = _load_launch_module("taskplanner_mock.launch.py")
    for robot_source, retraction_source, expected in (
        ("external", "virtual", True),
        ("virtual", "external", True),
        ("external", "external", True),
    ):
        # ParameterValue caches its first evaluation. A real launch resolves
        # one reviewed route pair, so recreate the description per case.
        description = module.generate_launch_description()
        orchestrator = next(
            entity
            for entity in description.entities
            if isinstance(entity, Node)
            and entity.node_executable == "bed_robot_arm_group_orchestrator"
        )
        parameters = {
            _parameter_name(key): value
            for key, value in orchestrator._Node__parameters[0].items()
        }
        context = LaunchContext()
        context.launch_configurations.update(
            {
                "default_bundle": "thyroidectomy_demo",
                "robot_endpoint_source": robot_source,
                "retraction_endpoint_source": retraction_source,
            }
        )
        for action in module._bed_robot_contract_configuration(context):
            action.visit(context)
        assert (
            parameters["suppress_retraction_state_machine"].evaluate(context)
            is expected
        )


def test_authored_external_workflow_keeps_state_machine_admission() -> None:
    module = _load_launch_module("taskplanner_mock.launch.py")
    description = module.generate_launch_description()
    orchestrator = next(
        entity
        for entity in description.entities
        if isinstance(entity, Node)
        and entity.node_executable == "bed_robot_arm_group_orchestrator"
    )
    parameters = {
        _parameter_name(key): value
        for key, value in orchestrator._Node__parameters[0].items()
    }
    context = LaunchContext()
    context.launch_configurations.update(
        {
            "default_bundle": "nephrectomy",
            "robot_endpoint_source": "external",
            "retraction_endpoint_source": "external",
        }
    )
    for action in module._bed_robot_contract_configuration(context):
        action.visit(context)
    assert (
        parameters["suppress_retraction_state_machine"].evaluate(context) is False
    )


def test_direct_bridge_disables_tool_handover_for_inguinal_demo() -> None:
    module = _load_launch_module("taskplanner_mock.launch.py")
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
    context.launch_configurations["default_bundle"] = (
        "inguinal_hernia_repair_demo"
    )
    for action in module._bed_robot_contract_configuration(context):
        action.visit(context)

    assert parameters["tool_handover_enabled"].evaluate(context) is False


def test_emulator_receives_only_documented_procedure_type() -> None:
    module = _load_launch_module("taskplanner_mock.launch.py")
    for bundle_id, expected in (
        ("thyroidectomy", "thyroidectomy"),
        ("thyroidectomy_demo", "thyroidectomy"),
        ("nephrectomy", "nephrectomy"),
        ("inguinal_hernia_repair", ""),
        ("inguinal_hernia_repair_demo", "inguinal_hernia_repair"),
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


def test_inguinal_demo_starts_typed_dialogue_and_voice_retraction_lanes() -> None:
    module = _load_launch_module("taskplanner_mock.launch.py")
    description = module.generate_launch_description()
    by_executable = {
        entity.node_executable: entity
        for entity in description.entities
        if isinstance(entity, Node)
    }
    context = LaunchContext()
    context.launch_configurations.update(
        {
            "default_bundle": "inguinal_hernia_repair_demo",
            "input_profile": "external",
            "vlm_mode": "dual",
            "surgeon_actor_mode": "llm",
            "validation_mode": "demo",
            "perception_provider": "pnu_hand_blood",
            "enable_rfdetr_perception": "true",
            "enable_no_image_camera": "true",
            "enable_synthetic_scene_camera": "true",
            "field_snapshot_url": "http://127.0.0.1:9000/frame.jpg",
        }
    )
    for action in module._bed_robot_contract_configuration(context):
        action.visit(context)

    assert by_executable["voice_intent_resolver"].condition.evaluate(context) is True
    assert by_executable["real_vlm"].condition.evaluate(context) is True
    for executable in (
        "mock_vlm",
        "synthetic_scene_camera",
        "no_image_camera",
        "snapshot_bridge",
        "rfdetr_perception_bridge",
        "pnu_perception_bridge",
        "surgeon_actor",
        "llm_surgeon_actor",
        "phase_estimator",
    ):
        assert by_executable[executable].condition.evaluate(context) is False

    orchestrator = by_executable["bed_robot_arm_group_orchestrator"]
    parameters = {
        _parameter_name(key): value
        for key, value in orchestrator._Node__parameters[0].items()
    }
    assert parameters["retractor_legacy_raw_voice_enabled"].evaluate(context) is False

    real_vlm = by_executable["real_vlm"]
    vlm_parameters = {
        _parameter_name(key): value
        for key, value in real_vlm._Node__parameters[0].items()
    }
    assert vlm_parameters["enable_text_only_dialogue"].evaluate(context) is True


def test_live_launch_wraps_external_runtime_contract(monkeypatch) -> None:
    # Generic Debug/replay compatibility variables must not reopen the legacy
    # raw-String command path in Live.
    monkeypatch.setenv("SPEECH_INPUT_MODE", "sentence_text")
    monkeypatch.setenv("SENTENCE_INPUT_TOPIC", "/legacy/sentence")
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
    assert (
        arguments["robot_endpoint_source"]._LaunchConfiguration__variable_name[0].text
        == "robot_endpoint_source"
    )
    assert arguments["speech_input_mode"] == "utterance"
    assert arguments["speech_output_mode"] == "typed_utterance"
    assert arguments["speech_typed_output_topic"] == "/surgery/audio/admitted_utterance"
    assert arguments["enable_tts_echo_guard"] == "true"
    assert arguments["tts_playback_status_topic"] == "/tts/playback_status"
    assert arguments["voice_command_input_mode"] == "utterance"
    assert arguments["voice_command_input_topic"] == "/surgery/audio/admitted_utterance"
    assert arguments["voice_command_output_topic"] == "/surgery/voice/proposal"
    assert arguments["vlm_function_gate_enabled"] == "true"
    assert arguments["vlm_function_gate_ledger_path"] == (
        "/taskplanner-tts-state/vlm_function_gate.sqlite3"
    )
    assert arguments["voice_intent_require_source_metadata"] == "true"
    assert arguments["require_asr_runtime_status"] == "true"
    context = LaunchContext()
    assert perform_substitutions(
        context, [arguments["speech_input_topic"]]
    ) == "/sensors/surgeon/utterance"
    assert arguments["retractor_voice_interpreter_mode"].name[0].text == (
        "RETRACTOR_VOICE_INTERPRETER_MODE"
    )
    assert arguments["retractor_voice_interpreter_mode"].default_value[0].text == (
        "vlm_with_fallback"
    )
    assert (
        arguments[
            "retractor_voice_vlm_base_url"
        ]._LaunchConfiguration__variable_name[0].text
        == "vlm_base_url"
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
    assert arguments["enable_rfdetr_perception"] == "false"
    assert arguments["require_field_image"] == "false"
    assert arguments["surgeon_actor_mode"] == "none"
    assert arguments["require_integration_preflight"] == "true"
    assert (
        arguments["vlm_mode"]._LaunchConfiguration__variable_name[0].text
        == "vlm_mode"
    )
    monkeypatch.delenv("REQUIRE_PERCEPTION_ON_START", raising=False)
    context.launch_configurations["default_bundle"] = "thyroidectomy_demo"
    assert perform_substitutions(
        context, [arguments["preflight_require_perception"]]
    ) == "False"
    context.launch_configurations["default_bundle"] = "thyroidectomy"
    assert perform_substitutions(
        context, [arguments["preflight_require_perception"]]
    ) == "False"
    monkeypatch.setenv("REQUIRE_PERCEPTION_ON_START", "true")
    assert perform_substitutions(
        context, [arguments["preflight_require_perception"]]
    ) == "True"
    # The external typed contract stays subscribed across stopped-state bundle
    # switches. The active bundle decides whether both fresh views are needed.
    assert arguments["preflight_require_rfdetr_tool_observations"] == "true"
    assert arguments["preflight_require_metric_3d"] == "false"


def test_live_perception_is_fixed_to_external_typed_dds(monkeypatch) -> None:
    for name in (
        "PERCEPTION_BACKEND",
        "PERCEPTION_PROVIDER",
        "PERCEPTION_LOCATION",
        "PERCEPTION_ENDPOINT",
        "RFDETR_SERVICE_URL",
    ):
        monkeypatch.delenv(name, raising=False)

    module = _load_launch_module("taskplanner_live.launch.py")
    description = module.generate_launch_description()
    declarations = {
        entity.name: entity
        for entity in description.entities
        if isinstance(entity, DeclareLaunchArgument)
    }
    context = LaunchContext()

    expected = {
        "perception_backend": "external",
        "perception_provider": "external_rfdetr_topics",
        "perception_location": "remote",
        "perception_endpoint": "",
        "rfdetr_service_url": "",
    }
    for name, value in expected.items():
        assert perform_substitutions(
            context,
            declarations[name]._DeclareLaunchArgument__default_value,
        ) == value
        context.launch_configurations[name] = value

    for action in module.resolve_launch_perception(context):
        action.visit(context)
    assert context.launch_configurations["perception_provider"] == (
        "external_rfdetr_topics"
    )
    assert context.launch_configurations["perception_endpoint"] == ""

    include = next(
        entity
        for entity in description.entities
        if isinstance(entity, IncludeLaunchDescription)
    )
    arguments = dict(include.launch_arguments)
    assert arguments["enable_rfdetr_perception"] == "false"
    assert arguments["preflight_require_rfdetr_tool_observations"] == "true"


def test_live_vlm_uses_raw_visuals_and_typed_rfdetr_tool_observations(
    monkeypatch,
) -> None:
    # These legacy raster settings may still serve the low-latency operator
    # overlay.  Live VLM tool-location evidence must instead come from the
    # typed CAM3/CAM4 RF-DETR contracts.
    monkeypatch.setenv(
        "SEGMENTED_FLIR_TOPIC",
        "/preview/viplab/flir/segmented/compressed",
    )
    monkeypatch.setenv(
        "CAM4_OVERLAY_TOPIC",
        "/preview/viplab/cam4/overlay/compressed",
    )
    # Deployment-local camera aliases must not leak into the launch-default
    # contract asserted below.
    monkeypatch.delenv("FLIR_INPUT_TOPIC", raising=False)
    monkeypatch.delenv(
        "CAM3_TOOL_OBSERVATIONS_EXPECTED_MODEL_VERSION",
        raising=False,
    )
    monkeypatch.delenv(
        "CAM4_TOOL_OBSERVATIONS_EXPECTED_MODEL_VERSION",
        raising=False,
    )
    module = _load_launch_module("taskplanner_live.launch.py")
    description = module.generate_launch_description()
    include = next(
        entity
        for entity in description.entities
        if isinstance(entity, IncludeLaunchDescription)
    )
    arguments = dict(include.launch_arguments)

    assert perform_substitutions(
        LaunchContext(), [arguments["field_image_topic"]]
    ) == (
        "/synced/flir/color/image_raw/compressed"
    )
    assert arguments["rfdetr_flir_output_topic"] == (
        "/taskplanner/internal/rfdetr/flir/segmented/compressed"
    )
    assert arguments["flir_overlay_image_topic"] == (
        "/taskplanner/internal/rfdetr/flir/segmentation_overlay/compressed"
    )
    assert arguments["cam4_overlay_image_topic"] == (
        "/taskplanner/internal/rfdetr/cam4/detection_overlay/compressed"
    )
    assert arguments["composite_image_topic"] == (
        "/taskplanner/internal/vlm/model_visual/compressed"
    )
    assert arguments["require_rfdetr_applied_field_image"] == "false"
    assert arguments["require_rfdetr_cam4_overlay"] == "false"
    assert perform_substitutions(
        LaunchContext(), [arguments["cam3_tool_observations_topic"]]
    ) == "/perception/cam_3/tool/observations"
    assert perform_substitutions(
        LaunchContext(), [arguments["cam4_tool_observations_topic"]]
    ) == "/perception/cam_4/tool/observations"
    assert perform_substitutions(
        LaunchContext(),
        [arguments["cam3_tool_observations_expected_model_version"]],
    ) == ""
    assert perform_substitutions(
        LaunchContext(),
        [arguments["cam4_tool_observations_expected_model_version"]],
    ) == ""
    assert all(
        "rfdetr" not in str(arguments[name]).casefold()
        for name in (
            "field_image_topic",
            "composite_image_topic",
        )
    )


def test_live_typed_rfdetr_model_pin_is_explicit_opt_in(monkeypatch) -> None:
    monkeypatch.setenv(
        "CAM3_TOOL_OBSERVATIONS_EXPECTED_MODEL_VERSION",
        "incident-pin-cam3",
    )
    monkeypatch.setenv(
        "CAM4_TOOL_OBSERVATIONS_EXPECTED_MODEL_VERSION",
        "incident-pin-cam4",
    )

    module = _load_launch_module("taskplanner_live.launch.py")
    description = module.generate_launch_description()
    include = next(
        entity
        for entity in description.entities
        if isinstance(entity, IncludeLaunchDescription)
    )
    arguments = dict(include.launch_arguments)

    assert perform_substitutions(
        LaunchContext(),
        [arguments["cam3_tool_observations_expected_model_version"]],
    ) == "incident-pin-cam3"
    assert perform_substitutions(
        LaunchContext(),
        [arguments["cam4_tool_observations_expected_model_version"]],
    ) == "incident-pin-cam4"


def test_reviewed_live_demo_boot_contract_is_consistent(monkeypatch) -> None:
    # The declaration default is independent of a developer's currently
    # exported runtime override (the integration restart uses one explicitly).
    monkeypatch.delenv("TASKPLANNER_LIVE_DEFAULT_BUNDLE", raising=False)
    root = Path(__file__).resolve().parents[3]
    integration_env = (root / "config" / "integration.env.example").read_text(
        encoding="utf-8"
    )
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    module = _load_launch_module("taskplanner_live.launch.py")
    description = module.generate_launch_description()
    declared = {
        entity.name: entity
        for entity in description.entities
        if isinstance(entity, DeclareLaunchArgument)
    }
    context = LaunchContext()

    assert "TASKPLANNER_LIVE_DEFAULT_BUNDLE=thyroidectomy_demo" in integration_env
    assert "VLM_MODE=real" in integration_env
    assert (
        "default_bundle:=$${TASKPLANNER_LIVE_DEFAULT_BUNDLE:-thyroidectomy_demo}"
        in compose
    )
    assert perform_substitutions(
        context,
        declared["default_bundle"]._DeclareLaunchArgument__default_value,
    ) == "thyroidectomy_demo"


def test_live_model_contract_ignores_stale_provider_overrides(monkeypatch) -> None:
    monkeypatch.setenv("VLM_MODE", "fake")
    monkeypatch.setenv("VLM_BASE_URL", "http://127.0.0.1:8001")
    monkeypatch.setenv("VLM_PROVIDER_ID", "vllm")
    monkeypatch.setenv("VLM_MODEL_ID", "unsloth/legacy-model")
    monkeypatch.setenv(
        "RETRACTOR_VOICE_VLM_BASE_URL",
        "http://127.0.0.1:8001",
    )
    monkeypatch.setenv("RETRACTOR_VOICE_VLM_MODEL_ID", "legacy-retractor")
    monkeypatch.setenv(
        "VOICE_COMMAND_SELECTOR_ENDPOINT",
        "http://127.0.0.1:8001/v1/chat/completions",
    )
    monkeypatch.setenv("VOICE_COMMAND_SELECTOR_MODEL", "legacy-selector")
    monkeypatch.setenv("VLM_API_KEY", "test-key")

    module = _load_launch_module("taskplanner_live.launch.py")
    description = module.generate_launch_description()
    declared = {
        entity.name: entity
        for entity in description.entities
        if isinstance(entity, DeclareLaunchArgument)
    }
    include = next(
        entity
        for entity in description.entities
        if isinstance(entity, IncludeLaunchDescription)
    )
    arguments = dict(include.launch_arguments)
    context = LaunchContext()
    expected = {
        "vlm_mode": "real",
        "vlm_base_url": "http://127.0.0.1:8080",
        "vlm_provider_id": "ninfer",
        "vlm_model_id": "qwen3.6-35b-a3b",
    }
    for name, value in expected.items():
        declaration = declared[name]
        assert perform_substitutions(
            context,
            declaration._DeclareLaunchArgument__default_value,
        ) == value
        assert declaration._DeclareLaunchArgument__choices == (value,)
    context.launch_configurations.update(expected)

    fixed_arguments = {
        "vlm_mode": "real",
        "vlm_base_url": "http://127.0.0.1:8080",
        "vlm_provider_id": "ninfer",
        "vlm_model_id": "qwen3.6-35b-a3b",
        "vlm_api_mode": "openai_compat",
        "retractor_voice_vlm_base_url": "http://127.0.0.1:8080",
        "retractor_voice_vlm_model_id": "qwen3.6-35b-a3b",
        "voice_command_selector_endpoint": (
            "http://127.0.0.1:8080/v1/chat/completions"
        ),
        "voice_command_selector_model": "qwen3.6-35b-a3b",
    }
    for name, value in fixed_arguments.items():
        argument = arguments[name]
        if isinstance(argument, str):
            assert argument == value
        else:
            assert perform_substitutions(context, [argument]) == value
    assert perform_substitutions(
        context,
        [arguments["retractor_voice_vlm_api_key"]],
    ) == "test-key"


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
    context.launch_configurations["publish_camera_aliases"] = "true"
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
    context.launch_configurations["controller_contract_max_age_sec"] = "0.01"
    context.launch_configurations["dispatch_readiness_max_age_sec"] = "7.25"
    assert included_arguments["controller_contract_max_age_sec"].perform(context) == "0.01"
    assert included_arguments["dispatch_readiness_max_age_sec"].perform(context) == "7.25"
    context.launch_configurations["publish_flir_while_idle"] = "false"
    assert nodes["camera_alias_relay"].condition.evaluate(context) is True

    # The relay stays available across an idle bundle switch.  Its own
    # stopped+validated WorldState gate prevents native camera acquisition
    # until an eligible procedure is actually running.
    context.launch_configurations["default_bundle"] = "inguinal_hernia_repair_demo"
    assert nodes["camera_alias_relay"].condition.evaluate(context) is True
    context.launch_configurations["default_bundle"] = "thyroidectomy_demo"

    alias_parameters = evaluate_parameters(
        context, nodes["camera_alias_relay"]._Node__parameters
    )[0]
    assert alias_parameters["flir_public_topic"] == "/surgery/images/flir/compressed"
    assert alias_parameters["cam4_public_topic"] == "/surgery/images/cam4/compressed"
    assert alias_parameters["default_bundle"] == "thyroidectomy_demo"
    assert alias_parameters["publish_flir_while_idle"] is False
