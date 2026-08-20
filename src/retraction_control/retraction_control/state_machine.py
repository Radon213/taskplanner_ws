"""Deterministic physical-state admission for retraction control.

Admission is intentionally side-effect free.  The state changes only when a
worker calls :meth:`RetractionStateMachine.begin` and later reports a verified
success or failure.  This prevents a successful service response from being
mistaken for completed robot motion.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from threading import RLock

from .command_models import (
    Command,
    CommandRequest,
    ErrorCode,
    ErrorDetail,
    ResultCode,
    StateTransitionError,
)


class RetractionState(str, Enum):
    IDLE = "idle"
    DIRECT_TEACHING = "direct_teaching"
    TAUGHT_READY = "taught_ready"
    RETRACTING = "retracting"
    # Compatibility name used by the Taskplanner-side vocabulary.  It is an
    # alias, not a separate physical state.
    RETRACTION_ACTIVE = "retracting"
    TOOL_CHANGING = "tool_changing"
    STOPPING = "stopping"
    FAULT = "fault"


_ADMISSIBLE: dict[RetractionState, frozenset[Command]] = {
    RetractionState.IDLE: frozenset({Command.START_DIRECT_TEACH}),
    RetractionState.DIRECT_TEACHING: frozenset({Command.FINISH_DIRECT_TEACH}),
    RetractionState.TAUGHT_READY: frozenset({Command.START_RETRACTION}),
    RetractionState.RETRACTING: frozenset(
        {
            Command.ADJUST_RETRACTION,
            Command.CHANGE_TOOL,
            Command.STOP_RETRACTION,
        }
    ),
    # Stop has a priority path while a tool-change operation is in progress.
    RetractionState.TOOL_CHANGING: frozenset({Command.STOP_RETRACTION}),
    RetractionState.STOPPING: frozenset(),
    RetractionState.FAULT: frozenset(),
}

_SUCCESS_STATE: dict[tuple[RetractionState, Command], RetractionState] = {
    (RetractionState.IDLE, Command.START_DIRECT_TEACH): RetractionState.DIRECT_TEACHING,
    (
        RetractionState.DIRECT_TEACHING,
        Command.FINISH_DIRECT_TEACH,
    ): RetractionState.TAUGHT_READY,
    (
        RetractionState.TAUGHT_READY,
        Command.START_RETRACTION,
    ): RetractionState.RETRACTING,
    (
        RetractionState.RETRACTING,
        Command.ADJUST_RETRACTION,
    ): RetractionState.RETRACTING,
    (RetractionState.RETRACTING, Command.CHANGE_TOOL): RetractionState.RETRACTING,
    (RetractionState.RETRACTING, Command.STOP_RETRACTION): RetractionState.TAUGHT_READY,
    (
        RetractionState.TOOL_CHANGING,
        Command.STOP_RETRACTION,
    ): RetractionState.TAUGHT_READY,
}

_RUNNING_STATE: dict[Command, RetractionState] = {
    Command.CHANGE_TOOL: RetractionState.TOOL_CHANGING,
    Command.STOP_RETRACTION: RetractionState.STOPPING,
}


def _coerce_command(value: Command | CommandRequest | int) -> Command:
    if isinstance(value, CommandRequest):
        return value.command
    try:
        return Command(value)
    except (TypeError, ValueError) as exc:
        raise StateTransitionError(
            ErrorCode.INVALID_COMMAND,
            f"unknown command: {value!r}",
            field="command",
            result_code=ResultCode.INVALID_COMMAND,
        ) from exc


def _coerce_state(value: RetractionState | str) -> RetractionState:
    try:
        return RetractionState(value)
    except (TypeError, ValueError) as exc:
        raise StateTransitionError(
            ErrorCode.COMMAND_NOT_ALLOWED,
            f"unknown retraction state: {value!r}",
            field="state",
        ) from exc


def admissible_commands(state: RetractionState | str) -> frozenset[Command]:
    """Return the closed command set admitted by a physical state."""

    return _ADMISSIBLE[_coerce_state(state)]


@dataclass(frozen=True, slots=True)
class TransitionPlan:
    command: Command
    source_state: RetractionState
    running_state: RetractionState | None
    success_state: RetractionState


def plan_transition(
    state: RetractionState | str,
    command: Command | CommandRequest | int,
) -> TransitionPlan:
    """Validate and describe a transition without changing any state."""

    physical_state = _coerce_state(state)
    normalized_command = _coerce_command(command)
    if normalized_command not in _ADMISSIBLE[physical_state]:
        raise StateTransitionError(
            ErrorCode.COMMAND_NOT_ALLOWED,
            (
                f"{normalized_command.name} is not allowed while state is "
                f"{physical_state.value}"
            ),
            field="command",
            context={
                "state": physical_state.value,
                "command": normalized_command.name,
                "allowed": [item.name for item in _ADMISSIBLE[physical_state]],
            },
        )
    return TransitionPlan(
        command=normalized_command,
        source_state=physical_state,
        running_state=_RUNNING_STATE.get(normalized_command),
        success_state=_SUCCESS_STATE[(physical_state, normalized_command)],
    )


@dataclass(frozen=True, slots=True)
class Admission:
    """A side-effect-free decision returned to the service layer."""

    command: Command
    command_id: str | None
    source_state: RetractionState
    running_state: RetractionState | None
    success_state: RetractionState
    priority: bool = False
    interrupted_command_id: str | None = None


@dataclass(frozen=True, slots=True)
class StateSnapshot:
    state: RetractionState
    revision: int
    active_command: Command | None
    active_command_id: str | None
    last_error: ErrorDetail | None


@dataclass(slots=True)
class _ActiveTransition:
    admission: Admission


class RetractionStateMachine:
    """Thread-safe lifecycle state for one hardware-owning worker."""

    def __init__(self, initial_state: RetractionState = RetractionState.IDLE) -> None:
        state = _coerce_state(initial_state)
        if state is not RetractionState.IDLE:
            raise StateTransitionError(
                ErrorCode.COMMAND_NOT_ALLOWED,
                (
                    "cold start must begin in IDLE; restore only through a "
                    "verified session"
                ),
                field="initial_state",
            )
        self._state = state
        self._revision = 0
        self._active: _ActiveTransition | None = None
        self._last_error: ErrorDetail | None = None
        self._lock = RLock()

    @property
    def state(self) -> RetractionState:
        with self._lock:
            return self._state

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    @property
    def active_command(self) -> Command | None:
        with self._lock:
            return (
                self._active.admission.command if self._active is not None else None
            )

    @property
    def active_command_id(self) -> str | None:
        with self._lock:
            return (
                self._active.admission.command_id if self._active is not None else None
            )

    @property
    def last_error(self) -> ErrorDetail | None:
        with self._lock:
            return self._last_error

    def snapshot(self) -> StateSnapshot:
        with self._lock:
            return StateSnapshot(
                state=self._state,
                revision=self._revision,
                active_command=self.active_command,
                active_command_id=self.active_command_id,
                last_error=self._last_error,
            )

    def can_admit(self, command: Command | CommandRequest | int) -> bool:
        try:
            self.admit(command)
        except StateTransitionError:
            return False
        return True

    def admit(self, command: Command | CommandRequest | int) -> Admission:
        """Check service admission without mutating physical state.

        A stop request is the only command allowed to supersede an active
        retraction-start, adjustment, or tool-change command.  The execution
        layer remains responsible for cancelling/holding the interrupted
        hardware call.
        """

        normalized = _coerce_command(command)
        command_id = command.command_id if isinstance(command, CommandRequest) else None
        with self._lock:
            priority = False
            interrupted_command_id: str | None = None
            active: Admission | None = None
            if self._active is not None:
                active = self._active.admission
                priority = (
                    normalized is Command.STOP_RETRACTION
                    and active.command
                    in {
                        Command.START_RETRACTION,
                        Command.ADJUST_RETRACTION,
                        Command.CHANGE_TOOL,
                    }
                )
                if not priority:
                    raise StateTransitionError(
                        ErrorCode.COMMAND_ALREADY_ACTIVE,
                        "another physical command is already active",
                        field="command_id",
                        context={
                            "active_command": active.command.name,
                            "active_command_id": active.command_id,
                        },
                    )
                interrupted_command_id = active.command_id

            if (
                priority
                and active is not None
                and active.command is Command.START_RETRACTION
                and self._state is RetractionState.TAUGHT_READY
            ):
                # START_RETRACTION may already be issuing motion while the
                # verified physical state deliberately remains TAUGHT_READY.
                # STOP must still be admitted, without first claiming that
                # retraction completed.
                plan = TransitionPlan(
                    command=Command.STOP_RETRACTION,
                    source_state=RetractionState.TAUGHT_READY,
                    running_state=RetractionState.STOPPING,
                    success_state=RetractionState.TAUGHT_READY,
                )
            else:
                plan = plan_transition(self._state, normalized)
            return Admission(
                command=normalized,
                command_id=command_id,
                source_state=plan.source_state,
                running_state=plan.running_state,
                success_state=plan.success_state,
                priority=priority,
                interrupted_command_id=interrupted_command_id,
            )

    def begin(
        self,
        command: Command | CommandRequest | int,
        command_id: str | None = None,
    ) -> Admission:
        """Record that the worker actually began an admitted operation."""

        with self._lock:
            admission = self.admit(command)
            if isinstance(command, CommandRequest):
                if command_id is not None and command_id != command.command_id:
                    raise StateTransitionError(
                        ErrorCode.COMMAND_ID_MISMATCH,
                        "explicit command_id differs from the validated request",
                        field="command_id",
                    )
                command_id = command.command_id
            admission = Admission(
                command=admission.command,
                command_id=command_id,
                source_state=admission.source_state,
                running_state=admission.running_state,
                success_state=admission.success_state,
                priority=admission.priority,
                interrupted_command_id=admission.interrupted_command_id,
            )
            self._active = _ActiveTransition(admission)
            if admission.running_state is not None:
                self._state = admission.running_state
            self._last_error = None
            self._revision += 1
            return admission

    mark_started = begin

    def complete(
        self,
        command_id: str | None = None,
        *,
        session_valid: bool | None = None,
    ) -> StateSnapshot:
        """Apply a worker-confirmed physical success.

        Direct-teach completion additionally requires an explicitly verified
        session.  Truthy objects are not accepted; the caller must pass the
        boolean value ``True``.
        """

        with self._lock:
            if self._active is None:
                raise StateTransitionError(
                    ErrorCode.COMMAND_NOT_ACTIVE,
                    "no physical command is active",
                    field="command_id",
                )
            admission = self._active.admission
            self._require_matching_command_id(command_id, admission.command_id)
            if (
                admission.command is Command.FINISH_DIRECT_TEACH
                and session_valid is not True
            ):
                raise StateTransitionError(
                    ErrorCode.SESSION_NOT_VALID,
                    (
                        "direct teaching can finish only after session integrity "
                        "validation"
                    ),
                    field="session_valid",
                )
            self._state = admission.success_state
            self._active = None
            self._last_error = None
            self._revision += 1
            return self.snapshot()

    mark_succeeded = complete

    def fail(
        self,
        error: Exception | ErrorDetail | str,
        command_id: str | None = None,
        *,
        fatal: bool = True,
    ) -> StateSnapshot:
        """Record a worker failure, entering FAULT for unrecoverable errors."""

        with self._lock:
            if self._active is not None:
                self._require_matching_command_id(
                    command_id, self._active.admission.command_id
                )
                rollback_state = self._active.admission.source_state
                if rollback_state in (
                    RetractionState.TOOL_CHANGING,
                    RetractionState.STOPPING,
                ):
                    rollback_state = RetractionState.RETRACTING
            else:
                if command_id is not None:
                    raise StateTransitionError(
                        ErrorCode.COMMAND_NOT_ACTIVE,
                        "no physical command is active",
                        field="command_id",
                    )
                rollback_state = self._state

            self._last_error = self._error_detail(error)
            self._state = RetractionState.FAULT if fatal else rollback_state
            self._active = None
            self._revision += 1
            return self.snapshot()

    mark_failed = fail

    def reset_fault(
        self,
        *,
        diagnostics_verified: bool,
        restored_session_verified: bool = False,
    ) -> StateSnapshot:
        """Explicitly leave FAULT after diagnostics, never by implicit restart."""

        with self._lock:
            if self._state is not RetractionState.FAULT:
                raise StateTransitionError(
                    ErrorCode.COMMAND_NOT_ALLOWED,
                    "fault reset is only valid in FAULT",
                    field="state",
                )
            if diagnostics_verified is not True:
                raise StateTransitionError(
                    ErrorCode.FAULT_RESET_NOT_VERIFIED,
                    "fault reset requires explicit successful diagnostics",
                    field="diagnostics_verified",
                )
            self._state = (
                RetractionState.TAUGHT_READY
                if restored_session_verified is True
                else RetractionState.IDLE
            )
            self._active = None
            self._last_error = None
            self._revision += 1
            return self.snapshot()

    def restore_verified_session(self, *, session_verified: bool) -> StateSnapshot:
        """Cold-start restoration gate for an already validated teaching session."""

        with self._lock:
            if self._state is not RetractionState.IDLE or self._active is not None:
                raise StateTransitionError(
                    ErrorCode.COMMAND_NOT_ALLOWED,
                    "session restoration is only valid from an inactive IDLE state",
                    field="state",
                )
            if session_verified is not True:
                raise StateTransitionError(
                    ErrorCode.SESSION_NOT_VALID,
                    "saved session integrity was not verified",
                    field="session_verified",
                )
            self._state = RetractionState.TAUGHT_READY
            self._revision += 1
            return self.snapshot()

    @staticmethod
    def _require_matching_command_id(
        supplied: str | None, expected: str | None
    ) -> None:
        if supplied is not None and supplied != expected:
            raise StateTransitionError(
                ErrorCode.COMMAND_ID_MISMATCH,
                "completion/failure command_id does not match the active command",
                field="command_id",
                context={"expected": expected, "received": supplied},
            )

    @staticmethod
    def _error_detail(error: Exception | ErrorDetail | str) -> ErrorDetail:
        if isinstance(error, ErrorDetail):
            return error
        detail = getattr(error, "detail", None)
        if isinstance(detail, ErrorDetail):
            return detail
        return ErrorDetail(
            code=ErrorCode.COMMAND_NOT_ALLOWED,
            category=StateTransitionError.default_category,
            message=str(error),
            result_code=ResultCode.ERROR,
        )


StateMachine = RetractionStateMachine


__all__ = [
    "Admission",
    "RetractionState",
    "RetractionStateMachine",
    "StateMachine",
    "StateSnapshot",
    "TransitionPlan",
    "admissible_commands",
    "plan_transition",
]
