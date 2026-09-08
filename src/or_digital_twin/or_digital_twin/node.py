"""ROS 2 node for publishing the runtime digital twin."""

from __future__ import annotations

from collections import deque
from dataclasses import asdict
import json
import math
from pathlib import Path
import threading
import time
import uuid

from hand_keypoint_interfaces.msg import (
    HandFacingArray,
    HandGestureArray,
    HandKeypoints,
)
from procedure_spec import (
    ProcedurePriorScorer,
    ScenarioConfigSnapshot,
    compact_procedure_prompt,
    discover_prompt_bundle_dirs,
    get_default_spec_dir,
    load_bundle,
    load_frozen_handover_ngram_prior,
    load_scenario_consumer_bundle,
    parse_scenario_config,
)
import rclpy
from rcl_interfaces.msg import SetParametersResult
from rclpy.node import Node
from rclpy.parameter import Parameter
from rclpy.qos import (
    DurabilityPolicy,
    HistoryPolicy,
    QoSProfile,
    ReliabilityPolicy,
)
from std_msgs.msg import String
from surgical_interop_msgs.msg import BedRobotArmStateArray
from surgical_perception_msgs.msg import TrackedToolBeliefArray
from surgical_msgs.msg import (
    BedRobotArmGroupActionProposal,
    BedRobotArmGroupCommand,
    BedRobotArmGroupRequest,
    BedRobotArmGroupState,
    BedRobotArmGroupStatus,
    FilteredPhase,
    InstrumentState,
    InputSourceStatus,
    BTContextSnapshot,
    EventDigest,
    PerceptionScene,
    PhaseEvidence,
    PhaseTransitionCue,
    RankedToolPrediction,
    ReducerDecisionEvent,
    SimulationEvent,
    SimulationState,
    SurgeonActorEvent,
    SurgeonOutwardSignal,
    SurgeonRequest,
    ToolObservation,
    TwinEvent,
    VLMInferenceProposal,
    VLMHealth,
    VLMRequestContext,
    VLMReducerDecision,
    VLMResult,
    WorldState,
)

from .hand_handover_gate import (
    ContinuousHandHandoverGate,
    ExactStampHandTripletJoiner,
    FrameDisposition,
    HandFrameEvidence,
    HandPerceptionPins,
    classify_hand_frame,
    stamp_key,
    stamp_key_sec,
    validate_hand_health,
)
from .models import RankedToolPredictionBelief
from .ngram_policy import arbitrate_ngram_intent
from .tool_policy_status import (
    TOOL_POLICY_PARAMETER_NAMES,
    TOOL_POLICY_STATUS_TOPIC,
    tool_policy_status_payload,
)
from .twin import CAM4_TYPED_MAYO_OBSERVATION_SOURCE, ORDigitalTwin
WORLD_STATE_MAINTENANCE_PERIOD_SEC = 0.5
HAND_HANDOVER_WATCHDOG_PERIOD_SEC = 0.1
WORLD_STATE_IDLE_CHECKPOINT_SEC = 2.0
VLM_STARTUP_HEALTH_GRACE_SEC = 6.0
WORLD_STATE_KNOWN_INACTIVE = frozenset(
    {"idle", "halted", "completed", "terminated"}
)
HAND_HANDOVER_GATE_PARAMETER_BINDINGS = {
    "hand_handover_dwell_sec": "dwell_sec",
    "hand_handover_release_sec": "release_sec",
    "hand_handover_release_confirm_sec": "release_confirm_sec",
    "hand_handover_soft_unknown_grace_sec": "soft_unknown_grace_sec",
    "hand_handover_max_positive_gap_sec": "max_positive_gap_sec",
    "hand_handover_max_source_age_sec": "max_source_age_sec",
    "hand_handover_future_tolerance_sec": "future_tolerance_sec",
    "hand_handover_observation_timeout_sec": "max_receipt_silence_sec",
    "hand_handover_minimum_positive_samples": "minimum_positive_samples",
}
DIRECT_DELIVERY_TASK_TYPES = frozenset(
    {
        "direct_handover",
        "mayo_handover",
        "pick_up_and_handover",
        "pick_up_from_mayo_and_handover",
        "predicted_tool_handover",
        "put_down_and_handover",
        "replace_and_handover",
        "tool_handover",
    }
)
DIRECT_DELIVERY_TARGET_ANCHORS = frozenset(
    {"handover_zone", "surgeon", "surgeon_hand", "surgeon_receive_zone"}
)
SKILL_EVENTS_REQUIRING_CURRENT_RUN = frozenset(
    {
        "RobotTaskStarted",
        "RobotTaskCompleted",
        "RobotGraspedTool",
        "ToolPrepared",
        "ToolHandoverCompleted",
        "ShadowAdditionalToolHandoverCompleted",
        "PredictedToolReturnedToRack",
        "UnusedPrepositionReturned",
        "ToolReceivedFromSurgeon",
        "ToolRetrievedFromMayo",
        "ToolSentToCleaner",
        "ToolCleaningProgress",
        "ToolCleaningCompleted",
        "ToolReturnedToTray",
    }
)
FINISHING_SKILL_TASK_TYPES = frozenset(
    {
        "return_unused_preposition",
        "return_preposition_to_tray",
        "retrieve_from_mayo",
        "tool_retrieve",
    }
)
BELIEF_OWNED_TOOL_LOCATION_EVENTS = frozenset(
    {
        "RobotGraspedTool",
        "ToolPrepared",
        "ToolHandoverCompleted",
        "ShadowAdditionalToolHandoverCompleted",
        "ToolReceivedFromSurgeon",
        "ToolRetrievedFromMayo",
        "ToolSentToCleaner",
        "ToolCleaningProgress",
        "ToolCleaningCompleted",
        "ToolReturnedToTray",
        "PredictedToolReturnedToRack",
        "UnusedPrepositionReturned",
    }
)


class ORDigitalTwinNode(Node):
    _IMPORTANT_NORMAL_EVENTS = {
        "PhaseUpdated",
        "VoiceTranscriptObserved",
        "SurgeonRequestObserved",
        "SurgeonActorEventObserved",
        "ToolHandoverCompleted",
        "ToolReceivedFromSurgeon",
        "ToolSentToCleaner",
        "ToolCleaningCompleted",
        "ToolReturnedToTray",
        "RobotTaskStarted",
        "RobotTaskCompleted",
        "BedRobotArmGroupRequestObserved",
        "BedRobotArmGroupRequestRejected",
        "BedRobotArmGroupProposalObserved",
        "BedRobotArmGroupProposalRejected",
        "BedRobotArmGroupCommandApproved",
        "BedRobotArmGroupCommandCompleted",
        "BedRobotArmGroupCommandRejected",
        "BedRobotArmGroupCommandCancelled",
    }

    def __init__(self) -> None:
        super().__init__("or_digital_twin")
        self.declare_parameter("spec_dir", str(get_default_spec_dir()))
        self.declare_parameter(
            "scenario_config_topic", "/simulation/scenario_config"
        )
        self.declare_parameter("vlm_recent_event_count", 6)
        self.declare_parameter("validation_mode", "bt_twin")
        self.declare_parameter("phase_authority", "reducer")
        self.declare_parameter("vlm_mode", "mock")
        self.declare_parameter("vlm_health_timeout_sec", 6.0)
        self.declare_parameter("vlm_evidence_max_gap_sec", 2.5)
        # Autonomous tool policy is DT-owned and derives only from the frozen
        # 0704 handover n-gram distribution.  VLM tool/Mayo scores remain
        # observability input, never an action-policy input.
        self.declare_parameter("ngram_prepare_probability_threshold", 0.125)
        self.declare_parameter("ngram_recovery_probability_threshold", 0.391)
        self.declare_parameter(
            "ngram_recovery_enabled_tools",
            ["T02", "T08"],
        )
        self.declare_parameter("ngram_policy_stability_sec", 0.30)
        self.declare_parameter(
            "hand_gesture_topic",
            "/perception/cam_4/hand/gestures",
        )
        self.declare_parameter(
            "hand_facing_topic",
            "/perception/cam_4/hand/facing",
        )
        self.declare_parameter(
            "hand_keypoints_topic",
            "/perception/cam_4/hand/keypoints",
        )
        self.declare_parameter(
            "hand_health_topic",
            "/perception/cam_4/hand/health",
        )
        self.declare_parameter("hand_handover_dwell_sec", 0.300)
        self.declare_parameter("hand_handover_release_sec", 0.500)
        # CAM4 may miss an isolated frame while a requester is already
        # holding the exact pose.  These two short windows are intentionally
        # separate from the longer fresh-release rearm interval above.
        self.declare_parameter("hand_handover_release_confirm_sec", 0.180)
        self.declare_parameter("hand_handover_soft_unknown_grace_sec", 0.180)
        self.declare_parameter("hand_handover_max_positive_gap_sec", 0.500)
        self.declare_parameter("hand_handover_max_source_age_sec", 0.500)
        self.declare_parameter("hand_handover_future_tolerance_sec", 0.500)
        self.declare_parameter("hand_handover_health_timeout_sec", 2.0)
        self.declare_parameter("hand_handover_observation_timeout_sec", 0.500)
        self.declare_parameter("hand_handover_minimum_positive_samples", 4)
        self.declare_parameter("hand_handover_minimum_gesture_score", 0.5)
        self.declare_parameter("hand_handover_minimum_handedness_score", 0.5)
        self.declare_parameter("hand_handover_minimum_palm_up_score", 0.0)
        self.declare_parameter("hand_mapping_operator_approved", False)
        self.declare_parameter(
            "hand_expected_source_frame_id",
            "cam_4_color_optical_frame",
        )
        self.declare_parameter(
            "hand_expected_gesture_model_name",
            "VIPLab Top-View Landmark Gesture Classifier",
        )
        self.declare_parameter(
            "hand_expected_gesture_model_version",
            "landmark-geometry-v2-world-closed",
        )
        self.declare_parameter(
            "hand_expected_gesture_model_sha256",
            "258ed1676863df9317fb084a446a2ae9645c1f4acc468a4c4ad92979cf0ef821",
        )
        self.declare_parameter(
            "hand_expected_facing_estimator_name",
            "VIPLab CAM4 Depth Palm-Facing Estimator",
        )
        self.declare_parameter(
            "hand_expected_facing_estimator_version",
            "depth-palm-normal-v1",
        )
        self.declare_parameter(
            "hand_expected_facing_spec_sha256",
            "45c61773790b8510f373bd9f0940ce1189567e7f4e74ff3c0d6c5b5ee3785b37",
        )
        self.declare_parameter(
            "hand_expected_calibration_version",
            "cam4_live_aprilgrid_depth_workplane_20260822",
        )
        self.declare_parameter(
            "hand_expected_handedness_mapping_version",
            "cam4_forced_right_camera_constraint_v1_pending_live_check",
        )
        self.declare_parameter(
            "hand_expected_depth_registration_backend",
            "cuda_cabi_v1",
        )
        self.declare_parameter("accept_validation_actor_events", False)
        self.declare_parameter("accept_non_override_structured_requests", False)
        self.declare_parameter("evaluation_observation_topic", "")
        self.declare_parameter("allow_shadow_type_instance_requests", False)
        self.declare_parameter("allow_open_set_phase_bootstrap", False)
        self.declare_parameter("bed_robot_status_timeout_sec", 2.0)
        self.declare_parameter("bed_robot_source_max_age_sec", 2.0)
        self.declare_parameter("bed_robot_source_future_tolerance_sec", 0.5)
        self.declare_parameter("cam4_mayo_observation_max_age_sec", 2.0)
        self.declare_parameter(
            "cam4_mayo_observation_future_tolerance_sec",
            0.5,
        )
        self.declare_parameter(
            "tool_belief_topic",
            "/surgery/perception/tool_beliefs",
        )
        self._spec_dir = str(self.get_parameter("spec_dir").value)
        self._scenario_config_root = Path(self._spec_dir).resolve().parent
        self._scenario_config_revision = ""
        self._pending_scenario_config: ScenarioConfigSnapshot | None = None
        self._scenario_config_lock = threading.RLock()
        self._vlm_recent_event_count = max(1, int(self.get_parameter("vlm_recent_event_count").value))
        self._validation_mode = str(self.get_parameter("validation_mode").value)
        self._phase_authority = str(self.get_parameter("phase_authority").value)
        self._vlm_mode = str(self.get_parameter("vlm_mode").value)
        self._vlm_health_timeout_sec = max(0.5, float(self.get_parameter("vlm_health_timeout_sec").value))
        self._vlm_evidence_max_gap_sec = max(
            0.5,
            float(self.get_parameter("vlm_evidence_max_gap_sec").value),
        )
        self._ngram_prepare_probability_threshold = max(
            0.0,
            min(
                1.0,
                float(
                    self.get_parameter(
                        "ngram_prepare_probability_threshold"
                    ).value
                ),
            ),
        )
        self._ngram_recovery_probability_threshold = max(
            0.0,
            min(
                1.0,
                float(
                    self.get_parameter(
                        "ngram_recovery_probability_threshold"
                    ).value
                ),
            ),
        )
        self._ngram_policy_stability_sec = max(
            0.1,
            float(self.get_parameter("ngram_policy_stability_sec").value),
        )
        self._hand_gesture_topic = str(
            self.get_parameter("hand_gesture_topic").value
        ).strip()
        self._hand_facing_topic = str(
            self.get_parameter("hand_facing_topic").value
        ).strip()
        self._hand_keypoints_topic = str(
            self.get_parameter("hand_keypoints_topic").value
        ).strip()
        self._hand_health_topic = str(
            self.get_parameter("hand_health_topic").value
        ).strip()
        self._hand_health_timeout_sec = max(
            0.1,
            float(self.get_parameter("hand_handover_health_timeout_sec").value),
        )
        self._hand_observation_timeout_sec = max(
            0.1,
            float(
                self.get_parameter(
                    "hand_handover_observation_timeout_sec"
                ).value
            ),
        )
        self._hand_source_max_age_sec = max(
            0.0,
            float(
                self.get_parameter(
                    "hand_handover_max_source_age_sec"
                ).value
            ),
        )
        self._hand_source_future_tolerance_sec = max(
            0.0,
            float(
                self.get_parameter(
                    "hand_handover_future_tolerance_sec"
                ).value
            ),
        )
        self._hand_minimum_gesture_score = max(
            0.0,
            min(
                1.0,
                float(
                    self.get_parameter(
                        "hand_handover_minimum_gesture_score"
                    ).value
                ),
            ),
        )
        self._hand_minimum_handedness_score = max(
            0.0,
            min(
                1.0,
                float(
                    self.get_parameter(
                        "hand_handover_minimum_handedness_score"
                    ).value
                ),
            ),
        )
        self._hand_minimum_palm_up_score = max(
            -1.0,
            min(
                1.0,
                float(
                    self.get_parameter(
                        "hand_handover_minimum_palm_up_score"
                    ).value
                ),
            ),
        )
        self._hand_mapping_operator_approved = bool(
            self.get_parameter("hand_mapping_operator_approved").value
        )
        self._hand_perception_pins = HandPerceptionPins(
            source_frame_id=str(
                self.get_parameter("hand_expected_source_frame_id").value
            ),
            gesture_model_name=str(
                self.get_parameter("hand_expected_gesture_model_name").value
            ),
            gesture_model_version=str(
                self.get_parameter("hand_expected_gesture_model_version").value
            ),
            gesture_model_sha256=str(
                self.get_parameter("hand_expected_gesture_model_sha256").value
            ),
            facing_estimator_name=str(
                self.get_parameter("hand_expected_facing_estimator_name").value
            ),
            facing_estimator_version=str(
                self.get_parameter("hand_expected_facing_estimator_version").value
            ),
            facing_spec_sha256=str(
                self.get_parameter("hand_expected_facing_spec_sha256").value
            ),
            calibration_version=str(
                self.get_parameter("hand_expected_calibration_version").value
            ),
            handedness_mapping_version=str(
                self.get_parameter(
                    "hand_expected_handedness_mapping_version"
                ).value
            ),
            depth_registration_backend=str(
                self.get_parameter(
                    "hand_expected_depth_registration_backend"
                ).value
            ),
        )
        self._hand_handover_gate_config = self._hand_handover_gate_config_from_parameters()
        self._hand_handover_gate = self._build_hand_handover_gate(
            self._hand_handover_gate_config
        )
        self._accept_validation_actor_events = bool(
            self.get_parameter("accept_validation_actor_events").value
        )
        self._accept_non_override_structured_requests = bool(
            self.get_parameter("accept_non_override_structured_requests").value
        )
        self._bed_robot_status_timeout_sec = max(
            0.1,
            float(self.get_parameter("bed_robot_status_timeout_sec").value),
        )
        self._bed_robot_source_max_age_sec = max(
            0.1,
            float(self.get_parameter("bed_robot_source_max_age_sec").value),
        )
        self._bed_robot_source_future_tolerance_sec = max(
            0.0,
            float(
                self.get_parameter(
                    "bed_robot_source_future_tolerance_sec"
                ).value
            ),
        )
        self._cam4_mayo_observation_max_age_sec = max(
            0.1,
            float(
                self.get_parameter(
                    "cam4_mayo_observation_max_age_sec"
                ).value
            ),
        )
        self._cam4_mayo_observation_future_tolerance_sec = max(
            0.0,
            float(
                self.get_parameter(
                    "cam4_mayo_observation_future_tolerance_sec"
                ).value
            ),
        )
        self._tool_belief_topic = str(
            self.get_parameter("tool_belief_topic").value
        ).strip()
        self._twin = ORDigitalTwin(
            load_bundle(self._spec_dir),
            allow_shadow_type_instance_requests=bool(
                self.get_parameter(
                    "allow_shadow_type_instance_requests"
                ).value
            ),
            allow_open_set_phase_bootstrap=bool(
                self.get_parameter("allow_open_set_phase_bootstrap").value
            ),
        )
        requestable_tool_ids = set(
            self._twin.spec.list_requestable_instrument_ids()
        )
        self._ngram_recovery_enabled_tools = frozenset(
            str(tool_id).strip()
            for tool_id in self.get_parameter(
                "ngram_recovery_enabled_tools"
            ).value
            if str(tool_id).strip() in requestable_tool_ids
        )
        self._stamp_all_bed_robot_arm_groups()
        self._prior_scorer = ProcedurePriorScorer(self._twin.spec, compact_procedure_prompt(self._spec_dir))
        self._handover_ngram_prior = load_frozen_handover_ngram_prior(
            self._twin.spec,
            self._spec_dir,
        )
        self._bundle_metadata_cache = self._build_bundle_metadata()
        self._important_events: deque[SimulationEvent] = deque(maxlen=self._vlm_recent_event_count)
        self._validated_tool_request_history: deque[dict] = deque(maxlen=12)
        self._completed_handover_history: deque[dict] = deque(maxlen=12)
        # The n-gram deque is intentionally short, but repeat policy means
        # "completed at least once in this phase" for the whole current run.
        self._completed_handover_tools_by_phase: dict[str, set[str]] = {}
        self._latest_outward_signal: SurgeonOutwardSignal | None = None
        self._vlm_health_by_topic: dict[str, tuple[VLMHealth, float]] = {}
        self._input_source_status_by_id: dict[str, InputSourceStatus] = {}
        self._visual_admission_by_channel: dict[
            str, tuple[int, int, float]
        ] = {}
        self._visual_runtime_epoch_floor = 0
        self._visual_runtime_source_stamp_floor_sec = 0.0
        self._cam4_mayo_source_epoch_floor_by_source: dict[str, int] = {}
        self._cam4_mayo_admission_by_channel: dict[
            str, tuple[int, int, float]
        ] = {}
        self._accepted_cam4_mayo_episodes: set[str] = set()
        self._last_lifecycle_control_signature: tuple[str, str] | None = None
        self._last_world_emit_signature: tuple | None = None
        self._last_world_emit_monotonic = 0.0
        self._vlm_evidence_blocked = False
        # A VLM produces inference health only after it receives an admitted
        # scenario request.  Keep this separate from visual-result freshness:
        # an idle model or the first request in flight is not a failed model.
        self._vlm_health_run_started_monotonic: float | None = None
        self._perception_health_seen = False
        self._perception_enabled = True
        self._ngram_preparation_stability: dict[str, dict] = {}
        self._ngram_recovery_stability: dict[str, dict] = {}
        # ``/skill/events`` has no explicit procedure-run field. Do not let a
        # delayed callback from a preceding runtime enter the fresh Twin:
        # every mutating task event must cross this current-run fence first.
        self._skill_event_runtime_epoch = 0
        self._skill_event_source_stamp_floor_ns = self._event_stamp_ns(
            self._stamp()
        )
        self._current_run_skill_task_ids: set[str] = set()
        # Fail closed until the pinned CAM4 gesture detector publishes a fresh
        # empty frame while its RGB inference health lease is valid.  The
        # hand-free state itself is leased: silence or detector-health loss
        # re-latches occupancy instead of leaving Mayo motion enabled.
        self._cam4_mayo_hand_present = True
        self._cam4_mayo_hand_source_stamp_ns: int | None = None
        self._cam4_mayo_hand_received_monotonic = 0.0
        self._twin.state.cam4_mayo_hand_present = True
        self._hand_handover_joiner = ExactStampHandTripletJoiner(max_pending=32)
        self._hand_health_payload: dict | None = None
        self._hand_health_received_monotonic = 0.0
        self._hand_health_ready = False
        self._hand_health_reason = "hand_health_missing"
        self._pending_bed_robot_arm_group_requests: dict[str, BedRobotArmGroupRequest] = {}
        self._bed_robot_status_received_monotonic = 0.0
        self._bed_robot_status_source_stamp_ns: int | None = None
        self._phase_entered_ros_sec = self._stamp_sec(self._stamp())
        self.add_on_set_parameters_callback(self._on_parameters_changed)

        self._world_pub = self.create_publisher(WorldState, "/twin/world_state", 20)
        self._tool_pub = self.create_publisher(InstrumentState, "/twin/tool_states", 50)
        self._event_pub = self.create_publisher(TwinEvent, "/twin/events", 50)
        self._tool_policy_status_pub = self.create_publisher(
            String,
            TOOL_POLICY_STATUS_TOPIC,
            QoSProfile(
                history=HistoryPolicy.KEEP_LAST,
                depth=1,
                reliability=ReliabilityPolicy.RELIABLE,
                durability=DurabilityPolicy.TRANSIENT_LOCAL,
            ),
        )
        self._last_tool_policy_status_json = ""
        # ScenarioStore may restart independently while the Twin stays live.
        # Retaining one authoritative lifecycle frame lets that small config
        # owner immediately distinguish a stopped/paused boundary from a
        # running procedure instead of treating startup silence as idle.
        self._simulation_state_pub = self.create_publisher(
            SimulationState,
            "/simulation/state",
            QoSProfile(
                history=HistoryPolicy.KEEP_LAST,
                depth=1,
                reliability=ReliabilityPolicy.RELIABLE,
                durability=DurabilityPolicy.TRANSIENT_LOCAL,
            ),
        )
        self._simulation_event_pub = self.create_publisher(SimulationEvent, "/simulation/event", 50)
        self._vlm_context_summary_pub = self.create_publisher(String, "/twin/vlm_context_summary", 10)
        self._vlm_request_context_pub = self.create_publisher(VLMRequestContext, "/twin/vlm_request_context", 10)
        self._important_event_pub = self.create_publisher(SimulationEvent, "/twin/important_event", 20)
        self._perception_scene_pub = self.create_publisher(PerceptionScene, "/simulation/perception_scene", 20)
        self._outward_signal_pub = self.create_publisher(SurgeonOutwardSignal, "/surgeon/outward_signal", 20)
        self._reducer_decision_pub = self.create_publisher(ReducerDecisionEvent, "/twin/reducer_decisions", 50)
        self._vlm_proposal_pub = self.create_publisher(VLMInferenceProposal, "/vlm/inference_proposals", 50)
        self._vlm_reducer_pub = self.create_publisher(VLMReducerDecision, "/vlm/reducer_decisions", 50)

        self.create_subscription(SurgeonActorEvent, "/surgeon/actor_event", self._on_surgeon_actor_event, 20)
        self.create_subscription(PhaseTransitionCue, "/surgeon/phase_transition_cue", self._on_phase_transition_cue, 20)
        self.create_subscription(PhaseEvidence, "/vlm/phase_evidence", self._on_phase_evidence, 20)
        self.create_subscription(ToolObservation, "/vlm/tool_observations", self._on_observation, 50)
        if self._tool_belief_topic:
            self.create_subscription(
                TrackedToolBeliefArray,
                self._tool_belief_topic,
                self._on_tool_beliefs,
                20,
            )
        evaluation_observation_topic = str(
            self.get_parameter("evaluation_observation_topic").value
        ).strip()
        if evaluation_observation_topic:
            self.create_subscription(
                ToolObservation,
                evaluation_observation_topic,
                self._on_observation,
                50,
            )
        self.create_subscription(VLMResult, "/vlm/result", self._on_vlm_result, 20)
        self.create_subscription(VLMResult, "/vlm_real/result", self._on_vlm_result, 20)
        hand_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=10,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.VOLATILE,
        )
        hand_health_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        self.create_subscription(
            HandGestureArray,
            self._hand_gesture_topic,
            self._on_hand_gesture,
            hand_qos,
        )
        self.create_subscription(
            HandFacingArray,
            self._hand_facing_topic,
            self._on_hand_facing,
            hand_qos,
        )
        self.create_subscription(
            HandKeypoints,
            self._hand_keypoints_topic,
            self._on_hand_keypoints,
            hand_qos,
        )
        self.create_subscription(
            String,
            self._hand_health_topic,
            self._on_hand_health,
            hand_health_qos,
        )
        self.create_subscription(
            InputSourceStatus,
            "/input/flir/status",
            self._on_input_source_status,
            10,
        )
        self.create_subscription(
            InputSourceStatus,
            "/input/cam4/status",
            self._on_input_source_status,
            10,
        )
        self.create_subscription(
            InputSourceStatus,
            "/input/vlm/status",
            self._on_input_source_status,
            10,
        )
        self.create_subscription(VLMHealth, "/vlm/health", lambda msg: self._on_vlm_health("/vlm/health", msg), 10)
        self.create_subscription(
            VLMHealth,
            "/vlm_real/health",
            lambda msg: self._on_vlm_health("/vlm_real/health", msg),
            10,
        )
        self.create_subscription(
            String,
            "/surgery/perception/rfdetr/health",
            self._on_perception_health,
            10,
        )
        self.create_subscription(TwinEvent, "/skill/events", self._on_skill_event, 50)
        self.create_subscription(
            BedRobotArmGroupRequest,
            "/surgeon/bed_robot_arm_group_request",
            self._on_bed_robot_arm_group_request,
            20,
        )
        self.create_subscription(
            BedRobotArmGroupActionProposal,
            "/vlm/bed_robot_arm_group_proposal",
            self._on_bed_robot_arm_group_proposal,
            20,
        )
        self.create_subscription(
            BedRobotArmGroupActionProposal,
            "/vlm_real/bed_robot_arm_group_proposal",
            self._on_bed_robot_arm_group_proposal,
            20,
        )
        self.create_subscription(
            BedRobotArmGroupCommand,
            "/bt/bed_robot_arm_group_command",
            self._on_bed_robot_arm_group_command,
            20,
        )
        self.create_subscription(
            BedRobotArmGroupStatus,
            "/bed_robot_arm_group/status",
            self._on_bed_robot_arm_group_status,
            50,
        )
        self.create_subscription(
            BedRobotArmStateArray,
            "/external/bed_robot_arms/status",
            self._on_bed_robot_arm_controller_status,
            20,
        )
        # ODT is an observer/state owner, not a voice-command gateway.  It
        # receives typed SurgeonRequest state updates after command_router
        # has dispatched them; no ASR or raw-transcript subscription is
        # created here.
        self.create_subscription(SurgeonRequest, "/surgeon/request", self._on_surgeon_request, 20)
        self.create_subscription(FilteredPhase, "/phase/filtered", self._on_phase, 20)
        self.create_subscription(String, "/simulation/control_state", self._on_control, 20)
        scenario_config_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        self.create_subscription(
            String,
            str(self.get_parameter("scenario_config_topic").value),
            self._on_scenario_config,
            scenario_config_qos,
        )

        self.create_timer(
            WORLD_STATE_MAINTENANCE_PERIOD_SEC,
            self._on_world_state_timer,
        )
        # Health is latched at 1 Hz, while gesture/facing frames arrive near
        # 15 Hz. Keep their leases separate and withdraw a vanished live hand
        # signal within the bounded 0.4 s observation lease (+ one 0.1 s tick).
        self.create_timer(
            HAND_HANDOVER_WATCHDOG_PERIOD_SEC,
            self._on_hand_handover_watchdog,
        )
        self._publish_world_state()

    def _hand_handover_gate_config_from_parameters(self) -> dict[str, float | int]:
        """Read the one timing owner for CAM4 hand-request admission.

        These values deliberately live together: changing a timing value
        rebuilds the pure gate and clears only the current implicit-hand
        evidence.  It does not affect an already dispatched robot Action.
        """

        return self._normalize_hand_handover_gate_config(
            {
                "dwell_sec": self.get_parameter(
                    "hand_handover_dwell_sec"
                ).value,
                "release_sec": self.get_parameter(
                    "hand_handover_release_sec"
                ).value,
                "release_confirm_sec": self.get_parameter(
                    "hand_handover_release_confirm_sec"
                ).value,
                "soft_unknown_grace_sec": self.get_parameter(
                    "hand_handover_soft_unknown_grace_sec"
                ).value,
                "max_positive_gap_sec": self.get_parameter(
                    "hand_handover_max_positive_gap_sec"
                ).value,
                "max_source_age_sec": self.get_parameter(
                    "hand_handover_max_source_age_sec"
                ).value,
                "future_tolerance_sec": self.get_parameter(
                    "hand_handover_future_tolerance_sec"
                ).value,
                "max_receipt_silence_sec": self.get_parameter(
                    "hand_handover_observation_timeout_sec"
                ).value,
                "minimum_positive_samples": self.get_parameter(
                    "hand_handover_minimum_positive_samples"
                ).value,
            }
        )

    @staticmethod
    def _normalize_hand_handover_gate_config(
        values: dict[str, object],
    ) -> dict[str, float | int]:
        """Validate timing independently from the surrounding ROS node."""

        def positive(name: str) -> float:
            value = float(values[name])
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"{name} must be a finite value greater than zero")
            return value

        def nonnegative(name: str) -> float:
            value = float(values[name])
            if not math.isfinite(value) or value < 0.0:
                raise ValueError(
                    f"{name} must be a finite value greater than or equal to zero"
                )
            return value

        release_sec = positive("release_sec")
        release_confirm_sec = positive("release_confirm_sec")
        return {
            "dwell_sec": positive("dwell_sec"),
            "release_sec": release_sec,
            "release_confirm_sec": min(release_sec, release_confirm_sec),
            "soft_unknown_grace_sec": positive("soft_unknown_grace_sec"),
            "max_positive_gap_sec": positive("max_positive_gap_sec"),
            "max_source_age_sec": nonnegative("max_source_age_sec"),
            "future_tolerance_sec": nonnegative("future_tolerance_sec"),
            "max_receipt_silence_sec": positive("max_receipt_silence_sec"),
            "minimum_positive_samples": max(
                2, int(values["minimum_positive_samples"])
            ),
        }

    @staticmethod
    def _build_hand_handover_gate(
        values: dict[str, float | int],
    ) -> ContinuousHandHandoverGate:
        return ContinuousHandHandoverGate(**values)

    def _replace_hand_handover_gate(
        self,
        values: dict[str, float | int],
    ) -> None:
        """Apply live hand-gate timing without restarting the state owner."""

        self._hand_handover_gate_config = values
        self._hand_observation_timeout_sec = float(
            values["max_receipt_silence_sec"]
        )
        self._hand_source_max_age_sec = float(values["max_source_age_sec"])
        self._hand_source_future_tolerance_sec = float(
            values["future_tolerance_sec"]
        )
        self._hand_handover_gate = self._build_hand_handover_gate(values)
        # A timing update is a new admission epoch.  It never alters an active
        # robot task, but cannot carry the old candidate into the new timing
        # regime.
        self._withdraw_hand_handover_evidence()

    def _on_parameters_changed(self, params):
        # Keep the generic parameter callback usable in the small pure-node
        # tests too: those tests construct a partial node solely to exercise a
        # different parameter family, before hand-gate state has been built.
        # Only hydrate this timing owner when one of its parameters is changed.
        gate_values: dict[str, float | int] | None = None
        gate_changed = False
        for parameter in params:
            config_key = HAND_HANDOVER_GATE_PARAMETER_BINDINGS.get(
                parameter.name
            )
            if config_key is not None:
                if gate_values is None:
                    gate_values = dict(self._hand_handover_gate_config)
                gate_values[config_key] = parameter.value
                gate_changed = True
        if gate_changed:
            try:
                gate_values = self._normalize_hand_handover_gate_config(
                    gate_values or {}
                )
            except (TypeError, ValueError) as exc:
                return SetParametersResult(
                    successful=False,
                    reason=f"invalid hand handover timing: {exc}",
                )

        for parameter in params:
            if parameter.name in HAND_HANDOVER_GATE_PARAMETER_BINDINGS:
                continue
            if parameter.name == "spec_dir":
                try:
                    next_spec_dir = str(parameter.value)
                    next_spec = load_bundle(next_spec_dir)
                    state = self._twin.state
                    paused_quiescent = bool(
                        state.running
                        and state.execution_state == "paused"
                        and getattr(state, "active_robot_task", None) is None
                        and not bool(getattr(state, "cleaner_busy", False))
                        and not self._pending_bed_robot_arm_group_requests
                    )
                    if state.running and not paused_quiescent:
                        raise ValueError(
                            "scenario config requires stopped or quiescent paused state"
                        )
                    if paused_quiescent:
                        self._twin.swap_spec_preserving_paused_world(next_spec)
                    else:
                        self._twin.reset_spec(next_spec)
                        self._stamp_all_bed_robot_arm_groups()
                    # Commit the selected bundle only after its world swap
                    # completed.  A rejected runtime update must keep the
                    # last-good configuration and world together.
                    self._spec_dir = next_spec_dir
                    self._prior_scorer = ProcedurePriorScorer(self._twin.spec, compact_procedure_prompt(self._spec_dir))
                    self._handover_ngram_prior = load_frozen_handover_ngram_prior(
                        self._twin.spec,
                        self._spec_dir,
                    )
                    requestable_tool_ids = set(
                        self._twin.spec.list_requestable_instrument_ids()
                    )
                    self._ngram_recovery_enabled_tools = frozenset(
                        tool_id
                        for tool_id in self._ngram_recovery_enabled_tools
                        if tool_id in requestable_tool_ids
                    )
                    self._bundle_metadata_cache = self._build_bundle_metadata()
                    self._important_events.clear()
                    self._clear_tool_histories()
                    self._ngram_preparation_stability.clear()
                    self._ngram_recovery_stability.clear()
                    self._reset_hand_handover_state()
                    self._advance_visual_runtime_epoch()
                    self._advance_skill_event_runtime_epoch()
                    self._pending_bed_robot_arm_group_requests.clear()
                    if not paused_quiescent:
                        self._reset_bed_robot_controller_freshness()
                    self._phase_entered_ros_sec = self._stamp_sec(self._stamp())
                    self._last_lifecycle_control_signature = None
                    self._publish_world_state()
                except Exception as exc:
                    return SetParametersResult(
                        successful=False,
                        reason=f"failed to reload spec bundle: {exc}",
                    )
            elif parameter.name == "vlm_recent_event_count":
                self._vlm_recent_event_count = max(1, int(parameter.value))
                self._important_events = deque(self._important_events, maxlen=self._vlm_recent_event_count)
            elif parameter.name == "validation_mode":
                self._validation_mode = str(parameter.value)
            elif parameter.name == "phase_authority":
                self._phase_authority = str(parameter.value)
            elif parameter.name == "vlm_mode":
                self._vlm_mode = str(parameter.value)
            elif parameter.name == "vlm_health_timeout_sec":
                self._vlm_health_timeout_sec = max(0.5, float(parameter.value))
            elif parameter.name == "ngram_prepare_probability_threshold":
                self._ngram_prepare_probability_threshold = max(
                    0.0, min(1.0, float(parameter.value))
                )
            elif parameter.name == "ngram_recovery_probability_threshold":
                self._ngram_recovery_probability_threshold = max(
                    0.0, min(1.0, float(parameter.value))
                )
            elif parameter.name == "ngram_recovery_enabled_tools":
                values = [
                    str(tool_id).strip()
                    for tool_id in parameter.value
                    if str(tool_id).strip()
                ]
                requestable_tool_ids = set(
                    self._twin.spec.list_requestable_instrument_ids()
                )
                unknown = sorted(set(values) - requestable_tool_ids)
                if unknown:
                    return SetParametersResult(
                        successful=False,
                        reason=(
                            "ngram_recovery_enabled_tools contains unknown "
                            "instrument IDs: " + ", ".join(unknown)
                        ),
                    )
                self._ngram_recovery_enabled_tools = frozenset(values)
                # Re-enabling a tool starts a fresh probability dwell instead
                # of inheriting time accumulated while recovery was disabled.
                self._ngram_recovery_stability.clear()
            elif parameter.name == "ngram_policy_stability_sec":
                self._ngram_policy_stability_sec = max(
                    0.1, float(parameter.value)
                )
            elif parameter.name == "vlm_evidence_max_gap_sec":
                self._vlm_evidence_max_gap_sec = max(
                    0.5,
                    float(parameter.value),
                )
            elif parameter.name == "hand_mapping_operator_approved":
                self._hand_mapping_operator_approved = bool(parameter.value)
                self._refresh_hand_health_admission()
            elif parameter.name == "accept_validation_actor_events":
                self._accept_validation_actor_events = bool(parameter.value)
            elif parameter.name == "accept_non_override_structured_requests":
                self._accept_non_override_structured_requests = bool(parameter.value)
            elif parameter.name == "bed_robot_status_timeout_sec":
                self._bed_robot_status_timeout_sec = max(
                    0.1, float(parameter.value)
                )
            elif parameter.name == "bed_robot_source_max_age_sec":
                self._bed_robot_source_max_age_sec = max(
                    0.1, float(parameter.value)
                )
            elif parameter.name == "bed_robot_source_future_tolerance_sec":
                self._bed_robot_source_future_tolerance_sec = max(
                    0.0, float(parameter.value)
                )
            elif parameter.name == "cam4_mayo_observation_max_age_sec":
                self._cam4_mayo_observation_max_age_sec = max(
                    0.1, float(parameter.value)
                )
            elif (
                parameter.name
                == "cam4_mayo_observation_future_tolerance_sec"
            ):
                self._cam4_mayo_observation_future_tolerance_sec = max(
                    0.0, float(parameter.value)
                )
        if gate_changed:
            self._replace_hand_handover_gate(gate_values or {})
        if any(parameter.name in TOOL_POLICY_PARAMETER_NAMES for parameter in params):
            self._publish_tool_policy_status(force=True)
        return SetParametersResult(successful=True)

    def _on_scenario_config(self, message: String) -> None:
        """Apply ScenarioStore selection at a stopped or quiescent pause boundary."""

        try:
            snapshot = parse_scenario_config(message.data)
            load_scenario_consumer_bundle(
                snapshot,
                fixed_spec_root=self._scenario_config_root,
            )
        except Exception as exc:
            self.get_logger().warning(
                f"digital twin scenario config ignored: {exc}",
                throttle_duration_sec=2.0,
            )
            return
        with self._scenario_config_lock:
            if snapshot.revision == self._scenario_config_revision:
                return
            self._pending_scenario_config = snapshot
        self._apply_pending_scenario_config_if_quiescent()

    def _apply_pending_scenario_config_if_quiescent(self) -> None:
        with self._scenario_config_lock:
            snapshot = self._pending_scenario_config
            state = self._twin.state
            paused_quiescent = bool(
                state.running
                and state.execution_state == "paused"
                and state.active_robot_task is None
                and not state.cleaner_busy
                and not self._pending_bed_robot_arm_group_requests
            )
            if snapshot is None or (state.running and not paused_quiescent):
                return
        try:
            bundle = load_scenario_consumer_bundle(
                snapshot,
                fixed_spec_root=self._scenario_config_root,
            )
            result = self.set_parameters_atomically(
                [Parameter(name="spec_dir", value=bundle.spec_dir)]
            )
        except Exception as exc:  # pragma: no cover - rclpy transport failure
            self.get_logger().error(
                f"digital twin scenario config swap failed: {exc}"
            )
            return
        if not bool(getattr(result, "successful", False)):
            self.get_logger().error(
                "digital twin scenario config swap rejected: "
                f"{getattr(result, 'reason', 'unknown reason')}"
            )
            return
        with self._scenario_config_lock:
            self._scenario_config_revision = snapshot.revision
            if self._pending_scenario_config is snapshot:
                self._pending_scenario_config = None
        self.get_logger().info(
            "digital twin scenario revision applied atomically: "
            f"{snapshot.bundle_name}@{snapshot.revision}"
        )

    def _stamp(self):
        return self.get_clock().now().to_msg()

    def _required_vlm_health_topics(self) -> list[str]:
        vlm_mode = str(getattr(self, "_vlm_mode", "mock"))
        if vlm_mode == "real":
            return ["/vlm/health"]
        if vlm_mode == "dual":
            return ["/vlm_real/health"]
        return []

    def _on_vlm_health(self, topic: str, msg: VLMHealth) -> None:
        self._vlm_health_by_topic[topic] = (msg, time.monotonic())
        self._publish_world_state_if_dirty()

    def _refresh_hand_health_admission(self) -> bool:
        payload = getattr(self, "_hand_health_payload", None)
        ready, reason = validate_hand_health(
            payload,
            pins=self._hand_perception_pins,
            operator_mapping_approved=bool(
                getattr(self, "_hand_mapping_operator_approved", False)
            ),
        )
        received_at = float(
            getattr(self, "_hand_health_received_monotonic", 0.0)
        )
        if (
            ready
            and (
                received_at <= 0.0
                or time.monotonic() - received_at
                > float(getattr(self, "_hand_health_timeout_sec", 2.0))
            )
        ):
            ready = False
            reason = "hand_health_stale"
        previous_ready = bool(getattr(self, "_hand_health_ready", False))
        previous_reason = str(
            getattr(self, "_hand_health_reason", "hand_health_missing")
        )
        self._hand_health_ready = ready
        self._hand_health_reason = reason
        if not ready:
            gate = getattr(self, "_hand_handover_gate", None)
            if gate is not None:
                gate.withdraw(preserve_episode=True)
            self._withdraw_hand_handover_evidence()
        if ready != previous_ready or reason != previous_reason:
            self._publish_reducer_decision_event(
                input_type="hand_perception_health",
                input_id="cam4_right_hand",
                input_source="cam4_hand_perception",
                accepted=ready,
                reason=reason,
                detail={
                    "gesture_topic": self._hand_gesture_topic,
                    "facing_topic": self._hand_facing_topic,
                    "keypoints_topic": self._hand_keypoints_topic,
                    "health_topic": self._hand_health_topic,
                    "mapping_operator_approved": bool(
                        self._hand_mapping_operator_approved
                    ),
                },
            )
        return ready

    def _on_hand_health(self, msg: String) -> None:
        try:
            payload = json.loads(str(msg.data))
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = None
        self._hand_health_payload = payload if isinstance(payload, dict) else None
        self._hand_health_received_monotonic = time.monotonic()
        self._refresh_hand_health_admission()
        self._publish_world_state_if_dirty()

    def _on_hand_gesture(self, msg: HandGestureArray) -> None:
        occupancy_changed = self._update_cam4_mayo_hand_presence(msg)
        triplet = self._hand_handover_joiner.add_gesture(msg)
        if triplet is not None:
            self._on_hand_observation_triplet(*triplet)
        if occupancy_changed:
            self._publish_world_state_if_dirty()

    def _project_cam4_mayo_hand_presence(self, present: bool) -> bool:
        """Apply the occupancy bit and reconcile one uncommitted request."""

        present = bool(present)
        state = self._twin.state
        occupancy_changed = bool(
            getattr(state, "cam4_mayo_hand_present", True)
        ) != present
        self._cam4_mayo_hand_present = present
        reconcile = getattr(self._twin, "set_cam4_mayo_hand_present", None)
        if callable(reconcile):
            request_changed = bool(reconcile(present))
        else:
            state.cam4_mayo_hand_present = present
            request_changed = False
        return occupancy_changed or request_changed

    def _update_cam4_mayo_hand_presence(
        self,
        msg: HandGestureArray,
    ) -> bool:
        """Project any pinned CAM4 hand detection into Mayo occupancy.

        Gesture class, palm facing, handedness, and pose confidence are
        intentionally irrelevant here.  CAM4's configured field of view is
        the Mayo work surface; a non-empty detector result therefore blocks a
        new Mayo manipulation.  Invalid, duplicate, or older frames never
        clear a previously observed hand.
        """

        key = stamp_key(msg)
        pins = self._hand_perception_pins
        if key is None or key[2] != pins.source_frame_id:
            return False
        if (
            str(getattr(msg, "model_name", "")) != pins.gesture_model_name
            or str(getattr(msg, "model_version", ""))
            != pins.gesture_model_version
            or str(getattr(msg, "model_asset_sha256", ""))
            != pins.gesture_model_sha256
        ):
            return False
        present = bool(getattr(msg, "hands", ()))
        stamp_ns = int(key[0]) * 1_000_000_000 + int(key[1])
        last_stamp_ns = getattr(
            self, "_cam4_mayo_hand_source_stamp_ns", None
        )
        if present:
            # Any pinned non-empty detection is safe to consume even when it
            # is delayed or out of order: the only consequence is blocking a
            # new Mayo motion. Never let source rewind hide a detected hand.
            if last_stamp_ns is None or stamp_ns > int(last_stamp_ns):
                self._cam4_mayo_hand_source_stamp_ns = stamp_ns
            return self._project_cam4_mayo_hand_presence(True)
        if last_stamp_ns is not None and stamp_ns <= int(last_stamp_ns):
            # A duplicate/rewound empty result cannot retain an earlier
            # hand-free lease.
            return self._project_cam4_mayo_hand_presence(True)
        receipt_monotonic = time.monotonic()
        source_stamp_sec = stamp_key_sec(key)
        source_now_sec = float(self._stamp_sec(self._stamp()))
        empty_source_time_fresh = bool(
            math.isfinite(source_stamp_sec)
            and math.isfinite(source_now_sec)
            and source_stamp_sec > 0.0
            and source_now_sec - source_stamp_sec
            <= float(getattr(self, "_hand_source_max_age_sec", 0.5))
            and source_stamp_sec - source_now_sec
            <= float(
                getattr(self, "_hand_source_future_tolerance_sec", 0.5)
            )
        )
        if (
            not empty_source_time_fresh
            or not self._cam4_mayo_hand_detector_ready(
                now_monotonic=receipt_monotonic
            )
        ):
            # A producer may continue emitting empty arrays while reporting
            # that RGB/gesture inference is unavailable, or a delayed replay
            # may deliver an old empty result. Neither is evidence that the
            # physical Mayo workspace is hand-free now.
            return self._project_cam4_mayo_hand_presence(True)
        # Only a fully admitted empty frame may advance the clear-side order
        # and receipt leases. Rejected replay/future data cannot renew or
        # poison them.
        self._cam4_mayo_hand_source_stamp_ns = stamp_ns
        self._cam4_mayo_hand_received_monotonic = receipt_monotonic
        return self._project_cam4_mayo_hand_presence(False)

    def _cam4_mayo_hand_detector_ready(
        self,
        *,
        now_monotonic: float | None = None,
    ) -> bool:
        """Validate only the RGB gesture-detector health needed for occupancy.

        Palm-facing, depth, handedness, and mapping health intentionally do
        not participate: Mayo occupancy is pose-independent.  The gesture
        model provenance and receipt lease remain mandatory before an empty
        frame can clear the fail-closed latch.
        """

        payload = getattr(self, "_hand_health_payload", None)
        if not isinstance(payload, dict):
            return False
        if payload.get("schema") != "pnu.hand_keypoint_health.v1":
            return False
        for field in (
            "gesture_rgb_ready",
            "gesture_model_ready",
            "gesture_inference_ready",
        ):
            if payload.get(field) is not True:
                return False
        pins = self._hand_perception_pins
        if payload.get("gesture_model_version") != pins.gesture_model_version:
            return False
        if payload.get("gesture_model_asset_sha256") != pins.gesture_model_sha256:
            return False
        received_monotonic = float(
            getattr(self, "_hand_health_received_monotonic", 0.0)
        )
        now = (
            time.monotonic()
            if now_monotonic is None
            else float(now_monotonic)
        )
        return bool(
            received_monotonic > 0.0
            and now >= received_monotonic
            and now - received_monotonic
            <= float(getattr(self, "_hand_health_timeout_sec", 2.0))
        )

    def _expire_cam4_mayo_hand_free_lease(self) -> bool:
        """Re-latch occupancy when fresh healthy empty-frame evidence ends."""

        state = self._twin.state
        if bool(getattr(state, "cam4_mayo_hand_present", True)):
            return False
        now_monotonic = time.monotonic()
        received_monotonic = float(
            getattr(self, "_cam4_mayo_hand_received_monotonic", 0.0)
        )
        observation_fresh = bool(
            received_monotonic > 0.0
            and now_monotonic >= received_monotonic
            and now_monotonic - received_monotonic
            <= float(getattr(self, "_hand_observation_timeout_sec", 0.4))
        )
        if observation_fresh and self._cam4_mayo_hand_detector_ready(
            now_monotonic=now_monotonic
        ):
            return False
        return self._project_cam4_mayo_hand_presence(True)

    def _on_hand_facing(self, msg: HandFacingArray) -> None:
        triplet = self._hand_handover_joiner.add_facing(msg)
        if triplet is not None:
            self._on_hand_observation_triplet(*triplet)

    def _on_hand_keypoints(self, msg: HandKeypoints) -> None:
        triplet = self._hand_handover_joiner.add_keypoints(msg)
        if triplet is not None:
            self._on_hand_observation_triplet(*triplet)

    def _on_hand_observation_triplet(
        self,
        gesture_msg: HandGestureArray,
        facing_msg: HandFacingArray,
        keypoints_msg: HandKeypoints,
    ) -> None:
        state = self._twin.state
        if self._active_robot_task_is_direct_delivery():
            # The receiving hand is part of the in-flight delivery, not a new
            # request. Drop the queued pair and require a fresh release before
            # the direct-hand channel can arm again after task completion.
            self._suspend_hand_handover_state()
            self._publish_world_state_if_dirty()
            return
        key = stamp_key(gesture_msg)
        if key is None:
            return
        evidence = classify_hand_frame(
            gesture_msg,
            facing_msg,
            keypoints_message=keypoints_msg,
            pins=self._hand_perception_pins,
            minimum_gesture_score=self._hand_minimum_gesture_score,
            minimum_handedness_score=self._hand_minimum_handedness_score,
            minimum_palm_up_score=self._hand_minimum_palm_up_score,
        )
        if not self._refresh_hand_health_admission():
            evidence = HandFrameEvidence(
                stamp_key_sec(key),
                FrameDisposition.UNKNOWN,
                self._hand_health_reason,
            )
        # Hand perception is an observer input, not an execution request.  Keep
        # its reducer-visible evidence live while the scenario is idle or
        # paused so an operator can verify the external CAM4 path before a
        # run.  Direct delivery remains independently gated in
        # ORDigitalTwin.direct_hand_preposition_ready(), which requires the
        # running execution state before any action can be selected or sent.
        update = self._hand_handover_gate.observe(
            evidence,
            source_now_sec=self._stamp_sec(self._stamp()),
            receipt_monotonic=time.monotonic(),
        )
        self._apply_hand_handover_update(update, evidence)

    def _apply_hand_handover_update(self, update, evidence) -> None:
        state = self._twin.state
        right_hand_tool = str(
            getattr(state, "right_hand_tool", "") or ""
        ).strip()
        repeat_handover_requires_voice = bool(
            update.active
            and right_hand_tool
            and self._repeat_handover_requires_explicit_voice(
                right_hand_tool
            )
        )
        signal_active = bool(
            update.active and not repeat_handover_requires_voice
        )
        before = (
            bool(state.implicit_request_visible),
            str(state.implicit_request_hand_pose),
            float(state.implicit_request_confidence),
            float(state.implicit_request_stability_sec),
            int(state.implicit_request_generation),
        )
        if signal_active:
            state.implicit_request_visible = True
            # A hand shape cannot identify an instrument. Selection remains a
            # separate reducer/BT decision: prepared right-hand tool first;
            # when the robot is empty, the reducer's current eligible rank 1.
            # Autonomous preparation keeps its independent policy threshold.
            state.implicit_request_tool = ""
            state.implicit_request_hand_pose = "open_receive"
            state.implicit_request_confidence = max(
                0.0, min(1.0, float(update.confidence))
            )
            state.implicit_request_stability_sec = max(
                0.0, float(update.stability_sec)
            )
            state.implicit_request_generation = max(
                int(state.implicit_request_generation),
                int(update.generation),
            )
        else:
            self._withdraw_hand_handover_evidence()
        if update.rising_edge and update.active:
            input_id = (
                f"cam4_hand_handover:{update.generation}:"
                f"{evidence.source_stamp_sec:.9f}"
            )
            detail = {
                "handedness": "Right",
                "hand_shape": "Open_Palm",
                "facing": "PALM_UP",
                "duration_sec": round(float(update.stability_sec), 3),
                "confidence": round(float(update.confidence), 3),
                "hand_index_frame_local": int(evidence.hand_index),
                "gesture_score": round(float(evidence.gesture_score), 3),
                "handedness_score": round(
                    float(evidence.handedness_score), 3
                ),
                "palm_up_score": round(float(evidence.palm_up_score), 3),
                "request_created": False,
                "tool_resolved": False,
            }
            if repeat_handover_requires_voice:
                detail["required_request_source"] = "voice"
                self._publish_reducer_decision_event(
                    input_type="hand_handover_signal",
                    input_id=input_id,
                    input_source="cam4_hand_perception",
                    accepted=False,
                    reason=(
                        "automatic_repeat_handover_requires_explicit_voice"
                    ),
                    affected_tool=right_hand_tool,
                    detail=detail,
                )
                after = (
                    bool(state.implicit_request_visible),
                    str(state.implicit_request_hand_pose),
                    float(state.implicit_request_confidence),
                    float(state.implicit_request_stability_sec),
                    int(state.implicit_request_generation),
                )
                if after != before:
                    self._publish_world_state_if_dirty()
                return
            self._publish_reducer_decision_event(
                input_type="hand_handover_signal",
                input_id=input_id,
                input_source="cam4_hand_perception",
                accepted=True,
                reason="right_open_palm_palm_up_300ms",
                affected_tool="",
                detail=detail,
            )
            self._publish_event(
                "HandHandoverSignalAccepted",
                detail={
                    "source": "cam4_hand_perception",
                    **detail,
                },
                target_owner="surgeon",
                mode="evidence_only",
            )
        after = (
            bool(state.implicit_request_visible),
            str(state.implicit_request_hand_pose),
            float(state.implicit_request_confidence),
            float(state.implicit_request_stability_sec),
            int(state.implicit_request_generation),
        )
        if after != before:
            self._publish_world_state_if_dirty()

    def _withdraw_hand_handover_evidence(self) -> None:
        state = self._twin.state
        state.implicit_request_visible = False
        state.implicit_request_tool = ""
        state.implicit_request_hand_pose = ""
        state.implicit_request_confidence = 0.0
        state.implicit_request_stability_sec = 0.0

    def _reset_hand_handover_state(self) -> None:
        joiner = getattr(self, "_hand_handover_joiner", None)
        if joiner is not None:
            joiner.clear()
        gate = getattr(self, "_hand_handover_gate", None)
        if gate is not None:
            gate.reset_all()
        self._withdraw_hand_handover_evidence()
        # Permit a restarted source epoch to establish a new monotonic frame.
        # A reset invalidates any earlier hand-free lease, so remain occupied
        # until the restarted detector supplies fresh healthy empty evidence.
        self._cam4_mayo_hand_source_stamp_ns = None
        self._cam4_mayo_hand_received_monotonic = 0.0
        self._project_cam4_mayo_hand_presence(True)
        if hasattr(self._twin.state, "implicit_request_generation"):
            self._twin.state.implicit_request_generation = 0

    def _suspend_hand_handover_state(self) -> None:
        """Withdraw direct-hand authority until a fresh release is observed."""

        joiner = getattr(self, "_hand_handover_joiner", None)
        if joiner is not None:
            joiner.clear()
        gate = getattr(self, "_hand_handover_gate", None)
        if gate is not None:
            gate.inhibit_until_release()
        self._withdraw_hand_handover_evidence()

    def _active_robot_task_is_direct_delivery(self) -> bool:
        task = getattr(self._twin.state, "active_robot_task", None)
        if task is None or not str(getattr(task, "task_id", "")).strip():
            return False
        task_type = str(getattr(task, "task_type", "")).strip().lower()
        target_anchor = str(
            getattr(task, "target_anchor_id", "")
        ).strip().lower()
        return (
            task_type in DIRECT_DELIVERY_TASK_TYPES
            or target_anchor in DIRECT_DELIVERY_TARGET_ANCHORS
        )

    def _expire_hand_handover_evidence(self) -> None:
        self._refresh_hand_health_admission()
        self._expire_cam4_mayo_hand_free_lease()
        update = self._hand_handover_gate.expire(
            receipt_monotonic=time.monotonic()
        )
        if update is not None:
            self._withdraw_hand_handover_evidence()

    def _on_input_source_status(self, msg: InputSourceStatus) -> None:
        source_id = str(msg.source_id or "").strip().lower()
        if not source_id:
            return
        self._input_source_status_by_id[source_id] = msg
        if source_id == "vlm":
            self._publish_world_state_if_dirty()

    def _source_is_ready(self, source_id: str) -> bool:
        status = getattr(self, "_input_source_status_by_id", {}).get(source_id)
        if status is None:
            return True
        return bool(status.healthy and str(status.state).upper() == "READY")

    def _perception_gate_active(self) -> bool:
        # RF-DETR remains advisory. This gate concerns the VLM evidence stream
        # itself, so raw pixels may still be used when detection is disabled.
        # Refreshing this gate can itself withdraw public VLM evidence when a
        # health lease expires. Publish that semantic edge immediately rather
        # than waiting for the next maintenance checkpoint.
        self._publish_world_state_if_dirty()
        return bool(
            "vlm_unhealthy"
            in getattr(getattr(self._twin, "state", None), "safety_flags", [])
        )

    def _camera_gate_active(self, source_id: str) -> bool:
        status = getattr(self, "_input_source_status_by_id", {}).get(source_id)
        return bool(status is not None and not self._source_is_ready(source_id))

    def _advance_visual_runtime_epoch(self) -> None:
        self._visual_runtime_epoch_floor = max(
            0,
            int(getattr(self, "_visual_runtime_epoch_floor", 0)),
        ) + 1
        getattr(self, "_visual_admission_by_channel", {}).clear()
        getattr(self, "_cam4_mayo_admission_by_channel", {}).clear()
        getattr(self, "_accepted_cam4_mayo_episodes", set()).clear()
        getattr(self, "_cam4_mayo_source_epoch_floor_by_source", {}).clear()
        # CAM4 owns an independent process epoch, so the shared VLM runtime
        # counter cannot order it.  A source-time floor at each lifecycle edge
        # prevents an in-flight frame from the preceding run crossing reset.
        try:
            self._visual_runtime_source_stamp_floor_sec = self._stamp_sec(
                self._stamp()
            )
        except Exception:
            self._visual_runtime_source_stamp_floor_sec = time.time()

    @staticmethod
    def _event_stamp_ns(stamp) -> int:
        """Return a positive source timestamp or zero when it is unusable."""

        try:
            sec = int(getattr(stamp, "sec", 0))
            nanosec = int(getattr(stamp, "nanosec", 0))
        except (TypeError, ValueError):
            return 0
        if sec < 0 or nanosec < 0:
            return 0
        return sec * 1_000_000_000 + nanosec

    def _advance_skill_event_runtime_epoch(self) -> None:
        """Invalidate every Action event chain from the preceding run."""

        self._skill_event_runtime_epoch = max(
            0,
            int(getattr(self, "_skill_event_runtime_epoch", 0)),
        ) + 1
        self._current_run_skill_task_ids = set()
        try:
            self._skill_event_source_stamp_floor_ns = self._event_stamp_ns(
                self._stamp()
            )
        except Exception:
            # A missing clock must never turn a prior action into a current
            # one. The zero floor still combines with the fresh run and
            # command-chain checks below.
            self._skill_event_source_stamp_floor_ns = 0

    @staticmethod
    def _skill_event_detail(message: TwinEvent) -> dict:
        try:
            detail = json.loads(message.detail_json) if message.detail_json else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return detail if isinstance(detail, dict) else {}

    @staticmethod
    def _skill_event_task_id(detail: dict) -> str:
        return str(
            detail.get("task_id") or detail.get("command_id") or ""
        ).strip()

    def _admit_current_run_skill_event(
        self,
        message: TwinEvent,
        detail: dict,
    ) -> tuple[bool, str]:
        """Admit only Action events proven to belong to the current run."""

        event_type = str(message.event_type or "").strip()
        if event_type not in SKILL_EVENTS_REQUIRING_CURRENT_RUN:
            return True, ""
        # Partial Node fixtures use ``__new__`` and deliberately have no
        # runtime epoch. Production construction always initializes it.
        if not hasattr(self, "_skill_event_runtime_epoch"):
            return True, ""

        state = self._twin.state
        execution_state = str(
            getattr(state, "execution_state", "")
        ).strip().lower()
        if (
            not bool(getattr(state, "running", False))
            or execution_state not in {"running", "finishing"}
            or not str(getattr(state, "procedure_run_id", "")).strip()
        ):
            return False, "skill_event_without_current_running_procedure"

        event_run_id = str(
            getattr(message, "procedure_run_id", "")
            or detail.get("procedure_run_id", "")
        ).strip()
        if event_run_id != str(state.procedure_run_id).strip():
            return False, "skill_event_procedure_run_mismatch"

        source_stamp_ns = self._event_stamp_ns(message.stamp)
        source_floor_ns = int(
            getattr(self, "_skill_event_source_stamp_floor_ns", 0)
        )
        if source_stamp_ns <= 0:
            return False, "skill_event_missing_source_stamp"
        if source_floor_ns > 0 and source_stamp_ns <= source_floor_ns:
            return False, "skill_event_precedes_current_runtime_epoch"

        task_id = self._skill_event_task_id(detail)
        if not task_id:
            return False, "skill_event_missing_command_id"
        if event_type == "RobotTaskStarted":
            if execution_state == "finishing":
                # Completion may start only its own constrained cleanup legs.
                # A normal prepare/handover request is still forbidden after
                # the spoken finish boundary, while this set can drain held
                # tools and the frozen Mayo-to-tray target snapshot.
                task_type = str(detail.get("task_type", "")).strip()
                if task_type not in FINISHING_SKILL_TASK_TYPES:
                    return False, "skill_task_start_after_finish_requested"
            return True, ""
        if task_id not in getattr(self, "_current_run_skill_task_ids", set()):
            return False, "skill_event_without_current_task_start"
        return True, ""

    def _reject_visual_evidence(
        self,
        *,
        channel: str,
        source: str,
        reason: str,
        message,
    ) -> None:
        publisher = getattr(self, "_reducer_decision_pub", None)
        if publisher is None:
            return
        correlation_id = str(getattr(message, "correlation_id", ""))
        source_epoch = int(getattr(message, "source_epoch", 0))
        source_sequence = int(getattr(message, "source_sequence", 0))
        self._publish_reducer_decision_event(
            input_type="visual_evidence_admission",
            input_id=correlation_id or channel,
            input_source=source or "unknown",
            accepted=False,
            reason=reason,
            detail={
                "channel": channel,
                "source_epoch": source_epoch,
                "source_sequence": source_sequence,
                "correlation_id": correlation_id,
                "runtime_epoch_floor": int(
                    getattr(self, "_visual_runtime_epoch_floor", 0)
                ),
            },
        )

    def _admit_visual_evidence(
        self,
        message,
        *,
        channel: str,
        source: str,
        require_epoch: bool,
    ) -> bool:
        source_epoch = max(0, int(getattr(message, "source_epoch", 0)))
        source_sequence = max(
            0,
            int(getattr(message, "source_sequence", 0)),
        )
        stamp_sec = self._stamp_sec(getattr(message, "stamp", None))
        epoch_floor = max(
            0,
            int(getattr(self, "_visual_runtime_epoch_floor", 0)),
        )
        if require_epoch and source_epoch <= 0:
            self._reject_visual_evidence(
                channel=channel,
                source=source,
                reason="missing_visual_source_epoch",
                message=message,
            )
            return False
        if source_epoch and source_epoch < epoch_floor:
            self._reject_visual_evidence(
                channel=channel,
                source=source,
                reason="stale_visual_source_epoch",
                message=message,
            )
            return False
        if source_epoch > epoch_floor:
            self._visual_runtime_epoch_floor = source_epoch
            getattr(self, "_visual_admission_by_channel", {}).clear()

        tracker = getattr(self, "_visual_admission_by_channel", None)
        if tracker is None:
            tracker = {}
            self._visual_admission_by_channel = tracker
        previous = tracker.get(channel)
        if previous is not None:
            previous_epoch, previous_sequence, previous_stamp = previous
            if source_epoch and source_epoch < previous_epoch:
                reason = "stale_visual_source_epoch"
            elif (
                source_epoch
                and source_epoch == previous_epoch
                and source_sequence > 0
                and previous_sequence > 0
                and source_sequence <= previous_sequence
            ):
                reason = (
                    "duplicate_visual_source_sequence"
                    if source_sequence == previous_sequence
                    else "out_of_order_visual_source_sequence"
                )
            elif (
                source_sequence == 0
                and stamp_sec > 0.0
                and previous_stamp > 0.0
                and stamp_sec <= previous_stamp
            ):
                reason = (
                    "duplicate_visual_source_stamp"
                    if abs(stamp_sec - previous_stamp) <= 1e-9
                    else "out_of_order_visual_source_stamp"
                )
            else:
                reason = ""
            if reason:
                self._reject_visual_evidence(
                    channel=channel,
                    source=source,
                    reason=reason,
                    message=message,
                )
                return False

        tracker[channel] = (
            source_epoch,
            source_sequence,
            stamp_sec,
        )
        return True

    @staticmethod
    def _cam4_mayo_episode_metadata(
        message: ToolObservation,
    ) -> tuple[float, str] | None:
        """Decode the bridge-authored uninterrupted visibility episode.

        The correlation is deliberately redundant with the typed epoch and
        sequence.  A producer cannot change only one field and turn an old
        continuous detection into a new post-handover appearance.
        """

        correlation_id = str(getattr(message, "correlation_id", ""))
        parts = correlation_id.split(":", 5)
        if len(parts) != 6 or parts[:2] != ["cam4-rfdetr-mayo", "v2"]:
            return None
        try:
            encoded_epoch = int(parts[2])
            encoded_sequence = int(parts[3])
            episode_started_ns = int(parts[4])
        except (TypeError, ValueError):
            return None
        source_epoch = int(getattr(message, "source_epoch", 0))
        source_sequence = int(getattr(message, "source_sequence", 0))
        instrument_name = str(getattr(message, "instrument_id", "")).strip()
        if (
            encoded_epoch <= 0
            or encoded_sequence <= 0
            or episode_started_ns <= 0
            or encoded_epoch != source_epoch
            or encoded_sequence != source_sequence
            or parts[5].strip() != instrument_name
        ):
            return None
        stamp_ns = int(getattr(message.stamp, "sec", 0)) * 1_000_000_000 + int(
            getattr(message.stamp, "nanosec", 0)
        )
        if stamp_ns <= 0 or episode_started_ns > stamp_ns:
            return None
        episode_id = (
            f"{source_epoch}:{episode_started_ns}:{instrument_name.casefold()}"
        )
        return episode_started_ns / 1_000_000_000.0, episode_id

    def _admit_cam4_mayo_observation(
        self,
        message: ToolObservation,
        *,
        channel: str,
        source: str,
    ) -> bool:
        """Fence the independent CAM4 process against replay and stale time."""

        source_epoch = max(0, int(getattr(message, "source_epoch", 0)))
        source_sequence = max(0, int(getattr(message, "source_sequence", 0)))
        stamp_sec = self._stamp_sec(getattr(message, "stamp", None))

        def reject(reason: str) -> bool:
            self._reject_visual_evidence(
                channel=channel,
                source=source,
                reason=reason,
                message=message,
            )
            return False

        if source_epoch <= 0:
            return reject("missing_cam4_source_epoch")
        if source_sequence <= 0:
            return reject("missing_cam4_source_sequence")
        if not math.isfinite(stamp_sec) or stamp_sec <= 0.0:
            return reject("missing_cam4_source_stamp")
        try:
            now_sec = self._stamp_sec(self._stamp())
        except Exception:
            now_sec = time.time()
        source_age_sec = now_sec - stamp_sec
        if source_age_sec > float(
            getattr(self, "_cam4_mayo_observation_max_age_sec", 2.0)
        ):
            return reject("stale_cam4_source_stamp")
        if source_age_sec < -float(
            getattr(
                self,
                "_cam4_mayo_observation_future_tolerance_sec",
                0.5,
            )
        ):
            return reject("future_cam4_source_stamp")
        if stamp_sec < float(
            getattr(self, "_visual_runtime_source_stamp_floor_sec", 0.0)
        ):
            return reject("cam4_source_precedes_runtime_epoch")

        epoch_floors = getattr(
            self, "_cam4_mayo_source_epoch_floor_by_source", None
        )
        if epoch_floors is None:
            epoch_floors = {}
            self._cam4_mayo_source_epoch_floor_by_source = epoch_floors
        epoch_floor = max(0, int(epoch_floors.get(source, 0)))
        if source_epoch < epoch_floor:
            return reject("stale_cam4_source_epoch")
        tracker = getattr(self, "_cam4_mayo_admission_by_channel", None)
        if tracker is None:
            tracker = {}
            self._cam4_mayo_admission_by_channel = tracker
        if source_epoch > epoch_floor:
            epoch_floors[source] = source_epoch
            source_channel_prefix = f"cam4_tool:{source}:"
            for tracked_channel in tuple(tracker):
                if tracked_channel.startswith(source_channel_prefix):
                    tracker.pop(tracked_channel, None)
            if source == "cam4_rfdetr_mayo_observation":
                getattr(self, "_accepted_cam4_mayo_episodes", set()).clear()

        previous = tracker.get(channel)
        if previous is not None:
            previous_epoch, previous_sequence, previous_stamp = previous
            if source_epoch < previous_epoch:
                return reject("stale_cam4_source_epoch")
            if source_epoch == previous_epoch and source_sequence <= previous_sequence:
                return reject(
                    "duplicate_cam4_source_sequence"
                    if source_sequence == previous_sequence
                    else "out_of_order_cam4_source_sequence"
                )
            if source_epoch == previous_epoch and stamp_sec <= previous_stamp:
                return reject(
                    "duplicate_cam4_source_stamp"
                    if abs(stamp_sec - previous_stamp) <= 1e-9
                    else "out_of_order_cam4_source_stamp"
                )
        tracker[channel] = (source_epoch, source_sequence, stamp_sec)
        return True

    def _on_perception_health(self, msg: String) -> None:
        try:
            payload = json.loads(msg.data)
        except (json.JSONDecodeError, TypeError):
            return
        if (
            not isinstance(payload, dict)
            or payload.get("schema") != "taskplanner.rfdetr_health.v1"
        ):
            return
        previous_health = (
            bool(getattr(self, "_perception_health_seen", False)),
            bool(getattr(self, "_perception_enabled", True)),
        )
        self._perception_health_seen = True
        self._perception_enabled = bool(payload.get("enabled"))
        if not self._perception_enabled:
            self._twin.clear_object_detection_evidence()
        health_changed = previous_health != (
            self._perception_health_seen,
            self._perception_enabled,
        )
        self._run_time_based_maintenance()
        current_signature = self._world_maintenance_signature()
        if (
            health_changed
            or getattr(self, "_last_world_emit_signature", None) is None
            or current_signature != self._last_world_emit_signature
        ):
            self._emit_world_state()

    def _refresh_vlm_safety_flags(self) -> None:
        required_topics = self._required_vlm_health_topics()
        if not required_topics:
            if hasattr(self._twin, "set_safety_flag"):
                self._twin.set_safety_flag("vlm_unhealthy", False)
            self._vlm_evidence_blocked = False
            return
        now = time.monotonic()
        state = self._twin.state
        execution_active = bool(getattr(state, "running", False)) and str(
            getattr(state, "execution_state", "")
        ).strip().lower() in {"starting", "running"}
        started_at = getattr(self, "_vlm_health_run_started_monotonic", None)
        if execution_active and started_at is None:
            # A state-core restart can reconcile into an already-running
            # scenario without observing its original start control frame.
            # Give the reattached VLM health lease one normal timeout window.
            started_at = now
            self._vlm_health_run_started_monotonic = now
        startup_grace = max(
            float(getattr(self, "_vlm_health_timeout_sec", VLM_STARTUP_HEALTH_GRACE_SEC)),
            VLM_STARTUP_HEALTH_GRACE_SEC,
        )
        startup_waiting = bool(
            execution_active
            and started_at is not None
            and now - float(started_at) <= startup_grace
        )
        unhealthy = False
        for topic in required_topics:
            sample = getattr(self, "_vlm_health_by_topic", {}).get(topic)
            if sample is None:
                # No VLM request is deliberately made while idle.  At start,
                # the initial request may still be queued/in flight, so only
                # treat a missing health lease as a fault after its bounded
                # first-response window.
                unhealthy = unhealthy or (execution_active and not startup_waiting)
                continue
            health, received_at = sample
            if now - received_at > float(
                getattr(self, "_vlm_health_timeout_sec", 6.0)
            ):
                unhealthy = unhealthy or (execution_active and not startup_waiting)
                continue
            if not bool(health.connected and health.healthy) or bool(health.last_error):
                unhealthy = True
        # ``/input/vlm/status`` measures *result freshness*.  It is expected
        # to be MISSING/STALE while an intentionally idle VLM has no scenario
        # request, and while the first current-epoch inference is in flight.
        # Do not relabel those expected evidence states as a model fault.
        if unhealthy:
            if (
                not getattr(self, "_vlm_evidence_blocked", False)
                and hasattr(self._twin, "clear_perception_evidence")
            ):
                self._twin.clear_perception_evidence()
        self._vlm_evidence_blocked = unhealthy
        if hasattr(self._twin, "set_safety_flag"):
            self._twin.set_safety_flag("vlm_unhealthy", unhealthy)

    def _instrument_metadata_payload(self, spec) -> list[dict]:
        requestable_instruments = spec.list_requestable_instrument_ids()
        requestable_set = set(requestable_instruments)
        return [
            {
                "id": instrument.id,
                "display_name": instrument.display_name,
                "display_name_ko": instrument.display_name_ko,
                "aliases": list(instrument.aliases),
                "category": instrument.category,
                "inventory_count": int(getattr(instrument, "inventory_count", 1)),
                "role": instrument.role,
                "handover_profile": instrument.handover_profile,
                "requestable": instrument.id in requestable_set,
                "home_location_id": spec.get_initial_location(instrument.id) or "",
                "home_location_type": (
                    spec.get_initial_location_type(instrument.id) or ""
                ),
            }
            for instrument in spec.bundle.instruments
        ]

    def _bundle_metadata_payload(self, spec) -> dict:
        requestable_instruments = spec.list_requestable_instrument_ids()
        return {
            "id": spec.bundle.procedure_id,
            "display_name": spec.bundle.procedure_display_name,
            "display_name_ko": spec.bundle.procedure_display_name_ko,
            "target_site": spec.bundle.procedure_target_site,
            "target_site_ko": spec.bundle.procedure_target_site_ko,
            "approach": spec.bundle.procedure_approach,
            "approach_ko": spec.bundle.procedure_approach_ko,
            "default_phase_id": spec.default_phase_id,
            "normal_phase_ids": list(spec.normal_phase_ids),
            "interrupt_phase_ids": list(spec.interrupt_phase_ids),
            "requestable_instruments": requestable_instruments,
            "phases": [
                {
                    "id": phase.id,
                    "display_name": phase.display_name,
                    "display_name_ko": phase.display_name_ko,
                }
                for phase in spec.bundle.phases
            ],
            "instruments": self._instrument_metadata_payload(spec),
        }

    def _build_bundle_metadata(self) -> list[dict]:
        spec_dir = Path(self._spec_dir)
        parent_dir = spec_dir.parent
        if not parent_dir.is_dir():
            return [self._bundle_metadata_payload(self._twin.spec)]
        bundles: list[dict] = []
        for candidate in discover_prompt_bundle_dirs(parent_dir):
            try:
                bundles.append(self._bundle_metadata_payload(load_bundle(candidate)))
            except Exception as exc:
                self.get_logger().warning(f"failed to load bundle metadata from {candidate}: {exc}")
        return bundles or [self._bundle_metadata_payload(self._twin.spec)]

    def _catalog_entry(self, section: str, key: str) -> dict:
        section_map = self._twin.spec.bundle.display_catalog.get(section, {})
        if not isinstance(section_map, dict):
            return {}
        entry = section_map.get(key, {})
        return entry if isinstance(entry, dict) else {}

    def _augment_event_detail(self, event_type: str, detail: dict, **kwargs) -> dict:
        event_catalog = self._catalog_entry("events", event_type)
        augmented = dict(detail) if isinstance(detail, dict) else {"detail": detail}
        augmented.setdefault("display_key", event_type)
        if event_catalog.get("severity"):
            augmented.setdefault("severity", event_catalog["severity"])
        if event_catalog.get("tone"):
            augmented.setdefault("tone", event_catalog["tone"])
        if event_catalog.get("category"):
            augmented.setdefault("category", event_catalog["category"])
        instrument_id = kwargs.get("instrument_id", "")
        if instrument_id:
            augmented.setdefault("tool_id", instrument_id)
        source = kwargs.get("source_location_id", "") or kwargs.get("location_id", "")
        target = kwargs.get("target_location_id", "") or kwargs.get("location_id", "")
        if source:
            augmented.setdefault("source", source)
        if target:
            augmented.setdefault("target", target)
        if kwargs.get("mode"):
            augmented.setdefault("mode", kwargs["mode"])
        if kwargs.get("status"):
            augmented.setdefault("status", kwargs["status"])
        return augmented

    def _world_maintenance_signature(self) -> tuple:
        state = self._twin.state
        retraction = state.bed_robot_arm_groups.get("retraction")
        ranked = tuple(
            (
                int(item.rank),
                str(item.instrument_id),
                float(item.confidence),
                float(item.stability_sec),
            )
            for item in state.ranked_tool_predictions
        )
        return (
            bool(state.running),
            str(state.execution_state),
            str(state.procedure_run_id),
            (
                bool(getattr(retraction, "connected", False)),
                str(getattr(retraction, "state", "unknown")),
                str(getattr(retraction, "arm_id", "")),
                str(getattr(retraction, "end_effector_profile", "")),
                str(getattr(retraction, "error_code", "")),
            ),
            str(state.filtered_phase),
            float(state.phase_confidence),
            bool(state.phase_uncertain),
            float(state.phase_stability),
            "vlm_unhealthy" in state.safety_flags,
            str(state.predicted_tool),
            float(state.predicted_tool_confidence),
            float(state.predicted_tool_stability_sec),
            bool(getattr(state, "autonomous_preparation_ready", False)),
            ranked,
            bool(state.implicit_request_visible),
            str(state.implicit_request_tool),
            str(state.implicit_request_hand_pose),
            float(state.implicit_request_confidence),
            float(state.implicit_request_stability_sec),
            bool(state.cam4_mayo_hand_present),
        )

    def _run_time_based_maintenance(self) -> bool:
        before = self._world_maintenance_signature()
        bed_expired = self._expire_bed_robot_controller_status()
        self._expire_hand_handover_evidence()
        self._refresh_vlm_safety_flags()
        policy_changed = self._refresh_ngram_tool_policy(
            now_sec=self._monotonic_sec(),
        )
        return bool(
            bed_expired
            or policy_changed
            or self._world_maintenance_signature() != before
        )

    def _world_state_emit_due(
        self,
        *,
        now_monotonic: float,
        signature: tuple,
    ) -> bool:
        state = self._twin.state
        execution_state = str(state.execution_state).strip().lower()
        known_inactive = (
            not bool(state.running)
            and execution_state in WORLD_STATE_KNOWN_INACTIVE
        )
        if not known_inactive:
            # Active, paused, transitional, or inconsistent/unknown states use
            # the fail-safe 0.5 s cadence.
            return True
        previous_signature = getattr(
            self,
            "_last_world_emit_signature",
            None,
        )
        if previous_signature is None or signature != previous_signature:
            return True
        return (
            float(now_monotonic)
            - float(getattr(self, "_last_world_emit_monotonic", 0.0))
            >= WORLD_STATE_IDLE_CHECKPOINT_SEC
        )

    def _on_world_state_timer(self) -> None:
        self._run_time_based_maintenance()
        signature = self._world_maintenance_signature()
        if self._world_state_emit_due(
            now_monotonic=self._monotonic_sec(),
            signature=signature,
        ):
            self._emit_world_state()

    def _on_hand_handover_watchdog(self) -> None:
        before = self._world_maintenance_signature()
        self._expire_hand_handover_evidence()
        if self._world_maintenance_signature() != before:
            self._emit_world_state()

    def _publish_world_state_if_dirty(self) -> None:
        self._run_time_based_maintenance()
        signature = self._world_maintenance_signature()
        if (
            getattr(self, "_last_world_emit_signature", None) is None
            or signature != self._last_world_emit_signature
        ):
            self._emit_world_state()

    def _publish_world_state(self) -> None:
        """Run maintenance and immediately emit a semantic state edge."""
        self._run_time_based_maintenance()
        self._emit_world_state()

    def _emit_world_state(self) -> None:
        # Normalization may update public fields outside the compact cadence
        # signature. Run it only on an actual emission so an inactive skipped
        # checkpoint cannot hide an untracked semantic mutation.  Do not clear
        # the direct-hand evidence here: it is safe, read-only observability
        # while inactive, whereas actual direct delivery is gated by the Twin.
        self._twin.normalize_for_publish()
        world = WorldState()
        world.stamp = self._stamp()
        world.procedure_id = self._twin.state.procedure_id
        world.procedure_run_id = self._twin.state.procedure_run_id
        world.running = bool(self._twin.state.running)
        world.execution_state = self._twin.state.execution_state
        world.filtered_phase = self._twin.state.filtered_phase
        world.phase_confidence = float(self._twin.state.phase_confidence)
        world.phase_uncertain = bool(self._twin.state.phase_uncertain)
        world.phase_stability = float(self._twin.state.phase_stability)
        world.explicit_request_tool = self._twin.state.explicit_request_tool
        world.robot_state = self._twin.state.robot_state
        world.handover_allowed = self._twin.handover_allowed()
        world.recovery_required = self._twin.recovery_required()
        world.safety_flags = list(self._twin.state.safety_flags)
        world.expected_instruments = self._twin.get_expected_instruments()
        world.available_instruments = self._twin.get_available_instruments()
        world.recent_event_types = list(self._twin.state.recent_event_types)
        world.right_hand_tool = self._twin.state.right_hand_tool
        world.right_hand_tool_instance_id = (
            self._twin.state.right_hand_tool_instance_id
        )
        world.left_hand_tool = self._twin.state.left_hand_tool
        world.left_hand_tool_instance_id = (
            self._twin.state.left_hand_tool_instance_id
        )
        world.prepositioned_tool = self._twin.state.prepositioned_tool
        world.prepositioned_tool_instance_id = (
            self._twin.state.prepositioned_tool_instance_id
        )
        world.predicted_tool = self._twin.state.predicted_tool
        world.predicted_tool_confidence = float(self._twin.state.predicted_tool_confidence)
        world.predicted_tool_stability_sec = float(self._twin.state.predicted_tool_stability_sec)
        world.autonomous_preparation_ready = bool(
            getattr(self._twin.state, "autonomous_preparation_ready", False)
        )
        world.ranked_tool_predictions = []
        for belief in self._twin.state.ranked_tool_predictions:
            prediction = RankedToolPrediction()
            prediction.rank = int(belief.rank)
            prediction.instrument_id = belief.instrument_id
            prediction.confidence = float(belief.confidence)
            prediction.stability_sec = float(belief.stability_sec)
            world.ranked_tool_predictions.append(prediction)
        world.surgeon_intent = self._twin.state.surgeon_intent
        world.surgeon_request_tool = self._twin.state.surgeon_request_tool
        world.surgeon_request_instance_id = (
            self._twin.state.surgeon_request_instance_id
        )
        world.surgeon_request_generation = int(
            self._twin.state.surgeon_request_generation
        )
        world.surgeon_request_additional_instance_assumed = bool(
            self._twin.state.surgeon_request_additional_instance_assumed
        )
        world.explicit_request_voice_backed = self._twin.explicit_request_voice_backed()
        world.surgeon_ready_for_handover = bool(self._twin.state.surgeon_ready_for_handover)
        world.surgeon_ready_for_retrieval = bool(self._twin.state.surgeon_ready_for_retrieval)
        world.implicit_request_visible = bool(
            self._twin.state.implicit_request_visible
        )
        world.implicit_request_tool = self._twin.state.implicit_request_tool
        world.implicit_request_hand_pose = (
            self._twin.state.implicit_request_hand_pose
        )
        world.implicit_request_confidence = float(
            self._twin.state.implicit_request_confidence
        )
        world.implicit_request_stability_sec = float(
            self._twin.state.implicit_request_stability_sec
        )
        world.implicit_request_generation = int(
            self._twin.state.implicit_request_generation
        )
        world.cam4_mayo_hand_present = bool(
            self._twin.state.cam4_mayo_hand_present
        )
        world.cleaner_busy = bool(self._twin.state.cleaner_busy)
        world.cleaner_remaining_sec = float(self._twin.state.cleaner_remaining_sec)
        world.pending_transition_tools = list(self._twin.state.pending_transition_tools)
        world.active_recovery_tools = list(self._twin.state.active_recovery_tools)
        world.active_recovery_tool_instances = list(
            self._twin.state.active_recovery_tool_instances
        )
        active_task = self._twin.state.active_robot_task
        world.active_robot_task_id = active_task.task_id if active_task else ""
        world.active_robot_task_type = active_task.task_type if active_task else ""
        world.active_robot_task_tool_id = active_task.instrument_id if active_task else ""
        world.active_robot_task_tool_instance_id = (
            active_task.instrument_instance_id if active_task else ""
        )
        world.active_robot_task_arm = active_task.arm if active_task else ""
        world.active_robot_task_source_anchor = active_task.source_anchor_id if active_task else ""
        world.active_robot_task_target_anchor = active_task.target_anchor_id if active_task else ""
        world.active_robot_task_progress = float(active_task.progress) if active_task else 0.0
        world.active_robot_task_remaining_sec = float(active_task.remaining_sec) if active_task else 0.0
        world.bed_robot_arm_groups = [
            self._bed_robot_arm_group_state_message(payload, world.stamp)
            for payload in self._twin.bed_robot_arm_group_payload()
        ]
        world.instrument_states = []
        for payload in self._twin.instrument_payload():
            msg = InstrumentState()
            msg.stamp = self._stamp()
            msg.instrument_id = payload["instrument_id"]
            msg.instance_id = payload["instance_id"]
            msg.home_location_type = payload["home_location_type"]
            msg.home_location_id = payload["home_location_id"]
            msg.location_type = payload["location_type"]
            msg.location_id = payload["location_id"]
            msg.owner = payload["owner"]
            msg.status = payload["status"]
            msg.confidence = float(payload["confidence"])
            msg.cleanliness_state = payload["cleanliness_state"]
            msg.contaminated = bool(payload["contaminated"])
            msg.reserved_for = payload["reserved_for"]
            msg.last_holder = payload["last_holder"]
            msg.lifecycle_stage = payload["lifecycle_stage"]
            msg.next_required_transition = payload["next_required_transition"]
            msg.visual_anchor_id = payload["visual_anchor_id"]
            msg.preposition_origin_location_type = payload[
                "preposition_origin_location_type"
            ]
            msg.preposition_origin_location_id = payload[
                "preposition_origin_location_id"
            ]
            msg.preposition_origin_lifecycle_stage = payload[
                "preposition_origin_lifecycle_stage"
            ]
            msg.procedure_future_use_expected = bool(
                self._twin.procedure_future_use_expected(payload["instance_id"])
            )
            msg.mayo_placement_evidence = payload["mayo_placement_evidence"]
            msg.last_observed_sec = float(payload["last_update_sec"])
            msg.mayo_reuse_confidence = float(
                payload["mayo_reuse_confidence"]
            )
            msg.mayo_reuse_stability_sec = float(
                payload["mayo_reuse_stability_sec"]
            )
            msg.mayo_recovery_confidence = float(
                payload["mayo_recovery_confidence"]
            )
            msg.mayo_recovery_stability_sec = float(
                payload["mayo_recovery_stability_sec"]
            )
            msg.mayo_evidence_source = payload["mayo_evidence_source"]
            world.instrument_states.append(msg)
            self._tool_pub.publish(msg)
        self._world_pub.publish(world)

        simulation = SimulationState()
        simulation.stamp = world.stamp
        simulation.procedure_id = self._twin.state.procedure_id
        simulation.procedure_run_id = self._twin.state.procedure_run_id
        simulation.active_bundle = self._twin.state.procedure_id
        simulation.running = bool(self._twin.state.running)
        simulation.execution_state = self._twin.state.execution_state
        simulation.filtered_phase = self._twin.state.filtered_phase
        simulation.robot_state = self._twin.state.robot_state
        simulation.surgeon_intent = self._twin.state.surgeon_intent
        simulation.surgeon_request_tool = self._twin.state.surgeon_request_tool
        simulation.surgeon_request_instance_id = (
            self._twin.state.surgeon_request_instance_id
        )
        simulation.surgeon_request_generation = int(
            self._twin.state.surgeon_request_generation
        )
        simulation.surgeon_ready_for_handover = bool(self._twin.state.surgeon_ready_for_handover)
        simulation.surgeon_ready_for_retrieval = bool(self._twin.state.surgeon_ready_for_retrieval)
        simulation.cleaner_busy = bool(self._twin.state.cleaner_busy)
        simulation.cleaner_remaining_sec = float(self._twin.state.cleaner_remaining_sec)
        simulation.pending_transition_tools = list(self._twin.state.pending_transition_tools)
        simulation.active_recovery_tools = list(self._twin.state.active_recovery_tools)
        simulation.active_recovery_tool_instances = list(
            self._twin.state.active_recovery_tool_instances
        )
        simulation.right_hand_tool = self._twin.state.right_hand_tool
        simulation.right_hand_tool_instance_id = (
            self._twin.state.right_hand_tool_instance_id
        )
        simulation.left_hand_tool = self._twin.state.left_hand_tool
        simulation.left_hand_tool_instance_id = (
            self._twin.state.left_hand_tool_instance_id
        )
        simulation.prepositioned_tool = self._twin.state.prepositioned_tool
        simulation.prepositioned_tool_instance_id = (
            self._twin.state.prepositioned_tool_instance_id
        )
        simulation.active_robot_task_id = active_task.task_id if active_task else ""
        simulation.active_robot_task_type = active_task.task_type if active_task else ""
        simulation.active_robot_task_tool_id = active_task.instrument_id if active_task else ""
        simulation.active_robot_task_tool_instance_id = (
            active_task.instrument_instance_id if active_task else ""
        )
        simulation.active_robot_task_arm = active_task.arm if active_task else ""
        simulation.active_robot_task_source_anchor = active_task.source_anchor_id if active_task else ""
        simulation.active_robot_task_target_anchor = active_task.target_anchor_id if active_task else ""
        simulation.active_robot_task_progress = float(active_task.progress) if active_task else 0.0
        simulation.active_robot_task_remaining_sec = float(active_task.remaining_sec) if active_task else 0.0
        simulation.recent_events = list(self._twin.state.recent_event_types)
        simulation.instrument_states = list(world.instrument_states)
        simulation.bed_robot_arm_groups = list(world.bed_robot_arm_groups)
        requestable_instruments = (
            self._twin.spec.list_requestable_instrument_ids()
        )
        simulation.layout_json = json.dumps(
            {
                "entities": [
                    {
                        **asdict(entity),
                        "display_name": entity.label or entity.id,
                        "display_name_ko": entity.label or entity.id,
                    }
                    for entity in self._twin.spec.bundle.simulation_entities
                ],
                "anchors": [
                    {
                        **asdict(anchor),
                        "display_name": anchor.label or anchor.id,
                        "display_name_ko": anchor.label or anchor.id,
                    }
                    for anchor in self._twin.spec.bundle.simulation_anchors
                ],
                "metadata": {
                    "procedure": {
                        "id": self._twin.spec.bundle.procedure_id,
                        "display_name": self._twin.spec.bundle.procedure_display_name,
                        "display_name_ko": self._twin.spec.bundle.procedure_display_name_ko,
                        "target_site": self._twin.spec.bundle.procedure_target_site,
                        "target_site_ko": self._twin.spec.bundle.procedure_target_site_ko,
                        "approach": self._twin.spec.bundle.procedure_approach,
                        "approach_ko": self._twin.spec.bundle.procedure_approach_ko,
                    },
                    "display_catalog": self._twin.spec.bundle.display_catalog,
                    "normal_phase_ids": list(self._twin.spec.normal_phase_ids),
                    "interrupt_phase_ids": list(self._twin.spec.interrupt_phase_ids),
                    "default_phase_id": self._twin.spec.default_phase_id,
                    "requestable_instruments": requestable_instruments,
                    "phases": [
                        {
                            "id": phase.id,
                            "display_name": phase.display_name,
                            "display_name_ko": phase.display_name_ko,
                        }
                        for phase in self._twin.spec.bundle.phases
                    ],
                    "instruments": self._instrument_metadata_payload(
                        self._twin.spec
                    ),
                    "bundles": self._bundle_metadata_cache,
                },
            },
            sort_keys=True,
        )
        self._simulation_state_pub.publish(simulation)
        self._publish_tool_policy_status()
        self._publish_perception_scene(world)
        self._publish_vlm_context(world)
        # Commit the gate only after the entire public output bundle succeeds.
        self._last_world_emit_signature = self._world_maintenance_signature()
        self._last_world_emit_monotonic = self._monotonic_sec()

    def _publish_tool_policy_status(self, *, force: bool = False) -> None:
        publisher = getattr(self, "_tool_policy_status_pub", None)
        if publisher is None:
            return  # Partial pure-node test fixtures have no ROS publishers.
        payload = tool_policy_status_payload(
            state=self._twin.state,
            instruments=self._twin.instrument_states,
            prepare_probability_threshold=self._ngram_prepare_probability_threshold,
            recovery_probability_threshold=self._ngram_recovery_probability_threshold,
            dwell_sec=self._ngram_policy_stability_sec,
            recovery_enabled_tools=self._ngram_recovery_enabled_tools,
            recovery_dwell=self._ngram_recovery_stability,
        )
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        if force or encoded != getattr(self, "_last_tool_policy_status_json", ""):
            publisher.publish(String(data=encoded))
            self._last_tool_policy_status_json = encoded

    def _stamp_all_bed_robot_arm_groups(self) -> None:
        for belief in self._twin.state.bed_robot_arm_groups.values():
            # Zero means that no controller-owned status has been observed.
            belief.last_update_stamp_sec = 0
            belief.last_update_stamp_nanosec = 0
            belief.last_operation_stamp_sec = 0
            belief.last_operation_stamp_nanosec = 0

    @staticmethod
    def _set_bed_robot_arm_group_stamp(belief, stamp) -> None:
        belief.last_update_stamp_sec = int(stamp.sec)
        belief.last_update_stamp_nanosec = int(stamp.nanosec)

    def _bed_robot_arm_group_state_message(self, payload: dict, stamp) -> BedRobotArmGroupState:
        """Serialize only the public aggregate group contract into ROS state."""

        msg = BedRobotArmGroupState()
        update_sec = int(payload.get("last_update_stamp_sec", 0))
        update_nanosec = int(payload.get("last_update_stamp_nanosec", 0))
        if update_sec or update_nanosec:
            msg.stamp.sec = update_sec
            msg.stamp.nanosec = update_nanosec
        msg.group_id = str(payload.get("group_id", ""))
        msg.connected = bool(payload.get("connected", False))
        msg.state = str(payload.get("state", "unknown"))
        msg.operation = str(payload.get("operation", ""))
        msg.arm_id = str(payload.get("arm_id", ""))
        msg.target_tool_id = str(payload.get("target_tool_id", ""))
        msg.adjustment_mode = str(payload.get("adjustment_mode", ""))
        msg.target_retractor_id = str(payload.get("target_retractor_id", ""))
        msg.direction_frame = str(payload.get("direction_frame", ""))
        msg.direction = str(payload.get("direction", ""))
        msg.axis = str(payload.get("axis", ""))
        msg.distance_mm = float(payload.get("distance_mm", 0.0))
        msg.distance_origin = str(payload.get("distance_origin", ""))
        msg.raw_distance_text = str(payload.get("raw_distance_text", ""))
        msg.end_effector_profile = str(payload.get("end_effector_profile", ""))
        msg.active_request_id = str(payload.get("active_request_id", ""))
        msg.active_command_id = str(payload.get("active_command_id", ""))
        msg.progress = float(payload.get("progress", 0.0))
        msg.error_code = str(payload.get("error_code", ""))
        msg.error_message = str(payload.get("error_message", ""))
        msg.rejection_reason = str(payload.get("rejection_reason", ""))
        return msg

    def _publish_perception_scene(self, world: WorldState) -> None:
        scene = PerceptionScene()
        scene.stamp = world.stamp
        scene.procedure_id = world.procedure_id
        scene.running = bool(world.running)
        scene.execution_state = world.execution_state
        scene.scene_id = f"{world.procedure_id}:{int(world.stamp.sec)}:{int(world.stamp.nanosec)}"
        for instrument in world.instrument_states:
            if not instrument.location_id or not instrument.location_type:
                continue
            scene.visible_tool_ids.append(instrument.instrument_id)
            scene.visible_location_ids.append(instrument.location_id)
            scene.visible_location_types.append(instrument.location_type)
            scene.visible_confidences.append(float(instrument.confidence))
        signal = self._latest_outward_signal
        if signal is not None:
            scene.surgeon_signal_type = signal.signal_type
            scene.surgeon_signal_tool = signal.tool_id
            scene.surgeon_signal_phase = signal.phase_id
            scene.surgeon_hand_pose = signal.hand_pose
            scene.speech_text = signal.speech_text
        scene.active_task_type = world.active_robot_task_type
        scene.active_task_tool_id = world.active_robot_task_tool_id
        scene.active_task_source_anchor = world.active_robot_task_source_anchor
        scene.active_task_target_anchor = world.active_robot_task_target_anchor
        scene.active_task_progress = float(world.active_robot_task_progress)
        scene.scene_summary = (
            f"visible_tools={len(scene.visible_tool_ids)} "
            f"signal={scene.surgeon_signal_type or 'none'} "
            f"active_task={scene.active_task_type or 'none'}"
        )
        self._perception_scene_pub.publish(scene)

    def _compact_json(self, payload: dict) -> str:
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)

    def _event_payload(self, event: SimulationEvent) -> dict:
        detail: dict = {}
        if event.detail:
            try:
                parsed = json.loads(event.detail)
                detail = parsed if isinstance(parsed, dict) else {"detail": parsed}
            except Exception:
                detail = {"detail": event.detail}
        return {
            "stamp": {
                "sec": int(event.stamp.sec),
                "nanosec": int(event.stamp.nanosec),
            },
            "event_type": event.event_type,
            "tool": event.instrument_id,
            "from": event.from_anchor,
            "to": event.to_anchor,
            "arm": event.arm,
            "status": event.status,
            "severity": detail.get("severity", "normal"),
            "reason": detail.get("reason") or detail.get("note") or detail.get("voice_text") or detail.get("text", ""),
            "detail": detail,
        }

    def _important_event_selected(self, event_type: str, detail_json: str) -> bool:
        detail: dict = {}
        if detail_json:
            try:
                parsed = json.loads(detail_json)
                detail = parsed if isinstance(parsed, dict) else {}
            except Exception:
                detail = {}
        severity = str(detail.get("severity", "normal")).lower()
        if severity in {"warning", "error"}:
            return True
        if event_type in self._IMPORTANT_NORMAL_EVENTS:
            return True
        lowered = event_type.lower()
        return any(token in lowered for token in ("handovercompleted", "retriev", "cleaningcompleted", "returnedtotray"))

    def _record_important_event(self, event: SimulationEvent) -> None:
        if not self._important_event_selected(event.event_type, event.detail):
            return
        self._important_events.append(event)
        self._important_event_pub.publish(event)

    def _append_tool_history(
        self,
        history_name: str,
        tool_id: str,
        stamp,
    ) -> None:
        resolved = self._twin.spec.resolve_instrument_alias(str(tool_id)) or str(
            tool_id
        )
        history = getattr(self, history_name, None)
        if not resolved or history is None:
            return
        at = self._stamp_sec(stamp)
        if history:
            previous = history[-1]
            if (
                str(previous.get("tool", "")) == resolved
                and abs(float(previous.get("at", 0.0)) - at) < 0.05
            ):
                return
        history.append({"tool": resolved, "at": at})

    def _clear_tool_histories(self) -> None:
        request_history = getattr(self, "_validated_tool_request_history", None)
        if request_history is not None:
            request_history.clear()
        handover_history = getattr(self, "_completed_handover_history", None)
        if handover_history is not None:
            handover_history.clear()
        completed_by_phase = getattr(
            self,
            "_completed_handover_tools_by_phase",
            None,
        )
        if completed_by_phase is not None:
            completed_by_phase.clear()

    def _record_completed_handover_phase(self, tool_id: str) -> None:
        resolved = self._twin.spec.resolve_instrument_alias(str(tool_id)) or str(
            tool_id
        )
        phase_id = str(self._twin._active_context_phase_id() or "").strip()
        if not resolved or not phase_id:
            return
        completed_by_phase = getattr(
            self,
            "_completed_handover_tools_by_phase",
            None,
        )
        if completed_by_phase is None:
            completed_by_phase = {}
            self._completed_handover_tools_by_phase = completed_by_phase
        completed_by_phase.setdefault(phase_id, set()).add(resolved)

    def _context_tool_rows(self, world: WorldState) -> tuple[list[str], list[str], list[dict]]:
        active_tool_ids: list[str] = []
        non_home_tool_ids: list[str] = []
        tool_rows: list[dict] = []
        expected = set(world.expected_instruments)
        pending = set(world.pending_transition_tools)
        highlighted = {
            world.right_hand_tool,
            world.left_hand_tool,
            world.prepositioned_tool,
            world.surgeon_request_tool,
            world.explicit_request_tool,
        }
        for instrument in world.instrument_states:
            at_home = (
                instrument.location_id == instrument.home_location_id
                and instrument.location_type == instrument.home_location_type
                and instrument.lifecycle_stage in {"home_rack", "returned_home"}
            )
            if not at_home:
                non_home_tool_ids.append(instrument.instrument_id)
            if at_home and instrument.instrument_id not in expected and instrument.instrument_id not in pending:
                continue
            if instrument.instrument_id in highlighted or instrument.instrument_id in expected or instrument.instrument_id in pending or not at_home:
                active_tool_ids.append(instrument.instrument_id)
                tool_rows.append(
                    {
                        "id": instrument.instrument_id,
                        "lifecycle": instrument.lifecycle_stage,
                        "location_id": instrument.location_id,
                        "location_type": instrument.location_type,
                        "owner": instrument.owner,
                        "next": instrument.next_required_transition,
                        "contaminated": bool(instrument.contaminated),
                    }
                )
        return active_tool_ids, non_home_tool_ids, tool_rows

    def _publish_vlm_context(self, world: WorldState) -> None:
        active_tool_ids, non_home_tool_ids, tool_rows = self._context_tool_rows(world)
        recent_events = list(self._important_events)[-self._vlm_recent_event_count :]
        recent_payload = [self._event_payload(event) for event in recent_events]
        bed_group_rows = [
            {
                "group_id": group.group_id,
                "connected": bool(group.connected),
                "state": group.state,
                "operation": group.operation,
                "direction": group.direction,
                "distance_mm": round(float(group.distance_mm), 3),
                "distance_origin": group.distance_origin,
                "raw_distance_text": group.raw_distance_text,
                "end_effector_profile": group.end_effector_profile,
                "active_request_id": group.active_request_id,
                "active_command_id": group.active_command_id,
                "progress": round(float(group.progress), 3),
                "error_code": group.error_code,
                "error_message": group.error_message,
                "rejection_reason": group.rejection_reason,
            }
            for group in world.bed_robot_arm_groups
        ]
        pending_group_request = (
            list(self._pending_bed_robot_arm_group_requests.values())[-1]
            if self._pending_bed_robot_arm_group_requests
            else None
        )
        active_task = {
            "id": world.active_robot_task_id,
            "type": world.active_robot_task_type,
            "tool": world.active_robot_task_tool_id,
            "arm": world.active_robot_task_arm,
            "source": world.active_robot_task_source_anchor,
            "target": world.active_robot_task_target_anchor,
            "progress": round(float(world.active_robot_task_progress), 3),
            "remaining_sec": round(float(world.active_robot_task_remaining_sec), 2),
        }
        summary_payload = {
            "procedure": world.procedure_id,
            "execution_state": world.execution_state,
            "running": bool(world.running),
            "phase": {
                "id": world.filtered_phase,
                "confidence": round(float(world.phase_confidence), 3),
                "uncertain": bool(world.phase_uncertain),
                "stability": round(float(world.phase_stability), 3),
            },
            "request": {
                "explicit_tool": world.explicit_request_tool,
                "surgeon_tool": world.surgeon_request_tool,
                "intent": world.surgeon_intent,
                "handover_ready": bool(world.surgeon_ready_for_handover),
                "retrieval_ready": bool(world.surgeon_ready_for_retrieval),
            },
            "hands": {
                "right": world.right_hand_tool,
                "left": world.left_hand_tool,
                "prepositioned": world.prepositioned_tool,
            },
            "cleaner": {
                "busy": bool(world.cleaner_busy),
                "remaining_sec": round(float(world.cleaner_remaining_sec), 2),
            },
            "active_robot_task": active_task,
            "bed_robot_arm_groups": bed_group_rows,
            "pending_bed_robot_arm_group_request": (
                {
                    "request_id": pending_group_request.request_id,
                    "group_id": pending_group_request.group_id,
                    "operation": pending_group_request.operation,
                    "voice_text": pending_group_request.voice_text,
                    "procedure_id": pending_group_request.procedure_id,
                    "phase_id": pending_group_request.phase_id,
                    "end_effector_profile": pending_group_request.end_effector_profile,
                    "source": pending_group_request.source,
                }
                if pending_group_request is not None
                else None
            ),
            "expected_tools": list(world.expected_instruments),
            "pending_transition_tools": list(world.pending_transition_tools),
            "active_recovery_tools": list(world.active_recovery_tools),
            "non_home_tools": non_home_tool_ids,
            "tools": tool_rows,
            "recent_important_events": recent_payload,
        }

        summary_msg = String()
        summary_msg.data = self._compact_json(summary_payload)
        self._vlm_context_summary_pub.publish(summary_msg)

        request_context = VLMRequestContext()
        request_context.stamp = world.stamp
        request_context.procedure_id = world.procedure_id
        request_context.filtered_phase = world.filtered_phase
        request_context.phase_confidence = float(world.phase_confidence)
        request_context.phase_uncertain = bool(world.phase_uncertain)
        request_context.explicit_request_tool = world.explicit_request_tool
        request_context.surgeon_request_tool = world.surgeon_request_tool
        request_context.surgeon_intent = world.surgeon_intent
        request_context.right_hand_tool = world.right_hand_tool
        request_context.left_hand_tool = world.left_hand_tool
        request_context.prepositioned_tool = world.prepositioned_tool
        request_context.cleaner_busy = bool(world.cleaner_busy)
        request_context.cleaner_remaining_sec = float(world.cleaner_remaining_sec)
        request_context.phase_expected_tools = list(world.expected_instruments)
        request_context.active_tool_ids = active_tool_ids
        request_context.non_home_tool_ids = non_home_tool_ids
        request_context.pending_transition_tools = list(world.pending_transition_tools)
        request_context.bed_robot_arm_groups = list(world.bed_robot_arm_groups)
        request_context.has_pending_bed_robot_arm_group_request = pending_group_request is not None
        if pending_group_request is not None:
            request_context.pending_bed_robot_arm_group_request = pending_group_request
        for event in recent_events:
            digest = EventDigest()
            digest.stamp = event.stamp
            digest.event_type = event.event_type
            digest.instrument_id = event.instrument_id
            digest.anchor_id = event.to_anchor or event.from_anchor
            payload = self._event_payload(event)
            digest.reason = str(payload.get("reason", ""))
            digest.detail = event.detail
            request_context.recent_events.append(digest)
        request_context.bt_snapshot = BTContextSnapshot()
        request_context.compact_json = summary_msg.data
        self._vlm_request_context_pub.publish(request_context)

    def _publish_event(self, event_type: str, **kwargs) -> None:
        detail_context = dict(kwargs)
        raw_detail = detail_context.pop("detail", {})
        detail = self._augment_event_detail(event_type, raw_detail, **detail_context)
        event = TwinEvent()
        event.stamp = self._stamp()
        event.procedure_run_id = str(self._twin.state.procedure_run_id or "")
        event.event_type = event_type
        event.instrument_id = kwargs.get("instrument_id", "")
        event.instance_id = kwargs.get("instance_id", "")
        event.phase_id = kwargs.get("phase_id", "")
        event.location_id = kwargs.get("location_id", "")
        event.location_type = kwargs.get("location_type", "")
        event.owner = kwargs.get("owner", "")
        event.status = kwargs.get("status", "")
        event.confidence = float(kwargs.get("confidence", 0.0))
        event.detail_json = json.dumps(detail, sort_keys=True)
        event.arm = kwargs.get("arm", "")
        event.source_location_id = kwargs.get("source_location_id", "")
        event.source_location_type = kwargs.get("source_location_type", "")
        event.target_location_id = kwargs.get("target_location_id", "")
        event.target_location_type = kwargs.get("target_location_type", "")
        event.target_owner = kwargs.get("target_owner", "")
        event.cleaning_required = bool(kwargs.get("cleaning_required", False))
        event.mode = kwargs.get("mode", "")
        self._event_pub.publish(event)

        simulation_event = SimulationEvent()
        simulation_event.stamp = event.stamp
        simulation_event.procedure_run_id = event.procedure_run_id
        simulation_event.event_type = event.event_type
        simulation_event.instrument_id = event.instrument_id
        simulation_event.from_anchor = event.source_location_id or event.location_id
        simulation_event.to_anchor = event.target_location_id or event.location_id
        simulation_event.arm = event.arm
        simulation_event.status = event.status
        simulation_event.detail = event.detail_json
        self._simulation_event_pub.publish(simulation_event)
        self._record_important_event(simulation_event)

    def _on_surgeon_actor_event(self, msg: SurgeonActorEvent) -> None:
        if not self._accept_validation_actor_events:
            return
        self._publish_outward_signal(msg)
        self._twin.apply_surgeon_actor_event(msg)
        queue_detail = self._twin.request_queue_summary()
        self._publish_event(
            "SurgeonActorEventObserved",
            instrument_id=msg.tool_id,
            phase_id=msg.phase_id,
            detail={
                "event_type": msg.event_type,
                "voice_text": msg.voice_text,
                "note": msg.note,
                "override": bool(msg.override),
                "ready_for_handover": bool(msg.ready_for_handover),
                "ready_for_retrieval": bool(msg.ready_for_retrieval),
                **queue_detail,
            },
            mode=msg.event_type,
        )
        self._publish_world_state()

    def _publish_phase_decision_outputs(self, decision: dict, *, input_type: str, input_source: str) -> None:
        if not decision:
            return
        event_type = str(decision.get("event_type", "PhaseTransitionRejected"))
        target_phase = str(decision.get("target_phase", ""))
        accepted = bool(decision.get("accepted", False))
        reason = str(decision.get("reason", ""))
        cue_id = str(decision.get("cue_id", ""))
        self._publish_reducer_decision_event(
            input_type=input_type,
            input_id=cue_id or target_phase,
            input_source=input_source,
            accepted=accepted,
            reason=reason,
            affected_phase=target_phase,
            detail=decision,
        )
        self._publish_event(
            event_type,
            phase_id=target_phase,
            confidence=float(decision.get("confidence", 0.0)),
            detail=decision,
            mode=input_type,
        )
        if accepted and event_type == "PhaseTransitionAccepted" and target_phase:
            self._phase_entered_ros_sec = self._stamp_sec(self._stamp())

    def _on_phase_transition_cue(self, msg: PhaseTransitionCue) -> None:
        decision = self._twin.apply_phase_transition_cue(msg)
        self._publish_phase_decision_outputs(
            decision,
            input_type="phase_transition_cue",
            input_source=msg.source or "unknown",
        )
        self._publish_world_state()

    def _on_phase_evidence(self, msg: PhaseEvidence) -> None:
        source = str(msg.source or "mock_vlm")
        if self._perception_gate_active():
            self._reject_visual_evidence(
                channel="phase",
                source=source,
                reason="vlm_source_not_ready",
                message=msg,
            )
            return
        if not self._admit_visual_evidence(
            msg,
            channel="phase",
            source=source,
            require_epoch=(
                str(getattr(self, "_vlm_mode", "mock")) in {"real", "dual"}
                and "real_vlm" in source
            ),
        ):
            return
        fused = self._fuse_phase_evidence(msg)
        decisions = self._twin.apply_phase_evidence(fused)
        for decision in decisions:
            self._publish_phase_decision_outputs(
                decision,
                input_type="phase_evidence",
                input_source=fused.source or msg.source or "mock_vlm",
            )
        self._publish_world_state()

    def _runtime_prior_evidence(self) -> dict:
        hand_tools = [
            tool
            for tool in [
                self._twin.state.right_hand_tool,
                self._twin.state.left_hand_tool,
                self._twin.state.prepositioned_tool,
            ]
            if tool
        ]
        hand_tools.extend(
            state.instrument_id
            for state in self._twin.instrument_states.values()
            if state.lifecycle_stage == "surgeon_owned"
        )
        mayo_tools = [
            state.instrument_id
            for state in self._twin.instrument_states.values()
            if state.lifecycle_stage in {"mayo_reuse", "mayo_recovery"}
        ]
        events = [
            {
                "t": event.event_type,
                "tool": event.instrument_id,
                "anchor": event.to_anchor or event.from_anchor,
                "stamp_sec": self._stamp_sec(event.stamp),
            }
            for event in self._important_events
            if event.event_type
            in {
                "ToolHandoverCompleted",
                "ToolReceivedFromSurgeon",
                "ToolSentToCleaner",
                "ToolCleaningCompleted",
                "ToolReturnedToTray",
                "RobotTaskCompleted",
            }
        ]
        completed_handovers = list(
            getattr(self, "_completed_handover_history", [])
        )
        tool_requests = list(
            getattr(self, "_validated_tool_request_history", [])
        )
        return {
            "current_phase": self._twin._active_context_phase_id(),
            "phase_entered_sec": self._phase_entered_ros_sec,
            "recent_tools": completed_handovers,
            "completed_handovers": completed_handovers,
            "tool_requests": tool_requests,
            "mayo_tools": mayo_tools,
            "hand_tools": hand_tools,
            "events": events,
        }

    def _fuse_phase_evidence(self, msg: PhaseEvidence) -> PhaseEvidence:
        if "real_vlm" not in str(msg.source):
            return msg
        if getattr(self._twin, "phase_bootstrap_open", False):
            vlm_scores = {
                self._twin.spec.resolve_phase_id(str(phase_id))
                or str(phase_id): float(confidence)
                for phase_id, confidence in zip(
                    msg.phase_ids,
                    msg.phase_confidences,
                )
                if str(phase_id)
            }
            self._publish_reducer_decision_event(
                input_type="vlm_phase_fusion",
                input_id=f"phase_bootstrap:{self._stamp_sec(msg.stamp):.3f}",
                input_source=msg.source,
                accepted=bool(vlm_scores),
                reason="open_set_phase_bootstrap_vlm_only",
                affected_phase=(
                    max(vlm_scores, key=vlm_scores.get)
                    if vlm_scores
                    else ""
                ),
                detail={
                    "vlm": vlm_scores,
                    "prior": {},
                    "fused": "vlm_only",
                    "ground_truth_used": False,
                },
            )
            return msg
        prior = self._prior_scorer.score(self._runtime_prior_evidence()).get("phase", [])
        prior_scores = {str(item[0]): float(item[1]) for item in prior if isinstance(item, list) and len(item) == 2}
        vlm_scores = {
            self._twin.spec.resolve_phase_id(str(phase_id)) or str(phase_id): float(confidence)
            for phase_id, confidence in zip(msg.phase_ids, msg.phase_confidences)
            if str(phase_id)
        }
        if not prior_scores or not vlm_scores:
            return msg
        current_phase = self._twin.state.filtered_phase or self._twin.spec.default_phase_id
        switch_threshold = float(
            self._twin.spec.bundle.phase_guard.min_confidence_to_switch
        )
        normal_phase_ids = self._twin.spec.normal_phase_ids
        current_index = (
            normal_phase_ids.index(current_phase)
            if current_phase in normal_phase_ids
            else -1
        )
        candidates = set(prior_scores) | set(vlm_scores) | {current_phase}
        fused_scores: dict[str, float] = {}
        for phase_id in candidates:
            vlm_score = float(vlm_scores.get(phase_id, 0.0))
            prior_score = float(prior_scores.get(phase_id, 0.0))
            agreement = 0.08 if vlm_score >= 0.35 and prior_score >= 0.35 else 0.0
            fused_score = min(
                1.0,
                0.68 * vlm_score + 0.34 * prior_score + agreement,
            )
            if (
                phase_id != current_phase
                and self._twin.spec.is_normal_phase(phase_id)
                and normal_phase_ids.index(phase_id) > current_index
                and vlm_score >= switch_threshold
            ):
                # A procedure prior should smooth ambiguous observations, not
                # veto sustained high-confidence monotonic evidence. The twin
                # still enforces adjacency, dwell, and any explicit transition
                # interaction requirements.
                fused_score = max(fused_score, vlm_score)
            fused_scores[phase_id] = fused_score
        ranked = sorted(fused_scores.items(), key=lambda item: item[1], reverse=True)[:4]
        fused = PhaseEvidence()
        fused.stamp = msg.stamp
        fused.source = f"{msg.source}:fusion"
        fused.phase_ids = [item[0] for item in ranked]
        fused.phase_confidences = [float(item[1]) for item in ranked]
        fused.visible_instrument_ids = list(msg.visible_instrument_ids)
        fused.visible_instrument_confidences = list(msg.visible_instrument_confidences)
        fused.uncertainty = min(1.0, max(0.0, float(msg.uncertainty) + (0.15 if ranked and ranked[0][0] != current_phase and ranked[0][1] < 0.8 else 0.0)))
        fused.scene_summary = f"{msg.scene_summary}; phase_fusion={ranked[:2]}"
        self._publish_reducer_decision_event(
            input_type="vlm_phase_fusion",
            input_id=f"phase_fusion:{self._stamp_sec(msg.stamp):.3f}",
            input_source=msg.source,
            accepted=bool(ranked),
            reason="phase_prior_fused",
            affected_phase=ranked[0][0] if ranked else "",
            detail={
                "vlm": vlm_scores,
                "prior": prior_scores,
                "fused": ranked,
            },
        )
        return fused

    def _publish_vlm_reducer_decision(self, result: dict) -> None:
        decision = VLMReducerDecision()
        decision.stamp = self._stamp()
        decision.source = str(result.get("source", "legacy_tool_observation"))
        decision.proposal_id = str(result.get("proposal_id", ""))
        decision.instrument_id = str(result.get("instrument_id", ""))
        decision.proposed_transition = str(result.get("proposed_transition", ""))
        decision.reducer_result = str(result.get("reducer_result", "ignored"))
        decision.reducer_reason = str(result.get("reducer_reason", ""))
        decision.accepted = bool(result.get("accepted", False))
        decision.confidence = float(result.get("confidence", 0.0))
        decision.detail_json = json.dumps(result, sort_keys=True)
        self._vlm_reducer_pub.publish(decision)

    def _publish_reducer_decision_event(
        self,
        *,
        input_type: str,
        input_id: str,
        input_source: str,
        accepted: bool,
        reason: str,
        affected_tool: str = "",
        affected_phase: str = "",
        detail: dict | None = None,
    ) -> None:
        event = ReducerDecisionEvent()
        event.stamp = self._stamp()
        event.input_type = input_type
        event.input_id = input_id
        event.input_source = input_source
        event.accepted = bool(accepted)
        event.reason = reason
        event.affected_tool = affected_tool
        event.affected_phase = affected_phase
        event.detail_json = json.dumps(detail or {}, sort_keys=True)
        self._reducer_decision_pub.publish(event)

    def _outward_hand_pose(self, event_type: str) -> str:
        if event_type in {"request_tool", "extend_hand_for_handover"}:
            return "open_receive"
        if event_type in {"return_tool", "extend_hand_for_retrieval", "place_on_mayo_recovery"}:
            return "present_return"
        if event_type in {"place_on_mayo", "place_on_mayo_reuse"}:
            return "park_on_mayo_reuse"
        if event_type == "continue_using":
            return "using_tool"
        if event_type in {"advance_phase", "advance_phase_cue"}:
            return "phase_transition_signal"
        if event_type in {"request_procedure_completion", "complete_procedure"}:
            return "completion_signal"
        return ""

    def _publish_outward_signal(self, msg: SurgeonActorEvent) -> None:
        signal = SurgeonOutwardSignal()
        signal.stamp = msg.stamp if msg.stamp.sec or msg.stamp.nanosec else self._stamp()
        signal.procedure_id = self._twin.state.procedure_id
        signal.signal_id = (
            f"surgeon:{msg.event_type}:{msg.tool_id}:{msg.phase_id}:"
            f"{int(signal.stamp.sec)}.{int(signal.stamp.nanosec)}"
        )
        signal.signal_type = msg.event_type
        signal.tool_id = msg.tool_id
        signal.phase_id = msg.phase_id
        signal.hand_pose = self._outward_hand_pose(msg.event_type)
        signal.speech_text = msg.voice_text
        signal.confidence = 0.96 if not msg.override else 1.0
        signal.note = msg.note
        self._latest_outward_signal = signal
        self._outward_signal_pub.publish(signal)

    def _publish_vlm_inference_proposal(
        self,
        msg: ToolObservation,
        *,
        proposal_id: str,
        current_lifecycle: str,
        proposed_lifecycle: str,
        source: str = "legacy_tool_observation",
        detail: dict | None = None,
    ) -> None:
        proposal = VLMInferenceProposal()
        proposal.stamp = self._stamp()
        proposal.source = source
        proposal.proposal_id = proposal_id
        proposal.instrument_id = msg.instrument_id
        proposal.current_lifecycle = current_lifecycle
        proposal.proposed_lifecycle = proposed_lifecycle
        proposal.proposed_transition = f"{current_lifecycle}->{proposed_lifecycle}"
        proposal.location_type = msg.location_type
        proposal.location_id = msg.location_id
        proposal.confidence = float(msg.confidence)
        proposal.visible = bool(msg.visible)
        detail_payload = {
                "legacy_message": "ToolObservation",
                "instrument_id": msg.instrument_id,
                "location_type": msg.location_type,
                "location_id": msg.location_id,
                "confidence": float(msg.confidence),
                "visible": bool(msg.visible),
        }
        if detail:
            detail_payload.update(detail)
        proposal.detail_json = json.dumps(detail_payload, sort_keys=True)
        self._vlm_proposal_pub.publish(proposal)

    def _stamp_sec(self, msg_stamp) -> float:
        value = float(msg_stamp.sec) + float(msg_stamp.nanosec) / 1_000_000_000.0
        if value > 0.0:
            return value
        stamp = self._stamp()
        return float(stamp.sec) + float(stamp.nanosec) / 1_000_000_000.0

    def _configured_completed_repeat_handover_exclusions(
        self,
        phase_id: str = "",
    ) -> frozenset[str]:
        """Return phase-configured tools already handed over in this run."""

        resolved_phase = str(
            phase_id or self._twin._active_context_phase_id() or ""
        ).strip()
        configured = (
            self._twin.spec.get_scenario_policy()
            .automatic_repeat_handover_exclusions(resolved_phase)
        )
        completed_in_phase = set(
            getattr(self, "_completed_handover_tools_by_phase", {}).get(
                resolved_phase,
                set(),
            )
        )
        return frozenset(configured & completed_in_phase)

    def _automatic_repeat_handover_exclusions(
        self,
        phase_id: str = "",
    ) -> frozenset[str]:
        """Return completed Mayo tools excluded from automatic repeat use."""

        configured_completed = (
            self._configured_completed_repeat_handover_exclusions(phase_id)
        )
        mayo_reuse_tools = {
            state.instrument_id
            for state in self._twin.instrument_states.values()
            if str(getattr(state, "lifecycle_stage", "")) == "mayo_reuse"
            and str(getattr(state, "location_type", "")) == "mayo_stand"
            and str(getattr(state, "location_id", "")) == "mayo_stand"
        }
        return frozenset(configured_completed & mayo_reuse_tools)

    def _repeat_handover_requires_explicit_voice(
        self,
        tool_id: str,
        phase_id: str = "",
    ) -> bool:
        """Keep a completed Mayo repeat voice-only after robot pickup."""

        normalized_tool_id = str(tool_id or "").strip()
        if normalized_tool_id not in (
            self._configured_completed_repeat_handover_exclusions(phase_id)
        ):
            return False
        if normalized_tool_id in self._automatic_repeat_handover_exclusions(
            phase_id
        ):
            return True

        right_hand_instance_id = str(
            getattr(
                self._twin.state,
                "right_hand_tool_instance_id",
                "",
            )
            or ""
        ).strip()
        right_hand_state = self._twin.instrument_states.get(
            right_hand_instance_id
        )
        if (
            right_hand_state is None
            or str(getattr(right_hand_state, "instrument_id", ""))
            != normalized_tool_id
            or str(getattr(right_hand_state, "lifecycle_stage", ""))
            != "prepositioned_right"
        ):
            return False
        origin_lifecycle = str(
            getattr(
                right_hand_state,
                "preposition_origin_lifecycle_stage",
                "",
            )
            or ""
        ).strip()
        origin_location_type = str(
            getattr(
                right_hand_state,
                "preposition_origin_location_type",
                "",
            )
            or ""
        ).strip().casefold()
        origin_location_id = str(
            getattr(
                right_hand_state,
                "preposition_origin_location_id",
                "",
            )
            or ""
        ).strip().casefold()
        return bool(
            origin_lifecycle in {"mayo_reuse", "mayo_recovery"}
            or "mayo" in origin_location_type
            or "mayo" in origin_location_id
        )

    def _ngram_tool_prediction(self) -> tuple[str, float, dict]:
        """Return the DT-owned 0704 n-gram prediction and raw probabilities.

        VLM ``tool`` rows, procedure-path forecasts, and authored phase-role
        expectations deliberately do not enter this calculation.  The raw
        per-tool probabilities remain visible even when a tool is unavailable
        or excluded from automatic repeat handover; the rank-one scalar used
        for a direct hand fallback is selected only from eligible, physically
        handover-capable inventory.
        """

        requestable_ids = list(
            self._twin.spec.list_requestable_instrument_ids()
        )
        runtime_evidence = self._runtime_prior_evidence()
        ngram_prior = getattr(self, "_handover_ngram_prior", None)
        ngram_result = (
            ngram_prior.predict(
                phase_id=runtime_evidence.get("current_phase", ""),
                completed_handovers=runtime_evidence.get(
                    "completed_handovers", []
                ),
            )
            if ngram_prior is not None
            else None
        )
        scores = {tool_id: 0.0 for tool_id in requestable_ids}
        if not isinstance(ngram_result, dict):
            return "", 0.0, {
                "ngram": scores,
                "ranked_distribution": [],
                "eligible_candidates": [],
                "candidate_lifecycles": {},
                "automatic_repeat_handover_exclusions": [],
                "match": "",
                "support": 0,
                "has_ngram_evidence": False,
            }

        for item in ngram_result.get("candidates", []):
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                continue
            tool_id = (
                self._twin.spec.resolve_instrument_alias(str(item[0]))
                or str(item[0])
            )
            if tool_id not in scores:
                continue
            try:
                probability = float(item[1])
            except (TypeError, ValueError):
                continue
            if math.isfinite(probability):
                scores[tool_id] = max(0.0, min(1.0, probability))

        automatic_repeat_exclusions = self._automatic_repeat_handover_exclusions(
            str(runtime_evidence.get("current_phase", "") or "")
        )

        handover_lifecycles = {
            "home_rack",
            "returned_home",
            "mayo_reuse",
            "prepositioned_right",
        }
        candidate_lifecycles = {
            tool_id: sorted(
                {
                    str(getattr(state, "lifecycle_stage", "") or "")
                    for state in self._twin._instances_for_type(tool_id)
                }
            )
            for tool_id in requestable_ids
        }
        eligible_ids = [
            tool_id
            for tool_id in requestable_ids
            if tool_id not in automatic_repeat_exclusions
            if any(
                str(getattr(state, "lifecycle_stage", ""))
                in handover_lifecycles
                for state in self._twin._instances_for_type(tool_id)
            )
        ]
        # ``ngram`` remains the immutable raw statistical evidence used by
        # recovery.  The public ranked projection is an automatic-action
        # candidate list, so a voice-only repeat must not remain rank one and
        # disagree with the selected scalar prediction.
        ranked_distribution = sorted(
            (
                (tool_id, score)
                for tool_id, score in scores.items()
                if tool_id not in automatic_repeat_exclusions
            ),
            key=lambda item: (-item[1], requestable_ids.index(item[0])),
        )
        if not eligible_ids:
            return "", 0.0, {
                "ngram": scores,
                "ranked_distribution": ranked_distribution,
                "eligible_candidates": eligible_ids,
                "candidate_lifecycles": candidate_lifecycles,
                "automatic_repeat_handover_exclusions": sorted(
                    automatic_repeat_exclusions
                ),
                "match": str(ngram_result.get("match", "")),
                "support": int(ngram_result.get("support", 0) or 0),
                "has_ngram_evidence": True,
            }
        selected_tool, selected_probability = max(
            ((tool_id, scores[tool_id]) for tool_id in eligible_ids),
            key=lambda item: (item[1], -eligible_ids.index(item[0])),
        )
        if selected_probability <= 0.0:
            selected_tool = ""
        return selected_tool, selected_probability, {
            "ngram": scores,
            "ranked_distribution": ranked_distribution,
            "eligible_candidates": eligible_ids,
            "candidate_lifecycles": candidate_lifecycles,
            "automatic_repeat_handover_exclusions": sorted(
                automatic_repeat_exclusions
            ),
            "match": str(ngram_result.get("match", "")),
            "support": int(ngram_result.get("support", 0) or 0),
            "has_ngram_evidence": True,
        }

    @staticmethod
    def _update_ngram_probability_dwell(
        tracker: dict[str, dict],
        *,
        key: str,
        probability: float,
        condition: bool,
        now_sec: float,
    ) -> float:
        """Track continuous satisfaction of an n-gram probability predicate."""

        if not condition:
            tracker.pop(key, None)
            return 0.0
        entry = tracker.get(key)
        if entry is None:
            entry = {"first_seen": now_sec, "last_seen": now_sec}
            tracker[key] = entry
        else:
            entry["last_seen"] = max(
                now_sec, float(entry.get("last_seen", now_sec))
            )
        entry["probability"] = probability
        return max(0.0, now_sec - float(entry["first_seen"]))

    def _refresh_ngram_mayo_recovery(
        self,
        *,
        scores: dict[str, float],
        has_ngram_evidence: bool,
        preparation_tool_id: str,
        now_sec: float,
    ) -> bool:
        """Queue at most one DT-arbitrated low-probability Mayo recovery.

        Completion cleanup remains a terminal lifecycle obligation, while a
        running procedure uses only the per-tool 0704 n-gram probability.
        Neither path consults ``future_use_expected`` nor a VLM Mayo vote.
        A simultaneously ready preparation wins before a recovery transaction
        is opened, so the BT never receives two competing autonomous intents.
        """

        state = self._twin.state
        execution_state = str(state.execution_state)

        if execution_state == "finishing":
            # Voice completion has its own frozen instance snapshot.  Do not
            # reuse the running n-gram allowlist here: that would either omit
            # a legitimate Mayo target or start recovering Bovie/bipolar after
            # the user explicitly excluded them from terminal cleanup.
            self._ngram_recovery_stability.clear()
            candidates = sorted(
                (
                    tool_state
                    for instance_id in self._twin.completion_recovery_target_instances
                    if (
                        tool_state := self._twin.get_instrument_state(instance_id)
                    )
                    is not None
                    and str(getattr(tool_state, "lifecycle_stage", ""))
                    in {"mayo_reuse", "mayo_recovery"}
                    and str(getattr(tool_state, "location_type", ""))
                    == "mayo_stand"
                    and str(getattr(tool_state, "location_id", ""))
                    == "mayo_stand"
                ),
                key=lambda tool_state: (
                    float(getattr(tool_state, "last_update_sec", 0.0)),
                    tool_state.instance_id,
                ),
            )
            for candidate in candidates:
                if self._twin.queue_autonomous_mayo_recovery(
                    candidate.instance_id,
                    reason="completion_cleanup",
                ):
                    return True
            return False

        mayo_states = [
            tool_state
            for tool_state in self._twin.instrument_states.values()
            if tool_state.instrument_id
            in self._ngram_recovery_enabled_tools
            and str(getattr(tool_state, "lifecycle_stage", ""))
            == "mayo_reuse"
            and str(getattr(tool_state, "location_type", "")) == "mayo_stand"
            and str(getattr(tool_state, "location_id", "")) == "mayo_stand"
        ]
        valid_instance_ids = {tool_state.instance_id for tool_state in mayo_states}
        for instance_id in list(self._ngram_recovery_stability):
            if instance_id not in valid_instance_ids:
                self._ngram_recovery_stability.pop(instance_id, None)

        if (
            execution_state != "running"
            or not bool(state.running)
            or not has_ngram_evidence
        ):
            # Paused/idle wall time is not procedure evidence.  Otherwise a
            # recovery candidate becomes instantly ready on resume/start.
            self._ngram_recovery_stability.clear()
            return False

        ready: list[tuple[float, float, str]] = []
        for candidate in mayo_states:
            probability = float(scores.get(candidate.instrument_id, 0.0))
            dwell_sec = self._update_ngram_probability_dwell(
                self._ngram_recovery_stability,
                key=candidate.instance_id,
                probability=probability,
                condition=(
                    probability
                    <= self._ngram_recovery_probability_threshold
                ),
                now_sec=now_sec,
            )
            if dwell_sec >= self._ngram_policy_stability_sec:
                ready.append(
                    (
                        probability,
                        float(getattr(candidate, "last_update_sec", 0.0)),
                        candidate.instance_id,
                    )
                )
        selected_recovery = sorted(ready)[0] if ready else None
        intent = arbitrate_ngram_intent(
            preparation_tool_id=preparation_tool_id,
            recovery_tool_id=(
                selected_recovery[2] if selected_recovery is not None else ""
            ),
            preparation_probability=(
                float(scores.get(preparation_tool_id, 0.0))
                if preparation_tool_id
                else 0.0
            ),
            recovery_probability=(
                selected_recovery[0]
                if selected_recovery is not None
                else 1.0
            ),
        )
        if intent.action != "recover" or selected_recovery is None:
            return False

        probability, _last_update, instance_id = selected_recovery
        if intent.tool_id == instance_id:
            if self._twin.queue_autonomous_mayo_recovery(
                instance_id,
                reason=(
                    "ngram_probability_"
                    f"{probability:.3f}_le_"
                    f"{self._ngram_recovery_probability_threshold:.3f}"
                ),
            ):
                return True
        return False

    def _refresh_ngram_tool_policy(self, *, now_sec: float) -> bool:
        """Project immutable 0704 n-gram evidence into the DT world state."""

        state = self._twin.state
        if not (
            bool(state.running)
            and str(state.execution_state).strip().lower() == "running"
            and str(state.procedure_run_id).strip()
        ):
            return self._clear_inactive_tool_policy_state()
        before = (
            str(getattr(state, "predicted_tool", "")),
            float(getattr(state, "predicted_tool_confidence", 0.0)),
            float(getattr(state, "predicted_tool_stability_sec", 0.0)),
            bool(getattr(state, "autonomous_preparation_ready", False)),
            tuple(
                (
                    row.rank,
                    row.instrument_id,
                    row.confidence,
                    row.stability_sec,
                )
                for row in getattr(state, "ranked_tool_predictions", [])
            ),
            tuple(state.active_recovery_tool_instances),
        )
        tool_id, probability, detail = self._ngram_tool_prediction()
        for tracked_tool in list(self._ngram_preparation_stability):
            if tracked_tool != tool_id:
                self._ngram_preparation_stability.pop(tracked_tool, None)
        stability_sec = self._update_ngram_probability_dwell(
            self._ngram_preparation_stability,
            key=tool_id,
            probability=probability,
            condition=bool(
                bool(state.running)
                and str(state.execution_state) == "running"
                and tool_id
                and probability >= self._ngram_prepare_probability_threshold
            ),
            now_sec=now_sec,
        ) if tool_id else 0.0

        state.predicted_tool = tool_id
        state.predicted_tool_confidence = probability if tool_id else 0.0
        state.predicted_tool_stability_sec = stability_sec
        state.autonomous_preparation_ready = bool(
            bool(state.running)
            and str(state.execution_state) == "running"
            and tool_id
            and probability >= self._ngram_prepare_probability_threshold
            and stability_sec >= self._ngram_policy_stability_sec
            and not state.active_recovery_tool_instances
        )
        state.ranked_tool_predictions = [
            RankedToolPredictionBelief(
                rank=rank,
                instrument_id=candidate_id,
                confidence=candidate_probability,
                stability_sec=(
                    stability_sec if candidate_id == tool_id else 0.0
                ),
            )
            for rank, (candidate_id, candidate_probability) in enumerate(
                detail.get("ranked_distribution", []), start=1
            )
        ]
        recovery_changed = self._refresh_ngram_mayo_recovery(
            scores=detail.get("ngram", {}),
            has_ngram_evidence=bool(detail.get("has_ngram_evidence", False)),
            preparation_tool_id=(
                tool_id if state.autonomous_preparation_ready else ""
            ),
            now_sec=now_sec,
        )
        if recovery_changed:
            state.autonomous_preparation_ready = False
        after = (
            str(state.predicted_tool),
            float(state.predicted_tool_confidence),
            float(state.predicted_tool_stability_sec),
            bool(state.autonomous_preparation_ready),
            tuple(
                (
                    row.rank,
                    row.instrument_id,
                    row.confidence,
                    row.stability_sec,
                )
                for row in state.ranked_tool_predictions
            ),
            tuple(state.active_recovery_tool_instances),
        )
        return bool(recovery_changed or after != before)

    def _clear_tool_prediction_state(self) -> None:
        self._ngram_preparation_stability.clear()
        self._ngram_recovery_stability.clear()
        self._twin.state.predicted_tool = ""
        self._twin.state.predicted_tool_confidence = 0.0
        self._twin.state.predicted_tool_stability_sec = 0.0
        self._twin.state.autonomous_preparation_ready = False
        self._twin.state.ranked_tool_predictions = []

    def _clear_inactive_tool_policy_state(self) -> bool:
        """Remove run-scoped policy display state while preserving tool belief.

        This is deliberately a confidence/evidence reset only.  It never
        relocates an instrument, so stop/reset cannot create an artificial
        tray/Mayo transition.
        """

        state = self._twin.state
        before = (
            str(state.predicted_tool),
            float(state.predicted_tool_confidence),
            float(state.predicted_tool_stability_sec),
            bool(state.autonomous_preparation_ready),
            tuple(
                (row.rank, row.instrument_id, row.confidence, row.stability_sec)
                for row in state.ranked_tool_predictions
            ),
            tuple(
                (
                    instrument.instance_id,
                    instrument.mayo_reuse_confidence,
                    instrument.mayo_reuse_stability_sec,
                    instrument.mayo_recovery_confidence,
                    instrument.mayo_recovery_stability_sec,
                    instrument.mayo_evidence_source,
                )
                for instrument in self._twin.instrument_states.values()
            ),
        )
        self._clear_tool_prediction_state()
        # VLM Mayo rows are now observability-only and no longer own an
        # automatic policy clock.  Keeping retired VLM stability maps here
        # made an inactive/reset transition depend on fields that no longer
        # exist on this owner.
        self._twin.clear_all_mayo_policy_evidence()
        after = (
            str(state.predicted_tool),
            float(state.predicted_tool_confidence),
            float(state.predicted_tool_stability_sec),
            bool(state.autonomous_preparation_ready),
            tuple(
                (row.rank, row.instrument_id, row.confidence, row.stability_sec)
                for row in state.ranked_tool_predictions
            ),
            tuple(
                (
                    instrument.instance_id,
                    instrument.mayo_reuse_confidence,
                    instrument.mayo_reuse_stability_sec,
                    instrument.mayo_recovery_confidence,
                    instrument.mayo_recovery_stability_sec,
                    instrument.mayo_evidence_source,
                )
                for instrument in self._twin.instrument_states.values()
            ),
        )
        return after != before

    def _fused_tool_prediction(
        self,
        payload: dict,
        now_sec: float,
        received_sec: float | None = None,
    ) -> tuple[str, float, dict]:
        """Backward-compatible name for the n-gram-only prediction lookup."""

        del payload, now_sec, received_sec
        return self._ngram_tool_prediction()

    def _handle_vlm_tool_prediction(
        self,
        payload: dict,
        msg: VLMResult,
        now_sec: float,
        received_sec: float | None = None,
    ) -> None:
        """Keep the schema ingress, but do not admit VLM tool scores to policy.

        The current automatic policy is entirely regenerated from the frozen
        0704 n-gram table.  This compatibility entry point intentionally
        ignores ``payload['tool']`` and every VLM confidence so a fresh VLM
        frame cannot alter preparation or recovery eligibility.
        """

        del payload, msg, received_sec
        self._refresh_ngram_tool_policy(now_sec=now_sec)
        return

    def _on_vlm_result(self, msg: VLMResult) -> None:
        source = str(msg.source or "unknown_vlm")
        state = self._twin.state
        current_run_id = str(getattr(state, "procedure_run_id", "") or "").strip()
        if (
            bool(getattr(state, "running", False))
            and str(getattr(state, "execution_state", "")).strip().lower()
            == "running"
            and (
                not current_run_id
                or str(getattr(msg, "procedure_run_id", "") or "").strip()
                != current_run_id
            )
        ):
            self._reject_visual_evidence(
                channel="vlm_result",
                source=source,
                reason="vlm_procedure_run_mismatch",
                message=msg,
            )
            return
        if self._perception_gate_active():
            self._reject_visual_evidence(
                channel="vlm_result",
                source=source,
                reason="vlm_source_not_ready",
                message=msg,
            )
            return
        if not self._admit_visual_evidence(
            msg,
            channel="vlm_result",
            source=source,
            require_epoch=(
                str(getattr(self, "_vlm_mode", "mock")) in {"real", "dual"}
                and "real_vlm" in source
            ),
        ):
            return
        if str(msg.schema_version) not in {"2", "3", "4", "5", "6"}:
            return
        try:
            payload = json.loads(msg.raw_json)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict) or str(payload.get("v", "")) not in {
            "2",
            "3",
            "4",
            "5",
            "6",
        }:
            return
        if "gesture" in payload or "sg" in payload:
            self._publish_reducer_decision_event(
                input_type="vlm_result",
                input_id=str(getattr(msg, "correlation_id", "")),
                input_source=source,
                accepted=False,
                reason="vlm_hand_fields_forbidden",
                detail={"forbidden_fields": [
                    field for field in ("gesture", "sg") if field in payload
                ]},
            )
            return
        # Use the same monotonic clock as the periodic DT policy refresh.
        # VLM header timestamps are not a policy time base anymore.
        now_sec = self._monotonic_sec()
        received_sec = None
        self._handle_vlm_tool_prediction(
            payload,
            msg,
            now_sec,
            received_sec,
        )
        # VLM Mayo reuse/recovery votes are intentionally no longer consumed
        # by the Digital Twin.  Autonomous tool policy has already been
        # refreshed from the frozen 0704 n-gram distribution above.
        self._publish_world_state()
        return

    def _on_observation(self, msg: ToolObservation) -> None:
        """Keep raw visual observations out of the location-state reducer.

        ToolObservation is an input to the probabilistic belief tracker, not
        a competing Digital Twin location authority.  It is retained as a ROS
        subscription for wire compatibility and VLM observability, but it may
        not mutate an instrument lifecycle.  The committed tracker projection
        below is the only perception-to-Twin location path.
        """

        del msg

    def _on_cam4_mayo_observation(
        self,
        msg: ToolObservation,
    ) -> None:
        # This callback intentionally remains harmless for tests/manual
        # invocation after the dedicated subscription was retired.  CAM4
        # frames influence Twin location only after the tracker has weighted
        # occlusion/context and emitted a committed belief.
        del msg

    def _on_tool_beliefs(self, msg: TrackedToolBeliefArray) -> None:
        """Project committed belief rows without re-admitting raw evidence."""

        if not bool(getattr(msg, "observation_only", False)):
            return
        state = self._twin.state
        if str(getattr(msg, "procedure_id", "")).strip() != str(
            state.procedure_id
        ).strip():
            return
        # A stale snapshot from a preceding Reset/run must not replay its
        # committed placement into the fresh inventory.  Before a run begins
        # the Twin already represents the operator-verified start layout, so
        # there is nothing for an unbound tracker snapshot to project.
        run_id = str(getattr(msg, "procedure_run_id", "")).strip()
        if not run_id or run_id != str(state.procedure_run_id or "").strip():
            return
        header = getattr(msg, "header", None)
        source_stamp_sec = self._stamp_sec(
            getattr(header, "stamp", self._stamp())
        )
        changed = False
        for belief in getattr(msg, "tools", ()):
            committed_location_id = str(
                getattr(belief, "committed_location_id", "")
            ).strip()
            if not committed_location_id:
                continue
            try:
                existence_probability = float(
                    getattr(belief, "existence_probability", 0.0)
                )
            except (TypeError, ValueError):
                existence_probability = 0.0
            if not math.isfinite(existence_probability) or existence_probability < 0.85:
                # An inactive exchangeable capacity slot cannot become a Twin
                # instance merely because a snapshot contains its logical ID.
                continue
            result = self._twin.project_committed_tool_belief(
                instrument_id=str(getattr(belief, "instrument_id", "")),
                instance_id=str(getattr(belief, "instance_id", "")),
                committed_location_id=committed_location_id,
                confidence=float(
                    getattr(belief, "committed_location_probability", 0.0)
                ),
                proposal_id=(
                    f"tool-belief:{run_id}:{getattr(belief, 'track_id', '')}:"
                    f"{committed_location_id}"
                ),
                source_stamp_sec=source_stamp_sec,
                evidence_sources=tuple(
                    str(item)
                    for item in getattr(belief, "evidence_sources", ())
                ),
            )
            if result.get("event_type") == "ToolBeliefProjectionAccepted":
                changed = True
                if committed_location_id == "surgeon":
                    accepted_tool_id = str(
                        result.get("instrument_id", belief.instrument_id)
                    ).strip()
                    if accepted_tool_id:
                        self._append_tool_history(
                            "_completed_handover_history",
                            accepted_tool_id,
                            getattr(header, "stamp", self._stamp()),
                        )
                        self._record_completed_handover_phase(
                            accepted_tool_id
                        )
                self._publish_reducer_decision_event(
                    input_type="tool_belief_commit",
                    input_id=str(result.get("proposal_id", "")),
                    input_source="tool_belief_tracker",
                    accepted=True,
                    reason=str(result.get("reducer_reason", "")),
                    affected_tool=str(result.get("instrument_id", "")),
                    detail=result,
                )
                self._publish_event(
                    "ToolBeliefProjectionAccepted",
                    instrument_id=str(result.get("instrument_id", "")),
                    instance_id=str(result.get("instance_id", "")),
                    location_id=str(result.get("location_id", "")),
                    location_type=str(result.get("location_type", "")),
                    confidence=float(result.get("confidence", 0.0)),
                    mode="observation_projection",
                    detail=result,
                )
        if changed:
            self._publish_world_state()

    def _reconcile_tool_observation(
        self,
        msg: ToolObservation,
        *,
        source: str,
        cam4_mayo_channel: bool = False,
    ) -> None:
        # Callers map each input lane to its authority source before this
        # point.  Do not re-read ``msg.source`` here: a generic VLM-topic
        # payload must not relabel itself as the dedicated typed CAM4 lane.
        resolved_source = source
        is_cam4_detector = bool(
            cam4_mayo_channel
            and source
            in {
                "cam4_rfdetr_mayo_observation",
                CAM4_TYPED_MAYO_OBSERVATION_SOURCE,
            }
        )
        is_legacy_cam4_detector = (
            source == "cam4_rfdetr_mayo_observation"
        )
        if is_cam4_detector:
            if self._camera_gate_active("cam4"):
                self._reject_visual_evidence(
                    channel="cam4_tool_observation",
                    source=resolved_source,
                    reason="cam4_source_not_ready",
                    message=msg,
                )
                return
        elif self._perception_gate_active():
            self._reject_visual_evidence(
                channel="vlm_tool_observation",
                source=resolved_source,
                reason="vlm_source_not_ready",
                message=msg,
            )
            return
        resolved_tool = self._twin.spec.resolve_instrument_alias(msg.instrument_id) or msg.instrument_id
        admission_channel = (
            f"{'cam4' if is_cam4_detector else 'vlm'}_tool:"
            f"{resolved_source}:{resolved_tool}:{msg.location_type}:"
            f"{msg.location_id}"
        )
        placement_episode_started_sec: float | None = None
        placement_episode_id = ""
        if is_cam4_detector:
            if not self._admit_cam4_mayo_observation(
                msg,
                channel=admission_channel,
                source=resolved_source,
            ):
                return
            if is_legacy_cam4_detector:
                episode_metadata = self._cam4_mayo_episode_metadata(msg)
                if episode_metadata is None:
                    self._reject_visual_evidence(
                        channel=admission_channel,
                        source=resolved_source,
                        reason="invalid_cam4_presence_episode",
                        message=msg,
                    )
                    return
                placement_episode_started_sec, placement_episode_id = (
                    episode_metadata
                )
                if placement_episode_id in getattr(
                    self,
                    "_accepted_cam4_mayo_episodes",
                    set(),
                ):
                    # Renewals keep source freshness visible to admission, but
                    # an already-consumed local-detector episode is idempotent.
                    return
        elif not self._admit_visual_evidence(
            msg,
            channel=admission_channel,
            source=resolved_source,
            require_epoch=(
                str(getattr(self, "_vlm_mode", "mock"))
                in {"real", "dual"}
                and "real_vlm" in resolved_source
            ),
        ):
            return
        stamp_sec = float(msg.stamp.sec) + float(msg.stamp.nanosec) / 1_000_000_000.0
        proposal_id = (
            f"toolobs:{msg.instrument_id}:{msg.location_type}:{msg.location_id}:"
            f"{stamp_sec:.3f}:{msg.confidence:.2f}"
        )
        current_lifecycle = ""
        proposed_lifecycle = ""
        current_state = self._twin.get_instrument_state(resolved_tool)
        if current_state is not None:
            current_lifecycle = current_state.lifecycle_stage
        result = self._twin.reconcile_observation(
            msg,
            source=resolved_source,
            proposal_id=proposal_id,
            placement_episode_started_sec=placement_episode_started_sec,
            placement_episode_id=placement_episode_id,
        )
        if is_cam4_detector and placement_episode_id:
            # One uninterrupted visibility episode gets exactly one reducer
            # decision, whether accepted or rejected. Renewals are freshness
            # heartbeats, not repeated world mutations or audit events.
            consumed_episodes = getattr(
                self,
                "_accepted_cam4_mayo_episodes",
                None,
            )
            if consumed_episodes is None:
                consumed_episodes = set()
                self._accepted_cam4_mayo_episodes = consumed_episodes
            consumed_episodes.add(placement_episode_id)
        if result:
            proposed_lifecycle = str(result.get("proposed_lifecycle", ""))
            self._publish_vlm_inference_proposal(
                msg,
                proposal_id=proposal_id,
                current_lifecycle=current_lifecycle,
                proposed_lifecycle=proposed_lifecycle,
            )
            self._publish_vlm_reducer_decision(result)
            self._publish_event(
                str(result.get("event_type", "VLMProposalIgnored")),
                instrument_id=str(result.get("instrument_id", msg.instrument_id)),
                location_id=str(result.get("location_id", msg.location_id)),
                location_type=str(result.get("location_type", msg.location_type)),
                confidence=float(result.get("confidence", msg.confidence)),
                detail=result,
                mode=str(result.get("reducer_result", "ignored")),
            )
        self._publish_world_state()

    def _on_skill_event(self, msg: TwinEvent) -> None:
        detail = self._skill_event_detail(msg)
        admitted, _reason = self._admit_current_run_skill_event(msg, detail)
        if not admitted:
            return
        belief_owned_location_event = (
            msg.event_type in BELIEF_OWNED_TOOL_LOCATION_EVENTS
        )
        # Tool location has exactly one runtime owner.  Skill events remain
        # visible in the audit stream, while the belief tracker consumes them
        # as weighted evidence and later publishes the only committed location
        # projection.  Robot task lifecycle events still enter the Twin here.
        if not belief_owned_location_event:
            self._twin.apply_event(msg)
        task_id = self._skill_event_task_id(detail)
        if msg.event_type == "RobotTaskStarted" and task_id:
            self._current_run_skill_task_ids.add(task_id)
        elif msg.event_type == "RobotTaskCompleted" and task_id:
            self._current_run_skill_task_ids.discard(task_id)
        if (
            msg.event_type == "RobotTaskStarted"
            and self._active_robot_task_is_direct_delivery()
        ):
            self._suspend_hand_handover_state()
        if not isinstance(detail, dict):
            detail = {"detail": detail}
        detail.update(self._twin.request_queue_summary())
        msg.detail_json = json.dumps(
            self._augment_event_detail(
                msg.event_type,
                detail,
                instrument_id=msg.instrument_id,
                location_id=msg.location_id,
                status=msg.status,
                source_location_id=msg.source_location_id,
                target_location_id=msg.target_location_id,
                mode=msg.mode,
            ),
            sort_keys=True,
        )
        self._event_pub.publish(msg)
        simulation_event = SimulationEvent()
        simulation_event.stamp = msg.stamp
        simulation_event.procedure_run_id = msg.procedure_run_id
        simulation_event.event_type = msg.event_type
        simulation_event.instrument_id = msg.instrument_id
        simulation_event.from_anchor = msg.source_location_id or msg.location_id
        simulation_event.to_anchor = msg.target_location_id or msg.location_id
        simulation_event.arm = msg.arm
        simulation_event.status = msg.status
        simulation_event.detail = msg.detail_json
        self._simulation_event_pub.publish(simulation_event)
        self._record_important_event(simulation_event)
        self._publish_world_state()

    def _on_bed_robot_arm_group_request(self, msg: BedRobotArmGroupRequest) -> None:
        if (
            not bool(self._twin.state.running)
            or str(self._twin.state.execution_state).strip().lower() != "running"
            or str(getattr(msg, "procedure_run_id", "")).strip()
            != str(self._twin.state.procedure_run_id).strip()
        ):
            return
        group_id = str(msg.group_id or "").strip().lower()
        if group_id != "retraction":
            self._twin.update_bed_robot_arm_group_request(msg)
            self._publish_event(
                "BedRobotArmGroupRequestRejected",
                phase_id=msg.phase_id,
                status="rejected",
                mode="bed_robot_arm_group_request",
                detail={
                    "request_id": msg.request_id,
                    "group_id": group_id,
                    "operation": msg.operation,
                    "reason": "unsupported_group",
                },
            )
            self._publish_world_state()
            return
        if msg.request_id:
            self._pending_bed_robot_arm_group_requests[msg.request_id] = msg
        self._twin.update_bed_robot_arm_group_request(msg)
        self._publish_event(
            "BedRobotArmGroupRequestObserved",
            phase_id=msg.phase_id,
            status="pending",
            mode="bed_robot_arm_group_request",
            detail={
                "request_id": msg.request_id,
                "group_id": group_id,
                "operation": msg.operation,
                "voice_text": msg.voice_text,
                "procedure_id": msg.procedure_id,
                "phase_id": msg.phase_id,
                "arm_id": msg.arm_id,
                "target_tool_id": msg.target_tool_id,
                "adjustment_mode": msg.adjustment_mode,
                "target_retractor_id": msg.target_retractor_id,
                "direction_frame": msg.direction_frame,
                "end_effector_profile": msg.end_effector_profile,
                "source": msg.source,
            },
        )
        self._publish_world_state()

    def _on_bed_robot_arm_group_proposal(self, msg: BedRobotArmGroupActionProposal) -> None:
        source = str(getattr(msg, "source", "")) or "vlm_bed_robot_arm_group"
        if self._perception_gate_active():
            self._reject_visual_evidence(
                channel="bed_robot_arm_group_proposal",
                source=source,
                reason="vlm_source_not_ready",
                message=msg,
            )
            return
        request_id = str(msg.command.request_id or "unresolved")
        if not self._admit_visual_evidence(
            msg,
            channel=f"bed_robot_arm_group_proposal:{request_id}",
            source=source,
            require_epoch=(
                str(getattr(self, "_vlm_mode", "mock")) in {"real", "dual"}
                and "real_vlm" in source
            ),
        ):
            return
        command = msg.command
        if str(command.group_id or "").strip().lower() != "retraction":
            self._publish_event(
                "BedRobotArmGroupProposalRejected",
                phase_id=self._twin.state.filtered_phase,
                status="rejected",
                confidence=float(command.confidence),
                mode="vlm_bed_robot_arm_group_proposal",
                detail={
                    "request_id": command.request_id,
                    "command_id": command.command_id,
                    "group_id": command.group_id,
                    "operation": command.operation,
                    "reason": "unsupported_group",
                },
            )
            self._publish_world_state()
            return
        self._publish_event(
            "BedRobotArmGroupProposalObserved",
            phase_id=self._twin.state.filtered_phase,
            status="valid" if msg.valid else "rejected",
            confidence=float(command.confidence),
            mode="vlm_bed_robot_arm_group_proposal",
            detail={
                "schema_version": msg.schema_version,
                "valid": bool(msg.valid),
                "validation_error": msg.validation_error,
                "request_id": command.request_id,
                "command_id": command.command_id,
                "group_id": command.group_id,
                "operation": command.operation,
                "arm_id": command.arm_id,
                "target_tool_id": command.target_tool_id,
                "adjustment_mode": command.adjustment_mode,
                "target_retractor_id": command.target_retractor_id,
                "direction_frame": command.direction_frame,
                "direction": command.direction,
                "axis": command.axis,
                "distance_mm": float(command.distance_mm),
                "distance_origin": command.distance_origin,
                "raw_distance_text": command.raw_distance_text,
                "end_effector_profile": command.end_effector_profile,
                "rationale": command.rationale,
                "confidence": float(command.confidence),
            },
        )
        self._publish_world_state()

    def _on_bed_robot_arm_group_command(self, msg: BedRobotArmGroupCommand) -> None:
        if str(msg.group_id or "").strip().lower() != "retraction":
            self._publish_event(
                "BedRobotArmGroupCommandRejected",
                phase_id=self._twin.state.filtered_phase,
                status="rejected",
                confidence=float(msg.confidence),
                mode="bt_bed_robot_arm_group_guard",
                detail={
                    "request_id": msg.request_id,
                    "command_id": msg.command_id,
                    "group_id": msg.group_id,
                    "operation": msg.operation,
                    "reason": "unsupported_group",
                },
            )
            self._publish_world_state()
            return
        belief = self._twin.state.bed_robot_arm_groups.get(msg.group_id)
        state_update_ignored = False
        if belief is not None:
            command_stamp = msg.stamp
            if not int(command_stamp.sec) and not int(command_stamp.nanosec):
                command_stamp = self._stamp()
            command_ns = int(command_stamp.sec) * 1_000_000_000 + int(
                command_stamp.nanosec
            )
            current_ns = int(belief.last_operation_stamp_sec) * 1_000_000_000 + int(
                belief.last_operation_stamp_nanosec
            )
            state_update_ignored = bool(current_ns and command_ns <= current_ns)
            if not state_update_ignored:
                belief.last_operation_stamp_sec = int(command_stamp.sec)
                belief.last_operation_stamp_nanosec = int(command_stamp.nanosec)
                belief.active_request_id = msg.request_id
                belief.active_command_id = msg.command_id
                belief.operation = msg.operation
                belief.target_tool_id = msg.target_tool_id
                belief.adjustment_mode = msg.adjustment_mode
                belief.target_retractor_id = msg.target_retractor_id
                belief.direction_frame = msg.direction_frame
                belief.direction = msg.direction
                belief.axis = msg.axis
                belief.distance_mm = float(msg.distance_mm)
                belief.distance_origin = msg.distance_origin
                belief.raw_distance_text = msg.raw_distance_text
                belief.progress = 0.0
                belief.error_code = ""
                belief.error_message = ""
                belief.rejection_reason = ""
        self._publish_event(
            "BedRobotArmGroupCommandApproved",
            phase_id=self._twin.state.filtered_phase,
            status="approved",
            confidence=float(msg.confidence),
            mode="bt_bed_robot_arm_group_guard",
            detail={
                "request_id": msg.request_id,
                "command_id": msg.command_id,
                "group_id": msg.group_id,
                "operation": msg.operation,
                "arm_id": msg.arm_id,
                "target_tool_id": msg.target_tool_id,
                "adjustment_mode": msg.adjustment_mode,
                "target_retractor_id": msg.target_retractor_id,
                "direction_frame": msg.direction_frame,
                "direction": msg.direction,
                "axis": msg.axis,
                "distance_mm": float(msg.distance_mm),
                "distance_origin": msg.distance_origin,
                "raw_distance_text": msg.raw_distance_text,
                "end_effector_profile": msg.end_effector_profile,
                "rationale": msg.rationale,
                "confidence": float(msg.confidence),
                "state_update_ignored_stale": state_update_ignored,
            },
        )
        self._publish_world_state()

    def _on_bed_robot_arm_controller_status(
        self, msg: BedRobotArmStateArray
    ) -> None:
        source_stamp_ns = self._bed_robot_controller_source_stamp_ns(msg)
        if source_stamp_ns is None or source_stamp_ns <= 0:
            return
        source_age_sec = self._bed_robot_controller_source_age_sec(source_stamp_ns)
        if (
            source_age_sec > self._bed_robot_source_max_age_sec
            or source_age_sec < -self._bed_robot_source_future_tolerance_sec
        ):
            return
        if self._twin.update_bed_robot_arm_controller_status(msg) is not True:
            return
        self._bed_robot_status_received_monotonic = self._monotonic_sec()
        self._bed_robot_status_source_stamp_ns = source_stamp_ns
        # Accepted controller heartbeats keep freshness alive, but they are not
        # clinical/task events. Publishing each one flooded replay timelines
        # even while no bed-robot request or command existed.
        if not self._twin.bed_robot_arm_controller_state_changed():
            return
        self._publish_event(
            "BedRobotArmControllerStateUpdated",
            status=self._twin.state.bed_robot_arm_groups["retraction"].state,
            mode="external_bed_robot_arm_status",
            detail={
                "revision": int(msg.revision),
                "procedure_type": msg.procedure_type,
                "arms": [
                    {
                        "arm_id": arm.arm_id,
                        "role": arm.role,
                        "role_instance_id": arm.role_instance_id,
                        "state": arm.state,
                        "direct_teach_active": bool(arm.direct_teach_active),
                        "reason_code": arm.reason_code,
                    }
                    for arm in msg.arms
                ],
            },
        )
        self._publish_world_state()

    @staticmethod
    def _bed_robot_controller_source_stamp_ns(
        msg: BedRobotArmStateArray,
    ) -> int | None:
        sec = int(msg.stamp.sec)
        nanosec = int(msg.stamp.nanosec)
        if sec < 0 or nanosec < 0 or nanosec >= 1_000_000_000:
            return None
        return sec * 1_000_000_000 + nanosec

    @staticmethod
    def _wall_time_ns() -> int:
        return time.time_ns()

    @staticmethod
    def _monotonic_sec() -> float:
        return time.monotonic()

    def _bed_robot_controller_source_age_sec(self, source_stamp_ns: int) -> float:
        return (self._wall_time_ns() - source_stamp_ns) / 1_000_000_000.0

    def _reset_bed_robot_controller_freshness(self) -> None:
        self._bed_robot_status_received_monotonic = 0.0
        self._bed_robot_status_source_stamp_ns = None

    def _expire_bed_robot_controller_status(self) -> bool:
        received_at = self._bed_robot_status_received_monotonic
        source_stamp_ns = self._bed_robot_status_source_stamp_ns
        if received_at <= 0.0 or source_stamp_ns is None:
            return False
        receipt_age_sec = self._monotonic_sec() - received_at
        source_age_sec = self._bed_robot_controller_source_age_sec(source_stamp_ns)
        if (
            receipt_age_sec <= self._bed_robot_status_timeout_sec
            and source_age_sec <= self._bed_robot_source_max_age_sec
            and source_age_sec >= -self._bed_robot_source_future_tolerance_sec
        ):
            return False
        self._reset_bed_robot_controller_freshness()
        expired = bool(self._twin.expire_bed_robot_arm_controller_status())
        if expired:
            self._publish_event(
                "BedRobotArmControllerStateExpired",
                status="unknown",
                mode="external_bed_robot_arm_status",
                detail={
                    "reason_code": "controller_status_stale",
                    "receipt_age_sec": receipt_age_sec,
                    "source_age_sec": source_age_sec,
                },
            )
        return expired

    def _on_bed_robot_arm_group_status(self, msg: BedRobotArmGroupStatus) -> None:
        if (
            not bool(self._twin.state.running)
            or str(self._twin.state.execution_state).strip().lower() != "running"
            or str(getattr(msg, "procedure_run_id", "")).strip()
            != str(self._twin.state.procedure_run_id).strip()
        ):
            return
        if not int(msg.stamp.sec) and not int(msg.stamp.nanosec):
            msg.stamp = self._stamp()
        status_changed = self._twin.update_bed_robot_arm_group_status(msg)
        if (
            str(msg.group_id or "").strip().lower() == "retraction"
            and bool(msg.terminal)
            and msg.request_id
        ):
            self._pending_bed_robot_arm_group_requests.pop(msg.request_id, None)
        is_health = not msg.operation and msg.request_id.startswith("health-")
        if status_changed is not True:
            return
        if msg.outcome == "cancelled_by_runtime_control":
            event_type = "BedRobotArmGroupCommandCancelled"
        elif msg.outcome == "accepted":
            # The unified retraction Service reports request admission, not
            # physical completion of the controller-side command.
            event_type = "BedRobotArmGroupCommandAccepted"
        elif is_health:
            event_type = "BedRobotArmGroupAvailabilityChanged"
        elif bool(msg.terminal):
            event_type = (
                "BedRobotArmGroupCommandCompleted"
                if bool(msg.success)
                else "BedRobotArmGroupCommandRejected"
            )
        else:
            event_type = "BedRobotArmGroupStatusUpdated"
        self._publish_event(
            event_type,
            status=msg.state,
            confidence=float(msg.confidence),
            mode="bed_robot_arm_group_status",
            detail={
                "request_id": msg.request_id,
                "command_id": msg.command_id,
                "group_id": msg.group_id,
                "operation": msg.operation,
                "state": msg.state,
                "outcome": msg.outcome,
                "terminal": bool(msg.terminal),
                "success": bool(msg.success),
                "admission_only": msg.outcome == "accepted",
                "message": msg.message,
                "arm_id": msg.arm_id,
                "target_tool_id": msg.target_tool_id,
                "adjustment_mode": msg.adjustment_mode,
                "target_retractor_id": msg.target_retractor_id,
                "direction_frame": msg.direction_frame,
                "direction": msg.direction,
                "axis": msg.axis,
                "distance_mm": float(msg.distance_mm),
                "distance_origin": msg.distance_origin,
                "raw_distance_text": msg.raw_distance_text,
                "progress": float(msg.progress),
                "elapsed_sec": float(msg.elapsed_sec),
                "remaining_sec": float(msg.remaining_sec),
                "error_code": msg.error_code,
                "rejection_reason": msg.rejection_reason,
            },
        )
        self._publish_world_state()

    # Voice command admission is owned by command_router. ODT receives
    # only the resulting typed state updates and has no receipt/gateway path.
    def _on_surgeon_request(self, msg: SurgeonRequest) -> None:
        if (
            not bool(msg.override)
            and not self._accept_non_override_structured_requests
        ):
            self.get_logger().warning(
                "ignored non-override structured surgeon request; "
                "publish SpeechUtterance through the public input boundary",
                throttle_duration_sec=2.0,
            )
            return
        resolved = self._twin.update_surgeon_request(msg)
        if resolved and str(msg.event_type) in {"request_tool", "voice_request"}:
            self._append_tool_history(
                "_validated_tool_request_history",
                resolved,
                msg.stamp,
            )
            self._clear_tool_prediction_state()
        queue_detail = self._twin.request_queue_summary()
        self._publish_event(
            "SurgeonRequestObserved",
            instrument_id=resolved or msg.requested_tool,
            detail={
                "event_type": msg.event_type,
                "voice_text": msg.voice_text,
                "override": bool(msg.override),
                "note": msg.note,
                **queue_detail,
            },
            target_owner="surgeon",
            mode="surgeon_request",
        )
        self._publish_world_state()

    def _on_phase(self, msg: FilteredPhase) -> None:
        if self._phase_authority != "legacy_estimator":
            self._publish_reducer_decision_event(
                input_type="legacy_filtered_phase",
                input_id=msg.phase_id,
                input_source="phase_estimator",
                accepted=False,
                reason="ignored_because_reducer_is_phase_authority",
                affected_phase=msg.phase_id,
                detail={
                    "phase_id": msg.phase_id,
                    "confidence": float(msg.confidence),
                    "uncertain": bool(msg.uncertain),
                    "validation_mode": self._validation_mode,
                },
            )
            return
        self._twin.update_phase(msg)
        self._publish_event(
            "PhaseUpdated",
            phase_id=msg.phase_id,
            detail={
                "phase_id": msg.phase_id,
                "confidence": float(msg.confidence),
                "uncertain": bool(msg.uncertain),
                "stability": float(msg.stability),
            },
            mode="vlm_phase_estimator",
        )
        self._publish_world_state()

    def _on_control(self, msg: String) -> None:
        raw_command = msg.data.strip()
        command, _, start_phase_id = raw_command.partition(":")
        command = command.strip().lower()
        start_phase_id = start_phase_id.strip()
        lifecycle_commands = {
            "start",
            "start_runtime",
            "start_actors",
            "pause",
            "resume",
            "stop",
        }
        signature = (command, start_phase_id)
        if command in lifecycle_commands:
            if signature == getattr(
                self, "_last_lifecycle_control_signature", None
            ):
                # Duplicate transport frames are mutation-idempotent, but a
                # fresh state acknowledgment is still required by the
                # manager's publish-until-observed retry contract.
                self._publish_world_state()
                return
            if (
                command == "start"
                and not start_phase_id
                and bool(self._twin.state.running)
                and self._twin.state.execution_state == "running"
            ):
                self._last_lifecycle_control_signature = signature
                self._publish_world_state()
                return
            if (
                command == "pause"
                and self._twin.state.execution_state == "paused"
            ):
                self._last_lifecycle_control_signature = signature
                self._publish_world_state()
                return
            if (
                command == "resume"
                and bool(self._twin.state.running)
                and self._twin.state.execution_state == "running"
            ):
                self._last_lifecycle_control_signature = signature
                self._publish_world_state()
                return
            if (
                command == "stop"
                and not bool(self._twin.state.running)
                and self._twin.state.execution_state == "halted"
            ):
                self._last_lifecycle_control_signature = signature
                self._publish_world_state()
                return
            self._last_lifecycle_control_signature = signature
        if command in {
            "start",
            "start_runtime",
            "pause",
            "resume",
            "stop",
            "reset",
        }:
            self._advance_visual_runtime_epoch()
            self._advance_skill_event_runtime_epoch()
        if command in {"start", "start_runtime"}:
            self._vlm_health_run_started_monotonic = time.monotonic()
            self._pending_bed_robot_arm_group_requests.clear()
            self._reset_hand_handover_state()
            self._clear_tool_histories()
            # A new run must earn its n-gram dwell from zero.  Retaining the
            # previous run's first_seen timestamp made preparation immediately
            # ready after Reset (observed as 224 s / 55 s at run start).
            self._clear_inactive_tool_policy_state()
            # Start begins a new execution run from the current stopped Twin
            # state.  It must not erase real-to-sim observations, instrument
            # placement, or a researcher-authored pre-start adjustment.  The
            # explicit `reset` control below is the only canonical-layout
            # operation.
            self._twin.state.procedure_run_id = uuid.uuid4().hex
            self._phase_entered_ros_sec = self._stamp_sec(self._stamp())
            if start_phase_id:
                self._twin.set_initial_phase(start_phase_id)
                self._phase_entered_ros_sec = self._stamp_sec(self._stamp())
            # ``start_runtime`` is deliberately emitted before the behaviour
            # tree and controller-facing actors are ready.  Keeping the Twin
            # in a distinct transitional state prevents UI/hand/VLM paths
            # from treating the optimistic start acknowledgement as a fully
            # executable procedure.  The manager sends ``start_actors`` only
            # after the executor has confirmed that it is running.
            self._twin.set_execution_state(
                command == "start",
                "running" if command == "start" else "starting",
            )
        elif command == "start_actors":
            self._twin.set_execution_state(True, "running")
        elif command == "pause":
            self._vlm_health_run_started_monotonic = None
            self._suspend_hand_handover_state()
            self._clear_inactive_tool_policy_state()
            self._twin.set_execution_state(True, "paused")
        elif command == "resume":
            self._vlm_health_run_started_monotonic = time.monotonic()
            self._twin.set_execution_state(True, "running")
        elif command == "stop":
            self._vlm_health_run_started_monotonic = None
            self._pending_bed_robot_arm_group_requests.clear()
            self._reset_hand_handover_state()
            self._clear_inactive_tool_policy_state()
            self._twin.set_execution_state(False, "halted")
        elif command == "reset":
            self._vlm_health_run_started_monotonic = None
            self._last_lifecycle_control_signature = None
            self._pending_bed_robot_arm_group_requests.clear()
            self._reset_hand_handover_state()
            self._clear_tool_histories()
            self._clear_inactive_tool_policy_state()
            self._twin.reset_runtime()
            self._reset_bed_robot_controller_freshness()
            self._stamp_all_bed_robot_arm_groups()
            self._phase_entered_ros_sec = self._stamp_sec(self._stamp())
            self._twin.set_execution_state(False, "idle")
        if command in {"stop", "reset"}:
            self._apply_pending_scenario_config_if_quiescent()
        self._publish_world_state()

    def destroy_node(self):
        return super().destroy_node()


def main() -> None:
    rclpy.init()
    node = ORDigitalTwinNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            node.destroy_node()
        finally:
            try:
                if rclpy.ok():
                    rclpy.shutdown()
            except Exception:
                pass
