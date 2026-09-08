"""Bring up the fail-closed external Taskplanner integration runtime."""

from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument,
    IncludeLaunchDescription,
    OpaqueFunction,
)
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

from bringup.perception_config import resolve_launch_perception


# RF-DETR overlays are local-only operator rasters.  The VLM itself consumes
# raw visual panels plus typed CAM3/CAM4 ``ToolObservation2DArray`` evidence;
# no detector overlay/segmentation raster is used as a detector-input proxy.
# The public gateway exposes only raw FLIR/CAM4 aliases, so these names must
# never be supplied by generic overlay settings.
LIVE_RFDETR_FLIR_SEGMENTED_TOPIC = (
    "/taskplanner/internal/rfdetr/flir/segmented/compressed"
)
LIVE_RFDETR_FLIR_OVERLAY_TOPIC = (
    "/taskplanner/internal/rfdetr/flir/segmentation_overlay/compressed"
)
LIVE_RFDETR_CAM4_OVERLAY_TOPIC = (
    "/taskplanner/internal/rfdetr/cam4/detection_overlay/compressed"
)
LIVE_RFDETR_CAM3_OVERLAY_TOPIC = (
    "/taskplanner/internal/rfdetr/cam3/detection_overlay/compressed"
)
LIVE_RFDETR_CAM3_TOOL_OBSERVATIONS_TOPIC = (
    "/perception/cam_3/tool/observations"
)
LIVE_RFDETR_CAM4_TOOL_OBSERVATIONS_TOPIC = (
    "/perception/cam_4/tool/observations"
)
LIVE_RFDETR_BRIDGE_CAM3_TOOL_OBSERVATIONS_TOPIC = (
    "/taskplanner/internal/rfdetr/cam_3/tool/observations"
)
LIVE_RFDETR_BRIDGE_CAM4_TOOL_OBSERVATIONS_TOPIC = (
    "/taskplanner/internal/rfdetr/cam_4/tool/observations"
)
# Production observes model provenance but does not pin a deployment version.
# A non-empty environment override remains available for incident isolation.
LIVE_RFDETR_MODEL_VERSION_PIN = ""
LIVE_VLM_MODEL_VISUAL_TOPIC = (
    "/taskplanner/internal/vlm/model_visual/compressed"
)
LIVE_VLM_MODE = "real"
LIVE_VLM_BASE_URL = "http://127.0.0.1:8080"
LIVE_VLM_PROVIDER_ID = "ninfer"
LIVE_VLM_MODEL_ID = "qwen3.6-35b-a3b"


def _env(name: str, default: str) -> EnvironmentVariable:
    return EnvironmentVariable(name, default_value=default)


def generate_launch_description() -> LaunchDescription:
    vlm_mode = LaunchConfiguration("vlm_mode")
    vlm_base_url = LaunchConfiguration("vlm_base_url")
    vlm_provider_id = LaunchConfiguration("vlm_provider_id")
    vlm_model_id = LaunchConfiguration("vlm_model_id")
    publish_shared_state = LaunchConfiguration("publish_shared_state")
    publish_shared_free_text = LaunchConfiguration("publish_shared_free_text")
    publish_camera_aliases = LaunchConfiguration("publish_camera_aliases")
    publish_flir_while_idle = LaunchConfiguration("publish_flir_while_idle")
    perception_backend = LaunchConfiguration("perception_backend")
    perception_provider = LaunchConfiguration("perception_provider")
    perception_location = LaunchConfiguration("perception_location")
    perception_endpoint = LaunchConfiguration("perception_endpoint")
    default_bundle = LaunchConfiguration("default_bundle")
    speech_input_mode = LaunchConfiguration("speech_input_mode")
    speech_input_topic = LaunchConfiguration("speech_input_topic")
    sentence_input_topic = LaunchConfiguration("sentence_input_topic")
    robot_endpoint_source = LaunchConfiguration("robot_endpoint_source")
    retraction_endpoint_source = LaunchConfiguration("retraction_endpoint_source")
    external_controller_contract_id = LaunchConfiguration(
        "external_controller_contract_id"
    )
    external_capability_policy_id = LaunchConfiguration(
        "external_capability_policy_id"
    )
    controller_contract_max_age_sec = LaunchConfiguration(
        "controller_contract_max_age_sec"
    )
    # Procedure-specific perception diagnostics are decided by
    # integration_preflight from the *active* selected bundle. This launch-time
    # flag is only the explicit global opt-in for other Live procedures; tying
    # it to default_bundle would leave a safely switched stopped demo with the
    # wrong diagnostic requirements.
    global_perception_required = PythonExpression(
        [
            "'",
            _env("REQUIRE_PERCEPTION_ON_START", "false"),
            "'.lower() in ('true', '1', 'yes')",
        ]
    )
    # Keep the typed RF-DETR contract visible to the external Production
    # diagnostic observer when the process initially starts on a different
    # bundle. The observer applies it only after the operator selects the
    # thyroidectomy demo; otherwise a stopped-state switch could silently
    # inherit no CAM3/CAM4 location diagnostic.
    structured_rfdetr_tool_observations_enabled = "true"
    # Keep the read-only relay alive whenever its public contract is enabled.
    # It opens native streams only for a fresh, active and locally validated
    # selected bundle, so a stopped-state bundle switch remains supportable
    # even if Live was launched with a different initial bundle.
    camera_aliases_enabled = PythonExpression(
        [
            "'",
            publish_camera_aliases,
            "'.lower() in ('true', '1', 'yes')",
        ]
    )
    flir_input_topic = _env(
        "FLIR_INPUT_TOPIC",
        "/synced/flir/color/image_raw/compressed",
    )
    cam4_input_topic = _env(
        "CAM4_INPUT_TOPIC",
        "/synced/cam_4/color/image_raw/compressed",
    )
    cam3_input_topic = _env(
        "CAM3_INPUT_TOPIC",
        "/synced/cam_3/color/image_raw/compressed",
    )
    # Production never creates a detector process or HTTP adapter.  The
    # reviewed 192.168.1.7 runtime owns inference and publishes typed DDS.
    local_perception_adapter_enabled = "false"
    base_launch = PythonLaunchDescriptionSource(
        PathJoinSubstitution(
            [FindPackageShare("bringup"), "launch", "taskplanner_mock.launch.py"]
        )
    )
    return LaunchDescription(
        [
            DeclareLaunchArgument(
                "default_bundle",
                default_value=_env(
                    "TASKPLANNER_LIVE_DEFAULT_BUNDLE",
                    "thyroidectomy_demo",
                ),
                description=(
                    "Procedure bundle selected for the live runtime. An explicit "
                    "default_bundle:= argument still takes precedence."
                ),
            ),
            DeclareLaunchArgument(
                "speech_input_mode",
                default_value=_env("SPEECH_INPUT_MODE", "tagged_sentence"),
                choices=("utterance", "tagged_sentence"),
                description=(
                    "Select the existing typed microphone ASR or the external "
                    "[partial]/[final] sentence topic input."
                ),
            ),
            DeclareLaunchArgument(
                "speech_input_topic",
                default_value=_env(
                    "ASR_UTTERANCE_TOPIC", "/sensors/speech/utterance"
                ),
            ),
            DeclareLaunchArgument(
                "sentence_input_topic",
                default_value=_env(
                    "SENTENCE_INPUT_TOPIC", "/sensors/surgeon/sentence"
                ),
            ),
            DeclareLaunchArgument(
                "vlm_mode",
                default_value=LIVE_VLM_MODE,
                choices=(LIVE_VLM_MODE,),
                description="Production always runs the reviewed real VLM path.",
            ),
            DeclareLaunchArgument(
                "vlm_base_url",
                default_value=LIVE_VLM_BASE_URL,
                choices=(LIVE_VLM_BASE_URL,),
                description=(
                    "Production NInfer manager endpoint. Alternate model servers "
                    "belong to Lab."
                ),
            ),
            DeclareLaunchArgument(
                "vlm_provider_id",
                default_value=LIVE_VLM_PROVIDER_ID,
                choices=(LIVE_VLM_PROVIDER_ID,),
                description="Production is fixed to the NInfer provider.",
            ),
            DeclareLaunchArgument(
                "vlm_model_id",
                default_value=LIVE_VLM_MODEL_ID,
                choices=(LIVE_VLM_MODEL_ID,),
                description="Production is fixed to the reviewed 35B A3B model.",
            ),
            DeclareLaunchArgument(
                "robot_endpoint_source",
                default_value=_env("TASKPLANNER_ROBOT_ENDPOINT_SOURCE", "external"),
                choices=("external", "virtual"),
                description=(
                    "Launch-lifetime controller route. virtual uses only the "
                    "isolated in-process emulator endpoints."
                ),
            ),
            DeclareLaunchArgument(
                "retraction_endpoint_source",
                default_value=_env(
                    "TASKPLANNER_RETRACTION_ENDPOINT_SOURCE",
                    _env("TASKPLANNER_ROBOT_ENDPOINT_SOURCE", "external"),
                ),
                choices=("external", "virtual"),
                description=(
                    "Launch default for the retraction Service route. Runtime "
                    "selection remains stopped-only and independently bounded."
                ),
            ),
            DeclareLaunchArgument(
                "external_controller_contract_id",
                default_value=_env(
                    "TASKPLANNER_EXTERNAL_CONTROLLER_CONTRACT_ID",
                    "eir-nuc-tool-handover.real.v1",
                ),
                description=(
                    "Expected ID in the external controller's read-only contract "
                    "manifest for route diagnostics; it does not gate start or "
                    "dispatch."
                ),
            ),
            DeclareLaunchArgument(
                "external_capability_policy_id",
                default_value=_env(
                    "TASKPLANNER_EXTERNAL_CAPABILITY_POLICY_ID",
                    "eir-nuc-tool-handover.v1",
                ),
                description=(
                    "Exact reviewed handover capability policy expected from the "
                    "external controller manifest."
                ),
            ),
            DeclareLaunchArgument(
                "controller_contract_max_age_sec",
                default_value=_env("CONTROLLER_CONTRACT_MAX_AGE_SEC", "3.0"),
                description=(
                    "Legacy compatibility setting used for controller diagnostics "
                    "only; it never authorizes or blocks dispatch."
                ),
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
                "publish_flir_while_idle",
                default_value=_env("PUBLISH_FLIR_WHILE_IDLE", "false"),
                description=(
                    "Allow only the read-only public FLIR alias before a "
                    "procedure run becomes active. CAM4 remains gated."
                ),
            ),
            DeclareLaunchArgument(
                "perception_backend",
                default_value=_env("PERCEPTION_BACKEND", "external"),
                choices=("external",),
                description=(
                    "Production is fixed to externally owned typed DDS input."
                ),
            ),
            DeclareLaunchArgument(
                "perception_provider",
                default_value=_env(
                    "PERCEPTION_PROVIDER", "external_rfdetr_topics"
                ),
                choices=("external_rfdetr_topics",),
                description=(
                    "Production provider; local/PNU adapters belong to Lab."
                ),
            ),
            DeclareLaunchArgument(
                "perception_location",
                default_value=_env("PERCEPTION_LOCATION", "remote"),
                choices=("remote",),
                description=(
                    "External RF-DETR placement. No automatic fallback."
                ),
            ),
            DeclareLaunchArgument(
                "perception_endpoint",
                default_value=_env("PERCEPTION_ENDPOINT", ""),
                description=(
                    "Must remain empty: typed DDS has no HTTP worker endpoint."
                ),
            ),
            DeclareLaunchArgument(
                "rfdetr_service_url",
                default_value=_env(
                    "RFDETR_SERVICE_URL",
                    "",
                ),
                description="Deprecated alias; empty in Production.",
            ),
            OpaqueFunction(function=resolve_launch_perception),
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
                    # Read-only probabilistic Real-to-sim projection. It is
                    # deliberately absent from preflight and dispatch admission.
                    "enable_tool_belief_tracker": "true",
                    "publish_shared_state": publish_shared_state,
                    "publish_shared_free_text": publish_shared_free_text,
                    "execution_backend": "external",
                    # The operator's 2026-08-26 live CAM4 check accepted the
                    # pinned forced-Right/palm-facing mapping. The base launch
                    # remains fail-closed by default for mock/replay callers.
                    "hand_mapping_operator_approved": "true",
                    "robot_endpoint_source": robot_endpoint_source,
                    "retraction_endpoint_source": retraction_endpoint_source,
                    "enable_runtime_route_control": "true",
                    "external_controller_contract_id": (
                        external_controller_contract_id
                    ),
                    "external_capability_policy_id": (
                        external_capability_policy_id
                    ),
                    "controller_contract_max_age_sec": (
                        controller_contract_max_age_sec
                    ),
                    "speech_input_mode": speech_input_mode,
                    "speech_input_topic": speech_input_topic,
                    "sentence_input_topic": sentence_input_topic,
                    "speech_output_mode": "typed_utterance",
                    "speech_typed_output_topic": (
                        "/surgery/audio/admitted_utterance"
                    ),
                    # The local speaker is physically audible to the ASR
                    # microphone. Track typed playback status so a robot reply
                    # cannot be admitted again as a surgeon command.
                    "enable_tts_echo_guard": "true",
                    "tts_playback_status_topic": "/tts/playback_status",
                    "tts_echo_tail_sec": _env("TTS_ECHO_TAIL_SEC", "0.8"),
                    "tts_echo_similarity_threshold": _env(
                        "TTS_ECHO_SIMILARITY_THRESHOLD", "0.88"
                    ),
                    "voice_command_input_mode": "utterance",
                    # CommandRouter owns the admitted-ASR subscription.  The
                    # resolver sees only a router-forwarded catalog miss and
                    # returns a private proposal to that same router.
                    "voice_command_input_topic": (
                        "/surgery/voice/resolver_utterance"
                    ),
                    "resolver_input_topic": "/surgery/voice/resolver_utterance",
                    "resolver_output_topic": "/surgery/voice/proposal",
                    # Exact catalog commands (currently suction) use the
                    # lightweight direct router. They do not wait for a VLM
                    # function call, DT receipt, or BT reconstruction.
                    "command_router_enabled": "true",
                    "command_router_catalog_path": _env(
                        "COMMAND_ROUTER_CATALOG_PATH", ""
                    ),
                    "command_router_catalog_reload_sec": _env(
                        "COMMAND_ROUTER_CATALOG_RELOAD_SEC", "0.5"
                    ),
                    # Compatibility alias for the resolver proposal channel.
                    # No legacy composite consumer executes this output.
                    "voice_command_output_topic": "/surgery/voice/proposal",
                    "legacy_voice_intent_topic": (
                        "/surgery/voice/legacy_disabled"
                    ),
                    "enable_voice_procedure_control": "true",
                    # The operational ASR publishes an explicit
                    # has_confidence=false when its backend has no calibrated
                    # score. Live accepts that honest absence while the typed
                    # final/surgeon/fresh/procedure gates remain mandatory.
                    "voice_procedure_accept_missing_confidence": "true",
                    "voice_intent_require_source_metadata": "true",
                    "require_asr_runtime_status": "true",
                    "asr_runtime_status_topic": "/input/asr/runtime_status",
                    "asr_runtime_status_max_age_sec": _env(
                        "ASR_RUNTIME_STATUS_MAX_AGE_SEC",
                        "3.0",
                    ),
                    "asr_runtime_status_source_future_tolerance_sec": _env(
                        "ASR_RUNTIME_STATUS_SOURCE_FUTURE_TOLERANCE_SEC",
                        "0.5",
                    ),
                    # Natural-language resolution is intentionally local and
                    # deterministic in Live.  No raw transcript reaches
                    # execution and model availability cannot delay it.
                    "voice_command_selector_mode": "deterministic",
                    "vlm_mode": vlm_mode,
                    "vlm_base_url": vlm_base_url,
                    "vlm_provider_id": vlm_provider_id,
                    "vlm_model_id": vlm_model_id,
                    "vlm_api_mode": "openai_compat",
                    "vlm_publish_period_sec": _env(
                        "VLM_PUBLISH_PERIOD_SEC",
                        "1.0",
                    ),
                    "vlm_image_stale_sec": _env("VLM_IMAGE_STALE_SEC", "3.0"),
                    # Live visual admission tracks source frame time, rather
                    # than a newly received replayed image/health message.
                    "vlm_require_source_frame_timestamp": "true",
                    "vlm_model_input_max_source_lag_sec": "1.0",
                    "vlm_model_input_max_source_future_skew_sec": "0.25",
                    "vlm_max_output_tokens": _env(
                        "VLM_MAX_OUTPUT_TOKENS",
                        "384",
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
                    "enable_rfdetr_perception": local_perception_adapter_enabled,
                    "perception_backend": perception_backend,
                    "perception_provider": perception_provider,
                    "perception_location": perception_location,
                    "perception_endpoint": perception_endpoint,
                    "rfdetr_service_url": perception_endpoint,
                    "flir_input_topic": flir_input_topic,
                    "cam4_input_topic": cam4_input_topic,
                    "cam3_input_topic": cam3_input_topic,
                    # The model receives raw FLIR/CAM4 visual panels. RF-DETR
                    # tool geometry reaches it only through the typed CAM3/4
                    # observation topics below; transparent overlays remain
                    # a separate low-latency operator-display layer.
                    "field_image_topic": flir_input_topic,
                    "rfdetr_flir_output_topic": (
                        LIVE_RFDETR_FLIR_SEGMENTED_TOPIC
                    ),
                    "flir_overlay_image_topic": LIVE_RFDETR_FLIR_OVERLAY_TOPIC,
                    "cam4_overlay_image_topic": LIVE_RFDETR_CAM4_OVERLAY_TOPIC,
                    "cam3_overlay_image_topic": LIVE_RFDETR_CAM3_OVERLAY_TOPIC,
                    # This is a raw visual composite emitted by ``real_vlm``
                    # for local observability, not RF-DETR detector evidence.
                    "composite_image_topic": LIVE_VLM_MODEL_VISUAL_TOPIC,
                    "cam4_semantics_topic": _env(
                        "CAM4_SEMANTICS_TOPIC",
                        "/surgery/perception/cam4/semantics/json",
                    ),
                    "cam3_tool_observations_topic": _env(
                        "CAM3_TOOL_OBSERVATIONS_TOPIC",
                        LIVE_RFDETR_CAM3_TOOL_OBSERVATIONS_TOPIC,
                    ),
                    "cam4_tool_observations_topic": _env(
                        "CAM4_TOOL_OBSERVATIONS_TOPIC",
                        LIVE_RFDETR_CAM4_TOOL_OBSERVATIONS_TOPIC,
                    ),
                    # The shared base launch still declares Lab bridge topic
                    # arguments, but Production hard-disables that bridge.
                    # Keep its unused names private so a later Lab refactor
                    # cannot collide with the authoritative external topics.
                    "rfdetr_bridge_cam3_tool_observations_topic": (
                        LIVE_RFDETR_BRIDGE_CAM3_TOOL_OBSERVATIONS_TOPIC
                    ),
                    "rfdetr_bridge_cam4_tool_observations_topic": (
                        LIVE_RFDETR_BRIDGE_CAM4_TOOL_OBSERVATIONS_TOPIC
                    ),
                    "cam3_tool_observations_expected_model_version": _env(
                        "CAM3_TOOL_OBSERVATIONS_EXPECTED_MODEL_VERSION",
                        LIVE_RFDETR_MODEL_VERSION_PIN,
                    ),
                    "cam4_tool_observations_expected_model_version": _env(
                        "CAM4_TOOL_OBSERVATIONS_EXPECTED_MODEL_VERSION",
                        LIVE_RFDETR_MODEL_VERSION_PIN,
                    ),
                    "allow_legacy_cam4_semantics_fallback": "false",
                    "require_field_image": "false",
                    "require_rfdetr_applied_field_image": "false",
                    "require_rfdetr_cam4_overlay": "false",
                    "enable_integration_preflight_diagnostics": _env(
                        "ENABLE_INTEGRATION_PREFLIGHT_DIAGNOSTICS",
                        "true",
                    ),
                    "preflight_require_perception": global_perception_required,
                    "preflight_require_rfdetr_tool_observations": (
                        structured_rfdetr_tool_observations_enabled
                    ),
                    "rfdetr_vlm_request_context_topic": _env(
                        "RFDETR_VLM_REQUEST_CONTEXT_TOPIC",
                        "/context/vlm_request_context",
                    ),
                    "preflight_require_metric_3d": "false",
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
                    "cv_cam4_native_depth_compressed_topic": _env(
                        "CV_CAM4_NATIVE_DEPTH_COMPRESSED_TOPIC",
                        "/synced/cam_4/depth/image_rect_raw/compressedDepth",
                    ),
                    "cv_cam4_depth_camera_info_topic": _env(
                        "CV_CAM4_DEPTH_CAMERA_INFO_TOPIC",
                        "/synced/cam_4/depth/camera_info",
                    ),
                    "cv_cam4_depth_to_color_extrinsics_topic": _env(
                        "CV_CAM4_DEPTH_TO_COLOR_EXTRINSICS_TOPIC",
                        "/synced/cam_4/extrinsics/depth_to_color",
                    ),
                    "cv_cam4_aligned_depth_compressed_topic": _env(
                        "CV_CAM4_ALIGNED_DEPTH_COMPRESSED_TOPIC",
                        "/synced/cam_4/aligned_depth_to_color/"
                        "image_raw/compressedDepth",
                    ),
                    "cv_cam4_aligned_depth_camera_info_topic": _env(
                        "CV_CAM4_ALIGNED_DEPTH_CAMERA_INFO_TOPIC",
                        "/synced/cam_4/aligned_depth_to_color/camera_info",
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
                condition=IfCondition(camera_aliases_enabled),
                parameters=[
                    {
                        "flir_source_topic": flir_input_topic,
                        "flir_public_topic": "/surgery/images/flir/compressed",
                        "cam4_source_topic": cam4_input_topic,
                        "cam4_public_topic": "/surgery/images/cam4/compressed",
                        "cam3_overlay_source_topic": (
                            "/perception/cam_3/overlay/compressed"
                        ),
                        "cam3_overlay_public_topic": (
                            "/surgery/images/cam3/overlay/compressed"
                        ),
                        "cam4_overlay_source_topic": (
                            "/perception/cam_4/overlay/compressed"
                        ),
                        "cam4_overlay_public_topic": (
                            "/surgery/images/cam4/overlay/compressed"
                        ),
                        "suction_overlay_source_topic": (
                            "/perception/suction/overlay/compressed"
                        ),
                        "suction_overlay_public_topic": (
                            "/surgery/images/suction/overlay/compressed"
                        ),
                        "right_ee_overlay_source_topic": (
                            "/perception/right_ee/overlay/compressed"
                        ),
                        "right_ee_overlay_public_topic": (
                            "/surgery/images/right_ee/overlay/compressed"
                        ),
                        "default_bundle": default_bundle,
                        "publish_flir_while_idle": publish_flir_while_idle,
                    }
                ],
                output="screen",
            ),
        ]
    )
