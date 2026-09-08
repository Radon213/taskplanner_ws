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
from procedure_spec import get_default_spec_dir, load_bundle

from bringup.perception_config import resolve_launch_perception
from bringup.runtime_core_launch import build_bt_engine_actions


def _bed_robot_contract_configuration(context):
    bundle_id = LaunchConfiguration("default_bundle").perform(context).strip()
    spec_dir = str(context.launch_configurations.get("spec_dir", "")).strip()
    if not spec_dir:
        spec_dir = str(get_default_spec_dir().parent / bundle_id)
    runtime = load_bundle(spec_dir).get_scenario_runtime_requirements()
    return [
        SetLaunchConfiguration(
            "bed_robot_contract_enabled",
            "true" if runtime.bed_robot_contract_enabled else "false",
        ),
        SetLaunchConfiguration(
            "bed_robot_contract_procedure_type",
            runtime.procedure_type,
        ),
        SetLaunchConfiguration(
            "tool_handover_contract_enabled",
            "true" if runtime.tool_handover_action_required else "false",
        ),
        SetLaunchConfiguration(
            "procedure_image_vlm_enabled",
            "true" if runtime.image_vlm_enabled else "false",
        ),
        SetLaunchConfiguration(
            "procedure_dialogue_vlm_enabled",
            "true" if runtime.dialogue_vlm_enabled else "false",
        ),
        SetLaunchConfiguration(
            "procedure_perception_enabled",
            "true" if runtime.perception_enabled else "false",
        ),
        SetLaunchConfiguration(
            "voice_intent_resolver_enabled",
            "true" if runtime.voice_intent_resolver_enabled else "false",
        ),
        SetLaunchConfiguration(
            "procedure_surgeon_actor_enabled",
            "true" if runtime.surgeon_actor_enabled else "false",
        ),
        SetLaunchConfiguration(
            "procedure_phase_inference_enabled",
            "true" if runtime.phase_inference_enabled else "false",
        ),
        SetLaunchConfiguration(
            "retraction_workflow_state_enforced",
            "true" if runtime.retraction_workflow_state_enforced else "false",
        ),
    ]


def generate_launch_description() -> LaunchDescription:
    spec_dir = LaunchConfiguration("spec_dir")
    bundle_snapshot_root = LaunchConfiguration("bundle_snapshot_root")
    default_bundle = LaunchConfiguration("default_bundle")
    enable_tool_belief_tracker = LaunchConfiguration(
        "enable_tool_belief_tracker"
    )
    publish_shared_state = LaunchConfiguration("publish_shared_state")
    publish_shared_free_text = LaunchConfiguration("publish_shared_free_text")
    enable_rosbridge = LaunchConfiguration("enable_rosbridge")
    rosbridge_port = LaunchConfiguration("rosbridge_port")
    rosbridge_address = LaunchConfiguration("rosbridge_address")
    rosbridge_service_timeout = LaunchConfiguration("rosbridge_service_timeout")
    input_profile = LaunchConfiguration("input_profile")
    execution_backend = LaunchConfiguration("execution_backend")
    robot_endpoint_source = LaunchConfiguration(
        "robot_endpoint_source", default="external"
    )
    retraction_endpoint_source = LaunchConfiguration(
        "retraction_endpoint_source", default=robot_endpoint_source
    )
    enable_runtime_route_control = LaunchConfiguration(
        "enable_runtime_route_control"
    )
    external_controller_contract_id = LaunchConfiguration(
        "external_controller_contract_id"
    )
    external_capability_policy_id = LaunchConfiguration(
        "external_capability_policy_id"
    )
    controller_contract_max_age_sec = LaunchConfiguration(
        "controller_contract_max_age_sec"
    )
    asr_runtime_status_topic = LaunchConfiguration("asr_runtime_status_topic")
    require_asr_runtime_status = LaunchConfiguration("require_asr_runtime_status")
    asr_runtime_status_max_age_sec = LaunchConfiguration(
        "asr_runtime_status_max_age_sec"
    )
    asr_runtime_status_source_future_tolerance_sec = LaunchConfiguration(
        "asr_runtime_status_source_future_tolerance_sec"
    )
    bed_robot_contract_enabled = LaunchConfiguration(
        "bed_robot_contract_enabled"
    )
    bed_robot_contract_procedure_type = LaunchConfiguration(
        "bed_robot_contract_procedure_type"
    )
    tool_handover_contract_enabled = LaunchConfiguration(
        "tool_handover_contract_enabled"
    )
    procedure_image_vlm_enabled = LaunchConfiguration(
        "procedure_image_vlm_enabled"
    )
    procedure_dialogue_vlm_enabled = LaunchConfiguration(
        "procedure_dialogue_vlm_enabled"
    )
    procedure_perception_enabled = LaunchConfiguration(
        "procedure_perception_enabled"
    )
    voice_intent_resolver_enabled = LaunchConfiguration(
        "voice_intent_resolver_enabled"
    )
    procedure_surgeon_actor_enabled = LaunchConfiguration(
        "procedure_surgeon_actor_enabled"
    )
    procedure_phase_inference_enabled = LaunchConfiguration(
        "procedure_phase_inference_enabled"
    )
    speech_input_mode = LaunchConfiguration("speech_input_mode")
    speech_input_topic = LaunchConfiguration("speech_input_topic")
    sentence_input_topic = LaunchConfiguration("sentence_input_topic")
    effective_speech_source_topic = PythonExpression(
        [
            "'",
            speech_input_topic,
            "' if '",
            speech_input_mode,
            "'.strip().lower() == 'utterance' else '",
            sentence_input_topic,
            "'",
        ]
    )
    speech_output_mode = LaunchConfiguration("speech_output_mode")
    speech_typed_output_topic = LaunchConfiguration("speech_typed_output_topic")
    enable_tts_echo_guard = LaunchConfiguration("enable_tts_echo_guard")
    tts_playback_status_topic = LaunchConfiguration(
        "tts_playback_status_topic"
    )
    tts_echo_tail_sec = LaunchConfiguration("tts_echo_tail_sec")
    tts_echo_similarity_threshold = LaunchConfiguration(
        "tts_echo_similarity_threshold"
    )
    speech_min_confidence = LaunchConfiguration("speech_min_confidence")
    speech_max_age_sec = LaunchConfiguration("speech_max_age_sec")
    speech_source_timeout_sec = LaunchConfiguration("speech_source_timeout_sec")
    voice_command_input_mode = LaunchConfiguration("voice_command_input_mode")
    voice_command_input_topic = LaunchConfiguration("voice_command_input_topic")
    voice_command_output_topic = LaunchConfiguration("voice_command_output_topic")
    resolver_input_topic = LaunchConfiguration("resolver_input_topic")
    resolver_output_topic = LaunchConfiguration("resolver_output_topic")
    legacy_voice_intent_topic = LaunchConfiguration("legacy_voice_intent_topic")
    command_router_enabled = LaunchConfiguration("command_router_enabled")
    command_router_catalog_path = LaunchConfiguration("command_router_catalog_path")
    command_router_catalog_reload_sec = LaunchConfiguration(
        "command_router_catalog_reload_sec"
    )
    voice_intent_require_source_metadata = LaunchConfiguration(
        "voice_intent_require_source_metadata"
    )
    voice_intent_max_age_sec = LaunchConfiguration("voice_intent_max_age_sec")
    voice_intent_future_tolerance_sec = LaunchConfiguration(
        "voice_intent_future_tolerance_sec"
    )
    voice_intent_dedupe_retention_sec = LaunchConfiguration(
        "voice_intent_dedupe_retention_sec"
    )
    hand_mapping_operator_approved = LaunchConfiguration(
        "hand_mapping_operator_approved"
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
    vlm_require_source_frame_timestamp = LaunchConfiguration(
        "vlm_require_source_frame_timestamp"
    )
    vlm_model_input_max_source_lag_sec = LaunchConfiguration(
        "vlm_model_input_max_source_lag_sec"
    )
    vlm_model_input_max_source_future_skew_sec = LaunchConfiguration(
        "vlm_model_input_max_source_future_skew_sec"
    )
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
    cam3_input_topic = LaunchConfiguration("cam3_input_topic")
    field_image_topic = LaunchConfiguration("field_image_topic")
    rfdetr_flir_output_topic = LaunchConfiguration(
        "rfdetr_flir_output_topic"
    )
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
    require_rfdetr_cam4_overlay = LaunchConfiguration(
        "require_rfdetr_cam4_overlay"
    )
    enable_integration_preflight_diagnostics = LaunchConfiguration(
        "enable_integration_preflight_diagnostics"
    )
    preflight_require_perception = LaunchConfiguration(
        "preflight_require_perception"
    )
    preflight_require_rfdetr_tool_observations = LaunchConfiguration(
        "preflight_require_rfdetr_tool_observations"
    )
    rfdetr_vlm_request_context_topic = LaunchConfiguration(
        "rfdetr_vlm_request_context_topic"
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
    rule_surgeon_actor_enabled = PythonExpression(
        [
            "'",
            procedure_surgeon_actor_enabled,
            "'.lower() == 'true' and '",
            input_profile,
            "' == 'simulation' and '",
            surgeon_actor_mode,
            "' == 'rule'",
        ]
    )
    llm_surgeon_actor_enabled = PythonExpression(
        [
            "'",
            procedure_surgeon_actor_enabled,
            "'.lower() == 'true' and '",
            input_profile,
            "' == 'simulation' and '",
            surgeon_actor_mode,
            "' == 'llm'",
        ]
    )
    no_image_camera_enabled = PythonExpression(
        [
            "'",
            procedure_image_vlm_enabled,
            "'.lower() == 'true' and '",
            input_profile,
            "' == 'simulation' and '",
            enable_no_image_camera,
            "'.lower() in ('true', '1', 'yes')",
        ]
    )
    synthetic_scene_camera_enabled = PythonExpression(
        [
            "'",
            procedure_image_vlm_enabled,
            "'.lower() == 'true' and '",
            input_profile,
            "' == 'simulation' and '",
            enable_synthetic_scene_camera,
            "'.lower() in ('true', '1', 'yes')",
        ]
    )
    direct_execution_bridge_enabled = PythonExpression(
        [
            "'",
            execution_backend,
            "' != 'mock' or '",
            bed_robot_contract_enabled,
            "'.lower() == 'true')",
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
    virtual_robot_endpoint_enabled = PythonExpression(
        [
            "'",
            robot_endpoint_source,
            "'.strip().lower() == 'virtual'",
        ]
    )
    virtual_retraction_endpoint_enabled = PythonExpression(
        [
            "'",
            retraction_endpoint_source,
            "'.strip().lower() == 'virtual'",
        ]
    )
    retraction_state_machine_suppression_enabled = PythonExpression(
        [
            "'",
            retraction_endpoint_source,
            "'.strip().lower() == 'virtual' or '",
            LaunchConfiguration("retraction_workflow_state_enforced"),
            "'.strip().lower() != 'true'",
        ]
    )
    robot_contract_emulator_enabled = PythonExpression(
        [
            "'",
            execution_backend,
            "' == 'mock' and '",
            bed_robot_contract_enabled,
            "'.lower() == 'true' and '",
            robot_endpoint_source,
            "'.strip().lower() != 'virtual'",
        ]
    )
    tool_handover_endpoint = PythonExpression(
        [
            "'/integration/virtual/surgery/tool_handover' if '",
            robot_endpoint_source,
            "'.strip().lower() == 'virtual' else '/surgery/tool_handover'",
        ]
    )
    retraction_service_name = PythonExpression(
        [
            "'/integration/virtual/surgery/retraction/command' if '",
            retraction_endpoint_source,
            "'.strip().lower() == 'virtual' else '/surgery/retraction/command'",
        ]
    )
    controller_contract_topic = PythonExpression(
        [
            "'/integration/virtual/surgery/controller_contract' if '",
            robot_endpoint_source,
            "'.strip().lower() == 'virtual' else '/surgery/controller_contract'",
        ]
    )
    expected_controller_contract_id = PythonExpression(
        [
            "'taskplanner-virtual-eir-nuc.v1' if '",
            robot_endpoint_source,
            "'.strip().lower() == 'virtual' else '",
            external_controller_contract_id,
            "'",
        ]
    )
    expected_capability_policy_id = PythonExpression(
        [
            "'taskplanner-virtual-full-inventory.v1' if '",
            robot_endpoint_source,
            "'.strip().lower() == 'virtual' else '",
            external_capability_policy_id,
            "'",
        ]
    )
    physical_stop_confirmation_required = PythonExpression(
        [
            "'",
            robot_endpoint_source,
            "'.strip().lower() == 'external' and '",
            execution_backend,
            "'.strip().lower() == 'external'",
        ]
    )
    # Unlike the selected-route parameter above, this is the fixed safety
    # requirement for the external family after a stopped runtime switch.
    # A Live process may begin on virtual and later select external, so it
    # must not inherit virtual's ``False`` value for the external contract.
    external_physical_stop_confirmation_required = PythonExpression(
        [
            "'",
            execution_backend,
            "'.strip().lower() == 'external'",
        ]
    )
    emulator_contract_id = PythonExpression(
        [
            "'taskplanner-virtual-eir-nuc.v1' if '",
            robot_endpoint_source,
            "'.strip().lower() == 'virtual' else 'taskplanner-generic-emulator.v1'",
        ]
    )
    emulator_capability_policy_id = PythonExpression(
        [
            "'taskplanner-virtual-full-inventory.v1' if '",
            robot_endpoint_source,
            "'.strip().lower() == 'virtual' else 'taskplanner-generic-emulator.v1'",
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
                "enable_tool_belief_tracker",
                default_value="false",
                description=(
                    "Publish advisory fixed-inventory tool-location beliefs. "
                    "This observer is not a procedure-start or dispatch gate."
                ),
            ),
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
            DeclareLaunchArgument(
                "bundle_snapshot_root",
                default_value="/tmp/taskplanner-procedure-snapshots",
                description=(
                    "Private content-addressed ProcedureSpec snapshot root shared "
                    "by SimulationManager and hot-reload participants."
                ),
            ),
            DeclareLaunchArgument("enable_rosbridge", default_value="true"),
            DeclareLaunchArgument("rosbridge_port", default_value="9090"),
            DeclareLaunchArgument("rosbridge_address", default_value="127.0.0.1"),
            DeclareLaunchArgument("rosbridge_service_timeout", default_value="30.0"),
            DeclareLaunchArgument("input_profile", default_value="simulation"),
            DeclareLaunchArgument("execution_backend", default_value="mock"),
            DeclareLaunchArgument(
                "robot_endpoint_source",
                default_value="external",
                choices=("external", "virtual"),
                description=(
                    "Launch-lifetime robot endpoint source. virtual uses only "
                    "the isolated /integration/virtual/* emulator namespace."
                ),
            ),
            DeclareLaunchArgument(
                "retraction_endpoint_source",
                default_value=robot_endpoint_source,
                choices=("external", "virtual"),
                description=(
                    "Independent retraction Service source. It defaults to "
                    "robot_endpoint_source for legacy all-real/all-virtual runs."
                ),
            ),
            DeclareLaunchArgument(
                "enable_runtime_route_control",
                default_value="false",
                description=(
                    "Enable the stopped-only Live Action/Service route "
                    "coordinator. taskplanner_live sets this true."
                ),
            ),
            DeclareLaunchArgument(
                "external_controller_contract_id",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_EXTERNAL_CONTROLLER_CONTRACT_ID",
                    default_value="eir-nuc-tool-handover.real.v1",
                ),
                description=(
                    "Expected external controller manifest ID for route diagnostics; "
                    "it is not an integration-preflight or dispatch admission gate."
                ),
            ),
            DeclareLaunchArgument(
                "external_capability_policy_id",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_EXTERNAL_CAPABILITY_POLICY_ID",
                    default_value="eir-nuc-tool-handover.v1",
                ),
                description=(
                    "Exact reviewed handover capability policy expected from the "
                    "external controller manifest."
                ),
            ),
            DeclareLaunchArgument(
                "controller_contract_max_age_sec",
                default_value="3.0",
                description=(
                    "Legacy compatibility setting for controller diagnostics only; "
                    "it never authorizes or blocks dispatch."
                ),
            ),
            DeclareLaunchArgument(
                "asr_runtime_status_topic",
                default_value="/input/asr/runtime_status",
                description=(
                    "Operational ASR status lease. Live requires a fresh, "
                    "execution-capable payload in addition to the typed input "
                    "publisher."
                ),
            ),
            DeclareLaunchArgument(
                "require_asr_runtime_status",
                default_value="false",
                description=(
                    "Require a fresh taskplanner.asr.status.v1 status. Live "
                    "sets this true; Debug/replay keeps an explicit opt-in."
                ),
            ),
            DeclareLaunchArgument(
                "asr_runtime_status_max_age_sec",
                default_value="3.0",
            ),
            DeclareLaunchArgument(
                "asr_runtime_status_source_future_tolerance_sec",
                default_value="0.5",
            ),
            OpaqueFunction(function=_bed_robot_contract_configuration),
            DeclareLaunchArgument("speech_input_mode", default_value="utterance"),
            DeclareLaunchArgument(
                "speech_input_topic",
                default_value="/sensors/speech/utterance",
            ),
            DeclareLaunchArgument(
                "sentence_input_topic",
                default_value="/sensors/surgeon/sentence",
            ),
            DeclareLaunchArgument(
                "speech_output_mode",
                default_value="sentence_text",
                choices=("sentence_text", "typed_utterance"),
                description=(
                    "sentence_text is Debug/replay compatibility; "
                    "typed_utterance preserves final ASR metadata."
                ),
            ),
            DeclareLaunchArgument(
                "speech_typed_output_topic",
                default_value="/surgery/audio/admitted_utterance",
            ),
            DeclareLaunchArgument(
                "enable_tts_echo_guard",
                default_value="false",
                description=(
                    "Suppress typed ASR transcripts that match locally audible "
                    "TTS playback. Live enables this; mock/replay stays opt-in."
                ),
            ),
            DeclareLaunchArgument(
                "tts_playback_status_topic",
                default_value="/tts/playback_status",
            ),
            DeclareLaunchArgument("tts_echo_tail_sec", default_value="0.8"),
            DeclareLaunchArgument(
                "tts_echo_similarity_threshold",
                default_value="0.88",
            ),
            DeclareLaunchArgument("speech_min_confidence", default_value="0.55"),
            DeclareLaunchArgument("speech_max_age_sec", default_value="3.0"),
            DeclareLaunchArgument("speech_source_timeout_sec", default_value="5.0"),
            DeclareLaunchArgument(
                "voice_command_input_mode",
                default_value="sentence_text",
                choices=("sentence_text", "utterance"),
            ),
            DeclareLaunchArgument(
                "voice_command_input_topic",
                default_value="/surgery/voice/resolver_utterance",
                description=(
                    "Deprecated resolver-input alias. The command router is "
                    "the sole /surgery/audio/admitted_utterance subscriber."
                ),
            ),
            DeclareLaunchArgument(
                "voice_command_output_topic",
                default_value="/surgery/voice/proposal",
                description=(
                    "Deprecated resolver-output alias. Proposals return only "
                    "to command_router."
                ),
            ),
            DeclareLaunchArgument(
                "resolver_input_topic",
                # Honour the short-lived legacy argument as the default so
                # historical ``ros2 launch ... voice_command_input_topic:=``
                # invocations either retain their private wiring or fail
                # clearly if they try to attach the resolver to admitted ASR.
                default_value=voice_command_input_topic,
                description="Private command_router -> resolver input.",
            ),
            DeclareLaunchArgument(
                "resolver_output_topic",
                default_value=voice_command_output_topic,
                description="Private resolver -> command_router proposal output.",
            ),
            DeclareLaunchArgument(
                "legacy_voice_intent_topic",
                default_value="/surgery/voice/legacy_disabled",
                description=(
                    "Retired composite-launch intent bus. New voice commands "
                    "are dispatched only by command_router."
                ),
            ),
            DeclareLaunchArgument(
                "command_router_enabled",
                default_value="false",
                description=(
                    "Run the hot-reload direct command router for exact "
                    "catalog commands. It consumes typed, post-echo-guard ASR "
                    "and owns its typed endpoint dispatch."
                ),
            ),
            DeclareLaunchArgument(
                "command_router_catalog_path",
                default_value="",
                description=(
                    "Optional YAML command catalog. Empty uses voice_command's "
                    "installed default catalog."
                ),
            ),
            DeclareLaunchArgument(
                "command_router_catalog_reload_sec",
                default_value="0.5",
            ),
            DeclareLaunchArgument(
                "voice_intent_require_source_metadata",
                default_value="false",
            ),
            DeclareLaunchArgument("voice_intent_max_age_sec", default_value="3.0"),
            DeclareLaunchArgument(
                "voice_intent_future_tolerance_sec", default_value="1.0"
            ),
            DeclareLaunchArgument(
                "voice_intent_dedupe_retention_sec", default_value="120.0"
            ),
            DeclareLaunchArgument(
                "hand_mapping_operator_approved",
                default_value="false",
                description=(
                    "Explicit operator record that the pinned CAM4 Right/palm "
                    "mapping passed its live check. False keeps handover fail-closed."
                ),
            ),
            DeclareLaunchArgument(
                "enable_voice_procedure_control",
                default_value="false",
                description=(
                    "Permit validated typed voice lifecycle proposals to use "
                    "the normal simulation control path."
                ),
            ),
            DeclareLaunchArgument(
                "voice_procedure_accept_missing_confidence",
                default_value="false",
                description=(
                    "Permit a final typed ASR lifecycle command when its "
                    "source explicitly reports that calibrated confidence is "
                    "unavailable. Live opts in; supplied low confidence is "
                    "still rejected."
                ),
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
                "cam3_input_topic",
                default_value=EnvironmentVariable(
                    "CAM3_INPUT_TOPIC",
                    default_value="",
                ),
                description=(
                    "Optional CAM3 RGB input for the local RF-DETR camera "
                    "model. When set, it shares CAM4's checkpoint and model "
                    "instance."
                ),
            ),
            DeclareLaunchArgument(
                "field_image_topic",
                default_value="/surgery/images/field/compressed",
            ),
            DeclareLaunchArgument(
                "rfdetr_flir_output_topic",
                default_value="/surgery/images/field/compressed",
                description=(
                    "RF-DETR segmented FLIR raster for local operator "
                    "display. This is independent from the real VLM's raw "
                    "visual input topic."
                ),
            ),
            # Debug/mock keep the established public default.  Live overrides
            # this with its local RF-DETR-only model-ready image.
            DeclareLaunchArgument(
                "composite_image_topic",
                default_value="/surgery/images/vlm/composite/compressed",
            ),
            DeclareLaunchArgument(
                "flir_overlay_image_topic",
                default_value=(
                    "/surgery/images/flir/segmentation_overlay/compressed"
                ),
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
                        "/taskplanner/internal/rfdetr/cam3/"
                        "detection_overlay/compressed"
                    ),
                ),
                description="Optional local CAM3 RF-DETR transparent overlay.",
            ),
            DeclareLaunchArgument(
                "cam4_semantics_topic",
                default_value="",
            ),
            DeclareLaunchArgument(
                "cam3_tool_observations_topic",
                default_value="",
                description=(
                    "Optional typed CAM3 RF-DETR ToolObservation2DArray "
                    "input for the real VLM."
                ),
            ),
            DeclareLaunchArgument(
                "cam4_tool_observations_topic",
                default_value="",
                description=(
                    "Optional typed CAM4 RF-DETR ToolObservation2DArray "
                    "input for the real VLM."
                ),
            ),
            DeclareLaunchArgument(
                "rfdetr_bridge_cam3_tool_observations_topic",
                default_value=(
                    "/taskplanner/internal/rfdetr/cam_3/tool/observations"
                ),
                description=(
                    "Local HTTP RF-DETR bridge output. Keep distinct from the "
                    "authoritative typed observation input."
                ),
            ),
            DeclareLaunchArgument(
                "rfdetr_bridge_cam4_tool_observations_topic",
                default_value=(
                    "/taskplanner/internal/rfdetr/cam_4/tool/observations"
                ),
                description=(
                    "Local HTTP RF-DETR bridge output. Keep distinct from the "
                    "authoritative typed observation input."
                ),
            ),
            DeclareLaunchArgument(
                "cam3_tool_observations_expected_model_version",
                default_value="",
            ),
            DeclareLaunchArgument(
                "cam4_tool_observations_expected_model_version",
                default_value="",
            ),
            DeclareLaunchArgument(
                "allow_legacy_cam4_semantics_fallback",
                default_value="true",
            ),
            DeclareLaunchArgument("require_field_image", default_value="false"),
            DeclareLaunchArgument(
                "require_rfdetr_applied_field_image",
                default_value="false",
            ),
            DeclareLaunchArgument(
                "require_rfdetr_cam4_overlay",
                default_value="false",
            ),
            DeclareLaunchArgument(
                "enable_integration_preflight_diagnostics",
                default_value="false",
                description=(
                    "Run the optional read-only integration readiness observer. "
                    "Its reports never gate scenario, voice, or dispatch control."
                ),
            ),
            DeclareLaunchArgument(
                "preflight_require_perception",
                default_value="false",
            ),
            DeclareLaunchArgument(
                "preflight_require_rfdetr_tool_observations",
                default_value="false",
                description=(
                    "Include fresh typed CAM3/CAM4 RF-DETR tool observations "
                    "in optional integration diagnostics."
                ),
            ),
            DeclareLaunchArgument(
                "rfdetr_vlm_request_context_topic",
                default_value="/context/vlm_request_context",
                description=(
                    "Read-only VLM request-context topic used only to display "
                    "optional CAM3/CAM4 visual-alignment status."
                ),
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
                        "spec_dir": spec_dir,
                        "publish_free_text": ParameterValue(
                            publish_shared_free_text,
                            value_type=bool,
                        ),
                    }
                ],
                output="screen",
            ),
            Node(
                package="tool_belief_tracker",
                executable="tool_belief_tracker_node",
                name="tool_belief_tracker",
                condition=IfCondition(enable_tool_belief_tracker),
                parameters=[
                    PathJoinSubstitution(
                        [
                            FindPackageShare("tool_belief_tracker"),
                            "config",
                            "default.yaml",
                        ]
                    ),
                    {
                        "spec_dir": spec_dir,
                        "bundle_snapshot_root": bundle_snapshot_root,
                        "cam3_pose_topic": "/perception/cam_3/tool/poses",
                        "cam4_pose_topic": "/perception/cam_4/tool/poses",
                        "cam3_health_topic": "/perception/cam_3/tool/health",
                        "cam4_health_topic": "/perception/cam_4/tool/health",
                        "skill_command_topic": "/bt/skill_command",
                        "skill_status_topic": "/skill/status",
                        "skill_event_topic": "/skill/events",
                        "simulation_state_topic": "/simulation/state",
                        "output_topic": "/surgery/perception/tool_beliefs",
                    }
                ],
                output="screen",
                # This observer owns no control authority. If it fails, only
                # its process is restarted; the planner keeps running.
                respawn=True,
                respawn_delay=1.0,
            ),
            *build_bt_engine_actions(),
            Node(
                package="simulation_runtime",
                executable="speech_input_adapter",
                name="speech_input_adapter",
                parameters=[
                    {
                        "input_mode": speech_input_mode,
                        "input_topic": speech_input_topic,
                        "sentence_input_topic": sentence_input_topic,
                        "output_mode": speech_output_mode,
                        "typed_output_topic": speech_typed_output_topic,
                        "enable_tts_echo_guard": enable_tts_echo_guard,
                        "tts_playback_status_topic": tts_playback_status_topic,
                        "tts_echo_tail_sec": tts_echo_tail_sec,
                        "tts_echo_similarity_threshold": (
                            tts_echo_similarity_threshold
                        ),
                        "min_confidence": speech_min_confidence,
                        "max_age_sec": speech_max_age_sec,
                        "source_timeout_sec": speech_source_timeout_sec,
                    }
                ],
                output="screen",
            ),
            # The sole normal text-to-command boundary. It understands
            # natural Korean paraphrases and publishes one typed intent lane;
            # Digital Twin and BT retain endpoint execution authority.
            Node(
                package="voice_command",
                executable="voice_intent_resolver",
                name="voice_command_resolver",
                condition=IfCondition(voice_intent_resolver_enabled),
                parameters=[
                    {
                        "input_mode": voice_command_input_mode,
                        # Never attach this resolver directly to admitted ASR:
                        # CommandRouter owns that ingress and forwards only
                        # catalog misses to this private channel.
                        "input_topic": resolver_input_topic,
                        "output_topic": resolver_output_topic,
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
            # Exact deterministic commands belong to the lightweight router,
            # not the VLM/DT/BT admission chain. It reads the same typed ASR
            # stream after the ASR-owned TTS echo guard.
            Node(
                package="voice_command",
                executable="command_router",
                name="command_router",
                condition=IfCondition(command_router_enabled),
                parameters=[
                    {
                        "input_topic": speech_typed_output_topic,
                        "resolver_input_topic": resolver_input_topic,
                        "resolver_output_topic": resolver_output_topic,
                        "procedure_bundle": spec_dir,
                        "catalog_path": command_router_catalog_path,
                        "catalog_reload_sec": ParameterValue(
                            command_router_catalog_reload_sec,
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
                        "cam3_tool_observations_topic": (
                            cam3_tool_observations_topic
                        ),
                        "cam4_tool_observations_topic": (
                            cam4_tool_observations_topic
                        ),
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
            Node(
                package="or_digital_twin",
                executable="or_digital_twin",
                name="or_digital_twin",
                parameters=[
                    {
                        "spec_dir": spec_dir,
                        "validation_mode": validation_mode,
                        "vlm_mode": PythonExpression(
                            [
                                "'",
                                vlm_mode,
                                "' if '",
                                procedure_image_vlm_enabled,
                                "'.lower() == 'true' else 'disabled'",
                            ]
                        ),
                        "ngram_prepare_probability_threshold": 0.125,
                        "ngram_recovery_probability_threshold": 0.391,
                        "ngram_recovery_enabled_tools": ["T02", "T08"],
                        "ngram_policy_stability_sec": 0.30,
                        "hand_handover_dwell_sec": 0.300,
                        "hand_handover_release_sec": 0.500,
                        "hand_handover_release_confirm_sec": 0.180,
                        "hand_handover_soft_unknown_grace_sec": 0.180,
                        "hand_handover_max_positive_gap_sec": 0.500,
                        "hand_handover_observation_timeout_sec": 0.500,
                        "hand_handover_minimum_positive_samples": 4,
                        "hand_handover_minimum_palm_up_score": 0.0,
                        "hand_expected_source_frame_id": (
                            "cam_4_color_optical_frame"
                        ),
                        "hand_mapping_operator_approved": ParameterValue(
                            hand_mapping_operator_approved,
                            value_type=bool,
                        ),
                        "require_voice_intent_source_metadata": ParameterValue(
                            voice_intent_require_source_metadata,
                            value_type=bool,
                        ),
                        "voice_intent_max_age_sec": ParameterValue(
                            voice_intent_max_age_sec,
                            value_type=float,
                        ),
                        "voice_intent_future_tolerance_sec": ParameterValue(
                            voice_intent_future_tolerance_sec,
                            value_type=float,
                        ),
                        "voice_intent_dedupe_retention_sec": ParameterValue(
                            voice_intent_dedupe_retention_sec,
                            value_type=float,
                        ),
                        "accept_validation_actor_events": False,
                        "phase_authority": PythonExpression(
                            ["'legacy_estimator' if '", validation_mode, "' == 'demo' else 'reducer'"]
                        ),
                    }
                ],
                output="screen",
            ),
            Node(
                package="surgical_interop_execution",
                executable="fault_action_emulator",
                name="robot_contract_emulator",
                condition=IfCondition(robot_contract_emulator_enabled),
                parameters=[
                    {
                        "profile_path": robot_contract_profile,
                        "procedure_type": ParameterValue(
                            bed_robot_contract_procedure_type,
                            value_type=str,
                        ),
                        "robot_endpoint_source": robot_endpoint_source,
                        "tool_handover_endpoint": tool_handover_endpoint,
                        # This emulator owns only the public external family.
                        # The isolated virtual emulator below owns its own pair.
                        "retraction_service_name": "/surgery/retraction/command",
                        "controller_contract_topic": controller_contract_topic,
                        "controller_contract_id": emulator_contract_id,
                        "capability_policy_id": emulator_capability_policy_id,
                        "publish_bed_robot_status": ParameterValue(
                            PythonExpression(
                                [
                                    "not ('",
                                    retraction_endpoint_source,
                                    "'.strip().lower() == 'virtual')",
                                ]
                            ),
                            value_type=bool,
                        ),
                    }
                ],
                output="screen",
            ),
            # Keep the isolated virtual server available even when Live began
            # on the external controller. It never binds a public /surgery/*
            # name, so stopped-only UI route changes cannot shadow a partner
            # controller or emit physical motion.
            Node(
                package="surgical_interop_execution",
                executable="fault_action_emulator",
                name="virtual_robot_contract_emulator",
                condition=IfCondition(direct_execution_bridge_enabled),
                parameters=[
                    {
                        "profile_path": robot_contract_profile,
                        "procedure_type": ParameterValue(
                            bed_robot_contract_procedure_type,
                            value_type=str,
                        ),
                        "robot_endpoint_source": "virtual",
                        "tool_handover_endpoint": (
                            "/integration/virtual/surgery/tool_handover"
                        ),
                        "retraction_service_name": (
                            "/integration/virtual/surgery/retraction/command"
                        ),
                        "controller_contract_topic": (
                            "/integration/virtual/surgery/controller_contract"
                        ),
                        "controller_contract_id": "taskplanner-virtual-eir-nuc.v1",
                        "capability_policy_id": (
                            "taskplanner-virtual-full-inventory.v1"
                        ),
                        "publish_bed_robot_status": False,
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
                        "robot_endpoint_source": robot_endpoint_source,
                        "retraction_endpoint_source": retraction_endpoint_source,
                        "retraction_state_machine_suppressed": ParameterValue(
                            retraction_state_machine_suppression_enabled,
                            value_type=bool,
                        ),
                        "enable_runtime_route_control": ParameterValue(
                            enable_runtime_route_control,
                            value_type=bool,
                        ),
                        "direct_hand_dispatch_ledger_path": EnvironmentVariable(
                            "TASKPLANNER_DIRECT_HAND_LEDGER_PATH",
                            default_value=(
                                "/tmp/taskplanner-direct-hand-dispatch.sqlite3"
                            ),
                        ),
                        "direct_hand_state_max_age_sec": 1.0,
                        "tool_handover_endpoint": tool_handover_endpoint,
                        # Both endpoint families are declared up front.  The
                        # stopped-only coordinator selects these reviewed
                        # pairs atomically; it never accepts endpoint text
                        # from the browser.
                        "external_tool_handover_endpoint": (
                            "/surgery/tool_handover"
                        ),
                        "virtual_tool_handover_endpoint": (
                            "/integration/virtual/surgery/tool_handover"
                        ),
                        "tool_handover_enabled": ParameterValue(
                            tool_handover_contract_enabled,
                            value_type=bool,
                        ),
                        "retraction_service_name": retraction_service_name,
                        "external_retraction_service_name": (
                            "/surgery/retraction/command"
                        ),
                        "virtual_retraction_service_name": (
                            "/integration/virtual/surgery/retraction/command"
                        ),
                        "bed_robot_status_endpoint": "/external/bed_robot_arms/status",
                        "require_bed_robot_status": ParameterValue(
                            PythonExpression(["False"]), value_type=bool
                        ),
                        "controller_contract_topic": controller_contract_topic,
                        "external_controller_contract_topic": (
                            "/surgery/controller_contract"
                        ),
                        "virtual_controller_contract_topic": (
                            "/integration/virtual/surgery/controller_contract"
                        ),
                        "expected_controller_contract_id": (
                            expected_controller_contract_id
                        ),
                        "external_expected_controller_contract_id": (
                            external_controller_contract_id
                        ),
                        "virtual_expected_controller_contract_id": (
                            "taskplanner-virtual-eir-nuc.v1"
                        ),
                        "expected_capability_policy_id": (
                            expected_capability_policy_id
                        ),
                        "external_expected_capability_policy_id": (
                            external_capability_policy_id
                        ),
                        "virtual_expected_capability_policy_id": (
                            "taskplanner-virtual-full-inventory.v1"
                        ),
                        "external_require_bed_robot_status": False,
                        "external_require_physical_stop_confirmation": (
                            ParameterValue(
                                external_physical_stop_confirmation_required,
                                value_type=bool,
                            )
                        ),
                        "integration_readiness_topic": "/integration/readiness",
                        "require_physical_stop_confirmation": ParameterValue(
                            physical_stop_confirmation_required,
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
                        "require_voice_intent_source_metadata": ParameterValue(
                            voice_intent_require_source_metadata,
                            value_type=bool,
                        ),
                        "voice_intent_max_age_sec": ParameterValue(
                            voice_intent_max_age_sec,
                            value_type=float,
                        ),
                        "voice_intent_future_tolerance_sec": ParameterValue(
                            voice_intent_future_tolerance_sec,
                            value_type=float,
                        ),
                        "voice_intent_dedupe_retention_sec": ParameterValue(
                            voice_intent_dedupe_retention_sec,
                            value_type=float,
                        ),
                        "robot_endpoint_source": robot_endpoint_source,
                        "retraction_endpoint_source": retraction_endpoint_source,
                        "require_bed_robot_status": ParameterValue(
                            PythonExpression(["False"]), value_type=bool
                        ),
                        "suppress_retraction_state_machine": ParameterValue(
                            retraction_state_machine_suppression_enabled,
                            value_type=bool,
                        ),
                    }
                ],
                output="screen",
            ),
            Node(
                package="simulation_runtime",
                executable="integration_preflight",
                name="integration_preflight",
                condition=IfCondition(enable_integration_preflight_diagnostics),
                parameters=[
                    {
                        # Count the actual admitted source. A typed Live ASR
                        # route must never be satisfied by an unrelated legacy
                        # /sensors/surgeon/sentence publisher.
                        "sentence_topic": sentence_input_topic,
                        "speech_source_topic": effective_speech_source_topic,
                        "asr_runtime_status_topic": asr_runtime_status_topic,
                        "require_asr_runtime_status": ParameterValue(
                            require_asr_runtime_status,
                            value_type=bool,
                        ),
                        "asr_runtime_status_max_age_sec": ParameterValue(
                            asr_runtime_status_max_age_sec,
                            value_type=float,
                        ),
                        "asr_runtime_status_source_future_tolerance_sec": (
                            ParameterValue(
                                asr_runtime_status_source_future_tolerance_sec,
                                value_type=float,
                            )
                        ),
                        "spec_dir": spec_dir,
                        "tool_handover_action_name": tool_handover_endpoint,
                        "external_tool_handover_action_name": (
                            "/surgery/tool_handover"
                        ),
                        "virtual_tool_handover_action_name": (
                            "/integration/virtual/surgery/tool_handover"
                        ),
                        "require_tool_handover_action_server": ParameterValue(
                            tool_handover_contract_enabled,
                            value_type=bool,
                        ),
                        "retraction_service_name": retraction_service_name,
                        "external_retraction_service_name": (
                            "/surgery/retraction/command"
                        ),
                        "virtual_retraction_service_name": (
                            "/integration/virtual/surgery/retraction/command"
                        ),
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
                            PythonExpression(["False"]), value_type=bool
                        ),
                        "robot_endpoint_source": robot_endpoint_source,
                        "retraction_endpoint_source": retraction_endpoint_source,
                        "enable_runtime_route_control": ParameterValue(
                            enable_runtime_route_control,
                            value_type=bool,
                        ),
                        # Controller contracts remain bridge/controller
                        # telemetry. This observer reports selected Action and
                        # Service discovery but never admits a scenario start.
                        "require_controller_contract": False,
                        "controller_contract_topic": controller_contract_topic,
                        "external_controller_contract_topic": (
                            "/surgery/controller_contract"
                        ),
                        "virtual_controller_contract_topic": (
                            "/integration/virtual/surgery/controller_contract"
                        ),
                        "expected_controller_contract_id": (
                            expected_controller_contract_id
                        ),
                        "external_expected_controller_contract_id": (
                            external_controller_contract_id
                        ),
                        "virtual_expected_controller_contract_id": (
                            "taskplanner-virtual-eir-nuc.v1"
                        ),
                        "expected_capability_policy_id": (
                            expected_capability_policy_id
                        ),
                        "external_expected_capability_policy_id": (
                            external_capability_policy_id
                        ),
                        "virtual_expected_capability_policy_id": (
                            "taskplanner-virtual-full-inventory.v1"
                        ),
                        "external_require_bed_robot_arm_status": False,
                        "external_require_physical_stop_confirmation": (
                            ParameterValue(
                                external_physical_stop_confirmation_required,
                                value_type=bool,
                            )
                        ),
                        "controller_contract_max_age_sec": ParameterValue(
                            controller_contract_max_age_sec,
                            value_type=float,
                        ),
                        "require_physical_stop_confirmation": ParameterValue(
                            physical_stop_confirmation_required,
                            value_type=bool,
                        ),
                        "retraction_state_machine_suppressed": ParameterValue(
                            retraction_state_machine_suppression_enabled,
                            value_type=bool,
                        ),
                        "require_sentence_publisher": True,
                        "require_perception": ParameterValue(
                            PythonExpression(
                                [
                                    "'",
                                    procedure_perception_enabled,
                                    "'.lower() == 'true' and '",
                                    preflight_require_perception,
                                    "'.lower() in ('true', '1', 'yes')",
                                ]
                            ),
                            value_type=bool,
                        ),
                        "require_rfdetr_tool_observations": ParameterValue(
                            PythonExpression(
                                [
                                    "'",
                                    procedure_perception_enabled,
                                    "'.lower() == 'true' and '",
                                    preflight_require_rfdetr_tool_observations,
                                    "'.lower() in ('true', '1', 'yes')",
                                ]
                            ),
                            value_type=bool,
                        ),
                        "cam3_tool_observations_topic": (
                            cam3_tool_observations_topic
                        ),
                        "cam4_tool_observations_topic": (
                            cam4_tool_observations_topic
                        ),
                        "cam3_tool_observations_expected_model_version": (
                            cam3_tool_observations_expected_model_version
                        ),
                        "cam4_tool_observations_expected_model_version": (
                            cam4_tool_observations_expected_model_version
                        ),
                        "rfdetr_vlm_request_context_topic": (
                            rfdetr_vlm_request_context_topic
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
                        "bundle_snapshot_root": bundle_snapshot_root,
                        "surgeon_actor_mode": surgeon_actor_mode,
                        "manual_override_actor_mute_sec": 8.0,
                        "execution_backend": execution_backend,
                        "enable_runtime_route_control": ParameterValue(
                            enable_runtime_route_control,
                            value_type=bool,
                        ),
                        "enable_voice_procedure_control": ParameterValue(
                            LaunchConfiguration("enable_voice_procedure_control"),
                            value_type=bool,
                        ),
                        # This composite launch no longer consumes resolver
                        # proposals as a second executable voice lane. The
                        # command owner routes correlated proposals directly
                        # to their typed endpoints. Keep the retired input
                        # isolated for historical replay only.
                        "voice_intent_topic": legacy_voice_intent_topic,
                        "voice_procedure_min_confidence": ParameterValue(
                            speech_min_confidence,
                            value_type=float,
                        ),
                        "voice_procedure_accept_missing_confidence": ParameterValue(
                            LaunchConfiguration(
                                "voice_procedure_accept_missing_confidence"
                            ),
                            value_type=bool,
                        ),
                        "voice_procedure_max_age_sec": ParameterValue(
                            voice_intent_max_age_sec,
                            value_type=float,
                        ),
                        "voice_procedure_max_future_skew_sec": ParameterValue(
                            voice_intent_future_tolerance_sec,
                            value_type=float,
                        ),
                        "voice_procedure_dedupe_retention_sec": ParameterValue(
                            voice_intent_dedupe_retention_sec,
                            value_type=float,
                        ),
                    }
                ],
                output="screen",
            ),
            rosbridge_process,
            rosapi_node,
        ]
    )
