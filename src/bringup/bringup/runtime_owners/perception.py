"""Direct launch wiring for Taskplanner's optional perception and VLM owner."""

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

from bringup.perception_config import resolve_launch_perception
from bringup.runtime_profile import RUNTIME_PROFILE_NAMES, RuntimeProfileError, resolve_runtime_profile

_PERCEPTION_PROFILE_ARGUMENT_NAMES: Final[tuple[str, ...]] = (
    "default_bundle",
    "input_profile",
    "validation_mode",
    "vlm_mode",
    "vlm_base_url",
    "vlm_provider_id",
    "vlm_model_id",
    "vlm_api_mode",
    "vlm_publish_period_sec",
    "vlm_max_output_tokens",
    "vlm_generation_seed",
    "vlm_response_format",
    "vlm_reasoning_effort",
    "vlm_response_mode",
    "vlm_context_mode",
    "vlm_image_stale_sec",
    "vlm_require_source_frame_timestamp",
    "vlm_model_input_max_source_lag_sec",
    "vlm_model_input_max_source_future_skew_sec",
    "field_snapshot_url",
    "enable_rfdetr_perception",
    "perception_backend",
    "perception_provider",
    "perception_location",
    "perception_endpoint",
    "rfdetr_service_url",
    "pnu_service_url",
    "pnu_api_token_file",
    "pnu_allow_insecure_remote_http",
    "pnu_allow_unauthenticated_remote",
    "pnu_expected_model_digests_json",
    "pnu_expected_tool_support_plane_config_version",
    "pnu_depth_scale_m_per_unit",
    "pnu_depth_scale_validated",
    "pnu_depth_alignment_validated",
    "pnu_depth_alignment_id",
    "flir_input_topic",
    "cam3_input_topic",
    "cam4_input_topic",
    "field_image_topic",
    "rfdetr_flir_output_topic",
    "composite_image_topic",
    "flir_overlay_image_topic",
    "cam4_overlay_image_topic",
    "cam3_overlay_image_topic",
    "cam4_semantics_topic",
    "cam3_tool_observations_topic",
    "cam4_tool_observations_topic",
    "rfdetr_bridge_cam3_tool_observations_topic",
    "rfdetr_bridge_cam4_tool_observations_topic",
    "cam3_tool_observations_expected_model_version",
    "cam4_tool_observations_expected_model_version",
    "allow_legacy_cam4_semantics_fallback",
    "require_field_image",
    "require_rfdetr_applied_field_image",
    "require_rfdetr_cam4_overlay",
    "cv_contract_status_topic",
    "cv_cam4_rgb_topic",
    "cv_cam4_rgb_alias_topic",
    "cv_cam4_camera_info_topic",
    "cv_cam4_native_depth_compressed_topic",
    "cv_cam4_depth_camera_info_topic",
    "cv_cam4_depth_to_color_extrinsics_topic",
    "cv_cam4_aligned_depth_compressed_topic",
    "cv_cam4_aligned_depth_camera_info_topic",
    "cv_handover_tray_rgb_topic",
    "cv_handover_tray_camera_info_topic",
    "cv_handover_tray_aligned_depth_topic",
)


def _perception_profile_actions(context) -> list[SetLaunchConfiguration]:
    """Resolve the perception owner's small mode/profile surface.

    The direct owner intentionally receives only observation/VLM configuration.
    It never needs command, controller route, browser, or simulation-input
    values.  Live-only 1.7 typed observation bindings are kept in the pure
    profile so restarting perception cannot fall back to Debug raster topics.
    """

    profile_name = LaunchConfiguration("runtime_profile").perform(context)
    try:
        profile = resolve_runtime_profile(profile_name, environment=os.environ)
    except RuntimeProfileError as exc:
        raise RuntimeError(str(exc)) from exc
    values = profile.arguments_for("perception")
    return [
        SetLaunchConfiguration(name, values[name])
        for name in _PERCEPTION_PROFILE_ARGUMENT_NAMES
        if name in values
    ]


def _perception_actions() -> list[object]:
    """Build all observation, VLM, and phase lanes without the legacy graph.

    PNU configuration remains local to this owner.  A bad PNU endpoint or
    credential can fail this optional observation owner, but cannot block the
    state/command/execution core that has already started independently.
    """

    spec_dir = LaunchConfiguration("spec_dir")
    input_profile = LaunchConfiguration("input_profile")
    validation_mode = LaunchConfiguration("validation_mode")
    vlm_mode = LaunchConfiguration("vlm_mode")
    vlm_base_url = LaunchConfiguration("vlm_base_url")
    vlm_provider_id = LaunchConfiguration("vlm_provider_id")
    vlm_model_id = LaunchConfiguration("vlm_model_id")
    vlm_api_mode = LaunchConfiguration("vlm_api_mode")
    vlm_publish_period_sec = LaunchConfiguration("vlm_publish_period_sec")
    vlm_max_output_tokens = LaunchConfiguration("vlm_max_output_tokens")
    vlm_generation_seed = LaunchConfiguration("vlm_generation_seed")
    vlm_response_format = LaunchConfiguration("vlm_response_format")
    vlm_reasoning_effort = LaunchConfiguration("vlm_reasoning_effort")
    vlm_response_mode = LaunchConfiguration("vlm_response_mode")
    vlm_context_mode = LaunchConfiguration("vlm_context_mode")
    vlm_image_stale_sec = LaunchConfiguration("vlm_image_stale_sec")
    vlm_require_source_frame_timestamp = LaunchConfiguration(
        "vlm_require_source_frame_timestamp"
    )
    vlm_model_input_max_source_lag_sec = LaunchConfiguration(
        "vlm_model_input_max_source_lag_sec"
    )
    vlm_model_input_max_source_future_skew_sec = LaunchConfiguration(
        "vlm_model_input_max_source_future_skew_sec"
    )
    procedure_image_vlm_enabled = LaunchConfiguration("procedure_image_vlm_enabled")
    procedure_dialogue_vlm_enabled = LaunchConfiguration(
        "procedure_dialogue_vlm_enabled"
    )
    procedure_perception_enabled = LaunchConfiguration("procedure_perception_enabled")
    procedure_phase_inference_enabled = LaunchConfiguration(
        "procedure_phase_inference_enabled"
    )
    field_snapshot_url = LaunchConfiguration("field_snapshot_url")
    enable_rfdetr_perception = LaunchConfiguration("enable_rfdetr_perception")
    perception_backend = LaunchConfiguration("perception_backend")
    perception_provider = LaunchConfiguration("perception_provider")
    perception_location = LaunchConfiguration("perception_location")
    perception_endpoint = LaunchConfiguration("perception_endpoint")
    pnu_api_token_file = LaunchConfiguration("pnu_api_token_file")
    pnu_allow_insecure_remote_http = LaunchConfiguration(
        "pnu_allow_insecure_remote_http"
    )
    pnu_allow_unauthenticated_remote = LaunchConfiguration(
        "pnu_allow_unauthenticated_remote"
    )
    pnu_expected_model_digests_json = LaunchConfiguration(
        "pnu_expected_model_digests_json"
    )
    pnu_expected_tool_support_plane_config_version = LaunchConfiguration(
        "pnu_expected_tool_support_plane_config_version"
    )
    pnu_depth_scale_m_per_unit = LaunchConfiguration("pnu_depth_scale_m_per_unit")
    pnu_depth_scale_validated = LaunchConfiguration("pnu_depth_scale_validated")
    pnu_depth_alignment_validated = LaunchConfiguration(
        "pnu_depth_alignment_validated"
    )
    pnu_depth_alignment_id = LaunchConfiguration("pnu_depth_alignment_id")
    flir_input_topic = LaunchConfiguration("flir_input_topic")
    cam4_input_topic = LaunchConfiguration("cam4_input_topic")
    cam3_input_topic = LaunchConfiguration("cam3_input_topic")
    field_image_topic = LaunchConfiguration("field_image_topic")
    rfdetr_flir_output_topic = LaunchConfiguration("rfdetr_flir_output_topic")
    composite_image_topic = LaunchConfiguration("composite_image_topic")
    flir_overlay_image_topic = LaunchConfiguration("flir_overlay_image_topic")
    cam4_overlay_image_topic = LaunchConfiguration("cam4_overlay_image_topic")
    cam3_overlay_image_topic = LaunchConfiguration("cam3_overlay_image_topic")
    cam4_semantics_topic = LaunchConfiguration("cam4_semantics_topic")
    cam3_tool_observations_topic = LaunchConfiguration(
        "cam3_tool_observations_topic"
    )
    cam4_tool_observations_topic = LaunchConfiguration(
        "cam4_tool_observations_topic"
    )
    rfdetr_bridge_cam3_tool_observations_topic = LaunchConfiguration(
        "rfdetr_bridge_cam3_tool_observations_topic"
    )
    rfdetr_bridge_cam4_tool_observations_topic = LaunchConfiguration(
        "rfdetr_bridge_cam4_tool_observations_topic"
    )
    cam3_tool_observations_expected_model_version = LaunchConfiguration(
        "cam3_tool_observations_expected_model_version"
    )
    cam4_tool_observations_expected_model_version = LaunchConfiguration(
        "cam4_tool_observations_expected_model_version"
    )
    allow_legacy_cam4_semantics_fallback = LaunchConfiguration(
        "allow_legacy_cam4_semantics_fallback"
    )
    require_field_image = LaunchConfiguration("require_field_image")
    require_rfdetr_applied_field_image = LaunchConfiguration(
        "require_rfdetr_applied_field_image"
    )
    require_rfdetr_cam4_overlay = LaunchConfiguration("require_rfdetr_cam4_overlay")
    cv_contract_status_topic = LaunchConfiguration("cv_contract_status_topic")
    cv_cam4_rgb_topic = LaunchConfiguration("cv_cam4_rgb_topic")
    cv_cam4_rgb_alias_topic = LaunchConfiguration("cv_cam4_rgb_alias_topic")
    cv_cam4_camera_info_topic = LaunchConfiguration("cv_cam4_camera_info_topic")
    cv_cam4_native_depth_compressed_topic = LaunchConfiguration(
        "cv_cam4_native_depth_compressed_topic"
    )
    cv_cam4_depth_camera_info_topic = LaunchConfiguration(
        "cv_cam4_depth_camera_info_topic"
    )
    cv_cam4_depth_to_color_extrinsics_topic = LaunchConfiguration(
        "cv_cam4_depth_to_color_extrinsics_topic"
    )
    cv_cam4_aligned_depth_compressed_topic = LaunchConfiguration(
        "cv_cam4_aligned_depth_compressed_topic"
    )
    cv_cam4_aligned_depth_camera_info_topic = LaunchConfiguration(
        "cv_cam4_aligned_depth_camera_info_topic"
    )
    cv_handover_tray_rgb_topic = LaunchConfiguration("cv_handover_tray_rgb_topic")
    cv_handover_tray_camera_info_topic = LaunchConfiguration(
        "cv_handover_tray_camera_info_topic"
    )
    cv_handover_tray_aligned_depth_topic = LaunchConfiguration(
        "cv_handover_tray_aligned_depth_topic"
    )

    mock_vlm_enabled = PythonExpression(
        [
            "'",
            procedure_image_vlm_enabled,
            "'.lower() == 'true' and '",
            input_profile,
            "' == 'simulation' and ('",
            vlm_mode,
            "' == 'mock' or '",
            vlm_mode,
            "' == 'dual')",
        ]
    )
    real_vlm_enabled = PythonExpression(
        [
            "('",
            procedure_image_vlm_enabled,
            "'.lower() == 'true' or '",
            procedure_dialogue_vlm_enabled,
            "'.lower() == 'true') and ('",
            vlm_mode,
            "' == 'real' or '",
            vlm_mode,
            "' == 'dual')",
        ]
    )
    builtin_rfdetr_adapter_enabled = PythonExpression(
        [
            "'",
            procedure_perception_enabled,
            "'.lower() == 'true' and '",
            perception_provider,
            "' == 'builtin_rfdetr' and '",
            enable_rfdetr_perception,
            "'.lower() in ('true', '1', 'yes')",
        ]
    )
    pnu_adapter_enabled = PythonExpression(
        [
            "'",
            procedure_perception_enabled,
            "'.lower() == 'true' and '",
            perception_provider,
            "' == 'pnu_hand_blood'",
        ]
    )

    return [
        Node(
            package="simulation_runtime",
            executable="source_health_monitor",
            name="source_health_monitor",
            parameters=[
                {
                    "flir_topic": flir_input_topic,
                    "cam4_topic": cam4_input_topic,
                    "vlm_result_topic": PythonExpression(
                        [
                            "'/vlm_real/result' if '",
                            vlm_mode,
                            "' == 'dual' else '/vlm/result'",
                        ]
                    ),
                    "vlm_health_topic": PythonExpression(
                        [
                            "'/vlm_real/health' if '",
                            vlm_mode,
                            "' == 'dual' else '/vlm/health'",
                        ]
                    ),
                    "enable_vlm": ParameterValue(
                        PythonExpression(
                            [
                                "'",
                                procedure_image_vlm_enabled,
                                "'.lower() == 'true' and '",
                                vlm_mode,
                                "' in ('real', 'dual')",
                            ]
                        ),
                        value_type=bool,
                    ),
                }
            ],
            output="screen",
        ),
        Node(
            package="simulation_runtime",
            executable="cv_contract_monitor",
            name="cv_contract_monitor",
            parameters=[
                {
                    "perception_backend": perception_backend,
                    "perception_provider": perception_provider,
                    "perception_location": perception_location,
                    "perception_endpoint": perception_endpoint,
                    "status_topic": cv_contract_status_topic,
                    "cam4_rgb_topic": cv_cam4_rgb_topic,
                    "cam4_rgb_alias_topic": cv_cam4_rgb_alias_topic,
                    "cam4_camera_info_topic": cv_cam4_camera_info_topic,
                    "cam4_native_depth_compressed_topic": (
                        cv_cam4_native_depth_compressed_topic
                    ),
                    "cam4_depth_camera_info_topic": cv_cam4_depth_camera_info_topic,
                    "cam4_depth_to_color_extrinsics_topic": (
                        cv_cam4_depth_to_color_extrinsics_topic
                    ),
                    "cam4_aligned_depth_compressed_topic": (
                        cv_cam4_aligned_depth_compressed_topic
                    ),
                    "cam4_aligned_depth_camera_info_topic": (
                        cv_cam4_aligned_depth_camera_info_topic
                    ),
                    "handover_tray_rgb_topic": cv_handover_tray_rgb_topic,
                    "handover_tray_camera_info_topic": (
                        cv_handover_tray_camera_info_topic
                    ),
                    "handover_tray_aligned_depth_topic": (
                        cv_handover_tray_aligned_depth_topic
                    ),
                }
            ],
            output="screen",
        ),
        Node(
            package="vlm_node",
            executable="rfdetr_perception_bridge",
            name="rfdetr_perception_bridge",
            condition=IfCondition(builtin_rfdetr_adapter_enabled),
            parameters=[
                {
                    "service_url": perception_endpoint,
                    "flir_input_topic": flir_input_topic,
                    "cam4_input_topic": cam4_input_topic,
                    "cam3_input_topic": cam3_input_topic,
                    "flir_output_topic": rfdetr_flir_output_topic,
                    "flir_overlay_topic": flir_overlay_image_topic,
                    "cam4_overlay_topic": cam4_overlay_image_topic,
                    "cam3_overlay_topic": cam3_overlay_image_topic,
                    "cam4_semantics_topic": cam4_semantics_topic,
                    "cam3_tool_observations_topic": (
                        rfdetr_bridge_cam3_tool_observations_topic
                    ),
                    "cam4_tool_observations_topic": (
                        rfdetr_bridge_cam4_tool_observations_topic
                    ),
                    "max_rate_hz": 15.0,
                    "segmented_output_rate_hz": 2.0,
                }
            ],
            output="screen",
        ),
        Node(
            package="vlm_node",
            executable="pnu_perception_bridge",
            name="pnu_perception_bridge",
            condition=IfCondition(pnu_adapter_enabled),
            parameters=[
                {
                    "service_url": perception_endpoint,
                    "rgb_input_topic": cv_cam4_rgb_topic,
                    "color_camera_info_topic": cv_cam4_camera_info_topic,
                    "depth_input_topic": cv_cam4_aligned_depth_compressed_topic,
                    "depth_camera_info_topic": cv_cam4_aligned_depth_camera_info_topic,
                    "cam4_overlay_topic": cam4_overlay_image_topic,
                    "cam4_semantics_topic": cam4_semantics_topic,
                    "cam4_mayo_observation_topic": (
                        "/surgery/perception/cam4/mayo_tool_observations"
                    ),
                    "diagnostics_topic": "/surgery/perception/rfdetr/diagnostics/json",
                    "health_topic": "/surgery/perception/rfdetr/health",
                    "expected_model_digests_json": ParameterValue(
                        pnu_expected_model_digests_json,
                        value_type=str,
                    ),
                    "expected_tool_support_plane_config_version": ParameterValue(
                        pnu_expected_tool_support_plane_config_version,
                        value_type=str,
                    ),
                    "api_token_file": pnu_api_token_file,
                    "allow_insecure_remote_http": ParameterValue(
                        pnu_allow_insecure_remote_http,
                        value_type=bool,
                    ),
                    "allow_unauthenticated_remote": ParameterValue(
                        pnu_allow_unauthenticated_remote,
                        value_type=bool,
                    ),
                    "depth_scale_m_per_unit": ParameterValue(
                        pnu_depth_scale_m_per_unit,
                        value_type=float,
                    ),
                    "depth_scale_validated": ParameterValue(
                        pnu_depth_scale_validated,
                        value_type=bool,
                    ),
                    "depth_alignment_validated": ParameterValue(
                        pnu_depth_alignment_validated,
                        value_type=bool,
                    ),
                    "depth_alignment_id": pnu_depth_alignment_id,
                    "requested_algorithms": ["tool", "blood"],
                    "max_rate_hz": 15.0,
                }
            ],
            output="screen",
        ),
        Node(
            package="vlm_node",
            executable="mock_vlm",
            name="mock_vlm_node",
            condition=IfCondition(mock_vlm_enabled),
            parameters=[
                {
                    "spec_dir": spec_dir,
                    "perception_scene_observations": True,
                    "state_backed_observations": False,
                    "bed_robot_arm_group_proposals_enabled": ParameterValue(
                        PythonExpression(["'", vlm_mode, "' == 'mock'"]),
                        value_type=bool,
                    ),
                }
            ],
            output="screen",
        ),
        Node(
            package="vlm_node",
            executable="snapshot_bridge",
            name="field_snapshot_bridge",
            condition=IfCondition(
                PythonExpression(
                    [
                        "'",
                        procedure_image_vlm_enabled,
                        "'.lower() == 'true' and '",
                        field_snapshot_url,
                        "' != ''",
                    ]
                )
            ),
            parameters=[
                {
                    "snapshot_url": field_snapshot_url,
                    "max_source_age_sec": vlm_image_stale_sec,
                }
            ],
            output="screen",
        ),
        Node(
            package="vlm_node",
            executable="real_vlm",
            name="real_vlm_node",
            condition=IfCondition(real_vlm_enabled),
            parameters=[
                {
                    "spec_dir": spec_dir,
                    "base_url": vlm_base_url,
                    "provider_id": vlm_provider_id,
                    "model_id": vlm_model_id,
                    "api_mode": vlm_api_mode,
                    "publish_period_sec": vlm_publish_period_sec,
                    "max_output_tokens": vlm_max_output_tokens,
                    "generation_seed": ParameterValue(
                        vlm_generation_seed,
                        value_type=int,
                    ),
                    "response_format": vlm_response_format,
                    "reasoning_effort": vlm_reasoning_effort,
                    "response_mode": vlm_response_mode,
                    "context_mode": vlm_context_mode,
                    "image_stale_sec": vlm_image_stale_sec,
                    "require_source_frame_timestamp": ParameterValue(
                        vlm_require_source_frame_timestamp,
                        value_type=bool,
                    ),
                    "model_input_max_source_lag_sec": ParameterValue(
                        vlm_model_input_max_source_lag_sec,
                        value_type=float,
                    ),
                    "model_input_max_source_future_skew_sec": ParameterValue(
                        vlm_model_input_max_source_future_skew_sec,
                        value_type=float,
                    ),
                    "field_image_topic": field_image_topic,
                    "raw_field_image_topic": flir_input_topic,
                    "cam4_image_topic": cam4_input_topic,
                    "cam4_overlay_image_topic": cam4_overlay_image_topic,
                    "composite_image_topic": composite_image_topic,
                    "cam4_semantics_topic": cam4_semantics_topic,
                    "cam3_tool_observations_topic": cam3_tool_observations_topic,
                    "cam4_tool_observations_topic": cam4_tool_observations_topic,
                    "cam3_tool_observations_expected_model_version": (
                        cam3_tool_observations_expected_model_version
                    ),
                    "cam4_tool_observations_expected_model_version": (
                        cam4_tool_observations_expected_model_version
                    ),
                    "allow_legacy_cam4_semantics_fallback": ParameterValue(
                        allow_legacy_cam4_semantics_fallback,
                        value_type=bool,
                    ),
                    "require_field_image": ParameterValue(
                        require_field_image,
                        value_type=bool,
                    ),
                    "enable_text_only_dialogue": ParameterValue(
                        procedure_dialogue_vlm_enabled,
                        value_type=bool,
                    ),
                    "require_rfdetr_applied_field_image": ParameterValue(
                        require_rfdetr_applied_field_image,
                        value_type=bool,
                    ),
                    "require_rfdetr_cam4_overlay": ParameterValue(
                        require_rfdetr_cam4_overlay,
                        value_type=bool,
                    ),
                    "output_prefix": PythonExpression(
                        ["'/vlm' if '", vlm_mode, "' == 'real' else '/vlm_real'"]
                    ),
                    "context_prefix": PythonExpression(
                        [
                            "'/context' if '",
                            vlm_mode,
                            "' == 'real' else '/context_real'",
                        ]
                    ),
                }
            ],
            output="screen",
        ),
        Node(
            package="phase_estimator",
            executable="phase_estimator",
            name="phase_estimator",
            condition=IfCondition(
                PythonExpression(
                    [
                        "'",
                        procedure_phase_inference_enabled,
                        "'.lower() == 'true' and '",
                        validation_mode,
                        "' != 'bt_twin'",
                    ]
                )
            ),
            parameters=[{"spec_dir": spec_dir}],
            output="screen",
        ),
    ]


def _generate_perception_launch_description() -> LaunchDescription:
    """Build the independently restartable perception/VLM owner directly."""

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
                description="Mode-level perception and VLM defaults.",
            ),
            OpaqueFunction(
                function=lambda context: _perception_profile_actions(context)
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
                "procedure_dialogue_vlm_enabled",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_CAPABILITY_DIALOGUE_VLM", default_value="true"
                ),
            ),
            DeclareLaunchArgument(
                "procedure_perception_enabled",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_CAPABILITY_PERCEPTION", default_value="true"
                ),
            ),
            DeclareLaunchArgument(
                "procedure_phase_inference_enabled",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_CAPABILITY_PHASE_INFERENCE", default_value="true"
                ),
            ),
            DeclareLaunchArgument("input_profile", default_value="simulation"),
            DeclareLaunchArgument("validation_mode", default_value="bt_twin"),
            DeclareLaunchArgument("vlm_mode", default_value="real"),
            DeclareLaunchArgument("vlm_base_url", default_value="http://127.0.0.1:8080"),
            DeclareLaunchArgument("vlm_provider_id", default_value="ninfer"),
            DeclareLaunchArgument("vlm_model_id", default_value="qwen3.6-35b-a3b"),
            DeclareLaunchArgument("vlm_api_mode", default_value="openai_compat"),
            DeclareLaunchArgument("vlm_publish_period_sec", default_value="1.0"),
            DeclareLaunchArgument("vlm_max_output_tokens", default_value="384"),
            DeclareLaunchArgument("vlm_generation_seed", default_value="0"),
            DeclareLaunchArgument("vlm_response_format", default_value="json_schema"),
            DeclareLaunchArgument("vlm_reasoning_effort", default_value="none"),
            DeclareLaunchArgument("vlm_response_mode", default_value="live"),
            DeclareLaunchArgument("vlm_context_mode", default_value="actor_log"),
            DeclareLaunchArgument("vlm_image_stale_sec", default_value="3.0"),
            DeclareLaunchArgument(
                "vlm_require_source_frame_timestamp", default_value="false"
            ),
            DeclareLaunchArgument(
                "vlm_model_input_max_source_lag_sec", default_value="0.0"
            ),
            DeclareLaunchArgument(
                "vlm_model_input_max_source_future_skew_sec", default_value="1.0"
            ),
            DeclareLaunchArgument(
                "field_snapshot_url",
                default_value=EnvironmentVariable("FIELD_SNAPSHOT_URL", default_value=""),
            ),
            DeclareLaunchArgument("enable_rfdetr_perception", default_value="false"),
            DeclareLaunchArgument(
                "perception_backend",
                default_value=EnvironmentVariable("PERCEPTION_BACKEND", default_value="local"),
            ),
            DeclareLaunchArgument(
                "perception_provider",
                default_value=EnvironmentVariable("PERCEPTION_PROVIDER", default_value=""),
            ),
            DeclareLaunchArgument(
                "perception_location",
                default_value=EnvironmentVariable("PERCEPTION_LOCATION", default_value=""),
            ),
            DeclareLaunchArgument(
                "perception_endpoint",
                default_value=EnvironmentVariable("PERCEPTION_ENDPOINT", default_value=""),
            ),
            DeclareLaunchArgument(
                "rfdetr_service_url",
                default_value=EnvironmentVariable(
                    "RFDETR_SERVICE_URL", default_value="http://127.0.0.1:8010"
                ),
            ),
            DeclareLaunchArgument(
                "pnu_service_url",
                default_value=EnvironmentVariable("PNU_SERVICE_URL", default_value=""),
            ),
            DeclareLaunchArgument(
                "pnu_api_token_file",
                default_value=EnvironmentVariable(
                    "PNU_CLIENT_API_TOKEN_FILE", default_value=""
                ),
            ),
            DeclareLaunchArgument(
                "pnu_allow_insecure_remote_http",
                default_value=EnvironmentVariable(
                    "PNU_ALLOW_INSECURE_REMOTE_HTTP", default_value="false"
                ),
            ),
            DeclareLaunchArgument(
                "pnu_allow_unauthenticated_remote",
                default_value=EnvironmentVariable(
                    "PNU_ALLOW_UNAUTHENTICATED_REMOTE", default_value="false"
                ),
            ),
            DeclareLaunchArgument(
                "pnu_expected_model_digests_json",
                default_value=EnvironmentVariable(
                    "PNU_EXPECTED_MODEL_DIGESTS_JSON", default_value="{}"
                ),
            ),
            DeclareLaunchArgument(
                "pnu_expected_tool_support_plane_config_version",
                default_value=EnvironmentVariable(
                    "PNU_EXPECTED_TOOL_SUPPORT_PLANE_CONFIG_VERSION", default_value=""
                ),
            ),
            DeclareLaunchArgument(
                "pnu_depth_scale_m_per_unit",
                default_value=EnvironmentVariable(
                    "PNU_DEPTH_SCALE_M_PER_UNIT", default_value="0.0"
                ),
            ),
            DeclareLaunchArgument(
                "pnu_depth_scale_validated",
                default_value=EnvironmentVariable(
                    "PNU_DEPTH_SCALE_VALIDATED", default_value="false"
                ),
            ),
            DeclareLaunchArgument(
                "pnu_depth_alignment_validated",
                default_value=EnvironmentVariable(
                    "PNU_DEPTH_ALIGNMENT_VALIDATED", default_value="false"
                ),
            ),
            DeclareLaunchArgument(
                "pnu_depth_alignment_id",
                default_value=EnvironmentVariable("PNU_DEPTH_ALIGNMENT_ID", default_value=""),
            ),
            DeclareLaunchArgument(
                "flir_input_topic",
                default_value="/surgery/images/flir/compressed",
            ),
            DeclareLaunchArgument(
                "cam4_input_topic",
                default_value="/surgery/images/cam4/compressed",
            ),
            DeclareLaunchArgument(
                "cam3_input_topic",
                default_value=EnvironmentVariable("CAM3_INPUT_TOPIC", default_value=""),
            ),
            DeclareLaunchArgument(
                "field_image_topic",
                default_value="/surgery/images/field/compressed",
            ),
            DeclareLaunchArgument(
                "rfdetr_flir_output_topic",
                default_value="/surgery/images/field/compressed",
            ),
            DeclareLaunchArgument(
                "composite_image_topic",
                default_value="/surgery/images/vlm/composite/compressed",
            ),
            DeclareLaunchArgument(
                "flir_overlay_image_topic",
                default_value="/surgery/images/flir/segmentation_overlay/compressed",
            ),
            DeclareLaunchArgument(
                "cam4_overlay_image_topic",
                default_value="/surgery/images/cam4/detection_overlay/compressed",
            ),
            DeclareLaunchArgument(
                "cam3_overlay_image_topic",
                default_value=EnvironmentVariable(
                    "CAM3_RFDETR_OVERLAY_TOPIC",
                    default_value=(
                        "/taskplanner/internal/rfdetr/cam3/detection_overlay/compressed"
                    ),
                ),
            ),
            DeclareLaunchArgument("cam4_semantics_topic", default_value=""),
            DeclareLaunchArgument("cam3_tool_observations_topic", default_value=""),
            DeclareLaunchArgument("cam4_tool_observations_topic", default_value=""),
            DeclareLaunchArgument(
                "rfdetr_bridge_cam3_tool_observations_topic",
                default_value="/taskplanner/internal/rfdetr/cam_3/tool/observations",
            ),
            DeclareLaunchArgument(
                "rfdetr_bridge_cam4_tool_observations_topic",
                default_value="/taskplanner/internal/rfdetr/cam_4/tool/observations",
            ),
            DeclareLaunchArgument(
                "cam3_tool_observations_expected_model_version", default_value=""
            ),
            DeclareLaunchArgument(
                "cam4_tool_observations_expected_model_version", default_value=""
            ),
            DeclareLaunchArgument(
                "allow_legacy_cam4_semantics_fallback", default_value="true"
            ),
            DeclareLaunchArgument("require_field_image", default_value="false"),
            DeclareLaunchArgument(
                "require_rfdetr_applied_field_image", default_value="false"
            ),
            DeclareLaunchArgument("require_rfdetr_cam4_overlay", default_value="false"),
            DeclareLaunchArgument(
                "cv_contract_status_topic",
                default_value="/integration/cv_contract/status",
            ),
            DeclareLaunchArgument(
                "cv_cam4_rgb_topic",
                default_value="/synced/cam_4/color/image_raw/compressed",
            ),
            DeclareLaunchArgument(
                "cv_cam4_rgb_alias_topic",
                default_value="/surgery/images/cam4/compressed",
            ),
            DeclareLaunchArgument(
                "cv_cam4_camera_info_topic",
                default_value="/synced/cam_4/color/camera_info",
            ),
            DeclareLaunchArgument(
                "cv_cam4_native_depth_compressed_topic",
                default_value="/synced/cam_4/depth/image_rect_raw/compressedDepth",
            ),
            DeclareLaunchArgument(
                "cv_cam4_depth_camera_info_topic",
                default_value="/synced/cam_4/depth/camera_info",
            ),
            DeclareLaunchArgument(
                "cv_cam4_depth_to_color_extrinsics_topic",
                default_value="/synced/cam_4/extrinsics/depth_to_color",
            ),
            DeclareLaunchArgument(
                "cv_cam4_aligned_depth_compressed_topic",
                default_value=(
                    "/synced/cam_4/aligned_depth_to_color/image_raw/compressedDepth"
                ),
            ),
            DeclareLaunchArgument(
                "cv_cam4_aligned_depth_camera_info_topic",
                default_value="/synced/cam_4/aligned_depth_to_color/camera_info",
            ),
            DeclareLaunchArgument(
                "cv_handover_tray_rgb_topic",
                default_value="/surgery/images/tray/compressed",
            ),
            DeclareLaunchArgument(
                "cv_handover_tray_camera_info_topic",
                default_value="/surgery/cameras/tray/color/camera_info",
            ),
            DeclareLaunchArgument(
                "cv_handover_tray_aligned_depth_topic",
                default_value="/surgery/cameras/tray/aligned_depth",
            ),
            OpaqueFunction(function=resolve_launch_perception),
            *_perception_actions(),
        ]
    )

