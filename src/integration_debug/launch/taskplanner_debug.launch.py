"""Launch the scenario-free Taskplanner integration Debug Mode runtime."""

from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument,
    ExecuteProcess,
    OpaqueFunction,
    Shutdown,
)
from launch.conditions import IfCondition
from launch.substitutions import (
    EnvironmentVariable,
    LaunchConfiguration,
    PathJoinSubstitution,
)
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue
from launch_ros.substitutions import FindPackageShare

from integration_debug.bridge_policy import DEBUG_ROSAPI_TOPICS_GLOB
from simulation_runtime.cv_contract import (
    resolve_perception_selection,
    validate_perception_endpoint,
)


_PNU_ALGORITHMS = ("tool", "blood", "hand")


def _configuration(context, name: str) -> str:
    return LaunchConfiguration(name).perform(context).strip()


def _boolean_configuration(context, name: str) -> bool:
    value = _configuration(context, name).casefold()
    if value in {"true", "1", "yes", "on"}:
        return True
    if value in {"false", "0", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be true or false")


def _requested_pnu_algorithms(context) -> list[str]:
    raw = _configuration(context, "pnu_requested_algorithms")
    requested = [item.strip().casefold() for item in raw.split(",") if item.strip()]
    if (
        not requested
        or len(requested) != len(set(requested))
        or any(item not in _PNU_ALGORITHMS for item in requested)
    ):
        raise RuntimeError(
            "pnu_requested_algorithms must be a unique, nonempty CSV subset of "
            "tool,blood,hand"
        )
    requested_set = set(requested)
    return [name for name in _PNU_ALGORITHMS if name in requested_set]


def _launch_debug_pnu_bridge(context):
    """Create only the read-only PNU adapter selected for standalone Debug."""

    if not _boolean_configuration(context, "enable_pnu_perception"):
        return []
    provider = _configuration(context, "perception_provider")
    if provider != "pnu_hand_blood":
        raise RuntimeError(
            "enable_pnu_perception=true requires "
            "perception_provider=pnu_hand_blood"
        )
    location = _configuration(context, "perception_location")
    try:
        selection = resolve_perception_selection(
            provider=provider,
            location=location,
        )
        endpoint = _configuration(context, "perception_endpoint")
        if not endpoint:
            endpoint = _configuration(context, "pnu_service_url")
        if not endpoint and selection.location == "local":
            endpoint = "http://127.0.0.1:8020"
        endpoint = validate_perception_endpoint(endpoint, selection)
    except ValueError as exc:
        raise RuntimeError(f"invalid Debug PNU placement: {exc}") from exc

    allow_insecure_remote_http = _boolean_configuration(
        context, "pnu_allow_insecure_remote_http"
    )
    allow_unauthenticated_remote = _boolean_configuration(
        context, "pnu_allow_unauthenticated_remote"
    )
    depth_scale_validated = _boolean_configuration(
        context, "pnu_depth_scale_validated"
    )
    depth_alignment_validated = _boolean_configuration(
        context, "pnu_depth_alignment_validated"
    )
    return [
        Node(
            package="vlm_node",
            executable="pnu_perception_bridge",
            name="debug_pnu_perception_bridge",
            parameters=[
                {
                    "service_url": endpoint,
                    "rgb_input_topic": _configuration(
                        context, "pnu_rgb_input_topic"
                    ),
                    "color_camera_info_topic": _configuration(
                        context, "pnu_color_camera_info_topic"
                    ),
                    "depth_input_topic": _configuration(
                        context, "pnu_depth_input_topic"
                    ),
                    "depth_camera_info_topic": _configuration(
                        context, "pnu_depth_camera_info_topic"
                    ),
                    "cam4_overlay_topic": _configuration(
                        context, "pnu_overlay_topic"
                    ),
                    "cam4_pose_overlay_topic": _configuration(
                        context, "pnu_pose_overlay_topic"
                    ),
                    "cam4_semantics_topic": (
                        "/surgery/perception/cam4/semantics/json"
                    ),
                    "cam4_mayo_observation_topic": (
                        "/surgery/perception/cam4/mayo_tool_observations"
                    ),
                    "cam4_tool_observations_topic": (
                        "/surgery/perception/cam4/observations"
                    ),
                    "cam4_tool_pose_topic": (
                        "/surgery/perception/cam4/tool_poses"
                    ),
                    "cam4_hand_keypoints_topic": (
                        "/surgery/perception/cam4/hand_keypoints"
                    ),
                    "cam4_blood_semantics_topic": (
                        "/surgery/perception/cam4/blood_semantics/json"
                    ),
                    "diagnostics_topic": (
                        "/surgery/perception/rfdetr/diagnostics/json"
                    ),
                    "health_topic": "/surgery/perception/rfdetr/health",
                    "requested_algorithms": _requested_pnu_algorithms(context),
                    "expected_model_digests_json": _configuration(
                        context, "pnu_expected_model_digests_json"
                    ),
                    "expected_tool_support_plane_config_version": _configuration(
                        context,
                        "pnu_expected_tool_support_plane_config_version",
                    ),
                    "api_token_file": _configuration(
                        context, "pnu_api_token_file"
                    ),
                    "allow_insecure_remote_http": allow_insecure_remote_http,
                    "allow_unauthenticated_remote": (
                        allow_unauthenticated_remote
                    ),
                    "depth_scale_m_per_unit": float(
                        _configuration(context, "pnu_depth_scale_m_per_unit")
                    ),
                    "depth_scale_validated": depth_scale_validated,
                    "depth_alignment_validated": depth_alignment_validated,
                    "depth_alignment_id": _configuration(
                        context, "pnu_depth_alignment_id"
                    ),
                    "max_rate_hz": float(
                        _configuration(context, "pnu_max_rate_hz")
                    ),
                }
            ],
            on_exit=Shutdown(reason="debug PNU perception bridge stopped"),
            output="screen",
        )
    ]


def generate_launch_description() -> LaunchDescription:
    enable_rosbridge = LaunchConfiguration("enable_rosbridge")
    rosbridge_port = LaunchConfiguration("rosbridge_port")
    rosbridge_address = LaunchConfiguration("rosbridge_address")
    rosbridge_timeout = LaunchConfiguration("rosbridge_service_timeout")
    rosbridge_executable = LaunchConfiguration("rosbridge_executable")
    config_path = LaunchConfiguration("config_path")
    run_root = LaunchConfiguration("run_root")
    retraction_service_name = LaunchConfiguration("retraction_service_name")
    robot_endpoint_source = LaunchConfiguration("robot_endpoint_source")
    enable_virtual_robot = LaunchConfiguration("enable_virtual_robot")
    virtual_retraction_service_name = LaunchConfiguration(
        "virtual_retraction_service_name"
    )
    virtual_tool_handover_name = LaunchConfiguration(
        "virtual_tool_handover_name"
    )
    virtual_bed_robot_status_topic = LaunchConfiguration(
        "virtual_bed_robot_status_topic"
    )
    retraction_voice_interpreter_mode = LaunchConfiguration(
        "retraction_voice_interpreter_mode"
    )
    retraction_voice_vlm_base_url = LaunchConfiguration(
        "retraction_voice_vlm_base_url"
    )
    retraction_voice_vlm_model_id = LaunchConfiguration(
        "retraction_voice_vlm_model_id"
    )
    retraction_voice_vlm_api_key = LaunchConfiguration(
        "retraction_voice_vlm_api_key"
    )
    retraction_voice_vlm_timeout_sec = LaunchConfiguration(
        "retraction_voice_vlm_timeout_sec"
    )

    rosbridge = ExecuteProcess(
        condition=IfCondition(enable_rosbridge),
        cmd=[
            "ros2",
            "run",
            "integration_debug",
            rosbridge_executable,
            "--ros-args",
            "-p",
            ["port:=", rosbridge_port],
            "-p",
            ["address:=", rosbridge_address],
            "-p",
            ["default_call_service_timeout:=", rosbridge_timeout],
        ],
        on_exit=Shutdown(reason="integration debug rosbridge stopped"),
        output="screen",
    )

    speech_input_adapter = Node(
        package="simulation_runtime",
        executable="speech_input_adapter",
        name="debug_speech_input_adapter",
        parameters=[
            {
                "input_mode": "sentence_text",
                "sentence_input_topic": "/sensors/surgeon/sentence",
                "output_topic": "/surgery/audio/request_text",
                "status_topic": "/input/speech/status",
                "sentence_source_id": "integration_debug_asr_sentence",
                "sentence_dedupe_sec": 1.0,
            }
        ],
        on_exit=Shutdown(reason="debug speech input adapter stopped"),
        output="screen",
    )

    virtual_robot = Node(
        condition=IfCondition(enable_virtual_robot),
        package="surgical_interop_execution",
        executable="fault_action_emulator",
        name="integration_debug_virtual_robot",
        parameters=[
            {
                "profile_path": PathJoinSubstitution(
                    [
                        FindPackageShare("integration_debug"),
                        "config",
                        "virtual_robot.yaml",
                    ]
                ),
                "procedure_type": "nephrectomy",
                "max_retraction_distance_m": 0.050,
            }
        ],
        remappings=[
            ("/surgery/tool_handover", virtual_tool_handover_name),
            ("/surgery/retraction/command", virtual_retraction_service_name),
            ("/external/bed_robot_arms/status", virtual_bed_robot_status_topic),
            ("/test/action_emulator/status", "/integration/debug/virtual/status"),
        ],
        on_exit=Shutdown(reason="integration debug virtual robot stopped"),
        output="screen",
    )

    return LaunchDescription(
        [
            DeclareLaunchArgument("enable_rosbridge", default_value="true"),
            DeclareLaunchArgument("rosbridge_port", default_value="9091"),
            DeclareLaunchArgument("rosbridge_address", default_value="127.0.0.1"),
            DeclareLaunchArgument("rosbridge_service_timeout", default_value="30.0"),
            DeclareLaunchArgument(
                "rosbridge_executable",
                default_value="secure_debug_rosbridge",
            ),
            DeclareLaunchArgument(
                "config_path",
                default_value=PathJoinSubstitution(
                    [FindPackageShare("integration_debug"), "config", "integration_debug.yaml"]
                ),
            ),
            DeclareLaunchArgument(
                "run_root",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_RUN_ROOT", default_value="/tmp/taskplanner-runs"
                ),
            ),
            DeclareLaunchArgument(
                "retraction_service_name",
                default_value="/surgery/retraction/command",
            ),
            DeclareLaunchArgument(
                "robot_endpoint_source",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_DEBUG_ROBOT_ENDPOINT_SOURCE",
                    default_value="external",
                ),
                choices=("external", "virtual"),
            ),
            DeclareLaunchArgument("enable_virtual_robot", default_value="true"),
            DeclareLaunchArgument(
                "virtual_retraction_service_name",
                default_value="/integration/debug/virtual/retraction/command",
            ),
            DeclareLaunchArgument(
                "virtual_tool_handover_name",
                default_value="/integration/debug/virtual/tool_handover",
            ),
            DeclareLaunchArgument(
                "virtual_bed_robot_status_topic",
                default_value=(
                    "/integration/debug/virtual/bed_robot_arms/status"
                ),
            ),
            DeclareLaunchArgument(
                "retraction_voice_interpreter_mode",
                default_value=EnvironmentVariable(
                    "RETRACTOR_VOICE_INTERPRETER_MODE",
                    default_value="vlm_with_fallback",
                ),
                choices=("deterministic", "vlm_with_fallback"),
            ),
            DeclareLaunchArgument(
                "retraction_voice_vlm_base_url",
                default_value=EnvironmentVariable(
                    "RETRACTOR_VOICE_VLM_BASE_URL",
                    default_value="http://127.0.0.1:8080",
                ),
            ),
            DeclareLaunchArgument(
                "retraction_voice_vlm_model_id",
                default_value=EnvironmentVariable(
                    "RETRACTOR_VOICE_VLM_MODEL_ID",
                    default_value="qwen3.6-35b-a3b",
                ),
            ),
            DeclareLaunchArgument(
                "retraction_voice_vlm_api_key",
                default_value=EnvironmentVariable(
                    "RETRACTOR_VOICE_VLM_API_KEY",
                    default_value=EnvironmentVariable(
                        "VLM_API_KEY", default_value=""
                    ),
                ),
            ),
            DeclareLaunchArgument(
                "retraction_voice_vlm_timeout_sec",
                default_value=EnvironmentVariable(
                    "RETRACTOR_VOICE_VLM_TIMEOUT_SEC", default_value="2.0"
                ),
            ),
            DeclareLaunchArgument(
                "enable_pnu_perception",
                default_value=EnvironmentVariable(
                    "ENABLE_PNU_DEBUG_PERCEPTION", default_value="false"
                ),
                description=(
                    "Run only the read-only CAM4 PNU bridge in standalone Debug."
                ),
            ),
            DeclareLaunchArgument(
                "perception_provider",
                default_value=EnvironmentVariable(
                    "PERCEPTION_PROVIDER", default_value="disabled"
                ),
            ),
            DeclareLaunchArgument(
                "perception_location",
                default_value=EnvironmentVariable(
                    "PERCEPTION_LOCATION", default_value="local"
                ),
                choices=("local", "remote"),
            ),
            DeclareLaunchArgument(
                "perception_endpoint",
                default_value=EnvironmentVariable(
                    "PERCEPTION_ENDPOINT", default_value=""
                ),
            ),
            DeclareLaunchArgument(
                "pnu_service_url",
                default_value=EnvironmentVariable(
                    "PNU_SERVICE_URL", default_value=""
                ),
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
                description=(
                    "Development-only opt-in for HTTP to a non-loopback PNU "
                    "worker. Remote endpoints require HTTPS by default."
                ),
            ),
            DeclareLaunchArgument(
                "pnu_allow_unauthenticated_remote",
                default_value=EnvironmentVariable(
                    "PNU_ALLOW_UNAUTHENTICATED_REMOTE", default_value="false"
                ),
            ),
            DeclareLaunchArgument(
                "pnu_requested_algorithms",
                default_value=EnvironmentVariable(
                    "PNU_DEBUG_REQUESTED_ALGORITHMS",
                    default_value="tool,blood,hand",
                ),
                description=(
                    "Exact PNU model subset for Debug; unavailable models must "
                    "not be listed."
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
                    "PNU_EXPECTED_TOOL_SUPPORT_PLANE_CONFIG_VERSION",
                    default_value="",
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
                default_value=EnvironmentVariable(
                    "PNU_DEPTH_ALIGNMENT_ID", default_value=""
                ),
            ),
            DeclareLaunchArgument(
                "pnu_rgb_input_topic",
                default_value=EnvironmentVariable(
                    "CV_CAM4_RGB_TOPIC",
                    default_value="/synced/cam_4/color/image_raw/compressed",
                ),
            ),
            DeclareLaunchArgument(
                "pnu_color_camera_info_topic",
                default_value=EnvironmentVariable(
                    "CV_CAM4_CAMERA_INFO_TOPIC",
                    default_value="/synced/cam_4/color/camera_info",
                ),
            ),
            DeclareLaunchArgument(
                "pnu_depth_input_topic",
                default_value=EnvironmentVariable(
                    "CV_CAM4_ALIGNED_DEPTH_COMPRESSED_TOPIC",
                    default_value=(
                        "/synced/cam_4/aligned_depth_to_color/"
                        "image_raw/compressedDepth"
                    ),
                ),
            ),
            DeclareLaunchArgument(
                "pnu_depth_camera_info_topic",
                default_value=EnvironmentVariable(
                    "CV_CAM4_ALIGNED_DEPTH_CAMERA_INFO_TOPIC",
                    default_value=(
                        "/synced/cam_4/aligned_depth_to_color/camera_info"
                    ),
                ),
            ),
            DeclareLaunchArgument(
                "pnu_overlay_topic",
                default_value=EnvironmentVariable(
                    "CAM4_OVERLAY_TOPIC",
                    default_value=(
                        "/surgery/images/cam4/detection_overlay/compressed"
                    ),
                ),
            ),
            DeclareLaunchArgument(
                "pnu_pose_overlay_topic",
                default_value=EnvironmentVariable(
                    "CAM4_POSE_OVERLAY_TOPIC",
                    default_value=(
                        "/surgery/images/cam4/pose_overlay/compressed"
                    ),
                ),
            ),
            DeclareLaunchArgument(
                "pnu_max_rate_hz",
                default_value=EnvironmentVariable(
                    "PNU_DEBUG_MAX_RATE_HZ", default_value="5.0"
                ),
            ),
            OpaqueFunction(function=_launch_debug_pnu_bridge),
            speech_input_adapter,
            virtual_robot,
            # rosapi only exposes the same bounded multicam/debug topic set
            # that secure_debug_rosbridge can subscribe to.  The browser can
            # call /rosapi/topics, but no parameter-mutating rosapi service.
            Node(
                package="rosapi",
                executable="rosapi_node",
                # roslib's discovery client calls the conventional
                # absolute /rosapi/topics service, so retain rosapi's
                # canonical node/service namespace.
                name="rosapi",
                parameters=[
                    {
                        "topics_glob": DEBUG_ROSAPI_TOPICS_GLOB,
                        "services_glob": "[]",
                        "params_glob": "[]",
                    }
                ],
                output="screen",
            ),
            rosbridge,
            Node(
                package="integration_debug",
                executable="integration_debug_node",
                name="integration_debug_gateway",
                parameters=[
                    {
                        "config_path": config_path,
                        "run_root": run_root,
                        "retraction_service_name": retraction_service_name,
                        "robot_endpoint_source": robot_endpoint_source,
                        "virtual_robot_enabled": ParameterValue(
                            enable_virtual_robot,
                            value_type=bool,
                        ),
                        "virtual_retraction_service_name": (
                            virtual_retraction_service_name
                        ),
                        "virtual_tool_handover_name": (
                            virtual_tool_handover_name
                        ),
                        "virtual_bed_robot_status_topic": (
                            virtual_bed_robot_status_topic
                        ),
                        "retraction_voice_interpreter_mode": (
                            retraction_voice_interpreter_mode
                        ),
                        "retraction_voice_vlm_base_url": (
                            retraction_voice_vlm_base_url
                        ),
                        "retraction_voice_vlm_model_id": (
                            retraction_voice_vlm_model_id
                        ),
                        "retraction_voice_vlm_api_key": (
                            retraction_voice_vlm_api_key
                        ),
                        "retraction_voice_vlm_timeout_sec": (
                            ParameterValue(
                                retraction_voice_vlm_timeout_sec,
                                value_type=float,
                            )
                        ),
                    }
                ],
                on_exit=Shutdown(reason="integration debug gateway stopped"),
                output="screen",
            ),
        ]
    )
