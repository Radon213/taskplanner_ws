from __future__ import annotations

import pytest

from retraction_control.command_models import (
    Command,
    CommandRequest,
    ErrorCode,
    StateTransitionError,
    TargetSide,
)
from retraction_control.state_machine import (
    RetractionState,
    RetractionStateMachine,
    admissible_commands,
    plan_transition,
)


def _request(command: Command, command_id: str) -> CommandRequest:
    adjust = command is Command.ADJUST_RETRACTION
    return CommandRequest(
        protocol_version=1,
        source_id="taskplanner",
        command_id=command_id,
        command=command,
        target_side=TargetSide.LEFT if adjust else TargetSide.NONE,
        distance_m=0.050 if adjust else 0.0,
    )


def _reach_retracting(machine: RetractionStateMachine) -> None:
    machine.begin(_request(Command.START_DIRECT_TEACH, "teach-start"))
    machine.complete("teach-start")
    machine.begin(_request(Command.FINISH_DIRECT_TEACH, "teach-finish"))
    machine.complete("teach-finish", session_valid=True)
    machine.begin(_request(Command.START_RETRACTION, "retract-start"))
    machine.complete("retract-start")


def _reach_taught_ready(machine: RetractionStateMachine) -> None:
    machine.begin(_request(Command.START_DIRECT_TEACH, "teach-start"))
    machine.complete("teach-start")
    machine.begin(_request(Command.FINISH_DIRECT_TEACH, "teach-finish"))
    machine.complete("teach-finish", session_valid=True)


def test_admission_is_side_effect_free() -> None:
    machine = RetractionStateMachine()

    admission = machine.admit(_request(Command.START_DIRECT_TEACH, "cmd-1"))

    assert admission.source_state is RetractionState.IDLE
    assert admission.success_state is RetractionState.DIRECT_TEACHING
    assert machine.state is RetractionState.IDLE
    assert machine.revision == 0
    assert machine.active_command_id is None


def test_full_verified_state_sequence() -> None:
    machine = RetractionStateMachine()
    _reach_retracting(machine)

    assert machine.state is RetractionState.RETRACTING
    assert machine.can_admit(Command.ADJUST_RETRACTION)
    assert machine.can_admit(Command.CHANGE_TOOL)
    assert machine.can_admit(Command.STOP_RETRACTION)

    machine.begin(_request(Command.ADJUST_RETRACTION, "adjust"))
    machine.complete("adjust")
    assert machine.state is RetractionState.RETRACTING

    machine.begin(_request(Command.CHANGE_TOOL, "tool"))
    assert machine.state is RetractionState.TOOL_CHANGING
    machine.complete("tool")
    assert machine.state is RetractionState.RETRACTING

    machine.begin(_request(Command.STOP_RETRACTION, "stop"))
    assert machine.state is RetractionState.STOPPING
    machine.complete("stop")
    assert machine.state is RetractionState.TAUGHT_READY


def test_direct_teach_finish_requires_verified_session() -> None:
    machine = RetractionStateMachine()
    machine.begin(_request(Command.START_DIRECT_TEACH, "start"))
    machine.complete("start")
    machine.begin(_request(Command.FINISH_DIRECT_TEACH, "finish"))

    with pytest.raises(StateTransitionError) as raised:
        machine.complete("finish", session_valid=False)

    assert raised.value.code is ErrorCode.SESSION_NOT_VALID
    assert machine.state is RetractionState.DIRECT_TEACHING
    assert machine.active_command_id == "finish"


@pytest.mark.parametrize(
    ("state", "command"),
    [
        (RetractionState.IDLE, Command.START_RETRACTION),
        (RetractionState.DIRECT_TEACHING, Command.ADJUST_RETRACTION),
        (RetractionState.TAUGHT_READY, Command.CHANGE_TOOL),
        (RetractionState.RETRACTING, Command.FINISH_DIRECT_TEACH),
        (RetractionState.FAULT, Command.STOP_RETRACTION),
    ],
)
def test_invalid_state_command_pairs_fail_closed(
    state: RetractionState, command: Command
) -> None:
    with pytest.raises(StateTransitionError) as raised:
        plan_transition(state, command)

    assert raised.value.code is ErrorCode.COMMAND_NOT_ALLOWED


def test_stop_has_priority_path_during_tool_change() -> None:
    machine = RetractionStateMachine()
    _reach_retracting(machine)
    machine.begin(_request(Command.CHANGE_TOOL, "tool"))

    admission = machine.admit(_request(Command.STOP_RETRACTION, "stop"))
    assert admission.priority is True
    assert admission.interrupted_command_id == "tool"

    machine.begin(_request(Command.STOP_RETRACTION, "stop"))
    assert machine.active_command_id == "stop"
    assert machine.state is RetractionState.STOPPING


def test_stop_has_priority_path_while_start_retraction_is_physically_running() -> None:
    machine = RetractionStateMachine()
    _reach_taught_ready(machine)
    machine.begin(_request(Command.START_RETRACTION, "retract-start"))
    assert machine.state is RetractionState.TAUGHT_READY

    admission = machine.admit(_request(Command.STOP_RETRACTION, "urgent-stop"))

    assert admission.priority is True
    assert admission.interrupted_command_id == "retract-start"
    assert admission.source_state is RetractionState.TAUGHT_READY
    assert admission.running_state is RetractionState.STOPPING
    assert admission.success_state is RetractionState.TAUGHT_READY

    machine.begin(_request(Command.STOP_RETRACTION, "urgent-stop"))
    assert machine.state is RetractionState.STOPPING
    machine.complete("urgent-stop")
    assert machine.state is RetractionState.TAUGHT_READY


def test_unrecoverable_failure_enters_fault_and_reset_requires_diagnostics() -> None:
    machine = RetractionStateMachine()
    machine.begin(_request(Command.START_DIRECT_TEACH, "start"))

    failed = machine.fail("SDK connection lost", "start", fatal=True)

    assert failed.state is RetractionState.FAULT
    assert failed.last_error is not None
    with pytest.raises(StateTransitionError) as raised:
        machine.reset_fault(diagnostics_verified=False)
    assert raised.value.code is ErrorCode.FAULT_RESET_NOT_VERIFIED

    reset = machine.reset_fault(diagnostics_verified=True)
    assert reset.state is RetractionState.IDLE
    assert reset.last_error is None


def test_cold_start_session_restore_is_explicit() -> None:
    machine = RetractionStateMachine()

    with pytest.raises(StateTransitionError):
        machine.restore_verified_session(session_verified=False)

    restored = machine.restore_verified_session(session_verified=True)
    assert restored.state is RetractionState.TAUGHT_READY


def test_closed_admission_sets_match_plan() -> None:
    assert admissible_commands(RetractionState.IDLE) == frozenset(
        {Command.START_DIRECT_TEACH}
    )
    assert admissible_commands(RetractionState.STOPPING) == frozenset()
    assert admissible_commands(RetractionState.FAULT) == frozenset()
