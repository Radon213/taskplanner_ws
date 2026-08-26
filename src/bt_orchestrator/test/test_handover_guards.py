from types import SimpleNamespace

from bt_orchestrator.guards import should_allow_handover


def test_phase_uncertainty_is_not_a_handover_gate() -> None:
    world = SimpleNamespace(
        handover_allowed=True,
        phase_uncertain=True,
        recovery_required=False,
    )

    assert should_allow_handover(world)


def test_non_phase_recovery_guard_remains_fail_closed() -> None:
    world = SimpleNamespace(
        handover_allowed=True,
        phase_uncertain=False,
        recovery_required=True,
    )

    assert not should_allow_handover(world)
