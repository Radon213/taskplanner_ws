"""Stable execution-owner proxy endpoints for operator command adapters.

Command producers deliberately never learn whether a controller route is
external or virtual.  They call the two stable endpoints here; this process
observes the bridge-owned route snapshot and keeps one request bound to that
snapshot until its Service receipt or Action terminal result.

This is intentionally a narrow transport proxy.  It does not interpret ASR or
VLM output.  It does enforce the authoritative running-scenario admission
edge for non-stop retraction commands before forwarding them to a controller.
Controller validation, idempotency, feedback, result, and cancellation remain
intact.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import threading
import time
from typing import Any

import rclpy
from rclpy.action import ActionClient, ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import (
    QoSDurabilityPolicy,
    QoSHistoryPolicy,
    QoSProfile,
    QoSReliabilityPolicy,
)
from std_msgs.msg import String
from surgical_msgs.msg import SimulationState

from surgical_interop_msgs.action import ExecuteToolHandover
from surgical_interop_msgs.srv import ExecuteRetractionCommand

from .direct_hand_ledger import valid_procedure_run_id
from .mappings import retraction_request_allowed_by_scenario

from .virtual_endpoints import (
    EXECUTION_RETRACTION_PROXY_SERVICE_ENDPOINT,
    EXECUTION_ROUTE_STATE_TOPIC,
    EXECUTION_TOOL_HANDOVER_PROXY_ENDPOINT,
    VIRTUAL_ENDPOINT_SOURCE,
    is_isolated_virtual_endpoint,
    parse_execution_route_state,
)


EXECUTION_PROXY_ACTIVITY_SCHEMA = "taskplanner.execution_proxy_activity.v1"
EXECUTION_PROXY_ACTIVITY_TOPIC = "/taskplanner/execution/proxy_activity"
# The command proxy owns the lifecycle of direct typed requests.  It publishes
# these small private facts for the execution bridge, which remains the *only*
# producer of the public /surgery/execution_trace observer stream.
EXECUTION_PROXY_LIFECYCLE_SCHEMA = "taskplanner.execution_proxy_lifecycle.v1"
EXECUTION_PROXY_LIFECYCLE_TOPIC = "/taskplanner/execution/proxy_lifecycle"


def retraction_route_source(route: object) -> str:
    """Return the source latched for this Service request's route snapshot."""

    return str(
        getattr(route, "run_retraction_source", "")
        or getattr(route, "retraction_source", "")
    ).strip().casefold()


def is_isolated_virtual_retraction_route(route: object) -> bool:
    """True only for the dedicated virtual retraction Service namespace."""

    return (
        retraction_route_source(route) == VIRTUAL_ENDPOINT_SOURCE
        and is_isolated_virtual_endpoint(
            str(getattr(route, "retraction_service_name", ""))
        )
    )


def tool_handover_route_source(route: object) -> str:
    """Return the source latched for one tool-handover proxy Goal."""

    return str(
        getattr(route, "run_endpoint_source", "")
        or getattr(route, "selected_source", "")
    ).strip().casefold()


def validate_retraction_proxy_request(request: object) -> str:
    """Return a bounded rejection code for malformed public Service input."""

    try:
        protocol_version = int(getattr(request, "protocol_version"))
    except (TypeError, ValueError):
        return "invalid_protocol_version"
    if protocol_version != ExecuteRetractionCommand.Request.PROTOCOL_VERSION_V1:
        return "unsupported_protocol_version"
    if not str(getattr(request, "source_id", "") or "").strip():
        return "missing_source_id"
    if not str(getattr(request, "command_id", "") or "").strip():
        return "missing_command_id"
    try:
        distance_m = float(getattr(request, "distance_m"))
    except (TypeError, ValueError):
        return "invalid_distance_m"
    if not math.isfinite(distance_m):
        return "invalid_distance_m"
    return ""


def validate_tool_handover_proxy_goal(goal: object) -> str:
    """Keep the proxy a type boundary, not a second planning policy."""

    for field in (
        "command_id",
        "instrument_id",
        "instrument_instance_id",
        "source_location",
        "target_location",
    ):
        if not str(getattr(goal, field, "") or "").strip():
            return f"missing_{field}"
    return ""


@dataclass(slots=True)
class _ProxyAction:
    command_id: str
    upstream_goal_handle: Any | None = None
    downstream_goal_handle: Any | None = None
    cancel_requested: bool = False
    # Set immediately before ``send_goal_async``. It records that control may
    # already have crossed this owner boundary before ROS delivers a
    # GoalHandle, so stop/reset cannot make the controller lane look free.
    dispatched: bool = False
    endpoint: str = ""
    endpoint_source: str = ""
    dispatch_epoch: int = 0
    # A stopped run no longer owns the visible Action lane, but the controller
    # Goal remains a recovery concern until its terminal result or this bounded
    # timeout.  Keeping that distinction here prevents a clean UI reset from
    # silently forgetting a controller-facing request.
    recovery_deadline_monotonic: float | None = None


@dataclass(frozen=True, slots=True)
class _ProxyRequestActivity:
    """Source and lifetime identity for a direct proxy request."""

    endpoint_source: str
    dispatch_epoch: int
    procedure_run_id: str = ""


@dataclass(slots=True)
class _PendingRetractionReceipt:
    """One submitted Service call kept until receipt or bounded recovery expiry."""

    command_id: str
    future: Any
    dispatch_epoch: int
    request: object
    route: object
    procedure_run_id: str
    recovery_deadline_monotonic: float


class ExecutionCommandProxy(Node):
    """Expose fixed Service/Action names while the bridge owns route choice."""

    def __init__(self) -> None:
        super().__init__("execution_command_proxy")
        self.declare_parameter("route_state_topic", EXECUTION_ROUTE_STATE_TOPIC)
        self.declare_parameter(
            "retraction_proxy_service",
            EXECUTION_RETRACTION_PROXY_SERVICE_ENDPOINT,
        )
        self.declare_parameter(
            "tool_handover_proxy_action",
            EXECUTION_TOOL_HANDOVER_PROXY_ENDPOINT,
        )
        # ``service_receipt_timeout_sec`` used to make this Service callback
        # busy-poll a downstream Future.  Retain the parameter declaration so
        # existing launch files remain valid, but the proxy now acknowledges
        # local submission immediately and uses one bounded controller-recovery
        # window instead of occupying executor threads.
        self.declare_parameter("service_receipt_timeout_sec", 2.0)
        self.declare_parameter("controller_recovery_timeout_sec", 15.0)
        self._controller_recovery_timeout_sec = max(
            0.1,
            float(self.get_parameter("controller_recovery_timeout_sec").value),
        )
        self._lock = threading.RLock()
        self._callback_group = ReentrantCallbackGroup()
        self._route_state = None
        self._route_key: tuple[int, int] | None = None
        self._latest_simulation_state: SimulationState | None = None
        self._service_clients: dict[str, Any] = {}
        self._action_clients: dict[str, ActionClient] = {}
        self._dispatch_epoch = 0
        self._last_lifecycle_control_signature: tuple[str, str] | None = None
        self._active_service_ids: set[str] = set()
        self._active_service_activity: dict[str, _ProxyRequestActivity] = {}
        self._pending_retraction_receipts: dict[str, _PendingRetractionReceipt] = {}
        self._active_actions: dict[str, _ProxyAction] = {}
        self._controller_recovery_timer = self.create_timer(
            min(0.5, max(0.1, self._controller_recovery_timeout_sec / 4.0)),
            self._expire_controller_recovery,
            callback_group=self._callback_group,
        )

        route_qos = QoSProfile(
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=1,
            reliability=QoSReliabilityPolicy.RELIABLE,
            durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
        )
        self.create_subscription(
            String,
            str(self.get_parameter("route_state_topic").value),
            self._on_route_state,
            route_qos,
            callback_group=self._callback_group,
        )
        self.create_subscription(
            SimulationState,
            "/simulation/state",
            self._on_simulation_state,
            20,
            callback_group=self._callback_group,
        )
        # The proxy owns direct typed-request lifetimes.  It observes only
        # lifecycle edges so a request left behind by a stopped run cannot
        # keep the next run's local lane or a virtual-route switch hostage.
        self.create_subscription(
            String,
            "/simulation/control_state",
            self._on_runtime_control,
            20,
            callback_group=self._callback_group,
        )
        self._activity_pub = self.create_publisher(
            String,
            EXECUTION_PROXY_ACTIVITY_TOPIC,
            route_qos,
        )
        self._lifecycle_pub = self.create_publisher(
            String,
            EXECUTION_PROXY_LIFECYCLE_TOPIC,
            QoSProfile(
                history=QoSHistoryPolicy.KEEP_LAST,
                depth=50,
                reliability=QoSReliabilityPolicy.RELIABLE,
                durability=QoSDurabilityPolicy.VOLATILE,
            ),
        )
        self._retraction_service = self.create_service(
            ExecuteRetractionCommand,
            str(self.get_parameter("retraction_proxy_service").value),
            self._on_retraction_request,
            callback_group=self._callback_group,
        )
        self._tool_handover_server = ActionServer(
            self,
            ExecuteToolHandover,
            str(self.get_parameter("tool_handover_proxy_action").value),
            execute_callback=self._execute_tool_handover,
            goal_callback=self._tool_handover_goal,
            cancel_callback=self._cancel_tool_handover,
            callback_group=self._callback_group,
        )
        self._publish_activity()
        self.get_logger().info(
            "execution command proxy ready: "
            f"{self.get_parameter('retraction_proxy_service').value}, "
            f"{self.get_parameter('tool_handover_proxy_action').value}"
        )

    def _on_route_state(self, message: String) -> None:
        try:
            # The execution bridge is the sole owner of the scenario-local
            # retraction workflow policy.  This narrow transport proxy must
            # retain a bridge-selected external Service route even when that
            # local ordering is intentionally suppressed; endpoint/source
            # family validation still happens in the shared parser.
            state = parse_execution_route_state(
                json.loads(message.data),
                allow_external_retraction_state_machine_suppression=True,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            self.get_logger().warning(
                f"execution proxy ignored invalid route state: {exc}",
                throttle_duration_sec=2.0,
            )
            return
        key = (state.revision, state.initialization_revision)
        with self._lock:
            if self._route_key is not None and key < self._route_key:
                return
            self._route_state = state
            self._route_key = key

    def _on_runtime_control(self, message: String) -> None:
        """Detach a stopped run from UI ownership without forgetting the controller.

        A Service cannot be cancelled after dispatch and an Action cancellation
        is only a request.  Stop/reset therefore advances the local epoch so a
        new run does not inherit the old visible lane, while preserving the
        old controller records until a terminal result, recovery timeout, or
        later explicit operator resolution.  This is intentionally different
        from declaring the old request cancelled.
        """

        control, _, detail = str(getattr(message, "data", "")).partition(":")
        control = control.strip().casefold()
        signature = (control, detail.strip())
        if control not in {
            "start",
            "start_runtime",
            "start_actors",
            "pause",
            "resume",
            "stop",
            "reset",
        }:
            return
        changed = False
        actions_to_cancel: tuple[_ProxyAction, ...] = ()
        with self._lock:
            if signature == getattr(
                self, "_last_lifecycle_control_signature", None
            ):
                return
            self._last_lifecycle_control_signature = signature
            if control in {"pause", "stop", "reset"}:
                self._dispatch_epoch = int(
                    getattr(self, "_dispatch_epoch", 0)
                ) + 1
                changed = True
            if control in {"stop", "reset"}:
                deadline = time.monotonic() + float(
                    getattr(self, "_controller_recovery_timeout_sec", 15.0)
                )
                actions_to_cancel = tuple(self._active_actions.values())
                for active in self._active_actions.values():
                    active.cancel_requested = True
                    active.recovery_deadline_monotonic = deadline
                for pending in self._pending_retraction_receipts.values():
                    # Service transport has no cancel operation.  Its record
                    # remains in the activity projection until the controller
                    # responds or recovery times out.
                    pending.recovery_deadline_monotonic = deadline
        if changed:
            for active in actions_to_cancel:
                downstream = getattr(active, "downstream_goal_handle", None)
                if downstream is None:
                    continue
                try:
                    downstream.cancel_goal_async()
                except Exception:  # pragma: no cover - transport failure
                    pass
            self._publish_activity()

    def _on_simulation_state(self, message: SimulationState) -> None:
        """Cache only the authoritative lifecycle state needed for admission."""

        with self._lock:
            self._latest_simulation_state = message

    def _ready_route(self):
        with self._lock:
            state = self._route_state
        if state is None or state.initialization_state == "initializing":
            return None
        return state

    def _service_client(self, endpoint: str):
        with self._lock:
            client = self._service_clients.get(endpoint)
            if client is None:
                client = self.create_client(
                    ExecuteRetractionCommand,
                    endpoint,
                    callback_group=self._callback_group,
                )
                self._service_clients[endpoint] = client
            return client

    def _action_client(self, endpoint: str) -> ActionClient:
        with self._lock:
            client = self._action_clients.get(endpoint)
            if client is None:
                client = ActionClient(
                    self,
                    ExecuteToolHandover,
                    endpoint,
                    callback_group=self._callback_group,
                )
                self._action_clients[endpoint] = client
            return client

    @staticmethod
    def _rejection(response, *, command_id: str, reason: str, result_code: int):
        response.request_accepted = False
        response.result_code = int(result_code)
        response.command_id = str(command_id or "")
        response.message = str(reason)[:256]
        return response

    def _publish_retraction_lifecycle(
        self,
        *,
        request: object,
        route: object,
        stage: str,
        dispatch_submitted: bool,
        terminal: bool,
        evidence: str,
        reason_code: str,
        procedure_run_id: str = "",
    ) -> None:
        """Send a bounded private lifecycle fact to the public-trace owner.

        This is intentionally not an ``ExecutionTrace`` publisher.  The bridge
        serializes these proxy-owned direct-command facts with its legacy-BT
        traces, keeping one public observer owner and sequence.
        """

        publisher = getattr(self, "_lifecycle_pub", None)
        if publisher is None:
            return
        try:
            command_id = str(getattr(request, "command_id", "") or "").strip()
            command = int(getattr(request, "command"))
            target_side = int(getattr(request, "target_side"))
            distance_m = float(getattr(request, "distance_m"))
            if (
                not command_id
                or command < 0
                or command > 255
                or target_side < 0
                or target_side > 255
                or not math.isfinite(distance_m)
            ):
                return
            endpoint = str(
                getattr(route, "retraction_service_name", "") or ""
            ).strip()
            endpoint_source = retraction_route_source(route)
            if not procedure_run_id:
                with self._lock:
                    activity = getattr(self, "_active_service_activity", {}).get(
                        command_id
                    )
                procedure_run_id = str(
                    getattr(activity, "procedure_run_id", "") or ""
                ).strip()
            if not endpoint or endpoint_source not in {"external", "virtual"}:
                return
            publisher.publish(
                String(
                    data=json.dumps(
                        {
                            "schema": EXECUTION_PROXY_LIFECYCLE_SCHEMA,
                            "command_id": command_id,
                            "route": "retraction",
                            "transport": "service",
                            "endpoint": endpoint,
                            "endpoint_source": endpoint_source,
                            "stage": str(stage),
                            "dispatch_submitted": bool(dispatch_submitted),
                            "terminal": bool(terminal),
                            "evidence": str(evidence),
                            "reason_code": str(reason_code),
                            # This is source-run provenance captured at the
                            # proxy admission edge.  The bridge verifies it
                            # before publishing the public trace, so an old
                            # direct voice receipt cannot be relabelled as a
                            # newer procedure run.
                            "procedure_run_id": str(procedure_run_id)[:64],
                            "retraction_command": command,
                            "retraction_target_side": target_side,
                            "retraction_distance_m": distance_m,
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                )
            )
        except Exception:  # pragma: no cover - observer publication only
            logger = getattr(self, "get_logger", None)
            if callable(logger):
                try:
                    logger().warning("execution proxy lifecycle publish failed")
                except Exception:
                    pass

    @staticmethod
    def _downstream_retraction_receipt(downstream: object) -> tuple[bool, int]:
        """Read a Service receipt without letting malformed fields leak out."""

        accepted = bool(getattr(downstream, "request_accepted", False))
        try:
            result_code = int(
                getattr(
                    downstream,
                    "result_code",
                    ExecuteRetractionCommand.Response.RESULT_ERROR,
                )
            )
        except (TypeError, ValueError):
            result_code = ExecuteRetractionCommand.Response.RESULT_ERROR
        return accepted, result_code

    def _publish_retraction_receipt_lifecycle(
        self,
        *,
        request: object,
        route: object,
        downstream: object,
    ) -> None:
        """Emit the terminal trace facts for one resolved Service future."""

        accepted, result_code = self._downstream_retraction_receipt(downstream)
        if accepted:
            virtual_transaction = is_isolated_virtual_retraction_route(route)
            self._publish_retraction_lifecycle(
                request=request,
                route=route,
                stage="accepted",
                dispatch_submitted=True,
                # External Service responses remain terminal admission
                # receipts.  The virtual endpoint gets one more event for
                # its own handled-request completion below.
                terminal=not virtual_transaction,
                evidence="service_admission_only",
                reason_code="request_accepted",
            )
            if virtual_transaction:
                # This only means the isolated virtual Service finished
                # handling the request.  It is deliberately not a
                # controller or bed-arm physical completion assertion.
                self._publish_retraction_lifecycle(
                    request=request,
                    route=route,
                    stage="completed",
                    dispatch_submitted=True,
                    terminal=True,
                    evidence="virtual_service_transaction_completed",
                    reason_code="virtual_service_completed",
                )
            return
        self._publish_retraction_lifecycle(
            request=request,
            route=route,
            stage="rejected",
            dispatch_submitted=True,
            terminal=True,
            evidence="service_admission_only",
            reason_code=f"service_rejected_{result_code}",
        )

    def _service_request_is_current(
        self,
        command_id: str,
        *,
        expected_epoch: int,
    ) -> bool:
        """Whether this exact proxy Service reservation still owns the ID.

        Stop/reset advances the dispatch epoch but deliberately retains this
        record until the controller responds or its bounded recovery window
        expires.  The epoch check keeps an old callback from releasing a
        same-ID reservation made by a future scenario.
        """

        with self._lock:
            activities = getattr(self, "_active_service_activity", None)
            activity = (
                activities.get(command_id)
                if isinstance(activities, dict)
                else None
            )
            return bool(
                activity is not None
                and int(activity.dispatch_epoch) == int(expected_epoch)
            )

    def _release_retraction_request(
        self,
        command_id: str,
        *,
        expected_epoch: int | None = None,
    ) -> bool:
        """Release one in-flight request exactly once and refresh activity."""

        with self._lock:
            activities = getattr(self, "_active_service_activity", None)
            activity = (
                activities.get(command_id)
                if isinstance(activities, dict)
                else None
            )
            if expected_epoch is not None and (
                activity is None
                or int(activity.dispatch_epoch) != int(expected_epoch)
            ):
                return False
            pending = getattr(self, "_pending_retraction_receipts", None)
            if pending is None:
                pending = {}
                self._pending_retraction_receipts = pending
            pending.pop(command_id, None)
            was_active = command_id in self._active_service_ids
            self._active_service_ids.discard(command_id)
            if isinstance(activities, dict):
                activities.pop(command_id, None)
        if was_active:
            self._publish_activity()
        return was_active

    def _on_late_retraction_receipt(
        self,
        future: Any,
        *,
        command_id: str,
        request: object,
        route: object,
        expected_epoch: int,
    ) -> None:
        """Resolve one asynchronous Service receipt exactly once."""

        with self._lock:
            pending = getattr(self, "_pending_retraction_receipts", None)
            receipt = (
                pending.get(command_id) if isinstance(pending, dict) else None
            )
            if (
                receipt is None
                or receipt.future is not future
                or int(receipt.dispatch_epoch) != int(expected_epoch)
                or not self._service_request_is_current(
                    command_id,
                    expected_epoch=expected_epoch,
                )
            ):
                return
            # Claim the callback before emitting observer facts.  A Future
            # implementation is allowed to invoke a registered callback more
            # than once; only this claimant may publish its terminal result.
            pending.pop(command_id, None)
        try:
            downstream = future.result()
        except Exception as exc:  # pragma: no cover - ROS transport failure
            self._publish_retraction_lifecycle(
                request=request,
                route=route,
                stage="failed",
                dispatch_submitted=True,
                terminal=True,
                evidence="response_unavailable",
                reason_code=(
                    "selected_retraction_service_late_response_error_"
                    f"{type(exc).__name__}"
                ),
            )
        else:
            response_command_id = str(
                getattr(downstream, "command_id", "") or ""
            ).strip()
            if response_command_id != command_id:
                self._publish_retraction_lifecycle(
                    request=request,
                    route=route,
                    stage="failed",
                    dispatch_submitted=True,
                    terminal=True,
                    evidence="response_invalid",
                    reason_code="selected_retraction_service_command_id_mismatch",
                )
            else:
                self._publish_retraction_receipt_lifecycle(
                    request=request,
                    route=route,
                    downstream=downstream,
                )
        finally:
            self._release_retraction_request(
                command_id,
                expected_epoch=expected_epoch,
            )

    def _track_retraction_future(
        self,
        *,
        command_id: str,
        future: Any,
        request: object,
        route: object,
        expected_epoch: int,
    ) -> bool:
        """Track one submitted Future without waiting in a ROS callback."""

        with self._lock:
            pending = getattr(self, "_pending_retraction_receipts", None)
            if pending is None:
                pending = {}
                self._pending_retraction_receipts = pending
            if (
                command_id not in self._active_service_ids
                or command_id in pending
                or not self._service_request_is_current(
                    command_id,
                    expected_epoch=expected_epoch,
                )
            ):
                return False
            activity = getattr(self, "_active_service_activity", {}).get(command_id)
            procedure_run_id = str(
                getattr(activity, "procedure_run_id", "") or ""
            ).strip()
            pending[command_id] = _PendingRetractionReceipt(
                command_id=command_id,
                future=future,
                dispatch_epoch=expected_epoch,
                request=request,
                route=route,
                procedure_run_id=procedure_run_id,
                recovery_deadline_monotonic=(
                    time.monotonic()
                    + float(
                        getattr(self, "_controller_recovery_timeout_sec", 15.0)
                    )
                ),
            )
        try:
            future.add_done_callback(
                lambda completed, command_id=command_id, request=request, route=route, expected_epoch=expected_epoch: self._on_late_retraction_receipt(
                    completed,
                    command_id=command_id,
                    request=request,
                    route=route,
                    expected_epoch=expected_epoch,
                )
            )
        except Exception:  # pragma: no cover - incompatible transport future
            # The controller call has already been submitted.  Do not erase
            # its recovery record merely because an unusual client Future
            # cannot register a callback; the recovery timer will bound it.
            # Returning ``False`` lets observers see that the receipt cannot
            # be followed, while the pending entry keeps route changes and
            # same-ID reuse conservatively blocked.
            return False
        # Test doubles and some immediate transport adapters resolve before a
        # callback can be registered.  Claiming through the same callback path
        # keeps the lifecycle exactly-once in either ordering.
        try:
            if future.done():
                self._on_late_retraction_receipt(
                    future,
                    command_id=command_id,
                    request=request,
                    route=route,
                    expected_epoch=expected_epoch,
                )
        except Exception:
            pass
        return True

    def _on_retraction_request(self, request, response):
        """Forward one public request over the route selected before send."""

        reason = validate_retraction_proxy_request(request)
        command_id = str(getattr(request, "command_id", "") or "").strip()
        if reason:
            return self._rejection(
                response,
                command_id=command_id,
                reason=reason,
                result_code=ExecuteRetractionCommand.Response.RESULT_INVALID_PARAMETER,
            )
        with self._lock:
            simulation_state = self._latest_simulation_state
        if not retraction_request_allowed_by_scenario(request, simulation_state):
            return self._rejection(
                response,
                command_id=command_id,
                reason="scenario_not_running",
                result_code=ExecuteRetractionCommand.Response.RESULT_REJECTED,
            )
        route = self._ready_route()
        if route is None:
            return self._rejection(
                response,
                command_id=command_id,
                reason="execution_route_unavailable",
                result_code=ExecuteRetractionCommand.Response.RESULT_REJECTED,
            )
        with self._lock:
            if command_id in self._active_service_ids:
                return self._rejection(
                    response,
                    command_id=command_id,
                    reason="duplicate_command_inflight",
                    result_code=ExecuteRetractionCommand.Response.RESULT_REJECTED,
                )
            self._active_service_ids.add(command_id)
            activities = getattr(self, "_active_service_activity", None)
            if activities is None:
                activities = {}
                self._active_service_activity = activities
            procedure_run_id = str(
                getattr(simulation_state, "procedure_run_id", "") or ""
            ).strip()
            # The proxy may accept a legacy controller Service while the
            # bridge is not yet publishing run-scoped observability.  Never
            # relabel such a receipt with an arbitrary non-empty string: only
            # the reviewed run token is forwarded to the public trace owner.
            if not valid_procedure_run_id(procedure_run_id):
                procedure_run_id = ""
            activities[command_id] = _ProxyRequestActivity(
                endpoint_source=retraction_route_source(route),
                dispatch_epoch=int(getattr(self, "_dispatch_epoch", 0)),
                procedure_run_id=procedure_run_id,
            )
            request_epoch = int(activities[command_id].dispatch_epoch)
        self._publish_activity()
        try:
            with self._lock:
                activity = getattr(self, "_active_service_activity", {}).get(
                    command_id
                )
                request_is_current = bool(
                    activity is not None
                    and int(activity.dispatch_epoch) == request_epoch
                )
            if not request_is_current:
                return self._rejection(
                    response,
                    command_id=command_id,
                    reason="runtime_not_accepting_commands",
                    result_code=ExecuteRetractionCommand.Response.RESULT_REJECTED,
                )
            client = self._service_client(route.retraction_service_name)
            if not client.service_is_ready():
                self._publish_retraction_lifecycle(
                    request=request,
                    route=route,
                    stage="rejected",
                    dispatch_submitted=False,
                    terminal=True,
                    evidence="not_dispatched",
                    reason_code="selected_retraction_service_unavailable",
                )
                return self._rejection(
                    response,
                    command_id=command_id,
                    reason="selected_retraction_service_unavailable",
                    result_code=ExecuteRetractionCommand.Response.RESULT_REJECTED,
                )
            future = client.call_async(request)
            if not self._service_request_is_current(
                command_id,
                expected_epoch=request_epoch,
            ):
                return self._rejection(
                    response,
                    command_id=command_id,
                    reason="scenario_stop_local_tracking_discarded",
                    result_code=ExecuteRetractionCommand.Response.RESULT_REJECTED,
                )
            self._publish_retraction_lifecycle(
                request=request,
                route=route,
                stage="sent",
                dispatch_submitted=True,
                terminal=False,
                evidence="submission_only",
                reason_code="service_call_submitted",
            )
            if not self._track_retraction_future(
                command_id=command_id,
                future=future,
                request=request,
                route=route,
                expected_epoch=request_epoch,
            ):
                self._publish_retraction_lifecycle(
                    request=request,
                    route=route,
                    stage="unknown",
                    dispatch_submitted=True,
                    terminal=False,
                    evidence="response_unavailable",
                    reason_code="selected_retraction_service_tracking_unavailable",
                )
            # Service callbacks must return promptly.  This is a local
            # transport-submission acknowledgement, not a claim that the
            # selected controller accepted or completed the retraction.  The
            # later private lifecycle receipt is the sole source of the public
            # accepted/rejected execution trace and TTS event.
            response.request_accepted = True
            response.result_code = ExecuteRetractionCommand.Response.RESULT_ACCEPTED
            response.command_id = command_id
            response.message = "service_call_submitted"
            return response
        except Exception as exc:  # ROS transport errors are endpoint evidence.
            self._publish_retraction_lifecycle(
                request=request,
                route=route,
                stage="failed",
                dispatch_submitted=False,
                terminal=True,
                evidence="not_dispatched",
                reason_code=f"selected_retraction_service_error_{type(exc).__name__}",
            )
            return self._rejection(
                response,
                command_id=command_id,
                reason=f"selected_retraction_service_error:{type(exc).__name__}",
                result_code=ExecuteRetractionCommand.Response.RESULT_ERROR,
            )
        finally:
            # A successful ``call_async`` remains tracked by its done callback.
            # Only a pre-dispatch failure reaches this finally clause without a
            # pending record.
            with self._lock:
                pending = getattr(self, "_pending_retraction_receipts", {})
                is_tracked = command_id in pending
            if not is_tracked:
                self._release_retraction_request(
                    command_id,
                    expected_epoch=request_epoch,
                )

    def _expire_controller_recovery(self) -> None:
        """Bound old controller records without treating timeout as success."""

        now = time.monotonic()
        expired_services: list[_PendingRetractionReceipt] = []
        expired_actions: list[str] = []
        with self._lock:
            pending = getattr(self, "_pending_retraction_receipts", {})
            for command_id, receipt in tuple(pending.items()):
                if now < float(receipt.recovery_deadline_monotonic):
                    continue
                pending.pop(command_id, None)
                self._active_service_ids.discard(command_id)
                getattr(self, "_active_service_activity", {}).pop(command_id, None)
                expired_services.append(receipt)
            for command_id, active in tuple(self._active_actions.items()):
                deadline = active.recovery_deadline_monotonic
                if deadline is None or now < float(deadline):
                    continue
                self._active_actions.pop(command_id, None)
                expired_actions.append(command_id)
        for receipt in expired_services:
            self._publish_retraction_lifecycle(
                request=receipt.request,
                route=receipt.route,
                stage="unknown",
                dispatch_submitted=True,
                terminal=True,
                evidence="response_unavailable",
                reason_code="controller_recovery_timeout",
                procedure_run_id=receipt.procedure_run_id,
            )
        if expired_services or expired_actions:
            logger = getattr(self, "get_logger", None)
            if callable(logger):
                try:
                    logger().warning(
                        "execution proxy recovery timeout: "
                        f"services={len(expired_services)} actions={len(expired_actions)}"
                    )
                except Exception:
                    pass
            self._publish_activity()

    def _tool_handover_goal(self, goal) -> GoalResponse:
        reason = validate_tool_handover_proxy_goal(goal)
        command_id = str(getattr(goal, "command_id", "") or "").strip()
        route = self._ready_route()
        if reason or route is None:
            return GoalResponse.REJECT
        endpoint = str(getattr(route, "tool_handover_endpoint", "") or "").strip()
        if not endpoint:
            return GoalResponse.REJECT
        with self._lock:
            # Goal callbacks run in a re-entrant callback group. A stopped
            # run may leave the UI lane, but a Goal that crossed to the
            # controller remains an Action-lane blocker until a terminal
            # result/cancel/recovery timeout resolves it.
            current_epoch = int(getattr(self, "_dispatch_epoch", 0))
            if command_id in self._active_actions or any(
                int(getattr(active, "dispatch_epoch", -1)) == current_epoch
                or bool(
                    getattr(active, "dispatched", False)
                    or getattr(active, "downstream_goal_handle", None) is not None
                )
                for active in self._active_actions.values()
            ):
                return GoalResponse.REJECT
            self._active_actions[command_id] = _ProxyAction(
                command_id=command_id,
                endpoint=endpoint,
                endpoint_source=tool_handover_route_source(route),
                dispatch_epoch=current_epoch,
            )
        self._publish_activity()
        return GoalResponse.ACCEPT

    def _cancel_tool_handover(self, goal_handle) -> CancelResponse:
        command_id = str(getattr(goal_handle.request, "command_id", "") or "").strip()
        with self._lock:
            active = self._active_actions.get(command_id)
            if active is None:
                return CancelResponse.REJECT
            active.cancel_requested = True
            downstream = active.downstream_goal_handle
        if downstream is not None:
            try:
                downstream.cancel_goal_async()
            except Exception:  # pragma: no cover - remote transport failure
                pass
        return CancelResponse.ACCEPT

    async def _execute_tool_handover(self, goal_handle):
        request = goal_handle.request
        command_id = str(getattr(request, "command_id", "") or "").strip()
        result = ExecuteToolHandover.Result()
        release_lane = True
        reserved_action: _ProxyAction | None = None
        try:
            with self._lock:
                active = self._active_actions.get(command_id)
                if active is None:
                    return self._finish_proxy_action(
                        goal_handle,
                        result,
                        state="abort",
                        reason="proxy_goal_not_reserved",
                    )
                if int(active.dispatch_epoch) != int(
                    getattr(self, "_dispatch_epoch", 0)
                ):
                    return self._finish_proxy_action(
                        goal_handle,
                        result,
                        state="abort",
                        reason="runtime_not_accepting_commands",
                    )
                endpoint = active.endpoint
                reserved_action = active
                active.upstream_goal_handle = goal_handle
                cancel_requested = active.cancel_requested
            if not endpoint:
                return self._finish_proxy_action(
                    goal_handle,
                    result,
                    state="abort",
                    reason="selected_tool_handover_action_unavailable",
                )
            client = self._action_client(endpoint)
            if not client.server_is_ready():
                return self._finish_proxy_action(
                    goal_handle,
                    result,
                    state="abort",
                    reason="selected_tool_handover_action_unavailable",
                )
            with self._lock:
                active = self._active_actions.get(command_id)
                if active is not reserved_action or active.cancel_requested:
                    return self._finish_proxy_action(
                        goal_handle,
                        result,
                        state="cancel",
                        reason="runtime_not_accepting_commands",
                    )
                # From this edge the external controller may own a Goal even
                # if the asynchronous GoalHandle has not arrived yet.
                active.dispatched = True
            downstream_future = client.send_goal_async(
                request,
                feedback_callback=lambda feedback: self._relay_feedback(
                    command_id, feedback
                ),
            )
            # Once a Goal future exists, a transport exception can no longer
            # prove that the controller did not accept or continue the Goal.
            # Hold the lane until a rejection or terminal result is observed.
            release_lane = False
            downstream = await downstream_future
            with self._lock:
                active = self._active_actions.get(command_id)
                if active is not None:
                    active.downstream_goal_handle = downstream
                    cancel_requested = active.cancel_requested
                    stale_epoch = int(active.dispatch_epoch) != int(
                        getattr(self, "_dispatch_epoch", 0)
                    )
                else:
                    stale_epoch = True
            if not bool(getattr(downstream, "accepted", False)):
                release_lane = True
                return self._finish_proxy_action(
                    goal_handle,
                    result,
                    state="abort",
                    reason=(
                        "scenario_stop_local_tracking_discarded"
                        if stale_epoch
                        else "selected_tool_handover_goal_rejected"
                    ),
                )
            if cancel_requested or stale_epoch:
                try:
                    downstream.cancel_goal_async()
                except Exception:  # pragma: no cover - ROS transport failure
                    pass
            wrapped = await downstream.get_result_async()
            # A ROS Action result is terminal even when its application payload
            # is malformed.  It is now safe for a later Goal to use the lane.
            release_lane = True
            if stale_epoch:
                return self._finish_proxy_action(
                    goal_handle,
                    result,
                    state="cancel",
                    reason="scenario_stop_local_tracking_discarded",
                )
            downstream_result = getattr(wrapped, "result", None)
            if downstream_result is None:
                return self._finish_proxy_action(
                    goal_handle,
                    result,
                    state="abort",
                    reason="selected_tool_handover_result_unavailable",
                )
            return self._finish_proxy_action(
                goal_handle,
                downstream_result,
                state=(
                    "succeed"
                    if bool(getattr(downstream_result, "success", False))
                    else (
                        "cancel"
                        if str(getattr(downstream_result, "final_state", ""))
                        == ExecuteToolHandover.Result.FINAL_CANCELED
                        else "abort"
                    )
                ),
            )
        except Exception as exc:  # pragma: no cover - ROS transport failure
            return self._finish_proxy_action(
                goal_handle,
                result,
                state="abort",
                reason=f"selected_tool_handover_error:{type(exc).__name__}",
            )
        finally:
            if release_lane and reserved_action is not None:
                with self._lock:
                    current = self._active_actions.get(command_id)
                    removed = (
                        self._active_actions.pop(command_id, None)
                        if current is reserved_action
                        else None
                    )
                if removed is not None:
                    self._publish_activity()

    def _relay_feedback(self, command_id: str, feedback_message: Any) -> None:
        with self._lock:
            active = self._active_actions.get(command_id)
            goal_handle = active.upstream_goal_handle if active is not None else None
        if goal_handle is None:
            return
        feedback = getattr(feedback_message, "feedback", feedback_message)
        try:
            goal_handle.publish_feedback(feedback)
        except Exception:  # pragma: no cover - client disconnected
            pass

    @staticmethod
    def _finish_proxy_action(goal_handle, result, *, state: str, reason: str = ""):
        if reason:
            result.success = False
            result.final_state = (
                ExecuteToolHandover.Result.FINAL_CANCELED
                if state == "cancel"
                else ExecuteToolHandover.Result.FINAL_FAILED
            )
            result.reason_code = str(reason)[:128]
            result.failure_detail = ""
        if state == "succeed":
            goal_handle.succeed()
        elif state == "cancel":
            goal_handle.canceled()
        else:
            goal_handle.abort()
        return result

    def _publish_activity(self) -> None:
        publisher = getattr(self, "_activity_pub", None)
        if publisher is None:
            return
        with self._lock:
            service_count = len(self._active_service_ids)
            action_count = len(self._active_actions)
        publisher.publish(
            String(
                data=json.dumps(
                    {
                        "schema": EXECUTION_PROXY_ACTIVITY_SCHEMA,
                        "service_count": service_count,
                        "action_count": action_count,
                        "active": bool(service_count or action_count),
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
        )


def main() -> None:
    rclpy.init()
    node = ExecutionCommandProxy()
    executor = MultiThreadedExecutor(num_threads=2)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        executor.shutdown()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


__all__ = [
    "EXECUTION_PROXY_ACTIVITY_SCHEMA",
    "EXECUTION_PROXY_ACTIVITY_TOPIC",
    "EXECUTION_PROXY_LIFECYCLE_SCHEMA",
    "EXECUTION_PROXY_LIFECYCLE_TOPIC",
    "ExecutionCommandProxy",
    "is_isolated_virtual_retraction_route",
    "retraction_route_source",
    "validate_retraction_proxy_request",
    "validate_tool_handover_proxy_goal",
]
