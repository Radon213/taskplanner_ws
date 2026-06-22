"""Real/synthetic VLM node with LM Studio integration and compact state fan-out."""

from __future__ import annotations

from collections import deque
from dataclasses import asdict
import json
from pathlib import Path
import time
from typing import Any

from procedure_spec import get_default_spec_dir, load_bundle
import requests
import rclpy
from rcl_interfaces.msg import SetParametersResult
from rclpy.node import Node
from sensor_msgs.msg import CompressedImage
from std_msgs.msg import String
from surgical_msgs.msg import (
    BTContextSnapshot,
    BTDecision,
    EventDigest,
    PhaseEvidence,
    SimulationState,
    SurgeonGestureEvidence,
    SurgeonState,
    ToolObservation,
    TwinEvent,
    VLMHealth,
    VLMRequestContext,
    VLMResult,
    WorldState,
)

from .common import compact_json
from .lmstudio_client import LMStudioClient
from .prompt_builder import PromptBuilder
from .schema import SchemaValidationError, normalize_raw_text


class RealVLMNode(Node):
    def __init__(self) -> None:
        super().__init__("real_vlm_node")
        self.declare_parameter("spec_dir", str(get_default_spec_dir()))
        self.declare_parameter("base_url", "http://192.168.0.122:1234")
        self.declare_parameter("model_id", "gemma-4-26b-a4b-it")
        self.declare_parameter("api_mode", "lmstudio_native")
        self.declare_parameter("request_timeout_sec", 20.0)
        self.declare_parameter("max_output_tokens", 180)
        self.declare_parameter("temperature", 0.0)
        self.declare_parameter("top_p", 1.0)
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

        self._prompt_builder = PromptBuilder()
        self._active = False
        self._world: WorldState | None = None
        self._simulation: SimulationState | None = None
        self._surgeon_state: SurgeonState | None = None
        self._latest_bt: BTDecision | None = None
        self._recent_events: deque[EventDigest] = deque(maxlen=6)
        self._latest_images: dict[str, tuple[float, bytes, str]] = {}
        self._last_good_raw = ""
        self._last_good_payload: dict[str, Any] | None = None
        self._replay_payload: dict[str, Any] | None = None
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
            "If the image is black, blank, or says No image, emit "
            "{\"v\":\"1\",\"ph\":[[\"exposure\",0.0]],\"to\":[],\"sg\":[\"\",\"\",\"\",0.0],\"u\":1.0,\"sum\":\"no image available\"}."
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

        self.create_subscription(WorldState, "/twin/world_state", self._on_world, 20)
        self.create_subscription(SimulationState, "/simulation/state", self._on_simulation, 20)
        self.create_subscription(TwinEvent, "/twin/events", self._on_event, 50)
        self.create_subscription(BTDecision, "/bt/decision", self._on_bt_decision, 20)
        self.create_subscription(SurgeonState, "/surgeon/state", self._on_surgeon_state, 20)
        self.create_subscription(CompressedImage, self._field_image_topic, self._make_image_cb("field"), 10)
        self.create_subscription(CompressedImage, self._tray_image_topic, self._make_image_cb("tray"), 10)
        self.create_subscription(CompressedImage, self._synthetic_image_topic, self._make_image_cb("synthetic"), 10)
        self.create_subscription(String, "/simulation/control_state", self._on_control, 20)

        self._timer = self.create_timer(self._publish_period_sec, self._tick)

    def _load_parameters(self) -> None:
        self._spec_dir = str(self.get_parameter("spec_dir").value)
        self._spec = load_bundle(self._spec_dir)
        self._system_prompt = self._prompt_builder.build(self._spec_dir)
        self._base_url = str(self.get_parameter("base_url").value)
        self._model_id = str(self.get_parameter("model_id").value)
        self._api_mode = str(self.get_parameter("api_mode").value)
        self._request_timeout_sec = float(self.get_parameter("request_timeout_sec").value)
        self._max_output_tokens = int(self.get_parameter("max_output_tokens").value)
        self._temperature = float(self.get_parameter("temperature").value)
        self._top_p = float(self.get_parameter("top_p").value)
        self._retry_count = int(self.get_parameter("retry_count").value)
        self._publish_period_sec = float(self.get_parameter("publish_period_sec").value)
        self._response_mode = str(self.get_parameter("response_mode").value)
        self._replay_response_path = str(self.get_parameter("replay_response_path").value)
        self._output_prefix = str(self.get_parameter("output_prefix").value).rstrip("/")
        self._context_prefix = str(self.get_parameter("context_prefix").value).rstrip("/")
        self._field_image_topic = str(self.get_parameter("field_image_topic").value)
        self._tray_image_topic = str(self.get_parameter("tray_image_topic").value)
        self._synthetic_image_topic = str(self.get_parameter("synthetic_image_topic").value)
        self._image_stale_sec = float(self.get_parameter("image_stale_sec").value)
        self._oracle_scenario = list(self._spec.get_mock_perception_stages())
        self._oracle_scenario_length = sum(stage.duration_ticks for stage in self._oracle_scenario)
        self._oracle_bootstrap_tick = int(self._spec.get_mock_perception_bootstrap_tick())
        self._client = LMStudioClient(base_url=self._base_url, timeout_sec=self._request_timeout_sec)
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

    def _on_parameters_changed(self, params):
        reload_required = False
        for parameter in params:
            if parameter.name in {
                "spec_dir",
                "base_url",
                "model_id",
                "api_mode",
                "request_timeout_sec",
                "max_output_tokens",
                "temperature",
                "top_p",
                "retry_count",
                "publish_period_sec",
                "response_mode",
                "replay_response_path",
                "image_stale_sec",
            }:
                reload_required = True
        if reload_required:
            try:
                self._load_parameters()
            except Exception as exc:
                return SetParametersResult(successful=False, reason=str(exc))
        return SetParametersResult(successful=True)

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
        self._world = msg
        self._publish_context_summaries()

    def _on_simulation(self, msg: SimulationState) -> None:
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
        self._event_digest_pub.publish(digest)
        self._publish_context_summaries()

    def _on_bt_decision(self, msg: BTDecision) -> None:
        self._latest_bt = msg
        self._bt_snapshot_pub.publish(self._bt_snapshot_msg())
        self._publish_context_summaries()

    def _on_surgeon_state(self, msg: SurgeonState) -> None:
        self._surgeon_state = msg
        self._publish_context_summaries()

    def _on_control(self, msg: String) -> None:
        command = msg.data.strip().lower()
        if command in {"start", "start_actors"}:
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
            self._last_good_raw = ""
            self._last_good_payload = None
            self._oracle_tick = 0

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
        msg.compact_json = compact_json(context_dict)
        return msg, context_dict

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

    def _run_model(self, context_json: str, images: list[tuple[str, bytes, str]]) -> tuple[str, dict[str, Any], float, str, int, str]:
        retries_used = 0
        if self._response_mode == "replay":
            if self._replay_payload is None:
                raise RuntimeError("response_mode=replay requires replay_response_path")
            raw = json.dumps(self._replay_payload, separators=(",", ":"), sort_keys=True)
            normalized_raw, payload = normalize_raw_text(raw)
            return normalized_raw, payload, 0.0, "replay", retries_used, ""
        if self._response_mode == "oracle":
            payload = self._oracle_payload(json.loads(context_json))
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
                )
                normalized_raw, payload = normalize_raw_text(response.raw_text)
                return normalized_raw, payload, response.latency_sec, response.mode, attempt, ""
            except (requests.RequestException, SchemaValidationError, json.JSONDecodeError, RuntimeError, ValueError) as exc:  # type: ignore[name-defined]
                last_error = str(exc)
                retries_used = attempt + 1
        if self._last_good_payload is not None:
            return self._last_good_raw, self._last_good_payload, 0.0, "last_good", retries_used, last_error
        payload = self._oracle_payload(json.loads(context_json))
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        normalized_raw, payload = normalize_raw_text(raw)
        return normalized_raw, payload, 0.0, "oracle_fallback", retries_used, last_error

    def _tick(self, force: bool = False) -> None:
        if not force and not self._active:
            return
        if self._world is None or self._simulation is None:
            return
        request_context, context_dict = self._assemble_context()
        images, image_source = self._select_images()
        prompt_chars = len(self._system_prompt) + len(self._developer_instruction) + len(request_context.compact_json)
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
                request_context.compact_json,
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
                payload = self._oracle_payload(context_dict)
                raw_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
                mode = "oracle_fallback"
        if payload is None:
            return
        if mode not in {"last_good", "oracle_fallback"}:
            self._last_good_raw = raw_json
            self._last_good_payload = payload

        self._publish_vlm_outputs(payload, raw_json, image_source=image_source, latency_sec=latency_sec, prompt_chars=prompt_chars, parse_retry_count=parse_retry_count, last_error=last_error, mode=mode, healthy=healthy, connected=connected)
        if self._response_mode == "oracle":
            self._oracle_tick += 1

    def _publish_vlm_outputs(
        self,
        payload: dict[str, Any],
        raw_json: str,
        *,
        image_source: str,
        latency_sec: float,
        prompt_chars: int,
        parse_retry_count: int,
        last_error: str,
        mode: str,
        healthy: bool,
        connected: bool,
    ) -> None:
        stamp = self.get_clock().now().to_msg()
        phase_evidence = PhaseEvidence()
        phase_evidence.stamp = stamp
        phase_evidence.source = f"real_vlm:{self._spec.procedure_id}:{mode}"
        phase_evidence.phase_ids = [item[0] for item in payload["ph"]]
        phase_evidence.phase_confidences = [float(item[1]) for item in payload["ph"]]
        phase_evidence.visible_instrument_ids = [item[0] for item in payload["to"]]
        phase_evidence.visible_instrument_confidences = [float(item[3]) for item in payload["to"]]
        phase_evidence.scene_summary = str(payload.get("sum", ""))
        phase_evidence.uncertainty = float(payload.get("u", 0.0))
        self._phase_pub.publish(phase_evidence)

        for tool_id, location_id, location_type, confidence in payload["to"]:
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
        gesture.event_type = str(payload["sg"][0])
        gesture.requested_tool = str(payload["sg"][1])
        gesture.hand_pose = str(payload["sg"][2])
        gesture.confidence = float(payload["sg"][3])
        gesture.note = str(payload.get("sum", ""))
        self._gesture_pub.publish(gesture)

        result = VLMResult()
        result.stamp = stamp
        result.source = phase_evidence.source
        result.schema_version = "1"
        result.raw_json = raw_json
        result.summary = str(payload.get("sum", ""))
        result.phase_ids = list(phase_evidence.phase_ids)
        result.phase_confidences = list(phase_evidence.phase_confidences)
        result.observed_tool_ids = [item[0] for item in payload["to"]]
        result.observed_location_ids = [item[1] for item in payload["to"]]
        result.observed_location_types = [item[2] for item in payload["to"]]
        result.observed_confidences = [float(item[3]) for item in payload["to"]]
        result.gesture_event_type = gesture.event_type
        result.gesture_requested_tool = gesture.requested_tool
        result.gesture_hand_pose = gesture.hand_pose
        result.gesture_confidence = gesture.confidence
        result.uncertainty = float(payload.get("u", 0.0))
        self._result_pub.publish(result)

        health = VLMHealth()
        health.stamp = stamp
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
