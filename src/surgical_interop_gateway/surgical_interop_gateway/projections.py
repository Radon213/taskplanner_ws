"""Pure, deliberately narrow projections from internal Taskplanner state.

The gateway is an information boundary.  These helpers are intentionally free of
ROS dependencies so that the allowed public fields can be tested independently
of a running graph.  Do not add raw model payloads, planner rationale, or
predictions here without an explicit public-contract review.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


DT_ACCEPTED = "DT_ACCEPTED"
MODEL_OBSERVED = "MODEL_OBSERVED"
_SKILL_FAILURE_STATES = {
    "dispatch_failed",
    "failed",
    "rejected",
    "result_failed",
    "server_unavailable",
}


def _value(message: Any, name: str, default: Any = "") -> Any:
    """Return a ROS/message-like field without requiring ROS in unit tests."""

    return getattr(message, name, default)


def stamp_to_seconds(stamp: Any) -> float:
    """Convert a ROS Time-like object to seconds, accepting a missing stamp."""

    if stamp is None:
        return 0.0
    return float(_value(stamp, "sec", 0)) + float(_value(stamp, "nanosec", 0)) / 1_000_000_000.0


@dataclass(frozen=True)
class Freshness:
    """Freshness based on local receipt time, not untrusted source clock time."""

    available: bool
    fresh: bool
    age_sec: float


def freshness_from_receipt(
    received_monotonic_sec: float | None,
    now_monotonic_sec: float,
    stale_after_sec: float,
) -> Freshness:
    """Return stable freshness semantics for a source cached by the gateway."""

    if received_monotonic_sec is None:
        return Freshness(available=False, fresh=False, age_sec=-1.0)
    age_sec = max(0.0, now_monotonic_sec - received_monotonic_sec)
    return Freshness(
        available=True,
        fresh=age_sec <= max(0.0, stale_after_sec),
        age_sec=age_sec,
    )


@dataclass(frozen=True)
class ContextProjection:
    stamp: Any
    procedure_type: str
    procedure_active: bool
    current_phase: str
    phase_confidence: float
    phase_uncertain: bool
    execution_state: str
    safety_flags: tuple[str, ...]
    evidence_status: str = DT_ACCEPTED


def project_context(world: Any) -> ContextProjection:
    """Project only reducer-accepted surgery context, never planner predictions."""

    return ContextProjection(
        stamp=_value(world, "stamp", None),
        procedure_type=str(_value(world, "procedure_id", "")),
        procedure_active=bool(_value(world, "running", False)),
        current_phase=str(_value(world, "filtered_phase", "")),
        phase_confidence=float(_value(world, "phase_confidence", 0.0)),
        phase_uncertain=bool(_value(world, "phase_uncertain", True)),
        execution_state=str(_value(world, "execution_state", "")),
        safety_flags=_strings(_value(world, "safety_flags", ())),
    )


@dataclass(frozen=True)
class InstrumentProjection:
    stamp: Any
    instrument_id: str
    instance_id: str
    location_type: str
    location_id: str
    holder_role: str
    state: str
    visible: bool
    confidence: float
    evidence_status: str = DT_ACCEPTED


def project_instrument(instrument: Any) -> InstrumentProjection:
    """Project semantic tool location only; this is not a Cartesian pose."""

    return InstrumentProjection(
        stamp=_value(instrument, "stamp", None),
        instrument_id=str(_value(instrument, "instrument_id", "")),
        instance_id=str(_value(instrument, "instance_id", "")),
        location_type=str(_value(instrument, "location_type", "")),
        location_id=str(_value(instrument, "location_id", "")),
        holder_role=str(_value(instrument, "owner", "")),
        state=str(_value(instrument, "status", "")),
        # `visual_anchor_id` is a semantic/display anchor, not a camera
        # observation.  Never turn it into a visibility assertion.
        visible=False,
        confidence=float(_value(instrument, "confidence", 0.0)),
    )


def project_instruments(world: Any) -> tuple[InstrumentProjection, ...]:
    return tuple(project_instrument(item) for item in _value(world, "instrument_states", ()))


@dataclass(frozen=True)
class RobotProjection:
    stamp: Any
    robot_id: str
    robot_type: str
    connection_state: str
    execution_state: str
    active_command_id: str
    progress: float
    reason_code: str
    evidence_status: str = DT_ACCEPTED


def project_skill_robot_status(status: Any) -> RobotProjection:
    """Expose taskplanner's skill-execution status as one humanoid robot state.

    `SkillStatus` has no trusted connection boolean.  The gateway therefore
    distinguishes only a reported offline state; all other operational states
    remain ``unknown`` rather than inventing connectivity.
    """

    execution_state = str(_value(status, "state", ""))
    failed = execution_state in _SKILL_FAILURE_STATES or (
        not bool(_value(status, "success", True))
        and execution_state not in {"cancel_requested", "skipped_while_busy"}
    )
    return RobotProjection(
        stamp=_value(status, "stamp", None),
        robot_id="humanoid",
        robot_type="humanoid",
        connection_state="offline" if execution_state == "offline" else "unknown",
        execution_state=execution_state,
        active_command_id=str(_value(status, "command_id", "")),
        progress=float(_value(status, "progress", 0.0)),
        reason_code="skill_execution_failed" if failed else "",
    )


def project_bed_robot_arm_state(status: Any, stamp: Any = None) -> RobotProjection:
    """Project one controller-owned retraction-arm state."""

    execution_state = str(_value(status, "state", ""))
    return RobotProjection(
        stamp=stamp,
        robot_id=str(_value(status, "arm_id", "")),
        robot_type="bed_retraction_arm",
        # The controller document exposes no connectivity field. Receipt
        # availability and freshness are projected through SurgeryHealth.
        connection_state="unknown",
        execution_state=execution_state,
        active_command_id="",
        progress=0.0,
        reason_code=str(_value(status, "reason_code", "")),
    )


@dataclass(frozen=True)
class EventProjection:
    stamp: Any
    event_type: str
    subject_type: str
    subject_id: str
    phase: str
    location_type: str
    location_id: str
    state: str
    correlation_id: str
    confidence: float
    evidence_status: str = DT_ACCEPTED


def project_event(event: Any) -> EventProjection:
    """Project a DT event while excluding detail_json and planner-only intent."""

    instance_id = str(_value(event, "instance_id", ""))
    instrument_id = str(_value(event, "instrument_id", ""))
    phase_id = str(_value(event, "phase_id", ""))
    if instance_id or instrument_id:
        subject_type = "instrument"
        subject_id = instance_id or instrument_id
    elif phase_id:
        subject_type = "procedure"
        subject_id = phase_id
    else:
        subject_type = "system"
        subject_id = ""

    return EventProjection(
        stamp=_value(event, "stamp", None),
        event_type=str(_value(event, "event_type", "")),
        subject_type=subject_type,
        subject_id=subject_id,
        phase=phase_id,
        location_type=str(_value(event, "location_type", "")),
        location_id=str(_value(event, "location_id", "")),
        state=str(_value(event, "status", "")),
        correlation_id="",
        confidence=float(_value(event, "confidence", 0.0)),
    )


@dataclass(frozen=True)
class ClinicalObservationProjection:
    stamp: Any
    source: str
    summary: str
    phase_ids: tuple[str, ...]
    phase_confidences: tuple[float, ...]
    observed_tool_ids: tuple[str, ...]
    observed_location_ids: tuple[str, ...]
    observed_location_types: tuple[str, ...]
    observed_confidences: tuple[float, ...]
    gesture_event_type: str
    gesture_requested_tool: str
    gesture_hand_pose: str
    gesture_confidence: float
    uncertainty: float
    evidence_status: str = MODEL_OBSERVED


def _strings(values: Iterable[Any]) -> tuple[str, ...]:
    return tuple(str(value) for value in values)


def _floats(values: Iterable[Any]) -> tuple[float, ...]:
    return tuple(float(value) for value in values)


def project_clinical_observation(result: Any) -> ClinicalObservationProjection:
    """Project structured VLM observations while omitting raw_json and predictions."""

    return ClinicalObservationProjection(
        stamp=_value(result, "stamp", None),
        source=str(_value(result, "source", "")),
        summary=str(_value(result, "summary", "")),
        phase_ids=_strings(_value(result, "phase_ids", ())),
        phase_confidences=_floats(_value(result, "phase_confidences", ())),
        observed_tool_ids=_strings(_value(result, "observed_tool_ids", ())),
        observed_location_ids=_strings(_value(result, "observed_location_ids", ())),
        observed_location_types=_strings(_value(result, "observed_location_types", ())),
        observed_confidences=_floats(_value(result, "observed_confidences", ())),
        gesture_event_type=str(_value(result, "gesture_event_type", "")),
        gesture_requested_tool=str(_value(result, "gesture_requested_tool", "")),
        gesture_hand_pose=str(_value(result, "gesture_hand_pose", "")),
        gesture_confidence=float(_value(result, "gesture_confidence", 0.0)),
        uncertainty=float(_value(result, "uncertainty", 1.0)),
    )
