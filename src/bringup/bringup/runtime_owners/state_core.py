"""Direct launch wiring for Taskplanner's stateful Twin and BT core owner."""

from __future__ import annotations

import os
from typing import Final

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction, SetLaunchConfiguration
from launch.conditions import IfCondition
from launch.substitutions import EnvironmentVariable, LaunchConfiguration, PathJoinSubstitution, PythonExpression
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue
from launch_ros.substitutions import FindPackageShare

from bringup.runtime_core_launch import build_bt_engine_actions
from bringup.runtime_profile import RUNTIME_PROFILE_NAMES, RuntimeProfileError, resolve_runtime_profile

_STATE_CORE_PROFILE_ARGUMENT_NAMES: Final[tuple[str, ...]] = (
    "default_bundle",
    "execution_backend",
    "robot_endpoint_source",
    "retraction_endpoint_source",
    "enable_runtime_route_control",
    "external_controller_contract_id",
    "external_capability_policy_id",
    "controller_contract_max_age_sec",
    "asr_runtime_status_topic",
    "require_asr_runtime_status",
    "asr_runtime_status_max_age_sec",
    "asr_runtime_status_source_future_tolerance_sec",
    "speech_input_mode",
    "speech_input_topic",
    "sentence_input_topic",
    "speech_min_confidence",
    "legacy_voice_intent_topic",
    "voice_intent_require_source_metadata",
    "voice_intent_max_age_sec",
    "voice_intent_future_tolerance_sec",
    "voice_intent_dedupe_retention_sec",
    "hand_mapping_operator_approved",
    "enable_voice_procedure_control",
    "voice_procedure_accept_missing_confidence",
    "vlm_mode",
    "surgeon_actor_mode",
    "enable_integration_preflight_diagnostics",
    "preflight_require_perception",
    "preflight_require_rfdetr_tool_observations",
    "rfdetr_vlm_request_context_topic",
    "preflight_require_metric_3d",
    "perception_backend",
    "cam3_tool_observations_topic",
    "cam4_tool_observations_topic",
    "cam3_tool_observations_expected_model_version",
    "cam4_tool_observations_expected_model_version",
    "cv_contract_status_topic",
)


def _state_core_profile_actions(context) -> list[SetLaunchConfiguration]:
    """Resolve only the stateful Twin/BT owner's profile surface.

    The previous state-core projection inherited every declaration from the
    retained monolith, which made a scoped core restart parse camera, VLM,
    browser, and command-owner configuration.  This deliberately keeps the
    small set actually consumed by the Twin, lifecycle manager, optional
    diagnostic observer, and bed-group coordinator.
    """

    profile_name = LaunchConfiguration("runtime_profile").perform(context)
    try:
        profile = resolve_runtime_profile(profile_name, environment=os.environ)
    except RuntimeProfileError as exc:
        raise RuntimeError(str(exc)) from exc
    values = profile.arguments_for("state-core")
    return [
        SetLaunchConfiguration(name, values[name])
        for name in _STATE_CORE_PROFILE_ARGUMENT_NAMES
        if name in values
    ]


def _state_core_actions() -> list[object]:
    """Create the Twin, lifecycle, BT, and optional diagnostics directly.

    The order and node parameters mirror the retained mock launch.  Keeping
    this owner self-contained lets a same-mode core restart avoid loading the
    large legacy graph without changing controller route or lifecycle
    semantics.
    """

    default_bundle = LaunchConfiguration("default_bundle")
    spec_dir = LaunchConfiguration("spec_dir")
    bundle_snapshot_root = LaunchConfiguration("bundle_snapshot_root")
    validation_mode = LaunchConfiguration("validation_mode")
    vlm_mode = LaunchConfiguration("vlm_mode")
    execution_backend = LaunchConfiguration("execution_backend")
    robot_endpoint_source = LaunchConfiguration("robot_endpoint_source")
    retraction_endpoint_source = LaunchConfiguration("retraction_endpoint_source")
    enable_runtime_route_control = LaunchConfiguration("enable_runtime_route_control")
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
    bed_robot_contract_enabled = LaunchConfiguration("bed_robot_contract_enabled")
    bed_robot_contract_procedure_type = LaunchConfiguration(
        "bed_robot_contract_procedure_type"
    )
    tool_handover_contract_enabled = LaunchConfiguration(
        "tool_handover_contract_enabled"
    )
    procedure_image_vlm_enabled = LaunchConfiguration(
        "procedure_image_vlm_enabled"
    )
    procedure_perception_enabled = LaunchConfiguration(
        "procedure_perception_enabled"
    )
    retraction_workflow_state_enforced = LaunchConfiguration(
        "retraction_workflow_state_enforced"
    )
    speech_input_mode = LaunchConfiguration("speech_input_mode")
    speech_input_topic = LaunchConfiguration("speech_input_topic")
    sentence_input_topic = LaunchConfiguration("sentence_input_topic")
    speech_min_confidence = LaunchConfiguration("speech_min_confidence")
    legacy_voice_intent_topic = LaunchConfiguration("legacy_voice_intent_topic")
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
    enable_voice_procedure_control = LaunchConfiguration(
        "enable_voice_procedure_control"
    )
    voice_procedure_accept_missing_confidence = LaunchConfiguration(
        "voice_procedure_accept_missing_confidence"
    )
    surgeon_actor_mode = LaunchConfiguration("surgeon_actor_mode")
    enable_integration_preflight_diagnostics = LaunchConfiguration(
        "enable_integration_preflight_diagnostics"
    )
    preflight_require_perception = LaunchConfiguration("preflight_require_perception")
    preflight_require_rfdetr_tool_observations = LaunchConfiguration(
        "preflight_require_rfdetr_tool_observations"
    )
    rfdetr_vlm_request_context_topic = LaunchConfiguration(
        "rfdetr_vlm_request_context_topic"
    )
    preflight_require_metric_3d = LaunchConfiguration("preflight_require_metric_3d")
    perception_backend = LaunchConfiguration("perception_backend")
    cam3_tool_observations_topic = LaunchConfiguration("cam3_tool_observations_topic")
    cam4_tool_observations_topic = LaunchConfiguration("cam4_tool_observations_topic")
    cam3_tool_observations_expected_model_version = LaunchConfiguration(
        "cam3_tool_observations_expected_model_version"
    )
    cam4_tool_observations_expected_model_version = LaunchConfiguration(
        "cam4_tool_observations_expected_model_version"
    )
    cv_contract_status_topic = LaunchConfiguration("cv_contract_status_topic")

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
    retraction_state_machine_suppression_enabled = PythonExpression(
        [
            "'",
            retraction_endpoint_source,
            "'.strip().lower() == 'virtual' or '",
            retraction_workflow_state_enforced,
            "'.strip().lower() != 'true'",
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
    external_physical_stop_confirmation_required = PythonExpression(
        [
            "'",
            execution_backend,
            "'.strip().lower() == 'external'",
        ]
    )

    return [
        *build_bt_engine_actions(),
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
                    "hand_expected_source_frame_id": "cam_4_color_optical_frame",
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
                        [
                            "'legacy_estimator' if '",
                            validation_mode,
                            "' == 'demo' else 'reducer'",
                        ]
                    ),
                }
            ],
            output="screen",
        ),
        Node(
            package="bt_orchestrator",
            executable="decision_bridge",
            name="bt_decision_bridge",
            parameters=[
                {"target_node_name": "/tree_executor", "mirror_period_sec": 0.2}
            ],
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
                    "asr_runtime_status_source_future_tolerance_sec": ParameterValue(
                        asr_runtime_status_source_future_tolerance_sec,
                        value_type=float,
                    ),
                    "spec_dir": spec_dir,
                    "tool_handover_action_name": tool_handover_endpoint,
                    "external_tool_handover_action_name": "/surgery/tool_handover",
                    "virtual_tool_handover_action_name": (
                        "/integration/virtual/surgery/tool_handover"
                    ),
                    "require_tool_handover_action_server": ParameterValue(
                        tool_handover_contract_enabled,
                        value_type=bool,
                    ),
                    "retraction_service_name": retraction_service_name,
                    "external_retraction_service_name": "/surgery/retraction/command",
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
                    "require_controller_contract": False,
                    "controller_contract_topic": controller_contract_topic,
                    "external_controller_contract_topic": "/surgery/controller_contract",
                    "virtual_controller_contract_topic": (
                        "/integration/virtual/surgery/controller_contract"
                    ),
                    "expected_controller_contract_id": expected_controller_contract_id,
                    "external_expected_controller_contract_id": (
                        external_controller_contract_id
                    ),
                    "virtual_expected_controller_contract_id": (
                        "taskplanner-virtual-eir-nuc.v1"
                    ),
                    "expected_capability_policy_id": expected_capability_policy_id,
                    "external_expected_capability_policy_id": (
                        external_capability_policy_id
                    ),
                    "virtual_expected_capability_policy_id": (
                        "taskplanner-virtual-full-inventory.v1"
                    ),
                    "external_require_bed_robot_arm_status": False,
                    "external_require_physical_stop_confirmation": ParameterValue(
                        external_physical_stop_confirmation_required,
                        value_type=bool,
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
                    "cam3_tool_observations_topic": cam3_tool_observations_topic,
                    "cam4_tool_observations_topic": cam4_tool_observations_topic,
                    "cam3_tool_observations_expected_model_version": (
                        cam3_tool_observations_expected_model_version
                    ),
                    "cam4_tool_observations_expected_model_version": (
                        cam4_tool_observations_expected_model_version
                    ),
                    "rfdetr_vlm_request_context_topic": rfdetr_vlm_request_context_topic,
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
                        enable_voice_procedure_control,
                        value_type=bool,
                    ),
                    # Resolver proposals are private to command_router.  Do
                    # not let this state owner subscribe to them as a second
                    # executable command lane during a core restart.
                    "voice_intent_topic": legacy_voice_intent_topic,
                    "voice_procedure_min_confidence": ParameterValue(
                        speech_min_confidence,
                        value_type=float,
                    ),
                    "voice_procedure_accept_missing_confidence": ParameterValue(
                        voice_procedure_accept_missing_confidence,
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
    ]


def _generate_state_core_launch_description() -> LaunchDescription:
    """Build the stateful runtime core without parsing the legacy graph."""

    default_bundle = LaunchConfiguration("default_bundle")
    spec_default = PathJoinSubstitution(
        [FindPackageShare("procedure_spec"), "specs", default_bundle]
    )
    return LaunchDescription(
        [
            DeclareLaunchArgument(
                "runtime_profile",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_RUNTIME_MODE", default_value="mock"
                ),
                choices=RUNTIME_PROFILE_NAMES,
                description="Mode-level state-core defaults.",
            ),
            OpaqueFunction(function=lambda context: _state_core_profile_actions(context)),
            DeclareLaunchArgument(
                "default_bundle",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_DEFAULT_BUNDLE", default_value="thyroidectomy"
                ),
            ),
            DeclareLaunchArgument("spec_dir", default_value=spec_default),
            # Runtime capabilities are owner-local deployment settings, not
            # selected-scenario topology. ScenarioStore can change a bundle
            # without rebuilding this launch graph or restarting state-core.
            DeclareLaunchArgument(
                "bed_robot_contract_enabled",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_CAPABILITY_BED_ROBOT", default_value="true"
                ),
            ),
            DeclareLaunchArgument(
                "bed_robot_contract_procedure_type",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_CAPABILITY_PROCEDURE_TYPE",
                    default_value="thyroidectomy",
                ),
            ),
            DeclareLaunchArgument(
                "tool_handover_contract_enabled",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_CAPABILITY_TOOL_HANDOVER", default_value="true"
                ),
            ),
            DeclareLaunchArgument(
                "procedure_image_vlm_enabled",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_CAPABILITY_IMAGE_VLM", default_value="true"
                ),
            ),
            DeclareLaunchArgument(
                "procedure_perception_enabled",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_CAPABILITY_PERCEPTION", default_value="true"
                ),
            ),
            DeclareLaunchArgument(
                "retraction_workflow_state_enforced",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_CAPABILITY_RETRACTION_WORKFLOW_STATE",
                    default_value="false",
                ),
            ),
            DeclareLaunchArgument(
                "bundle_snapshot_root",
                default_value="/tmp/taskplanner-procedure-snapshots",
            ),
            DeclareLaunchArgument("validation_mode", default_value="bt_twin"),
            DeclareLaunchArgument("vlm_mode", default_value="real"),
            DeclareLaunchArgument("execution_backend", default_value="mock"),
            DeclareLaunchArgument(
                "robot_endpoint_source",
                default_value="external",
                choices=("external", "virtual"),
            ),
            DeclareLaunchArgument(
                "retraction_endpoint_source",
                default_value=LaunchConfiguration("robot_endpoint_source"),
                choices=("external", "virtual"),
            ),
            DeclareLaunchArgument("enable_runtime_route_control", default_value="false"),
            DeclareLaunchArgument(
                "external_controller_contract_id",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_EXTERNAL_CONTROLLER_CONTRACT_ID",
                    default_value="eir-nuc-tool-handover.real.v1",
                ),
            ),
            DeclareLaunchArgument(
                "external_capability_policy_id",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_EXTERNAL_CAPABILITY_POLICY_ID",
                    default_value="eir-nuc-tool-handover.v1",
                ),
            ),
            DeclareLaunchArgument("controller_contract_max_age_sec", default_value="3.0"),
            DeclareLaunchArgument(
                "asr_runtime_status_topic",
                default_value="/input/asr/runtime_status",
            ),
            DeclareLaunchArgument("require_asr_runtime_status", default_value="false"),
            DeclareLaunchArgument("asr_runtime_status_max_age_sec", default_value="3.0"),
            DeclareLaunchArgument(
                "asr_runtime_status_source_future_tolerance_sec",
                default_value="0.5",
            ),
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
                "legacy_voice_intent_topic",
                default_value="/surgery/voice/legacy_disabled",
                description=(
                    "Retired composite voice bus. New voice commands are "
                    "owned by the command router."
                ),
            ),
            DeclareLaunchArgument("speech_min_confidence", default_value="0.55"),
            DeclareLaunchArgument(
                "voice_intent_require_source_metadata",
                default_value="false",
            ),
            DeclareLaunchArgument("voice_intent_max_age_sec", default_value="3.0"),
            DeclareLaunchArgument(
                "voice_intent_future_tolerance_sec",
                default_value="1.0",
            ),
            DeclareLaunchArgument(
                "voice_intent_dedupe_retention_sec",
                default_value="120.0",
            ),
            DeclareLaunchArgument(
                "hand_mapping_operator_approved",
                default_value="false",
            ),
            DeclareLaunchArgument(
                "enable_voice_procedure_control",
                default_value="false",
            ),
            DeclareLaunchArgument(
                "voice_procedure_accept_missing_confidence",
                default_value="false",
            ),
            DeclareLaunchArgument("surgeon_actor_mode", default_value="llm"),
            DeclareLaunchArgument(
                "enable_integration_preflight_diagnostics",
                default_value="false",
            ),
            DeclareLaunchArgument("preflight_require_perception", default_value="false"),
            DeclareLaunchArgument(
                "preflight_require_rfdetr_tool_observations",
                default_value="false",
            ),
            DeclareLaunchArgument(
                "rfdetr_vlm_request_context_topic",
                default_value="/context/vlm_request_context",
            ),
            DeclareLaunchArgument("preflight_require_metric_3d", default_value="false"),
            DeclareLaunchArgument(
                "perception_backend",
                default_value=EnvironmentVariable(
                    "PERCEPTION_BACKEND", default_value="local"
                ),
            ),
            DeclareLaunchArgument("cam3_tool_observations_topic", default_value=""),
            DeclareLaunchArgument("cam4_tool_observations_topic", default_value=""),
            DeclareLaunchArgument(
                "cam3_tool_observations_expected_model_version",
                default_value="",
            ),
            DeclareLaunchArgument(
                "cam4_tool_observations_expected_model_version",
                default_value="",
            ),
            DeclareLaunchArgument(
                "cv_contract_status_topic",
                default_value="/integration/cv_contract/status",
            ),
            *_state_core_actions(),
        ]
    )


