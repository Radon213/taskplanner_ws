"""State-driven surgeon actor for authoritative single-twin runtime."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import random

from procedure_spec import get_default_spec_dir, load_bundle
import rclpy
from rcl_interfaces.msg import SetParametersResult
from rclpy.node import Node
from std_msgs.msg import String
from surgical_msgs.msg import (
    FilteredPhase,
    SurgeonActorEvent,
    SurgeonGestureEvidence,
    SurgeonRequest,
    SurgeonState,
    WorldState,
)


TOOL_DISPLAY_NAMES = {
    "retractor": "Army-Navy retractor",
    "cautery": "Cautery (Bovie)",
    "metzenbaum": "Metzenbaum scissors",
    "suction": "Suction tip",
    "right_angle": "Right-angle clamp",
    "forceps": "Tissue forceps",
    "grasper": "Atraumatic grasper",
    "bipolar": "Bipolar forceps",
    "scissors": "Curved scissors",
    "suction_irrigator": "Suction irrigator",
    "clip_applier": "Clip applier",
    "needle_driver": "Needle driver",
}

REQUEST_INTENTS = {"request_tool", "voice_request"}
RETURN_INTENTS = {"return_tool", "extend_hand_for_retrieval"}
PROCEDURE_INTENTS = {"request_procedure_completion", "complete_procedure"}


@dataclass(slots=True)
class ActorDecision:
    intent: str
    requested_tool: str = ""
    voice_text: str = ""
    ready_for_handover: bool = False
    ready_for_retrieval: bool = False
    scene_note: str = ""
    phase_id: str = ""
    actor_event_type: str = ""
    actor_tool_id: str = ""
    actor_phase_id: str = ""


class SurgeonActorNode(Node):
    def __init__(self) -> None:
        super().__init__("surgeon_actor")
        self.declare_parameter("spec_dir", str(get_default_spec_dir()))
        self.declare_parameter("decision_period_sec", 0.25)
        self.declare_parameter("random_voice_enabled", True)
        self.declare_parameter("same_tool_request_cooldown_sec", 4.0)
        self.declare_parameter("autonomous_phase_progression_enabled", True)
        self.declare_parameter("min_tool_use_sec", 3.0)
        self._spec_dir = str(self.get_parameter("spec_dir").value)
        self._decision_period_sec = float(self.get_parameter("decision_period_sec").value)
        self._autonomous_policy_enabled = bool(self.get_parameter("random_voice_enabled").value)
        self._autonomous_phase_progression_enabled = bool(
            self.get_parameter("autonomous_phase_progression_enabled").value
        )
        self._same_tool_request_cooldown_sec = float(
            self.get_parameter("same_tool_request_cooldown_sec").value
        )
        self._min_tool_use_sec = float(self.get_parameter("min_tool_use_sec").value)
        self._active = False
        self._world: WorldState | None = None
        self._phase_hint: FilteredPhase | None = None
        self._gesture_history: deque[dict[str, object]] = deque(maxlen=6)
        self._latched_vlm_state: dict[str, object] | None = None
        self._latched_vlm_ticks = 0
        self._active_override: SurgeonRequest | None = None
        self._override_queue: deque[SurgeonRequest] = deque(maxlen=10)
        self._published_request_signature = ""
        self._published_actor_signature = ""
        self._cancel_cooldown_ticks = 0
        self._active_voice_text = ""
        self._voice_hold_ticks = 0
        self._phase_entered_sec = 0.0
        self._current_phase_id = ""
        self._last_requested_tool_sec: dict[str, float] = {}
        self._last_lifecycle_by_tool: dict[str, str] = {}
        self._surgeon_tool_received_sec: dict[str, float] = {}
        self._rng = random.Random(7)

        self._load_spec(self._spec_dir)
        self.add_on_set_parameters_callback(self._on_parameters_changed)

        self._state_pub = self.create_publisher(SurgeonState, "/surgeon/state", 20)
        self._request_pub = self.create_publisher(SurgeonRequest, "/surgeon/request", 20)
        self._voice_pub = self.create_publisher(String, "/surgery/audio/request_text", 10)
        self._actor_event_pub = self.create_publisher(SurgeonActorEvent, "/surgeon/actor_event", 20)

        self.create_subscription(String, "/simulation/control_state", self._on_control, 20)
        self.create_subscription(SurgeonRequest, "/simulation/surgeon_override", self._on_override, 20)
        self.create_subscription(WorldState, "/twin/world_state", self._on_world, 20)
        self.create_subscription(FilteredPhase, "/phase/filtered", self._on_phase_hint, 20)
        self.create_subscription(SurgeonGestureEvidence, "/vlm/surgeon_gesture_evidence", self._on_gesture_evidence, 20)

        self._timer = self.create_timer(self._decision_period_sec, self._tick)

    def _load_spec(self, spec_dir: str) -> None:
        self._spec_dir = spec_dir
        self._spec = load_bundle(spec_dir)
        self._voice_templates_by_phase: dict[str, list[tuple[str, str, str]]] = {}
        for stage in self._spec.get_mock_surgeon_stages():
            if stage.event_type != "voice_request":
                continue
            self._voice_templates_by_phase.setdefault(stage.phase_id, []).append(
                (stage.requested_tool, stage.voice_text, stage.scene_note)
            )
        self._current_phase_id = self._spec.default_phase_id
        self._phase_entered_sec = self._current_time_sec()

    def _on_parameters_changed(self, params):
        for parameter in params:
            if parameter.name == "spec_dir":
                try:
                    self._load_spec(str(parameter.value))
                except Exception as exc:
                    return SetParametersResult(successful=False, reason=str(exc))
            elif parameter.name == "decision_period_sec":
                self._decision_period_sec = float(parameter.value)
            elif parameter.name == "random_voice_enabled":
                # Backward-compatible test hook: disabling the old random voice
                # generator now disables all autonomous surgeon-policy actions,
                # while still allowing VLM/manual cues and UI overrides through.
                self._autonomous_policy_enabled = bool(parameter.value)
            elif parameter.name == "same_tool_request_cooldown_sec":
                self._same_tool_request_cooldown_sec = float(parameter.value)
            elif parameter.name == "autonomous_phase_progression_enabled":
                self._autonomous_phase_progression_enabled = bool(parameter.value)
            elif parameter.name == "min_tool_use_sec":
                self._min_tool_use_sec = max(0.0, float(parameter.value))
        return SetParametersResult(successful=True)

    def _current_time_sec(self) -> float:
        stamp = self.get_clock().now().to_msg()
        return float(stamp.sec) + float(stamp.nanosec) / 1_000_000_000.0

    def _display_tool_name(self, tool_id: str) -> str:
        return TOOL_DISPLAY_NAMES.get(tool_id, tool_id.replace("_", " "))

    def _coerce_phase_id(self, phase_id: str) -> str:
        return phase_id if phase_id in self._spec.phase_ids else self._spec.default_phase_id

    def _cancel_cooldown_window_ticks(self) -> int:
        # Keep the next autonomous request outside the audit's short
        # cancel-request window. Manual/VLM cues still bypass this delay.
        return max(2, int(4.5 / max(self._decision_period_sec, 0.1)))

    def _instrument_map(self):
        if self._world is None:
            return {}
        return {instrument.instrument_id: instrument for instrument in self._world.instrument_states}

    def _tool_state(self, tool_id: str):
        return self._instrument_map().get(tool_id)

    def _tool_lifecycle(self, tool_id: str) -> str:
        state = self._tool_state(tool_id)
        return getattr(state, "lifecycle_stage", "") if state is not None else ""

    def _tool_is_with_surgeon(self, tool_id: str) -> bool:
        state = self._tool_state(tool_id)
        if state is None:
            return False
        return getattr(state, "lifecycle_stage", "") in {"surgeon_owned", "mayo_reuse", "mayo_recovery"}

    def _tool_is_temporarily_unavailable(self, tool_id: str) -> bool:
        state = self._tool_state(tool_id)
        if state is None:
            return False
        return getattr(state, "lifecycle_stage", "") in {"mayo_recovery", "recovering_left", "cleaning_left", "cleaned_left"}

    def _current_surgeon_tool(self) -> str:
        if self._world is None:
            return ""
        hand_tools = []
        for instrument in self._world.instrument_states:
            lifecycle = getattr(instrument, "lifecycle_stage", "")
            location_type = getattr(instrument, "location_type", "")
            status = getattr(instrument, "status", "")
            if lifecycle == "surgeon_owned" and (
                location_type in {"surgeon_hand", "return_zone"} or status == "handed_over"
            ):
                hand_tools.append(instrument.instrument_id)
        if hand_tools:
            return hand_tools[0]
        return ""

    def _reuse_candidate(self, expected_tools: list[str]) -> str:
        if self._world is None:
            return ""
        for tool_id in expected_tools:
            if self._tool_lifecycle(tool_id) == "mayo_reuse":
                return tool_id
        return ""

    def _non_current_reuse_candidate(self, expected_tools: list[str]) -> str:
        if self._world is None:
            return ""
        expected_set = set(expected_tools)
        for instrument in self._world.instrument_states:
            tool_id = getattr(instrument, "instrument_id", "")
            if tool_id and tool_id not in expected_set and getattr(instrument, "lifecycle_stage", "") == "mayo_reuse":
                return tool_id
        return ""

    def _select_requestable_tool(self, phase_id: str, preferred_tool: str = "") -> str:
        if self._world is None:
            return preferred_tool or ""
        phase_id = self._coerce_phase_id(phase_id)
        candidates: list[str] = []
        if preferred_tool:
            candidates.append(preferred_tool)
        candidates.extend(self._spec.get_expected_instruments(phase_id))
        seen: set[str] = set()
        for tool_id in candidates:
            if not tool_id or tool_id in seen:
                continue
            seen.add(tool_id)
            lifecycle = self._tool_lifecycle(tool_id)
            if lifecycle == "prepositioned_right":
                return tool_id
            if self._tool_is_with_surgeon(tool_id):
                continue
            if self._tool_is_temporarily_unavailable(tool_id):
                continue
            last_requested_sec = self._last_requested_tool_sec.get(tool_id, -9999.0)
            if self._current_time_sec() - last_requested_sec < self._same_tool_request_cooldown_sec:
                continue
            return tool_id
        return ""

    def _choose_voice_text(self, phase_id: str, tool_id: str) -> tuple[str, str]:
        phase_id = self._coerce_phase_id(phase_id)
        templates = self._voice_templates_by_phase.get(phase_id, [])
        for template_tool, voice_text, note in templates:
            if template_tool == tool_id and voice_text.strip():
                return voice_text.strip(), note
        spoken_tool = self._display_tool_name(tool_id)
        return f"{spoken_tool} please", f"Surgeon requests {spoken_tool}."

    def _tool_might_be_reused(self, tool_id: str, current_phase: str) -> bool:
        if not tool_id:
            return False
        current_phase = self._coerce_phase_id(current_phase)
        if self._is_terminal_phase(current_phase) and self._terminal_phase_ready():
            return False
        if tool_id in self._spec.get_expected_instruments(current_phase):
            return True
        for next_phase in self._spec.get_allowed_next_phases(current_phase):
            if tool_id in self._spec.get_expected_instruments(next_phase):
                return True
        return False

    def _tool_min_use_elapsed(self, tool_id: str) -> bool:
        if not tool_id:
            return True
        lifecycle = self._tool_lifecycle(tool_id)
        if lifecycle != "surgeon_owned":
            return True
        now_sec = self._current_time_sec()
        received_sec = self._surgeon_tool_received_sec.get(tool_id)
        if received_sec is None:
            self._surgeon_tool_received_sec[tool_id] = now_sec
            return self._min_tool_use_sec <= 0.0
        return now_sec - received_sec >= self._min_tool_use_sec

    def _hold_tool_until_min_use(self, tool_id: str, phase_id: str) -> ActorDecision:
        return ActorDecision(
            intent="continue_using",
            requested_tool=tool_id,
            scene_note=(
                f"Surgeon continues using {self._display_tool_name(tool_id)} "
                "before it can be parked or recovered."
            ),
            phase_id=phase_id,
        )

    def _is_terminal_phase(self, phase_id: str) -> bool:
        phase_order = self._spec.phase_ids
        return bool(phase_order and phase_id == phase_order[-1])

    def _terminal_phase_ready(self) -> bool:
        if self._world is None:
            return False
        phase_id = self._coerce_phase_id(self._world.filtered_phase or self._spec.default_phase_id)
        if not self._is_terminal_phase(phase_id):
            return False
        guard = self._spec.bundle.phase_guard
        dwell_elapsed = self._current_time_sec() - self._phase_entered_sec
        return dwell_elapsed >= max(
            float(guard.min_dwell_time_sec),
            float(self._spec.get_phase_min_duration(phase_id)),
        )

    def _cleanup_candidate_tool(self) -> str:
        if self._world is None:
            return ""
        current_tool = self._current_surgeon_tool()
        if current_tool:
            return current_tool
        for instrument in self._world.instrument_states:
            if getattr(instrument, "lifecycle_stage", "") in {"mayo_reuse", "surgeon_owned"}:
                return instrument.instrument_id
        return ""

    def _cleanup_still_pending(self) -> bool:
        if self._world is None:
            return True
        if (
            self._world.cleaner_busy
            or self._world.left_hand_tool
            or self._world.right_hand_tool
            or self._world.prepositioned_tool
            or self._world.pending_transition_tools
            or self._world.active_recovery_tools
            or self._world.active_robot_task_id
        ):
            return True
        for instrument in self._world.instrument_states:
            if getattr(instrument, "lifecycle_stage", "") not in {"home_rack", "returned_home"}:
                return True
        return False

    def _phase_interactions_complete_for_actor(self, phase_id: str) -> bool:
        """Only cue phase advancement after the surgeon has actually exercised the phase tools."""
        if self._world is None:
            return False
        phase_id = self._coerce_phase_id(phase_id)
        expected_tools = list(self._spec.get_expected_instruments(phase_id))
        if not expected_tools:
            return True
        completed_stages = {
            "surgeon_owned",
            "mayo_reuse",
            "mayo_recovery",
            "recovering_left",
            "cleaning_left",
            "cleaned_left",
            "returned_home",
        }
        states = self._instrument_map()
        for tool_id in expected_tools:
            state = states.get(tool_id)
            if state is None:
                return False
            lifecycle = getattr(state, "lifecycle_stage", "")
            status = getattr(state, "status", "")
            if lifecycle in completed_stages or status in {"handed_over", "in_use", "used", "contaminated"}:
                continue
            return False
        return True

    def _phase_advance_candidate(self) -> str:
        if self._world is None:
            return ""
        current_phase = self._coerce_phase_id(self._world.filtered_phase or self._spec.default_phase_id)
        guard = self._spec.bundle.phase_guard
        dwell_elapsed = self._current_time_sec() - self._phase_entered_sec
        min_dwell_sec = max(
            float(guard.min_dwell_time_sec),
            float(self._spec.get_phase_min_duration(current_phase)),
        )
        if dwell_elapsed < min_dwell_sec:
            return ""
        if self._world.cleaner_busy or self._world.pending_transition_tools:
            return ""
        if not self._phase_interactions_complete_for_actor(current_phase):
            return ""

        if self._phase_hint is not None:
            candidate = self._phase_hint.phase_id or ""
            if candidate and candidate != current_phase:
                if not self._spec.is_transition_allowed(current_phase, candidate):
                    return ""
                if bool(self._phase_hint.uncertain) or float(self._phase_hint.confidence) < float(
                    guard.min_confidence_to_switch
                ):
                    return ""
                return candidate

        if not self._autonomous_phase_progression_enabled:
            return ""

        return self._autonomous_phase_candidate(current_phase)

    def _autonomous_phase_candidate(self, current_phase: str) -> str:
        allowed_next = [
            phase_id
            for phase_id in self._spec.get_allowed_next_phases(current_phase)
            if phase_id != current_phase
        ]
        if not allowed_next:
            return ""
        phase_order = self._spec.phase_ids
        try:
            current_index = phase_order.index(current_phase)
        except ValueError:
            current_index = -1

        forward_allowed = [
            phase_id
            for phase_id in phase_order[current_index + 1 :]
            if phase_id in allowed_next
        ]
        if forward_allowed:
            return forward_allowed[0]

        # Do not auto-loop closure back to the first phase. Cyclic transitions
        # stay available for explicit VLM/manual phase evidence, but the
        # autonomous surgeon policy should stop at the end of the script.
        return ""

    def _prune_gesture_history(self, now_sec: float) -> None:
        while self._gesture_history and (now_sec - float(self._gesture_history[0]["stamp_sec"])) > 3.5:
            self._gesture_history.popleft()

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
            bucket = candidates.setdefault(signature, {"score": 0.0, "count": 0, "latest": sample})
            bucket["score"] = float(bucket["score"]) + weight
            bucket["count"] = int(bucket["count"]) + 1
            if float(sample["stamp_sec"]) >= float(bucket["latest"]["stamp_sec"]):
                bucket["latest"] = sample
        if not candidates:
            return None
        ranked = sorted(
            candidates.items(),
            key=lambda item: (float(item[1]["score"]), float(item[1]["latest"]["confidence"])),
            reverse=True,
        )
        for (event_type, requested_tool), best in ranked:
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
                if not requested_tool or self._tool_is_with_surgeon(requested_tool) or self._tool_is_temporarily_unavailable(requested_tool):
                    continue
                return {
                    "phase_id": latest["phase_id"] or (self._world.filtered_phase if self._world else self._current_phase_id),
                    "intent": "request_tool",
                    "requested_tool": requested_tool,
                    "ready_for_handover": True,
                    "ready_for_retrieval": False,
                    "scene_note": latest["note"] or "VLM inferred an open hand requesting a tool.",
                }
            if event_type == "return_tool":
                if not requested_tool or not self._tool_is_with_surgeon(requested_tool):
                    continue
                return {
                    "phase_id": latest["phase_id"] or (self._world.filtered_phase if self._world else self._current_phase_id),
                    "intent": "return_tool",
                    "requested_tool": requested_tool,
                    "ready_for_handover": False,
                    "ready_for_retrieval": True,
                    "scene_note": latest["note"] or "VLM inferred a used tool being presented for retrieval.",
                }
            if event_type == "request_procedure_completion":
                if self._world is None or not self._is_terminal_phase(self._world.filtered_phase):
                    continue
                return {
                    "phase_id": latest["phase_id"] or (self._world.filtered_phase if self._world else self._current_phase_id),
                    "intent": "request_procedure_completion",
                    "requested_tool": "",
                    "ready_for_handover": False,
                    "ready_for_retrieval": False,
                    "scene_note": latest["note"] or "VLM detected the surgeon requesting procedure completion.",
                }
            if event_type == "complete_procedure":
                if self._world is None or self._cleanup_still_pending():
                    continue
                return {
                    "phase_id": latest["phase_id"] or (self._world.filtered_phase if self._world else self._current_phase_id),
                    "intent": "complete_procedure",
                    "requested_tool": "",
                    "ready_for_handover": False,
                    "ready_for_retrieval": False,
                    "scene_note": latest["note"] or "VLM detected the surgeon confirming procedure completion.",
                }
        return None

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
            return self._tool_is_with_surgeon(requested_tool)
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

    def _publish_request(self, request: SurgeonRequest) -> None:
        request.stamp = self.get_clock().now().to_msg()
        self._request_pub.publish(request)
        voice = String()
        voice.data = request.voice_text if request.voice_text else ""
        self._voice_pub.publish(voice)

    def _publish_actor_event(self, decision: ActorDecision, *, override: bool = False) -> None:
        if not decision.actor_event_type:
            return
        signature = f"{decision.actor_event_type}:{decision.actor_tool_id}:{decision.actor_phase_id}:{decision.voice_text}"
        if signature == self._published_actor_signature:
            return
        event = SurgeonActorEvent()
        event.stamp = self.get_clock().now().to_msg()
        event.event_type = decision.actor_event_type
        event.tool_id = decision.actor_tool_id
        event.phase_id = decision.actor_phase_id
        event.voice_text = decision.voice_text
        event.note = decision.scene_note
        event.ready_for_handover = bool(decision.ready_for_handover)
        event.ready_for_retrieval = bool(decision.ready_for_retrieval)
        event.override = bool(override)
        self._actor_event_pub.publish(event)
        self._published_actor_signature = signature

    def _publish_request_transition(self, decision: ActorDecision | None, *, override: bool = False) -> None:
        request_intents = REQUEST_INTENTS.union(RETURN_INTENTS).union(PROCEDURE_INTENTS)
        signature = f"{decision.intent}:{decision.requested_tool}:{decision.voice_text}" if decision and decision.intent in request_intents else ""
        if signature == self._published_request_signature:
            return
        if decision and decision.intent in request_intents:
            request = SurgeonRequest()
            request.event_type = decision.intent
            request.requested_tool = decision.requested_tool
            request.voice_text = decision.voice_text
            request.ready_for_handover = bool(decision.ready_for_handover)
            request.ready_for_retrieval = bool(decision.ready_for_retrieval)
            request.override = bool(override)
            request.note = decision.scene_note
            self._publish_request(request)
            if decision.intent in REQUEST_INTENTS and decision.requested_tool:
                self._last_requested_tool_sec[decision.requested_tool] = self._current_time_sec()
        self._published_request_signature = signature

    def _publish_state(self, decision: ActorDecision, *, scripted: bool) -> None:
        state = SurgeonState()
        state.stamp = self.get_clock().now().to_msg()
        state.procedure_id = self._spec.procedure_id
        state.phase_id = decision.phase_id or (self._world.filtered_phase if self._world else self._current_phase_id)
        state.intent = decision.intent
        state.requested_tool = decision.requested_tool
        state.ready_for_handover = bool(decision.ready_for_handover)
        state.ready_for_retrieval = bool(decision.ready_for_retrieval)
        state.scripted = bool(scripted)
        state.voice_text = self._active_voice_text or decision.voice_text
        state.scene_note = decision.scene_note
        self._state_pub.publish(state)

    def _set_active_voice(self, voice_text: str, hold_ticks: int = 4) -> None:
        self._active_voice_text = voice_text.strip()
        self._voice_hold_ticks = hold_ticks if self._active_voice_text else 0

    def _decay_active_voice(self) -> None:
        if self._voice_hold_ticks > 0:
            self._voice_hold_ticks -= 1
            if self._voice_hold_ticks == 0:
                self._active_voice_text = ""

    def _clear_active_override(self) -> None:
        self._active_override = None

    def _activate_override(self, override: SurgeonRequest) -> None:
        self._active_override = override
        if override.voice_text:
            self._set_active_voice(override.voice_text, hold_ticks=6)

    def _decision_from_override(self, override: SurgeonRequest) -> ActorDecision:
        tool_id = self._spec.resolve_instrument_alias(override.requested_tool) or override.requested_tool
        actor_event_type = ""
        actor_tool_id = tool_id
        if override.event_type in {"request_tool", "voice_request", "return_tool", *PROCEDURE_INTENTS}:
            actor_event_type = override.event_type
        return ActorDecision(
            intent=override.event_type,
            requested_tool=tool_id,
            voice_text=override.voice_text,
            ready_for_handover=bool(override.ready_for_handover),
            ready_for_retrieval=bool(override.ready_for_retrieval),
            scene_note=override.note or "UI override injected",
            phase_id=self._world.filtered_phase if self._world else self._current_phase_id,
            actor_event_type=actor_event_type,
            actor_tool_id=actor_tool_id,
        )

    def _decide(self) -> ActorDecision:
        assert self._world is not None
        phase_id = self._coerce_phase_id(self._world.filtered_phase or self._spec.default_phase_id)
        expected_tools = list(self._spec.get_expected_instruments(phase_id))
        current_tool = self._current_surgeon_tool()

        effective_vlm_state = self._effective_vlm_state(self._stable_gesture_request())
        if effective_vlm_state is not None:
            tool_id = str(effective_vlm_state["requested_tool"])
            voice_text = ""
            actor_event_type = str(effective_vlm_state["intent"])
            if str(effective_vlm_state["intent"]) == "request_tool":
                voice_text, _ = self._choose_voice_text(phase_id, tool_id)
                self._set_active_voice(voice_text)
            elif str(effective_vlm_state["intent"]) == "return_tool":
                actor_event_type = "place_on_mayo_recovery"
            elif str(effective_vlm_state["intent"]) in {
                "request_procedure_completion",
                "complete_procedure",
            }:
                actor_event_type = ""
            return ActorDecision(
                intent=str(effective_vlm_state["intent"]),
                requested_tool=tool_id,
                voice_text=voice_text,
                ready_for_handover=bool(effective_vlm_state["ready_for_handover"]),
                ready_for_retrieval=bool(effective_vlm_state["ready_for_retrieval"]),
                scene_note=str(effective_vlm_state["scene_note"]),
                phase_id=str(effective_vlm_state["phase_id"]) or phase_id,
                actor_event_type=actor_event_type,
                actor_tool_id=tool_id,
            )

        if (
            self._world.cleaner_busy
            or self._world.left_hand_tool
            or self._world.pending_transition_tools
            or self._world.active_robot_task_id
        ):
            return ActorDecision(
                intent="idle",
                scene_note="Surgeon waits while the assistant completes a pending tool transition.",
                phase_id=phase_id,
            )

        if self._world.surgeon_request_tool:
            return ActorDecision(
                intent="idle",
                scene_note="Surgeon waits for the assistant to complete the active request cue.",
                phase_id=phase_id,
            )

        if self._world.execution_state == "completed":
            return ActorDecision(
                intent="procedure_complete",
                scene_note="Procedure is complete; surgeon holds final posture.",
                phase_id=phase_id,
            )

        if not self._autonomous_policy_enabled:
            return ActorDecision(
                intent="idle",
                scene_note="Autonomous surgeon policy disabled; waiting for VLM/manual cue.",
                phase_id=phase_id,
            )

        next_phase = self._phase_advance_candidate()
        if next_phase:
            return ActorDecision(
                intent="advance_phase_cue",
                scene_note=f"Surgeon indicates the procedure can advance to {next_phase}.",
                phase_id=phase_id,
                actor_event_type="advance_phase_cue",
                actor_phase_id=next_phase,
            )

        if self._world.execution_state == "finishing":
            cleanup_tool = self._cleanup_candidate_tool()
            if cleanup_tool:
                if self._tool_lifecycle(cleanup_tool) == "surgeon_owned" and not self._tool_min_use_elapsed(cleanup_tool):
                    return self._hold_tool_until_min_use(cleanup_tool, phase_id)
                return ActorDecision(
                    intent="return_tool",
                    requested_tool=cleanup_tool,
                    ready_for_retrieval=True,
                    scene_note=f"Surgeon marks {self._display_tool_name(cleanup_tool)} for recovery before closing.",
                    phase_id=phase_id,
                    actor_event_type="place_on_mayo_recovery",
                    actor_tool_id=cleanup_tool,
                )
            if self._cleanup_still_pending():
                return ActorDecision(
                    intent="idle",
                    scene_note="Surgeon waits while the assistant returns remaining instruments home.",
                    phase_id=phase_id,
                )
            return ActorDecision(
                intent="idle",
                scene_note="Surgeon waits for VLM confirmation before terminating the procedure.",
                phase_id=phase_id,
            )

        if self._terminal_phase_ready():
            return ActorDecision(
                intent="idle",
                scene_note=f"Surgeon holds closure posture while VLM watches for completion request.",
                phase_id=phase_id,
            )

        if current_tool:
            lifecycle = self._tool_lifecycle(current_tool)
            if lifecycle == "surgeon_owned" and not self._tool_min_use_elapsed(current_tool):
                return self._hold_tool_until_min_use(current_tool, phase_id)
            if lifecycle == "mayo_reuse" and current_tool in expected_tools:
                return ActorDecision(
                    intent="continue_using",
                    requested_tool=current_tool,
                    scene_note=f"Surgeon picks {self._display_tool_name(current_tool)} back up from mayo reuse.",
                    phase_id=phase_id,
                    actor_event_type="continue_using",
                    actor_tool_id=current_tool,
                )

            if current_tool not in expected_tools:
                return ActorDecision(
                    intent="return_tool",
                    requested_tool=current_tool,
                    ready_for_retrieval=True,
                    scene_note=f"Surgeon presents {self._display_tool_name(current_tool)} for recovery.",
                    phase_id=phase_id,
                    actor_event_type="place_on_mayo_recovery",
                    actor_tool_id=current_tool,
                )

            next_needed_tool = ""
            for tool_id in expected_tools:
                if tool_id == current_tool:
                    continue
                if self._tool_is_with_surgeon(tool_id):
                    continue
                if self._tool_is_temporarily_unavailable(tool_id):
                    continue
                next_needed_tool = tool_id
                break
            if next_needed_tool and self._tool_might_be_reused(current_tool, phase_id):
                return ActorDecision(
                    intent="place_on_mayo_reuse",
                    requested_tool=current_tool,
                    scene_note=f"Surgeon parks {self._display_tool_name(current_tool)} to free a hand for {self._display_tool_name(next_needed_tool)}.",
                    phase_id=phase_id,
                    actor_event_type="place_on_mayo_reuse",
                    actor_tool_id=current_tool,
                )
            return ActorDecision(
                intent="continue_using",
                requested_tool=current_tool,
                scene_note=f"Surgeon continues using {self._display_tool_name(current_tool)}.",
                phase_id=phase_id,
            )

        non_current_reuse_tool = self._non_current_reuse_candidate(expected_tools)
        if non_current_reuse_tool:
            return ActorDecision(
                intent="return_tool",
                requested_tool=non_current_reuse_tool,
                ready_for_retrieval=True,
                scene_note=f"Surgeon flags {self._display_tool_name(non_current_reuse_tool)} on reuse for recovery because it is not needed in the current phase.",
                phase_id=phase_id,
                actor_event_type="place_on_mayo_recovery",
                actor_tool_id=non_current_reuse_tool,
            )

        requestable_tool = self._select_requestable_tool(phase_id)
        if requestable_tool:
            voice_text, scene_note = self._choose_voice_text(phase_id, requestable_tool)
            self._set_active_voice(voice_text)
            return ActorDecision(
                intent="request_tool",
                requested_tool=requestable_tool,
                voice_text=voice_text,
                ready_for_handover=True,
                scene_note=scene_note,
                phase_id=phase_id,
                actor_event_type="request_tool",
                actor_tool_id=requestable_tool,
            )

        if expected_tools and all(self._tool_is_with_surgeon(tool_id) for tool_id in expected_tools):
            return ActorDecision(
                intent="idle",
                scene_note="Surgeon waits for VLM phase inference after expected instruments were delivered.",
                phase_id=phase_id,
            )

        reuse_tool = self._reuse_candidate(expected_tools)
        if reuse_tool:
            return ActorDecision(
                intent="continue_using",
                requested_tool=reuse_tool,
                scene_note=f"Surgeon retrieves {self._display_tool_name(reuse_tool)} from mayo reuse.",
                phase_id=phase_id,
                actor_event_type="continue_using",
                actor_tool_id=reuse_tool,
            )

        return ActorDecision(
            intent="idle",
            scene_note="Surgeon holds current posture.",
            phase_id=phase_id,
        )

    def _tick(self) -> None:
        if not self._active or self._world is None:
            return
        if self._active_override is not None:
            decision = self._decision_from_override(self._active_override)
            self._publish_request_transition(decision, override=True)
            self._publish_actor_event(decision, override=True)
            self._publish_state(decision, scripted=False)
            self._clear_active_override()
            return
        if self._override_queue:
            self._activate_override(self._override_queue.popleft())
            return

        decision = self._decide()
        self._publish_request_transition(decision)
        self._publish_actor_event(decision)
        self._publish_state(decision, scripted=False)
        self._decay_active_voice()

    def _on_control(self, msg: String) -> None:
        raw_command = msg.data.strip()
        command, _, start_phase_id = raw_command.partition(":")
        command = command.strip().lower()
        start_phase_id = start_phase_id.strip()
        if command in {"start", "start_actors"}:
            self._phase_hint = None
            if start_phase_id:
                self._current_phase_id = self._coerce_phase_id(start_phase_id)
            self._phase_entered_sec = self._current_time_sec()
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
            self._phase_hint = None
            self._current_phase_id = self._coerce_phase_id(start_phase_id) if start_phase_id else self._spec.default_phase_id
            self._phase_entered_sec = self._current_time_sec()
            self._active_voice_text = ""
            self._voice_hold_ticks = 0
            self._gesture_history.clear()
            self._latched_vlm_state = None
            self._latched_vlm_ticks = 0
            self._override_queue.clear()
            self._clear_active_override()
            self._published_request_signature = ""
            self._published_actor_signature = ""
            self._cancel_cooldown_ticks = 0
            self._last_requested_tool_sec.clear()
            self._last_lifecycle_by_tool.clear()
            self._surgeon_tool_received_sec.clear()

    def _on_override(self, msg: SurgeonRequest) -> None:
        if msg.event_type == "cancel_request":
            self._override_queue.clear()
            self._clear_active_override()
            self._published_request_signature = ""
            self._published_actor_signature = ""
            self._active_voice_text = ""
            self._voice_hold_ticks = 0
            return
        self._override_queue.append(msg)

    def _on_world(self, msg: WorldState) -> None:
        previous_phase = self._current_phase_id
        now_sec = self._current_time_sec()
        seen_tools: set[str] = set()
        for instrument in msg.instrument_states:
            tool_id = getattr(instrument, "instrument_id", "")
            if not tool_id:
                continue
            seen_tools.add(tool_id)
            lifecycle = getattr(instrument, "lifecycle_stage", "")
            previous_lifecycle = self._last_lifecycle_by_tool.get(tool_id, "")
            if lifecycle == "surgeon_owned" and previous_lifecycle != "surgeon_owned":
                self._surgeon_tool_received_sec[tool_id] = now_sec
            elif lifecycle != "surgeon_owned":
                self._surgeon_tool_received_sec.pop(tool_id, None)
            self._last_lifecycle_by_tool[tool_id] = lifecycle
        for tool_id in list(self._last_lifecycle_by_tool):
            if tool_id not in seen_tools:
                self._last_lifecycle_by_tool.pop(tool_id, None)
                self._surgeon_tool_received_sec.pop(tool_id, None)
        self._world = msg
        self._current_phase_id = self._coerce_phase_id(msg.filtered_phase or self._spec.default_phase_id)
        if previous_phase != self._current_phase_id:
            self._phase_entered_sec = self._current_time_sec()

    def _on_phase_hint(self, msg: FilteredPhase) -> None:
        self._phase_hint = msg

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


def main() -> None:
    rclpy.init()
    node = SurgeonActorNode()
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
