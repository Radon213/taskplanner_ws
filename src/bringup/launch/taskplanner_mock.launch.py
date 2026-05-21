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
    vlm_mode = LaunchConfiguration("vlm_mode")
    vlm_base_url = LaunchConfiguration("vlm_base_url")
    vlm_model_id = LaunchConfiguration("vlm_model_id")
    vlm_response_mode = LaunchConfiguration("vlm_response_mode")
    validation_mode = LaunchConfiguration("validation_mode")
    enable_synthetic_scene_camera = LaunchConfiguration("enable_synthetic_scene_camera")
    field_snapshot_url = LaunchConfiguration("field_snapshot_url")
    spec_default = PathJoinSubstitution(
        [FindPackageShare("procedure_spec"), "specs", "thyroidectomy"]
    )

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
            DeclareLaunchArgument("vlm_mode", default_value="mock"),
            DeclareLaunchArgument("vlm_base_url", default_value="http://192.168.0.122:1234"),
            DeclareLaunchArgument("vlm_model_id", default_value="gemma-4-26b-a4b-it"),
            DeclareLaunchArgument("vlm_response_mode", default_value="live"),
            DeclareLaunchArgument("validation_mode", default_value="bt_twin"),
            DeclareLaunchArgument("enable_synthetic_scene_camera", default_value="true"),
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
                condition=IfCondition(
                    PythonExpression(["'", vlm_mode, "' in ['mock', 'dual']"])
                ),
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
                executable="snapshot_bridge",
                name="field_snapshot_bridge",
                condition=IfCondition(
                    PythonExpression(["'", field_snapshot_url, "' != ''"])
                ),
                parameters=[{"snapshot_url": field_snapshot_url}],
                output="screen",
            ),
            Node(
                package="vlm_node",
                executable="real_vlm",
                name="real_vlm_node",
                condition=IfCondition(
                    PythonExpression(["'", vlm_mode, "' in ['real', 'dual']"])
                ),
                parameters=[
                    {
                        "spec_dir": spec_dir,
                        "base_url": vlm_base_url,
                        "model_id": vlm_model_id,
                        "response_mode": vlm_response_mode,
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
                parameters=[{"default_bundle": "thyroidectomy"}],
                output="screen",
            ),
            rosbridge_process,
        ]
    )
