import threading
from types import SimpleNamespace

from action_msgs.msg import GoalStatus
from builtin_interfaces.msg import Time
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

    def wait_for_service(self, *, timeout_sec):
        del timeout_sec
        return True

    def call_async(self, request):
        self.requests.append(request)
        return self.future


def _skill(command_id="trace-action"):
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
    )


def _group(command_id="trace-service"):
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
    )


def _trace_bridge():
    bridge = SurgicalInteropExecutionBridge.__new__(SurgicalInteropExecutionBridge)
    traces = []
    bridge._dispatch_lock = threading.RLock()
    bridge._execution_trace_sequence = 0
    bridge._execution_trace_pub = SimpleNamespace(publish=traces.append)
    bridge._stamp = lambda: Time(sec=42, nanosec=0)
    bridge._runtime_accepting_commands = True
    bridge._dispatch_ledger = DispatchLedger(max_entries=8)
    bridge._active_actions = {}
    bridge._active_services = {}
    bridge._tool_transfer_endpoint = "/surgery/tool_handover"
    bridge._retraction_service_name = "/surgery/retraction/command"
    bridge._retraction_source_id = "taskplanner-test"
    bridge._server_wait_timeout_sec = 0.1
    bridge._dispatch_admission_guard = lambda *args, **kwargs: ""
    bridge._publish_skill_status = lambda *args, **kwargs: None
    bridge._publish_group_status = lambda *args, **kwargs: None
    return bridge, traces


def _patch_trace_message_with_retraction_payload(monkeypatch):
    """Provide the post-build message shape in a source-only unit run."""

    monkeypatch.setattr(
        bridge_module,
        "ExecutionTrace",
        lambda: SimpleNamespace(
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
