from __future__ import annotations

from types import SimpleNamespace

from retraction_control.command_executor import (
    ExecutionAdmission,
    ExecutionOutcome,
    ExecutionStatus,
    ExecutorState,
)
from retraction_control.command_models import CommandRequest
from retraction_control.controller_backend import ControllerBackend
from retraction_control.runtime import RuntimeCommand
from retraction_control.state_machine import RetractionState


class StubExecutor:
    def __init__(self, state=ExecutorState.IDLE):
        self.state = state
        self.active_operation = ""
        self.outcomes = []
        self.admission = ExecutionAdmission(True, "accepted", "ok", state)

    def start(self):
        return None

    def check_admission(self, _request):
        return self.admission

    def execute(self, request, _stop):
        outcome = self.outcomes.pop(0)
        self.state = outcome.executor_state
        return outcome

    def health(self):
        return SimpleNamespace(healthy=True)

    def shutdown(self):
        self.state = ExecutorState.SHUTDOWN
        return _outcome(0, ExecutorState.SHUTDOWN)


def _request(command, command_id):
    return CommandRequest.from_wire(
        protocol_version=1,
        source_id="taskplanner",
        command_id=command_id,
        command=command,
        target_side=0,
        distance_m=0.0,
    )


def _outcome(command, state, *, status=ExecutionStatus.SUCCEEDED, code="completed"):
    return ExecutionOutcome(
        status=status,
        code=code,
        message=code,
        command_id=f"cmd-{command}",
        command=command,
        started_at_ns=1,
        finished_at_ns=2,
        executor_state=state,
    )


def test_physical_success_commits_core_state_only_after_executor_result():
    executor = StubExecutor()
    executor.outcomes.append(_outcome(1, ExecutorState.DIRECT_TEACHING))
    backend = ControllerBackend(executor)
    backend.start()
    request = _request(1, "cmd-1")
    backend.check_admission(request)
    assert backend.snapshot().state.state is RetractionState.IDLE

    report = backend.execute_runtime(
        RuntimeCommand("cmd-1", 1, request),
        __import__("threading").Event(),
    )

    assert report.success
    assert backend.snapshot().state.state is RetractionState.DIRECT_TEACHING


def test_verified_session_is_the_only_cold_start_path_to_taught_ready():
    backend = ControllerBackend(StubExecutor(ExecutorState.TAUGHT_READY))
    backend.start()
    assert backend.snapshot().state.state is RetractionState.TAUGHT_READY


def test_executor_rejection_rolls_back_core_without_claiming_success():
    executor = StubExecutor()
    executor.outcomes.append(
        _outcome(
            1,
            ExecutorState.IDLE,
            status=ExecutionStatus.REJECTED,
            code="profile_not_ready",
        )
    )
    backend = ControllerBackend(executor)
    backend.start()
    request = _request(1, "cmd-1")

    report = backend.execute_runtime(
        RuntimeCommand("cmd-1", 1, request),
        __import__("threading").Event(),
    )

    assert not report.success
    assert backend.snapshot().state.state is RetractionState.IDLE

