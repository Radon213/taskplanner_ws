"""ROS wrapper for the observation-only scenario-bounded belief tracker."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re

from procedure_spec import (
    ScenarioConfigSnapshot,
    get_default_spec_dir,
    load_bundle,
    load_bundle_at_revision,
    parse_scenario_config,
)
from rcl_interfaces.msg import ParameterDescriptor, SetParametersResult
import rclpy
from rclpy.node import Node
from rclpy.executors import ExternalShutdownException
from rclpy.parameter import Parameter
from rclpy.qos import (
    DurabilityPolicy,
    HistoryPolicy,
    QoSProfile,
    ReliabilityPolicy,
)
from std_msgs.msg import Bool, String
from std_srvs.srv import SetBool
from surgical_msgs.msg import (
    SimulationState,
    SkillCommand,
    SkillStatus,
    TwinEvent,
    WorldState,
)
from surgical_perception_msgs.msg import (
    ToolLocationProbability,
    ToolPoseArray,
    TrackedToolBelief,
    TrackedToolBeliefArray,
)

from .core import BeliefTracker, TrackerConfig, semantic_location
from .inventory import inventory_from_procedure_spec
from .parameters import validated_runtime_update


DEFAULT_BUNDLE_SNAPSHOT_ROOT = Path("/tmp/taskplanner-procedure-snapshots")
_BUNDLE_SNAPSHOT_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_BUNDLE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


STRICT_SCHEMA_VERSION = "taskplanner.fixed_inventory_tool_belief.v1"
EXCHANGEABLE_SCHEMA_VERSION = "taskplanner.exchangeable_tool_capacity_belief.v2"
STRICT_TRACKER_REVISION = "fixed_inventory_semantic_v1"
EXCHANGEABLE_TRACKER_REVISION = "exchangeable_capacity_semantic_v2"
ENABLED_SERVICE = "/surgery/perception/tool_beliefs/set_enabled"
ENABLED_TOPIC = "/surgery/perception/tool_beliefs/enabled"
_CONTROLLER_COMPLETION_EVENTS = frozenset(
    {
        "ToolPrepared",
        "ToolHandoverCompleted",
        "ToolRetrievedFromMayo",
        "ToolReturnedToTray",
        "UnusedPrepositionReturned",
    }
)


def enabled_state_qos_profile() -> QoSProfile:
    """Return the latched operational-state contract used by the UI."""

    return QoSProfile(
        history=HistoryPolicy.KEEP_LAST,
        depth=1,
        reliability=ReliabilityPolicy.RELIABLE,
        durability=DurabilityPolicy.TRANSIENT_LOCAL,
    )


def parameter_descriptor(
    description: str,
    *,
    constraints: str = "",
    read_only: bool = False,
) -> ParameterDescriptor:
    """Build discoverable ROS parameter metadata from one contract source."""

    return ParameterDescriptor(
        description=description,
        additional_constraints=constraints,
        read_only=read_only,
    )


@dataclass(slots=True)
class _ViewHealth:
    ready: bool = False
    received_sec: float = -1.0
    zone: str = "unknown"


def validate_tracker_identity_mode(spec, config: TrackerConfig) -> None:
    """Reject strict identity semantics for observer capacity absent from DT."""

    if config.exchangeable_instances:
        return
    initial_counts = spec.get_tool_inventory()
    capacities = spec.get_tool_inventory_capacity()
    expanded = [
        f"{instrument_id}({int(initial_counts.get(instrument_id, 0))}->{int(capacity)})"
        for instrument_id, capacity in capacities.items()
        if int(capacity) > int(initial_counts.get(instrument_id, 0))
    ]
    if expanded:
        raise ValueError(
            "exchangeable_instances=false cannot represent procedure capacity "
            "greater than DT initial_count: " + ", ".join(sorted(expanded))
        )


def build_tracker_candidate(
    spec_dir: str,
    config: TrackerConfig,
):
    """Fully validate a bundle and bounded slot inventory before runtime mutation."""

    resolved_spec_dir = Path(spec_dir).resolve()
    spec = load_bundle(resolved_spec_dir)
    if resolved_spec_dir.name != str(spec.procedure_id).strip():
        raise ValueError(
            "spec_dir bundle identity does not match loaded procedure_id"
        )
    validate_tracker_identity_mode(spec, config)
    inventory = inventory_from_procedure_spec(spec)
    tracker = BeliefTracker(
        inventory,
        config=config,
    )
    digest_payload = {
        "procedure_id": spec.procedure_id,
        # Include both controller-materialized initial counts and tracker-only
        # observational capacity. A capacity-only procedure edit must change
        # tracker_revision even before a dormant slot is observed.
        "inventory_initial_count": spec.get_tool_inventory(),
        "inventory_capacity": spec.get_tool_inventory_capacity(),
        "inventory": [
            {
                "instrument_id": item.instrument_id,
                "instance_id": item.instance_id,
                "display_name": item.display_name,
                "initial_location": item.initial_location,
                "initial_confidence": item.initial_confidence,
                "initial_activity_probability": item.initial_activity_probability,
                "exchangeable_population": item.exchangeable_population,
            }
            for item in inventory
        ],
    }
    digest = hashlib.sha256(
        json.dumps(
            digest_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    source_files = sorted(resolved_spec_dir.rglob("*.yaml"))
    shared_catalog = resolved_spec_dir.parent / "display_catalog.yaml"
    if shared_catalog.is_file():
        source_files.insert(0, shared_catalog)
    for source_file in source_files:
        digest.update(str(source_file.relative_to(resolved_spec_dir.parent)).encode("utf-8"))
        digest.update(source_file.read_bytes())
    return spec, tracker, f"sha256:{digest.hexdigest()}"


def resolve_bundle_spec_dir(spec_root: Path, bundle_id: str) -> Path:
    """Resolve one simple bundle id beneath a restart-fixed spec root."""

    bundle = str(bundle_id or "").strip()
    if (
        not _BUNDLE_NAME_PATTERN.fullmatch(bundle)
        or ".." in bundle
    ):
        raise ValueError("active bundle must be a simple identifier")
    root = Path(spec_root).resolve()
    candidate = (root / bundle).resolve()
    if candidate.parent != root:
        raise ValueError("active bundle escapes spec_root")
    return candidate


def validate_spec_dir_under_root(
    spec_root: Path,
    spec_dir: str,
    bundle_snapshot_root: Path | None = None,
) -> Path:
    """Accept an authored bundle or one manager-owned immutable snapshot."""

    root = Path(spec_root).resolve()
    candidate = Path(spec_dir).resolve()
    if candidate.parent == root:
        return candidate

    if bundle_snapshot_root is not None:
        snapshot_root = Path(bundle_snapshot_root).resolve()
        try:
            relative = candidate.relative_to(snapshot_root)
        except ValueError:
            relative = None
        if relative is not None:
            parts = relative.parts
            if (
                len(parts) == 4
                and parts[0] == parts[3]
                and _BUNDLE_NAME_PATTERN.fullmatch(parts[0])
                and ".." not in parts[0]
                and _BUNDLE_SNAPSHOT_DIGEST_PATTERN.fullmatch(parts[1])
                and parts[2] == "specs"
            ):
                return candidate

    raise ValueError(
        "spec_dir must name one bundle directly beneath restart-fixed spec_root "
        "or a canonical manager-owned bundle snapshot"
    )


def parse_view_health(payload: dict, *, fallback_zone: str, view: str) -> tuple[bool, str]:
    """Return semantic readiness and zone without letting ready override false."""

    status = str(payload.get("status", "")).strip().casefold()
    semantic_ready = payload.get("semantic_ready")
    ready = bool(payload.get("ready", False)) or status == "ready"
    if semantic_ready is False:
        ready = False
    raw_zone = str(
        payload.get("tf_workspace_zone")
        or payload.get("workspace_zone")
        or fallback_zone
    )
    zone = semantic_location(raw_zone)
    if zone == "unknown":
        zone = "tray" if view == "cam_3" else "mayo"
    return ready, zone


def build_rehydrated_tracker(spec, config: TrackerConfig, instrument_states):
    """Build a fresh bounded tracker and reconcile one complete DT snapshot."""

    tracker = BeliefTracker(inventory_from_procedure_spec(spec), config=config)
    fixed_ids = set(tracker.instance_ids)
    rows: list[tuple[str, str, str, float]] = []
    for state in instrument_states:
        raw_instrument = str(getattr(state, "instrument_id", "") or "")
        instrument_id = spec.resolve_instrument_alias(raw_instrument) or raw_instrument
        raw_instance = str(getattr(state, "instance_id", "") or "")
        instance_id = raw_instance
        match = re.search(r"#(\d+)$", raw_instance)
        if instance_id not in fixed_ids and match and instrument_id:
            instance_id = f"{instrument_id}#{int(match.group(1))}"
        if instance_id not in fixed_ids:
            continue
        location = semantic_location(
            str(getattr(state, "location_id", "") or ""),
            str(getattr(state, "location_type", "") or ""),
        )
        rows.append(
            (
                instance_id,
                instrument_id,
                location,
                float(getattr(state, "confidence", 0.0)),
            )
        )
    return tracker, tracker.reconcile_fixed_states(rows)


class ToolBeliefTrackerNode(Node):
    """Fuse typed camera and action evidence without control authority."""

    def __init__(self) -> None:
        super().__init__("tool_belief_tracker")

        topology_descriptor = parameter_descriptor(
            "Subscription/publisher topology is fixed for this node process.",
            constraints="restart required",
            read_only=True,
        )
        self.declare_parameter(
            "spec_dir",
            str(get_default_spec_dir()),
            parameter_descriptor(
                "Active procedure bundle directory.",
                constraints=(
                    "one direct child of spec_root or a canonical manager-owned "
                    "bundle snapshot; reload requires a fresh stopped SimulationState"
                ),
            ),
        )
        self.declare_parameter(
            "scenario_config_topic",
            "/simulation/scenario_config",
            topology_descriptor,
        )
        self.declare_parameter(
            "spec_root",
            "",
            parameter_descriptor(
                "Restart-fixed root used for active_bundle self-heal.",
                constraints="restart required",
                read_only=True,
            ),
        )
        self.declare_parameter(
            "bundle_snapshot_root",
            str(DEFAULT_BUNDLE_SNAPSHOT_ROOT),
            parameter_descriptor(
                "Restart-fixed root for SimulationManager immutable bundle snapshots.",
                constraints="restart required",
                read_only=True,
            ),
        )
        self.declare_parameter(
            "enabled",
            True,
            parameter_descriptor(
                "Runtime evidence fusion and belief publication switch.",
                constraints="boolean; runtime mutable",
            ),
        )
        self.declare_parameter(
            "cam3_pose_topic",
            "/perception/cam_3/tool/poses",
            topology_descriptor,
        )
        self.declare_parameter(
            "cam4_pose_topic",
            "/perception/cam_4/tool/poses",
            topology_descriptor,
        )
        self.declare_parameter(
            "cam3_health_topic",
            "/perception/cam_3/tool/health",
            topology_descriptor,
        )
        self.declare_parameter(
            "cam4_health_topic",
            "/perception/cam_4/tool/health",
            topology_descriptor,
        )
        self.declare_parameter(
            "output_topic",
            "/surgery/perception/tool_beliefs",
            topology_descriptor,
        )
        self.declare_parameter(
            "skill_command_topic",
            "/bt/skill_command",
            topology_descriptor,
        )
        self.declare_parameter(
            "skill_status_topic",
            "/skill/status",
            topology_descriptor,
        )
        self.declare_parameter(
            "skill_event_topic",
            "/skill/events",
            topology_descriptor,
        )
        self.declare_parameter(
            "simulation_state_topic",
            "/simulation/state",
            topology_descriptor,
        )
        self.declare_parameter(
            "world_state_topic",
            "/twin/world_state",
            topology_descriptor,
        )
        self.declare_parameter(
            "publish_period_sec",
            0.2,
            parameter_descriptor(
                "Belief snapshot publication period in seconds.",
                constraints="finite numeric value in [0.02, 10.0]",
            ),
        )
        # PNU health is nominally 1 Hz; 1.5 s avoids a false stale edge on the
        # measured ~1.004 s maximum interval while still bounding misses.
        self.declare_parameter(
            "health_freshness_sec",
            1.5,
            parameter_descriptor(
                "Maximum local receipt age for camera semantic health.",
                constraints="finite numeric value in [0.05, 30.0]",
            ),
        )
        defaults = TrackerConfig()
        self.declare_parameter(
            "exchangeable_instances",
            True,
            parameter_descriptor(
                "Treat scenario population IDs as exchangeable logical capacity slots.",
                constraints="boolean; runtime mutable",
            ),
        )
        self.declare_parameter(
            "mayo_appearance_assumes_surgeon_return",
            bool(defaults.mayo_appearance_assumes_surgeon_return),
            parameter_descriptor(
                "Assign an uncommanded same-type CAM4 Mayo appearance to a surgeon-owned logical slot before dormant capacity.",
                constraints="boolean; runtime mutable; observation only",
            ),
        )
        tracker_parameter_constraints = {
            "evidence_window_sec": "finite and > 0",
            "max_camera_evidence_dt_sec": "finite and >= evidence_window_sec",
            "miss_half_life_sec": "finite and > 0",
            "unknown_slot_retire_sec": "finite and > 0",
            "robot_motion_positive_scale": "finite and in [0, 1]",
            "robot_motion_negative_scale": "finite and in [0, 1]",
            "robot_motion_grace_sec": "finite and > 0",
            "mayo_hand_positive_scale": "finite and in [0, 1]",
            "mayo_hand_negative_scale": "finite and in [0, 1]",
            "mayo_hand_grace_sec": "finite and > 0",
            "positive_gain": "finite and > 0",
            "confirm_threshold": "finite and in [0, 1]",
            "commit_dwell_sec": "finite and > 0",
            "probable_threshold": (
                "finite and in [0, 1]; must not exceed confirm_threshold"
            ),
            "commit_release_threshold": "finite and in [0, 1]",
            "commit_switch_margin": "finite and in [0, 1]",
            "identity_assignment_margin": "finite and in [0, 1]",
            "uv_memory_sec": "finite and > 0",
            "uv_match_radius_px": "finite and > 0",
            "uv_ambiguity_margin_px": (
                "finite and > 0; must not exceed uv_match_radius_px"
            ),
            "uv_relabel_scale": "finite and in [0, 1]",
            "uv_ambiguous_evidence_scale": "finite and in [0, 1]",
        }
        for name in (
            "evidence_window_sec",
            "max_camera_evidence_dt_sec",
            "miss_half_life_sec",
            "unknown_slot_retire_sec",
            "robot_motion_positive_scale",
            "robot_motion_negative_scale",
            "robot_motion_grace_sec",
            "mayo_hand_positive_scale",
            "mayo_hand_negative_scale",
            "mayo_hand_grace_sec",
            "positive_gain",
            "confirm_threshold",
            "commit_dwell_sec",
            "probable_threshold",
            "commit_release_threshold",
            "commit_switch_margin",
            "identity_assignment_margin",
            "uv_memory_sec",
            "uv_match_radius_px",
            "uv_ambiguity_margin_px",
            "uv_relabel_scale",
            "uv_ambiguous_evidence_scale",
        ):
            self.declare_parameter(
                name,
                float(getattr(defaults, name)),
                parameter_descriptor(
                    "Runtime-tunable scenario-bounded inference parameter.",
                    constraints=tracker_parameter_constraints[name],
                ),
            )

        self._spec_dir = str(self.get_parameter("spec_dir").value)
        configured_spec_root = str(self.get_parameter("spec_root").value).strip()
        self._spec_root = Path(
            configured_spec_root or Path(self._spec_dir).resolve().parent
        ).resolve()
        self._bundle_snapshot_root = Path(
            str(self.get_parameter("bundle_snapshot_root").value)
        ).resolve()
        initial_spec_dir = validate_spec_dir_under_root(
            self._spec_root,
            self._spec_dir,
            self._bundle_snapshot_root,
        )
        self._spec, self._tracker, self._spec_inventory_digest = build_tracker_candidate(
            str(initial_spec_dir),
            self._read_tracker_config(),
        )
        self._spec_dir = str(initial_spec_dir)
        self._bundle_id = initial_spec_dir.name
        self._publish_period_sec = self._positive_parameter("publish_period_sec")
        self._health_freshness_sec = self._positive_parameter("health_freshness_sec")
        self._enabled = bool(self.get_parameter("enabled").value)
        self._procedure_run_id = ""
        self._hydrated_run_id: str | None = None
        self._awaiting_rehydrate = True
        self._scenario_active = False
        self._scenario_running = False
        self._scenario_state_received = False
        self._scenario_execution_state = ""
        self._scenario_config_revision = ""
        self._scenario_config_bundle = ""
        self._pending_scenario_config: ScenarioConfigSnapshot | None = None
        self._state_sync_ready = False
        self._locked_inventory_run_id = ""
        self._allow_active_spec_reconcile = False
        self._health = {
            "cam_3": _ViewHealth(zone="tray"),
            "cam_4": _ViewHealth(zone="mayo"),
        }

        output_topic = str(self.get_parameter("output_topic").value)
        self._publisher = self.create_publisher(
            TrackedToolBeliefArray,
            output_topic,
            10,
        )
        self._enabled_publisher = self.create_publisher(
            Bool,
            ENABLED_TOPIC,
            enabled_state_qos_profile(),
        )
        self._enabled_service = self.create_service(
            SetBool,
            ENABLED_SERVICE,
            self._handle_set_enabled,
        )
        sensor_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=5,
            reliability=ReliabilityPolicy.BEST_EFFORT,
        )
        self.create_subscription(
            ToolPoseArray,
            str(self.get_parameter("cam3_pose_topic").value),
            lambda message: self._on_pose("cam_3", message),
            sensor_qos,
        )
        self.create_subscription(
            ToolPoseArray,
            str(self.get_parameter("cam4_pose_topic").value),
            lambda message: self._on_pose("cam_4", message),
            sensor_qos,
        )
        self.create_subscription(
            String,
            str(self.get_parameter("cam3_health_topic").value),
            lambda message: self._on_health("cam_3", message),
            10,
        )
        self.create_subscription(
            String,
            str(self.get_parameter("cam4_health_topic").value),
            lambda message: self._on_health("cam_4", message),
            10,
        )
        self.create_subscription(
            SkillCommand,
            str(self.get_parameter("skill_command_topic").value),
            self._on_skill_command,
            50,
        )
        self.create_subscription(
            SkillStatus,
            str(self.get_parameter("skill_status_topic").value),
            self._on_skill_status,
            50,
        )
        self.create_subscription(
            TwinEvent,
            str(self.get_parameter("skill_event_topic").value),
            self._on_skill_event,
            50,
        )
        self.create_subscription(
            SimulationState,
            str(self.get_parameter("simulation_state_topic").value),
            self._on_simulation_state,
            20,
        )
        self.create_subscription(
            WorldState,
            str(self.get_parameter("world_state_topic").value),
            self._on_world_state,
            20,
        )
        # A focused tracker restart receives the ScenarioStore's current
        # bundle immediately.  This replaces manager-owned parameter fanout
        # and leaves the tracker free to retain its last good state if a new
        # authored YAML revision is malformed.
        self.create_subscription(
            String,
            str(self.get_parameter("scenario_config_topic").value),
            self._on_scenario_config,
            enabled_state_qos_profile(),
        )
        self._timer = self.create_timer(self._publish_period_sec, self._publish_snapshot)
        self.add_on_set_parameters_callback(self._on_parameters_changed)
        self._publish_enabled_state()
        identity_mode = (
            "exchangeable_logical_capacity"
            if self._tracker.config.exchangeable_instances
            else "strict_authored_instance"
        )
        self.get_logger().info(
            f"scenario-bounded tool belief tracker ready: "
            f"procedure={self._spec.procedure_id} instances={len(self._tracker.instance_ids)} "
            f"identity_mode={identity_mode} output={output_topic} "
            f"observation_only=true enabled={str(self._enabled).lower()}"
        )

    def _now_sec(self) -> float:
        return self.get_clock().now().nanoseconds / 1_000_000_000.0

    def _positive_parameter(self, name: str) -> float:
        value = float(self.get_parameter(name).value)
        if not math.isfinite(value) or value <= 0.0:
            raise ValueError(f"{name} must be finite and positive")
        return value

    def _publish_enabled_state(self) -> None:
        message = Bool()
        message.data = bool(self._enabled)
        self._enabled_publisher.publish(message)

    def _handle_set_enabled(self, request, response):
        requested = bool(request.data)
        result = self.set_parameters_atomically(
            [Parameter(name="enabled", value=requested)]
        )
        response.success = bool(getattr(result, "successful", False))
        if response.success:
            response.message = (
                "tool belief tracker enabled; awaiting a complete SimulationState"
                if requested
                else "tool belief tracker disabled"
            )
        else:
            response.message = str(
                getattr(result, "reason", "enabled update rejected")
                or "enabled update rejected"
            )
        return response

    def _read_tracker_config(self) -> TrackerConfig:
        return TrackerConfig(
            evidence_window_sec=float(self.get_parameter("evidence_window_sec").value),
            max_camera_evidence_dt_sec=float(
                self.get_parameter("max_camera_evidence_dt_sec").value
            ),
            miss_half_life_sec=float(self.get_parameter("miss_half_life_sec").value),
            unknown_slot_retire_sec=float(
                self.get_parameter("unknown_slot_retire_sec").value
            ),
            robot_motion_positive_scale=float(
                self.get_parameter("robot_motion_positive_scale").value
            ),
            robot_motion_negative_scale=float(
                self.get_parameter("robot_motion_negative_scale").value
            ),
            robot_motion_grace_sec=float(self.get_parameter("robot_motion_grace_sec").value),
            mayo_hand_positive_scale=float(
                self.get_parameter("mayo_hand_positive_scale").value
            ),
            mayo_hand_negative_scale=float(
                self.get_parameter("mayo_hand_negative_scale").value
            ),
            mayo_hand_grace_sec=float(
                self.get_parameter("mayo_hand_grace_sec").value
            ),
            positive_gain=float(self.get_parameter("positive_gain").value),
            confirm_threshold=float(self.get_parameter("confirm_threshold").value),
            commit_dwell_sec=float(self.get_parameter("commit_dwell_sec").value),
            probable_threshold=float(self.get_parameter("probable_threshold").value),
            commit_release_threshold=float(
                self.get_parameter("commit_release_threshold").value
            ),
            commit_switch_margin=float(self.get_parameter("commit_switch_margin").value),
            identity_assignment_margin=float(
                self.get_parameter("identity_assignment_margin").value
            ),
            uv_memory_sec=float(self.get_parameter("uv_memory_sec").value),
            uv_match_radius_px=float(
                self.get_parameter("uv_match_radius_px").value
            ),
            uv_ambiguity_margin_px=float(
                self.get_parameter("uv_ambiguity_margin_px").value
            ),
            uv_relabel_scale=float(
                self.get_parameter("uv_relabel_scale").value
            ),
            uv_ambiguous_evidence_scale=float(
                self.get_parameter("uv_ambiguous_evidence_scale").value
            ),
            exchangeable_instances=bool(
                self.get_parameter("exchangeable_instances").value
            ),
            mayo_appearance_assumes_surgeon_return=bool(
                self.get_parameter(
                    "mayo_appearance_assumes_surgeon_return"
                ).value
            ),
        )

    def _on_parameters_changed(self, parameters) -> SetParametersResult:
        updates = {parameter.name: parameter.value for parameter in parameters}
        try:
            next_config, next_period, next_health_freshness = validated_runtime_update(
                self._tracker.config,
                self._publish_period_sec,
                self._health_freshness_sec,
                updates,
            )
        except (TypeError, ValueError) as exc:
            return SetParametersResult(successful=False, reason=str(exc))
        next_enabled = bool(updates.get("enabled", self._enabled))
        identity_mode_changed = (
            next_config.exchangeable_instances
            != self._tracker.config.exchangeable_instances
        )

        candidate_spec = None
        candidate_tracker = None
        candidate_digest = None
        candidate_spec_dir = None
        if "spec_dir" in updates:
            if (
                not self._allow_active_spec_reconcile
                and (
                    not self._scenario_state_received
                    or self._scenario_active
                )
            ):
                return SetParametersResult(
                    successful=False,
                    reason=(
                        "spec_dir reload requires a fresh stopped "
                        "SimulationState"
                    ),
                )
            try:
                candidate_spec_dir = str(
                    validate_spec_dir_under_root(
                        self._spec_root,
                        str(updates["spec_dir"]).strip(),
                        self._bundle_snapshot_root,
                    )
                )
                (
                    candidate_spec,
                    candidate_tracker,
                    candidate_digest,
                ) = build_tracker_candidate(
                    str(candidate_spec_dir),
                    next_config,
                )
            except Exception as exc:
                return SetParametersResult(
                    successful=False,
                    reason=f"spec reload rejected before swap: {exc}",
                )

        if identity_mode_changed and candidate_spec is None:
            try:
                validate_tracker_identity_mode(self._spec, next_config)
            except ValueError as exc:
                return SetParametersResult(successful=False, reason=str(exc))

        reenable_tracker = None
        if not self._enabled and next_enabled and candidate_tracker is None:
            try:
                reenable_tracker = BeliefTracker(
                    inventory_from_procedure_spec(self._spec),
                    config=next_config,
                )
            except Exception as exc:
                return SetParametersResult(
                    successful=False,
                    reason=f"enabled update rejected before reset: {exc}",
                )

        identity_mode_tracker = None
        if (
            identity_mode_changed
            and candidate_tracker is None
            and reenable_tracker is None
        ):
            try:
                identity_mode_tracker = BeliefTracker(
                    inventory_from_procedure_spec(self._spec),
                    config=next_config,
                )
            except Exception as exc:
                return SetParametersResult(
                    successful=False,
                    reason=f"identity mode update rejected before reset: {exc}",
                )

        replacement_timer = None
        if not math.isclose(next_period, self._publish_period_sec):
            try:
                replacement_timer = self.create_timer(next_period, self._publish_snapshot)
            except Exception as exc:  # pragma: no cover - rclpy allocation failure
                return SetParametersResult(
                    successful=False,
                    reason=f"failed to replace publish timer: {exc}",
                )
        if candidate_spec is not None and candidate_tracker is not None:
            self._spec = candidate_spec
            self._tracker = candidate_tracker
            self._spec_dir = str(candidate_spec_dir)
            self._bundle_id = Path(candidate_spec_dir).name
            self._spec_inventory_digest = str(candidate_digest)
            self._procedure_run_id = ""
            self._hydrated_run_id = None
            self._awaiting_rehydrate = True
            self._state_sync_ready = False
        elif reenable_tracker is not None:
            # Re-enabling never resumes a stale pre-disable probability state.
            # The next complete, matching Digital Twin frame is the only
            # authority allowed to make publication ready again.
            self._tracker = reenable_tracker
            self._procedure_run_id = ""
            self._hydrated_run_id = None
            self._awaiting_rehydrate = True
            self._state_sync_ready = False
        elif identity_mode_tracker is not None:
            # Identity semantics cannot be changed in-place: strict and
            # exchangeable modes interpret slot activity differently. Rebuild
            # from the current scenario capacity and wait for a fresh DT frame
            # instead of republishing stale marginals under a new schema.
            self._tracker = identity_mode_tracker
            self._procedure_run_id = ""
            self._hydrated_run_id = None
            self._awaiting_rehydrate = True
            self._state_sync_ready = False
        else:
            self._tracker.update_config(next_config)
        if (
            candidate_tracker is not None
            or reenable_tracker is not None
            or identity_mode_tracker is not None
        ):
            self._locked_inventory_run_id = ""
        self._health_freshness_sec = next_health_freshness
        if replacement_timer is not None:
            previous_timer = self._timer
            self._timer = replacement_timer
            self._publish_period_sec = next_period
            previous_timer.cancel()
            self.destroy_timer(previous_timer)
        enabled_changed = self._enabled != next_enabled
        self._enabled = next_enabled
        if enabled_changed:
            self._publish_enabled_state()
            self.get_logger().info(
                "tool belief tracker "
                + (
                    "enabled; awaiting complete SimulationState rehydrate"
                    if self._enabled
                    else "disabled; evidence fusion and belief publication paused"
                )
            )
        if identity_mode_changed:
            self.get_logger().info(
                "tool belief identity mode changed atomically: "
                + (
                    "exchangeable_logical_capacity"
                    if next_config.exchangeable_instances
                    else "strict_authored_instance"
                )
            )
        if candidate_spec is not None:
            self.get_logger().info(
                "scenario-bounded inventory reloaded atomically: "
                f"procedure={candidate_spec.procedure_id} "
                f"instances={len(self._tracker.instance_ids)}"
            )
        return SetParametersResult(successful=True)

    def _tracker_revision(self) -> str:
        config = self._tracker.config
        payload = {
            name: getattr(config, name)
            for name in config.__dataclass_fields__
        }
        payload["publish_period_sec"] = self._publish_period_sec
        payload["health_freshness_sec"] = self._health_freshness_sec
        payload["procedure_id"] = self._spec.procedure_id
        payload["instance_ids"] = self._tracker.instance_ids
        payload["spec_inventory_digest"] = self._spec_inventory_digest
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()[:12]
        revision = (
            EXCHANGEABLE_TRACKER_REVISION
            if config.exchangeable_instances
            else STRICT_TRACKER_REVISION
        )
        return f"{revision}:{digest}"

    def _on_health(self, view: str, message: String) -> None:
        try:
            payload = json.loads(message.data)
        except (json.JSONDecodeError, TypeError):
            self._health[view] = _ViewHealth(
                ready=False,
                received_sec=self._now_sec(),
                zone=self._health[view].zone,
            )
            return
        if not isinstance(payload, dict):
            return
        ready, zone = parse_view_health(
            payload,
            fallback_zone=self._health[view].zone,
            view=view,
        )
        self._health[view] = _ViewHealth(
            ready=ready,
            received_sec=self._now_sec(),
            zone=zone,
        )

    def _resolve_instrument(self, class_name: str) -> str:
        raw = str(class_name or "").strip()
        direct = self._spec.resolve_instrument_alias(raw)
        if direct:
            return direct
        generic = {
            "forceps",
            "clamp",
            "cautery",
            "surgical",
            "instrument",
            "tool",
        }
        for token in re.findall(r"[0-9a-z가-힣]+", raw.casefold()):
            if token in generic:
                continue
            resolved = self._spec.resolve_instrument_alias(token)
            if resolved:
                return resolved
        return ""

    def _canonical_instance(self, raw_instance: str, instrument_id: str) -> str:
        value = str(raw_instance or "").strip()
        if value in self._tracker.instance_ids:
            return value
        match = re.search(r"#(\d+)$", value)
        if match and instrument_id:
            candidate = f"{instrument_id}#{int(match.group(1))}"
            if candidate in self._tracker.instance_ids:
                return candidate
        return ""

    def _freeze_operator_verified_run_prior(self, run_id: str) -> bool:
        """Freeze the belief the operator reviewed immediately before Start.

        No post-start detector capture is used.  This preserves the reviewed
        tool types, counts, and locations, and makes camera input evidence-only
        for the rest of the run.
        """

        if not run_id or run_id == self._locked_inventory_run_id:
            return False
        if not self._state_sync_ready:
            return False
        try:
            self._tracker = self._tracker.freeze_run_prior()
        except ValueError as exc:
            self.get_logger().warning(
                "tool belief could not freeze operator-verified run prior: "
                f"{exc}"
            )
            return False
        self._procedure_run_id = run_id
        self._hydrated_run_id = run_id
        self._awaiting_rehydrate = False
        self._locked_inventory_run_id = run_id
        self.get_logger().info(
            "tool belief operator-verified run prior frozen: "
            f"procedure_run_id={run_id} instances={len(self._tracker.instance_ids)}"
        )
        return True

    def _on_pose(self, view: str, message: ToolPoseArray) -> None:
        if not self._enabled:
            return
        now = self._now_sec()
        health = self._health[view]
        health_valid = (
            health.ready
            and health.received_sec >= 0.0
            and 0.0 <= now - health.received_sec <= self._health_freshness_sec
        )
        source_view = str(message.source_view or "").strip()
        if source_view and source_view != view:
            return
        detections: list[
            tuple[str, float] | tuple[str, float, float, float]
        ] = []
        fixed_instrument_ids = set(self._tracker.instrument_ids)
        for tool in message.tools:
            instrument_id = self._resolve_instrument(tool.class_name)
            if (
                not instrument_id
                or instrument_id not in fixed_instrument_ids
            ):
                self._tracker.note_ignored_class(tool.class_name)
                continue
            detection: tuple[str, float] | tuple[str, float, float, float] = (
                instrument_id,
                float(tool.class_confidence),
            )
            raw_uv = getattr(tool, "observation_point_uv_px", ())
            if (
                bool(getattr(tool, "observation_point_inside_mask", False))
                and isinstance(raw_uv, Sequence)
                and len(raw_uv) >= 2
            ):
                try:
                    u_value = float(raw_uv[0])
                    v_value = float(raw_uv[1])
                except (TypeError, ValueError):
                    pass
                else:
                    if math.isfinite(u_value) and math.isfinite(v_value):
                        detection = (
                            instrument_id,
                            float(tool.class_confidence),
                            u_value,
                            v_value,
                        )
            detections.append(detection)
        self._tracker.apply_camera_frame(
            view=view,
            zone=health.zone,
            detections=detections,
            # Use receipt time for rate/age math. The provider stamp remains
            # message provenance and may come from another host clock.
            timestamp_sec=now,
            health_valid=health_valid,
            model_version=message.model_version,
            ontology_version=message.ontology_version,
            calibration_version=message.calibration_version,
        )

    def _on_skill_command(self, message: SkillCommand) -> None:
        if not self._enabled:
            return
        active_run_id = str(self._procedure_run_id or "").strip()
        if not active_run_id or str(
            getattr(message, "procedure_run_id", "")
        ).strip() != active_run_id:
            return
        instrument_id = self._resolve_instrument(message.instrument_id) or str(
            message.instrument_id or ""
        )
        instance_id = self._canonical_instance(
            message.instrument_instance_id,
            instrument_id,
        )
        self._tracker.register_command(
            message.command_id,
            instrument_id,
            instance_id,
            message.source_location_id or message.source_location_type,
            message.target_location_id or message.target_location_type,
            self._now_sec(),
        )

    def _on_skill_status(self, message: SkillStatus) -> None:
        if not self._enabled:
            return
        active_run_id = str(self._procedure_run_id or "").strip()
        if not active_run_id or str(
            getattr(message, "procedure_run_id", "")
        ).strip() != active_run_id:
            return
        self._tracker.apply_skill_status(
            message.command_id,
            message.state,
            bool(message.success),
            float(message.progress),
            self._now_sec(),
            message.message,
        )

    def _on_skill_event(self, message: TwinEvent) -> None:
        if not self._enabled:
            return
        active_run_id = str(self._procedure_run_id or "").strip()
        if not active_run_id or str(
            getattr(message, "procedure_run_id", "")
        ).strip() != active_run_id:
            return
        # The execution bridge emits both a terminal SkillStatus and a
        # controller-completion TwinEvent for the same command.  SkillStatus
        # normally carries the correlated source and target through the command
        # ledger, so admitting the TwinEvent here would count one physical
        # response twice and make it unnecessarily hard for persistent camera
        # evidence at the source to reverse the claim.
        #
        # ``ToolReturnedToTray`` is the narrow exception.  During completion
        # cleanup the controller may emit the terminal receipt before the
        # asynchronous SkillStatus ledger has projected its target.  Without
        # this receipt the last in-flight ``robot`` observation can be
        # committed for one snapshot and be interpreted as a second right-hand
        # tool.  Mix the terminal tray evidence instead of hard-setting it;
        # persistent camera evidence can still overturn a failed return.
        try:
            detail = json.loads(message.detail_json or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            detail = {}
        if isinstance(detail, dict):
            authoritative_projection = bool(
                message.event_type in _CONTROLLER_COMPLETION_EVENTS
                and detail.get("authoritative_controller_completion") is True
            )
            correlated_task_completion = bool(
                message.event_type == "RobotTaskCompleted"
                and detail.get("transport") == "ros2_action"
                and str(detail.get("command_id", "")).strip()
            )
            if (
                authoritative_projection
                and message.event_type == "ToolReturnedToTray"
            ):
                instrument_id = self._resolve_instrument(
                    message.instrument_id
                ) or str(message.instrument_id or "")
                instance_id = self._canonical_instance(
                    message.instance_id,
                    instrument_id,
                )
                self._tracker.apply_semantic_event(
                    instrument_id=instrument_id,
                    instance_id=instance_id,
                    location="tray",
                    confidence=max(float(message.confidence), 0.35),
                    event_type=message.event_type,
                    timestamp_sec=self._now_sec(),
                )
                return
            if authoritative_projection or correlated_task_completion:
                return
        instrument_id = self._resolve_instrument(message.instrument_id) or str(
            message.instrument_id or ""
        )
        instance_id = self._canonical_instance(message.instance_id, instrument_id)
        raw_location = (
            message.location_id
            or message.target_location_id
            or message.source_location_id
            or message.location_type
        )
        self._tracker.apply_semantic_event(
            instrument_id=instrument_id,
            instance_id=instance_id,
            location=semantic_location(raw_location),
            confidence=float(message.confidence),
            event_type=message.event_type,
            timestamp_sec=self._now_sec(),
        )

    def _on_world_state(self, message: WorldState) -> None:
        """Feed Mayo hand occupancy as soft camera-occlusion context only."""

        if not self._enabled:
            return
        procedure_id = str(getattr(message, "procedure_id", "") or "").strip()
        if procedure_id and procedure_id != self._spec.procedure_id:
            return
        self._tracker.set_mayo_hand_present(
            bool(getattr(message, "cam4_mayo_hand_present", False)),
            self._now_sec(),
        )

    def _self_heal_active_bundle(self, active_bundle: str) -> bool:
        """Catch up after an optional participant missed a stopped switch."""

        if not active_bundle or active_bundle == self._bundle_id:
            return True
        try:
            candidate = resolve_bundle_spec_dir(self._spec_root, active_bundle)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self.get_logger().error(f"tool belief self-heal rejected: {exc}")
            return False
        self._allow_active_spec_reconcile = True
        try:
            result = self.set_parameters_atomically(
                [Parameter(name="spec_dir", value=str(candidate))]
            )
        finally:
            self._allow_active_spec_reconcile = False
        if not bool(getattr(result, "successful", False)):
            self.get_logger().error(
                "tool belief self-heal failed: "
                f"{getattr(result, 'reason', 'unknown reason')}"
            )
            return False
        return self._bundle_id == active_bundle

    def _rehydrate_from_simulation_state(self, message: SimulationState) -> None:
        run_id = str(message.procedure_run_id or "")[:160]
        if not self._awaiting_rehydrate and self._hydrated_run_id == run_id:
            self._procedure_run_id = run_id
            return
        candidate, complete = build_rehydrated_tracker(
            self._spec,
            self._tracker.config,
            message.instrument_states,
        )
        # Swap the new-run baseline even if this frame is incomplete. This
        # prevents a prior run belief from leaking while waiting for a complete
        # scenario-capacity snapshot (initial DT count in exchangeable mode).
        self._tracker = candidate
        self._procedure_run_id = run_id
        if complete:
            self._hydrated_run_id = run_id
            self._awaiting_rehydrate = False
            self._state_sync_ready = True
        else:
            self._hydrated_run_id = None
            self._awaiting_rehydrate = True
            self._state_sync_ready = False

    def _on_simulation_state(self, message: SimulationState) -> None:
        now = self._now_sec()
        robot_state = str(message.robot_state or "").strip().casefold()
        execution_state = str(message.execution_state or "").strip().casefold()
        self._scenario_execution_state = execution_state
        self._scenario_running = bool(message.running)
        self._scenario_active = bool(message.running) or execution_state in {
            "starting",
            "running",
            "paused",
        }
        self._scenario_state_received = True
        self._apply_pending_scenario_config_if_safe()
        active_bundle = str(message.active_bundle or "").strip()
        # ScenarioStore is the configuration owner.  Ignore a stale Twin
        # frame from the preceding bundle instead of self-healing back over a
        # freshly received, latched ScenarioStore revision.
        if (
            active_bundle
            and self._scenario_config_bundle
            and active_bundle != self._scenario_config_bundle
        ):
            return
        if active_bundle and not self._self_heal_active_bundle(active_bundle):
            return
        if (
            str(message.procedure_id or "").strip()
            and str(message.procedure_id).strip() != self._spec.procedure_id
        ):
            return
        if active_bundle and active_bundle != self._bundle_id:
            return

        # Disabled mode still consumes the authoritative scenario identity and
        # stopped/running state above so spec self-heal and reload admission do
        # not go stale. It deliberately does not mutate any belief state.
        if not self._enabled:
            return

        run_id = str(message.procedure_run_id or "")[:160]
        scenario_running = bool(message.running) and execution_state == "running"
        if scenario_running and run_id:
            # The display immediately before Start is operator-reviewed.  Do
            # not throw it away and recapture a new membership/count from a
            # post-start detector window; lock that live belief as this run's
            # immutable population prior instead.
            if self._locked_inventory_run_id != run_id:
                if not self._state_sync_ready:
                    # A node-only restart can have missed the pre-run display.
                    # In that exceptional case a complete state snapshot is a
                    # bootstrap prior, never a detector re-capture.
                    self._rehydrate_from_simulation_state(message)
                if not self._freeze_operator_verified_run_prior(run_id):
                    return
        else:
            self._locked_inventory_run_id = ""
            self._rehydrate_from_simulation_state(message)
        task_id = str(message.active_robot_task_id or "").strip()
        moving_states = {
            "busy",
            "picking",
            "handover_in_progress",
            "recovery_in_progress",
            "cleaning",
            "returning_home",
        }
        active = bool(task_id) or robot_state in moving_states
        self._tracker.set_robot_motion(active, now, task_id)
        if task_id and message.active_robot_task_tool_id:
            instrument_id = self._resolve_instrument(message.active_robot_task_tool_id) or str(
                message.active_robot_task_tool_id
            )
            instance_id = self._canonical_instance(
                message.active_robot_task_tool_instance_id,
                instrument_id,
            )
            self._tracker.register_command(
                task_id,
                instrument_id,
                instance_id,
                message.active_robot_task_source_anchor,
                message.active_robot_task_target_anchor,
                now,
            )

    def _on_scenario_config(self, message: String) -> None:
        """Queue the manager-owned revision without treating it as control I/O."""

        try:
            snapshot = parse_scenario_config(message.data)
            candidate = validate_spec_dir_under_root(
                self._spec_root,
                snapshot.spec_dir,
                self._bundle_snapshot_root,
            )
            if candidate.name != snapshot.bundle_name:
                raise ValueError(
                    "scenario config bundle_name does not match spec_dir"
                )
            load_bundle_at_revision(candidate, snapshot.revision)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self.get_logger().warning(
                f"tool belief scenario config ignored: {exc}",
                throttle_duration_sec=2.0,
            )
            return
        if (
            snapshot.revision == self._scenario_config_revision
            and snapshot.bundle_name == self._scenario_config_bundle
        ):
            return
        self._pending_scenario_config = snapshot
        self._apply_pending_scenario_config_if_safe()

    def _scenario_config_apply_is_safe(self) -> bool:
        """A belief-only refresh may run while the scenario is paused, never running."""

        if not bool(getattr(self, "_scenario_state_received", False)):
            return True
        state = str(
            getattr(self, "_scenario_execution_state", "") or ""
        ).strip().casefold()
        if state == "paused":
            return True
        return (
            not bool(getattr(self, "_scenario_running", False))
            and state in {"idle", "halted", "completed", "terminated"}
        )

    def _apply_pending_scenario_config_if_safe(self) -> None:
        snapshot = getattr(self, "_pending_scenario_config", None)
        if snapshot is None or not self._scenario_config_apply_is_safe():
            return
        try:
            candidate = validate_spec_dir_under_root(
                self._spec_root,
                snapshot.spec_dir,
                self._bundle_snapshot_root,
            )
            if candidate.name != snapshot.bundle_name:
                raise ValueError(
                    "scenario config bundle_name does not match spec_dir"
                )
            load_bundle_at_revision(candidate, snapshot.revision)
        except Exception as exc:
            self.get_logger().error(
                f"tool belief scenario config rejected before swap: {exc}"
            )
            self._pending_scenario_config = None
            return

        self._allow_active_spec_reconcile = True
        try:
            result = self.set_parameters_atomically(
                [Parameter(name="spec_dir", value=str(candidate))]
            )
        finally:
            self._allow_active_spec_reconcile = False
        if not bool(getattr(result, "successful", False)):
            self.get_logger().error(
                "tool belief scenario config swap rejected: "
                f"{getattr(result, 'reason', 'unknown reason')}"
            )
            return
        self._scenario_config_revision = snapshot.revision
        self._scenario_config_bundle = snapshot.bundle_name
        self._pending_scenario_config = None
        self.get_logger().info(
            "tool belief scenario revision applied atomically: "
            f"{snapshot.bundle_name}@{snapshot.revision}"
        )

    def _publish_snapshot(self) -> None:
        # Repeat the tiny operational state heartbeat so late rosbridge clients
        # still converge even when their subscription QoS cannot request the
        # transient-local sample retained by the ROS publisher.
        self._publish_enabled_state()
        if not self._enabled:
            return
        # A node-only warm restart starts with authored initial placement, but
        # that baseline must never be exposed as if it were current state.
        # Publish only after one complete, matching Digital Twin snapshot has
        # rehydrated every initially active slot. Exchangeable capacity beyond
        # the DT initial count remains explicitly inactive/unknown.
        if not self._state_sync_ready:
            return
        now = self._now_sec()
        output = TrackedToolBeliefArray()
        output.header.stamp = self.get_clock().now().to_msg()
        output.header.frame_id = "semantic_operating_room"
        output.schema_version = (
            EXCHANGEABLE_SCHEMA_VERSION
            if self._tracker.config.exchangeable_instances
            else STRICT_SCHEMA_VERSION
        )
        output.procedure_id = self._spec.procedure_id
        output.procedure_run_id = self._procedure_run_id
        output.tracker_revision = self._tracker_revision()
        output.observation_only = True
        output.robot_motion_active = self._tracker.robot_motion_active(now)
        output.robot_motion_negative_scale = float(
            self._tracker.config.robot_motion_negative_scale
        )
        output.ignored_out_of_inventory_count = int(
            self._tracker.ignored_out_of_inventory_count
        )
        output.ignored_class_names = list(self._tracker.ignored_class_names)
        output.ignored_ambiguous_command_count = int(
            self._tracker.ignored_ambiguous_command_count
        )
        for snapshot in self._tracker.snapshot(now):
            belief = TrackedToolBelief()
            belief.track_id = str(snapshot["track_id"])
            belief.instrument_id = str(snapshot["instrument_id"])
            belief.instance_id = str(snapshot["instance_id"])
            belief.display_name = str(snapshot["display_name"])
            belief.existence_probability = float(snapshot["existence_probability"])
            belief.status = str(snapshot["status"])
            belief.committed_location_id = str(snapshot["committed_location_id"])
            belief.committed_location_probability = float(
                snapshot["committed_location_probability"]
            )
            belief.most_likely_location_id = str(snapshot["most_likely_location_id"])
            belief.most_likely_probability = float(snapshot["most_likely_probability"])
            for location_id, probability in snapshot["locations"].items():
                location = ToolLocationProbability()
                location.location_id = str(location_id)
                location.probability = float(probability)
                belief.locations.append(location)
            positive_sec = snapshot["last_positive_sec"]
            if positive_sec is not None and positive_sec >= 0.0:
                belief.last_positive_stamp.sec = int(positive_sec)
                belief.last_positive_stamp.nanosec = int(
                    (positive_sec - int(positive_sec)) * 1_000_000_000
                )
            belief.last_positive_age_sec = float(snapshot["last_positive_age_sec"])
            belief.evidence_sources = list(snapshot["evidence_sources"])
            belief.motion_mode = str(snapshot["motion_mode"])
            belief.active_command_id = str(snapshot["active_command_id"])
            belief.model_version = str(snapshot["model_version"])
            belief.ontology_version = str(snapshot["ontology_version"])
            belief.calibration_version = str(snapshot["calibration_version"])
            belief.status_flags = list(snapshot["status_flags"])
            output.tools.append(belief)
        self._publisher.publish(output)


def main() -> None:
    rclpy.init()
    node = ToolBeliefTrackerNode()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
