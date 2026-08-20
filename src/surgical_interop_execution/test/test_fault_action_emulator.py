from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from surgical_interop_execution.fault_action_emulator import (
    _BED_ROBOT_STATUS_PERIOD_SEC,
    EmulatorProfile,
    FaultActionEmulator,
    Outcome,
    RouteProfile,
    validate_retraction_command,
    valid_tool_transition,
)
from surgical_interop_msgs.srv import ExecuteRetractionCommand


def test_only_reviewed_tool_transitions_are_accepted():
    assert valid_tool_transition("tray", "robot")
    assert valid_tool_transition("tray", "surgeon")
    assert valid_tool_transition("robot", "surgeon")
    assert valid_tool_transition("robot", "tray")
    assert valid_tool_transition("mayo", "robot")
    assert valid_tool_transition("mayo", "tray")
    assert not valid_tool_transition("surgeon", "robot")
    assert not valid_tool_transition("mayo", "surgeon")


def test_profile_sequence_is_deterministic(tmp_path: Path):
    path = tmp_path / "profile.yaml"
    path.write_text(
        """schema: taskplanner.action_emulator.v1
profile_id: test
routes:
  tool_handover:
    sequence:
      - {outcome: partial_failure, duration_sec: 0.1, fail_progress: 0.4}
      - {outcome: success, duration_sec: 0.2}
    default: {outcome: abort, reason_code: exhausted}
  retraction_command: {available: false}
""",
        encoding="utf-8",
    )
    profile = EmulatorProfile.load(path)
    route = profile.routes["tool_handover"]
    assert route.next().outcome == "partial_failure"
    assert route.next().outcome == "success"
    assert route.next().reason_code == "exhausted"
    assert profile.routes["retraction_command"].available is False
    assert "suction" not in profile.routes


def test_unknown_outcome_is_rejected(tmp_path: Path):
    path = tmp_path / "profile.yaml"
    path.write_text(
        """schema: taskplanner.action_emulator.v1
routes:
  tool_handover: {default: {outcome: teleport}}
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unsupported emulator outcome"):
        EmulatorProfile.load(path)


def _command(**overrides):
    values = {
        "protocol_version": 1,
        "source_id": "taskplanner",
        "command_id": "adjust-1",
        "command": ExecuteRetractionCommand.Request.COMMAND_ADJUST_RETRACTION,
        "target_side": ExecuteRetractionCommand.Request.TARGET_LEFT,
        "distance_m": 0.005,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    ("overrides", "result_code", "reason"),
    [
        (
            {"protocol_version": 99},
            ExecuteRetractionCommand.Response.RESULT_INVALID_PARAMETER,
            "unsupported_protocol_version",
        ),
        (
            {"source_id": ""},
            ExecuteRetractionCommand.Response.RESULT_INVALID_PARAMETER,
            "missing_source_id",
        ),
        (
            {"command_id": ""},
            ExecuteRetractionCommand.Response.RESULT_INVALID_PARAMETER,
            "missing_command_id",
        ),
        (
            {
                "command": 99,
            },
            ExecuteRetractionCommand.Response.RESULT_INVALID_COMMAND,
            "invalid_command",
        ),
        (
            {
                "target_side": ExecuteRetractionCommand.Request.TARGET_NONE,
            },
            ExecuteRetractionCommand.Response.RESULT_INVALID_PARAMETER,
            "adjust_requires_left_or_right_target",
        ),
        (
            {"distance_m": 0.0},
            ExecuteRetractionCommand.Response.RESULT_INVALID_PARAMETER,
            "invalid_adjust_distance_m",
        ),
        (
            {"distance_m": 0.051},
            ExecuteRetractionCommand.Response.RESULT_INVALID_PARAMETER,
            "invalid_adjust_distance_m",
        ),
    ],
)
def test_retraction_command_contract_rejects_invalid_request_fields(
    overrides, result_code, reason
):
    assert validate_retraction_command(_command(**overrides)) == (result_code, reason)


def test_retraction_command_contract_accepts_adjust_and_parameterless_forms():
    assert validate_retraction_command(_command()) == (
        ExecuteRetractionCommand.Response.RESULT_ACCEPTED,
        "",
    )
    assert validate_retraction_command(
        _command(
            command=ExecuteRetractionCommand.Request.COMMAND_CHANGE_TOOL,
            target_side=ExecuteRetractionCommand.Request.TARGET_NONE,
            distance_m=0.0,
        )
    ) == (ExecuteRetractionCommand.Response.RESULT_ACCEPTED, "")


def _bare_emulator(outcome: Outcome) -> FaultActionEmulator:
    emulator = FaultActionEmulator.__new__(FaultActionEmulator)
    emulator._lock = __import__("threading").RLock()
    emulator._active_ids = set()
    emulator._selected_outcomes = {}
    emulator._completed = {}
    emulator._route_counts = {}
    emulator._max_retraction_distance_m = 0.050
    emulator._profile = SimpleNamespace(
        routes={
            "retraction_command": RouteProfile(default=outcome),
        }
    )
    return emulator


def test_bed_robot_status_heartbeat_publishes_initial_snapshot_before_timer():
    emulator = FaultActionEmulator.__new__(FaultActionEmulator)
    calls = []
    timer = object()
    emulator._publish_bed_robot_status = lambda: calls.append(("publish", None))

    def create_timer(period_sec, callback):
        calls.append(("timer", period_sec, callback))
        return timer

    emulator.create_timer = create_timer

    emulator._start_bed_robot_status_heartbeat()

    assert calls[0] == ("publish", None)
    assert calls[1][0:2] == ("timer", _BED_ROBOT_STATUS_PERIOD_SEC)
    assert calls[1][2] is emulator._publish_bed_robot_status
    assert _BED_ROBOT_STATUS_PERIOD_SEC == 0.5
    assert emulator._bed_robot_status_timer is timer


def test_bed_robot_status_revisions_are_monotonic_across_checkpoints():
    emulator = FaultActionEmulator.__new__(FaultActionEmulator)
    published = []
    stamps = iter((11, 12, 13))
    emulator._bed_robot_revision = 0
    emulator._procedure_type = "thyroidectomy"
    emulator._bed_robot_status_pub = SimpleNamespace(
        publish=lambda message: published.append(message)
    )
    emulator.get_clock = lambda: SimpleNamespace(
        now=lambda: SimpleNamespace(
            to_msg=lambda: SimpleNamespace(sec=next(stamps), nanosec=0)
        )
    )

    emulator._publish_bed_robot_status()
    emulator._publish_bed_robot_status()

    assert [message.revision for message in published] == [1, 2]
    assert [message.stamp.sec for message in published] == [11, 12]
    assert all(message.procedure_type == "thyroidectomy" for message in published)
    assert all(len(message.arms) == 1 for message in published)
    assert all(message.arms[0].state == "standby" for message in published)


def test_retraction_service_is_immediate_admission_not_physical_result():
    emulator = _bare_emulator(
        Outcome(
            outcome="protective_stop",
            duration_sec=0.03,
            reason_code="guard_triggered",
        )
    )
    request = _command(command_id="service-1")
    response = SimpleNamespace(
        request_accepted=None,
        result_code=-1,
        command_id="",
        message="",
    )

    started = time.monotonic()
    result = emulator._request_retraction_command(request, response)
    elapsed = time.monotonic() - started

    assert elapsed < 0.025
    assert result.request_accepted is True
    assert result.result_code == ExecuteRetractionCommand.Response.RESULT_ACCEPTED
    assert result.command_id == "service-1"
    assert result.message == "guard_triggered"


def test_retraction_service_profile_rejects_without_claiming_execution():
    emulator = _bare_emulator(Outcome(outcome="reject", reason_code="controller_busy"))
    response = SimpleNamespace(
        request_accepted=None,
        result_code=-1,
        command_id="",
        message="",
    )

    result = emulator._request_retraction_command(_command(), response)

    assert result.request_accepted is False
    assert result.result_code == ExecuteRetractionCommand.Response.RESULT_REJECTED
    assert result.message == "controller_busy"
