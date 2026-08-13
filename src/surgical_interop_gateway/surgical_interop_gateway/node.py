"""Read-only ROS 2 gateway for the public surgical-integration topics.

This node deliberately has no publishers or services on Taskplanner's internal
command paths.  It only observes internal state, projects a reviewed subset,
and publishes the shared topics.  The projection helpers form the information
boundary; keep all policy-sensitive field selection there.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import threading
import time
from typing import Any
import uuid

import rclpy
from ament_index_python.packages import get_package_share_directory
from procedure_spec import load_bundle
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from surgical_interop_msgs.msg import (
    BedRobotArmStateArray,
    ClinicalObservation,
    ClinicalObservationArray,
    GatewayInfo,
    InstrumentCatalogEntry,
    InstrumentState,
    InstrumentStateArray,
    PhaseCatalogEntry,
    ProcedureCatalog,
    RobotEndEffectorState,
    RobotEndEffectorStateArray,
    RobotState,
    RobotStateArray,
    SpeechRecognitionState,
    SurgeryContext,
    SurgeryEvent,
    SurgeryHealth,
    ToolPrediction,
    ToolPredictionArray,
)
from std_msgs.msg import String
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
    UNKNOWN,
    ClinicalObservationProjection,
    Freshness,
    InstrumentProjection,
    RobotEndEffectorProjection,
    RobotProjection,
    ToolPredictionProjection,
    finite_nonnegative,
    finite_probability,
    freshness_from_receipt,
    project_clinical_observation,
    project_context,
    project_event,
    project_bed_robot_arm_state,
    project_instruments,
    project_robot_end_effectors,
    project_skill_robot_status,
    project_tool_predictions,
    stamp_to_seconds,
)


GATEWAY_OBSERVED = "GATEWAY_OBSERVED"
GATEWAY_OBSERVED_REDACTED = "GATEWAY_OBSERVED_REDACTED"
MODEL_OBSERVED_REDACTED = "MODEL_OBSERVED_REDACTED"
SCHEMA_VERSION = "1.0.0"
INTERFACE_VERSION = "0.3.0"
_MAX_ASR_STATUS_BYTES = 256 * 1024
_MAX_SPEECH_TEXT_CHARS = 2000
_PUBLIC_LATENCY_BASES = frozenset(
    {
        "api_round_trip",
        "latest_pcm_send_complete_to_final_receive",
    }
)
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
    received_stamp: Any = None


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
        # Free-form ASR/VLM text may contain patient or case information even
        # when the surrounding message is a reviewed public projection. Keep
        # the public state graph useful by default, but require a deliberate
        # deployment opt-in before either text field crosses this boundary.
        self._publish_free_text = bool(
            self.declare_parameter("publish_free_text", False).value
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

        self._default_bundle = str(
            self.declare_parameter("default_bundle", "thyroidectomy").value
        ).strip()
        if not self._default_bundle or Path(self._default_bundle).name != self._default_bundle:
            raise ValueError("default_bundle must be a single procedure bundle name")
        spec_root = Path(get_package_share_directory("procedure_spec")) / "specs"
        self._procedure_spec = load_bundle(spec_root / self._default_bundle)
        self._catalog_version = self._catalog_digest(self._procedure_spec)

        self._lock = threading.RLock()
        self._world: CachedMessage | None = None
        self._vlm_result: CachedMessage | None = None
        self._vlm_health: CachedMessage | None = None
        self._input_statuses: dict[str, CachedMessage] = {}
        self._skill_status: CachedMessage | None = None
        self._bed_robot_arm_status: CachedMessage | None = None
        self._bed_robot_arm_revision: int | None = None
        self._bed_robot_arm_source_stamp_sec: float | None = None
        self._speech_text: CachedMessage | None = None
        self._speech_sequence = 0
        self._asr_status: CachedMessage | None = None
        self._revision = 0
        self._event_sequence = 0
        self._clinical_sequence = 0
        self._gateway_instance_id = str(uuid.uuid4())
        self._procedure_run_id = ""
        self._procedure_run_start_source_stamp_sec: float | None = None
        self._last_procedure_active = False
        self._procedure_mismatch = False

        state_qos = _state_qos()
        self._context_pub = self.create_publisher(SurgeryContext, "/surgery/context", state_qos)
        self._instruments_pub = self.create_publisher(
            InstrumentStateArray, "/surgery/instruments", state_qos
        )
        self._robots_pub = self.create_publisher(RobotStateArray, "/surgery/robots", state_qos)
        self._prediction_pub = self.create_publisher(
            ToolPredictionArray, "/surgery/tool_predictions", state_qos
        )
        self._end_effectors_pub = self.create_publisher(
            RobotEndEffectorStateArray, "/surgery/robot_end_effectors", state_qos
        )
        self._catalog_pub = self.create_publisher(
            ProcedureCatalog, "/surgery/catalog", state_qos
        )
        self._gateway_info_pub = self.create_publisher(
            GatewayInfo, "/surgery/gateway_info", state_qos
        )
        self._speech_pub = self.create_publisher(
            SpeechRecognitionState, "/surgery/speech", state_qos
        )
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
            String,
            "/surgery/audio/request_text",
            self._on_speech_text,
            source_qos,
        )
        self.create_subscription(
            String,
            "/input/asr/runtime_status",
            self._on_asr_status,
            source_qos,
        )
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
    def _catalog_digest(spec: Any) -> str:
        payload = {
            "procedure_type": spec.procedure_id,
            "procedure_display_name": spec.bundle.procedure_display_name,
            "procedure_display_name_ko": spec.bundle.procedure_display_name_ko,
            "default_phase_id": spec.default_phase_id,
            "phases": [
                {
                    "ordinal": ordinal,
                    "phase_id": phase.id,
                    "display_name": phase.display_name,
                    "display_name_ko": phase.display_name_ko,
                    "phase_kind": (
                        "normal" if phase.id in set(spec.normal_phase_ids) else "interrupt"
                    ),
                    "possible_next_phase_ids": list(phase.possible_next),
                    "expected_instrument_ids": list(phase.expected_instruments),
                }
                for ordinal, phase in enumerate(spec.bundle.phases, start=1)
            ],
            "instruments": [
                {
                    "instrument_id": instrument.id,
                    "display_name": instrument.display_name,
                    "display_name_ko": instrument.display_name_ko,
                    "aliases": list(instrument.aliases),
                    "category": instrument.category,
                    "inventory_count": max(0, instrument.inventory_count),
                    "requestable": instrument.requestable,
                    "role": instrument.role,
                }
                for instrument in spec.bundle.instruments
            ],
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(canonical).hexdigest()}"

    @staticmethod
    def _monotonic() -> float:
        return time.monotonic()

    def _cache(
        self,
        message: Any,
        *,
        sequence: int = 0,
        received_stamp: Any = None,
    ) -> CachedMessage:
        return CachedMessage(
            message=message,
            received_monotonic_sec=self._monotonic(),
            sequence=sequence,
            received_stamp=received_stamp,
        )

    def _clear_run_scoped_state_locked(self) -> None:
        """Drop data that must never replay across procedure-run boundaries."""

        self._vlm_result = None
        self._skill_status = None
        self._bed_robot_arm_status = None
        self._bed_robot_arm_revision = None
        self._bed_robot_arm_source_stamp_sec = None
        self._speech_text = None
        self._speech_sequence = 0

    @staticmethod
    def _positive_source_stamp_sec(stamp: Any) -> float | None:
        """Return a usable source timestamp for cross-run late-event rejection."""

        try:
            stamp_sec = stamp_to_seconds(stamp)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(stamp_sec) or stamp_sec <= 0.0:
            return None
        return stamp_sec

    def _start_procedure_run_locked(self, world: Any) -> None:
        self._clear_run_scoped_state_locked()
        self._procedure_run_id = str(uuid.uuid4())
        self._procedure_run_start_source_stamp_sec = self._positive_source_stamp_sec(
            getattr(world, "stamp", None)
        )
        self._last_procedure_active = True

    def _end_procedure_run_locked(self) -> None:
        self._clear_run_scoped_state_locked()
        self._procedure_run_id = ""
        self._procedure_run_start_source_stamp_sec = None
        self._last_procedure_active = False

    def _public_world_locked(self, now_monotonic_sec: float) -> tuple[Any | None, bool]:
        """Resolve the current public run and close it when WorldState is stale."""

        cached = self._world
        world = cached.message if cached else None
        fresh = freshness_from_receipt(
            cached.received_monotonic_sec if cached else None,
            now_monotonic_sec,
            self._world_stale_after_sec,
        ).fresh
        active = bool(
            world is not None
            and fresh
            and getattr(world, "running", False)
            and self._last_procedure_active
            and str(getattr(world, "procedure_id", "")).strip()
            == self._procedure_spec.procedure_id
        )
        if not active and self._last_procedure_active:
            # A stale world ends the public run just like an explicit
            # running=false transition. A new fresh running WorldState starts
            # a new run with a new opaque identifier in _on_world.
            self._end_procedure_run_locked()
        return world, active

    def _on_world(self, message: WorldState) -> None:
        with self._lock:
            cached = self._cache(message)
            reported_procedure = str(getattr(message, "procedure_id", "")).strip()
            mismatch = bool(
                getattr(message, "running", False)
                and reported_procedure != self._procedure_spec.procedure_id
            )
            if mismatch and not self._procedure_mismatch:
                self.get_logger().error(
                    "public gateway rejected WorldState procedure/catalog mismatch: "
                    f"world={reported_procedure!r} catalog={self._procedure_spec.procedure_id!r}"
                )
            self._procedure_mismatch = mismatch
            requested_active = bool(getattr(message, "running", False) and not mismatch)
            if requested_active and not self._last_procedure_active:
                # Establish run identity before any immediately following event
                # can be assigned a sequence number for this session.
                self._start_procedure_run_locked(message)
            elif not requested_active and self._last_procedure_active:
                self._end_procedure_run_locked()
            self._world = cached

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

    def _on_speech_text(self, message: String) -> None:
        text = str(message.data).strip()
        if not text or len(text) > _MAX_SPEECH_TEXT_CHARS:
            self.get_logger().warning("ignored empty or oversized public speech text")
            return
        with self._lock:
            self._speech_sequence += 1
            self._speech_text = self._cache(
                message,
                sequence=self._speech_sequence,
                received_stamp=self.get_clock().now().to_msg(),
            )

    def _on_asr_status(self, message: String) -> None:
        raw = str(message.data)
        if len(raw.encode("utf-8")) > _MAX_ASR_STATUS_BYTES:
            self.get_logger().warning("ignored oversized ASR runtime status")
            return
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            self.get_logger().warning("ignored malformed ASR runtime status JSON")
            return
        if not isinstance(payload, dict) or payload.get("schema") != "taskplanner.asr.status.v1":
            self.get_logger().warning("ignored unsupported ASR runtime status schema")
            return
        asr = payload.get("asr")
        if not isinstance(asr, dict):
            self.get_logger().warning("ignored ASR runtime status without an asr object")
            return
        with self._lock:
            self._asr_status = self._cache(payload)

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
        """Publish active-procedure events immediately; snapshots are rate-limited.

        The latest fresh WorldState is the authority gate.  The first active
        WorldState source timestamp is also a lower bound for stamped events,
        preventing delayed events from the prior run from being relabeled with
        the new run identity.

        TwinEvent has no source run identifier. If either source uses a zero or
        otherwise unusable timestamp, or its clock resets so an old stamp looks
        newer than the current run start, a delayed event cannot be
        distinguished from a current event. In that unavoidable compatibility
        case this method can only apply the fresh-active receipt-time gate;
        consumers can still distinguish the accepted event using the
        identifiers embedded in SurgeryEvent.
        """

        try:
            projection = project_event(message)
            now_monotonic_sec = self._monotonic()
            with self._lock:
                world, active = self._public_world_locked(now_monotonic_sec)
                if not active or world is None or not self._procedure_run_id:
                    return

                event_stamp_sec = self._positive_source_stamp_sec(projection.stamp)
                run_start_stamp_sec = self._procedure_run_start_source_stamp_sec
                if (
                    event_stamp_sec is not None
                    and run_start_stamp_sec is not None
                    and event_stamp_sec < run_start_stamp_sec
                ):
                    self.get_logger().warning(
                        "ignored public event older than current procedure run: "
                        f"event_stamp={event_stamp_sec:.9f} "
                        f"run_start_stamp={run_start_stamp_sec:.9f}"
                    )
                    return

                self._event_sequence += 1
                public_event = SurgeryEvent()
                public_event.stamp = self._stamp_or_now(projection.stamp)
                public_event.sequence = self._event_sequence
                public_event.schema_version = SCHEMA_VERSION
                public_event.catalog_version = self._catalog_version
                public_event.gateway_instance_id = self._gateway_instance_id
                public_event.procedure_run_id = self._procedure_run_id
                public_event.procedure_type = str(
                    getattr(world, "procedure_id", "")
                ).strip()
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
                # Keep acceptance, run metadata capture, and publication under
                # the lifecycle lock so an active->idle/new-run callback cannot
                # interleave and relabel the event.
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
    def _public_asr_state(value: Any) -> str:
        state = str(value or "").strip().casefold()
        return {
            "unavailable": SpeechRecognitionState.STATE_UNAVAILABLE,
            "stopped": SpeechRecognitionState.STATE_IDLE,
            "idle": SpeechRecognitionState.STATE_IDLE,
            "starting": SpeechRecognitionState.STATE_PROCESSING,
            "stopping": SpeechRecognitionState.STATE_PROCESSING,
            "connecting": SpeechRecognitionState.STATE_PROCESSING,
            "recording": SpeechRecognitionState.STATE_LISTENING,
            "listening": SpeechRecognitionState.STATE_LISTENING,
            "running": SpeechRecognitionState.STATE_LISTENING,
            "connected": SpeechRecognitionState.STATE_READY,
            "ready": SpeechRecognitionState.STATE_READY,
            "error": SpeechRecognitionState.STATE_ERROR,
        }.get(state, SpeechRecognitionState.STATE_UNAVAILABLE)

    def _speech_message(
        self,
        *,
        stamp: Any,
        revision: int,
        procedure_type: str,
        procedure_active: bool,
    ) -> SpeechRecognitionState:
        message = SpeechRecognitionState()
        self._snapshot_metadata(
            message,
            stamp=stamp,
            revision=revision,
            procedure_type=procedure_type,
            procedure_active=procedure_active,
        )
        message.state = SpeechRecognitionState.STATE_UNAVAILABLE
        message.source = "taskplanner_asr"
        message.evidence_status = GATEWAY_OBSERVED
        if not procedure_active:
            return message

        with self._lock:
            speech = self._speech_text
            status = self._asr_status
        if status is None or not freshness_from_receipt(
            status.received_monotonic_sec,
            self._monotonic(),
            self._health_stale_after_sec,
        ).fresh:
            return message
        asr = status.message.get("asr", {})
        message.connected = bool(asr.get("connected", False))
        message.state = self._public_asr_state(asr.get("state"))
        message.available = bool(asr.get("available", False)) and message.connected
        speech_fresh = bool(
            speech
            and freshness_from_receipt(
                speech.received_monotonic_sec,
                self._monotonic(),
                self._health_stale_after_sec,
            ).fresh
        )
        if speech is None or not speech_fresh or not message.available:
            return message

        text = str(speech.message.data).strip()
        if not text:
            return message
        message.utterance_sequence = speech.sequence
        message.utterance_stamp = speech.received_stamp or stamp
        if self._publish_free_text:
            message.text = text
        else:
            message.evidence_status = GATEWAY_OBSERVED_REDACTED

        finals = asr.get("finals", [])
        if isinstance(finals, list):
            for row in reversed(finals[-32:]):
                if not isinstance(row, dict) or str(row.get("text", "")).strip() != text:
                    continue
                latency = row.get("response_latency_ms")
                latency_basis = str(row.get("latency_basis", "")).strip()
                if (
                    isinstance(latency, (int, float))
                    and not isinstance(latency, bool)
                    and math.isfinite(latency)
                    and latency >= 0
                    and latency_basis in _PUBLIC_LATENCY_BASES
                ):
                    message.latency_available = True
                    message.response_latency_ms = float(latency)
                    message.latency_basis = latency_basis
                break
        return message

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
        confidence = finite_probability(projection.confidence)
        message.confidence = confidence if confidence is not None else 0.0
        message.evidence_status = (
            projection.evidence_status if confidence is not None else UNKNOWN
        )
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
        progress = finite_probability(projection.progress)
        message.progress = progress if progress is not None else 0.0
        message.reason_code = projection.reason_code
        message.evidence_status = (
            projection.evidence_status if progress is not None else UNKNOWN
        )
        return message

    @staticmethod
    def _to_public_prediction(
        projection: ToolPredictionProjection,
    ) -> ToolPrediction | None:
        confidence = finite_probability(projection.confidence)
        stability_sec = finite_nonnegative(projection.stability_sec)
        if confidence is None or stability_sec is None:
            return None
        message = ToolPrediction()
        message.stamp = projection.stamp
        message.rank = projection.rank
        message.instrument_id = projection.instrument_id
        message.instance_id = projection.instance_id
        message.confidence = confidence
        message.stability_sec = stability_sec
        message.source = projection.source
        message.evidence_status = projection.evidence_status
        return message

    @staticmethod
    def _to_public_end_effector(
        projection: RobotEndEffectorProjection,
    ) -> RobotEndEffectorState:
        message = RobotEndEffectorState()
        message.stamp = projection.stamp
        message.robot_id = projection.robot_id
        message.end_effector_id = projection.end_effector_id
        confidence = finite_probability(projection.confidence)
        state = projection.state.casefold()
        valid_state = state in {
            RobotEndEffectorState.STATE_EMPTY,
            RobotEndEffectorState.STATE_HOLDING,
        }
        if confidence is None or not valid_state:
            # End effectors are configured rows, so retain the row but remove
            # the possession claim instead of dropping it from the snapshot.
            message.state = RobotEndEffectorState.STATE_UNKNOWN
            message.instrument_id = ""
            message.instance_id = ""
            message.confidence = 0.0
            message.evidence_status = UNKNOWN
            return message
        message.state = state
        message.instrument_id = projection.instrument_id
        message.instance_id = projection.instance_id
        message.confidence = confidence
        message.evidence_status = projection.evidence_status
        return message

    def _snapshot_metadata(
        self,
        message: Any,
        *,
        stamp: Any,
        revision: int,
        procedure_type: str,
        procedure_active: bool,
    ) -> None:
        message.stamp = stamp
        message.revision = revision
        message.schema_version = SCHEMA_VERSION
        if hasattr(message, "catalog_version"):
            message.catalog_version = self._catalog_version
        message.gateway_instance_id = self._gateway_instance_id
        message.procedure_run_id = self._procedure_run_id if procedure_active else ""
        message.procedure_type = procedure_type
        message.procedure_active = procedure_active

    def _gateway_info_message(
        self,
        *,
        stamp: Any,
        revision: int,
        procedure_type: str,
        procedure_active: bool,
    ) -> GatewayInfo:
        message = GatewayInfo()
        self._snapshot_metadata(
            message,
            stamp=stamp,
            revision=revision,
            procedure_type=procedure_type,
            procedure_active=procedure_active,
        )
        message.interface_version = INTERFACE_VERSION
        return message

    def _catalog_message(
        self,
        *,
        stamp: Any,
        revision: int,
        procedure_active: bool,
    ) -> ProcedureCatalog:
        spec = self._procedure_spec
        message = ProcedureCatalog()
        self._snapshot_metadata(
            message,
            stamp=stamp,
            revision=revision,
            procedure_type=spec.procedure_id,
            procedure_active=procedure_active,
        )
        message.procedure_display_name = spec.bundle.procedure_display_name
        message.procedure_display_name_ko = spec.bundle.procedure_display_name_ko
        message.default_phase_id = spec.default_phase_id
        normal_phase_ids = set(spec.normal_phase_ids)
        for ordinal, phase in enumerate(spec.bundle.phases, start=1):
            entry = PhaseCatalogEntry()
            entry.ordinal = ordinal
            entry.phase_id = phase.id
            entry.display_name = phase.display_name
            entry.display_name_ko = phase.display_name_ko
            entry.phase_kind = (
                PhaseCatalogEntry.KIND_NORMAL
                if phase.id in normal_phase_ids
                else PhaseCatalogEntry.KIND_INTERRUPT
            )
            entry.possible_next_phase_ids = list(phase.possible_next)
            entry.expected_instrument_ids = list(phase.expected_instruments)
            message.phases.append(entry)
        for instrument in spec.bundle.instruments:
            entry = InstrumentCatalogEntry()
            entry.instrument_id = instrument.id
            entry.display_name = instrument.display_name
            entry.display_name_ko = instrument.display_name_ko
            entry.aliases = list(instrument.aliases)
            entry.category = instrument.category
            entry.inventory_count = max(0, instrument.inventory_count)
            entry.requestable = instrument.requestable
            entry.role = instrument.role
            message.instruments.append(entry)
        return message

    def _to_public_clinical(
        self, projection: ClinicalObservationProjection, sequence: int
    ) -> ClinicalObservation:
        message = ClinicalObservation()
        message.stamp = projection.stamp
        message.sequence = sequence
        message.source = projection.source
        if self._publish_free_text:
            message.summary = projection.summary
        elif projection.summary and projection.evidence_status == MODEL_OBSERVED:
            message.evidence_status = MODEL_OBSERVED_REDACTED
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
        if not message.evidence_status:
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
        if self._procedure_mismatch:
            errors.append("procedure_catalog_mismatch")

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
                world, active = self._public_world_locked(now_monotonic_sec)
                vlm_result = self._vlm_result if active else None

            stamp = self.get_clock().now().to_msg()
            procedure_type = (
                str(getattr(world, "procedure_id", "")).strip()
                if active
                else self._procedure_spec.procedure_id
            )
            self._gateway_info_pub.publish(
                self._gateway_info_message(
                    stamp=stamp,
                    revision=revision,
                    procedure_type=procedure_type,
                    procedure_active=active,
                )
            )
            self._catalog_pub.publish(
                self._catalog_message(
                    stamp=stamp,
                    revision=revision,
                    procedure_active=active,
                )
            )

            if active:
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
                context.procedure_active = False
                context.phase_uncertain = True
                context.evidence_status = "UNKNOWN"
                self._context_pub.publish(context)
                instruments = InstrumentStateArray()
                instruments.stamp = context.stamp
                instruments.revision = revision
                self._instruments_pub.publish(instruments)

            robots = RobotStateArray()
            robots.stamp = stamp
            robots.revision = revision
            if active:
                robots.robots = [
                    self._to_public_robot(projection)
                    for projection in self._merged_robot_projections(fresh)
                ]
            self._robots_pub.publish(robots)

            clinical = ClinicalObservationArray()
            clinical.stamp = stamp
            clinical.revision = revision
            if active and vlm_result is not None and fresh["vlm_result"].fresh:
                projection = project_clinical_observation(vlm_result.message)
                clinical.stamp = self._stamp_or_now(projection.stamp)
                clinical.observations = [
                    self._to_public_clinical(projection, vlm_result.sequence)
                ]
            self._clinical_pub.publish(clinical)

            predictions = ToolPredictionArray()
            self._snapshot_metadata(
                predictions,
                stamp=stamp,
                revision=revision,
                procedure_type=procedure_type,
                procedure_active=active,
            )
            if active:
                for projection in project_tool_predictions(world):
                    public_prediction = self._to_public_prediction(projection)
                    if public_prediction is not None:
                        predictions.predictions.append(public_prediction)
            self._prediction_pub.publish(predictions)

            end_effectors = RobotEndEffectorStateArray()
            self._snapshot_metadata(
                end_effectors,
                stamp=stamp,
                revision=revision,
                procedure_type=procedure_type,
                procedure_active=active,
            )
            if active:
                end_effectors.end_effectors = [
                    self._to_public_end_effector(projection)
                    for projection in project_robot_end_effectors(world)
                ]
            self._end_effectors_pub.publish(end_effectors)

            self._speech_pub.publish(
                self._speech_message(
                    stamp=stamp,
                    revision=revision,
                    procedure_type=procedure_type,
                    procedure_active=active,
                )
            )

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
