from __future__ import annotations

from pathlib import Path

from or_digital_twin.twin import ORDigitalTwin
from procedure_spec import load_bundle
from surgical_msgs.msg import FilteredPhase


def _twin() -> ORDigitalTwin:
    return ORDigitalTwin(
        load_bundle(
            Path(__file__).parents[2]
            / "procedure_spec"
            / "procedure_spec"
            / "specs"
            / "thyroidectomy_demo"
        )
    )


def _phase(twin: ORDigitalTwin, *, uncertain: bool) -> FilteredPhase:
    phase = FilteredPhase()
    phase.phase_id = twin.state.filtered_phase
    phase.confidence = 0.42 if uncertain else 0.96
    phase.uncertain = uncertain
    phase.stability = 0.2 if uncertain else 0.9
    return phase


def test_phase_uncertainty_does_not_preserve_retracted_robot_state() -> None:
    twin = _twin()
    twin.state.phase_uncertain = True
    twin.state.robot_state = "retracted"

    twin._normalize_robot_state()

    assert twin.state.phase_uncertain is True
    assert twin.state.robot_state == "idle"


def test_phase_certainty_changes_only_phase_metadata_not_robot_state_policy() -> None:
    twin = _twin()

    twin.state.robot_state = "retracted"
    twin.update_phase(_phase(twin, uncertain=True))
    uncertain_robot_state = twin.state.robot_state
    assert twin.state.phase_uncertain is True

    twin.state.robot_state = "retracted"
    twin.update_phase(_phase(twin, uncertain=False))
    certain_robot_state = twin.state.robot_state
    assert twin.state.phase_uncertain is False

    assert uncertain_robot_state == "idle"
    assert certain_robot_state == "idle"
