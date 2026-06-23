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
    min_duration_sec: float = 0.0


@dataclass(slots=True)
class InstrumentSpec:
    id: str
    display_name: str
    display_name_ko: str
    aliases: list[str]
    category: str
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
class PhaseGuardPolicy:
    min_confidence_to_keep: float
    min_confidence_to_switch: float
    smoothing_window: int
    min_dwell_time_sec: float
    allow_unknown_phase: bool


@dataclass(slots=True)
class ActionGuardPolicy:
    block_handover_when_phase_uncertain: bool
    require_multi_evidence_for_handover: bool
    allow_prepositioning_when_uncertain: bool
    explicit_request_priority: bool


@dataclass(slots=True)
class HumanoidPolicy:
    handover_arm: str
    recovery_arm: str
    require_cleaning_after_surgeon_use: bool
    allow_anticipatory_hold: bool
    voice_override_preempts_preposition: bool
    direct_return_to_rack_for_unused_prepositioned_tool: bool


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
class MockSurgeonGesture:
    event_type: str
    requested_tool: str = ""
    hand_pose: str = ""
    confidence: float = 0.0
    note: str = ""


@dataclass(slots=True)
class MockPerceptionStage:
    name: str
    duration_ticks: int
    phase_hypotheses: list[MockPhaseHypothesis] = field(default_factory=list)
    observations: list[MockObservation] = field(default_factory=list)
    surgeon_gesture: MockSurgeonGesture | None = None
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
    normal_phase_ids: list[str] = field(default_factory=list)
    interrupt_phase_ids: list[str] = field(default_factory=list)
    phases: list[PhaseSpec] = field(default_factory=list)
    instruments: list[InstrumentSpec] = field(default_factory=list)
    display_catalog: dict[str, dict] = field(default_factory=dict)
    locations: list[SceneLocation] = field(default_factory=list)
    initial_placements: list[InitialPlacement] = field(default_factory=list)
    phase_guard: PhaseGuardPolicy | None = None
    action_guard: ActionGuardPolicy | None = None
    humanoid_policy: HumanoidPolicy | None = None
    simulation_entities: list[SimulationEntity] = field(default_factory=list)
    simulation_anchors: list[SimulationAnchor] = field(default_factory=list)
    mock_perception: MockPerceptionScenario | None = None
    mock_surgeon: MockSurgeonScenario | None = None
