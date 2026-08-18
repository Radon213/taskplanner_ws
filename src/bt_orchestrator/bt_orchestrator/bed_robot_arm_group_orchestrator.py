"""BT-side guards and routing for bed-mounted retraction arms.

The legacy ``BedRobotArmGroup*`` envelope is retained for compatibility, but
the lane is retraction-only.  Tool changes and fine retraction adjustments are
distinct operations that map onto the reviewed external service and Action.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import time
import uuid

from procedure_spec import (
    BedRobotArmGroupNormalizationError,
    RETRACTION_DIRECTIONS,
    get_default_spec_dir,
    infer_retraction_direction,
    load_bundle,
    normalize_retraction_request,
    validate_retraction_distance_proposal,
)
import rclpy
from rcl_interfaces.msg import SetParametersResult
from rclpy.node import Node
from std_msgs.msg import String
from surgical_interop_msgs.msg import BedRobotArmStateArray
from surgical_msgs.msg import (
    BedRobotArmGroupActionProposal,
    BedRobotArmGroupCommand,
    BedRobotArmGroupRequest,
    BedRobotArmGroupState,
    BedRobotArmGroupStatus,
    WorldState,
)


GROUP_RETRACTION = "retraction"
OP_ADJUSTMENT = "retraction"
OP_TOOL_CHANGE = "change_end_effector"
POLICY_ADJUSTMENT = "retraction_adjustment"
POLICY_TOOL_CHANGE = "tool_change"

ARM_IDS = frozenset({"arm_1", "arm_2"})
TARGET_TOOL_IDS = frozenset({"thyroid_retractor", "army_navy_retractor"})
SINGLE_TARGETS = frozenset({"left_malleable", "right_malleable"})
MULTI_TARGET = "both_malleable"
DIRECTION_FRAME = "surgeon_view"
CARDINAL_DIRECTIONS = frozenset({"up", "down", "left", "right"})
ADJUSTMENT_AXES = frozenset({"left_right", "up_down"})

_CONTROLLER_STATES = frozenset(
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
_PROCEDURE_ROLE_LAYOUTS = {
    "thyroidectomy": frozenset({"army_navy"}),
    "nephrectomy": frozenset({"left_malleable", "right_malleable"}),
}

_TOOL_ALIASES = {
    "thyroid_retractor": "thyroid_retractor",
    "갑상선": "thyroid_retractor",
    "army": "army_navy_retractor",
    "army_navy": "army_navy_retractor",
    "army_navy_retractor": "army_navy_retractor",
    "아미": "army_navy_retractor",
}


@dataclass(slots=True)
class PendingRetractionAdjustment:
    request: BedRobotArmGroupRequest
    received_at: float


class BedRobotArmGroupOrchestrator(Node):
    """Route tool changes and guarded VLM retraction adjustments."""

    _VOICE_DEDUP_SEC = 1.5
    _RETRACTION_BUSY_STATES = {
        "changing_tool",
        "moving_to_standby",
        "direct_teach",
        "fault",
        "protective_stop",
        "unknown",
    }

    def __init__(self) -> None:
        super().__init__("bed_robot_arm_group_orchestrator")
        self.declare_parameter("spec_dir", str(get_default_spec_dir()))
        self.declare_parameter("vlm_confidence_threshold", 0.60)
        self.declare_parameter("visual_direction_confidence_threshold", 0.75)
        # Real VLM defaults to 20 seconds per attempt with two retries.  Leave
        # enough room for all three attempts plus transport/validation margin.
        self.declare_parameter("vlm_proposal_timeout_sec", 70.0)
        self.declare_parameter("bed_robot_status_timeout_sec", 2.0)
        self.declare_parameter("bed_robot_source_max_age_sec", 2.0)
        self.declare_parameter("bed_robot_source_future_tolerance_sec", 0.5)
        self._spec_dir = str(self.get_parameter("spec_dir").value)
        self._confidence_threshold = float(
            self.get_parameter("vlm_confidence_threshold").value
        )
        self._visual_direction_threshold = float(
            self.get_parameter("visual_direction_confidence_threshold").value
        )
        self._vlm_proposal_timeout_sec = max(
            0.5,
            float(self.get_parameter("vlm_proposal_timeout_sec").value),
        )
        self._bed_robot_status_timeout_sec = max(
            0.1,
            float(self.get_parameter("bed_robot_status_timeout_sec").value),
        )
        self._bed_robot_source_max_age_sec = max(
            0.1,
            float(self.get_parameter("bed_robot_source_max_age_sec").value),
        )
        self._bed_robot_source_future_tolerance_sec = max(
            0.0,
            float(
                self.get_parameter(
                    "bed_robot_source_future_tolerance_sec"
                ).value
            ),
        )
        self._spec = load_bundle(self._spec_dir)
        self._bt_ready = False
        self._group_states: dict[str, BedRobotArmGroupState] = {}
        self._world: WorldState | None = None
        self._pending_retraction: PendingRetractionAdjustment | None = None
        self._inflight_commands: dict[str, BedRobotArmGroupCommand] = {}
        self._seen_request_ids: set[str] = set()
        self._dispatched_request_ids: set[str] = set()
        self._recent_voice_requests: dict[str, tuple[str, float]] = {}
        self._controller_arms_by_role: dict[str, object] = {}
        self._controller_status_received_at = 0.0
        self._controller_status_revision: int | None = None
        self._controller_status_source_stamp_ns: int | None = None
        self._controller_status_signature: tuple[object, ...] | None = None
        self._controller_status_epoch = 0
        self._operation_status_ns: dict[str, int] = {}
        self._last_lifecycle_control_signature: tuple[str, str] | None = None
        self.add_on_set_parameters_callback(self._on_parameters_changed)

        self._request_pub = self.create_publisher(
            BedRobotArmGroupRequest,
            "/surgeon/bed_robot_arm_group_request",
            20,
        )
        self._command_pub = self.create_publisher(
            BedRobotArmGroupCommand,
            "/bt/bed_robot_arm_group_command",
            20,
        )
        self._status_pub = self.create_publisher(
            BedRobotArmGroupStatus,
            "/bed_robot_arm_group/status",
            20,
        )
        self.create_subscription(
            BedRobotArmGroupRequest,
            "/surgeon/bed_robot_arm_group_request",
            self._on_group_request,
            20,
        )
        self.create_subscription(
            BedRobotArmGroupActionProposal,
            "/vlm/bed_robot_arm_group_proposal",
            self._on_group_proposal,
            20,
        )
        self.create_subscription(
            BedRobotArmGroupActionProposal,
            "/vlm_real/bed_robot_arm_group_proposal",
            self._on_group_proposal,
            20,
        )
        self.create_subscription(
            BedRobotArmGroupStatus,
            "/bed_robot_arm_group/status",
            self._on_group_status,
            50,
        )
        self.create_subscription(WorldState, "/twin/world_state", self._on_world, 20)
        self.create_subscription(
            BedRobotArmStateArray,
            "/external/bed_robot_arms/status",
            self._on_controller_status,
            20,
        )
        self.create_subscription(String, "/surgery/audio/request_text", self._on_voice, 20)
        self.create_subscription(String, "/simulation/control_state", self._on_control, 20)
        self.create_timer(0.2, self._expire_pending_retraction)

    def _stamp(self):
        return self.get_clock().now().to_msg()

    def _on_parameters_changed(self, params):
        for parameter in params:
            if parameter.name == "spec_dir":
                try:
                    new_spec_dir = str(parameter.value)
                    new_spec = load_bundle(new_spec_dir)
                except Exception as exc:
                    return SetParametersResult(
                        successful=False,
                        reason=f"failed to reload group procedure spec: {exc}",
                    )
                self._spec_dir = new_spec_dir
                self._spec = new_spec
                self._last_lifecycle_control_signature = None
                self._clear_runtime_state()
            elif parameter.name == "vlm_confidence_threshold":
                self._confidence_threshold = float(parameter.value)
            elif parameter.name == "visual_direction_confidence_threshold":
                self._visual_direction_threshold = float(parameter.value)
            elif parameter.name == "vlm_proposal_timeout_sec":
                self._vlm_proposal_timeout_sec = max(0.5, float(parameter.value))
            elif parameter.name == "bed_robot_status_timeout_sec":
                self._bed_robot_status_timeout_sec = max(0.1, float(parameter.value))
            elif parameter.name == "bed_robot_source_max_age_sec":
                self._bed_robot_source_max_age_sec = max(
                    0.1, float(parameter.value)
                )
            elif parameter.name == "bed_robot_source_future_tolerance_sec":
                self._bed_robot_source_future_tolerance_sec = max(
                    0.0, float(parameter.value)
                )
        return SetParametersResult(successful=True)

    @staticmethod
    def _public_procedure_type(procedure_id: str) -> str:
        normalized = str(procedure_id or "").strip().casefold()
        if normalized in {"thyroidectomy", "thyroidectomy_demo"}:
            return "thyroidectomy"
        return normalized

    @staticmethod
    def _aggregate_controller_state(arms_by_role: dict[str, object]) -> str:
        states = {str(arm.state).strip() for arm in arms_by_role.values()}
        for candidate in (
            "protective_stop",
            "fault",
            "unknown",
            "direct_teach",
            "changing_tool",
            "moving_to_standby",
            "retracting",
        ):
            if candidate in states:
                return candidate
        return "standby"

    @staticmethod
    def _controller_source_stamp_ns(msg: BedRobotArmStateArray) -> int | None:
        sec = int(msg.stamp.sec)
        nanosec = int(msg.stamp.nanosec)
        if sec < 0 or nanosec < 0 or nanosec >= 1_000_000_000:
            return None
        return sec * 1_000_000_000 + nanosec

    @staticmethod
    def _wall_time_ns() -> int:
        # The external controller contract uses wall-clock ROS time even when
        # Taskplanner itself is replaying against /clock.
        return time.time_ns()

    def _controller_source_age_sec(self, source_stamp_ns: int) -> float:
        return (self._wall_time_ns() - source_stamp_ns) / 1_000_000_000.0

    @staticmethod
    def _controller_snapshot_signature(
        procedure_type: str, arms_by_role: dict[str, object]
    ) -> tuple[object, ...]:
        return (
            procedure_type,
            tuple(
                sorted(
                    (
                        role_instance,
                        str(arm.arm_id).strip(),
                        str(arm.role).strip(),
                        str(arm.state).strip(),
                        bool(arm.direct_teach_active),
                        str(arm.reason_code).strip(),
                    )
                    for role_instance, arm in arms_by_role.items()
                )
            ),
        )

    def _on_controller_status(self, msg: BedRobotArmStateArray) -> None:
        procedure_type = self._public_procedure_type(msg.procedure_type)
        expected_roles = _PROCEDURE_ROLE_LAYOUTS.get(procedure_type)
        if expected_roles is None:
            return
        active_procedure = self._public_procedure_type(
            self._world.procedure_id if self._world is not None else self._spec.procedure_id
        )
        if procedure_type != active_procedure:
            return

        arms_by_role: dict[str, object] = {}
        arm_ids: set[str] = set()
        for arm in msg.arms:
            arm_id = str(arm.arm_id).strip()
            role_instance = str(arm.role_instance_id).strip()
            state = str(arm.state).strip()
            if (
                arm_id not in ARM_IDS
                or arm_id in arm_ids
                or str(arm.role).strip() != GROUP_RETRACTION
                or role_instance not in expected_roles
                or role_instance in arms_by_role
                or state not in _CONTROLLER_STATES
                or bool(arm.direct_teach_active) != (state == "direct_teach")
            ):
                return
            arm_ids.add(arm_id)
            arms_by_role[role_instance] = arm
        if frozenset(arms_by_role) != expected_roles:
            return

        revision = int(msg.revision)
        source_stamp_ns = self._controller_source_stamp_ns(msg)
        if source_stamp_ns is None or source_stamp_ns <= 0:
            return
        source_age_sec = self._controller_source_age_sec(source_stamp_ns)
        if (
            source_age_sec > self._bed_robot_source_max_age_sec
            or source_age_sec < -self._bed_robot_source_future_tolerance_sec
        ):
            return
        signature = self._controller_snapshot_signature(
            procedure_type, arms_by_role
        )
        controller_restarted = False
        previous_stamp_ns = self._controller_status_source_stamp_ns
        previous_revision = self._controller_status_revision
        if previous_stamp_ns is not None:
            # Source time remains the ordering fence across controller epochs.
            # A delayed snapshot from the previous epoch must not become current
            # merely because its revision is larger than the restarted counter.
            if source_stamp_ns <= previous_stamp_ns:
                return
            if previous_revision is not None and revision == previous_revision:
                # Same-revision heartbeats may refresh freshness, but a state
                # mutation without a revision advance is not authoritative.
                if signature != self._controller_status_signature:
                    return
            elif previous_revision is not None and revision < previous_revision:
                # The public contract has no epoch field. A strictly newer
                # source stamp plus a lower revision is the explicit restart
                # signal shared with the execution bridge.
                controller_restarted = True

        if controller_restarted:
            self._controller_status_epoch += 1
        self._controller_status_revision = revision
        self._controller_status_source_stamp_ns = source_stamp_ns
        self._controller_status_signature = signature
        self._controller_status_received_at = time.monotonic()
        self._controller_arms_by_role = arms_by_role

        # Never mix the previous controller epoch's displayed operation state
        # with the new controller snapshot. The local in-flight command map is
        # intentionally retained so dispatch stays blocked until the execution
        # bridge establishes the remote terminal state.
        current = (
            None
            if controller_restarted
            else self._group_states.get(GROUP_RETRACTION)
        )
        state = BedRobotArmGroupState()
        state.group_id = GROUP_RETRACTION
        state.stamp = msg.stamp
        state.connected = True
        state.state = self._aggregate_controller_state(arms_by_role)
        if len(arms_by_role) == 1:
            state.arm_id = str(next(iter(arms_by_role.values())).arm_id).strip()
        if current is not None:
            state.operation = current.operation
            state.target_tool_id = current.target_tool_id
            state.adjustment_mode = current.adjustment_mode
            state.target_retractor_id = current.target_retractor_id
            state.direction_frame = current.direction_frame
            state.direction = current.direction
            state.axis = current.axis
            state.distance_mm = float(current.distance_mm)
            state.distance_origin = current.distance_origin
            state.raw_distance_text = current.raw_distance_text
            state.active_request_id = current.active_request_id
            state.active_command_id = current.active_command_id
            state.progress = float(current.progress)
            state.error_code = current.error_code
            state.error_message = current.error_message
            state.rejection_reason = current.rejection_reason
        self._group_states[GROUP_RETRACTION] = state

    def _controller_status_guard(self) -> str:
        if not self._controller_arms_by_role:
            return "controller bed-arm status has not been observed"
        age = time.monotonic() - self._controller_status_received_at
        if age > self._bed_robot_status_timeout_sec:
            return "controller bed-arm status is stale"
        source_stamp_ns = self._controller_status_source_stamp_ns
        if source_stamp_ns is None:
            return "controller bed-arm source stamp is missing"
        source_age_sec = self._controller_source_age_sec(source_stamp_ns)
        if source_age_sec > self._bed_robot_source_max_age_sec:
            return "controller bed-arm source stamp is stale"
        if source_age_sec < -self._bed_robot_source_future_tolerance_sec:
            return "controller bed-arm source stamp is future-dated"
        return ""

    def _controller_arm_id_for_role(self, role_instance_id: str) -> str:
        arm = self._controller_arms_by_role.get(role_instance_id)
        return str(getattr(arm, "arm_id", "")).strip()

    @staticmethod
    def _voice_signature(text: str) -> str:
        return re.sub(r"[^0-9a-z가-힣]+", "", str(text).lower())

    @staticmethod
    def _utterance_matches(text: str, candidate: str) -> bool:
        left = BedRobotArmGroupOrchestrator._voice_signature(text)
        right = BedRobotArmGroupOrchestrator._voice_signature(candidate)
        # Scenario cues are exact surgeon lines. Free-form variants are handled
        # by the deterministic retraction parser below instead.
        return bool(left and right and left == right)

    def _on_world(self, msg: WorldState) -> None:
        self._world = msg
        # Bed-arm state is consumed directly from the controller topic. The
        # twin snapshot must never initialize or overwrite that authority.

    def _group_config(self, group_id: str):
        config = self._spec.get_bed_robot_arm_group_spec()
        if config is None:
            return None
        return next((group for group in config.groups if group.id == group_id), None)

    def _classify_voice(self, raw_text: str) -> tuple[str, str, str] | None:
        text = raw_text.strip().lower()
        # Clinical suction speech remains public evidence, but it is no longer
        # a bed-mounted robot-arm command.
        if "석션" in text or "suction" in text:
            return None
        if any(token in text for token in ("교체", "교환", "바꿔")):
            target_tool = next(
                (value for token, value in _TOOL_ALIASES.items() if token in text),
                "",
            )
            return (
                (GROUP_RETRACTION, OP_TOOL_CHANGE, target_tool)
                if target_tool
                else None
            )

        phase_id = self._world.filtered_phase if self._world is not None else ""
        transitions = self._spec.get_bed_robot_arm_end_effector_transitions(phase_id)
        for transition in transitions:
            if any(self._utterance_matches(raw_text, item) for item in transition.utterances):
                target_tool = _TOOL_ALIASES.get(transition.to_profile, "")
                if transition.group_id == GROUP_RETRACTION and target_tool:
                    return GROUP_RETRACTION, OP_TOOL_CHANGE, target_tool

        cues = self._spec.get_bed_robot_arm_group_cues(phase_id)
        for cue in cues:
            if any(self._utterance_matches(raw_text, item) for item in cue.utterances):
                if cue.group_id != GROUP_RETRACTION or cue.operation not in {
                    OP_ADJUSTMENT,
                    POLICY_ADJUSTMENT,
                }:
                    continue
                return GROUP_RETRACTION, OP_ADJUSTMENT, cue.end_effector_profile

        if any(token in text for token in ("당겨", "견인", "리트랙션", "리트랙터")):
            state = self._group_states.get(GROUP_RETRACTION)
            profile = state.end_effector_profile if state is not None else ""
            return GROUP_RETRACTION, OP_ADJUSTMENT, profile
        return None

    def _on_voice(self, msg: String) -> None:
        classified = self._classify_voice(msg.data)
        if classified is None:
            return
        signature = self._voice_signature(msg.data)
        now = time.monotonic()
        recent = self._recent_voice_requests.get(signature)
        if recent is not None and now - recent[1] <= self._VOICE_DEDUP_SEC:
            return
        group_id, operation, profile = classified
        request = BedRobotArmGroupRequest()
        request.stamp = self._stamp()
        request.request_id = f"voice-{uuid.uuid4().hex}"
        request.group_id = group_id
        request.operation = operation
        request.voice_text = msg.data.strip()
        request.procedure_id = self._world.procedure_id if self._world is not None else self._spec.procedure_id
        request.phase_id = self._world.filtered_phase if self._world is not None else ""
        request.end_effector_profile = profile
        if operation == OP_TOOL_CHANGE:
            request.arm_id = self._tool_change_arm_for_text(request.voice_text)
            request.target_tool_id = profile
        else:
            direction = infer_retraction_direction(request.voice_text)
            if direction in {"LEFT_RIGHT", "UP_DOWN"}:
                request.adjustment_mode = "multi"
                request.target_retractor_id = MULTI_TARGET
            else:
                request.adjustment_mode = "single"
                request.target_retractor_id = self._single_target_for_text(
                    request.voice_text, profile
                )
            request.direction_frame = DIRECTION_FRAME
        request.source = "deterministic_voice_router"
        self._recent_voice_requests[signature] = (request.request_id, now)
        self._request_pub.publish(request)

    def _tool_change_arm_for_text(self, text: str) -> str:
        observed_arm_id = self._controller_arm_id_for_role("army_navy")
        if observed_arm_id:
            return observed_arm_id
        lowered = str(text).lower()
        arm_2_tokens = (
            "arm 2",
            "arm_2",
            "2번 암",
            "2 번 암",
            "두 번째 암",
            "두번째 암",
        )
        arm_1_tokens = (
            "arm 1",
            "arm_1",
            "1번 암",
            "1 번 암",
            "첫 번째 암",
            "첫번째 암",
        )
        if any(token in lowered for token in arm_2_tokens):
            return "arm_2"
        if any(token in lowered for token in arm_1_tokens):
            return "arm_1"
        return ""

    @staticmethod
    def _single_target_for_text(text: str, profile: str) -> str:
        lowered = str(text).lower()
        if any(token in lowered for token in ("왼쪽 말레어블", "좌측 말레어블", "left malleable")):
            return "left_malleable"
        if any(token in lowered for token in ("오른쪽 말레어블", "우측 말레어블", "right malleable")):
            return "right_malleable"
        normalized_profile = str(profile).strip().lower()
        return normalized_profile if normalized_profile in SINGLE_TARGETS else ""

    def _on_group_request(self, msg: BedRobotArmGroupRequest) -> None:
        msg.operation = {
            POLICY_ADJUSTMENT: OP_ADJUSTMENT,
            POLICY_TOOL_CHANGE: OP_TOOL_CHANGE,
        }.get(msg.operation, msg.operation)
        if msg.operation == OP_TOOL_CHANGE:
            observed_arm_id = self._controller_arm_id_for_role("army_navy")
            if observed_arm_id:
                msg.arm_id = observed_arm_id
        request_id = msg.request_id.strip()
        if not request_id:
            self._publish_terminal(msg, success=False, error_code="missing_request_id", reason="request_id is required")
            return
        if request_id in self._seen_request_ids:
            return

        signature = self._voice_signature(msg.voice_text)
        now = time.monotonic()
        recent = self._recent_voice_requests.get(signature) if signature else None
        if recent is not None and recent[0] != request_id and now - recent[1] <= self._VOICE_DEDUP_SEC:
            self._seen_request_ids.add(request_id)
            self._publish_terminal(msg, success=True, outcome="duplicate_suppressed")
            return
        if signature:
            self._recent_voice_requests[signature] = (request_id, now)
        self._seen_request_ids.add(request_id)

        inflight = self._inflight_commands.get(msg.group_id)
        if inflight is not None:
            idempotent_inflight = bool(
                inflight.operation == msg.operation
                and msg.operation == OP_TOOL_CHANGE
                and inflight.target_tool_id == msg.target_tool_id
            )
            if idempotent_inflight:
                self._publish_terminal(
                    msg,
                    success=True,
                    outcome="already_in_flight",
                    reason=(
                        f"group '{msg.group_id}' is already executing "
                        f"'{msg.operation}'"
                    ),
                )
            else:
                self._publish_terminal(
                    msg,
                    success=False,
                    error_code="request_in_flight",
                    reason=(
                        f"group '{msg.group_id}' is still executing "
                        f"'{inflight.operation}'"
                    ),
                )
            return

        if msg.group_id == GROUP_RETRACTION and self._pending_retraction is not None:
            self._publish_terminal(
                msg,
                success=False,
                error_code="request_in_flight",
                reason="a retraction adjustment is already awaiting VLM validation",
            )
            return

        reason = self._request_guard_reason(msg)
        if reason:
            self._publish_terminal(msg, success=False, error_code="bt_guard_rejected", reason=reason)
            return
        state = self._group_states.get(msg.group_id)
        if self._is_idempotent(msg, state):
            self._publish_terminal(msg, success=True, outcome="already_satisfied")
            return

        if msg.operation == OP_ADJUSTMENT:
            if self._pending_retraction is not None:
                self._publish_terminal(
                    msg,
                    success=False,
                    error_code="request_in_flight",
                    reason="a retraction request is already awaiting VLM validation or completion",
                )
                return
            self._pending_retraction = PendingRetractionAdjustment(msg, now)
            return

        self._publish_command_from_request(msg)

    def _request_guard_reason(self, request: BedRobotArmGroupRequest) -> str:
        if request.group_id != GROUP_RETRACTION:
            return f"unsupported logical group '{request.group_id}'"
        group_config = self._group_config(request.group_id)
        if group_config is None or not group_config.enabled:
            return f"group '{request.group_id}' is disabled for procedure '{self._spec.procedure_id}'"
        policy_aliases = {
            OP_ADJUSTMENT: {OP_ADJUSTMENT, POLICY_ADJUSTMENT},
            OP_TOOL_CHANGE: {OP_TOOL_CHANGE, POLICY_TOOL_CHANGE},
        }.get(request.operation, {request.operation})
        if not policy_aliases.intersection(group_config.allowed_operations):
            return f"operation '{request.operation}' is not allowed for group '{request.group_id}'"
        if self._world is None:
            return "world state is not available"
        if not self._bt_ready:
            return "behavior tree executor is not ready for group dispatch"
        if not bool(self._world.running) or self._world.execution_state != "running":
            return "simulation is not in the running state"
        if request.procedure_id and request.procedure_id != self._world.procedure_id:
            return "request procedure does not match current world state"
        if request.phase_id and request.phase_id != self._world.filtered_phase:
            return "request phase does not match current world state"
        state = self._group_states.get(request.group_id)
        if state is None or not state.connected or state.state in {"offline", "fault"}:
            return f"group '{request.group_id}' is not connected and ready"
        controller_status_reason = self._controller_status_guard()
        if controller_status_reason:
            return controller_status_reason
        local_inflight = getattr(self, "_inflight_commands", {}).get(request.group_id)
        if local_inflight is not None and local_inflight.request_id != request.request_id:
            return (
                f"group '{request.group_id}' has an in-flight command "
                f"'{local_inflight.operation}'"
            )
        active_conflict = bool(
            state.active_command_id
            or (
                state.active_request_id
                and state.active_request_id != request.request_id
            )
        )
        if active_conflict and state.operation != request.operation:
            return (
                f"group '{request.group_id}' is busy with operation "
                f"'{state.operation or 'unknown'}'"
            )
        if request.operation not in {OP_ADJUSTMENT, OP_TOOL_CHANGE}:
            return f"unsupported retraction operation '{request.operation}'"
        if request.group_id == GROUP_RETRACTION:
            if request.operation == OP_ADJUSTMENT and (
                active_conflict or state.state in self._RETRACTION_BUSY_STATES
            ):
                return f"retraction is blocked while group state is '{state.state}'"
            if request.operation == OP_TOOL_CHANGE and (
                active_conflict or state.state in self._RETRACTION_BUSY_STATES
            ):
                return f"group operation is blocked while retraction state is '{state.state}'"
            if request.operation == OP_ADJUSTMENT:
                if request.target_retractor_id == MULTI_TARGET:
                    target_arms = [
                        self._controller_arms_by_role.get("left_malleable"),
                        self._controller_arms_by_role.get("right_malleable"),
                    ]
                else:
                    target_arms = [
                        self._controller_arms_by_role.get(
                            request.target_retractor_id
                        )
                    ]
                if any(arm is None for arm in target_arms):
                    return "requested retractor role is unavailable"
                for arm in target_arms:
                    arm_state = str(arm.state).strip()
                    if bool(arm.direct_teach_active) or arm_state == "direct_teach":
                        return "retraction is blocked while direct teach is active"
                    if arm_state not in {"standby", "retracting"}:
                        return (
                            "retraction adjustment requires controller state "
                            "standby or retracting"
                        )
                if request.direction_frame != DIRECTION_FRAME:
                    return "retraction adjustment requires direction_frame surgeon_view"
                if request.adjustment_mode == "single":
                    if request.target_retractor_id not in SINGLE_TARGETS:
                        return "single adjustment requires one malleable target"
                elif request.adjustment_mode == "multi":
                    if request.target_retractor_id != MULTI_TARGET:
                        return "multi adjustment requires both_malleable"
                else:
                    return "adjustment_mode must be single or multi"
            if request.operation == OP_TOOL_CHANGE:
                expected_arm_id = self._controller_arm_id_for_role("army_navy")
                if not expected_arm_id:
                    return "army_navy retraction role is unavailable"
                if request.arm_id not in ARM_IDS:
                    return "tool_change requires arm_id arm_1 or arm_2"
                if request.arm_id != expected_arm_id:
                    return "tool_change arm_id does not match controller role assignment"
                if request.target_tool_id not in TARGET_TOOL_IDS:
                    return "tool_change target_tool_id is unsupported"
                target_arm = self._controller_arms_by_role["army_navy"]
                if bool(target_arm.direct_teach_active) or target_arm.state != "standby":
                    return "tool_change requires the retraction arm to be standby"
                transitions = self._spec.get_bed_robot_arm_end_effector_transitions(
                    self._world.filtered_phase
                )
                transition_allowed = any(
                    transition.group_id == GROUP_RETRACTION
                    and (
                        getattr(transition, "target_tool_id", "")
                        or _TOOL_ALIASES.get(transition.to_profile, "")
                    )
                    == request.target_tool_id
                    for transition in transitions
                )
                if not transition_allowed:
                    return (
                        "requested tool transition is not allowed "
                        "for the current procedure phase"
                    )
        return ""

    @staticmethod
    def _is_idempotent(request: BedRobotArmGroupRequest, state: BedRobotArmGroupState | None) -> bool:
        # RequestToolChange.completed means the controller sequence completed;
        # it does not prove that a tool is physically attached. Never suppress
        # a later request by treating the previous target as mounted state.
        return False

    def _on_group_proposal(self, msg: BedRobotArmGroupActionProposal) -> None:
        pending = self._pending_retraction
        if pending is None:
            return
        request = pending.request
        command = msg.command
        if command.request_id != request.request_id:
            return
        if request.request_id in self._dispatched_request_ids:
            return
        rejection = self._proposal_guard_reason(request, msg)
        if rejection:
            self._pending_retraction = None
            self._publish_terminal(
                request,
                success=False,
                error_code="vlm_proposal_rejected",
                reason=rejection,
                command=command,
            )
            return

        approved = BedRobotArmGroupCommand()
        approved.stamp = self._stamp()
        approved.request_id = request.request_id
        approved.command_id = command.command_id.strip() or f"cmd-{uuid.uuid4().hex}"
        approved.group_id = GROUP_RETRACTION
        approved.operation = OP_ADJUSTMENT
        approved.adjustment_mode = request.adjustment_mode
        approved.target_retractor_id = request.target_retractor_id
        approved.direction_frame = DIRECTION_FRAME
        approved.direction = command.direction
        approved.axis = command.axis
        approved.distance_mm = float(command.distance_mm)
        approved.distance_origin = command.distance_origin
        approved.raw_distance_text = command.raw_distance_text
        state = self._group_states.get(GROUP_RETRACTION)
        approved.end_effector_profile = (
            request.end_effector_profile
            or (state.end_effector_profile if state is not None else "")
        )
        approved.rationale = command.rationale
        approved.confidence = float(command.confidence)
        self._pending_retraction = None
        self._inflight_commands[GROUP_RETRACTION] = approved
        self._dispatched_request_ids.add(request.request_id)
        self._command_pub.publish(approved)

    def _proposal_guard_reason(
        self,
        request: BedRobotArmGroupRequest,
        proposal: BedRobotArmGroupActionProposal,
    ) -> str:
        command = proposal.command
        request_guard = self._request_guard_reason(request)
        if request_guard:
            return f"request became invalid while awaiting VLM: {request_guard}"
        if not proposal.valid:
            return proposal.validation_error or "VLM marked proposal invalid"
        if command.group_id != GROUP_RETRACTION or command.operation != OP_ADJUSTMENT:
            return "VLM proposal must target one retraction group operation"
        if command.adjustment_mode != request.adjustment_mode:
            return "VLM adjustment_mode does not match the pending request"
        if command.target_retractor_id != request.target_retractor_id:
            return "VLM target_retractor_id does not match the pending request"
        if command.direction_frame != DIRECTION_FRAME:
            return "VLM direction_frame must be surgeon_view"
        if request.adjustment_mode == "single":
            if command.direction not in CARDINAL_DIRECTIONS or command.axis != "none":
                return "single adjustment requires a cardinal direction and axis none"
            proposal_direction = command.direction.upper()
        elif request.adjustment_mode == "multi":
            if command.direction != "none" or command.axis not in ADJUSTMENT_AXES:
                return "multi adjustment requires direction none and one documented axis"
            proposal_direction = command.axis.upper()
        else:
            return "unsupported retraction adjustment_mode"
        if proposal_direction not in RETRACTION_DIRECTIONS:
            return "VLM proposal direction is outside the six-value enum"
        spoken_direction = infer_retraction_direction(request.voice_text)
        if spoken_direction and spoken_direction != proposal_direction:
            return "VLM direction conflicts with the spoken direction"
        confidence_threshold = (
            self._confidence_threshold if spoken_direction else self._visual_direction_threshold
        )
        if float(command.confidence) < confidence_threshold:
            return f"VLM confidence is below {confidence_threshold:.2f}"
        if not spoken_direction and not command.rationale.strip():
            return "visual direction inference has no supporting rationale"
        raw_distance_text = command.raw_distance_text.strip()
        if command.distance_origin != "defaulted" and raw_distance_text:
            if self._voice_signature(raw_distance_text) not in self._voice_signature(request.voice_text):
                return "VLM raw distance text is not grounded in the original request"
        try:
            validate_retraction_distance_proposal(
                raw_distance_text=raw_distance_text,
                distance_mm=float(command.distance_mm),
                distance_origin=command.distance_origin,
            )
        except BedRobotArmGroupNormalizationError as exc:
            return f"deterministic distance validation failed: {exc}"
        try:
            source_normalized = normalize_retraction_request(
                request.voice_text,
                vlm_direction=proposal_direction,
                qualitative_distance_mm=(
                    float(command.distance_mm)
                    if command.distance_origin == "qualitative_inferred"
                    else None
                ),
            )
        except BedRobotArmGroupNormalizationError as exc:
            return f"source request distance validation failed: {exc}"
        if source_normalized.distance_origin != command.distance_origin:
            return (
                "VLM distance origin conflicts with the original request: "
                f"expected {source_normalized.distance_origin}"
            )
        if abs(source_normalized.distance_mm - float(command.distance_mm)) > 1e-6:
            return (
                "VLM distance conflicts with the original request: "
                f"expected {source_normalized.distance_mm:g} mm"
            )
        state = self._group_states.get(GROUP_RETRACTION)
        if state is None or not state.connected or state.state in self._RETRACTION_BUSY_STATES:
            return "retraction group became unavailable before proposal approval"
        return ""

    def _publish_command_from_request(self, request: BedRobotArmGroupRequest) -> None:
        command = BedRobotArmGroupCommand()
        command.stamp = self._stamp()
        command.request_id = request.request_id
        command.command_id = f"cmd-{uuid.uuid4().hex}"
        command.group_id = request.group_id
        command.operation = request.operation
        command.arm_id = request.arm_id
        command.target_tool_id = request.target_tool_id
        command.adjustment_mode = request.adjustment_mode
        command.target_retractor_id = request.target_retractor_id
        command.direction_frame = request.direction_frame
        command.direction = ""
        command.axis = ""
        command.distance_mm = 0.0
        command.distance_origin = ""
        command.raw_distance_text = ""
        command.end_effector_profile = (
            request.end_effector_profile
            if request.operation == OP_ADJUSTMENT
            else ""
        )
        command.rationale = "deterministic group routing"
        command.confidence = 1.0
        self._inflight_commands[request.group_id] = command
        self._dispatched_request_ids.add(request.request_id)
        self._command_pub.publish(command)

    def _publish_terminal(
        self,
        request: BedRobotArmGroupRequest,
        *,
        success: bool,
        outcome: str = "",
        error_code: str = "",
        reason: str = "",
        command: BedRobotArmGroupCommand | None = None,
    ) -> None:
        state = self._group_states.get(request.group_id)
        status = BedRobotArmGroupStatus()
        status.stamp = self._stamp()
        status.request_id = request.request_id
        status.command_id = command.command_id if command is not None else ""
        status.group_id = request.group_id
        status.operation = request.operation
        status.arm_id = command.arm_id if command is not None else request.arm_id
        status.target_tool_id = (
            command.target_tool_id if command is not None else request.target_tool_id
        )
        status.adjustment_mode = (
            command.adjustment_mode if command is not None else request.adjustment_mode
        )
        status.target_retractor_id = (
            command.target_retractor_id
            if command is not None
            else request.target_retractor_id
        )
        status.direction_frame = (
            command.direction_frame if command is not None else request.direction_frame
        )
        status.state = state.state if state is not None else "offline"
        status.outcome = outcome or ("succeeded" if success else "rejected")
        status.terminal = True
        status.success = bool(success)
        status.message = reason or status.outcome
        status.direction = command.direction if command is not None else ""
        status.axis = command.axis if command is not None else ""
        status.distance_mm = float(command.distance_mm) if command is not None else 0.0
        status.distance_origin = command.distance_origin if command is not None else ""
        status.raw_distance_text = command.raw_distance_text if command is not None else ""
        status.end_effector_profile = ""
        status.confidence = float(command.confidence) if command is not None else 1.0
        status.progress = 1.0
        status.elapsed_sec = 0.0
        status.remaining_sec = 0.0
        status.error_code = error_code
        status.rejection_reason = reason
        self._status_pub.publish(status)

    def _on_group_status(self, msg: BedRobotArmGroupStatus) -> None:
        if msg.group_id != GROUP_RETRACTION:
            return
        inflight = self._inflight_commands.get(msg.group_id)
        if (
            inflight is not None
            and msg.request_id
            and msg.request_id != inflight.request_id
        ):
            # Status generated for an idempotent/rejected newer request must
            # not mutate the state of the command already owning this lane.
            return
        state = self._group_states.get(msg.group_id)
        incoming_ns = int(msg.stamp.sec) * 1_000_000_000 + int(msg.stamp.nanosec)
        current_operation_ns = self._operation_status_ns.get(msg.group_id, 0)
        if incoming_ns and current_operation_ns and incoming_ns < current_operation_ns:
            if (
                inflight is not None
                and msg.terminal
                and msg.request_id == inflight.request_id
            ):
                self._inflight_commands.pop(msg.group_id, None)
            return
        if (
            inflight is not None
            and msg.terminal
            and msg.request_id == inflight.request_id
        ):
            self._inflight_commands.pop(msg.group_id, None)
        if state is None:
            if msg.terminal and self._pending_retraction is not None:
                if msg.request_id == self._pending_retraction.request.request_id:
                    self._pending_retraction = None
            return
        if state is not None:
            if (
                state.active_request_id
                and msg.request_id
                and msg.request_id != state.active_request_id
            ):
                # A terminal result for a rejected/idempotent second request
                # must not erase the controller state of the in-flight one.
                return
            is_health = not msg.operation and msg.request_id.startswith("health-")
            if is_health:
                # Legacy server-health messages are not controller-owned arm
                # state and cannot make the lane connected or standby.
                return
            if incoming_ns:
                self._operation_status_ns[msg.group_id] = incoming_ns
            state.operation = msg.operation
            state.target_tool_id = msg.target_tool_id
            state.adjustment_mode = msg.adjustment_mode
            state.target_retractor_id = msg.target_retractor_id
            state.direction_frame = msg.direction_frame
            state.direction = msg.direction
            state.axis = msg.axis
            state.distance_mm = float(msg.distance_mm)
            state.distance_origin = msg.distance_origin
            state.raw_distance_text = msg.raw_distance_text
            state.active_request_id = "" if msg.terminal else msg.request_id
            state.active_command_id = "" if msg.terminal else msg.command_id
            state.progress = float(msg.progress)
            state.error_code = msg.error_code
            control_cancelled = msg.outcome == "cancelled_by_runtime_control"
            state.error_message = (
                msg.message
                if msg.terminal and not msg.success and not control_cancelled
                else ""
            )
            state.rejection_reason = "" if control_cancelled else msg.rejection_reason
        if msg.terminal and self._pending_retraction is not None:
            if msg.request_id == self._pending_retraction.request.request_id:
                self._pending_retraction = None

    def _expire_pending_retraction(self) -> None:
        pending = self._pending_retraction
        if pending is None:
            return
        elapsed = time.monotonic() - pending.received_at
        if elapsed < self._vlm_proposal_timeout_sec:
            return
        self._pending_retraction = None
        self._publish_terminal(
            pending.request,
            success=False,
            error_code="vlm_timeout",
            reason=(
                "no matching VLM retraction proposal arrived within "
                f"{self._vlm_proposal_timeout_sec:g} seconds"
            ),
        )

    def _clear_runtime_state(self) -> None:
        # Invalidate the last running snapshot immediately.  A fresh world
        # state must arrive after every start/stop/reset before guards can pass.
        self._world = None
        self._bt_ready = False
        self._pending_retraction = None
        self._inflight_commands.clear()
        self._seen_request_ids.clear()
        self._dispatched_request_ids.clear()
        self._recent_voice_requests.clear()
        self._group_states.clear()
        self._controller_arms_by_role.clear()
        self._controller_status_received_at = 0.0
        self._controller_status_revision = None
        self._controller_status_source_stamp_ns = None
        self._controller_status_signature = None
        self._controller_status_epoch = 0
        self._operation_status_ns.clear()

    def _on_control(self, msg: String) -> None:
        command, _, detail = msg.data.partition(":")
        command = command.strip().lower()
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
        if command == "start":
            if self._bt_ready:
                return
            self._clear_runtime_state()
            self._bt_ready = True
        elif command == "start_runtime":
            self._clear_runtime_state()
        elif command == "start_actors":
            self._bt_ready = True
        elif command == "pause":
            self._bt_ready = False
        elif command == "resume":
            self._bt_ready = True
        elif command in {"stop", "reset"}:
            if command == "reset":
                self._last_lifecycle_control_signature = None
            self._clear_runtime_state()


def main() -> None:
    rclpy.init()
    node = BedRobotArmGroupOrchestrator()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
