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
from surgical_msgs.msg import (
    FilteredPhase,
    InstrumentState,
    BTContextSnapshot,
    EventDigest,
    PerceptionScene,
    PhaseEvidence,
    PhaseTransitionCue,
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
    }

    def __init__(self) -> None:
        super().__init__("or_digital_twin")
        self.declare_parameter("spec_dir", str(get_default_spec_dir()))
        self.declare_parameter("vlm_recent_event_count", 6)
        self.declare_parameter("validation_mode", "bt_twin")
        self.declare_parameter("phase_authority", "reducer")
        self.declare_parameter("vlm_mode", "mock")
        self.declare_parameter("vlm_health_timeout_sec", 6.0)
        self.declare_parameter("mayo_retrieve_confidence_threshold", 0.5)
        self.declare_parameter("mayo_reuse_suppress_threshold", 0.5)
        self.declare_parameter("mayo_stability_sec", 5.0)
        self.declare_parameter("tool_predict_confidence_threshold", 0.8)
        self.declare_parameter("tool_predict_stability_sec", 3.0)
        self._spec_dir = str(self.get_parameter("spec_dir").value)
        self._vlm_recent_event_count = max(1, int(self.get_parameter("vlm_recent_event_count").value))
        self._validation_mode = str(self.get_parameter("validation_mode").value)
        self._phase_authority = str(self.get_parameter("phase_authority").value)
        self._vlm_mode = str(self.get_parameter("vlm_mode").value)
        self._vlm_health_timeout_sec = max(0.5, float(self.get_parameter("vlm_health_timeout_sec").value))
        self._mayo_retrieve_threshold = float(self.get_parameter("mayo_retrieve_confidence_threshold").value)
        self._mayo_reuse_threshold = float(self.get_parameter("mayo_reuse_suppress_threshold").value)
        self._mayo_stability_sec = max(0.1, float(self.get_parameter("mayo_stability_sec").value))
        self._tool_predict_threshold = float(self.get_parameter("tool_predict_confidence_threshold").value)
        self._tool_predict_stability_sec = max(0.1, float(self.get_parameter("tool_predict_stability_sec").value))
        self._twin = ORDigitalTwin(load_bundle(self._spec_dir))
        self._prior_scorer = ProcedurePriorScorer(self._twin.spec, compact_procedure_prompt(self._spec_dir))
        self._bundle_metadata_cache = self._build_bundle_metadata()
        self._important_events: deque[SimulationEvent] = deque(maxlen=self._vlm_recent_event_count)
        self._latest_outward_signal: SurgeonOutwardSignal | None = None
        self._vlm_health_by_topic: dict[str, tuple[VLMHealth, float]] = {}
        self._mayo_retrieve_stability: dict[str, dict] = {}
        self._mayo_reuse_stability: dict[str, dict] = {}
        self._mayo_promoted_tools: set[str] = set()
        self._tool_predict_stability: dict[str, dict] = {}
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
        self.create_subscription(VLMResult, "/vlm/result", self._on_vlm_result, 20)
        self.create_subscription(VLMResult, "/vlm_real/result", self._on_vlm_result, 20)
        self.create_subscription(VLMHealth, "/vlm/health", lambda msg: self._on_vlm_health("/vlm/health", msg), 10)
        self.create_subscription(
            VLMHealth,
            "/vlm_real/health",
            lambda msg: self._on_vlm_health("/vlm_real/health", msg),
            10,
        )
        self.create_subscription(TwinEvent, "/skill/events", self._on_skill_event, 50)
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
                    self._prior_scorer = ProcedurePriorScorer(self._twin.spec, compact_procedure_prompt(self._spec_dir))
                    self._bundle_metadata_cache = self._build_bundle_metadata()
                    self._important_events.clear()
                    self._mayo_retrieve_stability.clear()
                    self._mayo_reuse_stability.clear()
                    self._mayo_promoted_tools.clear()
                    self._tool_predict_stability.clear()
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
            elif parameter.name == "tool_predict_confidence_threshold":
                self._tool_predict_threshold = float(parameter.value)
            elif parameter.name == "tool_predict_stability_sec":
                self._tool_predict_stability_sec = max(0.1, float(parameter.value))
        return SetParametersResult(successful=True)

    def _stamp(self):
        return self.get_clock().now().to_msg()

    def _required_vlm_health_topics(self) -> list[str]:
        if self._vlm_mode == "real":
            return ["/vlm/health"]
        if self._vlm_mode == "dual":
            return ["/vlm_real/health"]
        return []

    def _on_vlm_health(self, topic: str, msg: VLMHealth) -> None:
        self._vlm_health_by_topic[topic] = (msg, time.monotonic())
        self._refresh_vlm_safety_flags()

    def _refresh_vlm_safety_flags(self) -> None:
        required_topics = self._required_vlm_health_topics()
        if not required_topics:
            self._twin.set_safety_flag("vlm_unhealthy", False)
            return
        now = time.monotonic()
        unhealthy = False
        for topic in required_topics:
            sample = self._vlm_health_by_topic.get(topic)
            if sample is None:
                unhealthy = True
                continue
            health, received_at = sample
            if now - received_at > self._vlm_health_timeout_sec:
                unhealthy = True
                continue
            if not bool(health.connected and health.healthy) or bool(health.last_error):
                unhealthy = True
        self._twin.set_safety_flag("vlm_unhealthy", unhealthy)

    def _bundle_metadata_payload(self, spec) -> dict:
        requestable_instruments = [
            instrument.id for instrument in spec.bundle.instruments if getattr(instrument, "requestable", True)
        ]
        return {
            "id": spec.bundle.procedure_id,
            "display_name": spec.bundle.procedure_display_name,
            "display_name_ko": spec.bundle.procedure_display_name_ko,
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
        world.left_hand_tool = self._twin.state.left_hand_tool
        world.prepositioned_tool = self._twin.state.prepositioned_tool
        world.predicted_tool = self._twin.state.predicted_tool
        world.predicted_tool_confidence = float(self._twin.state.predicted_tool_confidence)
        world.predicted_tool_stability_sec = float(self._twin.state.predicted_tool_stability_sec)
        world.surgeon_intent = self._twin.state.surgeon_intent
        world.surgeon_request_tool = self._twin.state.surgeon_request_tool
        world.surgeon_ready_for_handover = bool(self._twin.state.surgeon_ready_for_handover)
        world.surgeon_ready_for_retrieval = bool(self._twin.state.surgeon_ready_for_retrieval)
        world.cleaner_busy = bool(self._twin.state.cleaner_busy)
        world.cleaner_remaining_sec = float(self._twin.state.cleaner_remaining_sec)
        world.pending_transition_tools = list(self._twin.state.pending_transition_tools)
        world.active_recovery_tools = list(self._twin.state.active_recovery_tools)
        active_task = self._twin.state.active_robot_task
        world.active_robot_task_id = active_task.task_id if active_task else ""
        world.active_robot_task_type = active_task.task_type if active_task else ""
        world.active_robot_task_tool_id = active_task.instrument_id if active_task else ""
        world.active_robot_task_arm = active_task.arm if active_task else ""
        world.active_robot_task_source_anchor = active_task.source_anchor_id if active_task else ""
        world.active_robot_task_target_anchor = active_task.target_anchor_id if active_task else ""
        world.active_robot_task_progress = float(active_task.progress) if active_task else 0.0
        world.active_robot_task_remaining_sec = float(active_task.remaining_sec) if active_task else 0.0
        world.instrument_states = []
        for payload in self._twin.instrument_payload():
            msg = InstrumentState()
            msg.stamp = self._stamp()
            msg.instrument_id = payload["instrument_id"]
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
        simulation.surgeon_ready_for_handover = bool(self._twin.state.surgeon_ready_for_handover)
        simulation.surgeon_ready_for_retrieval = bool(self._twin.state.surgeon_ready_for_retrieval)
        simulation.cleaner_busy = bool(self._twin.state.cleaner_busy)
        simulation.cleaner_remaining_sec = float(self._twin.state.cleaner_remaining_sec)
        simulation.pending_transition_tools = list(self._twin.state.pending_transition_tools)
        simulation.active_recovery_tools = list(self._twin.state.active_recovery_tools)
        simulation.right_hand_tool = self._twin.state.right_hand_tool
        simulation.left_hand_tool = self._twin.state.left_hand_tool
        simulation.prepositioned_tool = self._twin.state.prepositioned_tool
        simulation.active_robot_task_id = active_task.task_id if active_task else ""
        simulation.active_robot_task_type = active_task.task_type if active_task else ""
        simulation.active_robot_task_tool_id = active_task.instrument_id if active_task else ""
        simulation.active_robot_task_arm = active_task.arm if active_task else ""
        simulation.active_robot_task_source_anchor = active_task.source_anchor_id if active_task else ""
        simulation.active_robot_task_target_anchor = active_task.target_anchor_id if active_task else ""
        simulation.active_robot_task_progress = float(active_task.progress) if active_task else 0.0
        simulation.active_robot_task_remaining_sec = float(active_task.remaining_sec) if active_task else 0.0
        simulation.recent_events = list(self._twin.state.recent_event_types)
        simulation.instrument_states = list(world.instrument_states)
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
            tool_id
            for tool_id, state in self._twin.instrument_states.items()
            if state.lifecycle_stage == "surgeon_owned"
        )
        mayo_tools = [
            tool_id
            for tool_id, state in self._twin.instrument_states.items()
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
        recent_tools = [
            {"tool": event["tool"], "at": event["stamp_sec"]}
            for event in events
            if event.get("tool")
        ]
        return {
            "current_phase": self._twin._active_context_phase_id(),
            "phase_entered_sec": self._phase_entered_ros_sec,
            "recent_tools": recent_tools,
            "mayo_tools": mayo_tools,
            "hand_tools": hand_tools,
            "events": events,
        }

    def _fuse_phase_evidence(self, msg: PhaseEvidence) -> PhaseEvidence:
        if "real_vlm" not in str(msg.source):
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
        candidates = set(prior_scores) | set(vlm_scores) | {current_phase}
        fused_scores: dict[str, float] = {}
        for phase_id in candidates:
            vlm_score = float(vlm_scores.get(phase_id, 0.0))
            prior_score = float(prior_scores.get(phase_id, 0.0))
            agreement = 0.08 if vlm_score >= 0.35 and prior_score >= 0.35 else 0.0
            fused_scores[phase_id] = min(1.0, 0.68 * vlm_score + 0.34 * prior_score + agreement)
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
    ) -> tuple[bool, float]:
        if not tool_id or confidence < threshold:
            if tool_id:
                tracker.pop(tool_id, None)
            return (False, 0.0)
        entry = tracker.get(tool_id)
        if entry is None or now_sec - float(entry.get("last_seen", now_sec)) > 2.5:
            entry = {"first_seen": now_sec, "last_seen": now_sec, "confidence": confidence}
            tracker[tool_id] = entry
        else:
            entry["last_seen"] = now_sec
            entry["confidence"] = confidence
        duration = now_sec - float(entry["first_seen"])
        return (duration >= stability_sec, duration)

    def _stable_reuse_tools(self, now_sec: float) -> set[str]:
        stable = set()
        for tool_id, entry in list(self._mayo_reuse_stability.items()):
            if now_sec - float(entry.get("last_seen", now_sec)) > 2.5:
                self._mayo_reuse_stability.pop(tool_id, None)
                continue
            if now_sec - float(entry.get("first_seen", now_sec)) >= self._mayo_stability_sec:
                stable.add(tool_id)
        return stable

    def _clear_stale_tool_prediction(self, now_sec: float) -> None:
        for tool_id, entry in list(self._tool_predict_stability.items()):
            if now_sec - float(entry.get("last_seen", now_sec)) > 2.5:
                self._tool_predict_stability.pop(tool_id, None)
        if not self._twin.state.predicted_tool:
            return
        entry = self._tool_predict_stability.get(self._twin.state.predicted_tool)
        if entry is None:
            self._twin.state.predicted_tool = ""
            self._twin.state.predicted_tool_confidence = 0.0
            self._twin.state.predicted_tool_stability_sec = 0.0

    def _clear_tool_prediction_state(self) -> None:
        self._tool_predict_stability.clear()
        self._twin.state.predicted_tool = ""
        self._twin.state.predicted_tool_confidence = 0.0
        self._twin.state.predicted_tool_stability_sec = 0.0

    def _tool_available_for_prediction(self, tool_id: str) -> bool:
        current_state = self._twin.instrument_states.get(tool_id)
        return bool(
            current_state is not None
            and not current_state.contaminated
            and current_state.lifecycle_stage in {"home_rack", "returned_home"}
        )

    def _vlm_tool_rows(self, payload: dict) -> list[list]:
        raw = payload.get("tool", [])
        if str(payload.get("v", "")) == "3":
            return [
                [str(item[0]), float(item[1])]
                for item in raw
                if isinstance(item, list) and len(item) == 2 and str(item[0])
            ]
        if isinstance(raw, list) and len(raw) == 2 and str(raw[0]):
            return [[str(raw[0]), float(raw[1])]]
        return []

    def _fused_tool_prediction(self, payload: dict, now_sec: float) -> tuple[str, float, dict]:
        vlm_rows = self._vlm_tool_rows(payload)
        vlm_scores = {
            self._twin.spec.resolve_instrument_alias(str(tool_id)) or str(tool_id): float(confidence)
            for tool_id, confidence in vlm_rows
            if str(tool_id)
        }
        prior = self._prior_scorer.score(self._runtime_prior_evidence()).get("tool", [])
        prior_scores = {
            self._twin.spec.resolve_instrument_alias(str(item[0])) or str(item[0]): float(item[1])
            for item in prior
            if isinstance(item, list) and len(item) == 2 and str(item[0])
        }
        candidates = sorted(set(vlm_scores) | set(prior_scores))
        fused_scores: dict[str, float] = {}
        unavailable: dict[str, str] = {}
        for tool_id in candidates:
            if tool_id not in self._twin.instrument_states:
                continue
            if not self._tool_available_for_prediction(tool_id):
                state = self._twin.instrument_states.get(tool_id)
                unavailable[tool_id] = state.lifecycle_stage if state is not None else "missing"
                continue
            vlm_score = float(vlm_scores.get(tool_id, 0.0))
            prior_score = float(prior_scores.get(tool_id, 0.0))
            agreement = 0.15 if vlm_score >= 0.35 and prior_score >= 0.35 else 0.0
            fused_scores[tool_id] = min(1.0, 0.15 + 0.62 * vlm_score + 0.28 * prior_score + agreement)
        if not fused_scores:
            return "", 0.0, {"vlm": vlm_scores, "prior": prior_scores, "unavailable": unavailable, "fused": {}}
        best_tool, best_confidence = max(fused_scores.items(), key=lambda item: item[1])
        selected_tool = best_tool
        selected_confidence = best_confidence
        vlm_top = max(vlm_scores.items(), key=lambda item: item[1])[0] if vlm_scores else ""
        prior_top = max(prior_scores.items(), key=lambda item: item[1])[0] if prior_scores else ""
        strong_new_consensus = bool(best_tool and best_tool == vlm_top and best_tool == prior_top)
        sticky_margin = 0.06 if strong_new_consensus else 0.12
        for tracked_tool, entry in self._tool_predict_stability.items():
            if tracked_tool != best_tool and strong_new_consensus:
                continue
            if now_sec - float(entry.get("last_seen", now_sec)) > 2.5:
                continue
            tracked_score = fused_scores.get(tracked_tool)
            if tracked_score is None:
                continue
            if tracked_score >= best_confidence - sticky_margin:
                selected_tool = tracked_tool
                selected_confidence = tracked_score
                break
        return selected_tool, selected_confidence, {
            "vlm": vlm_scores,
            "prior": prior_scores,
            "unavailable": unavailable,
            "fused": fused_scores,
            "selected": selected_tool,
            "vlm_top": vlm_top,
            "prior_top": prior_top,
            "strong_new_consensus": strong_new_consensus,
        }

    def _handle_vlm_tool_prediction(self, payload: dict, msg: VLMResult, now_sec: float) -> None:
        if str(payload.get("v", "")) == "2" and "real_vlm" not in str(msg.source):
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
            tool_id, confidence, fusion_detail = self._fused_tool_prediction(payload, now_sec)
        if not tool_id:
            self._clear_stale_tool_prediction(now_sec)
            if fusion_detail.get("unavailable"):
                self._publish_reducer_decision_event(
                    input_type="vlm_tool_prediction",
                    input_id=f"tool_prediction:none:{now_sec:.3f}",
                    input_source=msg.source,
                    accepted=False,
                    reason="no_available_fused_tool_prediction",
                    detail=fusion_detail,
                )
            return
        current_prediction = self._twin.state.predicted_tool
        if (
            current_prediction
            and current_prediction != tool_id
            and (
                bool(fusion_detail.get("strong_new_consensus"))
                or confidence >= self._tool_predict_threshold
            )
        ):
            self._tool_predict_stability.pop(current_prediction, None)
            self._twin.state.predicted_tool = ""
            self._twin.state.predicted_tool_confidence = 0.0
            self._twin.state.predicted_tool_stability_sec = 0.0
            self._publish_reducer_decision_event(
                input_type="vlm_tool_prediction",
                input_id=f"tool_prediction_clear:{current_prediction}:{now_sec:.3f}",
                input_source=msg.source,
                accepted=False,
                reason="cleared_stale_tool_prediction_for_new_consensus",
                affected_tool=current_prediction,
                detail={
                    "replacement_candidate": tool_id,
                    "confidence": confidence,
                    "fusion": fusion_detail,
                },
            )
        stable, duration = self._update_stability(
            self._tool_predict_stability,
            tool_id=tool_id,
            confidence=confidence,
            threshold=self._tool_predict_threshold,
            stability_sec=self._tool_predict_stability_sec,
            now_sec=now_sec,
        )
        self._clear_stale_tool_prediction(now_sec)
        if not stable:
            self._publish_reducer_decision_event(
                input_type="vlm_tool_prediction",
                input_id=f"tool_prediction:{tool_id}:{now_sec:.3f}",
                input_source=msg.source,
                accepted=False,
                reason="awaiting_tool_prediction_stability",
                affected_tool=tool_id,
                detail={
                    "confidence": confidence,
                    "duration_sec": round(duration, 3),
                    "threshold_sec": self._tool_predict_stability_sec,
                    "fusion": fusion_detail,
                },
            )
            return
        current_state = self._twin.instrument_states.get(tool_id)
        if current_state is None or current_state.contaminated or current_state.lifecycle_stage not in {"home_rack", "returned_home"}:
            self._publish_reducer_decision_event(
                input_type="vlm_tool_prediction",
                input_id=f"tool_prediction:{tool_id}:{now_sec:.3f}",
                input_source=msg.source,
                accepted=False,
                reason="predicted_tool_not_available_for_preposition",
                affected_tool=tool_id,
                detail={
                    "confidence": confidence,
                    "lifecycle": current_state.lifecycle_stage if current_state else "",
                    "fusion": fusion_detail,
                },
            )
            if self._twin.state.predicted_tool == tool_id:
                self._twin.state.predicted_tool = ""
                self._twin.state.predicted_tool_confidence = 0.0
                self._twin.state.predicted_tool_stability_sec = 0.0
            return
        changed = self._twin.state.predicted_tool != tool_id
        self._twin.state.predicted_tool = tool_id
        self._twin.state.predicted_tool_confidence = confidence
        self._twin.state.predicted_tool_stability_sec = duration
        self._publish_reducer_decision_event(
            input_type="vlm_tool_prediction",
            input_id=f"tool_prediction:{tool_id}:{now_sec:.3f}",
            input_source=msg.source,
            accepted=True,
            reason="stable_tool_prediction",
            affected_tool=tool_id,
            detail={
                "confidence": confidence,
                "duration_sec": round(duration, 3),
                "threshold_sec": self._tool_predict_stability_sec,
                "fusion": fusion_detail,
            },
        )
        if changed:
            self._publish_event(
                "VLMPredictedToolStable",
                instrument_id=tool_id,
                confidence=confidence,
                detail={
                    "source": msg.source,
                    "duration_sec": round(duration, 3),
                    "threshold_sec": self._tool_predict_stability_sec,
                },
                mode="accepted",
            )
            self._publish_world_state()

    def _on_vlm_result(self, msg: VLMResult) -> None:
        if str(msg.schema_version) not in {"2", "3"}:
            return
        try:
            payload = json.loads(msg.raw_json)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict) or str(payload.get("v", "")) not in {"2", "3"}:
            return
        now_sec = self._stamp_sec(msg.stamp)
        interrupt_visible = any(
            self._twin.spec.is_interrupt_phase(self._twin.spec.resolve_phase_id(str(phase_id)) or str(phase_id))
            and float(confidence) >= 0.5
            for phase_id, confidence in zip(msg.phase_ids, msg.phase_confidences)
        )
        if interrupt_visible:
            self._tool_predict_stability.clear()
            if self._twin.state.predicted_tool:
                self._twin.state.predicted_tool = ""
                self._twin.state.predicted_tool_confidence = 0.0
                self._twin.state.predicted_tool_stability_sec = 0.0
        self._handle_vlm_tool_prediction(payload, msg, now_sec)
        recover_votes: set[str] = set()
        for item in payload.get("mayo", []):
            if not isinstance(item, list) or len(item) != 3:
                continue
            tool_id = self._twin.spec.resolve_instrument_alias(str(item[0])) or str(item[0])
            decision = str(item[1])
            try:
                confidence = float(item[2])
            except (TypeError, ValueError):
                continue
            if decision == "reuse":
                self._update_stability(
                    self._mayo_reuse_stability,
                    tool_id=tool_id,
                    confidence=confidence,
                    threshold=self._mayo_reuse_threshold,
                    stability_sec=self._mayo_stability_sec,
                    now_sec=now_sec,
                )
            elif tool_id:
                if decision == "recover":
                    recover_votes.add(tool_id)
                self._mayo_reuse_stability.pop(tool_id, None)

        retrieve = payload.get("mayo_retrieve", ["", 0.0])
        if not isinstance(retrieve, list) or len(retrieve) != 2:
            return
        tool_id = self._twin.spec.resolve_instrument_alias(str(retrieve[0])) or str(retrieve[0])
        try:
            confidence = float(retrieve[1])
        except (TypeError, ValueError):
            return
        if not tool_id:
            return
        stable, duration = self._update_stability(
            self._mayo_retrieve_stability,
            tool_id=tool_id,
            confidence=confidence,
            threshold=self._mayo_retrieve_threshold,
            stability_sec=self._mayo_stability_sec,
            now_sec=now_sec,
        )
        if not stable:
            self._publish_reducer_decision_event(
                input_type="vlm_mayo_retrieve",
                input_id=f"mayo_retrieve:{tool_id}:{now_sec:.3f}",
                input_source=msg.source,
                accepted=False,
                reason="awaiting_mayo_retrieve_stability",
                affected_tool=tool_id,
                detail={
                    "confidence": confidence,
                    "duration_sec": round(duration, 3),
                    "threshold_sec": self._mayo_stability_sec,
                },
            )
            return
        if tool_id in self._stable_reuse_tools(now_sec) and tool_id not in recover_votes:
            self._publish_reducer_decision_event(
                input_type="vlm_mayo_retrieve",
                input_id=f"mayo_retrieve:{tool_id}:{now_sec:.3f}",
                input_source=msg.source,
                accepted=False,
                reason="mayo_reuse_stable_suppressed_recovery",
                affected_tool=tool_id,
                detail={"confidence": confidence},
            )
            return
        if tool_id in self._mayo_promoted_tools:
            current_state = self._twin.instrument_states.get(tool_id)
            if current_state is not None and current_state.lifecycle_stage in {"mayo_reuse", "mayo_recovery"}:
                return
            self._mayo_promoted_tools.discard(tool_id)
        observation = ToolObservation()
        observation.stamp = msg.stamp if msg.stamp.sec or msg.stamp.nanosec else self._stamp()
        observation.instrument_id = tool_id
        observation.location_id = "mayo_recovery_zone"
        observation.location_type = "mayo_recovery_zone"
        observation.confidence = confidence
        observation.visible = True
        proposal_id = f"mayo_retrieve:{tool_id}:{now_sec:.3f}:{confidence:.2f}"
        current_state = self._twin.instrument_states.get(tool_id)
        current_lifecycle = current_state.lifecycle_stage if current_state is not None else ""
        result = self._twin.promote_mayo_recovery_from_vlm(
            instrument_id=tool_id,
            confidence=confidence,
            source="vlm_mayo_retrieve",
            proposal_id=proposal_id,
            stamp_sec=now_sec,
        )
        if result:
            self._mayo_promoted_tools.add(tool_id)
            self._publish_vlm_inference_proposal(
                observation,
                proposal_id=proposal_id,
                current_lifecycle=current_lifecycle,
                proposed_lifecycle=str(result.get("proposed_lifecycle", "mayo_recovery")),
                source="vlm_mayo_retrieve",
                detail={
                    "schema_version": "2",
                    "mayo_stability_sec": round(duration, 3),
                    "raw_mayo_retrieve": retrieve,
                },
            )
            self._publish_vlm_reducer_decision(result)
            self._publish_event(
                str(result.get("event_type", "VLMProposalIgnored")),
                instrument_id=tool_id,
                location_id="mayo_recovery_zone",
                location_type="mayo_recovery_zone",
                confidence=confidence,
                detail=result,
                mode=str(result.get("reducer_result", "ignored")),
            )
            self._publish_world_state()

    def _on_observation(self, msg: ToolObservation) -> None:
        stamp_sec = float(msg.stamp.sec) + float(msg.stamp.nanosec) / 1_000_000_000.0
        proposal_id = (
            f"toolobs:{msg.instrument_id}:{msg.location_type}:{msg.location_id}:"
            f"{stamp_sec:.3f}:{msg.confidence:.2f}"
        )
        current_lifecycle = ""
        proposed_lifecycle = ""
        resolved_tool = self._twin.spec.resolve_instrument_alias(msg.instrument_id) or msg.instrument_id
        current_state = self._twin.instrument_states.get(resolved_tool)
        if current_state is not None:
            current_lifecycle = current_state.lifecycle_stage
        result = self._twin.reconcile_observation(
            msg,
            source="legacy_tool_observation",
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

    def _on_request(self, msg: String) -> None:
        self._publish_event(
            "VoiceTranscriptObserved",
            detail={"text": msg.data},
            mode="voice_request",
        )

    def _on_surgeon_request(self, msg: SurgeonRequest) -> None:
        resolved = self._twin.update_surgeon_request(msg)
        if resolved and str(msg.event_type) in {"request_tool", "voice_request"}:
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
        if command in {"start", "start_runtime"}:
            self._twin.reset_spec(self._twin.spec, seed_from_perception=False)
            self._phase_entered_ros_sec = self._stamp_sec(self._stamp())
            if start_phase_id:
                self._twin.set_initial_phase(start_phase_id)
                self._phase_entered_ros_sec = self._stamp_sec(self._stamp())
            self._twin.set_execution_state(True, "running")
        elif command == "pause":
            self._twin.set_execution_state(True, "paused")
        elif command == "resume":
            self._twin.set_execution_state(True, "running")
        elif command == "stop":
            self._twin.set_execution_state(False, "halted")
        elif command == "reset":
            self._twin.reset_runtime()
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
