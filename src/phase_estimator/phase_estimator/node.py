"""ROS node wrapper for the phase estimator."""

from __future__ import annotations

from procedure_spec import get_default_spec_dir, load_bundle
import rclpy
from rcl_interfaces.msg import SetParametersResult
from rclpy.node import Node
from std_msgs.msg import String
from surgical_msgs.msg import FilteredPhase, PhaseEvidence, WorldState

from .estimator import PhaseEstimator


class PhaseEstimatorNode(Node):
    def __init__(self) -> None:
        super().__init__("phase_estimator")
        self.declare_parameter("spec_dir", str(get_default_spec_dir()))
        self._spec_dir = str(self.get_parameter("spec_dir").value)
        self._load_spec(self._spec_dir)
        self._last_lifecycle_control_signature: tuple[str, str] | None = None
        self.add_on_set_parameters_callback(self._on_parameters_changed)
        self._publisher = self.create_publisher(FilteredPhase, "/phase/filtered", 20)
        self.create_subscription(PhaseEvidence, "/vlm/phase_evidence", self._on_evidence, 20)
        self.create_subscription(WorldState, "/twin/world_state", self._on_world, 20)
        self.create_subscription(String, "/simulation/control_state", self._on_control, 20)

    def _load_spec(self, spec_dir: str) -> None:
        spec = load_bundle(spec_dir)
        self._estimator = PhaseEstimator(spec)
        self._prior_phase = spec.default_phase_id
        self._prior_confidence = 0.0

    def _on_parameters_changed(self, params):
        for parameter in params:
            if parameter.name == "spec_dir":
                try:
                    self._spec_dir = str(parameter.value)
                    self._load_spec(self._spec_dir)
                    self._last_lifecycle_control_signature = None
                except Exception as exc:
                    return SetParametersResult(
                        successful=False,
                        reason=f"failed to reload spec bundle: {exc}",
                    )
        return SetParametersResult(successful=True)

    def _on_world(self, msg: WorldState) -> None:
        self._prior_phase = msg.filtered_phase or self._prior_phase
        self._prior_confidence = float(msg.phase_confidence)

    def _on_evidence(self, msg: PhaseEvidence) -> None:
        result = self._estimator.update(msg, self._prior_phase, self._prior_confidence)
        filtered = FilteredPhase()
        filtered.stamp = self.get_clock().now().to_msg()
        filtered.phase_id = str(result["phase_id"])
        filtered.confidence = float(result["confidence"])
        filtered.uncertain = bool(result["uncertain"])
        filtered.stability = float(result["stability"])
        filtered.allowed_next_phases = list(result["allowed_next_phases"])
        filtered.rationale = str(result["rationale"])
        self._publisher.publish(filtered)

    def _on_control(self, msg: String) -> None:
        command, _, detail = msg.data.strip().partition(":")
        command = command.lower()
        signature = (command, detail.strip())
        if command in {
            "start",
            "start_runtime",
            "start_actors",
            "pause",
            "resume",
            "stop",
        }:
            if signature == getattr(
                self, "_last_lifecycle_control_signature", None
            ):
                return
            self._last_lifecycle_control_signature = signature
        if command == "reset":
            self._last_lifecycle_control_signature = None
            self._load_spec(self._spec_dir)


def main() -> None:
    rclpy.init()
    node = PhaseEstimatorNode()
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
