"""Focused contracts for direct, independently restartable owner launches."""

from __future__ import annotations

from pathlib import Path
import sys

_SRC_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_SRC_ROOT / "procedure_spec"))
sys.path.insert(0, str(_SRC_ROOT / "simulation_runtime"))
sys.path.insert(0, str(_SRC_ROOT / "bringup"))

from launch import LaunchContext
from launch.actions import DeclareLaunchArgument, ExecuteProcess, IncludeLaunchDescription
from launch.substitutions import LaunchConfiguration
from launch.utilities import perform_substitutions
from launch_ros.actions import Node

from bringup.runtime_owner_launch import (
    OWNER_LAUNCH_BUILDERS,
    OWNER_NODE_IDENTITIES,
    _cam4_mayo_actions,
    _cam4_mayo_profile_actions,
    _command_actions,
    _execution_actions,
    _state_core_actions,
    generate_owner_launch_description,
    node_identity,
)
from bringup.runtime_profile import resolve_runtime_profile


BRINGUP_ROOT = Path(__file__).resolve().parents[1]


def _nodes(actions: list[object]) -> dict[str, Node]:
    return {
        action._Node__node_name: action  # noqa: SLF001 - launch contract inspection
        for action in actions
        if type(action) is Node
    }


def _parameter_names(node: Node) -> set[str]:
    parameter_sets = node._Node__parameters  # noqa: SLF001 - launch contract inspection
    assert len(parameter_sets) == 1
    parameters = parameter_sets[0]
    assert isinstance(parameters, dict)
    context = LaunchContext()
    return {perform_substitutions(context, key) for key in parameters}


def _parameters_by_name(node: Node) -> dict[str, object]:
    parameter_sets = node._Node__parameters  # noqa: SLF001 - launch contract inspection
    assert len(parameter_sets) == 1
    parameters = parameter_sets[0]
    assert isinstance(parameters, dict)
    context = LaunchContext()
    return {perform_substitutions(context, key): value for key, value in parameters.items()}


def _launch_configuration_name(value: object) -> str:
    """Return the declared name behind a launch-parameter substitution."""

    substitution = value[0] if isinstance(value, tuple) else value
    assert isinstance(substitution, LaunchConfiguration)
    variable = substitution._LaunchConfiguration__variable_name  # noqa: SLF001
    return perform_substitutions(LaunchContext(), variable)


def test_every_owner_launch_is_direct_and_has_one_owner_local_node_set() -> None:
    for owner, expected in OWNER_NODE_IDENTITIES.items():
        description = generate_owner_launch_description(owner)
        assert not any(
            isinstance(action, IncludeLaunchDescription)
            for action in description.entities
        ), owner
        actual = {
            node_identity(action)
            for action in description.entities
            if type(action) is Node
        }
        assert actual == expected, owner


def test_command_owner_has_one_admitted_ingress_and_private_resolver_lane() -> None:
    nodes = _nodes(_command_actions())
    resolver = nodes["voice_command_resolver"]
    router = nodes["command_router"]

    assert _parameter_names(resolver) >= {
        "input_mode",
        "input_topic",
        "output_topic",
        "procedure_bundle",
    }
    assert _parameter_names(router) >= {
        "input_topic",
        "resolver_input_topic",
        "resolver_output_topic",
        "catalog_path",
    }
    resolver_parameters = _parameters_by_name(resolver)
    router_parameters = _parameters_by_name(router)
    assert _launch_configuration_name(resolver_parameters["input_topic"]) == "resolver_input_topic"
    assert _launch_configuration_name(resolver_parameters["output_topic"]) == "resolver_output_topic"
    assert _launch_configuration_name(router_parameters["input_topic"]) == "speech_typed_output_topic"
    assert _launch_configuration_name(router_parameters["resolver_input_topic"]) == "resolver_input_topic"
    assert _launch_configuration_name(router_parameters["resolver_output_topic"]) == "resolver_output_topic"


def test_state_core_does_not_consume_private_resolver_proposals() -> None:
    nodes = _nodes(_state_core_actions())
    manager = nodes["simulation_manager"]
    parameters = _parameters_by_name(manager)
    assert "voice_intent_topic" in parameters
    assert _launch_configuration_name(parameters["voice_intent_topic"]) == "legacy_voice_intent_topic"


def test_cam4_mayo_owner_contains_only_the_typed_projection_adapter() -> None:
    nodes = _nodes(_cam4_mayo_actions())
    assert set(nodes) == {"cam4_typed_mayo_adapter"}
    parameters = _parameters_by_name(nodes["cam4_typed_mayo_adapter"])
    assert set(parameters) == {
        "input_topic",
        "output_topic",
        "scenario_config_topic",
        "spec_dir",
    }
    assert _launch_configuration_name(parameters["input_topic"]) == (
        "cam4_tool_observations_topic"
    )
    assert _launch_configuration_name(parameters["output_topic"]) == (
        "mayo_tool_observations_topic"
    )
    assert _launch_configuration_name(parameters["scenario_config_topic"]) == (
        "scenario_config_topic"
    )
    adapter_setup = (
        _SRC_ROOT / "vlm_node" / "setup.py"
    ).read_text(encoding="utf-8")
    assert "cam4_typed_mayo_adapter = vlm_node.cam4_typed_mayo_adapter:main" in (
        adapter_setup
    )


def test_cam4_mayo_live_profile_uses_live_bundle_before_scenario_config_arrives() -> None:
    context = LaunchContext()
    context.launch_configurations["runtime_profile"] = "live"
    for action in _cam4_mayo_profile_actions(context):
        action.execute(context)

    assert LaunchConfiguration("default_bundle").perform(context) == (
        "thyroidectomy_demo"
    )
    assert LaunchConfiguration("spec_dir").perform(context).endswith(
        "/procedure_spec/share/procedure_spec/specs/thyroidectomy_demo"
    )


def test_execution_owner_keeps_the_typed_proxy_and_persisted_route_selection() -> None:
    nodes = _nodes(_execution_actions())
    proxy = nodes["execution_command_proxy"]
    bridge = nodes["surgical_interop_execution_bridge"]
    assert _parameter_names(proxy) == {
        "route_state_topic",
        "retraction_proxy_service",
        "tool_handover_proxy_action",
        "service_receipt_timeout_sec",
    }
    bridge_parameters = _parameters_by_name(bridge)
    assert "route_selection_state_path" in bridge_parameters
    assert "route_selection_runtime_mode" in bridge_parameters


def test_debug_observer_owns_only_observer_and_private_rosbridge() -> None:
    description = generate_owner_launch_description("debug-observer")
    nodes = [action for action in description.entities if type(action) is Node]
    processes = [
        action for action in description.entities if type(action) is ExecuteProcess
    ]
    assert [node_identity(node) for node in nodes] == [
        ("integration_debug", "integration_debug_observer", "integration_debug_observer")
    ]
    assert len(processes) == 1
    declarations = {
        action.name
        for action in description.entities
        if isinstance(action, DeclareLaunchArgument)
    }
    assert {
        "enable_rosbridge",
        "rosbridge_port",
        "rosbridge_address",
        "rosbridge_executable",
    } <= declarations
    assert "virtual_robot_enabled" not in declarations


def test_surgery_record_owner_contains_only_the_read_only_submitter() -> None:
    description = generate_owner_launch_description("surgery-record")
    assert not any(type(action) is ExecuteProcess for action in description.entities)
    nodes = [action for action in description.entities if type(action) is Node]
    assert [node_identity(node) for node in nodes] == [
        (
            "integration_debug",
            "operational_surgery_record",
            "operational_surgery_record",
        )
    ]


def test_rosbag_recorder_is_manual_and_has_no_scenario_lifecycle_wiring() -> None:
    description = generate_owner_launch_description("rosbag-recorder")
    assert not any(type(action) is ExecuteProcess for action in description.entities)
    nodes = [action for action in description.entities if type(action) is Node]
    assert [node_identity(node) for node in nodes] == [
        (
            "integration_debug",
            "operational_rosbag_recorder",
            "operational_rosbag_recorder",
        )
    ]
    parameters = _parameters_by_name(nodes[0])
    assert set(parameters) == {
        "output_dir",
        "min_free_bytes",
        "max_bag_bytes",
        "max_cache_bytes",
    }
    assert "scenario" not in repr(description.entities).lower()
    assert "lifecycle" not in repr(description.entities).lower()


def test_debug_control_has_no_browser_bridge_or_virtual_default() -> None:
    description = generate_owner_launch_description("debug-control")
    assert not any(type(action) is ExecuteProcess for action in description.entities)
    declarations = {
        action.name: action
        for action in description.entities
        if isinstance(action, DeclareLaunchArgument)
    }
    assert "virtual_robot_enabled" in declarations
    assert perform_substitutions(
        LaunchContext(),
        declarations["virtual_robot_enabled"]._DeclareLaunchArgument__default_value,  # noqa: SLF001
    ) == "false"


def test_debug_virtual_owns_only_the_virtual_endpoint_emulator() -> None:
    description = generate_owner_launch_description("debug-virtual")
    assert not any(type(action) is ExecuteProcess for action in description.entities)
    nodes = [action for action in description.entities if type(action) is Node]
    assert [node_identity(node) for node in nodes] == [
        (
            "surgical_interop_execution",
            "fault_action_emulator",
            "integration_debug_virtual_robot",
        )
    ]
    declarations = {
        action.name
        for action in description.entities
        if isinstance(action, DeclareLaunchArgument)
    }
    assert {
        "profile_path",
        "virtual_retraction_service_name",
        "virtual_tool_handover_name",
        "virtual_bed_robot_status_topic",
    } <= declarations


def test_profile_is_capability_based_not_selected_bundle_topology() -> None:
    source = (BRINGUP_ROOT / "bringup" / "runtime_owner_launch.py").read_text(
        encoding="utf-8"
    )


def test_direct_owner_wiring_is_local_and_dispatcher_stays_thin() -> None:
    """Keep direct Node declarations out of the shared owner dispatcher."""

    owner_modules = {
        "scenario": "scenario",
        "operator-bridge": "operator_bridge",
        "state-core": "state_core",
        "command": "command",
        "tool-state": "tool_state",
        "perception": "perception",
        "cam4-mayo": "cam4_mayo",
        "projection": "projection",
        "execution": "execution",
        "simulation-input": "simulation_input",
    }
    dispatcher_source = (
        BRINGUP_ROOT / "bringup" / "runtime_owner_launch.py"
    ).read_text(encoding="utf-8")

    assert set(OWNER_LAUNCH_BUILDERS) == set(OWNER_NODE_IDENTITIES)
    assert "Node(" not in dispatcher_source
    assert "DeclareLaunchArgument(" not in dispatcher_source
    for owner, module_name in owner_modules.items():
        source = (
            BRINGUP_ROOT / "bringup" / "runtime_owners" / f"{module_name}.py"
        ).read_text(encoding="utf-8")
        assert "def _generate_" in source, owner
        assert "Node(" in source, owner
    assert "scenario_topology" not in source
    assert "taskplanner_mock.launch" not in source
    profile = resolve_runtime_profile("live")
    assert profile.arguments_for("command")["input_profile"] == "external"
    assert profile.arguments_for("execution")["execution_backend"] == "external"
    assert (
        resolve_runtime_profile("llm-surgeon").arguments_for("command")[
            "command_router_enabled"
        ]
        == "true"
    )


def test_installed_owner_entrypoints_are_present_without_composite_entries() -> None:
    setup_source = (BRINGUP_ROOT / "setup.py").read_text(encoding="utf-8")
    for launch_file in (
        "taskplanner_state_core.launch.py",
        "taskplanner_command.launch.py",
        "taskplanner_execution.launch.py",
        "taskplanner_cam4_mayo.launch.py",
        "taskplanner_surgery_record.launch.py",
        "taskplanner_rosbag_recorder.launch.py",
        "taskplanner_debug_observer.launch.py",
        "taskplanner_debug_control.launch.py",
        "taskplanner_debug_virtual.launch.py",
    ):
        assert launch_file in setup_source
    assert "launch/taskplanner_mock.launch.py" not in setup_source
    assert "launch/taskplanner_live.launch.py" not in setup_source
