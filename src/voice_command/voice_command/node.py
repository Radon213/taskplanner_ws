"""ROS adapter for proposal-only spoken-command interpretation."""

from __future__ import annotations

import itertools

import rclpy
from rclpy.node import Node
from std_msgs.msg import String

from surgical_msgs.msg import VoiceCommandIntent
from procedure_spec import load_voice_command_catalog

from .resolver import VoiceIntentResolver
from .selector import DeterministicCandidateSelector, OpenAICompatibleCandidateSelector


class VoiceIntentResolverNode(Node):
    """Convert final STT text to typed proposals without calling ROS actions."""

    def __init__(self) -> None:
        super().__init__("voice_intent_resolver")
        self.declare_parameter("input_topic", "/surgery/audio/request_text")
        self.declare_parameter("output_topic", "/surgery/voice/intent")
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
        procedure_id, catalog_id, tool_aliases = self._load_active_catalog()
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
            selector=selector,
            allow_selector_natural_variants=enable_selector_natural_variants,
        )
        self._publish_no_command = bool(
            self.get_parameter("publish_no_command").value
        )
        self._utterance_counter = itertools.count(1)
        input_topic = str(self.get_parameter("input_topic").value)
        output_topic = str(self.get_parameter("output_topic").value)
        self._publisher = self.create_publisher(VoiceCommandIntent, output_topic, 10)
        self._subscription = self.create_subscription(
            String,
            input_topic,
            self._on_transcript,
            10,
        )
        self.get_logger().info(
            "voice intent resolver ready: "
            f"{input_topic} -> {output_topic} "
            f"(selector={self.get_parameter('selector_mode').value}, "
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

    def _load_active_catalog(self) -> tuple[str, str, dict[str, tuple[str, ...]]]:
        bundle = str(self.get_parameter("procedure_bundle").value).strip()
        if not bundle:
            self.get_logger().warning(
                "procedure_bundle is empty; resolver will publish no executable proposals"
            )
            return "", "", {}
        try:
            catalog = load_voice_command_catalog(bundle)
        except (OSError, ValueError) as exc:
            self.get_logger().error(
                f"failed to load procedure_bundle {bundle!r}; fail-closed: {exc}"
            )
            return "", "", {}
        if catalog.ambiguous_aliases:
            self.get_logger().warning(
                "dropped "
                f"{len(catalog.ambiguous_aliases)} ambiguous active-catalog voice aliases"
            )
        return catalog.procedure_id, catalog.catalog_id, catalog.tool_aliases

    def _on_transcript(self, message: String) -> None:
        proposal = self._resolver.resolve(message.data)
        if not self._publish_no_command and proposal.disposition == "no_command":
            return
        output = VoiceCommandIntent()
        output.header.stamp = self.get_clock().now().to_msg()
        output.header.frame_id = "voice_intent_resolver"
        output.utterance_id = (
            f"voice-intent-{output.header.stamp.sec}-{output.header.stamp.nanosec}"
            f"-{next(self._utterance_counter)}"
        )
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
