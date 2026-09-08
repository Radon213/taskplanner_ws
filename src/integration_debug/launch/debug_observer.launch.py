"""Launch the read-only Debug observer plus its secured ROSBridge.

This is the standalone observer owner: camera, TF, status, and catalog
observation without ASR, record upload, PNU, a virtual robot, or a
mutable Debug command server.
"""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, ExecuteProcess, Shutdown
from launch.conditions import IfCondition
from launch.substitutions import EnvironmentVariable, LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare


def generate_launch_description() -> LaunchDescription:
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
            # Keep the legacy network wrapper's launch arguments so a Compose
            # switch does not change persisted Domain/discovery handling.
            DeclareLaunchArgument("enable_rosbridge", default_value="true"),
            DeclareLaunchArgument("rosbridge_port", default_value="9091"),
            DeclareLaunchArgument("rosbridge_address", default_value="127.0.0.1"),
            DeclareLaunchArgument("rosbridge_service_timeout", default_value="30.0"),
            DeclareLaunchArgument(
                "rosbridge_executable",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_DEBUG_ROSBRIDGE_EXECUTABLE",
                    default_value="secure_debug_rosbridge",
                ),
            ),
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
            DeclareLaunchArgument(
                "retraction_service_name",
                default_value="/surgery/retraction/command",
            ),
            rosbridge,
            observer,
        ]
    )
