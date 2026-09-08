"""Direct launch wiring for Taskplanner's typed speech and command owner."""

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

from bringup.runtime_profile import RUNTIME_PROFILE_NAMES, RuntimeProfileError, resolve_runtime_profile

_COMMAND_PROFILE_ARGUMENT_NAMES: Final[tuple[str, ...]] = (
    "default_bundle",
    "speech_input_mode",
    "speech_input_topic",
    "sentence_input_topic",
    "speech_output_mode",
    "speech_typed_output_topic",
    "enable_tts_echo_guard",
    "tts_playback_status_topic",
    "tts_echo_tail_sec",
    "tts_echo_similarity_threshold",
    "speech_min_confidence",
    "speech_max_age_sec",
    "speech_source_timeout_sec",
    "voice_command_input_mode",
    "voice_command_input_topic",
    "voice_command_output_topic",
    "resolver_input_topic",
    "resolver_output_topic",
    "command_router_enabled",
    "command_router_catalog_path",
    "command_router_catalog_reload_sec",
    "voice_intent_max_age_sec",
    "voice_intent_future_tolerance_sec",
    "voice_intent_dedupe_retention_sec",
    "voice_command_selector_mode",
    "voice_command_selector_endpoint",
    "voice_command_selector_model",
    "voice_command_selector_timeout_sec",
)


def _command_profile_actions(context) -> list[SetLaunchConfiguration]:
    """Apply only the command owner's profile values.

    The command plane consumes typed speech, resolves language proposals, and
    invokes the deterministic command router. It has no reason to inherit
    camera, execution, or browser-transport settings from the retained graph.
    """

    profile_name = LaunchConfiguration("runtime_profile").perform(context)
    try:
        profile = resolve_runtime_profile(profile_name, environment=os.environ)
    except RuntimeProfileError as exc:
        raise RuntimeError(str(exc)) from exc
    values = profile.arguments_for("command")
    return [
        SetLaunchConfiguration(name, values[name])
        for name in _COMMAND_PROFILE_ARGUMENT_NAMES
        if name in values
    ]


def _command_actions() -> list[object]:
    """Build the three direct command-plane nodes without the legacy graph."""

    spec_dir = LaunchConfiguration("spec_dir")
    speech_input_mode = LaunchConfiguration("speech_input_mode")
    speech_input_topic = LaunchConfiguration("speech_input_topic")
    sentence_input_topic = LaunchConfiguration("sentence_input_topic")
    speech_output_mode = LaunchConfiguration("speech_output_mode")
    speech_typed_output_topic = LaunchConfiguration("speech_typed_output_topic")
    enable_tts_echo_guard = LaunchConfiguration("enable_tts_echo_guard")
    tts_playback_status_topic = LaunchConfiguration("tts_playback_status_topic")
    tts_echo_tail_sec = LaunchConfiguration("tts_echo_tail_sec")
    tts_echo_similarity_threshold = LaunchConfiguration(
        "tts_echo_similarity_threshold"
    )
    speech_min_confidence = LaunchConfiguration("speech_min_confidence")
    speech_max_age_sec = LaunchConfiguration("speech_max_age_sec")
    speech_source_timeout_sec = LaunchConfiguration("speech_source_timeout_sec")
    voice_intent_resolver_enabled = LaunchConfiguration(
        "voice_intent_resolver_enabled"
    )
    voice_command_input_mode = LaunchConfiguration("voice_command_input_mode")
    voice_command_input_topic = LaunchConfiguration("voice_command_input_topic")
    voice_command_output_topic = LaunchConfiguration("voice_command_output_topic")
    resolver_input_topic = LaunchConfiguration("resolver_input_topic")
    resolver_output_topic = LaunchConfiguration("resolver_output_topic")
    command_router_enabled = LaunchConfiguration("command_router_enabled")
    command_router_catalog_path = LaunchConfiguration("command_router_catalog_path")
    command_router_catalog_reload_sec = LaunchConfiguration(
        "command_router_catalog_reload_sec"
    )
    voice_intent_max_age_sec = LaunchConfiguration("voice_intent_max_age_sec")
    voice_intent_future_tolerance_sec = LaunchConfiguration(
        "voice_intent_future_tolerance_sec"
    )
    voice_intent_dedupe_retention_sec = LaunchConfiguration(
        "voice_intent_dedupe_retention_sec"
    )
    voice_command_selector_mode = LaunchConfiguration("voice_command_selector_mode")
    voice_command_selector_endpoint = LaunchConfiguration(
        "voice_command_selector_endpoint"
    )
    voice_command_selector_model = LaunchConfiguration("voice_command_selector_model")
    voice_command_selector_timeout_sec = LaunchConfiguration(
        "voice_command_selector_timeout_sec"
    )

    return [
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
                    "tts_echo_similarity_threshold": tts_echo_similarity_threshold,
                    "min_confidence": speech_min_confidence,
                    "max_age_sec": speech_max_age_sec,
                    "source_timeout_sec": speech_source_timeout_sec,
                }
            ],
            output="screen",
        ),
        Node(
            package="voice_command",
            executable="voice_intent_resolver",
            name="voice_command_resolver",
            condition=IfCondition(voice_intent_resolver_enabled),
            parameters=[
                {
                    "input_mode": voice_command_input_mode,
                    "input_topic": resolver_input_topic,
                    "output_topic": resolver_output_topic,
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
    ]


def _generate_command_launch_description() -> LaunchDescription:
    """Build the small operator-command owner directly.

    New typed commands remain catalog edits inside command_router; this launch
    is intentionally just the owner process wiring and has no duplicate
    VLM, Digital Twin, or BT admission path.
    """

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
                description="Mode-level operator-command defaults.",
            ),
            OpaqueFunction(function=lambda context: _command_profile_actions(context)),
            DeclareLaunchArgument(
                "default_bundle",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_DEFAULT_BUNDLE", default_value="thyroidectomy"
                ),
            ),
            DeclareLaunchArgument("spec_dir", default_value=spec_default),
            DeclareLaunchArgument(
                "voice_intent_resolver_enabled",
                default_value=EnvironmentVariable(
                    "TASKPLANNER_CAPABILITY_VOICE_RESOLVER", default_value="true"
                ),
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
                "speech_output_mode",
                default_value="sentence_text",
                choices=("sentence_text", "typed_utterance"),
            ),
            DeclareLaunchArgument(
                "speech_typed_output_topic",
                default_value="/surgery/audio/admitted_utterance",
            ),
            DeclareLaunchArgument("enable_tts_echo_guard", default_value="false"),
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
            ),
            DeclareLaunchArgument(
                "voice_command_output_topic",
                default_value="/surgery/voice/proposal",
            ),
            DeclareLaunchArgument(
                "resolver_input_topic",
                default_value="/surgery/voice/resolver_utterance",
            ),
            DeclareLaunchArgument(
                "resolver_output_topic",
                default_value="/surgery/voice/proposal",
            ),
            DeclareLaunchArgument("command_router_enabled", default_value="false"),
            DeclareLaunchArgument("command_router_catalog_path", default_value=""),
            DeclareLaunchArgument(
                "command_router_catalog_reload_sec",
                default_value="0.5",
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
                "voice_command_selector_mode",
                default_value="deterministic",
                choices=("deterministic", "openai_compatible", "openai"),
            ),
            DeclareLaunchArgument(
                "voice_command_selector_endpoint",
                default_value="",
            ),
            DeclareLaunchArgument("voice_command_selector_model", default_value=""),
            DeclareLaunchArgument(
                "voice_command_selector_timeout_sec",
                default_value="0.35",
            ),
            *_command_actions(),
        ]
    )


