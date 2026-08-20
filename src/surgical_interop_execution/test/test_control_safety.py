import json
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

from action_msgs.msg import GoalStatus
from builtin_interfaces.msg import Time
from surgical_interop_msgs.action import ExecuteToolHandover
from surgical_interop_msgs.msg import BedRobotArmState, BedRobotArmStateArray
from surgical_interop_msgs.srv import ExecuteRetractionCommand

from surgical_interop_execution.bridge import (
    ActiveAction,
    ActiveService,
    SurgicalInteropExecutionBridge,
)
from surgical_interop_execution.mappings import (
    DispatchLedger,
    InternalGroupCommand,
    InternalSkillCommand,
    RETRACTION_COMMAND_ADJUST_RETRACTION,
    RETRACTION_COMMAND_CHANGE_TOOL,
    RETRACTION_COMMAND_STOP_RETRACTION,
    RETRACTION_TARGET_LEFT,
    RETRACTION_TARGET_NONE,
    RETRACTION_TARGET_RIGHT,
    RetractionCommandRequest,
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
        arm_id="",
        target_tool_id="",
        adjustment_mode="single",
        target_retractor_id="left_malleable",
        direction_frame="surgeon_view",
        direction="left",
        axis="none",
        distance_mm=5.0,
        end_effector_profile="left_malleable",
    )


def _tool_change_group(command_id: str = "tool-change-1") -> InternalGroupCommand:
    return replace(
        _group(command_id),
        operation="change_end_effector",
        arm_id="arm_1",
        target_tool_id="army_navy_retractor",
        adjustment_mode="",
        target_retractor_id="",
        direction_frame="",
        direction="",
        axis="",
        distance_mm=0.0,
        end_effector_profile="army_navy_retractor",
    )


def _retraction_request(
    command_id: str = "adjust-1",
    *,
    target_side: int = RETRACTION_TARGET_LEFT,
    command: int = RETRACTION_COMMAND_ADJUST_RETRACTION,
    distance_m: float = 0.005,
) -> RetractionCommandRequest:
    return RetractionCommandRequest(
        command_id=command_id,
        command=command,
        target_side=target_side,
        distance_m=distance_m,
    )


class _GoalHandle:
    def __init__(self):
        self.cancel_calls = 0
        self.accepted = True
        self.result_future = SimpleNamespace(add_done_callback=lambda callback: None)

    def cancel_goal_async(self):
        self.cancel_calls += 1

    def get_result_async(self):
        return self.result_future


def _bare_bridge() -> SurgicalInteropExecutionBridge:
    bridge = SurgicalInteropExecutionBridge.__new__(SurgicalInteropExecutionBridge)
    bridge._dispatch_lock = threading.RLock()
    bridge._runtime_accepting_commands = True
    bridge._retraction_source_id = "taskplanner-test"
    bridge._dispatch_ledger = DispatchLedger(max_entries=8)
    bridge._active_actions = {}
    bridge._active_services = {}
    bridge._require_bed_robot_status = True
    bridge._bed_robot_status_timeout_sec = 2.0
    # Most unit fixtures use small synthetic timestamps to test ordering. Tests
    # that exercise absolute freshness override this with the production limit.
    bridge._bed_robot_source_max_age_sec = 10_000_000_000.0
    bridge._bed_robot_source_future_tolerance_sec = 0.5
    bridge._bed_robot_revision = None
    bridge._bed_robot_source_stamp_ns = None
    bridge._bed_robot_epoch = 0
    bridge._bed_robot_signature = None
    bridge._bed_robot_procedure_type = ""
    bridge._bed_robot_received_monotonic = 0.0
    bridge._bed_robot_states = {}
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


def test_stop_requests_action_cancel_and_keeps_retraction_service_in_flight():
    bridge = _bare_bridge()
    skill = _skill()
    service_group = _group()
    goal_handle = _GoalHandle()
    bridge._active_actions = {
        ("tool_transfer", skill.command_id): ActiveAction(
            route="tool_transfer", command=skill, goal_handle=goal_handle
        )
    }
    bridge._active_services = {
        ("retraction", service_group.command_id): ActiveService(
            route="retraction", command=service_group, dispatched=True
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
    assert set(bridge._active_actions) == {("tool_transfer", skill.command_id)}
    assert set(bridge._active_services) == {
        ("retraction", service_group.command_id)
    }
    assert bridge._begin_action_dispatch("tool_transfer", _skill("after-stop")) == (
        "runtime_not_accepting_commands"
    )
    assert ("skill", "skill-1", {"state": "cancel_requested", "success": False,
            "reason_code": "cancel_requested_by_runtime_control"}) in statuses
    assert ("group", service_group.command_id, {
        "state": "unknown",
        "outcome": "awaiting_service_admission_after_stop",
        "terminal": False,
        "success": False,
        "reason_code": "service_not_cancellable_awaiting_response",
    }) in statuses


def test_retraction_service_acceptance_after_stop_preserves_unknown_physical_state():
    bridge = _bare_bridge()
    command = _group("adjust-cancel-1")
    bridge._active_services[("retraction", command.command_id)] = ActiveService(
        route="retraction",
        command=command,
        cancelled=True,
        dispatched=True,
    )
    statuses = []
    bridge._publish_group_status = lambda command, **kwargs: statuses.append(kwargs)

    bridge._on_retraction_service_result(
        command,
        SimpleNamespace(
            result=lambda: SimpleNamespace(
                request_accepted=True,
                result_code=ExecuteRetractionCommand.Response.RESULT_ACCEPTED,
                command_id=command.command_id,
                message="controller_received",
            )
        ),
    )

    assert ("retraction", command.command_id) not in bridge._active_services
    assert not bridge._runtime_is_accepting()
    assert statuses == [{
        "state": "unknown",
        "outcome": "accepted_after_stop",
        "terminal": False,
        "success": False,
        "reason_code": "controller_received",
    }]


def test_retraction_service_response_loss_keeps_lane_locked_and_nonterminal():
    bridge = _bare_bridge()
    command = _group("adjust-response-lost")
    bridge._active_services[("retraction", command.command_id)] = ActiveService(
        route="retraction", command=command
    )
    statuses = []
    bridge._publish_group_status = lambda command, **kwargs: statuses.append(kwargs)

    def _raise_transport_error():
        raise RuntimeError("service response lost")

    bridge._on_retraction_service_result(
        command, SimpleNamespace(result=_raise_transport_error)
    )

    assert ("retraction", command.command_id) in bridge._active_services
    assert not bridge._runtime_is_accepting()
    assert statuses == [{
        "state": "unknown",
        "outcome": "remote_state_unknown",
        "terminal": False,
        "success": False,
        "reason_code": "service_response_unavailable",
    }]


def test_tool_transfer_goal_response_loss_keeps_lane_locked():
    bridge = _bare_bridge()
    command = _skill("handover-response-lost")
    _activate_tool_transfer(bridge, command)
    statuses = []
    bridge._publish_skill_status = lambda command, **kwargs: statuses.append(kwargs)

    def _raise_transport_error():
        raise RuntimeError("goal response lost")

    bridge._on_tool_transfer_goal_response(
        command, SimpleNamespace(result=_raise_transport_error)
    )

    assert ("tool_transfer", command.command_id) in bridge._active_actions
    assert not bridge._runtime_is_accepting()
    assert statuses == [{
        "state": "unknown",
        "success": False,
        "reason_code": "goal_response_unavailable",
        "progress": 0.0,
    }]


def test_retraction_service_admission_is_transport_terminal_not_physical_completion():
    bridge = _bare_bridge()
    command = _tool_change_group("change-admitted")
    bridge._active_services[("retraction", command.command_id)] = ActiveService(
        route="retraction",
        command=command,
        dispatched=True,
        future=object(),
    )
    statuses = []
    bridge._publish_group_status = lambda command, **kwargs: statuses.append(kwargs)

    bridge._on_retraction_service_result(
        command,
        SimpleNamespace(
            result=lambda: SimpleNamespace(
                request_accepted=True,
                result_code=ExecuteRetractionCommand.Response.RESULT_ACCEPTED,
                command_id=command.command_id,
                message="accepted_for_controller_execution",
            )
        ),
    )

    assert ("retraction", command.command_id) not in bridge._active_services
    assert statuses == [{
        "state": "accepted",
        "outcome": "accepted",
        "terminal": True,
        "success": True,
        "reason_code": "accepted_for_controller_execution",
    }]


def test_missing_retraction_service_response_after_stop_remains_nonterminal_and_tracked():
    bridge = _bare_bridge()
    command = _tool_change_group("change-unknown")
    bridge._active_services[("retraction", command.command_id)] = ActiveService(
        route="retraction",
        command=command,
        cancelled=True,
        dispatched=True,
        future=object(),
    )
    statuses = []
    bridge._publish_group_status = lambda command, **kwargs: statuses.append(kwargs)

    bridge._on_retraction_service_result(
        command,
        SimpleNamespace(result=lambda: None),
    )

    assert ("retraction", command.command_id) in bridge._active_services
    assert not bridge._runtime_is_accepting()
    assert statuses == [{
        "state": "unknown",
        "outcome": "remote_state_unknown",
        "terminal": False,
        "success": False,
        "reason_code": "service_response_unavailable_after_stop",
    }]


def test_invalid_retraction_service_response_remains_nonterminal_and_tracked():
    bridge = _bare_bridge()
    command = _group("adjust-unknown")
    bridge._active_services[("retraction", command.command_id)] = ActiveService(
        route="retraction",
        command=command,
    )
    statuses = []
    bridge._publish_group_status = lambda command, **kwargs: statuses.append(kwargs)

    bridge._on_retraction_service_result(
        command,
        SimpleNamespace(
            result=lambda: SimpleNamespace(
                request_accepted=True,
                result_code=ExecuteRetractionCommand.Response.RESULT_REJECTED,
                command_id=command.command_id,
                message="inconsistent",
            )
        ),
    )

    assert ("retraction", command.command_id) in bridge._active_services
    assert not bridge._runtime_is_accepting()
    assert statuses == [{
        "state": "unknown",
        "outcome": "remote_state_unknown",
        "terminal": False,
        "success": False,
        "reason_code": "invalid_service_response",
    }]


def test_tool_change_admission_status_never_claims_physical_attachment():
    bridge = _bare_bridge()
    command = _tool_change_group()
    published = []
    bridge._stamp = lambda: Time()
    bridge._group_status_pub = SimpleNamespace(publish=published.append)

    bridge._publish_group_status(
        command,
        state="accepted",
        outcome="accepted",
        terminal=True,
        success=True,
        reason_code="request_accepted",
    )

    assert published[0].target_tool_id == "army_navy_retractor"
    assert published[0].end_effector_profile == ""


def test_retraction_service_request_uses_only_the_reviewed_fields():
    bridge = _bare_bridge()
    service_request = bridge._retraction_service_request(
        _retraction_request("service-request-1", distance_m=0.050)
    )

    assert service_request.protocol_version == 1
    assert service_request.source_id == "taskplanner-test"
    assert service_request.command_id == "service-request-1"
    assert (
        service_request.command
        == ExecuteRetractionCommand.Request.COMMAND_ADJUST_RETRACTION
    )
    assert service_request.target_side == ExecuteRetractionCommand.Request.TARGET_LEFT
    assert service_request.distance_m == 0.050


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


def test_reset_is_repeatable_and_reopens_the_next_start_edge():
    bridge = _bare_bridge()
    bridge._runtime_accepting_commands = False
    bridge._last_lifecycle_control_signature = None
    bridge._bed_robot_revision = 7

    bridge._on_control(SimpleNamespace(data="start"))
    bridge._on_control(SimpleNamespace(data="start"))
    bridge._on_control(SimpleNamespace(data="reset"))
    assert bridge._bed_robot_revision is None
    bridge._bed_robot_revision = 9
    bridge._on_control(SimpleNamespace(data="reset"))
    assert bridge._bed_robot_revision is None
    bridge._on_control(SimpleNamespace(data="start"))

    assert bridge._runtime_accepting_commands
    assert bridge._last_lifecycle_control_signature == ("start", "")


def test_full_lifecycle_transport_duplicates_are_edge_idempotent():
    bridge = _bare_bridge()
    bridge._runtime_accepting_commands = False
    bridge._last_lifecycle_control_signature = None
    reset_calls: list[bool] = []
    bridge._dispatch_ledger = SimpleNamespace(
        clear=lambda: reset_calls.append(True)
    )

    for control in (
        "reset",
        "reset",
        "start_runtime:P03",
        "start_runtime:P03",
        "start_actors:P03",
        "start_actors:P03",
        "pause",
        "pause",
        "resume",
        "resume",
        "stop",
        "stop",
    ):
        bridge._on_control(SimpleNamespace(data=control))

    assert reset_calls == [True, True]
    assert bridge._runtime_accepting_commands is False
    assert bridge._last_lifecycle_control_signature == ("stop", "")


def _bed_robot_snapshot(
    revision: int = 1,
    *,
    state: str = "standby",
    direct_teach_active: bool = False,
    procedure_type: str = "nephrectomy",
    stamp_ns: int = 1_000_000_000,
) -> BedRobotArmStateArray:
    snapshot = BedRobotArmStateArray()
    snapshot.stamp.sec = stamp_ns // 1_000_000_000
    snapshot.stamp.nanosec = stamp_ns % 1_000_000_000
    snapshot.revision = revision
    snapshot.procedure_type = procedure_type
    layout = (
        (("arm_1", "army_navy"),)
        if procedure_type == "thyroidectomy"
        else (
            ("arm_1", "left_malleable"),
            ("arm_2", "right_malleable"),
        )
    )
    for arm_id, role_instance_id in layout:
        arm = BedRobotArmState()
        arm.arm_id = arm_id
        arm.role = "retraction"
        arm.role_instance_id = role_instance_id
        arm.state = state
        arm.direct_teach_active = direct_teach_active
        arm.reason_code = "ok"
        snapshot.arms.append(arm)
    return snapshot


def test_bed_robot_status_requires_monotonic_valid_controller_snapshots():
    bridge = _bare_bridge()
    bridge.get_logger = lambda: SimpleNamespace(warning=lambda *_: None)

    bridge._on_bed_robot_status(_bed_robot_snapshot(2, stamp_ns=2_000_000_000))
    accepted_at = bridge._bed_robot_received_monotonic
    assert bridge._bed_robot_revision == 2
    assert set(bridge._bed_robot_states) == {"arm_1", "arm_2"}

    bridge._on_bed_robot_status(_bed_robot_snapshot(1, stamp_ns=1_000_000_000))
    assert bridge._bed_robot_revision == 2
    assert bridge._bed_robot_received_monotonic == accepted_at

    inconsistent = _bed_robot_snapshot(
        3,
        state="standby",
        direct_teach_active=True,
        stamp_ns=3_000_000_000,
    )
    bridge._on_bed_robot_status(inconsistent)
    assert bridge._bed_robot_revision == 2

    invalid_layout = _bed_robot_snapshot(3, stamp_ns=3_000_000_000)
    invalid_layout.procedure_type = "thyroidectomy"
    bridge._on_bed_robot_status(invalid_layout)
    assert bridge._bed_robot_revision == 2


def test_bed_robot_status_rejects_stale_and_future_source_time():
    bridge = _bare_bridge()
    bridge.get_logger = lambda: SimpleNamespace(warning=lambda *_: None)
    bridge._bed_robot_source_max_age_sec = 2.0
    bridge._bed_robot_source_future_tolerance_sec = 0.5
    bridge._wall_time_ns = lambda: 10_000_000_000

    bridge._on_bed_robot_status(
        _bed_robot_snapshot(1, stamp_ns=7_000_000_000)
    )
    assert bridge._bed_robot_states == {}

    bridge._on_bed_robot_status(
        _bed_robot_snapshot(1, stamp_ns=11_000_000_000)
    )
    assert bridge._bed_robot_states == {}

    bridge._on_bed_robot_status(
        _bed_robot_snapshot(1, stamp_ns=9_000_000_000)
    )
    assert set(bridge._bed_robot_states) == {"arm_1", "arm_2"}


def test_dispatch_guard_rechecks_source_time_after_reception():
    bridge = _bare_bridge()
    snapshot = _bed_robot_snapshot()
    bridge._bed_robot_states = {arm.arm_id: arm for arm in snapshot.arms}
    bridge._bed_robot_procedure_type = "nephrectomy"
    bridge._bed_robot_received_monotonic = time.monotonic()
    bridge._bed_robot_source_max_age_sec = 2.0
    bridge._wall_time_ns = lambda: 10_000_000_000
    bridge._bed_robot_source_stamp_ns = 7_000_000_000
    request = _retraction_request("adjust-stale-source")

    assert (
        bridge._bed_robot_dispatch_guard(request)
        == "bed_robot_source_stamp_stale"
    )


def test_bed_robot_status_accepts_heartbeat_and_new_controller_epoch():
    bridge = _bare_bridge()
    bridge.get_logger = lambda: SimpleNamespace(warning=lambda *_: None)

    bridge._on_bed_robot_status(_bed_robot_snapshot(9, stamp_ns=9_000_000_000))
    bridge._on_bed_robot_status(_bed_robot_snapshot(9, stamp_ns=10_000_000_000))
    assert bridge._bed_robot_revision == 9
    assert bridge._bed_robot_source_stamp_ns == 10_000_000_000
    assert bridge._bed_robot_epoch == 0

    bridge._on_bed_robot_status(_bed_robot_snapshot(1, stamp_ns=11_000_000_000))
    assert bridge._bed_robot_revision == 1
    assert bridge._bed_robot_source_stamp_ns == 11_000_000_000
    assert bridge._bed_robot_epoch == 1


def test_controller_restart_during_command_preserves_tracking_and_blocks_dispatch():
    bridge = _bare_bridge()
    bridge.get_logger = lambda: SimpleNamespace(warning=lambda *_: None)
    command = _group("adjust-during-restart")
    bridge._active_services[("retraction", command.command_id)] = ActiveService(
        route="retraction",
        command=command,
    )
    statuses = []
    bridge._publish_group_status = lambda command, **kwargs: statuses.append(kwargs)

    bridge._on_bed_robot_status(_bed_robot_snapshot(8, stamp_ns=8_000_000_000))
    bridge._on_bed_robot_status(_bed_robot_snapshot(1, stamp_ns=9_000_000_000))

    assert ("retraction", command.command_id) in bridge._active_services
    assert not bridge._runtime_is_accepting()
    assert statuses == [{
        "state": "unknown",
        "outcome": "remote_state_unknown",
        "terminal": False,
        "success": False,
        "reason_code": "controller_restarted_during_command",
    }]


def test_bed_robot_status_rejects_changed_payload_without_revision_advance():
    bridge = _bare_bridge()
    bridge.get_logger = lambda: SimpleNamespace(warning=lambda *_: None)

    bridge._on_bed_robot_status(_bed_robot_snapshot(4, stamp_ns=4_000_000_000))
    bridge._on_bed_robot_status(
        _bed_robot_snapshot(
            4,
            state="retracting",
            stamp_ns=5_000_000_000,
        )
    )

    assert bridge._bed_robot_revision == 4
    assert bridge._bed_robot_source_stamp_ns == 4_000_000_000
    assert all(arm.state == "standby" for arm in bridge._bed_robot_states.values())


def test_dispatch_guard_fails_closed_on_missing_stale_and_direct_teach_status():
    bridge = _bare_bridge()
    request = _retraction_request()
    assert bridge._bed_robot_dispatch_guard(request) == "bed_robot_status_missing"

    bridge._bed_robot_states = {"arm_1": _bed_robot_snapshot().arms[0]}
    bridge._bed_robot_procedure_type = "nephrectomy"
    bridge._bed_robot_received_monotonic = time.monotonic() - 3.0
    assert bridge._bed_robot_dispatch_guard(request) == "bed_robot_status_stale"

    direct_teach = _bed_robot_snapshot(
        state="direct_teach", direct_teach_active=True
    ).arms[0]
    bridge._bed_robot_states = {"arm_1": direct_teach}
    bridge._bed_robot_procedure_type = "nephrectomy"
    bridge._bed_robot_received_monotonic = time.monotonic()
    bridge._bed_robot_source_stamp_ns = time.time_ns()
    assert bridge._bed_robot_dispatch_guard(request) == "direct_teach_active"


def test_dispatch_guard_accepts_only_a_fresh_standby_target():
    bridge = _bare_bridge()
    snapshot = _bed_robot_snapshot()
    bridge._bed_robot_states = {arm.arm_id: arm for arm in snapshot.arms}
    bridge._bed_robot_procedure_type = "nephrectomy"
    bridge._bed_robot_received_monotonic = time.monotonic()
    bridge._bed_robot_source_stamp_ns = time.time_ns()
    request = _retraction_request()
    assert bridge._bed_robot_dispatch_guard(request) == ""


def test_retraction_adjustment_accepts_controller_retracting_state():
    bridge = _bare_bridge()
    snapshot = _bed_robot_snapshot(state="retracting")
    bridge._bed_robot_states = {arm.arm_id: arm for arm in snapshot.arms}
    bridge._bed_robot_procedure_type = "nephrectomy"
    bridge._bed_robot_received_monotonic = time.monotonic()
    bridge._bed_robot_source_stamp_ns = time.time_ns()
    request = _retraction_request(
        "adjust-active",
        target_side=RETRACTION_TARGET_RIGHT,
        distance_m=0.003,
    )

    assert bridge._bed_robot_dispatch_guard(request) == ""


def test_tool_change_only_requires_fresh_generic_controller_status():
    bridge = _bare_bridge()
    snapshot = _bed_robot_snapshot(procedure_type="thyroidectomy")
    bridge._bed_robot_states = {arm.arm_id: arm for arm in snapshot.arms}
    bridge._bed_robot_procedure_type = "thyroidectomy"
    bridge._bed_robot_received_monotonic = time.monotonic()
    bridge._bed_robot_source_stamp_ns = time.time_ns()
    request = _retraction_request(
        "change-1",
        command=RETRACTION_COMMAND_CHANGE_TOOL,
        target_side=RETRACTION_TARGET_NONE,
        distance_m=0.0,
    )

    assert bridge._bed_robot_dispatch_guard(request) == ""
    bridge._bed_robot_states["arm_1"].state = "retracting"
    assert bridge._bed_robot_dispatch_guard(request) == ""


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


def test_completed_mayo_prepare_preserves_mayo_origin_for_the_digital_twin():
    bridge = _bare_bridge()
    events = []
    bridge._stamp = lambda: Time()
    bridge._skill_event_pub = SimpleNamespace(publish=events.append)
    command = replace(
        _skill(),
        action="predict_tool",
        source_location_type="mayo_reuse_zone",
        source_location_id="mayo_reuse_zone",
        target_location_type="robot_right_hand",
        target_location_id="robot_right_hand",
        mode="anticipatory",
    )

    bridge._publish_tool_transfer_completed_events(
        command, final_state="completed", reason_code="completed"
    )

    assert len(events) == 1
    event = events[0]
    assert event.event_type == "ToolPrepared"
    assert event.source_location_type == "mayo_reuse_zone"
    assert event.source_location_id == "mayo_reuse_zone"
    assert event.target_location_type == "robot"
    assert event.target_location_id == "robot"


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


def test_unused_mayo_preposition_return_uses_the_public_tray_recovery_semantics():
    bridge = _bare_bridge()
    events = []
    bridge._stamp = lambda: Time()
    bridge._skill_event_pub = SimpleNamespace(publish=events.append)
    command = replace(
        _skill(),
        action="return_unused_preposition",
        source_location_type="robot_right_hand",
        source_location_id="robot_right_hand",
        target_location_type="mayo_reuse_zone",
        target_location_id="mayo_reuse_zone",
    )

    bridge._publish_tool_transfer_completed_events(
        command, final_state="returned_to_tray", reason_code="completed"
    )

    assert len(events) == 1
    event = events[0]
    assert event.event_type == "UnusedPrepositionReturned"
    assert event.source_location_type == "robot"
    assert event.source_location_id == "robot"
    assert event.target_location_type == "tray"
    assert event.target_location_id == "tray"


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
            status=GoalStatus.STATUS_ABORTED,
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
            status=GoalStatus.STATUS_CANCELED,
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


def test_retraction_service_response_must_match_admission_contract():
    bridge = _bare_bridge()
    command = _group("adjust-status-mismatch")
    bridge._active_services[("retraction", command.command_id)] = ActiveService(
        route="retraction", command=command
    )
    statuses = []
    bridge._publish_group_status = lambda command, **kwargs: statuses.append(kwargs)

    bridge._on_retraction_service_result(
        command,
        SimpleNamespace(
            result=lambda: SimpleNamespace(
                request_accepted=True,
                result_code=ExecuteRetractionCommand.Response.RESULT_REJECTED,
                command_id=command.command_id,
                message="inconsistent",
            )
        ),
    )

    assert ("retraction", command.command_id) in bridge._active_services
    assert not bridge._runtime_is_accepting()
    assert statuses[-1] == {
        "state": "unknown",
        "outcome": "remote_state_unknown",
        "terminal": False,
        "success": False,
        "reason_code": "invalid_service_response",
    }


def test_action_terminal_status_must_match_tool_transfer_payload():
    bridge = _bare_bridge()
    command = _skill("handover-status-mismatch")
    _activate_tool_transfer(bridge, command)
    events = []
    statuses = []
    bridge._publish_tool_transfer_completed_events = (
        lambda *args, **kwargs: events.append((args, kwargs))
    )
    bridge._publish_skill_status = lambda command, **kwargs: statuses.append(kwargs)

    bridge._on_tool_transfer_result(
        command,
        SimpleNamespace(
            result=lambda: SimpleNamespace(
                status=GoalStatus.STATUS_ABORTED,
                result=SimpleNamespace(
                    success=True,
                    final_state="completed",
                    reason_code="completed",
                ),
            )
        ),
    )

    assert events == []
    assert ("tool_transfer", command.command_id) not in bridge._active_actions
    assert not bridge._runtime_is_accepting()
    assert statuses[-1] == {
        "state": "failed",
        "success": False,
        "reason_code": "invalid_controller_result",
        "progress": 1.0,
    }


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
            status=GoalStatus.STATUS_ABORTED,
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
            status=GoalStatus.STATUS_ABORTED,
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
            status=GoalStatus.STATUS_CANCELED,
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
