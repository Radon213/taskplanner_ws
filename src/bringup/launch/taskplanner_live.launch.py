"""Bring up the fail-closed external Taskplanner integration runtime."""

from launch import LaunchDescription
from launch.actions import IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import EnvironmentVariable, PathJoinSubstitution, PythonExpression
from launch_ros.substitutions import FindPackageShare


def _env(name: str, default: str) -> EnvironmentVariable:
    return EnvironmentVariable(name, default_value=default)


def generate_launch_description() -> LaunchDescription:
    vlm_mode = _env("VLM_MODE", "real")
    perception_enabled = PythonExpression(
        ["'", vlm_mode, "' in ('real', 'dual')"]
    )
    base_launch = PythonLaunchDescriptionSource(
        PathJoinSubstitution(
            [FindPackageShare("bringup"), "launch", "taskplanner_mock.launch.py"]
        )
    )
    return LaunchDescription(
        [
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
                    "execution_backend": "external",
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
                    "rfdetr_service_url": _env(
                        "RFDETR_SERVICE_URL",
                        "http://127.0.0.1:8010",
                    ),
                    "flir_input_topic": _env(
                        "FLIR_INPUT_TOPIC",
                        "/surgery/images/flir/compressed",
                    ),
                    "cam4_input_topic": _env(
                        "CAM4_INPUT_TOPIC",
                        "/surgery/images/cam4/compressed",
                    ),
                    "field_image_topic": _env(
                        "SEGMENTED_FLIR_TOPIC",
                        "/surgery/images/flir/segmented/compressed",
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
                }.items(),
            )
        ]
    )
