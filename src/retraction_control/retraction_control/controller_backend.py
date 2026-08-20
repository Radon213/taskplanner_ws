"""Composition layer joining core state, adapters, and the command worker."""

from __future__ import annotations

from dataclasses import dataclass
import threading

from .command_executor import (
    CommandExecutor,
    ExecutionOutcome,
    ExecutionStatus,
    ExecutorHealth,
    ExecutorState,
)
from .command_models import (
    Command,
    CommandRequest,
    ErrorCode,
    StateTransitionError,
)
from .runtime import ExecutionReport, RuntimeCommand
from .state_machine import RetractionStateMachine, StateSnapshot


@dataclass(frozen=True, slots=True)
class BackendSnapshot:
    state: StateSnapshot
    executor_state: ExecutorState
    active_operation: str
    last_outcome: ExecutionOutcome | None


class ControllerBackend:
    """Keep admission state and physical execution results synchronized."""

    def __init__(
        self,
        executor: CommandExecutor,
        state_machine: RetractionStateMachine | None = None,
    ) -> None:
        self.executor = executor
        self.state_machine = state_machine or RetractionStateMachine()
        self._lock = threading.RLock()
        self._last_outcome: ExecutionOutcome | None = None

    @property
    def last_outcome(self) -> ExecutionOutcome | None:
        with self._lock:
            return self._last_outcome

    def start(self) -> None:
        """Perform startup I/O before the ROS Service becomes discoverable."""

        try:
            self.executor.start()
            if self.executor.state is ExecutorState.TAUGHT_READY:
                self.state_machine.restore_verified_session(session_verified=True)
            elif self.executor.state is not ExecutorState.IDLE:
                raise RuntimeError(
                    "executor startup must resolve to IDLE or TAUGHT_READY, got "
                    f"{self.executor.state.value}"
                )
        except Exception as exc:
            self.state_machine.fail(exc, fatal=True)
            raise

    def check_admission(self, request: CommandRequest) -> None:
        """Run both side-effect-free admission fences."""

        self.state_machine.admit(request)
        executor_admission = self.executor.check_admission(request)
        if not executor_admission.accepted:
            raise StateTransitionError(
                ErrorCode.COMMAND_NOT_ALLOWED,
                executor_admission.message,
                field="command",
                context={"executor_code": executor_admission.code},
            )

    def execute_runtime(
        self,
        runtime_command: RuntimeCommand,
        stop_requested: threading.Event,
    ) -> ExecutionReport:
        request = runtime_command.payload
        if not isinstance(request, CommandRequest):
            return ExecutionReport(
                False,
                "invalid_runtime_payload",
                "worker payload was not a validated CommandRequest",
            )

        try:
            self.state_machine.begin(request)
        except StateTransitionError as exc:
            return ExecutionReport(False, exc.code.value, str(exc))

        outcome = self.executor.execute(request, stop_requested)
        with self._lock:
            self._last_outcome = outcome

        try:
            if outcome.success:
                self.state_machine.complete(
                    request.command_id,
                    session_valid=(
                        True
                        if request.command is Command.FINISH_DIRECT_TEACH
                        else None
                    ),
                )
            else:
                self.state_machine.fail(
                    f"{outcome.code}: {outcome.message}",
                    request.command_id,
                    fatal=outcome.status is ExecutionStatus.FAILED,
                )
        except StateTransitionError as exc:
            # A disagreement between the physical executor and the core state
            # contract is itself fail-closed controller evidence.
            try:
                self.state_machine.fail(exc, fatal=True)
            except StateTransitionError:
                pass
            return ExecutionReport(
                False,
                "state_commit_failed",
                str(exc),
            )

        return ExecutionReport(
            outcome.success,
            outcome.code,
            outcome.message,
            canceled=outcome.canceled,
        )

    def snapshot(self) -> BackendSnapshot:
        with self._lock:
            return BackendSnapshot(
                state=self.state_machine.snapshot(),
                executor_state=self.executor.state,
                active_operation=self.executor.active_operation,
                last_outcome=self._last_outcome,
            )

    def health(self) -> ExecutorHealth:
        return self.executor.health()

    def shutdown(self) -> ExecutionOutcome:
        return self.executor.shutdown()


__all__ = ["BackendSnapshot", "ControllerBackend"]

