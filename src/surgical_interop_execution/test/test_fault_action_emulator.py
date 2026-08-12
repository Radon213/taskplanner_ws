from pathlib import Path

import pytest

from surgical_interop_execution.fault_action_emulator import (
    EmulatorProfile,
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
  retraction: {available: false}
  suction: {default: {outcome: success}}
""",
        encoding="utf-8",
    )
    profile = EmulatorProfile.load(path)
    route = profile.routes["tool_handover"]
    assert route.next().outcome == "partial_failure"
    assert route.next().outcome == "success"
    assert route.next().reason_code == "exhausted"
    assert profile.routes["retraction"].available is False


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
