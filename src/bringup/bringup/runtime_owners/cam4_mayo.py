"""Direct launch wiring for the typed CAM4-to-Mayo observation owner."""

from __future__ import annotations

import os

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction, SetLaunchConfiguration
from launch.substitutions import EnvironmentVariable, LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare

from bringup.runtime_profile import RUNTIME_PROFILE_NAMES, RuntimeProfileError, resolve_runtime_profile

def _cam4_mayo_profile_actions(context) -> list[SetLaunchConfiguration]:
    """Resolve only the typed CAM4 topic and default scenario bundle.

    The adapter is deliberately outside the VLM/perception owner: restarting
    a small 1.7 observation projection must not recreate a loaded VLM client
    or any optional local perception worker.  ScenarioStore can later update
    aliases over its retained configuration topic, but this launch is also
    usable when ScenarioStore is absent.
    """

    profile_name = LaunchConfiguration("runtime_profile").perform(context)
    try:
        profile = resolve_runtime_profile(profile_name, environment=os.environ)
    except RuntimeProfileError as exc:
        raise RuntimeError(str(exc)) from exc
    values = profile.arguments_for("cam4-mayo")
    actions = [
        SetLaunchConfiguration(name, values[name])
        for name in ("default_bundle", "cam4_tool_observations_topic")
        if name in values
    ]
    bundle_name = values.get("default_bundle", "")
    if bundle_name:
        # ``spec_dir``'s launch-argument default is expanded before this
        # profile hook runs. Set it explicitly so the Live default bundle is
        # usable immediately; the retained ScenarioStore update remains the
        # later alias authority when available.
        actions.append(
            SetLaunchConfiguration(
                "spec_dir",
                PathJoinSubstitution(
                    [FindPackageShare("procedure_spec"), "specs", bundle_name]
                ),
            )
        )
    return actions


def _cam4_mayo_actions() -> list[object]:
    """Run the independent typed 1.7 CAM4-to-Mayo projection only."""

    return [
        Node(
            package="vlm_node",
            executable="cam4_typed_mayo_adapter",
            name="cam4_typed_mayo_adapter",
            parameters=[
                {
                    "input_topic": LaunchConfiguration(
                        "cam4_tool_observations_topic"
                    ),
                    "output_topic": LaunchConfiguration(
                        "mayo_tool_observations_topic"
                    ),
                    "scenario_config_topic": LaunchConfiguration(
                        "scenario_config_topic"
                    ),
                    "spec_dir": LaunchConfiguration("spec_dir"),
                }
            ],
            output="screen",
        )
    ]


def _generate_cam4_mayo_launch_description() -> LaunchDescription:
    """Build a VLM-free, scenario-optional CAM4 Mayo adapter owner."""

    default_bundle = LaunchConfiguration("default_bundle")
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
                description="Typed CAM4 observation source profile.",
            ),
            DeclareLaunchArgument(
                "default_bundle",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_DEFAULT_BUNDLE", default_value="thyroidectomy"
                ),
            ),
            DeclareLaunchArgument("spec_dir", default_value=spec_default),
            DeclareLaunchArgument(
                "cam4_tool_observations_topic",
                default_value=EnvironmentVariable(
                    "CAM4_TOOL_OBSERVATIONS_TOPIC",
                    default_value="/perception/cam_4/tool/observations",
                ),
            ),
            DeclareLaunchArgument(
                "mayo_tool_observations_topic",
                default_value="/surgery/perception/cam4/mayo_tool_observations",
            ),
            DeclareLaunchArgument(
                "scenario_config_topic",
                default_value="/simulation/scenario_config",
            ),
            OpaqueFunction(
                function=lambda context: _cam4_mayo_profile_actions(context)
            ),
            *_cam4_mayo_actions(),
        ]
    )


