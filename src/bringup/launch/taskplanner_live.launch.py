"""Bring up the fail-closed external Taskplanner integration runtime."""

from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument,
    IncludeLaunchDescription,
    OpaqueFunction,
)
from launch.conditions import IfCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import (
    EnvironmentVariable,
    LaunchConfiguration,
    PathJoinSubstitution,
    PythonExpression,
)
from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare

from bringup.perception_config import resolve_launch_perception


def _env(name: str, default: str) -> EnvironmentVariable:
    return EnvironmentVariable(name, default_value=default)


def generate_launch_description() -> LaunchDescription:
    vlm_mode = _env("VLM_MODE", "real")
    publish_shared_state = LaunchConfiguration("publish_shared_state")
    publish_shared_free_text = LaunchConfiguration("publish_shared_free_text")
    publish_camera_aliases = LaunchConfiguration("publish_camera_aliases")
    publish_flir_while_idle = LaunchConfiguration("publish_flir_while_idle")
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
    pnu_depth_scale_m_per_unit = LaunchConfiguration(
        "pnu_depth_scale_m_per_unit"
    )
    pnu_depth_scale_validated = LaunchConfiguration(
        "pnu_depth_scale_validated"
    )
    pnu_depth_alignment_validated = LaunchConfiguration(
        "pnu_depth_alignment_validated"
    )
    pnu_depth_alignment_id = LaunchConfiguration("pnu_depth_alignment_id")
    default_bundle = LaunchConfiguration("default_bundle")
    flir_input_topic = _env(
        "FLIR_INPUT_TOPIC",
        "/synced/flir/color/image_raw/compressed",
    )
    cam4_input_topic = _env(
        "CAM4_INPUT_TOPIC",
        "/synced/cam_4/color/image_raw/compressed",
    )
    perception_enabled = PythonExpression(
        [
            "'",
            perception_provider,
            "' == 'builtin_rfdetr' and '",
            _env("ENABLE_RFDETR_PERCEPTION", "true"),
            "'.lower() in ('true', '1', 'yes') and '",
            vlm_mode,
            "' in ('real', 'dual')",
        ]
    )
    base_launch = PythonLaunchDescriptionSource(
        PathJoinSubstitution(
            [FindPackageShare("bringup"), "launch", "taskplanner_mock.launch.py"]
        )
    )
    return LaunchDescription(
        [
            DeclareLaunchArgument(
                "default_bundle",
                default_value=_env("TASKPLANNER_DEFAULT_BUNDLE", "thyroidectomy"),
                description="Procedure bundle selected for the live runtime.",
            ),
            DeclareLaunchArgument(
                "publish_shared_state",
                default_value="true",
                description=(
                    "Publish the curated read-only shared surgical state on "
                    "/surgery/* topics."
                ),
            ),
            DeclareLaunchArgument(
                "publish_shared_free_text",
                default_value=_env("PUBLISH_SHARED_FREE_TEXT", "false"),
                description=(
                    "Publish public ASR transcript and VLM summary text only "
                    "after deployment privacy review."
                ),
            ),
            DeclareLaunchArgument(
                "publish_camera_aliases",
                default_value="true",
                description=(
                    "Expose the external FLIR and CAM4 compressed streams on "
                    "stable /surgery/images/* aliases."
                ),
            ),
            DeclareLaunchArgument(
                "publish_flir_while_idle",
                default_value=_env("PUBLISH_FLIR_WHILE_IDLE", "false"),
                description=(
                    "Allow only the read-only public FLIR alias before a "
                    "procedure run becomes active. CAM4 remains gated."
                ),
            ),
            DeclareLaunchArgument(
                "perception_backend",
                default_value=_env("PERCEPTION_BACKEND", "local"),
                description=(
                    "local runs the built-in RF-DETR bridge; external reserves "
                    "the CV-team contract; disabled runs neither backend."
                ),
            ),
            DeclareLaunchArgument(
                "perception_provider",
                default_value=_env("PERCEPTION_PROVIDER", ""),
                description=(
                    "Explicit provider axis. Empty maps the legacy "
                    "PERCEPTION_BACKEND alias."
                ),
            ),
            DeclareLaunchArgument(
                "perception_location",
                default_value=_env("PERCEPTION_LOCATION", ""),
                description=(
                    "Worker placement: local or remote. No automatic failover."
                ),
            ),
            DeclareLaunchArgument(
                "perception_endpoint",
                default_value=_env("PERCEPTION_ENDPOINT", ""),
                description=(
                    "Explicit worker endpoint. Empty maps RFDETR_SERVICE_URL."
                ),
            ),
            DeclareLaunchArgument(
                "rfdetr_service_url",
                default_value=_env(
                    "RFDETR_SERVICE_URL",
                    "http://127.0.0.1:8010",
                ),
                description="Legacy alias for perception_endpoint.",
            ),
            DeclareLaunchArgument(
                "pnu_service_url",
                default_value=_env("PNU_SERVICE_URL", ""),
                description=(
                    "Optional PNU Hand/Tool/Blood endpoint alias. Remote "
                    "placement requires a non-loopback value."
                ),
            ),
            DeclareLaunchArgument(
                "pnu_api_token_file",
                default_value=_env("PNU_CLIENT_API_TOKEN_FILE", ""),
            ),
            DeclareLaunchArgument(
                "pnu_allow_insecure_remote_http",
                default_value=_env(
                    "PNU_ALLOW_INSECURE_REMOTE_HTTP",
                    "false",
                ),
                description=(
                    "Development-only opt-in for HTTP to a non-loopback PNU "
                    "worker. Remote endpoints require HTTPS by default."
                ),
            ),
            DeclareLaunchArgument(
                "pnu_allow_unauthenticated_remote",
                default_value=_env(
                    "PNU_ALLOW_UNAUTHENTICATED_REMOTE",
                    "false",
                ),
            ),
            DeclareLaunchArgument(
                "pnu_expected_model_digests_json",
                default_value=_env("PNU_EXPECTED_MODEL_DIGESTS_JSON", "{}"),
            ),
            DeclareLaunchArgument(
                "pnu_expected_tool_support_plane_config_version",
                default_value=_env(
                    "PNU_EXPECTED_TOOL_SUPPORT_PLANE_CONFIG_VERSION",
                    "",
                ),
            ),
            DeclareLaunchArgument(
                "pnu_depth_scale_m_per_unit",
                default_value=_env("PNU_DEPTH_SCALE_M_PER_UNIT", "0.0"),
            ),
            DeclareLaunchArgument(
                "pnu_depth_scale_validated",
                default_value=_env("PNU_DEPTH_SCALE_VALIDATED", "false"),
            ),
            DeclareLaunchArgument(
                "pnu_depth_alignment_validated",
                default_value=_env("PNU_DEPTH_ALIGNMENT_VALIDATED", "false"),
            ),
            DeclareLaunchArgument(
                "pnu_depth_alignment_id",
                default_value=_env("PNU_DEPTH_ALIGNMENT_ID", ""),
            ),
            OpaqueFunction(function=resolve_launch_perception),
            IncludeLaunchDescription(
                base_launch,
                launch_arguments={
                    "enable_rosbridge": "true",
                    "rosbridge_port": _env("ROSBRIDGE_PORT", "9090"),
                    "rosbridge_address": _env("ROSBRIDGE_ADDRESS", "127.0.0.1"),
                    "rosbridge_service_timeout": _env(
                        "ROSBRIDGE_SERVICE_TIMEOUT",
                        "30.0",
                    ),
                    "input_profile": "external",
                    "default_bundle": default_bundle,
                    "publish_shared_state": publish_shared_state,
                    "publish_shared_free_text": publish_shared_free_text,
                    "execution_backend": "external",
                    "execution_contract": "direct",
                    "speech_input_mode": "sentence_text",
                    "sentence_input_topic": _env(
                        "SENTENCE_INPUT_TOPIC",
                        "/sensors/surgeon/sentence",
                    ),
                    # The speech adapter remains the only ASR owner.  This
                    # text-only VLM receives its final transcript downstream
                    # and falls back deterministically if the local model is
                    # unavailable or returns an invalid closed-schema answer.
                    "retractor_voice_normalization_enabled": "true",
                    "retractor_voice_interpreter_mode": _env(
                        "RETRACTOR_VOICE_INTERPRETER_MODE",
                        "vlm_with_fallback",
                    ),
                    "retractor_voice_vlm_base_url": _env(
                        "RETRACTOR_VOICE_VLM_BASE_URL",
                        _env("VLM_BASE_URL", "http://127.0.0.1:8001"),
                    ),
                    "retractor_voice_vlm_model_id": _env(
                        "RETRACTOR_VOICE_VLM_MODEL_ID",
                        _env(
                            "VLM_MODEL_ID",
                            "unsloth/gemma-4-E4B-it-NVFP4",
                        ),
                    ),
                    "retractor_voice_vlm_api_key": _env(
                        "RETRACTOR_VOICE_VLM_API_KEY",
                        _env("VLM_API_KEY", ""),
                    ),
                    "retractor_voice_vlm_timeout_sec": _env(
                        "RETRACTOR_VOICE_VLM_TIMEOUT_SEC", "2.0"
                    ),
                    # Natural-language resolver: short, fully-grounded
                    # commands stay local for latency; models choose only an
                    # explicit selector-required candidate ID.  No raw
                    # transcript reaches execution.
                    "voice_command_selector_mode": _env(
                        "VOICE_COMMAND_SELECTOR_MODE", "openai_compatible"
                    ),
                    "voice_command_selector_endpoint": PythonExpression(
                        [
                            "'",
                            _env("VOICE_COMMAND_SELECTOR_ENDPOINT", ""),
                            "' if '",
                            _env("VOICE_COMMAND_SELECTOR_ENDPOINT", ""),
                            "' else '",
                            _env("VLM_BASE_URL", "http://127.0.0.1:8001"),
                            "/v1/chat/completions'",
                        ]
                    ),
                    "voice_command_selector_model": _env(
                        "VOICE_COMMAND_SELECTOR_MODEL",
                        _env("VLM_MODEL_ID", "unsloth/gemma-4-E4B-it-NVFP4"),
                    ),
                    "voice_command_selector_timeout_sec": _env(
                        "VOICE_COMMAND_SELECTOR_TIMEOUT_SEC", "0.35"
                    ),
                    "vlm_mode": vlm_mode,
                    "vlm_base_url": _env(
                        "VLM_BASE_URL",
                        "http://127.0.0.1:8001",
                    ),
                    "vlm_provider_id": _env("VLM_PROVIDER_ID", "vllm"),
                    "vlm_model_id": _env(
                        "VLM_MODEL_ID",
                        "unsloth/gemma-4-E4B-it-NVFP4",
                    ),
                    "vlm_api_mode": _env("VLM_API_MODE", "openai_compat"),
                    "vlm_publish_period_sec": _env(
                        "VLM_PUBLISH_PERIOD_SEC",
                        "1.0",
                    ),
                    "vlm_image_stale_sec": _env("VLM_IMAGE_STALE_SEC", "3.0"),
                    "vlm_max_output_tokens": _env(
                        "VLM_MAX_OUTPUT_TOKENS",
                        "320",
                    ),
                    "vlm_generation_seed": _env("VLM_GENERATION_SEED", "0"),
                    "vlm_response_format": _env(
                        "VLM_RESPONSE_FORMAT",
                        "json_schema",
                    ),
                    "vlm_reasoning_effort": _env(
                        "VLM_REASONING_EFFORT",
                        "none",
                    ),
                    "vlm_context_mode": _env("VLM_CONTEXT_MODE", "actor_log"),
                    "surgeon_actor_mode": "none",
                    "enable_no_image_camera": "false",
                    "enable_synthetic_scene_camera": "false",
                    "enable_rfdetr_perception": perception_enabled,
                    "perception_backend": perception_backend,
                    "perception_provider": perception_provider,
                    "perception_location": perception_location,
                    "perception_endpoint": perception_endpoint,
                    "rfdetr_service_url": perception_endpoint,
                    "pnu_api_token_file": pnu_api_token_file,
                    "pnu_allow_insecure_remote_http": (
                        pnu_allow_insecure_remote_http
                    ),
                    "pnu_allow_unauthenticated_remote": (
                        pnu_allow_unauthenticated_remote
                    ),
                    "pnu_expected_model_digests_json": (
                        pnu_expected_model_digests_json
                    ),
                    "pnu_expected_tool_support_plane_config_version": (
                        pnu_expected_tool_support_plane_config_version
                    ),
                    "pnu_depth_scale_m_per_unit": pnu_depth_scale_m_per_unit,
                    "pnu_depth_scale_validated": pnu_depth_scale_validated,
                    "pnu_depth_alignment_validated": (
                        pnu_depth_alignment_validated
                    ),
                    "pnu_depth_alignment_id": pnu_depth_alignment_id,
                    "flir_input_topic": flir_input_topic,
                    "cam4_input_topic": cam4_input_topic,
                    "field_image_topic": _env(
                        "SEGMENTED_FLIR_TOPIC",
                        "/surgery/images/flir/segmented/compressed",
                    ),
                    "cam4_overlay_image_topic": _env(
                        "CAM4_OVERLAY_TOPIC",
                        "/surgery/images/cam4/detection_overlay/compressed",
                    ),
                    "cam4_semantics_topic": _env(
                        "CAM4_SEMANTICS_TOPIC",
                        "/surgery/perception/cam4/semantics/json",
                    ),
                    "require_field_image": perception_enabled,
                    "require_integration_preflight": "true",
                    "preflight_require_perception": _env(
                        "REQUIRE_PERCEPTION_ON_START",
                        "false",
                    ),
                    "preflight_require_metric_3d": _env(
                        "PNU_REQUIRE_METRIC_3D_ON_START",
                        "false",
                    ),
                    "cv_contract_status_topic": _env(
                        "CV_CONTRACT_STATUS_TOPIC",
                        "/integration/cv_contract/status",
                    ),
                    "cv_cam4_rgb_topic": _env(
                        "CV_CAM4_RGB_TOPIC",
                        "/synced/cam_4/color/image_raw/compressed",
                    ),
                    "cv_cam4_rgb_alias_topic": _env(
                        "CV_CAM4_RGB_ALIAS_TOPIC",
                        "/surgery/images/cam4/compressed",
                    ),
                    "cv_cam4_camera_info_topic": _env(
                        "CV_CAM4_CAMERA_INFO_TOPIC",
                        "/synced/cam_4/color/camera_info",
                    ),
                    "cv_cam4_native_depth_compressed_topic": _env(
                        "CV_CAM4_NATIVE_DEPTH_COMPRESSED_TOPIC",
                        "/synced/cam_4/depth/image_rect_raw/compressedDepth",
                    ),
                    "cv_cam4_depth_camera_info_topic": _env(
                        "CV_CAM4_DEPTH_CAMERA_INFO_TOPIC",
                        "/synced/cam_4/depth/camera_info",
                    ),
                    "cv_cam4_depth_to_color_extrinsics_topic": _env(
                        "CV_CAM4_DEPTH_TO_COLOR_EXTRINSICS_TOPIC",
                        "/synced/cam_4/extrinsics/depth_to_color",
                    ),
                    "cv_cam4_aligned_depth_compressed_topic": _env(
                        "CV_CAM4_ALIGNED_DEPTH_COMPRESSED_TOPIC",
                        "/synced/cam_4/aligned_depth_to_color/"
                        "image_raw/compressedDepth",
                    ),
                    "cv_cam4_aligned_depth_camera_info_topic": _env(
                        "CV_CAM4_ALIGNED_DEPTH_CAMERA_INFO_TOPIC",
                        "/synced/cam_4/aligned_depth_to_color/camera_info",
                    ),
                    "cv_handover_tray_rgb_topic": _env(
                        "CV_HANDOVER_TRAY_RGB_TOPIC",
                        "/surgery/images/tray/compressed",
                    ),
                    "cv_handover_tray_camera_info_topic": _env(
                        "CV_HANDOVER_TRAY_CAMERA_INFO_TOPIC",
                        "/surgery/cameras/tray/color/camera_info",
                    ),
                    "cv_handover_tray_aligned_depth_topic": _env(
                        "CV_HANDOVER_TRAY_ALIGNED_DEPTH_TOPIC",
                        "/surgery/cameras/tray/aligned_depth",
                    ),
                }.items(),
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
                        "default_bundle": default_bundle,
                        "publish_flir_while_idle": publish_flir_while_idle,
                    }
                ],
                output="screen",
            ),
        ]
    )
