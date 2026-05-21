"""Mock perception publisher driven by procedure YAML."""

from __future__ import annotations

from collections import deque
import json
import random
from time import perf_counter

from procedure_spec import get_default_spec_dir, load_bundle
import rclpy
from rcl_interfaces.msg import SetParametersResult
from rclpy.node import Node
from std_msgs.msg import String
from surgical_msgs.msg import (
    PhaseEvidence,
    PerceptionScene,
    SimulationState,
    SurgeonGestureEvidence,
    SurgeonOutwardSignal,
    ToolObservation,
    VLMHealth,
    VLMResult,
)


class MockVLMNode(Node):
    def __init__(self) -> None:
        super().__init__("mock_vlm_node")
        self.declare_parameter("spec_dir", str(get_default_spec_dir()))
        self._spec_dir = str(self.get_parameter("spec_dir").value)
        self._active = False
        self._state_activation_enabled = False
        self._tick = 0
        self._timer = None
        self._rng = random.Random(7)
        self._phase_pub = self.create_publisher(PhaseEvidence, "/vlm/phase_evidence", 20)
        self._obs_pub = self.create_publisher(ToolObservation, "/vlm/tool_observations", 50)
        self._gesture_pub = self.create_publisher(SurgeonGestureEvidence, "/vlm/surgeon_gesture_evidence", 20)
        self._result_pub = self.create_publisher(VLMResult, "/vlm/result", 10)
        self._health_pub = self.create_publisher(VLMHealth, "/vlm/health", 10)
        self._request_pub = self.create_publisher(String, "/surgery/audio/request_text", 10)
        self._tool_location_history: dict[str, deque[str]] = {}
        self._latest_state: SimulationState | None = None
        self._latest_scene: PerceptionScene | None = None
        self._latest_outward_signal: SurgeonOutwardSignal | None = None
        self._state_phase_id = ""
        self._state_phase_ticks = 0
        self._state_stage_index = 0
        self._state_stage_ticks = 0
        self._delivered_by_phase: dict[str, set[str]] = {}
        self._completion_request_emitted = False
        self._completion_confirm_emitted = False
        self.declare_parameter("perception_scene_observations", True)
        self.declare_parameter("state_backed_observations", False)
        self.declare_parameter("scripted_gestures_enabled", False)
        self._load_spec(self._spec_dir)
        self.add_on_set_parameters_callback(self._on_parameters_changed)
        self.create_subscription(String, "/simulation/control_state", self._on_control, 20)
        self.create_subscription(PerceptionScene, "/simulation/perception_scene", self._on_scene, 20)
        self.create_subscription(SurgeonOutwardSignal, "/surgeon/outward_signal", self._on_outward_signal, 20)
        self.create_subscription(SimulationState, "/simulation/state", self._on_state, 20)

    def _load_spec(self, spec_dir: str) -> None:
        self._spec = load_bundle(spec_dir)
        self._scenario = self._spec.get_mock_perception_stages()
        if not self._scenario:
            raise ValueError(
                "The loaded procedure bundle does not define mock_perception stages."
            )
        self._scenario_length = sum(stage.duration_ticks for stage in self._scenario)
        if self._scenario_length <= 0:
            raise ValueError("Mock perception scenario must contain at least one positive-duration stage.")
        self._period_sec = float(self._spec.get_mock_perception_period_sec(default=1.0))
        self._bootstrap_tick = int(self._spec.get_mock_perception_bootstrap_tick())
        if self._timer is not None:
            self._timer.cancel()
        self._timer = self.create_timer(self._period_sec, self._publish)
        self._tick = 0
        self._tool_location_history = {}
        self._state_phase_id = ""
        self._state_phase_ticks = 0
        self._state_stage_index = 0
        self._state_stage_ticks = 0
        self._delivered_by_phase = {}
        self._completion_request_emitted = False
        self._completion_confirm_emitted = False

    def _state_backed_observations_enabled(self) -> bool:
        return bool(self.get_parameter("state_backed_observations").value)

    def _perception_scene_observations_enabled(self) -> bool:
        return bool(self.get_parameter("perception_scene_observations").value)

    def _scripted_gestures_enabled(self) -> bool:
        return bool(self.get_parameter("scripted_gestures_enabled").value)

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
        return SetParametersResult(successful=True)

    def _stage_for_tick(self, tick: int):
        cycle_tick = tick % self._scenario_length
        for stage in self._scenario:
            if cycle_tick < stage.duration_ticks:
                return stage
            cycle_tick -= stage.duration_ticks
        return self._scenario[-1]

    def _state_stage(self):
        return self._scenario[min(self._state_stage_index, len(self._scenario) - 1)]

    def _stage_primary_phase(self, stage) -> str:
        return stage.phase_hypotheses[0].phase_id if stage.phase_hypotheses else self._spec.default_phase_id

    def _mark_delivered_tools_from_state(self, state: SimulationState, phase_id: str) -> None:
        expected = set(self._spec.get_expected_instruments(phase_id))
        if not expected:
            return
        delivered = self._delivered_by_phase.setdefault(phase_id, set())
        delivered_lifecycles = {
            "surgeon_owned",
            "mayo_reuse",
            "mayo_recovery",
            "recovering_left",
            "cleaning_left",
            "cleaned_left",
            "returned_home",
        }
        for instrument in state.instrument_states:
            if instrument.instrument_id in expected and instrument.lifecycle_stage in delivered_lifecycles:
                delivered.add(instrument.instrument_id)

    def _mark_delivered_tools_from_scene(self, scene: PerceptionScene, phase_id: str) -> None:
        expected = set(self._spec.get_expected_instruments(phase_id))
        if not expected:
            return
        delivered = self._delivered_by_phase.setdefault(phase_id, set())
        delivered_location_types = {
            "surgeon_hand",
            "surgical_field",
            "bed_fixed_tool",
            "mayo_reuse_zone",
            "mayo_recovery_zone",
            "robot_left_hand",
            "cleaner_slot",
        }
        for tool_id, location_type in zip(scene.visible_tool_ids, scene.visible_location_types):
            if tool_id in expected and location_type in delivered_location_types:
                delivered.add(tool_id)

    def _scene_stage_ready_to_advance(self, scene: PerceptionScene, stage) -> bool:
        if self._state_stage_index >= len(self._scenario) - 1:
            return False
        if self._state_stage_ticks < max(int(stage.duration_ticks), 1):
            return False

        current_phase = self._stage_primary_phase(stage)
        next_phase = self._stage_primary_phase(self._scenario[self._state_stage_index + 1])
        if next_phase == current_phase:
            return True
        if scene.active_task_type:
            return False
        expected = set(self._spec.get_expected_instruments(current_phase))
        delivered = self._delivered_by_phase.get(current_phase, set())
        return expected.issubset(delivered)

    def _advance_scene_stage_if_ready(self, scene: PerceptionScene) -> None:
        stage = self._state_stage()
        phase_id = self._stage_primary_phase(stage)
        self._mark_delivered_tools_from_scene(scene, phase_id)
        self._state_stage_ticks += 1
        if self._scene_stage_ready_to_advance(scene, stage):
            self._state_stage_index += 1
            self._state_stage_ticks = 0

    def _state_stage_ready_to_advance(self, state: SimulationState, stage) -> bool:
        if self._state_stage_index >= len(self._scenario) - 1:
            return False
        if self._state_stage_ticks < max(int(stage.duration_ticks), 1):
            return False

        current_phase = self._stage_primary_phase(stage)
        next_phase = self._stage_primary_phase(self._scenario[self._state_stage_index + 1])
        if next_phase == current_phase:
            return True

        if state.active_robot_task_id or state.surgeon_request_tool:
            return False
        expected = set(self._spec.get_expected_instruments(current_phase))
        delivered = self._delivered_by_phase.get(current_phase, set())
        return expected.issubset(delivered)

    def _advance_state_stage_if_ready(self, state: SimulationState) -> None:
        stage = self._state_stage()
        phase_id = self._stage_primary_phase(stage)
        self._mark_delivered_tools_from_state(state, phase_id)
        self._state_stage_ticks += 1
        if self._state_stage_ready_to_advance(state, stage):
            self._state_stage_index += 1
            self._state_stage_ticks = 0

    @staticmethod
    def _clamp(value: float, low: float, high: float) -> float:
        return max(low, min(high, value))

    def _remember_observations(self, observations) -> None:
        for observation in observations:
            history = self._tool_location_history.setdefault(observation.instrument_id, deque(maxlen=4))
            history.append(observation.location_type)

    def _tool_recently_in(self, instrument_id: str, location_types: set[str]) -> bool:
        history = self._tool_location_history.get(instrument_id)
        if not history:
            return False
        return any(location in location_types for location in history)

    def _score_request_gesture(self, stage, gesture, observation_by_tool):
        tool_observation = observation_by_tool.get(gesture.requested_tool)
        phase_id = stage.phase_hypotheses[0].phase_id if stage.phase_hypotheses else self._spec.default_phase_id
        expected_tools = set(self._spec.get_expected_instruments(phase_id))

        score = float(gesture.confidence or 0.42)
        factors: list[str] = []
        if gesture.hand_pose == "open_receive":
            score += 0.14
            factors.append("open hand cue")
        if tool_observation is not None:
            if tool_observation.location_type in {"tray_slot", "mayo_reuse_zone", "mayo_stand"}:
                score += 0.18
                factors.append("tool staged and reachable")
            if tool_observation.location_type in {"mayo_recovery_zone", "return_zone", "surgical_field"}:
                score -= 0.28
                factors.append("tool not in requestable holding area")
        if gesture.requested_tool in expected_tools:
            score += 0.12
            factors.append("phase expects tool")
        if stage.phase_hypotheses and float(stage.phase_hypotheses[0].confidence) >= 0.84:
            score += 0.06
            factors.append("stable phase context")
        if stage.uncertainty >= 0.35:
            score -= 0.06
            factors.append("phase uncertain")
        return self._clamp(score, 0.05, 0.99), factors

    def _score_return_gesture(self, stage, gesture, observation_by_tool):
        tool_observation = observation_by_tool.get(gesture.requested_tool)
        phase_id = stage.phase_hypotheses[0].phase_id if stage.phase_hypotheses else self._spec.default_phase_id
        expected_tools = set(self._spec.get_expected_instruments(phase_id))

        score = float(gesture.confidence or 0.45)
        factors: list[str] = []
        if gesture.hand_pose == "present_return":
            score += 0.16
            factors.append("returning hand cue")
        if tool_observation is not None:
            if tool_observation.location_type == "mayo_recovery_zone":
                score += 0.34
                factors.append("tool placed in mayo recovery zone")
            elif tool_observation.location_type == "return_zone":
                score += 0.28
                factors.append("tool presented in return zone")
            elif tool_observation.location_type == "surgical_field":
                score += 0.08
                factors.append("tool still active in field")
            elif tool_observation.location_type == "mayo_reuse_zone":
                score -= 0.18
                factors.append("tool parked for reuse")
        if gesture.requested_tool not in expected_tools:
            score += 0.12
            factors.append("phase no longer expects tool")
        if phase_id in {"closure", "pedicle_control", "vessel_control"}:
            score += 0.08
            factors.append("late-phase exchange context")
        if self._tool_recently_in(gesture.requested_tool, {"surgical_field", "surgeon_hand", "return_zone"}):
            score += 0.14
            factors.append("recent surgeon-side use")
        if stage.uncertainty >= 0.35:
            score -= 0.05
            factors.append("phase uncertain")
        return self._clamp(score, 0.05, 0.99), factors

    def _infer_contextual_return(self, stage, observation_by_tool):
        phase_id = stage.phase_hypotheses[0].phase_id if stage.phase_hypotheses else self._spec.default_phase_id
        expected_tools = set(self._spec.get_expected_instruments(phase_id))
        best_candidate = None
        best_score = 0.0
        best_factors: list[str] = []

        for tool_id, observation in observation_by_tool.items():
            if observation.location_type not in {"mayo_recovery_zone", "return_zone"}:
                continue
            score = 0.42 + float(observation.confidence) * 0.28
            factors = ["context-only return inference"]
            if observation.location_type == "mayo_recovery_zone":
                score += 0.24
                factors.append("recovery zone placement")
            if tool_id not in expected_tools:
                score += 0.08
                factors.append("tool not expected in current phase")
            if self._tool_recently_in(tool_id, {"surgical_field", "surgeon_hand"}):
                score += 0.12
                factors.append("recent field use")
            if score > best_score:
                best_score = score
                best_candidate = tool_id
                best_factors = factors

        if best_candidate is None:
            return None
        return {
            "event_type": "return_tool",
            "requested_tool": best_candidate,
            "hand_pose": "context_recovery_inference",
            "confidence": self._clamp(best_score, 0.05, 0.99),
            "note": f"VLM inferred retrieval need from {', '.join(best_factors)}.",
        }

    def _publish_gesture(self, stage, stamp) -> None:
        observation_by_tool = {
            observation.instrument_id: observation
            for observation in stage.observations
            if observation.visible
        }
        gesture = stage.surgeon_gesture
        gesture_payload = None

        if gesture is not None:
            if gesture.event_type == "request_tool":
                confidence, factors = self._score_request_gesture(stage, gesture, observation_by_tool)
            elif gesture.event_type == "return_tool":
                confidence, factors = self._score_return_gesture(stage, gesture, observation_by_tool)
            else:
                confidence, factors = float(gesture.confidence), []
            note = gesture.note or stage.scene_summary
            if factors:
                note = f"{note} Context: {', '.join(factors)}."
            gesture_payload = {
                "event_type": gesture.event_type,
                "requested_tool": gesture.requested_tool,
                "hand_pose": gesture.hand_pose,
                "confidence": confidence,
                "note": note,
            }
        else:
            gesture_payload = self._infer_contextual_return(stage, observation_by_tool)

        if gesture_payload is None:
            return

        noisy_event_type = str(gesture_payload["event_type"])
        noisy_tool = str(gesture_payload["requested_tool"])
        noisy_hand_pose = str(gesture_payload["hand_pose"])
        noisy_note = str(gesture_payload["note"])
        noisy_confidence = self._clamp(
            float(gesture_payload["confidence"]) + self._rng.uniform(-0.16, 0.12),
            0.05,
            0.99,
        )

        base_hand_pose = str(gesture_payload["hand_pose"])

        if self._rng.random() < 0.18:
            noisy_event_type = ""
            noisy_tool = ""
            noisy_hand_pose = "occluded"
            noisy_note = f"hand cue partially occluded during {stage.name}"
            noisy_confidence = self._clamp(noisy_confidence * 0.35, 0.05, 0.45)
        elif self._rng.random() < 0.14:
            noisy_hand_pose = f"uncertain_{base_hand_pose}"
            noisy_confidence = self._clamp(noisy_confidence * 0.62, 0.05, 0.75)

        evidence = SurgeonGestureEvidence()
        evidence.stamp = stamp
        evidence.procedure_id = self._spec.procedure_id
        evidence.phase_id = stage.phase_hypotheses[0].phase_id if stage.phase_hypotheses else ""
        evidence.event_type = noisy_event_type
        evidence.requested_tool = noisy_tool
        evidence.hand_pose = noisy_hand_pose
        evidence.confidence = float(noisy_confidence)
        evidence.note = noisy_note
        self._gesture_pub.publish(evidence)

    def _publish(self) -> None:
        if not self._active:
            self._publish_health(
                mode="idle",
                image_source="mock_vlm",
                latency_sec=0.0,
                output_chars=0,
            )
            return
        if self._perception_scene_observations_enabled() and self._latest_scene is not None:
            self._publish_from_scene(self._latest_scene)
            return
        if self._state_backed_observations_enabled() and self._latest_state is not None:
            self._publish_from_state(self._latest_state)
            return
        started_at = perf_counter()
        stage = self._stage_for_tick(self._tick)
        visible_observations = [observation for observation in stage.observations if observation.visible]

        evidence = PhaseEvidence()
        evidence.stamp = self.get_clock().now().to_msg()
        evidence.source = f"mock_vlm:{self._spec.procedure_id}"
        evidence.phase_ids = [hypothesis.phase_id for hypothesis in stage.phase_hypotheses]
        evidence.phase_confidences = [
            hypothesis.confidence for hypothesis in stage.phase_hypotheses
        ]
        evidence.visible_instrument_ids = [
            observation.instrument_id for observation in visible_observations
        ]
        evidence.visible_instrument_confidences = [
            observation.confidence for observation in visible_observations
        ]
        evidence.scene_summary = stage.scene_summary
        evidence.uncertainty = stage.uncertainty
        self._phase_pub.publish(evidence)

        for observation_spec in visible_observations:
            observation = ToolObservation()
            observation.stamp = evidence.stamp
            observation.instrument_id = observation_spec.instrument_id
            observation.location_id = observation_spec.location_id
            observation.location_type = observation_spec.location_type
            observation.confidence = observation_spec.confidence
            observation.visible = observation_spec.visible
            self._obs_pub.publish(observation)

        self._remember_observations(visible_observations)
        if self._scripted_gestures_enabled():
            self._publish_gesture(stage, evidence.stamp)

        request = String()
        request.data = stage.explicit_request if self._scripted_gestures_enabled() else ""
        self._request_pub.publish(request)

        raw_json = json.dumps(
            {
                "v": "mock-1",
                "mode": "scripted",
                "ph": [
                    [phase_id, confidence]
                    for phase_id, confidence in zip(evidence.phase_ids, evidence.phase_confidences)
                ],
                "to": [
                    [
                        observation.instrument_id,
                        observation.location_id,
                        observation.location_type,
                        round(float(observation.confidence), 3),
                    ]
                    for observation in visible_observations
                ],
                "sg": ["", "", "", 0.0],
                "u": round(float(evidence.uncertainty), 3),
                "sum": evidence.scene_summary,
            },
            separators=(",", ":"),
            ensure_ascii=False,
        )
        self._publish_result(
            stamp=evidence.stamp,
            source=evidence.source,
            raw_json=raw_json,
            summary=evidence.scene_summary,
            phase_ids=list(evidence.phase_ids),
            phase_confidences=list(evidence.phase_confidences),
            observations=visible_observations,
            gesture=None,
            uncertainty=float(evidence.uncertainty),
        )
        self._publish_health(
            mode="mock_scripted",
            image_source="scripted_mock_perception",
            latency_sec=perf_counter() - started_at,
            output_chars=len(raw_json),
        )
        self._tick += 1

    def _publish_from_state(self, state: SimulationState) -> None:
        started_at = perf_counter()
        stamp = self.get_clock().now().to_msg()
        stage = self._state_stage()
        visible_observations = []
        for instrument in state.instrument_states:
            if not instrument.location_type:
                continue
            observation = ToolObservation()
            observation.stamp = stamp
            observation.instrument_id = instrument.instrument_id
            observation.location_id = instrument.location_id
            observation.location_type = instrument.location_type
            observation.confidence = max(0.86, float(getattr(instrument, "confidence", 0.0) or 0.0))
            observation.visible = True
            self._obs_pub.publish(observation)
            visible_observations.append(observation)

        evidence = PhaseEvidence()
        evidence.stamp = stamp
        evidence.source = f"mock_vlm:state_gated:{self._spec.procedure_id}:{stage.name}"
        evidence.phase_ids = [hypothesis.phase_id for hypothesis in stage.phase_hypotheses]
        evidence.phase_confidences = [
            float(hypothesis.confidence) for hypothesis in stage.phase_hypotheses
        ]
        evidence.visible_instrument_ids = [observation.instrument_id for observation in visible_observations]
        evidence.visible_instrument_confidences = [
            observation.confidence for observation in visible_observations
        ]
        evidence.scene_summary = (
            f"state-gated mock VLM stage={stage.name} "
            f"robot={state.robot_state or 'idle'}"
        )
        evidence.uncertainty = float(stage.uncertainty)
        self._phase_pub.publish(evidence)

        self._remember_observations(visible_observations)

        completion_gesture = self._completion_gesture_from_state(state)
        evidence_msg = None
        if completion_gesture is not None:
            evidence_msg = SurgeonGestureEvidence()
            evidence_msg.stamp = stamp
            evidence_msg.procedure_id = self._spec.procedure_id
            evidence_msg.phase_id = state.filtered_phase
            evidence_msg.event_type = completion_gesture["event_type"]
            evidence_msg.requested_tool = ""
            evidence_msg.hand_pose = completion_gesture["hand_pose"]
            evidence_msg.confidence = float(completion_gesture["confidence"])
            evidence_msg.note = completion_gesture["note"]
            self._gesture_pub.publish(evidence_msg)

        request = String()
        request.data = ""
        self._request_pub.publish(request)

        raw_json = json.dumps(
            {
                "v": "mock-1",
                "mode": "state_gated",
                "stage": stage.name,
                "ph": [
                    [phase_id, round(float(confidence), 3)]
                    for phase_id, confidence in zip(evidence.phase_ids, evidence.phase_confidences)
                ],
                "to": [
                    [
                        observation.instrument_id,
                        observation.location_id,
                        observation.location_type,
                        round(float(observation.confidence), 3),
                    ]
                    for observation in visible_observations
                ],
                "sg": [
                    evidence_msg.event_type if evidence_msg is not None else "",
                    evidence_msg.requested_tool if evidence_msg is not None else "",
                    evidence_msg.hand_pose if evidence_msg is not None else "",
                    round(float(evidence_msg.confidence), 3) if evidence_msg is not None else 0.0,
                ],
                "u": round(float(evidence.uncertainty), 3),
                "sum": evidence.scene_summary,
            },
            separators=(",", ":"),
            ensure_ascii=False,
        )
        self._publish_result(
            stamp=stamp,
            source=evidence.source,
            raw_json=raw_json,
            summary=evidence.scene_summary,
            phase_ids=list(evidence.phase_ids),
            phase_confidences=list(evidence.phase_confidences),
            observations=visible_observations,
            gesture=evidence_msg,
            uncertainty=float(evidence.uncertainty),
        )
        self._publish_health(
            mode="mock_state_gated",
            image_source="authoritative_twin_state",
            latency_sec=perf_counter() - started_at,
            output_chars=len(raw_json),
        )
        self._advance_state_stage_if_ready(state)

    def _phase_hypotheses_for_scene(self, scene: PerceptionScene, stage) -> tuple[list[str], list[float], float]:
        phase_scores: dict[str, float] = {
            hypothesis.phase_id: float(hypothesis.confidence)
            for hypothesis in stage.phase_hypotheses
        }
        uncertainty = float(stage.uncertainty)
        if scene.surgeon_signal_type in {"advance_phase", "advance_phase_cue"} and scene.surgeon_signal_phase:
            target = scene.surgeon_signal_phase
            phase_scores[target] = max(phase_scores.get(target, 0.0), 0.92)
            uncertainty = min(uncertainty, 0.14)
        ranked = sorted(phase_scores.items(), key=lambda item: item[1], reverse=True)
        return [item[0] for item in ranked], [float(item[1]) for item in ranked], uncertainty

    def _gesture_from_scene(self, scene: PerceptionScene, stamp) -> SurgeonGestureEvidence | None:
        if not scene.surgeon_signal_type:
            return None
        event_type = scene.surgeon_signal_type
        if event_type == "voice_request":
            event_type = "request_tool"
        if event_type == "place_on_mayo_recovery":
            event_type = "return_tool"
        if event_type not in {
            "request_tool",
            "return_tool",
            "request_procedure_completion",
            "complete_procedure",
        }:
            return None
        confidence = self._clamp(0.88 + self._rng.uniform(-0.08, 0.06), 0.55, 0.98)
        hand_pose = scene.surgeon_hand_pose or "observed_signal"
        if self._rng.random() < 0.08:
            hand_pose = "occluded"
            confidence = self._clamp(confidence * 0.45, 0.10, 0.50)
            event_type = ""
        evidence = SurgeonGestureEvidence()
        evidence.stamp = stamp
        evidence.procedure_id = self._spec.procedure_id
        evidence.phase_id = scene.surgeon_signal_phase
        evidence.event_type = event_type
        evidence.requested_tool = scene.surgeon_signal_tool
        evidence.hand_pose = hand_pose
        evidence.confidence = float(confidence)
        evidence.note = scene.scene_summary or "Mock VLM observed surgeon outward signal."
        return evidence

    def _publish_from_scene(self, scene: PerceptionScene) -> None:
        started_at = perf_counter()
        stamp = self.get_clock().now().to_msg()
        stage = self._state_stage()
        visible_observations: list[ToolObservation] = []
        for tool_id, location_id, location_type, base_confidence in zip(
            scene.visible_tool_ids,
            scene.visible_location_ids,
            scene.visible_location_types,
            scene.visible_confidences,
        ):
            if not tool_id or not location_type:
                continue
            if self._rng.random() < 0.04:
                continue
            observation = ToolObservation()
            observation.stamp = stamp
            observation.instrument_id = tool_id
            observation.location_id = location_id
            observation.location_type = location_type
            observation.confidence = self._clamp(float(base_confidence) + self._rng.uniform(-0.05, 0.04), 0.25, 0.99)
            observation.visible = True
            self._obs_pub.publish(observation)
            visible_observations.append(observation)

        phase_ids, phase_confidences, uncertainty = self._phase_hypotheses_for_scene(scene, stage)
        evidence = PhaseEvidence()
        evidence.stamp = stamp
        evidence.source = f"mock_vlm:perception_scene:{self._spec.procedure_id}:{stage.name}"
        evidence.phase_ids = phase_ids
        evidence.phase_confidences = phase_confidences
        evidence.visible_instrument_ids = [observation.instrument_id for observation in visible_observations]
        evidence.visible_instrument_confidences = [float(observation.confidence) for observation in visible_observations]
        evidence.scene_summary = scene.scene_summary or f"perception-scene stage={stage.name}"
        evidence.uncertainty = float(uncertainty)
        self._phase_pub.publish(evidence)

        self._remember_observations(visible_observations)
        gesture = self._gesture_from_scene(scene, stamp)
        if gesture is not None:
            self._gesture_pub.publish(gesture)
        request = String()
        request.data = scene.speech_text or ""
        self._request_pub.publish(request)

        raw_json = json.dumps(
            {
                "v": "mock-2",
                "mode": "perception_scene",
                "stage": stage.name,
                "ph": [
                    [phase_id, round(float(confidence), 3)]
                    for phase_id, confidence in zip(evidence.phase_ids, evidence.phase_confidences)
                ],
                "to": [
                    [
                        observation.instrument_id,
                        observation.location_id,
                        observation.location_type,
                        round(float(observation.confidence), 3),
                    ]
                    for observation in visible_observations
                ],
                "sg": [
                    gesture.event_type if gesture is not None else "",
                    gesture.requested_tool if gesture is not None else "",
                    gesture.hand_pose if gesture is not None else "",
                    round(float(gesture.confidence), 3) if gesture is not None else 0.0,
                ],
                "u": round(float(evidence.uncertainty), 3),
                "sum": evidence.scene_summary,
            },
            separators=(",", ":"),
            ensure_ascii=False,
        )
        self._publish_result(
            stamp=stamp,
            source=evidence.source,
            raw_json=raw_json,
            summary=evidence.scene_summary,
            phase_ids=list(evidence.phase_ids),
            phase_confidences=list(evidence.phase_confidences),
            observations=visible_observations,
            gesture=gesture,
            uncertainty=float(evidence.uncertainty),
        )
        self._publish_health(
            mode="mock_perception_scene",
            image_source="perception_scene",
            latency_sec=perf_counter() - started_at,
            output_chars=len(raw_json),
        )
        self._advance_scene_stage_if_ready(scene)

    def _publish_result(
        self,
        *,
        stamp,
        source: str,
        raw_json: str,
        summary: str,
        phase_ids: list[str],
        phase_confidences: list[float],
        observations: list[ToolObservation],
        gesture: SurgeonGestureEvidence | None,
        uncertainty: float,
    ) -> None:
        result = VLMResult()
        result.stamp = stamp
        result.source = source
        result.schema_version = "mock-1"
        result.raw_json = raw_json
        result.summary = summary
        result.phase_ids = phase_ids
        result.phase_confidences = [float(value) for value in phase_confidences]
        result.observed_tool_ids = [observation.instrument_id for observation in observations]
        result.observed_location_ids = [observation.location_id for observation in observations]
        result.observed_location_types = [observation.location_type for observation in observations]
        result.observed_confidences = [float(observation.confidence) for observation in observations]
        result.gesture_event_type = gesture.event_type if gesture is not None else ""
        result.gesture_requested_tool = gesture.requested_tool if gesture is not None else ""
        result.gesture_hand_pose = gesture.hand_pose if gesture is not None else ""
        result.gesture_confidence = float(gesture.confidence) if gesture is not None else 0.0
        result.uncertainty = float(uncertainty)
        self._result_pub.publish(result)

    def _publish_health(
        self,
        *,
        mode: str,
        image_source: str,
        latency_sec: float,
        output_chars: int,
        last_error: str = "",
    ) -> None:
        health = VLMHealth()
        health.stamp = self.get_clock().now().to_msg()
        health.connected = True
        health.healthy = not bool(last_error)
        health.model_id = f"mock_vlm:{self._spec.procedure_id}"
        health.image_source = image_source
        health.latency_sec = float(latency_sec)
        health.prompt_chars = 0
        health.output_chars = int(output_chars)
        health.parse_retry_count = 0
        health.last_error = last_error
        health.last_mode = mode
        self._health_pub.publish(health)

    def _cleanup_pending_in_state(self, state: SimulationState) -> bool:
        if (
            state.cleaner_busy
            or state.left_hand_tool
            or state.right_hand_tool
            or state.prepositioned_tool
            or state.pending_transition_tools
            or state.active_recovery_tools
            or state.active_robot_task_id
        ):
            return True
        for instrument in state.instrument_states:
            if instrument.lifecycle_stage not in {"home_rack", "returned_home"}:
                return True
        return False

    def _completion_gesture_from_state(self, state: SimulationState) -> dict[str, object] | None:
        terminal_phase = self._spec.phase_ids[-1] if self._spec.phase_ids else ""
        if state.filtered_phase != terminal_phase:
            self._completion_request_emitted = False
            self._completion_confirm_emitted = False
            return None
        # State-backed VLM acts like a camera recognizer: after a stable closure
        # view, it infers the surgeon's completion request. The twin reducer,
        # not the VLM, decides whether that request can change runtime state.
        if (
            state.execution_state == "running"
            and self._state_phase_ticks >= max(3, int(self._spec.get_phase_min_duration(terminal_phase)))
            and not self._completion_request_emitted
        ):
            self._completion_request_emitted = True
            return {
                "event_type": "request_procedure_completion",
                "hand_pose": "closure_done_signal",
                "confidence": 0.94,
                "note": "VLM observes the surgeon signaling that closure is complete and cleanup should begin.",
            }
        if (
            state.execution_state == "finishing"
            and not self._cleanup_pending_in_state(state)
            and not self._completion_confirm_emitted
        ):
            self._completion_confirm_emitted = True
            return {
                "event_type": "complete_procedure",
                "hand_pose": "final_completion_confirmation",
                "confidence": 0.96,
                "note": "VLM observes final surgeon confirmation after all instruments returned home.",
            }
        return None

    def _on_control(self, msg: String) -> None:
        command = msg.data.strip().lower()
        if command in {"start", "start_actors"}:
            state_backed = self._state_backed_observations_enabled()
            scene_backed = self._perception_scene_observations_enabled()
            should_seed = (not self._active) and self._tick == 0 and not state_backed and not scene_backed
            # In state-backed mode the first running frame must come from the
            # authoritative twin. Otherwise a stale pre-reset state can seed the
            # phase estimator and make a newly selected procedure start midway.
            self._state_activation_enabled = True
            self._active = scene_backed or not state_backed
            if should_seed:
                self._publish()
        elif command == "pause":
            self._active = False
        elif command == "resume":
            self._state_activation_enabled = True
            self._active = True
        elif command == "stop":
            self._state_activation_enabled = False
            self._active = False
        elif command == "reset":
            self._state_activation_enabled = False
            self._active = False
            self._latest_state = None
            self._latest_scene = None
            self._latest_outward_signal = None
            self._tick = 0
            self._tool_location_history = {}
            self._state_phase_id = ""
            self._state_phase_ticks = 0
            self._state_stage_index = 0
            self._state_stage_ticks = 0
            self._delivered_by_phase = {}
            self._completion_request_emitted = False
            self._completion_confirm_emitted = False

    def _on_scene(self, msg: PerceptionScene) -> None:
        self._latest_scene = msg
        if (
            self._state_activation_enabled
            and msg.running
            and msg.execution_state not in {"idle", "halted", "completed", "paused"}
        ):
            self._active = True
        elif msg.execution_state in {"idle", "halted", "completed", "paused"}:
            self._active = False

    def _on_outward_signal(self, msg: SurgeonOutwardSignal) -> None:
        self._latest_outward_signal = msg

    def _on_state(self, msg: SimulationState) -> None:
        self._latest_state = msg
        if self._perception_scene_observations_enabled():
            return
        # The control-state topic is not latched, so a late subscriber or a
        # restarted mock VLM can otherwise sit silent while the authoritative
        # twin is already running. Treat SimulationState as the recovery signal:
        # if the session is active, state-backed VLM observations must resume.
        if (
            self._state_activation_enabled
            and msg.running
            and msg.execution_state not in {"idle", "halted", "completed", "paused"}
        ):
            self._active = True
        elif msg.execution_state in {"idle", "halted", "completed", "paused"}:
            self._active = False
        phase_id = msg.filtered_phase or self._spec.default_phase_id
        if phase_id != self._state_phase_id:
            self._state_phase_id = phase_id
            self._state_phase_ticks = 0
        else:
            self._state_phase_ticks += 1


def main() -> None:
    rclpy.init()
    node = MockVLMNode()
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
