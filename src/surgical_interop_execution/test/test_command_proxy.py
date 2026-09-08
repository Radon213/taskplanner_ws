from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

from rclpy.action import CancelResponse, GoalResponse

from surgical_interop_execution.command_proxy import (
    ExecutionCommandProxy,
    retraction_request_allowed_by_scenario,
    validate_retraction_proxy_request,
    validate_tool_handover_proxy_goal,
)
from surgical_interop_msgs.action import ExecuteToolHandover
from surgical_interop_msgs.srv import ExecuteRetractionCommand


def test_retraction_proxy_keeps_v1_and_endpoint_payload_boundary() -> None:
    request = SimpleNamespace(
        protocol_version=ExecuteRetractionCommand.Request.PROTOCOL_VERSION_V1,
        source_id="taskplanner",
        command_id="voice-suction-abc",
        distance_m=0.0,
    )

    assert validate_retraction_proxy_request(request) == ""
    request.protocol_version = 2
    assert validate_retraction_proxy_request(request) == "unsupported_protocol_version"
    request.protocol_version = 1
    request.distance_m = float("nan")
    assert validate_retraction_proxy_request(request) == "invalid_distance_m"


def test_tool_handover_proxy_accepts_only_complete_typed_goal_shape() -> None:
    goal = SimpleNamespace(
        command_id="handover-abc",
        instrument_id="T04",
        instrument_instance_id="T04-1",
        source_location="tray",
        target_location="surgeon",
    )

    assert validate_tool_handover_proxy_goal(goal) == ""
    goal.target_location = ""
    assert validate_tool_handover_proxy_goal(goal) == "missing_target_location"


def test_proxy_keeps_bridge_selected_external_retraction_when_local_workflow_is_suppressed() -> None:
    """The transport proxy must not re-admit the bridge's scenario policy."""

    proxy = ExecutionCommandProxy.__new__(ExecutionCommandProxy)
    proxy._lock = threading.RLock()
    proxy._route_state = None
    proxy._route_key = None

    proxy._on_route_state(
        SimpleNamespace(
            data=json.dumps(
                {
                    "schema": "taskplanner.execution_route_state.v1",
                    "revision": 4,
                    "initialization_revision": 9,
                    "selected_source": "virtual",
                    "run_endpoint_source": "virtual",
                    "retraction_source": "external",
                    "run_retraction_source": "external",
                    "initialization_state": "running",
                    "tool_handover_endpoint": "/integration/virtual/surgery/tool_handover",
                    "retraction_service_name": "/surgery/retraction/command",
                    "controller_contract_topic": "/integration/virtual/surgery/controller_contract",
                    "expected_controller_contract_id": "taskplanner-virtual-eir-nuc.v1",
                    "expected_capability_policy_id": "taskplanner-virtual-full-inventory.v1",
                    "retraction_controller_contract_topic": "/surgery/controller_contract",
                    "retraction_expected_controller_contract_id": "eir-nuc-tool-handover.real.v1",
                    "require_bed_robot_status": False,
                    "require_physical_stop_confirmation": True,
                    "retraction_state_machine_suppressed": True,
                }
            )
        )
    )

    assert proxy._route_state is not None
    assert proxy._route_state.retraction_source == "external"
    assert proxy._route_state.retraction_service_name == "/surgery/retraction/command"


class _CompletedServiceFuture:
    def __init__(self, response) -> None:
        self._response = response

    def done(self) -> bool:
        return True

    def result(self):
        return self._response

    def add_done_callback(self, callback) -> None:
        callback(self)


class _ReadyServiceClient:
    def __init__(self, response) -> None:
        self._future = _CompletedServiceFuture(response)
        self.requests = []

    @staticmethod
    def service_is_ready() -> bool:
        return True

    def call_async(self, request):
        self.requests.append(request)
        return self._future


class _DeferredServiceFuture:
    def __init__(self) -> None:
        self._done = False
        self._response = None
        self._callbacks = []

    def done(self) -> bool:
        return self._done

    def result(self):
        if not self._done:
            raise RuntimeError("future is unresolved")
        return self._response

    def add_done_callback(self, callback) -> None:
        self._callbacks.append(callback)
        if self._done:
            callback(self)

    def resolve(self, response) -> None:
        self._response = response
        self._done = True
        # Exercise a callback implementation that can notify more than once.
        for callback in tuple(self._callbacks):
            callback(self)


class _DeferredServiceClient:
    def __init__(self, future) -> None:
        self._future = future
        self.requests = []

    @staticmethod
    def service_is_ready() -> bool:
        return True

    def call_async(self, request):
        self.requests.append(request)
        return self._future


def _proxy_with_retraction_route(*, source: str, client=None):
    proxy = ExecutionCommandProxy.__new__(ExecutionCommandProxy)
    proxy._lock = threading.RLock()
    proxy._active_service_ids = set()
    proxy._active_service_activity = {}
    proxy._pending_retraction_receipts = {}
    proxy._active_actions = {}
    proxy._dispatch_epoch = 0
    proxy._latest_simulation_state = SimpleNamespace(
        running=True,
        execution_state="running",
        procedure_run_id="a" * 32,
    )
    proxy._last_lifecycle_control_signature = None
    proxy._service_receipt_timeout_sec = 0.1
    proxy._controller_recovery_timeout_sec = 15.0
    lifecycle = []
    proxy._lifecycle_pub = SimpleNamespace(publish=lifecycle.append)
    proxy._activity_pub = SimpleNamespace(publish=lambda _message: None)
    endpoint = (
        "/integration/virtual/surgery/retraction/command"
        if source == "virtual"
        else "/surgery/retraction/command"
    )
    route = SimpleNamespace(
        retraction_source=source,
        run_retraction_source=source,
        retraction_service_name=endpoint,
    )
    proxy._ready_route = lambda: route
    response = SimpleNamespace(
        request_accepted=True,
        result_code=ExecuteRetractionCommand.Response.RESULT_ACCEPTED,
        command_id="voice-direct-teach-1",
        message="accepted",
    )
    if client is None:
        client = _ReadyServiceClient(response)
    proxy._service_client = lambda _endpoint: client
    return proxy, client, lifecycle


def _request(command_id: str = "voice-direct-teach-1"):
    return SimpleNamespace(
        protocol_version=ExecuteRetractionCommand.Request.PROTOCOL_VERSION_V1,
        source_id="taskplanner",
        command_id=command_id,
        command=1,
        target_side=0,
        distance_m=0.0,
    )


def test_retraction_admission_requires_a_started_run_but_keeps_stop_available() -> None:
    start = _request("before-start")
    stop = _request("before-stop")
    stop.command = 6
    idle = SimpleNamespace(
        running=False,
        execution_state="idle",
        procedure_run_id="",
    )
    running = SimpleNamespace(
        running=True,
        execution_state="running",
        procedure_run_id="run-1",
    )

    assert retraction_request_allowed_by_scenario(start, None) is False
    assert retraction_request_allowed_by_scenario(start, idle) is False
    assert retraction_request_allowed_by_scenario(start, running) is True
    assert retraction_request_allowed_by_scenario(stop, idle) is True


def test_proxy_rejects_retraction_before_scenario_start_without_forwarding() -> None:
    proxy, client, _lifecycle = _proxy_with_retraction_route(source="external")
    proxy._latest_simulation_state = SimpleNamespace(
        running=False,
        execution_state="idle",
        procedure_run_id="",
    )
    response = SimpleNamespace()

    proxy._on_retraction_request(_request("idle-retraction"), response)

    assert response.request_accepted is False
    assert response.result_code == ExecuteRetractionCommand.Response.RESULT_REJECTED
    assert response.message == "scenario_not_running"
    assert client.requests == []


def test_virtual_retraction_proxy_emits_sent_accepted_and_transaction_completed() -> None:
    proxy, client, lifecycle = _proxy_with_retraction_route(source="virtual")
    response = SimpleNamespace()

    proxy._on_retraction_request(_request(), response)

    assert len(client.requests) == 1
    facts = [json.loads(message.data) for message in lifecycle]
    assert [(fact["stage"], fact["terminal"]) for fact in facts] == [
        ("sent", False),
        ("accepted", False),
        ("completed", True),
    ]
    assert all(fact["command_id"] == "voice-direct-teach-1" for fact in facts)
    assert all(fact["procedure_run_id"] == "a" * 32 for fact in facts)
    assert all(fact["endpoint_source"] == "virtual" for fact in facts)
    assert all(fact["retraction_command"] == 1 for fact in facts)
    assert facts[-1]["evidence"] == "virtual_service_transaction_completed"
    assert facts[-1]["reason_code"] == "virtual_service_completed"


def test_external_retraction_proxy_keeps_service_completion_as_admission_only() -> None:
    proxy, _client, lifecycle = _proxy_with_retraction_route(source="external")
    response = SimpleNamespace()

    proxy._on_retraction_request(_request("external-direct-teach-1"), response)

    facts = [json.loads(message.data) for message in lifecycle]
    assert [(fact["stage"], fact["terminal"]) for fact in facts] == [
        ("sent", False),
        ("accepted", True),
    ]
    assert all(fact["endpoint_source"] == "external" for fact in facts)
    assert all(
        fact["evidence"] != "virtual_service_transaction_completed"
        for fact in facts
    )


def test_retraction_submission_returns_immediately_and_tracks_late_virtual_receipt() -> None:
    future = _DeferredServiceFuture()
    proxy, client, lifecycle = _proxy_with_retraction_route(
        source="virtual",
        client=_DeferredServiceClient(future),
    )
    activity = []
    proxy._activity_pub = SimpleNamespace(publish=activity.append)
    first_response = SimpleNamespace()

    proxy._on_retraction_request(_request("late-virtual-1"), first_response)

    assert client.requests and len(client.requests) == 1
    assert first_response.request_accepted is True
    assert first_response.message == "service_call_submitted"
    assert "late-virtual-1" in proxy._active_service_ids
    assert "late-virtual-1" in proxy._pending_retraction_receipts
    assert [json.loads(message.data)["stage"] for message in lifecycle] == ["sent"]
    assert json.loads(activity[-1].data)["active"] is True

    duplicate_response = SimpleNamespace()
    proxy._on_retraction_request(_request("late-virtual-1"), duplicate_response)
    assert duplicate_response.message == "duplicate_command_inflight"
    assert len(client.requests) == 1

    future.resolve(
        SimpleNamespace(
            request_accepted=True,
            result_code=ExecuteRetractionCommand.Response.RESULT_ACCEPTED,
            command_id="late-virtual-1",
            message="accepted",
        )
    )

    facts = [json.loads(message.data) for message in lifecycle]
    assert [(fact["stage"], fact["terminal"]) for fact in facts] == [
        ("sent", False),
        ("accepted", False),
        ("completed", True),
    ]
    assert "late-virtual-1" not in proxy._active_service_ids
    assert proxy._pending_retraction_receipts == {}
    assert json.loads(activity[-1].data)["active"] is False

    future.resolve(future.result())
    assert len(lifecycle) == 4


def test_stop_retains_waiting_service_response_until_terminal_receipt():
    future = _DeferredServiceFuture()
    proxy, client, lifecycle = _proxy_with_retraction_route(
        source="external",
        client=_DeferredServiceClient(future),
    )
    response = SimpleNamespace()
    proxy._on_retraction_request(_request("stop-race-service"), response)
    assert client.requests

    proxy._on_runtime_control(SimpleNamespace(data="stop:operator"))
    future.resolve(
        SimpleNamespace(
            request_accepted=True,
            result_code=ExecuteRetractionCommand.Response.RESULT_ACCEPTED,
            command_id="stop-race-service",
            message="accepted",
        )
    )
    assert response.request_accepted is True
    assert response.message == "service_call_submitted"
    assert proxy._active_service_ids == set()
    assert [json.loads(message.data)["stage"] for message in lifecycle] == [
        "sent",
        "accepted",
    ]


def test_retraction_receipt_requires_the_matching_command_id() -> None:
    future = _DeferredServiceFuture()
    proxy, _client, lifecycle = _proxy_with_retraction_route(
        source="external",
        client=_DeferredServiceClient(future),
    )
    response = SimpleNamespace()

    proxy._on_retraction_request(_request("receipt-match-1"), response)
    assert response.request_accepted is True
    future.resolve(
        SimpleNamespace(
            request_accepted=True,
            result_code=ExecuteRetractionCommand.Response.RESULT_ACCEPTED,
            command_id="different-command",
            message="accepted",
        )
    )

    facts = [json.loads(message.data) for message in lifecycle]
    assert facts[-1]["stage"] == "failed"
    assert (
        facts[-1]["reason_code"]
        == "selected_retraction_service_command_id_mismatch"
    )
    assert proxy._active_service_ids == set()


def test_retraction_proxy_refuses_dispatch_while_route_is_initializing() -> None:
    proxy = ExecutionCommandProxy.__new__(ExecutionCommandProxy)
    proxy._lock = threading.RLock()
    proxy._route_state = SimpleNamespace(initialization_state="initializing")
    proxy._latest_simulation_state = SimpleNamespace(
        running=True,
        execution_state="running",
        procedure_run_id="run-1",
    )
    proxy._active_service_ids = set()
    proxy._pending_retraction_receipts = {}
    proxy._active_actions = {}
    proxy._service_client = lambda _endpoint: (_ for _ in ()).throw(
        AssertionError("controller-facing Service must not be created")
    )
    response = SimpleNamespace()

    proxy._on_retraction_request(_request("startup-direct-teach-1"), response)

    assert response.request_accepted is False
    assert response.message == "execution_route_unavailable"
    assert proxy._active_service_ids == set()


def test_stop_retains_proxy_recovery_tracking_and_cancels_downstream_action():
    proxy, _client, _lifecycle = _proxy_with_retraction_route(source="external")
    downstream_cancel_calls = []
    downstream = SimpleNamespace(
        cancel_goal_async=lambda: downstream_cancel_calls.append(True)
    )
    proxy._active_service_ids = {"old-service"}
    proxy._active_service_activity = {"old-service": SimpleNamespace()}
    proxy._pending_retraction_receipts = {
        "old-service": SimpleNamespace(future=object())
    }
    proxy._active_actions = {
        "old-action": SimpleNamespace(downstream_goal_handle=downstream)
    }
    activity = []
    proxy._activity_pub = SimpleNamespace(publish=activity.append)

    proxy._on_runtime_control(SimpleNamespace(data="stop:operator"))

    assert proxy._dispatch_epoch == 1
    assert proxy._active_service_ids == {"old-service"}
    assert set(proxy._active_service_activity) == {"old-service"}
    assert set(proxy._pending_retraction_receipts) == {"old-service"}
    assert set(proxy._active_actions) == {"old-action"}
    assert downstream_cancel_calls == [True]
    assert json.loads(activity[-1].data)["active"] is True


def test_stopped_dispatched_proxy_action_still_blocks_next_run_goal() -> None:
    proxy, _client, _lifecycle = _proxy_with_retraction_route(source="external")
    proxy._active_actions = {
        "old-action": SimpleNamespace(
            dispatch_epoch=0,
            dispatched=True,
            downstream_goal_handle=None,
            cancel_requested=True,
        )
    }
    proxy._dispatch_epoch = 1
    proxy._ready_route = lambda: SimpleNamespace(
        tool_handover_endpoint="/surgery/tool_handover"
    )

    assert proxy._tool_handover_goal(_tool_goal("next-run-action")) == (
        GoalResponse.REJECT
    )


def _tool_goal(command_id: str):
    return SimpleNamespace(
        command_id=command_id,
        instrument_id="Adson forceps",
        instrument_instance_id="Adson forceps#1",
        source_location="tray",
        target_location="surgeon",
    )


def _proxy_with_tool_route():
    proxy = ExecutionCommandProxy.__new__(ExecutionCommandProxy)
    proxy._lock = threading.RLock()
    proxy._active_service_ids = set()
    proxy._active_actions = {}
    activity = []
    proxy._activity_pub = SimpleNamespace(publish=activity.append)
    route = SimpleNamespace(tool_handover_endpoint="/surgery/tool_handover")
    proxy._ready_route = lambda: route
    return proxy, activity


def test_tool_handover_proxy_rejects_any_goal_while_one_is_reserved() -> None:
    proxy, activity = _proxy_with_tool_route()

    assert proxy._tool_handover_goal(_tool_goal("first")) == GoalResponse.ACCEPT
    assert proxy._tool_handover_goal(_tool_goal("second")) == GoalResponse.REJECT

    assert tuple(proxy._active_actions) == ("first",)
    assert len(activity) == 1
    assert json.loads(activity[0].data)["action_count"] == 1


def test_tool_handover_proxy_goal_reservation_is_atomic_under_race() -> None:
    proxy, _activity = _proxy_with_tool_route()
    barrier = threading.Barrier(3)
    responses = []
    response_lock = threading.Lock()

    def submit(command_id: str) -> None:
        barrier.wait()
        response = proxy._tool_handover_goal(_tool_goal(command_id))
        with response_lock:
            responses.append(response)

    threads = [
        threading.Thread(target=submit, args=("race-a",)),
        threading.Thread(target=submit, args=("race-b",)),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=2.0)

    assert not any(thread.is_alive() for thread in threads)
    assert responses.count(GoalResponse.ACCEPT) == 1
    assert responses.count(GoalResponse.REJECT) == 1
    assert len(proxy._active_actions) == 1


class _ImmediateAwaitable:
    def __init__(self, value=None, *, error: Exception | None = None) -> None:
        self._value = value
        self._error = error

    def __await__(self):
        async def resolve():
            if self._error is not None:
                raise self._error
            return self._value

        return resolve().__await__()


class _ProxyGoalHandle:
    def __init__(self, request) -> None:
        self.request = request
        self.terminal_state = ""

    def succeed(self) -> None:
        self.terminal_state = "succeeded"

    def canceled(self) -> None:
        self.terminal_state = "canceled"

    def abort(self) -> None:
        self.terminal_state = "aborted"

    @staticmethod
    def publish_feedback(_feedback) -> None:
        pass


class _DownstreamGoalHandle:
    accepted = True

    def __init__(self, result) -> None:
        self._result = result
        self.cancel_calls = 0

    def cancel_goal_async(self):
        self.cancel_calls += 1
        return _ImmediateAwaitable(SimpleNamespace(goals_canceling=[object()]))

    def get_result_async(self):
        return _ImmediateAwaitable(SimpleNamespace(result=self._result))


class _ToolActionClient:
    def __init__(self, goal_response) -> None:
        self._goal_response = goal_response

    @staticmethod
    def server_is_ready() -> bool:
        return True

    def send_goal_async(self, _request, *, feedback_callback):
        del feedback_callback
        return self._goal_response


def test_tool_handover_proxy_cancel_keeps_lane_until_downstream_terminal() -> None:
    proxy, _activity = _proxy_with_tool_route()
    first = _tool_goal("cancel-first")
    assert proxy._tool_handover_goal(first) == GoalResponse.ACCEPT
    assert proxy._cancel_tool_handover(SimpleNamespace(request=first)) == (
        CancelResponse.ACCEPT
    )
    assert proxy._tool_handover_goal(_tool_goal("cancel-second")) == (
        GoalResponse.REJECT
    )

    result = ExecuteToolHandover.Result()
    result.success = False
    result.final_state = ExecuteToolHandover.Result.FINAL_CANCELED
    result.reason_code = ExecuteToolHandover.Result.REASON_CANCELED_SOURCE_UNCHANGED
    downstream = _DownstreamGoalHandle(result)
    proxy._action_client = lambda _endpoint: _ToolActionClient(
        _ImmediateAwaitable(downstream)
    )
    upstream = _ProxyGoalHandle(first)

    returned = asyncio.run(proxy._execute_tool_handover(upstream))

    assert returned is result
    assert upstream.terminal_state == "canceled"
    assert downstream.cancel_calls == 1
    assert proxy._active_actions == {}
    assert proxy._tool_handover_goal(_tool_goal("after-terminal")) == (
        GoalResponse.ACCEPT
    )


def test_tool_handover_proxy_response_loss_keeps_lane_fail_closed() -> None:
    proxy, _activity = _proxy_with_tool_route()
    first = _tool_goal("unknown-first")
    assert proxy._tool_handover_goal(first) == GoalResponse.ACCEPT
    proxy._action_client = lambda _endpoint: _ToolActionClient(
        _ImmediateAwaitable(error=RuntimeError("response unavailable"))
    )
    upstream = _ProxyGoalHandle(first)

    result = asyncio.run(proxy._execute_tool_handover(upstream))

    assert result.success is False
    assert result.reason_code == "selected_tool_handover_error:RuntimeError"
    assert upstream.terminal_state == "aborted"
    assert tuple(proxy._active_actions) == ("unknown-first",)
    assert proxy._tool_handover_goal(_tool_goal("unknown-second")) == (
        GoalResponse.REJECT
    )
