"""Direct launch wiring for Taskplanner's independent tool-belief owner."""

from __future__ import annotations

import os

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction, SetLaunchConfiguration
from launch.conditions import IfCondition
from launch.substitutions import EnvironmentVariable, LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare

from bringup.runtime_profile import RUNTIME_PROFILE_NAMES, RuntimeProfileError, resolve_runtime_profile

def _tool_state_profile_actions(context) -> list[SetLaunchConfiguration]:
    """Apply the small bundle/observer surface owned by tool-state."""

    profile_name = LaunchConfiguration("runtime_profile").perform(context)
    try:
        profile = resolve_runtime_profile(profile_name, environment=os.environ)
    except RuntimeProfileError as exc:
        raise RuntimeError(str(exc)) from exc
    values = profile.arguments_for("tool-state")
    return [
        SetLaunchConfiguration(name, values[name])
        for name in (
            "default_bundle",
            "enable_tool_belief_tracker",
        )
    ]


def _generate_tool_state_launch_description() -> LaunchDescription:
    """Build the advisory tool-belief observer without the legacy graph."""

    default_bundle = LaunchConfiguration("default_bundle")
    spec_dir = LaunchConfiguration("spec_dir")
    bundle_snapshot_root = LaunchConfiguration("bundle_snapshot_root")
    enable_tool_belief_tracker = LaunchConfiguration("enable_tool_belief_tracker")
    spec_default = PathJoinSubstitution(
        [FindPackageShare("procedure_spec"), "specs", default_bundle]
    )
    return LaunchDescription(
        [
            DeclareLaunchArgument(
                "runtime_profile",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_RUNTIME_MODE", default_value="mock"
                ),
                choices=RUNTIME_PROFILE_NAMES,
                description="Mode-level tool-belief defaults.",
            ),
            OpaqueFunction(function=lambda context: _tool_state_profile_actions(context)),
            DeclareLaunchArgument(
                "default_bundle",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_DEFAULT_BUNDLE", default_value="thyroidectomy"
                ),
            ),
            DeclareLaunchArgument("spec_dir", default_value=spec_default),
            DeclareLaunchArgument(
                "bundle_snapshot_root",
                default_value="/tmp/taskplanner-procedure-snapshots",
            ),
            DeclareLaunchArgument(
                "enable_tool_belief_tracker",
                default_value=EnvironmentVariable(
                    "ENABLE_TOOL_BELIEF_TRACKER", default_value="false"
                ),
            ),
            Node(
                package="tool_belief_tracker",
                executable="tool_belief_tracker_node",
                name="tool_belief_tracker",
                condition=IfCondition(enable_tool_belief_tracker),
                parameters=[
                    PathJoinSubstitution(
                        [
                            FindPackageShare("tool_belief_tracker"),
                            "config",
                            "default.yaml",
                        ]
                    ),
                    {
                        "spec_dir": spec_dir,
                        "bundle_snapshot_root": bundle_snapshot_root,
                        "cam3_pose_topic": "/perception/cam_3/tool/poses",
                        "cam4_pose_topic": "/perception/cam_4/tool/poses",
                        "cam3_health_topic": "/perception/cam_3/tool/health",
                        "cam4_health_topic": "/perception/cam_4/tool/health",
                        "skill_command_topic": "/bt/skill_command",
                        "skill_status_topic": "/skill/status",
                        "skill_event_topic": "/skill/events",
                        "simulation_state_topic": "/simulation/state",
                        "output_topic": "/surgery/perception/tool_beliefs",
                    },
                ],
                output="screen",
                respawn=True,
                respawn_delay=1.0,
            ),
        ]
    )

