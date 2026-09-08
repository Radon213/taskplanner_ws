"""Launch only the typed Debug control owner.

The browser-facing observer remains a separate process.  This owner creates
the configured Action/Service/Topic clients but intentionally starts no
rosbridge, virtual robot, ASR, record, PNU, or camera sidecar.
"""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, Shutdown
from launch.substitutions import EnvironmentVariable, LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue
from launch_ros.substitutions import FindPackageShare


def generate_launch_description() -> LaunchDescription:
    control = Node(
        package="integration_debug",
        executable="integration_debug_control",
        name="integration_debug_control",
        parameters=[
            {
                "config_path": LaunchConfiguration("config_path"),
                "run_root": LaunchConfiguration("run_root"),
                # Public Debug status remains observer-owned. Keep every
                # control projection private so the observer can compose it.
                "status_topic": "/integration/debug/control/status",
                "events_topic": "/integration/debug/control/events",
                "readiness_topic": "/integration/debug/control/readiness",
                "readiness_service": "/integration/debug/control/check_readiness",
                "robot_endpoint_source": LaunchConfiguration("robot_endpoint_source"),
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
        on_exit=Shutdown(reason="debug control owner stopped"),
        output="screen",
    )
    return LaunchDescription(
        [
            DeclareLaunchArgument(
                "config_path",
                default_value=PathJoinSubstitution(
                    [
                        FindPackageShare("integration_debug"),
                        "config",
                        "integration_debug.yaml",
                    ]
                ),
            ),
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
                "retraction_service_name",
                default_value="/surgery/retraction/command",
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
            control,
        ]
    )
