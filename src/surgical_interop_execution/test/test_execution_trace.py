import json
import threading
import time
from collections import OrderedDict
from dataclasses import replace
from types import SimpleNamespace

from action_msgs.msg import GoalStatus
from builtin_interfaces.msg import Time
from std_msgs.msg import String
from surgical_msgs.msg import TwinEvent
from surgical_interop_msgs.srv import ExecuteRetractionCommand
from surgical_interop_execution import bridge as bridge_module

from surgical_interop_execution.bridge import (
    ActiveAction,
    SurgicalInteropExecutionBridge,
)
from surgical_interop_execution.mappings import (
    DispatchLedger,
    InternalGroupCommand,
    InternalSkillCommand,
    RetractionCommandRequest,
    ToolHandoverRequest,
)


class _Future:
    def __init__(self, result=None):
        self._result = result
        self.callbacks = []

    def result(self):
        return self._result

    def add_done_callback(self, callback):
        self.callbacks.append(callback)


class _ActionClient:
    def __init__(self):
        self.future = _Future()
        self.goals = []

    def wait_for_server(self, *, timeout_sec):
        del timeout_sec
        return True

    def send_goal_async(self, goal, *, feedback_callback):
        self.goals.append((goal, feedback_callback))
        return self.future


class _ServiceClient:
    def __init__(self):
        self.future = _Future()
        self.requests = []

    def service_is_ready(self):
        return True

    def call_async(self, request):
        self.requests.append(request)
        return self.future


def _skill(command_id="trace-action", *, procedure_run_id="a" * 32):
    return InternalSkillCommand(
        command_id=command_id,
        action="tool_handover",
        instrument_id="T04",
        instrument_instance_id="T04#1",
        source_location_type="tray",
        source_location_id="tray-a-2",
        target_location_type="surgeon",
        target_location_id="surgeon",
        arm="",
        procedure_run_id=procedure_run_id,
    )


def _group(command_id="trace-service", *, procedure_run_id=""):
    return InternalGroupCommand(
        request_id=f"request-{command_id}",
        command_id=command_id,
        group_id="retraction",
        operation="retraction",
        arm_id="",
        target_tool_id="",
        adjustment_mode="single",
        target_retractor_id="left_malleable",
        direction_frame="surgeon_view",
        direction="left",
        axis="",
        distance_mm=5.0,
        end_effector_profile="left_malleable",
        procedure_run_id=procedure_run_id,
    )


def _trace_bridge():
    bridge = SurgicalInteropExecutionBridge.__new__(SurgicalInteropExecutionBridge)
    traces = []
    announcements = []
    bridge._dispatch_lock = threading.RLock()
    bridge._execution_trace_sequence = 0
    bridge._execution_trace_pub = SimpleNamespace(publish=traces.append)
    bridge._execution_announcement_pub = SimpleNamespace(
        publish=announcements.append
    )
    bridge._execution_announced_commands = OrderedDict()
    bridge._voice_request_targets = OrderedDict()
    bridge._pending_voice_replacement_announcements = OrderedDict()
    bridge._announcement_messages = announcements
    bridge._execution_trace_run_by_command = {}
    bridge._stamp = lambda: Time(sec=42, nanosec=0)
    bridge._runtime_accepting_commands = True
    bridge._latest_simulation_state = SimpleNamespace(
        running=True,
        execution_state="running",
        procedure_run_id="a" * 32,
    )
    bridge._latest_simulation_state_received_monotonic = time.monotonic()
    bridge._dispatch_ledger = DispatchLedger(max_entries=8)
    bridge._active_actions = {}
    bridge._active_services = {}
    bridge._tool_transfer_endpoint = "/surgery/tool_handover"
    bridge._retraction_service_name = "/surgery/retraction/command"
    bridge._retraction_source_id = "taskplanner-test"
    bridge._server_wait_timeout_sec = 0.1
    bridge._publish_skill_status = lambda *args, **kwargs: None
    bridge._publish_group_status = lambda *args, **kwargs: None
    bridge._skill_event_pub = SimpleNamespace(publish=lambda _event: None)
    return bridge, traces


def _patch_trace_message_with_retraction_payload(monkeypatch):
    """Provide the post-build message shape in a source-only unit run."""

    monkeypatch.setattr(
        bridge_module,
        "ExecutionTrace",
        lambda: SimpleNamespace(
            endpoint_source="",
            retraction_command=0,
            retraction_target_side=0,
            retraction_distance_m=0.0,
        ),
    )


def test_execution_trace_is_bounded_and_normalizes_unknown_enum_values():
    bridge, traces = _trace_bridge()
    bridge._publish_execution_trace(
        command_id="x\n" * 200,
        route=" tool_transfer\t",
        transport="not-an-action",
        endpoint="/endpoint\n" * 100,
        stage="not-a-stage",
        dispatch_submitted=True,
        terminal=False,
        evidence="not-evidence",
        reason_code="reason\n" * 100,
    )

    trace = traces[-1]
    assert trace.sequence == 1
    assert trace.stamp.sec == 42
    assert trace.route == "tool_transfer"
    assert trace.transport == "service"
    assert trace.stage == "unknown"
    assert trace.evidence == "response_invalid"
    assert trace.dispatch_submitted is True
    assert "\n" not in trace.command_id
    assert len(trace.command_id) <= 128
    assert len(trace.endpoint) <= 192
    assert trace.reason_code == "unrecognized_reason_code"


def test_action_trace_marks_submission_only_after_send_goal_returns():
    bridge, traces = _trace_bridge()
    client = _ActionClient()
    bridge._tool_transfer_client = client
    command = _skill()
    request = ToolHandoverRequest(
        command_id=command.command_id,
        instrument_id="Forceps",
        instrument_instance_id="Forceps#1",
        source_location="tray",
        target_location="surgeon",
    )

    bridge._dispatch_tool_transfer(command, request)

    assert len(client.goals) == 1
    trace = traces[-1]
    assert trace.command_id == command.command_id
    assert trace.route == "tool_transfer"
    assert trace.transport == "action"
    assert trace.endpoint == "/surgery/tool_handover"
    assert trace.stage == "sent"
    assert trace.dispatch_submitted is True
    assert trace.terminal is False
    assert trace.evidence == "submission_only"


def test_trace_publish_failure_never_interrupts_an_action_dispatch():
    bridge, _ = _trace_bridge()
    client = _ActionClient()

    def _raise_on_publish(_message):
        raise RuntimeError("observer unavailable")

    bridge._execution_trace_pub = SimpleNamespace(publish=_raise_on_publish)
    bridge._tool_transfer_client = client
    command = _skill("trace-observer-failure")
    request = ToolHandoverRequest(
        command_id=command.command_id,
        instrument_id="Forceps",
        instrument_instance_id="Forceps#1",
        source_location="tray",
        target_location="surgeon",
    )

    bridge._dispatch_tool_transfer(command, request)

    assert len(client.goals) == 1
    assert len(client.future.callbacks) == 1


def test_action_trace_distinguishes_goal_acceptance_from_controller_result():
    bridge, traces = _trace_bridge()
    command = _skill()
    bridge._active_actions[("tool_transfer", command.command_id)] = ActiveAction(
        route="tool_transfer", command=command
    )
    result_future = _Future()
    goal = SimpleNamespace(
        accepted=True,
        cancel_goal_async=lambda: None,
        get_result_async=lambda: result_future,
    )

    bridge._on_tool_transfer_goal_response(
        command, _Future(result=goal)
    )
    accepted = traces[-1]
    assert accepted.stage == "accepted"
    assert accepted.evidence == "goal_response"
    assert accepted.terminal is False

    bridge._publish_tool_transfer_completed_events = lambda *args, **kwargs: None
    bridge._on_tool_transfer_result(
        command,
        _Future(
            result=SimpleNamespace(
                status=GoalStatus.STATUS_SUCCEEDED,
                result=SimpleNamespace(
                    success=True,
                    final_state="completed",
                    reason_code="completed",
                ),
            )
        ),
    )
    completed = traces[-1]
    assert completed.stage == "completed"
    assert completed.evidence == "controller_result"
    assert completed.terminal is True


def test_finishing_recovery_announcement_is_marked_as_completion_cleanup():
    bridge, _ = _trace_bridge()
    run_id = "a" * 32
    bridge._latest_simulation_state = SimpleNamespace(
        running=True,
        execution_state="finishing",
        procedure_run_id=run_id,
    )
    command = replace(
        _skill("finish-retrieve-adson", procedure_run_id=run_id),
        action="retrieve_from_mayo",
        instrument_id="T02",
        mode="recovery",
        voice_backed=False,
    )
    bridge._active_actions[("tool_transfer", command.command_id)] = ActiveAction(
        route="tool_transfer", command=command
    )
    trace = SimpleNamespace(
        stage="accepted",
        dispatch_submitted=True,
        command_id=command.command_id,
        procedure_run_id=run_id,
        route="tool_transfer",
    )

    bridge._publish_execution_announcement(trace=trace, retraction_request=None)

    assert len(bridge._announcement_messages) == 1
    announcement = json.loads(bridge._announcement_messages[0].data)
    assert announcement["action"] == "retrieve_from_mayo"
    assert announcement["instrument_id"] == "T02"
    assert announcement["completion_cleanup"] is True


def test_failed_controller_result_preserves_failure_detail_in_task_event():
    bridge, _traces = _trace_bridge()
    command = _skill("trace-failed-with-detail")
    events = []
    statuses = []
    bridge._skill_event_pub = SimpleNamespace(publish=events.append)
    bridge._publish_skill_status = lambda _command, **kwargs: statuses.append(
        kwargs
    )
    bridge._active_actions[("tool_transfer", command.command_id)] = ActiveAction(
        route="tool_transfer",
        command=command,
        task_started_published=True,
    )

    bridge._on_tool_transfer_result(
        command,
        _Future(
            result=SimpleNamespace(
                status=GoalStatus.STATUS_ABORTED,
                result=SimpleNamespace(
                    success=False,
                    final_state="failed",
                    reason_code="grasp_not_confirmed",
                    failure_detail="gripper did not detect the instrument",
                ),
            )
        ),
    )

    assert statuses[-1]["state"] == "failed"
    assert statuses[-1]["success"] is False
    assert [event.event_type for event in events] == ["RobotTaskCompleted"]
    detail = json.loads(events[0].detail_json)
    assert detail["controller_final_state"] == "failed"
    assert detail["controller_reason_code"] == "grasp_not_confirmed"
    assert detail["failure_detail"] == "gripper did not detect the instrument"


def test_invalid_controller_result_keeps_failure_detail_but_never_succeeds():
    bridge, _traces = _trace_bridge()
    command = _skill("trace-invalid-result")
    events = []
    statuses = []
    completed_projections = []
    bridge._skill_event_pub = SimpleNamespace(publish=events.append)
    bridge._publish_skill_status = lambda _command, **kwargs: statuses.append(
        kwargs
    )
    bridge._publish_tool_transfer_completed_events = (
        lambda *_args, **_kwargs: completed_projections.append(True)
    )
    bridge._active_actions[("tool_transfer", command.command_id)] = ActiveAction(
        route="tool_transfer",
        command=command,
        task_started_published=True,
    )

    # The payload says failed, but the ROS terminal status says succeeded.
    # The bridge must retain fail-closed semantics and never project success.
    bridge._on_tool_transfer_result(
        command,
        _Future(
            result=SimpleNamespace(
                status=GoalStatus.STATUS_SUCCEEDED,
                result=SimpleNamespace(
                    success=False,
                    final_state="failed",
                    reason_code="grasp_not_confirmed",
                    failure_detail="instrument was not held at the target",
                ),
            )
        ),
    )

    assert statuses[-1]["state"] == "failed"
    assert statuses[-1]["success"] is False
    assert completed_projections == []
    assert [event.event_type for event in events] == ["RobotTaskCompleted"]
    detail = json.loads(events[0].detail_json)
    assert detail["controller_final_state"] == "failed"
    assert detail["controller_reason_code"] == "invalid_controller_result"
    assert detail["failure_detail"] == "instrument was not held at the target"


def test_voice_replacement_announces_requested_prepare_at_accepted_return() -> None:
    """The parking leg owns the one voice acknowledgement for a replacement."""

    bridge, _ = _trace_bridge()
    run_id = "a" * 32
    return_command = replace(
        _skill("return-bovie", procedure_run_id=run_id),
        action="return_unused_preposition",
        source_location_type="robot_right_hand",
        source_location_id="robot_right_hand",
        target_location_type="mayo_stand",
        target_location_id="mayo_stand",
        mode="explicit_request",
        voice_backed=True,
        request_generation=7,
    )
    bridge._execution_trace_run_by_command[return_command.command_id] = run_id
    bridge._active_actions[("tool_transfer", return_command.command_id)] = ActiveAction(
        route="tool_transfer",
        command=return_command,
    )
    return_goal = SimpleNamespace(
        accepted=True,
        cancel_goal_async=lambda: None,
        get_result_async=lambda: _Future(),
    )
    bridge._on_tool_transfer_goal_response(
        return_command,
        _Future(result=return_goal),
    )

    # A late twin observer event is still safe: it merely completes the
    # presentation context for an already accepted controller Action.
    assert bridge._announcement_messages == []
    event = TwinEvent()
    event.procedure_run_id = run_id
    event.event_type = "SurgeonRequestObserved"
    event.detail_json = json.dumps(
        {
            "active_request_event_type": "voice_request",
            "active_request_generation": 7,
            "active_request_tool": "T08",
        }
    )
    bridge._on_twin_event(event)

    assert len(bridge._announcement_messages) == 1
    return_announcement = json.loads(bridge._announcement_messages[0].data)
    assert return_announcement["command_id"] == "return-bovie"
    assert return_announcement["action"] == "prepare_tool"
    assert return_announcement["instrument_id"] == "T08"
    assert return_announcement["announcement_key"] == f"voice-prepare:{run_id}:7"

    prepare_command = replace(
        return_command,
        command_id="prepare-mosquito",
        action="prepare_tool",
        instrument_id="T08",
        source_location_type="tray",
        source_location_id="tray-a-2",
        target_location_type="robot_right_hand",
        target_location_id="robot_right_hand",
    )
    bridge._execution_trace_run_by_command[prepare_command.command_id] = run_id
    bridge._active_actions[("tool_transfer", prepare_command.command_id)] = ActiveAction(
        route="tool_transfer",
        command=prepare_command,
    )
    prepare_goal = SimpleNamespace(
        accepted=True,
        cancel_goal_async=lambda: None,
        get_result_async=lambda: _Future(),
    )
    bridge._on_tool_transfer_goal_response(
        prepare_command,
        _Future(result=prepare_goal),
    )

    assert len(bridge._announcement_messages) == 2
    prepare_announcement = json.loads(bridge._announcement_messages[1].data)
    assert prepare_announcement["command_id"] == "prepare-mosquito"
    assert prepare_announcement["announcement_key"] == return_announcement[
        "announcement_key"
    ]


def test_service_trace_marks_admission_without_claiming_physical_completion(
    monkeypatch,
):
    # Exercise the new source contract without requiring this focused unit run
    # to rebuild the generated ROS message first.
    _patch_trace_message_with_retraction_payload(monkeypatch)
    bridge, traces = _trace_bridge()
    client = _ServiceClient()
    bridge._retraction_service_client = client
    command = _group()
    request = RetractionCommandRequest(
        command_id=command.command_id,
        command=4,
        target_side=1,
        distance_m=0.005,
    )

    bridge._dispatch_retraction_service(command, request)
    submitted = traces[-1]
    assert submitted.transport == "service"
    assert submitted.stage == "sent"
    assert submitted.dispatch_submitted is True
    assert submitted.evidence == "submission_only"
    assert submitted.retraction_command == 4
    assert submitted.retraction_target_side == 1
    assert submitted.retraction_distance_m == 0.005

    client.future._result = SimpleNamespace(
        request_accepted=True,
        result_code=ExecuteRetractionCommand.Response.RESULT_ACCEPTED,
        command_id=command.command_id,
        message="controller-specific detail is not copied to the trace",
    )
    client.future.callbacks[0](client.future)
    admitted = traces[-1]
    assert admitted.stage == "accepted"
    assert admitted.dispatch_submitted is True
    assert admitted.terminal is True
    assert admitted.evidence == "service_admission_only"
    assert admitted.reason_code == "request_accepted"
    assert admitted.retraction_command == submitted.retraction_command
    assert admitted.retraction_target_side == submitted.retraction_target_side
    assert admitted.retraction_distance_m == submitted.retraction_distance_m


def test_virtual_legacy_service_trace_completes_only_its_transaction(
    monkeypatch,
):
    _patch_trace_message_with_retraction_payload(monkeypatch)
    bridge, traces = _trace_bridge()
    bridge._run_retraction_source = "virtual"
    bridge._retraction_endpoint_source = "virtual"
    bridge._retraction_service_name = (
        "/integration/virtual/surgery/retraction/command"
    )
    client = _ServiceClient()
    bridge._retraction_service_client = client
    command = _group("legacy-virtual-direct-teach")
    request = RetractionCommandRequest(
        command_id=command.command_id,
        command=1,
        target_side=0,
        distance_m=0.0,
    )

    bridge._dispatch_retraction_service(command, request)
    client.future._result = SimpleNamespace(
        request_accepted=True,
        result_code=ExecuteRetractionCommand.Response.RESULT_ACCEPTED,
        command_id=command.command_id,
        message="accepted",
    )
    client.future.callbacks[0](client.future)

    sent, accepted, completed = traces[-3:]
    assert [trace.stage for trace in (sent, accepted, completed)] == [
        "sent",
        "accepted",
        "completed",
    ]
    assert [trace.terminal for trace in (sent, accepted, completed)] == [
        False,
        False,
        True,
    ]
    assert completed.evidence == "virtual_service_transaction_completed"
    assert completed.retraction_command == 1


def test_rejected_service_trace_retains_the_dispatched_retraction_payload(
    monkeypatch,
):
    _patch_trace_message_with_retraction_payload(monkeypatch)
    bridge, traces = _trace_bridge()
    client = _ServiceClient()
    bridge._retraction_service_client = client
    command = _group("trace-service-rejected")
    request = RetractionCommandRequest(
        command_id=command.command_id,
        command=2,
        target_side=0,
        distance_m=0.0,
    )

    bridge._dispatch_retraction_service(command, request)
    client.future._result = SimpleNamespace(
        request_accepted=False,
        result_code=ExecuteRetractionCommand.Response.RESULT_REJECTED,
        command_id=command.command_id,
        message="rejected",
    )
    client.future.callbacks[0](client.future)

    submitted, rejected = traces[-2:]
    assert submitted.stage == "sent"
    assert rejected.stage == "rejected"
    assert rejected.retraction_command == submitted.retraction_command == 2
    assert rejected.retraction_target_side == submitted.retraction_target_side == 0
    assert rejected.retraction_distance_m == submitted.retraction_distance_m == 0.0


def test_bridge_projects_virtual_proxy_service_lifecycle_with_one_public_sequence(
    monkeypatch,
):
    _patch_trace_message_with_retraction_payload(monkeypatch)
    bridge, traces = _trace_bridge()
    # Exercise the mixed-route case: tool handover is external while the
    # independently selected retraction Service is virtual.
    bridge._run_endpoint_source = "external"
    bridge._robot_endpoint_source = "external"
    bridge._run_retraction_source = "virtual"
    bridge._retraction_endpoint_source = "virtual"

    run_id = "a" * 32

    def lifecycle(stage: str, *, terminal: bool, evidence: str, reason: str):
        return String(
            data=json.dumps(
                {
                    "schema": "taskplanner.execution_proxy_lifecycle.v1",
                    "command_id": "voice-direct-teach-1",
                    "route": "retraction",
                    "transport": "service",
                    "endpoint": "/integration/virtual/surgery/retraction/command",
                    "endpoint_source": "virtual",
                    "stage": stage,
                    "dispatch_submitted": True,
                    "terminal": terminal,
                    "evidence": evidence,
                    "reason_code": reason,
                    "procedure_run_id": run_id,
                    "retraction_command": 1,
                    "retraction_target_side": 0,
                    "retraction_distance_m": 0.0,
                }
            )
        )

    bridge._on_execution_proxy_lifecycle(
        lifecycle(
            "sent",
            terminal=False,
            evidence="submission_only",
            reason="service_call_submitted",
        )
    )
    bridge._on_execution_proxy_lifecycle(
        lifecycle(
            "accepted",
            terminal=False,
            evidence="service_admission_only",
            reason="request_accepted",
        )
    )
    bridge._on_execution_proxy_lifecycle(
        lifecycle(
            "completed",
            terminal=True,
            evidence="virtual_service_transaction_completed",
            reason="virtual_service_completed",
        )
    )

    assert [trace.sequence for trace in traces] == [1, 2, 3]
    assert [trace.stage for trace in traces] == ["sent", "accepted", "completed"]
    assert all(trace.command_id == "voice-direct-teach-1" for trace in traces)
    assert all(trace.endpoint_source == "virtual" for trace in traces)
    assert all(trace.retraction_command == 1 for trace in traces)
    assert all(trace.procedure_run_id == run_id for trace in traces)
    assert traces[-1].evidence == "virtual_service_transaction_completed"
    # Exactly the endpoint-accepted Service fact reaches TTS. Sent and the
    # virtual transaction-complete observation are deliberately silent.
    assert len(bridge._announcement_messages) == 1
    announcement = json.loads(bridge._announcement_messages[0].data)
    assert announcement["command_id"] == "voice-direct-teach-1"
    assert announcement["procedure_run_id"] == run_id
    assert announcement["retraction_command"] == 1


def test_bridge_rejects_nonvirtual_proxy_completion_claim(
    monkeypatch,
):
    _patch_trace_message_with_retraction_payload(monkeypatch)
    bridge, traces = _trace_bridge()

    bridge._on_execution_proxy_lifecycle(
        String(
            data=json.dumps(
                {
                    "schema": "taskplanner.execution_proxy_lifecycle.v1",
                    "command_id": "external-completion-claim",
                    "route": "retraction",
                    "transport": "service",
                    "endpoint": "/surgery/retraction/command",
                    "endpoint_source": "external",
                    "stage": "completed",
                    "dispatch_submitted": True,
                    "terminal": True,
                    "evidence": "virtual_service_transaction_completed",
                    "reason_code": "virtual_service_completed",
                    "retraction_command": 1,
                    "retraction_target_side": 0,
                    "retraction_distance_m": 0.0,
                }
            )
        )
    )

    assert traces == []
