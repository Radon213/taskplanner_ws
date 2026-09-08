"""Direct launch wiring for Taskplanner's read-only state and camera projection owner."""

from __future__ import annotations

import os

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction, SetLaunchConfiguration
from launch.conditions import IfCondition
from launch.substitutions import EnvironmentVariable, LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue
from launch_ros.substitutions import FindPackageShare

from bringup.runtime_profile import RUNTIME_PROFILE_NAMES, RuntimeProfileError, resolve_runtime_profile

def _projection_profile_actions(context) -> list[SetLaunchConfiguration]:
    """Apply the gateway's small read-only projection configuration."""

    profile_name = LaunchConfiguration("runtime_profile").perform(context)
    try:
        profile = resolve_runtime_profile(profile_name, environment=os.environ)
    except RuntimeProfileError as exc:
        raise RuntimeError(str(exc)) from exc
    values = profile.arguments_for("projection")
    return [
        SetLaunchConfiguration(name, values[name])
        for name in (
            "default_bundle",
            "publish_shared_state",
            "publish_shared_free_text",
            "flir_input_topic",
            "cam4_input_topic",
            "publish_camera_aliases",
            "publish_flir_while_idle",
        )
    ]


def _generate_projection_launch_description() -> LaunchDescription:
    """Build the read-only gateway/alias plane without the legacy graph."""

    default_bundle = LaunchConfiguration("default_bundle")
    spec_dir = LaunchConfiguration("spec_dir")
    publish_shared_state = LaunchConfiguration("publish_shared_state")
    publish_shared_free_text = LaunchConfiguration("publish_shared_free_text")
    flir_input_topic = LaunchConfiguration("flir_input_topic")
    cam4_input_topic = LaunchConfiguration("cam4_input_topic")
    publish_camera_aliases = LaunchConfiguration("publish_camera_aliases")
    publish_flir_while_idle = LaunchConfiguration("publish_flir_while_idle")
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
                description="Mode-level read-only projection defaults.",
            ),
            OpaqueFunction(function=lambda context: _projection_profile_actions(context)),
            DeclareLaunchArgument(
                "default_bundle",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_DEFAULT_BUNDLE", default_value="thyroidectomy"
                ),
            ),
            DeclareLaunchArgument("spec_dir", default_value=spec_default),
            DeclareLaunchArgument(
                "publish_shared_state",
                default_value=EnvironmentVariable(
                    "PUBLISH_SHARED_STATE", default_value="true"
                ),
            ),
            DeclareLaunchArgument(
                "publish_shared_free_text",
                default_value=EnvironmentVariable(
                    "PUBLISH_SHARED_FREE_TEXT", default_value="false"
                ),
            ),
            DeclareLaunchArgument(
                "flir_input_topic",
                default_value=EnvironmentVariable(
                    "FLIR_INPUT_TOPIC",
                    default_value="/synced/flir/color/image_raw/compressed",
                ),
            ),
            DeclareLaunchArgument(
                "cam4_input_topic",
                default_value=EnvironmentVariable(
                    "CAM4_INPUT_TOPIC",
                    default_value="/synced/cam_4/color/image_raw/compressed",
                ),
            ),
            DeclareLaunchArgument(
                "publish_camera_aliases",
                default_value=EnvironmentVariable(
                    "PUBLISH_CAMERA_ALIASES", default_value="true"
                ),
            ),
            DeclareLaunchArgument(
                "publish_flir_while_idle",
                default_value=EnvironmentVariable(
                    "PUBLISH_FLIR_WHILE_IDLE", default_value="false"
                ),
            ),
            Node(
                package="surgical_interop_gateway",
                executable="surgical_interop_gateway",
                name="surgical_interop_gateway",
                condition=IfCondition(publish_shared_state),
                parameters=[
                    {
                        "default_bundle": default_bundle,
                        "spec_dir": spec_dir,
                        "publish_free_text": ParameterValue(
                            publish_shared_free_text,
                            value_type=bool,
                        ),
                    }
                ],
                output="screen",
            ),
            Node(
                package="surgical_interop_gateway",
                executable="camera_alias_relay",
                name="surgical_camera_alias_relay",
                condition=IfCondition(publish_camera_aliases),
                parameters=[
                    {
                        "flir_source_topic": flir_input_topic,
                        "flir_public_topic": "/surgery/images/flir/compressed",
                        "cam4_source_topic": cam4_input_topic,
                        "cam4_public_topic": "/surgery/images/cam4/compressed",
                        "cam3_overlay_source_topic": (
                            "/perception/cam_3/overlay/compressed"
                        ),
                        "cam3_overlay_public_topic": (
                            "/surgery/images/cam3/overlay/compressed"
                        ),
                        "cam4_overlay_source_topic": (
                            "/perception/cam_4/overlay/compressed"
                        ),
                        "cam4_overlay_public_topic": (
                            "/surgery/images/cam4/overlay/compressed"
                        ),
                        "suction_overlay_source_topic": (
                            "/perception/suction/overlay/compressed"
                        ),
                        "suction_overlay_public_topic": (
                            "/surgery/images/suction/overlay/compressed"
                        ),
                        "right_ee_overlay_source_topic": (
                            "/perception/right_ee/overlay/compressed"
                        ),
                        "right_ee_overlay_public_topic": (
                            "/surgery/images/right_ee/overlay/compressed"
                        ),
                        "default_bundle": default_bundle,
                        "publish_flir_while_idle": publish_flir_while_idle,
                    }
                ],
                output="screen",
            ),
        ]
    )

