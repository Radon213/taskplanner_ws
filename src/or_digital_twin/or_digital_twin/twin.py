"""Digital twin belief manager for taskplanner v1."""

from __future__ import annotations

from collections import deque
from dataclasses import asdict
from typing import Any
import json
import re
import time

from procedure_spec import ProcedureSpec
from surgical_msgs.msg import (
    FilteredPhase,
    PhaseEvidence,
    PhaseTransitionCue,
    SurgeonActorEvent,
    SurgeonRequest,
    ToolObservation,
    TwinEvent,
)

from .models import (
    ActiveRobotTask,
    InstrumentBelief,
    SurgeonRequestCue,
    TwinState,
    LIFECYCLE_CLEANED_LEFT,
    LIFECYCLE_CLEANING_LEFT,
    LIFECYCLE_HOME_RACK,
    LIFECYCLE_MAYO_RECOVERY,
    LIFECYCLE_MAYO_REUSE,
    LIFECYCLE_PREPOSITIONED_RIGHT,
    LIFECYCLE_RECOVERING_LEFT,
    LIFECYCLE_RETURNED_HOME,
    LIFECYCLE_SURGEON_OWNED,
)


SURGEON_OWNED_LOCATION_TYPES = {"surgeon_hand", "surgical_field", "bed_fixed_tool", "return_zone"}
ACTIVE_REQUEST_INTENTS = {"request_tool", "voice_request", "extend_hand_for_handover"}
ACTIVE_RETURN_INTENTS = {"return_tool", "extend_hand_for_retrieval"}
RIGHT_HAND_LIFECYCLES = {LIFECYCLE_PREPOSITIONED_RIGHT}
LEFT_HAND_LIFECYCLES = {LIFECYCLE_RECOVERING_LEFT}
PENDING_TRANSITIONS_REQUIRE_ACTION = {"recover_left", "clean_left", "return_home", "return_unused_preposition"}
ALLOWED_EVENT_TRANSITIONS = {
    LIFECYCLE_HOME_RACK: {LIFECYCLE_PREPOSITIONED_RIGHT, LIFECYCLE_RETURNED_HOME},
    LIFECYCLE_RETURNED_HOME: {LIFECYCLE_PREPOSITIONED_RIGHT, LIFECYCLE_SURGEON_OWNED},
    LIFECYCLE_PREPOSITIONED_RIGHT: {LIFECYCLE_PREPOSITIONED_RIGHT, LIFECYCLE_SURGEON_OWNED, LIFECYCLE_RETURNED_HOME},
    LIFECYCLE_SURGEON_OWNED: {LIFECYCLE_SURGEON_OWNED, LIFECYCLE_MAYO_REUSE, LIFECYCLE_MAYO_RECOVERY, LIFECYCLE_RECOVERING_LEFT},
    LIFECYCLE_MAYO_REUSE: {LIFECYCLE_MAYO_REUSE, LIFECYCLE_SURGEON_OWNED, LIFECYCLE_MAYO_RECOVERY, LIFECYCLE_RECOVERING_LEFT},
    LIFECYCLE_MAYO_RECOVERY: {LIFECYCLE_MAYO_RECOVERY, LIFECYCLE_RECOVERING_LEFT},
    LIFECYCLE_RECOVERING_LEFT: {LIFECYCLE_RECOVERING_LEFT, LIFECYCLE_CLEANING_LEFT, LIFECYCLE_RETURNED_HOME},
    LIFECYCLE_CLEANING_LEFT: {LIFECYCLE_CLEANING_LEFT, LIFECYCLE_CLEANED_LEFT},
    LIFECYCLE_CLEANED_LEFT: {LIFECYCLE_CLEANED_LEFT, LIFECYCLE_RETURNED_HOME},
}
OBSERVATION_STICKY_DIRECT_BLOCKS = {
    (LIFECYCLE_HOME_RACK, LIFECYCLE_SURGEON_OWNED),
    (LIFECYCLE_SURGEON_OWNED, LIFECYCLE_RETURNED_HOME),
    (LIFECYCLE_MAYO_REUSE, LIFECYCLE_MAYO_RECOVERY),
}
SURGEON_ACTOR_LOCATION_EVENTS = {
    "place_on_mayo_reuse": ("mayo_reuse_zone", "mayo_reuse_zone", LIFECYCLE_MAYO_REUSE),
    "place_on_mayo_recovery": ("mayo_recovery_zone", "mayo_recovery_zone", LIFECYCLE_MAYO_RECOVERY),
    "continue_using": ("surgeon_hand", "surgeon_hand", LIFECYCLE_SURGEON_OWNED),
}


def _stamp_to_sec(stamp) -> float:
    return float(stamp.sec) + float(stamp.nanosec) / 1_000_000_000.0


def _normalize_request_text(raw_text: str) -> list[str]:
    lowered = raw_text.lower()
    cleaned = re.sub(r"[^a-z0-9_가-힣\s]", " ", lowered)
    return [token for token in cleaned.split() if token and token not in {"please", "tool"}]


def _owner_for_lifecycle(state: InstrumentBelief) -> str:
    if state.lifecycle_stage == LIFECYCLE_PREPOSITIONED_RIGHT:
        return "robot_right_hand"
    if state.lifecycle_stage in LEFT_HAND_LIFECYCLES:
        return "robot_left_hand"
    if state.lifecycle_stage == LIFECYCLE_CLEANED_LEFT:
        return "none"
    if state.lifecycle_stage == LIFECYCLE_SURGEON_OWNED:
        return "surgeon"
    return "none"


def _status_for_lifecycle(state: InstrumentBelief) -> str:
    if state.lifecycle_stage in {LIFECYCLE_HOME_RACK, LIFECYCLE_RETURNED_HOME}:
        return "available"
    if state.lifecycle_stage == LIFECYCLE_PREPOSITIONED_RIGHT:
        return "prepared"
    if state.lifecycle_stage == LIFECYCLE_SURGEON_OWNED:
        if state.location_type in {"surgical_field", "bed_fixed_tool"}:
            return "in_use"
        if state.location_type == "return_zone":
            return "presented_for_return"
        return "handed_over"
    if state.lifecycle_stage == LIFECYCLE_MAYO_REUSE:
        return "parked_for_reuse"
    if state.lifecycle_stage == LIFECYCLE_MAYO_RECOVERY:
        return "awaiting_retrieval"
    if state.lifecycle_stage == LIFECYCLE_RECOVERING_LEFT:
        return "received_return"
    if state.lifecycle_stage == LIFECYCLE_CLEANING_LEFT:
        return "cleaning"
    if state.lifecycle_stage == LIFECYCLE_CLEANED_LEFT:
        return "ready_to_return"
    return "available"


def _location_for_lifecycle(state: InstrumentBelief, lifecycle_stage: str) -> tuple[str, str]:
    if lifecycle_stage == LIFECYCLE_PREPOSITIONED_RIGHT:
        return ("robot_right_hand", "robot_right_hand")
    if lifecycle_stage == LIFECYCLE_SURGEON_OWNED:
        if state.location_type in SURGEON_OWNED_LOCATION_TYPES:
            return (state.location_type, state.location_id or "surgeon_hand")
        return ("surgeon_hand", "surgeon_hand")
    if lifecycle_stage == LIFECYCLE_MAYO_REUSE:
        return ("mayo_reuse_zone", "mayo_reuse_zone")
    if lifecycle_stage == LIFECYCLE_MAYO_RECOVERY:
        return ("mayo_recovery_zone", "mayo_recovery_zone")
    if lifecycle_stage == LIFECYCLE_RECOVERING_LEFT:
        return ("robot_left_hand", "robot_left_hand")
    if lifecycle_stage == LIFECYCLE_CLEANING_LEFT:
        return ("cleaner_slot", "cleaner_slot")
    if lifecycle_stage == LIFECYCLE_CLEANED_LEFT:
        return ("cleaner_slot", "cleaner_slot")
    return (state.home_location_type, state.home_location_id)


def _observed_lifecycle_for_location(state: InstrumentBelief, location_type: str, location_id: str) -> str:
    if location_type == "robot_right_hand":
        return LIFECYCLE_PREPOSITIONED_RIGHT
    if location_type in SURGEON_OWNED_LOCATION_TYPES:
        return LIFECYCLE_SURGEON_OWNED
    if location_type == "mayo_reuse_zone":
        return LIFECYCLE_MAYO_REUSE
    if location_type == "mayo_recovery_zone":
        return LIFECYCLE_MAYO_RECOVERY
    if location_type == "robot_left_hand":
        return LIFECYCLE_RECOVERING_LEFT
    if location_type == "cleaner_slot":
        return LIFECYCLE_CLEANED_LEFT if state.cleanliness_state == "ready" and not state.contaminated else LIFECYCLE_CLEANING_LEFT
    if location_type == state.home_location_type and location_id == state.home_location_id:
        if state.ever_surgeon_owned or state.lifecycle_stage not in {LIFECYCLE_HOME_RACK, LIFECYCLE_RETURNED_HOME}:
            return LIFECYCLE_RETURNED_HOME
        return LIFECYCLE_HOME_RACK
    return state.lifecycle_stage or LIFECYCLE_HOME_RACK


def _is_available_for_handover(state: InstrumentBelief) -> bool:
    return (
        state.lifecycle_stage in {LIFECYCLE_HOME_RACK, LIFECYCLE_RETURNED_HOME, LIFECYCLE_PREPOSITIONED_RIGHT}
        and not state.contaminated
    )


class ORDigitalTwin:
    """Event-driven runtime belief manager."""

    def __init__(self, spec: ProcedureSpec):
        self.spec = spec
        self.event_history: deque[dict[str, Any]] = deque(maxlen=200)
        self.instrument_states: dict[str, InstrumentBelief] = {}
        self._observation_candidates: dict[str, dict[str, Any]] = {}
        self._observation_violation_cooldowns: dict[tuple[str, str, str, str], float] = {}
        self._phase_evidence_history: deque[dict[str, Any]] = deque(maxlen=8)
        self._pending_phase_cues: dict[str, dict[str, Any]] = {}
        self._phase_entered_sec = self._monotonic_sec()
        self.reset_spec(spec)

    def reset_spec(self, spec: ProcedureSpec | None = None, *, seed_from_perception: bool = False) -> None:
        if spec is not None:
            self.spec = spec
        self.state = TwinState(
            procedure_id=self.spec.procedure_id,
            filtered_phase=self.spec.default_phase_id,
            phase_confidence=0.0,
            phase_uncertain=True,
            phase_stability=0.0,
        )
        self.instrument_states = {}
        self.event_history.clear()
        self._observation_candidates.clear()
        self._observation_violation_cooldowns.clear()
        self._phase_evidence_history.clear()
        self._pending_phase_cues.clear()
        self._phase_entered_sec = self._monotonic_sec()
        for instrument_id in self.spec.list_instrument_ids():
            location_id = self.spec.get_initial_location(instrument_id) or "unknown"
            location_type = self.spec.get_initial_location_type(instrument_id) or "unknown"
            self.instrument_states[instrument_id] = InstrumentBelief(
                instrument_id=instrument_id,
                home_location_type=location_type,
                home_location_id=location_id,
                location_type=location_type,
                location_id=location_id,
                owner="none",
                status="available",
                confidence=0.9,
                lifecycle_stage=LIFECYCLE_HOME_RACK,
                visual_anchor_id=location_id,
            )
        if seed_from_perception:
            self._seed_from_initial_perception()
        self._recompute_transient_state()

    def reset_runtime(self) -> None:
        self.reset_spec(self.spec, seed_from_perception=False)

    def set_execution_state(self, running: bool, execution_state: str) -> None:
        self.state.running = running
        self.state.execution_state = execution_state
        if not running and execution_state in {"idle", "halted", "completed"}:
            self._clear_active_robot_task()
            self.state.robot_state = "idle"
            self.state.cleaner_busy = False
            self.state.cleaner_remaining_sec = 0.0

    def normalize_for_publish(self) -> None:
        """Close transient invariants before exposing the authoritative state.

        Skill, surgeon-actor, and observation callbacks already recompute after
        each mutation. This extra public boundary makes the reducer contract
        explicit: no externally published snapshot should contain a temporary
        hand/cleaner occupancy conflict even if multiple inputs arrive in the
        same ROS tick.
        """
        self._sync_active_request_from_queue()
        self._recompute_transient_state()

    def _monotonic_sec(self) -> float:
        return time.monotonic()

    def _field_anchor_id(self) -> str:
        for anchor in self.spec.get_simulation_anchors():
            if anchor.id.startswith("field_region"):
                return anchor.id
        return "field_region"

    def _resolve_visual_anchor(
        self,
        *,
        lifecycle_stage: str,
        location_type: str,
        location_id: str,
        home_location_id: str,
    ) -> str:
        if location_id and any(
            location_id == anchor.id for anchor in self.spec.get_simulation_anchors()
        ):
            return location_id
        if lifecycle_stage in {LIFECYCLE_HOME_RACK, LIFECYCLE_RETURNED_HOME}:
            return home_location_id
        if lifecycle_stage == LIFECYCLE_PREPOSITIONED_RIGHT or location_type == "robot_right_hand":
            return "robot_right_hand"
        if lifecycle_stage == LIFECYCLE_RECOVERING_LEFT or location_type == "robot_left_hand":
            return "robot_left_hand"
        if lifecycle_stage in {LIFECYCLE_CLEANING_LEFT, LIFECYCLE_CLEANED_LEFT} or location_type == "cleaner_slot":
            return "cleaner_slot"
        if lifecycle_stage == LIFECYCLE_MAYO_REUSE or location_type == "mayo_reuse_zone":
            return "mayo_reuse_zone"
        if lifecycle_stage == LIFECYCLE_MAYO_RECOVERY or location_type == "mayo_recovery_zone":
            return "mayo_recovery_zone"
        if location_type == "return_zone":
            return "surgeon_return_zone"
        if location_type == "handover_zone":
            return "surgeon_receive_zone"
        if location_type == "surgeon_hand":
            return "surgeon_right_hand"
        if location_type in {"surgical_field", "bed_fixed_tool"}:
            return location_id or self._field_anchor_id()
        return location_id or home_location_id

    def _update_visual_anchor(self, state: InstrumentBelief) -> None:
        state.visual_anchor_id = self._resolve_visual_anchor(
            lifecycle_stage=state.lifecycle_stage,
            location_type=state.location_type,
            location_id=state.location_id,
            home_location_id=state.home_location_id,
        )

    def _clear_active_robot_task(self) -> None:
        self.state.active_robot_task = None

    def _clear_surgeon_request_state(self) -> None:
        self.state.surgeon_request_queue.clear()
        self.state.explicit_request_tool = ""
        self.state.surgeon_request_tool = ""
        self.state.surgeon_ready_for_handover = False
        self.state.surgeon_ready_for_retrieval = False

    def _active_request_cue(self) -> SurgeonRequestCue | None:
        return self.state.surgeon_request_queue[0] if self.state.surgeon_request_queue else None

    def _sync_active_request_from_queue(self) -> None:
        cue = self._active_request_cue()
        if cue is None:
            if self.state.surgeon_intent in ACTIVE_REQUEST_INTENTS or (
                self.state.surgeon_intent in ACTIVE_RETURN_INTENTS
                and not self.state.active_recovery_tools
            ):
                self.state.surgeon_intent = "idle"
            self.state.explicit_request_tool = ""
            self.state.surgeon_request_tool = ""
            self.state.surgeon_ready_for_handover = False
            self.state.surgeon_ready_for_retrieval = False
            return

        self.state.surgeon_intent = cue.event_type
        self.state.surgeon_request_tool = cue.instrument_id
        self.state.surgeon_ready_for_handover = bool(cue.ready_for_handover)
        self.state.surgeon_ready_for_retrieval = bool(cue.ready_for_retrieval)
        if cue.event_type in ACTIVE_REQUEST_INTENTS:
            self.state.explicit_request_tool = cue.instrument_id

    def _enqueue_surgeon_request(
        self,
        *,
        event_type: str,
        instrument_id: str,
        voice_text: str = "",
        note: str = "",
        ready_for_handover: bool = True,
        ready_for_retrieval: bool = False,
        override: bool = False,
    ) -> None:
        if not instrument_id:
            return
        cue = SurgeonRequestCue(
            event_type=event_type,
            instrument_id=instrument_id,
            voice_text=voice_text,
            note=note,
            ready_for_handover=ready_for_handover,
            ready_for_retrieval=ready_for_retrieval,
            override=override,
        )
        self.state.surgeon_request_queue.append(cue)
        self._sync_active_request_from_queue()
        self._record_event(
            "SurgeonRequestQueued",
            {
                "queued_tool": instrument_id,
                "event_type": event_type,
                "voice_text": voice_text,
                "queue_length": len(self.state.surgeon_request_queue),
                "active_request_tool": self.state.surgeon_request_tool,
            },
        )

    def _dequeue_active_request(self, reason: str) -> None:
        cue = self._active_request_cue()
        if cue is None:
            self._sync_active_request_from_queue()
            return
        completed_tool = cue.instrument_id
        self.state.surgeon_request_queue.popleft()
        self._sync_active_request_from_queue()
        self._record_event(
            "SurgeonRequestDequeued",
            {
                "completed_tool": completed_tool,
                "reason": reason,
                "queue_length": len(self.state.surgeon_request_queue),
                "active_request_tool": self.state.surgeon_request_tool,
            },
        )

    def request_queue_summary(self) -> dict[str, Any]:
        return {
            "queue_length": len(self.state.surgeon_request_queue),
            "active_request_tool": self.state.surgeon_request_tool,
            "queued_tools": [cue.instrument_id for cue in self.state.surgeon_request_queue],
        }

    def _cleanup_still_pending(self) -> bool:
        if (
            self.state.cleaner_busy
            or self.state.left_hand_tool
            or self.state.right_hand_tool
            or self.state.prepositioned_tool
            or self.state.active_recovery_tools
            or self.state.active_robot_task is not None
        ):
            return True
        for state in self.instrument_states.values():
            if state.lifecycle_stage not in {LIFECYCLE_HOME_RACK, LIFECYCLE_RETURNED_HOME}:
                return True
        return False

    def _queue_mayo_reuse_for_completion_cleanup(self, reason: str) -> None:
        if self.state.execution_state != "finishing":
            return
        for state in list(self.instrument_states.values()):
            if state.lifecycle_stage == LIFECYCLE_MAYO_REUSE:
                self._open_recovery_transaction(state.instrument_id, reason)

    def _begin_completion_cleanup(self) -> None:
        self._clear_surgeon_request_state()
        self.state.surgeon_intent = "procedure_finishing"
        if self.state.execution_state != "completed":
            self.state.running = True
            self.state.execution_state = "finishing"
            self._queue_mayo_reuse_for_completion_cleanup("completion_cleanup_queue_mayo_reuse")

    def _mark_completed(self) -> None:
        was_completed = self.state.execution_state == "completed"
        self._clear_surgeon_request_state()
        self._clear_active_robot_task()
        self.state.cleaner_busy = False
        self.state.cleaner_remaining_sec = 0.0
        self.state.left_hand_tool = ""
        self.state.right_hand_tool = ""
        self.state.prepositioned_tool = ""
        self.state.robot_state = "idle"
        self.state.running = False
        self.state.execution_state = "completed"
        self.state.surgeon_intent = "procedure_complete"
        self.state.pending_transition_tools = []
        self.state.active_recovery_tools = []
        if not was_completed:
            self._record_event(
                "ProcedureCompleted",
                {
                    "reason": "cleanup_complete",
                    "filtered_phase": self.state.filtered_phase,
                },
            )

    def _complete_if_cleanup_finished(self) -> None:
        if self.state.execution_state == "finishing" and not self._cleanup_still_pending():
            self._mark_completed()

    def _start_active_robot_task(
        self,
        *,
        task_id: str,
        task_type: str,
        instrument_id: str,
        arm: str,
        source_anchor_id: str,
        target_anchor_id: str,
        duration_sec: float,
    ) -> None:
        self.state.active_robot_task = ActiveRobotTask(
            task_id=task_id,
            task_type=task_type,
            instrument_id=instrument_id,
            arm=arm,
            source_anchor_id=source_anchor_id,
            target_anchor_id=target_anchor_id,
            started_at_sec=self._monotonic_sec(),
            duration_sec=max(float(duration_sec), 0.1),
            progress=0.0,
            remaining_sec=max(float(duration_sec), 0.0),
        )

    def _refresh_active_robot_task(self) -> None:
        task = self.state.active_robot_task
        if task is None or not task.task_id:
            return
        elapsed = max(self._monotonic_sec() - float(task.started_at_sec), 0.0)
        duration = max(float(task.duration_sec), 0.1)
        task.progress = max(0.0, min(1.0, elapsed / duration))
        task.remaining_sec = max(duration - elapsed, 0.0)

    def _seed_from_initial_perception(self) -> None:
        stages = self.spec.get_mock_perception_stages()
        if not stages:
            return
        bootstrap_index = self.spec.get_mock_perception_bootstrap_stage_index()
        stage = stages[min(bootstrap_index, len(stages) - 1)]
        if stage.phase_hypotheses:
            best_phase = max(stage.phase_hypotheses, key=lambda hypothesis: float(hypothesis.confidence))
            self.state.filtered_phase = best_phase.phase_id or self.state.filtered_phase
            self.state.phase_confidence = float(best_phase.confidence)
            self.state.phase_uncertain = bool(stage.uncertainty >= 0.35)
            self.state.phase_stability = max(self.state.phase_stability, 0.5)
        for observation in stage.observations:
            if not observation.visible:
                continue
            state = self.instrument_states.get(observation.instrument_id)
            if state is None:
                continue
            self._apply_observation_rebase(
                state=state,
                location_type=observation.location_type,
                location_id=observation.location_id,
                confidence=float(observation.confidence),
                stamp_sec=0.0,
            )

    def _record_event(self, event_type: str, payload: dict[str, Any]) -> None:
        self.event_history.append({"event_type": event_type, **payload})
        self.state.recent_event_types.appendleft(event_type)

    def _phase_evidence_summary(self, phase_id: str) -> tuple[float, float, int]:
        if not phase_id or not self._phase_evidence_history:
            return (0.0, 1.0, 0)
        recent = list(self._phase_evidence_history)[-max(1, int(self.spec.bundle.phase_guard.smoothing_window)) :]
        scores: list[float] = []
        uncertainties: list[float] = []
        for sample in recent:
            phase_scores = sample.get("scores", {})
            if not isinstance(phase_scores, dict):
                continue
            scores.append(float(phase_scores.get(phase_id, 0.0)))
            uncertainties.append(float(sample.get("uncertainty", 1.0)))
        if not scores:
            return (0.0, 1.0, 0)
        return (sum(scores) / len(scores), sum(uncertainties) / max(len(uncertainties), 1), len(scores))

    def _phase_interaction_complete(self, phase_id: str) -> bool:
        expected = list(self.spec.get_expected_instruments(phase_id))
        if not expected:
            return True
        for tool_id in expected:
            state = self.instrument_states.get(tool_id)
            if state is None:
                return False
            if state.ever_surgeon_owned:
                continue
            if state.lifecycle_stage in {
                LIFECYCLE_SURGEON_OWNED,
                LIFECYCLE_MAYO_REUSE,
                LIFECYCLE_MAYO_RECOVERY,
                LIFECYCLE_RECOVERING_LEFT,
                LIFECYCLE_CLEANING_LEFT,
                LIFECYCLE_CLEANED_LEFT,
                LIFECYCLE_RETURNED_HOME,
            }:
                continue
            return False
        return True

    def _phase_blocking_reason(self) -> str:
        if self.state.active_robot_task is not None:
            return "active_robot_task_pending"
        if self.state.pending_transition_tools:
            return "pending_tool_transition"
        if self.state.active_recovery_tools:
            return "active_recovery_pending"
        if self.state.cleaner_busy:
            return "cleaner_busy"
        if self.state.left_hand_tool:
            return "left_hand_occupied"
        return ""

    def _record_phase_decision(
        self,
        *,
        target_phase: str,
        accepted: bool,
        reason: str,
        confidence: float,
        cue_id: str = "",
    ) -> dict[str, Any]:
        event_type = "PhaseTransitionAccepted" if accepted else "PhaseTransitionRejected"
        payload = {
            "current_phase": self.state.filtered_phase,
            "target_phase": target_phase,
            "accepted": accepted,
            "reason": reason,
            "confidence": float(confidence),
            "cue_id": cue_id,
        }
        self._record_event(event_type, payload)
        return {"event_type": event_type, **payload}

    def _try_approve_phase_transition(self, target_phase: str, *, cue_id: str = "") -> dict[str, Any]:
        current_phase = self.state.filtered_phase or self.spec.default_phase_id
        guard = self.spec.bundle.phase_guard
        if not target_phase or target_phase == current_phase:
            return self._record_phase_decision(
                target_phase=target_phase or current_phase,
                accepted=False,
                reason="no_phase_change_requested",
                confidence=self.state.phase_confidence,
                cue_id=cue_id,
            )
        if target_phase not in self.spec.phase_ids:
            return self._record_phase_decision(
                target_phase=target_phase,
                accepted=False,
                reason="phase_out_of_bundle_scope",
                confidence=0.0,
                cue_id=cue_id,
            )
        if not self.spec.is_transition_allowed(current_phase, target_phase):
            return self._record_phase_decision(
                target_phase=target_phase,
                accepted=False,
                reason="phase_transition_not_allowed_by_spec",
                confidence=0.0,
                cue_id=cue_id,
            )
        cue = self._pending_phase_cues.get(target_phase)
        if cue is None:
            return self._record_phase_decision(
                target_phase=target_phase,
                accepted=False,
                reason="missing_surgeon_advance_cue",
                confidence=0.0,
                cue_id=cue_id,
            )
        dwell_elapsed = self._monotonic_sec() - self._phase_entered_sec
        min_dwell = max(float(guard.min_dwell_time_sec), float(self.spec.get_phase_min_duration(current_phase)))
        if dwell_elapsed < min_dwell:
            return self._record_phase_decision(
                target_phase=target_phase,
                accepted=False,
                reason="phase_min_dwell_not_satisfied",
                confidence=0.0,
                cue_id=str(cue.get("cue_id", cue_id)),
            )
        blocking_reason = self._phase_blocking_reason()
        if blocking_reason:
            return self._record_phase_decision(
                target_phase=target_phase,
                accepted=False,
                reason=blocking_reason,
                confidence=0.0,
                cue_id=str(cue.get("cue_id", cue_id)),
            )
        if not self._phase_interaction_complete(current_phase):
            return self._record_phase_decision(
                target_phase=target_phase,
                accepted=False,
                reason="required_tool_interaction_incomplete",
                confidence=0.0,
                cue_id=str(cue.get("cue_id", cue_id)),
            )

        average_confidence, average_uncertainty, sample_count = self._phase_evidence_summary(target_phase)
        if sample_count < max(1, int(guard.smoothing_window)):
            return self._record_phase_decision(
                target_phase=target_phase,
                accepted=False,
                reason="insufficient_phase_evidence_history",
                confidence=average_confidence,
                cue_id=str(cue.get("cue_id", cue_id)),
            )
        if average_confidence < float(guard.min_confidence_to_switch) or average_uncertainty > 0.45:
            return self._record_phase_decision(
                target_phase=target_phase,
                accepted=False,
                reason="phase_evidence_not_stable",
                confidence=average_confidence,
                cue_id=str(cue.get("cue_id", cue_id)),
            )

        self.state.filtered_phase = target_phase
        self.state.phase_confidence = float(average_confidence)
        self.state.phase_uncertain = False
        self.state.phase_stability = min(1.0, average_confidence)
        self._phase_entered_sec = self._monotonic_sec()
        self._pending_phase_cues.clear()
        if self.state.robot_state == "retracted":
            self.state.robot_state = "idle"
        self._recompute_transient_state()
        return self._record_phase_decision(
            target_phase=target_phase,
            accepted=True,
            reason="surgeon_cue_and_stable_phase_evidence",
            confidence=average_confidence,
            cue_id=str(cue.get("cue_id", cue_id)),
        )

    def apply_phase_transition_cue(self, cue: PhaseTransitionCue) -> dict[str, Any]:
        target_phase = cue.target_phase or self.state.filtered_phase
        cue_id = cue.cue_id or f"{cue.source}:{self.state.filtered_phase}->{target_phase}"
        self._pending_phase_cues[target_phase] = {
            "cue_id": cue_id,
            "source": cue.source,
            "current_phase": cue.current_phase or self.state.filtered_phase,
            "target_phase": target_phase,
            "confidence": float(cue.confidence),
            "reason": cue.reason,
            "stamp_sec": _stamp_to_sec(cue.stamp),
        }
        self._record_event(
            "PhaseTransitionCueObserved",
            {
                "cue_id": cue_id,
                "source": cue.source,
                "current_phase": cue.current_phase or self.state.filtered_phase,
                "target_phase": target_phase,
                "confidence": float(cue.confidence),
                "reason": cue.reason,
            },
        )
        return self._try_approve_phase_transition(target_phase, cue_id=cue_id)

    def apply_phase_evidence(self, evidence: PhaseEvidence) -> list[dict[str, Any]]:
        scores = {
            str(phase_id): float(confidence)
            for phase_id, confidence in zip(evidence.phase_ids, evidence.phase_confidences)
        }
        if not scores:
            return []
        self._phase_evidence_history.append(
            {
                "source": evidence.source,
                "scores": scores,
                "uncertainty": float(evidence.uncertainty),
                "stamp_sec": _stamp_to_sec(evidence.stamp),
                "summary": evidence.scene_summary,
            }
        )
        self.state.phase_uncertain = bool(float(evidence.uncertainty) > 0.35)
        self._record_event(
            "PhaseEvidenceObserved",
            {
                "source": evidence.source,
                "scores": scores,
                "uncertainty": float(evidence.uncertainty),
                "scene_summary": evidence.scene_summary,
            },
        )
        decisions: list[dict[str, Any]] = []
        for target_phase in list(self._pending_phase_cues):
            decisions.append(self._try_approve_phase_transition(target_phase))
        return decisions

    def _open_recovery_transaction(self, instrument_id: str, reason: str) -> None:
        if not instrument_id or instrument_id not in self.instrument_states:
            return
        state = self.instrument_states[instrument_id]
        if state.lifecycle_stage == LIFECYCLE_MAYO_REUSE:
            self._record_event(
                "RecoveryTransactionMarkedMayoReuse",
                {
                    "instrument_id": instrument_id,
                    "reason": reason,
                    "location_id": state.location_id,
                    "location_type": state.location_type,
                    "lifecycle_stage": state.lifecycle_stage,
                },
            )
        elif state.lifecycle_stage not in {
            LIFECYCLE_SURGEON_OWNED,
            LIFECYCLE_MAYO_RECOVERY,
            LIFECYCLE_RECOVERING_LEFT,
            LIFECYCLE_CLEANING_LEFT,
            LIFECYCLE_CLEANED_LEFT,
        }:
            self._record_event(
                "RecoveryTransactionIgnored",
                {
                    "instrument_id": instrument_id,
                    "reason": reason,
                    "lifecycle_stage": state.lifecycle_stage,
                },
            )
            return
        if instrument_id in self.state.active_recovery_tools:
            return
        self.state.active_recovery_tools.append(instrument_id)
        self._record_event(
            "RecoveryTransactionOpened",
            {
                "instrument_id": instrument_id,
                "reason": reason,
            },
        )

    def _close_recovery_transaction(self, instrument_id: str, reason: str) -> None:
        if not instrument_id or instrument_id not in self.state.active_recovery_tools:
            return
        self.state.active_recovery_tools = [
            tool_id for tool_id in self.state.active_recovery_tools if tool_id != instrument_id
        ]
        self._record_event(
            "RecoveryTransactionClosed",
            {
                "instrument_id": instrument_id,
                "reason": reason,
            },
        )

    def _recovery_transaction_active(self, instrument_id: str) -> bool:
        return bool(instrument_id and instrument_id in self.state.active_recovery_tools)

    def _set_flag(self, flag: str, enabled: bool) -> None:
        if enabled:
            if flag not in self.state.safety_flags:
                self.state.safety_flags.append(flag)
            return
        self.state.safety_flags = [existing for existing in self.state.safety_flags if existing != flag]

    def set_safety_flag(self, flag: str, enabled: bool) -> None:
        self._set_flag(flag, enabled)

    def _record_invariant_violation(
        self, *, reason: str, event_type: str, instrument_id: str, active_tool: str = "", proposed_stage: str = ""
    ) -> None:
        self._set_flag(reason, True)
        self._record_event(
            "InvariantViolationIgnored",
            {
                "reason": reason,
                "blocked_event_type": event_type,
                "instrument_id": instrument_id,
                "active_tool": active_tool,
                "proposed_stage": proposed_stage,
            },
        )

    def _record_vlm_proposal_decision(
        self,
        *,
        reducer_result: str,
        reducer_reason: str,
        source: str,
        proposal_id: str,
        instrument_id: str,
        current_stage: str,
        observed_stage: str,
        location_type: str,
        location_id: str,
        confidence: float,
    ) -> dict[str, Any]:
        event_type = {
            "accepted": "VLMProposalAccepted",
            "rejected": "VLMProposalRejected",
            "quarantined": "VLMProposalQuarantined",
        }.get(reducer_result, "VLMProposalIgnored")
        payload = {
            "source": source,
            "proposal_id": proposal_id,
            "instrument_id": instrument_id,
            "current_lifecycle": current_stage,
            "proposed_lifecycle": observed_stage,
            "proposed_transition": f"{current_stage}->{observed_stage}",
            "location_type": location_type,
            "location_id": location_id,
            "confidence": confidence,
            "reducer_result": reducer_result,
            "reducer_reason": reducer_reason,
        }
        self._record_event(event_type, payload)
        return {"event_type": event_type, **payload, "accepted": reducer_result == "accepted"}

    def _record_observation_violation(
        self,
        *,
        reason: str,
        source: str,
        proposal_id: str,
        instrument_id: str,
        current_stage: str,
        observed_stage: str,
        location_type: str,
        location_id: str,
        confidence: float,
        stamp_sec: float,
    ) -> dict[str, Any] | None:
        key = (instrument_id, current_stage, observed_stage, location_type)
        last_seen = self._observation_violation_cooldowns.get(key, -9999.0)
        if stamp_sec - last_seen < 1.0:
            return None
        self._observation_violation_cooldowns[key] = stamp_sec
        return self._record_vlm_proposal_decision(
            reducer_result="rejected",
            reducer_reason=reason,
            source=source,
            proposal_id=proposal_id,
            instrument_id=instrument_id,
            current_stage=current_stage,
            observed_stage=observed_stage,
            location_type=location_type,
            location_id=location_id,
            confidence=confidence,
        )

    def _set_lifecycle(
        self,
        state: InstrumentBelief,
        lifecycle_stage: str,
        *,
        location_type: str | None = None,
        location_id: str | None = None,
        confidence: float | None = None,
        reserved_for: str | None = None,
        last_update_sec: float | None = None,
    ) -> None:
        state.lifecycle_stage = lifecycle_stage
        if lifecycle_stage in {LIFECYCLE_SURGEON_OWNED, LIFECYCLE_MAYO_REUSE, LIFECYCLE_MAYO_RECOVERY}:
            state.ever_surgeon_owned = True
        if lifecycle_stage in {LIFECYCLE_RECOVERING_LEFT, LIFECYCLE_CLEANING_LEFT, LIFECYCLE_CLEANED_LEFT} and state.last_holder == "surgeon":
            state.ever_surgeon_owned = True

        default_location_type, default_location_id = _location_for_lifecycle(state, lifecycle_stage)
        state.location_type = location_type or default_location_type
        state.location_id = location_id or default_location_id
        state.owner = _owner_for_lifecycle(state)
        state.status = _status_for_lifecycle(state)
        if confidence is not None:
            state.confidence = confidence
        if last_update_sec is not None:
            state.last_update_sec = last_update_sec

        if lifecycle_stage in {LIFECYCLE_HOME_RACK, LIFECYCLE_PREPOSITIONED_RIGHT}:
            state.cleanliness_state = "sterile"
            state.contaminated = False
        elif lifecycle_stage == LIFECYCLE_RETURNED_HOME:
            state.cleanliness_state = "ready" if state.ever_surgeon_owned else "sterile"
            state.contaminated = False
        elif lifecycle_stage in {LIFECYCLE_SURGEON_OWNED, LIFECYCLE_MAYO_REUSE, LIFECYCLE_MAYO_RECOVERY, LIFECYCLE_RECOVERING_LEFT}:
            state.cleanliness_state = "used"
            state.contaminated = True
            state.last_holder = "surgeon"
        elif lifecycle_stage == LIFECYCLE_CLEANING_LEFT:
            state.cleanliness_state = "cleaning"
            state.contaminated = True
            state.last_holder = "surgeon"
        elif lifecycle_stage == LIFECYCLE_CLEANED_LEFT:
            state.cleanliness_state = "ready"
            state.contaminated = False
            state.last_holder = "cleaner"

        if lifecycle_stage == LIFECYCLE_PREPOSITIONED_RIGHT:
            state.last_holder = "robot_right_hand"
            if reserved_for is not None:
                state.reserved_for = reserved_for
        elif lifecycle_stage not in {LIFECYCLE_PREPOSITIONED_RIGHT}:
            state.reserved_for = ""
        self._update_visual_anchor(state)

    def _transition_allowed(self, state: InstrumentBelief, next_stage: str) -> bool:
        allowed_targets = ALLOWED_EVENT_TRANSITIONS.get(state.lifecycle_stage, set())
        return next_stage in allowed_targets

    def _has_strong_return_context(self, instrument_id: str) -> bool:
        if self._recovery_transaction_active(instrument_id):
            return True
        if self.state.surgeon_request_tool == instrument_id and self.state.surgeon_intent in ACTIVE_RETURN_INTENTS:
            return True
        if self.state.surgeon_ready_for_retrieval and (
            not self.state.surgeon_request_tool or self.state.surgeon_request_tool == instrument_id
        ):
            return True
        return False

    def _observation_transition_allowed(
        self,
        *,
        state: InstrumentBelief,
        observed_stage: str,
    ) -> tuple[bool, str]:
        current_stage = state.lifecycle_stage
        if observed_stage == current_stage:
            return (True, "")
        if (current_stage, observed_stage) in OBSERVATION_STICKY_DIRECT_BLOCKS:
            return (False, "observation_direct_rebase_forbidden")
        if not self._transition_allowed(state, observed_stage):
            return (False, "illegal_observation_transition")
        if observed_stage == LIFECYCLE_MAYO_RECOVERY and current_stage in {
            LIFECYCLE_SURGEON_OWNED,
            LIFECYCLE_MAYO_REUSE,
        }:
            if not self._has_strong_return_context(state.instrument_id):
                return (False, "observation_recovery_without_return_context")
        if observed_stage == LIFECYCLE_RETURNED_HOME and current_stage == LIFECYCLE_SURGEON_OWNED:
            return (False, "observation_direct_home_snap_forbidden")
        return (True, "")

    def _required_observation_streak(
        self,
        *,
        state: InstrumentBelief,
        observed_stage: str,
        confidence: float,
    ) -> int:
        if observed_stage == state.lifecycle_stage:
            return 1
        if confidence >= 0.97:
            return 1
        if observed_stage in {LIFECYCLE_SURGEON_OWNED, LIFECYCLE_MAYO_REUSE, LIFECYCLE_MAYO_RECOVERY}:
            return 2
        return 1

    def _clear_observation_candidate(self, instrument_id: str) -> None:
        self._observation_candidates.pop(instrument_id, None)

    def _observation_candidate_ready(
        self,
        *,
        state: InstrumentBelief,
        observed_stage: str,
        location_type: str,
        location_id: str,
        confidence: float,
        stamp_sec: float,
    ) -> bool:
        required = self._required_observation_streak(
            state=state,
            observed_stage=observed_stage,
            confidence=confidence,
        )
        if required <= 1:
            return True
        candidate = self._observation_candidates.get(state.instrument_id)
        if (
            candidate
            and candidate["stage"] == observed_stage
            and candidate["location_type"] == location_type
            and candidate["location_id"] == location_id
            and stamp_sec - float(candidate["last_seen"]) <= 3.5
        ):
            candidate["count"] = int(candidate["count"]) + 1
            candidate["last_seen"] = stamp_sec
        else:
            self._observation_candidates[state.instrument_id] = {
                "stage": observed_stage,
                "location_type": location_type,
                "location_id": location_id,
                "count": 1,
                "first_seen": stamp_sec,
                "last_seen": stamp_sec,
            }
            return False
        return int(candidate["count"]) >= required

    def _apply_event_transition(
        self,
        *,
        state: InstrumentBelief,
        next_stage: str,
        event_type: str,
        location_type: str | None = None,
        location_id: str | None = None,
        confidence: float | None = None,
        reserved_for: str | None = None,
    ) -> bool:
        if not self._transition_allowed(state, next_stage):
            self._record_invariant_violation(
                reason="illegal_lifecycle_transition",
                event_type=event_type,
                instrument_id=state.instrument_id,
                proposed_stage=next_stage,
            )
            return False
        self._set_lifecycle(
            state,
            next_stage,
            location_type=location_type,
            location_id=location_id,
            confidence=confidence,
            reserved_for=reserved_for,
            last_update_sec=getattr(self, "_current_event_stamp_sec", None),
        )
        self._clear_observation_candidate(state.instrument_id)
        return True

    def _apply_observation_rebase(
        self,
        *,
        state: InstrumentBelief,
        location_type: str,
        location_id: str,
        confidence: float,
        stamp_sec: float,
    ) -> None:
        observed_stage = _observed_lifecycle_for_location(state, location_type, location_id)
        self._set_lifecycle(
            state,
            observed_stage,
            location_type=location_type,
            location_id=location_id,
            confidence=confidence,
        )
        state.last_update_sec = stamp_sec
        self._clear_observation_candidate(state.instrument_id)

    def _clear_satisfied_request(self) -> None:
        self._sync_active_request_from_queue()
        requested_tool = self.state.surgeon_request_tool or self.state.explicit_request_tool
        if not requested_tool:
            return
        requested_state = self.instrument_states.get(requested_tool)
        if requested_state is None:
            return

        if self.state.surgeon_intent in ACTIVE_REQUEST_INTENTS and requested_state.lifecycle_stage in {
            LIFECYCLE_SURGEON_OWNED,
            LIFECYCLE_MAYO_REUSE,
        }:
            self._dequeue_active_request("requested_tool_handed_over")
            return

        if self.state.surgeon_intent in ACTIVE_RETURN_INTENTS and requested_state.lifecycle_stage in {
            LIFECYCLE_RECOVERING_LEFT,
            LIFECYCLE_CLEANING_LEFT,
            LIFECYCLE_CLEANED_LEFT,
            LIFECYCLE_RETURNED_HOME,
        }:
            self._dequeue_active_request("requested_tool_retrieved")

    def _derive_next_required_transition(self, state: InstrumentBelief) -> str:
        if state.lifecycle_stage == LIFECYCLE_PREPOSITIONED_RIGHT:
            if self.state.execution_state in {"finishing", "completed"}:
                return "return_unused_preposition"
            if state.instrument_id not in self.get_expected_instruments():
                return "return_unused_preposition"
            requested_tool = self.state.surgeon_request_tool or self.state.explicit_request_tool
            if requested_tool and requested_tool != state.instrument_id:
                return "return_unused_preposition"
            if self.state.right_hand_tool and self.state.right_hand_tool != state.instrument_id:
                return "return_unused_preposition"
            return ""
        if state.lifecycle_stage == LIFECYCLE_SURGEON_OWNED:
            if self.state.execution_state == "finishing":
                return "recover_left"
            if self._recovery_transaction_active(state.instrument_id):
                return "recover_left"
            if (
                self.state.surgeon_intent in ACTIVE_RETURN_INTENTS
                and self.state.surgeon_request_tool == state.instrument_id
            ):
                return "recover_left"
            return ""
        if state.lifecycle_stage == LIFECYCLE_MAYO_REUSE:
            if self.state.execution_state == "finishing":
                return "recover_left"
            if self._recovery_transaction_active(state.instrument_id):
                return "recover_left"
            return ""
        if state.lifecycle_stage == LIFECYCLE_MAYO_RECOVERY:
            return "recover_left"
        if state.lifecycle_stage == LIFECYCLE_RECOVERING_LEFT:
            return "clean_left" if state.contaminated else "return_home"
        if state.lifecycle_stage == LIFECYCLE_CLEANING_LEFT:
            return "clean_left"
        if state.lifecycle_stage == LIFECYCLE_CLEANED_LEFT:
            return "return_home"
        return ""

    def _recompute_transient_state(self) -> None:
        self._refresh_active_robot_task()
        self._normalize_surgeon_hand_conflicts()
        self._normalize_cleaner_conflicts()
        self._queue_mayo_reuse_for_completion_cleanup("completion_cleanup_recompute")
        for tool_id in list(self.state.active_recovery_tools):
            state = self.instrument_states.get(tool_id)
            if state is None or state.lifecycle_stage in {LIFECYCLE_HOME_RACK, LIFECYCLE_RETURNED_HOME}:
                self._close_recovery_transaction(tool_id, "terminal_lifecycle_observed")
        right_candidates = [
            state
            for state in self.instrument_states.values()
            if state.lifecycle_stage in RIGHT_HAND_LIFECYCLES
        ]
        left_candidates = [
            state
            for state in self.instrument_states.values()
            if state.lifecycle_stage in LEFT_HAND_LIFECYCLES
        ]

        self.state.right_hand_tool = max(
            right_candidates, key=lambda state: state.last_update_sec or 0.0, default=None
        ).instrument_id if right_candidates else ""
        self.state.left_hand_tool = max(
            left_candidates, key=lambda state: state.last_update_sec or 0.0, default=None
        ).instrument_id if left_candidates else ""
        self.state.prepositioned_tool = self.state.right_hand_tool if right_candidates else ""
        self.state.cleaner_busy = any(
            state.lifecycle_stage == LIFECYCLE_CLEANING_LEFT for state in self.instrument_states.values()
        )
        if not self.state.cleaner_busy:
            self.state.cleaner_remaining_sec = 0.0

        pending_tools: list[str] = []
        for state in self.instrument_states.values():
            state.next_required_transition = self._derive_next_required_transition(state)
            if state.next_required_transition:
                pending_tools.append(state.instrument_id)
        self.state.pending_transition_tools = pending_tools

        right_conflict = len({state.instrument_id for state in right_candidates}) > 1
        left_conflict = len({state.instrument_id for state in left_candidates}) > 1
        self._set_flag("right_arm_overloaded", right_conflict)
        self._set_flag("left_arm_overloaded", left_conflict)
        surgeon_owned_count = sum(
            1 for state in self.instrument_states.values() if state.lifecycle_stage == LIFECYCLE_SURGEON_OWNED
        )
        surgeon_owned_overloaded = surgeon_owned_count > 2
        was_surgeon_overloaded = "surgeon_owned_overloaded" in self.state.safety_flags
        self._set_flag("surgeon_owned_overloaded", surgeon_owned_overloaded)
        if surgeon_owned_overloaded and not was_surgeon_overloaded:
            self._record_event(
                "InvariantViolationIgnored",
                {
                    "reason": "surgeon_owned_overloaded",
                    "surgeon_owned_count": surgeon_owned_count,
                },
            )
        self._clear_satisfied_request()
        self._normalize_robot_state()
        self._complete_if_cleanup_finished()

    def _normalize_surgeon_hand_conflicts(self) -> None:
        hand_states = [
            state
            for state in self.instrument_states.values()
            if state.lifecycle_stage == LIFECYCLE_SURGEON_OWNED
            and (state.location_type == "surgeon_hand" or state.status == "handed_over")
        ]
        if len(hand_states) <= 1:
            return
        hand_states.sort(key=lambda state: state.last_update_sec or 0.0, reverse=True)
        for stale_state in hand_states[1:]:
            self._set_lifecycle(
                stale_state,
                LIFECYCLE_MAYO_RECOVERY,
                location_type="mayo_recovery_zone",
                location_id="mayo_recovery_zone",
                confidence=max(stale_state.confidence, 0.9),
            )
            self._record_event(
                "SurgeonHandConflictNormalized",
                {
                    "instrument_id": stale_state.instrument_id,
                    "kept_tool": hand_states[0].instrument_id,
                    "target_stage": LIFECYCLE_MAYO_RECOVERY,
                },
            )
            self._open_recovery_transaction(stale_state.instrument_id, "surgeon_hand_conflict_normalized")

    def _normalize_cleaner_conflicts(self) -> None:
        cleaner_states = [
            state
            for state in self.instrument_states.values()
            if state.lifecycle_stage == LIFECYCLE_CLEANING_LEFT
        ]
        if len(cleaner_states) <= 1:
            return
        active_task_tool = self.state.active_robot_task.instrument_id if self.state.active_robot_task is not None else ""
        cleaner_states.sort(
            key=lambda state: (
                0 if state.instrument_id == active_task_tool else 1,
                state.last_update_sec or 0.0,
                state.instrument_id,
            )
        )
        kept_state = cleaner_states[0]
        for queued_state in cleaner_states[1:]:
            self._set_lifecycle(
                queued_state,
                LIFECYCLE_RECOVERING_LEFT,
                location_type="robot_left_hand",
                location_id="robot_left_hand",
                confidence=max(queued_state.confidence, 0.9),
            )
            self._record_event(
                "CleanerConflictQueued",
                {
                    "instrument_id": queued_state.instrument_id,
                    "kept_tool": kept_state.instrument_id,
                    "reason": "cleaner_single_tool_invariant",
                },
            )
            self._open_recovery_transaction(queued_state.instrument_id, "cleaner_single_tool_invariant")

    def _normalize_robot_state(self) -> None:
        if self.state.robot_state == "fault":
            return
        task = self.state.active_robot_task
        if task is not None and task.task_id:
            if task.task_type in {"insert_into_cleaner", "cleaning_hold"}:
                self.state.robot_state = "cleaning"
            elif task.task_type == "auto_return_to_rack":
                self.state.robot_state = "returning_home"
            elif task.task_type == "move_to_handover":
                self.state.robot_state = "handover_in_progress"
            elif task.task_type == "receive_from_recovery_zone":
                self.state.robot_state = "recovery_in_progress"
            elif task.task_type == "pick_from_rack":
                self.state.robot_state = "picking"
            else:
                self.state.robot_state = "busy"
            return
        if self.state.cleaner_busy:
            self.state.robot_state = "cleaning"
            return
        left_state = self.instrument_states.get(self.state.left_hand_tool) if self.state.left_hand_tool else None
        if left_state is not None:
            self.state.robot_state = "busy"
            return
        if any(state.lifecycle_stage == LIFECYCLE_CLEANED_LEFT for state in self.instrument_states.values()):
            self.state.robot_state = "ready_to_return"
            return
        right_state = self.instrument_states.get(self.state.right_hand_tool) if self.state.right_hand_tool else None
        if right_state is not None:
            self.state.robot_state = "handover_ready"
            return
        if self.state.phase_uncertain and self.state.robot_state == "retracted":
            return
        self.state.robot_state = "idle"

    def _right_arm_conflict(self, instrument_id: str) -> bool:
        self._recompute_transient_state()
        return bool(self.state.right_hand_tool and self.state.right_hand_tool != instrument_id)

    def _left_arm_conflict(self, instrument_id: str) -> bool:
        self._recompute_transient_state()
        return bool(self.state.left_hand_tool and self.state.left_hand_tool != instrument_id)

    def _surgeon_hand_conflict(self, instrument_id: str) -> str:
        for tool_id, state in self.instrument_states.items():
            if tool_id == instrument_id:
                continue
            if state.lifecycle_stage != LIFECYCLE_SURGEON_OWNED:
                continue
            if state.location_type == "surgeon_hand" or state.status == "handed_over":
                return tool_id
        return ""

    def update_explicit_request(self, request_text: str) -> str:
        if not request_text.strip():
            self._record_event("ExplicitRequestUpdated", {"text": request_text, "resolved_tool": ""})
            return ""
        resolved = ""
        for token in _normalize_request_text(request_text):
            resolved = self.spec.resolve_instrument_alias(token) or ""
            if resolved:
                break
        if resolved:
            self._enqueue_surgeon_request(
                event_type="voice_request",
                instrument_id=resolved,
                voice_text=request_text,
                note="explicit voice text resolved to request cue",
                ready_for_handover=True,
            )
        self._recompute_transient_state()
        self._record_event("ExplicitRequestUpdated", {"text": request_text, "resolved_tool": resolved})
        return resolved

    def update_surgeon_request(self, request: SurgeonRequest) -> str:
        resolved = self.spec.resolve_instrument_alias(request.requested_tool) or request.requested_tool
        if not resolved and request.voice_text:
            for token in _normalize_request_text(request.voice_text):
                resolved = self.spec.resolve_instrument_alias(token) or ""
                if resolved:
                    break

        if request.event_type == "cancel_request":
            self._record_event(
                "LegacyCancelRequestIgnored",
                {
                    "requested_tool": resolved,
                    "voice_text": request.voice_text,
                    "queue_length": len(self.state.surgeon_request_queue),
                    "active_request_tool": self.state.surgeon_request_tool,
                },
            )
        elif request.event_type == "request_procedure_completion":
            self._begin_completion_cleanup()
        elif request.event_type == "complete_procedure":
            if self._cleanup_still_pending():
                self._begin_completion_cleanup()
            else:
                self._mark_completed()
        elif request.event_type in ACTIVE_REQUEST_INTENTS:
            self._enqueue_surgeon_request(
                event_type=request.event_type or "request_tool",
                instrument_id=resolved,
                voice_text=request.voice_text,
                note=request.note,
                ready_for_handover=bool(request.ready_for_handover or resolved),
                ready_for_retrieval=False,
                override=bool(request.override),
            )
        else:
            self.state.surgeon_intent = request.event_type or self.state.surgeon_intent
            self.state.surgeon_request_tool = resolved
            self.state.surgeon_ready_for_handover = bool(request.ready_for_handover)
            self.state.surgeon_ready_for_retrieval = bool(request.ready_for_retrieval)
            if request.event_type in ACTIVE_RETURN_INTENTS:
                self._open_recovery_transaction(resolved, "surgeon_request_return_tool")

        self._recompute_transient_state()
        self._record_event(
            "SurgeonRequestUpdated",
            {
                "event_type": request.event_type,
                "requested_tool": resolved,
                "voice_text": request.voice_text,
                "override": bool(request.override),
            },
        )
        return resolved

    def apply_surgeon_actor_event(self, event: SurgeonActorEvent) -> None:
        event_type = (event.event_type or "").strip()
        tool_id = self.spec.resolve_instrument_alias(event.tool_id) or event.tool_id
        phase_id = event.phase_id or self.state.filtered_phase
        state = self.instrument_states.get(tool_id) if tool_id else None

        if event_type in {"request_tool", "voice_request"}:
            # /surgeon/request is the canonical cue source. Actor events are kept
            # as observability signals so the same mock decision is not queued twice.
            self._sync_active_request_from_queue()
        elif event_type == "return_tool":
            self.state.surgeon_intent = event_type
            self.state.surgeon_request_tool = tool_id
            self.state.surgeon_ready_for_handover = False
            self.state.surgeon_ready_for_retrieval = bool(event.ready_for_retrieval or tool_id)
            self._open_recovery_transaction(tool_id, "surgeon_actor_return_tool")
        elif event_type == "cancel_request":
            self._record_event(
                "LegacyCancelActorEventIgnored",
                {
                    "instrument_id": tool_id,
                    "queue_length": len(self.state.surgeon_request_queue),
                    "active_request_tool": self.state.surgeon_request_tool,
                },
            )
        elif event_type in {"advance_phase", "advance_phase_cue"}:
            cue = PhaseTransitionCue()
            cue.stamp = event.stamp
            cue.source = "mock_surgeon"
            cue.cue_id = f"surgeon:{self.state.filtered_phase}->{phase_id}:{_stamp_to_sec(event.stamp):.3f}"
            cue.current_phase = self.state.filtered_phase
            cue.target_phase = phase_id
            cue.confidence = 0.96
            cue.reason = event.note or event_type
            self.apply_phase_transition_cue(cue)
        elif event_type == "request_procedure_completion":
            self._begin_completion_cleanup()
        elif event_type == "complete_procedure":
            if self._cleanup_still_pending():
                self._begin_completion_cleanup()
            else:
                self._mark_completed()
        elif event_type in SURGEON_ACTOR_LOCATION_EVENTS and state is not None:
            location_type, location_id, next_stage = SURGEON_ACTOR_LOCATION_EVENTS[event_type]
            reuse_tool_marked_for_recovery = (
                event_type == "place_on_mayo_recovery"
                and state.lifecycle_stage == LIFECYCLE_MAYO_REUSE
            )
            if reuse_tool_marked_for_recovery:
                self.state.surgeon_intent = "return_tool"
                self.state.surgeon_ready_for_handover = False
                self.state.surgeon_ready_for_retrieval = True
                self._open_recovery_transaction(tool_id, "surgeon_actor_place_on_mayo_recovery")
            else:
                if not self._apply_event_transition(
                    state=state,
                    next_stage=next_stage,
                    event_type=event_type,
                    location_type=location_type,
                    location_id=location_id,
                    confidence=max(state.confidence, 0.96),
                ):
                    return
                if event_type == "continue_using":
                    self.state.surgeon_intent = "continue_using"
                    self.state.surgeon_ready_for_handover = False
                    self.state.surgeon_ready_for_retrieval = False
                elif event_type == "place_on_mayo_reuse":
                    self.state.surgeon_intent = "park_for_reuse"
                    self.state.surgeon_ready_for_handover = False
                    self.state.surgeon_ready_for_retrieval = False
                elif event_type == "place_on_mayo_recovery":
                    self.state.surgeon_intent = "return_tool"
                    self.state.surgeon_ready_for_handover = False
                    self.state.surgeon_ready_for_retrieval = True
                    self._open_recovery_transaction(tool_id, "surgeon_actor_place_on_mayo_recovery")

        self._recompute_transient_state()
        self._record_event(
            "SurgeonActorEventApplied",
            {
                "event_type": event_type,
                "instrument_id": tool_id,
                "phase_id": phase_id,
                "override": bool(event.override),
                "voice_text": event.voice_text,
                "note": event.note,
            },
        )

    def update_phase(self, filtered_phase: FilteredPhase) -> None:
        phase_id = filtered_phase.phase_id or self.state.filtered_phase
        if phase_id not in self.spec.phase_ids:
            self._record_invariant_violation(
                reason="phase_out_of_bundle_scope",
                phase_id=phase_id,
                active_procedure=self.spec.procedure_id,
            )
            phase_id = self.state.filtered_phase if self.state.filtered_phase in self.spec.phase_ids else self.spec.default_phase_id
        self.state.filtered_phase = phase_id
        self.state.phase_confidence = float(filtered_phase.confidence)
        self.state.phase_uncertain = bool(filtered_phase.uncertain)
        self.state.phase_stability = float(filtered_phase.stability)
        if not self.state.phase_uncertain and self.state.robot_state == "retracted":
            self.state.robot_state = "idle"
        self._record_event(
            "PhaseUpdated",
            {
                "phase_id": self.state.filtered_phase,
                "confidence": self.state.phase_confidence,
                "uncertain": self.state.phase_uncertain,
            },
        )
        self._recompute_transient_state()

    def reconcile_observation(
        self,
        observation: ToolObservation,
        *,
        source: str = "legacy_tool_observation",
        proposal_id: str = "",
    ) -> dict[str, Any] | None:
        if not observation.visible:
            return None
        instrument_id = self.spec.resolve_instrument_alias(observation.instrument_id) or observation.instrument_id
        if instrument_id not in self.instrument_states:
            return None
        current = self.instrument_states[instrument_id]
        location_type = observation.location_type or current.location_type
        location_id = observation.location_id or current.location_id
        confidence = float(observation.confidence)
        stamp_sec = _stamp_to_sec(observation.stamp)
        proposal_id = proposal_id or f"{source}:{instrument_id}:{location_type}:{location_id}:{stamp_sec:.3f}"
        observed_stage = _observed_lifecycle_for_location(current, location_type, location_id)

        allowed, violation_reason = self._observation_transition_allowed(
            state=current,
            observed_stage=observed_stage,
        )
        if not allowed:
            result = self._record_observation_violation(
                reason=violation_reason,
                source=source,
                proposal_id=proposal_id,
                instrument_id=instrument_id,
                current_stage=current.lifecycle_stage,
                observed_stage=observed_stage,
                location_type=location_type,
                location_id=location_id,
                confidence=confidence,
                stamp_sec=stamp_sec,
            )
            self._clear_observation_candidate(instrument_id)
            return result

        if not self._observation_candidate_ready(
            state=current,
            observed_stage=observed_stage,
            location_type=location_type,
            location_id=location_id,
            confidence=confidence,
            stamp_sec=stamp_sec,
        ):
            return self._record_vlm_proposal_decision(
                reducer_result="quarantined",
                reducer_reason="awaiting_observation_hysteresis",
                source=source,
                proposal_id=proposal_id,
                instrument_id=instrument_id,
                current_stage=current.lifecycle_stage,
                observed_stage=observed_stage,
                location_type=location_type,
                location_id=location_id,
                confidence=confidence,
            )

        previous_stage = current.lifecycle_stage
        self._apply_observation_rebase(
            state=current,
            location_type=location_type,
            location_id=location_id,
            confidence=confidence,
            stamp_sec=stamp_sec,
        )
        self._recompute_transient_state()
        return self._record_vlm_proposal_decision(
            reducer_result="accepted",
            reducer_reason="legal_observation_transition" if previous_stage != current.lifecycle_stage else "state_already_consistent",
            source=source,
            proposal_id=proposal_id,
            instrument_id=instrument_id,
            current_stage=previous_stage,
            observed_stage=current.lifecycle_stage,
            location_type=current.location_type,
            location_id=current.location_id,
            confidence=confidence,
        )

    def apply_event(self, event: TwinEvent) -> None:
        instrument_id = event.instrument_id
        state = self.instrument_states.get(instrument_id) if instrument_id else None
        detail = json.loads(event.detail_json) if event.detail_json else {}
        self._current_event_stamp_sec = _stamp_to_sec(event.stamp)
        self._recompute_transient_state()

        if event.event_type == "RobotTaskStarted":
            self._start_active_robot_task(
                task_id=str(detail.get("task_id", detail.get("command_id", instrument_id))),
                task_type=str(detail.get("task_type", event.mode or event.event_type)),
                instrument_id=instrument_id,
                arm=event.arm or str(detail.get("arm", "")),
                source_anchor_id=event.source_location_id or str(detail.get("source_anchor_id", "")),
                target_anchor_id=event.target_location_id or str(detail.get("target_anchor_id", "")),
                duration_sec=float(detail.get("duration_sec", 0.1)),
            )
            self._record_event(
                event.event_type,
                {
                    "instrument_id": instrument_id,
                    **detail,
                },
            )
            self._recompute_transient_state()
            return

        if event.event_type == "RobotTaskCompleted":
            self._clear_active_robot_task()
            self._record_event(
                event.event_type,
                {
                    "instrument_id": instrument_id,
                    **detail,
                },
            )
            self._recompute_transient_state()
            return

        if state and event.event_type in {
            "RobotGraspedTool",
            "ToolPrepared",
            "ToolHandoverCompleted",
            "PredictedToolReturnedToRack",
        }:
            if self._right_arm_conflict(instrument_id):
                self._record_invariant_violation(
                    reason="right_arm_busy",
                    event_type=event.event_type,
                    instrument_id=instrument_id,
                    active_tool=self.state.right_hand_tool,
                )
                return

        if state and event.event_type in {
            "ToolReceivedFromSurgeon",
            "ToolSentToCleaner",
            "ToolCleaningProgress",
            "ToolCleaningCompleted",
            "ToolReturnedToTray",
        }:
            if self._left_arm_conflict(instrument_id):
                self._record_invariant_violation(
                    reason="left_arm_busy",
                    event_type=event.event_type,
                    instrument_id=instrument_id,
                    active_tool=self.state.left_hand_tool,
                )
                return

        if event.event_type == "RobotGraspedTool" and state:
            if not self._apply_event_transition(
                state=state,
                next_stage=LIFECYCLE_PREPOSITIONED_RIGHT,
                event_type=event.event_type,
                location_type=event.target_location_type or event.location_type or "robot_right_hand",
                location_id=event.target_location_id or event.location_id or "robot_right_hand",
                confidence=max(float(event.confidence), 0.95),
            ):
                return
            self.state.robot_state = "busy"
        elif event.event_type == "ToolPrepared" and state:
            if not self._apply_event_transition(
                state=state,
                next_stage=LIFECYCLE_PREPOSITIONED_RIGHT,
                event_type=event.event_type,
                location_type=event.target_location_type or event.location_type or "robot_right_hand",
                location_id=event.target_location_id or event.location_id or "robot_right_hand",
                confidence=max(float(event.confidence), 0.95),
                reserved_for=detail.get("reserved_for", self.state.filtered_phase),
            ):
                return
            self.state.robot_state = "prepared"
        elif event.event_type == "ToolHandoverCompleted" and state:
            active_surgeon_hand_tool = self._surgeon_hand_conflict(instrument_id)
            if active_surgeon_hand_tool:
                previous_state = self.instrument_states.get(active_surgeon_hand_tool)
                if previous_state is not None:
                    self._record_event(
                        "SurgeonHandConflictNormalized",
                        {
                            "instrument_id": active_surgeon_hand_tool,
                            "incoming_tool": instrument_id,
                            "target_stage": LIFECYCLE_MAYO_RECOVERY,
                        },
                    )
                    self._apply_event_transition(
                        state=previous_state,
                        next_stage=LIFECYCLE_MAYO_RECOVERY,
                        event_type="SurgeonHandConflictNormalized",
                        location_type="mayo_recovery_zone",
                        location_id="mayo_recovery_zone",
                        confidence=max(previous_state.confidence, 0.9),
                    )
                    self._open_recovery_transaction(active_surgeon_hand_tool, "surgeon_hand_conflict_normalized")
            if not self._apply_event_transition(
                state=state,
                next_stage=LIFECYCLE_SURGEON_OWNED,
                event_type=event.event_type,
                location_type="surgeon_hand",
                location_id="surgeon_hand",
                confidence=max(float(event.confidence), 0.95),
            ):
                return
            self.state.robot_state = "idle"
            if self.state.surgeon_request_tool == instrument_id:
                self._dequeue_active_request("handover_completed")
            else:
                self._sync_active_request_from_queue()
        elif event.event_type == "ToolReceivedFromSurgeon" and state:
            self._open_recovery_transaction(instrument_id, "robot_received_returned_tool")
            if not self._apply_event_transition(
                state=state,
                next_stage=LIFECYCLE_RECOVERING_LEFT,
                event_type=event.event_type,
                location_type="robot_left_hand",
                location_id="robot_left_hand",
                confidence=max(float(event.confidence), 0.95),
            ):
                return
            if self.state.surgeon_request_tool == instrument_id:
                self._dequeue_active_request("retrieval_started")
            else:
                self._sync_active_request_from_queue()
        elif event.event_type == "ToolSentToCleaner" and state:
            active_cleaner_tool = next(
                (
                    tool_id
                    for tool_id, cleaner_state in self.instrument_states.items()
                    if tool_id != instrument_id and cleaner_state.lifecycle_stage == LIFECYCLE_CLEANING_LEFT
                ),
                "",
            )
            if active_cleaner_tool:
                self._set_lifecycle(
                    state,
                    LIFECYCLE_RECOVERING_LEFT,
                    location_type="robot_left_hand",
                    location_id="robot_left_hand",
                    confidence=max(float(event.confidence), 0.9),
                )
                self.state.cleaner_busy = True
                self.state.robot_state = "busy"
                self._open_recovery_transaction(instrument_id, "cleaner_busy_queued")
                self._record_event(
                    "CleanerBusyToolQueued",
                    {
                        "instrument_id": instrument_id,
                        "active_cleaner_tool": active_cleaner_tool,
                        "reason": "cleaner_allows_one_tool",
                    },
                )
                self._recompute_transient_state()
                return
            if not self._apply_event_transition(
                state=state,
                next_stage=LIFECYCLE_CLEANING_LEFT,
                event_type=event.event_type,
                location_type="cleaner_slot",
                location_id="cleaner_slot",
                confidence=max(float(event.confidence), 0.95),
            ):
                return
            self.state.cleaner_busy = True
            self.state.cleaner_remaining_sec = float(detail.get("remaining_sec", 0.0))
            self.state.robot_state = "cleaning"
        elif event.event_type == "ToolCleaningProgress" and state:
            if state.lifecycle_stage != LIFECYCLE_CLEANING_LEFT:
                self._record_invariant_violation(
                    reason="cleaning_progress_without_cleaning_state",
                    event_type=event.event_type,
                    instrument_id=instrument_id,
                    proposed_stage=LIFECYCLE_CLEANING_LEFT,
                )
                return
            self.state.cleaner_busy = True
            self.state.cleaner_remaining_sec = float(detail.get("remaining_sec", self.state.cleaner_remaining_sec))
            self.state.robot_state = "cleaning"
        elif event.event_type == "ToolCleaningCompleted" and state:
            if not self._apply_event_transition(
                state=state,
                next_stage=LIFECYCLE_CLEANED_LEFT,
                event_type=event.event_type,
                location_type="cleaner_slot",
                location_id="cleaner_slot",
                confidence=max(float(event.confidence), 0.95),
            ):
                return
            self.state.cleaner_busy = False
            self.state.cleaner_remaining_sec = 0.0
            self.state.robot_state = "ready_to_return"
        elif event.event_type in {"ToolReturnedToTray", "PredictedToolReturnedToRack"} and state:
            if not self._apply_event_transition(
                state=state,
                next_stage=LIFECYCLE_RETURNED_HOME,
                event_type=event.event_type,
                location_type=event.target_location_type or state.home_location_type,
                location_id=event.target_location_id or state.home_location_id,
                confidence=max(float(event.confidence), 0.95),
            ):
                return
            self.state.robot_state = "idle"
            if event.event_type == "ToolReturnedToTray":
                self.state.cleaner_busy = False
                self.state.cleaner_remaining_sec = 0.0
                self._close_recovery_transaction(instrument_id, "tool_returned_home")
        elif event.event_type == "SafetyRetract":
            if self.state.left_hand_tool or self.state.right_hand_tool or self.state.cleaner_busy:
                self._record_invariant_violation(
                    reason="retract_blocked_by_payload",
                    event_type=event.event_type,
                    instrument_id=instrument_id or "",
                    active_tool=self.state.left_hand_tool or self.state.right_hand_tool,
                )
            else:
                self.state.robot_state = "retracted"
        elif event.event_type == "RobotWentIdle":
            self.state.robot_state = "idle"

        self._recompute_transient_state()
        self._record_event(
            event.event_type,
            {
                "instrument_id": instrument_id,
                "lifecycle_stage": state.lifecycle_stage if state is not None else "",
                **detail,
            },
        )

    def get_expected_instruments(self) -> list[str]:
        phase_id = self.state.filtered_phase
        if phase_id not in self.spec.phase_ids:
            phase_id = self.spec.default_phase_id
        return self.spec.get_expected_instruments(phase_id)

    def get_available_instruments(self) -> list[str]:
        return [
            instrument_id
            for instrument_id, state in self.instrument_states.items()
            if _is_available_for_handover(state)
        ]

    def handover_allowed(self) -> bool:
        if self.state.safety_flags:
            return False
        if (
            self.spec.bundle.action_guard
            and self.spec.bundle.action_guard.block_handover_when_phase_uncertain
            and self.state.phase_uncertain
        ):
            return False
        if self.state.active_robot_task is not None:
            return False
        if self.state.robot_state in {"fault", "retracted"} or self.state.cleaner_busy:
            return False
        requested_tool = self.state.surgeon_request_tool or self.state.explicit_request_tool
        if not requested_tool:
            return True
        requested_state = self.instrument_states.get(requested_tool)
        if requested_state is None:
            return False
        if self.state.surgeon_intent in ACTIVE_REQUEST_INTENTS and not self.state.surgeon_ready_for_handover:
            return False
        if requested_state.contaminated:
            return False
        if requested_state.lifecycle_stage in {LIFECYCLE_SURGEON_OWNED, LIFECYCLE_MAYO_REUSE}:
            return False
        if requested_state.lifecycle_stage not in {LIFECYCLE_HOME_RACK, LIFECYCLE_RETURNED_HOME, LIFECYCLE_PREPOSITIONED_RIGHT}:
            return False
        if requested_state.owner not in {"", "none", "robot_right_hand"}:
            return False
        return True

    def recovery_required(self) -> bool:
        return bool(self.state.active_recovery_tools) or any(
            state.next_required_transition in PENDING_TRANSITIONS_REQUIRE_ACTION
            for state in self.instrument_states.values()
        )

    def instrument_payload(self) -> list[dict[str, Any]]:
        return [asdict(state) for state in self.instrument_states.values()]

    def get_simulation_snapshot(self) -> dict[str, Any]:
        self._refresh_active_robot_task()
        return {
            "procedure_id": self.state.procedure_id,
            "running": self.state.running,
            "execution_state": self.state.execution_state,
            "filtered_phase": self.state.filtered_phase,
            "robot_state": self.state.robot_state,
            "surgeon_intent": self.state.surgeon_intent,
            "surgeon_request_tool": self.state.surgeon_request_tool,
            "surgeon_ready_for_handover": self.state.surgeon_ready_for_handover,
            "surgeon_ready_for_retrieval": self.state.surgeon_ready_for_retrieval,
            "cleaner_busy": self.state.cleaner_busy,
            "cleaner_remaining_sec": self.state.cleaner_remaining_sec,
            "pending_transition_tools": list(self.state.pending_transition_tools),
            "active_recovery_tools": list(self.state.active_recovery_tools),
            "right_hand_tool": self.state.right_hand_tool,
            "left_hand_tool": self.state.left_hand_tool,
            "prepositioned_tool": self.state.prepositioned_tool,
            "active_robot_task": asdict(self.state.active_robot_task) if self.state.active_robot_task else {},
            "recent_events": list(self.state.recent_event_types),
            "instrument_states": self.instrument_payload(),
        }
