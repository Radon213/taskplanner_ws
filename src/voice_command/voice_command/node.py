"""ROS adapter for proposal-only spoken-command interpretation."""

from __future__ import annotations

from dataclasses import dataclass
import itertools
import time

import rclpy
from rcl_interfaces.msg import SetParametersResult
from rclpy.node import Node
from std_msgs.msg import String

from surgical_msgs.msg import SpeechUtterance, VoiceCommandIntent
from procedure_spec import load_voice_command_catalog

from .resolver import VoiceIntentResolver
from .selector import DeterministicCandidateSelector, OpenAICompatibleCandidateSelector


def _stamp_sec(stamp) -> float:
    return float(stamp.sec) + float(stamp.nanosec) / 1_000_000_000.0


def source_observation_stamp(msg: SpeechUtterance):
    """Return the source-time envelope stamp used for live command admission."""

    for stamp in (msg.stamp, msg.end_stamp, msg.start_stamp):
        if _stamp_sec(stamp) > 0.0:
            return stamp
    return None


@dataclass(frozen=True, slots=True)
class TypedSpeechAdmission:
    accepted: bool
    reason: str
    stamp: object | None = None


class RecentUtteranceIds:
    """Suppress replays by immutable ASR utterance identifier."""

    def __init__(self, retention_sec: float) -> None:
        self._retention_sec = max(1.0, float(retention_sec))
        self._seen: dict[str, float] = {}

    def accept(self, utterance_id: str, now_monotonic: float) -> bool:
        cutoff = float(now_monotonic) - self._retention_sec
        self._seen = {
            key: seen_at
            for key, seen_at in self._seen.items()
            if seen_at >= cutoff
        }
        key = str(utterance_id or "").strip()
        if not key or key in self._seen:
            return False
        self._seen[key] = float(now_monotonic)
        return True


def evaluate_typed_speech_utterance(
    msg: SpeechUtterance,
    *,
    now_sec: float,
    max_age_sec: float,
    max_future_skew_sec: float,
) -> TypedSpeechAdmission:
    """Fail closed before ASR metadata can be converted to an intent.

    This is deliberately independent of the adapter.  A ROS topic is not an
    authentication boundary, so the execution-path resolver revalidates the
    final marker, immutable identity, source name, and source timestamp.
    """

    if not str(msg.text or "").strip():
        return TypedSpeechAdmission(False, "empty_text")
    if not bool(msg.is_final):
        return TypedSpeechAdmission(False, "interim_transcript")
    if not str(msg.utterance_id or "").strip():
        return TypedSpeechAdmission(False, "missing_utterance_id")
    if not str(msg.source or "").strip():
        return TypedSpeechAdmission(False, "missing_source")
    stamp = source_observation_stamp(msg)
    if stamp is None:
        return TypedSpeechAdmission(False, "missing_timestamp")
    age_sec = float(now_sec) - _stamp_sec(stamp)
    if age_sec > max(0.0, float(max_age_sec)):
        return TypedSpeechAdmission(False, f"stale:{age_sec:.3f}s")
    if age_sec < -max(0.0, float(max_future_skew_sec)):
        return TypedSpeechAdmission(False, f"future_timestamp:{-age_sec:.3f}s")
    return TypedSpeechAdmission(True, "accepted", stamp)


class VoiceIntentResolverNode(Node):
    """Convert final STT text to typed proposals without calling ROS actions."""

    def __init__(self) -> None:
        super().__init__("voice_intent_resolver")
        self.declare_parameter("input_topic", "/surgery/audio/request_text")
        # ``sentence_text`` remains an explicitly non-live compatibility
        # route for Debug/replay.  ``utterance`` is the only mode that may
        # preserve ASR authority metadata into an executable proposal.
        self.declare_parameter("input_mode", "sentence_text")
        self.declare_parameter("output_topic", "/surgery/voice/intent")
        self.declare_parameter("typed_input_max_age_sec", 3.0)
        self.declare_parameter("typed_input_max_future_skew_sec", 1.0)
        self.declare_parameter("typed_input_dedupe_retention_sec", 120.0)
        # Empty by default is fail-closed: tool aliases are derived only from
        # the active ProcedureSpec bundle, never a global T04-style mapping.
        self.declare_parameter("procedure_bundle", "")
        self.declare_parameter("selector_mode", "deterministic")
        self.declare_parameter("selector_endpoint", "")
        self.declare_parameter("selector_model", "")
        self.declare_parameter("selector_timeout_sec", 0.35)
        # Relaxed natural variants are VLM-selector-only and confirmation
        # required.  Keep this off until a confirmation/ack consumer exists.
        self.declare_parameter("enable_selector_natural_variants", False)
        self.declare_parameter("publish_no_command", True)

        selector, selector_is_model = self._build_selector()
        self._selector = selector
        self._selector_is_model = selector_is_model
        (
            procedure_id,
            catalog_id,
            tool_aliases,
            retractor_commands,
            retractor_max_distance_m,
            retractor_require_explicit_unit,
        ) = self._load_active_catalog()
        requested_natural_variants = bool(
            self.get_parameter("enable_selector_natural_variants").value
        )
        enable_selector_natural_variants = (
            requested_natural_variants and selector_is_model
        )
        if requested_natural_variants and not selector_is_model:
            self.get_logger().warning(
                "enable_selector_natural_variants ignored without openai_compatible selector"
            )
        self._resolver = VoiceIntentResolver(
            tool_aliases=tool_aliases,
            procedure_id=procedure_id,
            catalog_id=catalog_id,
            retractor_commands=retractor_commands,
            retractor_max_distance_m=retractor_max_distance_m,
            retractor_require_explicit_unit=retractor_require_explicit_unit,
            selector=selector,
            allow_selector_natural_variants=enable_selector_natural_variants,
        )
        self._publish_no_command = bool(
            self.get_parameter("publish_no_command").value
        )
        self._utterance_counter = itertools.count(1)
        self._input_mode = str(self.get_parameter("input_mode").value).strip().lower()
        if self._input_mode not in {"sentence_text", "utterance"}:
            raise ValueError("input_mode must be 'sentence_text' or 'utterance'")
        self._typed_input_max_age_sec = max(
            0.0,
            float(self.get_parameter("typed_input_max_age_sec").value),
        )
        self._typed_input_max_future_skew_sec = max(
            0.0,
            float(
                self.get_parameter("typed_input_max_future_skew_sec").value
            ),
        )
        self._typed_input_dedupe_retention_sec = max(
            0.0,
            float(
                self.get_parameter("typed_input_dedupe_retention_sec").value
            ),
        )
        self._recent_utterance_ids = RecentUtteranceIds(
            self._typed_input_dedupe_retention_sec
        )
        self.add_on_set_parameters_callback(self._on_parameters_changed)
        input_topic = str(self.get_parameter("input_topic").value)
        output_topic = str(self.get_parameter("output_topic").value)
        self._publisher = self.create_publisher(VoiceCommandIntent, output_topic, 10)
        if self._input_mode == "utterance":
            self._subscription = self.create_subscription(
                SpeechUtterance,
                input_topic,
                self._on_utterance,
                10,
            )
        else:
            self._subscription = self.create_subscription(
                String,
                input_topic,
                self._on_transcript,
                10,
            )
        self.get_logger().info(
            "voice intent resolver ready: "
            f"{input_topic} -> {output_topic} "
            f"(input_mode={self._input_mode}, "
            f"selector={self.get_parameter('selector_mode').value}, "
            f"procedure={procedure_id or 'UNBOUND'})",
        )

    def _build_selector(self):
        mode = str(self.get_parameter("selector_mode").value).strip().lower()
        if mode == "deterministic":
            return DeterministicCandidateSelector(), False
        if mode in {"openai_compatible", "openai"}:
            return (
                OpenAICompatibleCandidateSelector(
                    endpoint=str(self.get_parameter("selector_endpoint").value),
                    model=str(self.get_parameter("selector_model").value),
                    timeout_sec=float(
                        self.get_parameter("selector_timeout_sec").value
                    ),
                ),
                True,
            )
        raise ValueError(
            "selector_mode must be 'deterministic' or 'openai_compatible'"
        )

    @staticmethod
    def _catalog_for_bundle(
        bundle: str,
    ) -> tuple[
        str,
        str,
        dict[str, tuple[str, ...]],
        tuple[str, ...],
        tuple[str, ...],
        float,
        bool,
    ]:
        """Load a procedure-local alias catalog without a global fallback."""

        catalog = load_voice_command_catalog(bundle)
        return (
            catalog.procedure_id,
            catalog.catalog_id,
            catalog.tool_aliases,
            tuple(catalog.ambiguous_aliases),
            catalog.retractor_commands,
            catalog.retractor_max_distance_m,
            catalog.retractor_require_explicit_unit,
        )

    def _load_active_catalog(
        self,
    ) -> tuple[
        str,
        str,
        dict[str, tuple[str, ...]],
        tuple[str, ...],
        float,
        bool,
    ]:
        bundle = str(self.get_parameter("procedure_bundle").value).strip()
        if not bundle:
            self.get_logger().warning(
                "procedure_bundle is empty; resolver will publish no executable proposals"
            )
            return "", "", {}, (), 0.0, True
        try:
            (
                procedure_id,
                catalog_id,
                tool_aliases,
                ambiguous_aliases,
                retractor_commands,
                retractor_max_distance_m,
                retractor_require_explicit_unit,
            ) = self._catalog_for_bundle(bundle)
        except (OSError, ValueError) as exc:
            self.get_logger().error(
                f"failed to load procedure_bundle {bundle!r}; fail-closed: {exc}"
            )
            return "", "", {}, (), 0.0, True
        if ambiguous_aliases:
            self.get_logger().warning(
                "dropped "
                f"{len(ambiguous_aliases)} ambiguous active-catalog voice aliases"
            )
        return (
            procedure_id,
            catalog_id,
            tool_aliases,
            retractor_commands,
            retractor_max_distance_m,
            retractor_require_explicit_unit,
        )

    def _on_parameters_changed(self, parameters) -> SetParametersResult:
        """Atomically replace the active catalog while the runtime is stopped.

        The simulation manager closes the Live admission lease before issuing
        this update.  Reject invalid bundles instead of turning an already
        running resolver into an unbound/global vocabulary fallback.
        """

        procedure_bundle = next(
            (
                parameter
                for parameter in parameters
                if parameter.name == "procedure_bundle"
            ),
            None,
        )
        if procedure_bundle is None:
            return SetParametersResult(successful=True)
        bundle = str(procedure_bundle.value).strip()
        if not bundle:
            return SetParametersResult(
                successful=False,
                reason="procedure_bundle must identify a procedure bundle",
            )
        try:
            (
                procedure_id,
                catalog_id,
                tool_aliases,
                ambiguous_aliases,
                retractor_commands,
                retractor_max_distance_m,
                retractor_require_explicit_unit,
            ) = self._catalog_for_bundle(bundle)
        except (OSError, ValueError) as exc:
            return SetParametersResult(
                successful=False,
                reason=f"failed to load procedure_bundle: {exc}",
            )
        requested_natural_variants = bool(
            self.get_parameter("enable_selector_natural_variants").value
        )
        next_resolver = VoiceIntentResolver(
            tool_aliases=tool_aliases,
            procedure_id=procedure_id,
            catalog_id=catalog_id,
            retractor_commands=retractor_commands,
            retractor_max_distance_m=retractor_max_distance_m,
            retractor_require_explicit_unit=retractor_require_explicit_unit,
            selector=self._selector,
            allow_selector_natural_variants=(
                requested_natural_variants and self._selector_is_model
            ),
        )
        self._resolver = next_resolver
        self._recent_utterance_ids = RecentUtteranceIds(
            self._typed_input_dedupe_retention_sec
        )
        if ambiguous_aliases:
            self.get_logger().warning(
                "dropped "
                f"{len(ambiguous_aliases)} ambiguous active-catalog voice aliases"
            )
        self.get_logger().info(
            "voice command catalog reloaded for "
            f"{procedure_id or 'UNBOUND'}"
        )
        return SetParametersResult(successful=True)

    def _on_transcript(self, message: String) -> None:
        self._publish_resolved(
            message.data,
            source=None,
            source_stamp=None,
        )

    def _on_utterance(self, message: SpeechUtterance) -> None:
        admission = evaluate_typed_speech_utterance(
            message,
            now_sec=self.get_clock().now().nanoseconds / 1_000_000_000.0,
            max_age_sec=self._typed_input_max_age_sec,
            max_future_skew_sec=self._typed_input_max_future_skew_sec,
        )
        if not admission.accepted:
            self.get_logger().warning(
                f"rejected typed ASR utterance: {admission.reason}",
                throttle_duration_sec=2.0,
            )
            return
        if not self._recent_utterance_ids.accept(
            message.utterance_id,
            time.monotonic(),
        ):
            self.get_logger().warning(
                "rejected typed ASR utterance: duplicate_utterance_id",
                throttle_duration_sec=2.0,
            )
            return
        self._publish_resolved(
            message.text,
            source=message,
            source_stamp=admission.stamp,
        )

    def _publish_resolved(
        self,
        text: str,
        *,
        source: SpeechUtterance | None,
        source_stamp,
    ) -> None:
        proposal = self._resolver.resolve(text)
        if not self._publish_no_command and proposal.disposition == "no_command":
            return
        output = VoiceCommandIntent()
        if source is None:
            # This route is retained for Debug/replay observation.  It cannot
            # satisfy the Live downstream source-metadata gate.
            output.header.stamp = self.get_clock().now().to_msg()
            output.header.frame_id = "voice_intent_resolver:legacy_sentence_text"
            output.utterance_id = (
                f"voice-intent-{output.header.stamp.sec}-{output.header.stamp.nanosec}"
                f"-{next(self._utterance_counter)}"
            )
            output.source = "legacy_sentence_text_compatibility"
            output.source_is_final = False
            output.source_speaker_role = ""
            output.source_has_confidence = False
            output.source_confidence = 0.0
        else:
            output.header.stamp = source_stamp
            output.header.frame_id = f"speech_utterance:{str(source.source).strip()}"
            output.utterance_id = str(source.utterance_id).strip()
            output.source = str(source.source).strip()
            output.source_is_final = bool(source.is_final)
            output.source_speaker_role = str(source.speaker_role or "").strip()
            output.source_has_confidence = bool(source.has_confidence)
            output.source_confidence = float(source.confidence)
        output.raw_text = proposal.raw_text
        output.normalized_text = proposal.normalized_text
        output.procedure_id = proposal.procedure_id
        output.catalog_id = proposal.catalog_id
        output.intent = proposal.intent
        output.tool_id = proposal.tool_id
        output.retractor_command = proposal.retractor_command
        output.target_side = proposal.target_side
        output.distance_m = proposal.distance_m
        output.urgency = proposal.urgency
        output.provenance = proposal.provenance
        output.requires_confirmation = proposal.requires_confirmation
        output.disposition = proposal.disposition
        output.reason = proposal.reason
        output.evidence_spans = list(proposal.evidence_spans)
        self._publisher.publish(output)


def main(args: list[str] | None = None) -> None:
    rclpy.init(args=args)
    node = VoiceIntentResolverNode()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()
