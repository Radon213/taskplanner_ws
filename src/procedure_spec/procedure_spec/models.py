"""Dataclasses for the surgical procedure bundle."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class PhaseSpec:
    id: str
    display_name: str
    display_name_ko: str
    possible_next: list[str]
    expected_instruments: list[str]
    field_deployed_instruments: list[str] = field(default_factory=list)
    min_duration_sec: float = 0.0


@dataclass(slots=True)
class InstrumentSpec:
    id: str
    display_name: str
    display_name_ko: str
    aliases: list[str]
    category: str
    inventory_count: int = 1
    requestable: bool = True
    role: str = ""
    handover_profile: str = ""


@dataclass(slots=True)
class SceneLocation:
    id: str
    type: str


@dataclass(slots=True)
class InitialPlacement:
    instrument_id: str
    location_id: str


@dataclass(slots=True)
class InitialInstrumentState:
    instrument_id: str
    instance_id: str
    location_id: str
    lifecycle_stage: str
    confidence: float = 1.0


@dataclass(slots=True)
class PhaseGuardPolicy:
    min_confidence_to_keep: float
    min_confidence_to_switch: float
    smoothing_window: int
    min_dwell_time_sec: float
    allow_unknown_phase: bool
    min_evidence_duration_sec: float = 1.0


@dataclass(slots=True)
class ActionGuardPolicy:
    """Admission evidence that must remain fail-closed.

    Scenario choices used to live beside this field. New code must query
    :class:`ScenarioPolicy` through ``ProcedureSpec.get_scenario_policy()``.
    """

    require_multi_evidence_for_handover: bool


@dataclass(slots=True)
class HumanoidPolicy:
    """Legacy compatibility view of the authored scenario behavior."""

    handover_arm: str
    recovery_arm: str
    require_cleaning_after_surgeon_use: bool
    allow_anticipatory_hold: bool
    voice_override_preempts_preposition: bool
    return_unused_preposition_to_mayo: bool


@dataclass(frozen=True, slots=True)
class ScenarioRuntimeRequirements:
    """Scenario-selected runtime capabilities, never motion authorization.

    These values describe which runtime lanes the authored procedure wants and
    whether Taskplanner should enforce its local retraction workflow ordering.
    Controller admission, endpoint validation, stopped-route changes, preflight
    ACKs, freshness, provenance, and idempotency remain independent fail-closed
    checks in their owning runtime components.
    """

    procedure_type: str = ""
    tool_handover_action_required: bool = True
    retraction_service_required: bool = False
    bed_robot_status_required: bool = False
    image_vlm_enabled: bool = True
    dialogue_vlm_enabled: bool = False
    perception_enabled: bool = True
    rfdetr_tool_observations_required: bool = False
    voice_intent_resolver_enabled: bool = False
    surgeon_actor_enabled: bool = True
    phase_inference_enabled: bool = True
    retraction_workflow_state_enforced: bool = True
    legacy_raw_retractor_voice_enabled: bool = False

    @property
    def bed_robot_contract_enabled(self) -> bool:
        return bool(self.procedure_type and self.retraction_service_required)


@dataclass(frozen=True, slots=True)
class ScenarioPolicySpec:
    """Authored scenario choices, kept separate from safety interlocks.

    These values describe what a procedure demonstration chooses to do.  They
    do not authorize motion and must never replace controller, Service
    admission, stopped-state, freshness, idempotency, or preflight checks.
    """

    handover_arm: str = "right"
    recovery_arm: str = "left"
    require_cleaning_after_surgeon_use: bool = True
    allow_anticipatory_hold: bool = True
    voice_override_preempts_preposition: bool = True
    allow_prepositioning_when_uncertain: bool = False
    explicit_request_priority: bool = True
    unused_preposition_destination: str = "mayo"
    runtime_requirements: ScenarioRuntimeRequirements | None = None


@dataclass(slots=True)
class BedRobotArmGroupSpec:
    """Compatibility model for the single retraction controller lane."""

    id: str
    enabled: bool
    initial_end_effector_profile: str
    allowed_operations: list[str] = field(default_factory=list)
    allowed_voice_commands: list[str] = field(default_factory=list)
    voice_command_policy_configured: bool = False


@dataclass(slots=True)
class BedRobotArmGroupCueSpec:
    id: str
    phase_id: str
    group_id: str
    operation: str
    utterances: list[str] = field(default_factory=list)
    adjustment_mode: str = ""
    target_retractor_id: str = ""
    direction_frame: str = ""
    directions: list[str] = field(default_factory=list)
    default_distance_mm: float = 0.0
    end_effector_profile: str = ""
    feedback_text: str = ""


@dataclass(slots=True)
class BedRobotArmEndEffectorTransitionSpec:
    id: str
    phase_id: str
    group_id: str
    from_profile: str
    to_profile: str
    arm_id: str = ""
    target_tool_id: str = ""
    utterances: list[str] = field(default_factory=list)
    feedback_text: str = ""


@dataclass(slots=True)
class BedRobotArmProcedureSpec:
    """Procedure policy for retraction adjustment and tool-profile changes."""

    directions: list[str] = field(default_factory=list)
    distance_precedence: list[str] = field(default_factory=list)
    max_distance_mm: float = 30.0
    cm_to_mm_multiplier: float = 10.0
    require_explicit_unit: bool = True
    clamp_explicit_values: bool = False
    groups: list[BedRobotArmGroupSpec] = field(default_factory=list)
    cues: list[BedRobotArmGroupCueSpec] = field(default_factory=list)
    end_effector_transitions: list[BedRobotArmEndEffectorTransitionSpec] = field(default_factory=list)


@dataclass(slots=True)
class SimulationEntity:
    id: str
    type: str
    x: float
    y: float
    width: float = 0.0
    height: float = 0.0
    label: str = ""


@dataclass(slots=True)
class SimulationAnchor:
    id: str
    attached_to: str
    x: float
    y: float
    label: str = ""


@dataclass(slots=True)
class MockPhaseHypothesis:
    phase_id: str
    confidence: float


@dataclass(slots=True)
class MockObservation:
    instrument_id: str
    location_id: str
    location_type: str
    confidence: float
    visible: bool = True


@dataclass(slots=True)
class MockPerceptionStage:
    name: str
    duration_ticks: int
    phase_hypotheses: list[MockPhaseHypothesis] = field(default_factory=list)
    observations: list[MockObservation] = field(default_factory=list)
    scene_summary: str = ""
    uncertainty: float = 0.0
    explicit_request: str = ""


@dataclass(slots=True)
class MockPerceptionScenario:
    period_sec: float = 1.0
    stages: list[MockPerceptionStage] = field(default_factory=list)


@dataclass(slots=True)
class MockSurgeonStage:
    name: str
    phase_id: str
    duration_ticks: int
    event_type: str
    intent: str = ""
    requested_tool: str = ""
    voice_text: str = ""
    ready_for_handover: bool = False
    ready_for_retrieval: bool = False
    scene_note: str = ""


@dataclass(slots=True)
class MockSurgeonScenario:
    period_sec: float = 1.0
    stages: list[MockSurgeonStage] = field(default_factory=list)


@dataclass(slots=True)
class ProcedureBundle:
    procedure_id: str
    procedure_display_name: str
    procedure_display_name_ko: str
    procedure_target_site: str = ""
    procedure_target_site_ko: str = ""
    procedure_approach: str = ""
    procedure_approach_ko: str = ""
    default_phase_id: str = ""
    normal_phase_ids: list[str] = field(default_factory=list)
    interrupt_phase_ids: list[str] = field(default_factory=list)
    phases: list[PhaseSpec] = field(default_factory=list)
    instruments: list[InstrumentSpec] = field(default_factory=list)
    display_catalog: dict[str, dict] = field(default_factory=dict)
    locations: list[SceneLocation] = field(default_factory=list)
    initial_placements: list[InitialPlacement] = field(default_factory=list)
    initial_instrument_states: list[InitialInstrumentState] = field(
        default_factory=list
    )
    phase_guard: PhaseGuardPolicy | None = None
    action_guard: ActionGuardPolicy | None = None
    humanoid_policy: HumanoidPolicy | None = None
    scenario_policy: ScenarioPolicySpec | None = None
    bed_robot_arm_groups: BedRobotArmProcedureSpec | None = None
    simulation_entities: list[SimulationEntity] = field(default_factory=list)
    simulation_anchors: list[SimulationAnchor] = field(default_factory=list)
    mock_perception: MockPerceptionScenario | None = None
    mock_surgeon: MockSurgeonScenario | None = None
