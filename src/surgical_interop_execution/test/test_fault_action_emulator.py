from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from surgical_interop_execution.fault_action_emulator import (
    EmulatorProfile,
    FaultActionEmulator,
    Outcome,
    RouteProfile,
    _TOOL_CHANGE_RESULT_BY_OUTCOME,
    validate_retraction_adjustment,
    valid_tool_transition,
)


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
  retraction_adjustment: {available: false}
  tool_change: {default: {outcome: success}}
""",
        encoding="utf-8",
    )
    profile = EmulatorProfile.load(path)
    route = profile.routes["tool_handover"]
    assert route.next().outcome == "partial_failure"
    assert route.next().outcome == "success"
    assert route.next().reason_code == "exhausted"
    assert profile.routes["retraction_adjustment"].available is False
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


def _adjustment(**overrides):
    values = {
        "command_id": "adjust-1",
        "adjustment_mode": "single",
        "target_retractor_id": "left_malleable",
        "direction_frame": "surgeon_view",
        "direction": "left",
        "axis": "none",
        "distance_mm": 5.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"command_id": ""}, "missing_command_id"),
        ({"adjustment_mode": "vendor_mode"}, "invalid_adjustment_mode"),
        ({"target_retractor_id": "both_malleable"}, "invalid_target_retractor_id"),
        ({"direction_frame": "robot_base"}, "invalid_direction_frame"),
        ({"direction": "none"}, "invalid_single_adjustment"),
        ({"axis": "left_right"}, "invalid_single_adjustment"),
        ({"distance_mm": 0.0}, "invalid_distance_mm"),
        ({"distance_mm": 30.1}, "invalid_distance_mm"),
        (
            {
                "adjustment_mode": "multi",
                "target_retractor_id": "left_malleable",
                "direction": "none",
                "axis": "left_right",
            },
            "invalid_target_retractor_id",
        ),
        (
            {
                "adjustment_mode": "multi",
                "target_retractor_id": "both_malleable",
                "direction": "left",
                "axis": "left_right",
            },
            "invalid_multi_adjustment",
        ),
    ],
)
def test_retraction_adjustment_contract_rejects_invalid_goal_fields(overrides, reason):
    assert validate_retraction_adjustment(_adjustment(**overrides)) == reason


def test_retraction_adjustment_contract_accepts_document_single_and_multi_forms():
    assert validate_retraction_adjustment(_adjustment()) == ""
    assert validate_retraction_adjustment(
        _adjustment(
            adjustment_mode="multi",
            target_retractor_id="both_malleable",
            direction="none",
            axis="up_down",
            distance_mm=7.0,
        )
    ) == ""


def test_tool_change_emulator_covers_every_document_result_state():
    assert set(_TOOL_CHANGE_RESULT_BY_OUTCOME.values()) == {
        "completed",
        "failed",
        "canceled",
        "protective_stop",
        "unknown",
    }


class _Feedback:
    def __init__(self):
        self.state = ""
        self.command_id = ""


class _FakeGoalHandle:
    def __init__(self, request, *, cancel_after_feedback=False):
        self.request = request
        self.is_cancel_requested = False
        self.cancel_after_feedback = cancel_after_feedback
        self.feedback = []
        self.terminal = ""

    def publish_feedback(self, feedback):
        self.feedback.append((feedback.state, feedback.command_id))
        if self.cancel_after_feedback and feedback.state == "adjusting":
            self.is_cancel_requested = True

    def succeed(self):
        self.terminal = "succeeded"

    def abort(self):
        self.terminal = "aborted"

    def canceled(self):
        self.terminal = "canceled"


def _bare_emulator(outcome: Outcome) -> FaultActionEmulator:
    emulator = FaultActionEmulator.__new__(FaultActionEmulator)
    emulator._lock = __import__("threading").RLock()
    emulator._active_ids = set()
    emulator._selected_outcomes = {("retraction_adjustment", "adjust-1"): outcome}
    emulator._completed = {}
    emulator._route_counts = {}
    emulator._profile = SimpleNamespace(
        routes={
            "retraction_adjustment": RouteProfile(default=outcome),
            "tool_change": RouteProfile(default=outcome),
        }
    )
    return emulator


def test_retraction_cancel_emits_recovering_before_remote_canceled_result():
    emulator = _bare_emulator(Outcome(outcome="success", duration_sec=0.2))
    handle = _FakeGoalHandle(_adjustment(), cancel_after_feedback=True)

    state, reason, progress = emulator._run_action(
        "retraction_adjustment", handle, _Feedback
    )

    assert [row[0] for row in handle.feedback][:2] == ["adjusting", "recovering"]
    assert all(row[1] == "adjust-1" for row in handle.feedback)
    assert handle.terminal == "canceled"
    assert (state, reason, progress) == ("canceled", "canceled", 1.0)


@pytest.mark.parametrize(
    ("outcome_name", "final_state", "terminal"),
    [
        ("failed", "fault", "aborted"),
        ("canceled", "canceled", "canceled"),
        ("protective_stop", "protective_stop", "aborted"),
        ("unknown", "unknown", "aborted"),
    ],
)
def test_retraction_emulator_returns_document_terminal_states(
    outcome_name, final_state, terminal
):
    emulator = _bare_emulator(Outcome(outcome=outcome_name, duration_sec=0.0))
    handle = _FakeGoalHandle(_adjustment())

    state, _, _ = emulator._run_action(
        "retraction_adjustment", handle, _Feedback
    )

    assert state == final_state
    assert handle.terminal == terminal


def test_tool_change_service_honors_blocking_delay_and_document_result():
    emulator = _bare_emulator(
        Outcome(
            outcome="protective_stop",
            duration_sec=0.03,
            reason_code="guard_triggered",
        )
    )
    request = SimpleNamespace(
        command_id="change-1",
        arm_id="arm_1",
        target_tool_id="army_navy_retractor",
    )
    response = SimpleNamespace(success=None, result="", reason_code="")

    started = time.monotonic()
    result = emulator._request_tool_change(request, response)
    elapsed = time.monotonic() - started

    assert elapsed >= 0.025
    assert result.success is False
    assert result.result == "protective_stop"
    assert result.reason_code == "guard_triggered"
