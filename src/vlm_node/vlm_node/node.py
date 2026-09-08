"""Mock perception publisher driven by procedure YAML."""

from __future__ import annotations

import json
import random
from time import perf_counter

from procedure_spec import (
    BedRobotArmGroupNormalizationError,
    get_default_spec_dir,
    load_bundle,
    normalize_retraction_request,
)
import rclpy
from rcl_interfaces.msg import SetParametersResult
from rclpy.node import Node
from std_msgs.msg import String
from surgical_msgs.msg import (
    BedRobotArmGroupActionProposal,
    BedRobotArmGroupRequest,
    PhaseEvidence,
    PerceptionScene,
    SimulationState,
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
        self._result_pub = self.create_publisher(VLMResult, "/vlm/result", 10)
        self._health_pub = self.create_publisher(VLMHealth, "/vlm/health", 10)
        self._bed_group_proposal_pub = self.create_publisher(
            BedRobotArmGroupActionProposal,
            "/vlm/bed_robot_arm_group_proposal",
            20,
        )
        self._seen_bed_group_request_ids: set[str] = set()
        self._latest_state: SimulationState | None = None
        self._latest_scene: PerceptionScene | None = None
        self._state_phase_id = ""
        self._state_phase_ticks = 0
        self._state_stage_index = 0
        self._state_stage_ticks = 0
        self._delivered_by_phase: dict[str, set[str]] = {}
        self.declare_parameter("perception_scene_observations", True)
        self.declare_parameter("state_backed_observations", False)
        self.declare_parameter("bed_robot_arm_group_proposals_enabled", True)
        self._load_spec(self._spec_dir)
        self.add_on_set_parameters_callback(self._on_parameters_changed)
        self.create_subscription(String, "/simulation/control_state", self._on_control, 20)
        self.create_subscription(PerceptionScene, "/simulation/perception_scene", self._on_scene, 20)
        self.create_subscription(SimulationState, "/simulation/state", self._on_state, 20)
        self.create_subscription(
            BedRobotArmGroupRequest,
            "/surgeon/bed_robot_arm_group_request",
            self._on_bed_robot_arm_group_request,
            20,
        )

    @classmethod
    def _normalize_mock_group_request(cls, voice_text: str):
        return normalize_retraction_request(voice_text)

    def _on_bed_robot_arm_group_request(
        self, msg: BedRobotArmGroupRequest
    ) -> None:
        if not bool(self.get_parameter("bed_robot_arm_group_proposals_enabled").value):
            return
        if msg.group_id != "retraction" or msg.operation not in {
            "retraction",
            "retraction_adjustment",
        }:
            return
        if msg.direction_frame != "surgeon_view":
            return
        if msg.adjustment_mode == "single":
            if msg.target_retractor_id not in {
                "left_malleable",
                "right_malleable",
                "left_army_navy",
                "right_army_navy",
            }:
                return
        elif msg.adjustment_mode == "multi":
            if msg.target_retractor_id not in {
                "both_malleable",
                "both_army_navy",
            }:
                return
        else:
            return
        request_id = str(msg.request_id or "").strip()
        if not request_id or request_id in self._seen_bed_group_request_ids:
            return
        self._seen_bed_group_request_ids.add(request_id)

        proposal = BedRobotArmGroupActionProposal()
        proposal.stamp = self.get_clock().now().to_msg()
        proposal.schema_version = "4"
        command = proposal.command
        command.stamp = proposal.stamp
        command.request_id = request_id
        command.command_id = f"mock-vlm-{request_id}"
        command.procedure_run_id = msg.procedure_run_id
        command.group_id = "retraction"
        command.operation = "retraction"
        command.adjustment_mode = msg.adjustment_mode
        command.target_retractor_id = msg.target_retractor_id
        command.direction_frame = msg.direction_frame
        command.end_effector_profile = msg.end_effector_profile
        try:
            normalized = self._normalize_mock_group_request(msg.voice_text)
        except BedRobotArmGroupNormalizationError as exc:
            proposal.valid = False
            proposal.validation_error = (
                "mock VLM cannot ground one of the six retraction directions: "
                f"{exc}"
            )
            command.rationale = "insufficient spoken direction evidence in mock mode"
            command.confidence = 0.0
        else:
            proposal.valid = True
            if msg.adjustment_mode == "multi":
                command.direction = "none"
                command.axis = normalized.direction.lower()
            else:
                command.direction = normalized.direction.lower()
                command.axis = "none"
            command.distance_mm = float(normalized.distance_mm)
            command.distance_origin = normalized.distance_origin
            command.raw_distance_text = normalized.raw_distance_text
            command.rationale = "deterministic mock VLM interpretation of surgeon speech"
            command.confidence = 0.99
        proposal.raw_json = json.dumps(
            {
                "v": "4",
                "bed_robot_arm_group": {
                    "request_id": command.request_id,
                    "group_id": command.group_id,
                    "operation": command.operation,
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
                }
                if proposal.valid
                else None,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self._bed_group_proposal_pub.publish(proposal)

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
        self._state_phase_id = ""
        self._state_phase_ticks = 0
        self._state_stage_index = 0
        self._state_stage_ticks = 0
        self._delivered_by_phase = {}
        self._seen_bed_group_request_ids.clear()

    def _state_backed_observations_enabled(self) -> bool:
        return bool(self.get_parameter("state_backed_observations").value)

    def _perception_scene_observations_enabled(self) -> bool:
        return bool(self.get_parameter("perception_scene_observations").value)

    def _on_parameters_changed(self, params):
        for parameter in params:
            if parameter.name == "spec_dir":
                try:
                    self._spec_dir = str(parameter.value)
                    self._load_spec(self._spec_dir)
                    self._last_lifecycle_control_command = ""
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
            "surgeon",
            "surgeon_hand",
            "surgical_field",
            "mayo_stand",
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
        uncertainty: float,
    ) -> None:
        result = VLMResult()
        result.stamp = stamp
        latest_state = self._latest_state
        result.procedure_run_id = (
            str(latest_state.procedure_run_id or "").strip()
            if latest_state is not None
            and bool(latest_state.running)
            and str(latest_state.execution_state or "").strip().lower() == "running"
            else ""
        )
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

    def _on_control(self, msg: String) -> None:
        command = msg.data.strip().partition(":")[0].strip().lower()
        if command in {
            "start",
            "start_actors",
            "pause",
            "resume",
            "stop",
        }:
            if command == getattr(
                self, "_last_lifecycle_control_command", ""
            ):
                return
            self._last_lifecycle_control_command = command
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
            self._last_lifecycle_control_command = ""
            self._state_activation_enabled = False
            self._active = False
            self._latest_state = None
            self._latest_scene = None
            self._tick = 0
            self._state_phase_id = ""
            self._state_phase_ticks = 0
            self._state_stage_index = 0
            self._state_stage_ticks = 0
            self._delivered_by_phase = {}
            self._seen_bed_group_request_ids.clear()

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
