"""Read-only ROS 2 gateway for the public surgical-integration topics.

This node deliberately has no publishers or services on Taskplanner's internal
command paths.  It only observes internal state, projects a reviewed subset,
and publishes the shared topics.  The projection helpers form the information
boundary; keep all policy-sensitive field selection there.
"""

from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Any

import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from surgical_interop_msgs.msg import (
    BedRobotArmStateArray,
    ClinicalObservation,
    ClinicalObservationArray,
    InstrumentState,
    InstrumentStateArray,
    RobotState,
    RobotStateArray,
    SurgeryContext,
    SurgeryEvent,
    SurgeryHealth,
)
from surgical_msgs.msg import (
    InputSourceStatus,
    SkillStatus,
    TwinEvent,
    VLMHealth,
    VLMResult,
    WorldState,
)

from .projections import (
    DT_ACCEPTED,
    MODEL_OBSERVED,
    ClinicalObservationProjection,
    Freshness,
    InstrumentProjection,
    RobotProjection,
    freshness_from_receipt,
    project_clinical_observation,
    project_context,
    project_event,
    project_bed_robot_arm_state,
    project_instruments,
    project_skill_robot_status,
)


GATEWAY_OBSERVED = "GATEWAY_OBSERVED"
_SKILL_FAILURE_STATES = {
    "dispatch_failed",
    "failed",
    "rejected",
    "result_failed",
    "server_unavailable",
}
_BED_ROBOT_PROCEDURE_LAYOUTS = {
    "thyroidectomy": frozenset({"army_navy"}),
    "nephrectomy": frozenset({"left_malleable", "right_malleable"}),
}
_BED_ROBOT_STATES = frozenset(
    {
        "standby",
        "direct_teach",
        "retracting",
        "changing_tool",
        "moving_to_standby",
        "fault",
        "protective_stop",
        "unknown",
    }
)


@dataclass(frozen=True)
class CachedMessage:
    """An inbound message together with the local time at which it arrived."""

    message: Any
    received_monotonic_sec: float
    sequence: int = 0


def _state_qos() -> QoSProfile:
    """Reliable latched state so new institutional consumers get a snapshot."""

    return QoSProfile(
        history=HistoryPolicy.KEEP_LAST,
        depth=1,
        reliability=ReliabilityPolicy.RELIABLE,
        durability=DurabilityPolicy.TRANSIENT_LOCAL,
    )


def _event_qos() -> QoSProfile:
    """Reliable but non-latched events; history is not current state."""

    return QoSProfile(
        history=HistoryPolicy.KEEP_LAST,
        depth=50,
        reliability=ReliabilityPolicy.RELIABLE,
        durability=DurabilityPolicy.VOLATILE,
    )


def _source_qos() -> QoSProfile:
    return QoSProfile(
        history=HistoryPolicy.KEEP_LAST,
        depth=20,
        reliability=ReliabilityPolicy.RELIABLE,
        durability=DurabilityPolicy.VOLATILE,
    )


class SurgicalInteropGateway(Node):
    """Project safe public context without providing any control endpoint."""

    _SOURCE_NAMES = (
        "world_state",
        "speech_input",
        "flir",
        "cam4",
        "vlm_result",
        "vlm_health",
        "skill_status",
        "bed_robot_arm_status",
    )

    def __init__(self) -> None:
        super().__init__("surgical_interop_gateway")

        self._publish_period_sec = max(
            0.1, float(self.declare_parameter("publish_period_sec", 1.0).value)
        )
        self._world_stale_after_sec = max(
            0.1,
            float(self.declare_parameter("world_stale_after_sec", 3.0).value),
        )
        self._vlm_stale_after_sec = max(
            0.1,
            float(self.declare_parameter("vlm_stale_after_sec", 3.0).value),
        )
        self._robot_stale_after_sec = max(
            0.1,
            float(self.declare_parameter("robot_stale_after_sec", 3.0).value),
        )
        self._health_stale_after_sec = max(
            0.1,
            float(self.declare_parameter("health_stale_after_sec", 6.0).value),
        )
        self._required_health_sources = {
            str(value).strip()
            for value in self.declare_parameter(
                "required_health_sources", ["world_state", "speech_input"]
            ).value
            if str(value).strip()
        }
        unknown_required_sources = self._required_health_sources.difference(self._SOURCE_NAMES)
        if unknown_required_sources:
            raise ValueError(
                "required_health_sources contains unknown source(s): "
                + ", ".join(sorted(unknown_required_sources))
            )

        self._lock = threading.RLock()
        self._world: CachedMessage | None = None
        self._vlm_result: CachedMessage | None = None
        self._vlm_health: CachedMessage | None = None
        self._input_statuses: dict[str, CachedMessage] = {}
        self._skill_status: CachedMessage | None = None
        self._bed_robot_arm_status: CachedMessage | None = None
        self._bed_robot_arm_revision: int | None = None
        self._bed_robot_arm_source_stamp_sec: float | None = None
        self._revision = 0
        self._event_sequence = 0
        self._clinical_sequence = 0

        state_qos = _state_qos()
        self._context_pub = self.create_publisher(SurgeryContext, "/surgery/context", state_qos)
        self._instruments_pub = self.create_publisher(
            InstrumentStateArray, "/surgery/instruments", state_qos
        )
        self._robots_pub = self.create_publisher(RobotStateArray, "/surgery/robots", state_qos)
        self._events_pub = self.create_publisher(SurgeryEvent, "/surgery/events", _event_qos())
        self._clinical_pub = self.create_publisher(
            ClinicalObservationArray, "/surgery/clinical_observations", state_qos
        )
        self._health_pub = self.create_publisher(SurgeryHealth, "/surgery/health", state_qos)

        source_qos = _source_qos()
        self.create_subscription(WorldState, "/twin/world_state", self._on_world, source_qos)
        self.create_subscription(TwinEvent, "/twin/events", self._on_event, source_qos)
        self.create_subscription(VLMResult, "/vlm/result", self._on_vlm_result, source_qos)
        self.create_subscription(VLMHealth, "/vlm/health", self._on_vlm_health, source_qos)
        self.create_subscription(
            InputSourceStatus,
            "/input/speech/status",
            lambda message: self._on_input_status("speech_input", message),
            source_qos,
        )
        self.create_subscription(
            InputSourceStatus,
            "/input/flir/status",
            lambda message: self._on_input_status("flir", message),
            source_qos,
        )
        self.create_subscription(
            InputSourceStatus,
            "/input/cam4/status",
            lambda message: self._on_input_status("cam4", message),
            source_qos,
        )
        self.create_subscription(SkillStatus, "/skill/status", self._on_skill_status, source_qos)
        self.create_subscription(
            BedRobotArmStateArray,
            "/external/bed_robot_arms/status",
            self._on_bed_robot_arm_status,
            source_qos,
        )
        self.create_timer(self._publish_period_sec, self._publish_snapshots)
        self.get_logger().info(
            "Public surgical interop gateway started: read-only projection to /surgery/*"
        )

    @staticmethod
    def _monotonic() -> float:
        return time.monotonic()

    def _cache(self, message: Any, *, sequence: int = 0) -> CachedMessage:
        return CachedMessage(
            message=message,
            received_monotonic_sec=self._monotonic(),
            sequence=sequence,
        )

    def _on_world(self, message: WorldState) -> None:
        with self._lock:
            self._world = self._cache(message)

    def _on_vlm_result(self, message: VLMResult) -> None:
        with self._lock:
            self._clinical_sequence += 1
            self._vlm_result = self._cache(message, sequence=self._clinical_sequence)

    def _on_vlm_health(self, message: VLMHealth) -> None:
        with self._lock:
            self._vlm_health = self._cache(message)

    def _on_input_status(self, source: str, message: InputSourceStatus) -> None:
        with self._lock:
            self._input_statuses[source] = self._cache(message)

    def _on_skill_status(self, message: SkillStatus) -> None:
        with self._lock:
            self._skill_status = self._cache(message)

    def _on_bed_robot_arm_status(self, message: BedRobotArmStateArray) -> None:
        revision = int(message.revision)
        source_stamp_sec = (
            float(message.stamp.sec) + float(message.stamp.nanosec) / 1e9
        )
        procedure_type = str(message.procedure_type).strip().casefold()
        expected_roles = _BED_ROBOT_PROCEDURE_LAYOUTS.get(procedure_type)
        arm_ids: set[str] = set()
        roles: set[str] = set()
        valid = expected_roles is not None and len(message.arms) == len(expected_roles)
        for arm in message.arms:
            arm_id = str(arm.arm_id).strip()
            role_instance = str(arm.role_instance_id).strip()
            state = str(arm.state).strip()
            valid = bool(
                valid
                and arm_id in {"arm_1", "arm_2"}
                and arm_id not in arm_ids
                and str(arm.role).strip() == "retraction"
                and role_instance in expected_roles
                and role_instance not in roles
                and state in _BED_ROBOT_STATES
                and bool(arm.direct_teach_active) == (state == "direct_teach")
            )
            arm_ids.add(arm_id)
            roles.add(role_instance)
        if (
            not valid
            or frozenset(roles) != expected_roles
            or source_stamp_sec <= 0.0
        ):
            self.get_logger().warning(
                "ignored invalid bed robot arm status at public gateway"
            )
            return
        with self._lock:
            current_stamp = self._bed_robot_arm_source_stamp_sec
            current_revision = self._bed_robot_arm_revision
            stale = bool(
                current_stamp is not None
                and (
                    source_stamp_sec < current_stamp
                    or (
                        source_stamp_sec == current_stamp
                        and current_revision is not None
                        and revision <= current_revision
                    )
                )
            )
            if stale:
                self.get_logger().warning(
                    "ignored stale bed robot arm status "
                    f"stamp={source_stamp_sec:.9f} revision={revision}"
                )
                return
            self._bed_robot_arm_revision = revision
            self._bed_robot_arm_source_stamp_sec = source_stamp_sec
            self._bed_robot_arm_status = self._cache(message)

    def _on_event(self, message: TwinEvent) -> None:
        """Publish events immediately; snapshots remain rate-limited."""

        try:
            projection = project_event(message)
            with self._lock:
                self._event_sequence += 1
                sequence = self._event_sequence
            public_event = SurgeryEvent()
            public_event.stamp = self._stamp_or_now(projection.stamp)
            public_event.sequence = sequence
            public_event.event_type = projection.event_type
            public_event.subject_type = projection.subject_type
            public_event.subject_id = projection.subject_id
            public_event.phase = projection.phase
            public_event.location_type = projection.location_type
            public_event.location_id = projection.location_id
            public_event.state = projection.state
            public_event.correlation_id = projection.correlation_id
            public_event.confidence = projection.confidence
            public_event.evidence_status = projection.evidence_status
            self._events_pub.publish(public_event)
        except Exception as exc:  # pragma: no cover - defensive ROS boundary
            self.get_logger().error(f"Unable to publish public surgery event: {exc}")

    def _source_freshness(self, now_monotonic_sec: float) -> dict[str, Freshness]:
        with self._lock:
            world = self._world
            vlm_result = self._vlm_result
            vlm_health = self._vlm_health
            input_statuses = dict(self._input_statuses)
            skill_status = self._skill_status
            bed_robot_arm_status = self._bed_robot_arm_status
        freshness = {
            "world_state": freshness_from_receipt(
                world.received_monotonic_sec if world else None,
                now_monotonic_sec,
                self._world_stale_after_sec,
            ),
            "vlm_result": freshness_from_receipt(
                vlm_result.received_monotonic_sec if vlm_result else None,
                now_monotonic_sec,
                self._vlm_stale_after_sec,
            ),
            "vlm_health": freshness_from_receipt(
                vlm_health.received_monotonic_sec if vlm_health else None,
                now_monotonic_sec,
                self._health_stale_after_sec,
            ),
            "skill_status": freshness_from_receipt(
                skill_status.received_monotonic_sec if skill_status else None,
                now_monotonic_sec,
                self._robot_stale_after_sec,
            ),
            "bed_robot_arm_status": freshness_from_receipt(
                bed_robot_arm_status.received_monotonic_sec
                if bed_robot_arm_status
                else None,
                now_monotonic_sec,
                self._robot_stale_after_sec,
            ),
        }
        for source in ("speech_input", "flir", "cam4"):
            cached = input_statuses.get(source)
            receipt = freshness_from_receipt(
                cached.received_monotonic_sec if cached else None,
                now_monotonic_sec,
                self._health_stale_after_sec,
            )
            if cached is None or not receipt.fresh:
                freshness[source] = receipt
                continue
            state = str(getattr(cached.message, "state", "")).upper()
            healthy = bool(getattr(cached.message, "healthy", False))
            freshness[source] = Freshness(
                available=state not in {"", "MISSING", "DISABLED"},
                fresh=healthy and state in {"READY", "HEALTHY"},
                age_sec=float(getattr(cached.message, "age_sec", receipt.age_sec)),
            )
        return freshness

    def _stamp_or_now(self, stamp: Any) -> Any:
        return stamp if stamp is not None else self.get_clock().now().to_msg()

    @staticmethod
    def _to_public_instrument(projection: InstrumentProjection) -> InstrumentState:
        message = InstrumentState()
        message.stamp = projection.stamp
        message.instrument_id = projection.instrument_id
        message.instance_id = projection.instance_id
        message.location_type = projection.location_type
        message.location_id = projection.location_id
        message.holder_role = projection.holder_role
        message.state = projection.state
        message.visible = projection.visible
        message.confidence = projection.confidence
        message.evidence_status = projection.evidence_status
        return message

    @staticmethod
    def _to_public_robot(projection: RobotProjection) -> RobotState:
        message = RobotState()
        message.stamp = projection.stamp
        message.robot_id = projection.robot_id
        message.robot_type = projection.robot_type
        message.connection_state = projection.connection_state
        message.execution_state = projection.execution_state
        message.active_command_id = projection.active_command_id
        message.progress = projection.progress
        message.reason_code = projection.reason_code
        message.evidence_status = projection.evidence_status
        return message

    @staticmethod
    def _to_public_clinical(
        projection: ClinicalObservationProjection, sequence: int
    ) -> ClinicalObservation:
        message = ClinicalObservation()
        message.stamp = projection.stamp
        message.sequence = sequence
        message.source = projection.source
        message.summary = projection.summary
        message.phase_ids = list(projection.phase_ids)
        message.phase_confidences = list(projection.phase_confidences)
        message.observed_tool_ids = list(projection.observed_tool_ids)
        message.observed_location_types = list(projection.observed_location_types)
        message.observed_location_ids = list(projection.observed_location_ids)
        message.observed_confidences = list(projection.observed_confidences)
        message.gesture_event_type = projection.gesture_event_type
        message.gesture_requested_tool = projection.gesture_requested_tool
        message.gesture_hand_pose = projection.gesture_hand_pose
        message.gesture_confidence = projection.gesture_confidence
        message.uncertainty = projection.uncertainty
        message.evidence_status = projection.evidence_status
        return message

    def _merged_robot_projections(
        self,
        fresh: dict[str, Freshness],
    ) -> tuple[RobotProjection, ...]:
        """Publish humanoid state and controller-owned retraction-arm state."""

        robots: dict[str, RobotProjection] = {}

        with self._lock:
            skill_status = self._skill_status
            bed_robot_arm_status = self._bed_robot_arm_status

        if skill_status is not None and fresh["skill_status"].fresh:
            skill_robot = project_skill_robot_status(skill_status.message)
            robots[skill_robot.robot_id] = skill_robot

        if bed_robot_arm_status is not None and fresh["bed_robot_arm_status"].fresh:
            array = bed_robot_arm_status.message
            for arm in array.arms:
                status_robot = project_bed_robot_arm_state(arm, array.stamp)
                if status_robot.robot_id:
                    robots[status_robot.robot_id] = status_robot
        return tuple(robots[key] for key in sorted(robots))

    def _health_message(
        self,
        *,
        revision: int,
        fresh: dict[str, Freshness],
    ) -> SurgeryHealth:
        unavailable = [name for name in self._SOURCE_NAMES if not fresh[name].available]
        stale = [
            name
            for name in self._SOURCE_NAMES
            if fresh[name].available and not fresh[name].fresh
        ]
        errors: list[str] = []

        with self._lock:
            vlm_health = self._vlm_health.message if self._vlm_health else None
            skill_status = self._skill_status.message if self._skill_status else None
            bed_robot_arm_status = (
                self._bed_robot_arm_status.message
                if self._bed_robot_arm_status
                else None
            )
            input_statuses = [entry.message for entry in self._input_statuses.values()]

        if vlm_health is not None:
            if not bool(vlm_health.connected):
                errors.append("vlm_disconnected")
            if not bool(vlm_health.healthy):
                errors.append("vlm_unhealthy")
        skill_state = str(getattr(skill_status, "state", "")) if skill_status is not None else ""
        skill_failed = skill_state in _SKILL_FAILURE_STATES or (
            skill_status is not None
            and not bool(getattr(skill_status, "success", True))
            and skill_state not in {"cancel_requested", "skipped_while_busy"}
        )
        if skill_failed:
            errors.append("skill_execution_failed")
        if bed_robot_arm_status is not None and any(
            str(getattr(arm, "reason_code", "")).strip().lower()
            not in {"", "ok"}
            for arm in bed_robot_arm_status.arms
        ):
            errors.append("bed_robot_error")
        for status in input_statuses:
            error_code = str(getattr(status, "error_code", "")).strip()
            if error_code:
                errors.append(error_code)

        required_unavailable = sorted(set(unavailable).intersection(self._required_health_sources))
        required_stale = sorted(set(stale).intersection(self._required_health_sources))
        if required_unavailable:
            state = "unavailable"
        elif required_stale or errors:
            state = "degraded"
        else:
            state = "healthy"

        message = SurgeryHealth()
        message.stamp = self.get_clock().now().to_msg()
        message.revision = revision
        message.healthy = state == "healthy"
        message.state = state
        message.unavailable_sources = unavailable
        message.stale_sources = stale
        message.error_codes = sorted(set(errors))
        message.evidence_status = GATEWAY_OBSERVED
        return message

    def _publish_snapshots(self) -> None:
        """Publish clear current snapshots and overwrite stale latched state safely."""

        try:
            now_monotonic_sec = self._monotonic()
            fresh = self._source_freshness(now_monotonic_sec)
            with self._lock:
                self._revision += 1
                revision = self._revision
                world = self._world.message if self._world else None
                vlm_result = self._vlm_result

            if world is not None and fresh["world_state"].fresh:
                context_projection = project_context(world)
                context = SurgeryContext()
                context.stamp = self._stamp_or_now(context_projection.stamp)
                context.revision = revision
                context.procedure_type = context_projection.procedure_type
                context.procedure_active = context_projection.procedure_active
                context.current_phase = context_projection.current_phase
                context.phase_confidence = context_projection.phase_confidence
                context.phase_uncertain = context_projection.phase_uncertain
                context.execution_state = context_projection.execution_state
                context.evidence_status = context_projection.evidence_status
                context.safety_flags = list(context_projection.safety_flags)
                self._context_pub.publish(context)

                instruments = InstrumentStateArray()
                instruments.stamp = self._stamp_or_now(context_projection.stamp)
                instruments.revision = revision
                instruments.instruments = [
                    self._to_public_instrument(projection)
                    for projection in project_instruments(world)
                ]
                self._instruments_pub.publish(instruments)
            else:
                # Overwrite transient-local data with an explicit unknown state
                # instead of leaving a late-joining consumer with an old fact.
                context = SurgeryContext()
                context.stamp = self.get_clock().now().to_msg()
                context.revision = revision
                context.evidence_status = "UNKNOWN"
                self._context_pub.publish(context)
                instruments = InstrumentStateArray()
                instruments.stamp = context.stamp
                instruments.revision = revision
                self._instruments_pub.publish(instruments)

            robots = RobotStateArray()
            robots.stamp = self.get_clock().now().to_msg()
            robots.revision = revision
            robots.robots = [
                self._to_public_robot(projection)
                for projection in self._merged_robot_projections(fresh)
            ]
            self._robots_pub.publish(robots)

            clinical = ClinicalObservationArray()
            clinical.stamp = self.get_clock().now().to_msg()
            clinical.revision = revision
            if vlm_result is not None and fresh["vlm_result"].fresh:
                projection = project_clinical_observation(vlm_result.message)
                clinical.stamp = self._stamp_or_now(projection.stamp)
                clinical.observations = [
                    self._to_public_clinical(projection, vlm_result.sequence)
                ]
            self._clinical_pub.publish(clinical)

            self._health_pub.publish(self._health_message(revision=revision, fresh=fresh))
        except Exception as exc:  # pragma: no cover - defensive ROS boundary
            self.get_logger().error(f"Unable to publish public state snapshots: {exc}")


def main(args: list[str] | None = None) -> None:
    rclpy.init(args=args)
    node = SurgicalInteropGateway()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()
