"""ROS-independent, single-worker execution of validated retraction commands."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
from types import MappingProxyType
import threading
from typing import Any, Callable, Mapping, Protocol, Sequence, TYPE_CHECKING

from .adapters import (
    AdapterError,
    Aft200AdapterProtocol,
    ForceTorqueSample,
    IndyDcp3AdapterProtocol,
    JointStateSample,
    OwnershipError,
    SingleOwnerGuard,
)
from .adapters.clock import Clock, SystemClock
from .algorithms.force_jog import ForceJogPlan, plan_force_jog
from .command_models import RetractionControlError
from .teaching_session import (
    SessionValidationError,
    TeachingSession,
    TeachingSessionError,
    TeachingSessionMetadata,
    TeachingSessionRecorder,
    TeachingSessionRepository,
)
from .target_planner import (
    CallableTargetPlanner,
    LastSampleTargetPlanner,
    TargetPlanner,
    TargetPlannerIdentity,
)

if TYPE_CHECKING:
    from .command_models import CommandRequest
    from .profile_loader import ExecutionProfile


COMMAND_START_DIRECT_TEACH = 1
COMMAND_FINISH_DIRECT_TEACH = 2
COMMAND_START_RETRACTION = 3
COMMAND_ADJUST_RETRACTION = 4
COMMAND_CHANGE_TOOL = 5
COMMAND_STOP_RETRACTION = 6
TARGET_NONE = 0
TARGET_LEFT = 1
TARGET_RIGHT = 2


class ExecutorState(str, Enum):
    UNINITIALIZED = "uninitialized"
    IDLE = "idle"
    DIRECT_TEACHING = "direct_teaching"
    TAUGHT_READY = "taught_ready"
    RETRACTING = "retracting"
    FAULT = "fault"
    SHUTDOWN = "shutdown"


class ExecutionStatus(str, Enum):
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    FAILED = "failed"
    CANCELED = "canceled"


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    """Physical execution result, distinct from ROS Service admission."""

    status: ExecutionStatus
    code: str
    message: str
    command_id: str
    command: int
    started_at_ns: int
    finished_at_ns: int
    executor_state: ExecutorState
    affected_arm_id: str = ""
    target_side: int = TARGET_NONE
    session_id: str = ""
    cleanup_errors: tuple[str, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "cleanup_errors", tuple(self.cleanup_errors))
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))

    @property
    def success(self) -> bool:
        return self.status is ExecutionStatus.SUCCEEDED

    @property
    def result_code(self) -> str:
        """Compatibility with the ROS-independent ``CommandWorker`` report."""

        return self.code

    @property
    def canceled(self) -> bool:
        return self.status is ExecutionStatus.CANCELED

    @property
    def terminal(self) -> bool:
        return True

    @property
    def operation(self) -> int:
        return self.command


@dataclass(frozen=True, slots=True)
class ExecutorHealth:
    checked_at_ns: int
    started: bool
    owner_acquired: bool
    robot_connected: bool
    sensor_available: bool
    controller_fault_code: str = ""
    stale_sensor_ids: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def healthy(self) -> bool:
        return (
            self.started
            and self.robot_connected
            and self.sensor_available
            and not self.controller_fault_code
            and not self.errors
        )


@dataclass(frozen=True, slots=True)
class ExecutionAdmission:
    """Side-effect-free executor preflight for a Service admission path."""

    accepted: bool
    code: str
    message: str
    executor_state: ExecutorState


class StopSignal(Protocol):
    def is_set(self) -> bool: ...


class _ExecutionRejected(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _ExecutionCanceled(RuntimeError):
    pass


def _value(obj: object, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _enum_int(value: object, field_name: str) -> int:
    try:
        return int(value)  # IntEnum and wire integers
    except (TypeError, ValueError) as exc:
        raise _ExecutionRejected(
            "invalid_request", f"{field_name} must be an integer enum value"
        ) from exc


def _error_code(error: BaseException, fallback: str) -> str:
    value = getattr(error, "code", fallback)
    return str(getattr(value, "value", value))


class CommandExecutor:
    """Execute one validated command at a time against injected adapters.

    Construction performs no hardware I/O.  :meth:`start` (or the first
    :meth:`execute`) acquires the optional process lock, starts the receive-only
    sensor reader, checks controller state, and restores only IDLE or
    TAUGHT_READY from checksummed storage.
    """

    def __init__(
        self,
        *,
        robot: IndyDcp3AdapterProtocol,
        force_sensor: Aft200AdapterProtocol,
        profile: ExecutionProfile | object,
        sessions: TeachingSessionRepository,
        robot_id: str,
        controller_id: str,
        source_revision: str,
        force_freshness_timeout_ns: int | None = None,
        stop_policy: str | None = None,
        clock: Clock | None = None,
        owner_guard: SingleOwnerGuard | None = None,
        calibration_metadata: Mapping[str, Any] | None = None,
        execution_mode: str = "fake",
        target_planner: TargetPlanner | None = None,
        target_calculator: Callable[
            [tuple[JointStateSample, ...], tuple[ForceTorqueSample, ...]],
            tuple[Mapping[str, Sequence[float]], Mapping[str, Sequence[float]]],
        ]
        | None = None,
        target_planner_identity: TargetPlannerIdentity | None = None,
    ) -> None:
        if force_freshness_timeout_ns is None:
            force_freshness_timeout_ns = _value(
                profile, "force_freshness_timeout_ns", None
            )
        if force_freshness_timeout_ns is None or int(force_freshness_timeout_ns) <= 0:
            raise ValueError("force_freshness_timeout_ns must be positive")
        if stop_policy is None:
            stop_policy = _value(profile, "stop_policy", "")
        normalized_stop_policy = str(stop_policy).strip().lower()
        if normalized_stop_policy not in {"stop", "hold"}:
            raise ValueError("stop_policy must be explicitly set to 'stop' or 'hold'")
        for name, value in (
            ("robot_id", robot_id),
            ("controller_id", controller_id),
            ("source_revision", source_revision),
        ):
            if not str(value).strip():
                raise ValueError(f"{name} must not be empty")
        self.robot = robot
        self.force_sensor = force_sensor
        self.profile = profile
        self.sessions = sessions
        self.robot_id = str(robot_id).strip()
        self.controller_id = str(controller_id).strip()
        self.source_revision = str(source_revision).strip()
        self.force_freshness_timeout_ns = int(force_freshness_timeout_ns)
        self.stop_policy = normalized_stop_policy
        self.clock = clock or SystemClock()
        self.owner_guard = owner_guard
        self.calibration_metadata = dict(calibration_metadata or {})
        normalized_mode = str(execution_mode).strip().lower()
        if normalized_mode not in {"fake", "shadow", "hardware"}:
            raise ValueError("execution_mode must be fake, shadow, or hardware")
        if target_planner is not None and target_calculator is not None:
            raise ValueError("provide target_planner or target_calculator, not both")
        if target_calculator is not None:
            if target_planner_identity is None:
                raise ValueError(
                    "an injected target_calculator requires a checksum-bound identity"
                )
            target_planner = CallableTargetPlanner(
                target_planner_identity, target_calculator
            )
        elif target_planner_identity is not None:
            raise ValueError(
                "target_planner_identity is only valid with target_calculator"
            )
        self.execution_mode = normalized_mode
        self.target_planner = target_planner or LastSampleTargetPlanner()
        if self.execution_mode == "hardware" and self.target_planner.identity.synthetic:
            raise ValueError("synthetic target planner is forbidden in hardware mode")

        self._execute_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._stop_requested = threading.Event()
        self._state = ExecutorState.UNINITIALIZED
        self._active_command_id = ""
        self._active_operation = ""
        self._last_error_code = ""
        self._last_error_message = ""
        self._recorder: TeachingSessionRecorder | None = None
        self._latest_session_id = ""
        self._cumulative_jog_mm: dict[str, float] = {}
        self._started = False
        self._stop_followup_required = False

    @property
    def state(self) -> ExecutorState:
        with self._state_lock:
            return self._state

    @property
    def active_command_id(self) -> str:
        with self._state_lock:
            return self._active_command_id

    @property
    def active_operation(self) -> str:
        with self._state_lock:
            return self._active_operation

    @property
    def last_error(self) -> tuple[str, str]:
        with self._state_lock:
            return self._last_error_code, self._last_error_message

    @property
    def started(self) -> bool:
        return self._started

    def request_stop(self) -> None:
        """Signal cooperative cancellation; a queued STOP command performs I/O."""

        self._stop_requested.set()

    def start(self) -> None:
        """Acquire authority and establish a read-only sensor/controller baseline."""

        if self._started:
            return
        if self.state is ExecutorState.SHUTDOWN:
            raise RuntimeError("a shut down executor cannot be restarted")
        guard_acquired = False
        try:
            if self.owner_guard is not None:
                self.owner_guard.acquire()
                guard_acquired = True
            self.force_sensor.start()
            controller = self.robot.controller_state()
            if not controller.connected:
                raise AdapterError(
                    "controller_disconnected",
                    "robot controller did not confirm a connection",
                    component="indy_dcp3",
                    operation="controller_state",
                    retryable=True,
                )
            if controller.fault_code:
                raise AdapterError(
                    "controller_fault",
                    f"robot controller reports fault {controller.fault_code}",
                    component="indy_dcp3",
                    operation="controller_state",
                )
            if controller.direct_teaching or controller.motion_active:
                raise AdapterError(
                    "unsafe_startup_state",
                    "controller is moving or already in direct-teaching mode",
                    component="indy_dcp3",
                    operation="controller_state",
                )
            try:
                latest = self.sessions.latest_valid(
                    expected_profile_name=self._profile_name,
                    expected_profile_checksum=self._profile_checksum,
                )
            except TeachingSessionError:
                latest = None
            with self._state_lock:
                self._latest_session_id = latest.session_id if latest else ""
                self._state = (
                    ExecutorState.TAUGHT_READY if latest else ExecutorState.IDLE
                )
                self._last_error_code = ""
                self._last_error_message = ""
            self._started = True
        except BaseException:
            with self._state_lock:
                self._state = ExecutorState.FAULT
            try:
                self.force_sensor.stop()
            except BaseException:
                pass
            if guard_acquired and self.owner_guard is not None:
                self.owner_guard.release()
            raise

    @property
    def _profile_name(self) -> str:
        return str(_value(self.profile, "name", "")).strip()

    @property
    def _profile_version(self) -> str:
        return str(_value(self.profile, "version", "")).strip()

    @property
    def _profile_checksum(self) -> str:
        return str(_value(self.profile, "checksum", "")).strip()

    def execute(
        self,
        request: CommandRequest | object,
        stop_requested: StopSignal | Callable[[], bool] | None = None,
    ) -> ExecutionOutcome:
        """Synchronously execute a previously admitted request.

        ``stop_requested`` is checked between bounded adapter operations and
        tool-change waypoints.  It is never interpreted as Service admission.
        """

        started_at_ns = self.clock.monotonic_ns()
        outer_request = request
        payload = _value(request, "payload", None)
        if payload is not None:
            request = payload
        command_id = str(
            _value(request, "command_id", _value(outer_request, "command_id", ""))
        ).strip()
        try:
            command = _enum_int(
                _value(request, "command", _value(outer_request, "command", None)),
                "command",
            )
            target_side = _enum_int(
                _value(request, "target_side", TARGET_NONE), "target_side"
            )
        except _ExecutionRejected as exc:
            return self._outcome(
                ExecutionStatus.REJECTED,
                exc.code,
                str(exc),
                command_id,
                0,
                started_at_ns,
            )
        if not command_id:
            return self._outcome(
                ExecutionStatus.REJECTED,
                "invalid_command_id",
                "command_id must not be empty",
                command_id,
                command,
                started_at_ns,
                target_side=target_side,
            )
        if not self._execute_lock.acquire(blocking=False):
            return self._outcome(
                ExecutionStatus.REJECTED,
                "executor_busy",
                "another physical command is already executing",
                command_id,
                command,
                started_at_ns,
                target_side=target_side,
            )

        affected_arm_id = ""
        session_id = ""
        details: dict[str, Any] = {}
        try:
            try:
                self.start()
            except OwnershipError as exc:
                return self._outcome(
                    ExecutionStatus.REJECTED,
                    "hardware_authority_busy",
                    str(exc),
                    command_id,
                    command,
                    started_at_ns,
                    target_side=target_side,
                )
            except AdapterError as exc:
                return self._failed_outcome(
                    exc,
                    command_id,
                    command,
                    started_at_ns,
                    target_side=target_side,
                )

            with self._state_lock:
                self._active_command_id = command_id
                self._active_operation = self._operation_name(command)

            self._check_admission_state(command)
            if command != COMMAND_STOP_RETRACTION and self._stop_is_set(stop_requested):
                raise _ExecutionCanceled("stop was requested before execution")
            self._require_motion_ready()

            if command == COMMAND_START_DIRECT_TEACH:
                session_id = self._start_direct_teach(request, stop_requested)
            elif command == COMMAND_FINISH_DIRECT_TEACH:
                session_id = self._finish_direct_teach(stop_requested)
            elif command == COMMAND_START_RETRACTION:
                session_id, affected_arm_id = self._start_retraction(
                    request, stop_requested
                )
            elif command == COMMAND_ADJUST_RETRACTION:
                affected_arm_id = str(
                    _value(self._resolve_side(target_side), "arm_id", "")
                )
                affected_arm_id, distance_mm = self._adjust_retraction(
                    request, stop_requested
                )
                details["distance_mm"] = distance_mm
            elif command == COMMAND_CHANGE_TOOL:
                affected_arm_id = ",".join(
                    dict.fromkeys(
                        str(_value(waypoint, "arm_id", ""))
                        for waypoint in tuple(
                            _value(self.profile, "tool_change_waypoints", ())
                        )
                        if str(_value(waypoint, "arm_id", ""))
                    )
                )
                affected_arm_id = self._change_tool(stop_requested)
            elif command == COMMAND_STOP_RETRACTION:
                cleanup_errors = self._stop_retraction()
                if cleanup_errors:
                    raise AdapterError(
                        "stop_cleanup_failed",
                        "; ".join(cleanup_errors),
                        component="executor",
                        operation="stop_retraction",
                    )
            else:
                raise _ExecutionRejected(
                    "invalid_command", f"unsupported command value: {command}"
                )

            with self._state_lock:
                self._last_error_code = ""
                self._last_error_message = ""
            return self._outcome(
                ExecutionStatus.SUCCEEDED,
                "completed",
                "physical command completed and was confirmed",
                command_id,
                command,
                started_at_ns,
                affected_arm_id=affected_arm_id,
                target_side=target_side,
                session_id=session_id,
                details=details,
            )
        except _ExecutionRejected as exc:
            return self._outcome(
                ExecutionStatus.REJECTED,
                exc.code,
                str(exc),
                command_id,
                command,
                started_at_ns,
                target_side=target_side,
            )
        except _ExecutionCanceled as exc:
            cleanup_errors = self._cleanup_after_failure(command)
            with self._state_lock:
                self._stop_followup_required = not cleanup_errors
                self._state = (
                    ExecutorState.FAULT
                    if cleanup_errors
                    else ExecutorState.TAUGHT_READY
                )
            return self._outcome(
                ExecutionStatus.CANCELED,
                "stop_requested",
                str(exc),
                command_id,
                command,
                started_at_ns,
                affected_arm_id=affected_arm_id,
                target_side=target_side,
                session_id=session_id,
                cleanup_errors=cleanup_errors,
            )
        except (AdapterError, TeachingSessionError) as exc:
            cleanup_errors = self._cleanup_after_failure(command)
            code = _error_code(exc, "execution_failed")
            with self._state_lock:
                self._state = ExecutorState.FAULT
                self._last_error_code = code
                self._last_error_message = str(exc)
            return self._outcome(
                ExecutionStatus.FAILED,
                code,
                str(exc),
                command_id,
                command,
                started_at_ns,
                affected_arm_id=affected_arm_id,
                target_side=target_side,
                session_id=session_id,
                cleanup_errors=cleanup_errors,
            )
        except Exception as exc:
            cleanup_errors = self._cleanup_after_failure(command)
            with self._state_lock:
                self._state = ExecutorState.FAULT
                self._last_error_code = "unexpected_execution_error"
                self._last_error_message = str(exc)
            return self._outcome(
                ExecutionStatus.FAILED,
                "unexpected_execution_error",
                str(exc),
                command_id,
                command,
                started_at_ns,
                affected_arm_id=affected_arm_id,
                target_side=target_side,
                session_id=session_id,
                cleanup_errors=cleanup_errors,
            )
        finally:
            with self._state_lock:
                self._active_command_id = ""
                self._active_operation = ""
            self._execute_lock.release()

    def check_admission(self, request: CommandRequest | object) -> ExecutionAdmission:
        """Check local state/profile constraints without hardware or state changes.

        The core state machine remains the authoritative admission fence.  This
        secondary preflight prevents queuing work that this executor cannot
        currently run; it intentionally does not call :meth:`start`, adapters,
        the session repository, or the clock.
        """

        outer_request = request
        payload = _value(request, "payload", None)
        if payload is not None:
            request = payload
        try:
            command = _enum_int(
                _value(request, "command", _value(outer_request, "command", None)),
                "command",
            )
            if self.state is ExecutorState.UNINITIALIZED:
                raise _ExecutionRejected(
                    "executor_not_started", "executor startup has not completed"
                )
            if self._execute_lock.locked() and command != COMMAND_STOP_RETRACTION:
                raise _ExecutionRejected(
                    "executor_busy", "another physical command is executing"
                )
            urgent_in_flight_stop = (
                command == COMMAND_STOP_RETRACTION
                and self._execute_lock.locked()
                and self.active_operation
                in {"start_retraction", "adjust_retraction", "change_tool"}
            )
            if not urgent_in_flight_stop:
                self._check_admission_state(command)
            self._require_motion_ready()
            if command == COMMAND_ADJUST_RETRACTION:
                self._plan_adjustment(request)
            if command == COMMAND_CHANGE_TOOL and not tuple(
                _value(self.profile, "tool_change_waypoints", ())
            ):
                raise _ExecutionRejected(
                    "tool_change_waypoints_missing",
                    "profile has no approved tool-change waypoint sequence",
                )
        except (_ExecutionRejected, TypeError, ValueError) as exc:
            return ExecutionAdmission(
                accepted=False,
                code=str(getattr(exc, "code", "invalid_request")),
                message=str(exc),
                executor_state=self.state,
            )
        return ExecutionAdmission(
            accepted=True,
            code="accepted",
            message="executor preflight accepted",
            executor_state=self.state,
        )

    def _outcome(
        self,
        status: ExecutionStatus,
        code: str,
        message: str,
        command_id: str,
        command: int,
        started_at_ns: int,
        *,
        affected_arm_id: str = "",
        target_side: int = TARGET_NONE,
        session_id: str = "",
        cleanup_errors: Sequence[str] = (),
        details: Mapping[str, Any] | None = None,
    ) -> ExecutionOutcome:
        outcome_details = {
            "execution_mode": self.execution_mode,
            "evidence_level": (
                "physical"
                if self.execution_mode == "hardware"
                else "record_only"
                if self.execution_mode == "shadow"
                else "synthetic"
            ),
            "physical_completion_confirmed": bool(
                self.execution_mode == "hardware"
                and status is ExecutionStatus.SUCCEEDED
            ),
        }
        outcome_details.update(details or {})
        return ExecutionOutcome(
            status=status,
            code=str(code),
            message=str(message),
            command_id=command_id,
            command=int(command),
            started_at_ns=int(started_at_ns),
            finished_at_ns=self.clock.monotonic_ns(),
            executor_state=self.state,
            affected_arm_id=str(affected_arm_id),
            target_side=int(target_side),
            session_id=str(session_id),
            cleanup_errors=tuple(cleanup_errors),
            details=outcome_details,
        )

    def _failed_outcome(
        self,
        exc: AdapterError,
        command_id: str,
        command: int,
        started_at_ns: int,
        *,
        target_side: int,
    ) -> ExecutionOutcome:
        with self._state_lock:
            self._state = ExecutorState.FAULT
            self._last_error_code = exc.code
            self._last_error_message = str(exc)
        return self._outcome(
            ExecutionStatus.FAILED,
            exc.code,
            str(exc),
            command_id,
            command,
            started_at_ns,
            target_side=target_side,
        )

    def _check_admission_state(self, command: int) -> None:
        state = self.state
        if command == COMMAND_STOP_RETRACTION and self._stop_followup_required:
            return
        allowed = {
            COMMAND_START_DIRECT_TEACH: {
                ExecutorState.IDLE,
            },
            COMMAND_FINISH_DIRECT_TEACH: {ExecutorState.DIRECT_TEACHING},
            COMMAND_START_RETRACTION: {ExecutorState.TAUGHT_READY},
            COMMAND_ADJUST_RETRACTION: {ExecutorState.RETRACTING},
            COMMAND_CHANGE_TOOL: {ExecutorState.RETRACTING},
            COMMAND_STOP_RETRACTION: {ExecutorState.RETRACTING},
        }
        if state not in allowed.get(command, set()):
            raise _ExecutionRejected(
                "invalid_state",
                f"command {command} cannot execute while executor is {state.value}",
            )

    def _require_motion_ready(self) -> None:
        check = getattr(self.profile, "require_motion_ready", None)
        if not callable(check):
            raise _ExecutionRejected(
                "profile_incompatible",
                "execution profile does not provide require_motion_ready()",
            )
        try:
            check()
        except Exception as exc:
            raise _ExecutionRejected(
                _error_code(exc, "profile_not_motion_ready"), str(exc)
            ) from exc
        if not self._profile_name or not self._profile_version or not self._profile_checksum:
            raise _ExecutionRejected(
                "profile_identity_missing",
                "execution profile name/version/checksum are required",
            )

    def _start_direct_teach(
        self,
        request: object,
        stop_requested: StopSignal | Callable[[], bool] | None,
    ) -> str:
        if self._recorder is not None:
            raise _ExecutionRejected("recording_busy", "a teaching recorder is active")
        command_id = str(_value(request, "command_id", ""))
        session_id = self._session_id_for_command(command_id)
        calibration = dict(self.calibration_metadata)
        calibration["approved"] = bool(
            _value(self.profile, "calibration_approved", False)
        )
        metadata = TeachingSessionMetadata(
            session_id=session_id,
            created_at_ns=self.clock.wall_time_ns(),
            profile_name=self._profile_name,
            profile_version=self._profile_version,
            profile_checksum=self._profile_checksum,
            robot_id=self.robot_id,
            controller_id=self.controller_id,
            source_revision=self.source_revision,
            target_planner=self.target_planner.identity.as_dict(),
            calibration=calibration,
        )
        recorder = TeachingSessionRecorder(metadata)
        self._recorder = recorder
        self.robot.set_friction_compensation(_value(self.profile, "teach_friction"))
        self.robot.set_custom_gain(False, None)
        self.robot.set_direct_teaching(True)
        if not self.robot.is_direct_teaching_enabled():
            raise AdapterError(
                "direct_teaching_not_confirmed",
                "controller did not confirm direct-teaching mode",
                component="indy_dcp3",
                operation="set_direct_teaching",
            )
        self.force_sensor.begin_recording(session_id)
        self._capture_teaching_samples(recorder, stop_requested)
        with self._state_lock:
            self._state = ExecutorState.DIRECT_TEACHING
        return session_id

    def capture_teaching_sample(
        self, stop_requested: StopSignal | Callable[[], bool] | None = None
    ) -> int:
        """Capture one sample per configured arm/sensor while teaching is active."""

        recorder = self._recorder
        if self.state is not ExecutorState.DIRECT_TEACHING or recorder is None:
            raise SessionValidationError(
                "not_recording", "no direct-teaching session is active"
            )
        return self._capture_teaching_samples(recorder, stop_requested)

    def _capture_teaching_samples(
        self,
        recorder: TeachingSessionRecorder,
        stop_requested: StopSignal | Callable[[], bool] | None,
    ) -> int:
        captured = 0
        for mapping in self._side_mappings():
            self._raise_if_stopped(stop_requested)
            joint = self.robot.read_joint_state(str(_value(mapping, "arm_id", "")))
            force = self.force_sensor.latest_sample(
                str(_value(mapping, "sensor_id", ""))
            )
            self._validate_force_freshness(force)
            recorder.record_pair(joint, force)
            captured += 1
        if captured == 0:
            raise _ExecutionRejected(
                "side_mapping_missing", "profile does not define any side mapping"
            )
        return captured

    def _finish_direct_teach(
        self, stop_requested: StopSignal | Callable[[], bool] | None
    ) -> str:
        recorder = self._recorder
        if recorder is None:
            raise _ExecutionRejected("not_recording", "no teaching recorder is active")
        self._capture_teaching_samples(recorder, stop_requested)
        self.robot.set_direct_teaching(False)
        if self.robot.is_direct_teaching_enabled():
            raise AdapterError(
                "direct_teaching_disable_not_confirmed",
                "controller still reports direct-teaching mode",
                component="indy_dcp3",
                operation="set_direct_teaching",
            )
        self.robot.set_friction_compensation(_value(self.profile, "normal_friction"))
        self.force_sensor.end_recording()
        target_plan = self.target_planner.plan(
            recorder.joint_samples, recorder.force_samples
        )
        if target_plan.identity != self.target_planner.identity:
            raise _ExecutionRejected(
                "target_planner_identity_changed",
                "target planner identity changed while teaching was active",
            )
        session = recorder.finish(
            completed_at_ns=self.clock.wall_time_ns(),
            target_joint_positions=target_plan.joint_positions,
            target_force_n=target_plan.force_targets_n,
            normally_completed=True,
        )
        self.sessions.save(session)
        self._recorder = None
        self._latest_session_id = session.session_id
        with self._state_lock:
            self._state = ExecutorState.TAUGHT_READY
        return session.session_id

    def _start_retraction(
        self,
        request: object,
        stop_requested: StopSignal | Callable[[], bool] | None,
    ) -> tuple[str, str]:
        requested_session_id = str(_value(request, "session_id", "")).strip()
        session = self._load_session(requested_session_id)
        for mapping in self._side_mappings():
            self._raise_if_stopped(stop_requested)
            force = self.force_sensor.latest_sample(
                str(_value(mapping, "sensor_id", ""))
            )
            self._validate_force_freshness(force)
        self.robot.set_custom_gain(True, _value(self.profile, "custom_gain"))
        affected: list[str] = []
        for arm_id, positions in sorted(session.target_joint_positions.items()):
            self._raise_if_stopped(stop_requested)
            self.robot.move_joint_positions(
                arm_id,
                positions,
                waypoint_name=f"teaching-session:{session.session_id}",
            )
            affected.append(arm_id)
        self._confirm_controller_settled()
        self._cumulative_jog_mm.clear()
        with self._state_lock:
            self._state = ExecutorState.RETRACTING
        return session.session_id, ",".join(affected)

    def _load_session(self, requested_session_id: str) -> TeachingSession:
        if requested_session_id:
            return self.sessions.load(
                requested_session_id,
                expected_profile_name=self._profile_name,
                expected_profile_checksum=self._profile_checksum,
                expected_target_planner_checksum=self.target_planner.identity.checksum,
            )
        if self._latest_session_id:
            return self.sessions.load(
                self._latest_session_id,
                expected_profile_name=self._profile_name,
                expected_profile_checksum=self._profile_checksum,
                expected_target_planner_checksum=self.target_planner.identity.checksum,
            )
        return self.sessions.latest_valid(
            expected_profile_name=self._profile_name,
            expected_profile_checksum=self._profile_checksum,
            expected_target_planner_checksum=self.target_planner.identity.checksum,
        )

    def _adjust_retraction(
        self,
        request: object,
        stop_requested: StopSignal | Callable[[], bool] | None,
    ) -> tuple[str, float]:
        plan = self._plan_adjustment(request)
        force = self.force_sensor.latest_sample(
            str(plan.sensor_id)
        )
        self._validate_force_freshness(force)
        self._raise_if_stopped(stop_requested)
        self.robot.jog_tcp(
            plan.arm_id,
            axis=plan.axis,
            distance_mm=plan.signed_distance_mm,
            frame=plan.frame,
        )
        self._confirm_controller_settled()
        self._cumulative_jog_mm[plan.arm_id] = plan.cumulative_distance_mm
        return plan.arm_id, plan.distance_mm

    def _plan_adjustment(self, request: object) -> ForceJogPlan:
        side = _enum_int(_value(request, "target_side", TARGET_NONE), "target_side")
        if side not in {TARGET_LEFT, TARGET_RIGHT}:
            raise _ExecutionRejected(
                "invalid_target_side", "adjust retraction requires LEFT or RIGHT"
            )
        mapping = self._resolve_side(side)
        arm_id = str(_value(mapping, "arm_id", ""))
        previous = self._cumulative_jog_mm.get(arm_id, 0.0)
        try:
            return plan_force_jog(
                self.profile,  # type: ignore[arg-type]
                side,
                _value(request, "distance_m"),
                previous_cumulative_mm=previous,
            )
        except RetractionControlError as exc:
            raise _ExecutionRejected(
                _error_code(exc, "invalid_adjustment"), str(exc)
            ) from exc

    def _change_tool(
        self, stop_requested: StopSignal | Callable[[], bool] | None
    ) -> str:
        waypoints = tuple(_value(self.profile, "tool_change_waypoints", ()))
        if not waypoints:
            raise _ExecutionRejected(
                "tool_change_waypoints_missing",
                "profile has no approved tool-change waypoint sequence",
            )
        affected: list[str] = []
        for index, waypoint in enumerate(waypoints):
            self._raise_if_stopped(stop_requested)
            name = str(_value(waypoint, "name", f"waypoint-{index + 1}"))
            arm_id = str(_value(waypoint, "arm_id", ""))
            positions = _value(
                waypoint,
                "joint_positions",
                _value(waypoint, "positions", None),
            )
            if not arm_id or positions is None:
                raise _ExecutionRejected(
                    "tool_change_waypoint_invalid",
                    f"tool-change waypoint {name!r} lacks arm_id or joint positions",
                )
            self.robot.move_joint_positions(
                arm_id, tuple(float(value) for value in positions), waypoint_name=name
            )
            if arm_id not in affected:
                affected.append(arm_id)
        self._confirm_controller_settled()
        return ",".join(affected)

    def _stop_retraction(self) -> tuple[str, ...]:
        errors: list[str] = []
        self._cleanup_call(errors, "stop_motion", self.robot.stop_motion)
        if self.stop_policy == "hold":
            self._cleanup_call(errors, "hold_position", self.robot.hold_position)
        self._cleanup_call(
            errors,
            "disable_custom_gain",
            lambda: self.robot.set_custom_gain(False, None),
        )
        if not errors:
            try:
                self._confirm_controller_settled()
            except AdapterError as exc:
                errors.append(f"controller_confirmation:{exc.code}:{exc}")
        self._cumulative_jog_mm.clear()
        self._stop_requested.clear()
        if not errors:
            with self._state_lock:
                self._stop_followup_required = False
                self._state = ExecutorState.TAUGHT_READY
        return tuple(errors)

    def _confirm_controller_settled(self) -> None:
        controller = self.robot.controller_state()
        if not controller.connected:
            raise AdapterError(
                "controller_disconnected",
                "robot controller connection was lost",
                component="indy_dcp3",
                operation="controller_state",
                retryable=True,
            )
        if controller.fault_code:
            raise AdapterError(
                "controller_fault",
                f"robot controller reports fault {controller.fault_code}",
                component="indy_dcp3",
                operation="controller_state",
            )
        if controller.motion_active:
            raise AdapterError(
                "motion_not_settled",
                "robot controller still reports active motion",
                component="indy_dcp3",
                operation="controller_state",
                retryable=True,
            )

    def _validate_force_freshness(self, sample: ForceTorqueSample) -> None:
        now_ns = self.clock.monotonic_ns()
        age_ns = now_ns - int(sample.timestamp_ns)
        if age_ns < 0:
            raise AdapterError(
                "force_sample_from_future",
                f"force sample for {sample.sensor_id!r} is ahead of the monotonic clock",
                component="aft200",
                operation="latest_sample",
            )
        if age_ns > self.force_freshness_timeout_ns:
            raise AdapterError(
                "force_sample_stale",
                f"force sample for {sample.sensor_id!r} is stale",
                component="aft200",
                operation="latest_sample",
                retryable=True,
            )

    def _resolve_side(self, side: int) -> object:
        resolve = getattr(self.profile, "resolve_side", None)
        if not callable(resolve):
            raise _ExecutionRejected(
                "profile_incompatible", "execution profile lacks resolve_side()"
            )
        try:
            return resolve(side)
        except Exception as exc:
            raise _ExecutionRejected(
                _error_code(exc, "side_mapping_invalid"), str(exc)
            ) from exc

    def _side_mappings(self) -> tuple[object, ...]:
        mappings = _value(self.profile, "side_mappings", {})
        values = mappings.values() if isinstance(mappings, Mapping) else mappings
        unique: dict[tuple[str, str], object] = {}
        for mapping in values:
            key = (
                str(_value(mapping, "arm_id", "")),
                str(_value(mapping, "sensor_id", "")),
            )
            if not all(key):
                continue
            unique[key] = mapping
        return tuple(unique[key] for key in sorted(unique))

    def _stop_is_set(
        self, signal: StopSignal | Callable[[], bool] | None
    ) -> bool:
        if self._stop_requested.is_set():
            return True
        if signal is None:
            return False
        if callable(signal) and not hasattr(signal, "is_set"):
            return bool(signal())
        is_set = getattr(signal, "is_set", None)
        return bool(is_set()) if callable(is_set) else False

    def _raise_if_stopped(
        self, signal: StopSignal | Callable[[], bool] | None
    ) -> None:
        if self._stop_is_set(signal):
            raise _ExecutionCanceled("stop was requested during execution")

    def _cleanup_after_failure(self, command: int) -> tuple[str, ...]:
        errors: list[str] = []
        if command in {
            COMMAND_START_RETRACTION,
            COMMAND_ADJUST_RETRACTION,
            COMMAND_CHANGE_TOOL,
            COMMAND_STOP_RETRACTION,
        }:
            self._cleanup_call(errors, "stop_motion", self.robot.stop_motion)
            self._cleanup_call(
                errors,
                "disable_custom_gain",
                lambda: self.robot.set_custom_gain(False, None),
            )
        if command in {COMMAND_START_DIRECT_TEACH, COMMAND_FINISH_DIRECT_TEACH}:
            self._cleanup_call(
                errors, "end_recording", self.force_sensor.end_recording
            )
            self._cleanup_call(
                errors,
                "disable_direct_teaching",
                lambda: self.robot.set_direct_teaching(False),
            )
            self._cleanup_call(
                errors,
                "disable_custom_gain",
                lambda: self.robot.set_custom_gain(False, None),
            )
            self._cleanup_call(
                errors,
                "restore_friction",
                lambda: self.robot.set_friction_compensation(
                    _value(self.profile, "normal_friction")
                ),
            )
            if self._recorder is not None:
                self._recorder.abort()
                self._recorder = None
        return tuple(errors)

    @staticmethod
    def _cleanup_call(
        errors: list[str], name: str, operation: Callable[[], None]
    ) -> None:
        try:
            operation()
        except Exception as exc:
            code = str(getattr(exc, "code", type(exc).__name__))
            errors.append(f"{name}:{code}:{exc}")

    @staticmethod
    def _session_id_for_command(command_id: str) -> str:
        if command_id and all(
            character.isalnum() or character in "_.-" for character in command_id
        ) and len(command_id) <= 120:
            return f"teach-{command_id}"
        digest = hashlib.sha256(command_id.encode("utf-8")).hexdigest()[:24]
        return f"teach-{digest}"

    @staticmethod
    def _operation_name(command: int) -> str:
        return {
            COMMAND_START_DIRECT_TEACH: "start_direct_teach",
            COMMAND_FINISH_DIRECT_TEACH: "finish_direct_teach",
            COMMAND_START_RETRACTION: "start_retraction",
            COMMAND_ADJUST_RETRACTION: "adjust_retraction",
            COMMAND_CHANGE_TOOL: "change_tool",
            COMMAND_STOP_RETRACTION: "stop_retraction",
        }.get(command, "unknown")

    def health(self) -> ExecutorHealth:
        """Perform bounded adapter reads and report controller/sensor readiness."""

        errors: list[str] = []
        robot_connected = False
        controller_fault_code = ""
        stale: list[str] = []
        try:
            controller = self.robot.controller_state()
            robot_connected = controller.connected
            controller_fault_code = controller.fault_code
        except AdapterError as exc:
            errors.append(f"robot:{exc.code}:{exc}")
        for mapping in self._side_mappings():
            sensor_id = str(_value(mapping, "sensor_id", ""))
            try:
                self._validate_force_freshness(
                    self.force_sensor.latest_sample(sensor_id)
                )
            except AdapterError as exc:
                stale.append(sensor_id)
                errors.append(f"sensor:{sensor_id}:{exc.code}:{exc}")
        return ExecutorHealth(
            checked_at_ns=self.clock.monotonic_ns(),
            started=self._started,
            owner_acquired=(
                self.owner_guard is None or self.owner_guard.acquired
            ),
            robot_connected=robot_connected,
            sensor_available=not stale and bool(self._side_mappings()),
            controller_fault_code=controller_fault_code,
            stale_sensor_ids=tuple(stale),
            errors=tuple(errors),
        )

    def shutdown(self) -> ExecutionOutcome:
        """Best-effort, ordered de-energization followed by adapter close."""

        started_at_ns = self.clock.monotonic_ns()
        with self._execute_lock:
            errors: list[str] = []
            self._stop_requested.set()
            self._cleanup_call(errors, "stop_motion", self.robot.stop_motion)
            self._cleanup_call(
                errors, "end_recording", self.force_sensor.end_recording
            )
            self._cleanup_call(
                errors,
                "disable_direct_teaching",
                lambda: self.robot.set_direct_teaching(False),
            )
            self._cleanup_call(
                errors,
                "disable_custom_gain",
                lambda: self.robot.set_custom_gain(False, None),
            )
            self._cleanup_call(
                errors,
                "restore_friction",
                lambda: self.robot.set_friction_compensation(
                    _value(self.profile, "normal_friction")
                ),
            )
            self._cleanup_call(errors, "stop_sensor", self.force_sensor.stop)
            self._cleanup_call(errors, "close_robot", self.robot.close)
            self._cleanup_call(errors, "close_sensor", self.force_sensor.close)
            if self.owner_guard is not None:
                self.owner_guard.release()
            self._started = False
            if self._recorder is not None:
                self._recorder.abort()
                self._recorder = None
            with self._state_lock:
                self._state = ExecutorState.SHUTDOWN
                self._active_command_id = ""
                self._active_operation = ""
                if errors:
                    self._last_error_code = "shutdown_cleanup_failed"
                    self._last_error_message = "; ".join(errors)
            return self._outcome(
                ExecutionStatus.FAILED if errors else ExecutionStatus.SUCCEEDED,
                "shutdown_cleanup_failed" if errors else "shutdown_complete",
                "; ".join(errors) if errors else "executor shut down safely",
                "shutdown",
                0,
                started_at_ns,
                cleanup_errors=errors,
            )

    def __call__(
        self,
        request: object,
        stop_requested: StopSignal | Callable[[], bool] | None = None,
    ) -> ExecutionOutcome:
        """Allow direct injection into the ROS-independent ``CommandWorker``."""

        return self.execute(request, stop_requested)


__all__ = [
    "CommandExecutor",
    "ExecutionAdmission",
    "ExecutionOutcome",
    "ExecutionStatus",
    "ExecutorHealth",
    "ExecutorState",
]
