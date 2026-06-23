"""Mock surgeon node for scripted requests and overrides."""

from __future__ import annotations

from collections import deque
import random

from procedure_spec import get_default_spec_dir, load_bundle
import rclpy
from rcl_interfaces.msg import SetParametersResult
from rclpy.node import Node
from std_msgs.msg import String
from surgical_msgs.msg import (
    FilteredPhase,
    SimulationState,
    SurgeonGestureEvidence,
    SurgeonRequest,
    SurgeonState,
)

TOOL_DISPLAY_NAMES = {
    "retractor": "Army-Navy retractor",
    "cautery": "cautery",
    "metzenbaum": "Metzenbaum scissors",
    "suction": "suction tip",
    "right_angle": "right-angle clamp",
    "forceps": "tissue forceps",
    "grasper": "atraumatic grasper",
    "bipolar": "bipolar forceps",
    "scissors": "curved scissors",
    "suction_irrigator": "suction irrigator",
    "clip_applier": "clip applier",
    "needle_driver": "needle driver",
}


class MockSurgeonNode(Node):
    def __init__(self) -> None:
        super().__init__("mock_surgeon")
        self.declare_parameter("spec_dir", str(get_default_spec_dir()))
        self.declare_parameter("random_voice_enabled", True)
        self._spec_dir = str(self.get_parameter("spec_dir").value)
        self._random_voice_enabled = bool(self.get_parameter("random_voice_enabled").value)
        self._state_pub = self.create_publisher(SurgeonState, "/surgeon/state", 20)
        self._request_pub = self.create_publisher(SurgeonRequest, "/surgeon/request", 20)
        self._voice_pub = self.create_publisher(String, "/surgery/audio/request_text", 10)
        self._rng = random.Random(42)
        self._active = False
        self._tick = 0
        self._last_stage_name = ""
        self._current_phase_id = ""
        self._active_voice_text = ""
        self._voice_hold_ticks = 0
        self._next_random_voice_tick = 0
        self._override_hold_ticks = 0
        self._active_override: SurgeonRequest | None = None
        self._override_queue: deque[SurgeonRequest] = deque(maxlen=10)
        self._instrument_states: dict[str, object] = {}
        self._gesture_history: deque[dict[str, object]] = deque(maxlen=6)
        self._published_vlm_signature = ""
        self._latched_vlm_state: dict[str, object] | None = None
        self._latched_vlm_ticks = 0
        self._timer = None
        self._load_spec(self._spec_dir)
        self.add_on_set_parameters_callback(self._on_parameters_changed)
        self.create_subscription(String, "/simulation/control_state", self._on_control, 20)
        self.create_subscription(SurgeonRequest, "/simulation/surgeon_override", self._on_override, 20)
        self.create_subscription(FilteredPhase, "/phase/filtered", self._on_phase, 20)
        self.create_subscription(SimulationState, "/simulation/state", self._on_simulation_state, 20)
        self.create_subscription(
            SurgeonGestureEvidence,
            "/vlm/surgeon_gesture_evidence",
            self._on_gesture_evidence,
            20,
        )

    def _load_spec(self, spec_dir: str) -> None:
        self._spec = load_bundle(spec_dir)
        self._scenario = self._spec.get_mock_surgeon_stages()
        if not self._scenario:
            raise ValueError("The loaded procedure bundle does not define mock_surgeon stages.")
        self._scenario_length = sum(stage.duration_ticks for stage in self._scenario)
        self._period_sec = float(self._spec.get_mock_surgeon_period_sec(default=1.0))
        self._current_phase_id = self._spec.default_phase_id
        self._active_voice_text = ""
        self._voice_hold_ticks = 0
        self._override_hold_ticks = 0
        self._active_override = None
        self._schedule_next_random_voice()
        if self._timer is not None:
            self._timer.cancel()
        self._timer = self.create_timer(self._period_sec, self._publish)
        self._tick = 0
        self._last_stage_name = ""

    def _on_parameters_changed(self, params):
        for parameter in params:
            if parameter.name == "spec_dir":
                try:
                    self._spec_dir = str(parameter.value)
                    self._load_spec(self._spec_dir)
                except Exception as exc:
                    return SetParametersResult(
                        successful=False,
                        reason=f"failed to reload spec bundle: {exc}",
                    )
            if parameter.name == "random_voice_enabled":
                self._random_voice_enabled = bool(parameter.value)
                if self._random_voice_enabled:
                    self._schedule_next_random_voice()
                else:
                    self._active_voice_text = ""
                    self._voice_hold_ticks = 0
        return SetParametersResult(successful=True)

    def _stage_for_tick(self, tick: int):
        cycle_tick = tick % self._scenario_length
        for stage in self._scenario:
            if cycle_tick < stage.duration_ticks:
                return stage
            cycle_tick -= stage.duration_ticks
        return self._scenario[-1]

    def _publish_request(self, request: SurgeonRequest) -> None:
        request.stamp = self.get_clock().now().to_msg()
        self._request_pub.publish(request)
        voice = String()
        voice.data = request.voice_text if request.event_type == "voice_request" else ""
        self._voice_pub.publish(voice)

    def _publish_state(self, *, phase_id: str, intent: str, requested_tool: str, ready_for_handover: bool, ready_for_retrieval: bool, scripted: bool, scene_note: str, voice_text: str = "") -> None:
        state = SurgeonState()
        state.stamp = self.get_clock().now().to_msg()
        state.procedure_id = self._spec.procedure_id
        state.phase_id = phase_id
        state.intent = intent
        state.requested_tool = requested_tool
        state.ready_for_handover = bool(ready_for_handover)
        state.ready_for_retrieval = bool(ready_for_retrieval)
        state.scripted = bool(scripted)
        state.voice_text = voice_text
        state.scene_note = scene_note
        self._state_pub.publish(state)

    def _set_active_voice(self, voice_text: str, hold_ticks: int = 3) -> None:
        self._active_voice_text = voice_text.strip()
        self._voice_hold_ticks = hold_ticks if self._active_voice_text else 0

    def _decay_active_voice(self) -> None:
        if self._voice_hold_ticks > 0:
            self._voice_hold_ticks -= 1
            if self._voice_hold_ticks == 0:
                self._active_voice_text = ""

    def _clear_active_override(self) -> None:
        self._active_override = None
        self._override_hold_ticks = 0

    def _activate_override(self, override: SurgeonRequest, hold_ticks: int = 6) -> None:
        self._active_override = override
        self._override_hold_ticks = hold_ticks
        if override.voice_text:
            self._set_active_voice(override.voice_text, hold_ticks=hold_ticks)

    def _schedule_next_random_voice(self) -> None:
        if not self._random_voice_enabled:
            self._next_random_voice_tick = 10**9
            return
        self._next_random_voice_tick = self._tick + self._rng.randint(5, 12)

    def _display_tool_name(self, tool_id: str) -> str:
        return TOOL_DISPLAY_NAMES.get(tool_id, tool_id.replace("_", " "))

    def _tool_is_in_field(self, tool_id: str) -> bool:
        instrument = self._instrument_states.get(tool_id)
        if instrument is None:
            return False
        return getattr(instrument, "status", "") == "in_use" or getattr(instrument, "location_type", "") == "surgical_field"

    def _tool_is_with_surgeon(self, tool_id: str) -> bool:
        instrument = self._instrument_states.get(tool_id)
        if instrument is None:
            return False
        return (
            getattr(instrument, "owner", "") == "surgeon"
            or getattr(instrument, "location_type", "") == "surgeon_hand"
            or getattr(instrument, "location_type", "") in {"mayo_reuse_zone", "mayo_recovery_zone"}
            or getattr(instrument, "location_id", "") in {"surgeon_right_hand", "surgeon_left_hand"}
            or getattr(instrument, "status", "") in {"handed_over", "in_use", "parked_for_reuse", "awaiting_retrieval"}
            or getattr(instrument, "last_holder", "") == "surgeon"
        )

    def _tool_is_temporarily_unavailable(self, tool_id: str) -> bool:
        instrument = self._instrument_states.get(tool_id)
        if instrument is None:
            return False
        return (
            getattr(instrument, "cleanliness_state", "") == "cleaning"
            or getattr(instrument, "location_id", "") == "cleaner_slot"
            or getattr(instrument, "location_type", "") == "mayo_recovery_zone"
        )

    def _prune_gesture_history(self, now_sec: float) -> None:
        while self._gesture_history and (now_sec - float(self._gesture_history[0]["stamp_sec"])) > 3.5:
            self._gesture_history.popleft()

    def _current_time_sec(self) -> float:
        now = self.get_clock().now().to_msg()
        return float(now.sec) + float(now.nanosec) / 1_000_000_000.0

    def _stable_gesture_request(self) -> dict[str, object] | None:
        now_sec = self._current_time_sec()
        self._prune_gesture_history(now_sec)
        candidates: dict[tuple[str, str], dict[str, object]] = {}

        for sample in self._gesture_history:
            event_type = str(sample["event_type"])
            requested_tool = str(sample["requested_tool"])
            confidence = float(sample["confidence"])
            if not event_type or confidence < 0.18:
                continue
            age_sec = max(now_sec - float(sample["stamp_sec"]), 0.0)
            weight = confidence * max(0.22, 1.0 - age_sec / 3.0)
            signature = (event_type, requested_tool)
            bucket = candidates.setdefault(
                signature,
                {"score": 0.0, "count": 0, "latest": sample},
            )
            bucket["score"] = float(bucket["score"]) + weight
            bucket["count"] = int(bucket["count"]) + 1
            if float(sample["stamp_sec"]) >= float(bucket["latest"]["stamp_sec"]):
                bucket["latest"] = sample

        if not candidates:
            return None

        ranked_candidates = sorted(
            candidates.items(),
            key=lambda item: (
                float(item[1]["score"]),
                float(item[1]["latest"]["confidence"]),
            ),
            reverse=True,
        )

        for (event_type, requested_tool), best in ranked_candidates:
            latest = best["latest"]
            score = float(best["score"])
            count = int(best["count"])
            latest_confidence = float(latest["confidence"])
            latest_pose = str(latest.get("hand_pose", ""))
            fast_path = latest_confidence >= 0.84 and not latest_pose.startswith(("uncertain", "occluded"))
            if fast_path:
                if score < 0.72:
                    continue
            elif score < 1.02 or count < 2:
                continue

            if event_type == "request_tool":
                if (
                    not requested_tool
                    or self._tool_is_with_surgeon(requested_tool)
                    or self._tool_is_temporarily_unavailable(requested_tool)
                ):
                    continue
                return {
                    "phase_id": latest["phase_id"] or self._current_phase_id,
                    "intent": "request_tool",
                    "requested_tool": requested_tool,
                    "ready_for_handover": True,
                    "ready_for_retrieval": False,
                    "scene_note": latest["note"] or "VLM inferred an open hand requesting a tool.",
                }
            if event_type == "return_tool":
                if not requested_tool or not (
                    self._tool_is_with_surgeon(requested_tool) or self._tool_is_in_field(requested_tool)
                ):
                    continue
                return {
                    "phase_id": latest["phase_id"] or self._current_phase_id,
                    "intent": "return_tool",
                    "requested_tool": requested_tool,
                    "ready_for_handover": False,
                    "ready_for_retrieval": True,
                    "scene_note": latest["note"] or "VLM inferred a used tool being presented for retrieval.",
                }
        return None

    def _publish_vlm_request_transition(self, stable_state: dict[str, object] | None, voice_active: bool) -> None:
        signature = (
            f"{stable_state['intent']}:{stable_state['requested_tool']}"
            if stable_state is not None
            else ""
        )
        if signature == self._published_vlm_signature:
            return
        if stable_state is not None:
            request = SurgeonRequest()
            request.event_type = str(stable_state["intent"])
            request.requested_tool = str(stable_state["requested_tool"])
            request.ready_for_handover = bool(stable_state["ready_for_handover"])
            request.ready_for_retrieval = bool(stable_state["ready_for_retrieval"])
            request.override = False
            request.note = str(stable_state["scene_note"])
            self._publish_request(request)
            self._published_vlm_signature = signature
            return
        if self._published_vlm_signature and not voice_active:
            request = SurgeonRequest()
            request.event_type = "cancel_request"
            request.override = False
            request.note = "VLM hand cue is no longer stable."
            self._publish_request(request)
        self._published_vlm_signature = ""

    def _request_state_still_relevant(self, state: dict[str, object] | None) -> bool:
        if state is None:
            return False
        intent = str(state.get("intent", ""))
        requested_tool = str(state.get("requested_tool", ""))
        if not requested_tool:
            return False
        if intent == "request_tool":
            return not self._tool_is_with_surgeon(requested_tool) and not self._tool_is_temporarily_unavailable(requested_tool)
        if intent == "return_tool":
            return self._tool_is_with_surgeon(requested_tool) or self._tool_is_in_field(requested_tool)
        return False

    def _effective_vlm_state(self, stable_state: dict[str, object] | None) -> dict[str, object] | None:
        if stable_state is not None:
            self._latched_vlm_state = dict(stable_state)
            self._latched_vlm_ticks = 3
            return stable_state
        if self._latched_vlm_ticks > 0 and self._request_state_still_relevant(self._latched_vlm_state):
            self._latched_vlm_ticks -= 1
            return self._latched_vlm_state
        self._latched_vlm_state = None
        self._latched_vlm_ticks = 0
        return None

    def _select_requestable_tool(
        self,
        phase_id: str,
        preferred_tool: str = "",
        *,
        exact_only: bool = False,
    ) -> str:
        ordered_candidates: list[str] = []
        if preferred_tool:
            ordered_candidates.append(preferred_tool)
        if not exact_only:
            ordered_candidates.extend(self._spec.get_expected_instruments(phase_id))

        seen: set[str] = set()
        deduped = [tool_id for tool_id in ordered_candidates if tool_id and not (tool_id in seen or seen.add(tool_id))]
        if not self._instrument_states:
            return deduped[0] if deduped else ""

        for tool_id in deduped:
            if self._tool_is_with_surgeon(tool_id):
                continue
            if self._tool_is_temporarily_unavailable(tool_id):
                continue
            return tool_id
        return ""

    def _voice_stages_for_phase(self, phase_id: str):
        return [
            stage
            for stage in self._scenario
            if stage.event_type == "voice_request" and stage.phase_id == phase_id
        ]

    def _choose_random_voice_request(self, stage):
        if stage.ready_for_retrieval:
            return None
        phase_id = self._current_phase_id or stage.phase_id or self._spec.default_phase_id
        voice_stages = self._voice_stages_for_phase(phase_id)

        scripted_options: list[tuple[str, str, str]] = []
        for voice_stage in voice_stages:
            requested_tool = self._select_requestable_tool(
                phase_id,
                voice_stage.requested_tool,
                exact_only=bool(voice_stage.requested_tool),
            )
            if not requested_tool:
                continue
            phrase = voice_stage.voice_text.strip() or f"{self._display_tool_name(requested_tool)} please"
            scripted_options.append((requested_tool, phrase, voice_stage.scene_note))

        if scripted_options:
            requested_tool, phrase, note = self._rng.choice(scripted_options)
        else:
            candidates = self._spec.get_expected_instruments(phase_id)
            if stage.requested_tool and stage.requested_tool not in candidates:
                candidates = [stage.requested_tool, *candidates]
            candidates = [
                tool_id
                for tool_id in candidates
                if not self._tool_is_with_surgeon(tool_id)
                and not self._tool_is_temporarily_unavailable(tool_id)
            ]
            if not candidates:
                return None
            requested_tool = self._rng.choice(candidates)
            spoken_tool = self._display_tool_name(requested_tool)
            phrase = self._rng.choice(
                [
                    f"{spoken_tool} please",
                    f"Need {spoken_tool}",
                    f"{spoken_tool} now",
                ]
            )
            note = f"Irregular verbal request during {phase_id}"

        request = SurgeonRequest()
        request.event_type = "voice_request"
        request.requested_tool = requested_tool
        request.voice_text = phrase
        request.ready_for_handover = True
        request.ready_for_retrieval = False
        request.override = False
        request.note = note
        return request

    def _publish(self) -> None:
        if not self._active:
            return
        while self._override_queue and self._override_queue[0].event_type == "cancel_request":
            self._override_queue.popleft()
            self._clear_active_override()
            self._active_voice_text = ""
            self._voice_hold_ticks = 0

        if self._active_override is not None and self._override_hold_ticks > 0:
            override = self._active_override
            self._publish_state(
                phase_id="override",
                intent=override.event_type,
                requested_tool=override.requested_tool,
                ready_for_handover=override.ready_for_handover,
                ready_for_retrieval=override.ready_for_retrieval,
                scripted=False,
                voice_text=self._active_voice_text,
                scene_note=override.note or "UI override injected",
            )
            self._override_hold_ticks -= 1
            if self._override_hold_ticks == 0:
                self._clear_active_override()
                self._active_voice_text = ""
                self._voice_hold_ticks = 0
            return

        if self._override_queue:
            override = self._override_queue.popleft()
            self._activate_override(override)
            self._publish_request(override)
            self._publish_state(
                phase_id="override",
                intent=override.event_type,
                requested_tool=override.requested_tool,
                ready_for_handover=override.ready_for_handover,
                ready_for_retrieval=override.ready_for_retrieval,
                scripted=False,
                voice_text=self._active_voice_text,
                scene_note=override.note or "UI override injected",
            )
            return

        stage = self._stage_for_tick(self._tick)
        self._current_phase_id = stage.phase_id or self._current_phase_id
        phase_id = stage.phase_id or self._current_phase_id or self._spec.default_phase_id
        stable_vlm_state = self._stable_gesture_request()
        effective_vlm_state = self._effective_vlm_state(stable_vlm_state)
        if effective_vlm_state:
            state_intent = str(effective_vlm_state["intent"])
            state_requested_tool = str(effective_vlm_state["requested_tool"])
            state_handover = bool(effective_vlm_state["ready_for_handover"])
            state_retrieval = bool(effective_vlm_state["ready_for_retrieval"])
            state_scene_note = str(effective_vlm_state["scene_note"])
            state_phase_id = str(effective_vlm_state["phase_id"])
        else:
            state_intent = stage.event_type or "idle"
            state_requested_tool = stage.requested_tool or ""
            state_handover = bool(stage.ready_for_handover or stage.event_type in {"request_tool", "voice_request", "extend_hand_for_handover"})
            state_retrieval = bool(stage.ready_for_retrieval or stage.event_type in {"return_tool", "extend_hand_for_retrieval"})
            state_scene_note = stage.scene_note or ""
            state_phase_id = stage.phase_id or phase_id

        if (
            self._random_voice_enabled
            and self._tick >= self._next_random_voice_tick
            and self._last_stage_name == stage.name
        ):
            request = self._choose_random_voice_request(stage)
            self._schedule_next_random_voice()
            if request is not None:
                self._set_active_voice(request.voice_text)
                self._publish_request(request)
                state_intent = request.event_type
                state_requested_tool = request.requested_tool
                state_handover = True
                state_retrieval = False
                state_scene_note = request.note
                state_phase_id = phase_id

        if self._last_stage_name != stage.name:
            self._last_stage_name = stage.name

        voice_active = bool(self._active_voice_text)
        self._publish_vlm_request_transition(None if voice_active else effective_vlm_state, voice_active)

        self._publish_state(
            phase_id=state_phase_id,
            intent=state_intent,
            requested_tool=state_requested_tool,
            ready_for_handover=state_handover,
            ready_for_retrieval=state_retrieval,
            scripted=True,
            voice_text=self._active_voice_text,
            scene_note=state_scene_note,
        )
        self._tick += 1
        self._decay_active_voice()

    def _on_control(self, msg: String) -> None:
        raw_command = msg.data.strip()
        command, _, start_phase_id = raw_command.partition(":")
        command = command.strip().lower()
        start_phase_id = start_phase_id.strip()
        if command == "start":
            self._active = True
            if start_phase_id:
                self._current_phase_id = start_phase_id
        elif command == "pause":
            self._active = False
        elif command == "resume":
            self._active = True
        elif command == "stop":
            self._active = False
        elif command == "reset":
            self._active = False
            self._tick = 0
            self._last_stage_name = ""
            if start_phase_id:
                self._current_phase_id = start_phase_id
            self._active_voice_text = ""
            self._voice_hold_ticks = 0
            self._clear_active_override()
            self._schedule_next_random_voice()
            self._override_queue.clear()
            self._gesture_history.clear()
            self._published_vlm_signature = ""
            self._latched_vlm_state = None
            self._latched_vlm_ticks = 0

    def _on_override(self, msg: SurgeonRequest) -> None:
        if msg.event_type == "cancel_request":
            self._override_queue.clear()
            self._clear_active_override()
            self._active_voice_text = ""
            self._voice_hold_ticks = 0
            return
        self._override_queue.append(msg)

    def _on_phase(self, msg: FilteredPhase) -> None:
        if msg.phase_id:
            self._current_phase_id = msg.phase_id

    def _on_gesture_evidence(self, msg: SurgeonGestureEvidence) -> None:
        stamp_sec = float(msg.stamp.sec) + float(msg.stamp.nanosec) / 1_000_000_000.0
        self._gesture_history.append(
            {
                "stamp_sec": stamp_sec,
                "phase_id": msg.phase_id,
                "event_type": msg.event_type,
                "requested_tool": msg.requested_tool,
                "hand_pose": msg.hand_pose,
                "confidence": float(msg.confidence),
                "note": msg.note,
            }
        )
        if not self._active:
            return
        stable_vlm_state = self._stable_gesture_request()
        effective_vlm_state = self._effective_vlm_state(stable_vlm_state)
        voice_active = bool(self._active_voice_text)
        self._publish_vlm_request_transition(None if voice_active else effective_vlm_state, voice_active)

    def _on_simulation_state(self, msg: SimulationState) -> None:
        self._instrument_states = {instrument.instrument_id: instrument for instrument in msg.instrument_states}


def main() -> None:
    rclpy.init()
    node = MockSurgeonNode()
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
