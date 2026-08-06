from __future__ import annotations

from pathlib import Path

from or_digital_twin.twin import ORDigitalTwin
from procedure_spec import load_bundle
from surgical_msgs.msg import PhaseEvidence


def _specs_root() -> Path:
    return (
        Path(__file__).parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
    )


def _procedure_twin(
    spec_name: str = "thyroidectomy",
    *,
    open_set_bootstrap: bool = False,
) -> ORDigitalTwin:
    spec = load_bundle(_specs_root() / spec_name)
    return ORDigitalTwin(
        spec,
        allow_open_set_phase_bootstrap=open_set_bootstrap,
    )


def _thyroid_twin(*, open_set_bootstrap: bool = False) -> ORDigitalTwin:
    return _procedure_twin(
        open_set_bootstrap=open_set_bootstrap,
    )


def _phase_evidence(
    target_phase: str,
    confidence: float,
    *,
    source_time_sec: float = 0.0,
    uncertainty: float = 0.1,
) -> PhaseEvidence:
    evidence = PhaseEvidence()
    evidence.source = "real_vlm:test"
    evidence.phase_ids = [target_phase]
    evidence.phase_confidences = [confidence]
    evidence.uncertainty = uncertainty
    whole_seconds = int(source_time_sec)
    evidence.stamp.sec = whole_seconds
    evidence.stamp.nanosec = int(
        round((source_time_sec - whole_seconds) * 1_000_000_000)
    )
    return evidence


def _normal_phase_chain(
    twin: ORDigitalTwin,
    *,
    minimum_length: int = 3,
) -> tuple[str, ...]:
    normal_phases = tuple(
        phase_id
        for phase_id in twin.spec.phase_ids
        if twin.spec.is_normal_phase(phase_id)
    )
    for start_index in range(
        len(normal_phases) - minimum_length + 1
    ):
        candidate = normal_phases[
            start_index : start_index + minimum_length
        ]
        if all(
            twin.spec.get_next_normal_phase(current_phase)
            == target_phase
            for current_phase, target_phase in zip(
                candidate,
                candidate[1:],
            )
        ):
            return candidate
    raise AssertionError(
        f"procedure needs {minimum_length} adjacent normal phases"
    )


def _source_times(
    twin: ORDigitalTwin,
    *,
    duration_scale: float = 1.0,
) -> tuple[float, ...]:
    guard = twin.spec.bundle.phase_guard
    sample_count = max(2, int(guard.smoothing_window))
    reference_duration = max(1.0, float(guard.min_dwell_time_sec))
    source_duration = reference_duration * duration_scale
    start_time = reference_duration
    return tuple(
        start_time
        + source_duration * index / (sample_count - 1)
        for index in range(sample_count)
    )


def _high_switch_confidence(twin: ORDigitalTwin) -> float:
    threshold = float(
        twin.spec.bundle.phase_guard.min_confidence_to_switch
    )
    return min(1.0, threshold + (1.0 - threshold) / 2.0)


def _set_phase_dwell_satisfied(
    twin: ORDigitalTwin,
    now: list[float],
) -> None:
    guard = twin.spec.bundle.phase_guard
    now[0] += max(
        float(guard.min_dwell_time_sec),
        float(
            twin.spec.get_phase_min_duration(
                twin.state.filtered_phase
            )
        ),
    ) + 1.0


def _field_guard_fixture() -> tuple[ORDigitalTwin, str, str]:
    for spec_path in sorted(_specs_root().iterdir()):
        if not spec_path.is_dir():
            continue
        spec = load_bundle(spec_path)
        normal_phases = [
            phase_id
            for phase_id in spec.phase_ids
            if spec.is_normal_phase(phase_id)
        ]
        for current_phase in normal_phases:
            target_phase = spec.get_next_normal_phase(current_phase)
            if (
                not target_phase
                or not spec.get_field_deployed_instruments(target_phase)
            ):
                continue
            twin = ORDigitalTwin(spec)
            twin.set_initial_phase(current_phase)
            if not twin._phase_field_deployment_ready(target_phase):
                return twin, current_phase, target_phase
    raise AssertionError(
        "test fixture needs an adjacent phase with an unmet physical "
        "field-deployment requirement"
    )


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
    current_phase, target_phase, _ = _normal_phase_chain(twin)
    now = [100.0]
    twin._monotonic_sec = lambda: now[0]
    twin.set_initial_phase(current_phase)
    _set_phase_dwell_satisfied(twin, now)

    decisions = []
    for source_time_sec in _source_times(twin):
        decisions.extend(
            twin.apply_phase_evidence(
                _phase_evidence(
                    target_phase,
                    _high_switch_confidence(twin),
                    source_time_sec=source_time_sec,
                )
            )
        )
        now[0] += 1.0

    assert twin.state.filtered_phase == target_phase
    assert any(
        decision.get("accepted")
        and decision.get("reason") == "stable_vlm_phase_evidence"
        for decision in decisions
    )


def test_stable_later_phase_evidence_catches_up_only_one_guarded_step() -> None:
    twin = _thyroid_twin()
    current_phase, adjacent_phase, downstream_phase = (
        _normal_phase_chain(twin)
    )
    now = [100.0]
    twin._monotonic_sec = lambda: now[0]
    twin.set_initial_phase(current_phase)
    _set_phase_dwell_satisfied(twin, now)

    decisions = []
    for source_time_sec in _source_times(twin):
        decisions.extend(
            twin.apply_phase_evidence(
                _phase_evidence(
                    downstream_phase,
                    _high_switch_confidence(twin),
                    source_time_sec=source_time_sec,
                )
            )
        )
        now[0] += 1.0

    assert twin.state.filtered_phase == adjacent_phase
    assert any(
        decision.get("accepted")
        and decision.get("reason") == "stable_downstream_vlm_evidence"
        for decision in decisions
    )


def test_later_phase_evidence_cannot_bypass_interaction_guard() -> None:
    base = _thyroid_twin()
    current_phase, adjacent_phase, downstream_phase = (
        _normal_phase_chain(base)
    )
    required_tool = next(
        tool_id
        for tool_id in base.spec.get_expected_instruments(
            current_phase
        )
        if base._instances_for_type(tool_id)
    )
    twin = ORDigitalTwin(
        base.spec,
        phase_transition_required_counts={
            (current_phase, adjacent_phase): {required_tool: 1}
        },
    )
    now = [100.0]
    twin._monotonic_sec = lambda: now[0]
    twin.set_initial_phase(current_phase)
    _set_phase_dwell_satisfied(twin, now)

    for source_time_sec in _source_times(twin):
        twin.apply_phase_evidence(
            _phase_evidence(
                downstream_phase,
                _high_switch_confidence(twin),
                source_time_sec=source_time_sec,
            )
        )
        now[0] += 1.0

    assert twin.state.filtered_phase == current_phase
    assert any(
        event.get("event_type") == "PhaseTransitionRejected"
        and event.get("reason")
        == "required_transition_evidence_incomplete"
        for event in twin.event_history
    )


def test_single_vlm_phase_spike_does_not_advance_normal_phase() -> None:
    twin = _thyroid_twin()
    current_phase, target_phase, _ = _normal_phase_chain(twin)
    now = [100.0]
    twin._monotonic_sec = lambda: now[0]
    twin.set_initial_phase(current_phase)
    _complete_phase_interactions(twin, current_phase)
    _set_phase_dwell_satisfied(twin, now)

    twin.apply_phase_evidence(
        _phase_evidence(
            target_phase,
            _high_switch_confidence(twin),
            source_time_sec=_source_times(twin)[0],
        )
    )

    assert twin.state.filtered_phase == current_phase


def test_duplicate_source_timestamp_counts_as_one_phase_observation() -> None:
    twin = _thyroid_twin()
    current_phase, target_phase, _ = _normal_phase_chain(twin)
    now = [100.0]
    twin._monotonic_sec = lambda: now[0]
    twin.set_initial_phase(current_phase)
    _set_phase_dwell_satisfied(twin, now)
    duplicate_source_time = _source_times(twin)[0]

    for _ in range(
        int(twin.spec.bundle.phase_guard.smoothing_window) + 2
    ):
        twin.apply_phase_evidence(
            _phase_evidence(
                target_phase,
                _high_switch_confidence(twin),
                source_time_sec=duplicate_source_time,
            )
        )
        now[0] += 1.0

    assert twin.state.filtered_phase == current_phase
    _, _, sample_count = twin._phase_evidence_summary(target_phase)
    assert sample_count == 1


def test_interleaved_duplicate_source_frame_cannot_count_twice() -> None:
    twin = _thyroid_twin()
    current_phase, target_phase, _ = _normal_phase_chain(twin)
    now = [100.0]
    twin._monotonic_sec = lambda: now[0]
    twin.set_initial_phase(current_phase)
    source_times = _source_times(twin)

    first = _phase_evidence(
        target_phase,
        _high_switch_confidence(twin),
        source_time_sec=source_times[0],
    )
    twin.apply_phase_evidence(first)
    twin.apply_phase_evidence(
        _phase_evidence(
            current_phase,
            _high_switch_confidence(twin),
            source_time_sec=source_times[1],
        )
    )
    twin.apply_phase_evidence(first)

    assert len(twin._phase_evidence_history) == 2
    assert any(
        event.get("event_type") == "PhaseEvidenceDuplicateSuppressed"
        for event in twin.event_history
    )


def test_changed_context_on_same_source_frame_replaces_correlated_vote() -> None:
    twin = _thyroid_twin()
    current_phase, target_phase, _ = _normal_phase_chain(twin)
    now = [100.0]
    twin._monotonic_sec = lambda: now[0]
    twin.set_initial_phase(current_phase)
    source_times = _source_times(twin)

    twin.apply_phase_evidence(
        _phase_evidence(
            target_phase,
            _high_switch_confidence(twin),
            source_time_sec=source_times[0],
        )
    )
    twin.apply_phase_evidence(
        _phase_evidence(
            current_phase,
            _high_switch_confidence(twin),
            source_time_sec=source_times[1],
        )
    )
    twin.apply_phase_evidence(
        _phase_evidence(
            current_phase,
            _high_switch_confidence(twin),
            source_time_sec=source_times[0],
        )
    )

    assert len(twin._phase_evidence_history) == 2
    average, _, sample_count = twin._phase_evidence_summary(target_phase)
    assert average == 0.0
    assert sample_count == 2
    assert any(
        event.get("event_type")
        == "PhaseEvidenceCorrelatedFrameUpdated"
        for event in twin.event_history
    )


def test_source_timed_support_survives_indeterminate_occluded_frame() -> None:
    twin = _thyroid_twin()
    current_phase, target_phase, _ = _normal_phase_chain(twin)
    now = [100.0]
    twin._monotonic_sec = lambda: now[0]
    twin.set_initial_phase(current_phase)
    _set_phase_dwell_satisfied(twin, now)
    guard = twin.spec.bundle.phase_guard
    first_stamp = 10.0
    second_stamp = (
        first_stamp + float(guard.min_evidence_duration_sec) + 0.1
    )

    twin.apply_phase_evidence(
        _phase_evidence(
            target_phase,
            _high_switch_confidence(twin),
            source_time_sec=first_stamp,
        )
    )
    twin.apply_phase_evidence(
        _phase_evidence(
            current_phase,
            0.2,
            source_time_sec=(first_stamp + second_stamp) / 2.0,
            uncertainty=0.9,
        )
    )
    twin.apply_phase_evidence(
        _phase_evidence(
            target_phase,
            _high_switch_confidence(twin),
            source_time_sec=second_stamp,
        )
    )

    assert twin.state.filtered_phase == target_phase


def test_clear_alternative_phase_remains_contradictory_evidence() -> None:
    twin = _thyroid_twin()
    current_phase, target_phase, _ = _normal_phase_chain(twin)
    now = [100.0]
    twin._monotonic_sec = lambda: now[0]
    twin.set_initial_phase(current_phase)
    _set_phase_dwell_satisfied(twin, now)

    for source_time_sec, phase_id in (
        (10.0, target_phase),
        (10.6, current_phase),
        (11.2, target_phase),
    ):
        twin.apply_phase_evidence(
            _phase_evidence(
                phase_id,
                _high_switch_confidence(twin),
                source_time_sec=source_time_sec,
            )
        )

    assert twin.state.filtered_phase == current_phase
    average, _, sample_count = twin._phase_evidence_summary(target_phase)
    assert sample_count == 3
    assert average < float(
        twin.spec.bundle.phase_guard.min_confidence_to_switch
    )


def test_legacy_evidence_without_source_clock_keeps_full_window_guard() -> None:
    twin = _thyroid_twin()
    current_phase, target_phase, _ = _normal_phase_chain(twin)
    now = [100.0]
    twin._monotonic_sec = lambda: now[0]
    twin.set_initial_phase(current_phase)
    _set_phase_dwell_satisfied(twin, now)

    for _ in range(2):
        twin.apply_phase_evidence(
            _phase_evidence(
                target_phase,
                _high_switch_confidence(twin),
            )
        )
        now[0] += 1.0

    assert twin.state.filtered_phase == current_phase


def test_phase_evidence_holds_for_low_confidence_or_high_uncertainty() -> None:
    confidence_twin = _thyroid_twin()
    current_phase, target_phase, _ = _normal_phase_chain(
        confidence_twin
    )
    confidence_now = [100.0]
    confidence_twin._monotonic_sec = lambda: confidence_now[0]
    confidence_twin.set_initial_phase(current_phase)
    _set_phase_dwell_satisfied(confidence_twin, confidence_now)
    switch_threshold = float(
        confidence_twin.spec.bundle.phase_guard.min_confidence_to_switch
    )

    for source_time_sec in _source_times(confidence_twin):
        confidence_twin.apply_phase_evidence(
            _phase_evidence(
                target_phase,
                max(0.0, switch_threshold / 2.0),
                source_time_sec=source_time_sec,
            )
        )
        confidence_now[0] += 1.0

    assert confidence_twin.state.filtered_phase == current_phase

    uncertainty_twin = _thyroid_twin()
    uncertainty_now = [100.0]
    uncertainty_twin._monotonic_sec = lambda: uncertainty_now[0]
    uncertainty_twin.set_initial_phase(current_phase)
    _set_phase_dwell_satisfied(uncertainty_twin, uncertainty_now)
    for source_time_sec in _source_times(uncertainty_twin):
        uncertainty_twin.apply_phase_evidence(
            _phase_evidence(
                target_phase,
                _high_switch_confidence(uncertainty_twin),
                source_time_sec=source_time_sec,
                uncertainty=1.0,
            )
        )
        uncertainty_now[0] += 1.0

    assert uncertainty_twin.state.filtered_phase == current_phase


def test_unique_samples_with_insufficient_source_duration_hold_phase() -> None:
    twin = _thyroid_twin()
    current_phase, target_phase, _ = _normal_phase_chain(twin)
    now = [100.0]
    twin._monotonic_sec = lambda: now[0]
    twin.set_initial_phase(current_phase)
    _set_phase_dwell_satisfied(twin, now)
    sample_count = max(
        2,
        int(twin.spec.bundle.phase_guard.smoothing_window),
    )

    for source_time_sec in _source_times(
        twin,
        duration_scale=1.0 / (sample_count * sample_count),
    ):
        twin.apply_phase_evidence(
            _phase_evidence(
                target_phase,
                _high_switch_confidence(twin),
                source_time_sec=source_time_sec,
            )
        )
        now[0] += 1.0

    assert twin.state.filtered_phase == current_phase


def test_stable_source_time_evidence_cannot_bypass_physical_guard() -> None:
    twin, current_phase, target_phase = _field_guard_fixture()
    now = [100.0]
    twin._monotonic_sec = lambda: now[0]
    twin.set_initial_phase(current_phase)
    _set_phase_dwell_satisfied(twin, now)
    assert not twin._phase_field_deployment_ready(target_phase)

    for source_time_sec in _source_times(twin):
        twin.apply_phase_evidence(
            _phase_evidence(
                target_phase,
                _high_switch_confidence(twin),
                source_time_sec=source_time_sec,
            )
        )
        now[0] += 1.0

    assert twin.state.filtered_phase == current_phase
    assert any(
        event.get("event_type") == "PhaseTransitionRejected"
        and event.get("reason") == "phase_field_deployment_not_observed"
        for event in twin.event_history
    )


def test_stable_public_vlm_evidence_can_acquire_a_midprocedure_phase_once() -> None:
    twin = _thyroid_twin(open_set_bootstrap=True)
    _, _, target_phase = _normal_phase_chain(twin)
    now = [100.0]
    twin._monotonic_sec = lambda: now[0]

    decisions = []
    for source_time_sec in _source_times(twin):
        decisions.extend(
            twin.apply_phase_evidence(
                _phase_evidence(
                    target_phase,
                    _high_switch_confidence(twin),
                    source_time_sec=source_time_sec,
                )
            )
        )
        now[0] += 1.0

    assert twin.state.filtered_phase == target_phase
    assert not twin.phase_bootstrap_open
    assert any(
        decision.get("accepted")
        and decision.get("reason") == "stable_vlm_phase_bootstrap"
        for decision in decisions
    )


def test_stable_current_phase_evidence_closes_open_set_bootstrap() -> None:
    twin = _thyroid_twin(open_set_bootstrap=True)
    current_phase, _, downstream_phase = _normal_phase_chain(twin)
    now = [100.0]
    twin._monotonic_sec = lambda: now[0]

    decisions = []
    for source_time_sec in _source_times(twin):
        decisions.extend(
            twin.apply_phase_evidence(
                _phase_evidence(
                    current_phase,
                    _high_switch_confidence(twin),
                    source_time_sec=source_time_sec,
                )
            )
        )
        now[0] += 1.0

    assert twin.state.filtered_phase == current_phase
    assert not twin.phase_bootstrap_open
    assert any(
        decision.get("accepted")
        and decision.get("event_type") == "PhaseBootstrapResolved"
        and decision.get("reason") == "stable_vlm_phase_bootstrap_confirmed_current"
        for decision in decisions
    )

    for source_time_sec in (
        value + 10.0 for value in _source_times(twin)
    ):
        twin.apply_phase_evidence(
            _phase_evidence(
                downstream_phase,
                _high_switch_confidence(twin),
                source_time_sec=source_time_sec,
            )
        )
        now[0] += 1.0

    assert twin.state.filtered_phase != downstream_phase


def test_explicit_start_phase_disables_midprocedure_bootstrap() -> None:
    twin = _thyroid_twin(open_set_bootstrap=True)
    current_phase, _, downstream_phase = _normal_phase_chain(twin)
    twin.set_initial_phase(current_phase)

    for source_time_sec in _source_times(twin):
        twin.apply_phase_evidence(
            _phase_evidence(
                downstream_phase,
                _high_switch_confidence(twin),
                source_time_sec=source_time_sec,
            )
        )

    assert twin.state.filtered_phase == current_phase
    assert not twin.phase_bootstrap_open
