"""Core phase estimation logic."""

from __future__ import annotations

from collections import defaultdict, deque

from procedure_spec import ProcedureSpec
from surgical_msgs.msg import PhaseEvidence


def _stamp_to_sec(stamp) -> float:
    return float(stamp.sec) + float(stamp.nanosec) / 1_000_000_000.0


class PhaseEstimator:
    """Temporal smoothing and guarded transition logic."""

    def __init__(self, spec: ProcedureSpec):
        self.spec = spec
        window = max(int(spec.bundle.phase_guard.smoothing_window), 1)
        self._history: deque[dict[str, float]] = deque(maxlen=window)
        self._current_phase = spec.default_phase_id
        self._last_switch_sec = 0.0

    def update(
        self,
        evidence: PhaseEvidence,
        prior_phase: str,
        prior_confidence: float,
    ) -> dict[str, object]:
        scores = {
            phase_id: float(confidence)
            for phase_id, confidence in zip(evidence.phase_ids, evidence.phase_confidences)
        }
        self._history.append(scores)

        averaged = defaultdict(float)
        for sample in self._history:
            for phase_id, confidence in sample.items():
                averaged[phase_id] += confidence / len(self._history)

        chosen_prior = prior_phase or self._current_phase
        averaged[chosen_prior] += min(max(prior_confidence, 0.0), 1.0) * 0.08
        if not averaged:
            averaged[self._current_phase] = prior_confidence

        ranked = sorted(averaged.items(), key=lambda item: item[1], reverse=True)
        best_phase, best_score = ranked[0]
        second_score = ranked[1][1] if len(ranked) > 1 else 0.0

        current_phase = self._current_phase
        current_score = averaged.get(current_phase, prior_confidence)
        phase_guard = self.spec.bundle.phase_guard
        stamp_sec = _stamp_to_sec(evidence.stamp)
        dwell_elapsed = (
            self._last_switch_sec == 0.0
            or (stamp_sec - self._last_switch_sec) >= phase_guard.min_dwell_time_sec
        )

        if best_phase != current_phase:
            allowed = self.spec.is_transition_allowed(current_phase, best_phase)
            if allowed and dwell_elapsed and best_score >= phase_guard.min_confidence_to_switch:
                self._current_phase = best_phase
                self._last_switch_sec = stamp_sec
            else:
                best_phase = current_phase
                best_score = max(current_score, best_score * 0.7)

        confidence = max(min(best_score, 1.0), 0.0)
        uncertain = (
            confidence < phase_guard.min_confidence_to_keep
            or (confidence - second_score) < 0.1
            or float(evidence.uncertainty) > 0.35
        )
        stability = min(1.0, len(self._history) / max(self._history.maxlen or 1, 1)) * confidence
        rationale = (
            f"current={self._current_phase} confidence={confidence:.2f} "
            f"best_delta={(confidence - second_score):.2f} history={len(self._history)}"
        )
        return {
            "phase_id": self._current_phase,
            "confidence": confidence,
            "uncertain": uncertain,
            "stability": stability,
            "allowed_next_phases": self.spec.get_allowed_next_phases(self._current_phase),
            "rationale": rationale,
        }
