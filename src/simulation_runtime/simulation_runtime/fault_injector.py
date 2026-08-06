"""Opt-in ROS relays for deterministic release fault campaigns."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import time
from typing import Any, Callable

import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import CompressedImage
from std_msgs.msg import String
from surgical_msgs.msg import SpeechUtterance, VLMHealth, VLMResult

from .fault_scenario import (
    FaultEvent,
    FaultScenario,
    compact_fault_report,
    transform_image_bytes,
    transform_speech_text,
)


@dataclass
class DelayedMessage:
    release_monotonic_sec: float
    publisher: Any
    message: Any
    source: str
    duplicate_count: int


class FaultInjector(Node):
    """Relay only explicitly remapped test topics; never starts by default."""

    def __init__(self) -> None:
        super().__init__("taskplanner_fault_injector")
        self.declare_parameter("scenario_path", "")
        self.declare_parameter("enabled", False)
        self.declare_parameter("start_on_first_message", True)
        self.declare_parameter("start_on_first_image", False)
        self.declare_parameter("raw_flir_topic", "/test/fault/raw/flir/compressed")
        self.declare_parameter("raw_cam4_topic", "/test/fault/raw/cam4/compressed")
        self.declare_parameter("raw_speech_topic", "/test/fault/raw/speech/utterance")
        self.declare_parameter("raw_sentence_topic", "/test/fault/raw/speech/sentence")
        self.declare_parameter("raw_vlm_result_topic", "/test/fault/raw/vlm/result")
        self.declare_parameter("raw_vlm_health_topic", "/test/fault/raw/vlm/health")
        self.declare_parameter("flir_topic", "/surgery/images/flir/compressed")
        self.declare_parameter("cam4_topic", "/surgery/images/cam4/compressed")
        self.declare_parameter("speech_topic", "/shadow/speech/utterance")
        self.declare_parameter("sentence_topic", "/sensors/surgeon/sentence")
        self.declare_parameter("vlm_result_topic", "/vlm/result")
        self.declare_parameter("vlm_health_topic", "/vlm/health")
        self.declare_parameter("status_topic", "/test/fault/status")

        scenario_path = str(self.get_parameter("scenario_path").value).strip()
        enabled = bool(self.get_parameter("enabled").value)
        if not enabled or not scenario_path:
            raise RuntimeError(
                "fault injector requires enabled=true and an explicit scenario_path"
            )
        self._scenario = FaultScenario.load(scenario_path)
        self._started_monotonic: float | None = (
            None
            if bool(self.get_parameter("start_on_first_message").value)
            else time.monotonic()
        )
        self._start_on_first_image = bool(
            self.get_parameter("start_on_first_image").value
        )
        self._sequence: dict[str, int] = {}
        self._last_good: dict[str, Any] = {}
        self._reorder_pending: dict[str, tuple[Any, Any]] = {}
        self._delayed: list[DelayedMessage] = []
        self._counters: dict[str, dict[str, int]] = {}

        self._flir_pub = self.create_publisher(
            CompressedImage,
            str(self.get_parameter("flir_topic").value),
            qos_profile_sensor_data,
        )
        self._cam4_pub = self.create_publisher(
            CompressedImage,
            str(self.get_parameter("cam4_topic").value),
            qos_profile_sensor_data,
        )
        self._speech_pub = self.create_publisher(
            SpeechUtterance,
            str(self.get_parameter("speech_topic").value),
            20,
        )
        self._sentence_pub = self.create_publisher(
            String,
            str(self.get_parameter("sentence_topic").value),
            20,
        )
        self._vlm_result_pub = self.create_publisher(
            VLMResult,
            str(self.get_parameter("vlm_result_topic").value),
            10,
        )
        self._vlm_health_pub = self.create_publisher(
            VLMHealth,
            str(self.get_parameter("vlm_health_topic").value),
            10,
        )
        self._status_pub = self.create_publisher(
            String,
            str(self.get_parameter("status_topic").value),
            10,
        )

        self.create_subscription(
            CompressedImage,
            str(self.get_parameter("raw_flir_topic").value),
            lambda message: self._process("flir", message, self._flir_pub),
            qos_profile_sensor_data,
        )
        self.create_subscription(
            CompressedImage,
            str(self.get_parameter("raw_cam4_topic").value),
            lambda message: self._process("cam4", message, self._cam4_pub),
            qos_profile_sensor_data,
        )
        self.create_subscription(
            SpeechUtterance,
            str(self.get_parameter("raw_speech_topic").value),
            lambda message: self._process("speech", message, self._speech_pub),
            20,
        )
        self.create_subscription(
            String,
            str(self.get_parameter("raw_sentence_topic").value),
            lambda message: self._process("sentence", message, self._sentence_pub),
            20,
        )
        self.create_subscription(
            VLMResult,
            str(self.get_parameter("raw_vlm_result_topic").value),
            lambda message: self._process("vlm_result", message, self._vlm_result_pub),
            10,
        )
        self.create_subscription(
            VLMHealth,
            str(self.get_parameter("raw_vlm_health_topic").value),
            lambda message: self._process("vlm_health", message, self._vlm_health_pub),
            10,
        )
        self.create_timer(0.02, self._flush_delayed)
        self.create_timer(1.0, self._publish_status)

    def _events(self, source: str) -> tuple[FaultEvent, ...]:
        if self._started_monotonic is None:
            return ()
        elapsed_sec = time.monotonic() - self._started_monotonic
        sources = (source, "speech") if source == "sentence" else (source,)
        return self._scenario.active_any(sources, elapsed_sec)

    def _count(self, source: str, key: str, amount: int = 1) -> None:
        counters = self._counters.setdefault(source, {})
        counters[key] = counters.get(key, 0) + amount

    def _transform(self, source: str, message: Any, events: tuple[FaultEvent, ...]) -> Any:
        sequence = self._sequence[source]
        transformed = deepcopy(message)
        if isinstance(transformed, CompressedImage):
            transformed.data = transform_image_bytes(
                bytes(transformed.data),
                events=events,
                scenario=self._scenario,
                source=source,
                sequence=sequence,
            )
        elif isinstance(transformed, SpeechUtterance):
            transformed.text, transformed.is_final = transform_speech_text(
                transformed.text, events
            )
        elif isinstance(transformed, String):
            transformed.data, _ = transform_speech_text(transformed.data, events)
        elif isinstance(transformed, VLMResult) and any(
            event.kind == "vlm_invalid_schema" for event in events
        ):
            transformed.schema_version = "invalid"
            transformed.raw_json = "{invalid-json"
            transformed.phase_ids = []
            transformed.phase_confidences = []
            transformed.observed_tool_ids = []
            transformed.observed_confidences = []
        elif isinstance(transformed, VLMHealth) and any(
            event.kind
            in {"vlm_unhealthy", "vlm_timeout", "vlm_http_500", "vlm_restart"}
            for event in events
        ):
            failure_kind = next(
                event.kind
                for event in events
                if event.kind
                in {"vlm_unhealthy", "vlm_timeout", "vlm_http_500", "vlm_restart"}
            )
            transformed.connected = False
            transformed.healthy = False
            transformed.last_mode = failure_kind
            transformed.last_error = f"fault_injected_{failure_kind}"
        return transformed

    def _process(self, source: str, message: Any, publisher: Any) -> None:
        if self._started_monotonic is None and (
            not self._start_on_first_image or source in {"flir", "cam4"}
        ):
            self._started_monotonic = time.monotonic()
        self._sequence[source] = self._sequence.get(source, 0) + 1
        self._count(source, "received")
        events = self._events(source)
        kinds = {event.kind for event in events}
        for event in events:
            self._count(source, f"applied_{event.kind}")
        if "drop" in kinds or (
            source == "vlm_result"
            and kinds.intersection({"vlm_timeout", "vlm_http_500", "vlm_restart"})
        ):
            self._count(source, "dropped")
            return
        if "freeze" in kinds:
            if source not in self._last_good:
                self._count(source, "dropped")
                return
            transformed = deepcopy(self._last_good[source])
            self._count(source, "frozen")
        else:
            try:
                transformed = self._transform(source, message, events)
            except Exception as exc:
                self._count(source, "transform_errors")
                self.get_logger().warning(f"{source} fault transform failed: {exc}")
                return
            self._last_good[source] = deepcopy(transformed)

        duplicate_count = 2 if "duplicate" in kinds else 1
        delay_sec = max(
            [float(event.params.get("delay_sec", 0.5)) for event in events if event.kind == "delay"]
            or [0.0]
        )
        if "reorder" in kinds:
            pending = self._reorder_pending.pop(source, None)
            if pending is None:
                self._reorder_pending[source] = (publisher, transformed)
                self._count(source, "reorder_held")
                return
            self._emit(publisher, transformed, duplicate_count, source)
            pending_publisher, pending_message = pending
            self._emit(pending_publisher, pending_message, 1, source)
            self._count(source, "reordered")
            return
        if delay_sec > 0.0:
            self._delayed.append(
                DelayedMessage(
                    time.monotonic() + delay_sec,
                    publisher,
                    transformed,
                    source,
                    duplicate_count,
                )
            )
            self._count(source, "delayed")
            return
        self._emit(publisher, transformed, duplicate_count, source)

    def _emit(self, publisher: Any, message: Any, count: int, source: str) -> None:
        for _ in range(count):
            publisher.publish(deepcopy(message))
            self._count(source, "published")
        if count > 1:
            self._count(source, "duplicated", count - 1)

    def _flush_delayed(self) -> None:
        now = time.monotonic()
        ready = [item for item in self._delayed if item.release_monotonic_sec <= now]
        self._delayed = [item for item in self._delayed if item.release_monotonic_sec > now]
        for item in ready:
            self._emit(
                item.publisher,
                item.message,
                item.duplicate_count,
                item.source,
            )

    def _publish_status(self) -> None:
        message = String()
        message.data = compact_fault_report(self._scenario, self._counters)
        self._status_pub.publish(message)


def main() -> None:
    rclpy.init()
    node = FaultInjector()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
