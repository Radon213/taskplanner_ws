from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import time

from bt_orchestrator.bed_robot_arm_group_orchestrator import (
    BedRobotArmGroupOrchestrator,
    PendingRetractionAdjustment,
)


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
    router._command_pub = _CapturePublisher()
    router._request_pub = _CapturePublisher()
    router._status_pub = _CapturePublisher()
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
