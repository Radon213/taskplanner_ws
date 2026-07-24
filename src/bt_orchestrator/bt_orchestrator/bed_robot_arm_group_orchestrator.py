"""BT-side guards and routing for logical bed robot-arm groups.

This node is intentionally unaware of physical arm IDs, member counts, and
mounting positions.  It routes exactly one aggregate command to either the
``suction`` or ``retraction`` controller lane.
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
from surgical_msgs.msg import (
    BedRobotArmGroupActionProposal,
    BedRobotArmGroupCommand,
    BedRobotArmGroupRequest,
    BedRobotArmGroupState,
    BedRobotArmGroupStatus,
    WorldState,
)


GROUP_SUCTION = "suction"
GROUP_RETRACTION = "retraction"
OP_SUCTION_START = "suction_start"
OP_SUCTION_STOP = "suction_stop"
OP_RETRACTION = "retraction"
OP_RELEASE = "release_retraction"
OP_CHANGE = "change_end_effector"


@dataclass(slots=True)
class PendingRetraction:
    request: BedRobotArmGroupRequest
    received_at: float


class BedRobotArmGroupOrchestrator(Node):
    """Route deterministic suction and guarded VLM retraction requests."""

    _VOICE_DEDUP_SEC = 1.5
    _RETRACTION_BUSY_STATES = {
        "retracting",
        "releasing",
        "changing_end_effector",
        "approaching",
    }

    def __init__(self) -> None:
        super().__init__("bed_robot_arm_group_orchestrator")
        self.declare_parameter("spec_dir", str(get_default_spec_dir()))
        self.declare_parameter("vlm_confidence_threshold", 0.60)
        self.declare_parameter("visual_direction_confidence_threshold", 0.75)
        # Real VLM defaults to 20 seconds per attempt with two retries.  Leave
        # enough room for all three attempts plus transport/validation margin.
        self.declare_parameter("vlm_proposal_timeout_sec", 70.0)
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
        self._spec = load_bundle(self._spec_dir)
        self._bt_ready = False
        self._group_states: dict[str, BedRobotArmGroupState] = {}
        self._world: WorldState | None = None
        self._pending_retraction: PendingRetraction | None = None
        self._inflight_commands: dict[str, BedRobotArmGroupCommand] = {}
        self._seen_request_ids: set[str] = set()
        self._dispatched_request_ids: set[str] = set()
        self._recent_voice_requests: dict[str, tuple[str, float]] = {}
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
                self._clear_runtime_state()
            elif parameter.name == "vlm_confidence_threshold":
                self._confidence_threshold = float(parameter.value)
            elif parameter.name == "visual_direction_confidence_threshold":
                self._visual_direction_threshold = float(parameter.value)
            elif parameter.name == "vlm_proposal_timeout_sec":
                self._vlm_proposal_timeout_sec = max(0.5, float(parameter.value))
        return SetParametersResult(successful=True)

    @staticmethod
    def _voice_signature(text: str) -> str:
        return re.sub(r"[^0-9a-z가-힣]+", "", str(text).lower())

    @staticmethod
    def _utterance_matches(text: str, candidate: str) -> bool:
        left = BedRobotArmGroupOrchestrator._voice_signature(text)
        right = BedRobotArmGroupOrchestrator._voice_signature(candidate)
        # Scenario cues are exact surgeon lines.  Substring matching lets short
        # entries such as "석션" or "아미" shadow a longer stop/retraction
        # command, so free-form variants are handled by the deterministic
        # operation parser below instead.
        return bool(left and right and left == right)

    def _on_world(self, msg: WorldState) -> None:
        self._world = msg
        for incoming in msg.bed_robot_arm_groups:
            current = self._group_states.get(incoming.group_id)
            incoming_ns = int(incoming.stamp.sec) * 1_000_000_000 + int(
                incoming.stamp.nanosec
            )
            current_ns = (
                int(current.stamp.sec) * 1_000_000_000 + int(current.stamp.nanosec)
                if current is not None
                else -1
            )
            # Direct status is authoritative only until a newer twin snapshot
            # incorporates it.  This both rejects stale cross-topic snapshots
            # and lets the initial world supply the configured profile after a
            # profile-less action-server health heartbeat.
            if current is None or incoming_ns >= current_ns:
                self._group_states[incoming.group_id] = incoming
            elif (
                current is not None
                and not current.end_effector_profile
                and incoming.end_effector_profile
            ):
                # A profile-less controller health sample may arrive before
                # the first configured twin snapshot.  Seed only this static
                # field; never roll operational state or active IDs backward.
                current.end_effector_profile = incoming.end_effector_profile

    def _group_config(self, group_id: str):
        config = self._spec.get_bed_robot_arm_group_spec()
        if config is None:
            return None
        return next((group for group in config.groups if group.id == group_id), None)

    def _classify_voice(self, raw_text: str) -> tuple[str, str, str] | None:
        text = raw_text.strip().lower()
        if "석션" in text:
            stop_tokens = ("빼", "빠져", "스탑", "정지", "중지", "멈춰", "꺼")
            operation = OP_SUCTION_STOP if any(token in text for token in stop_tokens) else OP_SUCTION_START
            return GROUP_SUCTION, operation, "suction"
        if any(token in text for token in ("견인 해제", "리트랙션 해제", "당김 풀", "견인 풀")):
            return GROUP_RETRACTION, OP_RELEASE, ""
        if any(token in text for token in ("교체", "교환", "바꿔")):
            profiles = {
                "모스키토": "mosquito",
                "말레어블": "malleable",
                "메이요": "mayo",
                "아미": "army",
                "갑상선": "thyroid_retractor",
            }
            profile = next((value for token, value in profiles.items() if token in text), "")
            return GROUP_RETRACTION, OP_CHANGE, profile

        phase_id = self._world.filtered_phase if self._world is not None else ""
        transitions = self._spec.get_bed_robot_arm_end_effector_transitions(phase_id)
        for transition in transitions:
            if any(self._utterance_matches(raw_text, item) for item in transition.utterances):
                return transition.group_id, OP_CHANGE, transition.to_profile

        cues = self._spec.get_bed_robot_arm_group_cues(phase_id)
        for cue in cues:
            if any(self._utterance_matches(raw_text, item) for item in cue.utterances):
                return cue.group_id, cue.operation, cue.end_effector_profile

        if any(token in text for token in ("당겨", "견인", "리트랙션", "리트랙터")):
            state = self._group_states.get(GROUP_RETRACTION)
            profile = state.end_effector_profile if state is not None else ""
            return GROUP_RETRACTION, OP_RETRACTION, profile
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
        request.source = "deterministic_voice_router"
        self._recent_voice_requests[signature] = (request.request_id, now)
        self._request_pub.publish(request)

    def _on_group_request(self, msg: BedRobotArmGroupRequest) -> None:
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
                and (
                    msg.group_id == GROUP_SUCTION
                    or msg.operation == OP_RELEASE
                    or (
                        msg.operation == OP_CHANGE
                        and inflight.end_effector_profile
                        == msg.end_effector_profile
                    )
                )
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
            if msg.operation == OP_RELEASE:
                superseded = self._pending_retraction.request
                self._pending_retraction = None
                self._publish_terminal(
                    superseded,
                    success=False,
                    outcome="cancelled_by_newer_request",
                    error_code="request_superseded",
                    reason="pending VLM retraction was cancelled by a newer release request",
                )
            else:
                self._publish_terminal(
                    msg,
                    success=False,
                    error_code="request_in_flight",
                    reason="a retraction request is already awaiting VLM validation",
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

        if msg.operation == OP_RETRACTION:
            if self._pending_retraction is not None:
                self._publish_terminal(
                    msg,
                    success=False,
                    error_code="request_in_flight",
                    reason="a retraction request is already awaiting VLM validation or completion",
                )
                return
            self._pending_retraction = PendingRetraction(msg, now)
            return

        self._publish_command_from_request(msg)

    def _request_guard_reason(self, request: BedRobotArmGroupRequest) -> str:
        if request.group_id not in {GROUP_SUCTION, GROUP_RETRACTION}:
            return f"unsupported logical group '{request.group_id}'"
        group_config = self._group_config(request.group_id)
        if group_config is None or not group_config.enabled:
            return f"group '{request.group_id}' is disabled for procedure '{self._spec.procedure_id}'"
        if request.operation not in group_config.allowed_operations:
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
        if request.group_id == GROUP_RETRACTION:
            if request.operation == OP_RETRACTION and (
                active_conflict or state.state in self._RETRACTION_BUSY_STATES
            ):
                return f"retraction is blocked while group state is '{state.state}'"
            if request.operation in {OP_RELEASE, OP_CHANGE} and (
                active_conflict or state.state in self._RETRACTION_BUSY_STATES
            ):
                return f"group operation is blocked while retraction state is '{state.state}'"
            if request.operation == OP_RETRACTION:
                if not state.end_effector_profile:
                    return "retraction group has no active end-effector profile"
                if request.end_effector_profile and request.end_effector_profile != state.end_effector_profile:
                    return (
                        "requested end-effector profile does not match the active group profile; "
                        "perform change_end_effector first"
                    )
            if request.operation == OP_CHANGE and not request.end_effector_profile:
                return "change_end_effector requires a target group profile"
            if request.operation == OP_CHANGE:
                if state.state != "holding":
                    return "change_end_effector requires the retraction group to be holding"
                transitions = self._spec.get_bed_robot_arm_end_effector_transitions(
                    self._world.filtered_phase
                )
                transition_allowed = any(
                    transition.group_id == GROUP_RETRACTION
                    and transition.from_profile == state.end_effector_profile
                    and transition.to_profile == request.end_effector_profile
                    for transition in transitions
                )
                if not transition_allowed:
                    return (
                        "requested end-effector profile transition is not allowed "
                        "for the current procedure phase"
                    )
        return ""

    @staticmethod
    def _is_idempotent(request: BedRobotArmGroupRequest, state: BedRobotArmGroupState | None) -> bool:
        if state is None:
            return False
        if request.operation == OP_SUCTION_START:
            return state.state == "suctioning" or (
                state.operation == OP_SUCTION_START
                and bool(state.active_request_id or state.active_command_id)
            )
        if request.operation == OP_SUCTION_STOP:
            return state.state in {"standby", "stopping"} or (
                state.operation == OP_SUCTION_STOP
                and bool(state.active_request_id or state.active_command_id)
            )
        if request.operation == OP_RELEASE:
            return state.state == "standby" or (
                state.operation == OP_RELEASE
                and bool(state.active_request_id or state.active_command_id)
            )
        if request.operation == OP_CHANGE:
            return (
                state.state == "standby"
                and state.end_effector_profile == request.end_effector_profile
            ) or (
                state.operation == OP_CHANGE
                and state.end_effector_profile == request.end_effector_profile
                and bool(state.active_request_id or state.active_command_id)
            )
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
        approved.operation = OP_RETRACTION
        approved.direction = command.direction
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
        if command.group_id != GROUP_RETRACTION or command.operation != OP_RETRACTION:
            return "VLM proposal must target one retraction group operation"
        if command.direction not in RETRACTION_DIRECTIONS:
            return "VLM proposal direction is outside the six-value enum"
        spoken_direction = infer_retraction_direction(request.voice_text)
        if spoken_direction and spoken_direction != command.direction:
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
                vlm_direction=command.direction,
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
        if request.end_effector_profile and state.end_effector_profile != request.end_effector_profile:
            return "end-effector profile changed while request was pending"
        expected_profile = request.end_effector_profile or state.end_effector_profile
        if command.end_effector_profile and command.end_effector_profile != expected_profile:
            return "VLM end-effector profile does not match the active group profile"
        return ""

    def _publish_command_from_request(self, request: BedRobotArmGroupRequest) -> None:
        command = BedRobotArmGroupCommand()
        command.stamp = self._stamp()
        command.request_id = request.request_id
        command.command_id = f"cmd-{uuid.uuid4().hex}"
        command.group_id = request.group_id
        command.operation = request.operation
        command.direction = ""
        command.distance_mm = 0.0
        command.distance_origin = ""
        command.raw_distance_text = ""
        command.end_effector_profile = request.end_effector_profile
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
        status.state = state.state if state is not None else "offline"
        status.outcome = outcome or ("succeeded" if success else "rejected")
        status.terminal = True
        status.success = bool(success)
        status.message = reason or status.outcome
        status.direction = command.direction if command is not None else ""
        status.distance_mm = float(command.distance_mm) if command is not None else 0.0
        status.distance_origin = command.distance_origin if command is not None else ""
        status.raw_distance_text = command.raw_distance_text if command is not None else ""
        status.end_effector_profile = (
            state.end_effector_profile if state is not None else request.end_effector_profile
        )
        status.confidence = float(command.confidence) if command is not None else 1.0
        status.progress = 1.0
        status.elapsed_sec = 0.0
        status.remaining_sec = 0.0
        status.error_code = error_code
        status.rejection_reason = reason
        self._status_pub.publish(status)

    def _on_group_status(self, msg: BedRobotArmGroupStatus) -> None:
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
        if state is not None:
            incoming_ns = int(msg.stamp.sec) * 1_000_000_000 + int(
                msg.stamp.nanosec
            )
            current_ns = int(state.stamp.sec) * 1_000_000_000 + int(
                state.stamp.nanosec
            )
            if current_ns and incoming_ns < current_ns:
                # A matching terminal may be observed through the twin before
                # the direct status callback. It can close the local lane but
                # must never roll the newer aggregate state backward.
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
        if state is None and msg.group_id in {GROUP_SUCTION, GROUP_RETRACTION}:
            state = BedRobotArmGroupState()
            state.group_id = msg.group_id
            self._group_states[msg.group_id] = state
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
                available = (
                    msg.error_code != "server_unavailable"
                    and bool(msg.success)
                )
                next_state = (
                    "offline"
                    if not available
                    else (
                        (msg.state or "standby")
                        if state.state == "offline"
                        else state.state
                    )
                )
                changed = bool(
                    state.connected != available
                    or state.state != next_state
                )
                state.connected = available
                state.state = next_state
                # Availability is orthogonal to the most recent operational
                # result.  Preserve controller rejection/error metadata so a
                # ready heartbeat cannot make an error disappear.
                if changed:
                    state.stamp = msg.stamp
                return
            state.stamp = msg.stamp
            next_state = msg.state or state.state or "standby"
            state.connected = (
                msg.error_code != "server_unavailable"
                and next_state not in {"offline", "fault"}
            )
            state.state = next_state
            state.operation = msg.operation
            state.direction = msg.direction
            state.distance_mm = float(msg.distance_mm)
            state.distance_origin = msg.distance_origin
            state.raw_distance_text = msg.raw_distance_text
            if msg.end_effector_profile and msg.terminal and msg.success:
                state.end_effector_profile = msg.end_effector_profile
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

    def _on_control(self, msg: String) -> None:
        command = msg.data.partition(":")[0].strip().lower()
        if command == "start":
            self._clear_runtime_state()
            self._bt_ready = True
        elif command == "start_runtime":
            self._clear_runtime_state()
        elif command == "start_actors":
            self._bt_ready = True
        elif command in {"stop", "reset"}:
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
