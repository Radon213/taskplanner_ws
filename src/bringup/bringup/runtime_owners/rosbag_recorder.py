"""Direct launch wiring for the manual, read-only rosbag2 recorder owner."""

from __future__ import annotations

from typing import Final

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import EnvironmentVariable, LaunchConfiguration
from launch_ros.actions import Node


NodeIdentity = tuple[str, str, str]

ROSBAG_RECORDER_OWNER_NODE_IDENTITIES: Final[dict[str, frozenset[NodeIdentity]]] = {
    "rosbag-recorder": frozenset(
        {
            (
                "integration_debug",
                "operational_rosbag_recorder",
                "operational_rosbag_recorder",
            )
        }
    )
}


def generate_rosbag_recorder_launch_description() -> LaunchDescription:
    """Launch one recorder controlled only by its own manual service.

    The node deliberately receives no scenario-control or lifecycle parameters:
    its recording window is an operator-managed switch, independent of any
    procedure start, pause, completion, or reset.
    """

    return LaunchDescription(
        [
            DeclareLaunchArgument(
                "output_dir",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_ROSBAG_OUTPUT_DIR", default_value="/taskplanner-rosbags"
                ),
            ),
            DeclareLaunchArgument(
                "min_free_bytes",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_ROSBAG_MIN_FREE_BYTES", default_value="21474836480"
                ),
            ),
            DeclareLaunchArgument(
                "max_bag_bytes",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_ROSBAG_MAX_BAG_BYTES", default_value="17179869184"
                ),
            ),
            DeclareLaunchArgument(
                "max_cache_bytes",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_ROSBAG_MAX_CACHE_BYTES", default_value="536870912"
                ),
            ),
            Node(
                package="integration_debug",
                executable="operational_rosbag_recorder",
                name="operational_rosbag_recorder",
                parameters=[
                    {
                        "output_dir": LaunchConfiguration("output_dir"),
                        "min_free_bytes": LaunchConfiguration("min_free_bytes"),
                        "max_bag_bytes": LaunchConfiguration("max_bag_bytes"),
                        "max_cache_bytes": LaunchConfiguration("max_cache_bytes"),
                    }
                ],
                output="screen",
            ),
        ]
    )
