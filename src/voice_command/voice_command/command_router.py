"""Hot-reloadable direct ROS command router.

This is deliberately a thin transport adapter. It trusts the already admitted
typed ASR stream and dispatches a catalog match without asking the VLM, Digital
Twin, or BT to reconstruct the same deterministic command.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import Any

import rclpy
from rcl_interfaces.msg import SetParametersResult
from rclpy.action import ActionClient
from rclpy.node import Node
from rosidl_runtime_py.set_message import set_message_fields
from rosidl_runtime_py.utilities import get_action, get_message, get_service
from rclpy.qos import (
    DurabilityPolicy,
    HistoryPolicy,
    QoSProfile,
    ReliabilityPolicy,
)
from std_msgs.msg import String
from surgical_msgs.msg import SpeechUtterance, VoiceCommandIntent
from surgical_msgs.srv import ControlSimulation, InjectSurgeonOverride

from procedure_spec import load_bundle, parse_scenario_config

from .command_catalog import (
    CatalogCommand,
    CommandCatalogError,
    CommandCatalogReloader,
    CommandRouter,
    EXECUTION_RETRACTION_PROXY_SERVICE_ENDPOINT,
    EXECUTION_TOOL_HANDOVER_PROXY_ENDPOINT,
    RoutedCommand,
    deterministic_command_id,
    default_command_catalog_path,
    render_command_payload,
)
from .scenario_reload import scenario_config_bundle_path


ACTION_CANCEL_GRACE_SEC = 5.0
RESOLVER_PENDING_TTL_SEC = 10.0

_RETRACTION_COMMAND_CODES = {
    "start_direct_teach": 1,
    "finish_direct_teach": 2,
    "start_retraction": 3,
    "adjust_retraction": 4,
    "change_tool": 5,
    "stop_retraction": 6,
}
_RETRACTION_TARGET_SIDES = {"none": 0, "left": 1, "right": 2, "both": 3}


def default_start_phase_id_for_bundle(bundle: str) -> str:
    """Resolve the same authored default phase used by the Start button.

    The ScenarioStore owns bundle selection.  This small helper only reads the
    selected bundle's already-authored ``default_phase_id`` so a spoken start
    produces the same ``ControlSimulation`` payload as the UI's default Start
    button.  It deliberately does not involve VLM, Twin, or BT state.
    """

    normalized_bundle = str(bundle or "").strip()
    if not normalized_bundle:
        return ""
    phase_id = str(load_bundle(normalized_bundle).default_phase_id or "").strip()
    if not phase_id:
        raise ValueError("procedure bundle has no resolved default start phase")
    return phase_id


@dataclass(frozen=True, slots=True)
class _PendingResolverUtterance:
    source: str
    utterance_id: str
    text: str
    expires_monotonic: float


@dataclass(slots=True)
class _ActiveAction:
    """The router's short-lived observation of one dispatched Action goal.

    The controller remains authoritative for execution and idempotency.  This
    record exists only so the router does not lose ROS Action feedback, result,
    and cancellation semantics after the initial goal request.
    """

    routed: RoutedCommand
    deadline_monotonic: float
    goal_handle: Any | None = None
    result_future: Any | None = None
    cancel_future: Any | None = None
    cancel_requested: bool = False
    cancel_sent: bool = False
    cancel_reason: str = ""
    cancel_deadline_monotonic: float | None = None


class CommandRouterNode(Node):
    """Dispatch exact catalog commands from the admitted ASR stream."""

    def __init__(self) -> None:
        super().__init__("command_router")
        self.declare_parameter(
            "input_topic", "/surgery/audio/admitted_utterance"
        )
        # Router is the sole subscriber to admitted ASR.  It forwards only
        # exact-catalog misses to this private resolver ingress, then accepts
        # the corresponding typed proposal back on a separate private topic.
        self.declare_parameter(
            "resolver_input_topic", "/surgery/voice/resolver_utterance"
        )
        # VLM/logging/Debug observers receive this faithful relay rather than
        # attaching to the executable admitted-ASR ingress themselves.
        self.declare_parameter(
            "observed_utterance_topic", "/surgery/audio/observed_utterance"
        )
        self.declare_parameter(
            "resolver_output_topic", "/surgery/voice/proposal"
        )
        self.declare_parameter(
            "simulation_control_service", "/simulation/control"
        )
        self.declare_parameter(
            "operational_surgeon_override_service",
            "/simulation/operational_surgeon_override",
        )
        # The ScenarioStore is the only writer for the selected procedure.
        # This launch-time bundle gives the router the same immediate default
        # phase as the UI; the latched ScenarioStore notice then keeps it in
        # sync if the researcher selects another bundle.
        self.declare_parameter("procedure_bundle", "")
        self.declare_parameter(
            "scenario_config_topic", "/simulation/scenario_config"
        )
        self.declare_parameter(
            "catalog_path", str(default_command_catalog_path())
        )
        self.declare_parameter("catalog_reload_sec", 0.5)

        configured_catalog_path = str(
            self.get_parameter("catalog_path").value
        ).strip()
        self._catalog_path = configured_catalog_path or str(
            default_command_catalog_path()
        )
        self._catalog_reloader = CommandCatalogReloader(self._catalog_path)
        self._router: CommandRouter | None = None
        self._service_clients: dict[tuple[str, str], Any] = {}
        self._topic_publishers: dict[tuple[str, str], Any] = {}
        self._action_clients: dict[tuple[str, str], ActionClient] = {}
        self._active_actions: dict[str, _ActiveAction] = {}
        self._resolver_pending: dict[str, _PendingResolverUtterance] = {}
        self._simulation_control_service = str(
            self.get_parameter("simulation_control_service").value
        ).strip()
        self._operational_surgeon_override_service = str(
            self.get_parameter("operational_surgeon_override_service").value
        ).strip()
        self._default_start_phase_id = ""
        self._scenario_config_revision = ""
        self._scenario_config_bundle = ""
        self._scenario_config_spec_dir = ""
        self._load_initial_default_start_phase()
        self.add_on_set_parameters_callback(self._on_parameters_changed)
        self._reload_catalog(force=True)
        self._subscription = self.create_subscription(
            SpeechUtterance,
            str(self.get_parameter("input_topic").value),
            self._on_utterance,
            10,
        )
        self._resolver_input_publisher = self.create_publisher(
            SpeechUtterance,
            str(self.get_parameter("resolver_input_topic").value),
            10,
        )
        self._observed_utterance_publisher = self.create_publisher(
            SpeechUtterance,
            str(self.get_parameter("observed_utterance_topic").value),
            QoSProfile(
                history=HistoryPolicy.KEEP_LAST,
                depth=10,
                reliability=ReliabilityPolicy.RELIABLE,
                durability=DurabilityPolicy.TRANSIENT_LOCAL,
            ),
        )
        self._resolver_output_subscription = self.create_subscription(
            VoiceCommandIntent,
            str(self.get_parameter("resolver_output_topic").value),
            self._on_resolved_intent,
            10,
        )
        self._scenario_config_subscription = self.create_subscription(
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
        self._reload_timer = self.create_timer(
            max(0.1, float(self.get_parameter("catalog_reload_sec").value)),
            self._reload_catalog,
        )
        # The Action timeout is catalog-owned per command.  This timer only
        # observes and cancels an already-dispatched goal; it never re-applies
        # scenario or VLM policy.
        self._action_lifecycle_timer = self.create_timer(
            0.1, self._poll_action_timeouts
        )
        self.get_logger().info(
            "command router ready: "
            f"{self.get_parameter('input_topic').value} -> catalog "
            f"{self._catalog_path}; resolver misses -> "
            f"{self.get_parameter('resolver_input_topic').value}"
        )

    def _load_initial_default_start_phase(self) -> None:
        """Install the launch-selected bundle's phase without blocking startup."""

        bundle = str(self.get_parameter("procedure_bundle").value).strip()
        if not bundle:
            return
        try:
            self._default_start_phase_id = default_start_phase_id_for_bundle(bundle)
        except (OSError, RuntimeError, ValueError) as exc:
            self.get_logger().warning(
                "voice start will use the simulation current phase until the "
                f"ScenarioStore snapshot arrives: {exc}"
            )

    def _on_scenario_config(self, message: String) -> None:
        """Follow the selected bundle's UI-equivalent default start phase.

        This is a read-only ScenarioStore binding.  It never blocks ASR
        ingress or asks a running procedure to reload: the store itself only
        publishes a new selected bundle at its allowed boundary.
        """

        try:
            snapshot = parse_scenario_config(message.data)
            candidate_bundle = scenario_config_bundle_path(snapshot)
            phase_id = default_start_phase_id_for_bundle(candidate_bundle)
        except (OSError, RuntimeError, ValueError) as exc:
            self.get_logger().warning(
                f"voice start scenario config ignored; keeping last valid phase: {exc}",
                throttle_duration_sec=2.0,
            )
            return
        if (
            snapshot.revision == getattr(self, "_scenario_config_revision", "")
            and snapshot.bundle_name == getattr(self, "_scenario_config_bundle", "")
            and snapshot.spec_dir == getattr(self, "_scenario_config_spec_dir", "")
        ):
            return
        self._default_start_phase_id = phase_id
        self._scenario_config_revision = snapshot.revision
        self._scenario_config_bundle = snapshot.bundle_name
        self._scenario_config_spec_dir = snapshot.spec_dir
        self.get_logger().info(
            "voice procedure start now follows ScenarioStore bundle "
            f"{snapshot.bundle_name}@{snapshot.revision} (phase={phase_id})"
        )

    def _on_parameters_changed(self, parameters) -> SetParametersResult:
        for parameter in parameters:
            if parameter.name == "catalog_path":
                candidate = str(parameter.value).strip()
                if not candidate:
                    return SetParametersResult(
                        successful=False,
                        reason="catalog_path must be non-empty",
                    )
                try:
                    reloader = CommandCatalogReloader(candidate)
                    changed, error = reloader.reload_if_changed(force=True)
                    if not changed:
                        raise CommandCatalogError(error or "catalog did not load")
                except CommandCatalogError as exc:
                    return SetParametersResult(successful=False, reason=str(exc))
                self._catalog_path = candidate
                self._catalog_reloader = reloader
                self._set_catalog(reloader)
        return SetParametersResult(successful=True)

    def _reload_catalog(self, force: bool = False) -> None:
        changed, error = self._catalog_reloader.reload_if_changed(force=force)
        if changed:
            self._set_catalog(self._catalog_reloader)
            names = ", ".join(
                command.name for command in self._catalog_reloader.catalog.commands
            )
            self.get_logger().info(f"command catalog loaded: {names}")
        elif error:
            self.get_logger().error(
                f"command catalog rejected; keeping last valid catalog: {error}",
                throttle_duration_sec=2.0,
            )

    def _set_catalog(self, reloader: CommandCatalogReloader) -> None:
        if reloader.catalog is None:
            return
        self._router = CommandRouter(reloader.catalog)

    def _on_utterance(self, message: SpeechUtterance) -> None:
        # ``speech_input_adapter`` is the only ASR ingress owner. It already
        # handles final/source/freshness/TTS echo/dedupe before publishing this
        # topic. Repeating those policy checks here made a one-line catalog
        # command depend on a second, drifting admission model.
        observed_publisher = getattr(self, "_observed_utterance_publisher", None)
        if observed_publisher is not None:
            # The relay changes no field and has no return path into dispatch.
            observed_publisher.publish(message)
        router = self._router
        if router is None:
            self.get_logger().error("command router has no valid command catalog")
            return
        if router.has_multiple_command_spans(message.text):
            # Do not fall through to the resolver: it intentionally does not
            # own catalog-only commands such as suction, so forwarding a mixed
            # segment there could still execute the other fragment.
            self.get_logger().warning(
                f"ignored multi-command ASR final {message.text!r}",
                throttle_duration_sec=2.0,
            )
            return
        try:
            routed = router.route(
                message.text,
                source=str(getattr(message, "source", "") or ""),
                utterance_id=str(
                    getattr(message, "utterance_id", "") or ""
                ),
            )
        except CommandCatalogError as exc:
            self.get_logger().warning(
                f"command router did not dispatch {message.text!r}: {exc}",
                throttle_duration_sec=2.0,
            )
            return
        if routed is None:
            self._forward_to_resolver(message)
            return
        self._dispatch(routed)

    @staticmethod
    def _resolver_key(source: object, utterance_id: object) -> str:
        normalized_source = str(source or "").strip()
        normalized_utterance_id = str(utterance_id or "").strip()
        return (
            f"{normalized_source}:{normalized_utterance_id}"
            if normalized_source and normalized_utterance_id
            else ""
        )

    def _forward_to_resolver(self, message: SpeechUtterance) -> None:
        """Forward one non-catalog utterance without re-admitting ASR.

        The small correlation record is not an ASR dedupe or safety policy. It
        merely proves that the proposal returning from the resolver belongs to
        an utterance this router actually forwarded, so a second publisher of
        the proposal topic cannot create a command lane.
        """

        source = str(getattr(message, "source", "") or "").strip()
        utterance_id = str(getattr(message, "utterance_id", "") or "").strip()
        key = self._resolver_key(source, utterance_id)
        if not key:
            self.get_logger().warning(
                "resolver miss ignored because admitted utterance has no identity"
            )
            return
        now = time.monotonic()
        pending = getattr(self, "_resolver_pending", None)
        if pending is None:
            pending = {}
            self._resolver_pending = pending
        for stale_key, item in tuple(pending.items()):
            if item.expires_monotonic <= now:
                pending.pop(stale_key, None)
        pending[key] = _PendingResolverUtterance(
            source=source,
            utterance_id=utterance_id,
            text=str(getattr(message, "text", "") or ""),
            expires_monotonic=now + RESOLVER_PENDING_TTL_SEC,
        )
        publisher = getattr(self, "_resolver_input_publisher", None)
        if publisher is None:
            pending.pop(key, None)
            self.get_logger().error("command router resolver publisher is unavailable")
            return
        publisher.publish(message)

    def _on_resolved_intent(self, message: VoiceCommandIntent) -> None:
        """Dispatch the resolver's single correlated semantic proposal."""

        key = self._resolver_key(
            getattr(message, "source", ""),
            getattr(message, "utterance_id", ""),
        )
        pending = getattr(self, "_resolver_pending", {})
        # Read, validate, then consume. A malformed or forged proposal must
        # not be able to erase the real resolver response that is still on its
        # way for the same admitted utterance.
        item = pending.get(key) if key else None
        if item is None:
            self.get_logger().warning(
                "ignored uncorrelated resolver proposal",
                throttle_duration_sec=2.0,
            )
            return
        if item.expires_monotonic <= time.monotonic():
            pending.pop(key, None)
            self.get_logger().warning(
                "ignored expired resolver proposal",
                throttle_duration_sec=2.0,
            )
            return
        if str(getattr(message, "raw_text", "") or "") != item.text:
            self.get_logger().warning("ignored resolver proposal with text mismatch")
            return
        pending.pop(key, None)
        self._dispatch_resolved_intent(message)

    def _dispatch_resolved_intent(self, message: VoiceCommandIntent) -> None:
        """Map a grounded semantic intent to one typed endpoint adapter.

        This intentionally does not consult a scenario allowlist, Behavior
        Tree, Digital Twin receipt, or VLM response.  Endpoint-side state,
        range, type, idempotency, feedback, result, and cancellation checks
        remain the final authority.
        """

        if str(getattr(message, "disposition", "") or "").strip() != "propose":
            return
        if bool(getattr(message, "requires_confirmation", False)):
            return
        source = str(getattr(message, "source", "") or "").strip()
        utterance_id = str(getattr(message, "utterance_id", "") or "").strip()
        if not source or not utterance_id:
            return
        intent = str(getattr(message, "intent", "") or "").strip()
        routed: RoutedCommand | None = None
        if intent == "procedure_start":
            routed = self._typed_service_command(
                name="voice_procedure_start",
                prefix="voice-procedure-start",
                interface_type="surgical_msgs/srv/ControlSimulation",
                endpoint=self._simulation_control_service,
                payload={
                    "command": "start",
                    "start_phase_id": str(
                        getattr(self, "_default_start_phase_id", "") or ""
                    ),
                },
                source=source,
                utterance_id=utterance_id,
            )
        elif intent == "procedure_stop":
            routed = self._typed_service_command(
                # A spoken procedure end is deliberately distinct from the
                # operator UI's immediate Stop control.  The runtime expands
                # this typed finish request into the bounded Mayo cleanup
                # workflow before it emits the terminal completion receipt.
                name="voice_procedure_finish",
                prefix="voice-procedure-finish",
                interface_type="surgical_msgs/srv/ControlSimulation",
                endpoint=self._simulation_control_service,
                payload={"command": "finish", "start_phase_id": ""},
                source=source,
                utterance_id=utterance_id,
            )
        elif intent == "tool_handover":
            tool_id = str(getattr(message, "tool_id", "") or "").strip()
            if not tool_id:
                return
            routed = self._typed_service_command(
                name="voice_tool_handover",
                prefix="voice-tool-handover",
                interface_type="surgical_msgs/srv/InjectSurgeonOverride",
                endpoint=self._operational_surgeon_override_service,
                payload={
                    "event_type": "voice_request",
                    "requested_tool": tool_id,
                    "voice_text": "",
                    "ready_for_handover": True,
                    "ready_for_retrieval": False,
                    "clear_pending_requests": False,
                },
                source=source,
                utterance_id=utterance_id,
            )
        elif intent == "tool_retrieve":
            tool_id = str(getattr(message, "tool_id", "") or "").strip()
            if not tool_id:
                return
            routed = self._typed_service_command(
                name="voice_tool_retrieve",
                prefix="voice-tool-retrieve",
                interface_type="surgical_msgs/srv/InjectSurgeonOverride",
                endpoint=self._operational_surgeon_override_service,
                payload={
                    "event_type": "return_tool",
                    "requested_tool": tool_id,
                    "voice_text": str(getattr(message, "raw_text", "") or ""),
                    "ready_for_handover": False,
                    "ready_for_retrieval": True,
                    "clear_pending_requests": False,
                },
                source=source,
                utterance_id=utterance_id,
            )
        elif intent == "retractor_command":
            routed = self._typed_retraction_command(
                message,
                source=source,
                utterance_id=utterance_id,
            )
        if routed is not None:
            self._dispatch(routed)

    @staticmethod
    def _typed_service_command(
        *,
        name: str,
        prefix: str,
        interface_type: str,
        endpoint: str,
        payload: dict[str, Any],
        source: str,
        utterance_id: str,
    ) -> RoutedCommand:
        command_id = deterministic_command_id(
            prefix,
            source=source,
            utterance_id=utterance_id,
            command_name=name,
        )
        command = CatalogCommand(
            name=name,
            phrases=(),
            kind="service",
            interface_type=interface_type,
            endpoint=endpoint,
            payload=dict(payload),
            command_id_prefix=prefix,
            action_timeout_sec=None,
        )
        rendered_payload = render_command_payload(payload, command_id)
        return RoutedCommand(
            command=command,
            command_id=command_id,
            endpoint=endpoint,
            payload=rendered_payload,
        )

    def _typed_retraction_command(
        self,
        message: VoiceCommandIntent,
        *,
        source: str,
        utterance_id: str,
    ) -> RoutedCommand | None:
        command_name = str(
            getattr(message, "retractor_command", "") or ""
        ).strip()
        command_code = _RETRACTION_COMMAND_CODES.get(command_name)
        if command_code is None:
            self.get_logger().warning(
                f"ignored unsupported typed retraction command {command_name!r}"
            )
            return None
        target_side_name = str(
            getattr(message, "target_side", "none") or "none"
        ).strip().casefold()
        target_side = _RETRACTION_TARGET_SIDES.get(target_side_name)
        if target_side is None:
            self.get_logger().warning("ignored typed retraction command with invalid side")
            return None
        try:
            distance_m = float(getattr(message, "distance_m", 0.0))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(distance_m):
            return None
        if command_name == "adjust_retraction" and distance_m == 0.0:
            return None
        if command_name != "adjust_retraction":
            distance_m = 0.0
            if command_name not in {"finish_direct_teach"}:
                target_side = 0
        prefix = f"voice-{command_name.replace('_', '-')}"
        return self._typed_service_command(
            name=f"voice_{command_name}",
            prefix=prefix,
            interface_type="surgical_interop_msgs/srv/ExecuteRetractionCommand",
            endpoint=EXECUTION_RETRACTION_PROXY_SERVICE_ENDPOINT,
            payload={
                "protocol_version": 1,
                "source_id": "taskplanner",
                "command_id": "{command_id}",
                "command": command_code,
                "target_side": target_side,
                "distance_m": distance_m,
            },
            source=source,
            utterance_id=utterance_id,
        )

    def _dispatch(self, routed: RoutedCommand) -> None:
        try:
            self._assert_execution_proxy_boundary(routed)
            if routed.command.kind == "service":
                self._dispatch_service(routed)
            elif routed.command.kind == "topic":
                self._dispatch_topic(routed)
            elif routed.command.kind == "action":
                self._dispatch_action(routed)
            else:  # Catalog validation prevents this; keep callback resilient.
                raise CommandCatalogError(
                    f"unsupported command transport {routed.command.kind!r}"
                )
        except Exception as exc:  # A bad research catalog must not kill ASR.
            self.get_logger().error(
                f"command {routed.command.name} was not dispatched: "
                f"{type(exc).__name__}: {exc}"
            )

    @staticmethod
    def _assert_execution_proxy_boundary(routed: RoutedCommand) -> None:
        """Fence physical catalog transport behind stable execution proxies."""

        interface_type = routed.command.interface_type
        if interface_type == "surgical_interop_msgs/srv/ExecuteRetractionCommand":
            if routed.endpoint != EXECUTION_RETRACTION_PROXY_SERVICE_ENDPOINT:
                raise CommandCatalogError(
                    "physical retraction commands must use the execution proxy endpoint"
                )
        if interface_type == "surgical_interop_msgs/action/ExecuteToolHandover":
            if routed.endpoint != EXECUTION_TOOL_HANDOVER_PROXY_ENDPOINT:
                raise CommandCatalogError(
                    "physical tool-handover commands must use the execution proxy endpoint"
                )

    def _dispatch_service(self, routed: RoutedCommand) -> None:
        key = (routed.command.interface_type, routed.endpoint)
        client = self._service_clients.get(key)
        service_type = get_service(routed.command.interface_type)
        if client is None:
            client = self.create_client(service_type, routed.endpoint)
            self._service_clients[key] = client
        if not client.service_is_ready():
            self.get_logger().warning(
                f"service unavailable for {routed.command.name}: {routed.endpoint}"
            )
            return
        request = service_type.Request()
        set_message_fields(request, dict(routed.payload))
        future = client.call_async(request)
        future.add_done_callback(
            lambda result, command=routed: self._on_service_result(command, result)
        )
        self.get_logger().info(
            f"dispatched {routed.command.name} command_id={routed.command_id} "
            f"to {routed.endpoint}"
        )

    def _dispatch_topic(self, routed: RoutedCommand) -> None:
        key = (routed.command.interface_type, routed.endpoint)
        publisher = self._topic_publishers.get(key)
        message_type = get_message(routed.command.interface_type)
        if publisher is None:
            publisher = self.create_publisher(message_type, routed.endpoint, 10)
            self._topic_publishers[key] = publisher
        message = message_type()
        set_message_fields(message, dict(routed.payload))
        publisher.publish(message)
        self.get_logger().info(
            f"published {routed.command.name} command_id={routed.command_id} "
            f"to {routed.endpoint}"
        )

    def _dispatch_action(self, routed: RoutedCommand) -> None:
        key = (routed.command.interface_type, routed.endpoint)
        client = self._action_clients.get(key)
        action_type = get_action(routed.command.interface_type)
        if client is None:
            client = ActionClient(self, action_type, routed.endpoint)
            self._action_clients[key] = client
        if not client.server_is_ready():
            self.get_logger().warning(
                f"action server unavailable for {routed.command.name}: "
                f"{routed.endpoint}"
            )
            return
        goal = action_type.Goal()
        set_message_fields(goal, dict(routed.payload))
        active_actions = self._active_action_map()
        if routed.command_id in active_actions:
            raise CommandCatalogError(
                f"action command_id is already active: {routed.command_id}"
            )
        timeout_sec = routed.command.action_timeout_sec
        if timeout_sec is None or timeout_sec <= 0.0:
            # Catalog validation supplies this for Actions.  Keep the adapter
            # bounded if a hand-built RoutedCommand reaches it in a test.
            raise CommandCatalogError(
                f"action {routed.command.name} has no positive completion timeout"
            )
        deadline = time.monotonic() + timeout_sec
        future = client.send_goal_async(
            goal,
            feedback_callback=lambda feedback, command=routed: self._on_action_feedback(
                command, feedback
            ),
        )
        active_actions[routed.command_id] = _ActiveAction(
            routed=routed,
            deadline_monotonic=deadline,
        )
        future.add_done_callback(
            lambda result, command=routed: self._on_action_goal_response(command, result)
        )
        self.get_logger().info(
            f"sent {routed.command.name} command_id={routed.command_id} "
            f"to {routed.endpoint}"
        )

    def _on_service_result(self, routed: RoutedCommand, future) -> None:
        try:
            response = future.result()
        except Exception as exc:
            self.get_logger().error(
                f"service result failed for {routed.command.name} "
                f"command_id={routed.command_id}: {exc}"
            )
            return
        # The small simulation override service reports ``success`` while the
        # execution proxy responses expose ``request_accepted``.  Preserve the
        # latter when present, but do not turn a valid manual-return response
        # into the misleading ``accepted=None`` operator log.
        accepted = getattr(
            response,
            "request_accepted",
            getattr(response, "success", None),
        )
        response_id = str(getattr(response, "command_id", ""))
        self.get_logger().info(
            f"service result for {routed.command.name} command_id={routed.command_id} "
            f"accepted={accepted!r} response_command_id={response_id!r}"
        )

    def _on_action_goal_response(self, routed: RoutedCommand, future) -> None:
        try:
            goal_handle = future.result()
        except Exception as exc:
            self.get_logger().error(
                f"action goal failed for {routed.command.name} "
                f"command_id={routed.command_id}: {exc}"
            )
            self._finish_action_observation(
                routed.command_id,
                state="goal_response_error",
                detail=str(exc),
            )
            return
        active = self._active_action_map().get(routed.command_id)
        self.get_logger().info(
            f"action goal for {routed.command.name} command_id={routed.command_id} "
            f"accepted={bool(getattr(goal_handle, 'accepted', False))}"
        )
        if not bool(getattr(goal_handle, "accepted", False)):
            self._finish_action_observation(
                routed.command_id,
                state="rejected",
                detail="goal_rejected",
            )
            return
        if active is None:
            # The request exceeded its bounded observation window before the
            # server replied.  Do not allow a late acceptance to execute
            # unnoticed; issue a best-effort cancellation immediately.
            self.get_logger().warning(
                f"late accepted action goal for {routed.command.name} "
                f"command_id={routed.command_id}; requesting cancellation"
            )
            self._cancel_orphaned_goal(routed, goal_handle)
            return
        active.goal_handle = goal_handle
        try:
            result_future = goal_handle.get_result_async()
        except Exception as exc:
            self.get_logger().error(
                f"action result future failed for {routed.command.name} "
                f"command_id={routed.command_id}: {exc}"
            )
            self._finish_action_observation(
                routed.command_id,
                state="result_future_error",
                detail=str(exc),
            )
            return
        active.result_future = result_future
        result_future.add_done_callback(
            lambda result, command=routed: self._on_action_result(command, result)
        )
        if active.cancel_requested:
            self._send_action_cancel(routed.command_id, active)

    def _on_action_feedback(self, routed: RoutedCommand, feedback_message: Any) -> None:
        """Observe generic Action feedback without interpreting its schema."""

        if routed.command_id not in self._active_action_map():
            return
        feedback = getattr(feedback_message, "feedback", feedback_message)
        self.get_logger().info(
            f"action feedback for {routed.command.name} "
            f"command_id={routed.command_id}: {self._brief(feedback)}"
        )

    def _on_action_result(self, routed: RoutedCommand, future: Any) -> None:
        try:
            wrapped = future.result()
            status = getattr(wrapped, "status", None)
            result = getattr(wrapped, "result", wrapped)
        except Exception as exc:
            self._finish_action_observation(
                routed.command_id,
                state="result_error",
                detail=str(exc),
            )
            return
        active = self._active_action_map().pop(routed.command_id, None)
        if active is None:
            self.get_logger().warning(
                f"late action result for {routed.command.name} "
                f"command_id={routed.command_id}: status={status!r} "
                f"result={self._brief(result)}"
            )
            return
        self.get_logger().info(
            f"action result for {routed.command.name} command_id={routed.command_id} "
            f"status={status!r} cancel_requested={active.cancel_requested} "
            f"result={self._brief(result)}"
        )

    def cancel_action(self, command_id: str, *, reason: str = "operator_request") -> bool:
        """Request cancellation of one active Action by its stable command ID.

        This is deliberately a router-local interface rather than another
        generic ROS control service.  Callers that need physical stop behavior
        must use the controller's explicit stop command; this only preserves
        the Action cancellation protocol for the goal the router owns.
        """

        active = self._active_action_map().get(str(command_id))
        if active is None:
            return False
        if not active.cancel_requested:
            active.cancel_requested = True
            active.cancel_reason = str(reason or "cancel_requested")
            active.cancel_deadline_monotonic = (
                time.monotonic() + ACTION_CANCEL_GRACE_SEC
            )
            self.get_logger().warning(
                f"action cancellation requested for {active.routed.command.name} "
                f"command_id={active.routed.command_id} reason={active.cancel_reason}"
            )
        if active.goal_handle is None:
            # A goal response may still arrive.  _on_action_goal_response will
            # send the cancellation as soon as a handle exists.
            return True
        return self._send_action_cancel(str(command_id), active)

    def cancel_all_actions(self, *, reason: str = "router_shutdown") -> tuple[str, ...]:
        """Best-effort cancellation hook for a focused router restart."""

        cancelled: list[str] = []
        for command_id in tuple(self._active_action_map()):
            if self.cancel_action(command_id, reason=reason):
                cancelled.append(command_id)
        return tuple(cancelled)

    def _send_action_cancel(self, command_id: str, active: _ActiveAction) -> bool:
        if active.cancel_sent or active.goal_handle is None:
            return False
        active.cancel_sent = True
        try:
            future = active.goal_handle.cancel_goal_async()
        except Exception as exc:
            self.get_logger().error(
                f"action cancellation failed for {active.routed.command.name} "
                f"command_id={command_id}: {exc}"
            )
            return False
        active.cancel_future = future
        future.add_done_callback(
            lambda result, routed=active.routed: self._on_action_cancel_response(
                routed, result
            )
        )
        return True

    def _on_action_cancel_response(self, routed: RoutedCommand, future: Any) -> None:
        try:
            response = future.result()
            accepted = bool(getattr(response, "goals_canceling", ()))
        except Exception as exc:
            self.get_logger().error(
                f"action cancellation response failed for {routed.command.name} "
                f"command_id={routed.command_id}: {exc}"
            )
            return
        level = self.get_logger().info if accepted else self.get_logger().warning
        level(
            f"action cancellation for {routed.command.name} "
            f"command_id={routed.command_id} accepted={accepted}"
        )

    def _poll_action_timeouts(self) -> None:
        """Bound generic Action observation and request protocol cancellation."""

        now = time.monotonic()
        for command_id, active in tuple(self._active_action_map().items()):
            if not active.cancel_requested and now >= active.deadline_monotonic:
                self.cancel_action(command_id, reason="completion_timeout")
                continue
            if (
                active.cancel_requested
                and active.cancel_deadline_monotonic is not None
                and now >= active.cancel_deadline_monotonic
            ):
                current = self._active_action_map().get(command_id)
                if current is active:
                    self._active_action_map().pop(command_id, None)
                    self.get_logger().error(
                        f"action lifecycle timed out after cancellation for "
                        f"{active.routed.command.name} command_id={command_id}; "
                        "controller state is not inferred"
                    )

    def _finish_action_observation(
        self, command_id: str, *, state: str, detail: str
    ) -> None:
        active = self._active_action_map().pop(command_id, None)
        if active is None:
            return
        self.get_logger().warning(
            f"action {state} for {active.routed.command.name} "
            f"command_id={command_id}: {detail}"
        )

    def _cancel_orphaned_goal(
        self, routed: RoutedCommand, goal_handle: Any
    ) -> None:
        try:
            future = goal_handle.cancel_goal_async()
        except Exception as exc:
            self.get_logger().error(
                f"late action cancellation failed for {routed.command.name} "
                f"command_id={routed.command_id}: {exc}"
            )
            return
        future.add_done_callback(
            lambda result, command=routed: self._on_action_cancel_response(
                command, result
            )
        )

    def _active_action_map(self) -> dict[str, _ActiveAction]:
        """Keep direct adapter tests independent from full Node construction."""

        active = getattr(self, "_active_actions", None)
        if active is None:
            active = {}
            self._active_actions = active
        return active

    @staticmethod
    def _brief(value: Any, *, limit: int = 512) -> str:
        text = repr(value)
        return text if len(text) <= limit else f"{text[: limit - 3]}..."

    def destroy_node(self):
        self.cancel_all_actions(reason="router_shutdown")
        return super().destroy_node()


def main(args: list[str] | None = None) -> None:
    rclpy.init(args=args)
    node = CommandRouterNode()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()


__all__ = ["CommandRouterNode", "main"]
