"""Launch the scenario-free Taskplanner integration Debug Mode runtime."""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, ExecuteProcess, Shutdown
from launch.conditions import IfCondition
from launch.substitutions import EnvironmentVariable, LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue
from launch_ros.substitutions import FindPackageShare

from integration_debug.bridge_policy import DEBUG_ROSAPI_TOPICS_GLOB


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
