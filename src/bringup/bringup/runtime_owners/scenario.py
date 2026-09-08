"""Direct launch wiring for the standalone ScenarioStore owner."""

from __future__ import annotations

import os

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction, SetLaunchConfiguration
from launch.substitutions import EnvironmentVariable, LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue
from launch_ros.substitutions import FindPackageShare

from bringup.runtime_profile import RUNTIME_PROFILE_NAMES, RuntimeProfileError, resolve_runtime_profile

def _scenario_actions() -> list[object]:
    """Create the standalone ScenarioStore owner introduced by this migration."""

    default_bundle = LaunchConfiguration("default_bundle")
    return [
        DeclareLaunchArgument(
            "scenario_config_topic", default_value="/simulation/scenario_config"
        ),
        DeclareLaunchArgument(
            "simulation_state_topic", default_value="/simulation/state"
        ),
        DeclareLaunchArgument(
            "simulation_state_max_age_sec", default_value="3.0"
        ),
        DeclareLaunchArgument(
            "select_bundle_service", default_value="/simulation/select_bundle"
        ),
        DeclareLaunchArgument(
            "scenario_selection_state_path",
            default_value=EnvironmentVariable(
                "TASKPLANNER_SCENARIO_SELECTION_STATE_PATH", default_value=""
            ),
            description=(
                "Optional atomic last-good selected-bundle snapshot owned only "
                "by ScenarioStore."
            ),
        ),
        Node(
            package="simulation_runtime",
            executable="scenario_store",
            name="scenario_store",
            parameters=[
                {
                    "spec_root": PathJoinSubstitution(
                        [FindPackageShare("procedure_spec"), "specs"]
                    ),
                    "default_bundle": default_bundle,
                    "scenario_config_topic": LaunchConfiguration(
                        "scenario_config_topic"
                    ),
                    "simulation_state_topic": LaunchConfiguration(
                        "simulation_state_topic"
                    ),
                    "simulation_state_max_age_sec": ParameterValue(
                        LaunchConfiguration("simulation_state_max_age_sec"),
                        value_type=float,
                    ),
                    "select_bundle_service": LaunchConfiguration(
                        "select_bundle_service"
                    ),
                    "selection_state_path": LaunchConfiguration(
                        "scenario_selection_state_path"
                    ),
                }
            ],
            output="screen",
        ),
    ]


def _scenario_profile_actions(context) -> list[SetLaunchConfiguration]:
    """Apply only the two values that the independent ScenarioStore consumes.

    Scenario selection is deliberately a tiny owner: it does not need to
    import the retained mock graph merely to inherit camera, VLM, execution,
    or voice defaults.  The state root and bundle remain profile-derived so a
    dedicated ScenarioStore restart reconstructs the same last-good context
    as the rest of the selected mode.
    """

    profile_name = LaunchConfiguration("runtime_profile").perform(context)
    try:
        profile = resolve_runtime_profile(profile_name, environment=os.environ)
    except RuntimeProfileError as exc:
        raise RuntimeError(str(exc)) from exc
    values = profile.arguments_for("scenario")
    return [
        SetLaunchConfiguration(name, values[name])
        for name in ("default_bundle", "scenario_selection_state_path")
    ]


def _generate_scenario_owner_launch_description() -> LaunchDescription:
    """Build ScenarioStore without parsing the legacy 2,400-line launch graph."""

    return LaunchDescription(
        [
            DeclareLaunchArgument(
                "runtime_profile",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_RUNTIME_MODE", default_value="mock"
                ),
                choices=RUNTIME_PROFILE_NAMES,
                description=(
                    "Mode-level bundle default for the standalone ScenarioStore."
                ),
            ),
            # Keep this an anonymous OpaqueFunction so the generic owner-graph
            # test can distinguish it from retained legacy configuration hooks.
            OpaqueFunction(function=lambda context: _scenario_profile_actions(context)),
            DeclareLaunchArgument(
                "default_bundle",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_DEFAULT_BUNDLE", default_value="thyroidectomy"
                ),
            ),
            *_scenario_actions(),
        ]
    )

