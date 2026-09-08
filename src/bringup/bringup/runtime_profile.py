"""Pure runtime-profile resolution for the split Taskplanner launch owners.

The Compose/runtime controller identifies Live with ``EXECUTION_BACKEND=action``
because that is the operational controller contract.  ROS launch uses the
semantic value ``external`` instead.  Keeping that translation here prevents
each owner launch from growing its own version of the mode mapping.

This module intentionally has no ROS or launch imports.  It is safe to test
with ordinary Python and accepts its environment as an explicit input.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Mapping


RUNTIME_OWNER_NAMES: Final[tuple[str, ...]] = (
    "operator-bridge",
    "scenario",
    "state-core",
    "command",
    "tool-state",
    "perception",
    "cam4-mayo",
    "projection",
    "execution",
    "simulation-input",
)

RUNTIME_PROFILE_NAMES: Final[tuple[str, ...]] = (
    "mock",
    "live",
    "llm-surgeon",
)


class RuntimeProfileError(ValueError):
    """Raised when a caller asks for an unknown runtime profile or owner."""


@dataclass(frozen=True)
class RuntimeProfile:
    """Resolved, launch-ready mode values without any ROS substitutions."""

    name: str
    launch_arguments: Mapping[str, str]
    enabled_owners: tuple[str, ...]

    def arguments_for(self, owner: str) -> dict[str, str]:
        """Return this profile's values with the owner's local defaults.

        Owner launches are independently runnable.  In particular, the
        operator bridge alone turns on the legacy graph's rosbridge actions;
        every other owner keeps them off so no second websocket server can be
        started accidentally.
        """

        _validate_owner(owner)
        values = dict(self.launch_arguments)
        values["enable_rosbridge"] = (
            "true" if owner == "operator-bridge" else "false"
        )
        return values


def _validate_owner(owner: str) -> None:
    if owner not in RUNTIME_OWNER_NAMES:
        known = ", ".join(RUNTIME_OWNER_NAMES)
        raise RuntimeProfileError(
            f"unknown runtime owner {owner!r}; expected one of {known}"
        )


def _env(environment: Mapping[str, object], name: str, default: str) -> str:
    value = environment.get(name, default)
    return str(value).strip() if value is not None else default


def _common_arguments(environment: Mapping[str, object]) -> dict[str, str]:
    """Values shared by all profile projections.

    Most legacy launch arguments deliberately retain their declaration
    defaults.  The values here are the externally configurable runtime
    identity and source/topic settings that must agree across independent
    owners.
    """

    return {
        "publish_shared_state": _env(environment, "PUBLISH_SHARED_STATE", "true"),
        "publish_shared_free_text": _env(
            environment, "PUBLISH_SHARED_FREE_TEXT", "false"
        ),
        "rosbridge_port": _env(environment, "ROSBRIDGE_PORT", "9090"),
        "rosbridge_address": _env(environment, "ROSBRIDGE_ADDRESS", "127.0.0.1"),
        "rosbridge_service_timeout": _env(
            environment, "ROSBRIDGE_SERVICE_TIMEOUT", "30.0"
        ),
        "speech_input_mode": _env(environment, "SPEECH_INPUT_MODE", "utterance"),
        "sentence_input_topic": _env(
            environment, "SENTENCE_INPUT_TOPIC", "/sensors/surgeon/sentence"
        ),
        "speech_min_confidence": _env(environment, "SPEECH_MIN_CONFIDENCE", "0.55"),
        "speech_max_age_sec": _env(environment, "SPEECH_MAX_AGE_SEC", "3.0"),
        "speech_source_timeout_sec": _env(
            environment, "SPEECH_SOURCE_TIMEOUT_SEC", "5.0"
        ),
        # State-core must never consume the resolver's private proposal
        # output.  Retain an explicitly inert historical lane only for old
        # replay fixtures that set it deliberately.
        "legacy_voice_intent_topic": _env(
            environment,
            "TASKPLANNER_LEGACY_VOICE_INTENT_TOPIC",
            "/surgery/voice/legacy_disabled",
        ),
        "vlm_mode": _env(environment, "VLM_MODE", "real"),
        "vlm_base_url": _env(environment, "VLM_BASE_URL", "http://127.0.0.1:8080"),
        "vlm_provider_id": _env(environment, "VLM_PROVIDER_ID", "ninfer"),
        "vlm_model_id": _env(environment, "VLM_MODEL_ID", "qwen3.6-35b-a3b"),
        "vlm_api_mode": _env(environment, "VLM_API_MODE", "openai_compat"),
        "vlm_publish_period_sec": _env(
            environment, "VLM_PUBLISH_PERIOD_SEC", "1.0"
        ),
        "vlm_image_stale_sec": _env(environment, "VLM_IMAGE_STALE_SEC", "3.0"),
        "vlm_max_output_tokens": _env(environment, "VLM_MAX_OUTPUT_TOKENS", "384"),
        "vlm_generation_seed": _env(environment, "VLM_GENERATION_SEED", "0"),
        "vlm_response_format": _env(environment, "VLM_RESPONSE_FORMAT", "json_schema"),
        "vlm_reasoning_effort": _env(environment, "VLM_REASONING_EFFORT", "none"),
        # This is consumed only by the real-VLM owner.  Keeping its ordinary
        # launch default here lets the independently restartable perception
        # owner preserve the legacy response lane without importing a wrapper.
        "vlm_response_mode": _env(environment, "VLM_RESPONSE_MODE", "live"),
        "vlm_context_mode": _env(environment, "VLM_CONTEXT_MODE", "actor_log"),
        "field_snapshot_url": _env(environment, "FIELD_SNAPSHOT_URL", ""),
        "enable_rfdetr_perception": _env(
            environment, "ENABLE_RFDETR_PERCEPTION", "false"
        ),
        "perception_backend": _env(environment, "PERCEPTION_BACKEND", "local"),
        "perception_provider": _env(environment, "PERCEPTION_PROVIDER", ""),
        "perception_location": _env(environment, "PERCEPTION_LOCATION", ""),
        "perception_endpoint": _env(environment, "PERCEPTION_ENDPOINT", ""),
        "rfdetr_service_url": _env(
            environment, "RFDETR_SERVICE_URL", "http://127.0.0.1:8010"
        ),
        "pnu_service_url": _env(environment, "PNU_SERVICE_URL", ""),
        "pnu_api_token_file": _env(environment, "PNU_CLIENT_API_TOKEN_FILE", ""),
        "pnu_allow_insecure_remote_http": _env(
            environment, "PNU_ALLOW_INSECURE_REMOTE_HTTP", "false"
        ),
        "pnu_allow_unauthenticated_remote": _env(
            environment, "PNU_ALLOW_UNAUTHENTICATED_REMOTE", "false"
        ),
        "pnu_expected_model_digests_json": _env(
            environment, "PNU_EXPECTED_MODEL_DIGESTS_JSON", "{}"
        ),
        "pnu_expected_tool_support_plane_config_version": _env(
            environment, "PNU_EXPECTED_TOOL_SUPPORT_PLANE_CONFIG_VERSION", ""
        ),
        "pnu_depth_scale_m_per_unit": _env(
            environment, "PNU_DEPTH_SCALE_M_PER_UNIT", "0.0"
        ),
        "pnu_depth_scale_validated": _env(
            environment, "PNU_DEPTH_SCALE_VALIDATED", "false"
        ),
        "pnu_depth_alignment_validated": _env(
            environment, "PNU_DEPTH_ALIGNMENT_VALIDATED", "false"
        ),
        "pnu_depth_alignment_id": _env(environment, "PNU_DEPTH_ALIGNMENT_ID", ""),
        "cam4_semantics_topic": _env(environment, "CAM4_SEMANTICS_TOPIC", ""),
        "cv_contract_status_topic": _env(
            environment, "CV_CONTRACT_STATUS_TOPIC", "/integration/cv_contract/status"
        ),
        "cv_cam4_rgb_topic": _env(
            environment, "CV_CAM4_RGB_TOPIC", "/synced/cam_4/color/image_raw/compressed"
        ),
        "cv_cam4_rgb_alias_topic": _env(
            environment, "CV_CAM4_RGB_ALIAS_TOPIC", "/surgery/images/cam4/compressed"
        ),
        "cv_cam4_camera_info_topic": _env(
            environment, "CV_CAM4_CAMERA_INFO_TOPIC", "/synced/cam_4/color/camera_info"
        ),
        "cv_cam4_native_depth_compressed_topic": _env(
            environment,
            "CV_CAM4_NATIVE_DEPTH_COMPRESSED_TOPIC",
            "/synced/cam_4/depth/image_rect_raw/compressedDepth",
        ),
        "cv_cam4_depth_camera_info_topic": _env(
            environment, "CV_CAM4_DEPTH_CAMERA_INFO_TOPIC", "/synced/cam_4/depth/camera_info"
        ),
        "cv_cam4_depth_to_color_extrinsics_topic": _env(
            environment,
            "CV_CAM4_DEPTH_TO_COLOR_EXTRINSICS_TOPIC",
            "/synced/cam_4/extrinsics/depth_to_color",
        ),
        "cv_cam4_aligned_depth_compressed_topic": _env(
            environment,
            "CV_CAM4_ALIGNED_DEPTH_COMPRESSED_TOPIC",
            "/synced/cam_4/aligned_depth_to_color/image_raw/compressedDepth",
        ),
        "cv_cam4_aligned_depth_camera_info_topic": _env(
            environment,
            "CV_CAM4_ALIGNED_DEPTH_CAMERA_INFO_TOPIC",
            "/synced/cam_4/aligned_depth_to_color/camera_info",
        ),
        "cv_handover_tray_rgb_topic": _env(
            environment, "CV_HANDOVER_TRAY_RGB_TOPIC", "/surgery/images/tray/compressed"
        ),
        "cv_handover_tray_camera_info_topic": _env(
            environment,
            "CV_HANDOVER_TRAY_CAMERA_INFO_TOPIC",
            "/surgery/cameras/tray/color/camera_info",
        ),
        "cv_handover_tray_aligned_depth_topic": _env(
            environment,
            "CV_HANDOVER_TRAY_ALIGNED_DEPTH_TOPIC",
            "/surgery/cameras/tray/aligned_depth",
        ),
        "actor_base_url": _env(environment, "ACTOR_BASE_URL", "http://127.0.0.1:1234"),
        "actor_provider_id": _env(environment, "ACTOR_PROVIDER_ID", "auto"),
        "actor_model_id": _env(
            environment, "ACTOR_MODEL_ID", "google/gemma-4-12b-qat"
        ),
        "actor_response_format": _env(
            environment, "ACTOR_RESPONSE_FORMAT", "json_schema"
        ),
        "actor_reasoning_effort": _env(
            environment, "ACTOR_REASONING_EFFORT", "none"
        ),
        "flir_input_topic": _env(
            environment, "FLIR_INPUT_TOPIC", "/synced/flir/color/image_raw/compressed"
        ),
        "cam3_input_topic": _env(
            environment, "CAM3_INPUT_TOPIC", "/synced/cam_3/color/image_raw/compressed"
        ),
        "cam4_input_topic": _env(
            environment, "CAM4_INPUT_TOPIC", "/synced/cam_4/color/image_raw/compressed"
        ),
        "publish_camera_aliases": _env(
            environment, "PUBLISH_CAMERA_ALIASES", "true"
        ),
        "publish_flir_while_idle": _env(
            environment, "PUBLISH_FLIR_WHILE_IDLE", "false"
        ),
        # ScenarioStore is the only selected-bundle writer.  Its tiny
        # last-good snapshot is intentionally outside the image so restarting
        # just this owner neither resets an operator selection nor requires a
        # whole Taskplanner restart.
        "scenario_selection_state_path": _env(
            environment, "TASKPLANNER_SCENARIO_SELECTION_STATE_PATH", ""
        ),
    }


def _live_arguments(environment: Mapping[str, object]) -> dict[str, str]:
    """Return the invariant portion of the reviewed external Live profile."""

    values = _common_arguments(environment)
    # ``action`` is a mode-controller value, while launch owners need
    # ``external`` to select the direct Action/Service adapter.  Do not pass
    # the controller spelling through to each independent owner.
    values.update(
        {
            "default_bundle": _env(
                environment, "TASKPLANNER_LIVE_DEFAULT_BUNDLE", "thyroidectomy_demo"
            ),
            "input_profile": "external",
            "execution_backend": "external",
            "robot_endpoint_source": _env(
                environment,
                "TASKPLANNER_ROBOT_ENDPOINT_SOURCE",
                _env(
                    environment, "TASKPLANNER_LIVE_ROBOT_ENDPOINT_SOURCE", "external"
                ),
            ),
            "retraction_endpoint_source": _env(
                environment,
                "TASKPLANNER_RETRACTION_ENDPOINT_SOURCE",
                _env(
                    environment, "TASKPLANNER_LIVE_ROBOT_ENDPOINT_SOURCE", "external"
                ),
            ),
            "external_controller_contract_id": _env(
                environment,
                "TASKPLANNER_EXTERNAL_CONTROLLER_CONTRACT_ID",
                _env(
                    environment,
                    "TASKPLANNER_LIVE_EXTERNAL_CONTROLLER_CONTRACT_ID",
                    "eir-nuc-tool-handover.real.v1",
                ),
            ),
            "external_capability_policy_id": _env(
                environment,
                "TASKPLANNER_EXTERNAL_CAPABILITY_POLICY_ID",
                _env(
                    environment,
                    "TASKPLANNER_LIVE_EXTERNAL_CAPABILITY_POLICY_ID",
                    "eir-nuc-tool-handover.v1",
                ),
            ),
            "controller_contract_max_age_sec": _env(
                environment, "CONTROLLER_CONTRACT_MAX_AGE_SEC", "3.0"
            ),
            "enable_runtime_route_control": "true",
            "enable_tool_belief_tracker": "true",
            # External ASR publishes tagged String transcripts by default. The
            # operator can select the original typed microphone ingress for a
            # session with SPEECH_INPUT_MODE=utterance; the adapter keeps both
            # subscriptions alive and admits only the selected mode.
            "speech_input_mode": _env(
                environment, "SPEECH_INPUT_MODE", "tagged_sentence"
            ).lower(),
            "speech_input_topic": _env(
                environment, "ASR_UTTERANCE_TOPIC", "/sensors/surgeon/utterance"
            ),
            "speech_output_mode": "typed_utterance",
            "speech_typed_output_topic": "/surgery/audio/admitted_utterance",
            "enable_tts_echo_guard": "true",
            "tts_playback_status_topic": "/tts/playback_status",
            "tts_echo_tail_sec": _env(environment, "TTS_ECHO_TAIL_SEC", "0.8"),
            "tts_echo_similarity_threshold": _env(
                environment, "TTS_ECHO_SIMILARITY_THRESHOLD", "0.88"
            ),
            "voice_command_input_mode": "utterance",
            # CommandRouter is the single admitted-ASR ingress. It forwards
            # only catalog misses to the resolver and consumes its correlated
            # proposal itself; no BT/Digital-Twin command bus sits in front of
            # a typed endpoint adapter.
            "voice_command_input_topic": "/surgery/voice/resolver_utterance",
            "voice_command_output_topic": "/surgery/voice/proposal",
            "resolver_input_topic": "/surgery/voice/resolver_utterance",
            "resolver_output_topic": "/surgery/voice/proposal",
            "command_router_enabled": "true",
            "command_router_catalog_path": _env(
                environment, "COMMAND_ROUTER_CATALOG_PATH", ""
            ),
            "command_router_catalog_reload_sec": _env(
                environment, "COMMAND_ROUTER_CATALOG_RELOAD_SEC", "0.5"
            ),
            "enable_voice_procedure_control": "true",
            "voice_procedure_accept_missing_confidence": "true",
            "voice_intent_require_source_metadata": "true",
            "require_asr_runtime_status": "true",
            "asr_runtime_status_topic": "/input/asr/runtime_status",
            "asr_runtime_status_max_age_sec": _env(
                environment, "ASR_RUNTIME_STATUS_MAX_AGE_SEC", "3.0"
            ),
            "asr_runtime_status_source_future_tolerance_sec": _env(
                environment, "ASR_RUNTIME_STATUS_SOURCE_FUTURE_TOLERANCE_SEC", "0.5"
            ),
            # Live command resolution is deterministic.  Model-assisted
            # candidate selection remains a Debug/replay experiment and can
            # never become a required hop for a local typed command.
            "voice_command_selector_mode": "deterministic",
            "vlm_api_mode": "openai_compat",
            "vlm_require_source_frame_timestamp": "true",
            "vlm_model_input_max_source_lag_sec": "1.0",
            "vlm_model_input_max_source_future_skew_sec": "0.25",
            "surgeon_actor_mode": "none",
            "enable_no_image_camera": "false",
            "enable_synthetic_scene_camera": "false",
            "enable_rfdetr_perception": "false",
            "perception_backend": "external",
            "perception_provider": "external_rfdetr_topics",
            "perception_location": "remote",
            "perception_endpoint": "",
            "rfdetr_service_url": "",
            "field_image_topic": values["flir_input_topic"],
            "rfdetr_flir_output_topic": (
                "/taskplanner/internal/rfdetr/flir/segmented/compressed"
            ),
            "flir_overlay_image_topic": (
                "/taskplanner/internal/rfdetr/flir/segmentation_overlay/compressed"
            ),
            "cam4_overlay_image_topic": (
                "/taskplanner/internal/rfdetr/cam4/detection_overlay/compressed"
            ),
            "cam3_overlay_image_topic": (
                "/taskplanner/internal/rfdetr/cam3/detection_overlay/compressed"
            ),
            "composite_image_topic": "/taskplanner/internal/vlm/model_visual/compressed",
            "cam4_semantics_topic": _env(
                environment,
                "CAM4_SEMANTICS_TOPIC",
                "/surgery/perception/cam4/semantics/json",
            ),
            "cam3_tool_observations_topic": _env(
                environment,
                "CAM3_TOOL_OBSERVATIONS_TOPIC",
                "/perception/cam_3/tool/observations",
            ),
            "cam4_tool_observations_topic": _env(
                environment,
                "CAM4_TOOL_OBSERVATIONS_TOPIC",
                "/perception/cam_4/tool/observations",
            ),
            "rfdetr_bridge_cam3_tool_observations_topic": (
                "/taskplanner/internal/rfdetr/cam_3/tool/observations"
            ),
            "rfdetr_bridge_cam4_tool_observations_topic": (
                "/taskplanner/internal/rfdetr/cam_4/tool/observations"
            ),
            # The state-core diagnostic observer consumes these Live-only
            # perception labels directly.  Keep them in the pure profile
            # rather than importing the legacy Live wrapper during a scoped
            # core restart.
            "cam3_tool_observations_expected_model_version": _env(
                environment,
                "CAM3_TOOL_OBSERVATIONS_EXPECTED_MODEL_VERSION",
                "",
            ),
            "cam4_tool_observations_expected_model_version": _env(
                environment,
                "CAM4_TOOL_OBSERVATIONS_EXPECTED_MODEL_VERSION",
                "",
            ),
            "allow_legacy_cam4_semantics_fallback": "false",
            "require_field_image": "false",
            "require_rfdetr_applied_field_image": "false",
            "require_rfdetr_cam4_overlay": "false",
            "enable_integration_preflight_diagnostics": _env(
                environment,
                "ENABLE_INTEGRATION_PREFLIGHT_DIAGNOSTICS",
                "true",
            ),
            "preflight_require_perception": _env(
                environment, "REQUIRE_PERCEPTION_ON_START", "false"
            ),
            "preflight_require_rfdetr_tool_observations": "true",
            "rfdetr_vlm_request_context_topic": _env(
                environment,
                "RFDETR_VLM_REQUEST_CONTEXT_TOPIC",
                "/context/vlm_request_context",
            ),
            "preflight_require_metric_3d": "false",
            "cv_contract_status_topic": _env(
                environment,
                "CV_CONTRACT_STATUS_TOPIC",
                "/integration/cv_contract/status",
            ),
            "hand_mapping_operator_approved": "true",
        }
    )
    return values


def _llm_surgeon_arguments(environment: Mapping[str, object]) -> dict[str, str]:
    """Return the simulation/LLM profile without inheriting Live semantics."""

    values = _common_arguments(environment)
    values.update(
        {
            "default_bundle": _env(
                environment, "TASKPLANNER_DEFAULT_BUNDLE", "thyroidectomy"
            ),
            "input_profile": "simulation",
            "execution_backend": "mock",
            # The command owner is resident in this profile too.  Keeping
            # the router enabled preserves the single typed ingress rather
            # than leaving resolver proposals with no dispatch owner.
            "command_router_enabled": "true",
            "surgeon_actor_mode": _env(environment, "SURGEON_ACTOR_MODE", "llm"),
            "enable_no_image_camera": _env(
                environment, "ENABLE_NO_IMAGE_CAMERA", "true"
            ),
            "enable_synthetic_scene_camera": _env(
                environment, "ENABLE_SYNTHETIC_SCENE_CAMERA", "false"
            ),
            "enable_rfdetr_perception": _env(
                environment, "ENABLE_RFDETR_PERCEPTION", "false"
            ),
            "perception_backend": _env(environment, "PERCEPTION_BACKEND", "disabled"),
            "perception_provider": _env(environment, "PERCEPTION_PROVIDER", "disabled"),
            "perception_location": _env(environment, "PERCEPTION_LOCATION", "local"),
            "perception_endpoint": _env(environment, "PERCEPTION_ENDPOINT", ""),
            "enable_integration_preflight_diagnostics": _env(
                environment,
                "ENABLE_INTEGRATION_PREFLIGHT_DIAGNOSTICS",
                "false",
            ),
            "enable_tool_belief_tracker": _env(
                environment, "ENABLE_TOOL_BELIEF_TRACKER", "false"
            ),
        }
    )
    return values


def _mock_arguments(environment: Mapping[str, object]) -> dict[str, str]:
    values = _common_arguments(environment)
    values.update(
        {
            "default_bundle": _env(
                environment, "TASKPLANNER_DEFAULT_BUNDLE", "thyroidectomy"
            ),
            "input_profile": _env(environment, "INPUT_PROFILE", "simulation"),
            "execution_backend": "mock",
            "enable_integration_preflight_diagnostics": _env(
                environment,
                "ENABLE_INTEGRATION_PREFLIGHT_DIAGNOSTICS",
                "false",
            ),
            "enable_tool_belief_tracker": _env(
                environment, "ENABLE_TOOL_BELIEF_TRACKER", "false"
            ),
        }
    )
    return values


def resolve_runtime_profile(
    profile_name: str,
    *,
    environment: Mapping[str, object] | None = None,
) -> RuntimeProfile:
    """Resolve a runtime mode into semantic launch arguments.

    ``environment`` is optional but explicit, so callers can use a frozen
    mapping in tests or pass a compose environment without reading process
    globals.  ``live`` always maps the controller spelling ``action`` to the
    launch spelling ``external``; this is intentionally not delegated to each
    owner or Docker command.
    """

    normalized = str(profile_name).strip().casefold().replace("_", "-")
    if normalized not in RUNTIME_PROFILE_NAMES:
        known = ", ".join(RUNTIME_PROFILE_NAMES)
        raise RuntimeProfileError(
            f"unknown runtime profile {profile_name!r}; expected one of {known}"
        )
    env: Mapping[str, object] = environment or {}
    if normalized == "live":
        values = _live_arguments(env)
        owners = tuple(
            owner for owner in RUNTIME_OWNER_NAMES if owner != "simulation-input"
        )
    elif normalized == "llm-surgeon":
        values = _llm_surgeon_arguments(env)
        owners = RUNTIME_OWNER_NAMES
    else:
        values = _mock_arguments(env)
        owners = RUNTIME_OWNER_NAMES
    return RuntimeProfile(
        name=normalized,
        launch_arguments=values,
        enabled_owners=owners,
    )
