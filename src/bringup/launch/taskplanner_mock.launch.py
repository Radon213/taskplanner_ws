"""Bring up the taskplanner v1 mock runtime."""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, ExecuteProcess
from launch.conditions import IfCondition
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution, PythonExpression
from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare


def generate_launch_description() -> LaunchDescription:
    spec_dir = LaunchConfiguration("spec_dir")
    enable_rosbridge = LaunchConfiguration("enable_rosbridge")
    rosbridge_port = LaunchConfiguration("rosbridge_port")
    rosbridge_address = LaunchConfiguration("rosbridge_address")
    rosbridge_service_timeout = LaunchConfiguration("rosbridge_service_timeout")
    vlm_mode = LaunchConfiguration("vlm_mode")
    vlm_base_url = LaunchConfiguration("vlm_base_url")
    vlm_model_id = LaunchConfiguration("vlm_model_id")
    vlm_api_mode = LaunchConfiguration("vlm_api_mode")
    vlm_publish_period_sec = LaunchConfiguration("vlm_publish_period_sec")
    vlm_max_output_tokens = LaunchConfiguration("vlm_max_output_tokens")
    vlm_response_format = LaunchConfiguration("vlm_response_format")
    vlm_reasoning_effort = LaunchConfiguration("vlm_reasoning_effort")
    vlm_response_mode = LaunchConfiguration("vlm_response_mode")
    vlm_context_mode = LaunchConfiguration("vlm_context_mode")
    vlm_image_stale_sec = LaunchConfiguration("vlm_image_stale_sec")
    surgeon_actor_mode = LaunchConfiguration("surgeon_actor_mode")
    actor_base_url = LaunchConfiguration("actor_base_url")
    actor_model_id = LaunchConfiguration("actor_model_id")
    actor_response_format = LaunchConfiguration("actor_response_format")
    actor_reasoning_effort = LaunchConfiguration("actor_reasoning_effort")
    validation_mode = LaunchConfiguration("validation_mode")
    enable_no_image_camera = LaunchConfiguration("enable_no_image_camera")
    enable_synthetic_scene_camera = LaunchConfiguration("enable_synthetic_scene_camera")
    field_snapshot_url = LaunchConfiguration("field_snapshot_url")
    spec_default = PathJoinSubstitution(
        [FindPackageShare("procedure_spec"), "specs", "thyroidectomy"]
    )
    mock_vlm_enabled = PythonExpression(
        ["'", vlm_mode, "' == 'mock' or '", vlm_mode, "' == 'dual'"]
    )
    real_vlm_enabled = PythonExpression(
        ["'", vlm_mode, "' == 'real' or '", vlm_mode, "' == 'dual'"]
    )
    rule_surgeon_actor_enabled = PythonExpression(["'", surgeon_actor_mode, "' == 'rule'"])
    llm_surgeon_actor_enabled = PythonExpression(["'", surgeon_actor_mode, "' == 'llm'"])

    rosbridge_process = ExecuteProcess(
        condition=IfCondition(enable_rosbridge),
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

    return LaunchDescription(
        [
            DeclareLaunchArgument("spec_dir", default_value=spec_default),
            DeclareLaunchArgument("enable_rosbridge", default_value="true"),
            DeclareLaunchArgument("rosbridge_port", default_value="9090"),
            DeclareLaunchArgument("rosbridge_address", default_value="127.0.0.1"),
            DeclareLaunchArgument("rosbridge_service_timeout", default_value="30.0"),
            DeclareLaunchArgument("vlm_mode", default_value="real"),
            DeclareLaunchArgument("vlm_base_url", default_value="http://127.0.0.1:1234"),
            DeclareLaunchArgument("vlm_model_id", default_value="qwen3.6-35b-a3b-mtp@q2_k_xl"),
            DeclareLaunchArgument("vlm_api_mode", default_value="openai_compat"),
            DeclareLaunchArgument("vlm_publish_period_sec", default_value="1.0"),
            DeclareLaunchArgument("vlm_max_output_tokens", default_value="320"),
            DeclareLaunchArgument("vlm_response_format", default_value="json_schema"),
            DeclareLaunchArgument("vlm_reasoning_effort", default_value="none"),
            DeclareLaunchArgument("vlm_response_mode", default_value="live"),
            DeclareLaunchArgument("vlm_context_mode", default_value="actor_log"),
            DeclareLaunchArgument("vlm_image_stale_sec", default_value="3.0"),
            DeclareLaunchArgument("surgeon_actor_mode", default_value="llm"),
            DeclareLaunchArgument("actor_base_url", default_value="http://127.0.0.1:1234"),
            DeclareLaunchArgument("actor_model_id", default_value="google/gemma-4-12b-qat"),
            DeclareLaunchArgument("actor_response_format", default_value="json_schema"),
            DeclareLaunchArgument("actor_reasoning_effort", default_value="none"),
            DeclareLaunchArgument("validation_mode", default_value="bt_twin"),
            DeclareLaunchArgument("enable_no_image_camera", default_value="true"),
            DeclareLaunchArgument("enable_synthetic_scene_camera", default_value="false"),
            DeclareLaunchArgument("field_snapshot_url", default_value=""),
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
                package="vlm_node",
                executable="mock_vlm",
                name="mock_vlm_node",
                condition=IfCondition(mock_vlm_enabled),
                parameters=[
                    {
                        "spec_dir": spec_dir,
                        "perception_scene_observations": True,
                        "state_backed_observations": False,
                    }
                ],
                output="screen",
            ),
            Node(
                package="vlm_node",
                executable="synthetic_scene_camera",
                name="synthetic_scene_camera",
                condition=IfCondition(enable_synthetic_scene_camera),
                output="screen",
            ),
            Node(
                package="vlm_node",
                executable="no_image_camera",
                name="no_image_camera",
                condition=IfCondition(enable_no_image_camera),
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
                        "model_id": vlm_model_id,
                        "api_mode": vlm_api_mode,
                        "publish_period_sec": vlm_publish_period_sec,
                        "max_output_tokens": vlm_max_output_tokens,
                        "response_format": vlm_response_format,
                        "reasoning_effort": vlm_reasoning_effort,
                        "response_mode": vlm_response_mode,
                        "context_mode": vlm_context_mode,
                        "image_stale_sec": vlm_image_stale_sec,
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
                        "model_id": actor_model_id,
                        "response_format": actor_response_format,
                        "reasoning_effort": actor_reasoning_effort,
                        "decision_period_sec": 0.25,
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
                        "tool_predict_stability_sec": 3.0,
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
                package="bt_orchestrator",
                executable="decision_bridge",
                name="bt_decision_bridge",
                parameters=[{"target_node_name": "/tree_executor", "mirror_period_sec": 0.2}],
                output="screen",
            ),
            Node(
                package="simulation_runtime",
                executable="simulation_manager",
                name="simulation_manager",
                parameters=[
                    {
                        "default_bundle": "thyroidectomy",
                        "surgeon_actor_mode": surgeon_actor_mode,
                        "manual_override_actor_mute_sec": 8.0,
                    }
                ],
                output="screen",
            ),
            rosbridge_process,
        ]
    )
