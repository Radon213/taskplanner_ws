"""ROS adapter for typed, grounded spoken-command interpretation."""

from __future__ import annotations

import itertools

import rclpy
from rcl_interfaces.msg import SetParametersResult
from rclpy.node import Node
from rclpy.parameter import Parameter
from rclpy.qos import (
    DurabilityPolicy,
    HistoryPolicy,
    QoSProfile,
    ReliabilityPolicy,
)
from std_msgs.msg import String

from surgical_msgs.msg import (
    SpeechUtterance,
    VoiceCommandIntent,
)
try:  # A focused resolver restart must still work on an old local overlay.
    from surgical_msgs.msg import SimulationState
except ImportError:  # pragma: no cover - generated interface availability
    SimulationState = None  # type: ignore[assignment,misc]
from procedure_spec import (
    ScenarioConfigSnapshot,
    load_voice_command_catalog,
    parse_scenario_config,
)

from .resolver import VoiceIntentResolver
from .scenario_reload import (
    scenario_config_bundle_path,
    scenario_config_reload_is_authorized,
)
from .selector import DeterministicCandidateSelector, OpenAICompatibleCandidateSelector


def _stamp_sec(stamp) -> float:
    return float(stamp.sec) + float(stamp.nanosec) / 1_000_000_000.0


def source_observation_stamp(msg: SpeechUtterance):
    """Return the source-time envelope stamp used for live command admission."""

    for stamp in (msg.stamp, msg.end_stamp, msg.start_stamp):
        if _stamp_sec(stamp) > 0.0:
            return stamp
    return None


class VoiceIntentResolverNode(Node):
    """Convert final STT text to typed intents without calling ROS actions."""

    def __init__(self) -> None:
        super().__init__("voice_intent_resolver")
        # The command router is the sole admitted-ASR subscriber.  It forwards
        # only catalog misses to this private topic, so the resolver cannot
        # become a parallel command ingress.
        self.declare_parameter("input_topic", "/surgery/voice/resolver_utterance")
        # ``sentence_text`` remains a Debug/replay observation route. It never
        # reaches an executable path because the router dispatches only a
        # proposal correlated to an utterance it forwarded itself.
        self.declare_parameter("input_mode", "utterance")
        # A proposal is consumed by command_router only. It is not a shared
        # DT/BT/simulation command bus and carries no function-gate envelope.
        self.declare_parameter("output_topic", "/surgery/voice/proposal")
        # Empty by default is fail-closed: tool aliases are derived only from
        # the active ProcedureSpec bundle, never a global T04-style mapping.
        self.declare_parameter("procedure_bundle", "")
        # ScenarioStore is the single writer for hot scenario selection.  The
        # resolver only observes its latched snapshot and swaps its local
        # catalog at an authoritative paused/stopped boundary.
        self.declare_parameter(
            "scenario_config_topic", "/simulation/scenario_config"
        )
        self.declare_parameter("simulation_state_topic", "/simulation/state")
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
        configured_input_topic = str(self.get_parameter("input_topic").value)
        if configured_input_topic == "/surgery/audio/admitted_utterance":
            raise ValueError(
                "voice_intent_resolver must not subscribe to admitted ASR; "
                "command_router owns that ingress"
            )
        self._scenario_config_revision = ""
        self._scenario_config_bundle = ""
        self._scenario_config_spec_dir = ""
        self._pending_scenario_config: ScenarioConfigSnapshot | None = None
        self._authoritative_state_received = False
        self._authoritative_running = False
        self._authoritative_execution_state = ""
        self.add_on_set_parameters_callback(self._on_parameters_changed)
        input_topic = configured_input_topic
        output_topic = str(self.get_parameter("output_topic").value)
        self._publisher = self.create_publisher(
            VoiceCommandIntent,
            output_topic,
            10,
        )
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
        if SimulationState is None:
            self.get_logger().warning(
                "scenario-config watcher disabled: SimulationState type is unavailable"
            )
        else:
            self.create_subscription(
                SimulationState,
                str(self.get_parameter("simulation_state_topic").value),
                self._on_simulation_state,
                20,
            )
            self.create_subscription(
                String,
                str(self.get_parameter("scenario_config_topic").value),
                self._on_scenario_config,
                QoSProfile(
                    history=HistoryPolicy.KEEP_LAST,
                    depth=1,
                    reliability=ReliabilityPolicy.RELIABLE,
                    durability=DurabilityPolicy.TRANSIENT_LOCAL,
                ),
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
        """Atomically replace the active catalog at an operator quiescence point."""

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
        if not self._scenario_config_reload_is_safe():
            return SetParametersResult(
                successful=False,
                reason=(
                    "procedure_bundle changes require an authoritative "
                    "paused or stopped SimulationState"
                ),
            )
        bundle = str(procedure_bundle.value).strip()
        try:
            self._replace_resolver_bundle(bundle)
        except (OSError, ValueError) as exc:
            return SetParametersResult(
                successful=False,
                reason=f"failed to load procedure_bundle: {exc}",
            )
        return SetParametersResult(successful=True)

    def _replace_resolver_bundle(self, bundle: str) -> None:
        """Build then atomically install one local resolver catalog.

        The candidate is completely parsed before assignment so a malformed
        authored revision leaves the last known-good resolver in place.
        """

        normalized_bundle = str(bundle).strip()
        if not normalized_bundle:
            raise ValueError("procedure_bundle must identify a procedure bundle")
        (
            procedure_id,
            catalog_id,
            tool_aliases,
            ambiguous_aliases,
            retractor_commands,
            retractor_max_distance_m,
            retractor_require_explicit_unit,
        ) = self._catalog_for_bundle(normalized_bundle)
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
        if ambiguous_aliases:
            self.get_logger().warning(
                "dropped "
                f"{len(ambiguous_aliases)} ambiguous active-catalog voice aliases"
            )
        self.get_logger().info(
            "voice command catalog reloaded for "
            f"{procedure_id or 'UNBOUND'}"
        )

    def _scenario_config_reload_is_safe(self) -> bool:
        """Do not turn a configuration observer into a runtime start gate."""

        return scenario_config_reload_is_authorized(
            state_received=self._authoritative_state_received,
            running=self._authoritative_running,
            execution_state=self._authoritative_execution_state,
        )

    def _on_simulation_state(self, message: SimulationState) -> None:
        self._authoritative_state_received = True
        self._authoritative_running = bool(message.running)
        self._authoritative_execution_state = str(
            message.execution_state or ""
        ).strip()
        self._apply_pending_scenario_config_if_safe()

    def _on_scenario_config(self, message: String) -> None:
        """Observe one ScenarioStore revision; no ASR path is gated here."""

        try:
            snapshot = parse_scenario_config(message.data)
            # Reject a stale retained revision before it can occupy the one
            # pending slot until the next paused/stopped boundary.
            scenario_config_bundle_path(snapshot)
        except (OSError, RuntimeError, ValueError) as exc:
            self.get_logger().warning(
                f"voice scenario config ignored: {exc}",
                throttle_duration_sec=2.0,
            )
            return
        if (
            snapshot.revision == self._scenario_config_revision
            and snapshot.bundle_name == self._scenario_config_bundle
            and snapshot.spec_dir == self._scenario_config_spec_dir
        ):
            return
        self._pending_scenario_config = snapshot
        self._apply_pending_scenario_config_if_safe()

    def _apply_pending_scenario_config_if_safe(self) -> None:
        snapshot = self._pending_scenario_config
        if snapshot is None or not self._scenario_config_reload_is_safe():
            return
        try:
            candidate_bundle = scenario_config_bundle_path(snapshot)
        except (OSError, RuntimeError, ValueError) as exc:
            self.get_logger().error(
                f"voice scenario config rejected before reload: {exc}"
            )
            self._pending_scenario_config = None
            return

        configured_bundle = str(
            self.get_parameter("procedure_bundle").value
        ).strip()
        if configured_bundle == candidate_bundle:
            try:
                # A same-bundle revision can change aliases or voice policy;
                # ROS may elide a no-op parameter set, so reload it directly.
                self._replace_resolver_bundle(candidate_bundle)
            except (OSError, ValueError) as exc:
                self.get_logger().error(
                    f"voice scenario config reload rejected: {exc}"
                )
                self._pending_scenario_config = None
                return
        else:
            result = self.set_parameters_atomically(
                [Parameter(name="procedure_bundle", value=candidate_bundle)]
            )
            if not bool(getattr(result, "successful", False)):
                self.get_logger().error(
                    "voice scenario config swap rejected: "
                    f"{getattr(result, 'reason', 'unknown reason')}"
                )
                self._pending_scenario_config = None
                return

        self._scenario_config_revision = snapshot.revision
        self._scenario_config_bundle = snapshot.bundle_name
        self._scenario_config_spec_dir = snapshot.spec_dir
        self._pending_scenario_config = None
        self.get_logger().info(
            "voice scenario revision applied atomically: "
            f"{snapshot.bundle_name}@{snapshot.revision}"
        )

    def _on_transcript(self, message: String) -> None:
        self._publish_resolved(
            message.data,
            source=None,
            source_stamp=None,
        )

    def _on_utterance(self, message: SpeechUtterance) -> None:
        # ASR final/source/freshness/TTS-echo/utterance-ID checks belong to
        # speech_input_adapter, before command_router.  Repeating them here
        # created a second admission boundary with drift-prone parameters.
        # Router correlation makes this private hop non-executable on its own.
        self._publish_resolved(
            message.text,
            source=message,
            source_stamp=source_observation_stamp(message)
            or self.get_clock().now().to_msg(),
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
        # The existing VoiceCommandIntent remains byte-for-byte compatible.
        # Resolver-only proposals do not claim gateway/run/function identity.
        output.gateway_instance_id = ""
        output.procedure_run_id = ""
        output.function_request_id = ""
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
