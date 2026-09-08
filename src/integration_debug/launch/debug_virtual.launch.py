"""Launch the isolated virtual Debug robot emulator.

This owner exists for standalone virtual Debug only. It deliberately does not
start the browser bridge, Debug observer/control node, ASR, record, PNU,
or camera workers. Pair it with ``debug_control.launch.py`` using
``robot_endpoint_source:=virtual virtual_robot_enabled:=true`` when a typed
Debug control owner should target the virtual endpoints.
"""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, Shutdown
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare


def generate_launch_description() -> LaunchDescription:
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
