"""Launch the always-on, read-only multicam observation bridge."""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, ExecuteProcess
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

from integration_debug.bridge_policy import MULTICAM_OBSERVER_ROSAPI_TOPICS_GLOB


def generate_launch_description() -> LaunchDescription:
    rosbridge_port = LaunchConfiguration("rosbridge_port")
    rosbridge_address = LaunchConfiguration("rosbridge_address")
    rosbridge_timeout = LaunchConfiguration("rosbridge_service_timeout")

    return LaunchDescription(
        [
            DeclareLaunchArgument("rosbridge_port", default_value="9094"),
            DeclareLaunchArgument("rosbridge_address", default_value="127.0.0.1"),
            DeclareLaunchArgument("rosbridge_service_timeout", default_value="10.0"),
            # Keep this rosapi node isolated from Debug Mode's canonical
            # /rosapi service namespace.  Topic discovery is the observer's
            # only allowed service call; service and parameter discovery are
            # disabled at the rosapi node itself as a second safety boundary.
            Node(
                package="rosapi",
                executable="rosapi_node",
                namespace="multicam_observer",
                name="rosapi",
                parameters=[
                    {
                        "topics_glob": MULTICAM_OBSERVER_ROSAPI_TOPICS_GLOB,
                        "services_glob": "[]",
                        "params_glob": "[]",
                    }
                ],
                output="screen",
            ),
            ExecuteProcess(
                cmd=[
                    "ros2",
                    "run",
                    "integration_debug",
                    "secure_multicam_rosbridge",
                    "--ros-args",
                    "-p",
                    ["port:=", rosbridge_port],
                    "-p",
                    ["address:=", rosbridge_address],
                    "-p",
                    ["default_call_service_timeout:=", rosbridge_timeout],
                ],
                output="screen",
            ),
        ]
    )
