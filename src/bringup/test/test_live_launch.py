import importlib.util
from pathlib import Path

from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch_ros.actions import Node


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
        in {"mock_skill_server", "mock_bed_robot_arm_group_server"}
    ]
    assert len(mock_nodes) == 2
    assert all(node.condition is not None for node in mock_nodes)


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
