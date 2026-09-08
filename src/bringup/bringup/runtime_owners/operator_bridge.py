"""Direct launch wiring for Taskplanner's private operator ROS bridge owner."""

from __future__ import annotations

import os

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, ExecuteProcess, OpaqueFunction, SetLaunchConfiguration
from launch.substitutions import EnvironmentVariable, LaunchConfiguration, PythonExpression
from launch_ros.actions import Node

from bringup.runtime_profile import RUNTIME_PROFILE_NAMES, RuntimeProfileError, resolve_runtime_profile

def _operator_bridge_profile_actions(context) -> list[SetLaunchConfiguration]:
    """Resolve only the rosbridge settings consumed by its dedicated owner."""

    profile_name = LaunchConfiguration("runtime_profile").perform(context)
    try:
        profile = resolve_runtime_profile(profile_name, environment=os.environ)
    except RuntimeProfileError as exc:
        raise RuntimeError(str(exc)) from exc
    values = profile.arguments_for("operator-bridge")
    return [
        SetLaunchConfiguration(name, values[name])
        for name in (
            "rosbridge_port",
            "rosbridge_address",
            "rosbridge_service_timeout",
        )
    ]


def _operator_bridge_actions() -> list[object]:
    """Build the one private rosbridge/rosapi plane directly.

    This owner is intentionally independent of scenario, perception, and
    execution launch declarations.  The browser can therefore recover its
    private ROS transport without constructing the legacy runtime graph.
    """

    rosbridge_port = LaunchConfiguration("rosbridge_port")
    rosbridge_address = LaunchConfiguration("rosbridge_address")
    rosbridge_service_timeout = LaunchConfiguration("rosbridge_service_timeout")
    return [
        ExecuteProcess(
            respawn=True,
            respawn_delay=5.0,
            cmd=[
                "bash",
                "-lc",
                PythonExpression(
                    [
                        "'if ros2 pkg prefix rosbridge_server >/dev/null 2>&1; then "
                        "ros2 run rosbridge_server rosbridge_websocket --ros-args -p port:=' + str(",
                        rosbridge_port,
                        ") + ' -p address:=' + '",
                        rosbridge_address,
                        "' + ' -p default_call_service_timeout:=' + str(",
                        rosbridge_service_timeout,
                        ") + '; else echo \"[taskplanner-owner-bridge] rosbridge_server is not installed\"; fi'",
                    ]
                ),
            ],
            output="screen",
        ),
        Node(
            package="rosapi",
            executable="rosapi_node",
            name="rosapi",
            parameters=[{"use_sim_time": False}],
            output="screen",
        ),
    ]


def _generate_operator_bridge_launch_description() -> LaunchDescription:
    """Build the private browser bridge without parsing the legacy graph."""

    return LaunchDescription(
        [
            DeclareLaunchArgument(
                "runtime_profile",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_RUNTIME_MODE", default_value="mock"
                ),
                choices=RUNTIME_PROFILE_NAMES,
                description="Mode-level private rosbridge defaults.",
            ),
            OpaqueFunction(
                function=lambda context: _operator_bridge_profile_actions(context)
            ),
            DeclareLaunchArgument(
                "rosbridge_port",
                default_value=EnvironmentVariable("ROSBRIDGE_PORT", default_value="9090"),
            ),
            DeclareLaunchArgument(
                "rosbridge_address",
                default_value=EnvironmentVariable(
                    "ROSBRIDGE_ADDRESS", default_value="127.0.0.1"
                ),
            ),
            DeclareLaunchArgument(
                "rosbridge_service_timeout",
                default_value=EnvironmentVariable(
                    "ROSBRIDGE_SERVICE_TIMEOUT", default_value="30.0"
                ),
            ),
            *_operator_bridge_actions(),
        ]
    )


