"""ROS node wrapper for the phase estimator."""

from __future__ import annotations

from pathlib import Path

from procedure_spec import (
    ScenarioConfigSnapshot,
    get_default_spec_dir,
    load_bundle,
    load_scenario_consumer_bundle,
    parse_scenario_config,
)
import rclpy
from rcl_interfaces.msg import SetParametersResult
from rclpy.node import Node
from rclpy.parameter import Parameter
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import String
from surgical_msgs.msg import (
    FilteredPhase,
    PhaseEvidence,
    SimulationState,
    WorldState,
)

from .estimator import PhaseEstimator


class PhaseEstimatorNode(Node):
    def __init__(self) -> None:
        super().__init__("phase_estimator")
        self.declare_parameter("spec_dir", str(get_default_spec_dir()))
        self.declare_parameter(
            "scenario_config_topic", "/simulation/scenario_config"
        )
        self._spec_dir = str(self.get_parameter("spec_dir").value)
        # ScenarioStore is the configuration owner.  A consumer's root stays
        # anchored to its launch-time bundle so a topic publisher cannot make
        # this observer load arbitrary local files later.
        self._scenario_config_root = Path(self._spec_dir).resolve().parent
        self._scenario_config_topic = str(
            self.get_parameter("scenario_config_topic").value
        ).strip()
        if not self._scenario_config_topic:
            raise ValueError("scenario_config_topic must not be empty")
        self._scenario_config_revision = ""
        self._pending_scenario_config: ScenarioConfigSnapshot | None = None
        self._scenario_state_received = False
        self._scenario_running = False
        self._scenario_execution_state = ""
        # A focused owner restart begins quiescent.  The retained ScenarioStore
        # snapshot can therefore rehydrate this local observer before the
        # first continuously-published SimulationState arrives.
        self._scenario_initial_idle = True
        self._load_spec(self._spec_dir)
        self._last_lifecycle_control_signature: tuple[str, str] | None = None
        self.add_on_set_parameters_callback(self._on_parameters_changed)
        self._publisher = self.create_publisher(FilteredPhase, "/phase/filtered", 20)
        self.create_subscription(PhaseEvidence, "/vlm/phase_evidence", self._on_evidence, 20)
        self.create_subscription(WorldState, "/twin/world_state", self._on_world, 20)
        self.create_subscription(
            SimulationState,
            "/simulation/state",
            self._on_simulation_state,
            20,
        )
        self.create_subscription(String, "/simulation/control_state", self._on_control, 20)
        self.create_subscription(
            String,
            self._scenario_config_topic,
            self._on_scenario_config,
            QoSProfile(
                depth=1,
                reliability=ReliabilityPolicy.RELIABLE,
                durability=DurabilityPolicy.TRANSIENT_LOCAL,
            ),
        )

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

    def _scenario_config_candidate(self, snapshot: ScenarioConfigSnapshot) -> str:
        """Return one valid direct child of this node's fixed spec root.

        ScenarioStore owns selection, not filesystem authority.  The local
        consumer repeats only containment, bundle identity, and normal bundle
        parsing before it replaces its last known-good estimator.
        """

        return load_scenario_consumer_bundle(
            snapshot,
            fixed_spec_root=self._scenario_config_root,
        ).spec_dir

    def _scenario_config_apply_is_safe(self) -> bool:
        """Keep local estimator replacement outside active scenario execution."""

        if not bool(getattr(self, "_scenario_state_received", False)):
            return bool(getattr(self, "_scenario_initial_idle", False))
        state = str(
            getattr(self, "_scenario_execution_state", "") or ""
        ).strip().casefold()
        if state == "paused":
            return True
        return (
            not bool(getattr(self, "_scenario_running", False))
            and state
            in {"idle", "stopped", "halted", "completed", "terminated", "error", "failed"}
        )

    def _on_scenario_config(self, message: String) -> None:
        """Stage one retained ScenarioStore revision; never select it here."""

        try:
            snapshot = parse_scenario_config(message.data)
            # Validate before occupying the one pending slot.  The candidate
            # is loaded again immediately before the local atomic swap so a
            # concurrent authored save cannot replace the last good spec.
            self._scenario_config_candidate(snapshot)
        except Exception as exc:
            self.get_logger().warning(f"phase estimator scenario config ignored: {exc}")
            return
        if (
            snapshot.revision == getattr(self, "_scenario_config_revision", "")
            and str(Path(snapshot.spec_dir).resolve())
            == str(Path(getattr(self, "_spec_dir", "")).resolve())
        ):
            return
        self._pending_scenario_config = snapshot
        self._apply_pending_scenario_config_if_safe()

    def _apply_pending_scenario_config_if_safe(self) -> None:
        """Commit a staged revision only at this owner's quiescent boundary."""

        snapshot = getattr(self, "_pending_scenario_config", None)
        if snapshot is None or not self._scenario_config_apply_is_safe():
            return
        try:
            candidate = self._scenario_config_candidate(snapshot)
        except Exception as exc:
            if getattr(self, "_pending_scenario_config", None) == snapshot:
                self._pending_scenario_config = None
            self.get_logger().warning(
                f"phase estimator scenario config rejected before swap: {exc}"
            )
            return

        result = self.set_parameters_atomically(
            [Parameter(name="spec_dir", value=candidate)]
        )
        if not bool(getattr(result, "successful", False)):
            self.get_logger().warning(
                "phase estimator scenario config swap rejected: "
                f"{getattr(result, 'reason', '') or 'unknown reason'}"
            )
            return
        if getattr(self, "_pending_scenario_config", None) == snapshot:
            self._scenario_config_revision = snapshot.revision
            self._pending_scenario_config = None
        self.get_logger().info(
            "phase estimator scenario revision applied locally: "
            f"{snapshot.bundle_name}@{snapshot.revision}"
        )

    def _on_simulation_state(self, msg: SimulationState) -> None:
        self._scenario_state_received = True
        self._scenario_running = bool(getattr(msg, "running", False))
        self._scenario_execution_state = str(
            getattr(msg, "execution_state", "") or ""
        ).strip()
        self._apply_pending_scenario_config_if_safe()

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
        if command in {"start", "start_runtime", "start_actors", "resume"}:
            self._scenario_initial_idle = False
        elif command in {"pause", "stop", "reset"}:
            self._scenario_initial_idle = True
        self._apply_pending_scenario_config_if_safe()


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
