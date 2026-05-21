"""Internal dataclasses for the digital twin."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

LIFECYCLE_HOME_RACK = "home_rack"
LIFECYCLE_PREPOSITIONED_RIGHT = "prepositioned_right"
LIFECYCLE_SURGEON_OWNED = "surgeon_owned"
LIFECYCLE_MAYO_REUSE = "mayo_reuse"
LIFECYCLE_MAYO_RECOVERY = "mayo_recovery"
LIFECYCLE_RECOVERING_LEFT = "recovering_left"
LIFECYCLE_CLEANING_LEFT = "cleaning_left"
LIFECYCLE_CLEANED_LEFT = "cleaned_left"
LIFECYCLE_RETURNED_HOME = "returned_home"


@dataclass(slots=True)
class InstrumentBelief:
    instrument_id: str
    home_location_type: str
    home_location_id: str
    location_type: str
    location_id: str
    owner: str
    status: str
    confidence: float
    cleanliness_state: str = "sterile"
    contaminated: bool = False
    reserved_for: str = ""
    last_holder: str = "none"
    lifecycle_stage: str = LIFECYCLE_HOME_RACK
    next_required_transition: str = ""
    visual_anchor_id: str = ""
    ever_surgeon_owned: bool = False
    last_update_sec: float = 0.0


@dataclass(slots=True)
class ActiveRobotTask:
    task_id: str = ""
    task_type: str = ""
    instrument_id: str = ""
    arm: str = ""
    source_anchor_id: str = ""
    target_anchor_id: str = ""
    started_at_sec: float = 0.0
    duration_sec: float = 0.0
    progress: float = 0.0
    remaining_sec: float = 0.0


@dataclass(slots=True)
class SurgeonRequestCue:
    event_type: str
    instrument_id: str
    voice_text: str = ""
    note: str = ""
    ready_for_handover: bool = True
    ready_for_retrieval: bool = False
    override: bool = False


@dataclass(slots=True)
class TwinState:
    procedure_id: str
    filtered_phase: str
    phase_confidence: float
    phase_uncertain: bool
    phase_stability: float
    explicit_request_tool: str = ""
    robot_state: str = "idle"
    safety_flags: list[str] = field(default_factory=list)
    recent_event_types: deque[str] = field(default_factory=lambda: deque(maxlen=20))
    right_hand_tool: str = ""
    left_hand_tool: str = ""
    prepositioned_tool: str = ""
    surgeon_intent: str = ""
    surgeon_request_tool: str = ""
    surgeon_ready_for_handover: bool = False
    surgeon_ready_for_retrieval: bool = False
    surgeon_request_queue: deque[SurgeonRequestCue] = field(default_factory=deque)
    cleaner_busy: bool = False
    cleaner_remaining_sec: float = 0.0
    pending_transition_tools: list[str] = field(default_factory=list)
    active_recovery_tools: list[str] = field(default_factory=list)
    running: bool = False
    execution_state: str = "idle"
    active_robot_task: ActiveRobotTask | None = None
