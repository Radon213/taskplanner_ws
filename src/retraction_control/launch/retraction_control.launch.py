"""Launch the isolated native retraction controller runtime."""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, SetEnvironmentVariable, Shutdown
from launch.substitutions import (
    EnvironmentVariable,
    LaunchConfiguration,
    PathJoinSubstitution,
)
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue
from launch_ros.substitutions import FindPackageShare


def generate_launch_description() -> LaunchDescription:
    ros_domain_id = LaunchConfiguration("ros_domain_id")
    rmw_implementation = LaunchConfiguration("rmw_implementation")
    profile_path = LaunchConfiguration("profile_path")
    adapter_mode = LaunchConfiguration("adapter_mode")
    data_directory = LaunchConfiguration("data_directory")
    allow_motion = LaunchConfiguration("allow_motion")
    sdk_license_path = LaunchConfiguration("sdk_license_path")

    return LaunchDescription(
        [
            DeclareLaunchArgument("ros_domain_id", default_value="0"),
            DeclareLaunchArgument(
                "rmw_implementation", default_value="rmw_cyclonedds_cpp"
            ),
            DeclareLaunchArgument(
                "profile_path",
                default_value=PathJoinSubstitution(
                    [FindPackageShare("retraction_control"), "config", "fake.yaml"]
                ),
            ),
            DeclareLaunchArgument(
                "adapter_mode",
                default_value="fake",
                choices=("fake", "shadow", "hardware"),
                description=(
                    "fake is deterministic; shadow records calls without motion; "
                    "hardware requires an approved profile and allow_motion=true"
                ),
            ),
            DeclareLaunchArgument(
                "data_directory",
                default_value=EnvironmentVariable(
                    "RETRACTION_CONTROL_DATA_DIR",
                    default_value="/tmp/retraction-control",
                ),
            ),
            DeclareLaunchArgument("allow_motion", default_value="false"),
            DeclareLaunchArgument(
                "sdk_license_path",
                default_value=EnvironmentVariable(
                    "NEUROMEKA_LICENSE_PATH", default_value=""
                ),
                description="Path to a runtime secret; never the license value itself.",
            ),
            # These values apply only to processes spawned by this isolated
            # launch description; the existing domain-97 handover runtime is
            # not overlaid or restarted.
            SetEnvironmentVariable(name="ROS_DOMAIN_ID", value=ros_domain_id),
            SetEnvironmentVariable(
                name="RMW_IMPLEMENTATION", value=rmw_implementation
            ),
            Node(
                package="retraction_control",
                executable="command_server_node",
                name="retraction_command_server",
                parameters=[
                    {
                        "profile_path": profile_path,
                        "adapter_mode": adapter_mode,
                        "data_directory": data_directory,
                        "allow_motion": ParameterValue(allow_motion, value_type=bool),
                        "sdk_license_path": sdk_license_path,
                        "expected_ros_domain_id": ParameterValue(
                            ros_domain_id, value_type=int
                        ),
                    }
                ],
                on_exit=Shutdown(reason="retraction command server stopped"),
                output="screen",
            ),
        ]
    )
