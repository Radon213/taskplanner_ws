import json
import threading
import time
from types import SimpleNamespace

from procedure_spec import RetractionState
from std_msgs.msg import String
from surgical_interop_msgs.srv import ExecuteRetractionCommand

from integration_debug.node import (
    MAX_EVENT_SUMMARY_ITEMS,
    MAX_EVENT_SUMMARY_STRING_CHARS,
    InputStats,
    IntegrationDebugNode,
    _bounded_event_summary,
)


def test_recent_event_summary_bounds_large_nested_payloads() -> None:
    result = _bounded_event_summary(
        {
            "status_json": "x" * (MAX_EVENT_SUMMARY_STRING_CHARS + 500),
            "rows": list(range(MAX_EVENT_SUMMARY_ITEMS + 4)),
        }
    )

    assert len(result["status_json"]) < MAX_EVENT_SUMMARY_STRING_CHARS + 80
    assert "500 chars omitted" in result["status_json"]
    assert len(result["rows"]) == MAX_EVENT_SUMMARY_ITEMS + 1
    assert "4 items omitted" in result["rows"][-1]
    assert len(json.dumps(result)) < 4096


def test_non_speech_string_input_updates_monitor_only() -> None:
    class Harness:
        pass

    harness = Harness()
    harness._lock = threading.RLock()
    topic = "/integration/cv_contract/status"
    harness._input_stats = {topic: InputStats()}
    harness._asr_topic = "/sensors/surgeon/sentence"
    harness._last_sentence = "surgeon sentence remains authoritative"

    IntegrationDebugNode._on_string_input(
        harness,
        topic,
        String(data='{"schema":"taskplanner.cv_external_contract.v1"}'),
    )

    stats = harness._input_stats[topic]
    assert stats.message_count == 1
    assert stats.last_sample.startswith('{"schema"')
    assert harness._last_sentence == "surgeon sentence remains authoritative"


def test_retraction_service_request_uses_the_single_public_contract() -> None:
    request = IntegrationDebugNode._build_retraction_service_request(
        object(),
        "debug-command-1",
        {
            "command": "adjust_retraction",
            "target_side": "right",
            "distance_m": 0.05,
        },
    )

    assert request.protocol_version == ExecuteRetractionCommand.Request.PROTOCOL_VERSION_V1
    assert request.source_id == "taskplanner_debug"
    assert request.command_id == "debug-command-1"
    assert request.command == ExecuteRetractionCommand.Request.COMMAND_ADJUST_RETRACTION
    assert request.target_side == ExecuteRetractionCommand.Request.TARGET_RIGHT
    assert request.distance_m == 0.05


def test_legacy_retraction_debug_operations_fail_before_manual_interlock() -> None:
    for operation, payload, required_text in (
        (
            "retraction_adjustment",
            {"direction": "left", "axis": "left_right", "multi": True},
            "legacy direction, axis, and multi-retractor",
        ),
        (
            "tool_change",
            {"arm_id": "arm_1", "target_tool_id": "thyroid_retractor"},
            "legacy arm_id and target_tool_id",
        ),
    ):
        accepted, command_id, message = IntegrationDebugNode._dispatch_action(
            object(), operation, payload, source="ui"
        )
        assert not accepted
        assert command_id == ""
        assert required_text in message


def test_retraction_service_admission_never_claims_physical_completion() -> None:
    class Harness:
        pass

    events: list[tuple[str, dict[str, object]]] = []
    harness = Harness()
    harness._lock = threading.RLock()
    harness._active_command_id = "debug-command-1"
    harness._active_route = "retraction_service"
    harness._active_goal_handle = object()
    harness._retraction_state = RetractionState.TAUGHT_READY
    harness._last_retraction_rejection_reason = ""
    harness._action_status = {
        "command": "start_retraction",
        "response_semantics": "admission",
        "started_monotonic": time.monotonic() - 0.1,
        "progress": 0.75,
        "success": True,
        "terminal": False,
    }
    harness._record = lambda event_type, payload: events.append((event_type, payload))

    IntegrationDebugNode._finish_retraction_service_admission(
        harness,
        "debug-command-1",
        request_accepted=True,
        result_code=ExecuteRetractionCommand.Response.RESULT_ACCEPTED,
        state="accepted",
        reason_code="RESULT_ACCEPTED",
        response_message="accepted for controller admission",
    )

    assert harness._action_status["response_semantics"] == "admission"
    assert harness._action_status["state"] == "accepted"
    assert harness._action_status["request_accepted"] is True
    assert harness._action_status["result_code"] == 0
    assert harness._action_status["progress"] == 0.0
    assert harness._action_status["success"] is False
    assert harness._active_command_id == ""
    assert harness._retraction_state is RetractionState.RETRACTION_ACTIVE
    assert events == [
        (
            "retraction_service_response",
            {
                "command_id": "debug-command-1",
                "command": "start_retraction",
                "request_accepted": True,
                "result_code": 0,
                "reason_code": "RESULT_ACCEPTED",
                "message": "accepted for controller admission",
            },
        )
    ]


def test_manual_session_transitions_publish_status_without_waiting_for_timer() -> None:
    class Harness:
        def _execute_command(self, operation, _payload):
            return True, "", f"{operation} accepted"

    for operation in ("arm", "disarm", "reset_fault"):
        events: list[str] = []
        harness = Harness()
        harness._record = lambda *_args: events.append("recorded")
        harness._publish_status = lambda: events.append("status_published")
        response = SimpleNamespace()

        result = IntegrationDebugNode._handle_command(
            harness,
            SimpleNamespace(operation=operation, payload_json="{}"),
            response,
        )

        assert result is response
        assert response.accepted is True
        assert events == ["recorded", "status_published"]
