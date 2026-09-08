"""Direct launch wiring for Taskplanner's synthetic camera and surgeon actor owner."""

from __future__ import annotations

import os
from typing import Final

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction, SetLaunchConfiguration
from launch.conditions import IfCondition
from launch.substitutions import EnvironmentVariable, LaunchConfiguration, PathJoinSubstitution, PythonExpression
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue
from launch_ros.substitutions import FindPackageShare

from bringup.runtime_profile import RUNTIME_PROFILE_NAMES, RuntimeProfileError, resolve_runtime_profile

_SIMULATION_INPUT_PROFILE_ARGUMENT_NAMES: Final[tuple[str, ...]] = (
    "default_bundle",
    "input_profile",
    "vlm_mode",
    "surgeon_actor_mode",
    "actor_base_url",
    "actor_provider_id",
    "actor_model_id",
    "actor_response_format",
    "actor_reasoning_effort",
    "enable_no_image_camera",
    "enable_synthetic_scene_camera",
)


def _simulation_input_profile_actions(context) -> list[SetLaunchConfiguration]:
    """Resolve only the simulation actor/camera owner profile surface.

    This owner is started by the LLM-surgeon profile.  Keeping its profile
    resolution local prevents an actor/camera restart from parsing command,
    Digital Twin, VLM, perception, or controller-route declarations.
    """

    profile_name = LaunchConfiguration("runtime_profile").perform(context)
    try:
        profile = resolve_runtime_profile(profile_name, environment=os.environ)
    except RuntimeProfileError as exc:
        raise RuntimeError(str(exc)) from exc
    values = profile.arguments_for("simulation-input")
    return [
        SetLaunchConfiguration(name, values[name])
        for name in _SIMULATION_INPUT_PROFILE_ARGUMENT_NAMES
        if name in values
    ]


def _simulation_input_actions() -> list[object]:
    """Build synthetic camera and surgeon-actor inputs without the monolith."""

    spec_dir = LaunchConfiguration("spec_dir")
    input_profile = LaunchConfiguration("input_profile")
    vlm_mode = LaunchConfiguration("vlm_mode")
    surgeon_actor_mode = LaunchConfiguration("surgeon_actor_mode")
    actor_base_url = LaunchConfiguration("actor_base_url")
    actor_provider_id = LaunchConfiguration("actor_provider_id")
    actor_model_id = LaunchConfiguration("actor_model_id")
    actor_response_format = LaunchConfiguration("actor_response_format")
    actor_reasoning_effort = LaunchConfiguration("actor_reasoning_effort")
    enable_no_image_camera = LaunchConfiguration("enable_no_image_camera")
    enable_synthetic_scene_camera = LaunchConfiguration(
        "enable_synthetic_scene_camera"
    )
    procedure_image_vlm_enabled = LaunchConfiguration("procedure_image_vlm_enabled")
    procedure_surgeon_actor_enabled = LaunchConfiguration(
        "procedure_surgeon_actor_enabled"
    )

    rule_surgeon_actor_enabled = PythonExpression(
        [
            "'",
            procedure_surgeon_actor_enabled,
            "'.lower() == 'true' and '",
            input_profile,
            "' == 'simulation' and '",
            surgeon_actor_mode,
            "' == 'rule'",
        ]
    )
    llm_surgeon_actor_enabled = PythonExpression(
        [
            "'",
            procedure_surgeon_actor_enabled,
            "'.lower() == 'true' and '",
            input_profile,
            "' == 'simulation' and '",
            surgeon_actor_mode,
            "' == 'llm'",
        ]
    )
    no_image_camera_enabled = PythonExpression(
        [
            "'",
            procedure_image_vlm_enabled,
            "'.lower() == 'true' and '",
            input_profile,
            "' == 'simulation' and '",
            enable_no_image_camera,
            "'.lower() in ('true', '1', 'yes')",
        ]
    )
    synthetic_scene_camera_enabled = PythonExpression(
        [
            "'",
            procedure_image_vlm_enabled,
            "'.lower() == 'true' and '",
            input_profile,
            "' == 'simulation' and '",
            enable_synthetic_scene_camera,
            "'.lower() in ('true', '1', 'yes')",
        ]
    )

    return [
        Node(
            package="vlm_node",
            executable="synthetic_scene_camera",
            name="synthetic_scene_camera",
            condition=IfCondition(synthetic_scene_camera_enabled),
            output="screen",
        ),
        Node(
            package="vlm_node",
            executable="no_image_camera",
            name="no_image_camera",
            condition=IfCondition(no_image_camera_enabled),
            parameters=[
                {
                    "image_topic": "/surgery/images/field/compressed",
                    "fps": 30.0,
                    "label": "",
                    "spec_dir": spec_dir,
                }
            ],
            output="screen",
        ),
        Node(
            package="simulation_runtime",
            executable="surgeon_actor",
            name="surgeon_actor",
            condition=IfCondition(rule_surgeon_actor_enabled),
            parameters=[
                {
                    "spec_dir": spec_dir,
                    "decision_period_sec": 0.25,
                    "min_tool_use_sec": 3.0,
                }
            ],
            output="screen",
        ),
        Node(
            package="simulation_runtime",
            executable="llm_surgeon_actor",
            name="surgeon_actor",
            condition=IfCondition(llm_surgeon_actor_enabled),
            parameters=[
                {
                    "spec_dir": spec_dir,
                    "base_url": actor_base_url,
                    "provider_id": actor_provider_id,
                    "model_id": actor_model_id,
                    "response_format": actor_response_format,
                    "reasoning_effort": actor_reasoning_effort,
                    "decision_period_sec": 0.25,
                    "require_voice_for_tool_requests": ParameterValue(
                        PythonExpression(["'", vlm_mode, "' == 'voice_only'"]),
                        value_type=bool,
                    ),
                }
            ],
            output="screen",
        ),
    ]


def _generate_simulation_input_launch_description() -> LaunchDescription:
    """Build the scoped LLM-surgeon input owner without legacy graph parsing."""

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
                description="Mode-level synthetic camera and surgeon-actor defaults.",
            ),
            OpaqueFunction(
                function=lambda context: _simulation_input_profile_actions(context)
            ),
            DeclareLaunchArgument(
                "default_bundle",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_DEFAULT_BUNDLE", default_value="thyroidectomy"
                ),
            ),
            DeclareLaunchArgument("spec_dir", default_value=spec_default),
            DeclareLaunchArgument(
                "procedure_image_vlm_enabled",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_CAPABILITY_IMAGE_VLM", default_value="true"
                ),
            ),
            DeclareLaunchArgument(
                "procedure_surgeon_actor_enabled",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_CAPABILITY_SURGEON_ACTOR", default_value="true"
                ),
            ),
            DeclareLaunchArgument("input_profile", default_value="simulation"),
            DeclareLaunchArgument("vlm_mode", default_value="real"),
            DeclareLaunchArgument("surgeon_actor_mode", default_value="llm"),
            DeclareLaunchArgument(
                "actor_base_url", default_value="http://127.0.0.1:1234"
            ),
            DeclareLaunchArgument("actor_provider_id", default_value="auto"),
            DeclareLaunchArgument(
                "actor_model_id", default_value="google/gemma-4-12b-qat"
            ),
            DeclareLaunchArgument(
                "actor_response_format", default_value="json_schema"
            ),
            DeclareLaunchArgument("actor_reasoning_effort", default_value="none"),
            DeclareLaunchArgument("enable_no_image_camera", default_value="true"),
            DeclareLaunchArgument(
                "enable_synthetic_scene_camera", default_value="false"
            ),
            *_simulation_input_actions(),
        ]
    )


