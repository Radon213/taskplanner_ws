"""Launch the scenario-free Taskplanner integration Debug Mode runtime."""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, ExecuteProcess, Shutdown
from launch.conditions import IfCondition
from launch.substitutions import EnvironmentVariable, LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare

from integration_debug.bridge_policy import DEBUG_ROSAPI_TOPICS_GLOB


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
            "integration_debug",
            "secure_debug_rosbridge",
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
            # rosapi only exposes the same bounded multicam/debug topic set
            # that secure_debug_rosbridge can subscribe to.  The browser can
            # call /rosapi/topics, but no parameter-mutating rosapi service.
            Node(
                package="rosapi",
                executable="rosapi_node",
                # roslib's discovery client calls the conventional
                # absolute /rosapi/topics service, so retain rosapi's
                # canonical node/service namespace.
                name="rosapi",
                parameters=[
                    {
                        "topics_glob": DEBUG_ROSAPI_TOPICS_GLOB,
                        "services_glob": "[]",
                        "params_glob": "[]",
                    }
                ],
                output="screen",
            ),
            rosbridge,
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
