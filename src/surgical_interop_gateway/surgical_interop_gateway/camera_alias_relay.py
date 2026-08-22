"""Demand-driven aliases for the two public compressed camera topics.

The camera owners retain their native topic names.  This node exposes stable
Taskplanner-owned aliases without decoding or re-encoding JPEG payloads.  It
only creates a source subscription while a consumer is actually matched to an
alias, so an idle public endpoint does not pull a full image stream over DDS.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Iterable

import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import CompressedImage
from surgical_msgs.msg import WorldState


@dataclass(frozen=True)
class CameraAliasBinding:
    """One resolved native-source to public-alias mapping."""

    name: str
    source_topic: str
    public_topic: str


def active_camera_aliases(
    bindings: Iterable[CameraAliasBinding],
) -> tuple[CameraAliasBinding, ...]:
    """Return a loop-free alias plan.

    A source already published on its public name would bypass the procedure
    activity gate, so it is rejected. Cross-channel cycles and duplicate public
    owners are also configuration errors; the node fails before creating ROS
    entities instead of risking a privacy bypass or camera feedback loop.
    """

    requested = tuple(bindings)
    direct_public = [
        binding.public_topic
        for binding in requested
        if binding.source_topic == binding.public_topic
    ]
    if direct_public:
        raise ValueError(
            "camera source must not use the gated public topic directly: "
            + ", ".join(sorted(direct_public))
        )
    active = requested
    public_topics = [binding.public_topic for binding in active]
    if len(set(public_topics)) != len(public_topics):
        raise ValueError("camera aliases must have unique public topics")

    source_topics = {binding.source_topic for binding in active}
    collisions = source_topics.intersection(public_topics)
    if collisions:
        raise ValueError(
            "camera alias input/output cycle detected: "
            + ", ".join(sorted(collisions))
        )
    return active


def publish_when_requested(publisher: Any, message: CompressedImage) -> bool:
    """Publish one unchanged compressed frame only for a matched consumer."""

    if publisher.get_subscription_count() <= 0:
        return False
    publisher.publish(message)
    return True


def _camera_qos() -> QoSProfile:
    """Workbook image QoS compatible with reliable or best-effort sources."""

    return QoSProfile(
        history=HistoryPolicy.KEEP_LAST,
        depth=5,
        reliability=ReliabilityPolicy.BEST_EFFORT,
        durability=DurabilityPolicy.VOLATILE,
    )


def _state_qos() -> QoSProfile:
    return QoSProfile(
        history=HistoryPolicy.KEEP_LAST,
        depth=5,
        reliability=ReliabilityPolicy.RELIABLE,
        durability=DurabilityPolicy.VOLATILE,
    )


def procedure_is_active(
    *,
    running: bool,
    procedure_id: str,
    expected_procedure_id: str,
    received_monotonic_sec: float | None,
    now_monotonic_sec: float,
    stale_after_sec: float,
) -> bool:
    """Fail closed unless the matching procedure run is currently fresh."""

    if received_monotonic_sec is None:
        return False
    age_sec = max(0.0, now_monotonic_sec - received_monotonic_sec)
    return bool(
        running
        and procedure_id.strip() == expected_procedure_id.strip()
        and age_sec <= max(0.0, stale_after_sec)
    )


class CameraAliasRelay(Node):
    """Expose stable public names for externally owned compressed cameras."""

    def __init__(self) -> None:
        super().__init__("surgical_camera_alias_relay")

        self._expected_procedure_id = str(
            self.declare_parameter("default_bundle", "thyroidectomy").value
        ).strip()
        self._world_stale_after_sec = max(
            0.1,
            float(self.declare_parameter("world_stale_after_sec", 3.0).value),
        )
        self._world_running = False
        self._world_procedure_id = ""
        self._world_received_monotonic_sec: float | None = None
        # Explicit demo-only escape hatch for a read-only FLIR preview. The
        # default remains fail-closed; Live enables this through its reviewed
        # mode environment while CAM4 keeps the active-procedure gate.
        self._publish_flir_while_idle = bool(
            self.declare_parameter("publish_flir_while_idle", False).value
        )

        requested = (
            CameraAliasBinding(
                name="flir",
                source_topic=self.resolve_topic_name(
                    str(
                        self.declare_parameter(
                            "flir_source_topic",
                            "/synced/flir/color/image_raw/compressed",
                        ).value
                    )
                ),
                public_topic=self.resolve_topic_name(
                    str(
                        self.declare_parameter(
                            "flir_public_topic",
                            "/surgery/images/flir/compressed",
                        ).value
                    )
                ),
            ),
            CameraAliasBinding(
                name="cam4",
                source_topic=self.resolve_topic_name(
                    str(
                        self.declare_parameter(
                            "cam4_source_topic",
                            "/synced/cam_4/color/image_raw/compressed",
                        ).value
                    )
                ),
                public_topic=self.resolve_topic_name(
                    str(
                        self.declare_parameter(
                            "cam4_public_topic",
                            "/surgery/images/cam4/compressed",
                        ).value
                    )
                ),
            ),
        )
        active = active_camera_aliases(requested)
        active_names = {binding.name for binding in active}
        for binding in requested:
            if binding.name not in active_names:
                self.get_logger().info(
                    f"{binding.name} already uses its public topic "
                    f"{binding.public_topic}; relay disabled for this channel"
                )

        self._alias_publishers: dict[str, Any] = {}
        self._source_subscriptions: dict[str, Any] = {}
        self._active_bindings = {binding.name: binding for binding in active}
        self._qos = _camera_qos()
        for binding in active:
            publisher = self.create_publisher(
                CompressedImage,
                binding.public_topic,
                self._qos,
            )
            self._alias_publishers[binding.name] = publisher
            self.get_logger().info(
                f"public {binding.name} camera alias: "
                f"{binding.source_topic} -> {binding.public_topic} "
                "(best-effort/volatile, depth 5, demand-driven)"
            )
        self._demand_timer = self.create_timer(0.5, self._reconcile_source_demand)
        self._world_subscription = self.create_subscription(
            WorldState,
            "/twin/world_state",
            self._on_world_state,
            _state_qos(),
        )

    @staticmethod
    def _monotonic() -> float:
        return time.monotonic()

    def _on_world_state(self, message: WorldState) -> None:
        self._world_running = bool(message.running)
        self._world_procedure_id = str(message.procedure_id)
        self._world_received_monotonic_sec = self._monotonic()
        # Reconcile immediately on a stop/mismatch so an already acquired
        # source is released without waiting for the periodic timer.
        self._reconcile_source_demand()

    def _procedure_active(self) -> bool:
        return procedure_is_active(
            running=self._world_running,
            procedure_id=self._world_procedure_id,
            expected_procedure_id=self._expected_procedure_id,
            received_monotonic_sec=self._world_received_monotonic_sec,
            now_monotonic_sec=self._monotonic(),
            stale_after_sec=self._world_stale_after_sec,
        )

    def _alias_available(self, name: str) -> bool:
        return bool(
            self._procedure_active()
            or (name == "flir" and self._publish_flir_while_idle)
        )

    def _publish_if_available(
        self,
        name: str,
        publisher: Any,
        message: CompressedImage,
    ) -> bool:
        # Check the run gate again in the frame callback. This closes the
        # interval between WorldState expiry and the next reconciliation tick.
        if not self._alias_available(name):
            return False
        return publish_when_requested(publisher, message)

    def _reconcile_source_demand(self) -> None:
        """Match native subscriptions to current public alias demand."""

        for name, binding in self._active_bindings.items():
            publisher = self._alias_publishers[name]
            requested = (
                self._alias_available(name)
                and publisher.get_subscription_count() > 0
            )
            subscription = self._source_subscriptions.get(name)
            if requested and subscription is None:
                self._source_subscriptions[name] = self.create_subscription(
                    CompressedImage,
                    binding.source_topic,
                    lambda message, alias=name, output=publisher: (
                        self._publish_if_available(alias, output, message)
                    ),
                    self._qos,
                )
                self.get_logger().info(
                    f"{name} alias acquired source stream on demand"
                )
            elif not requested and subscription is not None:
                self.destroy_subscription(subscription)
                del self._source_subscriptions[name]
                self.get_logger().info(
                    f"{name} alias released idle source stream"
                )


def main(args: list[str] | None = None) -> None:
    rclpy.init(args=args)
    node: CameraAliasRelay | None = None
    try:
        node = CameraAliasRelay()
        rclpy.spin(node)
    finally:
        if node is not None:
            node.destroy_node()
        rclpy.shutdown()
