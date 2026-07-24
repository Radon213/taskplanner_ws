"""Real/synthetic VLM node with LM Studio integration and compact state fan-out."""

from __future__ import annotations

from collections import deque
from dataclasses import asdict
import json
import os
from pathlib import Path
import time
from typing import Any

from procedure_spec import (
    BedRobotArmGroupNormalizationError,
    ProcedurePriorScorer,
    compact_procedure_prompt,
    get_default_spec_dir,
    infer_retraction_direction,
    load_bundle,
    normalize_retraction_request,
)
import requests
import rclpy
from rcl_interfaces.msg import SetParametersResult
from rclpy.node import Node
from sensor_msgs.msg import CompressedImage
from std_msgs.msg import String
from surgical_msgs.msg import (
    BedRobotArmGroupActionProposal,
    BedRobotArmGroupCommand,
    BedRobotArmGroupRequest,
    BedRobotArmGroupStatus,
    BTContextSnapshot,
    BTDecision,
    EventDigest,
    PhaseEvidence,
    SkillStatus,
    SimulationState,
    SurgeonOutwardSignal,
    SurgeonRequest,
    SurgeonGestureEvidence,
    ToolObservation,
    TwinEvent,
    VLMHealth,
    VLMRequestContext,
    VLMResult,
    WorldState,
)
from surgical_msgs.srv import ListModels

from .common import compact_json
from .lmstudio_client import LMStudioClient
from .prompt_builder import PromptBuilder
from .schema import SchemaValidationError, compact_vlm_json_schema, normalize_raw_text


PUBLIC_DIGITAL_TWIN_EVENT_TYPES = {
    "RobotTaskStarted",
    "RobotTaskCompleted",
    "RobotGraspedTool",
    "ToolPrepared",
    "ToolHandoverCompleted",
    "ToolReceivedFromSurgeon",
    "ToolSentToCleaner",
    "ToolCleaningProgress",
    "ToolCleaningCompleted",
    "ToolReturnedToTray",
    "PredictedToolReturnedToRack",
    "ProcedureCompleted",
    "SurgeonRequestObserved",
    "VLMStableMayoRetrieve",
    "VLMPredictedToolStable",
    "VoiceTranscriptObserved",
    "InvariantViolation",
    "InvariantViolationIgnored",
}


class RealVLMNode(Node):
    def __init__(self) -> None:
        super().__init__("real_vlm_node")
        self.declare_parameter("spec_dir", str(get_default_spec_dir()))
        self.declare_parameter("base_url", "http://192.168.0.122:1234")
        self.declare_parameter("api_key", "")
        self.declare_parameter("model_id", "gemma-4-26b-a4b-it")
        self.declare_parameter("api_mode", "lmstudio_native")
        self.declare_parameter("request_timeout_sec", 20.0)
        self.declare_parameter("max_output_tokens", 320)
        self.declare_parameter("temperature", 0.0)
        self.declare_parameter("top_p", 1.0)
        self.declare_parameter("response_format", "none")
        self.declare_parameter("reasoning_effort", "")
        self.declare_parameter("retry_count", 2)
        self.declare_parameter("publish_period_sec", 2.0)
        self.declare_parameter("response_mode", "live")
        self.declare_parameter("replay_response_path", "")
        self.declare_parameter("output_prefix", "/vlm")
        self.declare_parameter("context_prefix", "/context")
        self.declare_parameter("field_image_topic", "/surgery/images/field/compressed")
        self.declare_parameter("tray_image_topic", "/surgery/images/tray/compressed")
        self.declare_parameter("synthetic_image_topic", "/surgery/images/synthetic/compressed")
        self.declare_parameter("image_stale_sec", 5.0)
        self.declare_parameter("context_mode", "world")

        self._prompt_builder = PromptBuilder()
        self._active = False
        self._world: WorldState | None = None
        self._simulation: SimulationState | None = None
        self._latest_bt: BTDecision | None = None
        self._recent_events: deque[EventDigest] = deque(maxlen=6)
        self._latest_images: dict[str, tuple[float, bytes, str]] = {}
        self._last_good_raw = ""
        self._last_good_payload: dict[str, Any] | None = None
        self._replay_payload: dict[str, Any] | None = None
        self._recent_observed_signals: deque[dict[str, Any]] = deque(maxlen=12)
        self._recent_speech: deque[dict[str, Any]] = deque(maxlen=10)
        self._recent_skill_statuses: deque[dict[str, Any]] = deque(maxlen=8)
        self._latest_bed_robot_arm_group_request: BedRobotArmGroupRequest | None = None
        self._last_bed_robot_arm_group_proposal_request_id = ""
        self._actor_overlay: dict[str, Any] = {}
        self._last_simulation_bundle = ""
        self._last_world_phase = ""
        self._phase_entered_wall_sec = time.time()
        self._oracle_scenario = []
        self._oracle_scenario_length = 0
        self._oracle_bootstrap_tick = 0
        self._oracle_tick = 0
        self._developer_instruction = (
            "Return exactly one valid JSON object and nothing else. "
            "All object keys must be double-quoted strings: \"v\", \"ph\", \"to\", \"sg\", \"u\", \"sum\". "
            "Never omit quotes around keys. Never use true/false/null. "
            "Confidence values and u must be numeric floats between 0.0 and 1.0, not strings and not booleans. "
            "Use exact tool ids and location ids from context. "
            "If gesture is absent, sg must be exactly [\"\",\"\",\"\",0.0]. "
            "If the image is black, blank, or text-only, keep the current context phase at low confidence, emit no tool observations, and set u to 1.0."
        )

        self._load_parameters()
        self.add_on_set_parameters_callback(self._on_parameters_changed)

        self._phase_summary_pub = self.create_publisher(String, self._topic(self._context_prefix, "phase_summary"), 10)
        self._tool_summary_pub = self.create_publisher(String, self._topic(self._context_prefix, "tool_lifecycle_summary"), 10)
        self._event_digest_pub = self.create_publisher(EventDigest, self._topic(self._context_prefix, "event_digest"), 20)
        self._bt_snapshot_pub = self.create_publisher(BTContextSnapshot, self._topic(self._context_prefix, "bt_context_snapshot"), 10)
        self._request_context_pub = self.create_publisher(VLMRequestContext, self._topic(self._context_prefix, "vlm_request_context"), 10)
        self._result_pub = self.create_publisher(VLMResult, self._topic(self._output_prefix, "result"), 10)
        self._health_pub = self.create_publisher(VLMHealth, self._topic(self._output_prefix, "health"), 10)
        self._phase_pub = self.create_publisher(PhaseEvidence, self._topic(self._output_prefix, "phase_evidence"), 10)
        self._tool_pub = self.create_publisher(ToolObservation, self._topic(self._output_prefix, "tool_observations"), 30)
        self._gesture_pub = self.create_publisher(SurgeonGestureEvidence, self._topic(self._output_prefix, "surgeon_gesture_evidence"), 10)
        self._bed_robot_arm_group_proposal_pub = self.create_publisher(
            BedRobotArmGroupActionProposal,
            self._topic(self._output_prefix, "bed_robot_arm_group_proposal"),
            10,
        )
        self._model_catalog_service = self.create_service(ListModels, "~/list_models", self._on_list_models)

        self.create_subscription(WorldState, "/twin/world_state", self._on_world, 20)
        self.create_subscription(SimulationState, "/simulation/state", self._on_simulation, 20)
        self.create_subscription(TwinEvent, "/twin/events", self._on_event, 50)
        self.create_subscription(BTDecision, "/bt/decision", self._on_bt_decision, 20)
        self.create_subscription(SurgeonRequest, "/surgeon/request", self._on_surgeon_request, 20)
        self.create_subscription(SurgeonOutwardSignal, "/surgeon/outward_signal", self._on_outward_signal, 20)
        self.create_subscription(SkillStatus, "/skill/status", self._on_skill_status, 50)
        self.create_subscription(String, "/surgeon/actor_overlay", self._on_actor_overlay, 20)
        self.create_subscription(String, "/surgery/audio/request_text", self._on_request_text, 20)
        self.create_subscription(
            BedRobotArmGroupRequest,
            "/surgeon/bed_robot_arm_group_request",
            self._on_bed_robot_arm_group_request,
            20,
        )
        self.create_subscription(
            BedRobotArmGroupStatus,
            "/bed_robot_arm_group/status",
            self._on_bed_robot_arm_group_status,
            20,
        )
        self.create_subscription(CompressedImage, self._field_image_topic, self._make_image_cb("field"), 10)
        self.create_subscription(CompressedImage, self._tray_image_topic, self._make_image_cb("tray"), 10)
        self.create_subscription(CompressedImage, self._synthetic_image_topic, self._make_image_cb("synthetic"), 10)
        self.create_subscription(String, "/simulation/control_state", self._on_control, 20)

        self._timer = self.create_timer(self._publish_period_sec, self._tick)

    def _load_parameters(self, overrides: dict[str, Any] | None = None) -> None:
        override_values = overrides or {}

        def param_value(name: str) -> Any:
            return override_values.get(name, self.get_parameter(name).value)

        self._spec_dir = str(param_value("spec_dir"))
        self._spec = load_bundle(self._spec_dir)
        self._base_url = str(param_value("base_url"))
        self._api_key = str(param_value("api_key") or os.environ.get("VLM_API_KEY", ""))
        self._model_id = str(param_value("model_id"))
        self._api_mode = str(param_value("api_mode"))
        self._request_timeout_sec = float(param_value("request_timeout_sec"))
        self._max_output_tokens = int(param_value("max_output_tokens"))
        self._temperature = float(param_value("temperature"))
        self._top_p = float(param_value("top_p"))
        self._response_format = str(param_value("response_format")).strip().lower()
        self._reasoning_effort = str(param_value("reasoning_effort")).strip()
        self._retry_count = int(param_value("retry_count"))
        self._publish_period_sec = float(param_value("publish_period_sec"))
        self._response_mode = str(param_value("response_mode"))
        self._replay_response_path = str(param_value("replay_response_path"))
        self._output_prefix = str(param_value("output_prefix")).rstrip("/")
        self._context_prefix = str(param_value("context_prefix")).rstrip("/")
        self._field_image_topic = str(param_value("field_image_topic"))
        self._tray_image_topic = str(param_value("tray_image_topic"))
        self._synthetic_image_topic = str(param_value("synthetic_image_topic"))
        self._image_stale_sec = float(param_value("image_stale_sec"))
        self._context_mode = str(param_value("context_mode")).strip().lower()
        if self._context_mode == "actor_log":
            self._procedure_prompt = compact_procedure_prompt(self._spec_dir)
            self._prior_scorer = ProcedurePriorScorer(self._spec, self._procedure_prompt)
            self._system_prompt = self._actor_log_system_prompt()
            self._developer_instruction = self._actor_log_developer_instruction()
            self._json_schema = compact_vlm_json_schema("4")
        else:
            self._context_mode = "world"
            self._procedure_prompt = compact_procedure_prompt(self._spec_dir)
            self._prior_scorer = ProcedurePriorScorer(self._spec, self._procedure_prompt)
            self._system_prompt = self._prompt_builder.build(self._spec_dir)
            self._developer_instruction = self._world_developer_instruction()
            self._json_schema = compact_vlm_json_schema("1")
        self._oracle_scenario = list(self._spec.get_mock_perception_stages())
        self._oracle_scenario_length = sum(stage.duration_ticks for stage in self._oracle_scenario)
        self._oracle_bootstrap_tick = int(self._spec.get_mock_perception_bootstrap_tick())
        self._client = LMStudioClient(
            base_url=self._base_url,
            timeout_sec=self._request_timeout_sec,
            api_key=self._api_key,
        )
        self._replay_payload = self._load_replay_payload(self._replay_response_path)

    def _load_replay_payload(self, replay_path: str) -> dict[str, Any] | None:
        if not replay_path.strip():
            return None
        path = Path(replay_path)
        if not path.is_file():
            self.get_logger().warning(f"Replay response path does not exist: {replay_path}")
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            self.get_logger().warning(f"Failed to read replay response payload: {exc}")
            return None

    def _topic(self, prefix: str, suffix: str) -> str:
        return f"{prefix}/{suffix}".replace("//", "/")

    def _world_developer_instruction(self) -> str:
        return (
            "Return exactly one valid JSON object and nothing else. "
            "All object keys must be double-quoted strings: \"v\", \"ph\", \"to\", \"sg\", \"u\", \"sum\". "
            "Never omit quotes around keys. Never use true/false/null. "
            "Confidence values and u must be numeric floats between 0.0 and 1.0, not strings and not booleans. "
            "Use exact tool ids and location ids from context. "
            "If gesture is absent, sg must be exactly [\"\",\"\",\"\",0.0]. "
            "If the image is black, blank, or text-only, keep the current context phase at low confidence, emit no tool observations, and set u to 1.0."
        )

    def _actor_log_system_prompt(self) -> str:
        phases = [
            {
                "id": phase.id,
                "next": list(phase.possible_next),
                "tools": list(phase.expected_instruments),
            }
            for phase in self._spec.bundle.phases
        ]
        tools = [
            {
                "id": instrument.id,
                "name": instrument.display_name,
                "role": instrument.role,
                "requestable": bool(getattr(instrument, "requestable", True)),
            }
            for instrument in self._spec.bundle.instruments
        ]
        return (
            "You are the VLM-side interpreter for a surgical task-planning simulator. "
            "The visual input may be a black No image frame with text overlays. "
            "Infer the likely surgical phase, next requested tool, handover intent, and Mayo stand recovery/reuse need from visible overlay text, public speech, public signals, skill status, digital-twin events, and procedure-derived candidates. "
            "Do not assume the surgeon actor's hidden phase; infer from public speech, visible gestures, Mayo contents, and procedure flow. "
            "Separate the current/held tool from the next tool that is likely to be requested soon. "
            "Prefer public visual/events over previous predictions; use previous predictions only as weak temporal smoothing. "
            "If no public evidence indicates phase progression, keep the current runtime phase high and mark uncertainty rather than jumping ahead from tool order alone. "
            "Use context.candidates as the procedure prior. When public evidence is weak, keep its ranking; only reorder it when speech, visible gesture, image evidence, or recent tool events clearly justify it. "
            "Mayo stand text is visual evidence that a tool is on Mayo, but it does not reveal whether the tool should be recovered or reused. "
            "Field event text is public visual evidence of an intermittent surgical event; treat it as an event overlay, not as the current surgical phase. "
            "Use recover only when the tool is no longer likely to be reused soon. Use reuse when it is likely needed again. "
            "In this simulator, a tool placed on Mayo after use should usually become recover unless recent speech or the doctor sequence clearly indicates near-term reuse. "
            "If Mayo has tools and recent public observations show a different tool request or handover after the Mayo placement, evaluate the older Mayo tool as recover. "
            "When any Mayo tool is recover, mayo_retrieve must name the highest-confidence recover tool; do not leave mayo_retrieve empty. "
            "The doctor procedure prompt may use Pxx/Txx ids. Use the runtime_phase and tools[*].rt crosswalk, and emit only runtime phase/tool ids in JSON. "
            "If a doctor tool has an empty runtime id, treat it as an unmodeled cue and do not emit it as the requested tool. "
            "You also interpret pending logical bed-robot-arm-group retraction requests. "
            "The planner knows only the suction and retraction groups; never infer or emit a physical arm id, arm count, or per-arm command. "
            "Suction is routed deterministically outside the VLM, so never produce a suction proposal. "
            "For a pending retraction request, choose exactly one of UP, DOWN, LEFT, RIGHT, LEFT_RIGHT, or UP_DOWN only when speech or visual evidence supports the direction. "
            "If direction evidence is insufficient, emit a null bed_robot_arm_group so no action is issued. "
            "Return exactly one JSON object with top-k phase and tool candidates. "
            f"Runtime ontology: {json.dumps({'phases': phases, 'tools': tools}, separators=(',', ':'))}. "
            f"Doctor procedure prompt: {json.dumps(self._procedure_prompt, separators=(',', ':'))}"
        )

    def _actor_log_developer_instruction(self) -> str:
        return (
            "Return schema v4 only: "
            "{\"v\":\"4\",\"phase\":[[phase_id,confidence]],\"tool\":[[tool_id,confidence]],"
            "\"intent\":[intent_type,tool_id,confidence],\"mayo\":[[tool_id,\"recover|reuse\",confidence]],"
            "\"mayo_retrieve\":[tool_id,confidence],\"u\":uncertainty,\"sum\":\"short reason\","
            "\"bed_robot_arm_group\":null_or_retraction_object}. "
            "Emit 1-4 phase candidates and 1-4 next-tool candidates using exact phase ids and tool ids from context. "
            "intent_type should be handover when speech/hand indicates a tool request; otherwise use none. "
            "tool rows must rank the most likely next runtime tools requested soon, not Pxx/Txx doctor ids. "
            "Do not rank a tool merely because it is currently held, on Mayo, or being cleaned; rank it only if it is likely to be requested again. "
            "If public evidence is ambiguous, copy the order of candidates.tool and lower confidence instead of guessing a different tool. "
            "If candidates.tool is non-empty and its top row differs from previous.tool, do not repeat previous.tool unless fresh speech explicitly requests it. "
            "Use mayo rows to classify visible Mayo tools as recover or reuse. "
            "For each visible Mayo tool, emit one mayo row. If a Mayo tool is not the current requested tool and is not the immediate next reusable tool, mark recover with confidence >=0.55. "
            "If no Mayo tool should be recovered, mayo_retrieve must be [\"\",0.0]. "
            "When pending_bed_robot_arm_group_request is absent, not retraction/retraction, or lacks enough direction evidence, bed_robot_arm_group must be null. "
            "Otherwise bed_robot_arm_group must contain exactly request_id, group_id, operation, direction, distance_mm, distance_origin, raw_distance_text, end_effector_profile, rationale, confidence. "
            "Copy request_id exactly; group_id and operation must both be retraction; copy the requested end_effector_profile; never name a physical arm. "
            "Direction is one of UP, DOWN, LEFT, RIGHT, LEFT_RIGHT, UP_DOWN. Korean aliases include 위/위쪽/상방, 아래/아래쪽/하방, 왼쪽/좌측, 오른쪽/우측, 좌우/양쪽, 상하/위와 아래. "
            "Distance precedence: explicit number plus mm/cm wins and cm is multiplied by 10; an explicit number without a unit means mm; qualitative anchors are 아주 살짝/미세하게=1, 살짝=5, 조금/조금 더=10, 많이=20, 아주 많이/최대한=30; no distance expression defaults to 10 mm. "
            "Do not clamp explicit distances: 5 cm must remain 50 mm. Only qualitative inference is restricted to 1..30 mm. "
            "Set distance_origin to exactly explicit_with_unit, explicit_unit_inferred, qualitative_inferred, or defaulted and preserve the matched phrase in raw_distance_text (empty for defaulted). "
            "A deterministic normalizer will recompute numbers and units from the request, so never approximate an explicit value. "
            "Never include markdown or extra text."
        )

    def _on_parameters_changed(self, params):
        reload_required = False
        overrides = {parameter.name: parameter.value for parameter in params}
        spec_changed = "spec_dir" in overrides
        for parameter in params:
            if parameter.name in {
                "spec_dir",
                "base_url",
                "api_key",
                "model_id",
                "api_mode",
                "request_timeout_sec",
                "max_output_tokens",
                "temperature",
                "top_p",
                "response_format",
                "reasoning_effort",
                "retry_count",
                "publish_period_sec",
                "response_mode",
                "replay_response_path",
                "image_stale_sec",
                "context_mode",
            }:
                reload_required = True
        if reload_required:
            try:
                self._load_parameters(overrides)
                if spec_changed:
                    self._recent_events.clear()
                    self._last_good_raw = ""
                    self._last_good_payload = None
                    self._recent_observed_signals.clear()
                    self._recent_speech.clear()
                    self._recent_skill_statuses.clear()
                    self._actor_overlay = {}
                    self._latest_bed_robot_arm_group_request = None
                    self._last_bed_robot_arm_group_proposal_request_id = ""
                    self._phase_entered_wall_sec = time.time()
                    self._oracle_tick = 0
            except Exception as exc:
                return SetParametersResult(successful=False, reason=str(exc))
        return SetParametersResult(successful=True)

    def _on_list_models(self, _request, response):
        try:
            response.model_ids = self._client.list_models()
            response.success = True
            response.message = f"{len(response.model_ids)} model(s) from {self._base_url}"
        except Exception as exc:
            response.success = False
            response.model_ids = []
            response.message = f"Model catalog unavailable at {self._base_url}: {exc}"
        return response

    def _make_image_cb(self, label: str):
        def _cb(msg: CompressedImage) -> None:
            stamp = float(msg.header.stamp.sec) + float(msg.header.stamp.nanosec) / 1_000_000_000.0
            mime_type = "image/jpeg" if msg.format.lower() in {"jpeg", "jpg"} else "image/png"
            self._latest_images[label] = (stamp, bytes(msg.data), mime_type)

        return _cb

    def _oracle_stage_for_tick(self, tick: int):
        if not self._oracle_scenario:
            return None
        if self._oracle_scenario_length <= 0:
            return self._oracle_scenario[0]
        cycle_tick = tick % self._oracle_scenario_length
        for stage in self._oracle_scenario:
            if cycle_tick < stage.duration_ticks:
                return stage
            cycle_tick -= stage.duration_ticks
        return self._oracle_scenario[-1]

    def _on_world(self, msg: WorldState) -> None:
        phase = str(getattr(msg, "filtered_phase", "") or "")
        if phase and self._last_world_phase and phase != self._last_world_phase:
            self._reset_transient_public_evidence()
            self._phase_entered_wall_sec = time.time()
        elif phase and not self._last_world_phase:
            self._phase_entered_wall_sec = time.time()
        if phase:
            self._last_world_phase = phase
        self._world = msg
        self._publish_context_summaries()

    def _on_simulation(self, msg: SimulationState) -> None:
        active_bundle = str(getattr(msg, "active_bundle", "") or "")
        if active_bundle and active_bundle != self._last_simulation_bundle:
            self._last_simulation_bundle = active_bundle
            self._reset_public_evidence()
        self._simulation = msg
        self._publish_context_summaries()

    def _on_event(self, msg: TwinEvent) -> None:
        digest = EventDigest()
        digest.stamp = msg.stamp
        digest.event_type = msg.event_type
        digest.instrument_id = msg.instrument_id
        digest.anchor_id = msg.target_location_id or msg.location_id or msg.target_location_type
        detail = {}
        if msg.detail_json:
            try:
                detail = json.loads(msg.detail_json)
            except json.JSONDecodeError:
                detail = {"detail_json": msg.detail_json}
        digest.reason = (
            str(
                detail.get("note")
                or detail.get("voice_text")
                or msg.mode
                or msg.status
                or msg.event_type
            )
        )
        digest.detail = compact_json(
            {
                "tool": msg.instrument_id,
                "loc": msg.location_id,
                "target": msg.target_location_id,
                "arm": msg.arm,
                "mode": msg.mode,
            }
        )
        self._recent_events.append(digest)
        self._ingest_public_twin_event(msg, detail)
        self._event_digest_pub.publish(digest)
        self._publish_context_summaries()

    def _on_bt_decision(self, msg: BTDecision) -> None:
        self._latest_bt = msg
        self._bt_snapshot_pub.publish(self._bt_snapshot_msg())
        self._publish_context_summaries()

    def _append_public_speech(self, text: str) -> None:
        clean = str(text or "").strip()
        if clean:
            now = round(time.time(), 2)
            if self._recent_speech:
                last = self._recent_speech[-1]
                try:
                    last_age = now - float(last.get("at", 0.0))
                except (TypeError, ValueError):
                    last_age = 999.0
                if str(last.get("text", "")) == clean[:240] and last_age <= 1.0:
                    return
            self._recent_speech.append({"text": clean[:240], "at": now})

    def _append_public_signal(
        self,
        *,
        signal_type: str,
        tool_id: str = "",
        hand_pose: str = "",
        speech_text: str = "",
    ) -> None:
        if signal_type in {"advance_phase", "advance_phase_cue"}:
            return
        signal: dict[str, Any] = {}
        if signal_type in {
            "request_tool",
            "voice_request",
            "place_on_mayo",
            "continue_using",
            "small_talk",
            "request_procedure_completion",
            "complete_procedure",
        }:
            signal["type"] = signal_type
        if hand_pose:
            signal["hand"] = hand_pose
        if tool_id:
            signal["tool"] = tool_id
        if signal:
            signal["at"] = round(time.time(), 2)
            self._recent_observed_signals.append(signal)

    def _ingest_public_twin_event(self, msg: TwinEvent, detail: dict[str, Any]) -> None:
        event_type = str(msg.event_type or "")
        tool_id = str(msg.instrument_id or detail.get("requested_tool") or detail.get("tool") or "")
        voice_text = str(detail.get("voice_text") or detail.get("text") or "")
        actor_event_type = str(detail.get("event_type") or event_type)
        if event_type == "VoiceTranscriptObserved":
            self._append_public_speech(voice_text)
        if event_type == "SurgeonRequestObserved":
            self._append_public_signal(
                signal_type=actor_event_type if actor_event_type in {"request_tool", "voice_request"} else "request_tool",
                tool_id=tool_id,
            )
        elif event_type == "SurgeonActorEventObserved" and actor_event_type in {
            "request_tool",
            "voice_request",
            "place_on_mayo",
            "small_talk",
        }:
            self._append_public_signal(
                signal_type=actor_event_type,
                tool_id=tool_id,
            )

    def _on_surgeon_request(self, msg: SurgeonRequest) -> None:
        tool_id = self._spec.resolve_instrument_alias(msg.requested_tool) or str(msg.requested_tool or "")
        if not tool_id and msg.voice_text:
            tool_id = self._tool_from_public_speech([{"text": msg.voice_text}])
        event_type = str(msg.event_type or "request_tool")
        if event_type not in {"request_tool", "voice_request"}:
            return
        self._append_public_signal(
            signal_type=event_type,
            tool_id=tool_id,
        )

    def _on_outward_signal(self, msg: SurgeonOutwardSignal) -> None:
        self._append_public_signal(
            signal_type=msg.signal_type,
            tool_id=msg.tool_id,
            hand_pose=msg.hand_pose,
        )

    def _on_request_text(self, msg: String) -> None:
        self._append_public_speech(msg.data)

    def _on_bed_robot_arm_group_request(self, msg: BedRobotArmGroupRequest) -> None:
        """Track only VLM-routed logical retraction requests.

        Suction start/stop is intentionally absent here: the BT group lane
        routes those operations deterministically without asking the VLM.
        """
        self._append_public_speech(msg.voice_text)
        if str(msg.group_id) != "retraction" or str(msg.operation) != "retraction":
            return
        request_id = str(msg.request_id).strip()
        if not request_id:
            self.get_logger().warning("Ignoring retraction request without request_id")
            return
        if (
            self._latest_bed_robot_arm_group_request is not None
            and self._latest_bed_robot_arm_group_request.request_id == request_id
        ):
            return
        if self._latest_bed_robot_arm_group_request is not None:
            self.get_logger().warning(
                "Ignoring retraction request %s while request %s is still pending"
                % (
                    request_id,
                    self._latest_bed_robot_arm_group_request.request_id,
                )
            )
            return
        self._latest_bed_robot_arm_group_request = msg
        self._last_bed_robot_arm_group_proposal_request_id = ""

    def _on_bed_robot_arm_group_status(self, msg: BedRobotArmGroupStatus) -> None:
        pending = self._latest_bed_robot_arm_group_request
        if pending is None or str(msg.request_id) != str(pending.request_id):
            return
        if bool(msg.terminal):
            self._latest_bed_robot_arm_group_request = None

    def _bed_robot_arm_group_state_rows(self) -> list[dict[str, Any]]:
        if self._world is None:
            return []
        rows: list[dict[str, Any]] = []
        for state in getattr(self._world, "bed_robot_arm_groups", []):
            group_id = str(getattr(state, "group_id", ""))
            if group_id not in {"suction", "retraction"}:
                continue
            rows.append(
                {
                    "group_id": group_id,
                    "connected": bool(getattr(state, "connected", False)),
                    "state": str(getattr(state, "state", "")),
                    "operation": str(getattr(state, "operation", "")),
                    "direction": str(getattr(state, "direction", "")),
                    "distance_mm": round(float(getattr(state, "distance_mm", 0.0)), 3),
                    "distance_origin": str(getattr(state, "distance_origin", "")),
                    "end_effector_profile": str(
                        getattr(state, "end_effector_profile", "")
                    ),
                    "active_request_id": str(getattr(state, "active_request_id", "")),
                    "progress": round(float(getattr(state, "progress", 0.0)), 3),
                    "error_code": str(getattr(state, "error_code", "")),
                    "rejection_reason": str(getattr(state, "rejection_reason", "")),
                }
            )
        return rows

    def _pending_bed_robot_arm_group_request_context(self) -> dict[str, Any] | None:
        request = self._latest_bed_robot_arm_group_request
        if request is None:
            return None
        return {
            "request_id": str(request.request_id),
            "group_id": str(request.group_id),
            "operation": str(request.operation),
            "voice_text": str(request.voice_text),
            "procedure_id": str(request.procedure_id),
            "phase_id": str(request.phase_id),
            "end_effector_profile": str(request.end_effector_profile),
            "source": str(request.source),
        }

    def _on_skill_status(self, msg: SkillStatus) -> None:
        if msg.state != "completed":
            return
        self._recent_skill_statuses.append(
            {
                "at": round(time.time(), 2),
                "action": msg.action,
                "tool": msg.instrument_id,
                "state": msg.state,
                "success": bool(msg.success),
                "message": msg.message,
            }
        )

    def _on_actor_overlay(self, msg: String) -> None:
        try:
            payload = json.loads(msg.data)
        except json.JSONDecodeError:
            return
        if isinstance(payload, dict):
            mayo = payload.get("mayo", [])
            field_event = payload.get("field_event", [])
            if isinstance(field_event, list):
                field_event_lines = [str(item) for item in field_event if str(item)]
            elif isinstance(field_event, str) and field_event:
                field_event_lines = [field_event]
            else:
                field_event_lines = []
            self._actor_overlay = {
                "hand": str(payload.get("hand", "") or ""),
                "mayo": [str(item) for item in mayo if str(item)] if isinstance(mayo, list) else [],
                "field_event": field_event_lines,
            }

    def _on_control(self, msg: String) -> None:
        command = msg.data.strip().partition(":")[0].strip().lower()
        if command in {"start", "start_actors"}:
            self._reset_public_evidence()
            self._active = True
        elif command == "pause":
            self._active = False
        elif command == "resume":
            self._active = True
        elif command == "stop":
            self._active = False
        elif command == "reset":
            self._active = False
            self._world = None
            self._simulation = None
            self._last_world_phase = ""
            self._phase_entered_wall_sec = time.time()
            self._last_good_raw = ""
            self._last_good_payload = None
            self._reset_public_evidence()
            self._oracle_tick = 0

    def _reset_public_evidence(self) -> None:
        self._recent_events.clear()
        self._reset_transient_public_evidence()
        self._actor_overlay = {}
        self._latest_bed_robot_arm_group_request = None
        self._last_bed_robot_arm_group_proposal_request_id = ""
        self._phase_entered_wall_sec = time.time()

    def _reset_transient_public_evidence(self) -> None:
        self._recent_observed_signals.clear()
        self._recent_speech.clear()
        self._recent_skill_statuses.clear()

    def _bt_snapshot_msg(self) -> BTContextSnapshot:
        snapshot = BTContextSnapshot()
        if self._latest_bt is None:
            return snapshot
        snapshot.stamp = self._latest_bt.stamp
        snapshot.procedure_id = self._world.procedure_id if self._world is not None else ""
        snapshot.filtered_phase = self._world.filtered_phase if self._world is not None else ""
        snapshot.decision = self._latest_bt.decision
        snapshot.selected_tool = self._latest_bt.selected_tool
        snapshot.selected_tool_lifecycle = self._latest_bt.selected_tool_lifecycle
        snapshot.next_required_transition = self._latest_bt.next_required_transition
        snapshot.blocking_guard = self._latest_bt.blocking_guard
        snapshot.decision_reason = self._latest_bt.decision_reason
        snapshot.rationale = self._latest_bt.rationale
        return snapshot

    def _publish_context_summaries(self) -> None:
        if self._world is None or self._simulation is None:
            return
        context_msg, context_dict = self._assemble_context()
        if self._context_mode != "actor_log":
            self._request_context_pub.publish(context_msg)

        phase_summary = String()
        phase_summary.data = compact_json(
            {
                "proc": self._world.procedure_id,
                "ph": self._world.filtered_phase,
                "conf": round(float(self._world.phase_confidence), 3),
                "unc": bool(self._world.phase_uncertain),
                "req": self._world.surgeon_request_tool or self._world.explicit_request_tool,
            }
        )
        self._phase_summary_pub.publish(phase_summary)

        tool_summary = String()
        tool_summary.data = compact_json(
            {
                "active": context_dict["tools"],
                "pending": context_dict["pending"],
            }
        )
        self._tool_summary_pub.publish(tool_summary)

    def _assemble_context(self) -> tuple[VLMRequestContext, dict[str, Any]]:
        assert self._world is not None
        assert self._simulation is not None
        active_tools: list[str] = []
        non_home_tools: list[str] = []
        tool_rows: list[dict[str, Any]] = []
        for instrument in self._world.instrument_states:
            at_home = (
                instrument.location_id == instrument.home_location_id
                and instrument.location_type == instrument.home_location_type
                and instrument.lifecycle_stage in {"home_rack", "returned_home"}
            )
            is_context_relevant = (
                not at_home
                or instrument.instrument_id in set(self._world.expected_instruments)
                or instrument.instrument_id in set(self._world.pending_transition_tools)
                or instrument.instrument_id
                in {
                    self._world.right_hand_tool,
                    self._world.left_hand_tool,
                    self._world.prepositioned_tool,
                    self._world.surgeon_request_tool,
                    self._world.explicit_request_tool,
                }
            )
            if at_home:
                continue
            non_home_tools.append(instrument.instrument_id)
            if is_context_relevant:
                active_tools.append(instrument.instrument_id)
                tool_rows.append(
                    {
                        "id": instrument.instrument_id,
                        "lc": instrument.lifecycle_stage,
                        "loc": instrument.location_id,
                        "lt": instrument.location_type,
                        "nx": instrument.next_required_transition,
                        "own": instrument.owner,
                    }
                )

        recent_events = list(self._recent_events)[-6:]
        bt_snapshot = self._bt_snapshot_msg()
        context_dict = {
            "proc": self._world.procedure_id,
            "ph": {
                "id": self._world.filtered_phase,
                "c": round(float(self._world.phase_confidence), 3),
                "u": bool(self._world.phase_uncertain),
            },
            "rq": {
                "exp": self._world.explicit_request_tool,
                "sg": self._world.surgeon_request_tool,
                "intent": self._world.surgeon_intent,
            },
            "hands": {
                "rh": self._world.right_hand_tool,
                "lh": self._world.left_hand_tool,
                "pre": self._world.prepositioned_tool,
                "cb": bool(self._world.cleaner_busy),
                "ct": round(float(self._world.cleaner_remaining_sec), 2),
            },
            "exp": list(self._world.expected_instruments),
            "tools": tool_rows,
            "pending": list(self._world.pending_transition_tools),
            "bed_robot_arm_groups": self._bed_robot_arm_group_state_rows(),
            "pending_bed_robot_arm_group_request": (
                self._pending_bed_robot_arm_group_request_context()
            ),
            "ev": [
                {
                    "t": event.event_type,
                    "tool": event.instrument_id,
                    "a": event.anchor_id,
                    "r": event.reason,
                }
                for event in recent_events
            ],
            "bt": {
                "d": bt_snapshot.decision,
                "tool": bt_snapshot.selected_tool,
                "lc": bt_snapshot.selected_tool_lifecycle,
                "nx": bt_snapshot.next_required_transition,
                "why": bt_snapshot.decision_reason or bt_snapshot.rationale,
                "blk": bt_snapshot.blocking_guard,
            },
        }

        msg = VLMRequestContext()
        msg.stamp = self._world.stamp
        msg.procedure_id = self._world.procedure_id
        msg.filtered_phase = self._world.filtered_phase
        msg.phase_confidence = float(self._world.phase_confidence)
        msg.phase_uncertain = bool(self._world.phase_uncertain)
        msg.explicit_request_tool = self._world.explicit_request_tool
        msg.surgeon_request_tool = self._world.surgeon_request_tool
        msg.surgeon_intent = self._world.surgeon_intent
        msg.right_hand_tool = self._world.right_hand_tool
        msg.left_hand_tool = self._world.left_hand_tool
        msg.prepositioned_tool = self._world.prepositioned_tool
        msg.cleaner_busy = bool(self._world.cleaner_busy)
        msg.cleaner_remaining_sec = float(self._world.cleaner_remaining_sec)
        msg.phase_expected_tools = list(self._world.expected_instruments)
        msg.active_tool_ids = active_tools
        msg.non_home_tool_ids = non_home_tools
        msg.pending_transition_tools = list(self._world.pending_transition_tools)
        msg.recent_events = recent_events
        msg.bt_snapshot = bt_snapshot
        msg.bed_robot_arm_groups = list(
            getattr(self._world, "bed_robot_arm_groups", [])
        )
        pending_group_request = self._latest_bed_robot_arm_group_request
        msg.has_pending_bed_robot_arm_group_request = pending_group_request is not None
        if pending_group_request is not None:
            msg.pending_bed_robot_arm_group_request = pending_group_request
        msg.compact_json = compact_json(context_dict)
        return msg, context_dict

    def _public_event_digests(self) -> list[EventDigest]:
        return [event for event in self._recent_events if event.event_type in PUBLIC_DIGITAL_TWIN_EVENT_TYPES][-6:]

    def _public_digital_twin_context(self) -> dict[str, Any]:
        if self._world is None or self._simulation is None:
            return {}
        _, world_context = self._assemble_context()
        public_events = [
            {
                "t": event.event_type,
                "tool": event.instrument_id,
                "anchor": event.anchor_id,
                "stamp_sec": float(event.stamp.sec) + float(event.stamp.nanosec) / 1_000_000_000.0,
            }
            for event in self._public_event_digests()
        ]
        return {
            "hands": world_context.get("hands", {}),
            "request": {
                "tool": self._world.surgeon_request_tool or self._world.explicit_request_tool,
                "intent": self._world.surgeon_intent,
                "ready_for_handover": bool(self._world.surgeon_ready_for_handover),
                "ready_for_retrieval": bool(self._world.surgeon_ready_for_retrieval),
            },
            "tools": world_context.get("tools", []),
            "bed_robot_arm_groups": self._bed_robot_arm_group_state_rows(),
            "events": public_events[-6:],
        }

    def _actor_log_request_context_msg(self, context: dict[str, Any], compact_context: str) -> VLMRequestContext:
        msg = VLMRequestContext()
        stamp = self.get_clock().now().to_msg()
        msg.stamp = stamp
        msg.procedure_id = self._spec.procedure_id
        if self._world is not None:
            msg.right_hand_tool = self._world.right_hand_tool
            msg.left_hand_tool = self._world.left_hand_tool
            msg.prepositioned_tool = self._world.prepositioned_tool
            msg.cleaner_busy = bool(self._world.cleaner_busy)
            msg.cleaner_remaining_sec = float(self._world.cleaner_remaining_sec)
            msg.active_tool_ids = [str(row.get("id", "")) for row in context.get("digital_twin", {}).get("tools", []) if row.get("id")]
            msg.recent_events = self._public_event_digests()
            msg.bed_robot_arm_groups = list(
                getattr(self._world, "bed_robot_arm_groups", [])
            )
        pending_group_request = self._latest_bed_robot_arm_group_request
        msg.has_pending_bed_robot_arm_group_request = pending_group_request is not None
        if pending_group_request is not None:
            msg.pending_bed_robot_arm_group_request = pending_group_request
        msg.compact_json = compact_context
        return msg

    def _tool_from_public_speech(self, speech_rows: list[dict[str, Any]]) -> str:
        for row in reversed(speech_rows):
            text = str(row.get("text", "")).strip()
            if not text:
                continue
            resolved = self._spec.resolve_instrument_alias(text)
            if resolved:
                return resolved
            lowered = text.lower()
            for instrument in self._spec.bundle.instruments:
                names = [instrument.id, instrument.display_name, *list(getattr(instrument, "aliases", []))]
                if any(name and str(name).lower() in lowered for name in names):
                    return instrument.id
        return ""

    def _tools_from_public_speech(self, speech_rows: list[dict[str, Any]]) -> list[str]:
        tools: list[str] = []
        for row in speech_rows:
            text = str(row.get("text", "")).strip()
            if not text:
                continue
            lowered = text.lower()
            for instrument in self._spec.bundle.instruments:
                names = [instrument.id, instrument.display_name, *list(getattr(instrument, "aliases", []))]
                if any(name and str(name).lower() in lowered for name in names) and instrument.id not in tools:
                    tools.append(instrument.id)
        return tools

    def _canonical_tool_id(self, raw_tool: object) -> str:
        tool_id = str(raw_tool or "").strip()
        if not tool_id:
            return ""
        return self._spec.resolve_instrument_alias(tool_id) or tool_id

    def _canonical_phase_id(self, raw_phase: object) -> str:
        phase_id = str(raw_phase or "").strip()
        if not phase_id:
            return ""
        return self._spec.resolve_phase_id(phase_id) or phase_id

    def _actor_log_prior_evidence(self) -> dict[str, Any]:
        visual = dict(self._actor_overlay)
        speech_rows = self._fresh_rows(self._recent_speech, max_age_sec=18.0)
        observed_signals = self._fresh_rows(self._recent_observed_signals, max_age_sec=8.0)
        skill_statuses = self._fresh_rows(self._recent_skill_statuses, max_age_sec=22.0)
        public_events = [
            {
                "t": event.event_type,
                "tool": event.instrument_id,
                "anchor": event.anchor_id,
                "stamp_sec": float(event.stamp.sec) + float(event.stamp.nanosec) / 1_000_000_000.0,
            }
            for event in self._public_event_digests()
        ]
        hand_tools: list[str] = []
        recent_tools: list[str] = []
        current_phase = ""
        if self._world is not None:
            current_phase = self._world.filtered_phase
            # For next-request prediction, only surgeon-owned tools count as
            # already progressed. Robot-held/prepositioned tools are visible
            # context, but the surgeon may still ask for them next.
            hand_tools = [
                instrument.instrument_id
                for instrument in self._world.instrument_states
                if instrument.lifecycle_stage == "surgeon_owned"
            ]
        return {
            "current_phase": current_phase or self._spec.default_phase_id,
            "phase_entered_sec": self._phase_entered_wall_sec,
            "speech": speech_rows,
            "speech_tools": self._tools_from_public_speech([row for row in speech_rows if isinstance(row, dict)]),
            "recent_tools": recent_tools,
            "observed_signals": observed_signals,
            "skill_status": skill_statuses,
            "mayo_tools": list(visual.get("mayo", [])) if isinstance(visual.get("mayo", []), list) else [],
            "field_event": list(visual.get("field_event", [])) if isinstance(visual.get("field_event", []), list) else [],
            "hand_tools": hand_tools,
            "events": public_events,
        }

    def _fresh_rows(self, rows: deque[dict[str, Any]], *, max_age_sec: float) -> list[dict[str, Any]]:
        now = time.time()
        fresh: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                at = float(row.get("at", 0.0))
            except (TypeError, ValueError):
                at = 0.0
            if at <= 0.0 or now - at <= max_age_sec:
                fresh.append(dict(row))
        return fresh

    def _assemble_actor_log_context_dict(self) -> dict[str, Any]:
        evidence = self._actor_log_prior_evidence()
        candidates = self._prior_scorer.score(evidence)
        context = {
            "proc": self._spec.procedure_id,
            "procedure_prompt_id": str(self._procedure_prompt.get("id", "")) if isinstance(self._procedure_prompt, dict) else "",
            "phases": [
                {"id": phase.id, "tools": list(phase.expected_instruments), "next": list(phase.possible_next)}
                for phase in self._spec.bundle.phases
            ],
            "tools": [
                {
                    "id": instrument.id,
                    "role": instrument.role,
                    "requestable": bool(getattr(instrument, "requestable", True)),
                }
                for instrument in self._spec.bundle.instruments
            ],
            "evidence_window": {
                "visual": dict(self._actor_overlay),
                "speech": list(evidence.get("speech", [])),
                "observed_signals": list(evidence.get("observed_signals", [])),
                "skill_status": list(evidence.get("skill_status", [])),
            },
            "digital_twin": self._public_digital_twin_context(),
            "bed_robot_arm_groups": self._bed_robot_arm_group_state_rows(),
            "pending_bed_robot_arm_group_request": (
                self._pending_bed_robot_arm_group_request_context()
            ),
            "candidates": candidates,
            "previous": {
                "phase": self._last_good_payload.get("phase", ["", 0.0]) if self._last_good_payload else ["", 0.0],
                "tool": self._last_good_payload.get("tool", ["", 0.0]) if self._last_good_payload else ["", 0.0],
            },
        }
        return context

    def _assemble_actor_log_context(self) -> str:
        return compact_json(self._assemble_actor_log_context_dict())

    def _select_images(self) -> tuple[list[tuple[str, bytes, str]], str]:
        now = time.time()

        def fresh(label: str) -> tuple[bytes, str] | None:
            payload = self._latest_images.get(label)
            if payload is None:
                return None
            stamp_sec, image_bytes, mime_type = payload
            if now - stamp_sec > self._image_stale_sec:
                return None
            return image_bytes, mime_type

        selected: list[tuple[str, bytes, str]] = []
        image_source = []
        field = fresh("field")
        tray = fresh("tray")
        synthetic = fresh("synthetic")
        if field is not None:
            selected.append(("field", field[0], field[1]))
            image_source.append("field")
        if tray is not None:
            selected.append(("tray", tray[0], tray[1]))
            image_source.append("tray")
        if (field is None or tray is None) and synthetic is not None:
            selected.append(("synthetic", synthetic[0], synthetic[1]))
            image_source.append("synthetic")
        if not selected and synthetic is not None:
            selected.append(("synthetic", synthetic[0], synthetic[1]))
            image_source.append("synthetic")
        return selected, "+".join(image_source) or "none"

    def _oracle_payload(self, context_dict: dict[str, Any]) -> dict[str, Any]:
        assert self._world is not None
        stage = self._oracle_stage_for_tick(self._oracle_tick)
        if stage is None:
            phases = [
                [
                    self._world.filtered_phase,
                    round(float(self._world.phase_confidence or 0.9), 3),
                ]
            ]
            observations = []
            for instrument in self._world.instrument_states:
                include = (
                    instrument.instrument_id in context_dict["exp"]
                    or instrument.instrument_id in context_dict["pending"]
                    or instrument.instrument_id in {
                        self._world.right_hand_tool,
                        self._world.left_hand_tool,
                        self._world.prepositioned_tool,
                        self._world.surgeon_request_tool,
                        self._world.explicit_request_tool,
                    }
                    or instrument.location_id != instrument.home_location_id
                    or instrument.location_type != instrument.home_location_type
                )
                if not include:
                    continue
                observations.append(
                    [
                        instrument.instrument_id,
                        instrument.location_id,
                        instrument.location_type,
                        0.97,
                    ]
                )
            gesture = ["", "", "", 0.0]
            uncertainty = 0.34 if self._world.phase_uncertain else 0.08
            summary = (
                f"phase={self._world.filtered_phase}; "
                f"request={self._world.surgeon_request_tool or self._world.explicit_request_tool or 'none'}; "
                f"decision={self._latest_bt.decision if self._latest_bt else 'none'}"
            )
            return {
                "v": "1",
                "ph": phases,
                "to": observations,
                "sg": gesture,
                "u": uncertainty,
                "sum": summary,
            }

        phases = [
            [hypothesis.phase_id, round(float(hypothesis.confidence), 3)]
            for hypothesis in stage.phase_hypotheses
        ] or [[self._world.filtered_phase, round(float(self._world.phase_confidence or 0.9), 3)]]
        observations = [
            [
                observation.instrument_id,
                observation.location_id,
                observation.location_type,
                round(float(observation.confidence), 3),
            ]
            for observation in stage.observations
            if observation.visible
        ]
        gesture = ["", "", "", 0.0]
        if stage.surgeon_gesture is not None:
            gesture = [
                stage.surgeon_gesture.event_type,
                stage.surgeon_gesture.requested_tool,
                stage.surgeon_gesture.hand_pose,
                round(float(stage.surgeon_gesture.confidence), 3),
            ]
        uncertainty = float(stage.uncertainty)
        current_request = self._world.surgeon_request_tool or self._world.explicit_request_tool or "none"
        summary = (
            f"oracle_stage={stage.name}; "
            f"phase={phases[0][0] if phases else self._world.filtered_phase}; "
            f"request={current_request}; "
            f"decision={self._latest_bt.decision if self._latest_bt else 'none'}"
        )
        return {
            "v": "1",
            "ph": phases,
            "to": observations,
            "sg": gesture,
            "u": uncertainty,
            "sum": summary,
        }

    def _actor_log_fallback_payload(self, context_dict: dict[str, Any]) -> dict[str, Any]:
        evidence_window = context_dict.get("evidence_window", {}) if isinstance(context_dict, dict) else {}
        visual = evidence_window.get("visual", {}) if isinstance(evidence_window, dict) else {}
        mayo = visual.get("mayo", []) if isinstance(visual, dict) else []
        speech = evidence_window.get("speech", []) if isinstance(evidence_window, dict) else []
        speech_rows = speech if isinstance(speech, list) else []
        requested_tool = self._tool_from_public_speech([row for row in speech_rows if isinstance(row, dict)])
        observed_signals = evidence_window.get("observed_signals", []) if isinstance(evidence_window, dict) else []
        latest_signal = observed_signals[-1] if isinstance(observed_signals, list) and observed_signals else {}
        has_request_signal = isinstance(latest_signal, dict) and str(latest_signal.get("type", "")) in {"request_tool", "voice_request"}
        intent_type = "handover" if requested_tool and has_request_signal else "none"
        candidates = context_dict.get("candidates", {}) if isinstance(context_dict, dict) else {}
        phase_rows = list(candidates.get("phase", [])) if isinstance(candidates, dict) else []
        tool_rows = list(candidates.get("tool", [])) if isinstance(candidates, dict) else []
        if not phase_rows:
            phase_rows = [[self._spec.default_phase_id, 0.35]]
        if requested_tool and not any(row[0] == requested_tool for row in tool_rows if isinstance(row, list) and row):
            tool_rows = [[requested_tool, 0.72], *tool_rows[:3]]
        if not tool_rows:
            tool_rows = [["", 0.0]]
        immediate_reuse_tool = ""
        for row in tool_rows:
            if isinstance(row, list) and row and str(row[0]):
                immediate_reuse_tool = str(row[0])
                break
        mayo_entries: list[list[Any]] = []
        recover_candidates: list[tuple[str, float]] = []
        for tool_id in (mayo if isinstance(mayo, list) else []):
            tool_id = str(tool_id)
            if not tool_id:
                continue
            if tool_id == requested_tool or tool_id == immediate_reuse_tool:
                mayo_entries.append([tool_id, "reuse", 0.62])
            else:
                confidence = 0.62 if requested_tool else 0.56
                mayo_entries.append([tool_id, "recover", confidence])
                recover_candidates.append((tool_id, confidence))
        mayo_retrieve = ["", 0.0]
        if recover_candidates:
            mayo_retrieve = [recover_candidates[0][0], recover_candidates[0][1]]
        return {
            "v": "4",
            "phase": phase_rows[:4],
            "tool": tool_rows[:4],
            "intent": [intent_type, requested_tool if intent_type != "none" else "", 0.7 if intent_type != "none" else 0.0],
            "mayo": mayo_entries,
            "mayo_retrieve": mayo_retrieve,
            "u": 0.55,
            "sum": "actor-log fallback",
            "bed_robot_arm_group": self._fallback_bed_robot_arm_group_proposal(),
        }

    def _fallback_bed_robot_arm_group_proposal(self) -> dict[str, Any] | None:
        """Build a safe oracle fallback only when speech states direction.

        A missing spoken direction cannot be recovered from the fallback path,
        because that path has no trustworthy visual reasoning.  Returning null
        turns into an explicit invalid proposal and therefore no group action.
        """
        request = self._latest_bed_robot_arm_group_request
        if request is None or not infer_retraction_direction(request.voice_text):
            return None
        try:
            normalized = normalize_retraction_request(request.voice_text)
        except BedRobotArmGroupNormalizationError:
            return None
        return {
            "request_id": str(request.request_id),
            "group_id": "retraction",
            "operation": "retraction",
            "direction": normalized.direction,
            "distance_mm": float(normalized.distance_mm),
            "distance_origin": normalized.distance_origin,
            "raw_distance_text": normalized.raw_distance_text,
            "end_effector_profile": str(request.end_effector_profile),
            "rationale": "direction and distance normalized from explicit speech",
            "confidence": 0.76,
        }

    def _fallback_payload(self, context_dict: dict[str, Any]) -> dict[str, Any]:
        if self._context_mode == "actor_log":
            return self._actor_log_fallback_payload(context_dict)
        return self._oracle_payload(context_dict)

    def _candidate_rows(self, context_dict: dict[str, Any], key: str) -> list[list[Any]]:
        candidates = context_dict.get("candidates", {}) if isinstance(context_dict, dict) else {}
        rows = candidates.get(key, []) if isinstance(candidates, dict) else []
        cleaned: list[list[Any]] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, list) or len(row) < 2:
                continue
            item_id = str(row[0])
            if not item_id:
                continue
            try:
                confidence = float(row[1])
            except (TypeError, ValueError):
                continue
            cleaned.append([item_id, max(0.0, min(1.0, confidence))])
        return cleaned

    def _merge_ranked_rows(self, preferred: list[list[Any]], model_rows: list[list[Any]], *, limit: int) -> list[list[Any]]:
        merged: list[list[Any]] = []
        seen: set[str] = set()
        for row in [*preferred, *model_rows]:
            if not isinstance(row, list) or len(row) < 2:
                continue
            item_id = str(row[0])
            if not item_id or item_id in seen:
                continue
            try:
                confidence = float(row[1])
            except (TypeError, ValueError):
                continue
            seen.add(item_id)
            merged.append([item_id, round(max(0.0, min(1.0, confidence)), 3)])
            if len(merged) >= limit:
                break
        return merged

    def _normal_phase_rows(self, rows: list[list[Any]], *, fallback_phase: str = "") -> list[list[Any]]:
        normal_rows = [
            row
            for row in rows
            if isinstance(row, list)
            and row
            and not self._spec.is_interrupt_phase(str(row[0]))
        ]
        if normal_rows:
            return normal_rows
        if fallback_phase and not self._spec.is_interrupt_phase(fallback_phase):
            return [[fallback_phase, 0.35]]
        return []

    def _stabilize_actor_log_payload(self, payload: dict[str, Any], context_dict: dict[str, Any]) -> dict[str, Any]:
        if str(payload.get("v", "")) not in {"3", "4"}:
            return payload
        stabilized = dict(payload)
        evidence_window = context_dict.get("evidence_window", {}) if isinstance(context_dict, dict) else {}
        visual = evidence_window.get("visual", {}) if isinstance(evidence_window, dict) else {}
        speech = evidence_window.get("speech", []) if isinstance(evidence_window, dict) else []
        speech_rows = speech if isinstance(speech, list) else []
        observed_signals = evidence_window.get("observed_signals", []) if isinstance(evidence_window, dict) else []
        latest_signal = observed_signals[-1] if isinstance(observed_signals, list) and observed_signals else {}
        requested_tool = self._tool_from_public_speech([row for row in speech_rows if isinstance(row, dict)])
        has_request_signal = isinstance(latest_signal, dict) and str(latest_signal.get("type", "")) in {
            "request_tool",
            "voice_request",
        }

        phase_candidates = self._candidate_rows(context_dict, "phase")
        candidate_evidence = context_dict.get("candidates", {}).get("evidence", {}) if isinstance(context_dict, dict) else {}
        fallback_phase = str(candidate_evidence.get("current_phase", "") if isinstance(candidate_evidence, dict) else "")
        model_phase_rows = [
            [str(row[0]), float(row[1])]
            for row in stabilized.get("phase", [])
            if isinstance(row, list) and len(row) >= 2 and str(row[0])
        ]
        if phase_candidates:
            stabilized["phase"] = self._normal_phase_rows(
                self._merge_ranked_rows(phase_candidates, model_phase_rows, limit=4),
                fallback_phase=fallback_phase,
            )[:4]
        elif model_phase_rows:
            stabilized["phase"] = self._normal_phase_rows(model_phase_rows, fallback_phase=fallback_phase)[:4]

        tool_candidates = self._candidate_rows(context_dict, "tool")
        model_tool_rows = [
            [str(row[0]), float(row[1])]
            for row in stabilized.get("tool", [])
            if isinstance(row, list) and len(row) >= 2 and str(row[0])
        ]
        if tool_candidates:
            stabilized["tool"] = self._merge_ranked_rows(tool_candidates, model_tool_rows, limit=4)

        intent = stabilized.get("intent", ["none", "", 0.0])
        if not isinstance(intent, list) or len(intent) < 3:
            intent = ["none", "", 0.0]
        if requested_tool and has_request_signal:
            intent = ["handover", requested_tool, max(0.72, min(1.0, float(intent[2]) if len(intent) > 2 else 0.0))]
        stabilized["intent"] = [str(intent[0]), str(intent[1]), round(max(0.0, min(1.0, float(intent[2]))), 3)]

        summary = str(stabilized.get("sum", ""))
        if tool_candidates or phase_candidates:
            stabilized["sum"] = (summary + "; candidate-stabilized").strip("; ")[:180]
        return stabilized

    def _run_model(self, context_json: str, images: list[tuple[str, bytes, str]]) -> tuple[str, dict[str, Any], float, str, int, str]:
        retries_used = 0
        if self._response_mode == "replay":
            if self._replay_payload is None:
                raise RuntimeError("response_mode=replay requires replay_response_path")
            raw = json.dumps(self._replay_payload, separators=(",", ":"), sort_keys=True)
            normalized_raw, payload = normalize_raw_text(raw)
            return normalized_raw, payload, 0.0, "replay", retries_used, ""
        if self._response_mode == "oracle":
            payload = self._fallback_payload(json.loads(context_json))
            raw = json.dumps(payload, separators=(",", ":"), sort_keys=True)
            normalized_raw, payload = normalize_raw_text(raw)
            return normalized_raw, payload, 0.0, "oracle", retries_used, ""

        last_error = ""
        for attempt in range(self._retry_count + 1):
            developer_prompt = self._developer_instruction
            if attempt > 0:
                developer_prompt += " Previous response was invalid. Re-emit schema only."
            try:
                response = self._client.request_json(
                    system_prompt=self._system_prompt,
                    developer_prompt=developer_prompt,
                    user_context_json=context_json,
                    images=images,
                    model_id=self._model_id,
                    temperature=self._temperature,
                    top_p=self._top_p,
                    max_output_tokens=self._max_output_tokens,
                    api_mode=self._api_mode,
                    response_format=self._response_format,
                    json_schema=self._json_schema,
                    reasoning_effort=self._reasoning_effort,
                )
                normalized_raw, payload = normalize_raw_text(response.raw_text)
                return normalized_raw, payload, response.latency_sec, response.mode, attempt, ""
            except (requests.RequestException, SchemaValidationError, json.JSONDecodeError, RuntimeError, ValueError) as exc:  # type: ignore[name-defined]
                last_error = str(exc)
                retries_used = attempt + 1
        if self._last_good_payload is not None:
            return self._last_good_raw, self._last_good_payload, 0.0, "last_good", retries_used, last_error
        payload = self._fallback_payload(json.loads(context_json))
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        normalized_raw, payload = normalize_raw_text(raw)
        return normalized_raw, payload, 0.0, "oracle_fallback", retries_used, last_error

    def _cacheable_payload(
        self,
        raw_json: str,
        payload: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        """Remove one-shot group commands from temporal phase/tool memory."""
        if str(payload.get("v", "")) != "4" or payload.get("bed_robot_arm_group") is None:
            return raw_json, payload
        cache_payload = dict(payload)
        cache_payload["bed_robot_arm_group"] = None
        return (
            json.dumps(cache_payload, separators=(",", ":"), sort_keys=True),
            cache_payload,
        )

    def _tick(self, force: bool = False) -> None:
        if not force and not self._active:
            return
        context_stamp = self.get_clock().now().to_msg()
        if self._context_mode == "actor_log":
            context_dict = self._assemble_actor_log_context_dict()
            request_context_json = compact_json(context_dict)
            request_context_msg = self._actor_log_request_context_msg(context_dict, request_context_json)
            context_stamp = request_context_msg.stamp
            self._request_context_pub.publish(request_context_msg)
        else:
            if self._world is None or self._simulation is None:
                return
            request_context, context_dict = self._assemble_context()
            request_context_json = request_context.compact_json
            context_stamp = request_context.stamp
        images, image_source = self._select_images()
        prompt_chars = len(self._system_prompt) + len(self._developer_instruction) + len(request_context_json)
        raw_json = ""
        payload: dict[str, Any] | None = None
        latency_sec = 0.0
        mode = self._response_mode
        parse_retry_count = 0
        last_error = ""
        healthy = True
        connected = True
        try:
            raw_json, payload, latency_sec, mode, parse_retry_count, last_error = self._run_model(
                request_context_json,
                images,
            )
        except Exception as exc:  # pragma: no cover - safety net
            last_error = str(exc)
            healthy = False
            connected = False
            if self._last_good_payload is not None:
                raw_json = self._last_good_raw
                payload = self._last_good_payload
                mode = "last_good"
            else:
                payload = self._fallback_payload(context_dict)
                raw_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
                mode = "oracle_fallback"
        if payload is None:
            return
        if self._context_mode == "actor_log":
            payload = self._stabilize_actor_log_payload(payload, context_dict)
            raw_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        if mode not in {"last_good", "oracle_fallback"}:
            self._last_good_raw, self._last_good_payload = self._cacheable_payload(
                raw_json,
                payload,
            )

        self._publish_vlm_outputs(
            payload,
            raw_json,
            observation_stamp=context_stamp,
            image_source=image_source,
            latency_sec=latency_sec,
            prompt_chars=prompt_chars,
            parse_retry_count=parse_retry_count,
            last_error=last_error,
            mode=mode,
            healthy=healthy,
            connected=connected,
        )
        if self._response_mode == "oracle":
            self._oracle_tick += 1

    def _validate_bed_robot_arm_group_proposal(
        self,
        proposal: dict[str, Any],
        request: BedRobotArmGroupRequest,
    ) -> str:
        if str(proposal.get("request_id", "")) != str(request.request_id):
            return "request_id does not match the pending retraction request"
        if str(proposal.get("group_id", "")) != "retraction":
            return "VLM proposal must target the retraction group"
        if str(proposal.get("operation", "")) != "retraction":
            return "VLM proposal operation must be retraction"

        requested_profile = str(request.end_effector_profile).strip()
        proposed_profile = str(proposal.get("end_effector_profile", "")).strip()
        if requested_profile and proposed_profile != requested_profile:
            return "end_effector_profile does not match the pending request"

        distance_origin = str(proposal.get("distance_origin", ""))
        raw_distance_text = str(proposal.get("raw_distance_text", "")).strip()
        voice_text = str(request.voice_text).strip()
        if distance_origin == "defaulted" and raw_distance_text:
            return "defaulted distance must have empty raw_distance_text"
        if raw_distance_text:
            compact_raw = "".join(raw_distance_text.split())
            compact_voice = "".join(voice_text.split())
            if compact_raw not in compact_voice:
                return "raw_distance_text is not present in the source request"

        try:
            distance_mm = float(proposal.get("distance_mm", 0.0))
            normalized = normalize_retraction_request(
                voice_text,
                vlm_direction=str(proposal.get("direction", "")),
                qualitative_distance_mm=(
                    distance_mm if distance_origin == "qualitative_inferred" else None
                ),
            )
        except (BedRobotArmGroupNormalizationError, TypeError, ValueError) as exc:
            return f"deterministic request normalization failed: {exc}"

        if normalized.direction != str(proposal.get("direction", "")):
            return (
                "direction contradicts the source request: "
                f"expected {normalized.direction}"
            )
        if normalized.distance_origin != distance_origin:
            return (
                "distance_origin contradicts the source request: "
                f"expected {normalized.distance_origin}"
            )
        if abs(float(normalized.distance_mm) - distance_mm) > 1e-6:
            return (
                "distance_mm contradicts deterministic source normalization: "
                f"expected {normalized.distance_mm:g} mm"
            )
        return ""

    def _publish_bed_robot_arm_group_proposal(
        self,
        payload: dict[str, Any],
        raw_json: str,
        stamp,
    ) -> None:
        if str(payload.get("v", "")) != "4":
            return
        request = self._latest_bed_robot_arm_group_request
        if request is None:
            return
        request_id = str(request.request_id)
        if request_id == self._last_bed_robot_arm_group_proposal_request_id:
            return

        group_payload = payload.get("bed_robot_arm_group")
        message = BedRobotArmGroupActionProposal()
        message.stamp = stamp
        message.schema_version = "4"
        message.raw_json = raw_json
        command = BedRobotArmGroupCommand()
        command.stamp = stamp
        command.request_id = request_id
        command.command_id = f"vlm-{request_id}"
        command.group_id = "retraction"
        command.operation = "retraction"
        command.end_effector_profile = str(request.end_effector_profile)

        if not isinstance(group_payload, dict):
            message.valid = False
            message.validation_error = (
                "VLM declined retraction: insufficient direction evidence"
            )
        else:
            command.request_id = str(group_payload.get("request_id", request_id))
            command.command_id = f"vlm-{command.request_id}"
            command.group_id = str(group_payload.get("group_id", ""))
            command.operation = str(group_payload.get("operation", ""))
            command.direction = str(group_payload.get("direction", ""))
            command.distance_mm = float(group_payload.get("distance_mm", 0.0))
            command.distance_origin = str(group_payload.get("distance_origin", ""))
            command.raw_distance_text = str(group_payload.get("raw_distance_text", ""))
            command.end_effector_profile = str(
                group_payload.get("end_effector_profile", "")
            )
            command.rationale = str(group_payload.get("rationale", ""))
            command.confidence = float(group_payload.get("confidence", 0.0))
            message.validation_error = self._validate_bed_robot_arm_group_proposal(
                group_payload,
                request,
            )
            message.valid = not bool(message.validation_error)

        message.command = command
        self._bed_robot_arm_group_proposal_pub.publish(message)
        self._last_bed_robot_arm_group_proposal_request_id = request_id

    def _publish_vlm_outputs(
        self,
        payload: dict[str, Any],
        raw_json: str,
        *,
        observation_stamp,
        image_source: str,
        latency_sec: float,
        prompt_chars: int,
        parse_retry_count: int,
        last_error: str,
        mode: str,
        healthy: bool,
        connected: bool,
    ) -> None:
        stamp = observation_stamp
        schema_version = str(payload.get("v", "1"))
        if schema_version in {"3", "4"}:
            phase_rows = [
                [str(item[0]), float(item[1])]
                for item in payload.get("phase", [])
                if isinstance(item, list) and len(item) == 2 and str(item[0])
            ]
            if not phase_rows:
                phase_rows = [[self._spec.default_phase_id, 0.0]]
            observed_rows = [
                [str(item[0]), "mayo_stand", "mayo_stand", float(item[2])]
                for item in payload.get("mayo", [])
                if isinstance(item, list) and len(item) == 3 and str(item[0])
            ]
            intent = payload.get("intent", ["", "", 0.0])
            intent_type = str(intent[0])
            gesture_type = "request_tool" if intent_type in {"handover", "request_tool"} else intent_type
            hand_pose = "open_receive" if gesture_type == "request_tool" else ""
            gesture_row = [gesture_type, str(intent[1]), hand_pose, float(intent[2])]
            uncertainty = float(payload.get("u", 0.0))
            summary = str(payload.get("sum", ""))
        elif schema_version == "2":
            phase_rows = [[str(payload["phase"][0]), float(payload["phase"][1])]]
            observed_rows = [
                [str(item[0]), "mayo_stand", "mayo_stand", float(item[2])]
                for item in payload.get("mayo", [])
                if str(item[0])
            ]
            intent = payload.get("intent", ["", "", 0.0])
            intent_type = str(intent[0])
            gesture_type = "request_tool" if intent_type in {"handover", "request_tool"} else intent_type
            hand_pose = "open_receive" if gesture_type == "request_tool" else ""
            gesture_row = [gesture_type, str(intent[1]), hand_pose, float(intent[2])]
            uncertainty = float(payload.get("u", 0.0))
            summary = str(payload.get("sum", ""))
        else:
            phase_rows = list(payload["ph"])
            observed_rows = list(payload["to"])
            gesture_row = list(payload["sg"])
            uncertainty = float(payload.get("u", 0.0))
            summary = str(payload.get("sum", ""))

        phase_rows = [
            [self._canonical_phase_id(item[0]), item[1]]
            for item in phase_rows
            if item and self._canonical_phase_id(item[0])
        ]
        observed_rows = [
            [self._canonical_tool_id(item[0]), item[1], item[2], item[3]]
            for item in observed_rows
            if item and self._canonical_tool_id(item[0])
        ]
        if len(gesture_row) >= 2:
            gesture_row[1] = self._canonical_tool_id(gesture_row[1])

        phase_evidence = PhaseEvidence()
        phase_evidence.stamp = stamp
        phase_evidence.source = f"real_vlm:{self._spec.procedure_id}:{mode}"
        phase_evidence.phase_ids = [item[0] for item in phase_rows]
        phase_evidence.phase_confidences = [float(item[1]) for item in phase_rows]
        phase_evidence.visible_instrument_ids = [item[0] for item in observed_rows]
        phase_evidence.visible_instrument_confidences = [float(item[3]) for item in observed_rows]
        phase_evidence.scene_summary = summary
        phase_evidence.uncertainty = uncertainty
        self._phase_pub.publish(phase_evidence)

        if schema_version == "1":
            for tool_id, location_id, location_type, confidence in observed_rows:
                observation = ToolObservation()
                observation.stamp = stamp
                observation.instrument_id = tool_id
                observation.location_id = location_id
                observation.location_type = location_type
                observation.confidence = float(confidence)
                observation.visible = True
                self._tool_pub.publish(observation)

        gesture = SurgeonGestureEvidence()
        gesture.stamp = stamp
        gesture.procedure_id = self._spec.procedure_id
        gesture.phase_id = phase_evidence.phase_ids[0] if phase_evidence.phase_ids else ""
        gesture.event_type = str(gesture_row[0])
        gesture.requested_tool = str(gesture_row[1])
        gesture.hand_pose = str(gesture_row[2])
        gesture.confidence = float(gesture_row[3])
        gesture.note = summary
        self._gesture_pub.publish(gesture)

        result = VLMResult()
        result.stamp = stamp
        result.source = phase_evidence.source
        result.schema_version = schema_version
        result.raw_json = raw_json
        result.summary = summary
        result.phase_ids = list(phase_evidence.phase_ids)
        result.phase_confidences = list(phase_evidence.phase_confidences)
        result.observed_tool_ids = [item[0] for item in observed_rows]
        result.observed_location_ids = [item[1] for item in observed_rows]
        result.observed_location_types = [item[2] for item in observed_rows]
        result.observed_confidences = [float(item[3]) for item in observed_rows]
        result.gesture_event_type = gesture.event_type
        result.gesture_requested_tool = gesture.requested_tool
        result.gesture_hand_pose = gesture.hand_pose
        result.gesture_confidence = gesture.confidence
        result.uncertainty = uncertainty
        self._result_pub.publish(result)

        self._publish_bed_robot_arm_group_proposal(
            payload,
            raw_json,
            stamp,
        )

        health = VLMHealth()
        health.stamp = self.get_clock().now().to_msg()
        health.connected = bool(connected)
        health.healthy = bool(healthy and not last_error)
        health.model_id = self._model_id
        health.image_source = image_source
        health.latency_sec = float(latency_sec)
        health.prompt_chars = int(prompt_chars)
        health.output_chars = len(raw_json)
        health.parse_retry_count = int(parse_retry_count)
        health.last_error = last_error
        health.last_mode = mode
        self._health_pub.publish(health)


def main() -> None:
    rclpy.init()
    node = RealVLMNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            node.destroy_node()
        finally:
            if rclpy.ok():
                rclpy.shutdown()
