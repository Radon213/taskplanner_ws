"""Simulation manager service surface for bundle and runtime control."""

from __future__ import annotations

import json
from pathlib import Path
import socket
import threading
import time

from ament_index_python.packages import get_package_share_directory
from btops_interfaces.msg import ExecutionSnapshot
from btops_interfaces.srv import CommandExecutor, GetRuntimeState, StartBehavior
from procedure_spec import parse_scenario_config
import rclpy
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import (
    DurabilityPolicy,
    HistoryPolicy,
    QoSProfile,
    ReliabilityPolicy,
)
from std_msgs.msg import String
from surgical_msgs.msg import SimulationState, SurgeonRequest
from surgical_msgs.srv import (
    ControlSimulation,
    InjectSurgeonOverride,
)

from .scenario_store import (
    load_scenario_snapshot,
)


RESOURCE_ID = "tree/taskplanner_bt_trees::surgical_assist_v1::TaskplannerAssistDemo"
ENTRY_POINT = "TaskplannerAssistDemo"
NODE_MANIFESTS = ["taskplanner_bt_nodes::taskplanner_bt_nodes"]
RETRYABLE_START_ERROR_MARKERS = (
    "/tree_executor/set_parameters",
    "address already in use",
    "previous one is still busy",
    "currently executing",
    "parameter is not allowed to change while tree executor is running",
    "node_manifest_identities is empty",
)
ALLOWED_OVERRIDE_EVENTS = {"request_tool", "voice_request", "return_tool", "cancel_request"}
TOOL_REQUIRED_OVERRIDE_EVENTS = {"request_tool", "voice_request", "return_tool"}
TRANSITION_READY_EXECUTOR_STATES = {"idle", "halted", "terminated"}
NO_RUNTIME_SNAPSHOT_PREFIX = "No runtime snapshot is available for"
EXECUTOR_SNAPSHOT_MAX_AGE_SEC = 3.0
# The ordinary start path only needs a bounded confirmation that an old BT
# session is not still active.  This is a local owner service; using the
# generic one-second transition probe here made the start button visibly wait
# before it could dispatch a new tree.  A slow/unavailable service still goes
# through the existing explicit terminate-and-settle recovery path below.
PRESTART_EXECUTOR_SERVICE_TIMEOUT_SEC = 0.15
PRESTART_EXECUTOR_RESPONSE_TIMEOUT_SEC = 0.35
EXECUTOR_RUNNING_STATES = frozenset(
    {"running", "active", "executing", "succeeded"}
)
EXECUTOR_START_FAILURE_STATES = frozenset(
    {
        "failed",
        "rejected",
        "canceled",
        "start_failed",
        "idle",
        "halted",
        "terminated",
    }
)
TERMINAL_RECEIPT_SCHEMA = "taskplanner.simulation.lifecycle_terminal.v1"
TERMINAL_RECEIPT_TOPIC = "/simulation/lifecycle_terminal"
PROCEDURE_LIFECYCLE_EVENT_SCHEMA = "taskplanner.simulation.lifecycle_event.v1"
PROCEDURE_LIFECYCLE_EVENT_TOPIC = "/simulation/lifecycle_event"


class SimulationManagerNode(Node):
    def __init__(self) -> None:
        super().__init__("simulation_manager")
        default_root = Path(get_package_share_directory("procedure_spec")) / "specs"
        self.declare_parameter("spec_root", str(default_root))
        self.declare_parameter(
            "scenario_config_topic",
            "/simulation/scenario_config",
        )
        self.declare_parameter("default_bundle", "thyroidectomy")
        self.declare_parameter("executor_name", "tree_executor")
        self.declare_parameter("tick_rate_hz", 0.1)
        self.declare_parameter("groot2_port", 0)
        self.declare_parameter("surgeon_actor_mode", "llm")
        self.declare_parameter("manual_override_actor_mute_sec", 8.0)
        self.declare_parameter("execution_backend", "mock")
        # Groot2 is an optional debug observer, not part of procedure execution.
        # Keeping it disabled on the operational path prevents an ephemeral-port
        # collision from turning a valid scenario start into a 10 second timeout.
        # Debug launchers may opt in explicitly when a Groot session is needed.
        self.declare_parameter("enable_bt_monitoring", False)
        # The execution bridge owns the single public route coordinator.
        # Mode transitions do not reserve this scenario manager or wait for a
        # Digital Twin receipt: its normal control surface remains independent.
        self.declare_parameter("enable_runtime_route_control", False)

        self._spec_root = Path(str(self.get_parameter("spec_root").value))
        self._active_bundle = str(self.get_parameter("default_bundle").value)
        initial_scenario = load_scenario_snapshot(
            self._spec_root,
            self._active_bundle,
        )
        self._active_spec_dir = initial_scenario.spec_dir
        self._active_spec = initial_scenario.spec
        self._active_config_revision = initial_scenario.revision
        # ScenarioStore owns the selected bundle.  The state core keeps only
        # its observed revision so its lifecycle/BT policy uses the same parsed
        # spec without becoming another config writer.
        self._scenario_config_revision = self._active_config_revision
        self._pending_scenario_config = None
        self._scenario_config_lock = threading.RLock()
        self._executor_name = str(self.get_parameter("executor_name").value)
        self._tick_rate_hz = float(self.get_parameter("tick_rate_hz").value)
        self._groot2_port = int(self.get_parameter("groot2_port").value)
        self._surgeon_actor_mode = str(
            self.get_parameter("surgeon_actor_mode").value
        ).strip().lower()
        self._manual_override_actor_mute_sec = float(
            self.get_parameter("manual_override_actor_mute_sec").value
        )
        self._execution_backend = str(
            self.get_parameter("execution_backend").value
        ).strip().lower()
        self._enable_bt_monitoring = bool(
            self.get_parameter("enable_bt_monitoring").value
        )
        self._enable_runtime_route_control = bool(
            self.get_parameter("enable_runtime_route_control").value
        )
        self._running = False
        self._execution_state = "idle"
        self._operation_name = ""
        self._operation_cancel = threading.Event()
        self._operation_lock = threading.Lock()
        self._operation_rejection_message = ""
        self._override_in_progress = False
        self._completion_terminate_started = False
        # Terminal receipt publication and the follow-on authored-layout reset
        # are one lifecycle transaction.  Keep a local mutex so an old
        # completion heartbeat cannot race a concurrent terminal path into a
        # second reset.
        self._terminal_auto_reset_lock = threading.Lock()
        self._terminal_receipt_lock = threading.Lock()
        self._last_terminal_receipt_run_id = ""
        self._active_procedure_run_id = ""
        self._active_procedure_bundle = ""
        # A fresh manager has never dispatched this executor.  Once a start is
        # attempted, only an explicit terminal BTops snapshot can restore this
        # bit.  This distinguishes a legitimate initial no-session state from
        # a lost/unknown runtime snapshot after execution began.
        self._executor_settled_confirmed = True
        self._latest_state: SimulationState | None = None
        self._latest_state_generation = 0
        self._latest_state_received_monotonic = 0.0
        self._latest_state_lock = threading.Lock()
        self._latest_executor_state = ""
        self._latest_executor_state_generation = 0
        self._latest_executor_state_received_monotonic = 0.0
        self._latest_executor_state_lock = threading.Lock()
        self._callback_group = ReentrantCallbackGroup()

        self._control_pub = self.create_publisher(String, "/simulation/control_state", 10)
        terminal_receipt_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        self._terminal_receipt_pub = self.create_publisher(
            String,
            TERMINAL_RECEIPT_TOPIC,
            terminal_receipt_qos,
        )
        # This edge is distinct from the terminal receipt below: it announces
        # that an admitted finish request has entered the manager's
        # ``finishing`` state, while cleanup and executor settlement are still
        # in progress.  TTS observes it for the immediate operator cue.
        self._lifecycle_event_pub = self.create_publisher(
            String,
            PROCEDURE_LIFECYCLE_EVENT_TOPIC,
            QoSProfile(
                history=HistoryPolicy.KEEP_LAST,
                depth=16,
                reliability=ReliabilityPolicy.RELIABLE,
                durability=DurabilityPolicy.TRANSIENT_LOCAL,
            ),
        )
        self._override_pub = self.create_publisher(SurgeonRequest, "/simulation/surgeon_override", 10)
        self._direct_request_pub = self.create_publisher(SurgeonRequest, "/surgeon/request", 10)
        self.create_subscription(
            SimulationState,
            "/simulation/state",
            self._on_simulation_state,
            20,
            callback_group=self._callback_group,
        )
        # ScenarioStore is the sole writer.  This state core observes the
        # latched snapshot and atomically refreshes only its local parsed spec.
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
            callback_group=self._callback_group,
        )
        self.create_subscription(
            ExecutionSnapshot,
            "/btops/execution_snapshot",
            self._on_executor_snapshot,
            20,
            callback_group=self._callback_group,
        )
        self._start_client = self.create_client(
            StartBehavior,
            "/btops/start_behavior",
            callback_group=self._callback_group,
        )
        self._command_client = self.create_client(
            CommandExecutor,
            "/btops/command_executor",
            callback_group=self._callback_group,
        )
        self._runtime_client = self.create_client(
            GetRuntimeState,
            "/btops/get_runtime_state",
            callback_group=self._callback_group,
        )
        self.create_service(
            ControlSimulation,
            "/simulation/control",
            self._handle_control,
            callback_group=self._callback_group,
        )
        self.create_service(
            InjectSurgeonOverride,
            "/simulation/inject_surgeon_override",
            self._handle_debug_override,
            callback_group=self._callback_group,
        )
        # This is the operational voice ingress.  It deliberately shares the
        # existing typed request contract with Debug but has a different
        # lifecycle admission rule: operational commands are meaningful only
        # while the active scenario is running.  Keeping the endpoints
        # separate prevents a Debug intervention from becoming an accidental
        # live voice route, without inventing a second command schema.
        self.create_service(
            InjectSurgeonOverride,
            "/simulation/operational_surgeon_override",
            self._handle_operational_override,
            callback_group=self._callback_group,
        )

    def _wait_future(self, future, timeout_sec: float = 10.0):
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            if future.done():
                return future.result()
            time.sleep(0.05)
        raise TimeoutError("Timed out waiting for async operation to complete.")

    def _publish_control(
        self,
        command: str,
        repeat_count: int = 2,
        *,
        wait_for_subscriber: bool = True,
    ) -> None:
        """Publish one lifecycle edge without making normal Start poll observers.

        The Digital Twin, UI, and actor nodes observe this topic.  They are not
        an admission dependency for an otherwise valid ``/simulation/control``
        request.  Most lifecycle changes retain the short subscriber wait for
        delivery robustness, but the initial ``start_runtime`` edge must reach
        BT startup immediately after a warm restart instead of spending up to
        two seconds waiting for the ROS graph to report its observers.
        """

        if wait_for_subscriber:
            deadline = time.time() + 2.0
            while (
                self._control_pub.get_subscription_count() < 1
                and time.time() < deadline
            ):
                time.sleep(0.05)
        msg = String()
        msg.data = command
        publication_count = max(1, repeat_count)
        for index in range(publication_count):
            self._control_pub.publish(msg)
            # Keep the duplicate transport guard, but do not add a fixed sleep
            # after the final publication.  State acknowledgement, not this
            # delay, is the actual completion boundary.
            if index + 1 < publication_count:
                time.sleep(0.05)

    @staticmethod
    def _control_with_phase(command: str, phase_id: str = "") -> str:
        phase_id = str(phase_id or "").strip()
        return f"{command}:{phase_id}" if phase_id else command

    def _normalize_start_phase(self, phase_id: str = "") -> str:
        requested = str(phase_id or "").strip()
        if not requested:
            return ""
        if requested not in self._active_spec.phase_ids:
            allowed = ", ".join(self._active_spec.phase_ids)
            raise ValueError(f"unknown start phase '{requested}' for {self._active_bundle}; allowed: {allowed}")
        return requested

    def _on_simulation_state(self, msg: SimulationState) -> None:
        with self._latest_state_lock:
            self._latest_state = msg
            self._latest_state_generation += 1
            # Receipt time, rather than source time, is the mode-transition
            # liveness contract.  Identical semantic heartbeats must refresh it.
            self._latest_state_received_monotonic = time.monotonic()
        observed_execution_state = str(msg.execution_state or "").strip().casefold()
        run_id = str(getattr(msg, "procedure_run_id", "") or "").strip()
        binds_active_run = bool(msg.running) and observed_execution_state in {
            "running",
            "paused",
            "finishing",
        }
        # A manager restarted during the final Twin frame can still settle the
        # executor and issue the receipt for the already-observed run.
        binds_unowned_completion = (
            observed_execution_state == "completed"
            and not self._active_procedure_run_id
        )
        if run_id and (binds_active_run or binds_unowned_completion):
            self._active_procedure_run_id = run_id
            self._active_procedure_bundle = str(
                getattr(msg, "active_bundle", "") or ""
            ).strip()
        if msg.execution_state == "completed":
            should_terminate = self._running or self._execution_state != "completed"
            self._running = False
            self._execution_state = "completed"
            if should_terminate and not self._completion_terminate_started:
                self._completion_terminate_started = True
                thread = threading.Thread(
                    target=self._terminate_executor_after_completion,
                    args=(msg,),
                    name="simulation-completion-terminate",
                    daemon=True,
                )
                thread.start()
        self._apply_pending_scenario_config_if_quiescent()

    def _on_scenario_config(self, message: String) -> None:
        """Observe ScenarioStore's selected revision without republishing it."""

        try:
            published = parse_scenario_config(message.data)
            candidate = load_scenario_snapshot(
                self._spec_root,
                published.bundle_name,
            )
            if candidate.revision != published.revision:
                raise ValueError(
                    "scenario config revision does not match the authored bundle"
                )
            if Path(published.spec_dir).resolve() != candidate.spec_dir.resolve():
                raise ValueError(
                    "scenario config spec_dir does not match the configured spec root"
                )
        except Exception as exc:
            self.get_logger().warning(
                f"simulation manager scenario config ignored: {exc}",
                throttle_duration_sec=2.0,
            )
            return

        with self._scenario_config_lock:
            if (
                candidate.bundle_name == self._active_bundle
                and candidate.revision == self._scenario_config_revision
            ):
                return
            self._pending_scenario_config = candidate
        self._apply_pending_scenario_config_if_quiescent()

    def _scenario_config_apply_is_safe(self) -> bool:
        """Allow the local read-only spec view to follow paused/stopped state.

        ScenarioStore is the only selection writer.  The core does not reset,
        fan out parameters, or otherwise transact this update; it simply holds
        a pending parsed snapshot until the authoritative Twin says that the
        procedure is no longer actively running.  A newly started core may
        also accept the latched snapshot before its first Twin frame because it
        has not started any execution itself.
        """

        state = None
        lock = getattr(self, "_latest_state_lock", None)
        if lock is not None:
            with lock:
                state = getattr(self, "_latest_state", None)
        if state is None:
            return (
                not bool(getattr(self, "_running", False))
                and str(getattr(self, "_execution_state", "idle") or "")
                .strip()
                .casefold()
                in {"idle", "halted", "completed", "terminated"}
            )
        execution_state = str(
            getattr(state, "execution_state", "") or ""
        ).strip().casefold()
        # The Twin correctly represents a pause as ``running=True`` because
        # the procedure still owns its world state.  It is nevertheless the
        # explicit intervention boundary for a config observer.  All other
        # accepted states must be fully stopped.
        if execution_state == "paused":
            return True
        return (
            not bool(getattr(state, "running", False))
            and execution_state in {"idle", "halted", "completed", "terminated"}
        )

    def _apply_pending_scenario_config_if_quiescent(self) -> None:
        """Atomically refresh only the core's local parsed scenario view."""

        lock = getattr(self, "_scenario_config_lock", None)
        if lock is None:  # Lightweight historical unit fixtures.
            return
        with lock:
            candidate = getattr(self, "_pending_scenario_config", None)
            if candidate is None:
                return
            if not self._scenario_config_apply_is_safe():
                return
            self._active_bundle = candidate.bundle_name
            self._active_spec_dir = candidate.spec_dir
            self._active_config_revision = candidate.revision
            self._scenario_config_revision = candidate.revision
            self._active_spec = candidate.spec
            self._pending_scenario_config = None
        self.get_logger().info(
            "observed ScenarioStore revision: "
            f"{candidate.bundle_name}@{candidate.revision}"
        )

    def _on_executor_snapshot(self, msg: ExecutionSnapshot) -> None:
        if str(getattr(msg, "executor_name", "") or "") != self._executor_name:
            return
        state = str(getattr(msg, "execution_state", "") or "").strip().lower()
        if not state:
            return
        with self._latest_executor_state_lock:
            self._latest_executor_state = state
            self._latest_executor_state_generation += 1
            self._latest_executor_state_received_monotonic = time.monotonic()

    def _executor_state_event(self) -> tuple[str, int, float]:
        lock = getattr(self, "_latest_executor_state_lock", None)
        if lock is None:
            return "", 0, 0.0
        with lock:
            return (
                str(getattr(self, "_latest_executor_state", "") or ""),
                int(getattr(self, "_latest_executor_state_generation", 0)),
                float(
                    getattr(
                        self,
                        "_latest_executor_state_received_monotonic",
                        0.0,
                    )
                ),
            )

    def _terminate_executor_after_completion(self, completed_state: SimulationState) -> None:
        settled = False
        try:
            _, executor_generation, _ = self._executor_state_event()
            success, message = self._command_executor("terminate")
            if not success:
                raise RuntimeError(message or "failed to terminate completed executor")
            if not self._wait_for_executor_idle(
                timeout_sec=8.0,
                after_generation=executor_generation,
            ):
                raise RuntimeError("completed executor did not settle")
            settled = True
        except Exception as exc:
            self.get_logger().warn(f"failed to terminate executor after completion: {exc}")
        finally:
            self._running = False
            self._execution_state = "completed"
        try:
            if settled:
                self._finalize_terminal_run(
                    terminal_kind="completed",
                    execution_state="completed",
                    state=completed_state,
                    message="procedure completed and executor settled",
                )
        finally:
            # Keep this guard asserted through the automatic reset.  Otherwise
            # a repeated completed heartbeat can start a second finalizer while
            # the Twin is still acknowledging the first reset edge.
            self._completion_terminate_started = False

    def _latest_simulation_state(self) -> SimulationState | None:
        with self._latest_state_lock:
            return self._latest_state

    def _publish_terminal_receipt(
        self,
        *,
        terminal_kind: str,
        execution_state: str,
        state: SimulationState | None = None,
        message: str = "",
    ) -> bool:
        """Publish one source-agnostic receipt after lifecycle settlement.

        Digital Twin publishes ``halted`` before the executor termination wait
        has completed.  Consumers that create irreversible side effects must
        therefore observe this manager-owned receipt instead of treating a
        raw state frame as proof that Stop succeeded.
        """

        observed = state or self._latest_simulation_state()
        if observed is None:
            self.get_logger().warn(
                f"{terminal_kind} settled without a simulation state; terminal receipt omitted"
            )
            return False
        run_id = str(getattr(observed, "procedure_run_id", "") or "").strip()
        if not run_id:
            self.get_logger().warn(
                f"{terminal_kind} settled without procedure_run_id; terminal receipt omitted"
            )
            return False
        expected_state = {
            "stop": "halted",
            "completed": "completed",
        }.get(terminal_kind)
        observed_state = str(
            getattr(observed, "execution_state", "") or ""
        ).strip().casefold()
        if (
            expected_state is None
            or execution_state != expected_state
            or bool(getattr(observed, "running", False))
            or observed_state != expected_state
        ):
            self.get_logger().warn(
                f"invalid {terminal_kind} terminal state; terminal receipt omitted"
            )
            return False
        if run_id != self._active_procedure_run_id:
            self.get_logger().warn(
                f"stale {terminal_kind} run id; terminal receipt omitted"
            )
            return False
        observed_bundle = str(
            getattr(observed, "active_bundle", "") or ""
        ).strip()
        if (
            self._active_procedure_bundle
            and observed_bundle != self._active_procedure_bundle
        ):
            self.get_logger().warn(
                f"mismatched {terminal_kind} bundle; terminal receipt omitted"
            )
            return False
        with self._terminal_receipt_lock:
            if run_id == self._last_terminal_receipt_run_id:
                return False
            self._last_terminal_receipt_run_id = run_id
        payload = {
            "schema": TERMINAL_RECEIPT_SCHEMA,
            "procedure_id": str(getattr(observed, "procedure_id", "") or "").strip(),
            "active_bundle": str(getattr(observed, "active_bundle", "") or "").strip(),
            "procedure_run_id": run_id,
            "terminal_kind": terminal_kind,
            "execution_state": execution_state,
            "success": True,
            "message": str(message or "").strip()[:300],
            "stamp_sec": round(self.get_clock().now().nanoseconds / 1_000_000_000.0, 6),
        }
        self._terminal_receipt_pub.publish(
            String(data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        )
        return True

    def _publish_procedure_lifecycle_event(
        self,
        *,
        event: str,
        procedure_run_id: str,
    ) -> bool:
        """Publish one manager-owned lifecycle edge after admission.

        The event is intentionally tiny and run-scoped.  It is only an audio
        presentation hint; it cannot admit, route, or complete the procedure.
        A missing publisher or run identity must never make the finish command
        fail, so this helper is best-effort and fail-closed for TTS.
        """

        normalized_event = str(event or "").strip()
        run_id = str(procedure_run_id or "").strip()
        publisher = getattr(self, "_lifecycle_event_pub", None)
        if normalized_event != "procedure_finishing" or not run_id or publisher is None:
            return False
        payload = {
            "schema": PROCEDURE_LIFECYCLE_EVENT_SCHEMA,
            "event": normalized_event,
            "procedure_run_id": run_id,
        }
        try:
            publisher.publish(
                String(data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            )
        except Exception as exc:
            self.get_logger().warning(
                f"procedure lifecycle event publish failed: {type(exc).__name__}"
            )
            return False
        return True

    @staticmethod
    def _is_current_terminal_state(
        state: SimulationState | None,
        *,
        procedure_run_id: str,
        active_bundle: str,
        execution_state: str,
    ) -> bool:
        """Return whether the latest Twin frame is still this terminal run."""

        if state is None:
            return False
        if bool(getattr(state, "running", False)):
            return False
        if (
            str(getattr(state, "execution_state", "") or "").strip().casefold()
            != str(execution_state or "").strip().casefold()
        ):
            return False
        if (
            str(getattr(state, "procedure_run_id", "") or "").strip()
            != str(procedure_run_id or "").strip()
        ):
            return False
        observed_bundle = str(getattr(state, "active_bundle", "") or "").strip()
        return not active_bundle or observed_bundle == active_bundle

    def _finalize_terminal_run(
        self,
        *,
        terminal_kind: str,
        execution_state: str,
        state: SimulationState | None = None,
        message: str = "",
        allow_cancelled_reset: bool = False,
    ) -> bool:
        """Publish the durable terminal receipt, then restore the idle layout.

        UI Reset already converges through ``_reset_digital_twin_to_idle``.
        Terminal paths use that same owner-controlled route, but only after
        their matching terminal receipt is visible to record/observer nodes.
        A newer run, a manual reset, or an unsettled terminal must never be
        overwritten by a stale completion callback.
        """

        terminal_state = state or self._latest_simulation_state()
        if terminal_state is None:
            self.get_logger().warn(
                f"{terminal_kind} settled without a terminal frame; automatic reset skipped"
            )
            return False
        procedure_run_id = str(
            getattr(terminal_state, "procedure_run_id", "") or ""
        ).strip()
        active_bundle = str(
            getattr(terminal_state, "active_bundle", "")
            or self._active_procedure_bundle
            or self._active_bundle
        ).strip()

        with self._terminal_auto_reset_lock:
            if not self._publish_terminal_receipt(
                terminal_kind=terminal_kind,
                execution_state=execution_state,
                state=terminal_state,
                message=message,
            ):
                return False

            latest = self._latest_simulation_state()
            if not self._is_current_terminal_state(
                latest,
                procedure_run_id=procedure_run_id,
                active_bundle=active_bundle,
                execution_state=execution_state,
            ):
                self.get_logger().warn(
                    f"{terminal_kind} reset skipped because the terminal run is no longer current"
                )
                return False

            self._execution_state = "resetting"
            try:
                reset_kwargs = {"expected_bundle": active_bundle}
                if allow_cancelled_reset:
                    reset_kwargs["allow_cancelled_reset"] = True
                self._reset_digital_twin_to_idle(**reset_kwargs)
            except Exception as exc:
                # Terminal work itself has already settled and been receipted.
                # Preserve that truthful terminal state rather than turning a
                # reset acknowledgement failure into a fake idle state.
                self._execution_state = execution_state
                self.get_logger().error(
                    f"automatic reset after {terminal_kind} failed: {exc}"
                )
                return False

            # The acknowledgement predicate already proved the Twin is idle.
            # Set the local mirror explicitly as well, so this helper remains
            # correct if the reset transport implementation changes.
            self._set_idle_state()
            self._active_procedure_run_id = ""
            self._active_procedure_bundle = ""
            self.get_logger().info(
                f"{terminal_kind} terminal run reset to the authored idle layout"
            )
            return True

    def _wait_for_simulation_state(
        self,
        predicate,
        timeout_sec: float,
        description: str,
        *,
        warn: bool = True,
        after_generation: int = -1,
    ) -> bool:
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            with self._latest_state_lock:
                state = self._latest_state
                generation = self._latest_state_generation
            if state is not None and generation > after_generation:
                try:
                    if predicate(state):
                        return True
                except Exception:
                    pass
            time.sleep(0.02)
        if warn:
            self.get_logger().warn(f"Timed out waiting for {description}.")
        return False

    def _raise_if_operation_cancelled(self) -> None:
        if self._operation_cancel.is_set():
            raise RuntimeError("operation interrupted by newer control command")

    def _publish_control_until(self, command: str, predicate, timeout_sec: float, description: str) -> bool:
        deadline = time.time() + timeout_sec
        attempts = 3
        for attempt in range(attempts):
            if self._operation_cancel.is_set():
                return False
            with self._latest_state_lock:
                previous_generation = self._latest_state_generation
            self._publish_control(command, repeat_count=2)
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            wait_slice = max(0.5, remaining / max(1, attempts - attempt))
            if self._wait_for_simulation_state(
                predicate,
                timeout_sec=min(wait_slice, remaining),
                description=description,
                warn=False,
                after_generation=previous_generation,
            ):
                return True
        self.get_logger().warn(f"Timed out waiting for {description}.")
        return False

    def _all_instruments_at_initial_layout(self, state: SimulationState) -> bool:
        if not state.instrument_states:
            return False
        configured_states = {
            str(initial_state.instance_id): initial_state
            for initial_state in self._active_spec.get_initial_instrument_states()
        }
        observed_configured_instances: set[str] = set()
        for instrument in state.instrument_states:
            instance_id = str(instrument.instance_id)
            configured_state = configured_states.get(instance_id)
            if configured_state is not None:
                observed_configured_instances.add(instance_id)
                if str(instrument.location_id) != str(configured_state.location_id):
                    return False
                if (
                    configured_state.lifecycle_stage
                    and str(instrument.lifecycle_stage)
                    != str(configured_state.lifecycle_stage)
                ):
                    return False
                continue
            home_location_id = str(instrument.home_location_id)
            home_location_type = str(instrument.home_location_type)
            if home_location_id and str(instrument.location_id) != home_location_id:
                return False
            if home_location_type and str(instrument.location_type) != home_location_type:
                return False
        return observed_configured_instances == set(configured_states)

    def _start_behavior(self, clear_blackboard: bool) -> tuple[bool, str]:
        if not self._start_client.wait_for_service(timeout_sec=5.0):
            return False, "btops start_behavior service is unavailable"
        last_message = "btops start_behavior returned no response"
        for attempt in range(5):
            self._raise_if_operation_cancelled()
            # The caller has already established the executor-idle boundary.
            # Re-querying GetRuntimeState here serialized the complete BT trace
            # a second time on every ordinary start.  Retry branches below still
            # terminate and confirm idle before their next dispatch.
            _, executor_generation, _ = self._executor_state_event()
            monitoring_enabled = bool(
                getattr(self, "_enable_bt_monitoring", False)
            )
            # AutoAPMS reserves -1 as the only "do not install a
            # BT::Groot2Publisher" sentinel.  Port 0 is still treated as a
            # real publisher request and fails during tree startup.
            requested_groot2_port = (
                self._reserve_groot2_port() if monitoring_enabled else -1
            )
            request = StartBehavior.Request()
            request.executor_name = self._executor_name
            request.mode = "resource"
            request.category = "tree"
            request.resource_identity = RESOURCE_ID
            request.inline_source = ""
            request.source_format = ""
            request.build_handler = ""
            request.entry_point = ENTRY_POINT
            request.node_manifest_identities = list(NODE_MANIFESTS)
            request.attach = False
            request.clear_blackboard = clear_blackboard
            request.enable_monitoring = monitoring_enabled
            request.tick_rate_hz = float(self._tick_rate_hz)
            request.requested_groot2_port = int(requested_groot2_port)
            request.parameter_assignments = []
            # From dispatch until a terminal executor snapshot is observed, a
            # mode transition must fail closed even if the digital twin has
            # already published an inactive-looking frame.
            self._executor_settled_confirmed = False
            future = self._start_client.call_async(request)
            deadline = time.time() + 15.0
            response = None
            while time.time() < deadline:
                self._raise_if_operation_cancelled()
                if future.done():
                    response = future.result()
                    break
                time.sleep(0.05)
            if response is None:
                if self._wait_for_executor_running(
                    timeout_sec=2.0,
                    after_generation=executor_generation,
                ):
                    return True, "simulation started after delayed start_behavior response"
                time.sleep(0.25 * (attempt + 1))
                continue
            if response.success:
                return True, str(response.message)
            last_message = str(response.message)
            self._restore_settled_after_explicit_start_rejection()
            lowered_message = last_message.lower()
            if any(
                marker in lowered_message
                for marker in RETRYABLE_START_ERROR_MARKERS
            ):
                self.get_logger().warn(
                    "retrying BT start after transient executor failure "
                    f"(attempt={attempt + 1}, groot2_port={requested_groot2_port}): "
                    f"{last_message}"
                )
                if self._wait_for_executor_running(
                    timeout_sec=2.0,
                    after_generation=executor_generation,
                ):
                    return True, last_message
                self._command_executor("terminate")
                self._wait_for_executor_idle(timeout_sec=8.0)
                time.sleep(0.35 * (attempt + 1))
                continue
            return False, last_message
        return False, last_message

    def _restore_settled_after_explicit_start_rejection(self) -> bool:
        """Recover the initial no-session invariant after an explicit reject.

        A timed-out StartBehavior call may still have dispatched work, so it
        deliberately leaves the executor fail-closed.  Only an actual failed
        response followed by BTops' exact no-snapshot result proves that the
        rejected request created no executor session.
        """

        try:
            success, _state, detail = self._get_runtime_state_detail(
                service_timeout_sec=0.25,
                response_timeout_sec=0.75,
            )
        except Exception:
            return False
        if not success and str(detail).startswith(NO_RUNTIME_SNAPSHOT_PREFIX):
            self._executor_settled_confirmed = True
            return True
        return False

    def _reserve_groot2_port(self) -> int:
        if self._groot2_port > 0:
            return int(self._groot2_port)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            return int(sock.getsockname()[1])

    def _command_executor(self, command: str) -> tuple[bool, str]:
        if not self._command_client.wait_for_service(timeout_sec=5.0):
            return False, "btops command_executor service is unavailable"
        request = CommandExecutor.Request()
        request.executor_name = self._executor_name
        request.command = command
        future = self._command_client.call_async(request)
        response = self._wait_future(future, timeout_sec=10.0)
        if response is None:
            return False, "btops command_executor returned no response"
        return bool(response.success), str(response.message)

    def _get_runtime_state_detail(
        self,
        *,
        service_timeout_sec: float = 5.0,
        response_timeout_sec: float = 10.0,
    ) -> tuple[bool, str, str]:
        if not self._runtime_client.wait_for_service(
            timeout_sec=service_timeout_sec
        ):
            return False, "unknown", "btops runtime-state service is unavailable"
        request = GetRuntimeState.Request()
        request.executor_name = self._executor_name
        future = self._runtime_client.call_async(request)
        response = self._wait_future(future, timeout_sec=response_timeout_sec)
        if response is None:
            return False, "unknown", "btops runtime-state returned no response"
        if not response.success:
            return False, "unknown", str(response.message or "runtime state unavailable")
        return (
            True,
            str(response.snapshot.execution_state).strip().lower(),
            str(response.message or "ok"),
        )

    def _get_runtime_state(self) -> tuple[bool, str]:
        success, state, _detail = self._get_runtime_state_detail()
        return success, state

    def _wait_for_executor_idle(
        self,
        timeout_sec: float = 8.0,
        *,
        after_generation: int = -1,
    ) -> bool:
        deadline = time.monotonic() + timeout_sec
        next_service_probe = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            state, generation, received_monotonic = self._executor_state_event()
            age_sec = time.monotonic() - received_monotonic
            if (
                received_monotonic > 0.0
                and generation > after_generation
                and 0.0 <= age_sec <= EXECUTOR_SNAPSHOT_MAX_AGE_SEC
                and state in TRANSITION_READY_EXECUTOR_STATES
            ):
                self._executor_settled_confirmed = True
                return True
            if time.monotonic() < next_service_probe:
                time.sleep(0.02)
                continue
            try:
                success, service_state, detail = self._get_runtime_state_detail(
                    service_timeout_sec=0.25,
                    response_timeout_sec=0.75,
                )
                if success and service_state in TRANSITION_READY_EXECUTOR_STATES:
                    self._executor_settled_confirmed = True
                    return True
                if (
                    not success
                    and str(detail).startswith(NO_RUNTIME_SNAPSHOT_PREFIX)
                    and self._executor_settled_confirmed
                ):
                    return True
            except Exception:
                pass
            next_service_probe = time.monotonic() + 0.5
            time.sleep(0.02)
        return False

    def _wait_for_executor_running(
        self,
        timeout_sec: float = 3.0,
        *,
        after_generation: int = -1,
    ) -> bool:
        deadline = time.monotonic() + timeout_sec
        next_service_probe = time.monotonic() + 0.1
        service_probe_count = 0
        # AutoAPMS often reports "succeeded" once the detached executor has
        # actually initialized while the tree keeps ticking.  Do not accept the
        # earlier "starting"/"accepted" transport states: asynchronous Groot or
        # tree-construction failures can still arrive after those.  The event
        # topic is intentionally preferred over the
        # GetRuntimeState service: that response also serializes the full tree,
        # blackboard, and transition history.
        while time.monotonic() < deadline:
            state, generation, received_monotonic = self._executor_state_event()
            age_sec = time.monotonic() - received_monotonic
            if (
                generation > after_generation
                and received_monotonic > 0.0
                and 0.0 <= age_sec <= EXECUTOR_SNAPSHOT_MAX_AGE_SEC
                and state in EXECUTOR_RUNNING_STATES
            ):
                return True
            if (
                generation > after_generation
                and received_monotonic > 0.0
                and 0.0 <= age_sec <= EXECUTOR_SNAPSHOT_MAX_AGE_SEC
                and state in EXECUTOR_START_FAILURE_STATES
            ):
                return False
            # With optional Groot recording disabled the runtime-state response
            # no longer carries a large transition ledger.  Two early probes
            # let the operational path observe AutoAPMS' asynchronous
            # "succeeded" result without waiting for the gateway's 0.5 s
            # periodic publication.  Debug/Groot mode remains event-only.
            if (
                not bool(getattr(self, "_enable_bt_monitoring", False))
                and service_probe_count < 2
                and time.monotonic() >= next_service_probe
            ):
                service_probe_count += 1
                next_service_probe = time.monotonic() + 0.15
                try:
                    success, service_state, _detail = self._get_runtime_state_detail(
                        service_timeout_sec=0.1,
                        response_timeout_sec=0.25,
                    )
                    if success and service_state in EXECUTOR_RUNNING_STATES:
                        return True
                    if success and service_state in EXECUTOR_START_FAILURE_STATES:
                        return False
                except Exception:
                    pass
            time.sleep(0.02)
        # Compatibility fallback for an older BTops runtime that does not emit
        # execution_snapshot.  Perform one bounded query, never a large-snapshot
        # polling loop.
        try:
            success, state, _detail = self._get_runtime_state_detail(
                service_timeout_sec=0.25,
                response_timeout_sec=0.75,
            )
            return bool(success and state in EXECUTOR_RUNNING_STATES)
        except Exception:
            return False

    def _prepare_executor_for_restart(self) -> None:
        manager_inactive = (
            not self._running
            and self._execution_state in {"idle", "terminated", "halted"}
        )
        state, _generation, received_monotonic = self._executor_state_event()
        event_age_sec = time.monotonic() - received_monotonic
        event_is_fresh = (
            received_monotonic > 0.0
            and 0.0 <= event_age_sec <= EXECUTOR_SNAPSHOT_MAX_AGE_SEC
        )
        if manager_inactive and event_is_fresh and state in TRANSITION_READY_EXECUTOR_STATES:
            self._executor_settled_confirmed = True
            return
        if manager_inactive:
            try:
                success, service_state, detail = self._get_runtime_state_detail(
                    service_timeout_sec=PRESTART_EXECUTOR_SERVICE_TIMEOUT_SEC,
                    response_timeout_sec=PRESTART_EXECUTOR_RESPONSE_TIMEOUT_SEC,
                )
                if success and service_state in TRANSITION_READY_EXECUTOR_STATES:
                    self._executor_settled_confirmed = True
                    return
                if (
                    not success
                    and str(detail).startswith(NO_RUNTIME_SNAPSHOT_PREFIX)
                    and bool(getattr(self, "_executor_settled_confirmed", False))
                ):
                    return
            except Exception as exc:
                raise RuntimeError(f"executor state check failed before start: {exc}") from exc
        success, message = self._command_executor("terminate")
        if not success:
            raise RuntimeError(message or "failed to settle behavior tree executor")
        if not self._wait_for_executor_idle(timeout_sec=4.0):
            raise RuntimeError("behavior tree executor did not settle before start")

    def _reset_digital_twin_to_idle(
        self,
        *,
        expected_bundle: str | None = None,
        allow_cancelled_reset: bool = False,
    ) -> None:
        bundle = self._active_bundle if expected_bundle is None else expected_bundle
        predicate = lambda state: (
            (not bundle or state.active_bundle == bundle)
            and (not state.running)
            and state.execution_state == "idle"
            and self._all_instruments_at_initial_layout(state)
        )
        if allow_cancelled_reset:
            # Stop/Reset can interrupt an asynchronous Start before that
            # worker has observed its cancellation event.  Keep the event set
            # so Start cannot commit actors, but still deliver the terminal
            # reset after its executor has settled.
            self._publish_control("reset", repeat_count=2)
            reset_confirmed = self._wait_for_simulation_state(
                predicate,
                timeout_sec=8.0,
                description="idle digital twin frame after reset",
            )
        else:
            reset_confirmed = self._publish_control_until(
                "reset",
                predicate,
                timeout_sec=8.0,
                description="idle digital twin frame after reset",
            )
        if not reset_confirmed:
            raise RuntimeError("digital twin did not publish an idle home frame after reset")
        self._set_idle_state()

    def _stop_digital_twin_to_halted(self) -> bool:
        expected_bundle = self._active_procedure_bundle or self._active_bundle
        expected_run_id = self._active_procedure_run_id
        if not self._publish_control_until(
            "stop",
            lambda state: (
                state.active_bundle == expected_bundle
                and bool(expected_run_id)
                and state.procedure_run_id == expected_run_id
                and (not state.running)
                and state.execution_state == "halted"
            ),
            timeout_sec=5.0,
            description="halted digital twin frame after stop",
        ):
            self.get_logger().warn("digital twin did not publish a halted frame after stop")
            return False
        return True

    def _begin_operation(self, name: str) -> bool:
        with self._operation_lock:
            self._operation_rejection_message = ""
            if self._operation_name:
                self._operation_rejection_message = (
                    f"{self._operation_name} already in progress"
                )
                return False
            self._operation_cancel.clear()
            self._operation_name = name
            return True

    def _finish_operation(self, name: str) -> None:
        with self._operation_lock:
            if self._operation_name == name:
                self._operation_name = ""
                self._operation_cancel.clear()

    def _wait_for_operation_clear(self, timeout_sec: float = 10.0) -> bool:
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            with self._operation_lock:
                if not self._operation_name:
                    return True
            time.sleep(0.1)
        return False

    def _run_async(self, name: str, target) -> tuple[bool, str]:
        if not self._begin_operation(name):
            return False, self._operation_rejection_message or "operation rejected"

        def runner() -> None:
            try:
                target()
            except Exception as exc:
                self.get_logger().error(f"{name} operation failed: {exc}")
                if name == "start" and not self._operation_cancel.is_set():
                    self._publish_control("stop")
                    self._set_idle_state()
            finally:
                self._finish_operation(name)

        thread = threading.Thread(target=runner, name=f"simulation-{name}", daemon=True)
        thread.start()
        return True, f"{name} requested"

    def _run_sync(self, name: str, target) -> tuple[bool, str]:
        if not self._begin_operation(name):
            return False, self._operation_rejection_message or "operation rejected"
        try:
            message = target()
            return True, str(message or f"{name} completed")
        except Exception as exc:
            self.get_logger().error(f"{name} operation failed: {exc}")
            return False, str(exc)
        finally:
            self._finish_operation(name)

    def _start_sequence(self, start_phase_id: str = "", *, prepare_executor: bool = True) -> str:
        started_monotonic = time.monotonic()
        start_phase_id = self._normalize_start_phase(start_phase_id)
        # An empty phase means "continue from the currently selected twin
        # state".  Explicit Reset is the only command that restores authored
        # layout/phase.  This keeps pre-start real-to-sim observations and
        # researcher edits intact instead of rebuilding the world on Start.
        target_phase_id = start_phase_id
        self._running = False
        self._completion_terminate_started = False
        if prepare_executor:
            # Preserve the preceding idle/halted state during the executor
            # readiness check.  Marking this manager ``starting`` first makes
            # _prepare_executor_for_restart miss its safe idle fast path and
            # send an unnecessary terminate request to an already-idle tree.
            self._prepare_executor_for_restart()
        self._raise_if_operation_cancelled()
        self._execution_state = "starting"
        # The button and a grounded voice command both land on this same
        # ControlSimulation service.  ``start_runtime`` is an observer-facing
        # lifecycle edge, not a second start admission or a Digital-Twin
        # receipt gate.  Publishing it once without a subscriber poll lets an
        # idle core begin the BT request immediately; the execution bridge
        # still keeps controller-facing dispatch disabled until
        # ``start_actors`` is committed below after the executor confirms it
        # is running.
        self._publish_control(
            self._control_with_phase("start_runtime", start_phase_id),
            repeat_count=1,
            wait_for_subscriber=False,
        )
        self._raise_if_operation_cancelled()
        _, executor_generation, _ = self._executor_state_event()
        success, message = self._start_behavior(clear_blackboard=True)
        if success:
            self._raise_if_operation_cancelled()
            if not self._wait_for_executor_running(
                timeout_sec=3.0,
                after_generation=executor_generation,
            ):
                self._publish_control("stop")
                self._set_idle_state()
                raise RuntimeError("behavior tree executor did not enter a running state")
            # This is also the bed-group dispatch gate.  No surgeon actor or
            # external group request can cause motion before BT startup is
            # positively confirmed.
            self._commit_start_actors(start_phase_id)
            latency_ms = (time.monotonic() - started_monotonic) * 1000.0
            self.get_logger().info(
                f"{message or 'simulation started'}; start_latency_ms={latency_ms:.1f}"
            )
            phase_suffix = f" from {target_phase_id}" if start_phase_id else ""
            return message or f"simulation running on {self._active_bundle}{phase_suffix}"
        self._publish_control("stop")
        self._set_idle_state()
        raise RuntimeError(message or "failed to start simulation")

    def _commit_start_actors(self, start_phase_id: str = "") -> None:
        """Atomically commit the final start gate against stop/reset.

        Holding the operation lock across the cancellation check, control
        publication, and local state commit guarantees one of two orders:
        start_actors then reset, or reset with no later start_actors.
        """

        with self._operation_lock:
            if self._operation_cancel.is_set():
                raise RuntimeError("operation interrupted by newer control command")
            self._publish_control(
                self._control_with_phase("start_actors", start_phase_id)
            )
            self._running = True
            self._execution_state = "running"

    def _interrupt_start_sequence(self, command: str) -> str:
        with self._operation_lock:
            self._operation_cancel.set()
            self._running = False
            self._execution_state = "resetting" if command == "reset" else "stopping"
        self._publish_control("reset" if command == "reset" else "stop")
        try:
            _, executor_generation, _ = self._executor_state_event()
            success, message = self._command_executor("terminate")
            if not success:
                raise RuntimeError(message or "failed to request executor termination")
            if not self._wait_for_executor_idle(
                timeout_sec=6.0,
                after_generation=executor_generation,
            ):
                raise RuntimeError("behavior tree executor did not settle after termination")
        except Exception as exc:
            self.get_logger().warn(f"failed to terminate executor while interrupting start: {exc}")
            raise
        if command == "reset":
            self._publish_control("reset")
            self._set_idle_state()
            return "start interrupted; simulation runtime reset to idle"
        if not self._stop_digital_twin_to_halted():
            raise RuntimeError("digital twin did not confirm halted state after stop")
        self._execution_state = "halted"
        reset_completed = self._finalize_terminal_run(
            terminal_kind="stop",
            execution_state="halted",
            message="start interrupted and executor settled",
            allow_cancelled_reset=True,
        )
        if reset_completed:
            return "start interrupted; simulation stopped and runtime reset to idle"
        return "start interrupted; simulation stopped"

    def _pause_sequence(self) -> str:
        if self._execution_state == "paused":
            return "simulation already paused"
        if not self._running or self._execution_state != "running":
            raise RuntimeError("simulation is not running")
        success, message = self._command_executor("pause")
        if not success:
            raise RuntimeError(message or "failed to pause simulation")
        self._publish_control("pause")
        self._running = True
        self._execution_state = "paused"
        self.get_logger().info(message or "simulation paused")
        return message or "simulation paused"

    def _resume_sequence(self) -> str:
        if self._running and self._execution_state == "running":
            return "simulation already running"
        if self._execution_state != "paused":
            raise RuntimeError("simulation is not paused")
        self._publish_control("resume")
        self._wait_for_simulation_state(
            lambda state: state.running and state.execution_state == "running" and len(state.instrument_states) > 0,
            timeout_sec=5.0,
            description="resumed digital twin frame",
        )
        success, message = self._command_executor("resume")
        if not success:
            self._publish_control("pause")
            raise RuntimeError(message or "failed to resume simulation")
        self._wait_for_executor_running(timeout_sec=8.0)
        self._running = True
        self._execution_state = "running"
        self.get_logger().info(message or "simulation resumed")
        return message or "simulation resumed"

    def _stop_sequence(self) -> str:
        self._running = False
        self._execution_state = "stopping"
        if not self._stop_digital_twin_to_halted():
            raise RuntimeError("digital twin did not confirm halted state after stop")
        _, executor_generation, _ = self._executor_state_event()
        success, message = self._command_executor("terminate")
        if not success:
            raise RuntimeError(message or "failed to request executor termination")
        if not self._wait_for_executor_idle(
            timeout_sec=8.0,
            after_generation=executor_generation,
        ):
            raise RuntimeError("behavior tree executor did not settle after termination")
        if success:
            self._execution_state = "halted"
            reset_completed = self._finalize_terminal_run(
                terminal_kind="stop",
                execution_state="halted",
                message=message or "simulation stopped and executor settled",
            )
            self.get_logger().info(message or "simulation stopped")
            if reset_completed:
                return f"{message or 'simulation stopped'}; simulation runtime reset to idle"
            return message or "simulation stopped"
        self._execution_state = "halted"
        raise RuntimeError(message or "failed to stop simulation")

    def _finish_sequence(self) -> str:
        """Ask the Twin to complete its bounded end-of-procedure cleanup.

        This intentionally does not halt the BT or clear the current world
        state.  A typed completion request lets the Twin preserve the target
        snapshot, drive the normal recovery Actions, and publish ``completed``
        only after that work has actually settled.
        """

        if self._execution_state == "completed":
            return "simulation already completed"
        if self._execution_state == "finishing":
            return "completion cleanup already in progress"
        if not self._running or self._execution_state != "running":
            raise RuntimeError("simulation is not running")

        completion = SurgeonRequest()
        completion.stamp = self.get_clock().now().to_msg()
        completion.event_type = "request_procedure_completion"
        # The typed completion request must reach the Twin even when the
        # optional autonomous surgeon actor is disabled or muted.  It remains
        # an admitted SimulationManager command, not a raw voice topic.
        completion.override = True
        completion.note = "simulation_manager voice procedure finish"
        self._direct_request_pub.publish(completion)

        # Make repeat voice utterances idempotent immediately; the Twin's
        # authoritative finishing/completed snapshots still own settlement.
        self._execution_state = "finishing"
        run_id = str(getattr(self, "_active_procedure_run_id", "") or "").strip()
        if not run_id:
            latest = self._latest_simulation_state()
            run_id = str(getattr(latest, "procedure_run_id", "") or "").strip()
        self._publish_procedure_lifecycle_event(
            event="procedure_finishing",
            procedure_run_id=run_id,
        )
        self.get_logger().info("completion cleanup requested")
        return "completion cleanup requested"

    def _reset_sequence(self) -> str:
        self._running = False
        self._execution_state = "resetting"
        self._completion_terminate_started = False
        self._prepare_executor_for_restart()
        self._reset_digital_twin_to_idle()
        self._active_procedure_run_id = ""
        self._active_procedure_bundle = ""
        return "simulation runtime reset to idle"

    def _set_idle_state(self) -> None:
        self._running = False
        self._execution_state = "idle"

    def _handle_control(self, request, response):
        command = request.command.strip().lower()
        requested_start_phase = str(getattr(request, "start_phase_id", "") or "").strip()
        try:
            if self._operation_name == "start":
                if command in {"stop", "reset"}:
                    message = self._interrupt_start_sequence(command)
                    response.success = True
                    response.message = message
                    response.running = self._running
                    response.execution_state = self._execution_state
                    return response
                if command == "start":
                    response.success = True
                    response.message = "start already in progress"
                    response.running = self._running
                    response.execution_state = self._execution_state
                    return response
                if command in {"pause", "resume", "finish"}:
                    response.success = False
                    response.message = "simulation is still starting"
                    response.running = self._running
                    response.execution_state = self._execution_state
                    return response
            elif command in {"start", "resume"} and self._operation_name in {"stop", "reset", "pause"}:
                if not self._wait_for_operation_clear(timeout_sec=25.0):
                    response.success = False
                    response.message = f"{self._operation_name} already in progress"
                    response.running = self._running
                    response.execution_state = self._execution_state
                    return response
            elif command == self._operation_name and command in {
                "pause",
                "resume",
                "reset",
                "stop",
                "finish",
            }:
                response.success = True
                response.message = f"{command} already in progress"
                response.running = self._running
                response.execution_state = self._execution_state
                return response

            if command == "start":
                try:
                    normalized_start_phase = self._normalize_start_phase(requested_start_phase)
                except ValueError as exc:
                    response.success = False
                    response.message = str(exc)
                    response.running = self._running
                    response.execution_state = self._execution_state
                    return response
                if self._running and self._execution_state == "running":
                    response.success = True
                    response.message = "simulation already running"
                else:
                    success, message = self._run_async(
                        "start",
                        lambda: self._start_sequence(normalized_start_phase),
                    )
                    response.success = success
                    response.message = message
            elif command == "pause":
                success, message = self._run_sync("pause", self._pause_sequence)
                response.success = success
                response.message = message
            elif command == "resume":
                success, message = self._run_sync("resume", self._resume_sequence)
                response.success = success
                response.message = message
            elif command == "status":
                response.success = True
                response.message = "simulation status"
            elif command == "reset":
                success, message = self._run_async("reset", self._reset_sequence)
                response.success = success
                response.message = message
            elif command == "stop":
                if not self._running and self._execution_state in {"idle", "halted", "terminated"}:
                    response.success = True
                    response.message = "simulation already stopped"
                    response.running = self._running
                    response.execution_state = self._execution_state
                    return response
                success, message = self._run_sync("stop", self._stop_sequence)
                response.success = success
                response.message = message
            elif command == "finish":
                success, message = self._run_sync("finish", self._finish_sequence)
                response.success = success
                response.message = message
            else:
                response.success = False
                response.message = f"unsupported simulation control command '{request.command}'"
            response.running = self._running
            response.execution_state = self._execution_state
            return response
        except Exception as exc:
            response.success = False
            response.message = str(exc)
            response.running = self._running
            response.execution_state = self._execution_state
            return response

    def _handle_debug_override(self, request, response):
        """Accept explicit Debug intervention only at a non-running boundary."""

        state = str(self._execution_state or "").strip().casefold()
        if state == "running" or state in {"starting", "resuming", "finishing"}:
            response.success = False
            response.message = (
                "simulation is running; pause or stop before injecting a Debug "
                "surgeon override"
            )
            return response
        return self._handle_override(request, response, source="debug")

    def _handle_operational_override(self, request, response):
        """Accept the one operational typed-voice route during a live scenario."""

        if not self._running or self._execution_state != "running":
            response.success = False
            response.message = "simulation is not running; operational surgeon override was not published"
            return response
        return self._handle_override(request, response, source="operational")

    def _handle_override(self, request, response, *, source: str = "manual"):
        with self._operation_lock:
            if bool(getattr(self, "_override_in_progress", False)):
                response.success = False
                response.message = "another surgeon override is already in progress"
                return response
            self._override_in_progress = True
        try:
            return self._handle_override_impl(request, response, source=source)
        finally:
            with self._operation_lock:
                self._override_in_progress = False

    def _handle_override_impl(self, request, response, *, source: str = "manual"):
        event_type = request.event_type.strip()
        if event_type not in ALLOWED_OVERRIDE_EVENTS:
            response.success = False
            response.message = (
                f"unsupported surgeon override event_type '{request.event_type}'; "
                f"expected one of {sorted(ALLOWED_OVERRIDE_EVENTS)}"
            )
            return response
        requested_tool = request.requested_tool.strip()
        canonical_tool = ""
        if requested_tool:
            canonical_tool = self._active_spec.resolve_instrument_alias(requested_tool) or ""
            if not canonical_tool:
                response.success = False
                response.message = (
                    f"unknown tool '{requested_tool}' for active bundle '{self._active_bundle}'"
                )
                return response
        elif event_type in TOOL_REQUIRED_OVERRIDE_EVENTS:
            response.success = False
            response.message = f"event_type '{event_type}' requires requested_tool"
            return response

        # Manual input has one authoritative route. The legacy rule actor consumes
        # /simulation/surgeon_override; LLM/disabled actor modes use the public
        # /surgeon/request topic directly.
        mute_msg = String()
        mute_msg.data = f"mute_actor:{self._manual_override_actor_mute_sec:.1f}"
        self._control_pub.publish(mute_msg)

        def publish_manual_request(msg: SurgeonRequest) -> None:
            if self._surgeon_actor_mode == "rule":
                self._override_pub.publish(msg)
            else:
                self._direct_request_pub.publish(msg)

        if request.clear_pending_requests:
            cancel = SurgeonRequest()
            cancel.stamp = self.get_clock().now().to_msg()
            cancel.event_type = "cancel_request"
            cancel.override = True
            cancel.note = "clear pending requests before manual override"
            publish_manual_request(cancel)

        msg = SurgeonRequest()
        msg.stamp = self.get_clock().now().to_msg()
        msg.event_type = event_type
        msg.requested_tool = canonical_tool
        msg.voice_text = request.voice_text
        msg.ready_for_handover = bool(request.ready_for_handover)
        msg.ready_for_retrieval = bool(request.ready_for_retrieval)
        msg.override = True
        msg.note = (
            f"simulation_manager {source}_override "
            f"actor_muted_sec={self._manual_override_actor_mute_sec:.1f}"
        )
        publish_manual_request(msg)

        response.success = True
        response.message = (
            f"{source} surgeon override published; autonomous actor muted for "
            f"{self._manual_override_actor_mute_sec:.1f}s"
        )
        return response


def main() -> None:
    rclpy.init()
    node = SimulationManagerNode()
    executor = MultiThreadedExecutor(num_threads=4)
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
