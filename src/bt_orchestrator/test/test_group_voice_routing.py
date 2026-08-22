from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from procedure_spec import (
    NormalizedRetractionCommand,
    RetractionCommand,
    RetractionState,
    RetractionTargetSide,
)
from bt_orchestrator.bed_robot_arm_group_orchestrator import (
    BedRobotArmGroupOrchestrator,
    PendingRetractionAdjustment,
)
from bt_orchestrator.retractor_voice_interpreter import RetractionVoiceInterpretation


def test_start_heartbeat_does_not_clear_ready_runtime() -> None:
    router = BedRobotArmGroupOrchestrator.__new__(
        BedRobotArmGroupOrchestrator
    )
    router._bt_ready = True
    router._last_lifecycle_control_signature = None
    router._clear_runtime_state = lambda: (_ for _ in ()).throw(
        AssertionError("ready start heartbeat cleared runtime state")
    )

    router._on_control(SimpleNamespace(data="start"))
    router._on_control(SimpleNamespace(data="start"))

    assert router._bt_ready is True


def test_reset_is_repeatable_and_reopens_the_next_start_edge() -> None:
    router = BedRobotArmGroupOrchestrator.__new__(
        BedRobotArmGroupOrchestrator
    )
    router._bt_ready = False
    router._last_lifecycle_control_signature = None
    clear_count = 0

    def clear_runtime_state() -> None:
        nonlocal clear_count
        clear_count += 1
        router._bt_ready = False

    router._clear_runtime_state = clear_runtime_state

    router._on_control(SimpleNamespace(data="start"))
    router._on_control(SimpleNamespace(data="start"))
    router._on_control(SimpleNamespace(data="reset"))
    router._on_control(SimpleNamespace(data="reset"))
    router._on_control(SimpleNamespace(data="start"))

    assert clear_count == 4
    assert router._bt_ready is True
from procedure_spec import load_bundle
from std_msgs.msg import String
from surgical_interop_msgs.msg import BedRobotArmState, BedRobotArmStateArray
from surgical_msgs.msg import (
    BedRobotArmGroupActionProposal,
    BedRobotArmGroupCommand,
    BedRobotArmGroupRequest,
    BedRobotArmGroupState,
    BedRobotArmGroupStatus,
    VoiceCommandIntent,
    WorldState,
)


class _CapturePublisher:
    def __init__(self) -> None:
        self.messages = []

    def publish(self, message) -> None:
        self.messages.append(message)


def _spec_dir(name: str) -> Path:
    return (
        Path(__file__).parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
        / name
    )


def _controller_status(
    *,
    procedure: str = "thyroidectomy",
    revision: int = 1,
    stamp_sec: int = 10,
    stamp_nanosec: int = 0,
    state_name: str = "standby",
    thyroid_arm_id: str = "arm_1",
) -> BedRobotArmStateArray:
    status = BedRobotArmStateArray()
    status.stamp.sec = stamp_sec
    status.stamp.nanosec = stamp_nanosec
    status.revision = revision
    status.procedure_type = (
        "thyroidectomy"
        if procedure in {"thyroidectomy", "thyroidectomy_demo"}
        else procedure
    )
    role_layout = (
        [(thyroid_arm_id, "army_navy")]
        if status.procedure_type == "thyroidectomy"
        else [("arm_1", "left_malleable"), ("arm_2", "right_malleable")]
    )
    for arm_id, role_instance_id in role_layout:
        arm = BedRobotArmState()
        arm.arm_id = arm_id
        arm.role = "retraction"
        arm.role_instance_id = role_instance_id
        arm.state = state_name
        arm.direct_teach_active = state_name == "direct_teach"
        arm.reason_code = "ok"
        status.arms.append(arm)
    return status


def _router(
    procedure: str = "thyroidectomy",
    phase_id: str = "P04",
    *,
    state_name: str = "standby",
    thyroid_arm_id: str = "arm_1",
    with_controller_status: bool = True,
):
    router = BedRobotArmGroupOrchestrator.__new__(BedRobotArmGroupOrchestrator)
    router._spec = load_bundle(_spec_dir(procedure))
    router._bt_ready = True
    router._world = SimpleNamespace(
        running=True,
        execution_state="running",
        procedure_id=procedure,
        filtered_phase=phase_id,
    )
    state = BedRobotArmGroupState()
    state.group_id = "retraction"
    state.connected = False
    state.state = "unknown"
    router._group_states = {"retraction": state}
    router._pending_retraction = None
    router._inflight_commands = {}
    router._seen_request_ids = set()
    router._dispatched_request_ids = set()
    router._recent_voice_requests = {}
    # Existing raw-transcript cases below are compatibility tests.  Production
    # construction defaults this off; typed voice intents are the normal path.
    router._retractor_legacy_raw_voice_enabled = True
    router._retractor_voice_normalization_enabled = True
    router._retractor_voice_interpreter_mode = "deterministic"
    router._retractor_voice_state = RetractionState.IDLE
    router._normalized_retractor_requests = {}
    router._normalized_retractor_commands = {}
    router._normalized_retractor_command_requests = {}
    router._normalized_retractor_sources = {}
    router._pending_text_vlm_interpretations = {}
    router._command_pub = _CapturePublisher()
    router._request_pub = _CapturePublisher()
    router._status_pub = _CapturePublisher()
    router._retractor_voice_status_pub = _CapturePublisher()
    router._confidence_threshold = 0.6
    router._visual_direction_threshold = 0.75
    router._bed_robot_status_timeout_sec = 2.0
    router._bed_robot_source_max_age_sec = 10_000_000_000.0
    router._bed_robot_source_future_tolerance_sec = 0.5
    router._wall_time_ns = lambda: 40_000_000_000
    router._controller_arms_by_role = {}
    router._controller_status_received_at = 0.0
    router._controller_status_revision = None
    router._controller_status_source_stamp_ns = None
    router._controller_status_signature = None
    router._controller_status_epoch = 0
    router._operation_status_ns = {}
    router._stamp = lambda: BedRobotArmGroupStatus().stamp
    if with_controller_status:
        status = _controller_status(
            procedure=procedure,
            state_name=state_name,
            thyroid_arm_id=thyroid_arm_id,
        )
        router._on_controller_status(status)
        router._controller_status_received_at = time.monotonic()
    return router


def test_controller_status_rejects_stale_and_future_source_time() -> None:
    router = _router(with_controller_status=False)
    router._bed_robot_source_max_age_sec = 2.0
    router._wall_time_ns = lambda: 10_000_000_000

    router._on_controller_status(_controller_status(stamp_sec=7))
    router._on_controller_status(_controller_status(stamp_sec=11))

    assert router._controller_arms_by_role == {}
    assert router._controller_status_source_stamp_ns is None

    router._on_controller_status(_controller_status(stamp_sec=9))
    assert router._controller_status_source_stamp_ns == 9_000_000_000
    assert router._controller_status_guard() == ""


def test_controller_guard_rechecks_source_age_before_dispatch() -> None:
    router = _router()
    router._bed_robot_source_max_age_sec = 2.0
    router._wall_time_ns = lambda: 13_000_000_000

    assert (
        router._controller_status_guard()
        == "controller bed-arm source stamp is stale"
    )


def test_controller_restart_accepts_lower_revision_with_newer_source_stamp() -> None:
    router = _router(state_name="retracting", thyroid_arm_id="arm_1")
    router._on_controller_status(
        _controller_status(
            revision=8,
            stamp_sec=20,
            state_name="retracting",
            thyroid_arm_id="arm_1",
        )
    )
    current = router._group_states["retraction"]
    current.operation = "change_end_effector"
    current.active_request_id = "req-active"
    current.active_command_id = "cmd-active"
    current.error_code = "old_epoch_error"
    inflight = BedRobotArmGroupCommand()
    inflight.request_id = "req-active"
    inflight.command_id = "cmd-active"
    inflight.group_id = "retraction"
    inflight.operation = "change_end_effector"
    router._inflight_commands["retraction"] = inflight

    router._on_controller_status(
        _controller_status(
            revision=1,
            stamp_sec=30,
            state_name="standby",
            thyroid_arm_id="arm_2",
        )
    )

    state = router._group_states["retraction"]
    assert router._controller_status_epoch == 1
    assert router._controller_status_revision == 1
    assert router._controller_status_source_stamp_ns == 30_000_000_000
    assert router._controller_arms_by_role["army_navy"].arm_id == "arm_2"
    assert state.connected is True
    assert state.state == "standby"
    assert state.arm_id == "arm_2"
    assert state.operation == ""
    assert state.active_request_id == ""
    assert state.active_command_id == ""
    assert state.error_code == ""
    assert router._inflight_commands["retraction"] is inflight


def test_controller_restart_keeps_source_stamp_as_cross_epoch_ordering_fence() -> None:
    router = _router()
    router._on_controller_status(
        _controller_status(revision=9, stamp_sec=20, state_name="retracting")
    )
    router._on_controller_status(
        _controller_status(
            revision=1,
            stamp_sec=30,
            state_name="standby",
            thyroid_arm_id="arm_2",
        )
    )

    # Delayed old-epoch state has a larger revision but an older source stamp.
    router._on_controller_status(
        _controller_status(
            revision=10,
            stamp_sec=25,
            state_name="fault",
            thyroid_arm_id="arm_1",
        )
    )
    # A changed payload with the same revision is not a valid heartbeat.
    router._on_controller_status(
        _controller_status(
            revision=1,
            stamp_sec=31,
            state_name="fault",
            thyroid_arm_id="arm_2",
        )
    )

    state = router._group_states["retraction"]
    assert router._controller_status_epoch == 1
    assert router._controller_status_revision == 1
    assert router._controller_status_source_stamp_ns == 30_000_000_000
    assert state.state == "standby"
    assert state.arm_id == "arm_2"

    # An unchanged same-revision heartbeat is accepted only to refresh source time.
    router._on_controller_status(
        _controller_status(
            revision=1,
            stamp_sec=32,
            state_name="standby",
            thyroid_arm_id="arm_2",
        )
    )
    assert router._controller_status_source_stamp_ns == 32_000_000_000
    assert router._controller_status_epoch == 1


def _request(
    request_id: str = "req-1",
    *,
    operation: str = "retraction",
    voice_text: str = "왼쪽 말레어블을 오른쪽으로 10 mm 당겨줘",
    procedure: str = "nephrectomy",
    phase_id: str = "P02",
) -> BedRobotArmGroupRequest:
    request = BedRobotArmGroupRequest()
    request.request_id = request_id
    request.group_id = "retraction"
    request.operation = operation
    request.voice_text = voice_text
    request.procedure_id = procedure
    request.phase_id = phase_id
    request.adjustment_mode = "single"
    request.target_retractor_id = "left_malleable"
    request.direction_frame = "surgeon_view"
    request.end_effector_profile = "left_malleable"
    return request


def _proposal(**overrides) -> BedRobotArmGroupActionProposal:
    proposal = BedRobotArmGroupActionProposal()
    proposal.valid = True
    command = proposal.command
    command.request_id = "req-1"
    command.command_id = "vlm-req-1"
    command.group_id = "retraction"
    command.operation = "retraction"
    command.adjustment_mode = "single"
    command.target_retractor_id = "left_malleable"
    command.direction_frame = "surgeon_view"
    command.direction = "right"
    command.axis = "none"
    command.distance_mm = 10.0
    command.distance_origin = "explicit_with_unit"
    command.raw_distance_text = "10 mm"
    command.end_effector_profile = "left_malleable"
    command.rationale = "spoken direction"
    command.confidence = 0.95
    for field, value in overrides.items():
        setattr(command, field, value)
    return proposal


def test_clinical_suction_speech_is_not_a_bed_robot_arm_command() -> None:
    router = _router()

    for utterance in (
        "석션 주세요",
        "석션 빼 주세요",
        "Yankauer suction please",
        "air suction",
    ):
        assert router._classify_voice(utterance) is None


def test_tool_change_and_fine_adjustment_are_distinct_voice_routes() -> None:
    router = _router()

    assert router._classify_voice("1번 암을 아미로 교체해줘") == (
        "retraction",
        "change_end_effector",
        "army_navy_retractor",
    )
    assert router._classify_voice("아미를 위로 조금 당겨줘") == (
        "retraction",
        "retraction",
        "",
    )


def test_voice_router_populates_documented_tool_change_fields() -> None:
    router = _router()
    message = String()
    message.data = "2번 암을 아미로 교체해줘"

    router._on_voice(message)

    request = router._request_pub.messages[-1]
    assert request.operation == "change_end_effector"
    assert request.arm_id == "arm_1"
    assert request.target_tool_id == "army_navy_retractor"
    assert request.adjustment_mode == ""


def test_thyroid_tool_change_uses_observed_role_assignment_not_spoken_arm() -> None:
    router = _router(thyroid_arm_id="arm_2")
    message = String()
    message.data = "1번 암을 아미로 교체해줘"

    router._on_voice(message)

    request = router._request_pub.messages[-1]
    assert request.arm_id == "arm_2"
    assert router._request_guard_reason(request) == ""


def test_voice_router_populates_single_and_multi_adjustment_fields() -> None:
    router = _router("nephrectomy", "P02")
    single = String()
    single.data = "왼쪽 말레어블을 오른쪽으로 10 mm 당겨줘"
    router._on_voice(single)

    single_request = router._request_pub.messages[-1]
    assert single_request.adjustment_mode == "single"
    assert single_request.target_retractor_id == "left_malleable"
    assert single_request.direction_frame == "surgeon_view"

    multi = String()
    multi.data = "양측 말레어블을 좌우로 10 mm 당겨줘"
    router._on_voice(multi)

    multi_request = router._request_pub.messages[-1]
    assert multi_request.adjustment_mode == "multi"
    assert multi_request.target_retractor_id == "both_malleable"
    assert multi_request.direction_frame == "surgeon_view"


def test_world_mirror_cannot_claim_controller_state_or_mounted_profile() -> None:
    router = _router(with_controller_status=False)
    retraction = BedRobotArmGroupState()
    retraction.group_id = "retraction"
    retraction.connected = True
    retraction.state = "standby"
    retraction.arm_id = "arm_1"
    retraction.end_effector_profile = "army_navy_retractor"
    retraction.operation = "change_end_effector"
    retraction.target_tool_id = "army_navy_retractor"
    retraction.stamp.sec = 20
    unsupported = BedRobotArmGroupState()
    unsupported.group_id = "unsupported"
    unsupported.stamp.sec = 30
    world = WorldState()
    world.bed_robot_arm_groups = [unsupported, retraction]

    router._on_world(world)

    observed = router._group_states["retraction"]
    assert set(router._group_states) == {"retraction"}
    assert observed.connected is False
    assert observed.state == "unknown"
    assert observed.arm_id == ""
    assert observed.end_effector_profile == ""
    assert observed.operation == ""
    assert observed.target_tool_id == ""


def test_stale_world_snapshot_cannot_rollback_retraction_state() -> None:
    router = _router()
    current = router._group_states["retraction"]
    current.active_request_id = "req-active"
    stale = BedRobotArmGroupState()
    stale.group_id = "retraction"
    stale.connected = True
    stale.state = "standby"
    stale.stamp.sec = 10
    world = WorldState()
    world.bed_robot_arm_groups = [stale]

    router._on_world(world)

    observed = router._group_states["retraction"]
    assert observed.state == "standby"
    assert observed.connected is True
    assert observed.arm_id == "arm_1"


def test_documented_tool_change_is_allowed_and_bypasses_vlm() -> None:
    router = _router()
    request = _request(
        operation="change_end_effector",
        voice_text="1번 암을 아미로 교체해줘",
        procedure="thyroidectomy",
        phase_id="P04",
    )
    request.arm_id = "arm_1"
    request.target_tool_id = "army_navy_retractor"
    request.adjustment_mode = ""
    request.target_retractor_id = ""
    request.direction_frame = ""
    request.end_effector_profile = "army_navy_retractor"

    assert router._request_guard_reason(request) == ""
    router._on_group_request(request)

    assert router._pending_retraction is None
    command = router._command_pub.messages[-1]
    assert command.operation == "change_end_effector"
    assert command.arm_id == "arm_1"
    assert command.target_tool_id == "army_navy_retractor"


def test_tool_change_rejects_fields_outside_the_document_enum() -> None:
    router = _router()
    request = _request(
        operation="change_end_effector",
        procedure="thyroidectomy",
        phase_id="P04",
    )
    request.arm_id = "arm_3"
    request.target_tool_id = "custom_retractor"

    assert "arm_id" in router._request_guard_reason(request)
    request.arm_id = "arm_1"
    assert "target_tool_id" in router._request_guard_reason(request)


def test_documented_single_and_multi_adjustments_wait_for_vlm() -> None:
    single_router = _router("nephrectomy", "P02")
    single = _request()
    assert single_router._request_guard_reason(single) == ""
    single_router._on_group_request(single)
    assert single_router._pending_retraction is not None
    assert single_router._command_pub.messages == []

    multi_router = _router("nephrectomy", "P02")
    multi = _request(
        voice_text="양측 말레어블을 상하로 10 mm 당겨줘",
    )
    multi.adjustment_mode = "multi"
    multi.target_retractor_id = "both_malleable"
    multi.end_effector_profile = "both_malleable"
    assert multi_router._request_guard_reason(multi) == ""


def test_adjustment_is_allowed_while_controller_reports_retracting() -> None:
    router = _router("nephrectomy", "P02", state_name="retracting")

    assert router._request_guard_reason(_request()) == ""


def test_request_fails_closed_until_controller_status_is_observed() -> None:
    router = _router("nephrectomy", "P02", with_controller_status=False)

    reason = router._request_guard_reason(_request())

    assert "not connected" in reason or "not been observed" in reason
    state = router._group_states["retraction"]
    assert state.connected is False
    assert state.state == "unknown"


def test_adjustment_guard_rejects_mismatched_mode_target_and_frame() -> None:
    router = _router("nephrectomy", "P02")
    request = _request()
    request.target_retractor_id = "both_malleable"
    assert "one malleable target" in router._request_guard_reason(request)
    request.target_retractor_id = "left_malleable"
    request.direction_frame = "robot_base"
    assert "surgeon_view" in router._request_guard_reason(request)


def _proposal_guard_router():
    router = _router("nephrectomy", "P02", state_name="retracting")
    router._request_guard_reason = lambda _request: ""
    return router


def test_bt_revalidates_distance_against_original_utterance() -> None:
    request = _request(voice_text="오른쪽으로 50 mm 당겨줘")
    proposal = _proposal(
        raw_distance_text="10 mm",
        distance_mm=10.0,
        distance_origin="explicit_with_unit",
    )

    reason = _proposal_guard_router()._proposal_guard_reason(request, proposal)

    assert "original request" in reason


def test_bt_rejects_vlm_mode_or_target_rewrite() -> None:
    request = _request()
    reason = _proposal_guard_router()._proposal_guard_reason(
        request,
        _proposal(adjustment_mode="multi", target_retractor_id="both_malleable"),
    )
    assert "adjustment_mode" in reason


def test_bt_accepts_documented_multi_axis_and_dispatches_it() -> None:
    router = _proposal_guard_router()
    router._group_states["retraction"].end_effector_profile = "both_malleable"
    request = _request(voice_text="양측 말레어블을 상하로 10 mm 당겨줘")
    request.adjustment_mode = "multi"
    request.target_retractor_id = "both_malleable"
    request.end_effector_profile = "both_malleable"
    proposal = _proposal(
        adjustment_mode="multi",
        target_retractor_id="both_malleable",
        direction="none",
        axis="up_down",
        end_effector_profile="both_malleable",
    )
    router._pending_retraction = PendingRetractionAdjustment(request, 0.0)

    assert router._proposal_guard_reason(request, proposal) == ""
    router._on_group_proposal(proposal)

    command = router._command_pub.messages[-1]
    assert command.adjustment_mode == "multi"
    assert command.target_retractor_id == "both_malleable"
    assert command.direction == "none"
    assert command.axis == "up_down"


def test_second_adjustment_is_rejected_while_lane_is_pending() -> None:
    router = _router("nephrectomy", "P02", state_name="retracting")
    first = _request("req-first")
    router._pending_retraction = PendingRetractionAdjustment(first, 0.0)

    router._on_group_request(_request("req-second"))

    rejected = router._status_pub.messages[-1]
    assert rejected.request_id == "req-second"
    assert rejected.success is False
    assert rejected.error_code == "request_in_flight"


def test_completed_tool_change_does_not_cache_mount_or_skip_retry() -> None:
    router = _router()
    first = _request(
        "req-tool-1",
        operation="change_end_effector",
        voice_text="아미로 교체해줘",
        procedure="thyroidectomy",
        phase_id="P04",
    )
    first.arm_id = "arm_1"
    first.target_tool_id = "army_navy_retractor"
    first.adjustment_mode = ""
    first.target_retractor_id = ""
    first.direction_frame = ""

    router._on_group_request(first)
    first_command = router._command_pub.messages[-1]
    completed = BedRobotArmGroupStatus()
    completed.request_id = first.request_id
    completed.command_id = first_command.command_id
    completed.group_id = "retraction"
    completed.operation = "change_end_effector"
    completed.target_tool_id = "army_navy_retractor"
    completed.end_effector_profile = "army_navy_retractor"
    completed.terminal = True
    completed.success = True
    router._on_group_status(completed)

    state = router._group_states["retraction"]
    assert state.end_effector_profile == ""

    retry = _request(
        "req-tool-2",
        operation="change_end_effector",
        voice_text="아미로 다시 교체해줘",
        procedure="thyroidectomy",
        phase_id="P04",
    )
    retry.arm_id = "arm_1"
    retry.target_tool_id = "army_navy_retractor"
    retry.adjustment_mode = ""
    retry.target_retractor_id = ""
    retry.direction_frame = ""
    router._on_group_request(retry)

    assert [item.request_id for item in router._command_pub.messages] == [
        "req-tool-1",
        "req-tool-2",
    ]


def test_non_retraction_status_cannot_create_a_state_entry() -> None:
    router = _router()
    status = BedRobotArmGroupStatus()
    status.group_id = "unsupported"
    status.state = "running"

    router._on_group_status(status)

    assert set(router._group_states) == {"retraction"}


def _dispatch_normalized_voice(router, transcript: str) -> BedRobotArmGroupCommand:
    message = String()
    message.data = transcript
    router._on_voice(message)
    request = router._request_pub.messages[-1]
    router._on_group_request(request)
    return router._command_pub.messages[-1]


def _typed_retractor_intent(
    *,
    retractor_command: str = "start_direct_teach",
    raw_text: str = "교시 시작",
    intent: str = "retractor_command",
    disposition: str = "propose",
    requires_confirmation: bool = False,
    target_side: str = "none",
    distance_m: float = 0.0,
    tool_id: str = "",
    procedure_id: str = "nephrectomy",
    catalog_id: str = "",
    urgency: str = "",
    provenance: str = "voice_intent_resolver.v1",
) -> VoiceCommandIntent:
    """Generated-message fixture for the typed voice-control boundary."""

    message = VoiceCommandIntent()
    message.intent = intent
    message.retractor_command = retractor_command
    message.raw_text = raw_text
    message.normalized_text = raw_text
    message.procedure_id = procedure_id
    message.catalog_id = catalog_id
    message.disposition = disposition
    message.requires_confirmation = requires_confirmation
    message.target_side = target_side
    message.distance_m = distance_m
    message.tool_id = tool_id
    message.urgency = urgency
    message.provenance = provenance
    return message


def test_typed_natural_teach_start_reaches_retractor_service_lane() -> None:
    router = _router("nephrectomy", "P02")
    router._retractor_legacy_raw_voice_enabled = False

    # The free-form phrase did not need to contain "직접".  That semantic
    # expansion belongs to the central resolver; this consumer only accepts
    # its explicit, typed proposal.
    router._on_voice_intent(
        _typed_retractor_intent(raw_text="자 이제 교시를 시작해보자")
    )

    request = router._request_pub.messages[-1]
    assert request.operation == "start_direct_teach"
    assert request.voice_text == "자 이제 교시를 시작해보자"
    assert request.source.endswith(":voice_command_intent")
    router._on_group_request(request)

    command = router._command_pub.messages[-1]
    assert command.operation == "start_direct_teach"
    assert command.rationale.startswith(
        "retractor_voice_normalizer:voice_command_intent"
    )


def test_raw_string_and_generic_missing_voice_intents_do_not_trigger_retractor(
) -> None:
    router = _router("nephrectomy", "P02")
    router._retractor_legacy_raw_voice_enabled = False

    # Raw STT is no longer an action input.  It is consumed by the central
    # resolver, which will publish a typed clarify/no-command instead.
    for transcript in ("교시 시작", "도구 줘", "자 이제 시작해보자"):
        router._on_voice(SimpleNamespace(data=transcript))
    assert router._request_pub.messages == []

    # A missing object must stay a clarification; no state-based filling or
    # next-tool guess is allowed in the BT adapter.
    router._on_voice_intent(
        _typed_retractor_intent(
            intent="",
            retractor_command="",
            raw_text="도구 줘",
            disposition="clarify",
        )
    )
    # Confirmation-required proposals are equally non-actuating here.
    router._on_voice_intent(
        _typed_retractor_intent(
            raw_text="교시 시작",
            requires_confirmation=True,
        )
    )

    assert router._request_pub.messages == []
    statuses = [
        json.loads(item.data) for item in router._retractor_voice_status_pub.messages
    ]
    assert [item["stage"] for item in statuses] == [
        "typed_intent_rejected",
        "typed_intent_rejected",
    ]
    assert statuses[0]["reason"] == "typed_intent_is_not_retractor_command"
    assert statuses[1]["reason"] == "typed_intent_requires_confirmation"


def test_typed_retractor_intent_rejects_wrong_slots_and_local_state() -> None:
    router = _router("nephrectomy", "P02")
    router._retractor_legacy_raw_voice_enabled = False

    # A lifecycle command cannot smuggle a physical slot through the typed
    # schema, and a start-retraction command is disallowed before teach is
    # admitted into the local state machine.
    router._on_voice_intent(
        _typed_retractor_intent(target_side="left", distance_m=0.01)
    )
    router._on_voice_intent(
        _typed_retractor_intent(
            retractor_command="start_retraction",
            raw_text="견인 시작",
        )
    )

    assert router._request_pub.messages == []
    statuses = [
        json.loads(item.data) for item in router._retractor_voice_status_pub.messages
    ]
    assert statuses[0]["reason"] == "typed_intent_nonadjustment_has_physical_slots"
    assert statuses[1]["reason"] == "typed_intent_command_not_allowed_in_local_state"


@pytest.mark.parametrize(
    ("procedure_id", "world_procedure_id", "expected_reason"),
    [
        ("", None, "typed_intent_procedure_id_is_missing"),
        (
            "thyroidectomy",
            None,
            "typed_intent_procedure_id_mismatches_loaded_spec",
        ),
        (
            "nephrectomy",
            "thyroidectomy",
            "typed_intent_procedure_id_mismatches_current_world",
        ),
    ],
)
def test_typed_retractor_intent_rejects_stale_or_unbound_procedure(
    procedure_id: str,
    world_procedure_id: str | None,
    expected_reason: str,
) -> None:
    router = _router("nephrectomy", "P02")
    router._retractor_legacy_raw_voice_enabled = False
    if world_procedure_id is not None:
        router._world.procedure_id = world_procedure_id

    router._on_voice_intent(
        _typed_retractor_intent(procedure_id=procedure_id)
    )

    assert router._request_pub.messages == []
    status = json.loads(router._retractor_voice_status_pub.messages[-1].data)
    assert status["stage"] == "typed_intent_rejected"
    assert status["reason"] == expected_reason


def _service_admission(
    router,
    command: BedRobotArmGroupCommand,
    *,
    accepted: bool,
) -> None:
    status = BedRobotArmGroupStatus()
    status.request_id = command.request_id
    status.command_id = command.command_id
    status.group_id = "retraction"
    status.operation = command.operation
    status.terminal = True
    status.success = accepted
    status.outcome = "accepted" if accepted else "rejected"
    status.message = "request_accepted" if accepted else "request_rejected"
    router._on_group_status(status)


def test_six_retractor_voice_commands_dispatch_through_one_service_lane() -> None:
    router = _router("nephrectomy", "P02")

    direct_start = _dispatch_normalized_voice(router, "직접 교시 시작")
    assert direct_start.operation == "start_direct_teach"
    assert router._retractor_voice_state == RetractionState.IDLE
    _service_admission(router, direct_start, accepted=True)
    assert router._retractor_voice_state == RetractionState.DIRECT_TEACHING

    direct_finish = _dispatch_normalized_voice(router, "직접 교시 종료")
    assert direct_finish.operation == "finish_direct_teach"
    _service_admission(router, direct_finish, accepted=True)
    assert router._retractor_voice_state == RetractionState.TAUGHT_READY

    retraction_start = _dispatch_normalized_voice(router, "Retraction 시작")
    assert retraction_start.operation == "start_retraction"
    _service_admission(router, retraction_start, accepted=True)
    assert router._retractor_voice_state == RetractionState.RETRACTION_ACTIVE

    adjustment = _dispatch_normalized_voice(router, "Retraction 오른쪽 5cm 더")
    assert adjustment.operation == "retraction"
    assert adjustment.target_retractor_id == "right_malleable"
    assert adjustment.direction == "right"
    assert adjustment.axis == "none"
    assert adjustment.distance_mm == 50.0
    _service_admission(router, adjustment, accepted=True)
    assert router._retractor_voice_state == RetractionState.RETRACTION_ACTIVE

    retraction_stop = _dispatch_normalized_voice(router, "Retraction 종료")
    assert retraction_stop.operation == "stop_retraction"
    _service_admission(router, retraction_stop, accepted=True)
    assert router._retractor_voice_state == RetractionState.IDLE

    tool_change = _dispatch_normalized_voice(router, "Tool change")
    assert tool_change.operation == "change_end_effector"
    assert tool_change.target_tool_id == ""
    _service_admission(router, tool_change, accepted=True)
    assert router._retractor_voice_state == RetractionState.IDLE

    event = json.loads(router._retractor_voice_status_pub.messages[-1].data)
    assert event["stage"] == "service_admitted"
    assert event["interpreter_source"] == "deterministic"
    assert event["vlm_invoked"] is False
    assert event["state"] == "idle"


def test_voice_state_does_not_advance_for_non_admission_status() -> None:
    router = _router("nephrectomy", "P02")
    command = _dispatch_normalized_voice(router, "직접 교시 시작")

    _service_admission(router, command, accepted=False)

    assert router._retractor_voice_state == RetractionState.IDLE
    event = json.loads(router._retractor_voice_status_pub.messages[-1].data)
    assert event["stage"] == "service_not_admitted"
    assert event["vlm_invoked"] is False


class _ImmediateFuture:
    def __init__(self, result) -> None:
        self._result = result

    def done(self) -> bool:
        return True

    def result(self):
        return self._result


class _ImmediateExecutor:
    def __init__(self, result) -> None:
        self._result = result

    def submit(self, *_args, **_kwargs):
        return _ImmediateFuture(self._result)


class _StaticInterpreter:
    def __init__(self, result) -> None:
        self._result = result

    def interpret(self, *_args, **_kwargs):
        return self._result


class _FailingExecutor:
    def submit(self, *_args, **_kwargs):
        raise RuntimeError("executor closed")


@pytest.mark.parametrize(
    ("transcript", "state", "command", "operation"),
    [
        (
            "리트렉터 직접 가르치기 모드 켜줘",
            RetractionState.IDLE,
            RetractionCommand.START_DIRECT_TEACH,
            "start_direct_teach",
        ),
        (
            "가르치기 이제 다 됐어",
            RetractionState.DIRECT_TEACHING,
            RetractionCommand.FINISH_DIRECT_TEACH,
            "finish_direct_teach",
        ),
        (
            "이제 견인 들어가자",
            RetractionState.TAUGHT_READY,
            RetractionCommand.START_RETRACTION,
            "start_retraction",
        ),
        (
            "오른쪽으로 한 번 더 당겨",
            RetractionState.RETRACTION_ACTIVE,
            RetractionCommand.ADJUST_RETRACTION,
            "retraction",
        ),
        (
            "장비 다른 걸로 바꿔줘",
            RetractionState.IDLE,
            RetractionCommand.CHANGE_TOOL,
            "change_end_effector",
        ),
        (
            "견인은 여기서 끝내",
            RetractionState.RETRACTION_ACTIVE,
            RetractionCommand.STOP_RETRACTION,
            "stop_retraction",
        ),
    ],
)
def test_fuzzy_demo_corpus_reaches_text_vlm_before_legacy_router(
    transcript: str,
    state: RetractionState,
    command: RetractionCommand,
    operation: str,
) -> None:
    router = _router("nephrectomy", "P02")
    router._retractor_voice_state = state
    router._retractor_voice_interpreter_mode = "vlm_with_fallback"
    target_side = (
        RetractionTargetSide.RIGHT
        if command == RetractionCommand.ADJUST_RETRACTION
        else RetractionTargetSide.NONE
    )
    result = RetractionVoiceInterpretation(
        normalized=NormalizedRetractionCommand(
            command=command,
            target_side=target_side,
            distance_m=(0.050 if command == RetractionCommand.ADJUST_RETRACTION else 0.0),
            confidence=0.80,
            reason="normalized_text_vlm_grounded",
        ),
        interpreter_source="text_vlm",
        vlm_invoked=True,
        detail="text_vlm_normalized",
    )
    router._retractor_voice_interpreter = _StaticInterpreter(result)
    router._retractor_voice_executor = _ImmediateExecutor(result)

    message = String()
    message.data = transcript
    router._on_voice(message)
    router._drain_text_vlm_interpretations()

    request = router._request_pub.messages[-1]
    assert request.operation == operation
    assert request.source.endswith(":text_vlm")


def test_text_vlm_result_is_routed_with_explicit_provenance() -> None:
    router = _router("nephrectomy", "P02")
    router._retractor_voice_state = RetractionState.TAUGHT_READY
    router._retractor_voice_interpreter_mode = "vlm_with_fallback"
    result = RetractionVoiceInterpretation(
        normalized=NormalizedRetractionCommand(
            command=RetractionCommand.START_RETRACTION,
            target_side=RetractionTargetSide.NONE,
            distance_m=0.0,
            confidence=0.80,
            reason="normalized_text_vlm",
        ),
        interpreter_source="text_vlm",
        vlm_invoked=True,
        detail="text_vlm_normalized",
    )
    router._retractor_voice_interpreter = _StaticInterpreter(result)
    router._retractor_voice_executor = _ImmediateExecutor(result)

    message = String()
    message.data = "Retraction 시작"
    router._on_voice(message)
    router._drain_text_vlm_interpretations()

    request = router._request_pub.messages[-1]
    assert request.operation == "start_retraction"
    assert request.source.endswith(":text_vlm")
    event = json.loads(router._retractor_voice_status_pub.messages[-1].data)
    assert event["stage"] == "normalized"
    assert event["interpreter_source"] == "text_vlm"
    assert event["vlm_invoked"] is True


def test_text_vlm_submit_failure_falls_back_without_claiming_invocation() -> None:
    router = _router("nephrectomy", "P02")
    router._retractor_voice_interpreter_mode = "vlm_with_fallback"
    router._retractor_voice_interpreter = _StaticInterpreter(None)
    router._retractor_voice_executor = _FailingExecutor()

    message = String()
    message.data = "직접 교시 시작"
    router._on_voice(message)

    request = router._request_pub.messages[-1]
    assert request.operation == "start_direct_teach"
    assert request.source.endswith(":deterministic_fallback")
    event = json.loads(router._retractor_voice_status_pub.messages[-1].data)
    assert event["interpreter_source"] == "deterministic_fallback"
    assert event["vlm_invoked"] is False
    assert event["detail"] == "text_vlm_submit_error:RuntimeError"
