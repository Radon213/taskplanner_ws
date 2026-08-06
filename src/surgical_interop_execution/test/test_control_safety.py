import json
import threading
from dataclasses import replace
from types import SimpleNamespace

from builtin_interfaces.msg import Time
from surgical_interop_msgs.action import ExecuteToolHandover

from surgical_interop_execution.bridge import (
    ActiveAction,
    ActiveService,
    SurgicalInteropExecutionBridge,
)
from surgical_interop_execution.mappings import (
    DispatchLedger,
    InternalGroupCommand,
    InternalSkillCommand,
)


def _skill(command_id: str = "skill-1") -> InternalSkillCommand:
    return InternalSkillCommand(
        command_id=command_id,
        action="tool_handover",
        instrument_id="T04",
        instrument_instance_id="T04#1",
        source_location_type="tray_slot",
        source_location_id="tray-a-2",
        target_location_type="handover_zone",
        target_location_id="surgeon_receive_zone",
        arm="right",
        request_generation=4,
        mode="explicit_request",
        rationale="internal only",
        target_owner="surgeon",
        cleaning_required=True,
    )


def _group(command_id: str = "group-1") -> InternalGroupCommand:
    return InternalGroupCommand(
        request_id="request-1",
        command_id=command_id,
        group_id="retraction",
        operation="retraction",
        direction="LEFT",
        distance_mm=5.0,
        end_effector_profile="",
    )


class _GoalHandle:
    def __init__(self):
        self.cancel_calls = 0

    def cancel_goal_async(self):
        self.cancel_calls += 1


def _bare_bridge() -> SurgicalInteropExecutionBridge:
    bridge = SurgicalInteropExecutionBridge.__new__(SurgicalInteropExecutionBridge)
    bridge._dispatch_lock = threading.RLock()
    bridge._runtime_accepting_commands = True
    bridge._dispatch_ledger = DispatchLedger(max_entries=8)
    bridge._active_actions = {}
    bridge._active_services = {}
    return bridge


def _activate_tool_transfer(
    bridge: SurgicalInteropExecutionBridge,
    command: InternalSkillCommand,
    *,
    cancel_requested: bool = False,
    goal_handle=None,
) -> None:
    bridge._active_actions[("tool_transfer", command.command_id)] = ActiveAction(
        route="tool_transfer",
        command=command,
        goal_handle=goal_handle,
        cancelled=cancel_requested,
    )


def test_public_tool_handover_state_vocabulary_is_fixed_and_minimal():
    assert {
        ExecuteToolHandover.Feedback.STATE_MOVING_TO_SOURCE,
        ExecuteToolHandover.Feedback.STATE_GRASPING,
        ExecuteToolHandover.Feedback.STATE_MOVING_TO_TARGET,
        ExecuteToolHandover.Feedback.STATE_WAITING_FOR_TAKEOVER,
        ExecuteToolHandover.Feedback.STATE_PLACING,
        ExecuteToolHandover.Feedback.STATE_HOLDING,
        ExecuteToolHandover.Feedback.STATE_STOPPING,
        ExecuteToolHandover.Feedback.STATE_RETREATING,
        ExecuteToolHandover.Feedback.STATE_RECOVERING_TO_TRAY,
    } == {
        "moving_to_source",
        "grasping",
        "moving_to_target",
        "waiting_for_takeover",
        "placing",
        "holding",
        "stopping",
        "retreating",
        "recovering_to_tray",
    }
    assert {
        ExecuteToolHandover.Result.FINAL_COMPLETED,
        ExecuteToolHandover.Result.FINAL_CANCELED,
        ExecuteToolHandover.Result.FINAL_FAILED,
    } == {"completed", "canceled", "failed"}
    assert {
        ExecuteToolHandover.Result.REASON_CANCELED_SOURCE_UNCHANGED,
        ExecuteToolHandover.Result.REASON_CANCELED_RECOVERED_TO_TRAY,
    } == {"canceled_source_unchanged", "canceled_recovered_to_tray"}


def test_unknown_controller_feedback_state_is_not_forwarded():
    bridge = _bare_bridge()
    command = _skill()
    _activate_tool_transfer(bridge, command)
    statuses = []
    bridge._publish_skill_status = lambda command, **kwargs: statuses.append(kwargs)

    bridge._on_tool_transfer_feedback(
        command,
        SimpleNamespace(
            feedback=SimpleNamespace(state="robot_vendor_step_17", progress=0.4)
        ),
    )

    assert statuses == [
        {
            "state": "fault",
            "success": False,
            "reason_code": "invalid_controller_feedback_state",
            "progress": 0.4,
        }
    ]


def test_cancel_recovery_feedback_remains_visible_until_terminal_result():
    bridge = _bare_bridge()
    command = _skill()
    _activate_tool_transfer(bridge, command, cancel_requested=True)
    statuses = []
    bridge._publish_skill_status = lambda command, **kwargs: statuses.append(kwargs)

    bridge._on_tool_transfer_feedback(
        command,
        SimpleNamespace(
            feedback=SimpleNamespace(state="recovering_to_tray", progress=0.8)
        ),
    )

    assert statuses == [
        {
            "state": "recovering_to_tray",
            "success": False,
            "reason_code": "cancel_recovery",
            "progress": 0.8,
        }
    ]


def test_stop_cancels_pending_and_accepted_actions_and_blocks_new_dispatch():
    bridge = _bare_bridge()
    skill = _skill()
    group = _group()
    service_group = _group("suction-1")
    goal_handle = _GoalHandle()
    bridge._active_actions = {
        ("tool_transfer", skill.command_id): ActiveAction(
            route="tool_transfer", command=skill, goal_handle=goal_handle
        ),
        ("retraction", group.command_id): ActiveAction(
            route="retraction", command=group
        ),
    }
    bridge._active_services = {
        ("suction", service_group.command_id): ActiveService(
            route="suction", command=service_group
        )
    }
    statuses = []
    bridge._publish_skill_status = lambda command, **kwargs: statuses.append(
        ("skill", command.command_id, kwargs)
    )
    bridge._publish_group_status = lambda command, **kwargs: statuses.append(
        ("group", command.command_id, kwargs)
    )

    bridge._on_control(SimpleNamespace(data="stop:operator"))

    assert not bridge._runtime_accepting_commands
    assert goal_handle.cancel_calls == 1
    assert all(active.cancelled for active in bridge._active_actions.values())
    assert all(active.cancelled for active in bridge._active_services.values())
    assert bridge._begin_action_dispatch("tool_transfer", _skill("after-stop")) == (
        "runtime_not_accepting_commands"
    )
    assert ("skill", "skill-1", {"state": "cancel_requested", "success": False,
            "reason_code": "cancel_requested_by_runtime_control"}) in statuses
    assert sum(
        1
        for category, _, kwargs in statuses
        if category == "group" and kwargs["outcome"] == "cancelled_by_runtime_control"
    ) == 2


def test_active_command_id_remains_deduplicated_even_after_ledger_eviction():
    bridge = _bare_bridge()
    active = _skill("active-1")
    bridge._active_actions = {
        ("tool_transfer", active.command_id): ActiveAction(
            route="tool_transfer", command=active
        )
    }
    bridge._dispatch_ledger = DispatchLedger(max_entries=1)
    assert bridge._dispatch_ledger.reserve("older-command")
    assert bridge._dispatch_ledger.reserve("newer-command")

    assert bridge._begin_action_dispatch("tool_transfer", active) == "duplicate_command"


def test_next_tool_goal_is_blocked_while_cancel_recovery_is_active():
    bridge = _bare_bridge()
    active = _skill("active-1")
    _activate_tool_transfer(bridge, active, cancel_requested=True)

    assert (
        bridge._begin_action_dispatch(
            "tool_transfer",
            replace(_skill("next-1"), request_generation=5),
        )
        == "tool_transfer_busy"
    )


def test_reset_clears_dedupe_ledger_but_stop_does_not():
    bridge = _bare_bridge()
    assert bridge._dispatch_ledger.reserve("command-1", explicit_request_generation=3)

    bridge._on_control(SimpleNamespace(data="stop"))
    assert not bridge._dispatch_ledger.reserve("command-1", explicit_request_generation=3)

    bridge._on_control(SimpleNamespace(data="reset"))
    assert bridge._dispatch_ledger.reserve("command-1", explicit_request_generation=3)


def test_only_start_or_start_actors_enable_external_dispatch():
    bridge = _bare_bridge()
    bridge._runtime_accepting_commands = False

    bridge._on_control(SimpleNamespace(data="start_runtime"))
    assert not bridge._runtime_accepting_commands
    bridge._on_control(SimpleNamespace(data="start_actors"))
    assert bridge._runtime_accepting_commands
    bridge._on_control(SimpleNamespace(data="stop"))
    assert not bridge._runtime_accepting_commands
    bridge._on_control(SimpleNamespace(data="start"))
    assert bridge._runtime_accepting_commands


def test_completed_handover_event_contains_only_the_dt_reconciliation_fields():
    bridge = _bare_bridge()
    events = []
    bridge._stamp = lambda: Time()
    bridge._skill_event_pub = SimpleNamespace(publish=events.append)

    bridge._publish_tool_transfer_completed_events(
        _skill(), final_state="handover_complete", reason_code="completed"
    )

    assert len(events) == 1
    event = events[0]
    assert event.event_type == "ToolHandoverCompleted"
    assert event.instrument_id == "T04"
    assert event.instance_id == "T04#1"
    assert event.source_location_type == "tray_slot"
    assert event.source_location_id == "tray-a-2"
    assert event.target_location_type == "handover_zone"
    assert event.target_location_id == "surgeon_receive_zone"
    assert event.arm == ""
    assert json.loads(event.detail_json) == {
        "command_id": "skill-1",
        "controller_final_state": "handover_complete",
        "controller_reason_code": "completed",
    }
    assert event.target_owner == ""
    assert not event.cleaning_required
    assert event.mode == ""


def test_completed_retrieve_reconciles_pickup_then_return_to_tray():
    bridge = _bare_bridge()
    events = []
    bridge._stamp = lambda: Time()
    bridge._skill_event_pub = SimpleNamespace(publish=events.append)
    command = replace(
        _skill(),
        action="retrieve_from_mayo",
        source_location_type="mayo_recovery_zone",
        source_location_id="mayo_recovery_zone",
        target_location_type="tray_slot",
        target_location_id="tray-a-2",
    )

    bridge._publish_tool_transfer_completed_events(
        command, final_state="returned_to_tray", reason_code="completed"
    )

    assert [event.event_type for event in events] == [
        "ToolRetrievedFromMayo",
        "ToolReturnedToTray",
    ]
    assert events[0].source_location_id == "mayo_recovery_zone"
    assert events[0].target_location_id == "robot_left_hand"
    assert events[1].source_location_id == "robot_left_hand"
    assert events[1].target_location_id == "tray-a-2"


def test_completed_prepare_records_a_generic_robot_hold_without_an_arm():
    bridge = _bare_bridge()
    events = []
    bridge._stamp = lambda: Time()
    bridge._skill_event_pub = SimpleNamespace(publish=events.append)
    command = replace(
        _skill(),
        action="predict_tool",
        target_location_type="robot_right_hand",
        target_location_id="robot_right_hand",
        mode="anticipatory",
    )

    bridge._publish_tool_prepared_event(
        command, final_state="prepared", reason_code="completed"
    )

    assert len(events) == 1
    event = events[0]
    assert event.event_type == "ToolPrepared"
    assert event.status == "prepared"
    assert event.arm == ""
    assert event.source_location_type == "tray_slot"
    assert event.source_location_id == "tray-a-2"
    assert event.target_location_type == "robot"
    assert event.target_location_id == "robot"
    assert event.target_owner == ""
    assert event.mode == ""


def test_unused_preposition_return_reconciles_robot_to_tray():
    bridge = _bare_bridge()
    events = []
    bridge._stamp = lambda: Time()
    bridge._skill_event_pub = SimpleNamespace(publish=events.append)
    command = replace(
        _skill(),
        action="return_unused_preposition",
        source_location_type="robot_right_hand",
        source_location_id="robot_right_hand",
        target_location_type="tray_slot",
        target_location_id="main_tray_slot_4",
    )

    bridge._publish_tool_transfer_completed_events(
        command, final_state="returned_to_tray", reason_code="completed"
    )

    assert len(events) == 1
    event = events[0]
    assert event.event_type == "UnusedPrepositionReturned"
    assert event.arm == ""
    assert event.source_location_type == "robot"
    assert event.source_location_id == "robot"
    assert event.target_location_type == "tray_slot"
    assert event.target_location_id == "main_tray_slot_4"


def test_failed_or_cancelled_tool_transfer_never_publishes_completion_events():
    bridge = _bare_bridge()
    events = []
    statuses = []
    bridge._publish_tool_transfer_completed_events = (
        lambda *args, **kwargs: events.append((args, kwargs))
    )
    bridge._publish_skill_status = lambda command, **kwargs: statuses.append(kwargs)
    failed_command = _skill()
    _activate_tool_transfer(bridge, failed_command)
    failed_future = SimpleNamespace(
        result=lambda: SimpleNamespace(
            result=SimpleNamespace(
                success=False,
                final_state="failed",
                reason_code="controller_rejected",
            )
        )
    )

    bridge._on_tool_transfer_result(failed_command, failed_future)
    assert events == []
    assert statuses[-1]["reason_code"] == "controller_rejected"

    canceled_command = _skill("cancelled-1")
    _activate_tool_transfer(bridge, canceled_command, cancel_requested=True)
    canceled_future = SimpleNamespace(
        result=lambda: SimpleNamespace(
            result=SimpleNamespace(
                success=False,
                final_state="canceled",
                reason_code="canceled_recovered_to_tray",
            )
        )
    )
    bridge._on_tool_transfer_result(canceled_command, canceled_future)
    assert events == []
    assert statuses[-1] == {
        "state": "canceled",
        "success": False,
        "reason_code": "canceled_recovered_to_tray",
        "progress": 1.0,
    }
    assert (
        bridge._begin_action_dispatch(
            "tool_transfer",
            replace(_skill("after-recovery"), request_generation=6),
        )
        == ""
    )


def test_failed_tray_to_robot_transfer_never_publishes_a_prepared_event():
    bridge = _bare_bridge()
    events = []
    statuses = []
    bridge._publish_tool_transfer_completed_events = (
        lambda *args, **kwargs: events.append((args, kwargs))
    )
    bridge._publish_skill_status = lambda command, **kwargs: statuses.append(kwargs)
    command = replace(_skill(), action="predict_tool")
    _activate_tool_transfer(bridge, command)
    failed_future = SimpleNamespace(
        result=lambda: SimpleNamespace(
            result=SimpleNamespace(
                success=False,
                final_state="failed",
                reason_code="grasp_failed",
            )
        )
    )

    bridge._on_tool_transfer_result(command, failed_future)

    assert events == []
    assert statuses[-1]["reason_code"] == "grasp_failed"


def test_inconsistent_success_result_fails_closed_without_a_dt_event():
    bridge = _bare_bridge()
    events = []
    statuses = []
    bridge._publish_tool_transfer_completed_events = (
        lambda *args, **kwargs: events.append((args, kwargs))
    )
    bridge._publish_skill_status = lambda command, **kwargs: statuses.append(kwargs)
    command = _skill()
    _activate_tool_transfer(bridge, command)
    inconsistent_future = SimpleNamespace(
        result=lambda: SimpleNamespace(
            result=SimpleNamespace(
                success=True,
                final_state="failed",
                reason_code="vendor_specific_success",
            )
        )
    )

    bridge._on_tool_transfer_result(command, inconsistent_future)

    assert events == []
    assert statuses[-1] == {
        "state": "failed",
        "success": False,
        "reason_code": "invalid_controller_result",
        "progress": 1.0,
    }
def test_canceled_result_requires_a_machine_readable_recovery_outcome():
    bridge = _bare_bridge()
    events = []
    statuses = []
    command = _skill()
    _activate_tool_transfer(bridge, command, cancel_requested=True)
    bridge._publish_tool_transfer_completed_events = (
        lambda *args, **kwargs: events.append((args, kwargs))
    )
    bridge._publish_skill_status = lambda command, **kwargs: statuses.append(kwargs)
    ambiguous_future = SimpleNamespace(
        result=lambda: SimpleNamespace(
            result=SimpleNamespace(
                success=False,
                final_state="canceled",
                reason_code="operator_cancel",
            )
        )
    )

    bridge._on_tool_transfer_result(command, ambiguous_future)

    assert events == []
    assert statuses[-1] == {
        "state": "failed",
        "success": False,
        "reason_code": "invalid_controller_result",
        "progress": 1.0,
    }
    assert not bridge._runtime_is_accepting()
