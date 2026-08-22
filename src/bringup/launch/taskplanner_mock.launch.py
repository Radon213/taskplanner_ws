"""Bring up the configurable Taskplanner runtime."""

from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument,
    ExecuteProcess,
    OpaqueFunction,
    SetLaunchConfiguration,
)
from launch.conditions import IfCondition
from launch.substitutions import (
    EnvironmentVariable,
    LaunchConfiguration,
    PathJoinSubstitution,
    PythonExpression,
)
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue
from launch_ros.substitutions import FindPackageShare

from bringup.perception_config import resolve_launch_perception


def _bed_robot_contract_configuration(context):
    bundle_id = LaunchConfiguration("default_bundle").perform(context).strip()
    if bundle_id in {"thyroidectomy", "thyroidectomy_demo"}:
        procedure_type = "thyroidectomy"
    elif bundle_id == "nephrectomy":
        procedure_type = "nephrectomy"
    else:
        procedure_type = ""
    return [
        SetLaunchConfiguration(
            "bed_robot_contract_enabled",
            "true" if procedure_type else "false",
        ),
        SetLaunchConfiguration(
            "bed_robot_contract_procedure_type",
            procedure_type,
        ),
    ]


def generate_launch_description() -> LaunchDescription:
    spec_dir = LaunchConfiguration("spec_dir")
    default_bundle = LaunchConfiguration("default_bundle")
    publish_shared_state = LaunchConfiguration("publish_shared_state")
    publish_shared_free_text = LaunchConfiguration("publish_shared_free_text")
    enable_rosbridge = LaunchConfiguration("enable_rosbridge")
    rosbridge_port = LaunchConfiguration("rosbridge_port")
    rosbridge_address = LaunchConfiguration("rosbridge_address")
    rosbridge_service_timeout = LaunchConfiguration("rosbridge_service_timeout")
    input_profile = LaunchConfiguration("input_profile")
    execution_backend = LaunchConfiguration("execution_backend")
    execution_contract = LaunchConfiguration("execution_contract")
    bed_robot_contract_enabled = LaunchConfiguration(
        "bed_robot_contract_enabled"
    )
    bed_robot_contract_procedure_type = LaunchConfiguration(
        "bed_robot_contract_procedure_type"
    )
    speech_input_mode = LaunchConfiguration("speech_input_mode")
    sentence_input_topic = LaunchConfiguration("sentence_input_topic")
    speech_min_confidence = LaunchConfiguration("speech_min_confidence")
    speech_max_age_sec = LaunchConfiguration("speech_max_age_sec")
    speech_source_timeout_sec = LaunchConfiguration("speech_source_timeout_sec")
    retractor_voice_normalization_enabled = LaunchConfiguration(
        "retractor_voice_normalization_enabled"
    )
    retractor_voice_interpreter_mode = LaunchConfiguration(
        "retractor_voice_interpreter_mode"
    )
    retractor_voice_vlm_base_url = LaunchConfiguration("retractor_voice_vlm_base_url")
    retractor_voice_vlm_model_id = LaunchConfiguration("retractor_voice_vlm_model_id")
    retractor_voice_vlm_api_key = LaunchConfiguration("retractor_voice_vlm_api_key")
    retractor_voice_vlm_timeout_sec = LaunchConfiguration(
        "retractor_voice_vlm_timeout_sec"
    )
    voice_command_selector_mode = LaunchConfiguration("voice_command_selector_mode")
    voice_command_selector_endpoint = LaunchConfiguration(
        "voice_command_selector_endpoint"
    )
    voice_command_selector_model = LaunchConfiguration("voice_command_selector_model")
    voice_command_selector_timeout_sec = LaunchConfiguration(
        "voice_command_selector_timeout_sec"
    )
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
    surgeon_actor_mode = LaunchConfiguration("surgeon_actor_mode")
    actor_base_url = LaunchConfiguration("actor_base_url")
    actor_provider_id = LaunchConfiguration("actor_provider_id")
    actor_model_id = LaunchConfiguration("actor_model_id")
    actor_response_format = LaunchConfiguration("actor_response_format")
    actor_reasoning_effort = LaunchConfiguration("actor_reasoning_effort")
    validation_mode = LaunchConfiguration("validation_mode")
    enable_no_image_camera = LaunchConfiguration("enable_no_image_camera")
    enable_synthetic_scene_camera = LaunchConfiguration("enable_synthetic_scene_camera")
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
    flir_input_topic = LaunchConfiguration("flir_input_topic")
    cam4_input_topic = LaunchConfiguration("cam4_input_topic")
    field_image_topic = LaunchConfiguration("field_image_topic")
    cam4_overlay_image_topic = LaunchConfiguration("cam4_overlay_image_topic")
    cam4_semantics_topic = LaunchConfiguration("cam4_semantics_topic")
    require_field_image = LaunchConfiguration("require_field_image")
    require_integration_preflight = LaunchConfiguration(
        "require_integration_preflight"
    )
    preflight_require_perception = LaunchConfiguration(
        "preflight_require_perception"
    )
    preflight_require_metric_3d = LaunchConfiguration(
        "preflight_require_metric_3d"
    )
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
    spec_default = PathJoinSubstitution(
        [FindPackageShare("procedure_spec"), "specs", default_bundle]
    )
    mock_vlm_enabled = PythonExpression(
        [
            "'",
            input_profile,
            "' == 'simulation' and ('",
            vlm_mode,
            "' == 'mock' or '",
            vlm_mode,
            "' == 'dual')",
        ]
    )
    real_vlm_enabled = PythonExpression(
        ["'", vlm_mode, "' == 'real' or '", vlm_mode, "' == 'dual'"]
    )
    rule_surgeon_actor_enabled = PythonExpression(
        [
            "'",
            input_profile,
            "' == 'simulation' and '",
            surgeon_actor_mode,
            "' == 'rule'",
        ]
    )
    llm_surgeon_actor_enabled = PythonExpression(
        [
            "'",
            input_profile,
            "' == 'simulation' and '",
            surgeon_actor_mode,
            "' == 'llm'",
        ]
    )
    no_image_camera_enabled = PythonExpression(
        [
            "'",
            input_profile,
            "' == 'simulation' and '",
            enable_no_image_camera,
            "'.lower() in ('true', '1', 'yes')",
        ]
    )
    synthetic_scene_camera_enabled = PythonExpression(
        [
            "'",
            input_profile,
            "' == 'simulation' and '",
            enable_synthetic_scene_camera,
            "'.lower() in ('true', '1', 'yes')",
        ]
    )
    mock_legacy_execution_enabled = PythonExpression(
        [
            "'",
            execution_backend,
            "' == 'mock' and ('",
            execution_contract,
            "' == 'legacy' or '",
            bed_robot_contract_enabled,
            "'.lower() != 'true')",
        ]
    )
    legacy_execution_bridge_enabled = PythonExpression(
        [
            "'",
            execution_contract,
            "' == 'legacy' or ('",
            execution_backend,
            "' == 'mock' and '",
            bed_robot_contract_enabled,
            "'.lower() != 'true')",
        ]
    )
    direct_execution_bridge_enabled = PythonExpression(
        [
            "'",
            execution_contract,
            "' == 'direct' and ('",
            execution_backend,
            "' != 'mock' or '",
            bed_robot_contract_enabled,
            "'.lower() == 'true')",
        ]
    )
    builtin_rfdetr_adapter_enabled = PythonExpression(
        [
            "'",
            perception_provider,
            "' == 'builtin_rfdetr' and '",
            enable_rfdetr_perception,
            "'.lower() in ('true', '1', 'yes')",
        ]
    )
    pnu_adapter_enabled = PythonExpression(
        ["'", perception_provider, "' == 'pnu_hand_blood'"]
    )
    mock_direct_contract_enabled = PythonExpression(
        [
            "'",
            execution_backend,
            "' == 'mock' and '",
            execution_contract,
            "' == 'direct' and '",
            bed_robot_contract_enabled,
            "'.lower() == 'true'",
        ]
    )
    robot_contract_profile = PathJoinSubstitution(
        [FindPackageShare("bringup"), "config", "robot_contract_success.yaml"]
    )

    rosbridge_process = ExecuteProcess(
        condition=IfCondition(enable_rosbridge),
        respawn=True,
        respawn_delay=5.0,
        cmd=[
            "bash",
            "-lc",
            PythonExpression(
                [
                    "'if ros2 pkg prefix rosbridge_server >/dev/null 2>&1; then "
                    "ros2 run rosbridge_server rosbridge_websocket --ros-args -p port:=' + str(",
                    rosbridge_port,
                    ") + ' -p address:=' + '",
                    rosbridge_address,
                    "' + ' -p default_call_service_timeout:=' + str(",
                    rosbridge_service_timeout,
                    ") + '; else echo \"[taskplanner_mock] rosbridge_server is not installed\"; fi'",
                ]
            ),
        ],
        output="screen",
    )
    rosapi_node = Node(
        package="rosapi",
        executable="rosapi_node",
        name="rosapi",
        condition=IfCondition(enable_rosbridge),
        parameters=[{"use_sim_time": False}],
        output="screen",
    )

    return LaunchDescription(
        [
            DeclareLaunchArgument("default_bundle", default_value="thyroidectomy"),
            DeclareLaunchArgument(
                "publish_shared_state",
                default_value=EnvironmentVariable(
                    "PUBLISH_SHARED_STATE",
                    default_value="true",
                ),
                description="Publish the curated read-only /surgery/* state gateway.",
            ),
            DeclareLaunchArgument(
                "publish_shared_free_text",
                default_value=EnvironmentVariable(
                    "PUBLISH_SHARED_FREE_TEXT",
                    default_value="false",
                ),
                description=(
                    "Publish public ASR transcript and VLM summary text. "
                    "Keep false unless the deployment has reviewed PHI handling."
                ),
            ),
            DeclareLaunchArgument("spec_dir", default_value=spec_default),
            DeclareLaunchArgument("enable_rosbridge", default_value="true"),
            DeclareLaunchArgument("rosbridge_port", default_value="9090"),
            DeclareLaunchArgument("rosbridge_address", default_value="127.0.0.1"),
            DeclareLaunchArgument("rosbridge_service_timeout", default_value="30.0"),
            DeclareLaunchArgument("input_profile", default_value="simulation"),
            DeclareLaunchArgument("execution_backend", default_value="mock"),
            DeclareLaunchArgument(
                "execution_contract",
                default_value="direct",
                description=(
                    "direct uses the focused public robot contracts; legacy is "
                    "retained only for humanoid compatibility."
                ),
            ),
            OpaqueFunction(function=_bed_robot_contract_configuration),
            DeclareLaunchArgument("speech_input_mode", default_value="utterance"),
            DeclareLaunchArgument(
                "sentence_input_topic",
                default_value="/sensors/surgeon/sentence",
            ),
            DeclareLaunchArgument("speech_min_confidence", default_value="0.55"),
            DeclareLaunchArgument("speech_max_age_sec", default_value="3.0"),
            DeclareLaunchArgument("speech_source_timeout_sec", default_value="5.0"),
            DeclareLaunchArgument(
                "retractor_voice_normalization_enabled", default_value="true"
            ),
            DeclareLaunchArgument(
                "retractor_voice_interpreter_mode",
                default_value="deterministic",
                choices=("deterministic", "vlm_with_fallback"),
                description=(
                    "Use the dedicated text-only VLM normalizer when configured; "
                    "deterministic normalization remains the safe fallback."
                ),
            ),
            DeclareLaunchArgument(
                "retractor_voice_vlm_base_url", default_value="http://127.0.0.1:8001"
            ),
            DeclareLaunchArgument("retractor_voice_vlm_model_id", default_value=""),
            DeclareLaunchArgument("retractor_voice_vlm_api_key", default_value=""),
            DeclareLaunchArgument(
                "retractor_voice_vlm_timeout_sec", default_value="2.0"
            ),
            DeclareLaunchArgument(
                "voice_command_selector_mode",
                default_value="deterministic",
                choices=("deterministic", "openai_compatible", "openai"),
                description=(
                    "Bounded candidate selector for natural-language voice "
                    "resolution; it never receives execution authority."
                ),
            ),
            DeclareLaunchArgument("voice_command_selector_endpoint", default_value=""),
            DeclareLaunchArgument("voice_command_selector_model", default_value=""),
            DeclareLaunchArgument(
                "voice_command_selector_timeout_sec", default_value="0.35"
            ),
            DeclareLaunchArgument("vlm_mode", default_value="real"),
            DeclareLaunchArgument("vlm_base_url", default_value="http://127.0.0.1:8001"),
            DeclareLaunchArgument("vlm_provider_id", default_value="vllm"),
            DeclareLaunchArgument("vlm_model_id", default_value="unsloth/gemma-4-E4B-it-NVFP4"),
            DeclareLaunchArgument("vlm_api_mode", default_value="openai_compat"),
            DeclareLaunchArgument("vlm_publish_period_sec", default_value="1.0"),
            DeclareLaunchArgument("vlm_max_output_tokens", default_value="320"),
            DeclareLaunchArgument("vlm_generation_seed", default_value="0"),
            DeclareLaunchArgument("vlm_response_format", default_value="json_schema"),
            DeclareLaunchArgument("vlm_reasoning_effort", default_value="none"),
            DeclareLaunchArgument("vlm_response_mode", default_value="live"),
            DeclareLaunchArgument("vlm_context_mode", default_value="actor_log"),
            DeclareLaunchArgument("vlm_image_stale_sec", default_value="3.0"),
            DeclareLaunchArgument("surgeon_actor_mode", default_value="llm"),
            DeclareLaunchArgument("actor_base_url", default_value="http://127.0.0.1:1234"),
            DeclareLaunchArgument("actor_provider_id", default_value="auto"),
            DeclareLaunchArgument("actor_model_id", default_value="google/gemma-4-12b-qat"),
            DeclareLaunchArgument("actor_response_format", default_value="json_schema"),
            DeclareLaunchArgument("actor_reasoning_effort", default_value="none"),
            DeclareLaunchArgument("validation_mode", default_value="bt_twin"),
            DeclareLaunchArgument("enable_no_image_camera", default_value="true"),
            DeclareLaunchArgument("enable_synthetic_scene_camera", default_value="false"),
            DeclareLaunchArgument("field_snapshot_url", default_value=""),
            DeclareLaunchArgument("enable_rfdetr_perception", default_value="false"),
            DeclareLaunchArgument(
                "perception_backend",
                default_value=EnvironmentVariable(
                    "PERCEPTION_BACKEND",
                    default_value="local",
                ),
                description=(
                    "local owns built-in RF-DETR outputs; external disables it "
                    "and reserves the CV-team contract; disabled owns neither."
                ),
            ),
            DeclareLaunchArgument(
                "perception_provider",
                default_value=EnvironmentVariable(
                    "PERCEPTION_PROVIDER",
                    default_value="",
                ),
                description=(
                    "Explicit provider axis: builtin_rfdetr, pnu_hand_blood, or "
                    "disabled. Empty preserves PERCEPTION_BACKEND compatibility."
                ),
            ),
            DeclareLaunchArgument(
                "perception_location",
                default_value=EnvironmentVariable(
                    "PERCEPTION_LOCATION",
                    default_value="",
                ),
                description=(
                    "Worker placement: local or remote. It requires an explicit "
                    "perception_provider; no automatic failover is performed."
                ),
            ),
            DeclareLaunchArgument(
                "perception_endpoint",
                default_value=EnvironmentVariable(
                    "PERCEPTION_ENDPOINT",
                    default_value="",
                ),
                description=(
                    "Single HTTP(S) worker endpoint. Empty preserves the legacy "
                    "rfdetr_service_url/RFDETR_SERVICE_URL alias."
                ),
            ),
            DeclareLaunchArgument(
                "rfdetr_service_url",
                default_value=EnvironmentVariable(
                    "RFDETR_SERVICE_URL",
                    default_value="http://127.0.0.1:8010",
                ),
            ),
            DeclareLaunchArgument(
                "pnu_service_url",
                default_value=EnvironmentVariable(
                    "PNU_SERVICE_URL",
                    default_value="",
                ),
                description=(
                    "Optional PNU Hand/Tool/Blood worker alias. Local PNU "
                    "selection defaults to http://127.0.0.1:8020; remote "
                    "selection requires an explicit non-loopback endpoint."
                ),
            ),
            DeclareLaunchArgument(
                "pnu_api_token_file",
                default_value=EnvironmentVariable(
                    "PNU_CLIENT_API_TOKEN_FILE",
                    default_value="",
                ),
            ),
            DeclareLaunchArgument(
                "pnu_allow_insecure_remote_http",
                default_value=EnvironmentVariable(
                    "PNU_ALLOW_INSECURE_REMOTE_HTTP",
                    default_value="false",
                ),
                description=(
                    "Development-only opt-in for HTTP to a non-loopback PNU "
                    "worker. Remote endpoints require HTTPS by default."
                ),
            ),
            DeclareLaunchArgument(
                "pnu_allow_unauthenticated_remote",
                default_value=EnvironmentVariable(
                    "PNU_ALLOW_UNAUTHENTICATED_REMOTE",
                    default_value="false",
                ),
            ),
            DeclareLaunchArgument(
                "pnu_expected_model_digests_json",
                default_value=EnvironmentVariable(
                    "PNU_EXPECTED_MODEL_DIGESTS_JSON",
                    default_value="{}",
                ),
            ),
            DeclareLaunchArgument(
                "pnu_expected_tool_support_plane_config_version",
                default_value=EnvironmentVariable(
                    "PNU_EXPECTED_TOOL_SUPPORT_PLANE_CONFIG_VERSION",
                    default_value="",
                ),
            ),
            DeclareLaunchArgument(
                "pnu_depth_scale_m_per_unit",
                default_value=EnvironmentVariable(
                    "PNU_DEPTH_SCALE_M_PER_UNIT",
                    default_value="0.0",
                ),
            ),
            DeclareLaunchArgument(
                "pnu_depth_scale_validated",
                default_value=EnvironmentVariable(
                    "PNU_DEPTH_SCALE_VALIDATED",
                    default_value="false",
                ),
            ),
            DeclareLaunchArgument(
                "pnu_depth_alignment_validated",
                default_value=EnvironmentVariable(
                    "PNU_DEPTH_ALIGNMENT_VALIDATED",
                    default_value="false",
                ),
            ),
            DeclareLaunchArgument(
                "pnu_depth_alignment_id",
                default_value=EnvironmentVariable(
                    "PNU_DEPTH_ALIGNMENT_ID",
                    default_value="",
                ),
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
                "field_image_topic",
                default_value="/surgery/images/field/compressed",
            ),
            DeclareLaunchArgument(
                "cam4_overlay_image_topic",
                default_value="/surgery/images/cam4/detection_overlay/compressed",
            ),
            DeclareLaunchArgument(
                "cam4_semantics_topic",
                default_value="",
            ),
            DeclareLaunchArgument("require_field_image", default_value="false"),
            DeclareLaunchArgument(
                "require_integration_preflight",
                default_value="false",
            ),
            DeclareLaunchArgument(
                "preflight_require_perception",
                default_value="false",
            ),
            DeclareLaunchArgument(
                "preflight_require_metric_3d",
                default_value="false",
            ),
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
                "cv_cam4_aligned_depth_compressed_topic",
                default_value=(
                    "/synced/cam_4/aligned_depth_to_color/"
                    "image_raw/compressedDepth"
                ),
            ),
            DeclareLaunchArgument(
                "cv_cam4_aligned_depth_camera_info_topic",
                default_value=(
                    "/synced/cam_4/aligned_depth_to_color/camera_info"
                ),
            ),
            DeclareLaunchArgument(
                "cv_cam4_native_depth_compressed_topic",
                default_value=(
                    "/synced/cam_4/depth/image_rect_raw/compressedDepth"
                ),
            ),
            DeclareLaunchArgument(
                "cv_cam4_depth_camera_info_topic",
                default_value="/synced/cam_4/depth/camera_info",
            ),
            DeclareLaunchArgument(
                "cv_cam4_depth_to_color_extrinsics_topic",
                default_value=(
                    "/synced/cam_4/extrinsics/depth_to_color"
                ),
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
            Node(
                package="surgical_interop_gateway",
                executable="surgical_interop_gateway",
                name="surgical_interop_gateway",
                condition=IfCondition(publish_shared_state),
                parameters=[
                    {
                        "default_bundle": default_bundle,
                        "publish_free_text": ParameterValue(
                            publish_shared_free_text,
                            value_type=bool,
                        ),
                    }
                ],
                output="screen",
            ),
            Node(
                package="btops_gateway",
                executable="btops_gateway",
                name="btops_gateway",
                output="screen",
            ),
            Node(
                package="auto_apms_behavior_tree",
                executable="tree_executor",
                name="tree_executor",
                parameters=[{"tick_rate": 0.1, "groot2_port": 0, "state_change_logger": True}],
                output="screen",
            ),
            Node(
                package="simulation_runtime",
                executable="speech_input_adapter",
                name="speech_input_adapter",
                parameters=[
                    {
                        "input_mode": speech_input_mode,
                        "sentence_input_topic": sentence_input_topic,
                        "min_confidence": speech_min_confidence,
                        "max_age_sec": speech_max_age_sec,
                        "source_timeout_sec": speech_source_timeout_sec,
                    }
                ],
                output="screen",
            ),
            # The sole normal text-to-command boundary.  It understands
            # natural Korean paraphrases but publishes proposal-only typed
            # intents; Digital Twin and BT retain execution authority.
            Node(
                package="voice_command",
                executable="voice_intent_resolver",
                name="voice_command_resolver",
                parameters=[
                    {
                        "input_topic": "/surgery/audio/request_text",
                        "output_topic": "/surgery/voice/intent",
                        # Bind aliases to this exact ProcedureSpec bundle;
                        # never fall back to a global T-ID vocabulary.
                        "procedure_bundle": spec_dir,
                        "selector_mode": voice_command_selector_mode,
                        "selector_endpoint": voice_command_selector_endpoint,
                        "selector_model": voice_command_selector_model,
                        "selector_timeout_sec": ParameterValue(
                            voice_command_selector_timeout_sec,
                            value_type=float,
                        ),
                    }
                ],
                output="screen",
            ),
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
                                ["'", vlm_mode, "' in ('real', 'dual')"]
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
                        "cam4_depth_camera_info_topic": (
                            cv_cam4_depth_camera_info_topic
                        ),
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
                        "flir_output_topic": field_image_topic,
                        "cam4_overlay_topic": cam4_overlay_image_topic,
                        "cam4_semantics_topic": cam4_semantics_topic,
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
                        "depth_input_topic": (
                            cv_cam4_aligned_depth_compressed_topic
                        ),
                        "depth_camera_info_topic": (
                            cv_cam4_aligned_depth_camera_info_topic
                        ),
                        "cam4_overlay_topic": cam4_overlay_image_topic,
                        "cam4_semantics_topic": cam4_semantics_topic,
                        "cam4_mayo_observation_topic": (
                            "/surgery/perception/cam4/mayo_tool_observations"
                        ),
                        "diagnostics_topic": (
                            "/surgery/perception/rfdetr/diagnostics/json"
                        ),
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
                        "requested_algorithms": ["tool", "blood", "hand"],
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
                package="vlm_node",
                executable="snapshot_bridge",
                name="field_snapshot_bridge",
                condition=IfCondition(
                    PythonExpression(["'", field_snapshot_url, "' != ''"])
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
                        "field_image_topic": field_image_topic,
                        "raw_field_image_topic": flir_input_topic,
                        "cam4_image_topic": cam4_input_topic,
                        "cam4_overlay_image_topic": cam4_overlay_image_topic,
                        "cam4_semantics_topic": cam4_semantics_topic,
                        "require_field_image": ParameterValue(
                            require_field_image,
                            value_type=bool,
                        ),
                        "output_prefix": PythonExpression(
                            ["'/vlm' if '", vlm_mode, "' == 'real' else '/vlm_real'"]
                        ),
                        "context_prefix": PythonExpression(
                            ["'/context' if '", vlm_mode, "' == 'real' else '/context_real'"]
                        ),
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
            Node(
                package="phase_estimator",
                executable="phase_estimator",
                name="phase_estimator",
                condition=IfCondition(
                    PythonExpression(["'", validation_mode, "' != 'bt_twin'"])
                ),
                parameters=[{"spec_dir": spec_dir}],
                output="screen",
            ),
            Node(
                package="or_digital_twin",
                executable="or_digital_twin",
                name="or_digital_twin",
                parameters=[
                    {
                        "spec_dir": spec_dir,
                        "validation_mode": validation_mode,
                        "vlm_mode": vlm_mode,
                        "tool_predict_evidence_confidence_threshold": 0.5,
                        "tool_predict_stability_sec": 3.0,
                        "vlm_implicit_request_confidence_threshold": 0.8,
                        "vlm_implicit_request_stability_sec": 0.7,
                        "vlm_implicit_request_release_sec": 1.5,
                        "accept_validation_actor_events": False,
                        "phase_authority": PythonExpression(
                            ["'legacy_estimator' if '", validation_mode, "' == 'demo' else 'reducer'"]
                        ),
                    }
                ],
                output="screen",
            ),
            Node(
                package="skill_execution",
                executable="mock_skill_server",
                name="mock_skill_server",
                condition=IfCondition(mock_legacy_execution_enabled),
                parameters=[
                    {
                        "action_name": "/skill/execute",
                        "rack_pick_sec": 1.0,
                        "rack_to_handover_sec": 1.2,
                        "surgeon_handover_sec": 1.0,
                        "mayo_recovery_pickup_sec": 1.0,
                        "cleaner_insert_sec": 0.8,
                        "cleaning_hold_sec": 4.5,
                        "cleaner_to_rack_sec": 1.0,
                        "mayo_dwell_sec": 0.8,
                    }
                ],
                output="screen",
            ),
            Node(
                package="skill_execution",
                executable="skill_action_bridge",
                name="skill_action_bridge",
                condition=IfCondition(legacy_execution_bridge_enabled),
                parameters=[
                    {
                        "action_name": "/skill/execute",
                        "min_repeat_interval_sec": 2.0,
                        "server_wait_timeout_sec": 3.0,
                    }
                ],
                output="screen",
            ),
            Node(
                package="surgical_interop_execution",
                executable="fault_action_emulator",
                name="robot_contract_emulator",
                condition=IfCondition(mock_direct_contract_enabled),
                parameters=[
                    {
                        "profile_path": robot_contract_profile,
                        "procedure_type": ParameterValue(
                            bed_robot_contract_procedure_type,
                            value_type=str,
                        ),
                    }
                ],
                output="screen",
            ),
            Node(
                package="surgical_interop_execution",
                executable="surgical_interop_execution_bridge",
                name="surgical_interop_execution_bridge",
                condition=IfCondition(direct_execution_bridge_enabled),
                parameters=[
                    {
                        "spec_dir": spec_dir,
                        "tool_handover_endpoint": "/surgery/tool_handover",
                        "retraction_service_name": "/surgery/retraction/command",
                        "bed_robot_status_endpoint": "/external/bed_robot_arms/status",
                        "require_bed_robot_status": ParameterValue(
                            bed_robot_contract_enabled,
                            value_type=bool,
                        ),
                        "server_wait_timeout_sec": 3.0,
                    }
                ],
                output="screen",
            ),
            Node(
                package="bt_orchestrator",
                executable="decision_bridge",
                name="bt_decision_bridge",
                parameters=[{"target_node_name": "/tree_executor", "mirror_period_sec": 0.2}],
                output="screen",
            ),
            Node(
                package="bt_orchestrator",
                executable="bed_robot_arm_group_orchestrator",
                name="bed_robot_arm_group_orchestrator",
                condition=IfCondition(bed_robot_contract_enabled),
                parameters=[
                    {
                        "spec_dir": spec_dir,
                        "vlm_confidence_threshold": 0.6,
                        "visual_direction_confidence_threshold": 0.75,
                        # real_vlm: 20 sec per attempt * 3 attempts + margin
                        "vlm_proposal_timeout_sec": 70.0,
                        "retractor_voice_normalization_enabled": ParameterValue(
                            retractor_voice_normalization_enabled,
                            value_type=bool,
                        ),
                        "retractor_voice_interpreter_mode": retractor_voice_interpreter_mode,
                        "retractor_voice_vlm_base_url": retractor_voice_vlm_base_url,
                        "retractor_voice_vlm_model_id": retractor_voice_vlm_model_id,
                        "retractor_voice_vlm_api_key": retractor_voice_vlm_api_key,
                        "retractor_voice_vlm_timeout_sec": ParameterValue(
                            retractor_voice_vlm_timeout_sec,
                            value_type=float,
                        ),
                    }
                ],
                output="screen",
            ),
            Node(
                package="simulation_runtime",
                executable="integration_preflight",
                name="integration_preflight",
                condition=IfCondition(require_integration_preflight),
                parameters=[
                    {
                        "sentence_topic": sentence_input_topic,
                        "tool_handover_action_name": "/surgery/tool_handover",
                        "retraction_service_name": "/surgery/retraction/command",
                        "require_retraction_service": ParameterValue(
                            bed_robot_contract_enabled,
                            value_type=bool,
                        ),
                        "bed_robot_arm_status_topic": "/external/bed_robot_arms/status",
                        "active_bundle": default_bundle,
                        "procedure_type": ParameterValue(
                            bed_robot_contract_procedure_type,
                            value_type=str,
                        ),
                        "require_bed_robot_arm_status": ParameterValue(
                            bed_robot_contract_enabled,
                            value_type=bool,
                        ),
                        "require_sentence_publisher": True,
                        "require_perception": ParameterValue(
                            preflight_require_perception,
                            value_type=bool,
                        ),
                        "require_metric_3d": ParameterValue(
                            preflight_require_metric_3d,
                            value_type=bool,
                        ),
                        "perception_backend": perception_backend,
                        "cv_contract_status_topic": cv_contract_status_topic,
                    }
                ],
                output="screen",
            ),
            Node(
                package="simulation_runtime",
                executable="simulation_manager",
                name="simulation_manager",
                parameters=[
                    {
                        "default_bundle": default_bundle,
                        "surgeon_actor_mode": surgeon_actor_mode,
                        "manual_override_actor_mute_sec": 8.0,
                        "execution_backend": execution_backend,
                        "require_integration_preflight": ParameterValue(
                            require_integration_preflight,
                            value_type=bool,
                        ),
                    }
                ],
                output="screen",
            ),
            rosbridge_process,
            rosapi_node,
        ]
    )
