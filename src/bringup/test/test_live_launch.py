import importlib.util
from pathlib import Path

from launch import LaunchContext
from launch.actions import (
    DeclareLaunchArgument,
    IncludeLaunchDescription,
    SetLaunchConfiguration,
)
from launch_ros.actions import Node
from launch.utilities import perform_substitutions


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
        "speech_input_mode",
        "sentence_input_topic",
        "enable_rfdetr_perception",
        "require_integration_preflight",
    }.issubset(arguments)

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
            "require_tool_change_service",
            "require_retraction_adjustment_server",
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
        "require_tool_change_service": True,
        "require_retraction_adjustment_server": False,
        "require_bed_robot_arm_status": True,
    }
    assert _preflight_requirements(module, "thyroidectomy_demo") == {
        "require_tool_change_service": True,
        "require_retraction_adjustment_server": False,
        "require_bed_robot_arm_status": True,
    }
    assert _preflight_requirements(module, "nephrectomy") == {
        "require_tool_change_service": False,
        "require_retraction_adjustment_server": True,
        "require_bed_robot_arm_status": True,
    }
    assert _preflight_requirements(module, "inguinal_hernia_repair") == {
        "require_tool_change_service": False,
        "require_retraction_adjustment_server": False,
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
