"""Manual probe for the direct CAM4 hand-handover signal path."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import math
import os
import subprocess
import sys
import time

from hand_keypoint_interfaces.msg import (
    HandFacing,
    HandFacingArray,
    HandGesture,
    HandGestureArray,
)
from procedure_spec import get_default_spec_dir, load_bundle
import rclpy
from rclpy.parameter import Parameter
from rclpy.parameter_client import AsyncParameterClient
from rclpy.qos import (
    DurabilityPolicy,
    QoSProfile,
    ReliabilityPolicy,
    qos_profile_sensor_data,
)
from std_msgs.msg import String
from surgical_perception_msgs.msg import ToolObservation2DArray
from surgical_msgs.msg import (
    BTDecision,
    ExecutionTrace,
    PhaseEvidence,
    SkillCommand,
    SkillStatus,
    TwinEvent,
    VLMResult,
)

from .smoke_test import ManagedProcess, SmokeHarness


HAND_FRAME_ID = "cam_4_color_optical_frame"
HAND_GESTURE_MODEL_NAME = "VIPLab Top-View Landmark Gesture Classifier"
HAND_GESTURE_MODEL_VERSION = "landmark-geometry-v2-world-closed"
HAND_GESTURE_MODEL_SHA256 = (
    "258ed1676863df9317fb084a446a2ae9645c1f4acc468a4c4ad92979cf0ef821"
)
HAND_FACING_ESTIMATOR_NAME = "VIPLab CAM4 Depth Palm-Facing Estimator"
HAND_FACING_ESTIMATOR_VERSION = "depth-palm-normal-v1"
HAND_FACING_SPEC_SHA256 = (
    "45c61773790b8510f373bd9f0940ce1189567e7f4e74ff3c0d6c5b5ee3785b37"
)
HAND_CALIBRATION_VERSION = "cam4_live_aprilgrid_depth_workplane_20260822"
HAND_MAPPING_VERSION = "cam4_forced_right_camera_constraint_v1_pending_live_check"
HAND_REQUEST_SAMPLE_COUNT = 8
HAND_REQUEST_DURATION_SEC = 0.56
HAND_RELEASE_SAMPLE_COUNT = 10
HAND_RELEASE_DURATION_SEC = 0.70
VIRTUAL_TOOL_HANDOVER_ENDPOINT = "/integration/virtual/surgery/tool_handover"
VIRTUAL_ENDPOINT_SOURCE = "virtual"
HANDOVER_ACTIONS = frozenset({"direct_handover", "pick_up_and_handover"})
PROBE_CAM3_TOOL_OBSERVATIONS_TOPIC = (
    "/taskplanner/probe/rfdetr/cam_3/tool/observations"
)
PROBE_CAM4_TOOL_OBSERVATIONS_TOPIC = (
    "/taskplanner/probe/rfdetr/cam_4/tool/observations"
)


def _stamp_to_sec(stamp) -> float:
    return float(stamp.sec) + float(stamp.nanosec) / 1_000_000_000.0


@dataclass
class ProbeResult:
    bundle: str
    implicit_request_tool_id: str
    initial_preposition_tool_id: str
    replacement_preposition_tool_id: str
    explicit_request_tool_id: str
    visual_evidence_did_not_create_explicit_request: bool
    implicit_request_tool_was_unspecified: bool
    implicit_handover_completed: bool
    initial_preposition_completed: bool
    prediction_evidence_withdrawal_returned: bool
    wrong_preposition_returned: bool
    replacement_preposition_completed: bool
    voice_correction_used_put_down_and_handover: bool
    explicit_handover_completed: bool
    implicit_request_to_handover_sec: float
    prediction_to_preposition_sec: float
    prediction_evidence_withdrawal_release_sec: float
    wrong_preposition_release_sec: float
    replacement_prediction_to_preposition_sec: float
    invariant_violation_count: int
    hand_episode_count: int
    hand_episode_tool_ids: list[str]
    hand_episode_durations_sec: list[float]


@dataclass(frozen=True)
class HandEpisodeObservation:
    procedure_run_id: str
    generation: int
    command_id: str
    action: str
    instrument_id: str
    instrument_instance_id: str
    terminal_reason_code: str


@dataclass(frozen=True)
class HandOnlyProbeResult:
    bundle: str
    input_scope: str
    endpoint_source: str
    endpoint: str
    completed_episode_count: int
    requested_episode_count: int
    procedure_run_ids: list[str]
    command_ids: list[str]
    tool_ids: list[str]
    episode_durations_sec: list[float]
    external_execution_trace_count: int
    invariant_violation_count: int


def _require_hand_topic_injection_allowed(allowed: bool) -> None:
    if not allowed:
        raise RuntimeError(
            "CAM4 hand-topic injection is disabled. Pass the explicit "
            "allow_topic_injection=True opt-in only on an isolated or reviewed "
            "virtual route."
        )


def _positive_int(name: str, value: int, *, minimum: int = 1) -> int:
    result = int(value)
    if result < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {result}")
    return result


def _positive_duration(name: str, value: float) -> float:
    result = float(value)
    if not math.isfinite(result) or result <= 0.0:
        raise ValueError(f"{name} must be > 0, got {result}")
    return result


def _probe_domain_nodes(timeout_sec: float = 4.0) -> list[str]:
    """Discover pre-existing nodes without consulting the ROS daemon cache."""

    try:
        completed = subprocess.run(
            ["ros2", "node", "list", "--no-daemon"],
            check=False,
            capture_output=True,
            text=True,
            timeout=max(0.5, float(timeout_sec)),
            env=os.environ.copy(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"could not inspect the selected ROS domain: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(
            "could not inspect the selected ROS domain"
            + (f": {detail}" if detail else "")
        )
    return sorted(
        {
            line.strip()
            for line in completed.stdout.splitlines()
            if line.strip().startswith("/")
        }
    )


def _build_hand_observation_pair(stamp, *, request_pose: bool):
    """Build one exact-stamp typed hand pair.

    The release sample is a valid Right/Closed_Fist/PALM_UP observation, not an
    UNKNOWN or a stream gap. This is important because only a continuously
    observed non-request pose re-arms a direct-hand episode.
    """

    gesture_hand = HandGesture()
    gesture_hand.hand_index = 0
    gesture_hand.has_handedness = True
    gesture_hand.handedness_label = "Right"
    gesture_hand.handedness_score = 0.99
    gesture_hand.has_classification = True
    gesture_hand.category_name = "Open_Palm" if request_pose else "Closed_Fist"
    gesture_hand.score = 0.98

    gesture = HandGestureArray()
    gesture.header.stamp = stamp
    gesture.header.frame_id = HAND_FRAME_ID
    gesture.model_name = HAND_GESTURE_MODEL_NAME
    gesture.model_version = HAND_GESTURE_MODEL_VERSION
    gesture.model_asset_sha256 = HAND_GESTURE_MODEL_SHA256
    gesture.supported_gestures = [
        "Closed_Fist",
        "Open_Palm",
        "Pointing_Up",
        "Thumb_Down",
        "Thumb_Up",
        "Victory",
        "ILoveYou",
    ]
    gesture.rejection_category = "None"
    gesture.hands = [gesture_hand]

    facing_hand = HandFacing()
    facing_hand.hand_index = 0
    facing_hand.has_handedness = True
    facing_hand.handedness_label = "Right"
    facing_hand.handedness_score = 0.99
    facing_hand.has_facing = True
    facing_hand.facing_label = "PALM_UP"
    facing_hand.palm_up_score = 0.95

    facing = HandFacingArray()
    # Assign the complete Header so stamp and frame_id cannot drift between the
    # two independently published topics.
    facing.header = gesture.header
    facing.estimator_name = HAND_FACING_ESTIMATOR_NAME
    facing.estimator_version = HAND_FACING_ESTIMATOR_VERSION
    facing.estimator_spec_sha256 = HAND_FACING_SPEC_SHA256
    facing.calibration_version = HAND_CALIBRATION_VERSION
    facing.handedness_mapping_version = HAND_MAPPING_VERSION
    facing.supported_facings = ["PALM_UP", "PALM_DOWN", "EDGE"]
    facing.rejection_category = "UNKNOWN"
    facing.hands = [facing_hand]
    return gesture, facing


def _build_synthetic_hand_health() -> String:
    health = String()
    health.data = json.dumps(
        {
            "schema": "pnu.hand_keypoint_health.v1",
            "ready": True,
            "rgb_ready": True,
            "depth_ready": True,
            "depth_alignment_validated": True,
            "depth_registration_ready": True,
            "depth_registration_degraded": False,
            "depth_registration_backend_active": "cuda_cabi_v1",
            "model_ready": True,
            "hand_inference_ready": True,
            "gesture_model_ready": True,
            "gesture_inference_ready": True,
            "palm_facing_observation_ready": True,
            "palm_facing_mapping_verified": True,
            "handedness_policy": "forced_camera_constraint",
            "forced_handedness_label": "Right",
            "gesture_model_version": HAND_GESTURE_MODEL_VERSION,
            "gesture_model_asset_sha256": HAND_GESTURE_MODEL_SHA256,
            "palm_facing_estimator": {
                "name": HAND_FACING_ESTIMATOR_NAME,
                "version": HAND_FACING_ESTIMATOR_VERSION,
                "spec_sha256": HAND_FACING_SPEC_SHA256,
                "calibration_version": HAND_CALIBRATION_VERSION,
                "handedness_mapping_version": HAND_MAPPING_VERSION,
            },
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return health


def _build_synthetic_rfdetr_readiness_frame(
    stamp,
    *,
    view: str,
    sequence: int,
) -> ToolObservation2DArray:
    """Build fresh typed no-detection evidence for the isolated probe only."""

    if view not in {"cam_3", "cam_4"}:
        raise ValueError(f"unsupported probe RF-DETR view: {view!r}")
    frame = ToolObservation2DArray()
    frame.header.stamp = stamp
    frame.header.frame_id = f"{view}_color_optical_frame"
    frame.sequence = int(sequence)
    frame.schema_version = "pnu.tool_observation_2d.v1"
    frame.observation_id = f"manual-probe-{view}-{int(sequence)}"
    frame.view = view
    frame.image_width = 1280
    frame.image_height = 720
    frame.model_version = "manual-probe-typed-empty-v1"
    frame.ontology_version = "taskplanner-tool-catalog-v1"
    frame.instances = []
    return frame


def _validate_virtual_route_payload(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise RuntimeError("Execution-route state is not a JSON object.")
    if payload.get("schema") != "taskplanner.execution_route_state.v1":
        raise RuntimeError("Execution-route state schema is missing or unsupported.")
    if payload.get("selected_source") != VIRTUAL_ENDPOINT_SOURCE:
        raise RuntimeError(
            "Hand probe refuses a non-virtual tool route: "
            f"selected_source={payload.get('selected_source')!r}"
        )
    if payload.get("tool_handover_endpoint") != VIRTUAL_TOOL_HANDOVER_ENDPOINT:
        raise RuntimeError(
            "Hand probe refuses an unexpected tool endpoint: "
            f"{payload.get('tool_handover_endpoint')!r}"
        )
    if payload.get("action_server_ready") is not True:
        raise RuntimeError("Virtual tool-handover Action server is not ready.")
    return dict(payload)


def _probe_runtime_command(args: argparse.Namespace, spec_dir) -> list[str]:
    """Return a launch command pinned to the isolated virtual endpoint family."""

    return [
        "ros2",
        "launch",
        "bringup",
        "taskplanner_mock.launch.py",
        f"spec_dir:={spec_dir}",
        f"default_bundle:={args.spec_name}",
        "enable_rosbridge:=false",
        "execution_backend:=mock",
        "execution_contract:=direct",
        "robot_endpoint_source:=virtual",
        "retraction_endpoint_source:=virtual",
        "enable_runtime_route_control:=false",
        # Virtual dispatch is lease-gated too.  Without the matching publisher
        # the Action bridge correctly rejects every probe command as
        # integration_readiness_lease_missing.
        "require_integration_preflight:=true",
        "preflight_require_rfdetr_tool_observations:=true",
        f"cam3_tool_observations_topic:={PROBE_CAM3_TOOL_OBSERVATIONS_TOPIC}",
        f"cam4_tool_observations_topic:={PROBE_CAM4_TOOL_OBSERVATIONS_TOPIC}",
        f"vlm_mode:={args.vlm_mode}",
        f"vlm_response_mode:={args.vlm_response_mode}",
        f"vlm_base_url:={args.vlm_base_url}",
        f"vlm_model_id:={args.vlm_model_id}",
        f"surgeon_actor_mode:={args.surgeon_actor_mode}",
    ]


class ManualProbeHarness(SmokeHarness):
    def __init__(self) -> None:
        self._detailed_skill_statuses: list[
            tuple[str, str, str, bool, str, str]
        ] = []
        super().__init__()
        self._hand_gesture_pub = self.create_publisher(
            HandGestureArray, "/perception/cam_4/hand/gestures", 20
        )
        self._hand_facing_pub = self.create_publisher(
            HandFacingArray, "/perception/cam_4/hand/facing", 20
        )
        hand_health_qos = QoSProfile(
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        self._hand_health_pub = self.create_publisher(
            String, "/perception/cam_4/hand/health", hand_health_qos
        )
        self._probe_cam3_tool_observations_pub = self.create_publisher(
            ToolObservation2DArray,
            PROBE_CAM3_TOOL_OBSERVATIONS_TOPIC,
            qos_profile_sensor_data,
        )
        self._probe_cam4_tool_observations_pub = self.create_publisher(
            ToolObservation2DArray,
            PROBE_CAM4_TOOL_OBSERVATIONS_TOPIC,
            qos_profile_sensor_data,
        )
        self._synthetic_rfdetr_readiness_enabled = False
        self._synthetic_rfdetr_sequence = 0
        self._synthetic_rfdetr_timer = self.create_timer(
            0.2,
            self._publish_synthetic_rfdetr_readiness,
        )
        self._parameter_client = AsyncParameterClient(self, "/surgeon_actor")
        self._decision_log: list[BTDecision] = []
        self._event_log: list[TwinEvent] = []
        self._skill_command_log: list[SkillCommand] = []
        self._execution_trace_log: list[ExecutionTrace] = []
        self._surgeon_request_log: list[tuple[float, str, str]] = []
        self._latest_execution_route: dict[str, object] | None = None
        self._vlm_result_pub = self.create_publisher(VLMResult, "/vlm/result", 20)
        self._phase_evidence_pub = self.create_publisher(
            PhaseEvidence,
            "/vlm/phase_evidence",
            20,
        )
        self.create_subscription(BTDecision, "/bt/decision", self._on_probe_decision, 20)
        self.create_subscription(SkillCommand, "/bt/skill_command", self._skill_command_log.append, 20)
        self.create_subscription(TwinEvent, "/skill/events", self._on_probe_event, 50)
        self.create_subscription(
            ExecutionTrace,
            "/surgery/execution_trace",
            self._execution_trace_log.append,
            50,
        )
        route_qos = QoSProfile(
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        self.create_subscription(
            String,
            "/integration/execution_route/state",
            self._on_execution_route_state,
            route_qos,
        )

    def _on_probe_decision(self, msg: BTDecision) -> None:
        self._decision_log.append(msg)

    def _on_probe_event(self, msg: TwinEvent) -> None:
        self._event_log.append(msg)

    def _on_skill_status(self, msg: SkillStatus) -> None:  # type: ignore[override]
        """Retain the controller reason without changing the shared smoke harness."""

        super()._on_skill_status(msg)
        record = (
            msg.command_id,
            msg.action,
            msg.state,
            bool(msg.success),
            msg.message,
            msg.instrument_id,
        )
        if record not in self._detailed_skill_statuses:
            self._detailed_skill_statuses.append(record)

    def _on_execution_route_state(self, msg: String) -> None:
        try:
            payload = json.loads(str(msg.data))
        except (TypeError, ValueError, json.JSONDecodeError):
            return
        if isinstance(payload, dict):
            self._latest_execution_route = payload

    def _on_surgeon_request(self, msg):  # type: ignore[override]
        super()._on_surgeon_request(msg)
        self._surgeon_request_log.append(
            (_stamp_to_sec(msg.stamp), msg.event_type, msg.requested_tool)
        )

    def wait_for_probe_subscribers(self, timeout_sec: float = 10.0) -> None:
        self.wait_until(
            lambda: self._phase_evidence_pub.get_subscription_count() > 0
            and self._vlm_result_pub.get_subscription_count() > 0
            and self._hand_gesture_pub.get_subscription_count() > 0
            and self._hand_facing_pub.get_subscription_count() > 0
            and self._hand_health_pub.get_subscription_count() > 0,
            timeout_sec,
            "probe evidence subscribers",
        )

    def assert_virtual_execution_route(self, timeout_sec: float = 10.0) -> dict[str, object]:
        self.wait_until(
            lambda: self._latest_execution_route is not None
            and self._latest_execution_route.get("selected_source")
            == VIRTUAL_ENDPOINT_SOURCE
            and self._latest_execution_route.get("tool_handover_endpoint")
            == VIRTUAL_TOOL_HANDOVER_ENDPOINT
            and self._latest_execution_route.get("action_server_ready") is True,
            timeout_sec,
            "ready virtual execution-route state",
        )
        return _validate_virtual_route_payload(self._latest_execution_route)

    def set_random_voice_enabled(self, enabled: bool) -> bool:
        if not self._parameter_client.services_are_ready():
            if not self._parameter_client.wait_for_services(timeout_sec=3.0):
                return False
        future = self._parameter_client.set_parameters(
            [Parameter(name="random_voice_enabled", value=enabled)]
        )
        rclpy.spin_until_future_complete(self, future, timeout_sec=10.0)
        response = future.result()
        if response is None:
            return False
        results = getattr(response, "results", [])
        if not all(result.successful for result in results):
            reasons = "; ".join(result.reason or "unknown" for result in results)
            raise RuntimeError(f"Failed to set random_voice_enabled: {reasons}")
        return bool(results)

    def choose_probe_tool(self) -> str:
        if self._latest_world is None:
            raise RuntimeError("No world state available yet for probe.")
        expected = set(self._latest_world.expected_instruments)

        def requestable(instrument) -> bool:
            return (
                not instrument.contaminated
                and instrument.lifecycle_stage in {"home_rack", "returned_home", "prepositioned_right"}
                and instrument.owner in {"", "none", "robot_right_hand"}
                and instrument.location_type not in {"mayo_reuse_zone", "mayo_recovery_zone", "surgical_field", "surgeon_hand"}
            )

        instruments = list(self._latest_world.instrument_states)

        for instrument in instruments:
            if requestable(instrument) and instrument.instrument_id == self._latest_world.surgeon_request_tool:
                return instrument.instrument_id
        for instrument in instruments:
            if requestable(instrument) and instrument.instrument_id in expected:
                return instrument.instrument_id
        for instrument in instruments:
            if requestable(instrument) and instrument.instrument_id == self._latest_world.prepositioned_tool:
                return instrument.instrument_id

        for instrument in instruments:
            if requestable(instrument) and instrument.instrument_id not in expected:
                return instrument.instrument_id
        for instrument in instruments:
            if requestable(instrument):
                return instrument.instrument_id
        raise RuntimeError("Could not find a requestable tool for the current phase.")

    def choose_recovery_probe_tool(self, preferred_tool: str) -> str:
        if self._latest_world is None:
            raise RuntimeError("No world state available for recovery probe.")
        recoverable = [
            instrument.instrument_id
            for instrument in self._latest_world.instrument_states
            if instrument.lifecycle_stage in {"surgeon_owned", "mayo_recovery"}
        ]
        if preferred_tool in recoverable:
            return preferred_tool
        if recoverable:
            return recoverable[0]
        raise RuntimeError("Could not find a recoverable surgeon tool for the return probe.")

    def choose_override_probe_tool(self, excluded_tool: str) -> str:
        if self._latest_world is None:
            raise RuntimeError("No world state available for override probe.")
        expected = set(self._latest_world.expected_instruments)

        def requestable(instrument) -> bool:
            return (
                instrument.instrument_id != excluded_tool
                and not instrument.contaminated
                and instrument.lifecycle_stage in {"home_rack", "returned_home"}
                and instrument.owner in {"", "none"}
                and instrument.location_type not in {"mayo_reuse_zone", "mayo_recovery_zone", "surgical_field", "surgeon_hand"}
            )

        instruments = list(self._latest_world.instrument_states)
        for instrument in instruments:
            if requestable(instrument) and instrument.instrument_id not in expected:
                return instrument.instrument_id
        for instrument in instruments:
            if requestable(instrument):
                return instrument.instrument_id
        raise RuntimeError("Could not find an alternate explicit-request tool for override probe.")

    def publish_synthetic_hand_health(
        self,
        *,
        allow_topic_injection: bool = False,
    ) -> None:
        _require_hand_topic_injection_allowed(allow_topic_injection)
        self._hand_health_pub.publish(_build_synthetic_hand_health())

    def enable_synthetic_rfdetr_readiness(
        self,
        *,
        allow_topic_injection: bool = False,
    ) -> None:
        _require_hand_topic_injection_allowed(allow_topic_injection)
        self._synthetic_rfdetr_readiness_enabled = True

    def _publish_synthetic_rfdetr_readiness(self) -> None:
        if not self._synthetic_rfdetr_readiness_enabled:
            return
        self._synthetic_rfdetr_sequence += 1
        stamp = self.get_clock().now().to_msg()
        self._probe_cam3_tool_observations_pub.publish(
            _build_synthetic_rfdetr_readiness_frame(
                stamp,
                view="cam_3",
                sequence=self._synthetic_rfdetr_sequence,
            )
        )
        self._probe_cam4_tool_observations_pub.publish(
            _build_synthetic_rfdetr_readiness_frame(
                stamp,
                view="cam_4",
                sequence=self._synthetic_rfdetr_sequence,
            )
        )

    def _spin_hand_interval(self, duration_sec: float) -> None:
        deadline = time.monotonic() + max(0.0, float(duration_sec))
        while time.monotonic() < deadline:
            rclpy.spin_once(
                self,
                timeout_sec=min(0.05, max(0.0, deadline - time.monotonic())),
            )

    def _emit_hand_observation_sequence(
        self,
        *,
        request_pose: bool,
        sample_count: int,
        duration_sec: float,
        allow_topic_injection: bool = False,
    ) -> None:
        _require_hand_topic_injection_allowed(allow_topic_injection)
        count = _positive_int("sample_count", sample_count, minimum=2)
        duration = _positive_duration("duration_sec", duration_sec)
        interval_sec = duration / float(count - 1)
        for index in range(count):
            stamp = self.get_clock().now().to_msg()
            gesture, facing = _build_hand_observation_pair(
                stamp,
                request_pose=request_pose,
            )
            self._hand_gesture_pub.publish(gesture)
            self._hand_facing_pub.publish(facing)
            if index + 1 < count:
                self._spin_hand_interval(interval_sec)

    def emit_hand_handover_probe(
        self,
        *,
        allow_topic_injection: bool = False,
        inject_synthetic_health: bool = False,
        sample_count: int = HAND_REQUEST_SAMPLE_COUNT,
        duration_sec: float = HAND_REQUEST_DURATION_SEC,
    ) -> None:
        """Emit one direct-hand request episode using the historical API name."""

        _require_hand_topic_injection_allowed(allow_topic_injection)
        if inject_synthetic_health:
            self.publish_synthetic_hand_health(allow_topic_injection=True)
        self._emit_hand_observation_sequence(
            request_pose=True,
            sample_count=sample_count,
            duration_sec=duration_sec,
            allow_topic_injection=True,
        )

    def emit_hand_handover_release(
        self,
        *,
        allow_topic_injection: bool = False,
        inject_synthetic_health: bool = False,
        sample_count: int = HAND_RELEASE_SAMPLE_COUNT,
        duration_sec: float = HAND_RELEASE_DURATION_SEC,
    ) -> None:
        """Emit a continuous valid non-request pose that re-arms the episode."""

        _require_hand_topic_injection_allowed(allow_topic_injection)
        if inject_synthetic_health:
            self.publish_synthetic_hand_health(allow_topic_injection=True)
        self._emit_hand_observation_sequence(
            request_pose=False,
            sample_count=sample_count,
            duration_sec=duration_sec,
            allow_topic_injection=True,
        )

    def emit_hand_handover_stability_probe(
        self,
        *,
        iterations: int = 1,
        allow_topic_injection: bool = False,
        inject_synthetic_health: bool = False,
        request_sample_count: int = HAND_REQUEST_SAMPLE_COUNT,
        request_duration_sec: float = HAND_REQUEST_DURATION_SEC,
        release_sample_count: int = HAND_RELEASE_SAMPLE_COUNT,
        release_duration_sec: float = HAND_RELEASE_DURATION_SEC,
    ) -> None:
        """Emit request episodes separated by a valid release dwell.

        This method deliberately does not infer controller completion. Call
        ``wait_for_virtual_hand_episode_result`` after an individual request
        when exercising a running virtual scenario with real state changes.
        """

        _require_hand_topic_injection_allowed(allow_topic_injection)
        count = _positive_int("iterations", iterations)
        for index in range(count):
            self.emit_hand_handover_probe(
                allow_topic_injection=True,
                inject_synthetic_health=inject_synthetic_health,
                sample_count=request_sample_count,
                duration_sec=request_duration_sec,
            )
            if index + 1 < count:
                self.emit_hand_handover_release(
                    allow_topic_injection=True,
                    inject_synthetic_health=inject_synthetic_health,
                    sample_count=release_sample_count,
                    duration_sec=release_duration_sec,
                )

    def emit_stable_tool_prediction(
        self,
        *,
        phase_id: str,
        tool_id: str,
        confidence: float = 0.92,
        duration_sec: float = 6.5,
    ) -> None:
        end_time = time.time() + duration_sec
        while time.time() < end_time:
            stamp = self.get_clock().now().to_msg()
            evidence = PhaseEvidence()
            evidence.stamp = stamp
            evidence.source = "manual_probe"
            evidence.phase_ids = [phase_id]
            evidence.phase_confidences = [0.95]
            evidence.visible_instrument_ids = []
            evidence.visible_instrument_confidences = []
            evidence.uncertainty = 1.0 - confidence
            evidence.scene_summary = "manual stable phase observation"
            self._phase_evidence_pub.publish(evidence)

            payload = {
                "v": "2",
                "phase": [phase_id, 0.95],
                "tool": [tool_id, confidence],
                "intent": ["none", "", 0.0],
                "mayo": [],
                "mayo_retrieve": ["", 0.0],
                "u": 0.05,
                "sum": f"manual stable next-tool prediction for {tool_id}",
            }
            msg = VLMResult()
            msg.stamp = stamp
            msg.source = "manual_probe"
            msg.schema_version = "2"
            msg.raw_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
            msg.summary = payload["sum"]
            msg.phase_ids = [phase_id]
            msg.phase_confidences = [0.95]
            msg.observed_tool_ids = []
            msg.observed_location_ids = []
            msg.observed_location_types = []
            msg.observed_confidences = []
            msg.uncertainty = 0.05
            self._vlm_result_pub.publish(msg)
            rclpy.spin_once(self, timeout_sec=0.05)
            time.sleep(0.45)

    def emit_stable_phase_observation(
        self,
        *,
        phase_id: str,
        confidence: float = 0.95,
        duration_sec: float = 2.0,
    ) -> None:
        end_time = time.time() + duration_sec
        while time.time() < end_time:
            stamp = self.get_clock().now().to_msg()
            evidence = PhaseEvidence()
            evidence.stamp = stamp
            evidence.source = "manual_probe"
            evidence.phase_ids = [phase_id]
            evidence.phase_confidences = [confidence]
            evidence.visible_instrument_ids = []
            evidence.visible_instrument_confidences = []
            evidence.uncertainty = 1.0 - confidence
            evidence.scene_summary = "manual stable phase observation"
            self._phase_evidence_pub.publish(evidence)

            payload = {
                "v": "2",
                "phase": [phase_id, confidence],
                "tool": ["", 0.0],
                "intent": ["none", "", 0.0],
                "mayo": [],
                "mayo_retrieve": ["", 0.0],
                "u": 1.0 - confidence,
                "sum": "manual stable phase observation",
            }
            msg = VLMResult()
            msg.stamp = stamp
            msg.source = "manual_probe"
            msg.schema_version = "2"
            msg.raw_json = json.dumps(
                payload,
                separators=(",", ":"),
                sort_keys=True,
            )
            msg.summary = payload["sum"]
            msg.phase_ids = [phase_id]
            msg.phase_confidences = [confidence]
            msg.observed_tool_ids = []
            msg.observed_location_ids = []
            msg.observed_location_types = []
            msg.observed_confidences = []
            msg.uncertainty = 1.0 - confidence
            self._vlm_result_pub.publish(msg)
            rclpy.spin_once(self, timeout_sec=0.05)
            time.sleep(0.2)

    def wait_for_request_transition(self, event_type: str, tool_id: str, timeout_sec: float = 10.0) -> None:
        self.wait_until(
            lambda: any(
                request_event == event_type and requested_tool == tool_id
                for _, request_event, requested_tool in self._surgeon_request_log
            ),
            timeout_sec,
            f"surgeon request transition {event_type}:{tool_id}",
        )

    def wait_for_decision(
        self, decision: str, tool_id: str, timeout_sec: float = 12.0
    ) -> BTDecision:
        self.wait_until(
            lambda: any(
                record.decision == decision and record.selected_tool == tool_id
                for record in self._decision_log
            ),
            timeout_sec,
            f"bt decision {decision}:{tool_id}",
        )
        return next(
            record
            for record in reversed(self._decision_log)
            if record.decision == decision and record.selected_tool == tool_id
        )

    def wait_for_skill_event(
        self, event_type: str, tool_id: str, timeout_sec: float = 16.0
    ) -> TwinEvent:
        self.wait_until(
            lambda: any(
                event.event_type == event_type and event.instrument_id == tool_id
                for event in self._event_log
            ),
            timeout_sec,
            f"skill event {event_type}:{tool_id}",
        )
        return next(
            event
            for event in reversed(self._event_log)
            if event.event_type == event_type and event.instrument_id == tool_id
        )

    def wait_for_skill_command(
        self, action: str, tool_id: str, timeout_sec: float = 12.0
    ) -> SkillCommand:
        self.wait_until(
            lambda: any(
                command.action == action and command.instrument_id == tool_id
                for command in self._skill_command_log
            ),
            timeout_sec,
            f"skill command {action}:{tool_id}",
        )
        return next(
            command
            for command in reversed(self._skill_command_log)
            if command.action == action and command.instrument_id == tool_id
        )

    def wait_for_virtual_hand_episode_result(
        self,
        *,
        expected_tool_id: str,
        command_log_start: int,
        trace_log_start: int,
        event_log_start: int,
        timeout_sec: float = 20.0,
    ) -> HandEpisodeObservation:
        """Observe one exactly-once implicit handover on the virtual route."""

        self.assert_virtual_execution_route(timeout_sec=min(timeout_sec, 10.0))

        def matching_commands() -> list[SkillCommand]:
            return [
                command
                for command in self._skill_command_log[command_log_start:]
                if command.mode == "implicit_request"
                and command.action in HANDOVER_ACTIONS
                and command.instrument_id == expected_tool_id
            ]

        self.wait_until(
            lambda: bool(matching_commands()),
            timeout_sec,
            f"implicit virtual handover command for {expected_tool_id}",
        )
        commands = matching_commands()
        if len(commands) != 1:
            raise RuntimeError(
                "Expected exactly one implicit handover command for the episode, "
                f"observed {len(commands)}."
            )
        command = commands[0]
        expected_command_id = (
            f"skill-hand-{command.procedure_run_id}-"
            f"{int(command.implicit_request_generation)}-{command.action}"
        )
        if not command.procedure_run_id or int(command.implicit_request_generation) <= 0:
            raise RuntimeError("Implicit handover command omitted its run/episode identity.")
        if command.command_id != expected_command_id:
            raise RuntimeError(
                "Implicit handover command ID was not deterministic: "
                f"expected={expected_command_id!r} actual={command.command_id!r}"
            )

        self.wait_until(
            lambda: any(
                trace.command_id == command.command_id and bool(trace.terminal)
                for trace in self._execution_trace_log[trace_log_start:]
            ),
            timeout_sec,
            f"terminal virtual execution trace for {command.command_id}",
        )
        traces = [
            trace
            for trace in self._execution_trace_log[trace_log_start:]
            if trace.command_id == command.command_id
        ]
        non_virtual = [
            trace
            for trace in traces
            if trace.endpoint_source != VIRTUAL_ENDPOINT_SOURCE
            or trace.endpoint != VIRTUAL_TOOL_HANDOVER_ENDPOINT
        ]
        if non_virtual:
            trace = non_virtual[0]
            raise RuntimeError(
                "Hand episode escaped the virtual route: "
                f"source={trace.endpoint_source!r} endpoint={trace.endpoint!r}"
            )
        terminal = next(trace for trace in reversed(traces) if trace.terminal)
        if terminal.stage != "completed":
            raise RuntimeError(
                "Virtual handover did not complete successfully: "
                f"stage={terminal.stage!r} reason={terminal.reason_code!r}"
            )

        self.wait_until(
            lambda: any(
                event.event_type == "ToolHandoverCompleted"
                and event.instrument_id == expected_tool_id
                for event in self._event_log[event_log_start:]
            ),
            timeout_sec,
            f"ToolHandoverCompleted event for {expected_tool_id}",
        )
        return HandEpisodeObservation(
            procedure_run_id=command.procedure_run_id,
            generation=int(command.implicit_request_generation),
            command_id=command.command_id,
            action=command.action,
            instrument_id=command.instrument_id,
            instrument_instance_id=command.instrument_instance_id,
            terminal_reason_code=terminal.reason_code,
        )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec-name", default="thyroidectomy")
    parser.add_argument(
        "--vlm-mode",
        default="none",
        choices=["none", "mock", "real", "dual"],
        help=(
            "External VLM source for the probe. The default disables competing "
            "publishers; the probe still injects public VLM evidence itself."
        ),
    )
    parser.add_argument("--vlm-response-mode", default="live")
    parser.add_argument("--vlm-base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--vlm-model-id", default="unsloth/gemma-4-E4B-it-NVFP4")
    parser.add_argument(
        "--surgeon-actor-mode",
        default="rule",
        choices=["rule", "llm", "none"],
        help="Actor used by the probe runtime; rule is deterministic after muting.",
    )
    parser.add_argument(
        "--allow-hand-topic-injection",
        action="store_true",
        help=(
            "Explicitly permit publishing synthetic CAM4 hand topics. Without "
            "this flag the probe exits before launching a runtime."
        ),
    )
    parser.add_argument(
        "--inject-synthetic-hand-health",
        action="store_true",
        help=(
            "Publish the pinned synthetic hand-health document before each "
            "request/release sequence. This is opt-in because it replaces the "
            "latched health observation in the selected ROS domain."
        ),
    )
    parser.add_argument(
        "--inject-synthetic-rfdetr-readiness",
        action="store_true",
        help=(
            "Continuously publish typed empty CAM3/CAM4 RF-DETR frames needed "
            "by perception-gated demo bundles. This is isolated-test evidence, "
            "not detector validation."
        ),
    )
    parser.add_argument(
        "--hand-iterations",
        type=int,
        default=1,
        help="Number of completed virtual direct-hand episodes to exercise.",
    )
    parser.add_argument(
        "--hand-only",
        action="store_true",
        help=(
            "Stop after the typed hand-input virtual Action soak. Between "
            "episodes the scenario is stopped and started so the same "
            "requestable inventory is restored."
        ),
    )
    parser.add_argument(
        "--hand-release-duration-sec",
        type=float,
        default=HAND_RELEASE_DURATION_SEC,
        help="Valid non-request dwell between hand episodes (default: 0.7 s).",
    )
    parser.add_argument(
        "--hand-release-samples",
        type=int,
        default=HAND_RELEASE_SAMPLE_COUNT,
        help="Valid non-request samples between hand episodes (default: 10).",
    )
    parser.add_argument(
        "--ros-domain-id",
        type=int,
        default=223,
        help=(
            "Isolated ROS domain for the spawned probe runtime. The default "
            "avoids the normal deployment domain."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    if not args.allow_hand_topic_injection:
        print(
            "Manual probe refused to publish CAM4 hand topics without "
            "--allow-hand-topic-injection.",
            file=sys.stderr,
        )
        return 2
    if not args.inject_synthetic_hand_health:
        print(
            "The isolated manual probe has no live CAM4 health publisher; pass "
            "--inject-synthetic-hand-health explicitly.",
            file=sys.stderr,
        )
        return 2
    try:
        _positive_int("hand_iterations", args.hand_iterations)
        _positive_int("hand_release_samples", args.hand_release_samples, minimum=2)
        _positive_duration(
            "hand_release_duration_sec",
            args.hand_release_duration_sec,
        )
    except (TypeError, ValueError) as exc:
        print(f"Invalid hand probe arguments: {exc}", file=sys.stderr)
        return 2
    if not 0 <= int(args.ros_domain_id) <= 232:
        print("--ros-domain-id must be between 0 and 232.", file=sys.stderr)
        return 2
    os.environ["ROS_DOMAIN_ID"] = str(int(args.ros_domain_id))
    try:
        existing_domain_nodes = _probe_domain_nodes()
    except RuntimeError as exc:
        print(f"Manual probe domain check failed: {exc}", file=sys.stderr)
        return 2
    if existing_domain_nodes:
        print(
            "Manual probe refused a non-empty ROS domain "
            f"{args.ros_domain_id}: {', '.join(existing_domain_nodes)}",
            file=sys.stderr,
        )
        return 2
    spec_dir = get_default_spec_dir().parent / str(args.spec_name)
    bundle = load_bundle(spec_dir)
    runtime_requirements = bundle.get_scenario_runtime_requirements()
    if (
        runtime_requirements.rfdetr_tool_observations_required
        and not args.inject_synthetic_rfdetr_readiness
    ):
        print(
            "The selected bundle requires fresh typed CAM3/CAM4 RF-DETR "
            "evidence; pass --inject-synthetic-rfdetr-readiness explicitly "
            "for this isolated probe.",
            file=sys.stderr,
        )
        return 2
    runtime = ManagedProcess(
        name="taskplanner_probe_runtime",
        command=_probe_runtime_command(args, spec_dir),
    )

    rclpy.init()
    harness = ManualProbeHarness()
    result = None
    try:
        if args.inject_synthetic_rfdetr_readiness:
            harness.enable_synthetic_rfdetr_readiness(
                allow_topic_injection=True,
            )
        runtime.start()
        harness.wait_for_services()
        harness.wait_for_catalog_entry()
        harness.wait_for_probe_subscribers()
        harness.assert_virtual_execution_route()
        harness.select_bundle(args.spec_name)
        if (
            args.surgeon_actor_mode != "none"
            and not harness.set_random_voice_enabled(False)
        ):
            raise RuntimeError(
                "selected surgeon actor does not expose random_voice_enabled"
            )
        time.sleep(1.0)
        harness.control("start")
        harness.wait_until(
            lambda: harness._latest_world is not None
            and harness._latest_world.execution_state == "running",
            18.0,
            "running world state",
        )
        if harness._latest_world is None:
            raise RuntimeError("No world state available after start.")
        tool_id = harness.choose_probe_tool()
        phase_id = harness._latest_world.filtered_phase
        harness.emit_stable_phase_observation(phase_id=phase_id)
        harness.wait_for_handover_window(timeout_sec=12.0)

        prediction_started_at = time.monotonic()
        harness.emit_stable_tool_prediction(
            phase_id=phase_id,
            tool_id=tool_id,
            duration_sec=1.4,
        )
        harness.wait_for_skill_command("predict_tool", tool_id)
        harness.wait_until(
            lambda: harness._latest_world is not None
            and harness._latest_world.prepositioned_tool == tool_id,
            16.0,
            "stable VLM-predicted tool to become prepositioned",
        )
        prediction_to_preposition_sec = time.monotonic() - prediction_started_at

        request_log_start = len(harness._surgeon_request_log)
        first_implicit_tool_id = tool_id
        hand_episode_tool_ids: list[str] = []
        hand_episode_durations_sec: list[float] = []
        hand_episode_observations: list[HandEpisodeObservation] = []
        for episode_index in range(int(args.hand_iterations)):
            command_log_start = len(harness._skill_command_log)
            trace_log_start = len(harness._execution_trace_log)
            event_log_start = len(harness._event_log)
            implicit_started_at = time.monotonic()
            harness.emit_hand_handover_probe(
                allow_topic_injection=True,
                inject_synthetic_health=args.inject_synthetic_hand_health,
            )
            harness.wait_for_decision("implicit_request", tool_id)
            episode_observation = harness.wait_for_virtual_hand_episode_result(
                expected_tool_id=tool_id,
                command_log_start=command_log_start,
                trace_log_start=trace_log_start,
                event_log_start=event_log_start,
            )
            hand_episode_observations.append(episode_observation)
            hand_episode_tool_ids.append(tool_id)
            hand_episode_durations_sec.append(
                round(time.monotonic() - implicit_started_at, 3)
            )

            harness.wait_until(
                lambda tool_id=tool_id: harness._latest_world is not None
                and any(
                    instrument.instrument_id == tool_id
                    and instrument.owner == "surgeon"
                    for instrument in harness._latest_world.instrument_states
                ),
                10.0,
                f"{tool_id} to appear with surgeon",
            )
            if episode_index + 1 >= int(args.hand_iterations):
                continue

            harness.emit_hand_handover_release(
                allow_topic_injection=True,
                inject_synthetic_health=args.inject_synthetic_hand_health,
                sample_count=args.hand_release_samples,
                duration_sec=args.hand_release_duration_sec,
            )
            harness.wait_until(
                lambda: harness._latest_world is not None
                and not bool(harness._latest_world.implicit_request_visible),
                4.0,
                "released direct-hand evidence",
            )
            if args.hand_only:
                previous_run_id = str(
                    harness._latest_world.procedure_run_id
                    if harness._latest_world is not None
                    else ""
                )
                harness.control("stop")
                harness.wait_until(
                    lambda: harness._latest_world is not None
                    and harness._latest_world.execution_state == "halted",
                    10.0,
                    "halted world between typed hand episodes",
                )
                harness.control("start")
                harness.wait_until(
                    lambda: harness._latest_world is not None
                    and harness._latest_world.execution_state == "running"
                    and bool(harness._latest_world.procedure_run_id)
                    and harness._latest_world.procedure_run_id != previous_run_id,
                    18.0,
                    "fresh running world between typed hand episodes",
                )
            tool_id = harness.choose_probe_tool()
            next_phase = (
                harness._latest_world.filtered_phase
                if harness._latest_world is not None
                else phase_id
            )
            harness.emit_stable_tool_prediction(
                phase_id=next_phase,
                tool_id=tool_id,
                duration_sec=1.4,
            )
            harness.wait_for_skill_command("predict_tool", tool_id)
            harness.wait_until(
                lambda tool_id=tool_id: harness._latest_world is not None
                and harness._latest_world.prepositioned_tool == tool_id,
                16.0,
                f"episode {episode_index + 2} tool to become prepositioned",
            )
            harness.wait_for_handover_window(timeout_sec=12.0)

        implicit_handover_sec = hand_episode_durations_sec[0]
        leaked_direct_hand_request = any(
            event_type in {"request_tool", "voice_request"}
            or bool(requested_tool)
            for _, event_type, requested_tool in harness._surgeon_request_log[
                request_log_start:
            ]
        )
        if leaked_direct_hand_request:
            raise RuntimeError(
                "direct hand evidence leaked into the explicit surgeon request path"
            )

        if args.hand_only:
            external_execution_traces = [
                trace
                for trace in harness._execution_trace_log
                if trace.endpoint_source != VIRTUAL_ENDPOINT_SOURCE
                or trace.endpoint == "/surgery/tool_handover"
            ]
            if external_execution_traces:
                raise RuntimeError(
                    "typed hand soak observed a non-virtual execution trace"
                )
            hand_result = HandOnlyProbeResult(
                bundle=args.spec_name,
                input_scope=(
                    "synthetic_typed_hand_and_readiness;"
                    "not_cam4_or_mediapipe_accuracy"
                ),
                endpoint_source=VIRTUAL_ENDPOINT_SOURCE,
                endpoint=VIRTUAL_TOOL_HANDOVER_ENDPOINT,
                completed_episode_count=len(hand_episode_observations),
                requested_episode_count=int(args.hand_iterations),
                procedure_run_ids=[
                    observation.procedure_run_id
                    for observation in hand_episode_observations
                ],
                command_ids=[
                    observation.command_id
                    for observation in hand_episode_observations
                ],
                tool_ids=[
                    observation.instrument_id
                    for observation in hand_episode_observations
                ],
                episode_durations_sec=hand_episode_durations_sec,
                external_execution_trace_count=0,
                invariant_violation_count=len(
                    harness._world_invariant_violations
                ),
            )
            if hand_result.completed_episode_count != int(args.hand_iterations):
                raise RuntimeError(
                    "typed hand soak completed fewer episodes than requested"
                )
            if len(set(hand_result.command_ids)) != len(hand_result.command_ids):
                raise RuntimeError("typed hand soak reused an Action command ID")
            print(json.dumps(asdict(hand_result), indent=2, sort_keys=True))
            return 0

        prediction_tool = harness.choose_probe_tool()
        prediction_phase = harness._latest_world.filtered_phase if harness._latest_world else phase_id
        harness.emit_stable_tool_prediction(
            phase_id=prediction_phase,
            tool_id=prediction_tool,
            duration_sec=1.4,
        )
        harness.wait_for_skill_command("predict_tool", prediction_tool)
        harness.wait_until(
            lambda: harness._latest_world is not None
            and harness._latest_world.prepositioned_tool == prediction_tool,
            16.0,
            "stable VLM-predicted tool to become prepositioned",
        )
        if harness._latest_world is None:
            raise RuntimeError("No world state available for replacement probe.")
        prepositioned_tool = harness._latest_world.prepositioned_tool

        withdrawal_started_at = time.monotonic()
        harness.emit_stable_phase_observation(
            phase_id=harness._latest_world.filtered_phase,
            duration_sec=2.0,
        )
        harness.wait_for_skill_event(
            "UnusedPrepositionReturned",
            prepositioned_tool,
        )
        prediction_evidence_withdrawal_release_sec = (
            time.monotonic() - withdrawal_started_at
        )
        harness.wait_until(
            lambda: harness._latest_world is not None
            and not harness._latest_world.prepositioned_tool,
            8.0,
            "expired prediction preparation slot to become empty",
        )

        replacement_tool = harness.choose_override_probe_tool(prepositioned_tool)
        replacement_started_at = time.monotonic()
        harness.emit_stable_tool_prediction(
            phase_id=harness._latest_world.filtered_phase,
            tool_id=replacement_tool,
            duration_sec=1.4,
        )
        harness.wait_for_skill_command("predict_tool", replacement_tool)
        harness.wait_until(
            lambda: harness._latest_world is not None
            and harness._latest_world.prepositioned_tool == replacement_tool,
            16.0,
            "replacement prediction to become prepositioned",
        )
        replacement_prediction_to_preposition_sec = (
            time.monotonic() - replacement_started_at
        )

        correction_tool = harness.choose_override_probe_tool(replacement_tool)
        correction_started_at = time.monotonic()
        harness.inject_voice_override(correction_tool)
        harness.wait_for_override_request(correction_tool)
        harness.wait_for_decision("explicit_request", correction_tool)
        harness.wait_for_skill_command("put_down_and_handover", correction_tool)
        harness.wait_for_skill_event("UnusedPrepositionReturned", replacement_tool)
        wrong_preposition_release_sec = time.monotonic() - correction_started_at
        harness.wait_for_skill_event("ToolHandoverCompleted", correction_tool)

        result = ProbeResult(
            bundle=args.spec_name,
            implicit_request_tool_id=first_implicit_tool_id,
            initial_preposition_tool_id=first_implicit_tool_id,
            replacement_preposition_tool_id=replacement_tool,
            explicit_request_tool_id=correction_tool,
            visual_evidence_did_not_create_explicit_request=True,
            implicit_request_tool_was_unspecified=True,
            implicit_handover_completed=True,
            initial_preposition_completed=True,
            prediction_evidence_withdrawal_returned=True,
            wrong_preposition_returned=True,
            replacement_preposition_completed=True,
            voice_correction_used_put_down_and_handover=True,
            explicit_handover_completed=True,
            implicit_request_to_handover_sec=round(implicit_handover_sec, 3),
            prediction_to_preposition_sec=round(
                prediction_to_preposition_sec,
                3,
            ),
            prediction_evidence_withdrawal_release_sec=round(
                prediction_evidence_withdrawal_release_sec,
                3,
            ),
            wrong_preposition_release_sec=round(
                wrong_preposition_release_sec,
                3,
            ),
            replacement_prediction_to_preposition_sec=round(
                replacement_prediction_to_preposition_sec,
                3,
            ),
            invariant_violation_count=len(harness._world_invariant_violations),
            hand_episode_count=len(hand_episode_tool_ids),
            hand_episode_tool_ids=hand_episode_tool_ids,
            hand_episode_durations_sec=hand_episode_durations_sec,
        )
        print(json.dumps(asdict(result), indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"Manual probe failed: {exc}", file=sys.stderr)
        if harness._surgeon_request_log:
            print("\nRecent surgeon requests:", file=sys.stderr)
            for _, event_type, requested_tool in harness._surgeon_request_log[-8:]:
                print(f"  - {event_type}:{requested_tool}", file=sys.stderr)
        if harness._decision_log:
            print("\nRecent BT decisions:", file=sys.stderr)
            for record in harness._decision_log[-12:]:
                print(
                    f"  - decision={record.decision} tool={record.selected_tool} action={record.action}",
                    file=sys.stderr,
                )
        if harness._event_log:
            print("\nRecent skill events:", file=sys.stderr)
            for event in harness._event_log[-12:]:
                print(
                    f"  - event={event.event_type} tool={event.instrument_id} "
                    f"instance={event.instance_id or 'none'} "
                    f"target={event.target_location_id or event.location_id}",
                    file=sys.stderr,
                )
        if harness._detailed_skill_statuses:
            print("\nRecent skill statuses:", file=sys.stderr)
            for (
                command_id,
                action,
                state,
                success,
                reason,
                instrument_id,
            ) in harness._detailed_skill_statuses[-16:]:
                print(
                    f"  - action={action} state={state} "
                    f"success={success} tool={instrument_id or 'none'} "
                    f"command={command_id or 'none'} reason={reason or 'none'}",
                    file=sys.stderr,
                )
        if harness._latest_world is not None:
            print("\nLatest world summary:", file=sys.stderr)
            print(
                f"  - phase={harness._latest_world.filtered_phase} "
                f"state={harness._latest_world.execution_state} "
                f"uncertain={harness._latest_world.phase_uncertain} "
                f"expected={list(harness._latest_world.expected_instruments)} "
                f"prepositioned={harness._latest_world.prepositioned_tool} "
                f"surgeon_request={harness._latest_world.surgeon_request_tool} "
                f"active_task={harness._latest_world.active_robot_task_id}",
                file=sys.stderr,
            )
            for instrument in harness._latest_world.instrument_states:
                print(
                    "  - tool="
                    f"{instrument.instrument_id} lifecycle={instrument.lifecycle_stage} "
                    f"loc=({instrument.location_type},{instrument.location_id}) "
                    f"owner={instrument.owner} contaminated={instrument.contaminated} "
                    f"next={instrument.next_required_transition}",
                    file=sys.stderr,
                )
        if runtime.log_path:
            print("\nRuntime log tail:", file=sys.stderr)
            print(runtime.tail(), file=sys.stderr)
        return 1
    finally:
        try:
            harness.control("stop")
        except Exception:
            pass
        harness.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
        runtime.stop()


if __name__ == "__main__":
    raise SystemExit(main())
