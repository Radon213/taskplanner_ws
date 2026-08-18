"""Record every public inference and decision layer into one append-only trace."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable

import rclpy
from rclpy.node import Node
from rclpy.qos import (
    DurabilityPolicy,
    HistoryPolicy,
    QoSProfile,
    ReliabilityPolicy,
)
from sensor_msgs.msg import CompressedImage
from std_msgs.msg import String
from surgical_interop_msgs.msg import BedRobotArmStateArray
from surgical_msgs.msg import (
    BedRobotArmGroupCommand,
    BedRobotArmGroupRequest,
    BTDecision,
    PhaseEvidence,
    ReducerDecisionEvent,
    ShadowReplayState,
    SimulationState,
    SkillCommand,
    SkillStatus,
    ToolObservation,
    TwinEvent,
    VLMHealth,
    VLMInferenceProposal,
    VLMReducerDecision,
    VLMRequestContext,
    VLMResult,
    WorldState,
)
from vlm_node.real_vlm import summarize_public_perception_json
from vlm_node.rfdetr_contract import parse_cam4_semantics_json

from .message_conversion import (
    compressed_image_payload,
    message_payload,
    message_source_stamp,
)
from .trace_io import AsyncTraceWriter, TraceWriter


IMAGE_TRACE_QOS = QoSProfile(
    history=HistoryPolicy.KEEP_LAST,
    depth=256,
    reliability=ReliabilityPolicy.BEST_EFFORT,
    durability=DurabilityPolicy.VOLATILE,
)
DIAGNOSTICS_TRACE_QOS = QoSProfile(
    history=HistoryPolicy.KEEP_LAST,
    depth=50,
    reliability=ReliabilityPolicy.BEST_EFFORT,
    durability=DurabilityPolicy.VOLATILE,
)
RECORDER_FLUSH_EVERY_RECORDS = 128
SEMANTIC_CHECKPOINT_LAYERS = frozenset(
    {
        "bed_robot_arm_status",
        "bt_context_ingress",
        "reducer_fused",
        "rfdetr_health",
        "runtime_control",
        "runtime_state",
        "shadow_replay_state",
    }
)


def semantic_trace_signature(payload: dict[str, Any]) -> str:
    """Hash semantic state while ignoring transport freshness metadata."""

    def normalize(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): normalize(item)
                for key, item in value.items()
                if str(key)
                not in {"stamp", "stamp_nanosec", "stamp_sec", "revision"}
            }
        if isinstance(value, list):
            return [normalize(item) for item in value]
        return value

    encoded = json.dumps(
        normalize(payload),
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class SemanticTraceGate:
    """Record transitions immediately and unchanged state as sparse checkpoints."""

    def __init__(
        self,
        *,
        checkpoint_sec: float = 30.0,
        layers: frozenset[str] = SEMANTIC_CHECKPOINT_LAYERS,
    ) -> None:
        self._checkpoint_sec = max(0.0, float(checkpoint_sec))
        self._layers = layers
        self._state: dict[tuple[str, str], tuple[str, float]] = {}
        self._source_revisions: dict[tuple[str, str], int] = {}
        self._lock = threading.Lock()

    def should_append(
        self,
        *,
        layer: str,
        topic: str,
        payload: dict[str, Any],
        now_monotonic: float | None = None,
    ) -> bool:
        if layer not in self._layers or self._checkpoint_sec <= 0.0:
            return True
        # Runtime controls are semantic state transitions plus late-join
        # heartbeats. Preserve the first edge and checkpoint, but do not turn
        # identical retained retries into duplicate trace history.
        runtime_reset = False
        if layer == "runtime_control":
            control = (
                str(payload.get("data", ""))
                .partition(":")[0]
                .strip()
                .lower()
            )
            runtime_reset = control == "reset"
            if not runtime_reset and control not in {
                "start",
                "start_runtime",
                "start_actors",
                "pause",
                "resume",
                "stop",
            }:
                return True
        now = time.monotonic() if now_monotonic is None else float(now_monotonic)
        signature = semantic_trace_signature(payload)
        key = (str(layer), str(topic))
        with self._lock:
            if runtime_reset:
                # Reset is an explicit edge, not a heartbeat. Record every
                # request and invalidate the previous lifecycle checkpoint so
                # the next identical start/stop edge is never suppressed.
                self._state.pop(key, None)
                return True
            previous = self._state.get(key)
            revision_rollback = False
            if layer == "bed_robot_arm_status":
                raw_revision = payload.get("revision")
                if isinstance(raw_revision, int) and not isinstance(raw_revision, bool):
                    previous_revision = self._source_revisions.get(key)
                    revision_rollback = (
                        previous_revision is not None
                        and raw_revision < previous_revision
                    )
                    self._source_revisions[key] = raw_revision
            if (
                previous is not None
                and signature == previous[0]
                and not revision_rollback
                and now - previous[1] < self._checkpoint_sec
            ):
                return False
            self._state[key] = (signature, now)
            return True


def _safe_run_component(run_id: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", str(run_id).strip())
    return value.strip(".-") or "run"


def open_run_trace_writer(
    output_path: Path,
    *,
    run_id: str,
    mode: str,
    existing_file_policy: str = "unique",
    asynchronous: bool = False,
    flush_every_records: int = 1,
) -> tuple[TraceWriter | AsyncTraceWriter, Path]:
    """Open a trace without truncating or crashing on an existing run."""

    def open_candidate(path: Path) -> TraceWriter | AsyncTraceWriter:
        writer = TraceWriter(
            path,
            run_id=run_id,
            mode=mode,
            flush_every_records=flush_every_records,
        )
        return AsyncTraceWriter(writer) if asynchronous else writer

    policy = str(existing_file_policy or "unique").strip().lower()
    if policy not in {"error", "unique"}:
        raise ValueError(
            "existing_file_policy must be 'unique' or 'error'"
        )
    requested = Path(output_path)
    if policy == "error":
        return open_candidate(requested), requested

    try:
        return open_candidate(requested), requested
    except FileExistsError:
        pass

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    run_component = _safe_run_component(run_id)
    suffix = requested.suffix
    stem = requested.stem if suffix else requested.name
    for index in range(1, 10_000):
        candidate = requested.with_name(
            f"{stem}.{run_component}.{stamp}.{index:03d}{suffix}"
        )
        try:
            return open_candidate(candidate), candidate
        except FileExistsError:
            continue
    raise FileExistsError(
        f"could not allocate a unique trace path for {requested}"
    )


class ShadowTraceRecorderNode(Node):
    def __init__(self) -> None:
        super().__init__("shadow_trace_recorder")
        self.declare_parameter("output_path", "")
        self.declare_parameter("run_id", "")
        self.declare_parameter("mode", "strict")
        self.declare_parameter("existing_file_policy", "unique")
        self.declare_parameter("semantic_checkpoint_sec", 30.0)
        self.declare_parameter(
            "field_image_topic",
            "/surgery/cam4/color/image/compressed",
        )
        self.declare_parameter("flir_image_topic", "")
        self.declare_parameter(
            "segmented_flir_image_topic",
            "/surgery/images/flir/segmented/compressed",
        )
        self.declare_parameter("cam4_image_topic", "")
        self.declare_parameter("tray_image_topic", "")
        self.declare_parameter(
            "composite_image_topic",
            "/surgery/images/vlm/composite/compressed",
        )
        self.declare_parameter(
            "perception_bboxes_topic",
            "/surgery/perception/cam4/tools/bboxes/json",
        )
        self.declare_parameter(
            "perception_segmentation_topic",
            "/surgery/perception/cam4/tools/segmentation/json",
        )
        self.declare_parameter(
            "cam4_semantics_topic",
            "/surgery/perception/cam4/semantics/json",
        )
        self.declare_parameter(
            "rfdetr_health_topic",
            "/surgery/perception/rfdetr/health",
        )
        self.declare_parameter(
            "rfdetr_diagnostics_topic",
            "/surgery/perception/rfdetr/diagnostics/json",
        )
        self.declare_parameter("source_transcript_topic", "/surgery/transcript")
        self.declare_parameter("fault_status_topic", "/test/fault/status")
        output_path = Path(str(self.get_parameter("output_path").value))
        run_id = str(self.get_parameter("run_id").value).strip()
        mode = str(self.get_parameter("mode").value).strip()
        if not output_path.name:
            raise ValueError("output_path is required")
        if not run_id:
            raise ValueError("run_id is required")
        self._writer, self._output_path = open_run_trace_writer(
            output_path,
            run_id=run_id,
            mode=mode,
            existing_file_policy=str(
                self.get_parameter("existing_file_policy").value
            ),
            asynchronous=True,
            flush_every_records=RECORDER_FLUSH_EVERY_RECORDS,
        )
        self._semantic_trace_gate = SemanticTraceGate(
            checkpoint_sec=float(
                self.get_parameter("semantic_checkpoint_sec").value
            )
        )

        self._subscribe_image(
            str(self.get_parameter("field_image_topic").value),
            "field",
        )
        self._subscribe_image(
            str(self.get_parameter("flir_image_topic").value),
            "flir",
            layer="normalized_input_image",
        )
        self._subscribe_image(
            str(self.get_parameter("segmented_flir_image_topic").value),
            "flir_rfdetr_segmented",
            layer="vlm_preprocessed_input_image",
        )
        self._subscribe_image(
            str(self.get_parameter("cam4_image_topic").value),
            "cam4",
            layer="normalized_input_image",
        )
        self._subscribe_image(
            str(self.get_parameter("tray_image_topic").value),
            "tray",
        )
        self._subscribe_image(
            str(self.get_parameter("composite_image_topic").value),
            "vlm_model_ready_composite",
            layer="vlm_model_input_image",
        )
        self._subscribe(
            String,
            str(self.get_parameter("perception_bboxes_topic").value),
            "normalized_perception",
            "std_msgs/msg/String",
            payload_transform=lambda msg: summarize_public_perception_json(
                msg.data,
                kind="bboxes",
            ),
        )
        self._subscribe(
            String,
            str(self.get_parameter("perception_segmentation_topic").value),
            "normalized_perception",
            "std_msgs/msg/String",
            payload_transform=lambda msg: summarize_public_perception_json(
                msg.data,
                kind="segmentation",
            ),
        )
        self._subscribe(
            String,
            str(self.get_parameter("cam4_semantics_topic").value),
            "cam4_semantic_perception",
            "std_msgs/msg/String",
            payload_transform=lambda msg: parse_cam4_semantics_json(
                msg.data
            ),
        )
        self._subscribe(
            String,
            str(self.get_parameter("rfdetr_health_topic").value),
            "rfdetr_health",
            "std_msgs/msg/String",
            payload_transform=self._json_string_payload,
        )
        self._subscribe(
            String,
            str(self.get_parameter("rfdetr_diagnostics_topic").value),
            "rfdetr_diagnostics",
            "std_msgs/msg/String",
            payload_transform=self._json_string_payload,
            qos=DIAGNOSTICS_TRACE_QOS,
        )
        self._subscribe(
            String,
            str(self.get_parameter("source_transcript_topic").value),
            "input_transcript",
            "std_msgs/msg/String",
        )
        self._subscribe(
            String,
            str(self.get_parameter("fault_status_topic").value),
            "fault_injection_status",
            "std_msgs/msg/String",
            payload_transform=self._json_string_payload,
        )
        self._subscribe(
            String,
            "/surgery/audio/request_text",
            "input_transcript",
            "std_msgs/msg/String",
        )
        self._subscribe(
            String,
            "/simulation/control_state",
            "runtime_control",
            "std_msgs/msg/String",
        )
        self._subscribe(
            ShadowReplayState,
            "/shadow/replay_state",
            "shadow_replay_state",
            "surgical_msgs/msg/ShadowReplayState",
        )
        self._subscribe(
            String,
            "/shadow/ground_truth/state",
            "evaluation_ground_truth",
            "std_msgs/msg/String",
            payload_transform=self._json_string_payload,
        )
        self._subscribe(
            SimulationState,
            "/simulation/state",
            "runtime_state",
            "surgical_msgs/msg/SimulationState",
        )
        self._subscribe(
            VLMRequestContext,
            "/context/vlm_request_context",
            "vlm_request",
            "surgical_msgs/msg/VLMRequestContext",
        )
        self._subscribe(
            VLMHealth,
            "/vlm/health",
            "vlm_health",
            "surgical_msgs/msg/VLMHealth",
        )
        self._subscribe(
            VLMResult,
            "/vlm/result",
            "vlm_raw",
            "surgical_msgs/msg/VLMResult",
        )
        self._subscribe(
            VLMResult,
            "/vlm/model_raw_result",
            "vlm_model_raw",
            "surgical_msgs/msg/VLMResult",
        )
        self._subscribe(
            ToolObservation,
            "/vlm/tool_observations",
            "vlm_tool_observation",
            "surgical_msgs/msg/ToolObservation",
        )
        self._subscribe(
            PhaseEvidence,
            "/vlm/phase_evidence",
            "vlm_proposal",
            "surgical_msgs/msg/PhaseEvidence",
        )
        self._subscribe(
            VLMInferenceProposal,
            "/vlm/inference_proposals",
            "vlm_proposal",
            "surgical_msgs/msg/VLMInferenceProposal",
        )
        self._subscribe(
            VLMReducerDecision,
            "/vlm/reducer_decisions",
            "vlm_reducer_decision",
            "surgical_msgs/msg/VLMReducerDecision",
        )
        self._subscribe(
            ReducerDecisionEvent,
            "/twin/reducer_decisions",
            "reducer_event",
            "surgical_msgs/msg/ReducerDecisionEvent",
        )
        self._subscribe(
            WorldState,
            "/twin/world_state",
            "reducer_fused",
            "surgical_msgs/msg/WorldState",
        )
        self._subscribe(
            WorldState,
            "/bt/context_ingress",
            "bt_context_ingress",
            "surgical_msgs/msg/WorldState",
        )
        self._subscribe(
            BTDecision,
            "/bt/decision",
            "bt_decision",
            "surgical_msgs/msg/BTDecision",
        )
        self._subscribe(
            SkillCommand,
            "/bt/skill_command",
            "skill_command",
            "surgical_msgs/msg/SkillCommand",
        )
        self._subscribe(
            SkillStatus,
            "/skill/status",
            "skill_status",
            "surgical_msgs/msg/SkillStatus",
        )
        self._subscribe(
            BedRobotArmGroupRequest,
            "/surgeon/bed_robot_arm_group_request",
            "bed_robot_arm_group_request",
            "surgical_msgs/msg/BedRobotArmGroupRequest",
        )
        self._subscribe(
            BedRobotArmGroupCommand,
            "/bt/bed_robot_arm_group_command",
            "bed_robot_arm_group_command",
            "surgical_msgs/msg/BedRobotArmGroupCommand",
        )
        self._subscribe(
            BedRobotArmStateArray,
            "/external/bed_robot_arms/status",
            "bed_robot_arm_status",
            "surgical_interop_msgs/msg/BedRobotArmStateArray",
        )
        self._subscribe(
            TwinEvent,
            "/skill/events",
            "skill_event",
            "surgical_msgs/msg/TwinEvent",
        )
        self._subscribe(
            String,
            "/shadow/skill_outcome",
            "shadow_sink",
            "std_msgs/msg/String",
            payload_transform=self._json_string_payload,
        )
        self._subscribe(
            ToolObservation,
            "/shadow/evaluation_observation",
            "evaluation_observation",
            "surgical_msgs/msg/ToolObservation",
        )
        self._subscribe(
            String,
            "/shadow/reconciliation_event",
            "evaluation_observation",
            "std_msgs/msg/String",
            payload_transform=self._json_string_payload,
        )
        if self._output_path != output_path:
            self.get_logger().warning(
                f"trace path already existed; recording this run to "
                f"{self._output_path}"
            )
        else:
            self.get_logger().info(
                f"recording shadow trace to {self._output_path}"
            )

    @staticmethod
    def _json_string_payload(msg: String) -> dict[str, Any]:
        try:
            value = json.loads(msg.data)
        except json.JSONDecodeError:
            return {"data": msg.data}
        return value if isinstance(value, dict) else {"data": value}

    @staticmethod
    def _correlation_id(payload: dict[str, Any]) -> str:
        for key in (
            "command_id",
            "proposal_id",
            "input_id",
            "request_id",
            "event_id",
            "active_robot_task_id",
        ):
            value = str(payload.get(key, "") or "").strip()
            if value:
                return value
        return ""

    def _now_sec(self) -> float:
        return self.get_clock().now().nanoseconds / 1_000_000_000.0

    def _append(
        self,
        *,
        layer: str,
        topic: str,
        message_type: str,
        payload: dict[str, Any],
        source_stamp_sec: float | None,
    ) -> None:
        if not self._semantic_trace_gate.should_append(
            layer=layer,
            topic=topic,
            payload=payload,
        ):
            return
        self._writer.append(
            layer=layer,
            topic=topic,
            message_type=message_type,
            ros_time_sec=self._now_sec(),
            wall_time_sec=time.time(),
            payload=payload,
            source_stamp_sec=source_stamp_sec,
            correlation_id=self._correlation_id(payload),
        )

    def _subscribe(
        self,
        message_class: Any,
        topic: str,
        layer: str,
        message_type: str,
        *,
        payload_transform: Callable[[Any], dict[str, Any]] = message_payload,
        qos: int | QoSProfile = 50,
    ) -> None:
        if not topic:
            return

        def callback(msg: Any) -> None:
            self._append(
                layer=layer,
                topic=topic,
                message_type=message_type,
                payload=payload_transform(msg),
                source_stamp_sec=message_source_stamp(msg),
            )

        self.create_subscription(message_class, topic, callback, qos)

    def _subscribe_image(
        self,
        topic: str,
        source: str,
        *,
        layer: str = "input_image",
    ) -> None:
        if not topic:
            return

        def callback(msg: CompressedImage) -> None:
            self._append(
                layer=layer,
                topic=topic,
                message_type="sensor_msgs/msg/CompressedImage",
                payload=compressed_image_payload(msg, source=source),
                source_stamp_sec=message_source_stamp(msg),
            )

        self.create_subscription(
            CompressedImage,
            topic,
            callback,
            IMAGE_TRACE_QOS,
        )

    def close(self) -> None:
        writer = getattr(self, "_writer", None)
        if writer is not None:
            writer.close()


def main() -> None:
    rclpy.init()
    node = ShadowTraceRecorderNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.close()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
