"""Launch the scenario-free Taskplanner integration Debug Mode runtime."""

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
    config_path = LaunchConfiguration("config_path")
    run_root = LaunchConfiguration("run_root")

    rosbridge = ExecuteProcess(
        condition=IfCondition(enable_rosbridge),
        cmd=[
            "ros2",
            "run",
            "rosbridge_server",
            "rosbridge_websocket",
            "--ros-args",
            "-p",
            ["port:=", rosbridge_port],
            "-p",
            ["address:=", rosbridge_address],
            "-p",
            ["default_call_service_timeout:=", rosbridge_timeout],
        ],
        output="screen",
    )

    return LaunchDescription(
        [
            DeclareLaunchArgument("enable_rosbridge", default_value="true"),
            DeclareLaunchArgument("rosbridge_port", default_value="9091"),
            DeclareLaunchArgument("rosbridge_address", default_value="127.0.0.1"),
            DeclareLaunchArgument("rosbridge_service_timeout", default_value="30.0"),
            DeclareLaunchArgument(
                "config_path",
                default_value=PathJoinSubstitution(
                    [FindPackageShare("integration_debug"), "config", "integration_debug.yaml"]
                ),
            ),
            DeclareLaunchArgument(
                "run_root",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_RUN_ROOT", default_value="/tmp/taskplanner-runs"
                ),
            ),
            rosbridge,
            Node(
                package="rosapi",
                executable="rosapi_node",
                name="debug_rosapi",
                condition=IfCondition(enable_rosbridge),
                parameters=[{"use_sim_time": False}],
                output="screen",
            ),
            Node(
                package="integration_debug",
                executable="integration_debug_node",
                name="integration_debug_gateway",
                parameters=[{"config_path": config_path, "run_root": run_root}],
                on_exit=Shutdown(reason="integration debug gateway stopped"),
                output="screen",
            ),
        ]
    )
