"""Bring up the fail-closed external Taskplanner integration runtime."""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
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


def _env(name: str, default: str) -> EnvironmentVariable:
    return EnvironmentVariable(name, default_value=default)


def generate_launch_description() -> LaunchDescription:
    vlm_mode = _env("VLM_MODE", "real")
    publish_shared_state = LaunchConfiguration("publish_shared_state")
    publish_shared_free_text = LaunchConfiguration("publish_shared_free_text")
    publish_camera_aliases = LaunchConfiguration("publish_camera_aliases")
    perception_backend = LaunchConfiguration("perception_backend")
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
            perception_backend,
            "' == 'local' and '",
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
                "perception_backend",
                default_value=_env("PERCEPTION_BACKEND", "local"),
                description=(
                    "local runs the built-in RF-DETR bridge; external reserves "
                    "the CV-team contract; disabled runs neither backend."
                ),
            ),
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
                    "rfdetr_service_url": _env(
                        "RFDETR_SERVICE_URL",
                        "http://127.0.0.1:8010",
                    ),
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
                    "cv_cam4_aligned_depth_topic": _env(
                        "CV_CAM4_ALIGNED_DEPTH_TOPIC",
                        "/synced/cam_4/depth/image_rect_raw",
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
                    }
                ],
                output="screen",
            ),
        ]
    )
