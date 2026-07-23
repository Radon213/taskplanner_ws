"""Simulation manager service surface for bundle and runtime control."""

from __future__ import annotations

from pathlib import Path
import socket
import threading
import time

from ament_index_python.packages import get_package_share_directory
from btops_interfaces.srv import CommandExecutor, GetRuntimeState, StartBehavior
from procedure_spec import load_bundle
import rclpy
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.parameter import Parameter
from rclpy.parameter_client import AsyncParameterClient
from std_msgs.msg import String
from surgical_msgs.msg import SimulationState, SurgeonActorEvent, SurgeonRequest
from surgical_msgs.srv import ControlSimulation, InjectSurgeonOverride, SelectSimulationBundle


RESOURCE_ID = "tree/taskplanner_bt_trees::surgical_assist_v1::TaskplannerAssistDemo"
ENTRY_POINT = "TaskplannerAssistDemo"
NODE_MANIFESTS = ["taskplanner_bt_nodes::taskplanner_bt_nodes"]
ALLOWED_OVERRIDE_EVENTS = {"request_tool", "voice_request", "return_tool", "cancel_request"}
TOOL_REQUIRED_OVERRIDE_EVENTS = {"request_tool", "voice_request", "return_tool"}


class SimulationManagerNode(Node):
    def __init__(self) -> None:
        super().__init__("simulation_manager")
        default_root = Path(get_package_share_directory("procedure_spec")) / "specs"
        self.declare_parameter("spec_root", str(default_root))
        self.declare_parameter("default_bundle", "thyroidectomy")
        self.declare_parameter("executor_name", "tree_executor")
        self.declare_parameter("tick_rate_hz", 0.1)
        self.declare_parameter("groot2_port", 0)
        self.declare_parameter("manual_override_actor_mute_sec", 300.0)

        self._spec_root = Path(str(self.get_parameter("spec_root").value))
        self._active_bundle = str(self.get_parameter("default_bundle").value)
        self._active_spec_dir, self._active_spec = self._load_spec_for_bundle(self._active_bundle)
        self._executor_name = str(self.get_parameter("executor_name").value)
        self._tick_rate_hz = float(self.get_parameter("tick_rate_hz").value)
        self._groot2_port = int(self.get_parameter("groot2_port").value)
        self._manual_override_actor_mute_sec = float(self.get_parameter("manual_override_actor_mute_sec").value)
        self._running = False
        self._execution_state = "idle"
        self._bundle_dirty = False
        self._operation_name = ""
        self._operation_cancel = threading.Event()
        self._operation_lock = threading.Lock()
        self._completion_terminate_started = False
        self._latest_state: SimulationState | None = None
        self._latest_state_lock = threading.Lock()
        self._callback_group = ReentrantCallbackGroup()

        self._control_pub = self.create_publisher(String, "/simulation/control_state", 10)
        self._override_pub = self.create_publisher(SurgeonRequest, "/simulation/surgeon_override", 10)
        self._direct_request_pub = self.create_publisher(SurgeonRequest, "/surgeon/request", 10)
        self._direct_actor_event_pub = self.create_publisher(SurgeonActorEvent, "/surgeon/actor_event", 10)
        self.create_subscription(
            SimulationState,
            "/simulation/state",
            self._on_simulation_state,
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
        self._parameter_clients = {
            "/mock_vlm_node": AsyncParameterClient(
                self, "/mock_vlm_node", callback_group=self._callback_group
            ),
            "/real_vlm_node": AsyncParameterClient(
                self, "/real_vlm_node", callback_group=self._callback_group
            ),
            "/no_image_camera": AsyncParameterClient(
                self, "/no_image_camera", callback_group=self._callback_group
            ),
            "/phase_estimator": AsyncParameterClient(
                self, "/phase_estimator", callback_group=self._callback_group
            ),
            "/or_digital_twin": AsyncParameterClient(
                self, "/or_digital_twin", callback_group=self._callback_group
            ),
            "/surgeon_actor": AsyncParameterClient(
                self, "/surgeon_actor", callback_group=self._callback_group
            ),
        }

        self.create_service(
            SelectSimulationBundle,
            "/simulation/select_bundle",
            self._handle_select_bundle,
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
            self._handle_override,
            callback_group=self._callback_group,
        )

    def _wait_future(self, future, timeout_sec: float = 10.0):
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            if future.done():
                return future.result()
            time.sleep(0.05)
        raise TimeoutError("Timed out waiting for async operation to complete.")

    def _load_spec_for_bundle(self, bundle_name: str):
        bundle_dir = self._spec_root / bundle_name
        if not bundle_dir.is_dir():
            raise FileNotFoundError(f"bundle '{bundle_name}' not found under {self._spec_root}")
        return bundle_dir, load_bundle(bundle_dir)

    def _spec_dir_for_bundle(self, bundle_name: str) -> Path:
        spec_dir, _ = self._load_spec_for_bundle(bundle_name)
        return spec_dir

    def _publish_control(self, command: str, repeat_count: int = 2) -> None:
        deadline = time.time() + 2.0
        while self._control_pub.get_subscription_count() < 1 and time.time() < deadline:
            time.sleep(0.05)
        msg = String()
        msg.data = command
        for _ in range(max(1, repeat_count)):
            self._control_pub.publish(msg)
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
        if msg.execution_state == "completed":
            should_terminate = self._running or self._execution_state != "completed"
            self._running = False
            self._execution_state = "completed"
            if should_terminate and not self._completion_terminate_started:
                self._completion_terminate_started = True
                thread = threading.Thread(
                    target=self._terminate_executor_after_completion,
                    name="simulation-completion-terminate",
                    daemon=True,
                )
                thread.start()

    def _terminate_executor_after_completion(self) -> None:
        try:
            self._command_executor("terminate")
            self._wait_for_executor_idle(timeout_sec=8.0)
        except Exception as exc:
            self.get_logger().warn(f"failed to terminate executor after completion: {exc}")
        finally:
            self._running = False
            self._execution_state = "completed"
            self._completion_terminate_started = False

    def _wait_for_simulation_state(
        self,
        predicate,
        timeout_sec: float,
        description: str,
        *,
        warn: bool = True,
    ) -> bool:
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            with self._latest_state_lock:
                state = self._latest_state
            if state is not None:
                try:
                    if predicate(state):
                        return True
                except Exception:
                    pass
            time.sleep(0.1)
        if warn:
            self.get_logger().warn(f"Timed out waiting for {description}.")
        return False

    def _raise_if_operation_cancelled(self) -> None:
        if self._operation_cancel.is_set():
            raise RuntimeError("operation interrupted by newer control command")

    @staticmethod
    def _state_stamp_key(state: SimulationState | None) -> tuple[int, int] | None:
        if state is None:
            return None
        return (int(state.stamp.sec), int(state.stamp.nanosec))

    def _publish_control_until(self, command: str, predicate, timeout_sec: float, description: str) -> bool:
        deadline = time.time() + timeout_sec
        attempts = 3
        for attempt in range(attempts):
            if self._operation_cancel.is_set():
                return False
            with self._latest_state_lock:
                previous_stamp = self._state_stamp_key(self._latest_state)
            self._publish_control(command, repeat_count=2)
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            wait_slice = max(0.5, remaining / max(1, attempts - attempt))
            if self._wait_for_simulation_state(
                lambda state: self._state_stamp_key(state) != previous_stamp and predicate(state),
                timeout_sec=min(wait_slice, remaining),
                description=description,
                warn=False,
            ):
                return True
        self.get_logger().warn(f"Timed out waiting for {description}.")
        return False

    @staticmethod
    def _all_instruments_home(state: SimulationState) -> bool:
        if not state.instrument_states:
            return False
        for instrument in state.instrument_states:
            home_location_id = str(instrument.home_location_id)
            home_location_type = str(instrument.home_location_type)
            if home_location_id and str(instrument.location_id) != home_location_id:
                return False
            if home_location_type and str(instrument.location_type) != home_location_type:
                return False
        return True

    def _set_spec_dir_on_runtime(self, spec_dir: Path) -> None:
        optional_clients = {
            "/mock_vlm_node",
            "/real_vlm_node",
            "/no_image_camera",
            "/phase_estimator",
            "/surgeon_actor",
        }
        required_clients = [
            (name, client) for name, client in self._parameter_clients.items() if name not in optional_clients
        ]
        optional_parameter_clients = [
            (name, client) for name, client in self._parameter_clients.items() if name in optional_clients
        ]
        updated_clients: set[str] = set()

        def update_client(name, client, wait_sec: float) -> bool:
            ready = client.services_are_ready()
            if not ready and wait_sec > 0:
                ready = client.wait_for_services(timeout_sec=wait_sec)
            if ready:
                future = client.set_parameters([Parameter(name="spec_dir", value=str(spec_dir))])
                response = self._wait_future(future, timeout_sec=10.0)
                results = getattr(response, "results", response or [])
                failed = [
                    result
                    for result in results
                    if not bool(getattr(result, "successful", False))
                ]
                if failed:
                    reason = "; ".join(str(getattr(result, "reason", "")) for result in failed).strip()
                    raise RuntimeError(f"spec update rejected by {name}: {reason or 'unknown reason'}")
                updated_clients.add(name)
                return True
            return False

        deadline = time.time() + 8.0
        pending_clients = required_clients
        while pending_clients and time.time() < deadline:
            still_pending = []
            for name, client in pending_clients:
                if not update_client(name, client, wait_sec=0.25):
                    still_pending.append((name, client))
            pending_clients = still_pending
        if pending_clients:
            missing = ", ".join(name for name, _ in pending_clients)
            raise TimeoutError(f"parameter services not ready for: {missing}")

        for name, client in optional_parameter_clients:
            try:
                if not update_client(name, client, wait_sec=0.05):
                    self.get_logger().info(f"optional parameter service unavailable, skipping spec update for {name}")
            except Exception as exc:
                self.get_logger().warn(f"optional spec update failed for {name}: {exc}")
        if not updated_clients.intersection({"/mock_vlm_node", "/real_vlm_node"}):
            self.get_logger().info("no VLM parameter service was available during bundle switch; continuing without direct spec update")

    def _start_behavior(self, clear_blackboard: bool) -> tuple[bool, str]:
        if not self._start_client.wait_for_service(timeout_sec=5.0):
            return False, "btops start_behavior service is unavailable"
        last_message = "btops start_behavior returned no response"
        retryable_markers = (
            "/tree_executor/set_parameters",
            "previous one is still busy",
            "currently executing",
            "parameter is not allowed to change while tree executor is running",
        )
        for attempt in range(5):
            self._raise_if_operation_cancelled()
            self._wait_for_executor_idle(timeout_sec=3.0)
            requested_groot2_port = self._reserve_groot2_port()
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
            request.enable_monitoring = True
            request.tick_rate_hz = float(self._tick_rate_hz)
            request.requested_groot2_port = int(requested_groot2_port)
            request.parameter_assignments = []
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
                if self._wait_for_executor_running(timeout_sec=4.0):
                    return True, "simulation started after delayed start_behavior response"
                time.sleep(0.25 * (attempt + 1))
                continue
            if response.success:
                return True, str(response.message)
            last_message = str(response.message)
            lowered_message = last_message.lower()
            if any(marker in lowered_message for marker in retryable_markers):
                if self._wait_for_executor_running(timeout_sec=4.0):
                    return True, last_message
                self._command_executor("terminate")
                self._wait_for_executor_idle(timeout_sec=8.0)
                time.sleep(0.35 * (attempt + 1))
                continue
            return False, last_message
        return False, last_message

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

    def _get_runtime_state(self) -> tuple[bool, str]:
        if not self._runtime_client.wait_for_service(timeout_sec=5.0):
            return False, "unknown"
        request = GetRuntimeState.Request()
        request.executor_name = self._executor_name
        future = self._runtime_client.call_async(request)
        response = self._wait_future(future, timeout_sec=10.0)
        if response is None or not response.success:
            return False, "unknown"
        return True, str(response.snapshot.execution_state).lower()

    def _wait_for_executor_idle(self, timeout_sec: float = 8.0) -> bool:
        deadline = time.time() + timeout_sec
        idle_states = {"", "idle", "terminated", "halted", "unknown"}
        while time.time() < deadline:
            try:
                success, state = self._get_runtime_state()
                if not success or state in idle_states:
                    return True
            except Exception:
                return True
            time.sleep(0.15)
        return False

    def _wait_for_executor_running(self, timeout_sec: float = 10.0) -> bool:
        deadline = time.time() + timeout_sec
        # AutoAPMS reports a successful StartTreeExecutor dispatch as
        # "accepted" and later often "succeeded" while the tree keeps ticking.
        # Treat those as live executor states so startup and bundle restart do
        # not burn the full timeout waiting for a state string that this backend
        # does not emit.
        running_states = {"running", "active", "executing", "starting", "accepted", "succeeded"}
        while time.time() < deadline:
            try:
                success, state = self._get_runtime_state()
                if success and state in running_states:
                    return True
            except Exception:
                pass
            time.sleep(0.15)
        return False

    def _prepare_executor_for_restart(self) -> None:
        if not self._running and self._execution_state in {"idle", "terminated", "halted"}:
            try:
                success, state = self._get_runtime_state()
                if not success or state in {"", "idle", "terminated", "halted", "unknown"}:
                    return
            except Exception:
                return
        try:
            self._command_executor("terminate")
            self._wait_for_executor_idle(timeout_sec=8.0)
        except Exception:
            # It is fine if the executor was not running yet.
            pass

    def _reset_digital_twin_to_idle(self) -> None:
        if not self._publish_control_until(
            "reset",
            lambda state: (
                state.active_bundle == self._active_bundle
                and (not state.running)
                and state.execution_state == "idle"
                and self._all_instruments_home(state)
            ),
            timeout_sec=8.0,
            description="idle digital twin frame after reset",
        ):
            raise RuntimeError("digital twin did not publish an idle home frame after reset")
        self._set_idle_state()

    def _stop_digital_twin_to_halted(self) -> None:
        if not self._publish_control_until(
            "stop",
            lambda state: (
                state.active_bundle == self._active_bundle
                and (not state.running)
                and state.execution_state == "halted"
            ),
            timeout_sec=5.0,
            description="halted digital twin frame after stop",
        ):
            self.get_logger().warn("digital twin did not publish a halted frame after stop")

    def _begin_operation(self, name: str) -> bool:
        with self._operation_lock:
            if self._operation_name:
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
            return False, f"{self._operation_name} already in progress"

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
            return False, f"{self._operation_name} already in progress"
        try:
            message = target()
            return True, str(message or f"{name} completed")
        except Exception as exc:
            self.get_logger().error(f"{name} operation failed: {exc}")
            return False, str(exc)
        finally:
            self._finish_operation(name)

    def _start_sequence(self, start_phase_id: str = "", *, prepare_executor: bool = True) -> str:
        start_phase_id = self._normalize_start_phase(start_phase_id)
        target_phase_id = start_phase_id or self._active_spec.default_phase_id
        self._running = False
        self._execution_state = "starting"
        self._completion_terminate_started = False
        if prepare_executor:
            self._prepare_executor_for_restart()
        self._raise_if_operation_cancelled()
        if not self._publish_control_until(
            "reset",
            lambda state: (
                state.active_bundle == self._active_bundle
                and (not state.running)
                and state.execution_state == "idle"
                and self._all_instruments_home(state)
            ),
            timeout_sec=8.0,
            description="initial idle digital twin frame",
        ):
            self._raise_if_operation_cancelled()
            raise RuntimeError("digital twin did not publish an idle home frame after reset")
        self._raise_if_operation_cancelled()
        if not self._publish_control_until(
            self._control_with_phase("start_runtime", start_phase_id),
            lambda state: (
                state.active_bundle == self._active_bundle
                and state.running
                and state.execution_state == "running"
                and state.filtered_phase == target_phase_id
                and self._all_instruments_home(state)
            ),
            timeout_sec=8.0,
            description=f"initial home running digital twin frame at {target_phase_id}",
        ):
            self._raise_if_operation_cancelled()
            raise RuntimeError("digital twin did not enter running state before BT start")
        self._raise_if_operation_cancelled()
        # Actors can begin observing the prepared running frame while BTops is
        # starting. Requests are queued in the twin, so this reduces the
        # perceived wait for the first tool without letting BT run before the
        # digital twin is initialized.
        self._publish_control(self._control_with_phase("start_actors", start_phase_id))
        self._raise_if_operation_cancelled()
        success, message = self._start_behavior(clear_blackboard=True)
        if success:
            self._raise_if_operation_cancelled()
            self._wait_for_executor_running(timeout_sec=10.0)
            self._raise_if_operation_cancelled()
            self._running = True
            self._execution_state = "running"
            self._bundle_dirty = False
            self.get_logger().info(message or "simulation started")
            phase_suffix = f" from {target_phase_id}" if start_phase_id else ""
            return message or f"simulation running on {self._active_bundle}{phase_suffix}"
        self._publish_control("stop")
        self._set_idle_state()
        raise RuntimeError(message or "failed to start simulation")

    def _interrupt_start_sequence(self, command: str) -> str:
        self._operation_cancel.set()
        self._running = False
        self._execution_state = "resetting" if command == "reset" else "stopping"
        self._publish_control("reset" if command == "reset" else "stop")
        try:
            self._command_executor("terminate")
            self._wait_for_executor_idle(timeout_sec=6.0)
        except Exception as exc:
            self.get_logger().warn(f"failed to terminate executor while interrupting start: {exc}")
        if command == "reset":
            self._publish_control("reset")
            self._set_idle_state()
            return "start interrupted; simulation runtime reset to idle"
        self._execution_state = "halted"
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
        self._stop_digital_twin_to_halted()
        success, message = self._command_executor("terminate")
        self._wait_for_executor_idle(timeout_sec=8.0)
        if success:
            self._execution_state = "halted"
            self.get_logger().info(message or "simulation stopped")
            return message or "simulation stopped"
        self._execution_state = "halted"
        raise RuntimeError(message or "failed to stop simulation")

    def _reset_sequence(self) -> str:
        self._running = False
        self._execution_state = "resetting"
        self._completion_terminate_started = False
        self._prepare_executor_for_restart()
        self._reset_digital_twin_to_idle()
        return "simulation runtime reset to idle"

    def _set_idle_state(self) -> None:
        self._running = False
        self._execution_state = "idle"

    def _handle_select_bundle(self, request, response):
        try:
            interrupted_start = False
            if self._operation_name == "start":
                if not request.restart_if_running:
                    response.success = False
                    response.message = "cannot switch bundle while simulation is starting without restart_if_running=true"
                    response.active_bundle = self._active_bundle
                    response.spec_dir = str(self._active_spec_dir)
                    return response
                self._interrupt_start_sequence("reset")
                if not self._wait_for_operation_clear(timeout_sec=12.0):
                    response.success = False
                    response.message = "start operation is still stopping; retry bundle switch"
                    response.active_bundle = self._active_bundle
                    response.spec_dir = str(self._active_spec_dir)
                    return response
                interrupted_start = True
            elif self._operation_name:
                if not self._wait_for_operation_clear(timeout_sec=25.0):
                    response.success = False
                    response.message = f"{self._operation_name} already in progress"
                    response.active_bundle = self._active_bundle
                    response.spec_dir = str(self._active_spec_dir)
                    return response

            was_running = interrupted_start or self._running or self._execution_state in {"starting", "running", "paused"}
            if was_running and not request.restart_if_running:
                response.success = False
                response.message = "cannot switch bundle while simulation is running without restart_if_running=true"
                response.active_bundle = self._active_bundle
                response.spec_dir = str(self._active_spec_dir)
                return response

            spec_dir, spec = self._load_spec_for_bundle(request.bundle_name)
            if was_running and request.restart_if_running:
                self._prepare_executor_for_restart()
            self._set_spec_dir_on_runtime(spec_dir)
            self._active_bundle = request.bundle_name
            self._active_spec_dir = spec_dir
            self._active_spec = spec
            self._bundle_dirty = True
            if was_running and request.restart_if_running:
                success, message = self._run_sync(
                    "bundle-restart",
                    lambda: self._start_sequence(prepare_executor=False),
                )
                response.success = success
                response.message = message
            else:
                self._prepare_executor_for_restart()
                self._reset_digital_twin_to_idle()
                response.success = True
                response.message = f"active bundle set to {request.bundle_name}"
            response.active_bundle = self._active_bundle
            response.spec_dir = str(spec_dir)
            return response
        except Exception as exc:
            response.success = False
            response.message = str(exc)
            response.active_bundle = self._active_bundle
            response.spec_dir = ""
            return response

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
                if command in {"pause", "resume"}:
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
                    self._running = False
                    self._execution_state = "starting"
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
                self._bundle_dirty = False
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

    def _handle_override(self, request, response):
        event_type = request.event_type.strip()
        if event_type not in ALLOWED_OVERRIDE_EVENTS:
            response.success = False
            response.message = (
                f"unsupported surgeon override event_type '{request.event_type}'; "
                f"expected one of {sorted(ALLOWED_OVERRIDE_EVENTS)}"
            )
            return response
        if self._execution_state == "paused":
            response.success = False
            response.message = "simulation paused; resume before injecting surgeon override"
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

        # Manual override test path:
        # 1) mute the autonomous LLM actor briefly
        # 2) clear pending request queue when requested
        # 3) publish the manual cue directly to /surgeon/request so the digital twin sees it
        # 4) keep /simulation/surgeon_override and /surgeon/actor_event for observability
        mute_msg = String()
        mute_msg.data = f"mute_actor:{self._manual_override_actor_mute_sec:.1f}"
        self._control_pub.publish(mute_msg)

        if request.clear_pending_requests:
            cancel = SurgeonRequest()
            cancel.stamp = self.get_clock().now().to_msg()
            cancel.event_type = "cancel_request"
            cancel.override = True
            cancel.note = "clear pending requests before manual override"
            self._override_pub.publish(cancel)
            self._direct_request_pub.publish(cancel)

        msg = SurgeonRequest()
        msg.stamp = self.get_clock().now().to_msg()
        msg.event_type = event_type
        msg.requested_tool = canonical_tool
        msg.voice_text = request.voice_text
        msg.ready_for_handover = bool(request.ready_for_handover)
        msg.ready_for_retrieval = bool(request.ready_for_retrieval)
        msg.override = True
        msg.note = f"simulation_manager manual_override actor_muted_sec={self._manual_override_actor_mute_sec:.1f}"
        self._override_pub.publish(msg)
        self._direct_request_pub.publish(msg)

        actor_event = SurgeonActorEvent()
        actor_event.stamp = msg.stamp
        actor_event.event_type = event_type
        actor_event.tool_id = canonical_tool
        actor_event.phase_id = ""
        actor_event.voice_text = request.voice_text
        actor_event.note = msg.note
        actor_event.ready_for_handover = bool(request.ready_for_handover)
        actor_event.ready_for_retrieval = bool(request.ready_for_retrieval)
        actor_event.override = True
        self._direct_actor_event_pub.publish(actor_event)

        response.success = True
        response.message = f"manual surgeon override published; autonomous actor muted for {self._manual_override_actor_mute_sec:.1f}s"
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
