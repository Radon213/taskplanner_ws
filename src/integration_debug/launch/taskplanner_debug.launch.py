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
                    default_value=EnvironmentVariable(
                        "VLM_BASE_URL",
                        default_value="http://127.0.0.1:8001",
                    ),
                ),
            ),
            DeclareLaunchArgument(
                "retraction_voice_vlm_model_id",
                default_value=EnvironmentVariable(
                    "RETRACTOR_VOICE_VLM_MODEL_ID",
                    default_value=EnvironmentVariable(
                        "VLM_MODEL_ID",
                        default_value="unsloth/gemma-4-E4B-it-NVFP4",
                    ),
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
