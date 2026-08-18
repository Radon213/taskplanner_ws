from types import SimpleNamespace
import threading

import pytest
from rclpy.action import GoalResponse

from skill_execution.bridge import SkillActionBridge
from skill_execution.mock_server import (
    ALLOWED_ACTIONS,
    MockSkillActionServer,
    SkillGoalInterrupted,
)


def test_skill_bridge_rejects_commands_while_runtime_is_inactive() -> None:
    bridge = SkillActionBridge.__new__(SkillActionBridge)
    bridge._runtime_accepting_commands = False
    bridge._coerce_command = lambda _msg: SimpleNamespace(action="ignored")
    statuses: list[tuple[str, bool, str]] = []
    bridge._publish_status = lambda _command, state, success, message: statuses.append(
        (state, success, message)
    )

    bridge._on_command(SimpleNamespace())

    assert statuses == [
        (
            "rejected",
            False,
            "simulation runtime is not accepting skill commands",
        )
    ]


def test_skill_bridge_pause_cancel_is_edge_triggered_and_resume_reopens() -> None:
    bridge = SkillActionBridge.__new__(SkillActionBridge)
    bridge._runtime_accepting_commands = True
    bridge._last_lifecycle_control_signature = None
    bridge._request_dispatch_ledger = SimpleNamespace(clear=lambda: None)
    cancellations: list[str] = []
    bridge._cancel_active_goal = cancellations.append

    bridge._on_control(SimpleNamespace(data="pause"))
    bridge._on_control(SimpleNamespace(data="pause"))
    assert bridge._runtime_accepting_commands is False
    assert cancellations == ["pause"]

    bridge._on_control(SimpleNamespace(data="resume"))
    assert bridge._runtime_accepting_commands is True

    bridge._on_control(SimpleNamespace(data="pause"))
    assert cancellations == ["pause", "pause"]


def test_skill_bridge_reset_is_never_persistently_deduplicated() -> None:
    bridge = SkillActionBridge.__new__(SkillActionBridge)
    bridge._runtime_accepting_commands = True
    bridge._last_lifecycle_control_signature = None
    cancellations: list[str] = []
    clears: list[bool] = []
    bridge._cancel_active_goal = cancellations.append
    bridge._request_dispatch_ledger = SimpleNamespace(
        clear=lambda: clears.append(True)
    )

    bridge._on_control(SimpleNamespace(data="reset"))
    bridge._on_control(SimpleNamespace(data="reset"))

    assert cancellations == ["reset", "reset"]
    assert clears == [True, True]
    assert bridge._runtime_accepting_commands is False

    bridge._on_control(SimpleNamespace(data="start"))
    bridge._on_control(SimpleNamespace(data="start"))
    assert bridge._runtime_accepting_commands is True


def test_skill_bridge_repeated_reset_cancels_the_same_goal_once() -> None:
    bridge = SkillActionBridge.__new__(SkillActionBridge)
    bridge._runtime_accepting_commands = True
    bridge._last_lifecycle_control_signature = None
    bridge._request_dispatch_ledger = SimpleNamespace(clear=lambda: None)
    bridge._active_command = SimpleNamespace(command_id="goal-1")
    cancel_calls: list[bool] = []
    bridge._active_goal_handle = SimpleNamespace(
        cancel_goal_async=lambda: cancel_calls.append(True)
    )
    bridge._active_signature = ("tool_handover",)
    bridge._active_command_id = "goal-1"
    bridge._command_started_ns = {"goal-1": 1}
    bridge._cancelled_command_ids = set()
    statuses: list[str] = []
    bridge._publish_status = lambda *_args: statuses.append("cancel_requested")

    bridge._on_control(SimpleNamespace(data="reset"))
    bridge._on_control(SimpleNamespace(data="reset"))

    assert cancel_calls == [True]
    assert statuses == ["cancel_requested"]


class _Logger:
    def warning(self, _message: str) -> None:
        pass

    def info(self, _message: str) -> None:
        pass


def test_mock_server_rejects_late_goal_and_paused_generation_is_stable() -> None:
    server = MockSkillActionServer.__new__(MockSkillActionServer)
    server._runtime_accepting_commands = False
    server._last_lifecycle_control_signature = None
    server._control_generation = 0
    server._control_lock = threading.Lock()
    server._accepted_goal_generations = {}
    server.get_logger = lambda: _Logger()
    goal = SimpleNamespace(action=next(iter(ALLOWED_ACTIONS)), instrument_id="", mode="")

    assert server._on_goal(goal) == GoalResponse.REJECT

    server._on_control(SimpleNamespace(data="start"))
    assert server._on_goal(goal) == GoalResponse.ACCEPT
    server._on_control(SimpleNamespace(data="pause"))
    server._on_control(SimpleNamespace(data="pause"))
    assert server._runtime_accepting_commands is False
    assert server._control_generation == 1
    assert server._on_goal(goal) == GoalResponse.REJECT

    server._on_control(SimpleNamespace(data="resume"))
    assert server._runtime_accepting_commands is True


def test_mock_server_reset_is_repeatable_and_reopens_the_next_start_edge() -> None:
    server = MockSkillActionServer.__new__(MockSkillActionServer)
    server._runtime_accepting_commands = False
    server._last_lifecycle_control_signature = None
    server._control_generation = 0
    server._control_lock = threading.Lock()
    server._accepted_goal_generations = {}

    server._on_control(SimpleNamespace(data="start"))
    server._on_control(SimpleNamespace(data="start"))
    server._on_control(SimpleNamespace(data="reset"))
    server._on_control(SimpleNamespace(data="reset"))
    server._on_control(SimpleNamespace(data="start"))

    assert server._control_generation == 2
    assert server._runtime_accepting_commands is True


def test_mock_server_rejects_goal_accepted_before_pause_even_after_resume() -> None:
    server = MockSkillActionServer.__new__(MockSkillActionServer)
    server._runtime_accepting_commands = True
    server._last_lifecycle_control_signature = None
    server._control_generation = 0
    server._control_lock = threading.Lock()
    server._accepted_goal_generations = {}
    server.get_logger = lambda: _Logger()
    goal = SimpleNamespace(
        action=next(iter(ALLOWED_ACTIONS)),
        instrument_id="",
        mode="",
        command_id="accepted-before-pause",
    )

    assert server._on_goal(goal) == GoalResponse.ACCEPT
    server._on_control(SimpleNamespace(data="pause"))
    server._on_control(SimpleNamespace(data="resume"))

    with pytest.raises(SkillGoalInterrupted):
        server._begin_goal_execution(goal)
