"""ROS adapter for the independently restartable ScenarioStore owner.

It keeps the existing ``/simulation/select_bundle`` service and
``/simulation/scenario_config`` topic shape.  The node decides only whether a
full scenario swap is being requested at a paused or stopped Twin boundary;
lifecycle, BT, ODT, execution routing, and endpoint admission remain owned
elsewhere.
"""

from __future__ import annotations

from pathlib import Path
import threading
import time

from ament_index_python.packages import get_package_share_directory
import rclpy
from rclpy.node import Node
from rclpy.qos import (
    DurabilityPolicy,
    HistoryPolicy,
    QoSProfile,
    ReliabilityPolicy,
)
from std_msgs.msg import String
from surgical_msgs.msg import SimulationState
from surgical_msgs.srv import SelectSimulationBundle

from .scenario_store import (
    ScenarioSnapshot,
    ScenarioStore,
    load_selected_bundle_state,
    load_scenario_snapshot,
    persist_selected_bundle_state,
    scenario_change_is_allowed,
    scenario_config_json,
    validate_bundle_name,
)

class ScenarioStoreNode(Node):
    """Own the selected bundle and publish its current read-only revision."""

    def __init__(self) -> None:
        super().__init__("scenario_store")
        default_root = Path(get_package_share_directory("procedure_spec")) / "specs"
        self.declare_parameter("spec_root", str(default_root))
        self.declare_parameter("default_bundle", "thyroidectomy")
        self.declare_parameter("scenario_config_topic", "/simulation/scenario_config")
        self.declare_parameter("simulation_state_topic", "/simulation/state")
        self.declare_parameter("simulation_state_max_age_sec", 3.0)
        self.declare_parameter("select_bundle_service", "/simulation/select_bundle")
        self.declare_parameter("selection_state_path", "")

        self._spec_root = Path(str(self.get_parameter("spec_root").value))
        default_bundle = str(self.get_parameter("default_bundle").value)
        configured_state_path = str(
            self.get_parameter("selection_state_path").value
        ).strip()
        self._selection_state_path = (
            Path(configured_state_path) if configured_state_path else None
        )
        initial_bundle = self._initial_bundle(default_bundle)
        self._store = ScenarioStore(
            load_scenario_snapshot(self._spec_root, initial_bundle)
        )
        self._state_lock = threading.RLock()
        self._latest_state: SimulationState | None = None
        self._latest_state_received_monotonic = 0.0
        self._simulation_state_max_age_sec = max(
            0.1,
            float(self.get_parameter("simulation_state_max_age_sec").value),
        )

        snapshot_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        self._scenario_config_pub = self.create_publisher(
            String,
            str(self.get_parameter("scenario_config_topic").value),
            snapshot_qos,
        )
        self.create_subscription(
            SimulationState,
            str(self.get_parameter("simulation_state_topic").value),
            self._on_simulation_state,
            snapshot_qos,
        )
        self.create_service(
            SelectSimulationBundle,
            str(self.get_parameter("select_bundle_service").value),
            self._handle_select_bundle,
        )
        self._publish_snapshot()

    def _initial_bundle(self, default_bundle: str) -> str:
        """Use the persisted selection when available, otherwise the default.

        A malformed or stale local preference must not prevent the independent
        owner from starting.  It is an operational convenience, not a second
        scenario-validation gate.
        """

        state_path = getattr(self, "_selection_state_path", None)
        if state_path is None:
            return default_bundle
        try:
            persisted = load_selected_bundle_state(state_path)
        except Exception as exc:
            self.get_logger().warning(
                f"ignoring invalid scenario selection state: {exc}"
            )
            return default_bundle
        if not persisted:
            return default_bundle
        try:
            # A valid-looking state file can outlive a renamed/deleted
            # researcher bundle.  Verify only that the local bundle still
            # parses; the actual snapshot is loaded once more below for the
            # committed store.  A stale preference must never stop an owner
            # restart.
            load_scenario_snapshot(self._spec_root, persisted)
        except Exception as exc:
            self.get_logger().warning(
                f"ignoring unavailable persisted scenario selection: {exc}"
            )
            return default_bundle
        return persisted

    def _persist_selection(self) -> None:
        state_path = getattr(self, "_selection_state_path", None)
        if state_path is None:
            return
        try:
            persist_selected_bundle_state(
                state_path,
                self._store.snapshot(),
            )
        except Exception as exc:
            self.get_logger().warning(
                f"scenario selection is active but could not be persisted: {exc}"
            )

    def _on_simulation_state(self, message: SimulationState) -> None:
        with self._state_lock:
            self._latest_state = message
            self._latest_state_received_monotonic = time.monotonic()

    def _publish_snapshot(self) -> None:
        message = String()
        message.data = scenario_config_json(self._store.snapshot())
        self._scenario_config_pub.publish(message)

    def _selection_is_mutable(self) -> tuple[bool, str]:
        """Return whether a full bundle swap has a safe intervention boundary.

        This is intentionally a narrow lifecycle observation, not an endpoint,
        camera, VLM, or controller readiness gate.  Those owners retain their
        own admission rules when a later command actually reaches them.
        """

        with self._state_lock:
            state = self._latest_state
            received_monotonic = self._latest_state_received_monotonic
        if state is None:
            return (
                False,
                "scenario selection is waiting for the current simulation state; "
                "pause or stop the scenario first",
            )
        age_sec = time.monotonic() - received_monotonic
        if age_sec > self._simulation_state_max_age_sec:
            return (
                False,
                "scenario selection is waiting for a fresh simulation state; "
                f"last state is {age_sec:.1f}s old",
            )
        return scenario_change_is_allowed(
            running=getattr(state, "running", False),
            execution_state=getattr(state, "execution_state", ""),
        )

    @staticmethod
    def _set_metadata(
        response,
        *,
        active_revision: str,
        candidate_revision: str,
        changed: bool,
        applied: bool,
        disposition: str,
    ) -> None:
        # The installed generated service may lag the source interface during
        # a local iteration.  Required legacy fields are still set below.
        values = {
            "active_config_revision": active_revision,
            "candidate_config_revision": candidate_revision,
            "changed": bool(changed),
            "applied": bool(applied),
            "disposition": disposition,
        }
        for name, value in values.items():
            try:
                setattr(response, name, value)
            except AttributeError:
                pass

    def _response_from_current(
        self,
        response,
        *,
        success: bool,
        message: str,
        candidate: ScenarioSnapshot | None = None,
        changed: bool = False,
        applied: bool = False,
        disposition: str = "rejected",
    ):
        current = self._store.snapshot()
        response.success = bool(success)
        response.message = str(message)
        response.active_bundle = current.bundle_name
        response.spec_dir = str(candidate.spec_dir if candidate is not None else current.spec_dir)
        self._set_metadata(
            response,
            active_revision=current.revision,
            candidate_revision=(candidate.revision if candidate is not None else current.revision),
            changed=changed,
            applied=applied,
            disposition=disposition,
        )
        return response

    def _handle_select_bundle(self, request, response):
        """Load one candidate, then atomically publish it when mutable.

        ``restart_if_running`` is accepted for service compatibility but never
        causes this configuration owner to interrupt a procedure or restart an
        owner.  Full bundle selections require a paused or stopped lifecycle
        boundary, while resident owners converge from the retained snapshot.
        """

        requested = str(getattr(request, "bundle_name", "") or "").strip()
        try:
            bundle_name = validate_bundle_name(requested)
            candidate = load_scenario_snapshot(self._spec_root, bundle_name)
        except Exception as exc:
            return self._response_from_current(
                response,
                success=False,
                message=str(exc),
                disposition="rejected",
            )

        current = self._store.snapshot()
        changed = (
            candidate.bundle_name != current.bundle_name
            or candidate.revision != current.revision
        )
        if bool(getattr(request, "preview_only", False)):
            return self._response_from_current(
                response,
                success=True,
                message=(
                    f"bundle preview detected configuration changes for {bundle_name}"
                    if changed
                    else f"bundle preview found no configuration changes for {bundle_name}"
                ),
                candidate=candidate,
                changed=changed,
                disposition=("preview_change_available" if changed else "preview_unchanged"),
            )

        expected_revision = str(
            getattr(request, "expected_candidate_revision", "") or ""
        ).strip()
        if expected_revision and expected_revision != candidate.revision:
            return self._response_from_current(
                response,
                success=False,
                message=(
                    "bundle configuration changed since preview; preview the "
                    "candidate revision again before applying it"
                ),
                candidate=candidate,
                changed=changed,
                disposition="revision_changed",
            )
        if not changed:
            return self._response_from_current(
                response,
                success=True,
                message=f"active bundle {bundle_name} has no configuration changes",
                candidate=candidate,
                disposition="unchanged",
            )
        if candidate.bundle_name == current.bundle_name and not bool(
            getattr(request, "reload_if_changed", False)
        ):
            return self._response_from_current(
                response,
                success=True,
                message=(
                    f"active bundle {bundle_name} has configuration changes; "
                    "set reload_if_changed=true to apply them"
                ),
                candidate=candidate,
                changed=True,
                disposition="change_available",
            )

        mutable, state_message = self._selection_is_mutable()
        if not mutable:
            return self._response_from_current(
                response,
                success=False,
                message=state_message,
                candidate=candidate,
                changed=changed,
                disposition="deferred_paused_or_stopped_required",
            )

        self._store.replace(candidate)
        self._persist_selection()
        self._publish_snapshot()
        applied_message = (
            f"active bundle {bundle_name} reloaded via scenario store"
            if candidate.bundle_name == current.bundle_name
            else f"active bundle set to {bundle_name} via scenario store"
        )
        return self._response_from_current(
            response,
            success=True,
            message=applied_message,
            candidate=candidate,
            changed=True,
            applied=True,
            disposition="applied",
        )


def main() -> None:
    rclpy.init()
    node = ScenarioStoreNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


__all__ = ["ScenarioStoreNode", "main"]
