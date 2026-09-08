"""Read-only Debug observer and separately admitted Debug-control owners."""

from __future__ import annotations

from typing import Final

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, ExecuteProcess, Shutdown
from launch.conditions import IfCondition
from launch.substitutions import EnvironmentVariable, LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue
from launch_ros.substitutions import FindPackageShare


NodeIdentity = tuple[str, str, str]

DEBUG_OWNER_NODE_IDENTITIES: Final[dict[str, frozenset[NodeIdentity]]] = {
    "debug-observer": frozenset(
        {
            (
                "integration_debug",
                "integration_debug_observer",
                "integration_debug_observer",
            )
        }
    ),
    "debug-control": frozenset(
        {
            (
                "integration_debug",
                "integration_debug_control",
                "integration_debug_control",
            )
        }
    ),
    "debug-virtual": frozenset(
        {
            (
                "surgical_interop_execution",
                "fault_action_emulator",
                "integration_debug_virtual_robot",
            )
        }
    ),
}


def _config_default() -> PathJoinSubstitution:
    return PathJoinSubstitution(
        [FindPackageShare("integration_debug"), "config", "integration_debug.yaml"]
    )


def generate_debug_observer_launch_description() -> LaunchDescription:
    """Launch read-only Debug observation and its private secured ROSBridge.

    The bridge belongs to this owner because it exposes only observer topics;
    control is a separate owner and never piggybacks on the browser bridge.
    This retains the historical two-process observer boundary (node + bridge)
    without reviving the all-in-one `integration-debug` service.
    """

    enable_rosbridge = LaunchConfiguration("enable_rosbridge")
    rosbridge_port = LaunchConfiguration("rosbridge_port")
    rosbridge_address = LaunchConfiguration("rosbridge_address")
    rosbridge_timeout = LaunchConfiguration("rosbridge_service_timeout")
    rosbridge_executable = LaunchConfiguration("rosbridge_executable")
    config_path = LaunchConfiguration("config_path")
    run_root = LaunchConfiguration("run_root")
    robot_endpoint_source = LaunchConfiguration("robot_endpoint_source")
    retraction_service_name = LaunchConfiguration("retraction_service_name")

    rosbridge = ExecuteProcess(
        condition=IfCondition(enable_rosbridge),
        cmd=[
            "ros2",
            "run",
            "integration_debug",
            rosbridge_executable,
            "--ros-args",
            "-p",
            ["port:=", rosbridge_port],
            "-p",
            ["address:=", rosbridge_address],
            "-p",
            ["default_call_service_timeout:=", rosbridge_timeout],
        ],
        on_exit=Shutdown(reason="debug observer rosbridge stopped"),
        output="screen",
    )
    observer = Node(
        package="integration_debug",
        executable="integration_debug_observer",
        name="integration_debug_observer",
        parameters=[
            {
                "config_path": config_path,
                "run_root": run_root,
                "robot_endpoint_source": robot_endpoint_source,
                "retraction_service_name": retraction_service_name,
            }
        ],
        on_exit=Shutdown(reason="debug observer stopped"),
        output="screen",
    )
    return LaunchDescription(
        [
            DeclareLaunchArgument("enable_rosbridge", default_value="true"),
            DeclareLaunchArgument(
                "rosbridge_port",
                default_value=EnvironmentVariable(
                    "ROSBRIDGE_DEBUG_PORT", default_value="9093"
                ),
            ),
            DeclareLaunchArgument(
                "rosbridge_address",
                default_value=EnvironmentVariable(
                    "ROSBRIDGE_ADDRESS", default_value="127.0.0.1"
                ),
            ),
            DeclareLaunchArgument("rosbridge_service_timeout", default_value="30.0"),
            DeclareLaunchArgument(
                "rosbridge_executable",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_DEBUG_ROSBRIDGE_EXECUTABLE",
                    default_value="secure_debug_rosbridge",
                ),
            ),
            DeclareLaunchArgument("config_path", default_value=_config_default()),
            DeclareLaunchArgument(
                "run_root",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_RUN_ROOT", default_value="/tmp/taskplanner-runs"
                ),
            ),
            DeclareLaunchArgument(
                "robot_endpoint_source",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_DEBUG_ROBOT_ENDPOINT_SOURCE",
                    default_value="external",
                ),
                choices=("external", "virtual"),
            ),
            DeclareLaunchArgument(
                "retraction_service_name", default_value="/surgery/retraction/command"
            ),
            rosbridge,
            observer,
        ]
    )


def generate_debug_control_launch_description() -> LaunchDescription:
    """Launch only the Debug typed-control owner, without a browser bridge."""

    return LaunchDescription(
        [
            DeclareLaunchArgument("config_path", default_value=_config_default()),
            DeclareLaunchArgument(
                "run_root",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_RUN_ROOT", default_value="/tmp/taskplanner-runs"
                ),
            ),
            DeclareLaunchArgument(
                "robot_endpoint_source",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_DEBUG_ROBOT_ENDPOINT_SOURCE",
                    default_value="external",
                ),
                choices=("external", "virtual"),
            ),
            DeclareLaunchArgument("virtual_robot_enabled", default_value="false"),
            DeclareLaunchArgument(
                "retraction_service_name", default_value="/surgery/retraction/command"
            ),
            DeclareLaunchArgument(
                "virtual_retraction_service_name",
                default_value="/integration/debug/virtual/retraction/command",
            ),
            DeclareLaunchArgument(
                "virtual_tool_handover_name",
                default_value="/integration/debug/virtual/tool_handover",
            ),
            DeclareLaunchArgument(
                "virtual_bed_robot_status_topic",
                default_value="/integration/debug/virtual/bed_robot_arms/status",
            ),
            Node(
                package="integration_debug",
                executable="integration_debug_control",
                name="integration_debug_control",
                parameters=[
                    {
                        "config_path": LaunchConfiguration("config_path"),
                        "run_root": LaunchConfiguration("run_root"),
                        "robot_endpoint_source": LaunchConfiguration(
                            "robot_endpoint_source"
                        ),
                        "virtual_robot_enabled": ParameterValue(
                            LaunchConfiguration("virtual_robot_enabled"), value_type=bool
                        ),
                        "retraction_service_name": LaunchConfiguration(
                            "retraction_service_name"
                        ),
                        "virtual_retraction_service_name": LaunchConfiguration(
                            "virtual_retraction_service_name"
                        ),
                        "virtual_tool_handover_name": LaunchConfiguration(
                            "virtual_tool_handover_name"
                        ),
                        "virtual_bed_robot_status_topic": LaunchConfiguration(
                            "virtual_bed_robot_status_topic"
                        ),
                    }
                ],
                output="screen",
            ),
        ]
    )


def generate_debug_virtual_launch_description() -> LaunchDescription:
    """Launch only the virtual Debug endpoint provider.

    This is a direct owner launch rather than an include of the historical
    Debug graph: restarting the emulator must not reconstruct the browser
    bridge, observer, typed-control node, ASR, perception, or a scenario.
    The endpoint names match the separately configured Debug-control owner.
    """

    virtual_robot = Node(
        package="surgical_interop_execution",
        executable="fault_action_emulator",
        name="integration_debug_virtual_robot",
        parameters=[
            {
                "profile_path": LaunchConfiguration("profile_path"),
                "procedure_type": LaunchConfiguration("procedure_type"),
                "max_retraction_distance_m": LaunchConfiguration(
                    "max_retraction_distance_m"
                ),
            }
        ],
        remappings=[
            ("/surgery/tool_handover", LaunchConfiguration("virtual_tool_handover_name")),
            (
                "/surgery/retraction/command",
                LaunchConfiguration("virtual_retraction_service_name"),
            ),
            (
                "/external/bed_robot_arms/status",
                LaunchConfiguration("virtual_bed_robot_status_topic"),
            ),
            ("/test/action_emulator/status", "/integration/debug/virtual/status"),
        ],
        on_exit=Shutdown(reason="integration debug virtual robot stopped"),
        output="screen",
    )
    return LaunchDescription(
        [
            DeclareLaunchArgument(
                "profile_path",
                default_value=PathJoinSubstitution(
                    [
                        FindPackageShare("integration_debug"),
                        "config",
                        "virtual_robot.yaml",
                    ]
                ),
            ),
            DeclareLaunchArgument("procedure_type", default_value="nephrectomy"),
            DeclareLaunchArgument("max_retraction_distance_m", default_value="0.050"),
            DeclareLaunchArgument(
                "virtual_retraction_service_name",
                default_value="/integration/debug/virtual/retraction/command",
            ),
            DeclareLaunchArgument(
                "virtual_tool_handover_name",
                default_value="/integration/debug/virtual/tool_handover",
            ),
            DeclareLaunchArgument(
                "virtual_bed_robot_status_topic",
                default_value="/integration/debug/virtual/bed_robot_arms/status",
            ),
            virtual_robot,
        ]
    )
