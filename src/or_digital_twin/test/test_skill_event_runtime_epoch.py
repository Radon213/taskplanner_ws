from __future__ import annotations

import json
from types import SimpleNamespace

from or_digital_twin.node import ORDigitalTwinNode
from surgical_msgs.msg import TwinEvent


def _task_event(
    event_type: str,
    *,
    command_id: str,
    stamp_sec: int,
    task_type: str = "tool_handover",
) -> TwinEvent:
    message = TwinEvent()
    message.procedure_run_id = "fresh-run"
    message.event_type = event_type
    message.instrument_id = "T01"
    message.stamp.sec = stamp_sec
    message.stamp.nanosec = 0
    message.detail_json = json.dumps(
        {
            "task_id": command_id,
            "command_id": command_id,
            "task_type": task_type,
        }
    )
    return message


def _node() -> tuple[ORDigitalTwinNode, list[str], list[str]]:
    node = ORDigitalTwinNode.__new__(ORDigitalTwinNode)
    applied: list[str] = []
    published: list[str] = []
    state = SimpleNamespace(
        running=True,
        execution_state="running",
        procedure_run_id="fresh-run",
        active_robot_task=None,
    )
    node._twin = SimpleNamespace(
        state=state,
        apply_event=lambda message: applied.append(message.event_type),
        request_queue_summary=lambda: {},
    )
    node._skill_event_runtime_epoch = 1
    node._skill_event_source_stamp_floor_ns = 100 * 1_000_000_000
    node._current_run_skill_task_ids = set()
    node._active_robot_task_is_direct_delivery = lambda: False
    node._append_tool_history = lambda *_args, **_kwargs: None
    node._augment_event_detail = lambda _event_type, detail, **_kwargs: detail
    node._event_pub = SimpleNamespace(
        publish=lambda message: published.append(message.event_type)
    )
    node._simulation_event_pub = SimpleNamespace(publish=lambda _message: None)
    node._record_important_event = lambda _message: None
    node._publish_world_state = lambda: None
    return node, applied, published


def test_previous_run_task_events_cannot_replay_into_a_fresh_runtime() -> None:
    node, applied, published = _node()

    # This is a delayed RobotTaskStarted from before the current start edge.
    node._on_skill_event(
        _task_event("RobotTaskStarted", command_id="old-action", stamp_sec=99)
    )

    assert applied == []
    assert published == []
    assert node._current_run_skill_task_ids == set()


def test_current_task_chain_requires_a_fresh_start_and_correlated_completion() -> None:
    node, applied, published = _node()

    node._on_skill_event(
        _task_event("RobotTaskStarted", command_id="new-action", stamp_sec=101)
    )
    # A delayed completion for the old action must not mutate or clear the
    # freshly started action chain.
    node._on_skill_event(
        _task_event("RobotTaskCompleted", command_id="old-action", stamp_sec=102)
    )
    node._on_skill_event(
        _task_event("ToolHandoverCompleted", command_id="new-action", stamp_sec=103)
    )
    node._on_skill_event(
        _task_event("RobotTaskCompleted", command_id="new-action", stamp_sec=104)
    )

    assert applied == [
        "RobotTaskStarted",
        "RobotTaskCompleted",
    ]
    # The physical receipt remains in the audit stream, but tool location is
    # projected later from the belief owner's committed update.
    assert published == [
        "RobotTaskStarted",
        "ToolHandoverCompleted",
        "RobotTaskCompleted",
    ]
    assert node._current_run_skill_task_ids == set()


def test_finishing_accepts_only_completion_of_an_already_started_task() -> None:
    node, applied, published = _node()
    node._on_skill_event(
        _task_event("RobotTaskStarted", command_id="pre-finish-action", stamp_sec=101)
    )
    node._twin.state.execution_state = "finishing"

    # A terminal controller receipt must drain the pre-finish Action. Without
    # it, active_robot_task remains occupied forever and terminal cleanup can
    # never issue its robot-to-Mayo or robot-to-tray leg.
    node._on_skill_event(
        _task_event("RobotTaskCompleted", command_id="pre-finish-action", stamp_sec=102)
    )
    # Finishing never opens a new Action chain.
    node._on_skill_event(
        _task_event("RobotTaskStarted", command_id="after-finish-action", stamp_sec=103)
    )
    # A finishing-only cleanup leg is the sole new Action chain allowed after
    # the speech boundary, and its completion remains correlated normally.
    node._on_skill_event(
        _task_event(
            "RobotTaskStarted",
            command_id="terminal-cleanup-action",
            stamp_sec=104,
            task_type="return_preposition_to_tray",
        )
    )
    node._on_skill_event(
        _task_event(
            "RobotTaskCompleted",
            command_id="terminal-cleanup-action",
            stamp_sec=105,
            task_type="return_preposition_to_tray",
        )
    )

    assert applied == [
        "RobotTaskStarted",
        "RobotTaskCompleted",
        "RobotTaskStarted",
        "RobotTaskCompleted",
    ]
    assert published == applied
    assert node._current_run_skill_task_ids == set()


def test_runtime_epoch_clears_pre_restart_command_chain() -> None:
    node, _applied, _published = _node()
    node._stamp = lambda: SimpleNamespace(sec=200, nanosec=0)
    node._current_run_skill_task_ids.add("old-action")

    node._advance_skill_event_runtime_epoch()

    assert node._skill_event_runtime_epoch == 2
    assert node._skill_event_source_stamp_floor_ns == 200 * 1_000_000_000
    assert node._current_run_skill_task_ids == set()
