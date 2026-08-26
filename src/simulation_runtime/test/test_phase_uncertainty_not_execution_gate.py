from __future__ import annotations

from types import SimpleNamespace

import pytest

from simulation_runtime.surgeon_actor import SurgeonActorNode


def _actor_with_phase_hint(*, uncertain: bool, confidence: float) -> SurgeonActorNode:
    actor = SurgeonActorNode.__new__(SurgeonActorNode)
    actor._world = SimpleNamespace(
        filtered_phase="P01",
        cleaner_busy=False,
        pending_transition_tools=[],
    )
    actor._spec = SimpleNamespace(
        default_phase_id="P01",
        phase_ids=("P01", "P02"),
        bundle=SimpleNamespace(
            phase_guard=SimpleNamespace(
                min_dwell_time_sec=0.0,
                min_confidence_to_switch=0.8,
            )
        ),
        get_phase_min_duration=lambda _phase_id: 0.0,
        is_transition_allowed=lambda current, candidate: (
            current == "P01" and candidate == "P02"
        ),
    )
    actor._phase_entered_sec = 0.0
    actor._current_time_sec = lambda: 1.0
    actor._phase_interactions_complete_for_actor = lambda _phase_id: True
    actor._phase_hint = SimpleNamespace(
        phase_id="P02",
        uncertain=uncertain,
        confidence=confidence,
    )
    actor._autonomous_phase_progression_enabled = True
    actor._autonomous_phase_candidate = lambda _phase_id: ""
    return actor


@pytest.mark.parametrize("uncertain", [False, True])
def test_phase_uncertainty_does_not_block_high_confidence_phase_advance(
    uncertain: bool,
) -> None:
    actor = _actor_with_phase_hint(uncertain=uncertain, confidence=0.95)

    assert actor._phase_advance_candidate() == "P02"


@pytest.mark.parametrize("uncertain", [False, True])
def test_phase_confidence_threshold_remains_independent_of_uncertainty(
    uncertain: bool,
) -> None:
    actor = _actor_with_phase_hint(uncertain=uncertain, confidence=0.79)

    assert actor._phase_advance_candidate() == ""
