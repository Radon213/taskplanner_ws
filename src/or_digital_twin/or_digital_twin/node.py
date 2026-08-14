"""ROS 2 node for publishing the runtime digital twin."""

from __future__ import annotations

from collections import deque
from dataclasses import asdict
import json
from pathlib import Path
import time

from procedure_spec import (
    ProcedurePriorScorer,
    compact_procedure_prompt,
    discover_prompt_bundle_dirs,
    get_default_spec_dir,
    load_bundle,
)
import rclpy
from rcl_interfaces.msg import SetParametersResult
from rclpy.node import Node
from std_msgs.msg import String
from surgical_interop_msgs.msg import BedRobotArmStateArray
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

from .twin import ORDigitalTwin
from .models import RankedToolPredictionBelief


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
        self.declare_parameter("vlm_recent_event_count", 6)
        self.declare_parameter("validation_mode", "bt_twin")
        self.declare_parameter("phase_authority", "reducer")
        self.declare_parameter("vlm_mode", "mock")
        self.declare_parameter("vlm_health_timeout_sec", 6.0)
        self.declare_parameter("vlm_evidence_max_gap_sec", 2.5)
        self.declare_parameter("mayo_retrieve_confidence_threshold", 0.5)
        self.declare_parameter("mayo_reuse_suppress_threshold", 0.5)
        self.declare_parameter("mayo_stability_sec", 5.0)
        self.declare_parameter("tool_predict_evidence_confidence_threshold", 0.5)
        self.declare_parameter("tool_predict_confidence_threshold", 0.8)
        self.declare_parameter("tool_predict_stability_sec", 3.0)
        self.declare_parameter("vlm_implicit_request_confidence_threshold", 0.8)
        self.declare_parameter("vlm_implicit_request_stability_sec", 0.7)
        self.declare_parameter("vlm_implicit_request_release_sec", 1.5)
        self.declare_parameter("accept_validation_actor_events", False)
        self.declare_parameter("accept_non_override_structured_requests", False)
        self.declare_parameter("evaluation_observation_topic", "")
        self.declare_parameter(
            "allow_shadow_request_capacity_reconciliation",
            False,
        )
        self.declare_parameter("allow_shadow_type_instance_requests", False)
        self.declare_parameter("allow_open_set_phase_bootstrap", False)
        self.declare_parameter("bed_robot_status_timeout_sec", 2.0)
        self.declare_parameter("bed_robot_source_max_age_sec", 2.0)
        self.declare_parameter("bed_robot_source_future_tolerance_sec", 0.5)
        self._spec_dir = str(self.get_parameter("spec_dir").value)
        self._vlm_recent_event_count = max(1, int(self.get_parameter("vlm_recent_event_count").value))
        self._validation_mode = str(self.get_parameter("validation_mode").value)
        self._phase_authority = str(self.get_parameter("phase_authority").value)
        self._vlm_mode = str(self.get_parameter("vlm_mode").value)
        self._vlm_health_timeout_sec = max(0.5, float(self.get_parameter("vlm_health_timeout_sec").value))
        self._vlm_evidence_max_gap_sec = max(
            0.5,
            float(self.get_parameter("vlm_evidence_max_gap_sec").value),
        )
        self._mayo_retrieve_threshold = float(self.get_parameter("mayo_retrieve_confidence_threshold").value)
        self._mayo_reuse_threshold = float(self.get_parameter("mayo_reuse_suppress_threshold").value)
        self._mayo_stability_sec = max(0.1, float(self.get_parameter("mayo_stability_sec").value))
        self._tool_predict_evidence_threshold = float(
            self.get_parameter("tool_predict_evidence_confidence_threshold").value
        )
        self._tool_predict_threshold = float(self.get_parameter("tool_predict_confidence_threshold").value)
        self._tool_predict_stability_sec = max(0.1, float(self.get_parameter("tool_predict_stability_sec").value))
        self._vlm_implicit_request_threshold = float(
            self.get_parameter("vlm_implicit_request_confidence_threshold").value
        )
        self._vlm_implicit_request_stability_sec = max(
            0.1,
            float(self.get_parameter("vlm_implicit_request_stability_sec").value),
        )
        self._vlm_implicit_request_release_sec = max(
            0.1,
            float(self.get_parameter("vlm_implicit_request_release_sec").value),
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
        self._twin = ORDigitalTwin(
            load_bundle(self._spec_dir),
            allow_shadow_request_capacity_reconciliation=bool(
                self.get_parameter(
                    "allow_shadow_request_capacity_reconciliation"
                ).value
            ),
            allow_shadow_type_instance_requests=bool(
                self.get_parameter(
                    "allow_shadow_type_instance_requests"
                ).value
            ),
            allow_open_set_phase_bootstrap=bool(
                self.get_parameter("allow_open_set_phase_bootstrap").value
            ),
        )
        self._stamp_all_bed_robot_arm_groups()
        self._prior_scorer = ProcedurePriorScorer(self._twin.spec, compact_procedure_prompt(self._spec_dir))
        self._bundle_metadata_cache = self._build_bundle_metadata()
        self._important_events: deque[SimulationEvent] = deque(maxlen=self._vlm_recent_event_count)
        self._validated_tool_request_history: deque[dict] = deque(maxlen=12)
        self._completed_handover_history: deque[dict] = deque(maxlen=12)
        self._latest_outward_signal: SurgeonOutwardSignal | None = None
        self._vlm_health_by_topic: dict[str, tuple[VLMHealth, float]] = {}
        self._input_source_status_by_id: dict[str, InputSourceStatus] = {}
        self._visual_admission_by_channel: dict[
            str, tuple[int, int, float]
        ] = {}
        self._visual_runtime_epoch_floor = 0
        self._vlm_evidence_blocked = False
        self._perception_health_seen = False
        self._perception_enabled = True
        self._mayo_retrieve_stability: dict[str, dict] = {}
        self._mayo_reuse_stability: dict[str, dict] = {}
        self._tool_predict_stability: dict[str, dict] = {}
        self._tool_prediction_last_sample_by_source: dict[
            str, tuple[float, str]
        ] = {}
        self._vlm_implicit_request_stability: dict[str, dict] = {}
        self._vlm_implicit_request_episode_tool = ""
        self._vlm_implicit_request_release_since: float | None = None
        self._pending_bed_robot_arm_group_requests: dict[str, BedRobotArmGroupRequest] = {}
        self._bed_robot_status_received_monotonic = 0.0
        self._bed_robot_status_source_stamp_ns: int | None = None
        self._phase_entered_ros_sec = self._stamp_sec(self._stamp())
        self.add_on_set_parameters_callback(self._on_parameters_changed)

        self._world_pub = self.create_publisher(WorldState, "/twin/world_state", 20)
        self._tool_pub = self.create_publisher(InstrumentState, "/twin/tool_states", 50)
        self._event_pub = self.create_publisher(TwinEvent, "/twin/events", 50)
        self._simulation_state_pub = self.create_publisher(SimulationState, "/simulation/state", 20)
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
        self.create_subscription(
            ToolObservation,
            "/surgery/perception/cam4/mayo_tool_observations",
            self._on_cam4_mayo_observation,
            50,
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
        self.create_subscription(String, "/surgery/audio/request_text", self._on_request, 20)
        self.create_subscription(SurgeonRequest, "/surgeon/request", self._on_surgeon_request, 20)
        self.create_subscription(FilteredPhase, "/phase/filtered", self._on_phase, 20)
        self.create_subscription(String, "/simulation/control_state", self._on_control, 20)

        self.create_timer(0.5, self._publish_world_state)
        self._publish_world_state()

    def _on_parameters_changed(self, params):
        for parameter in params:
            if parameter.name == "spec_dir":
                try:
                    self._spec_dir = str(parameter.value)
                    self._twin.reset_spec(load_bundle(self._spec_dir))
                    self._stamp_all_bed_robot_arm_groups()
                    self._prior_scorer = ProcedurePriorScorer(self._twin.spec, compact_procedure_prompt(self._spec_dir))
                    self._bundle_metadata_cache = self._build_bundle_metadata()
                    self._important_events.clear()
                    self._validated_tool_request_history.clear()
                    self._completed_handover_history.clear()
                    self._mayo_retrieve_stability.clear()
                    self._mayo_reuse_stability.clear()
                    self._tool_predict_stability.clear()
                    self._tool_prediction_last_sample_by_source.clear()
                    self._clear_vlm_implicit_request_state()
                    self._advance_visual_runtime_epoch()
                    self._pending_bed_robot_arm_group_requests.clear()
                    self._reset_bed_robot_controller_freshness()
                    self._phase_entered_ros_sec = self._stamp_sec(self._stamp())
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
            elif parameter.name == "mayo_retrieve_confidence_threshold":
                self._mayo_retrieve_threshold = float(parameter.value)
            elif parameter.name == "mayo_reuse_suppress_threshold":
                self._mayo_reuse_threshold = float(parameter.value)
            elif parameter.name == "mayo_stability_sec":
                self._mayo_stability_sec = max(0.1, float(parameter.value))
            elif parameter.name == "tool_predict_evidence_confidence_threshold":
                self._tool_predict_evidence_threshold = float(parameter.value)
            elif parameter.name == "tool_predict_confidence_threshold":
                self._tool_predict_threshold = float(parameter.value)
            elif parameter.name == "tool_predict_stability_sec":
                self._tool_predict_stability_sec = max(0.1, float(parameter.value))
            elif parameter.name == "vlm_evidence_max_gap_sec":
                self._vlm_evidence_max_gap_sec = max(
                    0.5,
                    float(parameter.value),
                )
            elif parameter.name == "vlm_implicit_request_confidence_threshold":
                self._vlm_implicit_request_threshold = float(parameter.value)
            elif parameter.name == "vlm_implicit_request_stability_sec":
                self._vlm_implicit_request_stability_sec = max(
                    0.1,
                    float(parameter.value),
                )
            elif parameter.name == "vlm_implicit_request_release_sec":
                self._vlm_implicit_request_release_sec = max(
                    0.1,
                    float(parameter.value),
                )
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
        return SetParametersResult(successful=True)

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
        self._refresh_vlm_safety_flags()

    def _on_input_source_status(self, msg: InputSourceStatus) -> None:
        source_id = str(msg.source_id or "").strip().lower()
        if not source_id:
            return
        self._input_source_status_by_id[source_id] = msg
        if source_id == "vlm":
            self._refresh_vlm_safety_flags()

    def _source_is_ready(self, source_id: str) -> bool:
        status = getattr(self, "_input_source_status_by_id", {}).get(source_id)
        if status is None:
            return True
        return bool(status.healthy and str(status.state).upper() == "READY")

    def _perception_gate_active(self) -> bool:
        # RF-DETR remains advisory. This gate concerns the VLM evidence stream
        # itself, so raw pixels may still be used when detection is disabled.
        self._refresh_vlm_safety_flags()
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
        self._perception_health_seen = True
        self._perception_enabled = bool(payload.get("enabled"))
        if not self._perception_enabled:
            self._twin.clear_object_detection_evidence()
        self._refresh_vlm_safety_flags()
        self._publish_world_state()

    def _refresh_vlm_safety_flags(self) -> None:
        required_topics = self._required_vlm_health_topics()
        if not required_topics:
            if hasattr(self._twin, "set_safety_flag"):
                self._twin.set_safety_flag("vlm_unhealthy", False)
            self._vlm_evidence_blocked = False
            return
        now = time.monotonic()
        unhealthy = False
        for topic in required_topics:
            sample = getattr(self, "_vlm_health_by_topic", {}).get(topic)
            if sample is None:
                unhealthy = True
                continue
            health, received_at = sample
            if now - received_at > float(
                getattr(self, "_vlm_health_timeout_sec", 6.0)
            ):
                unhealthy = True
                continue
            if not bool(health.connected and health.healthy) or bool(health.last_error):
                unhealthy = True
        if not self._source_is_ready("vlm"):
            unhealthy = True
        if unhealthy:
            self._clear_tool_prediction_state()
            self._mayo_retrieve_stability.clear()
            self._mayo_reuse_stability.clear()
            self._clear_vlm_implicit_request_state()
            if (
                not getattr(self, "_vlm_evidence_blocked", False)
                and hasattr(self._twin, "clear_perception_evidence")
            ):
                self._twin.clear_perception_evidence()
        self._vlm_evidence_blocked = unhealthy
        if hasattr(self._twin, "set_safety_flag"):
            self._twin.set_safety_flag("vlm_unhealthy", unhealthy)

    def _bundle_metadata_payload(self, spec) -> dict:
        requestable_instruments = [
            instrument.id for instrument in spec.bundle.instruments if getattr(instrument, "requestable", True)
        ]
        return {
            "id": spec.bundle.procedure_id,
            "display_name": spec.bundle.procedure_display_name,
            "display_name_ko": spec.bundle.procedure_display_name_ko,
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
            "instruments": [
                {
                    "id": instrument.id,
                    "display_name": instrument.display_name,
                    "display_name_ko": instrument.display_name_ko,
                    "aliases": list(instrument.aliases),
                    "category": instrument.category,
                    "inventory_count": int(getattr(instrument, "inventory_count", 1)),
                    "role": instrument.role,
                    "handover_profile": instrument.handover_profile,
                    "requestable": bool(getattr(instrument, "requestable", True)),
                }
                for instrument in spec.bundle.instruments
            ],
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

    def _publish_world_state(self) -> None:
        self._expire_bed_robot_controller_status()
        self._expire_stale_vlm_evidence(
            self._stamp_sec(self._stamp()),
        )
        self._refresh_vlm_safety_flags()
        self._twin.normalize_for_publish()
        world = WorldState()
        world.stamp = self._stamp()
        world.procedure_id = self._twin.state.procedure_id
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
                    },
                    "display_catalog": self._twin.spec.bundle.display_catalog,
                    "normal_phase_ids": list(self._twin.spec.normal_phase_ids),
                    "interrupt_phase_ids": list(self._twin.spec.interrupt_phase_ids),
                    "default_phase_id": self._twin.spec.default_phase_id,
                    "requestable_instruments": [
                        instrument.id
                        for instrument in self._twin.spec.bundle.instruments
                        if getattr(instrument, "requestable", True)
                    ],
                    "phases": [
                        {
                            "id": phase.id,
                            "display_name": phase.display_name,
                            "display_name_ko": phase.display_name_ko,
                        }
                        for phase in self._twin.spec.bundle.phases
                    ],
                    "instruments": [
                        {
                            "id": instrument.id,
                            "display_name": instrument.display_name,
                            "display_name_ko": instrument.display_name_ko,
                            "aliases": list(instrument.aliases),
                            "category": instrument.category,
                            "inventory_count": int(getattr(instrument, "inventory_count", 1)),
                            "role": instrument.role,
                            "handover_profile": instrument.handover_profile,
                            "requestable": bool(getattr(instrument, "requestable", True)),
                        }
                        for instrument in self._twin.spec.bundle.instruments
                    ],
                    "bundles": self._bundle_metadata_cache,
                },
            },
            sort_keys=True,
        )
        self._simulation_state_pub.publish(simulation)
        self._publish_perception_scene(world)
        self._publish_vlm_context(world)

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

    def _update_stability(
        self,
        tracker: dict[str, dict],
        *,
        tool_id: str,
        confidence: float,
        threshold: float,
        stability_sec: float,
        now_sec: float,
        received_sec: float | None = None,
    ) -> tuple[bool, float]:
        received_at = now_sec if received_sec is None else received_sec
        max_gap_sec = getattr(self, "_vlm_evidence_max_gap_sec", 2.5)
        if not tool_id or confidence < threshold:
            if tool_id:
                tracker.pop(tool_id, None)
            return (False, 0.0)
        entry = tracker.get(tool_id)
        if (
            entry is None
            or now_sec - float(entry.get("last_seen", now_sec))
            > max_gap_sec
        ):
            entry = {
                "first_seen": now_sec,
                "last_seen": now_sec,
                "last_received": received_at,
                "confidence": confidence,
            }
            tracker[tool_id] = entry
        else:
            last_seen = float(entry.get("last_seen", now_sec))
            entry["last_received"] = max(
                received_at,
                float(entry.get("last_received", received_at)),
            )
            if now_sec < last_seen:
                duration = max(
                    0.0,
                    last_seen - float(entry.get("first_seen", last_seen)),
                )
                return (duration >= stability_sec, duration)
            entry["last_seen"] = now_sec
            entry["confidence"] = confidence
        duration = max(0.0, now_sec - float(entry["first_seen"]))
        return (duration >= stability_sec, duration)

    def _clear_stale_tool_prediction(self, now_sec: float) -> None:
        max_gap_sec = getattr(self, "_vlm_evidence_max_gap_sec", 2.5)
        for tool_id, entry in list(self._tool_predict_stability.items()):
            last_received = float(
                entry.get("last_received", entry.get("last_seen", now_sec))
            )
            if (
                now_sec >= last_received
                and now_sec - last_received
                > max_gap_sec
            ):
                self._tool_predict_stability.pop(tool_id, None)
        if not self._twin.state.predicted_tool:
            self._twin.state.ranked_tool_predictions = []
            return
        entry = self._tool_predict_stability.get(self._twin.state.predicted_tool)
        if entry is None:
            self._twin.state.predicted_tool = ""
            self._twin.state.predicted_tool_confidence = 0.0
            self._twin.state.predicted_tool_stability_sec = 0.0
            self._twin.state.ranked_tool_predictions = []

    def _expire_stale_vlm_evidence(self, now_sec: float) -> None:
        """Withdraw visual facts even when the VLM stops publishing."""

        self._clear_stale_tool_prediction(now_sec)
        state = self._twin.state
        if not state.implicit_request_visible:
            if self._vlm_implicit_request_release_since is not None:
                self._release_vlm_implicit_request_episode(now_sec)
            return

        tracking_key = state.implicit_request_tool or "__unresolved__"
        entry = self._vlm_implicit_request_stability.get(tracking_key)
        max_gap_sec = getattr(self, "_vlm_evidence_max_gap_sec", 2.5)
        last_received = (
            float(entry.get("last_received", entry.get("last_seen", now_sec)))
            if entry is not None
            else None
        )
        if (
            last_received is None
            or (
                now_sec >= last_received
                and now_sec - last_received > max_gap_sec
            )
        ):
            self._release_vlm_implicit_request_episode(now_sec)

    def _clear_tool_prediction_state(self) -> None:
        self._tool_predict_stability.clear()
        getattr(
            self,
            "_tool_prediction_last_sample_by_source",
            {},
        ).clear()
        self._twin.state.predicted_tool = ""
        self._twin.state.predicted_tool_confidence = 0.0
        self._twin.state.predicted_tool_stability_sec = 0.0
        self._twin.state.ranked_tool_predictions = []

    def _tool_prediction_sample_status(
        self,
        *,
        source: str,
        now_sec: float,
        payload: dict,
    ) -> str:
        """Admit at most one temporal sample per source observation."""

        tracker = getattr(
            self,
            "_tool_prediction_last_sample_by_source",
            None,
        )
        if tracker is None:
            tracker = {}
            self._tool_prediction_last_sample_by_source = tracker
        source_key = source or "unknown"
        signature = json.dumps(
            self._vlm_tool_rows(payload),
            ensure_ascii=True,
            separators=(",", ":"),
        )
        previous = tracker.get(source_key)
        if previous is not None:
            previous_stamp, _previous_signature = previous
            if now_sec < previous_stamp - 1e-6:
                return "stale_out_of_order_tool_prediction"
            if abs(now_sec - previous_stamp) <= 1e-6:
                return "duplicate_tool_prediction_observation"
        tracker[source_key] = (now_sec, signature)
        return "accepted"

    def _clear_vlm_implicit_request_state(self) -> None:
        self._vlm_implicit_request_stability.clear()
        self._vlm_implicit_request_episode_tool = ""
        self._vlm_implicit_request_release_since = None
        state = self._twin.state
        state.implicit_request_visible = False
        state.implicit_request_tool = ""
        state.implicit_request_hand_pose = ""
        state.implicit_request_confidence = 0.0
        state.implicit_request_stability_sec = 0.0

    def _release_vlm_implicit_request_episode(self, now_sec: float) -> None:
        self._vlm_implicit_request_stability.clear()
        state = self._twin.state
        state.implicit_request_visible = False
        state.implicit_request_tool = ""
        state.implicit_request_hand_pose = ""
        state.implicit_request_confidence = 0.0
        state.implicit_request_stability_sec = 0.0
        if not self._vlm_implicit_request_episode_tool:
            self._vlm_implicit_request_release_since = None
            return
        if self._vlm_implicit_request_release_since is None:
            self._vlm_implicit_request_release_since = now_sec
            return
        if (
            now_sec - self._vlm_implicit_request_release_since
            >= self._vlm_implicit_request_release_sec
        ):
            self._vlm_implicit_request_episode_tool = ""
            self._vlm_implicit_request_release_since = None

    def _requestable_instrument(self, tool_id: str) -> bool:
        return any(
            instrument.id == tool_id
            and bool(getattr(instrument, "requestable", True))
            for instrument in self._twin.spec.bundle.instruments
        )

    def _handle_vlm_implicit_request(
        self,
        payload: dict,
        msg: VLMResult,
        now_sec: float,
        received_sec: float | None = None,
    ) -> None:
        """Validate visual gesture evidence without creating a surgeon request."""

        event_type = str(msg.gesture_event_type).strip().lower()
        hand_pose = str(msg.gesture_hand_pose).strip().lower()
        try:
            confidence = float(msg.gesture_confidence)
        except (TypeError, ValueError):
            confidence = 0.0
        raw_tool = str(msg.gesture_requested_tool).strip()
        tool_id = self._twin.spec.resolve_instrument_alias(raw_tool) or raw_tool
        is_visual_request = bool(
            event_type in {"request_tool", "handover"}
            and hand_pose == "open_receive"
            and confidence > 0.0
        )
        if not is_visual_request:
            self._release_vlm_implicit_request_episode(now_sec)
            return

        self._vlm_implicit_request_release_since = None
        tracking_key = tool_id or "__unresolved__"
        input_id = f"implicit_request:{tracking_key}:{now_sec:.3f}"
        if tool_id and not self._requestable_instrument(tool_id):
            self._vlm_implicit_request_stability.pop(tracking_key, None)
            self._release_vlm_implicit_request_episode(now_sec)
            self._publish_reducer_decision_event(
                input_type="vlm_implicit_request",
                input_id=input_id,
                input_source=msg.source,
                accepted=False,
                reason="implicit_request_tool_not_requestable",
                affected_tool=tool_id,
                detail={"confidence": confidence, "hand_pose": hand_pose},
            )
            return

        state = self._twin.state
        if not bool(state.running) or str(state.execution_state) != "running":
            self._release_vlm_implicit_request_episode(now_sec)
            self._publish_reducer_decision_event(
                input_type="vlm_implicit_request",
                input_id=input_id,
                input_source=msg.source,
                accepted=False,
                reason="implicit_request_runtime_not_running",
                affected_tool=tool_id,
                detail={
                    "confidence": confidence,
                    "execution_state": str(state.execution_state),
                },
            )
            return

        for tracked_tool in list(self._vlm_implicit_request_stability):
            if tracked_tool != tracking_key:
                self._vlm_implicit_request_stability.pop(tracked_tool, None)
        _, duration = self._update_stability(
            self._vlm_implicit_request_stability,
            tool_id=tracking_key,
            confidence=confidence,
            threshold=0.01,
            stability_sec=0.0,
            now_sec=now_sec,
            received_sec=received_sec,
        )

        if self._vlm_implicit_request_episode_tool != tracking_key:
            self._vlm_implicit_request_episode_tool = tracking_key
            state.implicit_request_generation += 1
        state.implicit_request_visible = True
        state.implicit_request_tool = tool_id
        state.implicit_request_hand_pose = hand_pose
        state.implicit_request_confidence = max(0.0, min(1.0, confidence))
        state.implicit_request_stability_sec = max(0.0, duration)
        self._publish_reducer_decision_event(
            input_type="vlm_implicit_request",
            input_id=input_id,
            input_source=msg.source,
            accepted=True,
            reason="verified_visual_open_palm_evidence",
            affected_tool=tool_id,
            detail={
                "confidence": confidence,
                "duration_sec": round(duration, 3),
                "detector_required": False,
                "policy_ready": bool(
                    tool_id
                    and confidence >= self._vlm_implicit_request_threshold
                    and duration >= self._vlm_implicit_request_stability_sec
                ),
                "tool_resolved": bool(tool_id),
                "request_created": False,
            },
        )
        self._publish_event(
            "VLMGestureEvidenceVerified",
            instrument_id=tool_id,
            detail={
                "source": "vlm_visual_gesture",
                "confidence": confidence,
                "hand_pose": hand_pose,
                "duration_sec": round(duration, 3),
            },
            target_owner="surgeon",
            mode="evidence_only",
        )

    def _vlm_tool_rows(self, payload: dict) -> list[list]:
        raw = payload.get("tool", [])
        if str(payload.get("v", "")) in {"3", "4"}:
            return [
                [str(item[0]), float(item[1])]
                for item in raw
                if isinstance(item, list) and len(item) == 2 and str(item[0])
            ]
        if isinstance(raw, list) and len(raw) == 2 and str(raw[0]):
            return [[str(raw[0]), float(raw[1])]]
        return []

    def _fused_tool_prediction(
        self,
        payload: dict,
        now_sec: float,
        received_sec: float | None = None,
    ) -> tuple[str, float, dict]:
        vlm_rows = self._vlm_tool_rows(payload)
        vlm_scores = {
            self._twin.spec.resolve_instrument_alias(str(tool_id)) or str(tool_id): float(confidence)
            for tool_id, confidence in vlm_rows
            if str(tool_id)
        }
        prior_result = self._prior_scorer.score(self._runtime_prior_evidence())
        prior = prior_result.get("tool", [])
        prior_evidence = prior_result.get("evidence", {})
        path_forecast = (
            prior_evidence.get("procedure_path_forecast", {})
            if isinstance(prior_evidence, dict)
            else {}
        )
        prior_scores = {
            self._twin.spec.resolve_instrument_alias(str(item[0])) or str(item[0]): float(item[1])
            for item in prior
            if isinstance(item, list) and len(item) == 2 and str(item[0])
        }
        # Procedure priors may nudge observed VLM candidates, but must never
        # invent an action candidate that the current perception result did not
        # propose.
        candidates = set(vlm_scores)
        path_tool = self._twin.spec.resolve_instrument_alias(
            str(path_forecast.get("tool", ""))
        ) or str(path_forecast.get("tool", ""))
        path_confidence = max(
            0.0,
            min(1.0, float(path_forecast.get("confidence", 0.0) or 0.0)),
        )
        path_instances = self._twin._instances_for_type(path_tool) if path_tool else []
        path_lifecycles = {
            str(getattr(state, "lifecycle_stage", "") or "")
            for state in path_instances
        }
        path_available = bool(
            path_lifecycles.intersection(
                {"home_rack", "returned_home", "mayo_reuse", "prepositioned_right"}
            )
        )
        if (
            path_tool
            and path_confidence >= self._tool_predict_evidence_threshold
            and path_available
        ):
            candidates.add(path_tool)
        fused_scores: dict[str, float] = {}
        candidate_lifecycles: dict[str, list[str]] = {}
        for tool_id in sorted(candidates):
            instances = self._twin._instances_for_type(tool_id)
            if not instances:
                continue
            candidate_lifecycles[tool_id] = sorted(
                {
                    str(getattr(state, "lifecycle_stage", "") or "")
                    for state in instances
                }
            )
            vlm_score = max(0.0, min(1.0, float(vlm_scores.get(tool_id, 0.0))))
            prior_score = max(0.0, min(1.0, float(prior_scores.get(tool_id, 0.0))))
            remaining = 1.0 - vlm_score
            prior_nudge = 0.15 * prior_score * remaining
            agreement_nudge = (
                0.05 * remaining
                if vlm_score >= 0.35 and prior_score >= 0.35
                else 0.0
            )
            fused_scores[tool_id] = min(
                1.0,
                vlm_score + prior_nudge + agreement_nudge,
            )
            if tool_id == path_tool and path_available:
                fused_scores[tool_id] = max(
                    fused_scores[tool_id],
                    path_confidence,
                )
        if not fused_scores:
            return "", 0.0, {
                "vlm": vlm_scores,
                "prior": prior_scores,
                "candidate_lifecycles": candidate_lifecycles,
                "fused": {},
                "procedure_path_forecast": path_forecast,
                "path_available": path_available,
            }

        eligible_scores: dict[str, float] = {}
        for tool_id, confidence in fused_scores.items():
            if confidence >= self._tool_predict_evidence_threshold:
                eligible_scores[tool_id] = confidence
        if not eligible_scores:
            self._tool_predict_stability.clear()
            return "", 0.0, {
                "vlm": vlm_scores,
                "prior": prior_scores,
                "candidate_lifecycles": candidate_lifecycles,
                "fused": fused_scores,
                "durations_sec": {
                    tool_id: 0.0 for tool_id in fused_scores
                },
                "procedure_path_forecast": path_forecast,
                "path_available": path_available,
            }

        selected_tool, selected_confidence = max(
            eligible_scores.items(),
            key=lambda item: item[1],
        )
        # Readiness measures continuity of the current top candidate. A
        # different winner invalidates the previous candidate's preparation
        # clock instead of letting intermittent ranked appearances accumulate.
        for tracked_tool in list(self._tool_predict_stability):
            if tracked_tool != selected_tool:
                self._tool_predict_stability.pop(tracked_tool, None)
        _, selected_duration = self._update_stability(
            self._tool_predict_stability,
            tool_id=selected_tool,
            confidence=selected_confidence,
            threshold=self._tool_predict_evidence_threshold,
            stability_sec=self._tool_predict_stability_sec,
            now_sec=now_sec,
            received_sec=received_sec,
        )
        durations = {
            tool_id: (
                selected_duration if tool_id == selected_tool else 0.0
            )
            for tool_id in fused_scores
        }
        vlm_top = max(vlm_scores.items(), key=lambda item: item[1])[0] if vlm_scores else ""
        prior_top = max(prior_scores.items(), key=lambda item: item[1])[0] if prior_scores else ""
        strong_new_consensus = bool(
            selected_tool
            and selected_tool == vlm_top
            and selected_tool == prior_top
        )
        return selected_tool, selected_confidence, {
            "vlm": vlm_scores,
            "prior": prior_scores,
            "candidate_lifecycles": candidate_lifecycles,
            "fused": fused_scores,
            "durations_sec": durations,
            "selected": selected_tool,
            "selected_duration_sec": durations.get(selected_tool, 0.0),
            "vlm_top": vlm_top,
            "prior_top": prior_top,
            "strong_new_consensus": strong_new_consensus,
            "procedure_path_forecast": path_forecast,
            "path_available": path_available,
        }

    def _handle_vlm_tool_prediction(
        self,
        payload: dict,
        msg: VLMResult,
        now_sec: float,
        received_sec: float | None = None,
    ) -> None:
        if (
            not self._twin.state.predicted_tool
            and not getattr(self._twin.state, "ranked_tool_predictions", [])
        ):
            # A reset/interrupt may clear reducer state outside this callback.
            # Never carry the previous run's continuity clock into a new rank 1.
            self._tool_predict_stability.clear()
        sample_status = self._tool_prediction_sample_status(
            source=str(msg.source),
            now_sec=now_sec,
            payload=payload,
        )
        if sample_status != "accepted":
            self._publish_reducer_decision_event(
                input_type="vlm_tool_prediction",
                input_id=f"tool_prediction_ignored:{now_sec:.3f}",
                input_source=msg.source,
                accepted=False,
                reason=sample_status,
                detail={"tool_rows": self._vlm_tool_rows(payload)},
            )
            return

        legacy_v2 = bool(
            str(payload.get("v", "")) == "2"
            and "real_vlm" not in str(msg.source)
        )
        if legacy_v2:
            raw_tool = payload.get("tool", ["", 0.0])
            if not isinstance(raw_tool, list) or len(raw_tool) != 2:
                self._clear_stale_tool_prediction(now_sec)
                return
            tool_id = self._twin.spec.resolve_instrument_alias(str(raw_tool[0])) or str(raw_tool[0])
            try:
                confidence = float(raw_tool[1])
            except (TypeError, ValueError):
                self._clear_stale_tool_prediction(now_sec)
                return
            fusion_detail = {"legacy_v2": True, "tool": tool_id, "confidence": confidence}
        else:
            tool_id, confidence, fusion_detail = self._fused_tool_prediction(
                payload,
                now_sec,
                received_sec,
            )
        if not tool_id:
            self._clear_stale_tool_prediction(now_sec)
            return
        if legacy_v2:
            _, duration = self._update_stability(
                self._tool_predict_stability,
                tool_id=tool_id,
                confidence=confidence,
                threshold=self._tool_predict_evidence_threshold,
                stability_sec=self._tool_predict_stability_sec,
                now_sec=now_sec,
                received_sec=received_sec,
            )
        else:
            duration = float(
                fusion_detail.get("selected_duration_sec", 0.0)
            )
        policy_ready = bool(
            confidence >= self._tool_predict_threshold
            and duration >= self._tool_predict_stability_sec
        )
        self._clear_stale_tool_prediction(now_sec)
        changed = self._twin.state.predicted_tool != tool_id
        self._twin.state.predicted_tool = tool_id
        self._twin.state.predicted_tool_confidence = confidence
        self._twin.state.predicted_tool_stability_sec = duration
        if legacy_v2:
            ranked_rows = [(tool_id, confidence)]
        else:
            ranked_rows = sorted(
                (
                    (candidate_id, float(candidate_confidence))
                    for candidate_id, candidate_confidence in fusion_detail.get(
                        "fused", {}
                    ).items()
                    if float(candidate_confidence)
                    >= self._tool_predict_evidence_threshold
                ),
                key=lambda item: (-item[1], item[0]),
            )[:3]
        self._twin.state.ranked_tool_predictions = [
            RankedToolPredictionBelief(
                rank=rank,
                instrument_id=candidate_id,
                confidence=candidate_confidence,
                stability_sec=duration if rank == 1 else 0.0,
            )
            for rank, (candidate_id, candidate_confidence) in enumerate(
                ranked_rows, start=1
            )
        ]
        self._publish_reducer_decision_event(
            input_type="vlm_tool_prediction",
            input_id=f"tool_prediction:{tool_id}:{now_sec:.3f}",
            input_source=msg.source,
            accepted=True,
            reason="verified_tool_prediction_evidence",
            affected_tool=tool_id,
            detail={
                "confidence": confidence,
                "duration_sec": round(duration, 3),
                "evidence_confidence_threshold": self._tool_predict_evidence_threshold,
                "policy_confidence_threshold": self._tool_predict_threshold,
                "threshold_sec": self._tool_predict_stability_sec,
                "policy_ready": policy_ready,
                "fusion": fusion_detail,
            },
        )
        if changed:
            self._publish_event(
                "VLMToolPredictionEvidenceUpdated",
                instrument_id=tool_id,
                confidence=confidence,
                detail={
                    "source": msg.source,
                    "duration_sec": round(duration, 3),
                    "evidence_confidence_threshold": self._tool_predict_evidence_threshold,
                    "policy_confidence_threshold": self._tool_predict_threshold,
                    "threshold_sec": self._tool_predict_stability_sec,
                    "policy_ready": policy_ready,
                },
                mode="evidence_only",
            )

    def _on_vlm_result(self, msg: VLMResult) -> None:
        source = str(msg.source or "unknown_vlm")
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
        if str(msg.schema_version) not in {"2", "3", "4"}:
            return
        try:
            payload = json.loads(msg.raw_json)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict) or str(payload.get("v", "")) not in {"2", "3", "4"}:
            return
        now_sec = self._stamp_sec(msg.stamp)
        received_sec = self._stamp_sec(self._stamp())
        self._handle_vlm_tool_prediction(
            payload,
            msg,
            now_sec,
            received_sec,
        )
        self._handle_vlm_implicit_request(
            payload,
            msg,
            now_sec,
            received_sec,
        )
        mayo_rows: dict[str, tuple[str, float]] = {}
        for item in payload.get("mayo", []):
            if not isinstance(item, list) or len(item) != 3:
                continue
            tool_id = self._twin.spec.resolve_instrument_alias(str(item[0])) or str(item[0])
            decision = str(item[1]).strip().lower()
            try:
                confidence = float(item[2])
            except (TypeError, ValueError):
                continue
            if tool_id and decision in {"recover", "reuse"}:
                mayo_rows[tool_id] = (decision, confidence)

        retrieve = payload.get("mayo_retrieve", ["", 0.0])
        if isinstance(retrieve, list) and len(retrieve) == 2:
            retrieve_tool = (
                self._twin.spec.resolve_instrument_alias(str(retrieve[0]))
                or str(retrieve[0])
            )
            try:
                retrieve_confidence = float(retrieve[1])
            except (TypeError, ValueError):
                retrieve_confidence = 0.0
            if retrieve_tool and retrieve_tool not in mayo_rows:
                mayo_rows[retrieve_tool] = ("recover", retrieve_confidence)

        observed_tools = set(mayo_rows)
        for tool_id, (decision, confidence) in mayo_rows.items():
            if decision == "reuse":
                tracker = self._mayo_reuse_stability
                opposite_tracker = self._mayo_retrieve_stability
                threshold = self._mayo_reuse_threshold
            else:
                tracker = self._mayo_retrieve_stability
                opposite_tracker = self._mayo_reuse_stability
                threshold = self._mayo_retrieve_threshold
            opposite_tracker.pop(tool_id, None)
            stable, duration = self._update_stability(
                tracker,
                tool_id=tool_id,
                confidence=confidence,
                threshold=threshold,
                stability_sec=self._mayo_stability_sec,
                now_sec=now_sec,
                received_sec=received_sec,
            )
            current_state = self._twin.get_instrument_state(
                tool_id,
                allowed_lifecycles={"mayo_reuse", "mayo_recovery"},
            )
            instance_id = current_state.instance_id if current_state else tool_id
            proposal_id = (
                f"mayo_policy:{instance_id}:{decision}:"
                f"{now_sec:.3f}:{confidence:.2f}"
            )
            result = self._twin.record_mayo_policy_evidence(
                instrument_id=instance_id,
                evidence_type=decision,
                confidence=confidence,
                stability_sec=duration,
                source="vlm_mayo_policy",
                proposal_id=proposal_id,
                stamp_sec=now_sec,
            )
            if not result:
                continue
            result["policy_ready"] = bool(stable)
            self._publish_vlm_reducer_decision(result)
            self._publish_event(
                "VLMMayoPolicyEvidenceVerified",
                instrument_id=tool_id,
                instance_id=str(result.get("instance_id", "")),
                location_id=str(result.get("location_id", "mayo_stand")),
                location_type=str(result.get("location_type", "mayo_stand")),
                confidence=confidence,
                detail=result,
                mode="evidence_only",
            )

        for tracker in (
            self._mayo_retrieve_stability,
            self._mayo_reuse_stability,
        ):
            for tracked_tool, entry in list(tracker.items()):
                if (
                    tracked_tool not in observed_tools
                    and now_sec - float(entry.get("last_seen", now_sec)) > 2.5
                ):
                    tracker.pop(tracked_tool, None)
                    self._twin.clear_mayo_policy_evidence(tracked_tool)
        self._publish_world_state()

    def _on_observation(self, msg: ToolObservation) -> None:
        source = (
            str(getattr(msg, "source", ""))
            or "vlm_cam4_mayo_observation"
            if msg.location_type == "mayo_stand"
            else "legacy_tool_observation"
        )
        self._reconcile_tool_observation(msg, source=source)

    def _on_cam4_mayo_observation(
        self,
        msg: ToolObservation,
    ) -> None:
        self._reconcile_tool_observation(
            msg,
            source="cam4_rfdetr_mayo_observation",
        )

    def _reconcile_tool_observation(
        self,
        msg: ToolObservation,
        *,
        source: str,
    ) -> None:
        resolved_source = str(getattr(msg, "source", "")) or source
        is_cam4_detector = source == "cam4_rfdetr_mayo_observation"
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
            f"{resolved_tool}:{msg.location_type}:{msg.location_id}"
        )
        if not self._admit_visual_evidence(
            msg,
            channel=admission_channel,
            source=resolved_source,
            require_epoch=(
                not is_cam4_detector
                and str(getattr(self, "_vlm_mode", "mock"))
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
        )
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
        self._twin.apply_event(msg)
        if msg.event_type == "ToolHandoverCompleted" and msg.instrument_id:
            self._append_tool_history(
                "_completed_handover_history",
                msg.instrument_id,
                msg.stamp,
            )
        try:
            detail = json.loads(msg.detail_json) if msg.detail_json else {}
            if not isinstance(detail, dict):
                detail = {"detail": detail}
        except Exception:
            detail = {"detail_json": msg.detail_json}
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

    def _expire_bed_robot_controller_status(self) -> None:
        received_at = self._bed_robot_status_received_monotonic
        source_stamp_ns = self._bed_robot_status_source_stamp_ns
        if received_at <= 0.0 or source_stamp_ns is None:
            return
        receipt_age_sec = self._monotonic_sec() - received_at
        source_age_sec = self._bed_robot_controller_source_age_sec(source_stamp_ns)
        if (
            receipt_age_sec <= self._bed_robot_status_timeout_sec
            and source_age_sec <= self._bed_robot_source_max_age_sec
            and source_age_sec >= -self._bed_robot_source_future_tolerance_sec
        ):
            return
        self._reset_bed_robot_controller_freshness()
        if self._twin.expire_bed_robot_arm_controller_status():
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

    def _on_bed_robot_arm_group_status(self, msg: BedRobotArmGroupStatus) -> None:
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

    def _on_request(self, msg: String) -> None:
        resolved = ""
        shadow_assumptions: list[dict] = []
        completion_requested = self._twin.is_explicit_procedure_completion_request(
            msg.data
        )
        if completion_requested:
            request = SurgeonRequest()
            request.stamp = self._stamp()
            request.event_type = "request_procedure_completion"
            request.voice_text = msg.data
            request.note = "explicit public voice completion signal"
            self._twin.update_surgeon_request(request)
            self._clear_tool_prediction_state()
        elif self._twin.is_explicit_voice_tool_request(msg.data):
            resolved = self._twin.update_explicit_request(msg.data)
            shadow_assumptions = self._twin.drain_shadow_assumption_audit()
            if resolved:
                self._append_tool_history(
                    "_validated_tool_request_history",
                    resolved,
                    self._stamp(),
                )
                self._clear_tool_prediction_state()
        for index, assumption in enumerate(shadow_assumptions):
            event_type = str(assumption.get("event_type", "shadow_assumption"))
            self._publish_reducer_decision_event(
                input_type="shadow_state_assumption",
                input_id=f"{event_type}:{resolved}:{index}",
                input_source="public_voice_request",
                accepted=True,
                reason=str(assumption.get("reason", "")),
                affected_tool=str(
                    assumption.get("instrument_id")
                    or assumption.get("incoming_request_tool")
                    or resolved
                ),
                detail=assumption,
            )
        self._publish_event(
            "VoiceTranscriptObserved",
            instrument_id=resolved,
            detail={
                "text": msg.data,
                "resolved_tool": resolved,
                "command_type": (
                    "procedure_completion"
                    if completion_requested
                    else "tool_request"
                    if resolved
                    else "observation"
                ),
                "shadow_assumptions": shadow_assumptions,
            },
            mode="voice_request",
        )
        if completion_requested or resolved:
            self._publish_world_state()

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
        if command in {
            "start",
            "start_runtime",
            "pause",
            "resume",
            "stop",
            "reset",
        }:
            self._advance_visual_runtime_epoch()
        if command in {"start", "start_runtime"}:
            self._pending_bed_robot_arm_group_requests.clear()
            self._clear_vlm_implicit_request_state()
            self._clear_tool_histories()
            self._twin.reset_spec(self._twin.spec, seed_from_perception=False)
            self._reset_bed_robot_controller_freshness()
            self._stamp_all_bed_robot_arm_groups()
            self._phase_entered_ros_sec = self._stamp_sec(self._stamp())
            if start_phase_id:
                self._twin.set_initial_phase(start_phase_id)
                self._phase_entered_ros_sec = self._stamp_sec(self._stamp())
            self._twin.set_execution_state(True, "running")
        elif command == "pause":
            self._clear_vlm_implicit_request_state()
            self._twin.set_execution_state(True, "paused")
        elif command == "resume":
            self._twin.set_execution_state(True, "running")
        elif command == "stop":
            self._pending_bed_robot_arm_group_requests.clear()
            self._clear_vlm_implicit_request_state()
            self._twin.set_execution_state(False, "halted")
        elif command == "reset":
            self._pending_bed_robot_arm_group_requests.clear()
            self._clear_vlm_implicit_request_state()
            self._clear_tool_histories()
            self._twin.reset_runtime()
            self._reset_bed_robot_controller_freshness()
            self._stamp_all_bed_robot_arm_groups()
            self._phase_entered_ros_sec = self._stamp_sec(self._stamp())
            self._twin.set_execution_state(False, "idle")
        self._publish_world_state()


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
