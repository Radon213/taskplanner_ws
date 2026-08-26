from __future__ import annotations

from pathlib import Path


PACKAGE_DIR = Path(__file__).parents[1]
BT_AUDIT_PATH = PACKAGE_DIR / "bringup" / "bt_audit.py"
SMOKE_TEST_PATH = PACKAGE_DIR / "bringup" / "smoke_test.py"


def _section(text: str, start: str, end: str) -> str:
    start_at = text.index(start)
    return text[start_at : text.index(end, start_at)]


def test_bt_audit_never_uses_phase_uncertainty_as_a_decision_guard() -> None:
    source = BT_AUDIT_PATH.read_text(encoding="utf-8")
    anticipatory = _section(
        source,
        'elif decision.decision == "anticipatory_handover":',
        'elif decision.decision == "hold":',
    )
    hold = _section(
        source,
        'elif decision.decision == "hold":',
        'elif decision.decision == "idle":',
    )

    assert "phase_uncertain" not in source
    assert "if not world.handover_allowed:" in anticipatory
    assert "world.handover_allowed and not explicit_tool" in hold
    assert "another stronger guard reason" in hold


def test_smoke_handover_window_ignores_phase_uncertainty() -> None:
    source = SMOKE_TEST_PATH.read_text(encoding="utf-8")
    handover_window = _section(
        source,
        "def wait_for_handover_window",
        "def choose_override_tool",
    )

    assert "phase_uncertain" not in source
    assert "self._latest_world.handover_allowed" in handover_window
    assert "self._latest_world.cleaner_busy" in handover_window
