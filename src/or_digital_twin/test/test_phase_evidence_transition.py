from __future__ import annotations

from pathlib import Path

from or_digital_twin.twin import ORDigitalTwin
from procedure_spec import load_bundle
from surgical_msgs.msg import PhaseEvidence


def _thyroid_twin(*, open_set_bootstrap: bool = False) -> ORDigitalTwin:
    spec = load_bundle(
        Path(__file__).parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
        / "thyroidectomy"
    )
    return ORDigitalTwin(
        spec,
        allow_open_set_phase_bootstrap=open_set_bootstrap,
    )


def _phase_evidence(target_phase: str, confidence: float) -> PhaseEvidence:
    evidence = PhaseEvidence()
    evidence.source = "real_vlm:test"
    evidence.phase_ids = [target_phase, "P03"]
    evidence.phase_confidences = [confidence, 0.2]
    evidence.uncertainty = 0.1
    return evidence


def _complete_phase_interactions(twin: ORDigitalTwin, phase_id: str) -> None:
    for tool_id in twin.spec.get_expected_instruments(phase_id):
        for state in twin._instances_for_type(tool_id):
            state.ever_surgeon_owned = True
    next_phase = twin.spec.get_next_normal_phase(phase_id)
    for tool_id, required_count in twin._phase_transition_required_counts(
        phase_id, next_phase
    ).items():
        for state in twin._instances_for_type(tool_id)[:required_count]:
            twin._phase_instance_interactions[phase_id][tool_id].add(
                state.instance_id
            )


def test_stable_vlm_evidence_advances_normal_phase_without_surgeon_cue() -> None:
    twin = _thyroid_twin()
    now = [100.0]
    twin._monotonic_sec = lambda: now[0]
    twin.set_initial_phase("P03")
    now[0] = 106.0

    decisions = []
    for _ in range(twin.spec.bundle.phase_guard.smoothing_window):
        decisions.extend(twin.apply_phase_evidence(_phase_evidence("P04", 0.92)))
        now[0] += 1.0

    assert twin.state.filtered_phase == "P04"
    assert any(
        decision.get("accepted")
        and decision.get("reason") == "stable_vlm_phase_evidence"
        for decision in decisions
    )


def test_stable_later_phase_evidence_catches_up_only_one_guarded_step() -> None:
    twin = _thyroid_twin()
    now = [100.0]
    twin._monotonic_sec = lambda: now[0]
    twin.set_initial_phase("P03")
    now[0] = 106.0

    decisions = []
    for _ in range(twin.spec.bundle.phase_guard.smoothing_window):
        decisions.extend(
            twin.apply_phase_evidence(_phase_evidence("P05", 0.94))
        )
        now[0] += 1.0

    assert twin.state.filtered_phase == "P04"
    assert any(
        decision.get("accepted")
        and decision.get("reason") == "stable_downstream_vlm_evidence"
        for decision in decisions
    )


def test_later_phase_evidence_cannot_bypass_interaction_guard() -> None:
    base = _thyroid_twin()
    twin = ORDigitalTwin(
        base.spec,
        phase_transition_required_counts={
            ("P03", "P04"): {"T05": 2}
        },
    )
    now = [100.0]
    twin._monotonic_sec = lambda: now[0]
    twin.set_initial_phase("P03")
    now[0] = 106.0

    for _ in range(twin.spec.bundle.phase_guard.smoothing_window + 1):
        twin.apply_phase_evidence(_phase_evidence("P05", 0.94))
        now[0] += 1.0

    assert twin.state.filtered_phase == "P03"


def test_single_vlm_phase_spike_does_not_advance_normal_phase() -> None:
    twin = _thyroid_twin()
    now = [100.0]
    twin._monotonic_sec = lambda: now[0]
    twin.set_initial_phase("P03")
    _complete_phase_interactions(twin, "P03")
    now[0] = 106.0

    twin.apply_phase_evidence(_phase_evidence("P04", 0.99))

    assert twin.state.filtered_phase == "P03"


def test_stable_public_vlm_evidence_can_acquire_a_midprocedure_phase_once() -> None:
    twin = _thyroid_twin(open_set_bootstrap=True)
    now = [100.0]
    twin._monotonic_sec = lambda: now[0]

    decisions = []
    for _ in range(twin.spec.bundle.phase_guard.smoothing_window):
        decisions.extend(twin.apply_phase_evidence(_phase_evidence("P05", 0.94)))
        now[0] += 1.0

    assert twin.state.filtered_phase == "P05"
    assert not twin.phase_bootstrap_open
    assert any(
        decision.get("accepted")
        and decision.get("reason") == "stable_vlm_phase_bootstrap"
        for decision in decisions
    )


def test_explicit_start_phase_disables_midprocedure_bootstrap() -> None:
    twin = _thyroid_twin(open_set_bootstrap=True)
    twin.set_initial_phase("P01")

    for _ in range(twin.spec.bundle.phase_guard.smoothing_window):
        twin.apply_phase_evidence(_phase_evidence("P05", 0.94))

    assert twin.state.filtered_phase == "P01"
    assert not twin.phase_bootstrap_open
