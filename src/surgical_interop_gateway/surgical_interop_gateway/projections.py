"""Pure, deliberately narrow projections from internal Taskplanner state.

The gateway is an information boundary.  These helpers are intentionally free of
ROS dependencies so that the allowed public fields can be tested independently
of a running graph.  Do not add raw model payloads, planner rationale, or
predictions here without an explicit public-contract review.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import Any, Iterable


DT_ACCEPTED = "DT_ACCEPTED"
MODEL_OBSERVED = "MODEL_OBSERVED"
UNKNOWN = "UNKNOWN"
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


def finite_probability(value: Any) -> float | None:
    """Return a public probability only when it is finite and in [0, 1]."""

    if isinstance(value, bool):
        return None
    try:
        probability = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
        return None
    return probability


def finite_nonnegative(value: Any) -> float | None:
    """Return a finite non-negative public duration/value, else ``None``."""

    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < 0.0:
        return None
    return number


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

    phase_confidence = finite_probability(_value(world, "phase_confidence", 0.0))
    confidence_valid = phase_confidence is not None

    return ContextProjection(
        stamp=_value(world, "stamp", None),
        procedure_type=str(_value(world, "procedure_id", "")),
        procedure_active=bool(_value(world, "running", False)),
        current_phase=str(_value(world, "filtered_phase", "")),
        phase_confidence=phase_confidence if confidence_valid else 0.0,
        phase_uncertain=bool(_value(world, "phase_uncertain", True)) or not confidence_valid,
        execution_state=str(_value(world, "execution_state", "")),
        safety_flags=_strings(_value(world, "safety_flags", ())),
        evidence_status=DT_ACCEPTED if confidence_valid else UNKNOWN,
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

    confidence = finite_probability(_value(instrument, "confidence", 0.0))
    confidence_valid = confidence is not None

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
        confidence=confidence if confidence_valid else 0.0,
        evidence_status=DT_ACCEPTED if confidence_valid else UNKNOWN,
    )


def project_instruments(world: Any) -> tuple[InstrumentProjection, ...]:
    return tuple(project_instrument(item) for item in _value(world, "instrument_states", ()))


@dataclass(frozen=True)
class ToolPredictionProjection:
    """One reviewed current-tool forecast for display, never an instruction."""

    stamp: Any
    rank: int
    instrument_id: str
    instance_id: str
    confidence: float
    stability_sec: float
    source: str
    evidence_status: str = DT_ACCEPTED


def project_tool_predictions(world: Any) -> tuple[ToolPredictionProjection, ...]:
    """Project the reducer-accepted top forecast as a ranked public snapshot."""

    instrument_id = str(_value(world, "predicted_tool", "")).strip()
    if not instrument_id:
        return ()
    confidence = finite_probability(_value(world, "predicted_tool_confidence", 0.0))
    stability_sec = finite_nonnegative(
        _value(world, "predicted_tool_stability_sec", 0.0)
    )
    # A ranked prediction is meaningful only as an intact tuple.  Do not clamp
    # malformed model evidence into an apparently valid public forecast.
    if confidence is None or stability_sec is None:
        return ()
    return (
        ToolPredictionProjection(
            stamp=_value(world, "stamp", None),
            rank=1,
            instrument_id=instrument_id,
            # WorldState currently does not bind the forecast to an instance.
            instance_id="",
            confidence=confidence,
            stability_sec=stability_sec,
            source="digital_twin",
        ),
    )


@dataclass(frozen=True)
class RobotEndEffectorProjection:
    stamp: Any
    robot_id: str
    end_effector_id: str
    state: str
    instrument_id: str
    instance_id: str
    confidence: float
    evidence_status: str = DT_ACCEPTED


def project_robot_end_effectors(
    world: Any,
) -> tuple[RobotEndEffectorProjection, ...]:
    """Expose the two reducer-authoritative humanoid hand assignments.

    Both hands are present while a procedure is active so a UI can distinguish
    a known empty hand from a missing/stale snapshot.  The node suppresses the
    entire array when the procedure itself is inactive or unavailable.
    """

    projections: list[RobotEndEffectorProjection] = []
    for hand in ("right", "left"):
        instrument_id = str(_value(world, f"{hand}_hand_tool", "")).strip()
        instance_id = str(
            _value(world, f"{hand}_hand_tool_instance_id", "")
        ).strip()
        projections.append(
            RobotEndEffectorProjection(
                stamp=_value(world, "stamp", None),
                robot_id="humanoid",
                end_effector_id=f"{hand}_hand",
                state="HOLDING" if instrument_id or instance_id else "EMPTY",
                instrument_id=instrument_id,
                instance_id=instance_id,
                confidence=1.0,
            )
        )
    return tuple(projections)


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
    progress = finite_probability(_value(status, "progress", 0.0))
    return RobotProjection(
        stamp=_value(status, "stamp", None),
        robot_id="humanoid",
        robot_type="humanoid",
        connection_state="offline" if execution_state == "offline" else "unknown",
        execution_state=execution_state,
        active_command_id=str(_value(status, "command_id", "")),
        progress=progress if progress is not None else 0.0,
        reason_code="skill_execution_failed" if failed else "",
        evidence_status=DT_ACCEPTED if progress is not None else UNKNOWN,
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
    """Project a DT event without confusing acceptance with success.

    ``DT_ACCEPTED`` means that the reducer accepted the *event fact* as public
    evidence.  It does not mean that the operation described by the event was
    successful.  Older internal events often leave ``status`` empty, so derive
    an explicit outcome from the reviewed event name in that case.  Only the
    command/task identifier is selected from ``detail_json``; the remainder of
    the private payload stays behind the gateway boundary.
    """

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

    event_type = str(_value(event, "event_type", ""))
    state = str(_value(event, "status", "")).strip()
    if not state:
        normalized_event = event_type.casefold()
        for suffix, outcome in (
            ("rejected", "rejected"),
            ("failed", "failed"),
            ("ignored", "ignored"),
            ("cancelled", "cancelled"),
            ("canceled", "cancelled"),
            ("accepted", "accepted"),
            ("completed", "completed"),
            ("started", "started"),
            ("detected", "detected"),
            ("observed", "observed"),
            ("updated", "updated"),
        ):
            if normalized_event.endswith(suffix):
                state = outcome
                break

    correlation_id = ""
    raw_detail = _value(event, "detail_json", "")
    if raw_detail:
        try:
            detail = json.loads(str(raw_detail))
        except (TypeError, ValueError, json.JSONDecodeError):
            detail = None
        if isinstance(detail, dict):
            correlation_id = str(
                detail.get("command_id") or detail.get("task_id") or ""
            ).strip()

    confidence = finite_probability(_value(event, "confidence", 0.0))
    return EventProjection(
        stamp=_value(event, "stamp", None),
        event_type=event_type,
        subject_type=subject_type,
        subject_id=subject_id,
        phase=phase_id,
        location_type=str(_value(event, "location_type", "")),
        location_id=str(_value(event, "location_id", "")),
        state=state,
        correlation_id=correlation_id,
        confidence=confidence if confidence is not None else 0.0,
        evidence_status=DT_ACCEPTED if confidence is not None else UNKNOWN,
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


def _items(values: Iterable[Any]) -> tuple[Any, ...]:
    try:
        return tuple(values)
    except TypeError:
        return ()


def project_clinical_observation(result: Any) -> ClinicalObservationProjection:
    """Project structured VLM observations while omitting raw_json and predictions."""

    malformed = False

    phase_ids = _strings(_items(_value(result, "phase_ids", ())))
    raw_phase_confidences = _items(_value(result, "phase_confidences", ()))
    if len(phase_ids) != len(raw_phase_confidences):
        phase_ids = ()
        phase_confidences: tuple[float, ...] = ()
        malformed = True
    else:
        valid_phase_rows: list[tuple[str, float]] = []
        for phase_id, raw_confidence in zip(phase_ids, raw_phase_confidences):
            confidence = finite_probability(raw_confidence)
            if confidence is None:
                malformed = True
                continue
            valid_phase_rows.append((phase_id, confidence))
        phase_ids = tuple(row[0] for row in valid_phase_rows)
        phase_confidences = tuple(row[1] for row in valid_phase_rows)

    observed_tool_ids = _strings(_items(_value(result, "observed_tool_ids", ())))
    observed_location_ids = _strings(
        _items(_value(result, "observed_location_ids", ()))
    )
    observed_location_types = _strings(
        _items(_value(result, "observed_location_types", ()))
    )
    raw_observed_confidences = _items(
        _value(result, "observed_confidences", ())
    )
    observed_lengths = {
        len(observed_tool_ids),
        len(observed_location_ids),
        len(observed_location_types),
        len(raw_observed_confidences),
    }
    if len(observed_lengths) != 1:
        observed_tool_ids = ()
        observed_location_ids = ()
        observed_location_types = ()
        observed_confidences: tuple[float, ...] = ()
        malformed = True
    else:
        valid_observed_rows: list[tuple[str, str, str, float]] = []
        for tool_id, location_id, location_type, raw_confidence in zip(
            observed_tool_ids,
            observed_location_ids,
            observed_location_types,
            raw_observed_confidences,
        ):
            confidence = finite_probability(raw_confidence)
            if confidence is None:
                malformed = True
                continue
            valid_observed_rows.append(
                (tool_id, location_id, location_type, confidence)
            )
        observed_tool_ids = tuple(row[0] for row in valid_observed_rows)
        observed_location_ids = tuple(row[1] for row in valid_observed_rows)
        observed_location_types = tuple(row[2] for row in valid_observed_rows)
        observed_confidences = tuple(row[3] for row in valid_observed_rows)

    gesture_event_type = str(_value(result, "gesture_event_type", ""))
    gesture_requested_tool = str(_value(result, "gesture_requested_tool", ""))
    gesture_hand_pose = str(_value(result, "gesture_hand_pose", ""))
    gesture_confidence = finite_probability(
        _value(result, "gesture_confidence", 0.0)
    )
    if gesture_confidence is None:
        # Gesture fields form one claim. Clearing all of them avoids publishing
        # a categorical gesture paired with fabricated numeric certainty.
        gesture_event_type = ""
        gesture_requested_tool = ""
        gesture_hand_pose = ""
        gesture_confidence = 0.0
        malformed = True

    uncertainty = finite_probability(_value(result, "uncertainty", 1.0))
    if uncertainty is None:
        # Maximum uncertainty is the conservative scalar fallback.
        uncertainty = 1.0
        malformed = True

    return ClinicalObservationProjection(
        stamp=_value(result, "stamp", None),
        source=str(_value(result, "source", "")),
        summary=str(_value(result, "summary", "")),
        phase_ids=phase_ids,
        phase_confidences=phase_confidences,
        observed_tool_ids=observed_tool_ids,
        observed_location_ids=observed_location_ids,
        observed_location_types=observed_location_types,
        observed_confidences=observed_confidences,
        gesture_event_type=gesture_event_type,
        gesture_requested_tool=gesture_requested_tool,
        gesture_hand_pose=gesture_hand_pose,
        gesture_confidence=gesture_confidence,
        uncertainty=uncertainty,
        evidence_status=UNKNOWN if malformed else MODEL_OBSERVED,
    )
