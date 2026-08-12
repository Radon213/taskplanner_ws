"""Internal dataclasses for the digital twin."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

LIFECYCLE_HOME_RACK = "home_rack"
LIFECYCLE_PREPOSITIONED_RIGHT = "prepositioned_right"
LIFECYCLE_SURGEON_OWNED = "surgeon_owned"
LIFECYCLE_MAYO_REUSE = "mayo_reuse"
LIFECYCLE_MAYO_RECOVERY = "mayo_recovery"
LIFECYCLE_DROPPED_FLOOR = "dropped_floor"
LIFECYCLE_RECOVERING_LEFT = "recovering_left"
LIFECYCLE_CLEANING_LEFT = "cleaning_left"
LIFECYCLE_CLEANED_LEFT = "cleaned_left"
LIFECYCLE_RETURNED_HOME = "returned_home"


@dataclass(slots=True)
class InstrumentBelief:
    instrument_id: str
    instance_id: str
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
    preposition_origin_location_type: str = ""
    preposition_origin_location_id: str = ""
    preposition_origin_lifecycle_stage: str = ""
    ever_surgeon_owned: bool = False
    last_update_sec: float = 0.0
    mayo_placement_evidence: str = ""
    mayo_reuse_confidence: float = 0.0
    mayo_reuse_stability_sec: float = 0.0
    mayo_recovery_confidence: float = 0.0
    mayo_recovery_stability_sec: float = 0.0
    mayo_evidence_source: str = ""


@dataclass(slots=True)
class ActiveRobotTask:
    task_id: str = ""
    task_type: str = ""
    instrument_id: str = ""
    instrument_instance_id: str = ""
    arm: str = ""
    source_anchor_id: str = ""
    target_anchor_id: str = ""
    started_at_sec: float = 0.0
    duration_sec: float = 0.0
    progress: float = 0.0
    remaining_sec: float = 0.0


@dataclass(slots=True)
class BedRobotArmGroupBelief:
    """Planner belief for the retraction interface compatibility lane.

    Detailed motion state remains controller-owned. The target fields preserve
    the reviewed public contract without letting the twin invent arm geometry.
    """

    group_id: str
    connected: bool = False
    state: str = "unknown"
    operation: str = ""
    arm_id: str = ""
    target_tool_id: str = ""
    adjustment_mode: str = ""
    target_retractor_id: str = ""
    direction_frame: str = ""
    direction: str = ""
    axis: str = ""
    distance_mm: float = 0.0
    distance_origin: str = ""
    raw_distance_text: str = ""
    end_effector_profile: str = ""
    active_request_id: str = ""
    active_command_id: str = ""
    progress: float = 0.0
    error_code: str = ""
    error_message: str = ""
    rejection_reason: str = ""
    # Source timestamp of the last controller-owned arm-state snapshot. World
    # snapshots must not make an unchanged aggregate look newly observed.
    last_update_stamp_sec: int = 0
    last_update_stamp_nanosec: int = 0
    # Internal command/result ordering is separate from controller-owned arm
    # state time. A service result must not make physical status look newer.
    last_operation_stamp_sec: int = 0
    last_operation_stamp_nanosec: int = 0


@dataclass(slots=True)
class SurgeonRequestCue:
    event_type: str
    instrument_id: str
    instance_id: str = ""
    generation: int = 0
    voice_text: str = ""
    note: str = ""
    ready_for_handover: bool = True
    ready_for_retrieval: bool = False
    override: bool = False
    shadow_additional_instance_assumed: bool = False


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
    predicted_tool: str = ""
    predicted_tool_confidence: float = 0.0
    predicted_tool_stability_sec: float = 0.0
    surgeon_intent: str = ""
    surgeon_request_tool: str = ""
    surgeon_request_instance_id: str = ""
    surgeon_request_generation: int = 0
    surgeon_request_additional_instance_assumed: bool = False
    surgeon_ready_for_handover: bool = False
    surgeon_ready_for_retrieval: bool = False
    implicit_request_visible: bool = False
    implicit_request_tool: str = ""
    implicit_request_hand_pose: str = ""
    implicit_request_confidence: float = 0.0
    implicit_request_stability_sec: float = 0.0
    implicit_request_generation: int = 0
    surgeon_request_queue: deque[SurgeonRequestCue] = field(default_factory=deque)
    cleaner_busy: bool = False
    cleaner_remaining_sec: float = 0.0
    pending_transition_tools: list[str] = field(default_factory=list)
    active_recovery_tools: list[str] = field(default_factory=list)
    active_recovery_tool_instances: list[str] = field(default_factory=list)
    running: bool = False
    execution_state: str = "idle"
    active_robot_task: ActiveRobotTask | None = None
    bed_robot_arm_groups: dict[str, BedRobotArmGroupBelief] = field(default_factory=dict)
    right_hand_tool_instance_id: str = ""
    left_hand_tool_instance_id: str = ""
    prepositioned_tool_instance_id: str = ""
